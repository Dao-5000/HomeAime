/* ============================================================
   Homeaime · Flow v7 增强层（flow7.js）
   ------------------------------------------------------------
   只在 UI 层做三件事，不改任何功能模块：
     1) 统一图标：把原项目页面里散落的 emoji 换成同一套 SVG 线条图标
        （复用壳层已有的 #i-* 实心图标 + 本文件新增的线条图标）
     2) 交互/动效增强：视图切换、卡片悬浮、按压缩放等
     3) 为新渲染出来的内容自动补图标（MutationObserver）

   回滚：删掉 index.html 里对本文件与 flow7.css 的两行引用即可。
   ============================================================ */
'use strict';
(function () {
  /* ---------- 1. 图标定义 ---------- */
  /* 壳层已有 symbol（实心）直接复用 */
  const SOLID = {
    brain: 'i-brain', folder: 'i-folder', cam: 'i-cam', search: 'i-search', phone: 'i-phone',
    sliders: 'i-sliders', moon: 'i-moon', plus: 'i-plus', spark: 'i-spark', heart: 'i-heart',
    leaf: 'i-leaf', users: 'i-users', pulse: 'i-pulse', grid: 'i-grid', home: 'i-home',
  };
  /* 新增 symbol（线条，1.8 描边） */
  const NEW = {
    book: '<path d="M4 5a2 2 0 0 1 2-2h5v17H6a2 2 0 0 0-2 2z"/><path d="M20 5a2 2 0 0 0-2-2h-5v17h5a2 2 0 0 1 2 2z"/>',
    notebook: '<rect x="5.5" y="3" width="13" height="18" rx="2"/><path d="M3 7.5h5M3 12h5M3 16.5h5"/>',
    gift: '<rect x="3" y="8" width="18" height="13" rx="2"/><path d="M3 12.5h18M12 8v13"/><path d="M12 8S10.6 3 8.2 3a2.4 2.4 0 0 0 0 5zM12 8s1.4-5 3.8-5a2.4 2.4 0 0 1 0 5z"/>',
    music: '<path d="M9 18V6.5l10-2V16"/><ellipse cx="6.4" cy="18" rx="2.6" ry="2.2"/><ellipse cx="16.4" cy="16" rx="2.6" ry="2.2"/>',
    headphones: '<path d="M4 14v-2a8 8 0 0 1 16 0v2"/><rect x="3" y="13.5" width="4" height="6.5" rx="1.6"/><rect x="17" y="13.5" width="4" height="6.5" rx="1.6"/>',
    tv: '<rect x="3" y="5" width="18" height="12" rx="2"/><path d="M8.5 21h7M12 17v4"/>',
    gamepad: '<rect x="2.5" y="7" width="19" height="10" rx="5"/><path d="M7 12h4M9 10v4M15.8 11.4h.01M17.8 13.4h.01"/>',
    pickaxe: '<path d="M3.5 20.5L11 13"/><path d="M8 11.5c0-5 4-8.5 9.5-8.5 0 5.5-3.5 9.5-8.5 9.5z"/><path d="M13 15l4.5 4.5"/>',
    pencil: '<path d="M4 20h4L20 8l-4-4L4 16z"/><path d="M14.5 5.5l4 4"/>',
    clipboard: '<rect x="6" y="4.5" width="12" height="16.5" rx="2"/><path d="M9.5 4.5a2.5 2.5 0 0 1 5 0"/><path d="M9 10.5h6M9 14.5h4"/>',
    thumbUp: '<path d="M7 10.5v9.5H4.5V10.5zM7 10.5l3.6-6.8a1.9 1.9 0 0 1 3.4 1.5L13.4 9h4.3a1.9 1.9 0 0 1 1.9 2.2l-1.2 6.6a1.9 1.9 0 0 1-1.9 1.6H7"/>',
    thumbDown: '<path d="M17 13.5V4h2.5v9.5zM17 13.5l-3.6 6.8a1.9 1.9 0 0 1-3.4-1.5L10.6 15H6.3a1.9 1.9 0 0 1-1.9-2.2l1.2-6.6A1.9 1.9 0 0 1 7.5 4.6H17"/>',
    xCircle: '<circle cx="12" cy="12" r="8.6"/><path d="M9.2 9.2l5.6 5.6M14.8 9.2l-5.6 5.6"/>',
    upload: '<path d="M12 16V4.5M7.5 9l4.5-4.5L16.5 9"/><path d="M4.5 19.5h15"/>',
    download: '<path d="M12 4.5V16M7.5 11.5L12 16l4.5-4.5"/><path d="M4.5 19.5h15"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20.5 4v5h-5"/>',
    broom: '<path d="M15 3.5l5.5 5.5-8.5 8.5H7v-5z"/><path d="M7 12.5l4.5 4.5M7 17.5L3.5 21"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M8.5 21h7"/>',
    rocket: '<path d="M6 15.5c-1.2 2.5-1.4 4.6-1.4 4.6s2.1-.2 4.6-1.4"/><path d="M9.5 14.5L6 11c0-6 4.5-10 12-10 0 7.5-4 12-10 12z"/><circle cx="14.5" cy="8" r="1.5"/>',
    message: '<path d="M4.5 5h15a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5H10l-5.5 4V6.5A1.5 1.5 0 0 1 6 5z"/>',
    eye: '<path d="M2.5 12S6 6.2 12 6.2 21.5 12 21.5 12 18 17.8 12 17.8 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.6"/>',
    target: '<circle cx="12" cy="12" r="8.2"/><circle cx="12" cy="12" r="4.2"/><circle cx="12" cy="12" r="1"/>',
    lock: '<rect x="5" y="10" width="14" height="10.5" rx="2.4"/><path d="M8.2 10V7.2a3.8 3.8 0 0 1 7.6 0V10"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2.2M12 19.3v2.2M4.2 12H2M22 12h-2.2M5.6 5.6L4 4M20 20l-1.6-1.6M18.4 5.6L20 4M4 20l1.6-1.6"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.4"/><path d="M3.5 10h17M8 3.5v3.5M16 3.5v3.5"/>',
    checkCircle: '<circle cx="12" cy="12" r="8.6"/><path d="M8.4 12.5l2.6 2.6 4.6-5.2"/>',
    warn: '<path d="M12 4l8.6 15.4H3.4z"/><path d="M12 10v4.2M12 17.2h.01"/>',
    trash: '<path d="M4 7h16M9.5 7V4.8h5V7M6.5 7l1 12.5h9L17.5 7M10 11v5.5M14 11v5.5"/>',
    bulb: '<path d="M9.5 18.5h5M10.5 21h3"/><path d="M12 3a6 6 0 0 1 3.6 10.8c-.7.5-1.1 1.3-1.1 2.1v.6H9.5v-.6c0-.8-.4-1.6-1.1-2.1A6 6 0 0 1 12 3z"/>',
    clock: '<circle cx="12" cy="12" r="8.6"/><path d="M12 7v5.2l3.4 2"/>',
    globe: '<circle cx="12" cy="12" r="8.6"/><path d="M3.4 12h17.2M12 3.4c3 3.2 3 14 0 17.2M12 3.4c-3 3.2-3 14 0 17.2"/>',
    save: '<path d="M5 4.5h11L19.5 8v11.5H5z"/><path d="M8.5 4.5V10h7V4.5M8.5 19.5V14h8v5.5"/>',
    palette: '<path d="M12 3.2a8.8 8.8 0 1 0 0 17.6c1.7 0 2-1 1.2-2-.9-1.1-.2-2.5 1.3-2.5h2.4a3.9 3.9 0 0 0 3.9-3.9c0-5.1-3.9-9.2-8.8-9.2z"/><circle cx="8" cy="10.2" r="1"/><circle cx="12" cy="7.6" r="1"/><circle cx="16" cy="10.2" r="1"/>',
    award: '<circle cx="12" cy="9" r="5"/><path d="M9.2 13.4L8 21l4-2.1 4 2.1-1.2-7.6"/>',
    pin: '<path d="M12 21s7-6.6 7-11.2a7 7 0 1 0-14 0C5 14.4 12 21 12 21z"/><circle cx="12" cy="9.8" r="2.5"/>',
    bell: '<path d="M6 16.5V11a6 6 0 1 1 12 0v5.5l1.8 1.8H4.2z"/><path d="M10 19.5a2 2 0 0 0 4 0"/>',
    gear: '<circle cx="12" cy="12" r="3.1"/><path d="M19.6 13.5a7.7 7.7 0 0 0 0-3l1.7-1.3-2-3.4-2 .8a7.7 7.7 0 0 0-2.6-1.5L14.3 3h-4l-.4 2.1a7.7 7.7 0 0 0-2.6 1.5l-2-.8-2 3.4L5 10.5a7.7 7.7 0 0 0 0 3l-1.7 1.3 2 3.4 2-.8a7.7 7.7 0 0 0 2.6 1.5l.4 2.1h4l.4-2.1a7.7 7.7 0 0 0 2.6-1.5l2 .8 2-3.4z"/>',
    film: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M8 4.5v15M16 4.5v15M3 9.5h5M3 14.5h5M16 9.5h5M16 14.5h5"/>',
    flame: '<path d="M12 3c3 3.4 6 5.4 6 9.4a6 6 0 0 1-12 0c0-1.6.7-3 1.8-4.3.6 1.4 1.6 2.3 2.7 2.8C10 8.4 10.8 5.8 12 3z"/>',
  };

  /* ---------- 2. emoji → 图标 映射 ---------- */
  const M = (name) => ({ n: name, solid: !!SOLID[name] });
  const MAP = {
    '🧠': M('brain'), '📖': M('book'), '📚': M('book'), '📕': M('book'), '📗': M('book'),
    '📔': M('notebook'), '📒': M('notebook'), '📓': M('notebook'),
    '📁': M('folder'), '📂': M('folder'), '🗂': M('folder'),
    '⚙': M('gear'), '🛠': M('gear'), '🔧': M('gear'),
    '🌙': M('moon'), '🌜': M('moon'), '🌛': M('moon'),
    '📞': M('phone'), '☎': M('phone'), '📱': M('phone'),
    '➕': M('plus'), '✨': M('spark'), '🌟': M('spark'), '💫': M('spark'), '✦': M('spark'),
    '❤': M('heart'), '💕': M('heart'), '💞': M('heart'), '💗': M('heart'), '💖': M('heart'), '🫶': M('heart'), '💘': M('heart'),
    '🍃': M('leaf'), '🌿': M('leaf'), '🌱': M('leaf'), '🌾': M('leaf'), '🍀': M('leaf'),
    '👤': M('users'), '👥': M('users'), '🫂': M('users'),
    '📊': M('pulse'), '📈': M('pulse'), '📉': M('pulse'),
    '▦': M('grid'), '🧩': M('grid'), '⊞': M('grid'),
    '🏠': M('home'), '🏡': M('home'),
    '🎁': M('gift'), '🎀': M('gift'),
    '🎵': M('music'), '🎶': M('music'), '♪': M('music'), '♬': M('music'), '♩': M('music'), '🎼': M('music'),
    '🎧': M('headphones'),
    '📺': M('tv'), '🎬': M('film'), '🎥': M('film'),
    '🎮': M('gamepad'), '🕹': M('gamepad'),
    '⛏': M('pickaxe'), '🔨': M('pickaxe'), '🪓': M('pickaxe'), '⚒': M('pickaxe'),
    '✏': M('pencil'), '📝': M('pencil'), '🖊': M('pencil'), '🖋': M('pencil'), '✍': M('pencil'),
    '📋': M('clipboard'), '📄': M('clipboard'), '📃': M('clipboard'),
    '👍': M('thumbUp'), '👎': M('thumbDown'),
    '❌': M('xCircle'), '🚫': M('xCircle'),
    '📤': M('upload'), '📥': M('download'),
    '↻': M('refresh'), '🔄': M('refresh'), '⟳': M('refresh'),
    '🧹': M('broom'), '🧽': M('broom'),
    '🎤': M('mic'), '🎙': M('mic'),
    '🚀': M('rocket'),
    '💬': M('message'), '💭': M('message'), '🗨': M('message'),
    '👁': M('eye'), '👀': M('eye'),
    '🎯': M('target'),
    '🔒': M('lock'), '🔐': M('lock'),
    '☀': M('sun'), '🌞': M('sun'),
    '📅': M('calendar'), '📆': M('calendar'), '🗓': M('calendar'),
    '✅': M('checkCircle'), '☑': M('checkCircle'), '✔': M('checkCircle'),
    '⚠': M('warn'), '❗': M('warn'),
    '🗑': M('trash'), '🧺': M('trash'),
    '💡': M('bulb'),
    '⏰': M('clock'), '⏱': M('clock'), '🕐': M('clock'),
    '🌐': M('globe'), '🌍': M('globe'),
    '💾': M('save'), '💿': M('save'),
    '🎨': M('palette'),
    '🏆': M('award'), '🏅': M('award'), '🥇': M('award'),
    '📌': M('pin'), '📍': M('pin'), '🔖': M('pin'),
    '🔔': M('bell'), '🔕': M('bell'),
    '🔍': M('search'), '🔎': M('search'),
    '📷': M('cam'), '📸': M('cam'), '🖼': M('cam'),
    '😤': M('flame'), '💢': M('flame'), '🔥': M('flame'),
  };

  const EMOJI_RE = /^([\s\u3000]*)([\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}]\uFE0F?)(.*)$/su;

  function sprite() {
    let svg = document.getElementById('hz-ic-sprite');
    if (svg) return;
    svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.id = 'hz-ic-sprite';
    svg.setAttribute('aria-hidden', 'true');
    svg.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
    let defs = '<defs>';
    for (const k in NEW) defs += '<symbol id="hz-ic-' + k + '" viewBox="0 0 24 24">' + NEW[k] + '</symbol>';
    defs += '</defs>';
    svg.innerHTML = defs;
    document.body.appendChild(svg);
  }

  function mark(icon) {
    const span = document.createElement('span');
    span.className = 'hz-ico';
    span.setAttribute('aria-hidden', 'true');
    if (icon.solid) {
      span.innerHTML = '<svg class="hz-ic solid" viewBox="0 0 24 24"><use href="#' + SOLID[icon.n] + '"/></svg>';
    } else {
      span.innerHTML = '<svg class="hz-ic" viewBox="0 0 24 24"><use href="#hz-ic-' + icon.n + '"/></svg>';
    }
    return span;
  }

  /* 把 root 内「以 emoji 开头、且整体很短」的文本换成图标 + 文字。
     限制长度是为了绝不改到聊天正文/信件内容。 */
  function iconify(root) {
    if (!root || root.nodeType !== 1) return 0;
    if (root.closest && root.closest('svg')) return 0;
    let n = 0;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const jobs = [];
    let node;
    while ((node = walker.nextNode())) {
      const txt = node.nodeValue;
      if (!txt || txt.length > 22) continue;
      const m = EMOJI_RE.exec(txt);
      if (!m) continue;
      const icon = MAP[m[2].replace(/\uFE0F/g, '')];
      if (!icon) continue;
      const rest = m[3];
      const parent = node.parentNode;
      if (!parent || parent.closest('svg')) continue;
      /* 只处理「图标位」：emoji 后面是空白、或紧跟中文，且剩余文字很短 */
      const restTrim = rest.replace(/^[\s\u3000]+/, '');
      if (rest && !/^[\s\u3000]/.test(rest) && !/^[\u4e00-\u9fff（(]/.test(rest)) continue;
      const isPath = /^[A-Za-z]:[\\/]/.test(restTrim) || restTrim.indexOf('\\') >= 0;
      if (restTrim.length > 14 && !isPath) continue;
      if (parent.querySelector && parent.querySelector(':scope > .hz-ico')) continue;
      jobs.push({ node, icon, text: restTrim });
    }
    for (const j of jobs) {
      const span = mark(j.icon);
      j.node.parentNode.insertBefore(span, j.node);
      j.node.nodeValue = j.text;
      n++;
    }
    return n;
  }

  window.hzIconify = iconify;

  /* ---------- 3. 自动补图标（增量，避免每次点击都全量扫描 DOM） ---------- */
  /* ---------- 3.4 页面架构重排：左侧导航轨 + 右侧工作区 ---------- */
  /* 把原项目页面里「工具栏型」的元素（分段控件/筛选 tabs/chips）抽到左侧竖排导轨，
     其余内容放右侧工作区。只移动节点、不复制，原有事件与引用全部保留；
     模块若重建了 innerHTML，下一次刷新会自动重新套用。 */
  const RAIL_SEL = '.seg, .mem-tabs, .chips';
  function relayout(section) {
    if (!section || section.dataset.wkSkip === '1') return;
    if (section.querySelector(':scope > .wk')) return;      // 已重排
    const kids = [...section.children];
    if (kids.length < 2) { section.dataset.wkSkip = '1'; return; }
    const rail = kids.filter((k) => k.matches && k.matches(RAIL_SEL));
    if (!rail.length) { section.dataset.wkSkip = '1'; return; }
    const wrap = document.createElement('div'); wrap.className = 'wk';
    const side = document.createElement('aside'); side.className = 'wk-side';
    const main = document.createElement('div'); main.className = 'wk-main';
    rail.forEach((e) => side.appendChild(e));
    kids.filter((k) => rail.indexOf(k) < 0).forEach((e) => main.appendChild(e));
    wrap.appendChild(side); wrap.appendChild(main);
    section.appendChild(wrap);
  }
  function relayoutActive() {
    relayout(document.querySelector('.hz-view.on .hz-vslot > section[id^="page-"]'));
  }

  let queue = [];
  let raf = 0;
  function flush() {
    raf = 0;
    const list = queue; queue = [];
    for (const n of list) {
      try { iconify(n); } catch (_) {}
    }
    try { relayoutActive(); } catch (_) {}
    for (const n of list) { try { tidyText(n); } catch (_) {} }
  }
  function scheduleNodes(nodes) {
    if (!nodes || !nodes.length) return;
    /* 合并到下一帧；限制单帧工作量，避免卡顿 */
    for (const n of nodes) {
      if (!n || n.nodeType !== 1) continue;
      if (n.id === 'hz-ic-sprite' || (n.classList && n.classList.contains('hz-ico'))) continue;
      if (n.closest && n.closest('#hz-ic-sprite')) continue;
      queue.push(n);
    }
    if (queue.length > 60) queue = queue.slice(-60);
    if (!raf) raf = requestAnimationFrame(flush);
  }
  function scheduleAll() { scheduleNodes([document.getElementById('stage'), document.getElementById('app') || document.body]); }

  /* ---------- 4. 用户昵称 / 伴侣头像 ---------- */
  /* 我的昵称存在后端 /api/user/profile（不是 Store.meNickname）→ 问候语一直显示兜底「朋友」。
     这里拉一次回填到 Store 与 #hzNick。 */
  function sidOf() {
    try {
      return (window.Session && Session.getSessionId && Session.getSessionId())
        || localStorage.getItem('ai_companion_session_id')
        || localStorage.getItem('session_id') || 'default';
    } catch (_) { return 'default'; }
  }
  async function syncMe() {
    let nick = '';
    try {
      const r = await fetch('/api/user/profile?session_id=' + encodeURIComponent(sidOf()) + '&character_id=default');
      if (r.ok) { const d = await r.json(); nick = ((d && d.nickname) || '').trim(); }
    } catch (_) {}
    /* 没填「我的资料-昵称」时，退回 TA 对你的称呼（call_user，例如「宝」），
       总比一直显示兜底的「朋友」自然。 */
    if (!nick && window.Store && Store.listContacts) {
      try {
        const c = (Store.listContacts() || [])[0];
        if (c) {
          const r2 = await fetch('/api/pc/character/get?name=' + encodeURIComponent(c.name));
          if (r2.ok) { const cfg = await r2.json(); nick = ((cfg && cfg.call_user) || '').trim(); }
        }
      } catch (_) {}
    }
    if (!nick) return;
    try { Store.saveSettings({ meNickname: nick }); } catch (_) {}
    const nk = document.getElementById('hzNick');
    if (nk) nk.textContent = nick;
  }

  /* 伴侣头像：总览主角卡原来是首字占位。这里按需回填（每个角色只查一次）。 */
  const avTried = {};
  async function syncAvatars() {
    if (!window.Store || !Store.listContacts) return;
    let changed = false;
    for (const c of (Store.listContacts() || [])) {
      if (!c || c.avatarUrl || c.avatar || avTried[c.name]) continue;
      avTried[c.name] = 1;
      try {
        const r = await fetch('/api/pc/character/get?name=' + encodeURIComponent(c.name));
        if (!r.ok) continue;
        const cfg = await r.json();
        const av = cfg && (cfg.avatar || cfg.avatarUrl || '');
        if (av) { Store.updateContact(c.id, { avatarUrl: av, avatar: av }); changed = true; }
      } catch (_) {}
    }
    if (changed) { try { window.hzRenderOverview && window.hzRenderOverview(); } catch (_) {} }
  }

  /* ---------- 5. 高级感动态：指针聚光 + 卡片微视差 ---------- */
  function premiumMotion() {
    const stage = document.getElementById('stage');
    if (!stage || stage.dataset.hzMotion) return;
    stage.dataset.hzMotion = '1';
    let spot = document.getElementById('hzSpot');
    if (!spot) { spot = document.createElement('div'); spot.id = 'hzSpot'; stage.appendChild(spot); }
    let raf = 0, tx = 0.5, ty = 0.3;
    stage.addEventListener('mousemove', (e) => {
      const r = stage.getBoundingClientRect();
      tx = Math.max(0, Math.min(1, (e.clientX - r.left) / Math.max(1, r.width)));
      ty = Math.max(0, Math.min(1, (e.clientY - r.top) / Math.max(1, r.height)));
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        spot.style.setProperty('--mx', (tx * 100).toFixed(1) + '%');
        spot.style.setProperty('--my', (ty * 100).toFixed(1) + '%');
        document.querySelectorAll('#hzDeck .dcard, #hzBento .tile').forEach((el, i) => {
          const d = (i % 3 + 1) * 2.4;
          el.style.setProperty('--px', ((tx - .5) * d).toFixed(2) + 'px');
          el.style.setProperty('--py', ((ty - .5) * d).toFixed(2) + 'px');
        });
      });
    });
    stage.addEventListener('mouseleave', () => {
      document.querySelectorAll('#hzDeck .dcard, #hzBento .tile').forEach((el) => {
        el.style.setProperty('--px', '0px'); el.style.setProperty('--py', '0px');
      });
    });
  }

  /* ---------- 6. 真实相识天数（后端 relationship created_at） ---------- */
  window.__hzTenure = window.__hzTenure || {};
  async function syncRelStats() {
    if (!window.Store || !Store.listContacts) return;
    const cs = Store.listContacts() || [];
    let changed = false;
    for (const c of cs.slice(0, 4)) {
      if (!c || window.__hzTenure[c.name]) continue;
      try {
        const r = await fetch('/api/relationship/state?session_id=' + encodeURIComponent(sidOf())
          + '&character_id=' + encodeURIComponent(c.name));
        if (!r.ok) continue;
        const st = await r.json();
        /* ★ 2026-09-15：相识天数优先用后端算好的 known_days（口径 = 角色卡 created_at），
           它才是真正的"认识多久"。原来的 st.created_at 是关系记录行的创建时间，重打包/
           换会话被重建后就变成当天 → 总览显示"相识 1 天"。 */
        const ca = st && (st.known_days != null ? null : st.created_at);
        if (st && Number(st.known_days) > 0) {
          window.__hzTenure[c.name] = Number(st.known_days);
          changed = true;
          continue;
        }
        if (!ca) continue;
        const d0 = new Date(String(ca).replace(' ', 'T'));
        if (isNaN(d0.getTime())) continue;
        window.__hzTenure[c.name] = Math.max(1, Math.floor((Date.now() - d0.getTime()) / 864e5) + 1);
        changed = true;
      } catch (_) {}
    }
    if (changed) { try { window.hzRenderOverview && window.hzRenderOverview(); } catch (_) {} }
  }

  /* ---------- 7. 视图滚动视差 ---------- */
  function scrollParallax() {
    const st = document.getElementById('stage');
    if (!st || st.dataset.hzSy) return;
    st.dataset.hzSy = '1';
    document.addEventListener('scroll', (e) => {
      const t = e.target;
      if (!t || !t.classList || !t.classList.contains('hz-vslot')) return;
      const v = t.closest('.hz-view');
      if (!v) return;
      v.style.setProperty('--sy', Math.min(26, t.scrollTop * 0.06).toFixed(1) + 'px');
    }, true);
  }

  /* ---------- 8. Esc 关弹层 + 点空白关聊天 ---------- */
  function sheetObj() { try { return (0, eval)('typeof Sheet!=="undefined"?Sheet:null') || window.Sheet; } catch (_) { return null; } }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    try {
      const s = document.getElementById('sheet');
      const S = sheetObj();
      if (s && s.classList.contains('show') && S && S.close) S.close();
    } catch (_) {}
  });
  /* 点聊天抽屉之外的空白 → 关掉聊天（原来只能点左上「‹」，容易以为退不回去） */
  document.addEventListener('click', (e) => {
    const page = document.getElementById('chat-page');
    const panel = document.getElementById('chatpanel');
    const chatOpen = (page && page.classList.contains('open')) || (panel && panel.classList.contains('open'));
    if (!chatOpen || !e.target || !e.target.closest) return;
    const keep = '#chat-page,.chatpanel,.scrim,.topbar,.dock,#hzFan,#hzFab,.deck,.bento,.ov-grid,.overlay.open,.sheet,.mask';
    if (e.target.closest(keep)) return;
    try {
      if (window.Chat && window.Chat.close) window.Chat.close();
      if (typeof window.closeHzChat === 'function') window.closeHzChat();
    } catch (_) {}
  }, true);

  /* ---------- 9. 聊天窗：拖出独立窗口 + 抽屉宽度可调并记忆 ---------- */
  const QS = new URLSearchParams(location.search);
  const IS_POPOUT = QS.get('popout') === 'chat';
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /* 独立窗口模式：只显示聊天，并把「‹」变成关窗 */
  async function bootPopout() {
    document.body.classList.add('popout-chat');
    for (let i = 0; i < 80 && !(window.Store && window.Chat); i++) await sleep(200);
    let id = QS.get('contact') || '';
    const nm = QS.get('name') || '';
    try {
      if (!id && nm) { const c = (Store.listContacts() || []).find((x) => x.name === nm); id = c && c.id; }
      if (!id) { const c = (Store.listContacts() || [])[0]; id = c && c.id; }
      if (id) { if (window.hzOpenChat) window.hzOpenChat(id); else Chat.open(id); }
    } catch (_) {}
    const back = document.getElementById('chat-back');
    if (back) back.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      try { window.desktopWin && window.desktopWin.closeChatWindow(); } catch (_) {}
    };
  }

  function doPopout() {
    const c = window.Chat && window.Chat.contact;
    try {
      window.desktopWin && window.desktopWin.openChatWindow({ contactId: (c && c.id) || '', name: (c && c.name) || '' });
    } catch (_) {}
    try { window.Chat && window.Chat.close && window.Chat.close(); } catch (_) {}
  }

  function chatWindowExtras() {
    if (IS_POPOUT) return;
    const page = document.getElementById('chat-page');
    const header = page && page.querySelector('.chat-header');
    if (!page || page.dataset.hzPop) return;
    page.dataset.hzPop = '1';

    /* 头部加「弹出为独立窗口」按钮 */
    if (header && !document.getElementById('chat-popout')) {
      const btn = document.createElement('button');
      btn.id = 'chat-popout'; btn.type = 'button';
      btn.title = '拖出为独立窗口（也可按住标题往外拖）'; btn.textContent = '⧉';
      const more = header.querySelector('#chat-more');
      header.insertBefore(btn, more || null);
      btn.addEventListener('click', (e) => { e.stopPropagation(); doPopout(); });
    }

    /* 按住标题横/纵向拖 >140px → 弹出 */
    if (header) {
      let sx = 0, sy = 0, dragging = false;
      header.addEventListener('mousedown', (e) => {
        if (e.target.closest('button')) return;
        dragging = true; sx = e.clientX; sy = e.clientY;
      });
      window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        if (Math.abs(e.clientX - sx) > 140 || Math.abs(e.clientY - sy) > 140) { dragging = false; doPopout(); }
      });
      window.addEventListener('mouseup', () => { dragging = false; });
    }

    /* 左缘拖拽改宽度，宽度记进 localStorage */
    const saved = parseInt(localStorage.getItem('hzChatW') || '', 10);
    if (saved > 300) page.style.width = Math.min(saved, window.innerWidth - 60) + 'px';
    const grip = document.createElement('div');
    grip.className = 'hz-chat-resize';
    grip.title = '拖动调整宽度（会自动记住）';
    page.appendChild(grip);
    grip.addEventListener('mousedown', (e) => {
      e.preventDefault(); e.stopPropagation();
      const sx0 = e.clientX, w0 = page.getBoundingClientRect().width;
      document.body.classList.add('hz-resizing');
      const mv = (ev) => {
        const w = Math.max(320, Math.min(window.innerWidth - 60, w0 + (sx0 - ev.clientX)));
        page.style.width = w + 'px';
      };
      const up = () => {
        document.removeEventListener('mousemove', mv);
        document.removeEventListener('mouseup', up);
        document.body.classList.remove('hz-resizing');
        try { localStorage.setItem('hzChatW', String(Math.round(page.getBoundingClientRect().width))); } catch (_) {}
      };
      document.addEventListener('mousemove', mv);
      document.addEventListener('mouseup', up);
    });
  }

  /* 窄窗（独立聊天窗）里输入框放不下长提示语，自动换短的 */
  function shortPlaceholder() {
    var ta = document.getElementById('chat-input');
    if (ta && ta.placeholder !== '请输入消息') ta.placeholder = '请输入消息';
  }

  /* ---------- 10. 细节：ISO 时间戳格式化 + 她们页整行可点 ---------- */
  const ISO_RE = /\b(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):\d{2}(?:\.\d+)?Z?/g;
  function tidyText(root) {
    if (!root || root.nodeType !== 1) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const jobs = [];
    let n;
    while ((n = walker.nextNode())) {
      const t = n.nodeValue;
      if (!t || t.indexOf("T") < 0 || t.length > 200) continue;
      const m = t.match(ISO_RE);
      if (!m) continue;
      jobs.push([n, t.replace(ISO_RE, "$1-$2-$3 $4:$5")]);
    }
    jobs.forEach(([node, val]) => { node.nodeValue = val; });
  }
  /* 她们页：模块自己的行点击在某些情况下不触发，这里做一次事件委托兜底 */
  document.addEventListener("click", (e) => {
    const row = e.target && e.target.closest && e.target.closest("#page-persona .row");
    if (!row) return;
    if (e.target.closest(".pc-contact-settings")) return;
    const nameEl = row.querySelector(".row-label > div:first-child");
    const name = nameEl && nameEl.textContent.trim();
    if (!name) return;
    let c = null;
    try { c = window.Store && Store.getContact(name); } catch (_) {}
    if (!c) return;
    e.stopPropagation();
    try { if (window.hzOpenChat) window.hzOpenChat(c.id); else if (window.Chat) Chat.open(c.id); } catch (_) {}
  }, true);

  function boot() {
    sprite();
    scheduleAll();
    /* 记录真实 Chat.open 的异常：新壳层缺 DOM 时最容易在这里炸，
       一炸 hzOpenChat 就退到兜底面板（用户会觉得「聊天不对劲/退不回去」）。 */
    try {
      const C = window.Chat;
      if (C && typeof C.open === 'function' && !C.open.__hzWrapped) {
        const o = C.open;
        C.open = function () {
          try { return o.apply(this, arguments); }
          catch (e) {
            window.__hzChatOpenErr = e.message + ' | ' + String(e.stack || '').split('\n').slice(0, 5).join(' <- ');
            try { window.desktopWin && window.desktopWin._sendDebug && window.desktopWin._sendDebug('[hz7] Chat.open 异常: ' + window.__hzChatOpenErr); } catch (_) {}
            throw e;
          }
        };
        C.open.__hzWrapped = true;
      }
    } catch (_) {}
    /* 只处理「新增节点」，不再每次点击/键盘全量扫描（那是输入卡顿的主因） */
    try {
      const obs = new MutationObserver((recs) => {
        for (const r of recs) {
          if (r.type === 'childList' && r.addedNodes && r.addedNodes.length) scheduleNodes(r.addedNodes);
        }
      });
      obs.observe(document.body, { childList: true, subtree: true });
    } catch (_) {}
    /* 视图切换后，只对该视图子树补一次图标 */
    const orig = window.hzShowView;
    if (typeof orig === 'function' && !orig.__hzIco) {
      const wrapped = function () {
        const r = orig.apply(this, arguments);
        const name = arguments[0];
        if (name) {
          try { document.querySelectorAll('.hz-fan .f-it').forEach((b) => b.classList.toggle('on', b.dataset.v === name)); } catch (_) {}
        }
        setTimeout(() => {
          const v = document.querySelector('.hz-view.on');
          if (v) scheduleNodes([v]);
          try { relayoutActive(); } catch (_) {}
        }, 80);
        return r;
      };
      wrapped.__hzIco = true;
      wrapped.__hzOrig = orig;
      window.hzShowView = wrapped;
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(runAll, 450));
  else setTimeout(runAll, 450);

  function runAll() {
    if (IS_POPOUT) {
      boot();
      setTimeout(bootPopout, 300);
      setTimeout(shortPlaceholder, 800);
      window.addEventListener('resize', shortPlaceholder);
      return;
    }
    boot();
    setTimeout(() => {
      try { syncMe(); syncAvatars(); syncRelStats(); premiumMotion(); scrollParallax(); chatWindowExtras(); shortPlaceholder(); } catch (_) {}
    }, 700);
    /* 回到前台时再补一次（用户在设置里改了昵称/头像） */
    window.addEventListener('focus', () => { try { syncMe(); syncAvatars(); syncRelStats(); } catch (_) {} });
    window.addEventListener('resize', shortPlaceholder);
    /* 设置页保存「我的资料」后立即刷新问候语 */
    window.addEventListener('hz-me-saved', () => { try { syncMe(); } catch (_) {} });
  }
})();
