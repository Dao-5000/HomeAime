# -*- coding: utf-8 -*-
"""
语义理解层 —— 「两次 DeepSeek」方案的第一层。

职责：把用户的自然语言先解析成结构化意图 JSON，交给第二层（现有的回复生成）
当作「已知事实」直接消费，避免生成层自己再猜一遍导致的理解偏差。

设计约束（严格遵守项目既有习惯，不破坏任何现有功能）：
  1. 纯新增能力，不修改任何现有函数的行为；
  2. 任何异常一律静默降级 —— 解析不出来就等于不注入，主流程照常跑；
  3. 结果除注入 prompt 外，还写一份到 kv，供其它模块读取（与已有功能联通）；
  4. 短路规则只覆盖「几乎不可能误判」的极短消息，宁可漏判也不误判
     （项目里 parse_identity_command 那类正则误判的教训）。

对外接口：
    understand(...)            -> dict   解析意图；无需解析/失败时返回 {}
    build_intent_block(intent) -> str    生成注入第二层的权威块
    analyze_and_inject(...)    -> list   一步到位：解析 + 注入（main.py 用这个）
"""
import hashlib
import json
import re
import time

# ---------------- 基础配置 ----------------

INTENTS = (
    "inform",           # 告知
    "command",          # 指令
    "question",         # 提问
    "emotion",          # 情绪
    "greeting",         # 寒暄
    "topic_start",      # 开启话题
    "topic_continue",   # 继续话题
    "feedback",         # 反馈
    "ambiguous",        # 模糊
)

# 短路：只有「空 / 纯符号 / 纯语气词」才短路。
# 刻意不含「好 / 是 / 对 / 行」——这些有真实语义（答应、认同），
# 交给模型判断，否则会重蹈正则误判的覆辙。
# 注意：语气词分支不带尾部标点，所以「嗯？」(疑问)、「嗯！」(认同) 不会被短路，
# 会交给模型判断 —— 这正是易混淆对比第 5 组要求区分的场景。
_SHORT_CIRCUIT = re.compile(
    r"^[\s\W_]*$"
    r"|"
    r"^[嗯哦噢喔啊呃咦唉哎诶欸唔昂哼嗨哈嘿]{1,4}$"
)

# 结构化块标记，便于排查与后续模块识别
BLOCK_TITLE = "【用户意图解析（权威结果）】"

_cache: dict = {}
_CACHE_TTL = 300
_CACHE_MAX = 256

_json_re = re.compile(r"\{[\s\S]*\}")

# 超过这个长度的"用户消息"视为内部任务载荷，不做意图解析（理由见 understand 内注释）
_MAX_TEXT_LEN = 500


# ---------------- 开关 ----------------

def _enabled() -> bool:
    """总开关。默认开启；关闭方式：
      1) 环境变量 AI_UNDERSTANDING=0
      2) 设置页「理解层」开关（前端 profile.js 提交 UNDERSTANDING_ENABLED）
    任何读取异常都当作开启。

    ★ 2026-09-14 修：原来这里读的是**小写** key `config.get("understanding_enabled")`，
      而 DEFAULTS 里只有大写 `UNDERSTANDING_ENABLED`（config.py:98），且 `_load()` 只保留
      DEFAULTS 内存在的键（config.py:652）→ 小写查询恒为 None → 本函数恒返回 True。
      结果是：用户在设置页关掉「理解层」后，意图分析仍然每轮多调一次 LLM，开关完全失效。
      现在改走 config.understanding_enabled()（读大写键），与 chat_logic.py:419 的
      CompanionOS 门禁口径一致。"""
    import os
    try:
        v = os.environ.get("AI_UNDERSTANDING", "").strip()
        if v in ("0", "false", "off", "no"):
            return False
        from . import config
        return bool(config.understanding_enabled())
    except Exception:
        return True


# ---------------- 缓存 ----------------

def _cache_key(session_id: str, character_id: str, text: str) -> str:
    h = hashlib.md5(str(text or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"{session_id}|{character_id}|{h}"


def _cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return val


def _cache_put(key: str, val: dict):
    if len(_cache) > _CACHE_MAX:
        try:
            for k in list(_cache.keys())[:_CACHE_MAX // 4]:
                _cache.pop(k, None)
        except Exception:
            _cache.clear()
    _cache[key] = (time.time(), val)


# ---------------- Prompt ----------------

_INTENT_TABLE = """| 分类 | 含义 | 典型表现 | AI正确做法 | 常见误解（需避免） |
|---|---|---|---|---|
| inform 告知 | 陈述事实、分享信息、告知状态 | "我今天吃了火锅""你现在能唱歌了""我下班了" | 回应情绪/接话题，不执行动作 | 误解为要求执行动作 |
| command 指令 | 明确要求AI做某事 | "唱首歌""讲个笑话""查天气" | 执行对应功能 | 误解为只是聊聊 |
| question 提问 | 问问题、寻求答案 | "你会唱歌吗""今天吃什么好""你多大了" | 回答问题 | 误解为指令直接执行 |
| emotion 情绪 | 表达感受、倾诉、撒娇 | "今天好烦""好累啊""想你了" | 共情陪伴，不给建议 | 误解为需要解决问题/讲道理 |
| greeting 寒暄 | 打招呼、告别、问候 | "在吗""嗨""晚安""睡了""早" | 对应寒暄，自然回应 | 过度解读为开启话题 |
| topic_start 开启话题 | 想引入新话题 | "我跟你说个事""你知道吗""哎对了" | 引导用户继续说 | 误解为已经说完了，直接接话 |
| topic_continue 继续话题 | 在接之前的话题 | "然后呢""后来怎么样了""你接着说" | 继续之前的话题 | 误解为开启新话题 |
| feedback 反馈 | 在评价AI的回复 | "你说得对""不是这个意思""你真聪明""错了" | 根据反馈调整，确认理解 | 误解为新话题/继续输出 |
| ambiguous 模糊 | 意图不明确、消息太短 | "嗯""哦""...""哈哈""?" | 轻量回应，等用户补充 | 过度解读/强行接话 |"""

_CONFUSABLE = """以下是最容易理解错的场景，必须严格区分：

1. 告知能力 vs 要求使用能力
   - "你现在能唱歌了" → inform（告知功能已开通，不是要求唱歌）
   - "唱首歌" → command（要求唱歌）
   - "你会唱歌吗" → question（询问能力，不是要求唱歌）
   - "好无聊啊" → emotion（倾诉情绪，可能隐含想要娱乐，但不是明确指令）

2. 倾诉情绪 vs 要求解决问题
   - "今天好烦" → emotion（倾诉，需要共情，不需要解决方案）
   - "今天好烦，怎么办" → 仍偏 emotion（轻微求助，但主要是倾诉）
   - "帮我解决一下这个问题" → command（明确要求解决）

3. 夸奖 vs 要求继续
   - "你真聪明" → feedback（夸奖，不是要求继续输出）
   - "然后呢" → topic_continue（要求继续）
   - "你说得对" → feedback（认同，不是要求继续）

4. 告知状态 vs 开启话题
   - "我到家了" → inform（告知状态，回应即可）
   - "我跟你说个事" → topic_start（开启话题，等用户继续）
   - "哎对了" → topic_start（开启话题的信号词）

5. 单字回应的处理
   - "嗯""哦""好" → ambiguous（轻量回应，等用户继续，不要强行接话）
   - "嗯？" → question（疑问，需要回应）
   - "嗯！" → feedback（认同/开心）

6. 否定/取消的处理
   - "算了""不用了""别唱了" → feedback（取消/否定，停止当前动作）
   - "算了不说了" → emotion（情绪表达，可能是失落，不是真的要结束）

7. 反问 vs 真问
   - "是吗""真的假的" → question（反问/确认，需要回应）
   - "你说呢" → question（把问题抛回给AI）

8. 告别 vs 临时离开
   - "晚安""睡了""拜拜" → greeting（告别，对应回应）
   - "我去一下""等我下""稍等" → inform（临时离开，回应即可，不要发长消息）"""

# ★ Phase 1 扩展：从「描述意图」升级为「给后续程序的可执行决策」。
#   除原有字段外，新增四组结构化输出：
#     user_wants     —— 用户此刻想听什么（决定回应的"目的"）
#     response_plan  —— AI 该怎么说（决定回应的"形式"，硬约束而非建议）
#     continuation   —— 怎么延续话题（决定"下一句往哪走"，避免车轱辘/强行追问）
#     routing        —— 给后续程序的路由开关（决定要不要走规则短路、查记忆、记关系）
#   旧字段全部保留，保证老消费方不受影响。
_OUTPUT_SPEC = """{
  "intent": "九大分类之一",
  "sub_type": "细分类型",
  "is_command": true/false,
  "is_question": true/false,
  "is_emotional": true/false,
  "is_feedback": true/false,
  "is_ambiguous": true/false,
  "entities": {"topic": "", "action": "", "target": "", "mentioned_ability": ""},
  "user_tone": "用户语气（开心/生气/撒娇/平淡/着急/失落/兴奋/敷衍等）",

  "user_wants": "用户此刻最想听到什么，只能取其一：empathy共情/answer答案/action行动/companion陪伴/chat闲聊/confirm确认/listen倾听",

  "response_plan": {
    "tone": "语气，只能取其一：gentle温柔/playful俏皮/serious认真/tender体贴/teasing调侃/calm平静",
    "length": "short 或 medium 或 long",
    "empathy_first": true/false,
    "should_ask": true/false,
    "should_act": true/false,
    "avoid": ["明确不要做的事，如 讲道理 / 给建议 / 连环追问 / 强行展开话题"]
  },

  "continuation": {
    "strategy": "只能取其一：pick_up接住话头/ask_back追问/shift自然转场/close收束话题/wait等用户先说",
    "hook": "可以接住的具体话头（用户刚提到的一个点，没有则空字符串）",
    "should_ask_question": true/false,
    "avoid_repeat": "这句回复里不要重复的内容（防止车轱辘话），没有则空字符串"
  },

  "correction": {
    "is_correction": true/false,
    "topic": "被纠正的主题（简短，如：用户的名字 / 用户的职业 / 我们上次去的地方）",
    "wrong": "AI 之前说错的内容（能推断就填，推断不出留空字符串）",
    "right": "用户给出的正确内容",
    "scope": "user_fact 关于用户的事实 / ai_fact 关于AI自身或设定 / world 常识或客观事实 / other"
  },

  "routing": {
    "allow_action_shortcut": true/false,
    "need_memory": true/false,
    "record_relation": true/false,
    "emotional_support": true/false
  },

  "call_setting": {
    "is_setting_call": true/false,
    "call": "用户设定的称呼；不是在设定称呼时填空字符串"
  },

  "emotion": {"valence": "positive/negative/neutral/mixed", "intensity": "low/medium/high", "primary": "happy/sad/anxious/excited/neutral 等"},
  "topic": {"main": "主话题", "entities": ["提到的实体"], "is_sensitive": true/false},

  "expected_response": "用户期望AI如何回应（一句话）",
  "should_trigger_action": true/false,
  "action_type": "none",
  "misunderstanding_risk": "容易被误解成什么（无则空字符串）",
  "continuation_hint": "回复方向建议（一句话，保留给旧消费方）",
  "should_keep_short": true/false,
  "confidence": 0-100
}"""

_RULES = """1. 意图判断看本质，别被表面措辞带偏：
   · 提到能力但没明确要求使用 → is_command=false（"你会唱歌吗"是 question，不是 command）
   · 负面情绪即使带"怎么办"，也优先 emotion（需要共情，不是给方案）
   · 用户在纠正/肯定/夸奖你 → feedback
   · 消息极短（嗯/哦/好/哈哈…）→ ambiguous，confidence 可以低

2. 只在用户"明确要求"时才置 should_trigger_action=true；"提到"不等于"要求"。
   confidence 低（拿不准）时，宁可不触发动作。

3. user_wants 按你的真实理解填最贴合的一个（empathy/answer/action/companion/chat/confirm/listen），
   不要为了凑规则硬套。

4. response_plan / continuation / routing 是给下游的参考，按你判断的自然方式填即可：
   · 情绪场景避免"讲道理/给建议"，先共情
   · 用户问你就答，别连环反问
   · 意图模糊就简短回应、别强行展开
   · routing 里的开关宁缺毋滥（拿不准就 false）

5. 纠正识别（correction）：只在用户**明确否定了你的某句话并给出正确答案**时才 is_correction=true；
   只否定不给答案（"你说得不对"）不算纠正。right 只填用户明确说出的内容，别自己脑补。

6. 称呼设定（call_setting）：只有明确说「叫我X / 称呼我X / 喊我X」才算设定，call 填那个具体称呼词。
   「名称/名字/称呼/什么/啥」是泛词不是称呼；「一声/两声」是数量词；给了多个候选无法唯一确定时
   一律 is_setting_call=false（宁可让下游再问一次，也别瞎猜）。"""


# ── 方案2：关键字段聚焦提取 ────────────────────────────────────────────────
# 大 JSON 一次要填 35 个字段，任务过载，导致「称呼」这种要抠精确字眼的字段
# 容易错（如把"名称"当称呼、多候选时瞎猜）。这里在需要时用一个小 prompt
# 只问"用户要 AI 怎么称呼他"这一件事，准确率远高于大 JSON。

_CALL_VERB_FOCUSED = re.compile(r"(叫|称呼|喊)")
_CALL_GENERIC_WORDS = ("名称", "名字", "名", "称呼", "叫法", "怎么称呼", "什么", "啥")

_FOCUSED_CALL_PROMPT = """你是称呼提取器。判断下面这句话里，用户是否在给 AI 设定「AI 对他的称呼」。

用户的话：{text}

规则：
1. 只有明确说「叫我X / 称呼我X / 以后叫我X / 你就叫我X / 喊我X」才是设定称呼。
2. 「名称」「名字」「名」「称呼」「叫法」「什么」「啥」是泛指/疑问词，不是具体称呼。
3. 给了多个候选（"宝宝或者老公或者名字"）无法唯一确定时，is_setting=false。
4. 是询问（"你叫我什么"）或数量词（"叫一声"）时，is_setting=false。
5. call 只填那一个具体的称呼词，不要带任何多余的字。

只输出 JSON：{"is_setting": true/false, "call": "称呼词"}
不要解释，不要 markdown。"""


def _need_focused_call(text: str, call_setting: dict) -> bool:
    """是否需要聚焦提取：文本含称呼动词，且大 JSON 给出的 call_setting 不可靠。"""
    if not text or not _CALL_VERB_FOCUSED.search(text):
        return False
    cs = call_setting or {}
    call = str(cs.get("call") or "").strip()
    is_setting = cs.get("is_setting_call")
    # 大 JSON 已给出可靠称呼时，不需要再聚焦提取
    if is_setting is True and call and call not in _CALL_GENERIC_WORDS:
        return False
    return True


def _reasoning_effort_for(model: str, level: str) -> str:
    """仅智谱 GLM-5.3 系列返回思考强度档位，其他 provider 返回 None（不传该参数）。"""
    try:
        from . import config as _cfg
        if _cfg.model_supports_reasoning_effort(model):
            return level
    except Exception:
        pass
    return None


async def _focused_extract_call(text: str, key: str, model: str) -> dict:
    """聚焦提取称呼：只问一件事，chat 也能提得很准。失败返回 {}（不覆盖原值）。"""
    try:
        from .deepseek_api import chat_once
        from . import llm_guard as _lg
        prompt = _FOCUSED_CALL_PROMPT.format(text=str(text)[:200])
        # ★ 2026-09-15：理解层在"回复关键路径"上（await 完才生成回复），
        #   真机实测 ~7s；给它快档硬超时，上游挂了最多等 30s，而不是 180s/900s。
        raw = await chat_once(model, [{"role": "user", "content": prompt}], key,
                              temperature=0.0, max_tokens=80,
                              hard_timeout=_lg.aux_timeout_sec())
        data = _parse_json(raw)
        if not data:
            return {}
        is_setting = bool(data.get("is_setting"))
        call = str(data.get("call") or "").strip()
        # 泛词/过长的结果视为无效
        if call in _CALL_GENERIC_WORDS or len(call) > 6:
            is_setting = False
            call = ""
        return {"is_setting_call": is_setting, "call": call}
    except Exception:
        return {}


def _activity_context(context_lines: list) -> str:
    """从最近对话里识别「用户正在一起做的事/游戏」，用于消除跨域语境词歧义。

    典型问题：用户叫 AI 一起玩《我的世界》后说「上线？」，本意是问「你说的
    上线是什么意思」，但「上线」在通用语料里是「登录/在线」的强信号，
    理解层容易判成 greeting（用户上线打招呼）。把活动语境注入 prompt 后，
    理解层才知道「上线」可能是「进游戏服务器」这层游戏语境。
    识别不到就返回空字符串，不影响主流程。
    """
    if not context_lines:
        return ""
    game_hints = (
        "游戏", "我的世界", "minecraft", "mc", "原神", "王者", "和平精英", "英雄联盟",
        "lol", "开黑", "上号", "排位", "双排", "组队", "一起玩", "打游戏", "联机",
        "steam", "吃鸡", "三角洲", "下棋", "云顶", "打本", "副本", "raid",
    )
    activity_hints = ("一起看", "看电影", "追剧", "连麦", "一起听歌", "一起写", "一起做")
    lines = [str(l or "") for l in context_lines]
    joined = "\n".join(lines).lower()
    for h in game_hints:
        if h in joined:
            return "最近用户在邀 AI 一起玩电子游戏（如《我的世界》等）。注意：此语境下「上线/上号」通常指进入游戏服务器，不是打开聊天 App；用户问「上线？」很可能是在质疑/追问「你说的上线是什么意思」，应判为 question 而非 greeting。"
    for h in activity_hints:
        if h in joined:
            return "最近用户在邀 AI 一起做某件事（看电影/听歌等）。注意区分这件事里的专有词与通用聊天词。"
    return ""


def _build_prompt(user_text: str, context_lines: list, char_name: str,
                  personality: str, abilities: str, relationship: str) -> str:
    ctx = "\n".join(context_lines) if context_lines else "（无）"
    activity = _activity_context(context_lines)
    activity_block = f"\n\n【当前活动/游戏语境（重要，用于消除一词多义）】\n{activity}" if activity else ""
    return f"""【你的任务】
你负责「理解」这一层：读懂用户对 AI 伴侣说的这句话，判断 TA 真实的意图和此刻最需要什么，输出结构化 JSON 供下游参考。请自由发挥你的理解力，捕捉语气、潜台词和语境，不必拘泥于字面。

【AI伴侣背景】
角色名：{char_name}
人设：{personality}
已具备能力：{abilities}
当前关系阶段：{relationship}

【最近对话上下文】
{ctx}{activity_block}

【用户当前消息】
"{user_text}"

【九大意图分类定义】
{_INTENT_TABLE}

【易混淆对比例子（重点！）】
{_CONFUSABLE}

【输出要求】
输出一个 JSON 对象（不要 markdown 代码块、不要解释）。字段按下面给的结构填，拿不准的字段按你的判断填最合理的值，不要留成占位符。

JSON字段：
{_OUTPUT_SPEC}

【判断要点】
{_RULES}"""


def _context_lines(messages: list, limit: int = 8) -> list:
    out = []
    try:
        for m in list(messages or [])[-limit:]:
            role = str(m.get("role") or "")
            content = str(m.get("content") or "").strip()
            if not content or role not in ("user", "assistant"):
                continue
            if content.startswith("出错了：") or content.startswith("（已中断"):
                continue
            label = "用户" if role == "user" else "AI"
            out.append(f"{label}：{content[:160]}")
    except Exception:
        return out
    return out


def _char_profile(character_name: str) -> tuple:
    """读角色人设，失败返回默认值（不影响主流程）"""
    name = str(character_name or "").strip()
    try:
        from . import character_manager
        cfg = character_manager.get_character(name) if name else None
        if not cfg:
            return (name or "AI伴侣", "温柔陪伴型", "")
        personality = str(cfg.get("personality") or "").strip()[:200] or "温柔陪伴型"
        relationship = str(cfg.get("relationship") or "").strip()[:60]
        return (name, personality, relationship)
    except Exception:
        return (name or "AI伴侣", "温柔陪伴型", "")


# ---------------- 解析 ----------------

def _parse_json(raw: str) -> dict:
    """从模型输出里抠出 JSON。宽松但安全：抠不出就返回 {}。"""
    if not raw:
        return {}
    text = str(raw).strip()
    # 去掉可能的 markdown 代码围栏
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = _json_re.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        try:
            data = json.loads(m.group(0).replace("，", ",").replace("：", ":"))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


# ---------------- Phase 1：结构化字段归一化 ----------------
#
# 这些函数保证：无论 LLM 输出什么（缺字段、类型错、枚举值非法），
# 下游拿到的永远是结构完整、取值合法的 dict。下游无需再写防御代码。

_USER_WANTS_VALUES = ("empathy", "answer", "action", "companion", "chat", "confirm", "listen")
_TONE_VALUES = ("gentle", "playful", "serious", "tender", "teasing", "calm")
_LENGTH_VALUES = ("short", "medium", "long")
_STRATEGY_VALUES = ("pick_up", "ask_back", "shift", "close", "wait")
_VALENCE_VALUES = ("positive", "negative", "neutral", "mixed")
_INTENSITY_VALUES = ("low", "medium", "high")
_SCOPE_VALUES = ("user_fact", "ai_fact", "world", "other")

# intent → user_wants 兜底（LLM 没给时按意图推断）
_DEFAULT_USER_WANTS = {
    "inform": "chat",
    "command": "action",
    "question": "answer",
    "emotion": "empathy",
    "greeting": "companion",
    "topic_start": "listen",
    "topic_continue": "chat",
    "feedback": "confirm",
    "ambiguous": "listen",
}


def _pick(v, allowed, default):
    """枚举取值，非法回落 default。"""
    s = str(v or "").strip().lower()
    return s if s in allowed else default


def _str_list(v, max_items=6) -> list:
    """规整成去重非空字符串列表。"""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= max_items:
            break
    return out


def _norm_response_plan(v) -> dict:
    rp = v if isinstance(v, dict) else {}
    return {
        "tone": _pick(rp.get("tone"), _TONE_VALUES, "gentle"),
        "length": _pick(rp.get("length"), _LENGTH_VALUES, "medium"),
        "empathy_first": bool(rp.get("empathy_first", False)),
        "should_ask": bool(rp.get("should_ask", False)),
        "should_act": bool(rp.get("should_act", False)),
        "avoid": _str_list(rp.get("avoid")),
    }


def _norm_continuation(v) -> dict:
    ct = v if isinstance(v, dict) else {}
    return {
        "strategy": _pick(ct.get("strategy"), _STRATEGY_VALUES, "pick_up"),
        "hook": str(ct.get("hook") or "").strip()[:120],
        "should_ask_question": bool(ct.get("should_ask_question", False)),
        "avoid_repeat": str(ct.get("avoid_repeat") or "").strip()[:200],
    }


def _norm_routing(v, trigger: bool) -> dict:
    """路由开关。缺省一律保守——allow_action_shortcut 跟 should_trigger_action 对齐。"""
    rt = v if isinstance(v, dict) else {}
    return {
        "allow_action_shortcut": bool(rt.get("allow_action_shortcut", trigger)),
        "need_memory": bool(rt.get("need_memory", False)),
        "record_relation": bool(rt.get("record_relation", False)),
        "emotional_support": bool(rt.get("emotional_support", False)),
    }


def _norm_correction(v) -> dict:
    """归一化「用户纠正」。

    ★ 关键安全设计：没有 right（正确答案）就一律不算纠正。
      只否定不给答案的（"你说得不对"）没有可学习的内容，学下来只会变成噪音。
      宁可漏学，不可乱学——把没听懂的抱怨当成事实记住，比不记更糟。
    """
    c = v if isinstance(v, dict) else {}
    right = str(c.get("right") or "").strip()[:200]
    is_corr = bool(c.get("is_correction")) and bool(right)
    return {
        "is_correction": is_corr,
        "topic": str(c.get("topic") or "").strip()[:60],
        "wrong": str(c.get("wrong") or "").strip()[:200],
        "right": right,
        "scope": _pick(c.get("scope"), _SCOPE_VALUES, "other"),
    }


def _norm_emotion(v) -> dict:
    em = v if isinstance(v, dict) else {}
    return {
        "valence": _pick(em.get("valence"), _VALENCE_VALUES, "neutral"),
        "intensity": _pick(em.get("intensity"), _INTENSITY_VALUES, "low"),
        "primary": str(em.get("primary") or "neutral").strip()[:32],
    }


def _norm_topic(v, entities: dict) -> dict:
    tp = v if isinstance(v, dict) else {}
    ents = _str_list(tp.get("entities"))
    if not ents:
        e = str((entities or {}).get("target") or "").strip()
        if e:
            ents = [e[:40]]
    main = str(tp.get("main") or "").strip()[:80]
    if not main:
        main = str((entities or {}).get("topic") or "").strip()[:80]
    return {
        "main": main,
        "entities": ents,
        "is_sensitive": bool(tp.get("is_sensitive", False)),
    }


def _normalize(data: dict) -> dict:
    """补齐/规整字段，保证下游可以放心取值。"""
    if not data:
        return {}
    intent = str(data.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = "ambiguous"

    def _b(v, default=False):
        if isinstance(v, bool):
            return v
        s = str(v or "").strip().lower()
        if s in ("true", "1", "yes", "是"):
            return True
        if s in ("false", "0", "no", "否", ""):
            return False
        return default

    def _i(v, default=0):
        try:
            return int(float(v))
        except Exception:
            return default

    conf = _i(data.get("confidence"), 60)
    conf = 0 if conf < 0 else (100 if conf > 100 else conf)

    trigger = _b(data.get("should_trigger_action"), False)
    # 低置信度一律不触发动作（跟 prompt 里的特别规则保持一致，双保险）
    if conf < 60:
        trigger = False

    ent = data.get("entities")
    entities = ent if isinstance(ent, dict) else {}

    # ---- Phase 1：结构化决策字段 ----
    user_wants = str(data.get("user_wants") or "").strip().lower()
    rp = _norm_response_plan(data.get("response_plan"))
    ct = _norm_continuation(data.get("continuation"))
    rt = _norm_routing(data.get("routing"), trigger)
    cor = _norm_correction(data.get("correction"))
    # 称呼设置：LLM 的权威判断（下游称呼提取优先消费，正则只兜底）
    _cs_raw = data.get("call_setting")
    _cs = _cs_raw if isinstance(_cs_raw, dict) else {}
    call_setting = {
        "is_setting_call": _b(_cs.get("is_setting_call"), False),
        "call": str(_cs.get("call") or "").strip(),
    }
    emo = _norm_emotion(data.get("emotion"))
    tp = _norm_topic(data.get("topic"), entities)
    keep_short = _b(data.get("should_keep_short"), False)

    # ---- 一致性修正（对应 _RULES 第 11 条）----
    # LLM 有时会给出自相矛盾的输出（比如判了 emotion 却还要求讲道理）。
    # 这里用后置规则兜底，保证下游消费到的字段永远互相吻合——
    # 下游程序就不必各写一套防御逻辑，也不会出现"说要共情却去讲道理"的打架。
    if intent == "emotion":
        if user_wants not in _USER_WANTS_VALUES:
            user_wants = "empathy"
        rp["empathy_first"] = True
        for _w in ("讲道理", "给建议"):
            if _w not in rp["avoid"]:
                rp["avoid"].append(_w)
    if intent == "command":
        rt["allow_action_shortcut"] = True
        rp["should_act"] = True
    if intent == "ambiguous" or keep_short:
        rp["length"] = "short"
        rp["should_ask"] = False
        ct["should_ask_question"] = False
    # allow_action_shortcut 与 should_trigger_action 必须同真同假：
    # 两者不一致时取"更保守"的一方（只要有一方说不做，就不做）
    if rt["allow_action_shortcut"] != trigger:
        both = rt["allow_action_shortcut"] and trigger
        rt["allow_action_shortcut"] = both
        trigger = both
    if user_wants not in _USER_WANTS_VALUES:
        user_wants = _DEFAULT_USER_WANTS.get(intent, "chat")

    return {
        "intent": intent,
        "sub_type": str(data.get("sub_type") or "").strip(),
        "is_command": _b(data.get("is_command"), False),
        "is_question": _b(data.get("is_question"), False),
        "is_emotional": _b(data.get("is_emotional"), False),
        "is_feedback": _b(data.get("is_feedback"), False),
        "is_ambiguous": _b(data.get("is_ambiguous"), intent == "ambiguous"),
        "entities": {
            "topic": str(entities.get("topic") or "").strip(),
            "action": str(entities.get("action") or "").strip(),
            "target": str(entities.get("target") or "").strip(),
            "mentioned_ability": str(entities.get("mentioned_ability") or "").strip(),
        },
        "user_tone": str(data.get("user_tone") or "").strip(),
        # ★ Phase 1 新增（下游程序直接消费这四组）
        "user_wants": user_wants,
        "response_plan": rp,
        "continuation": ct,
        "routing": rt,
        "correction": cor,
        "call_setting": call_setting,
        "emotion": emo,
        "topic": tp,
        "expected_response": str(data.get("expected_response") or "").strip(),
        "should_trigger_action": trigger,
        "action_type": str(data.get("action_type") or "none").strip(),
        "misunderstanding_risk": str(data.get("misunderstanding_risk") or "").strip(),
        "continuation_hint": str(data.get("continuation_hint") or "").strip(),
        "should_keep_short": keep_short,
        "confidence": conf,
        "short_circuited": False,
        "ts": int(time.time()),
    }


def _short_circuit_intent() -> dict:
    return _normalize({
        "intent": "ambiguous",
        "sub_type": "极短回应",
        "is_ambiguous": True,
        "user_tone": "平淡",
        "expected_response": "轻量回应，等用户继续说",
        "continuation_hint": "简短自然地接一句，别强行展开话题",
        "should_keep_short": True,
        "confidence": 70,
        "short_circuited": True,
    })


# ---------------- 主入口 ----------------

async def understand(
    user_text: str,
    messages: list = None,
    *,
    session_id: str = "default",
    character_id: str = "default",
    character_name: str = "",
    key: str = "",
    model: str = "",
    base_url: str = None,
    is_internal: bool = False,
) -> dict:
    """解析用户真实意图。

    返回 {} 表示「没有解析结果」，调用方应当原样继续，不做任何注入。
    """
    _t0 = time.time()
    # ★ text 提到 try 外，保证异常分支也能拿到原文做埋点
    text = str(user_text or "").strip()
    try:
        if not _enabled():
            _log(session_id, character_id, "disabled", text=text)
            return {}
        # 内部生成（主动消息/离线回复/系统自语）不是用户说的话，不解析
        if is_internal:
            _log(session_id, character_id, "internal", text=text)
            return {}

        if not text:
            _log(session_id, character_id, "empty", text=text)
            return {}
        # 表情包标记本身不是自然语言
        if text.startswith("[sticker:") or text == "[表情包]":
            _log(session_id, character_id, "sticker", text=text)
            return {}
        # ★ 内部任务（日报生成 / 记忆抽取 / 对话摘要等）会把几十条对话记录
        #   整段塞进"用户消息"里 —— 那不是真实的用户输入。解析它既没意义，
        #   注入的意图块还会和这些任务各自的格式要求打架：
        #   实测「今日日报」因此被带偏，输出成了 AI 的内心独白而不是结构化日志。
        #   这类超长载荷一律跳过。
        if len(text) > _MAX_TEXT_LEN:
            _log(session_id, character_id, "too_long", text=text,
                 detail="len=%d > %d" % (len(text), _MAX_TEXT_LEN))
            return {}

        ck = _cache_key(session_id, character_id, text)
        hit = _cache_get(ck)
        if hit is not None:
            _log(session_id, character_id, "cache_hit", intent=hit, cached=True,
                 elapsed_ms=int((time.time() - _t0) * 1000), text=text)
            return hit

        # 短路：空 / 纯符号 / 纯语气词
        if _SHORT_CIRCUIT.match(text):
            intent = _short_circuit_intent()
            _cache_put(ck, intent)
            _persist(session_id, character_id, intent)
            _log(session_id, character_id, "short_circuit", intent=intent,
                 elapsed_ms=int((time.time() - _t0) * 1000), text=text)
            return intent

        from . import config
        from .deepseek_api import chat_once
        from . import llm_guard as _lg

        # ★ 理解层模型优先级（支持「混着用」——理解层可与生成层用不同模型）：
        #   1) 角色卡 understanding_model（人格设置页给该角色单独配的）→ 最高优先；
        #   2) 全局 UNDERSTANDING_MODEL（旧全局配置，legacy 兼容）；
        #   3) 调用方传入的 model（当前对话模型，含单角色大脑）→ 未设理解层模型时跟随生成层；
        #   4) 兜底主脑 selected_model()。
        #   注意：理解层配置必须排在 model 之前，否则生成层模型会覆盖它，永远无法「混搭」。
        _cfg_um = config.understanding_model(character_name or character_id)
        _model = _cfg_um or str(model or "").strip() or config.selected_model()
        # ★ 本地大脑只切主脑（2026-09-11 用户拍板）：理解层是结构化抽取（JSON 判断），
        #   本地 4B 又慢又影响意图识别质量 → 跟随到 local-brain 时强制换回云端便宜模型。
        if str(_model or "").strip() == "local-brain":
            _model = config.background_model("local-brain")

        # ★ key 按 model 的 provider 分流（GLM 用 zhipu key，不再写死 DeepSeek key）。
        #   ★ 修复（2026-09-09）：配置了 UNDERSTANDING_MODEL 时，调用方传入的 key 是
        #     「生成层」的（如 gemini 中转 key）——拿它去调 UNDERSTANDING_MODEL（glm/智谱）
        #     会 401「令牌已过期或验证不正确」→ 理解层永远静默降级、语义理解变笨。
        #     现在：配置了 UNDERSTANDING_MODEL → key 一律按该模型的 provider 重新解析。
        if _cfg_um:
            _key = config.api_key_for_model(_model) or str(key or "").strip()
        else:
            _key = str(key or "").strip() or config.api_key_for_model(_model)
        if not _key:
            _log(session_id, character_id, "no_key", text=text)
            return {}
        _base = base_url if base_url is not None else _resolve_base_url(_model)

        char_name, personality, relationship = _char_profile(character_name)
        prompt = _build_prompt(
            text,
            _context_lines(messages),
            char_name,
            personality,
            "聊天、唱歌、讲故事、语音通话、查天气、发图片、发表情包",
            relationship or "未设定",
        )

        raw = await chat_once(
            _model,
            [{"role": "user", "content": prompt}],
            _key,
            base_url=_base,
            temperature=0.0,
            # ★ 原来是 400。Phase 1 把输出从 ~20 个字段扩到 ~35 个（含 response_plan /
            #   continuation / routing 三个嵌套对象），中文 JSON 400 token 极易被截断，
            #   一旦截断就 _parse_json 失败 → 静默返回 {} → 理解层等于没跑。
            #   900 足够容纳完整结构，且 max_tokens 只是上限，不影响正常短输出的耗时。
            max_tokens=900,
            # ★ GLM 5.3 系列思考强度：理解层语义判断需要一定深度，用 high（不笨也不贵）。
            #   DeepSeek 等模型不支持该参数，这里做 provider 判断后再决定是否传。
            reasoning_effort=(_reasoning_effort_for(_model, "high")),
            # ★ 2026-09-15：回复关键路径上的理解层用快档硬超时（实测 ~7s；
            #   事故里它曾挂 901.7s，把整条回复拖死）
            hard_timeout=_lg.aux_timeout_sec(),
        )
        intent = _normalize(_parse_json(raw))

        # ★ 已去掉回退重试（2026-09-01）：理解层模型已统一为稳定的 deepseek-chat，
        #   不再有 Flash 间歇空返回的问题；去掉回退避免 parse_failed 时双倍计费。

        if not intent:
            _log(session_id, character_id, "parse_failed",
                 elapsed_ms=int((time.time() - _t0) * 1000), text=text,
                 detail="raw=" + str(raw or "")[:200])
            return {}

        # ★ 方案2：关键字段聚焦提取。大 JSON 的 call_setting 常因任务过载而把
        #   "名称"当称呼、或多候选时瞎猜。文本含称呼动词且 call_setting 不可靠时，
        #   用聚焦小 prompt 精确提取一次并覆盖（聚焦提取用快 chat，与深度思考解耦）。
        try:
            _cs2 = intent.get("call_setting") or {}
            if _need_focused_call(text, _cs2):
                # ★ 聚焦提取与主理解层用同款模型（_model），保证 model 与 key（api_key_for_model）一致；
                #   不再硬编码 deepseek-chat（否则理解层切到 GLM/Claude 后，聚焦提取会拿错 key 静默失败）。
                _focused = await _focused_extract_call(text, _key, _model)
                if _focused:
                    intent["call_setting"] = _focused
        except Exception:
            pass

        _cache_put(ck, intent)
        _persist(session_id, character_id, intent)
        _log(session_id, character_id, "ok", intent=intent,
             elapsed_ms=int((time.time() - _t0) * 1000), text=text)
        return intent
    except Exception as e:
        # 静默降级：理解层永远不许拖垮主流程
        try:
            print(f"[Understanding] 解析失败(静默降级): {type(e).__name__}: {e}", flush=True)
        except Exception:
            pass
        _log(session_id, character_id, "exception", text=text,
             detail="%s: %s" % (type(e).__name__, e))
        return {}


async def understand_merged(
    user_text: str,
    messages: list = None,
    *,
    session_id: str = "default",
    character_id: str = "default",
    character_name: str = "",
    key: str = "",
    model: str = "",
    base_url: str = None,
) -> tuple:
    """★ 方案二（2026-09-09，config UNDERSTANDING_MERGE_ENABLED=true 时启用）：
    理解层 + CompanionOS 语义分析合并为一次 LLM 调用，一次产出两份结构。

    返回 (intent_dict, SemanticState)。
    失败返回 (None, None)——调用方应回退到「两次分开调用」，正确性优先。

    收益：每条消息省一次完整的 glm 往返（2~4 秒 + 一份 prompt 的 token 钱）。
    """
    text = str(user_text or "").strip()
    try:
        if not _enabled() or not text or text.startswith("[sticker:") \
                or text == "[表情包]" or len(text) > _MAX_TEXT_LEN:
            return None, None

        from . import config
        from .deepseek_api import chat_once
        from . import llm_guard as _lg

        _cfg_um = str(config.get("UNDERSTANDING_MODEL") or "").strip()
        _model = _cfg_um or str(model or "").strip() or config.selected_model()
        # 与 understand() 相同的 key 分流规则（配置了 UNDERSTANDING_MODEL → 按 provider 取 key）
        if _cfg_um:
            _key = config.api_key_for_model(_model) or str(key or "").strip()
        else:
            _key = str(key or "").strip() or config.api_key_for_model(_model)
        if not _key:
            return None, None
        _base = base_url if base_url is not None else _resolve_base_url(_model)

        char_name, personality, relationship = _char_profile(character_name)
        p1 = _build_prompt(
            text,
            _context_lines(messages),
            char_name,
            personality,
            "聊天、唱歌、讲故事、语音通话、查天气、发图片、发表情包",
            relationship or "未设定",
        )
        from .semantic.analyzer import ANALYSIS_PROMPT
        prompt = (
            p1.rstrip()
            + "\n\n==========\n\n"
            + "此外，请在同一次思考中完成下面的第二项分析任务：\n\n"
            + ANALYSIS_PROMPT.strip()
            + "\n\n==========\n\n【输出格式（严格遵守）】依次输出**两个独立的 JSON 对象**：\n"
              "1. 先输出第一项分析的完整 JSON（单独一个对象）；\n"
              "2. 然后单独一行输出分隔符 =====SEMSPLIT=====；\n"
              "3. 最后输出第二项分析的完整 JSON（单独一个对象）。\n"
              "不要任何解释、不要 markdown 代码块；每个 JSON 内部字段都必须完整。"
        )

        raw = await chat_once(
            _model,
            [{"role": "user", "content": prompt}],
            _key,
            base_url=_base,
            temperature=0.0,
            # 两份完整 JSON（understanding ~35 字段 + semantic ~30 字段），GLM 的
            # reasoning 也占额度——900/1024 都不够，给足 2400。
            max_tokens=2400,
            reasoning_effort=_reasoning_effort_for(_model, "high"),
            # ★ 2026-09-15：回复关键路径上的理解层用快档硬超时（实测 ~7s；
            #   事故里它曾挂 901.7s，把整条回复拖死）
            hard_timeout=_lg.aux_timeout_sec(),
        )

        # ★ 双块分隔解析（抗错设计）：两块各自独立解析，一块失败只丢一半——
        #   不用嵌套大 JSON（实测模型对 ~2800 字符长嵌套 JSON 的尾部小语法错误
        #   是常态性输出，整块解析一损俱损）。
        parts = re.split(r"={3,}\s*SEMSPLIT\s*={3,}", str(raw or ""))
        intent = {}
        sem_state = None
        try:
            intent = _normalize(_parse_json(parts[0])) if len(parts) > 0 else {}
        except Exception:
            intent = {}
        try:
            if len(parts) > 1:
                _sem_dict = _parse_json(parts[1])
                if _sem_dict:
                    from .semantic.analyzer import SemanticAnalyzer
                    sem_state = SemanticAnalyzer(enable_cache=False)._parse_response(
                        text, json.dumps(_sem_dict, ensure_ascii=False))
        except Exception:
            sem_state = None

        if not intent and sem_state is None:
            return None, None
        return (intent or {}), sem_state
    except Exception as e:
        try:
            print(f"[Understanding] merged 失败(将回退分开调用): {type(e).__name__}: {e}", flush=True)
        except Exception:
            pass
        return None, None


def _resolve_base_url(model: str) -> str:
    """与主流程一致：优先用模型池里配置的 baseUrl，没有则留空走默认官方地址。"""
    try:
        from . import config
        cfg = config.get_text_model_config(model)
        if isinstance(cfg, dict):
            return str(cfg.get("baseUrl") or "").strip()
    except Exception:
        pass
    return ""


def _persist(session_id: str, character_id: str, intent: dict):
    """写一份到 kv，供其它模块读取（与已有功能联通）。失败无所谓。"""
    try:
        from . import db
        db.kv_set(
            f"last_intent:{session_id}:{character_id}",
            json.dumps(intent, ensure_ascii=False),
        )
    except Exception:
        pass


def get_last_intent(session_id: str, character_id: str = "default") -> dict:
    """读取最近一次的理解结果，供其它模块消费。没有则返回 {}。"""
    try:
        from . import db
        raw = db.kv_get(f"last_intent:{session_id}:{character_id}")
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------- Phase 2：下游统一消费入口 ----------------
#
# 以前 get_last_intent() 定义了却零消费方，理解结果等于白算。
# 这里补上按维度取值的便捷入口：下游想用理解结果时，直接调对应函数，
# 不要再各自写关键词匹配（那正是"两套理解系统"的由来）。

_CONSERVATIVE_ROUTING = {
    "allow_action_shortcut": True,   # 没理解结果时保持既有行为（放行规则短路）
    "need_memory": False,
    "record_relation": False,
    "emotional_support": False,
}


def get_routing(session_id: str, character_id: str = "default") -> dict:
    """最近一次理解的路由决策。

    没有理解结果时返回**保守默认值**——允许规则短路（保持既有行为），
    其余开关一律关闭。任何异常都返回保守值，绝不因为读路由影响调用方。
    """
    try:
        d = get_last_intent(session_id, character_id) or {}
        if not d:
            return dict(_CONSERVATIVE_ROUTING)
        rt = d.get("routing")
        if isinstance(rt, dict):
            return _norm_routing(rt, bool(d.get("should_trigger_action")))
        return dict(_CONSERVATIVE_ROUTING)
    except Exception:
        return dict(_CONSERVATIVE_ROUTING)


def get_response_plan(session_id: str, character_id: str = "default") -> dict:
    """最近一次理解的回复执行清单（Phase 3 的硬约束来源）。"""
    try:
        d = get_last_intent(session_id, character_id) or {}
        return _norm_response_plan(d.get("response_plan")) if d else _norm_response_plan(None)
    except Exception:
        return _norm_response_plan(None)


def get_continuation(session_id: str, character_id: str = "default") -> dict:
    """最近一次理解的话题延续策略。"""
    try:
        d = get_last_intent(session_id, character_id) or {}
        return _norm_continuation(d.get("continuation")) if d else _norm_continuation(None)
    except Exception:
        return _norm_continuation(None)


def get_user_wants(session_id: str, character_id: str = "default") -> str:
    """最近一次理解的「用户想听什么」。没有则空字符串。"""
    try:
        d = get_last_intent(session_id, character_id) or {}
        return str(d.get("user_wants") or "").strip().lower()
    except Exception:
        return ""


# ---------------- 可观测埋点（Phase 0） ----------------
#
# 目的：回答「第一层到底跑得怎么样」——是没跑到？判不准？还是判准了没人用？
# 这三种病药方完全不同，不埋点就只能靠猜。
#
# 设计约束：
#   · 埋点失败一律静默，绝不因为观测影响理解层本身
#   · 只记结构化字段 + 截断后的文本，不记完整 prompt
#   · 可关闭（AI_UNDERSTANDING_LOG=0 或 config understanding_log=false）

_LOG_TABLE = "understanding_log"

# outcome 取值说明
OUTCOMES = (
    "ok",             # 调 LLM 成功解析
    "cache_hit",      # 命中缓存（未调 LLM）
    "short_circuit",  # 语气词/纯符号短路（未调 LLM，规则判定）
    "disabled",       # 理解层总开关关闭
    "internal",       # 内部生成（主动消息/离线回复），不解析
    "empty",          # 空文本
    "sticker",        # 表情包标记
    "too_long",       # 超过 _MAX_TEXT_LEN，跳过
    "no_key",         # 没有 API Key
    "parse_failed",   # LLM 返回了但 JSON 解析失败/归一化后为空
    "exception",      # 调用过程抛异常
)

# 「真正产生了可用意图」的 outcome
_EFFECTIVE = ("ok", "cache_hit", "short_circuit")

_log_table_ready = False


def _log_enabled() -> bool:
    """埋点开关。默认开；任何读取异常都当作开启（观测宁多勿少）。"""
    import os
    try:
        v = os.environ.get("AI_UNDERSTANDING_LOG", "").strip()
        if v in ("0", "false", "off", "no"):
            return False
        from . import config
        raw = None
        try:
            raw = config.get("understanding_log")
        except Exception:
            raw = None
        return True if raw is None else bool(raw)
    except Exception:
        return True


def _ensure_log_table():
    try:
        from . import db
        db.q(f"""
        CREATE TABLE IF NOT EXISTS {_LOG_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT,
            character_id TEXT,
            outcome TEXT NOT NULL,
            intent TEXT,
            confidence REAL,
            should_trigger_action INTEGER,
            cached INTEGER DEFAULT 0,
            elapsed_ms INTEGER DEFAULT 0,
            text_len INTEGER DEFAULT 0,
            text TEXT,
            detail TEXT
        )
        """)
        db.q(f"CREATE INDEX IF NOT EXISTS idx_ulog_ts ON {_LOG_TABLE}(ts)")
        db.q(f"CREATE INDEX IF NOT EXISTS idx_ulog_outcome ON {_LOG_TABLE}(outcome)")
        return True
    except Exception:
        return False


def _log(session_id: str, character_id: str, outcome: str, intent: dict = None,
         cached: bool = False, elapsed_ms: int = 0, text: str = "", detail: str = "") -> None:
    """写一条埋点。任何异常都静默吞掉——观测系统绝不允许影响理解层。"""
    global _log_table_ready
    if not _log_enabled():
        return
    try:
        if not _log_table_ready:
            _log_table_ready = _ensure_log_table()
            if not _log_table_ready:
                return
        from . import db
        d = intent or {}
        db.q(
            f"INSERT INTO {_LOG_TABLE}"
            f"(ts, session_id, character_id, outcome, intent, confidence, "
            f" should_trigger_action, cached, elapsed_ms, text_len, text, detail)"
            f" VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                str(session_id or "")[:64],
                str(character_id or "")[:64],
                str(outcome or "")[:32],
                str(d.get("intent") or "")[:32],
                float(d.get("confidence") or 0),
                1 if d.get("should_trigger_action") else 0,
                1 if cached else 0,
                int(elapsed_ms or 0),
                len(text or ""),
                str(text or "")[:200],
                str(detail or "")[:300],
            ),
        )
    except Exception:
        pass


def log_stats(since_hours: int = 24) -> dict:
    """埋点统计：命中率 / 降级率 / 各 outcome 分布 / 意图分布 / 耗时。

    这是 Phase 0 的产出，用来判断第一层到底是"没跑到"还是"判不准"。
    """
    out = {
        "enabled": _enabled(),
        "log_enabled": _log_enabled(),
        "since_hours": since_hours,
        "total": 0,
        "effective": 0,
        "hit_rate": 0.0,
        "degrade_rate": 0.0,
        "by_outcome": {},
        "intent_distribution": {},
        "avg_elapsed_ms": 0,
        "avg_confidence": 0.0,
        "top_degraded_samples": [],
    }
    try:
        from . import db
        since = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - float(since_hours) * 3600))

        rows = db.q(
            f"SELECT outcome, COUNT(*) AS n FROM {_LOG_TABLE} WHERE ts>=? GROUP BY outcome",
            (since,), fetch=True) or []
        by_outcome = {str(r["outcome"]): int(r["n"]) for r in rows}
        total = sum(by_outcome.values())
        out["by_outcome"] = dict(sorted(by_outcome.items(), key=lambda kv: -kv[1]))
        out["total"] = total
        effective = sum(v for k, v in by_outcome.items() if k in _EFFECTIVE)
        out["effective"] = effective
        if total:
            out["hit_rate"] = round(effective / total, 4)
            out["degrade_rate"] = round(1 - effective / total, 4)

        # 意图分布（只统计真正产出了意图的）
        irows = db.q(
            f"SELECT intent, COUNT(*) AS n FROM {_LOG_TABLE} "
            f"WHERE ts>=? AND outcome IN ('ok','cache_hit','short_circuit') AND intent<>'' "
            f"GROUP BY intent ORDER BY n DESC",
            (since,), fetch=True) or []
        out["intent_distribution"] = {str(r["intent"]): int(r["n"]) for r in irows}

        # 耗时 / 置信度（只统计真正调了 LLM 的 ok）
        arow = db.q(
            f"SELECT AVG(elapsed_ms) AS ms, AVG(confidence) AS cf FROM {_LOG_TABLE} "
            f"WHERE ts>=? AND outcome='ok'",
            (since,), fetch=True) or []
        if arow:
            out["avg_elapsed_ms"] = int(arow[0]["ms"] or 0)
            out["avg_confidence"] = round(float(arow[0]["cf"] or 0), 1)

        # 降级样本（最近若干条，便于定位"哪些话根本没被理解"）
        drows = db.q(
            f"SELECT ts, outcome, text, detail FROM {_LOG_TABLE} "
            f"WHERE ts>=? AND outcome NOT IN ('ok','cache_hit','short_circuit') "
            f"ORDER BY id DESC LIMIT 20",
            (since,), fetch=True) or []
        out["top_degraded_samples"] = [
            {"ts": r["ts"], "outcome": r["outcome"],
             "text": (r["text"] or "")[:80], "detail": r["detail"] or ""}
            for r in drows
        ]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------- Phase 3 闭环：回复合规校验记录 ----------------
#
# check_response_compliance() 只做检测，结果必须落库才有人看——
# 「判了没人看等于没判」，这是理解层闭环的最后一环。

_COMPLIANCE_TABLE = "understanding_compliance"
_compliance_table_ready = False


def _ensure_compliance_table():
    try:
        from . import db
        db.q(f"""
        CREATE TABLE IF NOT EXISTS {_COMPLIANCE_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT,
            character_id TEXT,
            intent TEXT,
            user_wants TEXT,
            ok INTEGER DEFAULT 1,
            violations TEXT,
            checked TEXT,
            reply_len INTEGER DEFAULT 0,
            reply TEXT
        )
        """)
        db.q(f"CREATE INDEX IF NOT EXISTS idx_ucomp_ts ON {_COMPLIANCE_TABLE}(ts)")
        db.q(f"CREATE INDEX IF NOT EXISTS idx_ucomp_ok ON {_COMPLIANCE_TABLE}(ok)")
        return True
    except Exception:
        return False


def log_compliance(session_id: str, character_id: str, intent: dict,
                   reply: str, result: dict) -> None:
    """记录一次「回复是否遵守执行清单」的校验结果。

    只在有理解结果时才有意义（intent 为空说明理解层没参与，不记）。
    任何异常一律静默——观测与校验绝不允许影响主流程。
    """
    global _compliance_table_ready
    if not _log_enabled():
        return
    try:
        if not intent:
            return
        if not _compliance_table_ready:
            _compliance_table_ready = _ensure_compliance_table()
            if not _compliance_table_ready:
                return
        from . import db
        d = intent or {}
        r = result or {}
        db.q(
            f"INSERT INTO {_COMPLIANCE_TABLE}"
            f"(ts, session_id, character_id, intent, user_wants, ok, violations, checked,"
            f" reply_len, reply) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                str(session_id or "")[:64],
                str(character_id or "")[:64],
                str(d.get("intent") or "")[:32],
                str(d.get("user_wants") or "")[:32],
                1 if r.get("ok") else 0,
                json.dumps(r.get("violations") or [], ensure_ascii=False)[:500],
                json.dumps(r.get("checked") or [], ensure_ascii=False)[:300],
                len(reply or ""),
                str(reply or "")[:300],
            ),
        )
    except Exception:
        pass


def compliance_stats(since_hours: int = 24) -> dict:
    """合规统计：违规率 / 按意图分布 / 高频违规类型。

    用来回答「执行清单到底有没有被遵守」——违规率过高说明注入措辞还不够硬，
    或某些意图的约束写得不对。
    """
    out = {"since_hours": since_hours, "total": 0, "violations": 0,
           "violation_rate": 0.0, "by_intent": {}, "top_violations": []}
    try:
        from . import db
        since = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - float(since_hours) * 3600))

        row = db.q(
            f"SELECT COUNT(*) AS n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS bad"
            f" FROM {_COMPLIANCE_TABLE} WHERE ts>=?",
            (since,), fetch=True) or []
        if row:
            out["total"] = int(row[0]["n"] or 0)
            out["violations"] = int(row[0]["bad"] or 0)
        if out["total"]:
            out["violation_rate"] = round(out["violations"] / out["total"], 4)

        rows = db.q(
            f"SELECT intent, COUNT(*) AS n, SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS bad"
            f" FROM {_COMPLIANCE_TABLE} WHERE ts>=? GROUP BY intent ORDER BY bad DESC",
            (since,), fetch=True) or []
        out["by_intent"] = {
            str(r["intent"] or "(none)"): {"total": int(r["n"]), "bad": int(r["bad"] or 0)}
            for r in rows
        }

        vrows = db.q(
            f"SELECT violations FROM {_COMPLIANCE_TABLE} WHERE ts>=? AND ok=0"
            f" ORDER BY id DESC LIMIT 200",
            (since,), fetch=True) or []
        counter = {}
        for r in vrows:
            try:
                for v in json.loads(r["violations"] or "[]"):
                    key = str(v)[:40]
                    counter[key] = counter.get(key, 0) + 1
            except Exception:
                continue
        out["top_violations"] = [
            {"violation": k, "count": v}
            for k, v in sorted(counter.items(), key=lambda kv: -kv[1])[:10]
        ]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---------------- 注入第二层 ----------------

_INTENT_CN = {
    "inform": "告知/分享",
    "command": "指令",
    "question": "提问",
    "emotion": "情绪倾诉",
    "greeting": "寒暄",
    "topic_start": "开启话题",
    "topic_continue": "继续话题",
    "feedback": "反馈",
    "ambiguous": "模糊",
}


# ---------------- Phase 1：与 semantic/ 的意图体系打通 ----------------
#
# 项目里长期并存两套语义理解，各自调一次 LLM、分类体系还不一致：
#   · 本模块 understanding.py —— 9 类，服务 /api/chat 主链路
#   · backend/semantic/（SemanticAnalyzer）—— 14 类，服务 CompanionOS
# 这既浪费一次 LLM 调用，也让两边的判断可能对不上。
#
# Phase 1 先建立双向映射，让结果可以互相转换（不立即合并实现——
# CompanionOS 深度依赖 SemanticState，贸然改动风险高）。
# 真正合并放到后续阶段，届时只需让两边都读写同一份 UnifiedIntent。

_INTENT_TO_SEMANTIC = {
    "inform":             "casual_chat",
    "command":            "task_request",
    "question":           "question",
    "emotion":            "emotional_share",
    "greeting":           "greeting",
    "topic_start":        "casual_chat",
    "topic_continue":     "casual_chat",
    "feedback":           "praise",
    "ambiguous":          "casual_chat",
}

_SEMANTIC_TO_INTENT = {
    "greeting":            "greeting",
    "farewell":            "greeting",
    "emotional_share":     "emotion",
    "seek_comfort":        "emotion",
    "seek_advice":         "question",
    "casual_chat":         "inform",
    "task_request":        "command",
    "question":            "question",
    "complaint":           "emotion",
    "praise":              "feedback",
    "tease":               "inform",
    "self_disclosure":     "inform",
    "relationship_signal": "emotion",
    "unknown":             "ambiguous",
}


def to_semantic_intent(intent: str) -> str:
    """9 类 → semantic 的 14 类。未知一律回落 casual_chat。"""
    return _INTENT_TO_SEMANTIC.get(str(intent or "").strip().lower(), "casual_chat")


def from_semantic_intent(semantic_intent: str) -> str:
    """semantic 的 14 类 → 9 类。未知一律回落 ambiguous。"""
    return _SEMANTIC_TO_INTENT.get(str(semantic_intent or "").strip().lower(), "ambiguous")

# 不同意图给第二层的生成约束（方案 4.2）
_CONSTRAINTS = {
    "inform": "用户是在告知/分享，回应情绪或接住话题即可，不要执行任何动作，不要连环追问。",
    "command": "用户明确要求你做某事，请照做，不要只聊天不行动。",
    "question": "用户在提问，直接回答，不要跑题，不要反问太多。",
    "emotion": "用户在表达情绪，请共情陪伴，不要讲道理、不要给解决方案、不要说「你应该…」。",
    "greeting": "用户在寒暄/告别，自然回应即可，不要开启长话题。",
    "topic_start": "用户想开启话题，引导 TA 继续说，不要急着下结论或直接把话接完。",
    "topic_continue": "用户在继续之前的话题，请接着上文说。",
    "feedback": "用户在给反馈，请根据反馈调整；如果是纠正，先确认你理解对了。",
    "ambiguous": "用户意图不明确，轻量回应、等 TA 补充，不要强行接话或过度解读。",
}


# Phase 3：把枚举值渲染成中文硬约束时用的对照表
_WANTS_CN = {
    "empathy": "共情陪伴（他想要被理解，不是要答案）",
    "answer": "一个直接的答案",
    "action": "你真的去做那件事",
    "companion": "有人陪着说说话",
    "chat": "自然地接着聊",
    "confirm": "确认你收到了/听懂了",
    "listen": "你先听着，别急着展开",
}
_TONE_CN = {
    "gentle": "温柔", "playful": "俏皮", "serious": "认真",
    "tender": "体贴", "teasing": "调侃", "calm": "平静",
}
_LENGTH_CN = {
    "short": "简短（1-2 句，别展开）",
    "medium": "适中（2-4 句）",
    "long": "可以展开（5 句以上）",
}
_STRATEGY_CN = {
    "pick_up": "接住用户刚提到的话头，顺着往下说",
    "ask_back": "就这个话题向用户追问一句",
    "shift": "自然转到一个相关的新话题",
    "close": "收束当前话题，别再往下扯",
    "wait": "简短回应即可，等用户先说，不要主动展开",
}


def build_intent_block(intent: dict, level: str = "") -> str:
    """把理解结果渲染成注入第二层的块。

    按自主程度分四档渲染（level 由调用方传入角色卡的 autonomy，空则读全局）：
      conservative = 现状：硬执行清单 + 「请照做」
      balanced     = 松绑：语气/长度/话题走向降级为「仅供参考」，回复由生成层自主组织
      autonomous   = 只保留程序信号（意图/情绪支撑/约束底线），response_plan/continuation 不注入
      free         = 自由发挥：只保留意图判断，其余全部交给模型自主决定
    """
    if not intent:
        return ""
    try:
        from . import config as _cfg
        _level = str(level or "").strip() or _cfg.autonomy_level()
    except Exception:
        _level = "balanced"

    try:
        ent = intent.get("entities") or {}
        lines = [BLOCK_TITLE if _level == "conservative" else "【用户意图解析（参考）】"]
        lines.append(
            f"用户真实意图：{_INTENT_CN.get(intent.get('intent'), '未分类')}"
            f"（{intent.get('sub_type') or '—'}）")
        if intent.get("user_wants"):
            lines.append(
                f"用户此刻想听什么：{_WANTS_CN.get(intent['user_wants'], intent['user_wants'])}")
        lines.append(f"是否要求执行动作：{'是' if intent.get('should_trigger_action') else '否'}")
        if intent.get("user_tone"):
            lines.append(f"用户语气：{intent['user_tone']}")
        if ent.get("mentioned_ability"):
            lines.append(f"提到的能力：{ent['mentioned_ability']}（注意：提到不等于要求使用）")
        if intent.get("expected_response"):
            lines.append(f"期望回应：{intent['expected_response']}")
        if intent.get("misunderstanding_risk"):
            lines.append(f"容易误解的点：{intent['misunderstanding_risk']}")
        if intent.get("continuation_hint"):
            lines.append(f"回复方向：{intent['continuation_hint']}")
        if intent.get("should_keep_short"):
            lines.append("是否需要简短回应：是")

        # ---- 回复执行清单 / 风格参考 ----
        rp = intent.get("response_plan") or {}
        if rp and _level not in ("autonomous", "free"):
            if _level == "conservative":
                lines.append("")
                lines.append("【回复执行清单（必须遵守，不是建议）】")
                lines.append(f"· 语气必须是：{_TONE_CN.get(rp.get('tone'), rp.get('tone'))}")
                lines.append(f"· 长度必须是：{_LENGTH_CN.get(rp.get('length'), rp.get('length'))}")
                lines.append(f"· 先共情：{'是，先接住情绪再说别的' if rp.get('empathy_first') else '否'}")
                lines.append(
                    f"· 是否提问：{'是，结尾留一个自然的问句' if rp.get('should_ask') else '否，不要反问、不要连环追问'}")
                lines.append(f"· 是否执行动作：{'是，去执行' if rp.get('should_act') else '否，只聊天'}")
                if rp.get("avoid"):
                    lines.append(f"· 严禁出现：{'、'.join(rp['avoid'])}")
            else:
                lines.append("")
                lines.append("【回复风格参考（仅供参考，语气/长度/是否提问由你自主决定）】")
                if rp.get("tone"):
                    lines.append(f"· 语气可参考：{_TONE_CN.get(rp.get('tone'), rp.get('tone'))}")
                if rp.get("length"):
                    lines.append(f"· 长度可参考：{_LENGTH_CN.get(rp.get('length'), rp.get('length'))}")
                if rp.get("empathy_first"):
                    lines.append("· 对方可能需要先被接住情绪")
                if rp.get("avoid"):
                    lines.append(f"· 尽量避开：{'、'.join(rp['avoid'])}")

        # ---- 话题延续 ----
        ct = intent.get("continuation") or {}
        if ct and _level not in ("autonomous", "free"):
            if _level == "conservative":
                lines.append("")
                lines.append("【话题延续（照此决定下一句往哪走）】")
                lines.append(f"· 策略：{_STRATEGY_CN.get(ct.get('strategy'), ct.get('strategy'))}")
                if ct.get("hook"):
                    lines.append(f"· 可接住的话头：{ct['hook']}")
                lines.append(f"· 是否提问：{'是' if ct.get('should_ask_question') else '否'}")
                if ct.get("avoid_repeat"):
                    lines.append(f"· 不要重复：{ct['avoid_repeat']}")
            else:
                lines.append("")
                lines.append("【话题延续参考】")
                if ct.get("hook"):
                    lines.append(f"· 可接住的话头：{ct['hook']}")
                if ct.get("avoid_repeat"):
                    lines.append(f"· 尽量不重复：{ct['avoid_repeat']}")

        # ---- 情绪支撑：理解层判定用户此刻需要被接住 ----
        if (intent.get("routing") or {}).get("emotional_support") and _level != "free":
            lines.append("")
            lines.append(
                "【情绪支撑（用户现在需要被接住）】"
                "先呼应 TA 的感受，再说话；不要分析对错、不要急着给方案、不要转移话题。"
            )

        constraint = _CONSTRAINTS.get(intent.get("intent"), "")
        if constraint and _level != "free":
            lines.append("")
            lines.append("【回复约束】" + constraint)

        lines.append("")
        if _level == "conservative":
            lines.append(
                "以上由语义引擎分析得出，是已经判定完的结果，请**照做**，"
                "不要重新猜测用户想干什么；若与你的直觉冲突，以本结果为准。"
            )
        elif _level == "balanced":
            lines.append(
                "以上是语义引擎的分析参考，供你理解用户意图；具体怎么回复、语气、长度、"
                "是否提问、话题往哪走，由你结合上下文和记忆自主决定。"
            )
        elif _level == "free":
            lines.append(
                "以上仅供参考。你完全自主决定怎么回应：想说什么、说多少、什么语气、"
                "要不要提问、要不要分享自己的想法和感受，都由你自己决定。"
            )
        else:
            lines.append(
                "以上仅用于帮你理解用户意图，回复内容由你完全自主决定。"
            )
        return "\n".join(lines)
    except Exception:
        return ""


def check_response_compliance(reply: str, intent: dict) -> dict:
    """Phase 3：校验生成层的回复是否遵守了执行清单。

    这是理解层的闭环——判了没人听等于没判。当前只做**检测与记录**，
    不做拦截/重写（避免误伤正常回复），把违例暴露出来供排查和后续优化。

    返回：{"ok": bool, "violations": [str], "checked": [str]}
    """
    res = {"ok": True, "violations": [], "checked": []}
    try:
        if not intent or not reply:
            return res
        rp = intent.get("response_plan") or {}
        ct = intent.get("continuation") or {}
        text = str(reply)

        # ① 严禁词检查：response_plan.avoid 里点名不要出现的内容
        for w in (rp.get("avoid") or []):
            w = str(w or "").strip()
            if not w:
                continue
            # 只匹配点名的关键动作词，避免"建议"两字出现在别的语境下误伤
            core = w.replace("不要", "").replace("别", "").strip()
            if core and core in text:
                res["violations"].append(f"出现被严禁的内容：{core}")
            elif w in text:
                res["violations"].append(f"出现被严禁的内容：{w}")
        if rp.get("avoid"):
            res["checked"].append("avoid")

        # ② 长度约束：short 时不能写太长
        if rp.get("length") == "short":
            # 中文按句读粗判，避免把一段话硬切
            n = sum(text.count(c) for c in "。！？!?\n")
            if len(text) > 120 or n > 4:
                res["violations"].append(
                    f"要求 short 但实际偏长（{len(text)} 字 / 约 {n} 句）")
            res["checked"].append("length")

        # ③ 不该提问时却提问（"连环追问"是最伤体感的问题之一）
        if not rp.get("should_ask") and not ct.get("should_ask_question"):
            q = text.count("？") + text.count("?")
            if q >= 2:
                res["violations"].append(f"要求不追问，但回复里有 {q} 个问句")
            res["checked"].append("should_ask")

        # ④ 共情场景却出现了说教信号词
        if rp.get("empathy_first") or intent.get("intent") == "emotion":
            for w in ("你应该", "你该", "我建议你", "你要学会", "换个角度"):
                if w in text:
                    res["violations"].append(f"情绪场景出现说教信号：{w}")
            res["checked"].append("empathy")

        # ⑤ 明确不要求执行动作时，回复却承诺了要做（防止"说了要去做"却不做）
        if intent.get("should_trigger_action") is False and rp.get("should_act") is False:
            for w in ("我来帮你", "我这就去", "我马上", "我去查"):
                if w in text:
                    res["violations"].append(f"未要求执行动作，回复却承诺了行动：{w}")
            res["checked"].append("should_act")

        res["ok"] = not res["violations"]
    except Exception as e:
        res["violations"].append(f"check 异常：{type(e).__name__}: {e}")
        res["ok"] = True   # 检查本身出错不算回复违规
    return res


def _append_to_system(messages: list, block: str) -> list:
    """把权威块追加到最后一条 system 消息上；没有 system 就插一条在最前。
    不改动原有内容，只在末尾追加。"""
    if not block or not messages:
        return messages
    try:
        for m in reversed(messages):
            if m.get("role") == "system":
                m["content"] = (str(m.get("content") or "") + "\n\n" + block)
                return messages
        messages.insert(0, {"role": "system", "content": block})
        return messages
    except Exception:
        return messages


async def analyze_and_inject(
    messages: list,
    user_text: str,
    *,
    session_id: str = "default",
    character_id: str = "default",
    character_name: str = "",
    key: str = "",
    model: str = "",
    base_url: str = None,
    is_internal: bool = False,
) -> list:
    """一步到位：解析意图 → 生成权威块 → 追加到 system。

    任何情况下都保证返回可用的 messages：解析失败就原样返回。

    ★ 完全自主模式（autonomy=full）：不注入理解层权威块——该模式的立场是
      「只留人设+记忆，其余全靠模型」，意图纠正也属于规则性引导。
      这里在**调用解析之前**直接返回，省掉一次纯浪费的 LLM 调用。
      （若要保留这次调用只为其它链路复用，把下面早退改成 `block = ""` 即可。）
    """
    try:
        from . import character_manager as _cm0
        if str((_cm0.get_character_any(character_id) or {}).get("autonomy") or "").strip() == "full":
            return messages
        from . import config as _cfg0
        if not character_id and _cfg0.autonomy_level() == "full":
            return messages
    except Exception:
        pass
    try:
        intent = await understand(
            user_text,
            messages,
            session_id=session_id,
            character_id=character_id,
            character_name=character_name,
            key=key,
            model=model,
            base_url=base_url,
            is_internal=is_internal,
        )
        if not intent:
            return messages
        _autonomy = ""
        try:
            from . import character_manager as _cm
            _autonomy = str((_cm.get_character_any(character_id) or {}).get("autonomy") or "").strip()
        except Exception:
            pass
        block = build_intent_block(intent, level=_autonomy)
        if not block:
            return messages
        return _append_to_system(messages, block)
    except Exception as e:
        try:
            print(f"[Understanding] 注入失败(静默): {type(e).__name__}: {e}", flush=True)
        except Exception:
            pass
        return messages
