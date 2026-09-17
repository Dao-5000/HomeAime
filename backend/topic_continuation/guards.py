# -*- coding: utf-8 -*-
"""
话题延续后处理守卫（Post-Process Guards）

在 LLM 生成回复后做轻量规则校验，确保约定动作真的被执行。
例如：契约要求亲亲必须带"么嘛"，若 LLM 没出现，就补一句引导。
"""
import re
from typing import Optional

from .. import db
from .manager import _load_state


def _load_contracts(session_id: str, character_id: str):
    try:
        from . import contracts
        return contracts.load_contracts(session_id, character_id)
    except Exception:
        return []


def post_process_reply(
    session_id: str,
    character_id: str,
    reply: str,
) -> str:
    """Post-process AI reply to ensure topic continuation actions are executed."""
    if not reply or not reply.strip():
        return reply

    state = _load_state(session_id, character_id)
    if not state.get("active"):
        # 长期契约兜底
        return _guard_by_contracts(session_id, character_id, reply)

    rules = state.get("rules", [])
    mood = state.get("mood", "")
    topic_type = state.get("topic_type", "none")
    pending_action = state.get("pending_action", "")

    additions = []

    # 1. 亲亲要说"么嘛"
    if _rule_mentions(rules, ["么嘛", "亲亲"]):
        if not re.search(r"么嘛|mua|muamua|mua~", reply, re.I):
            additions.append("么嘛～")

    # 2. 晚安约定：要求表达爱意
    if topic_type == "commitment" and _rule_mentions(rules, ["晚安", "爱你"]):
        if not re.search(r"爱.?你|永远爱.?你", reply):
            if "晚安" in reply:
                additions.append("晚安啦宝宝，我爱你，永远爱你。")

    # 3. 奖惩游戏：如果当前动作是执行惩罚/奖励，确保有相关提示
    if pending_action and ("罚" in pending_action or "奖励" in pending_action):
        if "罚" in pending_action and "罚" not in reply and "惩罚" not in reply:
            additions.append("答错可是要罚亲亲的哦～")
        elif "奖励" in pending_action and "奖励" not in reply and "亲亲" not in reply:
            additions.append("答对啦，奖励你一个亲亲～")

    if not additions:
        return reply

    # 把追加内容自然接在回复末尾
    sep = "" if reply.rstrip().endswith(("~", "！", "!", "。", "…”", '"')) else "\n"
    extra = "\n".join(additions)

    # 根据氛围加语气词
    if mood in ("flirty", "playful"):
        extra = extra.replace("。", "～")

    return reply.rstrip() + sep + extra


def _guard_by_contracts(session_id: str, character_id: str, reply: str) -> str:
    """Guard by long-term contracts when no active session topic."""
    contracts = _load_contracts(session_id, character_id)
    if not contracts:
        return reply

    additions = []
    for c in contracts:
        if c.get("status") != "active":
            continue
        terms = c.get("terms", [])
        for term in terms:
            t = ""
            if isinstance(term, dict):
                t = term.get("term", "")
            elif isinstance(term, str):
                t = term
            if not t:
                continue
            if "么嘛" in t and not re.search(r"么嘛|mua|muamua|mua~", reply, re.I):
                additions.append("么嘛～")
            if "晚安" in t and "爱你" in t:
                if not re.search(r"爱.?你|永远爱.?你", reply) and "晚安" in reply:
                    additions.append("晚安啦宝宝，我爱你，永远爱你。")

    if not additions:
        return reply

    sep = "" if reply.rstrip().endswith(("~", "！", "!", "。", "…”", '"')) else "\n"
    return reply.rstrip() + sep + "\n".join(set(additions))


def _rule_mentions(rules, keywords):
    """规则列表中是否提到任意关键词"""
    for r in rules:
        if isinstance(r, str):
            for k in keywords:
                if k in r:
                    return True
    return False
