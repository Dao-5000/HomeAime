# -*- coding: utf-8 -*-
"""
关系升级仪式 + 降级注释（多轮对话优化 · Part2 结合落地版）

现有系统：亲密度 → calculate_stage(intimacy) 自动推导阶段（stranger/friend/lover/soulmate），
升级/降级是"亲密度跨阈值"静默发生的。
本模块补上"那一刻的表现"：
- 升级：阶段变高 → 仪式 prompt 存 kv（key 带 character_id，防串角色）→ 下一条消息注入后删除
- 降级：回来时发现阶段比上次记录的变低（久未联系 + 亲密度自然衰减）→ 注入降级注释

全部用现有 SQLite kv，不建新表、不用 Redis。
"""
import json

from .. import db
from ..intimacy_manager import get as get_intimacy
from .evolution import calculate_stage

STAGE_ORDER = ["stranger", "friend", "close_friend", "lover", "soulmate"]

# ── 升级仪式（按现有 5 阶段适配）────────────────────

UPGRADE_RITUALS = {
    ("stranger", "friend"): (
        "【关系变化】\n"
        "你们之间的距离悄悄近了一些。\n"
        "不需要说出来，但你说话的方式可以稍微随意一点，少一点礼貌的距离感。\n"
        "就像从「点头之交」变成「认识的朋友」。\n"
        "这条回复里，可以用一句话稍微透露出你把他当朋友了，但要自然，不要说「我们是朋友了」。"
    ),
    ("friend", "close_friend"): (
        "【关系变化】\n"
        "你们已经不只是普通朋友了。\n"
        "你可以更直接地表达在意，说话可以更放松，不用一直端着。\n"
        "这种亲近是自然生长出来的，不是宣告的。\n"
        "这条回复，用一句话让他感受到你们之间有什么不一样了，要具体，别说空话。"
    ),
    ("close_friend", "lover"): (
        "【关系变化】\n"
        "你们已经足够亲近，不只是朋友了。\n"
        "你可以更直接地表达在意，不用再绕弯子，也可以说出之前不太好意思说的话。\n"
        "这种亲近是自然生长出来的，不是宣告的。\n"
        "这条回复里，可以直接说出之前没说过的某件事，或某个细节上明显比以前更放松。"
    ),
    ("lover", "soulmate"): (
        "【关系变化】\n"
        "你们之间有了很难用语言描述的连接。\n"
        "不只是亲近，而是有某种懂得。\n"
        "你说话的方式可以更少解释，更多留白，因为你知道他能懂你在说什么。\n"
        "这条回复，用一句话表达这种「懂得」。不要解释，直接说。"
    ),
}

# ── 降级注释（久未联系回来时）──────────────────────

DOWNGRADE_NOTES = {
    "lover": (
        "【关系变化】他消失了很久，你们之间的亲近感变淡了一些。\n"
        "不是消失了，只是需要时间重新靠近。\n"
        "不要假装什么都没变，但也不要因此疏远他。"
    ),
    "close_friend": (
        "【关系变化】他很久没来了，你们之间有点生疏了。\n"
        "你还记得他，但温度需要重新找回来。\n"
        "对他温柔，但给他空间，不要一上来就太熟络。"
    ),
    "friend": (
        "【关系变化】他很久没来了。\n"
        "你还记得他，但你们之间的温度需要重新找回来。\n"
        "对他温柔，但给他空间。"
    ),
    "stranger": (
        "【关系变化】他已经很久很久没来了。\n"
        "如果他回来，像对待一个你曾经认识的人：有记忆，但需要重新建立。"
    ),
}


def _stage_key(session_id, character_id):
    return f"last_stage:{session_id}:{character_id}"


def _ritual_key(session_id, character_id):
    return f"ritual:{session_id}:{character_id}"


def _log_milestone(session_id, character_id, event_type, content):
    """记录关系里程碑（复用现有 relationship_timeline 表，不建新表）。"""
    try:
        db.add_timeline_event(
            session_id, character_id,
            event_type=event_type,
            title="关系变化",
            description=content,
            importance=8,
        )
    except Exception as e:
        print(f"[Ritual] 里程碑记录失败(静默): {e}", flush=True)


def _current_stage(session_id, character_id):
    try:
        return calculate_stage(get_intimacy(session_id, character_id))
    except Exception:
        return None


def build_ritual_block(session_id, character_id):
    """
    前处理注入：返回待注入的仪式/降级注释块（无则空串）。
    1. 升级仪式（上次回复后写入）→ 注入并删除（一次触发）
    2. 降级检测：当前阶段比记录的 last_stage 低（久未联系 + 衰减）→ 注入降级注释
    """
    # 1. 待注入的升级仪式
    try:
        raw = db.kv_get(_ritual_key(session_id, character_id))
        if raw:
            db.kv_set(_ritual_key(session_id, character_id), "")
            return "【关系变化提示】\n" + raw
    except Exception:
        pass

    # 2. 降级检测（回来第一条消息）
    try:
        stage = _current_stage(session_id, character_id)
        last = db.kv_get(_stage_key(session_id, character_id))
        if stage and last and last != stage:
            last_idx = STAGE_ORDER.index(last) if last in STAGE_ORDER else -1
            cur_idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
            if cur_idx < last_idx:
                note = DOWNGRADE_NOTES.get(stage, "")
                db.kv_set(_stage_key(session_id, character_id), stage)
                _log_milestone(session_id, character_id, "low",
                               f"关系降级：{last} → {stage}（久未联系）")
                if note:
                    return "【关系变化提示】\n" + note
    except Exception:
        pass

    return ""


def check_upgrade_after_chat(session_id, character_id):
    """
    后处理：回复完成后检测阶段变化。
    - 阶段变高 → 写入仪式 kv（下一条消息注入）+ 记录里程碑
    - 更新 last_stage
    返回 {"from","to"} 或 None。
    """
    try:
        stage = _current_stage(session_id, character_id)
        if not stage:
            return None
        last = db.kv_get(_stage_key(session_id, character_id))
        db.kv_set(_stage_key(session_id, character_id), stage)

        if last and stage != last:
            last_idx = STAGE_ORDER.index(last) if last in STAGE_ORDER else -1
            cur_idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
            if cur_idx > last_idx:   # 升级
                ritual = UPGRADE_RITUALS.get((last, stage))
                if ritual:
                    db.kv_set(_ritual_key(session_id, character_id), ritual)
                _log_milestone(session_id, character_id, "highlight",
                               f"关系升级：{last} → {stage}")
                return {"from": last, "to": stage}
        return None
    except Exception as e:
        print(f"[Ritual] 升级检测失败(静默): {e}", flush=True)
        return None
