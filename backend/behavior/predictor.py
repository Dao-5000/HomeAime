# -*- coding:utf-8 -*-
"""
Behavior Predictor v1.0
LLM行为模式提取器：

  使用 LLM 从长期聊天记录中提取稳定的行为模式。
  只输出稳定规律，不根据一次事件判断。
"""
import json

from .. import config
from ..deepseek_api import chat_once
from ..llm_json import loads_llm_json
from .database import save_pattern, clear_patterns


PREDICT_PROMPT = """
分析用户长期行为，提取稳定的行为模式。

历史聊天记录：
{history}

请分析以下方面：
1. time_pattern：用户的时间习惯（通常在什么时间聊天，周几最活跃）
2. emotion_trend：用户的情绪趋势（近期情绪变化方向，是否有持续压力）
3. communication_pattern：用户的沟通行为（难过时是否主动说，回复速度变化）
4. stress_period：用户是否处于压力期（工作/学习压力的周期性）
5. social_pattern：用户的社交习惯（喜欢独处还是倾诉）

只输出稳定规律，不要根据一次事件判断。
如果某个方面没有足够数据，可以不输出。

输出JSON：
{{
  "patterns": [
    {{
      "type": "time_pattern",
      "pattern": "用户通常在晚上聊天，周末更喜欢长聊",
      "confidence": 0.8
    }}
  ]
}}
"""


async def predict_behavior(history_text, character_id="default"):
    """
    使用 LLM 预测用户行为模式。

    Args:
        history_text: 历史聊天记录文本

    Returns:
        dict: 预测结果 {patterns: [...]}
    """
    key = config.api_key_for_model(config.memory_extract_model(character_id))
    if not key:
        return {"patterns": []}

    if not history_text or not str(history_text).strip():
        return {"patterns": []}

    prompt = PREDICT_PROMPT.format(history=str(history_text)[:8000])

    try:
        result = await chat_once(
            config.memory_extract_model(character_id),
            [
                {
                    "role": "system",
                    "content": "你是行为分析专家，只输出JSON，不要解释。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            key,
            temperature=0.2,
            # ★ 2026-09-12：800 偏小，容易被截断（日志里有 result=```j 的现场）。
            max_tokens=2500
        )
    except Exception as e:
        print(f"[BehaviorPredictor] LLM调用失败: {e}", flush=True)
        return {"patterns": []}

    try:
        # ★ 2026-09-12：容错解析（详见 backend/llm_json.py 的说明）。
        data = loads_llm_json(result)
        if isinstance(data, dict) and "patterns" in data:
            return data
        return {"patterns": []}
    except Exception as e:
        print(f"[BehaviorPredictor] JSON解析失败: {e}, result={str(result or '')[:200]}", flush=True)
        return {"patterns": []}


async def run_behavior_analysis(session_id, character_id="default", messages=None, emotion_records=None):
    """
    执行完整的行为分析流程。

    流程：
    1. 规则分析（时间模式、情绪趋势、沟通模式）
    2. LLM深度分析
    3. 保存结果到数据库

    Args:
        session_id: 会话ID
        character_id: 角色ID
        messages: 消息列表
        emotion_records: 情绪记录列表

    Returns:
        int: 保存的模式数量
    """
    from .analyzer import analyze_all_patterns

    if not messages:
        from .. import db
        messages = db.recent_messages(session_id, 100, character_id)

    if not messages:
        print("[BehaviorPredictor] 没有足够的聊天记录，跳过分析", flush=True)
        return 0

    # 1. 规则分析
    rule_patterns = analyze_all_patterns(messages, emotion_records)

    # 2. LLM深度分析
    history_text = "\n".join([
        f"{m.get('role', 'user')}: {m.get('content', '')}"
        for m in messages[-50:]
        if m.get('content')
    ])

    llm_result = await predict_behavior(history_text, character_id=character_id)
    llm_patterns = llm_result.get("patterns", [])

    # 3. 合并结果
    all_patterns = rule_patterns + llm_patterns

    if not all_patterns:
        print("[BehaviorPredictor] 没有提取到行为模式", flush=True)
        return 0

    # 4. 清除旧模式，保存新模式
    try:
        clear_patterns(session_id, character_id)
    except Exception:
        pass

    saved_count = 0
    for item in all_patterns:
        if not isinstance(item, dict):
            continue

        pattern_type = item.get("type", "")
        pattern = item.get("pattern", "")
        confidence = item.get("confidence", 0.5)

        if not pattern_type or not pattern:
            continue

        try:
            save_pattern(
                session_id,
                character_id,
                pattern_type,
                pattern,
                float(confidence or 0.5)
            )
            saved_count += 1
        except Exception as e:
            print(f"[BehaviorPredictor] 保存模式失败: {e}", flush=True)

    print(f"[BehaviorPredictor] 行为分析完成，保存了 {saved_count} 个模式", flush=True)
    return saved_count
