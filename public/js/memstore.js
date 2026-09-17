'use strict';
/* ============================================================
   记忆存储：TA 的记忆文件夹（手动 / 粘贴文本 / 导入文件 / 聊天记住）
   ============================================================ */
const MemStore = {
  contactId: null,

  open(contactId) {
    this.contactId = contactId;
    this.render();
    $('#memstore-page').classList.add('open');
  },

  close() {
    $('#memstore-page').classList.remove('open');
  },

  get c() { return Store.getContact(this.contactId); },

  items() { return (this.c && this.c.memStore) || []; },

  saveItems(list) {
    if (this.c) Store.updateContact(this.c.id, { memStore: list });
  },

  render() {
    const c = this.c;
    if (!c) { this.close(); return; }
    const body = $('#memstore-body');
    body.innerHTML = '';

    body.appendChild(h('div', { class: 'help-box', style: 'margin:2px 2px 12px', text:
      '这里是 TA 的记忆文件夹：粘贴文本、导入文件、或长按聊天消息「让 TA 记住」都会存进来。\n' +
      '开关关闭的记忆不会被 TA 使用，但会保留在这里。' }));

    // ★ 简化：只留 2 个主按钮（添加 / 立刻整理），其他次要入口折叠到"+ 添加"展开菜单
    const addMenuItems = [
      { label: '手动写一条',      action: () => openEntrySheet(null) },
      { label: 'AI 提取（让 AI 总结一段文字）', action: () => openExtractSheet() },
      { label: '粘贴文本',        action: () => openPasteSheet() },
      { label: '导入文件（txt/md）', action: () => fileInput.click() },
      { label: '导入角色卡（json）', action: () => cardInput.click() },
    ];
    let addMenuOpen = false;
    const addMenu = h('div', { class: 'ms-add-menu', hidden: true });
    for (const it of addMenuItems) {
      const b = h('button', { class: 'ms-add-menu-item', text: it.label });
      b.addEventListener('click', () => {
        addMenu.hidden = true;
        addMenuOpen = false;
        it.action();
      });
      addMenu.appendChild(b);
    }
    const addBtn = h('button', { class: 'chip', text: '＋ 添加' });
    addBtn.addEventListener('click', () => {
      addMenuOpen = !addMenuOpen;
      addMenu.hidden = !addMenuOpen;
    });
    const summarizeBtn = h('button', { class: 'chip primary', text: '✨ 立刻整理' });
    summarizeBtn.addEventListener('click', () => forceSummarize(this.c, this));
    const tool = h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: '记忆文件夹（' + this.items().length + ' 条）' }),
      addBtn,
      summarizeBtn,
    );
    body.appendChild(tool);
    body.appendChild(addMenu);

    const cardInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
    cardInput.addEventListener('change', () => {
      const f = cardInput.files[0];
      if (!f) return;
      importCharCardFile(f, this.contactId, () => {
        this.render();
        renderContacts();
      });
      cardInput.value = '';
    });
    body.appendChild(cardInput);

    const fileInput = h('input', { type: 'file', accept: '.txt,.md,text/plain,text/markdown', hidden: true });
    fileInput.addEventListener('change', () => {
      const f = fileInput.files[0];
      if (!f) return;
      if (f.size > 300 * 1024) { toast('文件太大（限 300KB）'); fileInput.value = ''; return; }
      const r = new FileReader();
      r.onload = () => {
        const content = String(r.result || '').trim();
        if (!content) { toast('文件内容为空'); return; }
        const list = this.items();
        list.unshift({ id: Store.uid(), title: f.name.replace(/\.(txt|md)$/i, ''), content, source: 'file', date: dateStrOf(Date.now()), enabled: true });
        this.saveItems(list);
        this.render();
        toast('已导入「' + f.name + '」');
      };
      r.readAsText(f, 'utf-8');
      fileInput.value = '';
    });
    body.appendChild(fileInput);

    const items = this.items().slice().sort((a, b) => ((b.date || '').localeCompare(a.date || '')));
    if (!items.length) {
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('layers'),
        h('div', { text: '记忆文件夹是空的' }),
        h('div', { style: 'margin-top:8px', text: '点「粘贴文本」或「导入文件」，把想让它记住的事存进来' }),
      ));
      return;
    }

    const callName = (this.c && this.c.callsYou || '').trim() || '你';
    const humanize = (t) => String(t || '')
      .replace(/用户核心档案/g, callName + ' 核心档案')
      .replace(/该用户/g, callName)
      .replace(/用户/g, callName);
    for (const e of items) {
      // ★ debug-extract 默认折叠（让记忆库页不那么乱），点开才显示详情
      const isDebug = e.source === 'debug-extract';
      const contentEl = h('div', { class: 'ms-content', text: humanize(e.content) });
      if (isDebug) contentEl.style.display = 'none';
      const card = h('div', { class: 'ms-card' + (e.enabled === false ? ' off' : '') + (isDebug ? ' debug' : '') },
        h('div', { class: 'ms-head' },
          h('label', { class: 'switch mini' },
            h('input', { type: 'checkbox' }),
            h('span', { class: 'track' }),
            h('span', { class: 'thumb' }),
          ),
          h('span', { class: 'ms-badge ' + (e.source || 'manual'), text: srcLabel(e.source) }),
          h('span', { class: 'ms-date', text: e.date || '' }),
          isDebug ? h('span', { class: 'ms-debug-expand', text: '点击查看失败原因', style: 'color:#999;font-size:12px;cursor:pointer;' }) : null,
          h('button', { class: 'ms-op', text: '编辑' }),
          h('button', { class: 'ms-op del', text: '删除' }),
        ),
        e.title ? h('div', { class: 'ms-title', text: humanize(e.title) }) : null,
        contentEl,
      );
      // debug-extract 点 head 也能展开
      if (isDebug) {
        const head = card.querySelector('.ms-head');
        head.style.cursor = 'pointer';
        head.addEventListener('click', (ev) => {
          if (ev.target.closest('.ms-op')) return;     // 编辑/删除不触发
          if (ev.target.closest('.switch')) return;    // 开关不触发
          const show = contentEl.style.display === 'none';
          contentEl.style.display = show ? '' : 'none';
          card.querySelector('.ms-debug-expand').textContent = show ? '收起' : '点击查看失败原因';
        });
      }
      card.querySelector('.switch input').checked = e.enabled !== false;
      card.querySelector('.switch input').addEventListener('change', (ev) => {
        const list = this.items();
        const it = list.find((x) => x.id === e.id);
        if (it) it.enabled = ev.target.checked;
        this.saveItems(list);
        this.render();
        toast(ev.target.checked ? '已启用这条记忆' : '已停用这条记忆（保留但不再使用）');
      });
      const ops = card.querySelectorAll('.ms-op');
      ops[0].addEventListener('click', () => openEntrySheet(e));
      ops[1].addEventListener('click', () => {
        if (confirm('删除这条记忆？')) {
          this.saveItems(this.items().filter((x) => x.id !== e.id));
          this.render();
          toast('已删除');
        }
      });
      body.appendChild(card);
    }
  },
};

function srcLabel(s) {
  const m = { manual: '手动', import: '文本', file: '文件', chat: '聊天', 'auto-extract': 'AI 学习', 'auto-summary': 'AI 总结', auto: '对话回顾', 'chat-snippet': '对话片段', 'debug-extract': '⚠️ 抽取失败' };
  return m[s] || '记忆';
}

/* ============================================================
   用户手动触发总结（跳过 2h 间隔检查）
   - 如果记忆库条目 ≥ 2 条，立即调用 AI 总结
   - 不弹任何中间态反馈；只在结束给一个简短结果
   ============================================================ */
async function forceSummarize(contact, ctx) {
  if (!contact) return;
  const mems = (contact.memStore || []).filter((e) => e.enabled !== false);
  if (mems.length < 2) { return; }
  const conv = Store.getConversation(contact.id);
  if (!conv) { return; }
  // 跳过触发条件（2h 间隔）直接跑：临时把 lastSummarizeAt 设成 0
  Store.updateContact(contact.id, { lastSummarizeAt: 0 });
  const ok = await autoSummarizeMemory(contact, conv.id);
  if (ok && ctx && ctx.render) ctx.render();
}

/** 手动添加 / 编辑记忆条目 */
function openEntrySheet(entry) {
  const isNew = !entry;
  const titleInput = h('input', { type: 'text', placeholder: '标题（可选）', value: entry ? (entry.title || '') : '' });
  const contentInput = h('textarea', { placeholder: '记忆内容…', style: 'min-height:140px' });
  if (entry) contentInput.value = entry.content || '';
  const form = h('div', {},
    field('标题', titleInput),
    field('内容 *', contentInput),
    h('button', { class: 'btn btn-primary', text: isNew ? '存入文件夹' : '保存修改' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!content) { toast('内容不能为空'); return; }
    const list = MemStore.items();
    if (isNew) {
      list.unshift({ id: Store.uid(), title, content, source: 'manual', date: dateStrOf(Date.now()), enabled: true });
      toast('已存入记忆文件夹');
    } else {
      const it = list.find((x) => x.id === entry.id);
      if (it) { it.title = title; it.content = content; }
      toast('已保存');
    }
    MemStore.saveItems(list);
    MemStore.render();
    Sheet.close();
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: isNew ? '添加记忆' : '编辑记忆' }), form));
}

/** 粘贴文本导入（批量） */
function openPasteSheet() {
  const titleInput = h('input', { type: 'text', placeholder: '标题（可选），如：我的喜好清单' });
  const contentInput = h('textarea', { placeholder: '把想让它记住的内容粘贴到这里…', style: 'min-height:200px' });
  const form = h('div', {},
    field('标题', titleInput),
    field('内容 *', contentInput),
    h('button', { class: 'btn btn-primary', text: '导入到记忆库' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!content) { toast('请粘贴内容'); return; }
    const list = MemStore.items();
    list.unshift({ id: Store.uid(), title, content, source: 'import', date: dateStrOf(Date.now()), enabled: true });
    MemStore.saveItems(list);
    MemStore.render();
    Sheet.close();
    toast('已导入到记忆库 📁');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '粘贴文本导入记忆' }), form));
}

$('#memstore-back').addEventListener('click', () => MemStore.close());
$('#memstore-done').addEventListener('click', () => MemStore.close());

/* ============================================================
   AI 文本提取记忆：粘贴/读取任意文本 → AI 提炼成记忆条目
   ============================================================ */

/** 用 AI 从文本中提取记忆条目，返回 [{title, content}] */
async function extractMemoriesFromText(text, contactId) {
  let content = String(text || '').trim();
  if (!content) throw new Error('文本为空');
  // 超长文本智能裁剪：保留开头 + 结尾，中间省略（控制 AI 处理量，加快速度）
  if (content.length > 13000) {
    content = content.slice(0, 10000) + '\n……（中间内容省略）……\n' + content.slice(-3000);
  }
  const contact = contactId ? Store.getContact(contactId) : null;
  const settings = Store.getSettings();
  const brain = contact
    ? brainConfig(contact, settings)
    : { model: 'deepseek-chat', baseUrl: 'https://api.deepseek.com', key: settings.apiKey };
  const sys = '你是记忆整理助手。从下面的文本中提取值得长期记住的记忆条目（事实、喜好、性格特征、重要关系、约定、习惯、忌讳、用户在意的事），每条一句话，最多 15 条。严格按格式输出，每行一条：【记忆】内容。没有值得记的则只输出：无';
  const usr = String(content).slice(0, 20000);
  let out = '';
  await streamAI({
    model: brain.model, baseUrl: brain.baseUrl,
    messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }],
    key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
  }, (d) => { out += d; });
  const items = out.split('\n')
    .map((l) => l.trim())
    .filter((l) => /^【记忆】|^记忆[:：]/.test(l))
    .map((l) => l.replace(/^【记忆】|^记忆[:：]\s*/, '').trim())
    .filter(Boolean);
  return items.map((c) => ({ title: 'AI 提取', content: c }));
}

/** 提取记忆弹层：输入文本 → 提取 → 勾选保存 */
function openExtractSheet(prefill) {
  const ta = h('textarea', { placeholder: '把要提炼的文本粘贴到这里（对话记录、设定、笔记…），AI 会自动提炼成记忆条目', style: 'min-height:180px' });
  if (prefill) ta.value = prefill;
  const resultBox = h('div', { style: 'margin-top:12px' });
  const goBtn = h('button', { class: 'btn btn-primary', text: '开始提取' });
  const cancelBtn = h('button', { class: 'btn btn-plain', text: '取消' });
  cancelBtn.addEventListener('click', () => Sheet.close());

  goBtn.addEventListener('click', async () => {
    goBtn.disabled = true;
    goBtn.textContent = '提取中…';
    resultBox.innerHTML = '';
    ProgressUI.show('AI 正在通读这段文本…', Math.round(ta.value.length / 2) + ' 字（约 10-30 秒，请勿关闭）');
    try {
      const items = await extractMemoriesFromText(ta.value, MemStore.contactId);
      ProgressUI.hide();
      if (!items.length) {
        resultBox.innerHTML = '';
        resultBox.appendChild(h('div', { class: 'help-box', style: 'margin:0', text: '没有提取到值得记住的内容。' }));
        goBtn.disabled = false;
        goBtn.textContent = '重新提取';
        return;
      }
      resultBox.innerHTML = '';
      const checks = [];
      for (const it of items) {
        const l = h('label', { class: 'chk-row' }, h('input', { type: 'checkbox' }), h('span', { text: it.content }));
        l.querySelector('input').checked = true;
        checks.push(l);
        resultBox.appendChild(l);
      }
      const saveBtn = h('button', { class: 'btn btn-primary', text: '保存选中的 ' + items.length + ' 条到记忆文件夹' });
      saveBtn.addEventListener('click', () => {
        const picked = [];
        checks.forEach((l, i) => { if (l.querySelector('input').checked) picked.push(items[i]); });
        if (!picked.length) { toast('没有选中任何条目'); return; }
        const list = MemStore.items().slice();
        for (const p of picked) list.unshift({ id: Store.uid(), title: p.title || 'AI 提取', content: p.content, source: 'import', date: dateStrOf(Date.now()), enabled: true });
        MemStore.saveItems(list);
        MemStore.render();
        Sheet.close();
        toast('已存入 ' + picked.length + ' 条记忆 📁');
      });
      resultBox.appendChild(saveBtn);
      goBtn.disabled = false;
      goBtn.textContent = '重新提取';
    } catch (err) {
      ProgressUI.hide();
      resultBox.innerHTML = '';
      resultBox.appendChild(h('div', { class: 'help-box', style: 'margin:0', text: '提取失败：' + (err.message || err) }));
      goBtn.disabled = false;
      goBtn.textContent = '重新提取';
    }
  });

  const form = h('div', {},
    ta,
    h('div', { style: 'display:flex;gap:8px;margin-top:10px' }, goBtn, cancelBtn),
    resultBox,
  );
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: 'AI 提取记忆' }), form));
}

/* ============================================================
   角色卡 JSON 导入（character_name / relationship / personality / core_memory / dialogue_history）
   ============================================================ */
function parseCharCard(obj) {
  const card = { name: '', relation: { rel: '', level: null }, personality: '', coreMemory: [], dialogueRaw: null, dialogue: '' };
  if (!obj || typeof obj !== 'object') return card;
  card.name = String(obj.character_name || obj.name || '').trim();
  const relStr = String(obj.relationship || obj.relation || '');
  card.relation = mapRelation(relStr);
  card.personality = String(obj.personality || obj.system || '').trim();
  const cm = obj.core_memory || obj.coreMemory || [];
  if (Array.isArray(cm)) card.coreMemory = cm.map((x) => String(x)).filter(Boolean);
  else if (typeof cm === 'string' && cm.trim()) card.coreMemory = [cm.trim()];
  if (obj.dialogue_history != null) {
    card.dialogueRaw = obj.dialogue_history;
    card.dialogue = typeof obj.dialogue_history === 'string'
      ? obj.dialogue_history
      : JSON.stringify(obj.dialogue_history, null, 2);
  }
  return card;
}

/** 把 dialogue_history 转换成真实会话消息（模型能直接读取的历史） */
function dialogueToMessages(dialogue) {
  let arr = dialogue;
  if (typeof dialogue === 'string') {
    try { arr = JSON.parse(dialogue); } catch (_) { return null; }
  }
  if (!Array.isArray(arr)) return null;
  const msgs = [];
  let ts = Date.now() - arr.length * 60000;
  const push = (role, content) => {
    const c = String(content == null ? '' : content).trim();
    if (!c) return;
    ts += 60000;
    msgs.push({ id: Store.uid(), role, content: c, ts, status: role === 'user' ? 'sent' : 'done' });
  };
  for (const item of arr) {
    if (item && typeof item === 'object') {
      // 支持 {user:..., assistant:...} 成对格式
      if (item.user !== undefined && item.assistant !== undefined) {
        push('user', item.user);
        push('assistant', item.assistant);
        continue;
      }
      let role = String(item.role || item.speaker || item.who || '').toLowerCase();
      let content = item.content || item.text || item.message || '';
      if (role === 'user' || role === 'me' || role === 'human' || role === '用户') role = 'user';
      else if (role === 'assistant' || role === 'ai' || role === 'bot' || role === 'model' || role === '角色' || role === 'ta') role = 'assistant';
      else if (/assistant|ai|bot|model|角色/.test(role)) role = 'assistant';
      else role = 'user';
      push(role, content);
    } else if (typeof item === 'string') {
      push('user', item);
    }
  }
  return msgs.slice(-200);
}

/** 把中文关系描述映射到系统关系 + 亲密度 */
function mapRelation(r) {
  if (/亲密|热恋|恋人|老婆|老公|对象|女朋友|男朋友/.test(r)) return { rel: 'lover', level: r.indexOf('亲密') !== -1 || r.indexOf('热恋') !== -1 ? 8 : 6 };
  if (/挚友|死党|闺蜜|好兄弟|好友/.test(r)) return { rel: 'bestfriend', level: 7 };
  if (/家人|亲戚|哥哥|姐姐|妹妹|弟弟|父母|爸妈|儿子|女儿/.test(r)) return { rel: 'family', level: 6 };
  if (/导师|老师|前辈|师父/.test(r)) return { rel: 'mentor', level: 4 };
  if (/同事|上司|老板|合伙人/.test(r)) return { rel: 'coworker', level: 4 };
  if (/网友|网友关系/.test(r)) return { rel: 'netfriend', level: 3 };
  return { rel: '', level: null };
}

/** 读取 JSON 文件 → 角色卡 → 预览应用 */
function importCharCardFile(file, contactId, after) {
  const r = new FileReader();
  r.onload = () => {
    try {
      const obj = JSON.parse(String(r.result));
      const card = parseCharCard(obj);
      if (!card.name && !card.personality && !card.coreMemory.length && !card.dialogue) {
        toast('没识别到角色卡内容（需要 name/relationship/personality/core_memory 等字段）');
        return;
      }
      openCharCardSheet(card, contactId, after);
    } catch (err) {
      toast('JSON 解析失败：' + err.message, 3000);
    }
  };
  r.readAsText(file, 'utf-8');
}

/** 角色卡应用预览（勾选要导入哪些） */
function openCharCardSheet(card, contactId, after) {
  const isNew = !contactId;
  const summary = [
    '📋 识别到角色卡：',
    '名字：' + (card.name || '（无）'),
    '关系：' + (card.relation.rel ? relationLabel(card.relation.rel) + (card.relation.level ? ' ' + card.relation.level + '级' : '') : '（未识别）'),
    '性格设定：' + (card.personality ? card.personality.length + ' 字' : '（无）'),
    '核心记忆：' + card.coreMemory.length + ' 条',
    '对话历史：' + (Array.isArray(card.dialogueRaw) ? card.dialogueRaw.length + ' 条（将导入为真实聊天记录）' : (card.dialogue ? Math.round(card.dialogue.length / 2) + ' 字' : '（无）')),
  ].join('\n');

  const mkChk = (label, def) => {
    const l = h('label', { class: 'chk-row' }, h('input', { type: 'checkbox' }), h('span', { text: label }));
    l.querySelector('input').checked = def;
    return l;
  };
  const chkName = mkChk(card.name ? '把名字设为「' + card.name + '」' : '（角色卡没有名字）', !!card.name);
  const chkRel = mkChk(card.relation.rel ? '设置关系为「' + relationLabel(card.relation.rel) + '」' : '（未识别到关系）', !!card.relation.rel);
  const chkPers = mkChk(isNew ? '写入性格设定' : '用角色卡性格覆盖当前性格设定', !!card.personality);
  const chkCore = mkChk('核心记忆存入记忆文件夹（' + card.coreMemory.length + ' 条）', card.coreMemory.length > 0);
  const chkDial = mkChk(Array.isArray(card.dialogueRaw) ? '对话历史导入为真实聊天记录（' + card.dialogueRaw.length + ' 条）' : '对话历史存档到记忆文件夹', !!card.dialogue);

  const form = h('div', {},
    h('div', { class: 'help-box', style: 'margin:0 0 12px;white-space:pre-wrap', text: summary }),
    chkName, chkRel, chkPers, chkCore, chkDial,
    h('button', { class: 'btn btn-primary', text: isNew ? '创建并导入' : '导入到 TA' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const id = applyCharCard(card, contactId, {
      name: chkName.querySelector('input').checked,
      rel: chkRel.querySelector('input').checked,
      pers: chkPers.querySelector('input').checked,
      core: chkCore.querySelector('input').checked,
      dial: chkDial.querySelector('input').checked,
    });
    Sheet.close();
    if (after) after(id);
    toast(isNew ? '已创建「' + card.name + '」' : '已导入到 TA 的记忆');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: isNew ? '导入角色卡' : '导入角色卡到 ' + (Store.getContact(contactId) ? Store.getContact(contactId).name : '') }), form));
}

/** 应用角色卡：contactId 为空则新建伴侣 */
function applyCharCard(card, contactId, opts) {
  const entries = [];
  if (opts.core) {
    for (const m of card.coreMemory) {
      entries.push({ id: Store.uid(), title: '核心记忆', content: m, source: 'import', date: dateStrOf(Date.now()), enabled: true });
    }
  }

  let id;
  if (!contactId) {
    id = Store.uid();
    Store.addContact({
      id,
      name: opts.name && card.name ? card.name : 'AI 伴侣',
      relation: opts.rel && card.relation.rel ? card.relation.rel : '',
      relationLevel: opts.rel && card.relation.rel ? (card.relation.level || 5) : 5,
      system: opts.pers ? card.personality : '',
      memStore: entries,
      avatar: '', avatarUrl: '', builtin: false,
    });
    renderContacts();
    renderChatList();
  } else {
    id = contactId;
    const patch = {};
    if (opts.name && card.name) patch.name = card.name;
    if (opts.rel && card.relation.rel) { patch.relation = card.relation.rel; patch.relationLevel = card.relation.level || 5; }
    if (opts.pers && card.personality) patch.system = card.personality;
    if (entries.length) {
      const list = ((Store.getContact(contactId) || {}).memStore || []).slice();
      for (const e of entries) list.unshift(e);
      patch.memStore = list;
    }
    Store.updateContact(contactId, patch);
  }

  // 对话历史：优先导入为真实会话消息（模型能直接读到），解析失败才退回记忆文件夹
  if (opts.dial && card.dialogue) {
    const msgs = dialogueToMessages(card.dialogueRaw);
    if (msgs && msgs.length) {
      const conv = Store.ensureConversation(id);
      for (const m of msgs) Store.addMessage(conv.id, m);
      Store.touchConversation(conv.id, msgs[msgs.length - 1].content);
      // 导入完成后刷新正在打开的聊天
      if (Chat.contact && Chat.contact.id === id) Chat.rerender();
    } else {
      const fallback = { id: Store.uid(), title: '对话历史（导入）', content: card.dialogue.slice(0, 8000), source: 'import', date: dateStrOf(Date.now()), enabled: true };
      const c = Store.getContact(id);
      Store.updateContact(id, { memStore: [fallback].concat((c && c.memStore) || []) });
    }
    renderContacts();
    renderChatList();
  }

  return id;
}
