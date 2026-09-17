# -*- coding:utf-8 -*-
"""
Knowledge Graph Prompt v1.0
知识图谱 Prompt 构建：

  将知识图谱数据转换为 LLM 可理解的 Prompt 文本。
"""
from .query import get_knowledge_graph_for_prompt, get_knowledge_summary


def build_knowledge_prompt(session_id, character_id, user_message="", limit=10):
    """
    构建知识图谱 Prompt。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        user_message: 用户消息（用于相关性搜索）
        limit: 返回数量

    Returns:
        str: 知识图谱 Prompt 文本
    """
    try:
        content = get_knowledge_graph_for_prompt(
            session_id,
            character_id,
            user_message,
            limit=limit
        )
    except Exception as e:
        print(f"[KnowledgeGraph] 构建Prompt失败: {e}", flush=True)
        return ""

    if not content:
        return ""

    return (
        "以下是用户的知识结构信息，帮助理解用户的人生结构和事物关联。"
        "自然理解和运用，不要向用户提到知识图谱。\n\n"
        + content
    )


def build_knowledge_summary_prompt(session_id, character_id, limit=10):
    """
    构建知识图谱摘要 Prompt。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        limit: 返回数量

    Returns:
        str: 知识图谱摘要 Prompt
    """
    try:
        summary = get_knowledge_summary(session_id, character_id, limit=limit)
    except Exception as e:
        print(f"[KnowledgeGraph] 构建摘要失败: {e}", flush=True)
        return ""

    if not summary:
        return ""

    return (
        "【用户知识结构】\n"
        "以下是根据长期互动整理的用户人生结构，"
        "帮助理解用户的整体情况。\n"
        + summary
    )


def format_entity(entity):
    """格式化实体为文本"""
    if not entity:
        return ""

    name = entity.get("name", "")
    entity_type = entity.get("entity_type", "")
    description = entity.get("description", "")

    from .entity import get_entity_type_name
    type_name = get_entity_type_name(entity_type)

    if description:
        return f"{name}（{type_name}）：{description}"
    else:
        return f"{name}（{type_name}）"


def format_relation(relation, entities_map=None):
    """格式化关系为文本"""
    if not relation:
        return ""

    source_name = relation.get("source_name", "")
    target_name = relation.get("target_name", "")
    relation_type = relation.get("relation_type", "")

    from .relation import get_relation_type_name
    rel_name = get_relation_type_name(relation_type)

    if source_name and target_name:
        return f"{source_name} {rel_name} {target_name}"
    elif target_name:
        return f"{rel_name}：{target_name}"
    else:
        return ""


def format_event(event):
    """格式化事件为文本"""
    if not event:
        return ""

    description = event.get("description", "")
    time_str = event.get("time", "")
    importance = event.get("importance", 5)

    if time_str:
        return f"[{time_str}] {description}"
    else:
        return description
