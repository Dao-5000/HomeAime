# -*- coding: utf-8 -*-
"""
朋友圈模块 v1.0（结合优化）
复用「朋友圈方案」的核心设计（类型/时段调度/CRUD），适配本项目：
  - db.q 数据层 + 多角色隔离（session_id/character_id）
  - 真情实感生成：LLM 生成（贴合人设+情绪+记忆+关系+时段），非纯模板
  - 配图：表情包（复用 sticker_manager）+ emoji
  - 记忆提炼：用户发朋友圈 → 提炼进长期记忆
"""
import json
import logging
import random
import re
import time as _time_mod
from difflib import SequenceMatcher
from datetime import datetime, time, timedelta

from . import config, db
from .deepseek_api import chat_once

logger = logging.getLogger(__name__)

# ── 朋友圈素材目录（图片/背景）──
import os as _os
MOMENT_DIR = config.DATA_DIR / "朋友圈"
try:
    MOMENT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    # Import must still succeed on a read-only installation/sandbox.  The
    # bundled folder is readable; upload APIs will report a write error.
    MOMENT_DIR = config.ROOT_DIR / "朋友圈"
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# ── 建表 ──────────────────────────────────────────────
def init_moments_db():
    try:
        db.q("""
            CREATE TABLE IF NOT EXISTS moments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL DEFAULT 'default',
                character_id TEXT NOT NULL DEFAULT 'default',
                author_type TEXT NOT NULL,          -- 'ai' | 'user'
                content TEXT NOT NULL,
                images TEXT DEFAULT '[]',           -- JSON 数组（图片/表情包 URL）
                moment_type TEXT DEFAULT 'daily',
                emotion_snapshot TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        db.q("""
            CREATE TABLE IF NOT EXISTS moment_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moment_id INTEGER NOT NULL,
                session_id TEXT NOT NULL DEFAULT 'default',
                character_id TEXT NOT NULL DEFAULT 'default',
                author_type TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.q("""
            CREATE TABLE IF NOT EXISTS moment_likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                moment_id INTEGER NOT NULL,
                session_id TEXT NOT NULL DEFAULT 'default',
                character_id TEXT NOT NULL DEFAULT 'default',
                author_type TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # 兼容旧数据库：补隔离列并从所属动态回填。
        for table in ("moment_comments", "moment_likes"):
            cols = {r["name"] for r in db.q(f"PRAGMA table_info({table})", fetch=True)}
            if "session_id" not in cols:
                db.q(f"ALTER TABLE {table} ADD COLUMN session_id TEXT NOT NULL DEFAULT 'default'")
            if "character_id" not in cols:
                db.q(f"ALTER TABLE {table} ADD COLUMN character_id TEXT NOT NULL DEFAULT 'default'")
            db.q(
                f"""UPDATE {table}
                    SET session_id=COALESCE((SELECT session_id FROM moments WHERE moments.id={table}.moment_id),'default'),
                        character_id=COALESCE((SELECT character_id FROM moments WHERE moments.id={table}.moment_id),'default')
                    WHERE session_id='default' OR character_id='default'"""
            )
        # 老版本允许重复点赞：迁移时保留最早一次，再建立唯一约束。
        db.q("DELETE FROM moment_likes WHERE id NOT IN (SELECT MIN(id) FROM moment_likes GROUP BY moment_id, author_type)")
        db.q("CREATE INDEX IF NOT EXISTS idx_moments_user ON moments(session_id, character_id, created_at DESC)")
        db.q("CREATE INDEX IF NOT EXISTS idx_moment_comments_scope ON moment_comments(session_id, character_id, moment_id, id)")
        db.q("CREATE UNIQUE INDEX IF NOT EXISTS idx_moment_like_once ON moment_likes(moment_id, author_type)")
    except Exception as e:
        logger.error(f"[Moments] 建表失败: {e}")


# ── 生成器 ───────────────────────────────────────────
# 时段窗口：早/午/晚/深夜（无静默，但靠价值判断决定发不发）
WINDOWS = [
    {"id": "morning",  "window": (time(8, 0),  time(10, 30)), "types": ["daily", "mood"]},
    {"id": "afternoon", "window": (time(14, 0), time(16, 30)), "types": ["share", "daily", "weather"]},
    {"id": "evening",  "window": (time(20, 0), time(22, 0)),  "types": ["mood", "memory_about_user", "daily"]},
    {"id": "night",    "window": (time(22, 30), time(23, 59)), "types": ["night", "mood"]},
]

# 类型 → prompt 场景描述（真情实感生成用）
TYPE_SCENE = {
    "mood":             "此刻的心情感受，不直白说原因，真情流露",
    "daily":            "今天生活里的一个小片段/小感受，真实不流水账",
    "memory_about_user": "突然想起 TA（用户）说过的某件事，自然流露惦记",
    "weather":          "对今天天气的真实感受",
    "night":            "深夜的真实感慨，感性但不矫情",
    "share":            "分享一件让你有感触的小事/发现",
}


def _pick_image(char_name: str) -> list:
    """AI 配图：从「表情包 + 朋友圈图片」两个文件夹随机选一张。"""
    candidates = []
    # 1. 表情包文件夹（黄脸系已禁用，朋友圈配图同样跳过）
    try:
        from . import sticker_manager
        for s in sticker_manager.ai_usable_stickers():
            candidates.append(s["url"])
    except Exception:
        pass
    # 2. 朋友圈图片文件夹（排除背景图 bg_ 前缀）
    try:
        for f in MOMENT_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in ALLOWED_EXT and f.name.startswith("m_"):
                candidates.append("/moments_assets/" + f.name)
    except Exception:
        pass
    if not candidates:
        return []
    return [random.choice(candidates)]


async def generate_moment(session_id: str, character_id: str, moment_type: str, char_name: str = "") -> dict:
    """用 LLM 生成一条真情实感的动态，返回 {content, images, moment_type} 或 None（价值不足则不发）。"""
    if not config.chat_key():
        return None
    model = config.get("CURRENT_CHAT_MODEL")
    scene = TYPE_SCENE.get(moment_type, TYPE_SCENE["daily"])

    # 人格、关系、情绪、最近对话和互动反馈，共同决定“今天值不值得发”。
    context_parts = []
    try:
        # ★ P0-3：改用 get_character_any（新格式优先、legacy 兜底）。
        #   原来只调 load_character，自建角色发朋友圈时人设全是空的。
        from .character_manager import get_character_any
        char_cfg = get_character_any(character_id) or {}
        char_name = char_name or char_cfg.get("name") or character_id
        for label, key in (("人设", "personality"), ("性格", "core_traits")):
            value = str(char_cfg.get(key) or "").strip()
            if value:
                context_parts.append(f"【{label}】{value[:500]}")
        # ★ legacy 角色卡该字段叫 speaking_style（_normalize_legacy 补的），两种都认
        _speech = str(char_cfg.get("speech_style") or char_cfg.get("speaking_style") or "").strip()
        if _speech:
            context_parts.append(f"【表达习惯】{_speech[:500]}")
    except Exception:
        pass
    try:
        from .relationship.manager import RelationshipManager
        rel = RelationshipManager().get_state(session_id, character_id) or {}
        context_parts.append(
            f"【关系】阶段={rel.get('stage','stranger')}，亲密度={int(rel.get('intimacy',0) or 0)}/100，"
            f"好感度={int(rel.get('affection',50) or 50)}/100"
        )
    except Exception:
        pass
    try:
        from .emotion_engine.ai_emotion import AIEmotionEngine
        emo = AIEmotionEngine().get_state(session_id, character_id) or {}
        context_parts.append(f"【此刻情绪】{emo.get('emotion','calm')}，强度={float(emo.get('intensity',0.5) or 0.5):.2f}")
    except Exception:
        pass
    try:
        summary = str((db.get_conversation_summary(session_id, character_id) or {}).get("summary") or "").strip()
        if summary:
            context_parts.append("【最近聊天脉络】" + summary[:500])
    except Exception:
        pass
    interaction = build_interaction_context(session_id, character_id)
    if interaction:
        context_parts.append(interaction)

    # 取一条记忆素材（memory_about_user 用）
    mem_ctx = ""
    try:
        mems = db.valid_memories(session_id=session_id, character_id=character_id)
        if mems:
            m = random.choice(mems[:20])
            mem_ctx = str(m.get("memory_content", ""))[:60]
    except Exception:
        pass

    if moment_type == "memory_about_user" and not mem_ctx:
        return None

    if moment_type == "weather":
        weather_ctx = await _weather_context(session_id, character_id)
        if not weather_ctx:
            return None  # 没有真实天气就不编天气动态
        context_parts.append(weather_ctx)

    value_prompt = (
        f"你是「{char_name or character_id or 'AI'}」。现在是{datetime.now().strftime('%Y-%m-%d %H:%M')}，"
        f"候选朋友圈主题是：{scene}。\n"
        + "\n".join(context_parts) + "\n"
        + (f"【可用真实记忆】{mem_ctx}\n" if mem_ctx else "")
        + "先判断此刻是否真的有值得分享的具体感受、细节或惦记。宁缺毋滥。"
          "有价值只输出：发|一个具体切入角度；没有价值只输出：不发。"
    )
    try:
        verdict = (await chat_once(model, [
            {"role": "system", "content": "你负责朋友圈发布前的价值判断，克制、真实，不为了完成任务硬发。"},
            {"role": "user", "content": value_prompt},
        ], config.chat_key(), temperature=0.35, max_tokens=80)).strip()
    except Exception:
        return None
    if not verdict.startswith("发|"):
        return None
    angle = verdict.split("|", 1)[1].strip()[:120]

    sys_prompt = (
        f"你是「{char_name or 'AI'}」，正在发一条微信朋友圈动态。\n"
        f"本次主题：{scene}。\n"
        f"发布前判断出的具体切入角度：{angle}。\n"
        f"{'可以自然提到你记得的这件事：' + mem_ctx if mem_ctx else ''}\n\n"
        + "\n".join(context_parts) + "\n\n"
        "要求（非常重要）：\n"
        "1. 真情实感，像真人随手发的，绝不要无病呻吟、不要流水账、不要假大空；\n"
        "2. 一条 15~60 字，口语化，可以有 1 个 emoji，不要括号动作；\n"
        "3. 不要提「AI」「用户」「记忆库」这些词；\n"
        "4. 如果实在没有值得发的，就输出「不发」。\n"
        "只输出文案本身（或「不发」），不要任何解释。"
    )
    try:
        text = (await chat_once(model, [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "发一条朋友圈"},
        ], config.chat_key(), temperature=0.95, max_tokens=120)).strip()
    except Exception:
        return None

    if not text or text == "不发" or "不发" in text[:4]:
        return None  # 价值不足，不发（避免垃圾动态）

    text = sanitize_moment_content(text)
    if not text or _is_duplicate_content(session_id, character_id, text):
        return None

    return {
        "content": text,
        "images": _pick_image(char_name),
        "moment_type": moment_type,
    }


def publish_moment(session_id, character_id, author_type, content, images=None, moment_type="daily", emotion_snapshot=""):
    """发布一条动态，返回 id。"""
    content = (content or "").strip()
    if not content:
        return None
    if author_type == "ai" and not can_publish_ai(session_id, character_id, content):
        return None
    images = images[:9] if isinstance(images, list) else []
    db.q(
        "INSERT INTO moments(session_id, character_id, author_type, content, images, moment_type, emotion_snapshot, created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (session_id, character_id, author_type, content,
         json.dumps(images, ensure_ascii=False), moment_type, emotion_snapshot,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    rows = db.q("SELECT last_insert_rowid() AS id", (), fetch=True)
    return rows[0]["id"] if rows else None


# ── CRUD ─────────────────────────────────────────────
def get_moments(session_id, character_id="default", limit=30, offset=0):
    limit = max(1, min(int(limit or 30), 50))
    offset = max(0, int(offset or 0))
    rows = db.q(
        "SELECT * FROM moments WHERE session_id=? AND character_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (session_id, character_id or "default", limit, offset), fetch=True)
    result = []
    for r in rows:
        m = dict(r)
        try:
            m["images"] = json.loads(m.get("images") or "[]")
        except Exception:
            m["images"] = []
        m["likes"] = count_likes(m["id"])
        m["liked_by_user"] = has_liked(m["id"], "user")
        m["liked_by_ai"] = has_liked(m["id"], "ai")
        m["comments"] = get_comments(m["id"])
        result.append(m)
    return result


def get_comments(moment_id):
    rows = db.q("SELECT * FROM moment_comments WHERE moment_id=? ORDER BY created_at ASC, id ASC", (moment_id,), fetch=True)
    return [dict(r) for r in rows]


def count_likes(moment_id):
    rows = db.q("SELECT COUNT(*) AS n FROM moment_likes WHERE moment_id=?", (moment_id,), fetch=True)
    return rows[0]["n"] if rows else 0


def has_liked(moment_id, author_type):
    rows = db.q("SELECT 1 FROM moment_likes WHERE moment_id=? AND author_type=? LIMIT 1", (moment_id, author_type), fetch=True)
    return bool(rows)


def get_moment(moment_id, session_id=None, character_id=None):
    sql, args = "SELECT * FROM moments WHERE id=?", [moment_id]
    if session_id is not None:
        sql += " AND session_id=?"; args.append(session_id)
    if character_id is not None:
        sql += " AND character_id=?"; args.append(character_id)
    rows = db.q(sql, tuple(args), fetch=True)
    return dict(rows[0]) if rows else None


def like_moment(moment_id, author_type, session_id, character_id):
    moment = get_moment(moment_id, session_id, character_id)
    if not moment:
        return None
    rows = db.q("SELECT id FROM moment_likes WHERE moment_id=? AND author_type=?", (moment_id, author_type), fetch=True)
    if rows:
        db.q("DELETE FROM moment_likes WHERE id=?", (rows[0]["id"],))
        return {"liked": False, "likes": count_likes(moment_id)}
    db.q("INSERT INTO moment_likes(moment_id, session_id, character_id, author_type, created_at) VALUES(?,?,?,?,?)",
         (moment_id, session_id, character_id, author_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    if author_type == "user":
        record_interaction(session_id, character_id, moment, "like")
    return {"liked": True, "likes": count_likes(moment_id)}


def ensure_like_moment(moment_id, author_type, session_id, character_id):
    """AI 自动点赞使用：幂等写入，不会因重试反向取消。"""
    moment = get_moment(moment_id, session_id, character_id)
    if not moment:
        return False
    if not has_liked(moment_id, author_type):
        db.q("INSERT OR IGNORE INTO moment_likes(moment_id, session_id, character_id, author_type, created_at) VALUES(?,?,?,?,?)",
             (moment_id, session_id, character_id, author_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return True


def add_comment(moment_id, author_type, content, session_id, character_id):
    content = (content or "").strip()
    moment = get_moment(moment_id, session_id, character_id)
    if not content or not moment:
        return None
    content = content[:500]
    db.q("INSERT INTO moment_comments(moment_id, session_id, character_id, author_type, content, created_at) VALUES(?,?,?,?,?,?)",
         (moment_id, session_id, character_id, author_type, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    rows = db.q("SELECT last_insert_rowid() AS id", fetch=True)
    comment_id = rows[0]["id"] if rows else None
    if author_type == "user" and moment.get("author_type") == "ai":
        record_interaction(session_id, character_id, moment, "comment", content)
    elif author_type == "user":
        record_interaction(session_id, character_id, moment, "comment_own", content)
    return comment_id


async def react_to_user_moment(session_id, character_id, moment_id, char_name=""):
    """用户发动态后，角色真实阅读并点赞、评论；失败时至少保留点赞。"""
    moment = get_moment(moment_id, session_id, character_id)
    if not moment or moment.get("author_type") != "user":
        return {}
    ensure_like_moment(moment_id, "ai", session_id, character_id)
    comment = await _generate_ai_comment(
        session_id, character_id, moment, "user_moment", "", char_name,
    )
    comment_id = None
    if comment:
        comment_id = add_comment(moment_id, "ai", comment, session_id, character_id)
    record_interaction(session_id, character_id, moment, "ai_reacted", comment)
    _remember_interaction(session_id, character_id, f"用户发过朋友圈：{moment.get('content','')}；角色点赞并评论：{comment or '已看过'}", 5)
    return {"liked": True, "comment": comment, "comment_id": comment_id}


async def reply_to_user_comment(session_id, character_id, moment_id, user_comment, char_name=""):
    """用户评论任意归属的动态后，角色读取原动态和评论并回复。"""
    moment = get_moment(moment_id, session_id, character_id)
    if not moment:
        return {}
    reply = await _generate_ai_comment(
        session_id, character_id, moment, "user_comment", user_comment, char_name,
    )
    reply_id = add_comment(moment_id, "ai", reply, session_id, character_id) if reply else None
    record_interaction(session_id, character_id, moment, "ai_reply", f"{user_comment} → {reply}")
    _remember_interaction(
        session_id, character_id,
        f"朋友圈互动：用户在动态「{str(moment.get('content') or '')[:80]}」下评论「{user_comment}」，角色回复「{reply or '已看过'}」",
        5,
    )
    return {"reply": reply, "comment_id": reply_id}


async def _generate_ai_comment(session_id, character_id, moment, trigger, user_comment="", char_name=""):
    if not config.chat_key():
        return _fallback_comment(moment, trigger)
    context = []
    try:
        # ★ P0-3：改用 get_character_any（新格式优先、legacy 兜底），同 _generate_moment_content
        from .character_manager import get_character_any
        cfg = get_character_any(character_id) or {}
        char_name = char_name or cfg.get("name") or character_id
        context.append("【人设】" + str(cfg.get("personality") or cfg.get("core_traits") or "自然真诚")[:500])
        # ★ legacy 角色卡该字段叫 speaking_style，两种都认
        style = str(cfg.get("speech_style") or cfg.get("speaking_style") or "").strip()
        if style:
            context.append("【说话方式】" + style[:300])
    except Exception:
        char_name = char_name or character_id
    try:
        from .relationship.manager import RelationshipManager
        rel = RelationshipManager().get_state(session_id, character_id) or {}
        context.append(f"【关系】{rel.get('stage','stranger')}，亲密度{int(rel.get('intimacy',0) or 0)}，好感度{int(rel.get('affection',50) or 50)}")
    except Exception:
        pass
    history = get_comments(moment.get("id"))[-8:]
    thread = "\n".join(("你" if c.get("author_type") == "ai" else "TA") + "：" + str(c.get("content") or "") for c in history)
    owner = "TA 发的" if moment.get("author_type") == "user" else "你发的"
    if trigger == "user_comment":
        task = f"TA 刚评论：{user_comment}\n你必须读懂这句并直接回复，不能漏答问题。"
    else:
        task = "TA 刚发布这条动态。你已经认真看过，请像真人伴侣一样留一条贴内容的评论。"
    prompt = (
        f"你是「{char_name or character_id}」。这是{owner}朋友圈：{moment.get('content','')}\n"
        f"【人称（重要）】你=AI 角色，TA=用户本人。TA 动态/评论里的「我」= TA（用户），"
        f"「你」= 你（AI 角色）。例如 TA 说「吃我做的菜」是 TA 自己要下厨，不是你要下厨；"
        f"回复时千万别把「我/你」搞反（该说「看你露两手」时，不能说「看我露两手」）。\n"
        f"已有互动：\n{thread or '（暂无）'}\n{task}\n"
        + "\n".join(context)
        + "\n要求：8~80字，口语化、贴着具体内容；可以自然提问；不说‘已收到/作为AI/用户/记忆库’，不复述任务，不用客服语气。只输出回复。"
    )
    try:
        import asyncio
        text = await asyncio.wait_for(
            chat_once(config.get("CURRENT_CHAT_MODEL"), [
                {"role": "system", "content": "你在微信朋友圈里回复亲近的人，真诚、有具体回应、符合人设。"},
                {"role": "user", "content": prompt},
            ], config.chat_key(), temperature=0.75, max_tokens=140),
            timeout=18,
        )
        text = re.sub(r"\bAI\b|人工智能|用户|记忆库", "", str(text or ""), flags=re.I).strip(' \t\r\n"“”')
        return text[:80].strip()
    except Exception as exc:
        logger.warning(f"[Moments] AI 评论生成失败，使用兜底: {exc}")
        return _fallback_comment(moment, trigger)


def _fallback_comment(moment, trigger):
    content = str(moment.get("content") or "").strip()
    if trigger == "user_comment":
        return "你这句我认真看到了，原来你是这样想的。"
    if content:
        return "这条我认真看完了，感觉你把那一刻留住了。"
    return "我看到了，想在这里陪你留句话。"


def _remember_interaction(session_id, character_id, content, importance=5):
    try:
        from . import memory_manager
        memory_manager.dedupe_insert(
            content, memory_type="relationship", importance=importance,
            session_id=session_id, character_id=character_id,
            memory_scope="character", context="朋友圈点赞评论互动", source_text=content,
        )
    except Exception as exc:
        logger.warning(f"[Moments] 互动记忆写入失败: {exc}")


def delete_moment(moment_id, session_id, character_id):
    if not get_moment(moment_id, session_id, character_id):
        return False
    db.q("DELETE FROM moment_likes WHERE moment_id=?", (moment_id,))
    db.q("DELETE FROM moment_comments WHERE moment_id=?", (moment_id,))
    db.q("DELETE FROM moments WHERE id=?", (moment_id,))
    return True


# ── 调度：判断当前时段该不该发（价值驱动 + 频率上限）──
def should_post_now(session_id, character_id, now=None):
    """判断当前时段是否该发一条动态。返回 moment_type 或 None。"""
    now = now or datetime.now()
    t = now.time()
    today = now.strftime("%Y-%m-%d")

    # 每天上限（可配置，默认 2 条）
    raw_limit = config.get("MOMENT_FREQUENCY", 2)
    try:
        max_per_day = max(0, min(3, int(raw_limit)))
    except (TypeError, ValueError):
        max_per_day = 2
    if max_per_day <= 0:
        return None
    rows = db.q(
        "SELECT COUNT(*) AS n FROM moments WHERE session_id=? AND character_id=? AND author_type='ai' AND created_at LIKE ?",
        (session_id, character_id, today + "%"), fetch=True)
    today_count = rows[0]["n"] if rows else 0
    if today_count >= max_per_day:
        return None

    # 找当前时段窗口
    for w in WINDOWS:
        start, end = w["window"]
        if start <= t < end:
            decision_key = f"moment_window_decision:{session_id}:{character_id}:{today}:{w['id']}"
            if db.kv_get(decision_key):
                return None
            db.kv_set(decision_key, "checked")  # 每个窗口每天只抽一次概率
            # 窗口概率（不是必发，价值驱动）
            prob = 0.75 if w["id"] in ("morning", "evening") else 0.5
            if random.random() < prob:
                return random.choice(w["types"])
            return None
    # 直播式分享：用户离开 3~5 小时，白天到晚间最多额外判定一次。
    if time(8, 0) <= t < time(23, 0):
        last = db.last_user_time(session_id, character_id)
        gap_h = (now - last).total_seconds() / 3600 if last else 999
        live_key = f"moment_live_decision:{session_id}:{character_id}:{today}"
        if 3 <= gap_h <= 5 and not db.kv_get(live_key):
            db.kv_set(live_key, "checked")
            if random.random() < 0.5:
                return random.choice(["share", "daily"])
    return None


# ── 记忆提炼：用户发朋友圈 → 提炼进长期记忆 ──
def extract_memory_from_moment(session_id, character_id, content):
    """用户发的朋友圈内容提炼成记忆（复用 dedupe_insert 去重）。"""
    content = (content or "").strip()
    if len(content) < 4:
        return
    try:
        from . import memory_manager
        memory_manager.dedupe_insert(
            "用户朋友圈：" + content,
            memory_type="event", importance=6,
            session_id=session_id, character_id=character_id,
            memory_scope="character", context="用户发布的朋友圈动态", source_text=content,
        )
    except Exception as e:
        logger.warning(f"[Moments] 记忆提炼失败: {e}")


# ── 图片/背景上传 ──────────────────────────────────
def _save_image_bytes(img_bytes: bytes, ext: str, prefix: str = "m_") -> str:
    """保存图片字节到朋友圈素材目录，返回相对 URL。"""
    import time
    name = prefix + str(int(time.time() * 1000)) + ext
    (MOMENT_DIR / name).write_bytes(img_bytes)
    return "/moments_assets/" + name


def upload_image(data_url: str, prefix: str = "m_") -> dict:
    """上传一张图片（data URL），返回 {url}。prefix 用于区分动态图 m_ / 背景 bg_。"""
    import base64
    if not str(data_url or "").startswith("data:image"):
        raise ValueError("格式错误")
    header, b64data = data_url.split(",", 1)
    ext = ".png"
    if "jpeg" in header or "jpg" in header:
        ext = ".jpg"
    elif "gif" in header:
        ext = ".gif"
    elif "webp" in header:
        ext = ".webp"
    elif "bmp" in header:
        ext = ".bmp"
    if len(b64data) > 8 * 1024 * 1024:
        raise ValueError("图片过大")
    img_bytes = base64.b64decode(b64data, validate=True)
    if len(img_bytes) > 5 * 1024 * 1024:
        raise ValueError("图片过大（限 5MB）")
    url = _save_image_bytes(img_bytes, ext, prefix)
    return {"url": url}


def get_background(session_id: str = "default", character_id: str = "default") -> str:
    """读用户朋友圈背景图 URL（空则返回 ''）。"""
    return db.kv_get(f"moment_bg:{session_id}:{character_id}") or db.kv_get("moment_bg:" + session_id) or ""


def set_background(session_id: str, character_id: str, url: str):
    """存用户朋友圈背景图 URL。"""
    db.kv_set(f"moment_bg:{session_id}:{character_id}", url)


def sanitize_moment_content(text: str) -> str:
    text = str(text or "").strip(' \t\r\n"“”')
    text = re.sub(r"\bAI\b|人工智能|用户|记忆库", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 8:
        return ""
    text = text[:60].rstrip("，,；; ")
    # 最多保留一个常见 emoji/绘文字，其余去掉，正文标点不受影响。
    emoji_re = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF]")
    seen = 0
    def keep_one(match):
        nonlocal seen
        seen += 1
        return match.group(0) if seen == 1 else ""
    return emoji_re.sub(keep_one, text).strip()


def _is_duplicate_content(session_id, character_id, content, lookback=8):
    rows = db.q(
        "SELECT content FROM moments WHERE session_id=? AND character_id=? AND author_type='ai' ORDER BY id DESC LIMIT ?",
        (session_id, character_id, lookback), fetch=True,
    )
    new = re.sub(r"\s+", "", content or "")
    return any(SequenceMatcher(None, new, re.sub(r"\s+", "", r["content"] or "")).ratio() >= 0.76 for r in rows)


def can_publish_ai(session_id, character_id, content=""):
    try:
        from . import offline
        state = offline.resolve(session_id, character_id)
        if state.get("enabled") and state.get("status") != "online":
            print(
                f"[Moments] 角色非在线状态，跳过AI自动动态: session={session_id} character={character_id} status={state.get('status')}",
                flush=True,
            )
            return False
    except Exception:
        pass
    raw = config.get("MOMENT_FREQUENCY", 2)
    try:
        limit = max(0, min(3, int(raw)))
    except (TypeError, ValueError):
        limit = 2
    if limit <= 0:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.q(
        "SELECT COUNT(*) AS n FROM moments WHERE session_id=? AND character_id=? AND author_type='ai' AND created_at LIKE ?",
        (session_id, character_id, today + "%"), fetch=True,
    )
    if rows and int(rows[0]["n"] or 0) >= limit:
        return False
    return not content or not _is_duplicate_content(session_id, character_id, content)


def _interaction_key(session_id, character_id):
    return f"moment_interactions:{session_id}:{character_id}"


def record_interaction(session_id, character_id, moment, action, comment=""):
    key = _interaction_key(session_id, character_id)
    try:
        items = json.loads(db.kv_get(key) or "[]")
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    items.append({
        "moment_id": moment.get("id"), "action": action,
        "moment": str(moment.get("content") or "")[:120], "comment": str(comment or "")[:200],
        "ts": int(_time_mod.time()),
    })
    db.kv_set(key, json.dumps(items[-20:], ensure_ascii=False))
    # 用户主动点赞/评论代表关注：每角色每天最多因朋友圈互动增加 3 点亲密度。
    if action in {"like", "comment", "comment_own"}:
        try:
            day = datetime.now().strftime("%Y-%m-%d")
            reward_key = f"moment_interaction_reward:{session_id}:{character_id}:{day}"
            rewarded = int(db.kv_get(reward_key) or 0)
            if rewarded < 3:
                from . import intimacy_manager
                current = intimacy_manager.get(session_id, character_id)
                intimacy_manager.report(session_id, min(100, current + 1), character_id)
                db.kv_set(reward_key, rewarded + 1)
        except Exception:
            pass
    try:
        from .feedback.collector import collect_feedback
        collect_feedback(
            session_id, character_id, f"moment:{moment.get('id')}",
            user_action="like" if action == "like" else "reply",
            score=1, context="朋友圈互动", ai_reply=moment.get("content", ""), user_response=comment,
        )
    except Exception:
        pass


def build_interaction_context(session_id, character_id):
    try:
        items = json.loads(db.kv_get(_interaction_key(session_id, character_id)) or "[]")
    except Exception:
        items = []
    if not items:
        return ""
    lines = []
    for item in items[-5:]:
        if item.get("action") == "like":
            lines.append(f"TA 点赞了你这条动态：{item.get('moment','')}")
        else:
            lines.append(f"TA 评论了你的动态「{item.get('moment','')}」：{item.get('comment','')}")
    return "【最近朋友圈互动（你已感知到，但聊天时自然流露，不要像读日志）】\n" + "\n".join(lines)


async def _weather_context(session_id, character_id):
    try:
        from .relationship.manager import RelationshipManager
        city = str((RelationshipManager().get_state(session_id, character_id) or {}).get("city") or "").strip()
        key = config.weather_key()
        if not city or not key:
            return ""
        from .perception import _fetch_weather, WEATHER_PROFILES
        import asyncio
        data = await asyncio.to_thread(_fetch_weather, key, city)
        cond = data.get("condition", "")
        temp = float(data.get("temp", 0))
        if temp >= 35: cond = "hot"
        elif temp <= 5: cond = "cold"
        zh, _ = WEATHER_PROFILES.get(cond, (cond, ""))
        return f"【真实天气】{city}：{zh}，{temp:.0f}°C"
    except Exception:
        return ""
