'use strict';
/* ============================================================
   通讯录页 + AI 伴侣创建表单（核心信息）
   细粒度设置（人性化/主动频率/免打扰/记忆库）在「资料与设置」页
   ============================================================ */

function renderContacts() {
  const wrap = $('#page-persona');
  if (!wrap) return;
  wrap.innerHTML = '';
  const contacts = Store.listContacts();
  refreshVisibleRelationshipStates(contacts);

  const addRow = h('div', { class: 'row' },
    h('div', { class: 'avatar sm', style: 'background:var(--soft,#EFEFEC);color:var(--text,#141414);font-size:26px', text: '＋' }),
    h('div', { class: 'row-label' },
      h('div', { text: '添加 AI 伴侣' }),
      h('div', { class: 'row-sub', text: '创建后可在「资料与设置」里细调 TA 的一切' }),
    ),
  );
  addRow.addEventListener('click', () => openContactSheet(null));
  wrap.appendChild(h('div', { class: 'list-group' }, addRow));

  if (!contacts.length) {
    wrap.appendChild(h('div', { class: 'empty-state' },
      emptyIcon('user'),
      h('div', { text: '还没有 AI 伴侣' }),
      h('div', { style: 'margin-top:8px', text: '点上方「添加 AI 伴侣」，亲手创造一个' }),
      h('div', { style: 'margin-top:4px', text: '懂你的 TA 吧（恋人 / 挚友 / 家人…都可以）' }),
    ));
    return;
  }

  const group = h('div', { class: 'list-group' });
  group.appendChild(h('div', { class: 'group-title', text: '我的 AI 伴侣 (' + contacts.length + ')' }));
  // ★ 2026-09-15：亲密度数值面板默认隐藏（后端 RELATIONSHIP_UI_VISIBLE=false）
  const _relUI = (typeof window.relUIShow === 'function') ? window.relUIShow() : false;
  for (const c of contacts) {
    const inti = intimacyInfo(c);
    const sig = signatureOf(c);

    const row = h('div', { class: 'row' },
      avatarEl(c, 'sm'),
      h('div', { class: 'row-label' },
        h('div', { text: c.name }),
        h('div', { class: 'row-sub', text: sig }),
        _relUI ? h('div', { class: 'intimacy-line' },
          h('div', { class: 'intimacy-bar' },
            h('div', { class: 'intimacy-fill', style: 'width:' + inti.pct + '%' }),
          ),
          h('span', { class: 'intimacy-text', text: inti.name + ' · ' + inti.v + '/100' }),
        ) : null,
      ),
      h('div', { class: 'row-arrow', text: '›' }),
    );
    row.addEventListener('click', () => Chat.open(c.id));
    // 长按 → 资料与设置
    let timer = null, moved = false;
    row.addEventListener('touchstart', () => {
      moved = false;
      timer = setTimeout(() => { if (!moved) Profile.open(c.id); }, 450);
    }, { passive: true });
    row.addEventListener('touchmove', () => { moved = true; clearTimeout(timer); }, { passive: true });
    ['touchend', 'touchcancel'].forEach((ev) => row.addEventListener(ev, () => clearTimeout(timer)));
    row.addEventListener('contextmenu', (e) => { e.preventDefault(); Profile.open(c.id); });
    group.appendChild(row);
  }
  wrap.appendChild(group);
}

const _relationshipRefreshInFlight = new Set();
function refreshVisibleRelationshipStates(contacts) {
  if (!Array.isArray(contacts) || !contacts.length) return;
  const sid = (typeof Session !== 'undefined' && Session.getSessionId)
    ? Session.getSessionId() : (localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default');
  contacts.forEach((contact) => {
    if (!contact || !contact.id || !contact.name) return;
    const key = sid + ':' + contact.id;
    if (_relationshipRefreshInFlight.has(key)) return;
    _relationshipRefreshInFlight.add(key);
    fetch(`/api/relationship/state?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(contact.name)}`)
      .then(r => r.ok ? r.json() : null)
      .then((state) => {
        if (!state) return;
        // ★ 后端还没有任何真实交互记录（无天数、陌生人阶段且三项全为初始值）时不回写：
        //   否则刚创建、用户手动设定过初始亲密度的联系人会被后端初始值（0/50）覆盖清零。
        const pristine = !Number(state.interaction_days)
          && (!state.stage || state.stage === 'stranger')
          && !Number(state.intimacy)
          && !Number(state.trust)
          && (!Number(state.affection) || Number(state.affection) === 50);
        if (pristine) return;
        const patch = {};
        for (const field of ['intimacy', 'affection', 'trust']) {
          const n = Number(state[field]);
          if (Number.isFinite(n)) patch[field] = Math.max(0, Math.min(100, n));
        }
        if (state.stage) patch.relationshipStage = state.stage;
        if (Object.keys(patch).length) {
          Store.updateContact(contact.id, patch);
          const current = Store.getContact(contact.id);
          const changed = ['intimacy', 'affection', 'trust', 'relationshipStage']
            .some((field) => String((contact || {})[field]) !== String((current || {})[field]));
          if (changed) setTimeout(renderContacts, 0);
        }
      })
      .catch(() => {})
      .finally(() => _relationshipRefreshInFlight.delete(key));
  });
}

/** TA 的“签名”：背景故事第一行；没有则默认文案 */
function signatureOf(c) {
  if (c.background && c.background.trim()) {
    const first = c.background.trim().split('\n')[0].trim();
    return first.length > 28 ? first.slice(0, 28) + '…' : first;
  }
  return '这个人很神秘，什么都没留下';
}

function relationLabel(v) {
  const map = { lover: '恋人', bestfriend: '挚友', family: '家人', mentor: '导师', coworker: '同事', netfriend: '网友' };
  return map[v] || '';
}

/** 新聊天：选择联系人 */
function showNewChatSheet() {
  const contacts = Store.listContacts();
  if (!contacts.length) {
    toast('通讯录还没有 AI 伴侣，先创建一个吧');
    return;
  }
  const sheetEl = h('div', { class: 'action-sheet' },
    h('div', { class: 'sheet-title', text: '选择联系人' }),
  );
  for (const c of contacts) {
    const row = h('div', { class: 'action-item' },
      avatarEl(c, 'sm'),
      h('div', { style: 'margin-left:12px;flex:1', text: c.name }),
    );
    row.addEventListener('click', () => { Sheet.close(); Chat.open(c.id); });
    sheetEl.appendChild(row);
  }
  Sheet.open(sheetEl, 'action-sheet');
}

/* ============================================================
   AI 伴侣创建表单（核心信息；细调在资料与设置页）
   ============================================================ */
function openContactSheet(contactId) {
  const c = contactId ? Store.getContact(contactId) : null;
  const isNew = !c;
  const form = h('div', {});

  /* ---- 基本 ---- */
  const nameInput = h('input', { type: 'text', placeholder: 'TA 的名字（如：林晚、Sam）', value: c ? c.name : '' });

  /* 角色卡 JSON 一键导入 */
  const cardInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
  const cardBtn = h('button', { class: 'btn btn-plain', style: 'font-size:13px', text: '📋 导入角色卡 JSON（一键建好 TA）' });
  cardBtn.addEventListener('click', () => cardInput.click());
  cardInput.addEventListener('change', () => {
    const f = cardInput.files[0];
    if (!f) return;
    importCharCardFile(f, null, () => {
      Sheet.close();
      renderContacts();
      renderChatList();
    });
    cardInput.value = '';
  });

  const relSelect = h('select', {}, h('option', { value: 'other', text: '（不指定关系）' }));
  for (const [k, def] of Object.entries(RELATIONS)) {
    if (k === 'other') continue;
    relSelect.appendChild(h('option', { value: k, text: relationLabel(k) }));
  }
  relSelect.value = c ? (c.relation || 'other') : 'other';

  let relLevel = c ? (Number(c.relationLevel) || 5) : 5;
  const relLevelSlider = h('input', { type: 'range', min: '1', max: '10', step: '1', value: String(relLevel) });
  const relLevelText = h('div', { class: 'level-num' });
  function refreshRelLevel() {
    relLevel = Number(relLevelSlider.value);
    const rel = relSelect.value;
    relLevelText.textContent = rel === 'other' ? relLevel + '/10' : closenessLabel(relLevel);
  }
  relLevelSlider.addEventListener('input', refreshRelLevel);
  relSelect.addEventListener('change', refreshRelLevel);
  refreshRelLevel();

  let initialIntimacy = c && c.intimacy != null
    ? Math.max(0, Math.min(100, Number(c.intimacy) || 0)) : 30;
  const intimacySlider = h('input', { type: 'range', min: '0', max: '100', step: '1', value: String(initialIntimacy) });
  const intimacyText = h('div', { class: 'level-num', text: initialIntimacy + '/100' });
  intimacySlider.addEventListener('input', () => {
    initialIntimacy = Number(intimacySlider.value);
    intimacyText.textContent = initialIntimacy + '/100';
  });

  /* ---- 头像（仅照片） ---- */
  let avatarUrl = c ? (c.avatarUrl || '') : '';
  const avatarPreview = h('div', { class: 'avatar-preview' });
  function refreshAvatarPreview() {
    avatarPreview.innerHTML = '';
    if (avatarUrl) {
      avatarPreview.appendChild(h('img', { src: avatarUrl, alt: '' }));
    } else {
      const letter = h('div', { class: 'avatar-preview-letter', text: (nameInput.value || '?').charAt(0) });
      letter.style.background = 'hsl(' + avatarHue(nameInput.value || '?') + ', 55%, 58%)';
      avatarPreview.appendChild(letter);
    }
  }
  nameInput.addEventListener('input', refreshAvatarPreview);
  refreshAvatarPreview();
  const photoInput = h('input', { type: 'file', accept: 'image/*', hidden: true });
  const photoBtn = h('button', { class: 'btn btn-plain', text: avatarUrl ? '更换照片' : '上传照片做头像' });
  photoBtn.addEventListener('click', () => photoInput.click());
  photoInput.addEventListener('change', () => {
    const f = photoInput.files[0];
    if (!f) return;
    fileToDataUrl(f, 128, (url) => {
      if (!url) { toast('图片读取失败'); return; }
      avatarUrl = url;
      photoBtn.textContent = '更换照片';
      refreshAvatarPreview();
      toast('已设置照片头像');
    });
    photoInput.value = '';
  });
  const clearPhotoBtn = h('button', { class: 'btn btn-plain', text: '清除照片' });
  clearPhotoBtn.addEventListener('click', () => { avatarUrl = ''; photoBtn.textContent = '上传照片做头像'; refreshAvatarPreview(); toast('已清除'); });
  const avatarBox = h('div', {},
    avatarPreview,
    h('div', { style: 'display:flex;gap:8px;margin-top:8px' }, photoBtn, clearPhotoBtn),
    photoInput,
  );

  /* ---- 称呼与自称 ---- */
  const callsInput = h('input', { type: 'text', placeholder: 'TA 怎么称呼你？如：亲爱的、宝、小名', value: c ? (c.callsYou || '') : '' });

  let selfRef = c ? (c.selfRef || '我') : '我';
  const selfRefSelect = h('select', {});
  for (const s of SELF_REFS) selfRefSelect.appendChild(h('option', { value: s, text: s }));
  selfRefSelect.value = SELF_REFS.includes(selfRef) ? selfRef : '自定义…';
  const selfRefInput = h('input', { type: 'text', placeholder: '自定义自称', hidden: true, value: SELF_REFS.includes(selfRef) ? '' : selfRef });
  function refreshSelfRefInput() {
    selfRefInput.hidden = selfRefSelect.value !== '自定义…';
    if (selfRefSelect.value !== '自定义…') selfRefInput.value = '';
  }
  selfRefSelect.addEventListener('change', refreshSelfRefInput);
  refreshSelfRefInput();

  /* ---- 人设 ---- */
  const langSelect = h('select', {});
  for (const l of LANGUAGES) langSelect.appendChild(h('option', { value: l, text: l }));
  langSelect.value = c ? (c.language || '简体中文') : '简体中文';

  const traitSet = new Set(c ? (c.traits || '').split(/[、,，]/).map((s) => s.trim()).filter(Boolean) : []);
  const traitChips = h('div', { class: 'chips' });
  for (const t of TRAITS) {
    const b = h('button', { type: 'button', class: 'chip', text: t });
    if (traitSet.has(t)) b.classList.add('selected');
    b.addEventListener('click', () => {
      if (traitSet.has(t)) { traitSet.delete(t); b.classList.remove('selected'); }
      else { traitSet.add(t); b.classList.add('selected'); }
    });
    traitChips.appendChild(b);
  }
  // ★ 渲染"自定义"性格 chip：编辑伴侣时，把已保存的自定义标签也作为 selected chip 重新挂上
  if (c) {
    for (const t of Array.from(traitSet)) {
      if (TRAITS.indexOf(t) >= 0) continue;
      const b = h('button', { type: 'button', class: 'chip selected', text: t });
      b.addEventListener('click', () => { traitSet.delete(t); b.remove(); });
      traitChips.appendChild(b);
    }
  }
  // 自定义性格标签
  const customTraitInput = h('input', { type: 'text', placeholder: '自定义性格，如：爱撒娇、有点小霸道', value: '' });
  const addTraitBtn = h('button', { class: 'btn btn-plain', text: '添加' });
  function addCustomTrait() {
    const t = customTraitInput.value.trim();
    if (!t) return;
    if (traitSet.has(t)) { toast('这个性格已添加'); return; }
    traitSet.add(t);
    const b = h('button', { type: 'button', class: 'chip selected', text: t });
    b.addEventListener('click', () => { traitSet.delete(t); b.remove(); });
    traitChips.appendChild(b);
    customTraitInput.value = '';
  }
  addTraitBtn.addEventListener('click', addCustomTrait);
  customTraitInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addCustomTrait(); } });

  const hobbiesInput = h('textarea', { placeholder: 'TA 的爱好，如：打游戏、撸猫、看动漫…' });
  hobbiesInput.value = c ? (c.hobbies || '') : '';
  const bgInput = h('textarea', { placeholder: 'TA 的背景故事：从哪来、经历过什么、为什么在你身边…' });
  bgInput.value = c ? (c.background || '') : '';

  /* ---- 说话风格（可多选，含自定义） ---- */
  const styleSet = new Set((c ? (c.style || '') : '').split(',').map((s) => s.trim()).filter(Boolean));
  const styleChips = h('div', { class: 'chips' });
  for (const [k, def] of Object.entries(STYLES)) {
    const b = h('button', { type: 'button', class: 'chip', text: def.label });
    if (styleSet.has(k)) b.classList.add('selected');
    b.addEventListener('click', () => {
      if (styleSet.has(k)) { styleSet.delete(k); b.classList.remove('selected'); }
      else { styleSet.add(k); b.classList.add('selected'); }
      refreshCustomStyle();
    });
    styleChips.appendChild(b);
  }
  const customStyleInput = h('textarea', { placeholder: '自定义说话风格：TA 怎么说话、爱用什么词、语气如何……写越细越像', hidden: !styleSet.has('custom') });
  customStyleInput.value = c ? (c.customStyle || '') : '';
  function refreshCustomStyle() { customStyleInput.hidden = !styleSet.has('custom'); }
  refreshCustomStyle();

  /* ---- 聊天背景 ---- */
  let wallpaper = c ? (c.wallpaper || '') : '';
  const swatches = h('div', { class: 'swatches' });
  const defSwatch = h('button', { type: 'button', class: 'swatch selected', text: '默' });
  defSwatch.style.fontSize = '11px';
  defSwatch.style.color = '#888';
  defSwatch.addEventListener('click', () => { wallpaper = ''; $$('.swatch').forEach((x) => x.classList.remove('selected')); defSwatch.classList.add('selected'); toast('已恢复默认背景'); });
  swatches.appendChild(defSwatch);
  const paintSwatch = (color) => {
    const s = h('button', { type: 'button', class: 'swatch' + (wallpaper === color ? ' selected' : '') });
    s.style.background = color;
    if (color === '#2F3542') s.style.border = '1px solid #ccc';
    s.addEventListener('click', () => {
      wallpaper = color;
      $$('.swatch').forEach((x) => x.classList.remove('selected'));
      s.classList.add('selected');
      toast('已选聊天背景');
    });
    return s;
  };
  for (const w of WALLPAPERS) swatches.appendChild(paintSwatch(w));
  if (wallpaper && wallpaper.startsWith('data:')) {
    const s = h('button', { type: 'button', class: 'swatch selected' });
    s.style.backgroundImage = 'url(' + wallpaper + ')';
    s.style.backgroundSize = 'cover';
    swatches.appendChild(s);
  }
  const wallInput = h('input', { type: 'file', accept: 'image/*', hidden: true });
  const wallBtn = h('button', { class: 'btn btn-plain', text: '上传照片做聊天背景' });
  wallBtn.addEventListener('click', () => wallInput.click());
  wallInput.addEventListener('change', () => {
    const f = wallInput.files[0];
    if (!f) return;
    fileToDataUrl(f, 640, (url) => {
      if (!url) { toast('图片读取失败'); return; }
      wallpaper = url;
      toast('已设置聊天背景');
    });
    wallInput.value = '';
  });
  const clearWallBtn = h('button', { class: 'btn btn-plain', text: '恢复默认' });
  clearWallBtn.addEventListener('click', () => {
    wallpaper = '';
    $$('.swatch').forEach((x) => x.classList.remove('selected'));
    defSwatch.classList.add('selected');
    toast('已恢复默认');
  });
  const wallBox = h('div', {},
    swatches,
    h('div', { style: 'display:flex;gap:8px;margin-top:8px' }, wallBtn, clearWallBtn),
    wallInput,
  );

  /* ---- AI 大脑（服务商 / 模型） ---- */
  let provider = c ? (c.aiProvider || 'deepseek') : 'deepseek';
  let aiBase = c ? (c.aiBase || '') : '';
  const providerSelect = h('select', {});
  for (const [k, def] of Object.entries(AI_PROVIDERS)) {
    providerSelect.appendChild(h('option', { value: k, text: def.label }));
  }
  providerSelect.value = AI_PROVIDERS[provider] ? provider : 'deepseek';
  const modelSelect = h('select', {});
  const modelInput = h('input', { type: 'text', placeholder: '模型名称（如 qwen2.5:7b）', hidden: true, value: c ? c.model : '' });
  const baseInput = h('input', { type: 'text', placeholder: '接口地址，如 http://127.0.0.1:8080/v1', hidden: true, value: aiBase });
  function refreshModelControl() {
    const def = AI_PROVIDERS[providerSelect.value];
    modelSelect.innerHTML = '';
    if (def.models && def.models.length) {
      modelSelect.hidden = false;
      modelInput.hidden = true;
      for (const m of def.models) modelSelect.appendChild(h('option', { value: m, text: m }));
      const cur = c ? c.model : '';
      modelSelect.value = def.models.includes(cur) ? cur : def.models[0];
    } else {
      modelSelect.hidden = true;
      modelInput.hidden = false;
    }
    baseInput.hidden = !(providerSelect.value === 'custom' || providerSelect.value === 'ollama');
  }
  providerSelect.addEventListener('change', refreshModelControl);
  refreshModelControl();

  /* ---- 组装 ---- */
  form.appendChild(sectionTitle('基本'));
  form.appendChild(field('名字 *', nameInput));
  form.appendChild(field('', cardBtn));
  form.appendChild(cardInput);
  form.appendChild(field('TA 和你的关系', relSelect));
  form.appendChild(field('关系程度（越往右越亲密）', h('div', {}, relLevelSlider, relLevelText)));
  form.appendChild(field('初始/当前亲密度', h('div', {}, intimacySlider, intimacyText)));
  form.appendChild(field('头像（上传照片）', avatarBox));
  form.appendChild(field('TA 怎么称呼你', callsInput));
  form.appendChild(field('TA 怎么自称', h('div', {}, selfRefSelect, h('div', { style: 'margin-top:8px' }, selfRefInput))));

  form.appendChild(sectionTitle('人设'));
  form.appendChild(field('回复语言', langSelect));
  form.appendChild(field('性格（可多选，可自定义）', h('div', {}, traitChips, h('div', { style: 'display:flex;gap:8px;margin-top:8px' }, customTraitInput, addTraitBtn))));
  form.appendChild(field('爱好', hobbiesInput));
  form.appendChild(field('背景故事', bgInput));
  form.appendChild(field('说话风格（可多选）', h('div', {}, styleChips, h('div', { style: 'margin-top:8px' }, customStyleInput))));
  form.appendChild(field('聊天背景', wallBox));
  form.appendChild(field('AI 大脑（服务商）', h('div', {}, providerSelect, h('div', { style: 'margin-top:8px' }, modelSelect, modelInput), h('div', { style: 'margin-top:8px' }, baseInput))));

  form.appendChild(h('div', { class: 'help-box', style: 'margin:0 0 14px', text: '创建后，点聊天页顶部的名字或「⋯」菜单 →「资料与设置」，可以继续细调：打字节奏、回复长度、错别字、颜文字、口头禅、主动聊天频率、免打扰时段、记忆与记忆库等。' }));

  /* ---- 保存 ---- */
  const saveBtn = h('button', { class: 'btn btn-primary', text: isNew ? '创建 TA' : '保存修改' });
  saveBtn.addEventListener('click', async () => {
    const name = nameInput.value.trim();
    if (!name) { toast('请填写 TA 的名字'); return; }
    const selfRefVal = selfRefSelect.value === '自定义…' ? (selfRefInput.value.trim() || '我') : selfRefSelect.value;
    const payload = {
      name,
      relation: relSelect.value === 'other' ? '' : relSelect.value,
      relationLevel: relLevel,
      ...(isNew || c.intimacy != null ? { intimacy: initialIntimacy } : {}),
      avatarUrl,
      avatar: '',
      callsYou: callsInput.value.trim(),
      selfRef: selfRefVal,
      language: langSelect.value,
      traits: Array.from(traitSet).join('、'),
      hobbies: hobbiesInput.value.trim(),
      background: bgInput.value.trim(),
      style: Array.from(styleSet).join(','),
      customStyle: styleSet.has('custom') ? customStyleInput.value.trim() : '',
      wallpaper,
      model: modelSelect.hidden ? modelInput.value.trim() : modelSelect.value,
      aiProvider: providerSelect.value,
      aiBase: baseInput.value.trim(),
      builtin: false,
      // 防御性默认：避免字段缺失造成"刷新后消失"的错觉
      system: '',
      memory: '',
      catchphrase: '',
      typo: false,
      kaomoji: false,
      sticker: false,
      emojiFreq: 'normal',
      replyLen: 'normal',
      replySpeed: 'normal',
      replyDelay: 'normal',
      replyDelayMin: 2,
      replyDelayMax: 8,
      offlineEnabled: false,
      offlineMaxDelayMin: 30,
      splitMsg: true,
      replySegMin: 1,
      replySegMax: 6,
      proactiveMaxMin: 120,
      proactiveFailUntil: 0,   // 失败退避用；主动排期已交由后端把关（nextProactive 已删）
      quietStart: '',
      quietEnd: '',
      memStore: [],
      logs: [],
      summarizedUpTo: 0,
      lastSummarizeAt: 0,
      proactiveFmtHistory: [],
    };
    // 联系人资料必须同步到后端角色卡；否则聊天能看到本地联系人，但
    // 后端调度器/人格提示/语音仍找不到角色，表现为 404 或默认人格。
    const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const characterCard = {
      character_name: name,
      self_name: selfRefVal,
      call_user: callsInput.value.trim() || '你',
      personality: Array.from(traitSet).join('、') || '温柔体贴，回复自然，记得用户说过的话。',
      relationship: relSelect.value === 'other' ? '' : relSelect.value,
      hobbies: hobbiesInput.value.trim(),
      worldview: bgInput.value.trim(),
      speaking_style: Array.from(styleSet).join('、'),
      avatar: avatarUrl || '',
      persona_configured: true,
    };
    try {
      const cardRes = await fetch('/api/pc/character/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(characterCard),
      });
      const cardData = await cardRes.json().catch(() => ({}));
      if (!cardRes.ok || !cardData.ok) throw new Error(cardData?.error?.message || '角色卡保存失败');
    } catch (e) {
      toast('人格保存失败：' + (e.message || '请检查后端')); return;
    }
    const relationshipStage = initialIntimacy >= 90 ? 'soulmate' : initialIntimacy >= 70 ? 'lover' : initialIntimacy >= 40 ? 'close_friend' : initialIntimacy >= 20 ? 'friend' : 'stranger';
    const syncRelationship = () => {
      fetch('/api/intimacy/report', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, character_id: name, value: initialIntimacy })
      }).catch(() => {});
      fetch('/api/relationship/update', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid, character_id: name, intimacy: initialIntimacy, stage: relationshipStage })
      }).catch(() => {});
    };
    if (isNew) {
      const newContact = Object.assign({ id: Store.uid() }, payload, { relationshipStage });
      Store.addContact(newContact);
      syncRelationship();
      toast('已创建：' + name + '。进入聊天页后可继续细调 TA。', 3000);
    } else {
      Store.updateContact(c.id, Object.assign({}, payload, { relationshipStage }));
      syncRelationship();
      toast('已保存');
    }
    Sheet.close();
    renderContacts();
    renderChatList();
  });
  form.appendChild(saveBtn);

  /* ---- 删除 / 取消 ---- */
  if (!isNew) {
    const delBtn = h('button', { class: 'btn btn-danger', text: '删除这个伴侣' });
    delBtn.addEventListener('click', () => {
      if (confirm('确定删除「' + c.name + '」？聊天记录会一并删除。')) {
        Store.deleteContact(c.id);
        Sheet.close();
        Chat.close();
        renderContacts();
        renderChatList();
        toast('已删除');
      }
    });
    form.appendChild(delBtn);
  }
  const cancelBtn = h('button', { class: 'btn btn-plain', text: '取消' });
  cancelBtn.addEventListener('click', () => Sheet.close());
  form.appendChild(cancelBtn);

  const title = h('div', { class: 'sheet-title', text: isNew ? '创建你的 AI 伴侣' : '编辑 ' + (c ? c.name : '') });
  Sheet.open(h('div', {}, title, form));
}

function sectionTitle(text) {
  return h('div', { class: 'form-section-title', text: text });
}

function field(label, control) {
  return h('div', { class: 'field' },
    h('div', { class: 'field-label', text: label }),
    control,
  );
}

