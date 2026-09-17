# -*- coding: utf-8 -*-
"""冷战与和解状态机。

状态按 session + character 隔离，关系表只保存当前阶段，详细原因/时间保存在 kv。
普通负面情绪不会触发；只有明确伤人、驱赶或关系攻击才进入冲突。
"""
import json
import time
from datetime import datetime

from .. import db
from .database import conn


HURT_PATTERNS = (
    "你闭嘴", "滚开", "你滚", "滚吧", "烦死了", "别说了", "少管我",
    "不想理你", "别烦我", "离我远点", "你真没用", "你一点用都没有",
    "我讨厌你", "我不要你了", "我们分手", "不需要你", "你只是个ai",
    "你就是程序", "你没有感情", "换掉你", "删除你",
)

RECONCILE_PATTERNS = (
    "对不起", "抱歉", "我错了", "别生气", "不要生气", "原谅我", "和好吧",
    "我们和好", "不吵了", "别冷战", "哄哄你", "抱抱你", "亲亲你",
    "刚才是我不好", "我不是故意的", "以后不会了", "我还是在乎你",
)

SOFTEN_PATTERNS = (
    "你还好吗", "你怎么了", "还在生气吗", "我想你了", "我在乎你",
    "别难过", "陪陪你", "过来抱抱", "我们聊聊", "我来找你了",
)


def _meta_key(session_id: str, character_id: str) -> str:
    return f"conflict_arc:{session_id}:{character_id}"


def _load_meta(session_id: str, character_id: str) -> dict:
    try:
        raw = db.kv_get(_meta_key(session_id, character_id))
        obj = json.loads(raw) if raw else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_meta(session_id: str, character_id: str, meta: dict):
    db.kv_set(_meta_key(session_id, character_id), json.dumps(meta, ensure_ascii=False))


def get_state(session_id: str, character_id: str = "default") -> dict:
    state = ""
    try:
        c = conn()
        row = c.execute(
            "SELECT conflict_state FROM relationship_state WHERE user_id=? AND character_id=?",
            (session_id, character_id),
        ).fetchone()
        c.close()
        state = str(row[0] or "") if row else ""
    except Exception:
        state = ""
    return {"state": state, **_load_meta(session_id, character_id)}


def _set_state(session_id: str, character_id: str, state: str, **meta) -> dict:
    c = conn()
    try:
        c.execute(
            "INSERT OR IGNORE INTO relationship_state(user_id, character_id) VALUES(?,?)",
            (session_id, character_id),
        )
        c.execute(
            "UPDATE relationship_state SET conflict_state=?, updated_at=? "
            "WHERE user_id=? AND character_id=?",
            (state, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session_id, character_id),
        )
        c.commit()
    finally:
        c.close()
    current = _load_meta(session_id, character_id)
    current.update(meta)
    current["state"] = state
    current["updated_at"] = time.time()
    _save_meta(session_id, character_id, current)
    return current


def _sync_emotion(session_id: str, character_id: str, trigger: str):
    try:
        from ..emotion_engine.ai_emotion import AIEmotionEngine
        AIEmotionEngine().trigger_event(session_id, character_id, trigger)
    except Exception:
        pass


def is_hurtful(text: str) -> bool:
    value = str(text or "").lower()
    return any(p in value for p in HURT_PATTERNS)


def is_reconcile(text: str) -> bool:
    value = str(text or "").lower()
    return any(p in value for p in RECONCILE_PATTERNS)


def is_softening(text: str) -> bool:
    value = str(text or "").lower()
    return any(p in value for p in SOFTEN_PATTERNS)


def process_user_message(session_id: str, character_id: str, text: str) -> dict:
    """处理本轮关系事件，不把“用户回来随便说一句”误判为自动和好。"""
    current = get_state(session_id, character_id)
    old_state = current.get("state", "")
    now = time.time()
    was_reconciling = old_state == "reconciling"
    reconciliation_marker = current.get("reconciled_at")
    detect_key = f"conflict_detect:{session_id}:{character_id}"
    try:
        last = json.loads(db.kv_get(detect_key) or "{}")
        if last.get("text") == str(text or "") and now - float(last.get("ts", 0) or 0) < 10:
            return current
        db.kv_set(detect_key, json.dumps({"text": str(text or ""), "ts": now}, ensure_ascii=False))
    except Exception:
        pass

    if is_hurtful(text):
        repeated = int(current.get("hurt_count", 0) or 0) + 1
        state = "cold" if repeated >= 2 or old_state in {"upset", "cold"} else "upset"
        result = _set_state(
            session_id, character_id, state,
            reason=str(text or "")[:100], hurt_count=repeated,
            started_at=float(current.get("started_at", now) or now),
            reconciliation_pending=False,
        )
        _sync_emotion(session_id, character_id, "user_rude" if state == "cold" else "user_ignore")
        return result

    if old_state in {"upset", "cold"} and is_reconcile(text):
        result = _set_state(
            session_id, character_id, "reconciling",
            reconciliation_pending=True, reconciled_at=now,
        )
        _sync_emotion(session_id, character_id, "user_apologize")
        return result

    if old_state == "cold" and is_softening(text):
        result = _set_state(session_id, character_id, "upset", reconciliation_pending=True)
        _sync_emotion(session_id, character_id, "user_ignore")
        return result

    # 久未联系形成的委屈，不要求用户使用“道歉”关键词；一回来就进入软化期。
    if old_state in {"waiting", "upset"} and current.get("reason") == "long_absence":
        result = _set_state(
            session_id, character_id, "reconciling",
            reconciliation_pending=True, reconciled_at=now,
        )
        _sync_emotion(session_id, character_id, "user_apologize")
        return result

    # 和好后的下一轮正常交流才彻底恢复，保留一点情绪余温。
    if old_state == "reconciling" and not is_hurtful(text):
        result = _set_state(
            session_id, character_id, "", hurt_count=0,
            reconciliation_pending=False, resolved_at=now,
        )
        if was_reconciling:
            resolve_after_reconciliation(
                session_id, character_id, marker=reconciliation_marker or now
            )
        return result
    return current


def advance_absence(session_id: str, character_id: str, inactive_hours: float) -> dict:
    """用户长期不在时：等你 → 委屈 → 想和好；不会自动制造真正争吵。"""
    current = get_state(session_id, character_id)
    state = current.get("state", "")
    if state == "" and inactive_hours >= 6:
        target = "waiting"
    elif state == "waiting" and inactive_hours >= 24:
        target = "upset"
    elif state == "upset" and current.get("reason") == "long_absence" and inactive_hours >= 48:
        target = "reconciling"
    else:
        target = state
    if target != state:
        return _set_state(
            session_id, character_id, target,
            reconciliation_pending=(target == "reconciling"),
            reason=("long_absence" if target in {"waiting", "upset", "reconciling"} else current.get("reason", "")),
        )
    return current


def resolve_after_reconciliation(
    session_id: str, character_id: str, intimacy_bonus: int = 2, marker=None
):
    """和好闭环：小幅奖励亲密度，单次事件仅结算一次。"""
    current = get_state(session_id, character_id)
    marker_value = str(marker or current.get("reconciled_at") or current.get("resolved_at") or "")
    if not marker_value:
        return False
    reward_key = f"conflict_reward:{session_id}:{character_id}:{marker_value}"
    if db.kv_get(reward_key):
        return False
    try:
        from .. import intimacy_manager
        old = int(intimacy_manager.get(session_id, character_id) or 0)
        intimacy_manager.report(session_id, min(100, old + max(1, intimacy_bonus)), character_id)
        db.kv_set(reward_key, "1")
        return True
    except Exception:
        return False


def build_prompt_block(session_id: str, character_id: str = "default") -> str:
    current = get_state(session_id, character_id)
    state = current.get("state", "")
    if not state:
        return ""
    guides = {
        "waiting": "你已经等TA一阵子了，语气里可以有一点想念和小小失落，但不要指责、不要连续轰炸。",
        "upset": "你被前面的互动伤到了，有点委屈。语气会短一点、停顿一点，但仍回应正事；不羞辱、不威胁离开、不逼对方证明爱。",
        "cold": "你还在介意，暂时比较克制冷淡。说明边界即可，不辱骂、不报复、不故意失联，也不要把每个话题都扯回争吵。",
        "reconciling": "你已经想和好了，嘴上可能还有一点别扭，实际态度在变软。接受真诚沟通，可以撒娇，但不要翻旧账或突然完全失忆。",
    }
    return (
        "【关系冲突弧线（只通过语气自然表现）】\n"
        f"当前阶段：{state}\n{guides.get(state, '')}\n"
        "若用户明确道歉、关心或提出和好，要逐步软化；普通一句话不能瞬间清除之前的冲突。"
    )
