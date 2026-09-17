# -*- coding:utf-8 -*-
"""
Reflection Database v1.0
反思数据库：

  存储AI对用户和关系的长期理解。
"""
from .. import db


def init_reflection_db():
    """初始化反思数据库表"""
    db.q(
        """
        CREATE TABLE IF NOT EXISTS memory_reflection (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            reflection_type TEXT,
            content TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP
        )
        """
    )

    # 兼容旧库：幂等 ALTER 补 character_id（老库缺列时兜底，多角色隔离）
    try:
        _cols = [r[1] for r in db.q("PRAGMA table_info(memory_reflection)", fetch=True)]
        if "character_id" not in _cols:
            db.q("ALTER TABLE memory_reflection ADD COLUMN character_id TEXT DEFAULT 'default'")
        db.q("UPDATE memory_reflection SET character_id='default' WHERE character_id IS NULL")
    except Exception:
        pass

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_reflection_session
        ON memory_reflection(session_id, character_id)
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_reflection_type
        ON memory_reflection(reflection_type)
        """
    )


def save_reflection(session_id, character_id, reflection_type, content, confidence=0.5):
    """保存反思"""
    result = db.q(
        """
        INSERT INTO memory_reflection
        (session_id, character_id, reflection_type, content, confidence)
        VALUES(?,?,?,?,?)
        """,
        (session_id, character_id, reflection_type, content, confidence)
    )
    return result.lastrowid if hasattr(result, 'lastrowid') else None


def get_recent_reflections(session_id, character_id, limit=10):
    """获取最近的反思"""
    rows = db.q(
        """
        SELECT * FROM memory_reflection
        WHERE session_id=? AND character_id=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (session_id, character_id, limit),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_reflections_by_type(session_id, character_id, reflection_type, limit=5):
    """按类型获取反思"""
    rows = db.q(
        """
        SELECT * FROM memory_reflection
        WHERE session_id=? AND character_id=? AND reflection_type=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (session_id, character_id, reflection_type, limit),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_reflection_count(session_id, character_id):
    """获取反思数量"""
    rows = db.q(
        """
        SELECT COUNT(*) as cnt FROM memory_reflection
        WHERE session_id=? AND character_id=?
        """,
        (session_id, character_id),
        fetch=True
    )
    return int(rows[0]["cnt"]) if rows else 0


def touch_reflection(reflection_id):
    """更新反思的最后使用时间"""
    db.q(
        """
        UPDATE memory_reflection
        SET last_used=CURRENT_TIMESTAMP
        WHERE id=?
        """,
        (reflection_id,)
    )
