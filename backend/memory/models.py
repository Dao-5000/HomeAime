# -*- coding:utf-8 -*-
"""
记忆数据模型：
  使用 dataclass 定义 Memory 数据结构，便于类型提示和代码可读性。
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Memory:
    """记忆数据模型"""
    user_id: str
    memory_type: str
    content: str
    importance: int = 5
    character_id: str = "default"
    id: Optional[int] = None
    embedding: Optional[str] = None
    created_at: Optional[str] = None
    last_used: Optional[str] = None

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "character_id": self.character_id,
            "memory_type": self.memory_type,
            "content": self.content,
            "importance": self.importance,
            "embedding": self.embedding,
            "created_at": self.created_at,
            "last_used": self.last_used,
        }
