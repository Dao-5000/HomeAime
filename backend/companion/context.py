# -*- coding:utf-8 -*-
"""
Companion Context v1.0
统一上下文对象：
  使用简单的字典结构存储所有上下文信息。
  提供 set/get/export 方法。
"""


class CompanionContext:
    """统一的伴侣上下文对象"""

    def __init__(self):
        self.data = {
            "character": {},
            "profile": {},
            "relationship": {},
            "memory": "",
            "emotion": {},
            "open_loops": [],
            "behavior": {},
            "time": "",
            "intimacy": "",
            "ai_state": {},
            "timeline": [],
            "life_profile": [],
            "behavior_pattern": [],
            "personality_state": {},
            "final_personality": {},
            "feedback_profile": {},
            "corrections": "",
            "multimodal": {},
            "knowledge_graph": "",
            "reflection": "",
            "identity": "",
        }

    def set(self, key, value):
        """设置上下文字段"""
        # ★ 可选：character key 只接受 dict，从源头阻断类型污染
        if key == "character" and value is not None and not isinstance(value, dict):
            print(f"[CompanionContext] [WARN] character key 期望 dict，收到 {type(value).__name__}，已忽略", flush=True)
            return
        self.data[key] = value

    def get(self, key, default=None):
        """获取上下文字段"""
        return self.data.get(key, default)

    def export(self):
        """导出完整上下文字典"""
        return self.data
