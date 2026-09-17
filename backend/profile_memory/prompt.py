# -*- coding:utf-8 -*-
"""
Life Profile Prompt v1.0
人生档案 Prompt 生成器：

  将整理后的人生档案转换为可注入 system prompt 的文本。
  帮助 AI 理解用户的长期变化和成长轨迹。
"""
from .manager import PROFILE_TYPES


def build_life_profile_prompt(profiles):
    """
    将人生档案转换为 Prompt 文本。

    Args:
        profiles: 档案列表（来自 LifeProfileManager.get()）

    Returns:
        str: Prompt 文本
    """
    if not profiles:
        return ""

    # 按类型分组
    grouped = {}
    for profile in profiles:
        profile_type = profile.get("profile_type", "other")
        if profile_type not in grouped:
            grouped[profile_type] = []
        grouped[profile_type].append(profile)

    if not grouped:
        return ""

    blocks = []

    # 按重要性排序的类型顺序
    type_order = [
        "life_phase",
        "growth_trajectory",
        "personality_pattern",
        "behavior_pattern",
        "emotional_pattern",
        "important_values",
        "relationship_story",
    ]

    for profile_type in type_order:
        if profile_type not in grouped:
            continue

        items = grouped[profile_type]
        if not items:
            continue

        type_name = PROFILE_TYPES.get(profile_type, profile_type)
        lines = []

        for item in items:
            content = item.get("content", "")
            if content:
                lines.append(f"- {content}")

        if lines:
            block = f"【{type_name}】\n" + "\n".join(lines)
            blocks.append(block)

    # 处理其他未在 type_order 中的类型
    for profile_type, items in grouped.items():
        if profile_type in type_order:
            continue
        type_name = PROFILE_TYPES.get(profile_type, profile_type)
        lines = []
        for item in items:
            content = item.get("content", "")
            if content:
                lines.append(f"- {content}")
        if lines:
            block = f"【{type_name}】\n" + "\n".join(lines)
            blocks.append(block)

    if not blocks:
        return ""

    return (
        "【用户长期人生档案】\n"
        "以下是根据长期互动整理的用户画像和成长轨迹，"
        "用于理解用户的长期变化和深层需求。"
        "自然理解和运用，不要逐条复述，不要向用户提到档案系统。\n\n"
        + "\n\n".join(blocks)
    )


def get_profile_summary(profiles):
    """
    获取人生档案的简短摘要（用于日志和调试）。

    Args:
        profiles: 档案列表

    Returns:
        str: 摘要文本
    """
    if not profiles:
        return "无档案"

    type_counts = {}
    for p in profiles:
        ptype = p.get("profile_type", "other")
        type_counts[ptype] = type_counts.get(ptype, 0) + 1

    parts = [f"{PROFILE_TYPES.get(k, k)}:{v}" for k, v in type_counts.items()]
    return f"共{len(profiles)}条档案（" + "，".join(parts) + "）"
