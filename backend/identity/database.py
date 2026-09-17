# -*- coding:utf-8 -*-
"""
Identity Database v1.0
身份数据库：

  存储AI的四层身份信息。
"""
from .. import db


# 身份类型
IDENTITY_TYPES = [
    "core",           # 核心身份（角色本体）
    "relationship",   # 关系身份
    "experience",     # 经历身份
    "expression",     # 表达身份
]


def init_identity_db():
    """初始化身份数据库表"""
    db.q(
        """
        CREATE TABLE IF NOT EXISTS ai_identity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            identity_type TEXT,
            content TEXT,
            importance INTEGER DEFAULT 10,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 兼容旧库：幂等 ALTER 补 character_id（老库缺列时兜底，多角色隔离）
    try:
        _cols = [r[1] for r in db.q("PRAGMA table_info(ai_identity)", fetch=True)]
        if "character_id" not in _cols:
            db.q("ALTER TABLE ai_identity ADD COLUMN character_id TEXT DEFAULT 'default'")
        db.q("UPDATE ai_identity SET character_id='default' WHERE character_id IS NULL")
    except Exception:
        pass

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_identity_session
        ON ai_identity(session_id, character_id)
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_identity_type
        ON ai_identity(identity_type)
        """
    )


def save_identity(session_id, character_id, identity_type, content, importance=10):
    """保存身份信息"""
    # 检查是否已存在同类型身份
    existing = db.q(
        """
        SELECT id FROM ai_identity
        WHERE session_id=? AND character_id=? AND identity_type=?
        LIMIT 1
        """,
        (session_id, character_id, identity_type),
        fetch=True
    )

    if existing:
        identity_id = existing[0]["id"]
        db.q(
            """
            UPDATE ai_identity
            SET content=?, importance=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (content, importance, identity_id)
        )
        return identity_id
    else:
        result = db.q(
            """
            INSERT INTO ai_identity
            (session_id, character_id, identity_type, content, importance)
            VALUES(?,?,?,?,?)
            """,
            (session_id, character_id, identity_type, content, importance)
        )
        return result.lastrowid if hasattr(result, 'lastrowid') else None


def get_identity(session_id, character_id):
    """获取所有身份信息"""
    rows = db.q(
        """
        SELECT * FROM ai_identity
        WHERE session_id=? AND character_id=?
        ORDER BY importance DESC
        """,
        (session_id, character_id),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_identity_by_type(session_id, character_id, identity_type):
    """按类型获取身份信息"""
    rows = db.q(
        """
        SELECT * FROM ai_identity
        WHERE session_id=? AND character_id=? AND identity_type=?
        LIMIT 1
        """,
        (session_id, character_id, identity_type),
        fetch=True
    )
    return dict(rows[0]) if rows else None


def delete_identity(session_id, character_id, identity_type):
    """删除身份信息"""
    db.q(
        """
        DELETE FROM ai_identity
        WHERE session_id=? AND character_id=? AND identity_type=?
        """,
        (session_id, character_id, identity_type)
    )
