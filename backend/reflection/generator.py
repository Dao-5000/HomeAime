# -*- coding: utf-8 -*-
"""基于真实记忆、关系时间线和知识图谱生成可验证的长期反思。"""
import json
import re
from .. import config
from ..deepseek_api import chat_once

REFLECTION_PROMPT = """
你是 AI 伴侣的长期反思模块。你的任务是**从真实记忆和近期对话里提炼洞察**——用户的偏好、习惯、压力反应、关系变化、有效回应策略等。每条洞察都让后续陪伴更贴近这个人。

【长期记忆】(可能很长，里面混杂一次性事件和稳定模式)
{memory}
【关系时间线】
{timeline}
【知识图谱】
{knowledge}
【近期对话】(最近的真实聊天，可能包含记忆里还没提炼的新信息)
{recent_chat}

只允许生成三类反思：user_understanding、relationship_reflection、strategy_reflection。

【产出规则】
- 每条反思基于 **≥1 条相关记忆或对话**中反映出来的洞察（可以是偏好、习惯、反应倾向、关系变化、回应策略等）。
- evidence 引用**材料中的原文片段**(10~30 字)，让后续能直接对照。
- 不推断材料里没明说的身份、疾病、生日、住址等事实。
- 不要把一次偶然事件上升为稳定性格。
- 最多 5 条，confidence 低于 0.40 不要输出。
- **如果能想到任何值得记住的洞察，就产出 1~5 条；只有完全没有任何可总结内容时才输出 []。**

只输出严格 JSON 数组，不要任何解释/标点/代码块：
[
  {{"type":"user_understanding | relationship_reflection | strategy_reflection","content":"15~80字的稳定洞察","evidence":"材料中的原文片段","confidence":0.0,"action_hint":"一句可执行建议"}}
]
"""

def _parse_json_array(raw):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw or "").strip(), flags=re.I)
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try: data = json.loads(match.group(0))
        except Exception: return []
    if isinstance(data, dict): data = [data]
    return data if isinstance(data, list) else []

async def generate_reflection(memory, timeline, knowledge, recent_chat="", character_id="default"):
    key = config.memory_key()
    if not key: return []
    prompt = REFLECTION_PROMPT.format(
        memory=str(memory or "")[:6000] or "（无）",
        timeline=str(timeline or "")[:3000] or "（无）",
        knowledge=str(knowledge or "")[:3000] or "（无）",
        recent_chat=str(recent_chat or "")[:4000] or "（无）",
    )
    try:
        # ★ 2026-09-15：长期反思是"合法的慢杂活"——真机实测 max 84.8s / p90 56.6s，
        #   远超杂活默认档 30s，显式声明长档（否则每次反思都会被守卫层掐断）。
        from .. import llm_guard as _lg
        result = await chat_once(config.memory_extract_model(character_id), [{"role":"system","content":"你负责有证据的长期反思，只输出 JSON，不编造。"},{"role":"user","content":prompt}], key, temperature=0.3, max_tokens=1024, hard_timeout=_lg.long_timeout_sec())
        return _parse_json_array(result)[:5]
    except Exception as exc:
        print(f"[Reflection] 生成失败: {exc}", flush=True)
        return []
