# -*- coding: utf-8 -*-
"""
目标系统 —— 骨子"自主规划行动"的载体。

用户说"我想坚持跑步一个月""我想早点睡"，骨子记成目标，之后：
  · 记录进度；
  · 由 scheduler 定时检查（复用现有 60s tick），到期/到点提醒、追问进度；
  · 目标完成/放弃后，交给反思引擎总结（复用 reflection/）。

数据层：db 新建 agent_goals 表（懒创建），带 session_id + character_id 双隔离键。
"""
import time

_TABLE = "agent_goals"
_ready = False


def _ensure_table():
    global _ready
    if _ready:
        return True
    try:
        from .. import db
        db.q(f"""
        CREATE TABLE IF NOT EXISTS {_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            progress_note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        db.q(f"CREATE INDEX IF NOT EXISTS idx_agoal_sess ON {_TABLE}(session_id, character_id)")
        _ready = True
        return True
    except Exception:
        return False


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def record_goal(session_id: str, character_id: str, content: str) -> int:
    """记录一个新目标，返回 goal_id；失败返回 0。"""
    content = str(content or "").strip()
    if not content or not _ensure_table():
        return 0
    try:
        from .. import db
        now = _now()
        cur = db.q(f"INSERT INTO {_TABLE}(session_id, character_id, content, status, created_at, updated_at) "
                   f"VALUES(?,?,?,?,?,?)",
                   (session_id, character_id, content, "active", now, now))
        return int(cur.lastrowid) if cur else 0
    except Exception:
        return 0


def update_progress(goal_id: int, note: str) -> bool:
    try:
        from .. import db
        db.q(f"UPDATE {_TABLE} SET progress_note=?, updated_at=? WHERE id=?",
             (str(note or "")[:500], _now(), goal_id))
        return True
    except Exception:
        return False


def complete_goal(goal_id: int, status: str = "done") -> bool:
    try:
        from .. import db
        db.q(f"UPDATE {_TABLE} SET status=?, updated_at=? WHERE id=?",
             (status, _now(), goal_id))
        return True
    except Exception:
        return False


def list_goals(session_id: str, character_id: str, active_only: bool = True) -> list:
    """列出目标。active_only=True 只返回进行中的。"""
    if not _ensure_table():
        return []
    try:
        from .. import db
        if active_only:
            rows = db.q(f"SELECT * FROM {_TABLE} WHERE session_id=? AND character_id=? AND status='active' "
                        f"ORDER BY id DESC LIMIT 50",
                        (session_id, character_id), fetch=True) or []
        else:
            rows = db.q(f"SELECT * FROM {_TABLE} WHERE session_id=? AND character_id=? "
                        f"ORDER BY id DESC LIMIT 50",
                        (session_id, character_id), fetch=True) or []
        return [dict(r) for r in rows]
    except Exception:
        return []


def build_goal_block(session_id: str, character_id: str) -> str:
    """进行中的目标注入块 —— 让骨子主动推进、适时提醒进度。"""
    goals = list_goals(session_id, character_id, active_only=True)
    if not goals:
        return ""
    lines = ["【TA 正在坚持的目标（你应主动关心进度、适时鼓励）】"]
    for g in goals[:5]:
        note = f"（最近进度：{g['progress_note']}）" if g.get("progress_note") else ""
        lines.append(f"- {g['content']}{note}")
    return "\n".join(lines)
