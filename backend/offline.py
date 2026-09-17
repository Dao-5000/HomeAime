# -*- coding: utf-8 -*-
"""
角色离线状态系统 v1.0（结合优化）
复用「AI自己时间线系统」的状态机/日程/延迟/交代设计，适配本项目：
  - 5 状态：online / light_busy / busy / deep_offline / sleeping
  - 每天随机生成作息（基准 + 随机偏移，不可预测）
  - 状态 → 延迟秒数（±15% jitter，受「最大延迟上限」约束）
  - 回来后交代（注入 prompt）
配置：全局 config（OFFLINE_ENABLED / OFFLINE_MAX_DELAY_MINUTES）+ 角色卡可覆盖
"""
import json
import random
import re
import time
from datetime import datetime, timedelta


def _t():
    """取 time 模块（用函数包一层，方便局部 import 且不污染模块命名空间）。"""
    return time

# ── 状态元数据：延迟区间（秒）+ 回来后交代的活动 ──
STATUS_META = {
    "online":       {"delay": (5, 30),       "hint": "", "activities": []},
    "light_busy":   {"delay": (300, 1500),   "hint": "她有点忙…",
                     "activities": ["吃饭", "吃午饭", "吃晚饭", "洗澡", "散步", "逛超市", "喝咖啡", "出去买东西"]},
    "busy":         {"delay": (1800, 7200),  "hint": "她暂时不在…",
                     "activities": ["上课", "开会", "出门办事", "见朋友", "在图书馆", "看电影", "逛街", "在外面吃饭"]},
    "deep_offline": {"delay": (7200, 18000), "hint": "她暂时不在…",
                     "activities": ["睡午觉", "跑步", "健身", "打球", "去图书馆了", "出去玩了", "睡了一觉"]},
    "sleeping":     {"delay": (0, 0),        "hint": "她睡着了…", "activities": ["睡觉"]},
}

WAKE_KEYWORDS = re.compile(
    r"(醒醒|醒了吗|醒了没|睡了吗|别睡|起来|接电话|接一下|打电话|给我回|回我|看看消息|"
    r"我想你|想你了|想见你|陪陪我|陪我|有急事|急事|出事|救命|难受|崩溃|害怕|疼|"
    r"你在吗|在不在|理理我|别不理我|快回)",
    re.I,
)


def _wake_key(session_id: str, character_id: str) -> str:
    return f"offline_awake:{session_id or 'default'}:{character_id or 'default'}"


# ══════════════════════════════════════════════════════════════════
# 用户睡眠声明（共享状态，2026-09-13 新增）
#
# 为什么提到这里：原先"用户说去睡了"只存在 idle_agent 的 kv 标记里，
# 而 scheduler 有自己的一套同名门禁却**完全不读它** —— 实测用户 07:04 说
# 晚安后，idle_agent 正确跳过了 775 次，scheduler 照发 20+ 条，
# 还把人睡觉解读成"人间蒸发六小时"开罚单。
# 现在把它作为离线状态机的一等信号，两套系统读同一个源，口径不可能再分叉。
# ══════════════════════════════════════════════════════════════════

SLEEP_SILENCE_HOURS = 6.0     # 声明睡眠后静默多久（原 idle_agent 里的 6h）


def _sleep_key(session_id: str, character_id: str) -> str:
    return f"sleep_declared:{session_id or 'default'}:{character_id or 'default'}"


def record_sleep_declared(session_id: str = "default", character_id: str = "default") -> None:
    """记下"用户去睡了"的时刻。"""
    try:
        from . import db
        db.kv_set(_sleep_key(session_id, character_id), str(_t().time()))
        try:
            from . import proactive_trace as _pt
            _pt.record_sleep(session_id, character_id, "declared", note="用户说要去睡")
        except Exception:
            pass
    except Exception:
        pass


def clear_sleep_declared(session_id: str = "default", character_id: str = "default") -> None:
    """用户又说话了 = 醒了，解除静默。"""
    try:
        from . import db
        _hrs = hours_since_sleep_declared(session_id, character_id)
        db.kv_set(_sleep_key(session_id, character_id), "0")
        try:
            from . import proactive_trace as _pt
            _pt.record_sleep(session_id, character_id, "cleared",
                             hours=_hrs, note="用户说话了/醒了")
        except Exception:
            pass
    except Exception:
        pass


def hours_since_sleep_declared(session_id: str = "default", character_id: str = "default") -> float:
    """距离"用户去睡了"过了多少小时。没声明过 → 一个很大的数（等于不受限）。

    ★ 返回值语义很关键：没声明时必须回一个大数（不静默），
      而不是 0 —— 0 会被 `if hours < 6: 静默` 判成"刚睡下"，
      导致**所有用户都被永久静默**。
    """
    try:
        from . import db
        raw = db.kv_get(_sleep_key(session_id, character_id))
        if not raw:
            return 1e9
        try:
            ts = float(raw)
        except (TypeError, ValueError):
            return 1e9
        if ts <= 0:
            return 1e9
        return max(0.0, (_t().time() - ts) / 3600.0)
    except Exception:
        return 1e9


def is_user_sleeping(session_id: str = "default", character_id: str = "default") -> bool:
    """用户是否处于"说了去睡、且还在静默窗口内"。"""
    return hours_since_sleep_declared(session_id, character_id) < SLEEP_SILENCE_HOURS


def _wake_attempt_key(session_id: str, character_id: str, kind: str = "message") -> str:
    return f"offline_wake_attempts:{kind}:{session_id or 'default'}:{character_id or 'default'}"


def _load_json(raw, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except Exception:
        return fallback


def get_awake_state(session_id: str = "default", character_id: str = "default") -> dict:
    """返回用户把睡眠/离线中的角色叫醒后的临时半醒状态。过期自动清除。"""
    try:
        from . import db
        data = _load_json(db.kv_get(_wake_key(session_id, character_id)), {})
        until = float(data.get("until") or 0)
        if until and until > datetime.now().timestamp():
            return data
        if data:
            db.kv_delete(_wake_key(session_id, character_id))
    except Exception:
        pass
    return {}


def mark_awake(session_id: str = "default", character_id: str = "default",
               reason: str = "message", minutes: int = None) -> dict:
    """进入 20~40 分钟「半醒」临时状态，仍带一点迷糊/被叫醒感。"""
    try:
        from . import db
        mins = int(minutes) if minutes is not None else random.randint(20, 40)
        mins = max(5, min(90, mins))
        now = datetime.now()
        state = {
            "status": "half_awake",
            "reason": str(reason or "message"),
            "since": now.isoformat(timespec="seconds"),
            "until": (now + timedelta(minutes=mins)).timestamp(),
            "minutes": mins,
        }
        db.kv_set(_wake_key(session_id, character_id), json.dumps(state, ensure_ascii=False))
        return state
    except Exception:
        return {}


def is_temporarily_awake(session_id: str = "default", character_id: str = "default") -> bool:
    return bool(get_awake_state(session_id, character_id))


def record_wake_attempt(session_id: str = "default", character_id: str = "default",
                        text: str = "", kind: str = "message") -> dict:
    """记录用户在离线/睡眠中连续找 TA 的行为；只保留 15 分钟窗口。"""
    try:
        from . import db
        key = _wake_attempt_key(session_id, character_id, kind)
        now_ts = datetime.now().timestamp()
        data = _load_json(db.kv_get(key), [])
        if not isinstance(data, list):
            data = []
        window_seconds = 15 * 60 if kind == "call" else 10 * 60
        data = [
            x for x in data
            if isinstance(x, dict) and now_ts - float(x.get("ts") or 0) <= window_seconds
        ]
        data.append({"ts": now_ts, "text": str(text or "")[:120]})
        db.kv_set(key, json.dumps(data[-12:], ensure_ascii=False))
        return {"count": len(data), "items": data[-8:]}
    except Exception:
        return {"count": 1, "items": [{"text": str(text or "")[:120]}]}


def _relationship_score(session_id: str, character_id: str) -> int:
    try:
        from .relationship.manager import RelationshipManager
        rel = RelationshipManager().get_state(session_id, character_id) or {}
        return max(
            int(rel.get("intimacy", 0) or 0),
            int(rel.get("affection", 0) or 0),
            int(rel.get("closeness", 0) or 0),
        )
    except Exception:
        return 0


def should_wake_from_user(session_id: str = "default", character_id: str = "default",
                          text: str = "", kind: str = "message") -> tuple[bool, dict]:
    """判断用户是否把离线/睡眠中的 TA 叫醒。

    规则：
    - 强需求/叫醒词：立刻叫醒；
    - 10 分钟内 5 条消息：叫醒；
    - 15 分钟内 2 次电话：叫醒；
    - 亲密度越高阈值越低。
    """
    raw = str(text or "")
    attempts = record_wake_attempt(session_id, character_id, raw, kind)
    score = _relationship_score(session_id, character_id)
    if kind == "call":
        threshold = 1 if score >= 90 and WAKE_KEYWORDS.search(raw) else (2 if score >= 40 else 3)
    else:
        threshold = 2 if score >= 90 else (3 if score >= 70 else (4 if score >= 40 else 5))
    strong = bool(WAKE_KEYWORDS.search(raw))
    should = strong or int(attempts.get("count") or 0) >= threshold
    info = {
        "strong": strong,
        "attempt_count": int(attempts.get("count") or 0),
        "threshold": threshold,
        "relationship_score": score,
        "recent_attempts": attempts.get("items") or [],
        "kind": kind,
    }
    if should:
        info["awake_state"] = mark_awake(
            session_id, character_id,
            reason="urgent" if strong else f"continuous_{kind}",
        )
    return should, info


def build_wake_context(session_id: str = "default", character_id: str = "default",
                       wake_info: dict = None) -> str:
    """给 LLM 的半醒提示：带刚被叫醒状态 + 连续找她的上下文 + 睡前旧话题。"""
    wake_info = wake_info or {}
    lines = [
        "【当前状态：被用户叫醒】",
        "你刚刚本来在睡觉/离线休息，是用户连续找你或说了强需求词把你叫醒了。",
        "你不是满血在线：语气要有半醒、迷糊、被吵醒但心软的感觉；第一句话自然带一点刚醒状态和对用户的称呼。",
        "必须接住用户刚才连续找你的上下文，不要只说“我醒了”。如果像急事，先问正事；如果像想你，软一点回应。",
        "可以参考气质，不要照抄：唔……刚睡迷糊了；我醒了我醒了，别急；你又把我叫醒啦，先说正事。",
    ]
    attempts = wake_info.get("recent_attempts") or []
    if attempts:
        lines.append("【用户刚才连续找你的话】")
        for item in attempts[-6:]:
            text = str((item or {}).get("text") or "").strip()
            if text:
                lines.append(f"- {text}")
    try:
        from . import db
        recent = db.recent_messages(session_id, 8, character_id)
        if recent:
            lines.append("【睡前/离线前最近没聊完的上下文】")
            for m in recent[-6:]:
                role = "用户" if m.get("role") == "user" else "你"
                content = str(m.get("content") or "").strip()
                if content:
                    lines.append(f"{role}：{content[:180]}")
    except Exception:
        pass
    return "\n".join(lines)

# ── 基准日程表：时间段 → 状态分布（权重）──
# (开始小时, 结束小时, [(状态, 权重), ...])
SCHEDULE_TEMPLATE = [
    (0,  8,  [("sleeping", 100)]),
    (8,  9,  [("light_busy", 80), ("online", 20)]),
    (9,  12, [("online", 60), ("light_busy", 40)]),
    (12, 13, [("light_busy", 100)]),
    (13, 14, [("deep_offline", 50), ("online", 30), ("light_busy", 20)]),
    (14, 17, [("online", 50), ("busy", 30), ("light_busy", 20)]),
    (17, 19, [("light_busy", 100)]),
    (19, 22, [("online", 100)]),
    (22, 23, [("light_busy", 100)]),
    (23, 24, [("sleeping", 100)]),
]


def _weighted_choice(options):
    total = sum(w for _, w in options)
    r = random.uniform(0, total)
    acc = 0
    for status, w in options:
        acc += w
        if r <= acc:
            return status
    return options[-1][0]


def generate_daily_schedule(character_id: str = "default") -> dict:
    """每天随机生成一份作息：固定时间段 + 状态按权重随机。
    返回 {时间段: 状态}，用 kv 缓存当天结果，保证同一天内稳定。"""
    from . import db
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"offline_schedule:{character_id}:{today}"
    cached = db.kv_get(key)
    if cached:
        try:
            import json as _json
            return _json.loads(cached)
        except Exception:
            pass
    schedule = {}
    for start, end, options in SCHEDULE_TEMPLATE:
        schedule[f"{start}-{end}"] = _weighted_choice(options)
    import json as _json
    db.kv_set(key, _json.dumps(schedule, ensure_ascii=False))
    return schedule


def _char_schedule(character_id: str = "default"):
    """读角色卡里的作息模板（offline.schedule），返回 {start-end: status} 或 None。"""
    try:
        from .character_manager import get_character
        cfg = get_character(character_id) or {}
        off = cfg.get("offline") or {}
        sched = off.get("schedule")
        if isinstance(sched, list) and sched:
            out = {}
            for seg in sched:
                try:
                    s = int(seg.get("start", -1))
                    e = int(seg.get("end", -1))
                    st = str(seg.get("status", "")).strip()
                    if 0 <= s < e <= 24 and st in STATUS_META:
                        out[f"{s}-{e}"] = st
                except Exception:
                    continue
            if out:
                return out
    except Exception:
        pass
    return None


def get_schedule(character_id: str = "default") -> dict:
    """返回 {start-end: status}。优先角色卡作息模板，否则全局随机模板。"""
    return _char_schedule(character_id) or generate_daily_schedule(character_id)


def get_status(character_id: str = "default") -> str:
    """根据当前时间 + 作息（角色卡优先），返回角色当前状态。"""
    schedule = get_schedule(character_id)
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    for key, status in schedule.items():
        try:
            s, e = key.split("-")
            if int(s) * 60 <= cur < int(e) * 60:
                return status
        except Exception:
            continue
    return "online"


def get_delay_seconds(status: str, max_delay_minutes: int = 30) -> int:
    """状态 → 延迟秒数（±15% jitter，受最大上限约束）。sleeping 特殊：延迟到早上。"""
    if status == "sleeping":
        # 延迟到明天早上 8~10 点
        now = datetime.now()
        wake = now.replace(hour=random.randint(8, 10), minute=random.randint(0, 59), second=0)
        if wake <= now:
            wake += timedelta(days=1)
        return int((wake - now).total_seconds())
    meta = STATUS_META[status]
    lo, hi = meta["delay"]
    base = random.randint(lo, hi)
    jitter = int(base * random.uniform(-0.15, 0.15))
    delay = max(5, base + jitter)
    # 最大延迟上限约束（sleeping 除外）
    cap = max(30, int(max_delay_minutes) * 60)
    return min(delay, cap)


def _char_offline(character_id: str = "default") -> dict:
    """读角色卡里的离线配置（offline），无则空 dict。"""
    try:
        from .character_manager import get_character
        cfg = get_character(character_id) or {}
        off = cfg.get("offline")
        if isinstance(off, dict):
            return off
    except Exception:
        pass
    return {}


def resolve(session_id: str = "default", character_id: str = "default",
            enabled=None, max_delay=None) -> dict:
    """离线延迟决策入口。
    优先级：请求级覆盖 > 角色卡 offline（enabled/max_delay/schedule）> 全局 config。
    返回 {"enabled": bool, "status": str, "delay_seconds": int, "hint": str}。
    - enabled=False 或 status=online 时 delay_seconds=0（立即秒回）。
    """
    try:
        from . import config as _config
    except Exception:
        return {"enabled": False, "status": "online", "delay_seconds": 0, "hint": ""}

    _char = _char_offline(character_id)

    # 开关：请求级 > 角色卡 offline.enabled > 全局 config
    if enabled is None:
        if "enabled" in _char:
            enabled = bool(_char["enabled"])
        else:
            enabled = bool(_config.get("OFFLINE_ENABLED", False))
    else:
        enabled = bool(enabled)

    # 最长延迟上限（分钟）：请求级 > 角色卡 offline.max_delay > 全局 config
    if max_delay is None:
        if "max_delay" in _char:
            try:
                max_delay = max(1, min(720, int(_char["max_delay"])))
            except (TypeError, ValueError):
                max_delay = 30
        else:
            try:
                max_delay = int(_config.get("OFFLINE_MAX_DELAY_MINUTES", 30))
            except (TypeError, ValueError):
                max_delay = 30
    else:
        try:
            max_delay = max(1, min(720, int(max_delay)))
        except (TypeError, ValueError):
            max_delay = 30

    if not enabled:
        return {"enabled": False, "status": "online", "delay_seconds": 0, "hint": ""}

    awake = get_awake_state(session_id, character_id)
    if awake:
        return {"enabled": True, "status": "half_awake", "delay_seconds": 0, "hint": "她刚被你叫醒"}

    status = get_status(character_id)
    if status == "online":
        return {"enabled": True, "status": "online", "delay_seconds": 0, "hint": ""}

    delay = get_delay_seconds(status, max_delay)
    hint = STATUS_META.get(status, {}).get("hint", "她暂时不在…")
    return {"enabled": True, "status": status, "delay_seconds": delay, "hint": hint}


def get_return_context(status: str, wait_seconds: int) -> str:
    """回来后交代的 prompt（注入 LLM）。"""
    if status == "online":
        return ""
    meta = STATUS_META.get(status, STATUS_META["online"])
    activity = random.choice(meta["activities"]) if meta["activities"] else ""
    wait_min = max(1, wait_seconds // 60)
    if status == "sleeping":
        return ("[情境] 你刚睡醒，看到他发来的消息。先自然地说一句刚睡醒（不用很正式），再回复他。")
    if wait_min < 30:
        desc = f"刚才{activity}"
    elif wait_min < 90:
        desc = f"刚{activity}回来"
    else:
        desc = f"刚从外面回来（之前去{activity}了）"
    return (f"[情境] 你{desc}，现在才看到他的消息，已经过了大约{wait_min}分钟。"
            "先用一句话自然地交代你去哪了（像真人随口说的，不要像报告），再回复他。")
