'use strict';
/* ============================================================
   记忆页统一 UI：三个 Tab — 长期记忆 | 每日晚报 | 导入导出
   ============================================================ */

/* ─── 状态 ─── */
let _memTab    = 'memories';   // 'memories' | 'diary' | 'manage'
let _memPage   = 1;
let _memSearch = '';
let _memChar   = 'all';
let _diaryPage = 1;
let _diaryChar = 'all';
const PER_PAGE = 15;

function _memorySessionId() {
  try {
    if (window.Session && typeof window.Session.getSessionId === 'function') {
      return window.Session.getSessionId() || 'default';
    }
  } catch (_) {}
  return localStorage.getItem('ai_companion_session_id')
    || localStorage.getItem('session_id') || 'default';
}

function _memoryCharacterId() {
  if (_memChar !== 'all') return _memChar;
  // ★ 2026-09-14：原来没打开会话时直接回落 default —— 于是记忆页永远显示「通用」桶，
  //   用户看不到角色专属记忆。改成优先当前会话角色，其次第一位伴侣，最后才 default。
  try {
    if (typeof Chat !== 'undefined' && Chat && Chat.contactId) {
      const act = Store.getContact(Chat.contactId);
      if (act) return act.name || act.id || 'default';
    }
    const cs = (Store.listContacts && Store.listContacts()) || [];
    if (cs.length) return cs[0].name || cs[0].id || 'default';
  } catch (_) {}
  return 'default';
}

/* ─── 入口 ─── */
function renderMemory() {
  const wrap = $('#page-memory');
  wrap.innerHTML = '';

  // ── Tab 栏
  wrap.appendChild(_buildTabs());

  // ── Tab 内容区
  const body = h('div', { class: 'mem-body' });
  wrap.appendChild(body);

  if (_memTab === 'memories') _renderMemories(body);
  else if (_memTab === 'diary')    _renderDiary(body);
  else if (_memTab === 'manage')   _renderManage(body);
}

/* ══════════════════════════════════════════════════
   Tab 栏
══════════════════════════════════════════════════ */
function _buildTabs() {
  const bar = h('div', { class: 'mem-tabs' });
  const tabs = [
    { key: 'memories', label: '🧠 长期记忆' },
    { key: 'diary',    label: '📖 每日晚报' },
    { key: 'manage',   label: '⚙️ 导入导出' },
  ];
  for (const t of tabs) {
    const btn = h('button', {
      class: 'mem-tab' + (_memTab === t.key ? ' active' : ''),
      text: t.label
    });
    btn.addEventListener('click', () => {
      _memTab = t.key;
      _memPage = 1;
      _diaryPage = 1;
      renderMemory();
    });
    bar.appendChild(btn);
  }
  return bar;
}

/* ══════════════════════════════════════════════════
   Tab 1：长期记忆（接后端 /api/pc/memory/list）
══════════════════════════════════════════════════ */
async function _renderMemories(wrap) {
  // ★ 2026-09-14 修复：本函数此前是「只追加不清旧」，每次搜索/切chip/翻页/重评
  //   都会再塞一份工具栏+列表+分页 → 页面越点越多、看起来像重复。这里先清掉自己上一轮渲染的内容。
  //   只清自己生成的节点，避免误删外层注入的记忆海容器（.sea-modes/.sea-wrap）。
  ['mem-toolbar','mem-loading','mem-list','mem-pager','empty-state','mem-error']
    .forEach(function (cls) {
      Array.prototype.slice.call(wrap.children).forEach(function (el) {
        if (el.classList && el.classList.contains(cls)) el.remove();
      });
    });
  // ── 工具栏：搜索 + 角色筛选
  const toolbar = h('div', { class: 'mem-toolbar' });

  const searchInput = h('input', {
    class: 'mem-search',
    placeholder: '搜索记忆内容…',
    value: _memSearch
  });
  searchInput.addEventListener('input', (e) => {
    _memSearch = e.target.value;
    _memPage   = 1;
    _renderMemories(wrap);
  });
  toolbar.appendChild(searchInput);

  // 角色筛选 chips
  const chips = h('div', { class: 'chips mem-char-chips' });
  const allChip = h('button', {
    class: 'chip' + (_memChar === 'all' ? ' selected' : ''),
    text: '全部'
  });
  allChip.addEventListener('click', () => {
    _memChar = 'all'; _memPage = 1; _renderMemories(wrap);
  });
  chips.appendChild(allChip);

  const contacts = Store.listContacts();
  for (const c of contacts) {
    // ★ 用角色名做 character_id 过滤（记忆按角色名隔离），UUID 只是 localStorage 键
    const charKey = c.name || c.id;
    const chip = h('button', {
      class: 'chip' + (_memChar === charKey ? ' selected' : ''),
      text: c.name
    });
    chip.addEventListener('click', () => {
      _memChar = charKey; _memPage = 1; _renderMemories(wrap);
    });
    chips.appendChild(chip);
  }
  toolbar.appendChild(chips);

  // ★ 重评重要性按钮：把 importance=5 的历史记忆交给 LLM 重新打分（一次性修复）
  const reEvalBtn = h('button', {
    class: 'chip',
    text: '✨ 重评重要性'
  });
  reEvalBtn.title = '让 AI 重新评估所有 importance=5 的记忆（重要/一般/临时分层）';
  reEvalBtn.addEventListener('click', async () => {
    const sid = _memorySessionId();
    const cid = _memoryCharacterId();
    toast('正在让 AI 重新评估记忆重要性…');
    try {
      const res = await fetch('/api/memory/re_evaluate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, character_id: cid }),
      });
      const j = await res.json();
      if (j.ok) {
        toast(`重评完成：评估 ${j.evaluated} 条，更新 ${j.updated} 条（共 ${j.total} 条）`, 6000);
        _renderMemories(wrap);
      } else {
        toast('重评失败：' + (j.error || '未知错误'));
      }
    } catch (e) {
      toast('重评失败：' + e.message);
    }
  });
  toolbar.appendChild(reEvalBtn);

  wrap.appendChild(toolbar);

  // ── 加载数据
  const loading = h('div', { class: 'mem-loading', text: '加载中…' });
  wrap.appendChild(loading);

  let allMems = [];
  try {
    // 构建请求参数
    const params = new URLSearchParams({
      page:      _memPage,
      per_page:  PER_PAGE,
      search:    _memSearch,
      session_id: _memorySessionId(),
    });
    params.set('character_id', _memoryCharacterId());

    const res  = await fetch(`/api/pc/memory/list?${params}`);
    const data = await res.json();
    allMems    = data.memories || [];
    const total = data.total || 0;
    const totalPages = Math.max(1, Math.ceil(total / PER_PAGE));

    loading.remove();

    if (!allMems.length) {
      wrap.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('layers'),
        h('div', { text: '还没有记忆' }),
        h('div', {
          style: 'margin-top:8px;color:var(--text-3)',
          text: '和 AI 聊天后会自动提炼长期记忆'
        })
      ));
      return;
    }

    // ── 记忆卡片列表
    const list = h('div', { class: 'mem-list' });
    for (const m of allMems) {
      list.appendChild(_buildMemoryCard(m));
    }
    wrap.appendChild(list);

    // ── 分页
    wrap.appendChild(_buildPager(_memPage, totalPages, total, (p) => {
      _memPage = p; _renderMemories(wrap);
    }));

  } catch (e) {
    loading.remove();
    wrap.appendChild(h('div', {
      class: 'mem-error',
      text: '加载失败，请稍后重试'
    }));
  }
}

function _buildMemoryCard(m) {
  const card = h('div', { class: 'mem-card' });

  // 头部：类型标签 + 重要度 + 时间
  const head = h('div', { class: 'mem-card-head' });
  const typeMap = {
    fact: '事实', emotion: '情感', preference: '偏好',
    event: '事件', habit: '习惯', goal: '目标'
  };
  head.appendChild(h('span', {
    class: 'mem-type-badge mem-type-' + (m.memory_type || 'fact'),
    text: typeMap[m.memory_type] || m.memory_type || '记忆'
  }));

  // 重要度星星
  // ★ 2026-09-14：作用域徽章——一眼看出这条是全局记忆还是某角色专属，
  //   避免把记忆「类型」标签（事实/偏好/通用）误当成角色名。
  const _scope = String(m.memory_scope || ((m.character_id && m.character_id !== 'default') ? 'character' : 'global'));
  head.appendChild(h('span', {
    class: 'mem-scope-badge' + (_scope === 'global' ? ' is-global' : ''),
    text: _scope === 'global' ? '全局' : ((m.character_id && m.character_id !== 'default') ? m.character_id : '角色'),
  }));

  const imp = Math.min(10, Math.max(1, Number(m.importance) || 5));
  const stars = imp >= 8 ? '★★★' : imp >= 5 ? '★★' : '★';
  head.appendChild(h('span', { class: 'mem-importance', text: stars }));

  head.appendChild(h('span', {
    class: 'mem-date',
    text: _fmtRelTime(m.create_time)
  }));
  card.appendChild(head);

  // 内容
  card.appendChild(h('div', {
    class: 'mem-card-content',
    text: m.memory_content || ''
  }));

  // 底部：scope 标签 + 编辑/删除操作
  const scopeMap = {
    global: '通用', character: '角色专属', relationship: '关系记忆'
  };
  const foot = h('div', { class: 'mem-card-foot' });
  foot.appendChild(h('span', {
    class: 'mem-scope',
    text: scopeMap[m.memory_scope] || m.memory_scope || ''
  }));

  // ★ 记忆统一：以后端 sqlite 为唯一真相源，记忆页可直接编辑/删除
  const editBtn = h('button', { class: 'mem-op', text: '✏️ 编辑' });
  editBtn.addEventListener('click', () => _editMemory(m));
  foot.appendChild(editBtn);

  const delBtn = h('button', { class: 'mem-op mem-op-danger', text: '🗑 删除' });
  delBtn.addEventListener('click', () => _deleteMemory(m));
  foot.appendChild(delBtn);

  card.appendChild(foot);

  return card;
}

/* ── 编辑后端长期记忆（调用 /api/pc/memory/update） ── */
async function _editMemory(m) {
  const content = prompt('编辑这条记忆：', m.memory_content || '');
  if (content === null) return;
  const trimmed = String(content || '').trim();
  if (!trimmed) { toast('内容不能为空'); return; }
  try {
    const res = await fetch('/api/pc/memory/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: m.id,
        content: trimmed,
        importance: Number(m.importance) || 5,
        memory_type: m.memory_type || 'fact',
        // ★ P1-8：统一走 _memorySessionId()，它按
        //   Session.getSessionId() > ai_companion_session_id > session_id > default 兜底。
        //   原来漏了 ai_companion_session_id，用户只有标准 key 时会掉进 default 桶。
        session_id: (window.Chat && Chat.sessionId) || _memorySessionId(),
        character_id: (window.Chat && Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default'
      })
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || '保存失败');
    toast('已保存');
    renderMemory();
  } catch (e) { toast(e.message || '保存失败'); }
}

/* ── 删除后端长期记忆 ── */
async function _deleteMemory(m) {
  if (!confirm('删除这条记忆？')) return;
  try {
    const res = await fetch('/api/pc/memory/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: 'db:' + m.id,
        // ★ P1-8：统一走 _memorySessionId()，它按
        //   Session.getSessionId() > ai_companion_session_id > session_id > default 兜底。
        //   原来漏了 ai_companion_session_id，用户只有标准 key 时会掉进 default 桶。
        session_id: (window.Chat && Chat.sessionId) || _memorySessionId(),
        character_id: (window.Chat && Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default'
      })
    });
    if (!res.ok) throw new Error('删除失败');
    toast('已删除');
    renderMemory();
  } catch (e) { toast(e.message || '删除失败'); }
}

/* ══════════════════════════════════════════════════
   Tab 2：每日晚报（原 memory.js 逻辑，整合进来）
══════════════════════════════════════════════════ */
async function _renderDiary(wrap) {
  const contacts = Store.listContacts();

  // 角色筛选 chips
  const chips = h('div', { class: 'chips', style: 'padding:10px 12px 0' });
  const addChip = (k, label) => {
    const b = h('button', {
      class: 'chip' + (_diaryChar === k ? ' selected' : ''),
      text: label
    });
    b.addEventListener('click', () => {
      _diaryChar = k; _diaryPage = 1; _renderDiary(wrap);
    });
    chips.appendChild(b);
  };
  addChip('all', '全部');
  for (const c of contacts) addChip(c.id, c.name);
  wrap.appendChild(chips);

  // 收集并按天分组
  const map = {};
  const days = [];
  for (const c of contacts) {
    if (_diaryChar !== 'all' && _diaryChar !== c.id) continue;
    for (const l of (c.logs || [])) {
      if (!map[l.date]) { map[l.date] = []; days.push(l.date); }
      map[l.date].push({ contact: c, text: l.text });
    }
  }
  // 合并后端持久化晚报；相同角色+日期+内容自动去重。
  try {
    const res = await fetch('/api/memory/daily_reports?session_id=' + encodeURIComponent(_memorySessionId()));
    const data = await res.json();
    for (const r of (data.reports || [])) {
      const contact = contacts.find((c) => (c.name || c.id) === r.character_id)
        || { id: r.character_id, name: r.character_id };
      if (_diaryChar !== 'all' && _diaryChar !== contact.id && _diaryChar !== contact.name) continue;
      if (!map[r.report_date]) { map[r.report_date] = []; days.push(r.report_date); }
      if (!map[r.report_date].some((x) => x.contact.name === contact.name && x.text === r.content)) {
        map[r.report_date].push({ contact, text: r.content });
      }
    }
  } catch (_) {}
  days.sort((a, b) => (a < b ? 1 : -1));

  if (!days.length) {
    wrap.appendChild(h('div', { class: 'empty-state' },
      emptyIcon('layers'),
      h('div', { text: '还没有每日记忆' }),
      h('div', {
        style: 'margin-top:8px;color:var(--text-3)',
        text: '每晚 23:00 自动生成情绪晚报，也可在伴侣资料页手动生成'
      })
    ));
    return;
  }

  // 分页
  const totalPages = Math.max(1, Math.ceil(days.length / PER_PAGE));
  if (_diaryPage > totalPages) _diaryPage = totalPages;
  const pageDays = days.slice(
    (_diaryPage - 1) * PER_PAGE,
    _diaryPage * PER_PAGE
  );

  // 时间轴
  const tl = h('div', { class: 'mem-timeline' });
  for (const d of pageDays) {
    const dayNode = h('div', { class: 'mem-day' });
    dayNode.appendChild(h('div', { class: 'mem-day-head' },
      h('span', { class: 'mem-dot' }),
      h('span', { class: 'mem-day-date', text: _fmtDay(d) }),
      h('span', { class: 'mem-day-count', text: map[d].length + ' 篇' }),
    ));
    for (const e of map[d]) {
      const cwrap = h('div', { class: 'mem-day-cards' });
      const cname = h('div', {
        class: 'mem-contact',
        text: '💛 ' + e.contact.name
      });
      cname.addEventListener('click', () => Logs.open(e.contact.id));
      cwrap.appendChild(cname);
      cwrap.appendChild(renderLogCard(e.text));
      dayNode.appendChild(cwrap);
    }
    tl.appendChild(dayNode);
  }
  wrap.appendChild(tl);

  wrap.appendChild(_buildPager(_diaryPage, totalPages, days.length, (p) => {
    _diaryPage = p; _renderDiary(wrap);
  }));
}

/* ══════════════════════════════════════════════════
   Tab 3：导入导出管理
══════════════════════════════════════════════════ */
function _renderManage(wrap) {
  const section = h('div', { class: 'mem-manage' });

  // ── 导出区
  const exportBox = h('div', { class: 'mem-manage-box' });
  exportBox.appendChild(h('div', { class: 'mem-manage-title', text: '📤 导出记忆' }));
  exportBox.appendChild(h('div', {
    class: 'mem-manage-desc',
    text: '将所有长期记忆导出为文件，可用于备份或迁移。'
  }));

  const exportBtns = h('div', { class: 'mem-manage-btns' });

  const btnTxt = h('button', { class: 'btn btn-secondary', text: '导出 TXT' });
  btnTxt.addEventListener('click', () => {
    const q = new URLSearchParams({
      format: 'txt', session_id: _memorySessionId(), character_id: _memoryCharacterId()
    });
    window.location.href = '/api/pc/memory/export?' + q;
  });
  exportBtns.appendChild(btnTxt);

  const btnJson = h('button', { class: 'btn btn-secondary', text: '导出 JSON' });
  btnJson.addEventListener('click', () => {
    const q = new URLSearchParams({
      format: 'json', session_id: _memorySessionId(), character_id: _memoryCharacterId()
    });
    window.location.href = '/api/pc/memory/export?' + q;
  });
  exportBtns.appendChild(btnJson);
  exportBox.appendChild(exportBtns);
  section.appendChild(exportBox);

  // ── 导入区
  const importBox = h('div', { class: 'mem-manage-box' });
  importBox.appendChild(h('div', { class: 'mem-manage-title', text: '📥 导入记忆' }));
  importBox.appendChild(h('div', {
    class: 'mem-manage-desc',
    text: '从备份文件恢复记忆，支持 merge（保留现有）或 overwrite（全量替换）。'
  }));

  const modeRow = h('div', { class: 'mem-manage-row' });
  modeRow.appendChild(h('span', { text: '导入模式：' }));
  const modeSelect = h('select', { class: 'mem-select' });
  [['merge', '合并（保留现有）'], ['overwrite', '覆盖（全量替换）']].forEach(([v, t]) => {
    const opt = h('option', { value: v, text: t });
    modeSelect.appendChild(opt);
  });
  modeRow.appendChild(modeSelect);
  importBox.appendChild(modeRow);

  const fileInput = h('input', { type: 'file', class: 'mem-file-input', accept: '.txt,.json' });
  importBox.appendChild(fileInput);

  const btnImport = h('button', { class: 'btn btn-primary', text: '开始导入' });
  btnImport.addEventListener('click', async () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) { toast('请先选择文件'); return; }
    btnImport.disabled = true;
    btnImport.textContent = '导入中…';
    try {
      const buf    = await file.arrayBuffer();
      const b64    = btoa(String.fromCharCode(...new Uint8Array(buf)));
      const res    = await fetch('/api/pc/memory/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          content_b64: b64,
          mode: modeSelect.value,
          session_id: _memorySessionId(),
          character_id: _memoryCharacterId()
        })
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error.message);
      toast(`导入成功，共 ${data.total || 0} 条记忆`);
      _memTab  = 'memories';
      _memPage = 1;
      renderMemory();
    } catch (e) {
      toast('导入失败：' + e.message);
    } finally {
      btnImport.disabled  = false;
      btnImport.textContent = '开始导入';
    }
  });
  importBox.appendChild(btnImport);
  section.appendChild(importBox);

  // ── 迁移本地历史记忆区（记忆统一：前端 localStorage → 后端 sqlite）
  const migrateBox = h('div', { class: 'mem-manage-box' });
  migrateBox.appendChild(h('div', { class: 'mem-manage-title', text: '🔄 迁移本地历史记忆' }));
  migrateBox.appendChild(h('div', {
    class: 'mem-manage-desc',
    text: '把之前存在浏览器本地的记忆（记忆文件夹 / 核心人格 / 用户档案）迁到后端数据库，与自动提炼的记忆统一管理、可编辑删除。'
  }));
  const btnMigrate = h('button', { class: 'btn btn-primary', text: '开始迁移本地记忆' });
  btnMigrate.addEventListener('click', async () => {
    btnMigrate.disabled = true;
    btnMigrate.textContent = '迁移中…';
    try {
      const items = [];
      const contacts = Store.listContacts();
      for (const c of contacts) {
        const cname = c.name || c.id;
        for (const e of (c.memStore || [])) {
          if (!e || e.enabled === false || !(e.content || '').trim()) continue;
          items.push({ content: (e.title ? e.title + '：' : '') + e.content, character_name: cname });
        }
        if (c.memory && String(c.memory).trim()) {
          items.push({ content: '关于' + cname + '的核心记忆：' + String(c.memory).trim(), character_name: cname });
        }
      }
      const profile = String((Store.getSettings().userProfile) || '').trim();
      if (profile) {
        items.push({ content: '用户核心档案：' + profile, character_name: 'default' });
      }
      if (!items.length) { toast('没有可迁移的本地记忆'); btnMigrate.disabled = false; btnMigrate.textContent = '开始迁移本地记忆'; return; }

      const res = await fetch('/api/pc/memory/migrate_local', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items })
      });
      const data = await res.json();
      if (data.error) throw new Error(data.error.message);
      toast('迁移完成：新增/更新 ' + (data.saved || 0) + ' 条（共发送 ' + items.length + ' 条）');
      _memTab = 'memories'; _memPage = 1; renderMemory();
    } catch (e) {
      toast('迁移失败：' + (e.message || e));
    } finally {
      btnMigrate.disabled = false;
      btnMigrate.textContent = '开始迁移本地记忆';
    }
  });
  migrateBox.appendChild(btnMigrate);
  section.appendChild(migrateBox);

  wrap.appendChild(section);
}

/* ══════════════════════════════════════════════════
   公共组件
══════════════════════════════════════════════════ */
function _buildPager(page, totalPages, total, onChange) {
  const pager = h('div', { class: 'mem-pager' });
  const prev = h('button', {
    class: 'chip',
    text: '‹ 上一页',
    disabled: page <= 1
  });
  prev.addEventListener('click', () => { if (page > 1) onChange(page - 1); });

  const info = h('span', {
    class: 'mem-pager-info',
    text: `共 ${total} 条・第 ${page}/${totalPages} 页`
  });
  const next = h('button', {
    class: 'chip',
    text: '下一页 ›',
    disabled: page >= totalPages
  });
  next.addEventListener('click', () => { if (page < totalPages) onChange(page + 1); });

  pager.appendChild(prev);
  pager.appendChild(info);
  pager.appendChild(next);
  return pager;
}

function _fmtRelTime(ts) {
  if (!ts) return '';
  const d   = new Date(ts.replace ? ts.replace(' ', 'T') : ts);
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 60000)     return '刚刚';
  if (diff < 3600000)   return Math.floor(diff / 60000) + ' 分钟前';
  if (diff < 86400000)  return Math.floor(diff / 3600000) + ' 小时前';
  if (diff < 604800000) return Math.floor(diff / 86400000) + ' 天前';
  return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

function _fmtDay(d) {
  const [, m, dd] = d.split('-');
  const w = '日一二三四五六'[new Date(d + 'T00:00:00').getDay()];
  return Number(m) + '月' + Number(dd) + '日 · 周' + w;
}
