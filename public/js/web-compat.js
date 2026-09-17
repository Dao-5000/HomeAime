/* ============================================================
 * web-compat.js — 网页版 Electron 兼容层
 * 用途：网页端测试时注入 window.desktopWin / window.electronAPI
 *       （Electron 里不存在此文件场景——preload 已提供真实现，
 *        仅在纯浏览器环境下本文件生效，且不覆盖真实 desktopWin）
 * 原则：
 *   - 若 window.desktopWin 已存在（Electron），什么都不做
 *   - 网页端提供完整 API 面，通知降级为浏览器 Notification
 *   - 所有方法带 console 提示，方便测试时区分"网页降级"与"真实失败"
 * ============================================================ */
(function () {
  if (window.desktopWin && window.desktopWin.isDesktop !== undefined) {
    return; // Electron 环境，跳过
  }

  var __isWebCompat = true;

  // ── 浏览器通知降级（Notification API）──
  var _notifEnabled = false;
  function _ensureNotifPermission() {
    if (!('Notification' in window)) return false;
    if (Notification.permission === 'granted') { _notifEnabled = true; return true; }
    if (Notification.permission === 'default') {
      Notification.requestPermission().then(function (p) {
        _notifEnabled = (p === 'granted');
      });
    }
    return _notifEnabled;
  }
  function _webNotify(data) {
    try {
      if (!_ensureNotifPermission()) return;
      var title = data && (data.title || data.name) ? (data.title || data.name) : 'AI伴侣';
      var opts = {};
      if (data && data.content) opts.body = data.content;
      if (data && data.avatar) { try { opts.icon = data.avatar; } catch (_) {} }
      new Notification(title, opts);
    } catch (e) {
      console.warn('[web-compat] 浏览器通知失败(降级):', e);
    }
  }

  function _log(api, args) {
    console.info('[web-compat] desktopWin.' + api + ' 被调用(网页降级)', args || '');
  }

  // ── 完整 API 面（与 preload.js 对齐）──
  window.desktopWin = {
    isDesktop: false,
    isWebCompat: true,

    isFocused: function () { return document.hasFocus && document.hasFocus(); },

    minimize:  function () { _log('minimize'); },
    toggleMax: function () { _log('toggleMax'); },
    close:     function () { _log('close'); },

    showNotify: function (data) {
      _log('showNotify', data && data.content);
      _webNotify(data);
    },

    onNotifyReply: function () { return function () {}; },
    onNotifyOpen:  function () { return function () {}; },
    onNotifyQuickReply: function () { return function () {}; },

    sendNotifyQuickReplyResult: function (data) { _log('sendNotifyQuickReplyResult', data); },

    getNotifyConfig: function () {
      _log('getNotifyConfig');
      var saved = {};
      try { saved = JSON.parse(localStorage.getItem('web_notify_config') || '{}'); } catch (_) {}
      return Promise.resolve(Object.assign({
        enabled: true,
        quietStart: '23:00', quietEnd: '08:00',
        showOnFocus: false
      }, saved));
    },

    setNotifyConfig: function (cfg) {
      _log('setNotifyConfig', cfg);
      try { localStorage.setItem('web_notify_config', JSON.stringify(cfg || {})); } catch (_) {}
      return Promise.resolve({ ok: true });
    },

    onWindowState: function (cb) {
      // 网页版：监听页面可见性变化模拟窗口状态
      if (typeof cb === 'function') {
        document.addEventListener('visibilitychange', function () {
          cb({ focused: !document.hidden, minimized: document.hidden });
        });
      }
      return function () {};
    },

    _sendDebug: function (msg) { _log('_sendDebug', msg); },
  };

  // ── electronAPI 兜底（部分旧代码引用）──
  if (!window.electronAPI) {
    window.electronAPI = {
      isDesktop: false,
      showNotify: window.desktopWin.showNotify,
      minimize: window.desktopWin.minimize,
      close: window.desktopWin.close,
    };
  }

  console.info('[web-compat] 已注入网页版兼容层（desktopWin/electronAPI），通知降级为浏览器通知');
})();
