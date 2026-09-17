# -*- coding:utf-8 -*-
"""
Companion Pipeline v1.0
模块执行管道：

  负责按优先级调用各个模块，收集上下文。

  模块处理器接口：
    handler.load(session_id, character_id, message) -> context
    handler.update(session_id, character_id, context) -> None
"""
from .. import db


# 模块处理器映射
MODULE_HANDLERS = {}


def register_module(name, handler):
    """注册模块处理器"""
    MODULE_HANDLERS[name] = handler


class CompanionPipeline:
    """模块执行管道"""

    def __init__(self, modules=None, weights=None):
        """
        初始化管道。

        Args:
            modules: 模块列表
            weights: 模块权重字典
        """
        self.modules = modules or []
        self.weights = weights or {}
        self.handlers = self._build_handlers()

    def _build_handlers(self):
        """构建模块处理器"""
        handlers = {}

        for module in self.modules:
            if module in MODULE_HANDLERS:
                handlers[module] = MODULE_HANDLERS[module]
            else:
                handlers[module] = self._default_handler(module)

        return handlers

    def _default_handler(self, module):
        """默认模块处理器（直接从数据库读取）"""
        handler_map = {
            "character": self._load_character,
            "memory": self._load_memory,
            "relationship": self._load_relationship,
            "emotion": self._load_emotion,
            "ai_state": self._load_ai_state,
            "timeline": self._load_timeline,
            "personality": self._load_personality,
            "behavior": self._load_behavior,
            "feedback": self._load_feedback,
            "open_loops": self._load_open_loops,
        }
        return handler_map.get(module, lambda *args, **kwargs: {})

    def execute(self, session_id, character_id, message, semantic_state=None):
        """
        执行管道，收集所有模块上下文。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            message: 用户消息
            semantic_state: SemanticState 语义状态对象（可选）

        Returns:
            dict: 合并后的上下文
        """
        context = {}

        for module in self.modules:
            handler = self.handlers.get(module)
            if not handler:
                continue

            try:
                weight = self.weights.get(module, 0.5)
                module_context = handler(session_id, character_id, message, semantic_state)
                context[module] = {
                    "data": module_context,
                    "weight": weight,
                }
            except Exception as e:
                print(f"[CompanionPipeline] 模块 {module} 加载失败: {e}", flush=True)
                context[module] = {
                    "data": {},
                    "weight": 0,
                    "error": str(e),
                }

        return context

    def execute_updates(self, session_id, character_id, context, update_modules=None):
        """
        执行状态更新。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            context: 上下文
            update_modules: 需要更新的模块列表
        """
        if not update_modules:
            return

        for module in update_modules:
            handler = self.handlers.get(module)
            if not handler or not hasattr(handler, "update"):
                continue

            try:
                handler.update(session_id, character_id, context)
            except Exception as e:
                print(f"[CompanionPipeline] 模块 {module} 更新失败: {e}", flush=True)

    # ========== 默认模块加载器 ==========

    def _load_character(self, session_id, character_id, message, semantic_state=None):
        """加载角色人格"""
        try:
            # ★ P0-3：改用 get_character_any —— 原来只调 load_character（新格式），
            #   自建角色（只存在于 legacy 角色配置/）在这里永远拿到 {}，人格配置静默失效。
            from ..character_manager import get_character_any
            return get_character_any(character_id)
        except Exception:
            return {}

    def _load_memory(self, session_id, character_id, message, semantic_state=None):
        """加载长期记忆"""
        try:
            from ..memory_manager import memory_block, build_memory_query
            recent = db.recent_messages(session_id, 6, character_id)
            query = build_memory_query(message, recent)
            return memory_block(query=query, session_id=session_id, character_id=character_id)
        except Exception:
            return ""

    def _load_relationship(self, session_id, character_id, message, semantic_state=None):
        """加载关系状态（接入 SemanticState）"""
        try:
            state = db.get_relationship_state(session_id, character_id) or {}

            # 语义关系信号：让关系模块第一次知道用户消息的语义含义
            if semantic_state:
                state["semantic_relationship"] = {
                    "signal": semantic_state.relationship.signal_type,
                    "intimacy_delta": semantic_state.relationship.intimacy_delta,
                    "milestone": semantic_state.relationship.is_milestone,
                }

            return state
        except Exception:
            return {}

    def _load_emotion(self, session_id, character_id, message, semantic_state=None):
        """加载用户情绪（接入 SemanticState）"""
        try:
            emotion = db.get_emotion_state(session_id, character_id) or {}

            # 语义情绪：实时情绪，比数据库里的更准确
            if semantic_state:
                emotion["semantic"] = {
                    "emotion": semantic_state.emotion.primary_emotion,
                    "valence": semantic_state.emotion.valence.value,
                    "intensity": semantic_state.emotion.intensity.value,
                    "needs_support": semantic_state.needs_emotional_support(),
                }

            return emotion
        except Exception:
            return {}

    def _load_ai_state(self, session_id, character_id, message, semantic_state=None):
        """加载AI自身状态"""
        try:
            from ..companion.ai_state.manager import AIStateManager
            return AIStateManager().get_state(session_id, character_id)
        except Exception:
            return {}

    def _load_timeline(self, session_id, character_id, message, semantic_state=None):
        """加载共同经历"""
        try:
            from ..timeline.manager import TimelineManager
            return TimelineManager().get_recent_events(session_id, character_id, limit=5)
        except Exception:
            return []

    def _load_personality(self, session_id, character_id, message, semantic_state=None):
        """加载人格状态"""
        try:
            from ..personality.manager import PersonalityManager
            return PersonalityManager().get_state(session_id, character_id)
        except Exception:
            return {}

    def _load_behavior(self, session_id, character_id, message, semantic_state=None):
        """加载行为模式"""
        try:
            from ..behavior.strategy import get_behavior_patterns
            return get_behavior_patterns(session_id, character_id, limit=5)
        except Exception:
            return []

    def _load_feedback(self, session_id, character_id, message, semantic_state=None):
        """加载反馈偏好"""
        try:
            from ..feedback.learner import FeedbackLearner
            return FeedbackLearner().get_profile(session_id, character_id)
        except Exception:
            return {}

    def _load_open_loops(self, session_id, character_id, message, semantic_state=None):
        """加载未完成事项"""
        try:
            return db.get_open_loops(session_id, character_id, limit=5)
        except Exception:
            return []
