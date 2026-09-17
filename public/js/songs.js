'use strict';
/* ============================================================
   AI 学歌：导入整首歌 → 显示「已学会的歌」
   ------------------------------------------------------------
   阶段一落地导入 / 列表 / 试听 / 删除；
   后续接入 demucs 分离与 RVC 换声后，status 会从 imported 一路走到 ready。
   ============================================================ */

// 状态与后端 backend/singing/song_library.py 的 SONG_STATUS 对应。
// separating=分离人声伴奏 / converting=换声学唱 / rendering=合并伴奏
const SONGS_STATUS_BADGE = {
  imported:   { t: '已导入',   c: '#8a8a8a' },
  separating: { t: '分离中',   c: '#e8a33d' },
  converting: { t: '学唱中',   c: '#b07cff' },
  rendering:  { t: '合流中',   c: '#4a9eff' },
  ready:      { t: '✓ 已学会', c: '#35c26a' },
  failed:     { t: '失败',     c: '#e05c5c' },
};

// 仍在处理的阶段 —— 处于这些状态时前端持续轮询刷新进度
function _spIsBusy(s) {
  return s.status === 'separating' || s.status === 'converting' || s.status === 'rendering';
}

function _spEscape(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (m) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]
  ));
}

const SongsPanel = {
  _overlay: null,
  _audio: null,

  open() {
    if (this._overlay) return;
    this._render();
  },

  _ctx() {
    const sid = window.Session ? Session.getSessionId() : 'default';
    const cid = (window.Chat && Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default';
    return { sid, cid };
  },

  async _render() {
    const overlay = document.createElement('div');
    overlay.id = 'songs-overlay';
    overlay.innerHTML = `
      <div class="vc-panel" style="width:420px;max-width:92vw">
        <div class="vc-header">
          <span>🎵 AI 学歌</span>
          <button class="vc-close" id="sp-close">✕</button>
        </div>
        <div class="vc-body">
          <div class="vc-hint">导入一整首歌，之后她就能带着伴奏唱给你听。导入后需要分离人声与伴奏、再用她的音色学习，完成后状态会变成「已学会」。</div>
          <div style="display:flex;gap:8px;margin-bottom:10px">
            <input id="sp-title" placeholder="歌名（留空则用文件名）"
              style="flex:1;min-width:0;padding:8px 10px;border:1px solid var(--border,#ddd);border-radius:8px;font-size:13px;background:var(--bg-0,#fff);color:var(--text-1,#222)">
            <button class="btn btn-primary" id="sp-pick" style="white-space:nowrap">导入歌曲</button>
          </div>
          <input type="file" id="sp-file" accept="audio/*" style="display:none">
          <div id="sp-list" style="max-height:50vh;overflow:auto">加载中…</div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    this._overlay = overlay;
    requestAnimationFrame(() => overlay.classList.add('show'));
    overlay.querySelector('#sp-close').addEventListener('click', () => this._close());

    const fileInput = overlay.querySelector('#sp-file');
    const pickBtn = overlay.querySelector('#sp-pick');
    const titleEl = overlay.querySelector('#sp-title');
    pickBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
      const f = fileInput.files && fileInput.files[0];
      if (!f) return;
      await this._upload(f, (titleEl.value || '').trim());
      fileInput.value = '';
      titleEl.value = '';
    });

    await this._loadList();
  },

  async _upload(file, title) {
    const listEl = this._overlay && this._overlay.querySelector('#sp-list');
    if (listEl) listEl.innerHTML = '<div style="padding:16px;color:var(--text-3,#999);font-size:13px">上传中…</div>';
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('title', title || '');
      const { sid, cid } = this._ctx();
      fd.append('session_id', sid);
      fd.append('character_id', cid);
      const resp = await fetch('/api/songs/import', { method: 'POST', body: fd });
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || '导入失败');
      toast('已导入《' + (data.song && data.song.title) + '》');
    } catch (e) {
      toast('导入失败：' + (e && e.message));
    }
    await this._loadList();
  },

  async _loadList() {
    const listEl = this._overlay && this._overlay.querySelector('#sp-list');
    if (!listEl) return;
    try {
      const { sid, cid } = this._ctx();
      const resp = await fetch('/api/songs?session_id=' + encodeURIComponent(sid)
        + '&character_id=' + encodeURIComponent(cid));
      const data = await resp.json();
      const list = (data && data.songs) || [];
      if (!list.length) {
        listEl.innerHTML = '<div style="padding:18px;text-align:center;color:var(--text-3,#999);font-size:13px">还没有导入任何歌</div>';
        return;
      }
      listEl.innerHTML = list.map((s) => this._row(s)).join('');
      listEl.querySelectorAll('[data-del]').forEach((b) => {
        b.addEventListener('click', () => this._remove(b.getAttribute('data-del')));
      });
      listEl.querySelectorAll('[data-play]').forEach((b) => {
        b.addEventListener('click', () => this._play(
          b.getAttribute('data-play'), b.getAttribute('data-kind') || 'original'));
      });
      listEl.querySelectorAll('[data-learn]').forEach((b) => {
        b.addEventListener('click', () => this._learn(b.getAttribute('data-learn')));
      });
      listEl.querySelectorAll('[data-cancel]').forEach((b) => {
        b.addEventListener('click', () => this._cancel(b.getAttribute('data-cancel')));
      });
      // 有歌在学习中就开轮询，让状态自己走完
      if (list.some(_spIsBusy)) {
        this._startPolling();
      }
    } catch (e) {
      listEl.innerHTML = '<div style="padding:18px;color:var(--text-3,#999);font-size:13px">加载失败</div>';
    }
  },

  _row(s) {
    const b = SONGS_STATUS_BADGE[s.status] || { t: s.status || '未知', c: '#8a8a8a' };
    const meta = [s.size_mb ? s.size_mb + 'MB' : '', s.error || ''].filter(Boolean).join(' · ');
    const busy = _spIsBusy(s);
    // 学会之后优先放成品（她带伴奏唱的版本），没学会就只能听原曲
    const kind = s.has_final ? 'final' : 'original';
    const playLabel = s.has_final ? '听她唱' : '听原曲';
    // 学习中：把后端回写的阶段文案显示出来（"用她的音色学唱"），
    // 否则整首歌要走几分钟，用户只看得到"分离中"三个字，不知道有没有在动。
    const detail = busy
      ? (s.stage ? _spEscape(s.stage) : _spEscape(b.t))
      : meta;
    const pct = Math.max(0, Math.min(100, Number(s.progress) || 0));
    const bar = busy
      ? `<div style="height:3px;background:var(--border,#e6e6e6);border-radius:2px;margin-top:5px;overflow:hidden">
           <div style="height:100%;width:${pct}%;background:${b.c};border-radius:2px;transition:width .5s ease"></div>
         </div>`
      : '';
    return `<div style="display:flex;align-items:center;gap:8px;padding:10px 4px;border-bottom:1px solid var(--border,#eee);font-size:13px">
      <div style="flex:1;min-width:0">
        <div style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_spEscape(s.title)}</div>
        <div style="font-size:11px;color:var(--text-3,#999);margin-top:2px">
          <span style="color:${b.c}">${b.t}</span>${detail ? ' · ' + detail : ''}
        </div>
        ${bar}
      </div>
      <button class="chip" data-play="${_spEscape(s.id)}" data-kind="${kind}">${playLabel}</button>
      ${busy
        ? '<button class="chip" data-cancel="' + _spEscape(s.id) + '">中止</button>'
        : '<button class="chip" data-learn="' + _spEscape(s.id) + '">重新学习</button>'}
      <button class="chip" data-del="${_spEscape(s.id)}">删除</button>
    </div>`;
  },

  async _learn(id) {
    try {
      await fetch('/api/songs/' + encodeURIComponent(id) + '/learn', { method: 'POST' });
      toast('开始学习，完成后状态会变成「已学会」');
      this._startPolling();
    } catch (e) {
      toast('启动失败');
    }
    await this._loadList();
  },

  async _cancel(id) {
    try {
      await fetch('/api/songs/' + encodeURIComponent(id) + '/cancel', { method: 'POST' });
      toast('已中止，可以重新学习');
    } catch (e) {
      toast('中止失败');
    }
    this._stopPolling();
    await this._loadList();
  },

  /** 学习中的歌自动刷新状态，省得手动点刷新 */
  _startPolling() {
    if (this._timer) return;
    const tick = async () => {
      if (!this._overlay) { this._stopPolling(); return; }
      try {
        const { sid, cid } = this._ctx();
        const r = await fetch('/api/songs?session_id=' + encodeURIComponent(sid)
          + '&character_id=' + encodeURIComponent(cid));
        const d = await r.json();
        if ((d.songs || []).some(_spIsBusy)) await this._loadList();
        else this._stopPolling();
      } catch (e) { this._stopPolling(); }
    };
    this._timer = setInterval(tick, 3000);
  },

  _stopPolling() {
    if (this._timer) { clearInterval(this._timer); this._timer = null; }
  },

  async _remove(id) {
    if (!window.confirm('确定删除这首歌及其全部产物？')) return;
    try {
      await fetch('/api/songs/' + encodeURIComponent(id), { method: 'DELETE' });
      toast('已删除');
    } catch (e) {
      toast('删除失败');
    }
    await this._loadList();
  },

  _play(id, kind) {
    try {
      if (this._audio) { this._audio.pause(); this._audio = null; }
      const a = new Audio('/api/songs/' + encodeURIComponent(id)
        + '/audio?kind=' + encodeURIComponent(kind || 'original'));
      this._audio = a;
      a.play().catch(() => toast('无法播放，可能格式不支持'));
    } catch (e) {
      toast('播放失败');
    }
  },

  _close() {
    if (!this._overlay) return;
    if (this._audio) { this._audio.pause(); this._audio = null; }
    const ov = this._overlay;
    ov.classList.remove('show');
    setTimeout(() => { if (ov.parentNode) ov.parentNode.removeChild(ov); }, 300);
    this._overlay = null;
  },
};

window.SongsPanel = SongsPanel;
