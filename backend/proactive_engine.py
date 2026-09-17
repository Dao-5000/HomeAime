# -*- coding: utf-8 -*-
"""主动消息**单一引擎**（2026-09-15 重做）。

为什么重做：同一件事此前有**三套实现** —— 前端 `proactiveTick` 自己计时并生成、
`idle_agent` 自己计时、`scheduler` 自己一套闸门。三者口径互相漂移，后果实测：
  · 用户设「可用时段 20:00-01:44」「间隔 90-120 分」不起效果（多条链路绕闸门）
  · 前端生成完整条 42k 上下文的主动消息，后端才拦下 → 每 11 分钟白烧一次
  · App 一重启内存计时归零，"距上次主动"被低估
现在：**一个引擎**管住"什么时候能说、说什么、怎么记账"，其余链路只当内容来源。

用户定义的节奏（本引擎的主干，必须逐字实现）：
    用户说话 ──────────────────────────► 打断 + 整个重置
      AI 最后一句 ──► 静默窗口（默认 5 分钟，可配）
                          │ 期间用户说话 → 重置
                          │ AI 主动延续话题（+2 分钟）算对话延续 → 窗口重新起算
                          ▼
                      判定「这轮聊完了」──► 开始间隔计时（设置里 90–120 分钟随机）
                          ▼
                      到点 + 通过 时段 / 免打扰 / 每日上限 ──► 允许开口（模型自己决定说不说）

状态持久化在 kv（`proactive_fsm:{session}:{character}`）：重启不丢计时。
总开关 `PROACTIVE_ENGINE_ENABLED`（默认 true）：置 false 立即回退旧口径。
"""
import json
import random
import time
from datetime import datetime

# ── 豁免口径（与人格设置页文案一致，全项目唯一一份）──────────────
# 全豁免（连时段也不看）：用户自设提醒 / 到点承诺 / 危机 / 测试按钮
EXEMPT_TYPES = {"pending_task", "reminder", "promise", "crisis"}
# 只豁免间隔（仍须落在可用时段内）：开屏问候 / 话题延续 / 用户当场索要的语音 / 早晚安信件
INTERVAL_ONLY_TYPES = {"startup", "topic_continue", "morning", "night",
                       "good_morning", "good_night", "festival", "milestone"}

_DEFAULTS = {
    "PROACTIVE_ENGINE_ENABLED": True,
    "PROACTIVE_QUIET_WINDOW_SEC": 300,      # "不继续聊窗口器"：AI 最后一句后多久算聊完
    "PROACTIVE_DAILY_CAP": 8,               # 每日"找话说"上限（提醒/承诺不计入）
    "PROACTIVE_CAP_EXCLUDE_EXEMPT": True,   # 上限是否排除提醒/承诺/危机
    "PROACTIVE_SMALL_CTX": True,            # 闲聊类用小上下文生成（省 token）
}
# ★ 2026-09-16 删除 PROACTIVE_HELD_TTL_HOURS + held（攒着没说）机制：
#   旧行为是"被闸门拦住时也先把话说出来、存起来等窗口开了再补"，
#   结果是话按几小时前的语境生成、却在新时刻发出去（用户实测："晚饭吃了没"半夜发），
#   而且每次被拦都白跑一次 LLM。用户拍板：**只有到点才生成**。

# 模型可以主动表示"这次没什么想说的"：命中则本轮不发，并重新计时
SILENCE_MARKERS = ("[[不说话]]", "[[沉默]]", "[[不说]]")


def is_silence(text: str) -> bool:
    """模型是否表达了"这次不说"（约定标记，任意位置命中即算）。"""
    t = str(text or "").strip()
    if not t:
        return True
    return any(m in t for m in SILENCE_MARKERS)


def _cfg(key, default=None):
    try:
        from . import config
        v = config.get(key, _DEFAULTS.get(key, default))
        return _DEFAULTS.get(key, default) if v in (None, "") else v
    except Exception:
        return _DEFAULTS.get(key, default)


def engine_enabled() -> bool:
    try:
        return bool(_cfg("PROACTIVE_ENGINE_ENABLED", True))
    except Exception:
        return True


def quiet_window_sec() -> float:
    try:
        return max(60.0, float(_cfg("PROACTIVE_QUIET_WINDOW_SEC", 300)))
    except Exception:
        return 300.0


def daily_cap() -> int:
    try:
        return max(0, int(_cfg("PROACTIVE_DAILY_CAP", 8)))
    except Exception:
        return 8


def cap_excludes_exempt() -> bool:
    try:
        return bool(_cfg("PROACTIVE_CAP_EXCLUDE_EXEMPT", True))
    except Exception:
        return True


def model_decides(cid: str) -> bool:
    """「主动回复由模型自主」（角色卡字段 `proactive_model_decides`，人格设置里的开关）。

    ★ 2026-09-16 用户拍板新增：
      开 = 跳过节奏状态机（不再等 90–120 分钟），由模型自己决定"什么时候想说、说什么"；
          但**免打扰/睡眠/离线/可用时段/每日上限照旧生效**（用户底线：时段外一条都不许发）。
      关（默认）= 按全局「主动发言间隔」计时，到点才问模型。
    带缓存：decide() 每轮都会问，避免频繁读角色卡文件；TTL 10 秒，
    并且保存角色卡时会调 invalidate_model_decides() 立即失效（开关点了马上生效）。
    """
    now = time.time()
    try:
        _hit = _MODEL_DECIDES_CACHE.get(cid)
        if _hit and now - _hit[0] < 10:
            return bool(_hit[1])
    except Exception:
        pass
    val = False
    try:
        from . import character_manager as _cm
        card = _cm.get_character_any(cid) or {}
        val = bool(card.get("proactive_model_decides"))
    except Exception:
        val = False
    try:
        _MODEL_DECIDES_CACHE[cid] = (now, val)
    except Exception:
        pass
    return val


def invalidate_model_decides(cid: str = "") -> None:
    """角色卡保存后调用：让「主动回复由模型自主」开关立刻生效（不用等 TTL）。"""
    try:
        if cid:
            _MODEL_DECIDES_CACHE.pop(cid, None)
        else:
            _MODEL_DECIDES_CACHE.clear()
    except Exception:
        pass


_MODEL_DECIDES_CACHE = {}


def small_ctx_enabled() -> bool:
    try:
        return bool(_cfg("PROACTIVE_SMALL_CTX", True))
    except Exception:
        return True


# ── 状态存取（kv，跨重启）──────────────────────────────────────
def _state_key(sid: str, cid: str) -> str:
    return f"proactive_fsm:{sid or 'default'}:{cid or 'default'}"


_PLACEHOLDER_CIDS = {"", "default", "?", "??", "none", "null"}


def effective_character_id(sid: str, cid: str = "") -> str:
    """把退化的角色名（'' / 'default' / '??'）自愈成"该会话最近说话的真实角色"。

    ★ 2026-09-15 实测：scheduler / idle_agent / 前端 register 都可能带着
      `character_id='default'` 来判定 —— 于是"可用时段"读的是**兜底 08:00-23:00**，
      消息也被记到 default 名下（用户在自己的角色会话里看不到、时段设置也不生效）。
      在引擎这一层统一自愈，所有调用方一次性受益（不必逐个链路去改）。
    """
    c = str(cid or "").strip()
    if c and c.lower() not in _PLACEHOLDER_CIDS:
        return c
    try:
        from . import db
        for sql, args in (
            ("SELECT character_id FROM chat_history WHERE session_id=? "
             "AND character_id NOT IN ('default','?','??','') ORDER BY id DESC LIMIT 1", (sid,)),
            ("SELECT character_id FROM chat_history "
             "WHERE character_id NOT IN ('default','?','??','') ORDER BY id DESC LIMIT 1", ()),
        ):
            rows = db.q(sql, args, fetch=True)
            if not rows:
                continue
            try:
                _r = str(rows[0]["character_id"] or "").strip()
            except Exception:
                _r = str(rows[0].get("character_id") or "").strip()
            if _r and _r.lower() not in _PLACEHOLDER_CIDS:
                return _r
    except Exception:
        pass
    return c or "default"


def _load(sid: str, cid: str) -> dict:
    try:
        from . import db
        raw = db.kv_get(_state_key(sid, cid))
        st = json.loads(raw) if raw else {}
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _save(sid: str, cid: str, st: dict) -> None:
    try:
        from . import db
        db.kv_set(_state_key(sid, cid), json.dumps(st, ensure_ascii=False))
    except Exception:
        pass


def interval_pair_sec(character_id: str = "") -> tuple:
    """间隔区间（秒）= 用户设的「主动发言间隔」（分钟）。取不到给 90–120 分。"""
    try:
        from . import config
        lo_min, hi_min = config.proactive_interval_pair(character_id)
        lo, hi = float(lo_min or 0) * 60.0, float(hi_min or 0) * 60.0
        if lo <= 0 and hi <= 0:
            return (90 * 60.0, 120 * 60.0)
        if hi < lo:
            hi = lo
        return (lo, hi)
    except Exception:
        return (90 * 60.0, 120 * 60.0)


def _roll_interval(cid: str) -> float:
    lo, hi = interval_pair_sec(cid)
    return random.uniform(lo, hi) if hi > lo else lo


def _cold_start(sid: str, cid: str, now: float) -> dict:
    """没有状态时，用消息库/共享标记推出一个合理的起点（不凭空立刻开口）。"""
    st = {"phase": "waiting", "last_user_at": 0.0, "last_ai_at": 0.0,
          "settle_until": 0.0, "next_allowed_at": 0.0, "last_proactive_at": 0.0,
          "created_at": now}
    try:
        from . import db
        rows = db.recent_messages(sid, 4, cid) or []
        for r in reversed(rows):
            _ts = str(r.get("timestamp") or "")
            try:
                _t = datetime.fromisoformat(_ts).timestamp()
            except Exception:
                _t = 0.0
            if not _t:
                continue
            if str(r.get("role")) == "user":
                st["last_user_at"] = max(st["last_user_at"], _t)
            else:
                st["last_ai_at"] = max(st["last_ai_at"], _t)
    except Exception:
        pass
    try:
        from .proactive_quality import last_push_time
        _lp = float(last_push_time(sid, cid) or 0)
        if _lp > 0:
            st["last_proactive_at"] = _lp
    except Exception:
        pass
    _base = max([x for x in (st["last_user_at"], st["last_ai_at"], st["last_proactive_at"]) if x] or [0])
    if _base <= 0:
        # 完全没有历史：给一个完整间隔期，避免刚启动就冒一条
        st["phase"] = "waiting"
        st["next_allowed_at"] = now + _roll_interval(cid)
    else:
        st["phase"] = "waiting"
        st["next_allowed_at"] = _base + _roll_interval(cid)
    return st


def state(sid: str, cid: str, *, advance_it: bool = True) -> dict:
    """读状态（默认顺带推进 FSM）。返回带计算字段的 dict。"""
    now = time.time()
    st = _load(sid, cid) or _cold_start(sid, cid, now)
    if advance_it:
        st = _advance(sid, cid, st, now)
    lo, hi = interval_pair_sec(cid)
    q = quiet_window_sec()
    out = dict(st)
    out.update({
        "now": now,
        "quiet_window_sec": q,
        "interval_sec": [lo, hi],
        "seconds_to_next": max(0, int(float(st.get("next_allowed_at") or 0) - now)),
        "settle_remaining": max(0, int(float(st.get("settle_until") or 0) - now)),
        "cap": daily_cap(),
        "sent_today": daily_count(sid, cid),
    })
    return out


def _advance(sid: str, cid: str, st: dict, now: float) -> dict:
    """FSM 推进：conversing → settling → waiting → ready。"""
    q = quiet_window_sec()
    phase = str(st.get("phase") or "")
    changed = False
    if phase == "conversing":
        _lu = float(st.get("last_user_at") or 0)
        if _lu and now >= _lu + q:
            st["next_allowed_at"] = now + _roll_interval(cid)
            st["phase"] = "waiting"
            changed = True
    elif phase == "settling":
        _su = float(st.get("settle_until") or 0)
        if not _su or now >= _su:
            st["next_allowed_at"] = now + _roll_interval(cid)
            st["phase"] = "waiting"
            changed = True
    elif phase == "waiting":
        _na = float(st.get("next_allowed_at") or 0)
        if _na and now >= _na:
            st["phase"] = "ready"
            changed = True
        elif not _na:
            st["next_allowed_at"] = now + _roll_interval(cid)
            changed = True
    elif not phase:
        st.update(_cold_start(sid, cid, now))
        changed = True
    # 旧状态里可能残留 held 字段（2026-09-16 前的版本写的）：读到就顺手清掉
    if "held" in st:
        st.pop("held", None)
        changed = True
    if changed:
        _save(sid, cid, st)
    return st


# ── 事件入口（由 db.add_message 统一挂钩，见文件尾 on_message）──────
def on_user_message(sid: str, cid: str) -> None:
    """用户说话 = 打断 + 整个重置（用户方案的核心）。"""
    now = time.time()
    st = _load(sid, cid) or {}
    st.update({"phase": "conversing", "last_user_at": now,
               "settle_until": 0.0, "next_allowed_at": 0.0})
    _save(sid, cid, st)


def on_ai_message(sid: str, cid: str, kind: str = "reply") -> None:
    """AI 说话：
      · proactive（主动消息）→ 重新开始间隔计时
      · 其它（回复 / 话题延续）→ 进入静默窗口（2 分钟的补话也算对话延续）
    """
    now = time.time()
    st = _load(sid, cid) or {}
    st["last_ai_at"] = now
    if kind == "proactive":
        st["last_proactive_at"] = now
        st["phase"] = "waiting"
        st["next_allowed_at"] = now + _roll_interval(cid)
    else:
        st["phase"] = "settling"
        st["settle_until"] = now + quiet_window_sec()
    _save(sid, cid, st)


def on_message(sid: str, cid: str, role: str, extra: dict = None) -> None:
    """db.add_message 的统一挂钩：所有链路的用户/AI 消息都会驱动 FSM。"""
    try:
        extra = extra or {}
        if str(role) == "user":
            on_user_message(sid, cid)
        elif str(role) == "assistant":
            _src = str(extra.get("source") or "")
            _pt = str(extra.get("proactive_type") or "")
            if _src == "proactive":
                on_ai_message(sid, cid, "topic_continue" if _pt == "topic_continue" else "proactive")
            else:
                on_ai_message(sid, cid, "reply")
    except Exception:
        pass


# ── 每日上限 ─────────────────────────────────────────────────
def daily_count(sid: str, cid: str) -> int:
    """今天已发出的"找话说"条数（提醒/承诺/危机按配置决定是否计入）。"""
    try:
        from . import db
        day = datetime.now().strftime("%Y-%m-%d")
        rows = db.q("SELECT extra FROM chat_history WHERE session_id=? AND character_id=? "
                    "AND timestamp LIKE ? AND extra LIKE '%proactive%'",
                    (sid, cid, day + "%"), fetch=True) or []
        n = 0
        for r in rows:
            try:
                _e = r["extra"]
            except Exception:
                _e = (r.get("extra") if hasattr(r, "get") else "") or ""
            try:
                ex = json.loads(_e or "{}")
            except Exception:
                ex = {}
            _pt = str(ex.get("proactive_type") or "")
            if cap_excludes_exempt() and _pt in EXEMPT_TYPES:
                continue
            n += 1
        return n
    except Exception:
        return 0


# ── 唯一闸门 ─────────────────────────────────────────────────
def decide(sid: str, cid: str, *, ptype: str = "general", force: bool = False,
           dnd_exempt: bool = False, startup: bool = False,
           user_requested_audio: bool = False, scheduled: bool = False,
           caller: str = "_engine") -> dict:
    """**唯一裁决**：能不能现在开口。所有链路都必须问它。

    顺序：全豁免 → 免打扰 → 睡眠 → 离线 → 可用时段 → 每日上限 → 节奏状态机。
    返回：{allowed, reason, retry_after, exempt, hours, phase, next_allowed_at,
           next_window_start, sent_today, cap, quiet_until, model_decides}

    ★ 2026-09-15 修（定时类被节奏机误拦）：`scheduled=True` 表示这条是**到点就该发的
      定时内容**（早晚安问候、睡前信、纪念日信件…），它的间隔由 `scheduler._deliver`
      里的「定时类最小间隔 max(10, 下限/2) 分钟」单独管 —— 不能再叠加一层
      "距上次主动发言还差 N 分钟"的节奏机门禁，否则第一条定时问候就会被拦掉
      （实测：`tools/_test_scheduled_spacing_tdd.py` 首条期望 True 却得 False）。
      所以 scheduled 只豁免**节奏状态机**（`_interval_exempt`），
      免打扰/睡眠/离线/活跃时段/每日上限**照旧生效** —— 用户的"时段外发出 0"底线不变。
    """
    now = time.time()
    sid = str(sid or "default").strip() or "default"
    cid = effective_character_id(sid, cid)      # ★ 角色名退化时自愈（见函数说明）
    lo_sec, hi_sec = interval_pair_sec(cid)
    st = state(sid, cid)          # 顺带推进 FSM
    out = {
        "allowed": True, "reason": "", "retry_after": 0, "exempt": "", "hours": st.get("hours", ""),
        "phase": st.get("phase"), "next_allowed_at": int(float(st.get("next_allowed_at") or 0)),
        "next_window_start": 0, "sent_today": st.get("sent_today", 0), "cap": st.get("cap", 0),
        "quiet_until": int(float(st.get("settle_until") or 0)),
        "model_decides": False, "interval_sec": [int(lo_sec), int(hi_sec)],
    }
    try:
        from . import config as _cfg
        out["hours"] = str(_cfg.resolve_active_hours(cid) or "")
        out["lo_min"] = float(_cfg.proactive_interval_min(cid) or 0)
    except Exception:
        out["lo_min"] = 0.0
    try:
        out["model_decides"] = model_decides(cid)
    except Exception:
        out["model_decides"] = False

    def _deny(reason, retry_after, hint="", exempt=""):
        d = dict(out)
        d.update({"allowed": False, "reason": reason,
                  "retry_after": max(30, int(retry_after or 60)), "hint": hint, "exempt": exempt})
        try:
            from . import proactive_trace as _pt
            _pt.record_skip(sid, cid, reason, detail="type=%s" % ptype, source=caller)
            if caller != "_precheck":
                _pt.record_gate(sid, cid, who=caller, ptype=ptype, decision="reject",
                                reason=reason, exempt=exempt,
                                detail=("%s（%s）" % (hint or reason, out["hours"]))[:120],
                                numbers={"retry_after": d["retry_after"],
                                         "sent_today": out.get("sent_today"),
                                         "phase": out.get("phase")})
        except Exception:
            pass
        print(f"[ProactiveEngine] 拦截 {reason}（{hint or ''}）type={ptype} "
              f"phase={out.get('phase')} retry_after={d['retry_after']}s", flush=True)
        return d

    _full_exempt = bool(force or dnd_exempt or ptype in EXEMPT_TYPES)
    _interval_exempt = bool(startup or user_requested_audio or scheduled
                            or ptype in INTERVAL_ONLY_TYPES)

    # ① 全豁免：直接放行（提醒/承诺/危机/测试按钮）
    if _full_exempt:
        out["exempt"] = "full"
        return out

    # ② 全局免打扰
    try:
        from . import config as _cfg2
        if _cfg2.is_dnd_now():
            return _deny("dnd", _cfg2.dnd_seconds_remaining(), "全局免打扰时段")
    except Exception as _e:
        print(f"[ProactiveEngine] DND 门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ③ 用户睡眠声明
    try:
        from . import offline as _offline
        _sh = _offline.hours_since_sleep_declared(sid, cid)
        if _sh < _offline.SLEEP_SILENCE_HOURS:
            return _deny("user_sleeping", (_offline.SLEEP_SILENCE_HOURS - _sh) * 3600,
                         "用户声明睡眠后 %.1fh < %.1fh" % (_sh, _offline.SLEEP_SILENCE_HOURS))
    except Exception as _e:
        print(f"[ProactiveEngine] 睡眠门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ④ 角色离线/半醒
    try:
        from . import offline as _offline2
        _off = _offline2.resolve(sid, cid)
        if _off.get("enabled") and _off.get("status") not in {"online", "half_awake"}:
            return _deny("offline", max(300, int(_off.get("delay_seconds") or 0)),
                         _off.get("hint") or "角色离线中")
    except Exception as _e:
        print(f"[ProactiveEngine] 离线门禁异常(继续): {type(_e).__name__}: {_e}", flush=True)

    # ⑤ 角色可用时段（除全豁免外一律要过）
    #    retry_after 给"距窗口开启的秒数" → 前端会一路退避到窗口打开，不再每 10 分钟空转
    try:
        from . import config as _cfg3
        if not _cfg3.character_active_now(cid):
            _wait = seconds_until_window(out["hours"])
            out["next_window_start"] = int(now) + int(_wait)
            return _deny("outside_active_hours", _wait,
                         "当前不在该角色的活跃时段内（%s）" % out["hours"])
    except Exception as _e:
        print(f"[ProactiveEngine] 时段门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ⑥ 每日上限（提醒/承诺/危机不计入）
    _cap = int(out.get("cap") or 0)
    if _cap > 0 and int(out.get("sent_today") or 0) >= _cap:
        return _deny("daily_cap", 3600, "今天已发 %d 条（上限 %d）" % (out["sent_today"], _cap))

    # ⑦ 节奏状态机（用户方案主干）：话题延续/开屏/索要语音只豁免它，不豁免上面的门禁
    #    ★ 2026-09-16：「主动回复由模型自主」也豁免它 —— 那时由模型自己决定何时想说，
    #      免打扰/睡眠/离线/时段/上限（上面 ②~⑥）照旧管着。
    _auto = model_decides(cid)
    if _auto and not _interval_exempt:
        _interval_exempt = True
        out["model_decides"] = True
    if not _interval_exempt:
        _phase = str(out.get("phase") or "")
        if _phase == "conversing":
            _left = quiet_window_sec() - (now - float(st.get("last_user_at") or now))
            return _deny("settling_window", max(30, _left), "用户还在聊（静默窗口未过）")
        if _phase == "settling":
            _left = float(st.get("settle_until") or 0) - now
            return _deny("settling_window", max(30, _left), "刚聊完，静默窗口中")
        if _phase != "ready":
            _left = float(st.get("next_allowed_at") or 0) - now
            return _deny("interval_waiting", max(30, _left),
                         "到点前还需等 %d 分（间隔 %d-%d 分）"
                         % (int(max(0, _left) / 60), int(lo_sec / 60), int(hi_sec / 60)))

    out["exempt"] = "interval" if _interval_exempt else ""
    try:
        from . import proactive_trace as _ptg
        _ptg.record_gate(sid, cid, who=caller, ptype=ptype, decision="allow", reason="passed",
                         exempt=out["exempt"],
                         detail=("模型自主模式（只受时段/免打扰/上限约束）" if _auto
                                 else "仅豁免间隔" if out["exempt"] == "interval"
                                 else "到点 + 时段内"),
                         numbers={"sent_today": out.get("sent_today"), "phase": out.get("phase")})
    except Exception:
        pass
    return out


def seconds_until_window(hours: str) -> int:
    """距该角色可用时段开启还有多少秒（跨天支持）。解析不出来给 600。"""
    try:
        from . import config as _cfg
        parsed = _cfg.parse_time_range(hours)
        if not parsed:
            return 600
        sh, sm, _eh, _em = parsed
        now = datetime.now()
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if start <= now:
            from datetime import timedelta
            start = start + timedelta(days=1)
        return max(60, int((start - now).total_seconds()))
    except Exception:
        return 600


# ── 重新计时 ────────────────────────────────────────────────
def rearm(sid: str, cid: str, *, short: bool = False) -> float:
    """重新开始间隔计时，返回新的 next_allowed_at。

    short=True（模型自主模式下的"这次不说"）：只等一个静默窗口就再问一次；
    short=False（普通模式 / 已发出）：重新掷一次全局间隔（90–120 分）。
    """
    now = time.time()
    st = _load(sid, cid) or {}
    delay = quiet_window_sec() if short else _roll_interval(cid)
    st["phase"] = "waiting"
    st["next_allowed_at"] = now + delay
    st["settle_until"] = 0.0
    _save(sid, cid, st)
    return st["next_allowed_at"]


# ── 唯一发送口 ───────────────────────────────────────────────
def deliver(sid: str, cid: str, content: str, *, ptype: str = "general",
            source: str = "_engine", qq: bool = True, push_frontend: bool = True) -> int:
    """所有主动消息的唯一出口：清理 → 入库 → 记账 → trace → 推 QQ/前端。"""
    text = str(content or "").strip()
    if not text:
        return 0
    try:
        from .proactive_quality import sanitize_proactive_message
        _allow_greet = ptype in {"morning", "night", "good_morning", "good_night", "festival", "milestone"}
        text = sanitize_proactive_message(
            text, require_hook=not _allow_greet, max_chars=180, allow_greetings=_allow_greet,
            reject_banned=False, session_id=sid, character_id=cid)
    except Exception:
        pass
    if not text:
        return 0
    _mid = 0
    try:
        from . import db
        _pushed_at = int(time.time())
        _mid = db.add_message(sid, "assistant", text, cid, extra={
            "source": "proactive", "proactive_type": ptype, "pushed_at": _pushed_at,
            "replied": 0, "via": source})
        try:
            from .proactive_quality import mark_sent
            mark_sent(sid, cid, _pushed_at)
        except Exception:
            pass
        try:
            from . import proactive_trace as _pt
            _pt.record_deliver(sid, cid, text, proactive_type=ptype, source=source)
            _pt.record_gate(sid, cid, who=source, ptype=ptype, decision="allow",
                            reason="delivered", detail="引擎发送")
        except Exception:
            pass
    except Exception as e:
        print(f"[ProactiveEngine] 入库失败: {type(e).__name__}: {e}", flush=True)
        return 0
    if qq:
        try:
            from . import onebot
            import asyncio
            for _ln in [x.strip() for x in text.split("\n") if x.strip()][:3]:
                try:
                    asyncio.get_running_loop().create_task(onebot.send_qq_message(_ln))
                except Exception:
                    break
        except Exception:
            pass
    if push_frontend:
        try:
            from . import onebot as _ob2
            import asyncio
            asyncio.get_running_loop().create_task(_ob2._push_to_app(sid, cid, text, role="assistant"))
        except Exception:
            pass
    return int(_mid or 0)


# ── 小上下文生成（闲聊类专用，替代 42k 全量上下文）────────────────
def _small_prompt(sid: str, cid: str, hint: str = "") -> tuple:
    """返回 (system, user)：人格摘要 + 最近 10 条 + 当前时间 +（可选：调用方给的提示）。

    ★ 2026-09-16：参数由旧的 `held`（被拦下、留着以后说的整句）改成 `hint`（**当次**提示）——
      旧语义会让几小时前的语境串到现在的消息里，已删除；现在的 hint 只来自
      「马上发一条（测试）」这类**当场**动作。
    """
    name, persona, style = cid, "", ""
    try:
        from . import character_manager as _cm
        card = _cm.get_character_any(cid) or {}
        name = str(card.get("self_name") or card.get("character_name") or cid)
        persona = str(card.get("personality") or "")[:120]
        _ds = card.get("dialogue_style") or {}
        if isinstance(_ds, dict):
            style = str(_ds.get("evolved_style") or "")[:120]
    except Exception:
        pass
    lines = []
    try:
        from . import db
        for m in (db.recent_messages(sid, 10, cid) or []):
            _t = str(m.get("content") or "").replace("\n", " ").strip()[:60]
            if not _t or _t.startswith("【"):
                continue
            lines.append(("TA：" if str(m.get("role")) == "user" else "我：") + _t)
    except Exception:
        pass
    now = datetime.now()
    system = (
        f"你是{name}，用户的 AI 伴侣。人设：{persona or '（无）'}。说话风格：{style or '（无）'}\n"
        f"现在是 {now.strftime('%m-%d %H:%M')}。你打算主动跟 TA 说句话。\n"
        "要求：像真人发微信一样自然，1~3 句短句；可以顺着最近聊过的话题，也可以说此刻想到的；"
        "不要提「主动消息」「系统」「记录」这类词；不要固定句式模板；只输出要说的话本身。\n"
        # ★ 2026-09-16：给模型一个"这次不说"的出口（用户方案：允许开口≠必须开口）
        f"如果你此刻**确实没什么想说的**（没什么可分享、也没什么想问），"
        f"就只输出 {SILENCE_MARKERS[0]} 这四个字，不要勉强找话。"
    )
    if hint:
        system += f"\n（这次可以围绕这个说：{str(hint)[:120]}）"
    user = "最近聊过：\n" + ("\n".join(lines[-10:]) if lines else "（还没聊过）") + "\n\n现在说一句。"
    return system, user


async def generate_chatty(sid: str, cid: str, *, hint: str = "") -> str:
    """闲聊类主动消息（小上下文）。失败返回空串 —— 引擎会静默跳过这一轮。"""
    try:
        from . import chat_logic as _cl
        from . import config as _cfg
        from . import llm_guard as _lg
        from .deepseek_api import chat_once
        model = _cl.pick_model(None, True, cid)
        key = _cfg.api_key_for_model(model)
        if not key:
            return ""
        system, user = _small_prompt(sid, cid, hint)
        out = await chat_once(model, [{"role": "system", "content": system},
                                      {"role": "user", "content": user}],
                              key, temperature=0.9, max_tokens=300,
                              hard_timeout=_lg.long_timeout_sec())
        return str(out or "").strip()
    except Exception as e:
        print(f"[ProactiveEngine] 小上下文生成失败: {type(e).__name__}: {e}", flush=True)
        return ""


async def tick_once(sid: str, cid: str) -> dict:
    """引擎自己的一轮：推进 FSM → 问闸门 → **到点才生成** → 发送。

    用户方案主干（2026-09-16 定稿）：
      用户说话 → 打断并整体重置；AI 最后一句 → 静默窗口；过窗口才开始计时；
      到点且过时段/免打扰/每日上限 → 才生成（模型可以决定"这次不说"）；发出后重新计时。
    ★ 被拦时**什么都不做**：不预生成、不攒（旧 held 机制已删除 —— 它会让话的语境过期）。
    """
    sid = str(sid or "default").strip() or "default"
    cid = effective_character_id(sid, cid)      # ★ 退化角色名自愈
    d = decide(sid, cid, ptype="general", caller="_engine.tick")
    if not d.get("allowed"):
        # 不生成、不攒、不留句子；原因已由 decide() 记进 trace
        return {"sent": 0, "reason": d.get("reason"), "retry_after": d.get("retry_after")}
    text = await generate_chatty(sid, cid)
    if is_silence(text):
        # 模型自己决定"这次不说"（或生成失败/空）：不发，并**重新计时**
        _why = "declined" if str(text or "").strip() else "empty_generation"
        if str(text or "").strip():
            print("[ProactiveEngine] 模型选择这次不说（[[不说话]]）→ 本轮不发，重新计时", flush=True)
        _na = rearm(sid, cid, short=model_decides(cid))
        try:
            from . import proactive_trace as _pt
            _pt.record("skip", session=sid, char=cid, reason=_why,
                       detail="模型自主决定不说" if _why == "declined" else "生成空",
                       who="_engine.tick", next_in_min=int((_na - time.time()) / 60))
        except Exception:
            pass
        return {"sent": 0, "reason": _why}
    _mid = deliver(sid, cid, text, ptype="general", source="_engine.tick")
    if not _mid:
        # 兜底：清理后为空/发送失败也要重新计时，否则会卡在 ready 每轮重试
        rearm(sid, cid)
        return {"sent": 0, "reason": "deliver_failed"}
    return {"sent": 1, "message_id": _mid}


def status(sid: str, cid: str, tail: int = 5) -> dict:
    """给设置页/体检脚本：下次可主动时间 + 最近 N 次主动/被拦原因。"""
    st = state(sid, cid)
    now = time.time()
    out = {
        "engine_enabled": engine_enabled(),
        "phase": st.get("phase"),
        "phase_cn": {"conversing": "你在聊（已重置）", "settling": "刚聊完（静默窗口）",
                     "waiting": "等间隔到点", "ready": "可以开口"}.get(str(st.get("phase")), str(st.get("phase"))),
        "next_allowed_at": int(float(st.get("next_allowed_at") or 0)),
        "seconds_to_next": st.get("seconds_to_next", 0),
        "next_allowed_cn": datetime.fromtimestamp(float(st.get("next_allowed_at") or now)).strftime("%m-%d %H:%M")
        if float(st.get("next_allowed_at") or 0) > 0 else "",
        "quiet_window_sec": quiet_window_sec(),
        "settle_remaining": st.get("settle_remaining", 0),
        "interval_sec": st.get("interval_sec"),
        "hours": "",
        "in_window": False,
        "next_window_start": 0,
        "sent_today": st.get("sent_today", 0),
        "cap": st.get("cap", 0),
        "model_decides": False,          # 下面按角色卡填（「主动回复由模型自主」开关）
        "recent": [],
    }
    try:
        from . import config as _cfg
        out["hours"] = str(_cfg.resolve_active_hours(cid) or "")
        out["in_window"] = bool(_cfg.character_active_now(cid))
        if not out["in_window"]:
            out["next_window_start"] = int(now) + seconds_until_window(out["hours"])
    except Exception:
        pass
    try:
        out["model_decides"] = model_decides(cid)
    except Exception:
        out["model_decides"] = False
    try:
        import os
        from . import config as _cfg2
        fp = os.path.join(str(_cfg2.DATA_DIR), "proactive_trace.jsonl")
        rows = []
        if os.path.isfile(fp):
            for ln in open(fp, encoding="utf-8").read().splitlines()[-60:]:
                try:
                    j = json.loads(ln)
                except Exception:
                    continue
                if j.get("char") != cid or j.get("kind") not in ("deliver", "gate", "skip"):
                    continue
                rows.append(j)
        out["recent"] = rows[-int(tail):]
    except Exception:
        pass
    return out
