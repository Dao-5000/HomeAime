'use strict';
/* ============================================================
   关系档案：统计概览 + 日历 + 按日期查看聊天记录
   数据来自后端 /api/relationship/archive（本地数据库，不分页）
   ============================================================ */

const Archive = {
  contactId: null,
  view: 'overview',      // overview（统计+日历）| day（某天的消息）
  selectedDate: '',
  stats: null,
  _calYear: null,
  _calMonth: null,       // 0-based

  open(contactId) {
    this.contactId = contactId;
    this.view = 'overview';
    this.selectedDate = '';
    this.stats = null;
    this.render();
    $('#archive-page').classList.add('open');
  },

  close() {
    $('#archive-page').classList.remove('open');
  },

  get c() { return Store.getContact(this.contactId); },

  get sid() {
    return window.Session?.getSessionId?.() ||
      localStorage.getItem('ai_companion_session_id') ||
      localStorage.getItem('session_id') || 'default';
  },

  /* ---------------- 渲染入口 ---------------- */
  async render() {
    const c = this.c;
    if (!c) { this.close(); return; }
    const body = $('#archive-body');
    body.innerHTML = '';
    if (this.view === 'day') {
      await this._renderDay(body);
      return;
    }
    await this._renderOverview(body);
  },

  /* ---------------- 统计概览 + 日历 ---------------- */
  async _renderOverview(body) {
    body.appendChild(h('div', { class: 'help-box', style: 'margin:2px 2px 12px', text: '你们在一起的所有记录，都在这了。点日历里带标记的日期，可以回看那天的聊天。' }));

    const loading = h('div', { class: 'empty-state' }, h('div', { text: '加载中…' }));
    body.appendChild(loading);

    let stats = null;
    try {
      const res = await fetch('/api/relationship/archive?character_id=' + encodeURIComponent(this.c.name || this.c.id));
      const data = await res.json();
      if (!data || data.ok === false) throw new Error(data?.error || '加载失败');
      stats = data;
      this.stats = data;
    } catch (e) {
      loading.remove();
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('journal'),
        h('div', { text: '加载失败：' + (e.message || e) }),
      ));
      return;
    }
    loading.remove();

    // ---- 统计卡片 ----
    body.appendChild(this._buildStats(stats));

    // ---- 相识日期 ----
    if (stats.first_date && stats.days) {
      body.appendChild(h('div', { style: 'text-align:center;color:#8a8a8a;font-size:12px;margin:4px 0 12px' },
        h('span', { text: '已相识 ' + stats.days + ' 天 · 从 ' + stats.first_date + ' 起' }),
      ));
    }

    // ---- 日历 ----
    const today = new Date();
    if (this._calYear == null) {
      this._calYear = today.getFullYear();
      this._calMonth = today.getMonth();
    }
    body.appendChild(this._buildCalendar(stats.active_dates || []));
  },

  _buildStats(stats) {
    const items = [
      { n: stats.total_messages ?? 0, label: '条消息' },
      { n: stats.total_memories ?? 0, label: '条记忆' },
      { n: stats.total_calls ?? 0, label: '次通话' },
      { n: stats.total_stickers ?? 0, label: '个表情包' },
      { n: stats.total_images ?? 0, label: '张图片' },
      { n: fmtMusicMins(stats.music_minutes ?? 0), label: '一起听音乐' },
    ];
    const grid = h('div', {
      style: 'display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:0 2px 14px',
    });
    for (const it of items) {
      grid.appendChild(h('div', {
        style: 'background:var(--card,#fff);border-radius:12px;padding:14px 6px;text-align:center;box-shadow:0 1px 3px rgba(0,0,0,.06)',
      },
        h('div', { style: 'font-size:22px;font-weight:700;color:var(--accent,#e25b7c)', text: String(it.n) }),
        h('div', { style: 'font-size:12px;color:#999;margin-top:4px', text: it.label }),
      ));
    }
    return grid;
  },

  /* ---------------- 日历 ---------------- */
  _buildCalendar(activeDates) {
    const wrap = h('div', { style: 'background:var(--card,#fff);border-radius:14px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,.06)' });
    const activeSet = new Set(activeDates);

    // 头部：月份切换
    const year = this._calYear, month = this._calMonth;
    const header = h('div', { style: 'display:flex;align-items:center;justify-content:space-between;margin-bottom:10px' });
    const prevBtn = h('button', { text: '‹', style: 'border:none;background:none;font-size:22px;color:var(--accent,#e25b7c);cursor:pointer;padding:0 8px' });
    const nextBtn = h('button', { text: '›', style: 'border:none;background:none;font-size:22px;color:var(--accent,#e25b7c);cursor:pointer;padding:0 8px' });
    const title = h('div', { style: 'font-size:15px;font-weight:600', text: year + '年' + (month + 1) + '月' });
    prevBtn.addEventListener('click', () => { this._calMonth--; if (this._calMonth < 0) { this._calMonth = 11; this._calYear--; } this.render(); });
    nextBtn.addEventListener('click', () => { this._calMonth++; if (this._calMonth > 11) { this._calMonth = 0; this._calYear++; } this.render(); });
    header.appendChild(prevBtn);
    header.appendChild(title);
    header.appendChild(nextBtn);
    wrap.appendChild(header);

    // 星期表头
    const weekRow = h('div', { style: 'display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:12px;color:#999;margin-bottom:4px' });
    ['日', '一', '二', '三', '四', '五', '六'].forEach((w) => weekRow.appendChild(h('div', { text: w })));
    wrap.appendChild(weekRow);

    // 日期格子
    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const grid = h('div', { style: 'display:grid;grid-template-columns:repeat(7,1fr);gap:4px' });

    for (let i = 0; i < firstDay; i++) grid.appendChild(h('div', {}));

    const todayStr = dateStrOf(Date.now());
    for (let d = 1; d <= daysInMonth; d++) {
      const dateStr = year + '-' + pad2(month + 1) + '-' + pad2(d);
      const hasMsg = activeSet.has(dateStr);
      const isToday = dateStr === todayStr;
      const cell = h('div', {
        style: 'position:relative;height:36px;display:flex;align-items:center;justify-content:center;border-radius:8px;cursor:pointer;font-size:13px;' +
          (hasMsg ? 'background:var(--accent-soft,#fde7ee);color:var(--accent,#e25b7c);font-weight:600;' : 'color:#555;') +
          (isToday ? 'box-shadow:inset 0 0 0 1px var(--accent,#e25b7c);' : ''),
        text: String(d),
      });
      if (hasMsg) {
        cell.addEventListener('click', () => { this.selectedDate = dateStr; this.view = 'day'; this.render(); });
      }
      grid.appendChild(cell);
    }
    wrap.appendChild(grid);

    wrap.appendChild(h('div', { style: 'font-size:11px;color:#aaa;margin-top:8px;text-align:center', text: '粉色 = 那天聊过 · 点击查看当天记录' }));
    return wrap;
  },

  /* ---------------- 某天的消息 ---------------- */
  async _renderDay(body) {
    const dateStr = this.selectedDate;
    // 顶部返回栏
    const backBar = h('div', { style: 'display:flex;align-items:center;gap:8px;margin:2px 2px 12px' });
    const backBtn = h('button', { text: '‹ 返回日历', style: 'border:none;background:var(--card,#fff);border-radius:8px;padding:6px 12px;font-size:13px;color:var(--accent,#e25b7c);cursor:pointer' });
    backBtn.addEventListener('click', () => { this.view = 'overview'; this.render(); });
    backBar.appendChild(backBtn);
    backBar.appendChild(h('div', { style: 'font-size:14px;font-weight:600', text: dateStr }));
    body.appendChild(backBar);

    const loading = h('div', { class: 'empty-state' }, h('div', { text: '加载中…' }));
    body.appendChild(loading);

    let msgs = [];
    try {
      const res = await fetch('/api/relationship/archive/messages?character_id=' + encodeURIComponent(this.c.name || this.c.id) + '&date=' + encodeURIComponent(dateStr));
      const data = await res.json();
      if (!data || data.ok === false) throw new Error(data?.error || '加载失败');
      msgs = data.messages || [];
    } catch (e) {
      loading.remove();
      body.appendChild(h('div', { class: 'empty-state' }, h('div', { text: '加载失败：' + (e.message || e) })));
      return;
    }
    loading.remove();

    if (!msgs.length) {
      body.appendChild(h('div', { class: 'empty-state' }, emptyIcon('journal'), h('div', { text: '这一天没有记录' })));
      return;
    }

    body.appendChild(h('div', { style: 'font-size:12px;color:#999;margin:0 2px 8px', text: '共 ' + msgs.length + ' 条消息' }));

    const list = h('div', { style: 'display:flex;flex-direction:column;gap:8px;padding-bottom:20px' });
    for (const m of msgs) {
      list.appendChild(this._buildMsgBubble(m));
    }
    body.appendChild(list);
  },

  _buildMsgBubble(m) {
    const isUser = m.role === 'user';
    const time = (m.timestamp || '').slice(11, 16);
    const content = this._prettyContent(m.content || '');

    const bubble = h('div', {
      style: 'max-width:78%;padding:9px 12px;border-radius:12px;font-size:14px;line-height:1.5;word-break:break-word;' +
        (isUser
          ? 'align-self:flex-end;background:var(--accent,#e25b7c);color:#fff;border-bottom-right-radius:4px;'
          : 'align-self:flex-start;background:var(--card,#fff);color:#333;border-bottom-left-radius:4px;box-shadow:0 1px 2px rgba(0,0,0,.05);'),
    });
    if (content) bubble.appendChild(h('div', { text: content }));

    const timeEl = h('div', { style: 'font-size:10px;margin-top:3px;opacity:.6;text-align:right', text: time });
    bubble.appendChild(timeEl);

    const row = h('div', { style: 'display:flex;flex-direction:column' }, bubble);
    return row;
  },

  _prettyContent(content) {
    let t = String(content || '');
    // 表情包标记
    t = t.replace(/\[sticker:[^\]]*\]/g, '[表情包]');
    return t.trim();
  },
};

/* ---------------- 事件绑定 ---------------- */
$('#archive-back').addEventListener('click', () => {
  if (Archive.view === 'day') {
    Archive.view = 'overview';
    Archive.render();
  } else {
    Archive.close();
  }
});
$('#archive-done').addEventListener('click', () => Archive.close());

/* ---------------- 一起听音乐时长格式化 ---------------- */
function fmtMusicMins(minutes) {
  const m = Math.max(0, Math.round(Number(minutes) || 0));
  if (m < 60) return m + '分';
  const h = Math.floor(m / 60);
  const mm = m % 60;
  return mm === 0 ? (h + '小时') : (h + '时' + mm + '分');
}
