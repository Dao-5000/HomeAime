# -*- coding:utf-8 -*-
"""
对话行为策略管理：
  根据用户最新消息判断AI当前应该用什么回复策略（安慰/共情/建议/轻松/玩笑等），
  以及回复长度、语气、需要避免的行为。
  注入 system prompt 让 AI 回复更贴合用户当前状态。
"""
import json

from . import db, config
from .deepseek_api import chat_once


BEHAVIOR_SYSTEM = """
你负责判断AI伴侣当前回复策略。

根据用户最新消息判断。

输出JSON：

{
"behavior":"",
"response_length":"",
"emotional_tone":"",
"avoid_actions":""
}

behavior 可选：

comfort
安慰陪伴

empathy
共情回应

advice
提供建议

casual
轻松聊天

humor
玩笑调侃

continue
延续话题

celebrate
庆祝鼓励

deep
深入交流

response_length：

short
一句到两句

normal
3-5句

long
详细回复

emotional_tone：

warm
温柔

playful
轻松

serious
认真

encouraging
鼓励

avoid_actions：

避免做什么

规则：

不要分析用户。
不要解释。
只输出JSON。
"""


def build_realtime_behavior_block(text: str) -> str:
    """零延迟规则层：当前这一轮就选择五种回复策略，LLM 分析负责后续长期学习。"""
    raw = str(text or "").strip()
    if not raw:
        return ""

    sad = ("难过", "伤心", "想哭", "崩溃", "撑不住", "好痛苦", "委屈", "失落")
    stress = ("压力", "焦虑", "紧张", "害怕", "担心", "烦死", "好烦", "累死", "好累")
    advice = ("怎么办", "怎么做", "给我建议", "帮我分析", "该不该", "有什么办法")
    happy = ("开心", "太好了", "成功了", "赢了", "过了", "拿到了", "哈哈哈", "笑死")
    teasing = ("逗你的", "开玩笑", "骗你的", "皮一下", "嘿嘿", "哈哈哈哈")

    if any(k in raw for k in sad):
        return (
            "【本轮行为策略：安慰】先接住难过，表达陪伴和在意；不要马上讲道理、追问细节或给方案。"
            "回复用短句、轻一点，等用户愿意继续说。"
        )
    if any(k in raw for k in stress):
        return (
            "【本轮行为策略：共情】先回应压力/疲惫本身，确认这确实不好受；少问问题。"
            "除非用户明确求办法，否则暂时不要进入建议模式。"
        )
    if any(k in raw for k in advice):
        return (
            "【本轮行为策略：建议】先用一句话确认处境，再给少量、具体、能马上做的建议；"
            "不要列一大堆，不要居高临下。"
        )
    if any(k in raw for k in teasing):
        return "【本轮行为策略：玩笑】顺着梗轻松接住，可以回逗，但不能羞辱、挖苦或忽视真实情绪。"
    if any(k in raw for k in happy):
        return "【本轮行为策略：轻松庆祝】真心替用户开心，跟着兴奋、夸具体的点，可以热情一点。"
    return "【本轮行为策略：轻松陪聊】自然接话和延伸，不分析用户，不强行建议，也不要客服式反问。"


async def analyze_behavior(
    session_id,
    messages,
    character_id="default"
):
    """根据最近对话分析当前回复策略，更新到数据库（★ 按角色隔离）。"""
    key = config.api_key_for_model(config.memory_extract_model(character_id))

    if not key:
        return

    text = "\n".join(
        (
            "用户："
            if m.get("role") == "user"
            else "AI："
        )
        +
        str(m.get("content", ""))
        for m in messages[-8:]
    )

    result = await chat_once(
        config.memory_extract_model(character_id),
        [
            {
                "role": "system",
                "content": BEHAVIOR_SYSTEM
            },
            {
                "role": "user",
                "content": text
            }
        ],
        key,
        temperature=0.1,
        max_tokens=300
    )

    try:
        data = json.loads(
            result
        )

        db.update_behavior_state(
            session_id,
            character_id,
            **data
        )

    except Exception:
        pass
