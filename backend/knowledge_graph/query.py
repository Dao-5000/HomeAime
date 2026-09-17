# -*- coding:utf-8 -*-
"""
Knowledge Query v1.0
知识查询：

  从知识图谱中查询与用户消息相关的实体、关系和事件。
"""
from .. import db
from .database import (
    get_entity_by_name,
    get_entities_by_type,
    get_all_entities,
    get_relations_for_entity,
    get_events,
)


def search_related(session_id, character_id, query, limit=10):
    """
    搜索与查询相关的知识。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        query: 查询文本
        limit: 返回数量

    Returns:
        dict: {entities, relations, events}
    """
    result = {
        "entities": [],
        "relations": [],
        "events": [],
    }

    if not query:
        return result

    query = str(query).strip()

    # 1. 搜索实体（名称匹配）
    try:
        rows = db.q(
            """
            SELECT * FROM kg_entity
            WHERE session_id=? AND character_id=?
              AND (name LIKE ? OR description LIKE ?)
            ORDER BY importance DESC
            LIMIT ?
            """,
            (session_id, character_id, f"%{query}%", f"%{query}%", limit),
            fetch=True
        )
        result["entities"] = [dict(r) for r in rows]
    except Exception as e:
        print(f"[KnowledgeGraph] 搜索实体失败: {e}", flush=True)

    # 2. 获取相关关系（★ 按角色隔离）
    entity_ids = [e["id"] for e in result["entities"]]
    if entity_ids:
        try:
            placeholders = ",".join(["?"] * len(entity_ids))
            rows = db.q(
                f"""
                SELECT r.*, e.name as target_name, e.entity_type as target_type
                FROM kg_relation r
                LEFT JOIN kg_entity e ON r.target_id = e.id
                WHERE r.session_id=? AND r.character_id=? AND r.source_id IN ({placeholders})
                LIMIT ?
                """,
                (session_id, character_id, *entity_ids, limit * 2),
                fetch=True
            )
            result["relations"] = [dict(r) for r in rows]
        except Exception as e:
            print(f"[KnowledgeGraph] 搜索关系失败: {e}", flush=True)

    # 3. 搜索相关事件
    try:
        rows = db.q(
            """
            SELECT * FROM kg_event
            WHERE session_id=? AND character_id=?
              AND description LIKE ?
            ORDER BY importance DESC
            LIMIT ?
            """,
            (session_id, character_id, f"%{query}%", limit),
            fetch=True
        )
        result["events"] = [dict(r) for r in rows]
    except Exception as e:
        print(f"[KnowledgeGraph] 搜索事件失败: {e}", flush=True)

    return result


def get_user_knowledge(session_id, character_id, limit=20):
    """
    获取用户的核心知识结构。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        limit: 返回数量

    Returns:
        dict: {entities_by_type, relations, events}
    """
    result = {
        "entities_by_type": {},
        "relations": [],
        "events": [],
    }

    # 获取所有实体
    entities = get_all_entities(session_id, character_id, limit=limit * 2)

    # 按类型分组
    for entity in entities:
        entity_type = entity.get("entity_type", "other")
        if entity_type not in result["entities_by_type"]:
            result["entities_by_type"][entity_type] = []
        result["entities_by_type"][entity_type].append(entity)

    # 获取重要实体的关系（★ 按角色隔离）
    important_entities = entities[:limit]
    for entity in important_entities:
        relations = get_relations_for_entity(entity["id"], session_id, character_id)
        result["relations"].extend(relations)

    # 获取重要事件
    result["events"] = get_events(session_id, character_id, limit=limit)

    return result


def get_knowledge_summary(session_id, character_id, limit=10):
    """
    获取知识图谱摘要（用于 Prompt）。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        limit: 返回数量

    Returns:
        str: 知识图谱摘要文本
    """
    knowledge = get_user_knowledge(session_id, character_id, limit=limit)

    lines = []

    # 实体摘要
    entities_by_type = knowledge.get("entities_by_type", {})
    if entities_by_type:
        lines.append("【用户知识结构】")

        type_names = {
            "company": "工作",
            "interest": "兴趣",
            "project": "项目",
            "emotion": "情绪状态",
            "goal": "目标",
            "habit": "习惯",
            "skill": "技能",
            "place": "地点",
            "person": "重要人物",
            "health": "健康",
        }

        for entity_type, entities in entities_by_type.items():
            if not entities:
                continue

            type_name = type_names.get(entity_type, entity_type)
            names = [e.get("name", "") for e in entities[:5] if e.get("name")]
            if names:
                lines.append(f"{type_name}：{'、'.join(names)}")

    # 关系摘要
    relations = knowledge.get("relations", [])
    if relations:
        lines.append("")
        lines.append("【重要关系】")
        seen = set()
        for rel in relations[:limit]:
            target_name = rel.get("target_name", "")
            relation_type = rel.get("relation_type", "")
            if target_name and relation_type and target_name not in seen:
                seen.add(target_name)
                from .relation import get_relation_type_name
                rel_name = get_relation_type_name(relation_type)
                lines.append(f"- {rel_name}：{target_name}")

    # 事件摘要
    events = knowledge.get("events", [])
    if events:
        lines.append("")
        lines.append("【重要经历】")
        for event in events[:5]:
            desc = event.get("description", "")
            time_str = event.get("time", "")
            if desc:
                if time_str:
                    lines.append(f"- {time_str}：{desc}")
                else:
                    lines.append(f"- {desc}")

    return "\n".join(lines)


def _get_knowledge_graph_for_prompt_v1_REMOVED(session_id, character_id, user_message="", limit=10):
    """
    ⚠ 已废弃：本文件底部那个同名函数把它整个覆盖了，这段代码从来没生效过。

    ★ 2026-09-13：这里原来定义的是 `get_knowledge_graph_for_prompt`，与后面
      v2.0 版**同名**。Python 后定义者胜，所以这段（带 search_related 相关检索、
      能按当前消息命中实体的版本）实际是死代码 —— 抽取实体关系的成本一直在付，
      收益是零。现在它的有效逻辑已经合并进唯一的那个函数（见文件末尾），
      本函数改名为 *_REMOVED 并保留 30 天做对照，之后可安全删除。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        user_message: 用户消息（用于相关性搜索）
        limit: 返回数量

    Returns:
        str: 知识图谱 Prompt 文本
    """
    # 如果有用户消息，先搜索相关知识
    if user_message:
        related = search_related(session_id, character_id, user_message, limit=limit)
        if related.get("entities") or related.get("events"):
            lines = ["【相关知识】"]

            for entity in related.get("entities", [])[:5]:
                name = entity.get("name", "")
                desc = entity.get("description", "")
                if name:
                    if desc:
                        lines.append(f"- {name}：{desc}")
                    else:
                        lines.append(f"- {name}")

            for event in related.get("events", [])[:3]:
                desc = event.get("description", "")
                if desc:
                    lines.append(f"- 经历：{desc}")

            if len(lines) > 1:
                return "\n".join(lines)

    # 否则返回用户知识摘要
    return get_knowledge_summary(session_id, character_id, limit=limit)


def get_two_hop_relations(session_id, character_id, entity_name,
                           min_confidence=0.4):
    """
    两跳关系推理（不做三跳——SQLite性能保护）
    返回：A→B→C 的推理链，过滤低置信度关系
    """
    # 第一跳：找A的直接关系
    entity_a = get_entity_by_name(session_id, character_id, entity_name)
    if not entity_a:
        return []

    first_hop = get_relations_for_entity(
        entity_a["id"], session_id, character_id,
        min_confidence=min_confidence
    )
    if not first_hop:
        return []

    chains = []

    # 第二跳：对每个B找B的关系（B→C）
    for rel_ab in first_hop:
        b_id   = rel_ab.get("target_id")
        b_name = rel_ab.get("target_name", "")
        if not b_id or not b_name:
            continue

        second_hop = get_relations_for_entity(
            b_id, session_id, character_id,
            min_confidence=min_confidence,
            exclude_id=entity_a["id"]   # 避免A→B→A的环
        )

        for rel_bc in second_hop[:3]:   # 每个B最多取3条，控制结果量
            c_name = rel_bc.get("target_name", "")
            if not c_name:
                continue

            # 联合置信度：两跳相乘（越远越不确定）
            joint_conf = round(
                float(rel_ab.get("confidence", 0.5))
                * float(rel_bc.get("confidence", 0.5)),
                3
            )
            if joint_conf < 0.15:   # 联合置信度太低，跳过
                continue

            chains.append({
                "path":       [entity_name, b_name, c_name],
                "relations":  [rel_ab["relation_type"], rel_bc["relation_type"]],
                "confidence": joint_conf,
                "text":       (
                    f"{entity_name} → {_rel_cn(rel_ab['relation_type'])} → "
                    f"{b_name} → {_rel_cn(rel_bc['relation_type'])} → {c_name}"
                )
            })

    # 按联合置信度排序，取最有价值的5条
    chains.sort(key=lambda x: x["confidence"], reverse=True)
    return chains[:5]


def _rel_cn(rel_type: str) -> str:
    """关系类型转中文（复用relation.py的get_relation_type_name）"""
    try:
        from .relation import get_relation_type_name
        return get_relation_type_name(rel_type)
    except Exception:
        return rel_type


def get_knowledge_graph_for_prompt(
    session_id, character_id,
    user_message="", limit=10
):
    """
    ★ 2026-09-13 合并版（此前这里**重复定义了两次**，后者静默覆盖前者）：

    本文件里曾有两个同名函数：
      · 第一个（带 search_related 相关检索）—— 生效时能按当前消息命中实体；
      · 第二个（v2.0 实体分型 + 二跳推理）—— 定义在后面，把它**整个覆盖**了。
    结果：v1 那段"按 user_message 检索相关实体/事件"的分支成了死代码，
    抽取实体关系的成本一直付，收益却是零 ——
    用户问「你还记得我同事叫什么吗」时，图谱里明明有，却从来不按这句话去查。

    现在两者合一，按优先级拼：
      ① 当前消息命中的实体/事件（最相关，放最前）
      ② 长期结构：按类型分组的实体
      ③ 高置信度关系（contradicted 的不注入，避免错误信息）
      ④ 当前消息触发的两跳推理

    置信度过滤：contradicted(0.2-0.3)的关系不注入，避免错误信息
    """
    lines = []

    # ── Part0：当前消息命中的实体/经历（v1 的相关检索，接回来）
    if user_message and str(user_message).strip():
        try:
            related = search_related(
                session_id, character_id, str(user_message).strip(), limit=limit
            )
            _ents = related.get("entities") or []
            _evts = related.get("events") or []
            if _ents or _evts:
                lines.append("【当前话题相关的已知信息】")
                for entity in _ents[:5]:
                    name = entity.get("name", "")
                    desc = entity.get("description", "")
                    if name:
                        lines.append(f"- {name}：{desc}" if desc else f"- {name}")
                for event in _evts[:3]:
                    desc = event.get("description", "")
                    if desc:
                        lines.append(f"- 经历：{desc}")
        except Exception as e:
            print(f"[KnowledgeGraph] 相关检索失败: {e}", flush=True)

    # ── Part1：实体结构（长期事实）
    knowledge = get_user_knowledge(session_id, character_id, limit=limit)
    _type_names = {
        "person": "重要人物", "company": "工作/公司", "place": "常去地点",
        "interest": "兴趣爱好", "project": "项目/作品", "goal": "目标",
        "habit": "习惯", "skill": "技能", "pet": "宠物", "health": "健康",
        "food": "饮食偏好", "media": "喜欢的内容", "emotion": "情绪模式",
    }
    for etype, entities in knowledge.get("entities_by_type", {}).items():
        if etype in ("user", "other", "concept", "event", "object"):
            continue   # 这些类型信息太泛，不注入
        type_name = _type_names.get(etype, etype)
        names     = [e["name"] for e in entities[:5]]
        if names:
            lines.append(f"{type_name}：{'、'.join(names)}")

    # ── Part2：高置信度关系（只注入explicit和高confidence inferred）
    relations = knowledge.get("relations", [])
    high_conf_rels = [
        r for r in relations
        if float(r.get("confidence", 0.5)) >= 0.6
        and r.get("source_type", "inferred") != "contradicted"
    ]
    if high_conf_rels:
        lines.append("\n关键关系：")
        for rel in high_conf_rels[:8]:
            rel_name  = _rel_cn(rel.get("relation_type", ""))
            src_name  = rel.get("source_name", "")
            tgt_name  = rel.get("target_name", "")
            if src_name and tgt_name and rel_name:
                lines.append(f"- {src_name} {rel_name} {tgt_name}")

    # ── Part3：当前消息相关的两跳推理（有user_message时激活）
    if user_message and user_message.strip():
        import re
        words = re.findall(r'[\u4e00-\u9fa5]{2,6}', user_message)
        _seen_chains = set()
        for word in words[:3]:   # 最多检查3个词，性能保护
            chains = get_two_hop_relations(
                session_id, character_id, word,
                min_confidence=0.4
            )
            for chain in chains:
                _k = tuple(chain["path"])
                if _k in _seen_chains:
                    continue
                _seen_chains.add(_k)
                if not lines:
                    lines.append("")
                lines.append(f"- 推理：{chain['text']}（置信{chain['confidence']}）")

    if not lines:
        return ""

    return (
        "以下是你长期积累的用户知识结构（与近期记忆不重复，"
        "自然运用，不要逐条复述）：\n"
        + "\n".join(lines)
    )
