# -*- coding:utf-8 -*-
"""
AI自身状态管理：
  维护AI伴侣自身的连续状态，包括当前交流状态、对用户的印象、最近关注的事情等。
  注入 system prompt 让 AI 保持人格连续性，回复更有连贯性。
"""
import json

from . import db, config
from .deepseek_api import chat_once


AI_STATE_SYSTEM = """
你负责维护AI伴侣自身的连续状态。

注意：
AI不是拥有真实意识，而是维护一致的人格表现。

根据最近互动更新：

输出JSON：

{
"current_mood":"",
"user_impression":"",
"recent_focus":"",
"wanted_topics":"",
"interaction_notes":""
}

字段：

current_mood:
AI当前交流状态，例如：
温柔、开心、关心、担心、期待

user_impression:
AI目前对用户形成的印象

recent_focus:
最近AI比较关注用户什么事情

wanted_topics:
未来自然想继续聊的话题

interaction_notes:
互动细节，例如：
用户喜欢被鼓励
用户今天需要陪伴

规则：

1. 不虚构用户信息
2. 不生成AI真实情感
3. 保持角色连续性
4. 不要夸大依恋
"""


async def update_ai_state(
    session_id,
    messages,
    character_id="default"
):
    """从最近对话中分析AI自身状态，更新到数据库（按角色隔离）。"""
    key = config.api_key_for_model(config.memory_extract_model(character_id))

    if not key:
        return

    # ★ Task 14：agent 轮次（extra.scope == "agent"）不参与她的状态统计（current_mood 等）
    #   —— 她干完活的汇报不是「最近互动」。判定统一走 director.is_agent_row；局部导入避免
    #   循环依赖，且不吞异常（过滤是正确性要求，不许静默退回「照算」）。
    from .agent.director import is_agent_row as _is_agent_row
    messages = [m for m in (messages or []) if not _is_agent_row(m.get("extra"))]

    old = db.get_ai_inner_state(
        session_id, character_id
    )

    text = "\n".join(
        (
            "用户："
            if m.get("role") == "user"
            else "AI："
        )
        +
        str(
            m.get(
                "content",
                ""
            )
        )
        for m in messages
    )

    result = await chat_once(
        config.memory_extract_model(character_id),
        [
            {
                "role": "system",
                "content": AI_STATE_SYSTEM
            },
            {
                "role": "user",
                "content":
                "旧状态：\n"
                +
                json.dumps(
                    old,
                    ensure_ascii=False
                )
                +
                "\n\n最近互动：\n"
                +
                text[-4000:]
            }
        ],
        key,
        temperature=0.2,
        max_tokens=600
    )

    try:
        data = json.loads(
            result
        )

        db.update_ai_inner_state(
            session_id,
            character_id,
            **data
        )

    except Exception:
        pass
