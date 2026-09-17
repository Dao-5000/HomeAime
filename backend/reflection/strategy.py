# -*- coding: utf-8 -*-
"""把有证据的反思应用到后续回复、人格偏移和主动消息策略。

★ 2026-09-17 补（反思回流）：memory_reflection 有 916 条，但 `last_used` **全为 NULL**，
  `touch_reflection()` 全项目零调用 —— 反思只按"最近 10 条"注入，
  系统从来不知道哪条真的进了 prompt、哪条有用。结果是反思库只进不出、无法加权，
  也无法像记忆那样"越用越准"。
  现在：① 注入时登记 last_used（真被用上）；② 排序优先"用得多的 + 置信度高的"，
  让反复印证有效的反思上浮、长期没用过的自然沉底（只排序，不删行）。
"""
import json

from .database import get_recent_reflections, touch_reflection

REFLECTION_TYPE_NAMES = {"user_understanding": "对你的长期理解",
                         "relationship_reflection": "关系变化",
                         "strategy_reflection": "相处策略"}

# 每个类型最多注入几条（原来写死 [:4]，这里提出来便于调）
REFLECTION_PER_TYPE = 4


def rank_reflections(rows: list) -> list:
    """按"被用过 + 置信度高 + 较新"排序。

    last_used 为空的排后面：从没进过 prompt 的反思不该抢占有限的注入额度。
    """
    def _key(r):
        used = 1.0 if r.get("last_used") else 0.0
        try:
            conf = float(r.get("confidence") or 0.0)
        except Exception:
            conf = 0.0
        return (used, conf, str(r.get("created_at") or ""))

    return sorted(rows or [], key=_key, reverse=True)


def get_reflection_summary(session_id, character_id, limit=10):
    result = {k: [] for k in REFLECTION_TYPE_NAMES}
    for row in get_recent_reflections(session_id, character_id, limit=limit):
        rtype = row.get("reflection_type", "user_understanding")
        content = str(row.get("content") or "").strip()
        if rtype in result and content:
            result[rtype].append(content)
    return result


def build_reflection_prompt(session_id, character_id, limit=10):
    """拼注入块，并把**真正注入了的**反思记为"被用过"。"""
    rows = rank_reflections(get_recent_reflections(session_id, character_id, limit=limit))
    summary = {k: [] for k in REFLECTION_TYPE_NAMES}
    used_ids = []
    for row in rows:
        rtype = row.get("reflection_type", "user_understanding")
        content = str(row.get("content") or "").strip()
        if rtype not in summary or not content:
            continue
        if len(summary[rtype]) >= REFLECTION_PER_TYPE:
            continue
        summary[rtype].append(content)
        if row.get("id") is not None:
            used_ids.append(row["id"])

    lines = ["【长期相处后形成的理解（内部使用）】"]
    for key in ("user_understanding", "relationship_reflection", "strategy_reflection"):
        if summary.get(key):
            lines.append(REFLECTION_TYPE_NAMES[key] + "：")
            lines.extend("- " + x for x in summary[key])
    if len(lines) == 1:
        return ""

    # ★ 注入即"被用过"：写 last_used，让反思库有回流依据（失败不影响注入）
    for rid in used_ids:
        try:
            touch_reflection(rid)
        except Exception:
            pass

    lines.append("这些是有证据的相处经验，不是用户身份事实；自然执行，不要提反思、数据库或系统。"
                 "若与用户当前明确表述冲突，以当前表达为准。")
    return "\n".join(lines)


def apply_reflection_to_personality(reflection_summary, current_personality=None):
    text = "\n".join(reflection_summary.get("strategy_reflection", []) or [])
    out = {}
    if any(k in text for k in ("温柔", "先共情", "先安慰", "耐心")):
        out["warmth_delta"] = 2
    if any(k in text for k in ("需要空间", "不要催", "减少打扰", "不喜欢被催")):
        out.update(initiative_delta=-2, attachment_delta=-1)
    if any(k in text for k in ("喜欢直接", "简洁", "说重点")):
        out["dominance_delta"] = 1
    if any(k in text for k in ("喜欢玩笑", "幽默", "接梗", "轻松")):
        out["humor_delta"] = 2
    return out


def apply_reflection_to_proactive(reflection_summary, current_strategy=None):
    text = "\n".join((reflection_summary.get("user_understanding", []) or []) +
                     (reflection_summary.get("strategy_reflection", []) or []))
    out = {}
    if any(k in text for k in ("压力", "忙", "加班", "考试", "需要空间", "不要催")):
        out.update(push_frequency="low", push_tone="gentle")
    if any(k in text for k in ("夜聊", "深夜", "睡前")):
        out["push_best_time"] = "night"
    if any(k in text for k in ("早起", "早晨", "通勤")):
        out["push_best_time"] = "morning"
    if any(k in text for k in ("幽默", "玩笑", "接梗")):
        out["push_tone"] = "playful"
    return out


def apply_reflection_to_personality_state(session_id, character_id="default"):
    """把反思结论落到人格偏移上。

    ★ 2026-09-17 修（真机五维被钉死的事故）：
      原来这里自己算 `old + delta` 再 clamp 到 ±20。而
      `apply_reflection_to_personality` 只产出**正**delta（温柔+2、幽默+2…），
      于是每次反思都往上顶、顶到 20 就被钳死，**永远回不来** ——
      真机现场五维全顶格（warmth 20 / dominance -20 / humor 20 / init 20 / attach 20），
      我加在 `update_five_dim` 里的回中趋势被这条写入者整体绕过。
      现在统一走 `personality_manager.apply_five_dim_delta`（单一权威）：
      先回中、再叠加、最后钳制 —— 反复的反思只会让它**缓慢逼近**上限，
      而不是钉在边界上；反思停止后维度会自己回落。
    """
    from .. import db
    from .. import personality_manager as _pm
    summary = get_reflection_summary(session_id, character_id, 12)
    old, patch = db.get_personality_state(session_id, character_id) or {}, {}
    for field, delta in apply_reflection_to_personality(summary, {}).items():
        patch[field] = _pm.apply_five_dim_delta(int(old.get(field, 0) or 0), int(delta))
    strategies = summary.get("strategy_reflection", []) or []
    if strategies:
        patch["relationship_behavior"] = "；".join(
            str(x).split("【行动提示：", 1)[0].strip() for x in strategies[:3])[:500]
    if any(any(k in x for k in ("不要说教", "不喜欢被催", "不要追问")) for x in strategies):
        patch["forbidden_phrases"] = "说教式分析、连续催促、机械追问"
    if patch:
        db.update_personality_state(session_id, character_id, _reason="reflection", **patch)
    apply_reflection_to_proactive_strategy(session_id, character_id)
    return patch


def apply_reflection_to_proactive_strategy(session_id, character_id="default"):
    from .. import db
    hints = apply_reflection_to_proactive(get_reflection_summary(session_id, character_id, 15), {})
    if hints:
        db.kv_set(f"proactive_hints:{session_id}:{character_id}",
                  json.dumps(hints, ensure_ascii=False))
    return hints
