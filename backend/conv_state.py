# -*- coding: utf-8 -*-
"""
对话状态追踪（多轮对话优化 · 轻量规则版）

方案目标：让 AI 知道"这次对话聊到哪了、有没有未完成的话题、上一轮问的对方还没答"。
结合现有架构的落地：
- 状态存 SQLite kv（key 带 session_id + character_id 角色隔离），持久不丢
- 每轮用规则更新（零 LLM 成本）：话题推断 / 话题切换 / pending_question
- 注入 prompt 的【本次对话状态】块（简短，控制体积）
"""
import json
import re

from . import db

# 对话状态 kv key（角色隔离）
def _key(session_id, character_id):
    return f"conv_state:{session_id}:{character_id}"


def _default_state():
    return {
        "turn": 0,             # 本轮对话的第几轮
        "main_thread": "",     # 本次对话主线
        "topic": "",           # 当前话题
        "topic_depth": 0,      # 当前话题来回了几次
        "pending_question": "",  # AI 上一轮问的、对方还没正面回答的
        "unresolved": "",      # 没说完就被换掉的话题
        "last_user_msg": "",   # 上一轮用户消息（防重复判断）
    }


def _load(session_id, character_id):
    raw = db.kv_get(_key(session_id, character_id))
    if not raw:
        return _default_state()
    try:
        state = json.loads(raw)
        merged = _default_state()
        merged.update(state)
        return merged
    except Exception:
        return _default_state()


def _save(session_id, character_id, state):
    try:
        db.kv_set(_key(session_id, character_id), json.dumps(state, ensure_ascii=False))
    except Exception:
        pass


# ── 规则：话题推断 / 相关性 / 问句检测 ──────────────

_STOPWORDS = set("的了在是我他她你们和或但也就都吗呢啊把被让给从到这那还又很吧呀哦嗯")
_QUESTION_WORDS = ["什么", "怎么", "为什么", "哪里", "谁", "几", "多少", "是不是", "有没有", "吗", "呢"]


def _clean(text):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or ""))


def _bigrams(text):
    t = _clean(text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _infer_topic(user_msg):
    """从用户消息推断话题：取消息里去掉停用词后的核心串（最长 12 字）。"""
    msg = str(user_msg or "").strip()
    if not msg:
        return ""
    # 取第一个有意义的片段（≥2字连续中文），去掉停用词
    segs = re.findall(r"[\u4e00-\u9fff]{2,}", msg)
    for seg in segs:
        core = "".join(ch for ch in seg if ch not in _STOPWORDS)
        if len(core) >= 2:
            return core[:12]
    return msg[:12]


def _related(topic, msg):
    """当前话题和用户消息是否相关（有共同 2-gram 即相关）。"""
    if not topic or not msg:
        return False
    a = _bigrams(topic)
    b = _bigrams(msg)
    if not a or not b:
        return False
    return bool(a & b)


def _is_question(text):
    t = str(text or "")
    if re.search(r"[?？]", t):
        return True
    return any(w in t for w in _QUESTION_WORDS)


def _extract_question(text):
    """从 AI 回复里截取问句片段（去掉结尾问号后最长 20 字）。"""
    t = str(text or "").strip()
    for sep in ["？", "?"]:
        if sep in t:
            tail = t.rsplit(sep, 1)[0]
            return tail[-20:]
    return t[-20:]


# ── 每轮更新（规则版，零成本）──────────────────────

def update_conv_state(session_id, character_id, user_msg, ai_reply):
    """每轮对话结束后更新状态（纯规则，无 LLM）。"""
    user_msg = str(user_msg or "").strip()
    ai_reply = str(ai_reply or "").strip()
    if not user_msg:
        return None

    state = _load(session_id, character_id)
    state["turn"] = int(state.get("turn", 0)) + 1

    # 话题推断 + 切换检测
    topic = _infer_topic(user_msg)
    cur = state.get("topic", "")
    if cur and _related(topic, cur):
        state["topic_depth"] = int(state.get("topic_depth", 1)) + 1
    else:
        if cur and state.get("topic_depth", 0) >= 2:
            state["unresolved"] = cur   # 聊到一半换了话题 → 记为未完成
        state["topic"] = topic
        state["topic_depth"] = 1

    # 主线：第一轮定主线
    if not state.get("main_thread"):
        state["main_thread"] = topic

    # pending_question：AI 这轮问了 → 记录，等用户下轮回答后清除
    if _is_question(ai_reply):
        state["pending_question"] = _extract_question(ai_reply)
    else:
        state["pending_question"] = ""

    state["last_user_msg"] = user_msg
    _save(session_id, character_id, state)
    return state


def _answered_pending(session_id, character_id, user_msg):
    """用户消息回答了上一轮的 pending_question → 清除。"""
    state = _load(session_id, character_id)
    if state.get("pending_question") and user_msg:
        # 简单启发：用户消息包含上一轮 pending_question 里的核心词 → 视为回答
        pq = state.get("pending_question", "")
        if _related(pq, user_msg):
            state["pending_question"] = ""
            _save(session_id, character_id, state)


# ── 注入 prompt 块 ─────────────────────────────────

def build_conv_state_block(session_id, character_id, user_msg=""):
    """生成【本次对话状态】块；无状态返回空。"""
    if user_msg:
        _answered_pending(session_id, character_id, user_msg)
        # ★ 换话题保护：用户本轮消息若与上一轮的 pending_question / 未聊完话题
        #   不相关，说明已经换了话题，立即清空旧引导。否则模型会被
        #   「你上一轮问过他 X」这种强引导带偏，反复追旧话题（如用户问
        #   「想我了吗」却被引回「宝宝」）。
        _state = _load(session_id, character_id)
        _changed = False
        _pq = str(_state.get("pending_question") or "").strip()
        if _pq and not _related(_pq, user_msg):
            _state["pending_question"] = ""
            _changed = True
        _un = str(_state.get("unresolved") or "").strip()
        if _un and not _related(_un, user_msg):
            _state["unresolved"] = ""
            _changed = True
        if _changed:
            _save(session_id, character_id, _state)
    state = _load(session_id, character_id)
    if int(state.get("turn", 0)) == 0:
        return ""

    lines = []
    if state.get("main_thread"):
        lines.append(f"本次对话主线：{state['main_thread']}")
    if state.get("topic"):
        line = f"当前在聊：{state['topic']}"
        if int(state.get("topic_depth", 1)) >= 3:
            line += "（已经聊了一段时间）"
        lines.append(line)
    if state.get("pending_question"):
        lines.append(f"你上一轮问过他：「{state['pending_question']}」，他还没正面回答")
    if state.get("unresolved"):
        lines.append(f"还没聊完的：{state['unresolved']}")

    if not lines:
        return ""
    return "【本次对话状态】\n" + "\n".join(lines)
