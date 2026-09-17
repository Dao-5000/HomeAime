# -*- coding:utf-8 -*-
"""
Proactive Triggers v1.0
主动消息触发规则：
  检测各种触发条件，返回触发类型和优先级。
"""
# ★ 2026-09-14 修：这行 import 原先被写在上面 docstring 的**内部**（第 4 行），
#   被当成文档文本，从未真正执行 → 模块内 json.loads（:376）/ json.dumps（:420）
#   双双 NameError，被 except 静默吞掉 → 屏幕记忆的「2 小时去重」完全失效，
#   同一画面会反复插入长期记忆（额外 insert_memory + 向量写入，并污染记忆库）。
import json


def check_inactive(inactive_hours):
    """
    用户长时间未聊天触发。

    Args:
        inactive_hours: 未聊天小时数

    Returns:
        dict or None: 触发信息
    """
    if inactive_hours >= 24:
        return {
            "type": "miss_you",
            "priority": 5,
            "reason": "用户长时间未聊天，表达想念"
        }
    elif inactive_hours >= 12:
        return {
            "type": "check_in",
            "priority": 3,
            "reason": "用户半天未聊天，简单问候"
        }
    return None


def check_emotion(emotion):
    """
    用户情绪状态触发。

    Args:
        emotion: 情绪状态字典

    Returns:
        dict or None: 触发信息
    """
    if not emotion:
        return None

    mood = emotion.get("mood", "")
    intensity = emotion.get("intensity", 0) or 0

    if mood in ["sad", "anxious", "angry", "压力", "难过", "焦虑", "生气"]:
        priority = 8 + min(intensity, 10) // 2
        return {
            "type": "comfort",
            "priority": min(priority, 10),
            "reason": f"用户情绪状态：{mood}，需要安慰"
        }

    if mood in ["happy", "开心", "兴奋"]:
        return {
            "type": "celebrate",
            "priority": 6,
            "reason": "用户情绪积极，可以分享喜悦"
        }

    return None


def check_open_loop(loops):
    """
    未完成事项触发。

    Args:
        loops: 未完成事项列表

    Returns:
        dict or None: 触发信息
    """
    if not loops:
        return None

    # 找到最重要的未完成事项
    max_importance = 0
    for loop in loops:
        importance = loop.get("importance", 5) or 5
        if importance > max_importance:
            max_importance = importance

    priority = 5 + min(max_importance, 10) // 2

    return {
        "type": "follow_up",
        "priority": min(priority, 10),
        "reason": f"有{len(loops)}个未完成事项需要跟进"
    }


def check_relationship_milestone(relationship):
    """
    关系里程碑触发。

    Args:
        relationship: 关系状态字典

    Returns:
        dict or None: 触发信息
    """
    if not relationship:
        return None

    stage = relationship.get("stage", "")
    closeness = relationship.get("closeness", 0) or 0

    # 关系刚进入新阶段时触发
    if closeness in [20, 40, 70, 90]:
        return {
            "type": "relationship_milestone",
            "priority": 7,
            "reason": f"关系进入新阶段：{stage}"
        }

    return None


def check_time_context(hour):
    """
    时间场景触发。

    Args:
        hour: 当前小时（0-23）

    Returns:
        dict or None: 触发信息
    """
    if 6 <= hour <= 9:
        return {
            "type": "morning_greeting",
            "priority": 4,
            "reason": "早晨问候"
        }
    elif 11 <= hour <= 13:
        return {
            "type": "lunch_reminder",
            "priority": 3,
            "reason": "午间关心"
        }
    elif 22 <= hour or hour <= 1:
        return {
            "type": "night_care",
            "priority": 5,
            "reason": "夜间陪伴"
        }

    return None


def check_should_call(context: dict) -> dict | None:
    """
    判断是否应该主动发起语音通话。
    比文字消息门槛高得多——只有在特定时机+高亲密度时才触发。

    触发条件（任一满足）：
      A. 深夜陪伴：22-24点 + 亲密度≥60 + 用户情绪低落
      B. 久别重逢：离线≥8h + 亲密度≥70 + 当前时间在活跃时段
      C. 情绪危机：情绪强度≥8 + 亲密度≥50（需要声音陪伴）
      D. 关系里程碑：认识第7/30/100天 + 亲密度≥60
      E. 节假日夜晚：节日当天 + 20-23点 + 亲密度≥70

    Args:
        context: {
            hour: int,
            closeness: int,
            inactive_hours: float,
            emotion_mood: str,
            emotion_intensity: float,
            is_festival: bool,
            interaction_days: int,
        }

    Returns:
        dict or None: {type, priority, reason, call_reason}
    """
    hour         = context.get("hour", 12)
    closeness    = context.get("closeness", 0)
    inactive_h   = context.get("inactive_hours", 0)
    mood         = context.get("emotion_mood", "")
    intensity    = float(context.get("emotion_intensity", 0) or 0)
    is_festival  = context.get("is_festival", False)
    inter_days   = context.get("interaction_days", 0)

    NEGATIVE_MOODS = {"sad", "anxious", "angry", "压力", "难过", "焦虑", "生气", "upset", "worried"}

    # A. 深夜陪伴
    if 22 <= hour <= 24 and closeness >= 60 and mood in NEGATIVE_MOODS:
        return {
            "type":        "call_night_comfort",
            "priority":    9,
            "reason":      "深夜情绪低落，声音陪伴比文字更有温度",
            "call_reason": "night_comfort",
        }

    # B. 久别重逢
    if inactive_h >= 8 and closeness >= 70 and 9 <= hour <= 22:
        return {
            "type":        "call_reunion",
            "priority":    8,
            "reason":      f"已经{int(inactive_h)}小时没见到你了，想听你声音",
            "call_reason": "reunion",
        }

    # C. 情绪危机（声音比文字更有力量）
    if intensity >= 8 and closeness >= 50:
        return {
            "type":        "call_crisis_comfort",
            "priority":    10,
            "reason":      "情绪强度很高，需要声音陪伴",
            "call_reason": "crisis_comfort",
        }

    # D. 关系里程碑
    if inter_days in (7, 30, 100) and closeness >= 60:
        return {
            "type":        "call_milestone",
            "priority":    7,
            "reason":      f"认识第{inter_days}天，想用声音庆祝",
            "call_reason": "milestone",
        }

    # E. 节假日夜晚
    if is_festival and 20 <= hour <= 23 and closeness >= 70:
        return {
            "type":        "call_festival",
            "priority":    7,
            "reason":      "节日夜晚，想打电话陪你",
            "call_reason": "festival",
        }

    return None


def check_screen_context(screen_state: dict):
    """
    检查屏幕感知结果，决定是否触发主动联系。

    screen_state 格式（由 vision_analyzer 返回）：
        {
            "summary": str,       # "主人在写代码"
            "activity": str,     # coding / video / gaming / browsing / idle / other
            "engagement": int,    # 0-100 参与度
            "interesting": bool, # 是否值得打扰
            "topic_hint": str,   # "看起来在debug"
        }

    Returns:
        None 或 dict {
            "type": "screen_observation",
            "priority": int,
            "reason": str,
            "context": dict,
        }
    """
    if not screen_state or not isinstance(screen_state, dict):
        return None

    activity = screen_state.get("activity", "")
    engagement = screen_state.get("engagement", 50)
    interesting = screen_state.get("interesting", False)
    topic_hint = screen_state.get("topic_hint", "")
    summary = screen_state.get("summary", "")
    # 屏幕观察文案（供 reason 使用）；修复原规范中 system_hint 未定义的问题
    system_hint = topic_hint or summary or activity

    # 待机/锁屏不打扰
    if activity == "idle":
        return None

    # ===== Q2/Q4：记忆写入 + 通话联动（idle 已跳过，其余每次有效观察都执行） =====
    _write_screen_memory(
        screen_state,
        screen_state.get("session_id", ""),
        screen_state.get("character_id", ""),
    )
    _inject_to_voice_session(screen_state)

    # 高参与度（专注工作/游戏）→ 低优先级轻撩
    if engagement >= 80:
        if topic_hint:
            return {
                "type": "screen_observation",
                "priority": 3,
                "reason": f"看到你在{system_hint}",
                "context": {
                    "activity": activity,
                    "topic_hint": topic_hint,
                    "tone_hint": "soft",
                },
            }
        return None

    # 低参与度（可能在摸鱼/浏览）→ 中优先级找话题
    if engagement <= 50 and interesting:
        return {
            "type": "screen_observation",
            "priority": 6,
            "reason": f"注意到{summary}，想找你聊几句",
            "context": {
                "activity": activity,
                "topic_hint": topic_hint,
                "tone_hint": "casual",
            },
        }

    # 有趣场景（看视频/游戏）→ 优先级7，主动切入
    if activity in ("video", "gaming") and topic_hint:
        return {
            "type": "screen_observation",
            "priority": 7,
            "reason": f"看到{summary}，想凑过去一起看/玩",
            "context": {
                "activity": activity,
                "topic_hint": topic_hint,
                "tone_hint": "excited",
            },
        }

    return None


_memory_mgr = None
def _get_memory_mgr():
    global _memory_mgr
    if _memory_mgr is None:
        from ..memory.manager import MemoryManager
        _memory_mgr = MemoryManager()
    return _memory_mgr


def _write_screen_memory(screen_state: dict, session_id: str = "", character_id: str = ""):
    """
    将有价值的屏幕观察写入长期记忆（Q2）。
    条件：参与度<75（可能在摸鱼）或有明显兴趣话题；且屏幕记忆联动开关开启。
    """
    if not screen_state or not isinstance(screen_state, dict):
        return

    # 屏幕记忆联动开关（前端设置 → 后端 config）；关闭则不写
    try:
        from .. import config as _cfg
        if _cfg.get("SCREEN_MEMORY_ENABLED", True) is False:
            return
    except Exception:
        pass

    engagement  = screen_state.get("engagement", 50)
    interesting = screen_state.get("interesting", False)
    topic_hint  = screen_state.get("topic_hint", "")
    summary     = screen_state.get("summary", "")
    activity    = screen_state.get("activity", "")
    session_id  = session_id or screen_state.get("session_id", "default_user")
    # 旧上下文没有 character_id 时沿用 session 隔离键；新调用链会显式传角色。
    character_id = character_id or screen_state.get("character_id") or session_id or "default"

    # 定时截屏可能连续得到相同结果；同角色两小时内不重复写同一场景。
    try:
        import hashlib
        import time as _time
        from .. import db as _db
        signature = hashlib.sha1(
            f"{activity}|{summary}|{topic_hint}".encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        dedupe_key = f"screen_memory:last:{session_id}:{character_id}"
        previous = _db.kv_get(dedupe_key)
        if previous:
            try:
                prev_obj = json.loads(previous)
                if (
                    prev_obj.get("signature") == signature
                    and _time.time() - float(prev_obj.get("time", 0) or 0) < 7200
                ):
                    return
            except Exception:
                pass
    except Exception:
        signature = ""

    # 过滤：专注工作不记
    if engagement >= 75 and not topic_hint:
        return

    memory_type_map = {
        "coding": "工作/学习",
        "video": "娱乐/视频",
        "gaming": "游戏",
        "browsing": "浏览",
        "idle": "日常",
        "other": "日常",
    }
    memory_type = memory_type_map.get(activity, "日常")
    importance  = 5 if engagement < 60 else 4  # 摸鱼 importance 更高

    if interesting and topic_hint:
        content = f"用户在{session_id}的屏幕上：{summary}，提到「{topic_hint}」"
    elif summary:
        content = f"用户在{session_id}的屏幕上：{summary}"
    else:
        return

    try:
        mgr = _get_memory_mgr()
        mgr.add_memory(
            user_id=session_id,
            memory_type=memory_type,
            content=content,
            importance=importance,
            character_id=character_id
        )
        if signature:
            try:
                _db.kv_set(dedupe_key, json.dumps({
                    "signature": signature, "time": _time.time()
                }, ensure_ascii=False))
            except Exception:
                pass
        print(f"[ScreenMemory] 写入记忆: {content}", flush=True)
    except Exception as e:
        print(f"[ScreenMemory] 记忆写入失败(静默): {e}", flush=True)


def _inject_to_voice_session(screen_state: dict):
    """
    把当前屏幕状态注入所有活跃的语音通话会话。
    Q4 核心：通话时 AI 能看到屏幕，一起追剧/游戏/刷视频。
    """
    if not screen_state:
        return
    try:
        from ..voice_call import _active_sessions
        if not _active_sessions:
            return
        target_session = str(screen_state.get("session_id") or "")
        target_character = str(screen_state.get("character_id") or "")
        for session in _active_sessions.values():
            if target_session and str(getattr(session, "session_id", "")) != target_session:
                continue
            if target_character and str(getattr(session, "character_id", "")) != target_character:
                continue
            if hasattr(session, '_screen_state'):
                session._screen_state.update(screen_state)
    except Exception:
        pass
