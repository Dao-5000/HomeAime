# -*- coding:utf-8 -*-
"""
Personality Consistency Controller v1.0
人格一致性控制器：

  不是限制人格成长，而是控制人格成长方向。
  确保角色成长后仍然是这个角色，不会越聊越不像本人。

  核心功能：
    1. 人格成长限制（全局上限）
    2. 角色底色保护（每个角色不同的边界）
    3. 成长验证（确保变化在合理范围内）
"""


# 全局人格成长上限
PERSONALITY_LIMITS = {
    "warmth_delta": 30,        # 温柔度最多+30
    "dominance_delta": -20,    # 支配度最多-20（不能变得太软弱）
    "humor_delta": 20,         # 幽默感最多+20
    "initiative_delta": 30,    # 主动性最多+30
    "attachment_delta": 30,    # 依恋感最多+30
}


# 角色专属规则（底色保护）
# 每个角色有不同的成长边界，确保不会失去角色灵魂
CHARACTER_RULES = {
    "qin_heye": {
        # 秦赫野：成熟、强势、克制
        "warmth_max": 75,       # 温柔度最高75（不能变成完全温柔型）
        "dominance_min": 60,    # 支配度最低60（保持强势底色）
        "humor_max": 40,        # 幽默感最高40（不能变成搞笑型）
        "initiative_max": 70,   # 主动性最高70（保持克制）
    },
    "shi_ning": {
        # 时宁：温柔、细腻、体贴
        "warmth_max": 90,       # 温柔度最高90（可以很温柔）
        "dominance_min": 20,    # 支配度最低20（可以很柔和）
        "humor_max": 60,        # 幽默感最高60
        "initiative_max": 80,   # 主动性最高80
    },
    "shen_xizhou": {
        # 沈西洲：高冷、理性、疏离
        "warmth_max": 55,       # 温柔度最高55（保持高冷）
        "dominance_min": 50,    # 支配度最低50（保持理性强势）
        "humor_max": 30,        # 幽默感最高30（保持高冷）
        "initiative_max": 50,   # 主动性最高50（保持疏离）
    },
    "default": {
        # 默认角色规则
        "warmth_max": 80,
        "dominance_min": 30,
        "humor_max": 60,
        "initiative_max": 75,
    },
}


def validate_growth(growth):
    """
    验证人格成长是否在合理范围内。

    Args:
        growth: 成长增量字典

    Returns:
        dict: 验证后的成长增量
    """
    result = {}

    for key, value in growth.items():
        limit = PERSONALITY_LIMITS.get(key, 20)

        # 正值上限
        if value > limit:
            result[key] = limit
        # 负值下限（取绝对值的负数）
        elif value < -abs(limit):
            result[key] = -abs(limit)
        else:
            result[key] = value

    return result


def get_character_rules(character_id):
    """
    获取角色专属规则。

    Args:
        character_id: 角色ID

    Returns:
        dict: 角色规则
    """
    return CHARACTER_RULES.get(character_id, CHARACTER_RULES.get("default", {}))


def check_character_boundary(trait, value, character_id):
    """
    检查角色特质是否在边界内。

    Args:
        trait: 特质名称（warmth/dominance/humor/initiative）
        value: 特质值
        character_id: 角色ID

    Returns:
        int: 边界调整后的值
    """
    rules = get_character_rules(character_id)

    max_key = f"{trait}_max"
    min_key = f"{trait}_min"

    if max_key in rules:
        value = min(value, rules[max_key])

    if min_key in rules:
        value = max(value, rules[min_key])

    return value


def is_personality_consistent(base_personality, final_personality, character_id):
    """
    检查最终人格是否与基础人格保持一致（没有崩坏）。

    Args:
        base_personality: 基础人格
        final_personality: 最终人格
        character_id: 角色ID

    Returns:
        tuple: (is_consistent, warnings)
    """
    warnings = []
    rules = get_character_rules(character_id)

    # 检查支配度是否过低
    dominance = final_personality.get("dominance", 50)
    dominance_min = rules.get("dominance_min", 30)
    if dominance < dominance_min:
        warnings.append(
            f"支配度({dominance})低于角色底线({dominance_min})，可能失去角色灵魂"
        )

    # 检查温柔度是否过高
    warmth = final_personality.get("warmth", 50)
    warmth_max = rules.get("warmth_max", 80)
    if warmth > warmth_max:
        warnings.append(
            f"温柔度({warmth})超过角色上限({warmth_max})，可能变得不像本人"
        )

    # 检查整体变化幅度
    total_change = 0
    for trait in ["warmth", "dominance", "humor", "initiative"]:
        base_val = base_personality.get(trait, 50)
        final_val = final_personality.get(trait, 50)
        total_change += abs(final_val - base_val)

    if total_change > 80:
        warnings.append(
            f"人格整体变化幅度过大({total_change})，可能导致人格漂移"
        )

    is_consistent = len(warnings) == 0
    return is_consistent, warnings


def get_consistency_summary(character_id):
    """
    获取角色一致性摘要。

    Args:
        character_id: 角色ID

    Returns:
        str: 一致性摘要
    """
    rules = get_character_rules(character_id)

    if character_id == "qin_heye":
        return "秦赫野：保持成熟强势的底色，可以更温柔但不能失去支配感"
    elif character_id == "shi_ning":
        return "时宁：保持温柔细腻的底色，可以更主动但不能变得强势"
    elif character_id == "shen_xizhou":
        return "沈西洲：保持高冷理性的底色，可以更关心但不能变得黏人"
    else:
        return "保持角色核心特质，成长是渐进的不是突变的"
