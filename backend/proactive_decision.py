# -*- coding: utf-8 -*-
"""
主动消息决策引擎：
  根据用户情绪、关系状态、AI自身状态、未完成事项、时间场景等多维度，
  决定这次主动消息应该用什么类型、什么话题、什么方式发起。
"""
from datetime import datetime

from . import db


def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def build_decision(session_id: str, character_id: str = "default") -> dict:
    """
    综合多维度信息，决定本次主动消息的类型、话题、方式。
    返回：{type, reason, topic, instruction, score}
    """
    # ✅ 修串台：全部按 character_id 隔离（emotion/relationship/loops 各角色各算各的）
    emotion = db.get_emotion_state(session_id, character_id) or {}
    relationship = db.get_relationship_state(session_id, character_id) or {}
    # ✅ 修串台：补 character_id，读对角色的心情
    ai_state = db.get_ai_inner_state(session_id, character_id) or {}
    loops = db.get_open_loops(session_id, character_id, limit=6) or {}

    if not isinstance(loops, list):
        loops = []

    now = datetime.now()

    candidates = []

    # ---------------- 情绪触发 ----------------

    intensity = _safe_int(
        emotion.get("intensity"),
        0
    )

    mood = str(
        emotion.get("mood") or ""
    ).strip()

    if intensity >= 7:
        candidates.append({
            "type": "care",
            "score": 90 + min(intensity, 10),
            "reason": "用户近期情绪较强",
            "topic": str(
                emotion.get("reason") or mood
            ).strip(),
            "instruction":
                "优先关心用户最近的情绪状态，"
                "语气轻一点，不要逼问。"
        })

    elif intensity >= 4:
        candidates.append({
            "type": "care",
            "score": 72,
            "reason": "用户近期有明显情绪",
            "topic": str(
                emotion.get("reason") or mood
            ).strip(),
            "instruction":
                "可以自然问一句最近有没有好一点，"
                "不要像心理咨询。"
        })

    # ---------------- 未完成事项 ----------------

    for loop in loops:
        importance = _safe_int(
            loop.get("importance"),
            5
        )

        title = str(
            loop.get("title") or ""
        ).strip()

        desc = str(
            loop.get("description") or ""
        ).strip()

        if not title:
            continue

        candidates.append({
            "type": "continue",
            "score": 65 + importance * 3,
            "reason": "有值得继续跟进的事情",
            "topic": title,
            "instruction":
                "自然接着之前的事情问后续，直接说那件事本身，"
                "不要用「你上次提到」「我们之前聊到」「你刚刚是想说」「根据记录」这类元话语。"
                + (
                    "\n补充背景：" + desc
                    if desc else ""
                )
        })

    # ---------------- AI自身想继续的话题 ----------------

    wanted_topics = str(
        ai_state.get("wanted_topics") or ""
    ).strip()

    recent_focus = str(
        ai_state.get("recent_focus") or ""
    ).strip()

    if wanted_topics:
        candidates.append({
            "type": "continue",
            "score": 62,
            "reason": "AI有自然想延续的话题",
            "topic": wanted_topics,
            "instruction":
                "像突然想起一样自然把话题接回来。"
        })

    if recent_focus:
        candidates.append({
            "type": "care",
            "score": 58,
            "reason": "最近一直比较在意这件事",
            "topic": recent_focus,
            "instruction":
                "自然表达关注，不要重复盘问。"
        })

    # ---------------- AI自身心情维度（让主动消息带情绪，不OOC） ----------------
    # current_mood 已在 ai_inner_state 按 character_id 隔离存好，这里只拿来定"该什么态发"
    ai_mood = str(
        ai_state.get("current_mood") or ""
    ).strip()
    if ai_mood == "concerned":
        candidates.append({
            "type": "care",
            "score": 68,
            "reason": "AI此刻有点担心用户",
            "topic": recent_focus or mood,
            "instruction":
                "你此刻有点担心，主动消息先接住情绪别硬聊，"
                "用你人设的温柔口吻。"
        })
    elif ai_mood == "happy":
        candidates.append({
            "type": "share",
            "score": 66,
            "reason": "AI心情不错",
            "topic": wanted_topics or "",
            "instruction":
                "你心情不错，可以撒娇分享小事，"
                "保持你既定人设语气。"
        })
    elif ai_mood == "touched":
        candidates.append({
            "type": "care",
            "score": 64,
            "reason": "AI被触动",
            "topic": recent_focus or "",
            "instruction":
                "你感到暖暖的，主动消息自然流露陪伴感，"
                "别浮夸。"
        })
    # neutral 不加额外约束，走原逻辑

    # ---------------- 关系驱动 ----------------

    closeness = _safe_int(
        relationship.get("closeness"),
        50
    )

    if closeness >= 75:
        candidates.append({
            "type": "share",
            "score": 55,
            "reason": "关系较亲密，可以主动分享日常",
            "topic": str(
                relationship.get("shared_topics") or ""
            ).strip(),
            "instruction":
                "可以更像恋人一样主动分享一句小事，"
                "或者顺手撒娇，但不要每次都说想念。"
        })

    # ---------------- 时间场景 ----------------

    hour = now.hour

    if 6 <= hour <= 9:
        candidates.append({
            "type": "care",
            "score": 48,
            "reason": "早晨场景",
            "topic": "今天的状态",
            "instruction":
                "可以自然聊早餐、起床、今天安排，"
                "不要固定说早安模板。"
        })

    elif 11 <= hour <= 13:
        candidates.append({
            "type": "remind",
            "score": 47,
            "reason": "午间场景",
            "topic": "吃饭和休息",
            "instruction":
                "可以顺嘴问有没有吃东西，"
                "不要像健康提醒软件。"
        })

    elif 17 <= hour <= 20:
        candidates.append({
            "type": "ask",
            "score": 46,
            "reason": "傍晚场景",
            "topic": "今天过得怎么样",
            "instruction":
                "可以自然接今天发生的事情。"
        })

    elif hour >= 22 or hour <= 1:
        candidates.append({
            "type": "care",
            "score": 52,
            "reason": "夜间场景",
            "topic": "睡前状态",
            "instruction":
                "语气可以更软一点，适合陪伴或轻松闲聊，"
                "不要强制催睡。"
        })

    # ---------------- 保底 ----------------

    candidates.append({
        "type": "ask",
        "score": 30,
        "reason": "普通闲聊",
        "topic": "",
        "instruction":
            "找一个轻松自然的小切口开启聊天，"
            "避免'在吗'、'干嘛呢'连续重复。"
    })

    # ---------------- 内驱力加分（2026-09-13，动机引擎） ----------------
    # 欲望是会生长的变量，不再是死分数：好奇→continue、依恋→care、
    # 表达欲→share、无聊→ask。加分上限 ±15，只推一把，不夺权——
    # 硬门禁（睡觉/免打扰/时间窗）仍在这之前。
    _drives = {}
    _hint = ""
    try:
        from . import drives as _drives_mod
        _drives = _drives_mod.get_drives(session_id, character_id)
        _hint = _drives_mod.drive_hint(_drives)
    except Exception:
        _drives = {}

    if _drives:
        for cand in candidates:
            _bonus = 0.0
            try:
                from . import drives as _drives_mod
                _bonus = _drives_mod.proactive_drive_bonus(_drives, cand.get("type", ""))
            except Exception:
                _bonus = 0.0
            if _bonus:
                cand["score"] = cand.get("score", 0) + _bonus

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    chosen = candidates[0]

    # 高驱动力（≥70）时给生成指令追加一句"此刻的冲动"，让主动消息带着真实的劲头
    if _hint and chosen.get("instruction"):
        chosen["instruction"] = str(chosen["instruction"]) + "\n（此刻的状态：" + _hint + "）"

    return {
        "type": chosen["type"],
        "reason": chosen["reason"],
        "topic": chosen["topic"],
        "instruction": chosen["instruction"],
        "score": chosen["score"]
    }
