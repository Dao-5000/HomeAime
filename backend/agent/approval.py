# -*- coding: utf-8 -*-
"""
授权机制 —— Agent 危险操作的"闸门"。

流程：
  loop.py 要执行危险工具（写文件/执行命令）
    → create_approval() 生成改动预览 + 一个 approval_id
    → 通过 SSE 把预览推给前端（嵌在聊天里显示：改了啥 + [同意][取消]）
    → wait_for_approval() 轮询等待用户决定
    → 同意：执行；取消：记录被拒原因（供反馈闭环），终止该步

被拒不是终点：reject 时会把"被拒原因"交给 memory.py，让助手像真人一样
追问"哪里不合适"，并记进改造记忆，下次换个方式。
"""
import asyncio
import json
import time
import uuid
from difflib import unified_diff

# id -> {id, session_id, character_id, tool, summary, detail, status, reason, event}
_pending: dict = {}

# 等待超时（秒）：超过视为取消，避免用户不在时无限挂起
_WAIT_TIMEOUT = 180


def _diff_preview(path: str, old: str, new: str, max_lines: int = 40) -> str:
    """生成 diff 预览（改前/改后），用于展示"要改什么"。"""
    try:
        old_lines = (old or "").splitlines()
        new_lines = (new or "").splitlines()
        d = list(unified_diff(old_lines, new_lines, fromfile=f"{path}（改前）",
                              tofile=f"{path}（改后）", lineterm=""))
        return "\n".join(d[:max_lines])
    except Exception:
        return ""


def create_approval(session_id: str, character_id: str, tool: str,
                    summary: str, detail: dict) -> dict:
    """生成一次授权请求，返回 approval dict（含 id）。"""
    aid = uuid.uuid4().hex[:12]
    # 为 file_write 生成 diff 预览
    if tool == "file_write":
        try:
            from .tools import _safe_path
            p = _safe_path(detail.get("path", ""))
            old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
            detail = dict(detail)
            detail["diff"] = _diff_preview(str(detail.get("path", "")), old, detail.get("content", ""))
        except Exception:
            pass
    approval = {
        "id": aid,
        "session_id": str(session_id or "default"),
        "character_id": str(character_id or "default"),
        "tool": tool,
        "summary": str(summary or ""),
        "detail": detail or {},
        "status": "pending",
        "reason": "",
        "created_at": time.time(),
        "event": asyncio.Event(),
    }
    _pending[aid] = approval
    return approval


def get_approval(aid: str) -> dict:
    """按 id 查询（前端展示用）。"""
    a = _pending.get(aid)
    if not a:
        return {}
    return {k: v for k, v in a.items() if k != "event"}


def list_pending(session_id: str, character_id: str) -> list:
    """列出某会话下所有待确认的授权（前端拉取/恢复用）。"""
    out = []
    for a in _pending.values():
        if a["session_id"] == session_id and a["character_id"] == character_id \
                and a["status"] == "pending":
            out.append(get_approval(a["id"]))
    return out


def _resolve(aid: str, status: str, reason: str = "") -> bool:
    a = _pending.get(aid)
    if not a or a["status"] != "pending":
        return False
    a["status"] = status
    a["reason"] = str(reason or "")
    try:
        a["event"].set()
    except Exception:
        pass
    return True


def approve(aid: str) -> bool:
    return _resolve(aid, "approved")


def reject(aid: str, reason: str = "") -> bool:
    """取消授权，记录原因。返回是否成功。"""
    ok = _resolve(aid, "rejected", reason)
    if ok:
        # 被拒反馈：交给 memory.py 记录，供助手追问/反思
        try:
            from . import memory as _mem
            a = _pending.get(aid)
            _mem.record_rejection(
                a["session_id"], a["character_id"], a["tool"],
                a.get("summary", ""), a.get("reason", ""),
            )
        except Exception:
            pass
    return ok


async def wait_for_approval(aid: str, timeout: float = None) -> str:
    """等待用户决定。返回 'approved' / 'rejected' / 'timeout'。"""
    a = _pending.get(aid)
    if not a:
        return "timeout"
    timeout = timeout if timeout is not None else _WAIT_TIMEOUT
    try:
        await asyncio.wait_for(a["event"].wait(), timeout=timeout)
    except asyncio.TimeoutError:
        _resolve(aid, "rejected", "等待超时自动取消")
        return "timeout"
    return a.get("status", "rejected")
