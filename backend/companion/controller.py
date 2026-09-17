# -*- coding:utf-8 -*-
"""
Companion Controller v1.0
AI伴侣统一控制器：

  定位：
  - 不是存数据
  - 不是计算关系
  - 只是调度已有模块

  调度的模块：
  - character_manager（人设）
  - db.get_profile（用户画像）
  - relationship.RelationshipManager（关系状态）
  - memory_manager（长期记忆）
  - db.get_emotion_state（情绪状态）
  - db.get_open_loops（未完成事项）
  - intimacy_manager（亲密度策略）
"""
from .context import CompanionContext

import asyncio

from .. import (
    db,
    character_manager,
    memory_manager,
    intimacy_manager,
)
from ..db import (
    _run_sync,
    async_get_profile,
    async_get_emotion_state,
    async_get_open_loops,
)
from ..relationship.manager import RelationshipManager


class CompanionController:
    """
    AI伴侣统一控制器：
    负责调度已有模块，收集所有上下文信息。
    """

    async def build(
        self,
        session_id,
        character_id,
        user_message,
        image=None,
        messages=None,
        style_override=None
    ):
        """
        构建完整的伴侣上下文。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            user_message: 用户当前消息
            image: 用户发送的图片（URL/base64/本地路径），可选
            messages: 完整消息列表（可选）
            style_override: 请求级风格覆盖（如 action_brackets）

        Returns:
            CompanionContext: 统一的上下文对象
        """
        ctx = CompanionContext()

        # ══════════════════════════════════════════
        # 第一批：角色配置（其他步骤的前置依赖，串行）
        # ══════════════════════════════════════════
        try:
            # ★ P0-3：统一走 get_character_any（新格式优先、legacy 兜底）。
            #   原先手写的 load_character or get_character 与 get_character_any 语义等价，
            #   但散落各处容易漏改；统一成一个入口，保证所有调用点读取语义一致。
            _char_dict = character_manager.get_character_any(character_id)
            _char_prompt = character_manager.build_system_prompt(
                character_id,
                character_id=character_id,
                session_id=session_id,
                style_override=style_override,
                user_message=user_message or ""
            )
            ctx.set("character",        _char_dict)
            ctx.set("character_prompt", _char_prompt)
        except Exception as e:
            print(f"[CompanionController] 加载人设失败: {e}", flush=True)

        # ══════════════════════════════════════════
        # 第二批：独立IO全部并行（不互相依赖）
        # ══════════════════════════════════════════

        async def _load_profile():
            try:
                profile = await async_get_profile(session_id, character_id)
                # ★ 全局「我的信息」：从 character_id=default 补结构化字段（生日/性别/城市/简介/话题），角色画像优先
                if character_id and character_id != "default":
                    try:
                        _global = await async_get_profile(session_id, "default")
                        if _global:
                            # 角色档案常带空字符串/空 JSON 占位，不能覆盖全局“我的信息”。
                            merged = dict(_global)
                            for _k, _v in (profile or {}).items():
                                if _v not in (None, "", [], {}, "{}"):
                                    merged[_k] = _v
                            profile = merged
                    except Exception:
                        pass
                if profile:
                    _filtered = _filter_profile_fields(profile, user_message, threshold=0.15)
                    if _filtered:
                        ctx.set("profile", _filtered)
            except Exception as e:
                print(f"[Controller] 用户画像失败: {e}", flush=True)

        async def _load_relationship():
            try:
                rel = RelationshipManager().get_state(session_id, character_id)
                ctx.set("relationship", rel)
            except Exception as e:
                print(f"[Controller] 关系状态失败: {e}", flush=True)

        async def _load_memory():
            try:
                # ★ 改：同步DB调用移到这里，由 gather 并行执行，不再阻塞主流程
                _recent_sync = await _run_sync(
                    db.recent_messages, session_id, 6, character_id
                )
                _mem_query = memory_manager.build_memory_query(
                    user_message, _recent_sync
                )
                # ★ 理解层 routing.need_memory：用户提到以前的事/共同经历时加强召回。
                #   时序说明：main.py 的语义闸门先调 understand() 并把结果写进 kv
                #   （understanding._persist），本函数在其之后执行，所以这里读到的是
                #   **本轮**的路由决策，不是上一轮的。读不到则保守取 False（行为不变）。
                _boost = False
                try:
                    from ..understanding import get_routing
                    _boost = bool(
                        (get_routing(session_id, character_id) or {}).get("need_memory"))
                except Exception:
                    _boost = False
                mem = await _run_sync(
                    memory_manager.memory_block,
                    query=_mem_query,
                    session_id=session_id,
                    character_id=character_id,
                    user_message=user_message or "",   # ★ 三路召回：场景加权
                    boost=_boost                       # ★ 理解层：加强召回
                )
                ctx.set("memory", mem)
            except Exception as e:
                print(f"[Controller] 长期记忆失败: {e}", flush=True)

        async def _load_emotion():
            try:
                emotion = await async_get_emotion_state(session_id, character_id)
                if emotion:
                    ctx.set("emotion", emotion)
            except Exception as e:
                print(f"[Controller] 情绪状态失败: {e}", flush=True)

        async def _load_open_loops():
            try:
                loops = await async_get_open_loops(session_id, character_id)
                if loops:
                    ctx.set("open_loops", loops)
            except Exception as e:
                print(f"[Controller] 未完成事项失败: {e}", flush=True)

        async def _load_intimacy():
            try:
                from ..intimacy_manager import style_block
                blk = await _run_sync(style_block, session_id, character_id)
                if blk:
                    ctx.set("intimacy", blk)
            except Exception as e:
                print(f"[Controller] 亲密度策略失败: {e}", flush=True)

        async def _load_ai_state():
            try:
                from .ai_state.manager import AIStateManager
                ai_state = AIStateManager().get_state(session_id, character_id)
                ctx.set("ai_state", ai_state)
            except Exception as e:
                print(f"[Controller] AI自身状态失败: {e}", flush=True)

        async def _load_timeline():
            try:
                # ★ 保留真身主链路：TimelineManager（不用 db 版查询）
                from ..timeline.manager import TimelineManager
                events = TimelineManager().get_recent_events(
                    session_id, character_id, limit=5
                )
                ctx.set("timeline", events)
            except Exception as e:
                print(f"[Controller] 时间线失败: {e}", flush=True)

        async def _load_life_profile():
            try:
                from ..profile_memory.manager import LifeProfileManager
                profiles = LifeProfileManager().get(session_id, character_id, limit=10)
                ctx.set("life_profile", profiles)
            except Exception as e:
                print(f"[Controller] 人生档案失败: {e}", flush=True)

        async def _load_behavior():
            try:
                from ..behavior.strategy import get_behavior_patterns
                patterns = get_behavior_patterns(session_id, character_id, limit=5)
                ctx.set("behavior_pattern", patterns)
            except Exception as e:
                print(f"[Controller] 行为模式失败: {e}", flush=True)

        async def _load_feedback():
            try:
                from ..feedback.learner import FeedbackLearner
                fb = FeedbackLearner().get_profile(session_id, character_id)
                ctx.set("feedback_profile", fb)
            except Exception as e:
                print(f"[Controller] 反馈偏好失败: {e}", flush=True)

        async def _load_corrections():
            """用户纠正过的事实性知识（"你以为是 X，其实是 Y"）。

            与 feedback_profile 的区别：后者是风格统计（喜欢温柔/简短），
            前者是**可执行的事实**——用户亲自教过的、必须改过来的东西。
            """
            try:
                from ..feedback.corrections import build_corrections_prompt
                ctx.set("corrections",
                        build_corrections_prompt(session_id, character_id) or "")
            except Exception as e:
                print(f"[Controller] 用户纠正加载失败: {e}", flush=True)

        async def _load_kg():
            try:
                from ..knowledge_graph.prompt import build_knowledge_prompt
                kg = build_knowledge_prompt(session_id, character_id, user_message)
                if kg:
                    ctx.set("knowledge_graph", kg)
            except Exception as e:
                print(f"[Controller] 知识图谱失败: {e}", flush=True)

        async def _load_reflection():
            try:
                from ..reflection.strategy import build_reflection_prompt
                ref = build_reflection_prompt(session_id, character_id)
                if ref:
                    ctx.set("reflection", ref)
            except Exception as e:
                print(f"[Controller] 记忆反思失败: {e}", flush=True)

        async def _load_identity():
            try:
                from ..identity.manager import IdentityManager
                identity = IdentityManager().build_identity_prompt(session_id, character_id)
                if identity:
                    ctx.set("identity", identity)
            except Exception as e:
                print(f"[Controller] AI身份失败: {e}", flush=True)

        async def _load_multimodal():
            try:
                from ..multimodal.manager import MultimodalManager
                mm  = MultimodalManager()
                mmc = await mm.process_async(
                    message=user_message,
                    image=image,
                    include_environment=True
                )
                ctx.set("multimodal",        mmc.export())
                ctx.set("multimodal_prompt", mm.build_prompt(mmc))
            except Exception as e:
                print(f"[Controller] 多模态感知失败: {e}", flush=True)

        # ★ 全部并行——15个独立IO同时跑，不互相等待
        await asyncio.gather(
            _load_profile(),
            _load_relationship(),
            _load_memory(),
            _load_emotion(),
            _load_open_loops(),
            _load_intimacy(),
            _load_ai_state(),
            _load_timeline(),
            _load_life_profile(),
            _load_behavior(),
            _load_feedback(),
            _load_corrections(),
            _load_kg(),
            _load_reflection(),
            _load_identity(),
            _load_multimodal(),
            return_exceptions=True   # 任何一个失败不影响其他
        )

        # ══════════════════════════════════════════
        # 第三批：依赖第一/二批结果的计算步骤（必须串行）
        # ══════════════════════════════════════════

        # 步骤12：人格状态（依赖 character）
        try:
            from ..personality.manager import PersonalityManager
            personality_state = await _run_sync(
                PersonalityManager().get_state, session_id, character_id
            )
            ctx.set("personality_state", personality_state)
        except Exception as e:
            print(f"[Controller] 人格状态失败: {e}", flush=True)

        # 步骤13：最终人格计算（依赖 character + personality_state）
        try:
            from ..personality.builder import (
                extract_base_traits,
                build_final_personality,
                get_personality_delta_from_state,
            )
            character_config  = ctx.get("character", {})
            personality_state = ctx.get("personality_state", {})
            base_traits       = extract_base_traits(character_config)
            delta             = get_personality_delta_from_state(personality_state)
            final_personality = build_final_personality(base_traits, delta, character_id)
            ctx.set("final_personality", final_personality)
        except Exception as e:
            print(f"[Controller] 最终人格计算失败: {e}", flush=True)

        return ctx


def _filter_profile_fields(
    profile: dict,
    user_message: str,
    threshold: float = 0.15
) -> dict:
    """
    按当前对话语义相关性动态筛选画像字段
    复用 memory_manager._relevance()，不重复造轮子

    始终保留：nickname（称呼必须知道）
    阈值以下：跳过（不注入）
    阈值以上：注入
    兜底：如果所有字段都低于阈值，返回完整画像（总比空好）
    """
    if not profile or not user_message:
        return profile

    try:
        from ..memory.memory_manager import _relevance
    except ImportError:
        return profile   # 导入失败降级完整注入

    # 始终保留的字段
    ALWAYS_INCLUDE = {"nickname", "id", "session_id", "character_id",
                      "updated_time", "ext_json"}

    scored_fields = {}
    for field, value in profile.items():
        if field in ALWAYS_INCLUDE:
            continue
        if not value or not str(value).strip():
            continue
        score = _relevance(user_message, str(value))
        scored_fields[field] = score

    # 兜底：全低于阈值时返回完整画像
    if not any(v >= threshold for v in scored_fields.values()):
        return profile

    # 筛选：高于阈值的字段 + 始终保留的字段
    result = {k: v for k, v in profile.items() if k in ALWAYS_INCLUDE}
    for field, score in scored_fields.items():
        if score >= threshold:
            result[field] = profile[field]

    return result


# 全局单例
_controller_instance = None


def get_controller():
    """获取 CompanionController 全局单例"""
    global _controller_instance
    if _controller_instance is None:
        _controller_instance = CompanionController()
    return _controller_instance
