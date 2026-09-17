# -*- coding: utf-8 -*-
"""
对话质感引擎 v1.0（方式A：情绪化 Prompt 提示）
复用「语气情绪优化方案」的核心映射（长度表/风格提示），适配本项目 AIEmotion 情绪枚举。

方式B（后处理：打错字/口癖/欲言又止）后续接入，见 README 备注。
"""
from . import time_system

# ── 本项目情绪枚举 → 质感风格档 ──
EMOTION_MAP = {
    "happy":       "happy",
    "excited":     "excited",
    "tender":      "warm",
    "playful":     "happy",
    "calm":        "neutral",
    "worried":     "worried",
    "sad":         "sad",
    "upset":       "sad",
    "angry":       "angry",
    "cold":        "sulky",
    "reconciling": "warm",
    "loving":      "longing",
}

# ── 长度控制（风格档 → 目标字数范围）──
RESPONSE_LENGTH = {
    "happy":   (60, 150),
    "warm":    (40, 120),
    "shy":     (20, 80),
    "excited": (80, 200),
    "sad":     (20, 60),
    "sulky":   (5, 30),
    "angry":   (10, 40),
    "tired":   (15, 60),
    "worried": (30, 100),
    "jealous": (20, 70),
    "longing": (40, 120),
    "neutral": (30, 100),
}

# ── 情绪风格提示（复用方案的 _get_style_hint）──
STYLE_HINTS = {
    "sulky":   "在生闷气，话很少，只说关键词，不解释，等对方哄",
    "shy":     "有点害羞，说话会吞吞吐吐，说到一半停住，用省略号",
    "excited": "很兴奋，话多，句子短，喜欢重复对方说的话表示赞同",
    "sad":     "有点难过，话少，说话慢，每句话都短，会用省略号",
    "jealous": "有点吃醋，说话有点小脾气，不直接说，但能感受到",
    "longing": "很想念，话里会不经意带出来，有点感性，喜欢问对方在干嘛",
    "tired":   "很累，语气疲惫，句子短，但还是想聊",
    "angry":   "有点生气，语气硬，但不会完全冷漠",
    "warm":    "语气温柔，说话有点慢，句子里有温度",
    "happy":   "开心，话多，喜欢说哈哈，喜欢聊今天发生的事",
}


def map_emotion(emotion: str) -> str:
    """把本项目情绪枚举映射到质感风格档。"""
    return EMOTION_MAP.get(str(emotion or "").lower(), "neutral")


def get_length_hint(style: str, intensity: float = 0.5, is_late_night: bool = False) -> str:
    """返回长度提示（给 LLM 的指令）。"""
    min_len, max_len = RESPONSE_LENGTH.get(style, RESPONSE_LENGTH["neutral"])
    if is_late_night:
        max_len = int(max_len * 0.6)
        min_len = int(min_len * 0.7)
    intensity = max(0.0, min(1.0, float(intensity or 0.5)))
    target = int(min_len + (max_len - min_len) * intensity)

    if target < 20:
        return f"回复要非常简短，{target}字以内，话少但有分量"
    if target < 50:
        return f"回复适中简短，大约{target}字左右"
    if target < 100:
        return f"回复正常长度，大约{target}字左右，自然展开"
    return f"可以话多一些，{target}字左右，撒娇/聊天都可以"


def get_prompt_hints(emotion: str, intensity: float = 0.5, is_late_night: bool = False) -> str:
    """
    生成写入 system prompt 的质感提示（方式A）。
    emotion: 本项目 AIEmotion 情绪（happy/excited/tender/.../loving）
    """
    style = map_emotion(emotion)
    hints = [get_length_hint(style, intensity, is_late_night)]

    if style in STYLE_HINTS:
        h = STYLE_HINTS[style]
        if intensity > 0.75:
            h += f"（情绪比较强烈，强度{intensity:.1f}）"
        hints.append(h)

    if is_late_night:
        hints.append("深夜了，说话要轻柔，句子短，暧昧感强，适合说说心里话")

    return "；".join(hints)


def build_texture_block(emotion: str, intensity: float = 0.5) -> str:
    """生成注入 prompt 的质感块（含当前时段是否深夜）。"""
    period = time_system.time_period()
    is_late = period in ("凌晨", "夜晚")
    hints = get_prompt_hints(emotion, intensity, is_late)
    if not hints:
        return ""
    return "【当前语气质感】\n" + hints
