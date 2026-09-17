/*
 * internal-msg-core.js — 「内部消息识别 + 强制记忆抽取」的纯逻辑单一真相源
 *
 * 为什么要有这个文件（2026-09-16 缺陷修复）：
 *   1) 旧 store.js 自带一份"内部摘要"关键词表，命中就在**加载时 filter 删除并写回 localStorage**
 *      —— 无条件、静默、永久、不可恢复，且零日志（见 UI改版方案\_取证-关键词删消息-2026-09-16.md）。
 *      判据本身与 backend/db.py 的 summary_heads 是逐字复制的两份，任何修改都会两端不一致。
 *   2) 旧 chat.js:1808 自带一条过宽正则 `我的.{1,8}是`，把「我的想法是这样」「我的意思是」
 *      当事实，强制抽取（还绕过长度的门禁）疯狂往记忆库写。
 *   现在两处判据都收敛到本文件（前端只有这一份；后端 db.py 那份不动，仍是历史遗留的第二份）。
 *
 * 职责边界（重要）：
 *   - 关键词启发式 = **只用于识别旧版脏数据**，且**只用于隐藏**，任何路径都**不用于删除**；
 *   - 新消息应当由产出它的内部流水线打结构化标记 `internal: true`（标记是唯一权威判据）；
 *   - 删除必须由用户显式操作（清空会话 / 删除会话），本模块不提供任何删除能力。
 *
 * 导出方式（UMD 风格，与 notify-core.js / proactive-core.js 一致）：
 *   - Node / CommonJS：module.exports = { ... }
 *   - 浏览器 / Electron 渲染进程：window.InternalMsgCore = { ... }
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.InternalMsgCore = api;
  else if (root) root.InternalMsgCore = api;
})(typeof self !== 'undefined' ? self : (typeof globalThis !== 'undefined' ? globalThis : this), function () {
  'use strict';

  /* ---------------- 1) 显式结构化标记（新数据的唯一权威判据） ----------------
   * 内部流水线写消息时必须自己带上（例如生成主动消息的合成 user 轮、
   * 提醒/定时任务的自产轮、后端同步回来的内部任务轮）。
   */
  function isExplicitlyInternal(m) {
    if (!m || typeof m !== 'object') return false;
    if (m.internal === true) return true;
    const extra = m.extra;
    return !!(extra && typeof extra === 'object' && extra.internal === true);
  }

  /* ---------------- 2) 旧关键词启发式（只认脏数据，只用于隐藏） ----------------
   * 与原 store.js:127-134 判据**逐字等价**，保证"识别面"不变（不扩大、不缩小）：
   *   - 只认 role === 'user' 的气泡；
   *   - 判据 A：6 个标题词里 ≥2 个出现在正文开头或换行之后；
   *   - 判据 B：以全角「（」开头且含时间/空闲/定时话术。
   */
  const LEGACY_LEAK_HEADS = ['近期心情', '关于用户', '用户偏好', '近期事件', 'AI状态', 'AI 当前状态'];
  const LEGACY_LEAK_TIME_RE = /(?:空闲时间到了|定时时间到了|当前时间:)/;

  function isLegacyLeakText(text) {
    const s = String(text == null ? '' : text).trim();
    if (!s) return false;
    let count = 0;
    for (const h of LEGACY_LEAK_HEADS) {
      if (s.startsWith(h) || s.includes('\n' + h)) count++;
    }
    return count >= 2 || (s.startsWith('（') && LEGACY_LEAK_TIME_RE.test(s));
  }

  /** 旧关键词启发式是否命中**这条消息**（沿用旧判据的角色门：只认 user 气泡）。
   *  ★ 只在"加载时识别旧版脏数据"这一处使用；命中后由调用方打上 internal:true 标记。 */
  function isLegacyLeakMessage(m) {
    if (!m || typeof m !== 'object') return false;
    if (m.role !== 'user') return false;
    return isLegacyLeakText(m.content);
  }

  /**
   * 隐藏判据（读时过滤用）：**只看结构化标记**，不猜正文。
   *   - 新数据：内部流水线写入时自带 internal:true；
   *   - 旧脏数据：加载时由 isLegacyLeakMessage 识别并补上 internal:true（历史迁移，一次性）。
   * 为什么读时不再跑关键词：用户自己粘贴的多行内容同样会命中关键词启发式，
   * 若在读/写路径上按正文隐藏，用户"刚发出去的话"会当场从界面消失（正是本缺陷的体验形态）。
   * 关键词只用于"认出旧数据"，一旦认出就固化成标记 —— 标记是唯一权威判据。
   */
  function isInternalMessage(m) {
    return isExplicitlyInternal(m);
  }

  /* ---------------- 3) 强制记忆抽取：真事实形态 + 上限 ----------------
   * 旧判据 /(记住|我叫|我过敏|我喜欢|我不喜欢|我的.{1,8}是|我讨厌|我对.{1,6}过敏)/
   * 的过宽点在 `我的.{1,8}是`：它把「我的想法是这样」「我的意思是」「我的是这样」这类
   * 与记忆无关的日常句当成事实。这里改成"属性名词 + 系动词/称呼动词"的真事实形态。
   */
  const FORCE_ROUNDS = 5;                       // 轮数兜底：每 5 个成功轮强制整合一次
  const FORCE_MIN_TEXT_LEN = 4;                 // 关键词支的最短长度（挡住"哦/嗯"这类单字刷抽取）
  const FORCE_WINDOW_MS = 10 * 60 * 1000;       // 关键词支的计数窗口
  const FORCE_MAX_PER_WINDOW = 3;               // 窗口内最多强制几次（超出→降级为非强制，不丢这一轮抽取）

  const FORCE_FACT_ATTRS = '(?:名字|姓名|生日|年龄|岁数|职业|工作|单位|公司|学校|专业|家乡|老家|住址|地址|电话|手机|微信|血型|星座|身高|体重|口味|忌口|过敏|病史|作息|爱好|习惯)';
  const FORCE_FACT_RE = new RegExp(
    '记住'                                                        // 明确祈使：「记住X」
    + '|我(?:叫|姓)[\\u4e00-\\u9fa5A-Za-z·]{1,10}'                  // 我叫小明 / 我姓王
    + '|我(?:对[^，。！？,.!?\\s]{1,12})?过敏'                       // 我对花生过敏 / 我过敏
    + '|我(?:不)?(?:喜欢|爱|讨厌|害怕|习惯|忌口)[^，。！？,.!?\\s]{1,12}' // 我喜欢X / 我不喜欢X / 我讨厌X
    + '|我的' + FORCE_FACT_ATTRS + '(?:叫|是|为|在)'                 // 我的生日是… / 我的名字叫…
  );

  /** 是否像"用户在明确告知一条事实/偏好"（旧 `我的.{1,8}是` 的收敛替代）。 */
  function looksLikeFactStatement(text) {
    const s = String(text == null ? '' : text).trim();
    if (s.length < FORCE_MIN_TEXT_LEN) return false;
    return FORCE_FACT_RE.test(s);
  }

  /**
   * 决定本轮是否"强制抽取"（force 会绕过长度的门禁，所以必须有上限）。
   *   force=true 的两条支路：轮数兜底（reason='rounds'）/ 真事实形态（reason='fact'）；
   *   force=false 时调用方**仍会做非强制抽取**，只是不绕过长度门。
   * input: { text, roundsSinceExtract, recentForceTs, now }
   * 返回:  { force, reason: 'rounds'|'fact'|'none'|'capped', next, recentForceTs }
   */
  function decideForceExtract(input) {
    input = input || {};
    const rounds = Number(input.roundsSinceExtract) || 0;
    const next = rounds + 1;
    const now = Number(input.now) || 0;
    const windowMs = Number(input.windowMs) > 0 ? Number(input.windowMs) : FORCE_WINDOW_MS;
    const maxPerWindow = Number(input.maxPerWindow) > 0 ? Number(input.maxPerWindow) : FORCE_MAX_PER_WINDOW;
    const recent = (Array.isArray(input.recentForceTs) ? input.recentForceTs : [])
      .map((t) => Number(t) || 0)
      .filter((t) => now - t < windowMs);

    if (next >= FORCE_ROUNDS) return { force: true, reason: 'rounds', next, recentForceTs: recent };
    if (!looksLikeFactStatement(input.text)) return { force: false, reason: 'none', next, recentForceTs: recent };
    if (recent.length >= maxPerWindow) return { force: false, reason: 'capped', next, recentForceTs: recent };
    return { force: true, reason: 'fact', next, recentForceTs: recent.concat([now]) };
  }

  return {
    LEGACY_LEAK_HEADS,
    LEGACY_LEAK_TIME_RE,
    FORCE_FACT_RE,
    FORCE_ROUNDS,
    FORCE_MIN_TEXT_LEN,
    FORCE_WINDOW_MS,
    FORCE_MAX_PER_WINDOW,
    isExplicitlyInternal,
    isLegacyLeakText,
    isLegacyLeakMessage,
    isInternalMessage,
    looksLikeFactStatement,
    decideForceExtract,
  };
});
