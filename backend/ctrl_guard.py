# -*- coding: utf-8 -*-
"""电脑操控的动作分级 + 动作日志 + 截图限频（第 3 步，2026-09-15 用户拍板 B 级自主）。

三件事：
  1. **分级**：低风险（开应用/网址/搜索/音量/通知）直通；高风险（跑命令/键鼠/改文件）必须先问。
  2. **动作日志**：每次动作落一条 jsonl（谁发起、什么动作、参数、结果、是否需确认）。
     用途：① 设置页/排障能看"她到底做了什么"；② 采集器的「编造动作」维度拿它当**执行证据**
     —— 她声称做了而日志里没有，就是编造（彻底堵死"搞定啦✓"）。
  3. **截图限频**：截图是最贵的动作（每次都是一次带图调用），默认每分钟最多 1 张。

设计原则（省 token）：**全部本地字符串判断 + 落文件，零模型调用**。
"""
from __future__ import annotations

import json
import os
import time

from . import config

# ── 分级表 ────────────────────────────────────────────────
# ★ 必须用 control.ALLOWED_TYPES 里的**真实动作名**（2026-09-15 实测踩坑）：
#   我第一版凭印象写了 "type"/"mouse"/"click"，而项目里实际叫 type_text / mouse_click /
#   screenshot_look / clipboard_read/write / window / lock / shutdown_timer…
#   → 那些真名会落进 "unknown" 分支被判成"需确认"，**把她的既有能力也拦掉**
#   （"帮我输入 XXX"→type_text、"看看我的屏幕"→screenshot_look 都会被误拦）。
#   现在逐个对齐真名；写动作分级时**先去 control.ALLOWED_TYPES 核一遍名字**。
LOW_RISK = {
    # 直通（用户明确指令 + 系统自带确认）：开/关应用、网址、音量、媒体、通知、
    # 锁屏、看屏幕（截图有独立限频）、窗口操作、读剪贴板、取消关机
    "open_app", "close_app", "url", "open_url", "search",
    "volume", "volume_set", "media", "notify",
    "lock", "screen_info", "screenshot_look", "screenshot", "window",
    "clipboard_read", "shutdown_cancel", "show_desktop",
}
HIGH_RISK = {
    # 必须先问：代打字/点鼠标（会真的改动 TA 的电脑）、写剪贴板、定时关机
    "type_text", "mouse_click", "clipboard_write", "shutdown_timer", "shutdown",
    "run_command", "shell", "exec", "keypress", "hotkey", "drag",
    "file_write", "file_delete", "write_file", "power", "restart",
}

_LOG_NAME = "action_log.jsonl"
_SHOT_KV = "ctrl_screenshot_ts"


def risk_of(action) -> str:
    """动作风险级别：low / high / unknown（未知按 high 处理，保守）。"""
    t = str((action or {}).get("type") or "").strip().lower()
    if not t:
        return "unknown"
    if t in LOW_RISK:
        return "low"
    if t in HIGH_RISK:
        return "high"
    if t.startswith("dev_"):        # 开发类工具（改文件/跑命令）一律高风险
        return "high"
    return "unknown"


def needs_confirm(action) -> bool:
    """是否必须先问用户。低风险不拦；高风险/未知一律拦（除非已标记 confirmed）。"""
    a = action or {}
    if a.get("confirmed") or a.get("_confirmed"):
        return False
    if not gate_enabled():
        return False
    return risk_of(a) != "low"


def gate_enabled() -> bool:
    """分级闸门开关（config CTRL_TIER_GATE，默认 True）。"""
    try:
        v = config.get("CTRL_TIER_GATE", True)
    except Exception:
        return True
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def log_path() -> str:
    return os.path.join(str(config.DATA_DIR), _LOG_NAME)


def log_action(action, result, *, session_id: str = "default", character_id: str = "default",
               initiator: str = "ai", confirmed=None, extra: dict = None) -> dict:
    """落一条动作日志（append jsonl，失败静默——绝不因为日志把动作搞挂）。"""
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "character_id": character_id,
        "initiator": initiator,               # ai / user / system
        "type": str((action or {}).get("type") or ""),
        "risk": risk_of(action),
        "action": {k: v for k, v in (action or {}).items() if not str(k).startswith("_")},
        "confirmed": bool(confirmed) if confirmed is not None else bool((action or {}).get("confirmed")),
        "ok": bool((result or {}).get("ok")),
        "result": str((result or {}).get("error") or (result or {}).get("message")
                      or (result or {}).get("output") or "")[:200],
        "executed": bool((result or {}).get("ok")),
    }
    if extra:
        rec.update(extra)
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print("[CtrlGuard] 动作日志写入失败(静默): %s: %s" % (type(e).__name__, e), flush=True)
    return rec


def recent(limit: int = 20) -> list:
    """最近 N 条动作（设置页/体检用）。"""
    p = log_path()
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max(1, limit):]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        return out
    except Exception:
        return []


def screenshot_max_per_min() -> int:
    try:
        return int(config.get("CTRL_SCREENSHOT_MAX_PER_MIN", 1) or 1)
    except Exception:
        return 1


def screenshot_allowed(now: float = None) -> bool:
    """截图限频：一分钟内最多 N 张（默认 1）。"""
    cap = screenshot_max_per_min()
    if cap <= 0:
        return True
    now = now or time.time()
    try:
        from . import db
        stamps = json.loads(db.kv_get(_SHOT_KV) or "[]")
        if not isinstance(stamps, list):
            stamps = []
    except Exception:
        stamps = []
    fresh = [float(s) for s in stamps if isinstance(s, (int, float, str)) and now - float(s) < 60]
    return len(fresh) < cap


def note_screenshot(now: float = None) -> None:
    """记录一次截图（供限频用）。"""
    now = now or time.time()
    try:
        from . import db
        stamps = json.loads(db.kv_get(_SHOT_KV) or "[]")
        if not isinstance(stamps, list):
            stamps = []
        stamps = [float(s) for s in stamps if now - float(s) < 300]
        stamps.append(now)
        db.kv_set(_SHOT_KV, json.dumps(stamps[-20:]))
    except Exception:
        pass
