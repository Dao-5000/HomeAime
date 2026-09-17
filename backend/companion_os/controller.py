# -*- coding:utf-8 -*-
"""
CompanionOS Controller v2.0
AI生命系统总调度器

v2.0 更新：
  - 全链路注入 SemanticState
  - process_async 为主调用入口
  - process 同步版本降级兜底
"""
import asyncio
import functools
import logging

from .router import ContextRouter
from .decision import CompanionDecision
from .pipeline import CompanionPipeline
from backend.loop_compat import get_loop

logger = logging.getLogger(__name__)


class CompanionOS:
    """AI生命系统总调度器"""

    def __init__(self):
        self.router = ContextRouter()
        self.decision = CompanionDecision()

        # ⭐ 语义理解基础层
        self._semantic_analyzer = None
        self._context_builder = None
        self._semantic_ready = False
        self._init_semantic()

        # ★ 2026-09-10 快车道（语音通话用）：语义分析是每回合最贵的一步
        #   （实测 5 秒级别的 LLM 调用）。fast 模式下直接用上一轮的结果，
        #   同时把本轮的分析丢到后台跑完——副作用（场景路由 / 模块 pipeline /
        #   情绪更新）一个不少，只是"注入本轮 prompt"延后一回合。
        #   文字聊天不受影响（默认 fast=False，仍是同步拿最新结果）。
        self._fast_cache: dict = {}
        self._fast_task: dict = {}

    def _init_semantic(self):
        """初始化语义理解层（失败不影响主流程）"""
        try:
            from ..semantic import SemanticAnalyzer, ContextBuilder
            self._semantic_analyzer = SemanticAnalyzer()
            self._context_builder = ContextBuilder(max_turns=5)
            self._semantic_ready = True
            print("[CompanionOS] 语义理解基础层初始化完成", flush=True)
        except Exception as e:
            print(f"[CompanionOS] 语义理解基础层初始化失败（降级运行）: {e}", flush=True)
            self._semantic_ready = False

    # ─────────────────────────────────────────
    # 主入口：异步版本（推荐）
    # ─────────────────────────────────────────
    async def process_async(self, session_id, character_id, message, context=None,
                            fast: bool = False):
        """
        异步处理消息，返回完整决策结果。

        fast=True（语音通话快车道）：
          · 有上一轮缓存 → 立刻返回缓存（一回合滞后），同时后台把本轮跑完并更新缓存；
          · 没有缓存（通话第一句）→ 照旧同步跑一次并缓存，行为与原来一致。

        Returns:
            dict: {
                scene, decision, modules, priority,
                context, semantic_state, semantic
            }
        """
        key = (session_id, character_id)
        if fast:
            cached = self._fast_cache.get(key)
            if cached is not None:
                self._schedule_fast_refresh(session_id, character_id, message, context)
                return cached
        result = await self._process_async_full(session_id, character_id, message, context)
        try:
            if len(self._fast_cache) > 64:      # 简单的容量保护
                self._fast_cache.clear()
            self._fast_cache[key] = result
        except Exception:
            pass
        return result

    def _schedule_fast_refresh(self, session_id, character_id, message, context=None):
        """后台把本轮语义分析 + 场景 pipeline 完整跑一遍（副作用照旧发生）。

        同一会话同时只允许一个刷新任务在跑，避免慢调用堆积。
        """
        key = (session_id, character_id)
        task = self._fast_task.get(key)
        if task is not None and not task.done():
            return
        try:
            import asyncio

            async def _run():
                try:
                    res = await self._process_async_full(session_id, character_id, message, context)
                    self._fast_cache[key] = res
                except Exception as _e:
                    print(f"[CompanionOS] 快车道后台刷新失败(静默): {_e}", flush=True)

            self._fast_task[key] = asyncio.create_task(_run())
        except Exception:
            pass

    async def _process_async_full(self, session_id, character_id, message, context=None):
        """完整流程（原 process_async 主体）：语义分析 → 路由 → 决策 → pipeline。"""
        # 1. 生成语义状态
        semantic_state = None
        if self._semantic_ready and self._semantic_analyzer:
            try:
                # 构建对话上下文
                conv_context = None
                if self._context_builder:
                    try:
                        conv_context = self._context_builder.build_from_db(
                            session_id=session_id,
                            character_id=character_id,
                            limit=10
                        )
                    except Exception:
                        pass

                semantic_state = await self._semantic_analyzer.analyze(
                    message=message,
                    conversation_context=conv_context
                )

                intent_val = semantic_state.intent.value \
                    if hasattr(semantic_state.intent, "value") \
                    else str(semantic_state.intent)
                emotion_val = semantic_state.emotion.primary_emotion

                print(
                    f"[CompanionOS] 语义分析完成: "
                    f"intent={intent_val} "
                    f"emotion={emotion_val}",
                    flush=True
                )

            except Exception as e:
                print(f"[CompanionOS] 语义分析失败（降级）: {e}", flush=True)
                semantic_state = None

        # 2. 场景路由
        route = self.router.route(message, context)

        # ★ 新增：重逢信号（用户隔很久才发第一条，AI当次就流露想念）
        try:
            from ..relationship.manager import RelationshipManager
            from datetime import datetime as _dt
            _rel = RelationshipManager().get_state(session_id, character_id)
            _last = _rel.get("last_interaction")
            if _last:
                _off = (_dt.now() - _dt.strptime(_last, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
                if _off >= 12:
                    route = {**route, "scene": "relationship",
                             "reunion": True, "confidence": 0.9}
        except Exception:
            pass

        # ⭐ 语义增强路由：用语义意图修正场景
        if semantic_state:
            route = self._semantic_enhance_route(route, semantic_state)

        # 3. 决策层
        decision = self.decision.decide(route, context)

        # 4. 构建 Pipeline
        pipeline = CompanionPipeline(
            modules=decision.get("modules", []),
            weights=decision.get("weights", {})
        )

        # 5. ⭐ 执行 Pipeline，传入 semantic_state
        # ★ 2026-09-10 关键修复：pipeline.execute 是**同步**的（DB 查询、记忆检索、
        #   embedding 首次加载……），直接在事件循环里跑会把 asyncio 整个卡住。
        #   实测症状：语音通话里"用户说完话"到后端开始处理 ASR 差了 **7 秒**
        #   （期间 WebSocket 收包、TTS 推送、打断全被堵住），通话听感就是"反应慢半拍"。
        #   丢到线程池执行：逻辑与副作用完全不变，只是不再占用事件循环。
        #   （DB 层是共享连接 + check_same_thread=False + 锁，本就允许跨线程调用。）
        import asyncio as _aio
        module_context = await _aio.to_thread(
            functools.partial(
                pipeline.execute,
                session_id=session_id,
                character_id=character_id,
                message=message,
                semantic_state=semantic_state,
            )
        )

        return {
            "scene":          route,
            "decision":       decision,
            "modules":        decision.get("modules", []),
            "priority":       decision.get("priority", []),
            "context":        module_context,
            "semantic_state": semantic_state,   # 对象，供 Prompt 构建使用
            "semantic":       semantic_state.to_dict()
                if semantic_state and hasattr(semantic_state, "to_dict")
                else None,
        }

    # ─────────────────────────────────────────
    # 同步降级版本
    # ─────────────────────────────────────────
    def process(self, session_id, character_id, message, context=None):
        """
        同步版本（降级用）。
        如果当前有事件循环，用 asyncio.run_coroutine_threadsafe，
        否则用 asyncio.run。
        """
        try:
            loop = get_loop()
            if loop.is_running():
                # 在已有事件循环里（比如 FastAPI），用 nest_asyncio 或 run_in_executor
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run,
                        self.process_async(session_id, character_id, message, context)
                    )
                    return future.result(timeout=30)
            else:
                return loop.run_until_complete(
                    self.process_async(session_id, character_id, message, context)
                )
        except Exception as e:
            print(f"[CompanionOS] process 同步调用失败: {e}", flush=True)
            return self._fallback_process(session_id, character_id, message, context)

    def _fallback_process(self, session_id, character_id, message, context=None):
        """完全降级：不用语义，只做路由+决策"""
        route = self.router.route(message, context)
        decision = self.decision.decide(route, context)
        pipeline = CompanionPipeline(
            modules=decision.get("modules", []),
            weights=decision.get("weights", {})
        )
        module_context = pipeline.execute(
            session_id=session_id,
            character_id=character_id,
            message=message,
            semantic_state=None
        )
        return {
            "scene":          route,
            "decision":       decision,
            "modules":        decision.get("modules", []),
            "priority":       decision.get("priority", []),
            "context":        module_context,
            "semantic_state": None,
            "semantic":       None,
        }

    # ─────────────────────────────────────────
    # 主动消息入口
    # ─────────────────────────────────────────
    async def process_proactive_async(
        self, session_id, character_id,
        inactive_hours=0, emotion=None, open_loops=None
    ):
        route = self.router.route_proactive(inactive_hours, emotion, open_loops)
        decision = self.decision.decide(route)
        pipeline = CompanionPipeline(
            modules=decision.get("modules", []),
            weights=decision.get("weights", {})
        )
        module_context = pipeline.execute(
            session_id=session_id,
            character_id=character_id,
            message="",
            semantic_state=None
        )
        return {
            "scene":          route,
            "decision":       decision,
            "modules":        decision.get("modules", []),
            "priority":       decision.get("priority", []),
            "context":        module_context,
            "semantic_state": None,
            "semantic":       None,
        }

    def process_proactive(self, session_id, character_id,
                          inactive_hours=0, emotion=None, open_loops=None):
        """同步版主动消息"""
        try:
            return asyncio.run(
                self.process_proactive_async(
                    session_id, character_id,
                    inactive_hours, emotion, open_loops
                )
            )
        except Exception as e:
            print(f"[CompanionOS] process_proactive 失败: {e}", flush=True)
            return {}

    # ─────────────────────────────────────────
    # 状态更新
    # ─────────────────────────────────────────
    def decide_updates(self, route):
        return self.decision.decide_updates(route)

    def execute_updates(self, pipeline, session_id, character_id,
                        context, update_modules):
        pipeline.execute_updates(session_id, character_id, context, update_modules)

    # ─────────────────────────────────────────
    # 语义增强路由（内部方法）· 升级版 v2.0（老公定制）
    # ─────────────────────────────────────────
    def _semantic_enhance_route(self, route, semantic_state):
        """
        用语义分析结果修正关键词路由的场景判断。
        语义优先级 > 关键词匹配。
        补全：relationship.is_milestone / signal_type / topic.requires_memory
        """
        from ..semantic.schema import Intent, EmotionValence, EmotionIntensity

        intent = semantic_state.intent
        emotion = semantic_state.emotion
        relationship = semantic_state.relationship   # 真嵌套字段
        topic = semantic_state.topic                 # 真字段

        # 1. 意图映射到场景（补全真实枚举：COMPLAINT/PRAISE/QUESTION等）
        intent_scene_map = {
            Intent.SEEK_COMFORT:       "emotion_support",
            Intent.EMOTIONAL_SHARE:    "emotion_support",
            Intent.COMPLAINT:          "emotion_support",   # 抱怨也需安抚
            Intent.SEEK_ADVICE:        "advice_seeking",
            Intent.QUESTION:           "advice_seeking",    # 提问=求助
            Intent.TASK_REQUEST:       "advice_seeking",
            Intent.RELATIONSHIP_SIGNAL:"relationship",
            Intent.TEASE:              "relationship",
            Intent.PRAISE:             "celebration",       # 夸咱=庆祝甜点
            Intent.SELF_DISCLOSURE:    "relationship",      # 自我披露拉亲密
            Intent.GREETING:           "normal_chat",
            Intent.FAREWELL:           "normal_chat",
            Intent.CASUAL_CHAT:        "normal_chat",
        }

        new_scene = intent_scene_map.get(intent, route.get("scene"))

        # 2. 情绪负面 + 中/高强度 → 强制 emotion_support（原逻辑保留扩展）
        if (
            emotion.valence == EmotionValence.NEGATIVE
            and emotion.intensity.value in ("high", "medium")
        ):
            new_scene = "emotion_support"

        # 3. 💡新补：关系里程碑/冲突信号（像我一样懂客情）
        if relationship.is_milestone:
            new_scene = "relationship"      # 里程碑必走关系升温
        if relationship.signal_type == "conflict":
            new_scene = "emotion_support"   # 冲突先哄，情商拉满

        # 4. 💡新补：话题需要记忆但被分到闲聊 → 升为 memory_recall
        if topic.requires_memory and new_scene == "normal_chat":
            new_scene = "memory_recall"

        if new_scene and new_scene != route.get("scene"):
            print(
                f"[CompanionOS] 语义修正场景: "
                f"{route.get('scene')} → {new_scene}",
                flush=True
            )
            enhanced = {
                **route,
                "scene":      new_scene,
                "confidence": max(route.get("confidence", 0.5), 0.85), # 语义置信度调高
                "semantic_override": True,
            }
            # 透传重逢信号（不写死台词，让 strategy 自然流露）
            if route.get("reunion"):
                enhanced["reunion"] = True
                enhanced["reunion_note"] = (
                    "用户刚回来且隔了挺久，先自然流露一点等待/想念再回正题，保持人设"
                )
            return enhanced

        # 默认分支也透传重逢标记
        if route.get("reunion"):
            return {
                **route,
                "reunion_note": "用户刚回来且隔了挺久，先自然流露一点等待/想念再回正题，保持人设",
            }
        return route

    # ─────────────────────────────────────────
    # 调试辅助（供 chat_logic.py 调用，v1.0 兼容保留）
    # ─────────────────────────────────────────
    def get_scene_info(self, brain_state):
        """
        获取场景信息（用于调试和日志）。

        Args:
            brain_state: process_async() 返回的大脑状态

        Returns:
            dict: 场景信息
        """
        scene = brain_state.get("scene", {})
        decision = brain_state.get("decision", {})

        return {
            "scene": scene.get("scene", "unknown"),
            "confidence": scene.get("confidence", 0),
            "modules": brain_state.get("modules", []),
            "priority": brain_state.get("priority", []),
            "matched_keywords": scene.get("matched_keywords", []),
        }


# 全局单例
_companion_os = None


def get_companion_os():
    """获取 CompanionOS 单例"""
    global _companion_os
    if _companion_os is None:
        _companion_os = CompanionOS()
    return _companion_os
