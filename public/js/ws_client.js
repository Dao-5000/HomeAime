'use strict';
/* ============================================================
   ws_client.js（新增文件，不属于原项目代码）
   WebSocket 通信层：
   - 连接本地 FastAPI 后端 /ws，断线自动重连
   - 上报当前伴侣（含亲密度），供后端做亲密度联动 / 主动发言
   - 接收后端推送：闲置主动消息（渲染成聊天气泡，保留打字机效果）、配置更新
   原有功能零影响：只在原有对象上做运行时包装，不改原文件。
   ============================================================ */

/* 简单分段函数：确保长文本一定能拆成多条气泡 */
function _simpleSplit(text, minSeg, maxSeg) {
  if (!text || text.length < 15) return [text];
  // 按换行符优先切
  let parts = text.split(/\n+/).map(s => s.trim()).filter(Boolean);
  // 按句号问号切
  if (parts.length <= 1) {
    const re = /[^。！？!?]+[。！？!?]?/g;
    parts = (text.match(re) || [text]).map(s => s.trim()).filter(Boolean);
  }
  // 按逗号切
  if (parts.length <= 1 && text.length > 30) {
    const re2 = /[^，,；;]+[，,；;]?/g;
    parts = (text.match(re2) || [text]).map(s => s.trim()).filter(Boolean);
  }
  // 强制按长度切
  if (parts.length <= 1 && text.length > 50) {
    const mid = Math.floor(text.length / 2);
    let cut = mid;
    for (let i = mid; i < text.length && i < mid + 15; i++) {
      if ('，, 。'.includes(text[i])) { cut = i + 1; break; }
    }
    parts = [text.slice(0, cut).trim(), text.slice(cut).trim()].filter(Boolean);
  }
  if (parts.length <= 1) return [text];
  // 目标段数
  const lo = Math.max(1, Math.round(Number(minSeg) || 1));
  const hi = Math.max(lo, Math.round(Number(maxSeg) || lo));
  const target = lo + Math.floor(Math.random() * (hi - lo + 1));
  const n = Math.max(1, Math.min(target, parts.length));
  if (n <= 1) return [text];
  // 平均分配
  const segs = [];
  const per = Math.floor(parts.length / n);
  let extra = parts.length % n;
  let idx = 0;
  for (let i = 0; i < n; i++) {
    let cnt = per + (extra > 0 ? 1 : 0);
    if (extra > 0) extra--;
    segs.push(parts.slice(idx, idx + cnt).join(''));
    idx += cnt;
  }
  return segs.filter(s => s.trim());
}

/* ★ toast 防抖：同一联系人 10 秒内多条主动消息只弹一次应用内通知，
   避免连续推送时通知轮番轰炸（用户还没进聊天页就先被弹窗刷屏） */
let _lastToastTs = 0;
let _lastToastContactId = null;

const WsClient = {
  ws: null,
  connected: false,
  retry: 0,
  timer: null,

  url() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/ws';
  },

  connect() {
    if (this.ws) { try { this.ws.close(); } catch (_) {} }
    let ws;
    try { ws = new WebSocket(this.url()); } catch (_) { return this.scheduleReconnect(); }
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true;
      this.retry = 0;
      // ★ 多session v3.0：session_id 来源优先级
      //   localStorage 持久化 > 新生成并持久化（Chat.contact.id 是角色id，不能当 session 标识）
      let bindSid = null;
      try {
        bindSid = localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id');
        if (!bindSid) {
          bindSid = 'session_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
          localStorage.setItem('session_id', bindSid);
        }
        // 同步到全局，让 /api/chat 等接口能读到同一个 session_id
        if (typeof Chat !== 'undefined') Chat.sessionId = bindSid;
      } catch (_) {
        bindSid = 'default';
      }
      this.send({ type: 'bind', session_id: bindSid });
      this.sendHello();
    };
    ws.onclose = (event) => { console.log('[DEBUG ws closed] code=', event.code, 'reason=', event.reason); this.connected = false; this.scheduleReconnect(); };
    ws.onerror = () => { console.log('[DEBUG ws error]'); try { ws.close(); } catch (_) {} };
    ws.onmessage = (ev) => {
      console.log('[DEBUG ws raw message]', String(ev.data).slice(0,200));
      let msg = null;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (!msg || !msg.type) return;
      // ★ Agent「干活」窗口（R19/R22）：agent_* 事件走 WebSocket 这条独立通道，
      //   放在整个分发链**最前面**认领 —— 认领成功（返回 true）就 return，
      //   保证下面任何处理器都不会再渲染一次同一个事件。
      //   非 agent_* 一律返回 false，后续分支行为完全不变。
      if (window.AgentWindowIngest && window.AgentWindowIngest(msg)) return;
      // ★ bind 确认（可选日志）
      if (msg.type === 'bound') {
        console.log('[WS] session绑定成功:', msg.session_id);
        return;
      }
      // ★ 2026-09-16 用户拍板：危机弹窗/热线卡片**已删除**（原来这里收到
      //   type==='crisis_alert' 或 crisis===true 就强制弹窗）。后端那套也已整套移除
      //   （backend/crisis.py + main.py 的 P0 硬拦截 + chat_logic 的情绪/危机关注注入），
      //   所以这里不再有任何危机分支；万一收到旧字段也当普通消息忽略，不再打断对话。
      if (msg.type === 'crisis_alert' || msg.crisis === true) {
        return;
      }
      // ★ 网页端异步 TTS：web_audio 直接播放
      if (msg.type === 'web_audio' && msg.audio) {
        // 兼容仍在运行的旧后端：普通文字回复的 web_audio 不再自动播放。
        // 明确语音消息由聊天气泡承载，并在用户点击后播放。
        return;
      }
      // ★ AI 主动来电
      if (msg.type === 'incoming_call') {
        const dnd = (typeof NotifyCore !== 'undefined' && NotifyCore.isGlobalDnd)
          ? NotifyCore.isGlobalDnd(window.__pcConfig || {}) : false;
        const userRequested = msg.call_reason === 'user_requested';
        if (dnd && !msg.dnd_exempt && !userRequested) return;
        IncomingCall.show(msg);
        return;
      }
      if (msg.type === 'intimacy_state') {
        const cid = msg.character_id || (typeof Chat !== 'undefined' && Chat.contact && (Chat.contact.name || Chat.contact.id));
        const c = typeof Store !== 'undefined' && Store.getContact ? Store.getContact(cid) : null;
        if (c && Number.isFinite(Number(msg.value))) Store.updateContact(c.id, { intimacy: Math.max(0, Math.min(100, Number(msg.value))) });
        return;
      }
      if (msg.type === 'proactive') this.onProactive(msg);
      else if (msg.type === 'qq_user_msg') this.onQqUserMsg(msg);
      else if (msg.type === 'memory_update') this.onMemoryUpdate(msg);
      else if (msg.type === 'ptt_start') { try { window.PcPTT && PcPTT.start(); } catch (_) {} }
      else if (msg.type === 'ptt_stop')  { try { window.PcPTT && PcPTT.stop(); } catch (_) {} }
      else if (msg.type === 'pc_proposal') {
        // ★ 感知系统提议动作：浮层 + 语音询问，按住 F9 说「可以」确认
        try { window.PcPTT && PcPTT.showProposal(msg.text || '', msg.audio || ''); } catch (_) {}
      }
      else if (msg.type === 'config') {
        window.__pcConfig = msg.config || null;
        try { document.dispatchEvent(new CustomEvent('pc-config', { detail: msg.config })); } catch (_) {}
      }
    };
  },

  scheduleReconnect() {
    if (this.timer) clearTimeout(this.timer);
    this.retry = Math.min(this.retry + 1, 6);
    this.timer = setTimeout(() => this.connect(), Math.min(1000 * Math.pow(2, this.retry), 15000));
  },

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify(obj)); } catch (_) {}
    }
  },

  resolveContact(ref) {
    if (!ref || typeof Store === 'undefined') return null;
    try {
      const key = String(ref || '').trim().toLowerCase();
      return (Store.listContacts() || []).find((c) =>
        String(c.id || '').trim().toLowerCase() === key ||
        String(c.name || '').trim().toLowerCase() === key ||
        String(c.character_id || '').trim().toLowerCase() === key
      ) || null;
    } catch (_) {
      return null;
    }
  },

  /* 上报当前聊天伴侣：id / 名字 / 亲密度 / 人设摘要 */
  sendHello() {
    const c = (typeof Chat !== 'undefined' && Chat.contact) ? Chat.contact : null;
    let contact = null;
    if (c) {
      contact = {
        id: c.id,
        name: c.name || '',
        avatar: c.avatarUrl || c.avatar || '',
        intimacy: (c.intimacy != null ? Number(c.intimacy) : null),
        system: String(c.system || '').slice(0, 1200),
        personality: String(c.traits || '').slice(0, 600),
        proactiveMaxMin: (c.proactiveMaxMin != null ? Number(c.proactiveMaxMin) : null),
        quietStart: c.quietStart || '',
        quietEnd: c.quietEnd || '',
        character_id: c.name || c.character_id || c.id,
      };
    }
    // ★ 同步前端 API Key + 视觉 Key 给后端（通话/唱歌/记忆/画面感知的 LLM 需要后端也有 key）
    const settings  = (typeof Store !== 'undefined' && Store.getSettings) ? Store.getSettings() : {};
    const apiKey    = (settings && settings.apiKey) || '';
    const visionKey = (settings && settings.visionKey) || '';
    this.send({ type: 'hello', session_id: (typeof Chat !== 'undefined' && Chat.sessionId) || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default', contact, api_key: apiKey, vision_key: visionKey });
  },

  sendActivity() {
    const sid = (typeof Chat !== 'undefined' && Chat.sessionId) ? Chat.sessionId : (localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default');
    const c = (typeof Chat !== 'undefined' && Chat.contact) ? Chat.contact : {};
    window.__homeAimeLastUserActivityAt = Date.now();
    this.send({ type: 'activity', session_id: sid, character_id: c.name || c.character_id || c.id || 'default' });
    this.sendHello();   // 顺带同步亲密度
  },

  /* 上报用户是否正在看聊天界面（用于发朋友圈/主动消息时机判断）*/
  sendViewing(viewing) {
    const sid = (typeof Chat !== 'undefined' && Chat.sessionId)
      ? Chat.sessionId
      : (localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default');
    const cid = (typeof Chat !== 'undefined' && Chat.contact)
      ? (Chat.contact.name || Chat.contact.id)
      : '';
    this.send({ type: 'viewing', session_id: sid, character_id: cid, viewing: !!viewing });
  },

  /* 上报通话结果（接听/拒绝/超时）*/
  sendCallResult(result, contactId, characterId) {
    this.send({
      type:       'call_result',
      result:     result,   // 'accepted' | 'rejected' | 'timeout'
      contact_id: contactId,
      // ★ 真实角色名（后端冷却 key 用），没有则退回 contactId
      character_id: characterId || contactId,
      ts:         Date.now(),
    });
  },

  /* ---------- 后端主动消息：渲染成正常聊天气泡（带打字机效果） ---------- */
  onProactive(msg) {
    console.log('[DEBUG onProactive] msg received:', JSON.stringify(msg).slice(0,200));
    // ★ 主动消息携带的音频（TTS）也要播放（早前只播了 web_audio，漏了 proactive 的 audio）
    if (msg.audio && !msg.dnd_silent && !msg.silent && !msg.sleep_letter) {
      try { var _pa = new Audio(msg.audio); _pa.play().catch(() => {}); } catch (_e) {}
    }
    let contactId = msg.contact_id;
    let contact = this.resolveContact(contactId) || this.resolveContact(msg.character_id);
    if (!contact && typeof Chat !== 'undefined' && Chat.contact) contact = Chat.contact;
    if (contact) contactId = contact.id;
    if (!contactId) contactId = msg.character_id || '';
    // 自动保存信件（早晚安/惊喜/节日/睡前总结 + AI 自发写的"信"）
    const cat = msg.template_category;
    // ★ 新增：AI 自发写的"信"（无 template_category 的长文本 + 信的特征词），也自动归档
    const _spontaneousLetter = !cat && typeof msg.content === 'string'
      && msg.content.length > 150
      && /(见字如面|提笔|写下这封信|写这封信|——你的|——爱你的|亲爱的|展信|亲启|写信给你)/.test(msg.content);
    const effectiveCat = cat || (_spontaneousLetter ? 'letter' : '');
    if (effectiveCat && typeof saveLetter === 'function') {
      const typeMap = { morning: 'morning', night: 'evening', surprise: 'gift', festival: 'letter', anniversary: 'letter', letter: 'letter', evening: 'evening' };
      const titleMap = {
        morning: '早安信', night: '晚安信',
        surprise: msg.surprise_label || '小惊喜',
        festival: msg.festival_name || '节日祝福',
        anniversary: msg.festival_name || '纪念日信',
        letter: msg.letter_title || '信件',
        evening: '睡前总结信',
      };
      const letterType = typeMap[effectiveCat] || 'letter';
      // 同一事件/同一内容才去重；允许同一天既有晚安信，也有睡前总结信。
      const content = String(msg.content || '').trim();
      const eventCharacterId = msg.character_id || msg.contact_id || contactId;
      const sourceKey = msg.keep_source_key || msg.sleep_letter_date || msg.letter_key || '';
      const existing = Store.letters.list().find(l =>
        l.sessionId === (msg.session_id || '') &&
        l.characterId === eventCharacterId &&
        ((sourceKey && l.sourceKey === sourceKey) ||
         (!sourceKey && l.type === letterType && String(l.content || '').trim() === content))
      );
      const letterMeta = {
        sessionId: msg.session_id || '',
        characterId: eventCharacterId,
        sleepLetter: !!msg.sleep_letter,
        sourceKey,
        availableAt: msg.available_at || '',
        audio: msg.audio || '',
        voiceLetter: !!msg.voice_letter || !!msg.audio,
        permanent: !!msg.permanent_keep,
        kept: !!msg.permanent_keep,
        keepSourceKey: msg.keep_source_key || (sourceKey ? 'letter:' + sourceKey : ''),
      };
      if (!existing) {
        saveLetter(letterType, titleMap[effectiveCat] || '信件', content, {
          ...letterMeta,
        });
      } else if ((!existing.audio || !existing.voiceLetter) && (letterMeta.audio || letterMeta.voiceLetter)) {
        // 同一封信可能先以文字抵达、稍后才生成 TTS；补写音频字段而不重复建信。
        Store.letters.update(existing.id, letterMeta);
      }
    }
    contact = contact || Store.getContact(contactId);
    // 信件已经归档；联系人尚未加载时只跳过聊天气泡，不丢失信件。
    if (!contact) return;
    // ★ silent 消息（睡前总结信）：只存信件、不渲染气泡、不打扰
    if (msg.silent) {
      return;
    }
    const conv = Store.ensureConversation(contactId);
    const settings = Store.getSettings();
    const instant = !!settings.replyInstant;
    const body = document.getElementById('chat-body');

    let segs = [String(msg.content || '')];
    // 主动消息默认开启分段（内联分段函数，确保可用）
    const rawText = String(msg.content || '');
    // ★ QQ 来源的推送：后端已按发送顺序分段（每段一条 ws 消息），App 不再二次拆分——
    //   保证 App 气泡与 QQ 气泡 1:1、顺序一致（逗号二次切分会造成两边不一致）
    if (msg.source !== 'qq' && rawText.length >= 15) {
      try {
        const affection = Math.max(0, Math.min(100, Number(contact.affection != null ? contact.affection : 50) || 0));
        const [minSeg, maxSeg] = dynamicSegmentRange(contact, rawText,
          contact.replySegMin != null ? Number(contact.replySegMin) : 1,
          contact.replySegMax != null ? Number(contact.replySegMax) : 6);
        segs = (typeof splitIntoSegments === 'function')
          ? splitIntoSegments(rawText, minSeg, maxSeg)
          : _simpleSplit(rawText, minSeg, maxSeg);
      } catch (_) {
        segs = _simpleSplit(rawText, 1, 3);
      }
    }

    let i = 0;
    let notified = false;

    const deliverNext = () => {
      if (i >= segs.length) {
        try { Chat.setTyping(false); } catch (_) {}
        try { renderChatList(); } catch (_) {}
        return;
      }

      const seg = segs[i++];

      const m = {
        id: Store.uid(),
        role: 'assistant',
        content: seg,
        // ★ 用后端显式 ts（多段按发送序号递增），避免同一秒推送在排序后乱序
        ts: (typeof msg.ts === 'number' && msg.ts > 0) ? msg.ts + i * 1000 : Date.now(),
        status: 'done',
      };

      // 1. 永远先落 Store
      Store.addMessage(conv.id, m);

      // 2. "正在看"必须包含主窗口真的处于前台
      const page = document.getElementById('chat-page');

      const windowActuallyFocused =
        (typeof _winFocused === 'function')
          ? _winFocused()
          : (typeof document.hasFocus === 'function'
              ? document.hasFocus()
              : document.visibilityState === 'visible');

      const viewing = !!(
        typeof Chat !== 'undefined' &&
        Chat.contact &&
        Chat.contact.id === contact.id &&
        page &&
        page.classList.contains('open') &&
        windowActuallyFocused
      );

      // 3. 未查看时增加未读
      if (!viewing) {
        conv.unread = (conv.unread || 0) + 1;
      }

      // touch 放在 unread 修改之后，让 unread 一起保存
      Store.touchConversation(conv.id, seg);

      // 4. 第一段真正保存完成之后，再弹通知
      if (!notified) {
        notified = true;

        // ★ UI 打磨 v1：移动端微震动反馈（桌面端 navigator.vibrate 不存在，自动跳过）
        if (!msg.dnd_silent) {
          try {
            if (navigator.vibrate) navigator.vibrate([25, 50, 25]);
          } catch (_) {}
        }

        try {
          if (msg.dnd_silent) throw new Error('__DND_SILENT__');
          if (typeof inAppNotify === 'function') {
            // 通知仍显示完整主动消息，不只是第一段（10秒防抖，同联系人不重复弹）
            const _now = Date.now();
            if (!(_now - _lastToastTs < 10000 && _lastToastContactId === contact.id)) {
              _lastToastTs = _now;
              _lastToastContactId = contact.id;
              inAppNotify(contact, msg.content || seg);
            }
          }
        } catch (e) {
          if (!e || e.message !== '__DND_SILENT__') console.error('[onProactive] inAppNotify failed:', e);
        }
      }

      if (viewing && !Chat.streaming) {
        // 只有用户真的正在看这个窗口，才做打字机动画
        Chat.setTyping(true);

        const el = Chat.renderMsg(m);
        body.appendChild(el);
        Chat.scrollBottom();

        const bubble = el.querySelector('.bubble');

        if (instant || !bubble || !seg) {
          Chat.setTyping(false);
          setTimeout(deliverNext, 350);
        } else {
          this.typewrite(bubble, seg, contact, () => {
            Chat.setTyping(false);
            setTimeout(deliverNext, 500);
          });
        }
      } else {
        // 后台 / 最小化：
        // 不碰隐藏的 chat-body，不跑 typewrite
        try { renderChatList(); } catch (_) {}

        setTimeout(deliverNext, 450);
      }
    };

    deliverNext();
  },

  /* 「📝 更新记忆」卡片：TA 把 TA 的批评学成了持久偏好（对齐 Claude 名场面）。
     渲染成一条特殊气泡插进当前聊天（若聊天页开着），无论是否开着都弹一次轻提示。 */
  onMemoryUpdate(msg) {
    const learned = String(msg.learned || '').trim();
    if (!learned) return;
    let contactId = msg.contact_id;
    let contact = this.resolveContact(contactId) || this.resolveContact(msg.character_id);
    if (!contact && typeof Chat !== 'undefined' && Chat.contact) contact = Chat.contact;
    if (!contact) return;
    contactId = contact.id;
    const conv = Store.ensureConversation(contactId);
    const m = {
      id: Store.uid(),
      role: 'assistant',
      type: 'memory_update',
      content: '📝 更新记忆',
      learned: learned,
      ts: Date.now(),
      status: 'done',
    };
    Store.addMessage(conv.id, m);
    const body = document.getElementById('chat-body');
    const viewing = !!(
      typeof Chat !== 'undefined' && Chat.contact && Chat.contact.id === contactId &&
      body && document.getElementById('chat-page')?.classList.contains('open')
    );
    if (viewing) {
      const el = Chat.renderMsg(m);
      body.appendChild(el);
      Chat.scrollBottom();
    }
    try { renderChatList(); } catch (_) {}
    toast('📝 TA 把你的话记下来了');
  },

  /* QQ 用户消息：渲染成用户气泡（与 onProactive 同款联系人解析，但不带打字机/通知） */
  onQqUserMsg(msg) {
    let contactId = msg.contact_id;
    let contact = this.resolveContact(contactId) || this.resolveContact(msg.character_id);
    if (!contact && typeof Chat !== 'undefined' && Chat.contact) contact = Chat.contact;
    if (!contact) return;
    contactId = contact.id;
    const conv = Store.ensureConversation(contactId);
    const text = String(msg.content || '').trim();
    if (!text) return;

    const m = {
      id: Store.uid(),
      role: 'user',
      content: text,
      ts: (typeof msg.ts === 'number' && msg.ts > 0) ? msg.ts : Date.now(),
      status: 'done',
    };
    Store.addMessage(conv.id, m);

    const page = document.getElementById('chat-page');
    const windowActuallyFocused =
      (typeof _winFocused === 'function')
        ? _winFocused()
        : (typeof document.hasFocus === 'function'
            ? document.hasFocus()
            : document.visibilityState === 'visible');
    const viewing = !!(
      typeof Chat !== 'undefined' &&
      Chat.contact &&
      Chat.contact.id === contact.id &&
      page &&
      page.classList.contains('open') &&
      windowActuallyFocused
    );

    if (!viewing) {
      conv.unread = (conv.unread || 0) + 1;
      Store.touchConversation(conv.id, text);
    }

    if (viewing && !Chat.streaming) {
      const el = Chat.renderMsg(m);
      const body = document.getElementById('chat-body');
      if (body) { body.appendChild(el); Chat.scrollBottom(); }
    } else {
      try { renderChatList(); } catch (_) {}
    }
  },

  /* 打字机：与原 chat.js 的节奏一致（60ms 一拍，速度随伴侣设置） */
  typewrite(bubble, text, contact, done) {
    const speed = (contact && contact.replySpeed) || 'normal';
    const pace = ({ fast: 8, normal: 3, slow: 1 }[speed] || 3);
    const step = Math.max(1, Math.round(pace));
    let i = 0;
    bubble.textContent = '';
    const iv = setInterval(() => {
      i = Math.min(text.length, i + step);
      bubble.textContent = text.slice(0, i);
      try { Chat.scrollBottom(); } catch (_) {}
      if (i >= text.length) { clearInterval(iv); if (done) done(); }
    }, 60);
  },
};

/* ---------- 运行时钩子（不改原文件）：切换伴侣 / 发送消息时上报 ---------- */
(function () {
  // 切换伴侣 → 重新 hello
  if (typeof Chat !== 'undefined' && Chat.open) {
    const _open = Chat.open.bind(Chat);
    Chat.open = function (id) {
      const r = _open(id);
      WsClient.sendHello();
      return r;
    };
  }
  // 发送消息 → 活动上报（重置闲置计时）
  document.addEventListener('click', (e) => {
    if (e.target && e.target.id === 'chat-send') WsClient.sendActivity();
  });
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) WsClient.sendActivity();
  });

  WsClient.connect();
})();

/* ---------- 用户是否在看聊天：定期上报（状态变化才发，减少流量） ---------- */
function _isViewingChat() {
  const page = document.getElementById('chat-page');
  const chatOpen = !!(page && page.classList.contains('open'));
  const focused = (typeof _winFocused === 'function') ? _winFocused() : document.hasFocus();
  const minimized = (typeof _winMinimizedState !== 'undefined') ? _winMinimizedState : false;
  return chatOpen && focused && !minimized;
}

let _lastViewing = null;
function _reportViewing() {
  try {
    const v = _isViewingChat();
    if (v !== _lastViewing) {
      _lastViewing = v;
      WsClient.sendViewing(v);
    }
  } catch (_) {}
}
setInterval(_reportViewing, 15000);
document.addEventListener('visibilitychange', _reportViewing);
window.addEventListener('focus', _reportViewing);
window.addEventListener('blur', _reportViewing);

/* ★ 2026-09-16 危机弹窗已按用户要求整套删除（原来是个强制遮罩弹窗 + 三条热线号码）。
   要恢复：从 git 历史取回这个函数与上面那个危机分支即可。 */

// 顶层 const 不会成为 window 的属性，显式挂出去，
// 否则 pc_enhance.js 里 `window.WsClient` 的检查恒为假，主动消息配置改动发不出去。
window.WsClient = WsClient;


