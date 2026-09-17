'use strict';
/* ============================================================
   pc_enhance.js（新增文件，不属于原项目代码）
   PC 桌面版增强入口：
   1. 「AI 内核设置」弹窗（模型选择/自定义、记忆提炼轮次、主动发言时间范围）
      —— 入口挂在原「全局设置」页里，样式复用原有 class，视觉风格统一
   2. 记忆库导入导出：在原「记忆存储」页工具栏加「导出备份 / 导入备份」
      支持加密 JSON + 纯文本 TXT，按扩展名自动识别，合并/覆盖两种模式
   所有改动均为运行时注入，不修改任何原有 JS 文件；后端不可用时静默隐藏，不影响原功能。
   ============================================================ */
const PCEnhance = {
  cfg: null,
  backendOk: false,

  async api(path, opts) {
    const res = await fetch(path, opts);
    const j = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((j && j.error && j.error.message) || ('请求失败 ' + res.status));
    return j;
  },

  async init() {
    // 桌面端窗口控制 + 数据迁移：不依赖后端，先注入
    this.injectDesktopChrome();
    this.wrapSwitchTab();
    // 记忆库 UI 覆盖：不依赖后端，先执行
    this.wrapMemStore();
    // Profile 记忆数同步 + 人格卡片设置按钮
    this.wrapProfile();
    // 设置页入口：不依赖后端，先注入（确保即使后端连接失败也能看到入口）
    this.buildSettingsOverlay();
    this.wrapSettingsPage();
    // 后端增强（模型/记忆库导入导出）：后端可用才做
    try {
      this.cfg = await this.api('/api/pc/config');
      this.backendOk = true;
      if (this.settingsBodyEl) this.renderSettingsBody();
    } catch (_) { this.backendOk = false; }
    document.addEventListener('pc-config', (e) => {
      this.cfg = e.detail || this.cfg;
      if (this.settingsBodyEl) this.renderSettingsBody();
    });
  },

  /* ================= 桌面端窗口控制（无边框窗口的最小化/关闭） ================= */
  /* 解决问题1：去掉系统右上角按钮，改为注入原 top-btn 风格的两个小图标 */

  injectDesktopChrome() {
    if (!(window.desktopWin && window.desktopWin.isDesktop)) return; // 仅桌面端
    if (!document.getElementById('pc-drag-style')) {
      const st = document.createElement('style');
      st.id = 'pc-drag-style';
      st.textContent =
        '#topbar{ -webkit-app-region: drag; }' +
        '#topbar-left,#topbar-right,.pc-win-btn{ -webkit-app-region: no-drag; }' +
        '#topbar-right{ width:auto !important; min-width:40px; gap:2px; padding-right:6px; }' +
        '.pc-win-btn{ width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;border:none;background:transparent;color:var(--text);cursor:pointer;border-radius:6px; }' +
        '.pc-win-btn:hover{ background:rgba(0,0,0,0.08); }' +
        '.pc-win-btn.close:hover{ background:#FA5151;color:#fff; }' +
        '.pc-win-btn svg{ width:16px;height:16px;display:block; }' +
        /* 聊天头部可拖动 */
        '.chat-header{ -webkit-app-region: drag; }' +
        '.chat-header button{ -webkit-app-region: no-drag; }' +
        /* 美化记忆按钮：去掉 emoji，用 SVG */
        '#chat-mem{ font-size:0 !important; width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center; border:none; background:transparent; cursor:pointer; border-radius:8px; transition:background .15s; }' +
        '#chat-mem:hover{ background:rgba(0,0,0,0.08); }' +
        '#chat-mem svg{ width:20px; height:20px; color:#555; }' +
        /* 学习档案按钮（🎓）：与 📚 同一套样式 */
        '#chat-learn{ font-size:0 !important; width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center; border:none; background:transparent; cursor:pointer; border-radius:8px; transition:background .15s; }' +
        '#chat-learn:hover{ background:rgba(0,0,0,0.08); }' +
        '#chat-learn svg{ width:20px; height:20px; color:#555; }' +
        /* 学习档案面板 */
        '.learn-sec{ margin:0 0 14px; }' +
        '.learn-item{ padding:10px 12px; margin:6px 4px; border-radius:10px; background:rgba(0,0,0,0.035); }' +
        '.learn-item-main{ font-size:14px; line-height:1.5; color:#222; word-break:break-word; }' +
        '.learn-item-meta{ font-size:12px; line-height:1.45; color:#888; margin-top:4px; word-break:break-word; }' +
        '.learn-item .chip{ margin-top:8px; }' +
        '.learn-loading{ padding:20px 12px; color:#888; font-size:13px; }' +
        '#chat-more{ width:34px; height:34px; display:inline-flex; align-items:center; justify-content:center; border-radius:8px; }' +
        '#chat-more:hover{ background:rgba(0,0,0,0.08); }';
      document.head.appendChild(st);
    }
    // 替换 📚 emoji 为 SVG 图标
    this.beautifyChatHeader();
  },

  beautifyChatHeader() {
    const btn = document.getElementById('chat-mem');
    if (!btn) return;
    // 替换 📚 emoji 为 SVG 书本图标
    if (!btn.querySelector('svg')) {
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>';
      btn.title = '记忆库';
    }
    // 学习档案按钮同样换成 SVG（学士帽），与 📚 视觉一致
    const learnBtn = document.getElementById('chat-learn');
    if (learnBtn && !learnBtn.querySelector('svg')) {
      learnBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 10 12 5 2 10l10 5 10-5z"/><path d="M6 12v5c0 1.7 2.7 3 6 3s6-1.3 6-3v-5"/></svg>';
      learnBtn.title = '她学到了什么 / 她整理了什么（可撤回）';
    }
    // 移除聊天窗口的设置按钮（已移到左侧人格卡片）
    const oldSet = document.getElementById('chat-settings-btn');
    if (oldSet) oldSet.remove();
    // 注入表情包按钮
    this.injectStickerButton();
  },

  stickerPanelEl: null,

  injectStickerButton() {
    if (document.getElementById('chat-sticker')) return;
    const imgBtn = document.getElementById('chat-image');
    if (!imgBtn) return;
    const btn = document.createElement('button');
    btn.id = 'chat-sticker';
    btn.title = '表情包';
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/></svg>';
    btn.style.cssText = 'width:36px;height:36px;display:inline-flex;align-items:center;justify-content:center;border:none;background:transparent;cursor:pointer;border-radius:8px;transition:background .15s;';
    btn.querySelector('svg').style.cssText = 'width:20px;height:20px;color:#555;';
    btn.addEventListener('mouseenter', () => { btn.style.background = 'rgba(0,0,0,0.06)'; });
    btn.addEventListener('mouseleave', () => { btn.style.background = 'transparent'; });
    btn.addEventListener('click', () => this.openStickerPanel());
    imgBtn.parentNode.insertBefore(btn, imgBtn.nextSibling);
  },

  async openStickerPanel() {
    if (this.stickerPanelEl) {
      this.stickerPanelEl.remove();
      this.stickerPanelEl = null;
      return;
    }
    const inputBar = document.getElementById('chat-inputbar');
    if (!inputBar) return;
    try {
      const data = await this.api('/api/pc/sticker/list');
      const stickers = data.stickers || [];
      const panel = document.createElement('div');
      panel.id = 'pc-sticker-panel';
      panel.style.cssText = 'position:absolute;bottom:60px;left:10px;width:320px;max-height:260px;overflow-y:auto;background:#fff;border:1px solid #eee;border-radius:10px;box-shadow:0 4px 20px rgba(0,0,0,0.12);padding:12px;z-index:100;';
      let html = '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;">';
      if (stickers.length === 0) {
        html = '<div style="text-align:center;color:#999;padding:20px;font-size:13px;">还没有表情包<br>把图片放到「表情包」文件夹即可使用</div>';
      } else {
        stickers.forEach(s => {
          html += `<div class="pc-sticker-item" data-file="${s.filename}" style="cursor:pointer;border-radius:6px;overflow:hidden;aspect-ratio:1;background:#f5f5f5;display:flex;align-items:center;justify-content:center;">
            <img src="${s.url}" style="max-width:100%;max-height:100%;object-fit:contain;" loading="lazy">
          </div>`;
        });
        html += '</div>';
      }
      panel.innerHTML = html;
      inputBar.style.position = 'relative';
      inputBar.appendChild(panel);
      this.stickerPanelEl = panel;
      panel.querySelectorAll('.pc-sticker-item').forEach(item => {
        item.addEventListener('click', () => {
          const input = document.getElementById('chat-input');
          if (input) {
            input.value += '[sticker:' + item.dataset.file + ']';
            input.dispatchEvent(new Event('input'));
          }
          panel.remove();
          this.stickerPanelEl = null;
        });
      });
      // 点击外部关闭
      setTimeout(() => {
        const closeHandler = (e) => {
          if (!panel.contains(e.target) && e.target.id !== 'chat-sticker') {
            panel.remove();
            this.stickerPanelEl = null;
            document.removeEventListener('click', closeHandler);
          }
        };
        document.addEventListener('click', closeHandler);
      }, 10);
    } catch (e) {
      console.error('加载表情包失败', e);
    }
  },

  renderStickersInMessages() {
    /* 把聊天消息里的 [sticker:文件名] 渲染为图片 */
    const msgs = document.querySelectorAll('#chat-body .msg-content, #chat-body .message-content');
    msgs.forEach(el => {
      if (el.dataset.stickerRendered) return;
      const html = el.innerHTML;
      if (html.includes('[sticker:')) {
        el.innerHTML = html.replace(/\[sticker:([^\]]+)\]/g,
          '<img src="/stickers/$1" style="max-width:200px;max-height:200px;border-radius:8px;display:block;margin-top:4px;" onerror="this.style.display=\'none\'">');
        el.dataset.stickerRendered = '1';
      }
    });
  },

  /* 人格卡片加设置按钮 + 记忆数同步 */
  enhancePersonaPage() {
    const wrap = $('#page-persona');
    if (!wrap) return;
    const self = this;
    // 角色配置已合并到「资料与设置」页，不再单独入口
    // 调度与模板入口已移除——早晚安/节日/惊喜后台默认运行，用户通过对话控制
    // （软件本质是聊天软件，不暴露设置面板）
    const inject = () => {
      const contacts = Store.listContacts();
      const rows = wrap.querySelectorAll('.row');
      // 第一个 row 是"添加 AI 伴侣"，跳过
      for (let i = 1; i < rows.length && i <= contacts.length; i++) {
        const row = rows[i];
        const c = contacts[i - 1];
        if (!c || row.querySelector('.pc-contact-settings')) continue;
        // 把右侧 › 箭头替换为 设置齿轮 + ›
        const arrow = row.querySelector('.row-arrow');
        if (arrow) {
          const gear = document.createElement('button');
          gear.className = 'pc-contact-settings';
          gear.title = '人格设置';
          gear.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="width:18px;height:18px;color:#888"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
          gear.style.cssText = 'width:30px;height:30px;display:inline-flex;align-items:center;justify-content:center;border:none;background:transparent;cursor:pointer;border-radius:6px;margin-right:4px;';
          gear.addEventListener('click', (e) => {
            e.stopPropagation();
            Profile.open(c.id);
          });
          gear.addEventListener('mouseenter', () => { gear.style.background = 'rgba(0,0,0,0.06)'; });
          gear.addEventListener('mouseleave', () => { gear.style.background = 'transparent'; });
          arrow.parentNode.insertBefore(gear, arrow);
        }
      }
    };
    inject();
    // MutationObserver 监听页面重渲染，自动重新注入设置按钮
    if (!self._personaObserver) {
      self._personaObserver = new MutationObserver(() => {
        if (wrap.offsetParent !== null) inject();
      });
      self._personaObserver.observe(wrap, { childList: true, subtree: true });
    }
  },

  /* 覆盖 Profile.render：记忆存储数字同步后端 + 注入角色细节字段 */
  wrapProfile() {
    const self = this;
    const origRender = Profile.render.bind(Profile);
    Profile.render = function () {
      origRender();
      // 异步获取后端记忆数，更新显示
      self.api('/api/pc/memory/all').then((data) => {
        const total = (data.db_count || 0) + (data.disk_count || 0);
        const arrows = document.querySelectorAll('#profile-body .row-arrow');
        for (const el of arrows) {
          if (el.textContent && el.textContent.includes('共') && el.textContent.includes('条')) {
            el.textContent = '共 ' + total + ' 条 ›';
            break;
          }
        }
      }).catch(() => {});
      // 注入角色细节字段
      self._injectCharDetails();
      // 注入表情包管理
      self._injectStickerManager();
    };
    // 覆盖 Profile.save：保存主动发言间隔后通知后端更新
    const origSave = Profile.save.bind(Profile);
    Profile.save = function (patch) {
      origSave(patch);
      if (patch && patch.proactiveMaxMin !== undefined && window.WsClient) {
        try {
          const c = Store.getContact(Profile.contactId);
          if (c) WsClient.sendHello();
        } catch (_) {}
      }
    };
  },

  async _injectCharDetails() {
    const body = $('#profile-body');
    if (!body) return;
    const c = Profile.c;
    if (!c) return;
    // 避免重复注入
    if (body.querySelector('#pc-char-details-card')) return;
    // 从后端获取角色配置
    let charCfg = {};
    try {
      charCfg = await this.api('/api/pc/character/get?name=' + encodeURIComponent(c.name));
    } catch (e) {}
    // 找到人设卡片，在它后面插入
    const cards = body.querySelectorAll('.p-card');
    const detailCard = document.createElement('div');
    detailCard.id = 'pc-char-details-card';
    detailCard.className = 'p-card';
    detailCard.innerHTML = `
      <div class="p-subtitle">角色细节</div>
      <div class="p-row">
        <div class="p-row-label"><div>年龄</div></div>
        <input id="pc-cd-age" type="text" placeholder="如：18" value="${charCfg.age || ''}" style="width:120px;padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;">
      </div>
      <div class="p-row">
        <div class="p-row-label"><div>职业/身份</div></div>
        <input id="pc-cd-occupation" type="text" placeholder="如：学生、画师" value="${charCfg.occupation || ''}" style="flex:1;padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;">
      </div>
      <div class="p-row">
        <div class="p-row-label"><div>生日</div></div>
        <input id="pc-cd-birthday" type="text" placeholder="如：3月15日" value="${charCfg.birthday || ''}" style="width:140px;padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;">
      </div>
      <div class="p-row">
        <div class="p-row-label"><div>口头禅</div></div>
        <input id="pc-cd-catchphrase" type="text" placeholder="如：哼、才不是呢" value="${charCfg.catchphrase || ''}" style="flex:1;padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;">
      </div>
      <div class="p-row">
        <div class="p-row-label"><div>喜欢的东西</div></div>
        <input id="pc-cd-likes" type="text" placeholder="如：甜食、猫咪" value="${charCfg.likes || ''}" style="flex:1;padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;">
      </div>
      <div class="p-row">
        <div class="p-row-label"><div>害怕/禁忌</div></div>
        <input id="pc-cd-fears" type="text" placeholder="如：怕黑、讨厌被忽略" value="${charCfg.fears || ''}" style="flex:1;padding:8px 10px;border:1px solid #e0e0e0;border-radius:8px;font-size:14px;">
      </div>
    `;
    // 插入到人设卡片后面（第二个卡片）
    if (cards.length >= 2) {
      cards[1].parentNode.insertBefore(detailCard, cards[1].nextSibling);
    } else {
      body.appendChild(detailCard);
    }
    // 保存事件：失焦时同步到后端
    const fields = ['age', 'occupation', 'birthday', 'catchphrase', 'likes', 'fears'];
    fields.forEach(f => {
      const el = document.getElementById('pc-cd-' + f);
      if (el) {
        el.addEventListener('change', () => this._syncCharToBackend(c.name));
      }
    });
  },

  async _syncCharToBackend(name) {
    const fields = ['age', 'occupation', 'birthday', 'catchphrase', 'likes', 'fears'];
    const patch = {};
    fields.forEach(f => {
      const el = document.getElementById('pc-cd-' + f);
      if (el) patch[f] = el.value.trim();
    });
    // 先获取现有配置，再合并
    try {
      const existing = await this.api('/api/pc/character/get?name=' + encodeURIComponent(name));
      const data = Object.assign({}, existing, patch, { character_name: name });
      await this.api('/api/pc/character/save', data, 'POST');
    } catch (e) {
      // 角色不存在则新建
      const data = Object.assign({ character_name: name, personality: '', call_user: '你' }, patch);
      try { await this.api('/api/pc/character/save', data, 'POST'); } catch (e2) {}
    }
  },

  async _injectStickerManager() {
    const body = $('#profile-body');
    if (!body) return;
    if (body.querySelector('#pc-sticker-card')) return;
    const card = document.createElement('div');
    card.id = 'pc-sticker-card';
    card.className = 'p-card';
    card.innerHTML = `
      <div class="p-subtitle">表情包库</div>
      <div style="margin-bottom:12px;">
        <button id="pc-sticker-upload" style="padding:8px 16px;background:#f0f0f0;border:none;border-radius:8px;cursor:pointer;font-size:14px;">+ 上传表情包</button>
        <input type="file" id="pc-sticker-file" accept="image/*" multiple style="display:none;">
        <span id="pc-sticker-count" style="margin-left:12px;color:#888;font-size:13px;"></span>
      </div>
      <div id="pc-sticker-grid" style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;"></div>
    `;
    // 插入到角色细节卡片后面
    const detailCard = body.querySelector('#pc-char-details-card');
    if (detailCard) {
      detailCard.parentNode.insertBefore(card, detailCard.nextSibling);
    } else {
      body.appendChild(card);
    }
    // 上传逻辑
    const self = this;
    const fileInput = card.querySelector('#pc-sticker-file');
    card.querySelector('#pc-sticker-upload').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async (e) => {
      const files = e.target.files;
      for (const file of files) {
        // 转成base64
        const reader = new FileReader();
        await new Promise((resolve) => {
          reader.onload = resolve;
          reader.readAsDataURL(file);
        });
        try {
          await self.api('/api/pc/sticker/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: file.name, data: reader.result })
          });
        } catch (err) {
          console.error('上传失败', err);
        }
      }
      fileInput.value = '';
      self._loadStickerGrid();
    });
    this._loadStickerGrid();
  },

  async _loadStickerGrid() {
    const grid = document.getElementById('pc-sticker-grid');
    const countEl = document.getElementById('pc-sticker-count');
    if (!grid) return;
    try {
      const data = await this.api('/api/pc/sticker/list');
      const stickers = data.stickers || [];
      if (countEl) countEl.textContent = '共 ' + stickers.length + ' 张';
      grid.innerHTML = '';
      stickers.forEach(s => {
        const item = document.createElement('div');
        item.style.cssText = 'position:relative;width:100%;padding-top:100%;border-radius:8px;overflow:hidden;background:#f5f5f5;cursor:pointer;';
        item.innerHTML = `<img src="${s.url}" style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover;">
          <button class="pc-sticker-del" data-fn="${s.filename}" style="position:absolute;top:2px;right:2px;width:20px;height:20px;border:none;border-radius:50%;background:rgba(0,0,0,0.5);color:#fff;font-size:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;">×</button>`;
        item.querySelector('.pc-sticker-del').addEventListener('click', async (e) => {
          e.stopPropagation();
          try {
            await this.api('/api/pc/sticker/delete', { filename: s.filename }, 'POST');
            this._loadStickerGrid();
          } catch (_) {}
        });
        grid.appendChild(item);
      });
    } catch (_) {}
  },



  wrapSwitchTab() {
    const self = this;
    const orig = window.switchTab;
    if (typeof orig !== 'function') return;
    window.switchTab = function (name) {
      orig(name);
      // 窗口按钮只在桌面端注入
      if (window.desktopWin && window.desktopWin.isDesktop) {
        try { self.injectWinButtons(); } catch (_) {}
      }
      // 人格设置按钮所有端都注入
      if (name === 'persona') {
        setTimeout(() => self.enhancePersonaPage(), 50);
      }
    };
    setTimeout(() => {
      if (window.desktopWin && window.desktopWin.isDesktop) {
        self.injectWinButtons();
      }
      self.enhancePersonaPage();
    }, 0);
  },

  injectWinButtons() {
    const box = document.getElementById('topbar-right');
    if (!box || box.querySelector('#pc-win-btns') || !window.desktopWin) return;
    const grp = h('span', { id: 'pc-win-btns', style: 'display:inline-flex;gap:2px' });
    const minBtn = h('button', { class: 'pc-win-btn', 'aria-label': '最小化', title: '最小化' });
    minBtn.innerHTML = '<svg viewBox="0 0 16 16"><rect x="3" y="7.3" width="10" height="1.4" fill="currentColor"/></svg>';
    minBtn.addEventListener('click', () => window.desktopWin.minimize());
    const closeBtn = h('button', { class: 'pc-win-btn close', 'aria-label': '关闭', title: '关闭' });
    closeBtn.innerHTML = '<svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" fill="none" stroke-linecap="round"/></svg>';
    closeBtn.addEventListener('click', () => window.desktopWin.close());
    grp.appendChild(minBtn);
    grp.appendChild(closeBtn);
    box.appendChild(grp);
    // 同时美化聊天头部按钮
    this.beautifyChatHeader();
    // 渲染消息中的表情包
    this.renderStickersInMessages();
    // 定期渲染新消息中的表情包
    if (!this._stickerRenderTimer) {
      this._stickerRenderTimer = setInterval(() => this.renderStickersInMessages(), 2000);
    }
  },

  /* ================= 数据迁移（浏览器 localStorage → 桌面端） ================= */
  /* 解决问题2：原网页端人格/记忆存在 localStorage(aiwechat:v9)，换环境读不到 */

  openMigrateSheet() {
    const exportScript =
      "var d=localStorage.getItem('aiwechat:v9');" +
      "if(!d){alert('原页面没有数据');}else{var b=new Blob([d],{type:'application/json'});" +
      "var a=document.createElement('a');a.href=URL.createObjectURL(b);" +
      "a.download='aiwechat-v9-backup.json';a.click();}";
    const scriptArea = h('textarea', { readonly: true, style: 'width:100%;height:64px;font-size:11px;font-family:monospace;border-radius:8px;padding:8px;resize:none' });
    scriptArea.value = exportScript;
    const copyBtn = h('button', { class: 'btn btn-plain', style: 'margin-top:8px', text: '复制这段脚本' });
    copyBtn.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(exportScript); toast('已复制，去原浏览器控制台粘贴执行'); }
      catch (_) { scriptArea.select(); try { document.execCommand('copy'); toast('已复制'); } catch (e) { toast('请手动选中复制'); } }
    });
    const pasteArea = h('textarea', { placeholder: '在此粘贴备份 JSON 文本（或点下面按钮选文件）…', style: 'width:100%;height:110px;font-size:12px;border-radius:8px;padding:8px;margin-top:10px;resize:none' });
    const fileInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
    const fileBtn = h('button', { class: 'btn btn-plain', text: '选择 .json 文件' });
    fileBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      const f = fileInput.files[0]; if (!f) return;
      const r = new FileReader();
      r.onload = () => { pasteArea.value = String(r.result || ''); toast('文件已读取，点「导入并重载」'); };
      r.onerror = () => toast('文件读取失败');
      r.readAsText(f, 'utf-8');
    });
    const goBtn = h('button', { class: 'btn btn-primary', text: '导入并重载' });
    goBtn.addEventListener('click', () => {
      const txt = pasteArea.value.trim();
      if (!txt) { toast('请先粘贴或选择备份文件'); return; }
      let obj;
      try { obj = JSON.parse(txt); }
      catch (e) { toast('JSON 解析失败：' + e.message, 3500); return; }
      if (!obj || typeof obj !== 'object' || !obj.version) { toast('不是有效的备份（缺少 version 字段）'); return; }
      if (!confirm('将用这份备份覆盖当前桌面端的人格/记忆/对话数据，确认导入？')) return;
      try {
        Store.importData(obj);
        toast('导入成功，即将重载…');
        setTimeout(() => location.reload(), 800);
      } catch (e) {
        toast('导入失败：' + e.message, 3500);
      }
    });
    const cancelBtn = h('button', { class: 'btn btn-plain', text: '取消' });
    cancelBtn.addEventListener('click', () => Sheet.close());
    Sheet.open(h('div', {},
      h('div', { class: 'sheet-title', text: '迁移网页端数据' }),
      h('div', { class: 'help-box', text:
        '把原浏览器里的人格/记忆/对话记录搬到桌面端：\n' +
        '1. 在原浏览器打开本项目当前网页（地址栏中的 127.0.0.1 端口）\n' +
        '2. 按 F12 打开控制台，粘贴下方脚本回车，会下载 aiwechat-v9-backup.json\n' +
        '3. 回到这里点「选择 .json 文件」选刚才的 json，或直接把内容粘贴到框里\n' +
        '4. 点「导入并重载」' }),
      scriptArea, copyBtn, pasteArea, fileInput,
      h('div', { style: 'display:flex;gap:8px;margin-top:10px' }, fileBtn, goBtn, cancelBtn)));
  },

  /* ================= AI 内核设置弹窗 ================= */
  /* 复用原 overlay 显示/隐藏机制：默认隐藏，加 .open 才显示。
     原项目用 #settings-page 写隐藏规则（靠 ID），新建 #pc-settings-page 没有，
     必须自己补一段默认隐藏的样式，否则会平铺显示+挡住下层点击。 */

  buildSettingsOverlay() {
    if (!document.getElementById('pc-settings-style')) {
      const st = document.createElement('style');
      st.id = 'pc-settings-style';
      st.textContent =
        '#pc-settings-page{' +
          'position:absolute;inset:0;z-index:27;background:#F2F2F2;' +
          'display:flex;flex-direction:column;' +
          'transform:translateX(100%);transition:transform 0.25s ease;' +
          'visibility:hidden;}' +
        '#pc-settings-page.open{transform:translateX(0);visibility:visible;}' +
        '#pc-settings-body{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:12px 12px calc(20px + var(--safe-bottom,0px));}' +
        '@media(min-width:860px){#pc-settings-page{left:380px;border-left:0.5px solid var(--border,#e0e0e0);}}';
      document.head.appendChild(st);
    }
    if (document.getElementById('pc-settings-page')) return; // 防重
    const page = h('section', { id: 'pc-settings-page', class: 'overlay' },
      h('header', { class: 'profile-header' },
        h('button', { id: 'pc-settings-back', 'aria-label': '返回', text: '‹' }),
        h('div', { class: 'profile-header-title', text: 'AI 内核设置' }),
        h('button', { id: 'pc-settings-done', text: '完成', style: 'padding:6px 16px;border:none;border-radius:16px;background:var(--green,#141414);color:#fff;font-size:14px;font-weight:600;cursor:pointer' }),
      ),
      h('div', { id: 'pc-settings-body' }),
    );
    document.getElementById('app').appendChild(page);
    this.settingsBodyEl = page.querySelector('#pc-settings-body');
    page.querySelector('#pc-settings-back').addEventListener('click', () => this.closeSettings());
    page.querySelector('#pc-settings-done').addEventListener('click', () => this.closeSettings());
    this.renderSettingsBody();
  },

  openSettings() {
    if (!this.backendOk) { toast('后端服务未连接，PC 增强设置不可用'); return; }
    document.getElementById('pc-settings-page').classList.add('open');
    this.renderSettingsBody();
  },
  closeSettings() {
    const p = document.getElementById('pc-settings-page');
    if (p) p.classList.remove('open');
  },

  save(patch, inputEl) {
    this.api('/api/pc/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }).then((cfg) => {
      this.cfg = cfg;
      toast('已生效 ✓（无需重启）');
      this.renderSettingsBody();
    }).catch((e) => {
      toast('保存失败：' + e.message, 3200);
      if (inputEl) try { inputEl.classList.add('error'); } catch (_) {}
    });
  },

  renderSettingsBody() {
    const cfg = this.cfg || {};
    const body = this.settingsBodyEl;
    body.innerHTML = '';

    /* ★ 聊天模型/记忆提炼模型已迁至「人格设置」（角色卡），AI内核弹窗不再提供（2026-09-11）。 */

    /* ---- 记忆 ---- */
    const memGroup = h('div', { class: 'list-group' },
      h('div', { class: 'group-title', text: '记忆系统' }));
    const intervalInput = h('input', { type: 'number', min: '2', max: '20', value: String(cfg.AUTO_MEMORY_INTERVAL) });
    intervalInput.style.width = '80px';
    intervalInput.addEventListener('change', () => {
      const n = parseInt(intervalInput.value, 10);
      if (!(n >= 2 && n <= 20)) { toast('轮次必须在 2~20 之间'); intervalInput.value = String(cfg.AUTO_MEMORY_INTERVAL); return; }
      this.save({ AUTO_MEMORY_INTERVAL: n });
    });
    memGroup.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '自动记忆提炼轮次' }),
        h('div', { class: 'row-sub', text: '每 N 轮对话自动提炼一次（2~20，默认 4）' })),
      intervalInput));
    memGroup.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '后端长期记忆' }),
        h('div', { class: 'row-sub', text: '在「记忆存储」页可导入导出' })),
      h('div', { class: 'row-arrow', text: (cfg.memory_count != null ? cfg.memory_count : '—') + ' 条' })));

    /* ---- 主动发言 ---- */
    const idleGroup = h('div', { class: 'list-group' },
      h('div', { class: 'group-title', text: '闲置主动发言' }));
    // ★ 2026-09-14 改造：这里原先有**第二套**档位选择器（10–20/20–40/30–60/40–90/60–120
    //   ＋"自定义"），和「全局设置 → 主动发言间隔」写的是同一对 key
    //   （IDLE_TRIGGER_MIN/MAX_MINUTES）。两套控件 = 两个口径，用户看到的是哪个生效都说不清。
    //   现在统一成和全局设置**同一形态的自由输入**：两个分钟数字框。
    //   口径：上限是主设定，下限没被手动改过时自动取上限一半（填 120 → 60–120）。
    const lo = cfg.IDLE_TRIGGER_MIN_MINUTES, hi = cfg.IDLE_TRIGGER_MAX_MINUTES;
    const loInput = h('input', { type: 'number', min: '1', max: '1440', value: String(lo), style: 'width:88px' });
    const hiInput = h('input', { type: 'number', min: '1', max: '1440', value: String(hi), style: 'width:88px' });
    let pcLoAuto = Math.round(Number(hi) / 2) === Number(lo);
    const applyInterval = (src) => {
      let l = Math.round(Number(loInput.value) || 0);
      let r = Math.round(Number(hiInput.value) || 0);
      r = Math.min(1440, Math.max(1, r || 1));
      if (src === 'lo' && l > 0) pcLoAuto = false;
      if (src === 'hi' && pcLoAuto) l = Math.round(r / 2);
      l = Math.min(1440, Math.max(1, l || 1));
      if (r < l) { const t = l; l = r; r = t; }
      loInput.value = String(l); hiInput.value = String(r);
      this.save({ IDLE_TRIGGER_MIN_MINUTES: l, IDLE_TRIGGER_MAX_MINUTES: r });
    };
    loInput.addEventListener('change', () => applyInterval('lo'));
    hiInput.addEventListener('change', () => applyInterval('hi'));
    idleGroup.appendChild(h('div', { class: 'setting-row', style: 'display:block' },
      h('div', { class: 'row-label' },
        h('div', { text: '主动发言间隔' }),
        h('div', { class: 'row-sub', text: '闲置多久后 AI 会主动找你，在此区间内随机；只改上限时下限自动折半（与「全局设置」同一项）' })),
      h('div', { style: 'display:flex;gap:8px;margin-top:8px;align-items:center' },
        loInput, h('span', { text: '—' }), hiInput, h('span', { text: '分钟' }))));

    const idleSwitch = h('label', { class: 'switch' },
      h('input', { type: 'checkbox' }),
      h('span', { class: 'track' }),
      h('span', { class: 'thumb' }));
    const idleCheck = idleSwitch.querySelector('input');
    idleCheck.checked = !!cfg.IDLE_AGENT_ENABLED;
    idleCheck.addEventListener('change', () => this.save({ IDLE_AGENT_ENABLED: idleCheck.checked }));
    idleGroup.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '闲置主动 Agent' }),
        h('div', { class: 'row-sub', text: '关闭后后端不再主动推送消息（原 App 内主动陪伴不受影响）' })),
      idleSwitch));

    // ★ 2026-09-14 移除：PC 增强设置里的「全局主动发言时段」控件。
    //   主动消息时段已统一到**角色卡**（人格设置 → 主动与免打扰 → 主动消息可用时段），
    //   全局这份与角色设置冲突，且是"时段限制不管用"的根源之一。只保留一个数据源。
    const _pcIdleTimeRemoved = true;

    /* ---- Key ---- */
    const keyGroup = h('div', { class: 'list-group' },
      h('div', { class: 'group-title', text: '后端 DeepSeek API Key（可选）' }));
    const keyInput = h('input', { type: 'text', placeholder: cfg.api_key_set ? '已配置（输入可覆盖）' : 'sk-...' });
    keyInput.addEventListener('change', () => {
      const v = keyInput.value.trim();
      if (!v) return;
      this.save({ api_key: v });
    });
    keyGroup.appendChild(h('div', { class: 'setting-row', style: 'display:block' }, keyInput));
    keyGroup.appendChild(h('div', { class: 'help-box', text:
      'Key 读取优先级：环境变量 > 项目 .env > config.json > 此处。\n聊天走代理模式时前端无需填 Key。' }));

    body.appendChild(memGroup);
    body.appendChild(idleGroup);
    body.appendChild(keyGroup);

    /* ---- 数据迁移 ---- */
    body.appendChild(h('div', { class: 'list-group' },
      h('div', { class: 'group-title', text: '数据迁移' }),
      h('div', { class: 'setting-row', id: 'pc-migrate-row' },
        h('div', { class: 'row-label' },
          h('div', { text: '迁移网页端人格/记忆数据' }),
          h('div', { class: 'row-sub', text: '把原浏览器里的联系人、人格、记忆文件夹、对话记录搬到桌面端' })),
        h('button', { class: 'btn btn-primary', style: 'flex-shrink:0', text: '迁移',
          onclick: () => this.openMigrateSheet() }))));
  },

  /* 入口：挂到原「全局设置」页 */
  wrapSettingsPage() {
    const self = this;
    const orig = window.renderSettings;
    if (typeof orig !== 'function') return;
    window.renderSettings = function () {
      orig();
      try { self.injectSettingsEntry(); } catch (_) {}
    };
  },

  injectSettingsEntry() {
    const wrap = document.getElementById('settings-body');
    if (!wrap) return;
    // PC增强入口
    if (!wrap.querySelector('#pc-entry-row')) {
      const row = h('div', { class: 'list-group', id: 'pc-entry-group' },
        h('div', { class: 'group-title', text: 'PC 桌面增强' }));
      const entry = h('div', { class: 'setting-row', id: 'pc-entry-row' },
        h('div', { class: 'row-label' },
          h('div', { text: 'AI 内核设置' }),
          h('div', { class: 'row-sub', text: '记忆提炼轮次 / 主动发言时间范围 / API Key' })),
        h('div', { class: 'row-arrow', text: '›' }));
      entry.addEventListener('click', () => this.openSettings());
      row.appendChild(entry);
      wrap.appendChild(row);
    }
  },

  /* ================= 统一记忆库（磁盘文件 + 数据库长期记忆合并） ================= */

  memTab: 'library',   // 'library' | 'extract'
  memData: null,
  memLoading: false,

  wrapMemStore() {
    const self = this;
    // 完全覆盖原 MemStore.render，PC 端走后端统一记忆库
    MemStore.render = function () {
      self.renderMemStore();
    };
    // 覆盖 open，确保标题正确
    const origOpen = MemStore.open.bind(MemStore);
    MemStore.open = function (contactId) {
      self.contactId = contactId;
      self.memTab = 'library';
      self.memData = null;
      origOpen(contactId);
      // 改标题
      const titleEl = document.querySelector('#memstore-page .profile-header-title');
      if (titleEl) titleEl.textContent = '记忆库';
    };
  },

  async renderMemStore() {
    const body = $('#memstore-body');
    if (!body) return;
    body.innerHTML = '';

    // Tab 栏
    const tabBar = h('div', { class: 'chips', style: 'padding:10px 12px 0;gap:6px' });
    const tabLib = h('button', { class: 'chip' + (this.memTab === 'library' ? ' selected' : ''), text: '记忆库' });
    const tabExt = h('button', { class: 'chip' + (this.memTab === 'extract' ? ' selected' : ''), text: '记忆提炼' });
    tabLib.addEventListener('click', () => { this.memTab = 'library'; this.renderMemStore(); });
    tabExt.addEventListener('click', () => { this.memTab = 'extract'; this.renderMemStore(); });
    tabBar.appendChild(tabLib);
    tabBar.appendChild(tabExt);
    body.appendChild(tabBar);

    if (this.memTab === 'library') {
      await this.renderMemLibrary(body);
    } else {
      await this.renderMemExtract(body);
    }
  },

  currentCharName() {
    return (window.Chat && Chat.contact && Chat.contact.name) ? Chat.contact.name : '';
  },

  async loadMemData() {
    if (this.memData && !this._memForceReload) return this.memData;
    this._memForceReload = false;
    this.memLoading = true;
    const cname = this.currentCharName();
    try {
      this.memData = await this.api('/api/pc/memory/all?character=' + encodeURIComponent(cname));
    } catch (e) {
      this.memData = { disk: [], db: [], disk_count: 0, db_count: 0, total: 0, error: e.message };
    }
    this.memLoading = false;
    return this.memData;
  },

  async renderMemLibrary(body) {
    const data = await this.loadMemData();
    const cname = this.currentCharName();

    // 角色上下文横幅
    body.appendChild(h('div', { class: 'ms-char-banner' },
      h('span', { class: 'ms-char-avatar', text: cname ? (cname[0] || '?') : '★' }),
      h('div', { class: 'ms-char-info' },
        h('div', { class: 'ms-char-name', text: cname ? (cname + ' 的记忆库') : '通用记忆库' }),
        h('div', { class: 'ms-char-sub', text: cname
          ? '只显示「' + cname + '」的专属记忆 + 通用记忆'
          : '未选中角色，仅显示通用记忆' }),
      ),
    ));

    body.appendChild(h('div', { class: 'help-box', style: 'margin:2px 2px 12px', text:
      '这里是 TA 的记忆库：磁盘文件（长篇设定/对话记录）和长期记忆（AI 自动提炼 + 手动记住）合并管理。\n' +
      '所有内容都会在聊天时自动注入，关闭程序也不会丢失。' }));

    // 工具栏
    const addMenuItems = [
      { label: '手动写一条记忆', action: () => this.openMemAddSheet() },
      { label: '粘贴文本（存为磁盘文件）', action: () => this.openMemPasteSheet() },
      { label: '导入文件（txt/md）', action: () => this._memFileInput.click() },
    ];
    let addMenuOpen = false;
    const addMenu = h('div', { class: 'ms-add-menu', hidden: true });
    for (const it of addMenuItems) {
      const b = h('button', { class: 'ms-add-menu-item', text: it.label });
      b.addEventListener('click', () => { addMenu.hidden = true; addMenuOpen = false; it.action(); });
      addMenu.appendChild(b);
    }
    const addBtn = h('button', { class: 'chip', text: '＋ 添加' });
    addBtn.addEventListener('click', () => { addMenuOpen = !addMenuOpen; addMenu.hidden = !addMenuOpen; });

    const tool = h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: '共 ' + (data.total || 0) + ' 条' }),
      addBtn,
    );
    body.appendChild(tool);
    body.appendChild(addMenu);

    // 文件导入 input
    const fileInput = h('input', { type: 'file', accept: '.txt,.md,text/plain,text/markdown', hidden: true });
    fileInput.addEventListener('change', () => {
      const f = fileInput.files[0];
      if (!f) return;
      if (f.size > 500 * 1024) { toast('文件太大（限 500KB）'); fileInput.value = ''; return; }
      const r = new FileReader();
      r.onload = async () => {
        const content = String(r.result || '');
        try {
          await this.api('/api/library/write', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: f.name, content }),
          });
          this._memForceReload = true;
          this.renderMemStore();
          toast('已导入「' + f.name + '」');
        } catch (e) { toast('导入失败：' + e.message); }
      };
      r.readAsText(f, 'utf-8');
      fileInput.value = '';
    });
    this._memFileInput = fileInput;
    body.appendChild(fileInput);

    if (data.error) {
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('alert'),
        h('div', { text: '加载失败：' + data.error }),
      ));
      return;
    }

    // --- 磁盘文件分组 ---
    if (data.disk && data.disk.length) {
      body.appendChild(h('div', { class: 'ms-group-title', text: '📁 磁盘文件（' + data.disk_count + '）' }));
      for (const item of data.disk) {
        body.appendChild(this.buildMemCard(item, 'disk'));
      }
    }

    // --- 数据库长期记忆分组 ---
    if (data.db && data.db.length) {
      body.appendChild(h('div', { class: 'ms-group-title', text: '🧠 长期记忆（' + data.db_count + '）' }));
      for (const item of data.db) {
        body.appendChild(this.buildMemCard(item, 'db'));
      }
    }

    if (!data.total) {
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('layers'),
        h('div', { text: '记忆库是空的' }),
        h('div', { style: 'margin-top:8px', text: '点「添加」手动写一条，或导入 txt/md 文件' }),
      ));
    }
  },

  buildMemCard(item, kind) {
    const isDisk = kind === 'disk';
    const title = isDisk ? (item.title || '未命名') : '';
    const content = item.content || '';
    const date = item.date || '';
    const badge = isDisk ? '文件' : '记忆';

    const contentEl = h('div', { class: 'ms-content', text: content });
    const card = h('div', { class: 'ms-card' + (isDisk ? ' disk' : ' db') },
      h('div', { class: 'ms-head' },
        h('span', { class: 'ms-badge ' + (isDisk ? 'file' : 'manual'), text: badge }),
        title ? h('span', { class: 'ms-title-inline', style: 'font-weight:600;margin-left:6px', text: title }) : null,
        h('span', { class: 'ms-date', text: date }),
        h('button', { class: 'ms-op', text: '编辑' }),
        h('button', { class: 'ms-op del', text: '删除' }),
      ),
      contentEl,
    );
    // 长内容底部渐隐提示
    if (content.length > 120) {
      card.appendChild(h('div', { class: 'ms-fade' }));
    }
    const ops = card.querySelectorAll('.ms-op');
    ops[0].addEventListener('click', () => this.openMemEditSheet(item, kind));
    ops[1].addEventListener('click', async () => {
      if (!confirm('删除这条' + (isDisk ? '文件' : '记忆') + '？')) return;
      try {
        await this.api('/api/pc/memory/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // ★ P1-8：补全 ai_companion_session_id，与其它模块的 fallback 链一致
          body: JSON.stringify({ id: item.id, session_id: (window.Chat && Chat.sessionId) || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default', character_id: (window.Chat && Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default' }),
        });
        this._memForceReload = true;
        this.renderMemStore();
        toast('已删除');
      } catch (e) { toast('删除失败：' + e.message); }
    });
    return card;
  },

  openMemAddSheet() {
    const ta = h('textarea', { placeholder: '写下想让 TA 记住的事…（一条一句话）', style: 'min-height:120px' });
    const form = h('div', {},
      field('记忆内容 *', ta),
      h('button', { class: 'btn btn-primary', text: '存入记忆库' }),
      h('button', { class: 'btn btn-plain', text: '取消' }),
    );
    const btns = form.querySelectorAll('button');
    btns[0].addEventListener('click', async () => {
      const content = ta.value.trim();
      if (!content) { toast('内容不能为空'); return; }
      try {
        const cname = (window.Chat && Chat.contact && Chat.contact.name) ? Chat.contact.name : '';
        await this.api('/api/pc/memory/add', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content, character_name: cname }),
        });
        this._memForceReload = true;
        this.renderMemStore();
        Sheet.close();
        toast('已存入记忆库');
      } catch (e) { toast('保存失败：' + e.message); }
    });
    btns[1].addEventListener('click', () => Sheet.close());
    Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '手动添加记忆' }), form));
  },

  openMemPasteSheet() {
    const nameInput = h('input', { type: 'text', placeholder: '文件名（如：助手设定）', value: '' });
    const ta = h('textarea', { placeholder: '粘贴长篇内容…会存为磁盘文件，聊天时整体注入', style: 'min-height:200px' });
    const form = h('div', {},
      field('文件名', nameInput),
      field('内容 *', ta),
      h('button', { class: 'btn btn-primary', text: '存入记忆库' }),
      h('button', { class: 'btn btn-plain', text: '取消' }),
    );
    const btns = form.querySelectorAll('button');
    btns[0].addEventListener('click', async () => {
      const name = nameInput.value.trim() || ('记忆_' + Date.now());
      const content = ta.value.trim();
      if (!content) { toast('内容不能为空'); return; }
      try {
        await this.api('/api/library/write', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, content }),
        });
        this._memForceReload = true;
        this.renderMemStore();
        Sheet.close();
        toast('已存入为文件');
      } catch (e) { toast('保存失败：' + e.message); }
    });
    btns[1].addEventListener('click', () => Sheet.close());
    Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '粘贴文本为文件' }), form));
  },

  async openMemEditSheet(item, kind) {
    const isDisk = kind === 'disk';
    let fullContent = item.content || '';
    // 磁盘文件需要读取完整内容
    if (isDisk && item.id) {
      try {
        const name = item.id.replace('disk:', '');
        const r = await this.api('/api/library/read?name=' + encodeURIComponent(name));
        fullContent = r.content || fullContent;
      } catch (_) {}
    }
    const nameInput = isDisk
      ? h('input', { type: 'text', value: item.title || '' })
      : null;
    const ta = h('textarea', { style: 'min-height:200px' });
    ta.value = fullContent;
    const children = [field('内容 *', ta)];
    if (isDisk) children.unshift(field('文件名', nameInput));
    const form = h('div', {},
      ...children,
      h('button', { class: 'btn btn-primary', text: '保存修改' }),
      h('button', { class: 'btn btn-plain', text: '取消' }),
    );
    const btns = form.querySelectorAll('button');
    btns[0].addEventListener('click', async () => {
      const content = ta.value;
      if (!content.trim()) { toast('内容不能为空'); return; }
      try {
        if (isDisk) {
          const name = (nameInput.value.trim() || item.title || '未命名') + '.txt';
          await this.api('/api/library/write', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, content }),
          });
          // 如果改名了，删除旧文件
          if (item.id !== 'disk:' + name) {
            try { await this.api('/api/pc/memory/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: item.id, session_id: (window.Chat && Chat.sessionId) || 'default', character_id: (window.Chat && Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default' }) }); } catch (_) {}
          }
        } else {
          // 数据库记忆：删除旧的再加新的（简化处理）
          await this.api('/api/pc/memory/delete', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: item.id, session_id: (window.Chat && Chat.sessionId) || 'default', character_id: (window.Chat && Chat.contact && (Chat.contact.id || Chat.contact.name)) || 'default' }),
          });
          const cname = (window.Chat && Chat.contact && Chat.contact.name) ? Chat.contact.name : '';
          await this.api('/api/pc/memory/add', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content: content.trim(), character_name: cname }),
          });
        }
        this._memForceReload = true;
        this.renderMemStore();
        Sheet.close();
        toast('已保存');
      } catch (e) { toast('保存失败：' + e.message); }
    });
    btns[1].addEventListener('click', () => Sheet.close());
    Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '编辑' + (isDisk ? '文件' : '记忆') }), form));
  },

  async renderMemExtract(body) {
    const data = await this.loadMemData();

    body.appendChild(h('div', { class: 'help-box', style: 'margin:2px 2px 12px', text:
      'AI 会在聊天中自动提炼值得记住的信息（每 N 轮一次），也可以点「立即提炼」从最近对话中提取。\n' +
      '这里的记忆会在聊天时自动注入 system prompt，支持加密导出备份，换设备可导入恢复。' }));

    // 工具栏
    const extractBtn = h('button', { class: 'chip primary', text: '✨ 立即提炼' });
    extractBtn.addEventListener('click', async () => {
      extractBtn.disabled = true;
      extractBtn.textContent = '提炼中…';
      try {
        const cname = this.currentCharName();
        const r = await this.api('/api/pc/memory/extract_now', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: 'default', character_name: cname }),
        });
        this._memForceReload = true;
        this.renderMemStore();
        toast('提炼完成：新增 ' + (r.extracted || 0) + ' 条（当前共 ' + (r.total || 0) + ' 条）');
      } catch (e) {
        toast('提炼失败：' + e.message, 3200);
        extractBtn.disabled = false;
        extractBtn.textContent = '✨ 立即提炼';
      }
    });
    const expBtn = h('button', { class: 'chip', text: '⬇ 导出' });
    expBtn.addEventListener('click', () => this.openExportSheet());
    const impBtn = h('button', { class: 'chip', text: '⬆ 导入' });
    impBtn.addEventListener('click', () => this.openImportSheet());

    const tool = h('div', { class: 'sec-toolbar' },
      h('span', { class: 'sec-toolbar-title', text: '长期记忆（' + (data.db_count || 0) + ' 条）' }),
      extractBtn, expBtn, impBtn,
    );
    body.appendChild(tool);

    if (data.error) {
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('alert'),
        h('div', { text: '加载失败：' + data.error }),
      ));
      return;
    }

    if (!data.db || !data.db.length) {
      body.appendChild(h('div', { class: 'empty-state' },
        emptyIcon('layers'),
        h('div', { text: '还没有长期记忆' }),
        h('div', { style: 'margin-top:8px', text: '聊一会儿天，或点「立即提炼」从最近对话中提取' }),
      ));
      return;
    }

    for (const item of data.db) {
      body.appendChild(this.buildMemCard(item, 'db'));
    }
  },

  openExportSheet() {
    const fmtSel = h('select', {},
      h('option', { value: 'json', text: '加密 JSON（AES，需密钥）' }),
      h('option', { value: 'txt', text: '纯文本 TXT（每条一行）' }));
    const keyInput = h('input', { type: 'password', placeholder: '加密密钥（至少 4 位，JSON 必填）', style: 'margin-top:8px' });
    const goBtn = h('button', { class: 'btn btn-primary', style: 'margin-top:10px', text: '导出到本地' });
    const cancelBtn = h('button', { class: 'btn btn-plain', style: 'margin-top:10px', text: '取消' });
    cancelBtn.addEventListener('click', () => Sheet.close());
    goBtn.addEventListener('click', async () => {
      const fmt = fmtSel.value;
      const key = keyInput.value;
      if (fmt === 'json' && (!key || key.length < 4)) { toast('JSON 导出需要至少 4 位密钥'); return; }
      goBtn.disabled = true;
      goBtn.textContent = '导出中…';
      try {
        const url = '/api/pc/memory/export?format=' + encodeURIComponent(fmt)
          + '&key=' + encodeURIComponent(key);
        const res = await fetch(url);
        if (!res.ok) {
          const j = await res.json().catch(() => ({}));
          throw new Error((j.error && j.error.message) || ('导出失败 ' + res.status));
        }
        const blob = await res.blob();
        const cd = res.headers.get('Content-Disposition') || '';
        const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
        const fname = m ? decodeURIComponent(m[1]) : ('memory_backup.' + fmt);
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = fname;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
        Sheet.close();
        toast('已导出「' + fname + '」');
      } catch (e) {
        toast('导出失败：' + e.message, 3200);
        goBtn.disabled = false;
        goBtn.textContent = '导出到本地';
      }
    });
    Sheet.open(h('div', {},
      h('div', { class: 'sheet-title', text: '导出后端长期记忆' }),
      h('div', { class: 'help-box', text: '导出后端记忆库（含「记住：」和自动提炼的记忆）。\nTXT 可直接打开编辑；加密 JSON 换设备导入时需同一密钥。' }),
      fmtSel, keyInput,
      h('div', { style: 'display:flex;gap:8px' }, goBtn, cancelBtn)));
  },

  openImportSheet() {
    let file = null;
    const fileBtn = h('button', { class: 'btn btn-plain', text: '选择文件（.json / .txt）' });
    const fileLabel = h('div', { class: 'row-sub', text: '未选择文件' });
    const fileInput = h('input', { type: 'file', accept: '.json,.txt', hidden: true });
    fileBtn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      file = fileInput.files[0] || null;
      fileLabel.textContent = file ? file.name : '未选择文件';
    });
    const modeSel = h('select', { style: 'margin-top:8px' },
      h('option', { value: 'merge', text: '合并（与现有记忆去重合并）' }),
      h('option', { value: 'overwrite', text: '覆盖（清空现有记忆后导入）' }));
    const keyInput = h('input', { type: 'password', placeholder: '解密密钥（加密 JSON 必填）', style: 'margin-top:8px' });
    const goBtn = h('button', { class: 'btn btn-primary', style: 'margin-top:10px', text: '导入' });
    const cancelBtn = h('button', { class: 'btn btn-plain', style: 'margin-top:10px', text: '取消' });
    cancelBtn.addEventListener('click', () => Sheet.close());
    goBtn.addEventListener('click', async () => {
      if (!file) { toast('请先选择文件'); return; }
      const isJson = /\.json$/i.test(file.name);
      if (isJson && !keyInput.value) { toast('加密 JSON 需要输入密钥'); return; }
      goBtn.disabled = true;
      goBtn.textContent = '导入中…';
      try {
        const buf = await file.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let bin = '';
        const CHUNK = 0x8000;
        for (let i = 0; i < bytes.length; i += CHUNK) {
          bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
        }
        const b64 = btoa(bin);
        const result = await this.api('/api/pc/memory/import', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            filename: file.name,
            content_b64: b64,
            mode: modeSel.value,
            key: keyInput.value,
          }),
        });
        Sheet.close();
        toast('导入完成：' + (result.imported || 0) + ' 条（当前共 ' + (result.total || 0) + ' 条）');
      } catch (e) {
        toast('导入失败：' + e.message, 3500);
        goBtn.disabled = false;
        goBtn.textContent = '导入';
      }
    });
    Sheet.open(h('div', {},
      h('div', { class: 'sheet-title', text: '导入记忆备份' }),
      h('div', { class: 'help-box', text: '格式按扩展名自动识别：.json = 加密备份（需密钥），.txt = 纯文本（每行一条，UTF-8）。' }),
      h('div', { style: 'display:flex;gap:8px;align-items:center' }, fileBtn, fileLabel),
      fileInput, modeSel, keyInput,
      h('div', { style: 'display:flex;gap:8px' }, goBtn, cancelBtn)));
  },

  /* ---------------- 角色配置管理面板 ---------------- */

  charPanelEl: null,
  charFormEl: null,
  charListEl: null,
  charEditing: null,

  buildCharacterPanel() {
    if (this.charPanelEl) return;
    const panel = document.createElement('div');
    panel.id = 'pc-character-panel';
    panel.style.cssText = 'position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,0.4);';
    panel.innerHTML = `
      <div style="width:640px;max-height:85vh;background:#fff;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,0.2);display:flex;flex-direction:column;overflow:hidden;">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid #eee;">
          <div style="font-size:17px;font-weight:600;">角色配置管理</div>
          <button id="pc-char-close" style="border:none;background:none;font-size:20px;cursor:pointer;color:#888;width:30px;height:30px;border-radius:6px;">×</button>
        </div>
        <div id="pc-char-body" style="flex:1;overflow-y:auto;padding:20px;"></div>
      </div>`;
    document.body.appendChild(panel);
    this.charPanelEl = panel;
    this.charListEl = panel.querySelector('#pc-char-body');
    panel.querySelector('#pc-char-close').addEventListener('click', () => this.closeCharacterPanel());
    panel.addEventListener('click', (e) => { if (e.target === panel) this.closeCharacterPanel(); });
  },

  openCharacterPanel() {
    this.buildCharacterPanel();
    this.charPanelEl.style.display = 'flex';
    this.loadCharacterList();
  },

  closeCharacterPanel() {
    if (this.charPanelEl) this.charPanelEl.style.display = 'none';
    this.charEditing = null;
  },

  async loadCharacterList() {
    const body = this.charListEl;
    body.innerHTML = '<div style="text-align:center;color:#999;padding:20px;">加载中...</div>';
    try {
      const data = await this.api('/api/pc/character/list');
      const chars = data.characters || [];
      let html = `<button id="pc-char-new" style="width:100%;padding:14px;border:2px dashed #ccc;border-radius:10px;background:none;cursor:pointer;font-size:15px;color:#555;margin-bottom:16px;">＋ 创建新角色</button>`;
      if (chars.length === 0) {
        html += '<div style="text-align:center;color:#999;padding:30px;">还没有角色，点上方创建第一个</div>';
      } else {
        for (const c of chars) {
          html += `<div class="pc-char-item" data-name="${c.name}" style="display:flex;align-items:center;justify-content:space-between;padding:14px;border:1px solid #eee;border-radius:10px;margin-bottom:10px;cursor:pointer;">
            <div>
              <div style="font-weight:600;font-size:15px;">${c.name}</div>
              <div style="font-size:12px;color:#999;margin-top:2px;">${c.relationship || '未设定关系'} · 称呼用户「${c.call_user || '你'}」</div>
            </div>
            <div style="display:flex;gap:8px;">
              <button class="pc-char-edit" data-name="${c.name}" style="padding:6px 12px;border:1px solid #ddd;border-radius:6px;background:none;cursor:pointer;font-size:13px;">编辑</button>
              <button class="pc-char-del" data-name="${c.name}" style="padding:6px 12px;border:1px solid #fcc;border-radius:6px;background:none;cursor:pointer;font-size:13px;color:#e55;">删除</button>
            </div>
          </div>`;
        }
      }
      body.innerHTML = html;
      body.querySelector('#pc-char-new').addEventListener('click', () => this.showCharacterForm(null));
      body.querySelectorAll('.pc-char-edit').forEach(btn => {
        btn.addEventListener('click', (e) => { e.stopPropagation(); this.showCharacterForm(btn.dataset.name); });
      });
      body.querySelectorAll('.pc-char-del').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('确定删除角色「' + btn.dataset.name + '」？配置文件会被删除。')) return;
          await this.api('/api/pc/character/delete', { name: btn.dataset.name }, 'POST');
          this.loadCharacterList();
        });
      });
    } catch (e) {
      body.innerHTML = '<div style="text-align:center;color:#e55;padding:20px;">加载失败：' + e.message + '</div>';
    }
  },

  async showCharacterForm(name) {
    this.charEditing = name;
    const body = this.charListEl;
    let cfg = {
      character_name: '', self_name: '', call_user: '你',
      personality: '', dialogue_style: { action_brackets: true, prefer_length: 'medium', allow_emoji: false, open_question: true, empathy_first: true },
      tone_particles: [], worldview: '', relationship: '',
      age: '', occupation: '', hobbies: '', catchphrase: '', likes: '', fears: '', birthday: '',
    };
    if (name) {
      try { cfg = await this.api('/api/pc/character/get?name=' + encodeURIComponent(name)); } catch (e) {}
    }
    const s = cfg.dialogue_style || {};
    body.innerHTML = `
      <div style="margin-bottom:18px;">
        <div style="font-size:13px;color:#888;margin-bottom:6px;font-weight:600;">基本信息</div>
        <div style="display:flex;gap:12px;margin-bottom:12px;">
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">角色名称 *</label>
            <input id="pc-char-name" value="${cfg.character_name || ''}" style="width:100%;padding:10px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:14px;outline:none;transition:border-color .2s;" placeholder="例如：助手、星尘" onfocus="this.style.borderColor='#4a90d9'" onblur="this.style.borderColor='#e0e0e0'">
          </div>
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">自称</label>
            <input id="pc-char-self" value="${cfg.self_name || ''}" style="width:100%;padding:10px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:14px;outline:none;" placeholder="例如：我、人家">
          </div>
        </div>
        <div style="display:flex;gap:12px;margin-bottom:12px;">
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">怎么称呼你</label>
            <input id="pc-char-call" value="${cfg.call_user || '你'}" style="width:100%;padding:10px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:14px;outline:none;" placeholder="例如：宝、主人">
          </div>
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">你们的关系</label>
            <input id="pc-char-relation" value="${cfg.relationship || ''}" style="width:100%;padding:10px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:14px;outline:none;" placeholder="恋人、好友、师徒">
          </div>
        </div>
      </div>
      <div style="margin-bottom:18px;">
        <div style="font-size:13px;color:#888;margin-bottom:6px;font-weight:600;">人格设定</div>
        <textarea id="pc-char-personality" rows="3" style="width:100%;padding:10px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:14px;outline:none;resize:vertical;line-height:1.6;" placeholder="傲娇嘴硬心软 / 温柔成熟 / 清冷少年 等，描述性格、说话习惯、口头禅、禁忌">${cfg.personality || ''}</textarea>
      </div>
      <div style="margin-bottom:18px;">
        <div style="font-size:13px;color:#888;margin-bottom:8px;font-weight:600;">角色细节</div>
        <div style="display:flex;gap:12px;margin-bottom:10px;">
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">年龄</label>
            <input id="pc-char-age" value="${cfg.age || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：18">
          </div>
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">职业/身份</label>
            <input id="pc-char-occupation" value="${cfg.occupation || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：学生、画师">
          </div>
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">生日</label>
            <input id="pc-char-birthday" value="${cfg.birthday || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：3月15日">
          </div>
        </div>
        <div style="margin-bottom:10px;">
          <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">爱好</label>
          <input id="pc-char-hobbies" value="${cfg.hobbies || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：看动漫、听音乐、做饭">
        </div>
        <div style="display:flex;gap:12px;margin-bottom:10px;">
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">口头禅</label>
            <input id="pc-char-catchphrase" value="${cfg.catchphrase || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：哼、才不是呢">
          </div>
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">语气词（逗号分隔）</label>
            <input id="pc-char-tone" value="${(cfg.tone_particles||[]).join('、')}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="哼、啦、嘛、唔">
          </div>
        </div>
        <div style="display:flex;gap:12px;">
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">喜欢的东西</label>
            <input id="pc-char-likes" value="${cfg.likes || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：甜食、猫咪、下雨天">
          </div>
          <div style="flex:1;">
            <label style="display:block;font-size:12px;color:#999;margin-bottom:4px;">害怕/禁忌</label>
            <input id="pc-char-fears" value="${cfg.fears || ''}" style="width:100%;padding:9px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:13px;outline:none;" placeholder="例如：怕黑、讨厌被忽略">
          </div>
        </div>
      </div>
      <div style="margin-bottom:18px;">
        <div style="font-size:13px;color:#888;margin-bottom:8px;font-weight:600;">对话风格</div>
        <div style="display:flex;flex-wrap:wrap;gap:12px;font-size:13px;background:#f8f9fa;padding:12px;border-radius:10px;">
          <label style="display:flex;align-items:center;gap:5px;cursor:pointer;"><input type="checkbox" id="pc-char-action" ${s.action_brackets ? 'checked' : ''}> 括号动作描写</label>
          <label style="display:flex;align-items:center;gap:5px;cursor:pointer;"><input type="checkbox" id="pc-char-emoji" ${s.allow_emoji ? 'checked' : ''}> 允许emoji</label>
          <label style="display:flex;align-items:center;gap:5px;cursor:pointer;"><input type="checkbox" id="pc-char-question" ${s.open_question ? 'checked' : ''}> 结尾反问</label>
          <label style="display:flex;align-items:center;gap:5px;cursor:pointer;"><input type="checkbox" id="pc-char-empathy" ${s.empathy_first ? 'checked' : ''}> 优先共情</label>
        </div>
        <div style="margin-top:10px;font-size:13px;color:#666;display:flex;align-items:center;gap:12px;">
          <span>偏好篇幅：</span>
          <label style="cursor:pointer;"><input type="radio" name="pc-char-len" value="short" ${s.prefer_length==='short'?'checked':''}> 短句</label>
          <label style="cursor:pointer;"><input type="radio" name="pc-char-len" value="medium" ${s.prefer_length!=='short'&&s.prefer_length!=='long'?'checked':''}> 适中</label>
          <label style="cursor:pointer;"><input type="radio" name="pc-char-len" value="long" ${s.prefer_length==='long'?'checked':''}> 较长</label>
        </div>
      </div>
      <div style="margin-bottom:20px;">
        <div style="font-size:13px;color:#888;margin-bottom:6px;font-weight:600;">世界观 / 背景（可选）</div>
        <textarea id="pc-char-world" rows="2" style="width:100%;padding:10px 12px;border:1px solid #e0e0e0;border-radius:10px;font-size:14px;outline:none;resize:vertical;line-height:1.6;" placeholder="角色背景、你们的初始关系、特殊设定">${cfg.worldview || ''}</textarea>
      </div>
      <div style="display:flex;gap:10px;justify-content:flex-end;">
        <button id="pc-char-back" style="padding:10px 22px;border:1px solid #e0e0e0;border-radius:10px;background:#fff;cursor:pointer;font-size:14px;color:#666;">返回</button>
        <button id="pc-char-save" style="padding:10px 28px;border:none;border-radius:10px;background:linear-gradient(135deg,#4a90d9,#357abd);color:#fff;cursor:pointer;font-size:14px;font-weight:600;box-shadow:0 2px 8px rgba(74,144,217,0.3);">保存</button>
      </div>`;
    body.querySelector('#pc-char-back').addEventListener('click', () => this.loadCharacterList());
    body.querySelector('#pc-char-save').addEventListener('click', () => this.saveCharacter());
  },

  async saveCharacter() {
    const name = document.getElementById('pc-char-name').value.trim();
    if (!name) { alert('角色名称不能为空'); return; }
    const lenEl = document.querySelector('input[name="pc-char-len"]:checked');
    const toneStr = document.getElementById('pc-char-tone').value;
    const data = {
      character_name: name,
      self_name: document.getElementById('pc-char-self').value.trim(),
      call_user: document.getElementById('pc-char-call').value.trim() || '你',
      personality: document.getElementById('pc-char-personality').value.trim(),
      dialogue_style: {
        action_brackets: document.getElementById('pc-char-action').checked,
        allow_emoji: document.getElementById('pc-char-emoji').checked,
        open_question: document.getElementById('pc-char-question').checked,
        empathy_first: document.getElementById('pc-char-empathy').checked,
        prefer_length: lenEl ? lenEl.value : 'medium',
      },
      tone_particles: toneStr ? toneStr.split(/[,，、]/).map(s => s.trim()).filter(Boolean) : [],
      worldview: document.getElementById('pc-char-world').value.trim(),
      relationship: document.getElementById('pc-char-relation').value.trim(),
      age: document.getElementById('pc-char-age').value.trim(),
      occupation: document.getElementById('pc-char-occupation').value.trim(),
      hobbies: document.getElementById('pc-char-hobbies').value.trim(),
      catchphrase: document.getElementById('pc-char-catchphrase').value.trim(),
      likes: document.getElementById('pc-char-likes').value.trim(),
      fears: document.getElementById('pc-char-fears').value.trim(),
      birthday: document.getElementById('pc-char-birthday').value.trim(),
    };
    try {
      await this.api('/api/pc/character/save', data, 'POST');
      alert('角色「' + name + '」已保存');
      this.loadCharacterList();
    } catch (e) {
      alert('保存失败：' + e.message);
    }
  },

  /* ---------------- 调度与模板面板 ---------------- */

  schedPanelEl: null,
  schedTab: 'morning',

  buildSchedulerPanel() {
    if (this.schedPanelEl) return;
    const panel = document.createElement('div');
    panel.id = 'pc-scheduler-panel';
    panel.style.cssText = 'position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,0.4);';
    panel.innerHTML = `
      <div style="width:680px;max-height:85vh;background:#fff;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,0.2);display:flex;flex-direction:column;overflow:hidden;">
        <div style="display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid #eee;">
          <div style="font-size:17px;font-weight:600;">调度与模板</div>
          <button id="pc-sched-close" style="border:none;background:none;font-size:20px;cursor:pointer;color:#888;width:30px;height:30px;border-radius:6px;">×</button>
        </div>
        <div style="display:flex;border-bottom:1px solid #eee;padding:0 12px;">
          <div class="pc-sched-tab" data-tab="morning" style="padding:12px 16px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent;">早晚安</div>
          <div class="pc-sched-tab" data-tab="template" style="padding:12px 16px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent;">文案模板</div>
          <div class="pc-sched-tab" data-tab="anniversary" style="padding:12px 16px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent;">纪念日</div>
          <div class="pc-sched-tab" data-tab="task" style="padding:12px 16px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent;">定时任务</div>
        </div>
        <div id="pc-sched-body" style="flex:1;overflow-y:auto;padding:20px;"></div>
      </div>`;
    document.body.appendChild(panel);
    this.schedPanelEl = panel;
    panel.querySelector('#pc-sched-close').addEventListener('click', () => this.closeSchedulerPanel());
    panel.addEventListener('click', (e) => { if (e.target === panel) this.closeSchedulerPanel(); });
    panel.querySelectorAll('.pc-sched-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        this.schedTab = tab.dataset.tab;
        this._renderSchedTabs();
        this._loadSchedTab();
      });
    });
  },

  openSchedulerPanel() {
    this.buildSchedulerPanel();
    this.schedPanelEl.style.display = 'flex';
    this.schedTab = 'morning';
    this._renderSchedTabs();
    this._loadSchedTab();
  },

  closeSchedulerPanel() {
    if (this.schedPanelEl) this.schedPanelEl.style.display = 'none';
  },

  _renderSchedTabs() {
    this.schedPanelEl.querySelectorAll('.pc-sched-tab').forEach(tab => {
      if (tab.dataset.tab === this.schedTab) {
        tab.style.color = '#4a90d9';
        tab.style.borderBottomColor = '#4a90d9';
        tab.style.fontWeight = '600';
      } else {
        tab.style.color = '#666';
        tab.style.borderBottomColor = 'transparent';
        tab.style.fontWeight = 'normal';
      }
    });
  },

  async _loadSchedTab() {
    const body = this.schedPanelEl.querySelector('#pc-sched-body');
    if (this.schedTab === 'morning') this._renderMorningTab(body);
    else if (this.schedTab === 'template') await this._renderTemplateTab(body);
    else if (this.schedTab === 'anniversary') await this._renderAnniversaryTab(body);
    else if (this.schedTab === 'task') await this._renderTaskTab(body);
  },

  _renderMorningTab(body) {
    const cfg = this.cfg || {};
    body.innerHTML = `
      <div style="margin-bottom:20px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
          <span style="font-size:15px;font-weight:600;">早安推送</span>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;">
            <input type="checkbox" id="pc-morning-enabled" ${cfg.MORNING_ENABLED !== false ? 'checked' : ''}> 启用
          </label>
        </div>
        <div style="display:flex;gap:12px;align-items:center;font-size:13px;color:#666;">
          时间段：<input type="time" id="pc-morning-start" value="${cfg.MORNING_START || '07:00'}" style="padding:6px;border:1px solid #ddd;border-radius:6px;">
          ~ <input type="time" id="pc-morning-end" value="${cfg.MORNING_END || '09:30'}" style="padding:6px;border:1px solid #ddd;border-radius:6px;">
        </div>
        <div style="font-size:12px;color:#999;margin-top:6px;">窗口内随机时间推送，每天一次</div>
      </div>
      <div style="margin-bottom:20px;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
          <span style="font-size:15px;font-weight:600;">晚安推送</span>
          <label style="display:flex;align-items:center;gap:6px;font-size:13px;">
            <input type="checkbox" id="pc-night-enabled" ${cfg.NIGHT_ENABLED !== false ? 'checked' : ''}> 启用
          </label>
        </div>
        <div style="display:flex;gap:12px;align-items:center;font-size:13px;color:#666;">
          时间段：<input type="time" id="pc-night-start" value="${cfg.NIGHT_START || '22:00'}" style="padding:6px;border:1px solid #ddd;border-radius:6px;">
          ~ <input type="time" id="pc-night-end" value="${cfg.NIGHT_END || '23:30'}" style="padding:6px;border:1px solid #ddd;border-radius:6px;">
        </div>
      </div>
      <button id="pc-sched-save" style="padding:10px 24px;border:none;border-radius:8px;background:#4a90d9;color:#fff;cursor:pointer;font-weight:600;">保存设置</button>`;
    body.querySelector('#pc-sched-save').addEventListener('click', async () => {
      const patch = {
        MORNING_ENABLED: body.querySelector('#pc-morning-enabled').checked,
        MORNING_START: body.querySelector('#pc-morning-start').value,
        MORNING_END: body.querySelector('#pc-morning-end').value,
        NIGHT_ENABLED: body.querySelector('#pc-night-enabled').checked,
        NIGHT_START: body.querySelector('#pc-night-start').value,
        NIGHT_END: body.querySelector('#pc-night-end').value,
      };
      try {
        await this.api('/api/pc/config', patch, 'POST');
        this.cfg = Object.assign(this.cfg || {}, patch);
        alert('早晚安设置已保存');
      } catch (e) { alert('保存失败：' + e.message); }
    });
  },

  async _renderTemplateTab(body) {
    body.innerHTML = '<div style="text-align:center;color:#999;padding:20px;">加载中...</div>';
    try {
      const data = await this.api('/api/pc/template/list');
      const cats = { morning: '早安', night: '晚安', festival: '节日', anniversary: '纪念日' };
      let html = '';
      for (const [cat, label] of Object.entries(cats)) {
        const list = data[cat] || [];
        html += `<div style="margin-bottom:20px;">
          <div style="font-size:15px;font-weight:600;margin-bottom:8px;">${label}模板（${list.length}条）</div>
          <div id="pc-tpl-list-${cat}">`;
        list.forEach((t, i) => {
          html += `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px;background:#f9f9f9;border-radius:6px;margin-bottom:6px;">
            <div style="flex:1;font-size:13px;color:#444;">${t}</div>
            <button class="pc-tpl-del" data-cat="${cat}" data-idx="${i}" style="border:none;background:none;color:#e55;cursor:pointer;font-size:12px;padding:2px 6px;">删除</button>
          </div>`;
        });
        html += `</div>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <input type="text" id="pc-tpl-input-${cat}" placeholder="添加新模板，可用 {{call_user}} {{character_name}} {{festival_name}}" style="flex:1;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;">
            <button class="pc-tpl-add" data-cat="${cat}" style="padding:8px 16px;border:none;border-radius:6px;background:#4a90d9;color:#fff;cursor:pointer;font-size:13px;">添加</button>
          </div>
        </div>`;
      }
      body.innerHTML = html;
      body.querySelectorAll('.pc-tpl-del').forEach(btn => {
        btn.addEventListener('click', async () => {
          await this.api('/api/pc/template/delete', { category: btn.dataset.cat, index: parseInt(btn.dataset.idx) }, 'POST');
          this._loadSchedTab();
        });
      });
      body.querySelectorAll('.pc-tpl-add').forEach(btn => {
        btn.addEventListener('click', async () => {
          const input = body.querySelector('#pc-tpl-input-' + btn.dataset.cat);
          const val = input.value.trim();
          if (!val) return;
          await this.api('/api/pc/template/add', { category: btn.dataset.cat, content: val }, 'POST');
          this._loadSchedTab();
        });
      });
    } catch (e) {
      body.innerHTML = '<div style="color:#e55;">加载失败：' + e.message + '</div>';
    }
  },

  async _renderAnniversaryTab(body) {
    body.innerHTML = '<div style="text-align:center;color:#999;padding:20px;">加载中...</div>';
    try {
      const data = await this.api('/api/pc/anniversary/list');
      const list = data.anniversaries || [];
      let html = '<div style="margin-bottom:16px;">';
      if (list.length === 0) {
        html += '<div style="text-align:center;color:#999;padding:20px;">还没有纪念日</div>';
      } else {
        list.forEach(a => {
          html += `<div style="display:flex;align-items:center;gap:8px;padding:10px;background:#f9f9f9;border-radius:6px;margin-bottom:6px;">
            <div style="flex:1;">
              <div style="font-size:14px;font-weight:600;">${a.name}</div>
              <div style="font-size:12px;color:#888;">${a.month}月${a.day}日 · ${a.type}${a.character ? ' · ' + a.character : ''}</div>
            </div>
            <button class="pc-ann-del" data-id="${a.id}" style="border:none;background:none;color:#e55;cursor:pointer;font-size:12px;padding:4px 8px;">删除</button>
          </div>`;
        });
      }
      html += '</div>';
      html += `<div style="border-top:1px solid #eee;padding-top:16px;">
        <div style="font-size:14px;font-weight:600;margin-bottom:10px;">添加纪念日</div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input type="text" id="pc-ann-name" placeholder="名称（如：相识日）" style="flex:1;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;">
          <input type="number" id="pc-ann-month" placeholder="月" min="1" max="12" style="width:70px;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;">
          <input type="number" id="pc-ann-day" placeholder="日" min="1" max="31" style="width:70px;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;">
        </div>
        <div style="display:flex;gap:8px;align-items:center;">
          <input type="text" id="pc-ann-type" placeholder="类型（生日/纪念日/其他）" style="flex:1;padding:8px;border:1px solid #ddd;border-radius:6px;font-size:13px;">
          <button id="pc-ann-add" style="padding:8px 20px;border:none;border-radius:6px;background:#4a90d9;color:#fff;cursor:pointer;font-size:13px;">添加</button>
        </div>
      </div>`;
      body.innerHTML = html;
      body.querySelectorAll('.pc-ann-del').forEach(btn => {
        btn.addEventListener('click', async () => {
          await this.api('/api/pc/anniversary/delete', { id: parseInt(btn.dataset.id) }, 'POST');
          this._loadSchedTab();
        });
      });
      body.querySelector('#pc-ann-add').addEventListener('click', async () => {
        const name = body.querySelector('#pc-ann-name').value.trim();
        const month = parseInt(body.querySelector('#pc-ann-month').value);
        const day = parseInt(body.querySelector('#pc-ann-day').value);
        const type = body.querySelector('#pc-ann-type').value.trim() || '纪念日';
        if (!name || !month || !day) { alert('请填写名称和日期'); return; }
        await this.api('/api/pc/anniversary/add', { name, month, day, type }, 'POST');
        this._loadSchedTab();
      });
    } catch (e) {
      body.innerHTML = '<div style="color:#e55;">加载失败：' + e.message + '</div>';
    }
  },

  /**
   * 当前会话上下文：session_id + 角色名。
   * ★ P0-2：这些接口以前不传 session，后端只能回退到进程级单例
   *   active_session()，多标签/多端并发时会串到别的标签正在用的角色上。
   */
  _ctx() {
    const sid = (window.Session && Session.getSessionId)
      ? Session.getSessionId()
      : (localStorage.getItem('ai_companion_session_id')
         || localStorage.getItem('session_id') || 'default');
    const cid = (window.Chat && Chat.contact && (Chat.contact.name || Chat.contact.id)) || 'default';
    return { sid, cid };
  },

  async _renderTaskTab(body) {
    body.innerHTML = '<div style="text-align:center;color:#999;padding:20px;">加载中...</div>';
    try {
      const { sid, cid } = this._ctx();
      const data = await this.api(
        '/api/pc/task/list?session_id=' + encodeURIComponent(sid)
        + '&character=' + encodeURIComponent(cid)
        + '&character_id=' + encodeURIComponent(cid));
      const list = data.tasks || [];
      let html = '';
      if (list.length === 0) {
        html = '<div style="text-align:center;color:#999;padding:20px;">暂无定时任务<br><span style="font-size:12px;">聊天中说"5分钟后发爱我"即可创建</span></div>';
      } else {
        list.forEach(t => {
          const statusText = t.status === 'pending' ? '待执行' : t.status === 'done' ? '已完成' : '已取消';
          const statusColor = t.status === 'pending' ? '#4a90d9' : '#999';
          html += `<div style="display:flex;align-items:center;gap:8px;padding:10px;background:#f9f9f9;border-radius:6px;margin-bottom:6px;">
            <div style="flex:1;">
              <div style="font-size:13px;color:#444;">${t.content}</div>
              <div style="font-size:12px;color:#888;margin-top:2px;">触发时间：${t.trigger_time} · <span style="color:${statusColor}">${statusText}</span></div>
            </div>
            ${t.status === 'pending' ? `<button class="pc-task-run" data-id="${t.id}" style="border:1px solid #4a90d9;background:none;color:#4a90d9;cursor:pointer;font-size:12px;padding:4px 10px;border-radius:4px;margin-right:4px;">立即执行</button>` : ''}
            <button class="pc-task-del" data-id="${t.id}" style="border:none;background:none;color:#e55;cursor:pointer;font-size:12px;padding:4px 8px;">删除</button>
          </div>`;
        });
      }
      body.innerHTML = html;
      body.querySelectorAll('.pc-task-del').forEach(btn => {
        btn.addEventListener('click', async () => {
          const { sid, cid } = this._ctx();
          await this.api('/api/pc/task/delete', {
            id: parseInt(btn.dataset.id), session_id: sid,
            character: cid, character_id: cid,
          }, 'POST');
          this._loadSchedTab();
        });
      });
      body.querySelectorAll('.pc-task-run').forEach(btn => {
        btn.addEventListener('click', async () => {
          try {
            await this.api('/api/pc/task/trigger_now', { id: parseInt(btn.dataset.id) }, 'POST');
            alert('已触发执行');
            this._loadSchedTab();
          } catch (e) { alert('执行失败：' + e.message); }
        });
      });
    } catch (e) {
      body.innerHTML = '<div style="color:#e55;">加载失败：' + e.message + '</div>';
    }
  },
};

PCEnhance.init();
