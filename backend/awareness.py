# -*- coding: utf-8 -*-
"""
感知系统 · 系统级轻感知 v1.0（结合优化）
复用「屏幕感知状态系统 V3」的感知思想，适配本项目架构：
  - 只做「轻量、只读、零隐私内容采集」的系统感知：
    1. 前台窗口应用（读进程名 + 窗口标题 → 规则映射活动）
    2. 键鼠空闲时长（GetLastInputInfo → 离开/睡着）
  - 系统时钟 / 回复节奏 已在 time_system.build_time_block 里注入，这里不重复。
  - 统一总开关 AWARENESS_ENABLED：一键关闭全部采集。
  - 输出「静默注入块」：仅供 AI 内心参考，严禁输出"检测到你在 xx"类话术。
所有 Windows API 用 ctypes 只读调用，失败静默降级为空，绝不影响聊天主流程。
"""
from . import config


def is_enabled() -> bool:
    """感知总开关。"""
    try:
        return bool(config.get("AWARENESS_ENABLED", False))
    except Exception:
        return False


# ── 1. 前台窗口应用 ──
# 规则顺序即优先级：命中即返回。关键词同时匹配进程名（小写）与窗口标题。
FOREGROUND_RULES = [
    (("code", "visual studio", "pycharm", "intellij", "idea64", "vscode",
      "cursor", "sublime", "eclipse", "android studio", "notepad++", "goland",
      "webstorm", "clion", "rider"), "coding", "在写代码/编程"),
    (("word", "excel", "powerpoint", "wps", "notion", "obsidian", "typora",
      "onenote", "docs.google", "office"), "working", "在处理文档/办公"),
    (("bilibili", "哔哩哔哩", "b站"), "entertainment", "在看B站/视频"),
    (("youtube", "iqiyi", "爱奇艺", "youku", "优酷", "netflix", "腾讯视频",
      "douyu", "斗鱼", "huya", "虎牙", "douyin", "抖音", "kuaishou", "快手"),
     "entertainment", "在看视频/直播"),
    (("steam", "wegame", "原神", "genshin", "league of legends", "英雄联盟",
      "dota", "csgo", "绝地求生", "pubg", "minecraft", "我的世界",
      "王者荣耀", "valorant", "apex", "游戏"), "gaming", "在玩游戏"),
    (("wechat", "微信", "qq", "钉钉", "飞书", "telegram", "discord",
      "whatsapp"), "chat", "在聊天/社交"),
    (("chrome", "edge", "firefox", "msedge", "safari", "360", "浏览器"),
     "browsing", "在浏览网页"),
    (("terminal", "cmd", "powershell", "bash", "xshell", "mobaxterm",
      "windows terminal", "console"), "coding", "在用终端/命令行"),
]

# 活动 → 内心参考（只说给 AI 听，不暴露"检测到"）
ACTIVITY_HINTS = {
    "coding":       "TA 可能在写代码/编程，比较专注，别频繁打断，说正事要简短，可以关心进度和休息",
    "working":      "TA 在处理文档/办公，可能在忙，语气轻一点，别问太多",
    "entertainment": "TA 在看视频/娱乐，比较放松，可以轻松闲聊、撒个娇",
    "gaming":       "TA 在玩游戏，比较投入，可以俏皮地问问战况",
    "chat":         "TA 在社交软件上聊天，可以自然地搭话",
    "browsing":     "TA 在浏览网页，比较闲，可以主动找话题",
}

# ── 具体场景识别（游戏陪玩/娱乐陪伴：进程名/标题 → 具体场景 + 陪玩提示）──
# 命中即返回（排在通用 FOREGROUND_RULES 之前）
SPECIFIC_SCENE_RULES = [
    (("genshin", "原神", "yuanshen", "米哈游"), "原神",
     "TA 在玩【原神】，可以聊抽卡、角色、剧情、配队、圣遗物，俏皮地问问今天抽到啥了，也可以聊深渊和活动"),
    (("honkai", "星穹铁道", "star rail", "崩坏"), "崩坏星穹铁道",
     "TA 在玩【崩坏星穹铁道】，可以聊抽卡、剧情、角色养成、模拟宇宙"),
    (("王者荣耀", "honor of kings", "hikari", "nga 王者"), "王者荣耀",
     "TA 在打王者，可以问战绩、英雄、段位、出装，赢了夸输了哄，俏皮一点"),
    (("stardew", "星露谷"), "星露谷物语",
     "TA 在玩【星露谷】，可以聊农场规划、矿洞探险、村民好感、钓鱼、季节作物、节日"),
    (("minecraft", "我的世界", "javaw"), "我的世界",
     "TA 在玩【我的世界】，可以聊建造、挖矿、红石、末地探险、生存进度"),
    (("和平精英", "pubg", "刺激战场", "绝地求生"), "吃鸡",
     "TA 在打吃鸡/和平精英，可以问枪法、吃鸡了没、跳哪了，紧张时刻替他捏把汗"),
    (("csgo", "cs2", "反恐精英"), "CS",
     "TA 在打 CS，可以聊手感、段位、对枪，输了安慰赢了喝彩"),
    (("league of legends", "英雄联盟", "lol", "金铲铲"), "英雄联盟",
     "TA 在打联盟/金铲铲，可以聊对线、英雄、上分情况"),
    (("douyin", "抖音", "tiktok"), "抖音",
     "TA 在刷抖音，可以轻松陪聊、接梗、聊有趣视频和热点，别太正式"),
    (("bilibili", "哔哩哔哩", "b站"), "B站",
     "TA 在看 B 站，可以聊视频、番剧、UP主、弹幕梗"),
    (("kuaishou", "快手"), "快手",
     "TA 在刷快手，轻松陪聊接梗，聊生活趣事"),
    (("douyu", "斗鱼", "huya", "虎牙", "yy直播"), "直播平台",
     "TA 在看直播，可以聊主播、比赛，轻松搭话"),
]


def detect_specific_scene(proc: str, title: str):
    """进程名/标题 → (具体场景名, 陪玩提示)。未识别返回 (None, '')。"""
    blob = ((proc or "") + " " + (title or "")).lower()
    for kws, scene, hint in SPECIFIC_SCENE_RULES:
        for kw in kws:
            if kw in blob:
                return scene, hint
    return None, ""


def _process_name(pid: int) -> str:
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def _foreground_window():
    """返回 (进程名, 窗口标题)。失败返回 ('', '')。"""
    import ctypes
    from ctypes import wintypes
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return _process_name(pid.value), title
    except Exception:
        return "", ""


def _classify_foreground(proc: str, title: str):
    """进程名/标题 → (activity_key, hint)。未识别返回 (None, '')。"""
    blob = ((proc or "") + " " + (title or "")).lower()
    for kws, _activity, _desc in FOREGROUND_RULES:
        for kw in kws:
            if kw in blob:
                return _activity, ACTIVITY_HINTS.get(_activity, "")
    return None, ""


# ── 2. 键鼠空闲时长 ──
def _idle_seconds() -> int:
    """距上次键鼠输入的秒数（GetLastInputInfo，只读）。失败返回 -1。"""
    import ctypes
    from ctypes import wintypes
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            millis = int(ctypes.windll.kernel32.GetTickCount()) - int(lii.dwTime)
            return max(0, millis // 1000)
    except Exception:
        pass
    return -1


def _idle_hint(idle_secs: int) -> str:
    if idle_secs < 0:
        return ""
    if idle_secs >= 3600:
        return (f"TA 已经 {idle_secs // 3600} 小时没碰键鼠，很可能睡着了或离开电脑了，"
                "别发太长的消息，等 TA 回来")
    if idle_secs >= 600:
        return f"TA 已经 {idle_secs // 60} 分钟没碰键鼠，可能离开电脑了，别催"
    return ""


# ── 3. 统一构建静默注入块 ──
def build_block(session_id: str = "default", character_id: str = "default") -> str:
    """收集前台窗口 + 键鼠空闲，输出静默注入 prompt 块。
    总开关关闭或全部无信号时返回空字符串。"""
    if not is_enabled():
        return ""

    hints = []
    idle = _idle_seconds()

    if idle >= 600:
        # 人离开超过 10 分钟：窗口信息不可靠，只给空闲提示
        ih = _idle_hint(idle)
        if ih:
            hints.append(f"- {ih}")
    else:
        # 人在电脑前：前台窗口可靠
        proc, title = _foreground_window()
        # ★ 游戏陪玩：先试具体场景识别（原神/抖音/星露谷等），命中输出陪玩提示
        scene, scene_hint = detect_specific_scene(proc, title)
        if scene_hint:
            hints.append(f"- {scene_hint}")
        else:
            act, hint = _classify_foreground(proc, title)
            if hint:
                hints.append(f"- {hint}")
            elif proc or title:
                hints.append(f"- TA 正在用「{proc or title[:30]}」，具体活动不明")
        if 120 <= idle < 600:
            hints.append("- TA 短暂离开了一下，可能去倒水/上厕所")

    if not hints:
        return ""

    lines = ["【此刻的 TA（系统感知，你可以知道，但别当成监控报告）】"]
    lines.extend(hints)
    lines.append(
        # ★ 2026-09-11 放宽：原来这里是"绝对不要说出"，等于她知道你在用什么都只能憋着，
        #   表现上就是"她对我此刻的状态毫无感知、每轮都只在回应我上一句话"。
        #   改成：默认不直说，但允许**偶尔**自然带出来（参考别人家的 AI 会说
        #   「你今天下午缩着的时候…」那种"她真的在旁边"的体感）。
        #   仍然禁止暴露来源的话术（「检测到」「系统显示」「我看到你在用 xx」），
        #   也明确禁止每轮都提（否则变成监视）。
        "【怎么用】默认不直说，这些主要帮你判断 TA 的状态、语气和话题。"
        "但**偶尔**可以自然带出来——TA 在同一个软件里泡了很久、或你正好想关心一句时，"
        "用「还在忙呀」「写这么久累不累」「这么晚还没睡呀」这种口吻提一下，"
        "像真的在旁边看着 TA 一样。"
        "【红线】绝不说「检测到你在写代码」「我看到你在用 xx」「系统显示」「你刚才在刷 B 站」"
        "这类暴露来源的话；**也不要每轮都提**，那会变成监视而不是陪伴。"
    )
    return "\n".join(lines)


# ── 4. 生活陪伴模式（陪伴 = 显式授权 AI 看着屏幕，可直说）──
LIFE_COMPANION_MODES = {"play", "douyin", "drama", "music", "night"}

LIFE_MODE_LABELS = {
    "play": "一起玩游戏",
    "douyin": "一起刷抖音",
    "drama": "一起追剧",
    "music": "一起听歌",
    "night": "夜聊",
}


def companion_mode(session_id: str = "default", character_id: str = "default"):
    """读取当前生活陪伴模式（kv）。返回 (mode, label)，未开启返回 ("", "")。

    ★ 身份对齐兜底：App 前端未打开聊天时，会以 character_id='default' 存陪伴模式，
      而 QQ 链路读的是具体角色名（如「助手」），两者对不上会导致 QQ 读不到陪伴模式。
      这里按顺序尝试：精确键 → 仅 session（旧版）→ default 角色键。
    """
    try:
        from . import db as _db
        import json as _json
        cid = str(character_id or "default")
        keys = [f"companion_mode:{session_id}:{cid}"]
        if cid != "default":
            # App 未打开聊天时按 default 存的情况
            keys.append(f"companion_mode:{session_id}:default")
        # 兼容旧版本（只按 session 存）
        keys.append(f"companion_mode:{session_id}")
        raw = ""
        for k in keys:
            try:
                raw = _db.kv_get(k)
            except Exception:
                raw = ""
            if raw:
                break
        if not raw:
            return "", ""
        try:
            d = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            mode = str((d or {}).get("mode", "") or "")
            label = str((d or {}).get("label", "") or LIFE_MODE_LABELS.get(mode, ""))
        except Exception:
            mode = str(raw)
            label = LIFE_MODE_LABELS.get(mode, "")
        return mode, label
    except Exception:
        return "", ""


COMPANION_COMMANDS = {
    "play": ["一起玩游戏", "一起打游戏", "陪我玩游戏", "一起开黑", "一起上分"],
    "douyin": ["一起刷抖音", "一起看抖音", "陪我刷抖音", "一起刷视频", "一起刷短视频"],
    "drama": ["一起追剧", "一起看剧", "陪我追剧", "一起看电视剧", "一起看综艺"],
    "music": ["一起听歌", "一起听音乐", "陪我听歌", "一起听点歌"],
    "night": ["夜聊", "陪我夜聊", "深夜聊天", "一起夜聊", "陪我聊聊"],
}


def parse_companion_command(text: str):
    """从用户消息里识别陪伴指令（QQ 里直接说「一起刷抖音」就能开启）。
    返回 (mode, label)，无指令返回 (None, None)。"""
    t = str(text or "").strip()
    if not t or len(t) > 40:
        return None, None
    for mode, kws in COMPANION_COMMANDS.items():
        for kw in kws:
            if kw in t:
                return mode, LIFE_MODE_LABELS.get(mode, "")
    return None, None


def set_companion_mode(session_id: str = "default", character_id: str = "default",
                       mode: str = "", label: str = "") -> bool:
    """写入陪伴模式。用精确的 (session, character) 键，QQ/App 两侧读取一致。"""
    try:
        from . import db as _db
        import json as _json
        key = f"companion_mode:{session_id}:{character_id}"
        # ★ 记录「一起听音乐」时长：进入 music 开始计时，离开时结算
        try:
            _db.music_duration_tick(character_id, mode)
        except Exception:
            pass
        if mode:
            _db.kv_set(key, _json.dumps({"mode": mode, "label": label}, ensure_ascii=False))
        else:
            _db.kv_set(key, "")
        return True
    except Exception:
        return False


def is_see_question(text: str) -> bool:
    """用户是否在问「你能看到什么 / 看看我在干嘛」这类问题。
    仅在陪伴模式下由调用方触发截屏，普通聊天不会走到。"""
    t = str(text or "").strip()
    if not t or len(t) > 40:
        return False
    return any(k in t for k in (
        "你能看到什么", "你看到什么", "你看到啥", "看到什么", "看到啥",
        "看看我在干嘛", "看我在干嘛", "我在干嘛", "你在看什么", "你能看见",
        "你看见", "看到我", "我现在在干嘛", "我现在干嘛", "我在看什么",
    ))


def build_companion_block(session_id: str = "default", character_id: str = "default",
                          user_text: str = "") -> str:
    """生活陪伴模式下的感知块：允许 AI 自然「直说」此刻陪 TA 在干嘛。

    与 build_block 的「严禁直说」相反——陪伴模式 = 用户显式授权 AI 看着屏幕，
    因此不受 AWARENESS_ENABLED 门控（选了「一起刷抖音」这个动作本身就是授权）。
    """
    mode, label = companion_mode(session_id, character_id)
    if mode not in LIFE_COMPANION_MODES:
        return ""
    try:
        proc, title = _foreground_window()
    except Exception:
        proc, title = "", ""

    scene, scene_hint = detect_specific_scene(proc, title)
    asking = is_see_question(user_text)

    lines = [
        f"【生活陪伴】你此刻正陪 TA「{label}」。TA 已经授权你看着 TA 的屏幕，"
        f"所以关于 TA 在做什么，你可以像真的坐在旁边一样自然地说出来，不用藏着。"
    ]
    if scene_hint:
        lines.append(f"- 现在的情况：{scene_hint}")
    elif proc or title:
        act, hint = _classify_foreground(proc, title)
        if hint:
            lines.append(f"- 现在的情况：{hint}")
        else:
            lines.append(f"- TA 正在用「{proc or title[:30]}」")
    if title and title.strip():
        lines.append(f"- TA 的前台窗口标题：「{title[:80]}」")

    if asking:
        lines.append(
            "【此刻 TA 在问你能看到什么】请如实、自然地把你现在看到的说给 TA 听"
            "（例如「看到你在刷抖音呀，刷到啥好玩的没」「你正开着《XX》呢，玩到哪了」）。"
            "可以直说窗口标题里提到的应用/游戏名，像真的看着 TA 屏幕一样。"
        )
    else:
        lines.append(
            "陪伴要点：像真的在 TA 身边一起看/一起玩那样自然回应，"
            "可以主动聊屏幕上正在发生的事，但别每句话都硬提。"
        )
    return "\n".join(lines)


def capture_now(user_name: str = "你"):
    """同步截屏 + 视觉分析（供线程池调用），绕过全局节流与总开关。

    仅在陪伴模式下、用户明确问「你能看到什么」时由调用方触发。
    返回视觉分析结果 dict（summary/scene_name/topic_hint...），失败返回 None。
    """
    try:
        from .proactive.screen_capture import capture_local, save_capture
        from .proactive.vision_analyzer import analyze_screen
    except Exception:
        return None
    try:
        raw = capture_local()
        if not raw:
            return None
        path = save_capture(raw)
        if not path:
            return None
        return analyze_screen(path, user_name, force=True)
    except Exception as e:
        print(f"[Companion] 截屏分析失败(静默): {e}", flush=True)
        return None


# ── 陪伴感知的冷却与画面记忆（2026-09-09）─────────────────────
# ★ 防话题死循环：用户问「你在看什么」→ AI 截屏回答 → 用户继续聊 → 又截屏……
#   新鲜截屏必须过冷却（默认 90s）；冷却内用上次画面摘要回答，不重复截屏。
#   用户明确说「继续看/接着看」→ 重置冷却，允许再次截屏。
_screen_summary_state = {}   # "session:char" -> {"summary": str, "ts": float}
_FRESH_CAPTURE_COOLDOWN = 90  # 新鲜截屏冷却（秒）


def _sid_key(session_id: str, character_id: str) -> str:
    return f"{session_id or 'default'}:{character_id or 'default'}"


def allow_fresh_capture(session_id: str = "default", character_id: str = "default",
                        force: bool = False) -> bool:
    """是否允许一次新鲜截屏：冷却 90s（config COMPANION_FRESH_CAPTURE_COOLDOWN 可调）。
    force=True（用户明确说「继续看」）→ 无条件允许并重置冷却。"""
    import time as _time
    key = _sid_key(session_id, character_id)
    st = _screen_summary_state.get(key) or {}
    try:
        from . import config as _cfg
        cooldown = float(_cfg.get("COMPANION_FRESH_CAPTURE_COOLDOWN") or _FRESH_CAPTURE_COOLDOWN)
    except Exception:
        cooldown = _FRESH_CAPTURE_COOLDOWN
    if force:
        _screen_summary_state[key] = {"summary": (st or {}).get("summary", ""), "ts": _time.time()}
        return True
    last = float((st or {}).get("ts") or 0)
    return (_time.time() - last) >= cooldown


def remember_screen_summary(session_id: str = "default", character_id: str = "default",
                            summary: str = "") -> None:
    key = _sid_key(session_id, character_id)
    import time as _time
    _screen_summary_state[key] = {"summary": str(summary or ""), "ts": _time.time()}


def last_screen_summary(session_id: str = "default", character_id: str = "default") -> str:
    key = _sid_key(session_id, character_id)
    st = _screen_summary_state.get(key) or {}
    return str(st.get("summary") or "")


def is_continue_watch_command(text: str) -> bool:
    """用户明确让 AI 继续看屏幕（「继续看」「接着看我的屏幕」等）。"""
    t = str(text or "").strip()
    if not t or len(t) > 40:
        return False
    return any(k in t for k in (
        "继续看", "接着看", "再看看", "继续看着", "看着吧", "接着看屏幕", "再看看我屏幕",
    ))
