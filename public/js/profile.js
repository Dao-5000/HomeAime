'use strict';
/* ============================================================
   伴侣资料与设置页：所有细粒度设置（iOS 风格分组卡片，改动即保存）
   ============================================================ */

const Profile = {
  contactId: null,

  open(contactId) {
    this.contactId = contactId;
    // ★ 2026-09-14 新增：渲染前先把**角色卡字段回填到本联系人**。
    //   为什么需要：人格设置页里读的是 Store 联系人（c.xxx），但像
    //   activeHours / dndAllowGreetings / deep_thinking / understandingModel
    //   这些字段只存在后端角色卡里 —— Store 里从来没有，于是：
    //     · 控件显示为空/关（看着像"没保存"）
    //     · 更严重的是 active_hours 为空 → 后端 resolve_active_hours 拿不到
    //       → 主动消息时段限制失效
    //   这里只在字段"本地为空"时回填，不覆盖用户本地已改的值。
    this._hydrateFromCard();
    this.render();
    $('#profile-page').classList.add('open');
  },

  /** 从后端角色卡拉取字段并回填到 Store 联系人（异步、静默、失败不影响渲染） */
  async _hydrateFromCard() {
    try {
      const c = this.c;
      if (!c) return;
      const name = c.name || c.id;
      if (!name) return;
      const res = await fetch('/api/pc/character/get?name=' + encodeURIComponent(name));
      if (!res.ok) return;
      const card = await res.json();
      if (!card || card._missing) return;
      const patch = {};
      const put = (localKey, cardVal) => {
        const cur = c[localKey];
        const empty = (cur === undefined || cur === null || cur === '' || cur === false);
        if (empty && cardVal !== undefined && cardVal !== null && cardVal !== '') patch[localKey] = cardVal;
      };
      // 主动消息时段（本次重点）
      put('activeHours', card.active_hours);
      if (card.dnd_allow_greetings !== undefined && c.dndAllowGreetings === undefined) {
        patch.dndAllowGreetings = card.dnd_allow_greetings === true;
      }
      // ★ 2026-09-16：「主动回复由模型自主」开关（角色卡为权威，打开人格页时回填）
      if (card.proactive_model_decides !== undefined && c.proactiveModelDecides === undefined) {
        patch.proactiveModelDecides = card.proactive_model_decides === true;
      }
      // 顺带修掉同类"存了但读不回"的字段
      if (card.deep_thinking !== undefined && !c.deep_thinking) patch.deep_thinking = !!card.deep_thinking;
      if (card.autonomy !== undefined && !c.autonomy) patch.autonomy = card.autonomy || '';
      put('understandingModel', card.understanding_model);
      put('memoryModel', card.memory_model);
      put('model', card.model);
      put('aiProvider', card.ai_provider);
      put('aiBase', card.ai_base);
      if (Object.keys(patch).length) {
        Store.updateContact(this.contactId, patch);
        // 已渲染时刷新一次，让控件显示正确值
        if ($('#profile-body') && $('#profile-page').classList.contains('open')) this.render();
      }
    } catch (_) { /* 静默：回填失败不影响人格页使用 */ }
  },

  close() {
    $('#profile-page').classList.remove('open');
    // ★ 2026-09-16：停掉主动消息状态面板的自动刷新定时器（页面关了就不该再抓）
    try { if (window.__engStatusTimer) { clearInterval(window.__engStatusTimer); window.__engStatusTimer = null; } } catch (_) {}
    renderContacts();
    renderChatList();
  },

  get c() { return Store.getContact(this.contactId); },
  save(patch) { if (this.c) Store.updateContact(this.contactId, patch); },

  /* ---------------- 渲染 ---------------- */
  render() {
    const c = this.c;
    if (!c) { this.close(); return; }
    const body = $('#profile-body');
    body.innerHTML = '';
    let personaSyncTimer = null;
    // ★ 2026-09-14：把主动消息时段字段加入白名单 + 同步载荷。
    //   这两个字段原先**都不在白名单里**，所以前端设了也存不进角色卡 →
    //   后端 scheduler/idle_agent 永远读到空 → "主动消息时间范围限制不管用"。
    const personaFields = new Set(['name', 'relation', 'callsYou', 'selfRef', 'traits', 'hobbies', 'background', 'style', 'customStyle', 'avatarUrl', 'deep_thinking', 'autonomy', 'show_thinking', 'couple_mode', 'couple_prompt', 'model', 'understandingModel', 'memoryModel', 'aiProvider', 'aiBase', 'aiKey', 'activeHours', 'dndAllowGreetings', 'proactiveModelDecides']);
    // ★ 2026-09-14：proactiveMaxMin **已从身份字段里移除**。
    //   主动消息间隔统一以全局设置（主动发言间隔）为准，人格设置里那个控件已删。
    //   保留在数组里会带来风险：save() 会因此触发角色卡同步，而同步载荷里
    //   Number(undefined) || 0 = 0 → 后端把"上限 0"当成"不主动"，
    //   等于**用户什么都没做却把主动消息关掉了**。
    const syncPersonaCard = () => {
      clearTimeout(personaSyncTimer);
      personaSyncTimer = setTimeout(async () => {
        const latest = this.c;
        if (!latest) return;
        try {
          const res = await fetch('/api/pc/character/save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              character_name: latest.name,
              self_name: latest.selfRef || latest.name,
              call_user: latest.callsYou || '你',
              personality: latest.traits || '温柔体贴，回复自然，记得用户说过的话。',
              relationship: latest.relation || '',
              hobbies: latest.hobbies || '',
              worldview: latest.background || '',
              speaking_style: [latest.style || '', latest.customStyle || ''].filter(Boolean).join('；'),
              avatar: latest.avatarUrl || '',
              deep_thinking: !!latest.deep_thinking,
              // ★ 情侣模式：持久化到后端角色卡，开启后注入 COUPLE_PROMPT
              couple_mode: !!latest.couple_mode,
              couple_prompt: latest.couple_prompt || '',
              // ★ 单角色大脑：持久化到后端角色卡，让离线回复/主动消息/QQ 机器人也能用
              model: latest.model || '',
              // ★ 分层大脑（2026-09-11）：理解层/记忆提炼模型也归人格设置（角色卡）
              understanding_model: latest.understandingModel || '',
              memory_model: latest.memoryModel || '',
              ai_provider: latest.aiProvider || '',
              ai_base: latest.aiBase || '',
              ai_key: latest.aiKey || '',
              // ★ 2026-09-14：主动消息时段（角色卡为唯一权威，后端 resolve_active_hours 读它）
              active_hours: latest.activeHours || '',
              // 时段内是否允许早晚安穿透（默认否）
              dnd_allow_greetings: latest.dndAllowGreetings === true,
              // ★ 2026-09-16：主动回复是否由模型自主（跳过 90–120 分钟节奏机；
              //   免打扰/可用时段/每日上限仍由后端 decide() 把关）
              proactive_model_decides: latest.proactiveModelDecides === true,
              // ★ 2026-09-14：主动消息间隔**不再**同步到角色卡。
              //   间隔以全局设置为唯一来源（主动发言间隔），此处刻意不下发，
              //   避免"字段为空 → 写成 0 → 后端当成不主动"把功能关掉。
              persona_configured: true,
            }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok || !data.ok) throw new Error(data?.error?.message || '人格同步失败');
        } catch (e) {
          toast(e.message || '人格同步失败');
        }
      }, 350);
    };
    const save = (patch) => {
      this.save(patch);
      if (Object.keys(patch || {}).some((key) => personaFields.has(key))) syncPersonaCard();
    };

    /* ---- 顶部头像卡 ---- */
    const heroName = h('div', { class: 'p-hero-name', text: c.name });
    const heroSub = h('div', { class: 'p-hero-sub' });
    const refreshSub = () => {
      const bits = [];
      if (c.relation) bits.push(relationLabel(c.relation) + (c.relationLevel ? ' · ' + closenessLabel(c.relationLevel) : ''));
      if (c.traits) bits.push(c.traits);
      if (c.callsYou) bits.push('叫你「' + c.callsYou + '」');
      heroSub.innerHTML = '';
      heroSub.appendChild(h('span', { class: 'online-dot' }));
      heroSub.appendChild(document.createTextNode(' 在线 · ' + (bits.join(' · ') || 'AI 伴侣')));
    };
    refreshSub();
    const hero = h('div', { class: 'p-hero' },
      avatarEl(c, 'lg'), heroName, heroSub,
    );

    /* ================= 基本资料 ================= */
    const baseCard = h('div', { class: 'p-card' });

    const nameInput = h('input', { type: 'text', value: c.name });
    nameInput.addEventListener('change', () => {
      const v = nameInput.value.trim();
      if (!v) { toast('名字不能为空'); nameInput.value = c.name; return; }
      save({ name: v });
      heroName.textContent = v;
      refreshSub();
      toast('已保存');
    });
    baseCard.appendChild(pRow('名字', nameInput));

    /* 头像（照片） */
    const avBox = h('div', { class: 'p-avbox' });
    if (!c.avatarUrl && c.avatar) c.avatarUrl = c.avatar;
    const refreshAv = () => {
      avBox.innerHTML = '';
      if (c.avatarUrl) avBox.appendChild(h('img', { src: c.avatarUrl, alt: '' }));
      else {
        const letter = h('div', { class: 'p-av-letter', text: c.name.charAt(0) });
        letter.style.background = 'hsl(' + avatarHue(c.name) + ', 55%, 58%)';
        avBox.appendChild(letter);
      }
      avBox.appendChild(h('button', { class: 'p-av-btn', text: c.avatarUrl ? '更换' : '上传' }));
      avBox.appendChild(h('button', { class: 'p-av-btn', text: '清除' }));
      const btns = avBox.querySelectorAll('.p-av-btn');
      btns[0].addEventListener('click', () => avInput.click());
      btns[1].addEventListener('click', () => {
        if (!c.avatarUrl) return;
        save({ avatarUrl: '' });
        refreshAv();
      });
    };
    refreshAv();
    const avInput = h('input', { type: 'file', accept: 'image/*', hidden: true });
    avInput.addEventListener('change', () => {
      const f = avInput.files[0];
      if (!f) return;
      fileToDataUrl(f, 128, (url) => {
        if (!url) { toast('图片读取失败'); return; }
        save({ avatarUrl: url });
        refreshAv();
        hero.firstChild.replaceWith(avatarEl(c, 'lg'));
        toast('已保存头像');
      });
      avInput.value = '';
    });
    const avWrap = h('div', {});
    avWrap.appendChild(avBox);
    avWrap.appendChild(avInput);
    baseCard.appendChild(pRow('头像', avWrap));

    /* 关系 + 亲密度 */
    const relSelect = h('select', {}, h('option', { value: 'other', text: '（不指定）' }));
    for (const [k, def] of Object.entries(RELATIONS)) {
      if (k === 'other') continue;
      relSelect.appendChild(h('option', { value: k, text: relationLabel(k) }));
    }
    relSelect.value = c.relation || 'other';
    relSelect.addEventListener('change', () => {
      save({ relation: relSelect.value === 'other' ? '' : relSelect.value });
      refreshRelText();
      refreshSub();
      toast('已保存');
    });

    const relSlider = h('input', { type: 'range', min: '1', max: '10', step: '1', value: String(c.relationLevel || 5) });
    const relText = h('div', { class: 'level-num' });
    const refreshRelText = () => { relText.textContent = closenessLabel(Number(relSlider.value)); };
    relSlider.addEventListener('input', () => {
      const v = Number(relSlider.value);
      save({ relationLevel: v });
      refreshRelText();
      refreshSub();
      // ★ 新增：同步后端亲密度
    });
    refreshRelText();

    baseCard.appendChild(pRow('和 TA 的关系', relSelect));
    const relWrap = h('div', { class: 'p-slider-block' });
    relWrap.appendChild(relSlider);
    relWrap.appendChild(relText);
    relWrap.title = '角色的人设关系定位，与动态亲密度分开保存';
    baseCard.appendChild(pRow('亲密度（陌生人→恋人）', relWrap));

    const callsInput = h('input', { type: 'text', placeholder: '如：亲爱的、宝、小名', value: c.callsYou || '' });
    callsInput.addEventListener('change', () => {
      save({ callsYou: callsInput.value.trim() });
      refreshSub();
      toast('已保存');
    });
    baseCard.appendChild(pRow('TA 怎么称呼你', callsInput));

    const selfRefSelect = h('select', {});
    for (const s of SELF_REFS) selfRefSelect.appendChild(h('option', { value: s, text: s }));
    const curSelf = c.selfRef || '我';
    selfRefSelect.value = SELF_REFS.includes(curSelf) ? curSelf : '自定义…';
    const selfRefInput = h('input', { type: 'text', placeholder: '自定义自称', hidden: true, value: SELF_REFS.includes(curSelf) ? '' : curSelf });
    const refreshSelf = () => {
      selfRefInput.hidden = selfRefSelect.value !== '自定义…';
      if (selfRefSelect.value !== '自定义…') selfRefInput.value = '';
    };
    selfRefSelect.addEventListener('change', () => {
      refreshSelf();
      save({ selfRef: selfRefSelect.value === '自定义…' ? (selfRefInput.value.trim() || '我') : selfRefSelect.value });
    });
    selfRefInput.addEventListener('change', () => save({ selfRef: selfRefInput.value.trim() || '我' }));
    refreshSelf();
    const selfRefWrap = h('div', {});
    selfRefWrap.appendChild(selfRefSelect);
    const selfRefInputWrap = h('div', { style: 'margin-top:8px' });
    selfRefInputWrap.appendChild(selfRefInput);
    selfRefWrap.appendChild(selfRefInputWrap);
    baseCard.appendChild(pRow('TA 怎么自称', selfRefWrap));

    /* ================= 人设 ================= */
    const personaCard = h('div', { class: 'p-card' });
    personaCard.appendChild(h('div', { class: 'p-subtitle', text: '人设' }));

    const langSelect = h('select', {});
    for (const l of LANGUAGES) langSelect.appendChild(h('option', { value: l, text: l }));
    langSelect.value = c.language || '简体中文';
    langSelect.addEventListener('change', () => { save({ language: langSelect.value }); toast('已保存'); });
    personaCard.appendChild(pRow('回复语言', langSelect));

    /* ================= 音色设置 ================= */
    /* ── 音色设置卡片（第165行起，替换到原来的311行） ── */
    const voiceCard = h('div', { class: 'p-card' });
    voiceCard.appendChild(h('div', { class: 'p-subtitle', text: '🔊 语音音色' }));

    // 读当前已选 voice_key
    const _getVoiceKey = () =>
      (c.voice && typeof c.voice === 'object' ? c.voice.voice_key : c.voice) || '';

    // ── 已选展示（复用 p-hint 样式，和其他行一致）
    const selectedVoiceLabel = h('div', {
      class: 'p-hint',
      style: 'color:var(--accent);padding:2px 0 6px',
    });
    const _refreshLabel = () => {
      const vk = _getVoiceKey();
      selectedVoiceLabel.textContent = vk
        ? '当前：' + ((window._voiceLabelMap || {})[vk] || vk)
        : '默认（按角色性别自动推断）';
    };
    _refreshLabel();

    // ── 试听按钮（复用 chip 样式，和性格/风格 chips 一致）
    const previewBtn = h('button', { type: 'button', class: 'chip', text: '▶ 试听' });
    let _prevAudio = null;
    previewBtn.addEventListener('click', async () => {
      const vk = _getVoiceKey();
      if (!vk) { toast('请先选择一个音色'); return; }
      previewBtn.textContent = '生成中…';
      previewBtn.disabled = true;
      try {
        const res  = await fetch('/api/voice/preview', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({
            voice_key: vk,
            character_name: c.name,
            text: (c.name || 'AI') + '：你好，这是我的声音，好听吗？',
          }),
        });
        const data = await res.json();
        if (data.url) {
          if (_prevAudio) _prevAudio.pause();
          _prevAudio = new Audio(data.url);
          _prevAudio.play();
        } else {
          toast(data.error || '试听失败');
        }
      } catch {
        toast('试听失败，请确认后端已启动');
      } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = '▶ 试听';
      }
    });

    // ── 把已选展示 + 试听按钮合进一个 pRow（和"回复语言"那行风格完全一致）
    const voiceStatusWrap = h('div', { style: 'display:flex;align-items:center;gap:10px;width:100%' });
    voiceStatusWrap.appendChild(selectedVoiceLabel);
    voiceStatusWrap.appendChild(previewBtn);
    voiceCard.appendChild(pRow('当前音色', voiceStatusWrap));

    // ── 音色选择区（分组 chips，和"性格"/"说话风格"那两行完全一致）
    const voicePickerWrap = h('div', { style: 'width:100%' });
    voicePickerWrap.appendChild(h('div', { class: 'p-hint', text: '加载中…' }));
    voiceCard.appendChild(pRow('选择音色', voicePickerWrap, '点击切换，▶ 试听当前效果'));

    // Provider 中文标签
    const _PLABELS = {
      'edge-tts':   '免费 · Edge-TTS',
      'cosyvoice':  '本地 · CosyVoice3',
      'minimax':    '云端 · MiniMax',
      'xunfei':     '云端 · 讯飞',
      'volcengine': '云端 · 火山引擎',
      'azure':      '云端 · Azure',
      'clone':      '我的克隆音色',
    };

    // ── 异步加载音色列表（克隆成功后也会复用刷新）
    const loadVoiceList = async () => {
      try {
        const res    = await fetch('/api/voices?ts=' + Date.now(), { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const voices = await res.json();
        const currentProfiles = Object.assign({}, c.voiceProfiles || c.voice_profiles || {});
        const isClonedVoice = (v) => {
          const tags = Array.isArray(v.tags) ? v.tags.map(x => String(x)) : [];
          return String(v.key || '').startsWith('clone_')
            || String(v.key || '').startsWith('custom_')
            || !!v.is_clone
            || tags.includes('克隆')
            || tags.includes('自定义');
        };

        // key→label 映射，供试听展示用
        window._voiceLabelMap = {};
        for (const v of voices) {
          const engine = v.clone_engine_label || (v.provider === 'cosyvoice' ? 'CosyVoice3' : '');
          window._voiceLabelMap[v.key] = isClonedVoice(v) && engine
            ? ((v.label || v.key) + ' · ' + engine)
            : (v.label || v.key);
        }
        _refreshLabel();

        // 按 provider 分组
        const groups = {};
        for (const v of voices) {
          const p = isClonedVoice(v) ? 'clone' : (v.provider || 'other');
          (groups[p] = groups[p] || []).push(v);
        }
        const order = ['clone', 'cosyvoice', 'edge-tts', 'minimax', 'xunfei', 'volcengine', 'azure', 'other'];

        voicePickerWrap.innerHTML = '';
        const curVk = _getVoiceKey();

        for (const provider of order.filter(p => groups[p] && groups[p].length)) {
          const group = groups[provider];
          // 分组标题（复用 p-hint 样式）
          voicePickerWrap.appendChild(h('div', {
            class: 'p-hint',
            style: 'padding:8px 0 4px;font-weight:600;color:var(--text-2)',
            text:  provider === 'clone' ? '我的克隆音色（本地 · CosyVoice3 / 其他引擎）' : (_PLABELS[provider] || provider),
          }));

          // chips 行（和"性格 chips"完全一致的 p-chips + chip 组合）
          const chips = h('div', { class: 'p-chips' });
          for (const v of group) {
            const engine = v.clone_engine_label || (v.provider === 'cosyvoice' ? 'CosyVoice3' : '');
            const btn = h('button', {
              type:  'button',
              class: 'chip' + (curVk === v.key ? ' selected' : ''),
              text:  isClonedVoice(v) && engine ? ((v.label || v.key) + ' · ' + engine) : (v.label || v.key),
              title: (v.tags || []).join(' · '),
            });
            btn.addEventListener('click', () => {
              voicePickerWrap.querySelectorAll('.chip.selected')
                .forEach(b => b.classList.remove('selected'));
              btn.classList.add('selected');
              save({ voice: { voice_key: v.key, ...v } });
              _refreshLabel();
              toast('音色已切换：' + (v.label || v.key));
              _syncVoiceToBackend(c.id, c.name, v.key, currentProfiles, c.voiceStyle || 'auto');
            });
            chips.appendChild(btn);
          }
          voicePickerWrap.appendChild(chips);
        }

        const profileLabels = { call: '实时通话', tender: '温柔亲密', bright: '开心活泼', comfort: '安慰关心', calm: '冷静克制' };
        const multiWrap = h('div', { style: 'display:grid;gap:8px;width:100%' });
        Object.entries(profileLabels).forEach(([slot, label]) => {
          const select = h('select', {});
          select.appendChild(h('option', { value: '', text: '跟随主音色' }));
          voices.forEach(v => select.appendChild(h('option', { value: v.key, text: (v.label || v.key) })));
          const saved = currentProfiles[slot];
          select.value = typeof saved === 'string' ? saved : ((saved || {}).voice_key || '');
          select.addEventListener('change', () => {
            if (select.value) currentProfiles[slot] = select.value;
            else delete currentProfiles[slot];
            save({ voiceProfiles: Object.assign({}, currentProfiles) });
            _syncVoiceToBackend(c.id, c.name, _getVoiceKey(), currentProfiles, c.voiceStyle || 'auto');
            toast(label + '声线已保存');
          });
          multiWrap.appendChild(pRow(label, select));
        });
        voicePickerWrap.appendChild(h('div', { class: 'p-hint', style: 'padding:14px 0 4px;font-weight:600;color:var(--text-2)', text: '情境声线 · 一个角色可配置多套声音' }));
        voicePickerWrap.appendChild(multiWrap);

        const styleSelect = h('select', {});
        [['auto','自动匹配性格'],['gentle','温柔气声'],['bright','活泼亮声'],['deep','低沉松弛'],['cool','清冷克制'],['natural','自然口语']].forEach(([value, text]) => styleSelect.appendChild(h('option', { value, text })));
        styleSelect.value = c.voiceStyle || 'auto';
        styleSelect.addEventListener('change', () => {
          save({ voiceStyle: styleSelect.value });
          _syncVoiceToBackend(c.id, c.name, _getVoiceKey(), currentProfiles, styleSelect.value);
          toast('声音性格已保存');
        });
        voicePickerWrap.appendChild(pRow('声音性格', styleSelect, '自动根据人设调整语速、音高和情感指令'));

        // 克隆音色入口（一个虚线 chip，风格克制）
        voicePickerWrap.appendChild(h('div', {
          class: 'p-hint',
          style: 'padding:8px 0 4px;font-weight:600;color:var(--text-2)',
          text:  '我的克隆音色',
        }));
        const cloneChips = h('div', { class: 'p-chips' });
        const cloneBtn   = h('button', {
          type:  'button',
          class: 'chip',
          style: 'border-style:dashed;color:var(--accent)',
          text:  '+ 上传参考音频',
        });
        cloneBtn.addEventListener('click', () => {
          if (typeof VoiceClonePanel !== 'undefined') {
            VoiceClonePanel.open(c.id);
          } else {
            toast('请在「设置」→「语音克隆」里上传参考音频');
          }
        });
        cloneChips.appendChild(cloneBtn);
        voicePickerWrap.appendChild(cloneChips);

        // ★ 通话记录入口（查看 AI 通话历史）
        const historyBtn = h('button', {
          type: 'button', class: 'chip',
          style: 'margin-top:8px',
          text: '📞 查看通话记录'
        });
        historyBtn.addEventListener('click', () => {
          if (typeof CallHistoryPanel !== 'undefined') {
            CallHistoryPanel.open(c.id, c.name);
          } else {
            toast('通话记录组件未加载');
          }
        });
        voicePickerWrap.appendChild(h('div', {
          class: 'p-hint',
          style: 'padding:8px 0 4px;font-weight:600;color:var(--text-2)',
          text: '通话'
        }));
        voicePickerWrap.appendChild(historyBtn);

      } catch (e) {
        voicePickerWrap.innerHTML = '';
        voicePickerWrap.appendChild(h('div', {
          class: 'p-hint',
          style: 'color:var(--text-3)',
          text:  '音色加载失败（' + e.message + '）',
        }));
      }
    };
    window.refreshVoiceList = loadVoiceList;
    loadVoiceList();
    /* ── 音色设置卡片 END ── */

    // 通话黏人特效开关（读/写 localStorage: setting_vc_effects，下次通话生效）
    const fxRow = h('label', { class: 'setting-row' },
      h('span', { text: '🎧 通话黏人特效（声波律动+气泡）' }),
      (function () {
        const inp = h('input', { type: 'checkbox', id: 'set-vc-fx' });
        inp.checked = localStorage.getItem('setting_vc_effects') !== 'off';
        inp.addEventListener('change', function (e) {
          localStorage.setItem('setting_vc_effects', e.target.checked ? 'on' : 'off');
          if (typeof toast === 'function') toast('设置已存，下次通话生效哦~');
        });
        return inp;
      })()
    );
    voiceCard.appendChild(fxRow);

    /* 性格 chips + 自定义 */
    const traitSet = new Set((c.traits || '').split(/[、,，]/).map((s) => s.trim()).filter(Boolean));
    const traitChips = h('div', { class: 'p-chips' });
    for (const t of TRAITS) {
      const b = h('button', { type: 'button', class: 'chip' + (traitSet.has(t) ? ' selected' : ''), text: t });
      b.addEventListener('click', () => {
        if (traitSet.has(t)) { traitSet.delete(t); b.classList.remove('selected'); }
        else { traitSet.add(t); b.classList.add('selected'); }
        save({ traits: Array.from(traitSet).join('、') });
        refreshSub();
      });
      traitChips.appendChild(b);
    }
    // ★ 渲染"自定义"性格 chip：从 c.traits 里挑出不在 TRAITS 列表的部分，已保存的也作为 selected chip 重新挂上
    // （解决"自定义加进去退回去就消失"的体验 bug）
    for (const t of Array.from(traitSet)) {
      if (TRAITS.indexOf(t) >= 0) continue;   // 预定义的上面已渲染
      const b = h('button', { type: 'button', class: 'chip selected', text: t });
      b.addEventListener('click', () => {
        traitSet.delete(t);
        b.remove();
        save({ traits: Array.from(traitSet).join('、') });
        refreshSub();
      });
      traitChips.appendChild(b);
    }
    const customTraitInput = h('input', { type: 'text', placeholder: '自定义性格，回车添加', value: '' });
    customTraitInput.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      const t = customTraitInput.value.trim();
      if (!t || traitSet.has(t)) return;
      traitSet.add(t);
      const b = h('button', { type: 'button', class: 'chip selected', text: t });
      b.addEventListener('click', () => { traitSet.delete(t); b.remove(); save({ traits: Array.from(traitSet).join('、') }); refreshSub(); });
      traitChips.appendChild(b);
      customTraitInput.value = '';
      save({ traits: Array.from(traitSet).join('、') });
      refreshSub();
    });
    const traitWrap = h('div', { style: 'width:100%' });
    traitWrap.appendChild(traitChips);
    const traitInputWrap = h('div', { style: 'margin-top:8px' });
    traitInputWrap.appendChild(customTraitInput);
    traitWrap.appendChild(traitInputWrap);
    personaCard.appendChild(pRow('性格', traitWrap));

    const hobbiesInput = h('textarea', { placeholder: 'TA 的爱好…' });
    hobbiesInput.value = c.hobbies || '';
    hobbiesInput.addEventListener('change', () => { save({ hobbies: hobbiesInput.value.trim() }); toast('已保存'); });
    personaCard.appendChild(pRow('爱好', hobbiesInput));

    const bgInput = h('textarea', { placeholder: 'TA 的背景故事…' });
    bgInput.value = c.background || '';
    bgInput.addEventListener('change', () => { save({ background: bgInput.value.trim() }); toast('已保存'); });
    personaCard.appendChild(pRow('背景故事', bgInput));

    /* 说话风格（可多选）+ 自定义 */
    const styleSet = new Set((c.style || '').split(',').map((s) => s.trim()).filter(Boolean));
    const styleChips = h('div', { class: 'p-chips' });
    for (const [k, def] of Object.entries(STYLES)) {
      const b = h('button', { type: 'button', class: 'chip' + (styleSet.has(k) ? ' selected' : ''), text: def.label });
      b.addEventListener('click', () => {
        if (styleSet.has(k)) { styleSet.delete(k); b.classList.remove('selected'); }
        else { styleSet.add(k); b.classList.add('selected'); }
        customStyleInput.hidden = !styleSet.has('custom');
        save({ style: Array.from(styleSet).join(',') });
      });
      styleChips.appendChild(b);
    }
    const customStyleInput = h('textarea', { placeholder: '自定义说话风格…', hidden: !styleSet.has('custom') });
    customStyleInput.value = c.customStyle || '';
    customStyleInput.addEventListener('change', () => { save({ customStyle: customStyleInput.value.trim() }); toast('已保存'); });
    const styleWrap = h('div', { style: 'width:100%' });
    styleWrap.appendChild(styleChips);
    const styleInputWrap = h('div', { style: 'margin-top:8px' });
    styleInputWrap.appendChild(customStyleInput);
    styleWrap.appendChild(styleInputWrap);
    personaCard.appendChild(pRow('说话风格', styleWrap));

    /* 聊天背景 */
    const wallBox = h('div', { class: 'p-wall' });
    const refreshWall = () => {
      wallBox.innerHTML = '';
      const defSw = h('button', { type: 'button', class: 'swatch' + (!c.wallpaper ? ' selected' : ''), text: '默' });
      defSw.style.fontSize = '11px'; defSw.style.color = '#888';
      defSw.addEventListener('click', () => { save({ wallpaper: '' }); refreshWall(); });
      wallBox.appendChild(defSw);
      for (const w of WALLPAPERS) {
        const s = h('button', { type: 'button', class: 'swatch' + (c.wallpaper === w ? ' selected' : '') });
        s.style.background = w;
        if (w === '#2F3542') s.style.border = '1px solid #ccc';
        s.addEventListener('click', () => { save({ wallpaper: w }); refreshWall(); });
        wallBox.appendChild(s);
      }
      if (c.wallpaper && c.wallpaper.startsWith('data:')) {
        const s = h('button', { type: 'button', class: 'swatch selected' });
        s.style.backgroundImage = 'url(' + c.wallpaper + ')';
        s.style.backgroundSize = 'cover';
        wallBox.appendChild(s);
      }
      const up = h('button', { type: 'button', class: 'chip', text: '上传照片' });
      up.addEventListener('click', () => wallInput.click());
      wallBox.appendChild(up);
    };
    refreshWall();
    const wallInput = h('input', { type: 'file', accept: 'image/*', hidden: true });
    wallInput.addEventListener('change', () => {
      const f = wallInput.files[0];
      if (!f) return;
      fileToDataUrl(f, 640, (url) => {
        if (!url) { toast('图片读取失败'); return; }
        save({ wallpaper: url });
        refreshWall();
        toast('已设置聊天背景');
      });
      wallInput.value = '';
    });
    const wallWrap = h('div', {});
    wallWrap.appendChild(wallBox);
    wallWrap.appendChild(wallInput);
    personaCard.appendChild(pRow('聊天背景', wallWrap));

    /* ================= AI 大脑（服务商 / 模型 / Key） ================= */
    const brainCard = h('div', { class: 'p-card' });
    brainCard.appendChild(h('div', { class: 'p-subtitle', text: 'AI 大脑' }));

    const provSelect = h('select', {});
    for (const [k, def] of Object.entries(AI_PROVIDERS)) provSelect.appendChild(h('option', { value: k, text: def.label }));
    provSelect.value = AI_PROVIDERS[c.aiProvider] ? c.aiProvider : 'deepseek';

    const modelSelect = h('select', {});
    const modelInput = h('input', { type: 'text', placeholder: '模型名称', hidden: true, value: c.model || '' });
    // key 按 provider 分开存（c.aiKeys）；回退旧字段 c.aiKey（DeepSeek 时代遗留）
    const _aiKeys = (c.aiKeys && typeof c.aiKeys === 'object') ? c.aiKeys : {};
    const keyInput = h('input', { type: 'text', placeholder: '该服务商的 API Key',
      value: _aiKeys[provSelect.value] || (provSelect.value === 'deepseek' ? (c.aiKey || '') : '') });
    const baseInput = h('input', { type: 'text', placeholder: '接口地址，如 http://127.0.0.1:8080/v1', value: c.aiBase || '' });
    const baseWrap = h('div', { style: 'margin-top:8px' }, baseInput);

    // ★ 统一模型数据源：从后端 textModels 拉（全局/人格同源），回退前端 AI_PROVIDERS.models
    let _textModelsCache = null;
    // ★ 前端 AI_PROVIDERS key → 后端 textModels 的 provider 值（方向：前→后）
    const PROVIDER_MAP = { deepseek: 'deepseek', qwen: 'dashscope', glm: 'zhipu', moonshot: 'moonshot', openai: 'openai', claude: 'anthropic', google: 'google', xai: 'xai' };

    const refreshBrain = () => {
      const def = AI_PROVIDERS[provSelect.value];
      const provKey = PROVIDER_MAP[provSelect.value] || provSelect.value;
      modelSelect.innerHTML = '';
      const backendModels = [];
      if (_textModelsCache) {
        for (const [key, info] of Object.entries(_textModelsCache)) {
          if ((info.provider || '') === provKey) backendModels.push({ key, name: info.name || key });
        }
      }
      if (backendModels.length) {
        modelSelect.hidden = false;
        modelInput.hidden = true;
        // ★ 加「跟随全局」空选项：不选具体模型 = 用全局设置页的大脑，避免两边重复设置
        modelSelect.appendChild(h('option', { value: '', text: '跟随全局（默认）' }));
        for (const m of backendModels) modelSelect.appendChild(h('option', { value: m.key, text: m.name }));
        modelSelect.value = (c.model && backendModels.some(m => m.key === c.model)) ? c.model : '';
      } else if (def.models && def.models.length) {
        modelSelect.hidden = false;
        modelInput.hidden = true;
        modelSelect.appendChild(h('option', { value: '', text: '跟随全局（默认）' }));
        for (const m of def.models) modelSelect.appendChild(h('option', { value: m, text: m }));
        modelSelect.value = (c.model && def.models.includes(c.model)) ? c.model : '';
      } else {
        modelSelect.hidden = true;
        modelInput.hidden = false;
      }
      baseWrap.style.display = (provSelect.value === 'custom' || provSelect.value === 'ollama' || provSelect.value === 'claude') ? '' : 'none';
      // ★ 切换服务商时，key 输入框切到该服务商已存的 key（避免还显示上一个服务商的 key）
      const _ks = (this.c && this.c.aiKeys) || {};
      keyInput.value = _ks[provSelect.value] || (provSelect.value === 'deepseek' ? (this.c.aiKey || '') : '') || '';
      keyInput.placeholder = provSelect.value === 'ollama'
        ? '本地模型通常无需 Key'
        : 'API Key（' + def.keyHint + '，留空用设置页默认）';
    };
    provSelect.addEventListener('change', () => {
      // ★ 2026-09-11：换服务商时，旧模型不属于新服务商 → 一并清掉。
      //   否则卡里留着旧服务商的模型（如智谱服务商配着 gemini 模型），生成层永远回不到新服务商。
      const _provKey = PROVIDER_MAP[provSelect.value] || provSelect.value;
      const _newDef = AI_PROVIDERS[provSelect.value];
      const _belongsTo = (c.model && (
        (_textModelsCache && Object.entries(_textModelsCache).some(([k, info]) =>
          k === c.model && (info.provider || '') === _provKey))
        || ((_newDef.models || []).indexOf(c.model) !== -1)));
      if (c.model && !_belongsTo) save({ model: '' });
      save({ aiProvider: provSelect.value });
      refreshBrain();
      toast('已切换服务商');
    });
    modelSelect.addEventListener('change', () => save({ model: modelSelect.value }));
    modelInput.addEventListener('change', () => save({ model: modelInput.value.trim() }));
    keyInput.addEventListener('change', () => {
      const k = keyInput.value.trim();
      const prov = provSelect.value;
      // ★ 按 provider 分开存 key
      const _ks = (this.c && this.c.aiKeys) ? { ...this.c.aiKeys } : {};
      _ks[prov] = k;
      save({ aiKeys: _ks });
      // 兼容旧字段：deepseek 的 key 仍同步一份到 aiKey，避免别处读到空
      if (prov === 'deepseek') save({ aiKey: k });
      // 同步全局后端 key：按当前服务商决定同步到哪个字段（GLM → zhipu_api_key，Claude → claude_api_key，其余 → api_key）
      const field = prov === 'glm' ? 'zhipu_api_key' : (prov === 'claude' ? 'claude_api_key' : 'api_key');
      fetch('/api/pc/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [field]: k }),
      }).catch(() => {});
      toast(k ? '已保存' : '已清空');
    });
    baseInput.addEventListener('change', () => { save({ aiBase: baseInput.value.trim() }); toast('已保存'); });
    refreshBrain();
    // 异步拉取后端 textModels，填充后刷新模型下拉（全局/人格同源）
    fetch('/api/pc/config').then(r => r.json()).then(cfg => {
      if (cfg.textModels && typeof cfg.textModels === 'object') {
        _textModelsCache = cfg.textModels;
        refreshBrain();
        // 分层大脑下拉同样吃后端模型池
        fillLayerSelect(umSelect, (this.c && this.c.understandingModel) || '');
        fillLayerSelect(mmSelect, (this.c && this.c.memoryModel) || '');
      }
    }).catch(() => {});

    brainCard.appendChild(pRow('AI 服务商', provSelect));
    const modelWrap = h('div', {});
    modelWrap.appendChild(modelSelect);
    modelWrap.appendChild(modelInput);
    brainCard.appendChild(pRow('模型', modelWrap));

    /* ★ 2026-09-13 移除「本地大脑」开关与本地模型选择。
       原因：实测本地小模型（qwen3-4b/8b，Q4 量化）在真实对话负载下能力不足 ——
       数据集 A/B 回归测试里，完整 prompt 下复读率 27%、精简 prompt 下 53% 且
       答非所问从 0 涨到 15；被追问逻辑（"你都叫我去睡了还怎么聊到天亮啊"）
       时只会转移话题。已整体改回云端大脑，故把入口一并撤掉。
       历史实现见 git 快照 e114b61；后端路由代码同步移除。 */
    // ★ 分层大脑（2026-09-11）：理解层 / 记忆提炼模型也归人格设置（角色卡），
    //   留空 = 跟随全局默认；可与主脑不同模型（混搭，省 token）。
    const umSelect = h('select', {});
    const mmSelect = h('select', {});
    const _layerKeys = (sel) => Array.from(sel.options).map((o) => o.value);
    const fillLayerSelect = (sel, cur) => {
      sel.innerHTML = '';
      sel.appendChild(h('option', { value: '', text: '跟随全局（默认）' }));
      if (_textModelsCache) {
        for (const [key, info] of Object.entries(_textModelsCache)) {
          sel.appendChild(h('option', { value: key, text: info.name || key }));
        }
      }
      if (cur && _layerKeys(sel).indexOf(cur) !== -1) sel.value = cur;
    };
    fillLayerSelect(umSelect, c.understandingModel || '');
    fillLayerSelect(mmSelect, c.memoryModel || '');
    umSelect.addEventListener('change', () => save({ understandingModel: umSelect.value }));
    mmSelect.addEventListener('change', () => save({ memoryModel: mmSelect.value }));
    brainCard.appendChild(pRow('理解层模型', umSelect, '第一层：先读懂你的话、判断意图；可与主脑混搭'));
    brainCard.appendChild(pRow('记忆提炼模型', mmSelect, '后台记忆/状态提炼用，轻量快省即可'));
    brainCard.appendChild(pRow('API Key', keyInput, '留空则用设置页的默认 Key'));
    brainCard.appendChild(pRow('接口地址', baseWrap, 'Ollama / 自定义服务需要'));
    brainCard.appendChild(h('div', { class: 'help-box', style: 'margin:10px 12px 12px', text: '支持 DeepSeek、通义千问、智谱 GLM、Kimi、OpenAI、本地 Ollama（免费）和任意 OpenAI 兼容接口。每个伴侣可以换不同的“大脑”。发图片仍需在全局设置里配视觉模型 Key。' }));

    /* ================= 人性化 ================= */
    const humanCard = h('div', { class: 'p-card' });
    humanCard.appendChild(h('div', { class: 'p-subtitle', text: '说话风格（像真人）' }));

    /* ★ 语言模板（一键套用结构化语言风格，写后端角色卡） */
    const LANG_PRESETS = [
      { key: 'cold', label: '清冷' },
      { key: 'clingy', label: '粘人' },
      { key: 'buddy', label: '损友' },
      { key: 'literary', label: '文艺' },
      { key: 'northeast', label: '东北损嘴' },
      { key: 'gentle', label: '温柔治愈' },
      { key: 'tsundere', label: '傲娇' },
    ];
    const langChips = h('div', { class: 'p-chips', style: 'margin:10px 12px 6px' });
    const langChipMap = {};
    const refreshLang = (cur) => {
      const key = typeof cur === 'string' ? cur : '';
      Object.values(langChipMap).forEach((x) => x.classList.remove('selected'));
      if (langChipMap[key]) langChipMap[key].classList.add('selected');
    };
    const saveLangStyle = async (key, label) => {
      save({ language_style: key || '' });
      refreshLang(key);
      try {
        let existing = {};
        try {
          const gres = await fetch('/api/pc/character/get?name=' + encodeURIComponent(c.name));
          if (gres.ok) existing = await gres.json();
        } catch (_) {}
        const data = Object.assign({}, existing, { character_name: c.name, language_style: key || null });
        await fetch('/api/pc/character/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
        toast(key ? '已套用「' + label + '」语言模板' : '已恢复手动风格');
      } catch (e) {
        toast('语言模板保存失败');
      }
    };
    const manualChip = h('button', { type: 'button', class: 'chip', text: '手动' });
    manualChip.addEventListener('click', () => saveLangStyle('', '手动'));
    langChipMap[''] = manualChip;
    langChips.appendChild(manualChip);
    for (const p of LANG_PRESETS) {
      const b = h('button', { type: 'button', class: 'chip', text: p.label });
      b.addEventListener('click', () => saveLangStyle(p.key, p.label));
      langChipMap[p.key] = b;
      langChips.appendChild(b);
    }
    refreshLang(c.language_style);
    // 异步回显后端角色卡的 language_style（老角色 Store 里没有该字段）
    (async () => {
      try {
        const gres = await fetch('/api/pc/character/get?name=' + encodeURIComponent(c.name));
        if (!gres.ok) return;
        const cfg = await gres.json();
        if (cfg && cfg.language_style && c.language_style == null) refreshLang(cfg.language_style);
      } catch (_) {}
    })();
    humanCard.appendChild(pRow('语言模板', langChips, '一键套用预设说话风格；选「手动」= 用下面这些细节自己调'));

    const mkSelect = (opts, cur, onSave) => {
      const s = h('select', {});
      for (const o of opts) s.appendChild(h('option', { value: o.value, text: o.label }));
      s.value = cur;
      s.addEventListener('change', () => onSave(s.value));
      return s;
    };
    humanCard.appendChild(pRow('打字节奏', mkSelect(REPLY_SPEEDS, c.replySpeed || 'normal', (v) => save({ replySpeed: v })), '回复出现的快慢，像真人手速'));

    /* 回复延迟：多久之后才开始回复（不是打字速度），范围内随机 */
    const delaySelect = h('select', {});
    for (const o of REPLY_DELAYS) delaySelect.appendChild(h('option', { value: o.value, text: o.label }));
    delaySelect.value = c.replyDelay || 'normal';
    const delayRangeBox = h('div', { style: 'margin-top:8px;display:flex;gap:6px;align-items:center' });
    const delayMinInput = h('input', { type: 'number', min: '0', max: '120', step: '1', style: 'width:60px', value: String(c.replyDelayMin != null ? c.replyDelayMin : 2) });
    const delayMaxInput = h('input', { type: 'number', min: '0', max: '120', step: '1', style: 'width:60px', value: String(c.replyDelayMax != null ? c.replyDelayMax : 8) });
    const refreshDelayRange = () => {
      delayRangeBox.style.display = delaySelect.value === 'custom' ? 'flex' : 'none';
    };
    delayRangeBox.appendChild(delayMinInput);
    delayRangeBox.appendChild(h('span', { text: '~' }));
    delayRangeBox.appendChild(delayMaxInput);
    delayRangeBox.appendChild(h('span', { text: '秒' }));
    delaySelect.addEventListener('change', () => {
      save({ replyDelay: delaySelect.value });
      refreshDelayRange();
      toast('已保存');
    });
    delayMinInput.addEventListener('change', () => save({ replyDelayMin: Math.max(0, Math.round(Number(delayMinInput.value) || 0)) }));
    delayMaxInput.addEventListener('change', () => save({ replyDelayMax: Math.max(0, Math.round(Number(delayMaxInput.value) || 0)) }));
    refreshDelayRange();
    const delayWrap = h('div', {});
    delayWrap.appendChild(delaySelect);
    delayWrap.appendChild(delayRangeBox);
    humanCard.appendChild(pRow('回复延迟', delayWrap, '多久之后才回复：范围内随机，更像真人想一下再回'));

    humanCard.appendChild(pRow('回复长度', mkSelect(REPLY_LENS, c.replyLen || 'normal', (v) => save({ replyLen: v }))));
    humanCard.appendChild(pRow('emoji 使用', mkSelect(EMOJI_FREQS, c.emojiFreq || 'normal', (v) => save({ emojiFreq: v }))));

    const mkSwitch = (cur, onSave) => {
      const l = h('label', { class: 'switch' });
      const inp = h('input', { type: 'checkbox' });
      inp.checked = !!cur;
      inp.addEventListener('change', (e) => onSave(e.target.checked));
      l.appendChild(inp);
      l.appendChild(h('span', { class: 'track' }));
      l.appendChild(h('span', { class: 'thumb' }));
      return l;
    };
    humanCard.appendChild(pRow('偶尔打错字 / 口误', mkSwitch(c.typo, (v) => save({ typo: v })), '像真人一样偶尔手滑'));
    humanCard.appendChild(pRow('偶尔发颜文字', mkSwitch(c.kaomoji, (v) => save({ kaomoji: v })), '如 ^_^、T_T、(≧▽≦)'));
    humanCard.appendChild(pRow('发表情包', mkSwitch(c.sticker, (v) => save({ sticker: v })), 'TA 会偶尔发大表情，像微信表情包'));
    humanCard.appendChild(pRow('括号动作描写', mkSwitch(c.actions !== false, (v) => save({ actions: v })), '开：TA 会用 (低头玩手指) 描写动作神态；关：只留纯对话'));
    humanCard.appendChild(pRow('分段发送', mkSwitch(c.splitMsg !== false, (v) => save({ splitMsg: v })), '长回复按句拆成几条消息发（每条完整），更像真人连发'));
    humanCard.appendChild(pRow('深度思考模式', mkSwitch(!!c.deep_thinking, (v) => save({ deep_thinking: v })), '开：TA 回话前会先「思考」几秒，内容更深更懂你；但更耗 token、回得更慢。用哪个模型由大脑的服务商决定：GLM/智谱大脑用 GLM-5.3，DeepSeek 大脑用「深度思考用哪个模型」里选的那个（默认 DeepSeek V4.1 Flash）；该模型不可用时会自动回退到大脑，不会因此不说话'));
    humanCard.appendChild(pRow('展示思考过程', mkSwitch(!!c.show_thinking, (v) => save({ show_thinking: v })), '开：聊天页面默认展示 TA 深度思考的推理内容（思考多久展示多久），可点按钮展开/收起'));
    // ★ 内心独白（全局）：决定"她是照着清单做作业，还是在心里嘀咕"。
    //   2026-09-11：默认开。关掉 = 回退旧的「内部思考 3 步」文案。
    const innerMonoSwitch = mkSwitch(Store.getSettings().innerMonologue !== false, (v) => {
      Store.saveSettings({ innerMonologue: v });
      toast(v ? '已开启内心独白：她会用第一人称在心里想' : '已回退「内部思考 3 步」清单');
    });
    humanCard.appendChild(pRow('思考方式', innerMonoSwitch, '开（推荐）：她在心里用第一人称嘀咕，带情绪、针对你；关：回到「内部思考 3 步」清单——更听话，但思考更像在做作业'));
    // ★ 深度思考用哪个模型（全局）：2026-09-11 前写死 deepseek-reasoner，现可切。
    const dtmSel = h('select', {});
    [
      ['deepseek-flash',    'DeepSeek V4.1 Flash（快·有思考·推荐）'],
      ['deepseek-v4-flash', 'DeepSeek V4 Flash'],
      ['deepseek-v4-pro',   'DeepSeek V4 Pro（强·慢）'],
      ['deepseek-reasoner', 'DeepSeek Reasoner（旧行为·慢）'],
      ['',                  '跟随旧逻辑（等同 Reasoner）'],
    ].forEach(([v, t]) => dtmSel.appendChild(h('option', { value: v, text: t })));
    dtmSel.addEventListener('change', () => {
      fetch('/api/pc/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ DEEP_THINKING_MODEL: dtmSel.value }),
      }).then(() => toast('深度思考模型已切换')).catch(() => toast('保存失败'));
    });
    fetch('/api/pc/config').then(r => r.json()).then(cfg => {
      const cur = cfg.DEEP_THINKING_MODEL === undefined ? 'deepseek-flash' : String(cfg.DEEP_THINKING_MODEL || '');
      if ([...dtmSel.options].some(o => o.value === cur)) dtmSel.value = cur;
    }).catch(() => {});
    // ★ 2026-09-15 事故修正：这个下拉**只对 DeepSeek 大脑的角色生效**。
    //   原来这里写着"GLM/通义等角色开深度思考仍用原模型"，但实现是"配了就无条件用它"，
    //   于是 GLM 大脑的角色一开深度思考，回复就被静默切到 deepseek-flash ——
    //   DeepSeek 一宕机她就说不出话（真机 2026-09-15 凌晨）。现在按大脑 provider 分流：
    //   GLM 大脑 → glm-5.3；DeepSeek 大脑 → 本下拉；模型不可用时自动回退大脑。
    humanCard.appendChild(pRow('深度思考用哪个模型', dtmSel, '只对 DeepSeek 大脑的角色生效（GLM/通义等大脑固定用自家强模型）；上游不可用时会自动回退到大脑'));
    // ★ 情侣模式：开关 + 提示词输入框（内容存角色卡，随时可改、免重打包）
    const couplePromptInput = h('textarea', { placeholder: '情侣模式下想让 TA 额外遵守的说话规则/内容…（留空则用 character_manager.py 里的 COUPLE_PROMPT 兜底）' });
    couplePromptInput.value = c.couple_prompt || '';
    couplePromptInput.addEventListener('change', () => { save({ couple_prompt: couplePromptInput.value }); toast('提示词已保存'); });
    const couplePromptRow = pRow('提示词内容', couplePromptInput, '保存即生效，免重打包');
    couplePromptRow.style.display = c.couple_mode ? '' : 'none';
    humanCard.appendChild(pRow('情侣模式', mkSwitch(!!c.couple_mode, (v) => {
      save({ couple_mode: v });
      couplePromptRow.style.display = v ? '' : 'none';
      toast(v ? '已开启情侣模式' : '已关闭情侣模式');
    }), '开：给 TA 注入专属提示词，改变说话内容'));
    humanCard.appendChild(couplePromptRow);

    // ★ 理解层开关（全局）：关闭后跳过场景识别/语义分析，主脑直接拿用户话+上下文线索回应
    const understandingSwitch = mkSwitch(true, (v) => {
      fetch('/api/pc/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ UNDERSTANDING_ENABLED: v }),
      }).then(() => toast(v ? '已开启理解层（场景+情绪识别）' : '已关闭理解层（主脑直连）')).catch(() => toast('保存失败'));
    });
    understandingSwitch._checkbox = understandingSwitch.querySelector('input');
    fetch('/api/pc/config').then(r => r.json()).then(cfg => {
      if (understandingSwitch._checkbox) understandingSwitch._checkbox.checked = cfg.UNDERSTANDING_ENABLED !== false;
    }).catch(() => {});
    humanCard.appendChild(pRow('理解层', understandingSwitch, '关闭后：跳过场景/情绪识别，只有一个主脑直接根据你的话+记忆等线索回应（更自然、更省 token）'));

    // ★ 提示词压缩模式（全局）：把每轮恒定注入的大块换成语义等价的精简版。
    //   规则一条不减（48 条逐条核对通过），只删同义强调与重复表述；
    //   实测恒定注入 1925 → 1311 字（−32%，约省 921 token/轮）。
    const compactSwitch = mkSwitch(false, (v) => {
      fetch('/api/pc/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ PROMPT_COMPACT: v }),
      }).then(() => toast(v ? '已开启提示词压缩（省 token，规则不变）' : '已关闭提示词压缩（用完整版提示词）')).catch(() => toast('保存失败'));
    });
    compactSwitch._checkbox = compactSwitch.querySelector('input');
    fetch('/api/pc/config').then(r => r.json()).then(cfg => {
      if (compactSwitch._checkbox) compactSwitch._checkbox.checked = cfg.PROMPT_COMPACT === true;
    }).catch(() => {});
    humanCard.appendChild(pRow('提示词压缩', compactSwitch, '开启后：每轮注入的规则改用精简表述（省约一半 token），约束条目一条不减；关闭=用完整版原文'));

    /* 自主程度（按角色，五档） */
    const autonomySelect = h('select', {});
    [
      ['', '跟随全局（平衡）'],
      ['conservative', '保守（现状约束）'],
      ['balanced', '平衡（松绑语气/话题）'],
      ['autonomous', '自主（只留底线）'],
      ['free', '自由发挥（越聊越真）'],
      ['full', '完全自主（只留人设+记忆）'],
    ].forEach(([v, t]) => autonomySelect.appendChild(h('option', { value: v, text: t })));
    autonomySelect.value = c.autonomy || '';
    autonomySelect.addEventListener('change', () => { save({ autonomy: autonomySelect.value }); toast('自主程度已保存'); });
    humanCard.appendChild(pRow('自主程度', autonomySelect, '控制 TA 回复的自主空间：保守=现状；平衡=松绑语气/长度/话题；自主=只保留底线；自由发挥=删规则；完全自主=只注入人设人格+长期记忆（记忆库/摘要/承诺/没聊完的事/对话连贯）+时间+表情包能力，其余规则/情绪/感知/导演类一律不注入——模型自由发挥，但人设和记忆不变，聊多久都还是她。注意：该档会关闭危机干预提示，也不再注入人称规矩'));

    /* ================= 离线状态（统一写角色卡 offline，跨设备同步） ================= */
    const loadOfflineCfg = async () => {
      try {
        const gres = await fetch('/api/pc/character/get?name=' + encodeURIComponent(c.name));
        if (!gres.ok) return {};
        const cfg = await gres.json();
        return (cfg && cfg.offline) || {};
      } catch (_) { return {}; }
    };
    const saveOfflineCfg = async (patch, onOk) => {
      try {
        let existing = {};
        try {
          const gres = await fetch('/api/pc/character/get?name=' + encodeURIComponent(c.name));
          if (gres.ok) existing = await gres.json();
        } catch (_) {}
        const offline = Object.assign({}, (existing && existing.offline) || {}, patch);
        const data = Object.assign({}, existing, { character_name: c.name, offline });
        await fetch('/api/pc/character/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
        if (onOk) onOk();
      } catch (e) {
        toast('保存离线设置失败');
      }
    };

    // 开关（写角色卡，异步回填）
    const offlineSwitchEl = mkSwitch(false, (v) => saveOfflineCfg({ enabled: v }, () => toast(v ? '已开启离线状态模拟' : '已关闭离线状态模拟')));
    offlineSwitchEl._checkbox = offlineSwitchEl.querySelector('input');
    humanCard.appendChild(pRow('离线状态模拟', offlineSwitchEl, '开：TA 有自己的作息，睡觉/忙时会"暂时不在"，晚点才回你（存角色卡，换设备也生效）'));

    // 最长延迟（写角色卡，异步回填）
    const offDelayInput = h('input', { type: 'number', min: '1', max: '720', step: '5', style: 'width:70px', value: '30' });
    const offDelayWrap = h('div', { style: 'display:flex;gap:6px;align-items:center' });
    offDelayWrap.appendChild(offDelayInput);
    offDelayWrap.appendChild(h('span', { text: '分钟' }));
    offDelayInput.addEventListener('change', () => {
      const v = Math.max(1, Math.min(720, Math.round(Number(offDelayInput.value) || 30)));
      offDelayInput.value = String(v);
      saveOfflineCfg({ max_delay: v }, () => toast('已设置：最长延迟 ' + v + ' 分钟'));
    });
    humanCard.appendChild(pRow('最长回复延迟', offDelayWrap, 'TA 不在时最晚多久回你（超过会截短；睡觉会到早上才回）'));

    /* ================= 作息模板（写入角色卡，全宽编辑） ================= */
    humanCard.appendChild(h('div', { class: 'p-subtitle', style: 'margin-top:14px', text: '作息模板 · TA 什么时候在线' }));
    const schedList = h('div', { style: 'display:flex;flex-direction:column;gap:6px;margin:8px 12px 12px' });
    const OFFLINE_SEGMENTS = [
      { start: 0, end: 8, label: '0~8点 · 凌晨' },
      { start: 8, end: 12, label: '8~12点 · 上午' },
      { start: 12, end: 14, label: '12~14点 · 午间' },
      { start: 14, end: 17, label: '14~17点 · 下午' },
      { start: 17, end: 19, label: '17~19点 · 傍晚' },
      { start: 19, end: 22, label: '19~22点 · 晚上' },
      { start: 22, end: 23, label: '22~23点 · 深夜前' },
      { start: 23, end: 24, label: '23~24点 · 深夜' },
    ];
    const OFFLINE_STATUS_OPTS = [
      { value: 'online', label: '在线（秒回）' },
      { value: 'light_busy', label: '有点忙（几分钟）' },
      { value: 'busy', label: '很忙（半小时~2小时）' },
      { value: 'deep_offline', label: '出门/午睡（2小时+）' },
      { value: 'sleeping', label: '睡觉（早上回）' },
    ];
    const DEFAULT_STATUS = { 0:'sleeping', 8:'online', 12:'light_busy', 14:'online', 17:'light_busy', 19:'online', 22:'light_busy', 23:'sleeping' };
    const selects = {};
    OFFLINE_SEGMENTS.forEach((seg) => {
      const row = h('div', { style: 'display:flex;align-items:center;gap:8px' });
      const lab = h('div', { style: 'width:130px;font-size:13px;color:var(--text-2,#666);flex:none', text: seg.label });
      const sel = h('select', { style: 'flex:1;padding:6px 8px;border:1px solid #e0e0e0;border-radius:8px;font-size:13px;outline:none;background:#fff' });
      for (const o of OFFLINE_STATUS_OPTS) sel.appendChild(h('option', { value: o.value, text: o.label }));
      sel.value = DEFAULT_STATUS[seg.start] || 'online';
      selects[seg.start] = sel;
      row.appendChild(lab);
      row.appendChild(sel);
      schedList.appendChild(row);
    });

    // 恢复默认随机作息按钮
    const resetRandomBtn = h('button', { type: 'button', style: 'padding:6px 14px;border:1px solid #e0e0e0;border-radius:8px;background:#fafafa;cursor:pointer;font-size:13px;color:#555', text: '恢复默认随机作息' });
    resetRandomBtn.addEventListener('click', () => {
      OFFLINE_SEGMENTS.forEach((seg) => { selects[seg.start].value = DEFAULT_STATUS[seg.start] || 'online'; });
      saveOfflineCfg({ schedule: null }, () => toast('已恢复：每天随机作息'));
    });
    schedList.appendChild(resetRandomBtn);
    humanCard.appendChild(schedList);

    const saveSchedule = () => {
      const schedule = OFFLINE_SEGMENTS.map((seg) => ({ start: seg.start, end: seg.end, status: selects[seg.start].value }));
      saveOfflineCfg({ schedule }, () => toast('作息模板已保存'));
    };
    OFFLINE_SEGMENTS.forEach((seg) => {
      selects[seg.start].addEventListener('change', saveSchedule);
    });

    // 异步读取角色卡离线配置，回填开关/延迟/作息
    (async () => {
      const off = await loadOfflineCfg();
      if (offlineSwitchEl._checkbox) offlineSwitchEl._checkbox.checked = !!off.enabled;
      if (off.max_delay != null) offDelayInput.value = String(off.max_delay);
      const sched = Array.isArray(off.schedule) ? off.schedule : [];
      sched.forEach((seg) => {
        if (seg && seg.status && selects[seg.start]) selects[seg.start].value = seg.status;
      });
    })();

    /* 回复段数范围（分段发送开启时，回复拆成几段，范围内随机） */
    const segMinInput = h('input', { type: 'number', min: '1', max: '15', step: '1', style: 'width:52px', value: String(c.replySegMin != null ? c.replySegMin : 1) });
    const segMaxInput = h('input', { type: 'number', min: '1', max: '15', step: '1', style: 'width:52px', value: String(c.replySegMax != null ? c.replySegMax : 6) });
    const segRangeBox = h('div', { style: 'display:flex;gap:6px;align-items:center' });
    segRangeBox.appendChild(segMinInput);
    segRangeBox.appendChild(h('span', { text: '~' }));
    segRangeBox.appendChild(segMaxInput);
    segRangeBox.appendChild(h('span', { text: '段' }));
    segMinInput.addEventListener('change', () => save({ replySegMin: Math.max(1, Math.round(Number(segMinInput.value) || 1)) }));
    segMaxInput.addEventListener('change', () => save({ replySegMax: Math.max(1, Math.round(Number(segMaxInput.value) || 1)) }));
    humanCard.appendChild(pRow('回复段数范围', segRangeBox, '长回复拆成几条消息发（范围内随机，如 2~4 段）；仅在「分段发送」开启时生效'));

    const catchInput = h('input', { type: 'text', placeholder: '如：哈哈哈、真的假的', value: c.catchphrase || '' });
    catchInput.addEventListener('change', () => { save({ catchphrase: catchInput.value.trim() }); toast('已保存'); });
    humanCard.appendChild(pRow('口头禅', catchInput));

    /* ================= 主动与免打扰 ================= */
    const activeCard = h('div', { class: 'p-card' });
    activeCard.appendChild(h('div', { class: 'p-subtitle', text: '主动与免打扰' }));

    // ★ 2026-09-14 移除：「主动发消息（最长间隔·分钟）」控件。
    //   用户拍板：主动消息间隔**统一以全局设置为准**
    //   （全局设置 → 主动发言间隔，如「60–120 分钟（高冷）」）。
    //   删除原因：
    //     · 这个控件只存一个"上限"值，与全局的"区间"语义不一致；
    //     · 它的字段 proactiveMaxMin 原先不在白名单 → 只写 localStorage，
    //       后端从未读到（实测确认角色卡里该字段根本不存在）；
    //     · 它正是"跳出时间范围限制"三条链路口径不一致的根源之一。
    //   需要调节奏请去：全局设置 → 主动发言间隔。
    const activeCardHint = h('div', { class: 'p-hint',
      text: '主动消息的节奏由「全局设置 → 主动发言间隔」统一决定（如 60–120 分钟）。' });
    activeCard.appendChild(activeCardHint);

    // AI 主导日：每周一次由 TA 提出一个真正值得回答的问题。
    const leadWrap = h('div', { style: 'display:flex;align-items:center;gap:10px' });
    const leadSwitch = h('input', { type: 'checkbox' });
    leadSwitch.checked = c.aiLeadDay !== false;
    const leadDay = h('select', {});
    [['1','周一'],['2','周二'],['3','周三'],['4','周四'],['5','周五'],['6','周六'],['0','周日']].forEach(([v,t]) => leadDay.appendChild(h('option', { value:v, text:t })));
    leadDay.value = String(c.aiLeadDayWeekday != null ? c.aiLeadDayWeekday : 0);
    const saveLeadDay = () => {
      const enabled = !!leadSwitch.checked;
      const weekday = Number(leadDay.value);
      save({ aiLeadDay: enabled, aiLeadDayWeekday: weekday });
      fetch('/api/pc/config', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({
        AI_LEAD_DAY_ENABLED: enabled, AI_LEAD_DAY_WEEKDAY: weekday === 0 ? 6 : weekday - 1,
      }) }).catch(() => {});
      toast(enabled ? 'AI 主导日已开启' : 'AI 主导日已关闭');
    };
    leadSwitch.addEventListener('change', saveLeadDay);
    leadDay.addEventListener('change', saveLeadDay);
    leadWrap.appendChild(leadSwitch);
    leadWrap.appendChild(h('span', { text: '每周' }));
    leadWrap.appendChild(leadDay);
    leadWrap.appendChild(h('span', { class:'p-hint', text:'TA 会提出一个本周专属问题' }));
    activeCard.appendChild(pRow('AI 主导日', leadWrap));

    /* 测试按钮（★ 2026-09-14：不再显示"下次主动时间"）
       原先这里读联系人 nextProactive 显示"约 X 后（到期自动发）"，但前端排期已删除，
       主动节奏统一由后端把关（全局"主动发言间隔" + last_proactive_push 闸门），
       再显示前端倒计时就是假的。 */
    const testBtn = h('button', { class: 'chip', text: '马上发一条（测试）' });
    testBtn.addEventListener('click', () => {
      testBtn.textContent = '触发中…';
      proactiveTick({ force: true, contactId: c.id }).then(() => {
        testBtn.textContent = '马上发一条（测试）';
        toast('已触发，稍等几秒看消息');
      });
    });
    activeCard.appendChild(pRow('主动消息', testBtn, '发不发、隔多久由 TA 自己决定，受全局「主动发言间隔」限制'));
    activeCard.appendChild(pRow('定时任务（X分钟后回答我）', h('span', { class: 'blind-note', text: '发消息时说"一分钟后回答我"等，TA 会到点真发' })));

    // ★ 2026-09-14 改造：主动消息时段统一到角色卡 active_hours。
    //   旧实现用 quietStart/quietEnd（"免打扰时段"），但它**从未进入后端角色卡保存白名单**
    //   → 设了不起作用；而且 scheduler 只读全局时段、根本不看角色卡。
    //   现在这里直接编辑 activeHours（后端 resolve_active_hours 的第一优先级）。
    const ahFrom = h('input', { type: 'time', value: (c.activeHours || '').split('-')[0] || '' });
    const ahTo = h('input', { type: 'time', value: (c.activeHours || '').split('-')[1] || '' });
    // 状态面板的刷新函数：先占位，等下面把 engBox 建好后赋值（改时段/开关时要用到它）
    let refreshEng = () => {};
    const syncActiveHours = () => {
      const a = ahFrom.value, b = ahTo.value;
      // 两端都有值才写入；只填一端视为"暂不生效"，避免产生半截配置
      if (a && b) save({ activeHours: a + '-' + b });
      else if (!a && !b) save({ activeHours: '' });
      // ★ 2026-09-16 修：改完时段必须**刷新下面的状态面板**。
      //   面板本来只在打开资料页时抓一次 /api/proactive/status，改完时段它还是旧值
      //   （实测：输入框已是 20:00-01:44，面板仍显示 08:00-23:00 → 用户以为"设置没成功"）。
      setTimeout(() => { try { refreshEng(); } catch (_) {} }, 400);
    };
    ahFrom.addEventListener('change', syncActiveHours);
    ahTo.addEventListener('change', syncActiveHours);
    // ★ 2026-09-16：type=time 的输入框要**失焦/回车**才触发 change；
    //   用户"调完数值"往往还停在输入框里 → 既没保存、下面状态也不刷新（用户实测反馈）。
    //   这里补 input 事件：一边调一边就把状态面板刷成"将生效的值"。
    ahFrom.addEventListener('input', () => { setTimeout(() => { try { refreshEng(); } catch (_) {} }, 50); });
    ahTo.addEventListener('input', () => { setTimeout(() => { try { refreshEng(); } catch (_) {} }, 50); });
    const ahWrap = h('div', { style: 'display:flex;gap:8px;align-items:center' });
    ahWrap.appendChild(ahFrom);
    ahWrap.appendChild(h('span', { text: '—' }));
    ahWrap.appendChild(ahTo);
    activeCard.appendChild(pRow('主动消息可用时段', ahWrap,
      '只有这段时间 TA 会主动找你（支持跨天，如 22:00-06:00）。你设的定时提醒与到点承诺不受此限制；留空 = 默认 08:00-23:00'));

    // 早晚安是否允许穿透上面的时段（默认否 —— 尊重你设的静默范围）
    const greetSwitch = mkSwitch(c.dndAllowGreetings === true, (v) => {
      save({ dndAllowGreetings: v });
      toast(v ? '早晚安可在静默时段送达' : '静默时段不再发早晚安');
      setTimeout(() => { try { refreshEng(); } catch (_) {} }, 400);   // 同"可用时段"：改完刷新状态
    });
    greetSwitch._checkbox = greetSwitch.querySelector('input');
    activeCard.appendChild(pRow('时段内允许早晚安', greetSwitch,
      '关闭（默认）：过了可用时段就不发任何消息，包括早晚安。开启：早晚安仍会送达（静音不响铃）'));

    /* ★ 2026-09-16 新增（用户拍板）：「主动回复由模型自主」
       开：跳过全局「主动发言间隔」（90–120 分钟）的节奏机，由 TA 自己决定什么时候想说、说什么；
           免打扰/睡眠/离线/可用时段/每日上限**照旧生效** —— 时段外一条都不发。
       关（默认）：按全局间隔到点才问模型；模型仍可选择"这次不说"（输出 [[不说话]]）。 */
    const autoSwitch = mkSwitch(c.proactiveModelDecides === true, (v) => {
      save({ proactiveModelDecides: v });
      toast(v ? '已开启：由 TA 自己决定何时开口' : '已关闭：按全局「主动发言间隔」到点才开口');
      setTimeout(() => { try { refreshEng(); } catch (_) {} }, 400);
    });
    autoSwitch._checkbox = autoSwitch.querySelector('input');
    activeCard.appendChild(pRow('主动回复由模型自主', autoSwitch,
      '开：TA 想什么时候找你、想说什么，自己定（仍受免打扰/可用时段/每日上限限制）；'
      + '关（默认）：按全局「主动发言间隔」计时，到点才问 TA 要不要说'));

    /* ★ 2026-09-15 新增：主动消息引擎状态（用户要求"看得见下次什么时候能主动 + 最近被拦原因"）。
       数据来自 /api/proactive/status（单一引擎的真实状态，不是前端估的）。 */
    const engBox = h('div', { class: 'help-box', style: 'margin:10px 12px 12px;line-height:1.7' });
    const engRefresh = h('button', { class: 'btn-mini', text: '刷新' });
    const renderEng = (d, err) => {
      if (err || !d || d.ok === false) {
        engBox.textContent = '主动消息引擎状态读取失败：' + ((err && err.message) || (d && d.error) || '未知');
        return;
      }
      const _rows = Array.isArray(d.recent) ? d.recent.slice(-5).reverse() : [];
      const _fmtRow = (r) => {
        const t = String(r.ts || '').slice(11, 16);
        if (r.kind === 'deliver') return '· ' + t + ' 已发出（' + (r.ptype || r.proactive_type || '') + '）';
        return '· ' + t + ' 未发：' + ({ outside_active_hours: '不在可用时段', interval_waiting: '没到间隔',
          settling_window: '刚聊完（静默窗口）', daily_cap: '到每日上限', dnd: '免打扰',
          user_sleeping: '你说要睡了', offline: 'TA 离线', conversation_active: '你在聊' }[r.reason] || r.reason || '');
      };
      const _lines = [
        '引擎：' + (d.engine_enabled ? '已启用（单一实现，节奏由后端决定）' : '已关闭（回退旧逻辑）'),
        '当前状态：' + (d.phase_cn || d.phase || '?'),
        d.in_window === false
          ? ('可用时段：' + (d.hours || '默认') + '（现在**不在**时段内' +
             (d.next_window_start ? '，约 ' + new Date(d.next_window_start * 1000).toTimeString().slice(0, 5) + ' 开窗' : '') + '）')
          : ('可用时段：' + (d.hours || '默认') + '（现在在时段内）'),
        '下次可主动：' + ((d.seconds_to_next > 0)
          ? ((d.next_allowed_cn || '—') + '（还有 ' + Math.round(d.seconds_to_next / 60) + ' 分）')
          : (d.in_window === false ? '已到点，但当前不在可用时段' : '已到点，随时可以开口')),
        '静默窗口：' + Math.round((d.quiet_window_sec || 300) / 60) + ' 分钟' +
          (d.settle_remaining > 0 ? '（还要 ' + Math.round(d.settle_remaining / 60) + ' 分才算聊完）' : ''),
        '今日已主动：' + (d.sent_today || 0) + ' / ' + (d.cap || 0) + ' 条（提醒与到点承诺不计入）',
        '节奏来源：' + (d.model_decides ? '模型自主（由 TA 自己决定何时开口）'
                                        : '按全局「主动发言间隔」计时'),
        _rows.length ? ('最近：\n' + _rows.map(_fmtRow).join('\n')) : '最近：暂无记录',
        // 时间戳：让"是不是旧快照"一眼可见（用户此前无法判断面板是不是没刷新）
        '（更新于 ' + new Date().toTimeString().slice(0, 8) + '）',
      ].filter(Boolean);
      engBox.textContent = _lines.join('\n');
    };
    // 统一的刷新入口（按钮、"可用时段"改动、早晚安开关、自动定时都复用它）
    refreshEng = () => {
      fetch('/api/proactive/status?character_id=' + encodeURIComponent(c.id))
        .then((r) => r.json()).then((d) => renderEng(d)).catch((e) => renderEng(null, e));
    };
    engRefresh.addEventListener('click', () => refreshEng());
    refreshEng();          // 打开资料页时抓一次
    // ★ 2026-09-16：资料页开着的时候每 15 秒自刷一次 —— 面板再也不会长期停在旧快照
    //   （用户报"上面调了时段，下面状态不更新"的根因就是面板只在打开时抓一次）
    try {
      if (window.__engStatusTimer) clearInterval(window.__engStatusTimer);
      window.__engStatusTimer = setInterval(() => {
        const pg = document.getElementById('profile-page');
        if (!pg || !pg.classList.contains('open')) return;   // 页面关了就不抓（close() 也会清掉）
        refreshEng();
      }, 15000);
    } catch (_) {}
    const engWrap = h('div', {});
    engWrap.appendChild(engBox);
    engWrap.appendChild(engRefresh);
    activeCard.appendChild(pRow('主动消息状态', engWrap,
      '她"什么时候能主动找你"由后端引擎统一算：聊完 5 分钟静默 → 按全局「主动发言间隔」计时 → 到点且不在免打扰/上限内才会开口'));

    // 引擎参数（全局，写到 config.json；保存后用 /api/pc/config）
    const capInput = h('input', { type: 'number', min: '0', max: '50', style: 'width:70px',
      value: String((window.__pcConfig && window.__pcConfig.PROACTIVE_DAILY_CAP) != null
        ? window.__pcConfig.PROACTIVE_DAILY_CAP : 8) });
    const quietInput = h('input', { type: 'number', min: '1', max: '60', style: 'width:70px',
      value: String(Math.round(((window.__pcConfig && window.__pcConfig.PROACTIVE_QUIET_WINDOW_SEC) || 300) / 60)) });
    const saveEng = (patch) => {
      fetch('/api/pc/config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch) })
        .then(() => { toast('已保存'); setTimeout(() => engRefresh.click(), 200); })
        .catch(() => toast('保存失败'));
    };
    capInput.addEventListener('change', () =>
      saveEng({ PROACTIVE_DAILY_CAP: Math.max(0, Math.min(50, Math.round(Number(capInput.value) || 0))) }));
    quietInput.addEventListener('change', () =>
      saveEng({ PROACTIVE_QUIET_WINDOW_SEC: Math.max(1, Math.min(60, Math.round(Number(quietInput.value) || 5))) * 60 }));
    const engParamWrap = h('div', { style: 'display:flex;gap:8px;align-items:center' });
    engParamWrap.appendChild(h('span', { text: '每天最多' }));
    engParamWrap.appendChild(capInput);
    engParamWrap.appendChild(h('span', { text: '条；聊完静默' }));
    engParamWrap.appendChild(quietInput);
    engParamWrap.appendChild(h('span', { text: '分钟' }));
    activeCard.appendChild(pRow('主动消息上限与静默窗口', engParamWrap,
      '上限只限制"找话说"的消息（无聊问候/补话/关怀等）；你设的定时提醒与到点承诺不受上限影响'));


    /* ================= 知识与记忆 ================= */
    const knowCard = h('div', { class: 'p-card' });
    knowCard.appendChild(h('div', { class: 'p-subtitle', text: '知识与记忆' }));

    const sysInput = h('textarea', { placeholder: '补充性格设定或规矩…' });
    sysInput.value = c.system || '';
    sysInput.addEventListener('change', () => { save({ system: sysInput.value.trim() }); toast('已保存'); });
    knowCard.appendChild(pRow('性格补充设定', sysInput));

    const memInput = h('textarea', { placeholder: 'TA 已经知道的关于你的事…' });
    memInput.value = c.memory || '';
    memInput.addEventListener('change', () => { save({ memory: memInput.value.trim() }); toast('已保存'); });
    knowCard.appendChild(pRow('核心记忆', memInput, '长期关键信息，聊天中始终记得'));

    const reflectBox = h('div', { style:'display:flex;align-items:center;gap:8px;justify-content:flex-end' });
    const reflectStatus = h('span', { class:'p-hint', text:'正在读取学习状态…' });
    const reflectBtn = h('button', { type:'button', class:'chip', text:'现在复盘一次' });
    reflectBox.appendChild(reflectStatus); reflectBox.appendChild(reflectBtn);
    const reflectionIds = () => ({
      sid: window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default',
      cid: c.name || c.id || 'default',
    });
    const loadReflectionStatus = async () => {
      try {
        const ids = reflectionIds();
        const res = await fetch(`/api/reflection/status?session_id=${encodeURIComponent(ids.sid)}&character_id=${encodeURIComponent(ids.cid)}`);
        const data = await res.json();
        reflectStatus.textContent = `记忆 ${Number(data.memory_count || 0)} 条 · 有效反思 ${Number(data.reflection_count || 0)} 条`;
      } catch (_) { reflectStatus.textContent = '学习状态暂时不可用'; }
    };
    reflectBtn.addEventListener('click', async () => {
      reflectBtn.disabled = true; reflectBtn.textContent = '正在复盘…';
      try {
        const ids = reflectionIds();
        const res = await fetch('/api/reflection/run', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ session_id:ids.sid, character_id:ids.cid }) });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || '复盘失败');
        const count = Number(data.result?.count || 0);
        toast(count ? `复盘完成，形成 ${count} 条新理解` : '复盘完成，暂时没有足够证据形成新理解');
        await loadReflectionStatus();
      } catch (e) { toast(e.message || '复盘失败'); }
      finally { reflectBtn.disabled = false; reflectBtn.textContent = '现在复盘一次'; }
    });
    knowCard.appendChild(pRow('学习与反思', reflectBox, '从真实记忆中找长期规律；有证据才保存，并自动影响后续回复与主动关心'));
    loadReflectionStatus();

    /* 记忆存储文件夹入口 */
    const msControl = h('div', { style: 'text-align:right' });
    msControl.appendChild(h('span', { class: 'row-arrow', text: '共 ' + ((c.memStore || []).length) + ' 条 ›' }));
    const msRow = pRow('记忆存储', msControl, '文件夹：手动添加 / 粘贴文本 / 导入文件 / 聊天中让 TA 记住');
    msRow.addEventListener('click', () => MemStore.open(c.id));
    knowCard.appendChild(msRow);

    /* ---- 删除 ---- */
    const delBtn = h('button', { class: 'p-danger', text: '删除这个伴侣' });
    delBtn.addEventListener('click', () => {
      if (confirm('确定删除「' + c.name + '」？聊天记录会一并删除。')) {
        Store.deleteContact(c.id);
        this.close();
        Chat.close();
        renderContacts();
        renderChatList();
        toast('已删除');
      }
    });

    /* ---- 首次引导横幅 ---- */
    const guideSeen = !!(Store.getSettings().guideSeen);
    let guide = null;
    if (!guideSeen) {
      guide = h('div', { class: 'guide-banner' });
      guide.appendChild(h('div', { class: 'guide-title', text: '👋 这里是 TA 的「资料与设置」' }));
      guide.appendChild(h('div', { class: 'guide-text', text:
        '· 基本资料：名字、亲密度、称呼、自称\n' +
        '· 人设：语言、性格、爱好、背景、风格、聊天背景\n' +
        '· AI 大脑：给 TA 换不同的模型（DeepSeek/通义/Kimi/本地 Ollama…）\n' +
        '· 人性化：打字节奏、错别字、颜文字、表情包、口头禅\n' +
        '· 主动与免打扰：TA 多久主动找你一次、什么时段不打扰\n' +
        '· 知识与记忆：告诉 TA 关于你的事，或上传记忆库'
      }));
      const guideBtn = h('button', { class: 'btn btn-primary', text: '知道了，开始设置' });
      guideBtn.addEventListener('click', () => {
        Store.saveSettings({ guideSeen: true });
        guide.remove();
        toast('改完即自动保存');
      });
      guide.appendChild(guideBtn);
    }

    /* ---- 记忆日志入口 ---- */
    const logLabelEl = h('div', { class: 'p-label' });
    logLabelEl.appendChild(h('div', { text: '📔 记忆日志' }));
    logLabelEl.appendChild(h('div', { class: 'p-hint', text: '每晚 23:00 自动生成，查看 TA 的每日回忆' }));

    const logControlEl = h('div', { class: 'p-control' });
    logControlEl.style.textAlign = 'right';
    logControlEl.appendChild(h('span', { class: 'row-arrow', text: '›' }));

    const logRow = h('div', { class: 'p-row' });
    logRow.appendChild(logLabelEl);
    logRow.appendChild(logControlEl);
    logRow.addEventListener('click', () => Logs.open(c.id));

    const logCard = h('div', { class: 'p-card' });
    logCard.appendChild(logRow);

    /* ---- 关系档案入口 ---- */
    const arcLabelEl = h('div', { class: 'p-label' });
    arcLabelEl.appendChild(h('div', { text: '📂 关系档案' }));
    arcLabelEl.appendChild(h('div', { class: 'p-hint', text: '相识天数、消息/记忆统计，点日历回看任意一天的聊天' }));

    const arcControlEl = h('div', { class: 'p-control' });
    arcControlEl.style.textAlign = 'right';
    arcControlEl.appendChild(h('span', { class: 'row-arrow', text: '›' }));

    const arcRow = h('div', { class: 'p-row' });
    arcRow.appendChild(arcLabelEl);
    arcRow.appendChild(arcControlEl);
    arcRow.addEventListener('click', () => Archive.open(c.id));

    const archiveCard = h('div', { class: 'p-card' });
    archiveCard.appendChild(arcRow);

    /* ---- 外置记忆库入口 ---- */
    const emLabelEl = h('div', { class: 'p-label' });
    emLabelEl.appendChild(h('div', { text: '📚 外置记忆库' }));
    emLabelEl.appendChild(h('div', { class: 'p-hint', text: 'AI 的长期记忆图书馆：每天原文 + 日/周/月总结，点开翻阅' }));

    const emControlEl = h('div', { class: 'p-control' });
    emControlEl.style.textAlign = 'right';
    emControlEl.appendChild(h('span', { class: 'row-arrow', text: '›' }));

    const emRow = h('div', { class: 'p-row' });
    emRow.appendChild(emLabelEl);
    emRow.appendChild(emControlEl);
    emRow.addEventListener('click', () => ExtMemory.open(c.name || c.id));

    const emCard = h('div', { class: 'p-card' });
    emCard.appendChild(emRow);

    body.appendChild(hero);
    if (guide) body.appendChild(guide);
    body.appendChild(logCard);
    body.appendChild(archiveCard);
    body.appendChild(emCard);
    body.appendChild(secTitle('基本资料'));
    body.appendChild(baseCard);

    // —— 把"人设 / AI 大脑 / 人性化 / 主动与免打扰 / 知识与记忆" 5 块合并到「AI 人格」一级分组 ——
    body.appendChild(secTitle('AI 人格'));
    body.appendChild(personaCard);
    body.appendChild(voiceCard);
    body.appendChild(brainCard);
    body.appendChild(humanCard);
    body.appendChild(activeCard);
    body.appendChild(_buildGrowthCard(c, save));
    body.appendChild(knowCard);

    body.appendChild(delBtn);
  },
};

/* ---------------- 工具 ---------------- */
function pRow(label, control, hint) {
  const labelEl = h('div', { class: 'p-label' });
  if (typeof label === 'string') {
    labelEl.appendChild(document.createTextNode(label));
  } else if (label instanceof Node) {
    labelEl.appendChild(label);
  }
  if (hint) {
    labelEl.appendChild(h('div', { class: 'p-hint', text: hint }));
  }

  const controlEl = h('div', { class: 'p-control' });
  if (control instanceof Node) {
    controlEl.appendChild(control);
  } else if (control != null) {
    controlEl.appendChild(document.createTextNode(String(control)));
  }

  const row = h('div', { class: 'p-row' });
  row.appendChild(labelEl);
  row.appendChild(controlEl);
  return row;
}

function secTitle(text) {
  return h('div', { class: 'p-sec-title', text: text });
}

/* ---------------- 关系成长面板 ---------------- */

async function _syncIntimacyToBackend(contactId, levelValue) {
  // levelValue 是前端 1~10 级，后端是 0~100
  // 转换：level * 10 - 5（level=1→5, level=5→45, level=10→95）
  const backendValue = Math.round(levelValue * 10 - 5);
  try {
    await fetch('/api/intimacy/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default',
        character_id: (Store.getContact(contactId)?.name || contactId || 'default'),
        value: backendValue
      })
    });
  } catch (e) {
    // 静默，不影响本地保存
  }
}

function _buildGrowthCard(c, save) {
  const card = h('div', { class: 'p-card' });
  const moodWrap = h('div', { style: 'padding:0 12px 10px' });
  const moodText = h('div', { class: 'p-hint', text: '正在读取 TA 的状态…' });
  moodWrap.appendChild(moodText);
  (async () => {
    try {
      const sid = (typeof Session !== 'undefined' && Session.getSessionId)
        ? Session.getSessionId() : (localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default');
      const cid = c.name || c.id || 'default';
      const res = await fetch('/api/ai_mood/state?session_id=' + encodeURIComponent(sid)
        + '&character_id=' + encodeURIComponent(cid));
      const data = await res.json();
      const m = data && data.state;
      moodText.textContent = m ? `${m.emoji} ${m.status} · 心情：${m.emotion}` : '🌙 平静 · 正常陪伴中';
    } catch (_) {
      moodText.textContent = '暂时无法读取状态';
    }
  })();
  card.appendChild(h('div', { class: 'p-subtitle', text: '💕 关系成长' }));
  card.appendChild(pRow('AI 当前状态', moodWrap, '仅在你主动打开人格页时显示；聊天中只通过语气自然流露'));

  /* ★ 2026-09-15：亲密度/关系自动成长已砍，数值面板默认隐藏
     （后端 RELATIONSHIP_UI_VISIBLE=false）——数字不再变化、还可能显示被重置的旧值。
     开关置 true 时这些区块原样回来，代码没删。 */
  const _relUI = (typeof window.relUIShow === 'function') ? window.relUIShow() : false;

  if (_relUI) {
  // ── 当前关系阶段展示
  const stageWrap = h('div', { class: 'growth-stage-wrap' });
  let refreshStageSelection = () => {};
  _loadGrowthState(c, stageWrap, (state) => refreshStageSelection(state.stage));
  card.appendChild(stageWrap);

  // ── 角色成长档案：显示哪些性格在变化，以及变化依据
  const personalityGrowthWrap = h('div', { style: 'padding:0 12px 12px' });
  _loadPersonalityGrowth(c, personalityGrowthWrap);
  card.appendChild(personalityGrowthWrap);

  // ── 三维数值手动调节
  card.appendChild(h('div', {
    class: 'p-hint',
    style: 'padding:8px 12px 2px;font-weight:600;color:var(--text-1)',
    text: '手动调节数值'
  }));
  card.appendChild(h('div', {
    class: 'p-hint',
    style: 'padding:0 12px 8px',
    text: '自动成长以外，你也可以直接调整'
  }));

  // 亲密度
  card.appendChild(_buildValueRow('亲密度', 'intimacy', c, save, 0, 100,
    '影响AI说话的亲密程度和用词'));
  // 好感度
  card.appendChild(_buildValueRow('好感度', 'affection', c, save, 0, 100,
    'AI对你的喜爱程度，影响主动关心频率'));
  // 信任度
  card.appendChild(_buildValueRow('信任度', 'trust', c, save, 0, 100,
    '影响AI愿意分享内心想法的程度'));
  // 互动天数
  card.appendChild(_buildValueRow('互动天数', 'interactionDays', c, save, 0, 3650,
    '已互动的天数，影响专属习惯解锁'));

  // ── 关系阶段直接设定
  card.appendChild(h('div', {
    class: 'p-hint',
    style: 'padding:8px 12px 4px;font-weight:600;color:var(--text-1)',
    text: '直接设定关系阶段'
  }));

  const stageChips = h('div', { class: 'p-chips', style: 'padding:0 12px 12px' });
  const STAGES = [
    { key: 'stranger',     label: '陌生人', intimacy: 0  },
    { key: 'friend',       label: '朋友',   intimacy: 20 },
    { key: 'close_friend', label: '亲密朋友', intimacy: 40 },
    { key: 'lover',        label: '恋人',   intimacy: 70 },
    { key: 'soulmate',     label: '灵魂伴侣', intimacy: 90 },
  ];
  const stageButtons = {};
  const stageFromIntimacy = (value) => {
    const n = Number(value) || 0;
    if (n >= 90) return 'soulmate';
    if (n >= 70) return 'lover';
    if (n >= 40) return 'close_friend';
    if (n >= 20) return 'friend';
    return 'stranger';
  };
  refreshStageSelection = (stage) => {
    const selected = stage || c.relationshipStage || stageFromIntimacy(c.intimacy);
    Object.entries(stageButtons).forEach(([key, button]) => {
      button.classList.toggle('selected', key === selected);
      button.setAttribute('aria-pressed', key === selected ? 'true' : 'false');
    });
  };
  for (const s of STAGES) {
    const btn = h('button', { type: 'button', class: 'chip', text: s.label });
    stageButtons[s.key] = btn;
    btn.addEventListener('click', async () => {
      const oldStage = c.relationshipStage || stageFromIntimacy(c.intimacy);
      refreshStageSelection(s.key);
      Object.values(stageButtons).forEach((button) => { button.disabled = true; });
      try {
        const state = await _pushRelationState(c.id, {
          intimacy: s.intimacy,
          stage: s.key
        });
        const patch = {
          intimacy: Number(state?.intimacy ?? s.intimacy),
          affection: Number.isFinite(Number(state?.affection)) ? Number(state.affection) : Number(c.affection ?? 50),
          trust: Number.isFinite(Number(state?.trust)) ? Number(state.trust) : Number(c.trust ?? 0),
          relationshipStage: state?.stage || s.key,
        };
        if (Number.isFinite(Number(state?.interaction_days))) patch.interactionDays = Number(state.interaction_days);
        save(patch);
        refreshStageSelection(patch.relationshipStage);
        if (window.Store && typeof Store.updateContact === 'function') {
          Store.updateContact(c.id, patch);
        }
        toast('已设置为「' + s.label + '」');
        await _loadGrowthState(c, stageWrap, (latest) => refreshStageSelection(latest.stage));
      } catch (e) {
        refreshStageSelection(oldStage);
        toast(e.message || '关系阶段保存失败');
      } finally {
        Object.values(stageButtons).forEach((button) => { button.disabled = false; });
      }
    });
    stageChips.appendChild(btn);
  }
  refreshStageSelection();
  card.appendChild(stageChips);
  card.appendChild(h('div', { class: 'p-hint', style: 'padding:0 12px 12px', text: '选择阶段会把亲密度调整到该阶段的起点；之后仍会随互动自然成长。' }));
  } else {
    // ★ 关键：`if (_relUI)` 必须把**整段"关系数值"区块**包住（含下面这段阶段设定）。
    //   2026-09-15 踩坑：一开始只包到"互动天数"就把 } 收了，结果下面 阶段设定/里程碑
    //   仍在 if 之外，却引用 if 内部 `let` 声明的 stageWrap / refreshStageSelection →
    //   ReferenceError → **整个"人格设置"页打不开**（用户 21:4x 实测反馈）。
    //   教训：块级作用域的变量被后面引用时，node --check 查不出来（那是语法检查），
    //   必须确认注释区块的边界与变量作用域一致。
    card.appendChild(h('div', {
      class: 'p-hint',
      style: 'padding:8px 12px 12px',
      text: '亲密度/好感/信任等数值面板已关闭（省 token + 数值不再自动变化）。如需恢复，把 config.json 的 RELATIONSHIP_UI_VISIBLE 设为 true。'
    }));
  }

  // ── 里程碑历史
  const msTitle = h('div', {
    class: 'p-hint',
    style: 'padding:8px 12px 4px;font-weight:600;color:var(--text-1)',
    text: '✨ 里程碑记录'
  });
  card.appendChild(msTitle);

  const msWrap = h('div', { class: 'growth-milestones', style: 'padding:0 12px 12px' });
  _loadMilestones(c.id, msWrap);
  card.appendChild(msWrap);

  // ── 永久纪念收藏：月度信 / 里程碑信 / 语音 / 礼物
  const keepTitle = h('div', {
    class: 'p-hint',
    style: 'padding:8px 12px 4px;font-weight:600;color:var(--text-1);display:flex;justify-content:space-between;align-items:center;gap:8px',
  });
  keepTitle.appendChild(h('span', { text: '🎁 纪念收藏' }));
  const openKeepsBtn = h('button', { type: 'button', class: 'chip', text: '去信件页看 ★' });
  openKeepsBtn.addEventListener('click', () => {
    try {
      Profile.close();
      if (typeof openLetterKeeps === 'function') openLetterKeeps(c.name || c.id || '');
    } catch (_) {}
  });
  keepTitle.appendChild(openKeepsBtn);
  card.appendChild(keepTitle);
  const keepWrap = h('div', { class: 'relationship-keeps', style: 'padding:0 12px 12px' });
  _loadRelationshipKeeps(c, keepWrap);
  card.appendChild(keepWrap);

  return card;
}

async function _loadPersonalityGrowth(contact, wrap) {
  wrap.innerHTML = '<div class="p-hint" style="padding:8px 0">正在读取性格进化…</div>';
  try {
    const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const cid = contact?.name || contact?.character_id || contact?.id || 'default';
    const res = await fetch(`/api/personality/growth?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(cid)}`);
    const data = await res.json();
    if (!res.ok || data.ok === false) throw new Error(data.error || 'load failed');
    const dims = Array.isArray(data.dimensions) ? data.dimensions : [];
    const head = h('div', { style:'display:flex;justify-content:space-between;align-items:center;margin:8px 0 6px' });
    head.appendChild(h('div', { style:'font-weight:600;color:var(--text-1,#333)', text:'🌱 性格进化档案' }));
    head.appendChild(h('span', { class:'p-hint', text:`进化 ${Number(data.level || 0)}/100 · 互动 ${Number(data.interactions || 0)} 次` }));
    wrap.innerHTML = '';
    wrap.appendChild(head);
    const grid = h('div', { style:'display:grid;grid-template-columns:1fr 1fr;gap:8px' });
    dims.forEach((d) => {
      const box = h('div', { style:'padding:8px 10px;border:1px solid rgba(120,100,160,.12);border-radius:10px;background:rgba(120,100,160,.04)' });
      const row = h('div', { style:'display:flex;justify-content:space-between;font-size:12px;color:var(--text-2,#666)' });
      row.appendChild(h('span', { text:d.label || d.key }));
      row.appendChild(h('span', { text:`${Number(d.value || 0)}/${Number(d.max || 1)}` }));
      const track = h('div', { style:'height:5px;background:#eee;border-radius:5px;margin-top:6px;overflow:hidden' });
      const fill = h('div', { style:`height:100%;width:${Math.max(0, Math.min(100, Number(d.value || 0) / Math.max(1, Number(d.max || 1)) * 100))}%;background:linear-gradient(90deg,#c8a8ff,#8c6be8);border-radius:5px` });
      track.appendChild(fill); box.appendChild(row); box.appendChild(track); grid.appendChild(box);
    });
    wrap.appendChild(grid);
    const events = Array.isArray(data.events) ? data.events.slice(-3).reverse() : [];
    if (events.length) {
      const evTitle = h('div', { class:'p-hint', style:'margin-top:9px;font-weight:600', text:'最近为什么会变化' });
      wrap.appendChild(evTitle);
      const evBox = h('div', { style:'font-size:12px;color:var(--text-2,#666);line-height:1.6' });
      events.forEach((ev) => {
        const items = Array.isArray(ev.items) ? ev.items.join('；') : '';
        if (items) evBox.appendChild(h('div', { text:`· ${ev.time || ''} ${items}` }));
      });
      wrap.appendChild(evBox);
    }
    wrap.appendChild(h('div', { class:'p-hint', style:'margin-top:7px', text:'成长是慢慢累积的，不会改写角色原始人设；重要事实、提醒和生日不会故意记错。' }));
  } catch (_) {
    wrap.innerHTML = '<div class="p-hint" style="padding:8px 0;color:var(--text-3,#999)">性格进化档案暂时无法读取</div>';
  }
}

function _buildValueRow(label, field, c, save, min, max, hint) {
  // 字段映射（前端Store字段名 → 后端字段名）
  const backendFieldMap = {
    intimacy: 'intimacy',
    affection: 'affection',
    trust: 'trust',
    interactionDays: 'interaction_days'
  };

  const raw = Number(c[field]);
  const cur = Number.isFinite(raw) ? raw : (field === 'affection' ? 50 : 0);
  const slider = h('input', {
    type: 'range', min: String(min), max: String(max),
    step: '1', value: String(cur)
  });
  const numDisplay = h('span', {
    class: 'level-num',
    style: 'min-width:36px;text-align:right',
    text: String(cur)
  });

  let _debounceTimer = null;
  slider.addEventListener('input', () => {
    const v = Number(slider.value);
    numDisplay.textContent = String(v);
    save({ [field]: v });
    // 防抖500ms再同步后端
    clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(async () => {
      try {
        const state = await _pushRelationState(c.id, { [backendFieldMap[field]]: v });
        if (state) {
          const patch = {};
          for (const k of ['intimacy', 'affection', 'trust']) {
            if (Number.isFinite(Number(state[k]))) patch[k] = Math.max(0, Math.min(100, Number(state[k])));
          }
          if (state.stage) patch.relationshipStage = state.stage;
          if (Number.isFinite(Number(state.interaction_days))) patch.interactionDays = Number(state.interaction_days);
          if (Object.keys(patch).length) {
            save(patch);
            if (window.Store && typeof Store.updateContact === 'function') Store.updateContact(c.id, patch);
          }
        }
      } catch (e) {
        toast(e.message || '关系数值保存失败');
      }
    }, 500);
  });

  const labelDiv = h('div', { class: 'p-label' });
  labelDiv.appendChild(h('div', { text: label }));
  if (hint) labelDiv.appendChild(h('div', { class: 'p-hint', text: hint }));

  const row = h('div', { class: 'p-row' });
  row.appendChild(labelDiv);
  row.appendChild(
    h('div', { class: 'p-control' },
      h('div', { class: 'p-slider-block', style: 'flex:1' },
        slider, numDisplay
      )
    )
  );
  return row;
}

async function _loadGrowthState(contact, wrap, onLoaded) {
  wrap.innerHTML = '<div class="p-hint" style="padding:8px 12px">加载中…</div>';
  try {
    const sid = window.Session?.getSessionId?.() || (typeof Chat !== 'undefined' && Chat.sessionId) || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const cid = (contact && (contact.name || contact.character_id || contact.id)) || 'default';
    const res  = await fetch(`/api/relationship/state?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(cid)}`);
    const data = await res.json();
    if (contact) {
      // ★ 后端无真实交互记录（初始态）时不回写，避免把用户设定的初始亲密度清零
      const pristine = !Number(data.interaction_days)
        && (!data.stage || data.stage === 'stranger')
        && !Number(data.intimacy)
        && !Number(data.trust)
        && (!Number(data.affection) || Number(data.affection) === 50);
      if (!pristine) {
        const patch = {};
        for (const key of ['intimacy', 'affection', 'trust']) {
          if (Number.isFinite(Number(data[key]))) patch[key] = Math.max(0, Math.min(100, Number(data[key])));
        }
        if (data.stage) patch.relationshipStage = data.stage;
        Store.updateContact(contact.id, patch);
      }
    }
    wrap.innerHTML = '';
    wrap.appendChild(_buildGrowthProgress(data));
    if (typeof onLoaded === 'function') onLoaded(data);
    return data;
  } catch (e) {
    wrap.innerHTML = '<div class="p-hint" style="padding:8px 12px;color:var(--text-3)">暂无数据</div>';
  }
}

function _buildGrowthProgress(state) {
  const stage     = state.stage || 'stranger';
  const intimacy  = Number(state.intimacy  || 0);
  const affection = Number(state.affection || 50);
  const trust     = Number(state.trust     || 0);
  const days      = Number(state.interaction_days || 0);

  // 阶段进度计算
  const STAGE_THRESHOLDS = [
    { key: 'stranger',     label: '陌生人',   min: 0,  next: 20  },
    { key: 'friend',       label: '朋友',     min: 20, next: 40  },
    { key: 'close_friend', label: '亲密朋友', min: 40, next: 70  },
    { key: 'lover',        label: '恋人',     min: 70, next: 90  },
    { key: 'soulmate',     label: '灵魂伴侣', min: 90, next: 100 },
  ];
  const curStage  = STAGE_THRESHOLDS.find(s => s.key === stage)
    || STAGE_THRESHOLDS[0];
  const progress  = curStage.next > curStage.min
    ? Math.round((intimacy - curStage.min) / (curStage.next - curStage.min) * 100)
    : 100;

  const wrap = h('div', { class: 'growth-progress-wrap' });

  // 阶段标题行
  const titleRow = h('div', { class: 'growth-stage-row' });
  titleRow.appendChild(h('span', { class: 'growth-stage-badge', text: curStage.label }));
  titleRow.appendChild(h('span', {
    class: 'growth-stage-hint',
    text: `${days} 天 · 亲密度 ${intimacy}/100`
  }));
  wrap.appendChild(titleRow);

  // 进度条
  const barWrap = h('div', { class: 'growth-bar-wrap' });
  const barFill = h('div', {
    class: 'growth-bar-fill',
    style: `width:${Math.max(2, Math.min(100, progress))}%`
  });
  barWrap.appendChild(barFill);

  // 阶段节点
  const nodesWrap = h('div', { class: 'growth-nodes' });
  for (const s of STAGE_THRESHOLDS) {
    const pct = s.min;
    const active = intimacy >= s.min;
    const node = h('div', {
      class: 'growth-node' + (active ? ' active' : ''),
      style: `left:${pct}%`
    });
    node.title = s.label;
    nodesWrap.appendChild(node);
  }
  barWrap.appendChild(nodesWrap);
  wrap.appendChild(barWrap);

  // 三维数值小卡片
  const statsRow = h('div', { class: 'growth-stats-row' });
  const statItem = (label, val, color) => {
    const wrap = h('div', { class: 'growth-stat' });
    const valEl = h('div', { class: 'growth-stat-val' });
    valEl.style.color = color;
    valEl.textContent = String(val);
    const lblEl = h('div', { class: 'growth-stat-label' });
    lblEl.textContent = label;
    wrap.appendChild(valEl);
    wrap.appendChild(lblEl);
    return wrap;
  };
  statsRow.appendChild(statItem('亲密度', intimacy,  '#e91e8c'));
  statsRow.appendChild(statItem('好感度', affection, '#ff9800'));
  statsRow.appendChild(statItem('信任度', trust,     '#4caf50'));
  wrap.appendChild(statsRow);

  return wrap;
}

async function _loadMilestones(contactId, wrap) {
  wrap.innerHTML = '<div style="color:var(--text-3);font-size:13px">加载中…</div>';
  try {
    const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const contact = Store.getContact(contactId);
    const cid = (contact && (contact.name || contact.id)) || contactId || 'default';
    const res  = await fetch(
      `/api/relationship/milestones?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(cid)}`
    );
    const data = await res.json();
    const list = data.milestones || [];
    wrap.innerHTML = '';
    if (!list.length) {
      wrap.appendChild(h('div', {
        style: 'color:var(--text-3);font-size:13px',
        text: '还没有里程碑，继续聊天解锁 ✨'
      }));
      return;
    }
    const tl = h('div', { class: 'ms-timeline' });
    for (const m of list.slice(0, 10)) {
      const item = h('div', { class: 'ms-item' });
      item.appendChild(h('div', { class: 'ms-dot' }));
      item.appendChild(h('div', { class: 'ms-content' },
        h('div', { class: 'ms-title', text: m.title || m.event_type }),
        h('div', { class: 'ms-desc',  text: m.content || '' }),
        h('div', { class: 'ms-date',  text: _profileFmtRelTime(m.created_at) })
      ));
      tl.appendChild(item);
    }
    wrap.appendChild(tl);
  } catch (e) {
    wrap.innerHTML = '<div style="color:var(--text-3);font-size:13px">暂无里程碑</div>';
  }
}

async function _loadRelationshipKeeps(contact, wrap) {
  wrap.innerHTML = '<div class="p-hint">正在打开收藏盒…</div>';
  try {
    const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
    const cid = (contact && (contact.name || contact.character_id || contact.id)) || 'default';
    const res = await fetch(`/api/relationship/keeps?session_id=${encodeURIComponent(sid)}&character_id=${encodeURIComponent(cid)}&limit=100`);
    const data = await res.json();
    const list = data.keeps || [];
    wrap.innerHTML = '';
    if (!list.length) {
      wrap.appendChild(h('div', { class: 'keep-empty', text: '重要的信、声音和礼物会一直留在这里 ✨' }));
      return;
    }
    for (const item of list) {
      const gift = item.gift || {};
      const audioBtn = h('button', {
        class: 'keep-audio-btn',
        text: item.audio_url ? '▶ 听这封信' : '♬ 生成语音',
      });
      audioBtn.addEventListener('click', async (ev) => {
        ev.stopPropagation();
        audioBtn.disabled = true;
        try {
          let url = item.audio_url;
          if (!url) {
            const rr = await fetch(`/api/relationship/keeps/${encodeURIComponent(item.id)}/audio`, {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ session_id: sid, character_id: cid }),
            });
            const jj = await rr.json();
            if (!rr.ok || !jj.audio_url) throw new Error(jj.error || '语音生成失败');
            url = jj.audio_url;
            item.audio_url = url;
            audioBtn.textContent = '▶ 听这封信';
          }
          const audio = new Audio(url);
          await audio.play();
        } catch (e) {
          toast(e.message || '语音播放失败');
        } finally {
          audioBtn.disabled = false;
        }
      });
      const card = h('div', { class: 'keep-card' },
        h('div', { class: 'keep-card-top' },
          h('span', { class: 'keep-icon', text: gift.icon || (item.keep_type === 'monthly_letter' ? '🌙' : '💌') }),
          h('div', { class: 'keep-heading' },
            h('div', { class: 'keep-title', text: item.title || '纪念信' }),
            h('div', { class: 'keep-date', text: _profileFmtRelTime(item.created_at) }),
          ),
          item.permanent ? h('span', { class: 'keep-badge', text: '永久' }) : null,
        ),
        h('div', { class: 'keep-content', text: item.content || '' }),
        gift.label ? h('div', { class: 'keep-gift', text: `${gift.icon || '🎁'} ${gift.label}` }) : null,
        audioBtn,
      );
      wrap.appendChild(card);
    }
  } catch (_) {
    wrap.innerHTML = '<div class="p-hint">收藏暂时无法打开</div>';
  }
}

function _profileFmtRelTime(ts) {
  if (!ts) return '';
  const d   = new Date(ts.replace ? ts.replace(' ', 'T') : ts);
  const now = Date.now();
  const diff = now - d.getTime();
  if (diff < 60000)     return '刚刚';
  if (diff < 3600000)   return Math.floor(diff / 60000) + ' 分钟前';
  if (diff < 86400000)  return Math.floor(diff / 3600000) + ' 小时前';
  if (diff < 604800000) return Math.floor(diff / 86400000) + ' 天前';
  return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

async function _pushRelationState(contactId, fields) {
  const contact = Store.getContact(contactId);
  const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
  const cid = (contact && (contact.name || contact.id)) || contactId || 'default';
  const res = await fetch('/api/relationship/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sid, character_id: cid, ...fields })
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false || data.error) {
    throw new Error(data?.error?.message || data?.error || '关系设置保存失败');
  }
  return data.state || data;
}

async function _syncVoiceToBackend(contactId, characterName, voiceKey, voiceProfiles, voiceStyle) {
  /**
   * 把选择的 voice_key 同步到后端角色卡。
   * 调用 /api/character/voice 接口。
   */
  try {
    await fetch('/api/character/voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        character_name: characterName,
        voice_key:      voiceKey,
        voice_profiles: voiceProfiles || undefined,
        voice_style:    voiceStyle || undefined,
      })
    });
  } catch (e) {
    // 静默失败，本地 Store 已保存，下次打开时会回显
    console.warn('[VoiceSync] 同步失败:', e);
  }
}

/* ---------------- 事件绑定 ---------------- */
$('#profile-back').addEventListener('click', () => Profile.close());
$('#profile-done').addEventListener('click', () => Profile.close());


