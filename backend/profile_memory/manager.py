# -*- coding:utf-8 -*-
"""
Life Profile Manager v1.0
用户人生档案管理器：

  负责管理 life_profile 表，提供档案的增删查改。
  存储整理后的用户长期画像、人生阶段、关系总结、行为模式等。
"""
from .. import db


class LifeProfileManager:
    """用户人生档案管理器"""

    def save(
        self,
        session_id,
        character_id,
        profile_type,
        content,
        importance=5
    ):
        """
        保存人生档案。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            profile_type: 档案类型（life_phase、personality_pattern、behavior_pattern、important_values、relationship_story）
            content: 档案内容
            importance: 重要性 1-10
        """
        db.save_life_profile(
            session_id=session_id,
            character_id=character_id,
            profile_type=profile_type,
            content=content,
            importance=importance
        )

    def get(
        self,
        session_id,
        character_id,
        limit=10
    ):
        """
        获取人生档案。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            limit: 返回数量

        Returns:
            list: 档案列表
        """
        return db.get_life_profiles(
            session_id=session_id,
            character_id=character_id,
            limit=limit
        )

    def get_by_type(
        self,
        session_id,
        character_id,
        profile_type,
        limit=5
    ):
        """
        按类型获取人生档案。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            profile_type: 档案类型
            limit: 返回数量

        Returns:
            list: 档案列表
        """
        return db.get_life_profile_by_type(
            session_id=session_id,
            character_id=character_id,
            profile_type=profile_type,
            limit=limit
        )

    def count(
        self,
        session_id,
        character_id
    ):
        """统计人生档案数量"""
        return db.count_life_profiles(
            session_id=session_id,
            character_id=character_id
        )


# 档案类型定义
PROFILE_TYPES = {
    "life_phase": "人生阶段",
    "personality_pattern": "性格特点",
    "behavior_pattern": "行为习惯",
    "important_values": "重要价值观",
    "relationship_story": "与AI的重要经历",
    "growth_trajectory": "成长轨迹",
    "emotional_pattern": "情绪模式",
}


# 全局单例
_manager_instance = None


def get_manager():
    """获取 LifeProfileManager 全局单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = LifeProfileManager()
    return _manager_instance
