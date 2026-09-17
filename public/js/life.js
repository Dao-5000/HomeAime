'use strict';
/* ============================================================
   生活页：待办管理 + 生活手帐
   ============================================================ */
let lifeSeg = 'todo';
let hbCat = '全部';

function renderLife() {
  const wrap = $('#page-life');
  wrap.innerHTML = '';
  wrap.appendChild(h('div', { class: 'seg' },
    h('button', { class: 'seg-btn' + (lifeSeg === 'todo' ? ' active' : ''), text: '待办' }),
    h('button', { class: 'seg-btn' + (lifeSeg === 'hb' ? ' active' : ''), text: '手帐' }),
  ));
  const btns = wrap.querySelectorAll('.seg-btn');
  btns[0].addEventListener('click', () => { lifeSeg = 'todo'; renderLife(); });
  btns[1].addEventListener('click', () => { lifeSeg = 'hb'; renderLife(); });

  wrap.appendChild(h('div', { class: 'help-box', style: 'margin:10px 12px', text:
    lifeSeg === 'todo'
      ? '待办由 AI 记住：到期会在早报里提醒你，勾选即完成。'
      : '手帐由 AI 自动维护：聊天中的碎片想法会被自动捕捉、分类归档，也可手动记一笔。' }));

  if (lifeSeg === 'todo') renderTodosInto(wrap);
  else renderHandbookInto(wrap);
}

/* ================= 待办 ================= */
function renderTodosInto(wrap) {
  const todos = Store.todos.list().slice().sort((a, b) => (a.done - b.done) || ((a.due || '9999').localeCompare(b.due || '9999')));
  const today = dateStrOf(Date.now());

  const tool = h('div', { class: 'sec-toolbar' },
    h('span', { class: 'sec-toolbar-title', text: '待办事项（' + todos.filter((t) => !t.done).length + ' 未完成）' }),
    h('button', { class: 'chip', text: '＋ 添加' }),
  );
  tool.querySelector('button').addEventListener('click', () => openTodoSheet());
  wrap.appendChild(tool);

  if (!todos.length) {
    wrap.appendChild(h('div', { class: 'empty-state' },
      emptyIcon('list'),
      h('div', { text: '还没有待办' }),
      h('div', { style: 'margin-top:8px', text: '点「＋ 添加」记下要做的事，可设到期日' }),
    ));
    return;
  }

  for (const t of todos) {
    let dueText = '', dueCls = '';
    if (t.due) {
      const diff = Math.round((new Date(t.due + 'T00:00:00') - new Date(today + 'T00:00:00')) / 86400000);
      if (diff === 0) { dueText = '今天到期'; dueCls = 'due-today'; }
      else if (diff < 0) { dueText = '逾期 ' + (-diff) + ' 天'; dueCls = 'due-over'; }
      else dueText = t.due;
    }
    const row = h('div', { class: 'todo-row' + (t.done ? ' done' : '') },
      h('button', { class: 'todo-check' + (t.done ? ' on' : ''), text: t.done ? '✓' : '' }),
      h('div', { class: 'todo-main' },
        h('div', { class: 'todo-text', text: t.text }),
        dueText ? h('span', { class: 'todo-due ' + dueCls, text: dueText }) : null,
      ),
      h('button', { class: 'todo-del', text: '✕' }),
    );
    row.querySelector('.todo-check').addEventListener('click', () => {
      Store.todos.update(t.id, { done: !t.done });
      renderLife();
    });
    row.querySelector('.todo-del').addEventListener('click', () => {
      if (confirm('删除这条待办？')) { Store.todos.remove(t.id); renderLife(); }
    });
    wrap.appendChild(row);
  }
}

function openTodoSheet() {
  const textInput = h('input', { type: 'text', placeholder: '要做什么？' });
  const dueInput = h('input', { type: 'date' });
  const form = h('div', {},
    field('内容 *', textInput),
    field('到期日（可选）', dueInput),
    h('button', { class: 'btn btn-primary', text: '添加' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const text = textInput.value.trim();
    if (!text) { toast('请填写内容'); return; }
    Store.todos.add({ id: Store.uid(), text, due: dueInput.value || '', done: false, createdAt: Date.now() });
    Sheet.close();
    renderLife();
    toast('已添加待办');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '添加待办' }), form));
}

/* ================= 手帐 ================= */
function renderHandbookInto(wrap) {
  const tool = h('div', { class: 'sec-toolbar' },
    h('span', { class: 'sec-toolbar-title', text: '生活手帐（碎片想法自动归档）' }),
    h('button', { class: 'chip', text: '🧹 整理今日手帐' }),
    h('button', { class: 'chip', text: '＋ 记一笔' }),
  );
  const btns = tool.querySelectorAll('button');
  btns[0].addEventListener('click', async () => {
    btns[0].textContent = '整理中…';
    btns[0].disabled = true;
    const contacts = Store.listContacts().filter((c) => {
      const conv = Store.getConversation(c.id);
      const msgs = conv ? Store.getMessages(conv.id).filter((m) => m.ts && dateStrOf(m.ts) === dateStrOf(Date.now()) && m.role === 'user') : [];
      return msgs.length > 0;
    });
    let n = 0;
    for (const c of contacts) {
      try { n += await generateHandbookFor(c, dateStrOf(Date.now())) || 0; } catch (_) { /* 忽略 */ }
    }
    btns[0].textContent = '🧹 整理今日手帐';
    btns[0].disabled = false;
    renderLife();
    toast(n ? '已整理 ' + n + ' 条手帐' : '今天还没有值得整理的想法');
  });
  btns[1].addEventListener('click', () => openHandbookSheet());
  wrap.appendChild(tool);

  /* 分类筛选 */
  const cats = ['全部', '灵感', '计划', '目标', '心情', '其他'];
  const chips = h('div', { class: 'chips', style: 'padding:0 12px 8px' });
  for (const cat of cats) {
    const b = h('button', { class: 'chip' + (hbCat === cat ? ' selected' : ''), text: cat });
    b.addEventListener('click', () => { hbCat = cat; renderLife(); });
    chips.appendChild(b);
  }
  wrap.appendChild(chips);

  const items = Store.handbook.list()
    .filter((x) => hbCat === '全部' || x.category === hbCat)
    .sort((a, b) => (a.date < b.date ? 1 : -1));

  if (!items.length) {
    wrap.appendChild(h('div', { class: 'empty-state' },
      emptyIcon('journal'),
      h('div', { text: '手帐还是空的' }),
      h('div', { style: 'margin-top:8px', text: '聊天中的灵感会自动归档，也可手动记一笔' }),
    ));
    return;
  }

  let curDate = '';
  for (const it of items) {
    if (it.date !== curDate) {
      curDate = it.date;
      wrap.appendChild(h('div', { class: 'group-title', style: 'margin:10px 16px 4px', text: curDate }));
    }
    const card = h('div', { class: 'hb-card' },
      h('div', { class: 'hb-head' },
        h('span', { class: 'hb-cat', text: it.category }),
        h('span', { class: 'hb-src', text: it.contactName || '' }),
      ),
      it.title ? h('div', { class: 'hb-title', text: it.title }) : null,
      it.content ? h('div', { class: 'hb-content', text: it.content }) : null,
      h('button', { class: 'hb-del', text: '✕' }),
    );
    card.querySelector('.hb-del').addEventListener('click', () => {
      if (confirm('删除这条手帐？')) { Store.handbook.remove(it.id); renderLife(); }
    });
    wrap.appendChild(card);
  }
}

function openHandbookSheet() {
  const catSelect = h('select', {}, h('option', { value: '灵感', text: '灵感' }), h('option', { value: '计划', text: '计划' }),
    h('option', { value: '目标', text: '目标' }), h('option', { value: '心情', text: '心情' }), h('option', { value: '其他', text: '其他' }));
  const titleInput = h('input', { type: 'text', placeholder: '标题' });
  const contentInput = h('textarea', { placeholder: '内容…' });
  const form = h('div', {},
    field('分类', catSelect),
    field('标题', titleInput),
    field('内容', contentInput),
    h('button', { class: 'btn btn-primary', text: '保存' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!title && !content) { toast('写点什么吧'); return; }
    Store.handbook.add({ id: Store.uid(), date: dateStrOf(Date.now()), category: catSelect.value, title, content, contactName: '', createdAt: Date.now() });
    Sheet.close();
    renderLife();
    toast('已记一笔');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '记一笔手帐' }), form));
}
