'use strict';
/* ============================================================
   AI 伴侣 · Session 统一管理
   统一网页端 (web) 与桌面端 (desktop) 的 session_id。

   逻辑：
     1. 本地持久化 user_id（同一台设备不变）
     2. 启动时请求后端 /session/get_or_create，拿到统一 session_id
     3. 所有需要会话上下文的调用都用 window.Session.getSessionId()

   说明：本项目 public/ 为原生脚本（无 bundler），故用全局
   window.Session 暴露接口，而非 ES import/export。
   ============================================================ */
(function (global) {
  const USER_ID_KEY   = 'ai_companion_user_id';
  const SESSION_KEY   = 'ai_companion_session_id';
  const CHARACTER_KEY = 'ai_companion_character_id';

  // ─────────────────────────────────────────
  // 判断当前运行环境
  // ─────────────────────────────────────────
  function getDeviceType() {
    if (global.electronAPI || global.desktopWin) return 'desktop';
    return 'web';
  }

  // ─────────────────────────────────────────
  // 本地存储（兼容 Electron 与浏览器）
  // ─────────────────────────────────────────
  const storage = {
    get(key) {
      try { return localStorage.getItem(key); } catch { return null; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch { /* ignore */ }
    },
  };

  // ─────────────────────────────────────────
  // 统一的 fetch（同源，避免 Electron webSecurity 跨源拦截）
  // ─────────────────────────────────────────
  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  // ─────────────────────────────────────────
  // 主函数：初始化 session
  // ─────────────────────────────────────────
  let _sessionCache = null;

  async function initSession(characterId) {
    characterId = characterId || 'default';

    // 已初始化且角色一致，直接返回
    if (_sessionCache && _sessionCache.character_id === characterId) {
      return _sessionCache;
    }

    // 读取本地存储的 user_id（保证同一台设备 user_id 不变）
    let userId = storage.get(USER_ID_KEY);

    try {
      const data = await postJSON('/session/get_or_create', {
        user_id:      userId || null,
        character_id: characterId,
        device_type:  getDeviceType(),
      });

      // 持久化 user_id（首次注册后就固定了）
      if (!userId && data.user_id) {
        storage.set(USER_ID_KEY, data.user_id);
      }

      // 缓存 session_id
      storage.set(SESSION_KEY,   data.session_id);
      storage.set(CHARACTER_KEY, data.character_id);

      _sessionCache = data;

      console.log(
        `[Session] ${data.is_new ? '新建' : '恢复'} session`,
        data.session_id,
        `(${getDeviceType()})`
      );

      return data;

    } catch (e) {
      console.error('[Session] 初始化失败，使用本地缓存', e);

      // 降级：用本地缓存
      const cachedSession = storage.get(SESSION_KEY);
      const cachedUser    = storage.get(USER_ID_KEY);

      if (cachedSession) {
        _sessionCache = {
          session_id:   cachedSession,
          user_id:      cachedUser || 'offline_user',
          character_id: characterId,
          is_new:       false,
        };
        return _sessionCache;
      }

      // 完全降级：生成临时 session
      const tempId = `temp_${Date.now()}`;
      _sessionCache = {
        session_id:   tempId,
        user_id:      tempId,
        character_id: characterId,
        is_new:       true,
      };
      return _sessionCache;
    }
  }

  // ─────────────────────────────────────────
  // 获取当前 session_id / user_id（同步，需先 initSession）
  // ─────────────────────────────────────────
  function getSessionId() {
    return (_sessionCache && _sessionCache.session_id)
      || storage.get(SESSION_KEY)
      || null;
  }

  function getUserId() {
    return (_sessionCache && _sessionCache.user_id)
      || storage.get(USER_ID_KEY)
      || null;
  }

  // ─────────────────────────────────────────
  // 重置（切换角色时用）
  // ─────────────────────────────────────────
  function resetSession() {
    _sessionCache = null;
  }

  global.Session = {
    initSession,
    getSessionId,
    getUserId,
    resetSession,
    getDeviceType,
  };
})(window);
