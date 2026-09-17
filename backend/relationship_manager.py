# -*- coding: utf-8 -*-
"""
关系状态管理：
  维护用户与AI伴侣之间的关系状态，包括关系阶段、亲密程度、信任程度等。
  注入 system prompt 让 AI 根据关系阶段调整互动方式。
"""
import json

from . import db, config
from .deepseek_api import chat_once


RELATIONSHIP_SYSTEM = """
你负责维护用户与AI伴侣之间的关系状态。

根据最近对话判断关系变化。

输出JSON：

{
  "stage":"",
  "closeness":50,
  "trust":50,
  "dependency":50,
  "preferred_call":"",
  "relationship_style":"",
  "shared_topics":"",
  "recent_relationship_event":""
}

规则：

stage 可使用：
陌生期 / 熟悉期 / 暧昧期 / 恋爱期 / 热恋期 / 稳定陪伴期

closeness：
亲密程度 0-100

trust：
用户对AI的信任程度 0-100

dependency：
用户对陪伴关系的依赖程度 0-100

preferred_call：
用户明确喜欢的称呼

relationship_style：
例如：
温柔陪伴型
黏人恋爱型
互怼情侣型
朋友式恋爱
成熟稳定型

shared_topics：
最近两人常聊的重要共同话题，用顿号分隔

recent_relationship_event：
最近影响关系的重要事件或约定

要求：
1. 不要因为一两句普通聊天让数值剧烈变化
2. 每次变化建议控制在 ±1~5
3. 用户明确表达喜欢、信任、依赖、约定时可适当提高
4. 用户明确表达不满、疏远、反感时可降低
5. 没有新信息时尽量保持旧状态
6. 不要虚构
只输出JSON。
"""


async def update_relationship(
    session_id,
    messages,
    user_message: str = "",
    semantic=None,
    character_id="default"
):
    """从最近对话中分析关系状态变化，更新到数据库（★ 按角色隔离，不再恒写 default）。"""
    key = config.memory_key()

    if not key:
        return False

    # ★ Task 14：agent 轮次（extra.scope == "agent"）不参与关系数值 —— 她的干活汇报不是情感互动。
    #   判定统一走 director.is_agent_row；这里用局部导入避免模块级循环依赖（且**不吞异常**：
    #   过滤是正确性要求，导不进来就该响，不许静默退回「照算」）。
    from .agent.director import is_agent_row as _is_agent_row
    messages = [m for m in (messages or []) if not _is_agent_row(m.get("extra"))]

    old = db.get_relationship_state(
        session_id, character_id
    )

    transcript = "\n".join(
        (
            "用户：" if m.get("role") == "user"
            else "AI："
        ) + str(m.get("content") or "")
        for m in messages
        if m.get("content")
    )

    prompt = (
        "【当前关系状态】\n"
        + json.dumps(
            old,
            ensure_ascii=False
        )
        + "\n\n【最近对话】\n"
        + transcript[-4000:]
    )

    try:
        result = await chat_once(
            config.memory_extract_model(character_id),
            [
                {
                    "role": "system",
                    "content": RELATIONSHIP_SYSTEM
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            key,
            temperature=0.2,
            max_tokens=600
        )

        data = json.loads(
            result.strip()
        )

        db.update_relationship_state(
            session_id,
            character_id,
            **data
        )

        return True

    except Exception:
        return False
