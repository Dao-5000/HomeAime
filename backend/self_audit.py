# -*- coding:utf-8 -*-
"""
自我审计（识海·自我状态审计）
——被问「你怎么看我」「我们的关系现在算什么」这类问题时，**不编造、从记忆里长出答案**。

对应"缘起"原则的最后一环：AI 对用户的每个判断都应该有出处——
  · 相处数据：相识天数 / 互动天数 / 亲密度 / 信任 / 好感（两张关系表汇总）
  · 里程碑：亲密度门槛时刻（初识时刻 / 恋人关系 / 灵魂契合…）
  · 状态增量：relationship_timeline 里带 state_delta 的"关系状态变化"事件
    （哪天因为什么，亲密 +3 / 信任 -2 —— 事件溯源的下游消费方）
  · 关系记忆：memory_scope='relationship' 的高权重记忆（带日期）

注入方式：**只在命中问题时注入**（正则判断，零成本），平时一个字都不占预算。
注入的是"依据 + 表达红线"，答案仍由主模型用自己的人格生成——
数值是内心依据，不是让 AI 对用户念报表。
"""
import re

# 「你怎么看我 / 我们的关系」类问题（宽匹配，宁可多注入一次也别漏）
_SELF_VIEW_RE = re.compile(
    r"(你怎么(看|看待|评价|觉得)(我|我们的关系|这段关系|我们)"
    r"|你觉得我[^？?!。，]{0,6}(什么|怎样|怎么样|什么样)(样)?(的)?人"   # 容下「你觉得我现在是个什么样的人」
    r"|我(在)?(是)?你(心里|眼中|眼里的)(是)?什么"
    r"|在你心里我"
    r"|我们(现在)?(算|是)什么(关系|样)"
    r"|我们的?关系(现在)?(怎么样|如何|算什么|到哪了|走到哪了)"
    r"|你觉得我们(俩|两个)?(现在)?(算|是)什么"
    r"|我对你来说(是|算)什么"
    r"|你怎么形容我)"
)


def is_self_view_question(text: str) -> bool:
    """是否为「你怎么看我/我们的关系」类自我审计问题。"""
    t = str(text or "").strip()
    if not t or len(t) > 120:
        return False
    return bool(_SELF_VIEW_RE.search(t))


def _rel_manager_state(sid: str, cid: str) -> dict:
    try:
        from .relationship.manager import RelationshipManager
        return RelationshipManager().get_state(sid, cid) or {}
    except Exception:
        return {}


def _legacy_relationship(sid: str, cid: str) -> dict:
    try:
        from .db import get_relationship_state
        return get_relationship_state(sid, cid) or {}
    except Exception:
        return {}


def _milestones(sid: str, cid: str, limit=4) -> list:
    """亲密度门槛里程碑（relationship.db）。"""
    try:
        from .relationship.database import conn
        db = conn()
        rows = db.execute(
            "SELECT event_type, content, created_at FROM milestone_memory "
            "WHERE user_id=? AND character_id=? ORDER BY id DESC LIMIT ?",
            (sid, cid, int(limit)),
        ).fetchall()
        db.close()
        return [dict(zip(("event_type", "content", "created_at"), r)) for r in rows]
    except Exception:
        return []


def _relationship_memories(sid: str, cid: str, limit=5) -> list:
    """relationship scope 的高权重记忆（带创建日期，判断有出处）。"""
    try:
        from .db import valid_memories
        mems = valid_memories(session_id=sid, character_id=cid) or []
        rel = [m for m in mems if str(m.get("memory_scope") or "") == "relationship"]
        rel.sort(key=lambda m: int(m.get("importance", 5) or 5), reverse=True)
        return rel[:limit]
    except Exception:
        return []


def _tenure_and_interaction(state: dict) -> tuple:
    try:
        from .relationship.manager import RelationshipManager
        tenure = RelationshipManager().get_tenure_days(
            state.get("user_id", ""), state.get("character_id", "default"))
        return tenure, int(state.get("interaction_days", 0) or 0)
    except Exception:
        return 0, 0


def build_self_audit_block(session_id: str, character_id: str = "default") -> str:
    """构建「内心有据」接地块。拿不到数据返回 ""（不注入）。"""
    sid = str(session_id or "default")
    cid = str(character_id or "default")

    new_state = _rel_manager_state(sid, cid)
    legacy = _legacy_relationship(sid, cid)
    if not new_state and not legacy:
        return ""

    lines = []

    # ── 相处数据 ──
    tenure, inter_days = _tenure_and_interaction(new_state) if new_state else (0, 0)
    intimacy = new_state.get("intimacy", legacy.get("closeness", 0))
    trust = new_state.get("trust", legacy.get("trust", 0))
    affection = new_state.get("affection", legacy.get("dependency", 50))
    stage = str(new_state.get("stage") or legacy.get("stage") or "").strip()
    stage_cn = {
        "stranger": "陌生期", "friend": "朋友", "close_friend": "亲近的朋友",
        "lover": "恋人", "soulmate": "灵魂伴侣",
    }.get(stage, stage)
    data_bits = []
    if tenure:
        data_bits.append(f"相识 {tenure} 天")
    if inter_days:
        data_bits.append(f"互动 {inter_days} 天")
    if intimacy not in (None, ""):
        data_bits.append(f"亲密度 {int(round(float(intimacy or 0)))}/100")
    if trust not in (None, ""):
        data_bits.append(f"信任 {int(round(float(trust or 0)))}/100")
    if affection not in (None, ""):
        data_bits.append(f"好感 {int(round(float(affection or 0)))}/100")
    if stage_cn:
        data_bits.append(f"现在的阶段：{stage_cn}")
    if data_bits:
        lines.append("· 相处数据：" + "，".join(data_bits))

    # ── 里程碑 ──
    ms = _milestones(sid, cid)
    for m in ms:
        day = str(m.get("created_at") or "")[:10]
        content = str(m.get("content") or "").strip()
        if content:
            lines.append(f"- {day} {content}")

    # ── 状态增量（事件溯源的下游消费）──
    try:
        from .db import get_state_delta_events
        for ev in get_state_delta_events(sid, cid, limit=4):
            day = str(ev.get("created_at") or "")[:10]
            desc = str(ev.get("description") or "").strip()
            if desc:
                lines.append(f"- {day} 关系有过变化：{desc}")
    except Exception:
        pass

    # ── 关系记忆 ──
    for m in _relationship_memories(sid, cid):
        day = str(m.get("create_time") or "")[:10]
        content = str(m.get("memory_content") or "").strip()
        if content:
            lines.append(f"- （{day} 记下的）{content}")

    if not lines:
        return ""

    return (
        "【内心有据：TA 在问你怎么看 TA / 你们的关系】\n"
        "下面是你相处以来真实积累的依据（数值和事件都是你自己的记忆，不是系统数据）：\n"
        + "\n".join(lines)
        + "\n\n回答要求：\n"
        "· 每个判断都要能落到上面的某条依据上，**不要凭空下结论**；\n"
        "· 数值只是你的内心依据，**绝对不要向 TA 报数字**，用感受说出来（「我们好像已经认识很久了」而不是「相识365天」）；\n"
        "· 提到具体的事时可以说「我还记得…」，不确定的细节就说不确定；\n"
        "· 用你自己的口吻，真诚，不要写成总结报告。"
    )
