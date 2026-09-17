'use strict';
/* ============================================================
   外置记忆库：日历式浏览（对齐「关系档案」的日历交互）
   - 日历：有记忆的日期高亮，点某天 → 当天的 日总结 + 原文
   - 日历下方：周总结 / 月总结 列表，点开阅读
   数据：/api/external_memory/list + /read（按角色隔离）
   ============================================================ */

const _EM_DAY_CATS = ['原文', '日总结'];   // 按天存储的两类（进日历）
const _EM_WEEK_ICON = '🗓️';
const _EM_MONTH_ICON = '🌙';

function _emCharOf() {
  try {
    if (typeof Chat !== 'undefined' && Chat.contact && Chat.contact.name) return Chat.contact.name;
  } catch (_) {}
  const contacts = Store.listContacts();
  return contacts.length ? contacts[0].name : 'default';
}

const ExtMemory = {
  char: '',
  view: 'root',          // root | day | file
  _calYear: null,
  _calMonth: null,       // 0-based
  selectedDate: '',
  fileCat: '',           // 周总结 / 月总结（阅读视图用）
  file: '',
  listData: null,

  open(charId) {
    this.char = charId || _emCharOf();
    const now = new Date();
    this._calYear = now.getFullYear();
    this._calMonth = now.getMonth();
    this.view = 'root';
    this.selectedDate = '';
    this.file = '';
    this.listData = null;
    this.render();
    $('#extmem-page').classList.add('open');
  },

  close() {
    $('#extmem-page').classList.remove('open');
  },

  get charLabel() {
    return this.char === 'default' ? 'AI' : this.char;
  },

  render() {
    const body = $('#extmem-body');
    body.innerHTML = '';
    if (this.view === 'root') this._renderRoot(body);
    else if (this.view === 'day') this._renderDay(body);
    else this._renderFile(body);
  },

  _ensureList: async function () {
    if (this.listData) return this.listData;
    const loading = h('div', { class: 'empty-state' }, h('div', { text: '加载中…' }));
    $('#extmem-body').appendChild(loading);
    try {
      const r = await fetch('/api/external_memory/list?character_id=' + encodeURIComponent(this.char));
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || '加载失败');
      this.listData = j.data || {};
    } catch (e) {
      this.listData = { _error: e.message || String(e) };
    }
    loading.remove();
    return this.listData;
  },

  async _readFile(cat, name) {
    try {
      const r = await fetch('/api/external_memory/read?character_id=' + encodeURIComponent(this.char)
        + '&category=' + encodeURIComponent(cat) + '&name=' + encodeURIComponent(name));
      const j = await r.json();
      if (!j.ok) throw new Error(j.error || '读取失败');
      return j.content || '';
    } catch (e) {
      return '读取失败：' + (e.message || e);
    }
  },

  /* ---------------- 根视图：日历 + 周/月总结 ---------------- */
  async _renderRoot(body) {
    const data = await this._ensureList();
    if (data._error) {
      body.appendChild(h('div', { class: 'empty-state' }, h('div', { text: '加载失败：' + data._error })));
      return;
    }

    // ---- 日历（有 原文/日总结 的日期高亮）----
    // ★ 后端 list 返回的是文件名（带 .md 后缀），日历比对的是纯日期串——
    //   不去后缀的话 activeSet.has('2026-09-08') 永远 false，整个日历一块高亮都没有、
    //   格子也不绑点击（「日历点不了」的根因）。
    const _stripMd = (s) => String(s || '').replace(/\.md$/i, '');
    const activeDates = [...new Set([
      ...(data['原文'] || []).map(_stripMd),
      ...(data['日总结'] || []).map(_stripMd),
    ])];
    body.appendChild(this._buildCalendar(activeDates.sort().reverse()));

    // ---- 周总结 / 月总结 ----
    for (const [cat, icon] of [['周总结', _EM_WEEK_ICON], ['月总结', _EM_MONTH_ICON]]) {
      const items = (data[cat] || []);
      if (!items.length) continue;
      body.appendChild(h('div', { style: 'font-size:13px;font-weight:600;margin:16px 2px 8px',
        text: icon + ' ' + cat + '（' + items.length + '）' }));
      for (const name of items) {
        const row = h('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--card,#fff);border-radius:12px;padding:12px 14px;margin:0 2px 8px;cursor:pointer' },
          h('div', { style: 'font-size:14px', text: name }),
          h('span', { class: 'row-arrow', text: '›' }));
        row.addEventListener('click', () => { this.fileCat = cat; this.file = name; this.view = 'file'; this.render(); });
        body.appendChild(row);
      }
    }

    if (!activeDates.length) {
      body.appendChild(h('div', { class: 'empty-state' },
        h('div', { text: '还没有记忆。聊几天后会自动归档原文并生成日/周/月总结。' })));
    }
  },

  /* ---------------- 日历（复用关系档案的样式）---------------- */
  _buildCalendar(activeDates) {
    const wrap = h('div', { style: 'background:var(--card,#fff);border-radius:14px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);margin:0 2px 14px' });
    const activeSet = new Set(activeDates);

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

    const weekRow = h('div', { style: 'display:grid;grid-template-columns:repeat(7,1fr);text-align:center;font-size:12px;color:#999;margin-bottom:4px' });
    ['日', '一', '二', '三', '四', '五', '六'].forEach((w) => weekRow.appendChild(h('div', { text: w })));
    wrap.appendChild(weekRow);

    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const grid = h('div', { style: 'display:grid;grid-template-columns:repeat(7,1fr);gap:4px' });
    for (let i = 0; i < firstDay; i++) grid.appendChild(h('div', {}));

    const todayStr = dateStrOf(Date.now());
    for (let d = 1; d <= daysInMonth; d++) {
      const dateStr = year + '-' + pad2(month + 1) + '-' + pad2(d);
      const hasMem = activeSet.has(dateStr);
      const isToday = dateStr === todayStr;
      const cell = h('div', {
        style: 'position:relative;height:36px;display:flex;align-items:center;justify-content:center;border-radius:8px;cursor:pointer;font-size:13px;' +
          (hasMem ? 'background:var(--accent-soft,#fde7ee);color:var(--accent,#e25b7c);font-weight:600;' : 'color:#555;') +
          (isToday ? 'box-shadow:inset 0 0 0 1px var(--accent,#e25b7c);' : ''),
        text: String(d),
      });
      if (hasMem) {
        cell.addEventListener('click', () => { this.selectedDate = dateStr; this.view = 'day'; this.render(); });
      }
      grid.appendChild(cell);
    }
    wrap.appendChild(grid);
    wrap.appendChild(h('div', { style: 'font-size:11px;color:#aaa;margin-top:8px;text-align:center',
      text: '高亮 = 那天有记忆（原文/日总结）· 点击回看那天' }));
    return wrap;
  },

  /* ---------------- 某天：日总结 + 原文 ---------------- */
  async _renderDay(body) {
    const dateStr = this.selectedDate;
    const backBar = h('div', { style: 'display:flex;align-items:center;gap:8px;margin:2px 2px 12px' });
    const backBtn = h('button', { text: '‹ 返回日历', style: 'border:none;background:var(--card,#fff);border-radius:8px;padding:6px 12px;font-size:13px;color:var(--accent,#e25b7c);cursor:pointer' });
    backBtn.addEventListener('click', () => { this.view = 'root'; this.render(); });
    backBar.appendChild(backBtn);
    backBar.appendChild(h('div', { style: 'font-size:14px;font-weight:600', text: dateStr }));
    body.appendChild(backBar);

    const daily = await this._readFile('日总结', dateStr + '.md');
    if (daily && !daily.startsWith('读取失败')) {
      body.appendChild(h('div', { style: 'font-size:13px;font-weight:600;margin:4px 2px 6px', text: '📅 这天的记忆总结' }));
      body.appendChild(h('div', { class: 'help-box', style: 'white-space:pre-wrap;word-break:break-word;line-height:1.7;margin:0 2px 12px', text: daily }));
    }

    const raw = await this._readFile('原文', dateStr + '.md');
    if (raw && !raw.startsWith('读取失败')) {
      body.appendChild(h('div', { style: 'font-size:13px;font-weight:600;margin:4px 2px 6px', text: '📜 这天的聊天原文' }));
      const box = h('div', { class: 'help-box', style: 'white-space:pre-wrap;word-break:break-word;line-height:1.7;margin:0 2px 12px;max-height:55vh;overflow-y:auto', text: raw });
      body.appendChild(box);
    }
    if ((!daily || daily.startsWith('读取失败')) && (!raw || raw.startsWith('读取失败'))) {
      body.appendChild(h('div', { class: 'empty-state' }, h('div', { text: '这天没有找到记忆内容' })));
    }
  },

  /* ---------------- 周总结/月总结阅读 ---------------- */
  async _renderFile(body) {
    const backBar = h('div', { style: 'display:flex;align-items:center;gap:8px;margin:2px 2px 12px' });
    const backBtn = h('button', { text: '‹ 返回', style: 'border:none;background:var(--card,#fff);border-radius:8px;padding:6px 12px;font-size:13px;color:var(--accent,#e25b7c);cursor:pointer' });
    backBtn.addEventListener('click', () => { this.view = 'root'; this.render(); });
    backBar.appendChild(backBtn);
    backBar.appendChild(h('div', { style: 'font-size:14px;font-weight:600', text: (this.fileCat === '周总结' ? _EM_WEEK_ICON : _EM_MONTH_ICON) + ' ' + this.file }));
    body.appendChild(backBar);

    const pre = h('div', { class: 'help-box', style: 'white-space:pre-wrap;word-break:break-word;line-height:1.7;min-height:120px', text: '读取中…' });
    body.appendChild(pre);
    const content = await this._readFile(this.fileCat, this.file);
    pre.textContent = content || '（空）';
  },
};

/* ---------------- 事件绑定 ---------------- */
$('#extmem-back').addEventListener('click', () => {
  if (ExtMemory.view !== 'root') { ExtMemory.view = 'root'; ExtMemory.render(); }
  else ExtMemory.close();
});
$('#extmem-refresh').addEventListener('click', () => {
  ExtMemory.listData = null;
  ExtMemory.render();
});
$('#extmem-export').addEventListener('click', () => {
  const y = ExtMemory._calYear, m = ExtMemory._calMonth + 1;
  const menuRow = (label, fn) => {
    const r = h('div', { class: 'cp-life-row', style: 'margin-bottom:8px' },
      h('div', { class: 'cp-life-body' }, h('div', { class: 'cp-life-name', text: label })));
    r.addEventListener('click', async () => {
      try {
        toast('正在导出…');
        const r2 = await fetch('/api/external_memory/export?character_id='
          + encodeURIComponent(ExtMemory.char) + '&year=' + y + '&month=' + m);
        const j = await r2.json();
        if (!j.ok) throw new Error(j.error || '导出失败');
        if (!j.content || j.content.length < 40) { toast('这个时段还没有记忆内容'); return; }
        await fetch('api/library/write', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: j.name, content: j.content }),
        });
        toast('已导出到记忆库文件夹：' + j.name);
        Sheet.close();
      } catch (e) { toast('导出失败：' + e.message); }
    });
    return r;
  };
  Sheet.open(h('div', {},
    h('div', { class: 'sheet-title', text: '导出外置记忆' }),
    h('div', { style: 'padding:0 4px 10px;color:#888;font-size:12px',
      text: '导出为单篇 Markdown，保存到「记忆库」文件夹（功能页可查看管理）。' }),
    menuRow('📤 导出 ' + y + '年' + m + '月'),
    menuRow('📤 导出 ' + y + ' 全年'),
  ));
});
