# -*- coding:utf-8 -*-
"""
Prompt 动态组装：
  根据用户ID和查询文本获取相关记忆，组装成 system prompt 片段。
  v1.1：支持向量语义检索，根据用户当前消息检索最相关的记忆。
"""
from .manager import MemoryManager
from .retriever import search_memory, retrieve_memory, hybrid_search


def build_memory_prompt(
    user_id,
    query=None,
    character_id="default",
    limit=10
):
    """
    构建用户长期记忆的 Prompt 片段。
    如果提供 query，使用向量语义检索；
    否则获取最近的重要记忆。
    """
    # 如果有查询文本，使用混合检索（向量+关键词）
    if query and str(query).strip():
        memories = hybrid_search(
            user_id=user_id,
            query=query,
            top_k=limit,
            character_id=character_id
        )
    else:
        manager = MemoryManager()
        memories = manager.get_recent(
            user_id,
            limit,
            character_id
        )

    if not memories:
        return ""

    # 按类型分组
    type_groups = {}
    for m in memories:
        mtype = m.get("memory_type", "fact")
        if mtype not in type_groups:
            type_groups[mtype] = []
        type_groups[mtype].append(m)

    # 类型标题映射
    type_titles = {
        "fact": "用户事实",
        "preference": "用户喜好",
        "episode": "重要经历",
        "emotion": "长期情绪状态",
    }

    text = "\n【用户长期记忆】\n"

    for mtype, items in type_groups.items():
        title = type_titles.get(mtype, mtype)
        text += f"\n[{title}]\n"
        for m in items:
            content = m.get("content", "")
            importance = m.get("importance", 5)
            similarity = m.get("similarity")
            # 事件时间/情境（time_hint 已拼进 context，格式「昨天；深夜低落时」），
            # 带上它 AI 才能说出「你昨天说的那件事」「你前几天说」这类带时间的话
            ctx = str(m.get("context", "") or "").strip()
            # 重要记忆加星标
            star = "★" if importance >= 8 else ""
            # 相关度高的记忆加标记
            rel = ""
            if similarity is not None:
                rel = f"（相关度{similarity:.2f}）"
            time_part = f"（{ctx[:30]}）" if ctx else ""
            text += f"- {star}{content}{time_part}{rel}\n"

    text += "\n（以上是用户的长期记忆，聊天中自然参考，不要机械复述，不要向用户提到记忆系统）\n"

    return text


def build_memory_prompt_with_keyword(
    user_id,
    keyword,
    character_id="default",
    limit=5
):
    """
    根据关键词搜索记忆，构建相关的 Prompt 片段。
    用于在特定话题下检索相关记忆。
    """
    memories = search_memory(
        user_id,
        keyword,
        limit,
        character_id
    )

    if not memories:
        return ""

    text = "\n【相关记忆】\n"
    for m in memories:
        content = m.get("content", "")
        text += f"- {content}\n"

    text += "\n（以上是与当前话题相关的记忆，自然参考即可）\n"

    return text


def build_full_memory_context(
    user_id,
    current_topic=None,
    character_id="default",
    recent_limit=10,
    topic_limit=5
):
    """
    构建完整的记忆上下文：
    1. 与当前话题相关的记忆（向量语义检索）
    2. 最近的重要记忆（作为补充）
    """
    parts = []

    # 话题相关记忆（优先，使用语义检索）
    if current_topic:
        topic = build_memory_prompt(
            user_id=user_id,
            query=current_topic,
            character_id=character_id,
            limit=topic_limit
        )
        if topic:
            parts.append(topic)

    # 最近记忆（作为补充）
    recent = build_memory_prompt(
        user_id=user_id,
        query=None,
        character_id=character_id,
        limit=recent_limit
    )
    if recent:
        parts.append(recent)

    return "\n".join(parts)
