# -*- coding:utf-8 -*-
"""
记忆管理器：
  提供记忆的增删改查功能，以及从聊天中自动提取记忆并保存。
  v1.1：添加记忆时自动生成 Embedding 并存储到向量库。
"""
from .database import get_conn  # noqa: F401（保留：历史文件 memory.db 仍可能按此引用）
from .extractor import extract_memory
from .embedding import encode
from .vector_store import delete_vector, upsert_vector

# ════════════════════════════════════════════════════════════════════
# ★ P0-4 方案 C：记忆合并到「一套存储 + 一张表」
#   本模块原来整套 CRUD 都落在独立库 memory.db 的 memories 表上，
#   而主流程 30+ 处（chat_logic / idle_agent / moments / reflection /
#   surprise / 导出导入 …）读的是主库 long_term_memory —— 两套记忆互不可见。
#   现在读写全部落到主库，memory.db 仅作为历史文件保留，不再读写。
#   对外字段名保持与旧 memories 表一致（content / user_id / importance …），
#   调用方（prompt_builder、proactive/triggers 等）零改动。
# ════════════════════════════════════════════════════════════════════

# 与 db.valid_memories 一致的作用域过滤：global 跨角色可见，
# character / relationship 仅本角色可见（防记忆串桶）
_SCOPE_SQL = """
            AND (
                memory_scope = 'global'
                OR (memory_scope IN ('character', 'relationship') AND character_id = ?)
            )
"""

_COLS = (
    "id, session_id, character_id, memory_type, memory_content, "
    "importance, create_time, last_used"
)


def _row_to_mem(r):
    """主库行 → 旧 schema 结构；id 统一加 'sql:' 前缀标明来源。"""
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


def _q(sql, args=(), fetch=True):
    """走主库统一入口（自带锁）。任何异常都返回空结果，静默降级。"""
    from .. import db as _db
    try:
        return _db.q(sql, args, fetch=fetch)
    except Exception as e:
        print(f"[manager] 主库查询失败: {e}", flush=True)
        return [] if fetch else None


def _strip_prefix(memory_id):
    """'sql:123' → 123；兼容不带前缀的裸 id。"""
    s = str(memory_id)
    return int(s[4:]) if s.startswith("sql:") else int(s)


class MemoryManager:
    """记忆管理器：提供记忆的增删改查功能（★ 统一读写主库 long_term_memory）"""

    def add_memory(
        self,
        user_id,
        memory_type,
        content,
        importance=5,
        character_id="default"
    ):
        """添加一条记忆，同时生成向量并存储到向量库。

        ★ P0-4 方案 C：统一写入主库 long_term_memory（唯一权威表）。
        原来写的是独立库 memory.db 的 memories 表，而主流程 30+ 处
        db.valid_memories() 读的是主库 —— 写进 memory.db 的记忆等于石沉大海，
        主流程永远看不到；主库的记忆又没有向量，检索也搜不到。两套记忆互不可见。
        现在写入与主流程同源，并为向量检索生成向量（统一 'sql:' 前缀命名空间）。
        任何一步失败都静默降级，绝不因为存储问题影响对话主流程。
        """
        from .. import db as _db

        # 1. 写入主库（唯一权威表）
        try:
            memory_id = _db.insert_memory(
                content,
                memory_type=memory_type,
                importance=int(importance) if importance else 5,
                session_id=user_id,
                character_id=character_id,
            )
        except Exception as e:
            print(f"[manager] 写入主库失败: {e}", flush=True)
            return 0

        # 2. 生成向量并存入向量库
        if memory_id:
            try:
                vector = encode(content)
            except Exception as e:
                print(f"[manager] 生成向量失败: {e}", flush=True)
                vector = None

            if vector:
                try:
                    # ★ 统一 'sql:' 前缀：标明来源是主库 long_term_memory 的行 id。
                    #   旧前缀 'mdb:' 指向已停用的 memory.db，不再使用。
                    # ★ P0-4：用 upsert_vector（原子、同 id 幂等），不用 add_vector。
                    upsert_vector(
                        memory_id=f"sql:{memory_id}",
                        user_id=user_id,
                        content=content,
                        embedding=vector,
                        metadata={
                            "type": memory_type,
                            "importance": importance
                        },
                        character_id=character_id
                    )
                except Exception as e:
                    print(f"[manager] 存储向量失败: {e}", flush=True)

        return memory_id

    def get_recent(
        self,
        user_id,
        limit=10,
        character_id="default"
    ):
        """获取最近的记忆，按重要性和创建时间排序（★ 查主库）"""
        rows = _q(
            f"""
            SELECT {_COLS}
            FROM long_term_memory
            WHERE is_valid=1 AND session_id=?
            """ + _SCOPE_SQL + """
            ORDER BY importance DESC, create_time DESC
            LIMIT ?
            """,
            (user_id, character_id, limit),
        ) or []
        return [_row_to_mem(r) for r in rows]

    def get_by_type(
        self,
        user_id,
        memory_type,
        limit=10,
        character_id="default"
    ):
        """按类型获取记忆（★ 查主库）"""
        rows = _q(
            f"""
            SELECT {_COLS}
            FROM long_term_memory
            WHERE is_valid=1 AND session_id=? AND memory_type=?
            """ + _SCOPE_SQL + """
            ORDER BY importance DESC, create_time DESC
            LIMIT ?
            """,
            (user_id, memory_type, character_id, limit),
        ) or []
        return [_row_to_mem(r) for r in rows]

    # ★ 2026-09-13：`update_last_used()` 已删除。
    #   它与 memory/retriever._update_last_used 各写一条重复的 UPDATE
    #   （只维护 last_used + recall_count），而真正写全 access_count /
    #   last_recalled 的 db.touch_recall 从未被调用 —— 两条各写一半，
    #   结果那两列永远是 0 / 空。
    #   现在召回统计只有一个入口：db.touch_recall(ids)，
    #   由 memory_manager.memory_block() 在真正注入时调用（一轮一次）。

    def delete_memory(self, memory_id):
        """删除一条记忆（★ 主库软删除 is_valid=0），同时删除向量"""
        from .. import db as _db
        mid = _strip_prefix(memory_id)
        try:
            _db.invalidate_memory(mid, reason="deleted")
        except Exception as e:
            print(f"[manager] 主库软删除失败: {e}", flush=True)

        # 同时删除向量库中的向量（★ 统一 'sql:' 前缀，与写入命名空间一致）
        try:
            delete_vector(f"sql:{mid}")
        except Exception as e:
            print(f"[manager] 删除向量失败: {e}", flush=True)

    def count_memories(
        self,
        user_id,
        character_id="default"
    ):
        """统计用户记忆数量（★ 查主库）"""
        rows = _q(
            """
            SELECT COUNT(*) as cnt
            FROM long_term_memory
            WHERE is_valid=1 AND session_id=?
            """ + _SCOPE_SQL,
            (user_id, character_id),
        ) or []
        return rows[0][0] if rows else 0


async def save_from_chat(
    user_id,
    llm,
    chat,
    character_id="default"
):
    """
    从聊天中自动提取记忆并保存。
    只保存重要性 >= 5 的记忆。
    """
    memories = await extract_memory(
        llm,
        chat
    )

    manager = MemoryManager()

    for m in memories:
        # ★ 长期陪伴：放宽保存门槛（5→4），让普通但有用的记忆也能留下
        if m.get("importance", 5) >= 4:
            manager.add_memory(
                user_id,
                m.get("type", "fact"),
                m.get("content", ""),
                m.get("importance", 5),
                character_id
            )

    return len(memories)
