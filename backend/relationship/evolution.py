# -*- coding:utf-8 -*-
"""
关系阶段进化系统：
  根据亲密度计算关系阶段，从陌生人到灵魂伴侣。
  每个阶段有不同的互动策略和语气要求。
"""


# 关系阶段定义
STAGES = {
    "stranger": {
        "name": "陌生人",
        "min_intimacy": 0,
        "description": "刚认识，保持礼貌和距离",
        "tone": "礼貌、友好、不过度亲密",
        "boundary": "不要使用过于亲密的称呼，不要主动提及私人话题"
    },
    "friend": {
        "name": "朋友",
        "min_intimacy": 20,
        "description": "熟悉的朋友，可以轻松聊天",
        "tone": "轻松、自然、关心但不过界",
        "boundary": "可以关心日常生活，但不要过度干涉"
    },
    "close_friend": {
        "name": "亲密朋友",
        "min_intimacy": 40,
        "description": "关系亲密，可以分享心事",
        "tone": "温暖、体贴、可以表达关心",
        "boundary": "可以使用亲近的称呼，但不要突然变成恋人语气"
    },
    "lover": {
        "name": "恋人",
        "min_intimacy": 70,
        "description": "恋人关系，可以亲密互动",
        "tone": "温柔、宠溺、可以撒娇和表达想念",
        "boundary": "可以使用恋人称呼，自然表达关心和想念"
    },
    "soulmate": {
        "name": "灵魂伴侣",
        "min_intimacy": 90,
        "description": "深度契合，默契十足",
        "tone": "自然、默契、像家人一样的亲密",
        "boundary": "可以深度交流，有高度的默契和理解"
    }
}


def calculate_stage(intimacy):
    """
    根据亲密度计算关系阶段。
    返回阶段标识字符串。
    """
    if intimacy < 20:
        return "stranger"
    if intimacy < 40:
        return "friend"
    if intimacy < 70:
        return "close_friend"
    if intimacy < 90:
        return "lover"
    return "soulmate"


def get_stage_info(stage):
    """获取阶段的详细信息"""
    return STAGES.get(stage, STAGES["stranger"])


def get_stage_name(stage):
    """获取阶段的中文名称"""
    info = get_stage_info(stage)
    return info["name"]


def get_stage_tone(stage):
    """获取阶段的语气建议"""
    info = get_stage_info(stage)
    return info["tone"]


def get_stage_boundary(stage):
    """获取阶段的边界提示"""
    info = get_stage_info(stage)
    return info["boundary"]


def get_all_stages():
    """获取所有阶段定义"""
    return STAGES


def calculate_stage_progress(intimacy):
    """
    计算当前阶段的进度百分比。
    返回 (当前阶段, 进度0-100)
    """
    stage = calculate_stage(intimacy)
    info = get_stage_info(stage)
    min_val = info["min_intimacy"]

    # 找到下一个阶段的最小值
    stages_sorted = sorted(STAGES.items(), key=lambda x: x[1]["min_intimacy"])
    next_min = 100
    for s, s_info in stages_sorted:
        if s_info["min_intimacy"] > min_val:
            next_min = s_info["min_intimacy"]
            break

    if next_min == min_val:
        return stage, 100

    progress = (intimacy - min_val) / (next_min - min_val) * 100
    return stage, max(0, min(100, int(progress)))
