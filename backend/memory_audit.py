# -*- coding: utf-8 -*-
"""记忆变更审计 + 一键撤回（2026-09-17）。

## 为什么必须有
用户拍板"允许 AI 自己整理记忆，但每次改动要留痕、可查、可一键撤回"。
记忆一旦被合并/作废/改写，如果只留结果不留过程，出问题就无法恢复 ——
而这个库里有 4384 条长期陪伴记忆，误删是不可接受的。

## 设计
· **永不真删**：整理只改 memory_status / is_valid / importance / memory_content，
  原始行始终在表里；本模块额外把"改动前后的完整快照"记进 memory_audit。
· **一次操作一行审计**：谁能撤回、撤回哪一条、恢复成什么，全部落库。
· **撤回是"写回"而不是"删除记录"**：撤回后把审计行标记 reverted，
  保留完整链条（谁在什么时候撤回了什么），避免审计表自己变成不可审计的东西。

审计表与 long_term_memory 同库（local_db.db），跟主库一起备份/迁移。
"""
import json
from datetime import datetime

from . import db

# 允许审计/撤回的记忆字段（撤回时按这些字段写回）
_MEM_COLS = (
    "memory_content", "importance", "is_valid", "memory_status", "confidence",
    "context", "emotion_tag", "source_text", "memory_type", "memory_scope",
    "decay_score",
)

_ENSURED = False


def _table_exists() -> bool:
    try:
        rows = db.q("SELECT name FROM sqlite_master WHERE type='table' AND name='memory_audit'",
                    fetch=True)
        return bool(rows)
    except Exception:
        return False


def _ensure_table():
    """建表（幂等）。

    ★ 2026-09-17 修：原先只用模块级 `_ENSURED` 标志，一次建表后再也不检查 ——
      而主库会被删掉重建（测试隔离、用户换库、备份恢复都算），
      此时标志仍是 True、表却不存在，于是所有审计写入静默失败
      （`no such table: memory_audit`，回归用例 test_merge_then_revert_* 抓到的）。
      现在标志为 True 时也**确认一次表是否真的在**（一次极轻的 sqlite_master 查询），
      不在就重建。审计写入是"永远不该静默丢"的东西，这点开销值得。
    """
    global _ENSURED
    if _ENSURED and _table_exists():
        return
    db.q("""
        CREATE TABLE IF NOT EXISTS memory_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT 'default',
            character_id TEXT NOT NULL DEFAULT 'default',
            memory_id INTEGER,
            op TEXT NOT NULL,               -- merge/forget/regrade/rewrite/rejudge/revert
            reason TEXT DEFAULT '',          -- 人话原因（展示给用户）
            source TEXT DEFAULT 'auto',      -- auto(后台整理) / manual(用户) / tool(自检脚本)
            before_json TEXT,                -- 改动前完整快照（NULL = 操作前不存在）
            after_json TEXT,                 -- 改动后完整快照（NULL = 操作后已不存在）
            revertible INTEGER DEFAULT 1,    -- 是否允许一键撤回
            reverted INTEGER DEFAULT 0,      -- 是否已被撤回
            reverted_at TEXT,
            batch_id TEXT DEFAULT ''         -- 同一次整理共用一个 batch_id
        )
    """)
    db.q("CREATE INDEX IF NOT EXISTS idx_memory_audit_scope "
         "ON memory_audit(session_id, character_id, id DESC)")
    db.q("CREATE INDEX IF NOT EXISTS idx_memory_audit_mem "
         "ON memory_audit(memory_id, id DESC)")
    _ENSURED = True


def snapshot(memory_id: int):
    """取一条记忆的完整快照（撤回用）。不存在返回 None。"""
    rows = db.q("SELECT * FROM long_term_memory WHERE id=?", (int(memory_id),), fetch=True)
    if not rows:
        return None
    return {k: rows[0][k] for k in rows[0].keys()}


def _restore_fields(snap: dict) -> dict:
    return {k: snap.get(k) for k in _MEM_COLS if k in snap}


def record(op: str, memory_id, *, session_id: str = "default",
           character_id: str = "default", reason: str = "", source: str = "auto",
           before: dict = None, after: dict = None, revertible: bool = True,
           batch_id: str = "") -> int:
    """写一条审计。返回审计 id（0 表示写失败，但绝不影响主流程）。"""
    try:
        _ensure_table()
        cur = db.q(
            """INSERT INTO memory_audit
               (ts, session_id, character_id, memory_id, op, reason, source,
                before_json, after_json, revertible, reverted, batch_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,0,?)""",
            (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
             str(session_id or "default"), str(character_id or "default"),
             int(memory_id) if str(memory_id or "").isdigit() else None,
             str(op or "unknown"), str(reason or ""), str(source or "auto"),
             json.dumps(before, ensure_ascii=False) if before is not None else None,
             json.dumps(after, ensure_ascii=False) if after is not None else None,
             1 if revertible else 0, str(batch_id or "")),
        )
        return int(getattr(cur, "lastrowid", 0) or 0)
    except Exception as e:  # noqa: BLE001
        print(f"[MemoryAudit] 审计写入失败(不影响主流程): {e}", flush=True)
        return 0


def recent(session_id: str = "default", character_id: str = "default",
           limit: int = 50, only_revertible: bool = False) -> list:
    """最近改动记录（前端"她整理了什么"用）。"""
    try:
        _ensure_table()
        sql = ("SELECT id, ts, memory_id, op, reason, source, revertible, reverted, "
               "reverted_at, batch_id, before_json, after_json FROM memory_audit "
               "WHERE session_id=? AND character_id=?")
        args = [session_id, character_id]
        if only_revertible:
            sql += " AND revertible=1 AND reverted=0"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = db.q(sql, args, fetch=True) or []
        out = []
        for r in rows:
            d = dict(r)
            for k in ("before_json", "after_json"):
                try:
                    d[k.replace("_json", "")] = json.loads(d.get(k) or "null")
                except Exception:
                    d[k.replace("_json", "")] = None
            out.append(d)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[MemoryAudit] 读取失败: {e}", flush=True)
        return []


def revert(audit_id: int) -> dict:
    """按审计行把记忆写回改动前的样子。

    返回 {"ok": bool, "why": str, "memory_id": int}
    · 合并类（before 有值、after 有值）：内容/重要度/状态全部写回
    · 作废类（before 有效、after 无效）：is_valid 写回 1、memory_status 写回 active
    · 新增类（before 为 None）：撤回 = 把这条标记为 invalid（不能真删，
      否则引用它的审计行会失去对象）
    """
    try:
        _ensure_table()
        rows = db.q("SELECT * FROM memory_audit WHERE id=?", (int(audit_id),), fetch=True)
        if not rows:
            return {"ok": False, "why": "审计记录不存在", "memory_id": None}
        a = dict(rows[0])
        if not a.get("revertible"):
            return {"ok": False, "why": "该操作标记为不可撤回", "memory_id": a.get("memory_id")}
        if a.get("reverted"):
            return {"ok": False, "why": "该操作已经撤回过了", "memory_id": a.get("memory_id")}

        mid = a.get("memory_id")
        if mid is None:
            return {"ok": False, "why": "审计行没有关联记忆 id", "memory_id": None}

        if not _revert_one(a):
            return {"ok": False, "why": "该审计行没有可恢复的快照", "memory_id": int(mid)}

        db.q("UPDATE memory_audit SET reverted=1, reverted_at=? WHERE id=?",
             (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), int(audit_id)))

        # 撤回后清检索缓存，避免旧内容还在 8 秒缓存里
        try:
            from . import memory_manager
            memory_manager.invalidate_memory_cache(a.get("session_id"), a.get("character_id"))
        except Exception:
            pass
        return {"ok": True, "why": "已恢复改动前状态", "memory_id": int(mid)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "why": f"撤回失败: {e}", "memory_id": None}


def _revert_one(a: dict) -> bool:
    """把单条审计写回（revert / revert_batch 共用）。"""
    mid = a.get("memory_id")
    if mid is None:
        return False
    before = json.loads(a.get("before_json") or "null")
    if before:
        fields = _restore_fields(before)
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        db.q(f"UPDATE long_term_memory SET {sets}, update_time=? WHERE id=?",
             list(fields.values()) + [datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), int(mid)])
        try:
            if "memory_content" in fields:
                from .memory.vector_store import upsert_vector
                from .memory.embedding import encode
                snap = snapshot(int(mid)) or {}
                vec = encode(str(fields["memory_content"]))
                if vec:
                    upsert_vector(memory_id=f"sql:{mid}",
                                  user_id=str(snap.get("session_id") or "default"),
                                  content=str(fields["memory_content"]),
                                  embedding=vec,
                                  metadata={
                                      "type": str(snap.get("memory_type") or "fact"),
                                      "importance": int(snap.get("importance") or 5),
                                      "character_id": str(snap.get("character_id") or "default"),
                                  },
                                  character_id=str(snap.get("character_id") or "default"))
        except Exception as _ve:  # noqa: BLE001
            print(f"[MemoryAudit] 撤回后向量同步失败(不影响撤回): {_ve}", flush=True)
    else:
        # 原本不存在（新增）→ 撤回即失效，不真删
        db.q("UPDATE long_term_memory SET is_valid=0, memory_status='reverted', "
             "update_time=? WHERE id=?",
             (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), int(mid)))
    return True


def revert_batch(batch_id: str) -> dict:
    """整批撤回（合并/整理是一次操作动多条，必须整批回滚才有意义）。

    返回 {"ok": bool, "n": 恢复条数, "why": str}
    """
    try:
        _ensure_table()
        if not str(batch_id or "").strip():
            return {"ok": False, "n": 0, "why": "缺少 batch_id"}
        rows = db.q("SELECT * FROM memory_audit WHERE batch_id=? AND reverted=0 "
                    "AND revertible=1 ORDER BY id", (str(batch_id),), fetch=True) or []
        if not rows:
            return {"ok": False, "n": 0, "why": "该批次没有可撤回的记录"}
        n = 0
        for r in rows:
            a = dict(r)
            try:
                if _revert_one(a):
                    n += 1
                    db.q("UPDATE memory_audit SET reverted=1, reverted_at=? WHERE id=?",
                         (datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), int(a["id"])))
            except Exception as e:  # noqa: BLE001
                print(f"[MemoryAudit] 批次内第 {a.get('id')} 条撤回失败: {e}", flush=True)
        try:
            from . import memory_manager
            memory_manager.invalidate_memory_cache()
        except Exception:
            pass
        return {"ok": n > 0, "n": n, "why": f"已整批恢复 {n} 条改动"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "n": 0, "why": f"整批撤回失败: {e}"}


def recent_batches(session_id: str = "default", character_id: str = "default",
                   limit: int = 20) -> list:
    """按批次汇总最近改动（前端一行 = 一次整理，带"整批撤回"按钮）。"""
    try:
        _ensure_table()
        rows = db.q(
            """SELECT batch_id, MIN(ts) AS ts, COUNT(*) AS n, MAX(op) AS op,
                      MAX(reason) AS reason, MAX(source) AS source,
                      SUM(CASE WHEN reverted=1 THEN 1 ELSE 0 END) AS reverted_n
               FROM memory_audit
               WHERE session_id=? AND character_id=? AND batch_id<>''
               GROUP BY batch_id ORDER BY MIN(id) DESC LIMIT ?""",
            (session_id, character_id, int(limit)), fetch=True) or []
        out = []
        for r in rows:
            d = dict(r)
            d["reverted"] = bool(d.get("n") and d.get("reverted_n") == d.get("n"))
            out.append(d)
        return out
    except Exception:
        return []


def summary(session_id: str = "default", character_id: str = "default") -> dict:
    """改动统计（前端展示"她整理了多少"）。"""
    try:
        _ensure_table()
        rows = db.q(
            """SELECT op, COUNT(*) AS n,
                      SUM(CASE WHEN reverted=1 THEN 1 ELSE 0 END) AS reverted
               FROM memory_audit WHERE session_id=? AND character_id=?
               GROUP BY op""",
            (session_id, character_id), fetch=True) or []
        return {r["op"]: {"n": int(r["n"] or 0), "reverted": int(r["reverted"] or 0)}
                for r in rows}
    except Exception:
        return {}
