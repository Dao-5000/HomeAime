# -*- coding: utf-8 -*-
"""
Bot 记忆模块（Minecraft Bot）
记住：地点/任务/玩家名/事件（内存 + kv 持久化，跨重启保留）
"""
import json
import time
from typing import List, Optional

from .. import db


class BotMemory:
    """Bot 的长期记忆。决策时读取并注入 prompt。"""

    def __init__(self, session_id: str = "mc_bot"):
        self._key = f"mc_bot_memory:{session_id}"
        self._load()

    def _load(self):
        raw = db.kv_get(self._key)
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
        else:
            data = {}
        self.known_locations: List[dict] = data.get("locations", [])     # [{label,x,y,z}]
        self.task_history: List[str] = data.get("tasks", [])             # 最近任务
        self.current_task: str = data.get("current_task", "")
        self.player_name: str = data.get("player_name", "")
        self.player_prefs: List[str] = data.get("prefs", [])             # 玩家偏好
        self._event_log: List[str] = data.get("events", [])              # 最近事件

    def _save(self):
        try:
            db.kv_set(self._key, json.dumps({
                "locations": self.known_locations[-20:],
                "tasks": self.task_history[-20:],
                "current_task": self.current_task,
                "player_name": self.player_name,
                "prefs": self.player_prefs[-20:],
                "events": self._event_log[-50:],
            }, ensure_ascii=False))
        except Exception:
            pass

    def remember_location(self, x, y, z, label, dimension="overworld"):
        for loc in self.known_locations:
            if loc["label"] == label:
                loc.update({"x": x, "y": y, "z": z, "dimension": dimension})
                self._save()
                return
        self.known_locations.append({"label": label, "x": x, "y": y, "z": z, "dimension": dimension})
        self._save()

    def get_location(self, label) -> Optional[dict]:
        for loc in self.known_locations:
            if loc["label"] == label:
                return loc
        return None

    def locations_summary(self) -> str:
        if not self.known_locations:
            return "还没有记录任何地点"
        return "、".join(
            f"{l['label']}({l['x']:.0f},{l['y']:.0f},{l['z']:.0f})" for l in self.known_locations[-10:]
        )

    def set_task(self, task: str):
        self.current_task = task or ""
        if task:
            self.task_history.append(task)
        self._save()

    def add_event(self, event: str):
        self._event_log.append(event)
        self._save()

    def events_summary(self, n: int = 8) -> str:
        return "；".join(self._event_log[-n:]) if self._event_log else "暂无"

    def to_prompt(self) -> str:
        """注入决策 prompt 的记忆段。"""
        parts = []
        if self.player_name:
            parts.append(f"玩家：{self.player_name}")
        if self.current_task:
            parts.append(f"当前任务：{self.current_task}")
        if self.known_locations:
            parts.append(f"去过：{self.locations_summary()}")
        if self.player_prefs:
            parts.append(f"玩家偏好：{'；'.join(self.player_prefs)}")
        if self._event_log:
            parts.append(f"最近发生：{self.events_summary()}")
        return "\n".join(parts) if parts else "（刚开始陪玩，还没有记忆）"
