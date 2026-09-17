'use strict';
/* ============================================================
   按住说话控制电脑（Push-To-Talk）
   - 后端全局键盘钩子（F9）推 ptt_start / ptt_stop 到 WS
   - 按住期间 MediaRecorder 录麦克风；松开后音频发 /api/pc/voice_control
   - 后端：识别 → 意图（规则+LLM）→ 执行（打开应用/音量/媒体/打字/看屏幕）→ TTS 反馈
   - App 最小化也有效（录音依赖 getUserMedia，不受页面节流影响）
   ============================================================ */

const PcPTT = (() => {
  let rec = null;
  let chunks = [];
  let stream = null;
  let starting = false;
  let floatEl = null;

  function _showFloat(text) {
    if (!floatEl) {
      floatEl = document.createElement('div');
      floatEl.style.cssText = 'position:fixed;left:50%;top:18%;transform:translateX(-50%);'
        + 'z-index:99999;background:rgba(0,0,0,.78);color:#fff;padding:14px 22px;border-radius:14px;'
        + 'font-size:15px;pointer-events:none;box-shadow:0 4px 18px rgba(0,0,0,.3);'
        + 'max-width:80vw;text-align:center;line-height:1.6';
      document.body.appendChild(floatEl);
    }
    floatEl.style.display = 'block';
    floatEl.textContent = text;
  }
  function _hideFloat() { if (floatEl) floatEl.style.display = 'none'; }

  async function start() {
    if (starting || rec) return;
    starting = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      chunks = [];
      const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus' : 'audio/webm';
      rec = new MediaRecorder(stream, { mimeType: mime });
      rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
      rec.start(250);
      _showFloat('🎤 在听…松开 F9 我就去办');
    } catch (e) {
      _showFloat('麦克风不可用：' + (e.message || e));
      setTimeout(_hideFloat, 2500);
      stream = null;
    }
    starting = false;
  }

  async function stop() {
    if (!rec) { _hideFloat(); return; }
    const r = rec;
    rec = null;
    const done = new Promise((res) => { r.onstop = res; });
    try { r.stop(); } catch (_) {}
    await done;
    try { if (stream) stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
    stream = null;
    _showFloat('识别中…');

    const blob = new Blob(chunks, { type: 'audio/webm' });
    if (blob.size < 800) { _hideFloat(); return; }
    const b64 = await new Promise((res) => {
      const fr = new FileReader();
      fr.onload = () => res(String(fr.result).split(',')[1] || '');
      fr.readAsDataURL(blob);
    });

    try {
      const resp = await fetch('/api/pc/voice_control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          audio: b64,
          character_id: (typeof Chat !== 'undefined' && Chat.contact && Chat.contact.name) || '助手',
        }),
      });
      const j = await resp.json();
      if (!j.ok) {
        _showFloat('失败：' + (j.error || '未知'));
        setTimeout(_hideFloat, 2500);
        return;
      }
      _showFloat(j.reply || '好了～');
      // 语音反馈（有就播，播完再收浮层）
      if (j.audio) {
        try {
          const ab = Uint8Array.from(atob(j.audio), (c) => c.charCodeAt(0));
          const ctx = new (window.AudioContext || window.webkitAudioContext)();
          const decoded = await ctx.decodeAudioData(ab.buffer.slice(0));
          const src = ctx.createBufferSource();
          src.buffer = decoded;
          src.connect(ctx.destination);
          src.onended = () => { _hideFloat(); ctx.close().catch(() => {}); };
          src.start();
          return;
        } catch (_) { /* 播不了就走文字 */ }
      }
      setTimeout(_hideFloat, 2400);
    } catch (e) {
      _showFloat('失败：' + (e.message || e));
      setTimeout(_hideFloat, 2500);
    }
  }

  // ★ 感知系统动作提议：显示询问浮层 + 念出来，等用户按 F9 说「可以」
  //   （确认判断在后端状态机；浮层 75 秒后自动消失，与后端 TTL 一致）
  function showProposal(text, audioB64) {
    _showFloat('💡 ' + text);
    if (!audioB64) {
      setTimeout(() => { if (floatEl && floatEl.textContent.indexOf(text) >= 0) _hideFloat(); }, 12000);
      return;
    }
    try {
      const ab = Uint8Array.from(atob(audioB64), (c) => c.charCodeAt(0));
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      ctx.decodeAudioData(ab.buffer.slice(0), (decoded) => {
        const src = ctx.createBufferSource();
        src.buffer = decoded;
        src.connect(ctx.destination);
        src.onended = () => {
          ctx.close().catch(() => {});
          setTimeout(() => {
            if (floatEl && floatEl.textContent.indexOf(text) >= 0) _hideFloat();
          }, 8000);
        };
        src.start();
      }, () => ctx.close().catch(() => {}));
    } catch (_) { /* 播不了就纯文字 */ }
  }

  return { start, stop, showProposal };
})();

window.PcPTT = PcPTT;
