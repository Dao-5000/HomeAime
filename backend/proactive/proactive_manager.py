# coding=utf-8
"""
屏幕感知总调度
- 管理截图 → 视觉分析 → 触发评估 → 注入上下文
- 接入现有 proactive/idle_agent.py 的调度循环（不改动 idle_agent 核心逻辑）
"""
import time
from typing import Optional

from .. import config
from .context import ProactiveContext
from .decision_engine import get_engine
from .screen_capture import save_capture, capture_local, save_base64_capture
from .vision_analyzer import analyze_screen
from .triggers import check_screen_context

# 截图间隔（秒），默认 5 分钟
_SCREEN_INTERVAL = 300
_last_capture = 0.0


def is_screen_enabled() -> bool:
    """从 config 读取屏幕感知开关，默认关。受感知总开关 AWARENESS_ENABLED 门控。"""
    try:
        # 总开关关闭 → 截图/窗口/键鼠 全部不采集
        if not config.get("AWARENESS_ENABLED", False):
            return False
        return bool(config.get("SCREEN_PERCEPTION_ENABLED", False))
    except Exception:
        return False


def capture_and_analyze(session_id: str = "default", character_id: str = "default") -> Optional[dict]:
    """
    执行一次截图+视觉分析（同步，阻塞 IO）。
    由 idle_agent 调度循环通过线程池调用，避免卡住事件循环。
    """
    global _last_capture

    if not is_screen_enabled():
        return None

    # 观察间隔支持从 config 动态读取（前端设置页可调）
    interval = int(config.get("SCREEN_INTERVAL", _SCREEN_INTERVAL) or _SCREEN_INTERVAL)
    now = time.time()
    if now - _last_capture < interval:
        return None
    _last_capture = now

    # 1. 截图（优先本地截屏 mss，需 pip install mss）
    image_path = None
    try:
        raw = capture_local()
        if raw:
            image_path = save_capture(raw)
    except Exception as e:
        print(f"[ProactiveManager] 截图失败: {e}", flush=True)

    if not image_path:
        return None

    # 2. 视觉分析
    result = analyze_screen(image_path, "你", True)
    if result:
        # 让后续主动消息/通话注入时始终携带隔离键，避免角色之间串屏幕状态。
        result = dict(result)
        result["session_id"] = session_id
        result["character_id"] = character_id
    return result


def build_screen_context(
    screen_result: dict, session_id: str = "default", character_id: str = "default"
) -> dict:
    """
    把视觉分析结果格式化成 ProactiveContext.screen_state 兼容的 dict。
    直接用于 ProactiveContext.update_screen()。
    """
    if not screen_result:
        return {}
    return {
        "summary": screen_result.get("summary", ""),
        "activity": screen_result.get("activity", "other"),
        "engagement": screen_result.get("engagement", 50),
        "interesting": screen_result.get("interesting", False),
        "topic_hint": screen_result.get("topic_hint", ""),
        "scene_name": screen_result.get("scene_name", ""),
        "session_id": session_id,
        "character_id": character_id,
        "captured_at": time.time(),
    }


def handle_screen_trigger(screen_state: dict, current_context: ProactiveContext) -> Optional[dict]:
    """
    给定屏幕状态，走一遍决策引擎。
    返回决策结果（有触发）或 None（不打扰）。
    """
    current_context.update_screen(screen_state)
    engine = get_engine()
    return engine.decide(current_context)


async def capture_and_analyze_for_user(
    session_id: str = "default", character_id: str = "default"
) -> Optional[dict]:
    """
    为指定用户截图并分析，返回结果字典。
    供前端手动触发（"你看看我在干嘛"）调用（Q3）。
    """
    from .screen_capture import capture_local, save_capture
    from .vision_analyzer import analyze_screen
    from .triggers import _write_screen_memory

    raw = capture_local()
    if not raw:
        return None

    image_path = save_capture(raw)
    if not image_path:
        return None

    result = analyze_screen(image_path)
    if not result:
        return None

    # 写入记忆（受屏幕记忆联动开关控制）
    if result.get("interesting"):
        _write_screen_memory(result, session_id, character_id)

    return {
        "summary": result.get("summary", ""),
        "activity": result.get("activity", ""),
        "engagement": result.get("engagement", 0),
        "topic_hint": result.get("topic_hint", ""),
        "scene_name": result.get("scene_name", ""),
        "interesting": result.get("interesting", False),
        "session_id": session_id,
        "character_id": character_id,
    }
