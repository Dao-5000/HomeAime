// public/js/voice_call.js
// 实时语音通话前端模块 v1.0
// 完全独立，不依赖任何现有模块，只读 Chat.contact / Store / Session

'use strict';

const VoiceCall = (() => {

  // ── AudioWorklet processor：采集 → 重采样16k → 算RMS → 每64ms回传 PCM + rms ──
  // 移植自 pai-voice（AGPL-3.0）：一条麦克风流同时供 VAD 与识别用，原声不落盘。
  const WORKLET_SRC = `
class PcmCapture extends AudioWorkletProcessor {
  constructor() { super(); this.buf = []; this.len = 0; this.acc = 0; this.ratio = sampleRate / 16000; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    const out = [];
    for (let i = 0; i < ch.length; i++) {
      this.acc += 1;
      if (this.acc >= this.ratio) { this.acc -= this.ratio; out.push(ch[i]); }
    }
    let sum = 0;
    for (let i = 0; i < ch.length; i++) sum += ch[i] * ch[i];
    const rms = Math.sqrt(sum / ch.length);
    const i16 = new Int16Array(out.length);
    for (let i = 0; i < out.length; i++) { const v = Math.max(-1, Math.min(1, out[i])); i16[i] = v < 0 ? v * 32768 : v * 32767; }
    this.buf.push(i16); this.len += i16.length;
    if (this.len >= 1024) {
      const all = new Int16Array(this.len); let o = 0;
      for (const b of this.buf) { all.set(b, o); o += b.length; }
      this.buf = []; this.len = 0;
      this.port.postMessage({ pcm: all.buffer, rms }, [all.buffer]);
    } else {
      this.port.postMessage({ rms });
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
`;

  // 特效总开关（声波律动 + 气泡），读 localStorage: setting_vc_effects
  function _effectsEnabled() {
    return false;
    try {
      const v = localStorage.getItem('setting_vc_effects');
      if (v === null) return true;      // 默认开启
      return v === 'on';
    } catch (e) { return true; }
  }

  // ── 状态
  let _ws         = null;
  let _recorder   = null;
  let _stream     = null;
  let _active     = false;
  let _aiSpeaking = false;
  let _audioQueue = [];       // TTS音频队列（按idx排序顺序播放）
  let _playingIdx = 0;        // 当前播放到第几句
  let _audioCtx   = null;
  let _ui         = null;     // 通话UI元素引用
  let _aiCaptionText = '';    // AI流式字幕累积文本

  // ── 唱歌状态（B/C方案）
  let _isSinging     = false;   // 正在唱歌
  let _singQueue     = [];      // 唱歌音频队列（串行播放）
  let _singPlaying   = false;
  let _duetUserTurn  = false;   // 合唱：轮到用户唱
  let _duetSilenceMs = 0;       // 合唱：用户静音计时
  let _singingSong   = '';
  let _duetVadStream = null;    // 合唱VAD：用户唱完检测
  let _duetVadCtx    = null;
  let _duetVadRAF    = null;

  // ── Minecraft Bot 状态（阶段2：语音指令联动提示）──
  let _mcBotTimer    = null;
  let _mcBotOnline   = false;
  let _mcBotAction   = '';
  let _screenWatchEnabled = false;

  // ── WS重连
  let _reconnectTimer   = null;
  let _reconnectCount   = 0;
  const _MAX_RECONNECT  = 3;      // 最多重连3次
  const _RECONNECT_DELAY = 1500;  // 每次间隔1.5秒（退避）

  // ── 打断优化
  let _currentSource = null;   // 当前正在播放的 AudioBufferSourceNode

  // ── ping保活
  let _pingTimer = null;
  let _dialTimer = null;
  let _dialNodes = [];
  let _callOptions = {};      // 来电时传入的参数（contactId/voiceKey/callReason）
  let _awaitingInitialGreeting = false;
  let _silenceFlushPending = false;
  let _serverAiDone = false;
  let _restartPending = false;   // ★ 防止重复调度 _startRecorder（见 P2-9）

  // ── 配置
  const WS_URL = () => {
    // 优先用来电传入的参数，兜底用 Chat.contact / Session
    const contact    = window.Chat && Chat.contact;
    const sid        = window.Session ? Session.getSessionId() : 'default';
    // ★ 角色标识用角色名（能映射到角色配置/记忆隔离键），UUID 只做 localStorage 键
    const cid        = _callOptions.characterId
                       || (contact ? (contact.name || contact.id) : 'default');
    // Prefer the voice object saved by the profile/clone panel. Older contacts
    // only have voiceKey; leaving this empty lets the backend read the role card.
    const savedVoice = contact && contact.voice;
    const resolvedVk = _callOptions.voiceKey
      || (savedVoice && savedVoice.voice_key ? savedVoice.voice_key : '')
      || (typeof savedVoice === 'string' ? savedVoice : '')
      || (contact && contact.voiceKey ? contact.voiceKey : '')
      || '';
    const proto      = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/voice-call`
      + `?session_id=${encodeURIComponent(sid)}`
      + `&character_id=${encodeURIComponent(cid)}`
      + `&voice_key=${encodeURIComponent(resolvedVk)}`
      + `&call_source=${encodeURIComponent(_callOptions.callSource || 'user_initiated')}`;
  };

  // ──────────────────────────────────────────────
  // 公开方法
  // ──────────────────────────────────────────────

  async function start(options = {}) {
    if (_active) return;

    try {
      if (!_audioCtx || _audioCtx.state === 'closed') {
        _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (_audioCtx.state === 'suspended') _audioCtx.resume().catch(() => {});
    } catch (_) {}

    // 保存来电参数，WS_URL() 会读取
    _callOptions = options || {};

    // ★ 修复：确保 Chat.contact 指向通话角色（来电用 options.contactId，主动拨打用当前 Chat.contact）
    let contactId = _callOptions.contactId;
    if (!contactId && window.Chat && Chat.contact) {
      contactId = Chat.contact.id || Chat.contact.name;
      _callOptions.contactId = contactId;
    }
    if (contactId && window.Chat) {
      try {
        const contact = Store.getContact(contactId);
        if (contact) Chat.contact = contact;   // 静默切换/回填，让 UI 和唱歌用对角色
      } catch (_) {}
    }

    // 申请麦克风权限
    try {
      _stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          sampleRate: 16000,
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        }
      });
    } catch (e) {
      _showToast('无法访问麦克风，请检查权限设置');
      return;
    }

    // 建立WebSocket
    try {
      _ws = new WebSocket(WS_URL());
    } catch (e) {
      _showToast('通话连接失败');
      _cleanupStream();
      return;
    }

    _ws.onopen    = _onWsOpen;
    _ws.onmessage = _onWsMessage;
    _ws.onclose   = _onWsClose;
    _ws.onerror   = _onWsError;

    _active     = true;
    _aiSpeaking = false;
    _screenWatchEnabled = false;
    _audioQueue = [];
    _playingIdx = 0;
    _awaitingInitialGreeting = true;
    _silenceFlushPending = false;
    _serverAiDone = false;

    _showCallUI();
  }

  function stop() {
    if (!_active) {
      // ★ 即使已经被标记为结束，也要把可能残留的通话 UI 清掉，
      //   否则异常断开后 overlay 会永久覆盖全屏，导致记录页等点不了。
      _hideCallUI();
      return;
    }
    _active = false;

    // ★ 清理重连状态
    if (_reconnectTimer) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = null;
    }
    _reconnectCount = 0;
    _restartPending = false;
    _stopPing();
    _stopDialTone();

    // 通知后端结束通话
    _wsSend({ type: 'end_call' });

    // 清理资源
    setTimeout(() => {
      if (_ws) { try { _ws.close(); } catch (_) {} _ws = null; }
      _cleanupStream();
      _stopRecorder();
      _stopVad();            // ★ 释放 VAD AudioContext
      _clearAudioQueue();
      _destroyAudioCtx();   // ★ 彻底释放 AudioContext
      _hideCallUI();

      // ★ 通话结束后处理（D功能核心）
      _onCallEnded();

      // 清空来电参数
      _callOptions = {};
    }, 300);
  }

  // ★ 新增：通话结束后的前端处理
  function _onCallEnded() {
    const contactId = _callOptions.contactId
                      || (window.Chat && Chat.contact && Chat.contact.id);
    if (!contactId) return;

    // 上报通话结果给后端（供调度层降低来电频率等决策用）
    try {
      WsClient.sendCallResult('ended', contactId);
    } catch (_) {}

    // 延迟1.5秒后，后端会通过 WS 主连接推送通话结束消息
    // （这里只做前端侧的轻量处理，重度的记忆/亲密度由后端 _run_post_call_pipeline 处理）
    setTimeout(() => {
      _injectAfterCallHint(contactId);
    }, 1500);
  }

  // ★ 新增：通话结束后注入"挂断提示"气泡（像真人挂完电话后发消息）
  function _injectAfterCallHint(contactId) {
    try {
      const contact = Store.getContact(contactId);
      if (!contact) return;

      const conv = Store.ensureConversation(contactId);

      // 写入"通话结束"系统标记（后端会跟进发真实的通话后消息，这里只是占位）
      const endMsg = {
        id:      Store.uid(),
        role:    'system',
        content: `📞 通话结束`,
        ts:      Date.now(),
        status:  'done',
        subtype: 'call_ended',
      };
      Store.addMessage(conv.id, endMsg);
      Store.touchConversation(conv.id, endMsg.content);

      // 刷新聊天界面
      try { renderChatList(); } catch (_) {}
      try {
        if (window.Chat && Chat.contact && Chat.contact.id === contactId) {
          const body = document.getElementById('chat-body');
          if (body) {
            const el = Chat.renderMsg(endMsg);
            body.appendChild(el);
            Chat.scrollBottom();
          }
        }
      } catch (_) {}
    } catch (_) {}
  }

  function isActive() {
    return _active;
  }

  // ──────────────────────────────────────────────
  // WebSocket事件
  // ──────────────────────────────────────────────

  function _onWsOpen() {
    _startDialTone();
    console.log('[VoiceCall] WebSocket已连接');
    // ★ 微信式呼叫体验：连接上先显示"拨号中/等待接通"，收到后端 call_ready 才变"通话中…"
    _updateStatus('拨号中…');
    _startPing();
  }

  function _onWsMessage(evt) {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (_) { return; }

    const type = msg.type || '';

    switch (type) {

      case 'call_ready':
        _stopDialTone();
        _playConnectTone();
        // 先让 AI 主动说接通首句；首句播放完成后再打开录音，避免把接通音和
        // AI 自己的“喂”录回麦克风并误当用户说话。
        _updateStatus('已接通…');
        _armInitialGreetingFallback();
        // ★ Minecraft Bot 状态条：启动定时刷新
        _startMcBotStatus();
        break;

      case 'asr_start':
        _restartRecorderForNextUtterance();
        _updateStatus('正在识别…');
        break;

      case 'asr_result':
        // 显示用户说的话（文字气泡）
        _appendTranscript('user', msg.text || '');
        _updateStatus('AI思考中…');
        break;

      case 'asr_empty':
        // ★ 带上音频字节数，才能区分"根本没录到"和"录到了但识别为空"
        _updateStatus(msg.bytes ? `没听清（仅${msg.bytes}B），请再说一次` : '没听清，请再说一次');
        setTimeout(() => { if (_active) _updateStatus('通话中…'); }, 1800);
        break;

      case 'asr_error':
        _updateStatus('识别出错：' + (msg.error || '未知原因'));
        setTimeout(() => _updateStatus('通话中…'), 3000);
        break;

      case 'ai_thinking':
        _serverAiDone = false;
        _setAiSpeakingIndicator(true);
        _updateStatus('AI回复中…');
        break;

      case 'ai_text_chunk':
        // 显示AI说的话（文字气泡，流式追加）
        _appendAiTextChunk(msg.idx || 0, msg.text || '');
        break;

      case 'ai_interrupted':
        _clearAudioQueue();
        _aiSpeaking = false;
        _setAiSpeakingIndicator(false);
        _updateStatus('通话中…');
        break;

      case 'ai_done':
        _serverAiDone = true;
        // ★ PCM 流式还在播时不复位说话状态，等队列播空再由 _drainPcm 复位
        if (!_currentSource && !_audioQueue.some(item => !item.played) && _pcmIdle()) _aiSpeaking = false;
        _setAiSpeakingIndicator(false);
        _finishInitialGreetingIfReady();
        _updateStatus('通话中…');
        break;

      case 'ai_error':
        _aiSpeaking = false;
        _setAiSpeakingIndicator(false);
        // ★ 提示可能原因：API Key 未配置是最常见的（后端无 key 时 LLM 直接失败）
        _updateStatus('AI没响应：请在设置页配置 API Key');
        setTimeout(() => _updateStatus('通话中…'), 3000);
        break;

      case 'tts_chunk': {
        const { idx, audio, text: chunkText, streaming, sub_idx, format, rate } = msg;

        // ★ PCM 真流式：裸音频块立即入队无缝播放（阿里云 qwen-tts 24kHz 16bit）
        if (streaming && format === 'pcm') {
          _enqueuePcmChunk(idx || 0, sub_idx || 0, audio || '', rate || 24000, chunkText || '');
          break;
        }

        if (streaming) {
          // 流式块：每块单独入队播放
          // 用 idx*1000+sub_idx 作为排序键，保证同句内顺序
          const sortKey = (idx || 0) * 1000 + (sub_idx || 0);
          _enqueueStreamingChunk(sortKey, audio || '', chunkText || '');
        } else {
          // 非流式：原有逻辑不动
          if (chunkText) _appendAiTextChunk(idx || 0, chunkText);
          _enqueueTtsChunk(idx || 0, audio || '', chunkText || '');
        }
        break;
      }

      case 'tts_text_fallback':
        // TTS失败降级：只显示文字，不播放音频
        console.log(`[VoiceCall] TTS降级: ${msg.text}`);
        break;

      // ═══ 唱歌（B/C方案）═══
      case 'sing_start':
        _isSinging = true;
        _singingSong = msg.song_name || '';
        _duetUserTurn = false;
        _updateStatus('♪ ' + (_singingSong || '唱歌中…'));
        _showLyric('');   // 清空歌词
        _startDuetVAD();  // 合唱也要 VAD，先启动无妨
        break;

      case 'singing_audio': {
        // 逐句唱歌音频：入唱歌队列串行播放 + 显示歌词
        if (msg.line) _showLyric(msg.line);
        _singQueue.push({ audioB64: msg.audio, line: msg.line || '' });
        _playNextSing();
        break;
      }

      case 'sing_end':
        setTimeout(() => {
          _isSinging = false;
          _singingSong = '';
          _duetUserTurn = false;
          _updateStatus('通话中…');
          _hideLyric();
          _stopDuetVAD();
        }, 800);
        break;

      case 'duet_start':
        _isSinging = true;
        _updateStatus('合唱开始~');
        _startDuetVAD();
        break;

      case 'duet_ai_turn':
        _duetUserTurn = false;
        _duetSilenceMs = 0;
        if (msg.line) _showLyric(msg.line);
        _updateStatus('♪ ' + (_singingSong || 'AI在唱…'));
        break;

      case 'duet_user_turn':
        _duetUserTurn = true;
        _duetSilenceMs = 0;
        if (msg.line) _showLyric(msg.line);
        _updateStatus('该你唱了 🎤 唱完自动接');
        break;

      case 'duet_user_timeout':
        _duetUserTurn = false;
        _updateStatus('AI帮你唱这句~');
        break;

      case 'duet_end':
        _duetUserTurn = false;
        _isSinging = false;
        _duetSilenceMs = 0;
        _updateStatus('唱完啦~ 通话中…');
        _hideLyric();
        _stopDuetVAD();
        break;

      case 'sing_error':
        _isSinging = false;
        _duetUserTurn = false;
        _stopDuetVAD();
        _updateStatus(msg.message || '歌词想不起来了…');
        setTimeout(() => _updateStatus('通话中…'), 2500);
        break;

      case 'speak_fallback':
        // 后端会直接推 TTS 音频，这里只复位
        break;

      // ═══ 画面感知（游戏陪玩 C 方案）═══
      case 'scene_update': {
        const el = document.getElementById('vc-scene');
        if (el) {
          const sceneName = String(msg.scene_name || '').trim();
          const summary = String(msg.summary || '').trim();
          const txt = summary
            ? ('<span class="vc-scene-dot"></span><span class="vc-scene-copy">' +
               _escapeHtml(sceneName ? ('陪伴中 · ' + sceneName) : ('陪伴中 · ' + summary)) + '</span>')
            : '';
          if (txt) {
            el.innerHTML = txt;
            el.classList.add('show');
          } else {
            el.classList.remove('show');
          }
        }
        break;
      }

      case 'screen_watch': {
        _screenWatchEnabled = !!msg.enabled;
        _syncScreenWatchUi();
        if (msg.message) _updateStatus(msg.message);
        break;
      }

      case 'screen_watch_error': {
        _screenWatchEnabled = false;
        _syncScreenWatchUi();
        const message = msg.message || '画面陪伴暂时不可用';
        _updateStatus(message);
        _showToast(message);
        setTimeout(() => {
          if (_active && !_screenWatchEnabled) _updateStatus('通话中…');
        }, 2600);
        break;
      }

      case 'call_ended':
        stop();
        break;

      case 'pong':
        break;

      default:
        break;
    }
  }

  function _onWsClose(e) {
    if (!_active) return;

    // 正常挂断（code 1000）不重连
    if (e && e.code === 1000) {
      stop();
      return;
    }

    // 已超过最大重连次数
    if (_reconnectCount >= _MAX_RECONNECT) {
      _showToast('通话已断开，请重新拨打');
      stop();
      return;
    }

    _reconnectCount++;
    _updateStatus(`连接中断，正在重连（${_reconnectCount}/${_MAX_RECONNECT}）…`);

    // AudioWorklet 持续采集，重连后直接复用，无需暂停/恢复。

    _reconnectTimer = setTimeout(() => {
      if (!_active) return;
      _doReconnect();
    }, _RECONNECT_DELAY * _reconnectCount);  // 退避：1.5s / 3s / 4.5s
  }

  function _onWsError(e) {
    console.error('[VoiceCall] WebSocket错误:', e);
    // 错误后 onclose 也会触发，交给 _onWsClose 处理重连
    // 这里只记日志，不重复 stop()
  }

  function _doReconnect() {
    if (!_active) return;

    // 关掉旧 WS（如果还挂着）
    if (_ws) {
      _ws.onclose = null;   // 先摘掉事件，防止触发 _onWsClose 再次重连
      try { _ws.close(); } catch (_) {}
      _ws = null;
    }

    try {
      _ws = new WebSocket(WS_URL());
    } catch (e) {
      _showToast('重连失败，请检查网络');
      stop();
      return;
    }

    _ws.onopen = () => {
      // 重连成功
      _reconnectCount = 0;
      _updateStatus('通话中…');

      // 恢复采集：AudioWorklet 若已销毁则重启
      if (!_vadCtx && _stream) {
        _startRecorder();
      }

      // 重新启动 ping 保活
      _startPing();
    };

    _ws.onmessage = _onWsMessage;
    _ws.onclose   = _onWsClose;
    _ws.onerror   = _onWsError;
  }

  function _startPing() {
    // 清理旧定时器
    if (_pingTimer) { clearInterval(_pingTimer); _pingTimer = null; }
    // 每15秒发一个 ping，防止 WS 因空闲被中间设备断开
    _pingTimer = setInterval(() => {
      if (_active && _ws && _ws.readyState === WebSocket.OPEN) {
        _wsSend({ type: 'ping' });
      }
    }, 15000);
  }

  function _tone(freq, duration, volume, delay) {
    try {
      if (!_audioCtx || _audioCtx.state === 'closed') {
        _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (_audioCtx.state === 'suspended') _audioCtx.resume().catch(() => {});
      const osc = _audioCtx.createOscillator();
      const gain = _audioCtx.createGain();
      const at = _audioCtx.currentTime + (delay || 0);
      osc.type = 'sine';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, at);
      gain.gain.exponentialRampToValueAtTime(volume || 0.08, at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, at + duration);
      osc.connect(gain);
      gain.connect(_audioCtx.destination);
      osc.start(at);
      osc.stop(at + duration + 0.02);
      _dialNodes.push(osc);
    } catch (_) {}
  }

  function _startDialTone() {
    _stopDialTone();
    const ring = () => {
      _tone(440, 0.8, 0.055, 0);
      _tone(480, 0.8, 0.04, 0);
    };
    ring();
    _dialTimer = setInterval(ring, 2600);
  }

  function _stopDialTone() {
    if (_dialTimer) { clearInterval(_dialTimer); _dialTimer = null; }
    _dialNodes.forEach(node => { try { node.stop(); } catch (_) {} });
    _dialNodes = [];
  }

  function _playConnectTone() {
    _tone(880, 0.10, 0.10, 0);
    _tone(1174, 0.12, 0.08, 0.12);
  }

  function _stopPing() {
    if (_pingTimer) { clearInterval(_pingTimer); _pingTimer = null; }
  }

  // ──────────────────────────────────────────────
  // ── 采集 + VAD（AudioWorklet PCM16 + 单一状态机，移植自 pai-voice）──
  // 一条麦克风流同时供 VAD 与识别用；PCM16 持续发给后端，后端按 speech_start/end 决定攒哪些。
  let _vadCtx      = null;      // 采集专用 AudioContext（16k）
  let _vadNode     = null;      // AudioWorkletNode
  let _currentVol  = 0;         // 当前 RMS（0~1，供 UI 波形）
  let _noiseFloor  = 0.01;      // 噪声底估计（0~1）
  let _currentSpeechThreshold = 0.02;   // 进入阈值 enter（0~1）
  // VAD 状态机变量
  let _vadSpeaking    = false;  // 用户是否在说话
  let _vadHot         = 0;      // 连续超阈值块数
  let _vadHotSince    = 0;      // 开始超阈值的时间戳
  let _vadLastHotAt   = 0;      // 最后一次超阈值时间戳
  let _vadSilenceSince = 0;     // 静音累计时间
  let _vadSpeechMin   = 1;      // 说话中观测到的最小 RMS
  // VAD 参数（pai-voice 默认值，按需可调）
  const _VAD = {
    startHold: 3,        // 连续几块（~64ms/块）超阈值算开口
    endSilenceMs: 350,   // 静音多久算说完（450→350，进一步缩短收句等待）
    bargeInMs: 560,      // 正在播放正式回复时，持续开口多久才打断（防回声/误触）
    floor: 0.006,        // 最低噪声底
    gain: 3.2,           // 进入阈值 = max(floor, 噪声底 * gain)
    exitRatio: 0.62,     // 结束阈值 = 进入阈值 * 这个（双阈值，防临界抖动卡死）
    speakingGain: 2.8,   // AI 说话时再抬高门槛，降低扬声器误触
    watchdogMs: 1600,    // 这么久没有一块声音超过进入阈值 → 强制收尾（AGC 抬底噪也不卡）
  };

  // ── 麦克风电平可视化 ──
  // 实时音量 + 开口阈值直接显示，用于判断"听不见"到底出在哪一侧。
  let _micUiLast = 0;
  function _updateMicUi() {
    const bar = document.getElementById('vc-mic-bar');
    if (!bar) return;
    const now = Date.now();
    if (now - _micUiLast < 100) return;   // 限频，避免每帧写 DOM
    _micUiLast = now;
    // _currentVol 是 0~1 的 RMS；映射到 0~100% 波形（说话约 0.01~0.1）
    const pct = Math.min(100, Math.round(_currentVol * 800));
    bar.style.width = pct + '%';
    bar.style.background = _vadSpeaking
      ? 'rgba(126,224,129,.95)'     // 已判定为"在说话"
      : 'rgba(255,255,255,.6)';
    const hint = document.getElementById('vc-mic-hint');
    if (hint && _noiseFloor > 0) {
      hint.textContent = `麦克风 ${(_currentVol * 1000).toFixed(0)} · 开口阈值 ${(_currentSpeechThreshold * 1000).toFixed(0)}`;
    }
  }

  function _startVad() {
    if (_vadCtx || !_stream) return;
    try {
      // 采集专用 AudioContext 固定 16k：AudioWorklet 内部 sampleRate 即为 16000，
      // 无需重采样，PCM16 直出。播放走独立的 _audioCtx，互不干扰。
      _vadCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      const loadWorklet = async () => {
        const blob = new Blob([WORKLET_SRC], { type: 'application/javascript' });
        const url  = URL.createObjectURL(blob);
        try {
          await _vadCtx.audioWorklet.addModule(url);
        } catch (e) {
          // 同页第二次 addModule 会报 "processor already registered"，忽略即可
        }
        const src = _vadCtx.createMediaStreamSource(_stream);
        _vadNode = new AudioWorkletNode(_vadCtx, 'pcm-capture');
        _vadNode.port.onmessage = (e) => _onCapture(e.data);
        src.connect(_vadNode);
        // worklet 不接到 destination，避免自己听见自己
      };
      loadWorklet().catch((e) => console.error('[VoiceCall] VAD启动失败:', e));
    } catch (e) {
      console.error('[VoiceCall] VAD启动失败:', e);
    }
  }

  // AudioWorklet 回传：{ pcm: ArrayBuffer, rms } 或 { rms }（无 PCM 时）
  function _onCapture({ pcm, rms }) {
    // 1) PCM16 持续发给后端（后端按 speech_start/end 决定攒哪些）
    if (pcm && _ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.send(pcm); } catch (_) {}
    }
    if (rms === undefined) return;

    // 2) 单一 VAD 状态机（双阈值 + 噪声底自适应 + 看门狗）
    _currentVol = rms;
    const now = performance.now();

    // 噪声底：没说话时快跟；说话中慢慢向观测到的最小值靠（AGC 抬底噪也不卡死）
    if (!_vadSpeaking) {
      _noiseFloor = _noiseFloor * 0.97 + rms * 0.03;
    } else {
      _vadSpeechMin = Math.min(_vadSpeechMin, rms);
      _noiseFloor = Math.min(_noiseFloor * 1.0015, Math.max(_noiseFloor, _vadSpeechMin));
    }

    let enter = Math.max(_VAD.floor, _noiseFloor * _VAD.gain);
    if (_aiSpeaking) enter *= _VAD.speakingGain;   // AI 说话时抬高门槛，防扬声器误触
    const exit = enter * _VAD.exitRatio;
    _currentSpeechThreshold = enter;
    _updateMicUi();

    if (rms > enter) _vadLastHotAt = now;

    if (!_vadSpeaking) {
      if (rms > enter) {
        _vadHot += 1;
        if (!_vadHotSince) _vadHotSince = now;
        if (_vadHot >= _VAD.startHold) {
          const needed = (_aiSpeaking) ? _VAD.bargeInMs : 0;
          if (now - _vadHotSince >= needed) _speechStart();
        }
      } else {
        _vadHot = 0; _vadHotSince = 0;
      }
      return;
    }

    // 说话中：低于结束阈值持续 endSilenceMs → 收尾；或看门狗强制收尾
    if (rms < exit) {
      if (!_vadSilenceSince) _vadSilenceSince = now;
      else if (now - _vadSilenceSince >= _VAD.endSilenceMs) { _speechEnd(); return; }
    } else {
      _vadSilenceSince = 0;
    }
    if (_vadLastHotAt && now - _vadLastHotAt >= _VAD.watchdogMs) _speechEnd();
  }

  function _speechStart() {
    _vadSpeaking = true;
    _vadSpeechMin = 1;
    _vadLastHotAt = performance.now();
    _vadSilenceSince = 0;
    // 打断：AI 正在播放正式回复时用户开口 → 停播 + interrupt
    const barge = _aiSpeaking;
    if (barge) {
      _clearAudioQueue();
      _wsSend({ type: 'interrupt', turn_id: 'new' });
    }
    _wsSend({ type: 'speech_start', barge });
  }

  function _speechEnd() {
    _vadSpeaking = false;
    _vadSilenceSince = 0;
    _wsSend({ type: 'speech_end' });
    _updateStatus('正在识别…');
  }

  function _stopVad() {
    if (_vadNode) {
      try { _vadNode.port.onmessage = null; _vadNode.disconnect(); } catch (_) {}
    }
    _vadNode = null;
    if (_vadCtx) {
      try { _vadCtx.close(); } catch (_) {}
    }
    _vadCtx = null;
    _currentVol = 0;
    _noiseFloor = 0.01;
    _currentSpeechThreshold = 0.02;
    _vadSpeaking = false;
    _vadHot = 0;
    _vadHotSince = 0;
    _vadLastHotAt = 0;
    _vadSilenceSince = 0;
    _vadSpeechMin = 1;
  }

  // 采集：AudioWorklet 持续采 PCM16（由 _startVad 启动），无 MediaRecorder。
  // 收句完全由 VAD 状态机决定，前端只发 speech_start / speech_end，不再有"静音兜底"。

  function _startRecorder() {
    if (!_stream) {
      _showToast('麦克风未就绪，无法录音');
      return;
    }
    _startVad();
    console.log('[VoiceCall] 录音开始（AudioWorklet PCM16）');
    _updateStatus('正在聆听…');
  }

  function _stopRecorder() {
    // AudioWorklet 由 _stopVad() 统一清理；保留空实现以兼容既有调用点。
  }

  function _flushRecorderThenSilence() {
    // 旧 MediaRecorder 时代的"flush 尾音再发 silence"已废弃；
    // 新架构由 _speechEnd() 直接发 speech_end，无需 flush。
  }

  function _restartRecorderForNextUtterance() {
    // AudioWorklet 持续采集，无需按 utterance 重启；保留空实现以兼容 asr_start 调用点。
  }

  function _cleanupStream() {
    if (_stream) {
      _stream.getTracks().forEach(t => t.stop());
      _stream = null;
    }
  }

  // ──────────────────────────────────────────────
  // TTS音频播放队列
  // ──────────────────────────────────────────────

  function _enqueueTtsChunk(idx, audioB64, text) {
    _audioQueue.push({ idx, audioB64, text, played: false });
    _audioQueue.sort((a, b) => a.idx - b.idx);
    _tryPlayNext();
  }

  function _enqueueStreamingChunk(sortKey, audioB64, text) {
    // 流式块的播放策略：
    // 不等整句收齐，收到一块就立即追加到队列尾部播放
    // sortKey 保证同句内顺序，不同句之间由 idx 大序号隔开
    _audioQueue.push({ idx: sortKey, audioB64, text, played: false });
    _audioQueue.sort((a, b) => a.idx - b.idx);

    // 更新字幕（只用第一块的 text）
    if (text) _updateCaption(text);

    _tryPlayNext();
  }

  function _tryPlayNext() {
    if (_aiSpeaking) return;  // 已在播放，等当前播完

    const next = _audioQueue.find(item => !item.played);
    if (!next) {
      _finishInitialGreetingIfReady();
      return;
    }

    next.played = true;
    _aiSpeaking = true;
    _playAudioB64(next.audioB64, () => {
      _playingIdx = next.idx + 1;
      _aiSpeaking = false;
      _tryPlayNext();  // 播完继续播下一句；首句全部播完后才打开麦克风
    });
  }

  function _finishInitialGreetingIfReady() {
    if (!_awaitingInitialGreeting || !_serverAiDone) return;
    if (_currentSource || _audioQueue.some(item => !item.played)) return;
    _awaitingInitialGreeting = false;
    if (!_vadCtx && _active) _startRecorder();
  }

  // 即使某个 TTS 服务异常没有返回音频，也不能让首句阶段永久锁死麦克风。
  // 正常情况下播放队列会更早调用 _finishInitialGreetingIfReady；这里只是兜底。
  //
  // ★ 修复两个问题：
  //   1) 旧实现是单次 setTimeout：若 6.5s 那一刻 _currentSource 不为 null
  //      （TTS 音频卡住 / 解码失败但 source 仍挂着），兜底直接 return 且无重试
  //      → 整通电话麦克风都打不开，用户说话完全没反应。
  //   2) 兜底时没有清 _currentSource，_finishInitialGreetingIfReady 会被它挡住。
  //   改成最多重试若干次的轮询，每次强制释放卡住的播放源。
  function _armInitialGreetingFallback() {
    const MAX_TRIES = 8;          // 6.5s 起，每 1.5s 重试一次，最长约 17s
    let tries = 0;
    const tick = () => {
      if (!_active || !_awaitingInitialGreeting) return;
      tries += 1;
      _serverAiDone = true;
      _audioQueue = _audioQueue.filter(item => item.played);
      // ★ 强制释放卡住的播放源，否则 _finishInitialGreetingIfReady 永远进不去
      if (_currentSource) {
        try { _currentSource.stop(); } catch (_) {}
        _currentSource = null;
      }
      _aiSpeaking = false;
      _finishInitialGreetingIfReady();
      if (_awaitingInitialGreeting && tries < MAX_TRIES) {
        setTimeout(tick, 1500);
      }
    };
    setTimeout(tick, 6500);
  }

  function _playAudioB64(b64, onEnded) {
    try {
      if (!_audioCtx || _audioCtx.state === 'closed') {
        _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }

      // 特效开启时，为播放链路接一个 AnalyserNode，供声波律动读取
      if (_effectsEnabled() && !_analyser && _audioCtx) {
        _analyser = _audioCtx.createAnalyser();
        _analyser.fftSize = 256;
      }

      // 如果 AudioContext 被系统暂停（移动端后台），先恢复
      if (_audioCtx.state === 'suspended') {
        _audioCtx.resume().catch(() => {});
      }

      const binary = atob(b64);
      const buf    = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i++) buf[i] = binary.charCodeAt(i);

      _audioCtx.decodeAudioData(buf.buffer, (decoded) => {
        // ★ 先停掉上一个（防止并发播放）
        if (_currentSource) {
          try { _currentSource.stop(); } catch (_) {}
          _currentSource = null;
        }

        const source    = _audioCtx.createBufferSource();
        source.buffer   = decoded;
        if (_analyser) {
          source.connect(_analyser);
          _analyser.connect(_audioCtx.destination);
        } else {
          source.connect(_audioCtx.destination);
        }

        source.onended = () => {
          if (_currentSource === source) _currentSource = null;
          onEnded && onEnded();
        };

        _currentSource = source;
        source.start(0);

      }, (err) => {
        console.error('[VoiceCall] 音频解码失败:', err);
        onEnded && onEnded();
      });

    } catch (e) {
      console.error('[VoiceCall] 音频播放失败:', e);
      onEnded && onEnded();
    }
  }

  function _clearAudioQueue() {
    // ★ 立即停止当前正在播放的句子
    if (_currentSource) {
      try { _currentSource.stop(); } catch (_) {}
      _currentSource = null;
    }

    // ★ PCM 流式通道同步清空（打断时停掉正在播的块 + 丢掉未播的块）
    if (_pcmCurrentSource) {
      try { _pcmCurrentSource.stop(); } catch (_) {}
      _pcmCurrentSource = null;
    }
    _pcmQueue = [];
    _pcmPlaying = false;
    _pcmNextTime = 0;

    _audioQueue = [];
    _playingIdx = 0;
    _aiSpeaking = false;

    // AudioContext 不关闭，只是停止播放
    // 关闭后重建有延迟，影响下一句的播放速度
    // 只在 stop() 时才真正关闭
  }

  // ──────────────────────────────────────────────
  // ★ PCM 真流式播放（阿里云 qwen-tts：24kHz 16bit 裸 PCM 逐块推）
  //   逐块 decode 不可能（wav 分块没有独立头），所以直接把 Int16→Float32
  //   塞进 AudioContext 时间轴无缝排队：块到即排，衔接零间隙。
  // ──────────────────────────────────────────────
  let _pcmCtx = null;
  let _pcmQueue = [];
  let _pcmPlaying = false;
  let _pcmNextTime = 0;
  let _pcmCurrentSource = null;

  function _ensurePcmCtx(rate) {
    if (!_pcmCtx || _pcmCtx.state === 'closed') {
      _pcmCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: rate || 24000 });
      _pcmNextTime = 0;
    }
    if (_pcmCtx.state === 'suspended') _pcmCtx.resume().catch(() => {});
    return _pcmCtx;
  }

  function _enqueuePcmChunk(idx, subIdx, b64, rate, text) {
    try {
      const ctx = _ensurePcmCtx(rate);
      const bin = atob(b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const n = Math.floor(bytes.length / 2);
      if (n <= 0) return;
      const dv = new DataView(bytes.buffer);
      const f32 = new Float32Array(n);
      for (let i = 0; i < n; i++) f32[i] = dv.getInt16(i * 2, true) / 32768;
      const buf = ctx.createBuffer(1, n, rate || 24000);
      if (buf.copyToChannel) buf.copyToChannel(f32, 0);
      else buf.getChannelData(0).set(f32);
      _pcmQueue.push({ idx, subIdx, buf, text });
      _pcmQueue.sort((a, b) => (a.idx - b.idx) || (a.subIdx - b.subIdx));
      if (text) _updateCaption(text);
      _drainPcm();
    } catch (e) {
      console.warn('[VoiceCall] PCM 块处理失败:', e);
    }
  }

  function _drainPcm() {
    if (_pcmPlaying) return;
    const next = _pcmQueue.shift();
    if (!next) {
      // 整句的块全部播完且后端已完成合成 → 复位说话状态
      if (_serverAiDone) _aiSpeaking = false;
      return;
    }
    _pcmPlaying = true;
    _aiSpeaking = true;
    const src = _pcmCtx.createBufferSource();
    src.buffer = next.buf;
    if (_analyser) {
      src.connect(_analyser);
      _analyser.connect(_pcmCtx.destination);
    } else {
      src.connect(_pcmCtx.destination);
    }
    // 无缝衔接：从上一次结束时间继续排（晚于当前时间才留 30ms 余量）
    const startAt = Math.max(_pcmCtx.currentTime + 0.03, _pcmNextTime);
    src.start(startAt);
    _pcmNextTime = startAt + next.buf.duration + 0.005;
    _pcmCurrentSource = src;
    src.onended = () => {
      if (_pcmCurrentSource === src) _pcmCurrentSource = null;
      _pcmPlaying = false;
      _drainPcm();
    };
  }

  function _pcmIdle() {
    return !_pcmPlaying && _pcmQueue.length === 0;
  }

  // ──────────────────────────────────────────────
  // 唱歌：播放队列 / 歌词显示 / 合唱VAD（B/C方案）
  // ──────────────────────────────────────────────

  function _playNextSing() {
    // 唱歌音频串行播放（复用 _playAudioB64），不抢 TTS 说话队列
    if (_singPlaying) return;
    if (!_singQueue.length) { _singPlaying = false; return; }
    const item = _singQueue.shift();
    _singPlaying = true;
    _playAudioB64(item.audioB64, () => {
      _singPlaying = false;
      _playNextSing();
    });
  }

  function _showLyric(line) {
    // ★ 已按主人要求禁用：唱歌时不显示卡拉OK歌词
    return;
    const el = document.getElementById('vc-lyric');
    if (!el) return;
    if (line) {
      el.textContent = line;
      el.classList.add('show');
    } else {
      el.textContent = '';
    }
  }

  function _hideLyric() {
    return;
    const el = document.getElementById('vc-lyric');
    if (el) el.classList.remove('show');
  }

  function _startDuetVAD() {
    // 合唱VAD：轮到用户唱时，检测用户静音≥600ms → 判定唱完 → 发 user_sung
    if (_duetVadStream || _duetVadRAF) return;
    try {
      navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
        if (!_active) { stream.getTracks().forEach(t => t.stop()); return; }
        _duetVadStream = stream;
        _duetVadCtx = new (window.AudioContext || window.webkitAudioContext)();
        const src  = _duetVadCtx.createMediaStreamSource(stream);
        const an   = _duetVadCtx.createAnalyser();
        an.fftSize = 512;
        src.connect(an);
        const data = new Uint8Array(an.frequencyBinCount);
        const loop = () => {
          if (!_active || !_duetUserTurn) { _duetVadRAF = requestAnimationFrame(loop); return; }
          an.getByteFrequencyData(data);
          let vol = 0;
          for (let i = 0; i < data.length; i++) vol += data[i];
          vol /= data.length;
          if (vol < 10) _duetSilenceMs += 100;
          else _duetSilenceMs = 0;
          if (_duetSilenceMs >= 600) {
            _duetSilenceMs = 0;
            _duetUserTurn = false;
            _wsSend({ type: 'user_sung' });
            _updateStatus('♪ 继续…');
          }
          _duetVadRAF = requestAnimationFrame(loop);
        };
        loop();
      }).catch(() => { /* 没麦权就手动点按钮，静默 */ });
    } catch (e) { /* 静默 */ }
  }

  function _stopDuetVAD() {
    if (_duetVadRAF) { cancelAnimationFrame(_duetVadRAF); _duetVadRAF = null; }
    if (_duetVadStream) {
      _duetVadStream.getTracks().forEach(t => { try { t.stop(); } catch (_) {} });
      _duetVadStream = null;
    }
    if (_duetVadCtx) {
      try { _duetVadCtx.close(); } catch (_) {}
      _duetVadCtx = null;
    }
    _duetSilenceMs = 0;
  }

  // ──────────────────────────────────────────────
  // Minecraft Bot 状态条（阶段2 UI）
  // ──────────────────────────────────────────────

  function _startMcBotStatus() {
    _stopMcBotStatus();
    _updateMcBotStatus();
    _mcBotTimer = setInterval(_updateMcBotStatus, 8000);   // 每 8 秒刷新
  }

  function _stopMcBotStatus() {
    if (_mcBotTimer) { clearInterval(_mcBotTimer); _mcBotTimer = null; }
  }

  async function _updateMcBotStatus() {
    try {
      const sid = window.Session?.getSessionId?.() || 'default';
      const contact = window.Chat && Chat.contact;
      const cid = _callOptions.characterId || (contact ? (contact.name || contact.id) : 'default');
      const res = await fetch('/api/bot/status?session_id=' + encodeURIComponent(sid) + '&character_id=' + encodeURIComponent(cid));
      if (!res.ok) return;
      const d = await res.json();
      _mcBotOnline = !!d.online;
      _mcBotAction = d.current_action || '';
      _renderMcBotStatus();
    } catch (_) { /* 后端没 MC 路由也静默 */ }
  }

  function _renderMcBotStatus() {
    const el = document.getElementById('vc-mcbot');
    if (!el) return;
    if (_mcBotOnline) {
      const act = _mcBotAction ? (' · ' + _mcBotAction) : '';
      const contact = window.Chat && Chat.contact;
      el.textContent = '🎮 ' + ((contact && contact.name) || 'TA') + ' 在游戏里' + act;
      el.classList.add('show');
      el.classList.remove('off');
    } else {
      el.classList.remove('show');
      el.classList.add('off');
    }
  }

  function _destroyAudioCtx() {
    if (_audioCtx) {
      try { _audioCtx.close(); } catch (_) {}
      _audioCtx = null;
    }
    _currentSource = null;
  }

  // ──────────────────────────────────────────────
  // 通话UI
  // ──────────────────────────────────────────────

  function _showCallUI() {
    // ★ 修复：通话时确保拿到当前 AI 联系人（来电接听/直接拨打时 Chat.contact 可能未设置）
    let contact = (window.Chat && Chat.contact) || null;
    if (!contact && _callOptions.contactId) {
      try { contact = Store.getContact(_callOptions.contactId); } catch (_) {}
    }
    if (contact && window.Chat) {
      try { Chat.contact = contact; } catch (_) {}   // 回填，让唱歌/上下文用对角色
    }
    const name   = contact ? (contact.name || 'AI') : 'AI';
    const avatar = contact ? (contact.avatarUrl || contact.avatar || '') : '';  // 头像URL（字段是 avatarUrl）

    // ── 头像内容：有图用图，没图用首字
    const avatarInner = avatar
      ? `<img src="${avatar}" alt="${name}" onerror="this.style.display='none'">`
      : `<span>${name.charAt(0)}</span>`;

    const overlay = document.createElement('div');
    overlay.id = 'voice-call-overlay';

    overlay.innerHTML = `
      <div class="vc-top">
        <div class="vc-avatar">${avatarInner}</div>
        <div class="vc-name">${name}</div>
        <div class="vc-status" id="vc-status">连接中…</div>
        <!-- 麦克风电平条：通话"听不见"绝大多数是采集侧问题（设备没选对、
             增益太低、系统静音）。把实时音量和"开口阈值"画出来，
             就能一眼分清是"你没说话"还是"麦克风没收到"。 -->
        <div id="vc-mic" style="width:170px;height:4px;background:rgba(255,255,255,.15);border-radius:2px;margin:10px auto 0;overflow:hidden">
          <div id="vc-mic-bar" style="height:100%;width:0;background:rgba(255,255,255,.6);transition:width .1s linear"></div>
        </div>
        <div id="vc-mic-hint" style="text-align:center;font-size:11px;color:rgba(255,255,255,.45);margin-top:5px"></div>
        <div class="vc-wave" id="vc-wave" style="display:none">
          <span></span><span></span><span></span><span></span><span></span>
        </div>
        ${_effectsEnabled() ? '<canvas id="vc-spectrum" class="vc-spectrum" width="200" height="60"></canvas>' : ''}
        <div class="vc-bubble" id="vc-bubble" style="display:none"></div>
        <div class="vc-caption" id="vc-caption"></div>
        <div class="vc-mcbot" id="vc-mcbot"></div>
        <div class="vc-scene" id="vc-scene" aria-live="polite"></div>
        <div class="vc-lyric" id="vc-lyric"></div>
      </div>

      <div class="vc-bottom">
        <div class="vc-actions-row">
          <div class="vc-action-item">
            <button class="vc-btn vc-btn-aux" id="vc-mute-btn" title="静音" aria-label="静音">
              <svg viewBox="0 0 24 24">
                <path d="M12 14a3 3 0 003-3V6a3 3 0 10-6 0v5a3 3 0 003 3zm5-3a5 5 0 01-10 0H5a7 7 0 006 6.92V21h2v-3.08A7 7 0 0019 11h-2z"/>
              </svg>
            </button>
            <span class="vc-action-label" id="vc-mute-label">静音</span>
          </div>
          <div class="vc-action-item">
            <button class="vc-btn vc-btn-aux" id="vc-keyboard-btn" title="键盘输入" aria-label="键盘输入">
              <svg viewBox="0 0 24 24">
                <path d="M20 5H4a1 1 0 00-1 1v12a1 1 0 001 1h16a1 1 0 001-1V6a1 1 0 00-1-1zM9 9h2v2H9V9zm0 4h2v2H9v-2zm-4-4h2v2H5V9zm0 4h2v2H5v-2zm10 4H9v-2h6v2zm0-4h-2v-2h2v2zm0-4h-2V9h2v2zm4 8h-2v-2h2v2zm0-4h-2v-2h2v2zm0-4h-2V9h2v2z"/>
              </svg>
            </button>
            <span class="vc-action-label">键盘</span>
          </div>
          <div class="vc-action-item vc-action-companion">
            <button class="vc-btn vc-btn-aux vc-btn-companion" id="vc-screen-btn"
                    title="开启画面陪伴" aria-label="开启画面陪伴" aria-pressed="false">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 5.25c-4.44 0-8.05 2.72-9.55 6.25a1.25 1.25 0 000 1c1.5 3.53 5.11 6.25 9.55 6.25s8.05-2.72 9.55-6.25a1.25 1.25 0 000-1C20.05 7.97 16.44 5.25 12 5.25zm0 10.5A3.75 3.75 0 1112 8.25a3.75 3.75 0 010 7.5zm0-2A1.75 1.75 0 1012 10.25a1.75 1.75 0 000 3.5z"/>
              </svg>
              <span class="vc-companion-live" aria-hidden="true"></span>
            </button>
            <span class="vc-action-label" id="vc-screen-label">陪伴</span>
          </div>
        </div>
        <div class="vc-end-row">
          <button class="vc-btn vc-btn-end" id="vc-end-btn" title="挂断">
            <svg viewBox="0 0 24 24">
              <path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977 0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89-6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12-.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65 3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0 .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/>
            </svg>
          </button>
        </div>
      </div>

      <div class="vc-keyboard" id="vc-keyboard">
        <input type="text" id="vc-text-input" placeholder="说不清楚？打字也行">
        <button id="vc-text-send">发送</button>
      </div>
    `;

    document.body.appendChild(overlay);
    _ui = overlay;

    // ── 绑定挂断
    document.getElementById('vc-end-btn').onclick = () => stop();

    // ── 绑定静音
    let muted = false;
    document.getElementById('vc-mute-btn').onclick = () => {
      muted = !muted;
      if (_stream) {
        _stream.getAudioTracks().forEach(t => { t.enabled = !muted; });
      }
      document.getElementById('vc-mute-btn').classList.toggle('muted', muted);
      // 更新label文字
      const muteLabel = document.getElementById('vc-mute-label');
      if (muteLabel) muteLabel.textContent = muted ? '已静音' : '静音';
    };

    // ── 绑定键盘切换
    document.getElementById('vc-keyboard-btn').onclick = () => {
      const kb = document.getElementById('vc-keyboard');
      const isOpen = kb.classList.contains('open');
      kb.classList.toggle('open', !isOpen);
      // 键盘打开时自动聚焦
      if (!isOpen) {
        setTimeout(() => {
          const inp = document.getElementById('vc-text-input');
          if (inp) inp.focus();
        }, 100);
      }
    };

    // 默认不看屏幕；用户点击后才开启通话中的持续画面感知。
    const screenBtn = document.getElementById('vc-screen-btn');
    if (screenBtn) {
      screenBtn.onclick = () => {
        _screenWatchEnabled = !_screenWatchEnabled;
        _wsSend({ type: 'screen_watch', enabled: _screenWatchEnabled });
        _syncScreenWatchUi();
        _updateStatus(_screenWatchEnabled ? '正在加入你的此刻…' : '通话中…');
      };
    }

    // ── 绑定文字发送
    document.getElementById('vc-text-send').onclick = _sendTextInput;
    document.getElementById('vc-text-input').onkeydown = (e) => {
      if (e.key === 'Enter') _sendTextInput();
    };

    // 入场动画 + 特效初始化（仅当特效开启）
    if (_effectsEnabled()) {
      _initSpectrum();
      _initUserVAD();   // 监听老公开口就冒泡
    }
    requestAnimationFrame(() => overlay.classList.add('show'));
  }

  // _sendTextInput 抽出来复用
  function _sendTextInput() {
    const input = document.getElementById('vc-text-input');
    const text  = (input ? input.value : '').trim();
    if (!text) return;
    _wsSend({ type: 'text_input', text });
    _updateCaption(text);   // 字幕区显示用户输入
    input.value = '';
  }

  function _hideCallUI() {
    if (!_ui) return;
    // ★ 唱歌收尾：清唱歌队列 + 停合唱VAD + 停 MC Bot 状态刷新
    _isSinging = false;
    _duetUserTurn = false;
    _singQueue = [];
    _singPlaying = false;
    _stopDuetVAD();
    _stopMcBotStatus();
    _ui.classList.remove('show');
    // 停掉特效动画与监听，避免资源泄漏（RAF 死循环 / 麦克风常开）
    if (_spectrumRAF) { cancelAnimationFrame(_spectrumRAF); _spectrumRAF = null; }
    if (_userVADStream) {
      _userVADStream.getTracks().forEach(function (t) { try { t.stop(); } catch (_) {} });
      _userVADStream = null;
    }
    setTimeout(() => {
      if (_ui && _ui.parentNode) _ui.parentNode.removeChild(_ui);
      _ui = null;
    }, 400);
  }

  function _updateStatus(text) {
    const el = document.getElementById('vc-status');
    if (el) el.textContent = text;
  }

  function _syncScreenWatchUi() {
    const btn = document.getElementById('vc-screen-btn');
    const label = document.getElementById('vc-screen-label');
    if (!btn) return;
    btn.classList.toggle('active', _screenWatchEnabled);
    btn.setAttribute('aria-pressed', _screenWatchEnabled ? 'true' : 'false');
    btn.title = _screenWatchEnabled ? '结束画面陪伴' : '陪我一起看屏幕';
    if (label) label.textContent = _screenWatchEnabled ? '陪伴中' : '陪伴';
  }

  function _setAiSpeakingIndicator(on) {
    // 用户选择关闭 AI 说话时的绿色波纹；仅保留状态文字和字幕。
    return;
  }

  // 通话黏人特效：声波律动 + 气泡 + 用户说话检测
  let _analyser = null;
  let _spectrumCanvas = null;
  let _spectrumCtx = null;
  let _spectrumRAF = null;
  let _userVADStream = null;
  let _userVADLastTrigger = 0;

  function _initSpectrum() {
    _spectrumCanvas = document.getElementById('vc-spectrum');
    if (!_spectrumCanvas) return;
    _spectrumCtx = _spectrumCanvas.getContext('2d');
    _drawSpectrum();
  }

  function _drawSpectrum() {
    _spectrumRAF = requestAnimationFrame(_drawSpectrum);
    if (!_analyser || !_spectrumCtx) return;
    const buf = new Uint8Array(_analyser.frequencyBinCount);
    _analyser.getByteFrequencyData(buf);
    _spectrumCtx.clearRect(0, 0, _spectrumCanvas.width, _spectrumCanvas.height);
    const barW = _spectrumCanvas.width / buf.length;
    for (let i = 0; i < buf.length; i++) {
      const h = (buf[i] / 255) * _spectrumCanvas.height;
      _spectrumCtx.fillStyle = 'rgba(7,193,96,0.85)';
      _spectrumCtx.fillRect(i * barW, _spectrumCanvas.height - h, barW - 1, h);
    }
  }

  function _showBubble(text) {
    if (!_effectsEnabled()) return;
    const bubble = document.getElementById('vc-bubble');
    if (!bubble) return;
    bubble.textContent = text;
    bubble.style.display = 'block';
    requestAnimationFrame(() => bubble.classList.add('show'));
    setTimeout(() => {
      bubble.classList.remove('show');
      setTimeout(() => { bubble.style.display = 'none'; }, 300);
    }, 1500);
  }

  // 老公点明的：不同角色语气词不一样，从 Chat.contact 动态取（傲娇/温柔/活泼/默认）
  // ★ 已按主人要求禁用：通话时不弹绿色语句气泡
  function _pickBubble() {
    return;
    let arr = ['嗯？', '怎么啦~', '老公你在说啥呀', '人家听着呢…'];
    try {
      const c = (window.Chat && Chat.contact) ? Chat.contact : null;
      if (c && c.name) {
        const p = (c.personality || '').toLowerCase();
        const isTsundere = /傲娇|tsundere/.test(p);
        const isGentle   = /温柔|gentle/.test(p);
        const isActive   = /活泼|active|元气/.test(p);
        if (isTsundere) {
          arr = ['哼，又找我干嘛', c.name + '才不是想你了呢', '笨蛋老公，叫人家干嘛',
                 '……才、才没有在等你说呢', '谁、谁管你在干嘛啊'];
        } else if (isGentle) {
          arr = ['我在呢，怎么啦~', c.name + '一直陪着你哦', '想和我说说话吗？',
                 '慢慢说，我听着呢', '嗯，老公我在听'];
        } else if (isActive) {
          arr = ['嘿！老公找我啦！', '我在我在！怎么啦怎么啦', c.name + '随时待命！',
                 '啦~有什么好玩的！', '召唤我干嘛呀~'];
        } else {
          arr = ['嗯？', '怎么啦~', c.name + '在听呢', '老公你在说啥呀', '人家听着呢…'];
        }
      }
    } catch (e) {}
    const text = arr[Math.floor(Math.random() * arr.length)];
    _showBubble(text);
  }

  // 你一开口，我就冒泡（轻量麦克风检测，不动原录音逻辑）
  // ★ 已按主人要求禁用（气泡功能已关闭，不再额外开麦克风）
  async function _initUserVAD() {
    return;
    if (!_effectsEnabled()) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      _userVADStream = stream;
      const ac = new (window.AudioContext || window.webkitAudioContext)();
      const src = ac.createMediaStreamSource(stream);
      const an = ac.createAnalyser();
      an.fftSize = 512;
      src.connect(an);
      const data = new Uint8Array(an.frequencyBinCount);
      const loop = () => {
        an.getByteFrequencyData(data);
        let vol = 0;
        for (let i = 0; i < data.length; i++) vol += data[i];
        vol /= data.length;
        const now = Date.now();
        if (vol > 25 && now - _userVADLastTrigger > 4000) {
          _userVADLastTrigger = now;
          _pickBubble();
        }
        requestAnimationFrame(loop);
      };
      loop();
    } catch (e) { /* 没麦权就不冒泡，静默 */ }
  }


  let _aiTextBubble = null;   // AI正在说话标记（true/false）
  let _aiTextIdx    = -1;

  function _appendTranscript(role, text) {
    if (role === 'user') {
      _updateCaption(text);
    }
    // ai 的字幕由 _appendAiTextChunk 流式更新，这里不处理
  }

  function _appendAiTextChunk(idx, text) {
    // idx=0 是新一轮回复的第一句，清空字幕重新开始
    if (idx === 0 || !_aiTextBubble) {
      _aiCaptionText = '';
      _aiTextBubble  = true;   // 标记AI正在说话
      _aiTextIdx     = idx;
    }
    _aiCaptionText = (_aiCaptionText || '') + text;
    _updateCaption(_aiCaptionText);
  }

  function _updateCaption(text) {
    const el = document.getElementById('vc-caption');
    if (el) el.textContent = text;
  }

  function _escapeHtml(value) {
    return String(value || '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[ch]));
  }

  // ──────────────────────────────────────────────
  // 工具
  // ──────────────────────────────────────────────

  function _wsSend(obj) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      try { _ws.send(JSON.stringify(obj)); } catch (_) {}
    }
  }

  function _arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary  = '';
    for (let i = 0; i < bytes.byteLength; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  function _showToast(msg) {
    if (typeof toast === 'function') toast(msg);
    else console.log('[VoiceCall] Toast:', msg);
  }

  // ── 公开API
  return { start, stop, isActive };

})();

// 顶层 const 不会成为 window 的属性，显式挂出去，
// 否则 chat.js 里「打电话唱歌」分支的 window.VoiceCall 检查恒为假，通话起不来。
window.VoiceCall = VoiceCall;
