# -*- coding: utf-8 -*-
"""
MultiTurn Generator
根据 AI 情绪把一次回复拆成多条气泡，逐条调用 LLM 生成
"""
import asyncio
import logging
import random
from typing import List

logger = logging.getLogger(__name__)

EMOTION_DESCS = {
    "happy":       "你现在心情很好，回复轻松愉快",
    "excited":     "你现在很兴奋，回复充满活力",
    "tender":      "你现在很温柔，回复轻柔关怀",
    "playful":     "你现在想撒娇，回复俏皮",
    "worried":     "你现在有点担心，回复带着关心",
    "sad":         "你现在有点难过，回复低沉真实",
    "upset":       "你现在有点委屈，回复带着小情绪",
    "angry":       "你现在生气了，语气冷硬",
    "cold":        "你现在很冷漠，回复极简",
    "reconciling": "你的情绪在慢慢缓和，语气开始软化",
    "loving":      "你现在很有爱意，回复温柔深情",
    "calm":        "你现在平静，回复自然",
}


# ── 多段生成辅助：模型调用封装 / 重复检测 / 话题大纲 / 表达形式去味 ─────────────────

async def _gen_one(chat_once_fn, model, messages, api_key,
                   temperature=0.88, freq=0.3, pres=0.3, max_tokens=350,
                   base_url=""):
    """调用 LLM 生成一条气泡；同时兼容同步与异步的 chat_once。异常交给调用方处理。"""
    if asyncio.iscoroutinefunction(chat_once_fn):
        return await chat_once_fn(
            model, messages, api_key,
            temperature=temperature, max_tokens=max_tokens,
            frequency_penalty=freq, presence_penalty=pres,
            base_url=base_url,
        )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: chat_once_fn(
            model, messages, api_key,
            temperature=temperature, max_tokens=max_tokens,
            frequency_penalty=freq, presence_penalty=pres,
            base_url=base_url,
        ),
    )


def _extract_stream_delta(line):
    """解析 stream_chat 的 SSE 行，返回 (is_reasoning, text) 或 None。"""
    import json as _json
    raw = str(line or "").strip()
    if raw.startswith("data:"):
        raw = raw[5:].strip()
    if not raw:
        return None
    try:
        data = _json.loads(raw)
    except Exception:
        return None
    delta = ((data.get("choices") or [{}])[0].get("delta") or {})
    reasoning = delta.get("reasoning_content")
    content = delta.get("content")
    if reasoning:
        return (True, reasoning)
    if content:
        return (False, content)
    return None


def _norm_for_sim(text: str) -> str:
    r"""归一化文本用于相似度比对：剥离标点、空白与下划线。

    Python 的 \w 在 Unicode 模式下包含汉字，所以 [\s\W_] 只去掉符号与空白，保留字。
    """
    import re as _re
    return _re.sub(r"[\s\W_]+", "", str(text or ""), flags=_re.UNICODE).lower()


def _too_similar(new_text: str, existing: list, threshold: float = 0.72) -> bool:
    """判断新气泡是否与本次已发出的气泡重复（换说法重复 / 整体包含）。

    这是 prompt 软约束之外的硬性兜底：模型偶尔仍会把上一句换个说法再说一遍，
    光靠"不要重复前面说的话"这类指令压不住。
    """
    from difflib import SequenceMatcher
    n = _norm_for_sim(new_text)
    if not n:
        return False
    for old in (existing or [])[-6:]:
        o = _norm_for_sim(old)
        if not o:
            continue
        # 子串包含是强信号（短句被长句整体包住 = 明确重复），长度门槛放宽到 4 字；
        # 门槛再低会误伤"我知道""好吧"这类正常短回复。
        if len(n) >= 4 and (n in o or o in n):
            return True
        # 相似度比值对短文本很不稳定，要求两边都够长才比。
        # 0.72 能拦住"我今天去超市买了点水果/买了点菜"这种同句式换词的重复，
        # 又不会误伤语义不同但共享常用字的正常对话。
        if len(n) >= 6 and len(o) >= 6:
            if SequenceMatcher(None, n, o).ratio() >= threshold:
                return True
    return False


def _internal_duplicate(text: str) -> bool:
    """检测单条气泡内部的重复（LLM 在同一条消息里把同一句说了两遍）。

    这是 _too_similar 拦不住的场景：那条只比「这条 vs 之前的条」，
    而模型偶尔会在一次生成里自己复读，如「逗你玩呢……收回才舍不得呢。逗你玩呢……收回才舍不得呢」。
    """
    import re as _re
    from difflib import SequenceMatcher as _SM
    t = str(text or "").strip()
    if len(t) < 20:
        return False
    # ① 句子级重复：按句末标点切句，有相同/高度相似的句子即判重复
    sents = [s.strip('…~～.。') for s in _re.split(r'[。！？!?…～~]+', t) if s.strip('…~～.。').strip()]
    if len(sents) >= 2:
        for i in range(len(sents)):
            for j in range(i + 1, len(sents)):
                a, b = sents[i], sents[j]
                if a == b:
                    return True
                if len(a) >= 5 and len(b) >= 5 and _SM(None, a, b).ratio() >= 0.9:
                    return True
    # ② 整段兜底：前后半段高度相似（整段复读两遍）
    n = _norm_for_sim(t)
    half = len(n) // 2
    if half >= 8 and _SM(None, n[:half], n[half:]).ratio() >= 0.82:
        return True
    return False


def _split_into_turns(text: str, max_turns: int) -> list:
    """把一段完整回复按换行/句末标点拆成多个气泡。

    一次生成的内容天然连贯不重复，拆句后每个短句是一个独立气泡，
    既保留后端生成的质量，又像真人连发几条微信，且不会重复。
    """
    import re as _re
    t = str(text or "").strip()
    if not t:
        return []
    # 优先按换行拆（prompt 要求模型每句换行）
    parts = [p.strip() for p in t.split("\n") if p.strip()]
    if len(parts) <= 1:
        # 没换行就按句末标点拆
        parts = [p.strip() for p in _re.split(r'(?<=[。！？!?…])', t) if p.strip()]
    if not parts:
        return [t]
    # 控制数量：多了合并到最后一段（不丢内容，避免回复被截断）
    # 与 onebot._split_reply 的合并策略保持一致，修复 once 模式回复被静默截断
    if len(parts) > max_turns:
        parts = parts[:max_turns - 1] + ["".join(parts[max_turns - 1:])]
    return parts


# 用户意图 → 给模型的中文说明。
# 模型看不到"用户这句话到底是来干嘛的"时，只能顺着历史模式猜：
# 以前聊到"想我"之后常说晚安，这次就也回晚安，答非所问。
INTENT_DESC = {
    "greeting":           "在跟你打招呼",
    "farewell":           "在道别 / 要走了",
    "emotional_share":    "在跟你分享心情",
    "seek_comfort":       "想被安慰",
    "seek_advice":        "想听你的建议",
    "casual_chat":        "随口闲聊",
    "task_request":       "让你做一件事",
    "question":           "在问你一个问题，需要正面回答",
    "complaint":          "在抱怨",
    "praise":             "在夸你",
    "tease":              "在撒娇 / 调侃你",
    "self_disclosure":    "在跟你说自己的事",
    "relationship_signal": "在表达对你的感情",
}


_TOPIC_OUTLINE_PROMPT = (
    "你是对话规划助手。根据用户最后说的话和上下文，规划 AI 这一轮要聊的话题。\n"
    "要求：\n"
    "1. 只输出一个 JSON，不要任何解释文字、不要 markdown 代码块："
    "{\"topics\": [\"话题1\", \"话题2\"]}\n"
    "2. 话题必须围绕**用户正在说的这件事**深入展开，不要另起无关话题。\n"
    "3. 第一个话题固定是「回应用户刚说的事」；后面的话题必须是一条纵深链，"
    "按「反应 → 感受 → 细节 → 联想」在同一件事上层层往下挖，"
    "不要列几个并列无关的话题（那就是报菜名）。\n"
    "4. 每个话题不超过 12 个字，最多 3 个、最少 1 个。\n"
    "5. 用户只是敷衍应付（嗯、哦、好、哈哈哈）时只输出 1 个话题。\n"
)


def _parse_topics(raw: str, max_n: int) -> list:
    """解析大纲模型返回的 JSON；任何异常都返回 []，静默降级为无大纲。"""
    import json as _json
    import re as _re
    if not raw:
        return []
    try:
        m = _re.search(r"\{[^{}]*\}", str(raw), _re.S)
        if not m:
            return []
        data = _json.loads(m.group(0))
        items = data.get("topics") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out = []
        for it in items:
            s = str(it or "").strip()
            if s and s not in out:
                out.append(s[:24])
            if len(out) >= max_n:
                break
        return out
    except Exception:
        return []


def _load_recent_forms(session_id: str, character_id: str, limit: int = 8) -> list:
    """读取最近用过的表达形式（去味用）；失败返回 []。"""
    try:
        from .. import db as _db
        import json as _json
        raw = _db.kv_get(f"multiturn_recent_forms:{session_id or 'default'}:{character_id or 'default'}")
        if not raw:
            return []
        v = _json.loads(raw)
        return [str(x) for x in v][-limit:] if isinstance(v, list) else []
    except Exception:
        return []


def _save_recent_forms(session_id: str, character_id: str, forms: list, keep: int = 14):
    """记录最近用过的表达形式；失败静默忽略。"""
    try:
        from .. import db as _db
        import json as _json
        _db.kv_set(
            f"multiturn_recent_forms:{session_id or 'default'}:{character_id or 'default'}",
            _json.dumps([str(x) for x in (forms or [])][-keep:], ensure_ascii=False),
        )
    except Exception:
        pass


def _pick_form(pool: list, used_recent: list) -> str:
    """抽一个表达形式，优先避开最近用过的几个（避免连续几轮同一种腔调）。"""
    if not pool:
        return ""
    avoid = set((used_recent or [])[-3:])
    fresh = [f for f in pool if f not in avoid]
    return random.choice(fresh or pool)


class MultiTurnGenerator:

    def _chat_once(self):
        try:
            from ..deepseek_api import chat_once
            return chat_once
        except ImportError:
            return None

    def _model(self, override: str = ""):
        # ★ 单角色大脑跟随：调用方显式传入当前对话模型时优先用；否则回退全局。
        #   否则好感度≥85（走 /api/chat/stream）时，前端单角色模型传不进来，
        #   生成层永远 fallback 到全局 deepseek-chat。
        if override:
            return override
        try:
            from ..chat_logic import pick_model
            return pick_model(None, True)
        except Exception:
            return "deepseek-chat"

    def _api_key(self, override: str = ""):
        try:
            from ..config import api_key_for_model
            return api_key_for_model(self._model(override))
        except Exception:
            return ""

    def _base_url(self, override: str = "", base_url_override: str = ""):
        """根据当前模型的 provider 返回接口 base_url；未知 provider 返回空（用 DeepSeek 默认）。
        调用方显式传入 base_url_override 时优先用（自定义接口/Ollama 场景）。"""
        if base_url_override:
            return base_url_override
        try:
            from ..config import text_models
            _cfg = (text_models() or {}).get(self._model(override)) or {}
            return str(_cfg.get("baseUrl") or "")
        except Exception:
            return ""

    async def generate(
        self,
        base_messages: list,
        ai_emotion: dict,
        character_prompt: str = "",
        session_id: str = "default",
        character_id: str = "default",
        user_intent: str = "",
        model_override: str = "",
        base_url_override: str = "",
    ) -> List[dict]:
        """生成分段气泡列表（兼容旧接口，内部收集 generate_stream 的逐段结果）。

        每条格式：{"content": str, "delay": float, "turn_type": str, "emotion": str}
        """
        out: List[dict] = []
        async for _turn in self.generate_stream(
            base_messages, ai_emotion, character_prompt,
            session_id, character_id, user_intent,
            model_override=model_override,
            base_url_override=base_url_override,
        ):
            out.append(_turn)
        return out

    async def generate_stream(
        self,
        base_messages: list,
        ai_emotion: dict,
        character_prompt: str = "",
        session_id: str = "default",
        character_id: str = "default",
        user_intent: str = "",
        model_override: str = "",
        base_url_override: str = "",
    ):
        """逐段生成气泡并即时 yield（边生成边推送，供 stream 入口实时下发）。

        每条格式：{"content": str, "delay": float, "turn_type": str, "emotion": str}
        """
        from ..emotion_engine.ai_emotion import (
            BEHAVIOR_TEMPLATES, TURN_INSTRUCTIONS, SILENCE_BUBBLES
        )

        emotion  = ai_emotion.get("emotion", "calm")
        template = BEHAVIOR_TEMPLATES.get(emotion, BEHAVIOR_TEMPLATES["calm"])
        pattern  = template["pattern"]
        delays   = template["delays"]

        # ★ 补丁三：用 TURN_INSTRUCTIONS 里的指令影响 system prompt + 读取节奏参数
        emotion_str = ai_emotion.get("emotion", "calm") if isinstance(ai_emotion, dict) else "calm"
        intensity   = float(ai_emotion.get("intensity", 0.5) if isinstance(ai_emotion, dict) else 0.5)

        turn_instruction = TURN_INSTRUCTIONS.get(emotion_str, "")
        if turn_instruction and base_messages:
            for m in base_messages:
                if m.get("role") == "system":
                    m["content"] += f"\n\n【当前情绪节奏指令】\n{turn_instruction}"
                    break

        max_turns  = int(template.get("max_bubbles", template.get("turns", 3)) or 3)
        affection = 50
        try:
            from ..relationship.manager import RelationshipManager
            relation = RelationshipManager().get_state(
                session_id or "default", character_id or "default", create=False
            )
            affection = int(relation.get("affection", 50) or 50)
        except Exception:
            affection = 50
        intimacy = affection
        try:
            from .. import db as _db
            _rel = _db.get_relationship_state(session_id or "default", character_id or "default") or {}
            intimacy = int(_rel.get("intimacy", _rel.get("closeness", affection)) or affection)
            affection = int(_rel.get("affection", affection) or affection)
        except Exception:
            pass

        # 好感度只提高“连着说”的上限，不强制每次刷屏。
        # 目标段数还要看场景：短消息/严肃问题/低落或冲突时收敛，
        # 开心、撒娇、暖心话和重大事件才更容易出现 7~8 段。
        user_text = ""
        for _m in reversed(base_messages or []):
            if _m.get("role") == "user":
                user_text = str(_m.get("content") or "")
                break
        short_msg = len(user_text.strip()) <= 4
        serious = any(k in user_text for k in ("怎么办", "原因", "报错", "失败", "解释", "工作", "学习"))
        warm = any(k in user_text for k in ("想你", "喜欢你", "爱你", "抱抱", "谢谢你", "好开心", "生日", "纪念"))
        lively_emotion = emotion_str in ("happy", "excited", "tender", "playful", "loving") and intensity >= 0.65
        # ★ 只有明确的生气/冷漠才收敛段数；委屈/难过/担心是"需要被哄"的状态，
        #   不该因此变冷变少（否则用户一不满 AI 就越回越短，形成负循环）。
        # ★ 好感拉满（>=95）时，短消息也不收敛——用户发"睡了/晚了"这类短句，
        #   AI 不该突然从热情多段掉回一句冷场（"语音只剩一段"正是由此而来）。
        _affection_max = max(affection, intimacy)
        restrained = (short_msg and _affection_max < 95) or serious or emotion_str in ("angry", "cold")
        # 好感拉满时，原来有 55% 的概率只发 2~3 条，配上"末尾必问"就成了
        # 「回应一句 + 追问一句」的一问一答。这里把多段概率整体上调，
        # 并给非收敛场景抬一个 3 条的下限，让 4~6 段成为常态。
        if affection >= 100 or intimacy >= 100:
            if restrained:
                # 收敛场景（短消息 / 严肃问题 / 负面情绪）：不硬凑长篇，
                # 2~3 条为主。
                roll = random.random()
                if roll < 0.10:
                    max_turns = random.randint(4, 5)
                elif roll < 0.40:
                    max_turns = random.randint(3, 4)
                else:
                    max_turns = random.randint(2, 3)
            else:
                # ★ 好感拉满：不再限制段数，让 AI 自由发挥延续话题。
                #   保留 12 的护栏只防极端刷屏，正常场景下 _split_into_turns
                #   几乎不会再截断（模型一次输出很少超过 12 句）。
                max_turns = 12
        elif affection >= 95 or intimacy >= 95:
            if (warm or lively_emotion) and not restrained:
                max_turns = random.randint(4, 6)
            elif not restrained:
                max_turns = random.randint(3, 5)
            else:
                max_turns = random.randint(2, 3)
        elif affection >= 85 or intimacy >= 85:
            max_turns = random.randint(3, 6) if not restrained else random.randint(2, 4)
        elif affection >= 70 or intimacy >= 70:
            max_turns = random.randint(3, 5) if not restrained else random.randint(1, 3)
        elif affection >= 55 or intimacy >= 55:
            max_turns = random.randint(2, 4) if not restrained else random.randint(1, 2)

        # ★ 场景递进：命中回忆/安慰/撒娇等场景时，按场景的层次数放宽条数。
        #   只按好感度给条数的话，日常对话（calm 模板仅 2 条）没有递进空间，
        #   容易停留在"回应一句 + 追问一句"。取 max 是避免跟好感度分支互相压制。
        scene = None
        try:
            from .scene import detect as _detect_scene, suggest_turns as _scene_turns
            scene = _detect_scene(user_text, emotion_str)
            # 日常闲聊兜底：5 个具名场景都没命中时，用「接住→感受→细节→收住」的
            # 递进骨架，避免又退回「回应一句 + 追问一句」。
            # 只在关系够深且不收敛时启用——关系浅时硬凑长篇反而假。
            if scene is None and not restrained and max(affection, intimacy) >= 70:
                try:
                    from .scene import casual_scene as _casual
                    _cs = _casual()
                    if _cs:
                        scene = _cs
                        logger.info("[MultiTurn] 命中兜底场景「日常闲聊」")
                except Exception as _ce:
                    logger.warning(f"[MultiTurn] 兜底场景跳过: {_ce}")
            # casual 只是兜底递进骨架，不参与抬升条数：
            # 否则它的 max_turns=5 会把日常闲聊一律顶到 5~7 条，
            # 既啰嗦，也会把上面按好感度算出来的条数分布整个压平。
            if scene and scene.get("id") != "casual":
                _st = _scene_turns(scene, affection, intimacy, restrained)
                if _st > max_turns:
                    logger.info(
                        f"[MultiTurn] 场景「{scene.get('name')}」"
                        f" 条数 {max_turns} -> {_st}")
                    max_turns = _st
        except Exception as e:
            logger.warning(f"[MultiTurn] 场景识别跳过: {e}")
            scene = None

        # 原情绪模板通常只有 1~3 条；高好感时补充自然延展，且只在末尾问一次。
        if len(pattern) < max_turns:
            extension = ["share_feeling", "react", "tease", "comfort", "share", "detail_expand"]
            # tease_back 需要用户先调侃才成立，只在轻松情绪下补，否则模型会硬找调侃点
            if emotion_str in ("playful", "excited", "happy", "loving"):
                extension.append("tease_back")
            # recall 需要真实的共同经历做底，关系够深才允许，避免模型凭空编造回忆
            if max(affection, intimacy) >= 70:
                extension.append("recall")
            pattern = [p for p in pattern if p != "question"]
            # 补段时避开当前 pattern 已有的类型（如 happy 已含 react 就不再补 react），
            # 从剩余类型循环取，减少高好感多段时的类型重复（报菜名/换说法重复的诱因之一）。
            _seen = set(pattern)
            _pool = [t for t in extension if t not in _seen] or extension
            _idx = 0
            while len(pattern) < max_turns:
                pattern.append(_pool[_idx % len(_pool)])
                _idx += 1
            pattern = pattern[:max_turns]
            if max(affection, intimacy) >= 70 and pattern:
                # 只在「真的好奇、想把话递回给用户」时才以提问收尾。
                # 过去无条件钉成 question，配上 2~3 条就成了「回应一句 + 追问一句」的审讯循环。
                if random.random() < 0.45:
                    pattern[-1] = "question"
                else:
                    # 收尾类型要避开本轮已用过的，否则末条会和中间某条撞型（报菜名的诱因之一）
                    _tail_pool = [
                        t for t in ("share_feeling", "detail_expand", "share")
                        if t not in set(pattern[:-1])
                    ]
                    if _tail_pool:
                        pattern[-1] = random.choice(_tail_pool)
        base_delay = template.get("base_delay",   1.2)
        min_delay  = template.get("min_delay",    0.5)

        chat_once_fn = self._chat_once()
        model        = self._model(model_override)
        api_key      = self._api_key(model_override)
        base_url     = self._base_url(model_override, base_url_override)

        # ★ 话题大纲：先用便宜模型规划这一轮聊哪几个话题，再逐条生成。
        topics = []
        try:
            from .. import config as _cfg
            # 命中场景时由 layers 驱动纵深，并列式话题大纲用不上，跳过省一次调用
            if (getattr(_cfg, "MULTI_TURN_TOPIC_PLAN", True) and api_key
                    and max_turns >= 3 and scene is None):
                topics = await self._plan_topics(
                    base_messages, user_text, min(max_turns, len(pattern)),
                    chat_once_fn, model, api_key, character_id=character_id,
                )
                if topics:
                    logger.info(f"[MultiTurn] 话题大纲: {topics}")
        except Exception as e:
            logger.warning(f"[MultiTurn] 话题大纲跳过: {e}")
            topics = []

        # ★ 表达形式池：角色卡可用 dialogue_style.expression_forms 定制
        #   False → 关闭形式层；list → 只用指定的几种；不配 → 全部 10 种
        form_pool = []
        try:
            from ..emotion_engine.ai_emotion import EXPRESSION_FORMS
            # ★ P0-3：改用 get_character_any（新格式优先、legacy 兜底）。
            #   原来只调 load_character，自建角色的 expression_forms 定制永远读不到。
            from ..character_manager import get_character_any
            _cfg_forms = ((get_character_any(character_id) or {}).get("dialogue_style") or {}).get("expression_forms")
            if _cfg_forms is False:
                form_pool = []
            elif isinstance(_cfg_forms, list) and _cfg_forms:
                form_pool = [f for f in _cfg_forms if f in EXPRESSION_FORMS] or list(EXPRESSION_FORMS)
            else:
                form_pool = list(EXPRESSION_FORMS)
        except Exception:
            form_pool = []

        # ★ 记忆锚点（强版本）：检索与当前话题相关的往事，只取 1 条作可选素材。
        #   只在多段（>=3 条）时启用，避免为一两条气泡多跑一次检索。
        memory_anchor = ""
        if max_turns >= 3:
            try:
                _recent = [
                    str(m.get("content") or "")
                    for m in (base_messages or [])[-8:]
                    if isinstance(m, dict) and m.get("role") == "assistant"
                ]
                memory_anchor = self._build_memory_anchor(
                    session_id, character_id, user_text, recent_contents=_recent
                )
                if memory_anchor:
                    logger.info("[MultiTurn] 记忆锚点已注入（仅第一条气泡）")
            except Exception as e:
                logger.warning(f"[MultiTurn] 记忆锚点跳过: {e}")
                memory_anchor = ""

        # 去味：读取历史用过的形式，本轮避开最近用过的腔调
        used_forms = _load_recent_forms(session_id, character_id)

        # ★ 逐段生成（loop）：每段单独生成一句话，循环 N 次得到 N 条气泡。
        #   相比"一次生成多句再拆"（once），模型只需每次说一句，容易遵守，
        #   不会再出现"一次只输出十几个字"导致整轮只剩 1 条的情况。
        #   好感度拉满时 max_turns 会被抬到 12，这里收敛到 6 段（够多且不至于太慢）。
        _loop_turns = max(1, min(int(max_turns), 6))
        # 保底 3 段：关系够深、且不是收敛场景（短消息/严肃/负面情绪）时，
        # 前 3 条无条件生成，第 4 条起才让 AI 自己判断是否自然收尾。
        _min_turns = 3 if (max(affection, intimacy) >= 70 and not restrained) else 1
        # 去掉会让人把话说短的表达形式（好感拉满要热情多段，不需要"极短/说一半"）
        _short_forms = {"short_punch", "half_sentence", "trailing"}
        _form_pool = [f for f in form_pool if f not in _short_forms]
        # 调试日志：定位"话少"时一眼看清关键参数
        print(f"[MultiTurn][Debug] affection={affection} intimacy={intimacy} "
              f"emotion={emotion_str} restrained={restrained} max_turns={max_turns} "
              f"loop_turns={_loop_turns} min_turns={_min_turns}", flush=True)
        _seen = []
        _prev_turns = []
        for _i in range(_loop_turns):
            _ptype = pattern[_i] if _i < len(pattern) else "share_feeling"
            # 直接用 TURN_INSTRUCTIONS 的口语化描述（更贴人设，避免自定义"任务"腔）
            _task = TURN_INSTRUCTIONS.get(_ptype, "接着上一句往下说，给出新信息或更深的感受")
            # 前 _min_turns 条必须生成；之后每条让 AI 自己判断要不要收尾。
            _can_stop = _i >= _min_turns
            if _can_stop:
                _task += ("。如果你觉得这一轮已经说完整、不需要再往下接一句了，"
                          "就在这句末尾加「（说完了）」；如果还想继续聊，就正常说一句，"
                          "不要加任何标记。")
            _form = _form_pool[_i % len(_form_pool)] if _form_pool else ""
            _msgs = self._build_messages(
                base_messages, instruction=_task, emotion=emotion,
                turn_index=_i, total_turns=_loop_turns,
                previous_turns=_prev_turns, topics=topics, form=_form,
                memory_anchor=memory_anchor if _i == 0 else "",
                scene=scene, user_intent=user_intent,
            )
            _raw = ""
            try:
                _raw = await _gen_one(
                    chat_once_fn, model, _msgs, api_key,
                    temperature=0.8, freq=0.3, pres=0.3, max_tokens=250,
                    base_url=base_url,
                )
                _raw = (_raw or "").strip()
            except Exception as e:
                logger.error(f"[MultiTurn] 第{_i + 1}条生成失败: {e}")
                continue
            # 只取第一句（防模型偶尔输出多句/带解释）
            _first = _split_into_turns(_raw, 1) or []
            _c = str(_first[0]).strip() if _first else _raw
            if not _c:
                continue
            # 收尾标记：AI 主动说"说完了"时，剥掉标记并在满保底条数后提前结束
            _stop = False
            if _can_stop and "说完了" in _c:
                _c = (_c.replace("（说完了）", "").replace("(说完了)", "")
                        .replace("说完了", "").strip())
                _c = _c.strip("（）()[]【】 ")
                _stop = True
            if not _c:
                if _stop:
                    break
                continue
            # ★ 过滤纯省略号/纯标点的沉默气泡
            try:
                from ..emotion_engine.ai_emotion import is_silence
                if is_silence(_c):
                    continue
            except Exception:
                pass
            if _too_similar(_c, _seen) or _internal_duplicate(_c):
                continue
            _seen.append(_c)
            _prev_turns.append(_c)
            # 边生成边推送：delay 只做轻量节奏（生成本身已是间隔），按已发条数递增
            _n = len(_seen)
            if emotion_str in ("excited", "happy") and intensity > 0.7:
                _delay = max(min_delay, base_delay * 0.6 - (_n - 1) * 0.1)
            elif emotion_str in ("angry", "cold"):
                _delay = base_delay * 1.2 + (_n - 1) * 0.3
            elif emotion_str == "upset":
                _delay = min_delay if _n == 1 else base_delay * 0.8
            else:
                _delay = base_delay + (_n - 1) * 0.2
            yield {
                "content":   _c,
                "delay":     _delay,
                "turn_type": _ptype,
                "emotion":   emotion,
            }
            if _stop:
                logger.info(f"[MultiTurn] AI 主动收尾，本轮共 {_n} 条")
                break
        print(f"[MultiTurn][Debug] loop_turns={_loop_turns} generated={len(_seen)}", flush=True)

        # 记录本轮用过的表达形式，供下一轮避让（去味）
        if used_forms:
            _save_recent_forms(session_id, character_id, used_forms)

    async def generate_once_stream(
        self,
        base_messages: list,
        ai_emotion: dict,
        character_prompt: str = "",
        session_id: str = "default",
        character_id: str = "default",
        user_intent: str = "",
        model_override: str = "",
        base_url_override: str = "",
    ):
        """一次生成完整回复，再拆成气泡逐句 yield（once 模式，替代逐段独立生成）。

        相比 generate_stream（N 段 = N 次 LLM 串行调用），这里只调 1 次 LLM：
        - 提速：LLM 调用从 4~6 次降到 1 次，回复明显更快；
        - 顺序：一次生成的回复天然连贯，拆句后顺序不乱（修复逐段独立生成
          导致话题穿插、前后不接的问题）。
        每条格式同 generate_stream：{"content", "delay", "turn_type", "emotion"}
        """
        from ..emotion_engine.ai_emotion import BEHAVIOR_TEMPLATES

        emotion = ai_emotion.get("emotion", "calm") if isinstance(ai_emotion, dict) else "calm"
        template = BEHAVIOR_TEMPLATES.get(emotion, BEHAVIOR_TEMPLATES["calm"])
        base_delay = template.get("base_delay", 1.2)

        # 好感度（与 generate_stream 相同的读取口径）
        affection = 50
        try:
            from ..relationship.manager import RelationshipManager
            relation = RelationshipManager().get_state(
                session_id or "default", character_id or "default", create=False
            )
            affection = int(relation.get("affection", 50) or 50)
        except Exception:
            affection = 50
        intimacy = affection
        try:
            from .. import db as _db
            _rel = _db.get_relationship_state(session_id or "default", character_id or "default") or {}
            intimacy = int(_rel.get("intimacy", _rel.get("closeness", affection)) or affection)
            affection = int(_rel.get("affection", affection) or affection)
        except Exception:
            pass

        # 场景判断（决定是否收敛段数）
        user_text = ""
        for _m in reversed(base_messages or []):
            if _m.get("role") == "user":
                user_text = str(_m.get("content") or "")
                break
        short_msg = len(user_text.strip()) <= 4
        serious = any(k in user_text for k in ("怎么办", "原因", "报错", "失败", "解释", "工作", "学习"))
        _affection_max = max(affection, intimacy)
        restrained = (short_msg and _affection_max < 95) or serious or emotion in ("angry", "cold")

        # 段数上限（拆句条数上限；once 模式下模型一次输出，这里是拆句时的护栏）
        if restrained:
            max_turns = random.randint(2, 3)
        elif _affection_max >= 85:
            max_turns = random.randint(4, 6)
        elif _affection_max >= 55:
            max_turns = random.randint(3, 4)
        else:
            max_turns = random.randint(2, 3)

        # 记忆锚点（可选素材，只在 >=3 句时启用）
        memory_anchor = ""
        if max_turns >= 3:
            try:
                _recent = [
                    str(m.get("content") or "")
                    for m in (base_messages or [])[-8:]
                    if isinstance(m, dict) and m.get("role") == "assistant"
                ]
                memory_anchor = self._build_memory_anchor(
                    session_id, character_id, user_text, recent_contents=_recent
                )
            except Exception:
                memory_anchor = ""

        chat_once_fn = self._chat_once()
        model = self._model(model_override)
        api_key = self._api_key(model_override)
        base_url = self._base_url(model_override, base_url_override)

        # min_turns：restrained 场景下调，避免用户说"睡了"也硬凑 3 句
        min_turns = 1 if restrained else 3
        extra = self._build_once_extra(
            emotion, max_turns, topics=None, scene=None,
            user_intent=user_intent, memory_anchor=memory_anchor,
            min_turns=min_turns,
        )

        # ★ 2026-09-12 历史卫生（治"复读"，治本层）
        #   本地小模型（qwen3:4b）在"一整墙 assistant 消息"的上下文里会退化成
        #   直接照抄上一轮自己说过的话。实测 QQ 侧连发三轮逐字重复同两句
        #   （chat_history #9104~#9115），根因是喂进去的 60 条历史里混着
        #   23 条连续的 assistant 游戏唠嗑 + 多组重复的主动消息 —— 中间一句 user 都没有，
        #   4B 模型看到这种形状就顺着往下"续写自己"。
        #   这里只做两件最小改动，且**一条 user 都不删**（用户的话必须全在场）：
        #     1) 每段连续 assistant 只保留最后 1 条（把几十条唠嗑塌成 1 条状态）
        #     2) assistant 总数再兜一个上限，防止历史整体被自己的话占满
        _hist_raw = [m for m in (base_messages or []) if isinstance(m, dict)]
        _hist = []
        _run_start = None
        for _i, _m in enumerate(_hist_raw):
            if _m.get("role") == "assistant":
                if _run_start is None:
                    _run_start = len(_hist)
                    _hist.append(_m)
                else:
                    _hist[_run_start] = _m      # 同一段连续 assistant 里，永远留最新那条
            else:
                _run_start = None
                _hist.append(_m)
        _asst_idx = [i for i, m in enumerate(_hist) if m.get("role") == "assistant"]
        _MAX_ASST = 24
        if len(_asst_idx) > _MAX_ASST:
            _drop = set(_asst_idx[:len(_asst_idx) - _MAX_ASST])
            _hist = [m for i, m in enumerate(_hist) if i not in _drop]

        # ★ 2026-09-12 第二轮（方案C）：**跨历史去重**，治"复读吸引子"
        #   第一轮只折叠了「连续的 assistant」，但实测复读的那几句是被 user 消息
        #   **隔开**的，所以一条没少：「陪你在床头睡到天亮的」「新买的绿植」
        #   「抓汤圆」「院子里种胡萝卜」在最近 60 条里各自出现 3~5 次。
        #   模型看到自己说过五遍同样的话，就继续照抄 —— 这就是吸引子。
        #
        #   证据（决定性）：换成 qwen3:8b（两倍大的模型）并且给派生模型加了
        #   repeat_penalty 1.1 + repeat_last_n 256 之后，**复读内容一字未变**；
        #   而同一时刻同一份提示词，400 超窗从 7 次降到 0 次（说明模型本身没问题）。
        #   → 根因是历史，不是模型。
        #
        #   规则：某条 assistant 若与**更靠后**的另一条 assistant 近似（≥0.8），
        #   丢掉较早那条、只留最新一次。**user 消息一条都不动**。
        #   过短的消息（归一化后 <8 字）豁免，避免"嗯/…"这类短气泡被误删。
        try:
            _asst = [(i, _norm_for_sim(m.get("content")))
                     for i, m in enumerate(_hist) if m.get("role") == "assistant"]
            _drop_dup = set()
            for _a in range(len(_asst)):
                _ia, _na = _asst[_a]
                if _ia in _drop_dup or len(_na) < 8:
                    continue
                for _b in range(_a + 1, len(_asst)):
                    _ib, _nb = _asst[_b]
                    if _ib in _drop_dup or len(_nb) < 8:
                        continue
                    if _na == _nb or _too_similar(_na, [_nb], threshold=0.8):
                        _drop_dup.add(_ia)
                        break
            if _drop_dup:
                _hist = [m for i, m in enumerate(_hist) if i not in _drop_dup]
                print(f"[MultiTurn] 历史去重：丢掉 {len(_drop_dup)} 条较早的重复 assistant "
                      f"（剩 {len(_hist)} 条）", flush=True)
        except Exception as _de:
            print(f"[MultiTurn] 历史去重异常(放行): {type(_de).__name__}: {_de!r}", flush=True)

        # 2026-09-13：原「本地路由缩短历史窗口」（方案C-2）随本地大脑一起下线。
        # 见 git 快照 e114b61。

        # 把 extra 追加到 system 消息
        msgs = []
        patched = False
        for msg in _hist:
            if msg.get("role") == "system" and not patched:
                msgs.append({"role": "system", "content": (msg.get("content") or "") + extra})
                patched = True
            else:
                msgs.append(dict(msg))
        if not patched:
            msgs.insert(0, {"role": "system", "content": extra.lstrip()})

        raw = ""
        _thinking_buf = ""
        _thinking_sig_sent = False
        _thinking_full_sent = False
        _t_start = None
        try:
            from ..deepseek_api import stream_chat
            # ★ GLM-5.3 系列思考由模型默认开启（自适应，实测基线 5/5 触发）。
            #   实测 reasoning_effort=high 反而把思考触发率压到 2/5（智谱兼容层
            #   对该参数的处理不利于「先想再答」），故此处不传该参数。
            async for _kind, _payload in stream_chat(
                model, msgs, api_key,
                base_url=base_url, temperature=0.8, max_tokens=2048,
            ):
                if _kind == "line":
                    _piece = _extract_stream_delta(_payload)
                    if _piece is None:
                        continue
                    _is_reasoning, _text = _piece
                    if _is_reasoning:
                        _thinking_buf += _text
                        if not _thinking_sig_sent:
                            _thinking_sig_sent = True
                            _t_start = asyncio.get_running_loop().time()
                            yield {"turn_type": "thinking"}
                    else:
                        # ★ 思考结束（第一段正文到达）：把推理全文一次性交出去。
                        #   文本放在 thinking 字段、content 保持为空——按 content 取值
                        #   的消费方（离线回复落库 / QQ 生成）行为完全不变，
                        #   只有 SSE 出口（main.py /api/chat/stream）把它翻译给前端展示。
                        if _thinking_buf and not _thinking_full_sent:
                            _thinking_full_sent = True
                            _t_turn = {"turn_type": "thinking", "thinking": _thinking_buf}
                            if _t_start is not None:
                                _sec = int(round(asyncio.get_running_loop().time() - _t_start))
                                if _sec >= 1:
                                    _t_turn["seconds"] = _sec
                            yield _t_turn
                    if not _is_reasoning:
                        raw += _text
                elif _kind == "meta":
                    break
        except Exception as e:
            # ★ 网络抖动重试（2026-09-10）：凌晨实测 DNS 抖动（getaddrinfo failed /
            #   ConnectionReset）会让整轮生成直接失败 → 她"回不了话"。瞬态网络错误
            #   等 2 秒重试一次；仍失败才放弃（走调用方的降级链）。
            _ename = type(e).__name__
            _transient = any(k in str(e) for k in ("getaddrinfo", "ConnectError", "ConnectionReset", "ReadError", "timeout", "Timeout"))
            if _transient:
                logger.warning(f"[MultiTurn] once 生成遇网络抖动（{_ename}），2s 后重试一次")
                await asyncio.sleep(2)
                try:
                    async for _kind, _payload in stream_chat(
                        model, msgs, api_key,
                        base_url=base_url, temperature=0.8,
                        max_tokens=4096,
                    ):
                        if _kind == "line":
                            _piece = _extract_stream_delta(_payload)
                            if _piece is None:
                                continue
                            _is_reasoning, _text = _piece
                            if not _is_reasoning:
                                raw += _text
                        elif _kind == "meta":
                            break
                except Exception as _e2:
                    logger.error(f"[MultiTurn] once 重试仍失败: {type(_e2).__name__}: {_e2!r}")
                    return
            else:
                logger.error(f"[MultiTurn] once 生成失败: {_ename}: {e!r}")
                return

        raw = (raw or "").strip()
        if not raw:
            return

        parts = _split_into_turns(raw, max_turns)

        # ★ 2026-09-12 硬护栏（治"复读"，兜底层）
        #   提示词是软约束，本地 4B（qwen3:4b）不一定听。这里做硬检查：
        #   凡与历史里"自己说过的话"逐字或近似重复的气泡，一律不发。
        #   历史取原始 base_messages（含被折叠掉的唠嗑），因为模型可能抄的就是那些。
        # ★ 2026-09-12 修：比对窗口原来只取 base_messages[-24:]，而实测复读的句子
        #   来自更早的位置（#9199 复读的是 #9174/#9176 的话，早已滑出 24 条窗口）
        #   → 护栏报"丢掉 1/5 条"却漏掉了真正该拦的那几条。改成比对**整个历史**。
        _guard_pool = (base_messages or [])[-80:]
        _recent_asst = [
            str(m.get("content") or "").strip()
            for m in _guard_pool
            if isinstance(m, dict) and m.get("role") == "assistant"
            and str(m.get("content") or "").strip()
        ]
        _recent_norm = [_norm_for_sim(x) for x in _recent_asst]
        _recent_norm = [x for x in _recent_norm if x]

        def _dedup_parts(_ps):
            """返回 (保留的气泡, 被判为复读的条数)。"""
            kept, copied = [], 0
            for _p in _ps:
                _n = _norm_for_sim(_p)
                _dup = False
                if _n:
                    for _old in _recent_norm:
                        if _n == _old or (len(_n) >= 8 and _too_similar(_p, [_old], threshold=0.88)):
                            _dup = True
                            break
                if _dup:
                    copied += 1
                else:
                    kept.append(_p)
            return kept, copied

        _kept, _copied = _dedup_parts(parts)
        if _copied:
            print(f"[MultiTurn] once 复读护栏：丢掉 {_copied}/{len(parts)} 条与历史重复的气泡", flush=True)

        if not _kept:
            # 整段都在复读 —— 提高温度 + 显式点名，再要一次；仍失败就本轮不发，
            # 交给调用方的兜底链（继续硬发重复内容比少说一句更糟）。
            print("[MultiTurn] once 输出整段复读 → 升温重试一次", flush=True)
            try:
                _hard = [dict(m) for m in msgs]
                for _m in _hard:
                    if _m.get("role") == "system":
                        _m["content"] = (_m.get("content") or "") + (
                            "\n\n【严重警告·必须遵守】你上一次的回答是把聊天记录里"
                            "**你自己说过的话**原样照抄了一遍，这是无效回复。"
                            "这一次必须输出**全新的、之前从未出现过的**内容，"
                            "哪怕只说一句简短的话也算合格；只要出现旧句子就算失败。"
                        )
                        break
                _raw2 = ""
                async for _k2, _p2 in stream_chat(
                    model, _hard, api_key,
                    base_url=base_url, temperature=1.0, max_tokens=2048,
                ):
                    if _k2 == "line":
                        _pc = _extract_stream_delta(_p2)
                        if _pc is not None and not _pc[0]:
                            _raw2 += _pc[1]
                    elif _k2 == "meta":
                        break
                _kept2, _c2 = _dedup_parts(
                    _split_into_turns((_raw2 or "").strip(), max_turns)
                )
                if _c2:
                    print(f"[MultiTurn] once 重试后仍有 {_c2} 条复读被丢弃", flush=True)
                if not _kept2:
                    print("[MultiTurn] once 重试仍为复读，本轮放弃", flush=True)
                    return
                _kept = _kept2
            except Exception as _ce:
                logger.warning(f"[MultiTurn] once 反复读重试失败: {type(_ce).__name__}: {_ce!r}")
                return

        parts = _kept
        for i, p in enumerate(parts):
            yield {
                "content":   p,
                "delay":     base_delay + i * 0.2,
                "turn_type": "react",
                "emotion":   emotion,
            }

    def _build_memory_anchor(self, session_id, character_id, user_text,
                             recent_contents=None, max_items=1) -> str:
        """记忆锚点（强版本）：按当前话题语义检索长期记忆，只取 1 条作可选素材。

        与 companion 层那条通用【用户长期记忆】注入不同，这里只挑**和当前话题真正相关**
        的那一条，并设了四道闸防止 AI 变成翻旧账机器：

          闸1 相关度门槛 —— similarity 不达标就不用，宁缺毋滥
          闸2 一轮最多 1 条，且只在第一条气泡注入（调用方保证）
          闸3 与近几条已发内容重复的不提（不炒冷饭）
          闸4 同一条记忆 24 小时内不重复用（冷却）

        任何异常一律返回 ""，绝不影响正常生成。
        """
        if not user_text or not str(user_text).strip():
            return ""
        try:
            from .. import config as _cfg
            if not getattr(_cfg, "MULTI_TURN_MEMORY_ANCHOR", True):
                return ""
            from ..memory.retriever import hybrid_search
            hits = hybrid_search(
                user_id=session_id or "default",
                query=str(user_text)[:200],
                top_k=4,
                character_id=character_id or "default",
            )
        except Exception:
            return ""
        if not hits:
            return ""

        # 冷却记录：同一条记忆 24h 内不重复用
        cool_key = f"multiturn_mem_anchor:{session_id or 'default'}:{character_id or 'default'}"
        used_map = {}
        try:
            from .. import db as _db
            import json as _json
            raw = _db.kv_get(cool_key)
            if raw:
                used_map = _json.loads(raw) or {}
            if not isinstance(used_map, dict):
                used_map = {}
        except Exception:
            used_map = {}

        import time as _time
        now = _time.time()
        recent_norm = [_norm_for_sim(x) for x in (recent_contents or []) if x]
        recent_norm = [x for x in recent_norm if len(x) >= 4]

        picked = None
        for h in hits:
            content = str(h.get("content") or "").strip()
            if len(content) < 4:
                continue
            # 闸1 相关度门槛。
            # ★ 两种检索源的分数不可直接比较，用错门槛会导致功能永不触发：
            #   · 向量：similarity = raw_sim × 0.45~0.65 权重，已被压缩
            #     （真实余弦 0.8 加权后只剩 0.52），必须看未加权的 raw_sim；
            #   · 关键词：similarity = 匹配词数占比 × 权重 + 位置加成，
            #     全词命中也才约 0.45，门槛必须比向量低。
            src = str(h.get("source") or "")
            try:
                if src == "keyword":
                    score = float(h.get("similarity") or 0)
                else:
                    score = float(h.get("raw_sim") or h.get("similarity") or 0)
            except Exception:
                score = 0.0
            floor = 0.62 if src != "keyword" else 0.35
            if score < floor:
                continue
            n = _norm_for_sim(content)
            if n and any((n in r or r in n) for r in recent_norm):   # 闸3 不炒冷饭
                continue
            k = str(h.get("id") or content)[:64]
            if now - float(used_map.get(k, 0) or 0) < 86400:          # 闸4 24h 冷却
                continue
            picked = (content, k)
            break                                            # 闸2 只取 1 条

        if not picked:
            return ""
        content, k = picked
        used_map[k] = now
        try:
            from .. import db as _db
            import json as _json
            # 只保留最近 40 条，避免 kv 无限增长
            if len(used_map) > 40:
                used_map = dict(sorted(used_map.items(), key=lambda x: -float(x[1] or 0))[:40])
            _db.kv_set(cool_key, _json.dumps(used_map, ensure_ascii=False))
        except Exception:
            pass

        return (
            "\n【可选素材·你们之间的往事】\n"
            f"- {content}\n"
            "只在和当前话题有自然衔接点时才提，一句话带过即可；没有衔接点就完全不要提。"
            "当前话题永远是第一优先，不要为了用素材而岔开话题。"
        )

    async def _plan_topics(self, base_messages, user_text, turns,
                           chat_once_fn, model, api_key, character_id="default") -> list:
        """用便宜模型规划这一轮的话题大纲；任何失败都返回 []，静默降级。

        没有大纲时每条气泡独立生成，模型只知道"别重复"，不知道"这轮要聊什么"，
        于是变成一条一个话题的报菜名。只在 turns >= 3 时启用（1~2 条省掉这次调用）。
        """
        # 门槛由 3 降到 2：calm 模板默认就是 2 条，卡在 3 会让日常对话
        # 永远拿不到大纲，只能"回应一句 + 追问一句"，无从递进。
        if turns < 2 or chat_once_fn is None or not api_key:
            return []
        try:
            from .. import config as _cfg
            # ★ 2026-09-11：原 getattr(_cfg, "MEMORY_EXTRACT_MODEL") 永远取空（那是 dict 键不是模块属性），
            #   实际一直回退主脑。改走分层解析：角色卡 memory_model > 全局 > 主脑。
            plan_model = _cfg.memory_extract_model(character_id) or model
        except Exception:
            plan_model = model

        outline_messages = [
            dict(m) for m in (base_messages or [])[-6:]
            if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant")
        ]
        outline_messages.append({
            "role": "user",
            "content": (
                _TOPIC_OUTLINE_PROMPT
                + f"\n\n用户最后说：{str(user_text or '')[:200]}"
                + f"\n这一轮要发 {turns} 条，请规划最多 {min(3, turns)} 个话题。"
            ),
        })
        try:
            raw = await asyncio.wait_for(
                _gen_one(chat_once_fn, plan_model, outline_messages, api_key,
                         temperature=0.3, freq=0.0, pres=0.0, max_tokens=200),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[MultiTurn] 话题大纲超时，降级为无大纲")
            return []
        except Exception as e:
            logger.warning(f"[MultiTurn] 话题大纲失败: {e}")
            return []
        return _parse_topics(raw or "", min(3, turns))

    def _build_once_extra(self, emotion, max_turns, topics=None, scene=None,
                          user_intent="", memory_anchor="", min_turns=3):
        """构建「一次生成完整回复」的 system 附加块（替代逐条独立生成）。

        让模型一次输出多个短句（每句一行），再由 _split_into_turns 拆成气泡。
        一次生成天然连贯、不重复，比逐条独立生成稳定得多。

        ★ 2026-09-10 段数决定权还给模型（用户要求）：
          旧版「必须输出 X~Y 句」是先抽段数再填内容——模型没有"这条不用多说"的
          决定权，导致每轮固定 4 段、用户发一句也回 4 段。新版只给情境指引
          （简短的消息简短回，走心的话题可以多说），句数由模型按内容自己判断。
        """
        emotion_desc = EMOTION_DESCS.get(emotion, "自然回复")
        _guard = max(3, min(int(max_turns), 18))  # 只做防刷屏护栏（拆句器二次裁剪）

        # ★ 2026-09-14 提示词压缩模式：规则一条不减，只删「同义强调 / 重复表述」。
        #   原 834 字里可去掉的冗余：
        #     · 【最重要】与【不许复读】都在说"别重复"，合并成一条；
        #     · 【禁止扮演用户】三条子规则同义（别写 TA 的话 / 别写 TA 式回应 / 别自问自答），
        #       合并为一条并保留最易踩的「知道啦/好的」例子；
        #     · 【发图】"光说发一张给你用户看不到图"与"只有写标记才真的发"同义，留一句；
        #     · 【任务·最高优先级】与【规则】里"每句一行/完整有标点"重复，合并。
        #   全部原有约束项在压缩版里都有对应句子（见 tools/_verify_compact_prompt.py 的逐条核对）。
        try:
            from .. import config as _cfg
            _compact = _cfg.prompt_compact()
        except Exception:
            _compact = False

        if _compact:
            extra = (
                f"\n\n【当前情绪】{emotion_desc}\n"
                f"【任务·最高优先级】像真人连发微信一样输出回复：一次给最终版，每句单独一行，"
                f"句末有标点，不要先写一版再改一版。\n"
                f"【说多少由你决定】日常闲聊一两句就够，别硬凑；真的走心/有分享欲就多掏几句。"
                f"句数跟着内容走。\n"
                f"【不许复读】聊天记录里**你自己说过的句子一句都不许再说**（改一两个字也算）。"
                f"针对 TA 这一句说新的。\n"
                f"【规则】每句 8~30 字、完整；句子层层推进（先反应 → 再感受 → 再细节），"
                f"每句带新信息，不原地重复、不自相矛盾、语气一致、符合人设。\n"
                f"【接住用户的话】每句都接着 TA 刚说的那件事往下走，别另起话题。\n"
                f"【别替用户安排下一步】只回应眼前这句：TA 没说睡/走/忙，就别催睡、别道别，"
                f"也别因为夜深就默认他要睡。\n"
                f"【禁止扮演用户·最重要】只输出你自己说的话，不要替 TA 写回应"
                f"（尤其末尾别写「知道啦/好的/谢谢/嗯嗯」这类），也不要自问自答。\n"
                f"【发图】想发表情包就单独一行写 [sticker:文件名]（只能用表情包列表里的文件名）。"
                f"标记不算句数；不写标记就发不出图。\n"
            )
        else:
            extra = (
                f"\n\n【当前情绪】{emotion_desc}\n"
                f"【任务·最高优先级】像真人连发微信一样，直接输出你的回复，"
                f"每句单独一行（用换行符分隔）。\n"
                f"【说多少由你决定】像真人一样：对方一句话/日常闲聊，一两句就够，"
                f"别硬凑；话题值得展开、你真的有话说（走心/炫耀/安慰/分享欲上来了），"
                f"就多掏几句心里话。句数跟着内容走，绝对不要硬凑句子数。\n"
                f"【最重要】这是一段连贯的回复，不是多个候选答案——**直接输出最终回复，"
                f"绝对不要先写一版再写一版、绝对不要把同一个意思换个说法再说第二遍**。\n"
                # ★ 2026-09-12：原来只禁止"同一段里重复"，没禁止"照抄历史里自己说过的话"。
                #   本地 4B 模型会把上一轮自己的回复整句搬过来（实测连续三轮逐字重复），
                #   所以这里明确点名这条。
                f"【不许复读】上面聊天记录里**你自己已经说过的句子，一句都不许再说一遍**"
                f"（哪怕只改一两个字也不行）。针对 TA 这一句，说新的、没说过的话。\n"
                f"【规则】每句 8~30 字、完整、句末有标点；句子之间像真实对话一样层层推进"
                f"（先反应 → 再感受 → 再细节），每句都带新信息，不要原地重复、不要自相矛盾、"
                f"语气全程一致、符合人设。\n"
                f"【接住用户的话往下说】每句都接着用户刚说的那件事往下走，不要另起无关话题。\n"
            )
            extra += (
                "\n【别替用户安排下一步】只回应眼前这一句话。"
                "用户没有说要睡、要走、要忙，就不要催他睡觉或主动道别，"
                "也不要因为现在是深夜/凌晨就默认他要睡。"
            )
            # ★ 禁止扮演用户·最重要（2026-09-04 修复）
            #   once 模式 AI 一次输出多段时，偶尔会在末段自己演用户的话（"知道啦/好的/谢谢/嗯嗯"），
            #   用户以为是自己在回话，结果整段都是 AI 一人分饰两角。
            extra += (
                "\n【禁止扮演用户·最重要】你只输出你自己（AI）这一方想说的话，"
                "输出的每一句都是你（AI）发出的。"
                "不要在回复里包含 TA（用户）可能说的话——"
                "尤其末尾不要自己写一段「知道啦/好的/谢谢/嗯嗯/收到」之类像 TA 在回应你的内容；"
                "也不要问自己问题然后自己回答（自问自答）。"
                "如果你发现自己要写「用户会怎么回」式的内容，删掉，只留 AI 自己的话。"
            )
            # ★ 发图规则（once 模式也要明确，否则模型只说"发一张给你"却从不写标记）：
            #   [sticker:文件名] 标记不算在句数里；写标记才是"真的发图"，光说"发一张给你"用户看不到图。
            #   2026-09-13 放宽场景限制：想配图就写，不用等特殊场合；程序会把图拆成独立消息发。
            extra += (
                "\n【发图】想发表情包时随时可以写一行 [sticker:文件名]"
                "（文件名必须用前面表情包列表里列出的，不要编造），不需要特别的场景理由。"
                "这个标记不算在句数里。只有真的写出这个标记，程序才会把图片发给用户；"
                "光说「发一张给你」「发个表情包」只是文字，用户看不到任何图。"
            )
        _intent = str(user_intent or "").strip()
        if _intent and _intent != "unknown":
            _desc = INTENT_DESC.get(_intent, "")
            if _desc:
                extra += f"\n【用户这句话的意图】{_desc}，请针对这个意图正面回应。"
        if memory_anchor:
            extra += memory_anchor
        return extra

    def _build_messages(
        self,
        base_messages: list,
        instruction: str,
        emotion: str,
        turn_index: int,
        total_turns: int,
        previous_turns: list = None,
        topics: list = None,
        form: str = "",
        memory_anchor: str = "",
        scene: dict = None,
        user_intent: str = "",
    ) -> list:
        emotion_desc = EMOTION_DESCS.get(emotion, "自然回复")
        system_extra = (
            f"\n\n【当前情绪】{emotion_desc}\n"
            f"【你正在像真人一样连发微信】这是本轮第{turn_index + 1}条（共{total_turns}条），"
            f"现在自然地说这一句：{instruction}\n"
            f"【语气与人设最重要】用你自己的语气和对方聊天，撒娇、用词、亲疏、口头禅"
            f"全部按你的人设和当前情绪自然来，别变成官方客服或机械答题。\n"
            f"【规则】像真人发微信一样自然地说；一句话可长可短，别只蹦一两个字就停；"
            f"不用列表；不要'我'开头的自我介绍。\n"
            f"【接住上下文】接着用户刚说的那件事、或前一条气泡往下走，给出你的反应、"
            f"感受、联想或更深的感受；不要另起无关话题，也不要换个说法重复前面说过的。\n"
        )

        # ★ 别替用户安排下一步（通用约束，与意图是否识别成功无关）
        #   模型会顺着历史模式预测"用户接下来要做什么"：以前聊到"想我"之后
        #   常说晚安，这次就也回晚安；深夜更会默认对方要睡。答非所问多半源于此。
        system_extra += (
            "\n【别替用户安排下一步】只回应眼前这一句话。"
            "用户没有说要睡、要走、要忙，就不要催他睡觉或主动道别，"
            "也不要因为现在是深夜/凌晨就默认他要睡"
            "——哪怕你们以前聊到这里常说晚安。"
        )

        # ★ 禁止扮演用户（2026-09-04 修复）
        #   逐段模式虽然每段只让 AI 输出一句，但末段偶尔会自己演用户的回应
        #   （"知道啦/好的/谢谢/嗯嗯"），看起来像用户自己打的。
        system_extra += (
            "\n【禁止扮演用户】你这一条只输出你自己（AI）的话。"
            "不要写「知道啦/好的/谢谢/嗯嗯/收到」之类像 TA 在回应你的内容；"
            "不要问自己问题然后自己回答。"
        )

        # ★ 用户意图：进一步点明"眼前这句话是来干嘛的"，让回应更对得上
        _intent = str(user_intent or "").strip()
        if _intent and _intent != "unknown":
            _desc = INTENT_DESC.get(_intent, "")
            if _desc:
                system_extra += (
                    f"\n【用户这句话的意图】{_desc}，请针对这个意图正面回应。"
                )

        # ★ 场景递进：命中场景时按层次分配，每条负责一层。
        #   话题大纲规划的是"聊哪几个话题"（并列），递进结构规划的是"每条深入多少"
        #   （纵深）。命中场景时以后者为主，否则容易又变成并列话题各说一句。
        if scene:
            try:
                from .scene import assign_layer
                layer = assign_layer(scene, turn_index, total_turns)
                layers_all = list(scene.get("layers") or ())
                if layer:
                    system_extra += (
                        f"\n【本轮递进结构】当前是「{scene.get('name', '')}」场景，"
                        f"这一轮要层层深入："
                        + " → ".join(
                            f"{i + 1}){l}" for i, l in enumerate(layers_all[:total_turns]))
                        + f"。\n你这条（第{turn_index + 1}/{total_turns}条）负责：{layer}"
                        + "。必须比前一条更深一层、给出新信息或更深的感受，"
                        + "不要停在同一层换个说法，也不要提前跳到后面的层次。"
                    )
            except Exception:
                pass

        # ★ 话题大纲注入：告诉模型这一轮要聊哪几个话题、这条负责哪一个
        topics = [str(t).strip() for t in (topics or []) if str(t).strip()]
        if topics and not scene:
            n = len(topics)
            # 按条数均分话题：4 条 2 话题 → [0,0,1,1]；5 条 3 话题 → [0,0,1,1,2]
            idx = min(turn_index * n // max(total_turns, 1), n - 1)
            system_extra += (
                f"\n【本轮话题规划】这一轮共{n}个话题："
                + "、".join(f"「{t}」" for t in topics)
                + f"。你这条（第{turn_index + 1}/{total_turns}条）主要聊「{topics[idx]}」；"
                + "同一话题内要说出新信息或更深的感受，不要只换个说法重复前面说过的。"
            )

        previous_turns = [str(x).strip() for x in (previous_turns or []) if str(x).strip()]
        if previous_turns:
            system_extra += (
                "\n【本次回复已经发出的气泡】\n"
                + "\n".join(f"- {x[:180]}" for x in previous_turns[-6:])
                + "\n本条只能继续、补充或自然转折；绝对不要复述这些内容，"
                + "也**不要和前面已说的话自相矛盾**（例如前面承认躲着，后面又说没躲），"
                + "更**不要换种说法重复前面已经说过的同一个信息点**（前面已经说想吃糖醋排骨，"
                + "后面就不要再围绕糖醋排骨说第二遍，要给出新信息、新角度或新回应）。"
                + "\n【立场一致·重要】你前面气泡里的**态度、姿态、情绪方向**要和本条保持一致："
                + "前面说「我主动跑去找你」，后面就别说「我等你来找我」这类反过来、自相打架的话；"
                + "前面是撒娇/期待/心疼，后面不要突然冷淡或换成相反的立场。"
            )

        # ★ 表达形式注入：只约束"这句怎么组织"，用词/语气/亲疏仍严格由人格设定决定
        if form:
            try:
                from ..emotion_engine.ai_emotion import EXPRESSION_FORMS
                _fd = EXPRESSION_FORMS.get(form, "")
            except Exception:
                _fd = ""
            if _fd:
                system_extra += (
                    f"\n【本条表达形式】{_fd}\n"
                    f"形式只约束这一句怎么组织；用词、语气、亲疏程度仍严格遵循你的人格设定，"
                    f"不要为了迎合形式而说出不符合人设的话。"
                )

        # ★ 记忆锚点：只在第一条气泡注入，避免每一条都在提往事变成翻旧账
        if memory_anchor and turn_index == 0:
            system_extra += memory_anchor

        result = []
        patched = False
        for msg in base_messages:
            if msg.get("role") == "system" and not patched:
                result.append({
                    "role":    "system",
                    "content": msg["content"] + system_extra,
                })
                patched = True
            else:
                result.append(dict(msg))

        if not patched:
            result.insert(0, {
                "role":    "system",
                "content": system_extra.lstrip(),
            })

        return result
