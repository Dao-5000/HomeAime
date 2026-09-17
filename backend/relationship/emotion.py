# -*- coding:utf-8 -*-
"""
情感计算模块 v2.0
  原版：关键词匹配计算亲密度、好感度、信任度
  v2.0：接入 SemanticState，基于语义理解计算
        保持原有函数签名，调用方无需修改
        关键词逻辑保留为 fallback（SemanticState 不可用时使用）
"""
import re
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..semantic.schema import SemanticState


# ── 词表（降级兜底用）────────────────────────────────────────────────────

POSITIVE_WORDS = [
    "谢谢", "感谢", "喜欢你", "想你", "爱你", "陪我", "晚安",
    "早安", "开心", "高兴", "幸福", "温暖", "舒服", "好听",
    "好看", "可爱", "厉害", "棒", "好的", "嗯嗯", "哈哈",
    "嘻嘻", "嘿嘿", "么么哒", "抱抱", "亲亲", "宝贝", "宝宝",
    "亲爱的", "想你了", "等你", "依赖你", "信任你"
]

NEGATIVE_WORDS = [
    "讨厌", "烦", "别说了", "闭嘴", "滚", "走开", "不想理你",
    "生气", "难过", "伤心", "失望", "无语", "呵呵", "哦",
    "随便", "都行", "无所谓", "无聊", "笨", "蠢", "傻"
]

INTIMACY_WORDS = [
    "想你", "爱你", "抱抱", "亲亲", "宝贝", "宝宝", "亲爱的",
    "么么哒", "依赖你", "信任你", "陪我", "等你", "晚安",
    "早安", "一起", "我们"
]

TRUST_WORDS = ["信任你", "相信你", "依赖你", "告诉你", "跟你说"]
DISTRUST_WORDS = ["不信", "骗人", "撒谎", "假的", "忽悠"]


# ── 核心计算函数 ──────────────────────────────────────────────────────────

def calculate_affection(
    message: str,
    current: float,
    semantic: Optional["SemanticState"] = None
) -> float:
    """
    计算好感度变化，结果限制在 0-100。

    有 SemanticState 时：基于关系信号 + 情绪效价计算
    无 SemanticState 时：降级为词表匹配
    """
    if semantic is not None:
        delta = 0

        # 关系信号
        signal_type = semantic.relationship.signal_type
        if signal_type == "gratitude":
            delta += 2
        elif signal_type == "flirt":
            delta += 2
        elif signal_type == "conflict":
            delta -= 5

        # 情绪效价
        valence = semantic.emotion.valence
        if hasattr(valence, 'value'):
            valence_value = valence.value
        else:
            valence_value = str(valence)

        if valence_value == "positive":
            delta += 1
        elif valence_value == "negative":
            delta -= 2

        return max(0, min(100, current + delta))

    # ── 降级：词表匹配 ──
    score = 0
    message = str(message or "")
    for word in POSITIVE_WORDS:
        if word in message:
            score += 3
    for word in NEGATIVE_WORDS:
        if word in message:
            score -= 5
    return max(0, min(100, current + score))


def calculate_intimacy(
    message: str,
    current: float,
    semantic: Optional["SemanticState"] = None
) -> float:
    """
    计算亲密度变化，结果限制在 0-100。
    每次互动基础 +1，语义亲密信号额外加分。
    """
    if semantic is not None:
        score = 1  # 基础互动分

        # 关系信号
        signal_type = semantic.relationship.signal_type
        if signal_type == "flirt":
            score += 2
        elif signal_type == "gratitude":
            score += 1

        # 里程碑事件
        if semantic.relationship.is_milestone:
            score += 5

        return max(0, min(100, current + score))

    # ── 降级：词表匹配 ──
    score = 1
    message = str(message or "")
    for word in INTIMACY_WORDS:
        if word in message:
            score += 2
            break
    return max(0, min(100, current + score))


def calculate_trust(
    message: str,
    current: float,
    semantic: Optional["SemanticState"] = None
) -> float:
    """
    计算信任度变化，结果限制在 0-100。
    """
    if semantic is not None:
        score = 0

        # 关系信号
        signal_type = semantic.relationship.signal_type
        if signal_type == "gratitude":
            score += 2
        elif signal_type == "conflict":
            score -= 3

        # 情绪中表达信任
        if semantic.emotion.primary_emotion == "trust":
            score += 3

        return max(0, min(100, current + score))

    # ── 降级：词表匹配 ──
    score = 0
    message = str(message or "")
    for word in TRUST_WORDS:
        if word in message:
            score += 3
    for word in DISTRUST_WORDS:
        if word in message:
            score -= 5
    return max(0, min(100, current + score))


def detect_emotion(
    message: str,
    semantic: Optional["SemanticState"] = None
) -> str:
    """
    情绪检测，返回情绪标签字符串。
    有 SemanticState 时直接读 primary_emotion，不做任何匹配。
    """
    if semantic is not None:
        if semantic.emotion.primary_emotion:
            return semantic.emotion.primary_emotion

    # ── 降级：词表匹配 ──
    message = str(message or "")
    emotions = {
        "happy":       ["开心", "高兴", "哈哈", "嘻嘻", "嘿嘿", "棒", "厉害", "幸福"],
        "sad":         ["难过", "伤心", "哭", "委屈", "失落", "沮丧"],
        "angry":       ["生气", "烦", "讨厌", "滚", "闭嘴", "无语"],
        "anxious":     ["焦虑", "担心", "害怕", "紧张", "压力", "累"],
        "affectionate":["想你", "爱你", "抱抱", "亲亲", "宝贝", "宝宝", "亲爱的"],
    }
    for emotion, words in emotions.items():
        for word in words:
            if word in message:
                return emotion
    return "neutral"
