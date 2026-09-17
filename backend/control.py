# -*- coding: utf-8 -*-
"""
AI 控制电脑模块 v1.0（Windows 版，结合优化）
复用「AI控制电脑方案」的解析器/意图引擎设计，适配 Windows：
  - 解析器：从 AI 回复解析 [ACTION]{json}[/ACTION] 标记
  - 意图引擎：按情绪/场景主动建议动作（真人感：不无脑执行、有冷却、有概率）
  - 执行器：Windows 实现（打开应用/网址、音量、媒体、通知、看屏幕、打字）
安全：动作白名单，只允许安全动作；pyautogui FAILSAFE 保留（鼠标移左上角立即停止）。
"""
import asyncio
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import config as cfg

logger = logging.getLogger(__name__)

# ── 动作白名单（只允许这些安全动作）──
ALLOWED_TYPES = {"open_app", "close_app", "url", "volume", "volume_set", "media", "notify",
                 "screen_info", "type_text", "clipboard_read", "clipboard_write", "window",
                 "lock", "mouse_click", "shutdown_timer", "shutdown_cancel", "screenshot_look"}

# 应用名 → 进程名（关闭用；一个应用可能有多个候选进程名）
CLOSE_PROC_MAP = {
    "微信": ["WeChat.exe", "Weixin.exe"], "weixin": ["Weixin.exe", "WeChat.exe"],
    "wechat": ["WeChat.exe", "Weixin.exe"],
    "qq": ["QQ.exe"],
    "网易云": ["cloudmusic.exe"], "网易云音乐": ["cloudmusic.exe"], "netease": ["cloudmusic.exe"],
    "qq音乐": ["QQMusic.exe"],
    "抖音": ["Douyin.exe", "douyin.exe"],
    "b站": ["bilibili.exe"], "哔哩哔哩": ["bilibili.exe"], "bilibili": ["bilibili.exe"],
    "游戏": ["steam.exe"], "steam": ["steam.exe"],
    "记事本": ["notepad.exe"], "notepad": ["notepad.exe"],
    "计算器": ["Calculator.exe", "calc.exe"],
    "画图": ["mspaint.exe"], "mspaint": ["mspaint.exe"],
    "chrome": ["chrome.exe"], "谷歌浏览器": ["chrome.exe"],
    "edge": ["msedge.exe"], "edge浏览器": ["msedge.exe"],
    "音乐": ["QishuiMusic.exe", "ZuneMusic.exe"],
    "汽水音乐": ["QishuiMusic.exe"],
}

# ── 常见应用名 → Windows 启动命令 ──
APP_MAP = {
    "音乐": "microsoftmusic:", "music": "microsoftmusic:",
    "网易云": "netease-cloud-music:", "网易云音乐": "netease-cloud-music:",
    "记事本": "notepad", "notepad": "notepad",
    "计算器": "calc", "calc": "calc",
    "画图": "mspaint", "mspaint": "mspaint",
    "浏览器": "https://www.baidu.com", "浏览器打开百度": "https://www.baidu.com",
    "抖音": "https://www.douyin.com", "douyin": "https://www.douyin.com",
    "游戏": "steam:", "steam": "steam:",
    "微信": "https://wx.qq.com", "weixin": "https://wx.qq.com", "wechat": "https://wx.qq.com",
    "qq": "mqq:", "qq音乐": "qqmusic:",
    "哔哩哔哩": "https://www.bilibili.com", "b站": "https://www.bilibili.com",
    "bilibili": "https://www.bilibili.com",
    "chrome": "chrome:", "谷歌浏览器": "chrome:",
    "edge": "msedge:", "edge浏览器": "msedge:",
    "文件资源管理器": "explorer:", "资源管理器": "explorer:", "我的电脑": "explorer:",
    "任务管理器": "taskmgr", "taskmgr": "taskmgr",
    "设置": "ms-settings:", "系统设置": "ms-settings:",
    "cmd": "cmd", "命令行": "cmd", "终端": "cmd",
    "控制面板": "control",
}


# ══════════════════════════════════════════════
# 1. 解析器
# ══════════════════════════════════════════════
def parse_actions(text: str) -> Tuple[List[Dict], str]:
    """解析 [ACTION]{json}[/ACTION] 标记，返回 (actions, clean_text)。JSON 失败静默跳过。"""
    if not text:
        return [], text
    pattern = re.compile(r"\[ACTION\](.*?)\[/ACTION\]", re.DOTALL)
    actions = []
    for m in pattern.finditer(text):
        try:
            a = json.loads(m.group(1).strip())
            if isinstance(a, dict) and a.get("type") in ALLOWED_TYPES:
                actions.append(a)
        except Exception:
            pass  # JSON 失败静默跳过
    clean = pattern.sub("", text).strip()
    return actions, clean


# ══════════════════════════════════════════════
# 2. 意图引擎（主动建议动作，真人感：有概率 + 冷却）
# ══════════════════════════════════════════════
# 明确请求关键词 → 动作
EXPLICIT_RULES = [
    (lambda m: any(k in m for k in ("打开百度", "百度", "搜索一下", "帮我搜")),
     {"type": "url", "url": "https://www.baidu.com", "_hint": "帮你打开百度"},
     0.95),
    (lambda m: any(k in m for k in ("放歌", "放首歌", "来首歌", "听歌", "放音乐", "音乐")),
     {"type": "open_app", "app": "音乐", "_hint": "帮你开音乐"},
     0.9),
    (lambda m: any(k in m for k in ("声音大", "大声", "音量大")),
     {"type": "volume", "delta": 10, "_hint": "音量调大一点"},
     0.9),
    (lambda m: any(k in m for k in ("声音小", "小声", "音量小", "安静")),
     {"type": "volume", "delta": -10, "_hint": "音量调小一点"},
     0.9),
    (lambda m: any(k in m for k in ("刷抖音", "抖音", "看视频", "短视频")),
     {"type": "url", "url": "https://www.douyin.com", "_hint": "帮你打开抖音"},
     0.9),
]

# 情绪/场景 → 动作建议（概率触发，不无脑）
EMOTION_RULES = [
    (lambda ctx, m: ctx.get("emotion") in ("sad", "tired") and ctx.get("period") in ("晚上", "深夜"),
     {"type": "open_app", "app": "音乐", "_hint": "累了吧，我帮你开点音乐放松"},
     0.35),
    (lambda ctx, m: ctx.get("emotion") in ("loving", "longing", "tender") and "想你" in m,
     {"type": "open_app", "app": "音乐", "_hint": "突然想陪你听首歌"},
     0.3),
    (lambda ctx, m: ctx.get("emotion") == "excited" and any(k in m for k in ("无聊", "没意思")),
     {"type": "url", "url": "https://www.douyin.com", "_hint": "陪你刷会儿视频吧"},
     0.3),
    # ★ 感知场景扩展（结合屏幕理解 summary/topic_hint）
    (lambda ctx, m: ctx.get("period") == "深夜" and any(k in m for k in ("视频", "bilibili", "抖音", "直播", "B站")),
     {"type": "media", "cmd": "toggle", "_hint": "都这个点了，要不要先暂停视频去休息？"},
     0.45),
    (lambda ctx, m: any(k in m for k in ("游戏", "打游戏", "steam")),
     {"type": "volume", "delta": -10, "_hint": "帮你把音量调小一点，别吵着耳朵"},
     0.2),
    (lambda ctx, m: ctx.get("period") in ("深夜",) and any(k in m for k in ("游戏",)),
     {"type": "volume", "delta": -10, "_hint": "这么晚还在打呀，音量帮你调小一点，早点休息哦"},
     0.4),
]

_cooldown: Dict[str, datetime] = {}


def suggest_action(ctx: Dict, message: str) -> Optional[Dict]:
    """按「明确请求 > 情绪场景」主动建议动作，返回 action dict 或 None。"""
    message = message or ""
    # 1. 明确请求（高概率）
    for cond, action, prob in EXPLICIT_RULES:
        if cond(message) and random.random() < prob:
            return _with_cooldown(action)
    # 2. 情绪/场景（低概率 + 冷却，不无脑）
    for cond, action, prob in EMOTION_RULES:
        if cond(ctx, message) and random.random() < prob:
            return _with_cooldown(action)
    return None


def _with_cooldown(action: Dict) -> Optional[Dict]:
    key = action.get("type", "") + ":" + str(action.get("app") or action.get("url") or "")
    now = datetime.now()
    if key in _cooldown and (now - _cooldown[key]) < timedelta(minutes=10):
        return None  # 10 分钟内不重复建议同一动作
    _cooldown[key] = now
    return action


# ══════════════════════════════════════════════
# 3. Windows 执行器
# ══════════════════════════════════════════════
def _pyautogui():
    try:
        import pyautogui
        pyautogui.FAILSAFE = True   # 鼠标移到左上角立即停止
        pyautogui.PAUSE = 0.05
        return pyautogui
    except Exception:
        return None


def _notify(title: str, body: str):
    """Windows 通知（用 ctypes 消息框，无需额外依赖，可靠）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, body or "", title or "AI 伴侣", 0x40)
        return True
    except Exception:
        return False


def _screen_info() -> str:
    """获取当前前台窗口标题。"""
    try:
        import pygetwindow as gw
        win = gw.getActiveWindow()
        return win.title if win else ""
    except Exception:
        return ""


# ── 开始菜单快捷方式启动（应用打开的正确姿势）──
# 之前 APP_MAP 用 wechat:/netease-cloud-music: 这类协议或直接 URL，
# 协议没注册/没装对应 UWP 客户端时 start 会落到浏览器开网页（「打开的怎么是网页」）。
# 现在优先从开始菜单 .lnk 启动真实程序，协议/URL 只做兜底。
_START_MENU_DIRS = []
for _d in (os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                        "Microsoft", "Windows", "Start Menu", "Programs"),
           os.path.join(os.environ.get("APPDATA", ""),
                        "Microsoft", "Windows", "Start Menu", "Programs")):
    if _d and os.path.isdir(_d):
        _START_MENU_DIRS.append(_d)

APP_LNK_KEYWORDS = {
    "微信": ["微信", "weixin", "wechat"], "weixin": ["微信", "weixin", "wechat"],
    "wechat": ["微信", "weixin", "wechat"],
    "qq": ["qq"], "qq音乐": ["qqmusic", "qq音乐"],
    "网易云": ["cloudmusic", "网易云", "netease"], "网易云音乐": ["cloudmusic", "网易云", "netease"],
    "netease": ["cloudmusic", "网易云", "netease"],
    "b站": ["bilibili", "哔哩"], "哔哩哔哩": ["bilibili", "哔哩"], "bilibili": ["bilibili", "哔哩"],
    "抖音": ["抖音", "douyin"], "douyin": ["抖音", "douyin"],
    "游戏": ["steam"], "steam": ["steam"],
    "chrome": ["chrome"], "谷歌浏览器": ["chrome"],
    "音乐": ["groove", "zune", "音乐"],
    "浏览器": ["chrome", "edge", "浏览器"],
}

_lnk_cache = {"items": None, "ts": 0.0}


def _find_lnk(keywords):
    """开始菜单 .lnk 模糊匹配（60s 缓存）；多个命中取名字最短的（最像目标）。"""
    import time as _t
    if not _START_MENU_DIRS:
        return None
    now = _t.time()
    if _lnk_cache["items"] is None or now - _lnk_cache["ts"] > 60:
        items = []
        for d in _START_MENU_DIRS:
            try:
                from pathlib import Path as _P
                for p in _P(d).rglob("*.lnk"):
                    items.append((p.name.lower(), str(p)))
            except Exception:
                pass
        _lnk_cache["items"] = items
        _lnk_cache["ts"] = now
    items = _lnk_cache["items"] or []
    for kw in keywords:
        kw = kw.lower()
        hits = [(nm, path) for nm, path in items if kw in nm]
        if hits:
            hits.sort(key=lambda x: len(x[0]))   # 名字最短 = 最精确匹配
            return hits[0][1]
    return None


_startapps_cache = {"items": None, "ts": 0.0}


def _find_startapp(keywords):
    """Get-StartApps 兜底（UWP + 桌面 AppID，覆盖开始菜单没有 lnk 的场景）。
    返回 AUMID/AppID（用 os.startfile('shell:AppsFolder\\<id>') 启动）或 None。"""
    import subprocess as _sp
    if not keywords:
        return None
    now = time.time()
    if _startapps_cache["items"] is None or now - _startapps_cache["ts"] > 120:
        try:
            out = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
                capture_output=True, timeout=15).stdout.decode("utf-8", "replace")
            items = []
            for line in (out or "").splitlines():
                if "|" not in line:
                    continue
                nm, _, aid = line.partition("|")
                nm, aid = nm.strip().lower(), aid.strip()
                if nm and aid:
                    items.append((nm, aid))
            _startapps_cache["items"] = items
            _startapps_cache["ts"] = now
        except Exception:
            return None
    items = _startapps_cache["items"] or []
    for kw in keywords:
        kw = kw.lower()
        for nm, aid in items:
            if kw in nm:
                return aid
    return None


async def execute(action: Dict, session_id: str = "default", character_id: str = "default",
                  confirmed: bool = False) -> Dict:
    """执行单个 action（Windows），返回 {ok, ...}。动作类型白名单校验。

    ★ 2026-09-15 第 3 步（用户拍板 B 级自主）：
      · **分级闸门**：高风险动作（跑命令/键鼠/改文件）在未 confirmed 时**不执行**，
        返回 {"ok":False,"need_confirm":True} 交给上层"先问再做"流程 —— 规则落到后端，
        不只是写在她的提示词里；低风险（开应用/网址/调音量/通知）直通。
      · **动作日志**：每次执行（含被拦下的）落一条 action_log.jsonl（见 ctrl_guard）。
        采集器的「编造动作」维度拿它当执行证据：她说做了但日志里没有 = 编造。
    """
    atype = action.get("type", "")
    if atype not in ALLOWED_TYPES:
        return {"ok": False, "error": f"不允许的动作类型: {atype}"}
    _cg = None
    try:
        from . import ctrl_guard as _cgmod
        _cg = _cgmod
        if confirmed:
            action = dict(action or {})
            action["_confirmed"] = True
        if _cg.needs_confirm(action):
            _cg.log_action(action, {"ok": False, "error": "high_risk_requires_confirmation"},
                           session_id=session_id, character_id=character_id,
                           initiator="ai", confirmed=False)
            print("[CtrlGuard] 高风险动作拦下等确认: %s" % atype, flush=True)
            return {"ok": False, "need_confirm": True,
                    "reason": "high_risk_requires_confirmation", "action": action}
    except Exception as _ce:
        print("[CtrlGuard] 分级闸门异常(放行): %s: %s" % (type(_ce).__name__, _ce), flush=True)
    try:
        result = await asyncio.to_thread(_execute_sync, action)
        # ★ 记忆联动：用户触发的动作记录为偏好（打开应用/网址）
        if result.get("ok"):
            _record_preference(session_id, character_id, action)
        if _cg:
            _cg.log_action(action, result, session_id=session_id, character_id=character_id,
                           initiator=("user" if confirmed else "ai"), confirmed=confirmed)
        return result
    except Exception as e:
        if _cg:
            _cg.log_action(action, {"ok": False, "error": str(e)},
                           session_id=session_id, character_id=character_id, initiator="ai")
        return {"ok": False, "error": str(e)}


def _record_preference(session_id: str, character_id: str, action: Dict):
    """执行动作后，把「用户偏好」写入长期记忆（联动记忆系统）。"""
    try:
        from . import memory_manager
        atype = action.get("type")
        content = None
        if atype == "open_app":
            app = str(action.get("app") or "").strip()
            if app:
                content = f"用户喜欢/常用应用「{app}」"
        elif atype == "url":
            url = str(action.get("url") or "").strip()
            if url:
                content = f"用户常访问的网站：{url}"
        if content:
            memory_manager.dedupe_insert(content, session_id=session_id, character_id=character_id)
    except Exception as e:
        logger.warning(f"[Control] 记忆联动失败: {e}")


def _execute_sync(action: Dict) -> Dict:
    atype = action.get("type")
    if atype in ("open_app",):
        app = str(action.get("app") or "").strip()
        if not app:
            return {"ok": False, "error": "缺少应用名"}
        # 1) 系统程序直启（cmd/App Paths 都能解析）
        _direct = {"记事本": "notepad", "notepad": "notepad", "计算器": "calc", "calc": "calc",
                   "画图": "mspaint", "mspaint": "mspaint", "任务管理器": "taskmgr", "taskmgr": "taskmgr",
                   "cmd": "cmd", "命令行": "cmd", "终端": "cmd", "控制面板": "control",
                   "文件资源管理器": "explorer", "资源管理器": "explorer", "我的电脑": "explorer",
                   "edge": "msedge", "edge浏览器": "msedge"}
        if app.lower() in _direct:
            subprocess.Popen(["cmd", "/c", "start", "", _direct[app.lower()]], shell=False)
            return {"ok": True, "action": f"打开 {app}"}
        # 2) ★ 开始菜单快捷方式（真实程序：QQ/网易云/B站/Steam/抖音…）
        lnk = _find_lnk(APP_LNK_KEYWORDS.get(app.lower()) or [app])
        if lnk:
            try:
                os.startfile(lnk)
                return {"ok": True, "action": f"打开 {app}"}
            except Exception as e:
                logger.warning(f"[Control] lnk 启动失败({lnk}): {e}")
        # 2.5) ★ Get-StartApps AppID 兜底（UWP/桌面应用，lnk 缺失时也能开真程序）
        aid = _find_startapp(APP_LNK_KEYWORDS.get(app.lower()) or [app])
        if aid:
            try:
                os.startfile("shell:AppsFolder\\" + aid)
                return {"ok": True, "action": f"打开 {app}"}
            except Exception as e:
                logger.warning(f"[Control] AUMID 启动失败({aid}): {e}")
        # 3) 协议 / URL 兜底（UWP 协议、网页版）
        target = APP_MAP.get(app.lower()) or APP_MAP.get(app)
        if not target:
            return {"ok": False, "error": "没找到这个应用（试试说完整名字，如「打开微信」）"}
        if target.startswith("http"):
            webbrowser.open(target)
        else:
            subprocess.Popen(["cmd", "/c", "start", "", target], shell=False)
        return {"ok": True, "action": f"打开 {app}"}

    if atype == "url":
        url = str(action.get("url") or "").strip()
        if len(url) > 2048 or not re.match(r"^https?://[^\s]+$", url, re.I):
            return {"ok": False, "error": "仅支持 http/https 网址"}
        webbrowser.open(url)
        return {"ok": True, "action": f"打开 {url}"}

    if atype == "volume":
        pg = _pyautogui()
        if not pg:
            return {"ok": False, "error": "pyautogui 不可用"}
        delta = int(action.get("delta") or 0)
        key = "volumeup" if delta >= 0 else "volumedown"
        for _ in range(min(10, abs(delta) // 10 + 1)):
            pg.press(key)
        return {"ok": True, "action": f"音量{'+' if delta >= 0 else '-'}{abs(delta)}"}

    if atype == "media":
        pg = _pyautogui()
        if not pg:
            return {"ok": False, "error": "pyautogui 不可用"}
        cmd = str(action.get("cmd") or "toggle")
        media_key = {"play": "playpause", "pause": "playpause", "next": "nexttrack",
                     "prev": "prevtrack", "toggle": "playpause"}.get(cmd, "playpause")
        pg.press(media_key)
        return {"ok": True, "action": f"媒体 {cmd}"}

    if atype == "notify":
        title = str(action.get("title") or "AI 伴侣")
        body = str(action.get("body") or "")
        ok = _notify(title, body)
        return {"ok": ok, "action": "系统通知"}

    if atype == "screen_info":
        info = _screen_info()
        return {"ok": True, "info": info}

    if atype == "type_text":
        pg = _pyautogui()
        if not pg:
            return {"ok": False, "error": "pyautogui 不可用"}
        text = str(action.get("text") or "")
        if len(text) > 2000:
            return {"ok": False, "error": "输入文本过长"}
        pg.typewrite(text)
        return {"ok": True, "action": f"输入 {text[:20]}"}

    # ── 扩展动作（v2）──
    if atype == "volume_set":
        # 精确音量（pycaw，Windows 核心音频 API）
        try:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            val = max(0, min(100, int(action.get("value") or 50)))
            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(interface, POINTER(IAudioEndpointVolume))
            vol.SetMasterVolumeLevelScalar(val / 100.0, None)
            return {"ok": True, "action": f"音量设为 {val}"}
        except Exception as e:
            return {"ok": False, "error": f"精确音量不可用: {e}"}

    if atype == "clipboard_read":
        try:
            import ctypes
            CF_UNICODETEXT = 13
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            if not user32.OpenClipboard(0):
                return {"ok": False, "error": "剪贴板被占用"}
            try:
                if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                    return {"ok": False, "error": "剪贴板里没有文字"}
                h = user32.GetClipboardData(CF_UNICODETEXT)
                p = kernel32.GlobalLock(h)
                text = ctypes.c_wchar_p(p).value or ""
                kernel32.GlobalUnlock(h)
                text = (text or "").strip()
                return {"ok": True, "info": text[:500], "action": "读剪贴板"}
            finally:
                user32.CloseClipboard()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if atype == "clipboard_write":
        text = str(action.get("text") or "")[:5000]
        try:
            import ctypes
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            data = text.encode("utf-16-le") + b"\x00\x00"
            if not user32.OpenClipboard(0):
                return {"ok": False, "error": "剪贴板被占用"}
            try:
                user32.EmptyClipboard()
                h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                p = kernel32.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(CF_UNICODETEXT, h)
                return {"ok": True, "action": f"已放入剪贴板：{text[:20]}"}
            finally:
                user32.CloseClipboard()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    if atype == "window":
        pg = _pyautogui()
        if not pg:
            return {"ok": False, "error": "pyautogui 不可用"}
        cmd = str(action.get("cmd") or "")
        if cmd == "show_desktop":
            pg.hotkey("win", "d")
            return {"ok": True, "action": "显示桌面"}
        if cmd == "switch":
            pg.keyDown("alt")
            pg.press("tab")
            pg.keyUp("alt")
            return {"ok": True, "action": "切换窗口"}
        return {"ok": False, "error": f"未知窗口操作: {cmd}"}

    if atype == "lock":
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return {"ok": True, "action": "已锁屏"}

    if atype == "mouse_click":
        pg = _pyautogui()
        if not pg:
            return {"ok": False, "error": "pyautogui 不可用"}
        pos = action.get("pos") or {}
        x, y = pos.get("x"), pos.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            pg.click(int(x), int(y))
            return {"ok": True, "action": f"点击 ({int(x)},{int(y)})"}
        w, h = pg.size()
        pg.click(w // 2, h // 2)
        return {"ok": True, "action": "点击屏幕中央"}

    if atype == "shutdown_timer":
        mode = str(action.get("mode") or "shutdown")
        minutes = max(1, min(120, int(action.get("minutes") or 10)))
        sub = f"shutdown /{'r' if mode == 'reboot' else 's'} /t {minutes * 60}"
        subprocess.Popen(["cmd", "/c", sub], shell=False)
        return {"ok": True, "action": f"{minutes} 分钟后{'重启' if mode == 'reboot' else '关机'}（说「取消关机」可撤销）"}

    if atype == "shutdown_cancel":
        subprocess.Popen(["cmd", "/c", "shutdown /a"], shell=False)
        return {"ok": True, "action": "已取消关机计划"}

    if atype == "close_app":
        app = str(action.get("app") or "").strip()
        if not app:
            return {"ok": False, "error": "缺少应用名"}
        proc_names = CLOSE_PROC_MAP.get(app.lower()) or CLOSE_PROC_MAP.get(app) or [app + ".exe"]
        # 1) 找到正在运行的进程
        running = None
        for pn in proc_names:
            chk = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {pn}"],
                                 capture_output=True, text=True, timeout=10)
            if pn.lower() in (chk.stdout or "").lower():
                running = pn
                break
        if not running:
            return {"ok": False, "error": f"{app} 好像没在运行"}
        # 2) 先温柔关闭（WM_CLOSE，给保存数据的机会）
        subprocess.run(["taskkill", "/IM", running], capture_output=True, timeout=10)
        import time as _t
        _t.sleep(1.2)
        # 3) 还在 → 强杀
        chk = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {running}"],
                             capture_output=True, text=True, timeout=10)
        if running.lower() in (chk.stdout or "").lower():
            subprocess.run(["taskkill", "/IM", running, "/F"], capture_output=True, timeout=10)
        return {"ok": True, "action": f"已关闭 {app}"}

    return {"ok": False, "error": f"未知动作: {atype}"}


async def execute_actions(actions: List[Dict]) -> List[Dict]:
    """顺序执行多个 action。"""
    results = []
    for a in actions:
        results.append(await execute(a))
        await asyncio.sleep(0.3)
    return results


# ══════════════════════════════════════════════
# 4. 语音直控：用户文本 → 动作（按住 F9 说话控制电脑用）
# ══════════════════════════════════════════════
def parse_direct_command(text: str) -> Optional[Dict]:
    """口语 → 动作 dict（规则直配，快）。未命中返回 None（调用方可走 LLM 兜底）。"""
    t = str(text or "").strip()
    if not t:
        return None
    low = t.lower()

    if any(k in t for k in ("打开", "开一下", "启动", "运行", "开个")):
        for name, target in APP_MAP.items():
            if name in t or name.lower() in low:
                return {"type": "open_app", "app": name, "_hint": f"帮你打开{name}"}
    m = re.search(r"(?:打开|访问|进)\s*(https?://\S+)", t)
    if m:
        return {"type": "url", "url": m.group(1)}
    if any(k in t for k in ("声音大", "大声点", "音量大", "调大音量", "大点声", "声音调大")):
        return {"type": "volume", "delta": 15, "_hint": "音量调大一点"}
    if any(k in t for k in ("声音小", "小声点", "音量小", "调小音量", "小点声", "声音调小")):
        return {"type": "volume", "delta": -15, "_hint": "音量调小一点"}
    if any(k in t for k in ("静音", "声音关掉", "把声音关了")):
        return {"type": "volume", "delta": -100, "_hint": "已静音"}
    # ★ 音量精确到数："音量调到 50" / "音量设为 30"
    m = re.search(r"音量.{0,4}?(\d{1,3})|(\d{1,3})\s*(?:的音量|音量)", t)
    if m:
        val = int(m.group(1) or m.group(2))
        if 0 <= val <= 100:
            return {"type": "volume_set", "value": val, "_hint": f"音量调到 {val}"}
    # ★ 剪贴板
    if any(k in t for k in ("读一下我复制", "我剪贴板", "剪贴板里", "读剪贴板", "复制的内容", "我复制了什么")):
        return {"type": "clipboard_read", "_hint": "读一下你剪贴板里的内容"}
    m = re.search(r"(?:放进|放到|复制(?:到)?|存到)(?:我的)?剪贴板\s*[:：,，]?\s*(\S.{1,200})", t)
    if m:
        return {"type": "clipboard_write", "text": m.group(1).strip(), "_hint": "帮你放进剪贴板"}
    # ★ 窗口管理
    if any(k in t for k in ("显示桌面", "回桌面", "最小化所有", "回到桌面")):
        return {"type": "window", "cmd": "show_desktop", "_hint": "显示桌面"}
    if any(k in t for k in ("切换窗口", "切个窗口", "换窗口", "切窗口")):
        return {"type": "window", "cmd": "switch", "_hint": "帮你切换窗口"}
    # ★ 锁屏
    if any(k in t for k in ("锁屏", "锁定电脑", "锁一下屏", "把电脑锁了")):
        return {"type": "lock", "_hint": "帮你锁屏"}
    # ★ 鼠标点击
    if re.search(r"(?:帮我)?点(?:一下|击)(?:屏幕)?(中央|中间)?", t) and "点赞" not in t:
        return {"type": "mouse_click", "_hint": "帮你点一下屏幕中央"}
    # ★ 关机/重启（高危：voice_control 接口层会强制走「提议 → 语音确认」流程）
    if any(k in t for k in ("取消关机", "不关机了", "别关机")):
        return {"type": "shutdown_cancel", "_hint": "已取消关机计划"}
    m = re.search(r"(\d{1,3})\s*分钟(?:之?后)?(?:帮我|给我)?(关机|重启)", t)
    if m:
        return {"type": "shutdown_timer", "mode": "shutdown" if "关机" in m.group(2) else "reboot",
                "minutes": max(1, min(120, int(m.group(1)))),
                "_hint": f"{m.group(1)} 分钟后{'重启' if '重启' in m.group(2) else '关机'}"}
    if re.search(r"(?:帮我)?(?:定时)?(关机|重启)(?:电脑)?", t):
        mode = "reboot" if "重启" in t else "shutdown"
        return {"type": "shutdown_timer", "mode": mode, "minutes": 1,
                "_hint": f"1 分钟后{'重启' if mode == 'reboot' else '关机'}"}
    # ★ 看屏幕升级：真截图 + 视觉理解（接口层特殊处理）
    if any(k in t for k in ("看看我的屏幕", "我屏幕上是什么", "看屏幕", "看到什么", "我在干嘛",
                            "看一下我的屏幕", "看看我屏幕", "我在看什么")):
        return {"type": "screenshot_look", "_hint": "看看你的屏幕"}
    # ★ 关闭应用："关闭微信 / 把网易云关了 / 退出记事本 / 关掉抖音"
    #   （放在音量/关机之后：「把声音关掉」已先命中音量静音，「关机」已先命中关机）
    m = (re.search(r"(?:关闭|关掉|退出|关了)\s*(.{1,12}?)(?:程序|软件|应用)?(?:吧|呀|哈)?[。！]?$", t)
         or re.search(r"把\s*(.{1,12}?)\s*(?:关了|关掉|关闭)(?:吧|呀|哈)?[。！]?$", t))
    if m:
        _name = m.group(1).strip()
        # 排除非应用词（声音/音乐播放这类已在前面命中，到这里的多半是应用名）
        if _name and _name not in ("它", "这个", "那", "了"):
            for known in list(APP_MAP.keys()) + list(APP_LNK_KEYWORDS.keys()):
                if known in _name or _name in known:
                    return {"type": "close_app", "app": known, "_hint": f"帮你关闭{known}"}
            return {"type": "close_app", "app": _name, "_hint": f"帮你关闭{_name}"}
    if any(k in t for k in ("下一首", "换一首", "切歌")):
        return {"type": "media", "cmd": "next", "_hint": "切到下一首"}
    if any(k in t for k in ("上一首",)):
        return {"type": "media", "cmd": "prev", "_hint": "切到上一首"}
    if any(k in t for k in ("暂停", "停一下音乐", "别播了", "停止播放")):
        return {"type": "media", "cmd": "toggle", "_hint": "播放/暂停"}
    m = re.search(r"(?:帮我输入|输入|打字|帮我打)\s*[:：,，]?\s*(\S.{1,80})", t)
    if m:
        return {"type": "type_text", "text": m.group(1).strip(), "_hint": "帮你打字"}
    if any(k in t for k in ("看屏幕", "看我的屏幕", "屏幕上是什么", "我在干嘛", "看到什么")):
        return {"type": "screen_info", "_hint": "看一眼你的屏幕"}
    return None


_PC_TARGET_WORDS = ("在电脑上", "用电脑", "电脑上帮我", "帮我电脑", "电脑帮我", "在电脑里", "在电脑")
_PHONE_TARGET_WORDS = ("在手机上", "用手机", "手机上帮我", "帮我手机", "手机帮我", "在手机里", "手机上")


def parse_direct_command_with_target(text: str):
    """QQ 语音/文字控制入口：区分控制目标是电脑还是手机。
    返回 (target, action)：
      'pc'    → action 为电脑动作（QQ 直接执行，不询问）
      'phone' → 交给 iOS 快捷指令链路（onebot 0.2 的 parse_ios_command）
      ''      → 非控制指令（走正常聊天）
    ★ 默认目标 = 电脑（QQ 遥控场景）；说「在手机上/用手机」显式指定手机。
    """
    t = str(text or "").strip()
    if not t or len(t) > 80:
        return "", None
    # 显式手机 → 手机链路
    if any(w in t for w in _PHONE_TARGET_WORDS) or ("手机" in t and "电脑" not in t):
        return "phone", None
    # 显式电脑 → 剥掉目标词再解析
    for w in _PC_TARGET_WORDS:
        if w in t:
            a = parse_direct_command(t.replace(w, ""))
            return ("pc", a) if a else ("pc", None)
    # 无目标词：规则命中 → 默认电脑
    a = parse_direct_command(t)
    if a:
        return "pc", a
    return "", None


async def parse_direct_command_llm(text: str, key: str, model: str) -> Optional[Dict]:
    """规则未命中 → LLM 把口语转成动作 JSON（限 4s，失败返回 None）。"""
    if not key:
        return None
    try:
        from .deepseek_api import chat_once
        raw = await asyncio.wait_for(chat_once(model, [
            {"role": "system", "content":
             "把用户的话转换成电脑控制动作 JSON。可用类型与取值：\n"
             '{"type":"open_app","app":"音乐|网易云|微信|QQ|哔哩哔哩|记事本|计算器|画图|游戏|浏览器|chrome|edge|文件资源管理器|任务管理器|设置|cmd|控制面板"}\n'
             '{"type":"url","url":"完整http网址"}\n'
             '{"type":"volume","delta":正负数字}\n'
             '{"type":"volume_set","value":0到100}\n'
             '{"type":"media","cmd":"toggle|next|prev"}\n'
             '{"type":"type_text","text":"要输入的原话"}\n'
             '{"type":"clipboard_read"} 或 {"type":"clipboard_write","text":"内容"}\n'
             '{"type":"window","cmd":"show_desktop|switch"}\n'
             '{"type":"lock"}\n'
             '{"type":"mouse_click"}\n'
             '{"type":"shutdown_timer","mode":"shutdown|reboot","minutes":数字}\n'
             '{"type":"screenshot_look"}\n'
             "无法转换成上述动作就只输出 NONE。只输出一行 JSON 或 NONE，不要解释。"},
            {"role": "user", "content": str(text or "")[:120]},
        ], key, temperature=0.1, max_tokens=80), timeout=4.0)
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            a = json.loads(m.group())
            if isinstance(a, dict) and a.get("type") in ALLOWED_TYPES:
                return a
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════
# 5. 动作提议（感知系统 → 模型建议 → 先询问 → 用户按 F9 说「可以」才执行）
# ══════════════════════════════════════════════
_pending = {"action": None, "reply": "", "ts": 0.0}
_PROPOSAL_TTL = 75   # 提议 75 秒内有效，超时自动作废

# ══════════════════════════════════════════════════════════════════════════
# ★ 2026-09-16 安全修复：确认闸门 = 模型主判 → 精确短语兜底 → 拿不准不执行
#
#   缺陷（控制器核实）：旧 `_CONFIRM_WORDS` 里带单字（「好」「嗯」「行」「要」），
#   `match_confirmation` 又是**子串**判定（`any(w in t for w in …)`），
#   而 main.py 的语音端点拿到 'confirm' 就直接 `execute()`（真动作、不可逆）。
#   ⇒ 有待确认提议时说「好累啊」「好烦」「我要睡了」含「好」/「要」就被当成确认并真执行。
#
#   裁决（用户明确否决"光凭关键词判断"）：
#     ① 有 pending 提议时**问模型**（`confirm_verdict_llm`，口径同 backend/ai_promise.py 的
#        小严格-JSON 提取器 + 宽容解析）；模型层不可用（无 Key / 异常 / 解析不出）
#        → 降级 ②；
#     ② 兜底只认**整句相等**（`match_confirmation`，仅剥句末标点/空白），
#        不认子串、不认单字；
#     ③ 拿不准（ambiguous）⇒ **不执行**：提议保持 pending 等 TTL 自然过期，
#        既不静默执行、也不静默拒绝；
#     ④ QQ 路径（onebot.py）走同一道闸门（说「关机」不再直接排关机）。
# ══════════════════════════════════════════════════════════════════════════
# 兜底用的**整句**短语集（`match_confirmation` 里只做 `整句 ==`，绝不 `in`）。
# ★ 单字一律不进（「好」「嗯」「行」「要」「不」「别」只作为**独立整句**在下面显式列出，
#   这样「好累啊」「要不要一起看电影」这类闲聊永远命中不了）。
_CONFIRM_PHRASES = (
    "可以", "可以呀", "可以啊", "好的", "好呀", "好吧", "好", "行", "行吧",
    "嗯", "嗯嗯", "做吧", "执行", "执行吧", "确认", "确认吧", "去吧",
    "ok", "okay", "yes",
)
_REJECT_PHRASES = (
    "不", "不吧", "不要", "不用", "不用了", "别", "别了", "算了", "取消",
    "取消吧", "no", "stop",
)


def action_summary(action: Dict) -> str:
    """给模型/日志看的动作摘要（人类话术 + 结构化类型）。

    模型要判「用户是在答应她做这件事吗」，必须知道**提议本身是什么**：
    光有用户那两个字（「可以」）没有任何上下文，模型只能瞎猜。
    """
    try:
        a = action if isinstance(action, dict) else {}
        parts = []
        hint = str(a.get("_hint") or "").strip()
        if hint:
            parts.append(hint)
        parts.append(str(a.get("type") or "动作"))
        for k in ("app", "url", "cmd", "mode", "value", "delta", "minutes"):
            v = a.get(k)
            if v not in (None, ""):
                parts.append("%s=%s" % (k, v))
        return "；".join(parts)
    except Exception:
        return "动作"


def _normalize_confirm_text(text: str) -> str:
    """确认判定的归一化：剥掉标点，**不做任何包含匹配，也不合并内部空格**。

    「好的。」「OK！」「嗯嗯~」→ 归一到短语本身；「好 的」不会变成「好的」
    （口语里带空格/断字是听不清的信号，宁可判拿不准也不能猜）。
    """
    s = str(text or "").strip()
    for ch in "，,。.!！?？~～、；;：:“”\"'‘’（）()【】[]…—－":
        s = s.replace(ch, "")
    try:
        return s.strip().casefold()
    except AttributeError:
        return s.strip().lower()


def _phrase_set(words) -> frozenset:
    out = set()
    for w in words:
        _n = _normalize_confirm_text(w)
        if _n:
            out.add(_n)
    return frozenset(out)


_CONFIRM_EXACT = _phrase_set(_CONFIRM_PHRASES)
_REJECT_EXACT = _phrase_set(_REJECT_PHRASES)

# 模型主判的系统提示（口径照 backend/ai_promise.py 的提取器：小、严格、只要一行 JSON）
_CONFIRM_SYSTEM = (
    "你是电脑控制动作的「确认闸门」。她刚刚提议要帮用户操作电脑（下面给出提议内容），"
    "用户现在回了一句话，你要判断用户对**这个提议**的态度。\n"
    "\n"
    "只输出一行 JSON，不要解释、不要代码块：\n"
    '{"verdict":"confirm 或 reject 或 ambiguous 或 unrelated","confidence":0~1,"reason":"不超过15字"}\n'
    "\n"
    "判定口径（从严，宁可判拿不准）：\n"
    "- confirm   只有用户**明确表示同意做这个提议**才算：可以 / 好的 / 嗯嗯 / 执行吧 / 行 / OK / 做吧 / 确认\n"
    "- reject    用户**明确表示不要/取消这个提议**才算：不要 / 不用了 / 算了 / 别 / 取消 / 不 / no / stop\n"
    "- ambiguous 用户在**回应这个提议**、但说了别的、说不清、或只是在说自己的事：\n"
    "    · 闲聊、感慨、描述自己的状态：「好累啊」「好烦」「我要睡了」「好的我知道了」\n"
    "      —— 注意：「好累啊」里的「好」、「我要睡了」里的「要」、「还行吧」里的「行」"
    "都**不是**同意！\n"
    "    · 只是重复/追问提议、没说同意（「关机吗？」「你说什么」）\n"
    "    · 听不清、语气词、无意义（「嗯…」「那个」）\n"
    "    · 有同意也有别的意思、或有条件（「可以是可以，但是…」）\n"
    "- unrelated 用户**根本没在理这个提议**，在说一件完全不相干的事、或下了新指令：\n"
    "    · 「打开记事本」「音量调大」「今天天气怎么样」「帮我查下快递」\n"
    "    （用来判断「这句话还要不要留给这个提议」——说不准就判 ambiguous，别硬判 unrelated）\n"
    "\n"
    "★ 高风险的提议（关机/重启/锁屏/关闭应用/删除等）判 confirm 要更严："
    "稍有含糊就 ambiguous——判错的代价是真把用户电脑关了。\n"
)

# 前台等待上限（秒）：超过就认为模型层不可用 → 降级精确短语兜底，绝不卡住语音流程。
# 与 backend/ai_promise.py 的 _VERDICT_HARD_TIMEOUT 同档。
_CONFIRM_HARD_TIMEOUT = 6.0

# 认了标签但置信度低于这个值 → 也当拿不准（fail-closed）
_CONFIRM_MIN_CONFIDENCE = 0.5

_VERDICT_LABEL = {
    "confirm": "confirm", "yes": "confirm", "ok": "confirm", "okay": "confirm",
    "agree": "confirm", "approve": "confirm", "accept": "confirm", "true": "confirm",
    "确认": "confirm", "同意": "confirm", "可以": "confirm",
    "reject": "reject", "no": "reject", "deny": "reject", "cancel": "reject",
    "refuse": "reject", "false": "reject",
    "拒绝": "reject", "取消": "reject", "不要": "reject",
    "ambiguous": "ambiguous", "unknown": "ambiguous", "unsure": "ambiguous",
    "unclear": "ambiguous", "uncertain": "ambiguous", "none": "ambiguous",
    "拿不准": "ambiguous", "不确定": "ambiguous", "模糊": "ambiguous",
    "unrelated": "unrelated", "irrelevant": "unrelated", "other": "unrelated",
    "无关": "unrelated", "不相干": "unrelated",
}


def _parse_confirm_verdict(raw):
    """宽容解析模型输出 → 'confirm' / 'reject' / 'ambiguous' / None（解析不出）。

    先抽 JSON（同 backend/ai_promise.py `_parse` 的 `\\{[\\s\\S]*\\}` 口径），
    认不出结构化字段再退到文本关键词，最后按「先找 ambiguous 证据」的顺序兜底
    ——宁可判拿不准，也不能把一句含糊的话读成"同意"。
    """
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    obj = None
    try:
        m = re.search(r"\{[\s\S]*\}", txt)
        if m:
            data = json.loads(m.group(0))
            obj = data if isinstance(data, dict) else None
    except Exception:
        obj = None
    if obj is not None:
        conf = obj.get("confidence", obj.get("conf"))
        if isinstance(conf, str):
            try:
                conf = float(conf.strip().rstrip("%")) / (100.0 if conf.strip().endswith("%") else 1.0)
            except Exception:
                conf = None
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            # 标签明确但置信度低 → 拿不准（fail-closed）
            if float(conf) < _CONFIRM_MIN_CONFIDENCE:
                return "ambiguous"
        for key in ("verdict", "result", "decision", "answer", "action"):
            v = obj.get(key)
            if v is None:
                continue
            v = _normalize_confirm_text(v)
            if v in _VERDICT_LABEL:
                return _VERDICT_LABEL[v]
        if obj.get("confirm") is True:
            return "confirm"
        if obj.get("confirm") is False:
            return "ambiguous"
    low = txt.casefold()
    # ★ 顺序要紧：先找"拿不准/不相干"的证据——宁可判拿不准，也不能把一句含糊的话读成"同意"
    for w in ("unrelated", "irrelevant", "不相干", "无关"):
        if w in low:
            return "unrelated"
    for w in ("ambiguous", "拿不准", "不确定", "不清楚", "无法判断", "说不准"):
        if w in low:
            return "ambiguous"
    if re.search(r"(?<![a-z])confirm(?![a-z])", low) or "确认" in txt or "同意" in txt:
        return "confirm"
    if re.search(r"(?<![a-z])reject(?![a-z])", low) or "拒绝" in txt:
        return "reject"
    if re.search(r"(?<![a-z])yes(?![a-z])", low) or re.search(r"(?<![a-z])ok(?:ay)?(?![a-z])", low):
        return "confirm"
    if re.search(r"(?<![a-z])no(?![a-z])", low):
        return "reject"
    return None


async def confirm_verdict_llm(text: str, action: Dict) -> Dict:
    """问模型：用户这句话是对 pending 提议的**确认 / 拒绝 / 拿不准 / 不相干**？

    返回 {"verdict": 'confirm'|'reject'|'ambiguous'|'unrelated'|None,
          "source": 'model'|'model_unavailable', "raw": 模型原文}
    · None  = 模型层不可用（无 Key / 调用异常 / 返回解析不出）→ 调用方降级精确短语兜底。
    · 任一异常都**不抛**（确认链路在语音热路径上，绝不能因判定失败把流程弄崩）。
    """
    try:
        u = str(text or "").strip()
        if not u:
            return {"verdict": None, "source": "model_unavailable", "raw": ""}
        model = ""
        key = ""
        try:
            model = cfg.memory_extract_model()
            key = cfg.api_key_for_model(model)
        except Exception as e:
            print(f"[控制确认] 取模型/Key 失败 → 模型层不可用（降级精确短语）: {e}", flush=True)
            return {"verdict": None, "source": "model_unavailable", "raw": ""}
        if not key:
            print("[控制确认] 无可用 Key → 模型层不可用（降级精确短语）", flush=True)
            return {"verdict": None, "source": "model_unavailable", "raw": ""}
        from .deepseek_api import chat_once
        from . import ai_promise as _ap        # 宽容解析口径只有一份（`_parse`）
        content = ("她提议的动作：%s\n用户回的话：%s"
                   % (action_summary(action), u[:200]))
        raw = await chat_once(model,
                              [{"role": "system", "content": _CONFIRM_SYSTEM},
                               {"role": "user", "content": content}],
                              key, temperature=0.0, max_tokens=80,
                              hard_timeout=_CONFIRM_HARD_TIMEOUT)
        v = _parse_confirm_verdict(raw)
        if v:
            _ap._parse(raw)          # 同一套结构解析（日志口径一致，顺带留痕）
        else:
            print(f"[控制确认] 模型返回无法解析 → 视作不可用（降级精确短语）: {str(raw)[:80]!r}",
                  flush=True)
        return {"verdict": v, "source": ("model" if v else "model_unavailable"),
                "raw": str(raw or "")}
    except Exception as e:
        print(f"[控制确认] 模型层异常 → 视作不可用（降级精确短语）: {e}", flush=True)
        return {"verdict": None, "source": "model_unavailable", "raw": ""}


def _confirm_prompt(hint: str, channel: str = "voice") -> str:
    """提议的询问话术（分渠道：语音有 F9，QQ/文字只能回话）。

    ★ 只承诺「整句明确说可以」——不再像旧版那样暗示说个字就算（旧话术是判定的唯一提示，
      与现实判定口径不一致会让用户以为「好」就算数）。
    """
    if str(channel or "voice").strip().lower() in ("text", "qq", "onebot"):
        return hint + "，要我做吗？回我一句「可以」我就动手，「不要」就算了"
    return hint + "，要我做吗？按住 F9 说「可以」（整句）"


def propose(action: Dict, session_id: str = "default", character_id: str = "default",
            channel: str = "voice") -> Optional[Dict]:
    """把建议动作存入待确认槽，生成询问话术，异步推送给前端（浮层 + TTS 语音询问）。
    做之前先询问：动作在这里不会被执行，等用户**确认**才执行。

    ★ 2026-09-16：确认不再看关键词子串——真正是否执行由 `confirm_verdict`
      （模型主判 → 精确短语兜底 → 拿不准不执行）决定。TTL 语义不变。
    """
    try:
        if not isinstance(action, dict) or action.get("type") not in ALLOWED_TYPES:
            return None
        hint = str(action.get("_hint") or "要不要我帮你操作一下电脑")
        reply = _confirm_prompt(hint, channel)
        _pending.update(action=dict(action), reply=reply, ts=time.time())
        try:
            from .main import get_loop
            get_loop().create_task(_push_proposal(reply, session_id, character_id))
        except Exception:
            pass
        logger.info(f"[Control] 动作提议：{hint}（等待确认，channel={channel}）")
        return {"action": action, "reply": reply, "channel": channel}
    except Exception:
        return None


def pending_confirm_reply(channel: str = "voice") -> str:
    """拿不准时回给用户的话（提议**留着**，等用户重说一次或等 TTL 过期）。

    ★ 放这里而不是写死在 main.py/onebot.py 两处：两条链路必须说同一套话，
      否则 QQ 教的确认词和 App 教的确认词会越走越偏（用户按哪边说的都可能判错）。
    """
    if str(channel or "voice").strip().lower() in ("text", "qq", "onebot"):
        return "我没听准你是不是要我动手——回我「可以」我就做，「不要」就算了，我先不动"
    return ("我没听准你是不是要我动手——要的话按住 F9 说「可以」，"
            "不要就说「不要」，我先不动")


async def _push_proposal(text: str, session_id: str, character_id: str):
    audio = ""
    try:
        import base64 as _b64mod
        from . import tts as _tts
        try:
            from .character_manager import select_character_voice_cfg
            voice_cfg = select_character_voice_cfg(character_id, emotion="calm", mode="call",
                                                   fallback_voice_key="cosyvoice_default")
        except Exception:
            voice_cfg = None
        voice_cfg = voice_cfg or _tts.get_voice_cfg("cosyvoice_default") \
            or _tts.get_voice_cfg("edge_xiaoxiao") or {}
        if voice_cfg:
            mp3 = await asyncio.wait_for(_tts.generate_audio(text, voice_cfg, ""), timeout=8.0)
            if mp3:
                from pathlib import Path as _Path
                audio = _b64mod.b64encode(_Path(_tts._path_from_url(mp3)).read_bytes()).decode()
    except Exception:
        audio = ""
    try:
        from .main import ws_manager
        await ws_manager.push_to_session(session_id, {
            "type": "pc_proposal", "text": text,
            "audio": audio, "format": "mp3" if audio else "",
        })
    except Exception as e:
        logger.warning(f"[Control] 提议推送失败: {e}")


def match_confirmation(text: str) -> str:
    """**精确短语兜底**匹配（只在模型层不可用时用）。

    返回 'confirm' / 'reject' / 'ambiguous'（拿不准）。

    ★ 2026-09-16 安全修复：这里原来是**子串**判定（`any(w in t for w in _CONFIRM_WORDS)`）
      + 单字词表，于是「好累啊」「我要睡了」「好的我知道了」全被判成 confirm，
      而调用方拿到 confirm 就真执行 → 闲聊触发关机这类不可逆动作。
      现在只认**整句相等**（仅剥空白/标点），不认子串、不认单字包含。
    """
    t = _normalize_confirm_text(text)
    if not t:
        return "ambiguous"
    if t in _REJECT_EXACT:
        return "reject"
    if t in _CONFIRM_EXACT:
        return "confirm"
    return "ambiguous"


async def confirm_verdict(text: str, session_id: str = "default",
                          character_id: str = "default") -> str:
    """确认闸门（模型主判 → 精确短语兜底 → 拿不准不执行）→ 'confirm'/'reject'/'ambiguous'。

    ★ 调用方只有拿到 'confirm' 才允许执行。判定口径见 `confirm_verdict_detailed`。
    """
    r = await confirm_verdict_detailed(text, session_id, character_id)
    return str(r.get("verdict") or "ambiguous")


# 模型/兜底可能给出的判定取值（`unrelated` = 用户没在理这个提议，说的是别的事）
_CONFIRM_VERDICTS = ("confirm", "reject", "ambiguous", "unrelated")


async def confirm_verdict_detailed(text: str, session_id: str = "default",
                                  character_id: str = "default") -> Dict:
    """确认闸门（详细版）：多给一个 `source`，调用方靠它决定"提议留不留"。

    ★ 返回 {"verdict": 'confirm'|'reject'|'ambiguous'|'unrelated',
            "source": 'model'|'fallback'|'none'}

    裁决（用户明确否决"光凭关键词判断"）：
      · 模型判定**优先**：模型说 confirm 就执行（哪怕字面像闲聊），说 reject 就作废；
      · 模型说 ambiguous → 就是 ambiguous：**绝不再拿短语表去"补确认"**
        （模型判不出是她的事，不许降级逻辑把它掰成同意——「好吧」这种口语本来就是
         模型才有资格读的语气，恰恰是最容易误执行的一类）；
      · 模型说 unrelated（原话跟她这个提议没关系）→ 提议作废、原话按新指令继续走；
      · 模型层不可用（无 Key / 异常 / 解析不出）→ 降级 精确短语兜底（source='fallback'）；
      · 兜底也拿不准 → 'ambiguous'（不执行）。

    ★ 兜底路径比模型路径**更窄**：兜底只认明确整句，所以降级永远不会比模型更激进
      ——代价是模型挂掉时一句口语化的「嗯……行」会判成拿不准（提议自然过期，用户重说一次即可），
      这比"模型一坏就把闲聊当确认去真关机"安全得多（fail-closed 的代价方向必须选对）。
    """
    act = _pending.get("action")
    if not act:
        return {"verdict": "ambiguous", "source": "none"}
    m = await confirm_verdict_llm(text, act)
    v = m.get("verdict") if isinstance(m, dict) else None
    if v in _CONFIRM_VERDICTS:
        return {"verdict": v, "source": "model"}
    # 只有模型层不可用（v is None）才降级到精确短语兜底
    return {"verdict": match_confirmation(text), "source": "fallback"}


def void_if_stale_utterance(text: str) -> bool:
    """原话与 pending 提议**无关**时作废提议，返回 True（调用方接着按普通指令处理）。

    ★ 只给**降级/无模型结论**的情形用（见 `confirm_verdict_detailed` 的 verdict/source）：
      模型有结论时它自己会判 ambiguous（还在回应提议）还是 unrelated（压根没理它），
      调用方直接用那个结论，**不要**再拿短语表去猜——这就是"模型主判"的意思。

    ★ 为什么降级时还要有这一步：pending 是**全局槽**，75s 内用户完全可能说一件别的事
      （「打开记事本」）。旧实现是"不是确认 → 提议作废往下走"；改成拿不准不执行后，
      如果一律保留 pending，用户的正常语音控制就会被一个过期的提议挡住。
      所以降级时：拿不准 **且** 原话不像任何确认/拒绝短语 → 作废提议、放行新指令。

    ★ 这里只决定"提议留不留"，**从不决定"执不执行"**（执行权在 `confirm_verdict*`，
      本函数连 execute 都不碰）：即便把一句含糊同意当成新指令作废了提议，
      结果也只是"她没动手"，不会出现"闲聊被当成确认去真关机"。
    """
    if looks_like_confirm_utterance(text):
        return False
    return bool(take_pending())


def looks_like_confirm_utterance(text: str) -> bool:
    """这句话像不像在回应提议（是明确的确认/拒绝整句）？

    给调用方判断"拿不准时提议留不留"用：像 → 留（用户在回答，只是没听清她的意思）；
    不像 → 作废（用户在说别的事，别让过期提议挡住新指令）。
    """
    t = _normalize_confirm_text(text)
    if not t:
        return False
    return t in _CONFIRM_EXACT or t in _REJECT_EXACT


async def run_pending_action(action: Dict, session_id: str = "default",
                            character_id: str = "default") -> Optional[Dict]:
    """**唯一**允许执行 pending 动作的入口（getattr 取，验收可打桩记录"真执行了几次"）。

    调用前提：确认判定已经是 'confirm'。返回 None 表示没有动作可执行（绝不放行）。
    """
    if not isinstance(action, dict) or not action:
        return None
    # 取走 pending（= 这次确认生效；取不到也允许执行传入的 action，调用方必须已判 confirm）
    act = take_pending() or action
    # ★ 用 getattr 而不是直接调 `execute`：真执行入口是验收脚本唯一的"不可逆动作"观测点，
    #   必须能被打桩记录（模块内直接引用函数名会让打桩失效、验收就测不到"到底执行没执行"）。
    _exec = getattr(ctrl, "execute", None)
    if _exec is None:
        return None
    result = await _exec(act, session_id, character_id)
    result = result if isinstance(result, dict) else {}
    hint = str(act.get("_hint") or result.get("action") or "完成")
    extra = ""
    if result.get("ok") and act.get("type") == "screen_info":
        extra = f"，你开着「{str(result.get('info') or '')[:40]}」"
    reply = (hint + extra) if result.get("ok") else (
        hint + "，不过没成功：" + str(result.get("error") or "")[:60])
    return {"ok": True, "reply": reply, "result": result}


def has_pending() -> bool:
    a = _pending.get("action")
    ts = float(_pending.get("ts") or 0)
    return bool(a) and (time.time() - ts) < _PROPOSAL_TTL


def take_pending() -> Optional[Dict]:
    """取走待确认动作（有且未过期返回，否则 None；取走即作废）。"""
    a = _pending.get("action")
    ts = float(_pending.get("ts") or 0)
    _pending.update(action=None, reply="", ts=0.0)
    if a and (time.time() - ts) < _PROPOSAL_TTL:
        return a
    return None


# 本模块自身的引用：`run_pending_action` 用它取真执行入口（`ctrl.execute`），
# 这样验收脚本打桩 `control.execute` 能生效（模块内直接引用函数名会让打桩失效）。
ctrl = sys.modules[__name__]
