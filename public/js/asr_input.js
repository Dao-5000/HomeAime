/**
 * asr_input.js  v1.0
 * ASR 语音输入：按住麦克风说话 → 上传 /api/asr → 转文本 → 自动发消息
 * 复用已有 #chat-mic 按钮，替换原浏览器语音识别为后端 ASR
 * 聊天链路零改动：转好文本后填入 #chat-input + 触发 #chat-send 点击
 */
;(function () {
  'use strict'

  var mediaRecorder = null;
  var audioChunks   = [];
  var isRecording   = false;

  // ── 波纹动画 timer
  var _rippleTimer = null;

  // 录音中的麦克风图标（红点版）
  function _micRecordingIcon() {
    return '<svg viewBox="0 0 24 24" style="filter:drop-shadow(0 0 4px #ff4d6d)">'
         + '<path fill="#ff4d6d" d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3z"/>'
         + '<path fill="#ff4d6d" d="M17 11a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/>'
         + '<circle cx="18" cy="6" r="4" fill="#ff4d6d">'
         + '<animate attributeName="r" values="3;5;3" dur="1s" repeatCount="indefinite"/>'
         + '<animate attributeName="opacity" values="1;0.3;1" dur="1s" repeatCount="indefinite"/>'
         + '</circle>'
         + '</svg>';
  }

  // 扩散波纹（按钮外围）
  function _startRipple() {
    _stopRipple();
    var btn = document.getElementById('chat-mic');
    if (!btn) return;
    var ripple = document.createElement('span');
    ripple.id = 'asr-ripple';
    ripple.style.cssText = (
      'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);'
      + 'width:100%;height:100%;border-radius:50%;pointer-events:none;'
      + 'box-shadow:0 0 0 0 rgba(255,77,109,0.6);'
      + 'animation:asrPulse 1.2s ease-out infinite;'
    );
    // 确保父容器 position:relative
    btn.style.position = 'relative';
    btn.appendChild(ripple);

    // 注入动画 keyframes（只注一次）
    if (!document.getElementById('asr-style')) {
      var s = document.createElement('style');
      s.id = 'asr-style';
      s.textContent = (
        '@keyframes asrPulse{'
        + '0%{box-shadow:0 0 0 0 rgba(255,77,109,0.6)}'
        + '70%{box-shadow:0 0 0 12px rgba(255,77,109,0)}'
        + '100%{box-shadow:0 0 0 0 rgba(255,77,109,0)}'
        + '}'
      );
      document.head.appendChild(s);
    }
  }

  function _stopRipple() {
    var r = document.getElementById('asr-ripple');
    if (r) r.parentNode && r.parentNode.removeChild(r);
  }

  // 等 DOM 就绪
  function init() {
    var oldBtn = document.getElementById('chat-mic');
    if (!oldBtn) { setTimeout(init, 300); return; }

    // 克隆按钮替换原按钮（清除原浏览器语音识别的事件监听）
    var btn = oldBtn.cloneNode(true);
    oldBtn.parentNode.replaceChild(btn, oldBtn);
    btn.id = 'chat-mic';
    btn.title = '按住说话（ASR 语音转文字）';
    btn.style.cursor = 'pointer';

    // 按下开录，松开停录上传
    btn.addEventListener('mousedown',  startRec);
    btn.addEventListener('touchstart', startRec, {passive: true});
    btn.addEventListener('mouseup',    stopRec);
    btn.addEventListener('touchend',   stopRec);
    btn.addEventListener('mouseleave', stopRec);
  }

  async function startRec(e) {
    e.preventDefault();
    if (isRecording) return;
    try {
      var stream = await navigator.mediaDevices.getUserMedia({audio: true});
      audioChunks   = [];
      // 优先 webm（Chrome），降级 ogg（Firefox），再降级默认
      var mimeType  = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                    ? 'audio/webm;codecs=opus'
                    : MediaRecorder.isTypeSupported('audio/ogg;codecs=opus')
                    ? 'audio/ogg;codecs=opus' : '';
      mediaRecorder = mimeType
                    ? new MediaRecorder(stream, {mimeType: mimeType})
                    : new MediaRecorder(stream);
      mediaRecorder.ondataavailable = function (ev) {
        if (ev.data && ev.data.size > 0) audioChunks.push(ev.data);
      };
      mediaRecorder.start(100);
      isRecording = true;

      // ★ 美化：录音中改成动态波纹效果
      var btn = document.getElementById('chat-mic');
      if (btn) {
        btn.setAttribute('data-asr-recording', '1');
        btn.innerHTML = _micRecordingIcon();
        btn.style.cssText += ';position:relative;overflow:visible;';
      }
      _startRipple();
    } catch (err) {
      console.warn('[ASR] 麦克风权限被拒或不支持:', err);
      var btn = document.getElementById('chat-mic');
      if (btn && window.toast) toast('麦克风权限被拒绝，请在系统设置中允许');
    }
  }

  async function stopRec(e) {
    if (!isRecording || !mediaRecorder) return;
    isRecording = false;
    // 转写中：换成加载动画
    var btn = document.getElementById('chat-mic');
    if (btn) {
      btn.innerHTML = '<svg viewBox="0 0 24 24" style="animation:asrSpin 1s linear infinite">'
                    + '<path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8z" opacity=".3"/>'
                    + '<path d="M12 2a10 10 0 0 1 10 10h-2a8 8 0 0 0-8-8z"/>'
                    + '</svg>';
      // 注入旋转动画
      if (!document.getElementById('asr-style')) {
        var s = document.createElement('style');
        s.id = 'asr-style';
        s.textContent = '@keyframes asrSpin{to{transform:rotate(360deg)}}';
        document.head.appendChild(s);
      }
    }

    mediaRecorder.stop();
    mediaRecorder.stream.getTracks().forEach(function (t) { t.stop(); });

    await new Promise(function (res) {
      mediaRecorder.onstop = res;
    });

    var mimeType = mediaRecorder.mimeType || 'audio/webm';
    var ext      = mimeType.includes('ogg') ? 'ogg' : 'webm';
    var blob     = new Blob(audioChunks, {type: mimeType});

    if (blob.size < 1000) {            // 太短不发
      if (btn) btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>';
      return;
    }

    try {
      var formData = new FormData();
      formData.append('file', blob, 'audio.' + ext);

      var res  = await fetch('/api/asr', {method: 'POST', body: formData});
      var data = await res.json();

      if (data.text && data.text.trim()) {
        var inputEl = document.getElementById('chat-input');
        if (inputEl) {
          inputEl.value = data.text.trim();
          inputEl.dispatchEvent(new Event('input', {bubbles: true}));
        }
        // ★ 核心：触发现有发送按钮，聊天链路完全不动
        var sendBtn = document.getElementById('chat-send');
        if (sendBtn && !sendBtn.classList.contains('disabled')) {
          sendBtn.click();
        } else if (inputEl) {
          // 发送按钮不可用时，至少把文本留在输入框让用户手动发
          inputEl.focus();
        }
      } else {
        console.warn('[ASR] 识别结果为空:', data);
        if (window.toast) toast('语音识别失败，请重试');
      }
    } catch (err) {
      console.warn('[ASR] 上传失败:', err);
      if (window.toast) toast('语音上传失败：' + err.message);
    } finally {
      _stopRipple();
      var btn = document.getElementById('chat-mic');
      if (btn) {
        btn.removeAttribute('data-asr-recording');
        btn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2z"/></svg>';
        btn.style.cssText = btn.style.cssText
          .replace(/position:[^;]+;?/g, '')
          .replace(/overflow:[^;]+;?/g, '');
      }
    }
  }

  // 启动
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
