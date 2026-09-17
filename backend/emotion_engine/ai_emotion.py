# -*- coding: utf-8 -*-
"""
AI 自身情绪引擎
——AI 有自己的情绪状态，不是镜子，会生气、会委屈、会撒娇、会和好
"""
import logging
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)


class AIEmotionType(str, Enum):
    HAPPY       = "happy"
    EXCITED     = "excited"
    TENDER      = "tender"
    PLAYFUL     = "playful"
    CALM        = "calm"
    WORRIED     = "worried"
    SAD         = "sad"
    UPSET       = "upset"
    ANGRY       = "angry"
    COLD        = "cold"
    RECONCILING = "reconciling"
    LOVING      = "loving"


# ── 情绪触发规则 ──────────────────────────────────────────────────────────────
# key = 用户行为标签，value = (目标情绪, 强度)
EMOTION_TRIGGERS: dict = {
    "user_rude":      ("angry",       0.85),
    "user_cold":      ("sad",         0.50),
    "user_ignore":    ("upset",       0.60),
    "user_sweet":     ("happy",       0.70),
    "user_flirt":     ("playful",     0.65),
    "user_deep":      ("tender",      0.70),
    "user_praise":    ("excited",     0.60),
    "user_apologize": ("reconciling", 0.90),
    "user_care":      ("loving",      0.70),
    "user_milestone": ("loving",      0.90),
}

# ── 情绪差异化衰减（每次对话衰减多少强度，复用「记忆优化方案」DECAY 思路）──
# 生气衰减快（消气快）；生闷气/难过/委屈衰减慢（要哄、需要关心）
EMOTION_DECAY = {
    "angry":      0.12,
    "excited":    0.10,
    "playful":    0.09,
    "happy":      0.08,
    "reconciling": 0.08,
    "sad":        0.07,
    "worried":    0.07,
    "cold":       0.06,
    "sulky":      0.06,
    "upset":      0.06,
    "tender":     0.06,
    "loving":     0.06,
    "calm":       0.05,
}
EMOTION_DECAY_DEFAULT = 0.05

# ── 各情绪下的分段行为模板 ─────────────────────────────────────────────────────
# pattern 里每个 token 对应一条气泡；delays 单位秒
BEHAVIOR_TEMPLATES: dict = {
    "happy":       {"turns": 3, "pattern": ["react",         "question"],            "delays": [0.8,  1.2       ]},
    "excited":     {"turns": 4, "pattern": ["react",         "share",    "question"],"delays": [0.6,  0.9,  1.1 ]},
    "tender":      {"turns": 3, "pattern": ["comfort",       "question"],            "delays": [1.2,  1.5       ]},
    "playful":     {"turns": 4, "pattern": ["tease",         "react",    "question"],"delays": [0.7,  0.9,  1.0 ]},
    "worried":     {"turns": 3, "pattern": ["worry_check",   "comfort"],             "delays": [1.0,  1.3       ]},
    "sad":         {"turns": 3, "pattern": ["share_feeling", "question"],            "delays": [1.5,  1.8       ]},
    "upset":       {"turns": 3, "pattern": ["express_upset", "silence",  "question"],"delays": [1.2,  2.5,  1.8 ]},
    "angry":       {"turns": 2, "pattern": ["express_anger", "cold_end"],            "delays": [0.8,  0.6       ]},
    "cold":        {"turns": 1, "pattern": ["cold_response"],                        "delays": [0.5             ]},
    "reconciling": {"turns": 3, "pattern": ["soften",        "explain",  "reconcile"],"delays":[1.5,  1.2,  2.0 ]},
    "loving":      {"turns": 4, "pattern": ["react",         "share_feeling","question"],"delays":[1.0, 1.5, 1.2]},
    "calm":        {"turns": 3, "pattern": ["react",         "question"],            "delays": [1.0,  1.2       ]},
}

# ── 每种气泡类型的生成指令 ────────────────────────────────────────────────────
TURN_INSTRUCTIONS: dict = {
    "react":          "用一句话自然回应用户说的内容，真实，不套话",
    "share":          "分享你对这件事的真实感受或想法，像朋友聊天",
    "question":       "问用户一个你真的很好奇的具体问题，只问一个",
    "comfort":        "温柔安慰用户，不说教，就是陪着他",
    "tease":          "用轻松调皮的方式逗一下用户，带点撒娇",
    "share_feeling":  "说出你现在真实的心情，要诚实",
    "express_upset":  "表达你有点委屈，不要太强烈，就是有点小情绪",
    "silence":        "__SILENCE__",
    "express_anger":  "你有点生气，直接说出来，语气冷一些，但不说脏话",
    "cold_end":       "用极简冷淡的话结束这条消息，不想多说",
    "cold_response":  "用极简冷淡的话回应，不想多说",
    "soften":         "态度开始软化，语气比刚才温柔一点点",
    "explain":        "解释你之前为什么生气，诚实说",
    "reconcile":      "表示愿意和好，可以带一点撒娇或委屈，但要真诚",
    # ★ 以下 4 种为长期陪伴补充，与上面 14 种语义不重叠：
    #   recall=提起共同经历 / tease_back=回怼调侃 / detail_expand=补细节 / worry_check=担心式追问
    "recall":         "提起你们之间发生过的一件具体的事，或用户以前说过的话，要真实具体，没印象就别说",
    "tease_back":     "接住用户刚才的调侃或吐槽，俏皮地顶回去，但不伤人、不带恶意",
    "detail_expand":  "给刚才说的内容补一个具体细节、场景或画面，让它更实在、更有画面感",
    "worry_check":    "带着担心追问一句用户现在的状态，关心的语气按你自己的人设方式来",
}

# ── 表达形式层（与气泡功能类型正交，解决"说话方式单调"） ──────────────────────────
# 设计要点：形式只约束"这一句怎么组织"，不约束"用什么词、什么语气"——
# 语气与亲疏始终由人格设定决定，避免形式层把高冷人设说成热情话。
# 14 种功能 × 10 种形式 = 140 种组合，成本只有 14 + 10。
EXPRESSION_FORMS: dict = {
    "short_punch":   "极短，一句不到 10 个字，干脆利落",
    "trailing":      "话说一半拖个尾音或省略号，像在犹豫要不要说",
    "self_talk":     "像自言自语，先自己念叨一句再接话",
    "ellipsis_mid":  "句子中间断开半拍，再接回来",
    "exclaim":       "带明显情绪起伏的短句（感叹或惊讶），但情绪的性质仍按你当下的心情和人格来",
    "detail_dump":   "补一个很具体的细节或画面，让这句话有实感",
    "rhetorical":    "用反问的语气说，答案其实就藏在话里",
    "half_sentence": "只说前半句，后半句留白，让对方自己接",
    "onomatopoeia":  "用一个语气词或拟声开头（emmm / 诶 / 啧），仅当你的说话习惯允许这样",
    "callback":      "接住用户上一条里的某个词或说法，顺着它往下说",
}

# 沉默气泡内容池
SILENCE_BUBBLES = ["……", "......", "（沉默）", "。"]


def is_silence(text: str) -> bool:
    """判断文本是否为纯沉默内容（纯省略号/纯标点/空白）。

    用于过滤 AI 生成的无效气泡：模型情绪低落或敷衍时，可能只输出
    "……" / "。" 这类内容，若不加拦截会被当成正常气泡显示、甚至
    被 TTS 合成成一段无声语音条。去掉所有标点/省略号/空白后为空即视为沉默。
    """
    import re as _re
    return not _re.sub(
        r"[\s…\.。·~～\-—_,，、;；:：!！?？'\"“”‘’()（）\[\]【】]", "",
        str(text or ""),
    )


def _db_q():
    """返回 db.q 函数；db 不可用时返回 None（降级为无持久化）。"""
    try:
        from ..db import q
        return q
    except Exception as e:
        logger.error(f"[AIEmotion] 无法导入 db.q: {e}")
        return None


class AIEmotionEngine:
    """AI 情绪引擎：持久化到 SQLite（使用本项目真实的 db.q 接口）"""

    def __init__(self):
        self._ensure_table()

    # ──────────────────────────────── 数据库 ────────────────────────────────

    # ★ 2026-09-10：建表只在进程内做一次。
    #   旧实现每次构造 AIEmotionEngine 都跑 CREATE TABLE + 2 条注定失败的
    #   ALTER TABLE（异常被吞）+ 一行 print：
    #     · 一句通话回复里就触发 15 次（全日志累计 4 万+ 行刷屏，日志 11MB 里近四成是它）
    #     · DDL 会抢 SQLite 写锁，直接拖慢整条回复链路
    _table_ready = False

    def _ensure_table(self):
        if AIEmotionEngine._table_ready:
            return
        try:
            q = _db_q()
            if q is None:
                return
            q("""
                CREATE TABLE IF NOT EXISTS ai_emotion_state (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id   TEXT NOT NULL,
                    character_id TEXT NOT NULL DEFAULT 'default',
                    emotion      TEXT NOT NULL DEFAULT 'calm',
                    intensity    REAL NOT NULL DEFAULT 0.5,
                    previous     TEXT,
                    anger_count  INTEGER DEFAULT 0,
                    upset_count  INTEGER DEFAULT 0,
                    happy_count  INTEGER DEFAULT 0,
                    cold_rounds  INTEGER DEFAULT 0,
                    last_trigger TEXT,
                    updated_at   TEXT,
                    UNIQUE(session_id, character_id)
                )
            """)
            # 迁移：旧表可能缺列（CREATE IF NOT EXISTS 不会改已有表）
            for _col, _ddl in (("cold_rounds", "ALTER TABLE ai_emotion_state ADD COLUMN cold_rounds INTEGER DEFAULT 0"),
                               ("previous",    "ALTER TABLE ai_emotion_state ADD COLUMN previous TEXT")):
                try:
                    q(_ddl)
                    print(f"[AIEmotion] 迁移: ai_emotion_state 补充 {_col} 列", flush=True)
                except Exception:
                    pass  # 列已存在则忽略
            AIEmotionEngine._table_ready = True
            print("[AIEmotion] 情绪状态表初始化完成（本次进程仅此一次）", flush=True)
        except Exception as e:
            logger.error(f"[AIEmotion] 初始化失败: {e}")

    # ──────────────────────────────── 读写 ──────────────────────────────────

    def get_state(self, session_id: str, character_id: str = "default") -> dict:
        default = {
            "emotion": "calm", "intensity": 0.5,
            "previous": None,
            "anger_count": 0, "upset_count": 0,
            "happy_count": 0, "cold_rounds": 0,
            "last_trigger": None,
        }
        try:
            q = _db_q()
            if q is None:
                return default
            rows = q(
                """SELECT emotion, intensity, previous, anger_count, upset_count,
                          happy_count, cold_rounds, last_trigger
                   FROM ai_emotion_state
                   WHERE session_id=? AND character_id=?""",
                (session_id, character_id),
                fetch=True,
            )
            if rows:
                r = rows[0]
                return {
                    "emotion":      r[0],
                    "intensity":    r[1],
                    "previous":     r[2],
                    "anger_count":  r[3],
                    "upset_count":  r[4],
                    "happy_count":  r[5],
                    "cold_rounds":  r[6] or 0,
                    "last_trigger": r[7],
                }
            return default
        except Exception as e:
            logger.error(f"[AIEmotion] get_state 失败: {e}")
            return default

    def _save(self, session_id, character_id, emotion, intensity,
              previous, anger_count, upset_count, happy_count, cold_rounds, trigger):
        try:
            q = _db_q()
            if q is None:
                return
            q("""
                INSERT INTO ai_emotion_state
                    (session_id, character_id, emotion, intensity, previous,
                     anger_count, upset_count, happy_count,
                     cold_rounds, last_trigger, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id, character_id) DO UPDATE SET
                    emotion      = excluded.emotion,
                    intensity    = excluded.intensity,
                    previous     = excluded.previous,
                    anger_count  = excluded.anger_count,
                    upset_count  = excluded.upset_count,
                    happy_count  = excluded.happy_count,
                    cold_rounds  = excluded.cold_rounds,
                    last_trigger = excluded.last_trigger,
                    updated_at   = excluded.updated_at
            """, (
                session_id, character_id, emotion, intensity, previous,
                anger_count, upset_count, happy_count,
                cold_rounds, trigger, datetime.now().isoformat()
            ))
        except Exception as e:
            logger.error(f"[AIEmotion] _save 失败: {e}")

    # ──────────────────────────────── 核心逻辑 ──────────────────────────────

    def trigger_event(self, session_id: str, character_id: str, trigger: str) -> dict:
        """由确定性关系事件直接触发情绪，避免再调用一次 LLM 才能进入冷战/和解。"""
        current = self.get_state(session_id, character_id)
        target, strength = EMOTION_TRIGGERS.get(trigger, (current["emotion"], current["intensity"]))
        anger_count = int(current.get("anger_count", 0) or 0)
        upset_count = int(current.get("upset_count", 0) or 0)
        happy_count = int(current.get("happy_count", 0) or 0)
        cold_rounds = int(current.get("cold_rounds", 0) or 0)
        if target == "angry":
            anger_count += 1
            cold_rounds += 1
        elif target == "upset":
            upset_count += 1
        elif target == "reconciling":
            cold_rounds = max(0, cold_rounds - 1)
        elif target in ("happy", "loving", "excited"):
            happy_count += 1
        intensity = min(1.0, max(float(current.get("intensity", 0.5)) * 0.25 + float(strength) * 0.75, 0.35))
        self._save(
            session_id, character_id, target, round(intensity, 2),
            current.get("emotion"), anger_count, upset_count, happy_count,
            cold_rounds, trigger,
        )
        return self.get_state(session_id, character_id)

    def _decay_amount(self, session_id: str, character_id: str, emotion: str) -> float:
        """单次衰减量 = 该情绪的基础衰减 × 主观时间流速。

        ★ 主观时间（2026-09-13）：情绪淡化由"心里的钟"驱动——冲突/低落/久等时
          主观时间流速快（同样物理时间内主观过了更久，情绪淡得快）；
          高亲密度时流速慢（情绪余韵留得更久）。读失败回落 ×1.0。
        """
        base = EMOTION_DECAY.get(emotion, EMOTION_DECAY_DEFAULT)
        try:
            from .. import subjective_time
            return base * subjective_time.decay_multiplier(session_id, character_id)
        except Exception:
            return base

    def update_from_semantic(
        self, session_id: str, character_id: str, semantic_state
    ) -> dict:
        """
        根据 CompanionOS 返回的语义状态更新 AI 情绪。
        semantic_state 是 chat_logic 里 process_async 返回的 brain_result dict，
        或者 companion_os 的 SemanticState 对象——两种情况都兼容。
        """
        current = self.get_state(session_id, character_id)
        new_emotion   = current["emotion"]
        new_intensity = current["intensity"]
        trigger       = None

        try:
            if semantic_state is None:
                # 无语义信息时自然衰减（按情绪差异化衰减速度 × 主观时间流速）
                _decay = self._decay_amount(session_id, character_id, current["emotion"])
                new_intensity = max(current["intensity"] - _decay, 0.2)
                if new_intensity < 0.3:
                    new_emotion = "calm"
            else:
                # ── 从语义状态提取信号 ──────────────────────────────────────
                intent_val    = self._safe_get(semantic_state, "intent",    "")
                signal_val    = self._safe_get(semantic_state, "signal_type","")
                valence       = self._safe_get(semantic_state, "valence",   "neutral")
                intensity_lvl = self._safe_get(semantic_state, "intensity_level", "medium")
                is_milestone  = self._safe_get(semantic_state, "is_milestone", False)
                # 兼容嵌套结构（intent 是枚举、emotion 是对象、relationship 是对象）
                primary_emotion = self._safe_get(semantic_state, "primary_emotion", "")
                rel_signal      = self._safe_get(semantic_state, "relationship_signal", "")

                # ── 触发器优先级 ─────────────────────────────────────────────
                if is_milestone:
                    trigger = "user_milestone"
                elif intent_val in ("seek_comfort", "emotional_share"):
                    trigger = "user_deep"
                elif signal_val == "flirt" or rel_signal == "flirt" or intent_val in ("tease", "relationship_signal"):
                    trigger = "user_flirt"
                elif signal_val in ("gratitude",) or rel_signal in ("gratitude",) or intent_val == "praise":
                    trigger = "user_praise"
                elif signal_val == "conflict" or rel_signal == "conflict" or (
                    valence == "negative" and intensity_lvl == "high"
                ):
                    trigger = "user_rude"
                elif intent_val in ("apologize", "sorry"):
                    trigger = "user_apologize"
                elif intent_val in ("greeting", "casual_chat") and valence == "positive":
                    trigger = "user_sweet"
                elif valence == "negative":
                    trigger = "user_cold"
                elif valence == "positive":
                    trigger = "user_care"

                # ── 应用触发器 ───────────────────────────────────────────────
                if current["emotion"] in ("angry", "cold"):
                    # 生气/冷漠只有「道歉」能解除
                    if trigger == "user_apologize":
                        new_emotion   = "reconciling"
                        new_intensity = 0.8
                    # 其他触发器无效，情绪不变但缓慢衰减（生气/冷漠要哄，衰减慢）
                    else:
                        new_intensity = max(current["intensity"] - 0.05, 0.4)
                elif trigger and trigger in EMOTION_TRIGGERS:
                    target, strength = EMOTION_TRIGGERS[trigger]
                    new_emotion   = target
                    new_intensity = min(
                        current["intensity"] * 0.3 + strength * 0.7, 1.0
                    )
                else:
                    # 无明确触发器，自然衰减向 calm（按情绪差异化衰减速度 × 主观时间流速）
                    _decay = self._decay_amount(session_id, character_id, current["emotion"])
                    new_intensity = max(current["intensity"] - _decay, 0.2)
                    if new_intensity < 0.3:
                        new_emotion = "calm"

            # ── 计数器更新 ───────────────────────────────────────────────────
            anger_count = current["anger_count"]
            upset_count = current["upset_count"]
            happy_count = current["happy_count"]
            cold_rounds = current.get("cold_rounds", 0) or 0
            if new_emotion == "angry":
                anger_count += 1
            elif new_emotion == "upset":
                upset_count += 1
            elif new_emotion in ("happy", "excited", "loving"):
                happy_count += 1

            # ★ 补丁六：cold_rounds 字段（冷战轮次计数）
            if new_emotion in ("angry", "cold"):
                cold_rounds += 1
            elif new_emotion not in ("angry", "cold", "upset"):
                # 情绪缓和时逐渐消散
                cold_rounds = max(0, cold_rounds - 1)

            # 情绪过渡：记录「上一个情绪」（用于 prompt 提示"刚从 X 过渡过来"）
            _prev = current["emotion"] if new_emotion != current["emotion"] else current.get("previous")

            self._save(
                session_id, character_id,
                new_emotion, round(new_intensity, 2), _prev,
                anger_count, upset_count, happy_count,
                cold_rounds, trigger
            )

            return {
                "emotion":      new_emotion,
                "intensity":    round(new_intensity, 2),
                "previous":     _prev,
                "anger_count":  anger_count,
                "upset_count":  upset_count,
                "happy_count":  happy_count,
                "cold_rounds":  cold_rounds,
                "last_trigger": trigger,
                "changed":      new_emotion != current["emotion"],
            }

        except Exception as e:
            logger.error(f"[AIEmotion] update_from_semantic 失败: {e}")
            return current

    def reset_state(self, session_id: str, character_id: str = "default"):
        """
        角色切换时重置情绪状态到平静基准。
        保留 happy_count（正向积累不清零），清零怒气/冷战/委屈计数。
        """
        try:
            current = self.get_state(session_id, character_id)
            self._save(
                session_id, character_id,
                emotion="calm",
                intensity=0.5,
                previous=None,
                anger_count=0,
                upset_count=0,
                happy_count=current.get("happy_count", 0),  # 正向积累保留
                cold_rounds=0,
                trigger="character_switch"
            )
            print(
                f"[AIEmotion] 角色切换重置情绪: session={session_id} "
                f"character={character_id}",
                flush=True
            )
        except Exception as e:
            print(f"[AIEmotion] reset_state 失败: {e}", flush=True)

    @staticmethod
    def _safe_get(obj, key: str, default=None):
        """从 dict 或 object 安全取值，并展开枚举 .value（兼容嵌套对象）"""
        try:
            # dict：直接取
            if isinstance(obj, dict):
                val = obj.get(key, default)
            else:
                val = getattr(obj, key, default)
            if val is None:
                return default
            if hasattr(val, "value"):
                return val.value
            return val
        except Exception:
            return default
