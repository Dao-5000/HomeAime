# -*- coding: utf-8 -*-
"""
stdio 版 MCP 客户端（StardewValley-MCP 对接）。

StardewValley-MCP 的 MCP Server 是 Node 进程，走 stdio（stdin/stdout 按行分隔的
JSON-RPC 2.0），通过本地 JSON 文件（bridge_data.json / actions.json）与 C# SMAPI
Mod 桥接。与 backend/minecraft/mcp_client.py（HTTP streamable）协议层相同、
传输层不同——这里实现最小 stdio 传输：initialize → tools/list → tools/call。

设计要点：
  - 进程常驻：游戏开关不影响 MCP Server（它只读写 JSON 文件），只连接一次；
  - stderr 必须持续抽干：Node 进程往 stderr 打日志，PIPE 不读会缓冲区满而卡死；
  - 服务端可能主动发 request/notification（sampling/roots/log 等）：
    request 回 MethodNotFound，notification 忽略，只等自己 id 的响应。
"""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"


class StdioMCPClient:
    """一个 stdio MCP Server 对应一个客户端实例（进程级单例）。"""

    def __init__(self, command: str, args: list = None, env: dict = None, cwd: str = ""):
        self.command = str(command or "node").strip()
        self.args = list(args or [])
        self.env = dict(env or {})
        self.cwd = str(cwd or "").strip()
        self.proc: asyncio.subprocess.Process | None = None
        self._req_id = 0
        self._initialized = False
        self._lock = asyncio.Lock()   # 串行化请求（简单 client 不做并发复用）
        self._stderr_task = None

    # ── 生命周期 ────────────────────────────────────────────
    async def start(self):
        """启动 MCP Server 子进程并完成 MCP 握手。失败抛异常（调用方降级）。"""
        if self.proc and self.proc.returncode is None:
            return True
        env_base = {k: v for k, v in __import__("os").environ.items()}
        env_base.update(self.env)
        kwargs = {}
        if self.cwd:
            kwargs["cwd"] = self.cwd
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env_base,
                **kwargs,
            )
        except Exception as e:
            raise RuntimeError(f"MCP Server 启动失败（{self.command} {self.args}）: {e}")
        # 持续抽干 stderr（防缓冲区满卡死），顺手记日志
        async def _drain_err():
            try:
                while True:
                    line = await self.proc.stderr.readline()
                    if not line:
                        break
                    t = line.decode("utf-8", "replace").strip()
                    if t:
                        logger.debug(f"[StardewMCP] server stderr: {t[:200]}")
            except Exception:
                pass
        self._stderr_task = asyncio.create_task(_drain_err())
        await self.initialize()
        return True

    async def stop(self):
        """结束子进程。"""
        try:
            if self.proc and self.proc.returncode is None:
                self.proc.kill()
        except Exception:
            pass
        self.proc = None
        self._initialized = False

    @property
    def alive(self) -> bool:
        return bool(self.proc and self.proc.returncode is None)

    # ── 协议层 ──────────────────────────────────────────────
    def _next_id(self):
        self._req_id += 1
        return self._req_id

    async def _send(self, payload: dict):
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line.encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_until_response(self, want_id: int):
        """读到 want_id 的响应为止；服务端主动 request 回 MethodNotFound，notification 忽略。"""
        while True:
            raw = await self.proc.stdout.readline()
            if not raw:
                raise RuntimeError("MCP Server stdout 已关闭（进程退出）")
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                logger.warning(f"[StardewMCP] 非 JSON 行忽略: {raw[:120]!r}")
                continue
            mid = msg.get("id")
            if mid == want_id:
                if msg.get("error"):
                    raise RuntimeError(f"MCP error: {msg['error']}")
                return msg.get("result")
            # 不是我们要的响应：区分服务端主动 request（有 method）与 notification
            if msg.get("method"):
                if mid is not None:
                    # 服务端在等我们回 → 必须应答，否则它可能挂起
                    try:
                        await self._send({"jsonrpc": "2.0", "id": mid, "error":
                                          {"code": -32601, "message": "Method not found"}})
                    except Exception:
                        pass
                # notification 直接忽略（如 logging/message）
            # 否则：旧响应/未知消息 → 忽略

    async def _rpc(self, method, params=None, is_notification=False):
        async with self._lock:
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            if is_notification:
                await self._send(payload)
                return None
            rid = self._next_id()
            payload["id"] = rid
            await self._send(payload)
            return await self._read_until_response(rid)

    async def initialize(self):
        result = await self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "ai-companion-backend", "version": "1.0.0"},
        })
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

    @staticmethod
    def extract_text(tool_result):
        """从 tools/call 结果里提取纯文本。"""
        try:
            parts = []
            for item in (tool_result or {}).get("content", []) or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
            return "\n".join(parts)
        except Exception:
            return ""
