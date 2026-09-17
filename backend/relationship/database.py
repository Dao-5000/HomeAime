# -*- coding:utf-8 -*-
"""
关系状态数据库：
  独立的 SQLite 数据库（relationship.db），存储用户与AI伴侣的关系状态。
  与主数据库和记忆数据库隔离，便于独立管理。

★ 2026-09-15 修（"亲密度/关系状态老是重置"的根因）：
  原路径 = config.ROOT_DIR/"backend"/"data"/"relationship.db"，即**安装目录内**。
  打包版 ROOT_DIR = resources/，而 electron-builder 每次 `--win dir` 都会把
  win-unpacked 整个删掉重建 —— 于是「今天重打包 → 关系表被源树里那份旧文件覆盖」
  → 亲密度/信任/阶段/互动天数全部回到几天前，行 created_at 变成今天，
  连带总览的「相识天数」从 30 天掉成 1 天（前端按 created_at 换算）。
  现在与主库统一落到用户数据目录 config.DATA_DIR（%APPDATA%/HomeAime/data，
  或 AI_COMPANION_DATA_DIR），重打包不再丢；旧位置的文件**首次读取时自动搬过来**
  （复制不删除，留一份冷备份，可回滚）。
"""
import shutil
import sqlite3
from pathlib import Path
from .. import config

# 关系数据库路径：统一落在用户数据目录（不随重打包被清）
DB_PATH = config.DATA_DIR / "relationship.db"

# 历史位置（安装目录内）——只在用户数据目录还没有库时作为一次性数据源
LEGACY_DB_PATH = config.BACKEND_DIR / "data" / "relationship.db"

_migrated = False


def _migrate_legacy_once() -> None:
    """首次访问时把旧位置的关系库搬到用户数据目录（幂等，只做一次）。"""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if DB_PATH.exists() or not LEGACY_DB_PATH.exists():
            return
        if DB_PATH.resolve() == LEGACY_DB_PATH.resolve():
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 用 SQLite 备份 API 而不是 copy：避免拿到别处正在写入的半个文件
        src = sqlite3.connect(str(LEGACY_DB_PATH))
        try:
            dst = sqlite3.connect(str(DB_PATH))
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        print("[RelationshipDB] 关系库已从安装目录迁移到用户数据目录: %s -> %s（旧文件保留为冷备份）"
              % (LEGACY_DB_PATH, DB_PATH), flush=True)
    except Exception as e:
        print("[RelationshipDB] 关系库迁移失败（继续用旧位置）: %s: %s"
              % (type(e).__name__, e), flush=True)
        # 迁移失败就退回旧路径，保证功能可用（总比没有关系数据强）
        try:
            if not DB_PATH.exists():
                globals()["DB_PATH"] = LEGACY_DB_PATH
        except Exception:
            pass


def conn():
    """获取数据库连接，自动创建目录"""
    _migrate_legacy_once()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init_relationship():
    """初始化关系数据库，创建 relationship_state 表（修复多角色共享天数）。

    坑2 修复：原表 PRIMARY KEY 为 user_id 单列，导致 character_id 被吃掉、
    increment_interaction 的天数全角色共享。改为复合主键 (user_id, character_id)。
    旧库（单列 user_id 主键）启动时安全迁移：RENAME→重建复合主键表→迁移数据→DROP。

    ★ 修复（防数据丢失）：迁移全程 try/except，旧表（relationship_state_old）只在
    迁移成功后才删除；上次迁移失败残留的半成品新表会被丢弃、旧表恢复为源数据，
    不再出现「启动即 DROP 旧表 → 迁移失败 → 数据永久丢失」。
    """
    db = conn()
    try:
        cols = [r[1] for r in db.execute("PRAGMA table_info(relationship_state)").fetchall()]

        # ★ P1-9：只有主键仍是老的单列 (user_id) 时才需要重建迁移。
        #   此前是「表存在就无条件 RENAME → 重建 → 全表 INSERT OR REPLACE → DROP 旧表」，
        #   等于每次启动都把关系表全量重建一遍：既拖慢启动，又让"迁移失败"这条高危
        #   路径每次启动都被走一遍。已经是复合主键 (user_id, character_id) 就直接跳过。
        #   PRAGMA table_info 返回 (cid, name, type, notnull, dflt_value, pk)，
        #   下标 5 的 pk 非 0 表示该列属于主键。
        _pk_cols = ([r[1] for r in db.execute("PRAGMA table_info(relationship_state)").fetchall() if r[5]]
                    if cols else [])
        _need_migrate = bool(cols) and "character_id" not in _pk_cols

        if _need_migrate:
            # 上次迁移失败的残留：relationship_state_old 已存在 → 当前 relationship_state
            # 是半成品迁移产物，丢弃重建（旧表才是完整数据源）
            _has_old = bool(db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='relationship_state_old'"
            ).fetchall())
            if _has_old:
                db.execute("DROP TABLE IF EXISTS relationship_state")
            db.execute("ALTER TABLE relationship_state RENAME TO relationship_state_old")
            try:
                db.execute("""
                CREATE TABLE relationship_state(
                    user_id TEXT NOT NULL,
                    character_id TEXT NOT NULL DEFAULT 'default',
                    stage TEXT DEFAULT 'stranger',
                    intimacy INTEGER DEFAULT 0,
                    affection INTEGER DEFAULT 50,
                    trust INTEGER DEFAULT 0,
                    interaction_days INTEGER DEFAULT 0,
                    last_interaction TEXT,
                    nickname TEXT DEFAULT '',
                    conflict_state TEXT DEFAULT '',
                    city TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, character_id)
                )
                """)
                # 检查旧表是否有 character_id 列（更老的库可能没有）
                old_cols = [r[1] for r in db.execute("PRAGMA table_info(relationship_state_old)").fetchall()]
                has_char_id = "character_id" in old_cols
                has_city = "city" in old_cols
                if has_char_id and has_city:
                    db.execute("""
                    INSERT OR REPLACE INTO relationship_state
                    (user_id, character_id, stage, intimacy, affection, trust,
                     interaction_days, last_interaction, nickname, conflict_state, city, created_at, updated_at)
                    SELECT user_id, COALESCE(character_id,'default'), stage, intimacy, affection, trust,
                           interaction_days, last_interaction, nickname, conflict_state, city, created_at, updated_at
                    FROM relationship_state_old
                    """)
                elif has_char_id:
                    db.execute("""
                    INSERT OR REPLACE INTO relationship_state
                    (user_id, character_id, stage, intimacy, affection, trust,
                     interaction_days, last_interaction, nickname, conflict_state, created_at, updated_at)
                    SELECT user_id, COALESCE(character_id,'default'), stage, intimacy, affection, trust,
                           interaction_days, last_interaction, nickname, conflict_state, created_at, updated_at
                    FROM relationship_state_old
                    """)
                else:
                    # 最老的库：没有 character_id 列，全部用 'default'
                    db.execute("""
                    INSERT OR REPLACE INTO relationship_state
                    (user_id, character_id, stage, intimacy, affection, trust,
                     interaction_days, last_interaction, nickname, conflict_state, created_at, updated_at)
                    SELECT user_id, 'default', stage, intimacy, affection, trust,
                           interaction_days, last_interaction, nickname, conflict_state, created_at, updated_at
                    FROM relationship_state_old
                    """)
                # ★ 迁移成功后才删旧表（迁移失败时旧表保留，数据不丢）
                db.execute("DROP TABLE relationship_state_old")
            except Exception as _mig_e:
                # 迁移失败：丢弃半成品新表，恢复旧表为 relationship_state（数据不丢），异常上抛记录
                try:
                    db.execute("DROP TABLE IF EXISTS relationship_state")
                    db.execute("ALTER TABLE relationship_state_old RENAME TO relationship_state")
                except Exception:
                    pass
                raise
        elif not cols:
            db.execute("""
            CREATE TABLE relationship_state(
                user_id TEXT NOT NULL,
                character_id TEXT NOT NULL DEFAULT 'default',
                stage TEXT DEFAULT 'stranger',
                intimacy INTEGER DEFAULT 0,
                affection INTEGER DEFAULT 50,
                trust INTEGER DEFAULT 0,
                interaction_days INTEGER DEFAULT 0,
                last_interaction TEXT,
                nickname TEXT DEFAULT '',
                conflict_state TEXT DEFAULT '',
                city TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, character_id)
            )
            """)

        # 索引（复合主键已含 user_id,character_id，索引保留用于单列 character_id 查询）
        db.execute("CREATE INDEX IF NOT EXISTS idx_rel_char ON relationship_state(user_id, character_id)")

        # ★ 回忆博物馆：高光/低谷事件表（按 user_id+character_id 隔离）
        db.execute("""
        CREATE TABLE IF NOT EXISTS milestone_memory(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            character_id TEXT DEFAULT 'default',
            event_type TEXT,        -- 'highlight' / 'low'
            content TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_mm ON milestone_memory(user_id, character_id)")
        # 兼容旧库：幂等 ALTER 补 character_id（老库缺列时兜底，多角色隔离）
        try:
            _mm_cols = [r[1] for r in db.execute("PRAGMA table_info(milestone_memory)").fetchall()]
            if "character_id" not in _mm_cols:
                db.execute("ALTER TABLE milestone_memory ADD COLUMN character_id TEXT DEFAULT 'default'")
            db.execute("UPDATE milestone_memory SET character_id='default' WHERE character_id IS NULL")
        except Exception:
            pass

        # ★ 防御性 ALTER：即便旧库迁移因任何原因漏掉 city 列，也在此兜底补齐（不砸数据）
        _cols = [r[1] for r in db.execute("PRAGMA table_info(relationship_state)").fetchall()]
        if "city" not in _cols:
            db.execute("ALTER TABLE relationship_state ADD COLUMN city TEXT DEFAULT ''")

        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        print(f"[RelationshipDB] 初始化/迁移失败: {e}", flush=True)
        raise
    finally:
        try:
            db.close()
        except Exception:
            pass
