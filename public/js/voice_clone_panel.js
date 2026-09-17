'use strict';
// voice_clone_panel.js — 音色克隆面板（补完空壳）
const VoiceClonePanel = {
  _overlay: null,
  _contactId: '',

  open(contactId) {
    if (this._overlay) return;
    this._contactId = contactId || (window.Chat && Chat.contact ? Chat.contact.id : '');
    this._render();
  },

  _render() {
    const overlay = document.createElement('div');
    overlay.id = 'voice-clone-overlay';
    overlay.innerHTML = `
      <div class="vc-panel">
        <div class="vc-header">
          <span>克隆专属音色</span>
          <button class="vc-close" id="vc-clone-close">✕</button>
        </div>
        <div class="vc-body">
          <div class="vc-hint">上传 10~30 秒干净人声（MP3/WAV/M4A），AI 会用这个声音和你说话</div>
          <label class="vc-field">
            <span>音色名称</span>
            <input type="text" id="vc-clone-label" placeholder="比如：我的声音" maxlength="20">
          </label>
          <label class="vc-field">
            <span>克隆引擎</span>
            <select id="vc-clone-provider">
              <option value="cosyvoice">CosyVoice3（本地·推荐）</option>
              <option value="gptsovits">GPT-SoVITS（本地）</option>
              <option value="elevenlabs">ElevenLabs（云端）</option>
            </select>
          </label>
          <label class="vc-field" id="vc-prompt-text-wrap">
            <span>音频原文</span>
            <input type="text" id="vc-clone-prompt-text" placeholder="选填，但建议填：参考音频里实际说的话" maxlength="80">
          </label>
          <div class="vc-file-row">
            <input type="file" id="vc-clone-file" accept="audio/*" hidden>
            <button class="vc-file-btn" id="vc-clone-pick">📁 选择音频文件</button>
            <span class="vc-file-name" id="vc-clone-fname">未选择</span>
          </div>
          <div class="vc-engine-hint" id="vc-engine-hint"></div>
        </div>
        <div class="vc-footer">
          <button class="vc-cancel" id="vc-clone-cancel">取消</button>
          <button class="vc-confirm" id="vc-clone-confirm">开始克隆</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    this._overlay = overlay;
    requestAnimationFrame(() => overlay.classList.add('show'));

    const fileInput = overlay.querySelector('#vc-clone-file');
    const fnameEl   = overlay.querySelector('#vc-clone-fname');
    const provSel   = overlay.querySelector('#vc-clone-provider');
    const engineHint = overlay.querySelector('#vc-engine-hint');

    const _updateEngineHint = () => {
      const p = provSel.value;
      if (p === 'cosyvoice')
        engineHint.textContent = '需要本地启动 CosyVoice3 API（localhost:9881）';
      else if (p === 'gptsovits')
        engineHint.textContent = '需要本地启动 GPT-SoVITS API（localhost:9880）';
      else if (p === 'elevenlabs')
        engineHint.textContent = '需要配置 ELEVENLABS_API_KEY 环境变量';
      else
        engineHint.textContent = '';
    };
    _updateEngineHint();
    provSel.addEventListener('change', _updateEngineHint);

    overlay.querySelector('#vc-clone-pick').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      fnameEl.textContent = fileInput.files[0] ? fileInput.files[0].name : '未选择';
    });

    overlay.querySelector('#vc-clone-close').addEventListener('click', () => this._close());
    overlay.querySelector('#vc-clone-cancel').addEventListener('click', () => this._close());
    overlay.querySelector('#vc-clone-confirm').addEventListener('click', () => this._submit(fileInput));
  },

  async _submit(fileInput) {
    let file = fileInput.files[0];
    const label = document.getElementById('vc-clone-label').value.trim();
    const provider = document.getElementById('vc-clone-provider').value;
    const promptText = (document.getElementById('vc-clone-prompt-text') || {}).value || '';
    if (!file) { toast('请先选择音频文件'); return; }
    if (!label) { toast('请填写音色名称'); return; }

    // Normalize compressed references to a real WAV before upload. This avoids
    // CosyVoice treating mp3/m4a bytes as a wav and reverting to its demo voice.
    if (provider === 'cosyvoice' && file && !/\.wav$/i.test(file.name || '')) {
      try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const decoded = await ctx.decodeAudioData(await file.arrayBuffer());
        const length = decoded.length;
        const channels = decoded.numberOfChannels;
        const bytes = new ArrayBuffer(44 + length * channels * 2);
        const view = new DataView(bytes);
        const write = (offset, value) => {
          for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i));
        };
        write(0, 'RIFF'); view.setUint32(4, 36 + length * channels * 2, true);
        write(8, 'WAVE'); write(12, 'fmt '); view.setUint32(16, 16, true);
        view.setUint16(20, 1, true); view.setUint16(22, channels, true);
        view.setUint32(24, decoded.sampleRate, true);
        view.setUint32(28, decoded.sampleRate * channels * 2, true);
        view.setUint16(32, channels * 2, true); view.setUint16(34, 16, true);
        write(36, 'data'); view.setUint32(40, length * channels * 2, true);
        let offset = 44;
        for (let i = 0; i < length; i++) {
          for (let ch = 0; ch < channels; ch++) {
            const sample = Math.max(-1, Math.min(1, decoded.getChannelData(ch)[i]));
            view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
            offset += 2;
          }
        }
        await ctx.close().catch(() => {});
        file = new File([bytes], (file.name || 'reference').replace(/\.[^.]+$/, '') + '.wav', {
          type: 'audio/wav',
        });
      } catch (e) {
        toast('该音频无法解码，请换成清晰的 WAV/MP3 语音');
        return;
      }
    }

    const confirmBtn = document.getElementById('vc-clone-confirm');
    confirmBtn.disabled = true;
    confirmBtn.textContent = '克隆中…';

    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('label', label);
      fd.append('provider', provider);
      fd.append('user_id', this._contactId || 'default');
      fd.append('prompt_text', promptText.trim());

      const resp = await fetch('/api/voices/clone', { method: 'POST', body: fd });
      const data = await resp.json();
      if (data.success) {
        // Keep the local contact in sync with the backend binding. Otherwise
        // the next call can still send the stale default CosyVoice key.
        const localContact = window.Chat && Chat.contact ? Chat.contact : null;
        toast((data.warning ? '克隆已保存：' : '克隆成功：') + label);
        // 自动绑定到当前角色
        const charName = window.Chat && Chat.contact ? Chat.contact.name : '';
        if (charName && data.voice_key) {
          try {
            const bindResp = await fetch('/api/character/voice', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                character_name: charName,
                voice_key: data.voice_key,
                voice: data.voice || {
                  voice_key: data.voice_key,
                  provider,
                  label,
                  prompt_text: promptText.trim()
                }
              })
            });
            const bindData = await bindResp.json();
            if (!bindResp.ok || !bindData.ok) {
              throw new Error(bindData.error || 'voice binding failed');
            }
            if (localContact) {
              const voice = data.voice || { voice_key: data.voice_key, provider, label, prompt_text: promptText.trim() };
              if (window.Store && typeof Store.updateContact === 'function') {
                Store.updateContact(localContact.id, { voice, voiceKey: data.voice_key });
              }
              localContact.voice = voice;
              localContact.voiceKey = data.voice_key;
            }
          } catch (e) { /* 静默 */ }
        }
        // 刷新音色列表（如果 profile.js 的列表在页面上）
        if (typeof refreshVoiceList === 'function') refreshVoiceList();
        if (data.warning) setTimeout(() => toast(data.warning, 4500), 350);
        this._close();
      } else {
        toast(data.msg || '克隆失败');
        confirmBtn.disabled = false;
        confirmBtn.textContent = '开始克隆';
      }
    } catch (e) {
      toast('上传失败，请检查网络');
      confirmBtn.disabled = false;
      confirmBtn.textContent = '开始克隆';
    }
  },

  _close() {
    if (!this._overlay) return;
    const ov = this._overlay;
    ov.classList.remove('show');
    setTimeout(() => { if (ov.parentNode) ov.parentNode.removeChild(ov); }, 300);
    this._overlay = null;
  }
};
