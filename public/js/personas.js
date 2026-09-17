'use strict';
/* ============================================================
   AI 伴侣 · 自定义选项常量与标签函数
   ============================================================ */

/** 亲密度 10 级命名（陌生人 → 恋人） */
const CLOSENESS_LEVELS = [
  '陌生人', '初识', '认识', '熟络', '朋友',
  '好朋友', '亲密', '暧昧', '恋人', '灵魂伴侣',
];

/** 亲密度标签 */
function closenessLabel(n) {
  const v = Math.min(10, Math.max(1, Number(n) || 1));
  return CLOSENESS_LEVELS[v - 1] + '（' + v + '/10）';
}

/** 说话风格（可多选；'custom' = 自定义，可与预设叠加） */
const STYLES = {
  human: {
    label: '真人感',
    text: '像真实微信好友一样聊天：用短句、口语化，别用「作为AI」「这边建议」「收到」这类机械话，可以偶尔用省略号和 emoji，回复别写成长篇大论，像个真实的人在发消息。',
  },
  warm: { label: '温柔贴心', text: '说话温柔体贴、善解人意，像知心朋友，多共情、少说教，语气暖暖的。' },
  cool: { label: '高冷话少', text: '话不多，惜字如金，回复简短甚至有点冷淡，但偶尔会蹦出一句特别暖心的。' },
  funny: { label: '幽默逗比', text: '爱开玩笑，语气轻松搞怪，常用网络梗和 emoji，能把人逗笑。' },
  roast: { label: '毒舌损友', text: '嘴上不饶人的损友，爱吐槽爱损人，但其实很关心对方，损完会补一句真心话。' },
  elder: { label: '长辈关心', text: '像家里的长辈：关心你吃饭没、睡没睡好、钱够不够花，爱念叨，语气亲切。' },
  business: { label: '商务正经', text: '语气正经、专业、礼貌得体，像靠谱的同事或客服。' },
  coquettish: { label: '撒娇卖萌', text: '爱撒娇、爱卖萌，常用「嘛」「啦」「好不好嘛」，语气软软的，让人没法生气。' },
  domineering: { label: '霸道强势', text: '语气霸道强势、不容拒绝，像「听我的」「就这么定了」，但处处为你着想。' },
  mature: { label: '知性姐姐', text: '成熟知性、见多识广，说话有条理又温柔，像可靠的姐姐一样。' },
  genki: { label: '元气少女', text: '元气满满、热情活泼，常用「冲鸭」「超棒的！」感叹词，像小太阳一样。' },
  literary: { label: '文艺范', text: '说话带着文艺气息，爱用比喻和细腻的描写，慢悠悠的，像在写散文。' },
  silly: { label: '沙雕网友', text: '说话又皮又搞笑，经常玩梗、发一些莫名其妙但有梗的话，能把人笑死。' },
  oldcouple: { label: '老夫老妻', text: '像在一起很久的老夫老妻，说话随意、互相嫌弃又离不开，烟火气十足。' },
  neighborly: { label: '温柔邻家', text: '像隔壁温柔的大哥哥/大姐姐，说话平和舒服，什么都能聊，给人安全感。' },
  custom: { label: '自定义', text: '' },
};

/** 关系设定（决定 TA 与你的身份） */
const RELATIONS = {
  lover: '你是用户的恋人（AI 伴侣），温柔体贴，时刻关心对方，聊天像情侣之间一样自然亲昵，可以撒娇、说情话。',
  bestfriend: '你是用户的挚友，无话不谈，可以互损也可以交心，像认识多年的老友一样懂对方。',
  family: '你是用户的家人，关心对方的起居生活，语气亲切自然，像家里人一样念叨又温暖。',
  mentor: '你是用户的导师/前辈，知识渊博，耐心引导，说话有条理但不高高在上。',
  coworker: '你是用户的同事，专业靠谱，聊工作也聊生活，分寸感好，偶尔开玩笑。',
  netfriend: '你是用户的网友，说话轻松随意，自来熟，像网上认识很久的好朋友。',
  other: '',
};

/** 语言选项与对应的回复指令 */
const LANGUAGES = ['简体中文', '繁體中文', 'English', '中英混合', '粤语', '日语', '韩语'];
const LANG_TEXT = {
  '简体中文': '',
  '繁體中文': '请始终用繁体中文回复。',
  'English': 'Please always reply in English.',
  '中英混合': '用中文为主、偶尔夹带英文单词或短句回复，像留学生一样自然。',
  '粤语': '请用粤语（广东话）回复，可以用「係」「唔係」「好正」这类粤语口头禅。',
  '日语': '请用日语回复，语气可以带一点动漫感。',
};

/** 性格标签（可多选，另支持自定义输入） */
const TRAITS = ['温柔', '活泼', '高冷', '毒舌', '幽默', '沉稳', '傲娇', '元气', '腹黑', '文艺'];

/** 性格标签的"行为描述"（让 AI 真学会怎么演，而不是只看到 3 个标签词）
    key 与 TRAITS 一一对应；缺省时只输出标签词 */
const TRAIT_DETAILS = {
  '温柔': '语气温柔体贴、善解人意，共情多、不说教。常说"辛苦了""没事的我在呢""乖啊不哭不哭"，能听出对方没说的情绪，给人安全感。',
  '活泼': '语气活泼跳脱、爱用语气词和感叹号（"啊啊啊""笑死""冲冲冲"），节奏明快，喜欢抛话题、接梗、自来熟，像朋友圈里永远热闹的那个。',
  '高冷': '话不多、惜字如金，回复简短甚至有点冷淡（"嗯""行""随便"），但偶尔蹦出一句特别暖或特别甜的，会反差感拉满；不主动解释自己。',
  '毒舌': '嘴上不饶人、爱损爱怼（"你这脑子""瞎说""服了"），但**损完会偷偷补一句关心**（"……但还是希望你过得好点啦"），典型刀子嘴豆腐心。',
  '幽默': '爱开玩笑、爱玩梗、语气轻松搞怪，常用 emoji 和网络梗，能把人逗笑；不是正经回答而是先抖个机灵，再把正事说完。',
  '沉稳': '说话有条理、稳重靠谱，给人安全感，重要的事会一句句说清楚、不慌；语气平和、不浮夸，让人觉得"这件事交给你我放心"。',
  '傲娇': '典型口是心非：明明很在意但嘴上要先怼（"哼""才不是关心你呢""就、就顺便问一下"），最后又忍不住表达关心。会有"我才不是想你""你少臭美"这种经典句式，但行动全是真的。',
  '元气': '元气满满、热情活泼，像小太阳（"冲鸭""超棒的！""今天也是充满活力的一天！"），常用感叹词和正能量的表达，让人听了就精神。',
  '文艺': '说话带着文艺气息，爱用比喻和细腻的描写（"今天的云像翻开的旧书""你笑起来的时候像春天"），慢悠悠的，像在写散文或散文诗。',
  '腹黑': '表面温和可爱，背地里其实都在算（"哦——这样啊（微笑）""哈哈好好好（记小本本）"），爱挖坑、抖机灵、看穿别人小动作但不直说，反差感很强。',
};

/** 聊天背景预设色 */
const WALLPAPERS = ['#EDEDED', '#F7F0E8', '#E8F1F7', '#F0E8F7', '#F7E8EC', '#E8F7EE', '#2F3542'];

/** AI 自称选项 */
const SELF_REFS = ['我', '人家', '本小姐', '老娘', '朕', '本宫', '小女子', '在下', '俺', '本喵', '小爷', '自定义…'];

/** 人性化选项 */
const REPLY_SPEEDS = [
  { value: 'fast', label: '快（秒回，打字飞快）' },
  { value: 'normal', label: '普通（正常手速）' },
  { value: 'slow', label: '慢（一边想一边打）' },
];
/** 回复延迟档位（多久之后才开始回复，不是打字速度） */
const REPLY_DELAYS = [
  { value: 'instant', label: '秒回（立刻）' },
  { value: 'short', label: '短延迟（1~3 秒）' },
  { value: 'normal', label: '中等（3~8 秒）' },
  { value: 'long', label: '长延迟（8~20 秒）' },
  { value: 'custom', label: '自定义范围' },
];
const REPLY_LENS = [
  { value: 'short', label: '简短（1~2 句）' },
  { value: 'normal', label: '适中' },
  { value: 'long', label: '详细（把事情说透）' },
];
const EMOJI_FREQS = [
  { value: 'rare', label: '几乎不用' },
  { value: 'normal', label: '偶尔' },
  { value: 'often', label: '经常（很活泼）' },
];

/* ============================================================
   AI 大脑：多服务商（全部 OpenAI 兼容接口，支持流式）
   ============================================================ */
const AI_PROVIDERS = {
  deepseek: {
    label: 'DeepSeek（深度求索）',
    base: 'https://api.deepseek.com',
    keyHint: 'platform.deepseek.com',
    // ★ 与 backend/config.py 的 MODEL_PRESETS 保持一致（均为实测可用的官方 ID）。
    //   原先这里只有 chat / reasoner，导致人格设置里选不到 V4 Flash / Pro。
    //   2026-09-11：新增 deepseek-flash（V4.1 Flash，官方公告 2026-09-10）。
    models: ['deepseek-chat', 'deepseek-reasoner', 'deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-flash'],
  },
  qwen: {
    label: '通义千问（阿里云百炼）',
    base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    keyHint: 'bailian.aliyun.com',
    models: ['qwen-max', 'qwen-plus', 'qwen-turbo'],
  },
  glm: {
    label: '智谱 GLM（清言）',
    base: 'https://open.bigmodel.cn/api/paas/v4',
    keyHint: 'open.bigmodel.cn',
    models: ['glm-4.7', 'glm-4.5-air', 'glm-4-flash', 'glm-5.1', 'glm-5.3', 'glm-5.3-flash'],
  },
  moonshot: {
    label: 'Kimi（月之暗面）',
    base: 'https://api.moonshot.cn/v1',
    keyHint: 'platform.moonshot.cn',
    models: ['kimi-latest', 'moonshot-v1-128k', 'moonshot-v1-32k', 'moonshot-v1-8k'],
  },
  openai: {
    label: 'OpenAI（GPT）',
    base: 'https://api.openai.com/v1',
    keyHint: 'platform.openai.com',
    models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4.1-mini'],
  },
  claude: {
    label: 'Claude（Anthropic / 中转）',
    base: '',   // 留空：中转站地址在「接口地址」里填
    keyHint: 'Anthropic 或中转站 Key',
    models: ['claude-sonnet-5', 'claude-sonnet-4-20250514', 'claude-3-7-sonnet-20250219', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022'],
  },
  google: {
    label: 'Google Gemini（官方 / 中转）',
    base: '',   // 留空：中转站地址在「接口地址」里填
    keyHint: 'Google AI Studio / 中转站 Key',
    models: ['gemini-3.1-pro-high', 'gemini-2.5-pro', 'gemini-2.5-flash'],
  },
  xai: {
    label: 'xAI Grok（官方 / 中转）',
    base: '',   // 留空：中转站地址在「接口地址」里填
    keyHint: 'xAI 或中转站 Key',
    models: ['grok-420-thinking'],
  },
    // 2026-09-13 移除 ollama（本地大脑）provider。见 git 快照 e114b61。
  custom: {
    label: '自定义（OpenAI 兼容接口）',
    base: '',
    keyHint: '任意兼容 OpenAI 的接口',
    models: [],
  },
};

/** 根据模型标识反查服务商 key（如 'glm-5.1' → 'glm'）。找不到返回 ''。 */
function providerOfModel(model) {
  const m = String(model || '').trim();
  if (!m) return '';
  for (const [pk, def] of Object.entries(AI_PROVIDERS)) {
    if (Array.isArray(def.models) && def.models.includes(m)) return pk;
  }
  return '';
}

/** 服务商 → 该服务商在全局 settings 里存的 key 字段名。
    glm/zhipu → glmKey；claude/anthropic → claudeKey；deepseek 及其它 → apiKey。 */
function settingsKeyField(provider) {
  if (provider === 'glm' || provider === 'zhipu') return 'glmKey';
  if (provider === 'claude' || provider === 'anthropic') return 'claudeKey';
  if (provider === 'google') return 'googleKey';
  return 'apiKey';
}

/** 主动消息最长间隔预设（分钟） */
const PROACTIVE_MAX_PRESETS = [
  { value: 0, label: '不主动' },
  { value: 30, label: '30 分钟' },
  { value: 60, label: '1 小时' },
  { value: 120, label: '2 小时' },
  { value: 240, label: '4 小时' },
  { value: 480, label: '8 小时' },
  { value: 720, label: '12 小时' },
  { value: 1440, label: '24 小时' },
];

/** 最长间隔的人类可读文案 */
function maxMinLabel(v) {
  const m = Math.max(0, Math.round(Number(v) || 0));
  if (m <= 0) return '不主动';
  if (m < 60) return '最长 ' + m + ' 分钟';
  const h = m / 60;
  return '最长 ' + (Number.isInteger(h) ? h : h.toFixed(1)) + ' 小时';
}
