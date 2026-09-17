'use strict';
/* 前端主动间隔读取逻辑的真实单测（从 app.js 源码里抽取 _proactivePair 求值）。

   为什么这么测：主动消息间隔以前有三套口径（前端 max*0.3 / 后端 max*0.5 / 人格设置
   另一套），"设了 60-120 却发得又快又乱"就是这么来的。现在前端只认
   window.__pcConfig 的 IDLE_TRIGGER_MIN/MAX_MINUTES，本测把这段逻辑抽出来跑真实取值，
   而不是靠"看代码觉得对"。

   注意：抽取失败必须**报错**，不许静默跳过 —— 否则测试会因为"没测到"而假通过。 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const APP_JS = path.join(__dirname, '..', 'public', 'js', 'app.js');
const src = fs.readFileSync(APP_JS, 'utf8');

function extractFunction(source, name) {
  const start = source.indexOf('function ' + name + '(');
  assert.notStrictEqual(start, -1, `没在 app.js 里找到 function ${name}() —— 抽取逻辑需要更新`);
  // 大括号配对找函数结尾（够用：函数体内没有裸字符串里的不平衡括号）
  let i = source.indexOf('{', start);
  assert.notStrictEqual(i, -1, `${name} 没有函数体`);
  let depth = 0;
  for (let j = i; j < source.length; j++) {
    const ch = source[j];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return source.slice(start, j + 1);
    }
  }
  throw new Error(`${name} 函数体没有闭合`);
}

const pairSrc = extractFunction(src, '_proactivePair');
assert.ok(pairSrc.includes('IDLE_TRIGGER_MIN_MINUTES'), '抽取到的 _proactivePair 没读全局间隔 key');

function callPair(pcConfig) {
  const fn = new Function('window', pairSrc + '; return _proactivePair();');
  return fn({ __pcConfig: pcConfig });
}

test('前端间隔读全局设置（60/120）', () => {
  assert.deepStrictEqual(callPair({ IDLE_TRIGGER_MIN_MINUTES: 60, IDLE_TRIGGER_MAX_MINUTES: 120 }), [60, 120]);
});

test('前端间隔回退到后端默认（配置缺失/非法时 20/40）', () => {
  assert.deepStrictEqual(callPair({}), [20, 40]);
  assert.deepStrictEqual(callPair(null), [20, 40]);
  assert.deepStrictEqual(callPair({ IDLE_TRIGGER_MIN_MINUTES: 0, IDLE_TRIGGER_MAX_MINUTES: 0 }), [20, 40]);
  // 上下限颠倒 → 视为非法，回落默认（不静默取反，避免"设 120-60 结果跑 60-120"这种静默行为）
  assert.deepStrictEqual(callPair({ IDLE_TRIGGER_MIN_MINUTES: 120, IDLE_TRIGGER_MAX_MINUTES: 60 }), [20, 40]);
});

test('前端不再自己排期（源码里不允许再出现 nextProactive 读取）', () => {
  const readNext = /now\s*>=\s*\(?Number\(c\.nextProactive\)/.test(src);
  assert.strictEqual(readNext, false, 'app.js 又出现了用 nextProactive 自己计时（两层时钟会把间隔拉长）');
});

test('定句模板库确实已删除，且新来源机制在位', () => {
  assert.strictEqual(src.includes('PROACTIVE_FORMATS'), false, 'app.js 里还有旧定句模板库');
  assert.ok(src.includes('PROACTIVE_SOURCES'), 'app.js 缺少新的素材来源定义');
  assert.ok(src.includes('素材归属铁律'), 'app.js 缺少记忆归属铁律');
});
