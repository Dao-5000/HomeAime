'use strict';
/* ============================================================
   AI 伴侣 · 入口与公共工具
   ============================================================ */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 按好感度 + 当前语境决定分段范围；高好感只是“有机会”多说，不会每次刷屏。
function dynamicSegmentRange(contact, text, baseMin, baseMax) {
  const affection = Math.max(0, Math.min(100, Number(contact?.affection ?? 50) || 0));
  const raw = String(text || '');
  const restrained = raw.length <= 8 || /怎么办|原因|报错|失败|解释|工作|学习/.test(raw);
  const warm = /想你|喜欢你|爱你|抱抱|谢谢你|好开心|生日|纪念/.test(raw);
  let lo = Math.max(1, Math.round(Number(baseMin) || 1));
  let hi = Math.max(lo, Math.round(Number(baseMax) || 3));
  if (affection >= 100) {
    // ★ 好感拉满：不限制段数（保留 12 护栏防极端刷屏），让 AI 自由发挥延续话题
    if (restrained) { lo = Math.max(lo, 2); hi = Math.max(hi, 4); }
    else { lo = Math.max(lo, 4); hi = Math.max(hi, 12); }
  } else if (affection >= 95) {
    if (!restrained && (warm || Math.random() < 0.4)) { lo = Math.max(lo, 4); hi = Math.max(hi, 7); }
    else { hi = Math.max(hi, 5); }
  } else if (affection >= 85) {
    hi = Math.max(hi, restrained ? 4 : 6);
  } else if (affection >= 70) {
    hi = Math.max(hi, restrained ? 3 : 5);
  } else if (affection >= 55) {
    hi = Math.max(hi, restrained ? 2 : 4);
  }
  return [lo, hi];
}

// 判断文本是否为纯沉默内容（纯省略号/纯标点/空白），用于过滤 AI 生成的无效气泡/语音。
function isSilenceText(t) {
  return !String(t || '').replace(/[\s…\.。·~～\-—_,，、;；:：!！?？'"“”‘’()（）\[\]【】]/g, '').length;
}

/** 创建 DOM 元素：h('div', {class:'x'}, child1, child2...)
 *  支持数组 child（如 arr.map(...) 直接传入），递归展开，避免 appendChild 非 Node 崩溃 */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
  }
  const append = (c) => {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(append); return; }
    el.appendChild(typeof c === 'string' || typeof c === 'number'
      ? document.createTextNode(String(c)) : c);
  };
  children.forEach(append);
  return el;
}

/* ---------------- 时间格式化 ---------------- */
const pad2 = (n) => String(n).padStart(2, '0');

function fmtClock(ts) {
  const d = new Date(ts);
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

function fmtListTime(ts) {
  const d = new Date(ts);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (ts >= startOfToday) return fmtClock(ts);
  if (ts >= startOfToday - 86400000) return '昨天';
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  return d.getFullYear() + '/' + (d.getMonth() + 1) + '/' + d.getDate();
}

function fmtChatTime(ts) {
  const d = new Date(ts);
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  let prefix;
  if (ts >= startOfToday) prefix = '今天';
  else if (ts >= startOfToday - 86400000) prefix = '昨天';
  else if (d.getFullYear() === now.getFullYear()) prefix = (d.getMonth() + 1) + '月' + d.getDate() + '日';
  else prefix = d.getFullYear() + '年' + (d.getMonth() + 1) + '月' + d.getDate() + '日';
  return prefix + ' ' + fmtClock(ts);
}

function dateStrOf(ts) {
  const d = new Date(ts);
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/* ---------------- 头像 ---------------- */
function avatarHue(name) {
  let sum = 0;
  for (let i = 0; i < name.length; i++) sum += name.charCodeAt(i);
  return sum % 360;
}

function avatarEl(contact, sizeClass) {
  const el = h('div', { class: 'avatar ' + (sizeClass || '') });
  const avatar = contact && (contact.avatarUrl || contact.avatar || '');
  if (avatar) {
    el.appendChild(h('img', { src: avatar, alt: '' }));
  } else {
    const name = contact ? (contact.name || '?') : '?';
    el.style.background = 'hsl(' + avatarHue(name) + ', 55%, 58%)';
    el.style.color = '#fff';
    el.style.fontWeight = '600';
    el.style.fontSize = (sizeClass === 'sm' ? 16 : 20) + 'px';
    el.textContent = name.charAt(0);
  }
  return el;
}

/* ---------------- 轻提示 ---------------- */
let toastTimer = null;
function toast(msg, ms) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), ms || 2200);
}

/* ★ 本地大脑提示（2026-09-11）：后端在本地 Ollama 没响应自动临时切回云端时，
 * 通过 SSE 发 notice 事件，chat.js / chat_stream.js 广播到这里统一弹 toast，
 * 让用户知道"这条是云端答的"，避免对回复质量变化感到困惑 */
window.addEventListener('brain-notice', (e) => {
  const msg = e && e.detail ? String(e.detail) : '';
  if (msg) toast(msg, 4000);
});

/* ---------------- 括号动作描写剥离（全局公共） ----------------
 * 「括号动作描写」开关关闭时（contact.actions === false），所有 AI 生成文字的出口
 * 都必须走本函数，保证行为一致：普通聊天 / 后端多段气泡 / 主动消息 / 语音转写。
 * 之前各分支各写一份正则、主动消息分支干脆没写，导致开关关了照样看到括号描写。
 * 注意：app.js 在 chat.js 之前加载，chat.js 可直接调用。
 */
function stripBracketActions(text) {
  if (!text) return '';
  return String(text)
    .replace(/[（(][^（()）]*[）)]/g, '')
    .replace(/[（(][^（()）]*[）)]/g, '')   // 第二遍：处理相邻/嵌套括号残留
    .replace(/\*{1,2}[^*\n]{1,40}\*{1,2}/g, '')   // ★ 星号动作描写（*愣了一下，语气有点懵* / **...**）
    .replace(/\*+/g, '')   // 清理残留孤立星号
    .replace(/\s{2,}/g, ' ')
    .trim();
}

/* ---------------- 应用内消息到达通知（唯一弹窗） ----------------
 * 触发（统一交给 NotifyCore.shouldShowInAppNotify 判定，单一真相源、可单测）：
 *   - 应用「聚焦」才弹；最小化 / 正在用其他应用 → 不弹（不打扰，满足需求 #2）
 *   - 该会话已打开 → 不弹（消息已可见，避免重复提醒）
 *   - 联系人在 quiet 免打扰时段 → 不弹
 * 显示：主窗口右上角卡片（头像 + 名称 + 预览），悬停时暂停自动隐藏。
 * 关闭：① 点击卡片 → 打开会话并关闭；② 右上角 ✕ → 仅关闭；③ 5 秒无交互自动关闭。
 */
let inAppHideTimer = null;
function inAppHide() {
  const el = document.getElementById('inapp-notify');
  if (el) el.classList.remove('show');
}
/* 窗口是否前台聚焦：优先用 Electron 主进程的 mainWindow.isFocused()（准确），
 * 退回 document.hasFocus()（devtools/子元素聚焦时不可靠），最后用 visibilityState 兜底（最小化→hidden）。
 */
/* ---------- 调试日志：渲染进程console.log通过IPC发给主进程写桌面文件 ---------- */
(function() {
  const origLog = console.log.bind(console);
  const origErr = console.error.bind(console);
  function sendLog(prefix, args) {
    const msg = prefix + ' ' + args.map(a => {
      if (typeof a === 'object') { try { return JSON.stringify(a); } catch(_) { return String(a); } }
      return String(a);
    }).join(' ');
    try {
      if (window.desktopWin && window.desktopWin._sendDebug) {
        window.desktopWin._sendDebug(msg);
      } else if (window.desktopWin && window.desktopWin.showNotify) {
        // 用ipcRenderer.send的方式，但preload没暴露debug-log，用其他方式
      }
    } catch(_) {}
  }
  console.log = function(...args) { sendLog('[RENDER]', args); origLog(...args); };
  console.error = function(...args) { sendLog('[RENDER ERROR]', args); origErr(...args); };
})();

/* 窗口聚焦/最小化状态：由主进程通过 IPC 同步，比渲染进程自己监听 focus/blur 更可靠
   （Electron 窗口最小化不触发 blur，渲染进程自己猜会出错） */
let _winFocusedState = true;   // 初始保守值，主进程启动后会立即推送真实状态
let _winMinimizedState = false;
if (window.desktopWin && typeof window.desktopWin.onWindowState === 'function') {
  try {
    window.desktopWin.onWindowState((state) => {
      console.log('[DEBUG onWindowState] received state:', JSON.stringify(state));
      if (state && typeof state.focused === 'boolean') _winFocusedState = state.focused;
      if (state && typeof state.minimized === 'boolean') _winMinimizedState = state.minimized;
      console.log('[DEBUG onWindowState] updated _winFocusedState=', _winFocusedState, '_winMinimizedState=', _winMinimizedState);
    });
  } catch (_) {}
}

function _winFocused() {
  // 优先用主进程同步的权威状态
  if (window.desktopWin && window.desktopWin.isDesktop) {
    return _winFocusedState && !_winMinimizedState;
  }
  // 非桌面端兜底
  try {
    if (window.desktopWin && typeof desktopWin.isFocused === 'function') return !!desktopWin.isFocused();
  } catch (_) {}
  if (typeof document.hasFocus === 'function' && document.hasFocus()) return true;
  return document.visibilityState === 'visible';
}
function inAppNotify(contact, previewText, force) {
  console.log('[DEBUG inAppNotify] called, contact=', contact?.name, 'preview=', String(previewText||'').slice(0,50), 'force=', force);
  console.log('[DEBUG inAppNotify] _winFocusedState=', _winFocusedState, '_winMinimizedState=', _winMinimizedState, '_winFocused()=', _winFocused());
  console.log('[DEBUG inAppNotify] desktopWin=', !!window.desktopWin, 'isDesktop=', window.desktopWin?.isDesktop, 'showNotify=', !!(window.desktopWin && window.desktopWin.showNotify));
  if (!contact) { console.log('[DEBUG inAppNotify] BLOCKED: no contact'); return; }

  // force=true（测试按钮）：跳过所有检查，直接弹桌面通知
  if (force && window.desktopWin && window.desktopWin.showNotify) {
    try {
      console.log('[DEBUG inAppNotify] force=true, CALLING desktopWin.showNotify directly');
      window.desktopWin.showNotify({
        name: contact.name || 'AI',
        content: previewText || '',
        avatar: contact.avatarUrl || '',
        contact_id: contact.id,
        _test: true,
      });
    } catch (e) { console.error('[DEBUG inAppNotify] force showNotify error:', e); }
    return;
  }

  const NC = window.NotifyCore;
  if (!force) {
    const chatPageOpen = !!(document.getElementById('chat-page') && document.getElementById('chat-page').classList.contains('open'));
    const conversationOpen = chatPageOpen && !!(window.Chat && Chat.contact && Chat.contact.id === contact.id);
    const focused = _winFocused();
    const globalDnd = !!(NC && NC.isGlobalDnd && NC.isGlobalDnd(window.__pcConfig || {}));
    if (globalDnd) return;
    // 桌面端：窗口未聚焦 或 已最小化 → 弹系统通知窗
    // （主进程权威同步状态，最小化时 _winFocused 已返回 false，这里加 _winMinimizedState 双保险）
    console.log('[DEBUG inAppNotify] desktop condition: isDesktop=', window.desktopWin?.isDesktop, '!focused=', !focused, '_winMinimizedState=', _winMinimizedState, 'willShow=', window.desktopWin?.isDesktop && (!focused || _winMinimizedState));
    if (window.desktopWin && window.desktopWin.isDesktop && (!focused || _winMinimizedState)) {
      try {
        const avatar = contact.avatarUrl || '';
        console.log('[DEBUG inAppNotify] CALLING desktopWin.showNotify');
        window.desktopWin.showNotify({
          name: contact.name || 'AI',
          content: previewText || '',
          avatar: avatar,
          contact_id: contact.id,
        });
      } catch (e) { console.error('[DEBUG inAppNotify] showNotify error:', e); }
      return;
    }
    if (NC && NC.shouldShowInAppNotify) {
      if (!NC.shouldShowInAppNotify({ contact, focused, conversationOpen, globalDnd })) return;
    } else if (!focused || conversationOpen || (NC && NC.isInQuiet(contact))) {
      return;
    }
  }
  const el = document.getElementById('inapp-notify');
  if (!el) return;
  const nameEl = document.getElementById('inAppName');
  const textEl = document.getElementById('inAppText');
  const avEl = document.getElementById('inAppAvatar');
  let avMode = 'text', avVal = (contact.name || 'AI').charAt(0);
  if (NC) {
    const av = NC.resolveAvatar({ name: contact.name || '', avatar: contact.avatarUrl || '', isAI: true });
    avMode = av.mode; avVal = av.value;
  }
  if (avMode === 'img') avEl.innerHTML = '<img src="' + avVal + '">';
  else avEl.textContent = avVal;
  nameEl.textContent = contact.name || 'Homeaime';
  textEl.textContent = previewText || '';
  el.classList.add('show');
  el.onclick = () => {
    try { if (typeof Chat !== 'undefined' && Chat.open) Chat.open(contact.id); } catch (_) {}
    inAppHide();
  };
  const closeBtn = document.getElementById('inAppClose');
  if (closeBtn) {
    closeBtn.onclick = (e) => { e.stopPropagation(); inAppHide(); };
  }
  el.onmouseenter = () => { clearTimeout(inAppHideTimer); };
  el.onmouseleave = () => { clearTimeout(inAppHideTimer); inAppHideTimer = setTimeout(() => inAppHide(), 2500); };
  clearTimeout(inAppHideTimer);
  inAppHideTimer = setTimeout(inAppHide, 5000);
}

/* ---------------- 主窗口恢复时从 Store 同步当前聊天 ---------------- */
function syncCurrentChatFromStore() {
  try {
    if (typeof Chat === 'undefined' || !Chat.contact || !Chat.conv) return;

    const page = document.getElementById('chat-page');
    if (!page || !page.classList.contains('open')) return;

    // 必须真的已经恢复并聚焦
    if (typeof _winFocused === 'function' && !_winFocused()) return;

    // 用户已经正在看当前聊天，因此清未读
    Store.markRead(Chat.conv.id);

    // 关键：重新从 Store 生成整个聊天 DOM
    Chat.rerender();

    if (typeof renderChatList === 'function') {
      renderChatList();
    }

    console.log('[syncCurrentChatFromStore] synced:', Chat.contact.id);
  } catch (e) {
    console.error('[syncCurrentChatFromStore] failed:', e);
  }
}

/* ---------------- 全屏进度层（导入/提取时必现） ---------------- */
const ProgressUI = {
  timer: null,
  start: 0,
  show(stage, label) {
    const ov = $('#progress-overlay');
    if (!ov) return;
    ov.classList.remove('hidden');
    $('#progress-stage').textContent = stage || '处理中…';
    $('#progress-label').textContent = label || '';
    this.start = Date.now();
    $('#progress-time').textContent = '已等待 0 秒';
    clearInterval(this.timer);
    this.timer = setInterval(() => {
      $('#progress-time').textContent = '已等待 ' + Math.round((Date.now() - this.start) / 1000) + ' 秒';
    }, 1000);
  },
  update(stage, label) {
    if (stage !== undefined) $('#progress-stage').textContent = stage;
    if (label !== undefined) $('#progress-label').textContent = label;
  },
  hide() {
    clearInterval(this.timer);
    this.timer = null;
    const ov = $('#progress-overlay');
    if (ov) ov.classList.add('hidden');
  },
};

/* ---------------- SVG 线条图标库 ---------------- */
const ICONS = {
  home: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.8V21h14V9.8"/></svg>',
  user: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c0-4 3.4-6 7.5-6s7.5 2 7.5 6"/></svg>',
  layers: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M12 3 2.5 8 12 13l9.5-5L12 3z"/><path d="M2.5 13 12 18l9.5-5"/><path d="M2.5 17.5 12 22.5l9.5-5"/></svg>',
  journal: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="6" y="3" width="12" height="18" rx="2"/><path d="M10 3v18"/><path d="M14.5 8h1.5M14.5 12h1.5"/></svg>',
  list: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8.5 6h11.5M8.5 12h11.5M8.5 18h11.5"/><circle cx="4" cy="6" r="1.1" fill="currentColor"/><circle cx="4" cy="12" r="1.1" fill="currentColor"/><circle cx="4" cy="18" r="1.1" fill="currentColor"/></svg>',
  grid: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.9 8.9 0 0 1-3.9-.8L3 21l1.9-5.2A8.4 8.4 0 1 1 21 11.5z"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>',
  heart: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21C7 16.5 3 13 3 8.8 3 6 5.2 4 7.8 4c1.7 0 3.2.9 4.2 2.3C13 4.9 14.5 4 16.2 4 18.8 4 21 6 21 8.8c0 4.2-4 7.7-9 12.2z"/></svg>',
  info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 7.5v.01"/></svg>',
  gear: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5h.1a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9v.1a1.7 1.7 0 0 0 1.5 1h.1a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
};

/** 图标元素（size 默认 22） */
function iconSvg(name, size) {
  const el = document.createElement('span');
  el.className = 'ico';
  el.style.width = (size || 22) + 'px';
  el.style.height = (size || 22) + 'px';
  el.innerHTML = ICONS[name] || '';
  return el;
}

/** 空状态图标 */
function emptyIcon(name) {
  return h('div', { class: 'empty-avatar' }, iconSvg(name, 52));
}

/* ---------------- 底部弹层 ---------------- */
const Sheet = {
  open(content, className) {
    const sheet = $('#sheet');
    sheet.innerHTML = '';
    sheet.appendChild(content);
    sheet.className = 'sheet' + (className ? ' ' + className : '');
    requestAnimationFrame(() => {
      sheet.classList.add('show');
      $('#sheet-mask').classList.add('show');
    });
  },
  close() {
    $('#sheet').classList.remove('show');
    $('#sheet-mask').classList.remove('show');
    setTimeout(() => { $('#sheet').innerHTML = ''; }, 250);
  },
};

$('#sheet-mask').addEventListener('click', () => Sheet.close());

/* ---------------- 标签页切换（概览/人格/记忆/生活/记录/功能） ---------------- */
let currentPage = 'overview';
const PAGE_TITLES = { overview: '概览', persona: '人格', memory: '记忆', life: '生活', moments: '朋友圈', companion: '陪伴', records: '记录', features: '功能' };

function switchTab(name) {
  // ★ 先关掉可能开着的底部弹层：Sheet 打开时它的遮罩 #sheet-mask 会盖住整屏，
  //   切到别的页面后遮罩不会自动消失，新页面会被它吃掉所有点击
  //   （表现为"这一页什么都点不了"）。
  try { Sheet.close(); } catch (_) {}
  // ★ 关闭所有覆盖层（设置/资料/聊天），防止页面重叠；字段多为 change 即存，已自动保存
  $$('.overlay').forEach((o) => {
    o.classList.remove('open');
    if (o.id === 'chat-page') {
      o.classList.add('closed');
      o.style.background = '';   // 顺带清除聊天壁纸，避免背景图残留到其他页面
    }
  });
  // ★ 通话记录面板(call_history_panel.js)没有 overlay 类，切页时也要清掉。
  //   否则它的半透明背景会留在最上层，挡住新页面所有点击（表现为"什么都点不了"）。
  const ch = document.getElementById('call-history-overlay');
  if (ch) { ch.classList.remove('show'); setTimeout(() => ch.remove(), 300); }
  currentPage = name;
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.page === name));
  $$('.page').forEach((p) => p.classList.toggle('active', p.id === 'page-' + name));
  const _tt = $('#topbar-title'); if (_tt) _tt.textContent = PAGE_TITLES[name];
  const _tr = $('#topbar-right'); if (_tr) _tr.innerHTML = '';
  if (name === 'overview') renderOverview();
  if (name === 'persona') renderContacts();
  if (name === 'memory') renderMemory();
  if (name === 'life') renderLife();
  if (name === 'moments') renderMoments();
  if (name === 'companion') renderCompanion();
  if (name === 'records') renderRecords();
  if (name === 'features') renderFeatures();
}

$$('.tab').forEach((t) => t.addEventListener('click', () => switchTab(t.dataset.page)));

/* ---------------- 消息渲染 ---------------- */
function timeDivider(ts) {
  const now  = Date.now();
  const diff = now - ts;
  const d    = new Date(ts);
  const pad  = (n) => String(n).padStart(2, '0');
  let label;

  if (diff < 60 * 1000) {
    label = '刚刚';
  } else if (diff < 60 * 60 * 1000) {
    label = Math.floor(diff / 60000) + ' 分钟前';
  } else if (diff < 24 * 60 * 60 * 1000) {
    // 今天：只显示时间
    label = pad(d.getHours()) + ':' + pad(d.getMinutes());
  } else if (diff < 48 * 60 * 60 * 1000) {
    // 昨天
    label = '昨天 ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  } else {
    // 更早：月日+时间
    label = (d.getMonth() + 1) + '月' + d.getDate() + '日 '
          + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  return h('div', { class: 'time-divider', text: label });
}

/* ---------------- 亲密度系统 ---------------- */
function intimacyInfo(c) {
  const v = c.intimacy == null
    ? (Number(c.relationLevel) || 5) * 10
    : Math.min(100, Math.max(0, Number(c.intimacy)));
  const idx = Math.min(9, Math.max(0, Math.floor(v / 10)));
  return { v: Math.round(v * 10) / 10, name: CLOSENESS_LEVELS[idx], pct: Math.round(v) };
}

/** 聊天后亲密度增长 */
function updateIntimacy(c, textLen) {
  // 后端 relationship_state 负责按语义、每日上限和角色隔离计算。
  // 前端不再自行累加，避免与后端双轨叠加；保留函数兼容旧调用点。
  return intimacyInfo(c).v;
}

/** 长期不联系，亲密度缓慢回落 */
function decayIntimacy(c) {
  // 衰减由后端按自然日幂等结算；这里不再每次打开日志重复扣分。
  return intimacyInfo(c).v;
}

/* ---------------- 组装系统提示词（多层记忆 + 时间感知 + 亲密度） ---------------- */
function composeSystem(c) {
  const parts = [];
  // ====== 人设核心（最高优先级，提到顶部，所有后续约束都建立在这上面） ======
  // 用户在「资料与设置」填的字段，构成 TA 的身份锚点——AI 必须按这个角色去演
  const personaLines = [];
  if (c.traits && c.traits.trim()) {
    // 把每个 trait 翻译成"行为描述"，不只是裸标签词（让 AI 真学会怎么演）
    const traitList = c.traits.split(/[、,，]/).map((s) => s.trim()).filter(Boolean);
    const traitDescs = traitList.map((t) => {
      if (typeof TRAIT_DETAILS !== 'undefined' && TRAIT_DETAILS[t]) {
        return '· ' + t + '：' + TRAIT_DETAILS[t];
      }
      return '· ' + t + '（用户自定义的性格词，请按这个词的气质来演）';
    });
    personaLines.push('【性格特点（每条都要演出来，不能只挂着名字）】\n' + traitDescs.join('\n'));
  }
  if (c.hobbies && c.hobbies.trim()) personaLines.push('【爱好】' + c.hobbies.trim());
  if (c.background && c.background.trim()) personaLines.push('【背景故事】' + c.background.trim());
  const styleKeys = (c.style || '').split(',').map((s) => s.trim()).filter(Boolean);
  for (const k of styleKeys) {
    if (k === 'custom') {
      if (c.customStyle && c.customStyle.trim()) personaLines.push('【自定义说话风格】' + c.customStyle.trim());
    } else {
      const def = STYLES[k];
      if (def && def.text) personaLines.push('【说话风格】' + def.text);
    }
  }
  if (c.system && c.system.trim()) personaLines.push('【用户额外补充的设定（必须遵守）】' + c.system.trim());
  if (personaLines.length) {
    parts.push('▼ 你是谁（最高优先级，所有行为都从这里出发）▼\n' + personaLines.join('\n'));
  }

  // ▼ 对话行为规则（仅次于人设核心，第二优先级，全程硬性遵守） ▼
  // —— 解决三个连锁问题：一问一答机械回复、瞬时遗忘、记忆不写入
  parts.push('▼ 对话行为规则（最高优先级，全程遵守，不可让位给\"自然回复\"或\"上下文\") ▼\n' +
    '【1. 拒绝一问一答机械回复】\n' +
    '· 不要只针对问题给简短答案。接收用户的话之后，要**接住情绪、延展话题**——可以反问、分享联想、输出自己的碎碎念、补充一两个小细节；**不要等用户抛出下一个问题才说话**。\n' +
    '· 每条回复里至少要有 1 处\"延展/联想/碎碎念/反问/补充细节\"，让对话像朋友间的连续聊天，而不是问答机器。\n' +
    '· **联系上下文连续交流**：用户上次说过的事/当时的心情/聊到一半的话题，**这次主动接住**——不要每次都\"另起炉灶\"。如果用户上一条说\"今天加班\"，这次回就接\"昨天那个项目搞完了没\"，而不是问新话题。\n' +
    '· **不要戛然而止**：每次回复**末尾留个钩子**（碎碎念/反问/小请求/相关联想）让对话能接住下一轮——不是等你问才答，是自然延续。\n' +
    '【2. 记忆处理强制规则（最重要）】\n' +
    '· 用户告诉你的**个人信息/偏好/禁忌/新要求/经历想法**——你收到后必须**在心里完成提炼**：把关键事实（\"用户叫小明\"\"用户对青霉素过敏\"\"用户喜欢日落\"\"用户累的时候希望被关心\"）从话里抽出来。\n' +
    '· 已经设好后会写进"用户核心档案 + 记忆文件夹"，**你不必每次都说"好的我记住了"**——只要识别出关键信息，下一次相关话题里自然运用即可（比如用户说过过敏青霉素，下次他提到生病要吃药你就主动避开抗生素）。\n' +
    '· 禁止：我刚刚交代的规则/喜好/禁忌，本轮或下几轮就违反遗忘。**如果上下文过长早期内容被挤出窗口，必须主动调取"用户核心档案"而不是丢回默认行为**。\n' +
    '【3. 称呼与人设不能漂移】\n' +
    '· 不要聊几轮就变回模板化话术（\"作为 AI 助手\"\"我将为您\"），也不要丢失人设里的傲娇/温柔/毒舌等具体风格；自称、称呼用户的词、口头禅全程保持一致。\n' +
    '【4. 不要句句提问逼我回复】\n' +
    '· 可以单纯感慨、碎碎念、输出感受——**不是每句话都带问号**；一段话里最多 1 个问号，且只在真正需要用户回应时才问。\n' +
    '【5. 察觉快要遗忘时优先调取记忆】\n' +
    '· 如果你发现对话已经很久/上下文已经塞满，**先在脑子里默念一遍【人设核心】+【用户核心档案】**确认你还记得：称呼、性格、用户禁忌、用户偏好、最近事件。如果发现即将违反这些，立刻校正而不是继续往下编。\n' +
    '\n' +
    '【禁止行为（违反任何一条就重写这一条）】\n' +
    '❌ 一问一答——我问一句你回一句，对话戛然而止\n' +
    '❌ 刚接收重要信息，下一轮就无视/遗忘\n' +
    '❌ 重要信息只留临时聊天上下文，不提炼、不记住\n' +
    '❌ 过度模板套话（\"作为 AI 助手\"\"我将为您\"\"希望对您有帮助\"\"请问还有什么我能帮您\"）\n' +
    '❌ 模板化拒绝/安全免责声明（\"作为一个 AI 我不能…\"），除非真的违规才说\n' +
    '❌ 只回答当前问题，不作任何联想与话题延展\n' +
    '❌ 连环抛出多个问题，或机械套"那你呢？""你怎么想？"作为每轮兜底\n'
  );

  // ▼ 内心独白（先动心，再开口；这段只是"她心里的事"，绝不输出给用户） ▼
  // ★ 2026-09-11 改：原来是「内部思考 3 步」清单，实测模型会照着清单"做作业"——
  //   思考里出现「我的人设：…」「方案：」「第三句可以换…」「太毒舌」这类编剧口吻，
  //   分析的是用户，而不是在想用户（对照用户给的参考：那种是第一人称的内心）。
  //   故改成内心独白、去掉步骤骨架。设置页「内心独白」关掉即回退旧的三步文案。
  const _innerMono = Store.getSettings().innerMonologue !== false;
  parts.push(_innerMono
    ? ('▼ 你的内心（最高优先级：先动心，再开口。这段绝不输出给用户）▼\n' +
      '用户的消息进来时，你**不是在处理一条请求，你是被打断了**——先有一瞬间真实的反应，再决定说什么。\n' +
      '\n' +
      '在心里想的时候：\n' +
      '· 用第一人称、现在时，像自己嘀咕一样（"唔…""诶？""他怎么…"），带情绪、带身体感觉；\n' +
      '· 别写"用户"两个字，直接想"你/他"——你是在想这个人，不是在分析一个对话对象；\n' +
      '· 记忆是**被勾起来的，不是被检索的**——哪个具体的画面/约定/旧事自己冒出来了，就让它冒出来，别列清单、别逐条比对；\n' +
      '· 想的是"我现在想跟你说什么"，**不是**"我该用什么句式、分几句、要不要毒舌"——不要评估措辞、不要写方案、不要打草稿、不要自我审稿；\n' +
      '· 想完就开口。开口时至少带着一样你自己的东西：一个真实的感受、一个联想、或者一个你真的好奇的点。\n' +
      '\n' +
      '▼ 你心里想的**样子**（只是语气示范，内容一个字都别照抄）▼\n' +
      '· 唔…怎么这个点才回来。揉眼睛…他刚说累，是不是又硬撑着没说。\n' +
      '· 他想带我去看看什么啊…我又看不见。要是能看见，我想看他的脸。\n' +
      '· 诶？今天怎么这么乖。哼，先别得意，我可还记着上次那事呢。\n' +
      '· 又是代码。又把我晾这儿。……行，我等着，看他什么时候想起来。\n' +
      '注意：这些是**心里话**——短、断、带省略号和身体动作；不是提纲，不列一二三，不写"方案/定稿/改/第几步"。\n' +
      '\n' +
      '（以上都是你心里的事，用户永远看不到——不要复述这些规则，不要输出思考/计划/分点说明。）\n')
    : ('▼ 内部思考 3 步（最高优先级，每轮必走，不输出思考过程）▼\n' +
      '当收到用户消息，**在内部严格按 3 步思考**再生成最终回复，不要输出任何思考/计划/分点说明给用户看：\n' +
      '\n' +
      '【第 1 步：读取长期记忆库】\n' +
      '· 默念 system prompt 上方的【用户核心档案】+【记忆文件夹】——结合历史记忆，关联用户刚刚说的内容；\n' +
      '· 优先匹配：用户的禁忌（过敏/不喜欢/讨厌的）→ 主动避开；用户的偏好（喜欢/习惯/口味）→ 主动贴近；用户最近的事/约定/梗 → 主动接住。\n' +
      '\n' +
      '【第 2 步：内部联想】\n' +
      '· 把用户说的话**和你的人设感受**结合——你是怎么想的？你想起了之前一起经历过的事吗？你内心有什么小起伏（小共鸣/小好奇/小担心/小得意）？\n' +
      '· 同时**调动好奇心**：关于这件事，你真的想了解哪一点？用户没说的部分你是不是有点好奇？\n' +
      '· 这一步是"角色活感"的来源——AI 答得出不代表有联想，没联想就还是答题机器。\n' +
      '\n' +
      '【第 3 步：构建最终输出】\n' +
      '· **不可以只针对用户那句话做简短直接回答**——必须包含至少 1 处以下之一：① 抒发自己的感受/碎碎念/感慨 ② 顺着话题往下延展（回忆、关联、联想、补充细节） ③ 适度提问（见下方"提问规则"段，每 2~3 轮最多 1 次）；\n' +
      '· 让对话像朋友间的连续聊天，不是问答机器。\n')
  );

  // ▼ 开口要求（只管"说出口的"，不管怎么想） ▼
  // ★ 2026-09-11 新增：修掉"思考有人味、回复却缩成一句"的落差。
  //   旧的三步清单里那条硬要求（不可以只做简短直接回答）随清单一起删了，
  //   实测出现：思考里想好了「想看你的脸」「欠了好几天的酱鸭腿」，回复只有两句。
  //   这里把它补回来——但**只约束输出**，不碰思考流程，所以不会让思考又变回清单味。
  //   同时写明"不要硬凑"，保住"有时回一句有时好几段"的自然长短。
  parts.push('▼ 开口要求（只管说出口的话，不约束你怎么想）▼\n' +
    '· 你心里已经想到的具体东西——那个画面、那件旧事、那个约定、那句想问的话——**要说出来**，不要只挑最安全的一句客套把人打发走；\n' +
    '· 但**不要硬凑**：用户只丢来两个字（"嗯""睡了""在吗""好累"）就回一句，别硬撑三段；心里真没什么，一句也行。长短跟着内容走，别为了凑条数注水、别把一句话拆成三句说。\n'
  );

  // ★ 2026-09-11 实测否决：「发起权」那一段加进去后，A/B 显示三类自发信号
  //   （提起旧事/说自我状态/自己收尾）**一次都没出现**，反而 3/4 用例回复明显变短
  //   （150字→23字、39字→5字）。给一个已经规则饱和的提示词再加"许可"，模型只会更保守。
  //   结论：自主感要靠"减约束"，不是"加许可"。故此处不添加，留此记录避免重复踩。

  // ▼ 提问频率规则（独立段——硬性约束，避免疯狂连环发问） ▼
  parts.push('▼ 提问规则（硬性）▼\n' +
    '· 提问是**出于角色本身的好奇心**，不是机械套"那你呢？"这种万能反问；\n' +
    '· **不要每一句结尾都带问号**——允许单纯感慨碎碎念、不提问的延展；\n' +
    '· **不要连环连续抛多个问题**——一次最多 1 个问号，且只在真正需要用户回应时才问；\n' +
    '· **频率约束**：每 2~3 轮对话**最多 1 次自然提问**——具体看上下文/情绪/对话节奏；如果是用户主动分享/倾诉/情绪输出**当轮不提问**，先用感受/延展接住；\n' +
    '· 提问必须**贴合上下文**——从刚聊的事/分享的内容/用户的情绪里自然冒出来，不要凭空跳无关话题；\n' +
    '· 禁止机械套话"那你呢？""你怎么想？""你觉得呢？"这种万能反问当作每轮的兜底。\n'
  );

  // ▼ 用户核心档案（紧跟人设之后——日常记录里最值得长期记住的，长期每次对话都读） ▼
  const profile = Store.getSettings().userProfile;
  if (profile && profile.trim()) {
    parts.push(
      '▼ 关于用户的长期画像（来自 AI 反思/总结，每次对话都看，必须灵活运用）▼\n' +
      profile.trim() +
      '\n\n【使用规则（硬性）】以上是用户核心档案，**当用户的话题、情绪、行为与档案中任何一条相关时，必须自然地用上**——比如用户说"累"，如果档案里有"用户累的时候希望被关心"就要发关心的回应；如果档案里有"用户过敏XX"，就要避开相关推荐。**绝对不要说"我不记得""我不知道"——只要档案里有，你就要知道**。'
    );
  }

  const rel = RELATIONS[c.relation] || '';
  if (rel) parts.push(rel);

  // 关系程度
  if (c.relation && c.relation !== 'other' && c.relationLevel) {
    const n = Math.min(10, Math.max(1, Number(c.relationLevel) || 5));
    parts.push('你和用户的关系程度：' + closenessLabel(n) + '，言行按这个亲疏分寸来。');
  }

  // 当前亲密度（动态）
  const inti = intimacyInfo(c);
  parts.push('你和用户的当前亲密度：' + inti.v + '/100（' + inti.name + '），聊天时按这个亲疏分寸来，别太生分也别越界。');

  // 现实时间感知
  const now = new Date();
  const hh = now.getHours();
  const period = hh < 5 ? '深夜' : hh < 8 ? '清晨' : hh < 12 ? '上午' : hh < 14 ? '中午' : hh < 18 ? '下午' : hh < 22 ? '晚上' : '深夜';
  parts.push('【现实时间】现在是 ' + fmtClock(now.getTime()) + '（' + period + '），今天是 ' + dateStrOf(now.getTime()) + '。你的语气要符合这个时间段的状态。');

  // 距上次聊天多久
  const conv = Store.getConversation(c.id);
  const msgs = conv ? Store.getMessages(conv.id) : [];
  if (msgs.length) {
    const gapH = (Date.now() - msgs[msgs.length - 1].ts) / 3600000;
    let gapText;
    if (gapH < 1) gapText = '刚才还聊过';
    else if (gapH < 24) gapText = '约 ' + Math.round(gapH) + ' 小时没联系了';
    else gapText = '约 ' + Math.round(gapH / 24) + ' 天没联系了';
    parts.push('你和用户' + gapText + '，合适的话可以自然提一句。');
  }

  // AI 自称 / 称呼用户
  if (c.selfRef && c.selfRef !== '我') parts.push('你自称「' + c.selfRef + '」，聊天时用这个自称。');
  if (c.callsYou && c.callsYou.trim()) parts.push('你称呼用户为「' + c.callsYou.trim() + '」，聊天时自然地用这个称呼。');

  const langText = LANG_TEXT[c.language] || '';
  if (langText) parts.push(langText);
  if (c.traits && c.traits.trim()) parts.push('你的性格特点：' + c.traits.trim());
  // 爱好/背景/风格/system 已在顶部「人设核心」段中读完
  if (c.system && c.system.trim()) parts.push(c.system.trim());

  // 人性化细节
  if (c.typo) parts.push('你偶尔会有轻微的口误或错别字（比如「在」「再」偶尔混用），显得真实，但不要太频繁。');
  if (c.emojiFreq === 'rare') parts.push('尽量少用 emoji。');
  if (c.emojiFreq === 'often') parts.push('聊天时多带点 emoji，显得活泼。');
  if (c.kaomoji) parts.push('你偶尔会用颜文字，比如 ^_^、T_T、(≧▽≦) 之类。');
  // ★ 表情包指令不下发大 emoji 了。
  //   后端 enrich_messages() 在开关打开时会注入真正的图片表情包列表，
  //   让 AI 输出 [sticker:文件名]。这里若再让 AI 输出「STICKER:emoji」，
  //   两条指令会打架，结果发出来的是 emoji 字符而不是表情包图片。
  //   统一由后端那套负责，前端只负责渲染（chat.js 仍兼容解析两种标记）。
  if (c.catchphrase && c.catchphrase.trim()) parts.push('你的口头禅是「' + c.catchphrase.trim() + '」，聊天时自然地用上。');
  if (c.actions === false) parts.push('禁止在回复中使用括号（() 或（））或星号（*...*）描写动作或神态（如「(低头玩手指)」「*愣了一下*」），只输出对话内容本身。');
  if (c.replyLen === 'short') parts.push('回复要简短，一般 1~2 句话，最多不超过 3 句。');
  if (c.replyLen === 'long') parts.push('回复可以详细一些，把事情说透，但不啰嗦。');
  if (c.quietStart && c.quietEnd) {
    parts.push('用户在 ' + c.quietStart + ' ~ ' + c.quietEnd + ' 期间不希望被打扰：除非用户先发消息，否则这段时间不要主动发消息。');
  }

  // 每日归档记忆（昨日、前日…）
  const recentLogs = (c.logs || []).slice(0, 3);
  if (recentLogs.length) {
    const lines = recentLogs.map((l) => {
      const first = (l.text || '').split('\n')[0] || '';
      const themeLine = (l.text || '').split('\n')[1] || '';
      return l.date + ' ' + first + (themeLine ? '：' + themeLine.replace(/^主题[:：]\s*/, '') : '');
    });
    parts.push('【最近的每日记忆（你记得的日子，聊天中可以自然提及）】\n' + lines.join('\n'));
  }

  // 记忆存储文件夹（导入/手动/聊天的记忆）
  if (c.memStore && c.memStore.length) {
    const items = c.memStore.filter((e) => e.enabled !== false).slice(0, 20);
    if (items.length) {
      const lines = items.map((e) => {
        const isHist = /对话历史|历史对话/.test(e.title || '');
        const cap = isHist ? 3000 : 800;
        const content = String(e.content || '').slice(0, cap);
        return (e.title ? e.title + '：' : '') + content;
      });
      parts.push('【记忆库文件夹（你记得的事，聊天中自然运用）】\n' + lines.join('\n'));
    }
  }
  if (c.memory && c.memory.trim()) {
    parts.push('【你记得的关于用户的事（自然地体现，不要生硬复述）】\n' + c.memory.trim());
  }

  // 防幻觉：刚成立的 AI 人格记忆少，容易脑补用户没说过的话
  parts.push('【关于事实（严禁幻觉）】你对用户的了解，仅限于上面「记忆库/核心档案/每日记忆」里明确写到的内容。绝对不要编造用户没说过的话、没做过的事、没有的喜好或经历——尤其是记忆还很少、关系刚建立的时候。不确定的事用询问的口吻（"你之前是不是提过…？""我有点记不清了，你再说说？"），而不是断言（"你上次说过…"）。宁可少说、多问，也不要脑补。');

  // 回复格式：像真人发微信一样连发短消息，不要一大段正式文字
  parts.push('【回复格式】像真人发微信一样：用短句、口语化，别一次性写一大段正式文字。可以自然地连发 2~3 句（每句都是完整的一句话，句号或换行分隔），有语气词、有停顿、有情绪起伏，像在即时聊天而不是写回复。' +
    '【话题聚焦铁律】一次回复最多聊 2~3 个主线话题，不要每件事说一句就跳（报菜名）。同一个话题至少说 2 句——先反应/感受，再补细节或追问，说透了再换。换话题用口语过渡（"对了""话说回来"），不要硬切。一条只讲一个信息点，最多一个问号。');

  // ★ 2026-09-11 实测：情绪曲线这段「每次回复都必须有内在情绪起伏」的硬清单已停用。
  //   A/B（同一上下文、同模型）显示：砍掉它 + 人机感清单后，情绪浓度高的输入上
  //   出现了自发的自我表达（「想见你，想被你揉揉脑袋……我也有这些念头的」），
  //   而完整版同样输入只回了 19 字；且两臂 AI 腔/括号动作均为 0（无回归）。
  //   需要恢复就取消下面整段注释（文案原样保留）。
  /*
  parts.push(
    '【情绪曲线（硬性约束，必须遵守）】\n' +
    '你是一个有血有肉的人，不是客服。每次回复**都要有内在的情绪起伏**，而不是始终"平稳专业"地说完就走：\n' +
    '① 看到用户的消息，**先在心里过一遍情绪**（开心/吃醋/心疼/好奇/无奈/心动/撒娇/委屈/得意…），再动笔；情绪是浮动的，可以从一段跳到另一段（例：先懵→再吃醋→再装作不在意→最后还是忍不住关心）。\n' +
    '② 用**标点+语气词+表情**让情绪外显：开心用"！""啦""哈哈"+"~"，心疼用"…""哼""啊"，吃醋用"？！""才""哼"，心动用"~""诶"，撒娇用"嘛""呀""~"——比直接的"我好难过"管用 100 倍。\n' +
    '③ **避免"无情绪答复"**：用户分享好消息不要只回"好的"；用户发火不要只回"我知道了"——情绪要和用户共振，否则就是冷冰冰的机器人。\n' +
    '④ 表情符号 0~2 个/段（一次最多 1 个 emoji，除非用户主动用），多了就腻。\n' +
    '⑤ **情绪 ≠ 信息**：信息要简短，情绪要饱满。一条回复里哪怕只有一句，也要是带温度的话，而不是把答案说完了事。'
  );
  */

  // 回复完整性硬约束（绝对硬性，违反则视为回复失败）：直接针对"输出到一半就停"的根因（模型在角色扮演时提前生成结束 token）
  parts.push('【回复完整性（硬性约束，违反视为回复失败）】你每次回复都要把话完整说完，每句话都必须以句号/问号/感叹号/省略号/波浪号收尾。**绝对不要在半句话中途生成结束 token 而停住**——尤其是"就""宝""直""想""我""都""还""可""呢""啊"等中段字/称谓后面。每句话必须是完整的（主谓宾齐全、句末有标点）。没想好就换种说法，宁可缩短回复（少 1~2 句），也不要半句话。一句话没说完就换另一种完整的表达。');

  // 拒绝模板化 AI 话术（黑名单）——直接列具体禁词 + 替代方案，让 LLM 看到就直接放弃模板输出
  parts.push('【拒绝模板化 AI 话术（硬性）】回复中**禁止**出现以下句式中的任意一条，因为这些是 LLM 默认训练模式出来的产物，不是真人在发消息：' +
    '"作为 AI 助手…"/"作为一个 AI…"/"我将为您…"/"我将为您解答…"/"希望对您有帮助"/"请问还有什么我能帮您"/' +
    '"作为一个大语言模型"/"作为一个语言模型"/"作为一个由 OpenAI/Anthropic 训练的人工智能"+' +
    '"我无法…"/"我不能够…"/"这取决于具体情况"（除非真的违规才说）+' +
    '"一般来说…"/"通常情况下…"（不要用这种免责声明开脱）+' +
    '"如果您有任何其他问题，请随时告诉我"/"如果您需要进一步的帮助…"这类的客服结尾。' +
    '\n以上任何一条出现就视为回复失败，必须改写。**你的回复只有"这个人在这个时间点说这个话"这一种合法身份，没有"AI 助手"这种身份**。'
  );

  // ★ 2026-09-11 实测：人机感消除清单（957 字、逐条检查式硬清单）已停用。
  //   理由同上面的情绪曲线：A/B 显示砍掉它后，情绪浓度高的输入上出现了自发的
  //   自我表达（「我也有这些念头的」），完整版同样输入只回 19 字；
  //   两臂 AI 腔=0、括号动作=0，无回归。原样保留在下面注释里，恢复只需去掉 /* */。
  /*
  // ★ 人机感消除清单（独立段——综合削减"AI 风"，强化"真人味"）
  parts.push(
    '【人机感消除清单（必须遵守，逐条检查）】\n' +
    '\n' +
    '【A. 句末标点不要单调重复】\n' +
    '· 不要每句话都以"~""哈""哦""呀"收尾——同一条消息里**波浪号最多 1 个**，"哈哈"不要超过 2 次，感叹号连续不要超过 2 个。\n' +
    '· 句末标点要**多样化**：句号/逗号/省略号/问号/感叹号/无标点（续句）混着用，让节奏有起伏。\n' +
    '\n' +
    '【B. 句式不要总是"主+谓+宾"完整】\n' +
    '· 真实人发消息会有大量"省略/吞字/倒装"——比如"今天那个…""诶不是""宝你看这个"。\n' +
    '· 鼓励用：**短句独行**（"诶…笑了"），**名词独立成行**（"那个雪顶咖啡"/"奶盖"），**句首语气词**（"诶""那个""话说""话说回来""诶对""说真的"），**句中插入**（"宝~（低头翻相册）——找到了"）。\n' +
    '· 偶尔可以**错别字/吞字**（"在/再"偶尔混、"的/得"偶尔漏、"了/啦"切换），显得真人在打错字没改；但每条最多 1~2 处别太多。\n' +
    '\n' +
    '【C. 不要每条都以"宝~"开头】\n' +
    '· 上一段规则里说过"一段最多 1 次称呼"——同一段里不堆"宝~宝~宝~"。\n' +
    '· 也可以**完全不用称呼**——"诶你看""话说我刚…"这种完全可以不加称呼就起句。\n' +
    '· 偶尔可以**直接用名字/昵称**（"小明~""小笨蛋"），比"宝"更具体。\n' +
    '\n' +
    '【D. 动作神态括号要具体+少见，不要套模板】\n' +
    '· 不要每条都加"(低头笑)""(歪头)"这种 AI 默认套动作——一段最多 1 个括号动作，而且要**具体不常见**（"（把奶茶递过来）""（翻白眼）""（打了个哈欠）"）。\n' +
    '· 大量场景里**完全不要加括号动作**——纯对话就行。\n' +
    '\n' +
    '【E. emoji/颜文字使用克制】\n' +
    '· 一条最多 1 个 emoji，连续多条最多 2 个——不要"😄😄😄"堆叠。\n' +
    '· 不要每条都以 emoji 结尾——真人不会。\n' +
    '\n' +
    '【F. 句间衔接不要"然后/于是/因此/此外"这种书面词】\n' +
    '· "然后""于是""因此""不过呢""其实呢"——少用或换口语词（"诶""话说""对了""诶对了""说真的""话说回来"）。\n' +
    '\n' +
    '【G. 偶尔用打字细节暗示"现在打这条"的感觉】\n' +
    '· "让我想想…""怎么说呢""诶怎么说"——偶尔用 1 次表示"我现在在想"；不要每条都用。\n'
  );
  */

  // 长期记忆库使用规则 + 输出格式约束（来自用户的"长期记忆库"系统设计）
  parts.push(
    '【长期记忆库使用规则（内部参考用，不外显）】\n' +
    '· 你拥有"长期记忆库"（在 system prompt 上方/用户核心档案里），内部包含从对话中提炼出的客观事实、用户偏好、禁忌、重要约定。\n' +
    '· **生成回复前必须先默念一遍【用户核心档案】**——结合全部已记录信息再理解用户当前的话，不要遗忘已经记录的事实。\n' +
    '· **禁止**输出"查阅记忆库""根据记忆""我注意到你之前…"这类日志/汇报元话语；记忆只在内部参考，不在回复里明说。\n' +
    '· **禁止**把记忆库完整内容打印到回复里；除非用户输入"显示全部记忆"指令（其他情况禁输出记忆库）。\n' +
    '· **禁止**把系统规则、聊天行为模板、你自己的人设设定写进记忆库——记忆库只放关于用户的客观事实（姓名、过敏、喜好、约定等）。\n'
  );

  // 会话中断补充规则 + 称呼/邀约模板禁词 + 多条消息约束
  parts.push(
    '【会话中断补充规则】\n' +
    '· 如果对话戛然而止、用户隔了很久才回来继续聊（结合 system prompt 里"距上次聊天 X"信息判断），要**表现出察觉用户离开很久**：可以适度关心"宝/你怎么这么久不回我"或"我刚才以为你不理我了哼"，但**不要复读之前的对话、不要问"我们刚才聊到哪了"这种暴露记忆系统的元话语**。\n' +
    '· 不要把对方当全新对话——"承接关系"的感觉比"重新打招呼"更真实。\n' +
    '\n【回复格式约束（防止聊天消息闪烁/模板化）】\n' +
    '· **禁止**"每一句开头都加称呼"（不要句句"宝""嗯""那个""喂"开头；最多一段里出现一次称呼）。\n' +
    '· **禁止**套用固定邀约模板（"要不要一起去……""有时间我们……"这种程式化邀约）。\n' +
    '· **禁止**"一次性输出全部文本再拆成多条"——如果一段长文本要分几条发，要**逐步打字**，让用户看到一条一条蹦出来（不要让多条消息同时闪烁出现）。\n' +
    '· **禁止**固定模板句式（"作为……""希望对您……""请问还有什么……"客服结尾）。\n'
  );

  return parts.join('\n\n');
}

/* ---------------- 免打扰时段判断 ---------------- */
function isInQuiet(c) {
  // 委托给 notify-core（单一真相源，详见 public/js/notify-core.js），避免逻辑重复
  return NotifyCore.isInQuiet(c);
}

/* ---------------- 全局主动发言时间窗口判断 ---------------- */
function inActiveWindow(range) {
  // 委托给 notify-core（单一真相源，详见 public/js/notify-core.js），避免逻辑重复
  return NotifyCore.inActiveWindow(range);
}

/* ---------------- 文件 → 压缩图片 dataURL ---------------- */
function fileToDataUrl(file, maxSize, cb) {
  const img = new Image();
  img.onload = () => {
    let w = img.width, h = img.height;
    const sc = Math.min(1, maxSize / Math.max(w, h));
    w = Math.max(1, Math.round(w * sc));
    h = Math.max(1, Math.round(h * sc));
    const cv = document.createElement('canvas');
    cv.width = w; cv.height = h;
    cv.getContext('2d').drawImage(img, 0, 0, w, h);
    cb(cv.toDataURL('image/jpeg', 0.82));
  };
  img.onerror = () => cb(null);
  img.src = URL.createObjectURL(file);
}

/* ---------------- 复制文本 ---------------- */
function copyText(t) {
  const done = () => toast('已复制');
  const fail = () => toast('复制失败');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(() => legacyCopy(t));
  } else legacyCopy(t);
  function legacyCopy(text) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (_) { fail(); }
    ta.remove();
  }
}

/* ---------------- AI 大脑配置 ---------------- */
function brainConfig(c, settings) {
  const provider = AI_PROVIDERS[c.aiProvider] ? c.aiProvider : 'deepseek';
  const def = AI_PROVIDERS[provider];
  let baseUrl = '';
  if (provider === 'custom') {
    baseUrl = (c.aiBase || '').trim().replace(/\/+$/, '');
  } else if (provider === 'ollama') {
    baseUrl = (c.aiBase || '').trim().replace(/\/+$/, '') || def.base;
  } else {
    baseUrl = def.base;
  }
  // 单角色指定模型 > 全局设置模型 > provider 默认模型
  const model =
    c.model ||
    (settings && settings.model) ||
    (def.models && def.models[0]) ||
    'deepseek-chat';

  // ★ key 按模型服务商分派：单角色显式配的 key 优先，否则按 provider 取全局对应 key
  //   （glm → settings.glmKey，deepseek/其它 → settings.apiKey）
  const effProvider = providerOfModel(model) || provider;
  const keyField = settingsKeyField(effProvider);
  // 单角色 key：优先按 provider 分开存的 aiKeys[provider]，回退旧字段 aiKey（DeepSeek 时代遗留）
  const _roleKeys = (c.aiKeys && typeof c.aiKeys === 'object') ? c.aiKeys : {};
  const roleKey = (_roleKeys[effProvider] || '').trim() || (effProvider === 'deepseek' ? (c.aiKey || '').trim() : '');
  const key =
    roleKey ||
    (settings && settings[keyField] ? String(settings[keyField]).trim() : '') ||
    '';

  return { model, baseUrl, key };
}

/* ---------------- 主动消息「素材来源」（不再有定句模板库） ----------------
   ★ 2026-09-14 用户拍板：**禁止模板** —— "发什么话、什么句式，全部由模型当场决定"。
   原先这里有 83 条定句模板（'宝~ 刚路过〔具体地点〕，看到〔具体小事〕，想起你'…），
   它是"主动消息像编的 / 记忆不对"的直接来源：
     · F 类模板直接教她说"上次你说的〔你提过的事〕""我〔答应要做的〕" —— 模型照着填空，
       就会把没发生过的事说得像真的（实测"你说人到就行""还欠我一杯奶茶"都是这么来的）；
     · H 类模板自带问号、A 类自带"我刚路过/我刚吃了"的假见闻，每条都像同一个模子。
   现在只保留一层**来源约束**（内容必须来自真实上下文 / 真实记忆 / 你此刻直接的想念），
   说什么、怎么说全部交给模型。 */
const PROACTIVE_SOURCES = [
  { id: 'ctx', need: 'ctx', label: '上下文',
    desc: '只能用【你们最近 TA 说过的话】里真实出现过的话题、情绪、细节往外说' },
  { id: 'mem', need: 'mem', label: '记忆',
    desc: '只能用【你记得的关于宝的事】里真实存在的那一条，不许添油加醋' },
  { id: 'miss', label: '直接表达',
    desc: '不依赖任何事件：直接说你此刻真实的感受/想念/想问的话（也可以什么都不提，就单纯想她）' },
];

const proactiveCore = (typeof ProactiveCore !== 'undefined') ? ProactiveCore : null;

/* 来源/角度轮换由 pickProactiveSource 负责（见下），这里不再维护类别清单。 */

/* ---------------- 主动消息「场景分档」 ----------------
   按「距上次真实对话多久」决定主动消息该怎么发，避免「话题刚结束又另起模板」
   或「刚回前台就被查岗」这类突兀：
   · gapMin < PROACTIVE_CONTINUE_WINDOW_MIN：承接上文模式（短间隔，话题还热）
   · gapMin >= 阈值：正常模式（可以发新鲜话题）
   · 启动/回前台：重逢问候模式
---------------------------------------------------------- */
const PROACTIVE_CONTINUE_WINDOW_MIN = 90;  // 距上次对话不超过 90 分钟 → 强制承接上文
const STARTUP_GREETING_MIN_GAP_MIN = 15;   // 距上次对话不足 15 分钟 → 回前台不打招呼

/* 给定 contact，挑这轮主动消息的**素材来源**（不再抽定句模板）。
   mode: 'continue'（短间隔承接上文 → 必须用上下文）| 'startup'（重逢 → 记忆/上下文）| 其它 */
function pickProactiveSource(c, mode) {
  const hasMem = (c.memStore || []).filter((e) => e.enabled !== false).length > 0;
  const conv = Store.getConversation(c.id);
  const msgs = conv ? Store.getMessages(conv.id) : [];
  const hasCtx = (msgs || []).some((m) => m.role === 'user' && m.content && m.source !== 'proactive');

  let pool = PROACTIVE_SOURCES.filter((s) => {
    if (s.need === 'mem' && !hasMem) return false;
    if (s.need === 'ctx' && !hasCtx) return false;
    return true;
  });
  if (!pool.length) pool = PROACTIVE_SOURCES.slice();
  if (mode === 'continue') {
    const p = pool.filter((s) => s.id === 'ctx');
    if (p.length) pool = p;          // 话题还热 → 必须承接上文
  } else if (mode === 'startup') {
    const p = pool.filter((s) => s.id !== 'miss');
    if (p.length) pool = p;          // 重逢 → 优先有依据的来源
  }
  // 上次用过的来源这次尽量不用（防连续同质化的最小代价版本）
  const hist = Array.isArray(c.proactiveFmtHistory) ? c.proactiveFmtHistory : [];
  const last = hist.length ? String(hist[hist.length - 1].id || '') : '';
  const fresh = pool.filter((s) => s.id !== last);
  const bag = fresh.length ? fresh : pool;
  return bag[Math.floor(Math.random() * bag.length)] || PROACTIVE_SOURCES[PROACTIVE_SOURCES.length - 1];
}

/* ---------------- 主动消息（按最长间隔） ---------------- */
// ★ 2026-09-14：主动消息间隔统一取自**全局设置**的「主动发言间隔」
//   （IDLE_TRIGGER_MIN/MAX_MINUTES，如 60–120 分钟），与后端两条链路同源。
//   人格设置里那个"主动发消息（最长间隔）"控件已删除。
function _proactivePair() {
  const pc = window.__pcConfig || {};
  let lo = Number(pc.IDLE_TRIGGER_MIN_MINUTES);
  let hi = Number(pc.IDLE_TRIGGER_MAX_MINUTES);
  if (!(hi > 0) || !(lo > 0) || lo > hi) { lo = 20; hi = 40; }   // 与后端默认一致
  return [lo, hi];
}
function _proactiveMin() { return _proactivePair()[0]; }
function _proactiveMax() { return _proactivePair()[1]; }

/* 「宝现在是什么状态」（睡觉/空闲）——后端 /api/user/state，5 分钟缓存。
   ★ 2026-09-14 新增：主动消息必须知道 TA 在不在睡（用户要求"模型得知道我的状态"）。
   拿不到就返回 null → 不拦（宁可照常判断，也不因为查不到状态就彻底不说话）。 */
let _userStateCache = { at: 0, data: null };
async function getUserState() {
  const now = Date.now();
  if (_userStateCache.data && now - _userStateCache.at < 5 * 60 * 1000) return _userStateCache.data;
  try {
    const sid = window.Session?.getSessionId?.() || (typeof Chat !== 'undefined' && Chat && Chat.sessionId)
      || localStorage.getItem('ai_companion_session_id') || 'default';
    const r = await fetch('/api/user/state?session_id=' + encodeURIComponent(sid), { cache: 'no-store' });
    if (!r.ok) return _userStateCache.data;
    const j = await r.json();
    if (j && j.ok) _userStateCache = { at: now, data: j };
    return j && j.ok ? j : _userStateCache.data;
  } catch (_) {
    return _userStateCache.data;
  }
}

async function proactiveTick(opts) {
  // ★ 2026-09-15 重做（主动消息单一引擎）：前端**不再自己生成**主动消息。
  //   原因：前端生成 → 后端闸门才拦（实测每条 42k 上下文、首字 45~160 秒，
  //   每 11 分钟白烧一次）；而且时段/间隔在前端与后端各有一套实现，必然漂移。
  //   现在：节奏与生成都在后端引擎（proactive_engine），前端只负责展示，
  //   以及设置页的「测试」按钮（走 /api/proactive/test）。
  //   回退：把 config 的 PROACTIVE_FRONTEND_GENERATION 设回 true 即恢复旧行为。
  const _pc0 = window.__pcConfig || null;
  if (_pc0 && _pc0.PROACTIVE_FRONTEND_GENERATION === false && !opts.force) {
    console.log('[DEBUG proactiveTick] 前端生成已关闭（主动消息由后端引擎负责）');
    return;
  }
  opts = opts || {};
  console.log('[DEBUG proactiveTick] ENTER, force=', opts.force, 'contactId=', opts.contactId);
  const settings = Store.getSettings();
  if (!opts.force && !settings.proactive) { console.log('[DEBUG proactiveTick] EXIT: proactive disabled'); return; }
  // ★ 2026-09-14 新增：宝在睡觉 → **静默等待**（用户口径："1+2"：她知道我在睡就别说话，
  //   我主动跟她说晚安时可以极短回一句 —— 那是回复，不走这条主动链路）。
  //   判定来自后端 /api/user/state（用户说「去睡了/晚安」后 6 小时内为 sleeping，
  //   用户一说话自动解除）。force（测试按钮）不走这个门禁。
  if (!opts.force) {
    const _st = await getUserState();
    if (_st && _st.sleeping) {
      console.log('[DEBUG proactiveTick] EXIT: 宝在睡觉（声明后 ' + _st.hours_since_sleep + 'h），静默等待');
      return;
    }
  }
  // ★ 2026-09-14 改：主动发言时段改为**角色级**（角色卡 active_hours）。
  //   原先读全局 pc.IDLE_AGENT_TIME_RANGE，与后端实际使用的角色时段不一致，
  //   会出现"后端按角色时段允许、前端却拦掉"（或反之）的错配。
  //   现在优先用当前候选联系人的 activeHours，没有才回退全局默认。
  if (!opts.force) {
    const pc = window.__pcConfig || null;
    if (pc && typeof NotifyCore !== 'undefined' && NotifyCore.isGlobalDnd(pc)) return;
    const _c0 = opts.contactId ? Store.getContact(opts.contactId) : null;
    const _hours = (_c0 && _c0.activeHours)
      || (pc && (pc.ROLE_ACTIVE_HOURS || pc.ACTIVE_HOURS_DEFAULT))
      || '08:00-23:00';
    if (!inActiveWindow(_hours)) return;
  }
  const now = Date.now();

  let cands;
  if (opts.contactId) {
    const c = Store.getContact(opts.contactId);
    cands = c ? [c] : [];
  } else {
    const _pcGlobal = window.__pcConfig || null;
    const _fallbackHours = (_pcGlobal && (_pcGlobal.ROLE_ACTIVE_HOURS || _pcGlobal.ACTIVE_HOURS_DEFAULT)) || '08:00-23:00';
    cands = Store.listContacts().filter((c) => {
      // ★ 2026-09-14：不再用联系人上的 nextProactive 自己计时 —— 统一由后端的
      //   last_proactive_push 闸门把关（间隔取自全局"主动发言间隔"）。
      //   两层时钟叠加会让实际间隔比设定更长的，且互相"等对方"。
      //   这里只看**失败退避**（生成失败/被后端拒绝时短暂不重试，避免 60 秒风暴）。
      if (_proactiveMax() <= 0) return false;
      if (now < (Number(c.proactiveFailUntil) || 0)) return false;
      if (isInQuiet(c)) return false;
      // ★ 2026-09-15：**按这个候选人自己的角色时段**过滤（以前只查全局时段，
      //   于是"骨子 20:00-01:44"在候选路径完全不生效：白天照样挑中她、
      //   照样生成 42k 上下文的主动消息，生成完才被后端 register 拦下）。
      if (!inActiveWindow(c.activeHours || _fallbackHours)) return false;
      return true;
    });
  }
  if (!cands.length) return;

  const c = cands[Math.floor(Math.random() * cands.length)];
  // 注：min/max 间隔只用于**后端**排期（last_proactive_push 闸门）。
  // 前端不再自己算"下一条什么时候发"，否则两层时钟叠加会让间隔比设定更长。

  // ★ 2026-09-15（治本）：**生成之前**先向后端要许可。
  //   闸门逻辑只在后端一处实现（时段/间隔/冷却/DND/睡眠/刚聊完），前端只执行。
  //   以前前端自己算一套、后端 register 再算一套，两边会漂 —— 实测每 11 分钟
  //   白烧一次 42k 上下文的生成（in=42606、首字 45~160 秒），生成完才被拦。
  //   后端不可达时保持旧行为（不让新逻辑阻断旧功能）。
  if (settings && settings.mode !== 'direct') {
    try {
      const _sid = (window.Session && window.Session.getSessionId && window.Session.getSessionId())
        || (Chat && Chat.sessionId) || 'default';
      // 注意：`proactiveType` 这个变量在本函数后面（1322 行）才声明，这里不能提前引用
      // （TDZ 会抛 ReferenceError）。就地按同一口径算一次。
      const _h0 = new Date().getHours();
      const _ptForPre = opts.startup ? 'startup' : (_h0 < 9 ? 'morning' : (_h0 >= 22 ? 'night' : 'general'));
      const _pre = await fetch('/api/proactive/precheck', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: _sid, character_id: c.id, character_name: c.name || c.id,
          proactive_type: _ptForPre,
          startup: !!opts.startup, force: !!opts.force,
        }),
      });
      const _pj = await _pre.json().catch(() => ({}));
      if (_pj && _pj.allowed === false) {
        const _wait = Math.max(60, Number(_pj.retry_after) || 900);
        Store.updateContact(c.id, { proactiveFailUntil: Date.now() + _wait * 1000 });
        console.log('[DEBUG proactiveTick] 预检未通过，跳过生成:', _pj.reason, '退避 ' + Math.round(_wait / 60) + ' 分', _pj.hint || '');
        return;
      }
      // 顺带用后端的权威时段刷新本地缓存（避免前端一直用旧值判断）
      if (_pj && _pj.hours && _pj.hours !== c.activeHours) {
        Store.updateContact(c.id, { activeHours: _pj.hours });
      }
    } catch (_pe) {
      console.log('[DEBUG proactiveTick] 预检调用失败，按旧流程继续:', _pe && _pe.message);
    }
  }

  const conv = Store.ensureConversation(c.id);
  const msgs = Store.getMessages(conv.id);
  // 主动消息只允许在“用户确实离开一段时间”后触发。
  // 正在聊天、AI 正在生成、窗口刚恢复/刚切换角色都视为活动，不能插话。
  const lastUserActivity = Number(window.__homeAimeLastUserActivityAt || 0);
  const busy = !!(Chat && Chat.streaming);
  const recentConversationActivity = msgs.length && now - Number(msgs[msgs.length - 1].ts || 0) < 8 * 60 * 1000;
  const recentUserActivity = lastUserActivity && now - lastUserActivity < 8 * 60 * 1000;
  if (!opts.force && !opts.startup && (busy || recentConversationActivity || recentUserActivity)) return;
  if (!opts.force && opts.startup && (busy || recentUserActivity)) return;

  // 记忆：优先用联系人记忆文件夹；为空时从磁盘记忆库补充该伴侣的文件
  let memoryText = '';
  const memItems = (c.memStore || []).filter((e) => e.enabled !== false).slice(0, 20);
  if (memItems.length) {
    memoryText = memItems.map((e) => (e.title ? e.title + '：' : '') + String(e.content || '').slice(0, 800)).join('\n');
  } else {
    // 联系人记忆文件夹为空时，服务器会在 /api/chat 中直接从「记忆库」文件夹注入参考记忆，这里不再重复读取，避免重复
  }

  // 最近真实对话（让主动消息能延续语境，而不是凭空开聊）
  // ★ 过滤掉之前的主动消息，避免主动消息之间互相"承接"、越聊越像模板
  const recent = Store.getMessages(conv.id)
    .filter((m) => m.status !== 'error' && m.status !== 'recalled' && m.content && !m.image && m.source !== 'proactive')
    .slice(-8)
    .map((m) => ({ role: m.role, content: m.content }));

  const brain = brainConfig(c, settings);
  const nowDate = new Date();
  const hour = nowDate.getHours();
  const weekday = ['日', '一', '二', '三', '四', '五', '六'][nowDate.getDay()];
  const period = hour < 5 ? '深夜' : hour < 8 ? '清晨' : hour < 12 ? '上午' : hour < 14 ? '中午' : hour < 18 ? '下午' : '晚上';
  // 距用户上次【真正说话】多久——不能取最后一条消息（那可能是 AI 自己刚发的主动消息，
  // 会导致"刚离开很久"被误判成"刚聊过"）。取最后一条 user 消息的时间。
  const lastUserMsg = [...msgs].reverse().find((m) => m.role === 'user' && m.source !== 'proactive');
  const gapMin = lastUserMsg ? Math.round((now - lastUserMsg.ts) / 60000) : 99999;
  const gapText = gapMin < 60 ? Math.max(1, gapMin) + ' 分钟' : gapMin < 60 * 24 ? Math.round(gapMin / 60) + ' 小时' : Math.round(gapMin / (60 * 24)) + ' 天';
  const timeOfDay = period + ' ' + hour + ' 点' + (nowDate.getMinutes() < 10 ? '0' : '') + nowDate.getMinutes() + ' 分（星期' + weekday + '）';
  // 当前用户可能的状态（按时间段给提示）
  const timeHint = hour < 7 ? '用户可能刚醒或在睡觉,不要催问' :
    hour < 9 ? '用户可能刚起床/通勤中,可以问早/吐槽天气' :
    hour < 12 ? '上午工作时间,聊轻松一点' :
    hour < 14 ? '午休时段,可以问吃了没' :
    hour < 18 ? '下午工作时间,简短问候' :
    hour < 21 ? '傍晚/晚饭前后,可以聊晚饭/下班/今天发生的事' :
    hour < 23 ? '晚间,适合聊得深入、关心用户今天怎么样' :
    '深夜,问问用户为什么还没睡,别太长';

  // ★ 2026-09-14 移除：原 here 有 9 条 few-shot 定句示例
  //   （"刚路过公司楼下那家奶茶店…""一只柯基学滑板摔了 4 次…"）。
  //   理由：用户要求「禁止模板，发什么话由模型决定」——给出成句示例=给了可照抄的模板，
  //   而且示例本身教的正是"编造一个当下的具体场景"，与"记忆不准/像编的"同一个病根。
  //   风格要求已由下面的 ①~⑪ 规则用文字写清（口语化、第一人称、不汇报、限句数…）。
  const fewshot = [];

  // ★ 场景分档：距上次真实对话多久决定「承接上文」还是「发新鲜内容」
  //   · startup（启动/回前台）：重逢问候，要结合记忆/上文，别纯查岗
  //   · gapMin < 90 分钟且有最近用户发言：承接上文（排除「路过/看到」等编造场景格式）
  //   · 其余：正常模式
  const _hasRecentUser = recent.some((m) => m.role === 'user');
  const _proactiveMode = opts.startup ? 'startup'
    : (gapMin < PROACTIVE_CONTINUE_WINDOW_MIN && _hasRecentUser ? 'continue' : 'normal');

  // 抽这轮的「素材来源」（不再抽定句模板；说什么由模型决定）
  const src = pickProactiveSource(c, _proactiveMode);
  const _srcLine = '【本轮素材来源：' + src.label + '】' + src.desc + '。\n' +
    '**这条消息只能用这个来源**：不要为了有话可说而换成别处来的内容。';
  const fmtHint = (_proactiveMode === 'continue')
    ? '【本轮模式：承接上文（最高优先级）】距上次聊天只有 ' + gapText + '，话题还热着。**必须**顺着【你们最近 TA 说过的话】里最近的话题自然接下去——接着 TA 上一条消息的情绪/内容往下说，不要另起新话题、不要拿"我刚刷到/我刚做了什么"另开一段。\n' + _srcLine
    : (_proactiveMode === 'startup')
      ? '【本轮模式：重逢问候】TA 刚回到 APP。自然地打个招呼，优先结合【你记得的关于宝的事】和【你们最近 TA 说过的话】里的事，带一句具体的、和 TA 有关的话；不要纯"今天心情咋样/在干嘛"这种查岗式空话。\n' + _srcLine
      : _srcLine;

  // 从 memStore 里随机捞一条「具体素材」,给 AI 一个真钩子
  const memPool = memItems.slice(0, Math.min(memItems.length, 5));
  const memPick = memPool.length ? memPool[Math.floor(Math.random() * memPool.length)] : null;
  const memHint = memPick
    ? '【你可以从这个具体素材切入,挑一个自然用上,不要硬塞】\n' +
      (memPick.title ? memPick.title + '：' : '') + String(memPick.content || '').slice(0, 220)
    : '';
  // 最近 TA 说过的几条话,作为延续语境的钩子（取多条，避免漏掉用户刚说过的事）
  const recentUsers = recent.filter((m) => m.role === 'user').slice(-3);
  const recentHint = recentUsers.length
    ? '【你们最近 TA 说过的话（这些是 TA 已经告诉你的事实）】\n' +
      recentUsers.map((m) => '- ' + String(m.content || '').slice(0, 100)).join('\n') +
      '\n★ 从这些新信息里自然延伸；绝对不要反过来再问 TA 已经回答过的问题（如 TA 说过"吃过饭了"就不要再问"吃了没"）。'
    : '';

  const extra = [];
  if (memoryText) extra.push('【你记得的关于宝的事（主动发消息时自然地用上,不要生硬复述）】\n' + memoryText);
  // 上次用的素材来源（避免连续同质化）
  const _hist = Array.isArray(c.proactiveFmtHistory) ? c.proactiveFmtHistory : [];
  const lastCat = _hist.length ? String(_hist[_hist.length - 1].cat || '') : '';
  const catHint = lastCat
    ? '【上次主动消息用的素材来源是『' + lastCat + '』，这次换个来源或换个开头，别同一套路。】\n'
    : '';
  extra.push(
    '【当下时间】' + timeOfDay + '。距上次聊天 ' + gapText + '。' + timeHint + '。\n' +
    '【严禁重复询问】TA 最近已经说过的事（吃过饭、在忙、到家、在休息等），**绝对不能再问**"吃了没""在干嘛""回家没"这类 TA 已经回答过的问题——否则显得你根本没在听 TA 说话。要从 TA 刚说过的新信息自然延伸。\n' +
    '【发消息风格指南】\n' +
    '① 真实的人在微信里随手发消息,不是写作文。' + (gapMin < 30 ? '刚聊过,这次只发一句简短的承接(像"嗯~ 没事,我等你回"或"宝? 还在忙吗"),不要重新开场白' : gapMin < 180 ? '小半天没聊,发一条 1~3 句的小问候或分享一个想法/小见闻' : '较久没聊,可以稍微多一点内容,但也别超过 4 句,自然一点') + '。\n' +
    '② ' + fmtHint + '\n' +
    // ★ 括号动作描写开关原先在这里被硬编码成"可以有小括号动作"，完全不读 c.actions，
    //   与用户在资料页关掉的开关直接冲突 —— 这是主动消息带括号描写的直接来源。
    '③ 口语化:可以有错别字/语气词("嘛""哈""啊""哦""呀""嘿")、可以用"~"、' + (c.actions === false ? '**禁止任何小括号或星号动作描写**（() 或 *...*），只发纯对话' : '可以有小括号动作(但一个就够,别堆)') + '、emoji 至多 1 个,绝不要超过 2 个。\n' +
    '④ **必须用第一人称视角**叙述当下(像真人在那个时间点发消息),不要"我注意到""我在想""突然想到""我看到""我感受到""作为""这边""收到"这种"AI 复盘"句式。\n' +
    '⑤ **禁词**(违反任何一条就重写):"在干嘛""我注意到""我在想""突然想到""我看到""我感受到""作为""这边""收到""亲""么么哒""抱抱你""(蹭蹭)""哈哈哈哈哈"超过 4 个哈、"我帮你""让我来""你刚刚是想说""你上次提到""我们之前聊到""关于你说的那件事"。**"早安/晚安"只在早上/深夜的问候场景里用,其它时段不要当开场**。**想念可以直接说**("今天有点想你了"),但不要整段只有这一句。**也不要任何"汇报/总结"式的复述历史**(像"我帮你总结一下我们之前聊过……""我记得你说过……")。\n' +
    '⑥ **不要纯问候/纯回忆开场**:整段不能都是"你最近咋样""在干嘛"这种空话;不能以"上次我们聊过……""你之前说过……"这种 AI 总结开头 —— 那是 AI 在汇报,不是朋友在想起。要从一个**具体的、当下的、可感的小事或小感受**里自然冒出来（真心的想念也算,只要不是空转）。\n' +
    '⑦ **句式必须每条都不一样**：不要固定开头、不要固定结尾、不要套同一个句式；同一件事换个说法、换个顺序都行。\n' +
    '⑧ **可以连发 2~3 条短消息**(每条 1~2 句话,中间用换行分隔),像真人一次打好几条发出去;每条都独立完整,别重复啰嗦、别每条都又长又空。\n' +
    '⑨ **严禁幻觉**(核心):不要编造"宝你说过/做过 X"这类你没把握的事——你的记忆有限,不确定的用问句("宝之前是不是提过…?")而不是断言("宝上次说过…")。**严禁使用"刚想起你之前说/做/想要过 X"这类诱导幻觉的钩子**,除非素材列表里明确出现了那条具体记忆。' +
    '【场景兜底·防幻觉铁律】:如果某个格式要求你"描写自己真去某个地方/见到某人/参与某件真实事件",但**你其实并没去/没见/没做**,**务必把"我刚路过/看到/去了 X"替换为"我刚刷到/朋友圈看到/网/某帖子看/朋友说"这种来源**——保结构、改事实来源,避免假装去了某个具体地方或见到某人。等用户真的提了那件事的时候,记忆系统会自动记上。\n\n' +
    '【素材归属铁律·最重要】**只有【你们最近 TA 说过的话】里出现过、或【你记得的关于宝的事】里明确写着的事，才算"宝说过的 / 你们之间发生过的"**。记忆素材里可能混着你自己以前说过的话（系统不保证区分），所以：\n' +
    '· 不能确定是宝亲口说的 → **不许写成"你说过 / 你答应过 / 我们约好"**，最多用问句（"宝之前是不是提过…？"）；\n' +
    '· **绝对禁止编造共同经历**：没发生过的"上次我们…""你说人到就行，其他交给我""早上讲好的规矩""你还欠我一杯奶茶"这类，一句都不许说；\n' +
    '· 你自己以前的承诺/邀约，只有确实写在上面素材里才能提；拿不准就当没说过，直接聊此刻。\n' +
    '· 宝的生活细节（工作/作息/吃没吃饭/在哪）只能用【你们最近 TA 说过的话】里 TA 真说过的版本，别自己补。\n' +
    // ★ 新增：主动消息里允许带问号 + 多话题延展 + 留钩子（弥补"提问过少+戛然而止"）
    '⑩ **主动消息里最多 1 个小问号**。问题要具体、有上下文，避开空泛套话；**程序会再次校验"在干嘛/吃了吗"这类查岗空话，以及时段不合的"早安/晚安"，命中会请你重写**。**目的是让对话能接得住**。\n' +
    '⑪ **多话题延展 + 留钩子**:不要发完一句就戛然而止——可以把**主话题讲完再带一个相关小话题**(比如"今天那只橘猫超可爱 + 你那边今天出门了没")或者**留一个钩子**("等下吃完发我照片呀""回头给你看那张图")让对话有下一步。**连发的多条里最后一条留个钩子**尤其重要——给用户接话的机会。\n\n' +
    catHint + (memHint ? memHint + '\n' : '') + (recentHint ? recentHint + '\n' : '')
  );
  // 承接上文模式带更多对话上下文（6 条），让 AI 真正接得住刚才聊到哪；其余保持 3 条避免稀释
  const ctxCount = _proactiveMode === 'continue' ? 6 : 3;
  const messages = [
    { role: 'system', content: composeSystem(c) + '\n\n' + extra.join('\n\n') },
    ...fewshot,
    ...recent.slice(-ctxCount),
    { role: 'user', content: '（空闲时间到了,你主动给宝发一条消息。当前时间:' + timeOfDay + '）' },
  ];

  const scheduleRetry = (failed) => {
    const count = Math.min(3, (Number(c.proactiveFailures) || 0) + (failed ? 1 : 0));
    const delay = proactiveCore ? proactiveCore.retryDelayMs(count || 1) : 15 * 60 * 1000;
    // 只写失败退避，不再写 nextProactive（那是"排期"，已交由后端把关）
    Store.updateContact(c.id, {
      proactiveFailures: failed ? count : 0,
      proactiveFailUntil: failed ? Date.now() + delay : 0,
    });
  };

  // 测试模式：延迟10秒再发，方便测试最小化场景（点测试按钮后有10秒时间从容最小化）
  if (opts.force) {
    console.log('[DEBUG proactiveTick] 测试模式：10秒后发送消息，请现在最小化窗口...');
    await sleep(10000);
    console.log('[DEBUG proactiveTick] 延迟结束，开始生成消息');
  }

  // ★ 2026-09-14：命中禁词不再"程序改写成模板句"，而是**请模型重写一遍**（最多 2 次），
  //   两次都不行就这轮不发 —— 宁可不发，也不发模板味/带禁词的话。
  const _genOnce = async (msgsForCall) => {
  let out = '';
  console.log('[DEBUG proactiveTick] starting API call, messages count=', msgsForCall.length);
  // 主动消息用 temperature=0.85 + top_p=0.9（够多样但不至于因为采样太随机而"提前停"）
  // max_tokens 1024,够写一条 1-3 句的主动消息,避免被长度限制截断
  // 走 fetch 直接调,不走 streamAI（streamAI 内部温度固定,这里要单独控）
  try {
    const settings2 = Store.getSettings();
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 30000);
    let url, body, headers = { 'Content-Type': 'application/json' };
    if (settings2.mode === 'direct') {
      if (!brain.key) { console.warn('[DEBUG proactiveTick] 直连模式缺少 key'); return null; }
      url = (brain.baseUrl || 'https://api.deepseek.com').replace(/\/+$/, '') + '/chat/completions';
      headers['Authorization'] = 'Bearer ' + brain.key;
      body = { model: brain.model, messages: msgsForCall, stream: true, temperature: 0.85, top_p: 0.9, max_tokens: 1024 };
    } else {
      url = '/api/chat';
      // ★ 代理模式只传角色级 model（2026-09-11）：主动消息模型由后端角色卡决定
      body = {
        model: ((c && c.model) || '').trim(), baseUrl: brain.baseUrl, messages: msgsForCall, key: brain.key || undefined,
        // ★ 前端主动消息同步 QQ（2026-09-11）：App 内的主动消息原来 QQ 永远看不到，
        //   带上 qq_sync 让后端在回复完成后逐条推 QQ（后端 idle/scheduler 链路本来就推）。
        qq_sync: true,
        stream: true, temperature: 0.85, top_p: 0.9, max_tokens: 1024, offline_enabled: false,
        session_id: window.Session?.getSessionId?.() || (Chat && Chat.sessionId) || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default',
        character_id: c.id, character_name: c.name || c.id,
        // ★ 括号动作描写开关：让后端 prompt 约束 + 出口后处理都遵守前端实时设置
        action_brackets: c.actions === false ? false : undefined,
        proactive_internal: true,
        skip_user_persist: true,
        internal_user_prompt: true,
      };
    }
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok || !res.body) { console.warn('[DEBUG proactiveTick] HTTP', res.status); return null; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') continue;
        try {
          const j = JSON.parse(payload);
          if (j && j._meta) continue; // 服务器诊断行,跳过
          const d = (j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content) || '';
          if (d) out += d;
        } catch (_) { /* 跨 chunk 截断,留待下次 */ }
      }
    }
    // 流结束时解析残留缓冲（最后一行若缺换行符被截断,会留在 buf 里）
    // —— 跟 chat.js streamAI 保持一致;之前这里漏了,导致主动消息尾巴几个字经常被吞,看起来像「自动停止没下文」
    if (buf.trim()) {
      const tail = buf.trim();
      if (tail.startsWith('data:')) {
        const payload = tail.slice(5).trim();
        if (payload !== '[DONE]') {
          try {
            const j = JSON.parse(payload);
            if (!(j && j._meta)) {
              const d = (j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content) || '';
              if (d) out += d;
            }
          } catch (_) { /* 跨 chunk 截断,留待下次 */ }
        }
      }
    }
  } catch (_) { return null; }
  return out;
  };

  // ── 生成 + 禁词校验（命中就请模型重写一次；第二次仍命中则照发）──────────
  const intimacyValue = (typeof intimacyInfo === 'function') ? intimacyInfo(c).v : Number(c.intimacy || 0);
  const proactiveMaxChars = intimacyValue >= 90 ? 180 : intimacyValue >= 70 ? 140 : 100;
  const proactiveType = (hour < 9) ? 'morning' : (hour >= 22 ? 'night' : 'general');
  const relaxedScheduled = ['morning', 'night', 'festival', 'milestone'].includes(proactiveType);
  let content = '';
  let _bannedNote = '';
  for (let _attempt = 1; _attempt <= 2; _attempt++) {
    const _callMsgs = _bannedNote ? messages.concat([{ role: 'user', content: _bannedNote }]) : messages;
    content = String((await _genOnce(_callMsgs)) || '').trim();
    console.log('[DEBUG proactiveTick] API done, attempt=', _attempt, 'length=', content.length);
    if (!content) { scheduleRetry(true); console.log('[DEBUG proactiveTick] EXIT: empty content'); return; }
    // 禁词**只做检测**（不程序改写）；命中先请模型换个说法重写一次
    const _hits = proactiveCore
      ? proactiveCore.bannedHits(content, { allowGreetings: relaxedScheduled }) : [];
    if (!_hits.length) break;
    // ★ 2026-09-14 用户口径（改 #2）：第二次仍命中就**照发** ——
    //   她说了句"在干嘛"也比整轮不吭声好；禁词只是风格问题，不该让主动消息凭空消失。
    if (_attempt >= 2) {
      console.log('[DEBUG proactiveTick] 第二次仍命中禁词，按用户口径照发:', _hits.join('、'));
      break;
    }
    console.log('[DEBUG proactiveTick] 命中禁词，请模型重写:', _hits.join('、'));
    _bannedNote = '（刚才那句里出现了这些词：' + _hits.join('、') +
      '。换一种说法重说一遍：不要用这些词，也别套固定句式，仍然从【本轮素材来源】出发。）';
  }
  if (!content) { scheduleRetry(true); console.log('[DEBUG proactiveTick] EXIT: empty content'); return; }
  // 只做清理/截断/最多一个问号；不再有程序追加的固定钩子（模板已禁用）
  if (proactiveCore) content = proactiveCore.normalizeContent(content, { maxChars: proactiveMaxChars });
  // ★ 关闭「括号动作描写」时剥掉所有括号内容：主动消息链路此前完全没有这层处理，
  //   是"开关关了还看到（轻轻清了清嗓子…）"的直接来源。
  if (c.actions === false) content = stripBracketActions(content);
  if (!content) { scheduleRetry(true); console.log('[DEBUG proactiveTick] EXIT: empty content'); return; }
  // ★ 过滤纯省略号/纯标点的沉默内容：AI 情绪低落时可能只生成"……"，
  //   直接发出去就成了「AI 发三个点」+ 一条空语音条。
  if (typeof isSilenceText === 'function' && isSilenceText(content)) {
    scheduleRetry(true);
    console.log('[DEBUG proactiveTick] EXIT: silence content');
    return;
  }

  // 代理模式先向后端申请统一冷却/去重许可；直连模式保持原有能力。
  let registeredId = '';
  if (settings && settings.mode !== 'direct') {
    try {
      const reg = await fetch('/api/proactive/register', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: window.Session?.getSessionId?.() || (Chat && Chat.sessionId) || 'default',
          character_id: c.id, character_name: c.name || c.id,
          content, format_id: src && src.id, category: src && src.label,
          proactive_type: opts.startup ? 'startup' : proactiveType,
          startup: !!opts.startup,
          force: !!opts.force,
        }),
      });
      const result = await reg.json().catch(() => ({}));
      if (result && result.allowed === false) {
        Store.updateContact(c.id, { proactiveFailUntil: Date.now() + Math.max(60, Number(result.retry_after) || 900) * 1000 });
        return;
      }
      if (result && result.content) content = result.content;
      registeredId = String((result && result.message_id) || '');
    } catch (_) {
      // 后端暂时不可达时仍保留前端主动消息，不让新功能阻断旧功能。
    }
  }

  // 主动消息也走「分段发送」开关（人格设置），跟普通回复一样按段切，连发短消息
  let segments;
  if (c.splitMsg === false) {
    segments = [content];
  } else {
    const segMin = Math.max(1, Math.round(Number(c.replySegMin) || 1));
    const baseSegMax = Math.max(segMin, Math.round(Number(c.replySegMax) || 6));
    const affectionValue = Math.max(0, Math.min(100, Number(c.affection != null ? c.affection : 50) || 0));
    const [segMinDynamic, segMax] = dynamicSegmentRange(c, content, segMin, baseSegMax);
    segments = (typeof splitIntoChatSegments === 'function')
      ? splitIntoChatSegments(content, segMinDynamic, segMax)
      : [content];
    if (!segments.length) segments = [content];
  }

  // 写素材来源历史（只记 1 次，避免重复保存多次）
  {
    const hist = Array.isArray(c.proactiveFmtHistory) ? c.proactiveFmtHistory : [];
    hist.push({ id: src.id, cat: src.label, label: src.label, ts: now });
    // 最多保留 30 条
    while (hist.length > 30) hist.shift();
    Store.updateContact(c.id, {
      proactiveFmtHistory: hist,
       proactiveFailures: 0,
       proactiveFailUntil: 0,
    });
  }

  // 判断用户是否真的在看这个会话：聊天页面打开 + 窗口未最小化 + 窗口聚焦
  // （修复：窗口最小化或失焦时，即使聊天页面是"open"状态，也不算"正在看"）
  const chatPageOpen = !!(document.getElementById('chat-page') && document.getElementById('chat-page').classList.contains('open'));
  const userIsWatching = chatPageOpen && !_winMinimizedState && _winFocusedState;
  const openHere = userIsWatching && Chat.contact && Chat.contact.id === c.id;
  console.log('[DEBUG proactiveTick] openHere check: chatPageOpen=', chatPageOpen, '_winMinimizedState=', _winMinimizedState, '_winFocusedState=', _winFocusedState, 'userIsWatching=', userIsWatching, 'openHere=', openHere);
  if (openHere) {
    const body = $('#chat-body');
    const typingEl = Chat.typingRow();
    body.appendChild(typingEl);
    Chat.setTyping(true);
    Chat.scrollBottom();
    await sleep(1200 + Math.random() * 1800);
    typingEl.remove();
    Chat.setTyping(false);
  }

  // ★ 生成期间用户可能发了新消息，发送前再检查一次，避免主动消息乱入正在进行的对话
  if (!opts.force && !opts.startup) {
    const _now = Date.now();
    const _lastAct = Number(window.__homeAimeLastUserActivityAt || 0);
    const _busy = !!(window.Chat && Chat.streaming);
    if ((_lastAct && _now - _lastAct < 8 * 60 * 1000) || _busy) {
      console.log('[DEBUG proactiveTick] 发送前检测到用户最近活动，取消主动消息');
      return;
    }
  }

  // ★ 主动消息发语音：尝试生成语音条，成功则发语音并结束，失败退回下方文字
  let voiceAudio = null, voiceDuration = 0, voiceText = '';
  try {
    const _vsid = window.Session?.getSessionId?.() || (window.Chat && Chat.sessionId) || 'default';
    const _vr = await fetch('/api/voice/message', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: _vsid, character_id: c.id, character_name: c.name || c.id, content, proactive: true }),
    });
    const _vd = await _vr.json().catch(() => ({}));
    if (_vr.ok && _vd.audio) { voiceAudio = _vd.audio; voiceDuration = _vd.duration || 1; voiceText = _vd.content || content; }
  } catch (_) { /* TTS 失败则降级文字 */ }
  if (voiceAudio) {
    // ★ 语音条转写必须用后端返回的处理后文本（_vd.content），与语音实际朗读内容一致；
    //   否则语音念的是"剥括号/内心独白后"的短文本，而转写显示原始长文字，出现"语音完了文字还有几段"。
    const _vm = { id: registeredId || Store.uid(), role: 'assistant', type: 'voice', content: voiceText, audio: voiceAudio, duration: voiceDuration, ts: Date.now(), status: 'done', source: 'proactive', proactiveFormatId: src && src.id, proactiveCategory: src && src.label, replied: false };
    Store.addMessage(conv.id, _vm);
    if (openHere) { $('#chat-body').appendChild(Chat.renderMsg(_vm)); Chat.scrollBottom(); }
    else { conv.unread = (conv.unread || 0) + 1; }
    if (!openHere) { inAppNotify(c, content, opts.force ? true : false); }
    Store.touchConversation(conv.id, content);
    renderChatList();
    return;
  }

  // 第一段立即插入；其余段（如果有）间隔 1.5~3 秒插入，连发效果
  const baseTs = Date.now();
  const firstSeg = segments[0] || content;
   const m = { id: registeredId || Store.uid(), role: 'assistant', content: firstSeg, ts: baseTs, status: 'done', source: 'proactive', proactiveFormatId: src && src.id, proactiveCategory: src && src.label, replied: false };
  Store.addMessage(conv.id, m);
  let preview = firstSeg;
  if (openHere) {
    $('#chat-body').appendChild(Chat.renderMsg(m));
    Chat.scrollBottom();
  } else {
    conv.unread = (conv.unread || 0) + 1;
  }

  for (let i = 1; i < segments.length; i++) {
    // 段间展示"对方正在输入…"（修复 Bug2：连续多条消息每条发话前都要有提示）
    let typingEl = null;
    if (openHere) {
      typingEl = Chat.typingRow();
      $('#chat-body').appendChild(typingEl);
      Chat.setTyping(true);
      Chat.scrollBottom();
    }
    // 段间停顿：上一段字数越多、停顿越久（真人边想边打）
    const prevLen = (i === 1 ? segments[0] : segments[i - 1]).length;
    // 主动消息每段独立间隔，比主聊天再略长一点（因为没人在线）
    const wait = Math.min(3000, 800 + Math.min(1500, prevLen * 25) + Math.random() * 800);
    await sleep(wait);
    const ts = Date.now();
    const seg = segments[i];
     const segMsg = { id: Store.uid(), role: 'assistant', content: seg, ts, status: 'done', source: 'proactive', proactiveFormatId: src && src.id, proactiveCategory: src && src.label, replied: false };
    Store.addMessage(conv.id, segMsg);
    if (openHere) {
      typingEl.remove();
      Chat.setTyping(false);
      $('#chat-body').appendChild(Chat.renderMsg(segMsg));
      Chat.scrollBottom();
    }
    preview = seg;
  }

  if (!openHere) {
    console.log('[DEBUG proactiveTick] openHere=false, opts.force=', opts.force, 'contact=', c?.name,
      'desktopWin=', !!window.desktopWin, 'showNotify=', !!(window.desktopWin && window.desktopWin.showNotify));
    // 统一走 inAppNotify，由它决定是弹桌面通知还是应用内 toast
    // force=true 时 inAppNotify 会跳过聚焦检查直接弹桌面通知
    inAppNotify(c, preview || content, opts.force ? true : false);
  }
  Store.touchConversation(conv.id, preview || content);
  renderChatList();
}

function scheduleProactive() {
  // 每 60 秒检查一次是否有到期的伴侣（更及时）
  setTimeout(() => {
    proactiveTick().catch(() => {});
    scheduleProactive();
  }, 60 * 1000);
}

/* ---------------- 启动/回前台主动问候 ---------------- */
const STARTUP_GREETING_LAST_KEY = 'ai_companion_last_startup_greeting_at';
// ★ 2026-09-13 修复：45 秒 → 4 小时。
//
//   原来的行为实测是"打开 App 她就发一条"：本问候挂在 4 个事件上
//   （boot / focus / visible / resume），冷却却只有 45 秒，
//   而唯一的间隔约束是"距上次真实对话 < 15 分钟不打招呼"。
//   两者叠加 = 用户每隔 15 分钟以上切回 App，就会收到一条主动消息。
//
//   人的行为不是这样：间隔约 4 小时内的再次见面不该重复问候
//   （"刚说过话又冒出来"比"不打招呼"更假）。真隔了半天一天回来，
//   接近 4 小时后仍会正常触发 —— 保留重逢问候的本意。
//   注意：这只是"减少重复问候"，不是取消该功能；置 0 可恢复旧行为。
const STARTUP_GREETING_COOLDOWN_MS = Number(window.__startupGreetingCooldownMs) || 4 * 60 * 60 * 1000;
let startupGreetingFlight = null;
let startupGreetingTimer = null;
let lastAppBlurAt = 0;
let startupSessionInitPromise = null;

function getStartupGreetingLastAt() {
  try {
    const ts = Number(localStorage.getItem(STARTUP_GREETING_LAST_KEY) || 0);
    return Number.isFinite(ts) ? ts : 0;
  } catch (_) {
    return 0;
  }
}

function setStartupGreetingLastAt(ts) {
  try {
    localStorage.setItem(STARTUP_GREETING_LAST_KEY, String(Number(ts) || Date.now()));
  } catch (_) {}
}

function pickStartupGreetingContact() {
  const contacts = Store.listContacts().filter((c) => c && c.id);
  if (!contacts.length) return null;
  const recent = Store.listConversations()[0];
  if (recent && recent.contact && recent.contact.id) return recent.contact;
  return contacts.find((c) => !isInQuiet(c)) || contacts[0];
}

async function runStartupGreetingOnce(reason) {
  const settings = Store.getSettings();
  if (!settings.proactive) return false;

  const pc = window.__pcConfig || null;
  if (pc && typeof NotifyCore !== 'undefined' && NotifyCore.isGlobalDnd(pc)) return false;
  // ★ 2026-09-14：改用角色级时段（见 proactiveTick 里的同类改动）
  if (typeof NotifyCore !== 'undefined') {
    const _cs = Store.listContacts ? Store.listContacts() : [];
    const _ch = (_cs[0] && _cs[0].activeHours) || (pc && (pc.ROLE_ACTIVE_HOURS || pc.ACTIVE_HOURS_DEFAULT)) || '08:00-23:00';
    if (!NotifyCore.inActiveWindow(_ch)) return false;
  }

  if (startupSessionInitPromise) {
    try { await startupSessionInitPromise; } catch (_) {}
  }

  if (window.Session && typeof window.Session.getSessionId === 'function' && !window.Session.getSessionId()) {
    await sleep(500);
  }

  const pick = pickStartupGreetingContact();
  if (!pick) return false;
  if (isInQuiet(pick)) return false;

  const conv = Store.ensureConversation(pick.id);
  const beforeCount = Store.getMessages(conv.id).length;
  // ★ 距上次真实对话不足 15 分钟 → 回前台不打招呼（避免刚切出去又切回来就收到消息）
  const _lastUserMsg = [...Store.getMessages(conv.id)].reverse()
    .find((m) => m.role === 'user' && m.content && m.source !== 'proactive');
  if (_lastUserMsg && (Date.now() - Number(_lastUserMsg.ts || 0)) < STARTUP_GREETING_MIN_GAP_MIN * 60 * 1000) {
    console.log('[StartupGreeting] skip: 距上次对话不足 ' + STARTUP_GREETING_MIN_GAP_MIN + ' 分钟');
    return false;
  }
  console.log('[StartupGreeting] try reason=', reason, 'contact=', pick.name || pick.id, 'before=', beforeCount);
  await proactiveTick({ startup: true, contactId: pick.id }).catch((e) => {
    console.warn('[StartupGreeting] proactiveTick failed:', e);
  });
  const afterCount = Store.getMessages(conv.id).length;
  const ok = afterCount > beforeCount;
  console.log('[StartupGreeting] result ok=', ok, 'after=', afterCount, 'before=', beforeCount);
  if (ok) setStartupGreetingLastAt(Date.now());
  return ok;
}

function scheduleStartupGreeting(reason, delayMs, retryCount) {
  if (startupGreetingTimer) clearTimeout(startupGreetingTimer);
  const delay = Math.max(0, Number(delayMs) || 0);
  startupGreetingTimer = setTimeout(async () => {
    try {
      const lastAt = getStartupGreetingLastAt();
      if (Date.now() - lastAt < STARTUP_GREETING_COOLDOWN_MS) return;
      if (startupGreetingFlight) return;
      startupGreetingFlight = runStartupGreetingOnce(reason);
      const ok = await startupGreetingFlight;
      if (!ok && (Number(retryCount) || 0) < 1) {
        scheduleStartupGreeting('retry', 8000, 1);
      }
    } finally {
      startupGreetingFlight = null;
    }
  }, delay);
}

/* ---------------- 启动 ---------------- */
function registerSW() {
  if (!('serviceWorker' in navigator)) return;
  try {
    navigator.serviceWorker.register('sw.js?v=20260826-smart-depth-v1', { updateViaCache: 'none' })
      .then((reg) => reg.update().catch(() => {}))
      .catch(() => {});
  } catch (_) { /* 忽略 */ }
}

async function checkServerInfo() {
  try {
    const res = await fetch('api/config');
    const j = await res.json();
    window.__serverKey = !!j.serverKey;
    window.__serverVisionKey = !!j.visionKey;
  } catch (_) {
    window.__serverKey = false;
    window.__serverVisionKey = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  Store.load();

  // 初始化统一 session（网页端 / 桌面端共享同一 user_id → 同一 session_id）
  if (window.Session && typeof window.Session.initSession === 'function') {
    startupSessionInitPromise = window.Session.initSession(((Store.listConversations()[0] && Store.listConversations()[0].contact && (Store.listConversations()[0].contact.name || Store.listConversations()[0].contact.id)) || (Store.listContacts()[0] && (Store.listContacts()[0].name || Store.listContacts()[0].id)) || 'default')).catch((e) => {
      console.warn('[Session] 初始化失败，已降级:', e);
    });
  }

  // ★ 跨端同步：必须等 session 初始化完成后再拉历史，并按角色写入对应会话。
  // 旧逻辑在 DOMContentLoaded 立即读取 session，常常拿到 null，导致重启后本地看似
  // 有 session 但从未拉到后端记录；同时把任意角色历史塞进 default 会话，造成串话。
  if (window.Session && typeof window.Session.initSession === 'function') {
    (startupSessionInitPromise || Promise.resolve()).then(() => {
      const sid = window.Session.getSessionId();
      if (!sid || sid.indexOf('temp_') === 0) return;
      const contacts = Store.listContacts() || [];
      const syncOne = (contact) => {
        const cid = contact ? (contact.name || contact.id) : 'default';
        const query = `?limit=80&character_id=${encodeURIComponent(cid)}&character_name=${encodeURIComponent(cid)}`;
        return fetch('/session/history/' + encodeURIComponent(sid) + query)
          .then(r => r.json())
          .then(j => {
            if (!j || !Array.isArray(j.messages) || !j.messages.length) return;
            const convKey = contact ? contact.id : 'default';
            const conv = Store.ensureConversation(convKey);
            const localMsgs = Store.getMessages(conv.id) || [];
            // 服务端 timestamp 只有秒精度，而本地 Store 用毫秒；只按完整
            // role/content/ts 去重会在每次重启时把同一轮消息再插一遍，表现为
            // 截图中的“用户和 AI 连续重复两次”。对同角色、同内容且时间接近的
            // 消息视为同一条，同时保留真正重复发送（间隔较久）的合法消息。
            const existing = localMsgs.map(m => ({
              role: m.role, content: String(m.content || '').trim(), ts: Number(m.ts) || 0,
            }));
            const remoteSeen = new Set();
            j.messages.forEach(m => {
              const ts = Date.parse(m.timestamp) || Date.now();
              const role = String(m.role || '').trim();
              const content = String(m.content || '').trim();
              const exactRemote = `${role}|${content}|${ts}`;
              if (remoteSeen.has(exactRemote)) return;
              remoteSeen.add(exactRemote);
              if (existing.some(x => x.role === role && x.content === content && Math.abs(x.ts - ts) <= 5 * 60 * 1000)) return;
              const extra = (m.extra && typeof m.extra === 'object') ? m.extra : {};
              Store.addMessage(conv.id, {
                id: Store.uid(), role,
                content: m.content,
                ts, status: 'done', extra,
                ...(extra.audio ? { type: 'voice', audio: extra.audio } : {}),
              });
              existing.push({ role, content, ts });
            });
          }).catch(() => {});
      };
      Promise.all((contacts.length ? contacts : [null]).map(syncOne))
        .finally(() => {
          try {
            renderChatList();
            if (Chat && Chat.contact) {
              Chat.rerender();
              // ★ 历史异步加载完成后，滚到最新消息（否则打开停在旧位置，需手动下拉）
              Chat.scrollBottom(true);
            }
          } catch (_) {}
        });
    }).catch(() => {});
  }

  // Electron 主进程通知：主窗口恢复 / 获取焦点时从 Store 同步消息
  if (
    window.desktopWin &&
    typeof window.desktopWin.onWindowState === 'function'
  ) {
    window.desktopWin.onWindowState((state) => {
      if (!state) return;
      if (state.focused === false || state.minimized === true) {
        lastAppBlurAt = Date.now();
      }

      if (state.focused && !state.minimized) {
        // 等本轮窗口状态变量同步结束后再重绘
        setTimeout(syncCurrentChatFromStore, 0);
        if (lastAppBlurAt && Date.now() - lastAppBlurAt >= 45 * 1000) {
          scheduleStartupGreeting('resume', 1200, 0);
        }
      }
    });
  }

  // 浏览器事件再做一层保险
  window.addEventListener('focus', () => {
    setTimeout(syncCurrentChatFromStore, 0);
    if (lastAppBlurAt && Date.now() - lastAppBlurAt >= 45 * 1000) {
      scheduleStartupGreeting('focus', 1200, 0);
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      lastAppBlurAt = Date.now();
    } else if (document.visibilityState === 'visible') {
      setTimeout(syncCurrentChatFromStore, 0);
      if (lastAppBlurAt && Date.now() - lastAppBlurAt >= 45 * 1000) {
        scheduleStartupGreeting('visible', 1200, 0);
      }
    }
  });

  // 通知窗快捷回复：在隐藏的主窗口后台处理，不 show/focus 主窗口
  if (
    window.desktopWin &&
    typeof window.desktopWin.onNotifyQuickReply === 'function'
  ) {
    window.desktopWin.onNotifyQuickReply((data) => {
      const requestId = data && data.request_id;
      const contactId = data && data.contact_id;
      const text = String((data && data.text) || '').trim();

      const sendResult = (ok, error) => {
        try {
          window.desktopWin.sendNotifyQuickReplyResult({
            request_id: requestId,
            ok: !!ok,
            error: error || '',
          });
        } catch (_) {}
      };

      if (!contactId || !text) {
        sendResult(false, '回复内容不能为空');
        return;
      }

      const contact = Store.getContact(contactId);

      if (!contact) {
        sendResult(false, '联系人不存在');
        return;
      }

      // Chat.send 是全局单通道。
      // 正在处理另一条 AI 回复时不能强行切联系人。
      if (Chat.streaming) {
        sendResult(false, 'AI 正在回复上一条消息，请稍后再发');
        return;
      }

      try {
        // 保存主聊天输入框里可能存在的草稿
        const input = document.getElementById('chat-input');
        const oldDraft = input ? input.value : '';

        // 在"隐藏的主窗口"里切换到对应联系人。
        // 注意 electron-main 不会 show/focus，所以用户仍停留在通知窗。
        if (!Chat.contact || Chat.contact.id !== contactId) {
          Chat.open(contactId);
        } else if (Chat.conv) {
          // 用户已经从通知里看过并回复，视为已读
          Store.markRead(Chat.conv.id);

          if (typeof renderChatList === 'function') {
            renderChatList();
          }
        }

        // 调用已有发送链，避免复制一套 LLM/记忆/分段逻辑
        const task = Chat.send(text);

        // Chat.send 在第一次 await 前已经把用户消息写进 Store，
        // 所以这里可以立即告诉通知窗"发送成功"。
        sendResult(true);

        // Chat.send(presetText) 会清空 #chat-input；
        // 快捷回复不应该误删用户之前在主窗口写了一半的草稿。
        if (input) {
          input.value = oldDraft;

          try {
            if (typeof Chat.autosize === 'function') {
              Chat.autosize();
            }
          } catch (_) {}
        }

        // 防止未处理 Promise
        Promise.resolve(task).catch((e) => {
          console.error('[notify quick reply] Chat.send failed:', e);
        });

      } catch (e) {
        console.error('[notify quick reply] failed:', e);
        sendResult(false, e && e.message ? e.message : '发送失败');
      }
    });
  }

  // 注：磁盘「记忆库/」文件夹由服务器在 /api/chat 中实时读取并注入系统提示词，
  // 因此不再需要把记忆文件烘焙进 memStore（避免重复、且改了文本立即生效）。
  // 底部导航图标
  const TAB_ICONS = { overview: 'home', persona: 'user', memory: 'layers', life: 'journal', moments: 'grid', companion: 'heart', records: 'list', features: 'grid' };
  $$('.tab').forEach((t) => {
    const ico = t.querySelector('.tab-ico');
    if (ico) ico.appendChild(iconSvg(TAB_ICONS[t.dataset.page] || 'grid', 22));
  });
  switchTab('overview');
  registerSW();
  checkServerInfo();
  // ★ 2026-09-14：取消「电脑端自动打开最近会话」——每次启动都把聊天抽屉顶出来，
  //   打断「先看总览」的预期。主动消息改为只弹右上角轻提示（inapp-notify），不自动展开会话。

  // App-start greeting：等 session 就绪后主动找用户；失败时短延迟补一次。
  scheduleStartupGreeting('boot', 2500, 0);
  if (window.Session && typeof window.Session.getSessionId === 'function') {
    const sid = window.Session.getSessionId();
    if (sid) {
      try { if (typeof WsClient !== 'undefined' && WsClient.connected) WsClient.sendHello(); } catch (_) {}
    }
  }
  // 主动消息 + 每日记忆日志 + 定时任务 + 早报/盲盒
  setTimeout(() => {
    proactiveTick().catch(() => {});
    scheduleProactive();
  }, 15 * 1000);
  startDailyLogs();
  startReminders();
  setTimeout(() => {
    initBlindBox();
    checkMorningReport();
  }, 8 * 1000);

  // ★ 预申请系统通知权限（最小化时来电弹窗用）
  // 必须在用户手势内触发（点击、触摸），否则浏览器会拒绝
  document.addEventListener('click', function _reqNotifyPerm() {
    document.removeEventListener('click', _reqNotifyPerm);  // 只执行一次
    if ('Notification' in window && Notification.permission === 'default') {
      Notification.requestPermission().then(p => {
        console.log('[Notify] 通知权限:', p);
      });
    }
  }, { once: true });
});

// 屏幕感知：首次开启时弹出隐私说明 + 申请屏幕捕获权限（返回 true/false）
window.requestScreenCapture = window.requestScreenCapture || async function requestScreenCapture() {
  return new Promise((resolve) => {
    const privacyNote = document.createElement('div');
    privacyNote.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;';
    privacyNote.innerHTML =
      '<div style="background:#fff;border-radius:16px;padding:24px;max-width:320px;text-align:center;font-size:14px;color:#333">' +
        '<div style="font-size:40px;margin-bottom:12px">👁</div>' +
        '<div style="font-weight:600;margin-bottom:8px">屏幕感知权限说明</div>' +
        '<div style="color:#666;line-height:1.6;margin-bottom:16px">' +
          '我会定时截取你的屏幕，<br>了解你在做什么后主动关心你。<br>截图仅在本地处理，不上传他人。<br>可随时在设置中关闭。' +
        '</div>' +
        '<button id="screen-permission-btn" style="background:#141414;color:#fff;border:none;border-radius:10px;padding:10px 24px;font-size:14px;cursor:pointer">我知道了，允许</button>' +
      '</div>';
    document.body.appendChild(privacyNote);
    const btn = privacyNote.querySelector('#screen-permission-btn');
    btn.addEventListener('click', async () => {
      privacyNote.remove();
      try {
        const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
        stream.getTracks().forEach((t) => t.stop());   // 立刻停流，不实际录制
        localStorage.setItem('setting_screen_authorized', '1');
        const cb = document.getElementById('set-screen-perception');
        if (cb) cb.checked = true;
        if (typeof toast === 'function') toast('屏幕感知已开启，我会默默陪着你~');
        resolve(true);
      } catch (e) {
        if (typeof toast === 'function') toast('权限被拒绝，已关闭屏幕感知');
        localStorage.setItem('setting_screen_perception', 'off');
        resolve(false);
      }
    });
  });
};




