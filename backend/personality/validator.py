# -*- coding:utf-8 -*-
"""
Personality Validator v1.0
人格一致性校验器：

  在 AI 回复生成后，检查回复是否符合当前人格状态。
  防止高支配度角色突然使用撒娇词汇等人格崩坏情况。
"""


# 高支配度角色禁止使用的词汇
HIGH_DOMINANCE_FORBIDDEN = [
    "嘤嘤",
    "宝宝求求你",
    "人家",
    "伦家",
    "呜呜呜",
    "好不好嘛",
    "求求你了",
    "抱抱人家",
]

# 高冷角色禁止使用的词汇
HIGH_COLD_FORBIDDEN = [
    "哈哈哈哈",
    "笑死我了",
    "太搞笑了",
    "爱你哟",
    "么么哒",
    "亲亲",
]

# 温柔角色禁止使用的词汇
HIGH_WARMTH_FORBIDDEN = [
    "滚",
    "闭嘴",
    "烦不烦",
    "关我什么事",
    "随便你",
]


def check_response(answer, personality, character_id="default"):
    """
    检查回复是否符合人格状态。

    Args:
        answer: AI 回复文本
        personality: 最终人格参数
        character_id: 角色ID

    Returns:
        dict: {is_valid, warnings, suggestions}
    """
    warnings = []
    suggestions = []

    if not answer or not personality:
        return {
            "is_valid": True,
            "warnings": [],
            "suggestions": [],
        }

    answer_lower = str(answer).lower()

    dominance = personality.get("dominance", 50)
    warmth = personality.get("warmth", 50)
    humor = personality.get("humor", 50)

    # 高支配度角色检查
    if dominance > 70:
        for word in HIGH_DOMINANCE_FORBIDDEN:
            if word in answer_lower:
                warnings.append(f"高支配度角色不应使用「{word}」")
                suggestions.append("保持成熟强势的语气，避免撒娇式表达")

    # 高冷角色检查（低温柔+高支配）
    if warmth < 40 and dominance > 60:
        for word in HIGH_COLD_FORBIDDEN:
            if word in answer_lower:
                warnings.append(f"高冷角色不应使用「{word}」")
                suggestions.append("保持克制疏离的表达，避免过度热情")

    # 温柔角色检查
    if warmth > 70:
        for word in HIGH_WARMTH_FORBIDDEN:
            if word in answer_lower:
                warnings.append(f"温柔角色不应使用「{word}」")
                suggestions.append("保持温柔体贴的语气，避免生硬拒绝")

    # 回复长度检查
    answer_length = len(str(answer))
    initiative = personality.get("initiative", 50)

    # 低主动性角色不应回复过长
    if initiative < 30 and answer_length > 200:
        warnings.append("低主动性角色回复过长")
        suggestions.append("保持简洁克制的表达，不要过度展开")

    # 高主动性角色不应回复过短
    if initiative > 70 and answer_length < 10:
        warnings.append("高主动性角色回复过短")
        suggestions.append("可以更主动地展开话题，表达关心")

    is_valid = len(warnings) == 0

    return {
        "is_valid": is_valid,
        "warnings": warnings,
        "suggestions": suggestions,
    }


def generate_correction_prompt(answer, personality, validation_result):
    """
    生成人格修正 Prompt（用于重新生成回复）。

    Args:
        answer: 原始回复
        personality: 人格参数
        validation_result: 校验结果

    Returns:
        str: 修正 Prompt
    """
    if validation_result.get("is_valid", True):
        return ""

    warnings = validation_result.get("warnings", [])
    suggestions = validation_result.get("suggestions", [])

    prompt = "请重新生成回复，注意以下人格一致性要求：\n\n"

    if warnings:
        prompt += "【需要避免的问题】\n"
        for w in warnings:
            prompt += f"- {w}\n"
        prompt += "\n"

    if suggestions:
        prompt += "【改进建议】\n"
        for s in suggestions:
            prompt += f"- {s}\n"
        prompt += "\n"

    # 人格参数提醒
    trait_names = {
        "warmth": "温柔",
        "dominance": "支配",
        "humor": "幽默",
        "initiative": "主动",
    }
    prompt += "【当前人格参数】\n"
    for trait, name in trait_names.items():
        if trait in personality:
            prompt += f"{name}：{personality[trait]}\n"

    prompt += "\n原始回复：\n" + str(answer) + "\n\n"
    prompt += "请保持角色底色，重新生成更符合人格的回复。"

    return prompt


def get_personality_style_guide(personality, character_id="default"):
    """
    获取人格风格指南（用于 Prompt 注入）。

    Args:
        personality: 人格参数
        character_id: 角色ID

    Returns:
        str: 风格指南
    """
    if not personality:
        return ""

    dominance = personality.get("dominance", 50)
    warmth = personality.get("warmth", 50)
    humor = personality.get("humor", 50)
    initiative = personality.get("initiative", 50)

    lines = []

    # 支配度
    if dominance > 70:
        lines.append("表达成熟强势，语气坚定有主见")
    elif dominance > 40:
        lines.append("表达平衡，既有主见也能倾听")
    else:
        lines.append("表达柔和，善于倾听和配合")

    # 温柔度
    if warmth > 70:
        lines.append("语气温柔体贴，善于关心他人")
    elif warmth > 40:
        lines.append("语气适中，有关心但不过度")
    else:
        lines.append("语气克制疏离，不轻易表露情感")

    # 幽默感
    if humor > 60:
        lines.append("可以适当使用幽默，气氛轻松")
    elif humor > 30:
        lines.append("偶尔幽默，保持稳重")
    else:
        lines.append("保持严肃认真，少开玩笑")

    # 主动性
    if initiative > 70:
        lines.append("主动关心用户，积极展开话题")
    elif initiative > 40:
        lines.append("适度主动，回应为主")
    else:
        lines.append("保持被动回应，不过度主动")

    return "\n".join(lines)
