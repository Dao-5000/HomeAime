# -*- coding:utf-8 -*-
"""
Semantic Understanding Foundation - Analyzer
核心语义分析器，用 LLM 替代关键词匹配
一次调用，产出完整 SemanticState

适配说明：使用项目的 deepseek_api.chat_once 而非 openai 库
"""
import json
import logging
import re
from typing import Optional, List, Dict

from .. import config
from ..deepseek_api import chat_once
from .schema import (
    SemanticState, Intent, EmotionState, EmotionValence,
    EmotionIntensity, TopicInfo, RelationshipSignal,
    TopicType, InteractionPattern, InteractionSignal,
    TopicLayer, InteractionLayer, RelationshipDynamic,
    NarrativeLayer, TopicContinuation
)

logger = logging.getLogger(__name__)


# ★ 极短语气词短路（2026-09-09，与 understanding._SHORT_CIRCUIT 同规则）：
#   纯符号/纯语气词（嗯/哦/哈哈…≤4字）没有可供分析的语义，直接返回默认状态，
#   省一次 glm 调用。刻意不含「好/是/对/行」（有真实语义：答应/认同，交给模型），
#   也不含带标点的「嗯？」（疑问），宁可漏判不误判。
_SHORT_RE = re.compile(
    r"^[\s\W_]*$"
    r"|"
    r"^[嗯哦噢喔啊呃咦唉哎诶欸唔昂哼嗨哈嘿]{1,4}$"
)


# ============================================================================
# ★ 2026-09-15 用户拍板（省 token ①）：CompanionOS 语义分析改**本地规则**，不再每条消息
#   打一次模型。原口径每条消息一次 chat_once（实测 2 天 271,931 in / 143,863 out token，
#   是机械抽取器里最贵的一个），而这些标签大多可以从用户原话里稳定地判出来。
#
#   为什么敢降级：这些字段的下游用途是「场景路由 / 情绪加权 / 是否要回忆」这类**轻决策**，
#   判错一条不会毁掉回复质量；而主脑（回复本身）完全不受影响 —— 主脑仍然拿到完整的人设、
#   记忆、最近对话。想回退：config.json 把 SEMANTIC_ANALYZER_MODE 设成 "llm" 即可。
#
#   诚实边界（写清楚，免得后面误判）：本地规则只认"出现即判"的词面证据，不做反讽/省略/
#   多轮指代的推断。查不到的字段保持默认值（unknown / neutral / 空列表），**不猜**。
# ============================================================================
SEMANTIC_MODE_LOCAL = "local"
SEMANTIC_MODE_LLM = "llm"
SEMANTIC_MODE_OFF = "off"

# 意图规则：**顺序即优先级**（越具体的放前面，CASUAL_CHAT 兜底）
_INTENT_RULES = (
    (Intent.FAREWELL, ("晚安", "睡了", "去睡", "睡觉去", "拜拜", "再见", "下线", "先撤",
                       "我走了", "出去了", "上班去", "上学去", "洗澡去")),
    (Intent.GREETING, ("早安", "早上好", "中午好", "下午好", "晚上好", "你好", "在吗", "在么",
                       "我回来了", "我醒啦", "起床了", "嗨")),
    (Intent.SEEK_COMFORT, ("难受", "不开心", "想哭", "委屈", "撑不住", "安慰我", "抱抱我",
                           "心烦", "好累", "崩溃", "emo", "别难过", "哄哄我")),
    (Intent.RELATIONSHIP_SIGNAL, ("喜欢你", "爱你", "想你", "亲亲", "么嘛", "抱抱", "在一起",
                                  "表白", "我的人", "老公", "老婆", "宝")),
    (Intent.TASK_REQUEST, ("帮我", "给我", "提醒我", "叫我", "定个", "设置", "打开", "关掉",
                           "发个", "唱", "讲个", "查一下", "搜索", "记一下", "记住", "设个")),
    (Intent.SEEK_ADVICE, ("怎么办", "要不要", "你觉得", "建议", "该不该", "好不好", "行不行",
                          "选哪个", "怎么选")),
    (Intent.COMPLAINT, ("讨厌", "烦死", "气死", "气人", "又这样", "受够", "受不了", "抱怨")),
    (Intent.PRAISE, ("好棒", "真厉害", "真乖", "好可爱", "太好了", "厉害", "优秀", "谢谢",
                     "感谢", "辛苦了")),
    (Intent.TEASE, ("哼", "笨蛋", "傻瓜", "坏人", "坏蛋", "讨厌鬼", "皮")),
    (Intent.EMOTIONAL_SHARE, ("开心", "高兴", "难过", "郁闷", "心累", "兴奋", "感动", "失落",
                              "焦虑", "紧张", "害怕")),
)
_QUESTION_RE = re.compile(r"[?？]$|(吗|呢|什么|为什么|为啥|怎么|哪|谁|几点|多少|是不是|有没有)")
_ENTITY_RE = re.compile(r"[《【\"'「]([^》】\"'」]{1,12})[》】\"'」]")


def _hit(text: str, words) -> list:
    return [w for w in words if w and w in text]


def _first_hit(text: str, words):
    for w in words:
        if w and w in text:
            return w
    return ""


# 情绪词典：valence / 主情绪 / 关键词
_EMO_RULES = (
    (EmotionValence.NEGATIVE, "tired", ("好累", "累死", "困", "熬夜", "没睡", "睡不着", "疲惫")),
    (EmotionValence.NEGATIVE, "sad", ("难过", "想哭", "哭", "委屈", "失落", "伤心", "emo")),
    (EmotionValence.NEGATIVE, "angry", ("生气", "气死", "气人", "烦死", "讨厌", "火大", "受够")),
    (EmotionValence.NEGATIVE, "anxious", ("焦虑", "紧张", "害怕", "担心", "压力", "慌")),
    (EmotionValence.NEGATIVE, "lonely", ("孤单", "一个人", "没人", "寂寞")),
    (EmotionValence.NEGATIVE, "sick", ("疼", "难受", "不舒服", "生病", "发烧")),
    (EmotionValence.POSITIVE, "loving", ("喜欢你", "爱你", "想你", "亲亲", "么嘛", "抱抱")),
    (EmotionValence.POSITIVE, "happy", ("开心", "高兴", "太好了", "爽", "哈哈", "嘿嘿", "嘿嘿嘿")),
    (EmotionValence.POSITIVE, "excited", ("兴奋", "激动", "期待", "终于")),
)
_INTENSITY_WORDS = ("非常", "特别", "超级", "超", "太", "死了", "爆", "巨", "好想", "！！", "!!")


def _local_emotion(text: str) -> EmotionState:
    hits = []
    for valence, emo, words in _EMO_RULES:
        if _hit(text, words):
            hits.append((valence, emo))
    if not hits:
        return EmotionState(EmotionValence.NEUTRAL, EmotionIntensity.LOW, "neutral",
                            confidence=0.3)
    pos = any(v == EmotionValence.POSITIVE for v, _ in hits)
    neg = any(v == EmotionValence.NEGATIVE for v, _ in hits)
    valence = (EmotionValence.MIXED if (pos and neg)
               else (EmotionValence.POSITIVE if pos else EmotionValence.NEGATIVE))
    intensity = EmotionIntensity.LOW
    if _hit(text, _INTENSITY_WORDS):
        intensity = EmotionIntensity.HIGH
    elif len(text) >= 12 or text.count("！") + text.count("!") >= 1:
        intensity = EmotionIntensity.MEDIUM
    return EmotionState(valence, intensity, hits[0][1],
                        secondary_emotions=[e for _, e in hits[1:3]], confidence=0.55)


def _local_topic_continuation(text: str):
    """本地版"结构化话题"识别：只在**明确词面**命中时构建，否则返回 None（不猜）。

    真实聊天里这类话题有很强的话术特征：约定/说好/答应/规则/惩罚/奖励/亲亲/么嘛，
    所以本地规则够用；识别不到时保持 None，CompanionOS 按普通闲聊处理。
    """
    rule_words = ("约定", "说好", "说好了", "答应", "规则", "惩罚", "奖励", "不许", "必须",
                  "拉勾", "打勾", "打卡")
    intimate_words = ("亲亲", "么嘛", "抱抱", "亲一下", "摸摸头", "蹭蹭")
    if not (_hit(text, rule_words) or _hit(text, intimate_words)):
        return None
    signals = []
    topic_type = TopicType.NONE
    if _hit(text, rule_words):
        topic_type = TopicType.COMMITMENT
        signals.append(InteractionSignal.RULE_MAKING.value)
        if _hit(text, ("说好", "答应", "拉勾")):
            signals.append(InteractionSignal.PROMISE_MAKING.value)
        if _hit(text, ("惩罚", "奖励")):
            signals.append(InteractionSignal.REWARD.value)
    else:
        topic_type = TopicType.INTIMATE_PLAY
        signals.append(InteractionSignal.ACTION_REQUEST.value)
    asking_action = bool(_hit(text, ("好不好", "好不好嘛", "快", "现在", "该你了", "轮到")))
    tc = TopicContinuation(
        topic=TopicLayer(topic_type=topic_type,
                         topic_name=_first_hit(text, rule_words + intimate_words)[:12],
                         main_entities=_hit(text, intimate_words)[:3],
                         is_explicitly_started=bool(_hit(text, ("说好", "约定", "拉勾", "从现在开始"))),
                         is_explicitly_ended=bool(_hit(text, ("不玩了", "结束", "取消", "算了")))),
        interaction=InteractionLayer(
            pattern=(InteractionPattern.COMPLIANCE_CHECK if asking_action
                     else InteractionPattern.RULE_NEGOTIATION),
            rules=_hit(text, rule_words)[:3],
            current_phase="rule_reminder",
            expected_ai_action=("按约定回应用户此刻的要求" if asking_action else ""),
            escalation_potential=0.4 if asking_action else 0.2),
        relationship=RelationshipDynamic(power_dynamic="user_leads" if asking_action else "equal",
                                         intimacy_direction="increasing",
                                         emotional_stakes="medium",
                                         boundary_sensitivity="high"),
        is_continuing=True,
        should_perform_action=asking_action,
        mood=("flirty" if _hit(text, intimate_words) else "playful"),
        signals=signals or [InteractionSignal.NONE.value],
    )
    return tc


ANALYSIS_PROMPT = """你是一个语义分析引擎，负责分析用户消息并返回结构化JSON。

【分析目标】
输入：用户的一条消息（可能包含对话历史）
输出：严格的JSON结构，包含意图、情绪、话题、关系信号、话题延续等

【JSON输出格式】
{
  "intent": "<意图标签>",
  "intent_confidence": <0.0~1.0>,
  "emotion": {
    "valence": "<positive|negative|neutral|mixed>",
    "intensity": "<low|medium|high>",
    "primary_emotion": "<具体情绪词，英文>",
    "secondary_emotions": ["<情绪词>"],
    "confidence": <0.0~1.0>
  },
  "topic": {
    "main_topic": "<主话题>",
    "sub_topics": ["<子话题>"],
    "entities": ["<命名实体>"],
    "is_sensitive": <true|false>,
    "requires_memory": <true|false>
  },
  "relationship": {
    "intimacy_delta": <-1.0~1.0>,
    "trust_delta": <-1.0~1.0>,
    "signal_type": "<flirt|conflict|gratitude|complaint|null>",
    "is_milestone": <true|false>
  },
  "references_previous": <true|false>,
  "requires_proactive": <true|false>,
  "urgency": <0.0~1.0>,
  "inferred_user_state": {
    "tired": <true|false>,
    "stressed": <true|false>,
    "happy": <true|false>,
    "lonely": <true|false>,
    "location_hint": "<home|work|outside|unknown>"
  },
  "topic_continuation": {
    "topic": {
      "topic_type": "<none|intimate_play|commitment|roleplay|emotional_loop|challenge|planning|co_creation>",
      "topic_name": "<话题名称，例如'晚安亲亲约定'>",
      "main_entities": ["<关键词>"],
      "is_explicitly_started": <true|false>,
      "is_explicitly_ended": <true|false>
    },
    "interaction": {
      "pattern": "<none|turn_taking|reward_and_punishment|escalation|teasing_loop|compliance_check|rule_negotiation|promise_fulfillment|farewell_and_return>",
      "roles": {"user_role": "<用户角色>", "ai_role": "<AI角色>"},
      "rules": ["<已约定规则1>", "<规则2>"],
      "current_phase": "<当前阶段，例如rule_reminder/reward/punishment/promise_fulfillment>",
      "expected_ai_action": "<当前应立即执行的动作描述>",
      "escalation_potential": <0.0~1.0>
    },
    "relationship": {
      "power_dynamic": "<user_leads|ai_leads|equal>",
      "intimacy_direction": "<increasing|stable|decreasing>",
      "emotional_stakes": "<high|medium|low>",
      "trust_required": <true|false>,
      "boundary_sensitivity": "<high|medium|low>"
    },
    "narrative": {
      "arc_name": "<共同故事弧线名，例如'我们的甜蜜约定'>",
      "current_chapter": "<当前章节，例如'规则强化期'>",
      "shared_lore": ["<共享梗1>", "<共享梗2>"],
      "callback_opportunities": ["<可自然提起的话头1>"]
    },
    "is_continuing": <true|false>,
    "should_perform_action": <true|false>,
    "suggested_action": "<简洁动作建议>",
    "mood": "<flirty|playful|serious|tender|none>",
    "signals": ["<rule_making|rule_checking|reward|punishment|promise_making|promise_checking|action_request|topic_drop|none>"]
  }
}

【意图标签说明】
greeting, farewell, emotional_share, seek_comfort, seek_advice,
casual_chat, task_request, question, complaint, praise, tease,
self_disclosure, relationship_signal, unknown

【话题延续分析规则】
1. topic_type 必须精确识别：普通闲聊用 none；任何带规则/奖惩/约定的互动用相应类型。
2. 如果用户在延续之前的亲密/约定/角色扮演话题，is_continuing 为 true。
3. 如果用户要求 AI 执行约定动作（如“亲亲要说么嘛”“答错罚亲亲”），should_perform_action 为 true，expected_ai_action 写清楚具体动作。
4. rules 只记录当前已约定的规则，不要编造。
5. signals 可多选：规则制定/检查/奖励/惩罚/立约定/提醒履约/要求动作/结束话题。
6. mood 要贴合当前氛围：flirty（暧昧）、playful（俏皮）、tender（温柔）、serious（认真）。

【重要规则】
1. 只返回JSON，不要任何解释或markdown
2. 所有字段必须存在，不可省略
3. 基于语义理解，而非关键词匹配
4. 同样的意思用不同说法表达，结果应该一致
"""


class SemanticAnalyzer:
    """
    语义分析器

    设计原则：
    - 单次LLM调用，一次产出完整 SemanticState
    - 带缓存，相同输入不重复调用
    - 失败时返回安全默认值，不影响主流程
    - 适配项目的 deepseek_api.chat_once
    """

    def __init__(
        self,
        model: str = None,
        enable_cache: bool = True,
        cache_max_size: int = 100
    ):
        self.model = model or config.get("MEMORY_EXTRACT_MODEL") or "deepseek-chat"
        self.enable_cache = enable_cache
        self._cache: Dict[str, SemanticState] = {}
        self._cache_keys: List[str] = []  # 用于LRU淘汰
        self._cache_max_size = cache_max_size

    def _make_cache_key(self, message: str, context: Optional[str]) -> str:
        ctx_part = context[:100] if context else ""
        return f"{message[:200]}|||{ctx_part}"

    def _local_state(self, message: str) -> SemanticState:
        """★ 2026-09-15：本地规则版语义状态（不花一分钱 token）。

        判据全部来自用户原话的词面证据；查不到就保持默认值，不做推断也不编造。
        置信度统一给中等（0.5~0.6）—— 让下游知道"这是规则判的，不是模型判的"。
        """
        text = str(message or "")
        intent, confidence = Intent.CASUAL_CHAT, 0.4
        for _intent, _words in _INTENT_RULES:
            if _hit(text, _words):
                intent, confidence = _intent, 0.6
                break
        else:
            if _QUESTION_RE.search(text):
                intent, confidence = Intent.QUESTION, 0.55
            elif _hit(text, ("我", "我的")) and len(text) >= 6:
                intent, confidence = Intent.SELF_DISCLOSURE, 0.45

        _topic_words = (("工作", "上班", "加班", "老板"), ("游戏", "打游戏", "开黑"),
                        ("吃饭", "吃", "饿", "外卖"), ("睡觉", "睡", "困", "熬夜"),
                        ("学习", "考试", "作业", "上课"), ("代码", "bug", "程序", "项目"),
                        ("语音", "通话", "打电话"), ("记忆", "聊天记录"))
        main_topic = "general"
        for _ws in _topic_words:
            if _hit(text, _ws):
                main_topic = _ws[0]
                break

        rel_signal, rel_delta = None, 0.0
        if _hit(text, ("喜欢你", "爱你", "想你", "亲亲", "么嘛", "抱抱", "在一起")):
            rel_signal, rel_delta = "flirt", 0.1
        elif _hit(text, ("谢谢", "感谢", "辛苦了")):
            rel_signal, rel_delta = "gratitude", 0.1
        elif _hit(text, ("生气", "讨厌", "别理我", "吵架", "分手")):
            rel_signal, rel_delta = "conflict", -0.1
        elif _hit(text, ("烦", "抱怨", "又这样")):
            rel_signal, rel_delta = "complaint", -0.05

        user_state = {
            "tired": bool(_hit(text, ("累", "困", "熬夜", "没睡"))),
            "stressed": bool(_hit(text, ("压力", "忙", "赶", "烦", "焦虑"))),
            "happy": bool(_hit(text, ("开心", "高兴", "太好了", "爽"))),
            "lonely": bool(_hit(text, ("孤单", "一个人", "没人"))),
            "location_hint": ("home" if _hit(text, ("家", "回家", "床上"))
                              else "work" if _hit(text, ("公司", "上班", "单位"))
                              else "outside" if _hit(text, ("外面", "路上", "出门"))
                              else "unknown"),
        }

        return SemanticState(
            raw_message=text,
            intent=intent,
            intent_confidence=confidence,
            emotion=_local_emotion(text),
            topic=TopicInfo(
                main_topic=main_topic,
                entities=[m for m in _ENTITY_RE.findall(text)][:3],
                is_sensitive=bool(_hit(text, ("自杀", "自残", "不想活"))),
                # 只有明确的"回忆指代词"才置位，避免让记忆层无谓加班
                requires_memory=bool(_hit(text, ("还记得", "上次", "以前", "之前", "那天",
                                                 "你说过", "记不记得"))),
            ),
            relationship=RelationshipSignal(intimacy_delta=rel_delta,
                                            trust_delta=(0.05 if rel_signal == "gratitude" else 0.0),
                                            signal_type=rel_signal,
                                            is_milestone=bool(_hit(text, ("第一次", "纪念日", "一周年")))),
            references_previous=bool(_hit(text, ("上次", "之前", "你说过", "那天", "刚刚", "前面"))),
            # ★ 实测补充："帮我定个闹钟"这类**定时类任务**也是要 AI 主动到点执行的，
            #   原词表只认"提醒我/叫我"，会把它漏成 requires_proactive=False。
            requires_proactive=bool(_hit(text, ("提醒我", "叫我", "记得叫", "记得提醒",
                                                "闹钟", "定时", "到点", "定个"))),
            urgency=(0.8 if _hit(text, ("紧急", "马上", "现在就", "快点")) else 0.2),
            inferred_user_state=user_state,
            topic_continuation=_local_topic_continuation(text),
            analysis_version="2.1-local",
            raw_llm_response=None,
        )

    @staticmethod
    def _mode() -> str:
        """语义分析模式：local（默认，本地规则）/ llm（旧口径）/ off（直接给默认态）。"""
        try:
            _m = str(config.get("SEMANTIC_ANALYZER_MODE") or SEMANTIC_MODE_LOCAL).strip().lower()
        except Exception:
            _m = SEMANTIC_MODE_LOCAL
        return _m if _m in (SEMANTIC_MODE_LOCAL, SEMANTIC_MODE_LLM, SEMANTIC_MODE_OFF) \
            else SEMANTIC_MODE_LOCAL

    async def analyze(
        self,
        message: str,
        conversation_context: Optional[str] = None
    ) -> SemanticState:
        """
        分析用户消息，返回 SemanticState

        Args:
            message: 用户当前消息
            conversation_context: 最近几轮对话（可选，提高准确性）

        Returns:
            SemanticState: 结构化语义结果
        """
        if not message or not message.strip():
            return self._default_state(message)

        _mode = self._mode()
        if _mode == SEMANTIC_MODE_OFF:
            return self._default_state(message)
        if _mode == SEMANTIC_MODE_LOCAL:
            # ★ 极短语气词先短路（同旧口径），其余走本地规则，**不发起任何模型调用**
            if _SHORT_RE.match(message.strip()):
                return self._local_state(message)
            state_local = self._local_state(message)
            if self.enable_cache:
                self._write_cache(self._make_cache_key(message, conversation_context), state_local)
            return state_local

        # ── 以下为旧口径（SEMANTIC_ANALYZER_MODE=llm）：一次 LLM 调用

        # 缓存检查（★ 注意：prompt 版本升级后缓存内容格式可能不兼容，
        # 这里用 analysis_version 作为 key 前缀，天然让旧缓存失效）
        cache_key = self._make_cache_key(message, conversation_context)
        if self.enable_cache and cache_key in self._cache:
            logger.debug("SemanticAnalyzer: cache hit")
            return self._cache[cache_key]

        # 检查API key
        # ★ 修复（2026-09-09）：key 必须按「当前模型」的 provider 取——
        #   MEMORY_EXTRACT_MODEL 配了 glm-5.3-flash（智谱）时，memory_key() 返回的是
        #   别家的 key（gemini 中转）→ 智谱 401「令牌已过期」→ 语义分析永远静默降级
        #   （intent=unknown emotion=neutral，CompanionOS 的"懂你"层全失效）。
        key = config.api_key_for_model(self.model) or config.memory_key()
        if not key:
            return self._default_state(message)

        # 构建用户输入
        user_content = f"【当前消息】\n{message}"
        if conversation_context:
            user_content = f"【最近对话】\n{conversation_context}\n\n{user_content}"

        try:
            raw = await chat_once(
                self.model,
                [
                    {"role": "system", "content": ANALYSIS_PROMPT},
                    {"role": "user", "content": user_content}
                ],
                key,
                temperature=0.1,       # 低温，保证稳定性
                # ★ 2026-09-09：1024 不够——GLM 系是推理模型，reasoning_content 和
                #   JSON 输出共享 max_tokens，实测 topic_continuation 的长 JSON 会被
                #   截断（"Expecting ',' delimiter"）→ 解析失败 → 语义层白跑降级。
                max_tokens=2048,
                # ★ 修复（2026-09-04）：glm-5.3-flash 是推理模型，不传 reasoning_effort 时
                #   会输出超长 reasoning_content 占满 max_tokens，导致 content 为空 →
                #   parse error "Expecting value: line 1 column 1"。传 low 让它 reasoning 变短，
                #   content 正常输出 JSON。其它 provider 自动不传（返回 None）。
                reasoning_effort=("low" if config.model_supports_reasoning_effort(self.model) else None),
            )

            state = self._parse_response(message, raw)

            # 写缓存（LRU）
            if self.enable_cache:
                self._write_cache(cache_key, state)

            return state

        except Exception as e:
            logger.error(f"SemanticAnalyzer error: {e}")
            # ★ 401 = Key 失效。后端 config 里可能存着一个过期 Key，而 api_key() 中
            #   运行时 Key 优先级最低，于是永远用不上前端传来的有效 Key ——
            #   语义分析会一直静默降级（表现为"AI 听不懂你在说什么"，且没有任何提示）。
            #   这里拿到 401 时用运行时 Key 重试一次。
            if "401" in str(e):
                rt = ""
                try:
                    rt = config.runtime_api_key()
                except Exception:
                    rt = ""
                if rt and rt != key:
                    try:
                        raw2 = await chat_once(
                            self.model,
                            [
                                {"role": "system", "content": ANALYSIS_PROMPT},
                                {"role": "user", "content": user_content},
                            ],
                            rt,
                            temperature=0.1,
                            max_tokens=1024,
                        )
                        state2 = self._parse_response(message, raw2)
                        if self.enable_cache:
                            self._write_cache(cache_key, state2)
                        logger.info("SemanticAnalyzer: 运行时Key重试成功")
                        return state2
                    except Exception as e2:
                        logger.error(f"SemanticAnalyzer 运行时Key重试失败: {e2}")
            return self._default_state(message)

    def _parse_response(self, raw_message: str, raw_json: str) -> SemanticState:
        """解析LLM返回的JSON → SemanticState"""
        try:
            # 清理可能的 markdown 标记
            cleaned = str(raw_json or "").strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.strip("`")
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()

            data = json.loads(cleaned)

            emotion_data = data.get("emotion", {})
            topic_data = data.get("topic", {})
            rel_data = data.get("relationship", {})
            user_state_data = data.get("inferred_user_state", {})
            tc_data = data.get("topic_continuation", {}) or {}

            return SemanticState(
                raw_message=raw_message,
                intent=Intent(data.get("intent", "unknown")),
                intent_confidence=float(data.get("intent_confidence", 0.5)),
                emotion=EmotionState(
                    valence=EmotionValence(emotion_data.get("valence", "neutral")),
                    intensity=EmotionIntensity(emotion_data.get("intensity", "low")),
                    primary_emotion=emotion_data.get("primary_emotion", "neutral"),
                    secondary_emotions=emotion_data.get("secondary_emotions", []),
                    confidence=float(emotion_data.get("confidence", 0.5))
                ),
                topic=TopicInfo(
                    main_topic=topic_data.get("main_topic", "general"),
                    sub_topics=topic_data.get("sub_topics", []),
                    entities=topic_data.get("entities", []),
                    is_sensitive=bool(topic_data.get("is_sensitive", False)),
                    requires_memory=bool(topic_data.get("requires_memory", False))
                ),
                relationship=RelationshipSignal(
                    intimacy_delta=float(rel_data.get("intimacy_delta", 0.0)),
                    trust_delta=float(rel_data.get("trust_delta", 0.0)),
                    signal_type=rel_data.get("signal_type"),
                    is_milestone=bool(rel_data.get("is_milestone", False))
                ),
                references_previous=bool(data.get("references_previous", False)),
                requires_proactive=bool(data.get("requires_proactive", False)),
                urgency=float(data.get("urgency", 0.0)),
                inferred_user_state={
                    "tired": bool(user_state_data.get("tired", False)),
                    "stressed": bool(user_state_data.get("stressed", False)),
                    "happy": bool(user_state_data.get("happy", False)),
                    "lonely": bool(user_state_data.get("lonely", False)),
                    "location_hint": user_state_data.get("location_hint", "unknown")
                },
                topic_continuation=self._parse_topic_continuation(tc_data),
                analysis_version="2.0",
                raw_llm_response=raw_json
            )

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(f"SemanticAnalyzer parse error: {e}, raw: {str(raw_json)[:200]}")
            return self._default_state(raw_message)

    def _parse_topic_continuation(self, data: Dict) -> Optional[TopicContinuation]:
        """解析 topic_continuation JSON 部分"""
        if not data or not isinstance(data, dict):
            return None

        try:
            topic = data.get("topic", {}) or {}
            interaction = data.get("interaction", {}) or {}
            relationship = data.get("relationship", {}) or {}
            narrative = data.get("narrative", {}) or {}

            def _as_enum(enum_cls, value, default):
                if not value:
                    return default
                try:
                    return enum_cls(value)
                except ValueError:
                    return default

            topic_layer = TopicLayer(
                topic_type=_as_enum(TopicType, topic.get("topic_type"), TopicType.NONE),
                topic_name=topic.get("topic_name", ""),
                main_entities=topic.get("main_entities", []),
                is_explicitly_started=bool(topic.get("is_explicitly_started", False)),
                is_explicitly_ended=bool(topic.get("is_explicitly_ended", False)),
            )

            interaction_layer = InteractionLayer(
                pattern=_as_enum(InteractionPattern, interaction.get("pattern"), InteractionPattern.NONE),
                roles=interaction.get("roles", {}),
                rules=interaction.get("rules", []),
                current_phase=interaction.get("current_phase", ""),
                expected_ai_action=interaction.get("expected_ai_action", ""),
                escalation_potential=float(interaction.get("escalation_potential", 0.0) or 0.0),
            )

            relationship_layer = RelationshipDynamic(
                power_dynamic=relationship.get("power_dynamic", ""),
                intimacy_direction=relationship.get("intimacy_direction", ""),
                emotional_stakes=relationship.get("emotional_stakes", ""),
                trust_required=bool(relationship.get("trust_required", False)),
                boundary_sensitivity=relationship.get("boundary_sensitivity", ""),
            )

            narrative_layer = NarrativeLayer(
                arc_name=narrative.get("arc_name", ""),
                current_chapter=narrative.get("current_chapter", ""),
                shared_lore=narrative.get("shared_lore", []),
                callback_opportunities=narrative.get("callback_opportunities", []),
            )

            signals = data.get("signals", [])
            signals = [s for s in signals if isinstance(s, str)]

            return TopicContinuation(
                topic=topic_layer,
                interaction=interaction_layer,
                relationship=relationship_layer,
                narrative=narrative_layer,
                is_continuing=bool(data.get("is_continuing", False)),
                should_perform_action=bool(data.get("should_perform_action", False)),
                suggested_action=data.get("suggested_action", ""),
                mood=data.get("mood", ""),
                signals=signals,
            )
        except Exception as e:
            logger.warning(f"SemanticAnalyzer topic_continuation parse error: {e}")
            return None

    def _default_state(self, message: str) -> SemanticState:
        """分析失败时的安全默认值"""
        return SemanticState(
            raw_message=message,
            intent=Intent.UNKNOWN,
            intent_confidence=0.0,
            emotion=EmotionState(
                valence=EmotionValence.NEUTRAL,
                intensity=EmotionIntensity.LOW,
                primary_emotion="neutral",
                confidence=0.0
            ),
            topic=TopicInfo(main_topic="general"),
            relationship=RelationshipSignal(),
            topic_continuation=None,
            analysis_version="2.0",
        )

    def _write_cache(self, key: str, state: SemanticState):
        """LRU缓存写入"""
        if key in self._cache:
            self._cache_keys.remove(key)
        elif len(self._cache_keys) >= self._cache_max_size:
            oldest = self._cache_keys.pop(0)
            self._cache.pop(oldest, None)
        self._cache[key] = state
        self._cache_keys.append(key)

    def clear_cache(self):
        """清空缓存"""
        self._cache.clear()
        self._cache_keys.clear()
