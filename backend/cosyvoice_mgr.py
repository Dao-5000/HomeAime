# -*- coding: utf-8 -*-
"""
CosyVoice 本地 TTS 惰性管理器（2026-09-12）

过去：Electron 启动拉起 + run.py 启动拉起 + 启动预热 + scheduler watchdog 兜底
     → 每次进 APP 就把 TTS 服务常驻拉起，白吃内存。
现在：启动路径全部拆除；谁真正要用语音（气泡配音 / 通话 / 唱歌 / 克隆），
     谁调用 ensure() 按需拉起；空闲 COSYVOICE_IDLE_SHUTDOWN_MIN 分钟自动关闭
     （config.json 可配，0 = 常驻不关）；拉不起 / 没部署 → 调用方统一不发语音，
     文字内容照常送达（内容由各调用点的既有兜底保证，本模块只管"起不起"）。

只关我们自己拉起的进程（_OWNER）；用户手动跑的服务不归我们杀。
"""
import asyncio
import atexit
import os
import socket
import subprocess
import threading
import time

_PORT = 9881
_LOCK = threading.Lock()
_PROC = None            # 我们自己拉起的 Popen 句柄
_OWNER = False          # 是否拥有杀进程的权利（只杀自己拉的）
_LAST_USED = [0.0]
_REAPER_ON = False


def _log(msg):
    try:
        print(f"[CosyVoice-mgr] {msg}", flush=True)
    except Exception:
        pass


def port_up(timeout: float = 0.4) -> bool:
    """9881 端口是否有服务监听（毫秒级，可频繁调用）。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", _PORT))
            return True
        finally:
            s.close()
    except Exception:
        return False


def _candidates():
    try:
        from . import config as _cfg
        root = str(_cfg.ROOT_DIR)
    except Exception:
        root = os.getcwd()
    return [
        os.environ.get("COSYVOICE_HOME", ""),
        os.path.join(root, "CosyVoice"),
        os.path.join(os.path.dirname(root), "CosyVoice"),
        r"%COSYVOICE_HOME%",
    ]


def _idle_minutes() -> float:
    try:
        from . import config as _cfg
        return float(_cfg.get("COSYVOICE_IDLE_SHUTDOWN_MIN", 10))
    except Exception:
        return 10.0


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                           capture_output=True, timeout=5)
        else:
            proc.terminate()
    except Exception:
        pass


def touch():
    """合成成功后调用：刷新空闲计时。"""
    _LAST_USED[0] = time.time()


def _spawn_locked() -> bool:
    """持 _LOCK 调用。返回 False = 没部署/拉起失败。"""
    global _PROC, _OWNER, _REAPER_ON
    if port_up():
        return True
    if _PROC is not None and _PROC.poll() is None:
        return True   # 我们已经拉起、uvicorn 还在启动中
    _cv_dir = next((p for p in _candidates() if p and os.path.isdir(p)), "")
    _cv_py = os.path.join(_cv_dir, ".venv", "Scripts", "python.exe") if _cv_dir else ""
    if not _cv_dir or not os.path.exists(_cv_py):
        _log("未部署（找不到 CosyVoice 目录/.venv），跳过拉起")
        return False
    log_path = os.path.join(_cv_dir, "server_lazy.log")
    try:
        lf = open(log_path, "a", encoding="utf-8")
        _PROC = subprocess.Popen(
            [_cv_py, "-m", "uvicorn", "server:app",
             "--host", "127.0.0.1", "--port", str(_PORT)],
            cwd=_cv_dir, env=dict(os.environ),
            stdout=lf, stderr=lf,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _OWNER = True
        _LAST_USED[0] = time.time()
        if not _REAPER_ON:
            _REAPER_ON = True
            threading.Thread(target=_reaper, daemon=True).start()
            atexit.register(_shutdown_at_exit)
        _log(f"已后台拉起 (pid={_PROC.pid}, dir={_cv_dir})，模型首次合成时才加载")
        return True
    except Exception as e:
        _log(f"拉起失败(静默): {e}")
        return False


def _reaper():
    """空闲看门狗：空闲超过阈值自动关闭，释放内存（只关我们自己拉起的）。"""
    while True:
        time.sleep(30)
        try:
            idle_min = _idle_minutes()
            if idle_min <= 0 or not _OWNER or _PROC is None:
                continue
            if time.time() - _LAST_USED[0] < idle_min * 60:
                continue
            if not port_up():
                continue
            _log(f"空闲超过 {idle_min:.0f} 分钟，自动关闭释放内存（下次要用会再拉起）")
            _kill_tree(_PROC)
        except Exception:
            pass


def _shutdown_at_exit():
    try:
        if _OWNER and _PROC is not None and _PROC.poll() is None:
            _log("后端退出，顺手关闭 TTS 服务")
            _kill_tree(_PROC)
    except Exception:
        pass


def ensure(wait_seconds: float = 0.0) -> bool:
    """同步版：没运行就拉起（毫秒级 Popen，不阻塞）；wait_seconds>0 时轮询等端口就绪。
    返回当前是否可用。"""
    if port_up():
        _LAST_USED[0] = time.time()
        return True
    with _LOCK:
        ok = _spawn_locked()
    if not ok:
        return port_up()
    if wait_seconds > 0:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if port_up(0.3):
                _LAST_USED[0] = time.time()
                return True
            time.sleep(0.5)
        return port_up()
    return port_up()


async def ensure_async(wait_seconds: float = 0.0) -> bool:
    """异步版：等待期用 asyncio.sleep 让出事件循环（拉不起时不卡其他请求）。"""
    if port_up():
        _LAST_USED[0] = time.time()
        return True
    with _LOCK:
        ok = _spawn_locked()
    if not ok:
        return port_up()
    if wait_seconds > 0:
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if port_up(0.3):
                _LAST_USED[0] = time.time()
                return True
            await asyncio.sleep(0.5)
        return port_up()
    return port_up()
