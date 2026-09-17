# coding=utf-8
"""
屏幕截取模块
- 支持前端转发截图（getDisplayMedia）
- 支持后端本地截屏（mss 兜底，Windows/Mac/Linux）
- 隐私提示始终显示
"""
import base64
import io
import time
import uuid
from pathlib import Path
from typing import Optional

from .. import config

# 截图缓存目录（★ 显式固定 D 盘，绝不在 C 盘；自动清理防堆积）
_SCREEN_DIR = config.DATA_DIR / "screen_captures"
try:
    _SCREEN_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    _SCREEN_DIR = config.DATA_DIR / "screen_captures"


def _cleanup_old_captures(max_files: int = 50):
    """只保留最近 max_files 张截图，防止磁盘堆积。"""
    try:
        files = sorted(_SCREEN_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[max_files:]:
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def save_capture(image_data: bytes) -> Optional[str]:
    """
    保存截图到本地缓存（D 盘），返回相对路径。
    image_data: PNG 原始字节流
    """
    try:
        fname = f"{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        fp = _SCREEN_DIR / fname
        fp.write_bytes(image_data)
        _cleanup_old_captures()
        return str(fp)
    except Exception as e:
        print(f"[ScreenCapture] 保存失败: {e}", flush=True)
        return None


def save_base64_capture(b64_str: str) -> Optional[str]:
    """
    保存 base64 截图（前端 getDisplayMedia 转成 dataURL 传来）。
    b64_str: 不含 data:image/png;base64, 前缀的纯 base64 串
    """
    try:
        raw = base64.b64decode(b64_str)
        return save_capture(raw)
    except Exception as e:
        print(f"[ScreenCapture] Base64解码失败: {e}", flush=True)
        return None


def capture_local() -> Optional[bytes]:
    """
    后端本地截屏（mss 库，不依赖浏览器）。
    仅在 pip install mss 之后可用。
    """
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            img = sct.grab(sct.monitors[1])  # 主屏幕
            buf = io.BytesIO()
            # mss 返回的是 ScreenShot（BGRA 原始字节 + .size/.rgb 属性），
            # 不是 PIL Image —— 转 RGB 要用 Image.frombytes，直接 .convert 会 AttributeError
            img_rgb = Image.frombytes("RGB", img.size, img.rgb)
            img_rgb.save(buf, format="PNG")
            return buf.getvalue()
    except ImportError:
        print("[ScreenCapture] mss 未安装，请 pip install mss", flush=True)
        return None
    except Exception as e:
        print(f"[ScreenCapture] 本地截屏失败: {e}", flush=True)
        return None
