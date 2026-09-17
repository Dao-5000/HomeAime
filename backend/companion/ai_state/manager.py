# -*- coding:utf-8 -*-
"""
AI State Manager v1.0
AI角色自身状态管理器：

  统一读取 AI 的三个状态维度：
    - ai_inner_state（AI当前交流状态）
    - personality_state（角色长期人格）
    - conversation_behavior（当前交流行为策略）

  注意：当前 db 函数只支持 session_id，character_id 参数预留，
  后续多角色隔离完善后自动生效。
"""
from ... import db


class AIStateManager:
    """AI角色自身状态管理器"""

    def get_state(self, session_id, character_id="default"):
        """
        获取 AI 的完整状态。

        Args:
            session_id: 会话ID
            character_id: 角色ID（多角色隔离，已透传 db）

        Returns:
            dict: 包含 inner_state、personality、behavior 的完整状态
        """
        return {
            "inner_state": db.get_ai_inner_state(session_id, character_id) or {},
            "personality": db.get_personality_state(session_id, character_id) or {},
            "behavior": db.get_behavior_state(session_id, character_id) or {},
        }

    def get_inner_state(self, session_id, character_id="default"):
        """获取 AI 当前交流状态"""
        return db.get_ai_inner_state(session_id, character_id) or {}

    def get_personality(self, session_id, character_id="default"):
        """获取角色长期人格"""
        return db.get_personality_state(session_id, character_id) or {}

    def get_behavior(self, session_id, character_id="default"):
        """获取当前交流行为策略"""
        return db.get_behavior_state(session_id, character_id) or {}


# 全局单例
_manager_instance = None


def get_manager():
    """获取 AIStateManager 全局单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = AIStateManager()
    return _manager_instance
