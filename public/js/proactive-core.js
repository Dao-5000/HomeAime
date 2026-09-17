'use strict';

/* 主动消息的纯逻辑：浏览器与 node:test 共用，避免规则只写在 prompt 里。
   ★ 2026-09-14 用户拍板「禁止模板，发什么话由模型决定」后，这里**不再改写用户的文案**：
     · 删掉了 REPLACEMENTS（"在干嘛"→"这会儿手头忙不忙" 这类固定句替换）—— 那是模板；
     · 删掉了"没有问号就追加，你呢？"的固定钩子 —— 那也是模板；
     · 删掉了按模板 id 的时段过滤（定句模板库已整体删除）。
   现在只保留三件"机器该干的事"：检测禁词、清理格式、按长度截断。 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.ProactiveCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  // 空话/查岗式禁词：命中就请模型重写（不程序改写）
  // ★ 2026-09-14：「想你了」**已从禁词里去掉** —— 用户明确允许主动消息"直接表达思念"
  //   （"或直接表达思念的话"）。留下的是真正像查岗、像客服的空话。
  const BANNED = ['在干嘛', '吃了吗'];
  // 早晚安/节日/纪念日等场景里这两个词是正当的，不判禁
  const GREETING_WORDS = ['晚安', '早安'];

  function getPeriod(date) {
    const d = date instanceof Date ? date : new Date(date || Date.now());
    const h = d.getHours();
    if (h < 5) return 'late_night';
    if (h < 8) return 'early_morning';
    if (h < 11) return 'morning';
    if (h < 14) return 'lunch';
    if (h < 18) return 'afternoon';
    if (h < 22) return 'evening';
    return 'night';
  }

  /* 命中哪些禁词。allowGreetings=true 时放行问候类词（只见于定时主动消息）。 */
  function bannedHits(text, opts) {
    opts = opts || {};
    const allowGreetings = opts.allowGreetings === true;
    const words = allowGreetings ? BANNED.filter((w) => !GREETING_WORDS.includes(w)) : BANNED.concat(GREETING_WORDS);
    const s = String(text || '');
    const seen = [];
    for (const w of words) {
      if (s.includes(w) && !seen.includes(w)) seen.push(w);
    }
    return seen;
  }

  function hasBanned(text, opts) {
    return bannedHits(text, opts).length > 0;
  }

  function _trimComplete(text, maxChars) {
    if (text.length <= maxChars) return text;
    const cut = text.slice(0, maxChars);
    const last = Math.max(cut.lastIndexOf('。'), cut.lastIndexOf('！'), cut.lastIndexOf('？'), cut.lastIndexOf('\n'));
    return (last >= Math.floor(maxChars * 0.55) ? cut.slice(0, last + 1) : cut).trim();
  }

  /* 只做清理：去 think 块/引号/多余空白、统一问号、按长度截断、限制问句数量。
     不追加任何文字、不替换任何词。 */
  function normalizeContent(value, opts) {
    opts = opts || {};
    const maxChars = Math.max(20, Number(opts.maxChars) || 100);
    const maxQuestions = Number.isFinite(Number(opts.maxQuestions)) ? Number(opts.maxQuestions) : 1;
    let text = String(value || '')
      .replace(/<think>[\s\S]*?<\/think>/gi, '')
      .replace(/^[\s"“”]+|[\s"“”]+$/g, '')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n');
    text = _trimComplete(text, maxChars);
    if (!text) return '';

    text = text.replace(/[?？]/g, '？');
    if (maxQuestions >= 0) {
      let seen = 0;
      text = text.replace(/？/g, (m) => {
        seen += 1;
        return seen <= maxQuestions ? m : '。';
      });
    }
    return _trimComplete(text, maxChars).trim();
  }

  /* 兼容旧调用：清理（默认**不因禁词丢弃**，与后端口径一致）。
     传 rejectBanned:true 才在命中禁词时返回空串（调用方需自行重写/跳过）。 */
  function sanitizeContent(value, opts) {
    opts = opts || {};
    const text = normalizeContent(value, opts);
    if (!text) return '';
    if (opts.rejectBanned !== true) return text;
    return hasBanned(text, { allowGreetings: opts.allowGreetings === true }) ? '' : text;
  }

  function retryDelayMs(failures) {
    const n = Math.max(1, Number(failures) || 1);
    return [15, 30, 60][Math.min(n - 1, 2)] * 60 * 1000;
  }

  return {
    BANNED, GREETING_WORDS,
    getPeriod, bannedHits, hasBanned,
    normalizeContent, sanitizeContent, retryDelayMs,
  };
});
