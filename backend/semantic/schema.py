# -*- coding:utf-8 -*-
"""
Semantic Understanding Foundation - Schema
统一语义结果结构，所有模块从这里读取，而不是自己做关键词匹配
"""
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum


class Intent(str, Enum):
    """用户意图分类"""
    GREETING = "greeting"               # 打招呼
    FAREWELL = "farewell"               # 告别
    EMOTIONAL_SHARE = "emotional_share" # 分享情绪/心情
    SEEK_COMFORT = "seek_comfort"       # 寻求安慰
    SEEK_ADVICE = "seek_advice"         # 寻求建议
    CASUAL_CHAT = "casual_chat"         # 闲聊
    TASK_REQUEST = "task_request"       # 任务请求
    QUESTION = "question"               # 提问
    COMPLAINT = "complaint"             # 抱怨
    PRAISE = "praise"                   # 称赞
    TEASE = "tease"                     # 调侃/撒娇
    SELF_DISCLOSURE = "self_disclosure" # 自我披露（分享个人信息）
    RELATIONSHIP_SIGNAL = "relationship_signal"  # 关系信号
    UNKNOWN = "unknown"


class TopicType(str, Enum):
    """结构化互动话题大类"""
    NONE = "none"                       # 普通闲聊
    INTIMATE_PLAY = "intimate_play"     # 亲密/打趣小游戏
    COMMITMENT = "commitment"           # 约定/承诺/打卡
    ROLEPLAY = "roleplay"               # 角色扮演
    EMOTIONAL_LOOP = "emotional_loop"   # 情绪互哄
    CHALLENGE = "challenge"             # 问答/挑战
    PLANNING = "planning"               # 计划/安排
    CO_CREATION = "co_creation"         # 共创


class InteractionPattern(str, Enum):
    """互动模式"""
    NONE = "none"
    TURN_TAKING = "turn_taking"                     # 轮流做某事
    REWARD_AND_PUNISHMENT = "reward_and_punishment" # 奖惩机制
    ESCALATION = "escalation"                       # 逐步升级
    TEASING_LOOP = "teasing_loop"                   # 互相逗
    COMPLIANCE_CHECK = "compliance_check"           # 检查履约
    RULE_NEGOTIATION = "rule_negotiation"           # 规则协商
    PROMISE_FULFILLMENT = "promise_fulfillment"     # 履约/兑现承诺
    FAREWELL_AND_RETURN = "farewell_and_return"     # 暂时离开再回来


class InteractionSignal(str, Enum):
    """互动细粒度信号"""
    RULE_MAKING = "rule_making"             # 制定规则
    RULE_CHECKING = "rule_checking"         # 检查规则
    REWARD = "reward"                       # 奖励
    PUNISHMENT = "punishment"               # 惩罚
    PROMISE_MAKING = "promise_making"       # 立约定
    PROMISE_CHECKING = "promise_checking" # 提醒履约
    ACTION_REQUEST = "action_request"       # 要求 AI 做动作
    TOPIC_DROP = "topic_drop"               # 用户想结束话题
    NONE = "none"


class EmotionValence(str, Enum):
    """情绪效价"""
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class EmotionIntensity(str, Enum):
    """情绪强度"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class EmotionState:
    """情绪状态"""
    valence: EmotionValence = EmotionValence.NEUTRAL
    intensity: EmotionIntensity = EmotionIntensity.LOW
    primary_emotion: str = "neutral"       # 主情绪：happy, sad, anxious, excited...
    secondary_emotions: List[str] = field(default_factory=list)
    confidence: float = 0.0               # 分析置信度 0~1


@dataclass
class TopicInfo:
    """话题信息"""
    main_topic: str = "general"           # 主话题
    sub_topics: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)  # 命名实体（人名、地点、事件）
    is_sensitive: bool = False            # 是否敏感话题
    requires_memory: bool = False         # 是否需要查历史记忆


@dataclass
class RelationshipSignal:
    """关系信号"""
    intimacy_delta: float = 0.0           # 亲密度变化 -1.0 ~ +1.0
    trust_delta: float = 0.0             # 信任度变化
    signal_type: Optional[str] = None    # 信号类型：flirt, conflict, gratitude...
    is_milestone: bool = False           # 是否关系里程碑事件


@dataclass
class TopicLayer:
    """话题层：识别当前结构化话题"""
    topic_type: TopicType = TopicType.NONE
    topic_name: str = ""
    main_entities: List[str] = field(default_factory=list)
    is_explicitly_started: bool = False
    is_explicitly_ended: bool = False


@dataclass
class InteractionLayer:
    """互动层：识别互动模式、规则、阶段、AI 应执行动作"""
    pattern: InteractionPattern = InteractionPattern.NONE
    roles: Dict[str, str] = field(default_factory=dict)
    rules: List[str] = field(default_factory=list)
    current_phase: str = ""       # 例如 rule_reminder / reward / punishment / promise_fulfillment
    expected_ai_action: str = ""  # 当前应立即执行的动作描述
    escalation_potential: float = 0.0  # 0~1，升级潜力


@dataclass
class RelationshipDynamic:
    """关系层：互动中的权力、亲密、信任动态"""
    power_dynamic: str = ""       # user_leads / ai_leads / equal
    intimacy_direction: str = ""  # increasing / stable / decreasing
    emotional_stakes: str = ""     # high / medium / low
    trust_required: bool = False
    boundary_sensitivity: str = "" # high / medium / low


@dataclass
class NarrativeLayer:
    """叙事层：共同故事弧线、章节、可回调梗"""
    arc_name: str = ""
    current_chapter: str = ""
    shared_lore: List[str] = field(default_factory=list)
    callback_opportunities: List[str] = field(default_factory=list)


@dataclass
class TopicContinuation:
    """话题延续分析结果：四层 + 行动建议"""
    topic: TopicLayer = field(default_factory=TopicLayer)
    interaction: InteractionLayer = field(default_factory=InteractionLayer)
    relationship: RelationshipDynamic = field(default_factory=RelationshipDynamic)
    narrative: NarrativeLayer = field(default_factory=NarrativeLayer)

    # 顶层快捷信号
    is_continuing: bool = False
    should_perform_action: bool = False
    suggested_action: str = ""   # 例如"用'么嘛'亲一下并提醒惩罚规则"
    mood: str = ""               # 例如 flirty / playful / serious / tender
    signals: List[str] = field(default_factory=list)  # InteractionSignal 值列表

    def is_active(self) -> bool:
        return self.is_continuing or self.topic.topic_type != TopicType.NONE

    def requires_action(self) -> bool:
        return self.should_perform_action or bool(self.interaction.expected_ai_action)


@dataclass
class SemanticState:
    """
    统一语义状态 — 所有模块的输入来源
    由 SemanticAnalyzer 生成，注入 CompanionOS
    """
    # 原始输入
    raw_message: str = ""

    # 核心语义
    intent: Intent = Intent.UNKNOWN
    intent_confidence: float = 0.0

    # 情绪
    emotion: EmotionState = field(default_factory=EmotionState)

    # 话题
    topic: TopicInfo = field(default_factory=TopicInfo)

    # 关系信号
    relationship: RelationshipSignal = field(default_factory=RelationshipSignal)

    # 上下文感知
    references_previous: bool = False     # 是否引用了之前的对话
    requires_proactive: bool = False      # 是否需要主动行为
    urgency: float = 0.0                  # 紧急程度 0~1

    # 用户当前状态推断
    inferred_user_state: Dict[str, Any] = field(default_factory=dict)
    # 例如: {"tired": True, "stressed": True, "location_hint": "office"}

    # ★ 话题延续分析（新增）
    topic_continuation: Optional[TopicContinuation] = None

    # 元信息
    analysis_version: str = "2.0"
    raw_llm_response: Optional[str] = None  # 调试用，保留原始LLM输出

    def is_emotionally_negative(self) -> bool:
        return self.emotion.valence in (EmotionValence.NEGATIVE, EmotionValence.MIXED)

    def needs_emotional_support(self) -> bool:
        return (
            self.intent in (Intent.SEEK_COMFORT, Intent.EMOTIONAL_SHARE, Intent.COMPLAINT)
            and self.emotion.valence != EmotionValence.POSITIVE
        )

    def is_relationship_meaningful(self) -> bool:
        return (
            abs(self.relationship.intimacy_delta) > 0.1
            or self.relationship.is_milestone
            or (self.topic_continuation is not None and self.topic_continuation.is_active())
        )

    def to_dict(self) -> dict:
        """转换为字典（用于序列化和调试）"""
        tc = self.topic_continuation
        tc_dict = None
        if tc:
            tc_dict = {
                "topic": {
                    "topic_type": tc.topic.topic_type.value if isinstance(tc.topic.topic_type, TopicType) else tc.topic.topic_type,
                    "topic_name": tc.topic.topic_name,
                    "main_entities": tc.topic.main_entities,
                    "is_explicitly_started": tc.topic.is_explicitly_started,
                    "is_explicitly_ended": tc.topic.is_explicitly_ended,
                },
                "interaction": {
                    "pattern": tc.interaction.pattern.value if isinstance(tc.interaction.pattern, InteractionPattern) else tc.interaction.pattern,
                    "roles": tc.interaction.roles,
                    "rules": tc.interaction.rules,
                    "current_phase": tc.interaction.current_phase,
                    "expected_ai_action": tc.interaction.expected_ai_action,
                    "escalation_potential": tc.interaction.escalation_potential,
                },
                "relationship": {
                    "power_dynamic": tc.relationship.power_dynamic,
                    "intimacy_direction": tc.relationship.intimacy_direction,
                    "emotional_stakes": tc.relationship.emotional_stakes,
                    "trust_required": tc.relationship.trust_required,
                    "boundary_sensitivity": tc.relationship.boundary_sensitivity,
                },
                "narrative": {
                    "arc_name": tc.narrative.arc_name,
                    "current_chapter": tc.narrative.current_chapter,
                    "shared_lore": tc.narrative.shared_lore,
                    "callback_opportunities": tc.narrative.callback_opportunities,
                },
                "is_continuing": tc.is_continuing,
                "should_perform_action": tc.should_perform_action,
                "suggested_action": tc.suggested_action,
                "mood": tc.mood,
                "signals": tc.signals,
            }
        return {
            "raw_message": self.raw_message,
            "intent": self.intent.value if isinstance(self.intent, Intent) else self.intent,
            "intent_confidence": self.intent_confidence,
            "emotion": {
                "valence": self.emotion.valence.value if isinstance(self.emotion.valence, EmotionValence) else self.emotion.valence,
                "intensity": self.emotion.intensity.value if isinstance(self.emotion.intensity, EmotionIntensity) else self.emotion.intensity,
                "primary_emotion": self.emotion.primary_emotion,
                "secondary_emotions": self.emotion.secondary_emotions,
                "confidence": self.emotion.confidence,
            },
            "topic": {
                "main_topic": self.topic.main_topic,
                "sub_topics": self.topic.sub_topics,
                "entities": self.topic.entities,
                "is_sensitive": self.topic.is_sensitive,
                "requires_memory": self.topic.requires_memory,
            },
            "relationship": {
                "intimacy_delta": self.relationship.intimacy_delta,
                "trust_delta": self.relationship.trust_delta,
                "signal_type": self.relationship.signal_type,
                "is_milestone": self.relationship.is_milestone,
            },
            "references_previous": self.references_previous,
            "requires_proactive": self.requires_proactive,
            "urgency": self.urgency,
            "inferred_user_state": self.inferred_user_state,
            "topic_continuation": tc_dict,
            "analysis_version": self.analysis_version,
        }
