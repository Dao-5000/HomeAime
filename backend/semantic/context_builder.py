# -*- coding:utf-8 -*-
"""
Semantic Understanding Foundation - Context Builder
将对话历史格式化为 analyzer 可用的上下文字符串
"""
from typing import List, Dict, Optional


class ContextBuilder:
    """
    从对话历史构建语义分析所需的上下文
    只取最近几轮，控制token消耗
    """

    def __init__(self, max_turns: int = 5):
        self.max_turns = max_turns

    def build(
        self,
        history: List[Dict],
        character_name: str = "AI"
    ) -> Optional[str]:
        """
        Args:
            history: [{"role": "user"/"assistant", "content": "..."}, ...]
            character_name: AI角色名称

        Returns:
            格式化的上下文字符串，或 None（历史为空时）
        """
        if not history:
            return None

        recent = history[-self.max_turns * 2:]  # 每轮2条
        lines = []

        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue

            if role == "user":
                lines.append(f"用户：{content}")
            elif role == "assistant":
                lines.append(f"{character_name}：{content}")

        return "\n".join(lines) if lines else None

    def build_from_messages(
        self,
        messages: List[str],
        is_user: List[bool]
    ) -> Optional[str]:
        """兼容简单列表格式"""
        history = [
            {"role": "user" if is_u else "assistant", "content": m}
            for m, is_u in zip(messages, is_user)
        ]
        return self.build(history)

    def build_from_db(
        self,
        session_id: str,
        character_id: str = "default",
        limit: int = 10
    ) -> Optional[str]:
        """
        从数据库读取最近消息构建上下文。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            limit: 读取消息数量

        Returns:
            格式化的上下文字符串
        """
        try:
            from .. import db
            recent = db.recent_messages(session_id, limit, character_id)
            if not recent:
                return None

            lines = []
            for msg in recent:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if not content:
                    continue
                if role == "user":
                    lines.append(f"用户：{content}")
                else:
                    lines.append(f"AI：{content}")

            return "\n".join(lines) if lines else None
        except Exception as e:
            logger = __import__("logging").getLogger(__name__)
            logger.error(f"ContextBuilder build_from_db error: {e}")
            return None
