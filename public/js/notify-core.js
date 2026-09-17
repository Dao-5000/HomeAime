/*
 * notify-core.js — 应用内消息通知的「纯逻辑」核心模块
 *
 * 设计目标：把与 DOM、Electron、真实时钟无关的判定逻辑抽出来，
 * 既供前端 app.js 复用（单一真相源），也便于单元测试（Node 内置 node:test 可直接 require）。
 *
 * 导出方式（UMD 风格）：
 *   - Node / CommonJS 环境：module.exports = { ... }
 *   - 浏览器 / Electron 渲染进程：window.NotifyCore = { ... }
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.NotifyCore = api;
  else if (root) root.NotifyCore = api;
})(typeof self !== 'undefined' ? self : (typeof globalThis !== 'undefined' ? globalThis : this), function () {
  'use strict';

  /* ---------------- 头像解析 ----------------
   * 仅当「提供了头像 URL」且「不是非 AI 类型发件人」时，才用图片头像；
   * 否则一律回退到「名称首字」文字头像。
   * 所有聊天对象在通讯录里都是 AI 人设（isAI 恒为 true）；
   * 这里保留 isAI 判定，以便未来接入非 AI 发件人时不误显头像。
   */
  function resolveAvatar(data) {
    data = data || {};
    const avatar = data.avatar;
    const isAI = data.isAI;
    if (avatar && typeof avatar === 'string' && avatar.trim() && isAI !== false) {
      return { mode: 'img', value: avatar.trim() };
    }
    const name = data.name || data.contact_name || 'AI';
    return { mode: 'text', value: name.charAt(0) };
  }

  /* ---------------- 应用内通知「是否该弹」的统一判定（单一真相源，可单测）----------------
   * 三个触发条件必须同时满足才弹：
   *   1. 应用处于前台焦点（focused）—— 最小化 / 正在使用其他应用 → 不打扰（满足需求 #2）
   *   2. 该联系人的会话当前未打开 —— 已在看这个聊天就不重复提醒
   *   3. 联系人不在免打扰时段
   * ctx: { contact, focused, conversationOpen, now? }
   */
  function shouldShowInAppNotify(ctx) {
    ctx = ctx || {};
    if (!ctx.focused) return false;                 // 最小化 / 其他应用在前台 → 不打扰
    if (ctx.conversationOpen) return false;         // 会话已打开，消息可见，不重复提醒
    if (ctx.globalDnd && !ctx.dndExempt) return false; // 全局 DND：必要消息也只静默入库
    const c = ctx.contact || {};
    if (isInQuiet(c, ctx.now)) return false;         // 免打扰时段（now 可注入以确定性测试）
    return true;
  }

  /* ---------------- 免打扰时段判断（支持跨天）----------------
   * c: { quietStart: 'HH:MM', quietEnd: 'HH:MM' }
   * 返回当前是否处于免打扰时段。
   *   - 缺字段 / 解析失败 / 起止相同 → false（不限制）
   *   - 同日内 (s < e)：s <= cur < e
   *   - 跨天 (s > e，如 23:00-07:00)：cur >= s 或 cur < e
   * 可注入 now（Date）以确定性测试；默认取当前时间。
   */
  function isInQuiet(c, now) {
    if (!c || !c.quietStart || !c.quietEnd) return false;
    now = now || new Date();
    const cur = now.getHours() * 60 + now.getMinutes();
    const sh = String(c.quietStart).split(':');
    const eh = String(c.quietEnd).split(':');
    if (sh.length < 2 || eh.length < 2) return false;
    const s = Number(sh[0]) * 60 + Number(sh[1]);
    const e = Number(eh[0]) * 60 + Number(eh[1]);
    if (s === e) return false;
    return s < e ? (cur >= s && cur < e) : (cur >= s || cur < e);
  }

  function isGlobalDnd(config, now) {
    config = config || {};
    if (!config.DND_ENABLED) return false;
    return isInQuiet({
      quietStart: config.DND_START || '23:00',
      quietEnd: config.DND_END || '07:00',
    }, now);
  }

  /* ---------------- 全局主动发言时间窗口 ----------------
   * 解析 "8-23" / "08:00-23:00" → [sh, sm, eh, em]，非法返回 null。
   * 与后端 config.parse_time_range 语义保持一致（前端侧副本，便于单测与一致性）。
   */
  function parseTimeRange(str) {
    if (!str || typeof str !== 'string') return null;
    const m = str.trim().match(/^(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?$/);
    if (!m) return null;
    const sh = parseInt(m[1], 10), sm = m[2] ? parseInt(m[2], 10) : 0;
    const eh = parseInt(m[3], 10), em = m[4] ? parseInt(m[4], 10) : 0;
    if (sh > 23 || eh > 23 || sm > 59 || em > 59) return null;
    return [sh, sm, eh, em];
  }

  /* 当前分钟是否落在主动发言时间窗口内。
   * range 为空 / 非法 → 视为不限制（返回 true）。
   * 可注入 curMinutes 以确定性测试；默认取当前时间。
   */
  function inActiveWindow(range, curMinutes) {
    const p = parseTimeRange(range);
    if (!p) return true;
    const now = new Date();
    const cur = (curMinutes != null) ? curMinutes : (now.getHours() * 60 + now.getMinutes());
    const s = p[0] * 60 + p[1];
    const e = p[2] * 60 + p[3];
    if (s === e) return true;
    return s < e ? (cur >= s && cur < e) : (cur >= s || cur < e);
  }

  return {
    resolveAvatar: resolveAvatar,
    shouldShowInAppNotify: shouldShowInAppNotify,
    isInQuiet: isInQuiet,
    isGlobalDnd: isGlobalDnd,
    parseTimeRange: parseTimeRange,
    inActiveWindow: inActiveWindow,
  };
});
