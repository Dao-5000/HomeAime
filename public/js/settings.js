'use strict';
/* ============================================================
   全局设置页（覆盖层）：连接模式 / API Key / 模型 / 主动消息 / 视觉模型 / 用户档案 / 数据 / 帮助
   ============================================================ */
const Settings = {
  open() {
    renderSettings();
    $('#settings-page').classList.add('open');
  },
  close() { $('#settings-page').classList.remove('open'); },
};

async function savePcConfig(patch, options) {
  try {
    const res = await fetch('/api/pc/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data?.error?.message || data?.error || '配置保存失败');
    window.__pcConfig = Object.assign({}, window.__pcConfig || {}, data.config || data, patch);
    return data;
  } catch (e) {
    if (options && options.throwOnError) throw e;
    toast('配置保存失败：' + (e.message || '后端不可用'));
    return null;
  }
}

window.savePcConfig = savePcConfig;

function renderSettings() {
  const wrap = $('#settings-body');
  wrap.innerHTML = '';
  const s = Store.getSettings();

  // ★ 自动同步历史 Key：之前版本 Key 只存浏览器 localStorage（后端读不到），
  //   这里打开设置页时若本地有 Key 就静默同步给后端，语音通话/后端 LLM 才能用
  if (s.apiKey && s.apiKey.trim().startsWith('sk-')) {
    savePcConfig({ api_key: s.apiKey.trim() });
  }

  /* ---- 连接方式 ---- */
  const modeWrap = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '连接方式' }),
  );
  const modeRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: 'AI 连接模式' }),
    h('select', {},
      h('option', { value: 'proxy', text: '代理模式（Key 在服务器）' }),
      h('option', { value: 'direct', text: '直连模式（Key 在手机）' }),
    ),
  );
  modeRow.querySelector('select').value = s.mode;
  modeRow.querySelector('select').addEventListener('change', (e) => {
    Store.saveSettings({ mode: e.target.value });
    renderSettings();
  });
  modeWrap.appendChild(modeRow);

  if (s.mode === 'direct') {
    modeWrap.appendChild(h('div', { class: 'help-box', text:
      '直连模式：手机直接请求 DeepSeek 官方接口，不需要服务器，电脑可以关机。\n' +
      'API Key 保存在手机本地；发图片需要视觉模型，请使用代理模式。' }));
  } else {
    const hasServerKey = window.__serverKey;
    modeWrap.appendChild(h('div', { class: 'help-box', text: hasServerKey
      ? '✓ 检测到电脑服务器已配置 Key，手机上无需再填。'
      : '代理模式：聊天请求经过服务器转发，Key 存在服务器上（config.json）。\n' +
        '当前服务器未配置 Key，请在下方填写（会随请求发送给服务器）。' }));
  }

  /* ---- DeepSeek API Key ---- */
  const keyGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: 'DeepSeek API Key' }),
  );
  const keyRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: 'API Key' }),
    h('input', { type: 'text', placeholder: 'sk-...', value: s.apiKey }),
  );
  const keyInput = keyRow.querySelector('input');
  keyInput.addEventListener('change', () => {
    const k = keyInput.value.trim();
    Store.saveSettings({ apiKey: k });
    // ★ 同步到后端 config.json：语音通话/后端 LLM 走 config.api_key()，只存 localStorage 后端读不到
    savePcConfig({ api_key: k });
    toast(k ? 'Key 已保存（已同步后端）' : 'Key 已清空');
  });
  keyGroup.appendChild(keyRow);

  /* ---- 智谱 GLM Key ---- */
  const glmKeyGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '智谱 GLM Key' }),
  );
  const glmKeyRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: 'GLM API Key' }),
    h('input', { type: 'text', placeholder: '智谱开放平台的 Key', value: s.glmKey || '' }),
  );
  const glmKeyInput = glmKeyRow.querySelector('input');
  glmKeyInput.addEventListener('change', () => {
    const k = glmKeyInput.value.trim();
    Store.saveSettings({ glmKey: k });
    savePcConfig({ zhipu_api_key: k });
    toast(k ? '智谱 Key 已保存（已同步后端）' : '智谱 Key 已清空');
  });
  glmKeyRow.appendChild(h('a', {
    class: 'key-link', href: 'javascript:;',
    text: '没有Key？点这里',
    onclick: () => window.open('https://open.bigmodel.cn', '_blank'),
  }));
  glmKeyGroup.appendChild(glmKeyRow);

  /* ★ 模型设置已迁至「人格设置」（角色卡：主脑/理解层/记忆提炼），
     全局设置页不再提供模型选择（2026-09-11 统一数据目录配套）。 */

  /* ---- 真人感 ---- */
  const humanGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '真人感' }),
  );
  const proRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: 'AI 主动发消息' }),
      h('div', { class: 'row-sub', text: 'TA 会像真人一样偶尔主动找你（消耗少量额度）' }),
    ),
    h('label', { class: 'switch' },
      h('input', { type: 'checkbox' }),
      h('span', { class: 'track' }),
      h('span', { class: 'thumb' }),
    ),
  );
  const proCheck = proRow.querySelector('input');
  proCheck.checked = !!s.proactive;
  proCheck.addEventListener('change', async () => {
    const enabled = proCheck.checked;
    Store.saveSettings({ proactive: enabled });
    try { await savePcConfig({ IDLE_AGENT_ENABLED: enabled }, { throwOnError: true }); toast('主动消息已' + (enabled ? '开启' : '关闭')); }
    catch (_) { proCheck.checked = !enabled; Store.saveSettings({ proactive: !enabled }); toast('主动消息设置保存失败'); }
  });
  humanGroup.appendChild(proRow);

  const logRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '每日记忆日志' }),
      h('div', { class: 'row-sub', text: '每晚 23:00 自动生成当天对话的记忆（在 TA 的资料页查看）' }),
    ),
    h('label', { class: 'switch' },
      h('input', { type: 'checkbox' }),
      h('span', { class: 'track' }),
      h('span', { class: 'thumb' }),
    ),
  );
  const logCheck = logRow.querySelector('input');
  logCheck.checked = !!s.dailyLog;
  logCheck.addEventListener('change', () => Store.saveSettings({ dailyLog: logCheck.checked }));
  humanGroup.appendChild(logRow);

  const instRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '秒回模式' }),
      h('div', { class: 'row-sub', text: '关闭真人打字动画，回复立即整段显示（更快）' }),
    ),
    h('label', { class: 'switch' },
      h('input', { type: 'checkbox' }),
      h('span', { class: 'track' }),
      h('span', { class: 'thumb' }),
    ),
  );
  const instCheck = instRow.querySelector('input');
  instCheck.checked = !!s.replyInstant;
  instCheck.addEventListener('change', () => Store.saveSettings({ replyInstant: instCheck.checked }));
  humanGroup.appendChild(instRow);

  /* ---- 消息通知 ---- */
  const notifyGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '消息通知' }),
  );
  notifyGroup.appendChild(h('div', { class: 'help-box', text:
    'AI 主动发消息时，仅当应用处于前台且该会话未打开，才在右上角弹出轻量提示。\n' +
    '窗口最小化或你在用其他应用时不会打扰。\n' +
    '点击卡片打开会话，点 ✕ 或 5 秒后自动关闭。' }));
  const testRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '测试应用内通知' }),
    h('div', { class: 'row-arrow', text: '›' }),
  );
  testRow.addEventListener('click', () => {
    const cur = (typeof Chat !== 'undefined' && Chat.contact) ? Chat.contact : null;
    const mock = cur || { id: 'test', name: '助手', avatarUrl: '' };
    if (typeof inAppNotify === 'function') {
      inAppNotify(mock, '这是一条测试通知 — 看到说明弹窗正常工作。', true);
      Settings.close();
    } else {
      toast('通知模块未加载');
    }
  });
  notifyGroup.appendChild(testRow);

  /* ---- 图片识别（视觉模型） ---- */
  const visionGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '图片识别（视觉模型 Key）' }),
  );
  const hasServerVision = window.__serverVisionKey;
  if (hasServerVision) {
    visionGroup.appendChild(h('div', { class: 'help-box', text: '✓ 检测到电脑服务器已配置视觉模型 Key，发图片可直接使用。' }));
  } else {
    visionGroup.appendChild(h('div', { class: 'help-box', text:
      '发图片给 AI 需要「视觉模型 Key」（DeepSeek 本身看不了图）。\n' +
      '两种配置方式：\n' +
      '① 填在下面（保存在手机，随请求发送）\n' +
      '② 填到电脑 config.json 的 visionApiKey（保存在服务器）' }));
  }
  const vKeyRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '视觉模型 Key' }),
    h('input', { type: 'text', placeholder: '通义千问 DashScope 的 Key', value: s.visionKey }),
  );
  const vKeyInput = vKeyRow.querySelector('input');
  vKeyInput.addEventListener('change', async () => {
    const value = vKeyInput.value.trim();
    Store.saveSettings({ visionKey: value });
    try { await savePcConfig({ vision_api_key: value }, { throwOnError: true }); } catch (_) { toast('视觉 Key 保存失败'); }
  });
  visionGroup.appendChild(vKeyRow);

  const vModelSelect = h('select', {},
    h('option', { value: '', text: '加载中…' }),
  );
  const vModelRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '视觉模型' }),
    vModelSelect,
  );
  vModelSelect.addEventListener('change', async (e) => {
    const value = e.target.value;
    Store.saveSettings({ visionModel: value });
    try { await savePcConfig({ VISION_MODEL: value }, { throwOnError: true }); } catch (_) { toast('视觉模型保存失败'); }
  });
  visionGroup.appendChild(vModelRow);
  visionGroup.appendChild(h('div', { class: 'help-box', text:
    '图片由「通义千问 qwen-vl」理解。\n' +
    '获取：bailian.aliyun.com 注册 → 开通 DashScope → 创建 API-KEY（新用户有免费额度）。\n' +
    '填在手机上即可；图片功能需要代理模式。' }));

  /* ---- 语音合成（TTS Key） ---- */
  const ttsGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '语音合成（TTS Key）' }),
  );
  ttsGroup.appendChild(h('div', { class: 'help-box', text:
    '语音通话 / 语音回复使用云端 TTS 需要对应服务商的 Key（edge-tts 免费免 Key，可直接用）。\n' +
    '选择服务商 → 填入 Key → 保存；Key 存在电脑 backend/data/config.json。' }));

  const TTS_PROVIDERS = [
    { v: 'aliyun', label: '阿里云百炼（通话专用·付费）' },
    { v: 'cosyvoice', label: 'CosyVoice3（本地，无需 Key）' },
    { v: 'edge-tts', label: 'edge-tts（免费，无需 Key）' },
    { v: 'minimax', label: 'MiniMax（情感女声/男声）' },
    { v: 'volcengine', label: '火山引擎（字节跳动）' },
    { v: 'xunfei', label: '讯飞' },
    { v: 'azure', label: 'Azure（微软）' },
    { v: 'elevenlabs', label: 'ElevenLabs' },
  ];
  const TTS_FIELDS = {
    'aliyun': [
      ['dashscope_api_key', '阿里云百炼 API Key', '百炼控制台获取'],
    ],
    'cosyvoice': [],
    'edge-tts': [],
    'minimax': [
      ['minimax_api_key', 'MiniMax API Key', 'MiniMax 开放平台获取'],
      ['minimax_group_id', 'MiniMax Group ID', 'MiniMax 开放平台获取'],
    ],
    'volcengine': [
      ['volcengine_app_id', '火山 App ID', '火山引擎语音控制台'],
      ['volcengine_access_token', '火山 Access Token', '火山引擎语音控制台'],
      ['volcengine_cluster', '火山 Cluster（可留空）', '默认 volcano_tts'],
    ],
    'xunfei': [
      ['xunfei_app_id', '讯飞 App ID', '讯飞开放平台'],
      ['xunfei_api_key', '讯飞 API Key', '讯飞开放平台'],
      ['xunfei_api_secret', '讯飞 API Secret', '讯飞开放平台'],
    ],
    'azure': [
      ['azure_speech_key', 'Azure Speech Key', 'Azure 门户获取'],
      ['azure_speech_region', 'Azure Region', '如 eastasia'],
    ],
    'elevenlabs': [
      ['elevenlabs_api_key', 'ElevenLabs API Key', 'ElevenLabs 控制台'],
    ],
  };
  const FIELD_TO_SET = {
    minimax_api_key: 'minimax', minimax_group_id: 'minimax',
    volcengine_app_id: 'volcengine', volcengine_access_token: 'volcengine', volcengine_cluster: 'volcengine',
    xunfei_app_id: 'xunfei', xunfei_api_key: 'xunfei', xunfei_api_secret: 'xunfei',
    azure_speech_key: 'azure', azure_speech_region: 'azure',
    elevenlabs_api_key: 'elevenlabs',
    dashscope_api_key: 'aliyun',
  };

  const ttsProvRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: 'TTS 服务商' }),
    h('select', {}, ...TTS_PROVIDERS.map((p) => h('option', { value: p.v, text: p.label }))),
  );
  ttsGroup.appendChild(ttsProvRow);
  const ttsProvSel = ttsProvRow.querySelector('select');

  const ttsKeyRows = {};
  const ttsInputs = {};
  Object.keys(TTS_FIELDS).forEach((prov) => {
    TTS_FIELDS[prov].forEach(([field, label, ph]) => {
      if (ttsKeyRows[field]) return;
      const rowChildren = [
        h('div', { class: 'row-label', text: label }),
        h('input', { type: 'text', placeholder: ph, id: 'tts-input-' + field }),
      ];
      // ★ 阿里云 Key 输入框旁加「没有Key？点这里」跳转百炼官网
      if (field === 'dashscope_api_key') {
        rowChildren.push(h('a', {
          class: 'key-link', href: 'javascript:;',
          text: '没有Key？点这里',
          onclick: () => window.open('https://bailian.aliyun.com', '_blank'),
        }));
      }
      const row = h('div', { class: 'setting-row', id: 'tts-row-' + field }, ...rowChildren);
      ttsKeyRows[field] = row;
      const input = row.querySelector('input');
      ttsInputs[field] = input;
      input.addEventListener('change', () => {
        const v = input.value.trim();
        if (!v) return;   // 空值不提交，避免误清空；清空请用下方「清除」按钮
        savePcConfig({ [field]: v });
        toast(label + ' 已保存');
      });
      ttsGroup.appendChild(row);
    });
  });

  const ttsSet = { aliyun: false, 'cosyvoice': true, 'edge-tts': true, minimax: false, volcengine: false, xunfei: false, azure: false, elevenlabs: false };
  const ttsStatusBox = h('div', { class: 'help-box', style: 'margin-top:2px' });
  ttsGroup.appendChild(ttsStatusBox);

  function _ttsShowProvider(prov) {
    const fields = TTS_FIELDS[prov] || [];
    const shown = {};
    fields.forEach(([field]) => { shown[field] = true; });
    Object.keys(ttsKeyRows).forEach((field) => {
      ttsKeyRows[field].style.display = shown[field] ? '' : 'none';
    });
  }
  function _ttsRenderStatus() {
    const nameMap = [
      ['aliyun', '阿里云百炼'], ['cosyvoice', 'CosyVoice3'], ['edge-tts', 'edge-tts'], ['minimax', 'MiniMax'], ['volcengine', '火山'],
      ['xunfei', '讯飞'], ['azure', 'Azure'], ['elevenlabs', 'ElevenLabs'],
    ];
    ttsStatusBox.textContent = '配置状态：' + nameMap
      .map(([k, name]) => (ttsSet[k] ? '✓ ' : '○ ') + name)
      .join('　');
  }

  const ttsClearRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '清除当前服务商 Key' }),
    h('div', { class: 'row-arrow', text: '✕' }),
  );
  ttsClearRow.addEventListener('click', () => {
    const prov = ttsProvSel.value;
    const fields = TTS_FIELDS[prov] || [];
    if (!fields.length) { toast('当前服务商无需 Key'); return; }
    if (!confirm('确定清除该服务商已保存的 Key？')) return;
    const patch = {};
    fields.forEach(([field]) => {
      patch[field] = '';
      if (ttsInputs[field]) ttsInputs[field].value = '';
    });
    savePcConfig(patch);
    toast('已清除');
  });
  ttsGroup.appendChild(ttsClearRow);

  ttsProvSel.addEventListener('change', () => {
    savePcConfig({ tts_provider: ttsProvSel.value });
    _ttsShowProvider(ttsProvSel.value);
  });

  /* ---- 声音复刻（阿里云通话音色） ---- */
  const replicaGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '声音复刻（阿里云通话音色）' }),
  );
  const replicaBox = h('div', { class: 'help-box', style: 'margin-top:2px',
    text: '用助手的克隆音频在云端复刻一个专属音色，通话时就是"她"的声音。填好上面的阿里云 Key 后再点开始。' });
  replicaGroup.appendChild(replicaBox);
  const replicaRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '复刻助手音色' }),
    h('div', { class: 'row-arrow', text: '开始复刻 →' }),
  );
  replicaRow.addEventListener('click', () => { doReplica(); });
  replicaGroup.appendChild(replicaRow);

  async function doReplica() {
    replicaBox.textContent = '复刻中…（上传音频 + 生成音色，约需 10~60 秒，请稍候）';
    try {
      const optRes = await fetch('/api/voice-replica/options');
      const opt = await optRes.json().catch(() => ({}));
      if (!opt.key_set) {
        replicaBox.textContent = '⚠️ 还没填阿里云 Key。请在上方「语音合成」选「阿里云百炼」并填入 Key。';
        return;
      }
      const srcs = opt.sources || [];
      if (!srcs.length) {
        replicaBox.textContent = '⚠️ 没找到可复刻的参考音频（voice_clone_models 里没有克隆音色）。';
        return;
      }
      const createRes = await fetch('/api/voice-replica/create', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: 0 }),
      });
      const r = await createRes.json().catch(() => ({}));
      if (r && r.ok) {
        replicaBox.textContent = '✓ 复刻成功！voice_id = ' + r.voice_id + '（通话已自动使用该音色）';
        toast('声音复刻成功');
      } else {
        replicaBox.textContent = '✗ 复刻失败：' + ((r && r.error) || '未知错误');
      }
    } catch (e) {
      replicaBox.textContent = '✗ 复刻异常：' + (e.message || e);
    }
  }

  /* ---- 我的信息（结构化用户档案，存后端 sqlite） ---- */
  const meGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '我的信息' }),
  );
  meGroup.appendChild(h('div', { class: 'help-box', text: '让 AI 更了解你：生日、职业、城市、兴趣、话题偏好等，会注入对话让回复更贴心、更有话题。' }));

  // ★ 我的头像上传
  const avatarRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '我的头像' }),
  );
  const avatarWrap = h('div', { style: 'display:flex;align-items:center;gap:10px' });
  const avatarPreview = h('div', { style: 'width:48px;height:48px;border-radius:50%;overflow:hidden;background:#f0f0f0;display:flex;align-items:center;justify-content:center;flex:none' });
  const refreshMyAvatar = () => {
    avatarPreview.innerHTML = '';
    const av = Store.getSettings().myAvatar || '';
    if (av) avatarPreview.appendChild(h('img', { src: av, alt: '', style: 'width:100%;height:100%;object-fit:cover' }));
    else avatarPreview.appendChild(h('span', { text: '👤' }));
  };
  refreshMyAvatar();
  const avatarBtn = h('button', { class: 'btn btn-plain', text: '上传头像' });
  const avatarInput = h('input', { type: 'file', accept: 'image/*', hidden: true });
  avatarBtn.addEventListener('click', () => avatarInput.click());
  avatarInput.addEventListener('change', () => {
    const f = avatarInput.files[0];
    if (!f) return;
    fileToDataUrl(f, 128, (url) => {
      if (!url) { toast('图片读取失败'); return; }
      Store.saveSettings({ myAvatar: url });
      refreshMyAvatar();
      toast('头像已保存');
    });
    avatarInput.value = '';
  });
  avatarWrap.appendChild(avatarPreview);
  avatarWrap.appendChild(avatarBtn);
  avatarRow.appendChild(avatarWrap);
  meGroup.appendChild(avatarRow);

  const _mkField = (label, id, placeholder, type) => {
    const row = h('div', { class: 'setting-row' },
      h('div', { class: 'row-label', text: label }),
      h('input', { type: type || 'text', id, placeholder }),
    );
    return row;
  };
  meGroup.appendChild(_mkField('昵称', 'me-nickname', '你的昵称'));
  meGroup.appendChild(_mkField('生日', 'me-birthday', '如 2000-05-21'));
  const genderRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '性别' }),
    h('select', { id: 'me-gender' },
      h('option', { value: '', text: '不填' }),
      h('option', { value: 'male', text: '男生' }),
      h('option', { value: 'female', text: '女生' }),
      h('option', { value: 'other', text: '其他' }),
    ),
  );
  meGroup.appendChild(genderRow);
  meGroup.appendChild(_mkField('职业', 'me-occupation', '你的职业'));
  meGroup.appendChild(_mkField('城市', 'me-city', '所在城市'));
  meGroup.appendChild(_mkField('兴趣（顿号分隔）', 'me-hobbies', '如 打篮球、王阳明思想'));
  meGroup.appendChild(_mkField('话题偏好（顿号分隔）', 'me-topics', '如 科技、电影、美食'));
  const bioRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '简介' }),
    h('textarea', { id: 'me-bio', placeholder: '一句话介绍自己', style: 'flex:1;min-height:60px;padding:8px;border:1px solid #e0e0e0;border-radius:8px;font-size:13px;' }),
  );
  meGroup.appendChild(bioRow);
  const saveMeBtn = h('button', { class: 'btn btn-primary', text: '保存我的信息', style: 'margin:10px 14px' });
  meGroup.appendChild(saveMeBtn);

  // 异步加载
  (async () => {
    try {
      // ★ P1-8：补全 ai_companion_session_id，与其它模块的 fallback 链一致
      const sid = window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default';
      const res = await fetch('/api/user/profile?session_id=' + encodeURIComponent(sid) + '&character_id=default');
      if (!res.ok) throw new Error('profile load failed');
      const d = await res.json();
      const set = (id, v) => { const el = document.getElementById(id); if (el && v != null) el.value = v; };
      set('me-nickname', d.nickname);
      set('me-birthday', d.birthday);
      set('me-gender', d.gender);
      set('me-occupation', d.occupation);
      set('me-city', d.city);
      set('me-hobbies', (d.hobbies || []).join('、'));
      set('me-topics', (d.topic_preferences || []).join('、'));
      set('me-bio', d.bio);
    } catch (e) {}
  })();

  saveMeBtn.addEventListener('click', async () => {
    const split = (id) => document.getElementById(id).value.split(/[、,，\s]+/).map(s => s.trim()).filter(Boolean);
    const payload = {
      session_id: window.Session?.getSessionId?.() || localStorage.getItem('ai_companion_session_id') || localStorage.getItem('session_id') || 'default',
      character_id: 'default',
      nickname: document.getElementById('me-nickname').value.trim(),
      birthday: document.getElementById('me-birthday').value.trim(),
      gender: document.getElementById('me-gender').value,
      occupation: document.getElementById('me-occupation').value.trim(),
      city: document.getElementById('me-city').value.trim(),
      bio: document.getElementById('me-bio').value.trim(),
      hobbies: split('me-hobbies'),
      topic_preferences: split('me-topics'),
    };
    try {
      const res = await fetch('/api/user/profile', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data?.error?.message || '保存失败');
      // 立即回读校验，避免前端提示成功但实际写入错误桶。
      const verify = await fetch('/api/user/profile?session_id=' + encodeURIComponent(payload.session_id) + '&character_id=default');
      if (!verify.ok) throw new Error('保存后校验失败');
      toast('我的信息已保存 ✓');
      // 通知外层刷新问候语里的用户名字（flow7.js 监听）
      try { window.dispatchEvent(new CustomEvent('hz-me-saved')); } catch (_) {}
    } catch (e) { toast('保存失败'); }
  });

  /* ---- 用户核心档案（长期记忆） ---- */
  const profileGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '用户核心档案（长期记忆）' }),
  );
  const profileInput = h('textarea', { placeholder: '关于你的长期信息：我是谁、喜欢什么、讨厌什么、人生阶段、目标……所有 AI 伴侣都会记住', style: 'width:100%;min-height:120px;padding:12px 14px;border:1px solid #e0e0e0;border-radius:12px;font-size:14px;line-height:1.7;outline:none;resize:vertical;box-sizing:border-box;transition:border-color .2s' });
  profileInput.addEventListener('focus', () => { profileInput.style.borderColor = '#4a90d9'; });
  profileInput.addEventListener('blur', () => { profileInput.style.borderColor = '#e0e0e0'; });
  profileInput.value = s.userProfile || '';
  profileInput.addEventListener('change', () => {
    Store.saveSettings({ userProfile: profileInput.value.trim() });
    toast('已保存核心档案');
  });
  const profileRow = h('div', { style: 'padding:12px 14px' }, profileInput);
  profileGroup.appendChild(profileRow);
  profileGroup.appendChild(h('div', { class: 'help-box', text: '这是「核心人格记忆」：不会被对话冲掉，所有伴侣都会在聊天中参考。也可以在任何聊天里长按 AI 消息「让 TA 记住」。' }));

  /* ---- 数据管理 ---- */
  const dataGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '数据管理' }),
  );
  const btnRow = (label, fn) => {
    const row = h('div', { class: 'setting-row' },
      h('div', { class: 'row-label', text: label }),
      h('div', { class: 'row-arrow', text: '›' }),
    );
    row.addEventListener('click', fn);
    return row;
  };
  dataGroup.appendChild(btnRow('清空所有聊天记录', () => {
    if (confirm('确定清空所有聊天记录？伴侣保留。')) {
      Store.clearAllChats();
      renderChatList();
      toast('已清空');
    }
  }));
  dataGroup.appendChild(btnRow('重置全部数据', () => {
    if (confirm('确定重置？将删除所有联系人和聊天记录，恢复初始状态。')) {
      Store.resetAll();
      window.location.reload();
    }
  }));

  /* ---- 主动消息时间范围 ---- */
  const idleGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: 'AI 主动发言' }));
  // 总开关
  const idleSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }),
    h('span', { class: 'track' }),
    h('span', { class: 'thumb' }));
  idleSw.querySelector('input').checked = true;
  idleSw.querySelector('input').addEventListener('change', (e) => {
    savePcConfig({ IDLE_AGENT_ENABLED: e.target.checked });
  });
  idleGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '允许 AI 主动找你' }),
      h('div', { class: 'row-sub', text: '关闭后 AI 不会主动发消息' })),
    idleSw));
  // 间隔范围
  // ★ 2026-09-14 改为**自由输入的上下限**（用户要求）。
  //   原先只有 4 个固定档位（最长 60–120），想设 30–90 这种就做不到。
  //   现在两个分钟数字框，范围 1–1440 分；后端只认 IDLE_TRIGGER_MIN/MAX_MINUTES，
  //   并且这是**全站唯一**的主动消息节奏来源（人格设置里那个控件已删）。
  const intrLo = h('input', { type: 'number', min: '1', max: '1440', step: '1', value: '20',
    style: 'width:78px;padding:6px 8px;border:1px solid #ddd;border-radius:6px;font-size:14px;' });
  const intrHi = h('input', { type: 'number', min: '1', max: '1440', step: '1', value: '40',
    style: 'width:78px;padding:6px 8px;border:1px solid #ddd;border-radius:6px;font-size:14px;' });
  const intrHint = h('div', { class: 'row-sub', text: '最小 1，最大 1440（分钟）' });
  // ★ 用户口径：「上限是主设定，下限默认取上限的一半」（填 120 → 60–120）。
  //   下限框没被手动改过时，改上限会自动跟着折半；手动填过下限就尊重你的输入。
  let loAuto = true;
  const commitInterval = (src) => {
    let lo = Math.round(Number(intrLo.value) || 0);
    let hi = Math.round(Number(intrHi.value) || 0);
    hi = Math.min(1440, Math.max(1, hi || 1));
    if (src === 'lo' && lo > 0) loAuto = false;
    if (src === 'hi' && loAuto) lo = Math.round(hi / 2);
    lo = Math.min(1440, Math.max(1, lo || 1));
    if (hi < lo) { const t = lo; lo = hi; hi = t; }   // 自动纠正颠倒的输入
    intrLo.value = String(lo);
    intrHi.value = String(hi);
    intrHint.textContent = `约 ${lo}–${hi} 分钟一次（在此区间内随机）`;
    savePcConfig({ IDLE_TRIGGER_MIN_MINUTES: lo, IDLE_TRIGGER_MAX_MINUTES: hi });
  };
  intrLo.addEventListener('change', () => commitInterval('lo'));
  intrHi.addEventListener('change', () => commitInterval('hi'));
  idleGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '主动发言间隔' }),
      h('div', { class: 'row-sub', text: '闲置多久后 AI 会主动找你（在此区间内随机）；只改上限时下限自动折半' })),
    h('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' },
      intrLo, h('span', { text: '—' }), intrHi, h('span', { text: '分钟' }))));
  idleGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '说明' }),
      h('div', { class: 'row-sub', text: '这是全站唯一的主动消息节奏来源；可用时段在人格设置 → 主动与免打扰里配' })),
    intrHint));
  // ★ 2026-09-14 移除：「主动发言时段」全局控件。
  //   主动消息时段已统一到**角色卡**（人格设置 → 主动与免打扰 → 主动消息可用时段）。
  //   全局这份既与角色设置冲突，又是"时段限制不管用"的根源之一（两条链路各读一套）。
  //   现在只保留一个数据源；要按角色单独设请去人格设置页。
  const _idleTimeRemoved = true;

  const momentFreq = h('select', {},
    h('option', { value: '0', text: '关闭（0 条）' }),
    h('option', { value: '1', text: '克制（最多 1 条/天）' }),
    h('option', { value: '2', text: '自然（最多 2 条/天）' }),
    h('option', { value: '3', text: '活跃（最多 3 条/天）' }));
  momentFreq.value = '2';
  momentFreq.addEventListener('change', () => savePcConfig({ MOMENT_FREQUENCY: Number(momentFreq.value) }));
  idleGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: 'AI 朋友圈频率' }),
      h('div', { class: 'row-sub', text: '只是每日上限；没有值得分享的内容仍然不会发' })),
    momentFreq));

  // 关系升温速度（快热/慢热调节）
  const PACE_OPTS = [
    { value: 'slow', label: '慢热（慢慢升温，有成就感）' },
    { value: 'normal', label: '正常' },
    { value: 'fast', label: '快热（关系升温更快）' },
  ];
  const paceSel = h('select', {},
    ...PACE_OPTS.map((o) => h('option', { value: o.value, text: o.label })));
  paceSel.value = 'normal';
  paceSel.addEventListener('change', () => {
    savePcConfig({ RELATIONSHIP_PACE: paceSel.value });
    toast('关系升温速度已设置');
  });
  idleGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '关系升温速度' }),
      h('div', { class: 'row-sub', text: '决定亲密度每天涨多快（慢热更有成就感）' })),
    paceSel));

  /* ---- 早安 / 晚安 / 惊喜 ---- */
  const morningGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '早安 / 晚安' }));
  // 早安开关（与现有开关同款：track/thumb）
  const morningSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }),
    h('span', { class: 'track' }),
    h('span', { class: 'thumb' }));
  morningSw.querySelector('input').checked = true;
  morningSw.querySelector('input').addEventListener('change', (e) => {
    savePcConfig({ MORNING_ENABLED: e.target.checked });
  });
  morningGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '早安推送' }),
      h('div', { class: 'row-sub', text: '每天早上主动发早安' })),
    morningSw));
  const morningStart = h('input', { type: 'time', value: '07:00' });
  const morningEnd   = h('input', { type: 'time', value: '09:30' });
  const saveMorning  = () => {
    savePcConfig({ MORNING_START: morningStart.value, MORNING_END: morningEnd.value });
  };
  morningStart.addEventListener('change', saveMorning);
  morningEnd.addEventListener('change', saveMorning);
  morningGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '早安时间窗口' }),
      h('div', { class: 'row-sub', text: '在这个时间段内随机发送早安' })),
    h('div', { style: 'display:flex;gap:6px;align-items:center' },
      morningStart, h('span', { text: '—' }), morningEnd)));
  // 晚安开关
  const nightSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }),
    h('span', { class: 'track' }),
    h('span', { class: 'thumb' }));
  nightSw.querySelector('input').checked = true;
  nightSw.querySelector('input').addEventListener('change', (e) => {
    savePcConfig({ NIGHT_ENABLED: e.target.checked });
  });
  morningGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '晚安推送' }),
      h('div', { class: 'row-sub', text: '每天晚上主动发晚安' })),
    nightSw));
  const nightStart = h('input', { type: 'time', value: '22:00' });
  const nightEnd   = h('input', { type: 'time', value: '23:30' });
  const saveNight  = () => {
    savePcConfig({ NIGHT_START: nightStart.value, NIGHT_END: nightEnd.value });
  };
  nightStart.addEventListener('change', saveNight);
  nightEnd.addEventListener('change', saveNight);
  morningGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '晚安时间窗口' }),
      h('div', { class: 'row-sub', text: '在这个时间段内随机发送晚安' })),
    h('div', { style: 'display:flex;gap:6px;align-items:center' },
      nightStart, h('span', { text: '—' }), nightEnd)));
  // 随机惊喜开关
  const surpriseSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }),
    h('span', { class: 'track' }),
    h('span', { class: 'thumb' }));
  surpriseSw.querySelector('input').checked = true;
  surpriseSw.querySelector('input').addEventListener('change', (e) => {
    savePcConfig({ SURPRISE_ENABLED: e.target.checked });
  });
  morningGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '随机惊喜' }),
      h('div', { class: 'row-sub', text: '偶尔主动发送一条暖心小惊喜' })),
    surpriseSw));

  /* ---- 免打扰 ---- */
  const dndGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '免打扰' }));
  const dndSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }),
    h('span', { class: 'track' }),
    h('span', { class: 'thumb' }));
  dndSw.querySelector('input').checked = true;
  dndSw.querySelector('input').addEventListener('change', (e) => {
    savePcConfig({ DND_ENABLED: e.target.checked });
    _toggleDndTimeRow(e.target.checked);
  });
  dndGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '免打扰' }),
      h('div', { class: 'row-sub', text: '开启后在指定时段内不主动发消息' })),
    dndSw));
  const dndStart = h('input', { type: 'time', value: '23:00' });
  const dndEnd   = h('input', { type: 'time', value: '07:00' });
  const saveDnd  = () => {
    savePcConfig({ DND_START: dndStart.value, DND_END: dndEnd.value });
  };
  dndStart.addEventListener('change', saveDnd);
  dndEnd.addEventListener('change', saveDnd);
  const dndTimeRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' },
      h('div', { text: '免打扰时段' }),
      h('div', { class: 'row-sub', text: '支持跨天，例如 23:00 — 07:00' })),
    h('div', { style: 'display:flex;gap:6px;align-items:center' },
      dndStart, h('span', { text: '—' }), dndEnd));
  const _toggleDndTimeRow = (show) => {
    dndTimeRow.style.display = show ? '' : 'none';
  };
  dndGroup.appendChild(dndTimeRow);
  _toggleDndTimeRow(true);   // 默认启用 23:00-07:00

  const dndReminderSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }), h('span', { class: 'track' }), h('span', { class: 'thumb' }));
  dndReminderSw.querySelector('input').checked = true;
  dndReminderSw.querySelector('input').addEventListener('change', (e) => savePcConfig({ DND_ALLOW_REMINDERS: e.target.checked }));
  dndGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' }, h('div', { text: '定时提醒可送达' }), h('div', { class: 'row-sub', text: '按时出现，但免打扰内不响铃、不播语音' })), dndReminderSw));

  const dndGreetingSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }), h('span', { class: 'track' }), h('span', { class: 'thumb' }));
  dndGreetingSw.querySelector('input').checked = true;
  dndGreetingSw.querySelector('input').addEventListener('change', (e) => savePcConfig({ DND_ALLOW_GREETINGS: e.target.checked }));
  dndGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' }, h('div', { text: '早晚安可送达' }), h('div', { class: 'row-sub', text: '免打扰内静默收到，不弹通知' })), dndGreetingSw));

  const dndCallSw = h('label', { class: 'switch' },
    h('input', { type: 'checkbox' }), h('span', { class: 'track' }), h('span', { class: 'thumb' }));
  dndCallSw.querySelector('input').checked = false;
  dndCallSw.querySelector('input').addEventListener('change', (e) => savePcConfig({ DND_ALLOW_CALLS: e.target.checked }));
  dndGroup.appendChild(h('div', { class: 'setting-row' },
    h('div', { class: 'row-label' }, h('div', { text: '允许 AI 来电' }), h('div', { class: 'row-sub', text: '默认关闭；你主动要求“打给我”不受限制' })), dndCallSw));


  // 异步加载当前配置
  (async () => {
    try {
      const res = await fetch('/api/pc/config');
      const cfg = await res.json();

      // ★ 模型设置已迁至「人格设置」（角色卡），全局页不再填充 AI 大脑/理解层下拉（2026-09-11）。

      // ★ 视觉模型：动态填充（含 glm-5.3-flash 等新模型；修复下拉之前硬编码只有 3 个通义千问的问题）
      if (cfg.visionModels && vModelSelect) {
        const vModels = cfg.visionModels || {};
        const vKeys = Object.keys(vModels);
        if (vKeys.length) {
          vModelSelect.innerHTML = '';
          vKeys.forEach((key) => {
            const info = vModels[key] || {};
            const opt = document.createElement('option');
            opt.value = key;
            opt.textContent = info.name || key;
            vModelSelect.appendChild(opt);
          });
          const savedVm = s.visionModel || cfg.VISION_MODEL || '';
          if (savedVm && vKeys.indexOf(savedVm) !== -1) vModelSelect.value = savedVm;
        }
      }

      if (cfg.IDLE_AGENT_ENABLED === false) idleSw.querySelector('input').checked = false;
      if (cfg.IDLE_TRIGGER_MIN_MINUTES && cfg.IDLE_TRIGGER_MAX_MINUTES) {
        // ★ 2026-09-14：改为自由输入框回填（不再有 rangeSel 档位选择器）
        intrLo.value = String(cfg.IDLE_TRIGGER_MIN_MINUTES);
        intrHi.value = String(cfg.IDLE_TRIGGER_MAX_MINUTES);
        // 已存的下限正好是上限的一半 → 仍视为"跟随上限"，之后再改上限会继续折半
        loAuto = Math.round(cfg.IDLE_TRIGGER_MAX_MINUTES / 2) === Number(cfg.IDLE_TRIGGER_MIN_MINUTES);
        intrHint.textContent = `约 ${cfg.IDLE_TRIGGER_MIN_MINUTES}–${cfg.IDLE_TRIGGER_MAX_MINUTES} 分钟一次（在此区间内随机）`;
      }
      // ★ 2026-09-14：主动发言时段已移到人格设置（角色卡 active_hours），
      //   这里不再回填全局控件（控件本身也已移除）。
      if (typeof cfg.MOMENT_FREQUENCY !== 'undefined') momentFreq.value = String(cfg.MOMENT_FREQUENCY);
      // ★ 关系升温速度 加载
      if (cfg.RELATIONSHIP_PACE) paceSel.value = cfg.RELATIONSHIP_PACE;
      // ★ 早安/晚安/惊喜 加载
      if (cfg.MORNING_ENABLED === false) morningSw.querySelector('input').checked = false;
      if (cfg.MORNING_START) morningStart.value = cfg.MORNING_START;
      if (cfg.MORNING_END)   morningEnd.value   = cfg.MORNING_END;
      if (cfg.NIGHT_ENABLED === false) nightSw.querySelector('input').checked = false;
      if (cfg.NIGHT_START) nightStart.value = cfg.NIGHT_START;
      if (cfg.NIGHT_END)   nightEnd.value   = cfg.NIGHT_END;
      if (cfg.SURPRISE_ENABLED === false) surpriseSw.querySelector('input').checked = false;
      // ★ 免打扰 加载（开关 + 时段显隐）
      dndSw.querySelector('input').checked = cfg.DND_ENABLED !== false;
      _toggleDndTimeRow(cfg.DND_ENABLED !== false);
      if (cfg.DND_START) dndStart.value = cfg.DND_START;
      if (cfg.DND_END)   dndEnd.value   = cfg.DND_END;
      dndReminderSw.querySelector('input').checked = cfg.DND_ALLOW_REMINDERS !== false;
      dndGreetingSw.querySelector('input').checked = cfg.DND_ALLOW_GREETINGS !== false;
      dndCallSw.querySelector('input').checked = !!cfg.DND_ALLOW_CALLS;
      // ★ TTS 语音合成 加载（provider + 已配置状态 + 行显隐）
      if (cfg.tts_provider) {
        const pv = String(cfg.tts_provider).toLowerCase();
        if (TTS_PROVIDERS.some((p) => p.v === pv)) ttsProvSel.value = pv;
      }
      ttsSet.minimax = !!cfg.tts_minimax_set;
      ttsSet.volcengine = !!cfg.tts_volcengine_set;
      ttsSet.xunfei = !!cfg.tts_xunfei_set;
      ttsSet.azure = !!cfg.tts_azure_set;
      ttsSet.elevenlabs = !!cfg.tts_elevenlabs_set;
      ttsSet.aliyun = !!cfg.tts_aliyun_set;
      Object.keys(ttsInputs).forEach((field) => {
        const prov = FIELD_TO_SET[field];
        if (prov && ttsSet[prov]) ttsInputs[field].placeholder = '✓ 已配置（输入新值可替换）';
      });
      _ttsShowProvider(ttsProvSel.value);
      _ttsRenderStatus();
    } catch (_) {}
  })();


  /* ---- QQ 机器人（OneBot） ---- */
  const qqGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: 'QQ 机器人（OneBot）' }),
  );
  qqGroup.appendChild(h('div', { class: 'help-box', text:
    '把 QQ 机器人绑定到某个角色，QQ 聊天就复用该角色的人格 + 大脑模型（含人格设置里单独配的模型）。\n' +
    '留空 = 默认角色，跟随全局模型。保存后重启后端生效。' }));
  const qqCharRow = h('div', { class: 'setting-row' },
    h('div', { class: 'row-label', text: '绑定角色' }),
    h('select', {},
      h('option', { value: '', text: '默认角色（跟随全局模型）' }),
    ),
  );
  const qqSel = qqCharRow.querySelector('select');
  const _curQQ = ((window.__pcConfig && window.__pcConfig.QQ_CHARACTER) || '').trim();
  fetch('/api/pc/character/list').then((r) => r.json()).then((d) => {
    const chars = (d && d.characters) || [];
    qqSel.innerHTML = '';
    qqSel.appendChild(h('option', { value: '', text: '默认角色（跟随全局模型）' }));
    for (const ch of chars) {
      const o = h('option', { value: ch.name, text: ch.name + (ch.relationship ? '（' + ch.relationship + '）' : '') });
      if (ch.name === _curQQ) o.selected = true;
      qqSel.appendChild(o);
    }
    if (_curQQ && !chars.some((c) => c.name === _curQQ)) {
      qqSel.appendChild(h('option', { value: _curQQ, text: _curQQ + '（已绑定）' }));
      qqSel.value = _curQQ;
    }
  }).catch(() => {});
  qqSel.addEventListener('change', async () => {
    await savePcConfig({ QQ_CHARACTER: qqSel.value });
    toast('QQ 绑定角色已保存（重启后端生效）');
  });
  qqGroup.appendChild(qqCharRow);

  /* ---- 帮助 ---- */
  const helpGroup = h('div', { class: 'list-group' },
    h('div', { class: 'group-title', text: '帮助' }),
  );
  helpGroup.appendChild(h('div', { class: 'help-box', style: 'margin-top:2px', text:
    '【手机使用】\n' +
    '1. 手机和电脑连同一个 Wi-Fi\n' +
    '2. 电脑上运行：node server.js\n' +
    '3. Safari 打开终端显示的 http://电脑IP:3000\n' +
    '4. 点「分享」→「添加到主屏幕」，就像 App 一样\n\n' +
    '【不想开电脑？】\n' +
    '在「连接方式」切到「直连模式」并填 Key，再把页面部署到免费静态网站即可；图片功能仍需要代理。\n\n' +
    '【小技巧】\n' +
    '· 长按消息：自己的可撤回，AI 的可以复制或「让 TA 记住」\n' +
    '· 编辑联系人可以换照片头像、选说话风格、填记忆\n' +
    '· AI 偶尔会主动发消息，不想要可在「真人感」里关掉' }));

  /* 2026-09-13 移除「本地大脑与训练（Ollama）」整组：模型切换 / 思考开关 /
     回退模型 / 一键训练闭环全部撤掉，聊天统一走云端。
     原因见 public/js/profile.js 顶部注释与 git 快照 e114b61。 */


  wrap.appendChild(modeWrap);
  wrap.appendChild(keyGroup);
  wrap.appendChild(glmKeyGroup);
  wrap.appendChild(humanGroup);
  wrap.appendChild(notifyGroup);
  wrap.appendChild(visionGroup);
  wrap.appendChild(ttsGroup);
  wrap.appendChild(replicaGroup);
  wrap.appendChild(meGroup);
  wrap.appendChild(profileGroup);
  wrap.appendChild(dataGroup);
  wrap.appendChild(idleGroup);
  wrap.appendChild(morningGroup);   // 早安/晚安组
  wrap.appendChild(dndGroup);       // 免打扰组
  wrap.appendChild(qqGroup);        // QQ 机器人组
  wrap.appendChild(helpGroup);


}

$('#settings-back').addEventListener('click', () => Settings.close());
$('#settings-done').addEventListener('click', () => Settings.close());
