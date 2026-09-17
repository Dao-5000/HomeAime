# -*- coding: utf-8 -*-
"""LLM 递归总结：日总结 / 周总结 / 月总结。

递归链：原文 → 日总结 → 周总结 → 月总结。
模型用 MEMORY_EXTRACT_MODEL（默认 glm-5.3-flash），key 按 provider 自动分流。
"""
from datetime import datetime, timedelta

from .. import config
from . import paths


def _sum_model(character_id: str = "") -> str:
    # ★ 2026-09-11：角色卡 memory_model 优先（分层大脑归属），无卡回退全局
    return config.memory_extract_model(character_id)


def _sum_key() -> str:
    try:
        return config.api_key_for_model(_sum_model())
    except Exception:
        return ""


async def _llm(prompt: str, max_tokens: int = 1200, character_id: str = "") -> str:
    from ..deepseek_api import chat_once
    key = _sum_key()
    if not key:
        return ""
    try:
        # ★ 2026-09-15：日/周/月总结是"合法的慢杂活"——真机实测单次 24.62s（00:04），
        #   离杂活默认档 30s 太近，显式声明长档，避免被守卫层误杀。
        from .. import llm_guard as _lg
        out = await chat_once(
            _sum_model(character_id),
            [{"role": "user", "content": prompt}],
            key, temperature=0.3, max_tokens=max_tokens,
            hard_timeout=_lg.long_timeout_sec(),
        )
        return str(out or "").strip()
    except Exception:
        return ""


def _read(fp) -> str:
    try:
        return fp.read_text("utf-8")
    except Exception:
        return ""


def iso_week(date: datetime) -> str:
    y, w, _ = date.isocalendar()
    return f"{y}-W{w:02d}"


def week_start(week_key: str):
    """'2026-W36' → 该周周一（datetime），解析失败返回 None。"""
    try:
        y, w = str(week_key).split("-W")
        return datetime.strptime(f"{y}-W{int(w):02d}-1", "%G-W%V-%u")
    except Exception:
        return None


def weeks_of_days(character_id: str) -> list:
    """列出「有日总结、但还没生成周总结」的那些周，按时间从早到晚。

    ★ 2026-09-13：原来只补「上一自然周」（today - weekday - 7），App 连续
      停机两周以上时，中间整周永远不会被总结 —— 而周总结是月总结唯一的输入，
      漏一周等于那段时间在长期记忆里彻底消失。这里改成扫描所有日总结覆盖到的周，
      缺哪周补哪周（幂等靠 state 里的 weekly_summarized）。
    """
    done = set(paths.load_state(character_id).get("weekly_summarized") or [])
    daily_dir = paths.daily_dir(character_id)
    if not daily_dir.exists():
        return []
    # 本周还没过完，不总结
    this_week = iso_week(datetime.now())
    seen = {}
    for fp in sorted(daily_dir.glob("*.md")):
        try:
            d = datetime.strptime(fp.stem, "%Y-%m-%d")
        except Exception:
            continue
        wk = iso_week(d)
        if wk in done or wk == this_week:
            continue
        # 同一周记最早的周一
        if wk not in seen:
            seen[wk] = d
    return sorted(seen.keys())


async def summarize_day(session_id: str, character_id: str, day: str) -> bool:
    """生成某天（YYYY-MM-DD）的日总结。"""
    raw = _read(paths.raw_dir(character_id) / (day + ".md")).strip()
    if not raw:
        return False
    prompt = (
        f"下面是 TA 和用户在某一天（{day}）的聊天记录。请提炼成一份精炼的「当日记忆总结」，"
        f"用第一人称（AI 视角）写，保留：①当天关键事件/话题；②用户透露的新信息、情绪、偏好；"
        f"③重要约定、承诺、感情进展；④值得长期记住的细节。\n"
        f"★ 专有名词必须原样写出来：人名/昵称、宠物名、地点、物品、约定里的具体内容、"
        f"日期时间数字。凡是写成「取了个名字」「约定了某件事」却没写清具体是什么的，"
        f"都算记录失败——这份总结之后还会被周、月总结逐层压缩，细节在这里丢了就永远找不回来。\n"
        f"300 字以内，必须是完整句子（宁可少写一条，也不要写到一半断掉），"
        f"只输出正文，不要标题、不要解释。\n\n聊天记录：\n{raw[-6000:]}"
    )
    # ★ 1800（原 1000）：实测线上 9-05 的日总结在「还能远程帮他」处被拦腰截断，
    #   而这份残缺文本会被周/月总结继续引用 —— 按更长的正文 + 完整句子要求给足额度。
    out = await _llm(prompt, 1800, character_id=character_id)
    if not out:
        return False
    try:
        paths.ensure_dirs(character_id)
        (paths.daily_dir(character_id) / (day + ".md")).write_text(out, "utf-8")
        return True
    except Exception:
        return False


async def summarize_week(character_id: str, week_start: datetime) -> bool:
    """生成某周（week_start 为周一）的周总结，压缩 7 份日总结。"""
    blocks = []
    for i in range(7):
        d = (week_start + timedelta(days=i)).strftime("%Y-%m-%d")
        fp = paths.daily_dir(character_id) / (d + ".md")
        txt = _read(fp).strip()
        if txt:
            blocks.append(f"【{d}】\n{txt}")
    if not blocks:
        return False
    week_key = iso_week(week_start)
    prompt = (
        f"下面是一周内每天的「当日记忆总结」。请压缩成一份「周总结」，"
        f"保留最重要的：感情进展、关键事件、用户的重要信息与偏好、约定与承诺。"
        f"500 字以内，只输出正文。\n\n" + "\n\n".join(blocks)
    )
    out = await _llm(prompt, 1500, character_id=character_id)
    if not out:
        return False
    try:
        (paths.weekly_dir(character_id) / (week_key + ".md")).write_text(out, "utf-8")
        return True
    except Exception:
        return False


def _weeks_in_month(character_id: str, month: str) -> list:
    """列出属于某个月（YYYY-MM）的周总结文件，按周序返回 [(week_key, 文本)]。

    ★ 归属月份按「周中」（周四）判定，不按周一。
      否则跨月的周会被整体算进上个月 —— 实测 2026-W36（周一 8-31、周日 9-06）
      按周一会归到 8 月，导致 9 月一份周总结都凑不出来（要等到 W40 才有），
      而线上那份 2026-08.md 的月总结，输入正是只有一份 W35。
    月总结与「月总结门槛」共用这一份匹配逻辑，避免两处判断不一致。
    """
    out = []
    for fp in sorted(paths.weekly_dir(character_id).glob("*.md")):
        wk = fp.stem  # 2026-W36
        d = week_start(wk)
        if d is None:
            continue
        if (d + timedelta(days=3)).strftime("%Y-%m") != month:
            continue
        txt = _read(fp).strip()
        if txt:
            out.append((wk, txt))
    return out


def count_week_files(character_id: str, month: str) -> int:
    """某个月里已生成的周总结份数（供 service 判断月总结值不值得做）。"""
    try:
        return len(_weeks_in_month(character_id, month))
    except Exception:
        return 0


async def summarize_month(character_id: str, month: str) -> bool:
    """生成某月（YYYY-MM）的月总结，压缩该月内的周总结。"""
    blocks = [f"【{wk}】\n{txt}" for wk, txt in _weeks_in_month(character_id, month)]
    if not blocks:
        return False
    prompt = (
        f"下面是一个月内各周的「周总结」。请压缩成一份「月总结」，"
        f"提炼这个月的感情主线、重要转折、用户的核心变化与长期偏好。\n"
        f"★ 人名/昵称、宠物名、地点、约定与承诺里的具体内容要原样保留，不要概括成"
        f"「取了个名字」「约定了某件事」这类空话。\n"
        f"600 字以内，必须是完整句子，只输出正文。\n\n" + "\n\n".join(blocks)
    )
    out = await _llm(prompt, 2200, character_id=character_id)
    if not out:
        return False
    try:
        (paths.monthly_dir(character_id) / (month + ".md")).write_text(out, "utf-8")
        return True
    except Exception:
        return False
