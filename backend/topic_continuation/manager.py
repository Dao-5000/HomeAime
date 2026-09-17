# -*- coding: utf-8 -*-
"""
话题延续管理器（Topic Continuation Manager）

职责：
1. 按 (session_id, character_id) 维护当前会话的结构化互动状态。
2. 根据 SemanticAnalyzer 输出的 TopicContinuation 更新状态。
3. 生成 prompt block，注入到 system prompt，让 AI 主动延续约定/游戏。
4. 检测话题是否自然结束或用户主动退出。
"""
import json
import time
from typing import Optional

from .. import db
from ..semantic.schema import SemanticState, TopicType, InteractionSignal


# kv key（角色隔离）
def _state_key(session_id: str, character_id: str) -> str:
    return f"topic_cont_state:{session_id}:{character_id}"


def _default_state() -> dict:
    return {
        "active": False,
        "topic_type": "none",
        "topic_name": "",
        "pattern": "none",
        "rules": [],
        "roles": {},
        "pending_action": "",
        "mood": "",
        "drop_countdown": 0,
        "turns_active": 0,
        "last_user_msg": "",
        "last_update_at": 0,
        "shared_lore": [],
        "narrative_arc": "",
    }


def _load_state(session_id: str, character_id: str) -> dict:
    raw = db.kv_get(_state_key(session_id, character_id))
    if not raw:
        return _default_state()
    try:
        state = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(state, dict):
            return _default_state()
        merged = _default_state()
        merged.update(state)
        return merged
    except Exception:
        return _default_state()


def _save_state(session_id: str, character_id: str, state: dict):
    try:
        db.kv_set(_state_key(session_id, character_id), json.dumps(state, ensure_ascii=False))
    except Exception:
        pass


def _related_to_current(msg: str, state: dict) -> bool:
    """简单判断用户消息是否与当前话题相关"""
    if not msg:
        return False
    if not state.get("topic_name") and not state.get("rules"):
        return False

    # 合并关键词
    keywords = []
    if state.get("topic_name"):
        keywords.append(str(state["topic_name"]))
    for r in state.get("rules", []):
        if isinstance(r, str):
            keywords.append(r)
    for lore in state.get("shared_lore", []):
        if isinstance(lore, str):
            keywords.append(lore)

    # 提取用户消息中的中文字段
    import re
    segs = re.findall(r"[\u4e00-\u9fff]{2,}", str(msg))
    if not segs:
        return False

    # 2-gram 匹配
    def bigrams(text):
        t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text))
        return {t[i:i + 2] for i in range(len(t) - 1)}

    msg_bi = set()
    for seg in segs:
        msg_bi |= bigrams(seg)

    for kw in keywords:
        kw_bi = bigrams(kw)
        if kw_bi and (msg_bi & kw_bi):
            return True
    return False


def update_state(
    session_id: str,
    character_id: str,
    user_message: str,
    semantic_state: Optional[SemanticState] = None,
) -> dict:
    """
    每轮对话根据语义分析更新话题延续状态。
    返回更新后的状态字典。
    """
    state = _load_state(session_id, character_id)

    tc = semantic_state.topic_continuation if semantic_state else None
    if not tc:
        # 无结构化话题，可能只是闲聊；但如果之前有活跃话题，看是否相关
        if state.get("active"):
            if _related_to_current(user_message, state):
                state["drop_countdown"] = 0
                state["turns_active"] = int(state.get("turns_active", 0)) + 1
            else:
                state["drop_countdown"] = int(state.get("drop_countdown", 0)) + 1
                if state["drop_countdown"] >= 3:
                    # 连续 3 轮无关，结束话题
                    state = _default_state()
            state["last_user_msg"] = user_message
            state["last_update_at"] = int(time.time())
            _save_state(session_id, character_id, state)
        return state

    # 用户主动结束
    signals = tc.signals or []
    if InteractionSignal.TOPIC_DROP.value in signals or tc.topic.is_explicitly_ended:
        _save_state(session_id, character_id, _default_state())
        return _default_state()

    # 当前有结构化话题
    topic_type = tc.topic.topic_type.value if tc.topic.topic_type else "none"
    if topic_type != "none":
        state["active"] = True
        state["topic_type"] = topic_type
        state["topic_name"] = tc.topic.topic_name or state.get("topic_name", "")
        state["pattern"] = tc.interaction.pattern.value if tc.interaction.pattern else "none"
        state["roles"] = tc.interaction.roles if tc.interaction.roles else state.get("roles", {})
        state["mood"] = tc.mood or state.get("mood", "")
        state["pending_action"] = (
            tc.interaction.expected_ai_action
            or tc.suggested_action
            or state.get("pending_action", "")
        )
        state["turns_active"] = int(state.get("turns_active", 0)) + 1
        state["drop_countdown"] = 0

        # 规则更新：识别 rule_making / promise_making
        if tc.interaction.rules:
            existing = {r.strip() for r in state.get("rules", []) if isinstance(r, str)}
            for r in tc.interaction.rules:
                if isinstance(r, str) and r.strip() and r.strip() not in existing:
                    existing.add(r.strip())
                    state["rules"].append(r.strip())

        # 共享 lore
        if tc.narrative.shared_lore:
            lore_list = state.get("shared_lore", [])
            for lore in tc.narrative.shared_lore:
                if isinstance(lore, str) and lore and lore not in lore_list:
                    lore_list.append(lore)
            state["shared_lore"] = lore_list

        # 叙事弧线
        if tc.narrative.arc_name:
            state["narrative_arc"] = tc.narrative.arc_name

    else:
        # 语义分析未识别出结构化话题，但之前状态活跃
        if state.get("active"):
            if _related_to_current(user_message, state):
                state["drop_countdown"] = 0
                state["turns_active"] = int(state.get("turns_active", 0)) + 1
                state["pending_action"] = (
                    tc.suggested_action if tc and tc.suggested_action else state.get("pending_action", "")
                )
            else:
                state["drop_countdown"] = int(state.get("drop_countdown", 0)) + 1
                if state["drop_countdown"] >= 3:
                    state = _default_state()

    state["last_user_msg"] = user_message
    state["last_update_at"] = int(time.time())
    _save_state(session_id, character_id, state)
    return state


def build_block(
    session_id: str,
    character_id: str,
    semantic_state: Optional[SemanticState] = None,
) -> str:
    """
    生成【互动话题延续指令】prompt block。
    没有活跃话题时返回空字符串。
    """
    state = _load_state(session_id, character_id)
    if not state.get("active"):
        return ""

    # 如果语义分析提供了更实时的 pending_action，优先用它
    pending_action = ""
    mood = state.get("mood", "")
    if semantic_state and semantic_state.topic_continuation:
        tc = semantic_state.topic_continuation
        if tc.requires_action():
            pending_action = (
                tc.interaction.expected_ai_action
                or tc.suggested_action
                or state.get("pending_action", "")
            )
        mood = tc.mood or mood

    if not pending_action:
        pending_action = state.get("pending_action", "")

    topic_name = state.get("topic_name", "")
    topic_type = state.get("topic_type", "none")
    pattern = state.get("pattern", "none")
    rules = state.get("rules", [])
    roles = state.get("roles", {}) or {}
    turns_active = state.get("turns_active", 0)
    shared_lore = state.get("shared_lore", [])
    narrative_arc = state.get("narrative_arc", "")

    lines = []
    lines.append("【互动话题延续指令】")

    if narrative_arc:
        lines.append(f"你们正在进行的共同故事：{narrative_arc}")
    if topic_name:
        lines.append(f"当前互动话题：{topic_name}（类型：{topic_type}，模式：{pattern}）")
    elif topic_type != "none":
        lines.append(f"当前互动类型：{topic_type}（模式：{pattern}）")

    if roles and isinstance(roles, dict):
        role_parts = []
        if roles.get("user_role"):
            role_parts.append(f"用户角色：{roles['user_role']}")
        if roles.get("ai_role"):
            role_parts.append(f"你的角色：{roles['ai_role']}")
        if role_parts:
            lines.append("；".join(role_parts))

    if rules:
        lines.append("已约定的规则：")
        for i, r in enumerate(rules, 1):
            if isinstance(r, str) and r.strip():
                lines.append(f"  {i}. {r.strip()}")

    if shared_lore:
        lines.append("共同梗/共享记忆：")
        for lore in shared_lore:
            if isinstance(lore, str) and lore.strip():
                lines.append(f"  - {lore.strip()}")

    if pending_action:
        lines.append(f"你当前应立即执行的动作：{pending_action}")

    if mood:
        lines.append(f"当前氛围：{mood}")

    if turns_active >= 3:
        lines.append("这个话题已经持续好几轮了，保持自然连贯，不要像刚开始一样介绍规则。")

    lines.append(
        "【输出规则】\n"
        "1. 你必须直接执行上述约定动作，不能只口头解释或承诺。\n"
        "2. 保持当前氛围，不要突然切换成客服/机械语气。\n"
        "3. 如果规则里有具体话语要求（如'亲亲要说么嘛'），本次回复必须实际说出这句话。\n"
        "4. 可以自然加入奖励/惩罚的提示，推动互动继续。"
    )

    return "\n".join(lines)


def is_active(session_id: str, character_id: str) -> bool:
    state = _load_state(session_id, character_id)
    return bool(state.get("active"))


def get_active_topic(session_id: str, character_id: str) -> Optional[dict]:
    state = _load_state(session_id, character_id)
    if not state.get("active"):
        return None
    return {
        "topic_type": state.get("topic_type", "none"),
        "topic_name": state.get("topic_name", ""),
        "pattern": state.get("pattern", "none"),
        "rules": state.get("rules", []),
        "pending_action": state.get("pending_action", ""),
        "mood": state.get("mood", ""),
    }


def end_topic(session_id: str, character_id: str):
    """外部调用：强制结束当前话题"""
    _save_state(session_id, character_id, _default_state())
