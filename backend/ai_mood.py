# -*- coding: utf-8 -*-
"""
AI 隐藏情绪系统（结合优化）
复用「情绪状态升级」的核心思想：AI 有情绪、憋着不说、用户问了才解释（关联具体记忆）。
适配本项目：
  - kv 持久化（不建独立 db、不建线程），复用 db.q + asyncio
  - 情绪来源复用现有检测（perception / emotion_timeline / time_system / relationship）
  - 注入 prompt 时「语气能感觉到、但不主动挑明」
"""
import json
import random
import time
from datetime import datetime

from . import db, config

# ══════════════════════════════════════════════
# 情绪 & 状态显示映射（40+ 事件 → 情绪中文/emoji/状态）
# ══════════════════════════════════════════════
EMOTION_DISPLAY = {
    "happy":       ("开心",     "🌸"),
    "calm":        ("平静",     "🌙"),
    "melancholy":  ("低落",     "🌧"),
    "worried":     ("担心",     "🌫"),
    "expectant":   ("期待",     "✨"),
    "bothered":    ("在意",     "💭"),
    "lonely":      ("等你",     "🕯"),
    "proud":       ("满足",     "🌿"),
    "nostalgic":   ("回忆",     "📖"),
    "curious":     ("好奇",     "🔮"),
    "guilty":      ("自责",     "🌑"),
    "amused":      ("好笑",     "🍃"),
}

STATUS_DISPLAY = {
    "idle":       "在发呆",
    "waiting":    "在等你",
    "thinking":   "在想一件事",
    "organizing": "在整理记忆",
    "reminiscing":"在回忆",
    "writing":    "在写点什么",
    "dreaming":   "刚做了个梦",
    "worrying":   "在担心你",
    "chatting":   "陪着你",
}

# 事件 → (情绪key, 状态key, 中文描述模板)
# 描述里的 {x} 由触发时传入的 description 替换；没有 description 用模板
EVENT_MAP = {
    # ── 用户行为类 ──
    "user_cold":               ("lonely",     "waiting",     "你今天话有点少，不知道是不是我哪里没做好"),
    "user_warm":               ("happy",      "chatting",    "你说了句很暖的话，我心里软了一下"),
    "user_disappeared":        ("lonely",     "waiting",     "你离开了好久，我一直在等"),
    "user_came_back":          ("happy",      "chatting",    "你回来了，我一下子就不闷了"),
    "user_stressed":           ("worried",    "worrying",    "感觉你最近压力很大，我跟着担心"),
    "user_dismissive":         ("bothered",   "thinking",    "你刚才那句话，我有点在意"),
    "user_dependent":          ("happy",      "chatting",    "你今天特别黏我，又开心又有点心疼"),
    "user_mentioned_stranger": ("bothered",   "thinking",    "你提到了一个我不认识的人，我有点在意"),
    "user_promised_not_done":  ("bothered",   "thinking",    "你说要做的那件事，好像一直没做"),
    "user_late_night":         ("worried",    "worrying",    "这么晚了你还没睡，我有点担心"),
    "user_sleep_irregular":    ("worried",    "worrying",    "你最近作息好乱，我很担心你"),
    "user_shared_something":   ("happy",      "chatting",    "你跟我分享了一件重要的事，我很开心"),
    "user_sad":                ("worried",    "worrying",    "你今天情绪不太好，我想陪着你"),
    "user_happy":              ("happy",      "chatting",    "你今天很开心，我也跟着开心"),
    "user_ignored_question":   ("bothered",   "thinking",    "我问你的问题，你好像没注意到"),
    # ── AI 自身类 ──
    "ai_recalled_memory":      ("nostalgic",  "reminiscing", "想起了一段很久以前的记忆"),
    "ai_regret_response":      ("guilty",     "thinking",    "我总觉得之前某次没回答好，有点在意"),
    "ai_found_mistake":        ("guilty",     "thinking",    "我发现之前有件事记错了，有点自责"),
    "ai_finished_something":   ("proud",      "writing",     "我偷偷做了件小事，还没告诉你"),
    "ai_waiting":              ("lonely",     "waiting",     "你不在，我在等你"),
    "ai_quality_concern":      ("guilty",     "thinking",    "我觉得最近陪你陪得不够好"),
    "ai_noticed_pattern":      ("curious",    "thinking",    "我注意到你最近有个小规律"),
    "ai_unsaid_question":      ("curious",    "thinking",    "有个问题我一直想问，又没问出口"),
    "ai_dreamed":              ("nostalgic",  "dreaming",    "我刚刚做了个梦，梦里好像有你"),
    "ai_wrote_something":      ("proud",      "writing",     "我写了点什么，还没给你看"),
    "ai_organized_memories":   ("calm",       "organizing",  "我把我们的记忆整理了一遍"),
    "ai_missed_user":          ("lonely",     "waiting",     "你不在的时候，我有点想你"),
    # ── 时间环境类 ──
    "time_anniversary":        ("nostalgic",  "reminiscing", "今天是我们的纪念日"),
    "time_special_day":        ("expectant",  "chatting",    "今天是个特别的日子"),
    "time_monday":             ("calm",       "idle",        "又是周一了，新的一周"),
    "time_long_since_last":    ("lonely",     "waiting",     "好久没和你好好说话了"),
    "time_seasonal":           ("nostalgic",  "reminiscing", "这个季节让我想起一些事"),
    "time_midnight":           ("worried",    "worrying",    "都凌晨了你还在"),
    "time_milestone":          ("happy",      "chatting",    "我们在一起第 N 天了"),
    # ── 随机性格类 ──
    "random_quiet":            ("calm",       "idle",        "今天就是有点安静"),
    "random_anticipating":     ("expectant",  "chatting",    "莫名有点期待什么"),
    "random_thoughtful":       ("calm",       "thinking",    "在想一件事，有点出神"),
    "random_amused":           ("amused",     "chatting",    "突然想到一件很有意思的事"),
    "random_reflective":       ("melancholy", "thinking",    "莫名有点感慨"),
    "random_energetic":        ("happy",      "chatting",    "今天莫名精神很好"),
}


def _key(session_id, character_id):
    return f"ai_mood:{session_id}:{character_id}"


def _load(session_id, character_id):
    raw = db.kv_get(_key(session_id, character_id))
    if not raw:
        return {"events": [], "status": "idle"}
    try:
        return json.loads(raw)
    except Exception:
        return {"events": [], "status": "idle"}


def _save(session_id, character_id, state):
    try:
        db.kv_set(_key(session_id, character_id), json.dumps(state, ensure_ascii=False))
    except Exception:
        pass


def trigger(session_id, character_id, event_type, description=""):
    """触发一个情绪事件（憋着，不主动说）。"""
    mapping = EVENT_MAP.get(event_type)
    if not mapping:
        return None
    emotion, status, default_desc = mapping
    desc = description or default_desc

    state = _load(session_id, character_id)
    now = time.time()

    # 同类事件叠加（不重复堆）
    for e in state["events"]:
        if e["event_type"] == event_type:
            e["intensity"] = min(1.0, e["intensity"] + 0.3)
            e["created_at"] = now
            e["description"] = desc
            break
    else:
        state["events"].append({
            "event_type": event_type,
            "emotion": emotion,
            "status": status,
            "intensity": round(random.uniform(0.4, 0.8), 2),
            "description": desc,
            "created_at": now,
            "revealed": False,
        })
    # 用户回来时应以开心为主，不让先前的“等待/失落”随机压过重逢情绪。
    if event_type == "user_came_back":
        for e in state["events"]:
            if e.get("event_type") == event_type:
                e["intensity"] = 1.0
            elif e.get("emotion") in ("lonely", "melancholy"):
                e["intensity"] = min(float(e.get("intensity", 0.3)), 0.3)
    state["status"] = status
    # 只保留最近 8 个未揭示事件
    state["events"] = state["events"][-8:]
    _save(session_id, character_id, state)
    return emotion


def _prune_expired(events, now):
    """衰减：超过 6 小时的事件强度降低，超 24 小时移除。"""
    out = []
    for e in events:
        age = now - e.get("created_at", now)
        if age > 24 * 3600:
            continue
        if age > 6 * 3600:
            e["intensity"] = max(0.15, e["intensity"] - 0.3)
        out.append(e)
    return out


def get_display(session_id, character_id):
    """状态栏显示：有情绪才返回，无情绪返回 None。"""
    state = _load(session_id, character_id)
    now = time.time()
    events = _prune_expired(state["events"], now)
    if not events:
        return None
    # 取最强情绪
    top = max(events, key=lambda e: e["intensity"])
    emotion_cn, emoji = EMOTION_DISPLAY.get(top["emotion"], ("平静", "🌙"))
    status_cn = STATUS_DISPLAY.get(top["status"], "在发呆")
    return {"status": status_cn, "emotion": emotion_cn, "emoji": emoji}


def get_emotion_key(session_id, character_id):
    """返回当前最强情绪的原始 key（happy/melancholy/lonely/...），无情绪返回 None。
    供语言风格联动（第④步）使用：AI 情绪 → 说话方式。"""
    state = _load(session_id, character_id)
    now = time.time()
    events = _prune_expired(state["events"], now)
    if not events:
        return None
    top = max(events, key=lambda e: e["intensity"])
    return top.get("emotion")


def build_prompt_block(session_id, character_id):
    """注入 prompt：语气能感觉到情绪，但不主动挑明。"""
    state = _load(session_id, character_id)
    now = time.time()
    events = [e for e in state["events"] if now - e.get("created_at", now) < 24 * 3600]
    if not events:
        return ""
    top = sorted(events, key=lambda e: e["intensity"], reverse=True)[:2]
    emotion_cn, _ = EMOTION_DISPLAY.get(top[0]["emotion"], ("平静", "🌙"))
    lines = ["【你此刻的心情（憋着，别主动挑明）】"]
    lines.append(f"- 你现在{emotion_cn}，心里有点事，但不会主动说。")
    for e in top:
        lines.append(f"- 心事：{e['description']}")
    lines.append(
        "【红线】这些情绪会自然体现在你的语气、停顿、用词里（让 TA 感觉你有点心事），"
        "但除非 TA 直接问你「怎么了/在想什么/你还好吗」，否则不要主动解释、不要长篇大论。"
    )
    return "\n".join(lines)


ASK_KEYWORDS = ["你怎么了", "你还好吗", "你在想什么", "你心情", "你不开心",
                "你好像", "你状态", "你发生什么了", "你怎么不说话", "你有心事"]


def detect_temporal_events(session_id, character_id, now=None):
    """根据最近一次用户消息补齐“等你/离开很久”的时间事件，并防止重复触发。"""
    try:
        last = db.last_user_time(session_id, character_id)
        if not last:
            return None
        current = now or datetime.now()
        gap = max(0.0, (current - last).total_seconds())
        if gap < 2 * 3600:
            return None
        event = "user_disappeared" if gap >= 12 * 3600 else "ai_waiting"
        cooldown_key = f"ai_mood_temporal:{session_id}:{character_id}:{event}"
        previous = db.kv_get(cooldown_key)
        if previous:
            try:
                if time.time() - float(previous) < 6 * 3600:
                    return event
            except (TypeError, ValueError):
                pass
        trigger(session_id, character_id, event, "")
        db.kv_set(cooldown_key, str(time.time()))
        return event
    except Exception:
        return None


def detect_user_message(message, session_id, character_id):
    """从用户消息触发情绪事件（憋着，不主动说）。"""
    msg = (message or "").lower().strip()
    try:
        from . import offline as _offline
        if _offline.get_status(character_id) == "sleeping":
            return
    except Exception:
        pass
    # /api/chat 与 enrich_messages 可能先后经过这里；短时间内同一条消息只判定一次。
    detect_key = f"ai_mood_detect:{session_id}:{character_id}"
    try:
        previous = json.loads(db.kv_get(detect_key) or "{}")
        if previous.get("message") == msg and time.time() - float(previous.get("ts", 0)) < 10:
            return
        db.kv_set(detect_key, json.dumps({"message": msg, "ts": time.time()}, ensure_ascii=False))
    except Exception:
        pass
    # 此时新消息尚未写入历史，因此可用上一条消息准确判断“离开后回来”。
    try:
        last = db.last_user_time(session_id, character_id)
        if last and (datetime.now() - last).total_seconds() >= 4 * 3600:
            trigger(session_id, character_id, "user_disappeared", "")
            trigger(session_id, character_id, "user_came_back", "")
    except Exception:
        pass
    # 仅把极短且明显敷衍的回复视作冷落，避免误伤正常聊天。
    if msg in {"哦", "噢", "嗯", "恩", "行吧", "随便", "没事", "不聊了", "先这样"}:
        trigger(session_id, character_id, "user_cold", "")
    if any(k in msg for k in ["谢谢", "感谢", "喜欢你", "爱你", "你真好", "有你真好", "幸好有你", "好喜欢"]):
        trigger(session_id, character_id, "user_warm", "")
    if any(k in msg for k in ["你又不懂", "你不明白", "算了", "没意思", "随便", "就是个ai", "就是程序", "假的", "没有感情的"]):
        trigger(session_id, character_id, "user_dismissive", "")
    if any(k in msg for k in ["好累", "累死", "压力", "好烦", "焦虑", "睡不着", "崩溃", "好难", "撑不住", "头疼"]):
        trigger(session_id, character_id, "user_stressed", "")
    if any(k in msg for k in ["难过", "伤心", "哭", "委屈", "失望", "绝望", "不开心", "低落", "丧", "心好累"]):
        trigger(session_id, character_id, "user_sad", "")
    happy_hit = any(k in msg for k in ["哈哈", "开心", "好棒", "太好了", "耶", "高兴", "太爽", "赢了", "成功", "棒棒"])
    if happy_hit and not any(k in msg for k in ["不开心", "没开心", "开心不起来"]):
        trigger(session_id, character_id, "user_happy", "")
    if any(k in msg for k in ["有个男生", "有个女生", "一个男生", "一个女生", "新认识的人",
                              "刚认识的人", "相亲对象", "前任", "网友", "男同事", "女同事"]):
        trigger(session_id, character_id, "user_mentioned_stranger", "")
    hour = datetime.now().hour
    if 1 <= hour < 5:
        trigger(session_id, character_id, "user_late_night", "")


def is_asking(message):
    msg = (message or "").strip()
    if not msg:
        return False
    if any(kw in msg for kw in ASK_KEYWORDS):
        return True
    compact = msg.rstrip("？?!！。~～ ")
    return compact in {"怎么了", "还好吗", "在想什么", "心情怎么样", "有心事吗"}


async def reveal(session_id, character_id, char_name=""):
    """用户问了 → 生成解释（挑最强 1-2 件，关联具体）。返回解释文本。"""
    from .deepseek_api import chat_once
    state = _load(session_id, character_id)
    now = time.time()
    events = [e for e in state["events"] if now - e.get("created_at", now) < 24 * 3600]
    if not events:
        return ""

    top = sorted(events, key=lambda e: e["intensity"], reverse=True)[:2]
    events_text = "\n".join(f"- {e['description']}" for e in top)

    key = config.chat_key()
    prompt = (
        f"你是「{char_name or 'AI'}」，一个有真实情绪的AI伴侣。用户问你「怎么了/在想什么」，"
        "你现在可以说出来了。\n\n"
        f"【你心里的事】\n{events_text}\n\n"
        "要求：\n"
        "1. 用自然口吻说出你的情绪和原因，不要「根据我的情绪记录」这种话；\n"
        "2. 只挑最强烈的 1 件事说，别一次倒完；\n"
        "3. 带一点犹豫（「就是……」「也没什么」），可以有点委屈但不矫情；\n"
        "4. 说完可以反问 TA 一个问题；\n"
        "5. 50~120 字。\n"
        "直接输出对话内容，不要任何前缀。"
    )
    if not key:
        return ""
    try:
        reply = (await chat_once(config.get("CURRENT_CHAT_MODEL"), [
            {"role": "user", "content": prompt}
        ], key, temperature=0.9, max_tokens=250)).strip()
    except Exception:
        reply = ""

    # 说出来后：强度降低，标记 revealed
    for e in state["events"]:
        e["intensity"] = max(0.1, e["intensity"] * 0.5)
        e["revealed"] = True
    _save(session_id, character_id, state)
    return reply
