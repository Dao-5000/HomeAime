/* ============================================================
   Homeaime · Flow UI v9（flow-ui.js）
   1) 点页面外空白 → 自动关闭当前覆盖页
   2) ⌘K 真命令面板（可搜索、键盘可选、回车执行）
   3) 月亮按钮 → 真正的浅色主题（记忆上次选择）
   4) 记忆页 → 「记忆流」：统计头 + 时间脊柱 + 逐条浮现
   回滚：删掉 index.html 里对本文件的引用即可。
   ============================================================ */
'use strict';
(function () {
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const mod = (name) => { try { return (0, eval)('typeof ' + name + '!=="undefined"?' + name + ':null') || window[name]; } catch (_) { return window[name]; } };
  const VIEWS = { overview: '总览', memory: '记忆', companion: '陪伴', persona: '她们', life: '生活', moments: '朋友圈', records: '记录', features: '功能' };

  /* ---------- 屏蔽旧版占位提示 ---------- */
  (function mutePlaceholderToasts() {
    const orig = window.hzToast;
    if (typeof orig !== 'function' || orig.__hzMuted) return;
    const wrapped = function (msg) {
      if (/下一批/.test(String(msg))) return;
      return orig.apply(this, arguments);
    };
    wrapped.__hzMuted = true;
    window.hzToast = wrapped;
  })();

  /* ============================================================
     1. 点覆盖页之外 → 关闭
     ============================================================ */
  const KEEP = '#topbar-right,.topbar,.dock,#hzFan,#hzFab,.greet-block,.deck,.bento,.ov-grid';
  function closeOverlays(except) {
    let closed = [];
    $$('.overlay.open').forEach((o) => {
      if (o === except || o.id === 'chat-page' || o.id === 'chatpanel') return;
      o.classList.remove('open');
      closed.push(o.id);
    });
    const panel = $('#chatpanel'); if (panel && panel.classList.contains('open')) { panel.classList.remove('open'); closed.push('chatpanel'); }
    return closed;
  }
  document.addEventListener('click', (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    const open = $$('.overlay.open').filter((o) => o.id !== 'chat-page');
    if (!open.length) return;
    if (open.some((o) => o.contains(t))) return;         // 点在页面内
    if (t.closest(KEEP) || t.closest('.sheet') || t.closest('.mask') || t.closest('#hzPalette')) return;
    const closed = closeOverlays(null);
    if (closed.length) { try { window.hzToast && hzToast('已关闭 ' + closed.length + ' 个页面'); } catch (_) {} }
  }, true);

  /* ============================================================
     2. ⌘K 命令面板
     ============================================================ */
  function paletteActions() {
    const acts = [];
    Object.keys(VIEWS).forEach((v) => {
      acts.push({ g: '前往', t: VIEWS[v], k: 'view ' + v, icon: '#i-grid', run: () => window.hzShowView(v) });
    });
    const opens = [
      ['全局设置', 'Settings', '#i-sliders', () => mod('Settings').open()],
      ['伴侣资料与设置', 'Profile', '#i-users', () => { const c = (window.Store.listContacts() || [])[0]; if (c) mod('Profile').open(c.id); else window.hzShowView('persona'); }],
      ['记忆日志', 'Logs', '#i-brain', () => mod('Logs').open()],
      ['关系档案', 'Archive', '#i-folder', () => mod('Archive').open()],
      ['记忆存储', 'MemStore', '#i-folder', () => mod('MemStore').open()],
      ['外置记忆库', 'ExtMemory', '#i-folder', () => mod('ExtMemory').open()],
      ['自我觉察', 'SelfAware', '#i-spark', () => mod('SelfAware').open()],
    ];
    opens.forEach(([t, m, icon, run]) => acts.push({ g: '打开', t, k: m.toLowerCase(), icon, run }));
    const cs = (window.Store && Store.listContacts ? Store.listContacts() : []) || [];
    cs.slice(0, 5).forEach((c) => {
      acts.push({ g: '聊天', t: '和 ' + c.name + ' 聊天', k: 'chat ' + c.name, icon: '#i-pulse', run: () => window.hzOpenChat(c.id) });
    });
    acts.push({ g: '外观', t: '切换浅色 / 暗色', k: 'theme dark light 主题', icon: '#i-moon', run: toggleTheme });
    return acts;
  }

  let palSel = 0, palItems = [];
  function palOpen() {
    const mask = $('#hzPaletteMask'), pal = $('#hzPalette');
    if (!mask || !pal) return;
    mask.classList.add('on'); pal.classList.add('on');
    const inp = $('.hz-pal-in input', pal);
    inp.value = ''; palRender('');
    setTimeout(() => inp.focus(), 30);
  }
  function palClose() {
    const mask = $('#hzPaletteMask'), pal = $('#hzPalette');
    if (mask) mask.classList.remove('on');
    if (pal) pal.classList.remove('on');
  }
  function palIsOpen() { const p = $('#hzPalette'); return !!(p && p.classList.contains('on')); }
  function palRender(q) {
    const list = $('#hzPalette .hz-pal-list'); if (!list) return;
    const qq = String(q || '').trim().toLowerCase();
    const all = paletteActions();
    palItems = qq ? all.filter((a) => (a.t + ' ' + a.k).toLowerCase().indexOf(qq) >= 0) : all;
    if (!palItems.length) { list.innerHTML = '<div class="hz-pal-empty">没有匹配的命令</div>'; return; }
    if (palSel >= palItems.length) palSel = 0;
    let html = '', lastG = '';
    palItems.forEach((a, i) => {
      if (a.g !== lastG) { html += '<div class="hz-pal-sec">' + a.g + '</div>'; lastG = a.g; }
      html += '<div class="hz-pal-it' + (i === palSel ? ' sel' : '') + '" data-i="' + i + '">'
        + '<span class="pi"><svg viewBox="0 0 24 24"><use href="' + a.icon + '"/></svg></span>'
        + '<span class="pt">' + a.t + '</span></div>';
    });
    list.innerHTML = html;
    $$('.hz-pal-it', list).forEach((el) => el.addEventListener('click', () => palRun(+el.dataset.i)));
  }
  function palRun(i) {
    const a = palItems[i]; if (!a) return;
    palClose();
    try { a.run(); } catch (e) { try { window.hzToast && hzToast('执行失败：' + e.message); } catch (_) {} }
  }
  function palMove(d) {
    const n = palItems.length; if (!n) return;
    palSel = (palSel + d + n) % n;
    palRender($('.hz-pal-in input').value);
    const sel = $('#hzPalette .hz-pal-it.sel'); if (sel) sel.scrollIntoView({ block: 'nearest' });
  }
  function paletteBoot() {
    if ($('#hzPalette')) return;
    const mask = document.createElement('div'); mask.id = 'hzPaletteMask';
    const pal = document.createElement('div'); pal.id = 'hzPalette';
    pal.innerHTML =
      '<div class="hz-pal-in"><svg viewBox="0 0 24 24"><use href="#i-search"/></svg>'
      + '<input type="text" placeholder="搜索页面、设置、伴侣…" spellcheck="false"><kbd>ESC</kbd></div>'
      + '<div class="hz-pal-list"></div>';
    document.body.appendChild(mask); document.body.appendChild(pal);
    mask.addEventListener('click', palClose);
    pal.addEventListener('click', (e) => e.stopPropagation());
    const inp = $('.hz-pal-in input', pal);
    inp.addEventListener('input', () => { palSel = 0; palRender(inp.value); });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown') { e.preventDefault(); palMove(1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); palMove(-1); }
      else if (e.key === 'Enter') { e.preventDefault(); palRun(palSel); }
      else if (e.key === 'Escape') { e.preventDefault(); palClose(); }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && palIsOpen()) { palClose(); }
    });
  }
  /* 覆盖 flow-app 的占位实现（按钮与 Ctrl+K 都会走这里） */
  window.hzCmdkToggle = function () { palIsOpen() ? palClose() : palOpen(); };

  /* ============================================================
     3. 月亮按钮 → 真正的浅色主题
     ============================================================ */
  function applyTheme(light) {
    document.body.classList.toggle('light', !!light);
    if (light) document.body.classList.remove('dark');
    else document.body.classList.add('dark');
    try { localStorage.setItem('hzTheme', light ? 'light' : 'dark'); } catch (_) {}
  }
  function toggleTheme() {
    const light = !document.body.classList.contains('light');
    applyTheme(light);
    try { window.hzToast && hzToast(light ? '已切换浅色主题' : '已切换暗色主题'); } catch (_) {}
  }
  function themeBoot() {
    let saved = 'dark';
    try { saved = localStorage.getItem('hzTheme') || 'dark'; } catch (_) {}
    applyTheme(saved === 'light');
    const btn = document.getElementById('hzMoonBtn');
    if (btn && !btn.dataset.hzTheme) {
      btn.dataset.hzTheme = '1';
      btn.addEventListener('click', () => { setTimeout(() => { applyTheme(!document.body.classList.contains('light')); }, 0); });
    }
    /* flow-app 自带的那次 dark 切换会与上面互补；这里再兜一次同步 */
    if (btn) btn.addEventListener('click', () => { setTimeout(() => {
      const light = !document.body.classList.contains('dark') ? true : false;
      if (light) applyTheme(true); else applyTheme(false);
    }, 10); });
  }

  /* ============================================================
     4. 记忆页 → 记忆流
     ============================================================ */
  /* 记忆页的「时间轴浮现」逻辑已停用：记忆页现在由 flow-mem.js 接管
     （记忆海 + 列表）。原因：那套 reveal 依赖 opacity:0 的初始态 + 动画，
     一旦动画被打断/重跑，卡片就会永久隐形——用户看到的就是「列表显示不出来」。 */
  let memIO = null;
  function enhanceMemory() { /* 停用 */ }
  function updateSpine() { /* 停用 */ }
  /* 视图切换 / 滚动时挂钩 */
  function hookViews() {
    const orig = window.hzShowView;
    if (typeof orig === 'function' && !orig.__hzUi) {
      const wrapped = function () {
        const r = orig.apply(this, arguments);
        setTimeout(() => { try { enhanceMemory(); } catch (_) {} }, 220);
        return r;
      };
      wrapped.__hzUi = true; wrapped.__hzOrig = orig;
      window.hzShowView = wrapped;
    }
    document.addEventListener('scroll', () => { updateSpine(); }, true);
    setInterval(() => { try { enhanceMemory(); } catch (_) {} }, 2500);
    window.addEventListener('resize', () => { updateSpine(); });
  }

  /* ============================================================
     启动
     ============================================================ */
  function boot() {
    paletteBoot();
    themeBoot();
    hookViews();
    setTimeout(() => { try { enhanceMemory(); } catch (_) {} }, 900);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 600));
  else setTimeout(boot, 600);
})();
