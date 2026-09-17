# -*- coding:utf-8 -*-
"""
Proactive Context v1.0
主动决策上下文对象：
  封装主动消息决策所需的所有上下文信息。
"""


class ProactiveContext:
    """主动决策上下文"""

    def __init__(
        self,
        relationship=None,
        memories=None,
        emotion=None,
        open_loops=None,
        last_chat=None
    ):
        self.relationship = relationship or {}
        self.memories = memories or []
        self.emotion = emotion or {}
        self.open_loops = open_loops or []
        self.last_chat = last_chat or {}

        # 屏幕感知结果（由 vision_analyzer 填充）
        self.screen_state = {}

    def update_screen(self, screen_state: dict):
        """更新屏幕感知状态（被动/主动截屏后调用）"""
        if isinstance(screen_state, dict):
            self.screen_state = screen_state

    def to_dict(self):
        """转换为字典"""
        return {
            "relationship": self.relationship,
            "memories": self.memories,
            "emotion": self.emotion,
            "open_loops": self.open_loops,
            "last_chat": self.last_chat,
        }
