'use strict';
/* ============================================================
   记忆日志：每日 23:00 自动生成当天对话的结构化记忆
   - 生成：AI 读取当天对话 → 按严格格式输出 → 持久化到 contact.logs
   - 页面：日记风格渲染（头部渐变 / 分数圆环 / 时间轴 / 情绪流转 / 心里话）
   ============================================================ */

/** 日期字符串 YYYY-MM-DD */
function dateStrOf(ts) {
  const d = new Date(ts);
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

/**
 * 转写里「TA」那一侧的说话人标签。
 *
 * ★ 2026-09-16 用户报告：「你干嘛思考链里叫我用户啊」——
 *   转写（喂给模型的对话记录）原来把对方写成泛称「用户：」，模型读多了就
 *   照抄进思考链，思考链里也叫「用户」。现在改用**角色卡配的称呼**
 *   （contact.callsYou，如「宝」/TA 的名字），跟人设里的称呼一致。
 *   标签形状保持不变（`[HH:MM] <称呼>：`），后端解析器照旧能用。
 */
function userCallName(contact) {
  const c = (contact && contact.callsYou || '').trim();
  if (c) return c;
  const s = Store.getSettings() || {};
  const nick = String(s.userProfile || '').trim();
  return (nick.split(/[\s，,。；;：:\n]/)[0] || '').trim() || 'TA';
}

/** 日志生成的严格格式要求（带标记，便于规范渲染） */
const LOG_FORMAT = `你正在为「今日记忆日志」写一份结构化记录。请阅读下面的对话，严格按以下格式输出，不要输出任何多余内容（不要"好的""以下是"等开场白，不要解释，不要空行注释）：

第一行：日期 分数/10 情绪标签（分数 0-10，情绪标签 2-4 个字）。例：2026-08-17  8/10 开心甜蜜

第二行：主题：一行主题概括（15 字以内）

下一行：时间轴：
接下来每一行是一条，格式：HH:MM-HH:MM ⏳ 关键事件（30 字以内，提炼互动，不要复制对话；提到用户时用你对 TA 的称呼）。例：
18:34-19:19 ⏳ 宝贝吃饭未回，我有些担心；解释后约定下次提前告知

下一行：情绪流转：用 → 串联全天情绪变化。例：等待小烦躁→换头像开心→暧昧升温→互相思念→温馨承诺

下一行：【角色内心感想】
下一段：第一人称写 150 字以内的内心独白：是你自己心里的想法（不是发给用户的聊天），带你的角色性格，像写日记一样真诚自然。`;

/** 为某个伴侣生成某一天的记忆日志（返回文本或 null） */
async function generateLogFor(contact, dateStr) {
  const conv = Store.getConversation(contact.id);
  if (!conv) return null;
  const msgs = Store.getMessages(conv.id).filter(
    (m) => m.ts && dateStrOf(m.ts) === dateStr && m.status !== 'error' && m.status !== 'recalled'
  );
  if (!msgs.length) return null;

  const _who = userCallName(contact);
  const lines = msgs.slice(-80).map((m) => {
    const t = fmtClock(m.ts);
    if (m.type === 'sticker') return '[' + t + '] TA：[表情包' + m.content + ']';
    if (m.role === 'user') return '[' + t + '] ' + _who + '：' + (m.content || (m.image ? '[图片]' : ''));
    return '[' + t + '] TA：' + (m.content || (m.image ? '[图片]' : ''));
  });
  const transcript = lines.join('\n');

  const settings = Store.getSettings();
  const brain = brainConfig(contact, settings);
  const sys = composeSystem(contact) + '\n\n' + LOG_FORMAT;
  const usr = '今天是 ' + dateStr + '。以下是今天（' + dateStr + '）的全部对话记录：\n\n' + transcript;

  let out = '';
  await streamAI({
    model: brain.model, baseUrl: brain.baseUrl,
    messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }],
    key: brain.key,
    mode: settings.mode,
    proxyUrl: settings.proxyUrl,
    // ★ 内部生成：usr 是"今天的全部对话记录"，属于喂给模型的素材而非真实发言。
    //   不标记的话会被落库成 user 消息，下轮又被当历史喂回去，
    //   造成对话记录自我嵌套膨胀（"…分钟后我会发『以下是今天的全部对话记录』…"）。
    skipUserPersist: true,
  }, (d) => { out += d; });

  out = out.trim();
  if (!out) return null;
  const cleaned = out.replace(/^(好的|以下是|这是|好的，以下是)[^\n]*\n/i, '').trim();

  const logs = (contact.logs || []).filter((l) => l.date !== dateStr);
  logs.unshift({ date: dateStr, text: cleaned, ts: Date.now() });
  if (logs.length > 90) logs.length = 90;
  Store.updateContact(contact.id, { logs });
  // 同步到后端三层记忆，换浏览器或清理缓存后仍可恢复。
  try {
    const sid = window.Session?.getSessionId?.() || 'default';
    await fetch('/api/memory/daily_reports', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sid,
        character_id: contact.name || contact.id || 'default',
        date: dateStr,
        content: cleaned
      })
    });
  } catch (_) { /* 本地记录已经保存，后端暂不可用时不阻断 */ }
  return cleaned;
}

/** 后台检查：23:00 后（或次日补生成）为当天/最近有对话的伴侣生成日志 + 手帐 */
async function maybeRunDailyLogs() {
  const settings = Store.getSettings();
  if (!settings.dailyLog) return;
  const hour = new Date().getHours();
  const todayStr = dateStrOf(Date.now());

  for (const c of Store.listContacts()) {
    const conv = Store.getConversation(c.id);
    if (!conv) continue;
    const msgs = Store.getMessages(conv.id);
    let latestDate = null;
    for (const m of msgs) {
      const d = m.ts ? dateStrOf(m.ts) : null;
      if (d && (!latestDate || d > latestDate)) latestDate = d;
    }
    if (!latestDate) continue;
    decayIntimacy(c); // 长期不联系，亲密度缓慢回落
    if ((c.logs || []).some((l) => l.date === latestDate)) continue;
    const eligible = latestDate < todayStr || (latestDate === todayStr && hour >= 23);
    if (!eligible) continue;
    try {
      await generateLogFor(c, latestDate);
    } catch (_) { /* 没配 Key 等情况静默跳过 */ }
    try {
      await generateHandbookFor(c, latestDate);
    } catch (_) { /* 忽略 */ }
    await sleep(500);
  }
}

/** 手帐：从某天对话里自动捕捉碎片想法，分类归档 */
async function generateHandbookFor(contact, dateStr) {
  const conv = Store.getConversation(contact.id);
  if (!conv) return null;
  const msgs = Store.getMessages(conv.id).filter(
    (m) => m.ts && dateStrOf(m.ts) === dateStr && m.role === 'user' && m.status !== 'error' && m.status !== 'recalled'
  );
  if (!msgs.length) return null;
  // 当天已有手帐则跳过
  if (Store.handbook.list().some((x) => x.date === dateStr && x.contactName === contact.name)) return null;

  const transcript = msgs.slice(-60).map((m) => '[' + fmtClock(m.ts) + '] ' + userCallName(contact) + '：' + (m.content || '[图片]')).join('\n');
  const settings = Store.getSettings();
  const brain = brainConfig(contact, settings);
  const sys = composeSystem(contact) + '\n\n从下面的用户对话里捕捉 TA 有价值的碎片想法（灵感、计划、目标、情绪、值得记住的事），整理成手帐条目，最多 5 条。严格按格式输出，不要多余内容：\n【手帐条目】\n分类：灵感/计划/目标/心情/其他（选一个）\n标题：15 字内\n内容：50 字内\n（多条重复以上三行；当天没有值得记的，只输出：无）';
  const usr = '日期：' + dateStr + '\n用户说的话：\n' + transcript;

  let out = '';
  await streamAI({
    model: brain.model, baseUrl: brain.baseUrl,
    messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }],
    key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
  }, (d) => { out += d; });

  const blocks = (out.match(/【手帐条目】[\s\S]*?(?=【手帐条目】|$)/g) || []);
  let added = 0;
  for (const b of blocks) {
    const cat = (b.match(/分类[:：]\s*(.*)/) || [])[1] || '其他';
    const title = (b.match(/标题[:：]\s*(.*)/) || [])[1] || '';
    const content = (b.match(/内容[:：]\s*(.*)/) || [])[1] || '';
    if (!title && !content) continue;
    Store.handbook.add({ id: Store.uid(), date: dateStr, category: cat.trim(), title: title.trim(), content: content.trim(), contactName: contact.name, createdAt: Date.now() });
    added++;
  }
  return added;
}

/* ============================================================
   记忆日志页面
   ============================================================ */
const Logs = {
  contactId: null,
  generating: false,

  open(contactId) {
    this.contactId = contactId;
    this.render();
    $('#logs-page').classList.add('open');
  },

  close() {
    $('#logs-page').classList.remove('open');
  },

  get c() { return Store.getContact(this.contactId); },

  render() {
    const c = this.c;
    if (!c) { this.close(); return; }
    const body = $('#logs-body');
    body.innerHTML = '';

    const logs = (c.logs || []).slice();

    // 顶部说明 + 统计
    body.appendChild(h('div', { class: 'help-box', style: 'margin:2px 2px 12px', text:
      '每晚 23:00 自动把当天对话整理成记忆；错过会在下次打开时补生成。\n' +
      '日志保存在手机本地，是 TA 的「每日回忆」' }));

    if (!logs.length) {
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('journal'),
        h('div', { text: '还没有记忆日志' }),
        h('div', { style: 'margin-top:8px', text: '今晚 23:00 后自动生成，或点右上角「立即生成」' }),
      ));
      return;
    }

    const dates = logs.map((l) => l.date).filter(Boolean);
    if (dates.length) {
      body.appendChild(h('div', { class: 'log-stats' },
        h('span', { text: '📚 共 ' + logs.length + ' 篇记忆' }),
        h('span', { text: '最近：' + dates[0] }),
      ));
    }

    for (const log of logs) {
      body.appendChild(renderLogCard(log.text));
    }
  },

  /** 手动生成：最近的（今天或最近一次有对话的）那一天 */
  async generateNow(contactId) {
    if (this.generating) return;
    if (contactId) this.contactId = contactId;
    const c = this.c;
    if (!c) return;
    const conv = Store.getConversation(c.id);
    if (!conv) { toast('还没有对话记录'); return; }
    const msgs = Store.getMessages(conv.id);
    let latestDate = null;
    for (const m of msgs) {
      const d = m.ts ? dateStrOf(m.ts) : null;
      if (d && (!latestDate || d > latestDate)) latestDate = d;
    }
    if (!latestDate) { toast('还没有对话记录'); return; }
    if ((c.logs || []).some((l) => l.date === latestDate)) {
      if (!confirm('「' + latestDate + '」的记忆已生成，重新生成覆盖？')) return;
    }
    this.generating = true;
    const btn = $('#logs-gen');
    btn.textContent = '生成中…';
    btn.disabled = true;
    toast('正在整理今天的记忆…');
    try {
      await generateLogFor(c, latestDate);
      this.render();
      toast('已生成 ' + latestDate + ' 的记忆');
    } catch (err) {
      toast('生成失败：' + (err.message || err), 3200);
    }
    this.generating = false;
    btn.textContent = '立即生成';
    btn.disabled = false;
  },
};

/* ---------------- 解析日志文本 ---------------- */
function parseLog(text) {
  const lines = text.split('\n').map((s) => s.trim()).filter(Boolean);
  const mDate = text.match(/(\d{4}-\d{2}-\d{2})/);
  const date = mDate ? mDate[1] : '';
  const mScore = text.match(/(\d{1,2})\s*\/\s*10/);
  const score = mScore ? Math.min(10, Math.max(0, Number(mScore[1]))) : null;

  // 情绪标签：头部行去掉日期与分数后的剩余
  const headerLine = lines.find((l) => /^\d{4}-\d{2}-\d{2}/.test(l)) || lines[0] || '';
  let label = headerLine;
  if (mDate) label = label.replace(mDate[1], '');
  if (mScore) label = label.replace(mScore[0], '');
  label = label.replace(/[｜|\s/]+/g, ' ').trim();

  // 主题
  let theme = '';
  const themeLine = lines.find((l) => /^主题[:：]/.test(l)) || lines[1] || '';
  theme = themeLine.replace(/^主题[:：]\s*/, '').trim();

  // 内心感想
  const monoIdx = lines.findIndex((l) => l.indexOf('【角色内心感想】') !== -1);
  const monologue = monoIdx !== -1 ? lines.slice(monoIdx + 1).join('\n') : '';

  // 时间轴
  const timeRe = /^(\d{1,2}:\d{2}\s*[-~至]\s*\d{1,2}:\d{2}|\d{1,2}:\d{2})/;
  const tlStart = lines.findIndex((l) => /^时间轴[:：]/.test(l));
  const scopeStart = tlStart !== -1 ? tlStart + 1 : 2;
  const scopeEnd = monoIdx !== -1 ? monoIdx : lines.length;
  const timeline = [];
  for (let i = scopeStart; i < scopeEnd; i++) {
    const line = lines[i];
    if (!line) continue;
    if (/^情绪流转[:：]/.test(line) || line.indexOf('→') !== -1) continue;
    if (tlStart !== -1 || timeRe.test(line)) {
      const mt = line.match(timeRe);
      timeline.push({
        time: mt ? mt[1] : '',
        text: mt ? line.slice(mt[1].length).replace(/^[⏳\s]*/, '').trim() : line,
      });
    }
  }

  // 情绪流转
  let emotionFlow = [];
  const emoLine = lines.find((l) => /^情绪流转[:：]/.test(l)) || lines.find((l) => l.indexOf('→') !== -1) || '';
  const emoText = emoLine.replace(/^情绪流转[:：]\s*/, '').trim();
  if (emoText) emotionFlow = emoText.split('→').map((s) => s.trim()).filter(Boolean);

  return { date, score, label, theme, timeline, emotionFlow, monologue };
}

/* ---------------- 渲染日志卡片（日记风格） ---------------- */
function renderLogCard(text) {
  const p = parseLog(text);
  const card = h('div', { class: 'log-card' });

  // 顶部渐变横幅：日期 + 分数圆环 + 情绪胶囊
  const cls = p.score == null ? 'mid' : (p.score >= 8 ? 'good' : (p.score >= 5 ? 'mid' : 'low'));
  const top = h('div', { class: 'log-top ' + cls });
  const dateBox = h('div', { class: 'log-date-box' });
  if (p.date) {
    const d = new Date(p.date + 'T00:00:00');
    const week = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
    dateBox.appendChild(h('div', { class: 'log-date-big', text: Number(p.date.slice(5, 7)) + '月' + Number(p.date.slice(8, 10)) + '日' }));
    dateBox.appendChild(h('div', { class: 'log-date-sub', text: p.date.slice(0, 4) + ' · ' + week }));
  } else {
    dateBox.appendChild(h('div', { class: 'log-date-big', text: '记忆' }));
    dateBox.appendChild(h('div', { class: 'log-date-sub', text: 'TA 的每日回忆' }));
  }
  const right = h('div', { class: 'log-right' });
  if (p.score != null) {
    right.appendChild(h('div', { class: 'log-score-ring' }, String(p.score), h('small', { text: '/10' })));
  }
  if (p.label) right.appendChild(h('div', { class: 'log-emotion-pill', text: p.label }));
  top.appendChild(dateBox);
  top.appendChild(right);
  card.appendChild(top);

  if (p.theme) card.appendChild(h('div', { class: 'log-theme', text: '「' + p.theme + '」' }));

  if (p.timeline.length) {
    const tl = h('div', { class: 'log-timeline' });
    tl.appendChild(h('div', { class: 'log-sec-title', text: '🕐 时间轴' }));
    for (const it of p.timeline) {
      tl.appendChild(h('div', { class: 'log-tl-item' },
        h('span', { class: 'log-tl-time', text: it.time || '·' }),
        h('span', { class: 'log-tl-text', text: it.text }),
      ));
    }
    card.appendChild(tl);
  }

  if (p.emotionFlow.length) {
    card.appendChild(h('div', { class: 'log-sec-title', text: '🎢 情绪流转' }));
    const flow = h('div', { class: 'log-emotion-flow' });
    p.emotionFlow.forEach((e, i) => {
      if (i) flow.appendChild(h('span', { class: 'log-emo-arrow', text: '→' }));
      flow.appendChild(h('span', { class: 'log-emo-chip', text: e }));
    });
    card.appendChild(flow);
  }

  if (p.monologue) {
    card.appendChild(h('div', { class: 'log-mono-block' },
      h('div', { class: 'log-mono-label', text: '💭 TA 的心里话' }),
      h('div', { class: 'log-mono-text', text: p.monologue }),
    ));
  }

  return card;
}

/* ---------------- 事件与启动 ---------------- */
$('#logs-back').addEventListener('click', () => Logs.close());
$('#logs-gen').addEventListener('click', () => Logs.generateNow());

/** 启动：首次检查 + 每 10 分钟检查一次 */
function startDailyLogs() {
  setTimeout(() => { maybeRunDailyLogs().catch(() => {}); }, 15 * 1000);
  setInterval(() => { maybeRunDailyLogs().catch(() => {}); }, 10 * 60000);
}
