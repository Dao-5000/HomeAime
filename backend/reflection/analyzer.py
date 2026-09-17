# -*- coding: utf-8 -*-
"""反思触发、证据校验、去重和持久化。"""
import difflib
import re
import time
from datetime import datetime
from .. import config, db
from .database import save_reflection, get_recent_reflections
from .generator import generate_reflection

REFLECTION_TYPES = {"user_understanding", "relationship_reflection", "strategy_reflection"}

def _last_reflection_at(session_id, character_id) -> float:
    try:
        return float(db.kv_get(f"reflection_last_at:{session_id}:{character_id}") or 0)
    except Exception:
        return 0.0


def should_trigger_reflection(session_id, character_id, force=False):
    """是否该跑一次反思。

    ★ 2026-09-15 用户拍板改成「每天一次固定槽」（原：距上次 ≥30 分钟就再来一次）：
      实测 2 天反思烧掉 133,724 in / 81,870 out token，而且因为间隔太短、素材几乎没变，
      生成的内容与前一天高度重合（去重后大多被丢掉，等于白烧）。
      现在：每天只在 REFLECTION_SLOT_HOUR 点之后的**第一次机会**跑一次，一天就一次；
      想回旧口径把 config 的 REFLECTION_MODE 设成 "interval"（代码没删，可一键回退）。
    """
    if force:
        return True
    memories = db.valid_memories(session_id=session_id, character_id=character_id) or []
    # ★ 降低门槛：消息≥4、有记忆≥1（素材门槛保持，只是频率降到每天一次）
    if len(memories) < 1 or db.count_messages(session_id, character_id) < 4:
        return False

    last = _last_reflection_at(session_id, character_id)
    try:
        _mode = str(config.get("REFLECTION_MODE") or "daily").strip().lower()
    except Exception:
        _mode = "daily"

    if _mode == "interval":
        try:
            _hours = float(config.get("REFLECTION_INTERVAL_HOURS") or 24)
        except Exception:
            _hours = 24.0
        return time.time() - last >= max(0.5, _hours) * 3600

    # daily：每天一次，固定槽之后才允许
    try:
        _slot = int(config.get("REFLECTION_SLOT_HOUR") or 5)
    except Exception:
        _slot = 5
    _now = datetime.now()
    if _now.hour < max(0, min(23, _slot)):
        return False
    if last <= 0:
        return True
    try:
        return datetime.fromtimestamp(last).date() < _now.date()
    except Exception:
        return True

def load_recent_memory(session_id, character_id, limit=50):
    rows = db.valid_memories(session_id=session_id, character_id=character_id) or []
    return "\n".join(f"[重要度{m.get('importance',5)}] {m.get('memory_content','')}" for m in rows[:limit] if m.get("memory_content"))

def load_timeline(session_id, character_id, limit=20):
    rows = db.get_recent_timeline_events(session_id, character_id, limit) or []
    return "\n".join(f"[{e.get('created_at','')}] {e.get('title','')}：{e.get('description','')}".strip("：") for e in rows if e.get("title") or e.get("description"))

def load_knowledge_graph(session_id, character_id, limit=20):
    try:
        from ..knowledge_graph.query import get_knowledge_summary
        return get_knowledge_summary(session_id, character_id, limit=limit) or ""
    except Exception: return ""

def _evidence_supported(evidence, sources):
    """证据校验（放宽版）：只拦截「完全无来源的编造」。
    旧版要求 evidence 里 2~6 连续字符逐字命中 sources，flash 生成的总结性 evidence
    （如「用户工作压力大」）与原文（「最近好累工作好多」）逐字对不上，反思被整体误杀。
    现改为「任意 2 字符片段命中即放行」，evidence 为空但有 sources 兜底也放行。"""
    clean = lambda s: re.sub(r"[\s\[\]【】‘’“”'\"：:,，。；;]", "", str(s or ""))
    ev, src = clean(evidence), clean(sources)
    if not src:
        return False
    if len(ev) < 2:
        return True
    return any(ev[i:i+2] in src for i in range(len(ev) - 1))

async def update_reflection(session_id, character_id, force=False):
    if not should_trigger_reflection(session_id, character_id, force): return {"triggered":False,"count":0}
    memory, timeline, knowledge = load_recent_memory(session_id, character_id), load_timeline(session_id, character_id), load_knowledge_graph(session_id, character_id)
    # ★ 加入最近聊天记录，让反思也能从对话里提炼（而不只是已有记忆）
    recent_chat = ""
    try:
        msgs = db.recent_messages(session_id, 30, character_id)
        recent_chat = "\n".join(
            f"{'TA' if m.get('role')=='user' else '我'}：{m.get('content','')}"
            for m in msgs if m.get("content")
        )
    except Exception:
        pass
    sources = "\n".join((memory, timeline, knowledge, recent_chat))
    if not sources.strip():
        print(f"[Reflection] 无素材可复盘: memory={len(memory)} timeline={len(timeline)} knowledge={len(knowledge)} chat={len(recent_chat)} session={session_id} char={character_id}", flush=True)
        return {"triggered":False,"count":0}
    existing = [str(r.get("content") or "") for r in get_recent_reflections(session_id, character_id, 40)]
    saved = 0
    for item in await generate_reflection(memory, timeline, knowledge, recent_chat, character_id=character_id):
        rtype, content = str(item.get("type") or "user_understanding"), str(item.get("content") or "").strip()
        evidence, action = str(item.get("evidence") or "").strip(), str(item.get("action_hint") or "").strip()
        try: confidence = float(item.get("confidence") or 0)
        except Exception: confidence = 0
        if rtype not in REFLECTION_TYPES or len(content) < 15 or confidence < 0.45: continue
        if not _evidence_supported(evidence, sources):
            print(f"[Reflection] 丢弃无来源证据: {content[:30]}", flush=True); continue
        if any(difflib.SequenceMatcher(None, content, old).ratio() >= 0.85 for old in existing): continue
        save_reflection(session_id, character_id, rtype, content + (f"【行动提示：{action}】" if action else ""), confidence)
        existing.append(content); saved += 1
    db.kv_set(f"reflection_last_at:{session_id}:{character_id}", str(time.time()))
    db.kv_set(f"reflection_last_result:{session_id}:{character_id}", str(saved))
    print(f"[Reflection] 完成: {saved}条 session={session_id} character={character_id}", flush=True)
    return {"triggered":True,"count":saved}
