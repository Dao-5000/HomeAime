# -*- coding: utf-8 -*-
"""ACP 帧编解码 —— 换行分帧的 JSON-RPC 2.0（纯函数，无 IO）。

ACP v1 传输约定：一行一帧，`\\n` 结尾；同时带 id 与 method = 请求，只带 id = 响应，
只带 method = 通知；格式错误的行忽略。
"""
import json


def encode(obj: dict) -> bytes:
    """对象 → 一行 utf-8 字节（中文不转义）。"""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode(raw) -> dict | None:
    """一行 → 对象；坏行/空行 → None（与协议"忽略格式错误的行"一致）。"""
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8", "replace")
        except Exception:
            return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def classify(frame: dict) -> str:
    """response / request / notification / invalid"""
    if not isinstance(frame, dict):
        return "invalid"
    has_id = "id" in frame
    has_method = "method" in frame
    if has_id and ("result" in frame or "error" in frame):
        return "response"
    if has_id and has_method:
        return "request"
    if has_method:
        return "notification"
    return "invalid"
