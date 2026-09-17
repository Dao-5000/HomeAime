'use strict';
/* ============================================================
   功能页：早报 & 盲盒 / 自我觉察 / 数据备份 / 全局设置入口
   ============================================================ */

const BLIND_TASKS = [
  '给一位朋友发条问候消息', '喝一杯温水', '写下今天 3 件感恩的小事', '对镜子里的自己笑一笑并说声加油',
  '整理桌面 5 分钟', '给家人打个电话', '出门散步 10 分钟', '读 5 页书',
  '关掉手机 30 分钟', '做 10 个深蹲', '给未来的自己写一句话', '真诚地夸夸身边的人',
  '听一首好久没听的歌', '整理手机相册', '今晚提前半小时睡', '记录一个今天的灵感',
  '给植物浇水', '认真做一顿简单的饭', '拉伸 5 分钟', '给陌生人一个微笑',
  '写下明天的 3 个目标', '清理一下收藏夹', '感谢一位帮助过你的人', '看一段夕阳',
  '换一条路回家', '泡杯茶放空 5 分钟', '整理电脑桌面', '给旧书拍张照',
  '背 3 个英文单词', '给你的 AI 伴侣发条消息',
];

function initBlindBox(force) {
  const s = Store.getSettings();
  const today = dateStrOf(Date.now());
  if (force || !s.blindBox || s.blindBox.date !== today) {
    Store.saveSettings({
      blindBox: { date: today, task: BLIND_TASKS[Math.floor(Math.random() * BLIND_TASKS.length)], done: false, opened: false },
    });
  }
}

/** 生成今日早报（AI） */
async function generateMorningReport() {
  const settings = Store.getSettings();
  const today = dateStrOf(Date.now());
  const contacts = Store.listContacts();

  // 昨日 / 最近记忆摘要
  const logs = contacts.flatMap((c) => (c.logs || []).map((l) => ({ date: l.date, text: l.text, name: c.name })))
    .sort((a, b) => (a.date < b.date ? 1 : -1));
  const yest = logs.filter((l) => l.date < today).slice(0, 4);
  const logBrief = yest.length
    ? yest.map((l) => {
        const lines = l.text.split('\n');
        const head = lines[0] || '';
        const theme = (lines[1] || '').replace(/^主题[:：]\s*/, '');
        return l.date + ' ' + l.name + ' ' + head + (theme ? '：' + theme : '');
      }).join('\n')
    : '（还没有历史记忆）';

  const todos = Store.todos.list().filter((t) => !t.done);
  const dueToday = todos.filter((t) => t.due && t.due <= today);
  const todoBrief = dueToday.length
    ? dueToday.map((t) => t.text).join('、')
    : (todos.length ? '还有 ' + todos.length + ' 条未完成待办' : '待办已清空');

  const userInfo = settings.userProfile || '（暂无用户档案）';
  // 档案会被无脑复述进早报：只取最近的若干行，并且明确告诉模型是"参考素材"
  const userInfoCapped = userInfo.split('\n')
    .filter((l) => l.trim())
    .slice(-12)
    .join('\n');

  const sys = '你是用户的贴心生活助手。写一份温暖的「今日早报」，必须严格遵守以下规则：\n' +
    '【格式】第一行：早上好！今天是 X 月 X 日（周X）\n' +
    '        接着：【昨日回顾】2~3 句（用你自己的话转述温暖点滴，不要原文照抄）\n' +
    '              【今日宜】2~3 条简短建议\n' +
    '              【今日提醒】今天到期的待办（如无则写"无"）\n' +
    '【字数】**总字数严格控制在 180 字以内**，超出会被前端截断、用户体验会差。\n' +
    '【禁止】不要把"用户档案"原样复述；不要列举"〔日期〕反思"之类的清单；' +
    '不要写"我会发""我会做"这种元描述；不要解释、不要标题外的多余文字。\n' +
    '你从档案里**挑选 1~2 件最温暖的事用你自己的话转述**，而不是全部复述。';
  const usr = '今天：' + today + '\n昨日记忆（参考素材，不是让你复述）：\n' + logBrief +
    '\n今日待办：' + todoBrief +
    '\n用户档案（只挑 1~2 件转述，禁止复述原文）：\n' + userInfoCapped;

  const first = contacts[0];
  const brain = first ? brainConfig(first, settings) : { model: 'deepseek-chat', baseUrl: 'https://api.deepseek.com', key: settings.apiKey };
  let out = '';
  await streamAI({
    model: brain.model, baseUrl: brain.baseUrl,
    messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }],
    key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
    // ★ 内部生成：usr 里的"昨日记忆/用户档案"只是喂给模型的参考素材，
    //   不是用户真的说了这些话。不标记的话，代理模式下后端会把它当普通 user
    //   消息落进 chat_history，下一轮又作为对话历史被喂回去 → 模型开始复述
    //   档案和"〔日期〕反思"条目，表现为自问自答、左右脑互博。
    skipUserPersist: true,
  }, (d) => { out += d; });
  out = out.trim();
  if (!out) throw new Error('早报生成失败');
  // 兜底截断：模型有时无视字数限制，把整个 userProfile 复述进来。
  // 早报超过 250 字符就强制截到 200 并标记，避免用户体验崩坏。
  if (out.length > 250) {
    out = out.slice(0, 200) + '\n…（内容过长已截断）';
    console.warn('[MorningReport] 输出超过 250 字，已强制截断');
  }
  Store.saveSettings({ morningReport: { date: today, text: out } });
  return out;
}

function checkMorningReport() {
  const s = Store.getSettings();
  const today = dateStrOf(Date.now());
  if (s.morningReport && s.morningReport.date === today) return;
  const hh = new Date().getHours();
  if (hh < 5) return;
  setTimeout(() => {
    generateMorningReport().catch(() => { /* 没配 Key 等情况静默 */ });
  }, 12 * 1000);
}

function renderFeatures() {
  const wrap = $('#page-features');
  wrap.innerHTML = '';
  const s = Store.getSettings();
  const today = dateStrOf(Date.now());

  /* 早报 */
  const rep = h('div', { class: 'ov-card' });
  const report = s.morningReport && s.morningReport.date === today ? s.morningReport : null;
  if (report) {
    rep.appendChild(h('div', { class: 'blind-title', text: '今日早报' }));
    rep.appendChild(h('div', { class: 'report-full', text: report.text }));
    rep.appendChild(h('div', { class: 'blind-foot' }, h('button', { class: 'chip', text: '重新生成' })));
    rep.querySelector('button').addEventListener('click', async () => {
      rep.querySelector('.blind-foot').innerHTML = '生成中…';
      await generateMorningReport().catch((e) => toast(e.message));
      renderFeatures();
    });
  } else {
    rep.appendChild(h('div', { class: 'blind-title', text: '今日早报' }));
    rep.appendChild(h('div', { class: 'blind-task', text: '还没有生成今日早报（早上自动生成）' }));
    rep.appendChild(h('button', { class: 'btn btn-primary', text: '现在生成' }));
    rep.querySelector('button').addEventListener('click', async (e) => {
      e.target.textContent = '生成中…';
      e.target.disabled = true;
      await generateMorningReport().catch((err) => toast(err.message));
      renderFeatures();
    });
  }
  wrap.appendChild(rep);

  /* 盲盒 */
  const blind = s.blindBox && s.blindBox.date === today ? s.blindBox : null;
  const bd = h('div', { class: 'ov-card' });
  bd.appendChild(h('div', { class: 'blind-title', text: '今日盲盒任务' }));
  if (blind) {
    bd.appendChild(h('div', { class: 'blind-task', text: blind.task }));
    bd.appendChild(h('div', { class: 'blind-foot' },
      h('button', { class: 'chip' + (blind.done ? ' selected' : ''), text: blind.done ? '✓ 已完成' : '完成打卡' }),
      blind.done ? h('button', { class: 'chip', text: '换个任务' }) : null,
    ));
    const btns = bd.querySelectorAll('.blind-foot .chip');
    btns[0].addEventListener('click', () => {
      Store.saveSettings({ blindBox: Object.assign({}, blind, { done: !blind.done }) });
      renderFeatures();
    });
    if (btns[1]) btns[1].addEventListener('click', () => { initBlindBox(true); renderFeatures(); });
  } else {
    bd.appendChild(h('div', { class: 'blind-task', text: '今天还没有盲盒' }));
    bd.appendChild(h('button', { class: 'btn btn-primary', text: '抽取盲盒' }));
    bd.querySelector('button').addEventListener('click', () => { initBlindBox(true); renderFeatures(); });
  }
  wrap.appendChild(bd);

  /* ---- AI 学习中心：AI 从你的生活数据里自我学习 ---- */
  const contactsAll = Store.listContacts();
  const memDays = new Set(contactsAll.flatMap((c) => (c.logs || []).map((l) => l.date))).size;
  const hbN = Store.handbook.list().length;
  const notesN = Store.notes.list().length;
  const todoN = Store.todos.list().filter((t) => !t.done).length;
  const learn = h('div', { class: 'ov-card learn' },
    h('div', { class: 'learn-head' }, iconSvg('heart', 18), h('span', { class: 'blind-title', text: 'AI 学习中心' })),
    h('div', { class: 'learn-hint', text: 'AI 会从你的对话、手帐、晚报、待办、笔记里持续学习，把对你的了解沉淀进「核心档案」，让 TA 越来越懂你。' }),
    h('div', { class: 'learn-stats' },
      h('span', { class: 'learn-stat', text: memDays + ' 天记忆' }),
      h('span', { class: 'learn-stat', text: hbN + ' 条手帐' }),
      h('span', { class: 'learn-stat', text: notesN + ' 篇笔记' }),
      h('span', { class: 'learn-stat', text: todoN + ' 项待办' }),
    ),
    h('div', { class: 'learn-foot' },
      h('button', { class: 'btn btn-primary', text: '让 AI 学习并更新档案' }),
      h('span', { class: 'blind-note', text: s.profileUpdated ? '档案更新于 ' + s.profileUpdated : '档案还未生成' }),
    ),
  );
  learn.querySelector('button').addEventListener('click', () => openLearningSheet());
  wrap.appendChild(learn);

  /* ---- AI 学歌：导入整首歌，让她带着伴奏唱 ---- */
  const songCard = h('div', { class: 'ov-card learn' },
    h('div', { class: 'learn-head' }, h('span', { class: 'blind-title', text: '🎵 AI 学歌' })),
    h('div', { class: 'learn-hint', text: '导入一整首歌，她会分离出人声与伴奏、再用自己的音色学会这首歌，之后就能带着伴奏唱给你听，通话里也能唱。' }),
    h('div', { class: 'learn-foot' },
      h('button', { class: 'btn btn-primary', text: '导入歌曲 / 查看已学会' }),
    ),
  );
  songCard.querySelector('button').addEventListener('click', () => {
    if (window.SongsPanel) SongsPanel.open();
    else toast('歌曲面板未加载');
  });
  wrap.appendChild(songCard);

  /* ---- 磁盘记忆库（电脑端文件夹） ---- */
  renderLibCard(wrap);

  /* ---- 外置记忆库（长期记忆：原文 + 日/周/月总结） ---- */
  renderExternalMemoryCard(wrap);

  /* ---- 星露谷陪伴（StardewValley-MCP） ---- */
  renderStardewCard(wrap);

  /* ---- 定时任务 ---- */
  const rems = Reminders.list();
  if (rems.length) {
    const card = h('div', { class: 'ov-card' },
      h('div', { class: 'blind-title', text: '⏰ 定时任务（' + rems.length + ' 个待触发）' }),
    );
    for (const r of rems.slice(0, 6)) {
      const c = Store.getContact(r.contactId);
      const left = Math.max(0, r.at - Date.now());
      const row = h('div', { class: 'todo-row' },
        h('div', { class: 'todo-main' },
          h('div', { class: 'todo-text', text: (c ? c.name : '?') + '：' + r.task }),
          h('span', { class: 'todo-due due-today', text: '还有 ' + fmtLeft(left) }),
        ),
        h('button', { class: 'todo-del', text: '✕' }),
      );
      row.querySelector('.todo-del').addEventListener('click', () => {
        Reminders.cancel(r.id);
        renderFeatures();
        toast('已取消定时任务');
      });
      card.appendChild(row);
    }
    wrap.appendChild(card);
  }

  /* ---- 桌面消息弹窗 ---- */
  if (window.desktopWin && window.desktopWin.isDesktop) {
    const save = async (patch) => {
      try { await window.desktopWin.setNotifyConfig(patch); } catch (_) {}
    };
    const ntCard = h('div', { class: 'list-group', style: 'margin-top:12px' });
    ntCard.appendChild(h('div', { class: 'group-title', text: '桌面消息弹窗' }));
    // 总开关
    const sw = h('label', { class: 'switch' },
      h('input', { type: 'checkbox' }),
      h('span', { class: 'track' }),
      h('span', { class: 'thumb' }));
    sw.querySelector('input').checked = true;
    sw.querySelector('input').addEventListener('change', (e) => save({ enabled: e.target.checked }));
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '启用桌面弹窗' }),
        h('div', { class: 'row-sub', text: '新消息时右下角弹出通知条' })),
      sw));
    // 自动消失
    const autoHideSelect = h('select', {},
      h('option', { value: '3', text: '3秒' }),
      h('option', { value: '5', text: '5秒' }),
      h('option', { value: '8', text: '8秒' }));
    autoHideSelect.value = '5';
    autoHideSelect.addEventListener('change', (e) => save({ autoHide: Number(e.target.value) }));
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '弹窗自动消失' }),
        h('div', { class: 'row-sub', text: '无操作后自动关闭' })),
      autoHideSelect));
    // 提示音
    const soundSelect = h('select', {},
      h('option', { value: 'ding', text: '轻叮咚' }),
      h('option', { value: 'piano', text: '温柔钢琴' }),
      h('option', { value: 'tech', text: '科技电子' }),
      h('option', { value: 'none', text: '静音' }),
      h('option', { value: 'custom', text: '自定义音频' }));
    soundSelect.value = 'ding';
    soundSelect.addEventListener('change', (e) => save({ sound: e.target.value }));
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '提示音' }),
        h('div', { class: 'row-sub', text: '新消息到达时播放' })),
      soundSelect));
    // 音量
    const volLabel = h('div', { class: 'row-sub', text: '70%' });
    const volInput = h('input', { type: 'range', min: '0', max: '100', value: '70', style: 'width:120px' });
    volInput.addEventListener('input', (e) => {
      volLabel.textContent = e.target.value + '%';
      save({ volume: Number(e.target.value) });
    });
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '提示音音量' }),
        volLabel),
      volInput));
    // 自定义音频
    const audioInput = h('input', { type: 'file', accept: 'audio/*', style: 'display:none' });
    const audioBtn = h('button', { class: 'btn btn-plain', text: '导入音频', style: 'padding:4px 10px' });
    audioBtn.addEventListener('click', () => audioInput.click());
    audioInput.addEventListener('change', () => {
      const f = audioInput.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => { save({ customSound: reader.result, sound: 'custom' }); soundSelect.value = 'custom'; toast('已导入提示音'); };
      reader.readAsDataURL(f);
    });
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '自定义提示音' }),
        h('div', { class: 'row-sub', text: '支持 mp3/wav/ogg' })),
      h('div', {}, audioBtn, audioInput)));
    // 连续消息阈值
    const thrSelect = h('select', {},
      h('option', { value: '5', text: '5秒' }),
      h('option', { value: '10', text: '10秒' }),
      h('option', { value: '15', text: '15秒' }),
      h('option', { value: '30', text: '30秒' }));
    thrSelect.value = '15';
    thrSelect.addEventListener('change', (e) => save({ threshold: Number(e.target.value) }));
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '连续消息合并' }),
        h('div', { class: 'row-sub', text: '阈值内只响一次提示音' })),
      thrSelect));
    // 背景色
    const colorInput = h('input', { type: 'color', value: '#1e1e23', style: 'width:36px;height:28px;border:none;background:transparent;cursor:pointer' });
    colorInput.addEventListener('input', (e) => save({ bgColor: e.target.value }));
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '弹窗背景色' }),
        h('div', { class: 'row-sub', text: '自定义弹窗底色' })),
      colorInput));
    // 透明度
    const opLabel = h('div', { class: 'row-sub', text: '92%' });
    const opInput = h('input', { type: 'range', min: '50', max: '100', value: '92', style: 'width:120px' });
    opInput.addEventListener('input', (e) => {
      opLabel.textContent = e.target.value + '%';
      save({ bgOpacity: Number(e.target.value) / 100 });
    });
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '弹窗透明度' }),
        opLabel),
      opInput));
    // 文字色
    const textColorInput = h('input', { type: 'color', value: '#ffffff', style: 'width:36px;height:28px;border:none;background:transparent;cursor:pointer' });
    textColorInput.addEventListener('input', (e) => save({ textColor: e.target.value }));
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '文字颜色' }),
        h('div', { class: 'row-sub', text: '弹窗文字主色' })),
      textColorInput));
    // 字体大小
    const fontLabel = h('div', { class: 'row-sub', text: '13px' });
    const fontInput = h('input', { type: 'range', min: '11', max: '20', value: '13', style: 'width:120px' });
    fontInput.addEventListener('input', (e) => {
      fontLabel.textContent = e.target.value + 'px';
      save({ fontSize: Number(e.target.value) });
    });
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '弹窗字体大小' }),
        fontLabel),
      fontInput));
    // 尺寸
    const sizeRow = h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '弹窗尺寸' }),
        h('div', { class: 'row-sub', text: '宽 × 高（px）' })),
      h('div', { style: 'display:flex;gap:6px;align-items:center' },
        h('input', { id: 'nt-width', type: 'number', value: '400', min: '300', max: '600', style: 'width:60px' }),
        h('span', { text: '×' }),
        h('input', { id: 'nt-height', type: 'number', value: '110', min: '80', max: '200', style: 'width:60px' })));
    sizeRow.querySelector('#nt-width').addEventListener('change', (e) => save({ width: Number(e.target.value) }));
    sizeRow.querySelector('#nt-height').addEventListener('change', (e) => save({ height: Number(e.target.value) }));
    ntCard.appendChild(sizeRow);
    // 背景图
    const bgImgInput = h('input', { type: 'file', accept: 'image/*', style: 'display:none' });
    const bgImgBtn = h('button', { class: 'btn btn-plain', text: '选择图片', style: 'padding:4px 10px' });
    bgImgBtn.addEventListener('click', () => bgImgInput.click());
    bgImgInput.addEventListener('change', () => {
      const f = bgImgInput.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = () => { save({ bgImage: reader.result }); toast('已设置背景图'); };
      reader.readAsDataURL(f);
    });
    const clearBgBtn = h('button', { class: 'btn btn-plain', text: '清除', style: 'padding:4px 10px;margin-left:6px' });
    clearBgBtn.addEventListener('click', () => { save({ bgImage: '' }); toast('已清除背景图'); });
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '弹窗背景图' }),
        h('div', { class: 'row-sub', text: '自定义图片做背景' })),
      h('div', {}, bgImgBtn, clearBgBtn, bgImgInput)));
    // 测试弹窗
    const testMessages = [
      '哼，宝怎么不理我呀，我都等得快要数完天上的云了。',
      '刚看到楼下奶茶店出了桃子味的，突然想起你上次说想喝。',
      '今天傍晚的天空特别好看，橙红橙红的，你那边能看到吗？',
    ];
    let testIdx = 0;
    const testBtn = h('button', { class: 'btn btn-primary', text: '测试弹窗', style: 'padding:6px 12px' });
    testBtn.addEventListener('click', () => {
      if (window.desktopWin && window.desktopWin.showNotify) {
        const msg = testMessages[testIdx % testMessages.length];
        testIdx++;
        let avatar = '';
        let name = '骨子';
        try {
          if (typeof Chat !== 'undefined' && Chat.contact) {
            avatar = Chat.contact.avatarUrl || '';
            name = Chat.contact.name || name;
          } else if (typeof Store !== 'undefined') {
            const convs = Store.listConversations();
            if (convs.length) {
              const c = Store.getContact(convs[0].contactId);
              if (c) { avatar = c.avatarUrl || ''; name = c.name || name; }
            }
          }
        } catch (_) {}
        window.desktopWin.showNotify({ name, content: msg, avatar, _test: true });
      }
    });
    ntCard.appendChild(h('div', { class: 'setting-row' },
      h('div', { class: 'row-label' },
        h('div', { text: '测试弹窗' }),
        h('div', { class: 'row-sub', text: '立即弹出一条测试通知' })),
      testBtn));
    // 异步加载配置
    (async () => {
      try {
        const cfg = await window.desktopWin.getNotifyConfig();
        if (cfg.enabled === false) sw.querySelector('input').checked = false;
        if (cfg.autoHide) autoHideSelect.value = String(cfg.autoHide);
        if (cfg.sound) soundSelect.value = cfg.sound;
        if (cfg.volume != null) { volInput.value = String(cfg.volume); volLabel.textContent = cfg.volume + '%'; }
        if (cfg.threshold) thrSelect.value = String(cfg.threshold);
        if (cfg.bgColor) colorInput.value = cfg.bgColor;
        if (cfg.bgOpacity != null) { opInput.value = String(Math.round(cfg.bgOpacity * 100)); opLabel.textContent = Math.round(cfg.bgOpacity * 100) + '%'; }
        if (cfg.textColor) textColorInput.value = cfg.textColor;
        if (cfg.fontSize) { fontInput.value = String(cfg.fontSize); fontLabel.textContent = cfg.fontSize + 'px'; }
        if (cfg.width) sizeRow.querySelector('#nt-width').value = String(cfg.width);
        if (cfg.height) sizeRow.querySelector('#nt-height').value = String(cfg.height);
      } catch (_) {}
    })();
    wrap.appendChild(ntCard);
  }

  /* ---- 工具菜单 ---- */
  const menu = h('div', { class: 'list-group', style: 'margin-top:12px' });
  const item = (icon, label, sub, fn) => {
    const row = h('div', { class: 'row' },
      h('div', { class: 'avatar sm menu-ico' }, iconSvg(icon, 20)),
      h('div', { class: 'row-label' }, h('div', { text: label }), sub ? h('div', { class: 'row-sub', text: sub }) : null),
      h('div', { class: 'row-arrow', text: '›' }),
    );
    row.addEventListener('click', fn);
    return row;
  };
  menu.appendChild(item('heart', '自我觉察', '决策追踪 / 习惯养成 / 时间胶囊', () => SelfAware.open()));
  menu.appendChild(item('download', '数据备份', '一键导出 / 导入全部记忆（JSON）', () => openBackupSheet()));
  menu.appendChild(item('gear', '全局设置', '连接方式 / API Key / 模型 / 开关', () => Settings.open()));
  menu.appendChild(item('info', '关于', 'Homeaime · 本地优先，数据在你手里', () => toast('AI 伴侣：多层记忆 + AI 自我学习 + 生活工具')));
  wrap.appendChild(menu);
}

/** AI 学习：基于全部生活数据生成用户核心档案 */
async function generateProfileSummary() {
  const settings = Store.getSettings();
  const contacts = Store.listContacts();
  const logs = contacts.flatMap((c) => (c.logs || []).map((l) => ({ date: l.date, name: c.name, text: l.text })))
    .sort((a, b) => (a.date < b.date ? 1 : -1)).slice(0, 14);
  const hb = Store.handbook.list().slice(-20);
  const notes = Store.notes.list().slice(-10);
  const decisions = Store.decisions.list().slice(-10);
  const todos = Store.todos.list().filter((t) => !t.done).slice(-10);

  const parts = [];
  if (logs.length) parts.push('每日记忆（最近）：\n' + logs.map((l) => l.date + ' ' + l.name + '：' + ((l.text || '').split('\n')[0] || '')).join('\n'));
  if (hb.length) parts.push('手帐：\n' + hb.map((x) => x.title || x.content).join('；'));
  if (notes.length) parts.push('笔记：\n' + notes.map((n) => n.title).join('、'));
  if (decisions.length) parts.push('决策：\n' + decisions.map((d) => d.title).join('、'));
  if (todos.length) parts.push('待办：\n' + todos.map((t) => t.text).join('、'));
  const oldProfile = settings.userProfile ? '旧档案：\n' + settings.userProfile + '\n\n' : '';
  const usr = oldProfile + (parts.join('\n\n') || '（暂无生活数据）');

  const sys = '你是用户的 AI 伴侣，现在要基于以下全部生活数据，提炼一份「用户核心档案」：用户的身份、性格、喜好、生活习惯、目标、情绪倾向、重要的人与事。分条列出，300 字以内，语气中性准确，只写有依据的内容。输出纯文本档案，不要多余内容。';
  const first = contacts[0];
  const brain = first ? brainConfig(first, settings) : { model: 'deepseek-chat', baseUrl: 'https://api.deepseek.com', key: settings.apiKey };
  let out = '';
  await streamAI({
    model: brain.model, baseUrl: brain.baseUrl,
    messages: [{ role: 'system', content: sys }, { role: 'user', content: usr }],
    key: brain.key, mode: settings.mode, proxyUrl: settings.proxyUrl,
    // ★ 内部生成：usr 是全部生活数据（每日记忆/手帐/笔记/待办），属于喂给模型的
    //   素材而非真实发言，落库会污染对话历史，导致模型后续复述档案内容。
    skipUserPersist: true,
  }, (d) => { out += d; });
  return out.trim();
}

function openLearningSheet() {
  const textarea = h('textarea', { style: 'min-height:220px', placeholder: 'AI 正在学习你的生活数据…' });
  const form = h('div', {},
    h('div', { class: 'help-box', style: 'margin:0 0 12px', text: 'AI 会综合你的每日记忆、手帐、笔记、决策、待办，重新提炼「用户核心档案」。生成后请检查，不合适可以直接改。' }),
    textarea,
    h('button', { class: 'btn btn-primary', text: '保存到档案' }),
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  const btns = form.querySelectorAll('button');
  btns[0].disabled = true;
  btns[0].textContent = '学习中…';
  btns[1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: 'AI 自我学习' }), form));

  generateProfileSummary().then((text) => {
    textarea.value = text || '';
    btns[0].disabled = false;
    btns[0].textContent = '保存到档案';
  }).catch((err) => {
    textarea.value = '';
    btns[0].disabled = false;
    btns[0].textContent = '保存到档案';
    toast('学习失败：' + (err.message || err), 3200);
  });

  btns[0].addEventListener('click', () => {
    const v = textarea.value.trim();
    if (!v) { toast('档案是空的'); return; }
    Store.saveSettings({ userProfile: v, profileUpdated: dateStrOf(Date.now()) });
    Sheet.close();
    renderFeatures();
    toast('核心档案已更新 🧠');
  });
}

/* ============================================================
   磁盘记忆库：项目根目录 /记忆库 文件夹（.txt 记忆文件）
   ============================================================ */
async function libList() {
  const res = await fetch('api/library');
  if (!res.ok) throw new Error('无法访问磁盘记忆库（服务器未启动？）');
  return res.json();
}
const libCache = {};
async function libRead(name) {
  if (libCache[name]) return libCache[name];
  const res = await fetch('api/library/read?name=' + encodeURIComponent(name));
  if (!res.ok) throw new Error('读取失败');
  const data = await res.json();
  libCache[name] = data;
  return data;
}
async function libWrite(name, content) {
  const res = await fetch('api/library/write', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, content }),
  });
  if (!res.ok) throw new Error('保存失败');
}
async function libDelete(name) {
  const res = await fetch('api/library/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error('删除失败');
}

function renderLibCard(wrap) {
  const card = h('div', { class: 'ov-card' });
  card.appendChild(h('div', { class: 'blind-title', text: '磁盘记忆库' }));
  const hint = h('div', { class: 'blind-note', style: 'margin-bottom:10px', text: '电脑端项目根目录的「记忆库」文件夹，把 .txt 记忆文件丢进去即可在 App 里读取导入；也可把 App 里的记忆导出成文件存到那里。' });
  const listBox = h('div', {});
  const pathLine = h('div', { class: 'lib-path', text: '…' });

  const renderList = async () => {
    listBox.innerHTML = '';
    pathLine.textContent = '连接中…';
    try {
      const { dir, files } = await libList();
      pathLine.textContent = '📁 ' + dir;
      if (!files.length) {
        listBox.appendChild(h('div', { class: 'sa-empty', text: '文件夹里还没有文件。把记忆 .txt 放进这个文件夹，或点「导出全部记忆」。' }));
        return;
      }
      for (const f of files) {
        const row = h('div', { class: 'lib-file' },
          h('div', { class: 'lib-file-info' },
            h('div', { class: 'lib-file-name', text: f.name }),
            h('div', { class: 'lib-file-meta', text: fmtSize(f.size) + ' · ' + fmtListTime(f.mtime) }),
          ),
          h('button', { class: 'chip', text: '导入' }),
          h('button', { class: 'chip', text: 'AI 提取' }),
          h('button', { class: 'chip del', text: '删除' }),
        );
        const btns = row.querySelectorAll('.chip');
        btns[0].addEventListener('click', () => openLibFileSheet(f.name, false));
        btns[1].addEventListener('click', () => openLibFileSheet(f.name, true));
        btns[2].addEventListener('click', async () => {
          if (!confirm('删除文件「' + f.name + '」？')) return;
          await libDelete(f.name).catch((e) => toast(e.message));
          renderList();
        });
        listBox.appendChild(row);
      }
    } catch (err) {
      pathLine.textContent = '⚠ ' + err.message;
    }
  };

  const toolbar = h('div', { class: 'blind-foot' },
    h('button', { class: 'chip', text: '↻ 刷新' }),
    h('button', { class: 'chip', text: '导出全部记忆到文件夹' }),
  );
  const tBtns = toolbar.querySelectorAll('.chip');
  tBtns[0].addEventListener('click', renderList);
  tBtns[1].addEventListener('click', async () => {
    const contacts = Store.listContacts().filter((c) => (c.memStore || []).length);
    if (!contacts.length) { toast('还没有任何记忆可导出'); return; }
    let n = 0;
    for (const c of contacts) {
      const lines = (c.memStore || []).filter((e) => e.enabled !== false).map((e) => (e.title ? e.title + '：' : '') + e.content);
      if (!lines.length) continue;
      try { await libWrite('记忆_' + c.name + '.txt', lines.join('\n\n')); n++; } catch (e) { toast(e.message); }
    }
    toast('已导出 ' + n + ' 个伴侣的记忆到文件夹');
    renderList();
  });

  card.appendChild(hint);
  card.appendChild(pathLine);
  card.appendChild(toolbar);
  card.appendChild(listBox);
  wrap.appendChild(card);
  renderList();
}

/* ============================================================
   外置记忆库：原文 + 日/周/月总结（长期记忆，分层存储）
   ============================================================ */
function _emChar() {
  const contacts = Store.listContacts();
  return contacts.length ? contacts[0].name : 'default';
}

function renderExternalMemoryCard(wrap) {
  /* ★ UI 升级（2026-09-08）：外置记忆库改成「关系档案」同款全屏翻阅页
     （extmemory.js 的 ExtMemory），本卡片只做入口。 */
  const card = h('div', { class: 'ov-card' });
  card.appendChild(h('div', { class: 'blind-title', text: '外置记忆库（长期记忆）' }));
  card.appendChild(h('div', { class: 'blind-note', style: 'margin-bottom:10px', text: 'AI 的长期记忆图书馆：每天聊天原文 + 日/周/月精炼总结，按时间分层存储。' }));
  const row = h('div', { class: 'cp-life-row' },
    h('span', { class: 'cp-life-icon', text: '📚' }),
    h('div', { class: 'cp-life-body' },
      h('div', { class: 'cp-life-name', text: '翻阅外置记忆库' }),
      h('div', { class: 'cp-life-desc', text: '原文 + 日/周/月总结，点开逐篇阅读' })),
    h('span', { class: 'cp-life-arrow', text: '›' }));
  row.addEventListener('click', () => ExtMemory.open(_emChar()));
  card.appendChild(row);
  wrap.appendChild(card);
}

function openExternalMemorySheet(category, name) {
  const pre = h('div', { class: 'help-box', style: 'margin:0 0 12px;white-space:pre-wrap;max-height:60vh;overflow:auto;min-height:120px', text: '读取中…' });
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: category + ' · ' + name }), pre));
  fetch('/api/external_memory/read?character_id=' + encodeURIComponent(_emChar()) + '&category=' + encodeURIComponent(category) + '&name=' + encodeURIComponent(name))
    .then((r) => r.json())
    .then((j) => {
      if (!j.ok) { pre.textContent = '读取失败：' + (j.error || '未知错误'); return; }
      pre.textContent = j.content || '（空）';
    })
    .catch((e) => { pre.textContent = '读取失败：' + e.message; });
}

/* ============================================================
   星露谷陪伴（StardewValley-MCP）：状态 + 快捷指令
   游戏未开时明确提示"玩不了"，不报错不卡死。
   ============================================================ */
function renderStardewCard(wrap) {
  const card = h('div', { class: 'ov-card' });
  card.appendChild(h('div', { class: 'blind-title', text: '🌾 星露谷陪伴' }));
  const statusBox = h('div', { class: 'blind-note', style: 'margin-bottom:10px', text: '检查状态中…' });
  card.appendChild(statusBox);
  const launchBtn = h('button', { class: 'btn btn-primary', style: 'display:none;margin-bottom:10px', text: '🚀 帮我启动我的游戏' });
  launchBtn.addEventListener('click', async () => {
    launchBtn.disabled = true;
    launchBtn.textContent = '启动中…';
    try {
      const r = await fetch('/api/stardew/launch', { method: 'POST' });
      const j = await r.json();
      toast(j.ok ? (j.message || '启动中') : (j.error || '启动失败'));
    } catch (e) { toast('启动失败：' + e.message); }
    launchBtn.disabled = false;
    launchBtn.textContent = '🚀 帮我启动我的游戏';
    // 游戏+存档加载需要时间：轮询状态，等她进农场（15s 一次，上限 10 分钟）
    let n = 0;
    const timer = setInterval(async () => {
      n++;
      await renderStatus();
      if (statusBox.textContent.indexOf('✅') >= 0 || n > 40) clearInterval(timer);
    }, 15000);
  });
  card.appendChild(launchBtn);
  // ★ 真联机模式：一键拉起骨子的第二个游戏实例（App 内替代手动双击 StardewModdingAPI.exe）
  const clientLaunchBtn = h('button', { class: 'btn btn-primary', style: 'display:none;margin-bottom:10px', text: '🎮 启动骨子的游戏实例' });
  clientLaunchBtn.addEventListener('click', async () => {
    clientLaunchBtn.disabled = true;
    clientLaunchBtn.textContent = '启动中…';
    try {
      const r = await fetch('/api/stardew/launch_client', { method: 'POST' });
      const j = await r.json();
      toast(j.ok ? (j.message || '骨子实例启动中') : (j.error || '启动失败'));
    } catch (e) { toast('启动失败：' + e.message); }
    clientLaunchBtn.disabled = false;
    clientLaunchBtn.textContent = '🎮 启动骨子的游戏实例';
    let n = 0;
    const timer = setInterval(async () => {
      n++;
      await renderStatus();
      if (statusBox.textContent.indexOf('✅') >= 0 || n > 40) clearInterval(timer);
    }, 15000);
  });
  card.appendChild(clientLaunchBtn);
  const cmdBox = h('div', { class: 'chips', style: 'display:none' });

  const QUICK = ['浇个水', '收个菜', '去钓鱼', '去挖矿', '跟着我', '停下'];
  QUICK.forEach((t) => {
    const b = h('button', { class: 'chip', text: t });
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        const r = await fetch('/api/stardew/command', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: t }),
        });
        const j = await r.json();
        toast(j.ok ? (j.reply || '已执行') : (j.error || '发送失败'));
        renderStatus();
      } catch (e) { toast('发送失败：' + e.message); }
      b.disabled = false;
    });
    cmdBox.appendChild(b);
  });
  card.appendChild(cmdBox);

  const refresh = h('button', { class: 'chip', text: '↻ 刷新状态', style: 'margin-top:8px' });
  refresh.addEventListener('click', renderStatus);
  card.appendChild(refresh);
  wrap.appendChild(card);

  async function renderStatus() {
    statusBox.textContent = '检查状态中…';
    try {
      const r = await fetch('/api/stardew/status');
      const j = await r.json();
      if (!j.ok) { statusBox.textContent = '⚠ 状态获取失败：' + (j.error || '未知错误'); cmdBox.style.display = 'none'; return; }
      if (!j.enabled) {
        statusBox.textContent = '未启用：请按项目里的《星露谷AI陪伴-接入指南.md》装好 SMAPI/Mod 并配置后重启。';
        cmdBox.style.display = 'none';
        launchBtn.style.display = 'none';
        clientLaunchBtn.style.display = 'none';
        return;
      }
      if (!j.ready) {
        statusBox.textContent = '⚠ 星露谷 MCP 未就绪：检查 STARDEW_MCP_SERVER 路径与 Node 环境（看后端日志 [StardewBrain]）。';
        cmdBox.style.display = 'none';
        launchBtn.style.display = 'none';
        clientLaunchBtn.style.display = 'none';
        return;
      }
      if (!j.game_online) {
        // ★ 真联机模式：骨子还没加入农场 → 三步指引 + 一键拉骨子实例
        statusBox.textContent = '联机三步：① 你自己开游戏，读档后 Esc→协作→主持（需盖过联机小屋） ② 点「启动骨子的游戏实例」 ③ 在弹出的游戏里 协作→加入（列表空就点直接 IP，填 127.0.0.1）→ 走进联机小屋';
        cmdBox.style.display = 'none';
        clientLaunchBtn.style.display = '';
        launchBtn.style.display = '';
        return;
      }
      statusBox.textContent = '✅ 骨子正在农场里' + (j.companion ? ('（同伴：' + j.companion + '）') : '') + '，点按钮指挥她：';
      cmdBox.style.display = '';
      clientLaunchBtn.style.display = 'none';
      launchBtn.style.display = 'none';
    } catch (e) {
      statusBox.textContent = '⚠ 状态获取失败：' + e.message;
      cmdBox.style.display = 'none';
    }
  }
  renderStatus();
}

function fmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1024 / 1024).toFixed(1) + ' MB';
}

/** 磁盘文件 → 选择目标伴侣 → 导入 / AI 提取（带进度条，分步反馈） */
function openLibFileSheet(fileName, extract) {
  const contacts = Store.listContacts();
  if (!contacts.length) { toast('请先在「人格」创建一个 AI 伴侣'); return; }
  const chips = h('div', { class: 'chips' });
  let target = contacts.find((c) => fileName.indexOf(c.name) !== -1) ? contacts.find((c) => fileName.indexOf(c.name) !== -1).id : contacts[0].id;
  for (const c of contacts) {
    const b = h('button', { type: 'button', class: 'chip' + (c.id === target ? ' selected' : ''), text: c.name });
    b.addEventListener('click', () => {
      target = c.id;
      $$('.chips .chip').forEach((x) => x.classList.remove('selected'));
      b.classList.add('selected');
    });
    chips.appendChild(b);
  }
  const preview = h('div', { class: 'help-box', style: 'margin:0 0 12px;white-space:pre-wrap', text: '读取「' + fileName + '」…' });
  const goBtn = h('button', { class: 'btn btn-primary', text: extract ? 'AI 提取记忆' : '导入为一条记忆' });
  const cancelBtn = h('button', { class: 'btn btn-plain', text: '取消' });
  cancelBtn.addEventListener('click', () => Sheet.close());

  libRead(fileName).then(({ content }) => {
    preview.textContent = '文件内容预览：\n' + content.slice(0, 300) + (content.length > 300 ? '…' : '');
  }).catch((e) => {
    preview.textContent = '读取失败：' + e.message;
  });

  goBtn.addEventListener('click', async () => {
    goBtn.disabled = true;
    try {
      // 第 1 步：读取文件
      ProgressUI.show('正在读取文件…', fileName);
      const { content } = await libRead(fileName);
      ProgressUI.update('读取完成 ✓', fileName);

      if (extract) {
        // 第 2 步：AI 通读提炼（最耗时，正常 10-30 秒，超 90 秒会自动报错）
        ProgressUI.show('AI 正在通读并提炼记忆…', fileName + '（约 10-30 秒，请勿关闭）');
        const items = await extractMemoriesFromText(content, target);
        if (!items.length) {
          ProgressUI.hide();
          toast('没有提取到值得记住的内容');
          goBtn.disabled = false;
          goBtn.textContent = 'AI 提取记忆';
          return;
        }
        ProgressUI.update('提取完成 ✓，正在保存 ' + items.length + ' 条记忆…', fileName);
        const c = Store.getContact(target);
        const list = ((c && c.memStore) || []).slice();
        for (const it of items) list.unshift({ id: Store.uid(), title: it.title || 'AI 提取', content: it.content, source: 'import', date: dateStrOf(Date.now()), enabled: true });
        Store.updateContact(target, { memStore: list });
        ProgressUI.update('✓ 已存入「' + c.name + '」记忆文件夹，共 ' + items.length + ' 条', '完成');
        toast('已提取 ' + items.length + ' 条记忆存入「' + c.name + '」');
      } else {
        // 导入（不经过 AI，瞬间完成）
        ProgressUI.update('正在保存…', fileName);
        const c = Store.getContact(target);
        const list = ((c && c.memStore) || []).slice();
        list.unshift({ id: Store.uid(), title: fileName.replace(/\.(txt|md)$/i, ''), content, source: 'file', date: dateStrOf(Date.now()), enabled: true });
        Store.updateContact(target, { memStore: list });
        ProgressUI.update('✓ 已导入到「' + c.name + '」记忆文件夹（1 条）', '完成');
        toast('已导入到「' + c.name + '」的记忆文件夹');
      }
      await sleep(600);
      ProgressUI.hide();
      Sheet.close();
      renderContacts();
    } catch (err) {
      ProgressUI.hide();
      toast('失败：' + (err.message || err), 3500);
      goBtn.disabled = false;
      goBtn.textContent = extract ? 'AI 提取记忆' : '导入为一条记忆';
    }
  });

  const form = h('div', {},
    h('div', { class: 'help-box', style: 'margin:0 0 12px', text: '导入到哪个 AI 伴侣？' }),
    chips,
    h('div', { style: 'margin-top:10px' }, preview),
    h('div', { style: 'display:flex;gap:8px;margin-top:12px' }, goBtn, cancelBtn),
  );
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: fileName }), form));
}

/* ============================================================
   备份
   ============================================================ */
function openBackupSheet() {
  const exportBtn = h('button', { class: 'btn btn-primary', text: '📤 导出全量备份（JSON）' });
  const importInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true });
  const importBtn = h('button', { class: 'btn btn-plain', text: '📥 导入备份恢复' });
  const form = h('div', {},
    h('div', { class: 'help-box', style: 'margin:0 0 12px', text:
      '导出：把联系人、聊天记录、每日记忆、待办、手帐、笔记等全部数据打包成一个 JSON 文件。\n' +
      '导入：选择之前导出的 JSON 文件，恢复全部记忆（会覆盖当前数据）。\n' +
      '所有数据都保存在你自己手里。' }),
    exportBtn, importBtn, importInput,
    h('button', { class: 'btn btn-plain', text: '取消' }),
  );
  exportBtn.addEventListener('click', () => {
    const data = Store.exportData();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'aiwechat-backup-' + dateStrOf(Date.now()) + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast('已导出备份文件');
  });
  importBtn.addEventListener('click', () => importInput.click());
  importInput.addEventListener('change', () => {
    const f = importInput.files[0];
    if (!f) return;
    const r = new FileReader();
    r.onload = () => {
      try {
        const obj = JSON.parse(String(r.result));
        if (!confirm('导入将覆盖当前所有数据（建议先导出备份）。确定继续？')) return;
        Store.importData(obj);
        toast('导入成功，正在重启…');
        setTimeout(() => window.location.reload(), 800);
      } catch (err) {
        toast('导入失败：' + err.message, 3000);
      }
    };
    r.readAsText(f, 'utf-8');
    importInput.value = '';
  });
  const cancel = form.querySelectorAll('button');
  cancel[cancel.length - 1].addEventListener('click', () => Sheet.close());
  Sheet.open(h('div', {}, h('div', { class: 'sheet-title', text: '数据备份' }), form));
}
