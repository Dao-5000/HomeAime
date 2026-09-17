'use strict';
/*
 * 应用内消息通知 单元测试（node:test 内置运行器，无需额外依赖）
 * 运行：node --test test/   或   npm test
 *
 * 说明：本文件覆盖 notify-core.js（应用内通知的「纯逻辑」单一真相源）。
 * 由于 app.js 的 inAppNotify 委托给这里的判定函数，测试本文件即等价于
 * 测试生产代码的触发 / 显示 / 关闭前置逻辑，避免重复与冲突。
 */
const test = require('node:test');
const assert = require('node:assert');
const NC = require('../public/js/notify-core.js');

// 构造确定性的 Date，避免依赖真实时钟
function at(h, m) {
  return new Date(2026, 0, 1, h, m, 0, 0);
}

test('模块导出完整', () => {
  const need = ['resolveAvatar', 'shouldShowInAppNotify', 'isInQuiet', 'isGlobalDnd', 'parseTimeRange', 'inActiveWindow'];
  for (const k of need) {
    assert.strictEqual(typeof NC[k], 'function', '缺少导出: ' + k);
  }
});

/* ============ 1) 应用内通知「是否该弹」触发判定（单一真相源）============ */
test('触发: 聚焦 + 会话未打开 + 非免打扰 → 弹', () => {
  assert.strictEqual(
    NC.shouldShowInAppNotify({ contact: {}, focused: true, conversationOpen: false }),
    true);
});

test('触发: 未聚焦（最小化 / 其他应用在前台）→ 不弹（满足需求 #2）', () => {
  assert.strictEqual(
    NC.shouldShowInAppNotify({ contact: {}, focused: false, conversationOpen: false }),
    false);
});

test('触发: 该会话已打开 → 不弹（消息已可见，避免重复提醒）', () => {
  assert.strictEqual(
    NC.shouldShowInAppNotify({ contact: {}, focused: true, conversationOpen: true }),
    false);
});

test('触发: 联系人在免打扰时段 → 不弹', () => {
  const contact = { quietStart: '22:00', quietEnd: '23:00' };
  // focused 且会话未打开，但处于 quiet → 不弹
  assert.strictEqual(
    NC.shouldShowInAppNotify({ contact, focused: true, conversationOpen: false, now: at(22, 30) }),
    false);
});

test('触发: 免打扰时段外 + 聚焦 + 会话未打开 → 弹', () => {
  const contact = { quietStart: '22:00', quietEnd: '23:00' };
  assert.strictEqual(
    NC.shouldShowInAppNotify({ contact, focused: true, conversationOpen: false, now: at(20, 0) }),
    true);
});

test('触发: 全局 DND 中普通通知不弹', () => {
  assert.strictEqual(NC.shouldShowInAppNotify({
    contact: {}, focused: true, conversationOpen: false, globalDnd: true,
  }), false);
});

/* ============ 2) 头像逻辑（仅 AI 聊天对象显示图片头像）============ */
test('头像: AI 聊天对象 + 头像URL → 图片头像', () => {
  assert.deepStrictEqual(
    NC.resolveAvatar({ name: '助手', avatar: 'http://x/a.png', isAI: true }),
    { mode: 'img', value: 'http://x/a.png' });
  // 主动消息默认视为 AI（isAI 未传）→ 仍显示图片
  assert.deepStrictEqual(
    NC.resolveAvatar({ name: '助手', avatar: 'http://x/a.png' }),
    { mode: 'img', value: 'http://x/a.png' });
});

test('头像: 非 AI 类型即使有头像也回退文字（修复点）', () => {
  const r = NC.resolveAvatar({ name: '群聊', avatar: 'http://x/g.png', isAI: false });
  assert.strictEqual(r.mode, 'text', '非 AI 发件人不显示图片头像');
  assert.strictEqual(r.value, '群', '回退为名称首字');
});

test('头像: 无头像 → 文字首字；默认回退 AI', () => {
  assert.deepStrictEqual(NC.resolveAvatar({ name: '助手' }), { mode: 'text', value: '骨' });
  assert.deepStrictEqual(NC.resolveAvatar({}), { mode: 'text', value: 'A' });
});

test('头像: 空字符串头像 → 文字', () => {
  assert.deepStrictEqual(NC.resolveAvatar({ name: '助手', avatar: '' }), { mode: 'text', value: '骨' });
});

/* ============ 3) 免打扰（Do Not Disturb）============ */
test('免打扰: 缺字段 / 起止相同 → 不限制', () => {
  assert.strictEqual(NC.isInQuiet({}), false);
  assert.strictEqual(NC.isInQuiet({ quietStart: '23:00' }), false);
  assert.strictEqual(NC.isInQuiet(null), false);
  assert.strictEqual(NC.isInQuiet({ quietStart: '22:00', quietEnd: '22:00' }, at(23, 0)), false);
});

test('免打扰: 同日内边界判定（含起点、不含终点）', () => {
  const c = { quietStart: '22:00', quietEnd: '23:30' };
  assert.strictEqual(NC.isInQuiet(c, at(22, 0)), true, 'cur == 起点 → 命中');
  assert.strictEqual(NC.isInQuiet(c, at(23, 0)), true, '区间内 → 命中');
  assert.strictEqual(NC.isInQuiet(c, at(23, 30)), false, 'cur == 终点 → 不含');
  assert.strictEqual(NC.isInQuiet(c, at(21, 59)), false, '区间前 → 不命中');
});

test('免打扰: 跨天时段（23:00 - 07:00）', () => {
  const c = { quietStart: '23:00', quietEnd: '07:00' };
  assert.strictEqual(NC.isInQuiet(c, at(2, 0)), true);
  assert.strictEqual(NC.isInQuiet(c, at(6, 59)), true);
  assert.strictEqual(NC.isInQuiet(c, at(7, 0)), false, '跨天终点不含');
  assert.strictEqual(NC.isInQuiet(c, at(12, 0)), false, '白天不命中');
  assert.strictEqual(NC.isInQuiet(c, at(23, 0)), true, '跨天起点命中');
});

test('全局 DND: 默认跨天边界与关闭开关', () => {
  const cfg = { DND_ENABLED: true, DND_START: '23:00', DND_END: '07:00' };
  assert.strictEqual(NC.isGlobalDnd(cfg, at(23, 0)), true);
  assert.strictEqual(NC.isGlobalDnd(cfg, at(6, 59)), true);
  assert.strictEqual(NC.isGlobalDnd(cfg, at(7, 0)), false);
  assert.strictEqual(NC.isGlobalDnd({ ...cfg, DND_ENABLED: false }, at(23, 30)), false);
});

/* ============ 4) 全局主动发言时间窗口 ============ */
test('全局窗口: parseTimeRange 合法/非法', () => {
  assert.deepStrictEqual(NC.parseTimeRange('8-23'), [8, 0, 23, 0]);
  assert.deepStrictEqual(NC.parseTimeRange('08:00-23:00'), [8, 0, 23, 0]);
  assert.strictEqual(NC.parseTimeRange('abc'), null);
  assert.strictEqual(NC.parseTimeRange('25-1'), null, '小时越界');
  assert.strictEqual(NC.parseTimeRange(''), null);
});

test('全局窗口: inActiveWindow 同日内与跨天', () => {
  assert.strictEqual(NC.inActiveWindow('8-23', 8 * 60), true, '起点命中');
  assert.strictEqual(NC.inActiveWindow('8-23', 22 * 60 + 59), true);
  assert.strictEqual(NC.inActiveWindow('8-23', 23 * 60), false, '终点不含');
  assert.strictEqual(NC.inActiveWindow('8-23', 0), false, '窗口外');
  assert.strictEqual(NC.inActiveWindow('22:00-06:00', 2 * 60), true, '跨天命中');
  assert.strictEqual(NC.inActiveWindow('22:00-06:00', 12 * 60), false);
  assert.strictEqual(NC.inActiveWindow('', 100), true, '非法 → 不限制');
  assert.strictEqual(NC.inActiveWindow('garbage', 100), true, '非法 → 不限制');
});
