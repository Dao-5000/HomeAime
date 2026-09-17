/* 「干活」窗口纯函数单测：node --test test/agent-window.test.js */
const test = require('node:test');
const assert = require('node:assert');
const AW = require('../public/js/agent_window.js');

test('reduce: 聊天消息进 messages', () => {
  let s = AW.reduce(AW.initialState(), { type: 'agent_message', text: '宝，我回来了' });
  assert.strictEqual(s.messages.length, 1);
  assert.strictEqual(s.messages[0].text, '宝，我回来了');
});

test('reduce: 工具调用进 trail 且带参数摘要', () => {
  let s = AW.reduce(AW.initialState(), {
    type: 'agent_tool', tool: 'write', tool_call_id: 'tc1',
    args: { file_path: 'D:/x/y.py', content: 'print(1)' }, status: 'in_progress'
  });
  assert.strictEqual(s.trail.length, 1);
  assert.strictEqual(s.trail[0].tool, 'write');
  assert.ok(s.trail[0].summary.includes('y.py'), s.trail[0].summary);
});

test('reduce: 结果按 tool_call_id 关联回同一条 trail（不新增行）', () => {
  let s = AW.initialState();
  s = AW.reduce(s, { type: 'agent_tool', tool: 'pwsh', tool_call_id: 'tc9', args: { command: 'node -v' } });
  s = AW.reduce(s, { type: 'agent_result', tool: 'pwsh', tool_call_id: 'tc9', ok: true, result: 'v24.15.0' });
  assert.strictEqual(s.trail.length, 1);
  assert.strictEqual(s.trail[0].ok, true);
  assert.ok(s.trail[0].result.includes('v24.15.0'));
});

test('reduce: 失败结果标记 ok=false 并保留原始错误', () => {
  let s = AW.initialState();
  s = AW.reduce(s, { type: 'agent_tool', tool: 'write', tool_call_id: 'tc2', args: {} });
  s = AW.reduce(s, { type: 'agent_result', tool: 'write', tool_call_id: 'tc2', ok: false, status: 'failed',
                     result: 'Error: [sandbox: file access denied under workspace-write mode]' });
  assert.strictEqual(s.trail[0].ok, false);
  assert.ok(s.trail[0].result.includes('sandbox'));
});

test('reduce: 计划卡与审批卡各自独立占位', () => {
  let s = AW.initialState();
  s = AW.reduce(s, { type: 'agent_plan', plan: { steps: ['读文件', '改文件'], risk: '只动工作区', eta: '30 秒' } });
  assert.strictEqual(s.plan.steps.length, 2);
  s = AW.reduce(s, { type: 'agent_approval', approval: { id: 'ap1', summary: '要在工作区外动文件：D:/x.txt' } });
  assert.strictEqual(s.approval.id, 'ap1');
});

test('reduce: 用量更新写进 usage', () => {
  const s = AW.reduce(AW.initialState(), { type: 'agent_usage', used: 13700, size: 1000000, cost: null });
  assert.strictEqual(s.usage.used, 13700);
});

test('reduce: 停止事件把状态置为 stopped 并保留已产出内容', () => {
  let s = AW.initialState();
  s = AW.reduce(s, { type: 'agent_message', text: '写到一半' });
  s = AW.reduce(s, { type: 'agent_stopped' });
  assert.strictEqual(s.status, 'stopped');
  assert.strictEqual(s.messages.length, 1);
});

test('interjectMode: 忙的时候是 queue（ACP 一次只允许一个 prompt）', () => {
  assert.strictEqual(AW.interjectMode(true), 'queue');
  assert.strictEqual(AW.interjectMode(false), 'idle');
});

test('planCardHTML: 转义 HTML，不能把她的文案当标签渲染', () => {
  const html = AW.planCardHTML({ steps: ['<img src=x onerror=alert(1)>'], risk: '<b>r</b>', eta: '1s' });
  assert.ok(!html.includes('<img'), html);
  assert.ok(html.includes('&lt;img'), html);
});

test('approvalCardHTML: 显示目标路径与她的理由，并带同意/拒绝按钮', () => {
  const html = AW.approvalCardHTML({ approval: { id: 'ap9', summary: '要写 D:/out/r.txt', detail: { path: 'D:/out/r.txt' } } });
  assert.ok(html.includes('D:/out/r.txt'), html);
  assert.ok(html.includes('data-aw-approve="ap9"'), html);
  assert.ok(html.includes('data-aw-reject="ap9"'), html);
});

/* ===== 下面 4 个是本次实现补的「无条件断言」（简报清单之外，防注入 / 防编造耗时） ===== */

test('approvalCardHTML: 升档申请要显示她的理由与申请的权限（R29）', () => {
  const html = AW.approvalCardHTML({
    approval: {
      id: 'ap10', summary: '要在工作区外写文件',
      detail: { path: 'D:/outside/x.txt', justification: '你说过要我把报告写到桌面', sandbox_permissions: 'danger-full-access' }
    }
  });
  assert.ok(html.includes('你说过要我把报告写到桌面'), html);
  assert.ok(html.includes('danger-full-access'), html);
  assert.ok(html.includes('D:/outside/x.txt'), html);
});

test('approvalCardHTML: summary 里的 HTML 也必须被转义', () => {
  const html = AW.approvalCardHTML({ approval: { id: 'ap11', summary: '<img src=x onerror=alert(1)>', detail: {} } });
  assert.ok(!html.includes('<img'), html);
  assert.ok(html.includes('&lt;img'), html);
});

test('messageCardHTML: 聊天正文按文本渲染，标签被转义', () => {
  const html = AW.messageCardHTML({ text: '<b>宝</b> 我回来了' });
  assert.ok(!html.includes('<b>'), html);
  assert.ok(html.includes('&lt;b&gt;'), html);
});

test('toolLine: 单行文案带工具/摘要/状态，耗时没给就留占位不编数字', () => {
  const line = AW.toolLine({ tool: 'write', args: { file_path: 'D:/x/y.py' }, status: 'in_progress' });
  assert.ok(line.startsWith('write · D:/x/y.py · '), line);
  assert.ok(line.includes('…'), line);
  assert.ok(!/\d/.test(line), line);   /* 没给耗时就不许出现数字（不编造耗时） */
  assert.strictEqual(AW.toolLine({ tool: 'pwsh', summary: 'echo hi', ok: false, status: 'failed' }), 'pwsh · echo hi · 出错');
  assert.strictEqual(AW.toolLine({ tool: 'pwsh', summary: 'echo hi', ok: true, ms: 1200 }), 'pwsh · echo hi · 1200ms');
  assert.strictEqual(AW.toolLine({ tool: 'read', summary: 'D:/a.txt', ok: true }), 'read · D:/a.txt · 完成');
});

test('trailCardHTML: 失败行加 err 类，工具原文被转义', () => {
  const html = AW.trailCardHTML([
    { tool: 'write', summary: '<script>x</script>', ok: false, status: 'failed' },
    { tool: 'read', summary: 'D:/a.txt', ok: true }
  ]);
  assert.ok(html.includes('aw-trail-row err'), html);
  assert.ok(!html.includes('<script>'), html);
});

/* ===== R57 审查补测 ===== */

test('reduce: 没有对应工具调用的结果（孤儿 result）自己补一行 trail', () => {
  /* WS 丢包/乱序时，result 可能先到或 tool 事件被吞 —— 不能静默丢掉结果 */
  const s = AW.reduce(AW.initialState(), {
    type: 'agent_result', tool: 'pwsh', tool_call_id: 'tc-orphan', ok: false,
    status: 'failed', result: 'Error: boom'
  });
  assert.strictEqual(s.trail.length, 1);
  assert.strictEqual(s.trail[0].tool_call_id, 'tc-orphan');
  assert.strictEqual(s.trail[0].tool, 'pwsh');
  assert.strictEqual(s.trail[0].ok, false);
  assert.strictEqual(s.trail[0].status, 'failed');
  assert.ok(s.trail[0].result.includes('boom'), s.trail[0].result);
});

test('reduce: agent_thinking 置 running，agent_final 收口到 done 并保留正文', () => {
  let s = AW.reduce(AW.initialState(), { type: 'agent_thinking' });
  assert.strictEqual(s.status, 'running');
  s = AW.reduce(s, { type: 'agent_message', text: '中间过程' });
  s = AW.reduce(s, { type: 'agent_final', answer: '干完了' });
  assert.strictEqual(s.status, 'done');
  assert.strictEqual(s.messages.length, 2);
  assert.strictEqual(s.messages[1].text, '干完了');
});

test('reduce: agent_error 置 failed 且把错误正文带进 messages', () => {
  let s = AW.reduce(AW.initialState(), { type: 'agent_thinking' });
  s = AW.reduce(s, { type: 'agent_error', text: 'ACP 连接断了' });
  assert.strictEqual(s.status, 'failed');
  assert.strictEqual(s.messages.length, 1);
  assert.strictEqual(s.messages[0].text, 'ACP 连接断了');
});

test('reduce: 畸形 plan/approval 不抛，原样带过去由卡片守卫兜住', () => {
  let s = AW.reduce(AW.initialState(), { type: 'agent_plan', plan: { steps: 'abc' } });
  assert.strictEqual(typeof AW.planCardHTML(s.plan), 'string');
  s = AW.reduce(s, { type: 'agent_approval', approval: 'oops' });
  /* 非对象一律归一成 null（不把字符串当对象存进 state） */
  assert.strictEqual(s.approval, null);
  assert.strictEqual(typeof AW.approvalCardHTML({ approval: s.approval }), 'string');
  assert.strictEqual(typeof AW.approvalCardHTML({ approval: 'oops' }), 'string');
  assert.strictEqual(typeof AW.approvalCardHTML({ approval: { detail: 'nope' } }), 'string');
});

test('reduce: state 不持有 WS 事件对象本身（浅拷贝，防 Task 11 原地改污染事件）', () => {
  const plan = { steps: ['a'], risk: 'r', eta: 'e' };
  const args = { file_path: 'D:/a.txt' };
  const approval = { id: 'ap1', summary: 's', detail: { path: 'D:/p' } };
  let s = AW.reduce(AW.initialState(), { type: 'agent_plan', plan });
  s = AW.reduce(s, { type: 'agent_approval', approval });
  s = AW.reduce(s, { type: 'agent_tool', tool: 'write', tool_call_id: 'tc1', args });
  assert.notStrictEqual(s.plan, plan);
  assert.notStrictEqual(s.approval, approval);
  assert.notStrictEqual(s.trail[0].args, args);
  /* ★ Task 9+10 复评：原来这一行是 `notStrictEqual(s.messages, AW.initialState().messages)`
     —— 两个不同的空数组天然不等，恒真，等于没测。换成「写 state 不回头改事件对象」的反向证明：
     把 copy() 改成 `return o`（直接存事件对象）这一条立刻变红。 */
  s.plan.touched = 1;                       /* 往 state 里写 */
  assert.strictEqual(plan.touched, undefined, 'state 上的写入不许污染 WS 事件里的 plan');
  approval.summary = '事件对象被改过';        /* 往事件里写 */
  assert.strictEqual(s.approval.summary, 's', 'state 里的 approval 是独立对象，不受事件对象改写影响');
});

test('planCardHTML/trailCardHTML: 畸形数据一律不抛（WS 畸形不许打断渲染）', () => {
  assert.strictEqual(typeof AW.planCardHTML({ steps: 'abc' }), 'string');
  assert.strictEqual(typeof AW.planCardHTML({ steps: { a: 1 } }), 'string');
  assert.strictEqual(typeof AW.planCardHTML(null), 'string');
  assert.strictEqual(typeof AW.trailCardHTML('abc'), 'string');
  assert.strictEqual(typeof AW.trailCardHTML(null), 'string');
  assert.strictEqual(AW.planCardHTML({ steps: 'abc' }).includes('<li>'), false);
  assert.strictEqual(AW.trailCardHTML('abc').includes('aw-trail-row'), false);
});

test('esc: 单引号也转义（属性上下文不得用单引号，见 agent_window.js 注释）', () => {
  const html = AW.approvalCardHTML({ approval: { id: "a'b", summary: "it's", detail: {} } });
  assert.ok(!html.includes("a'b"), html);
  assert.ok(html.includes('a&#39;b'), html);
});

/* ===== Task 11：视图联通后端（HTTP 接线 + WS 事件吸收） ===== */

test('summarizeArgs: pattern 参数走 pattern= 分支（原来没有用例）', () => {
  /* summarizeArgs 没导出，从 trail 的 summary 上验 */
  let s = AW.reduce(AW.initialState(), {
    type: 'agent_tool', tool: 'search_code', tool_call_id: 'tc-p', args: { pattern: 'push_to_session' }
  });
  assert.strictEqual(s.trail[0].summary, 'pattern=push_to_session');
  /* 优先级：file_path/path > pattern > query（三个都给时按这个顺序取，别退化成「调用工具」） */
  s = AW.reduce(s, { type: 'agent_tool', tool: 'search_code', tool_call_id: 'tc-pq',
                     args: { pattern: 'p', query: 'q' } });
  assert.strictEqual(s.trail[1].summary, 'pattern=p');
  s = AW.reduce(s, { type: 'agent_tool', tool: 'read_source', tool_call_id: 'tc-pp',
                     args: { file_path: 'D:/a.py', pattern: 'p' } });
  assert.strictEqual(s.trail[2].summary, 'D:/a.py');
});

test('endpoints: 六条 URL 与后端路由一致', () => {
  const e = AW.endpoints;
  assert.strictEqual(e.state, '/api/agent/window/state');
  assert.strictEqual(e.send, '/api/agent/window/send');
  assert.strictEqual(e.confirm, '/api/agent/window/confirm');
  assert.strictEqual(e.interject, '/api/agent/window/interject');
  assert.strictEqual(e.stop, '/api/agent/window/stop');
  assert.strictEqual(e.approve, '/api/agent/window/approve');
  assert.strictEqual(e.upgrade, '/api/agent/window/upgrade');
});

test('sendBody: 带上 session/character/cwd/mode 四个字段', () => {
  const b = AW.sendBody({ sessionId: 's1', characterId: 'c1', cwd: 'D:/p', text: '帮我查天气' });
  assert.deepStrictEqual(b, { session_id: 's1', character_id: 'c1', cwd: 'D:/p', text: '帮我查天气', mode: 'auto' });
});

test('interjectPayload: 插话只带 session/task/text/interrupt（缺省值也要能直接发）', () => {
  const b = AW.interjectPayload({ sessionId: 's1', taskId: 't-1', text: '顺手把日志也删了', interrupt: true });
  assert.deepStrictEqual(b, { session_id: 's1', task_id: 't-1', text: '顺手把日志也删了', interrupt: true });
  assert.deepStrictEqual(AW.interjectPayload(), { session_id: 'default', task_id: '', text: '', interrupt: false });
  /* interrupt 必须是真布尔值：后端 `bool(body.get("interrupt"))`，传字符串会恒真 */
  assert.deepStrictEqual(AW.interjectPayload({ interrupt: 'no' }).interrupt, true);
  assert.strictEqual(AW.interjectPayload({ interrupt: 0 }).interrupt, false);
});

/* ---------- 假 DOM：零参渲染契约（Task 9+10 复评补测） ----------
   契约：flow-adapter.js 是 `window[cfg.render]()` **零参**调用（见该文件 mountRealView），
   所以 renderAgent 必须自己 `document.getElementById('page-agent')`。
   这里用最小假 DOM 把这条钉死 —— 把 `const el = sectionEl || document.getElementById(...)`
   改回 `const el = sectionEl` 时本组用例必红。 */

function makeEl(id) {
  const el = {
    id: id || '', innerHTML: '', textContent: '', value: '', dataset: {}, _handlers: {}, __kids: {},
    addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); },
    click() { (this._handlers.click || []).forEach(fn => fn({ target: this })); },
    querySelector(sel) { return kidOf(this, sel); },
    querySelectorAll() { return []; },
    contains() { return false; }
  };
  return el;
}

function matchesSel(html, sel) {
  if (sel.charAt(0) === '#') return html.indexOf('id="' + sel.slice(1) + '"') >= 0;
  if (sel.charAt(0) === '.') return new RegExp('class="[^"]*\\b' + sel.slice(1) + '\\b').test(html);
  return html.indexOf(sel) >= 0;
}

/* 真实 DOM 里父节点 innerHTML 被整体重写后旧子节点就作废了；这里按 innerHTML 快照模拟重建，
   子节点自身被写入（如 #awStream.innerHTML = …）不影响缓存命中。 */
function kidOf(parent, sel) {
  const hit = parent.__kids[sel];
  if (hit && hit.__born === parent.innerHTML) return hit;
  if (!matchesSel(parent.innerHTML, sel)) return null;
  const kid = makeEl('');
  kid.__born = parent.innerHTML;
  parent.__kids[sel] = kid;
  return kid;
}

const tick = () => new Promise(r => setTimeout(r, 0));

/* 触发 section 上的事件委托：按 id（插话/权限档按钮）或按 dataset（卡片上的按钮） */
function clickId(node, id) {
  (node._handlers.click || []).forEach(fn => fn({ target: { id: id, dataset: {} } }));
}
function clickData(node, ds) {
  (node._handlers.click || []).forEach(fn => fn({ target: { dataset: ds } }));
}

/* 用「带 document 的一次 require」拿真正的 renderAgent/AgentWindowIngest：
   agent_window.js 只在 `typeof document !== 'undefined'` 时才把它们挂到 root 上（Node 里正常 require 不碰 DOM）。
   结束后清 require 缓存 + 摘掉挂上去的全局函数，避免污染后续用例。 */
async function withFakeDom(node, fn) {
  const p = require.resolve('../public/js/agent_window.js');
  const hadDoc = Object.prototype.hasOwnProperty.call(globalThis, 'document');
  const savedDoc = globalThis.document;
  const savedFetch = globalThis.fetch;
  /* 用例会往 globalThis 上挂 Session/Store/Chat（renderAgent 从这里读 session_id / 当前角色）——用例结束必须摘掉，
     否则后面「没挂 Session/Chat」的用例会继承上一条的桩，断言就变成假的。 */
  /* ★ Task 13：AgentWindowMirrorToChat 也挂在 root 上 —— 不摘掉的话下一条用例会拿到上一条的旧闭包
     （步数计数器还停在上一条的状态里），镜像用例就可能假绿。 */
  const touched = ['Session', 'Store', 'Chat', 'AgentWindowMirrorToChat'];
  const savedGlobals = touched.map(k => [k, Object.prototype.hasOwnProperty.call(globalThis, k), globalThis[k]]);
  globalThis.document = {
    getElementById: (id) => (id === 'page-agent' ? node : null),
    body: { dataset: { page: 'chat' } }        /* 页面标记不是 agent → ingest 不负责重画，逼 renderAgent 自己画 */
  };
  delete require.cache[p];
  const fresh = require(p);
  try {
    return await fn(globalThis, fresh);
  } finally {
    if (hadDoc) globalThis.document = savedDoc; else delete globalThis.document;
    globalThis.fetch = savedFetch;
    savedGlobals.forEach(([k, had, v]) => { if (had) globalThis[k] = v; else delete globalThis[k]; });
    delete require.cache[p];
    delete globalThis.renderAgent;
    delete globalThis.AgentWindowIngest;
    delete globalThis.AgentWindowMirrorToChat;
  }
}

test('renderAgent: 零参调用自己定位 #page-agent 并把状态画进去（flow-adapter 契约）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/p' }) });
    assert.strictEqual(typeof g.renderAgent, 'function');
    /* 认领语义：只有 agent_* 返回 true（ws_client.js 据此 return，别的处理器才不会被跳过） */
    assert.strictEqual(g.AgentWindowIngest({ type: 'proactive', content: '主动消息' }), false);
    assert.strictEqual(g.AgentWindowIngest(null), false);
    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_final', answer: 'x' }), true);
    /* 先灌一条 WS 事件（页面标记是 chat，ingest 不重画） */
    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_message', task_id: 't-1', text: '宝，我回来了' }), true);

    g.renderAgent();                            /* ← 零参，与 flow-adapter 的调用方式一致 */
    assert.ok(node.innerHTML.indexOf('awStream') >= 0, '骨架必须建在被定位到的 #page-agent 上：' + node.innerHTML);
    const st = node.querySelector('#awStream');
    assert.ok(st, '零参调用后必须有 #awStream');
    assert.ok(st.innerHTML.indexOf('宝，我回来了') >= 0, '状态里的消息必须被画进流：' + st.innerHTML);
  });
});

test('renderAgent: 目标节点有骨架但缺 #awStream 时不抛（畸形/半截 DOM 不许打断渲染）', async () => {
  const node = makeEl('page-agent');
  node.innerHTML = '<div class="aw-wrap"><div class="aw-bar">手：就绪</div></div>';
  await withFakeDom(node, async (g) => {
    g.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
    assert.strictEqual(node.querySelector('#awStream'), null);   /* 前提：确实没有流容器 */
    assert.doesNotThrow(() => g.renderAgent());
    assert.doesNotThrow(() => g.renderAgent(node));
  });
});

test('renderAgent: 派活收到 kind=started 时记住 task_id，「停」据此发 stop（HTTP 真接线）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      if (String(url).indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-77', plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();                               /* 等 /state 的 cwd 落到 sectionEl.__aw */

    const box = node.querySelector('#awText');
    box.value = '帮我查一下今天上海的天气';
    node.querySelector('#awSend').click();
    await tick();

    const send = calls.filter(c => c.url === '/api/agent/window/send');
    assert.strictEqual(send.length, 1, JSON.stringify(calls));
    assert.strictEqual(send[0].body.text, '帮我查一下今天上海的天气');
    assert.strictEqual(send[0].body.mode, 'auto');
    assert.strictEqual(send[0].body.cwd, 'D:/proj');           /* /state 拿到的 cwd 要带进 send */
    assert.strictEqual(box.value, '', '发出去后输入框要清空');

    node.querySelector('#awStop').click();                     /* kind=started 没记住 task_id 的话这里发不出去 */
    await tick();
    const stop = calls.filter(c => c.url === '/api/agent/window/stop');
    assert.strictEqual(stop.length, 1, 'started 分支必须记住 task_id，否则「停」是死的：' + JSON.stringify(calls));
    assert.strictEqual(stop[0].body.task_id, 't-77');
  });
});

test('renderAgent: 计划卡确认按钮打到 confirm 端点并带上 task_id', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      if (String(url).indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-88',
        plan: { steps: ['读 backend/agent/loop.py', '把 _MAX_STEPS 改成 20'], risk: '只动工作区', eta: '30 秒' } }) });
    };
    g.renderAgent();
    await tick();

    node.querySelector('#awText').value = '帮我把 backend/agent/loop.py 的 _MAX_STEPS 改成 20';
    node.querySelector('#awSend').click();
    await tick();

    const st = node.querySelector('#awStream');
    assert.ok(st.innerHTML.indexOf('aw-plan') >= 0, 'kind=plan 要出现计划卡：' + st.innerHTML);
    assert.ok(st.innerHTML.indexOf('_MAX_STEPS 改成 20') >= 0, st.innerHTML);

    /* 模拟点【就这么干】：真 DOM 里由 section 上的事件委托接住带 data-aw-confirm 的按钮 */
    const handlers = node._handlers.click || [];
    assert.ok(handlers.length >= 1, 'section 上要有事件委托监听');
    handlers.forEach(fn => fn({ target: { dataset: { awConfirm: '1' } } }));
    await tick();

    const cf = calls.filter(c => c.url === '/api/agent/window/confirm');
    assert.strictEqual(cf.length, 1, '确认必须打到 confirm 端点：' + JSON.stringify(calls));
    assert.strictEqual(cf[0].body.task_id, 't-88');
    assert.strictEqual(node.querySelector('#awStream').innerHTML.indexOf('aw-plan'), -1,
      '确认后计划卡要收起来（否则用户会以为没生效）');
  });
});

/* ===== Task 12：插话两档（排队 / 打断）+ 状态条 ===== */

test('interjectPayload: 默认排队（ACP 一次只允许一个 prompt，不能直接连发）', () => {
  assert.deepStrictEqual(AW.interjectPayload('t1', '等等，别动那个文件', false),
    { task_id: 't1', text: '等等，别动那个文件', interrupt: false });
});

test('interjectPayload: 打断模式带 interrupt=true', () => {
  assert.strictEqual(AW.interjectPayload('t1', '停', true).interrupt, true);
});

test('interjectPayload: 位置形式发的是用户原文（R58：队列存原文，不许包 brief）', () => {
  const p = AW.interjectPayload('t1', '别删 D:/a.txt，那是我要的', true);
  assert.strictEqual(p.text, '别删 D:/a.txt，那是我要的');
  assert.strictEqual(p.text.indexOf('【'), -1, 'text 只能是用户原话：' + p.text);
  assert.deepStrictEqual(AW.interjectPayload(''), { task_id: '', text: '', interrupt: false });
  /* Task 11 的「对象形式」是对外契约，不许因为 Task 12 改成位置参数就消失 */
  assert.deepStrictEqual(AW.interjectPayload({ sessionId: 's9', taskId: 't3', text: '原文', interrupt: false }),
    { session_id: 's9', task_id: 't3', text: '原文', interrupt: false });
});

test('statusBar: 用量按 used/size 如实显示，后端不给耗时就不许出现时长', () => {
  const line = AW.statusBar({ status: 'running', taskId: 't-1', usage: { used: 13700, size: 1000000 } });
  /* ★ R64：整行相等 —— 只写 `includes('13700')` 之类会被「后面又补了一段假耗时」蒙过去 */
  assert.strictEqual(line, '状态：running ｜ 用量 13700/1000000 tok ｜ t-1');
  assert.strictEqual(/毫秒|耗时|秒|elapsed|\bs\b|min/i.test(line), false, '没有 elapsed 字段就不许编时长：' + line);
});

test('statusBar: 没用量就不画 tok 段；used=0 照实显示 0（不吞成空）', () => {
  assert.strictEqual(AW.statusBar({ status: 'idle' }).indexOf('tok'), -1);
  assert.ok(AW.statusBar({ status: 'running', usage: { used: 0 } }).indexOf('0 tok') >= 0);
});

test('renderAgent: 忙碌时出现两档插话按钮，排队档发 interrupt=false 且带 session_id', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.Session = { getSessionId: () => 's-42' };
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      if (u.indexOf('/interject') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, mode: 'queue', pending: 2 }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-9',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我把日志清一下';
    node.querySelector('#awSend').click();
    await tick();

    const st = node.querySelector('#awStream');
    assert.ok(st.innerHTML.indexOf('id="awQueue"') >= 0, '任务在跑就必须有「等她说完了再说」：' + st.innerHTML);
    assert.ok(st.innerHTML.indexOf('id="awInterrupt"') >= 0, '任务在跑就必须有「打断并说」：' + st.innerHTML);

    node.querySelector('#awText').value = '等等，别动那个文件';
    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awQueue', dataset: {} } }));
    await tick();

    const ij = calls.filter(c => c.url === '/api/agent/window/interject');
    assert.strictEqual(ij.length, 1, '点排队档必须打一次 interject：' + JSON.stringify(calls));
    assert.deepStrictEqual(ij[0].body,
      { session_id: 's-42', task_id: 't-9', text: '等等，别动那个文件', interrupt: false });
    assert.strictEqual(node.querySelector('#awText').value, '', '插话发出去后输入框要清空');
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('已记下') >= 0,
      '排队成功要有回执，不能静默：' + node.querySelector('#awStream').innerHTML);
  });
});

test('renderAgent: 打断档发 interrupt=true（ACP 要先 cancel 再插队首）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/interject') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, mode: 'interrupt' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-11',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我改配置';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awText').value = '停，先别改';
    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awInterrupt', dataset: {} } }));
    await tick();

    const ij = calls.filter(c => c.url === '/api/agent/window/interject');
    assert.strictEqual(ij.length, 1, JSON.stringify(calls));
    assert.strictEqual(ij[0].body.interrupt, true, '打断档必须 interrupt=true：' + JSON.stringify(ij[0].body));
    assert.strictEqual(ij[0].body.text, '停，先别改');
    assert.strictEqual(ij[0].body.task_id, 't-11');
    /* ★ R64：原来这里查 '打断' —— 常驻按钮文案「打断并说」本身就含着这两个字，等于没断言。
       改成只属于打断回执的「这就跟她说」（把回执整段删掉必须变红）。 */
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('这就跟她说') >= 0,
      '打断成功要有回执：' + node.querySelector('#awStream').innerHTML);
  });
});

test('renderAgent: 插话被后端拒绝（停在进行中/任务没在跑）时把错误原文贴出来，不吞', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/interject') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: false, error: '任务已停止，这句没有发出去' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-12',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个测试';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awText').value = '顺便把日志删了';
    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awQueue', dataset: {} } }));
    await tick();

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('任务已停止，这句没有发出去') >= 0, '后端拒绝必须原样显示：' + html);
    assert.strictEqual(html.indexOf('已记下'), -1, '被拒绝了就不许显示「已记下」：' + html);
    /* ★ R64：没递进去就把原话还回输入框，别让他白打一遍 */
    assert.strictEqual(node.querySelector('#awText').value, '顺便把日志删了',
      '插话失败要把文本还回输入框：' + node.querySelector('#awText').value);
  });
});

test('renderAgent: 停掉之后两档插话按钮要消失（空闲时没有「插话」这个对象）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/stop') >= 0) return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-30',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑一下测试';
    node.querySelector('#awSend').click();
    await tick();
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('id="awQueue"') >= 0, '前提：运行中要有按钮');

    node.querySelector('#awStop').click();
    await tick();
    const html = node.querySelector('#awStream').innerHTML;
    assert.strictEqual(html.indexOf('id="awQueue"'), -1, '已经停了就不该还能插话：' + html);
    assert.strictEqual(html.indexOf('id="awInterrupt"'), -1, '已经停了就不该还能打断：' + html);
  });
});

test('renderAgent: 状态条照实显示 agent_usage 的 used/size，不出现任何时长', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => new Promise(() => {});        /* /state 永不回，免得它盖掉状态条 */
    g.renderAgent();
    g.document.body.dataset.page = 'agent';       /* 让 ingest 负责重画 */
    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_usage', used: 6600, size: 200000 }), true);
    const bar = node.querySelector('.aw-bar');
    assert.ok(bar, '状态条节点要在');
    /* ★ R64：整行相等（原来只查 contains，补一句假耗时也能混过去） */
    assert.strictEqual(bar.textContent, '状态：idle ｜ 用量 6600/200000 tok');
    assert.strictEqual(/毫秒|耗时|秒|elapsed|\bs\b|min/i.test(bar.textContent), false, bar.textContent);
  });
});

/* ===== Task 15：权限档与升档 UI（二段式） ===== */

test('upgradePayload: 升档必须二段式（先 need_confirm 再 confirmed）', () => {
  assert.deepStrictEqual(AW.upgradePayload('s1', true, false), { session_id: 's1', enable: true, confirmed: false });
  assert.deepStrictEqual(AW.upgradePayload('s1', true, true), { session_id: 's1', enable: true, confirmed: true });
});

test('upgradePayload: 缺 session_id 兜底 default，enable/confirmed 收成真布尔', () => {
  assert.deepStrictEqual(AW.upgradePayload(), { session_id: 'default', enable: false, confirmed: false });
  assert.deepStrictEqual(AW.upgradePayload('', 1, 'yes'), { session_id: 'default', enable: true, confirmed: true });
});

test('controlBarHTML: 工作目录/权限档/模型三段都渲染，项目级时按钮是「临时放开」', () => {
  const html = AW.controlBarHTML({ cwd: 'D:/proj', permissionMode: 'workspace-write',
    model: 'deepseek-chat', configOptions: [] });
  assert.ok(html.indexOf('D:/proj') >= 0, html);
  assert.ok(html.indexOf('data-aw-perm="workspace-write"') >= 0, html);
  assert.ok(html.indexOf('项目级') >= 0, html);
  assert.ok(html.indexOf('id="awUpgrade"') >= 0, html);
  assert.strictEqual(html.indexOf('id="awUpgradeBack"'), -1, '项目级时不该出现「改回项目级」：' + html);
  assert.ok(html.indexOf('deepseek-chat') >= 0, html);
});

test('controlBarHTML: 已放开时如实显示「临时放开」，按钮换成「改回项目级」', () => {
  const html = AW.controlBarHTML({ cwd: 'D:/proj', permissionMode: 'danger-full-access', configOptions: [] });
  assert.ok(html.indexOf('data-aw-perm="danger-full-access"') >= 0, html);
  assert.ok(html.indexOf('临时放开') >= 0, html);
  assert.ok(html.indexOf('id="awUpgradeBack"') >= 0, html);
  assert.strictEqual(html.indexOf('id="awUpgrade"'), -1, '已放开时不该还能再点「临时放开」：' + html);
});

test('controlBarHTML: 模型优先取 config_options 的当前值；都没有就说跟随默认，不编模型名', () => {
  const fromOpts = AW.controlBarHTML({ cwd: 'D:/p', permissionMode: 'workspace-write', model: '',
    configOptions: [{ id: 'model', name: '模型', currentValue: 'deepseek-reasoner',
                      options: [{ value: 'deepseek-reasoner', name: 'R' }] }] });
  assert.ok(fromOpts.indexOf('deepseek-reasoner') >= 0, fromOpts);
  const none = AW.controlBarHTML({ cwd: 'D:/p', permissionMode: 'workspace-write' });
  assert.ok(none.indexOf('跟随 harness 默认') >= 0, none);
  assert.strictEqual(/gpt|claude|deepseek/i.test(none), false, '不知道模型就不许编名字：' + none);
});

test('controlBarHTML: 畸形 config_options/cwd/model 不抛、不当标签渲染，未知档按项目级', () => {
  const html = AW.controlBarHTML({ cwd: '<img src=x>', permissionMode: '', model: '<b>m</b>', configOptions: 'oops' });
  assert.ok(html.indexOf('<img') === -1, html);
  assert.ok(html.indexOf('<b>') === -1, html);
  assert.ok(html.indexOf('data-aw-perm="workspace-write"') >= 0, '未知档要按最保守的项目级显示：' + html);
  assert.strictEqual(typeof AW.controlBarHTML(null), 'string');
});

test('controlBarHTML: 二段式确认用后端给的 message 原文 + 确认/取消两个按钮，且文案转义', () => {
  const msg = '放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？';
  const html = AW.controlBarHTML({ cwd: 'D:/p', permissionMode: 'workspace-write',
    pendingUpgrade: { enable: true, message: msg } });
  assert.ok(html.indexOf(msg) >= 0, html);
  assert.ok(html.indexOf('id="awUpgradeYes"') >= 0, html);
  assert.ok(html.indexOf('id="awUpgradeNo"') >= 0, html);
  const evil = AW.controlBarHTML({ pendingUpgrade: { enable: true, message: '<img src=x onerror=alert(1)>' } });
  assert.ok(evil.indexOf('<img') === -1, evil);
});

test('renderAgent: 控件条显示工作目录/权限档/模型，点「临时放开」只发第一段 confirmed=false', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write', model: '',
          config_options: [{ id: 'model', name: '模型', currentValue: 'deepseek-chat', options: [] }] }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: true,
        message: '放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？' }) });
    };
    g.renderAgent();
    await tick();

    const ctl = node.querySelector('#awCtl');
    assert.ok(ctl, '控件条节点要在');
    assert.ok(ctl.innerHTML.indexOf('D:/proj') >= 0, ctl.innerHTML);
    assert.ok(ctl.innerHTML.indexOf('data-aw-perm="workspace-write"') >= 0, ctl.innerHTML);
    assert.ok(ctl.innerHTML.indexOf('deepseek-chat') >= 0, ctl.innerHTML);
    assert.strictEqual(ctl.innerHTML.indexOf('id="awUpgradeYes"'), -1, '还没点之前不该有确认按钮');

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgrade', dataset: {} } }));
    await tick();

    const up = calls.filter(c => c.url === '/api/agent/window/upgrade');
    assert.strictEqual(up.length, 1, '第一段只许发一次：' + JSON.stringify(calls));
    assert.deepStrictEqual(up[0].body, { session_id: 'default', enable: true, confirmed: false });
    const after = node.querySelector('#awCtl').innerHTML;
    assert.ok(after.indexOf('放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？') >= 0,
      '后端的确认文案要原样出现：' + after);
    assert.ok(after.indexOf('id="awUpgradeYes"') >= 0, after);
    assert.ok(after.indexOf('data-aw-perm="workspace-write"') >= 0, '还没确认就不许显示成已放开：' + after);
  });
});

test('renderAgent: 点确认才发第二段 confirmed=true，回执的 permission_mode 落到控件条', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      const body = (opt && opt.body) ? JSON.parse(opt.body) : {};
      if (body.confirmed) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: false,
          permission_mode: 'danger-full-access' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: true,
        message: '放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？' }) });
    };
    g.renderAgent();
    await tick();

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgrade', dataset: {} } }));
    await tick();
    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgradeYes', dataset: {} } }));
    await tick();

    const up = calls.filter(c => c.url === '/api/agent/window/upgrade');
    assert.strictEqual(up.length, 2, '确认后才该有第二段：' + JSON.stringify(calls));
    assert.deepStrictEqual(up[0].body, { session_id: 'default', enable: true, confirmed: false });
    assert.deepStrictEqual(up[1].body, { session_id: 'default', enable: true, confirmed: true });
    const ctl = node.querySelector('#awCtl').innerHTML;
    assert.ok(ctl.indexOf('data-aw-perm="danger-full-access"') >= 0, ctl);
    assert.ok(ctl.indexOf('id="awUpgradeBack"') >= 0, ctl);
    assert.strictEqual(ctl.indexOf('id="awUpgradeYes"'), -1, '确认完就该收起确认按钮：' + ctl);
  });
});

test('renderAgent: 点「算了」不发第二段，档位保持项目级（不静默放开）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: true,
        message: '放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？' }) });
    };
    g.renderAgent();
    await tick();

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgrade', dataset: {} } }));
    await tick();
    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgradeNo', dataset: {} } }));
    await tick();

    const up = calls.filter(c => c.url === '/api/agent/window/upgrade');
    assert.strictEqual(up.length, 1, '「算了」不许再发一段：' + JSON.stringify(calls));
    const ctl = node.querySelector('#awCtl').innerHTML;
    assert.ok(ctl.indexOf('data-aw-perm="workspace-write"') >= 0, ctl);
    assert.strictEqual(ctl.indexOf('id="awUpgradeYes"'), -1, '收起后不该还挂着确认按钮：' + ctl);
  });
});

test('renderAgent: 没走第一段就直接点确认，不许把升档发出去（防绕过确认静默放开）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      if (String(url).indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: false,
        permission_mode: 'danger-full-access' }) });
    };
    g.renderAgent();
    await tick();

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgradeYes', dataset: {} } }));
    await tick();

    assert.strictEqual(calls.filter(c => c.url === '/api/agent/window/upgrade').length, 0,
      '没有待确认的升档时，确认按钮不许触发任何请求：' + JSON.stringify(calls));
    assert.ok(node.querySelector('#awCtl').innerHTML.indexOf('data-aw-perm="workspace-write"') >= 0,
      node.querySelector('#awCtl').innerHTML);
  });
});

test('renderAgent: 后端没回 permission_mode 时不假装档位变了（照旧显示项目级）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      /* 后端只回 ok，没回档位 —— 前端不许自己假定已经放开 */
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: false }) });
    };
    g.renderAgent();
    await tick();

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgrade', dataset: {} } }));
    await tick();

    const ctl = node.querySelector('#awCtl').innerHTML;
    assert.ok(ctl.indexOf('data-aw-perm="workspace-write"') >= 0, '没回档位就得照旧显示项目级：' + ctl);
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('后端没回档位') >= 0,
      '要说清楚后端没回档位，不许当成已放开：' + node.querySelector('#awStream').innerHTML);
  });
});

test('renderAgent: 已放开时点「改回项目级」发 enable=false（后端顺手关掉放开进程）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'danger-full-access' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: false,
        permission_mode: 'workspace-write' }) });
    };
    g.renderAgent();
    await tick();
    assert.ok(node.querySelector('#awCtl').innerHTML.indexOf('id="awUpgradeBack"') >= 0, '前提：已放开态');

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgradeBack', dataset: {} } }));
    await tick();

    const up = calls.filter(c => c.url === '/api/agent/window/upgrade');
    assert.strictEqual(up.length, 1, JSON.stringify(calls));
    assert.deepStrictEqual(up[0].body, { session_id: 'default', enable: false, confirmed: false });
    const ctl = node.querySelector('#awCtl').innerHTML;
    assert.ok(ctl.indexOf('data-aw-perm="workspace-write"') >= 0, ctl);
    assert.ok(ctl.indexOf('id="awUpgrade"') >= 0, ctl);
  });
});

/* ★ 收尾修复（Important 2 的用户可见那一半）：后端**拒绝**收回升档（本会话还有活正跑在放开档上）时，
   HTTP 仍是 200，body 是 {ok:false, error:…}（backend/main.py:2620 直接返回 set_upgrade 的 dict）。
   界面必须①把原因原文说出来、②**不假装已经收回**（档位照实留在放开档，按钮还是「改回项目级」）——
   权限这件事说错了他会以为已经安全。 */
test('renderAgent: 收回被拒（ok:false）时说出原因，且不假装已收回档位', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    const REFUSE = '她还有一件活正跑在放开档上 —— 先按「停」再收回；这次没有改档，权限仍是放开的。';
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'danger-full-access' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: false, error: REFUSE }) });
    };
    g.renderAgent();
    await tick();
    assert.ok(node.querySelector('#awCtl').innerHTML.indexOf('id="awUpgradeBack"') >= 0, '前提：已放开态');

    (node._handlers.click || []).forEach(fn => fn({ target: { id: 'awUpgradeBack', dataset: {} } }));
    await tick();

    assert.strictEqual(calls.filter(c => c.url === '/api/agent/window/upgrade').length, 1, JSON.stringify(calls));
    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('权限档没改成：' + REFUSE) >= 0, '原因要原样说出来（静默 = 他以为已经收回）：' + html);
    const ctl = node.querySelector('#awCtl').innerHTML;
    assert.ok(ctl.indexOf('data-aw-perm="danger-full-access"') >= 0,
      '被拒之后档位照实留在放开档（不许显示成已收回）：' + ctl);
    assert.ok(ctl.indexOf('id="awUpgradeBack"') >= 0, '按钮还得是「改回项目级」（还能再试）：' + ctl);
  });
});

/* ===== R64 修复轮：错误响应不当成功 / 角色键 / 追补覆盖 ===== */

test('renderAgent: 后端 500 回的是 JSON 错误体 —— 插话不许当成打断成功（awPost 看 r.ok）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/interject') >= 0) {
        /* ★ backend/main.py:1095 的全局兜底：500 也是 **JSON** 响应，fetch 不会 reject */
        return Promise.resolve({ ok: false, status: 500,
          json: () => Promise.resolve({ error: { message: '服务器内部错误: RuntimeError' } }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-50',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个测试';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awText').value = '先别动那个文件';
    clickId(node, 'awInterrupt');
    await tick();

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('服务器内部错误: RuntimeError') >= 0, '500 的 JSON 错误要原样说出来：' + html);
    assert.strictEqual(html.indexOf('这就跟她说'), -1, '500 时一个字都没插进去，不许说打断了：' + html);
  });
});

test('renderAgent: 插话框空着点按钮要说一句（不静默 no-op）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-51',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个测试';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awText').value = '   ';        /* 只有空白 */
    clickId(node, 'awQueue');
    await tick();

    assert.strictEqual(calls.filter(c => c.url === '/api/agent/window/interject').length, 0,
      '空话不该发请求：' + JSON.stringify(calls));
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('空的') >= 0,
      '空输入点插话要说出来，不能什么都不发生：' + node.querySelector('#awStream').innerHTML);
  });
});

test('renderAgent: 连点「临时放开」只发一次第一段（在飞守卫）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: true,
        message: '放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？' }) });
    };
    g.renderAgent();
    await tick();

    clickId(node, 'awUpgrade');
    clickId(node, 'awUpgrade');                        /* 同步连点 */
    await tick();

    assert.strictEqual(calls.filter(c => c.url === '/api/agent/window/upgrade').length, 1,
      '连点只许发一次：' + JSON.stringify(calls));
  });
});

test('renderAgent: 连点「确认放开」只发一次 confirmed=true，确认行立刻收起', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj',
          permission_mode: 'workspace-write' }) });
      }
      const b = (opt && opt.body) ? JSON.parse(opt.body) : {};
      if (b.confirmed) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: false,
          permission_mode: 'danger-full-access' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, need_confirm: true,
        message: '放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？' }) });
    };
    g.renderAgent();
    await tick();
    clickId(node, 'awUpgrade');
    await tick();
    assert.ok(node.querySelector('#awCtl').innerHTML.indexOf('id="awUpgradeYes"') >= 0, '前提：出现确认行');

    clickId(node, 'awUpgradeYes');
    clickId(node, 'awUpgradeYes');                     /* 同步连点 */
    assert.strictEqual(node.querySelector('#awCtl').innerHTML.indexOf('id="awUpgradeYes"'), -1,
      '点过之后确认行要立刻收起（不许留一个还能再点的按钮）：' + node.querySelector('#awCtl').innerHTML);
    await tick();

    const up = calls.filter(c => c.url === '/api/agent/window/upgrade');
    assert.strictEqual(up.filter(c => c.body && c.body.confirmed === true).length, 1,
      '第二段只许发一次：' + JSON.stringify(calls));
  });
});

test('renderAgent: send 返回 ok:false 时把原因说出来（不静默）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: false, error: '手没接上（ACP 桥断了）' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我查天气';
    node.querySelector('#awSend').click();
    await tick();

    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('没派出去：手没接上（ACP 桥断了）') >= 0,
      'send 的 ok:false 要说清楚：' + node.querySelector('#awStream').innerHTML);
  });
});

test('renderAgent: /state 回 500 时状态条把后端错误原文说出来（不是空理由的「不可用」）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => Promise.resolve({ ok: false, status: 500,
      json: () => Promise.resolve({ error: { message: '服务器内部错误: RuntimeError' } }) });
    g.renderAgent();
    await tick();
    const bar = node.querySelector('.aw-bar');
    assert.ok(bar.textContent.indexOf('服务器内部错误: RuntimeError') >= 0,
      '/state 的 500 也要把原因说出来：' + bar.textContent);
    assert.ok(bar.textContent.indexOf('状态读不出来') >= 0, bar.textContent);
  });
});

test('renderAgent: 200 但响应体读不出来时也当失败说出来（不许返回 null 让调用方炸）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/interject') >= 0) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new Error('not json')) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-55',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个测试';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awText').value = '插一句';
    clickId(node, 'awQueue');
    await tick();

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('插话没递进去') >= 0, '读不出来的响应不能当成功：' + html);
    assert.strictEqual(html.indexOf('已记下'), -1, '更不许说已经记下了：' + html);
  });
});

test('renderAgent: 审批「同意」打到 approve 且只有 ok 才收卡', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    };
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: { id: 'ap9', summary: '要在工作区外写文件' } });
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('agent-approval') >= 0, '前提：审批卡在');

    clickData(node, { awApprove: 'ap9' });
    await tick();

    const ap = calls.filter(c => c.url === '/api/agent/window/approve');
    assert.strictEqual(ap.length, 1, JSON.stringify(calls));
    assert.deepStrictEqual(ap[0].body, { approval_id: 'ap9', allow: true });
    assert.strictEqual(node.querySelector('#awStream').innerHTML.indexOf('agent-approval'), -1,
      '同意成功后才收卡');
  });
});

test('renderAgent: 审批「拒绝」带 allow=false（别把同意/拒绝接反）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    };
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: { id: 'ap10', summary: '要在工作区外写文件' } });
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('agent-approval') >= 0, '前提：审批卡在');

    clickData(node, { awReject: 'ap10' });
    await tick();

    const ap = calls.filter(c => c.url === '/api/agent/window/approve');
    assert.strictEqual(ap.length, 1, JSON.stringify(calls));
    assert.deepStrictEqual(ap[0].body, { approval_id: 'ap10', allow: false });
    assert.strictEqual(node.querySelector('#awStream').innerHTML.indexOf('agent-approval'), -1,
      '拒绝成功后也收卡');
  });
});

test('renderAgent: 审批被后端否掉（180 秒超时已自动拒绝）时卡不许消失', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/approve') >= 0) {
        /* window.py:424-431：approve 失败只回 {"ok": false}，连 error 都没有 */
        return Promise.resolve({ json: () => Promise.resolve({ ok: false }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    };
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: { id: 'ap11', summary: '要在工作区外写文件' } });
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('agent-approval') >= 0, '前提：审批卡在');

    clickData(node, { awApprove: 'ap11' });
    await tick();

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('agent-approval') >= 0,
      '后端说没同意成，卡就必须留着（否则他会以为同意了，其实她早就当拒绝停手了）：' + html);
    assert.ok(html.indexOf('没送到') >= 0, '要说清楚没送到：' + html);
    /* ★ R66：原来这里是 `indexOf('180 秒')` —— 死断言：审批卡自带「超过 180 秒不回应，她会当作
       你拒绝并停手」，只要卡还在就恒真，任何变异都杀不死。换成只属于失败回执的那一串。 */
    assert.ok(html.indexOf('超过 180 秒不回应会被自动当拒绝') >= 0,
      '要点明 180 秒上限这条规则（用只出现在失败回执里的串，别用卡片自带的 180 秒）：' + html);
  });
});

test('renderAgent: 确认计划失败（ok:false）时计划卡不消失', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/confirm') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: false, error: '任务已结束' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-88',
        plan: { steps: ['读 backend/agent/loop.py'], risk: '只动工作区', eta: '30 秒' } }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我改一下 loop.py';
    node.querySelector('#awSend').click();
    await tick();
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('aw-plan') >= 0, '前提：计划卡在');

    clickData(node, { awConfirm: '1' });
    await tick();

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('aw-plan') >= 0, '确认没送到，计划卡不许收（否则他以为已经开工了）：' + html);
    assert.ok(html.indexOf('确认没送到：任务已结束') >= 0, '要说清楚为什么没送到：' + html);
  });
});

test('renderAgent: 停接口 500 时不许显示成已停（任务还在跑就还得能插话）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/stop') >= 0) {
        return Promise.resolve({ ok: false, status: 500,
          json: () => Promise.resolve({ error: { message: '服务器内部错误: TimeoutError' } }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-60',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个长任务';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awStop').click();
    await tick();

    const bar = node.querySelector('.aw-bar');
    assert.ok(bar.textContent.indexOf('running') >= 0, '没停掉就得还是 running：' + bar.textContent);
    assert.strictEqual(bar.textContent.indexOf('stopped'), -1, '不许显示成已停：' + bar.textContent);
    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('服务器内部错误: TimeoutError') >= 0, '要说清楚没停掉：' + html);
    assert.ok(html.indexOf('id="awQueue"') >= 0, '任务还在跑，插话按钮就得还在：' + html);
  });
});

test('renderAgent: 角色键用当前会话角色的名字（不发本地 uid，也不取 contacts[0]）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    /* 仓库惯例（chat.js / ws_client.js / companion.js）：角色键发**名字** */
    g.Chat = { contact: { id: 'uid-777', name: '助手' } };
    /* 就算 Store 里有别的联系人，也不许拿 contacts[0] 顶缸 */
    g.Store = { listContacts: () => [{ id: 'someone-else', name: '别人' }] };
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/confirm') >= 0) return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-70',
        plan: { steps: ['读文件'], risk: '只动工作区', eta: '10 秒' } }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我看下配置';
    node.querySelector('#awSend').click();
    await tick();

    const send = calls.filter(c => c.url === '/api/agent/window/send');
    assert.strictEqual(send.length, 1, JSON.stringify(calls));
    assert.strictEqual(send[0].body.character_id, '助手',
      '角色键要用当前角色的名字（本地 uid 会被后端回落成 default 桶）：' + JSON.stringify(send[0].body));
    assert.strictEqual(send[0].body.character_name, '助手', JSON.stringify(send[0].body));
    assert.strictEqual(send[0].body.character_id === 'uid-777', false, '不许发本地 uid');
    assert.strictEqual(send[0].body.character_id === 'someone-else', false, '不许取 contacts[0]');

    clickData(node, { awConfirm: '1' });
    await tick();
    const cf = calls.filter(c => c.url === '/api/agent/window/confirm');
    assert.strictEqual(cf.length, 1, JSON.stringify(calls));
    assert.strictEqual(cf[0].body.character_id, '助手', JSON.stringify(cf[0].body));
    assert.strictEqual(cf[0].body.character_name, '助手', JSON.stringify(cf[0].body));
  });
});

test('renderAgent: 没有 Chat.contact 时用 Chat.contactId 查当前角色（仍不取 contacts[0]）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.Chat = { contactId: 'c9' };
    g.Store = {
      getContact: (id) => (id === 'c9' ? { id: 'c9', name: '星尘' } : null),
      listContacts: () => [{ id: 'other', name: '别人' }]
    };
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-72',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个测试';
    node.querySelector('#awSend').click();
    await tick();

    const send = calls.filter(c => c.url === '/api/agent/window/send');
    assert.strictEqual(send.length, 1, JSON.stringify(calls));
    assert.strictEqual(send[0].body.character_id, '星尘', JSON.stringify(send[0].body));
    assert.strictEqual(send[0].body.character_name, '星尘', JSON.stringify(send[0].body));
  });
});

test('renderAgent: awPaint 手搓的 HTML 也要转义（消息正文与工具轨迹）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => new Promise(() => {});
    g.renderAgent();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_message', task_id: 't-80', text: '<img src=x onerror=alert(1)>' });
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-80', tool: 'read', tool_call_id: 'tc80',
                          args: { file_path: '<svg onload=alert(2)>' } });

    const html = node.querySelector('#awStream').innerHTML;
    assert.strictEqual(html.indexOf('<img'), -1, '消息正文不许当标签渲染：' + html);
    assert.strictEqual(html.indexOf('<svg'), -1, '工具参数不许当标签渲染：' + html);
    assert.ok(html.indexOf('&lt;img src=x onerror=alert(1)&gt;') >= 0, html);
    assert.ok(html.indexOf('&lt;svg onload=alert(2)&gt;') >= 0, html);
  });
});

/* ===== Task 13：聊天里的「她正在干活」联动卡 =====
   ★ R22：卡的驱动路径是 **WebSocket**（ws_client.js:108 → AgentWindowIngest），
   不是 chat.js 的 handleAgentEvent（那条只接 SSE，永远看不到 agent_* 事件）。
   所以下面这一组全部从 AgentWindowIngest 灌事件，不许从 chat.js 侧绕。 */

test('liveCardHTML: 一行联动卡 + 可点击属性', () => {
  const html = AW.liveCardHTML('t-1', 3);
  assert.ok(html.includes('她正在干活'), html);
  assert.ok(html.includes('第 3 步'), html);
  assert.ok(html.includes('data-aw-open="1"'), html);
  assert.ok(html.indexOf('\n') === -1, '联动卡必须是单行');
  /* 无条件补强：卡要知道自己属于哪次活（换任务时要据此重新数步数） */
  assert.ok(html.includes('data-aw-task="t-1"'), html);
  assert.ok(html.includes('class="aw-live"'), html);
});

test('liveCardHTML: task_id 里的引号/尖括号被转义（属性上下文不许被截断）', () => {
  const html = AW.liveCardHTML('t-"><img src=x>', 1);
  assert.strictEqual(html.indexOf('<img'), -1, html);
  assert.ok(html.includes('&quot;'), html);
  /* step 缺省/为 0 时按第 1 步（esc(step || 1)） */
  assert.ok(AW.liveCardHTML('t-1', 0).includes('第 1 步'), AW.liveCardHTML('t-1', 0));
  assert.ok(AW.liveCardHTML('t-1').includes('第 1 步'), AW.liveCardHTML('t-1'));
});

/* ---------- 假聊天正文：只实现联动卡用到的那几件事 ----------
   appendChild / removeChild / querySelector('.aw-live') / 滚动位置，
   以及 createElement('div').innerHTML = … → firstChild（模拟真 DOM 的属性 + dataset）。 */

function makeChatBody(id) {
  const body = {
    id: id || 'chat-body', children: [], scrollTop: 0, scrollHeight: 640, className: '',
    appendChild(c) { c.parentNode = body; body.children.push(c); return c; },
    removeChild(c) {
      const i = body.children.indexOf(c);
      if (i >= 0) body.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    querySelector(sel) {
      if (sel === '.aw-live') {
        return body.children.filter(c => String(c.className || '').split(/\s+/).indexOf('aw-live') >= 0)[0] || null;
      }
      return null;
    },
    querySelectorAll() { return []; }
  };
  return body;
}

function makeCardNode(attrs, inner) {
  const node = {
    className: attrs['class'] || '', parentNode: null, innerHTML: inner || '', _handlers: {}, dataset: {},
    addEventListener(t, fn) { (node._handlers[t] = node._handlers[t] || []).push(fn); },
    click() { (node._handlers.click || []).forEach(fn => fn({ target: node })); }
  };
  Object.keys(attrs).forEach(k => {
    if (k.indexOf('data-') !== 0) return;
    /* data-aw-open → dataset.awOpen（与真 DOM 同义） */
    node.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = attrs[k];
  });
  return node;
}

function makeChatDoc(chatBody, agentNode) {
  return {
    /* agentNode 给了就照常服务 renderAgent 的零参自定位（需要同时用 renderAgent + 联动卡时传第三个参数）；
       不给就是「页面不在 agent」：ingest 不负责重画 —— 联动卡必须与「干活」页是否打开无关 */
    getElementById: (id) => (id === (agentNode && agentNode.id) ? agentNode
                             : (id === chatBody.id ? chatBody : null)),
    body: { dataset: { page: 'chat' } },
    querySelector(sel) {
      if (sel === '#' + chatBody.id) return chatBody;
      if (sel === '.aw-live') return chatBody.querySelector('.aw-live');
      return null;
    },
    createElement() {
      const wrap = { firstChild: null, _html: '' };
      Object.defineProperty(wrap, 'innerHTML', {
        get: () => wrap._html,
        set(v) {
          wrap._html = String(v);
          const m = /^<div([^>]*)>([\s\S]*)<\/div>$/.exec(wrap._html);
          if (!m) { wrap.firstChild = null; return; }
          const attrs = {};
          m[1].replace(/([a-zA-Z-]+)="([^"]*)"/g, (_, k, val) => { attrs[k] = val; return ''; });
          wrap.firstChild = makeCardNode(attrs, m[2]);
        }
      });
      return wrap;
    }
  };
}

test('AgentWindowMirrorToChat: WS 的 agent_tool 在聊天里插一行卡，步数递增，点它跳「干活」', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    const shown = [];
    g.hzShowView = (name) => shown.push(name);

    /* 前提：非 agent_* 事件既不认领也不挂卡 */
    assert.strictEqual(g.AgentWindowIngest({ type: 'proactive', role: 'assistant', content: '主动消息' }), false);
    assert.strictEqual(chatBody.children.length, 0, '非 agent 事件不许往聊天里挂卡');

    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-1', tool: 'read',
                                             tool_call_id: 'tc1', args: { file_path: 'D:/a.txt' } }), true);
    assert.strictEqual(chatBody.children.length, 1,
      '★ WS 的 agent_tool 必须在聊天正文里挂出联动卡（「干活」页没打开也要挂）');
    const card = chatBody.children[0];
    assert.strictEqual(card.dataset.awOpen, '1', '卡片要带 data-aw-open="1"：' + card.innerHTML);
    assert.strictEqual(card.dataset.awTask, 't-1', '卡片要记住是哪个任务：' + card.innerHTML);
    assert.ok(card.innerHTML.indexOf('她正在干活') >= 0, card.innerHTML);
    assert.ok(card.innerHTML.indexOf('第 1 步') >= 0, card.innerHTML);
    assert.ok(card.innerHTML.indexOf('\n') === -1, '联动卡必须是单行：' + card.innerHTML);
    assert.strictEqual(chatBody.scrollTop, chatBody.scrollHeight, '挂了卡要滚到底，否则他看不见');

    /* 同一次活里的第二个工具调用 → 还是那一张卡，步数 +1 */
    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_tool', tool: 'write', tool_call_id: 'tc2', args: {} }), true);
    assert.strictEqual(chatBody.children.length, 1, '同一次活只许有一张卡');
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 2 步') >= 0, chatBody.children[0].innerHTML);

    chatBody.children[0].click();
    assert.deepStrictEqual(shown, ['agent'], '点卡要切到「干活」页');
    assert.strictEqual(chatBody.children.length, 1, '点一下不许把卡点没了');
  });
});

test('AgentWindowMirrorToChat: 没有 task_id 的 agent_tool 不挂卡（避免误挂到聊天上）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    g.hzShowView = () => {};

    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_tool', tool: 'read', tool_call_id: 'tc0', args: {} }), true);
    assert.strictEqual(chatBody.children.length, 0, '没有 task_id 就不该挂卡：' + JSON.stringify(chatBody.children));

    /* 但已经有卡时，后续没带 task_id 的同一次活继续数步数 */
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-2', tool: 'read', tool_call_id: 'tc1', args: {} });
    g.AgentWindowIngest({ type: 'agent_tool', tool: 'read', tool_call_id: 'tc2', args: {} });
    assert.strictEqual(chatBody.children.length, 1, '有卡时不许再挂第二张');
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 2 步') >= 0, chatBody.children[0].innerHTML);
  });
});

test('AgentWindowMirrorToChat: agent_final / agent_stopped / agent_error 都把卡收掉', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    g.hzShowView = () => {};
    const endings = ['agent_final', 'agent_stopped', 'agent_error'];
    endings.forEach((t, i) => {
      g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-' + i, tool: 'read', tool_call_id: 'tc' + i, args: {} });
      assert.strictEqual(chatBody.children.length, 1, '前提：' + t + ' 前卡在');
      /* ★ R66：用 window.py 结算时真正推的形状（带 task_id/session_id/character_id），不是裸事件 */
      g.AgentWindowIngest({ type: t, task_id: 't-' + i, session_id: 's-1', character_id: '助手',
                            status: 'done', answer: '干完了', text: '出错了' });
      assert.strictEqual(chatBody.children.length, 0, t + ' 之后聊天里的卡必须消失');
    });
    /* 收卡后下一件活重新从第 1 步数（步数不许跨任务累积） */
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-9', tool: 'read', tool_call_id: 'tc9', args: {} });
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 1 步') >= 0, chatBody.children[0].innerHTML);
  });
});

test('AgentWindowMirrorToChat: 上一件活的卡还在时换了 task_id → 从第 1 步重新数', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    g.hzShowView = () => {};
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-1', tool: 'read', tool_call_id: 'a', args: {} });
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-1', tool: 'read', tool_call_id: 'b', args: {} });
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 2 步') >= 0, chatBody.children[0].innerHTML);

    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-2', tool: 'read', tool_call_id: 'c', args: {} });
    const card = chatBody.children[0];
    assert.strictEqual(chatBody.children.length, 1, '还是同一张卡');
    assert.strictEqual(card.dataset.awTask, 't-2', '卡片要改认新任务：' + card.innerHTML);
    assert.ok(card.innerHTML.indexOf('第 1 步') >= 0,
      '换了任务就得从第 1 步数，不许把上一次活的步数带过来：' + card.innerHTML);
  });
});

test('AgentWindowMirrorToChat: 点卡时 hzShowView 没就绪不抛；畸形事件不许打断主流程', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    /* 故意不挂 hzShowView：flow-app.js 万一没起来，镜像也不许抛 */
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-1', tool: 'read', tool_call_id: 'tc1', args: {} });
    assert.strictEqual(chatBody.children.length, 1, '前提：卡在');
    assert.doesNotThrow(() => chatBody.children[0].click());

    /* 数字 task_id 也照收（esc 成字符串） */
    g.AgentWindowIngest({ type: 'agent_final', answer: 'x' });
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 42, tool: 'read', tool_call_id: 'tc2', args: {} });
    assert.strictEqual(chatBody.children[0].dataset.awTask, '42', chatBody.children[0].innerHTML);

    /* 收尾事件在没有卡时也照收，不抛 */
    assert.doesNotThrow(() => g.AgentWindowIngest({ type: 'agent_stopped' }));
    assert.doesNotThrow(() => g.AgentWindowIngest({ type: 'agent_error' }));
    assert.strictEqual(chatBody.children.length, 0);
  });
});

test('AgentWindowMirrorToChat: 没有 #chat-body 时退回 #cpBody（伴侣页的正文）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const cpBody = makeChatBody('cpBody');
    g.document = makeChatDoc(cpBody);
    g.hzShowView = () => {};
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-1', tool: 'read', tool_call_id: 'tc1', args: {} });
    assert.strictEqual(cpBody.children.length, 1, '聊天正文不在时要用 #cpBody 兜底');
    assert.ok(cpBody.children[0].innerHTML.indexOf('她正在干活') >= 0, cpBody.children[0].innerHTML);
  });
});

/* ===== R66：生产终止事件 / 停按钮 / 步数分桶 / 角色键实时 / 审批卡的失败出口 ===== */

test('R66: 后端结算推的终止事件（形状同 window.py 的 dict(ev, task_id=…)）能把卡收掉', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    g.hzShowView = () => {};
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-9', tool: 'read', tool_call_id: 'tc1', args: {} });
    assert.strictEqual(chatBody.children.length, 1, '前提：卡在');
    /* ★ R66：这就是 window.py 结算时真正推上 WS 的那一条（带 task_id / session_id / character_id）——
       卡的「收」必须由它驱动，不能只靠本地合成的假事件。 */
    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_final', task_id: 't-9',
                                             session_id: 's-1', character_id: '助手', status: 'done' }), true);
    assert.strictEqual(chatBody.children.length, 0, '生产终止事件必须把卡收掉');
  });
});

test('R66: 点「停」把聊天里的联动卡一起收掉（本地路径，不等后端结算）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody, node);        /* 同时要 renderAgent（零参自定位）+ 联动卡 */
    g.hzShowView = () => {};
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/stop') >= 0) return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-30',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个长任务';
    node.querySelector('#awSend').click();
    await tick();
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-30', tool: 'read', tool_call_id: 'tc1', args: {} });
    assert.strictEqual(chatBody.children.length, 1, '前提：卡在');

    node.querySelector('#awStop').click();
    await tick();
    assert.strictEqual(chatBody.children.length, 0,
      '「停」是最可能留下永久陈旧卡的路径：停成功就必须立刻收卡');
  });
});

test('R66: 停接口说没停掉时卡不许收（任务还在跑，还会继续出工具事件）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody, node);
    g.hzShowView = () => {};
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/stop') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: false, error: '任务已结束' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'started', task_id: 't-31',
        plan: null, reply: '' }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我跑个长任务';
    node.querySelector('#awSend').click();
    await tick();
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-31', tool: 'read', tool_call_id: 'tc1', args: {} });

    node.querySelector('#awStop').click();
    await tick();
    assert.strictEqual(chatBody.children.length, 1, '没停掉就还在干活，卡不许收：' + chatBody.children.length);
  });
});

test('R66: 两件活的步数各算各的（交错时不许把 B 的步数算到 A 头上）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const chatBody = makeChatBody('chat-body');
    g.document = makeChatDoc(chatBody);
    g.hzShowView = () => {};
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-A', tool: 'read', tool_call_id: 'a1', args: {} });
    assert.strictEqual(chatBody.children[0].dataset.awTask, 't-A', chatBody.children[0].innerHTML);
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 1 步') >= 0, chatBody.children[0].innerHTML);

    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-B', tool: 'read', tool_call_id: 'b1', args: {} });
    assert.strictEqual(chatBody.children[0].dataset.awTask, 't-B', chatBody.children[0].innerHTML);
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 1 步') >= 0, chatBody.children[0].innerHTML);

    /* ★ R66：回到 A 的第二件工具调用 —— 计数必须按 task_id 分桶，不能因为切走又切回就归零 */
    g.AgentWindowIngest({ type: 'agent_tool', task_id: 't-A', tool: 'write', tool_call_id: 'a2', args: {} });
    assert.strictEqual(chatBody.children[0].dataset.awTask, 't-A', chatBody.children[0].innerHTML);
    assert.ok(chatBody.children[0].innerHTML.indexOf('第 2 步') >= 0,
      'A 的第 2 件工具必须显示第 2 步（按 task_id 分桶），不是第 1 步也不是第 3 步：'
      + chatBody.children[0].innerHTML);
  });
});

test('R66: 角色键在 send/confirm 时实时读（Chat.close() 清空 contact / 换伴侣都要跟上）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.Chat = { contact: { id: 'uid-1', name: '助手' } };
    g.Store = { getContact: () => null, listContacts: () => [] };
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/confirm') >= 0) return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-70',
        plan: { steps: ['读文件'], risk: '只动工作区', eta: '10 秒' } }) });
    };
    g.renderAgent();
    await tick();

    /* 挂载之后 Chat.close() 那种状态：contact 没了，只剩 contactId */
    g.Chat.contact = null;
    g.Chat.contactId = 'c9';
    g.Store = { getContact: (id) => (id === 'c9' ? { id: 'c9', name: '星尘' } : null),
                listContacts: () => [{ id: 'other', name: '别人' }] };
    node.querySelector('#awText').value = '帮我看下配置';
    node.querySelector('#awSend').click();
    await tick();

    const send = calls.filter(c => c.url === '/api/agent/window/send');
    assert.strictEqual(send.length, 1, JSON.stringify(calls));
    assert.strictEqual(send[0].body.character_id, '星尘',
      '★ 挂载那一刻的快照已经过期（Chat.close() 清了 contact）—— 发请求时必须实时读：'
      + JSON.stringify(send[0].body));

    /* 换了伴侣：confirm 也必须发新名字，不许发挂载时的旧名字 */
    g.Chat.contact = { id: 'uid-2', name: '新伴侣' };
    clickData(node, { awConfirm: '1' });
    await tick();
    const cf = calls.filter(c => c.url === '/api/agent/window/confirm');
    assert.strictEqual(cf.length, 1, JSON.stringify(calls));
    assert.strictEqual(cf[0].body.character_id, '新伴侣', JSON.stringify(cf[0].body));
    assert.strictEqual(cf[0].body.character_id === '助手', false, '不许再发挂载那一刻的旧名字');
  });
});

test('R66: 连点「同意」只发一次 approve（第二次不许打出误导性的「没送到」）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    };
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: { id: 'ap20', summary: '要在工作区外写文件' } });
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('agent-approval') >= 0, '前提：审批卡在');

    clickData(node, { awApprove: 'ap20' });
    clickData(node, { awApprove: 'ap20' });            /* 同步连点 */
    await tick();

    assert.strictEqual(calls.filter(c => c.url === '/api/agent/window/approve').length, 1,
      '连点只许发一次（第二次后端只会回 ok:false）：' + JSON.stringify(calls));
    assert.strictEqual(node.querySelector('#awStream').innerHTML.indexOf('没送到'), -1,
      '不许因为自己连点而冒出「没送到」的误报：' + node.querySelector('#awStream').innerHTML);
  });
});

test('R66: 审批没送到时给「收起这张卡」，点了才收（原因照实说）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: false }) });
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: { id: 'ap21', summary: '要在工作区外写文件' } });

    clickData(node, { awApprove: 'ap21' });
    await tick();

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('agent-approval') >= 0, '后端说没同意成，卡必须留着：' + html);
    assert.ok(html.indexOf('后端说这条审批已经不在等待了') >= 0, '要说清为什么没送到：' + html);
    assert.ok(html.indexOf('data-aw-dismiss="approval"') >= 0,
      '失败态必须有明确出口（不然只能一直盯着点不动的卡）：' + html);

    clickData(node, { awDismiss: 'approval' });
    await tick();
    assert.strictEqual(node.querySelector('#awStream').innerHTML.indexOf('agent-approval'), -1,
      '点了「收起这张卡」才收');
  });
});

/* ---------- ★ 收尾修复：审批卡倒计时（180 秒上限）与超时出口 ----------
   复审实测的事实：backend/agent/approval.py:129-132 把 180 秒超时判成 rejected，但**一个 WS 事件都不推**；
   window.py 的终止事件只覆盖**任务**结算。于是本文件里 awState.approval 永远留着 ——
   用户点「同意」，后端回 {ok:false}（这条审批早已不在等待），界面印一行红字 +「收起这张卡」，
   **卡本身还在**：有出口，但卡上的状态是假的（看起来还在等他点）。
   spec §6.2 要求卡上有倒计时（180 秒上限）。审批对象自带 created_at（approval.py:63，
   get_approval 只摘掉 event），所以**每次重画**都按 created_at + 180 现算，归零就切「已超时」
   并把两个按钮禁掉；迟到的点击仍走原来的失败回执（说清没送到），绝不静默 no-op。 */

const AP_WAIT_S = 180;          /* 与 backend/agent/approval.py 的 _WAIT_TIMEOUT 对齐（脚本里有同值断言） */

test('approvalRemain: 剩余秒数按 created_at + 180 现算（后端没给时间就不编一个数）', () => {
  assert.strictEqual(AW.approvalTimeoutSec, AP_WAIT_S, '前端上限必须就是 180 秒（与后端 _WAIT_TIMEOUT 同值）');
  const t = 1700000000;
  assert.strictEqual(AW.approvalRemain({ created_at: t }, t * 1000), AP_WAIT_S, '刚创建 → 满 180 秒');
  assert.strictEqual(AW.approvalRemain({ created_at: t }, (t + 179) * 1000), 1, '179 秒前 → 还剩 1 秒');
  assert.strictEqual(AW.approvalRemain({ created_at: t }, (t + 180) * 1000), 0, '正好 180 秒 → 归零');
  assert.strictEqual(AW.approvalRemain({ created_at: t }, (t + 181) * 1000), 0, '超过 180 秒 → 0（不出现负数）');
  assert.strictEqual(AW.approvalRemain({ id: 'x' }, t * 1000), null, '没有 created_at → null（宁可不显示也不编）');
});

test('renderAgent: 审批卡显示倒计时（created_at 179 秒前 → 还剩 1 秒，两个按钮都能点）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: {
      id: 'ap40', summary: '要在工作区外写文件', created_at: Date.now() / 1000 - 179 } });

    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('还剩 1 秒') >= 0, '要按 created_at 现算剩余秒数（spec §6.2 的 180 秒上限）：' + html);
    assert.ok(html.indexOf('data-aw-approve="ap40"') >= 0, '还剩 1 秒时「同意」必须能点：' + html);
    assert.ok(html.indexOf('data-aw-reject="ap40"') >= 0, '还剩 1 秒时「拒绝」必须能点：' + html);
    assert.strictEqual(/class="agent-btn ok"[^>]*\bdisabled\b/.test(html), false,
      '没超时就不许禁用「同意」：' + html);
  });
});

test('renderAgent: 审批卡超时（created_at 181 秒前 → 已超时态 + 两个按钮禁用 + 能收起）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    g.AgentWindowIngest({ type: 'agent_approval', approval: {
      id: 'ap41', summary: '要在工作区外写文件', created_at: Date.now() / 1000 - 181 } });

    let html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('已超时 —— 她按拒绝停手了') >= 0,
      '超时要照实说（后端自动判拒且不推事件，卡不能还装成在等）：' + html);
    assert.ok(/class="agent-btn ok"[^>]*\bdisabled\b/.test(html), '超时后「同意」必须禁用：' + html);
    assert.ok(/class="agent-btn no"[^>]*\bdisabled\b/.test(html), '超时后「拒绝」必须禁用：' + html);
    assert.ok(html.indexOf('data-aw-dismiss="approval"') >= 0, '超时卡必须有出口（否则又是一张死卡）：' + html);

    clickData(node, { awDismiss: 'approval' });
    await tick();
    html = node.querySelector('#awStream').innerHTML;
    assert.strictEqual(html.indexOf('agent-approval'), -1, '点「收起这张卡」要真的收掉：' + html);
  });
});

test('renderAgent: 倒计时每次重画现算 —— 一个事件都没有、时间到了也必须切超时态', async () => {
  const node = makeEl('page-agent');
  const realNow = Date.now;
  let nowMs = 1700000000000;
  Date.now = function () { return nowMs; };            /* 冻结时钟：用例完全确定，不用等真时间 */
  try {
    await withFakeDom(node, async (g) => {
      g.fetch = () => Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
      g.renderAgent();
      await tick();
      g.document.body.dataset.page = 'agent';
      g.AgentWindowIngest({ type: 'agent_approval', approval: {
        id: 'ap43', summary: '要在工作区外写文件', created_at: nowMs / 1000 - 179 } });
      let html = node.querySelector('#awStream').innerHTML;
      assert.ok(html.indexOf('还剩 1 秒') >= 0, '前提：时钟冻结在「还剩 1 秒」：' + html);
      assert.strictEqual(/class="agent-btn ok"[^>]*\bdisabled\b/.test(html), false, '前提：这时还能点：' + html);

      nowMs += 190 * 1000;                       /* 时间走了 190 秒，但**一个 WS 事件都没有** */
      g.AgentWindowIngest({ type: 'agent_message', text: '（她还在等你回话）' });   /* 只重画一次 */

      html = node.querySelector('#awStream').innerHTML;
      assert.ok(html.indexOf('已超时 —— 她按拒绝停手了') >= 0,
        '剩余秒数必须每次重画现算（缓存进 state 的话会永远停在旧秒数，超时态永远不出现）：' + html);
      assert.ok(/class="agent-btn ok"[^>]*\bdisabled\b/.test(html), '超时后「同意」禁用：' + html);
    });
  } finally { Date.now = realNow; }
});

test('renderAgent: 迟到的那一下点击仍要说清没送到（不许静默 no-op）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      if (String(url).indexOf('/approve') >= 0) return Promise.resolve({ json: () => Promise.resolve({ ok: false }) });
      return Promise.resolve({ json: () => Promise.resolve({ ok: true }) });
    };
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    /* 卡上还剩 1 秒（还看得见、还能点），可他点下去的时候后端那边已经超时判拒了 —— 这一下必须说话 */
    g.AgentWindowIngest({ type: 'agent_approval', approval: {
      id: 'ap42', summary: '要在工作区外写文件', created_at: Date.now() / 1000 - 179 } });

    clickData(node, { awApprove: 'ap42' });
    await tick();

    const ap = calls.filter(c => c.url === '/api/agent/window/approve');
    assert.strictEqual(ap.length, 1, '迟到的点击也要真发出去问一次（静默 no-op 最坑）：' + JSON.stringify(calls));
    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('没送到') >= 0, '要说清这一下没送到：' + html);
    assert.ok(html.indexOf('超过 180 秒不回应会被自动当拒绝') >= 0, '要点明 180 秒上限这条规则：' + html);
    assert.ok(html.indexOf('data-aw-dismiss="approval"') >= 0, '失败后必须留出口：' + html);

    clickData(node, { awDismiss: 'approval' });
    await tick();
    assert.strictEqual(node.querySelector('#awStream').innerHTML.indexOf('agent-approval'), -1,
      '点了「收起这张卡」要真的收掉');
  });
});

test('R66: 计划卡确认失败也能收起（不许只能重试、不能取消）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/confirm') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ ok: false, error: '任务已结束' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-80',
        plan: { steps: ['读 backend/agent/loop.py'], risk: '只动工作区', eta: '30 秒' } }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我改一下 loop.py';
    node.querySelector('#awSend').click();
    await tick();
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('aw-plan') >= 0, '前提：计划卡在');

    clickData(node, { awConfirm: '1' });
    await tick();

    let html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('aw-plan') >= 0, '确认没送到，计划卡不许自己收：' + html);
    assert.ok(html.indexOf('data-aw-dismiss="plan"') >= 0, '失败态要有收起计划卡的出口：' + html);

    clickData(node, { awDismiss: 'plan' });
    await tick();
    html = node.querySelector('#awStream').innerHTML;
    assert.strictEqual(html.indexOf('aw-plan'), -1, '点了收起才收掉计划卡');
  });
});

/* ---------- ★ 折进来的第 3 条：取消「还没开工的计划」也要走终态 ----------
   后端老实现（window.py stop() 的 awaiting_confirm 分支）只 `status="cancelled"` 就 return：
   不发终止事件（前端那张计划卡永远收不掉）、不盖 ended_at、_SESSION_LAST 还指着这条死任务，
   而且不清 t["queue"] —— 计划卡挂着时主人补的那句话成了搁死文本（cancelled 不在 _IN_FLIGHT，
   send 再也不会把它排出去）。后端修好之后，前端这两半也必须跟上，否则「卡收得掉」只是空话。 */

test('renderAgent: 计划被取消（agent_stopped）后窗口里不再挂着那张点不动的计划卡', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    g.fetch = (url) => {
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-90',
        plan: { steps: ['读 backend/agent/loop.py', '把 _MAX_STEPS 改成 20'], risk: '只动工作区', eta: '30 秒' } }) });
    };
    g.renderAgent();
    await tick();
    g.document.body.dataset.page = 'agent';
    node.querySelector('#awText').value = '帮我把 _MAX_STEPS 改成 20';
    node.querySelector('#awSend').click();
    await tick();
    assert.ok(node.querySelector('#awStream').innerHTML.indexOf('aw-plan') >= 0, '前提：计划卡在');

    /* 后端取消未开工的计划后会推终态事件（cancelled → agent_stopped，带 task_id） */
    assert.strictEqual(g.AgentWindowIngest({ type: 'agent_stopped', task_id: 't-90' }), true);
    const html = node.querySelector('#awStream').innerHTML;
    assert.strictEqual(html.indexOf('aw-plan'), -1,
      '计划被取消后那张卡必须收掉（后端 confirm 只会回「任务不在待确认状态」，留着就是一张点不动的卡）：' + html);
    const bar = node.querySelector('.aw-bar');
    assert.ok(bar.textContent.indexOf('stopped') >= 0, '状态照实是 stopped：' + bar.textContent);
  });
});

test('renderAgent: 停掉「还没开工的计划」时把被丢掉的排队原话回报出来（不静默吞用户的话）', async () => {
  const node = makeEl('page-agent');
  await withFakeDom(node, async (g) => {
    const calls = [];
    g.fetch = (url, opt) => {
      calls.push({ url: String(url), body: (opt && opt.body) ? JSON.parse(opt.body) : null });
      const u = String(url);
      if (u.indexOf('/state') >= 0) {
        return Promise.resolve({ json: () => Promise.resolve({ capability: { ok: true }, cwd: 'D:/proj' }) });
      }
      if (u.indexOf('/stop') >= 0) {
        /* 后端新回执：停掉未开工的计划时，把还没跑就被丢掉的原话一并带回来 */
        return Promise.resolve({ json: () => Promise.resolve({ ok: true, dropped: 1,
          dropped_texts: ['顺便把 README 也看一下'] }) });
      }
      return Promise.resolve({ json: () => Promise.resolve({ ok: true, kind: 'plan', task_id: 't-91',
        plan: { steps: ['读 backend/agent/loop.py'], risk: '只动工作区', eta: '30 秒' } }) });
    };
    g.renderAgent();
    await tick();
    node.querySelector('#awText').value = '帮我把 _MAX_STEPS 改成 20';
    node.querySelector('#awSend').click();
    await tick();

    node.querySelector('#awStop').click();
    await tick();

    const st = calls.filter(c => c.url === '/api/agent/window/stop');
    assert.strictEqual(st.length, 1, JSON.stringify(calls));
    assert.strictEqual(st[0].body.task_id, 't-91', JSON.stringify(st[0].body));
    const html = node.querySelector('#awStream').innerHTML;
    assert.ok(html.indexOf('顺便把 README 也看一下') >= 0,
      '被丢掉的那句话要原样说出来（不说他就会以为还排着）：' + html);
    assert.ok(html.indexOf('1 句') >= 0, '要说清丢了几句：' + html);
  });
});

