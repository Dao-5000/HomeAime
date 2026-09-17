# -*- coding:utf-8 -*-
"""
关系策略模块：
  根据用户关系状态生成 Prompt 片段，
  指导 AI 根据关系阶段调整语气和互动方式。
"""
from .manager import RelationshipManager
from .evolution import get_stage_info, get_stage_name, get_stage_tone, get_stage_boundary


def build_relationship_prompt(
    user_id,
    character_id="default"
):
    """
    构建关系状态的 Prompt 片段。
    返回可直接注入 system prompt 的文本。
    """
    manager = RelationshipManager()
    state = manager.get_state(user_id, character_id)

    if not state:
        return ""

    stage = state.get("stage", "stranger")
    stage_info = get_stage_info(stage)

    intimacy = state.get("intimacy", 0) or 0
    affection = state.get("affection", 50) or 50
    trust = state.get("trust", 0) or 0
    interaction_days = state.get("interaction_days", 0) or 0
    nickname = state.get("nickname", "")
    conflict = state.get("conflict_state", "")

    text = "\n【当前关系状态】\n"
    text += f"关系阶段：{get_stage_name(stage)}（{stage}）\n"
    text += f"亲密度：{intimacy}/100\n"
    text += f"好感度：{affection}/100\n"
    text += f"信任度：{trust}/100\n"
    text += f"互动天数：{interaction_days}天\n"

    if nickname:
        text += f"用户昵称：{nickname}\n"

    if conflict:
        conflict_cn = {
            "upset": "委屈期", "cold": "冷战期", "reconciling": "缓和和解期"
        }.get(conflict, conflict)
        text += f"当前冲突：{conflict_cn}\n"

    text += f"\n【互动策略】\n"
    text += f"语气：{get_stage_tone(stage)}\n"
    text += f"边界：{get_stage_boundary(stage)}\n"
    text += f"说明：{stage_info['description']}\n"

    text += "\n根据关系阶段调整语气和亲密程度。"
    text += "不要机械说明关系数值，不要突然改变关系阶段。"
    text += "关系较浅时保持礼貌和距离，关系深入后可以自然增加亲密感。\n"
    if conflict:
        text += "冲突只影响当前语气，不得辱骂、威胁离开、情感勒索或拒绝正常帮助；和好需要逐步软化，不因普通一句话瞬间恢复。\n"

    return text


def get_relationship_summary(
    user_id,
    character_id="default"
):
    """获取关系状态的简要摘要（用于日志或调试）"""
    manager = RelationshipManager()
    state = manager.get_state(user_id, character_id)

    if not state:
        return "无关系记录"

    stage = state.get("stage", "stranger")
    return (
        f"[{get_stage_name(stage)}] "
        f"亲密{state.get('intimacy', 0)} "
        f"好感{state.get('affection', 50)} "
        f"信任{state.get('trust', 0)} "
        f"互动{state.get('interaction_days', 0)}天"
    )


def should_use_intimate_tone(
    user_id,
    character_id="default"
):
    """判断是否应该使用亲密语气（亲密度>=70）"""
    manager = RelationshipManager()
    state = manager.get_state(user_id, character_id)
    intimacy = state.get("intimacy", 0) or 0
    return intimacy >= 70


def should_use_lover_tone(
    user_id,
    character_id="default"
):
    """判断是否应该使用恋人语气（亲密度>=70）"""
    return should_use_intimate_tone(user_id, character_id)
