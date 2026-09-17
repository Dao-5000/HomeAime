# -*- coding:utf-8 -*-
"""
Memory Consolidator v1.0
记忆整理器：

  将碎片化的长期记忆和时间线事件整理成结构化的用户人生档案。
  解决长期运行后的记忆碎片化、重复信息过多、AI无法理解长期变化等问题。

  整理分类：
    1. life_phase：人生阶段
    2. personality_pattern：性格特点
    3. behavior_pattern：行为习惯
    4. important_values：重要价值观
    5. relationship_story：与AI的重要经历
    6. growth_trajectory：成长轨迹
    7. emotional_pattern：情绪模式
"""
import json

from .. import db, config
from ..deepseek_api import chat_once
from ..llm_json import loads_llm_json
from .manager import LifeProfileManager, PROFILE_TYPES


CONSOLIDATE_PROMPT = """
你是一个长期陪伴AI的记忆整理系统。

根据以下信息，生成用户长期档案。

长期记忆：
{memory}

关系事件：
{timeline}

请整理成结构化的用户人生档案，分类如下：

1. life_phase：人生阶段（用户当前处于什么人生阶段，有什么特征）
2. personality_pattern：性格特点（用户稳定的性格特征）
3. behavior_pattern：行为习惯（用户反复出现的行为模式）
4. important_values：重要价值观（用户看重什么）
5. relationship_story：与AI的重要经历（用户和AI之间的关键关系节点）
6. growth_trajectory：成长轨迹（用户的变化和成长）
7. emotional_pattern：情绪模式（用户情绪变化的规律）

要求：
- 只整理有长期价值的信息，不要记录临时细节
- 每条内容要具体、有依据，不要空泛
- 重要性1-10，越稳定越重要的信息分数越高
- 如果某个分类没有足够信息，可以不输出
- 不要编造信息，只基于提供的记忆和事件整理

输出JSON数组：
[
  {{
    "type": "life_phase",
    "content": "用户经历职业成长阶段，面对压力时倾向主动学习和解决问题",
    "importance": 9
  }}
]

★ 注意（2026-09-17 修）：上面 JSON 示例的花括号必须写成 `{{ }}`。
  下面 consolidate() 会对本模板调用 str.format()，未转义的花括号会被当成占位符，
  直接抛 `KeyError: '\n    "type"'` —— 真机日志里 **88 次失败、成功 0 次**，
  `life_profile` 表因此永远是空的（"她的人生档案"实际不存在）。
  回归用例见 test/py/test_batch3_consolidate.py：模板必须能 format，
  且格式化后要还原成单花括号的合法 JSON 示例。
"""


async def consolidate(memory_text, timeline_text, character_id="default"):
    """
    使用 LLM 整理记忆，生成人生档案。

    Args:
        memory_text: 长期记忆文本
        timeline_text: 时间线事件文本

    Returns:
        list: 整理后的档案列表
    """
    key = config.memory_key()
    if not key:
        return []

    if not memory_text and not timeline_text:
        return []

    prompt = CONSOLIDATE_PROMPT.format(
        memory=str(memory_text)[:6000] if memory_text else "（暂无）",
        timeline=str(timeline_text)[:4000] if timeline_text else "（暂无）"
    )

    try:
        result = await chat_once(
            config.memory_extract_model(character_id),
            [
                {
                    "role": "system",
                    "content": "你是记忆整理专家，只输出JSON数组，不要解释。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            key,
            temperature=0.2,
            max_tokens=1500
        )
    except Exception as e:
        print(f"[MemoryConsolidator] LLM调用失败: {e}", flush=True)
        return []

    try:
        # ★ 2026-09-12：容错解析（详见 backend/llm_json.py 的说明）。
        data = loads_llm_json(result)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "profiles" in data:
            return data["profiles"]
        return []
    except Exception as e:
        print(f"[MemoryConsolidator] JSON解析失败: {e}, result={str(result or '')[:200]}", flush=True)
        return []


async def run_consolidation(session_id, character_id="default"):
    """
    执行记忆整理任务。

    流程：
    1. 读取长期记忆
    2. 读取时间线事件
    3. 调用 LLM 整理
    4. 保存到 life_profile 表

    Args:
        session_id: 会话ID
        character_id: 角色ID

    Returns:
        int: 保存的档案数量
    """
    # 1. 读取长期记忆（最近50条）
    try:
        memories = db.valid_memories(session_id=session_id, character_id=character_id)[:50]
    except Exception:
        memories = db.valid_memories()[:50]

    memory_text = ""
    if memories:
        memory_text = "\n".join([
            f"- [{m.get('memory_type', 'fact')}] {m.get('memory_content', '')}"
            for m in memories
            if m.get('memory_content')
        ])

    # 2. 读取时间线事件（最近20条）
    try:
        timeline = db.get_recent_timeline_events(
            session_id=session_id,
            character_id=character_id,
            limit=20
        )
    except Exception:
        timeline = []

    timeline_text = ""
    if timeline:
        timeline_text = "\n".join([
            f"- [{e.get('event_type', '')}] {e.get('title', '')}: {e.get('description', '')}"
            for e in timeline
            if e.get('title')
        ])

    if not memory_text and not timeline_text:
        print("[MemoryConsolidator] 没有足够的记忆和时间线数据，跳过整理", flush=True)
        return 0

    # 3. 调用 LLM 整理
    profiles = await consolidate(memory_text, timeline_text, character_id=character_id)

    if not profiles:
        print("[MemoryConsolidator] 整理结果为空", flush=True)
        return 0

    # 4. 保存到 life_profile 表
    manager = LifeProfileManager()
    saved_count = 0

    for item in profiles:
        if not isinstance(item, dict):
            continue

        profile_type = item.get("type", "")
        content = item.get("content", "")
        importance = item.get("importance", 5)

        if not profile_type or not content:
            continue

        try:
            manager.save(
                session_id=session_id,
                character_id=character_id,
                profile_type=profile_type,
                content=content,
                importance=int(importance or 5)
            )
            saved_count += 1
        except Exception as e:
            print(f"[MemoryConsolidator] 保存档案失败: {e}", flush=True)

    print(f"[MemoryConsolidator] 记忆整理完成，保存了 {saved_count} 条档案", flush=True)
    return saved_count


# 整理触发配置
CONSOLIDATION_MEMORY_THRESHOLD = 100  # 累计100条新memory触发
CONSOLIDATION_DAYS_THRESHOLD = 7      # 或7天触发一次


def should_consolidate(session_id, character_id="default"):
    """
    判断是否应该执行记忆整理。

    Args:
        session_id: 会话ID
        character_id: 角色ID

    Returns:
        bool: 是否应该整理
    """
    # 简化版：每次调用有10%概率触发，避免过于频繁
    # 后续可以增加基于记忆数量和时间的精确判断
    import random
    return random.random() < 0.1
