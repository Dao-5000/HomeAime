'use strict';
/* ============================================================
   自我觉察：决策追踪 / 习惯养成 / 时间胶囊
   ============================================================ */
const SelfAware = {
  open() {
    this.render();
    $('#selfaware-page').classList.add('open');
  },
  close() { $('#selfaware-page').classList.remove('open'); },

  render() {
    const body = $('#selfaware-body');
    body.innerHTML = '';

    /* ---------- 决策追踪 ---------- */
    body.appendChild(secTitle('🧭 决策追踪'));
    const dTool = h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: '记录重要决定，回头复盘' }),
      h('button', { class: 'chip', text: '＋ 记录决策' }),
    );
    dTool.querySelector('button').addEventListener('click', () => openDecisionSheet());
    body.appendChild(dTool);
    const decisions = Store.decisions.list().slice().sort((a, b) => (a.date < b.date ? 1 : -1));
    if (!decisions.length) {
      body.appendChild(h('div', { class: 'sa-empty', text: '还没有记录。做重要决定时记下来，方便日后复盘。' }));
    }
    for (const d of decisions) {
      const card = h('div', { class: 'sa-card' },
        h('div', { class: 'sa-card-head' }, h('span', { class: 'sa-card-title', text: d.title }), h('span', { class: 'sa-card-date', text: d.date })),
        d.context ? h('div', { class: 'sa-card-line', text: '背景：' + d.context }) : null,
        h('div', { class: 'sa-card-line', text: '决定：' + d.choice }),
        d.result ? h('div', { class: 'sa-card-line', text: '结果：' + d.result }) : null,
        h('button', { class: 'sa-del', text: '✕' }),
      );
      card.querySelector('.sa-del').addEventListener('click', () => {
        if (confirm('删除这条决策记录？')) { Store.decisions.remove(d.id); this.render(); }
      });
      body.appendChild(card);
    }

    /* ---------- 习惯养成 ---------- */
    body.appendChild(secTitle('🌱 习惯养成'));
    const hTool = h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: '每天打卡，积累连续天数' }),
      h('button', { class: 'chip', text: '＋ 新习惯' }),
    );
    hTool.querySelector('button').addEventListener('click', () => openHabitSheet());
    body.appendChild(hTool);
    const habits = Store.habits.list();
    if (!habits.length) {
      body.appendChild(h('div', { class: 'sa-empty', text: '还没有习惯。比如：每天喝水、睡前读书、散步 20 分钟。' }));
    }
    const todayStr = dateStrOf(Date.now());
    for (const hb of habits) {
      const streak = habitStreak(hb.dates || []);
      const doneToday = (hb.dates || []).includes(todayStr);
      const row = h('div', { class: 'habit-row' },
        h('div', { class: 'habit-info' },
          h('div', { class: 'habit-name', text: hb.name }),
          h('div', { class: 'habit-streak', text: '🔥 连续 ' + streak + ' 天' }),
        ),
        h('button', { class: 'habit-check' + (doneToday ? ' on' : ''), text: doneToday ? '✓ 已打卡' : '打卡' }),
        h('button', { class: 'habit-del', text: '✕' }),
      );
      row.querySelector('.habit-check').addEventListener('click', () => {
        const dates = (hb.dates || []).slice();
        if (dates.includes(todayStr)) dates.splice(dates.indexOf(todayStr), 1);
        else dates.push(todayStr);
        Store.habits.update(hb.id, { dates });
        this.render();
      });
      row.querySelector('.habit-del').addEventListener('click', () => {
        if (confirm('删除这个习惯？')) { Store.habits.remove(hb.id); this.render(); }
      });
      body.appendChild(row);
    }

    /* ---------- 时间胶囊 ---------- */
    body.appendChild(secTitle('⏳ 时间胶囊'));
    const cTool = h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: '写给未来的自己' }),
      h('button', { class: 'chip', text: '＋ 埋一颗胶囊' }),
    );
    cTool.querySelector('button').addEventListener('click', () => openCapsuleSheet());
    body.appendChild(cTool);
    const capsules = Store.capsules.list().slice().sort((a, b) => ((a.openDate || '9999').localeCompare(b.openDate || '9999')));
    if (!capsules.length) {
      body.appendChild(h('div', { class: 'sa-empty', text: '给未来的自己写一段话，到日子才能打开。' }));
    }
    for (const cp of capsules) {
      const due = cp.openDate && cp.openDate <= todayStr;
      if (due && !cp.opened) Store.capsules.update(cp.id, { opened: true });
      const opened = cp.opened || due;
      const card = h('div', { class: 'capsule-card' + (opened ? ' opened' : '') },
        h('div', { class: 'capsule-head' },
          h('span', { text: opened ? '📬 已开启' : '🔒 封存中' }),
          h('span', { class: 'sa-card-date', text: '开启日 ' + (cp.openDate || '未定') }),
        ),
        opened
          ? h('div', { class: 'capsule-text', text: cp.text })
          : h('div', { class: 'capsule-sealed', text: '到 ' + cp.openDate + ' 才能打开' }),
        h('button', { class: 'sa-del', text: '✕' }),
      );
      card.querySelector('.sa-del').addEventListener('click', () => {
        if (confirm('删除这颗时间胶囊？')) { Store.capsules.remove(cp.id); this.render(); }
      });
      body.appendChild(card);
    }
  },
};

function habitStreak(dates) {
  const set = new Set(dates || []);
  let streak = 0;
  let d = new Date();
  if (!set.has(dateStrOf(d.getTime()))) d = new Date(d.getTime() - 86400000);
  while (set.has(dateStrOf(d.getTime()))) {
    streak++;
    d = new Date(d.getTime() - 86400000);
  }
  return streak;
}

function openDecisionSheet() {
  const t = h('input', { type: 'text', placeholder: '决策标题' });
  const ctx = h('textarea', { placeholder: '背景（可选）' });
  const ch = h('textarea', { placeholder: '做了什么决定' });
  const rs = h('textarea', { placeholder: '结果 / 复盘（可选）' });
  const form = h('div', {},
    field('标题 *', t), field('背景', ctx), field('决定 *', ch), field('结果/复盘', rs),
    h('button', { class: 'btn btn-primary', text: '保存' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const title = t.value.trim(), choice = ch.value.trim();
    if (!title || !choice) { toast('标题和决定必填'); return; }
    Store.decisions.add({ id: Store.uid(), date: dateStrOf(Date.now()), title, context: ctx.value.trim(), choice, result: rs.value.trim() });
    Sheet.close();
    SelfAware.render();
    toast('已记录决策');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '记录决策' }), form));
}

function openHabitSheet() {
  const t = h('input', { type: 'text', placeholder: '习惯名称，如：每天喝水' });
  const form = h('div', {},
    field('习惯名称 *', t),
    h('button', { class: 'btn btn-primary', text: '创建' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const name = t.value.trim();
    if (!name) { toast('填写习惯名称'); return; }
    Store.habits.add({ id: Store.uid(), name, dates: [], createdAt: Date.now() });
    Sheet.close();
    SelfAware.render();
    toast('已创建习惯');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '新习惯' }), form));
}

function openCapsuleSheet() {
  const txt = h('textarea', { placeholder: '写给未来的自己的一段话…', style: 'min-height:120px' });
  const date = h('input', { type: 'date' });
  const form = h('div', {},
    field('内容 *', txt),
    field('开启日期 *', date),
    h('button', { class: 'btn btn-primary', text: '封存' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const text = txt.value.trim();
    if (!text || !date.value) { toast('内容与开启日期必填'); return; }
    Store.capsules.add({ id: Store.uid(), date: dateStrOf(Date.now()), openDate: date.value, text, opened: false });
    Sheet.close();
    SelfAware.render();
    toast('胶囊已封存 🔒');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '埋一颗时间胶囊' }), form));
}

$('#selfaware-back').addEventListener('click', () => SelfAware.close());
$('#selfaware-done').addEventListener('click', () => SelfAware.close());
