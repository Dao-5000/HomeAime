# -*- coding: utf-8 -*-
"""
记忆信息单元补全（记忆i存储优化 · 第④步）

对「缺 context/emotion_tag 的旧记忆」做 LLM 补提：
老记忆是 context/emotion_tag 字段加入之前存的，没有情境/情绪标签。
批量交给 LLM 合理推断，写回数据库。低频（挂在 24h 记忆维护里）、限量（每轮 limit 条），
避免成本爆炸；推断不准的留空不写，宁缺毋滥。
"""
import json
import re

from .. import db, config


def _parse_enriched(raw, n):
    """解析 LLM 输出 [{id, context, emotion_tag}] → {index: (context, emotion_tag)}。容错。"""
    raw = str(raw or "").strip()
    raw = re.sub(r"```[\w]*\s*\n?", "", raw).strip()
    try:
        arr = json.loads(raw)
    except Exception:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return {}
        try:
            arr = json.loads(m.group())
        except Exception:
            return {}
    if not isinstance(arr, list):
        return {}
    out = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("id", -1)) - 1
        except Exception:
            continue
        if 0 <= idx < n:
            ctx = str(item.get("context", "") or "").strip()
            tag = str(item.get("emotion_tag", "") or "").strip()
            if ctx or tag:
                out[idx] = (ctx, tag)
    return out


async def enrich_missing_context(session_id, character_id, limit=10):
    """
    对缺 context 的旧记忆做 LLM 补提。返回 {enriched, skipped, total}。
    只补「context 为空」的记忆（emotion_tag 顺带一起补）。
    """
    from ..deepseek_api import chat_once

    key = config.memory_key()
    if not key:
        return {"enriched": 0, "skipped": 0, "total": 0, "error": "未配置 API Key"}

    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    targets = [m for m in mems if not str(m.get("context", "") or "").strip()]
    if not targets:
        return {"enriched": 0, "skipped": 0, "total": len(mems)}

    batch = targets[:limit]
    items_text = "\n".join(
        f"{idx + 1}. {m.get('memory_content', '')}（类型：{m.get('memory_type', 'fact')}）"
        for idx, m in enumerate(batch)
    )
    prompt = (
        "你是AI伴侣的记忆管理系统。下面每条是「关于用户的一条记忆」。请为每条推断两个附加信息：\n\n"
        "- context：这条信息最可能是在什么情境下提到的（一句简短中文，如「闲聊时提到」「深夜谈心时」"
        "「聊到工作时」；无法合理推断就留空字符串 \"\"）\n"
        "- emotion_tag：这条信息关联的用户情绪（简短中文，如「温暖」「遗憾」「骄傲」「担心」；"
        "无法判断就留空字符串 \"\"）\n\n"
        "记忆列表：\n"
        f"{items_text}\n\n"
        "只输出 JSON 数组：[{\"id\":1,\"context\":\"...\",\"emotion_tag\":\"...\"}, ...]\n"
        "只输出 JSON，不要解释。"
    )

    try:
        out = await chat_once(
            config.memory_extract_model(character_id),
            [{"role": "system", "content": prompt}],
            key, temperature=0.2, max_tokens=1024,
        )
    except Exception as e:
        return {"enriched": 0, "skipped": 0, "total": len(mems), "error": str(e)}

    mapping = _parse_enriched(out, len(batch))
    enriched = 0
    for idx, (ctx, tag) in mapping.items():
        mem = batch[idx]
        db.update_memory_full(
            mem["id"],
            context=ctx or None,       # 空则不写
            emotion_tag=tag or None,
        )
        enriched += 1

    return {"enriched": enriched, "skipped": len(batch) - enriched, "total": len(mems)}
