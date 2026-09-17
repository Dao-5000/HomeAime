/* 「干活」窗口：与 harness（DSH）对话的专用界面。
   设计约束：纯函数集中挂在 window.AgentWindow 上，便于 node --test 直接 require（见 test/agent-window.test.js）。
   边界（R19/R22）：ACP 事件走 WebSocket ingest（后续任务），本文件只做「事件 -> 状态 -> 卡片」的纯逻辑，不 fetch。

   ★ 渲染契约（Task 11 按这个写）：`window.renderAgent()` 与既有 8 个 render* 一致，**零参数调用、自己定位节点**
     （flow-adapter.js 是 `window[cfg.render]()` 零参调用）。传了 sectionEl 就用传进来的，没传就
     `document.getElementById('page-agent')`；两处都没有就直接 return（Node 里 require 本文件时不会碰 DOM）。 */
(function (root) {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[<>&"']/g,
      c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  /* 上面 esc() 现在也转义 ' —— 但请注意：**Task 11 拼属性时不得使用单引号或无引号属性**
     （必须写成 data-aw-x="值"），否则单引号闭合属性的老坑会重新出现。
     esc() 不区分属性上下文，只负责内容里的危险字符。 */

  /* state 里存的是 WS 事件的浅拷贝：绝不把事件对象本身塞进 state，
     避免 Task 11 原地改 state 时污染还被别处引用的事件对象。 */
  function copy(o) {
    return (o && typeof o === 'object') ? Object.assign({}, o) : null;
  }

  function summarizeArgs(tool, args) {
    const a = args || {};
    if (a.file_path || a.path) return String(a.file_path || a.path);
    if (a.command) return String(a.command).slice(0, 80);
    if (a.pattern) return 'pattern=' + String(a.pattern);
    if (a.query) return String(a.query).slice(0, 60);
    return tool ? ('调用 ' + tool) : '调用工具';
  }

  /* 聊天联动卡的正文（Task 13）：「建卡」与「后续改步数」共用同一句 ——
     两处各写一遍迟早漂移（步数文案改一处、另一处还是旧话）。 */
  function liveText(step) {
    return '她正在干活 · 第 ' + esc(step || 1) + ' 步 · <b>点我看进展</b>';
  }

  /* ACP configOptions 的形状：[{id, name, currentValue, options:[{value,name}]}]（session/new 回的）。
     取 model 档的当前值；取不到就返回空串 —— 宁可说「不知道」也不猜模型名。 */
  function modelCurrent(configOptions) {
    if (!Array.isArray(configOptions)) return '';
    for (let i = 0; i < configOptions.length; i++) {
      const o = configOptions[i];
      if (o && typeof o === 'object' && String(o.id || '') === 'model' && o.currentValue) return String(o.currentValue);
    }
    return '';
  }

  /* 审批（越界操作）的等待上限：与 backend/agent/approval.py 的 _WAIT_TIMEOUT 同值 ——
     两条真相迟早漂移（卡上数到 0 的时刻与后端判拒的时刻对不上就会出现「还剩 0 秒却还能点」）。
     tools/_verify_agent_window_api.py 有一条同值断言钉着这两个数字。 */
  const APPROVAL_WAIT_SEC = 180;

  const AW = {
    version: '1.0',
    initialState: function () {
      return { messages: [], trail: [], plan: null, approval: null, usage: null, status: 'idle', taskId: '',
               cardStuck: null };   /* ★ R66：卡住了（审批/计划没送到）时的失败提示 + 收起按钮 */
    },
    reduce: function (state, ev) {
      const s = Object.assign({}, state || AW.initialState());
      s.messages = (s.messages || []).slice();
      s.trail = (s.trail || []).slice();
      if (!ev || !ev.type) return s;
      if (ev.task_id) s.taskId = ev.task_id;
      switch (ev.type) {
        case 'agent_thinking':
          s.status = 'running';
          break;
        case 'agent_message':
          if (String(ev.text || '').trim()) s.messages.push({ text: String(ev.text) });
          break;
        case 'agent_tool':
          s.status = 'running';
          s.trail.push({
            tool_call_id: ev.tool_call_id || '', tool: ev.tool || '',
            summary: summarizeArgs(ev.tool, ev.args), args: copy(ev.args) || {},
            ok: null, result: '', status: ev.status || 'in_progress'
          });
          break;
        case 'agent_result': {
          const i = s.trail.findIndex(x => x.tool_call_id && x.tool_call_id === ev.tool_call_id);
          const row = { ok: ev.ok !== false, result: String(ev.result || ''), status: ev.status || '' };
          if (i >= 0) s.trail[i] = Object.assign({}, s.trail[i], row, { tool: s.trail[i].tool || ev.tool || '' });
          else s.trail.push({ tool_call_id: ev.tool_call_id || '', tool: ev.tool || '',
                              summary: summarizeArgs(ev.tool, {}), args: {}, ok: row.ok, result: row.result, status: row.status });
          break;
        }
        case 'agent_plan':
          s.plan = copy(ev.plan);
          break;
        case 'agent_approval':
          s.approval = copy(ev.approval);
          break;
        case 'agent_usage':
          s.usage = { used: ev.used, size: ev.size, cost: ev.cost };
          break;
        case 'agent_final':
          if (String(ev.answer || '').trim()) s.messages.push({ text: String(ev.answer) });
          s.status = 'done';
          s.plan = null;      /* ★ 第 3 条：活结束了，计划卡跟着收（见下面 agent_stopped 的注释） */
          break;
        case 'agent_stopped':
          s.status = 'stopped';
          /* ★ 折进来的第 3 条：终止事件也要收掉**计划卡** —— 后端现在连「取消还没开工的计划」
             也会推带 task_id 的 agent_stopped（window.py stop() 的 awaiting_confirm 分支走 _finish）。
             不收的话那张卡永远点不动：后端 confirm 只会回「任务不在待确认状态」。
             一次活结束了那张卡就没有意义了；窗口同一时刻只有一次活（R35 排队），不必按 task_id 分辨。 */
          s.plan = null;
          break;
        case 'agent_error':
          s.status = 'failed';
          if (String(ev.text || '').trim()) s.messages.push({ text: String(ev.text) });
          s.plan = null;      /* ★ 第 3 条：同上 —— 活已经失败，别再挂一张等确认的卡 */
          break;
      }
      return s;
    },
    interjectMode: function (busy) { return busy ? 'queue' : 'idle'; },
    /* ---------- 后端接线（Task 8 的 7 个端点，相对 URL 便于单测） ---------- */
    endpoints: {
      state: '/api/agent/window/state',
      send: '/api/agent/window/send',
      confirm: '/api/agent/window/confirm',
      interject: '/api/agent/window/interject',
      stop: '/api/agent/window/stop',
      approve: '/api/agent/window/approve',
      upgrade: '/api/agent/window/upgrade'
    },
    sendBody: function (o) {
      const b = { session_id: (o && o.sessionId) || 'default', character_id: (o && o.characterId) || 'default',
                  cwd: (o && o.cwd) || '', text: (o && o.text) || '', mode: (o && o.mode) || 'auto' };
      /* ★ R64：仓库惯例是**同时**发角色名（chat.js / ws_client.js / companion.js）——
         后端 _resolve_char_id 只认能解析到真实角色配置的键，本地 Store uid 会被回落成 "default"。
         名字拿不到时就不带这个字段（保持 Task 11 的 5 字段形状，老用例钉着它）。 */
      if (o && o.characterName) b.character_name = o.characterName;
      return b;
    },
    /* 插话体：两种调用形式都收 ——
       ① Task 11 已发布契约（对象）：interjectPayload({sessionId, taskId, text, interrupt})
       ② Task 12 简报（位置）：interjectPayload(taskId, text, interrupt) —— 不带 session_id，
          由调用点补上（后端 interject 按 session_id 校验任务归属，缺了会答「任务不存在」）。
       text 一律是**用户原话**（R58：队列按设计存原文），这里不许包 brief、不许加前缀。
       interrupt 收成真布尔 —— 后端是 `bool(body.get("interrupt"))`，传字符串会恒真。 */
    interjectPayload: function (a, text, interrupt) {
      /* 零参/对象 → Task 11 契约；字符串 → Task 12 位置形式。`a == null` 走对象分支是为了
         `interjectPayload()` 仍返回带 session_id 的完整可发体（老用例钉着这条）。 */
      if (a == null || typeof a === 'object') {
        return { session_id: (a && a.sessionId) || 'default', task_id: (a && a.taskId) || '',
                 text: (a && a.text) || '', interrupt: !!(a && a.interrupt) };
      }
      return { task_id: a || '', text: text || '', interrupt: !!interrupt };
    },
    /* 状态条文案：状态 / 用量 / task_id。
       ★ 后端事件（agent_usage）只带 used/size，**没有 elapsed-ms 字段** —— 所以这里只可能出现
         两个真实数字，绝不出现「耗时/毫秒/秒」这类时长（与 toolLine 同一条规矩：没数据就不显示，
         不编造；断言见 test「不出现任何时长」）。用量为 0 也照实显示，不吞成空。 */
    statusBar: function (state) {
      const s = state || {};
      const u = s.usage || {};
      let line = '状态：' + (s.status || 'idle');
      if (u.used != null) line += ' ｜ 用量 ' + u.used + (u.size ? ('/' + u.size) : '') + ' tok';
      if (s.taskId) line += ' ｜ ' + s.taskId;
      return line;
    },
    /* 升档体：★ 二段式 —— 第一段 confirmed=false 只拿后端的确认文案，用户点过确认才发
       confirmed=true（后端 `if enable and not confirmed: return need_confirm`），绝不静默放开。
       收回（enable=false）后端不要确认（顺手 stop_all 关掉放开的进程），所以一段就够。 */
    upgradePayload: function (sessionId, enable, confirmed) {
      return { session_id: sessionId || 'default', enable: !!enable, confirmed: !!confirmed };
    },
    /* 控件条（Task 15）：工作目录 ｜ 权限档 ｜ 模型（+ 待确认的升档文案）。
       纯函数：输入就是 renderAgent 从 /state 攒下来的字段。
       ★ 权限档只有「项目级(workspace-write) / 临时放开(danger-full-access)」两档，**未知值按最保守的
         项目级显示**（宁可显示得比实际严，也不能把没放开的显示成已放开）；
       ★ 模型名优先用 ACP configOptions 报的当前值，state.model 次之，都没有就说「跟随 harness 默认」
         —— 不编模型名；
       ★ 确认文案一律用后端 message 原文（esc 过），不在前端另写一套吓人的话。 */
    controlBarHTML: function (o) {
      const c = o || {};
      const danger = String(c.permissionMode || '') === 'danger-full-access';
      const model = modelCurrent(c.configOptions) || String(c.model || '');
      const pending = c.pendingUpgrade || null;
      let html = '<span class="seg"><span class="lab">工作目录</span>'
        + '<span class="path">' + esc(c.cwd || '（还没定）') + '</span></span>'
        + '<span class="seg" data-aw-perm="' + (danger ? 'danger-full-access' : 'workspace-write') + '">'
        + '<span class="lab">权限</span><span class="val">' + (danger ? '临时放开' : '项目级') + '</span>'
        + (danger ? '<button class="aw-btn ghost small" id="awUpgradeBack">改回项目级</button>'
                  : '<button class="aw-btn ghost small" id="awUpgrade">临时放开</button>')
        + '</span>'
        + '<span class="seg"><span class="lab">模型</span><span class="val">'
        + esc(model || '跟随 harness 默认') + '</span></span>';
      if (pending) {
        html += '<div class="aw-ctl-ask"><span class="warn">' + esc(pending.message) + '</span>'
          + '<button class="aw-btn ok small" id="awUpgradeYes">确认放开</button>'
          + '<button class="aw-btn ghost small" id="awUpgradeNo">算了</button></div>';
      }
      return html;
    },
    /* 轨迹卡单行文案：`工具 · 摘要 · 状态/耗时占位`。
       耗时由 Task 11 的 WS 事件带进来（ev.ms / ev.duration_ms / ev.duration），没带就留占位符，不编造数字。 */
    toolLine: function (ev) {
      const e = ev || {};
      const tool = e.tool || '工具';
      const summary = e.summary || summarizeArgs(e.tool, e.args);
      const ms = Number(e.ms != null ? e.ms : (e.duration_ms != null ? e.duration_ms : e.duration));
      let tail;
      if (e.ok === false || e.status === 'failed') tail = '出错';
      else if (e.ok === true) tail = (ms > 0 ? ms + 'ms' : '完成');
      else if (e.status === 'in_progress' || e.status === 'running' || e.ok === null) tail = '进行中 · …';
      else tail = '待执行 · …';
      return tool + ' · ' + summary + ' · ' + tail;
    },
    planCardHTML: function (plan) {
      const p = (plan && typeof plan === 'object') ? plan : {};
      const steps = (Array.isArray(p.steps) ? p.steps : []).map(x => '<li>' + esc(x) + '</li>').join('');
      return '<div class="aw-plan"><div class="aw-plan-h">她给你一份方案</div><ul>' + steps + '</ul>'
        + '<div class="aw-plan-meta">风险：' + esc(p.risk || '—') + ' ｜ 预估：' + esc(p.eta || '—') + '</div>'
        + '<div class="aw-plan-btns"><button class="aw-btn ok" data-aw-confirm="1">就这么干</button>'
        + '<button class="aw-btn ghost" data-aw-edit="1">改一下</button></div></div>';
    },
    /* 审批卡倒计时的上限（秒）—— 暴露出来是为了与后端 _WAIT_TIMEOUT 对账，不是给调用方改的。 */
    approvalTimeoutSec: APPROVAL_WAIT_SEC,
    /* 还剩几秒（向上取整）：created_at 是后端 create_approval 盖的 epoch 秒（approval.py:63），
       get_approval 只摘掉 event，所以这个字段一定在。
       返回 null = 后端没给时间（老数据/畸形事件）→ 调用方宁可不显示，也不编一个数字出来。 */
    approvalRemain: function (approval, nowMs) {
      const ap = approval || {};
      const t = Number(ap.created_at);
      if (!isFinite(t) || t <= 0) return null;
      const now = (typeof nowMs === 'number' && isFinite(nowMs)) ? nowMs : Date.now();
      return Math.max(0, Math.ceil(t + AW.approvalTimeoutSec - now / 1000));
    },
    approvalCardHTML: function (ev, nowMs) {
      const ap = (ev && ev.approval) || {};
      const d = (ap.detail && typeof ap.detail === 'object') ? ap.detail : {};
      const p = d.path || d.command || '';
      /* ★ R29：显示她的理由（升档申请会带 justification），并标明这是"申请升档" */
      const reason = d.justification ? '<div class="agent-approval-detail">她的理由：' + esc(d.justification) + '</div>' : '';
      const up = d.sandbox_permissions ? '<div class="agent-approval-detail">申请权限：' + esc(d.sandbox_permissions) + '</div>' : '';
      /* ★ 收尾修复：180 秒倒计时（spec §6.2 卡面要求）。后端超时会自己判拒，但**一个 WS 事件都不推**
         （approval.py:129-132 只 _resolve，不通知），终止事件又只覆盖**任务**结算 ——
         所以「还剩多久／有没有超时」只能前端按 created_at 现算。
         ★ 必须**每次重画都算**（awPaint 每次都调本函数）：把剩余秒数缓存进 state 的话，
         卡会永远停在收到事件那一刻的秒数，超时态永远不出现（又成一张死卡）。 */
      const remain = AW.approvalRemain(ap, nowMs);
      const expired = (remain === 0);
      const clock = expired
        /* 本地归零 == 后端已经判拒：approval.py:131 一到点就 _resolve(rejected) 并摘掉 _pending，
           她那边收到的是 reject-once，确实停手了 —— 这么说不是猜。 */
        ? '<div class="agent-approval-detail">已超时 —— 她按拒绝停手了</div>'
        : (remain == null
            ? '<div class="agent-approval-detail">（超过 180 秒不回应，她会当作你拒绝并停手）</div>'
            : '<div class="agent-approval-detail">还剩 ' + remain + ' 秒 —— 超时就当她拒绝了（上限 180 秒）</div>');
      /* ★ 超时态：两个按钮**禁用**（这时点同意没有意义，后端只会回 ok:false），并给「收起这张卡」——
         原来这条卡没有出口：点同意 → 一行红字 → 卡还留着，用户只能盯着它。
         ★ 禁用之外仍保留 data-aw-* 与失败回执：万一这一下正好和超时撞在一起（他点的时候还剩 1 秒），
         后端回 ok:false，界面必须把「没送到」说出来，绝不静默 no-op。 */
      const btns = expired
        ? '<div class="agent-approval-btns"><button class="agent-btn ok" disabled data-aw-approve="' + esc(ap.id) + '">同意</button>'
          + '<button class="agent-btn no" disabled data-aw-reject="' + esc(ap.id) + '">拒绝</button>'
          + '<button class="aw-btn ghost small" data-aw-dismiss="approval">收起这张卡</button></div>'
        : '<div class="agent-approval-btns"><button class="agent-btn ok" data-aw-approve="' + esc(ap.id) + '">同意</button>'
          + '<button class="agent-btn no" data-aw-reject="' + esc(ap.id) + '">拒绝</button></div>';
      return '<div class="agent-approval">'
        + '<div class="agent-approval-title">⚠️ 需要你确认：' + esc(ap.summary || '执行一个操作') + '</div>'
        + (p ? '<div class="agent-approval-detail">目标：' + esc(p) + '</div>' : '')
        + up + reason + clock + btns + '</div>';
    },
    /* 轨迹卡 HTML：一行一条 trail（复用 .aw-trail* 样式，Task 11 只负责往里灌事件）。
       非数组一律当空处理 —— WS 来的畸形数据不许把整趟渲染打断。 */
    trailCardHTML: function (trail) {
      const rows = (Array.isArray(trail) ? trail : []).map(x => {
        const err = (x && (x.ok === false || x.status === 'failed')) ? ' err' : '';
        return '<div class="aw-trail-row' + err + '"><span class="t">' + esc(AW.toolLine(x)) + '</span></div>';
      }).join('');
      return '<div class="aw-trail">' + rows + '</div>';
    },
    /* 聊天消息卡：她的/模型的原样文本一律 esc，绝不拼进标签 */
    messageCardHTML: function (msg) {
      const m = msg || {};
      return '<div class="aw-msg">' + esc(m.text) + '</div>';
    },
    /* 聊天联动卡（Task 13）：**一行**、可点击、带 data-aw-open="1"（点击 → hzShowView('agent')）。
       单行是硬要求 —— 它挂在聊天流的正文里，多行会在两条消息之间撑出一道空隙。
       data-aw-task 记住这是哪次活，换任务时镜像据此重新数步数。 */
    liveCardHTML: function (taskId, step) {
      return '<div class="aw-live" data-aw-open="1" data-aw-task="' + esc(taskId) + '">'
        + liveText(step) + '</div>';
    }
  };

  root.AgentWindow = AW;

  /* ---------- DOM 引导：只在浏览器里跑（node --test require 本文件时不碰 DOM） ---------- */
  if (typeof document !== 'undefined') {
    let awState = AW.initialState();
    /* 二段式升档的中间态：第一段已发、等用户对后端那句 message 点头。
       null = 没有待确认的升档 —— 此时「确认放开」按钮既不存在也不生效。 */
    let awPendingUpgrade = null;
    /* 升档请求在飞时挡住后续点击（R64：连点不许发两次 confirmed:true） */
    let awUpgradeBusy = false;
    /* ★ R66：审批结果在飞时同样挡住后续点击 —— 连点「同意」原来会发两次 POST，
       第二次必然拿到 {ok:false}，界面还会冒出「多半已经自动拒了」的误报。 */
    let awApprovalBusy = false;

    function awPost(url, body) {
      return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                          body: JSON.stringify(body || {}) })
        .then(r => Promise.resolve(r.json()).catch(() => null).then(j => ({ r: r, j: j })))
        .then(x => {
          /* ★ R64：后端 500 走的是全局兜底中间件，回的是 **JSON**（`{error:{message:"服务器内部错误: X"}}`，
             见 backend/main.py:1095），fetch 不会 reject —— 不看 r.ok 就会把它当成功、对用户说
             「已记下/打断了/已经停了」，其实什么都没发生。
             测试桩里没有 ok 字段（undefined）时按老行为当成功，既有接线不受影响。 */
          if (x.r && x.r.ok === false) {
            return { ok: false,
                     error: (x.j && x.j.error && x.j.error.message) || (x.j && x.j.detail) || ('HTTP ' + x.r.status) };
          }
          /* ★ 响应体读不出来（200 但不是 JSON / body 读失败）时返回失败对象，**不许返回 null** ——
             调用方第一句就是 `res.kind`，null 会当场抛进没人接的 promise 里，界面静默什么都不发生。 */
          if (x.j == null) {
            return { ok: false, error: 'HTTP ' + ((x.r && x.r.status) || '?') + ' 响应读不出来' };
          }
          return x.j;
        })
        .catch(() => ({ ok: false }));
    }

    /* 失败一律进消息流说清楚（与 send 的「没派出去」同一条通道）。
       ★ 不置 status=failed —— 那是**任务**的状态；卡/话没送到不等于任务失败，
         置 failed 还会把「插话两档」按钮一起弄没（它们只认 running）。 */
    function awFail(state, what, res) {
      return AW.reduce(state, { type: 'agent_message',
                                text: '（' + what + '：' + ((res && res.error) || '后端没说是为什么') + '）' });
    }

    /* 当前会话角色（R64）：仓库惯例是取 `Chat.contact`，退回 `Chat.contactId` 在 Store 里的那条
       （memory.js 同款）。**不取 contacts[0]** —— 那可能根本不是你正在说话的那个人。 */
    function awCharacter() {
      const chat = root.Chat || null;
      let c = (chat && chat.contact) || null;
      if (!c && chat && chat.contactId && root.Store && Store.getContact) {
        try { c = Store.getContact(chat.contactId); } catch (e) { c = null; }
      }
      return (c && typeof c === 'object') ? c : null;
    }
    /* 角色隔离键：发名字（name → character_id → id），拿不到就 'default'（后端自己的兜底）。 */
    function awCharacterKeys() {
      const c = awCharacter();
      const name = c ? String(c.name || c.character_id || c.id || '') : '';
      const keys = { character_id: name || 'default' };
      if (name) keys.character_name = name;
      return keys;
    }
    /* 给卡片上的请求补角色键（confirm 也要 —— main.py 两个端点都做 _resolve_char_id）
       ★ R66：**每次发请求实时读**，不用挂载时的快照 —— `Chat.close()` 会把 `Chat.contact` 置空
       （快照下来的名字就永久丢了，回落成 'default' 桶），换了伴侣也会继续发旧名字。 */
    function awCharBody() {
      const ck = awCharacterKeys();
      const b = { character_id: ck.character_id };
      if (ck.character_name) b.character_name = ck.character_name;
      return b;
    }

    /* 把当前状态画进「干活」窗口。
       ★ 只读 state，不原地改它的嵌套对象：copy() 是浅拷贝 —— `state.plan.steps` /
         `state.approval.detail` 仍然**共享** WS 事件里的那一份（见 test 里的反向断言）。
         要改子对象先克隆（Object.assign({}, x, {...})），别在渲染/交互路径上就地改，
         否则事件对象会被后续读取方（Task 13 的聊天镜像）看到被污染的版本。
         本文件里对 state 的写入全部是**换引用**（`awState.plan = null`）或整对象重造
         （`awState = AW.reduce(...)`），没有一处写到 `.steps` / `.detail` 里面。 */
    function awPaint(sectionEl) {
      const st = sectionEl.querySelector('#awStream'); if (!st) return;
      const parts = [];
      if (awState.plan) parts.push(AW.planCardHTML(awState.plan));
      if (awState.trail.length) {
        parts.push('<div class="aw-trail">' + awState.trail.map(r =>
          '<div class="aw-trail-row' + (r.ok === false ? ' err' : '') + '"><span class="t">' + esc(r.tool || '工具') + '</span>'
          + '<span>' + esc(r.summary) + '</span>' + (r.result ? '<span class="t">' + esc(String(r.result).slice(0, 120)) + '</span>' : '')
          + '</div>').join('') + '</div>');
      }
      if (awState.approval) parts.push(AW.approvalCardHTML({ approval: awState.approval }));
      /* ★ R66：失败态卡住了（审批/计划没送到，卡又不该自己消失）——给一句原因 + 一个明确的出口。
         没有这个按钮，用户只能一直盯着一张点不动的卡（会话结束才消）。 */
      if (awState.cardStuck) {
        parts.push('<div class="aw-stuck">' + esc(awState.cardStuck.text)
          + '<button class="aw-btn ghost small" data-aw-dismiss="' + esc(awState.cardStuck.what) + '">收起这张卡</button></div>');
      }
      /* ★ 插话两档（Task 12）：任务在跑才出现 —— 排队（队尾，等这一轮完）与打断（cancel 后插队首）。
         空闲时「插话」没有对象，按钮就不该在。 */
      if (awState.status === 'running' && awState.taskId) {
        parts.push('<div class="aw-interject">'
          + '<button class="aw-btn ghost" id="awQueue">等她说完了再说</button>'
          + '<button class="aw-btn ghost" id="awInterrupt">打断并说</button></div>');
      }
      parts.push(awState.messages.map(m => '<div class="aw-msg">' + esc(m.text) + '</div>').join(''));
      st.innerHTML = parts.join('');
      const bar = sectionEl.querySelector('.aw-bar');
      if (bar) bar.textContent = AW.statusBar(awState);
      awPaintCtl(sectionEl);
    }

    /* 控件条（Task 15）单独一个重画函数：/state 回来时只更新它，不动 #awStream
       （否则会把刚收下的 WS 事件流抹掉）。 */
    function awPaintCtl(sectionEl) {
      const ctl = sectionEl.querySelector('#awCtl');
      if (ctl) ctl.innerHTML = AW.controlBarHTML(Object.assign({}, sectionEl.__aw || {},
                                                                { pendingUpgrade: awPendingUpgrade }));
    }

    /* 零参契约：`window.renderAgent()` 自己找 `#page-agent`（与既有 render* 一致，见文件头注释，
       flow-adapter.js 是零参调用）。兼容传参调用（测试 / 手动挂载时可以直接给节点）。 */
    root.renderAgent = function (sectionEl) {
      const el = sectionEl || document.getElementById('page-agent');
      if (!el) return;
      if (!el.querySelector('.aw-wrap')) {
        el.innerHTML = '<div class="aw-wrap">'
          + '<div class="aw-bar">正在检查她的手…</div>'
          + '<div class="aw-ctl" id="awCtl"></div>'
          + '<div class="aw-stream" id="awStream"></div>'
          + '<div class="aw-input"><input id="awText" placeholder="让她干点什么…（这里发出去就是 agent 模式）">'
          + '<button id="awSend" class="aw-btn ok">派活</button>'
          + '<button id="awStop" class="aw-btn ghost">停</button></div></div>';
        const sid = (root.Session && Session.getSessionId && Session.getSessionId()) || 'default';
        /* ★ R66：角色键**不在这里快照** —— 挂载时读一次会在 Chat.close()（contact 置空）或换伴侣后
           永久过期。cid/cname 一律在发请求时用 awCharacterKeys() 实时取（send / confirm 都是）。 */
        el.__aw = { sid: sid, cwd: '', permissionMode: '', model: '', configOptions: [] };
        fetch(AW.endpoints.state + '?session_id=' + encodeURIComponent(sid))
          .then(r => r.json()).then(st => {
            el.__aw.cwd = (st && st.cwd) || '';
            /* ★ 权限档/模型/configOptions 都来自 state()，控件条据此渲染（Task 15） */
            el.__aw.permissionMode = (st && st.permission_mode) || '';
            el.__aw.model = (st && st.model) || '';
            el.__aw.configOptions = (st && st.config_options) || [];
            const cap = (st && st.capability) || {};
            const bar = el.querySelector('.aw-bar');
            /* ★ R64：/state 回 500 时 body 是 `{error:{message:…}}`（main.py:1095），不是 capability ——
               原来会显示成「手：不可用 ——」空理由，把后端的话丢了。 */
            const why = (st && st.error && st.error.message) || cap.reason || '';
            if (bar) bar.textContent = (st && st.error)
              ? ('手：状态读不出来 —— ' + why)
              : (cap.ok ? ('手：就绪 ｜ 工作目录 ' + ((st && st.cwd) || '')) : ('手：不可用 —— ' + why));
            awPaintCtl(el);
          }).catch(() => {});
        const sendBtn = el.querySelector('#awSend');
        if (sendBtn) sendBtn.addEventListener('click', function () {
          const box = el.querySelector('#awText'); if (!box) return;
          const text = String(box.value || '').trim(); if (!text) return;
          box.value = '';
          /* ★ R66：角色键在这里**实时读**（挂载快照会被 Chat.close() / 换伴侣弄过期） */
          const ck = awCharacterKeys();
          awPost(AW.endpoints.send, AW.sendBody({ sessionId: el.__aw.sid,
                                                  characterId: ck.character_id,
                                                  characterName: ck.character_name,
                                                  cwd: el.__aw.cwd, text: text })).then(res => {
            /* ★ 没派出去要说出来（后端 ok:false / 网络断了都在这里），不许静默什么都不发生 */
            if (res && res.ok === false) {
              awState = AW.reduce(awState, { type: 'agent_error',
                                             text: '没派出去：' + (res.error || res.reply || '未知原因') });
            }
            else if (res.kind === 'plan') { awState = AW.reduce(awState, { type: 'agent_plan', plan: res.plan, task_id: res.task_id }); }
            else if (res.kind === 'chat') { awState = AW.reduce(awState, { type: 'agent_message', text: '（这句我当成聊天了 —— 想让我动手就说「帮我…」）' }); }
            /* ★ started/queued：活已经派出去了但还没出事件 —— 立刻记住 task_id（「停」据此可用）
               并把状态置 running，否则从点「派活」到第一个 WS 事件之间界面是死的 */
            else if (res.kind === 'started' || res.kind === 'queued') {
              awState = AW.reduce(awState, { type: 'agent_thinking', task_id: res.task_id });
            }
            awPaint(el);
          });
        });
        const stopBtn = el.querySelector('#awStop');
        if (stopBtn) stopBtn.addEventListener('click', function () {
          if (!awState.taskId) return;
          const stopping = awState.taskId;
          awPost(AW.endpoints.stop, { session_id: el.__aw.sid, task_id: stopping }).then(res => {
            const failed = !!(res && res.ok === false);
            /* ★ R64：只有真停掉才置 stopped —— 500 / ok:false 时任务还在跑，显示成已停会让他以为停了 */
            awState = failed ? awFail(awState, '没能停掉', res)
                             : AW.reduce(awState, { type: 'agent_stopped' });
            /* ★ R66：停成功要**立刻**把聊天里那张「她正在干活」收掉。后端结算时也会推 agent_stopped，
               但那要等 cancel 真的落地（异步），中间这段时间聊天里挂着的是**假的**进行中。
               没停掉（failed）时绝不能收 —— 她还在干活，卡得留着。 */
            if (!failed) {
              try {
                root.AgentWindowMirrorToChat({ type: 'agent_stopped', task_id: stopping });
              } catch (e) { /* 镜像失败不影响「停」本身 */ }
            }
            /* ★ 折进来的第 3 条：停掉一张**还没开工的计划**时，后端会把"还没跑就被丢掉的原话"
               一并回事（`dropped` / `dropped_texts`）—— 必须说出来，不能吞：
               他以为那句话还排着，其实永远不会再跑了（静默丢用户的话 = R58 那类 Critical）。 */
            if (!failed && res && res.dropped) {
              const texts = Array.isArray(res.dropped_texts) ? res.dropped_texts : [];
              awState = AW.reduce(awState, { type: 'agent_message',
                text: '（停了 —— 还没跑的那 ' + res.dropped + ' 句一起丢掉了'
                      + (texts.length ? '：' + texts.join(' ／ ') : '') + '，要的话再发一次）' });
            }
            awPaint(el);
          });
        });
        /* 插话两档（Task 12）：按钮是 innerHTML 拼出来的 → 交给 section 上那一个事件委托（见下）。
           ★ 后端 interject 的判据是「任务存在 **且** session_id 对得上」，所以必须显式带 session_id
             （简报里的 payload 只给 task_id/text/interrupt，session_id 在调用点补）；
           ★ text 是用户原话，不加包装（R58：队列按设计存原文）。 */
        function sendInterject(interrupt) {
          const box = el.querySelector('#awText');
          if (!box) return;
          const text = String(box.value || '').trim();
          if (!text) {
            /* ★ R64：空输入点按钮原来什么都不发生（静默 no-op）—— 说一句，别让他以为按钮坏了 */
            awState = AW.reduce(awState, { type: 'agent_message', text: '（输入框是空的，这句话没插进去）' });
            awPaint(el);
            return;
          }
          if (!awState.taskId) return;
          box.value = '';
          const body = Object.assign({ session_id: (el.__aw && el.__aw.sid) || 'default' },
                                     AW.interjectPayload(awState.taskId, text, interrupt));
          awPost(AW.endpoints.interject, body).then(res => {
            let hint;
            if (res && res.ok === false) {
              /* ★ 后端「已喊停 / 任务没在跑 / HTTP 500」都在这里 —— 原样贴出来，绝不吞
                 （吞了他会以为话带到了）；★ R64：顺手把原话还回输入框，别让他白打一遍 */
              box.value = text;
              hint = '插话没递进去：' + (res.error || '未知原因');
            } else {
              hint = interrupt ? '打断了，这就跟她说'
                               : ('已记下，等这一轮结束就说' + (res && res.pending ? ('（排队 ' + res.pending + '）') : ''));
            }
            awState = AW.reduce(awState, { type: 'agent_message', text: '（' + hint + '）' });
            awPaint(el);
          });
        }
        /* 审批结果（R64）：★ 只有后端说 ok 才收卡。approve 回 `{"ok": false}` 时多半是这条审批已经不在等待了
           （approval.wait_for_approval 180 秒超时会自动判拒并从 _pending 摘掉）——
           卡一收他就会以为「同意了」，其实她早就按拒绝停手了。
           ★ R66：① 在飞守卫：连点原来会发两次 POST，第二次的 ok:false 是**我们自己**造的，
                     不该当成「后端说没同意成」说出去；
                  ② 失败时给「收起这张卡」—— 卡留着是对的，但不能只留一句红字、没有出口。 */
        function awSettleApproval(approvalId, allow) {
          if (awApprovalBusy) return;
          awApprovalBusy = true;
          awPost(AW.endpoints.approve, { approval_id: approvalId, allow: !!allow }).then(res => {
            awApprovalBusy = false;
            if (res && res.ok === false) {
              awState = Object.assign({}, awState, {
                cardStuck: { what: 'approval',
                             text: '（' + (allow ? '同意' : '拒绝') + '没送到：'
                                   + ((res && res.error) || '后端说这条审批已经不在等待了')
                                   + ' —— 超过 180 秒不回应会被自动当拒绝，这条多半已经自动拒了）' }
              });
            } else {
              awState = Object.assign({}, awState, { approval: null, cardStuck: null });
            }
            awPaint(el);
          });
        }
        /* 权限档（Task 15）：★ 二段式 —— doUpgrade(enable, false) 只发第一段拿后端的确认文案；
           用户点【确认放开】才 doUpgrade(enable, true) 发第二段。收回（enable=false）后端不要确认。 */
        function doUpgrade(enable, confirmed) {
          if (awUpgradeBusy) return;               /* ★ R64：在飞守卫 —— 连点不许发两次 */
          awUpgradeBusy = true;
          const sid = (el.__aw && el.__aw.sid) || 'default';
          awPost(AW.endpoints.upgrade, AW.upgradePayload(sid, enable, confirmed)).then(res => {
            awUpgradeBusy = false;
            if (res && res.need_confirm) {
              awPendingUpgrade = { enable: !!enable, message: String(res.message || '') };
            } else if (res && res.ok === false) {
              /* 升档失败 ≠ 任务失败：走 agent_message，别把 status 置成 failed（那是任务态） */
              awPendingUpgrade = null;
              awState = AW.reduce(awState, { type: 'agent_message',
                                             text: '（权限档没改成：' + (res.error || '未知原因') + '）' });
            } else {
              awPendingUpgrade = null;
              /* 档位只认后端回的 permission_mode —— 没回就说「后端没回档位」，不自己假定已放开 */
              if (res && res.permission_mode) el.__aw.permissionMode = String(res.permission_mode);
              awState = AW.reduce(awState, { type: 'agent_message',
                                             text: '（权限档：' + ((res && res.permission_mode) || '后端没回档位') + '）' });
            }
            awPaint(el);
          });
        }
        /* 卡片上的按钮是 innerHTML 拼出来的，所以用事件委托；
           属性一律双引号（见文件头 esc() 注释）。 */
        el.addEventListener('click', function (e) {
          const t = e.target;
          if (!t) return;
          if (t.id === 'awQueue') sendInterject(false);
          if (t.id === 'awInterrupt') sendInterject(true);
          if (t.id === 'awUpgrade') doUpgrade(true, false);
          if (t.id === 'awUpgradeBack') doUpgrade(false, false);
          /* 第二段只在「确实有待确认的升档」时才可能发出去 —— 单凭一个 id 不许静默放开。
             ★ R64：点下去就**同步**清掉待确认态并重画（确认行立刻收起，连点也不会再走一遍） */
          if (t.id === 'awUpgradeYes' && awPendingUpgrade) {
            const enable = awPendingUpgrade.enable;
            awPendingUpgrade = null;
            awPaint(el);
            doUpgrade(enable, true);
          }
          if (t.id === 'awUpgradeNo' && awPendingUpgrade) { awPendingUpgrade = null; awPaint(el); }
          if (!t.dataset) return;
          if (t.dataset.awConfirm) {
            /* ★ R64：确认没送到就别收计划卡（收了会以为她开工了）；
               ★ R66：确认失败同样给「收起这张卡」出口（不然只能重试、不能取消） */
            awPost(AW.endpoints.confirm, Object.assign({ session_id: el.__aw.sid, task_id: awState.taskId },
                                                       awCharBody())).then(res => {
              awState = (res && res.ok === false)
                ? Object.assign({}, awFail(awState, '确认没送到', res),
                                { cardStuck: { what: 'plan',
                                               text: '（计划卡没能确认 —— 可以再点一次「就这么干」，或者点右边收掉它）' } })
                : Object.assign({}, awState, { plan: null, cardStuck: null });
              awPaint(el);
            });
          }
          /* ★ R66：失败态的明确出口 —— 点「收起这张卡」才收掉那张卡（审批/计划各一处） */
          if (t.dataset.awDismiss) {
            awState = (t.dataset.awDismiss === 'plan')
              ? Object.assign({}, awState, { plan: null, cardStuck: null })
              : Object.assign({}, awState, { approval: null, cardStuck: null });
            awPaint(el);
          }
          if (t.dataset.awApprove) awSettleApproval(t.dataset.awApprove, true);
          if (t.dataset.awReject) awSettleApproval(t.dataset.awReject, false);
        });
      }
      awPaint(el);
    };

    /* ★ R22：把「她正在干活」联动卡镜像到聊天里。
       必须挂在 **WS 路径** 上（下面 AgentWindowIngest 调用它）—— chat.js:handleAgentEvent 只接
       SSE（chat_stream.js），ACP 的 agent_* 事件根本到不了那里，插在 chat.js 里卡永远不会出现。
       本函数只碰 DOM、不碰 awState：「干活」页没打开时也要照挂。
       ★ R66：① 步数按 **task_id 分桶**（两件活交错时各算各的，不把 B 的数算到 A 头上）；
              ② 「收」由后端结算时真推的终止事件驱动（window.py `_push_agent_terminal`），
                本地「停」按钮另走一次（见上面的 stop 处理）。 */
    const _awLiveSteps = {};  /* task_id → 已数到第几步（收卡时把对应桶清掉） */
    let _awLiveTask = '';     /* 卡当前属于哪次活 */
    function awOpenAgentView() {
      /* flow-app.js 的 hzShowView 是全局函数 —— 万一它没起来就当没点过（镜像不许影响主流程） */
      const show = (typeof root.hzShowView === 'function') ? root.hzShowView : null;
      if (show) show('agent');
    }
    root.AgentWindowMirrorToChat = function (ev) {
      try {
        if (!ev || !ev.type) return;
        if (ev.type === 'agent_tool') {
          const liveEl = document.querySelector('.aw-live');
          if (!liveEl) {
            if (!ev.task_id) return;                  /* 没有 task_id 就不镜像，避免误挂到聊天上 */
            const body = document.querySelector('#chat-body') || document.querySelector('#cpBody');
            if (!body) return;
            _awLiveTask = String(ev.task_id);
            _awLiveSteps[_awLiveTask] = 1;
            const wrap = document.createElement('div');
            wrap.innerHTML = AW.liveCardHTML(ev.task_id, 1);
            const card = wrap.firstChild;
            if (!card) return;                        /* 拼不出节点就宁可没卡，绝不抛进 WS 分发里 */
            card.addEventListener('click', function () { try { awOpenAgentView(); } catch (e) {} });
            body.appendChild(card);
            body.scrollTop = body.scrollHeight;       /* 滚到底，否则他根本看不见这张卡 */
          } else {
            /* 旧卡还在时又来了**另一次活**：卡改认新任务，步数接着**那个任务自己的桶**数
               （既不从 0 重来，也不把别的任务的步数算进来） */
            if (ev.task_id && String(ev.task_id) !== _awLiveTask) {
              _awLiveTask = String(ev.task_id);
              if (liveEl.dataset) liveEl.dataset.awTask = _awLiveTask;
            }
            const step = (_awLiveSteps[_awLiveTask] || 0) + 1;
            _awLiveSteps[_awLiveTask] = step;
            liveEl.innerHTML = liveText(step);
          }
        } else if (ev.type === 'agent_final' || ev.type === 'agent_stopped' || ev.type === 'agent_error') {
          const done = document.querySelector('.aw-live');
          if (done && done.parentNode) done.parentNode.removeChild(done);
          /* 这次活的计数随之作废；事件带 task_id 时把那个桶也清掉（下一件活不会带旧步数） */
          if (_awLiveTask) delete _awLiveSteps[_awLiveTask];
          if (ev.task_id) delete _awLiveSteps[String(ev.task_id)];
          _awLiveTask = '';
        }
      } catch (e) { /* 镜像失败不能影响主流程 */ }
    };

    /* WS 来的 agent_* 事件（★ R19：这条通道与 chat.js 的 SSE 处理器是互不相通的两条路）
       ★ R22：聊天侧的「她正在干活」联动卡也从**这里**插 —— WS 事件根本到不了 chat.js 的 handleAgentEvent */
    root.AgentWindowIngest = function (ev) {
      if (!ev || !ev.type || ev.type.indexOf('agent_') !== 0) return false;
      awState = AW.reduce(awState, ev);
      const sec = document.getElementById('page-agent');
      if (sec && document.body.dataset.page === 'agent') awPaint(sec);
      try { root.AgentWindowMirrorToChat && root.AgentWindowMirrorToChat(ev); } catch (e) {}
      return true;   /* ★ 认领该事件：调用方必须据此 return，避免其它处理器重复渲染 */
    };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = AW;
})(typeof window !== 'undefined' ? window : globalThis);
