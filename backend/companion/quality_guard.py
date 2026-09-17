# -*- coding: utf-8 -*-
"""
对话质量守卫层 v1.0
- OOC双层检测（快速规则 + LLM二次确认）
- 话术去重（最近N条相似度）
- 长度自适应
"""
import re
import asyncio
from difflib import SequenceMatcher
from .. import db

# ── OOC快速规则：命中任意一条即视为OOC嫌疑
_OOC_PATTERNS = [
    r"作为(一个)?AI",
    r"作为语言模型",
    r"我是(一个)?人工智能",
    r"我没有(真实的)?情感",
    r"我无法(真正)?感受",
    r"根据(您|你)的(问题|需求|描述)",
    r"以下是(我的)?回答",
    r"希望(这个)?(回答|解答)(对你)?有帮助",
    r"如果(你|您)还有(其他)?问题",
    r"祝(您|你)(生活|工作|学习)愉快",
    r"我(理解|明白)(您|你)的(感受|心情|想法)",
    r"请问(还有什么)?(可以|能)帮(您|你)",
    r"感谢(您|你)的(提问|分享|信任)",
]
_OOC_RE = re.compile("|".join(_OOC_PATTERNS))

# ── 去重相似度阈值 ─────────────────────────────────────
# ★ 0.48 太激进：会把「围绕同一话题、换个角度推进」的连续气泡误判成重复
#   而删掉，导致 AI 明明很开心却显得话少、冷淡。语义层面的"换个词重复"已由
#   dedup_check_async 的灰色区间交给 LLM 判定，字面层这里只需拦"几乎一字不差
#   的复读"，阈值回到 0.72 安全区。
_DEDUP_THRESHOLD = 0.72
# 字符集合重合度：同上，抬回安全区，只拦骨架几乎一致的真复读
_DEDUP_JACCARD = 0.70

# 含否定字但整体不是否定语义的词，统计否定词前先剔除。
# 否则「很不错 / 挺好的」会被当成"语义相反"放行，反而漏掉真正的重复。
_NEG_CHARS = set("不没无别莫未非")
_NEG_EXCEPT = ("不错", "不赖", "不愧", "不禁", "不俗", "不凡", "不亦",
               "不得不", "差不多", "了不起", "忍不住", "恨不得")


def _norm_for_dedup(text: str) -> str:
    """归一化：去掉标点/空白/大小写差异，只留字面骨架。"""
    return re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE).lower()


def _neg_count(text: str) -> int:
    t = str(text or "")
    for w in _NEG_EXCEPT:
        t = t.replace(w, "")
    return sum(1 for ch in t if ch in _NEG_CHARS)


def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

# ── LLM OOC确认 prompt
_OOC_CHECK_PROMPT = """你是一个严格的角色扮演质量检测员。
判断下面这段AI回复是否发生了"出戏"（OOC：out of character）。

出戏的定义：
- 突然用AI客服/助手口吻说话（如"作为AI""希望对你有帮助"）
- 突然变成百科/说教语气，丢失人设中的个性
- 用了人设中明确禁止的表达方式

角色设定摘要：
{personality_summary}

待检测回复：
{reply}

只回答：OOC 或 OK
不要解释，只回答这两个词之一。"""


def quick_ooc_check(text: str) -> bool:
    """
    快速规则检测是否OOC。
    返回 True = 疑似OOC，False = 正常。
    耗时 <1ms。
    """
    if not text:
        return False
    return bool(_OOC_RE.search(text))


async def llm_ooc_check(
    reply: str,
    personality_summary: str,
    api_key: str,
    model: str
) -> bool:
    """
    LLM二次确认OOC（只在快速规则命中时才调用）。
    返回 True = 确认OOC，False = 正常。
    """
    try:
        from ..deepseek_api import chat_once
        prompt = _OOC_CHECK_PROMPT.format(
            personality_summary=personality_summary[:300],
            reply=reply[:500]
        )
        result = await chat_once(
            model,
            [{"role": "user", "content": prompt}],
            api_key,
            temperature=0.0,
            max_tokens=5
        )
        return str(result or "").strip().upper().startswith("OOC")
    except Exception as e:
        print(f"[QualityGuard] LLM OOC确认失败(静默降级): {e}", flush=True)
        return False  # 降级：LLM失败时不拦截


def _similarity(a: str, b: str) -> float:
    """字符串相似度（0~1）"""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:200], b[:200]).ratio()


def is_repeat_of(new_text: str, old_text: str) -> bool:
    """判定两句话是否属于「同一句话换个说法」。

    纯字面比对（只靠 SequenceMatcher）拦不住中文的换词重复，这里叠三路信号：
      1. 子串包含 —— 本轮是多气泡拼接、历史是单条时长度悬殊，
         相似度会被稀释，但"整句被包住"仍是明确的重复信号；
      2. 序列相似度 —— 句式骨架一致（换主语/换宾语/换语气词）；
      3. 字符集合重合 —— 换词后骨架字符仍在，补上序列比值的盲区。

    例外：否定词数量不一致时放行。像「我喜欢」/「我不喜欢」字面高达 0.88，
    但语义相反，判成重复会让 AI 不敢正常表达态度。
    """
    n = _norm_for_dedup(new_text)
    o = _norm_for_dedup(old_text)
    if not n or not o:
        return False

    # 短句（"嗯嗯"/"好的"）相似度极不稳定，只认完全一样或整句被包住
    if len(n) < 6 or len(o) < 6:
        if n == o:
            return True
        short, long_ = (n, o) if len(n) <= len(o) else (o, n)
        return len(short) >= 4 and short in long_

    if _neg_count(n) != _neg_count(o):
        return False

    if n in o or o in n:
        # ★ 短句被整句包住才算复读：6 字以上的短句被完整包含才是"换个说法
        #   再说一遍"；太短的碎片被包住只是正常承接（如"我刚吃饭"vs"我刚吃饭呢"），
        #   不该误删，否则会把 AI 的连续气泡砍到只剩一两条、显得冷。
        short, long_ = (n, o) if len(n) <= len(o) else (o, n)
        return len(short) >= 6
    if SequenceMatcher(None, n, o).ratio() >= _DEDUP_THRESHOLD:
        return True
    return _jaccard(n, o) >= _DEDUP_JACCARD


def recent_ai_texts(session_id: str, character_id: str, limit: int = 8) -> list:
    """取最近 N 条 AI 说过的话，用于在重新生成时明确告诉模型要避开什么。

    ★ 关键：db.recent_messages 返回 sqlite3.Row，**没有 .get() 方法**。
      旧版用 m.get(...) 永远抛 'sqlite3.Row' object has no attribute 'get'，
      被 except 静默吞掉，导致"防复读"prompt 永远注入为空——
      这就是 AI 反复复读的真正根因。改为 m["content"] / m["role"]。
    """
    try:
        recent = db.recent_messages(session_id, limit * 2, character_id)
        return [
            str(m["content"] or "").strip()
            for m in recent
            if m["role"] == "assistant" and str(m["content"] or "").strip()
        ][-limit:]
    except Exception as e:
        # 不再"静默"——真实异常冒出来，方便定位
        print(f"[QualityGuard] 取历史发言失败: {type(e).__name__}: {e}", flush=True)
        return []


def dedup_check(
    new_reply: str,
    session_id: str,
    character_id: str,
    lookback: int = 8
) -> bool:
    """
    检测新回复是否与最近N条AI回复过于相似（纯字面判定，不调模型）。
    返回 True = 重复，False = 正常。
    """
    try:
        for old in recent_ai_texts(session_id, character_id, lookback):
            if is_repeat_of(new_reply, old):
                return True
        return False
    except Exception as e:
        print(f"[QualityGuard] 去重检测失败(静默): {e}", flush=True)
        return False


# ── 语义去重（灰色区间交给模型判定）──────────────────────────
# 字面比对有天花板：把"你今天是不是又熬夜了"改写成"你是不是又很晚才睡"，
# 字面分只有 0.4 出头，跟正常对话（0.25 左右）挨得太近，再降阈值就开始误伤。
# 所以分三档：够高直接判重复、够低直接放行，中间的灰色区间才问一次模型。
_GRAY_LOW = 0.30          # 低于此不可能是重复，连问都不问
_GRAY_HIGH = _DEDUP_THRESHOLD   # 高于此字面已能定案

_LLM_JUDGE_PROMPT = """判断下面两句话是不是在表达同一个意思。
注意：只是换个词、换个说法、换种句式，但意思相同的，也算同一个意思。

A：{a}

B：{b}

只回答 YES 或 NO，不要任何解释。YES=同一个意思，NO=不是同一个意思。"""


def _pair_score(new_text: str, old_text: str) -> float:
    """两句话的字面相似度（取序列比值与集合重合的较大者），用于灰色区间判定。"""
    n = _norm_for_dedup(new_text)
    o = _norm_for_dedup(old_text)
    if not n or not o:
        return 0.0
    if n in o or o in n:
        return 1.0
    return max(SequenceMatcher(None, n, o).ratio(), _jaccard(n, o))


async def _llm_is_same_meaning(a: str, b: str, chat_once_fn, model: str,
                               api_key: str) -> bool:
    """让模型判断两句话是否同义。任何失败都返回 False（宁可漏判也不误伤）。

    项目里的 chat_once 有同步和异步两种实现（调用方用 iscoroutinefunction
    判断），这里同样要两者都兼容，否则其中一种会直接抛错。
    """
    try:
        import asyncio
        import inspect
        prompt = _LLM_JUDGE_PROMPT.format(a=str(a)[:300], b=str(b)[:300])
        ret = chat_once_fn(model, [{"role": "user", "content": prompt}],
                           api_key, temperature=0.0, max_tokens=8)
        if inspect.isawaitable(ret):
            raw = await asyncio.wait_for(ret, timeout=10.0)
        else:
            raw = ret
        return str(raw or "").strip().upper().startswith("YES")
    except Exception as e:
        print(f"[QualityGuard] 语义判定失败(按不重复处理): {e}", flush=True)
        return False


async def dedup_check_async(
    new_reply: str,
    session_id: str,
    character_id: str,
    chat_once_fn=None,
    model: str = "",
    api_key: str = "",
    lookback: int = 8,
) -> bool:
    """去重判定（先字面，灰色区间再问模型）。返回 True = 重复。

    正常对话和明显重复都走字面判定，零额外开销；
    只有落在 0.30~0.48 之间、字面拿不准的那一对，才调一次模型。
    """
    try:
        olds = recent_ai_texts(session_id, character_id, lookback)
    except Exception as e:
        print(f"[QualityGuard] 取历史失败(静默): {e}", flush=True)
        return False

    best_score, best_old = 0.0, None
    for old in olds:
        if is_repeat_of(new_reply, old):
            return True
        s = _pair_score(new_reply, old)
        if s > best_score:
            best_score, best_old = s, old

    if best_old is None:
        return False
    if best_score < _GRAY_LOW:
        return False

    # 灰色区间：字面像又不像，交给模型定夺（只对最相似的一对，最多一次调用）
    if chat_once_fn is not None and api_key:
        return await _llm_is_same_meaning(
            new_reply, best_old, chat_once_fn, model, api_key)
    return False


def adaptive_length_hint(user_text: str) -> str:
    """
    根据用户消息长度返回回复长度建议（注入prompt）。
    """
    length = len(user_text.strip())
    if length <= 5:
        return "用户消息很短，回复自然一点，2~3 句为宜，别冷场、别敷衍。"
    elif length <= 20:
        return "用户消息较短，回复自然简洁，2~4句为宜。"
    elif length <= 80:
        return "正常长度消息，回复3~6句，保持对话节奏。"
    else:
        return "用户消息较长，可以适当展开，但避免超过200字。"


def build_personality_summary(character_config: dict, personality_state: dict) -> str:
    """
    从角色配置和人格状态中提取关键摘要，供OOC检测使用。
    """
    parts = []
    if character_config:
        name = character_config.get("name", "")
        persona = character_config.get("personality", "")
        if name:
            parts.append(f"角色名：{name}")
        if persona:
            parts.append(f"人设：{persona[:150]}")
    if personality_state:
        style = personality_state.get("speaking_style", "")
        forb  = personality_state.get("forbidden_phrases", "")
        if style:
            parts.append(f"说话风格：{style}")
        if forb:
            parts.append(f"禁止表达：{forb}")
    return "\n".join(parts) if parts else "保持人设，不要用AI客服口吻。"
