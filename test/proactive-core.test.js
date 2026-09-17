'use strict';
const test = require('node:test');
const assert = require('node:assert');
const core = require('../public/js/proactive-core.js');

/* ★ 2026-09-14 改：用户拍板「禁止模板，发什么话由模型决定」。
   这里原来的两条用例（"禁词会被程序化改写" / "没提问自动补一个钩子"）
   测的正是被删掉的两个模板行为（固定句替换、追加"，你呢？"），
   现在改成测新语义：只检测、不改写、不追加。 */

test('禁词只做检测，不改写成固定句', () => {
  const hits = core.bannedHits('早安，在干嘛？吃了吗？');
  assert.deepStrictEqual(hits.sort(), ['在干嘛', '吃了吗', '早安'].sort());
  assert.strictEqual(core.hasBanned('早安，在干嘛？'), true);
  assert.strictEqual(core.hasBanned('刚刷到一只会握手的小猫。'), false);
});

test('「想你了」不再是禁词（用户允许直接表达思念）', () => {
  assert.strictEqual(core.hasBanned('宝，今天有点想你了。'), false);
});

test('问候场景放行早晚安，但仍拦查岗式空话', () => {
  assert.strictEqual(core.hasBanned('晚安呀，今天辛苦了', { allowGreetings: true }), false);
  assert.strictEqual(core.hasBanned('早呀，新的一天慢慢来', { allowGreetings: true }), false);
  assert.strictEqual(core.hasBanned('早安，在干嘛呢', { allowGreetings: true }), true);
});

test('清理不再追加任何固定钩子', () => {
  const out = core.normalizeContent('刚刷到一只会握手的小猫，尾巴翘得像天线。', { maxChars: 100 });
  assert.strictEqual(out, '刚刷到一只会握手的小猫，尾巴翘得像天线。');
  assert.strictEqual((out.match(/？/g) || []).length, 0);
});

test('最多保留一个问句，多余问号变句号', () => {
  const out = core.normalizeContent('你今天出门了没？吃了啥？累不累？', { maxChars: 100 });
  assert.strictEqual((out.match(/？/g) || []).length, 1);
  assert.strictEqual((out.match(/。/g) || []).length, 2);
});

test('santizeContent 默认不因禁词丢弃；rejectBanned 才返回空串', () => {
  // 用户口径（改 #2）：禁词只影响风格，不该让她整轮不吭声
  assert.strictEqual(core.sanitizeContent('早安，在干嘛？', {}), '早安，在干嘛？');
  assert.strictEqual(core.sanitizeContent('宝，在干嘛？', { rejectBanned: true }), '');
  assert.strictEqual(
    core.sanitizeContent('晚安呀，今天辛苦了', { allowGreetings: true, rejectBanned: true }),
    '晚安呀，今天辛苦了',
  );
});

test('按长度截断到完整句', () => {
  const text = '今天看到好多有意思的小事，第一件想讲给你听。'.repeat(5);
  const out = core.normalizeContent(text, { maxChars: 60 });
  assert.ok(out.length <= 60);
  assert.ok(out.endsWith('。'));
});

test('失败退避按 15/30/60 分钟增长', () => {
  assert.strictEqual(core.retryDelayMs(1), 15 * 60 * 1000);
  assert.strictEqual(core.retryDelayMs(2), 30 * 60 * 1000);
  assert.strictEqual(core.retryDelayMs(5), 60 * 60 * 1000);
});

test('时段判断仍在（供 prompt 与门禁使用）', () => {
  assert.strictEqual(core.getPeriod(new Date(2026, 7, 26, 10)), 'morning');
  assert.strictEqual(core.getPeriod(new Date(2026, 7, 26, 23)), 'night');
});
