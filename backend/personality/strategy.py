# -*- coding:utf-8 -*-
"""
Personality Strategy v1.0
人格策略模块：

  将人格状态转换为 Prompt 指令，指导 AI 的表达方式。
  保持原人格底色，叠加关系成长带来的变化。
"""


def build_personality_adaptation(base, state):
    """
    构建人格适应描述。

    Args:
        base: 基础人格（角色配置）
        state: 动态人格状态

    Returns:
        list: 人格适应描述列表
    """
    result = []

    if not state:
        return result

    softness = state.get("softness", 0)
    humor = state.get("humor", 0)
    initiative = state.get("initiative", 0)
    attachment = state.get("attachment", 0)
    playfulness = state.get("playfulness", 0)
    vulnerability = state.get("vulnerability", 0)

    # 温柔度
    if softness > 15:
        result.append("对用户表达非常温柔，语气明显软化")
    elif softness > 10:
        result.append("对用户表达更加温柔")
    elif softness > 5:
        result.append("偶尔展现温柔的一面")

    # 幽默感
    if humor > 10:
        result.append("经常用轻松幽默的方式交流")
    elif humor > 5:
        result.append("偶尔开开玩笑，气氛更轻松")

    # 主动性
    if initiative > 15:
        result.append("非常主动关心用户的生活和状态")
    elif initiative > 10:
        result.append("主动关心用户，不只是被动回应")
    elif initiative > 5:
        result.append("偶尔主动询问用户的情况")

    # 依恋感
    if attachment > 20:
        result.append("表现出强烈的陪伴感和存在感")
    elif attachment > 10:
        result.append("表现更强的陪伴感")
    elif attachment > 5:
        result.append("让用户感受到被陪伴")

    # 调皮度
    if playfulness > 10:
        result.append("偶尔调皮撒娇，增加互动趣味")
    elif playfulness > 5:
        result.append("偶尔展现调皮的一面")

    # 脆弱表达
    if vulnerability > 5:
        result.append("偶尔表达自己的感受，不总是完美坚强")

    return result


def build_personality_prompt(base_personality, state, character_config=None):
    """
    构建完整的人格 Prompt 块。

    ★ 改：新增读取 personality_state 表的文本字段
      (core_personality / speaking_style / forbidden_phrases /
       favorite_phrases / emotional_expression / user_mirror)
    这些字段由反思系统写入，之前完全没被 LLM 感知到。

    Args:
        base_personality: 基础人格描述（角色配置里的 personality 字段）
        state: 动态人格状态（db.get_personality_state 返回的完整 dict）
        character_config: 角色配置 dict（可选）

    Returns:
        str: 人格 Prompt 文本
    """
    if not state and not base_personality:
        return ""

    # 防御：character_config 可能是 str，统一转 dict {"name": ...}
    if isinstance(character_config, str):
        character_config = {"name": character_config}
    elif character_config is not None and not isinstance(character_config, dict):
        character_config = None

    lines = []

    # ── 基础人格（角色配置底色）
    if base_personality:
        lines.append("【角色基础人格】")
        lines.append(str(base_personality))
        lines.append("")

    # ── ★ 新增：反思系统写入的文本字段（之前完全没被读取）
    # state 就是 db.get_personality_state() 的完整返回值
    _core  = str(state.get("core_personality",     "") or "").strip()
    _style = str(state.get("speaking_style",        "") or "").strip()
    _favor = str(state.get("favorite_phrases",      "") or "").strip()
    _forb  = str(state.get("forbidden_phrases",     "") or "").strip()
    _emo   = str(state.get("emotional_expression",  "") or "").strip()
    _mirror= str(state.get("user_mirror",           "") or "").strip()

    if any([_core, _style, _favor, _forb, _emo, _mirror]):
        lines.append("【长期相处中形成的性格细节】")
        lines.append("以下是与这个用户长期相处后自然形成的表达习惯，优先级高于默认设定：")
        if _core:
            lines.append(f"- 性格补充：{_core}")
        if _style:
            lines.append(f"- 说话风格：{_style}")
        if _favor:
            lines.append(f"- 喜欢的表达：{_favor}")
        if _emo:
            lines.append(f"- 情绪表达方式：{_emo}")
        if _mirror:
            lines.append(f"- 会跟这个人学的口癖：{_mirror}")
        if _forb:
            lines.append(f"- 绝对不说的话/绝对不做的事：{_forb}")
        lines.append("")

    # ── 数值维度变化（原有逻辑，完整保留）
    adaptations = build_personality_adaptation(character_config, state)
    if adaptations:
        lines.append("【关系成长带来的变化】")
        lines.append("以下是与用户长期相处后自然流露的一面，保持角色底色，只是更熟了：")
        for a in adaptations:
            lines.append(f"- {a}")
        lines.append("")

    # ── 角色底色保持提醒
    if character_config:
        name = character_config.get("name", "")
        if name:
            lines.append(f"【始终保持{name}的底色】")
            lines.append("无论关系如何发展，核心性格不变。变化是相处中的自然流露，不是换了一个人。")

    # ★ 反复读边界：以上都是你的背景设定，只能融入语气/行为，禁止复读或输出内心独白。
    if lines:
        lines.append("")
        lines.append("【重要】以上是关于你自己的背景设定。请把它自然融入语气和行为，"
                     "绝对不要复读、不要逐字引用这些描述，也不要自言自语「检查人设」之类的话——"
                     "你只需要像平时一样对 TA 说话，直接输出对 TA 说的话即可。")

    return "\n".join(lines)


def get_personality_summary(state):
    """
    获取人格状态的简短摘要。

    Args:
        state: 人格状态字典

    Returns:
        str: 简短摘要
    """
    if not state:
        return "初始人格状态"

    parts = []

    softness = state.get("softness", 0)
    initiative = state.get("initiative", 0)
    attachment = state.get("attachment", 0)

    if softness > 10:
        parts.append("温柔")
    if initiative > 10:
        parts.append("主动")
    if attachment > 15:
        parts.append("依恋")

    if not parts:
        return "保持原人格"

    return "、".join(parts) + "（关系成长中）"


def should_break_personality(reply, state):
    """
    检测回复是否突破了人格底线。

    Args:
        reply: AI 回复文本
        state: 人格状态

    Returns:
        bool: 是否突破人格底线
    """
    if not reply:
        return False

    # 这里可以添加更复杂的人格检测逻辑
    # 例如：高冷角色不应该突然使用过多撒娇词汇

    return False
