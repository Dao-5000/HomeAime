'use strict';
/* ============================================================
   companion.js — 「陪伴」Tab（微信简约风）
   游戏陪伴：卡片（当前只有我的世界接入 Bot）
   生活陪伴：聚合 UI 面板（一起玩游戏/刷抖音/追剧/听歌/夜聊）
   ============================================================ */

// ── 游戏陪伴（卡片）──
const COMPANION_GAMES = [
  { id: 'minecraft', label: '我的世界', icon: '⛏️', desc: '让当前 AI 角色进入你的世界一起玩' },
  { id: 'stardew',   label: '星露谷',   icon: '🌾', desc: '骨子进你的农场，一起种田钓鱼' },
];

// ── 生活陪伴（聚合 UI，一次选一个当前陪伴）──
const COMPANION_LIFE = [
  { id: 'play',     label: '一起玩游戏', icon: '🎮', desc: '开黑、通关、闲聊游戏' },
  { id: 'douyin',   label: '一起刷抖音', icon: '🎵', desc: '刷到好玩的记得叫我' },
  { id: 'drama',    label: '一起追剧',   icon: '📺', desc: '今晚追哪部？' },
  { id: 'music',    label: '一起听歌',   icon: '🎧', desc: '分享你的歌单' },
  { id: 'night',    label: '夜聊',       icon: '🌙', desc: '深夜谈心陪伴' },
];

function _companionSetting() {
  try {
    const s = Store.getSettings();
    const cid = (typeof Chat !== 'undefined' && Chat.contact) ? (Chat.contact.name || Chat.contact.id) : 'default';
    return (s.companionByCharacter && s.companionByCharacter[cid]) || s.companion || null;
  } catch (_) { return null; }
}

async function _saveCompanion(mode, label) {
  try {
    const s = Store.getSettings();
    const cid = (typeof Chat !== 'undefined' && Chat.contact) ? (Chat.contact.name || Chat.contact.id) : 'default';
    const map = Object.assign({}, s.companionByCharacter || {});
    map[cid] = mode ? { mode, label } : null;
    Store.saveSettings({ companionByCharacter: map, companion: mode ? { mode, label } : null });
  } catch (_) {}
  try {
    await fetch('/api/companion/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: window.Session?.getSessionId?.() || 'default',
        character_id: (typeof Chat !== 'undefined' && Chat.contact) ? (Chat.contact.name || Chat.contact.id) : 'default',
        mode: mode || '',
        label: label || '',
      }),
    });
  } catch (_) {}
  if (mode === 'minecraft') {
    await _bindMinecraft();
  }
}

// ★ Electron 默认禁用 window.prompt（安全考虑），所以用自定义 modal。
//   返回 number（合法端口）或 null（取消/无效）。
function _promptMinecraftPort() {
  return new Promise((resolve) => {
    const old = document.getElementById('mc-port-modal');
    if (old) old.remove();
    const wrap = document.createElement('div');
    wrap.id = 'mc-port-modal';
    wrap.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:9999;display:flex;align-items:center;justify-content:center;';
    const box = document.createElement('div');
    box.style.cssText = 'background:var(--bg-card,#fff);border-radius:12px;padding:18px 20px;width:280px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,.18);';
    const title = document.createElement('div');
    title.textContent = '输入 Minecraft 局域网端口';
    title.style.cssText = 'font-size:15px;font-weight:600;margin-bottom:6px;color:var(--text-main,#222);';
    const hint = document.createElement('div');
    hint.textContent = '在 MC 里「对局域网开放」后，聊天栏会显示端口号（每次开世界可能不同）';
    hint.style.cssText = 'font-size:12px;color:var(--text-sub,#888);margin-bottom:12px;line-height:1.5;';
    const input = document.createElement('input');
    input.type = 'number';
    input.id = 'mc-port-input';
    input.placeholder = '如 53371';
    input.min = 1;
    input.max = 65535;
    input.style.cssText = 'width:100%;padding:8px 10px;border:1px solid var(--border,#ddd);border-radius:6px;font-size:14px;box-sizing:border-box;color:var(--text-main,#222);background:transparent;';
    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:8px;justify-content:flex-end;margin-top:14px;';
    const cancel = document.createElement('button');
    cancel.textContent = '取消';
    cancel.style.cssText = 'padding:6px 14px;border:1px solid var(--border,#ddd);background:transparent;border-radius:6px;cursor:pointer;font-size:13px;color:var(--text-main,#222);';
    const ok = document.createElement('button');
    ok.textContent = '确定';
    ok.className = 'chip';
    ok.style.cssText = 'padding:6px 14px;cursor:pointer;';
    const close = (val) => { wrap.remove(); resolve(val); };
    cancel.onclick = () => close(null);
    ok.onclick = () => {
      const v = parseInt(input.value, 10);
      if (!v || v < 1 || v > 65535) {
        toast('端口无效，请填游戏里显示的数字');
        return;
      }
      close(v);
    };
    wrap.addEventListener('click', (e) => { if (e.target === wrap) close(null); });
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok.click(); });
    actions.appendChild(cancel);
    actions.appendChild(ok);
    box.appendChild(title);
    box.appendChild(hint);
    box.appendChild(input);
    box.appendChild(actions);
    wrap.appendChild(box);
    document.body.appendChild(wrap);
    setTimeout(() => { try { input.focus(); input.select(); } catch (_) {} }, 50);
  });
}

async function _bindMinecraft() {
  try {
    // 局域网端口是随机的，需用户开世界后从游戏聊天栏拿到。
    // ★ Electron 默认禁用 window.prompt，用自定义 modal 替代。
    const portNum = await _promptMinecraftPort();
    if (portNum === null) return;   // 用户取消或输入无效（modal 内部已 toast 提示）
    // 范围校验在 _promptMinecraftPort 内部完成

    const res = await fetch('/api/bot/bind', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: window.Session?.getSessionId?.() || 'default',
        character_id: (typeof Chat !== 'undefined' && Chat.contact) ? (Chat.contact.name || Chat.contact.id) : 'default',
        port: portNum,
      }),
    });
    const d = await res.json().catch(() => ({}));
    if (d.started === false) {
      toast('启动失败：请确认端口正确、且 MC 已「对局域网开放」');
    } else {
      toast('已启动，TA 正在进游戏…');
    }
    setTimeout(() => renderCompanion(), 1500);
  } catch (_) {}
}

// ── MC 在线状态（轮询后端真实状态，与卡片点击开关解耦）──
// 游戏陪伴现在是"自动感知"：后端检测到游戏开了会自动接入并打招呼（QQ/App 同步），
// 卡片上的状态灯只是**显示**真实状态，不需要点击才"开启"。
const _mcStatus = { online: false, companion: '', _timer: null };

function _refreshMcStatus() {
  fetch('/api/numen/status').then(r => r.json()).then(j => {
    const online = !!(j && j.ready && j.companion);
    const companion = (j && j.companion) || '';
    if (online !== _mcStatus.online || companion !== _mcStatus.companion) {
      _mcStatus.online = online;
      _mcStatus.companion = companion;
      // 面板正开着才重绘（状态灯变化即时可见）
      if (document.querySelector('.cp-grid')) renderCompanion();
    }
  }).catch(() => {
    if (_mcStatus.online) {
      _mcStatus.online = false;
      _mcStatus.companion = '';
      if (document.querySelector('.cp-grid')) renderCompanion();
    }
  });
}

function _startMcStatusPoll() {
  _refreshMcStatus();
  _refreshSdStatus();
  if (_mcStatus._timer) clearInterval(_mcStatus._timer);
  _mcStatus._timer = setInterval(_refreshMcStatus, 15000);
  if (_sdStatus._timer) clearInterval(_sdStatus._timer);
  _sdStatus._timer = setInterval(_refreshSdStatus, 15000);
}

// ── 星露谷在线状态（同 MC 自动感知模式：后端 15s 看护，这里只显示真实状态）──
const _sdStatus = { online: false, enabled: true, companion: '', _timer: null };

function _refreshSdStatus() {
  fetch('/api/stardew/status').then(r => r.json()).then(j => {
    const online = !!(j && j.enabled && j.ready && j.game_online);
    const enabled = !!(j && j.enabled);
    const companion = (j && j.companion) || '';
    if (online !== _sdStatus.online || companion !== _sdStatus.companion || enabled !== _sdStatus.enabled) {
      _sdStatus.online = online;
      _sdStatus.enabled = enabled;
      _sdStatus.companion = companion;
      if (document.querySelector('.cp-grid')) renderCompanion();
    }
  }).catch(() => {
    if (_sdStatus.online) {
      _sdStatus.online = false;
      _sdStatus.companion = '';
      if (document.querySelector('.cp-grid')) renderCompanion();
    }
  });
}

async function _launchStardew() {
  try {
    const r = await fetch('/api/stardew/launch', { method: 'POST' });
    const j = await r.json();
    toast(j.ok ? (j.message || '星露谷启动中') : (j.error || '启动失败'));
  } catch (e) { toast('启动失败：' + e.message); }
}

// ★ 真联机模式：拉起骨子的第二个游戏实例（第 2 步）。
//   完整流程：① 你自己开游戏读档并主持联机 ② 点此按钮自动弹出骨子实例 ③ 在骨子实例「协作→加入」进农场
async function _launchStardewClient() {
  try {
    const r = await fetch('/api/stardew/launch_client', { method: 'POST' });
    const j = await r.json();
    toast(j.ok ? (j.message || '骨子实例启动中') : (j.error || '启动失败'));
    setTimeout(() => _refreshSdStatus(), 5000);
  } catch (e) { toast('启动失败：' + e.message); }
}

// ★ AI 监督吃醋（2026-09-11，方案参考桌面「AI监督吃醋系统.zip」）
//   只读前台窗口进程名/标题 + 键鼠空闲（不截图、不 OCR、纯本地）。
//   ★ 生成位置修正：原先建在 _renderStatus() 的「当前陪伴状态」区里，
//     那个区只有"陪伴进行中"才渲染 → 没开陪伴时整个设置**根本看不到**。
//     现在挂在陪伴页顶部，任何时候都在。
function _renderJealousyCard() {
  const s = Store.getSettings();
  const jel = h('div', { class: 'cp-mc' });
  const jelToggle = h('input', { type: 'checkbox', style: 'width:18px;height:18px;accent-color:var(--accent,#e25b7c);cursor:pointer' });
  jelToggle.checked = !!s.jealousyEnabled;
  jelToggle.addEventListener('change', (e) => {
    const v = !!e.target.checked;
    Store.saveSettings({ jealousyEnabled: v });
    savePcConfig({ JEALOUSY_ENABLED: v });
    toast(v ? '监督吃醋已开启：她会留意你在电脑上忙什么' : '监督吃醋已关闭');
  });
  jel.appendChild(h('div', { style: 'display:flex;align-items:center;justify-content:space-between' },
    h('div', { class: 'cp-mc-title', text: '😤 监督吃醋' }),
    h('label', { style: 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer' },
      h('span', { text: '盯着我玩什么' }), jelToggle)));
  jel.appendChild(h('div', { class: 'cp-mc-hint', text: '只看你在用什么软件（进程名/窗口标题），不截图、不读内容、数据不出本机。刷短视频/逛别的AI伴侣/聊微信久了，她会自己憋出火来主动质问你。' }));
  const jlTrig = h('input', { type: 'number', min: '10', max: '100', style: 'width:52px;border:1px solid var(--border,#ddd);border-radius:8px;padding:5px 6px;font-size:12px;text-align:center' });
  const jlCd = h('input', { type: 'number', min: '5', max: '240', style: 'width:52px;border:1px solid var(--border,#ddd);border-radius:8px;padding:5px 6px;font-size:12px;text-align:center' });
  jlTrig.value = String(s.jealousyTrigger || 60);
  jlCd.value = String(s.jealousyCooldownMin || 30);
  const _saveJl = () => {
    const tr = Math.min(100, Math.max(10, parseInt(jlTrig.value, 10) || 60));
    const cd = Math.min(240, Math.max(5, parseInt(jlCd.value, 10) || 30));
    jlTrig.value = String(tr); jlCd.value = String(cd);
    Store.saveSettings({ jealousyTrigger: tr, jealousyCooldownMin: cd });
    savePcConfig({ JEALOUSY_TRIGGER: tr, JEALOUSY_MESSAGE_COOLDOWN: cd * 60 });
    toast('吃醋参数已更新');
  };
  jlTrig.addEventListener('change', _saveJl);
  jlCd.addEventListener('change', _saveJl);
  const _jlLine = (label, ctrl, hint) => h('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:8px;margin:8px 0' },
    h('div', { style: 'font-size:12px' },
      h('div', { text: label }),
      hint ? h('div', { style: 'font-size:11px;color:#999', text: hint }) : null),
    h('div', { style: 'display:flex;gap:4px;align-items:center' }, ctrl));
  jel.appendChild(_jlLine('吃醋阈值', [jlTrig, h('span', { style: 'font-size:11px;color:#999', text: '/100' })], '越高越不容易生气'));
  jel.appendChild(_jlLine('两次之间最少间隔', [jlCd, h('span', { style: 'font-size:11px;color:#999', text: '分钟' })], '嫌她唠叨就调大'));
  jel.appendChild(h('button', {
    class: 'chip', text: '😤 让她吃一次醋（试一下）', style: 'margin-top:6px',
    onclick: async () => {
      toast('正在让她酝酿情绪…');
      try {
        const r = await fetch('/api/jealousy/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: (window.Session && Session.getSessionId && Session.getSessionId()) || 'default',
            character_name: (typeof Chat !== 'undefined' && Chat.contact && (Chat.contact.name || Chat.contact.id)) || '',
          }),
        });
        const j = await r.json();
        if (j && j.ok) {
          toast(j.delivered ? ('她发了：' + (j.message || ''))
                            : ('她心里是这么想的：' + (j.message || '') + (j.why ? '（' + j.why + '）' : '')), 5200);
        } else {
          toast('这次没生成出来：' + ((j && (j.error || j.why)) || '稍后再试'));
        }
      } catch (_) { toast('触发失败，稍后再试'); }
    },
  }));
  // ★ 以后端 config.json 为准回填：吃醋也可能是在别处（接口/设置）改的，
  //   只看 localStorage 会出现"后端在跑、开关却显示关"的错觉。
  (async () => {
    try {
      const cfg = await (await fetch('/api/pc/config')).json();
      if (typeof cfg.JEALOUSY_ENABLED !== 'undefined') jelToggle.checked = !!cfg.JEALOUSY_ENABLED;
      if (typeof cfg.JEALOUSY_TRIGGER !== 'undefined') jlTrig.value = String(parseInt(cfg.JEALOUSY_TRIGGER, 10) || 60);
      if (typeof cfg.JEALOUSY_MESSAGE_COOLDOWN !== 'undefined') {
        jlCd.value = String(Math.max(1, Math.round((parseInt(cfg.JEALOUSY_MESSAGE_COOLDOWN, 10) || 1800) / 60)));
      }
    } catch (_) {}
  })();
  return jel;
}

function renderCompanion() {
  const wrap = $('#page-companion');
  wrap.innerHTML = '';
  const cur = _companionSetting();

  // ── 顶部横幅 ──
  wrap.appendChild(h('div', { class: 'cp-banner' },
    h('div', { class: 'cp-banner-title', text: '一起玩，一起陪' }),
    h('div', { class: 'cp-banner-sub', text: '选一个想一起做的事，TA 会感知你在干嘛，自然陪着你' }),
  ));

  // ── 监督设置（全局，任何时候都显示）──
  wrap.appendChild(h('div', { class: 'cp-sec-title', text: '监督设置' }));
  wrap.appendChild(_renderJealousyCard());

  // ── 游戏陪伴（卡片）──
  wrap.appendChild(h('div', { class: 'cp-sec-title', text: '游戏陪伴' }));
  const gameGrid = h('div', { class: 'cp-grid' });
  for (const g of COMPANION_GAMES) {
    const sel = cur && cur.mode === g.id;
    const st = g.id === 'stardew' ? _sdStatus : _mcStatus;
    const inGame = st.online;
    const card = h('div', { class: 'cp-card' + (sel ? ' selected' : '') },
      h('div', { class: 'cp-card-icon', text: g.icon }),
      h('div', { class: 'cp-card-name', text: g.label }),
      h('div', { class: 'cp-card-desc', text: inGame
        ? 'TA 已经在你的世界里玩了，QQ 上喊 TA 就行'
        : g.desc + '（点击开启并自动进游戏）' }),
      sel ? h('div', { class: 'cp-card-tag', text: '陪伴中' }) : null,
      inGame ? h('div', { class: 'cp-card-tag', style: 'color:#0a0;', text: '● ' + (st.companion || '骨子') + ' 已在游戏里' }) : null,
    );
    card.addEventListener('click', () => _toggleGame(g, wrap));
    gameGrid.appendChild(card);
  }
  wrap.appendChild(gameGrid);
  _startMcStatusPoll();

  // ── 生活陪伴（聚合 UI 面板）──
  wrap.appendChild(h('div', { class: 'cp-sec-title', text: '生活陪伴' }));
  const lifePanel = h('div', { class: 'cp-life-panel' });
  for (const l of COMPANION_LIFE) {
    const sel = cur && cur.mode === l.id;
    const row = h('div', { class: 'cp-life-row' + (sel ? ' selected' : '') },
      h('span', { class: 'cp-life-icon', text: l.icon }),
      h('div', { class: 'cp-life-body' },
        h('div', { class: 'cp-life-name', text: l.label }),
        h('div', { class: 'cp-life-desc', text: l.desc }),
      ),
      sel ? h('span', { class: 'cp-life-check', text: '✓' }) : h('span', { class: 'cp-life-arrow', text: '›' }),
    );
    row.addEventListener('click', () => _toggleCompanion(l.id, l.label, wrap));
    lifePanel.appendChild(row);
  }
  wrap.appendChild(lifePanel);

  // ── 当前陪伴状态区 ──
  const statusBox = h('div', { class: 'cp-status' });
  _renderStatus(statusBox, cur);
  wrap.appendChild(statusBox);
}

async function _renderStatus(box, cur) {
  box.innerHTML = '';
  if (!cur) {
    box.appendChild(h('div', { class: 'cp-status-empty', text: '还没开始陪伴，选一个上面的事吧' }));
    return;
  }
  const row = h('div', { class: 'cp-status-row' },
    h('span', { class: 'cp-status-dot' }),
    h('span', { class: 'cp-status-text', text: '正在陪 TA：' + cur.label }),
    h('button', { class: 'cp-status-close', text: '结束', onclick: () => { _saveCompanion(null, ''); renderCompanion(); toast('已结束陪伴'); } }),
  );
  box.appendChild(row);

  // ★ 感知与观察（生活陪伴模式的屏幕感知已并入这里）：TA 看屏幕 + 看完即评。
  //   观察间隔=多久看一眼；评论间隔=两次屏幕评论最小间隔；主动间隔=闲置多久主动找你聊。
  //   游戏陪伴（minecraft/stardew）不走屏幕感知，不显示这组。
  if (cur.mode !== 'minecraft' && cur.mode !== 'stardew') {
    const s = Store.getSettings();
    // 后端配置回填（本地没存过时以 config.json 为准）
    (async () => {
      try {
        const r = await fetch('/api/pc/config');
        const cfg = await r.json();
        const patch = {};
        if (typeof cfg.SCREEN_INTERVAL !== 'undefined' && !s.screenInterval) patch.screenInterval = parseInt(cfg.SCREEN_INTERVAL, 10) || 120;
        if (typeof cfg.COMPANION_IDLE_MIN_MINUTES !== 'undefined' && !s.companionIdleMin) {
          patch.companionIdleMin = parseInt(cfg.COMPANION_IDLE_MIN_MINUTES, 10) || 4;
          patch.companionIdleMax = parseInt(cfg.COMPANION_IDLE_MAX_MINUTES, 10) || 8;
        }
        if (typeof cfg.COMPANION_SCREEN_COMMENT_COOLDOWN !== 'undefined' && !s.companionCommentCooldown) {
          patch.companionCommentCooldown = Math.max(1, Math.round(parseInt(cfg.COMPANION_SCREEN_COMMENT_COOLDOWN, 10) / 60) || 5);
        }
        // ★ 感知开关回填（以后端配置为准）
        if (typeof cfg.SCREEN_PERCEPTION_ENABLED !== 'undefined') senseToggle.checked = !!cfg.SCREEN_PERCEPTION_ENABLED;
        // ★ 监督吃醋开关回填
        if (typeof cfg.JEALOUSY_ENABLED !== 'undefined' && jelToggle) jelToggle.checked = !!cfg.JEALOUSY_ENABLED;
        if (typeof cfg.JEALOUSY_TRIGGER !== 'undefined' && jlTrig) jlTrig.value = String(parseInt(cfg.JEALOUSY_TRIGGER, 10) || 60);
        if (typeof cfg.JEALOUSY_MESSAGE_COOLDOWN !== 'undefined' && jlCd) {
          jlCd.value = String(Math.max(1, Math.round((parseInt(cfg.JEALOUSY_MESSAGE_COOLDOWN, 10) || 1800) / 60)));
        }
        if (Object.keys(patch).length) Store.saveSettings(patch);
      } catch (_) {}
    })();
    const watchSel = h('select', {},
      h('option', { value: '30' }, '30 秒'),
      h('option', { value: '60' }, '1 分钟'),
      h('option', { value: '120' }, '2 分钟'),
      h('option', { value: '300' }, '5 分钟'));
    watchSel.value = String(s.screenInterval || 120);
    watchSel.addEventListener('change', (e) => {
      Store.saveSettings({ screenInterval: parseInt(e.target.value, 10) });
      savePcConfig({ SCREEN_INTERVAL: parseInt(e.target.value, 10) });
      toast('观察间隔已更新');
    });
    const ciMin = h('input', { type: 'number', min: '1', max: '60', style: 'width:52px;border:1px solid var(--border,#ddd);border-radius:8px;padding:5px 6px;font-size:12px;text-align:center' });
    const ciMax = h('input', { type: 'number', min: '2', max: '120', style: 'width:52px;border:1px solid var(--border,#ddd);border-radius:8px;padding:5px 6px;font-size:12px;text-align:center' });
    ciMin.value = String(s.companionIdleMin || 4);
    ciMax.value = String(s.companionIdleMax || 8);
    const _saveCi = () => {
      let lo = parseInt(ciMin.value, 10) || 4;
      let hi = parseInt(ciMax.value, 10) || 8;
      if (hi < lo) { hi = lo; ciMax.value = String(hi); }
      Store.saveSettings({ companionIdleMin: lo, companionIdleMax: hi });
      savePcConfig({ COMPANION_IDLE_MIN_MINUTES: lo, COMPANION_IDLE_MAX_MINUTES: hi });
      toast('主动间隔已更新');
    };
    ciMin.addEventListener('change', _saveCi);
    ciMax.addEventListener('change', _saveCi);
    const ccInp = h('input', { type: 'number', min: '1', max: '30', style: 'width:52px;border:1px solid var(--border,#ddd);border-radius:8px;padding:5px 6px;font-size:12px;text-align:center' });
    ccInp.value = String(s.companionCommentCooldown || 5);
    ccInp.addEventListener('change', (e) => {
      const v = Math.min(30, Math.max(1, parseInt(e.target.value, 10) || 5));
      e.target.value = String(v);
      Store.saveSettings({ companionCommentCooldown: v });
      savePcConfig({ COMPANION_SCREEN_COMMENT_COOLDOWN: v * 60 });
      toast('评论间隔已更新');
    });
    const sense = h('div', { class: 'cp-mc' });
    // ★ 感知总开关（👁 开/关本体；间隔们只是参数）
    const senseToggle = h('input', { type: 'checkbox', style: 'width:18px;height:18px;accent-color:var(--accent,#e25b7c);cursor:pointer' });
    senseToggle.addEventListener('change', (e) => {
      const v = !!e.target.checked;
      const patch = { SCREEN_PERCEPTION_ENABLED: v };
      if (v) patch.AWARENESS_ENABLED = true;   // 开感知时确保总开关也开（关感知不动总开关）
      savePcConfig(patch);
      toast(v ? '屏幕感知已开启，TA 会看着屏幕陪你' : '屏幕感知已关闭');
    });
    const senseTitle = h('div', { style: 'display:flex;align-items:center;justify-content:space-between' },
      h('div', { class: 'cp-mc-title', text: '👁 感知与观察' }),
      h('label', { style: 'display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer' },
        h('span', { text: '看着我的屏幕' }), senseToggle));
    sense.appendChild(senseTitle);
    sense.appendChild(h('div', { class: 'cp-mc-hint', text: '陪伴中 TA 会看着你的屏幕，看到有意思的就凑过来说一句；你问「你在看什么」TA 会告诉你（90 秒内不重复截图，说「继续看」可立刻刷新）。' }));
    const _senseLine = (label, ctrl, hint) => {
      const r = h('div', { style: 'display:flex;align-items:center;justify-content:space-between;gap:8px;margin:8px 0' },
        h('div', { style: 'font-size:12px' },
          h('div', { text: label }),
          hint ? h('div', { style: 'font-size:11px;color:#999', text: hint }) : null),
        h('div', { style: 'display:flex;gap:4px;align-items:center' }, ctrl));
      sense.appendChild(r);
    };
    _senseLine('观察间隔', watchSel, '多久看一眼屏幕');
    _senseLine('屏幕评论间隔', [ccInp, h('span', { style: 'font-size:11px;color:#999', text: '分钟' })], '两次评论最小间隔（防唠叨）');
    _senseLine('主动聊天间隔', [ciMin, h('span', { style: 'font-size:11px;color:#999', text: '~' }), ciMax, h('span', { style: 'font-size:11px;color:#999', text: '分' })], '你闲置多久 TA 主动找你聊');
    box.appendChild(sense);

    // ★ 2026-09-11：AI 监督吃醋卡片已移到「陪伴页顶部」（见 _renderJealousyCard）。
    //   原先放这里 → 只有"陪伴进行中"才渲染，"没开陪伴就完全看不到设置"，属放置失误。
  }

  // 我的世界 → 小爱 Bot 状态
  if (cur.mode === 'minecraft') {
    const roleName = (typeof Chat !== 'undefined' && Chat.contact && Chat.contact.name) || 'TA';
    const mc = h('div', { class: 'cp-mc' });
    mc.appendChild(h('div', { class: 'cp-mc-title', text: '🎮 ' + roleName + ' 在游戏里' }));
    try {
      const res = await fetch('/api/bot/status?session_id=' + encodeURIComponent(window.Session?.getSessionId?.() || 'default') + '&character_id=' + encodeURIComponent((typeof Chat !== 'undefined' && Chat.contact) ? (Chat.contact.name || Chat.contact.id) : 'default'));
      const d = await res.json();
      if (d.online) {
        const act = d.current_action ? ' · ' + d.current_action : '';
        mc.appendChild(h('div', { class: 'cp-mc-line', text: '● 在线' + act }));
        if (d.player_name) mc.appendChild(h('div', { class: 'cp-mc-line', text: '玩家：' + d.player_name }));
        mc.appendChild(h('div', { class: 'cp-mc-hint', text: '指令队列：' + (d.urgent_queue_size || 0) + ' · 最近心跳：' + (d.heartbeat_age_sec == null ? '未知' : Math.round(d.heartbeat_age_sec) + '秒前') }));
      } else {
        mc.appendChild(h('div', { class: 'cp-mc-line off', text: '○ ' + roleName + ' 尚未连接游戏' }));
        mc.appendChild(h('div', { class: 'cp-mc-hint', text: '游戏里「对局域网开放」→ 改好端口 → 双击 minecraft_bot\\start.bat' }));
      }
    } catch (_) {
      mc.appendChild(h('div', { class: 'cp-mc-line off', text: '○ 暂时无法读取 Bot 状态' }));
    }
    mc.appendChild(h('button', { class: 'chip', text: '重新连接', onclick: () => _bindMinecraft() }));
    mc.appendChild(h('button', { class: 'chip', text: '刷新状态', onclick: () => _renderStatus(box, cur) }));
    box.appendChild(mc);
  }

  // 星露谷 → 农场状态 + 快捷指挥
  if (cur.mode === 'stardew') {
    const sd = h('div', { class: 'cp-mc' });
    sd.appendChild(h('div', { class: 'cp-mc-title', text: '🌾 星露谷农场' }));
    if (!_sdStatus.enabled) {
      sd.appendChild(h('div', { class: 'cp-mc-line off', text: '○ 未启用：按《星露谷AI陪伴-接入指南.md》配置后重启' }));
    } else if (!_sdStatus.online) {
      // ★ 真联机模式：骨子还没加入农场 → 展示三步指引 + 一键拉骨子实例
      sd.appendChild(h('div', { class: 'cp-mc-line off', text: '○ 骨子还没加入农场 —— 联机三步：' }));
      sd.appendChild(h('div', { class: 'cp-mc-hint', text: '① 你自己开游戏：Steam 启动 → 读档 → Esc→协作→主持（需盖过联机小屋）' }));
      sd.appendChild(h('div', { class: 'cp-mc-hint', text: '② 点「启动骨子的游戏实例」，自动弹出第二个游戏' }));
      sd.appendChild(h('div', { class: 'cp-mc-hint', text: '③ 在弹出的游戏里：协作→加入（列表空就点「直接 IP」填 127.0.0.1）→ 走进联机小屋' }));
      sd.appendChild(h('button', { class: 'chip', text: '🎮 启动骨子的游戏实例', style: 'margin-top:8px;', onclick: () => { _launchStardewClient(); setTimeout(() => _renderStatus(box, cur), 8000); } }));
      sd.appendChild(h('button', { class: 'chip', text: '🚀 帮我启动我的游戏', style: 'margin-top:8px;margin-left:6px;', onclick: () => { _launchStardew(); setTimeout(() => _renderStatus(box, cur), 3000); } }));
    } else {
      sd.appendChild(h('div', { class: 'cp-mc-line', text: '● 骨子正在农场里' + (_sdStatus.companion ? '（' + _sdStatus.companion + '）' : '') }));
      // 同伴实时状态（在做什么/模式/位置/体力，后端从游戏 bridge_data 读取）
      try {
        const r = await fetch('/api/stardew/status');
        const j = await r.json();
        for (const c of (j.companions || [])) {
          sd.appendChild(h('div', { class: 'cp-mc-line', text: '🌾 ' + (c.name || '骨子') + '：' + (c.status || '待机') + '（' + (c.location || '?') + ' · 体力 ' + (c.stamina == null ? '?' : c.stamina) + '% · ' + (c.mode || '?') + '模式）' }));
        }
      } catch (_) {}
      const quick = h('div', { style: 'margin-top:8px;' });
      ['浇个水', '收个菜', '去钓鱼', '去挖矿', '跟着我', '停下'].forEach((t) => {
        quick.appendChild(h('button', { class: 'chip', text: t, style: 'margin:4px 6px 0 0;', onclick: async () => {
          try {
            const r = await fetch('/api/stardew/command', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ text: t }),
            });
            const j = await r.json();
            toast(j.ok ? (j.reply || '已执行') : (j.error || '失败'));
          } catch (e) { toast('发送失败'); }
        } }));
      });
      sd.appendChild(quick);
      sd.appendChild(h('div', { class: 'cp-mc-hint', text: '也可以直接在 QQ 上说「去浇下水」「跟着我」' }));
    }
    sd.appendChild(h('button', { class: 'chip', text: '刷新状态', style: 'margin-top:8px;', onclick: () => { _refreshSdStatus(); _renderStatus(box, cur); } }));
    box.appendChild(sd);
  }
}

function _toggleCompanion(id, label, wrap) {
  const cur = _companionSetting();
  if (cur && cur.mode === id) {
    _saveCompanion(null, '');
    toast('已结束陪伴');
  } else {
    _saveCompanion(id, label);
    toast('已开启「' + label + '」陪伴');
  }
  renderCompanion();
}

// 游戏卡片点击：minecraft 走原开关；stardew 额外自动拉起游戏（未启动时）
function _toggleGame(g, wrap) {
  const cur = _companionSetting();
  if (g.id === 'stardew') {
    if (cur && cur.mode === g.id) {
      _saveCompanion(null, '');
      toast('已结束陪伴');
    } else {
      _saveCompanion(g.id, g.label);
      toast('已开启「星露谷」陪伴' + (_sdStatus.online ? '' : '，正在帮你启动游戏…'));
      if (!_sdStatus.online) _launchStardew();
    }
    renderCompanion();
    return;
  }
  _toggleCompanion(g.id, g.label, wrap);
}
