# -*- coding: utf-8 -*-
"""session/update → agent_* 事件映射。

为什么要单独一层：
  · ACP 只给"通用工具生命周期"，`tool_call` 带 rawInput，`tool_call_update` 只带结果；
  · 审批请求（session/request_permission）**不带** title/rawInput
    （探针真实帧实测：params.toolCall 里只有 toolCallId 一个键），
    所以必须按 toolCallId 回查 `tool_call` 里记下的参数 —— 这就是 ToolCallRegistry 的用途。
"""
from collections import OrderedDict

_THOUGHT = "agent_thought_chunk"
_MESSAGE = "agent_message_chunk"
_TOOL_CALL = "tool_call"
_TOOL_UPDATE = "tool_call_update"
_USAGE = "usage_update"
#: 只有这两个状态才算"工具跑完"；ACP 允许 pending / in_progress 的中间更新，那不是结果。
_TERMINAL = ("completed", "failed")


def _copy_value(v):
    """纯 JSON 值的结构化拷贝：绝不把内部容器（含 rawInput 里的嵌套 dict）泄漏给调用方。

    桥接层会拿 get() 回来的 rawInput 去建审批卡，浅拷贝会让"读到的参数"和
    "注册表里的参数"是同一个对象，任一方改动都会污染另一方。
    """
    if isinstance(v, dict):
        return {k: _copy_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_copy_value(x) for x in v]
    return v


class ToolCallRegistry:
    """记住 toolCallId → {"tool": 标题, "kind": 工具类别, "rawInput": 参数}，有界（默认 200 条）。"""

    def __init__(self, maxsize: int = 200):
        self._d = OrderedDict()
        self._max = max(1, int(maxsize))

    def remember(self, update: dict) -> None:
        try:
            tid = str(update.get("toolCallId") or "").strip()
            if not tid:
                return
            raw = update.get("rawInput")
            self._d[tid] = {
                "tool": str(update.get("title") or "").strip(),
                "kind": str(update.get("kind") or "").strip(),
                "rawInput": _copy_value(raw) if isinstance(raw, dict) else {},
            }
            self._d.move_to_end(tid)
            while len(self._d) > self._max:
                self._d.popitem(last=False)
        except Exception:
            pass

    def get(self, tool_call_id: str) -> dict:
        """返回**副本**（不是内部记录本身）；查不到或坏入参一律返回 {}。"""
        try:
            return _copy_value(self._d.get(str(tool_call_id or "").strip()) or {})
        except Exception:
            return {}


def _text_of(content) -> str:
    """ACP content 可能是 str / list[{"type":"content","content":{"type":"text","text":...}}]。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return _text_of(content.get("content"))
    if isinstance(content, (list, tuple)):
        return "\n".join(filter(None, (_text_of(x) for x in content)))
    return str(content)


def map_update(params: dict, reg: ToolCallRegistry):
    """一条 session/update → 一个 agent_* 事件（无需上报的返回 None）。"""
    try:
        u = (params or {}).get("update") or {}
        kind = str(u.get("sessionUpdate") or "").strip()
        if kind == _THOUGHT:
            return {"type": "agent_thinking", "text": _text_of(u.get("content"))}
        if kind == _MESSAGE:
            return {"type": "agent_message", "text": _text_of(u.get("content"))}
        if kind == _TOOL_CALL:
            reg.remember(u)
            return {
                "type": "agent_tool",
                "tool": str(u.get("title") or "").strip(),
                "args": u.get("rawInput") if isinstance(u.get("rawInput"), dict) else {},
                "tool_call_id": str(u.get("toolCallId") or ""),
                "status": str(u.get("status") or ""),
            }
        if kind == _TOOL_UPDATE:
            tid = str(u.get("toolCallId") or "")
            status = str(u.get("status") or "")
            # ★ R21：中间态不是结果。ACP 允许 pending / in_progress 的 tool_call_update，
            #   放行会为同一个 toolCallId 长出"假的成功结果 + 真结果"两条同名轨迹，
            #   破坏"一个 toolCallId 一条 agent_result"的契约（长命令必然触发）。
            #   先判终态再回查注册表：回查要拷一份 rawInput，中间态白拷是浪费。
            if status not in _TERMINAL:
                return None
            info = reg.get(tid)
            return {
                "type": "agent_result",
                "tool": info.get("tool", ""),
                "tool_call_id": tid,
                "ok": status != "failed",
                "status": status,
                "result": _text_of(u.get("content")),
            }
        if kind == _USAGE:
            return {
                "type": "agent_usage",
                "used": u.get("used"),
                "size": u.get("size"),
                "cost": u.get("cost"),
            }
        return None
    except Exception:
        return None
