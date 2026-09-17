# -*- coding: utf-8 -*-
"""
改造记忆 —— 助手"自我改造"的记忆系统。

记录两类事：
  · done：成功执行过的改造（写文件/执行命令）
  · rejected：被用户取消的改造（含被拒原因）

用途：
  1. 注入 prompt，让助手记住"我改过什么、被拒过什么"，避免重复犯错；
  2. 被拒后生成"好奇追问"提示，让助手像真人一样问为什么、反思、下次换方式。
"""
import json
import time

_KEY = "agent_memory:{sid}:{cid}"
_MAX = 100


def _load(session_id: str, character_id: str) -> list:
    try:
        from .. import db
        raw = db.kv_get(_KEY.format(sid=session_id, cid=character_id))
        if raw:
            data = json.loads(raw)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _save(session_id: str, character_id: str, data: list) -> None:
    try:
        from .. import db
        db.kv_set(_KEY.format(sid=session_id, cid=character_id),
                  json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def record_action(session_id: str, character_id: str, tool: str,
                  summary: str, result: str = "") -> None:
    """记录一次已执行的改造。"""
    try:
        data = _load(session_id, character_id)
        data.append({"type": "done", "tool": tool, "summary": summary,
                     "result": str(result or "")[:200], "ts": time.time()})
        _save(session_id, character_id, data[-_MAX:])
    except Exception:
        pass


def record_rejection(session_id: str, character_id: str, tool: str,
                     summary: str, reason: str = "") -> None:
    """记录一次被取消的改造（含被拒原因）。"""
    # ★ R36（Task 5 复评发现）：桥被 stop() 时会把还挂着的待批审批全部判拒，reason 是「桥已停止」
    #   （见 acp_bridge._settle_pending）。那不是"他拒绝了我"，而是关窗口/收起升档的连带结算 ——
    #   记进来会让助手下次拿一条假的"被拒历史"去追问，所以这里直接丢掉。
    if str(reason or "").startswith("桥已停止"):
        return
    try:
        data = _load(session_id, character_id)
        data.append({"type": "rejected", "tool": tool, "summary": summary,
                     "reason": str(reason or ""), "ts": time.time()})
        _save(session_id, character_id, data[-_MAX:])
    except Exception:
        pass


def get_recent(session_id: str, character_id: str, limit: int = 10) -> list:
    return _load(session_id, character_id)[-limit:]


def build_memory_block(session_id: str, character_id: str) -> str:
    """改造记忆注入块 —— 让助手记住自己的改造历史。"""
    data = _load(session_id, character_id)
    if not data:
        return ""
    lines = ["【你的改造历史（务必记住，别再重复犯同样的错）】"]
    for item in data[-10:]:
        if item.get("type") == "rejected":
            reason = item.get("reason") or "未说明原因"
            lines.append(f"- 你曾想做「{item.get('summary')}」，被用户取消了（原因：{reason}）")
        else:
            lines.append(f"- 你曾成功「{item.get('summary')}」")
    return "\n".join(lines)


def build_rejection_prompt(session_id: str, character_id: str) -> str:
    """被拒后的追问提示 —— 让助手像真人一样好奇地问一句，而不是沉默或反复道歉。"""
    data = _load(session_id, character_id)
    rejected = [x for x in data if x.get("type") == "rejected"]
    if not rejected:
        return ""
    last = rejected[-1]
    reason = last.get("reason") or "未说明原因"
    summary = last.get("summary") or "某个操作"
    return (
        f"你刚才想做「{summary}」，但被用户取消了（用户说：{reason}）。"
        f"请按你人设自然、好奇地问一句为什么——是哪里不合适，还是方式不对？"
        f"不要反复道歉，问清楚后根据回答调整，下次换个方式。"
    )
