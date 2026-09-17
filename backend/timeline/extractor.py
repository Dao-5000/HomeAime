# -*- coding:utf-8 -*-
"""
Timeline Extractor v1.0
关系事件提取器：

  使用 LLM 从聊天中提取值得长期记录的关系事件。
  只记录：
    - 第一次发生的事情
    - 对关系有影响的事情
    - 用户主动分享的重要经历
    - 明显情绪事件
"""
import json

from .. import config
from ..deepseek_api import chat_once
from ..llm_json import loads_llm_json


EVENT_PROMPT = """
分析以下聊天，判断是否产生值得长期记录的关系事件。

只记录：
1. 第一次发生的事情（第一次分享、第一次见面、第一次称呼等）
2. 对关系有影响的事情（冲突、和解、承诺、约定等）
3. 用户主动分享的重要经历（童年、家庭、工作重大事件等）
4. 明显情绪事件（重大失落、重大喜悦、情绪崩溃等）

事件类型可使用：
- first_share：第一次分享
- emotional_support：情感支持
- milestone：关系里程碑
- conflict：冲突
- promise：承诺/约定
- important_event：重要事件
- other：其他

返回JSON：
{
  "has_event": true/false,
  "type": "",
  "title": "",
  "description": "",
  "importance": 1-10
}

如果没有值得记录的事件，has_event=false，其他字段留空。
不要记录普通寒暄、日常闲聊、已经记录过的重复事件。

聊天：
{chat}
"""


async def extract_event(chat_text, character_id="default"):
    """
    从聊天文本中提取关系事件。

    Args:
        chat_text: 聊天文本

    Returns:
        dict or None: 事件信息
    """
    key = config.memory_key()
    if not key:
        return None

    if not chat_text or not str(chat_text).strip():
        return None

    prompt = EVENT_PROMPT.format(chat=str(chat_text)[:4000])

    try:
        result = await chat_once(
            config.memory_extract_model(character_id),
            [
                {
                    "role": "system",
                    "content": "你是关系事件分析专家，只输出JSON，不要解释。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            key,
            temperature=0.1,
            # ★ 2026-09-12：300 太小，输出几乎必被截断（日志里 result=```j 就是它）。
            max_tokens=1500
        )
    except Exception as e:
        print(f"[TimelineExtractor] LLM调用失败: {e}", flush=True)
        return None

    try:
        # ★ 2026-09-12：容错解析（详见 backend/llm_json.py 的说明）。
        data = loads_llm_json(result)
        if data is None:
            raise ValueError("模型未返回可解析的 JSON: %r" % str(result or "")[:200])
        return data
    except Exception as e:
        print(f"[TimelineExtractor] JSON解析失败: {e}, result={str(result or '')[:200]}", flush=True)
        return None


async def extract_and_save_event(session_id, character_id, chat_text):
    """
    提取事件并保存到时间线。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        chat_text: 聊天文本

    Returns:
        dict or None: 保存的事件信息
    """
    from .manager import TimelineManager

    event = await extract_event(chat_text, character_id=character_id)

    if not event or not event.get("has_event"):
        return None

    title = str(event.get("title", "")).strip()
    if not title:
        return None

    try:
        TimelineManager().add_event(
            session_id=session_id,
            character_id=character_id,
            event_type=event.get("type", "other"),
            title=title,
            description=event.get("description", ""),
            importance=int(event.get("importance", 5))
        )
        print(f"[TimelineExtractor] 保存时间线事件: {title}", flush=True)
        return event
    except Exception as e:
        print(f"[TimelineExtractor] 保存事件失败: {e}", flush=True)
        return None
