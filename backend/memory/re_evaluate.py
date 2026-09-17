# -*- coding: utf-8 -*-
"""
记忆重要性重评（修复历史 importance=5 遗留）
批量交给 LLM 重新打分（1-10），更新 importance，让「长期/普通/待清理」分层真正生效。
"""
import json
import re

from .. import db, config


def _parse_scores(raw, n):
    """解析 LLM 输出 [{id, importance}] → {index: importance}。容错。"""
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
            imp = int(item.get("importance", 5))
        except Exception:
            continue
        if 0 <= idx < n:
            out[idx] = max(1, min(10, imp))
    return out


async def re_evaluate_importance(session_id, character_id, batch_size=15):
    """批量重评 importance==5 的历史记忆。返回 {evaluated, updated, total}。"""
    from ..deepseek_api import chat_once

    key = config.memory_key()
    if not key:
        return {"evaluated": 0, "updated": 0, "total": 0, "error": "未配置 API Key"}

    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    targets = [m for m in mems if int(m.get("importance", 5) or 5) == 5]
    if not targets:
        return {"evaluated": 0, "updated": 0, "total": len(mems)}

    model = config.memory_extract_model(character_id)
    evaluated = 0
    updated = 0

    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        items_text = "\n".join(
            f"{idx + 1}. {m.get('memory_content', '')}"
            for idx, m in enumerate(batch)
        )
        prompt = (
            "你是AI伴侣的记忆管理系统。请为下面每条用户记忆的重要性打分（1-10，10最重要）。\n\n"
            "打分标准：\n"
            "8-10：用户稳定身份信息（生日/职业/家人）、强烈偏好、重要承诺、双方关系里程碑\n"
            "4-7：一般习惯、普通喜好、日常进展\n"
            "1-3：临时提及、偶发小事、一次性内容\n\n"
            f"记忆列表：\n{items_text}\n\n"
            "请只返回 JSON 数组：[{\"id\":序号,\"importance\":分数}]"
        )
        try:
            raw = await chat_once(model, [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "请为每条记忆的重要性打分"}
            ], key, temperature=0.1, max_tokens=800)
            scores = _parse_scores(raw, len(batch))
        except Exception as e:
            print(f"[ReEvaluate] 批次调用失败: {e}", flush=True)
            continue

        for idx, imp in scores.items():
            if imp == 5:
                continue
            try:
                db.update_memory_importance(batch[idx]["id"], imp)
                updated += 1
            except Exception:
                pass
        evaluated += len(batch)

    return {"evaluated": evaluated, "updated": updated, "total": len(mems)}
