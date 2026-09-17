# -*- coding:utf-8 -*-
"""
Feedback Database v1.0
反馈数据库：

  独立的 feedback.db 数据库，存储用户对 AI 回复的反馈。
  包括显式反馈（点赞/点踩）和隐式反馈（回复行为）。
"""
import sqlite3
from pathlib import Path

from .. import config


# 数据库路径
# ★ 2026-09-17 修：原先是 `Path(config.ROOT_DIR) / "backend" / "data" / "feedback.db"`。
#   打包模式下 ROOT_DIR = <安装目录>/resources，于是反馈库被写进
#   resources\backend\data\ —— 而 electron-builder **每次重新打包都会删除重建**
#   win-unpacked，学习数据随之消失（真机实测该路径确实在活写：feedback.db mtime 0:58）。
#   现在统一落到 DATA_DIR（%APPDATA%\HomeAime\data），与主库同目录、一起被备份；
#   首次调用自动从旧位置搬迁一次（migrate_aux_db 幂等，旧文件保留）。
DB_PATH = config.migrate_aux_db("feedback.db")


# 反馈类型定义
FEEDBACK_TYPES = {
    "positive": "用户喜欢",
    "negative": "用户不满意",
    "continued": "用户继续聊天",
    "silence": "用户无回应",
    "correction": "用户纠正AI",
}


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
        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            message_id TEXT,
            feedback_type TEXT,
            score INTEGER DEFAULT 0,
            context TEXT DEFAULT '',
            ai_reply TEXT DEFAULT '',
            user_response TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # 兼容旧库：幂等 ALTER 补 character_id（老库缺列时兜底，多角色隔离）
    try:
        _cols = [r[1] for r in db.execute("PRAGMA table_info(ai_feedback)").fetchall()]
        if "character_id" not in _cols:
            db.execute("ALTER TABLE ai_feedback ADD COLUMN character_id TEXT DEFAULT 'default'")
        db.execute("UPDATE ai_feedback SET character_id='default' WHERE character_id IS NULL")
    except Exception:
        pass

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feedback_session
        ON ai_feedback(session_id, character_id)
        """
    )

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_feedback_type
        ON ai_feedback(feedback_type)
        """
    )

    db.commit()
    db.close()


def save_feedback(session_id, character_id, message_id, feedback_type,
                  score=0, context="", ai_reply="", user_response=""):
    """保存反馈记录"""
    db = conn()
    db.execute(
        """
        INSERT INTO ai_feedback
        (session_id, character_id, message_id, feedback_type, score, context, ai_reply, user_response)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (session_id, character_id, message_id, feedback_type, score, context, ai_reply, user_response)
    )
    db.commit()
    db.close()


def get_feedback_stats(session_id, character_id="default", limit=100):
    """获取反馈统计"""
    db = conn()
    rows = db.execute(
        """
        SELECT feedback_type, COUNT(*) as count
        FROM ai_feedback
        WHERE session_id=?
          AND character_id=?
        GROUP BY feedback_type
        ORDER BY count DESC
        LIMIT ?
        """,
        (session_id, character_id, limit)
    ).fetchall()
    db.close()
    return [dict(x) for x in rows]


def get_recent_feedback(session_id, character_id="default", limit=20):
    """获取最近的反馈记录"""
    db = conn()
    rows = db.execute(
        """
        SELECT *
        FROM ai_feedback
        WHERE session_id=?
          AND character_id=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (session_id, character_id, limit)
    ).fetchall()
    db.close()
    return [dict(x) for x in rows]


def get_feedback_by_type(session_id, character_id, feedback_type, limit=20):
    """按类型获取反馈"""
    db = conn()
    rows = db.execute(
        """
        SELECT *
        FROM ai_feedback
        WHERE session_id=?
          AND character_id=?
          AND feedback_type=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (session_id, character_id, feedback_type, limit)
    ).fetchall()
    db.close()
    return [dict(x) for x in rows]


def get_positive_rate(session_id, character_id="default"):
    """计算正面反馈率"""
    db = conn()
    row = db.execute(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN feedback_type IN ('positive', 'continued') THEN 1 ELSE 0 END) as positive
        FROM ai_feedback
        WHERE session_id=?
          AND character_id=?
        """,
        (session_id, character_id)
    ).fetchone()
    db.close()

    if not row or row["total"] == 0:
        return 0.5

    return row["positive"] / row["total"]


def clear_feedback(session_id, character_id="default"):
    """清除反馈记录（用于重置）"""
    db = conn()
    db.execute(
        """
        DELETE FROM ai_feedback
        WHERE session_id=?
          AND character_id=?
        """,
        (session_id, character_id)
    )
    db.commit()
    db.close()
