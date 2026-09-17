# -*- coding: utf-8 -*-
"""
情绪记忆模块 v1.0（适配现有项目）
复用「AI-Companion-Emotion」memory_emotion 的核心逻辑：
记录哪些话让 AI 开心过 / 伤害过它，同样的话再次触发时情绪反应更强（频率×强度放大）。
按 session_id + character_id 隔离（多角色独立）。
"""
import logging
from datetime import datetime
from typing import Optional, List, Dict, Tuple

logger = logging.getLogger(__name__)

# 触发词强度放大的上限
MAX_AMPLIFIER = 1.5


def _db_q():
    """返回 db.q 函数；db 不可用时返回 None（降级为无持久化）。"""
    try:
        from .db import q
        return q
    except Exception:
        return None


class EmotionMemory:
    """AI 情绪触发器记忆：记住哪些话让它开心/受伤，重复触发会强化情绪。"""

    def __init__(self):
        self._ensure_table()

    def _ensure_table(self):
        q = _db_q()
        if q is None:
            return
        try:
            q("""
                CREATE TABLE IF NOT EXISTS emotion_memories (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id     TEXT NOT NULL DEFAULT 'default',
                    character_id   TEXT NOT NULL DEFAULT 'default',
                    trigger_text   TEXT NOT NULL,
                    trigger_type   TEXT NOT NULL,
                    emotion_result TEXT NOT NULL,
                    intensity      REAL DEFAULT 0.5,
                    frequency      INTEGER DEFAULT 1,
                    last_triggered TEXT,
                    created_at     TEXT
                )
            """)
        except Exception as e:
            logger.error(f"[EmotionMemory] 建表失败: {e}")

    # ─────────────────────── 记录 ───────────────────────

    def record(self, session_id: str, character_id: str, trigger_text: str,
               trigger_type: str, emotion: str, intensity: float):
        """记录一次情绪触发。trigger_type: 'positive'/'negative'。"""
        q = _db_q()
        if q is None:
            return
        trigger_text = (trigger_text or "").strip()
        if not trigger_text:
            return
        now = datetime.now().isoformat()
        try:
            rows = q(
                "SELECT id, frequency, intensity FROM emotion_memories "
                "WHERE trigger_text=? AND trigger_type=? AND session_id=? AND character_id=?",
                (trigger_text, trigger_type, session_id, character_id),
                fetch=True,
            )
            if rows:
                r = rows[0]
                new_freq = int(r["frequency"] or 1) + 1
                new_intensity = min(1.0, float(r["intensity"] or 0.5) * 0.7 + float(intensity) * 0.3)
                q(
                    "UPDATE emotion_memories SET frequency=?, intensity=?, last_triggered=? WHERE id=?",
                    (new_freq, new_intensity, now, r["id"]),
                )
            else:
                q(
                    "INSERT INTO emotion_memories "
                    "(session_id, character_id, trigger_text, trigger_type, emotion_result, intensity, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (session_id, character_id, trigger_text, trigger_type, emotion, float(intensity), now),
                )
        except Exception as e:
            logger.error(f"[EmotionMemory] 记录失败: {e}")

    def record_positive(self, session_id, character_id, text, emotion="happy", intensity=0.6):
        self.record(session_id, character_id, text, "positive", emotion, intensity)

    def record_negative(self, session_id, character_id, text, emotion="hurt", intensity=0.6):
        self.record(session_id, character_id, text, "negative", emotion, intensity)

    # ─────────────────────── 查询触发 ───────────────────────

    def check_trigger(self, text: str, session_id: str, character_id: str) -> Optional[Tuple[str, float, str]]:
        """检查文本是否命中已知情绪记忆，返回 (trigger_type, amplified_intensity, emotion) 或 None。"""
        q = _db_q()
        if q is None:
            return None
        try:
            rows = q(
                "SELECT * FROM emotion_memories WHERE session_id=? AND character_id=? "
                "ORDER BY frequency DESC, intensity DESC",
                (session_id, character_id),
                fetch=True,
            ) or []
            for raw in rows:
                # db.q 返回 sqlite3.Row（没有 .get），先转 dict 再按字段访问
                mem = dict(raw)
                tt = str(mem.get("trigger_text") or "")
                if not tt:
                    continue
                if tt in text or text in tt:
                    freq = int(mem.get("frequency") or 1)
                    amplifier = min(MAX_AMPLIFIER, 1.0 + freq * 0.1)
                    amplified = min(1.0, float(mem.get("intensity") or 0.5) * amplifier)
                    return mem.get("trigger_type"), amplified, mem.get("emotion_result")
        except Exception as e:
            logger.error(f"[EmotionMemory] 查询触发失败: {e}")
        return None

    # ─────────────────────── Prompt 注入 ───────────────────────

    def get_format_for_prompt(self, session_id: str, character_id: str) -> str:
        """格式化为 prompt 提示（让我开心/受伤的话）。"""
        q = _db_q()
        if q is None:
            return ""
        lines = []
        try:
            pos = q(
                "SELECT * FROM emotion_memories WHERE trigger_type='positive' "
                "AND session_id=? AND character_id=? ORDER BY frequency DESC, intensity DESC LIMIT 3",
                (session_id, character_id), fetch=True,
            ) or []
            neg = q(
                "SELECT * FROM emotion_memories WHERE trigger_type='negative' "
                "AND session_id=? AND character_id=? ORDER BY frequency DESC, intensity DESC LIMIT 3",
                (session_id, character_id), fetch=True,
            ) or []
            # db.q 返回 sqlite3.Row（没有 .get），先转 dict 再按字段访问
            pos = [dict(r) for r in pos]
            neg = [dict(r) for r in neg]
            if pos:
                lines.append("【让我开心的事/话】")
                for m in pos:
                    lines.append(f"- {str(m.get('trigger_text',''))[:30]}（触发{m.get('frequency',1)}次）")
            if neg:
                lines.append("【曾经伤害过我的话】")
                for m in neg:
                    lines.append(f"- {str(m.get('trigger_text',''))[:30]}（我会更敏感）")
        except Exception as e:
            logger.error(f"[EmotionMemory] prompt 格式化失败: {e}")
        return "\n".join(lines)


# 全局单例
_emotion_memory = None


def get_emotion_memory() -> EmotionMemory:
    global _emotion_memory
    if _emotion_memory is None:
        _emotion_memory = EmotionMemory()
    return _emotion_memory
