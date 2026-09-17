# -*- coding: utf-8 -*-
"""
iOS 远程桥接（邮件触发快捷指令 + 截屏回传）

两条链路（非越狱 iPhone 的现实最优解）：
1. 操控手机（后端 → 手机）：
   AI 生成命令 → SMTP 发邮件，主题 `[REMOTE_CMD] <action>:<payload>`
   → iPhone「邮件」自动化匹配主题 → 快捷指令执行（打开 App / 打开 URL / 通知）。
   ⚠️ 单向无回执：后端只知道"邮件发出去了"，不知道手机是否真的执行。
      必须亮屏才会被邮件自动化处理。

2. 看手机屏幕（手机 → 后端）：
   iPhone「截屏」自动化 → 获取最新截屏 → POST /api/ios/screen
   → 后端视觉分析（qwen-vl）→ 存屏幕状态 → 下次聊天注入给 AI。
   ⚠️ 只能"用户截屏后 AI 才看得到"，快捷指令无法主动截屏。

安全：动作白名单，仅 open_app / url / notify / clipboard。
"""
import asyncio
import base64
import json
import re
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

from . import config

# ── 动作白名单 ──
IOS_ACTIONS = {"open_app", "url", "notify", "clipboard"}

# 常见 App 名 → iOS 快捷指令「打开 App」里显示的名字
APP_NAME_MAP = {
    "抖音": "抖音", "douyin": "抖音", "抖音极速版": "抖音极速版",
    "qq": "QQ", "QQ": "QQ",
    "微信": "微信", "wechat": "微信",
    "网易云": "网易云音乐", "网易云音乐": "网易云音乐",
    "b站": "哔哩哔哩", "bilibili": "哔哩哔哩", "哔哩哔哩": "哔哩哔哩",
    "淘宝": "淘宝", "京东": "京东", "支付宝": "支付宝", "拼多多": "拼多多",
    "小红书": "小红书", "微博": "微博", "知乎": "知乎",
    "王者荣耀": "王者荣耀", "原神": "原神", "和平精英": "和平精英",
    "哔哩哔哩漫画": "哔哩哔哩漫画", "爱奇艺": "爱奇艺", "腾讯视频": "腾讯视频",
    "优酷": "优酷", "网易云阅读": "网易云阅读",
}


# ── 中文 App 名 → 远程 ID（用于邮件主题，避免中文编码在 iOS 上显示成 "???"）──
APP_REMOTE_ID_MAP = {
    "抖音": "douyin", "douyin": "douyin",
    "QQ": "qq", "qq": "qq",
    "微信": "wechat", "微信": "wechat", "wechat": "wechat",
    "网易云": "music163", "网易云音乐": "music163",
    "bilibili": "bilibili", "B站": "bilibili", "哔哩哔哩": "bilibili",
    "王者荣耀": "wzry",
    "原神": "ys",
    "淘宝": "taobao",
    "京东": "jd",
    "支付宝": "alipay",
    "小红书": "xhs",
    "微博": "weibo",
    "知乎": "zhihu",
    "抖音极速版": "douyin_lite",
    "爱奇艺": "iqiyi",
    "腾讯视频": "tencent_video",
    "网易云阅读": "nread",
    "豆包": "doubao",
    "火影忍者": "naruto", "火影忍者手游": "naruto", "火影": "naruto",
    "皇室战争": "clashroyale", "皇室": "clashroyale",
    "汽水音乐": "qishui", "汽水": "qishui",
}

# 远程 ID → 中文显示名（供 AI 注入时把 douyin 还原成「抖音」）
APP_ID_TO_NAME = {
    "douyin": "抖音", "qq": "QQ", "wechat": "微信",
    "music163": "网易云音乐", "bilibili": "哔哩哔哩",
    "wzry": "王者荣耀", "ys": "原神", "taobao": "淘宝",
    "jd": "京东", "alipay": "支付宝", "xhs": "小红书",
    "weibo": "微博", "zhihu": "知乎", "douyin_lite": "抖音极速版",
    "iqiyi": "爱奇艺", "tencent_video": "腾讯视频", "nread": "网易云阅读",
    "doubao": "豆包", "naruto": "火影忍者手游",
    "clashroyale": "皇室战争", "qishui": "汽水音乐",
}

# remote_id → iOS URL scheme（Bark 通知点击后跳转打开对应 App）
APP_URL_SCHEME = {
    "douyin": "snssdk1128://", "douyin_lite": "snssdk1128://",
    "qq": "mqq://", "wechat": "weixin://",
    "music163": "orpheus://", "bilibili": "bilibili://",
    "taobao": "taobao://", "jd": "openapp.jdmobile://",
    "alipay": "alipays://", "xhs": "xhsdiscover://",
    "weibo": "sinaweibo://", "zhihu": "zhihu://",
    "iqiyi": "iqiyi://", "tencent_video": "tenvideo://",
    "doubao": "snssdk2329://", "wzry": "tencent1104466820://",
    "ys": "yuanshen://", "naruto": "naruto://",
    "clashroyale": "clashroyale://", "qishui": "qishui://",
}


def _cfg(key: str, default=""):
    try:
        return config.get(key, default)
    except Exception:
        return default


def is_enabled() -> bool:
    try:
        return bool(config.get("IOS_REMOTE_ENABLED", False))
    except Exception:
        return False


def bark_key() -> str:
    """Bark 推送 Key（配置后 open_app 走 Bark 秒级推送，优先于邮件）。"""
    try:
        return str(config.get("IOS_BARK_KEY", "") or "").strip()
    except Exception:
        return ""


# ── 1. 操控手机：发邮件 ──────────────────────────────────────────
def _send_mail_sync(subject: str, body: str) -> dict:
    """同步发一封邮件（smtplib，标准库无额外依赖）。返回 {ok, error?}。"""
    host = str(_cfg("IOS_SMTP_HOST", "") or "").strip()
    port = int(_cfg("IOS_SMTP_PORT", 465) or 465)
    user = str(_cfg("IOS_SMTP_USER", "") or "").strip()
    password = str(_cfg("IOS_SMTP_PASSWORD", "") or "").strip()
    mail_from = str(_cfg("IOS_MAIL_FROM", "") or user or "").strip()
    mail_to = str(_cfg("IOS_MAIL_TO", "") or "").strip()

    if not host or not user or not password or not mail_to:
        missing = [k for k, v in (("IOS_SMTP_HOST", host), ("IOS_SMTP_USER", user),
                                  ("IOS_SMTP_PASSWORD", password), ("IOS_MAIL_TO", mail_to)) if not v]
        return {"ok": False, "error": f"SMTP 未配置完整，缺少：{', '.join(missing)}"}

    msg = MIMEText(body or "", "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("AI伴侣", "utf-8")), mail_from))
    msg["To"] = mail_to

    server = None
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(user, password)
        server.sendmail(mail_from, [mail_to], msg.as_string())
        return {"ok": True, "sent": True}
    except Exception as e:
        return {"ok": False, "error": f"邮件发送失败: {e}"}
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


# ── 1.5 操控手机：Bark 秒级推送（点击通知打开 App）──────────────
def _send_bark_sync(title: str, body: str, url: str = "") -> dict:
    """同步推 Bark 通知（GET api.day.app/{key}/{title}/{body}?url=...）。
    url 为点击通知后跳转的地址（URL scheme 打开 App）。返回 {ok, error?}。"""
    import urllib.request
    import urllib.parse
    key = bark_key()
    if not key:
        return {"ok": False, "error": "Bark Key 未配置（IOS_BARK_KEY 为空）"}
    segs = ["https://api.day.app", key]
    if title:
        segs.append(urllib.parse.quote(title, safe=""))
    if body:
        segs.append(urllib.parse.quote(body, safe=""))
    api = "/".join(segs)
    params = {"level": "timeSensitive", "sound": "default"}
    if url:
        params["url"] = url
    if params:
        api += "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(api, method="GET")
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read().decode("utf-8", "ignore")
        if '"code":200' in data or '"code": 200' in data or "code\":200" in data:
            return {"ok": True, "sent": True}
        return {"ok": False, "error": f"Bark 返回异常: {data[:160]}"}
    except Exception as e:
        return {"ok": False, "error": f"Bark 推送失败: {e}"}


async def send_remote_cmd(action: str, payload: str = "") -> dict:
    """发一条远程指令。action ∈ IOS_ACTIONS。返回 {ok, sent?, error?}。

    ⚠️ 返回 ok=True 只代表"邮件已发出"，不代表手机已执行（单向无回执）。
    """
    action = str(action or "").strip()
    if action not in IOS_ACTIONS:
        return {"ok": False, "error": f"不支持的 iOS 动作: {action}"}
    if not is_enabled():
        return {"ok": False, "error": "iOS 远程操控未开启（IOS_REMOTE_ENABLED=false）"}
    payload = str(payload or "").strip()
    # ★ open_app 时用 ASCII 远程 ID（避免邮件主题里中文在 iOS 显示成 "???"）
    if action == "open_app":
        remote_id = (APP_REMOTE_ID_MAP.get(payload)
                     or APP_REMOTE_ID_MAP.get(payload.lower())
                     or payload)
        subject = f"[REMOTE_CMD] app:{remote_id}"
        body_payload = remote_id
    elif action == "url":
        subject = f"[REMOTE_CMD] url:{payload}"
        body_payload = payload
    elif action == "notify":
        subject = f"[REMOTE_CMD] notify:{payload}"
        body_payload = payload
    else:
        subject = f"[REMOTE_CMD] {action}:{payload}"
        body_payload = payload
    body = json.dumps({"action": action, "payload": body_payload}, ensure_ascii=False)
    # ★ Bark 优先：配置了 Bark Key 且是 open_app 时，走 Bark 秒级推送（点击通知打开 App）
    _bk = bark_key()
    if _bk and action == "open_app":
        _display = APP_ID_TO_NAME.get(body_payload) or body_payload
        _scheme = APP_URL_SCHEME.get(body_payload, "")
        _br = await asyncio.to_thread(
            _send_bark_sync,
            f"帮你打开「{_display}」",
            "点一下通知，我帮你打开",
            _scheme,
        )
        if _br.get("ok"):
            print(f"[iOS] Bark 推送成功: open_app:{body_payload}", flush=True)
        else:
            print(f"[iOS] Bark 推送失败: {_br.get('error')}", flush=True)
        return _br
    result = await asyncio.to_thread(_send_mail_sync, subject, body)
    if result.get("ok"):
        print(f"[iOS] 远程指令已发邮件，主题={subject}", flush=True)
    else:
        print(f"[iOS] 远程指令发邮件失败: {result.get('error')}", flush=True)
    return result


# ── 2. 操控指令识别（QQ 里说「帮我在手机上打开抖音」）──
# 匹配模式：(正则, action, 提取 payload 的函数或固定值)
_OPEN_APP_PATTERNS = [
    r"(?:帮我在手机上?|用快捷指令|手机(?:上)?|iPhone)(?:帮我)?(?:打开|启动|开一下|切到|切回)\s*([\u4e00-\u9fa5A-Za-z0-9]+)",
    r"(?:打开|启动|切到|切回)\s*手机(?:上的|的)?\s*([\u4e00-\u9fa5A-Za-z0-9]+)",
    # ★ 兜底：简单的「打开抖音」（不带"帮我/手机"前缀）。App 名必须是已知 App 才生效，
    #   由 _match_app 里做白名单校验，避免「打开网页/打开心结」这类误判。
    r"(?:打开|启动|切到|切回|开一下)\s*([\u4e00-\u9fa5A-Za-z0-9]{2,10})",
]
_OPEN_URL_PATTERNS = [
    r"(?:帮我在手机上?|手机(?:上)?|iPhone)(?:帮我)?(?:打开|访问)\s*(https?://[^\s]+)",
]
_NOTIFY_PATTERNS = [
    r"(?:手机)?(?:帮我)?提醒我\s*(.+)",
    r"手机(?:上)?给我?发?个?通知[:：]?\s*(.+)",
]


def _match_app(text: str):
    for pat in _OPEN_APP_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            name = m.group(1).strip()
            # 规范化到 iOS App 显示名
            app = APP_NAME_MAP.get(name) or APP_NAME_MAP.get(name.lower()) or name
            # ★ 白名单校验：必须是已知 App（兜底正则较宽，防止「打开网页/打开心结」误判）
            if (app in APP_REMOTE_ID_MAP or app.lower() in APP_REMOTE_ID_MAP
                    or app in APP_NAME_MAP.values()):
                return app
    return None


# 自然表达 → App：用户不会明说「打开抖音」，而是「我想刷抖音」「来把王者」这种
_NATURAL_APP_PATTERNS = [
    ("抖音", ["刷抖音", "看抖音", "刷视频", "看视频", "刷会儿", "刷短视频", "刷会抖", "刷抖"]),
    ("微信", ["聊微信", "回微信", "看微信", "发微信", "微信消息", "回个微信", "微信回"]),
    ("哔哩哔哩", ["刷b站", "看b站", "逛b站", "看番", "追番", "上b站", "刷B站", "看B站", "逛B站"]),
    ("豆包", ["问豆包", "用豆包", "让豆包", "豆包问", "问下豆包"]),
    ("火影忍者手游", ["打火影", "玩火影", "火影忍者", "打忍界", "玩忍界"]),
    ("皇室战争", ["打皇室", "玩皇室", "皇室战争", "打皇室战争"]),
    ("淘宝", ["逛淘宝", "上淘宝", "淘宝看看", "逛会儿淘宝", "网购", "淘宝买"]),
    ("汽水音乐", ["听汽水", "汽水音乐", "放汽水", "汽水放歌", "汽水听歌"]),
    ("网易云音乐", ["听网易云", "网易云", "放歌", "听歌", "来点音乐", "想听歌", "放首歌", "来首歌", "放点歌"]),
]


def _match_natural_app(text: str):
    """匹配自然表达里的 App 意图（「我想刷抖音」→ 抖音）。"""
    for app_name, kws in _NATURAL_APP_PATTERNS:
        for kw in kws:
            if kw in text:
                return app_name
    return None


def _match_url(text: str):
    for pat in _OPEN_URL_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip()
    return None


def _match_notify(text: str):
    for pat in _NOTIFY_PATTERNS:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(1).strip()
    return None


def parse_ios_command(text: str):
    """从用户消息识别 iOS 操控指令。返回 (action, payload) 或 (None, None)。"""
    t = str(text or "").strip()
    if not t or len(t) > 80:
        return None, None
    if not is_enabled():
        return None, None
    app = _match_app(t)
    if not app:
        app = _match_natural_app(t)
    if app:
        return "open_app", app
    url = _match_url(t)
    if url:
        return "url", url
    note = _match_notify(t)
    if note:
        return "notify", note
    return None, None


# ── 3. 看手机屏幕：截屏回传 + 视觉分析 ───────────────────────────
def _screen_state_key(session_id, character_id):
    return f"ios_screen:{session_id}:{character_id}"


def handle_screen_upload(image_b64: str, session_id: str = "default",
                         character_id: str = "default") -> dict:
    """处理 iPhone 上传的截图：保存 → 视觉分析 → 存状态。返回分析结果。"""
    try:
        from .proactive.screen_capture import save_base64_capture
        from .proactive.vision_analyzer import analyze_screen
    except Exception as e:
        return {"ok": False, "error": f"依赖不可用: {e}"}

    try:
        # 去掉 data:image/png;base64, 前缀
        b64 = str(image_b64 or "").strip()
        if "," in b64 and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        if not b64:
            return {"ok": False, "error": "缺少图片数据"}
        path = save_base64_capture(b64)
        if not path:
            return {"ok": False, "error": "图片保存失败"}
    except Exception as e:
        return {"ok": False, "error": f"图片解码失败: {e}"}

    try:
        result = analyze_screen(path, "你", force=True)
    except Exception as e:
        return {"ok": False, "error": f"视觉分析失败: {e}"}
    if not result:
        return {"ok": False, "error": "视觉分析无结果（可能未配置视觉 Key）"}

    # 存状态（供下次聊天注入）
    state = {
        "summary": result.get("summary", ""),
        "scene_name": result.get("scene_name", ""),
        "topic_hint": result.get("topic_hint", ""),
        "ts": __import__("time").time(),
    }
    try:
        from . import db
        db.kv_set(_screen_state_key(session_id, character_id),
                  json.dumps(state, ensure_ascii=False))
    except Exception:
        pass

    return {"ok": True, "state": state}


def get_screen_state(session_id: str = "default", character_id: str = "default"):
    """读最近一次手机屏幕分析结果。"""
    try:
        from . import db
        raw = db.kv_get(_screen_state_key(session_id, character_id))
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def build_ios_screen_block(session_id: str = "default", character_id: str = "default",
                           user_text: str = "") -> str:
    """把手机屏幕状态变成 prompt 注入块（用户主动截屏 = 授权 AI 看，可直说）。"""
    state = get_screen_state(session_id, character_id)
    if not state:
        return ""
    summary = str(state.get("summary") or "").strip()
    scene = str(state.get("scene_name") or "").strip()
    if not summary and not scene:
        return ""
    lines = ["【你刚看到的 TA 手机屏幕（TA 主动截屏给你看，可以自然直说）】"]
    if summary:
        lines.append(f"- 画面内容：{summary}")
    if scene:
        lines.append(f"- 应用/场景：{scene}")
    hint = str(state.get("topic_hint") or "").strip()
    if hint:
        lines.append(f"- 可以聊的点：{hint}")
    lines.append("TA 可能接着问「你看我在干嘛」，请像真的瞄了一眼 TA 手机那样自然回应。")
    return "\n".join(lines)


# ── 4. App 打开事件上报（不靠截图，打开什么软件就上报）──────────
def _current_app_key(session_id, character_id):
    return f"ios_current_app:{session_id}:{character_id}"


def _app_history_key(session_id, character_id):
    return f"ios_app_history:{session_id}:{character_id}"


def record_app_open(app_id: str, session_id: str = "default",
                    character_id: str = "default") -> dict:
    """记录用户打开了某个 App（纯事件上报，不上传屏幕内容）。
    iPhone「打开 App」自动化触发后 POST 到 /api/ios/app_open 调用这里。"""
    import time as _time
    app_id = str(app_id or "").strip().lower()
    if not app_id:
        return {"ok": False, "error": "缺少 app 标识"}
    try:
        from . import db
        now = _time.time()
        # 当前 App
        db.kv_set(_current_app_key(session_id, character_id),
                  json.dumps({"app": app_id, "ts": now}, ensure_ascii=False))
        # 最近 App 轨迹（去重，新的在前，最多 10 条）
        try:
            raw = db.kv_get(_app_history_key(session_id, character_id))
            hist = json.loads(raw) if isinstance(raw, str) else (raw or [])
            hist = [h for h in hist if str(h.get("app", "")).lower() != app_id]
            hist.insert(0, {"app": app_id, "ts": now})
            db.kv_set(_app_history_key(session_id, character_id),
                      json.dumps(hist[:10], ensure_ascii=False))
        except Exception:
            pass
        return {"ok": True, "app": app_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def build_app_activity_block(session_id: str = "default",
                             character_id: str = "default",
                             user_text: str = "") -> str:
    """生成注入块：让 AI 知道 TA 现在/最近在用什么 App（不靠截图）。"""
    try:
        from . import db
        raw = db.kv_get(_current_app_key(session_id, character_id))
        if not raw:
            return ""
        cur = json.loads(raw) if isinstance(raw, str) else (raw or {})
        app_id = str((cur or {}).get("app", "") or "").strip().lower()
        if not app_id:
            return ""
        display = APP_ID_TO_NAME.get(app_id) or app_id
        import time as _time
        secs = int(_time.time() - float((cur or {}).get("ts", 0) or 0))
        if secs < 120:
            head = f"TA 刚刚打开了「{display}」，现在大概率正用着它"
        elif secs < 3600:
            head = f"TA 约 {secs // 60} 分钟前打开了「{display}」"
        else:
            head = f"TA 之前打开过「{display}」"
        lines = [f"【TA 的手机活动（TA 授权你感知，不涉及屏幕内容）】{head}。"]
        lines.append("说与不说、具体说什么，全由你自己根据当下聊天氛围判断：想提就自然带一句，不想提就不提，别生硬。")
        return "\n".join(lines)
    except Exception:
        return ""
