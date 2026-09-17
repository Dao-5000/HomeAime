# -*- coding:utf-8 -*-
"""
情绪状态管理：
  从用户聊天中分析当前情绪状态，存入 emotional_state 表。
  注入 system prompt 让 AI 根据用户情绪调整回复语气。
"""
import json

from . import db, config
from .deepseek_api import chat_once


EMOTION_SYSTEM = """
你负责分析用户当前情绪状态。

根据聊天内容判断：

输出JSON：

{
"mood":"",
"intensity":0,
"reason":"",
"needs":"",
"positive_keywords":"",
"negative_keywords":""
}

规则：

mood:
开心/平静/焦虑/压力/疲惫/难过/生气/孤独/期待

intensity:
0-10

reason:
导致情绪的原因

needs:
用户当前更需要：
安慰/鼓励/建议/陪伴/庆祝

不要编造。

没有明显情绪时：
intensity=0
"""


async def update_emotion(
    session_id,
    messages,
    character_id="default"
):
    """从最近对话中分析用户情绪状态，更新到数据库（★ 按角色隔离）。"""
    key = config.memory_key()

    if not key:
        return

    # ★ Task 14：agent 轮次（extra.scope == "agent"）不参与情绪统计 —— 她干完活的汇报
    #   不是「用户情绪的信号」。判定统一走 director.is_agent_row；局部导入避免循环依赖，
    #   且不吞异常（过滤是正确性要求，不许静默退回「照算」）。
    from .agent.director import is_agent_row as _is_agent_row
    messages = [m for m in (messages or []) if not _is_agent_row(m.get("extra"))]

    text = "\n".join(
        [
            (
                "用户：" if m["role"] == "user"
                else "AI："
            )
            +
            str(m.get("content", ""))
            for m in messages
        ]
    )

    result = await chat_once(
        config.memory_extract_model(character_id),
        [
            {
                "role": "system",
                "content": EMOTION_SYSTEM
            },
            {
                "role": "user",
                "content": text[-3000:]
            }
        ],
        key,
        temperature=0.2,
        max_tokens=500
    )

    try:
        data = json.loads(result)
        db.update_emotion_state(
            session_id,
            character_id,
            **data
        )
        # ★ 连贯情绪：追加时间线快照 + 评估上次干预效果（反馈闭环）
        try:
            from .emotion_engine import emotion_timeline, emotion_feedback
            mood = str(data.get("mood", "")).strip()
            intensity = int(data.get("intensity", 0) or 0)
            score = emotion_timeline.append(
                session_id, character_id, mood, intensity,
                reason=data.get("reason", ""), needs=data.get("needs", "")
            )
            emotion_feedback.evaluate(session_id, character_id, mood, score)
        except Exception as _te:
            print(f"[EmotionTimeline] 追加/评估失败: {_te}", flush=True)
    except Exception:
        pass
