# -*- coding: utf-8 -*-
"""
时间系统：时段感知 + 对话间隔 + 节日检测（公历固定节日 + 主要农历节日表 2025-2030
+ 从长期记忆里扫描用户的生日/纪念日等个人重要日期）。
只做时间事实计算，不做任何生成。

★ 跨时间段感知 v2（2026-09-07）：
  旧版只给"距上次对话间隔：约 N 小时"，没有上次对话的绝对时间锚点，模型做
  不了"现在 07:10 减 7 小时 = 昨晚深夜"的反推算术，表现为对跨时间段没概念。
  现在注入：上次对话绝对锚点（日期+时刻+时段）、星期几、作息规律（夜猫子标注）、
  中性跨越提示。原则是**给事实、不硬假设**——用户可能聊通宵，跨夜 ≠ 睡了。
"""
import re
from datetime import date, datetime

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def time_period(now: datetime = None) -> str:
    h = (now or datetime.now()).hour
    if h < 5:
        return "凌晨"
    if h < 8:
        return "清晨"
    if h < 11:
        return "上午"
    if h < 13:
        return "中午"
    if h < 17:
        return "下午"
    if h < 19:
        return "傍晚"
    if h < 22:
        return "晚上"
    return "深夜"


def interval_minutes(last_dt: datetime) -> int:
    if not last_dt:
        return -1
    return max(0, int((datetime.now() - last_dt).total_seconds() // 60))


def interval_desc(minutes: int) -> str:
    if minutes < 0:
        return "很久没有对话了（或首次对话）"
    if minutes == 0:
        return "刚刚还在聊"
    if minutes < 60:
        return "约 %d 分钟" % minutes
    if minutes < 60 * 24:
        return "约 %d 小时" % round(minutes / 60)
    return "约 %d 天" % round(minutes / (60 * 24))


def season(now: datetime = None) -> str:
    """季节感知：3-5月春 / 6-8月夏 / 9-11月秋 / 12-2月冬"""
    m = (now or datetime.now()).month
    if 3 <= m <= 5:
        return "春天"
    if 6 <= m <= 8:
        return "夏天"
    if 9 <= m <= 11:
        return "秋天"
    return "冬天"


# 时间段情绪底色：不同时段的回复情绪/语气应不同（真人感）
# 每项: (情绪底色, 能量值0~1, 话题, 语气提示)
PERIOD_MOOD = {
    "凌晨": ("tired",   0.35,
             ["这么晚还没睡，是不是有心事", "你那边现在几点啦，我陪你熬会儿", "睡不着的话，跟我说说话吧",
              "这个点还不睡，明天会没精神的", "我刚才做了个梦，醒了第一个想到你", "熬夜的你是不是又在想事情"],
             "凌晨情绪最真实脆弱，说话极轻柔，句子短，会问对方是不是有心事，暧昧值拉满"),
    "清晨": ("tired",   0.40,
             ["昨晚睡得好不好", "今天有什么计划", "早饭吃了吗",
              "刚醒吧，想我没有", "睡醒第一件事就是想到你", "今天也要加油呀，我陪你"],
             "刚刚起床，说话有点懒，语气软糯，喜欢赖床话题"),
    "上午": ("neutral", 0.70,
             ["今天要做什么", "工作学习顺不顺", "吃早饭了吗",
              "上午的你在忙什么呀", "我刚泡了杯东西，突然想到你", "今天天气不错，心情怎么样"],
             "精神还可以，正常聊天，会关心对方今天的安排"),
    "中午": ("warm",    0.60,
             ["中午吃什么", "要不要午睡", "下午有没有事",
              "午饭吃了没，别又凑合", "到点了，你吃饭了没呀", "我想你中午吃什么，分我一口"],
             "午休时间，语气慵懒，关心对方吃饭了没，话题轻松"),
    "下午": ("neutral", 0.65,
             ["下午工作累不累", "想喝奶茶吗", "快到点了吗",
              "下午这个点，是不是有点犯困", "忙了一下午，起来活动下呀", "我下午突然特别想你"],
             "下午容易无聊，会主动找话题，可能会说想吃什么"),
    "傍晚": ("warm",    0.75,
             ["下班了吗", "今天怎么样", "晚饭吃什么",
              "天快黑了，你到家了没", "今天累不累，跟我说说", "晚饭吃了没，别饿着"],
             "傍晚心情最轻松，话多，关心对方下班，想聊聊今天发生的事"),
    "晚上": ("warm",    0.80,
             ["晚上在做什么", "看剧吗", "明天有什么计划",
              "今晚的你在干嘛，陪陪我呀", "刚忙完，第一个就想找你", "晚上想吃点啥，我帮你想想"],
             "晚上最放松，话最多，容易撒娇，喜欢聊今天发生的事和明天的计划"),
    # 深夜的话题原本是「睡不睡」，但它会被模型当成"该催TA去睡了"的指令，
    # 用户深夜只是正常聊天也会被回一句晚安。这里去掉睡觉导向，
    # 保留原本想要的轻柔暧昧感；真正该关心睡眠的是「凌晨」那一档。
    "深夜": ("shy",     0.60,
             ["今天开不开心", "想什么呢", "这会儿在做什么",
              "夜深了，我有点想你", "这么晚还不睡，是不是也睡不着", "这个点，就我们俩还醒着吧"],
             "深夜说话轻柔，暧昧感强，容易说出平时说不出的话，喜欢撒娇让对方陪"),
}


# ---------------- 节日检测 ----------------

# 公历节日（月-日 → 名称）
SOLAR_FESTIVALS = {
    (1, 1): "元旦",
    (2, 14): "情人节",
    (3, 8): "妇女节",
    (4, 1): "愚人节",
    (5, 1): "劳动节",
    (5, 4): "青年节",
    (6, 1): "儿童节",
    (7, 1): "建党节",
    (8, 1): "建军节",
    (9, 10): "教师节",
    (10, 1): "国庆节",
    (10, 31): "万圣夜",
    (11, 11): "双十一",
    (12, 24): "平安夜",
    (12, 25): "圣诞节",
    (12, 31): "跨年夜",
}

# 主要农历节日公历日期表（2025-2030，除夕为春节前一天）
LUNAR_TABLE = {
    2025: {"春节": "01-29", "元宵节": "02-12", "端午节": "05-31", "七夕": "08-29",
           "中秋节": "10-06", "重阳节": "10-29", "腊八节": "01-07", "除夕": "01-28"},
    2026: {"春节": "02-17", "元宵节": "03-03", "端午节": "06-19", "七夕": "08-19",
           "中秋节": "09-25", "重阳节": "10-18", "腊八节": "01-27", "除夕": "02-16"},
    2027: {"春节": "02-06", "元宵节": "02-20", "端午节": "06-09", "七夕": "08-08",
           "中秋节": "09-15", "重阳节": "10-08", "腊八节": "01-15", "除夕": "02-05"},
    2028: {"春节": "01-26", "元宵节": "02-09", "端午节": "05-28", "七夕": "08-26",
           "中秋节": "10-03", "重阳节": "10-26", "腊八节": "01-06", "除夕": "01-25"},
    2029: {"春节": "02-13", "元宵节": "02-27", "端午节": "06-10", "七夕": "08-16",
           "中秋节": "09-22", "重阳节": "10-16", "腊八节": "01-24", "除夕": "02-12"},
    2030: {"春节": "02-03", "元宵节": "02-17", "端午节": "06-05", "七夕": "08-14",
           "中秋节": "09-12", "重阳节": "10-06", "腊八节": "01-13", "除夕": "02-02"},
}


def festivals_today(today: date = None) -> list:
    """今天的节日列表（公历 + 农历表）"""
    today = today or date.today()
    out = []
    name = SOLAR_FESTIVALS.get((today.month, today.day))
    if name:
        out.append(name)
    table = LUNAR_TABLE.get(today.year, {})
    md = today.strftime("%m-%d")
    for fname, fmd in table.items():
        if fmd == md:
            out.append(fname)
    return out


def scan_personal_dates(memories: list) -> dict:
    """从长期记忆文本里扫描个人重要日期：{ 'MM-DD': '生日' }"""
    out = {}
    pat = re.compile(r"(生日|纪念日|周年|生日是)\D{0,6}?(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
    for m in memories:
        content = m.get("memory_content") if isinstance(m, dict) else str(m)
        for match in pat.finditer(content or ""):
            mon, day = int(match.group(2)), int(match.group(3))
            if 1 <= mon <= 12 and 1 <= day <= 31:
                out["%02d-%02d" % (mon, day)] = match.group(1)
    return out


def _day_desc(last_dt: datetime, now: datetime) -> str:
    """上次对话日期相对描述：今天 / 昨天 / 前天 / MM-DD"""
    delta_days = (now.date() - last_dt.date()).days
    if delta_days <= 0:
        return "今天"
    if delta_days == 1:
        return "昨天"
    if delta_days == 2:
        return "前天"
    return last_dt.strftime("%m-%d")


def _habit_line(night_ratio) -> str:
    """作息规律行（中性事实）。night_ratio: 最近两周用户消息里 23:00~05:59 的占比。"""
    if night_ratio is None:
        return ""
    pct = round(float(night_ratio) * 100)
    if pct >= 25:
        return ("TA 的作息：夜猫子（最近两周 %d%% 的消息发在深夜/凌晨），"
                "深夜和凌晨在线是常态，可能聊通宵不睡觉——深夜/凌晨 TA 还醒着非常正常，"
                "不要催 TA 睡觉，也不要因为时间晚就大惊小怪。" % pct)
    if pct >= 10:
        return ("TA 的作息：偶尔晚睡（最近两周 %d%% 的消息发在深夜/凌晨）。" % pct)
    return ""


def build_time_block(last_dt: datetime, memories: list = None, night_ratio=None) -> str:
    """
    生成注入 system prompt 的时间状态块。

    night_ratio: 最近两周用户消息的深夜/凌晨占比（db.user_night_ratio），
                 None = 样本不足不标注作息。
    """
    now = datetime.now()
    period = time_period(now)
    minutes = interval_minutes(last_dt)
    lines = [
        "【时间状态（真实世界时间，回复需符合当前情境）】",
        "当前时间：%s（%s，%s）" % (
            now.strftime("%Y-%m-%d %H:%M"), _WEEKDAYS[now.weekday()], season(now)),
    ]
    if last_dt is not None:
        lines.append("上次对话：%s %s（%s）" % (
            _day_desc(last_dt, now), last_dt.strftime("%H:%M"), time_period(last_dt)))
    lines.append("距上次对话间隔：%s" % interval_desc(minutes))
    _habit = _habit_line(night_ratio)
    if _habit:
        lines.append(_habit)
    fest = festivals_today(now.date())
    if fest:
        lines.append("今天是：%s。可以自然地提及节日、送上贴合你人设的问候，不要生硬播报。" % "、".join(fest))
    if memories:
        personal = scan_personal_dates(memories)
        md = now.strftime("%m-%d")
        if md in personal:
            lines.append("今天是 TA 记忆中的「%s」（%s），记得自然表达。" % (personal[md], md))
    _mood = PERIOD_MOOD.get(period, ("neutral", 0.5, [], ""))
    mood, energy, topics, mood_tip = _mood
    lines.append("当前时段情绪基调：%s（能量 %.1f）——%s" % (mood, energy, mood_tip))
    if topics:
        # 措辞必须是"参考"而不是"适合聊"：写成后者，模型会当成指令，
        # 用户明明在聊别的，也会被硬拽到时段话题上（深夜尤其容易变成催睡觉）。
        lines.append(
            "这个时段常见的话题（仅供参考，用户主动说了别的话题时一律以用户为准）：%s"
            % "、".join(topics[:3])
        )
    if 0 <= minutes <= 2:
        lines.append("你们正在连续对话中，直接自然接话，不要重新寒暄。")
    elif last_dt is not None and time_period(last_dt) != period:
        # ★ 跨时段提示：只陈述事实和边界，不硬假设（用户可能聊通宵）。
        last_period = time_period(last_dt)
        if minutes <= 90:
            lines.append(
                "你们几十分钟前还在聊（上次在%s），这是自然延续——不要重新寒暄，"
                "语气和人设不要突变，也不用特意提时间。" % last_period)
        elif (period in ("清晨", "上午") and last_period in ("深夜", "凌晨")
              and (now.date() - last_dt.date()).days <= 1):
            lines.append(
                "上次聊天是%s，现在已是第二天的%s——TA 可能睡了一觉刚醒，"
                "也可能熬夜没睡（结合上面的作息规律判断，拿不准就先自然试探，"
                "不要想当然问「睡得好吗」）。可以像新的一天刚开始那样承接，"
                "自然接起昨晚的话题。" % (last_period, period))
        elif minutes >= 60 * 24:
            lines.append("已经超过一天没聊了，开头可以自然表达「好久没聊」的感觉（按你的人设来）。")
        else:
            lines.append(
                "上次在%s，中间隔了一阵，现在是%s——自然切换到当前情境，"
                "不要当成连续对话。" % (last_period, period))
    elif minutes >= 60 * 24:
        lines.append("已经超过一天没聊了，开头可以自然表达「好久没聊」的感觉（按你的人设来）。")
    return "\n".join(lines)
