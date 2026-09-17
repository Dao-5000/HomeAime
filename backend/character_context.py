# -*- coding:utf-8 -*-
"""
角色上下文对象：
  封装 session_id 和 character_id 双维度，用于数据库查询和数据隔离。
  每个角色有独立的记忆、性格、关系、成长轨迹。
"""


class CharacterContext:
    """角色上下文：session_id + character_id 双维度隔离"""

    def __init__(
        self,
        session_id="default",
        character_id="default"
    ):
        self.session_id = session_id
        self.character_id = character_id

    def db_filter(self):
        """返回数据库查询的过滤条件字典"""
        return {
            "session_id": self.session_id,
            "character_id": self.character_id
        }

    def __repr__(self):
        return f"<CharacterContext session={self.session_id} character={self.character_id}>"
