# -*- coding:utf-8 -*-
"""
Semantic Understanding Foundation - Adapter
渐进迁移适配器
让旧模块无缝使用 SemanticState，不需要一次性重写所有模块
"""
from .schema import SemanticState, Intent, EmotionValence


class SemanticAdapter:
    """
    为旧模块提供兼容层

    旧模块原来做：if "难过" in message or "伤心" in message
    现在做：if adapter.is_sad(state)

    一步步迁移，不影响线上运行
    """

    @staticmethod
    def is_greeting(state: SemanticState) -> bool:
        return state.intent == Intent.GREETING

    @staticmethod
    def is_farewell(state: SemanticState) -> bool:
        return state.intent == Intent.FAREWELL

    @staticmethod
    def is_sad(state: SemanticState) -> bool:
        return (
            state.emotion.valence == EmotionValence.NEGATIVE
            and state.emotion.primary_emotion in (
                "sad", "depressed", "melancholy", "disappointed", "hurt"
            )
        )

    @staticmethod
    def is_happy(state: SemanticState) -> bool:
        return state.emotion.valence == EmotionValence.POSITIVE

    @staticmethod
    def is_anxious(state: SemanticState) -> bool:
        return state.emotion.primary_emotion in ("anxious", "worried", "nervous", "stressed")

    @staticmethod
    def wants_comfort(state: SemanticState) -> bool:
        return state.needs_emotional_support()

    @staticmethod
    def is_flirting(state: SemanticState) -> bool:
        return (
            state.intent == Intent.TEASE
            or state.relationship.signal_type == "flirt"
        )

    @staticmethod
    def is_complaining(state: SemanticState) -> bool:
        return state.intent == Intent.COMPLAINT

    @staticmethod
    def is_asking_question(state: SemanticState) -> bool:
        return state.intent == Intent.QUESTION

    @staticmethod
    def get_intimacy_change(state: SemanticState) -> float:
        return state.relationship.intimacy_delta

    @staticmethod
    def get_emotion_label(state: SemanticState) -> str:
        """给旧模块返回情绪字符串，保持接口兼容"""
        return state.emotion.primary_emotion

    @staticmethod
    def get_topic(state: SemanticState) -> str:
        return state.topic.main_topic

    @staticmethod
    def is_user_tired(state: SemanticState) -> bool:
        return state.inferred_user_state.get("tired", False)

    @staticmethod
    def is_user_stressed(state: SemanticState) -> bool:
        return state.inferred_user_state.get("stressed", False)

    @staticmethod
    def is_user_lonely(state: SemanticState) -> bool:
        return state.inferred_user_state.get("lonely", False)

    @staticmethod
    def get_user_location_hint(state: SemanticState) -> str:
        return state.inferred_user_state.get("location_hint", "unknown")

    @staticmethod
    def is_seeking_advice(state: SemanticState) -> bool:
        return state.intent == Intent.SEEK_ADVICE

    @staticmethod
    def is_self_disclosure(state: SemanticState) -> bool:
        return state.intent == Intent.SELF_DISCLOSURE

    @staticmethod
    def is_relationship_signal(state: SemanticState) -> bool:
        return state.intent == Intent.RELATIONSHIP_SIGNAL

    @staticmethod
    def is_urgent(state: SemanticState) -> bool:
        return state.urgency > 0.7

    @staticmethod
    def requires_memory_lookup(state: SemanticState) -> bool:
        return state.topic.requires_memory

    @staticmethod
    def get_entities(state: SemanticState) -> list:
        return state.topic.entities

    @staticmethod
    def to_legacy_emotion_dict(state: SemanticState) -> dict:
        """
        转换为旧模块兼容的情绪字典格式。
        用于渐进迁移，旧模块可以直接使用。
        """
        return {
            "mood": state.emotion.primary_emotion,
            "valence": state.emotion.valence.value,
            "intensity": state.emotion.intensity.value,
            "confidence": state.emotion.confidence,
            "reason": state.topic.main_topic,
            "needs": "comfort" if state.needs_emotional_support() else "normal",
        }

    @staticmethod
    def to_legacy_relationship_dict(state: SemanticState) -> dict:
        """转换为旧模块兼容的关系字典格式"""
        return {
            "intimacy_delta": state.relationship.intimacy_delta,
            "trust_delta": state.relationship.trust_delta,
            "signal_type": state.relationship.signal_type,
            "is_milestone": state.relationship.is_milestone,
        }
