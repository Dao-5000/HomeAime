'use strict';
/* ============================================================
   incoming_call.js — AI 主动来电界面
   微信风格：全屏来电 + 铃声 + 头像 + 来电文案 + 接听/拒绝
   ============================================================ */

const IncomingCall = {

    _overlay:   null,
    _audio:     null,
    _timer:     null,
    _msg:       null,
    _answered:  false,

    /* ── 外部入口：ws_client.js 收到 incoming_call 消息后调用 */
    show(msg) {
        // 防重
        if (this._overlay) return;

        this._msg      = msg;
        this._answered = false;

        this._render(msg);
        this._ringStart();

        // 30秒无响应自动超时
        this._timer = setTimeout(() => this._timeout(), 30_000);

        // ★ 页面最小化时用 Notification API 弹系统通知（如果有权限）
        this._trySystemNotify(msg);
    },

    /* ── 渲染来电全屏 UI */
    _render(msg) {
        const name   = msg.contact_name  || 'AI';
        const avatar = msg.contact_avatar || '';
        const text   = msg.call_text      || '来电话了';

        const avatarInner = avatar
            ? `<img src="${avatar}" alt="${name}"
                    onerror="this.style.display='none';
                             this.nextElementSibling.style.display='flex'">`
            : '';
        const avatarFallback = `
            <span class="ic-avatar-letter"
                  style="${avatar ? 'display:none' : ''}">
                ${name.charAt(0)}
            </span>`;

        const overlay = document.createElement('div');
        overlay.id = 'incoming-call-overlay';
        overlay.innerHTML = `
            <div class="ic-top">
                <div class="ic-label">AI 来电</div>
                <div class="ic-avatar">
                    ${avatarInner}
                    ${avatarFallback}
                </div>
                <div class="ic-name">${name}</div>
                <div class="ic-text">${text}</div>
                <div class="ic-wave" id="ic-wave">
                    <span></span><span></span>
                    <span></span><span></span><span></span>
                </div>
            </div>

            <div class="ic-bottom">
                <!-- 拒绝 -->
                <div class="ic-btn-wrap">
                    <button class="ic-btn ic-btn-reject" id="ic-reject">
                        <svg viewBox="0 0 24 24">
                            <path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977
                                     0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89
                                     -6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12
                                     -.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65
                                     3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0
                                     .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"/>
                        </svg>
                    </button>
                    <span class="ic-btn-label">拒绝</span>
                </div>

                <!-- 接听 -->
                <div class="ic-btn-wrap">
                    <button class="ic-btn ic-btn-accept" id="ic-accept">
                        <svg viewBox="0 0 24 24">
                            <path d="M20.01 15.38c-1.23 0-2.42-.2-3.53-.56a.977.977
                                     0 00-1.01.24l-1.57 1.97c-2.83-1.35-5.48-3.9-6.89
                                     -6.83l1.95-1.66c.27-.28.35-.67.24-1.02-.37-1.12
                                     -.56-2.32-.56-3.55 0-.54-.45-.99-.99-.99H4.19C3.65
                                     3 3 3.24 3 3.99 3 13.28 10.73 21 20.01 21c.71 0
                                     .99-.63.99-1.18v-3.45c0-.54-.45-.99-.99-.99z"
                                     transform="rotate(135 12 12)"/>
                        </svg>
                    </button>
                    <span class="ic-btn-label">接听</span>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);
        this._overlay = overlay;

        // 入场动画
        requestAnimationFrame(() => overlay.classList.add('show'));

        // 波形动画
        setTimeout(() => {
            const wave = document.getElementById('ic-wave');
            if (wave) wave.classList.add('active');
        }, 300);

        // 绑定按钮
        document.getElementById('ic-accept').onclick = () => this._accept();
        document.getElementById('ic-reject').onclick = () => this._reject();
    },

    /* ── 接听 */
    _accept() {
        if (this._answered) return;
        this._answered = true;

        const msg = this._msg;
        this._cleanup();

        // 上报接听结果（character_id=真实角色名，供后端冷却 key）
        WsClient.sendCallResult('accepted', msg.contact_id, msg.character_id);

        // ★ 复用现有 VoiceCall.start()，传入来电的 voice_key
        try {
            VoiceCall.start({
                contactId:   msg.contact_id,
                characterId: msg.character_id,
                voiceKey:   msg.voice_key || '',
                callReason: msg.call_reason || '',
                callSource: 'ai_initiated',
            });
        } catch (e) {
            console.error('[IncomingCall] 启动通话失败:', e);
            toast('通话启动失败，请重试');
        }
    },

    /* ── 拒绝 */
    _reject() {
        if (this._answered) return;
        this._answered = true;

        const msg = this._msg;
        WsClient.sendCallResult('rejected', msg && msg.contact_id, msg && msg.character_id);
        this._cleanup();
        toast('已拒绝来电');
    },

    /* ── 超时（30秒无响应） */
    _timeout() {
        if (this._answered) return;
        this._answered = true;

        const msg = this._msg;
        WsClient.sendCallResult('timeout', msg && msg.contact_id, msg && msg.character_id);
        this._cleanup();

        if (!msg) return;

        const contactId = msg.contact_id;
        if (!contactId) return;

        // ★ 确保会话存在（接缝：Store.ensureConversation 第204行）
        const conv = Store.ensureConversation(contactId);

        // ★ 写入"未接来电"系统消息
        const missedMsg = {
            id:      Store.uid(),
            role:    'system',          // system 类型，和普通 assistant 消息区分
            content: `📞 ${msg.contact_name || 'AI'} 来电，未接听`,
            ts:      Date.now(),
            status:  'done',
            subtype: 'missed_call',     // 供 Chat 渲染时识别，显示特殊样式
        };
        Store.addMessage(conv.id, missedMsg);

        // ★ 更新会话排序 + 未读计数
        Store.touchConversation(conv.id, missedMsg.content);

        // ★ unread++ （直接操作 conv 对象，Store 已持有同一引用）
        const cv = Store.getConversation(contactId);
        if (cv) {
            cv.unread = (cv.unread || 0) + 1;
            Store.save();
        }

        // ★ 刷新聊天列表（未读红点）
        try { renderChatList(); } catch (_) {}

        // ★ 如果当前就在这个联系人的聊天界面，追加消息气泡
        try {
            if (window.Chat && Chat.contact && Chat.contact.id === contactId) {
                const body = document.getElementById('chat-body');
                if (body) {
                    const el = Chat.renderMsg(missedMsg);
                    body.appendChild(el);
                    Chat.scrollBottom();
                }
            }
        } catch (_) {}
    },

    /* ── 铃声 */
    _ringStart() {
        // 用系统震动模拟来电节奏
        try {
            if (navigator.vibrate) {
                // 来电震动模式：震300ms 停200ms 循环
                const pattern = Array(8).fill([300, 200]).flat();
                navigator.vibrate(pattern);
            }
        } catch (_) {}

        // Web Audio API 合成铃声（无需音频文件，纯代码生成）
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            let startTime = ctx.currentTime;

            const playRing = () => {
                // 一声铃：两个音调交替（模拟经典来电音）
                [[1046, 0.15], [1318, 0.15]].forEach(([freq, dur], i) => {
                    const osc  = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.type      = 'sine';
                    osc.frequency.value = freq;
                    gain.gain.setValueAtTime(0.3, startTime + i * dur);
                    gain.gain.exponentialRampToValueAtTime(
                        0.001, startTime + i * dur + dur - 0.01
                    );
                    osc.start(startTime + i * dur);
                    osc.stop(startTime + i * dur + dur);
                });
                startTime += 0.3 + 1.2; // 一次铃声 + 间隔
            };

            // 响8声（30秒内）
            for (let i = 0; i < 8; i++) playRing();
            this._audio = ctx;
        } catch (_) {
            // Web Audio 不可用时静默降级
        }
    },

    /* ── 尝试系统级通知（页面最小化时） */
    _trySystemNotify(msg) {
        // 页面在前台且聚焦，不需要系统通知
        if (document.visibilityState === 'visible' && document.hasFocus()) return;

        const name = msg.contact_name || 'AI';
        const text = msg.call_text    || '来电话了';

        // Electron 环境：用 desktopWin 弹通知
        if (window.desktopWin && typeof window.desktopWin.showNotification === 'function') {
            try {
                window.desktopWin.showNotification({
                    title:  `${name} 来电`,
                    body:   text,
                    action: 'incoming_call',
                });
            } catch (_) {}
            return;
        }

        // Web 环境：Notification API
        if (!('Notification' in window)) return;

        const doNotify = () => {
            try {
                const n = new Notification(`📞 ${name} 来电`, {
                    body:    text,
                    icon:    msg.contact_avatar || '/favicon.ico',
                    tag:     'incoming-call',
                    renotify: true,
                    requireInteraction: true,  // 不自动消失
                });
                n.onclick = () => {
                    window.focus();
                    n.close();
                };
            } catch (_) {}
        };

        if (Notification.permission === 'granted') {
            doNotify();
        } else if (Notification.permission !== 'denied') {
            // 首次请求权限（需要用户手势，这里是收到来电时触发，
            // 实际上需要在用户首次打开app时请求，这里作为兜底）
            Notification.requestPermission().then(p => {
                if (p === 'granted') doNotify();
            });
        }
    },

    /* ── 清理 */
    _cleanup() {
        if (this._timer)   { clearTimeout(this._timer); this._timer = null; }
        if (this._audio)   { try { this._audio.close(); } catch (_) {} this._audio = null; }
        if (navigator.vibrate) { try { navigator.vibrate(0); } catch (_) {} }

        if (this._overlay) {
            const ov = this._overlay;
            ov.classList.remove('show');
            setTimeout(() => { if (ov.parentNode) ov.parentNode.removeChild(ov); }, 300);
            this._overlay = null;
        }
    },
};
