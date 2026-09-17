# -*- coding:utf-8 -*-
"""
记忆系统数据库模块：
  独立的 SQLite 数据库（memory.db），存储用户长期记忆。
  与主数据库隔离，便于独立管理和迁移。
"""
import sqlite3
from pathlib import Path
from .. import config

# 记忆数据库路径：与主库同目录（DATA_DIR）
# ★ 2026-09-17 修：原先是 `config.ROOT_DIR / "backend" / "data" / "memory.db"`，
#   打包模式下落进 resources\backend\data\ —— electron-builder 重打包会删除重建，
#   库随打包一起消失。现统一到 DATA_DIR 并自动搬迁（详见 config.migrate_aux_db）。
DB_PATH = config.migrate_aux_db("memory.db")


def get_conn():
    """获取数据库连接，自动创建目录"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """初始化记忆数据库，创建 memories 表"""
    conn = get_conn()

    conn.execute("""
    CREATE TABLE IF NOT EXISTS memories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL DEFAULT 'default',
        memory_type TEXT NOT NULL,
        content TEXT NOT NULL,
        importance INTEGER DEFAULT 5,
        embedding TEXT,
        vector_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_used TIMESTAMP
    )
    """)

    # 兼容旧库：增加 character_id 字段
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()]
    if "character_id" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN character_id TEXT DEFAULT 'default'")
    if "vector_id" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN vector_id TEXT")

    # 创建索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id, character_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance)")

    conn.commit()
    conn.close()
