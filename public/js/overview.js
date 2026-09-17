'use strict';
/* ============================================================
   概览页：问候 / 统计 / 盲盒 / 早报 / 今日待办 / 最近聊天
   ============================================================ */

function greetingText() {
  const hh = new Date().getHours();
  if (hh < 5) return '夜深了，还在忙吗';
  if (hh < 8) return '早上好，新的一天';
  if (hh < 12) return '上午好，元气满满';
  if (hh < 14) return '中午好，记得吃饭';
  if (hh < 18) return '下午好';
  if (hh < 22) return '晚上好';
  return '夜深了，早点休息';
}

function renderOverview() {
  const wrap = $('#page-overview');
  wrap.innerHTML = '';
  const settings = Store.getSettings();
  const contacts = Store.listContacts();
  const todos = Store.todos.list();
  const todayStr = dateStrOf(Date.now());

  // 汇总记忆
  const logs = contacts.flatMap((c) => (c.logs || []).map((l) => ({ date: l.date, text: l.text, contact: c })))
    .sort((a, b) => (a.date < b.date ? 1 : -1));
  const totalDays = new Set(logs.map((l) => l.date)).size;
  const todayLog = logs.find((l) => l.date === todayStr);
  const yesterdayLog = logs.find((l) => l.date < todayStr);

  const undone = todos.filter((t) => !t.done);
  const dueToday = undone.filter((t) => t.due && t.due <= todayStr);
  const maxInti = contacts.length ? Math.max(...contacts.map((c) => intimacyInfo(c).v)) : 0;

  /* ---- 问候卡 ---- */
  wrap.appendChild(h('div', { class: 'ov-card greet' },
    h('div', { class: 'greet-big', text: greetingText() + '，' + (Store.getSettings().meNickname || '朋友') }),
    h('div', { class: 'greet-sub', text: dateStrOf(Date.now()) + ' · 周' + '日一二三四五六'[new Date().getDay()] }),
  ));

  /* ---- 统计条 ---- */
  const stats = h('div', { class: 'ov-stats' });
  const stat = (num, label) => h('div', { class: 'ov-stat' }, h('div', { class: 'ov-num', text: num }), h('div', { class: 'ov-label', text: label }));
  stats.appendChild(stat(String(totalDays), '记忆天数'));
  stats.appendChild(stat(String(undone.length), '未完成待办'));
  stats.appendChild(stat(String(contacts.length), 'AI 伴侣'));
  // ★ 2026-09-15：亲密度面板默认隐藏（后端 RELATIONSHIP_UI_VISIBLE=false）
  if (typeof window.relUIShow !== 'function' || window.relUIShow()) {
    stats.appendChild(stat(maxInti ? Math.round(maxInti) + '' : '—', '最高亲密度'));
  }
  wrap.appendChild(stats);

  /* ---- 今日盲盒 ---- */
  const blind = settings.blindBox && settings.blindBox.date === todayStr ? settings.blindBox : null;
  const blindCard = h('div', { class: 'ov-card blind' });
  if (blind && blind.opened) {
    blindCard.appendChild(h('div', { class: 'blind-title', text: '今日盲盒任务' }));
    blindCard.appendChild(h('div', { class: 'blind-task', text: blind.task }));
    blindCard.appendChild(h('div', { class: 'blind-foot' },
      h('button', { class: 'chip' + (blind.done ? ' selected' : ''), text: blind.done ? '✓ 已完成' : '完成打卡' }),
      h('span', { class: 'blind-note', text: '完成它，给生活一点小仪式感' }),
    ));
    blindCard.querySelector('button').addEventListener('click', () => {
      Store.saveSettings({ blindBox: Object.assign({}, blind, { done: !blind.done }) });
      renderOverview();
    });
  } else {
    blindCard.appendChild(h('div', { class: 'blind-title', text: '今日盲盒' }));
    blindCard.appendChild(h('div', { class: 'blind-task', text: blind ? '已抽取，点开看看今天的小任务' : '一个随机的小任务，等你来开' }));
    const openBtn = h('button', { class: 'btn btn-primary', text: '打开盲盒' });
    openBtn.addEventListener('click', () => {
      if (!blind) initBlindBox();
      const s = Store.getSettings();
      Store.saveSettings({ blindBox: Object.assign({}, s.blindBox, { opened: true }) });
      renderOverview();
    });
    blindCard.appendChild(openBtn);
  }
  wrap.appendChild(blindCard);

  /* ---- 今日早报 ---- */
  const report = settings.morningReport && settings.morningReport.date === todayStr ? settings.morningReport : null;
  const repCard = h('div', { class: 'ov-card report' });
  if (report) {
    const firstLine = (report.text || '').split('\n')[0] || '今日早报';
    repCard.appendChild(h('div', { class: 'blind-title', text: '今日早报' }));
    repCard.appendChild(h('div', { class: 'report-preview', text: firstLine }));
    repCard.appendChild(h('div', { class: 'blind-foot' }, h('span', { class: 'blind-note', text: '已生成 · 功能页可查看全文' })));
  } else {
    repCard.appendChild(h('div', { class: 'blind-title', text: '今日早报' }));
    repCard.appendChild(h('div', { class: 'blind-task', text: '还没生成今天的早报' }));
    const genBtn = h('button', { class: 'btn btn-primary', text: '生成早报' });
    genBtn.addEventListener('click', async () => {
      genBtn.textContent = '生成中…';
      genBtn.disabled = true;
      await generateMorningReport();
      renderOverview();
    });
    repCard.appendChild(genBtn);
  }
  wrap.appendChild(repCard);

  /* ---- 今日晚报（情绪记忆） ---- */
  const logCard = h('div', { class: 'ov-card log' });
  if (todayLog) {
    const p = parseLog(todayLog.text);
    logCard.appendChild(h('div', { class: 'blind-title', text: '今日情绪晚报' }));
    logCard.appendChild(h('div', { class: 'log-row' },
      p.score != null ? h('span', { class: 'log-score', text: p.score + '/10' }) : null,
      p.label ? h('span', { class: 'log-label', text: p.label }) : null,
      h('span', { class: 'blind-note', text: '来自 ' + todayLog.contact.name }),
    ));
    if (p.theme) logCard.appendChild(h('div', { class: 'report-preview', text: '「' + p.theme + '」' }));
    logCard.addEventListener('click', () => Logs.open(todayLog.contact.id));
  } else {
    logCard.appendChild(h('div', { class: 'blind-title', text: '今日情绪晚报' }));
    logCard.appendChild(h('div', { class: 'blind-task', text: yesterdayLog ? '昨晚 23:00 会自动生成，也可手动生成' : '还没有晚报，聊聊天就有了' }));
    const genBtn = h('button', { class: 'btn btn-plain', text: '手动生成' });
    genBtn.addEventListener('click', async () => {
      genBtn.textContent = '生成中…';
      genBtn.disabled = true;
      const target = contacts.find((c) => (c.logs || []).length);
      if (target) await Logs.generateNow(target.id);
      else toast('还没有对话记录');
      renderOverview();
    });
    logCard.appendChild(genBtn);
  }
  wrap.appendChild(logCard);

  /* ---- 今日待办 ---- */
  if (dueToday.length) {
    const td = h('div', { class: 'ov-card' });
    td.appendChild(h('div', { class: 'blind-title', text: '今日待办（' + dueToday.length + '）' }));
    for (const t of dueToday.slice(0, 3)) {
      const row = h('div', { class: 'todo-row' },
        h('button', { class: 'todo-check', text: '' }),
        h('div', { class: 'todo-main' }, h('div', { class: 'todo-text', text: t.text }), h('span', { class: 'todo-due due-today', text: '今天到期' })),
      );
      row.querySelector('.todo-check').addEventListener('click', () => { Store.todos.update(t.id, { done: true }); renderOverview(); });
      td.appendChild(row);
    }
    td.appendChild(h('div', { class: 'blind-foot' }, h('button', { class: 'chip', text: '去生活页查看全部 →' })));
    td.querySelector('button').addEventListener('click', () => switchTab('life'));
    wrap.appendChild(td);
  }

  /* ---- 快捷入口 ---- */
  const quick = h('div', { class: 'ov-quick' });
  const q = (icon, label, fn) => {
    const b = h('button', { class: 'ov-quick-btn' }, iconSvg(icon, 22), h('span', { class: 'ov-quick-label', text: label }));
    b.addEventListener('click', fn);
    return b;
  };
  quick.appendChild(q('chat', '找 TA 聊天', () => switchTab('persona')));
  quick.appendChild(q('journal', '生活手帐', () => switchTab('life')));
  quick.appendChild(q('layers', '记忆时间轴', () => switchTab('memory')));
  quick.appendChild(q('download', '数据备份', () => switchTab('features')));
  wrap.appendChild(quick);

  /* ---- 最近聊天 ---- */
  wrap.appendChild(h('div', { class: 'group-title', style: 'margin:6px 16px 4px', text: '最近聊天' }));
  wrap.appendChild(h('div', { id: 'overview-chats' }));
  renderChatList();
}
