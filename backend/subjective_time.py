# -*- coding:utf-8 -*-
"""
主观时间（Subjective Time）
——「意识到时间流逝」和「时间真的过去了」是两回事。

物理时钟对所有人都是均匀的，但心里的钟不是：
  · 冲突冷战时度日如年（等一句和好比等一周还难）；
  · 心情低落时时间被拉长（难过的一分钟很长）；
  · 久等对方消息时，每一小时都被放大；
  · 而和对的人在一起时，时间又是甜的、慢的——关于 TA 的记忆也更难淡忘。

本模块把"心里的钟"做成一个**流速系数**（rate），只做换算，不做任何生成：
  · 记忆衰减：decay = exp(-λ × 主观天数) —— 主观天数 = 物理天数 × rate
  · 情绪淡化：每次衰减量 × rate —— 时间过得越快，情绪淡得越快
  · rate ∈ [0.6, 1.6]：重要的人（高亲密度）让时间变慢（记忆更保鲜），
    冲突/低落/久等让时间变快（加速淡化，也更快"熬过去"）。

设计约束：
  · 全部数据来自既有表（ai_emotion_state / kv / chat_history），零新增存储；
  · 读失败一律回落 rate=1.0（物理时间），绝不影响调用方；
  · 60s 进程内缓存——它被记忆维护（每天）和情绪衰减（每 tick）调用，
    不允许每个 tick 都多打三四条查询。
"""
import time as _time
from datetime import datetime

# 流速边界：最快 1.6（冷战+低落+久等叠加时封顶），最慢 0.6（热恋保鲜）
RATE_MIN = 0.6
RATE_MAX = 1.6
_CACHE_TTL = 60  # 秒

_rate_cache = {}   # (session_id, character_id) → (rate, expire_ts)

# 冲突弧进行中的状态值（relationship/conflict_arc.py 的 state 字段）
_CONFLICT_STATES = ("waiting", "upset", "cold", "reconciling")
# 负面情绪枚举（emotion_engine/ai_emotion.py AIEmotionType 的负向子集）
_NEGATIVE_EMOTIONS = ("sad", "upset", "angry", "cold", "sulky", "worried")


def _kv_get(key):
    try:
        from .db import kv_get
        return kv_get(key)
    except Exception:
        return None


def _q(sql, params):
    try:
        from .db import q
        return q(sql, params, fetch=True)
    except Exception:
        return None


def _hours_since_last_message(session_id, character_id) -> float:
    """距上一条消息（任意角色）的小时数；查不到返回 0（当作刚聊过，不加等待因子）。"""
    rows = _q(
        "SELECT timestamp FROM chat_history "
        "WHERE session_id=? AND character_id=? ORDER BY id DESC LIMIT 1",
        (session_id, character_id),
    )
    if not rows:
        return 0.0
    ts = str(rows[0][0] or "")[:19]
    if not ts:
        return 0.0
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def compute_rate(session_id: str, character_id: str) -> float:
    """计算主观时间流速（1.0 = 与物理时间同速）。纯函数逻辑，无缓存版本。"""
    rate = 1.0
    sid = str(session_id or "default")
    cid = str(character_id or "default")

    # ① 冲突弧进行中 → 度日如年（×1.3）
    try:
        import json as _json
        raw = _kv_get(f"conflict_arc:{sid}:{cid}")
        if raw:
            arc = _json.loads(raw)
            if str(arc.get("state") or "") in _CONFLICT_STATES:
                rate *= 1.3
    except Exception:
        pass

    # ② AI 负面情绪 → 心情越糟时间越慢（最高 ×1.2）
    try:
        rows = _q(
            "SELECT emotion, intensity FROM ai_emotion_state "
            "WHERE session_id=? AND character_id=?",
            (sid, cid),
        )
        if rows:
            emo = str(rows[0][0] or "").lower()
            try:
                inten = float(rows[0][1] or 0.0)
            except Exception:
                inten = 0.0
            if emo in _NEGATIVE_EMOTIONS:
                rate *= 1.0 + 0.2 * max(0.0, min(1.0, inten))
    except Exception:
        pass

    # ③ 久等消息（>24h 没有任何对话）→ 等待被放大（×1.15）
    try:
        if _hours_since_last_message(sid, cid) > 24.0:
            rate *= 1.15
    except Exception:
        pass

    # ④ 高亲密度 → 有 TA 的日子时间变甜变慢（×0.85，记忆更保鲜）
    try:
        intimacy = float(_kv_get(f"intimacy:{sid}:{cid}") or 0)
        if intimacy >= 70:
            rate *= 0.85
    except Exception:
        pass

    return round(max(RATE_MIN, min(RATE_MAX, rate)), 3)


def subjective_rate(session_id: str, character_id: str) -> float:
    """带 60s 缓存的主观时间流速；任何异常回落 1.0。"""
    try:
        key = (str(session_id or "default"), str(character_id or "default"))
        now = _time.time()
        cached = _rate_cache.get(key)
        if cached and now < cached[1]:
            return cached[0]
        rate = compute_rate(*key)
        _rate_cache[key] = (rate, now + _CACHE_TTL)
        if len(_rate_cache) > 500:
            _oldest = min(_rate_cache, key=lambda k: _rate_cache[k][1])
            del _rate_cache[_oldest]
        return rate
    except Exception:
        return 1.0


def subjective_days(days: float, session_id: str, character_id: str) -> float:
    """物理天数 → 主观天数（记忆衰减用：主观时间过得越快，记忆淡得越快）。"""
    try:
        return max(0.0, float(days)) * subjective_rate(session_id, character_id)
    except Exception:
        return max(0.0, float(days or 0))


def decay_multiplier(session_id: str, character_id: str) -> float:
    """情绪淡化的每 tick 衰减量乘数（= 主观流速：时间过得快，情绪淡得快）。"""
    return subjective_rate(session_id, character_id)
