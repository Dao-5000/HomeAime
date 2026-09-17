# -*- coding:utf-8 -*-
"""
Identity Builder v1.0
身份生成器：

  把角色配置 + 关系 + Timeline + Reflection 生成AI身份描述。
"""
import json

from .. import config
from ..deepseek_api import chat_once
from ..llm_json import loads_llm_json


# 身份生成 Prompt
IDENTITY_PROMPT = """
你是长期陪伴AI身份整理模块。

根据以下信息，生成AI身份核心。

角色：
{character}

关系：
{relationship}

共同经历：
{timeline}

长期理解：
{reflection}

生成AI身份的三个维度：

1. relationship_identity（关系身份）
   - 用户在AI心中的角色
   - 关系开始时间
   - 关系阶段

2. experience_identity（经历身份）
   - 重要共同经历
   - 关系转折点

3. expression_identity（表达身份）
   - 偏好称呼
   - 对话风格
   - 特殊用语

输出JSON：
{{
  "relationship_identity": {{}},
  "experience_identity": {{}},
  "expression_identity": {{}}
}}

要求：
1. 不要改变角色基础人格
2. 只基于提供的信息，不要虚构
3. 身份描述要具体，不要空泛
"""


async def build_identity(character, relationship, timeline, reflection, character_id="default"):
    """
    使用 LLM 构建身份。

    Args:
        character: 角色配置
        relationship: 关系状态
        timeline: 时间线
        reflection: 反思

    Returns:
        dict: 身份信息
    """
    key = config.memory_key()
    if not key:
        return {}

    prompt = IDENTITY_PROMPT.format(
        character=json.dumps(character, ensure_ascii=False)[:2000],
        relationship=json.dumps(relationship, ensure_ascii=False)[:1000],
        timeline=str(timeline)[:2000],
        reflection=str(reflection)[:1000],
    )

    try:
        result = await chat_once(
            config.memory_extract_model(character_id),
            [
                {
                    "role": "system",
                    "content": "你是AI身份整理模块，只输出JSON，不要解释。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            key,
            temperature=0.3,
            max_tokens=1500
        )
    except Exception as e:
        print(f"[Identity] LLM调用失败: {e}", flush=True)
        return {}

    try:
        # ★ 2026-09-12：容错解析（详见 backend/llm_json.py 的说明）。
        data = loads_llm_json(result)
        if not isinstance(data, dict):
            raise ValueError("模型未返回 JSON 对象: %r" % str(result or "")[:200])
        return data
    except Exception as e:
        print(f"[Identity] JSON解析失败: {e}", flush=True)
        return {}


async def update_identity(session_id, character_id, character_config, relationship, timeline, reflection):
    """
    更新身份信息。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        character_config: 角色配置
        relationship: 关系状态
        timeline: 时间线
        reflection: 反思

    Returns:
        dict: 更新结果
    """
    from .manager import IdentityManager

    manager = IdentityManager()

    # 初始化核心身份
    manager.init_core_identity(session_id, character_id, character_config)

    # 构建其他身份
    identity_data = await build_identity(character_config, relationship, timeline, reflection, character_id=character_id)

    if not identity_data:
        return {"updated": False}

    # 保存关系身份
    relationship_identity = identity_data.get("relationship_identity", {})
    if relationship_identity:
        manager.save(session_id, character_id, "relationship", relationship_identity, importance=9)

    # 保存经历身份
    experience_identity = identity_data.get("experience_identity", {})
    if experience_identity:
        manager.save(session_id, character_id, "experience", experience_identity, importance=8)

    # 保存表达身份
    expression_identity = identity_data.get("expression_identity", {})
    if expression_identity:
        manager.save(session_id, character_id, "expression", expression_identity, importance=8)

    print(f"[Identity] 身份更新完成", flush=True)
    return {"updated": True, "identity": identity_data}
