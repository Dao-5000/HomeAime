# -*- coding:utf-8 -*-
"""
Knowledge Graph Database v1.0
知识图谱数据库：

  三张核心表：
    1. kg_entity：实体（人/公司/地点/兴趣/项目等）
    2. kg_relation：关系（实体之间的关联）
    3. kg_event：事件（时间线上的重要事件）
"""
from .. import db


def init_knowledge_graph():
    """初始化知识图谱表"""
    # 实体表
    db.q(
        """
        CREATE TABLE IF NOT EXISTS kg_entity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            entity_type TEXT,
            name TEXT,
            description TEXT DEFAULT '',
            importance INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_kg_entity_session
        ON kg_entity(session_id, character_id)
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_kg_entity_name
        ON kg_entity(name)
        """
    )

    # 关系表
    db.q(
        """
        CREATE TABLE IF NOT EXISTS kg_relation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            source_id INTEGER,
            target_id INTEGER,
            relation_type TEXT,
            confidence REAL DEFAULT 0.5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_kg_relation_session
        ON kg_relation(session_id, character_id)
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_kg_relation_source
        ON kg_relation(source_id)
        """
    )

    # 事件表
    db.q(
        """
        CREATE TABLE IF NOT EXISTS kg_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            event_type TEXT,
            description TEXT,
            time TEXT DEFAULT '',
            importance INTEGER DEFAULT 5,
            related_entity_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    db.q(
        """
        CREATE INDEX IF NOT EXISTS idx_kg_event_session
        ON kg_event(session_id, character_id)
        """
    )

    # 增量抽取游标表（v2.0）
    db.q(
        """
        CREATE TABLE IF NOT EXISTS kg_extraction_cursor (
            session_id   TEXT,
            character_id TEXT DEFAULT 'default',
            last_msg_id  INTEGER DEFAULT 0,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(session_id, character_id)
        )
        """
    )

    # kg_relation 补 source_type / character_id 字段（旧表兼容，幂等 ALTER）
    try:
        _cols = [r[1] for r in db.q("PRAGMA table_info(kg_relation)", fetch=True)]
        if "source_type" not in _cols:
            db.q("ALTER TABLE kg_relation ADD COLUMN source_type TEXT DEFAULT 'inferred'")
        if "character_id" not in _cols:
            db.q("ALTER TABLE kg_relation ADD COLUMN character_id TEXT DEFAULT 'default'")
        db.q("UPDATE kg_relation SET character_id='default' WHERE character_id IS NULL")
    except Exception:
        pass   # 已存在则忽略


def save_entity(session_id, character_id, entity_type, name, description="", importance=5):
    """
    保存实体。

    如果同名实体已存在，则更新。
    """
    # 检查是否已存在
    existing = db.q(
        """
        SELECT id FROM kg_entity
        WHERE session_id=? AND character_id=? AND name=?
        LIMIT 1
        """,
        (session_id, character_id, name),
        fetch=True
    )

    if existing:
        entity_id = existing[0]["id"]
        db.q(
            """
            UPDATE kg_entity
            SET entity_type=?, description=?, importance=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (entity_type, description, importance, entity_id)
        )
        return entity_id
    else:
        db.q(
            """
            INSERT INTO kg_entity
            (session_id, character_id, entity_type, name, description, importance)
            VALUES(?,?,?,?,?,?)
            """,
            (session_id, character_id, entity_type, name, description, importance)
        )
        # db.q 的 INSERT 不返回 cursor → 用 last_insert_rowid 兜底
        try:
            _row = db.q("SELECT last_insert_rowid() AS rid", fetch=True)
            return _row[0]["rid"] if _row else None
        except Exception:
            return None


def save_relation(session_id, source_id, target_id, relation_type, confidence=0.5,
                  source_type="inferred", character_id="default"):
    """保存关系（v2.0：带 confidence 和 source_type；★ 按角色隔离）"""
    # 检查是否已存在相同关系
    existing = db.q(
        """
        SELECT id FROM kg_relation
        WHERE session_id=? AND character_id=? AND source_id=? AND target_id=? AND relation_type=?
        LIMIT 1
        """,
        (session_id, character_id, source_id, target_id, relation_type),
        fetch=True
    )

    if existing:
        return existing[0]["id"]

    result = db.q(
        """
        INSERT INTO kg_relation
        (session_id, character_id, source_id, target_id, relation_type, confidence, source_type)
        VALUES(?,?,?,?,?,?,?)
        """,
        (session_id, character_id, source_id, target_id, relation_type, confidence, source_type)
    )
    # db.q 的 INSERT 不返回 cursor → 用 last_insert_rowid 兜底
    try:
        _row = db.q("SELECT last_insert_rowid() AS rid", fetch=True)
        return _row[0]["rid"] if _row else None
    except Exception:
        return None


def save_event(session_id, character_id, event_type, description, time="", importance=5, related_entity_id=None):
    """保存事件"""
    db.q(
        """
        INSERT INTO kg_event
        (session_id, character_id, event_type, description, time, importance, related_entity_id)
        VALUES(?,?,?,?,?,?,?)
        """,
        (session_id, character_id, event_type, description, time, importance, related_entity_id)
    )
    # db.q 的 INSERT 不返回 cursor → 用 last_insert_rowid 兜底
    try:
        _row = db.q("SELECT last_insert_rowid() AS rid", fetch=True)
        return _row[0]["rid"] if _row else None
    except Exception:
        return None


def get_entity_by_name(session_id, character_id, name):
    """根据名称获取实体"""
    rows = db.q(
        """
        SELECT * FROM kg_entity
        WHERE session_id=? AND character_id=? AND name=?
        LIMIT 1
        """,
        (session_id, character_id, name),
        fetch=True
    )
    return dict(rows[0]) if rows else None


def get_entities_by_type(session_id, character_id, entity_type, limit=20):
    """按类型获取实体"""
    rows = db.q(
        """
        SELECT * FROM kg_entity
        WHERE session_id=? AND character_id=? AND entity_type=?
        ORDER BY importance DESC
        LIMIT ?
        """,
        (session_id, character_id, entity_type, limit),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_all_entities(session_id, character_id, limit=50):
    """获取所有实体"""
    rows = db.q(
        """
        SELECT * FROM kg_entity
        WHERE session_id=? AND character_id=?
        ORDER BY importance DESC
        LIMIT ?
        """,
        (session_id, character_id, limit),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_relations_for_entity(entity_id, session_id, character_id="default",
                              min_confidence=0.0, exclude_id=None):
    """
    查实体的一跳关系（v2.0，★ 按角色隔离）
    min_confidence: 最低置信度过滤
    exclude_id: 排除的目标实体id（防环）
    返回带 source_name/target_name
    """
    sql = """
        SELECT r.*,
               e.name as target_name, e.entity_type as target_type,
               se.name as source_name, se.entity_type as source_type_name
        FROM kg_relation r
        LEFT JOIN kg_entity e ON r.target_id = e.id
        LEFT JOIN kg_entity se ON r.source_id = se.id
        WHERE r.session_id=?
          AND r.character_id=?
          AND r.source_id=?
          AND r.confidence >= ?
    """
    params = [session_id, character_id, entity_id, min_confidence]
    if exclude_id:
        sql    += " AND r.target_id != ?"
        params.append(exclude_id)
    sql += " ORDER BY r.confidence DESC LIMIT 20"
    rows = db.q(sql, params, fetch=True)
    return [dict(r) for r in rows] if rows else []


def get_extraction_cursor(session_id, character_id="default"):
    """读取增量抽取游标"""
    rows = db.q("""SELECT * FROM kg_extraction_cursor
               WHERE session_id=? AND character_id=?""",
             (session_id, character_id), fetch=True)
    return dict(rows[0]) if rows else None


def get_events(session_id, character_id, limit=20):
    """获取事件列表"""
    rows = db.q(
        """
        SELECT * FROM kg_event
        WHERE session_id=? AND character_id=?
        ORDER BY importance DESC, created_at DESC
        LIMIT ?
        """,
        (session_id, character_id, limit),
        fetch=True
    )
    return [dict(r) for r in rows]
