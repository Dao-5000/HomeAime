# -*- coding:utf-8 -*-
"""
Text Analyzer v1.0
文本感知分析器：

  从文本消息中提取情绪、意图、紧急程度等信号。
  作为多模态感知层的文本维度。
"""


# 情绪关键词
EMOTION_KEYWORDS = {
    "positive": [
        "开心", "高兴", "快乐", "哈哈", "不错", "太好了", "棒",
        "成功", "通过", "录取", "升职", "喜欢", "爱", "想你",
        "谢谢", "感谢", "舒服", "轻松", "满足", "幸福",
    ],
    "negative": [
        "难过", "伤心", "累", "疲惫", "压力", "焦虑", "烦",
        "郁闷", "失落", "沮丧", "崩溃", "痛苦", "绝望", "孤独",
        "寂寞", "生气", "愤怒", "委屈", "想哭", "无助", "迷茫",
        "没精神", "提不起劲", "不开心", "心情不好",
    ],
    "neutral": [
        "嗯", "哦", "好", "知道了", "随便", "都行",
    ],
}

# 意图关键词
INTENT_KEYWORDS = {
    "seeking_advice": [
        "怎么办", "怎么", "如何", "建议", "推荐", "帮我",
        "应该", "可不可以", "能不能", "请教", "问一下",
    ],
    "sharing": [
        "今天", "刚才", "刚刚", "我去", "我吃", "我看",
        "告诉你", "跟你说", "你知道吗",
    ],
    "emotional_support": [
        "陪陪我", "安慰我", "抱抱", "想哭", "好累",
        "撑不住", "坚持不下去",
    ],
    "casual": [
        "在吗", "干嘛", "吃饭了吗", "睡了吗", "忙吗",
    ],
}

# 紧急程度关键词
URGENCY_KEYWORDS = {
    "high": [
        "紧急", "马上", "立刻", "现在", "救命", "出事了",
        "怎么办啊", "撑不住了",
    ],
    "medium": [
        "尽快", "等不及", "有点急", "麻烦",
    ],
    "low": [
        "有空", "闲了", "慢慢", "不着急",
    ],
}


def analyze_text(message):
    """
    分析文本消息，提取感知信号。

    Args:
        message: 文本消息

    Returns:
        dict: 分析结果 {emotion_hint, intent, urgency, length, ...}
    """
    msg = str(message or "")
    result = {
        "emotion_hint": "neutral",
        "intent": "unknown",
        "urgency": "low",
        "length": len(msg),
        "has_question": "?" in msg or "？" in msg,
        "has_exclamation": "!" in msg or "！" in msg,
    }

    if not msg:
        return result

    # 情绪分析
    emotion_scores = {"positive": 0, "negative": 0, "neutral": 0}
    for emotion, keywords in EMOTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in msg:
                emotion_scores[emotion] += 1

    max_emotion = max(emotion_scores, key=emotion_scores.get)
    if emotion_scores[max_emotion] > 0:
        result["emotion_hint"] = max_emotion

    # 意图分析
    intent_scores = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        score = sum(1 for k in keywords if k in msg)
        if score > 0:
            intent_scores[intent] = score

    if intent_scores:
        result["intent"] = max(intent_scores, key=intent_scores.get)

    # 紧急程度分析
    urgency_scores = {"high": 0, "medium": 0, "low": 0}
    for urgency, keywords in URGENCY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in msg:
                urgency_scores[urgency] += 1

    max_urgency = max(urgency_scores, key=urgency_scores.get)
    if urgency_scores[max_urgency] > 0:
        result["urgency"] = max_urgency

    # 消息长度分类
    if len(msg) < 10:
        result["length_category"] = "short"
    elif len(msg) < 50:
        result["length_category"] = "medium"
    else:
        result["length_category"] = "long"

    return result


def detect_emotion_intensity(message):
    """
    检测情绪强度（0-10）。

    Args:
        message: 文本消息

    Returns:
        int: 情绪强度
    """
    msg = str(message or "")
    intensity = 0

    # 感叹号增加强度
    intensity += msg.count("!") + msg.count("！")

    # 重复字增加强度
    for char in ["啊", "呀", "啦", "嘛", "呢"]:
        if char * 2 in msg:
            intensity += 2

    # 强烈情绪词增加强度
    strong_words = ["太", "非常", "特别", "超级", "极度", "崩溃", "绝望"]
    for word in strong_words:
        if word in msg:
            intensity += 3

    return min(10, intensity)


def detect_sarcasm(message):
    """
    简单检测反讽（基于标点和模式）。

    Args:
        message: 文本消息

    Returns:
        bool: 是否可能是反讽
    """
    msg = str(message or "")

    # 反讽模式
    sarcasm_patterns = [
        "呵呵", "行吧", "随便吧", "你说得对",
        "真是太棒了", "真是太好了",
    ]

    for pattern in sarcasm_patterns:
        if pattern in msg:
            return True

    return False
