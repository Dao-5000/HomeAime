# -*- coding: utf-8 -*-
"""合并抽取器（省 token ②，2026-09-15）。

## 为什么做
回复后的后台管线里，有**三个**抽取器读的是**同一批 messages**、各自发起一次模型调用、
各自产出一份 JSON、写各自的表：

  · memory_manager.extract_from_dialog   记忆提炼（2 天实测 171,866 in / 16,785 out）
  · open_loop_manager.extract_open_loops 未完成事项（145,373 in / 27,934 out）
  · profile_manager.extract_profile      用户画像

三份 prompt 的"输入"完全重叠，等于同一段对话被读了三遍、付了三遍钱。现在合并成**一次**
调用，产出 `{"memories": [...], "open_loops": [...], "profile": {...}}` 三段 JSON，
再分别交给各模块**原有的** apply_* 写入函数（写入逻辑一字未改，质量风险只在"提取"这一步）。

## 安全设计
1. **失败即回退**：这里返回 None 时，调用方按旧口径分别调三个抽取器（正确性优先，宁可多花钱）。
2. **缺段不写空**：某一段没解析出来就**跳过那一段**，绝不用空值覆盖已有画像/记忆。
3. **可一键回退**：config `MERGED_EXTRACT_ENABLED=false` → 完全回到旧口径（代码没删）。
4. **prompt 复用**：三段抽取指令直接引用各模块自己维护的 system prompt 常量，
   不另抄一份，避免"改了一处忘了另一处"。
"""
import json

from . import config
from .deepseek_api import chat_once

# 组合 prompt 的骨架：把三段抽取任务串在一次思考里，输出一个 JSON 对象
_MERGED_TEMPLATE = """你是一个后台信息抽取引擎。请在**同一次分析**里完成下面 {n} 项抽取任务，
最后只输出**一个** JSON 对象（不要解释、不要 markdown 代码块）。

【输出格式（严格遵守）】
{{
{schema}
}}

═══════════ 任务1：长期记忆提炼 ═══════════
{sys_memory}

═══════════ 任务2：未完成事项（开环）═══════════
{sys_open_loop}

═══════════ 任务3：用户画像 ═══════════
{sys_profile}

【硬性要求】
1. 三项任务共用同一段对话，不要重复输出同一件事。
2. 顶层只能是上述那一个 JSON 对象；memories/open_loops 必须是数组；profile 必须是对象。
3. 没有可抽取内容时给空数组 / 空对象，**不要编造**。
"""

_SCHEMA = (
    '  "memories": [ {{"content": "记忆正文", "type": "fact|preference|event|emotion|general",'
    ' "importance": 1-10, "context": "当时情境", "emotion_tag": "情绪标签",'
    ' "source_text": "用户原话片段"}} ],\n'
    '  "open_loops": [ {{"title": "未完成事项", "description": "细节", "category": "event|task|'
    'reminder|promise|agreement", "importance": 1-10, "trigger_time": "YYYY-MM-DD HH:MM 或空",'
    ' "reuse_id": 0, "resolve": false}} ],\n'
    '  "profile": {{ "nickname": "", "personality": "", "occupation": "", "hobbies": "",'
    ' "dislikes": "", "communication_style": "", "emotional_traits": "", "confidence": 0.0 }}'
)


def _enabled() -> bool:
    try:
        return bool(config.get("MERGED_EXTRACT_ENABLED", True))
    except Exception:
        return True


def _build_system_prompt() -> str:
    """拼出组合 system prompt（三段指令全部引用各模块自己的常量）。"""
    from .memory_manager import EXTRACT_SYSTEM
    from .open_loop_manager import OPEN_LOOP_SYSTEM
    from .profile_manager import PROFILE_SYSTEM_V2

    return _MERGED_TEMPLATE.format(
        n=3,
        schema=_SCHEMA,
        sys_memory=str(EXTRACT_SYSTEM or "").strip(),
        sys_open_loop=str(OPEN_LOOP_SYSTEM or "").strip(),
        sys_profile=str(PROFILE_SYSTEM_V2 or "").strip(),
    )


def _extract_json_object(raw: str) -> dict:
    """从模型输出里抠出顶层 JSON 对象（容忍 ```json 包裹与前后闲话）。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        data = json.loads(text[s:e + 1])
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def extract_merged(messages, session_id: str, character_id: str = "default"):
    """一次调用产出三段结果。

    返回 dict：{"memories": [...], "open_loops": [...], "profile": {...},
                "_model": ..., "_raw_len": ...}
    失败/关闭时返回 **None** —— 调用方据此回退到旧的"三次分别调用"。
    """
    if not _enabled():
        return None
    key = config.memory_key()
    if not key:
        return None

    text = "\n".join(
        ("%s：%s" % ("TA" if m.get("role") == "user" else "AI", m.get("content", "")))
        for m in (messages or []) if m.get("content")
    )
    if not text.strip():
        return None

    # 未完成事项要认领既有条目：把现有 pending 清单一并给它（与单独版口径一致）
    existing_txt = "（暂无）"
    try:
        from . import db
        _existing = db.get_open_loops(session_id, character_id, limit=25, order="recent") or []
        if _existing:
            existing_txt = "\n".join(
                "[id=%s] %s" % (r.get("id"), str(r.get("title") or "").strip())
                for r in _existing
            )
    except Exception:
        _existing = []

    user_content = (
        "【已有未完成清单（认领用，没有就留空）】\n" + existing_txt
        + "\n\n【最近对话】\n" + text[-8000:]
    )

    _model = config.memory_extract_model(character_id)
    try:
        raw = await chat_once(
            _model,
            [{"role": "system", "content": _build_system_prompt()},
             {"role": "user", "content": user_content}],
            key,
            temperature=0.2,
            # 三段 JSON 共用一次输出额度：记忆最长 + 开环 + 画像，给足 3000
            max_tokens=3000,
            reasoning_effort=("low" if config.model_supports_reasoning_effort(_model) else None),
        )
    except Exception as e:
        print("[MergedExtract] 调用失败，回退旧口径: %s: %s" % (type(e).__name__, e), flush=True)
        return None

    data = _extract_json_object(raw)
    if not data:
        print("[MergedExtract] 解析失败，回退旧口径（raw_len=%d）" % len(str(raw or "")), flush=True)
        return None

    mem = data.get("memories")
    loops = data.get("open_loops")
    prof = data.get("profile")
    if not isinstance(mem, list) and not isinstance(loops, list) and not isinstance(prof, dict):
        print("[MergedExtract] 三段都缺，回退旧口径", flush=True)
        return None

    return {
        "memories": mem if isinstance(mem, list) else [],
        "open_loops": loops if isinstance(loops, list) else [],
        "profile": prof if isinstance(prof, dict) else {},
        "_model": _model,
        "_raw_len": len(str(raw or "")),
        "_existing": _existing,
    }


async def apply_merged(result: dict, session_id: str, character_id: str = "default") -> dict:
    """把合并结果分别交给各模块**原有的**写入函数。缺段就跳过，不写空值。"""
    from . import memory_manager, open_loop_manager, profile_manager

    out = {"memories_in": 0, "open_loops_written": 0, "profile": False}
    if not result:
        return out

    # 记忆：先过 memory_filter 的校验/改写/去重（与单独版同一条后处理链）
    try:
        raw_mem = result.get("memories") or []
        if raw_mem:
            try:
                from . import memory_filter
                items = memory_filter.post_process_memories(raw_mem)
            except Exception:
                items = raw_mem
            out["memories_in"] = await memory_manager.apply_extracted_memories(
                items, session_id, character_id)
    except Exception as e:
        print("[MergedExtract] 记忆入库失败(静默): %s: %s" % (type(e).__name__, e), flush=True)

    # 未完成事项：走原有的认领/更新/关闭/新增逻辑
    try:
        loops = result.get("open_loops") or []
        if loops:
            out["open_loops_written"] = open_loop_manager.apply_open_loops(
                loops, session_id, character_id, existing=result.get("_existing"))
    except Exception as e:
        print("[MergedExtract] 开环入库失败(静默): %s: %s" % (type(e).__name__, e), flush=True)

    # 画像：走原有的写库逻辑
    try:
        prof = result.get("profile") or {}
        if prof:
            out["profile"] = profile_manager.apply_profile(prof, session_id, character_id)
    except Exception as e:
        print("[MergedExtract] 画像入库失败(静默): %s: %s" % (type(e).__name__, e), flush=True)

    return out
