# -*- coding: utf-8 -*-
"""
QQ 机器人接入（OneBot v11 · 反向 WebSocket）
架构：手机QQ -> 电脑NapCat -> OneBot WS -> 本后端 /onebot/v11/ws -> 复用聊天链路 -> 回复
NapCat 反向 WS 地址：ws://127.0.0.1:32123/onebot/v11/ws

会话打通：QQ 与 App 复用同一 session（QQ_SESSION_ID 或自动取角色最新 session），
实现「QQ 消息 App 可见、App 回复发 QQ、主动消息推 QQ」。
"""
import json
import asyncio
import re
import time

from fastapi import WebSocket

# 全局 OneBot 连接（供 main.py / scheduler.py 推送消息到 QQ）
_conn = None

# ── 消息防抖聚合 ──
# 用户连续发多条消息 / 发完还在打字时，不急着逐条回复，
# 而是等一个短暂的"静默窗口"再合并成一条统一回复（避免一问多答、抢话）。
_pending_texts = {}   # user_id -> [text, text, ...]
_pending_mids  = {}   # user_id -> [qq_msg_id, ...]（与 _pending_texts 一一对应）
_pending_tasks = {}   # user_id -> asyncio.Task（防抖定时器）
_pending_reply = {}   # user_id -> [被引用的 QQ message_id, ...]
_last_qq_msg_id = {}  # user_id -> 用户最后一条消息的 QQ message_id
# ★ 标记游戏陪伴执行器是否已处理该 user 的消息且回了话。
#   _dispatch_to_game 处理完 GameBrain.handle_qq_command 后，reply 非空就设置，
#   _flush_reply 检测到则跳过主链路生成（避免"动作确认话术"和"聊天回复"两条）。
_qq_game_processed = {}  # user_id -> 处理时间戳（time.time()）
_pending_getmsg = {}  # echo -> asyncio.Future（get_msg 响应等待）
_recent_stickers = {}  # user_id -> [filename, ...] 最近发过的表情包（避免连发重图）

# ★ 2026-09-16 安全修复：QQ 遥控里**必须先问再动手**的高危动作。
#   原来 QQ 路径只要正则命中就 `execute()`（说「关机」直接排 1 分钟后关机），
#   而 App 语音路径对同样的动作是强制走「提议 → 确认」的——两条路一严一松，
#   松的那条就是漏洞。现在 QQ 也走同一道确认闸门（control.propose + confirm_verdict）。
_HIGH_RISK_PC_TYPES = ("shutdown_timer",)


def _debounce_sec() -> float:
    """QQ 防抖窗口（秒）：config QQ_DEBOUNCE_SEC 可调，默认 4.0，限幅 1~15。

    ★ 2026-09-08 从 2.5 调到 4：用户气泡间的思考间隙普遍 >2.5s，
      导致"每个气泡各回一条"；合并成一个气泡才回总结（用户实测反馈）。
    """
    try:
        from . import config as _cfg
        v = float(_cfg.get("QQ_DEBOUNCE_SEC", 4.0) or 4.0)
        return min(max(v, 1.0), 15.0)
    except Exception:
        return 4.0


def qq_character() -> str:
    from . import config as _cfg
    try:
        c = str(_cfg.get("QQ_CHARACTER", "") or "").strip()
        return c or "default"
    except Exception:
        return "default"


def _allowed_users() -> set:
    """私聊白名单：空集合 = 允许所有人；非空 = 只允许集合内的 QQ 号。"""
    from . import config as _cfg
    try:
        v = _cfg.get("QQ_ALLOWED_USERS", [])
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        return {str(x).strip() for x in (v or []) if str(x).strip()}
    except Exception:
        return set()


def qq_target_users() -> list:
    """主动消息 / App 回复要推送到的 QQ 号列表（默认取白名单全部）。"""
    allowed = _allowed_users()
    if allowed:
        return sorted(allowed)
    return []


def qq_session_id() -> str:
    """QQ 机器人复用的 App 会话 ID（与 App 同一段对话、共享记忆）。"""
    from . import config as _cfg, db as _db
    try:
        sid = str(_cfg.get("QQ_SESSION_ID", "") or "").strip()
        if sid:
            return sid
        cid = qq_character()
        rows = _db.q(
            "SELECT session_id FROM sessions WHERE character_id=? "
            "ORDER BY last_active_at DESC LIMIT 1",
            (cid,),
            fetch=True,
        )
        if rows:
            return str(rows[0]["session_id"])
    except Exception:
        pass
    return "default"


def _extract_text(message) -> str:
    if isinstance(message, str):
        return message.strip()
    parts = []
    for seg in message or []:
        if isinstance(seg, dict):
            if seg.get("type") == "text":
                parts.append(str(seg.get("data", {}).get("text", "")))
        elif isinstance(seg, str):
            parts.append(seg)
    return "".join(parts).strip()


def _extract_reply_id(message) -> str:
    """提取 OneBot 消息里的 reply/quote 段 id（被引用的 QQ 消息 id）。"""
    for seg in message or []:
        if isinstance(seg, dict) and seg.get("type") in ("reply", "quote"):
            try:
                return str(seg.get("data", {}).get("id") or "").strip()
            except Exception:
                return ""
    return ""


def _is_voice_message(message) -> bool:
    """判断消息里是否包含语音（OneBot type="record"）。"""
    for seg in message or []:
        if isinstance(seg, dict) and seg.get("type") == "record":
            return True
    return False


def _is_unsupported_media(message) -> bool:
    """判断消息是否包含视频/文件等暂不支持的媒体类型（图片已支持，不再归入此类）。"""
    for seg in message or []:
        if isinstance(seg, dict) and seg.get("type") in ("video", "file", "face"):
            return True
    return False


def _extract_image_urls(message) -> list:
    """提取 OneBot 消息里的所有图片 URL（type="image" 段）。
    
    OneBot v11 图片段格式：
      {"type": "image", "data": {"file": "...", "url": "https://..."}}
    NapCat 可能只给 file 字段（相对路径 / base64），url 字段才是 HTTP 可访问地址。
    优先取 url，其次取 file（如果看起来像 URL）。
    """
    urls = []
    for seg in message or []:
        if isinstance(seg, dict) and seg.get("type") == "image":
            data = seg.get("data") or {}
            url = str(data.get("url") or "").strip()
            if not url:
                # file 字段有时是完整 URL（如群图），有时是本地路径/base64
                file_val = str(data.get("file") or "").strip()
                if file_val.startswith(("http://", "https://")):
                    url = file_val
            if url:
                urls.append(url)
    return urls


async def _download_image_as_base64(url: str, timeout: float = 15.0) -> str:
    """先从QQ/NapCat下载图片到本地内存，再转 base64 data URL。
    解决：QQ图片URL有时效+防盗链，视觉模型服务器无法直接访问。"""
    try:
        from .http_client import get_http_client
        client = get_http_client()
        r = await client.get(url, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if "image" not in content_type and not url.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            # 可能不是图片，跳过
            return ""
        import base64
        b64 = base64.b64encode(r.content).decode("utf-8")
        # 推断 MIME
        mime = "image/jpeg"
        if "png" in content_type or url.lower().endswith(".png"):
            mime = "image/png"
        elif "gif" in content_type or url.lower().endswith(".gif"):
            mime = "image/gif"
        elif "webp" in content_type or url.lower().endswith(".webp"):
            mime = "image/webp"
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"[OneBot] 图片下载失败: {e}", flush=True)
        return ""


async def _analyze_images(image_urls: list) -> str:
    """用视觉模型分析图片，返回中文描述。失败返回空串。"""
    if not image_urls:
        return ""
    from . import config as _cfg
    vkey = _cfg.vision_key()
    if not vkey:
        return "（发来了一张图，但我还没配置视觉能力，看不到）"
    vmodel = _cfg.vision_model()
    vprovider = _cfg.vision_provider()
    vbase_url = _cfg.vision_base_url()
    # ★ 解析真实视觉模型配置：model 名 / provider / baseUrl（glm-5.3-flash 等新模型走这里，
    #   否则直接用 key 当 model 会导致 siliconflow 的 Qwen2.5-VL-72B 等真实 ID 传错）
    try:
        _vcfg = _cfg.get_vision_model_config(vmodel)
        _real_model = str(_vcfg.get("model") or vmodel).strip() or vmodel
        _real_provider = str(_vcfg.get("provider") or vprovider).strip() or vprovider
        _real_base = str(_vcfg.get("baseUrl") or "").strip()
    except Exception:
        _real_model, _real_provider, _real_base = vmodel, vprovider, ""
    descriptions = []
    for url in image_urls:
        try:
            # ★ 先下载图片转 base64，避免 QQ URL 防盗链/时效导致视觉模型访问失败
            img_data_url = await _download_image_as_base64(url)
            if not img_data_url:
                # 下载失败，尝试直接传 URL（兜底，某些场景仍可用）
                img_data_url = url
            _prompt = ("仔细看这张图，尽量读出图上文字：1) 若是音乐/视频App界面、歌曲页、播放器，"
                       "读出歌名和歌手（如《晴天》周杰伦）；2) 图上其他醒目文字（标题、字幕、聊天、便签、截图内容等）"
                       "也尽量读出来；3) 最后一句概括画面。文字能读多少读多少，别遗漏，别只写'一张截图'。")
            _msg = {"role": "user", "content": [
                {"type": "text", "text": _prompt},
                {"type": "image_url", "image_url": {"url": img_data_url}},
            ]}
            if _real_provider == "siliconflow":
                _url = "https://api.siliconflow.cn/v1/chat/completions"
            elif _real_provider == "zhipu":
                _base = (_real_base or "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
                _url = _base if _base.endswith("/chat/completions") else _base + "/chat/completions"
            else:
                _base = (vbase_url or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
                # ★ vision_base_url 只到 /v1，必须补 /chat/completions，否则 404
                if _base.endswith("/chat/completions"):
                    _url = _base
                else:
                    _url = _base + "/chat/completions"
            # ★ 视觉专用参数（2026-09-10 实测）：glm-5.3-flash「始终思考」，深度思考
            #   会耗尽 max_tokens → 返回 200 但 content 空 →「没看清」。
            #   视觉是读图任务不需要长考：reasoning_effort=low + max_tokens 1500。
            #   其他 provider 不受影响（参数仅智谱链路附加，不支持时智谱返回 400 的
            #   概率极低——该模型强制要求带 effort 档位）。
            _body = {"model": _real_model, "messages": [_msg], "stream": False, "temperature": 0.3,
                     "max_tokens": 1500}
            if _real_provider == "zhipu":
                _body["reasoning_effort"] = "low"
            # ★ 视觉 key 按 provider 分派：provider 换了（如智谱）key 必须跟着换——
            #   vision_key() 里存的通常是老 provider（dashscope）的 key，
            #   拿去打 open.bigmodel.cn 会 401「没看清」（2026-09-08 实测）。
            _vkey = vkey
            if _real_provider == "zhipu":
                _zk = _cfg.zhipu_api_key()
                if _zk:
                    _vkey = _zk
            _headers = {"Authorization": f"Bearer {_vkey}"}
            from .http_client import get_http_client
            client = get_http_client()
            r = await client.post(_url, json=_body, headers=_headers, timeout=30)
            r.raise_for_status()
            _data = r.json()
            desc = (_data["choices"][0]["message"].get("content") or "").strip()
            if not desc:
                # ★ 空内容重试（2026-09-10）：glm 深度思考吃掉 max_tokens 时 content
                #   为空——降档 low 已在请求里，但偶发仍空就再发一次（带更强提示）。
                _msg[0]["text"] = "请用中文简要描述这张图片的内容（50字内，重点读出图中文字）。"
                _body["messages"] = [_msg]
                _body["max_tokens"] = 2000
                r2 = await client.post(_url, json=_body, headers=_headers, timeout=60)
                if r2.status_code == 200:
                    desc = (r2.json()["choices"][0]["message"].get("content") or "").strip()
            if desc:
                descriptions.append(desc)
        except Exception as e:
            print(f"[OneBot] 图片分析失败: {e}", flush=True)
    if descriptions:
        return "【用户发来图片：" + "；".join(descriptions) + "】"
    return "（发来了一张图，但我没能看清）"


def _clean_reply(text: str) -> str:
    t = str(text or "")
    t = re.sub(r"\[ACTION\][\s\S]*?\[/ACTION\]", " ", t, flags=re.I)
    # ★ 不能清除 [sticker:文件名] 标记：QQ 发送循环要靠它把表情包转成图片发出去，
    #   文字部分由 strip_sticker_markers 清理。这里一清，发图逻辑永远收不到标记，
    #   就出现「模型以为自己发了图，实际只发了文字」的幻觉。
    t = re.sub(r"\[/?(?:ACTION|SYSTEM|TOOL|MEMORY|STATE)[^\]]*\]", " ", t, flags=re.I)
    # ★ 修复（2026-09-04）：清理中转站/模型在 content 里夹带的思考块标签。
    #   Claude extended thinking、Qwen QwQ 等会把 <think>...</think> 或 <thinking>...</thinking> 直接放在 content 字段，
    #   不清理就会把模型内部推理过程泄露给用户（用户反馈 gpt/Claude 模型回复里出现 "Crafting warm nap response..."）。
    t = re.sub(r"<think>[\s\S]*?</think>", " ", t, flags=re.I)
    t = re.sub(r"<thinking>[\s\S]*?</thinking>", " ", t, flags=re.I)
    for _ in range(2):
        t = re.sub(r"（[^（）]*）|\([^()]*\)|【[^【】]*】", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _reply_max_seg(session_id: str, character_id: str) -> int:
    """根据好感度决定回复的最大段数（对齐 App 的 dynamicSegmentRange）。"""
    try:
        from .intimacy_manager import get as _get_intimacy
        iv = int(_get_intimacy(session_id, character_id) or 0)
        if iv >= 100:
            return 5
        if iv >= 85:
            return 4
        if iv >= 55:
            return 3
        return 2
    except Exception:
        return 3


def _split_reply(text: str, max_seg: int = 3) -> list:
    """把 AI 回复按语意拆成多段（每段一个气泡）。
    切分顺序：换行 → 句末标点 → 逗号/分号 → 长度强制。
    """
    t = str(text or "").strip()
    if len(t) < 15:
        return [t]

    # 1. 换行优先
    parts = [p.strip() for p in re.split(r"\n+", t) if p.strip()]

    # 2. 句末标点切
    sentences = []
    for p in parts:
        sentences += [s.strip() for s in re.findall(r"[^。！？!?]+[。！？!?]?", p) if s.strip()]

    # 3. 逗号/分号细分（语意更细）
    if len(sentences) <= 1 and len(t) > 30:
        sentences = [s.strip() for s in re.findall(r"[^，,；;]+[，,；;]?", t) if s.strip()]

    # 4. 长度强制切
    if len(sentences) <= 1 and len(t) > 50:
        mid = len(t) // 2
        for i in range(mid, min(mid + 15, len(t))):
            if t[i] in "，, 。":
                mid = i + 1
                break
        sentences = [t[:mid].strip(), t[mid:].strip()]

    sentences = [s for s in sentences if s.strip()]
    if len(sentences) <= 1:
        return [t]

    # 5. 段数上限：超出则把多余的并进最后一段
    if len(sentences) > max_seg:
        sentences = sentences[:max_seg - 1] + ["".join(sentences[max_seg - 1:])]

    # 6. 末段是单字/双字语气词则并回上一段
    if len(sentences) >= 2:
        tail = sentences[-1]
        if len(tail) <= 3 and re.fullmatch(r"[宝就呢啊嘛呀哦嗯哈呵嘿啦吧噢呗咯嘞噻欸么耶]{1,3}", tail):
            sentences[-2] += sentences[-1]
            sentences.pop()

    return [s for s in sentences if s.strip()]


async def send_qq_message(text: str) -> bool:
    """把 AI 文本推送到 QQ 大号（供 main.py / scheduler.py / idle_agent 调用）。

    ★ 2026-09-12 修：这里原来只发一个纯文字段，`[sticker:xxx]` 标记**原样当文字发出去**。
      而主动消息（scheduler._deliver / idle_agent._deliver_proactive / 提醒 / 她饿了）
      全走这个函数 —— 于是它们发的表情包在 QQ 里全变成 "[sticker:傲娇]" 这种字面文字。
      实测 NapCat 日志里有 23 条真实发送带着这种残留。
      QQ「回复」那条链（发送循环）本来就会解析标记转图片，唯独这条漏了，现在补齐。
    """
    global _conn
    t = _clean_reply(text)
    if not t or _conn is None:
        return False
    targets = qq_target_users()
    if not targets:
        return False

    # ★ 2026-09-13：文字与表情包拆成多条消息发。
    #   原来文字+图片段塞在同一个 send_private_msg 里，QQ 会把「文字+表情包」
    #   渲染进同一个气泡；拆开后各是独立气泡，跟真人发图一样。
    _text_msg = []
    _sticker_msgs = []
    try:
        from . import sticker_manager as _sm
        for _st in _sm.parse_sticker_markers(t):
            _seg = _sticker_image_segment(_st["filename"])
            if _seg:
                _sticker_msgs.append([_seg])
        t = _sm.strip_sticker_markers(t)
    except Exception as _se:
        print(f"[OneBot] 推QQ表情包解析失败(静默): {_se}", flush=True)
    if str(t or "").strip():
        _text_msg = [{"type": "text", "data": {"text": str(t).strip()}}]
    if not _text_msg and not _sticker_msgs:
        return False

    ok = False
    for target in targets:
        for _msg in ([_text_msg] if _text_msg else []) + _sticker_msgs:
            try:
                await _conn.send_text(json.dumps({
                    "action": "send_private_msg",
                    "params": {
                        "user_id": int(target) if target.isdigit() else target,
                        "message": _msg,
                    },
                    "echo": f"push_{target}_{int(time.time() * 1000)}",
                }, ensure_ascii=False))
                ok = True
            except Exception as e:
                print(f"[OneBot] 推QQ失败({target}): {e}", flush=True)
            await asyncio.sleep(0.25)
    return ok


async def send_qq_music_card(song: dict) -> bool:
    """发网易云音乐卡片到 QQ（OneBot music 段 type=163）。
    song: {id, name, artist}。成功返回 True。"""
    global _conn
    if _conn is None or not song or not song.get("id"):
        return False
    targets = qq_target_users()
    if not targets:
        return False
    _mid = str(song["id"])
    _msg = [{"type": "music", "data": {"type": "163", "id": _mid}}]
    ok = False
    for target in targets:
        try:
            await _conn.send_text(json.dumps({
                "action": "send_private_msg",
                "params": {
                    "user_id": int(target) if target.isdigit() else target,
                    "message": _msg,
                },
                "echo": f"music_{target}_{int(time.time() * 1000)}",
            }, ensure_ascii=False))
            ok = True
        except Exception as e:
            print(f"[OneBot] 发音乐卡片失败({target}): {e}", flush=True)
    return ok


async def send_qq_voice(text: str, audio_url: str) -> bool:
    """把 AI 语音推送到 QQ（record 段 + 可选转写文字）。
    成功返回 True；失败返回 False，调用方应回退 send_qq_message 发纯文字。
    audio_url 为本后端提供的 /tts_cache/ HTTP 地址。

    ★ 语音改为 base64 发送：NapCat 对 record 段 file 用 HTTP URL 时下载经常失败
    （表现为 QQ 端收不到语音条，只有文字），改成本地读文件转 base64 最稳。"""
    global _conn
    if _conn is None or not audio_url:
        return False
    t = _clean_reply(text)
    targets = qq_target_users()
    if not targets:
        return False
    # ★ 把 /tts_cache/ 的 URL 转成本地文件 → base64，绕开 NapCat 下载 HTTP URL 的坑
    file_field = audio_url
    try:
        import base64 as _b64
        import os as _os
        from . import tts as _tts
        _fname = str(audio_url).split("/")[-1].split("?")[0]
        _local = _os.path.join(_tts._CACHE_DIR, _fname)
        if _os.path.exists(_local) and _os.path.getsize(_local) > 0:
            with open(_local, "rb") as _f:
                file_field = "base64://" + _b64.b64encode(_f.read()).decode()
            print(f"[OneBot] 语音转 base64: {_fname} ({_os.path.getsize(_local)}B)", flush=True)
    except Exception as _be:
        print(f"[OneBot] 语音转 base64 失败，回退 URL: {_be}", flush=True)
    ok = False
    for target in targets:
        try:
            _msg = [{"type": "record", "data": {"file": file_field}}]
            if t:
                _msg.append({"type": "text", "data": {"text": t}})
            await _conn.send_text(json.dumps({
                "action": "send_private_msg",
                "params": {
                    "user_id": int(target) if target.isdigit() else target,
                    "message": _msg,
                },
                "echo": f"voicepush_{target}_{int(time.time() * 1000)}",
            }, ensure_ascii=False))
            ok = True
        except Exception as e:
            print(f"[OneBot] 推QQ语音失败({target}): {e}", flush=True)
    return ok


def _sticker_image_segment(filename: str) -> dict:
    """把表情包文件转成 OneBot image 段（读本地文件转 base64，NapCat 对 URL 下载常失败）。

    与 send_qq_voice 同思路：不依赖后端把文件暴露成 HTTP URL，直接 base64 最稳。
    ★ 兜底：模型常写 [sticker:开心]（中文情绪词）而非文件名，先按原名找文件，
      找不到就用 resolve_sticker 按 tag/desc 语义匹配到真实文件。
    """
    from pathlib import Path
    from . import config as _cfg
    safe = Path(str(filename or "")).name
    _candidates = [safe]
    try:
        from . import sticker_manager as _sm
        _resolved = _sm.resolve_sticker(safe)
        if _resolved and _resolved != safe:
            _candidates.append(_resolved)
    except Exception:
        pass
    for base in (_cfg.ROOT_DIR / "表情包", _cfg.DATA_DIR / "表情包"):
        for _name in _candidates:
            fp = base / _name
            if fp.exists():
                try:
                    import base64
                    # ★ 表情包缩小（2026-09-08 用户反馈"太大"）：原图直接发 QQ 会按原始
                    #   分辨率显示，表情包应是小图。缩到最长边 160px（带缓存，不重复编码）。
                    _send_fp = fp
                    try:
                        from PIL import Image as _PILImage
                        _cache_dir = _cfg.DATA_DIR / "sticker_cache"
                        _cache_dir.mkdir(parents=True, exist_ok=True)
                        _cached = _cache_dir / (fp.stem + "_s160" + fp.suffix.lower())
                        if _cached.exists():
                            _send_fp = _cached
                        else:
                            _img = _PILImage.open(fp)
                            _side = max(_img.size)
                            if _side > 160:
                                _ratio = 160 / _side
                                _img = _img.convert("RGBA" if fp.suffix.lower() in (".png", ".webp") else "RGB")
                                _img = _img.resize((max(1, int(_img.width * _ratio)), max(1, int(_img.height * _ratio))))
                                _img.save(_cached)
                            else:
                                _img.close()
                                _cached.write_bytes(fp.read_bytes())  # 原本就小，也进缓存
                            _send_fp = _cached
                    except Exception as _re:
                        print(f"[StickerDebug] 缩略失败用原图: {_re}", flush=True)
                        _send_fp = fp
                    b64 = base64.b64encode(_send_fp.read_bytes()).decode("ascii")
                    return {"type": "image", "data": {"file": "base64://" + b64}}
                except Exception:
                    return None
    return None


async def send_qq_image(filename: str, user_id: str = "") -> bool:
    """把一张表情包图片发送到 QQ（base64 image 段）。成功返回 True。"""
    global _conn
    if _conn is None:
        return False
    seg = _sticker_image_segment(filename)
    if not seg:
        return False
    targets = [user_id] if user_id else qq_target_users()
    if not targets:
        return False
    ok = False
    for target in targets:
        try:
            await _conn.send_text(json.dumps({
                "action": "send_private_msg",
                "params": {
                    "user_id": int(target) if target.isdigit() else target,
                    "message": [seg],
                },
                "echo": f"sticker_{target}_{int(time.time() * 1000)}",
            }, ensure_ascii=False))
            ok = True
        except Exception as e:
            print(f"[OneBot] 推QQ表情包失败({target}): {e}", flush=True)
    return ok


async def _push_to_app(session_id: str, character_id: str, content: str, role: str = "assistant", ts: float = None):
    """把 QQ 消息实时推送到 App。
    role="user"      → 推送 QQ 用户消息（前端渲染为用户气泡）
    role="assistant" → 推送 AI 回复（前端渲染为 AI 气泡）
    ts: 显式毫秒时间戳（多段回复按发送序号递增传入）。前端按 ts 排序显示，
    不带 ts 时前端用接收时刻——多段推送挤在同一毫秒/秒时 App 会乱序（2026-09-08 用户实测）。
    """
    try:
        # ★ App 不渲染游戏动作标记（QQ 侧已执行），推送前剥掉
        import re as _re
        content = _re.sub(r"\[sd[:：]\s*[^\]]{1,12}\s*\]", "", str(content or "")).strip()
    except Exception:
        pass
    try:
        from . import main as _main
        _payload = {
            "type": "proactive" if role == "assistant" else "qq_user_msg",
            "session_id": session_id,
            "contact_id": character_id,
            "character_id": character_id,
            "content": content,
            "source": "qq",
            "role": role,
        }
        if ts is not None:
            _payload["ts"] = int(ts)
        await _main.ws_manager.push_to_session(session_id, _payload)
    except Exception as e:
        print(f"[OneBot] 推App失败(静默): {e}", flush=True)


async def _fetch_qq_msg_text(ws, message_id: str) -> str:
    """通过 NapCat get_msg 获取某条 QQ 消息的原文（用于引用）。失败返回空串。"""
    if ws is None or not message_id:
        return ""
    echo = f"getmsg_{int(time.time() * 1000)}_{message_id}"
    try:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
    except Exception:
        return ""
    _pending_getmsg[echo] = fut
    try:
        await ws.send_text(json.dumps({
            "action": "get_msg",
            "params": {"message_id": int(message_id) if str(message_id).isdigit() else message_id},
            "echo": echo,
        }, ensure_ascii=False))
    except Exception:
        _pending_getmsg.pop(echo, None)
        return ""
    try:
        data = await asyncio.wait_for(fut, timeout=3.0)
    except Exception:
        _pending_getmsg.pop(echo, None)
        return ""
    try:
        _segs = (data or {}).get("data", {}).get("message") or []
        return _extract_text(_segs)
    except Exception:
        return ""


async def _generate_reply(user_id: str, text: str) -> str:
    from . import db as _db, config as _cfg
    from . import chat_logic
    from .deepseek_api import chat_once

    session_id = qq_session_id()
    character_id = qq_character()

    try:
        recent = _db.recent_messages(session_id, limit=60, character_id=character_id)
        # ★ db.recent_messages 返回 sqlite3.Row，没有 .get() 方法，必须用下标访问，
        #   否则永远抛异常被吞掉 → 历史上下文为空 → 复读/答非所问。
        messages = [
            {"role": m["role"], "content": m["content"] or ""}
            for m in recent
            if m["role"] in ("user", "assistant") and m["content"]
        ]
    except Exception:
        messages = []
    messages.append({"role": "user", "content": text})

    # ★ 2026-09-11：QQ 链路接入 Agent 模式（积木工具：看代码/设任务/学规则），与 App 同款。
    #   理解层先判一次；判定为任务型 → 走 Agent 循环，最终回答直接作为 QQ 回复。
    try:
        from .agent.mode import detect_agent_task as _detect
        from . import understanding as _und
        _intent = {}
        try:
            _intent = await _und.understand(text, messages[:-1], session_id=session_id,
                                            character_id=character_id, character_name=character_id) or {}
        except Exception:
            _intent = {}
        if _detect(_intent, text):
            from .agent.loop import run_agent_task
            _final = await run_agent_task(
                text, session_id=session_id, character_id=character_id,
                character_name=character_id, key="", model="",
            )
            if _final:
                return _final
    except Exception as _ag_err:
        print(f"[OneBot] Agent 分支失败(静默，回退普通聊天): {_ag_err}", flush=True)

    try:
        # ★ sticker=True：回退逻辑也走 App 同款表情包注入，避免回退时模型不知道能发图
        enriched = await chat_logic.enrich_messages(messages, session_id, character_id, sticker=True)
    except Exception as e:
        print(f"[OneBot] enrich 失败(静默): {e}", flush=True)
        enriched = messages

    # ★ 单角色大脑：QQ 机器人复用的角色若在人格设置页单独配了模型，这里跟随它
    model = chat_logic.pick_model(None, True, character_id)
    key = _cfg.api_key_for_model(model)
    if not key:
        return "（还没配置 API Key，先在电脑 App 设置页里填好）"
    # ★ base_url：按模型 provider 取接口地址（GLM/通义/Kimi 等非 DeepSeek 必须传，否则打到 DeepSeek 地址）
    try:
        _base_url = str((_cfg.text_models() or {}).get(model, {}).get("baseUrl") or "")
    except Exception:
        _base_url = ""
    # ★ 深度思考模式：角色卡开启 deep_thinking 后，生成层改用 deepseek-reasoner
    #   （仅 DeepSeek 生效；切到 GLM 等其它 provider 时深度思考不触发，保持原模型）
    try:
        from . import character_manager as _cm
        _cc = _cm.get_character_any(character_id) or {}
        if _cc.get("deep_thinking"):
            model = _cfg.deep_thinking_model(model)
    except Exception:
        pass

    # ★ 人称指代约束：一次生成多句时，"你/我"容易漂移导致逻辑混乱
    try:
        for _m in enriched:
            if isinstance(_m, dict) and _m.get("role") == "system":
                _m["content"] = (_m.get("content") or "") + (
                    "\n\n【人称指代·硬性】连续多句里「你」永远指用户、「我」永远指你自己，"
                    "不要漂移。是谁做的动作就写谁：比如「你承认我偷着乐」不能写成"
                    "「你承认自己偷着乐」。多句之间逻辑要连贯，别前后矛盾、别接不上。"
                )
                break
    except Exception:
        pass

    # ★ 2026-09-15（事故治本）：QQ 回复链的超时预算
    #   原来外层 wait_for 一把 180s 掐死 → 用户收到「（刚刚想事情卡住了，你再说一次～）」
    #   （真机 03:59:26 / 04:08:20 / 04:17:01 / 04:19:27 / 04:33:43 全是这句），
    #   可当时她的大脑（GLM）明明是活的 —— 挂掉的是 DeepSeek 上的后台杂活。
    #   现在：内层 chat_once 自带硬超时 + provider 熔断 + **失败切兜底模型**（llm_guard），
    #   外层只做"主模型 + 兜底都没回来"的最终保险 → 预算 = 长档 × 2 + 30s 余量。
    try:
        from . import llm_guard as _lg
        _budget = _lg.long_timeout_sec()
    except Exception:
        _budget = 180.0
    try:
        reply = await asyncio.wait_for(
            chat_once(model, enriched, key, temperature=0.8, max_tokens=2048,
                      base_url=_base_url, hard_timeout=_budget),
            timeout=_budget * 2 + 30,
        )
    except asyncio.TimeoutError:
        print(f"[OneBot] 生成超时（主模型 {model} 与兜底都没回来，预算 {_budget * 2 + 30:.0f}s）",
              flush=True)
        return "（刚刚想事情卡住了，你再说一次～）"
    except Exception as e:
        print(f"[OneBot] 生成失败: {e}", flush=True)
        return "（我这边出了点小状况，稍等再试一次）"

    _raw_reply = reply
    if "[sticker" in str(reply).lower():
        print(f"[StickerDebug] 旧逻辑生成含sticker(clean前): {str(reply)[:160]!r}", flush=True)
    reply = _clean_reply(reply)
    # ★ _clean_reply 会剥掉所有括号/方括号，极端时把合法回复清成空串，
    #   导致外层误走「全部失败」兜底。这里打印原文便于诊断。
    if not reply:
        print(f"[OneBot] _clean_reply 清空回复，原文: {str(_raw_reply)[:200]!r}", flush=True)
        return ""

    # ★ 去重：与最近 AI 发言重复则重新生成（对齐 App 聊天的防复读）
    try:
        from .companion.quality_guard import dedup_check_async, recent_ai_texts
        if await dedup_check_async(
            reply, session_id, character_id,
            chat_once_fn=chat_once, model=model, api_key=key,
        ):
            _said = recent_ai_texts(session_id, character_id, 5)
            _avoid = "\n".join(f"- {str(s)[:80]}" for s in _said if str(s or "").strip())
            _last = ""
            for _ in range(2):
                try:
                    _prompt = (
                        "你刚要发的这句话，和你之前说过的话重复了"
                        "（换个词、换个说法、换种句式都算重复）：\n"
                        + (_avoid or "- （暂无历史）")
                        + "\n请重新说一句：换一个角度，给出新信息、新的感受或新的追问。"
                        "不要复述上面任何一句的意思，也不要只是换几个词再说一遍。\n"
                        "只输出这一句话本身，不要任何解释、不要加引号。"
                    )
                    if _last:
                        _prompt += (
                            f"\n\n注意：你上一次重试说的是「{_last[:60]}」，"
                            f"仍然重复，请换一个完全不同的角度。"
                        )
                    _raw = await chat_once(
                        model, [{"role": "user", "content": _prompt}], key,
                        temperature=0.95, max_tokens=300, base_url=_base_url,
                    )
                    _new = str(_raw or "").strip()
                    if not _new:
                        continue
                    _last = _new
                    if not await dedup_check_async(
                        _new, session_id, character_id,
                        chat_once_fn=chat_once, model=model, api_key=key,
                    ):
                        reply = _clean_reply(_new)
                        break
                except Exception:
                    break
    except Exception as e:
        print(f"[OneBot] 去重失败(静默): {e}", flush=True)

    if not reply:
        return ""

    try:
        _db.add_message(session_id, "user", text, character_id, {"source": "qq"})
        # ★ [VOICE] 标记只留内容（这条是 once 生成器失败时的回退路径）
        _db.add_message(session_id, "assistant",
                        re.sub(r"\[/?voice\]", "", str(reply), flags=re.I).strip(),
                        character_id, {"source": "qq"})
    except Exception:
        pass

    return reply


async def _generate_reply_turns(user_id: str, text: str, capability_extra: str = "") -> list:
    """用 App 同款 MultiTurnGenerator（once 模式）生成多段回复。

    让 QQ 与 App 复用同一套生成器，气泡内容/条数/顺序一致，
    修复"QQ 与 App 气泡对不上"的问题。同时 once 模式一次生成只调 1 次 LLM，
    比旧链路（生成 + 去重 + 重试）更快。

    capability_extra: 能力说明（[SONG]/[VOICE] 标记用法）。★ 必须走**参数**注入到
    system 消息，绝不能拼进 text —— text 是用户那一轮的发言，拼进去会既让模型
    去"回应说明文字"（本地小模型直接答非所问），又被写进 chat_history 永久污染历史。

    成功返回 ["段1","段2",...]；任何失败返回 []，调用方回退旧逻辑。
    """
    from . import db as _db, config as _cfg
    from . import chat_logic

    session_id = qq_session_id()
    character_id = qq_character()

    try:
        recent = _db.recent_messages(session_id, limit=60, character_id=character_id)
        messages = [
            {"role": m["role"], "content": m["content"] or ""}
            for m in recent
            if m["role"] in ("user", "assistant") and m["content"]
        ]
    except Exception:
        messages = []
    messages.append({"role": "user", "content": text})

    try:
        # ★ sticker=True：QQ 也走 App 同款表情包注入（把表情包列表写进 system prompt），
        #   让模型知道「发图必须写 [sticker:文件名]，光说‘发一张’不算发」，否则会幻觉自己发了。
        enriched = await chat_logic.enrich_messages(messages, session_id, character_id, sticker=True)
    except Exception:
        enriched = messages

    # ★ 2026-09-12：能力说明（[SONG]/[VOICE]）注入到 **system**，不再拼进用户消息。
    #   原来这两段拼在 user turn 里 → 模型去"回应说明文字"（本地 4B 直接答非所问），
    #   而且会连带写进 chat_history 永久污染历史（实测 #9141）。
    if capability_extra:
        try:
            _cpatched = False
            for _cm in enriched:
                if isinstance(_cm, dict) and _cm.get("role") == "system":
                    _cm["content"] = (_cm.get("content") or "") + capability_extra
                    _cpatched = True
                    break
            if not _cpatched:
                enriched.insert(0, {"role": "system", "content": capability_extra.lstrip()})
        except Exception as _ce:
            print(f"[OneBot] 能力说明注入 system 失败(静默): {_ce}", flush=True)

    # ★ 单角色大脑：用当前角色（含角色卡 model）的模型，而不是全局 deepseek-chat
    #   （否则 QQ 复用的角色在人格设置里配了中转 Claude/Gemini 也不生效，白走全局）
    model = chat_logic.pick_model(None, True, character_id)
    key = _cfg.api_key_for_model(model)
    if not key:
        return []
    try:
        _base_url = str((_cfg.text_models() or {}).get(model, {}).get("baseUrl") or "")
    except Exception:
        _base_url = ""

    async def _try_generate(msgs):
        from .multi_turn.generator import MultiTurnGenerator
        gen = MultiTurnGenerator()
        turns = []
        async for t in gen.generate_once_stream(
            base_messages=msgs,
            ai_emotion={"emotion": "calm", "intensity": 0.5},
            session_id=session_id,
            character_id=character_id,
            model_override=model,
            base_url_override=_base_url,
        ):
            if t.get("turn_type") == "thinking":
                continue
            c = str(t.get("content") or "").strip()
            if "[sticker" in c.lower():
                print(f"[StickerDebug] once生成含sticker(clean前): {c[:160]!r}", flush=True)
            c = _clean_reply(c)
            if "[sticker" in c.lower():
                print(f"[StickerDebug] once生成含sticker(clean后): {c[:160]!r}", flush=True)
            if c:
                turns.append(c)
        return turns

    try:
        return await _try_generate(enriched)
    except Exception as e:
        print(f"[OneBot] once 生成失败: {e}", flush=True)
        # ★ 重试：截断历史只留最近 3 条 + 当前消息，减少上下文长度
        try:
            shorter = messages[-4:] if len(messages) > 4 else messages
            short_enriched = await chat_logic.enrich_messages(shorter, session_id, character_id, sticker=True)
            return await _try_generate(short_enriched)
        except Exception as e2:
            print(f"[OneBot] once 截断重试也失败: {e2}", flush=True)
            return []


# ★ 点歌意图提取（供 _flush_reply 触发搜歌 + 发网易云卡片）
_MUSIC_BLACKLIST_STARTS = ("你说", "你唱", "你讲", "你聊", "你陪", "听你", "看看", "你放", "你说话")


def _extract_music_request(text: str):
    """从用户文本识别点歌意图并提取关键词（可含歌手，如"周杰伦的晴天"）。
    只匹配明显点歌句式，避免误伤日常聊天。非点歌返回 None。"""
    import re as _re
    t = str(text or "").strip()
    if not t or len(t) > 60:
        return None
    # 泛泛说"想听歌/来点歌"（没点具体歌）→ 不触发（那是开启陪伴，不是点歌）
    if _re.search(r"(想听歌|想听音乐|听点歌|来点歌|放点歌|一起听歌|一起听音乐)$", t):
        return None
    _m = _re.search(
        r"(?:想听|我想听|帮我放|放一首|放首|放个|放点|来一首|来首|来点|点一首|点个|点首)"
        r"\s*[《「]?([\u4e00-\u9fa5A-Za-z0-9·&!！？?]{2,20}?)[》」]?", t)
    if not _m:
        return None
    kw = _m.group(1).strip()
    kw = _re.sub(r"[《》「」'\"，,。.!！？?·\s]", "", kw)
    kw = _re.sub(r"[的了我]吧$|[的了我]$", "", kw)
    if len(kw) < 2:
        return None
    if kw.startswith(_MUSIC_BLACKLIST_STARTS):
        return None
    return kw


def _pending_confirm_busy() -> bool:
    """有未过期的待确认提议 → QQ 这条消息先交给确认闸门（见 `_handle_pending_confirm`）。"""
    try:
        from . import control as _ctrl
        return bool(_ctrl.has_pending())
    except Exception:
        return False


async def _handle_pending_confirm(user_id: str, text: str) -> None:
    """QQ 路径的确认闸门（与 App 语音路径同一道 `control.confirm_verdict`）：

      · confirm → `run_pending_action` 真执行 + 回结果；
      · reject  → 作废提议；
      · 拿不准  → **不执行**：提议留着等 TTL（75s）自然过期，并说明怎么确认；
                 但如果这句原话跟提议毫无关系（是条新指令）→ 作废提议放行，不吞用户的指令。
    """
    try:
        from . import control as _ctrl
        if not _ctrl.has_pending():
            return
        sess, cid = qq_session_id(), qq_character()
        # ★ 顺序要紧：先判定（判定要读 pending 里的提议），拿到 confirm 才取走。
        _cv = await _ctrl.confirm_verdict_detailed(text, sess, cid)
        _rep = str(_cv.get("verdict") or "ambiguous")
        msg = ""
        if _rep == "confirm":
            act = _ctrl.take_pending()
            if act:
                r = await _ctrl.run_pending_action(act, sess, cid)
                msg = str((r or {}).get("reply") or "做好啦✓")
        elif _rep == "reject":
            _ctrl.take_pending()
            msg = "好的，那我不动了～"
        elif _rep == "unrelated":
            _ctrl.take_pending()   # 模型判「跟提议没关系」→ 作废，交给主链路正常处理
        elif str(_cv.get("source") or "") == "fallback" \
                and _ctrl.void_if_stale_utterance(text):
            pass            # 降级兜底 + 明显是条新指令 → 提议已作废，交给主链路正常处理
        else:
            msg = _ctrl.pending_confirm_reply("text")
        if not msg:
            return          # 原话与提议无关、提议已作废 → 交给主链路正常处理
        try:
            await send_qq_message(msg)
            # ★ 拦截发生在「推 user 气泡到 App」之前，这里补推保持聊天记录完整
            await _push_to_app(sess, cid, text, role="user")
            await _push_to_app(sess, cid, msg, role="assistant")
        except Exception:
            pass
        _qq_game_processed[user_id] = time.time()   # 主链路跳过（确认结果就是回复）
    except Exception as _pce:
        print(f"[OneBot] 待确认提议处理失败: {_pce}", flush=True)


def _handle_user_message(user_id: str, text: str, reply_id: str = ""):
    """收到用户私聊消息：立即推 App（用户气泡）+ 防抖调度回复。"""
    # 0.1 ★ 生活陪伴指令：QQ 里直接说「一起刷抖音/一起追剧/一起听歌…」即开启陪伴（不用切 App）
    try:
        from . import awareness as _aw
        _cm, _cl = _aw.parse_companion_command(text)
        if _cm is not None:
            _aw.set_companion_mode(qq_session_id(), qq_character(), _cm, _cl)
    except Exception:
        pass
    # 0.1 ★ 待确认提议（QQ 路径的确认闸门，与 App 语音路径同一道）：
    #   她提议了高危动作（关机/重启…）后，用户在 QQ 回「可以」才执行；
    #   「不要/算了」→ 作废；拿不准 → **不执行**，提议留着等 TTL（75s）自然过期。
    # ★ 2026-09-16 安全修复：以前 QQ 路径命中「关机」这类动作**连问都不问**直接 execute()
    #   （见下面 0.15），说一句「关机」就排了关机计划；现在高危动作一律先 propose 再问。
    #   （本函数是同步的、判定要 await，所以起个 task；拿不准时会顺手把消息交给主链路。）
    if _pending_confirm_busy():
        asyncio.create_task(_handle_pending_confirm(user_id, text))
        return
    # 0.15 ★ 电脑控制指令：QQ 里说「打开微信」「音量调到 30」「10 分钟后关机」
    #   → 直接执行电脑动作（QQ 遥控不询问）。必须排在 iOS 链路之前，
    #   否则「打开抖音」会被手机快捷指令抢走发到手机上。说「在手机上XX」走手机。
    #   ★ 2026-09-16：高危动作（关机/重启）**不再直接执行**——先 propose 问一句，
    #     用户回「可以」才动手（与 App 语音路径完全同一道闸门）。
    try:
        from . import control as _ctrl
        _tgt, _act = _ctrl.parse_direct_command_with_target(text)
        if _tgt == "pc" and _act:
            if _act.get("type") in _HIGH_RISK_PC_TYPES:
                async def _propose_high_risk():
                    _p = _ctrl.propose(_act, qq_session_id(), qq_character(), channel="text")
                    _ask = str((_p or {}).get("reply")
                               or "这是个大动作，回我一句「可以」我才会动手")
                    try:
                        await send_qq_message(_ask)
                        await _push_to_app(qq_session_id(), qq_character(), text, role="user")
                        await _push_to_app(qq_session_id(), qq_character(), _ask, role="assistant")
                    except Exception:
                        pass
                asyncio.create_task(_propose_high_risk())
                _qq_game_processed[user_id] = time.time()   # 主链路跳过（问询就是这次回复）
                return
            async def _run_pc_cmd():
                # ★ 真执行只从 run_pending_action 走（确认闸门的唯一出闸口，验收观测点）
                _r = await _ctrl.run_pending_action(_act, qq_session_id(), qq_character())
                _msg = str((_r or {}).get("reply") or "搞不定这个动作")
                try:
                    await send_qq_message(_msg)
                    # ★ 拦截发生在「推 user 气泡到 App」之前，这里补推保持聊天记录完整
                    await _push_to_app(qq_session_id(), qq_character(), text, role="user")
                    await _push_to_app(qq_session_id(), qq_character(), _msg, role="assistant")
                except Exception:
                    pass
            asyncio.create_task(_run_pc_cmd())
            _qq_game_processed[user_id] = time.time()   # 主链路跳过（控制结果就是回复）
            return
    except Exception as _pce:
        print(f"[OneBot] 电脑控制拦截失败: {_pce}", flush=True)
    # 0.2 ★ iOS 远程操控：QQ 里说「帮我在手机上打开抖音」→ 发邮件触发 iPhone 快捷指令
    try:
        from . import ios_bridge as _ios
        _ia, _ip = _ios.parse_ios_command(text)
        if _ia:
            async def _send_ios_cmd():
                _r = await _ios.send_remote_cmd(_ia, _ip)
                if _r.get("ok"):
                    print(f"[OneBot] iOS 指令已发邮件: {_ia}:{_ip}", flush=True)
                else:
                    print(f"[OneBot] iOS 指令下发失败: {_r.get('error')}", flush=True)
            asyncio.create_task(_send_ios_cmd())
    except Exception:
        pass
    # 0. 立即推 user 消息到 App（实时显示，不受防抖影响）
    asyncio.create_task(_push_to_app(qq_session_id(), qq_character(), text, role="user"))
    # 0.5 ★ 游戏大脑：无条件尝试推给游戏大脑（不再用 is_mc_command 判断只让"指令"走游戏）。
    #   骨子在线（ready 且 companion 非空）时，QQ 所有话都走游戏链路 → QQ 和游戏是同一个 AI；
    #   骨子没上线时 _dispatch_to_game 内部直接 return，由 QQ 主链路兜底普通聊天。
    try:
        asyncio.create_task(_dispatch_to_game(user_id, text))
    except Exception:
        pass
    # 1. 加入缓冲，重置防抖定时器
    _pending_texts.setdefault(user_id, []).append(text)
    if reply_id:
        _pending_reply.setdefault(user_id, []).append(reply_id)
    _reset_debounce(user_id)


async def _dispatch_to_game(user_id: str, text: str):
    """QQ 消息先过游戏陪伴执行器：只处理**明确的游戏指令**（挖矿/停下/回来/查状态等）。

    ★ 2026-09-07 切内置大脑模式：游戏内行为与聊天由 Numen 引擎负责（走 /v1 代理），
    执行器不再生成任何聊天回复。普通 QQ 聊天一律走主链路（人格+记忆生成）。
    执行器接手的指令会推确认/结果话术并设置 _qq_game_processed 标记，
    让主链路跳过，避免"确认话术 + 聊天回复"两条。
    """
    try:
        from .minecraft.game_bridge import get_game_brain
        brain = await get_game_brain()
        # ★ 骨子没上线（MCP 没开 / companion 未召唤）→ 直接返回，主链路兜底普通聊天
        if brain.ready and brain.companion:
            reply = await brain.handle_qq_command(text)
            if reply:
                await send_qq_message(reply)
                # 执行器已接手这条指令（动作确认/状态回答），主链路不再生成聊天
                _qq_game_processed[user_id] = time.time()
                return
    except Exception as e:
        print(f"[OneBot] 游戏执行器处理失败: {e}", flush=True)
    # ★ 星露谷陪伴执行器（Minecraft 未接手时才尝试；未启用 get_stardew_brain 返回 None 静默跳过）
    try:
        from .stardew.game_bridge import get_stardew_brain
        sb = await get_stardew_brain()
        if sb is not None:
            reply = await sb.handle_qq_command(text)
            if reply:
                await send_qq_message(reply)
                _qq_game_processed[user_id] = time.time()
    except Exception:
        pass


def _on_typing(user_id: str):
    """对方正在输入：重置防抖（等用户打完所有消息再回复）。"""
    if _pending_texts.get(user_id):
        _reset_debounce(user_id)


def _reset_debounce(user_id: str, delay: float = None):
    old = _pending_tasks.pop(user_id, None)
    if old and not old.done():
        old.cancel()
    _pending_tasks[user_id] = asyncio.create_task(_flush_reply(user_id, delay if delay else _debounce_sec()))


async def _flush_reply(user_id: str, delay: float = None):
    """防抖到期：合并缓冲的所有消息，统一生成并发送回复。"""
    global _conn
    try:
        await asyncio.sleep(delay if delay else _debounce_sec())
        texts = _pending_texts.pop(user_id, [])
        _pending_tasks.pop(user_id, None)
        if not texts:
            return
        # ★ 2026-09-07 内置大脑模式：骨子游戏在线时，QQ **聊天**照常走主链路
        #   （人格+记忆生成），只有明确游戏指令由执行器接手（见 _dispatch_to_game 的
        #   _qq_game_processed 标记）。旧的"在线 → 主链路整个跳过"逻辑已删——
        #   它是给"游戏大脑独占 QQ 回复"的外接模式设计的，现在会让聊天死寂。
        # ★ 旧标记检查（执行器已处理且回了话，30 秒内跳过——动作确认不用再聊一句）
        if _qq_game_processed.get(user_id, 0) > time.time() - 30:
            _qq_game_processed.pop(user_id, None)
            return
        user_raw = "\n".join(texts)
        combined = user_raw
        _sid = qq_session_id()
        _cid = qq_character()
        # ★ 引用：用户 reply 引用了某条 QQ 消息，拿到原文让 AI 读到
        _rids = _pending_reply.pop(user_id, [])
        if _rids:
            try:
                _rtxt = await _fetch_qq_msg_text(_conn, _rids[-1])
                if _rtxt:
                    combined = user_raw + "\n【用户引用的消息】" + _rtxt + "\n（这是用户引用/回复的那条消息原文，请围绕它回应）"
            except Exception:
                pass
        # ★ 定时提醒解析（2026-09-16 改为模型主判 → 正则兜底，见
        #   UI改版方案/_定时提醒误触发-根因与修法-2026-09-16.md）：
        #   旧版直接跑正则，把「到时候聊到两点半吧」当委托、抽出残尾「半吧」建任务；
        #   现在判断权交给模型（与她同一套承诺提取口径），正则只在她不可用时兜底。
        #   命中 → 建任务 + 确认话术（无明确事项时是反问句），
        #   设置 _qq_game_processed 让主链路跳过生成（不走 LLM，省一次调用）。
        try:
            from . import chat_logic as _cl_timed
            _timed = await _cl_timed.resolve_timed_reminder(
                combined, character_name=_cid, session_id=_sid, character_id=_cid)
            if _timed:
                # ★ 2026-09-16：QQ 这条同样优先用她自己的话说（事实由 chat_logic 校验，
                #   模型不可用/说错 → 退回原模板句）
                _tk_reply = await _cl_timed.build_reminder_reply_voiced(
                    _timed, "qq", character_name=_cid, character_id=_cid, session_id=_sid)
                await send_qq_message(_tk_reply)
                try:
                    await _push_to_app(_sid, _cid, _tk_reply, role="assistant")
                except Exception:
                    pass
                try:
                    from . import db as _db_tk
                    _db_tk.add_message(_sid, "assistant", _tk_reply, _cid)
                except Exception:
                    pass
                _qq_game_processed[user_id] = time.time()
                return
        except Exception as _timed_err:
            print(f"[OneBot] 定时提醒解析失败(静默): {_timed_err}", flush=True)
        # ★ 音乐分享（纯模型驱动）：告诉 AI 可用 [SONG] 标记点歌，语义判断/选歌全交给模型。
        #   支持模糊表达（"一起听/想让你陪"）与截图场景（视觉描述已拼进 combined）。
        #
        # ★★★ 2026-09-12 重要修复：这两段能力说明原来是用
        #       combined = combined + "…【音乐分享能力】…"
        #     拼进**用户消息**里的（combined 就是这条 user turn 的 content）。
        #     后果有两层，都很严重：
        #       1) 模型要"回应"这两段说明 —— 本地 4B（qwen3:4b）分不清"指令"和
        #          "用户说的话"，直接开始答非所问；云端 glm-5.3-flash 靠自身能力
        #          勉强扛住，所以这个 bug 一直没被发现。
        #       2) 更糟的是它**连同说明一起写进 chat_history**（实测 #9141：
        #          用户只打了「你在说啥呀」，落库的 user 消息后面跟着 400 字说明），
        #          之后每轮都被当历史喂回去，污染永久化。
        #     现在改成挂到 **system** 消息上（在 _generate_reply_turns 里注入），
        #     用户消息保持干净。
        _capability_extra = (
            "\n\n【音乐分享能力】如果用户想听歌、想让你陪TA一起听，或此刻很适合放首歌，"
            "你可以在回复的**最后单独一行**点歌（只在末尾，只写一行）：\n"
            "[SONG]歌名 歌手[/SONG]\n"
            "例：[SONG]晴天 周杰伦[/SONG]。点歌后会以网易云卡片发给TA。不需要就别写这行。"
        )
        # ★ 语音条（2026-09-11）：给她一个"主动发语音"的显式开关，取代原来靠关键词猜。
        #   只在 QQ 注入（App 那边用户索要语音走 /api/voice/message）。
        _capability_extra += (
            "\n\n【发语音】绝大多数时候用文字。只有当你**真的很想用声音说**"
            "（撒娇、哄睡、说句心里话，或用户想听你声音）时，才把要说的话整段用标记包起来：\n"
            "[VOICE]你要说的话[/VOICE]\n"
            "写了这个标记，这一轮就**只发一条语音条**，标记里的文字不会再单独发一遍，"
            "所以别在标记外重复同样的话。不需要语音就完全不要写这个标记，也别提\"我发语音\"。"
        )
        # ★ 优先用 App 同款 once 生成器（QQ 与 App 气泡/条数/顺序一致，且更快）；
        #   失败时回退旧逻辑（一次 chat_once + _split_reply）。
        _segs = await _generate_reply_turns(user_id, combined,
                                            capability_extra=_capability_extra)
        if _segs:
            # once 生成器已产出多段，落库（user 一条 + assistant 逐段）
            try:
                from . import db as _db
                _db.add_message(_sid, "user", user_raw, _cid, {"source": "qq"})
                for _seg in _segs:
                    _clean_seg = re.sub(r"\[SONG\].*?\[/SONG\]", "", str(_seg), flags=re.I | re.S)
                    # ★ [VOICE] 标记只留内容、不留标签（否则历史记录/App 里会显示 [VOICE]）
                    _clean_seg = re.sub(r"\[/?voice\]", "", _clean_seg, flags=re.I).strip()
                    if _clean_seg:
                        _db.add_message(_sid, "assistant", _clean_seg, _cid, {"source": "qq"})
            except Exception:
                pass
        else:
            # ★ 诊断：once 生成器返回空，先打日志再回退旧逻辑，方便定位中转站哪一步失败
            print(f"[OneBot] _generate_reply_turns 返回空，回退旧逻辑。text={combined[:80]!r}", flush=True)
            reply = await _generate_reply(user_id, combined)
            if not reply:
                # ★ 兜底：两条路径都失败时，至少发一条消息，不让 AI 完全沉默
                print(f"[OneBot] 生成全部失败(once+旧逻辑)，发送兜底回复。text={combined[:80]!r}", flush=True)
                reply = "（我刚刚想事情卡住了，你再说一次好不好～）"
            _segs = _split_reply(reply, _reply_max_seg(_sid, _cid))
        # ★ 解析模型点歌标记 [SONG]歌名[/SONG]：提取 → 搜网易云 → 清理标记 → 发送后发卡片
        _pending_song = None
        _song_requested = False
        try:
            from .music_share import pick_song as _music_pick
            _clean_segs = []
            for _s in (_segs or []):
                _sm = re.search(r"\[SONG\](.*?)\[/SONG\]", str(_s), re.I | re.S)
                _s_clean = re.sub(r"\[SONG\].*?\[/SONG\]", "", str(_s), flags=re.I | re.S).strip()
                if _sm and _pending_song is None:
                    _kw = _sm.group(1).strip()
                    if _kw:
                        _song_requested = True
                        _song = await asyncio.to_thread(_music_pick, _kw)
                        if _song and _song.get("id"):
                            _pending_song = _song
                            print(f"[OneBot] 模型点歌: {_kw} -> {_song.get('name')}", flush=True)
                if _s_clean:
                    _clean_segs.append(_s_clean)
            if _clean_segs and len(_clean_segs) != len(_segs):
                _segs = _clean_segs
        except Exception as _pe:
            print(f"[OneBot] 点歌解析失败(静默): {_pe}", flush=True)
        # ★ 执行闭环：模型点了歌但没搜到 → 回喂失败（下一轮纠正「说≠做」）
        if _song_requested and _pending_song is None:
            try:
                from . import execution_feedback as _ef
                _ef.report("点歌", False, "没搜到这首歌")
            except Exception:
                pass
        ws = _conn
        if ws is None:
            return
        # 1. 分段回复 QQ（每段一个气泡，模拟真人连发）
        # ★ 引用决策：用户连发多条时，让模型判断该引用哪条；
        #   单条消息直接引用。避免"AI 回复第一条内容却引用最后一条消息"的错乱。
        _reply_mid = ""
        mids = _pending_mids.pop(user_id, [])
        if mids:
            if len(texts) >= 2:
                # 连发多条：调用模型判断该引用哪条
                try:
                    from .deepseek_api import chat_once
                    from . import chat_logic, config as _cfg
                    _q_model = chat_logic.pick_model(None, True, _cid)
                    _q_key = _cfg.api_key_for_model(_q_model)
                    if _q_key and _q_model:
                        numbered = "\n".join(f"{i+1}. {u[:120]}" for i, u in enumerate(texts))
                        prompt = (
                            f"用户刚刚连续发了 {len(texts)} 条消息，你准备一次性回应。\n"
                            "下面是这几条消息（按先后顺序编号：1 是最早那条，"
                            f"{len(texts)} 是最后那条）：\n" + numbered + "\n\n"
                            "你会在第一条回复里引用其中一条消息来针对性回应。"
                            "请判断：哪条消息值得被引用（重要到需要单独回应）？\n"
                            "如果都不重要（只是日常寒暄/随口一说），输出 0，不要硬选。\n"
                            "只输出一个数字（0 或 1~" + str(len(texts)) + "），不要任何解释。"
                        )
                        raw = await asyncio.wait_for(
                            chat_once(_q_model, [{"role": "user", "content": prompt}], _q_key,
                                      temperature=0.4, max_tokens=8),
                            timeout=15,
                        )
                        import re as _re
                        _m = _re.search(r"\d+", str(raw or ""))
                        if _m:
                            _idx = int(_m.group(0))
                            if 1 <= _idx <= len(mids):
                                _reply_mid = mids[_idx - 1]
                except Exception as _qe:
                    print(f"[OneBot] 引用决策失败(静默): {_qe}", flush=True)
            if not _reply_mid and mids:
                # 单条消息 或 模型没选：引用用户最后那条
                _reply_mid = mids[-1]
        # ─────────────────────────────────────────────────────────────
        # ★ 2026-09-11 语音意图重做。
        #   原来是对**她自己的回复**做子串匹配（"语音"/"听我说"/"说给你听"…），
        #   实测误触发：用户说「想我没」→ 她回「你突然这么问，是想听我说想吧？」
        #   命中 "听我说" → 4 段文字全被转成语音发了一遍；她聊到「语音输出」也命中。
        #   而且文字先发一遍、语音再发一遍，同一句话在 QQ 上收到两遍。
        #   现在只有两个来源：
        #     (a) 用户明确索要 —— 只匹配**用户自己的话**（user_raw），不看她的回复
        #     (b) 她自己写的显式标记 [VOICE]…[/VOICE] —— 同点歌 [SONG] 的机制，
        #         由提示词教她"真想发语音时才写"，不靠猜关键词
        #   并且：该发语音时**只发语音、不发文字**（语音条就是这一条消息）。
        # ─────────────────────────────────────────────────────────────
        _voice_intent = False
        try:
            _voice_intent = any(k in str(user_raw) for k in (
                "发语音", "发条语音", "发声音", "想听你声音", "想听你的声音",
                "语音回我", "语音回复", "说句话给我听", "听你的声音",
            ))
            if any("[voice" in str(_s).lower() for _s in (_segs or [])):
                _voice_intent = True
                # 标记本身不能被念出来、也不能发出去
                _segs = [re.sub(r"\[/?voice\]", "", str(_s), flags=re.I).strip() for _s in _segs]
                _segs = [_s for _s in _segs if _s]
        except Exception as _vi_e:
            print(f"[OneBot] 语音意图判定失败(静默): {_vi_e}", flush=True)

        _sent_voice = False
        if _voice_intent and _segs:
            _voice_text = " ".join(str(_s) for _s in _segs).strip()
            if _voice_text:
                try:
                    from . import tts as _tts
                    from .character_manager import resolve_character_voice_cfg as _resolve_voice
                    _vcfg = _resolve_voice(_cid, infer=True) or {}
                    _vurl = await _tts.generate_audio(
                        _voice_text, _vcfg, base_url="http://127.0.0.1:32123")
                    if _vurl:
                        _sent_voice = await send_qq_voice("", _vurl)
                        if _sent_voice:
                            # 语音条也要让 App 看到这轮回复（跳过文字循环后 App 否则收不到）
                            await _push_to_app(_sid, _cid, _voice_text, ts=int(time.time() * 1000))
                except Exception as _ve:
                    print(f"[OneBot] 语音发送失败(静默): {_ve}", flush=True)
            if not _sent_voice:
                # 语音没发出去 → 回退发文字，不能让她这轮彻底沉默
                print("[OneBot] 语音未发出，回退文字", flush=True)
                _voice_intent = False

        _push_ts = int(time.time() * 1000)
        for _i, _seg in enumerate([] if _voice_intent else _segs):
            # ★ 显式递增 ts：保证 App 端气泡顺序与 QQ 发送顺序一致
            #   （此前靠前端 Date.now()，多段挤在同一秒会乱序，2026-09-08 用户实测）
            # ★ 解析 [sticker:文件名] 标记 → 每张图单独一条消息发（2026-09-13）。
            #   原来文字+图片段塞在同一个 send_private_msg 里，QQ 把它们渲染进
            #   同一个气泡，表情包就黏在对话框里；拆开后文字一条、每张图一条，
            #   跟真人发图一样。
            _seg_text = str(_seg)
            _sticker_fns = []
            try:
                from . import sticker_manager as _sm
                _stickers = _sm.parse_sticker_markers(_seg)
                if _stickers:
                    print(f"[StickerDebug] 发送循环解析到 {len(_stickers)} 个表情包: {[s['filename'] for s in _stickers]}", flush=True)
                _seg_text = _sm.strip_sticker_markers(_seg)
                # ★ 去重换新（2026-09-08 用户反馈"叫多发只重发同一张"）：
                #   同一轮里不重复同一张；刚发过的也尽量用 pick_for_context 换没发过的
                _recent = _recent_stickers.get(user_id, [])
                _sent_this = []
                for _st in _stickers:
                    _fname = _st["filename"]
                    if _fname in _recent or _fname in _sent_this:
                        try:
                            _alt = _sm.pick_for_context(_seg_text or str(_seg), "", exclude=_fname)
                            if _alt and _alt.get("filename") and _alt["filename"] not in _recent and _alt["filename"] not in _sent_this:
                                _fname = _alt["filename"]
                                print(f"[StickerDebug] 去重换新: -> {_fname}", flush=True)
                        except Exception:
                            pass
                    _sent_this.append(_fname)
                    _sticker_fns.append(_fname)
                if _sent_this:
                    _recent_stickers[user_id] = (_recent + _sent_this)[-6:]
            except Exception:
                pass
            # ★ 解析 [sd:动作] 标记 → 真执行星露谷动作（聊天里「说要做」=「游戏里真做」）
            if "[sd" in str(_seg_text).lower():
                try:
                    from .stardew.game_bridge import get_stardew_brain as _gsb
                    _sbr = await _gsb()
                    if _sbr is not None:
                        _seg_text = await _sbr.consume_markers(_seg_text)
                except Exception:
                    pass

            async def _send_qq_arr(_arr):
                await ws.send_text(json.dumps({
                    "action": "send_private_msg",
                    "params": {
                        "user_id": int(user_id) if user_id.isdigit() else user_id,
                        "message": _arr,
                    },
                    "echo": f"reply_{user_id}_{int(time.time() * 1000)}",
                }, ensure_ascii=False))

            _has_text = bool(str(_seg_text or "").strip())
            _reply_seg = (
                {"type": "reply", "data": {"id": _reply_mid}}
                if (_i == 0 and _reply_mid) else None
            )
            # 1) 文字（首段带引用）单独一条消息
            if _has_text:
                _text_arr = []
                if _reply_seg:
                    _text_arr.append(_reply_seg)
                _text_arr.append({"type": "text", "data": {"text": str(_seg_text).strip()}})
                try:
                    await _send_qq_arr(_text_arr)
                except Exception as e:
                    print(f"[OneBot] 发送失败: {e}", flush=True)
                # App 端同步：文字段推剥掉标记后的纯文本
                await _push_to_app(_sid, _cid, str(_seg_text).strip(), ts=_push_ts)
                _push_ts += 1
                await asyncio.sleep(0.3)
            # 2) 每张表情包单独一条消息（独立气泡，不跟文字黏一起）
            for _sidx, _fname in enumerate(_sticker_fns):
                _img_seg = _sticker_image_segment(_fname)
                if not _img_seg:
                    print(f"[StickerDebug] 图片段生成失败: {_fname}", flush=True)
                    try:
                        from . import execution_feedback as _ef
                        _ef.report("发表情包", False, f"图片「{_fname}」没找到")
                    except Exception:
                        pass
                    continue
                _st_arr = [_img_seg]
                # 纯表情包首段也要带引用（旧版行为：引用+图一条）
                if _reply_seg and _sidx == 0 and not _has_text:
                    _st_arr.insert(0, _reply_seg)
                try:
                    await _send_qq_arr(_st_arr)
                    # ★ 执行闭环：发图成功则回喂成功结果（下一轮给模型看）
                    try:
                        from . import execution_feedback as _ef
                        _ef.report("发表情包", True)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[OneBot] 发送失败: {e}", flush=True)
                # App 端同步：推标记行，前端把它渲染成独立表情包气泡
                await _push_to_app(_sid, _cid, f"[sticker:{_fname}]", ts=_push_ts)
                _push_ts += 1
                await asyncio.sleep(0.3)
            if not _has_text and not _sticker_fns:
                continue
        # 2.5 QQ 语音：已在上面的语音意图判定里处理（该发语音时只发语音、不发文字）。
        #     旧的"关键词逐段转语音"逻辑已移除 —— 它会误触发，且让同一句话收两遍。
        # 2.8 QQ 点歌：如果用户点歌且搜到歌，AI 回复后附一张网易云音乐卡片
        if _pending_song:
            try:
                _sent_ok = await send_qq_music_card(_pending_song)
                # ★ 修复：之前不检查返回值，白名单空时 send 静默 return False 也打
                #   「已发音乐卡片」——日志假象导致「推送音乐没了」排查绕远路
                if _sent_ok:
                    print(f"[OneBot] 已发音乐卡片: {_pending_song.get('name')}", flush=True)
                else:
                    print(f"[OneBot] 音乐卡片未发出(白名单空/连接断): {_pending_song.get('name')}", flush=True)
                # ★ 执行闭环：点歌成功也回喂（下一轮给模型看）
                try:
                    from . import execution_feedback as _ef
                    _ef.report("点歌", bool(_sent_ok), "" if _sent_ok else "卡片未送达")
                except Exception:
                    pass
            except Exception as _se:
                print(f"[OneBot] 音乐卡片发送失败(静默): {_se}", flush=True)
                try:
                    from . import execution_feedback as _ef
                    _ef.report("点歌", False, str(_se)[:60])
                except Exception:
                    pass
        # 3. 记忆抽取：QQ 对话也要积累记忆（复用 App 的 maybe_auto_extract 闭环）
        # ★ 与 App 对齐：走 count_round 轮次门控，避免每轮都提炼浪费 LLM 调用
        try:
            from . import chat_logic as _cl
            _ai_text = "\n".join(_segs) if _segs else ""
            if _cl.count_round(_sid, combined, _ai_text, _cid):
                asyncio.create_task(_cl.maybe_auto_extract(_sid, _cid, combined))
        except Exception as e:
            print(f"[OneBot] 记忆抽取调度失败(静默): {e}", flush=True)
        # 3.5 AI 承诺提取：QQ 对话里 AI 说的承诺（等你发完/八点半喊你等）也要记录并兑现。
        #     每轮都试（内部有粗筛，非承诺零成本），与 App 的承诺闭环对齐。
        try:
            from . import ai_promise as _ap
            _ai_full = "\n".join(_segs) if _segs else ""
            if _ai_full:
                # ★ 显式传 user_raw：委托类承诺（「六点半叫我」）源头在用户消息，
                #   不依赖函数内部再去查库兜底
                asyncio.create_task(_ap.extract_and_store(_sid, _cid, _ai_full, user_raw))
        except Exception as e:
            print(f"[OneBot] 承诺提取调度失败(静默): {e}", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[OneBot] flush 异常: {e}", flush=True)


async def _handle_voice_message(ws: WebSocket, user_id: str):
    """语音/媒体消息：友好提示（暂不支持转写/看视频）。"""
    reply = "我现在还听不了语音和视频，发文字跟我说吧～"
    try:
        await ws.send_text(json.dumps({
            "action": "send_private_msg",
            "params": {
                "user_id": int(user_id) if user_id.isdigit() else user_id,
                "message": [{"type": "text", "data": {"text": reply}}],
            },
            "echo": f"voice_{user_id}_{int(time.time() * 1000)}",
        }, ensure_ascii=False))
    except Exception as e:
        print(f"[OneBot] 语音提示发送失败: {e}", flush=True)


async def handle_onebot_ws(ws: WebSocket):
    global _conn
    await ws.accept()
    _conn = ws
    print("[OneBot] NapCat 已连接", flush=True)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:
                continue
            # get_msg 响应（引用消息拿原文用）
            _echo = data.get("echo")
            if _echo and str(_echo).startswith("getmsg_"):
                _fut = _pending_getmsg.pop(str(_echo), None)
                if _fut and not _fut.done():
                    try:
                        _fut.set_result(data)
                    except Exception:
                        pass
                continue
            if data.get("post_type") == "message" and data.get("message_type") == "private":
                user_id = str(data.get("user_id") or "").strip()
                if not user_id:
                    continue
                _allowed = _allowed_users()
                if _allowed and user_id not in _allowed:
                    print(f"[OneBot] 忽略非白名单私聊 {user_id}", flush=True)
                    continue
                _mid = str(data.get("message_id") or "").strip()
                if _mid:
                    _last_qq_msg_id[user_id] = _mid
                    _pending_mids.setdefault(user_id, []).append(_mid)
                _reply_id = _extract_reply_id(data.get("message"))
                text = _extract_text(data.get("message"))
                _raw_msg = data.get("message")
                # ★ 图片处理：提取图片 URL → 视觉模型分析 → 拼进用户消息
                _img_urls = _extract_image_urls(_raw_msg)
                if _img_urls:
                    # 异步分析图片，把描述拼进文字
                    _img_desc = await _analyze_images(_img_urls)
                    if text:
                        text = text + "\n" + _img_desc
                    else:
                        text = _img_desc
                if text:
                    _handle_user_message(user_id, text, reply_id=_reply_id)
                elif _is_voice_message(_raw_msg) or _is_unsupported_media(_raw_msg):
                    asyncio.create_task(_handle_voice_message(ws, user_id))
            elif data.get("post_type") == "notice":
                if data.get("sub_type") == "input_status":
                    uid = str(data.get("user_id") or "").strip()
                    # ★ NapCat 用 event_type(1=开始输入,2=停止输入)+status_text；标准 OneBot 用 status(0/1)
                    _evt = data.get("event_type")
                    _status = data.get("status")
                    _status_text = data.get("status_text")
                    # 只要收到输入状态变化（开始或停止），都重置防抖，等用户打完再回复
                    if uid and (_evt is not None or _status is not None or _status_text):
                        _on_typing(uid)
    except Exception as e:
        print(f"[OneBot] 连接断开: {e}", flush=True)
    finally:
        _conn = None
