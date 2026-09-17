# -*- coding:utf-8 -*-
"""
Personality Builder v1.0
最终人格构建器：

  将基础人格 + 关系成长 + 用户习惯 合并为最终人格。
  应用角色边界限制，确保人格一致性。
"""
from .consistency import (
    get_character_rules,
    check_character_boundary,
    is_personality_consistent,
    get_consistency_summary,
)


# 默认基础人格参数
DEFAULT_BASE_TRAITS = {
    "warmth": 50,
    "dominance": 50,
    "humor": 50,
    "initiative": 50,
    "attachment": 50,
}


def extract_base_traits(character_config):
    """
    从角色配置中提取基础人格参数。

    Args:
        character_config: 角色配置字典

    Returns:
        dict: 基础人格参数
    """
    if not character_config:
        return dict(DEFAULT_BASE_TRAITS)

    # 尝试从 core_traits 字段获取
    core_traits = character_config.get("core_traits", {})
    if isinstance(core_traits, dict) and core_traits:
        result = dict(DEFAULT_BASE_TRAITS)
        for key in DEFAULT_BASE_TRAITS:
            if key in core_traits:
                try:
                    result[key] = int(core_traits[key])
                except (ValueError, TypeError):
                    pass
        return result

    # 根据角色名称推断基础人格
    name = character_config.get("name", "")
    personality = character_config.get("personality", "")

    result = dict(DEFAULT_BASE_TRAITS)

    if "秦赫野" in name or "强势" in str(personality):
        result.update({
            "warmth": 40,
            "dominance": 80,
            "humor": 20,
            "initiative": 30,
        })
    elif "时宁" in name or "温柔" in str(personality):
        result.update({
            "warmth": 80,
            "dominance": 30,
            "humor": 50,
            "initiative": 60,
        })
    elif "沈西洲" in name or "高冷" in str(personality):
        result.update({
            "warmth": 30,
            "dominance": 70,
            "humor": 15,
            "initiative": 20,
        })

    return result


def build_final_personality(base, delta, character_id="default"):
    """
    构建最终人格。

    将基础人格 + 成长增量合并，应用角色边界限制。

    Args:
        base: 基础人格参数
        delta: 成长增量（delta 字段）
        character_id: 角色ID

    Returns:
        dict: 最终人格参数
    """
    if not base:
        base = dict(DEFAULT_BASE_TRAITS)

    result = dict(base)

    if not delta:
        return result

    # 应用增量
    for key, value in delta.items():
        # 去掉 _delta 后缀，得到特质名
        trait = key.replace("_delta", "")

        if trait not in result:
            continue

        try:
            old = int(result.get(trait, 50))
            new = old + int(value)
        except (ValueError, TypeError):
            continue

        # 应用角色边界限制
        new = check_character_boundary(trait, new, character_id)

        # 全局限制 0-100
        new = max(0, min(100, new))

        result[trait] = new

    return result


def build_personality_prompt(base, delta, final, character_id="default", character_config=None):
    """
    构建人格 Prompt 文本。

    Args:
        base: 基础人格
        delta: 成长增量
        final: 最终人格
        character_id: 角色ID
        character_config: 角色配置

    Returns:
        str: 人格 Prompt 文本
    """
    lines = []

    # 角色基础人格
    if character_config:
        name = character_config.get("name", "")
        personality = character_config.get("personality", "")
        if name or personality:
            lines.append("【角色基础人格】")
            if name:
                lines.append(f"姓名：{name}")
            if personality:
                if isinstance(personality, list):
                    lines.append("性格：" + "、".join(personality))
                else:
                    lines.append(f"性格：{personality}")
            lines.append("")

    # 长期成长
    if delta:
        growth_descriptions = []
        if delta.get("warmth_delta", 0) > 5:
            growth_descriptions.append("对用户更加温柔")
        if delta.get("initiative_delta", 0) > 5:
            growth_descriptions.append("更主动表达关心")
        if delta.get("attachment_delta", 0) > 5:
            growth_descriptions.append("表现出更强的陪伴感")
        if delta.get("humor_delta", 0) > 5:
            growth_descriptions.append("偶尔展现轻松幽默的一面")

        if growth_descriptions:
            lines.append("【长期关系成长】")
            lines.append("经过长期互动，在保持角色底色的基础上：")
            for desc in growth_descriptions:
                lines.append(f"- {desc}")
            lines.append("")

    # 当前人格参数
    if final:
        lines.append("【当前人格参数】")
        trait_names = {
            "warmth": "温柔",
            "dominance": "支配",
            "humor": "幽默",
            "initiative": "主动",
            "attachment": "依恋",
        }
        for trait, name in trait_names.items():
            if trait in final:
                lines.append(f"{name}：{final[trait]}")
        lines.append("")

    # 一致性提醒
    consistency_summary = get_consistency_summary(character_id)
    if consistency_summary:
        lines.append("【人格一致性提醒】")
        lines.append(consistency_summary)
        lines.append("成长是相处中的自然流露，不是换了一个人。")

    return "\n".join(lines)


def get_personality_delta_from_state(state):
    """
    从人格状态中提取 delta 字段。

    Args:
        state: 人格状态字典

    Returns:
        dict: delta 字段
    """
    if not state:
        return {}

    delta = {}
    delta_keys = [
        "warmth_delta",
        "dominance_delta",
        "humor_delta",
        "initiative_delta",
        "attachment_delta",
    ]

    for key in delta_keys:
        if key in state:
            try:
                delta[key] = int(state[key])
            except (ValueError, TypeError):
                delta[key] = 0

    return delta
