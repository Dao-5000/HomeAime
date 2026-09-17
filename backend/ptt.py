# -*- coding: utf-8 -*-
"""
按住说话（Push-To-Talk）：全局键盘钩子。

按住设定键（默认 F9）→ 推 ptt_start 给前端（开始录音）；
松开 → 推 ptt_stop（前端停止录音 → 发 /api/pc/voice_control → 识别 → 控制电脑）。

Windows 用 keyboard 库（低级钩子，无需管理员）；软件最小化时也生效。
事件经 ws_manager 推给渲染进程（active_session 兜底定位当前会话）。
"""
import threading
import logging

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()
_key = "f9"
_state = {"down": False}


def _emit(kind: str):
    """把 ptt 事件推给前端所有活跃会话（钩子线程 → 主事件循环）。"""
    try:
        import asyncio
        from .main import ws_manager, active_session
        sid = active_session() or "default"

        async def _push():
            try:
                await ws_manager.push_to_session(sid, {"type": kind, "key": _key})
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_push())
        except RuntimeError:
            try:
                from .main import get_loop
                asyncio.run_coroutine_threadsafe(_push(), get_loop())
            except Exception:
                pass
    except Exception:
        pass


def _on_key(event):
    try:
        name = str(event.name or "").lower()
        if name != _key:
            return
        etype = str(getattr(event, "event_type", "") or "").lower()
        if etype in ("down", "key_down"):
            if not _state["down"]:
                _state["down"] = True
                _emit("ptt_start")
        elif etype in ("up", "key_up"):
            if _state["down"]:
                _state["down"] = False
                _emit("ptt_stop")
    except Exception:
        pass


def start(key: str = "f9") -> bool:
    """启动全局钩子（进程内只启动一次）。返回是否成功。"""
    global _started, _key
    with _lock:
        if _started:
            return True
        try:
            import keyboard as _kb
            _key = (key or "f9").strip().lower()
            _kb.hook(_on_key)
            _started = True
            logger.info(f"[PTT] 按住说话已启动（按住 {_key} 对她说话，松开执行）")
            return True
        except Exception as e:
            logger.warning(f"[PTT] 启动失败: {e}")
            return False


def is_running() -> bool:
    return _started
