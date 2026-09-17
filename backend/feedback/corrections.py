# -*- coding: utf-8 -*-
"""用户纠正（User Corrections）—— 让 AI 能被用户引导并真的改过来。

为什么单独一个模块，而不是塞进 ai_feedback：
    ai_feedback 的语义是「反馈统计」（喜欢/不喜欢、风格偏好），
    而纠正是**可执行的事实性知识**——"你以为是 X，其实是 Y"。
    它需要按主题去重（同一件事以最新纠正为准）、需要被反复注入 prompt，
    混在统计表里没法查，也会把统计口径搞乱。

数据来源：understanding（第一层 DeepSeek）识别出的 correction 字段。
所有异常一律静默——记住用户的话很重要，但不能因此拖垮聊天主流程。
"""
import json
import time

_TABLE = "user_corrections"
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
            ts TEXT NOT NULL,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            topic TEXT DEFAULT '',
            wrong TEXT DEFAULT '',
            right TEXT NOT NULL,
            user_quote TEXT DEFAULT '',
            scope TEXT DEFAULT 'other',
            confidence REAL DEFAULT 0,
            applied_count INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1
        )
        """)
        db.q(f"CREATE INDEX IF NOT EXISTS idx_corr_sess ON {_TABLE}(session_id, character_id)")
        db.q(f"CREATE INDEX IF NOT EXISTS idx_corr_topic ON {_TABLE}(session_id, character_id, topic)")
        _ready = True
        return True
    except Exception:
        return False


# ---- 纠正 ↔ 记忆 关联（审计与回滚用）----
#
# 纠正会真的去改记忆库（作废旧的 / 写入新的）。万一纠正本身记错了，
# 得能查出来改了什么、并能退回去。所以每次改动都记一条关联。

_LINK_TABLE = "correction_memory_link"
_link_ready = False


def _ensure_link_table():
    global _link_ready
    if _link_ready:
        return True
    try:
        from .. import db
        db.q(f"""
        CREATE TABLE IF NOT EXISTS {_LINK_TABLE}(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            session_id TEXT,
            character_id TEXT DEFAULT 'default',
            correction_id INTEGER,
            memory_id INTEGER,
            action TEXT,
            content TEXT
        )
        """)
        db.q(f"CREATE INDEX IF NOT EXISTS idx_cml_corr ON {_LINK_TABLE}(correction_id)")
        db.q(f"CREATE INDEX IF NOT EXISTS idx_cml_mem ON {_LINK_TABLE}(memory_id)")
        _link_ready = True
        return True
    except Exception:
        return False


def _link(correction_id: int, session_id: str, character_id: str,
          memory_id: int, action: str, content: str) -> None:
    """记一条「纠正改动了哪条记忆」。失败无所谓——关联只是审计用，不阻断主流程。"""
    try:
        if not correction_id or not memory_id or not _ensure_link_table():
            return
        from .. import db
        db.q(
            f"INSERT INTO {_LINK_TABLE}"
            f"(ts, session_id, character_id, correction_id, memory_id, action, content)"
            f" VALUES(?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), str(session_id or "")[:64],
             str(character_id or "")[:64], int(correction_id), int(memory_id),
             str(action or "")[:32], str(content or "")[:200]))
    except Exception:
        pass


def get_correction_links(correction_id: int) -> list:
    """审计：这条纠正动过哪些记忆。返回 [{memory_id, action, content, ts}]。"""
    try:
        if not _ensure_link_table():
            return []
        from .. import db
        rows = db.q(
            f"SELECT memory_id, action, content, ts FROM {_LINK_TABLE}"
            f" WHERE correction_id=? ORDER BY id",
            (int(correction_id),), fetch=True) or []
        return [dict(r) for r in rows]
    except Exception:
        return []


def rollback_correction(correction_id: int) -> dict:
    """回滚一条纠正对记忆库造成的改动。

    用于「纠正本身记错了」的情况：
      · action=invalidated 的记忆 → 恢复（is_valid 改回 1）
      · action=created 的记忆     → 作废（is_valid=0）
    最后把这条纠正本身也置为失效，避免它继续被注入。

    注意：只回滚记忆，不删关联记录——关联是审计凭证，要留着。
    """
    out = {"restored": 0, "removed": 0}
    try:
        if not _ensure_link_table():
            return out
        from .. import db
        _ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in get_correction_links(correction_id):
            mid = r.get("memory_id")
            act = r.get("action")
            try:
                if act == "invalidated":
                    # 撤销纠正 → 恢复记忆（状态也复位，否则会留下
                    # is_valid=1 但 status='corrected' 的新不一致）
                    db.q("UPDATE long_term_memory SET is_valid=1, "
                         "memory_status='active', update_time=? WHERE id=?",
                         (_ts, int(mid)))
                    out["restored"] += 1
                elif act == "created":
                    # ★ 2026-09-13：补写 memory_status。原先只置 is_valid=0，
                    #   结果这批记忆留在 status='active'，事后无法区分
                    #   "为什么失效"（线上实测 24 条这种不一致，全部来自本路径）。
                    db.q("UPDATE long_term_memory SET is_valid=0, "
                         "memory_status='corrected', update_time=? WHERE id=?",
                         (_ts, int(mid)))
                    out["removed"] += 1
            except Exception:
                continue
        deactivate_correction(correction_id)
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    return out


def save_correction(session_id: str, character_id: str, correction: dict,
                    user_text: str = "", confidence: float = 0) -> int:
    """存一条用户纠正。返回新记录 id，失败返回 0。

    只接受「有正确答案」的纠正（correction.right 非空）——
    只是抱怨"你说得不对"没有可学习的内容，存下来只会变成噪音。
    """
    try:
        if not correction or not correction.get("is_correction"):
            return 0
        right = str(correction.get("right") or "").strip()
        if not right:
            return 0
        if not _ensure_table():
            return 0

        from .. import db
        cur = db.q(
            f"INSERT INTO {_TABLE}"
            f"(ts, session_id, character_id, topic, wrong, right, user_quote, scope,"
            f" confidence, applied_count, is_active) VALUES(?,?,?,?,?,?,?,?,?,0,1)",
            (
                time.strftime("%Y-%m-%d %H:%M:%S"),
                str(session_id or "")[:64],
                str(character_id or "")[:64],
                str(correction.get("topic") or "")[:60],
                str(correction.get("wrong") or "")[:200],
                right[:200],
                str(user_text or "")[:200],
                str(correction.get("scope") or "other")[:32],
                float(confidence or 0),
            ),
        )
        _new_id = 0
        try:
            _new_id = int(cur.lastrowid or 0)
        except Exception:
            _new_id = 0

        # ★ 反向修正长期记忆：把说错的旧记忆作废，写入正确内容。
        #   只在 prompt 里盖住是不够的——记忆检索照样会把旧错误翻出来，
        #   而且哪天纠正记录被清掉，旧错误就复活了。
        #   失败只影响"记忆修正"，不影响这条纠正记录本身。
        if _new_id:
            # ★ 同主题自动覆盖：新纠正生效后，同 topic 的旧纠正一律失效。
            #   用户可能先后纠正过同一件事（"我叫顾圣豪" → "是顾圣豪不是顾圣浩"），
            #   只有最后一次是对的。旧记录仍留在库里可追溯，但不再注入 prompt。
            try:
                _topic = str(correction.get("topic") or "").strip()
                if _topic:
                    db.q(
                        f"UPDATE {_TABLE} SET is_active=0"
                        f" WHERE session_id=? AND character_id=? AND topic=? AND id<?",
                        (str(session_id or ""), str(character_id or ""),
                         _topic, _new_id))
            except Exception as _oe:
                print(f"[Correction] 旧纠正失效处理失败(静默): {_oe}", flush=True)

            try:
                _cor = dict(correction)
                _cor["user_quote"] = str(user_text or "")[:200]
                _applied = apply_to_memory(session_id, character_id, _cor,
                                           correction_id=_new_id)
                print(f"[Correction] 已同步到记忆库: "
                      f"作废 {len(_applied.get('removed') or [])} 条, "
                      f"写入={_applied.get('added')}, "
                      f"命中={_applied.get('matched_texts')}", flush=True)
            except Exception as _ae:
                print(f"[Correction] 同步到记忆库失败(静默): {_ae}", flush=True)
        return _new_id
    except Exception:
        return 0


def _compose_memory_text(correction: dict) -> str:
    """把一条纠正转成记忆文本。

    带 topic 的话写成「关于用户的名字：顾圣豪」，比光秃秃一个「顾圣豪」
    更容易被检索命中，也不容易和其它同名内容混淆。
    """
    topic = str(correction.get("topic") or "").strip()
    right = str(correction.get("right") or "").strip()
    if topic and right:
        return "关于%s：%s" % (topic, right)
    return right


def apply_to_memory(session_id: str, character_id: str, correction: dict,
                    min_score: float = 0.45, correction_id: int = 0) -> dict:
    """把纠正反向应用到长期记忆库。

    用户说"我叫顾圣豪不是圣浩"时，记忆库里很可能还躺着一条「用户叫圣浩」。
    只在 prompt 里盖住它是不够的——记忆检索仍会把它翻出来，
    而且删纠正记录后旧错误又会复活。所以这里真的去修记忆：

      ① 找出与「错误说法」冲突的旧记忆
      ② 软删除它们（is_valid=0，记录仍在可追溯）+ 清掉向量
      ③ 用 dedupe_insert 写入正确内容（自动去重 + 同步向量库）
      ④ 失效记忆缓存，让下一次对话立刻用上

    min_score 与 memory_manager.forget_matching 保持一致（0.45），
    沿用全项目同一套匹配口径，不另立标准。

    任何异常静默，失败只影响"记忆修正"，不影响纠正记录本身。
    """
    result = {"removed": [], "added": "", "matched_texts": []}
    try:
        right = str(correction.get("right") or "").strip()
        wrong = str(correction.get("wrong") or "").strip()
        if not right:
            return result

        from .. import db
        from .. import memory_manager as mm

        # ① 找冲突的旧记忆
        targets = []
        if wrong:
            for mem in db.valid_memories(session_id=session_id,
                                         character_id=character_id):
                content = str(mem.get("memory_content") or "")
                if not content:
                    continue
                # 与 forget_matching 同一口径：包含关系直接命中，否则算相似度
                score = 1.0 if (wrong in content or content in wrong) \
                    else mm._similarity(wrong, content)
                if score >= min_score:
                    targets.append((score, mem))

        # ② 作废旧的（软删除 + 清向量，避免"搜得到但已作废"的孤儿）
        for _score, mem in targets:
            mid = mem.get("id")
            try:
                db.delete_memory(mid)
                mm._delete_sql_vector(mid)
                result["removed"].append(mid)
                result["matched_texts"].append(str(mem.get("memory_content") or "")[:80])
                # 记下关联，便于审计与回滚
                _link(correction_id, session_id, character_id, mid,
                      "invalidated", str(mem.get("memory_content") or ""))
            except Exception:
                continue

        # ③ 写入正确的
        _want = _compose_memory_text(correction)
        result["added"] = mm.dedupe_insert(
            content=_want,
            memory_type="fact",
            importance=8,      # 用户亲自纠正的，权重给高一些
            session_id=session_id,
            character_id=character_id,
            source_text=str(correction.get("user_quote") or "")[:200],
        )
        # dedupe_insert 只返回动作名、不给 id，回查一次拿到它才能记关联
        _new_mem_id = 0
        try:
            for m in db.valid_memories(session_id=session_id, character_id=character_id):
                if str(m.get("memory_content") or "").strip() == _want:
                    _new_mem_id = m.get("id")
                    break
        except Exception:
            _new_mem_id = 0
        result["memory_id"] = _new_mem_id
        if _new_mem_id:
            _link(correction_id, session_id, character_id, _new_mem_id, "created", _want)

        # ④ 失效缓存
        try:
            mm.invalidate_memory_cache(session_id, character_id)
        except Exception:
            pass
    except Exception as e:
        result["error"] = "%s: %s" % (type(e).__name__, e)
    return result


def get_active_corrections(session_id: str, character_id: str = "default",
                           limit: int = 20, max_age_days: int = 0) -> list:
    """取当前生效的纠正，按主题去重（同一主题保留最新一条），最新的排前面。

    去重分两层，双保险：
      1. 保存时已把同 topic 的旧记录置为 is_active=0（见 save_correction）
      2. 这里取回来再按 topic 去重一次，防止早期数据没经过第 1 步

    max_age_days > 0 时只返回最近 N 天内的纠正——很久以前纠正过的
    （且期间用户没再提起）会自然淡出，避免"用户三年前随口纠正的一句"
    被永久当成铁律。**记录本身仍然保留**，只是不再注入。
    """
    try:
        if not _ensure_table():
            return []
        from .. import db
        sql = (
            f"SELECT id, ts, topic, wrong, right, user_quote, scope, confidence, applied_count"
            f" FROM {_TABLE}"
            f" WHERE session_id=? AND character_id=? AND is_active=1")
        args = [str(session_id or ""), str(character_id or "")]
        if max_age_days and max_age_days > 0:
            sql += " AND ts>=?"
            args.append(time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(time.time() - float(max_age_days) * 86400)))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit) * 3)
        rows = db.q(sql, tuple(args), fetch=True) or []
    except Exception:
        return []

    seen = set()
    out = []
    for r in rows:
        d = dict(r)
        topic = str(d.get("topic") or "").strip()
        key = topic or ("right:" + str(d.get("right") or "")[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
        if len(out) >= limit:
            break
    return out


def mark_applied(ids: list) -> None:
    """记录这些纠正被注入过一次（用于观察哪些真的在用）。失败无所谓。"""
    try:
        if not ids or not _ensure_table():
            return
        from .. import db
        for i in ids:
            try:
                db.q(f"UPDATE {_TABLE} SET applied_count = applied_count + 1 WHERE id=?",
                     (int(i),))
            except Exception:
                continue
    except Exception:
        pass


def build_corrections_prompt(session_id: str, character_id: str = "default",
                             limit: int = 8) -> str:
    """生成注入 prompt 的纠正块。

    措辞要点：
      · 说清"你之前以为是 X，其实是 Y"，让模型能定位自己错在哪
      · 强调"不要再犯"，而不只是"记住"
      · 提醒不要向用户提起被纠正过（否则很出戏）
    """
    try:
        # 纠正的有效期：0 = 永久。想让旧纠正自然淡出就配 CORRECTION_TTL_DAYS。
        _ttl = 0
        try:
            from .. import config
            _ttl = int(config.get("CORRECTION_TTL_DAYS") or 0)
        except Exception:
            _ttl = 0
        items = get_active_corrections(session_id, character_id, limit=limit,
                                       max_age_days=_ttl)
        if not items:
            return ""

        lines = ["【用户纠正过你（务必记住，同样的错不要再犯）】"]
        for it in items:
            topic = str(it.get("topic") or "").strip()
            wrong = str(it.get("wrong") or "").strip()
            right = str(it.get("right") or "").strip()
            if not right:
                continue
            if topic and wrong:
                lines.append(
                    f"· 关于「{topic}」：你之前以为是「{wrong}」，"
                    f"用户纠正过——应该是「{right}」。")
            elif topic:
                lines.append(f"· 关于「{topic}」：用户说过应该是「{right}」。")
            else:
                lines.append(f"· 用户纠正过你：应该是「{right}」。")

        if len(lines) <= 1:
            return ""

        lines.append("")
        lines.append(
            "这些是用户亲自教过你的，已经改过来就好，"
            "不要向用户提起「你纠正过我」，也不要反复道歉。")
        # 记录被应用
        mark_applied([it.get("id") for it in items if it.get("id")])
        return "\n".join(lines)
    except Exception:
        return ""


def correction_stats(session_id: str = "", character_id: str = "",
                     since_hours: int = 24) -> dict:
    """纠正统计，供 /api/understanding/stats 观察这套机制是否在起作用。"""
    out = {"total": 0, "active": 0, "by_scope": {}, "topics": []}
    try:
        if not _ensure_table():
            return out
        from .. import db
        where = []
        args = []
        if session_id:
            where.append("session_id=?")
            args.append(str(session_id))
        if character_id:
            where.append("character_id=?")
            args.append(str(character_id))
        if since_hours:
            where.append("ts>=?")
            args.append(time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - float(since_hours) * 3600)))
        w = (" WHERE " + " AND ".join(where)) if where else ""

        row = db.q(
            f"SELECT COUNT(*) AS n, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS act"
            f" FROM {_TABLE}{w}", tuple(args), fetch=True) or []
        if row:
            out["total"] = int(row[0]["n"] or 0)
            out["active"] = int(row[0]["act"] or 0)

        rows = db.q(
            f"SELECT scope, COUNT(*) AS n FROM {_TABLE}{w} GROUP BY scope",
            tuple(args), fetch=True) or []
        out["by_scope"] = {str(r["scope"] or "other"): int(r["n"]) for r in rows}

        trows = db.q(
            f"SELECT topic, wrong, right, COUNT(*) AS n, MAX(ts) AS last"
            f" FROM {_TABLE}{w} GROUP BY topic ORDER BY n DESC LIMIT 10",
            tuple(args), fetch=True) or []
        out["topics"] = [
            {"topic": r["topic"] or "(未命名)", "right": (r["right"] or "")[:60],
             "count": int(r["n"]), "last": r["last"]}
            for r in trows
        ]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def deactivate_correction(correction_id: int) -> bool:
    """作废一条纠正（比如用户后来又改口了，或纠正记错了）。"""
    try:
        if not _ensure_table():
            return False
        from .. import db
        db.q(f"UPDATE {_TABLE} SET is_active=0 WHERE id=?", (int(correction_id),))
        return True
    except Exception:
        return False


def clear_corrections(session_id: str, character_id: str = "default") -> int:
    """清空某会话下的纠正（调试/重置用）。返回删除条数。"""
    try:
        if not _ensure_table():
            return 0
        from .. import db
        db.q("DELETE FROM user_corrections WHERE session_id=? AND character_id=?",
             (str(session_id or ""), str(character_id or "")))
        return 1
    except Exception:
        return 0
