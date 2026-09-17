# -*- coding:utf-8 -*-
"""
Proactive Scoring v1.0
主动消息评分系统：
  根据上下文和触发条件计算是否应该发送主动消息。
"""


def calculate_score(context, trigger):
    """
    计算主动消息评分。

    Args:
        context: ProactiveContext 对象
        trigger: 触发信息字典

    Returns:
        int: 评分（越高越应该发送）
    """
    score = 0

    # 1. 关系越深越允许主动
    intimacy = context.relationship.get("intimacy", 0) or 0
    closeness = context.relationship.get("closeness", 0) or 0
    relationship_score = (intimacy + closeness) / 2
    score += relationship_score * 0.2

    # 2. 触发事件权重
    if trigger:
        score += trigger.get("priority", 0) * 1.5

    # 3. 有未完成事项
    if context.open_loops:
        score += 8

    # 4. 有相关记忆
    if context.memories:
        score += min(len(context.memories), 5) * 2

    # 5. 情绪状态
    emotion = context.emotion
    if emotion:
        intensity = emotion.get("intensity", 0) or 0
        mood = emotion.get("mood", "")
        if mood in ["sad", "anxious", "angry", "压力", "难过", "焦虑"]:
            score += intensity * 0.5

    # 6. 上次聊天时间
    last_chat = context.last_chat
    if last_chat:
        inactive_hours = last_chat.get("inactive_hours", 0) or 0
        if inactive_hours >= 48:
            score += 5
        elif inactive_hours >= 24:
            score += 3

    return int(score)


def should_send(score, threshold=15):
    """
    判断是否应该发送主动消息。

    Args:
        score: 评分
        threshold: 阈值

    Returns:
        bool: 是否发送
    """
    return score >= threshold


def get_tone_intensity(context, trigger):
    """
    根据上下文和触发条件判断语气强度。

    Args:
        context: ProactiveContext 对象
        trigger: 触发信息字典

    Returns:
        str: 语气强度（gentle/normal/warm/intimate）
    """
    closeness = context.relationship.get("closeness", 0) or 0

    if closeness >= 80:
        return "intimate"
    elif closeness >= 60:
        return "warm"
    elif closeness >= 40:
        return "normal"
    else:
        return "gentle"
