# -*- coding:utf-8 -*-
"""
记忆检索器：
  提供关键词搜索、类型筛选、向量语义检索等功能。
  v1.1：增加向量语义检索（Embedding + ChromaDB）。
  检索到的记忆会自动更新 last_used 时间。
"""
from .database import get_conn  # noqa: F401（保留：外部可能仍按 retriever.get_conn 引用）
from .embedding import encode
from .vector_store import search_vector

# ════════════════════════════════════════════════════════════════════
# ★ P0-4 方案 C：记忆合并到「一套存储 + 一张表」
#   旧实现查的是独立库 memory.db 的 memories 表，而主流程 30+ 处
#   （chat_logic / idle_agent / moments / reflection / surprise / 导出导入…）
#   读的是主库 long_term_memory —— 两套记忆完全互不可见：
#     · 写进 memory.db 的记忆，valid_memories() 永远读不到
#     · 主库的记忆，又没有任何向量，hybrid_search 检索不到
#   这里统一查主库，让「写入 = 读取 = 检索」落在同一张表上。
#   memory.db 仅作为历史文件保留，不再写入、不再读取。
# ════════════════════════════════════════════════════════════════════

# 与 db.valid_memories 完全一致的记忆作用域过滤：
# global 跨角色可见；character / relationship 仅本角色可见（防记忆串桶）
_MEM_SCOPE_SQL = """
        AND (
            memory_scope = 'global'
            OR (memory_scope IN ('character', 'relationship') AND character_id = ?)
        )
"""

# 主库列顺序：id, session_id, character_id, memory_type, memory_content,
#            importance, create_time, last_used
_MEM_SELECT_COLS = (
    "id, session_id, character_id, memory_type, memory_content, "
    "importance, create_time, last_used"
)


def _row_to_mem(r):
    """主库行 → 检索器统一返回结构。

    字段名保持与旧 memories 表一致（content / user_id / importance …），
    调用方（hybrid_search 等）零改动。
    id 统一加 'sql:' 前缀，标明来源是主库，避免与历史向量 id 撞车。
    """
    return {
        "id":           f"sql:{r[0]}",
        "user_id":      r[1],
        "character_id": r[2],
        "memory_type":  r[3],
        "content":      r[4],
        "importance":   r[5],
        "created_at":   r[6],
        "last_used":    r[7],
    }


def retrieve_memory(
    user_id,
    query,
    top_k=5,
    character_id="default"
):
    """
    向量语义检索：
    将查询文本编码为向量，然后在向量库中搜索相似记忆。
    返回按相似度排序的记忆列表。
    """
    if not query or not str(query).strip():
        return []

    try:
        query_vector = encode(query)
        memories = search_vector(
            user_id=user_id,
            embedding=query_vector,
            limit=top_k,
            character_id=character_id
        )
        return memories
    except Exception as e:
        print(f"[retriever] 向量检索失败: {e}", flush=True)
        # 降级到关键词搜索
        return search_memory(
            user_id=user_id,
            keyword=query,
            limit=top_k,
            character_id=character_id
        )


def hybrid_search(
    user_id,
    query,
    top_k=5,
    character_id="default"
):
    """
    升级版混合检索
    1. 向量结果用真实similarity
    2. 关键词结果按匹配质量动态计分（不再写死0.6）
    3. 按query类型动态调整向量/关键词权重
    4. 各取 top_k*2 再合并，避免去重后不足
    5. 返回结构补全 memory_id/importance/memory_type
    """
    import re

    def _enrich(m):
        """向量结果只有 content/similarity/id，从主库补 importance/memory_type。

        ★ P0-4：记忆已合并到主库 long_term_memory 一张表，向量 id 统一为
          'sql:<主库行 id>'。这里只查主库 —— 旧实现还会按 'mdb:' 前缀去查
          已停用的 memory.db，那条分支现在不可能命中，留着只是误导。
        """
        from .. import db as _db
        out = dict(m)
        mid = m.get("id")
        if mid:
            s = str(mid)
            if s.startswith("sql:"):
                s = s[4:]
            try:
                row = _db.get_memory(int(s))
                if row:
                    out["importance"] = row.get("importance", 5)
                    out["memory_type"] = row.get("memory_type", "fact")
            except Exception:
                pass
        out.setdefault("importance", 5)
        out.setdefault("memory_type", "fact")
        return out

    # ── query类型判断（影响向量/关键词权重比例）
    # 人名/地名/专有词 → 关键词权重更高
    # 情感/语义模糊查询 → 向量权重更高
    has_proper_noun = bool(re.search(r'[\u4e00-\u9fa5]{2,4}(说|提|叫|的|去|在)', query or ""))
    if has_proper_noun:
        vec_w, kw_w = 0.45, 0.55   # 专有词查询，关键词权重更高
    else:
        vec_w, kw_w = 0.65, 0.35   # 语义查询，向量权重更高

    results = {}

    # ── 向量检索（各取 top_k*2 避免去重后不足）
    try:
        vector_results = retrieve_memory(
            user_id=user_id,
            query=query,
            top_k=top_k * 2,
            character_id=character_id
        )
        for m in vector_results:
            content = m.get("content", "")
            if not content:
                continue
            m = _enrich(m)
            raw_sim = float(m.get("similarity", 0.5))
            # 向量得分加权
            weighted = raw_sim * vec_w
            results[content] = {
                "content":      content,
                "similarity":   weighted,
                "raw_sim":      raw_sim,
                "source":       "vector",
                "memory_id":    m.get("id", ""),
                "importance":   m.get("importance", 5),
                "memory_type":  m.get("memory_type", "fact"),
            }
    except Exception:
        pass

    # ── 关键词检索（动态计算匹配质量）
    # query分词（简单按标点+空格切分，不依赖分词库）
    query_terms = [t.strip() for t in re.split(r'[\s，。！？、,.!?]+', query or "") if t.strip()]

    # ★ 按分词逐词检索，避免整句 LIKE（含空格/长句）落空
    keyword_results = []
    seen_ids = set()
    for _term in query_terms:
        try:
            _hits = search_memory(
                user_id=user_id,
                keyword=_term,
                limit=top_k * 2,
                character_id=character_id
            )
        except Exception:
            continue
        for _h in _hits:
            _hid = _h.get("id")
            if _hid in seen_ids:
                continue
            seen_ids.add(_hid)
            keyword_results.append(_h)
    # 兜底：无有效分词时整句查一次
    if not query_terms and query:
        try:
            keyword_results = search_memory(
                user_id=user_id,
                keyword=query,
                limit=top_k * 2,
                character_id=character_id
            )
        except Exception:
            pass

    for m in keyword_results:
        content = m.get("content", "")
        if not content or content in results:
            continue

        # 动态计算关键词匹配质量
        match_count = sum(1 for t in query_terms if t and t in content)
        # 位置加权：query最后一个词（最新意图）命中，额外+0.1
        position_bonus = 0.1 if query_terms and query_terms[-1] in content else 0.0
        # 长度惩罚：记忆内容过长说明匹配精度低
        length_penalty = max(0, (len(content) - 200) / 2000) * 0.1
        # 关键词得分：匹配数/总词数 * 权重 + 位置加成 - 长度惩罚
        kw_score = (
            (match_count / max(len(query_terms), 1)) * kw_w
            + position_bonus
            - length_penalty
        )
        kw_score = max(0.1, min(0.9, kw_score))  # 钳制到合理范围

        results[content] = {
            "content":      content,
            "similarity":   kw_score,
            "raw_sim":      kw_score,
            "source":       "keyword",
            "memory_id":    m.get("id", ""),
            "importance":   m.get("importance", 5),
            "memory_type":  m.get("memory_type", "fact"),
        }

    # ── importance二次加权（重要记忆得分上浮）
    for key in results:
        imp   = float(results[key].get("importance", 5))
        bonus = (imp - 5) / 50   # importance 1-10 → -0.08 ~ +0.1
        results[key]["similarity"] = min(1.0, results[key]["similarity"] + bonus)

    # ── 排序 + 截断
    sorted_results = sorted(
        results.values(),
        key=lambda x: x["similarity"],
        reverse=True
    )
    return sorted_results[:top_k]


def search_memory(
    user_id,
    keyword,
    limit=5,
    character_id="default"
):
    """
    按关键词搜索记忆（★ 统一查主库 long_term_memory）。
    返回按重要性排序的记忆列表。
    """
    from .. import db as _db

    try:
        rows = _db.q(
            f"""
            SELECT {_MEM_SELECT_COLS}
            FROM long_term_memory
            WHERE is_valid=1 AND session_id=? AND memory_content LIKE ?
            """
            + _MEM_SCOPE_SQL
            + """
            ORDER BY importance DESC
            LIMIT ?
            """,
            (user_id, f"%{keyword}%", character_id, limit),
            fetch=True,
        ) or []
    except Exception as e:
        print(f"[retriever] 关键词检索失败: {e}", flush=True)
        return []

    mems = [_row_to_mem(r) for r in rows]
    # ★ 2026-09-13 修复：**不再在这里累加召回计数**。
    #
    #   原先这里对每次检索返回的每条记忆都 recall_count+1。而本函数
    #   （以及下面的 search_by_type）会被 memory_block 一轮调用多次 ——
    #   hybrid_search 的关键词通道**对每个分词各查一次**，于是同一条记忆
    #   在一轮对话里就被 +N 次。实测后果：线上出现 rc=5628 / 5472 / 4213 的
    #   记忆，`_apply_three_way` 的 0.9^min(rc,10) 惩罚把 70 条记忆（含 9 条
    #   importance=10）永久压到 0.35 分，核心记忆反而被系统性地压下去。
    #
    #   现在召回统计只在**真实注入点**记一次：memory_manager.memory_block
    #   选出最终要注入的 mems 后调 db.touch_recall（一轮一次，语义正确）。
    return mems


def search_by_type(
    user_id,
    memory_type,
    keyword=None,
    limit=5,
    character_id="default"
):
    """按类型和关键词搜索记忆（★ 统一查主库 long_term_memory）"""
    from .. import db as _db

    try:
        if keyword:
            rows = _db.q(
                f"""
                SELECT {_MEM_SELECT_COLS}
                FROM long_term_memory
                WHERE is_valid=1 AND session_id=? AND memory_type=?
                AND memory_content LIKE ?
                """
                + _MEM_SCOPE_SQL
                + """
                ORDER BY importance DESC
                LIMIT ?
                """,
                (user_id, memory_type, f"%{keyword}%", character_id, limit),
                fetch=True,
            ) or []
        else:
            rows = _db.q(
                f"""
                SELECT {_MEM_SELECT_COLS}
                FROM long_term_memory
                WHERE is_valid=1 AND session_id=? AND memory_type=?
                """
                + _MEM_SCOPE_SQL
                + """
                ORDER BY importance DESC
                LIMIT ?
                """,
                (user_id, memory_type, character_id, limit),
                fetch=True,
            ) or []
    except Exception as e:
        print(f"[retriever] 类型检索失败: {e}", flush=True)
        return []

    mems = [_row_to_mem(r) for r in rows]
    # ★ 2026-09-13：同 search_memory —— 召回统计不在这里累加，
    #   统一由 memory_manager.memory_block 在真实注入点记一次。
    return mems


def get_all_types(
    user_id,
    character_id="default"
):
    """获取用户所有记忆类型及数量（★ 统一查主库 long_term_memory）"""
    from .. import db as _db

    try:
        rows = _db.q(
            """
            SELECT memory_type, COUNT(*) as cnt
            FROM long_term_memory
            WHERE is_valid=1 AND session_id=?
            """
            + _MEM_SCOPE_SQL
            + """
            GROUP BY memory_type
            ORDER BY cnt DESC
            """,
            (user_id, character_id),
            fetch=True,
        ) or []
    except Exception as e:
        print(f"[retriever] 类型统计失败: {e}", flush=True)
        return []

    return [
        {"type": r[0], "count": r[1]}
        for r in rows
    ]


# ★ 2026-09-13：`_update_last_used()` 已删除。
#   它原是"召回时更新使用统计"的第二条实现（只写 last_used + recall_count），
#   与 db.touch_recall() 并存 —— 两条各写一半，导致 access_count / last_recalled
#   永远为 0（线上 3486 条实测）。现在召回统计**只有一个入口**：
#       memory_manager.memory_block() → db.touch_recall(ids)
#   即"真正被注入的记忆"才记一次，一轮一次。
#   本文件里原先的两个调用点（search_memory / search_by_type）也已移除 ——
#   它们会在**每次检索调用**里对每条结果重复累加，正是 recall_count 被
#   堆到 5628 的原因。
