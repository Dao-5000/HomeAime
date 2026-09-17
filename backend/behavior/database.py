# -*- coding:utf-8 -*-
"""
Behavior Database v1.0
行为模式数据库：

  独立的 behavior.db 数据库，存储用户的行为模式。
  包括：时间习惯、情绪趋势、沟通行为等。
"""
import sqlite3
from pathlib import Path

from .. import config


# 数据库路径
# ★ 2026-09-17 修：与 feedback.db 同一个坑（打包目录会被 electron-builder 重建）。
#   详见 config.migrate_aux_db 的说明。
DB_PATH = config.migrate_aux_db("behavior.db")


def conn():
    """获取数据库连接"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init():
    """初始化数据库表"""
    db = conn()

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS behavior_pattern (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            pattern_type TEXT,
            pattern TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 兼容旧库：幂等 ALTER 补 character_id（老库缺列时兜底，多角色隔离）
    try:
        _cols = [r[1] for r in db.execute("PRAGMA table_info(behavior_pattern)").fetchall()]
        if "character_id" not in _cols:
            db.execute("ALTER TABLE behavior_pattern ADD COLUMN character_id TEXT DEFAULT 'default'")
        db.execute("UPDATE behavior_pattern SET character_id='default' WHERE character_id IS NULL")
    except Exception:
        pass

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_behavior_session
        ON behavior_pattern(session_id, character_id)
        """
    )

    db.commit()
    db.close()


def save_pattern(session_id, character_id, pattern_type, pattern, confidence=0.5):
    """保存行为模式"""
    db = conn()
    db.execute(
        """
        INSERT INTO behavior_pattern
        (session_id, character_id, pattern_type, pattern, confidence)
        VALUES(?,?,?,?,?)
        """,
        (session_id, character_id, pattern_type, pattern, confidence)
    )
    db.commit()
    db.close()


def get_patterns(session_id, character_id="default", limit=5):
    """获取用户的行为模式（按置信度排序）"""
    db = conn()
    rows = db.execute(
        """
        SELECT *
        FROM behavior_pattern
        WHERE session_id=?
          AND character_id=?
        ORDER BY confidence DESC
        LIMIT ?
        """,
        (session_id, character_id, limit)
    ).fetchall()
    db.close()
    return [dict(x) for x in rows]


def get_patterns_by_type(session_id, character_id, pattern_type, limit=5):
    """按类型获取行为模式"""
    db = conn()
    rows = db.execute(
        """
        SELECT *
        FROM behavior_pattern
        WHERE session_id=?
          AND character_id=?
          AND pattern_type=?
        ORDER BY confidence DESC
        LIMIT ?
        """,
        (session_id, character_id, pattern_type, limit)
    ).fetchall()
    db.close()
    return [dict(x) for x in rows]


def clear_patterns(session_id, character_id="default"):
    """清除用户的所有行为模式（用于重新分析）"""
    db = conn()
    db.execute(
        """
        DELETE FROM behavior_pattern
        WHERE session_id=?
          AND character_id=?
        """,
        (session_id, character_id)
    )
    db.commit()
    db.close()
