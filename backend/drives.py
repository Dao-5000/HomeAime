# -*- coding:utf-8 -*-
"""
内驱力系统（Drives / 行层的动机引擎）
——主动消息的"想要"，以前是静态评分；现在是会生长、会衰减、憋得住也憋不久的变量。

四个内驱力（0~100，全部按 (session_id, character_id) 隔离，存 kv）：
  curiosity   好奇心    —— 想知道后续：未闭环的事、到期的约定越积越好奇
  attachment  依恋      —— 想念：对方离开越久越想念，被回复/互动后缓解
  expressive  表达欲    —— 攒了一肚子想说的话，主动倾诉的冲动
  boredom     无聊度    —— 没人陪、没事做的烦闷，是最朴素的"想找人说话"

工作方式：
  · tick_drives()：由 scheduler 每 tick 调用（内部 5 分钟节流）——
    时间本身就会发酵：越久没聊，想念/无聊慢慢涨，涨到基线为止；
  · 事件增减：聊了天（boredom↓ attachment↓）、主动消息被无视（attachment↑）、
    主动开口（表达欲释放↓）——见 on_user_round / on_proactive_fired；
  · 消费：proactive_decision.build_decision 把 drive 变成候选加分
    （好奇→"continue"、依恋→"care"、表达欲→"share"、无聊→"ask"），
    并在最高驱动力 ≥70 时给 instruction 追加一句"此刻的冲动"。

设计约束：
  · 纯本地计算，零 LLM 调用；kv 读写失败一律静默回落默认值；
  · 只调分数、不夺权——硬门禁（睡觉/免打扰/时间窗）仍在决策引擎前面，
    drive 再高也不会半夜炸醒用户。
"""
import json
import time as _time

# ── 配置 ────────────────────────────────────────────────────────────
DRIVE_KEYS = ("curiosity", "attachment", "expressive", "boredom")

# 无事件时的自然回落基线（回归"平静的想"而不是永远归零）
DRIVE_BASELINE = {"curiosity": 25, "attachment": 30, "expressive": 25, "boredom": 15}
DEFAULT_DRIVES = dict(DRIVE_BASELINE)

MAX_VALUE = 100.0
TICK_THROTTLE = 300      # tick 最小间隔（秒）
_TICK_KEY = "drives_tick:{sid}:{cid}"
_STATE_KEY = "drives:{sid}:{cid}"

_cache = {}   # (sid, cid) → (drives_dict, expire_ts)
_CACHE_TTL = 30

# 驱动力 → 主动消息类型的加分系数（build_decision 消费）
# 加分上限刻意压在 ±15 内：drive 是"推一把"，不是"越权决定"
_TYPE_BONUS = {
    "continue": ("curiosity",   0.15),
    "care":     ("attachment",  0.12),
    "share":    ("expressive",  0.12),
    "ask":      ("boredom",     0.15),
}

# 高驱动力（≥此值）时给 instruction 追加的冲动措辞
_HIGH_DRIVE_HINTS = {
    "curiosity":  "你此刻对这个话题真的很好奇，那种想知道后续的劲头可以自然流露一点",
    "attachment": "你已经有点想TA了，主动消息里可以带一点真实的想念，但别变成查岗",
    "expressive": "你攒了几句想说的话，这次可以说出来，像憋了会儿终于开口",
    "boredom":    "你这会儿有点闲得发慌，想找人说说话的感觉可以有，但别显得无所事事",
}


def _kv_get(key):
    try:
        from .db import kv_get
        return kv_get(key)
    except Exception:
        return None


def _kv_set(key, value):
    try:
        from .db import kv_set
        kv_set(key, value)
    except Exception:
        pass


def _clamp(v):
    try:
        return max(0.0, min(MAX_VALUE, float(v)))
    except Exception:
        return 0.0


def get_drives(session_id: str, character_id: str = "default") -> dict:
    """读当前内驱力（无记录/解析失败 → 默认基线）。"""
    sid = str(session_id or "default")
    cid = str(character_id or "default")
    key = (sid, cid)
    now = _time.time()
    cached = _cache.get(key)
    if cached and now < cached[1]:
        return dict(cached[0])

    drives = dict(DEFAULT_DRIVES)
    raw = _kv_get(_STATE_KEY.format(sid=sid, cid=cid))
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for k in DRIVE_KEYS:
                    if k in data:
                        drives[k] = _clamp(data[k])
        except Exception:
            pass
    _cache[key] = (drives, now + _CACHE_TTL)
    return dict(drives)


def _save_drives(sid: str, cid: str, drives: dict):
    _kv_set(_STATE_KEY.format(sid=sid, cid=cid),
            json.dumps({k: round(_clamp(drives.get(k, 0)), 1) for k in DRIVE_KEYS},
                       ensure_ascii=False))
    _cache[(sid, cid)] = (drives, _time.time() + _CACHE_TTL)


def bump_drives(session_id: str, character_id: str = "default", **deltas) -> dict:
    """按事件增减内驱力（curiosity=+5 这样传），自动夹在 0~100。"""
    sid = str(session_id or "default")
    cid = str(character_id or "default")
    drives = get_drives(sid, cid)
    changed = False
    for k, dv in deltas.items():
        if k not in DRIVE_KEYS or not dv:
            continue
        drives[k] = _clamp(drives[k] + float(dv))
        changed = True
    if changed:
        _save_drives(sid, cid, drives)
    return drives


def _hours_since_last_user_message(sid: str, cid: str) -> float:
    """距用户上一条消息的小时数（内部生成/主动消息不计）。"""
    try:
        from .db import q
        rows = q(
            "SELECT timestamp FROM chat_history "
            "WHERE session_id=? AND character_id=? AND role='user' "
            "ORDER BY id DESC LIMIT 1",
            (sid, cid),
            fetch=True,
        )
        if not rows:
            return 1e9   # 从来没聊过 → 视作很久（新面孔，好奇心/想念按上限方向发酵）
        from datetime import datetime
        ts = str(rows[0][0] or "")[:19]
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
    except Exception:
        return 0.0   # 查不到按刚聊过处理（宁可不涨，不误涨）


def _pending_loops_count(sid: str, cid: str) -> int:
    try:
        from .db import get_open_loops
        loops = get_open_loops(sid, cid, limit=10)
        return len(loops) if isinstance(loops, list) else 0
    except Exception:
        return 0


def tick_drives(session_id: str, character_id: str = "default") -> dict:
    """时间发酵 + 向基线回落。scheduler 每 tick 调，内部 5 分钟节流。"""
    sid = str(session_id or "default")
    cid = str(character_id or "default")
    throttle_key = _TICK_KEY.format(sid=sid, cid=cid)
    now = _time.time()
    try:
        last = float(_kv_get(throttle_key) or 0)
    except Exception:
        last = 0.0
    if now - last < TICK_THROTTLE:
        return get_drives(sid, cid)
    _kv_set(throttle_key, str(int(now)))

    drives = get_drives(sid, cid)
    hours_quiet = _hours_since_last_user_message(sid, cid)

    # ── 时间发酵（上限受"距基线的最大涨幅"约束，不会无界增长）──
    # 依恋：2 小时没聊开始想念，每小时 +2
    if hours_quiet > 2.0:
        drives["attachment"] = _clamp(
            drives["attachment"] + min(8.0, (hours_quiet - 2.0) * 2.0))
    # 无聊：1 小时没人陪开始涨，每小时 +1.5
    if hours_quiet > 1.0:
        drives["boredom"] = _clamp(
            drives["boredom"] + min(6.0, (hours_quiet - 1.0) * 1.5))
    # 好奇心：有未闭环的事就惦记（每条 +1.5，封顶 +9）
    loops = _pending_loops_count(sid, cid)
    if loops > 0:
        drives["curiosity"] = _clamp(drives["curiosity"] + min(9.0, loops * 1.5))

    # ── 向基线回落（每 tick 回 3%，避免旧高峰永远挂着）──
    for k in DRIVE_KEYS:
        base = DRIVE_BASELINE[k]
        drives[k] = _clamp(drives[k] + (base - drives[k]) * 0.03)

    _save_drives(sid, cid, drives)
    return drives


def on_user_round(session_id: str, character_id: str = "default") -> None:
    """用户正常聊了一轮：无聊缓解、想念被安抚、表达欲部分释放。"""
    try:
        bump_drives(session_id, character_id,
                    boredom=-14, attachment=-5, expressive=-4)
    except Exception:
        pass


def on_proactive_fired(session_id: str, character_id: str = "default") -> None:
    """主动开口：表达欲释放、无聊缓解；但没有得到回应的那份想念会由
    on_proactive_ignored 在追问链里加回来。"""
    try:
        bump_drives(session_id, character_id,
                    expressive=-8, boredom=-8, attachment=-2)
    except Exception:
        pass


def on_proactive_ignored(session_id: str, character_id: str = "default") -> None:
    """主动消息发了、用户没理：想念反而更重（越没回越惦记）。"""
    try:
        bump_drives(session_id, character_id, attachment=+8, boredom=+4)
    except Exception:
        pass


def proactive_drive_bonus(drives: dict, action_type: str) -> float:
    """驱动力 → 决策候选加分（proactive_decision.build_decision 消费）。"""
    try:
        key, coef = _TYPE_BONUS.get(str(action_type or ""), (None, 0.0))
        if not key:
            return 0.0
        return round(_clamp(drives.get(key, 0)) * coef, 2)
    except Exception:
        return 0.0


def top_drive(drives: dict):
    """返回 (键名, 值)。没有 ≥70 的高驱动力时返回 (None, 0)。"""
    try:
        k = max(DRIVE_KEYS, key=lambda x: drives.get(x, 0))
        v = _clamp(drives.get(k, 0))
        return (k, v) if v >= 70 else (None, 0.0)
    except Exception:
        return (None, 0.0)


def drive_hint(drives: dict) -> str:
    """高驱动力 → 给主动消息 instruction 追加的一句"此刻的冲动"。"""
    k, _v = top_drive(drives)
    return _HIGH_DRIVE_HINTS.get(k, "") if k else ""
