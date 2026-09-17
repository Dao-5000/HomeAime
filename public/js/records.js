'use strict';
/* ============================================================
   记录页：原始对话记录（按日期筛选）+ 笔记（读书笔记/报告）
   ============================================================ */
let recSeg = 'chat';
let recContact = '';
let recDate = '';
let noteFilter = '';
let letterFilter = '';
let letterContact = '';
let letterKeptOnly = false;

function updateRecordsTabDot() {
  const dot = document.getElementById('tab-records-dot');
  if (dot) dot.style.display = Store.letters.unread() > 0 ? 'block' : 'none';
}

function openLetterKeeps(characterId) {
  recSeg = 'letter';
  letterContact = characterId || '';
  letterKeptOnly = true;
  if (typeof showPage === 'function') showPage('records');
  renderRecords();
}
window.openLetterKeeps = openLetterKeeps;

function renderRecords() {
  // ★ 兜底：渲染一旦抛异常，原先会留下一个空白页面 —— 表现为"这一页什么都点不了"，
  //   而且看不到任何原因。这里捕获后给出提示与重试入口，便于定位。
  try {
    _renderRecordsBody();
  } catch (e) {
    console.error('[records] 渲染失败:', e);
    try {
      const w = $('#page-records');
      if (w) {
        w.innerHTML = '';
        w.appendChild(h('div', { class: 'empty-state' },
          h('div', { style: 'font-size:40px;margin-bottom:8px', text: '⚠️' }),
          h('div', { text: '记录页渲染出错' }),
          h('div', {
            style: 'margin-top:8px;font-size:12px;opacity:.7;word-break:break-all',
            text: String((e && e.message) || e),
          }),
          h('button', {
            class: 'chip', style: 'margin-top:12px', text: '重试',
            onclick: () => renderRecords(),
          }),
        ));
      }
    } catch (_) {}
  }
}

function _renderRecordsBody() {
  updateRecordsTabDot();
  // ★ 修复：拉取「身份坦诚小作文」（离线时补收），存成信件
  try {
    _fetchPendingIdentityLetter();
  } catch (_) {}
  const wrap = $('#page-records');
  wrap.innerHTML = '';
  wrap.appendChild(h('div', { class: 'seg' },
    h('button', { class: 'seg-btn' + (recSeg === 'chat' ? ' active' : ''), text: '对话记录' }),
    h('button', { class: 'seg-btn' + (recSeg === 'note' ? ' active' : ''), text: '笔记' }),
    h('button', { class: 'seg-btn' + (recSeg === 'letter' ? ' active' : ''), text: '信件' },
      Store.letters.unread() > 0 ? h('span', { class: 'red-dot', style: 'margin-left:4px' }) : null),
  ));
  const btns = wrap.querySelectorAll('.seg-btn');
  btns[0].addEventListener('click', () => { recSeg = 'chat'; renderRecords(); });
  btns[1].addEventListener('click', () => { recSeg = 'note'; renderRecords(); });
  btns[2].addEventListener('click', () => { recSeg = 'letter'; renderRecords(); });

  wrap.appendChild(h('div', { class: 'help-box', style: 'margin:10px 12px', text:
    '这些由 AI 帮你留档：原始对话随时可回看，笔记（读书/报告）自动归档，支持按日期筛选。' }));

  if (recSeg === 'chat') renderChatRecords(wrap);
  else if (recSeg === 'note') renderNotes(wrap);
  else renderLetters(wrap);
}

/* ================= 对话记录 ================= */
function renderChatRecords(wrap) {
  const contacts = Store.listContacts();
  /* 伴侣筛选 */
  const chips = h('div', { class: 'chips', style: 'padding:10px 12px 0' });
  const addChip = (k, label) => {
    const b = h('button', { class: 'chip' + (recContact === k ? ' selected' : ''), text: label });
    b.addEventListener('click', () => { recContact = k; renderRecords(); });
    chips.appendChild(b);
  };
  for (const c of contacts) addChip(c.id, c.name);
  wrap.appendChild(chips);

  /* 日期筛选 */
  const dateRow = h('div', { style: 'display:flex;gap:8px;align-items:center;padding:8px 12px' },
    h('span', { class: 'rec-filter-label', text: '日期：' }),
    h('input', { type: 'date', value: recDate }),
    recDate ? h('button', { class: 'chip', text: '清除' }) : null,
  );
  dateRow.querySelector('input').addEventListener('change', (e) => { recDate = e.target.value; renderRecords(); });
  if (recDate) dateRow.querySelector('button').addEventListener('click', () => { recDate = ''; renderRecords(); });
  wrap.appendChild(dateRow);

  const target = recContact ? Store.getContact(recContact) : null;
  if (!target) {
    wrap.appendChild(h('div', { class: 'empty-state' }, emptyIcon('chat'), h('div', { text: '选择一个伴侣查看对话记录' })));
    return;
  }
  const conv = Store.getConversation(target.id);
  let msgs = conv ? Store.getMessages(conv.id) : [];
  if (recDate) msgs = msgs.filter((m) => m.ts && dateStrOf(m.ts) === recDate);
  if (!msgs.length) {
    wrap.appendChild(h('div', { class: 'empty-state' }, emptyIcon('list'), h('div', { text: recDate ? recDate + ' 没有对话' : '还没有对话记录' })));
    return;
  }

  const box = h('div', { class: 'rec-chat' });
  for (const m of msgs) {
    const line = h('div', { class: 'rec-line ' + (m.role === 'user' ? 'me' : 'other') },
      h('span', { class: 'rec-time', text: fmtClock(m.ts) }),
      h('span', { class: 'rec-bubble' }),
    );
    const bub = line.querySelector('.rec-bubble');
    if (m.type === 'sticker') bub.textContent = m.content;
    else if (m.image) bub.textContent = '[图片]' + (m.content && m.content !== '[图片]' ? ' ' + m.content : '');
    else if (m.status === 'recalled') bub.textContent = '（撤回的消息）';
    else bub.textContent = m.content;
    box.appendChild(line);
  }
  wrap.appendChild(box);
}

/* ================= 笔记 ================= */
function renderNotes(wrap) {
  const tool = h('div', { class: 'sec-toolbar' },
    h('span', { class: 'sec-toolbar-title', text: '读书笔记 / 报告' }),
    h('button', { class: 'chip', text: '＋ 新建笔记' }),
  );
  tool.querySelector('button').addEventListener('click', () => openNoteSheet(null));
  wrap.appendChild(tool);

  /* 日期筛选 */
  const dateRow = h('div', { style: 'display:flex;gap:8px;align-items:center;padding:8px 12px' },
    h('span', { class: 'rec-filter-label', text: '日期：' }),
    h('input', { type: 'date', value: noteFilter }),
    noteFilter ? h('button', { class: 'chip', text: '清除' }) : null,
  );
  dateRow.querySelector('input').addEventListener('change', (e) => { noteFilter = e.target.value; renderRecords(); });
  if (noteFilter) dateRow.querySelector('button').addEventListener('click', () => { noteFilter = ''; renderRecords(); });
  wrap.appendChild(dateRow);

  let notes = Store.notes.list().slice().sort((a, b) => (a.date < b.date ? 1 : -1));
  if (noteFilter) notes = notes.filter((n) => n.date === noteFilter);

  if (!notes.length) {
    wrap.appendChild(h('div', { class: 'empty-state' },
      emptyIcon('journal'),
      h('div', { text: '还没有笔记' }),
      h('div', { style: 'margin-top:8px', text: '记下读书笔记、工作报告或任何想留档的内容' }),
    ));
    return;
  }

  for (const n of notes) {
    const card = h('div', { class: 'note-card' },
      h('div', { class: 'note-head' },
        h('span', { class: 'note-cat', text: n.category }),
        h('span', { class: 'note-date', text: n.date }),
      ),
      h('div', { class: 'note-title', text: n.title }),
      h('div', { class: 'note-content', text: n.content }),
      h('div', { class: 'note-ops' },
        h('button', { class: 'chip', text: '编辑' }),
        h('button', { class: 'chip', text: '删除' }),
      ),
    );
    const ops = card.querySelectorAll('.note-ops .chip');
    ops[0].addEventListener('click', () => openNoteSheet(n));
    ops[1].addEventListener('click', () => {
      if (confirm('删除这篇笔记？')) { Store.notes.remove(n.id); renderRecords(); }
    });
    wrap.appendChild(card);
  }
}

function openNoteSheet(note) {
  const isNew = !note;
  const catSelect = h('select', {}, h('option', { value: '读书笔记', text: '读书笔记' }), h('option', { value: '报告', text: '报告' }), h('option', { value: '其他', text: '其他' }));
  if (note) catSelect.value = note.category;
  const titleInput = h('input', { type: 'text', placeholder: '标题', value: note ? note.title : '' });
  const contentInput = h('textarea', { placeholder: '内容…', style: 'min-height:140px' });
  if (note) contentInput.value = note.content;
  const form = h('div', {},
    field('分类', catSelect),
    field('标题', titleInput),
    field('内容', contentInput),
    h('button', { class: 'btn btn-primary', text: isNew ? '保存' : '保存修改' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].addEventListener('click', () => {
    const title = titleInput.value.trim();
    const content = contentInput.value.trim();
    if (!title && !content) { toast('写点什么吧'); return; }
    if (isNew) Store.notes.add({ id: Store.uid(), date: dateStrOf(Date.now()), category: catSelect.value, title, content });
    else Store.notes.update(note.id, { category: catSelect.value, title, content });
    Sheet.close();
    renderRecords();
    toast(isNew ? '已保存笔记' : '已更新');
  });
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: isNew ? '新建笔记' : '编辑笔记' }), form));
}

/* ================= 信件 ================= */
const LETTER_TYPES = {
  morning: { label: '早安', icon: '🌅', color: '#FF9800' },
  evening: { label: '晚安', icon: '🌙', color: '#5C6BC0' },
  letter:  { label: '信件', icon: '✉️', color: '#E91E63' },
  gift:    { label: '惊喜', icon: '🎁', color: '#9C27B0' },
};

function renderLetters(wrap) {
  try { syncLetterKeepsFromBackend(); } catch (_) {}
  // ★ 进入信件页即视为已读：去掉未读红竖线 + 红点，避免永久"未读"的观感
  try {
    let _marked = false;
    for (const _l of Store.letters.list()) {
      if (!_l.read) { Store.letters.update(_l.id, { read: true }); _marked = true; }
    }
    if (_marked) updateRecordsTabDot();
  } catch (_) {}
  const tool = h('div', { class: 'sec-toolbar' },
    h('span', { class: 'sec-toolbar-title', text: '信件与惊喜' }),
    h('button', {
      class: 'chip letter-keep-filter' + (letterKeptOnly ? ' selected' : ''),
      text: letterKeptOnly ? '★ 纪念收藏' : '☆ 纪念收藏',
      onclick: () => { letterKeptOnly = !letterKeptOnly; renderRecords(); },
    }),
  );
  wrap.appendChild(tool);

  /* 类型筛选 */
  const chips = h('div', { class: 'chips', style: 'padding:10px 12px 0' });
  const addChip = (k, label) => {
    const b = h('button', { class: 'chip' + (letterFilter === k ? ' selected' : ''), text: label });
    b.addEventListener('click', () => { letterFilter = k; renderRecords(); });
    chips.appendChild(b);
  };
  addChip('', '全部');
  for (const [k, v] of Object.entries(LETTER_TYPES)) addChip(k, v.icon + ' ' + v.label);
  wrap.appendChild(chips);

  const contactChips = h('div', { class: 'chips', style: 'padding:8px 12px 0' });
  const addContactChip = (k, label) => {
    const b = h('button', { class: 'chip' + (letterContact === k ? ' selected' : ''), text: label });
    b.addEventListener('click', () => { letterContact = k; renderRecords(); });
    contactChips.appendChild(b);
  };
  addContactChip('', '所有角色');
  for (const c of Store.listContacts()) addContactChip(c.name || c.id, c.name || c.id);
  wrap.appendChild(contactChips);

  const nowMs = Date.now();
  let letters = Store.letters.list().filter(l => !l.availableAt || new Date(l.availableAt).getTime() <= nowMs).slice().sort((a, b) => {
    const ak = `${a.date || ''} ${a.time || ''}`;
    const bk = `${b.date || ''} ${b.time || ''}`;
    return bk.localeCompare(ak);
  });
  if (letterFilter) letters = letters.filter((l) => l.type === letterFilter);
  if (letterContact) letters = letters.filter((l) => !l.characterId || l.characterId === letterContact);
  if (letterKeptOnly) letters = letters.filter((l) => !!l.kept || !!l.permanent);

  if (!letters.length) {
    wrap.appendChild(h('div', { class: 'empty-state' },
      h('div', { style: 'font-size:40px;margin-bottom:8px', text: '✉️' }),
      h('div', { text: '还没有信件' }),
      h('div', { style: 'margin-top:8px;font-size:12px;opacity:0.6', text: 'AI 写的早晚安、信件和小惊喜会自动存到这里' }),
    ));
    return;
  }

  for (const l of letters) {
    const meta = LETTER_TYPES[l.type] || LETTER_TYPES.letter;
    const favBtn = h('button', {
      class: 'letter-fav-btn' + ((l.kept || l.permanent) ? ' active' : ''),
      title: (l.kept || l.permanent) ? '已收藏到纪念收藏' : '收藏到纪念收藏',
      text: (l.kept || l.permanent) ? '★' : '☆',
      onclick: async (e) => {
        e.stopPropagation();
        await toggleLetterKeep(l, favBtn);
      },
    });
    // ★ 内容折叠：长信默认收起，点「展开全文」才显示全部
    const contentEl = h('div', { class: 'note-content', style: 'white-space:pre-wrap', text: l.content || '' });
    const contentWrap = h('div', { class: 'note-content-wrap' }, contentEl);
    const _long = (l.content || '').length > 120;
    if (_long) {
      contentEl.classList.add('folded');
      const toggle = h('button', { class: 'note-toggle', text: '展开全文' });
      toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        const folded = contentEl.classList.toggle('folded');
        contentEl.classList.toggle('expanded', !folded);
        toggle.textContent = folded ? '展开全文' : '收起';
      });
      contentWrap.appendChild(toggle);
    }
    const card = h('div', { class: 'note-card' + (l.read ? '' : ' unread') },
      h('div', { class: 'note-head' },
        h('span', { class: 'note-cat', style: 'background:' + meta.color + '20;color:' + meta.color, text: meta.icon + ' ' + meta.label }),
        h('span', { class: 'note-date', text: l.date + (l.time ? ' ' + l.time : '') }),
        l.read ? null : h('span', { class: 'red-dot', style: 'margin-left:auto' }),
        favBtn,
      ),
      h('div', { class: 'note-title', text: l.title || meta.label }),
      l.characterId ? h('div', { class: 'p-hint', style: 'margin:3px 0 7px', text: '来自 ' + l.characterId }) : null,
      contentWrap,
      l.audio ? h('button', {
        class: 'chip',
        style: 'margin-top:10px',
        text: '🔊 播放语音',
        onclick: (e) => {
          e.stopPropagation();
          playLetterAudio(l);
        },
      }) : null,
    );
    card.addEventListener('click', () => {
      if (!l.read) {
        Store.letters.update(l.id, { read: true });
        if (l.sleepLetter && typeof Session !== 'undefined') {
          const sid = l.sessionId || (Session.getSessionId ? Session.getSessionId() : 'default');
          const activeContact = (typeof Chat !== 'undefined' && Chat.contact) ? Chat.contact : null;
          const cid = l.characterId || (activeContact ? (activeContact.name || activeContact.id) : 'default');
          fetch('/api/proactive/letter/read', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sid, character_id: cid, date: l.date }),
          }).catch(() => {});
        }
        renderRecords();
      }
      _openLetterDetail(l);
    });
    wrap.appendChild(card);
  }
}

/* 供外部调用：保存一封信件 */
function saveLetter(type, title, content, meta) {
  const now = new Date();
  meta = meta || {};
  Store.letters.add({
    id: Store.uid(),
    date: dateStrOf(now),
    time: fmtClock(now),
    type,
    title,
    content,
    read: false,
    sessionId: meta.sessionId || '',
    characterId: meta.characterId || '',
    sleepLetter: !!meta.sleepLetter,
    sourceKey: meta.sourceKey || '',
    audio: meta.audio || '',
    voiceLetter: !!meta.voiceLetter || !!meta.audio,
    availableAt: meta.availableAt || '',
    permanent: !!meta.permanent,
    kept: !!meta.kept || !!meta.permanent,
    keepSourceKey: meta.keepSourceKey || meta.sourceKey || '',
  });
  updateRecordsTabDot();
}

/* 带超时的 fetch：后端/网络无响应时避免按钮永久 disabled、页面卡住 */
function _fetchTimeout(url, opts, ms) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms || 8000);
  return fetch(url, Object.assign({}, opts || {}, { signal: ctrl.signal }))
    .finally(() => clearTimeout(timer));
}

let _letterKeepSyncing = false;
async function syncLetterKeepsFromBackend() {
  if (_letterKeepSyncing) return;
  _letterKeepSyncing = true;
  try {
    // ★ P1-8：补全 ai_companion_session_id，与其它模块的 fallback 链一致
    const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const contacts = Store.listContacts();
    // ★ 并行拉取 + 超时：串行 fetch 在联系人较多或后端慢时会长时间占用同步标志，显得"卡"
    await Promise.all(contacts.map(async (c) => {
      const cid = c.name || c.id || '';
      if (!cid) return;
      let data;
      try {
        const res = await _fetchTimeout(`/api/relationship/keeps?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(cid)}&limit=300`, {}, 8000);
        if (!res.ok) return;
        data = await res.json().catch(() => ({}));
      } catch (_) { return; }
      const keys = new Set((data.keeps || []).map(k => String(k.source_key || '')).filter(Boolean));
      if (!keys.size) return;
      let changed = false;
      for (const l of Store.letters.list()) {
        if ((l.characterId || '') !== cid) continue;
        // ★ 已收藏的跳过：否则每次同步都 update + changed=true → 无限重渲染，
        //   页面每 50ms 重建一次，用户点击按钮时 DOM 恰好被替换，点击事件丢失（"点不动"）。
        if (l.kept) continue;
        const lk = _letterKeepSourceKey(l);
        const candidates = [lk, l.keepSourceKey, l.sourceKey, l.sourceKey ? ('letter:' + l.sourceKey) : ''].filter(Boolean);
        if (candidates.some(k => keys.has(String(k)))) {
          Store.letters.update(l.id, { kept: true, keepSourceKey: candidates.find(k => keys.has(String(k))) || lk });
          changed = true;
        }
      }
      if (changed && recSeg === 'letter') setTimeout(() => renderRecords(), 50);
    }));
  } catch (_) {
  } finally {
    _letterKeepSyncing = false;
  }
}

function _openLetterDetail(letter) {
  const meta = LETTER_TYPES[letter.type] || LETTER_TYPES.letter;
  const content = h('div', { class: 'letter-detail-content', text: letter.content || '' });
  const favBtn = h('button', {
    class: 'btn btn-plain letter-detail-fav' + ((letter.kept || letter.permanent) ? ' active' : ''),
    text: (letter.kept || letter.permanent) ? '★ 已收藏到纪念收藏' : '☆ 收藏到纪念收藏',
    onclick: async () => {
      await toggleLetterKeep(letter, favBtn, true);
    },
  });
  const box = h('div', { class: 'letter-detail' },
    h('div', { class: 'sheet-title', text: letter.title || meta.label }),
    h('div', { class: 'p-hint', text: (letter.date || '') + (letter.characterId ? ' · 来自 ' + letter.characterId : '') }),
    content,
    h('div', { class: 'letter-detail-actions' },
      favBtn,
      letter.audio ? h('button', { class: 'btn btn-primary', text: '播放语音', onclick: () => playLetterAudio(letter) }) : null,
    )
  );
  Sheet.open(box);
}

function _letterKeepSourceKey(letter) {
  if (!letter) return '';
  if (letter.keepSourceKey) return letter.keepSourceKey;
  if (letter.sourceKey) return 'letter:' + letter.sourceKey;
  const raw = [
    letter.sessionId || '',
    letter.characterId || '',
    letter.type || 'letter',
    letter.title || '',
    letter.date || '',
    letter.time || '',
    String(letter.content || '').slice(0, 80),
  ].join('|');
  let hash = 0;
  for (let i = 0; i < raw.length; i++) hash = ((hash << 5) - hash + raw.charCodeAt(i)) | 0;
  return 'local_letter:' + Math.abs(hash);
}

async function toggleLetterKeep(letter, btn, detailMode) {
  if (!letter) return;
  const willKeep = !(letter.kept || letter.permanent);
  const sourceKey = _letterKeepSourceKey(letter);
  const sid = letter.sessionId || (window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default');
  const activeContact = (typeof Chat !== 'undefined' && Chat.contact) ? Chat.contact : null;
  const cid = letter.characterId || (activeContact ? (activeContact.name || activeContact.id) : 'default');
  if (!cid || cid === 'default') {
    toast('先选择这封信对应的人格再收藏');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    if (willKeep) {
      const keepTypeMap = { morning: 'morning', evening: 'evening', gift: 'gift', letter: 'letter' };
      const res = await _fetchTimeout('/api/relationship/keeps', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sid,
          character_id: cid,
          keep_type: keepTypeMap[letter.type] || 'letter',
          title: letter.title || ((LETTER_TYPES[letter.type] || LETTER_TYPES.letter).label),
          content: letter.content || '',
          audio_url: letter.audio || '',
          source_key: sourceKey,
          gift: {
            icon: (LETTER_TYPES[letter.type] || LETTER_TYPES.letter).icon,
            label: letter.title || ((LETTER_TYPES[letter.type] || LETTER_TYPES.letter).label),
          },
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) throw new Error(data.error || '收藏失败');
      Store.letters.update(letter.id, { kept: true, keepSourceKey: data.source_key || sourceKey });
      letter.kept = true;
      letter.keepSourceKey = data.source_key || sourceKey;
      toast('已放进这个人格的纪念收藏');
    } else {
      const res = await _fetchTimeout('/api/relationship/keeps', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, character_id: cid, source_key: sourceKey }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) throw new Error(data.error || '取消失败');
      Store.letters.update(letter.id, { kept: false, permanent: false, keepSourceKey: sourceKey });
      letter.kept = false;
      letter.permanent = false;
      toast('已取消收藏');
    }
    if (btn) {
      const active = !!(letter.kept || letter.permanent);
      btn.classList.toggle('active', active);
      btn.textContent = detailMode ? (active ? '★ 已收藏到纪念收藏' : '☆ 收藏到纪念收藏') : (active ? '★' : '☆');
      btn.title = active ? '已收藏到纪念收藏' : '收藏到纪念收藏';
    }
    if (letterKeptOnly && !willKeep) renderRecords();
  } catch (e) {
    toast(e.message || '收藏失败');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 手动播放信件语音；用于浏览器拦截自动播放时的兜底。 */
function playLetterAudio(letter) {
  if (!letter || !letter.audio) {
    toast('这封信暂时没有语音');
    return;
  }
  try {
    const audio = new Audio(letter.audio);
    audio.play().catch(() => toast('请先点击页面后再播放语音'));
  } catch (_) {
    toast('语音播放失败');
  }
}

/* 拉取后端待补收的身份小作文 → 存成信件（读后清除，一次性） */
async function _fetchPendingIdentityLetter() {
  try {
    const c = (typeof Chat !== 'undefined' && Chat.contact) ? Chat.contact : null;
    const sid = (typeof Session !== 'undefined' && Session.getSessionId) ? Session.getSessionId() : 'default';
    const cid = c ? (c.name || c.id || '') : 'default';
    if (!cid) return;
    const res = await fetch(`/api/identity/letter?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(cid)}`);
    if (!res.ok) return;
    const j = await res.json();
    if (j && j.content) {
      const today = dateStrOf(Date.now());
      const existing = Store.letters.list().find(l =>
        l.type === 'letter' && l.title === '坦诚的信' &&
        l.characterId === cid && l.sessionId === sid
      );
      if (!existing) {
        saveLetter('letter', '坦诚的信', j.content, {
          sessionId: sid, characterId: cid, sourceKey: j.letter_key || '',
          keepSourceKey: j.letter_key ? ('letter:' + j.letter_key) : '',
        });
      }
      fetch('/api/identity/letter/ack', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, character_id: cid }),
      }).catch(() => {});
    }
  } catch (_) {}
}
