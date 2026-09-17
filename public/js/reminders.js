'use strict';
/* ============================================================
   定时任务：让 AI 拥有"时间概念"
   - 检测到"X分钟后回答我 / 明早提醒我"等请求 → 自动排期
   - 到点后 AI 真的会主动发一条消息（含人物性格）
   ============================================================ */

/** 从用户文本里解析延迟时长（毫秒），解析不到返回 null */
function parseDelay(text) {
  let m = text.match(/(\d+)\s*分钟/);
  if (m) return Number(m[1]) * 60000;
  m = text.match(/(\d+)\s*(?:小时|个钟)/);
  if (m) return Number(m[1]) * 3600000;
  if (/一分钟后|一分钟后|一分钟/.test(text)) return 60000;
  if (/半小时/.test(text)) return 1800000;
  m = text.match(/(\d{1,2})\s*点/);
  if (m) {
    const h = Number(m[1]);
    if (h >= 0 && h <= 24) {
      const now = new Date();
      const at = new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, 0, 0);
      if (at.getTime() <= now.getTime()) at.setTime(at.getTime() + 86400000);
      return at.getTime() - now.getTime();
    }
  }
  if (/明早|明天早上/.test(text)) {
    const now = new Date();
    const at = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, 8, 0, 0);
    return at.getTime() - now.getTime();
  }
  return null;
}

/** 剩余时间的人类可读文案 */
function fmtLeft(ms) {
  const sec = Math.max(1, Math.round(ms / 1000));
  if (sec < 60) return sec + ' 秒';
  const min = Math.round(sec / 60);
  if (min < 60) return min + ' 分钟';
  return Math.floor(min / 60) + ' 小时 ' + (min % 60) + ' 分';
}

const Reminders = {
  list() {
    return Store.reminders.list().filter((r) => !r.done).sort((a, b) => a.at - b.at);
  },
  add(contactId, at, task) {
    return Store.reminders.add({ id: Store.uid(), contactId, at, task, done: false });
  },
  cancel(id) {
    Store.reminders.remove(id);
  },

  /** 每 15 秒检查一次到期任务 */
  async tick() {
    const now = Date.now();
    const due = Store.reminders.list().filter((r) => !r.done && r.at <= now);
    for (const r of due) {
      try { await fireReminder(r); } catch (_) { /* 单条失败不影响其他 */ }
    }
  },
};

/** 到点执行：让 AI 按当时的要求主动发消息 */
async function fireReminder(r) {
  const c = Store.getContact(r.contactId);
  if (!c) { Store.reminders.update(r.id, { done: true }); return; }
  const conv = Store.ensureConversation(c.id);
  const settings = Store.getSettings();
  const brain = brainConfig(c, settings);
  const nowDate = new Date();
  const period = nowDate.getHours() < 5 ? '深夜' : nowDate.getHours() < 8 ? '清晨' : nowDate.getHours() < 12 ? '上午' : nowDate.getHours() < 14 ? '中午' : nowDate.getHours() < 18 ? '下午' : '晚上';
  const messages = [
    { role: 'system', content: composeSystem(c) + '\n\n现在是' + period + '，正是你之前答应用户的时间。用户当时的要求是：「' + r.task + '」。现在按这个要求主动给用户发一条消息，像真人一样自然，1~3 句话，回应用户当时的要求，不要用「作为」「这边」这类话。' },
    { role: 'user', content: '（定时时间到了，你主动发消息）' },
  ];
  let content = '';
  try {
    await streamAI({
      model: brain.model, baseUrl: brain.baseUrl, messages,
      key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
      offlineEnabled: false,
      proactiveInternal: true,
      skipUserPersist: true,
      internalUserPrompt: true,
    }, (d) => { content += d; });
  } catch (_) { return; }
  content = content.trim();
  if (!content) { Store.reminders.update(r.id, { done: true }); return; }

  const openHere = Chat.contact && Chat.contact.id === c.id;
  if (openHere) {
    const body = $('#chat-body');
    const typingEl = Chat.typingRow();
    body.appendChild(typingEl);
    Chat.setTyping(true);
    Chat.scrollBottom();
    await sleep(900 + Math.random() * 1200);
    typingEl.remove();
    Chat.setTyping(false);
  }

  const m = { id: Store.uid(), role: 'assistant', content, ts: Date.now(), status: 'done' };
  Store.addMessage(conv.id, m);
  if (openHere) {
    $('#chat-body').appendChild(Chat.renderMsg(m));
    Chat.scrollBottom();
  } else {
    conv.unread = (conv.unread || 0) + 1;
    inAppNotify(c, content);
  }
  Store.touchConversation(conv.id, content);
  Store.reminders.update(r.id, { done: true });
  renderChatList();
}

/** 启动定时任务调度 */
function startReminders() {
  setTimeout(() => { Reminders.tick().catch(() => {}); }, 10 * 1000);
  setInterval(() => { Reminders.tick().catch(() => {}); }, 15 * 1000);
}
