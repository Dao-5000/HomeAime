# -*- coding:utf-8 -*-
"""
YunLink Companion Brain Controller v1.0
AI伴侣统一大脑控制器：

  统一管理：
  - 角色人格
  - 用户记忆
  - 关系状态
  - 情绪状态
  - 未完成事项
  - 行为策略

  不负责生成回复，只负责提供上下文。

  架构：
                   User
                    ↓
                main.py
                    ↓
          Companion Brain
       ┌───────────────┐
       │ Character     │  人设
       └───────────────┘
       ┌───────────────┐
       │ Relationship  │  好感/信任
       └───────────────┘
       ┌───────────────┐
       │ Memory        │  经历
       └───────────────┘
       ┌───────────────┐
       │ Emotion       │  情绪
       └───────────────┘
       ┌───────────────┐
       │ Open Loop     │  未完成事件
       └───────────────┘
                    ↓
            Prompt Builder
                    ↓
                   LLM
"""
from . import db
from . import memory_manager
from . import character_manager


class CompanionBrain:
    """
    AI伴侣统一大脑控制器：
    负责收集和整理所有上下文信息，提供给 Prompt Builder。
    不负责生成回复，只负责提供上下文。
    """

    def __init__(self):
        pass

    def build_context(
        self,
        session_id="default",
        character_id="default",
        user_message=""
    ):
        """
        构建完整的伴侣大脑上下文。

        Args:
            session_id: 会话ID
            character_id: 角色ID（预留，当前函数内部适配现有签名）
            user_message: 用户当前消息（用于记忆检索）

        Returns:
            dict: 包含 character, relationship, emotion, memory, open_loops, behavior 的上下文
        """
        context = {}

        # =====================
        # 1. 角色人格
        # =====================
        try:
            # ★ P0-3：改用 get_character_any（新格式优先、legacy 兜底）。
            #   原来只调 load_character，自建角色在这里永远拿到 {}。
            character = character_manager.get_character_any(character_id)
            context["character"] = character or {}
        except Exception as e:
            print(f"[CompanionBrain] 加载角色人格失败: {e}", flush=True)
            context["character"] = {}

        # =====================
        # 2. 关系状态
        # =====================
        try:
            relationship = db.get_relationship_state(session_id, character_id)
            context["relationship"] = relationship or {}
        except Exception as e:
            print(f"[CompanionBrain] 获取关系状态失败: {e}", flush=True)
            context["relationship"] = {}

        # =====================
        # 3. 当前用户情绪
        # =====================
        try:
            emotion = db.get_emotion_state(session_id, character_id)
            context["emotion"] = emotion or {}
        except Exception as e:
            print(f"[CompanionBrain] 获取情绪状态失败: {e}", flush=True)
            context["emotion"] = {}

        # =====================
        # 4. 长期记忆
        # =====================
        try:
            # 使用用户消息作为查询关键词检索相关记忆（★ 按角色隔离）
            if user_message and str(user_message).strip():
                memories = memory_manager.search_memories(
                    query=user_message,
                    top_k=8,
                    min_score=0.15,
                    session_id=session_id,
                    character_id=character_id
                )
                # search_memories 返回 [(mem, score), ...]，提取记忆内容
                context["memory"] = [
                    {
                        "content": m.get("memory_content", ""),
                        "type": m.get("memory_type", "fact"),
                        "importance": m.get("importance", 5),
                        "score": score
                    }
                    for m, score in memories
                ]
            else:
                context["memory"] = []
        except Exception as e:
            print(f"[CompanionBrain] 检索长期记忆失败: {e}", flush=True)
            context["memory"] = []

        # =====================
        # 5. 未完成事项
        # =====================
        try:
            loops = db.get_open_loops(session_id, character_id, limit=5)
            context["open_loops"] = loops or []
        except Exception as e:
            print(f"[CompanionBrain] 获取未完成事项失败: {e}", flush=True)
            context["open_loops"] = []

        # =====================
        # 6. 行为状态
        # =====================
        try:
            behavior = db.get_behavior_state(session_id, character_id)
            context["behavior"] = behavior or {}
        except Exception as e:
            print(f"[CompanionBrain] 获取行为状态失败: {e}", flush=True)
            context["behavior"] = {}

        return context

    def get_relationship_for_idle(
        self,
        session_id="default",
        character_id="default"
    ):
        """
        为主动消息 Agent 提供关系状态。
        idle_agent.py 以后可以调用这个方法获取统一的关系状态。
        """
        try:
            return db.get_relationship_state(session_id, character_id) or {}
        except Exception:
            return {}

    def get_emotion_for_idle(
        self,
        session_id="default",
        character_id="default"
    ):
        """
        为主动消息 Agent 提供情绪状态。
        """
        try:
            return db.get_emotion_state(session_id, character_id) or {}
        except Exception:
            return {}

    def get_open_loops_for_idle(
        self,
        session_id="default",
        character_id="default",
        limit=5
    ):
        """
        为主动消息 Agent 提供未完成事项。
        """
        try:
            return db.get_open_loops(session_id, character_id, limit=limit) or []
        except Exception:
            return []


# 全局单例
_brain_instance = None


def get_brain():
    """获取 CompanionBrain 全局单例"""
    global _brain_instance
    if _brain_instance is None:
        _brain_instance = CompanionBrain()
    return _brain_instance
