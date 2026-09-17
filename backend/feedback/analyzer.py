# -*- coding:utf-8 -*-
"""
Feedback Analyzer v1.0
反馈分析器：

  分析用户行为，提取隐式反馈信号。
  用户不会每次点赞，所以需要通过行为判断反馈。
"""


def analyze_behavior(before, after):
    """
    分析用户行为变化。

    Args:
        before: 之前的行为状态
        after: 之后的行为状态

    Returns:
        dict: 分析结果
    """
    result = {}

    if not before or not after:
        return result

    # 回复长度增加 → 高参与度
    before_length = before.get("reply_length", 0)
    after_length = after.get("reply_length", 0)

    if after_length > before_length * 1.3 and after_length > 10:
        result["engagement"] = "+"
    elif after_length < before_length * 0.5 and after_length < 10:
        result["engagement"] = "-"

    # 主动分享 → 信任提升
    if after.get("user_share", False):
        result["trust"] = "+"

    # 用户快速结束 → 兴趣下降
    if after.get("short_reply", False) and not before.get("short_reply", False):
        result["interest"] = "-"

    # 回复速度变化
    before_time = before.get("response_time", 60)
    after_time = after.get("response_time", 60)

    if after_time < before_time * 0.5:
        result["responsiveness"] = "+"
    elif after_time > before_time * 2:
        result["responsiveness"] = "-"

    return result


def analyze_feedback_patterns(feedback_records):
    """
    分析反馈模式，提取用户偏好。

    Args:
        feedback_records: 反馈记录列表

    Returns:
        dict: 反馈模式分析
    """
    if not feedback_records:
        return {}

    patterns = {
        "preferred_styles": [],
        "avoid_styles": [],
        "preferred_length": "normal",
        "preferred_tone": "neutral",
        "positive_rate": 0.5,
    }

    positive_count = 0
    negative_count = 0
    total_count = 0

    style_scores = {}

    for record in feedback_records:
        feedback_type = record.get("feedback_type", "")
        ai_reply = record.get("ai_reply", "")
        context = record.get("context", "")

        total_count += 1

        if feedback_type in ["positive", "continued"]:
            positive_count += 1
        elif feedback_type in ["negative", "correction"]:
            negative_count += 1

        # 分析AI回复风格
        if ai_reply:
            style = _detect_reply_style(ai_reply)
            if style not in style_scores:
                style_scores[style] = {"positive": 0, "negative": 0}

            if feedback_type in ["positive", "continued"]:
                style_scores[style]["positive"] += 1
            elif feedback_type in ["negative", "correction"]:
                style_scores[style]["negative"] += 1

    # 计算正面反馈率
    if total_count > 0:
        patterns["positive_rate"] = positive_count / total_count

    # 提取偏好风格
    for style, scores in style_scores.items():
        total = scores["positive"] + scores["negative"]
        if total >= 2:
            rate = scores["positive"] / total
            if rate >= 0.7:
                patterns["preferred_styles"].append(style)
            elif rate <= 0.3:
                patterns["avoid_styles"].append(style)

    return patterns


def _detect_reply_style(reply):
    """
    升级版风格检测：不依赖硬编码关键词
    维度一：长度（short/normal/long）
    维度二：语气（warm/directive/playful/neutral）
    """
    reply  = str(reply).strip()
    length = len(reply)

    # 维度一：长度分级
    if length < 25:
        length_style = "short"
    elif length < 100:
        length_style = "normal"
    else:
        length_style = "long"

    # 维度二：语气——用标点+句式特征，不依赖语义词汇
    # 温柔：省略号/波浪线/叠词
    warm_signals    = reply.count("…") + reply.count("～") + reply.count("、、")
    # 指令性：句号结尾短句/建议句式
    directive_chars = reply.count("。") + reply.count(".")
    # 活泼：感叹号/问号/emoji密度
    playful_chars   = reply.count("！") + reply.count("!") + reply.count("？") + reply.count("?")

    # 叠词检测（哈哈/嗯嗯/好好/走走等 AA 型 + 好呀好呀/拜拜啦拜拜啦 等 ABAB 型）
    import re
    dupli = len(re.findall(r'(.)\1', reply))
    dupli += len(re.findall(r'(.{2,3})\1', reply))
    warm_signals += dupli

    if warm_signals >= 2:
        tone_style = "warm"
    elif playful_chars >= 2:
        tone_style = "playful"
    elif directive_chars >= 2 and length > 40:
        tone_style = "directive"
    else:
        tone_style = "neutral"

    return f"{length_style}_{tone_style}"


def analyze_proactive_effectiveness(proactive_records):
    """
    分析主动消息的有效性。

    Args:
        proactive_records: 主动消息记录列表

    Returns:
        dict: 有效性分析
    """
    if not proactive_records:
        return {}

    type_stats = {}

    for record in proactive_records:
        ptype = record.get("type", "general")
        replied = record.get("replied", False)

        if ptype not in type_stats:
            type_stats[ptype] = {"total": 0, "replied": 0}

        type_stats[ptype]["total"] += 1
        if replied:
            type_stats[ptype]["replied"] += 1

    # 计算回复率
    effectiveness = {}
    for ptype, stats in type_stats.items():
        if stats["total"] > 0:
            effectiveness[ptype] = stats["replied"] / stats["total"]

    return effectiveness
