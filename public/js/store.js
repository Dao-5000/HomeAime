'use strict';
/**
 * 数据层 v9：所有用户数据归属用户本人，保存在手机浏览器本地（localStorage）
 *  - 多层记忆：瞬时对话(conversations/messages) / 每日归档(contact.logs) / 核心人格(contact.memory + contact.memStore + settings.userProfile)
 *  - 记忆存储文件夹 contact.memStore: [{id,title,content,source:'manual'|'import'|'file'|'chat',date,enabled}]
 *  - 生活模块：todos / handbook / decisions / habits / capsules / notes / reminders（定时任务）
 *  - 支持一键导出 / 导入 JSON 备份
 */
const Store = (() => {
  const KEY = 'aiwechat:v9';
  let data = null;

  const CONTACT_DEFAULTS = {
    relation: '',
    relationLevel: 5,
    callsYou: '',
    selfRef: '我',
    language: '简体中文',
    traits: '',
    hobbies: '',
    background: '',
    wallpaper: '',
    aiProvider: 'deepseek',
    aiKey: '',
    aiBase: '',
    proactiveMaxMin: 120,
    // ★ 2026-09-14：nextProactive（前端自己排的下次主动时间）已删除。
    //   主动节奏**只由后端把关**（last_proactive_push + 全局"主动发言间隔"）。
    //   两层时钟叠加会让实际间隔比设定更长，且互相"等对方"。
    //   proactiveFailUntil 只用于**失败退避**（生成失败/被后端拒绝），不是排期。
    proactiveFailUntil: 0,
    quietStart: '',
    quietEnd: '',
    replySpeed: 'normal',
    replyDelay: 'normal',     // 回复延迟档位：instant/short/normal/long/custom
    replyDelayMin: 2,         // 自定义延迟最小秒数
    replyDelayMax: 8,         // 自定义延迟最大秒数
    offlineEnabled: false,    // 离线状态模拟开关（角色独立时间线：睡觉/忙时延迟回复）
    offlineMaxDelayMin: 30,   // 最长回复延迟（分钟，1~720）
    splitMsg: true,           // 分段发送：长回复按句拆成多条消息（每条完整）
    replySegMin: 1,           // 分段发送时，最少段数
    replySegMax: 6,           // 分段发送时，最多段数（范围内随机）
    lastSummarizeAt: 0,       // 上次 AI 自动总结记忆的时间戳
    roundsSinceExtract: 0,   // 距离上次抽取记忆的对话轮数（≥4 时强制触发一次）
    proactiveFmtHistory: [],  // 主动消息格式使用历史 [{id, cat, label, ts}]，防短期重复
    replyLen: 'normal',
    typo: false,
    emojiFreq: 'normal',
    kaomoji: false,
    catchphrase: '',
    memoryLib: '',
    memoryLibName: '',
    memStore: [],         // 记忆存储文件夹
    customStyle: '',
    sticker: false,
    actions: true,        // 括号动作描写（(低头玩手指) 之类）
    logs: [],                 // 每日归档记忆 [{date:'YYYY-MM-DD', text, ts}]
    summarizedUpTo: 0,        // 对话历史压缩进度（旧消息自动压缩成记忆）
    intimacy: null,           // 亲密度 0-100（null=按关系程度初始）
    affection: 50,            // 好感度 0-100；满值时允许更热情的 7~8 段回复
    avatar: '',
  };

  function defaults() {
    return {
      version: 9,
      contacts: [],
      conversations: [],
      messages: {},
      reminders: [],   // 定时任务 [{id, contactId, at, task, done}]
      todos: [],        // {id, text, due:'YYYY-MM-DD'|'', done, createdAt}
      handbook: [],     // {id, date, category, title, content, contactName, createdAt}
      decisions: [],    // {id, date, title, context, choice, result}
      habits: [],       // {id, name, dates:['YYYY-MM-DD'], createdAt}
      capsules: [],     // {id, date, openDate, text, opened}
      notes: [],        // {id, date, title, category, content}
      letters: [],      // {id, date, type, title, content, read, audio, voiceLetter, kept}
      settings: {
        model: 'deepseek-chat',
        apiKey: '',
        mode: 'proxy',
        proxyUrl: '',
        proactive: true,
        dailyLog: true,
        replyInstant: false,   // 秒回模式：关闭真人打字动画，回复立即整段显示
        guideSeen: false,
        visionKey: '',
        visionModel: 'qwen-vl-max',
        visionProvider: 'dashscope',
        morningReport: null,  // {date, text}
        blindBox: null,       // {date, task, done, opened}
        userProfile: '',      // 用户核心档案（AI 自我学习产出）
        profileUpdated: '',   // 档案最近更新时间
      },
    };
  }

  function migrateContact(c) {
    if (!c || typeof c !== 'object') return;   // 防脏数据：非对象条目直接跳过，避免 load 白屏
    for (const [k, v] of Object.entries(CONTACT_DEFAULTS)) {
      if (c[k] === undefined) c[k] = v;
    }
    if (c.avatarUrl === undefined) c.avatarUrl = '';
    if (c.style === undefined) c.style = '';
    if (c.memory === undefined) c.memory = '';
    if (c.builtin === undefined) c.builtin = false;
    if (c.proactiveLevel !== undefined && c.proactiveMaxMin === undefined) {
      const m = { 0: 0, 1: 600, 2: 480, 3: 360, 4: 240, 5: 120, 6: 90, 7: 60, 8: 45, 9: 30, 10: 20 };
      c.proactiveMaxMin = m[c.proactiveLevel] !== undefined ? m[c.proactiveLevel] : 120;
    }
    delete c.proactiveLevel;
    delete c.proactiveRate;
    // v7 的 memoryLib 文件 → v8 记忆文件夹（一条 file 记录）
    // 条件改为：memStore 里没有同 source('file') 的记录才导入，避免 memStore 已有内容时丢文件
    if (c.memoryLib && Array.isArray(c.memStore) && !c.memStore.some((m) => m && m.source === 'file')) {
      c.memStore.push({
        id: uid(), title: c.memoryLibName || '导入的记忆', content: c.memoryLib,
        source: 'file', date: '', enabled: true,
      });
    }
    delete c.memoryLib;
    delete c.memoryLibName;
  }

  /* ---------- 内部消息：识别 / 隐藏（**永不删除**） ----------
   * ★ 2026-09-16 缺陷修复。旧实现是：
   *     normalize() → purgeInternalLeakMessages() 用 filter 重建数组 → load() 末尾 save() 写回
   *   等于**每次启动、无开关、无日志、无备份地永久删除**本地消息（判据还只是正文关键词启发式）。
   *   另一个静默站点是 addMessage() 命中判据就 `return msg`，用户按发送后消息当场消失、零提示。
   * 现在的不变量（控制器裁决 ①②③⑤）：
   *   ① 加载**只打标 + 读时隐藏**，绝不删除；
   *   ② 写入路径**永不丢弃**用户文本：入库 + 可见 console.warn + 计数；
   *   ③ 关键词启发式退化为"只认旧版脏数据、只用于隐藏"；新数据用结构化标记 internal:true
   *      （判据单一来源见 public/js/internal-msg-core.js）；
   *   ⑤ 本文件不提供任何自动删除；删除只能由用户显式操作（清空会话 / 删除会话 / 清空全部聊天）。
   */
  function internalMsgCore() {
    if (typeof window !== 'undefined' && window.InternalMsgCore) return window.InternalMsgCore;
    if (typeof globalThis !== 'undefined' && globalThis.InternalMsgCore) return globalThis.InternalMsgCore;
    return null;
  }

  /** 是否应隐藏。核心模块缺失时**宁可显示也不误藏**（fail-open，绝不凭猜测删/藏数据）。 */
  function isInternalMessage(m) {
    const core = internalMsgCore();
    if (!core) return false;
    return core.isInternalMessage(m);
  }

  const _internalStats = { hiddenOnLoad: 0, flaggedOnLoad: 0, suspectedOnSend: 0, markedOnSend: 0 };

  /** 自查账本：用户/开发者可据此知道"到底有几条被隐藏了、有没有被丢过"。 */
  function internalMsgReport() {
    return {
      hiddenOnLoad: _internalStats.hiddenOnLoad,
      flaggedOnLoad: _internalStats.flaggedOnLoad,
      suspectedOnSend: _internalStats.suspectedOnSend,
      markedOnSend: _internalStats.markedOnSend,
      // 说明：没有任何"已删除"计数，因为不存在删除路径
    };
  }

  /**
   * 旧版脏数据识别：**只打标，不删除**（旧版这里是 filter 掉再写回 = 永久删除）。
   * 命中旧关键词的 user 气泡会被 isInternalMessage 在读时隐藏，但正文与原始数组原样保留，
   * 导出备份里仍能看到；真要清理必须由用户显式删会话。
   * 返回本次新打标的条数。
   */
  function flagLegacyInternalMessages() {
    if (!data || !data.messages || typeof data.messages !== 'object') return 0;
    const core = internalMsgCore();
    if (!core) return 0;
    let flagged = 0;
    let hidden = 0;
    for (const key of Object.keys(data.messages)) {
      const arr = Array.isArray(data.messages[key]) ? data.messages[key] : [];
      for (const m of arr) {
        if (!m || typeof m !== 'object') continue;
        if (m.internal === true) { hidden++; continue; }
        if (!core.isLegacyLeakMessage(m)) continue;
        m.internal = true;
        m.internalSource = 'legacy-keyword';   // 可追溯：这条是旧版脏数据被启发式认出来的
        m.internalFlaggedAt = Date.now();
        flagged++;
        hidden++;
      }
    }
    _internalStats.flaggedOnLoad += flagged;
    _internalStats.hiddenOnLoad = hidden;
    if (flagged) {
      // 可见日志（旧实现在这里是零输出，用户永远不知道自己的消息被处理过）
      console.warn('[Store] 发现 ' + flagged + ' 条旧版"内部摘要"形状的本地消息：已打标记并在界面隐藏，'
        + '**没有删除**（仍保留在本地存储与导出备份里）。可在设置里导出备份查看原文。');
    }
    return flagged;
  }

  function normalizeContactName(value) {
    return String(value || '').trim().toLocaleLowerCase();
  }

  // A character name is the backend identity key. Older builds sometimes
  // created one conversation with the contact UUID and another with its name,
  // and a fast double click could also insert the same character twice. Merge
  // those records without dropping either side's messages.
  function mergeDuplicateContactsAndConversations() {
    if (!data || !Array.isArray(data.contacts)) return;
    if (!Array.isArray(data.conversations)) data.conversations = [];
    if (!data.messages || typeof data.messages !== 'object' || Array.isArray(data.messages)) data.messages = {};

    const canonicalByName = new Map();
    const idRedirect = new Map();
    const contacts = [];
    for (const contact of data.contacts) {
      if (!contact || typeof contact !== 'object') continue;
      const key = normalizeContactName(contact.name);
      const existing = key ? canonicalByName.get(key) : null;
      if (!existing) {
        contacts.push(contact);
        if (key) canonicalByName.set(key, contact);
        if (contact.id) idRedirect.set(String(contact.id), contact.id);
        continue;
      }
      // Keep the first stable id, but fill fields that are missing there.
      for (const [field, value] of Object.entries(contact)) {
        if ((existing[field] === undefined || existing[field] === null || existing[field] === '') && value !== undefined) {
          existing[field] = value;
        }
      }
      if (contact.id) idRedirect.set(String(contact.id), existing.id);
    }
    data.contacts = contacts;

    for (const contact of contacts) {
      if (contact.id) idRedirect.set(String(contact.id), contact.id);
      if (contact.name) idRedirect.set(String(contact.name), contact.id);
    }

    const grouped = new Map();
    const orphans = [];
    for (const cv of data.conversations) {
      if (!cv || typeof cv !== 'object') continue;
      const canonicalId = idRedirect.get(String(cv.contactId || ''));
      if (!canonicalId) {
        orphans.push(cv);
        continue;
      }
      cv.contactId = canonicalId;
      const group = grouped.get(canonicalId) || [];
      group.push(cv);
      grouped.set(canonicalId, group);
    }

    const merged = orphans.slice();
    for (const group of grouped.values()) {
      group.sort((a, b) => Number(b.updatedAt || 0) - Number(a.updatedAt || 0));
      const winner = group[0];
      const allMessages = [];
      const seenMessageIds = new Set();
      let unread = 0;
      for (const cv of group) {
        unread += Number(cv.unread || 0);
        for (const msg of (data.messages[cv.id] || [])) {
          const msgKey = msg && msg.id ? String(msg.id) : JSON.stringify([msg && msg.ts, msg && msg.role, msg && msg.content]);
          if (seenMessageIds.has(msgKey)) continue;
          seenMessageIds.add(msgKey);
          allMessages.push(msg);
        }
        if (cv !== winner) delete data.messages[cv.id];
      }
      allMessages.sort((a, b) => Number((a && a.ts) || 0) - Number((b && b.ts) || 0));
      data.messages[winner.id] = allMessages;
      winner.unread = Math.min(999, unread);
      winner.updatedAt = Math.max(...group.map((cv) => Number(cv.updatedAt || 0)), Number(winner.createdAt || 0));
      merged.push(winner);
    }
    data.conversations = merged;
  }

  /** 数据规范化：新数据用默认结构；旧版本迁移到当前版本（导入备份时也会调用） */
  function normalize() {
    if (!data || !data.version) {
      data = defaults();
      return;
    }
    if (data.version < 9) {
      data.version = 9;
      data.contacts = data.contacts || [];
      for (const c of data.contacts) migrateContact(c);
      const s = data.settings || {};
      s.proactive = s.proactive !== undefined ? s.proactive : true;
      s.dailyLog = s.dailyLog !== undefined ? s.dailyLog : true;
      s.replyInstant = !!s.replyInstant;
      s.guideSeen = !!s.guideSeen;
      s.visionKey = s.visionKey || '';
      s.visionModel = s.visionModel || 'qwen-vl-max';
      s.visionProvider = s.visionProvider || 'dashscope';
      s.morningReport = s.morningReport || null;
      s.blindBox = s.blindBox || null;
      s.userProfile = s.userProfile || '';
      s.profileUpdated = s.profileUpdated || '';
      data.settings = s;
      for (const k of ['reminders', 'todos', 'handbook', 'decisions', 'habits', 'capsules', 'notes']) {
        if (!Array.isArray(data[k])) data[k] = [];
      }
    }
    // 顶层字段兜底（无条件，不依赖版本号）：用 defaults() 补齐所有缺失键（含 letters）
    const top = defaults();
    for (const k of Object.keys(top)) {
      if (data[k] === undefined) data[k] = top[k];
    }
    // 新字段兜底（不依赖版本号）
    if (Array.isArray(data.contacts)) {
      for (const c of data.contacts) migrateContact(c);
    }
    mergeDuplicateContactsAndConversations();
    // ★ 2026-09-16：这里以前是 purgeInternalLeakMessages()（加载即删除 + 立刻写回）。
    //   现在只打标、只隐藏，**不删除任何一行**。
    flagLegacyInternalMessages();
  }

  function load() {
    if (data) return data;
    try {
      const raw = localStorage.getItem(KEY);
      data = raw ? JSON.parse(raw) : null;
    } catch (_) { data = null; }
    normalize();
    save();
    return data;
  }

  let saveWarned = false;
  let _lastSaveOk = true;

  /** 上一次 save() 是否真的落盘了。save() 不抛异常，调用方只能靠它判断。 */
  function lastSaveOk() { return _lastSaveOk; }

  /**
   * 丢弃内存缓存、从 localStorage 重新载入。
   * 供外部直写 localStorage 后同步内存——否则下次 save() 会用旧内存覆盖回去。
   */
  function reload() { data = null; return load(); }

  /**
   * ★ 返回是否保存成功。
   * 原来失败时只 console.warn、不抛也不返回，调用方（记忆抽取等）完全感知不到
   * "其实没存进去"，兜底逻辑也就永远执行不到。
   */
  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(load()));
      saveWarned = false;
      _lastSaveOk = true;
      return true;
    } catch (e) {
      _lastSaveOk = false;
      // QuotaExceededError 等：提示一次，避免每次静默失败
      console.warn('[Store] 保存失败（' + ((e && e.name) || e) + '）:', e);
      if (!saveWarned) {
        saveWarned = true;
        try {
          if (typeof window !== 'undefined' && typeof window.alert === 'function') {
            window.alert('本地存储空间不足，数据可能无法保存。建议立即导出备份，并清理浏览器缓存。');
          }
        } catch (_) {}
      }
      return false;
    }
  }

  function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 9); }

  /* ---------- 备份 ---------- */
  const exportData = () => JSON.parse(JSON.stringify(load()));
  const importData = (obj) => {
    if (!obj || typeof obj !== 'object' || !obj.version) throw new Error('不是有效的备份文件');
    // 结构校验：contacts 必须是数组，其余集合兜底成合法结构，避免脏备份污染运行数据
    if (!Array.isArray(obj.contacts)) throw new Error('备份文件无效：contacts 必须是数组');
    if (obj.settings === null || typeof obj.settings !== 'object') obj.settings = {};
    if (!Array.isArray(obj.conversations)) obj.conversations = [];
    if (!obj.messages || typeof obj.messages !== 'object' || Array.isArray(obj.messages)) obj.messages = {};
    data = obj;
    normalize();   // 兼容旧版本备份：自动迁移到当前结构
    save();
    return true;
  };

  /* ---------- 通用集合 ---------- */
  const list = (k) => (load()[k] || []);
  const add = (k, obj) => { if (!Array.isArray(load()[k])) load()[k] = []; load()[k].push(obj); save(); return obj; };
  const update = (k, id, patch) => {
    const it = list(k).find((x) => x.id === id);
    if (it) Object.assign(it, patch);
    save();
    return it;
  };
  const remove = (k, id) => { load()[k] = list(k).filter((x) => x.id !== id); save(); };

  /* ---------- 联系人 ---------- */
  const listContacts = () => load().contacts.slice();
  // ★ 支持按 id(UUID) 或名字查找：后端主动消息/来电/通话后消息有时只带角色名
  const getContact = (id) => {
    if (!id) return null;
    const cs = load().contacts;
    return cs.find((c) => c.id === id)
      || cs.find((c) => c.name === id)
      || null;
  };
  const addContact = (c) => {
    const d = load();
    const key = normalizeContactName(c && c.name);
    const existing = key ? d.contacts.find((item) => normalizeContactName(item && item.name) === key) : null;
    if (existing) return existing;
    d.contacts.push(c);
    save();
    return c;
  };
  const updateContact = (id, patch) => {
    const c = getContact(id);
    if (c) Object.assign(c, patch);
    save();
    return c;
  };
  const deleteContact = (id) => {
    const d = load();
    d.contacts = d.contacts.filter((c) => c.id !== id);
    const convs = d.conversations.filter((c) => c.contactId === id);
    for (const cv of convs) delete d.messages[cv.id];
    d.conversations = d.conversations.filter((c) => c.contactId !== id);
    save();
  };

  /* ---------- 会话 ---------- */
  const getConversation = (contactId) => {
    const d = load();
    const contact = getContact(contactId);
    const canonicalId = contact ? contact.id : contactId;
    return d.conversations.find((c) => c.contactId === canonicalId || c.contactId === contactId) || null;
  };
  const ensureConversation = (contactId) => {
    const contact = getContact(contactId);
    const canonicalId = contact ? contact.id : contactId;
    let cv = getConversation(canonicalId);
    if (!cv) {
      cv = { id: uid(), contactId: canonicalId, createdAt: Date.now(), updatedAt: Date.now(), unread: 0 };
      load().conversations.push(cv);
      save();
    } else if (cv.contactId !== canonicalId) {
      cv.contactId = canonicalId;
      save();
    }
    return cv;
  };
  const listConversations = () =>
    load().conversations
      .map((cv) => ({ cv, contact: getContact(cv.contactId) }))
      .filter((x) => x.contact)
      .sort((a, b) => b.cv.updatedAt - a.cv.updatedAt);
  const touchConversation = (cvId, lastMsg) => {
    const cv = load().conversations.find((c) => c.id === cvId);
    if (cv) {
      cv.updatedAt = Date.now();
      if (lastMsg !== undefined) cv.lastMsg = lastMsg;
      save();
    }
  };
  const markRead = (cvId) => {
    const cv = load().conversations.find((c) => c.id === cvId);
    if (cv && cv.unread) { cv.unread = 0; save(); }
  };
  const deleteConversation = (cvId) => {
    const d = load();
    d.conversations = d.conversations.filter((c) => c.id !== cvId);
    delete d.messages[cvId];
    save();
  };
  const clearConversationMessages = (cvId) => { load().messages[cvId] = []; save(); };

  /* ---------- 消息 ---------- */
  // 读时隐藏（不删除）：隐藏判据 = 结构化标记 internal:true 或旧关键词启发式
  const getMessages = (cvId) => (load().messages[cvId] || [])
    .filter((m) => !isInternalMessage(m))
    .slice().sort((a, b) => a.ts - b.ts);
  /**
   * ★ 2026-09-16 缺陷修复：旧实现是
   *     if (isInternalLeakMessage(msg)) return msg;   // 命中就在写入这一刻静默丢弃
   *   —— 用户按下发送，消息在本地凭空消失：不落盘、不渲染、不报错、不留痕（chat.js:889 是触发点）。
   * 现在**一律入库**，只对内部形状的消息标记 + 隐藏 + 可见报警 + 计数：
   *   - 显式 internal:true（内部流水线自产）：保持标记 → 读时隐藏；
   *   - 仅被旧关键词启发式命中的（例如用户自己粘贴的多行排查记录）：入库且**保持可见**
   *     （用户刚发的话不能在界面消失），只打"疑似"标记 + warn，供事后排查。
   */
  const addMessage = (cvId, msg) => {
    if (!msg || typeof msg !== 'object') return msg;
    const core = internalMsgCore();
    if (core && core.isExplicitlyInternal(msg)) {
      if (msg.internal !== true) msg.internal = true;
      _internalStats.markedOnSend += 1;
    } else if (core && core.isLegacyLeakMessage(msg)) {
      if (msg.internalSuspected !== true) {
        msg.internalSuspected = true;
        msg.internalSuspectedSource = 'legacy-keyword';
        _internalStats.suspectedOnSend += 1;
        console.warn('[Store] 这条消息的正文命中了"内部摘要"形状（旧版脏数据判据）：**已原样保存并保持可见**，未丢弃。'
          + ' 若确认它是应用自产内容，请由产出它的流水线打 internal:true 标记。正文开头：'
          + JSON.stringify(String(msg.content || '').slice(0, 40)));
      }
    }
    const d = load();
    (d.messages[cvId] = d.messages[cvId] || []).push(msg);
    save();
    return msg;
  };
  const updateMessage = (cvId, id, patch) => {
    const m = getMessages(cvId).find((x) => x.id === id);
    if (m) Object.assign(m, patch);
    save();
  };

  /* ---------- 设置 ---------- */
  const getSettings = () => load().settings;
  // ★ 返回 save() 的结果，让调用方能判断"到底存进去没有"
  const saveSettings = (patch) => { Object.assign(load().settings, patch); return save(); };

  /* ---------- 数据管理 ---------- */
  const clearAllChats = () => { const d = load(); d.conversations = []; d.messages = {}; save(); };
  const resetAll = () => { localStorage.removeItem(KEY); data = null; load(); };

  return {
    load, save, uid, exportData, importData, reload, lastSaveOk,
    listContacts, getContact, addContact, updateContact, deleteContact,
    ensureConversation, getConversation, listConversations, touchConversation, markRead,
    deleteConversation, clearConversationMessages,
    getMessages, addMessage, updateMessage,
    internalMsgReport,
    getSettings, saveSettings,
    clearAllChats, resetAll,
    reminders: { list: () => list('reminders'), add: (o) => add('reminders', o), update: (id, p) => update('reminders', id, p), remove: (id) => remove('reminders', id) },
    todos: { list: () => list('todos'), add: (o) => add('todos', o), update: (id, p) => update('todos', id, p), remove: (id) => remove('todos', id) },
    handbook: { list: () => list('handbook'), add: (o) => add('handbook', o), remove: (id) => remove('handbook', id) },
    decisions: { list: () => list('decisions'), add: (o) => add('decisions', o), remove: (id) => remove('decisions', id) },
    habits: { list: () => list('habits'), add: (o) => add('habits', o), update: (id, p) => update('habits', id, p), remove: (id) => remove('habits', id) },
    capsules: { list: () => list('capsules'), add: (o) => add('capsules', o), update: (id, p) => update('capsules', id, p), remove: (id) => remove('capsules', id) },
    notes: { list: () => list('notes'), add: (o) => add('notes', o), update: (id, p) => update('notes', id, p), remove: (id) => remove('notes', id) },
    letters: { list: () => list('letters'), add: (o) => add('letters', o), update: (id, p) => update('letters', id, p), remove: (id) => remove('letters', id), unread: () => list('letters').filter(l => !l.read && (!l.availableAt || new Date(l.availableAt).getTime() <= Date.now())).length },
  };
})();

// 顶层 const 不会成为 window 的属性，显式挂出去，
// 否则其它脚本里的 `window.Store && ...` 恒为假，相关分支静默不执行。
window.Store = Store;
