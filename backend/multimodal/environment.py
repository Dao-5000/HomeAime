# -*- coding:utf-8 -*-
"""
Environment Analyzer v1.0
环境感知分析器：

  获取当前时间、时间段、星期等环境信息，
  用于辅助 AI 理解用户当前可能的状态。
"""
from datetime import datetime


def get_environment():
    """
    获取当前环境信息。

    Returns:
        dict: 环境信息 {time, period, weekday, date, ...}
    """
    now = datetime.now()

    # 时间段
    hour = now.hour
    if 5 <= hour < 9:
        period = "morning"
        period_cn = "早晨"
    elif 9 <= hour < 12:
        period = "forenoon"
        period_cn = "上午"
    elif 12 <= hour < 14:
        period = "noon"
        period_cn = "中午"
    elif 14 <= hour < 18:
        period = "afternoon"
        period_cn = "下午"
    elif 18 <= hour < 22:
        period = "evening"
        period_cn = "晚上"
    else:
        period = "night"
        period_cn = "深夜"

    # 星期
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    weekday = weekdays[now.weekday()]
    is_weekend = now.weekday() >= 5

    # 是否工作时间
    is_work_hours = 9 <= hour < 18 and not is_weekend

    # 是否休息时间
    is_rest_hours = hour >= 22 or hour < 7

    # 是否用餐时间
    is_meal_time = (
        (7 <= hour < 9) or  # 早餐
        (11 <= hour < 13) or  # 午餐
        (17 <= hour < 19)  # 晚餐
    )

    return {
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "period": period,
        "period_cn": period_cn,
        "weekday": weekday,
        "is_weekend": is_weekend,
        "is_work_hours": is_work_hours,
        "is_rest_hours": is_rest_hours,
        "is_meal_time": is_meal_time,
        "hour": hour,
        "minute": now.minute,
    }


def get_time_context():
    """
    获取时间上下文描述（用于 Prompt）。

    Returns:
        str: 时间上下文描述
    """
    env = get_environment()
    parts = []

    parts.append(f"现在是{env['weekday']}{env['period_cn']}{env['time']}")

    if env["is_weekend"]:
        parts.append("今天是周末")
    elif env["is_work_hours"]:
        parts.append("当前是工作时间")
    elif env["is_rest_hours"]:
        parts.append("已经是休息时间")

    if env["is_meal_time"]:
        parts.append("接近用餐时间")

    return "，".join(parts)


def get_proactive_time_suggestion():
    """
    获取主动消息的时间建议。

    Returns:
        dict: {best_time, reason}
    """
    env = get_environment()
    hour = env["hour"]

    # 最佳主动时间
    if 8 <= hour < 10:
        return {
            "best_time": "morning_greeting",
            "reason": "早晨适合轻松问候",
        }
    elif 12 <= hour < 14:
        return {
            "best_time": "lunch_check",
            "reason": "午餐时间适合关心吃饭",
        }
    elif 18 <= hour < 20:
        return {
            "best_time": "evening_check",
            "reason": "傍晚适合关心一天的情况",
        }
    elif 21 <= hour < 23:
        return {
            "best_time": "night_chat",
            "reason": "晚上适合深度聊天",
        }
    elif hour >= 23 or hour < 6:
        return {
            "best_time": "avoid",
            "reason": "深夜不适合主动打扰",
        }
    else:
        return {
            "best_time": "casual",
            "reason": "普通时间适合轻松问候",
        }


def get_user_likely_state():
    """
    根据时间推测用户可能的状态。

    Returns:
        str: 用户可能的状态描述
    """
    env = get_environment()
    hour = env["hour"]

    if 6 <= hour < 9:
        return "用户可能刚起床，准备开始一天"
    elif 9 <= hour < 12:
        return "用户可能在工作/学习"
    elif 12 <= hour < 14:
        return "用户可能在吃午饭/休息"
    elif 14 <= hour < 18:
        return "用户可能在工作/学习"
    elif 18 <= hour < 20:
        return "用户可能在下班/吃晚饭"
    elif 20 <= hour < 23:
        return "用户可能在放松/休息"
    else:
        return "用户可能已经休息，注意不要打扰"
