# -*- coding: utf-8 -*-
"""
情绪时间线 · 连贯情绪提升系统核心（结合优化）
复用「连贯情绪升级」的时间序列/趋势/streak 思想，适配本项目架构：
  - 数据源：emotion_manager.update_emotion 的对话情绪分析（mood/intensity/reason/needs）
  - 每次对话后追加快照到 emotion_timeline 表
  - 趋势分析（improving/declining/stable）+ 连续低落 streak + 干预判断
  - 输出「情绪趋势块」注入 system prompt（供 AI 感知情绪走向，自然调整）
"""
from datetime import datetime

from .. import db


# 中文 mood → 情绪分数（越高越积极，-1~1）
MOOD_SCORE = {
    "开心": 1.0,
    "兴奋": 0.8,
    "期待": 0.6,
    "放松": 0.4,
    "平静": 0.3,
    "孤独": -0.4,
    "疲惫": -0.3,
    "压力": -0.5,
    "焦虑": -0.6,
    "失落": -0.6,
    "烦躁": -0.5,
    "委屈": -0.5,
    "生气": -0.7,
    "难过": -0.8,
}

TREND_UP_THRESHOLD = 0.2
TREND_DOWN_THRESHOLD = -0.2


def score_of(mood: str) -> float:
    return MOOD_SCORE.get(str(mood or "").strip(), 0.0)


def append(session_id, character_id, mood, intensity=0, reason="", needs=""):
    """追加快照到时间线。返回 score。"""
    score = score_of(mood)
    try:
        db.q(
            "INSERT INTO emotion_timeline(session_id, character_id, mood, intensity, score, reason, needs, ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (session_id, character_id, str(mood or ""), int(intensity or 0),
             score, str(reason or ""), str(needs or ""),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
    except Exception as e:
        print(f"[EmotionTimeline] 追加快照失败: {e}", flush=True)
    return score


def get_recent(session_id, character_id, n=10):
    rows = db.q(
        "SELECT mood, intensity, score, reason, needs, ts FROM emotion_timeline "
        "WHERE session_id=? AND character_id=? ORDER BY id DESC LIMIT ?",
        (session_id, character_id, n), fetch=True)
    return list(reversed(rows))


def _scores(session_id, character_id, n=10):
    return [r["score"] for r in get_recent(session_id, character_id, n)]


def current_mood(session_id, character_id):
    rows = get_recent(session_id, character_id, 1)
    return rows[-1]["mood"] if rows else ""


def current_score(session_id, character_id):
    rows = get_recent(session_id, character_id, 1)
    return rows[-1]["score"] if rows else 0.0


def average_score(session_id, character_id, n=5):
    s = _scores(session_id, character_id, n)
    return sum(s) / len(s) if s else 0.0


def trend(session_id, character_id, short=3, long=8):
    """短期均值 vs 长期均值 → improving/declining/stable/unknown。"""
    s = _scores(session_id, character_id, long)
    if len(s) < long:
        return "unknown"
    short_avg = sum(s[-short:]) / short
    long_avg = sum(s) / long
    delta = short_avg - long_avg
    if delta >= TREND_UP_THRESHOLD:
        return "improving"
    if delta <= TREND_DOWN_THRESHOLD:
        return "declining"
    return "stable"


def low_streak(session_id, character_id):
    """连续低落次数（从最新往回数 score<0）。"""
    rows = get_recent(session_id, character_id, 30)
    n = 0
    for r in reversed(rows):
        if r["score"] < 0:
            n += 1
        else:
            break
    return n


def high_streak(session_id, character_id):
    rows = get_recent(session_id, character_id, 30)
    n = 0
    for r in reversed(rows):
        if r["score"] >= 0.5:
            n += 1
        else:
            break
    return n


def needs_intervention(session_id, character_id):
    ls = low_streak(session_id, character_id)
    cs = current_score(session_id, character_id)
    tr = trend(session_id, character_id)
    if ls >= 4:
        return True, f"连续{ls}次检测到低落情绪"
    if cs <= -0.5:
        return True, f"当前情绪得分过低({cs:.1f})"
    if tr == "declining" and cs < 0:
        return True, "情绪持续下滑且当前为负"
    return False, ""


def summary(session_id, character_id):
    _need, _reason = needs_intervention(session_id, character_id)
    return {
        "current_mood": current_mood(session_id, character_id),
        "current_score": round(current_score(session_id, character_id), 2),
        "average_score": round(average_score(session_id, character_id), 2),
        "trend": trend(session_id, character_id),
        "low_streak": low_streak(session_id, character_id),
        "high_streak": high_streak(session_id, character_id),
        "needs_intervention": _need,
        "intervention_reason": _reason,
    }


# 情绪 → 人格指令（供 AI 感知，只给内心参考，严禁直说）
MOOD_PERSONA_HINTS = {
    "开心": "用户现在心情不错，配合他的能量，活泼一点，分享他的快乐，让这个状态持续更久",
    "兴奋": "用户很兴奋，跟着他一起开心，情绪拉满",
    "期待": "用户在期待什么，可以好奇地问问，陪他一起期待",
    "放松": "用户很放松，可以轻松聊天，适当撒娇",
    "平静": "用户状态平稳，自然对话即可",
    "孤独": "用户可能感到孤独，多陪伴，多互动，让他感觉你在",
    "疲惫": "用户很累，语气温柔，让他好好休息，别问太多",
    "压力": "用户压力大，温柔安抚，不要催促，先共情再建议",
    "焦虑": "用户有些焦虑，用平稳的语气给他稳定感，说话简短有力",
    "难过": "用户情绪低落，不要急着解决，不要说没事的，就陪着他",
    "生气": "用户生气了，先安抚情绪，别讲道理，等他冷静",
    "失落": "用户有些失落，温柔安慰，给他信心",
    "烦躁": "用户烦躁，别添乱，安静一点，简短回应",
    "委屈": "用户委屈，站在他这边，先共情",
}


def build_prompt_block(session_id, character_id):
    """生成情绪趋势块，注入 system prompt。无足够数据时返回空。"""
    rows = get_recent(session_id, character_id, 3)
    if not rows:
        return ""
    mood = rows[-1]["mood"]
    tr = trend(session_id, character_id)
    ls = low_streak(session_id, character_id)
    hint = MOOD_PERSONA_HINTS.get(mood, "")

    lines = ["【用户情绪感知（连续追踪，仅供你内心参考，严禁直说）】"]
    if hint:
        lines.append(f"- 当前：{hint}")
    if tr == "declining":
        lines.append("- ⚠️ 情绪正在下滑：这轮对话很重要，主动带来温暖，把情绪往好的方向引")
    elif tr == "improving":
        lines.append("- ✅ 情绪在好转：给正向反馈，强化这种好起来的感受")
    if ls >= 4:
        lines.append(f"- 🔴 已连续 {ls} 次低落：这轮请全力陪伴和温暖，让他感到你真的很在乎")
    lines.append("【红线】自然融入，不要说出「检测到/分析出你情绪」这类话术。")
    return "\n".join(lines)
