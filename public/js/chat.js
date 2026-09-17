'use strict';
/* ============================================================
   聊天页：气泡对话 + AI 流式回复 + 语音输入 + 图片 + 长按操作
   ============================================================ */

/* ---------- 后台任务模型降级 ----------
 * 记忆总结 / 对话压缩 / 记忆抽取是"杂活"，旗舰/推理模型降级到同 provider 的便宜 flash，
 * 与后端 config.py 的 _BACKGROUND_DOWNGRADE 保持一致。生成回复（主聊天）不降级。
 */
const BG_MODEL_DOWNGRADE = {
  'deepseek-reasoner': 'deepseek-v4-flash',
  'deepseek-v4-pro': 'deepseek-v4-flash',
  'glm-5.3': 'glm-5.3-flash',
  'glm-5.1': 'glm-5.3-flash',
  'glm-4.7': 'glm-4-flash',
  'glm-4.5-air': 'glm-4-flash',
  // ★ Claude 中转没有便宜 Flash，杂活统一交给 glm-5.3-flash
  'claude-sonnet-5': 'glm-5.3-flash',
  'claude-sonnet-4-20250514': 'glm-5.3-flash',
  'claude-3-7-sonnet-20250219': 'glm-5.3-flash',
  'claude-3-5-sonnet-20241022': 'glm-5.3-flash',
  'claude-3-5-haiku-20241022': 'glm-5.3-flash',
  // ★ Gemini 没有便宜 Flash，杂活统一交给 glm-5.3-flash
  'gemini-3.1-pro-high': 'glm-5.3-flash',
};
function bgModel(model) {
  return (model && BG_MODEL_DOWNGRADE[model]) || model;
}

/* ---------- 客户端指令拦截器（Command Interceptor） ----------
 * 在用户消息送入 LLM 之前拦截动作指令，本地处理，避免：
 * - "给我发个表情包" → LLM 回复"我没有表情包"
 * - "帮我xx" / "给我xx" 等请求被当作普通对话
 *
 * 拦截成功返回 { intercepted: true, response: string }（调用方 early return）
 * 拦截失败返回 { intercepted: false }（正常走 LLM 流程）
 */
const CommandInterceptor = {
  /* 表情包指令正则（覆盖截图中的各类表达） */
  _stickerPatterns: [
    /(?:给我)?(?:发|来|发个|来个|来一张|发一张|扔|丢)\s*(?:个?)?(?:表情包|表情|贴纸|图片|图|sticker|表情)/i,
    /(?:有没有|有没|有没有能发的|能发).{0,5}(?:表情包|表情|贴纸|图|图片)/i,
    /(?:发个?|来个?|来张?).{0,5}(?:表情|贴纸|图|图片)/i,
    /(?:你(?:可以|能)?)?(?:给?我)?(?:发|来|丢|扔).{0,3}(?:表情包|表情|贴纸)/i,
  ],

  /* 其他动作指令（暂不拦截，仅标记；后续可扩展） */
  _actionPatterns: [
    /^(?:给我|帮我|替我|为我|请|麻烦|劳驾|拜托).{0,15}(?:发|说|告诉|提醒|找|查|搜|打开|关闭|启动|停止|播放|暂停|下载|上传|保存|删除|修改|创建|新建|发送|转发|复制|粘贴|翻译|转换|生成|画|写|做|弄|搞|整)/i,
  ],

  /**
   * 拦截入口：判断用户消息是否为可本地处理的指令
   * @param {string} text - 用户原始输入
   * @returns {{intercepted: boolean, type?: string, response?: string}}
   */
  intercept(text) {
    if (!text || !text.trim()) return { intercepted: false };
    const t = text.trim();

    // 优先级1：表情包指令 → 本地取素材渲染，不走 LLM
    for (const pat of this._stickerPatterns) {
      if (pat.test(t)) {
        return { intercepted: true, type: 'sticker', response: null };
        // response=null 表示由调用方异步处理（需调 API 取表情包列表）
      }
    }

    // 优先级2：其他动作指令 → 可选拦截（当前放行，标记类型供后续使用）
    for (const pat of this._actionPatterns) {
      if (pat.test(t)) {
        return { intercepted: false, type: 'action' };
      }
    }

    return { intercepted: false };
  },

  /**
   * 异步处理表情包指令：从后端获取随机表情包并渲染为气泡
   * @returns {Promise<{filename: string, url: string} | null>}
   */
  async fetchRandomSticker() {
    try {
      const resp = await fetch('/api/pc/sticker/list');
      if (!resp.ok) return null;
      const data = await resp.json();
      const stickers = (data && data.stickers) || [];
      if (stickers.length === 0) return null;
      // 随机取一张
      const pick = stickers[Math.floor(Math.random() * stickers.length)];
      return pick;
    } catch (e) {
      console.warn('[CommandInterceptor] 获取表情包列表失败:', e);
      return null;
    }
  },
};

function stickerMeaningText(m) {
  if (!m) return '表达情绪、用于接梗';
  return m._stickerMeaning || m._stickerDesc || m._stickerTag || m._stickerFilename || '表达情绪、用于接梗';
}

async function pickContextSticker(text, emotion, exclude) {
  try {
    const res = await fetch('/api/pc/sticker/pick', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text || '', emotion: emotion || '', exclude: exclude || '' }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data && data.sticker ? data.sticker : null;
  } catch (_) {
    return null;
  }
}

// ★ 星露谷动作标记：AI 回复里写 [sd:浇水] 等，剥出文本并调后端真执行
//   （游戏在线时，聊天里「说要做」=「游戏里真做」，解决说≠做）
function extractGameActions(text) {
  const acts = [];
  const cleaned = String(text || '')
    .replace(/\[sd[:：]\s*([^\]]{1,12})\s*\]/g, (m, act) => {
      acts.push(String(act || '').trim());
      return '';
    })
    .replace(/[ \t]{2,}/g, ' ')
    .trim();
  return { text: cleaned, actions: acts };
}
function execGameActions(actions) {
  if (!actions || !actions.length) return;
  actions.forEach((a) => {
    fetch('/api/stardew/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: a }),
    }).catch(() => {});
  });
}

const Chat = {
  contact: null,
  conv: null,
  streaming: false,
  pendingImage: null,
  pendingSends: [],
  _drainingQueue: false,

  _isAvatarChangeIntent(text) {
    const t = String(text || '').replace(/\s+/g, '');
    if (!t) return false;
    const avatarWord = /头像|头图|头像照片/.test(t);
    const changeWord = /换成|换上|设成|设置成|用作|拿来当|当成|作为|换个|换一下|改成/.test(t);
    const selfTarget = /你(?:的|自己)?|给你|帮你|替你|TA|ta/.test(t);
    const userTarget = /我的头像|给我换头像|把我头像/.test(t);
    return avatarWord && changeWord && selfTarget && !userTarget;
  },

  async _applyAvatarFromChat(image, text) {
    const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const name = this.contact?.name || this.contact?.id || 'default';
    const res = await fetch('/api/pc/character/avatar', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid, character_name: name, data: image, instruction: text || '' }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok || !data.avatar_url) throw new Error(data.error || '头像保存失败');
    Store.updateContact(this.contact.id, { avatarUrl: data.avatar_url, avatar: data.avatar_url });
    this.contact = Store.getContact(this.contact.id);
    const reply = '好呀，那我就换上这张。你亲自给我选的，我会好好留着。';
    const aiMsg = { id: Store.uid(), role: 'assistant', content: reply, ts: Date.now(), status: 'done', subtype: 'avatar_changed' };
    Store.addMessage(this.conv.id, aiMsg);
    Store.touchConversation(this.conv.id, reply);
    $('#chat-body').appendChild(this.renderMsg(aiMsg));
    this.rerender();
    renderChatList();
    if (typeof renderContacts === 'function') renderContacts();
    this.scrollBottom(true);
    toast('已换成 TA 的新头像');
  },

  open(contactId) {
    const contact = Store.getContact(contactId);
    if (!contact) return;
    this.contact = contact;
    this.conv = Store.ensureConversation(contactId);

    Store.markRead(this.conv.id);

    // markRead 只改 Store，不会自动刷新左侧聊天列表。
    // 所以这里立即刷新，让红点马上消失。
    try {
      if (typeof renderChatList === 'function') {
        renderChatList();
      }
    } catch (_) {}

    $('#chat-title').textContent = contact.name;
    this.setTyping(false);

    this.applyWallpaper();
    // ★ 先把抽屉推出来，再渲染历史消息：老会话动辄几百条，同步渲染会让人以为「点了没反应」
    $('#chat-page').classList.remove('closed');
    $('#chat-page').classList.add('open');
    this.setSendState();
    requestAnimationFrame(() => {
      try { this.rerender(); this.scrollBottom(true); }
      catch (e) { console.warn('[chat] rerender:', e); }
    });
    setTimeout(() => $('#chat-input').focus(), 300);

    // 同步后端权威关系数值，普通回复和分段回复都按真实好感度决定热情程度。
    const relationSid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const relationCid = contact.name || contact.id || 'default';
    fetch(`/api/relationship/state?session_id=${encodeURIComponent(relationSid)}&character_id=${encodeURIComponent(relationCid)}`)
      .then(r => r.ok ? r.json() : null)
      .then(state => {
        if (!state || !this.contact || this.contact.id !== contact.id) return;
        const patch = {};
        for (const key of ['intimacy', 'affection', 'trust']) {
          if (Number.isFinite(Number(state[key]))) patch[key] = Math.max(0, Math.min(100, Number(state[key])));
        }
        Store.updateContact(contact.id, patch);
        this.contact = Store.getContact(contact.id);
      }).catch(() => {});

    // 从角色持久配置回填头像，保证重启网页/EXE后仍显示“AI自己的头像”。
    fetch('/api/pc/character/get?name=' + encodeURIComponent(contact.name))
      .then((r) => r.ok ? r.json() : null)
      .then((cfg) => {
        const avatar = cfg && (cfg.avatar || cfg.avatarUrl || '');
        if (avatar && (!this.contact.avatarUrl || this.contact.avatarUrl !== avatar)) {
          Store.updateContact(this.contact.id, { avatarUrl: avatar, avatar });
          this.contact = Store.getContact(this.contact.id);
          this.rerender();
          renderChatList();
        }
      }).catch(() => {});

    // ★ 通知后端重置情绪状态（防角色串味：角色A的怒气/冷战不带入角色B）
    if (window.Session && typeof window.Session.getSessionId === 'function') {
      const _sid = window.Session.getSessionId();
      if (_sid) {
        fetch('/api/character/switch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id:   _sid,
            character_id: contact.name || contactId
          })
        }).catch(() => { /* 静默，不影响切换本身 */ });
      }
    }
    // 切换/首次打开联系人后重新上报人格，让后端能立即执行首次欢迎或上线问候。
    // 不能只依赖 WebSocket 初次连接：那时 Chat.contact 可能还是空的。
    setTimeout(() => {
      try {
        if (typeof WsClient !== 'undefined' && WsClient.connected) WsClient.sendHello();
      } catch (_) {}
    }, 0);
  },

  close() {
    this.streaming = false;
    this.pendingSends = [];
    this.pendingImage = null;
    this.contact = null;
    this.conv = null;
    $('#chat-page').classList.remove('open');
    $('#chat-page').classList.add('closed');
    this.clearWallpaper();
    this.setSendState();
  },

  /* 聊天背景壁纸（每个伴侣可单独设置） */
  applyWallpaper() {
    const page = $('#chat-page');
    const w = this.contact ? (this.contact.wallpaper || '') : '';
    if (!w) { page.style.background = ''; }
    else if (w.startsWith('#')) { page.style.background = w; }
    else { page.style.background = '#EDEDED center/cover no-repeat url("' + w + '")'; }
  },
  clearWallpaper() {
    $('#chat-page').style.background = '';
  },

  /* ---------- 渲染 ---------- */

  rerender() {
    const body = $('#chat-body');
    body.innerHTML = '';
    const msgs = Store.getMessages(this.conv.id);
    let lastTs = 0;
    for (const m of msgs) {
      if (m.ts - lastTs > 15 * 60 * 1000) body.appendChild(timeDivider(m.ts));
      lastTs = m.ts;
      // ★ 2026-09-11：思考过程持久化——历史消息带 thinking 的重画折叠框
      //   （原来只存在会话中的临时 DOM，刷新/重进对话就没了）
      if (m.thinking && this.contact && this.contact.show_thinking) {
        body.appendChild(_buildThinkingBlock(String(m.thinking), Number(m.thinkingSec) || 0));
      }
      body.appendChild(this.renderMsg(m));
    }
    if (!msgs.length) {
      body.appendChild(h('div', { class: 'empty-state' },
        avatarEl(this.contact, 'lg'),
        h('div', { style: 'margin-top:10px', text: '开始和 ' + this.contact.name + ' 聊天吧' }),
        h('div', { style: 'margin-top:6px;font-size:12px', text: '点上方名字可以细调 TA 的一切' }),
      ));
    }
    this.scrollBottom(true);
  },

  renderMsg(m) {
    // 已撤回的消息：居中灰色系统提示
    if (m.status === 'recalled') {
      return h('div', { class: 'sys-text', text: '你撤回了一条消息' });
    }

    // ★ 「📝 更新记忆」卡片：TA 把 TA 的批评学成了持久偏好（对齐 Claude 名场面）
    if (m.type === 'memory_update') {
      const wrap = h('div', { style: 'display:flex;margin:6px 0 10px;align-items:flex-start' });
      const card = h('div', {
        style: 'max-width:86%;background:var(--card,#fff);border-radius:16px;padding:10px 14px;box-shadow:0 10px 30px rgba(20,20,20,.07)'
      });
      card.appendChild(h('div', {
        style: 'display:flex;align-items:center;gap:6px;font-size:13px;font-weight:600;margin-bottom:5px',
        text: '✦ 已记入记忆'
      }));
      card.appendChild(h('div', {
        style: 'font-size:13px;color:#555;line-height:1.6;word-break:break-word',
        text: String(m.learned || '')
      }));
      wrap.appendChild(card);
      return wrap;
    }

    // ★ 未接来电：居中小标签，不占气泡
    if (m.subtype === 'missed_call') {
      const wrap = h('div', {
        class: 'msg-system',
        style: [
          'text-align:center',
          'margin:8px 0',
          'color:var(--text-3, #888)',
          'font-size:12px',
        ].join(';'),
      });
      const tag = h('span', {
        style: [
          'display:inline-flex',
          'align-items:center',
          'gap:4px',
          'background:var(--bg-2, #f2f2f7)',
          'border-radius:12px',
          'padding:4px 10px',
        ].join(';'),
        text: m.content,
      });
      wrap.appendChild(tag);
      return wrap;
    }

    // ★ 通话结束：居中小标签
    if (m.subtype === 'call_ended') {
      const wrap = h('div', {
        class: 'msg-system',
        style: 'text-align:center;margin:8px 0;color:var(--text-3,#888);font-size:12px',
      });
      wrap.appendChild(h('span', {
        style: [
          'display:inline-flex',
          'align-items:center',
          'gap:4px',
          'background:var(--bg-2,#f2f2f7)',
          'border-radius:12px',
          'padding:4px 10px',
        ].join(';'),
        text: m.content,
      }));
      return wrap;
    }

    // ★ 通话记录折叠：语音通话写进 chat_history 的内容只显示成一条居中标签，
    //   不刷屏。用户的话不显示（通话里已说过），AI 的话显示成摘要标签。
    if (m.source === 'voice_call') {
      if (m.role !== 'assistant') {
        return h('div', { style: 'display:none' });
      }
      const brief = String(m.content || '').replace(/\s+/g, ' ').trim();
      const show = brief.length > 28 ? brief.slice(0, 28) + '…' : brief;
      const wrap = h('div', {
        class: 'msg-system',
        style: 'text-align:center;margin:8px 0;color:var(--text-3,#888);font-size:12px',
      });
      wrap.appendChild(h('span', {
        style: [
          'display:inline-flex',
          'align-items:center',
          'gap:4px',
          'background:var(--bg-2,#f2f2f7)',
          'border-radius:12px',
          'padding:4px 10px',
          'max-width:80%',
          'overflow:hidden',
          'text-overflow:ellipsis',
          'white-space:nowrap',
        ].join(';'),
        text: '📞 ' + (show || '通话记录'),
        title: m.content || '',
      }));
      return wrap;
    }

    const row = h('div', { class: 'msg-row ' + (m.role === 'user' ? 'me' : 'other') });
    // ★ 用户消息显示「我的头像」，AI 消息显示角色头像
    let avatar;
    if (m.role === 'user') {
      const myAv = (Store.getSettings().myAvatar) || '';
      avatar = h('div', { class: 'avatar sm' });
      if (myAv) {
        avatar.appendChild(h('img', { src: myAv, alt: '' }));
      } else {
        avatar.style.background = 'hsl(' + avatarHue('我') + ', 55%, 58%)';
        avatar.style.color = '#fff';
        avatar.style.fontWeight = '600';
        avatar.style.fontSize = '16px';
        avatar.textContent = '我';
      }
    } else {
      avatar = avatarEl(this.contact, 'sm');
    }
    let bubble;
    if (m.type === 'voice' && m.audio) {
      const seconds = Math.max(1, Math.min(60, Number(m.duration) || Math.round(String(m.content || '').length / 4.2) || 1));
      const playBtn = h('button', {
        class: 'voice-message-btn', type: 'button', 'aria-label': '播放语音',

      });
      playBtn.innerHTML = '<span class="vbars"><i style="height:6px"></i><i style="height:11px"></i><i style="height:16px"></i><i style="height:9px"></i><i style="height:13px"></i></span><span class="voice-sec">' + seconds + '″</span>';
      let _audio = null;
      playBtn.addEventListener('click', () => {
        // ★ 点击切换：播放中→停止，停止→播放
        if (_audio && !_audio.paused) {
          try { _audio.pause(); _audio.currentTime = 0; } catch (_) {}
          _audio = null;
          playBtn.textContent = '🔊  ' + seconds + '″';
          return;
        }
        if (!_audio) {
          _audio = new Audio(m.audio);
          _audio.addEventListener('ended', () => { _audio = null; playBtn.querySelector('.voice-sec').textContent = seconds + '″'; }, { once: true });
          _audio.addEventListener('error', () => { _audio = null; playBtn.querySelector('.voice-sec').textContent = '失败'; }, { once: true });
        }
        
        _audio.play().catch(() => { playBtn.querySelector('.voice-sec').textContent = '失败'; });
      });
      playBtn.innerHTML = '<span class="vbars"><i style="height:6px"></i><i style="height:11px"></i><i style="height:16px"></i><i style="height:9px"></i><i style="height:13px"></i></span><span class="voice-sec">' + seconds + '″</span>';
      bubble = h('div', { class: 'bubble voice-message-bubble', title: m.content || '' }, playBtn);
    } else if (m.type === 'sticker') {
      // 表情包：优先渲染图片（拦截器传入 _stickerUrl），否则显示文字
      if (m._stickerUrl) {
        bubble = h('div', { class: 'bubble image-bubble sticker-bubble' },
          h('img', { src: m._stickerUrl, alt: '表情包', style: 'max-width:160px;max-height:160px;border-radius:8px;' }),
        );
      } else {
        bubble = h('div', { class: 'bubble sticker-bubble', text: m.content || '[表情包]' });
      }
    } else if (m.image) {
      // ★ UI 打磨 v1：图片点击全屏预览
      const imgEl = h('img', { src: m.image, alt: '图片' });
      imgEl.addEventListener('click', () => _showImageLightbox(m.image));
      bubble = h('div', { class: 'bubble image-bubble' }, imgEl);
      if (m.content && m.content !== '[图片]') bubble.appendChild(h('div', { class: 'img-caption', text: m.content }));
    } else {
      bubble = h('div', { class: 'bubble', text: m.content });
    }
    if (m.status === 'error') bubble.classList.add('error');
    if (m.status === 'streaming') bubble.classList.add('streaming');
    // ★ 引用消息：气泡内顶部渲染被引用的原文（像微信"回复"效果）
    if (m.reply_to && m.reply_to.content) {
      const q = h('div', { class: 'quote' });
      q.appendChild(h('span', { class: 'quote-who', text: m.reply_to.role === 'user' ? '我' : (m.reply_to.name || 'TA') }));
      q.appendChild(h('span', { class: 'quote-text', text: String(m.reply_to.content || '').slice(0, 80) }));
      bubble.insertBefore(q, bubble.firstChild);
    }
    row.appendChild(avatar);
    row.appendChild(bubble);

    this.bindMsgMenu(row, m);
    return row;
  },

  sendFeedback(messageId, action, aiReply) {
    let sid = 'default';
    if (window.Session && typeof window.Session.getSessionId === 'function') {
      try { sid = window.Session.getSessionId() || 'default'; } catch (_) {}
    }
    const cid = (this.contact && this.contact.id) || 'default';
    fetch('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sid,
        character_id: cid,
        message_id: String(messageId),
        action: action,
        ai_reply: aiReply || ''
      })
    }).catch(e => console.warn('[Feedback]', e));
  },

  typingRow() {
    const row = h('div', { class: 'typing-row' },
      avatarEl(this.contact, 'sm'),
      h('div', { class: 'typing-bubble' },
        h('span', { class: 'typing-dot' }), h('span', { class: 'typing-dot' }), h('span', { class: 'typing-dot' }),
      ),
    );
    return row;
  },

  /* 顶部状态：对方正在输入… / AI 情绪 / 在线 */
  setTyping(on) {
    this._typing = on;
    this._renderSubtitle();
  },
  setAiMood(display) {
    this._aiMood = display || null;
    this._renderSubtitle();
  },
  _renderSubtitle() {
    const sub = $('#chat-subtitle');
    sub.innerHTML = '';
    sub.appendChild(h('span', { class: 'online-dot' }));
    if (this._typing) {
      sub.appendChild(document.createTextNode(' 对方正在输入…'));
    } else {
      sub.appendChild(document.createTextNode(' 在线'));
    }
  },
  _refreshAiMood() {
    if (!this.contact) return;
    const sid = (typeof Session !== 'undefined' && Session.getSessionId)
      ? Session.getSessionId()
      : (localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default');
    const cid = this.contact.name || this.contact.id;
    fetch('/api/ai_mood/state?session_id=' + encodeURIComponent(sid) + '&character_id=' + encodeURIComponent(cid))
      .then((r) => r.json())
      .then((j) => { this.setAiMood(j.state); })
      .catch(() => {});
  },

  scrollBottom(force) {
    const body = $('#chat-body');
    if (!body) return;
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
    if (!force && !nearBottom) return;
    const roll = () => { if (body) body.scrollTop = body.scrollHeight; };
    roll();
    // ★ 图片/表情包/头像异步加载会改变高度，导致「进入聊天页不在最新处」。
    //   force 滚动时对尚未加载完的 img 挂 load 兜底，加载完再滚一次到底。
    if (force) {
      const imgs = body.querySelectorAll('img');
      for (const img of imgs) {
        if (!img.complete) {
          img.addEventListener('load', roll, { once: true });
        }
      }
    }
  },

  /* ---------- 长按操作 ---------- */

  bindMsgMenu(row, m) {
    const onLong = () => { if (!this.streaming) this.showMsgMenu(m); };
    let timer = null, moved = false;
    row.addEventListener('touchstart', () => {
      moved = false;
      timer = setTimeout(() => { if (!moved) onLong(); }, 450);
    }, { passive: true });
    row.addEventListener('touchmove', () => { moved = true; clearTimeout(timer); }, { passive: true });
    ['touchend', 'touchcancel'].forEach((ev) => row.addEventListener(ev, () => clearTimeout(timer)));
    row.addEventListener('contextmenu', (e) => { e.preventDefault(); onLong(); });
  },

  showMsgMenu(m) {
    const sheet = h('div', { class: 'action-sheet' },
      h('div', { class: 'sheet-title', text: '消息操作' }),
    );
    if (m.role === 'user') {
      sheet.appendChild(actionItem('↩️', '撤回', () => this.recall(m)));
    } else {
      if (m.content && !m.image) {
        sheet.appendChild(actionItem('📋', '复制', () => { Sheet.close(); copyText(m.content); }));
      }
      // ★ 反馈闭环：喜欢/不喜欢这条 AI 回复（从气泡旁按钮移到右键菜单）
      if (m.content && !m.image && m.status !== 'streaming' && m.status !== 'error') {
        sheet.appendChild(actionItem('👍', '喜欢这条回复', () => { Sheet.close(); this.sendFeedback(m.id, 'like', m.content || ''); toast('已标记喜欢'); }));
        sheet.appendChild(actionItem('👎', '不喜欢这条回复', () => { Sheet.close(); this.sendFeedback(m.id, 'dislike', m.content || ''); toast('已标记不喜欢'); }));
      }
    }
    // ★ 引用：像微信一样，回复时上方带被引用的原文
    if (m.content && m.type !== 'sticker') {
      sheet.appendChild(actionItem('↪️', '引用', () => {
        Sheet.close();
        this._quoteTarget = {
          role: m.role,
          content: (m.content || '[图片]'),
          name: m.role === 'assistant' ? (this.contact && this.contact.name) : '我',
        };
        this.setSendState();
        const _ta = $('#chat-input');
        if (_ta) _ta.focus();
      }));
    }
    // ★ 双向都支持：让 TA 记住单条消息（用户消息 + AI 回复）
    if (m.content && !m.image) {
      sheet.appendChild(actionItem('🧠', '让 TA 记住这句话', () => this.remember(m)));
    }
    // ★ 新增：记住「这段对话」——打包最近 1~3 对 User+Assistant 进记忆库
    sheet.appendChild(actionItem('💬', '记住这段对话', () => this.rememberSnippet(m)));
    sheet.appendChild(actionItem('❌', '取消', () => Sheet.close()));
    Sheet.open(sheet, 'action-sheet');
  },

  recall(m) {
    Sheet.close();
    Store.updateMessage(this.conv.id, m.id, { status: 'recalled', content: '你撤回了一条消息' });
    this.rerender();
    renderChatList();
    toast('已撤回');
  },

  remember(m) {
    Sheet.close();
    const who = m.role === 'user' ? '用户' : 'TA';
    const content = String(m.content || '').trim();
    if (!content) { toast('这条消息没有内容'); return; }
    // ★ 记忆统一：手动记忆只写入后端 sqlite（与自动提炼同源，记忆页可查看/编辑/删除）
    const cid = (this.contact && this.contact.name) || 'default';
    fetch('/api/pc/memory/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: (who === '用户' ? '用户说：' : 'TA说：') + content, character_name: cid })
    }).then((r) => r.json()).then((j) => {
      if (j && j.ok) toast('已记住 ✅');
      else toast((j && j.error && j.error.message) || '已记住 ✅');
    }).catch(() => toast('已记住 ✅'));
    renderChatList();
  },

  rememberSnippet(m) {
    Sheet.close();
    // 把这条消息附近的 1~3 对话打包（最多 8 条）
    const msgs = Store.getMessages(this.conv.id)
      .filter((x) => x.status !== 'error' && x.status !== 'recalled' && x.content)
      .sort((a, b) => a.ts - b.ts);
    const idx = msgs.findIndex((x) => x.id === m.id);
    if (idx < 0) return;
    const from = Math.max(0, idx - 3);
    const to = Math.min(msgs.length, idx + 4);
    const snippet = msgs.slice(from, to).map((x) => (x.role === 'user' ? '用户：' : 'TA：') + (x.content || '')).join('\n');
    const entry = {
      id: Store.uid(),
      title: (new Date().toLocaleDateString('zh-CN')) + ' 对话片段',
      content: snippet,
      source: 'chat-snippet',
      role: 'both',
      date: dateStrOf(Date.now()),
      ts: Date.now(),
      enabled: true,
    };
    const list = (this.contact.memStore || []).slice();
    list.unshift(entry);
    Store.updateContact(this.contact.id, { memStore: list });
    toast('已把这段对话存进记忆库 💾');
    renderChatList();
  },

  /* ---------- 系统指令：显示/清空 全部记忆 ---------- */

  // 用户在聊天输入"显示全部记忆"时触发：直接生成 AI 系统消息，把 userProfile + memStore 摘要打印出来
  // 不调 LLM，避免误解/节省 token；用户能核对 AI 是否真的记住了关键信息
  _showAllMemory() {
    const contact = this.contact;
    if (!contact) return;
    // ★ 改为跳转到记忆库页（不影响聊天观感）
    MemStore.open(contact.id);
  },

  // 用户在聊天输入"清空全部长期记忆库"时触发：直接清空 userProfile + memStore
  _clearAllMemory() {
    const contact = this.contact;
    if (!contact) return;
    if (!confirm('确认清空当前角色的全部长期记忆吗？\n\n包括：\n· 用户核心档案\n· 记忆文件夹（memStore）所有条目\n\n此操作不可撤销！')) return;

    const memsCount = (contact.memStore || []).length;
    Store.updateContact(contact.id, { memStore: [] });
    Store.saveSettings({ userProfile: '', profileUpdated: new Date().toISOString() });

    const text = '已清空全部长期记忆 🗑️\n\n· 清掉了 ' + memsCount + ' 条记忆文件夹条目\n· 清掉了用户核心档案\n\n下次对话会从空白重新开始。';
    const m = {
      id: Store.uid(),
      role: 'assistant',
      content: text,
      ts: Date.now(),
      status: 'done',
    };
    Store.addMessage(this.conv.id, m);
    const body = $('#chat-body');
    body.appendChild(this.renderMsg(m));
    this.scrollBottom();
    Store.touchConversation(this.conv.id, '（清空全部长期记忆）');
    toast('已清空全部记忆', 2000);
  },

  /* ---------- 输入 ---------- */

  setSendState() {
    const ta = $('#chat-input');
    const btn = $('#chat-send');
    const hasText = ta.value.trim().length > 0;
    const hasImg = !!this.pendingImage;
    // 生成回复期间仍允许输入；发送内容会按当前会话排队，避免打断正在生成的回复。
    const enabled = (hasText || hasImg);
    btn.classList.toggle('disabled', !enabled);
    btn.disabled = !enabled;

    // 图片预览条
    const pv = $('#chat-preview');
    pv.innerHTML = '';
    if (this.pendingImage) {
      pv.classList.remove('hidden');
      pv.appendChild(h('img', { src: this.pendingImage, alt: '' }));
      const x = h('button', { class: 'pv-x', text: '✕', 'aria-label': '取消图片' });
      x.addEventListener('click', () => {
        this.pendingImage = null;
        this.setSendState();
      });
      pv.appendChild(x);
    } else {
      pv.classList.add('hidden');
    }

    // ★ 引用预览条（像微信：回复时输入框上方带被引用气泡）
    const qv = $('#chat-quote');
    if (qv) {
      qv.innerHTML = '';
      if (this._quoteTarget) {
        const q = this._quoteTarget;
        qv.classList.remove('hidden');
        qv.appendChild(h('span', { class: 'quote-who', text: q.role === 'user' ? '我' : (q.name || 'TA') }));
        qv.appendChild(h('span', { class: 'quote-text', text: String(q.content || '').slice(0, 60) }));
        const x = h('button', { class: 'pv-x', text: '✕', 'aria-label': '取消引用' });
        x.addEventListener('click', () => {
          this._quoteTarget = null;
          this.setSendState();
        });
        qv.appendChild(x);
      } else {
        qv.classList.add('hidden');
      }
    }
  },

  autosize() {
    const ta = $('#chat-input');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 96) + 'px';
  },

  /* ---------- 发送与 AI 调用 ---------- */

  // ★ 判断两条用户消息是否属于"同一个意思的重复发送"（连发加重语气）。
  //   归一化后完全相同，或一方是另一方的叠加（"好好好" vs "好"）都算重复。
  _isSameIntent(a, b) {
    const norm = (s) => String(s || '')
      .replace(/[\s!！。.、,，~～\-—…'"“”‘’?？]+/g, '')
      .toLowerCase();
    const x = norm(a);
    const y = norm(b);
    if (!x || !y) return false;
    if (x === y) return true;
    // 叠加重复：如 "好好好" 与 "好"，"骨子天下第一可爱" 连发三遍
    const longer = x.length >= y.length ? x : y;
    const shorter = x.length >= y.length ? y : x;
    if (!shorter.length) return false;
    const repeated = longer.split(shorter).length - 1;
    return repeated >= 2 && longer.length <= shorter.length * 6;
  },

  async send(presetText, presetImage, _queuedItem = null) {
    const ta = $('#chat-input');
    const text = (typeof presetText === 'string' ? presetText : ta.value).trim();
    const image = presetImage !== undefined ? presetImage : this.pendingImage;
    if ((!text && !image) || !this.contact || !this.conv) return;

    // ★ 捕获本次发送要携带的"引用"目标（发送即清空，不残留到下一句）
    const quoteForThis = this._quoteTarget || null;
    if (quoteForThis) { this._quoteTarget = null; this.setSendState(); }

    // 记录真正的用户互动时间，供前端主动消息门禁使用。
    // 不能只看会话最后一条（可能是 AI 主动消息），也不能等后端落库后才判断。
    window.__homeAimeLastUserActivityAt = Date.now();

    if (this.streaming) {
      const queued = { text, image, contactId: this.contact.id, convId: this.conv.id };
      this.pendingImage = null;
      ta.value = '';
      this.autosize();
      this.setSendState();
      const queuedMsg = { id: Store.uid(), role: 'user', ts: Date.now(), status: 'sent', content: text || '[图片]', image: image || undefined, queued: true, reply_to: quoteForThis || undefined };
      queued.messageId = queuedMsg.id;

      // ★ 连发相同内容合并：用户连发多条一样的话（如"骨子天下第一可爱"×3）通常是加重
      //   语气，不该让 AI 分别回答三遍。队尾已有同一意图的内容时并入它，只发一次请求。
      //   内容不同的多条仍会分别入队，保留"挨个回复"的手感。
      const _lastQueued = this.pendingSends[this.pendingSends.length - 1];
      if (_lastQueued && !image && this._isSameIntent(_lastQueued.text, text)) {
        _lastQueued.dupCount = (_lastQueued.dupCount || 1) + 1;
        const _prev = Store.getMessages(this.conv.id).find(m => m.id === _lastQueued.messageId);
        if (_prev) {
          _prev.dupCount = _lastQueued.dupCount;
          const _node = document.querySelector('[data-mid="' + _lastQueued.messageId + '"]');
          if (_node) {
            let _badge = _node.querySelector('.dup-badge');
            if (!_badge) {
              _badge = document.createElement('span');
              _badge.className = 'dup-badge';
              _badge.style.cssText = 'font-size:11px;opacity:.55;margin-left:6px;';
              _node.appendChild(_badge);
            }
            _badge.textContent = '×' + _lastQueued.dupCount;
          }
        }
        Store.touchConversation(this.conv.id, text || '[图片]');
        this.scrollBottom();
        return;
      }

      this.pendingSends.push(queued);
      Store.addMessage(this.conv.id, queuedMsg);
      Store.touchConversation(this.conv.id, text || '[图片]');
      const body = $('#chat-body');
      if (body) body.appendChild(this.renderMsg(queuedMsg));
      this.scrollBottom();
      toast('已发送，AI回复完成后继续处理');
      return;
    }

    // 图片前置检查：直连模式不支持；未配置视觉模型 Key 时提前提醒
    const settings = Store.getSettings();
    const avatarChangeIntent = !!image && this._isAvatarChangeIntent(text);
    if (image && settings.mode === 'direct' && !avatarChangeIntent) {
      toast('图片功能需要「代理模式」，请到设置→连接方式切换');
      return;
    }
    if (image && !avatarChangeIntent && !settings.visionKey && !window.__serverVisionKey) {
      if (!confirm('还没配置图片识别 Key（设置→图片识别），AI 可能看不了这张图。仍要发送吗？')) return;
    }

    const queuedExisting = _queuedItem
      ? Store.getMessages(this.conv.id).find(m => m.id === _queuedItem.messageId)
      : null;
    const userMsg = queuedExisting || {
      id: Store.uid(),
      role: 'user',
      ts: Date.now(),
      status: 'sent',
      content: text || '[图片]',
      reply_to: quoteForThis || undefined,
    };
    // 从表情面板发送的 [sticker:文件名] 直接渲染为图片气泡，
    // 但保留原始标记送给后端，让 AI 能识别并接梗。
    const sentStickerMatch = !image && text.match(/^\s*\[sticker:([^\]]+)\]\s*$/i);
    if (sentStickerMatch) {
      userMsg.type = 'sticker';
      userMsg.content = '[表情包]';
      userMsg._stickerFilename = sentStickerMatch[1].trim();
      userMsg._stickerUrl = '/stickers/' + encodeURIComponent(userMsg._stickerFilename);
    }
    if (image) userMsg.image = image;
    this.pendingImage = null;

    if (queuedExisting) Store.updateMessage(this.conv.id, userMsg.id, { queued: false, status: 'sent' });
    else Store.addMessage(this.conv.id, userMsg);
    Store.touchConversation(this.conv.id, text || '[图片]');

    const body = $('#chat-body');
    if (!queuedExisting) body.appendChild(this.renderMsg(userMsg));
    this.scrollBottom();
    ta.value = '';
    this.autosize();

    // 图片 + 明确“换成你自己的头像”是本地确定性操作，不要求视觉模型，也不改用户头像。
    if (avatarChangeIntent) {
      try {
        await this._applyAvatarFromChat(image, text);
      } catch (e) {
        const fail = { id: Store.uid(), role:'assistant', content:'这张头像我没换成功：' + (e.message || '图片保存失败'), ts:Date.now(), status:'error' };
        Store.addMessage(this.conv.id, fail);
        body.appendChild(this.renderMsg(fail));
        toast(e.message || '头像保存失败');
      }
      this.streaming = false;
      this.setSendState();
      this._drainSendQueue();
      return;
    }

    // ★ 唱歌流程：说"唱首歌/唱歌/给我唱"进入唱歌（方案A/B）
    const _singMatch = /唱首歌|唱一首|唱首|来一首|来首|给我唱|唱给我|唱个歌|唱个|唱一下|唱两句|唱吧|唱歌|会唱|能唱|献唱|哼一首|哼一段|唱/.test(text)
      && !/别唱|不唱|不想唱|不用唱|唱什么唱/.test(text);
    if (_singMatch && !/晚安/.test(text)) {
      // 方案B：说"打电话唱/电话里唱" → 直接发起语音通话（通话里唱歌）
      if (/打电话|打过来|来电话|电话/.test(text) && window.VoiceCall && typeof VoiceCall.start === 'function') {
        VoiceCall.start({ contactId: this.contact.id, callReason: '唱歌' });
        return;
      }
      // 提取歌名
      let _songName = '';
      const _bracket = text.match(/[《〈「『]([^》〉」』]{1,20})[》〉」』]/);
      if (_bracket) _songName = _bracket[1];
      else {
        const _after = text.match(/(?:唱|来|来一首|来首|哼)\s*[《〈「『]?([\u4e00-\u9fffA-Za-z0-9]{1,20})/);
        if (_after && !/^(首|个|一)/.test(_after[1])) _songName = _after[1];
      }
      // 立即回一句自然的话（不阻塞，用户可继续发消息）
      const prepMsg = { id: Store.uid(), role: 'assistant', content: _songName ? ('好呀，这首《' + _songName + '》我唱给你听，稍等一下下～') : '好呀，我这就唱给你听，等我一下下～', ts: Date.now(), status: 'done' };
      Store.addMessage(this.conv.id, prepMsg);
      body.appendChild(this.renderMsg(prepMsg));
      this.scrollBottom(true);
      renderChatList();
      // 后台生成唱歌音频，完成后追加语音条（不阻塞聊天）
      (async () => {
        try {
          const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
          const res = await fetch('/api/sing', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              session_id: sid,
              character_id: this.contact.name || this.contact.id,
              character_name: this.contact.name || this.contact.id,
              user_text: text,
              song_name: _songName,
            }),
          });
          const result = await res.json().catch(() => ({}));
          if (result.needs_song_name) {
            const askMsg = { id: Store.uid(), role: 'assistant', content: result.reply || '想听什么歌呀？', ts: Date.now(), status: 'done' };
            Store.addMessage(this.conv.id, askMsg);
            body.appendChild(this.renderMsg(askMsg));
          } else if (result.ok && result.audio) {
            const singMsg = { id: Store.uid(), role: 'assistant', type: 'voice', content: '🎤 ' + (result.song_name || '唱给你听'), audio: result.audio, duration: result.duration || 8, ts: Date.now(), status: 'done' };
            Store.addMessage(this.conv.id, singMsg);
            body.appendChild(this.renderMsg(singMsg));
          } else {
            const errMsg = { id: Store.uid(), role: 'assistant', content: result.error || '唱歌失败，稍后再试', ts: Date.now(), status: 'error' };
            Store.addMessage(this.conv.id, errMsg);
            body.appendChild(this.renderMsg(errMsg));
          }
          this.scrollBottom(true);
        } catch (e) {
          const errMsg = { id: Store.uid(), role: 'assistant', status: 'error', content: '唱歌失败了：' + (e.message || '请稍后重试'), ts: Date.now() };
          Store.addMessage(this.conv.id, errMsg);
          body.appendChild(this.renderMsg(errMsg));
        } finally {
          renderChatList();
        }
      })();
      return;
    }

    // 用户明确索要“发一条语音”是产品动作，不交给模型口头敷衍。
    // “语音晚安”仍走后端晚安信逻辑，“语音电话”仍走来电逻辑。
    const previousMessages = Store.getMessages(this.conv.id)
      .filter((m) => m.id !== userMsg.id && m.status !== 'error' && m.status !== 'recalled');
    const lastAssistantMessage = [...previousMessages].reverse().find((m) => m.role === 'assistant');
    const repeatVoiceIntent = !!(lastAssistantMessage && lastAssistantMessage.type === 'voice')
      && /^(?:再|重新)(?:发|说|来|讲)(?:一遍|一次|一句|一条|一段)?(?:吧|呗|嘛|呀|啊)?$|^(?:再来|再发)(?:一个|一条|一段)?(?:吧|呗|嘛)?$/.test(text.replace(/\s+/g, ''));
    // ★ 2026-09-14 修：用户说「不要发语音」时，下面那条意图正则会被文本里的「发语音」
    //   三个字命中 → 误判成「用户在索要语音」→ 走 /api/voice/message 发一条语音，
    //   而该端点回复是模板硬编码（不经模型），于是用户刚说完别发语音、立刻收到语音，
    //   且内容答非所问（实测 #9821→#9822）。
    //   这里加否定守卫：只要「不要/别/不用…」在语音动作词附近，就一律不算语音意图。
    const voiceNegated = /(?:不要|不用|别再?|不许|不准|不想|不必|以后不要|别再|取消|停止)[^，。！？,!?]{0,6}(?:语音|声音|念|读|说给|发话)|(?:语音|声音)[^，。！？,!?]{0,4}(?:不要|别|不用)|(?:打字|文字)[^，。！？,!?]{0,4}(?:不要|别|不用|就行|就好)/.test(text);
    // ★ 2026-09-14 补：主语是"用户自己"时不算索要语音。如「我在语音里跟你说」
    //   「我今天语音聊了好久」——原正则只认「语音」二字，会把这些普通聊天
    //   误判成索要语音，进而走 /api/voice/message 拿到模板回复（答非所问）。
    const voiceSelfSubject = /我(?:在|用|正|今天|刚|昨天|平时|一直)?[^，。！？,!?]{0,6}(?:语音|声音)(?:里|中|上|聊|说|讲|发|打|听)/.test(text)
      && !/(?:你|TA|她|他)[^，。！？,!?]{0,6}(?:语音|声音|说给|念|读)/.test(text);
    const voiceMessageIntent = !image && !voiceNegated && !voiceSelfSubject
      && !/(?:晚安|电话|打过来|来电)/.test(text)
      && (repeatVoiceIntent
        || /(?:发|来|给).{0,5}(?:一条|一段|一个|个|条)?语音|语音.{0,6}(?:给我听|听听|叫|喊|说|讲|念|唤)|用(?:你的)?(?:声音|语音).{0,8}(?:说|讲|叫|喊).{0,6}(?:给我听|句话)|说句话.{0,5}给我听|(?:想|要|好想|快)?听(?:听|一下)?(?:你的|你)?(?:声音|语音)/.test(text));
    if (voiceMessageIntent) {
      this.streaming = true;
      this.setSendState();
      this.setTyping(true);
      try {
        const sid = window.Session?.getSessionId?.()
          || localStorage.getItem('ai_companion_session_id')
          || localStorage.getItem('session_id') || 'default';
        const res = await fetch('/api/voice/message', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sid,
            character_id: this.contact.name || this.contact.id,
            character_name: this.contact.name || this.contact.id,
            user_text: text,
            repeat: repeatVoiceIntent,
            previous_text: repeatVoiceIntent ? String(lastAssistantMessage.content || '') : '',
          }),
        });
        const result = await res.json().catch(() => ({}));
        // ★ 2026-09-14：后端识别到用户「要文字/不要语音」时，返回 text_only=true 且不带 audio。
        //   这不是失败，要把文字回复正常渲染出来，而不是弹「语音生成失败」。
        if (res.ok && result.ok && result.text_only) {
          const textMsg = {
            id: Store.uid(), role: 'assistant',
            content: result.content || '好，以后我多用文字跟你聊～',
            ts: Date.now(), status: 'done',
          };
          Store.addMessage(this.conv.id, textMsg);
          Store.touchConversation(this.conv.id, textMsg.content);
          body.appendChild(this.renderMsg(textMsg));
          this.scrollBottom(true);
          return;
        }
        if (!res.ok || !result.ok || !result.audio) {
          throw new Error(result.error || '语音生成失败');
        }
        const aiMsg = {
          id: Store.uid(), role: 'assistant', type: 'voice',
          content: result.content || '发给你的语音', audio: result.audio,
          duration: result.duration || 1,
          ts: Date.now(), status: 'done',
        };
        Store.addMessage(this.conv.id, aiMsg);
        Store.touchConversation(this.conv.id, '[语音] ' + aiMsg.content);
        body.appendChild(this.renderMsg(aiMsg));
        this.scrollBottom(true);
        // 微信式语音条：收到后保持静音，只有用户点击气泡才播放。
      } catch (e) {
        const fail = {
          id: Store.uid(), role: 'assistant', status: 'error',
          content: '这条语音没发成功：' + (e.message || '请检查音色配置'),
          ts: Date.now(),
        };
        Store.addMessage(this.conv.id, fail);
        body.appendChild(this.renderMsg(fail));
      } finally {
        this.setTyping(false);
        this.streaming = false;
        this.setSendState();
        this._drainSendQueue();
        renderChatList();
      }
      return;
    }

    // “给我打电话”属于动作指令：直接触发 AI 来电，不让模型推辞。
    if (!image && /(?:给我|向我|跟我|和我)?(?:打个|打一个|拨个|拨一个|来个|来一个)?(?:语音)?电话|打电话给我|给我来电|你打过来/.test(text)) {
      const contact = this.contact;
      const replies = [
        '好呀，正好想听听你的声音。',
        '可以呀，我这就打给你。',
        '嗯，正好有点想你了，等我一下。',
      ];
      const reply = replies[Math.floor(Math.random() * replies.length)];
      const aiMsg = {
        id: Store.uid(), role: 'assistant', content: reply,
        ts: Date.now(), status: 'done',
      };
      Store.addMessage(this.conv.id, aiMsg);
      Store.touchConversation(this.conv.id, reply);
      body.appendChild(this.renderMsg(aiMsg));
      this.scrollBottom();
      toast('好，稍后给你打过来');
      setTimeout(() => {
        if (typeof IncomingCall === 'undefined' || !contact) return;
        IncomingCall.show({
          type: 'incoming_call',
          session_id: window.Session?.getSessionId?.() || 'default',
          contact_id: contact.id,
          character_id: contact.name || contact.id,
          contact_name: contact.name || 'AI',
          contact_avatar: contact.avatarUrl || contact.avatar || '',
          voice_key: contact.voiceKey
            || (typeof contact.voice === 'string' ? contact.voice : '')
            || (contact.voice && contact.voice.voice_key ? contact.voice.voice_key : ''),
          call_reason: 'user_requested',
          call_text: '你让我打过来的',
        });
      }, 1400);
      this.setSendState();
      this._drainSendQueue();
      renderChatList();
      return;
    }

    // ====== 系统指令直通：不调 LLM，直接生成 AI 系统消息 ======
    // 用户输入"显示全部记忆" → 直接打印 userProfile + memStore 摘要
    if (text === '显示全部记忆') {
      return this._showAllMemory();
    }
    // 用户输入"清空全部长期记忆库" → 直接清空 userProfile + memStore
    if (text === '清空全部长期记忆库') {
      return this._clearAllMemory();
    }

    // 定时回复/提醒：检测到“X分钟后回答我 / 明早提醒我”等请求 → 静默排期（不弹窗，AI 到点真发）
    if (text && !image) {
      const delayMs = parseDelay(text);
      const settings = Store.getSettings();
      // 代理/桌面模式由后端可靠任务队列负责；仅直连模式使用本地提醒，避免同一句创建两份任务。
      if (settings.mode === 'direct' && delayMs && /提醒|回答|回复|告诉|叫我|发我|找我|回我|汇报|叫我|到点/.test(text)) {
        Reminders.add(this.contact.id, Date.now() + delayMs, text);
      }
    }

    this.streaming = true;
    this.setSendState();
    renderChatList();

    // 先不显示"正在输入"——等回复延迟过后再显示，像真人过一会才看到消息
    this.scrollBottom();

    let bubble = null;
    let msgId = null;
    let content = '';
    let queue = '';
    let drainTimer = null;

    // 分段气泡 SSE 分支与本地分支共享的变量（提到顶层，供结尾记忆抽取统一使用）
    const currentAffection = Math.max(0, Math.min(100, Number(this.contact.affection != null ? this.contact.affection : 50) || 0));
    // 高好感自动启用后端多气泡生成；满好感才能稳定生成 7~8 条独立内容，而非机械切句。
    const useStream = !!this.contact.useStreamBubbles || currentAffection >= 85;
    let finalText = '';
    let segments = [];
    let firstSeg = '';
    let moreSegs = [];
    let stickers = [];
    let lastSeg = '';

    try {
      // 构造上下文（提示词拼装也在 try 内：任何异常都会显示错误气泡而不是卡死）
      // ★ 上下文窗口抬高（2026-09-04）：AI 多段回复一轮就 4~6 条，60 条 ≈ 10 轮就被挤爆、
      //   早期对话全被挤出窗口导致健忘。抬到 120 条（≈20 轮），gpt-5.4/claude-sonnet-5 等大上下文模型扛得住。
      const history = Store.getMessages(this.conv.id)
        .filter((m) => m.id !== userMsg.id && m.status !== 'error' && m.status !== 'recalled' && m.source !== 'proactive')
        .slice(-120);
      const messages = [{ role: 'system', content: composeSystem(this.contact) }];
      for (const m of history) {
        if (m.type === 'sticker') {
          messages.push({
            role: m.role,
            content: `[历史消息：${m.role === 'user' ? '用户' : 'AI'}发了一张表情包；含义/标签：${stickerMeaningText(m)}]`
          });

        } else if (m.image) {

          // 历史图片不重复上传给模型。
          // 只保留文字语义，避免当前纯文字消息被错误识别为视觉请求。
          const historyImageText =
            m.content && m.content !== '[图片]'
              ? m.content
              : '[历史消息：发送过一张图片]';

          messages.push({
            role: m.role,
            content: historyImageText
          });

        } else {

          messages.push({
            role: m.role,
            content: m.content || ''
          });

        }
      }
      if (userMsg.image) messages.push({ role: 'user', image: userMsg.image, content: text });
      else messages.push({ role: 'user', content: text });

      // 用户从表情面板发来的标记会被后端识别含义；这里再明确“这是本轮表情”，
      // 避免模型把文件名当普通文字。
      const userStickerMatch = text.match(/^\s*\[sticker:([^\]]+)\]\s*$/i);
      if (userStickerMatch) {
        messages[messages.length - 1].content = text + '\n（这是用户刚发来的表情包，请接住它的情绪和梗再回应。）';
      }

      const brain = brainConfig(this.contact, settings);

      // ---- 回复节奏：秒回模式直接整段显示；否则按真人手速逐字显示 ----
      const instant = !!settings.replyInstant;
      const speed = this.contact.replySpeed || 'normal';
      // 每 100ms 显示的字符数（模拟真人手速，正常人约 5-10 字/秒）
      const paceMap = { fast: 2, normal: 1, slow: 1 };
      const pace = instant ? 1e9 : (paceMap[speed] != null ? paceMap[speed] : 1);
      const typeInterval = speed === 'slow' ? 160 : speed === 'fast' ? 75 : 110;
      // 回复延迟：多久之后才"开始"回复（范围内随机，更像真人想一下再回）；秒回模式覆盖一切
      const thinkMs = instant ? 0 : replyDelayMs(this.contact, text.length);

      // ▼ 关键修复 ▼：
      // 流式期间不创建/不写任何气泡——避免"完整段先出现然后被截断再拆段"的视觉 bug
      // content 持续累加到内存（供后续拆段用），但屏幕只剩"对方正在输入..."占位
      const onStreamTick = () => {
        if (queue.length === 0) { drainTimer = null; return; }
        // 不显示：仅消费 queue 防止内存膨胀
        const n = Math.min(queue.length, 256);
        queue = queue.slice(n);
      };

      const feed = (delta) => {
        // 深度思考结束：第一个正式内容到达，清掉「已思考 N 秒」计时（保留 start，用 end 算时长）
        if (this._thinkingTimer) {
          clearInterval(this._thinkingTimer);
          this._thinkingTimer = null;
          this._thinkingEnd = Date.now();
        }
        queue += delta;
        content += delta;
        if (!drainTimer) drainTimer = setInterval(onStreamTick, 60);
      };

      await sleep(thinkMs); // 假装在"看"你的消息——这段时间不显示正在输入

      // ★ 等待用户打完：如果用户又开始在输入框打字（准备下一句），继续等，
      //   等用户发出来（输入框被清空）或停止打字，再开始回复，避免打断连续说话。
      if (!instant) {
        let _waitGuard = 0;
        const _maxWait = 15000;  // 最多额外等 15 秒，避免一直不打字就卡死
        while (_waitGuard < _maxWait) {
          const _ta = $('#chat-input');
          const _draft = _ta ? _ta.value.trim() : '';
          if (!_draft) break;  // 输入框空了 = 没在打字（或已发出），继续正常回复
          await sleep(600);
          _waitGuard += 600;
        }
      }

      // ▼ 客户端指令拦截（修复：表情包等动作请求本地处理，不送入 LLM） ▼
      const cmdResult = CommandInterceptor.intercept(text);
      if (cmdResult.intercepted && cmdResult.type === 'sticker') {
        // 表情包指令：从后端素材库随机取一张渲染为气泡
        this.setTyping(true);
        this.scrollBottom();
        await sleep(400 + Math.random() * 600);  // 模拟"找表情包"的延迟
        const sticker = await CommandInterceptor.fetchRandomSticker();
        this.setTyping(false);
        if (sticker && sticker.url) {
          const stickerMsg = {
            id: Store.uid(), role: 'assistant', type: 'sticker',
            content: '[表情包]', ts: Date.now(), status: 'done',
            _stickerUrl: sticker.url, _stickerFilename: sticker.filename,
          };
          Store.addMessage(this.conv.id, stickerMsg);
          const body = $('#chat-body');
          body.appendChild(this.renderMsg(stickerMsg));
          this.scrollBottom();
        } else {
          // 素材库为空 → 用文字回复（但不是 LLM 的"我没有"）
          const emptyMsg = {
            id: Store.uid(), role: 'assistant', content: '唔……表情包箱空空的耶，你先传几张进来我以后就能发给你啦～',
            ts: Date.now(), status: 'done',
          };
          Store.addMessage(this.conv.id, emptyMsg);
          const body = $('#chat-body');
          body.appendChild(this.renderMsg(emptyMsg));
          this.scrollBottom();
        }
        this.streaming = false;
        this.setSendState();
        this._drainSendQueue();
        renderChatList();
        return;  // early return：不走 streamAI
      }

      // 微信风格：不显示三个点气泡，只在顶部状态栏显示"对方正在输入…"
      const body = $('#chat-body');

      if (useStream) {
        // ===== 分段气泡 SSE 分支（服务端按情绪把回复拆成多条 bubble） =====
        // ★ 思考状态按次重置：避免上一轮残留的思考文本渲染到这一轮
        this._thinkingText = ''; this._thinkingStart = null; this._thinkingEnd = null;
        // ★ 2026-09-12：本轮折叠框是否已画过 / 思考内容是否已挂到气泡上
        this._thinkingBlockShown = false;
        let _thinkingAttached = false;
        await new Promise((resolve) => {
          let settled = false;
          let streamTimeout = null;
          const finish = () => {
            if (settled) return;
            settled = true;
            if (streamTimeout) clearTimeout(streamTimeout);
            resolve();
          };
          streamTimeout = setTimeout(() => {
            if (settled) return;
            const m = {
              id: Store.uid(), role: 'assistant',
              content: 'AI 回复超时，请稍后重试。',
              ts: Date.now(), status: 'error',
            };
            try {
              if (!segments.length) {
                Store.addMessage(this.conv.id, m);
                if (body) body.appendChild(this.renderMsg(m));
              }
            } catch (err) { console.error('[ChatStream] timeout render failed:', err); }
            this.setTyping(false);
            finish();
          }, currentAffection >= 95 ? 180000 : 95000);
          if (!window.ChatStream || typeof window.ChatStream.sendMessage !== 'function') {
            const m = {
              id: Store.uid(), role: 'assistant',
              content: '聊天组件加载失败，请按 Ctrl+F5 刷新页面后重试。',
              ts: Date.now(), status: 'error',
            };
            Store.addMessage(this.conv.id, m);
            if (body) body.appendChild(this.renderMsg(m));
            finish();
            return;
          }
          ChatStream.sendMessage({
            sessionId:   window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default',
            // ★ 传角色名（能映射到角色配置/记忆隔离键），UUID 只做 localStorage 键
            characterId: this.contact.name || this.contact.id,
            characterName: this.contact.name || this.contact.id,
            message:     text,
            messages:    messages,
            replyTo:     quoteForThis || null,
            sticker:     !!(this.contact && this.contact.sticker),
            // ★ 把前端配置的 API Key 透传给后端。后端 config.api_key() 在打包
            //   运行时常常是空的（DATA_DIR 被 AI_COMPANION_DATA_DIR 重定向），
            //   语义分析等后端自调用模块拿不到 key 就只能静默降级。
            key:         (brainConfig(this.contact, Store.getSettings()) || {}).key || '',
            // ★ 单角色大脑（2026-09-11）：只透传人格设置里配的「角色级」模型；
            //   不再把全局 settings.model 兜底进来——模型归属后端角色卡统一决定，
            //   否则旧的全局值会盖住人格设置里换的主脑。
            model:       ((this.contact && this.contact.model) || '').trim(),
            baseUrl:     (brainConfig(this.contact, Store.getSettings()) || {}).baseUrl || '',
            timeoutMs:   currentAffection >= 95 ? 180000 : 90000,
            onTyping:    () => { this.setTyping(true); this.scrollBottom(); },
            onAgentEvent: (ev) => {
              // ★ 2026-09-11：Agent 事件渲染（原 chat_stream 不转发 agent_* 事件，
              //   她进了 Agent 干了活但界面什么都不显示 → "叫她看代码直接不回复"）
              handleAgentEvent(ev, this);
              if (this._thinkingTimer) {
                clearInterval(this._thinkingTimer);
                this._thinkingTimer = null;
                this._thinkingStart = null;
              }
            },
            onThinking:  (ev) => {
              // ★ 思考全文：分段气泡分支在思考结束时把推理全文一次性推来
              //   （type=thinking + content）。角色开启「展示思考过程」时，
              //   在本轮气泡上方插入可折叠的思考过程块，与本地流式分支同款。
              if (ev && ev.content) {
                if (this._thinkingTimer) {
                  clearInterval(this._thinkingTimer);
                  this._thinkingTimer = null;
                  this._thinkingEnd = Date.now();
                }
                const _txt = String(ev.content || '');
                if (_txt.trim()) {
                  // ★ 2026-09-11 持久化：思考全文先存本轮（首段消息创建时带上 thinking 字段），
                  //   DOM 折叠框只画一次（_thinkingBlockShown 防止 m1 处重复插）
                  this._thinkingText = _txt;
                  this._thinkingSec = (ev.seconds && Number(ev.seconds) > 0)
                    ? Math.min(30, Math.round(Number(ev.seconds)))
                    : ((this._thinkingStart && this._thinkingEnd)
                      ? Math.max(1, Math.round((this._thinkingEnd - this._thinkingStart) / 1000))
                      : 3);
                  if (this.contact && this.contact.show_thinking && body) {
                    body.appendChild(_buildThinkingBlock(_txt, this._thinkingSec));
                    this._thinkingBlockShown = true;
                    this.scrollBottom(true);
                  }
                }
                return;
              }
              // ★ 流式推理：GLM 开始思考时，顶部显示「已思考 N 秒」
              if (!this._thinkingStart) {
                this._thinkingStart = Date.now();
                if (this._thinkingTimer) clearInterval(this._thinkingTimer);
                this._thinkingTimer = setInterval(() => {
                  const _sec = Math.floor((Date.now() - this._thinkingStart) / 1000);
                  const _sub = document.querySelector('#chat-subtitle');
                  if (_sub) {
                    _sub.innerHTML = '';
                    const _dot = document.createElement('span');
                    _dot.className = 'online-dot';
                    _sub.appendChild(_dot);
                    _sub.appendChild(document.createTextNode(' 已思考 ' + _sec + ' 秒'));
                  }
                }, 1000);
                this.setTyping(true);
              }
            },
            onPending:   (info) => {
              this.setTyping(false);
              const _info = info || {};
              const _hint = _info.hint || '她暂时不在';
              const _eta = Math.max(1, Number(_info.eta) || 0);
              const _etaText = _eta < 60 ? '稍后' : (_eta < 3600 ? Math.round(_eta / 60) + ' 分钟' : Math.round(_eta / 3600) + ' 小时');
              const _noticeEl = h('div', { class: 'msg-system', style: 'text-align:center;margin:8px 0;color:var(--text-3,#888);font-size:12px' });
              _noticeEl.appendChild(h('span', { style: 'display:inline-flex;align-items:center;gap:4px;background:var(--bg-2,#f2f2f7);border-radius:12px;padding:4px 10px', text: _hint + '，预计 ' + _etaText + ' 后回复你' }));
              body.appendChild(_noticeEl);
              this.scrollBottom();
              Store.touchConversation(this.conv.id, _hint);
              toast(_hint + '，预计 ' + _etaText + ' 后回复你', 4000);
              finish();
            },
            onTurn:      (turn) => {
              // 深度思考结束：第一个正式内容到达，清掉「已思考 N 秒」计时
              if (this._thinkingTimer) {
                clearInterval(this._thinkingTimer);
                this._thinkingTimer = null;
                this._thinkingEnd = Date.now();
              }
              let c = (turn.content || '').trim();
              // 关闭「括号动作描写」时，剥掉所有（…）/(…) 内容（与本地流式分支保持一致）
              if (this.contact && this.contact.actions === false) {
                c = stripBracketActions(c);
              }
              // ★ 星露谷动作标记：剥出并真执行（聊天里说的=游戏里做的）
              const _ga = extractGameActions(c);
              if (_ga.actions.length) { execGameActions(_ga.actions); c = _ga.text; }
              const turnSticker = turn.sticker || null;
              if (!c && !turnSticker && !turn.audio) return;
              this.setTyping(true);
              const m = {
                id: Store.uid(), role: 'assistant',
                content: c, ts: Date.now(), status: 'done',
                _emotion: turn.emotion || 'calm',
              };
              // ★ AI 主动引用：气泡上方显示被引用的用户消息
              if (turn.reply_to && turn.reply_to.content) m.reply_to = turn.reply_to;
              // ★ 微信式语音条：有音频时渲染成可点击播放的语音条，不自动播放
              if (turn.audio) {
                m.type = 'voice';
                m.audio = turn.audio;
                m.duration = Math.max(1, Math.round(String(c || '').length / 4.2) || 1);
              } else if (turnSticker && turnSticker.url) {
                m.type = 'sticker';
                m.content = '[表情包]';
                m._stickerUrl = turnSticker.url;
                m._stickerFilename = turnSticker.filename || '';
              }
              // ★ 2026-09-12 修复：思考过程之前只在「本地流式拆段」分支挂到消息上，
              //   而聊天实际走的是本分段气泡分支（useStream）——这里从来不带 thinking，
              //   于是刷新页面重载历史时思考框必然消失（用户反馈
              //   "深度思考框展示刷新页面后又没了，没有永久保存"）。
              //   实测 localStorage 里连一条带 thinkingSec 的消息都没有，即从未落盘。
              //   在第一条气泡上带 thinking/thinkingSec，与 rerender 的读取端对齐。
              if (!_thinkingAttached) {
                _thinkingAttached = true;
                const _tt = (this._thinkingText && String(this._thinkingText).trim())
                  ? String(this._thinkingText).trim().slice(0, 3000) : '';
                if (_tt) {
                  const _ttSec = Number(this._thinkingSec)
                    || ((this._thinkingStart && this._thinkingEnd)
                      ? Math.max(1, Math.round((this._thinkingEnd - this._thinkingStart) / 1000)) : 0);
                  m.thinking = _tt;
                  m.thinkingSec = _ttSec > 0 ? _ttSec : 1;
                }
                // 关着展示开关也照样存（回看时再开就有），与另一分支保持一致
                this._thinkingText = ''; this._thinkingSec = 0;
              }
              Store.addMessage(this.conv.id, m);
              body.appendChild(this.renderMsg(m));
              this.scrollBottom(true);
              requestAnimationFrame(() => this.scrollBottom(true));
              finalText += (finalText ? '\n' : '') + c;
              segments.push(c);
            },
            onDone:   () => {
              if (this._thinkingTimer) { clearInterval(this._thinkingTimer); this._thinkingTimer = null; this._thinkingStart = null; }
              this.setTyping(false); finish();
            },
            onError:  (msg) => {
              if (!segments.length) {
                const m = {
                  id: Store.uid(), role: 'assistant',
                  content: msg || '（分段服务出错，请稍后用普通模式）',
                  ts: Date.now(), status: 'error',
                };
                Store.addMessage(this.conv.id, m);
                body.appendChild(this.renderMsg(m));
              }
              this.setTyping(false);
              finish();
            },
          });
        });
        firstSeg = segments[0] || finalText || '';
        moreSegs = segments.slice(1);
      } else {
      // ===== 本地流式 + 拆段打字分支（原逻辑，默认） =====
      this.setTyping(true);
      this.scrollBottom();
      let agentHandled = false;
      _agentTrail = null;  // 每次发送前重置 Agent 轨迹状态，避免上一次残留
      await streamAI({
        model: brain.model, baseUrl: brain.baseUrl, messages,
        key: brain.key,
        mode: settings.mode,
        proxyUrl: settings.proxyUrl,
        visionKey: settings.visionKey,
        visionModel: settings.visionModel,
      }, feed, null, (reasoning) => {
        // 深度思考：累积思考内容 + 顶部状态栏显示「已思考 N 秒」
        if (reasoning) this._thinkingText = (this._thinkingText || '') + reasoning;
        if (!this._thinkingStart) {
          this._thinkingStart = Date.now();
          if (this._thinkingTimer) clearInterval(this._thinkingTimer);
          this._thinkingTimer = setInterval(() => {
            const sec = Math.floor((Date.now() - this._thinkingStart) / 1000);
            const sub = document.querySelector('#chat-subtitle');
            if (sub) {
              sub.innerHTML = '';
              const dot = document.createElement('span');
              dot.className = 'online-dot';
              sub.appendChild(dot);
              sub.appendChild(document.createTextNode(' 已思考 ' + sec + ' 秒'));
            }
          }, 1000);
          this.setTyping(true);
        }
      }, (ev) => {
        // Agent 助手模式：渲染执行轨迹 + 最终答案
        const handled = handleAgentEvent(ev, this);
        if (handled) agentHandled = true;
        if (this._thinkingTimer) {
          clearInterval(this._thinkingTimer);
          this._thinkingTimer = null;
          this._thinkingStart = null;
        }
        this.setTyping(true);
      });
      // 等缓冲区排干（用 onStreamTick 消费，不入屏）
      let guard = 0;
      while (queue.length && guard++ < 3000) await sleep(70);
      if (drainTimer) { clearInterval(drainTimer); drainTimer = null; }
      queue = '';

      // ---- 表情包：STICKER:emoji → 大表情；[sticker:文件名] → 图片表情包 ----
      stickers = [];
      const gameActions = [];
      finalText = content
        .replace(/STICKER:\s*([^\s，。！？,.;:、]+)/g, (m, emoji) => { stickers.push({ emoji: emoji }); return ''; })
        .replace(/\[sticker:([^\]]+)\]/g, (m, fn) => { stickers.push({ file: String(fn || '').trim() }); return ''; })
        .replace(/\[sd[:：]\s*([^\]]{1,12})\s*\]/g, (m, act) => { gameActions.push(String(act || '').trim()); return ''; })
        .trim();
      // ★ 星露谷动作标记：真执行（游戏在线时聊天里「说要做」=「游戏里真做」）
      execGameActions(gameActions);

      // 普通流式也做可靠兜底：开启表情包后，用户回表情提高接梗概率；
      // 普通聊天维持低频，不会每轮都刷图。
      if (this.contact && this.contact.sticker && stickers.length === 0) {
        const userSentSticker = /^\s*\[sticker:[^\]]+\]\s*$/i.test(text);
        const probability = userSentSticker ? 0.55 : 0.16;
        if (Math.random() < probability) {
          const picked = await pickContextSticker(text + '\n' + finalText, window.__aiEmotion || '', userStickerMatch ? userStickerMatch[1] : '');
          if (picked && picked.filename) stickers.push({ file: picked.filename, meta: picked });
        }
      }

      // 关闭「括号动作描写」时，剥掉所有（…）/(…) 内容，只留纯对话
      if (this.contact && this.contact.actions === false) {
        finalText = stripBracketActions(finalText);
      }

      // ★ 对话质感后处理（方式B）：口癖 + 欲言又止，让回复更像真人随手打的
      if (typeof Texture !== 'undefined' && finalText && finalText.length <= 60) {
        try {
          finalText = Texture.process(finalText, window.__aiEmotion || 'calm', 0.5);
        } catch (_e) {}
      }

      // ★ AI 控制电脑：解析 [ACTION]{json}[/ACTION] 标记，清洗文本 + 调后端执行动作
      // ★ 执行闭环：执行结果 POST 回 /api/execution_feedback，下一轮回喂模型（纠正「说≠做」）
      try {
        const actionRe = /\[ACTION\]([\s\S]*?)\[\/ACTION\]/g;
        let m;
        while ((m = actionRe.exec(finalText)) !== null) {
          try {
            const act = JSON.parse(m[1].trim());
            if (act && act.type) {
              const sid = (typeof Session !== 'undefined' && Session.getSessionId) ? Session.getSessionId() : 'default';
              const cname = (Chat.contact && Chat.contact.name) ? Chat.contact.name : undefined;
              const _actName = '操作电脑（' + (act.target || act.app || act.name || act.type) + '）';
              fetch('/api/control/execute', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: act, session_id: sid, character_name: cname }),
              }).then(r => r.json().catch(() => ({}))).then(j => {
                fetch('/api/execution_feedback', {
                  method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ action: _actName, ok: !j.error, detail: (j.error && j.error.message) || '' }),
                }).catch(() => {});
              }).catch(() => {
                fetch('/api/execution_feedback', {
                  method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ action: _actName, ok: false, detail: '执行请求没发出去' }),
                }).catch(() => {});
              });
            }
          } catch (_e) {}
        }
        finalText = finalText.replace(/\[ACTION\][\s\S]*?\[\/ACTION\]/g, '').trim();
      } catch (_e) {}

      // ---- 拆段：按"分段发送"开关 + "回复段数范围"决定 firstSeg / moreSegs ----
      const affection = Math.max(0, Math.min(100, Number(this.contact.affection != null ? this.contact.affection : 50) || 0));
      const configuredMin = (this.contact.replySegMin != null) ? Number(this.contact.replySegMin) : 1;
      const configuredMax = (this.contact.replySegMax != null) ? Number(this.contact.replySegMax) : 6;
      const [segMin, segMax] = dynamicSegmentRange(this.contact, finalText, configuredMin, configuredMax);
      segments = (this.contact.splitMsg !== false) ? splitIntoSegments(finalText, segMin, segMax) : [finalText];
      firstSeg = segments[0] || finalText || '';
      moreSegs = segments.slice(1);

    // ---- 微信风格：不需要"想清楚再开打"的停顿，流结束即显示 ----
    // if (!instant && finalText) {
    //   await sleep(preTypeMs(finalText.length));
    // }

      // ---- 微信风格：只负责等待打字时间和填充文字，显示和滚动交给外层 ----
      // ★ UI 打磨 v1：打字节奏加随机扰动（短句快/长句慢 + ±20% jitter + 思考停顿 + 感叹号加分）
      const typeInto = (bubbleEl, text, segLen) => new Promise((resolve) => {
        if (!text) return resolve();

        const len = text.length;

        // 基础速度：短句快（脱口而出），长句慢（在组织语言）
        const basePerChar = len < 8  ? 100 :
                            len < 20 ? 130 :
                            len > 40 ? 165 : 145;
        const baseTime = len * basePerChar;

        // 随机扰动：±20% 基础时间
        const jitter = (Math.random() * 0.4 - 0.2) * baseTime;

        // 思考停顿：200-700ms（看完消息想一下再打）
        const thinkPause = 200 + Math.random() * 500;

        // 含感叹号/问号：额外停顿（激动/疑惑）
        const hasExclaim = /[！!？?]/.test(text);
        const exclamBonus = hasExclaim ? (80 + Math.random() * 180) : 0;

        // 总时间：最短600ms，最长9000ms
        const totalTime = Math.max(600, Math.min(9000,
          baseTime + jitter + thinkPause + exclamBonus
        ));

        setTimeout(() => {
          // 只填文字，不管显示和滚动（外层在await返回后紧挨着处理）
          bubbleEl.textContent = text;
          resolve();
        }, totalTime);
      });

      // ---- 先建第一段的消息气泡 + 逐字打字（根治：从未把整段打到屏上） ----
      if (firstSeg || stickers.length) {
      if (firstSeg) {
        msgId = Store.uid();
        // ★ 2026-09-11：思考过程随消息持久化（上限 3000 字防 localStorage 膨胀），
        //   展示开关仍是人格页的「展示思考过程」——关着也存，开着随时回看
        const _thinkTxt = (this._thinkingText && String(this._thinkingText).trim())
          ? String(this._thinkingText).trim().slice(0, 3000) : '';
        const _thinkSec = Number(this._thinkingSec)
          || ((this._thinkingStart && this._thinkingEnd)
            ? Math.max(1, Math.round((this._thinkingEnd - this._thinkingStart) / 1000)) : 0);
        const m1 = { id: msgId, role: 'assistant', content: '', ts: Date.now(), status: 'streaming',
                     thinking: _thinkTxt, thinkingSec: _thinkSec };
        Store.addMessage(this.conv.id, m1);
        const el1 = this.renderMsg(m1);
        el1.style.visibility = 'hidden';  // 先隐藏，不占位撑高度
        body.appendChild(el1);
        // ★ 思考过程块：角色开启「展示思考过程」且有思考内容时，插在回复气泡上方
        //   （ChatStream 分支已在 onThinking 时画过 → _thinkingBlockShown 防重复）
        if (_thinkTxt && this.contact && this.contact.show_thinking && !this._thinkingBlockShown) {
          body.insertBefore(_buildThinkingBlock(_thinkTxt, _thinkSec || 3), el1);
        }
        this._thinkingText = ''; this._thinkingSec = 0; this._thinkingStart = null;
        this._thinkingEnd = null; this._thinkingBlockShown = false;
        // 不在这里 scrollBottom，等气泡有内容再滚
        bubble = el1.querySelector('.bubble');
        this.setTyping(true);   // 修复 Bug2：本段发话前展示"对方正在输入…"（贯穿至全体段完成）
        await typeInto(bubble, firstSeg, firstSeg.length);   // 提笔停顿跟本段字数成比例
        // 文字填好了，再显示 + 滚动（两步必须紧挨着，中间不能有 await）
        el1.style.visibility = '';
        this.scrollBottom(true);  // force=true 强制滚
        requestAnimationFrame(() => this.scrollBottom(true));  // 重排后再滚一次保险
        Store.updateMessage(this.conv.id, msgId, { content: firstSeg, status: 'done' });
        bubble.classList.remove('streaming');
      }
        // 表情包（不动画，逐张插入）
        for (let i = 0; i < stickers.length; i++) {
          await sleep(150);
          const st = stickers[i];
          const sm = { id: Store.uid(), role: 'assistant', type: 'sticker', ts: Date.now() + i, status: 'done' };
          if (st && st.file) {
            // [sticker:文件名] → 图片表情包
            sm.content = '[表情包]';
            sm._stickerUrl = '/stickers/' + st.file;
            sm._stickerFilename = st.file;
            if (st.meta) {
              sm._stickerMeaning = st.meta.meaning || '';
              sm._stickerTag = st.meta.tag || '';
              sm._stickerDesc = st.meta.desc || '';
            }
          } else {
            // STICKER:emoji → 大表情
            sm.content = (st && st.emoji) || '😂';
          }
          Store.addMessage(this.conv.id, sm);
          body.appendChild(this.renderMsg(sm));
          this.scrollBottom();
        }
      }

      // 没有任何内容：提示异常而不是卡住（Agent 任务已由 agent_final 渲染，跳过）
      if (!firstSeg && !stickers.length && !agentHandled) {
        const m = {
          id: Store.uid(), role: 'assistant',
          content: '（对方没有回复内容：模型可能在思考超时或接口异常，请稍后重试）',
          ts: Date.now(), status: 'error',
        };
        Store.addMessage(this.conv.id, m);
        body.appendChild(this.renderMsg(m));
      }

      // ---- 后续段：每段独立成新消息气泡 + 打字时间后整条出现 ----
      // 段间停顿已由 typeInto 内部的打字时间模拟，这里不再额外加 segGapMs
      for (let i = 0; i < moreSegs.length; i++) {
        const seg = moreSegs[i];
        // await sleep(segGapMs(prevLen));  // 已由 typeInto 内部打字时间模拟
        const mid = Store.uid();
        const sm2 = { id: mid, role: 'assistant', content: '', ts: Date.now() + 100 + i, status: 'streaming' };
        Store.addMessage(this.conv.id, sm2);
        const segEl = this.renderMsg(sm2);
        segEl.style.visibility = 'hidden';  // 先隐藏
        body.appendChild(segEl);
        // 不在这里滚动
        const segBubble = segEl.querySelector('.bubble');
        await typeInto(segBubble, seg, seg.length);   // 提笔停顿跟本段字数成比例
        // 文字填好，显示 + 滚动同步
        segEl.style.visibility = '';
        this.scrollBottom(true);
        requestAnimationFrame(() => this.scrollBottom(true));
        Store.updateMessage(this.conv.id, mid, { content: seg, status: 'done' });
        segBubble.classList.remove('streaming');
      }   // 闭合「后续段 for」循环
      }   // ===== end else (本地分支) =====

      // 用最后一段作为 touchConversation 的摘要（两条分支统一）
      lastSeg = (useStream ? (segments[segments.length - 1] || '') : (moreSegs.length ? moreSegs[moreSegs.length - 1] : firstSeg)) || '';
      Store.touchConversation(this.conv.id, stickers.length ? '「表情包」' : (lastSeg || '（没有回复）'));

      // AI回复完成后，如果窗口不在前台，弹出桌面通知
      if (window.desktopWin && window.desktopWin.showNotify && finalText) {
        try {
          const focused = window.desktopWin.isFocused ? window.desktopWin.isFocused() : false;
          if (!focused) {
            window.desktopWin.showNotify({
              name: this.contact.name || 'AI伴侣',
              content: finalText,
              avatar: this.contact.avatarUrl || '',
              contact_id: this.contact.id,
            });
          }
        } catch (_) {}
      }

      // ---- 末段完整性检测：AI 偶尔会在半句自然 stop（finish_reason='stop' 但没收尾） ----
      // deepseek 采样随机性引发，靠 prompt 无法彻底消除；这里静默自动续写（像真人没打完，停一下又补一句，不留任何按钮/提示）
      const lastSegForHint = (moreSegs.length ? moreSegs[moreSegs.length - 1] : firstSeg) || '';
      if (looksIncompleteTail(finalText) && Chat._autoContinued !== this.conv.id) {
        Chat._autoContinued = this.conv.id;
        const tail = lastSegForHint.slice(-6);
        setTimeout(() => {
          if (!Chat.contact || Chat.contact.id !== this.contact.id) return;
          if (Chat.streaming) return;
          Chat.send('（接着刚才那段没说完的，从"' + tail + '"后面接着说，用你的角色继续，语气自然连贯，不要重复前面的内容）');
        }, 1200);
      }

      // 亲密度动态增长
      updateIntimacy(this.contact, text.length);
      // 旧对话自动压缩成记忆（后台执行，不阻塞回复）
      maybeCompressHistory(this.contact, this.conv.id).catch(() => {});
      // ★★★【关键修改】★★★
      // 抽取新信息（事实/偏好/反思）——之前是 fire-and-forget，但用户连续发消息时会出现
      // "上一句教的事下一句就忘"的 bug（异步抽取还没写入 userProfile，下一轮 prompt 就构建好了）。
      // 现在改为 await 同步：等抽取+写入完成，userProfile 才更新，**保证下一轮对话立刻能用到**。
      // 抽取/反思时间 1-3 秒（已经在 thinkMs 范围内），加上现在这个 await，用户感知不到差异。
      // 但同一对话内立刻发下一条消息 → 立刻能命中记忆。
      try {
        // 【5 轮兜底】累计对话轮数；每 5 轮强制抽取一次（即使本轮是闲聊），整合之前漏掉的事实
        const cur = Number(this.contact.roundsSinceExtract) || 0;
        // ★ 2026-09-16 收窄（控制器裁决 ④）：旧判据
        //     /(记住|我叫|我过敏|我喜欢|我不喜欢|我的.{1,8}是|我讨厌|我对.{1,6}过敏)/
        //   里 `我的.{1,8}是` 会把「我的想法是这样」「我的意思是」这类日常句当成事实，
        //   强制抽取（force 还会绕过长度的门禁）→ 短句反复刷抽取、往记忆库写垃圾。
        //   现在判据由 public/js/internal-msg-core.js 单一来源提供：要求"真事实形态" +
        //   最短长度 + 窗口内次数上限；超限只**降级为非强制**（本轮抽取照做，只是不绕过长度门）。
        const _recentForce = _forceExtractLog.get(this.contact.id) || [];
        const _fr = _decideForceExtract({
          text,
          roundsSinceExtract: cur,
          recentForceTs: _recentForce,
          now: Date.now(),
        });
        const next = _fr.next;
        const force = _fr.force;
        if (_fr.reason === 'capped') {
          const _ic = _internalCore();
          console.warn('[记忆抽取] 关键词强制抽取已达窗口上限（'
            + (_ic ? _ic.FORCE_MAX_PER_WINDOW : '?') + ' 次 / '
            + (_ic ? Math.round(_ic.FORCE_WINDOW_MS / 60000) : '?') + ' 分钟）：本轮降级为非强制抽取');
        }
        Store.updateContact(this.contact.id, { roundsSinceExtract: next });
        if (force) {
          if (_fr.reason === 'fact') _forceExtractLog.set(this.contact.id, _fr.recentForceTs);
          // 触发后清零，让下一轮重新累计
          Store.updateContact(this.contact.id, { roundsSinceExtract: 0 });
        }
        const mem = await extractNewMemory(this.contact, this.conv.id, text, lastSeg || firstSeg, force);
        // 记忆总结：积累到一定量后，AI 会把零散记忆整合成"关于你"的画像（同步等待以保证下次对话立即可用）
        await autoSummarizeMemory(this.contact, this.conv.id).catch(() => {});
      } catch (_) { /* 抽不出 / 模型挂了也不阻塞主流程 */ }
    } catch (err) {
      this.setTyping(false);
      if (drainTimer) { clearInterval(drainTimer); drainTimer = null; }
      // ★ 离线状态：角色暂时不在，显示灰色居中提示（不是错误）
      if (err && err.offline) {
        const info = err.offline || {};
        const hint = info.hint || '她暂时不在';
        const eta = Math.max(1, Number(info.eta) || 0);
        const etaText = eta < 60 ? '稍后' : (eta < 3600 ? Math.round(eta / 60) + ' 分钟' : Math.round(eta / 3600) + ' 小时');
        const noticeEl = h('div', { class: 'msg-system', style: 'text-align:center;margin:8px 0;color:var(--text-3,#888);font-size:12px' });
        noticeEl.appendChild(h('span', { style: 'display:inline-flex;align-items:center;gap:4px;background:var(--bg-2,#f2f2f7);border-radius:12px;padding:4px 10px', text: hint + '，预计 ' + etaText + ' 后回复你' }));
        $('#chat-body').appendChild(noticeEl);
        this.scrollBottom();
        Store.touchConversation(this.conv.id, hint);
        toast(hint + '，预计 ' + etaText + ' 后回复你', 4000);
      } else {
        // 把中断的半截消息标记为错误，避免重渲染后一直显示“正在生成”光标
        if (msgId) Store.updateMessage(this.conv.id, msgId, { status: 'error', content: content || '（已中断）' });
        if (bubble) bubble.classList.remove('streaming');
        const m = { id: Store.uid(), role: 'assistant', content: '出错了：' + err.message, ts: Date.now(), status: 'error' };
        Store.addMessage(this.conv.id, m);
        $('#chat-body').appendChild(this.renderMsg(m));
        Store.touchConversation(this.conv.id, '[出错] ' + err.message);
        toast(err.message, 3200);
      }
    }

    // 保险：无论什么路径，都确保"正在输入"状态被清掉
    this.setTyping(false);
    if (bubble) bubble.classList.remove('streaming');
    this.setTyping(false);

    this.streaming = false;
    this.setSendState();
    this._drainSendQueue();
    renderChatList();
  },

  async _drainSendQueue() {
    if (this._drainingQueue || this.streaming || !this.pendingSends.length) return;
    this._drainingQueue = true;
    try {
      while (!this.streaming && this.pendingSends.length) {
        const next = this.pendingSends.shift();
        if (!next || !this.contact || this.contact.id !== next.contactId) continue;
        if (!this.conv || this.conv.id !== next.convId) continue;
        await this._sendQueuedRequest(next);
      }
    } finally {
      this._drainingQueue = false;
    }
  },

  async _sendQueuedRequest(item) {
    await this.send(item.text, item.image, item);
  },
};

/* ============================================================
   Agent 助手模式：执行轨迹渲染（可折叠卡片）
   ============================================================ */
let _agentTrail = null;

function _agentLine(text, kind) {
  const line = h('div', { class: 'agent-line ' + (kind || '') });
  line.textContent = text;
  return line;
}

// ★ 深度思考过程块：微信气泡风格（她那一侧的白气泡），默认展开、思考等长的时间后
//   自动折叠，可点标题展开/收起
function _buildThinkingBlock(text, sec) {
  const sReal = Math.max(1, Math.round(Number(sec) || 3));
  const autoCollapse = Math.min(Math.max(sReal, 5), 25);  // 思考多久展示多久（上限 25s 自动收起）
  const wrap = h('div', { style: 'display:flex;margin:4px 0 10px;align-items:flex-start' });
  const bubble = h('div', {
    style: 'max-width:82%;background:var(--card,#fff);border-radius:4px 14px 14px 14px;padding:9px 12px;box-shadow:0 1px 3px rgba(0,0,0,.08)'
  });
  const header = h('div', { style: 'cursor:pointer;display:flex;align-items:center;gap:6px;font-size:12px;color:#888;user-select:none' });
  const content = h('div', { style: 'white-space:pre-wrap;word-break:break-word;font-size:12px;color:#999;line-height:1.7;margin-top:4px;max-height:280px;overflow-y:auto' });
  content.textContent = text || '';
  const render = () => {
    header.textContent = (wrap._collapsed ? '▸ ' : '▾ ') + '💭 思考过程 · ' + sReal + ' 秒';
    content.style.display = wrap._collapsed ? 'none' : '';
  };
  header.addEventListener('click', () => { wrap._collapsed = !wrap._collapsed; render(); });
  bubble.appendChild(header);
  bubble.appendChild(content);
  wrap.appendChild(bubble);
  render();
  setTimeout(() => { if (!wrap._collapsed) { wrap._collapsed = true; render(); } }, autoCollapse * 1000);
  return wrap;
}

function _ensureAgentTrail(body) {
  if (_agentTrail && _agentTrail.el && _agentTrail.el.parentNode) return _agentTrail;
  const card = h('div', { class: 'agent-trail' });
  const head = h('div', { class: 'agent-trail-head' });
  head.appendChild(h('span', { class: 'agent-trail-dot' }));
  head.appendChild(h('span', { class: 'agent-trail-title', text: '骨子正在帮你做事' }));
  head.appendChild(h('span', { class: 'agent-trail-status', text: '进行中' }));
  head.appendChild(h('span', { class: 'agent-trail-toggle', text: '收起' }));
  const trailBody = h('div', { class: 'agent-trail-body' });
  card.appendChild(head);
  card.appendChild(trailBody);
  body.appendChild(card);
  head.addEventListener('click', () => {
    card.classList.toggle('collapsed');
    const t = card.querySelector('.agent-trail-toggle');
    if (t) t.textContent = card.classList.contains('collapsed') ? '展开' : '收起';
  });
  _agentTrail = { el: card, body: trailBody };
  return _agentTrail;
}

function _renderAgentApproval(ap) {
  const box = h('div', { class: 'agent-approval' });
  box.appendChild(h('div', { class: 'agent-approval-title', text: '⚠️ 需要你确认：' + (ap.summary || '执行一个操作') }));
  const detail = ap.detail || {};
  if (detail.diff) {
    const pre = h('pre', { class: 'agent-approval-diff' });
    pre.textContent = detail.diff;
    box.appendChild(pre);
  } else if (detail.command) {
    box.appendChild(h('div', { class: 'agent-approval-detail', text: '命令：' + detail.command }));
  } else if (detail.content) {
    box.appendChild(h('div', { class: 'agent-approval-detail', text: '内容预览：' + String(detail.content).slice(0, 300) }));
  }
  const btnRow = h('div', { class: 'agent-approval-btns' });
  const okBtn = h('button', { class: 'agent-btn ok', text: '同意' });
  const noBtn = h('button', { class: 'agent-btn no', text: '取消' });
  const settle = (path, bodyObj, okText, noText) => {
    fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bodyObj || {}) }).catch(() => {});
    okBtn.disabled = true; noBtn.disabled = true;
    okBtn.textContent = okText; noBtn.textContent = noText;
  };
  okBtn.addEventListener('click', () => settle('/api/agent/approval/' + ap.id + '/approve', {}, '已同意', '取消'));
  noBtn.addEventListener('click', () => settle('/api/agent/approval/' + ap.id + '/reject', { reason: '' }, '同意', '已取消'));
  btnRow.appendChild(okBtn);
  btnRow.appendChild(noBtn);
  box.appendChild(btnRow);
  return box;
}

function handleAgentEvent(ev, chat) {
  const body = $('#chat-body');
  if (!body || !chat || !chat.conv) return false;
  const type = ev.type || '';

  if (type === 'agent_final') {
    const trail = _agentTrail;
    if (trail && trail.el && trail.el.parentNode) {
      const st = trail.el.querySelector('.agent-trail-status');
      if (st) st.textContent = '✓ 完成';
      trail.el.classList.add('collapsed');
      const t = trail.el.querySelector('.agent-trail-toggle');
      if (t) t.textContent = '展开';
    }
    _agentTrail = null;
    if (ev.answer) {
      const m = { id: Store.uid(), role: 'assistant', content: ev.answer, ts: Date.now(), status: 'done' };
      Store.addMessage(chat.conv.id, m);
      body.appendChild(chat.renderMsg(m));
      chat.scrollBottom(true);
    }
    return true;
  }

  const trail = _ensureAgentTrail(body);
  if (type === 'agent_thinking') {
    trail.body.appendChild(_agentLine('思考中…', 'think'));
  } else if (type === 'agent_tool') {
    trail.body.appendChild(_agentLine('调用工具：' + (ev.tool || '') + ' ' + JSON.stringify(ev.args || {}), 'tool'));
  } else if (type === 'agent_result') {
    const r = ev.result || {};
    const txt = r.ok ? (r.result || '完成') : ('失败：' + (r.result || ''));
    trail.body.appendChild(_agentLine('结果：' + txt, r.ok ? 'result' : 'error'));
  } else if (type === 'agent_approval') {
    trail.body.appendChild(_renderAgentApproval(ev.approval || {}));
  }
  chat.scrollBottom(true);
  return false;
}

/* ============================================================
   AI 调用：直连模式 / 代理模式，统一解析 SSE 流
   ============================================================ */
async function streamAI({
  model, baseUrl, messages, key, mode, proxyUrl, visionKey, visionModel, offlineEnabled,
  proactiveInternal, skipUserPersist, internalUserPrompt,
}, onDelta, onMeta, onThinking, onAgentEvent) {
  const direct = mode === 'direct';
  const hasImage = messages.some((m) => m.image);
  let res;

  // 服务器/直连模式都可能送来 _meta 行（在流末尾），用于告知"是否被截断"（finish_reason: length / 没收到 [DONE]）
  const fireMeta = (m) => { if (onMeta) { try { onMeta(m); } catch (_) { /* 不阻塞流 */ } } };

  // 直连模式：自己收集 finish_reason / sawDone / sawAnyData（因为没有服务器的 _meta 行）
  const directMeta = { sawAnyData: false, sawDone: false, finish_reason: null };

  // 超时保护：空闲 90 秒（不再收到数据块）才中止；持续出字永不掐断
  const controller = new AbortController();
  let firstByte = false;
  let timer = setTimeout(() => controller.abort(), 60000); // 首字前 60s 还没响应才放弃
  const refreshTimer = () => {
    clearTimeout(timer);
    // 拿到首字后放宽到 180s 空闲超时：只要模型还在持续出字就不会断
    timer = setTimeout(() => controller.abort(), firstByte ? 180000 : 60000);
  };
  const clearTimer = () => clearTimeout(timer);
  const timeoutErr = () => new Error('响应超时（模型思考太久或网络问题），已自动停止，请重试');

  try {
    if (direct) {
      if (hasImage) {
        throw new Error('图片消息需要「代理模式」（服务器转发给视觉模型）。请到设置页切换，或取消图片后发送。');
      }
      if (!key) throw new Error('直连模式需要 API Key：请在设置页填写，或在该伴侣的「AI 大脑」里填写');
      try {
        const url = (baseUrl || 'https://api.deepseek.com').replace(/\/+$/, '') + '/chat/completions';
        res = await fetch(url, {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + key,
          },
          body: JSON.stringify({ model, messages, stream: true, temperature: 0.7, max_tokens: 1024 }),
        });
      } catch (e) {
        if (e && e.name === 'AbortError') throw timeoutErr();
        throw new Error('直连 AI 服务失败（可能是浏览器跨域限制）。请到设置页切换为「代理模式」，或检查网络。');
      }
    } else {
      const base = (proxyUrl || '').trim().replace(/\/+$/, '');
      const url = base ? base + '/api/chat' : 'api/chat';
      try {
        res = await fetch(url, {
          method: 'POST',
          signal: controller.signal,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            model, baseUrl, messages,
            key: key || undefined,
            visionKey: visionKey || undefined,
            visionModel: visionModel || undefined,
            // ★ session_id 读 ws_client onopen 写入的全局（localStorage 持久化同一来源）
            session_id: (typeof Chat !== 'undefined' && Chat.sessionId)
              ? Chat.sessionId
              : (localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default'),
            character_name: (Chat.contact && Chat.contact.name) ? Chat.contact.name : undefined,
            // ★ 表情包开关：后端据此决定是否注入表情包列表到 system prompt
            sticker: !!(Chat.contact && Chat.contact.sticker),
            // ★ 括号动作描写开关：后端据此把 prompt 从"可用括号写动作神态"改成硬禁止，
            //   并在出口强剥所有括号。之前前端从不传，后端永远按角色卡的 true 走，
            //   于是前端说"禁止"、后端说"鼓励"，AI 收到矛盾指令自然还是会写括号。
            action_brackets: (Chat.contact && Chat.contact.actions === false) ? false : undefined,
            // ★ 离线状态：undefined=读角色卡（用户实时聊天）；false=明确不延迟（主动/定时消息）
            offline_enabled: offlineEnabled,
            proactive_internal: proactiveInternal,
            skip_user_persist: skipUserPersist,
            internal_user_prompt: internalUserPrompt,
          }),
        });
      } catch (e) {
        if (e && e.name === 'AbortError') throw timeoutErr();
        throw new Error('无法连接聊天服务器（' + url + '）。请确认电脑端服务器已启动、手机和电脑在同一网络。');
      }
    }

    if (!res.ok) {
      let msg = '请求失败 (' + res.status + ')';
      try {
        const j = await res.json();
        if (j && j.error && j.error.message) msg = j.error.message;
      } catch (_) { /* 忽略 */ }
      throw new Error(msg);
    }

    // ★ 离线状态：后端返回 X-Offline-Pending 头，说明角色暂时不在 → 延迟回复
    if (res.headers.get('X-Offline-Pending')) {
      let info = { eta: 0, hint: '' };
      try { info = await res.json(); } catch (_) {}
      const err = new Error('__OFFLINE_PENDING__');
      err.offline = info;
      throw err;
    }

    // 解析 SSE：buf 累积原始字节，找 \n 切完整行；切在 data 行中间时该行留在 buf，
    // 等下一个 chunk 接上即完整解析，无需额外的"pending"跨行串接（之前那版会把多行 payload
    // 串到一起，导致解析失败后永远累积、后续所有 delta 被吞掉，表现为卡在某个字符不再出字）
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      let chunk;
      try {
        chunk = await reader.read();
      } catch (e) {
        if (e && e.name === 'AbortError') throw timeoutErr();
        throw e;
      }
      const { done, value } = chunk;
      if (done) break;
      firstByte = true;
      refreshTimer(); // 收到数据块 → 重置空闲计时，只要还在出字就永不掐断
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (payload === '[DONE]') { directMeta.sawDone = true; continue; }
        try {
          const j = JSON.parse(payload);
          if (j && j.error) {
            const apiMessage = (j.error && j.error.message) || '模型接口返回错误';
            directMeta.sawAnyData = true;
            onDelta('模型接口错误：' + apiMessage);
            continue;
          }
          // 服务器侧（代理模式）会在流末尾追加一条 _meta:true 的诊断行；
          // 直连模式没有这层，由上游的 [DONE] 自然结束；finish_reason 在每个 chunk 的 choices[0] 里
          if (j && j._meta) {
            // ★ 彩蛋：身份坦诚回答前，先放 BGM（Letting Go 前奏 3 秒）
            if (j.play_bgm) playIdentityBgm();
            fireMeta(j);
            continue;
          }
          // ★ Agent 助手模式：后端推送执行轨迹事件（思考/工具/授权/结果/最终答案）
          if (j && j.type && j.type.indexOf('agent_') === 0) {
            directMeta.sawAnyData = true;
            if (onAgentEvent) { try { onAgentEvent(j); } catch (_) {} }
            continue;
          }
          // ★ 本地大脑提示（如自动回退云端）：toast 告知，不混进对话内容
          if (j && j.type === 'notice') {
            directMeta.sawAnyData = true;
            try { window.dispatchEvent(new CustomEvent('brain-notice', { detail: j.message || '' })); } catch (_) {}
            continue;
          }
          const c0 = j.choices && j.choices[0];
          if (c0 && c0.finish_reason) directMeta.finish_reason = c0.finish_reason;
          const reasoning = (c0 && c0.delta && c0.delta.reasoning_content) || '';
          if (reasoning) {
            directMeta.sawAnyData = true;
            if (onThinking) { try { onThinking(reasoning); } catch (_) {} }
          }
          const delta = (c0 && c0.delta && c0.delta.content) || '';
          if (delta) { directMeta.sawAnyData = true; onDelta(delta); }
        } catch (_) { /* data 行不完整（chunk 切在 JSON 中间），留待下次 chunk 接上 */ }
      }
    }
    // 流结束时解析残留缓冲（最后一行若缺换行符被截断）
    if (buf.trim()) {
      const tail = buf.trim();
      if (tail.startsWith('data:')) {
        const payload = tail.slice(5).trim();
        if (payload !== '[DONE]') {
          try {
            const j = JSON.parse(payload);
            if (j && j.error) {
              const apiMessage = (j.error && j.error.message) || '模型接口返回错误';
              directMeta.sawAnyData = true;
              onDelta('模型接口错误：' + apiMessage);
            } else if (j && j._meta) { fireMeta(j); }
            else if (j && j.type && j.type.indexOf('agent_') === 0) {
              directMeta.sawAnyData = true;
              if (onAgentEvent) { try { onAgentEvent(j); } catch (_) {} }
            }
            else if (j && j.type === 'notice') {
              directMeta.sawAnyData = true;
              try { window.dispatchEvent(new CustomEvent('brain-notice', { detail: j.message || '' })); } catch (_) {}
            }
            else {
              const c0 = j.choices && j.choices[0];
              if (c0 && c0.finish_reason) directMeta.finish_reason = c0.finish_reason;
              const reasoning = (c0 && c0.delta && c0.delta.reasoning_content) || '';
              if (reasoning) {
                directMeta.sawAnyData = true;
                if (onThinking) { try { onThinking(reasoning); } catch (_) {} }
              }
              const delta = (c0 && c0.delta && c0.delta.content) || '';
              if (delta) { directMeta.sawAnyData = true; onDelta(delta); }
            }
          } catch (_) { /* 忽略 */ }
        }
      }
    }
  } finally {
    clearTimer();
    // 直连模式：流结束后上报 meta（与代理模式 _meta 格式一致，下游 looksIncompleteTail 才能识别"自然 stop 但停在半句"）
    if (direct) fireMeta({ _meta: true, sawAnyData: directMeta.sawAnyData, sawDone: directMeta.sawDone, finish_reason: directMeta.finish_reason });
  }
}

/* ============================================================
   事件绑定
   ============================================================ */
function actionItem(icon, label, fn, danger) {
  const it = h('div', { class: 'action-item' + (danger ? ' danger' : '') },
    h('span', { class: 'action-icon', text: icon }),
    h('span', { text: label }),
  );
  it.addEventListener('click', fn);
  return it;
}

/* ============================================================
   工具：判断文本是否"看起来没收尾"——长度够 + 没以标点收尾
   用于检测 deepseek 自然 stop 但停在半句的情况（finish_reason='stop' 但没收尾）
   ============================================================ */
function looksIncompleteTail(text) {
  if (!text) return false;
  const t = String(text).trim();
  if (!t) return false;
  // 1-2 字纯语气词/称谓（"宝"、"嗯嗯"、"哦"）视为自然收尾，不触发
  if (t.length <= 3 && /^[宝就呢啊嘛呀哦嗯哈呵嘿啦吧噢呗咯嘞噻欸么耶哇呦]*$/.test(t)) return false;
  // 太短整体视为完整（少于 3 字）
  if (t.length < 3) return false;
  // 已正确收尾：句号/问号/感叹号/省略号/全角~ / 半角~ + 换行 + 关闭的括号引号
  if (/[。！？…～~\n\)」』"']$/.test(t)) return false;
  // ★ emoji 结尾（😭😢🥺😊😄😂 等）也视为自然收尾，不触发自动续写
  if (/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]$/u.test(t)) return false;
  return true;
}

/* ============================================================
   工具：计算回复延迟（多久之后才"开始"回复），按档位在范围内随机
   - instant 秒回 / short 1~3s / normal 3~8s / long 8~20s / custom 自定义范围
   - 消息越长，思考稍久一点（封顶 25 秒，避免让人等太久）
   ============================================================ */
function replyDelayMs(contact, textLen) {
  const d = (contact && contact.replyDelay) || 'normal';
  const L = Math.max(0, Number(textLen) || 0);
  let min = 3000, max = 8000;
  if (d === 'instant') { min = 0; max = 0; }
  else if (d === 'short') { min = 1000; max = 3000; }
  else if (d === 'long') { min = 8000; max = 20000; }
  else if (d === 'custom') {
    min = (Math.max(0, Number(contact.replyDelayMin) || 0)) * 1000;
    max = Math.max(min, (Number(contact.replyDelayMax) || 0) * 1000);
  }
  let ms = min + Math.random() * Math.max(0, max - min);
  ms = Math.min(ms + Math.min(2000, L * 8), 25000);
  return Math.round(ms);
}

/* ============================================================
   按 AI 输出字数计算"打字/思考"时序——比 replyDelayMs 更细分：
   ① preTypeMs：流结束 → 第一段开始打字之间的"想清楚再开打"停顿
   ② segGapMs：段间停顿，跟上一段字数成正比（说越多，下一段想更久）
   ③ pickUpMs：每段开始打字前的"提笔"小停顿，模拟人手犹豫
   设计目标：
   - 短回复 → 所有停顿都短 → 整体秒回感强
   - 长回复/多分段 → 停顿对应拉长 → 看起来像真在边想边打
   ============================================================ */
function preTypeMs(contentLen) {
  const L = Math.max(0, Number(contentLen) || 0);
  if (L === 0) return 600;
  if (L < 20) return 350 + Math.random() * 450;     // 极短："嗯""好"——350-800ms
  if (L < 60) return 600 + Math.random() * 700;     // 短回复：600-1300ms
  if (L < 150) return 900 + Math.random() * 900;    // 中回复：900-1800ms
  return 1200 + Math.random() * 1300;                // 长回复：1.2-2.5s
}
function segGapMs(prevSegLen) {
  // 上一段说完了，下一段"想说什么"——
  // 上一段越长，往往意味着接下来的内容也复杂，停顿略长一点
  const L = Math.max(0, Number(prevSegLen) || 0);
  if (L === 0) return 600;
  if (L < 15) return 400 + Math.random() * 500;     // 短段后：400-900ms
  if (L < 40) return 650 + Math.random() * 800;     // 中段后：650-1450ms
  return 1000 + Math.random() * 1100;                // 长段后：1.0-2.1s
}
function pickUpMs(segLen) {
  // 每段开始打字前的"提笔"小停顿（人手犹豫），短段犹豫短、长段犹豫长
  const L = Math.max(0, Number(segLen) || 0);
  if (L === 0) return 150;
  if (L < 25) return 120 + Math.random() * 180;     // 短段：120-300ms
  if (L < 60) return 200 + Math.random() * 300;     // 中段：200-500ms
  return 350 + Math.random() * 400;                  // 长段：350-750ms
}

/* ============================================================
   工具：把 AI 输出按句末标点切成 N 条独立消息（N 在 [minSeg, maxSeg] 随机）
   - 太短或句子太少不切
   - 每段都是完整句子（按句末标点切，绝不半句）
   - 平均分配句子，段数不超过句子总数
   - 末段单字语气词/称谓并回上一段
   ============================================================ */
function splitIntoSegments(text, minSeg, maxSeg) {
  if (!text) return [''];
  const lo = Math.max(1, Math.round(Number(minSeg) || 1));
  const hi = Math.max(lo, Math.round(Number(maxSeg) || lo));
  if (text.length < 15) return [text];

  // 第一步：按换行符优先切分（真人发微信经常换行）
  let parts = text.split(/\n+/).map(s => s.trim()).filter(Boolean);

  // 第二步：对每段再按句末标点（。！？!?）切分
  let sentences = [];
  for (const p of parts) {
    const re = /[^。！？!?]+[。！？!?]?/g;
    const ms = (p.match(re) || [p]).map(s => s.trim()).filter(Boolean);
    sentences = sentences.concat(ms);
  }

  // 第三步：如果句子数太少但文本较长，按逗号/分号再切
  if (sentences.length <= 1 && text.length > 30) {
    const re2 = /[^，,；;]+[，,；;]?/g;
    sentences = (text.match(re2) || [text]).map(s => s.trim()).filter(Boolean);
  }

  // 第四步：如果还是只有一段但文本很长（>50字），按长度强制切
  if (sentences.length <= 1 && text.length > 50) {
    const mid = Math.floor(text.length / 2);
    // 在中间附近找逗号或空格切
    let cut = mid;
    for (let i = mid; i < text.length && i < mid + 15; i++) {
      if ('，, 。'.includes(text[i])) { cut = i + 1; break; }
    }
    sentences = [text.slice(0, cut).trim(), text.slice(cut).trim()].filter(Boolean);
  }

  if (sentences.length <= 1) return [text];

  // 目标段数：范围内随机，但不超过句子数
  const target = lo + Math.floor(Math.random() * (hi - lo + 1));
  // ★ 换行分隔的多句（真人连发，如身份坦诚回答）→ 拆成所有句（每句一个气泡）
  const n = (parts.length >= 3)
    ? Math.min(sentences.length, 8)
    : Math.max(1, Math.min(target, sentences.length));
  if (n <= 1) return [text];

  // 平均分配句子到 n 段
  const segs = [];
  const per = Math.floor(sentences.length / n);
  let extra = sentences.length % n;
  let idx = 0;
  for (let i = 0; i < n; i++) {
    let cnt = per + (extra > 0 ? 1 : 0);
    if (extra > 0) extra--;
    segs.push(sentences.slice(idx, idx + cnt).join(''));
    idx += cnt;
  }
  // 末段过滤：把单字/双字语气词/称谓 并回上一段
  if (segs.length >= 2) {
    const tail = segs[segs.length - 1].trim();
    if (tail.length <= 3 && /^[宝就呢啊嘛呀哦嗯哈呵嘿啦吧噢呗咯嘞噻欸么耶]{1,3}$/.test(tail)) {
      segs[segs.length - 2] += segs[segs.length - 1];
      segs.pop();
    }
  }
  // ★ 过滤纯省略号/纯标点的沉默段（AI 情绪低落时可能输出"……"），
  //   否则会冒出「AI 发三个点」的空气泡。
  return segs.filter((s) => s.trim() && (typeof isSilenceText !== 'function' || !isSilenceText(s)));
}

/* ============================================================
   对话后异步抽取：把"用户新事实/偏好/事件"自动写进 memStore
   - 用同一个 AI 通道（fetch 直调），专门 prompt 抽取
   - 失败/网络问题静默,不影响主聊天
   - 每会话最多 12 秒,避免长时间挂起
   - 队列串行化：同一 contact 多次连续抽取排队，避免竞态——保证记忆按用户消息顺序写
   - 调用方 await：让 userProfile 在用户下一条消息到来前就写好，根治"上一句教他下一句就忘"
   ============================================================ */
/* ---------- 强制记忆抽取的判据与上限（单一来源：public/js/internal-msg-core.js） ----------
 * ★ 2026-09-16：旧代码在 Chat.send 里内联了一条过宽正则（`我的.{1,8}是` 等），
 *   把「我的想法是这样」「我的意思是」当事实并强制抽取（force 还绕过长度的门禁）。
 *   判据现在收敛到核心模块，这里只负责"取核心 + 记账"。
 */
const _forceExtractLog = new Map();   // contact.id → 最近强制抽取时间戳数组（窗口内次数上限用）

function _internalCore() {
  if (typeof window !== 'undefined' && window.InternalMsgCore) return window.InternalMsgCore;
  if (typeof globalThis !== 'undefined' && globalThis.InternalMsgCore) return globalThis.InternalMsgCore;
  return null;
}

/** 核心模块缺失时**不猜关键词**：force=false（本轮仍会做非强制抽取，只是不绕过长度门）。 */
function _decideForceExtract(input) {
  const core = _internalCore();
  if (!core) {
    return {
      force: false,
      reason: 'core-missing',
      next: (Number(input && input.roundsSinceExtract) || 0) + 1,
      recentForceTs: [],
    };
  }
  return core.decideForceExtract(input);
}

const _extractQueues = new Map();   // contact.id → 上一个抽取的 Promise
async function extractNewMemory(contact, convId, userText, assistantText, force) {
  if (!contact || !convId) return { total: 0 };
  // ★ 串行调度：把这次工作接到 contact 队列末尾 → 同一 contact 多次抽取严格按用户消息顺序写
  // （不串行化的 bug：用户快速连发 2 条消息，第二个 send 的 prompt 可能比第一个 send 的抽取更早完成，
  //   导致 userProfile 写入顺序错乱，第二个抽取的反思甚至被第一个覆盖）
  const prev = _extractQueues.get(contact.id) || Promise.resolve();
  const job = prev.catch(() => null).then(() => _doExtract(contact, convId, userText, assistantText, force));
  _extractQueues.set(contact.id, job.catch(() => ({ total: 0 })));
  return job;
}

// ★ 把抽取失败原因写到 memStore（用户能在记忆库页看到）+ console.warn（开发者工具可见）
// —— 之前 try/catch 失败完全吞掉，是"记忆库一直不更新"的根本原因
function _writeExtractDebug(contact, info) {
  try { console.warn('[extractNewMemory]', info); } catch (_) {}
  if (!contact) return;
  try {
    const stamp = new Date().toLocaleString('zh-CN', { hour12: false });
    const list = (contact.memStore || []).slice();
    // 同一分钟内只写一条（避免刷屏）
    const lastIsDebug = list[0] && list[0].source === 'debug-extract';
    const sameMin = lastIsDebug && (Date.now() - (list[0].ts || 0) < 60000);
    if (!sameMin) {
      list.unshift({
        id: Store.uid(),
        title: '⚠️ 抽取失败 ' + stamp,
        content: info,
        source: 'debug-extract',
        date: stamp,
        ts: Date.now(),
        enabled: false,   // ★ 禁用：不计入 learnedCount，不被 autoSummarizeMemory 总结
      });
      Store.updateContact(contact.id, { memStore: list });
    }
  } catch (e) {
    try { console.error('[extractNewMemory] debug write failed:', e); } catch (_) {}
  }
}

async function _doExtract(contact, convId, userText, assistantText, force) {
  // force=true 时（4-6 轮触发或"记住X"指令）放宽 userText 长度过滤；force=false 时太短输入不抽
  if (!userText) return { total: 0 };
  if (!force && userText.length < 2) return { total: 0 };
  if (userText.length > 600) userText = userText.slice(0, 600);
  if (assistantText && assistantText.length > 600) assistantText = assistantText.slice(0, 600);
  const settings = Store.getSettings();
  const brain = brainConfig(contact, settings);
  // ★ 称呼统一：把抽取的事实里"用户"字样替换为 AI 对 TA 的具体称呼——这样写到记忆库不人机感
  const callName = (contact.callsYou || '').trim() || 'TA';
  const sys = '你是"用户观察员 + 记忆提取器"两用的 AI。从下面这一对 用户/AI 的对话中，抽出两类信息，只输出严格 JSON：\n' +
    '【称呼规则（硬性）】在你抽取的每一条事实/偏好/事件/心情/反思中，**不要使用"用户"这种泛称**，而要用 AI 对 TA 的称呼「' + callName + '」（具体称呼来自角色设定，如「宝」「亲爱的」「小明」）。\n' +
    '示例：不要写「用户叫小明」/「用户的偏好是喝奶茶」，要写「' + callName + '叫小明」/「' + callName + '的偏好是喝奶茶」。这样存在记忆库里不像机器记录。\n' +
    '\n' +
    '【A 类：零散事实（写入记忆存储文件夹）】\n' +
    '{"facts":["' + callName + '的事实/背景/身份/工作/生活"],"prefs":["' + callName + '的偏好/喜好/厌恶/习惯"],' +
    '"events":["近期发生的事件/约定/计划/特殊日子"],"feelings":["' + callName + '当下流露的情感/状态"]}\n' +
    '【B 类：反思沉淀（写入' + callName + '核心档案）】——这是关键，**主动思考用户的话背后意味着什么**:\n' +
    '{"reflections":["' + callName + '这句话背后的潜台词/隐含需求/自我透露的情绪变化/未来可能的偏好/对 TA 性格/习惯的新理解","例子1：『今天好累啊』=可能不只是陈述、也可能是希望被关心 → 你理解了『' + callName + '累的时候偏好得到关心的回应』","例子2：『最近换了发型/衣服/洗发水』=用户在意外观/有新的生活偏好 → 你理解了『' + callName + '最近在变美』或『' + callName + '换了洗发水不喜欢有香味』","例子3：『我也不知道』连续出现 → 用户可能迷茫、不确定自己想要什么 → 你理解了『' + callName + '在 XX 情况下需要更具体的引导而不是开放问题』"]}\n' +
    '★【硬性要求】★\n' +
    '1) **【"记住…"类指令 + 身份状态必须捕获】**：' + callName + '只要说"记住X/我叫X/我过敏X/我喜欢X/我不喜欢X/我的X是Y"这类**主动明确告诉 TA 的事实**，**必须**原样进 facts 桶（用「' + callName + '」做主语，不要用"用户"），示例："记住我叫小明"→facts=["' + callName + '叫小明"]；"我过敏青霉素"→facts=["' + callName + '对青霉素过敏"]；"我最近在减肥"→facts=["' + callName + '最近在减肥"]。\n' +
    '   ★【否定性状态/身份同样重要】：' + callName + '说"我不上班""我是学生""我不抽烟""我不喝酒""我不吃辣""我早睡了""我没工作"这类**否定性身份/状态**，也**必须**进 facts 桶（这是 TA 是谁的一部分，以后要避免问和它冲突的问题），示例："我不上班"→facts=["' + callName + '不上班"]；"我是学生"→facts=["' + callName + '是学生"]；"我不吃辣"→prefs=["' + callName + '不吃辣"]。\n' +
    '2) **【A 类严格按事实抽、不要脑补】**；**B 类放宽去思考**（reflections 是你对用户的理解、可以是推断）。A 类每条 ≤ 30 字，B 类每条 ≤ 50 字。\n' +
    '3) **【反幻觉铁律】**reflection 必须基于本轮对话**明确说出**的内容来推断，不允许编造用户没说过的话；没把握宁可少写一条，不要硬凑。\n' +
    '4) **【每次至少给一些反应】**——用户主动回复必有原因（情绪/事件/偏好变化/抱怨），不要输出纯空数组；A 类可以为空（如果只是打招呼），但 B 类**只要有任何情绪/偏好/事件的迹象，就要写至少 1 条 reflection**。\n' +
    '5) **【偏好/禁忌优先】**：用户说喜欢/讨厌/习惯/不想——即使没说"记住"也要进 prefs（重要偏好长期影响对话）。\n' +
    '6) **只输出 JSON**，不要任何解释/标点/代码块。\n' +
    '7) **【自动提炼 → 自动写入长期记忆】**：从你提取的 facts/prefs/events 中，**自动选 1~2 条最重要的**也写进 B 类 reflections（格式："用户【某某事】，需在之后对话自然运用"）——因为只有进核心档案，下次 AI 对话 prompt 头部才会读到。**这是 AI"主动记忆"的核心契约**：用户不需要下指令"记住它"，你自己就要从他的话里识别关键事实并保存。\n' +
    '8) **【4-6 轮触发 + 闲聊过滤 + 去重】**：\n' +
    '   · **触发**：① 本轮出现"记住X/我叫X/我过敏X/我喜欢X/我不喜欢X/我的X是Y"——**立刻写**；② 累计 4-6 轮无重要新信息——**强制写一次**（整合之前漏掉的）；③ 出现重要新信息（身份/重大事件/明确偏好）——立即写。\n' +
    '   · **闲聊过滤**：纯打招呼（"你好""在吗""晚安"）、无信息量反应（"哈哈""嗯嗯""知道了"）、重复以前说过的事——**不写**。\n' +
    '   · **去重**：事实已在 A/B 类出现过就**不要重复记录**；只在新事实/补充/旧事实更新时写。\n' +
    '   · **每条一句**：≤ 30 字；只写关于用户的客观事实，**不要写系统规则、不要写人设、不要写"AI 角色扮演"这类元话语**。\n' +
    '9) **【用户指令直通】**：用户输入"显示全部记忆" → 返回 {"cmd":"show_memory"}；输入"清空全部长期记忆库" → 返回 {"cmd":"clear_memory"}；其他情况照常抽取。';
  // force=true 时给 LLM 提示"这是 4-6 轮强制整合触发或用户明确指令，请不要因为本轮闲聊就不写——必须把之前漏掉的关键事实整合写一次"
  const forceHint = force ? '\n\n【触发模式 = 强制】本轮为"累计 4-6 轮强制整合"或"用户明确指令"，**不论本轮内容是什么**，请回顾本对话中可能漏掉的关键事实（用户的偏好、身份、事件、约定），整合后写入；不要因为本轮是闲聊就返回空。' : '';
  const usr = '用户说：' + userText + (assistantText ? '\nAI答：' + assistantText : '') + forceHint;
  let out = '';
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 12000);
    const isDirect = settings.mode === 'direct';
    let url, body;
    if (isDirect) {
      const base = (brain.baseUrl || 'https://api.deepseek.com').replace(/\/+$/, '');
      url = base + '/chat/completions';
      body = { model: bgModel(brain.model), messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }], stream: false, temperature: 0.3 };
    } else {
      url = '/api/chat';
      // ★ 代理模式只传角色级 model（2026-09-11）：记忆提炼模型由后端按角色卡决定
      body = {
        model: ((contact && contact.model) || '').trim(), baseUrl: brain.baseUrl,
        messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }],
        key: brain.key || undefined, stream: false, temperature: 0.3,
        // ★ P0-2：以前这一支不带 session，后端回退到进程级单例 active_session()，
        //   多标签并发时会为「别的标签正在用的角色」创建调度器实例（串会话）。
        session_id: (window.Session && Session.getSessionId)
          ? Session.getSessionId()
          : (localStorage.getItem('ai_companion_session_id')
             || localStorage.getItem('session_id') || 'default'),
        character_name: (contact && (contact.name || contact.id)) || undefined,
      };
    }
    const headers = { 'Content-Type': 'application/json' };
    if (isDirect && brain.key) headers['Authorization'] = 'Bearer ' + brain.key;
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) return { total: 0 };
    const j = await res.json();
    out = (j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || '';
  } catch (_) { return { total: 0 }; }
  if (!out) return { total: 0 };

  // ★★★【关键修复】JSON 解析容错增强 ★★★
  // 之前 try/catch 解析失败直接 return {total:0} 静默离开，导致"记忆库一直不更新"——用户根本看不到
  // 现在的策略：先去代码块包裹 → 找最大 {…} → 多 fallback → 失败时**写一条 debug 记忆**让用户能在记忆库页看到
  let data = null;
  let parseError = null;
  try {
    // 1. 去 ```json / ``` 等代码块包裹（容错多种变体）
    let cleaned = out
      .replace(/```\w*\s*\n?/g, '')   // 去 ```json / ```JS / ``` 等所有代码块开头
      .replace(/\n?```\s*$/g, '')     // 去末尾 ```
      .trim();
    // 2. 找最大 {…} 块（第一个 { 到最后一个 }）
    const start = cleaned.indexOf('{');
    const lastClose = cleaned.lastIndexOf('}');
    if (start >= 0 && lastClose > start) {
      const candidate = cleaned.slice(start, lastClose + 1);
      data = JSON.parse(candidate);
    } else {
      data = JSON.parse(cleaned);
    }
  } catch (e) {
    parseError = (e && e.message) || 'JSON parse error';
  }
  if (!data || typeof data !== 'object') {
    // 失败时写 debug 记忆（用户能在记忆库页看到）+ console.warn
    _writeExtractDebug(contact, 'JSON 解析失败：' + parseError + ' | 原文本前 300 字：' + out.slice(0, 300));
    return { total: 0, error: parseError || 'parse_failed' };
  }

  // —— B 类：反思沉淀到 settings.userProfile ——
  let reflectCount = 0;
  if (Array.isArray(data.reflections) && data.reflections.length) {
    const newReflect = data.reflections
      .filter((x) => typeof x === 'string' && x.trim())
      .map((s) => s.trim().slice(0, 100));
    if (newReflect.length) {
      const cur = (Store.getSettings().userProfile || '').trim();
      const today = new Date();
      const stamp = (today.getMonth() + 1) + '/' + today.getDate();
      const appended = newReflect.map((s) => '· 〔' + stamp + '反思〕' + s).join('\n');
      const next = cur ? (cur + '\n' + appended) : appended;
      // ★ 兜底：save() 内部吞掉异常（QuotaExceededError 只 warn 不抛），
      //   所以这里不能靠 try/catch —— 那条分支永远不会执行。
      //   旧的兜底写 wb_settings / wb_data，那是遗留项目的 key，Store 只读
      //   'aiwechat:v9'，写进去永远读不回来，等于假兜底。
      //   改为检查 save 结果，失败时直写正确的 key，再用 Store.reload() 同步内存，
      //   否则下一次 save() 会拿旧内存覆盖回去。
      if (!Store.saveSettings({ userProfile: next, profileUpdated: new Date().toISOString() })) {
        try {
          console.warn('[extractNewMemory] saveSettings 未落盘，直写 aiwechat:v9');
          const raw = localStorage.getItem('aiwechat:v9');
          const obj = raw ? JSON.parse(raw) : {};
          if (!obj.settings || typeof obj.settings !== 'object') obj.settings = {};
          obj.settings.userProfile = next;
          obj.settings.profileUpdated = new Date().toISOString();
          localStorage.setItem('aiwechat:v9', JSON.stringify(obj));
          Store.reload();
        } catch (_) { _writeExtractDebug(contact, 'B 类写入失败（本地存储不可用/已满）'); }
      }
      reflectCount = newReflect.length;
    }
  }

  // —— A 类：事实/prefs/events/feelings 进 memStore ——
  const today = new Date();
  const dateLabel = (today.getMonth() + 1) + '/' + today.getDate();
  const list = (contact.memStore || []).slice();
  const bucketLabels = { facts: '关于用户', prefs: '用户偏好', events: '近期事件', feelings: '近期心情' };
  let added = 0;
  const addedKeys = [];
  for (const k of Object.keys(bucketLabels)) {
    const arr = Array.isArray(data[k]) ? data[k].filter((x) => typeof x === 'string' && x.trim()) : [];
    if (!arr.length) continue;
    const joined = arr.slice(0, 5).map((s) => s.trim().slice(0, 60)).join(' / ');
    if (!joined) continue;
    list.unshift({
      id: Store.uid(),
      title: bucketLabels[k] + '（' + dateLabel + '）',
      content: joined,
      source: 'auto-extract',
      date: dateLabel,
      ts: Date.now(),
      enabled: true,
    });
    added++;
    addedKeys.push(k);
    if (added >= 2) break;
  }
  if (added > 0) {
    // ★ 兜底：updateContact 内部不抛异常，改用 Store.lastSaveOk() 判断是否落盘。
    //   旧兜底写 wb_data（遗留 key，Store 读不到），这里直写 'aiwechat:v9' + reload 同步。
    Store.updateContact(contact.id, { memStore: list });
    if (!Store.lastSaveOk()) {
      try {
        console.warn('[extractNewMemory] updateContact 未落盘，直写 aiwechat:v9');
        const raw = localStorage.getItem('aiwechat:v9');
        const obj = raw ? JSON.parse(raw) : {};
        const cIdx = (obj.contacts || []).findIndex((c) => c.id === contact.id);
        if (cIdx >= 0) {
          obj.contacts[cIdx].memStore = list;
          localStorage.setItem('aiwechat:v9', JSON.stringify(obj));
          Store.reload();
        }
      } catch (_) { _writeExtractDebug(contact, 'A 类写入失败（本地存储不可用/已满）'); }
    }
  }
  return {
    total: added + reflectCount,
    facts: addedKeys.filter((k) => k === 'facts').length,
    reflections: reflectCount,
  };
}

/* ============================================================
   对话历史自动压缩：旧消息 → 记忆摘要（AI 记得更久，更像人）
   ============================================================ */
async function maybeCompressHistory(contact, convId) {
  const msgs = Store.getMessages(convId);
  if (msgs.length < 140) return;
  const from = Number(contact.summarizedUpTo) || 0;
  if (msgs.length - from < 120) return;
  const to = msgs.length - 20; // 保留最近 20 条完整对话
  if (to <= from) return;
  const slice = msgs.slice(from, to).filter((m) => m.status !== 'error' && m.status !== 'recalled');
  if (slice.length < 20) return;
  const transcript = slice.slice(-100)
    .map((m) => (m.role === 'user' ? '用户：' : 'TA：') + (m.content || (m.image ? '[图片]' : '')))
    .join('\n');
  const settings = Store.getSettings();
  const brain = brainConfig(contact, settings);
  const sys = '把下面的对话历史压缩成一段 200 字以内的记忆摘要，保留：关键事实、关系进展、约定、用户喜好与在意的事。用第一人称（你是 TA）。只输出摘要，不要多余内容。';
  let out = '';
  try {
    await streamAI({
      // ★ 代理模式只传角色级 model（2026-09-11）：摘要模型由后端按角色卡决定
      model: ((contact && contact.model) || '').trim(), baseUrl: brain.baseUrl,
      messages: [{ role: 'system', content: sys }, { role: 'user', content: transcript }],
      key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
      // 记忆总结是内部任务，绝不能把 transcript 当作真实用户消息写回聊天历史。
      proactiveInternal: true, skipUserPersist: true, internalUserPrompt: true,
    }, (d) => { out += d; });
  } catch (_) { return; }
  out = out.trim();
  if (!out) return;
  const entry = {
    id: Store.uid(), title: '对话回顾 ' + dateStrOf(slice[0].ts) + ' 起',
    content: out, source: 'auto', date: dateStrOf(Date.now()), ts: Date.now(), enabled: true,
  };
  const list = (contact.memStore || []).slice();
  list.unshift(entry);
  Store.updateContact(contact.id, { summarizedUpTo: to, memStore: list });
}

/* ============================================================
   AI 自主总结记忆：零散抽取/记住的记忆积累到一定量后，
   通读总结成一份「关于你」的画像，写入核心记忆 + 人格记忆库。
   → 聊得越多，画像越完整，AI 越懂你、越真实。
   → 用户可在「记忆存储」里查看历史总结（source: auto-summary）。
   触发条件（避免频繁消耗 token）：
   - "学到的"记忆 ≥ 6 条（自动抽取 / 聊天记住 / 对话回顾）
   - 距上次总结 ≥ 3 小时，且新增 ≥ 3 条
   ============================================================ */
async function autoSummarizeMemory(contact, convId) {
  if (!contact || !convId) return false;
  const memItems = (contact.memStore || []).filter((e) => e.enabled !== false);
  // 只总结"学到的"记忆（自动抽取/聊天记住/对话回顾），排除"AI 总结"本身，避免重复总结自己
  const learned = memItems.filter((e) => /^(auto-extract|chat|chat-snippet|auto)$/.test(e.source || ''));
  // 触发放宽：从 ≥6 → ≥3，让聊了 3、5 轮也能自动总结；用户立刻看到"AI 在整理记忆"的工作
  if (learned.length < 3) return false;
  const lastAt = Number(contact.lastSummarizeAt) || 0;
  const now = Date.now();
  if (lastAt && now - lastAt < 2 * 3600 * 1000) return false;   // 距上次总结至少 2 小时（原来 3 小时）
  const newSince = learned.filter((e) => (Number(e.ts) || 0) > lastAt);
  if (lastAt && newSince.length < 2) return false;   // 新增 ≥2 条（原来 3）

  const settings = Store.getSettings();
  const brain = brainConfig(contact, settings);
  const summary = learned.slice(0, 30)
    .map((e) => (e.title ? e.title + '：' : '') + String(e.content || '').slice(0, 300))
    .join('\n');
  const callName = (contact.callsYou || '').trim() || 'TA';
  const sys = '你是"记忆总结器"。下面是关于 ' + callName + ' 的一条条零散记忆，请通读后总结成一份「关于 ' + callName + '」的画像，用第一人称（你是 TA 的 AI 伴侣），分 6~12 点，每点一句话，总共 300 字以内。**不要用"用户"这种泛称**，直接用「' + callName + '」称呼。**只写下面素材里明确有、确定的真实信息，绝对不要脑补、推测或编造用户没说过/没做过的事**；记忆太少就少写几点，不确定的宁可不写。只输出画像正文，不要标题、不要"关于你"这类开头，不要解释。';
  let out = '';
  try {
    await streamAI({
      model: bgModel(brain.model), baseUrl: brain.baseUrl,
      messages: [{ role: 'system', content: sys }, { role: 'user', content: summary }],
      key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
      // AI 画像总结同样是内部任务，不得落成用户气泡或参与正常对话活动计时。
      proactiveInternal: true, skipUserPersist: true, internalUserPrompt: true,
    }, (d) => { out += d; });
  } catch (_) { return; }
  out = out.trim();
  if (!out || out.length < 10) return;
  // 写入核心记忆（contact.memory）+ 存一条"AI 总结"到人格记忆库（用户可查看历史）
  const entry = {
    id: Store.uid(),
    title: 'AI 总结 · 关于你（' + dateStrOf(Date.now()) + '）',
    content: out,
    source: 'auto-summary',
    date: dateStrOf(Date.now()),
    ts: now,
    enabled: true,
  };
  const list = (contact.memStore || []).slice();
  list.unshift(entry);

  // 同步追加到 settings.userProfile（用户全局核心档案）+ 标注本次总结时间
  // 不覆盖旧的"反思"条目，AI 自动总结拼在前面
  const curProfile = (Store.getSettings().userProfile || '').trim();
  const stamp = (now.getMonth() + 1) + '/' + now.getDate();
  const profileAppend = '【AI 自动总结 ' + stamp + '】\n' + out;
  const nextProfile = curProfile ? (profileAppend + '\n\n' + curProfile) : profileAppend;

  Store.updateContact(contact.id, { memStore: list, memory: out, lastSummarizeAt: now });
  Store.saveSettings({ userProfile: nextProfile, profileUpdated: new Date().toISOString() });
  return true;  // 让调用方 toast "✨ AI 已经把记忆整理成'关于你'的画像"
}

function bindChatEvents() {
  const ta = $('#chat-input');
  const sendBtn = $('#chat-send');

  ta.addEventListener('input', () => { Chat.setSendState(); Chat.autosize(); });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      Chat.send();
    }
  });
  sendBtn.addEventListener('click', () => Chat.send());

  // ===== 👁 看看：让 AI 看屏幕（Q3 手动触发） =====
  if (!document.getElementById('chat-look')) {
    const lookBtn = document.createElement('button');
    lookBtn.type = 'button';
    lookBtn.id = 'chat-look';
    lookBtn.title = '让 AI 看看你在干嘛';
    lookBtn.innerHTML = '<svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.8\" stroke-linecap=\"round\" style=\"width:14px;height:14px\"><path d=\"M2.5 12S6 5.8 12 5.8 21.5 12 21.5 12 18 18.2 12 18.2 2.5 12 2.5 12z\"/><circle cx=\"12\" cy=\"12\" r=\"2.8\"/></svg><span>看看屏幕</span>';
    lookBtn.style.cssText = 'background:var(--card,#fff);border:1px solid var(--border,#EBEBE8);border-radius:999px;padding:7px 13px;display:inline-flex;align-items:center;gap:6px;color:var(--text-sub,#8a8a86);font-size:11.5px;align-self:flex-end;line-height:1;cursor:pointer;font-family:inherit;';

    // 轻量加载态：返回带 .remove() 的元素，供按钮点击时显示/移除
    function _showLoading(text) {
      const el = document.createElement('div');
      el.className = 'vc-loading-tip';
      el.textContent = text || '加载中…';
      el.style.cssText = 'position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);'
        + 'background:rgba(0,0,0,0.72);color:#fff;padding:8px 16px;border-radius:10px;'
        + 'font-size:14px;z-index:9999;pointer-events:none;';
      document.body.appendChild(el);
      return el;
    }

    lookBtn.addEventListener('click', async () => {
      const spinner = _showLoading('让我看看…');
      try {
        const resp = await fetch('/api/screen/look', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: window.Session?.getSessionId?.() || 'default',
            character_id: (Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default',
            character_name: (Chat.contact && Chat.contact.name) || undefined,
          })
        });
        const data = await resp.json();
        spinner?.remove();

        if (data.ok) {
          // ★ 用角色语气把屏幕描述转成自然对话，不是硬编码引导语
          const summary   = data.summary   || '在做点什么';
          const topic     = data.topic_hint || '';
          const activity  = data.activity  || 'other';

          // 根据活动类型生成不同风格的引导语
          const guide_map = {
            coding:    ['哦～在写代码呀', '在敲代码呢', '又在写程序啦'],
            video:     ['在看视频呀', '在看什么呢～', '哇在看视频'],
            gaming:    ['在打游戏！', '哦打游戏中', '哇在玩游戏'],
            browsing:  ['在刷网页呢', '在看什么呢', '又在摸鱼了啦'],
            idle:      ['屏幕待机了', '在想什么呢', '发呆中？'],
            other:     ['在看什么呢', '在干啥呀～', '让我看看…'],
          };
          const guides = guide_map[activity] || guide_map.other;
          let guide = guides[Math.floor(Math.random() * guides.length)];

          // 角色化追加：有 topic_hint 就追问一句
          if (topic && topic !== summary) {
            guide += `，${topic}`;
          }
          guide += '呀～';

          await Chat?.send(guide);
        } else {
          toast('没看清楚，再试一次？');
        }
      } catch (e) {
        spinner?.remove();
        toast('查看失败');
      }
    });
    const bar = document.getElementById('chat-inputbar');
    if (bar) bar.appendChild(lookBtn);
  }

  $('#chat-back').addEventListener('click', () => Chat.close());

  // ★ 头部 📚 按钮：跳转到「记忆库」页（不再注入 AI 消息影响观感）
  $('#chat-mem').addEventListener('click', (ev) => {
    ev.stopPropagation();
    if (!Chat.contact) { toast('先选个伴侣'); return; }
    MemStore.open(Chat.contact.id);
  });

  // ★ 2026-09-17 头部 🎓 按钮：「她学到了什么 / 她整理了什么（可撤回）」。
  //   为什么放在聊天头上：用户对"她到底有没有在学"的疑问，是在聊天现场产生的；
  //   入口离得远就等于没有。
  const _learnBtn = $('#chat-learn');
  if (_learnBtn) {
    _learnBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (!Chat.contact) { toast('先选个伴侣'); return; }
      if (window.Learn) Learn.open(Chat.contact.id);
      else toast('学习档案模块未加载');
    });
  }

  // 点顶部名字 → 资料与设置
  $('#chat-header-center').addEventListener('click', () => {
    if (Chat.contact) Profile.open(Chat.contact.id);
  });
  $('#chat-title').style.cursor = 'pointer';

  $('#chat-more').addEventListener('click', () => {
    if (!Chat.contact) return;
    const c = Chat.contact;
    const sheet = h('div', { class: 'action-sheet' },
      h('div', { class: 'sheet-title', text: c.name }),
      actionItem('👤', '资料与设置', () => { Sheet.close(); Profile.open(c.id); }),
      actionItem('✏️', '编辑资料（快速）', () => { Sheet.close(); openContactSheet(c.id); }),
      actionItem('🗑️', '清空聊天记录', () => {
        Sheet.close();
        if (confirm('确定清空与「' + c.name + '」的聊天记录？')) {
          Store.clearConversationMessages(Chat.conv.id);
          Chat.open(c.id);
          toast('已清空');
        }
      }),
      actionItem('🚪', '删除这个会话', () => {
        Sheet.close();
        if (confirm('删除与「' + c.name + '」的会话？联系人保留。')) {
          Store.deleteConversation(Chat.conv.id);
          Chat.close();
          renderChatList();
          toast('会话已删除');
        }
      }),
      actionItem('💥', '删除联系人', () => {
        Sheet.close();
        if (confirm('确定删除联系人「' + c.name + '」？')) {
          Store.deleteContact(c.id);
          Chat.close();
          renderContacts();
          renderChatList();
          toast('已删除');
        }
      }, true),
    );
    Sheet.open(sheet, 'action-sheet');
  });

  /* 语音输入（iOS Safari 原生语音识别） */
  const micBtn = $('#chat-mic');
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    micBtn.style.display = 'none';
  } else {
    let rec = null, active = false;
    micBtn.addEventListener('click', () => {
      if (active) { try { rec.stop(); } catch (_) { /* 忽略 */ } return; }
      rec = new SR();
      rec.lang = 'zh-CN';
      rec.interimResults = true;
      rec.continuous = false;
      rec.onresult = (e) => {
        let t = '';
        for (let i = e.resultIndex; i < e.results.length; i++) t += e.results[i][0].transcript;
        ta.value = t;
        Chat.setSendState();
      };
      rec.onend = () => { active = false; micBtn.classList.remove('active'); rec = null; };
      rec.onerror = (e) => {
        active = false;
        micBtn.classList.remove('active');
        if (e.error === 'not-allowed') toast('麦克风权限被拒绝，请在系统设置中允许');
        else if (e.error === 'no-speech') toast('没有听到声音');
      };
      try {
        rec.start();
        active = true;
        micBtn.classList.add('active');
        toast('请说话…');
      } catch (_) { /* 忽略 */ }
    });
  }

  /* 发送图片 */
  const imgBtn = $('#chat-image');
  const imgInput = $('#chat-image-input');
  imgBtn.addEventListener('click', () => imgInput.click());
  imgInput.addEventListener('change', () => {
    const f = imgInput.files[0];
    if (!f) return;
    if (f.size > 8 * 1024 * 1024) { toast('图片太大（限 8MB）'); imgInput.value = ''; return; }
    fileToDataUrl(f, 1280, (url) => {
      if (!url) { toast('图片读取失败'); }
      else {
        Chat.pendingImage = url;
        Chat.setSendState();
        toast('已选图片，可加文字一起发送');
      }
    });
    imgInput.value = '';
  });
}

bindChatEvents();

/* ---------------- 图片全屏预览（lightbox） ---------------- */
// ★ UI 打磨 v1：点击图片气泡全屏查看，Esc/点外部关闭，右键不冒泡方便保存
function _showImageLightbox(src) {
  if (document.getElementById('_img-lightbox')) return;

  const overlay = document.createElement('div');
  overlay.id = '_img-lightbox';
  overlay.style.cssText = [
    'position:fixed;inset:0;z-index:9999;',
    'background:rgba(0,0,0,0.88);',
    'display:flex;align-items:center;justify-content:center;',
    'cursor:zoom-out;',
    'animation:_lbIn 0.18s ease;',
  ].join('');

  const img = document.createElement('img');
  img.src = src;
  img.style.cssText = [
    'max-width:92vw;max-height:88vh;',
    'border-radius:8px;',
    'box-shadow:0 8px 40px rgba(0,0,0,0.6);',
    'object-fit:contain;',
    'cursor:default;',
  ].join('');

  // 点击图片本身不关闭
  img.addEventListener('click', (e) => e.stopPropagation());
  // 右键不冒泡（方便保存）
  img.addEventListener('contextmenu', (e) => e.stopPropagation());

  const close = () => {
    overlay.style.animation = '_lbOut 0.15s ease forwards';
    setTimeout(() => {
      if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }, 150);
    document.removeEventListener('keydown', onKey);
  };

  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', close);

  overlay.appendChild(img);
  document.body.appendChild(overlay);
}

// ★ 修复：Chat 是顶层 const，不会自动挂到 window；显式挂载，
//   否则 app.js/voice_call.js/incoming_call.js 里的 `window.Chat && Chat.contact` 恒为 undefined，
//   导致通知去重失效、手动拨打 character_id 退化、音色克隆取不到联系人。
window.Chat = Chat;

/* ---------- 彩蛋：身份坦诚回答前放 BGM（Letting Go 前奏完整播放） ---------- */
function playIdentityBgm() {
  try {
    const audio = new Audio('assets/bgm_letting_go.mp3');
    audio.volume = 0.7;
    const p = audio.play();
    if (p && p.catch) p.catch(() => {});
    // 完整播放，不截断
  } catch (_) {}
}

