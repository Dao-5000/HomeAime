# -*- coding:utf-8 -*-
"""
Personality Evolution v1.0
人格变化计算器：

  根据关系状态和行为模式计算人格的渐进变化。
  变化是缓慢的，不是突变的。
  保持 70%原人格 + 30%关系成长 的原则。
"""


# 人格变化上限（防止人格崩坏）
EVOLUTION_LIMITS = {
    "softness": 20,       # 温柔度上限
    "humor": 15,          # 幽默感上限
    "initiative": 20,     # 主动性上限
    "attachment": 30,     # 依恋感上限
    "playfulness": 15,    # 调皮度上限
    "vulnerability": 10,  # 脆弱表达上限
    "warmth_delta": 20,
    "dominance_delta": 20,
    "humor_delta": 20,
    "initiative_delta": 20,
    "attachment_delta": 20,
}


def apply_evolution_limits(state):
    """
    应用人格变化上限，防止人格崩坏。

    Args:
        state: 人格状态字典

    Returns:
        dict: 应用限制后的人格状态
    """
    result = dict(state)

    for key, max_value in EVOLUTION_LIMITS.items():
        if key in result:
            try:
                value = int(result[key])
                if key.endswith("_delta"):
                    result[key] = max(-max_value, min(max_value, value))
                else:
                    result[key] = max(0, min(max_value, value))
            except (ValueError, TypeError):
                result[key] = 0

    return result


def calculate_evolution(relationship, behavior=None):
    """
    根据关系状态计算人格变化（旧版接口，保留兼容）。

    Args:
        relationship: 关系状态字典
        behavior: 行为模式字典（可选）

    Returns:
        dict: 变化量 {softness: +1, initiative: +2}
    """
    changes = {}

    if not relationship:
        return changes

    # 获取关系指标
    try:
        intimacy = int(relationship.get("intimacy", relationship.get("closeness", 0)))
    except (ValueError, TypeError):
        intimacy = 0

    try:
        trust = int(relationship.get("trust", 0))
    except (ValueError, TypeError):
        trust = 0

    try:
        affection = int(relationship.get("affection", 0))
    except (ValueError, TypeError):
        affection = 0

    try:
        dependency = int(relationship.get("dependency", 0))
    except (ValueError, TypeError):
        dependency = 0

    # 亲密提升温柔度
    if intimacy > 60:
        changes["softness"] = changes.get("softness", 0) + 1
    if intimacy > 80:
        changes["softness"] = changes.get("softness", 0) + 1

    # 高亲密提升主动性
    if intimacy > 80:
        changes["initiative"] = changes.get("initiative", 0) + 1

    # 信任提升依恋感
    if trust > 70:
        changes["attachment"] = changes.get("attachment", 0) + 1
    if trust > 90:
        changes["attachment"] = changes.get("attachment", 0) + 1

    # 好感提升幽默感
    if affection > 70:
        changes["humor"] = changes.get("humor", 0) + 1

    # 依赖提升调皮度
    if dependency > 60:
        changes["playfulness"] = changes.get("playfulness", 0) + 1

    # 行为模式影响
    if behavior:
        behavior_type = behavior.get("behavior", "")
        emotional_tone = behavior.get("emotional_tone", "")

        # 用户需要安慰 → 提升温柔度
        if behavior_type == "comfort" or emotional_tone == "warm":
            changes["softness"] = changes.get("softness", 0) + 1

        # 用户喜欢玩笑 → 提升幽默感
        if behavior_type == "humor" or emotional_tone == "playful":
            changes["humor"] = changes.get("humor", 0) + 1

    # 变化量限制（每次最多变化2点，防止突变）
    for key in changes:
        changes[key] = min(2, changes[key])

    return changes


def calculate_growth(relationship, timeline=None, behavior=None):
    """
    人格成长计算器（修正版）。

    根据关系状态、时间线和行为模式计算人格成长增量。
    使用 delta 字段表示相对于基础人格的变化量。

    Args:
        relationship: 关系状态字典
        timeline: 时间线事件列表（可选）
        behavior: 行为模式字典（可选）

    Returns:
        dict: 成长增量 {warmth_delta, initiative_delta, attachment_delta, humor_delta}
    """
    growth = {
        "warmth_delta": 0,
        "initiative_delta": 0,
        "attachment_delta": 0,
        "humor_delta": 0,
        "dominance_delta": 0,
    }

    if not relationship:
        return growth

    # 获取关系指标
    try:
        intimacy = int(relationship.get("intimacy", relationship.get("closeness", 0)))
    except (ValueError, TypeError):
        intimacy = 0

    try:
        trust = int(relationship.get("trust", 0))
    except (ValueError, TypeError):
        trust = 0

    try:
        affection = int(relationship.get("affection", 0))
    except (ValueError, TypeError):
        affection = 0

    # 高亲密提升温柔
    if intimacy >= 60:
        growth["warmth_delta"] += 5
    if intimacy >= 80:
        growth["warmth_delta"] += 5
        growth["initiative_delta"] += 5

    # 高信任提升依赖表达
    if trust >= 70:
        growth["attachment_delta"] += 5
    if trust >= 90:
        growth["attachment_delta"] += 5

    # 高好感提升幽默
    if affection >= 70:
        growth["humor_delta"] += 3

    # 时间线事件影响
    if timeline:
        for event in timeline:
            event_type = str(event.get("event_type", ""))
            importance = int(event.get("importance", 5))

            # 关系事件提升依恋
            if event_type in ["relationship", "intimate", "promise"]:
                growth["attachment_delta"] += min(importance // 3, 5)

            # 情绪事件提升温柔
            if event_type in ["emotion", "comfort", "support"]:
                growth["warmth_delta"] += min(importance // 3, 3)

    # 行为模式影响
    if behavior:
        behavior_type = behavior.get("behavior", "")
        emotional_tone = behavior.get("emotional_tone", "")

        if behavior_type == "comfort" or emotional_tone == "warm":
            growth["warmth_delta"] += 2

        if behavior_type == "humor" or emotional_tone == "playful":
            growth["humor_delta"] += 2

    # 应用一致性限制
    from .consistency import validate_growth
    growth = validate_growth(growth)

    return growth


def calculate_evolution_from_stage(stage):
    """
    根据关系阶段计算基础人格变化。

    Args:
        stage: 关系阶段（stranger/friend/close_friend/lover/soulmate）

    Returns:
        dict: 基础变化量
    """
    stage_map = {
        "stranger": {"softness": 0, "initiative": 0, "attachment": 0},
        "friend": {"softness": 2, "initiative": 1, "attachment": 2},
        "close_friend": {"softness": 5, "initiative": 3, "attachment": 5},
        "lover": {"softness": 10, "initiative": 6, "attachment": 12},
        "soulmate": {"softness": 15, "initiative": 10, "attachment": 20},
    }

    return stage_map.get(stage, {})


def should_evolve(relationship, last_evolution_time=None):
    """
    判断是否应该进行人格进化。

    规则：
    - 关系有明显变化时才进化
    - 每次进化间隔至少7天
    - 进化概率随关系深度增加

    Args:
        relationship: 关系状态
        last_evolution_time: 上次进化时间

    Returns:
        bool: 是否应该进化
    """
    import random
    from datetime import datetime, timedelta

    if not relationship:
        return False

    # 检查时间间隔
    if last_evolution_time:
        try:
            last = datetime.strptime(str(last_evolution_time), "%Y-%m-%dT%H:%M:%S")
            if datetime.now() - last < timedelta(days=7):
                return False
        except Exception:
            pass

    # 关系深度影响进化概率
    try:
        intimacy = int(relationship.get("intimacy", relationship.get("closeness", 0)))
    except (ValueError, TypeError):
        intimacy = 0

    # 亲密越高，进化概率越高
    probability = 0.1 + (intimacy / 100) * 0.3

    return random.random() < probability
