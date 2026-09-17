# -*- coding:utf-8 -*-
"""
Timeline Manager v1.0
长期关系时间线管理器：

  负责管理 relationship_timeline 表，提供事件的增删查改。
  记录 AI 与用户共同经历的发展轨迹。
"""
from .. import db


class TimelineManager:
    """长期关系时间线管理器"""

    def add_event(
        self,
        session_id,
        character_id,
        event_type,
        title,
        description,
        importance=5
    ):
        """
        添加时间线事件。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            event_type: 事件类型（first_share、emotional_support、milestone、conflict、promise等）
            title: 事件标题
            description: 事件描述
            importance: 重要性 1-10
        """
        db.add_timeline_event(
            session_id=session_id,
            character_id=character_id,
            event_type=event_type,
            title=title,
            description=description,
            importance=importance
        )

    def get_recent_events(
        self,
        session_id,
        character_id,
        limit=5
    ):
        """
        获取最近的时间线事件。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            limit: 返回数量

        Returns:
            list: 事件列表
        """
        return db.get_recent_timeline_events(
            session_id=session_id,
            character_id=character_id,
            limit=limit
        )

    def get_events_by_type(
        self,
        session_id,
        character_id,
        event_type,
        limit=10
    ):
        """
        按类型获取时间线事件。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            event_type: 事件类型
            limit: 返回数量

        Returns:
            list: 事件列表
        """
        rows = db.q(
            """
            SELECT *
            FROM relationship_timeline
            WHERE session_id=?
              AND character_id=?
              AND event_type=?
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
            """,
            (session_id, character_id, event_type, int(limit)),
            fetch=True
        )
        return [dict(r) for r in rows]

    def mark_event_used(self, event_id):
        """标记事件已被使用（增加使用次数）"""
        db.increase_timeline_used_count(event_id)

    def build_timeline_prompt(self, events):
        """
        将时间线事件转换为 Prompt 文本。

        Args:
            events: 事件列表

        Returns:
            str: Prompt 文本
        """
        if not events:
            return ""

        lines = []
        for event in events:
            title = event.get("title", "")
            desc = event.get("description", "")
            event_type = event.get("event_type", "")
            created_at = event.get("created_at", "")

            if title:
                line = f"- {title}"
                if desc:
                    line += f"：{desc}"
                if created_at:
                    line += f"（{created_at[:10]}）"
                lines.append(line)

        if not lines:
            return ""

        return (
            "【共同经历时间线】\n"
            "以下是你和用户之间发生过的重要事情，"
            "自然理解和运用，不要逐条复述，不要向用户提到时间线系统。\n"
            + "\n".join(lines)
        )


# 全局单例
_manager_instance = None


def get_manager():
    """获取 TimelineManager 全局单例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = TimelineManager()
    return _manager_instance
