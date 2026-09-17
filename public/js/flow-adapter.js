/* ============================================================
   Homeaime · Flow 接入适配层（flow-adapter.js）
   ------------------------------------------------------------
   作用：把「新版 Flow 壳层」与「原项目 36 个功能模块」接起来。

   背景（2026-09-13 UI 改版遗留）：
     · public/index.html 的壳层来自 UI改版方案/flow5 原型，原型自带一套演示
       视图（flow-app.js 里的 hzViewFns.*），只覆盖原项目 UI 的一部分；
     · 原项目的 8 个页面（#page-*）与 7 个覆盖页（#settings-page 等）才是
       功能完整的实现，但新壳层把它们包在 display:none 容器里，而且
       overview.js 没有被加载 → 点击后「没有反应 / 功能缺失」。

   本文件只做「接线」，不修改任何功能模块：
     1) 把模块暴露到 window（flow-app.js / pc_enhance.js 用 window.X 探测）
     2) 补回 index.html 漏掉的 overview.js
     3) memory/life/moments/records/features/persona/companion 视图改为挂载
        真实 #page-* 的渲染结果（原 DOM 与事件绑定全部保留）
     4) 修复壳层交互：Esc 关抽屉/覆盖层、FAB 展开态收口
   回滚：删掉 index.html 中对本文件的引用即可。
   ============================================================ */
'use strict';
(function () {
  console.info('[hz-adapter] script loaded');
  /* ---------- 0. 安全取全局（兼容 const 词法声明，如 const Settings = {...}） ---------- */
  function g(name) { try { return (0, eval)(name); } catch (_) { return undefined; } }
  /* flow-app.js 用 `const hzViewFns = {}` 声明：它是全局词法绑定，不是 window 属性。
     直接读 window.hzViewFns 恒为 undefined —— 这是适配层此前一直没跑起来的根因。 */
  function VF() { return g('hzViewFns') || window.hzViewFns; }

  /* ---------- 1. 模块挂载到 window ---------- */
  const EXPOSE = ['Settings', 'Profile', 'Logs', 'Archive', 'ExtMemory', 'MemStore',
    'SelfAware', 'Songs', 'SongsPanel', 'Features', 'Sheet', 'Chat', 'Store',
    'VoiceCall', 'Session', 'PcPTT', 'WsClient', 'desktopWin'];
  const EXPOSE_FN = ['renderOverview', 'renderContacts', 'renderMemory', 'renderLife',
    'renderMoments', 'renderCompanion', 'renderRecords', 'renderFeatures', 'renderChatList',
    'renderSettings', 'switchTab', 'intimacyInfo', 'openContactSheet', 'openLearningSheet',
    'openBackupSheet', 'generateMorningReport', 'initBlindBox', 'toast', 'hzShowView',
    'hzRenderOverview', 'closeHzFan'];
  function exposeAll() {
    const missing = [];
    for (const n of EXPOSE) {
      if (window[n] !== undefined) continue;
      const v = g(n);
      if (v !== undefined) window[n] = v; else missing.push(n);
    }
    for (const n of EXPOSE_FN) {
      if (typeof window[n] === 'function') continue;
      const f = g(n);
      if (typeof f === 'function') window[n] = f;
    }
    return missing;
  }

  /* ---------- 2. 补回 index.html 漏加载的模块 ---------- */
  const ENSURE = ['overview.js'];
  function ensureScripts(cb) {
    const todo = ENSURE.filter((src) => !document.querySelector('script[src*="' + src + '"]'));
    if (!todo.length) return cb();
    let pending = todo.length;
    todo.forEach((src) => {
      const s = document.createElement('script');
      s.src = 'js/' + src;
      s.defer = true;
      const done = () => { if (--pending <= 0) cb(); };
      s.onload = done; s.onerror = () => { console.warn('[hz-adapter] 模块加载失败:', src); done(); };
      document.body.appendChild(s);
    });
  }

  /* ---------- 3. 真实页面视图 ---------- */
  let HOME = null;                      // 惰性取「隐藏 page 容器」
  function homeNode() {
    if (HOME && document.body.contains(HOME)) return HOME;
    const sec = document.getElementById('page-overview');
    HOME = sec ? sec.parentNode : null;
    return HOME;
  }

  const REAL_VIEWS = {
    persona:   { page: 'persona',   render: 'renderContacts',  eyebrow: '她们在这里',        title: '她们',        sub: 'COMPANIONS' },
    memory:    { page: 'memory',    render: 'renderMemory',    eyebrow: '她的脑海里，存着你', title: '记忆',        sub: 'MEMORY ARCHIVE' },
    companion: { page: 'companion', render: 'renderCompanion', eyebrow: '她正在陪你',        title: 'Now Linking', sub: 'LINK TERMINAL' },
    life:      { page: 'life',      render: 'renderLife',      eyebrow: '把日子过好',        title: '生活',        sub: 'ROUTINE' },
    moments:   { page: 'moments',   render: 'renderMoments',   eyebrow: '她们的日常',        title: '朋友圈',      sub: 'MOMENTS' },
    records:   { page: 'records',   render: 'renderRecords',   eyebrow: '被保存下来的时间',  title: '记录',        sub: 'RECORDS' },
    features:  { page: 'features',  render: 'renderFeatures',  eyebrow: '一切能力，触手可及', title: '功能',        sub: 'MODULES' },
    agent:     { page: 'agent',     render: 'renderAgent',     eyebrow: '她的双手',  title: '干活', sub: 'AGENT TERMINAL' },
  };

  function mountRealView(v, name) {
    const cfg = REAL_VIEWS[name];
    if (!cfg || !v) return false;
    const sec = document.getElementById('page-' + cfg.page);
    const slot = v.querySelector('.hz-vslot');
    if (!sec || !slot) return false;
    const acts = v.querySelector('.v-acts'); if (acts) acts.innerHTML = '';
    if (sec.parentNode !== slot) slot.appendChild(sec);
    let fn = window[cfg.render] || g(cfg.render);
    try { if (typeof fn === 'function') fn(); }
    catch (e) { console.warn('[hz-adapter] ' + cfg.render + ' 渲染异常:', e); }
    return true;
  }

  /* 切换到别的视图时，把搬出去的 #page-* 放回隐藏容器 */
  function releaseRealViews(keepPage) {
    const h = homeNode(); if (!h) return;
    Object.keys(REAL_VIEWS).forEach((n) => {
      const p = REAL_VIEWS[n].page;
      if (p === keepPage) return;
      const sec = document.getElementById('page-' + p);
      if (sec && sec.parentNode !== h) h.appendChild(sec);
    });
  }

  /* 用真实渲染替换 flow-app.js 的演示视图 */
  function patchViewFns() {
    const vf = VF();
    if (!vf || !window.hzShowView) { console.warn('[hz-adapter] flow-app.js 未就绪'); return; }
    Object.keys(REAL_VIEWS).forEach((name) => {
      vf[name] = function (v) { mountRealView(v, name); };
    });
    const origShow = window.hzShowView;
    const patched = function (name) {
      /* 标记“切换动作源自 hzShowView”，避免下面的 switchTab 桥接再次回调 hzShowView 造成死循环 */
      window.__hzInShow = true;
      let r;
      try { r = origShow.apply(this, arguments); }
      finally { window.__hzInShow = false; }
      const v = name === 'overview' ? null : document.getElementById('hz-view-' + name);
      if (name === 'overview') releaseRealViews(null);
      if (v && !v.querySelector('.hz-vslot')) {
        const slot = document.createElement('div');
        slot.className = 'hz-vslot st';
        slot.style.setProperty('--i', '1');
        v.appendChild(slot);
      }
      if (v && REAL_VIEWS[name]) {
        /* 视图头部的操作区：由真实页面自己渲染，这里留空容器即可 */
        v.querySelectorAll('.v-acts').forEach((a) => { a.id = 'hzva-' + name; });
        mountRealView(v, name);
        releaseRealViews(REAL_VIEWS[name].page);
      }
      return r;
    };
    patched.__hzOrig = origShow;
    window.hzShowView = patched;

    /* 原项目页面内部大量用 switchTab('memory') 做导航（总览卡片的「共同心愿」等）。
       新壳层不认 .tab/.page，switchTab 只渲染不切视图 —— 这里桥接到 hzShowView。 */
    if (!window.__hzSwitchBridged) {
      window.__hzSwitchBridged = true;
      const origSwitch = window.switchTab || g('switchTab');
      if (typeof origSwitch === 'function') {
        window.switchTab = function (name) {
          const r = origSwitch.apply(this, arguments);
          if (!window.__hzInShow && name && name !== 'overview') {
            try { window.hzShowView(name); } catch (e) { console.warn('[hz-adapter] switchTab→hzShowView:', e); }
          }
          return r;
        };
      }
    }
  }

  /* ---------- 3.5 老壳层兼容节点（新壳层漏掉的静态 DOM） ---------- */
  /* pc_enhance.js 把「AI 内核设置」覆盖页 appendChild 到 #app（老壳层的页面容器）。
     新壳层删掉了 #app → TypeError，设置页根本创建不出来（点「AI 内核设置」无反应）。 */
  function ensureAppShim() {
    if (document.getElementById('app')) return;
    const app = document.createElement('div');
    app.id = 'app';
    document.body.appendChild(app);
  }

  /* records.js 用 #tab-records-dot 显示「记录」未读红点。新壳层把「记录」放进了 FAB 扇形，
     没有这个节点 → 有未读信件时也永远不提示。 */
  function ensureRecordsDot() {
    const btn = document.querySelector('.f-it[data-v="records"]');
    if (!btn || document.getElementById('tab-records-dot')) return;
    const dot = document.createElement('span');
    dot.id = 'tab-records-dot';
    dot.className = 'hz-unread-dot';
    btn.appendChild(dot);
    try { const f = g('updateRecordsTabDot'); if (typeof f === 'function') f(); } catch (_) {}
  }

  /* ---------- 4. 壳层交互修复 ---------- */
  function patchShell() {
    if (window.__hzShellPatched) return;
    window.__hzShellPatched = true;
    /* Esc：关掉最上层抽屉 / 覆盖层，避免「返回」不可达 */
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      const open = document.querySelector('#settings-page.open, #profile-page.open, #logs-page.open, ' +
        '#archive-page.open, #extmem-page.open, #memstore-page.open, #selfaware-page.open, #chat-page.open');
      if (!open) return;
      open.classList.remove('open');
      if (open.id === 'chat-page') open.classList.add('closed');
    });
    /* 点壳层空白处收起 FAB 扇形 */
    const stage = document.getElementById('stage');
    if (stage) stage.addEventListener('click', (e) => {
      /* 聊天抽屉：点抽屉外的空白 → 关掉聊天。
         原来只能点左上角细细的「‹」，用户会觉得「退不回去」。
         卡片/底部导航/顶栏/其它覆盖层不算空白，避免误关。 */
      const chat = document.getElementById('chat-page');
      if (chat && chat.classList.contains('open') && e.target && e.target.closest) {
        const keep = ['#chat-page', '.overlay.open', '.dock', '#hzFan', '#hzFab', '.topbar', '.deck', '.bento', '.ov-grid'];
        if (!keep.some((sel) => e.target.closest(sel))) {
          try { if (window.Chat && window.Chat.close) window.Chat.close(); } catch (_) {}
        }
      }
      const fan = document.getElementById('hzFan');
      if (!fan || !fan.classList.contains('open')) return;
      if (e.target.closest && (e.target.closest('#hzFan') || e.target.closest('#hzFab'))) return;
      if (typeof window.closeHzFan === 'function') window.closeHzFan();
    });
  }

  /* ---------- 5. 启动 ---------- */
  function boot(attempt) {
    attempt = attempt || 0;
    if (!window.Store || !VF()) {
      if (attempt > 60) { console.warn('[hz-adapter] 等待 Store/flow-app 超时'); return; }
      return setTimeout(() => boot(attempt + 1), 200);
    }
    ensureAppShim();
    const missing = exposeAll();
    ensureScripts(() => {
      exposeAll();
      patchViewFns();
      patchShell();
      ensureRecordsDot();
      releaseRealViews(null);
      if (missing.length) console.info('[hz-adapter] 模块未找到（可忽略浏览器端模块）:', missing.join(','));
      console.info('[hz-adapter] ready');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 300));
  else setTimeout(boot, 300);
})();
