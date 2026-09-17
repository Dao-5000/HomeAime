# -*- coding: utf-8 -*-
"""
数据库层：SQLite 本地文件（backend/data/local_db.db）
表：
  chat_history      聊天记录（后端接管持久化，换设备可随 db 迁移）
  long_term_memory  长期记忆库（手动 + 自动提炼）
  kv                杂项键值（轮次计数器、最后活跃时间、亲密度快照等）
"""
import sqlite3
import threading
import contextlib
import json
import asyncio as _asyncio
import functools as _functools
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from backend.loop_compat import get_loop


def _run_sync(fn, *args, **kwargs):
    """
    把同步DB调用扔进线程池，不阻塞事件循环。
    用法：await _run_sync(db.valid_memories, session_id=sid)
    """
    loop = get_loop()
    return loop.run_in_executor(
        None,
        _functools.partial(fn, *args, **kwargs)
    )


# ── 高频异步包装（供 controller/memory_manager/scheduler 直接 await）──

async def async_valid_memories(session_id="default", character_id="default", category=None):
    return await _run_sync(valid_memories, session_id=session_id,
                           character_id=character_id, category=category)

async def async_recent_messages(session_id: str, limit: int = 24, character_id: str = "default"):
    return await _run_sync(recent_messages, session_id, limit, character_id)

async def async_add_message(session_id: str, role: str, content: str,
                             character_id: str = "default", extra: dict = None):
    return await _run_sync(add_message, session_id, role, content, character_id, extra)

async def async_get_profile(session_id="default", character_id="default"):
    return await _run_sync(get_profile, session_id, character_id)

async def async_get_personality_state(session_id="default", character_id="default"):
    return await _run_sync(get_personality_state, session_id, character_id)

async def async_get_relationship_state(session_id="default", character_id="default"):
    return await _run_sync(get_relationship_state, session_id, character_id)

async def async_get_emotion_state(session_id="default", character_id="default"):
    return await _run_sync(get_emotion_state, session_id, character_id)

async def async_get_open_loops(session_id="default", character_id="default", limit=10):
    return await _run_sync(get_open_loops, session_id, character_id, limit)

async def async_kv_get(key: str):
    return await _run_sync(kv_get, key)

async def async_kv_set(key: str, value):
    return await _run_sync(kv_set, key, value)

async def async_get_recent_timeline_events(session_id="default", character_id="default", limit=5):
    return await _run_sync(get_recent_timeline_events, session_id, character_id, limit)


DB_PATH = config.DATA_DIR / "local_db.db"
_lock = threading.RLock()
_conn = None


def _is_internal_prompt_text(text) -> bool:
    """识别主动消息/定时提醒等内部任务文本，避免污染普通聊天历史。"""
    try:
        s = str(text or "").strip()
    except Exception:
        return False
    if not s:
        return False
    compact = s.replace("，", ",").replace("：", ":").replace(" ", "")
    bracketed = (
        (compact.startswith("（") and compact.endswith("）"))
        or (compact.startswith("(") and compact.endswith(")"))
    )
    # 旧版主动 Agent 使用括号包裹内部提示；新版部分路径曾把
    # 「近期心情/关于用户」摘要直接作为 user 写入，这类内容也必须视为内部伪消息。
    # 只在文本以明确的系统摘要标题开头、且至少包含两个标题时拦截，避免误伤
    # 用户正常谈论“我的近期心情”等句子。
    summary_heads = ("近期心情", "关于用户", "用户偏好", "近期事件", "AI状态", "AI 当前状态")
    head_count = sum(1 for h in summary_heads if s.startswith(h) or ("\n" + h) in s)
    if head_count >= 2:
        return True
    if not bracketed:
        return False
    # 只拦截后台任务格式，不拦截用户真实说“你以后主动发消息”之类的需求。
    if ("空闲时间到了" in compact or "定时时间到了" in compact) and (
        "主动发消息" in compact or "主动给" in compact
    ):
        return True
    if "当前时间:" in compact and "主动给" in compact and "发" in compact:
        return True
    return False


def is_internal_chat_message(role, content) -> bool:
    """判断是否应从普通聊天历史中隐藏的系统任务伪消息。"""
    try:
        if str(role or "").strip() != "user":
            return False
        return _is_internal_prompt_text(content)
    except Exception:
        return False


def _filter_chat_rows(rows):
    out = []
    for r in rows or []:
        try:
            role = r["role"] if isinstance(r, sqlite3.Row) else r.get("role")
            content = r["content"] if isinstance(r, sqlite3.Row) else r.get("content")
        except Exception:
            role = None
            content = None
        if is_internal_chat_message(role, content):
            continue
        out.append(r)
    return out


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def init():
    global _conn
    with _lock:
        if _conn is not None:
            return
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            extra TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id, character_id, id);
        CREATE TABLE IF NOT EXISTS long_term_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            -- 防记忆串桶：新记忆默认 character（角色专属），
            -- 只有明确判定为"用户中性基础信息"才允许 global（跨角色共享）
            memory_scope TEXT NOT NULL DEFAULT 'character',
            memory_content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            memory_type TEXT NOT NULL DEFAULT 'fact',
            importance INTEGER NOT NULL DEFAULT 5,
            create_time TEXT NOT NULL,
            update_time TEXT,
            last_used TEXT,
            is_valid INTEGER NOT NULL DEFAULT 1,
            memory_status TEXT DEFAULT 'active',
            confidence REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            decay_score REAL DEFAULT 1.0,
            context TEXT DEFAULT '',
            emotion_tag TEXT DEFAULT '',
            source_text TEXT DEFAULT '',
            recall_count INTEGER DEFAULT 0,
            last_recalled TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            character_name TEXT NOT NULL DEFAULT 'default',
            task_type TEXT NOT NULL DEFAULT 'once',
            trigger_time TEXT NOT NULL,
            content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT '',
            fired_at TEXT,
            source TEXT DEFAULT 'chat',
            idempotency_key TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, trigger_time);
        CREATE TABLE IF NOT EXISTS user_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            nickname TEXT DEFAULT '',
            personality TEXT DEFAULT '',
            occupation TEXT DEFAULT '',
            hobbies TEXT DEFAULT '',
            dislikes TEXT DEFAULT '',
            communication_style TEXT DEFAULT '',
            emotional_traits TEXT DEFAULT '',
            ext_json TEXT DEFAULT '{}',
            updated_time TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            summary TEXT NOT NULL DEFAULT '',
            covered_until_id INTEGER NOT NULL DEFAULT 0,
            created_time TEXT NOT NULL,
            update_time TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_session
        ON conversation_summary(session_id, character_id);
        -- ★ 摘要块（2026-09-13）：conversation_summary 那一行是「合并重写」的摘要，
        --   每次更新都要把上一版摘要再压一遍，专有名词被反复洗掉（洗到第三遍就失效）。
        --   这里改成只追加：每块覆盖一段固定的消息 id 区间，产出后永不修改，
        --   注入时挑最近几块。任何一条事实最多只被 LLM 压一次，「洗掉名字」的路径就断了。
        --   conversation_summary 继续保留为「最近一版」的兼容视图（idle_agent/moments 在读）。
        CREATE TABLE IF NOT EXISTS conversation_summary_block (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            from_msg_id INTEGER NOT NULL DEFAULT 0,
            to_msg_id INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            created_time TEXT NOT NULL,
            UNIQUE(session_id, character_id, to_msg_id)
        );        CREATE INDEX IF NOT EXISTS idx_summary_block_scope
        ON conversation_summary_block(session_id, character_id, to_msg_id DESC);

        -- ★ 2026-09-13 摘要块 ↔ 事实关联指针
        --   背景：摘要块是"只追加、不重写"的（有意的：避免递归压缩把专有名词
        --   越洗越丢）。代价实测暴露过：id=17 那块写着「AI 住在用户的电脑 4060 里」，
        --   而本地部署从未成功 —— 库里的错误记忆已清理，摘要块却不会跟着更新，
        --   错误叙述继续被注入。
        --   这两列让"块依赖哪条事实"可追踪：
        --     linked_facts —— 逗号分隔的 long_term_memory.id（人工/工具确认后写入）
        --     facts_stale  —— 1 表示其中至少一条已失效 → 注入时降级为历史叙述
        --   取"标注 + 降级"而不是"重写摘要块"，是为了不破坏只追加的不可变性。
        CREATE TABLE IF NOT EXISTS daily_memory_report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            report_date TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            created_time TEXT NOT NULL,
            update_time TEXT,
            UNIQUE(session_id, character_id, report_date)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_memory_report
        ON daily_memory_report(session_id, character_id, report_date DESC);
        CREATE TABLE IF NOT EXISTS emotional_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            mood TEXT DEFAULT '',
            intensity INTEGER DEFAULT 0,
            reason TEXT DEFAULT '',
            needs TEXT DEFAULT '',
            positive_keywords TEXT DEFAULT '',
            negative_keywords TEXT DEFAULT '',
            updated_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_emotion_session
        ON emotional_state(session_id, character_id);
        CREATE TABLE IF NOT EXISTS emotion_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            mood TEXT DEFAULT '',
            intensity INTEGER DEFAULT 0,
            score REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            needs TEXT DEFAULT '',
            ts TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_emotion_timeline_session
        ON emotion_timeline(session_id, character_id, id);
        CREATE TABLE IF NOT EXISTS relationship_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            stage TEXT DEFAULT '',
            closeness INTEGER DEFAULT 50,
            trust INTEGER DEFAULT 50,
            dependency INTEGER DEFAULT 50,
            preferred_call TEXT DEFAULT '',
            relationship_style TEXT DEFAULT '',
            shared_topics TEXT DEFAULT '',
            recent_relationship_event TEXT DEFAULT '',
            updated_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_session
        ON relationship_state(session_id, character_id);
        CREATE TABLE IF NOT EXISTS open_loops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            category TEXT DEFAULT 'event',
            status TEXT DEFAULT 'pending',
            importance INTEGER DEFAULT 5,
            trigger_time TEXT DEFAULT '',
            last_reminded TEXT DEFAULT '',
            created_time TEXT NOT NULL,
            update_time TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_open_loop_session
        ON open_loops(session_id, character_id);
        CREATE INDEX IF NOT EXISTS idx_open_loop_status
        ON open_loops(status);
        CREATE TABLE IF NOT EXISTS ai_inner_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            current_mood TEXT DEFAULT '',
            user_impression TEXT DEFAULT '',
            recent_focus TEXT DEFAULT '',
            wanted_topics TEXT DEFAULT '',
            interaction_notes TEXT DEFAULT '',
            updated_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_inner_session
        ON ai_inner_state(session_id, character_id);
        CREATE TABLE IF NOT EXISTS conversation_behavior (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            behavior TEXT DEFAULT '',
            response_length TEXT DEFAULT '',
            emotional_tone TEXT DEFAULT '',
            avoid_actions TEXT DEFAULT '',
            updated_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_behavior_session
        ON conversation_behavior(session_id, character_id);
        CREATE TABLE IF NOT EXISTS personality_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            core_personality TEXT DEFAULT '',
            speaking_style TEXT DEFAULT '',
            favorite_phrases TEXT DEFAULT '',
            forbidden_phrases TEXT DEFAULT '',
            emotional_expression TEXT DEFAULT '',
            relationship_behavior TEXT DEFAULT '',
            warmth_delta INTEGER DEFAULT 0,
            dominance_delta INTEGER DEFAULT 0,
            humor_delta INTEGER DEFAULT 0,
            initiative_delta INTEGER DEFAULT 0,
            attachment_delta INTEGER DEFAULT 0,
            -- 角色成长档案（相对基础人设的缓慢偏移，兼容旧库）
            softness INTEGER DEFAULT 0,
            playfulness INTEGER DEFAULT 0,
            vulnerability INTEGER DEFAULT 0,
            growth_interactions INTEGER DEFAULT 0,
            growth_events TEXT DEFAULT '[]',
            last_growth_at TEXT DEFAULT '',
            avoid_style_count INTEGER DEFAULT 0,
            last_negative_reply TEXT DEFAULT '',
            updated_time TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_personality_session
        ON personality_state(session_id, character_id);
        CREATE TABLE IF NOT EXISTS personality_history (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            snapshot     TEXT NOT NULL DEFAULT '{}',   -- JSON快照
            reason       TEXT DEFAULT '',              -- 触发原因（auto/user_cmd/feedback）
            created_time TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ph_session
        ON personality_history(session_id, character_id, created_time DESC);
        CREATE TABLE IF NOT EXISTS relationship_timeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            event_type TEXT DEFAULT '',
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            importance INTEGER DEFAULT 5,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            used_count INTEGER DEFAULT 0,
            -- ★ 事件溯源（2026-09-13）：记录"这件事让内部状态改变了多少"
            --   （JSON：{"intimacy":+3,"trust":-2,"reason":"TA主动分享心事"}）。
            --   对应"缘起"原则：所有状态变化都应能追溯到具体事件，
            --   事件是因、状态是果，而不是散落各处的果找不到因。
            state_delta TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_timeline_session
        ON relationship_timeline(session_id, character_id);
        CREATE TABLE IF NOT EXISTS life_profile (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            profile_type TEXT DEFAULT '',
            content TEXT NOT NULL,
            importance INTEGER DEFAULT 5,
            update_time TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_life_profile_session
        ON life_profile(session_id, character_id);
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS call_records (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT,
            character_id TEXT,
            start_time   REAL,
            end_time     REAL,
            duration_sec INTEGER,
            rounds       INTEGER,
            call_source  TEXT,
            call_result  TEXT
        );
        -- 关系纪念收藏：重要信件、专属语音和惊喜礼物永久保存，按会话+角色隔离。
        CREATE TABLE IF NOT EXISTS relationship_keeps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            keep_type TEXT NOT NULL DEFAULT 'letter',
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            audio_url TEXT DEFAULT '',
            gift_json TEXT DEFAULT '{}',
            source_key TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            permanent INTEGER NOT NULL DEFAULT 1,
            UNIQUE(session_id, character_id, source_key)
        );
        CREATE INDEX IF NOT EXISTS idx_relationship_keeps
        ON relationship_keeps(session_id, character_id, created_at DESC);
        """)
        # 兼容旧库：long_term_memory 可能没有 category 列
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(long_term_memory)").fetchall()]
        if "category" not in cols:
            _conn.execute("ALTER TABLE long_term_memory ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
        # 兼容旧库：long_term_memory 可能没有 memory_type 列
        if "memory_type" not in cols:
            _conn.execute("ALTER TABLE long_term_memory ADD COLUMN memory_type TEXT NOT NULL DEFAULT 'fact'")
        # 兼容旧库：long_term_memory 可能没有 importance 列
        if "importance" not in cols:
            _conn.execute("ALTER TABLE long_term_memory ADD COLUMN importance INTEGER NOT NULL DEFAULT 5")
        # 兼容旧库：long_term_memory 可能没有 last_used 列
        if "last_used" not in cols:
            _conn.execute("ALTER TABLE long_term_memory ADD COLUMN last_used TEXT")
        # 兼容旧库：long_term_memory 新增生命周期管理字段
        memory_cols = [
            r[1]
            for r in _conn.execute(
                "PRAGMA table_info(long_term_memory)"
            ).fetchall()
        ]
        new_memory_fields = {
            "memory_status":
            "TEXT DEFAULT 'active'",
            "confidence":
            "REAL DEFAULT 1.0",
            "access_count":
            "INTEGER DEFAULT 0",
            "decay_score":
            "REAL DEFAULT 1.0",
            "memory_scope":
            # ★ 防记忆串桶：老库加列时存量行默认 character（角色隔离），
            #   不再默认 global——global 会让所有角色共享，旧数据曾因此串桶
            "TEXT DEFAULT 'character'",
            # ★ 记忆信息单元（记忆i存储优化）：情境/情绪/溯源/召回统计
            "context":
            "TEXT DEFAULT ''",
            "emotion_tag":
            "TEXT DEFAULT ''",
            "source_text":
            "TEXT DEFAULT ''",
            "recall_count":
            "INTEGER DEFAULT 0",
            "last_recalled":
            "TEXT",
        }
        for name, field in new_memory_fields.items():
            if name not in memory_cols:
                _conn.execute(
                    f"""
                    ALTER TABLE long_term_memory
                    ADD COLUMN {name} {field}
                    """
                )
        # ★ 事件溯源（2026-09-13）：relationship_timeline 补 state_delta 列（老库迁移）
        _timeline_cols = [
            r[1]
            for r in _conn.execute("PRAGMA table_info(relationship_timeline)").fetchall()
        ]
        if "state_delta" not in _timeline_cols:
            _conn.execute(
                "ALTER TABLE relationship_timeline ADD COLUMN state_delta TEXT DEFAULT ''"
            )
        # ★ 摘要块关联指针（2026-09-13）：老库补 linked_facts / facts_stale 两列。
        #   CREATE TABLE IF NOT EXISTS 对已存在的表不生效，必须 ALTER 兜底。
        try:
            _sb_cols = [
                r[1]
                for r in _conn.execute(
                    "PRAGMA table_info(conversation_summary_block)").fetchall()
            ]
            if _sb_cols:
                if "linked_facts" not in _sb_cols:
                    _conn.execute(
                        "ALTER TABLE conversation_summary_block "
                        "ADD COLUMN linked_facts TEXT DEFAULT ''"
                    )
                if "facts_stale" not in _sb_cols:
                    _conn.execute(
                        "ALTER TABLE conversation_summary_block "
                        "ADD COLUMN facts_stale INTEGER DEFAULT 0"
                    )
                if "stale_note" not in _sb_cols:
                    _conn.execute(
                        "ALTER TABLE conversation_summary_block "
                        "ADD COLUMN stale_note TEXT DEFAULT ''"
                    )
        except Exception:
            pass
        # 兼容旧库：long_term_memory 可能没有 session_id 字段
        if "session_id" not in memory_cols:
            _conn.execute(
                "ALTER TABLE long_term_memory ADD COLUMN session_id TEXT NOT NULL DEFAULT 'default'"
            )
        # 兼容旧库：所有表增加 character_id 字段（多角色隔离）
        _char_id_tables = [
            "chat_history",
            "long_term_memory",
            "user_profile",
            "conversation_summary",
            "emotional_state",
            "relationship_state",
            "open_loops",
            "ai_inner_state",
            "conversation_behavior",
            "personality_state",
            # ★ 以下表建表时已带 character_id，但老库需要 ALTER 兜底
            "personality_history",
            "relationship_timeline",
            "life_profile",
            "call_records",
        ]
        for _tbl in _char_id_tables:
            try:
                _cols = [
                    r[1]
                    for r in _conn.execute(
                        f"PRAGMA table_info({_tbl})"
                    ).fetchall()
                ]
                if "character_id" not in _cols:
                    _conn.execute(
                        f"ALTER TABLE {_tbl} ADD COLUMN character_id TEXT DEFAULT 'default'"
                    )
                # 数据迁移：旧数据 character_id 为 NULL 时设为 'default'
                _conn.execute(
                    f"UPDATE {_tbl} SET character_id='default' WHERE character_id IS NULL"
                )
            except Exception:
                pass
        # 兼容旧库：personality_state 可能没有 delta 字段
        try:
            _personality_cols = [
                r[1]
                for r in _conn.execute(
                    "PRAGMA table_info(personality_state)"
                ).fetchall()
            ]
            _delta_fields = {
                "warmth_delta": "INTEGER DEFAULT 0",
                "dominance_delta": "INTEGER DEFAULT 0",
                "humor_delta": "INTEGER DEFAULT 0",
                "initiative_delta": "INTEGER DEFAULT 0",
                "attachment_delta": "INTEGER DEFAULT 0",
                "user_mirror": "TEXT DEFAULT ''",
                # ★ 风格避免信号（chat_logic 反馈学习写回依赖，缺列会导致永不落库）
                "avoid_style_count": "INTEGER DEFAULT 0",
                "last_negative_reply": "TEXT DEFAULT ''",
                "softness": "INTEGER DEFAULT 0",
                "playfulness": "INTEGER DEFAULT 0",
                "vulnerability": "INTEGER DEFAULT 0",
                "growth_interactions": "INTEGER DEFAULT 0",
                "growth_events": "TEXT DEFAULT '[]'",
                "last_growth_at": "TEXT DEFAULT ''",
            }
            for _name, _field in _delta_fields.items():
                if _name not in _personality_cols:
                    _conn.execute(
                        f"ALTER TABLE personality_state ADD COLUMN {_name} {_field}"
                    )
        except Exception:
            pass
        # user_profile兼容
        tables = [
            r[0]
            for r in _conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        if "user_profile" not in tables:
            _conn.execute(
                """
                CREATE TABLE user_profile (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL DEFAULT 'default',
                    character_id TEXT NOT NULL DEFAULT 'default',
                    nickname TEXT DEFAULT '',
                    personality TEXT DEFAULT '',
                    occupation TEXT DEFAULT '',
                    hobbies TEXT DEFAULT '',
                    dislikes TEXT DEFAULT '',
                    communication_style TEXT DEFAULT '',
                    emotional_traits TEXT DEFAULT '',
                    ext_json TEXT DEFAULT '{}',
                    updated_time TEXT NOT NULL
                )
                """
            )
        # 兼容旧库：chat_history 可能没有 extra 字段（主动推送反馈回溯用）
        try:
            _chat_cols = [
                r[1]
                for r in _conn.execute(
                    "PRAGMA table_info(chat_history)"
                ).fetchall()
            ]
            if "extra" not in _chat_cols:
                _conn.execute(
                    "ALTER TABLE chat_history ADD COLUMN extra TEXT"
                )
        except Exception:
            pass
        # ★ 用户画像：user_profile 补 ext_json 列（幂等，已有列不报错）
        try:
            _up_cols = [
                r[1]
                for r in _conn.execute(
                    "PRAGMA table_info(user_profile)"
                ).fetchall()
            ]
            if "ext_json" not in _up_cols:
                _conn.execute(
                    "ALTER TABLE user_profile ADD COLUMN ext_json TEXT DEFAULT '{}'"
                )
        except Exception:
            pass
        # ★ 定时任务可靠性/隔离迁移：保留旧表数据，幂等补列。
        try:
            _task_cols = [r[1] for r in _conn.execute("PRAGMA table_info(tasks)").fetchall()]
            _task_fields = {
                "session_id": "TEXT NOT NULL DEFAULT 'default'",
                "character_id": "TEXT NOT NULL DEFAULT 'default'",
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT DEFAULT ''",
                "fired_at": "TEXT",
                "source": "TEXT DEFAULT 'chat'",
                "idempotency_key": "TEXT DEFAULT ''",
            }
            for _name, _field in _task_fields.items():
                if _name not in _task_cols:
                    _conn.execute(f"ALTER TABLE tasks ADD COLUMN {_name} {_field}")
            _conn.execute("UPDATE tasks SET session_id='default' WHERE session_id IS NULL OR session_id=''")
            _conn.execute("UPDATE tasks SET character_id='default' WHERE character_id IS NULL OR character_id=''")
            # 上次进程若在执行中被关闭，启动时允许重新领取，不永久丢任务。
            _conn.execute("UPDATE tasks SET status='pending' WHERE status='processing'")
            _conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(session_id, character_id, status, trigger_time)")
        except Exception as _te:
            print(f"[DB] tasks 迁移失败(不影响启动): {_te}", flush=True)
        # ★ 用户画像：唯一索引 (session_id, character_id)，防多角色串写/重复行（幂等）
        try:
            _conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile_unique "
                "ON user_profile(session_id, character_id)"
            )
        except Exception:
            pass
        # 清理历史里已经落库的内部伪 user 消息，避免它们继续污染上下文。
        try:
            _dirty = _conn.execute(
                "SELECT id, content FROM chat_history WHERE role='user' AND ("
                "content LIKE '%空闲时间到了%' OR "
                "content LIKE '%定时时间到了%' OR "
                "content LIKE '%当前时间:%' OR "
                "content LIKE '近期心情%' OR "
                "content LIKE '关于用户%' OR "
                "content LIKE '用户偏好%' OR "
                "content LIKE '近期事件%'"
                ")"
            ).fetchall()
            _dirty_ids = [int(r["id"]) for r in _dirty if _is_internal_prompt_text(r["content"])]
            if _dirty_ids:
                _conn.executemany(
                    "DELETE FROM chat_history WHERE id=?",
                    [(x,) for x in _dirty_ids],
                )
                print(f"[DB] 已清理内部伪 user 消息 {len(_dirty_ids)} 条", flush=True)
        except Exception as _e:
            print(f"[DB] 内部伪消息清理失败(不影响启动): {_e}", flush=True)
        # ★ 防记忆串桶迁移：存量 global 记忆按收紧后的分类器重判一次
        #   （老库 ALTER 加 memory_scope 列时默认 'global'，导致私人偏好被所有角色共享）
        try:
            _migrate_legacy_global_scope(_conn)
        except Exception as _e:
            print(f"[DB] global 记忆重分类迁移失败(不影响启动): {_e}", flush=True)
        _conn.commit()


def _migrate_legacy_global_scope(conn):
    """
    一次性迁移（幂等，kv 标记防重跑）：
    旧库升级时 ALTER TABLE 加 memory_scope 列，存量行全部默认 'global'，
    造成"角色A的私人偏好被角色B读到"的记忆串桶。

    处理：对每条 scope='global' 的有效记忆，用收紧后的
    scope_classifier.classify_scope 重新判定：
      - 仍判 global（真·用户中性基础信息）→ 不动
      - 判为 character / relationship → 改写 scope；character_id 指派：
          该 session 下恰好只有一个非 default 活跃角色 → 指派给它
          否则 → 保持原值（通常为 'default'，即仅 default 会话可见）
    判定依据来源：记忆内容本身 + 该 session 的聊天历史，不依赖外部状态。
    """
    try:
        row = conn.execute(
            "SELECT value FROM kv WHERE key='memory_scope_reclassified_v1'"
        ).fetchone()
        if row:
            return
    except Exception:
        pass   # kv 表异常则继续尝试（最多少一次防重跑）

    try:
        from .memory.scope_classifier import classify_scope
    except Exception:
        return   # 分类器不可用时跳过（新库本来也没存量数据）

    rows = conn.execute(
        "SELECT id, session_id, character_id, memory_content, memory_type "
        "FROM long_term_memory WHERE memory_scope='global' AND is_valid=1"
    ).fetchall()
    if not rows:
        _kv_set_raw(conn, "memory_scope_reclassified_v1", "empty")
        return

    # 每个 session 的非 default 活跃角色表（用于 character_id 指派）
    _session_chars = {}
    moved = 0
    for r in rows:
        sid = r["session_id"]
        if sid not in _session_chars:
            try:
                chars = [
                    x[0] for x in conn.execute(
                        "SELECT DISTINCT character_id FROM chat_history "
                        "WHERE session_id=? AND character_id NOT IN ('', 'default')",
                        (sid,)
                    ).fetchall()
                ]
            except Exception:
                chars = []
            _session_chars[sid] = chars

        new_scope = classify_scope(r["memory_type"], r["memory_content"], r["character_id"])
        if new_scope == "global":
            continue

        # character_id 指派：唯一活跃角色 → 它；多个/没有 → 保持原值
        chars = _session_chars[sid]
        new_cid = r["character_id"]
        if len(chars) == 1:
            new_cid = chars[0]

        conn.execute(
            "UPDATE long_term_memory SET memory_scope=?, character_id=? WHERE id=?",
            (new_scope, new_cid, r["id"])
        )
        moved += 1

    _kv_set_raw(conn, "memory_scope_reclassified_v1", str(moved))
    if moved:
        print(
            f"[DB] 记忆防串桶迁移：{moved}/{len(rows)} 条 global 记忆重判为角色/关系隔离",
            flush=True
        )


def _kv_set_raw(conn, key, value):
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)",
            (key, str(value))
        )
    except Exception:
        pass


# ★ 事务嵌套深度：>0 时 q() 不再自动提交，交给最外层 transaction() 统一提交/回滚
_tx_depth = 0


def q(sql, args=(), fetch=False):
    with _lock:
        init()
        cur = _conn.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        if _tx_depth == 0:
            _conn.commit()   # 不在事务里才自动提交（单次调用行为与原先一致）
        if fetch:
            return rows   # fetch 模式：返回 rows，兼容所有现有调用方
        return cur        # 写入模式：返回 cursor，让 insert_memory 拿 lastrowid


@contextlib.contextmanager
def transaction():
    """把一批写操作包成一个事务：全部成功才提交，任何异常整体回滚。

    q() 默认每次都自动提交，多步写入（例如「作废旧记忆 + 写入新记忆」）
    中途失败就会留下半截数据 —— 旧数据没了、新数据也没进来。
    需要原子性的地方用 `with db.transaction():` 包起来即可，
    里面照常调用 q() 或基于 q() 的其它 db 函数。

    可重入：_lock 是 RLock，加上深度计数，嵌套时只有最外层真正提交/回滚。
    """
    global _tx_depth
    with _lock:
        init()
        _tx_depth += 1
        try:
            yield
        except Exception:
            if _tx_depth > 0:
                _tx_depth -= 1
            if _tx_depth == 0:
                try:
                    _conn.rollback()
                except Exception:
                    pass
            raise
        if _tx_depth > 0:
            _tx_depth -= 1
        if _tx_depth == 0:
            _conn.commit()


# ---------------- chat_history ----------------

def add_message(session_id: str, role: str, content: str, character_id: str = "default", extra: dict = None):
    import json as _json
    if is_internal_chat_message(role, content):
        return -1
    _extra_json = None
    if extra:
        try:
            _extra_json = _json.dumps(extra, ensure_ascii=False)
        except Exception:
            _extra_json = None
    cur = q("INSERT INTO chat_history(session_id, character_id, role, content, timestamp, extra) VALUES(?,?,?,?,?,?)",
            (session_id, character_id, role, content, _now(), _extra_json))
    # ★ 2026-09-15（主动消息单一引擎）：**这里是全项目唯一的消息落库口** ——
    #   用户消息 / AI 回复 / 主动消息、App 与 QQ 两条链路全都经过它。
    #   把引擎的节奏状态机挂在这里，就不需要在几十个调用点各写一次：
    #     用户说话 → 打断并重置；AI 说话 → 起静默窗口；主动消息 → 重启间隔计时。
    #   任何异常都吞掉：节奏记账绝不能影响消息本身落库。
    try:
        from . import proactive_engine as _eng
        _eng.on_message(session_id, character_id, role, extra)
    except Exception:
        pass
    return cur.lastrowid


def recent_messages(session_id: str, limit: int = 24, character_id: str = "default"):
    import json as _json
    try:
        _limit = max(1, min(int(limit), 500))
    except Exception:
        _limit = 24
    _fetch_limit = min(1000, max(_limit * 4, _limit + 12))
    rows = q("SELECT id, role, content, timestamp, extra FROM chat_history WHERE session_id=? AND character_id=? ORDER BY id DESC LIMIT ?",
             (session_id, character_id, _fetch_limit), fetch=True)
    rows = _filter_chat_rows(rows)[:_limit]
    out = []
    for r in reversed(rows):
        _extra = r["extra"]
        if _extra:
            try:
                _extra = _json.loads(_extra)
            except Exception:
                _extra = {}
        out.append({
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "timestamp": r["timestamp"],
            "extra": _extra,
        })
    return out


def update_message_extra(message_id, patch: dict):
    """给消息的 extra JSON 打补丁（读旧值合并后写回）。message_id 可为 int 或 str。"""
    import json as _json
    try:
        _mid = int(message_id)
    except (TypeError, ValueError):
        return
    rows = q("SELECT extra FROM chat_history WHERE id=?", (_mid,), fetch=True)
    if not rows:
        return
    _old = rows[0]["extra"] or "{}"
    try:
        _cur = _json.loads(_old)
        if not isinstance(_cur, dict):
            _cur = {}
    except Exception:
        _cur = {}
    _cur.update(patch or {})
    q("UPDATE chat_history SET extra=? WHERE id=?",
      (_json.dumps(_cur, ensure_ascii=False), _mid))


def last_user_time(session_id: str, character_id: str = None):
    """最后一条用户消息时间。传 character_id 时按角色隔离（多角色同 session 各算各的）。"""
    if character_id:
        rows = q(
            "SELECT role, content, timestamp FROM chat_history WHERE session_id=? AND character_id=? AND role='user' ORDER BY id DESC LIMIT 20",
            (session_id, character_id), fetch=True)
    else:
        rows = q("SELECT role, content, timestamp FROM chat_history WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 20",
                 (session_id,), fetch=True)
    rows = _filter_chat_rows(rows)
    if not rows:
        return None
    try:
        return datetime.strptime(rows[0]["timestamp"], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def last_chat_time(session_id: str, character_id: str = None):
    """最后一条消息时间（任意角色，含 AI 回复/主动消息）。

    ★ 对话间隔的基准：只按 user 消息算会明显失真——最后一条是 AI 主动消息、
      用户隔了几小时才回时，间隔被算到更早的用户消息上。
    """
    if character_id:
        rows = q(
            "SELECT role, content, timestamp FROM chat_history WHERE session_id=? AND character_id=? ORDER BY id DESC LIMIT 20",
            (session_id, character_id), fetch=True)
    else:
        rows = q(
            "SELECT role, content, timestamp FROM chat_history WHERE session_id=? ORDER BY id DESC LIMIT 20",
            (session_id,), fetch=True)
    rows = _filter_chat_rows(rows)
    if not rows:
        return None
    try:
        return datetime.strptime(rows[0]["timestamp"], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def user_night_ratio(session_id: str, character_id: str = None, days: int = 14):
    """最近 N 天用户消息中深夜+凌晨（23:00~05:59）的占比。

    用途：识别「夜猫子」作息——占比高的用户聊通宵是常态，
    时间系统据此避免"跨夜就默认 TA 睡了"的硬假设。

    Returns: float 占比；样本不足（<20 条）返回 None（调用方不标注作息）。
    """
    cutoff = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
    if character_id:
        row = q(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN CAST(substr(timestamp,12,2) AS INTEGER) >= 23
                                       OR CAST(substr(timestamp,12,2) AS INTEGER) < 5
                                     THEN 1 ELSE 0 END), 0) AS night
            FROM chat_history
            WHERE session_id=? AND character_id=? AND role='user' AND timestamp>=?
            """,
            (session_id, character_id, cutoff), fetch=True)
    else:
        row = q(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN CAST(substr(timestamp,12,2) AS INTEGER) >= 23
                                       OR CAST(substr(timestamp,12,2) AS INTEGER) < 5
                                     THEN 1 ELSE 0 END), 0) AS night
            FROM chat_history
            WHERE session_id=? AND role='user' AND timestamp>=?
            """,
            (session_id, cutoff), fetch=True)
    try:
        total = int(row[0]["total"] or 0) if row else 0
        if total < 20:
            return None
        night = int(row[0]["night"] or 0)
        return night / total
    except Exception:
        return None


def last_activity_time(session_id: str):
    """最后活跃（用户消息或心跳上报）"""
    ts = kv_get("last_activity:" + session_id)
    if ts:
        try:
            return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return last_user_time(session_id)


def current_chat_streak(session_id: str, character_id: str = "default") -> int:
    """按真实用户发言日期计算截至最近一次聊天的连续天数。"""
    from datetime import date as _date, timedelta as _timedelta
    rows = q(
        """SELECT timestamp, role, content
           FROM chat_history
           WHERE session_id=? AND character_id=? AND role='user'
           ORDER BY id DESC LIMIT 1600""",
        (session_id, character_id), fetch=True,
    )
    days = []
    seen = set()
    for row in rows:
        if is_internal_chat_message(row["role"], row["content"]):
            continue
        try:
            d = _date.fromisoformat(str(row["timestamp"])[:10])
        except Exception:
            continue
        if d not in seen:
            seen.add(d)
            days.append(d)
        if len(days) >= 1200:
            break
    if not days:
        return 0
    streak = 1
    expected = days[0] - _timedelta(days=1)
    for value in days[1:]:
        if value != expected:
            break
        streak += 1
        expected -= _timedelta(days=1)
    return streak


def touch_activity(session_id: str):
    kv_set("last_activity:" + session_id, _now())


# ---------------- conversation_summary ----------------

def get_conversation_summary(session_id: str = "default", character_id: str = "default") -> dict:
    rows = q(
        """
        SELECT
            id,
            session_id,
            character_id,
            summary,
            covered_until_id,
            created_time,
            update_time
        FROM conversation_summary
        WHERE session_id=? AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    return dict(rows[0]) if rows else {}


def save_conversation_summary(
    session_id: str,
    summary: str,
    covered_until_id: int = 0,
    character_id: str = "default"
):
    """按 (session_id, character_id) 保存/更新摘要，多角色摘要互不覆盖。"""
    old = get_conversation_summary(session_id, character_id)

    if old:
        q(
            """
            UPDATE conversation_summary
            SET summary=?,
                covered_until_id=?,
                update_time=?
            WHERE session_id=? AND character_id=?
            """,
            (
                summary,
                int(covered_until_id or 0),
                _now(),
                session_id,
                character_id
            )
        )
    else:
        q(
            """
            INSERT INTO conversation_summary
            (
                session_id,
                character_id,
                summary,
                covered_until_id,
                created_time
            )
            VALUES(?,?,?,?,?)
            """,
            (
                session_id,
                character_id,
                summary,
                int(covered_until_id or 0),
                _now()
            )
        )


# ---------------- conversation_summary_block（只追加的摘要块）----------------

def add_summary_block(session_id: str, character_id: str, from_msg_id: int,
                      to_msg_id: int, summary: str) -> bool:
    """追加一个摘要块。产出后不再修改（同一 to_msg_id 重复写入被 UNIQUE 忽略）。

    返回 True 表示本次真的插入了新块（重复调用返回 False）。
    """
    text = str(summary or "").strip()
    if not text:
        return False
    try:
        cur = q(
            """
            INSERT OR IGNORE INTO conversation_summary_block
                (session_id, character_id, from_msg_id, to_msg_id, summary, created_time)
            VALUES(?,?,?,?,?,?)
            """,
            (session_id, character_id, int(from_msg_id or 0),
             int(to_msg_id or 0), text, _now())
        )
        # INSERT OR IGNORE 撞上 UNIQUE 时不报错、rowcount 为 0 ——
        # 必须靠 rowcount 区分「真的插入了」和「已经有了」，否则重复调用会被误判成成功。
        return bool(getattr(cur, "rowcount", 0))
    except Exception:
        return False


def get_summary_blocks(session_id: str, character_id: str = "default",
                       limit: int = 3) -> list:
    """取最近 limit 个摘要块（时间从早到晚返回，方便顺序拼接）。"""
    try:
        rows = q(
            """
            SELECT id, from_msg_id, to_msg_id, summary, created_time,
                   linked_facts, facts_stale, stale_note
            FROM conversation_summary_block
            WHERE session_id=? AND character_id=?
            ORDER BY to_msg_id DESC
            LIMIT ?
            """,
            (session_id, character_id, int(limit)),
            fetch=True
        )
        return [dict(r) for r in reversed(list(rows or []))]
    except Exception:
        return []


def seed_summary_block_from_legacy(session_id: str, character_id: str = "default") -> bool:
    """把「合并重写时代」留下的单行摘要迁移成第 0 号摘要块（只做一次）。

    ★ 为什么必须做：旧单行摘要覆盖的是 0..covered_until_id 这一整段。
      如果直接切到"只看块"，而第一个新块只覆盖 covered_until_id 之后的内容，
      那 covered_until_id 之前的全部历史就会从注入里凭空消失（静默的信息倒退）。
      迁移成 0..covered_until_id 的块之后，新旧覆盖连续，注入内容只多不少。
    幂等：已经有过块、或没有旧摘要时直接返回 False。
    """
    try:
        if count_summary_blocks(session_id, character_id) > 0:
            return False
        item = get_conversation_summary(session_id, character_id) or {}
        text = str(item.get("summary") or "").strip()
        covered = int(item.get("covered_until_id") or 0)
        if not text or covered <= 0:
            return False
        return add_summary_block(session_id, character_id, 0, covered, text)
    except Exception:
        return False


def count_summary_blocks(session_id: str, character_id: str = "default") -> int:
    try:
        rows = q(
            "SELECT COUNT(*) FROM conversation_summary_block "
            "WHERE session_id=? AND character_id=?",
            (session_id, character_id),
            fetch=True
        )
        return int(rows[0][0]) if rows else 0
    except Exception:
        return 0


# ---------------- 摘要块 ↔ 事实关联（2026-09-13）----------------

def link_summary_block_facts(block_id: int, fact_ids) -> bool:
    """给摘要块登记它依赖的事实 id（逗号分隔存 linked_facts）。

    为什么需要人工/工具确认、而不是自动抽取：实测 LLM 批量判定**不可复现**
    （同输入两次运行给出不同结论），用自动结果直接建立"失效即降级"的联动，
    一旦判错就会把正常的摘要块标注成过时。所以本函数只负责"记下已确认的关联"。
    """
    try:
        ids = [int(x) for x in (fact_ids or []) if str(x).strip().isdigit()]
        q("UPDATE conversation_summary_block SET linked_facts=? WHERE id=?",
          (",".join(str(i) for i in ids), int(block_id)))
        return True
    except Exception:
        return False


def get_stale_summary_blocks(session_id: str = None, character_id: str = "default") -> list:
    """列出"关联事实已失效"的摘要块（facts_stale=1）。"""
    try:
        if session_id:
            rows = q(
                "SELECT id, session_id, character_id, from_msg_id, to_msg_id, stale_note "
                "FROM conversation_summary_block "
                "WHERE facts_stale=1 AND session_id=? AND character_id=? "
                "ORDER BY to_msg_id",
                (session_id, character_id), fetch=True)
        else:
            rows = q(
                "SELECT id, session_id, character_id, from_msg_id, to_msg_id, stale_note "
                "FROM conversation_summary_block WHERE facts_stale=1 ORDER BY id",
                fetch=True)
        return [dict(r) for r in (rows or [])]
    except Exception:
        return []


def refresh_summary_block_staleness(session_id: str = None,
                                    character_id: str = "default") -> dict:
    """重算所有已登记关联的摘要块的 stale 状态。

    判定：块里登记的任一 fact id 现在 `is_valid=0` → 该块标记 facts_stale=1，
    并把失效事实的内容摘录进 stale_note（注入时用于降级说明）。

    只扫"已登记关联"的块（linked_facts 非空），不猜、不自动关联 ——
    所以它对未登记关联的块不做任何改动，行为保守。
    """
    out = {"checked": 0, "stale": 0, "recovered": 0}
    try:
        if session_id:
            rows = q(
                "SELECT id, linked_facts FROM conversation_summary_block "
                "WHERE linked_facts IS NOT NULL AND linked_facts != '' "
                "AND session_id=? AND character_id=?",
                (session_id, character_id), fetch=True) or []
        else:
            rows = q(
                "SELECT id, linked_facts FROM conversation_summary_block "
                "WHERE linked_facts IS NOT NULL AND linked_facts != ''",
                fetch=True) or []

        for r in rows:
            bid = int(r[0])
            ids = [int(x) for x in str(r[1] or "").split(",") if x.strip().isdigit()]
            if not ids:
                continue
            out["checked"] += 1
            dead = []
            for fid in ids:
                mem = get_memory(fid)
                if mem is None:
                    continue
                if int(mem.get("is_valid", 1) or 0) == 0:
                    dead.append(str(mem.get("memory_content") or "")[:60])
            if dead:
                note = "以下关联事实已被取代/失效：" + "；".join(dead[:3])
                q("UPDATE conversation_summary_block SET facts_stale=1, stale_note=? WHERE id=?",
                  (note, bid))
                out["stale"] += 1
            else:
                # 事实恢复了（例如用户撤销纠正）→ 取消降级
                cur = q("SELECT facts_stale FROM conversation_summary_block WHERE id=?",
                        (bid,), fetch=True)
                if cur and int(cur[0][0] or 0) == 1:
                    q("UPDATE conversation_summary_block SET facts_stale=0, stale_note='' WHERE id=?",
                      (bid,))
                    out["recovered"] += 1
    except Exception as e:
        out["error"] = str(e)
    return out


def messages_after_id(
    session_id: str,
    after_id: int = 0,
    limit: int = 100,
    character_id: str = "default"
):
    rows = q(
        """
        SELECT id, role, content, timestamp
        FROM chat_history
        WHERE session_id=?
          AND character_id=?
          AND id>?
        ORDER BY id ASC
        LIMIT ?
        """,
        (
            session_id,
            character_id,
            int(after_id or 0),
            int(limit)
        ),
        fetch=True
    )
    return [dict(r) for r in rows if not is_internal_chat_message(r["role"], r["content"])]


def save_daily_memory_report(session_id: str, character_id: str, report_date: str, content: str):
    """按用户、角色和日期幂等保存每日晚报。"""
    q(
        """
        INSERT INTO daily_memory_report
            (session_id, character_id, report_date, content, created_time)
        VALUES(?,?,?,?,?)
        ON CONFLICT(session_id, character_id, report_date)
        DO UPDATE SET content=excluded.content, update_time=excluded.created_time
        """,
        (session_id, character_id, report_date, content, _now())
    )


def list_daily_memory_reports(session_id: str, character_id: str = "", limit: int = 180):
    if character_id:
        rows = q(
            """SELECT * FROM daily_memory_report
               WHERE session_id=? AND character_id=?
               ORDER BY report_date DESC LIMIT ?""",
            (session_id, character_id, int(limit)), fetch=True
        )
    else:
        rows = q(
            """SELECT * FROM daily_memory_report
               WHERE session_id=? ORDER BY report_date DESC, id DESC LIMIT ?""",
            (session_id, int(limit)), fetch=True
        )
    return [dict(r) for r in rows]


# ---------------- relationship_keeps ----------------

def save_relationship_keep(
    session_id: str,
    character_id: str,
    keep_type: str,
    title: str,
    content: str,
    audio_url: str = "",
    gift: dict = None,
    source_key: str = "",
):
    """永久保存一份关系纪念物；同一来源幂等，后续语音生成成功可补写 audio_url。"""
    import json as _json
    gift_json = _json.dumps(gift or {}, ensure_ascii=False)
    source_key = str(source_key or f"{keep_type}:{title}:{content[:40]}")
    q(
        """
        INSERT INTO relationship_keeps
          (session_id, character_id, keep_type, title, content, audio_url,
           gift_json, source_key, created_at, permanent)
        VALUES(?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(session_id, character_id, source_key) DO UPDATE SET
          keep_type=excluded.keep_type,
          title=excluded.title,
          content=excluded.content,
          audio_url=CASE WHEN excluded.audio_url<>'' THEN excluded.audio_url
                         ELSE relationship_keeps.audio_url END,
          gift_json=CASE WHEN excluded.gift_json<>'{}' THEN excluded.gift_json
                         ELSE relationship_keeps.gift_json END
        """,
        (
            session_id or "default", character_id or "default", keep_type or "letter",
            title or "", content or "", audio_url or "", gift_json, source_key,
            _now(),
        ),
    )


def list_relationship_keeps(session_id: str, character_id: str = "default", limit: int = 100):
    import json as _json
    rows = q(
        """
        SELECT id, session_id, character_id, keep_type, title, content,
               audio_url, gift_json, source_key, created_at, permanent
        FROM relationship_keeps
        WHERE session_id=? AND character_id=?
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (session_id or "default", character_id or "default", max(1, min(int(limit or 100), 500))),
        fetch=True,
    )
    out = []
    for row in rows:
        item = dict(row)
        try:
            item["gift"] = _json.loads(item.pop("gift_json") or "{}")
        except Exception:
            item["gift"] = {}
            item.pop("gift_json", None)
        out.append(item)
    return out


def get_relationship_keep(keep_id: int, session_id: str, character_id: str):
    rows = q(
        "SELECT * FROM relationship_keeps WHERE id=? AND session_id=? AND character_id=? LIMIT 1",
        (int(keep_id), session_id or "default", character_id or "default"), fetch=True,
    )
    return dict(rows[0]) if rows else {}


def relationship_keep_exists(session_id: str, character_id: str, source_key: str) -> bool:
    rows = q(
        "SELECT 1 FROM relationship_keeps WHERE session_id=? AND character_id=? AND source_key=? LIMIT 1",
        (session_id or "default", character_id or "default", source_key), fetch=True,
    )
    return bool(rows)


def update_relationship_keep_audio(keep_id: int, audio_url: str):
    q("UPDATE relationship_keeps SET audio_url=? WHERE id=?", (audio_url or "", int(keep_id)))


def delete_relationship_keep(session_id: str, character_id: str, source_key: str):
    q(
        "DELETE FROM relationship_keeps WHERE session_id=? AND character_id=? AND source_key=?",
        (session_id or "default", character_id or "default", source_key or ""),
    )


def latest_message_id(session_id: str, character_id: str = "default") -> int:
    rows = q(
        """
        SELECT id
        FROM chat_history
        WHERE session_id=? AND character_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    return int(rows[0]["id"]) if rows else 0


# ---------------- emotional_state ----------------

def get_emotion_state(
    session_id="default",
    character_id="default"
):
    rows = q(
        """
        SELECT *
        FROM emotional_state
        WHERE session_id=? AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    if rows:
        return dict(rows[0])
    return {}


def update_emotion_state(
    session_id="default",
    character_id="default",
    **kwargs
):
    """原子 UPSERT：INSERT ... ON CONFLICT DO UPDATE（只覆盖显式传入的字段，
    未传字段保留旧值；无旧行时用默认值插入）。修复多角色串写。"""
    _fields = [
        "mood", "intensity", "reason", "needs",
        "positive_keywords", "negative_keywords",
    ]
    _defaults = {
        "mood": "", "intensity": 0, "reason": "",
        "needs": "", "positive_keywords": "", "negative_keywords": "",
    }
    _provided = [f for f in _fields if f in kwargs]
    _vals = {
        f: (int(kwargs.get(f, 0) or 0) if f == "intensity" else kwargs.get(f, _defaults[f]))
        for f in _fields
    }

    _cols = ["session_id", "character_id"] + _fields + ["updated_time"]
    _marks = ",".join(["?"] * len(_cols))
    _params = [session_id, character_id] + [_vals.get(f, _defaults[f]) for f in _fields] + [_now()]

    if _provided:
        _set = ",".join(f"{f}=excluded.{f}" for f in _provided) + ",updated_time=excluded.updated_time"
        _sql = (
            f"INSERT INTO emotional_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET {_set}"
        )
    else:
        # 无显式字段：仅保证存在一行并刷新 updated_time
        _sql = (
            f"INSERT INTO emotional_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET updated_time=excluded.updated_time"
        )
    q(_sql, _params)


# ---------------- relationship_state ----------------

def get_relationship_state(session_id="default", character_id="default"):
    """读取关系状态（按 session_id + character_id 双键隔离，多角色各算各的）"""
    rows = q(
        """
        SELECT *
        FROM relationship_state
        WHERE session_id=? AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    state = dict(rows[0]) if rows else {}
    # 兼容新关系表已有数据、旧关系表仍是空/默认的情况。很多上下文模块还读
    # 旧表字段 closeness；这里运行时合并新表，让既有用户无需重新拖滑块。
    try:
        from .relationship.manager import RelationshipManager
        rel = RelationshipManager().get_state(session_id, character_id, create=False) or {}
        if rel:
            intimacy = max(0, min(100, int(float(rel.get("intimacy", 0) or 0))))
            trust = max(0, min(100, int(float(rel.get("trust", 0) or 0))))
            affection = max(0, min(100, int(float(rel.get("affection", 50) or 50))))
            if not state:
                state = {
                    "session_id": session_id,
                    "character_id": character_id,
                    "stage": rel.get("stage", ""),
                    "closeness": intimacy,
                    "trust": trust,
                    "dependency": affection,
                    "preferred_call": "",
                    "relationship_style": "",
                    "shared_topics": "",
                    "recent_relationship_event": "",
                }
            else:
                state["stage"] = rel.get("stage") or state.get("stage", "")
                state["closeness"] = intimacy
                state["trust"] = trust
                state["dependency"] = affection
            state["intimacy"] = intimacy
            state["affection"] = affection
    except Exception:
        pass
    return state


def update_relationship_state(
    session_id="default",
    character_id="default",
    **kwargs
):
    """原子 UPSERT：INSERT ... ON CONFLICT DO UPDATE（只覆盖显式传入字段）。"""
    _skip_new_bridge = bool(kwargs.pop("_skip_new_bridge", False))
    _fields = [
        "stage", "closeness", "trust", "dependency",
        "preferred_call", "relationship_style",
        "shared_topics", "recent_relationship_event",
    ]
    _defaults = {
        "stage": "", "closeness": 50, "trust": 50, "dependency": 50,
        "preferred_call": "", "relationship_style": "",
        "shared_topics": "", "recent_relationship_event": "",
    }
    _provided = [f for f in _fields if f in kwargs]
    _vals = {
        f: (int(kwargs.get(f, 0) or 0) if f in ("closeness", "trust", "dependency") else kwargs.get(f, _defaults[f]))
        for f in _fields
    }

    _cols = ["session_id", "character_id"] + _fields + ["updated_time"]
    _marks = ",".join(["?"] * len(_cols))
    _params = [session_id, character_id] + [_vals.get(f, _defaults[f]) for f in _fields] + [_now()]

    if _provided:
        _set = ",".join(f"{f}=excluded.{f}" for f in _provided) + ",updated_time=excluded.updated_time"
        _sql = (
            f"INSERT INTO relationship_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET {_set}"
        )
    else:
        _sql = (
            f"INSERT INTO relationship_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET updated_time=excluded.updated_time"
        )
    q(_sql, _params)

    # 桥接旧关系表 → 新关系表。项目里仍有部分真人感/主动策略模块读取
    # db.relationship_state(closeness)，而联系人/成长页读取 relationship.manager
    # 的 relationship.db(intimacy/affection/trust)。保持两边同步，避免
    # 前端显示 90，但提示词仍按默认 50 行事。
    try:
        if _skip_new_bridge:
            return
        bridge = {}
        if "closeness" in kwargs:
            bridge["intimacy"] = max(0, min(100, int(float(kwargs.get("closeness") or 0))))
        if "trust" in kwargs:
            bridge["trust"] = max(0, min(100, int(float(kwargs.get("trust") or 0))))
        if bridge:
            from .relationship.manager import RelationshipManager
            mgr = RelationshipManager()
            mgr.create_user(session_id, character_id)
            if "intimacy" in bridge:
                try:
                    from .relationship.evolution import calculate_stage
                    bridge["stage"] = calculate_stage(bridge["intimacy"])
                except Exception:
                    pass
            bridge["_skip_legacy_bridge"] = True
            mgr.update(session_id, character_id, **bridge)
            if "intimacy" in bridge:
                kv_set(f"intimacy:{session_id}:{character_id}", bridge["intimacy"])
    except Exception as _rel_bridge_e:
        print(f"[DB] 关系桥接到新表失败(静默): {_rel_bridge_e}", flush=True)


# ---------------- open loops ----------------

def add_open_loop(
    session_id,
    title,
    description="",
    category="event",
    importance=5,
    trigger_time="",
    character_id="default",
    status="pending"
):
    """新增未了结事项。

    ★ 2026-09-16：新增 status 参数（默认 pending，行为与原来完全一致）。
      定时提醒兜底路径要把「委托里没听清事项」的句子记成**待确认**而不是排程：
      status="awaiting_confirm" 的行不会被 get_open_loops（只取 pending）
      捞进 prompt、也不会被承诺兑现链路（_fetch_promises 只取 pending）当成到点任务。
    """
    q(
        """
        INSERT INTO open_loops
        (
            session_id,
            character_id,
            title,
            description,
            category,
            status,
            importance,
            trigger_time,
            created_time
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            session_id,
            character_id,
            title,
            description,
            category,
            str(status or "pending"),
            importance,
            trigger_time,
            _now()
        )
    )


def get_open_loops(
    session_id="default",
    character_id="default",
    limit=10,
    order="importance"
):
    """取未了结事项。

    order="importance"（默认，保持原有行为）：按重要度取，用于"提醒她别忘重要的事"。
    order="recent"：按最近记录取 —— ★ 2026-09-11 新增，专给抽取器做**去重认领**用。
      为什么必须分开：抽取器只看最近 20 条消息，所以它可能重复的必然是**最近刚记的**
      条目；而 importance 排序会被一堆 9 分的旧条目（生日/提醒/戒指）占满，
      近期 importance 3~6 的条目根本进不了清单 ——
      实测「猫取名」那几条就是这样被挡在外面，导致模型看不到、只能又新增一条。
    """
    _order_sql = "id DESC" if str(order) == "recent" else "importance DESC, id DESC"
    rows = q(
        f"""
        SELECT *
        FROM open_loops
        WHERE session_id=?
        AND character_id=?
        AND status='pending'
        ORDER BY {_order_sql}
        LIMIT ?
        """,
        (
            session_id,
            character_id,
            limit
        ),
        fetch=True
    )
    return [
        dict(r)
        for r in rows
    ]


def finish_open_loop(
    loop_id
):
    q(
        """
        UPDATE open_loops
        SET status='done',
        update_time=?
        WHERE id=?
        """,
        (
            _now(),
            loop_id
        )
    )


def update_open_loop(
    loop_id,
    title=None,
    description=None,
    category=None,
    importance=None,
    trigger_time=None,
    session_id=None,
    character_id=None,
):
    """就地更新一条未完成事项（★ 2026-09-11）。

    为什么需要它：`extract_open_loops` 原来**只 INSERT 不 UPDATE**，每轮从最近 20 条
    重新抽取 → 同一件事被反复抽出、互相矛盾、越积越多（实测骨子 513 条 pending，
    「猫取名」一件事就有 5 条：尚未取名 / 已取名为汤圆 / 需为白猫取名 … 全挂 pending）。
    现在抽取器会带上既有条目让模型认领，命中的走这里更新，而不是再插一条。

    session_id / character_id 传了就一起校验（防止模型给了别的会话的 id 被误改）。
    返回受影响行数。
    """
    sets, params = [], []
    for col, val in (("title", title), ("description", description),
                     ("category", category), ("importance", importance),
                     ("trigger_time", trigger_time)):
        if val is None:
            continue
        sets.append(f"{col}=?")
        params.append(int(val) if col == "importance" else str(val))
    if not sets:
        return 0
    sets.append("update_time=?")
    params.append(_now())

    where = "id=?"
    params.append(int(loop_id))
    if session_id is not None:
        where += " AND session_id=?"
        params.append(session_id)
    if character_id is not None:
        where += " AND character_id=?"
        params.append(character_id)

    try:
        # ★ 走 q()：它负责惰性 init()、自动提交与事务深度，别绕过去直连 _conn
        cur = q(f"UPDATE open_loops SET {','.join(sets)} WHERE {where}", tuple(params))
        return cur.rowcount or 0
    except Exception as e:
        print(f"[DB] update_open_loop 失败(静默): {e}", flush=True)
        return 0


# ---------------- ai_inner_state ----------------

def get_ai_inner_state(
    session_id="default",
    character_id="default"
):
    rows = q(
        """
        SELECT *
        FROM ai_inner_state
        WHERE session_id=? AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    return dict(rows[0]) if rows else {}


def update_ai_inner_state(
    session_id="default",
    character_id="default",
    **kwargs
):
    """原子 UPSERT：INSERT ... ON CONFLICT DO UPDATE（只覆盖显式传入字段）。"""
    _fields = [
        "current_mood", "user_impression", "recent_focus",
        "wanted_topics", "interaction_notes",
    ]
    _defaults = {f: "" for f in _fields}
    _provided = [f for f in _fields if f in kwargs]

    _cols = ["session_id", "character_id"] + _fields + ["updated_time"]
    _marks = ",".join(["?"] * len(_cols))
    _params = [session_id, character_id] + [kwargs.get(f, _defaults[f]) for f in _fields] + [_now()]

    if _provided:
        _set = ",".join(f"{f}=excluded.{f}" for f in _provided) + ",updated_time=excluded.updated_time"
        _sql = (
            f"INSERT INTO ai_inner_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET {_set}"
        )
    else:
        _sql = (
            f"INSERT INTO ai_inner_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET updated_time=excluded.updated_time"
        )
    q(_sql, _params)


# ---------------- conversation_behavior ----------------

def get_behavior_state(
    session_id="default",
    character_id="default"
):
    rows = q(
        """
        SELECT *
        FROM conversation_behavior
        WHERE session_id=? AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    return dict(rows[0]) if rows else {}


def update_behavior_state(
    session_id="default",
    character_id="default",
    **kwargs
):
    """原子 UPSERT：INSERT ... ON CONFLICT DO UPDATE（只覆盖显式传入字段）。"""
    _fields = ["behavior", "response_length", "emotional_tone", "avoid_actions"]
    _defaults = {f: "" for f in _fields}
    _provided = [f for f in _fields if f in kwargs]

    _cols = ["session_id", "character_id"] + _fields + ["updated_time"]
    _marks = ",".join(["?"] * len(_cols))
    _params = [session_id, character_id] + [kwargs.get(f, _defaults[f]) for f in _fields] + [_now()]

    if _provided:
        _set = ",".join(f"{f}=excluded.{f}" for f in _provided) + ",updated_time=excluded.updated_time"
        _sql = (
            f"INSERT INTO conversation_behavior ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET {_set}"
        )
    else:
        _sql = (
            f"INSERT INTO conversation_behavior ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET updated_time=excluded.updated_time"
        )
    q(_sql, _params)


# ---------------- personality_state ----------------

def get_personality_state(
    session_id="default",
    character_id="default"
):
    """读人格状态。

    ★ 2026-09-17 修（双键分叉导致学习写进读不到的那把键）：
      真机实测同一 session 下同时存在两行 ——
        (s_93ceb989…, '骨子')   ← 人设快照（core_personality/speaking_style）
        (s_93ceb989…, 'default') ← 反思与反馈写出来的**行为策略**在这行
      两边都在被写（updated_time 都是刚刚），而读侧只按 character_id 精确取一条：
      写进 'default' 的那些学习成果（warmth/humor 偏移、relationship_behavior）
      在按 '骨子' 读时**完全读不到** —— 约一半的人格学习等于白学。
      根因是有调用方用 character_id='default' 落库（历史遗留的多调用点）。

      修法：先精确取；取不到、或取到但没有任何"学到的东西"时，
      回落到同 session 的 'default' 行（只回落**行为策略类**字段，人设字段以精确行为准）。
      这是读侧兼容，不动写侧、不改历史数据，随时可回退。
    """
    rows = q(
        """
        SELECT *
        FROM personality_state
        WHERE session_id=? AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    exact = dict(rows[0]) if rows else {}

    _learned_fields = ("warmth_delta", "dominance_delta", "humor_delta",
                       "initiative_delta", "attachment_delta",
                       "relationship_behavior", "forbidden_phrases",
                       "avoid_style_count", "last_negative_reply")
    if str(character_id or "") == "default":
        return exact          # 本身就是 default 桶，没什么可回落
    if any(str(exact.get(f) or "").strip() not in ("", "0") for f in _learned_fields):
        return exact          # 精确行里已经有学习成果，优先用它

    try:
        rows2 = q(
            """
            SELECT *
            FROM personality_state
            WHERE session_id=? AND character_id='default'
            LIMIT 1
            """,
            (session_id,),
            fetch=True
        )
    except Exception:
        rows2 = None
    if not rows2:
        return exact
    fallback = dict(rows2[0])
    if not fallback:
        return exact
    merged = dict(fallback)
    # 人设类字段以精确行为准（有值就用精确行的），学习类字段用回落行补
    for k, v in exact.items():
        if k in ("id", "session_id", "character_id"):
            continue
        if str(v or "").strip() not in ("", "0"):
            merged[k] = v
    merged["session_id"] = session_id
    merged["character_id"] = character_id
    merged["_fallback_from_default"] = True
    return merged


def update_personality_state(
    session_id="default",
    character_id="default",
    **kwargs
):
    # ★ _skip_snapshot=True 时不存历史快照（rollback 用，避免回滚自身产生快照导致来回横跳）
    _skip_snap = bool(kwargs.pop("_skip_snapshot", False))

    # 快照逻辑需要旧值，保留一次 SELECT（写路径用 ON CONFLICT 原子 UPSERT）
    old = get_personality_state(
        session_id,
        character_id
    )

    _fields = [
        "core_personality", "speaking_style", "favorite_phrases",
        "forbidden_phrases", "emotional_expression",
        "relationship_behavior", "user_mirror",
        "warmth_delta", "dominance_delta", "humor_delta",
        "initiative_delta", "attachment_delta",
        "softness", "playfulness", "vulnerability",
        "growth_interactions", "growth_events", "last_growth_at",
        "avoid_style_count", "last_negative_reply",
    ]
    _defaults = {
        "core_personality": "", "speaking_style": "",
        "favorite_phrases": "", "forbidden_phrases": "",
        "emotional_expression": "", "relationship_behavior": "",
        "user_mirror": "", "avoid_style_count": 0,
        "last_negative_reply": "",
        "warmth_delta": 0, "dominance_delta": 0, "humor_delta": 0,
        "initiative_delta": 0, "attachment_delta": 0,
        "softness": 0, "playfulness": 0, "vulnerability": 0,
        "growth_interactions": 0, "growth_events": "[]", "last_growth_at": "",
    }
    _provided = [f for f in _fields if f in kwargs]
    _vals = {
        f: (int(kwargs.get(f, 0) or 0) if f in ("avoid_style_count", "warmth_delta", "dominance_delta", "humor_delta", "initiative_delta", "attachment_delta", "softness", "playfulness", "vulnerability", "growth_interactions") else kwargs.get(f, _defaults[f]))
        for f in _fields
    }

    # ★ UPDATE前存历史快照（只在有旧值时存，INSERT时不存；rollback 跳过快照）
    if old and not _skip_snap:
        try:
            import json as _json
            _snap = _json.dumps(old, ensure_ascii=False)
            _reason = kwargs.pop("_reason", "auto")
            q("""INSERT INTO personality_history
                (session_id, character_id, snapshot, reason, created_time)
                VALUES(?,?,?,?,?)""",
              (session_id, character_id, _snap, _reason, _now()))
            # ★ 只保留最近10个版本，超出自动删除（id DESC 次级排序防同秒乱序）
            q("""DELETE FROM personality_history
                WHERE session_id=? AND character_id=?
                AND id NOT IN (
                    SELECT id FROM personality_history
                    WHERE session_id=? AND character_id=?
                    ORDER BY created_time DESC, id DESC LIMIT 10
                )""",
              (session_id, character_id, session_id, character_id))
        except Exception:
            pass

    _cols = ["session_id", "character_id"] + _fields + ["updated_time"]
    _marks = ",".join(["?"] * len(_cols))
    _params = [session_id, character_id] + [_vals.get(f, _defaults[f]) for f in _fields] + [_now()]

    if _provided:
        _set = ",".join(f"{f}=excluded.{f}" for f in _provided) + ",updated_time=excluded.updated_time"
        _sql = (
            f"INSERT INTO personality_state ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET {_set}"
        )
    elif old:
        # 无显式字段且有旧行：无操作（保持原语义）
        return
    else:
        _sql = f"INSERT INTO personality_state ({','.join(_cols)}) VALUES({_marks})"
    q(_sql, _params)


def update_five_dim(
    session_id="default", character_id="default",
    warmth_delta=0, dominance_delta=0, humor_delta=0,
    initiative_delta=0, attachment_delta=0
):
    """只更新五维delta字段，不触碰文本字段"""
    old = get_personality_state(session_id, character_id)
    if old:
        q("""UPDATE personality_state SET
            warmth_delta=?, dominance_delta=?, humor_delta=?,
            initiative_delta=?, attachment_delta=?,
            updated_time=?
            WHERE session_id=? AND character_id=?""",
          (warmth_delta, dominance_delta, humor_delta,
           initiative_delta, attachment_delta,
           _now(), session_id, character_id))
    else:
        # 先INSERT一行空记录再更新
        update_personality_state(session_id, character_id)
        q("""UPDATE personality_state SET
            warmth_delta=?, dominance_delta=?, humor_delta=?,
            initiative_delta=?, attachment_delta=?
            WHERE session_id=? AND character_id=?""",
          (warmth_delta, dominance_delta, humor_delta,
           initiative_delta, attachment_delta,
           session_id, character_id))


def rollback_personality(session_id="default", character_id="default", steps=1):
    """
    回滚人格状态到steps版本前
    steps=1: 回滚到上一版本
    """
    import json as _json
    rows = q("""
        SELECT id, snapshot FROM personality_history
        WHERE session_id=? AND character_id=?
        ORDER BY created_time DESC, id DESC LIMIT ?
        OFFSET ?
    """, (session_id, character_id, 1, max(0, steps - 1)), fetch=True)

    if not rows:
        return False, "没有更早的人格历史版本"

    try:
        snap = _json.loads(rows[0]["snapshot"])
    except Exception:
        return False, "历史快照解析失败"

    if not isinstance(snap, dict):
        return False, "历史快照格式异常"

    # 剔除元数据键（session_id/character_id/id/时间戳），只回滚人格字段
    _snap = {k: v for k, v in snap.items()
             if k not in ("session_id", "character_id", "id",
                          "updated_time", "created_time")}

    update_personality_state(
        session_id, character_id,
        _reason="rollback",
        _skip_snapshot=True,
        **_snap
    )

    # ★ 消费制：删除用掉的快照，下次回滚继续往前（避免二次回滚拿到同一版本）
    try:
        q("DELETE FROM personality_history WHERE id=?", (rows[0]["id"],))
    except Exception:
        pass

    return True, f"已回滚到{steps}版本前的人格状态"


# ---------------- relationship_timeline ----------------


def add_timeline_event(
    session_id="default",
    character_id="default",
    event_type="",
    title="",
    description="",
    importance=5,
    state_delta=""
):
    """追加关系时间线事件。

    ★ state_delta（2026-09-13 事件溯源）：JSON 字符串，记录本次事件带来的
      内部状态增量（如 '{"intimacy":+3,"affection":-2,"reason":"..."}'）。
      传 dict 会自动序列化；空值存 ''。
    """
    if isinstance(state_delta, dict):
        try:
            state_delta = json.dumps(state_delta, ensure_ascii=False)
        except Exception:
            state_delta = ""
    q(
        """
        INSERT INTO relationship_timeline
        (
            session_id,
            character_id,
            event_type,
            title,
            description,
            importance,
            state_delta
        )
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            session_id,
            character_id,
            event_type,
            title,
            description,
            int(importance or 5),
            str(state_delta or "")
        )
    )


def get_state_delta_events(
    session_id="default",
    character_id="default",
    limit=8
):
    """读最近带状态增量的时间线事件（自我审计/「你怎么看我」接地用）。"""
    rows = q(
        """
        SELECT created_at, event_type, title, description, state_delta
        FROM relationship_timeline
        WHERE session_id=?
          AND character_id=?
          AND state_delta IS NOT NULL
          AND state_delta != ''
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (
            session_id,
            character_id,
            int(limit)
        ),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_recent_timeline_events(
    session_id="default",
    character_id="default",
    limit=5
):
    rows = q(
        """
        SELECT *
        FROM relationship_timeline
        WHERE session_id=?
          AND character_id=?
        ORDER BY importance DESC, created_at DESC
        LIMIT ?
        """,
        (
            session_id,
            character_id,
            int(limit)
        ),
        fetch=True
    )
    return [dict(r) for r in rows]


def increase_timeline_used_count(event_id):
    q(
        """
        UPDATE relationship_timeline
        SET used_count = used_count + 1
        WHERE id=?
        """,
        (event_id,)
    )


# ---------------- life_profile ----------------


def save_life_profile(
    session_id="default",
    character_id="default",
    profile_type="",
    content="",
    importance=5
):
    q(
        """
        INSERT INTO life_profile
        (
            session_id,
            character_id,
            profile_type,
            content,
            importance
        )
        VALUES(?,?,?,?,?)
        """,
        (
            session_id,
            character_id,
            profile_type,
            content,
            int(importance or 5)
        )
    )


def get_life_profiles(
    session_id="default",
    character_id="default",
    limit=10
):
    rows = q(
        """
        SELECT *
        FROM life_profile
        WHERE session_id=?
          AND character_id=?
        ORDER BY importance DESC, update_time DESC
        LIMIT ?
        """,
        (
            session_id,
            character_id,
            int(limit)
        ),
        fetch=True
    )
    return [dict(r) for r in rows]


def get_life_profile_by_type(
    session_id="default",
    character_id="default",
    profile_type="",
    limit=5
):
    rows = q(
        """
        SELECT *
        FROM life_profile
        WHERE session_id=?
          AND character_id=?
          AND profile_type=?
        ORDER BY importance DESC, update_time DESC
        LIMIT ?
        """,
        (
            session_id,
            character_id,
            profile_type,
            int(limit)
        ),
        fetch=True
    )
    return [dict(r) for r in rows]


def count_life_profiles(
    session_id="default",
    character_id="default"
):
    rows = q(
        """
        SELECT COUNT(*) as cnt
        FROM life_profile
        WHERE session_id=?
          AND character_id=?
        """,
        (session_id, character_id),
        fetch=True
    )
    return int(rows[0]["cnt"]) if rows else 0


# ---------------- long_term_memory ----------------

# ★ valid_memories() 的硬上限：单次读取的有效记忆条数。
#   历史：原 SQL 全量读取无 LIMIT，记忆上千后通道 A 评分循环变慢，于是砍到 200。
#   实测后果（2026-09-17）：活会话 2769 条有效记忆里 **92.8% 永远进不了打分**，
#   「刚说的话」只要不在 importance 前 200 名就再也想不起来 —— 这是"她记不住"
#   的头号结构性原因。现改为按调用方给的上限（默认 600），并且**检索路径改用
#   分层召回 recall_candidates()**，让"最近/高频/向量近邻"都有一席之地，
#   不再是单调的 importance 榜单。
_VALID_MEMORIES_LIMIT = 600
# 兼容旧名（曾用 200 表示"每次回复只读 200 条"）；新代码不要再引用这个常量。
_VALID_MEMORIES_LIMIT_LEGACY = 200


def valid_memories(category: str = None, session_id: str = "default",
                   character_id: str = "default", limit: int = None):
    # Memory Scope 过滤逻辑：
    # global 记忆：所有角色可见
    # character 记忆：仅当前角色可见
    # relationship 记忆：仅当前角色可见
    _limit = _VALID_MEMORIES_LIMIT if limit is None else max(1, int(limit))
    scope_where = """
        AND (
            memory_scope = 'global'
            OR (memory_scope IN ('character', 'relationship') AND character_id = ?)
        )
    """
    scope_params = [character_id]

    if category:
        rows = q(
            """
            SELECT
                id,
                session_id,
                character_id,
                memory_scope,
                memory_content,
                category,
                memory_type,
                importance,
                create_time,
                update_time,
                last_used,
                access_count,
                decay_score,
                context,
                emotion_tag,
                source_text,
                confidence,
                recall_count,
                last_recalled
            FROM long_term_memory
            WHERE is_valid=1
            AND category=?
            AND session_id=?
            """ + scope_where + """
            ORDER BY importance DESC, id DESC
            LIMIT ?
            """,
            [category, session_id] + scope_params + [_limit],
            fetch=True
        )
    else:
        rows = q(
            """
            SELECT
                id,
                session_id,
                character_id,
                memory_scope,
                memory_content,
                category,
                memory_type,
                importance,
                create_time,
                update_time,
                last_used,
                access_count,
                decay_score,
                context,
                emotion_tag,
                source_text,
                confidence,
                recall_count,
                last_recalled
            FROM long_term_memory
            WHERE is_valid=1
            AND session_id=?
            """ + scope_where + """
            ORDER BY importance DESC, id DESC
            LIMIT ?
            """,
            [session_id] + scope_params + [_limit],
            fetch=True
        )
    return [dict(r) for r in rows]


def recall_candidates(session_id: str = "default", character_id: str = "default",
                      query: str = None, per_channel: int = 200) -> list:
    """分层召回候选池 —— 检索路径**唯一**该用的取数函数。

    ★ 为什么必须有（2026-09-17 实测）：
      `valid_memories()` 是 `ORDER BY importance DESC, id DESC LIMIT N`，
      活会话 2769 条有效记忆里 2569 条（92.8%）永远进不了打分循环；
      而 4384 条记忆里**只有 158 条被真正召回注入过**（3.6%），
      6 条 importance=10 的老记忆各被召回 279~518 次，其余从未被想起。
      "她记不住"不是模型不会，是**候选池根本没让她看见**。

    四路各取一批（按 id 去重合并）：
      ① 重要度      —— 保住旧口径在意的核心记忆
      ② 最近新增    —— 昨天刚说的必须能想起来（低重要度也算）
      ③ 高频使用    —— 常被用到的（access_count/recall_count）优先保住
      ④ 向量近邻    —— 有 query 时按语义取最相近的（embedding 不可用则跳过）

    ★ ② 用 `create_time DESC` 排序，**不是 id DESC**：id 只反映插入顺序，
      历史导入 / 换库 / 补写都可能让"更晚说的话"拿到更小的 id，
      而 id DESC 一旦被高 id 的旧数据占满，新记忆就整个掉出池子
      （本函数的回归测试抓的正是这个：新记忆 id=1、旧记忆 id 2..261，
       id DESC 取 200 条全是旧的）。

    返回：记忆 dict 列表（去重后）。
    """
    scope_where = """
        AND (
            memory_scope = 'global'
            OR (memory_scope IN ('character', 'relationship') AND character_id = ?)
        )
    """
    cols = """id, session_id, character_id, memory_scope, memory_content, category,
              memory_type, importance, create_time, update_time, last_used, access_count,
              decay_score, context, emotion_tag, source_text, confidence,
              recall_count, last_recalled"""
    per = max(1, int(per_channel))
    out, seen = [], set()

    def _take(rows):
        for r in rows or []:
            d = dict(r)
            key = d.get("id")
            if key in seen:
                continue
            seen.add(key)
            out.append(d)

    # ① 重要度
    try:
        _take(q(f"""SELECT {cols} FROM long_term_memory
                    WHERE is_valid=1 AND session_id=? {scope_where}
                    ORDER BY importance DESC, id DESC LIMIT ?""",
                [session_id, character_id, per], fetch=True))
    except Exception:
        pass

    # ② 最近新增（按 create_time，低重要度的新记忆也必须在池子里）
    try:
        _take(q(f"""SELECT {cols} FROM long_term_memory
                    WHERE is_valid=1 AND session_id=? {scope_where}
                    ORDER BY create_time DESC, id DESC LIMIT ?""",
                [session_id, character_id, per], fetch=True))
    except Exception:
        pass

    # ③ 高频使用
    try:
        _take(q(f"""SELECT {cols} FROM long_term_memory
                    WHERE is_valid=1 AND session_id=? {scope_where}
                    ORDER BY (COALESCE(access_count,0) + COALESCE(recall_count,0)) DESC,
                             id DESC LIMIT ?""",
                [session_id, character_id, per], fetch=True))
    except Exception:
        pass

    # ④ 向量近邻（语义候选，不受 importance 排序影响）
    if query and str(query).strip():
        try:
            from .memory.embedding import encode as _enc
            from .memory.vector_store import search_vector as _sv
            _qv = _enc(str(query)[:512])
            if _qv:
                for h in (_sv(user_id=session_id, embedding=_qv, limit=per,
                              character_id=character_id) or []):
                    mid = str(h.get("id") or "").replace("sql:", "")
                    if not mid.isdigit() or int(mid) in seen:
                        continue
                    row = get_memory(int(mid))
                    if row and int(row.get("is_valid", 1) or 0) == 1:
                        seen.add(int(mid))
                        out.append(dict(row))
        except Exception:
            pass

    return out


def insert_memory(
    content: str,
    category: str = "general",
    memory_type: str = "fact",
    importance: int = 5,
    session_id: str = "default",
    character_id: str = "default",
    # ★ 防记忆串桶：默认 character（角色专属）。调用方必须显式传 global
    #   才会跨角色共享（scope_classifier 判定为用户中性基础信息时才传）
    memory_scope: str = "character",
    confidence: float = 1.0,          # ★ 新增
    context: str = "",                # ★ 信息单元：情境
    emotion_tag: str = "",            # ★ 信息单元：情绪标签
    source_text: str = "",            # ★ 信息单元：溯源原话
):
    cur = q(
        """
        INSERT INTO long_term_memory
        (
            session_id,
            character_id,
            memory_scope,
            memory_content,
            category,
            memory_type,
            importance,
            confidence,
            create_time,
            is_valid,
            context,
            emotion_tag,
            source_text
        )
        VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)
        """,
        (
            session_id,
            character_id,
            memory_scope,
            content,
            category,
            memory_type,
            importance,
            float(confidence),          # ★ 新增
            _now(),
            str(context or ""),
            str(emotion_tag or ""),
            str(source_text or "")
        )
    )
    # ★ 返回 lastrowid（供向量同步等调用方使用）
    return cur.lastrowid if cur is not None else 0


def update_memory(mem_id: int, content: str):
    q("UPDATE long_term_memory SET memory_content=?, update_time=? WHERE id=?",
      (content, _now(), mem_id))


def update_memory_full(mem_id: int, content: str = None, importance: int = None, memory_type: str = None,
                       context: str = None, emotion_tag: str = None, source_text: str = None):
    """综合更新记忆（内容/重要度/类型/情境/情绪/溯源），只更新传入的非空字段。"""
    sets = ["update_time=?"]
    params = [_now()]
    if content is not None:
        sets.append("memory_content=?")
        params.append(content)
    if importance is not None:
        sets.append("importance=?")
        params.append(int(importance))
    if memory_type is not None:
        sets.append("memory_type=?")
        params.append(memory_type)
    if context is not None:
        sets.append("context=?")
        params.append(str(context))
    if emotion_tag is not None:
        sets.append("emotion_tag=?")
        params.append(str(emotion_tag))
    if source_text is not None:
        sets.append("source_text=?")
        params.append(str(source_text))
    params.append(mem_id)
    q("UPDATE long_term_memory SET " + ", ".join(sets) + " WHERE id=?", params)


def touch_recall(mem_ids):
    """记忆被召回时更新使用统计 —— **全项目唯一**的召回统计写入口。

     ★ 2026-09-13 修复。原先有**四处各写各的** UPDATE（下面三处均已删除）：
        · db.touch_recall()（本函数）—— 当时是**零调用点**（写了也没人用）
        · memory/retriever._update_last_used()（写 last_used/recall_count）—— 实际在跑
        · memory/manager.update_last_used()（写 last_used/recall_count）—— 与上面重复
        · db.increase_memory_access()（写 access_count/last_used）—— **零调用点**
      后果（线上 3486 条有效记忆实测）：
        · `access_count` 全为 0 → calculate_decay 的 access 项、importance 的
          "高访问永久保留"、maintenance 的访问加权 —— 三处逻辑全部恒为 0；
        · `last_recalled` 全为空 → "最后被想起的时间"从未记录。
      **现状：上面另外三处已全部删除，本函数是全项目唯一的召回统计写入口。**
      调用点只有一个：`memory_manager.memory_block()` —— 即"这一批记忆真的被
      注入到 prompt 里了"才记一次，一轮一次，不会像旧实现那样在每次检索调用里
      对每条结果重复累加（那正是 recall_count 被堆到 5628、把 70 条记忆压死的成因）。

      字段语义（互不混用）：
      recall_count  —— 被召回次数（memory_manager 用它做有上限的重复惩罚）
      access_count  —— 访问次数（importance/decay/maintenance 用它判"核心记忆"）
      last_used / last_recalled —— "最后被用到/被想起"的时刻（_time_score 的新鲜度依据）
      update_time   —— **只表示内容被改过**，召回不写它（否则旧记忆会被越刷越"新"）
    """
    if not mem_ids:
        return
    ids = [int(x) for x in mem_ids if str(x).isdigit()]
    if not ids:
        return
    now = _now()
    with _lock:
        init()
        try:
            _conn.executemany(
                "UPDATE long_term_memory SET "
                "recall_count = COALESCE(recall_count,0) + 1, "
                "access_count = COALESCE(access_count,0) + 1, "
                "last_recalled = ?, last_used = ? WHERE id=?",
                [(now, now, i) for i in ids]
            )
            _conn.commit()
        except Exception:
            pass


def replace_all_memories(contents, session_id="default", character_id="default"):
    """覆盖导入：仅清空目标 (session_id, character_id) 的记忆后写入（不误删其他角色）。"""
    with _lock:
        init()
        _conn.execute(
            "DELETE FROM long_term_memory WHERE session_id=? AND character_id=?",
            (session_id, character_id)
        )
        now = _now()
        _conn.executemany(
            "INSERT INTO long_term_memory(session_id, character_id, memory_content, create_time, is_valid) "
            "VALUES(?,?,?,?,1)",
            [(session_id, character_id, c, now) for c in contents])
        _conn.commit()


def count_memories() -> int:
    rows = q("SELECT COUNT(*) AS n FROM long_term_memory WHERE is_valid=1", fetch=True)
    return rows[0]["n"] if rows else 0


def delete_memory(mem_id: int, reason: str = "deleted"):
    """软删除：标记 is_valid=0，保留记录可追溯。

    ★ 2026-09-13 修复：原先只写 `is_valid=0`，**不写 memory_status** ——
      于是失效记忆留在 `memory_status='active'`，看起来还是"生效中"。
      实测线上有 24 条这种不一致记录（全部来自"用户纠正"路径），
      导致事后无法判断一条记忆**为什么**失效（衰减清理？用户纠正？冲突作废？）。
      reason 取值约定：
        deleted     —— 普通删除/清理
        superseded  —— 被新事实取代（冲突判定作废）
        corrected   —— 用户明确纠正后作废
        archived    —— 衰减/低价值归档（旧路径沿用）
    """
    q(
        "UPDATE long_term_memory SET is_valid=0, memory_status=?, update_time=? WHERE id=?",
        (str(reason or "deleted"), _now(), mem_id),
    )


def touch_memory(mem_id: int):
    """更新记忆最后使用时间，用于时间衰减评分"""
    q(
        """
        UPDATE long_term_memory
        SET last_used=?
        WHERE id=?
        """,
        (
            _now(),
            mem_id
        )
    )


# ★ 2026-09-13：`increase_memory_access()` 已删除。
#   它写的正是 access_count/last_used，但**全仓零调用点**（线上实测 3486 条
#   有效记忆的 access_count 全为 0，正是因为它从没被调过）。
#   现在这些字段统一由 `touch_recall()` 维护，保留同名函数只会制造
#   "看起来有两条实现、实际只有一条在跑"的误解。
#   如需按 id 记一次访问，请调用 touch_recall([id])。


# 记忆失效原因（写进 memory_status，事后可追溯"为什么没这条记忆了"）
INVALID_REASONS = ("deleted", "superseded", "corrected", "archived")


def invalidate_memory(
    memory_id,
    reason: str = "archived",
):
    """使记忆失效（软删除）并记录**失效原因**。

    ★ 2026-09-13：原来这里把状态写死成 'archived'，而它同时被
      `manager.delete_memory`（用户删除）和 `memory_brain.smart_insert`
      （冲突作废）调用 —— 于是"冲突作废"和"低价值归档"在库里混成一种，
      事后无法区分（实测 372 条 archived 里混着两类）。
      现在由调用方传 reason：
        superseded —— 被新事实取代（memory_brain 冲突判定）
        corrected  —— 用户明确纠正（feedback/corrections）
        deleted    —— 用户/界面删除
        archived   —— 衰减或低价值归档
    """
    _r = str(reason or "archived")
    if _r not in INVALID_REASONS:
        _r = "archived"
    q(
        """
        UPDATE long_term_memory
        SET
            is_valid=0,
            memory_status=?,
            update_time=?
        WHERE id=?
        """,
        (
            _r,
            _now(),
            memory_id,
        )
    )


def get_memory(memory_id):
    """根据ID获取单条记忆"""
    rows = q(
        """
        SELECT *
        FROM long_term_memory
        WHERE id=?
        LIMIT 1
        """,
        (memory_id,),
        fetch=True
    )
    return dict(rows[0]) if rows else None


def update_memory_importance(memory_id, importance):
    """更新记忆的重要性分数"""
    q(
        """
        UPDATE long_term_memory
        SET importance=?
        WHERE id=?
        """,
        (
            int(importance),
            memory_id
        )
    )


def get_active_sessions(hours: int = 24) -> list:
    """
    返回最近 N 小时内有消息记录的 session 列表。
    用于反思定时任务、记忆清理等批量任务。

    Returns:
        list of dict: [{"session_id": ..., "character_id": ...}, ...]
    """
    rows = q(
        """
        SELECT DISTINCT session_id, character_id
        FROM chat_history
        -- ★ P1-6：timestamp 由 _now() 写成 'YYYY-MM-DDTHH:MM:SS'（带 T），
        --   而 SQLite 的 datetime('now', ...) 返回 'YYYY-MM-DD HH:MM:SS'（空格）。
        --   两者直接做字符串比较原本只是"碰巧"对：靠 'T'(0x54) > ' '(0x20) 的
        --   ASCII 序才没出错，一旦列里混入空格格式的旧数据/手工数据就会算错。
        --   用 replace 把两边统一成空格格式再比，两种存储格式都能正确命中。
        WHERE replace(timestamp, 'T', ' ') >= datetime('now', ? || ' hours')
        ORDER BY session_id
        """,
        (f"-{hours}",),
        fetch=True
    )
    return [dict(r) for r in rows] if rows else []


def count_messages(session_id: str, character_id: str = "default") -> int:
    """返回指定 session+character 的消息总数（用于反思轮次触发判断）。"""
    rows = q(
        "SELECT role, content FROM chat_history WHERE session_id=? AND character_id=?",
        (session_id, character_id),
        fetch=True
    )
    return sum(1 for r in (rows or []) if not is_internal_chat_message(r["role"], r["content"]))


# ---------------- 关系档案（统计 + 按日期浏览） ----------------

def music_duration_tick(character_id: str, new_mode: str) -> int:
    """陪伴模式切换时结算「一起听音乐」时长。
    new_mode=='music' 开始计时；离开 music 时结算本次时长。返回累计秒数。"""
    import time as _t
    _now = int(_t.time())
    _start_key = f"music_duration_start:{character_id}"
    _total_key = f"music_duration_total:{character_id}"
    _start = int(kv_get(_start_key) or 0)
    _total = int(kv_get(_total_key) or 0)
    if new_mode == "music":
        if not _start:
            kv_set(_start_key, str(_now))
    else:
        if _start:
            _total += max(0, _now - _start)
            kv_set(_total_key, str(_total))
            kv_set(_start_key, "0")
    return _total


def music_duration_seconds(character_id: str) -> int:
    """返回「一起听音乐」累计秒数（含当前进行中的未结算时长）。"""
    import time as _t
    _now = int(_t.time())
    _start = int(kv_get(f"music_duration_start:{character_id}") or 0)
    _total = int(kv_get(f"music_duration_total:{character_id}") or 0)
    if _start:
        _total += max(0, _now - _start)
    return _total


def archive_stats(character_id: str = "default") -> dict:
    """关系档案统计：总消息/记忆/通话/表情包/图片，以及首末日期、有对话的日期列表。

    跨 session 聚合（同一角色名下所有会话），排除系统内部伪消息。
    """
    out = {
        "total_messages": 0,
        "total_memories": 0,
        "total_calls": 0,
        "total_stickers": 0,
        "total_images": 0,
        "total_assistant": 0,
        "total_user": 0,
        "music_minutes": 0,
        "first_date": "",
        "last_date": "",
        "active_dates": [],
    }

    # 消息统计（跨 session，按角色聚合）
    rows = q(
        "SELECT role, content FROM chat_history WHERE character_id=?",
        (character_id,),
        fetch=True,
    )
    msgs = [r for r in (rows or []) if not is_internal_chat_message(r["role"], r["content"])]
    out["total_messages"] = len(msgs)
    out["total_user"] = sum(1 for r in msgs if r["role"] == "user")
    out["total_assistant"] = sum(1 for r in msgs if r["role"] == "assistant")
    out["total_stickers"] = sum(1 for r in msgs if "[sticker:" in (r["content"] or ""))
    # ★ 同时统计 Web/App 发的 [图片] 占位符 和 QQ 机器人发的「【用户发来图片：xxx】」描述
    out["total_images"] = sum(
        1 for r in msgs
        if (c := (r["content"] or "").strip()) == "[图片]" or c.startswith("【用户发来图片：")
    )

    # 记忆数
    mrows = q(
        "SELECT COUNT(*) AS c FROM long_term_memory WHERE character_id=? AND is_valid=1",
        (character_id,), fetch=True,
    )
    out["total_memories"] = (mrows[0]["c"] if mrows else 0)

    # 通话次数
    crows = q(
        "SELECT COUNT(*) AS c FROM call_records WHERE character_id=?",
        (character_id,), fetch=True,
    )
    out["total_calls"] = (crows[0]["c"] if crows else 0)

    # 日期范围 + 有对话的日期列表
    drows = q(
        "SELECT MIN(timestamp) AS mn, MAX(timestamp) AS mx FROM chat_history WHERE character_id=?",
        (character_id,), fetch=True,
    )
    if drows and drows[0]["mn"]:
        out["first_date"] = str(drows[0]["mn"])[:10]
        out["last_date"] = str(drows[0]["mx"])[:10]
    adrows = q(
        "SELECT DISTINCT substr(timestamp,1,10) AS d FROM chat_history WHERE character_id=? ORDER BY d",
        (character_id,), fetch=True,
    )
    out["active_dates"] = [r["d"] for r in (adrows or []) if r["d"]]
    # ★ 一起听音乐累计时长（分钟）
    try:
        out["music_minutes"] = music_duration_seconds(character_id) // 60
    except Exception:
        out["music_minutes"] = 0
    return out


def messages_by_date(character_id: str = "default", date: str = "") -> list:
    """返回某角色某天（YYYY-MM-DD）的所有消息，按时间正序，排除内部伪消息。"""
    import json as _json
    if not date:
        return []
    rows = q(
        "SELECT id, role, content, timestamp, extra FROM chat_history "
        "WHERE character_id=? AND substr(timestamp,1,10)=? ORDER BY id ASC",
        (character_id, date), fetch=True,
    )
    rows = _filter_chat_rows(rows)
    out = []
    for r in rows:
        _extra = r["extra"]
        if _extra:
            try:
                _extra = _json.loads(_extra)
            except Exception:
                _extra = {}
        out.append({
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "timestamp": r["timestamp"],
            "extra": _extra or {},
        })
    return out


# ---------------- user_profile ----------------

# ★ 2026-09-16：「我的信息」（character_id='default'）的**稳定桶**
#
#   用户报「在用户个人设置中设置的信息无法保存」的根因就在这里：
#   character_id='default' 是**全角色共享**的「我的信息」桶（main.py 里就是这么注释的），
#   可它的存储键里含 session_id —— 而 session_id 是按「**最近聊天的那个角色**」生成的：
#       public/js/app.js:1674  Session.initSession(Store.listConversations()[0].contact.name)
#       backend/main.py:6352   session_id = hash(user_id + ':' + character_id)
#   ⇒ 用户换一个角色聊天并重启后 session_id 就变了，同一份「我的信息」被写到/读到**另一个桶**：
#     保存那一刻读得回（前端 toast「已保存 ✓」），下次打开设置却是空的。
#   修法：character_id='default' 的读写一律归一到该用户 character_id='default' 的确定性
#   session（与 /session/get_or_create 同算法），于是它不再随当前角色漂移；认不出用户时
#   退回字面量 'default'（perception._get_city 一直用的就是这个口径）。
#   修复前已经写进"漂移桶"的旧行在读取时被**认领**（复制到稳定桶，不删旧行）。
GLOBAL_PROFILE_CHAR = "default"


def session_id_for(user_id: str, character_id: str) -> str:
    """user_id + character_id → 确定性 session_id（同输入恒同输出）。

    这是**唯一**实现：backend/main.py 的 _generate_session_id 已改为调用本函数，
    两处若各写一份，稳定桶的键位就会和 /session/get_or_create 给出的 session 对不上。
    """
    import hashlib
    raw = f"{user_id}:{character_id}"
    return "s_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _profile_session(session_id) -> str:
    """「我的信息」桶的稳定会话键（认不出用户 → 字面量 'default'）。"""
    sid = str(session_id or "").strip() or GLOBAL_PROFILE_CHAR
    if sid == GLOBAL_PROFILE_CHAR:
        return GLOBAL_PROFILE_CHAR
    try:
        rows = q("SELECT user_id FROM sessions WHERE session_id=? LIMIT 1", (sid,), fetch=True)
    except Exception:
        rows = None
    uid = str(rows[0]["user_id"] or "").strip() if rows else ""
    return session_id_for(uid, GLOBAL_PROFILE_CHAR) if uid else GLOBAL_PROFILE_CHAR


def _user_session_ids(session_id) -> list:
    """该 session 所属用户的全部 session_id（认领旧数据用；认不出用户则空表）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return []
    try:
        rows = q(
            "SELECT session_id FROM sessions WHERE user_id="
            "(SELECT user_id FROM sessions WHERE session_id=? LIMIT 1) "
            "ORDER BY last_active_at DESC",
            (sid,),
            fetch=True,
        )
    except Exception:
        return []
    return [str(r["session_id"]) for r in (rows or [])]


def _ensure_profile_unique_index():
    """user_profile 的 (session_id, character_id) 唯一索引（幂等）。"""
    try:
        q(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profile_unique "
            "ON user_profile(session_id, character_id)"
        )
    except Exception:
        pass


def _profile_row(session_id, character_id):
    rows = q(
        """
        SELECT *
        FROM user_profile
        WHERE session_id=?
        AND character_id=?
        LIMIT 1
        """,
        (session_id, character_id),
        fetch=True
    )
    if rows:
        return dict(rows[0])
    return {}


def _adopt_profile_row(old: dict, target_session: str):
    """把旧桶里的「我的信息」整行复制到稳定桶（只 INSERT，**不删来源行**）。"""
    _ensure_profile_unique_index()
    fields = ["nickname", "personality", "occupation", "hobbies", "dislikes",
              "communication_style", "emotional_traits", "ext_json"]
    vals = []
    for f in fields:
        v = old.get(f)
        if v is None:
            v = "{}" if f == "ext_json" else ""
        vals.append(v)
    cols = ["session_id", "character_id"] + fields + ["updated_time"]
    marks = ",".join(["?"] * len(cols))
    q(
        f"INSERT INTO user_profile ({','.join(cols)}) VALUES({marks}) "
        f"ON CONFLICT(session_id, character_id) DO NOTHING",
        [target_session, GLOBAL_PROFILE_CHAR] + vals + [old.get("updated_time") or _now()],
    )


def _get_global_profile(session_id) -> dict:
    """读「我的信息」；读不到时认领修复前写在"漂移桶"里的旧行（不删旧行）。"""
    canon = _profile_session(session_id)
    row = _profile_row(canon, GLOBAL_PROFILE_CHAR)
    if row:
        return row
    for legacy in list(_user_session_ids(session_id)) + [GLOBAL_PROFILE_CHAR]:
        if legacy == canon:
            continue
        old = _profile_row(legacy, GLOBAL_PROFILE_CHAR)
        if not old:
            continue
        try:
            _adopt_profile_row(old, canon)
            print("[Profile] 「我的信息」已从旧桶 %s 认领到稳定桶 %s（旧行保留）"
                  % (legacy, canon), flush=True)
            return _profile_row(canon, GLOBAL_PROFILE_CHAR) or old
        except Exception as e:
            print("[Profile] 认领「我的信息」失败(静默): %s" % e, flush=True)
            return old
    return {}


def get_global_profile() -> dict:
    """「我的信息」（全角色共享桶）——**后端读路径的唯一入口**。

    桌面端是单用户库，这里就是用户填的那一份；多用户库里有多个候选时取最近更新的一行。
    以前 perception._get_city 直接硬编码 db.get_profile("default","default")，
    而前端从来不会往那个键写 → 永远读空（用户填的城市进了天气链路也是空的）。
    """
    rows = q(
        "SELECT * FROM user_profile WHERE character_id=? "
        "ORDER BY updated_time DESC, id DESC LIMIT 1",
        (GLOBAL_PROFILE_CHAR,),
        fetch=True,
    )
    return dict(rows[0]) if rows else {}


def get_profile(session_id="default", character_id="default"):
    # 「我的信息」是全角色共享的，键不能跟着"当前角色"漂移
    if str(character_id or "") == GLOBAL_PROFILE_CHAR:
        return _get_global_profile(session_id)
    return _profile_row(session_id, character_id)


def update_profile(
    session_id="default",
    character_id="default",
    **kwargs
):
    """原子 UPSERT 更新用户画像（按 session_id + character_id 隔离，修复多角色串写）。"""
    # 「我的信息」（character_id='default'）写到稳定桶：否则换角色后旧值读不回
    if str(character_id or "") == GLOBAL_PROFILE_CHAR:
        session_id = _profile_session(session_id)
    # ★ 唯一索引（只在首次执行，幂等）——防同角色多行
    _ensure_profile_unique_index()

    _fields = [
        "nickname", "personality", "occupation", "hobbies",
        "dislikes", "communication_style", "emotional_traits",
        "ext_json",
    ]
    _defaults = {f: "" for f in _fields}
    _defaults["ext_json"] = "{}"
    _provided = [f for f in _fields if f in kwargs]

    _cols = ["session_id", "character_id"] + _fields + ["updated_time"]
    _marks = ",".join(["?"] * len(_cols))
    _params = [session_id, character_id] + [kwargs.get(f, _defaults[f]) for f in _fields] + [_now()]

    if _provided:
        # ★ 2026-09-17 修（画像被空值抹掉）：
        #   原先是 `f"{f}=excluded.{f}"` —— 只要调用方传了这个键就无条件覆盖。
        #   而画像抽取（profile_manager）每轮都会把 7 个键**全传一遍**，模型这轮
        #   没有新信息就返回空串 → 空串原样写回，把上一轮辛苦抽到的昵称/爱好抹掉。
        #   真机实测：活跃会话 user_profile.id=17 的 nickname/occupation/hobbies
        #   长度全为 0，而 updated_time 就是刚刚 —— "画像一直在更新"其实一直在清空。
        #   现在：**空值 = 这轮没有这条信息**，保留库里已有的非空值；
        #   只有非空值才覆盖（想真正清空请走记忆库界面的删除，而不是靠模型返回空串）。
        _set = ",".join(
            f"{f}=CASE WHEN excluded.{f} IS NULL OR excluded.{f}='' "
            f"THEN user_profile.{f} ELSE excluded.{f} END"
            for f in _provided
        ) + ",updated_time=excluded.updated_time"
        _sql = (
            f"INSERT INTO user_profile ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET {_set}"
        )
    else:
        _sql = (
            f"INSERT INTO user_profile ({','.join(_cols)}) VALUES({_marks}) "
            f"ON CONFLICT(session_id, character_id) DO UPDATE SET updated_time=excluded.updated_time"
        )
    q(_sql, _params)


# ---------------- tasks（临时定时任务） ----------------

def add_task(character_name: str, task_type: str, trigger_time: str, content: str,
             session_id: str = "default", character_id: str = "default",
             source: str = "chat", idempotency_key: str = "") -> int:
    with _lock:
        init()
        if idempotency_key:
            old = _conn.execute(
                "SELECT id FROM tasks WHERE session_id=? AND character_id=? AND idempotency_key=? "
                "AND status IN ('pending','processing') LIMIT 1",
                (session_id, character_id, idempotency_key),
            ).fetchone()
            if old:
                return int(old[0])
        cur = _conn.execute(
            "INSERT INTO tasks (session_id, character_id, character_name, task_type, trigger_time, content, "
            "status, attempts, source, idempotency_key, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id or "default", character_id or "default", character_name, task_type,
             trigger_time, content, "pending", 0, source, idempotency_key, _now()))
        _conn.commit()
        return cur.lastrowid


def list_pending_tasks(character_name: str = None, session_id: str = None, character_id: str = None) -> list:
    if session_id is not None and character_id is not None:
        rows = q("SELECT * FROM tasks WHERE status='pending' AND session_id=? AND character_id=? ORDER BY trigger_time ASC",
                 (session_id, character_id), fetch=True)
    elif character_name:
        rows = q("SELECT * FROM tasks WHERE status='pending' AND character_name=? ORDER BY trigger_time ASC",
                 (character_name,), fetch=True)
    else:
        rows = q("SELECT * FROM tasks WHERE status='pending' ORDER BY trigger_time ASC", fetch=True)
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════
# ★ 2026-09-16 孤儿定时任务（用户报「提醒不触发」，真机查库定位）
#   两个真实形状（都在用户库里躺着，attempts=0，永远不会被触发）：
#     · id=23：character_id='default'（写入时角色名没解析出来），session 是活跃的；
#     · id=1 ：character_id='骨子'，但 session_id 是**换角色/重启前的旧 session**。
#   原因：调度器一个桶一个实例（scheduler.py:3820-3849），领取用
#   `list_pending_tasks(session_id=…, character_id=…)`，而它是**两个字段精确匹配**；
#   而且投递用的是**任务行自己的** session/character（scheduler.py:1911-1921）——
#   所以就算放宽查询领到了，不"归位"的话消息也会落进旧会话，用户在界面上看不到。
#   下面三个函数就是修这个：放宽领取面（同用户+同角色；本会话的无角色行）+
#   领取即归位 + 列表可见。安全边界写在 list_claimable_tasks 的 docstring 里。
# ══════════════════════════════════════════════════════════════════════════

def user_session_ids(session_id) -> list:
    """该 session 所属用户的全部 session_id（对外薄封装，给调度器领取孤儿任务用）。"""
    return _user_session_ids(session_id)


def _session_character(session_id) -> str:
    """这个 session 当初是给哪个角色建的（sessions 表里记着；查不到返回空串）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return ""
    try:
        rows = q("SELECT character_id FROM sessions WHERE session_id=? LIMIT 1", (sid,), fetch=True)
    except Exception:
        return ""
    return str(rows[0]["character_id"] or "").strip() if rows else ""


def _claimable(task: dict, sid: str, cid: str) -> bool:
    """一条 pending 任务能不能被 (sid, cid) 这个调度器领取 —— 规则集中在这里，便于审查。"""
    t_sid = str(task.get("session_id") or "").strip()
    t_cid = str(task.get("character_id") or "").strip()
    if t_cid == cid:
        return True                      # 同角色（当前会话或同用户的旧会话）
    if t_sid == sid and t_cid in ("", "default"):
        return True                      # 我这个会话里的"无角色"行（真机 id=23 的形状）
    if t_cid in ("", "default") and _session_character(t_sid) == cid:
        return True                      # 旧会话的无角色行，而那个会话本来就是给这个角色建的
    return False


def list_claimable_tasks(session_id: str, character_id: str) -> list:
    """这个调度器**可以**领取的 pending 任务（含孤儿行）。

    安全边界（三条，反例都在 tools/_verify_orphan_tasks.py 里钉着）：
      1) 只在本用户的 session 集合里找（`user_session_ids` 走 sessions 表的 user_id）——
         别人的 session 一律不碰（防跨用户串消息）；
      2) 跨 session 的行必须**角色相同**才领（防 A 角色替 B 角色发消息）；
      3) 本会话里的"无角色行"（character_id 为空/default）由当前活跃角色领 ——
         历史上就是这个语义（default 桶的任务成功发出过 2 条）。
    """
    sid = str(session_id or "").strip() or "default"
    cid = str(character_id or "").strip() or "default"
    sids = [sid]
    for s in _user_session_ids(sid):
        s = str(s or "").strip()
        if s and s not in sids:
            sids.append(s)
    ph = ",".join("?" for _ in sids)
    rows = q("SELECT * FROM tasks WHERE status='pending' AND session_id IN (%s) "
             "ORDER BY trigger_time ASC" % ph, tuple(sids), fetch=True)
    return [dict(r) for r in (rows or []) if _claimable(dict(r), sid, cid)]


def retask_bucket(task_id: int, session_id: str, character_id: str, character_name: str = "") -> bool:
    """把一条任务挪到给定桶（领取孤儿任务后调用，保证投递落在活跃会话里）。"""
    cur = q("UPDATE tasks SET session_id=?, character_id=?, character_name=? WHERE id=?",
            (str(session_id), str(character_id), str(character_name or character_id), int(task_id)))
    return bool(cur and cur.rowcount == 1)


def list_all_tasks(character_name: str = None, session_id: str = None, character_id: str = None) -> list:
    if session_id is not None and character_id is not None:
        # ★ 与 list_claimable_tasks 同一套判据：孤儿行也要**看得见**，
        #   否则用户根本不知道有这么一条（它以前既不触发也不显示）。
        rows = q("SELECT * FROM tasks WHERE session_id IN (%s) ORDER BY trigger_time DESC"
                 % ",".join("?" for _ in ([session_id] + [s for s in _user_session_ids(session_id)
                                                         if s and s != session_id])),
                 tuple([session_id] + [s for s in _user_session_ids(session_id)
                                       if s and s != session_id]), fetch=True)
        return [dict(r) for r in (rows or [])
                if _claimable(dict(r), str(session_id), str(character_id))]
    elif character_name:
        rows = q("SELECT * FROM tasks WHERE character_name=? ORDER BY trigger_time DESC",
                 (character_name,), fetch=True)
    else:
        rows = q("SELECT * FROM tasks ORDER BY trigger_time DESC", fetch=True)
    return [dict(r) for r in rows]


def update_task_status(task_id: int, status: str):
    q("UPDATE tasks SET status=? WHERE id=?", (status, task_id))


def cancel_task(task_id: int, session_id: str = None, character_id: str = None) -> bool:
    """取消一条**尚未触发**的定时任务（软取消：status='cancelled'，不删行）。

    ★ 2026-09-17 新增。为什么必须有：
      真机事故 —— 骨子给自己排了「明天 02:50 提醒睡觉」，用户连说取消**取消不掉**：
        · 聊天里说「取消」没有任何路径（取消只有 API `/api/pc/task/delete`）；
        · agent 自己的 `cancel_task` 工具只 UPDATE `open_loops`，**根本不碰 tasks 表**。
      两个入口都到不了这里，任务就只能一直 pending。

    只允许取消 pending（processing/done/failed 的不动，避免把正在兑现的抢掉）。
    返回是否真的改了行。

    注：查询待取消清单请用上面已有的 `list_pending_tasks()`
        （2026-09-17 我一度在这里又写了一个同名函数，被 test_static_guards 的
         重复定义守卫抓到 —— 后者会静默覆盖前者，已删除）。
    """
    try:
        if session_id is not None and character_id is not None:
            cur = q("UPDATE tasks SET status='cancelled' "
                    "WHERE id=? AND session_id=? AND character_id=? AND status='pending'",
                    (int(task_id), session_id, character_id))
        else:
            cur = q("UPDATE tasks SET status='cancelled' WHERE id=? AND status='pending'",
                    (int(task_id),))
        return bool(cur and cur.rowcount == 1)
    except Exception as e:  # noqa: BLE001
        print(f"[DB] 取消定时任务失败 id={task_id}: {e}", flush=True)
        return False


def claim_task(task_id: int) -> bool:
    """原子领取任务，防多个调度循环重复发送。"""
    cur = q("UPDATE tasks SET status='processing', attempts=attempts+1 WHERE id=? AND status='pending'", (task_id,))
    return bool(cur and cur.rowcount == 1)


def finish_task(task_id: int):
    q("UPDATE tasks SET status='done', fired_at=?, last_error='' WHERE id=?", (_now(), task_id))


def fail_task(task_id: int, error: str, retry: bool = True):
    status = "pending" if retry else "failed"
    q("UPDATE tasks SET status=?, last_error=? WHERE id=?", (status, str(error or "")[:500], task_id))


def delete_task(task_id: int, session_id: str = None, character_id: str = None):
    if session_id is not None and character_id is not None:
        q("DELETE FROM tasks WHERE id=? AND session_id=? AND character_id=?", (task_id, session_id, character_id))
    else:
        q("DELETE FROM tasks WHERE id=?", (task_id,))


# ---------------- kv ----------------

def kv_get(key: str):
    rows = q("SELECT value FROM kv WHERE key=?", (key,), fetch=True)
    return rows[0]["value"] if rows else None


def kv_set(key: str, value):
    q("INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, str(value)))

def kv_claim_once(key: str, value="generating") -> bool:
    cur = q("INSERT OR IGNORE INTO kv(key, value) VALUES(?,?)", (key, str(value)))
    return bool(cur and cur.rowcount == 1)

def kv_delete(key: str):
    q("DELETE FROM kv WHERE key=?", (key,))


# ---------------- feedback 反馈闭环辅助（v2.0） ----------------

def get_recent_memories(session_id: str = "default", character_id: str = "default", limit: int = 3):
    """最近有效记忆列表（学习写回用，最新在前）"""
    rows = valid_memories(session_id=session_id, character_id=character_id)
    if not rows:
        return []
    return list(rows[:limit])


def get_relationship_field(session_id: str, character_id: str = "default", field: str = "proactive_weight"):
    """读取 relationship 附加字段（kv 存储，避免动表结构）"""
    _v = kv_get(f"rel_field:{field}:{session_id}:{character_id}")
    if _v is None:
        return None
    try:
        return float(_v)
    except (TypeError, ValueError):
        return None


def set_relationship_field(session_id: str, character_id: str = "default", field: str = "proactive_weight", value=None):
    """写入 relationship 附加字段（kv 存储）"""
    kv_set(f"rel_field:{field}:{session_id}:{character_id}", value)
