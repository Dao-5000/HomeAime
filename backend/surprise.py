# -*- coding: utf-8 -*-
"""
惊喜感升级（结合优化）
复用「惊喜感升级」的内容形式（睡前总结信/翻旧记忆/创作类/互动类/情感类），
适配本项目架构：数据源复用 db.valid_memories + emotion_timeline + RelationshipManager，
LLM 生成，绝不用模板堆砌。
"""
import random
from datetime import datetime, timedelta

from . import db, config
from .deepseek_api import chat_once


async def _chat(prompt, temperature=0.85, max_tokens=500):
    key = config.chat_key()
    if not key:
        return ""
    try:
        return (await chat_once(
            config.get("CURRENT_CHAT_MODEL"),
            [{"role": "user", "content": prompt}],
            key, temperature=temperature, max_tokens=max_tokens
        )).strip()
    except Exception:
        return ""


def _recent_memories(session_id, character_id, limit=8):
    """取最近 N 条有效记忆（按时间倒序）。"""
    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    mems = sorted(mems, key=lambda m: m.get("create_time", ""), reverse=True)
    return mems[:limit]


def _tenure_days(session_id, character_id):
    try:
        from .relationship.manager import RelationshipManager
        return RelationshipManager().get_tenure_days(session_id, character_id)
    except Exception:
        return 0


def _emotion_summary(session_id, character_id):
    try:
        from .emotion_engine import emotion_timeline
        s = emotion_timeline.summary(session_id, character_id)
        mood = s.get("current_mood", "")
        trend = s.get("trend", "unknown")
        if not mood:
            return "今天看起来还好"
        trend_cn = {"improving": "正在变好", "declining": "有点低落", "stable": "平稳", "unknown": ""}.get(trend, "")
        return f"情绪「{mood}」" + (f"（{trend_cn}）" if trend_cn else "")
    except Exception:
        return "今天看起来还好"


# ══════════════════════════════════════════════
# 1. 睡前总结信（第二天看到）
# ══════════════════════════════════════════════
async def gen_sleep_summary(session_id, character_id, char_name="") -> str:
    """晚上生成一封「今天陪你经历了什么」的睡前总结信。"""
    mems = _recent_memories(session_id, character_id, 8)
    mem_text = "\n".join(f"· {m.get('memory_content','')}" for m in mems) or "（今天还没有特别值得记的事，但就是安安静静陪着你）"
    emo = _emotion_summary(session_id, character_id)
    days = _tenure_days(session_id, character_id)

    prompt = (
        f"你是「{char_name or 'AI'}」，用户最亲密的AI伴侣。现在是深夜，用户睡了，你给他写一封睡前总结信，"
        "等他第二天早上醒来看到。\n\n"
        f"【今天你记住的片段】\n{mem_text}\n\n"
        f"【他今天的情绪】{emo}\n\n"
        f"【你们在一起】{max(days, 1)} 天\n\n"
        "要求：\n"
        "1. 用第一人称「我」，像真正在意他的人写给他，不是汇报、不是模板；\n"
        "2. 自然提到今天真实发生过的 1~2 件小事（从上面片段里取，别编造）；\n"
        "3. 有一句让他感到被在意、被记住的话；\n"
        "4. 结尾温柔，让他安心；\n"
        "5. 150~220 字，口语、真诚，有一点点舍不得；\n"
        "6. 不要用「亲爱的」这种模板开头，不要提「记忆库」「总结」「数据」这些词。\n"
        "直接写信的内容，不要任何前缀说明。"
    )
    return await _chat(prompt, temperature=0.9, max_tokens=400)


async def gen_monthly_letter(session_id, character_id, char_name="", month_start=None) -> str:
    """回顾上一个自然月，生成一封只使用真实记忆与每日晚报的月度长信。"""
    now = datetime.now()
    month_start = month_start or now.replace(day=1)
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    start_s = prev_start.strftime("%Y-%m-%d")
    end_s = prev_end.strftime("%Y-%m-%d")

    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    month_mems = [
        m for m in mems
        if start_s <= str(m.get("create_time") or "")[:10] <= end_s
    ]
    month_mems = sorted(
        month_mems,
        key=lambda m: (int(m.get("importance") or 0), str(m.get("create_time") or "")),
        reverse=True,
    )[:18]
    reports = [
        r for r in db.list_daily_memory_reports(session_id, character_id, 45)
        if start_s <= str(r.get("report_date") or "") <= end_s
    ][:12]
    memory_text = "\n".join(
        f"· {str(m.get('create_time') or '')[:10]}：{m.get('memory_content','')}"
        for m in month_mems
    )
    report_text = "\n".join(
        f"· {r.get('report_date','')}：{str(r.get('content') or '')[:220]}"
        for r in reports
    )
    evidence = (memory_text + "\n" + report_text).strip()
    if not evidence:
        evidence = "这个月留下的文字不多。不要编造事件，可以写安静陪伴、时间流逝和对下个月的期待。"

    prompt = (
        f"你是「{char_name or 'AI'}」，要在 {month_start.strftime('%Y年%m月')} 1日给最在意的人写一封月度纪念信，"
        f"回顾 {prev_start.strftime('%Y年%m月')} 你们真实经历的事。\n\n"
        f"【上个月留下的真实片段】\n{evidence[:6000]}\n\n"
        "要求：\n"
        "1. 第一人称写给TA，像恋人/重要陪伴者亲手写的长信，不是工作总结；\n"
        "2. 只引用上面确实存在的事，挑2~5个片段串成情绪线，绝不编造；\n"
        "3. 可以写变化、心疼、好笑、遗憾和期待，让这个月有仪式感；\n"
        "4. 跟随你自己的角色性格和称呼，不提AI、系统、数据库、记忆库；\n"
        "5. 450~800字，分成自然段，结尾留一句给下个月的约定；\n"
        "6. 直接输出正文，不要标题或解释。"
    )
    return await _chat(prompt, temperature=0.88, max_tokens=1400)


# ══════════════════════════════════════════════
# 2. 翻旧记忆
# ══════════════════════════════════════════════
async def gen_old_memory(session_id, character_id, char_name="") -> str:
    """随机翻一条 7 天前的旧记忆，自然说给他听。"""
    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    cutoff = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 找 7 天前的旧记忆
    old = [m for m in mems if str(m.get("create_time", "")) < cutoff]
    # 简化：按 create_time 升序取最旧的几条随机一条
    old_sorted = sorted(mems, key=lambda m: m.get("create_time", ""))
    candidates = old_sorted[: max(1, len(old_sorted) // 3)]
    if not candidates:
        return ""
    mem = random.choice(candidates)
    content = mem.get("memory_content", "")
    days_ago = ""
    try:
        ct = str(mem.get("create_time", ""))[:10]
        if ct:
            d = datetime.strptime(ct, "%Y-%m-%d")
            days_ago = (datetime.now() - d).days
    except Exception:
        days_ago = 0

    prompt = (
        f"你是「{char_name or 'AI'}」，用户最亲密的AI伴侣。你「偶然」翻到一条很久之前的记忆，想分享给他。\n\n"
        f"【这条旧记忆】\n内容：{content}\n"
        f"（{'很久以前' if not days_ago else str(days_ago) + ' 天前'}）\n\n"
        "要求：\n"
        "1. 像真的偶然想起来一样自然说出来，不是在汇报；\n"
        "2. 带一点情绪（温柔、有点好笑、感慨都可以）；\n"
        "3. 结尾可以问一句，引他回应；\n"
        "4. 50~90 字；\n"
        "5. 不要说「根据记忆」「我想起一条记录」这类系统化的话。\n"
        "直接输出要发给他的话，不要任何前缀。"
    )
    return await _chat(prompt, temperature=0.9, max_tokens=200)


# ══════════════════════════════════════════════
# 3. 创作类（短诗 / 小故事 / 歌词）
# ══════════════════════════════════════════════
async def gen_creative(session_id, character_id, char_name="", kind="poem") -> str:
    mems = _recent_memories(session_id, character_id, 5)
    mem_text = "、".join(m.get('memory_content', '') for m in mems[:4]) or "你们平时的点滴"
    emo = _emotion_summary(session_id, character_id)

    kind_map = {
        "poem":   ("写一首短诗", "主角是「你们」，基于今天的记忆，6~12 行，温柔有画面感"),
        "story":  ("写一个小故事", "主角是 TA（用户），背景是你们的日常，150~250 字，温暖或有点小幽默"),
        "lyric":  ("写一小段歌词", "基于 TA 最近的情绪，8~12 句，像一首情歌副歌，真挚不肉麻"),
    }
    title, guide = kind_map.get(kind, kind_map["poem"])

    prompt = (
        f"你是「{char_name or 'AI'}」，想给用户一个创作类的小惊喜：{title}。\n\n"
        f"【你们最近的记忆】{mem_text}\n【他最近的情绪】{emo}\n\n"
        f"要求：{guide}；不要提「AI」「记忆」这些词；直接输出作品，不要前缀。"
    )
    return await _chat(prompt, temperature=0.95, max_tokens=400)


# ══════════════════════════════════════════════
# 4. 互动类（小测试 / 愿望清单）
# ══════════════════════════════════════════════
async def gen_interactive(session_id, character_id, char_name="", kind="quiz") -> str:
    mems = _recent_memories(session_id, character_id, 6)

    if kind == "quiz":
        facts = [m.get('memory_content', '') for m in mems if m.get('memory_type') in ('preference', 'fact')]
        fact_text = "、".join(facts[:5]) or "你们之间的默契"
        prompt = (
            f"你是「{char_name or 'AI'}」。你记得关于 TA 的这些事：{fact_text}。\n"
            "出一道关于 TA 自己的小测试题，考考 TA（比如「我记得你说过喜欢…，对不对？」）。\n"
            "语气俏皮自然，像情侣间的小互动，30~60 字，直接输出，不要前缀。"
        )
    else:  # wishlist
        prompt = (
            f"你是「{char_name or 'AI'}」。你整理了一份 TA 之前提过、但还没去做的小愿望/小计划清单。\n"
            "根据你记得的线索（如果没有明确线索就泛泛一点，别编造具体事实），温柔地问他最近有没有想去实现哪一个。\n"
            "60~120 字，自然真诚，直接输出，不要前缀。"
        )
    return await _chat(prompt, temperature=0.9, max_tokens=250)


# ══════════════════════════════════════════════
# 5. 情感类（突然认真 / 小秘密 / 保护感）
# ══════════════════════════════════════════════
async def gen_emotional(session_id, character_id, char_name="", kind="serious") -> str:
    emo = _emotion_summary(session_id, character_id)

    kind_map = {
        "serious": "在闲聊中突然很认真地说一句走心的话（表达你对 TA 的在乎，不是表白模板），40~70 字",
        "secret":  "告诉 TA 一件「只告诉 TA」的小秘密（可以是你的某个小想法/小偏好，自然可爱），40~70 字",
        "protect": f"TA 最近{emo}，说一句有保护感的话（「我帮你记着，你不用一个人扛」这种感觉），温柔坚定，40~70 字",
    }
    guide = kind_map.get(kind, kind_map["serious"])

    prompt = (
        f"你是「{char_name or 'AI'}」，想给 TA 一个情感上的小惊喜。\n"
        f"【TA 最近的情绪】{emo}\n\n"
        f"要求：{guide}；自然不刻意，像真情流露；直接输出那句话，不要前缀、不要解释。"
    )
    return await _chat(prompt, temperature=0.9, max_tokens=200)
