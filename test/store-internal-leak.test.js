'use strict';
/*
 * 缺陷回归：关键词「内部泄漏」消息被**加载时无条件静默永久删除**
 *   store.js: purgeInternalLeakMessages() 在 normalize() 里 filter 掉消息 → load() 立刻 save() 写回
 *   ⇒ 无日志、无备份、不可恢复；另有 store.js addMessage() 命中判据时 `return msg` 静默丢弃用户输入。
 *
 * 本文件锁死的不变量（控制器裁决 ①②③⑤）：
 *   1) load()+normalize() **永不删除**任何消息（只打标 / 读时隐藏）；
 *   2) 带结构化标记 internal:true 的内部消息：读时隐藏、存储保留；
 *   3) 旧关键词脏数据：读时隐藏、存储保留；
 *   4) 强制记忆抽取的关键词支收窄（「我的想法是这样」「我的意思是」不再误触发，真事实形态仍触发）+ 有上限；
 *   5) 读路径不写盘、写回不删行；发送路径不再静默丢弃用户文本（可见报警 + 计数）。
 *
 * 运行：node --test --test-reporter=tap test/store-internal-leak.test.js
 */
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const STORE_PATH = path.join(__dirname, '..', 'public', 'js', 'store.js');
const CHAT_PATH = path.join(__dirname, '..', 'public', 'js', 'chat.js');
const CORE_PATH = path.join(__dirname, '..', 'public', 'js', 'internal-msg-core.js');
const KEY = 'aiwechat:v9';

/* ---------- 测试夹具 ---------- */

// 判据 A：两个关键词起行（旧实现判死）
const LEGACY_MULTI = {
  id: 'legacy-multi', role: 'user', ts: 1000, status: 'sent',
  content: '关于用户\n· 喜欢喝奶茶\n近期心情\n· 今天有点累',
};
// 判据 B：全角括号 + 时间词（旧实现判死）
const LEGACY_PAREN = {
  id: 'legacy-paren', role: 'user', ts: 2000, status: 'sent',
  content: '（空闲时间到了,你主动给宝发一条消息。当前时间:1:30）',
};
// 普通消息：任何实现下都必须保留且可见
const NORMAL = {
  id: 'normal', role: 'user', ts: 3000, status: 'sent',
  content: '今天天气不错，出去走了走。',
};
// 结构化标记的内部消息：内容**不命中**任何关键词，只有 internal:true —— 用来隔离"标记"与"关键词"
const MARKED = {
  id: 'marked', role: 'user', ts: 4000, status: 'sent',
  content: '内部任务轮：生成本轮主动消息（无正文关键词）', internal: true,
};

function seedData(messages) {
  return { version: 9, contacts: [], conversations: [], messages: { cv1: messages }, settings: {} };
}

function freshCore() {
  const warns = [];
  const origWarn = console.warn;
  console.warn = (...a) => { warns.push(a.map((x) => String(x)).join(' ')); };
  try {
    delete require.cache[require.resolve(CORE_PATH)];
    const core = require(CORE_PATH);
    return { core, warns, restore() { console.warn = origWarn; } };
  } catch (e) {
    console.warn = origWarn;
    throw e;
  }
}

/** 建一个干净的宿主环境（window/localStorage），加载 core + store，跑完务必 cleanup。 */
function withStore(seed, fn) {
  const bag = new Map();
  if (seed !== undefined) bag.set(KEY, JSON.stringify(seed));
  const localStorage = {
    getItem: (k) => (bag.has(k) ? bag.get(k) : null),
    setItem: (k, v) => { bag.set(k, String(v)); },
    removeItem: (k) => { bag.delete(k); },
  };
  const win = { localStorage, alert() {} };
  const warns = [];
  const origWarn = console.warn;
  console.warn = (...a) => { warns.push(a.map((x) => String(x)).join(' ')); };
  global.window = win;
  global.localStorage = localStorage;
  try {
    // 核心模块是**被测对象之一**：缺失时 store.js 应"宁可显示不误藏"，用例 2/3 会因此变红
    // （这里不硬 require，是为了让用例 1/5 针对"加载即删除"这条真缺陷给出可读的红，而不是 MODULE_NOT_FOUND）
    if (fs.existsSync(CORE_PATH)) {
      delete require.cache[require.resolve(CORE_PATH)];
      require(CORE_PATH);
    }
    delete require.cache[require.resolve(STORE_PATH)];
    require(STORE_PATH);
    assert.ok(win.Store, 'store.js 没有挂出 window.Store');
    return fn({
      Store: win.Store,
      warns,
      /** 磁盘（localStorage）里现在真实存着什么 */
      persisted: () => JSON.parse(bag.get(KEY) || 'null'),
      rawText: () => bag.get(KEY),
      warnText: () => warns.join('\n'),
    });
  } finally {
    console.warn = origWarn;
    delete global.window;
    delete global.localStorage;
    delete require.cache[require.resolve(STORE_PATH)];
    if (fs.existsSync(CORE_PATH)) delete require.cache[require.resolve(CORE_PATH)];
  }
}

/* ============ 用例 1：加载不再删除（旧实现必红）============ */
test('用例1: 正文含两个标题词起行的用户消息，load()+normalize() 之后仍然存在', () => {
  withStore(seedData([LEGACY_MULTI, NORMAL]), (env) => {
    const d = env.Store.load();       // 触发 normalize()（+ save() 写回）

    // 内存里还在
    assert.strictEqual(d.messages.cv1.length, 2, '加载后内存里的消息被删了');
    assert.ok(
      d.messages.cv1.some((m) => m.content === LEGACY_MULTI.content),
      '正文含两个标题词的消息在 normalize() 里被删除了（本缺陷正是"加载即永久删除"）',
    );

    // localStorage 里也还在
    const persisted = env.persisted();
    assert.strictEqual(persisted.messages.cv1.length, 2, '写回 localStorage 时消息被删了');
    assert.ok(
      persisted.messages.cv1.some((m) => m.id === 'legacy-multi' && m.content === LEGACY_MULTI.content),
      'localStorage 里已经找不到那条消息的正文（不可恢复）',
    );
    assert.strictEqual(persisted.messages.cv1.find((m) => m.id === 'normal').content, NORMAL.content);
  });
});

/* ============ 用例 2：结构化标记 → 隐藏但不删除 ============ */
test('用例2: internal:true 的消息被隐藏，但仍留在存储里（标记是唯一权威判据）', () => {
  withStore(seedData([MARKED, NORMAL]), (env) => {
    env.Store.load();

    const visible = env.Store.getMessages('cv1');
    assert.deepStrictEqual(
      visible.map((m) => m.id), ['normal'],
      'internal:true 的消息没有被隐藏（或普通消息被误藏）',
    );

    const persisted = env.persisted();
    assert.strictEqual(persisted.messages.cv1.length, 2, 'internal:true 的消息被从存储里删掉了');
    assert.strictEqual(persisted.messages.cv1.find((m) => m.id === 'marked').content, MARKED.content);
  });
});

/* ============ 用例 3：旧关键词脏数据 → 隐藏但不删除 ============ */
test('用例3: 旧关键词脏数据被隐藏，但存储里保留原行（只打标，不删除）', () => {
  withStore(seedData([LEGACY_MULTI, LEGACY_PAREN, NORMAL]), (env) => {
    env.Store.load();

    const visible = env.Store.getMessages('cv1').map((m) => m.id);
    assert.deepStrictEqual(visible, ['normal'], '旧关键词脏数据没有被隐藏（或普通消息被误藏）');

    const persisted = env.persisted();
    assert.strictEqual(persisted.messages.cv1.length, 3, '旧关键词脏数据被删除了');
    assert.strictEqual(persisted.messages.cv1.find((m) => m.id === 'legacy-multi').content, LEGACY_MULTI.content);
    assert.strictEqual(persisted.messages.cv1.find((m) => m.id === 'legacy-paren').content, LEGACY_PAREN.content);
    // 打标要可追溯（哪一条、为什么被隐藏）
    assert.strictEqual(persisted.messages.cv1.find((m) => m.id === 'legacy-multi').internal, true);
  });
});

/* ============ 用例 4：强制抽取关键词支收窄 ============ */
test('用例4a: 强制抽取关键词不再被日常句误触发，真事实形态仍然触发', () => {
  const { core, restore } = freshCore();
  try {
    assert.strictEqual(typeof core.looksLikeFactStatement, 'function', 'core 缺少 looksLikeFactStatement');
    // 取证报告点名的误触发句：必须不再触发
    assert.strictEqual(core.looksLikeFactStatement('我的想法是这样'), false, '「我的想法是这样」仍被当成事实');
    assert.strictEqual(core.looksLikeFactStatement('我的意思是'), false, '「我的意思是」仍被当成事实');
    assert.strictEqual(core.looksLikeFactStatement('我的意思是先这样吧'), false);
    assert.strictEqual(core.looksLikeFactStatement('我的想法是这样吧'), false);
    // 太短不给过（force 不允许绕过长度的意义在这里）
    assert.strictEqual(core.looksLikeFactStatement('哦'), false);
    // 真事实形态：仍然必须触发
    assert.strictEqual(core.looksLikeFactStatement('我对花生过敏'), true, '真事实被漏掉了');
    assert.strictEqual(core.looksLikeFactStatement('记住我下周三体检'), true);
    assert.strictEqual(core.looksLikeFactStatement('我叫林小满'), true);
    assert.strictEqual(core.looksLikeFactStatement('我的生日是五月三号'), true);
    assert.strictEqual(core.looksLikeFactStatement('我喜欢喝无糖豆浆'), true);
  } finally { restore(); }
});

test('用例4b: 强制抽取有轮数兜底 + 窗口内次数上限（force 不再被短句无限刷）', () => {
  const { core, restore } = freshCore();
  try {
    assert.strictEqual(typeof core.decideForceExtract, 'function', 'core 缺少 decideForceExtract');
    const W = core.FORCE_WINDOW_MS;
    assert.ok(W > 0, 'core 缺少 FORCE_WINDOW_MS');

    // 轮数兜底：第 5 轮强制
    let d = core.decideForceExtract({ text: '嗯', roundsSinceExtract: 4, recentForceTs: [], now: 1e6 });
    assert.strictEqual(d.force, true);
    assert.strictEqual(d.reason, 'rounds');
    assert.strictEqual(d.next, 5);

    // 关键词支：真事实 → 强制，并记一次时间戳
    d = core.decideForceExtract({ text: '我对花生过敏', roundsSinceExtract: 0, recentForceTs: [], now: 1e6 });
    assert.strictEqual(d.force, true);
    assert.strictEqual(d.reason, 'fact');
    assert.deepStrictEqual(d.recentForceTs, [1e6]);

    // 日常句 → 不强制（这一轮依然会做"非强制抽取"，只是不绕长度门）
    d = core.decideForceExtract({ text: '我的想法是这样', roundsSinceExtract: 0, recentForceTs: [], now: 1e6 });
    assert.strictEqual(d.force, false);
    assert.strictEqual(d.reason, 'none');

    // 窗口内刷满上限 → 降级为非强制（不丢这一轮抽取，只是不再 bypass）
    const full = [1e6, 1e6 + 1000, 1e6 + 2000];
    d = core.decideForceExtract({ text: '我对花生过敏', roundsSinceExtract: 0, recentForceTs: full, now: 1e6 + 3000 });
    assert.strictEqual(d.force, false);
    assert.strictEqual(d.reason, 'capped');

    // 窗口外的旧记录不再占用配额
    d = core.decideForceExtract({ text: '我对花生过敏', roundsSinceExtract: 0, recentForceTs: [1e6 - W - 1], now: 1e6 });
    assert.strictEqual(d.force, true);
    assert.strictEqual(d.reason, 'fact');
  } finally { restore(); }
});

/* ============ 用例 5：加载写回不删行 + 读路径不写盘 ============ */
test('用例5: load() 的写回不删除任何行（持久化数组长度不变），读路径也不写盘', () => {
  withStore(seedData([LEGACY_MULTI, LEGACY_PAREN, NORMAL]), (env) => {
    const before = env.persisted();
    assert.strictEqual(before.messages.cv1.length, 3);

    env.Store.load();

    const after = env.persisted();
    assert.strictEqual(after.messages.cv1.length, 3, 'load() 的写回删除了行（静默丢数据的根因）');
    assert.deepStrictEqual(
      after.messages.cv1.map((m) => m.id).sort(),
      ['legacy-multi', 'legacy-paren', 'normal'],
      '写回后行的集合变了',
    );

    // 读路径不得改盘
    const snapshot = env.rawText();
    env.Store.getMessages('cv1');
    env.Store.getMessages('cv1');
    assert.strictEqual(env.rawText(), snapshot, 'getMessages() 改写了 localStorage');
  });
});

/* ============ ② 发送路径不再静默丢弃 ============ */
test('②: 命中内部形状的消息在 addMessage 时不再被静默丢弃（入库 + 可见报警 + 计数）', () => {
  withStore(seedData([]), (env) => {
    env.Store.load();

    const outgoing = {
      id: 'outgoing', role: 'user', ts: 5000, status: 'sent',
      content: '关于用户偏好\n近期事件时间线\n这两块的字段对不上，帮我看下',
    };
    env.Store.addMessage('cv1', outgoing);

    const persisted = env.persisted();
    assert.strictEqual(persisted.messages.cv1.length, 1, '用户发出的消息在写入时被静默丢弃了（旧缺陷）');
    assert.strictEqual(persisted.messages.cv1[0].content, outgoing.content, '正文没有被原样保留');

    // 用户刚发的那条必须仍然可见（不能"发完就消失"）
    assert.deepStrictEqual(env.Store.getMessages('cv1').map((m) => m.id), ['outgoing']);

    // 可见报警 + 计数（旧实现零输出）
    assert.ok(/hidden|隐藏|内部|内摘要|保存但/.test(env.warnText()), '没有任何 console.warn 提示：' + env.warnText());
    const report = env.Store.internalMsgReport();
    assert.ok(report && report.suspectedOnSend >= 1, '没有计数：' + JSON.stringify(report));
  });
});

test('②: 明确标记 internal:true 的消息写入后隐藏，但同样入库（不丢弃）', () => {
  withStore(seedData([]), (env) => {
    env.Store.load();
    env.Store.addMessage('cv1', { id: 'task', role: 'user', ts: 6000, content: '内部任务轮：生成主动消息', internal: true });
    env.Store.addMessage('cv1', { id: 'real', role: 'user', ts: 6001, content: '在吗' });

    assert.deepStrictEqual(env.Store.getMessages('cv1').map((m) => m.id), ['real'], '标记消息没有被隐藏');
    assert.strictEqual(env.persisted().messages.cv1.length, 2, '标记消息没有入库');
  });
});

/* ============ ③ 前端单一来源 + ⑤ 删除路径必须消失 ============ */
// 源码断言只看**代码**：注释里记载旧实现（含旧正则/旧函数名）是允许且必要的，先剥掉注释再断言
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '');
}

test('③: 关键词判据在前端只有一份（store.js/chat.js 都从 internal-msg-core.js 取）', () => {
  const storeCode = stripComments(fs.readFileSync(STORE_PATH, 'utf8'));
  const chatCode = stripComments(fs.readFileSync(CHAT_PATH, 'utf8'));

  assert.ok(storeCode.includes('InternalMsgCore'), 'store.js 没有引用共享核心模块');
  assert.ok(chatCode.includes('InternalMsgCore'), 'chat.js 没有引用共享核心模块');
  assert.strictEqual(
    storeCode.includes('近期心情'), false,
    'store.js 里还自带一份关键词表（必须收敛到 internal-msg-core.js）',
  );
  assert.strictEqual(
    chatCode.includes('我过敏|我喜欢|我不喜欢'), false,
    'chat.js 里还自带旧的过宽正则（必须收敛到 internal-msg-core.js）',
  );
  assert.ok(chatCode.includes('_decideForceExtract'), 'chat.js 的强制抽取判据没有走核心模块');
  assert.ok(
    /_decideForceExtract\(\{[\s\S]{0,240}?roundsSinceExtract/.test(chatCode),
    'chat.js 没有把轮数与正文交给核心模块判定（force 判据可能又内联回去了）',
  );
});

test('⑤: 加载路径里不再存在任何删除消息的代码（防回归）', () => {
  const storeCode = stripComments(fs.readFileSync(STORE_PATH, 'utf8'));
  assert.strictEqual(
    storeCode.includes('purgeInternalLeakMessages'), false,
    'store.js 又出现了 purgeInternalLeakMessages（加载时删除）',
  );
  assert.strictEqual(
    /messages\[[^\]]*\]\s*=\s*filtered/.test(storeCode), false,
    'store.js 又把过滤后的数组写回 messages（删除并落盘）',
  );
  assert.strictEqual(
    /isInternalLeakMessage\s*\(/.test(storeCode), false,
    'store.js 又用关键词判据在写入侧丢弃消息',
  );
});
