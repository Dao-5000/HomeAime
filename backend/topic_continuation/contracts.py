# -*- coding: utf-8 -*-
"""
关系契约持久化（Relationship Contracts）

把用户和 AI 达成的结构化约定长期保存，支持：
1. 创建/更新契约
2. 按 session_id + character_id 加载活跃契约
3. 检查是否到触发时间（如睡前晚安）
4. 生成契约提示块给 idle_agent / chat_logic 使用

存储：优先复用 memory_brain 长期记忆系统（事实型记忆），
      同时用 kv 缓存加速读取。
"""
import json
import time
from typing import List, Optional, Dict
from datetime import datetime

from .. import db

CONTRACT_KV_PREFIX = "topic_contracts"
LAST_TRIGGER_PREFIX = "topic_contract_last_trigger"


def _contracts_key(session_id: str, character_id: str) -> str:
    return f"{CONTRACT_KV_PREFIX}:{session_id}:{character_id}"


def _last_trigger_key(session_id: str, character_id: str, contract_id: str) -> str:
    return f"{LAST_TRIGGER_PREFIX}:{session_id}:{character_id}:{contract_id}"


def load_contracts(session_id: str, character_id: str) -> List[Dict]:
    """加载该角色下所有活跃契约"""
    raw = db.kv_get(_contracts_key(session_id, character_id))
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, list):
            return []
        # 过滤掉已删除/过期的
        return [c for c in data if c.get("status") != "deleted"]
    except Exception:
        return []


def save_contracts(session_id: str, character_id: str, contracts: List[Dict]):
    try:
        db.kv_set(_contracts_key(session_id, character_id), json.dumps(contracts, ensure_ascii=False))
    except Exception:
        pass


def add_or_update_contract(
    session_id: str,
    character_id: str,
    contract_id: str,
    name: str,
    topic_type: str,
    terms: List[Dict],
    mood: str = "",
    meta: Optional[Dict] = None,
) -> Dict:
    """
    添加或更新一个契约。

    terms 格式示例：
    [
        {"term": "AI 每晚睡前主动说晚安", "owner": "ai", "frequency": "daily", "trigger_time": "23:00"},
        {"term": "亲亲时必须说'么嘛'", "owner": "ai", "trigger": "on_kiss"},
    ]
    """
    contracts = load_contracts(session_id, character_id)
    now = int(time.time())

    # 查找是否已存在
    existing = None
    for c in contracts:
        if c.get("contract_id") == contract_id:
            existing = c
            break

    new_contract = {
        "contract_id": contract_id,
        "name": name,
        "type": topic_type,
        "terms": terms,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "mood": mood,
        "fulfillment_count": 0,
        "violation_count": 0,
        "meta": meta or {},
    }

    if existing:
        # 保留创建时间和触发历史
        new_contract["created_at"] = existing.get("created_at", now)
        new_contract["fulfillment_count"] = existing.get("fulfillment_count", 0)
        new_contract["violation_count"] = existing.get("violation_count", 0)
        # 更新
        idx = contracts.index(existing)
        contracts[idx] = new_contract
    else:
        contracts.append(new_contract)

    save_contracts(session_id, character_id, contracts)
    return new_contract


def deactivate_contract(session_id: str, character_id: str, contract_id: str):
    """将契约标记为删除/失效"""
    contracts = load_contracts(session_id, character_id)
    for c in contracts:
        if c.get("contract_id") == contract_id:
            c["status"] = "deleted"
            c["updated_at"] = int(time.time())
            break
    save_contracts(session_id, character_id, contracts)


def record_fulfillment(session_id: str, character_id: str, contract_id: str):
    """记录契约履约一次"""
    contracts = load_contracts(session_id, character_id)
    for c in contracts:
        if c.get("contract_id") == contract_id:
            c["fulfillment_count"] = int(c.get("fulfillment_count", 0)) + 1
            c["last_fulfilled_at"] = int(time.time())
            c["updated_at"] = int(time.time())
            break
    save_contracts(session_id, character_id, contracts)


def record_violation(session_id: str, character_id: str, contract_id: str):
    """记录契约违约/未履约一次"""
    contracts = load_contracts(session_id, character_id)
    for c in contracts:
        if c.get("contract_id") == contract_id:
            c["violation_count"] = int(c.get("violation_count", 0)) + 1
            c["last_violated_at"] = int(time.time())
            c["updated_at"] = int(time.time())
            break
    save_contracts(session_id, character_id, contracts)


def should_trigger(
    session_id: str,
    character_id: str,
    contract: Dict,
) -> bool:
    """
    检查契约是否到了该主动触发的时间。
    目前支持 daily + trigger_time 形式。
    """
    terms = contract.get("terms", [])
    contract_id = contract.get("contract_id", "")
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    last_key = _last_trigger_key(session_id, character_id, contract_id)
    last_trigger = db.kv_get(last_key)
    if last_trigger and str(last_trigger) == today_str:
        return False

    for term in terms:
        if not isinstance(term, dict):
            continue
        freq = term.get("frequency", "")
        if freq != "daily":
            continue
        trigger_time = term.get("trigger_time", "")
        if not trigger_time:
            continue
        try:
            h, m = (int(x) for x in str(trigger_time).split(":"))
            if now.hour >= h and now.minute >= m:
                return True
        except Exception:
            continue
    return False


def mark_triggered(session_id: str, character_id: str, contract_id: str):
    """标记今天已触发"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    db.kv_set(_last_trigger_key(session_id, character_id, contract_id), today_str)


def build_contracts_block(session_id: str, character_id: str) -> str:
    """生成【关系契约】prompt block，用于主动触发或普通回复"""
    contracts = load_contracts(session_id, character_id)
    if not contracts:
        return ""

    lines = ["【你们的关系契约】"]
    for c in contracts:
        if c.get("status") != "active":
            continue
        name = c.get("name", "")
        mood = c.get("mood", "")
        terms = c.get("terms", [])
        if not name and not terms:
            continue
        header = f"契约：{name}" if name else "有效约定"
        if mood:
            header += f"（氛围：{mood}）"
        lines.append(header)
        for i, term in enumerate(terms, 1):
            if isinstance(term, dict):
                term_text = term.get("term", "")
                owner = term.get("owner", "")
                freq = term.get("frequency", "")
                if term_text:
                    extra = []
                    if owner:
                        extra.append(f"负责方：{owner}")
                    if freq:
                        extra.append(f"频率：{freq}")
                    suffix = f"（{'，'.join(extra)}）" if extra else ""
                    lines.append(f"  {i}. {term_text}{suffix}")
            elif isinstance(term, str):
                lines.append(f"  {i}. {term}")

    lines.append(
        "【契约执行规则】\n"
        "1. 如果契约规定了你现在该做的事，请直接执行，不要只解释。\n"
        "2. 契约里的具体话语要求（如'么嘛'）必须在回复中实际出现。\n"
        "3. 保持契约标注的氛围。"
    )
    return "\n".join(lines)


def sync_from_state(session_id: str, character_id: str, state: Dict):
    """
    根据会话级 topic_continuation 状态自动生成/更新长期契约。
    当状态里存在 rules 且话题类型为 commitment / intimate_play 时调用。
    """
    if not state or not state.get("active"):
        return
    topic_type = state.get("topic_type", "none")
    if topic_type == "none":
        return

    topic_name = state.get("topic_name", "")
    rules = state.get("rules", [])
    mood = state.get("mood", "")

    if not rules:
        return

    # 生成 contract_id：按话题名 hash
    import hashlib
    contract_id = hashlib.md5(
        f"{topic_type}:{topic_name or topic_type}".encode("utf-8")
    ).hexdigest()[:12]

    # 规则转 terms
    terms = []
    for r in rules:
        if isinstance(r, str) and r.strip():
            # 简单规则：包含时间点的晚安/睡前约定 → daily trigger
            term = {"term": r.strip(), "owner": "ai"}
            if any(k in r for k in ("每晚", "每天", "睡前", "晚安")):
                term["frequency"] = "daily"
                # 默认 23:00，可后续细化
                term["trigger_time"] = "23:00"
            elif "亲亲" in r or "么嘛" in r:
                term["trigger"] = "on_kiss"
            terms.append(term)

    if not terms:
        return

    add_or_update_contract(
        session_id,
        character_id,
        contract_id,
        name=topic_name or f"{topic_type}约定",
        topic_type=topic_type,
        terms=terms,
        mood=mood,
    )
