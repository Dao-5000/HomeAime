# -*- coding:utf-8 -*-
"""
Feedback Collector v1.0
反馈采集器：

  采集用户对 AI 回复的反馈，包括：
    1. 显式反馈：用户点赞/点踩/纠正
    2. 隐式反馈：用户回复行为（回复长度、回复速度、是否继续聊天）
"""
from .database import save_feedback


def collect_feedback(session_id, character_id, message_id, user_action,
                     score=0, context="", ai_reply="", user_response=""):
    """
    采集显式反馈。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        message_id: 消息ID
        user_action: 用户动作（like/dislike/reply/correct）
        score: 评分
        context: 上下文
        ai_reply: AI回复内容
        user_response: 用户回应内容

    Returns:
        str or None: 反馈类型
    """
    feedback = None

    if user_action == "like":
        feedback = "positive"
    elif user_action == "dislike":
        feedback = "negative"
    elif user_action == "reply":
        feedback = "continued"
    elif user_action == "correct":
        feedback = "correction"
    elif user_action == "silence":
        feedback = "silence"

    if feedback:
        save_feedback(
            session_id,
            character_id,
            message_id,
            feedback,
            score=score,
            context=context,
            ai_reply=ai_reply,
            user_response=user_response
        )

    return feedback


def collect_implicit_feedback(session_id, character_id, message_id,
                              ai_reply, user_response, response_time=None):
    """
    采集隐式反馈（基于用户回复行为）。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        message_id: 消息ID
        ai_reply: AI回复内容
        user_response: 用户回应内容
        response_time: 响应时间（秒）

    Returns:
        dict: 隐式反馈分析结果
    """
    if not user_response:
        # 用户无回应
        save_feedback(
            session_id,
            character_id,
            message_id,
            "silence",
            ai_reply=ai_reply
        )
        return {"feedback_type": "silence", "engagement": "low"}

    result = {
        "feedback_type": "continued",
        "engagement": "normal",
    }

    # 回复长度分析
    reply_length = len(str(user_response))
    ai_length = len(str(ai_reply or ""))

    if reply_length > ai_length * 1.5:
        # 用户回复比AI长很多 → 高参与度
        result["engagement"] = "high"
        result["trust"] = "+"
    elif reply_length < 5:
        # 用户回复很短 → 低参与度
        result["engagement"] = "low"
        result["interest"] = "-"

    # 回复速度分析
    if response_time is not None:
        if response_time < 10:
            result["response_speed"] = "fast"
            result["interest"] = "+"
        elif response_time > 120:
            result["response_speed"] = "slow"
            result["interest"] = "-"

    # 情绪关键词分析
    positive_words = ["谢谢", "喜欢", "开心", "好的", "嗯嗯", "哈哈", "爱你", "想你"]
    negative_words = ["不是", "不对", "别", "烦", "算了", "随便"]

    positive_count = sum(1 for w in positive_words if w in str(user_response))
    negative_count = sum(1 for w in negative_words if w in str(user_response))

    if positive_count > negative_count:
        result["sentiment"] = "positive"
        result["feedback_type"] = "positive"
    elif negative_count > positive_count:
        result["sentiment"] = "negative"
        result["feedback_type"] = "negative"

    # 保存反馈
    save_feedback(
        session_id,
        character_id,
        message_id,
        result["feedback_type"],
        score=1 if result["feedback_type"] == "positive" else 0,
        ai_reply=ai_reply,
        user_response=user_response
    )

    return result


# ★ 原 detect_correction() 已于 2026-08-30 删除（死代码）：
#   全项目零调用方，且实现只是关键词正则（"不是/不对/错了"），
#   既会把"我不知道对不对"误判成纠正，又只返回 bool——
#   拿不到"错在哪、正确的是什么"，没有可学习的内容。
#
#   用户纠正识别现已改由 **understanding（第一层 DeepSeek）** 负责：
#   输出结构化的 correction{topic, wrong, right, scope}，
#   能区分"纠正"与"单纯不满"，并落库到 feedback/corrections.py。
#   找纠正相关逻辑请去那里，不要在这里重建。
