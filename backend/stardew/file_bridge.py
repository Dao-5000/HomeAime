# -*- coding: utf-8 -*-
"""
文件桥（StardewClient 真联机模式的通信层）。

StardewClient Mod 与后端通过同一组本地文件通信（与 StardewMCPBridge 的文件协议一致）：
  - bridge_data.json：Mod 每秒写游戏状态（含 companions[player 状态包装]）
  - actions/<ts>-<seq>.json：后端写指令，Mod 逐条消费（读后即删）

与 StdioMCPClient 的接口对齐（call_tool/extract_text），StardewBrain 按配置选择桥。
真联机模式下不需要 Node MCP Server 进程 —— 后端直接读写文件即可。
"""
import json
import os
import time


class FileBridge:
    def __init__(self, bridge_path: str, action_dir: str):
        self.bridge_path = str(bridge_path or "").strip()
        self.action_dir = str(action_dir or "").strip()
        self._seq = 0

    @property
    def alive(self) -> bool:
        return bool(self.bridge_path and self.action_dir)

    # 接口兼容（StardewBrain 不感知差异）
    async def initialize(self):
        return True

    async def list_tools(self):
        return []

    # ── 读游戏状态 ──────────────────────────────────────────
    def read_state(self, max_age_sec: float = 0) -> dict | None:
        """读 bridge_data.json；max_age_sec>0 时要求数据新鲜（判断游戏是否在跑）。"""
        try:
            if not self.bridge_path or not os.path.isfile(self.bridge_path):
                return None
            if max_age_sec > 0:
                age = time.time() - os.path.getmtime(self.bridge_path)
                if age > max_age_sec:
                    return None
            with open(self.bridge_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    # ── 写指令（fire-and-forget，Mod 读后即删）──────────────
    async def call_tool(self, name, arguments=None):
        if not self.alive:
            return {"content": [{"type": "text", "text": "file bridge not configured"}], "isError": True}
        try:
            os.makedirs(self.action_dir, exist_ok=True)
            self._seq += 1
            payload = {"actionType": name}
            payload.update(arguments or {})
            final = os.path.join(self.action_dir, f"{int(time.time() * 1000)}-{self._seq:06d}.json")
            tmp = final + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, final)
            return {"content": [{"type": "text", "text": "sent"}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"write failed: {e}"}], "isError": True}

    @staticmethod
    def extract_text(tool_result):
        try:
            parts = []
            for item in (tool_result or {}).get("content", []) or []:
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
            return "\n".join(parts)
        except Exception:
            return ""
