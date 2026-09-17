# -*- coding:utf-8 -*-
"""
Behavior Analyzer v1.0
行为分析器：

  从聊天记录中分析用户的行为模式：
    1. 时间习惯（用户通常在什么时间聊天）
    2. 情绪趋势（用户近期情绪变化方向）
    3. 沟通行为（用户在不同状态下的沟通模式）
"""
from datetime import datetime
from collections import Counter


def analyze_time_pattern(messages):
    """
    分析用户的时间习惯。

    Args:
        messages: 消息列表

    Returns:
        dict or None: 时间模式分析结果
    """
    hours = []
    weekdays = []

    for m in messages:
        t = m.get("create_time") or m.get("timestamp")
        if not t:
            continue

        try:
            # 尝试多种时间格式
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
            hours.append(dt.hour)
            weekdays.append(dt.weekday())
        except Exception:
            continue

    if len(hours) < 10:
        return None

    # 平均聊天时间
    avg_hour = sum(hours) / len(hours)

    # 最活跃的时间段
    hour_counts = Counter(hours)
    peak_hour = hour_counts.most_common(1)[0][0]

    # 最活跃的星期
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday_counts = Counter(weekdays)
    peak_weekday = weekday_names[weekday_counts.most_common(1)[0][0]]

    # 判断时间段
    if avg_hour < 6:
        time_period = "深夜"
    elif avg_hour < 12:
        time_period = "上午"
    elif avg_hour < 18:
        time_period = "下午"
    else:
        time_period = "晚上"

    weekend_count = sum(1 for d in weekdays if d >= 5)
    weekend_ratio = weekend_count / max(1, len(weekdays))
    weekend_hint = "，周末明显更活跃" if weekend_ratio >= 0.45 else ""
    content = (
        f"用户通常在{time_period}（平均{int(avg_hour)}点）交流，"
        f"最活跃时间是{peak_hour}点，{peak_weekday}聊天最多{weekend_hint}"
    )

    return {
        "type": "time_pattern",
        "pattern": content,
        "confidence": min(0.5 + len(hours) * 0.03, 0.95),
        "details": {
            "avg_hour": avg_hour,
            "peak_hour": peak_hour,
            "peak_weekday": peak_weekday,
            "time_period": time_period,
            "weekend_ratio": weekend_ratio,
        }
    }


def analyze_emotion_trend(emotion_records):
    """
    分析用户的情绪趋势。

    Args:
        emotion_records: 情绪记录列表

    Returns:
        dict or None: 情绪趋势分析结果
    """
    if not emotion_records or len(emotion_records) < 3:
        return None

    moods = [e.get("mood", "") for e in emotion_records if e.get("mood")]
    intensities = [e.get("intensity", 0) for e in emotion_records]

    if not moods:
        return None

    # 最近情绪
    recent_moods = moods[-5:]
    mood_counts = Counter(recent_moods)
    dominant_mood = mood_counts.most_common(1)[0][0]

    # 情绪强度趋势
    if len(intensities) >= 4:
        old_avg = sum(intensities[:len(intensities)//2]) / (len(intensities)//2)
        new_avg = sum(intensities[len(intensities)//2:]) / (len(intensities) - len(intensities)//2)
        trend = "上升" if new_avg > old_avg + 1 else ("下降" if new_avg < old_avg - 1 else "稳定")
    else:
        trend = "稳定"

    # 负面情绪判断
    negative_moods = ["sad", "anxious", "angry", "压力", "难过", "焦虑", "生气", "疲惫"]
    negative_count = sum(1 for m in recent_moods if m in negative_moods)
    is_stressed = negative_count >= len(recent_moods) * 0.6

    if is_stressed:
        content = f"用户近期可能持续处于{dominant_mood}状态，情绪强度{trend}"
        pattern_type = "stress_period"
    else:
        content = f"用户近期主要情绪是{dominant_mood}，情绪状态{trend}"
        pattern_type = "emotion_trend"

    return {
        "type": pattern_type,
        "pattern": content,
        "confidence": min(0.5 + len(moods) * 0.05, 0.9),
        "details": {
            "dominant_mood": dominant_mood,
            "trend": trend,
            "is_stressed": is_stressed,
        }
    }


def analyze_communication_pattern(messages):
    """
    分析用户的沟通行为模式。

    Args:
        messages: 消息列表

    Returns:
        dict or None: 沟通模式分析结果
    """
    if not messages or len(messages) < 10:
        return None

    user_messages = [m for m in messages if m.get("role") == "user"]

    if not user_messages:
        return None

    # 平均消息长度
    lengths = [len(str(m.get("content", ""))) for m in user_messages]
    avg_length = sum(lengths) / len(lengths)

    # 消息频率
    if len(user_messages) >= 2:
        try:
            times = []
            for m in user_messages:
                t = m.get("create_time") or m.get("timestamp")
                if t:
                    if "T" in str(t):
                        times.append(datetime.strptime(str(t), "%Y-%m-%dT%H:%M:%S"))
                    else:
                        times.append(datetime.strptime(str(t), "%Y-%m-%d %H:%M:%S"))
            if len(times) >= 2:
                intervals = [(times[i+1] - times[i]).total_seconds() / 60 for i in range(len(times)-1)]
                avg_interval = sum(intervals) / len(intervals)
            else:
                avg_interval = 0
        except Exception:
            avg_interval = 0
    else:
        avg_interval = 0

    # 判断沟通风格
    if avg_length < 10:
        style = "简短直接"
    elif avg_length < 30:
        style = "适中"
    else:
        style = "详细倾诉"

    content = (
        f"用户沟通风格{style}（平均消息{int(avg_length)}字），"
        f"消息间隔约{int(avg_interval)}分钟"
    )

    return {
        "type": "communication_pattern",
        "pattern": content,
        "confidence": min(0.5 + len(user_messages) * 0.02, 0.85),
        "details": {
            "avg_length": avg_length,
            "avg_interval": avg_interval,
            "style": style,
        }
    }


def analyze_all_patterns(messages, emotion_records=None):
    """
    执行所有行为分析。

    Args:
        messages: 消息列表
        emotion_records: 情绪记录列表（可选）

    Returns:
        list: 分析结果列表
    """
    results = []

    # 时间模式
    time_pattern = analyze_time_pattern(messages)
    if time_pattern:
        results.append(time_pattern)

    # 情绪趋势
    if emotion_records:
        emotion_pattern = analyze_emotion_trend(emotion_records)
        if emotion_pattern:
            results.append(emotion_pattern)

    # 沟通模式
    comm_pattern = analyze_communication_pattern(messages)
    if comm_pattern:
        results.append(comm_pattern)

    return results
