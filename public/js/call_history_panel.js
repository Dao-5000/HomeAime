'use strict';
// call_history_panel.js — 通话记录面板
const CallHistoryPanel = {
  _overlay: null,

  open(contactId, contactName) {
    if (this._overlay) return;
    this._render(contactId, contactName);
  },

  async _render(contactId, contactName) {
    const overlay = document.createElement('div');
    overlay.id = 'call-history-overlay';
    overlay.innerHTML = `
      <div class="vc-panel">
        <div class="vc-header">
          <span>📞 ${contactName || 'AI'} 的通话记录</span>
          <button class="vc-close" id="ch-close">✕</button>
        </div>
        <div class="vc-body" id="ch-list" style="max-height:60vh;overflow:auto">
          加载中…
        </div>
      </div>`;
    document.body.appendChild(overlay);
    this._overlay = overlay;
    requestAnimationFrame(() => overlay.classList.add('show'));
    overlay.querySelector('#ch-close').addEventListener('click', () => this._close());
    // 点半透明背景也关闭，避免只能点✕按钮
    overlay.addEventListener('click', (e) => { if (e.target === overlay) this._close(); });

    try {
      const sid = window.Session ? Session.getSessionId() : 'default';
      // ★ 用角色名查通话记录（call_records 里 character_id 是角色名）
      const resp = await fetch(`/api/call/history?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(contactName || contactId)}`);
      const data = await resp.json();
      const list = data.records || [];
      const el = overlay.querySelector('#ch-list');
      if (!list.length) {
        el.innerHTML = '<div style="padding:20px;text-align:center;color:var(--text-3);font-size:13px">还没有通话记录</div>';
        return;
      }
      el.innerHTML = list.map(r => {
        const d = new Date((r.start_time || 0) * 1000);
        const date = `${d.getMonth()+1}月${d.getDate()}日 ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
        const dur = Math.floor((r.duration_sec || 0) / 60) + '分' + ((r.duration_sec || 0) % 60) + '秒';
        const src = r.call_source === 'ai_initiated' ? 'AI来电' : '你拨打';
        const res = r.call_result === 'completed' ? '✅' : '❌';
        return `<div style="padding:10px 4px;border-bottom:1px solid var(--border,#eee);display:flex;justify-content:space-between;font-size:13px">
          <span>${date}</span><span>${src}</span><span>${dur}</span><span>${res}</span>
        </div>`;
      }).join('');
    } catch (e) {
      overlay.querySelector('#ch-list').innerHTML = '<div style="padding:20px;color:var(--text-3)">加载失败</div>';
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
