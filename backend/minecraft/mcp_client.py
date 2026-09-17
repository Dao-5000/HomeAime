# -*- coding: utf-8 -*-
"""
Numen MCP 客户端（HTTP/SSE streamable transport）

Numen（minecraft-numen）在游戏客户端跑一个 MCP server，默认监听
http://127.0.0.1:8765/mcp（HTTP streamable，非 stdio）。

本模块实现最小 MCP 客户端：initialize → tools/list → tools/call，
用于后端作为「外接大脑」驱动游戏内的 AI 同伴（助手）。

协议要点（MCP streamable HTTP）：
  - 单一 POST 端点，JSON-RPC 2.0
  - initialize 握手后必须发 notifications/initialized
  - tools/call 返回 {content:[{type,text}], isError}
"""
import json
import logging

from ..http_client import get_http_client

logger = logging.getLogger(__name__)

# MCP 协议版本：numen 用的是较新版本；不匹配时 initialize 会报协议错误，可回退
MCP_PROTOCOL_VERSION = "2024-11-05"


class NumenMCPClient:
    def __init__(self, url="http://127.0.0.1:8765/mcp", token=""):
        self.url = (url or "http://127.0.0.1:8765/mcp").rstrip("/")
        self.token = token
        self._req_id = 0
        self._initialized = False

    async def ensure_session(self):
        """确保 MCP 会话已握手（按需调用：非侵入模式下不保持常驻连接）。"""
        if not self._initialized:
            await self.initialize()
        return True

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    async def _rpc(self, method, params=None, is_notification=False):
        client = get_http_client()
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not is_notification:
            payload["id"] = self._next_id()
        try:
            resp = await client.post(self.url, json=payload, headers=self._headers())
        except Exception as e:
            raise RuntimeError(f"MCP 连接失败（{self.url}）: {type(e).__name__}: {e}")
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"MCP {method} HTTP {resp.status_code}: {resp.text[:300]}")
        if is_notification:
            return None
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"MCP {method} 返回非 JSON: {resp.text[:300]}")
        if data.get("error"):
            raise RuntimeError(f"MCP {method} error: {data['error']}")
        return data.get("result")

    async def initialize(self):
        result = await self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ai-companion-backend", "version": "1.0.0"},
        })
        # 握手后必须发 initialized 通知（MCP 规范）
        try:
            await self._rpc("notifications/initialized", {}, is_notification=True)
        except Exception:
            pass
        self._initialized = True
        return result

    async def list_tools(self):
        result = await self._rpc("tools/list", {}) or {}
        return result.get("tools", [])

    async def call_tool(self, name, arguments=None):
        return await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def extract_text(self, tool_result):
        """从 MCP tools/call 结果里提取纯文本（content[].text 拼接）。"""
        try:
            parts = []
            for item in (tool_result or {}).get("content", []) or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
            return "\n".join(parts)
        except Exception:
            return ""
