# -*- coding: utf-8 -*-
"""
关系深度增强（结合优化）：专属梗（锚点）+ 边界（拒绝/松动）
复用「说话升级方案」的思想，适配本项目：kv 持久化（不建独立 db）、复用 db.q + chat_once。
"""
import json
import random
import time

from . import db, config
from .deepseek_api import chat_once


# ══════════════════════════════════════════════
# 一、专属梗（锚点）
# ══════════════════════════════════════════════
def _anchor_key(session_id, character_id):
    return f"anchors:{session_id}:{character_id}"


def _load_anchors(session_id, character_id):
    raw = db.kv_get(_anchor_key(session_id, character_id))
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_anchors(session_id, character_id, anchors):
    try:
        db.kv_set(_anchor_key(session_id, character_id), json.dumps(anchors, ensure_ascii=False))
    except Exception:
        pass


def record_anchor(session_id, character_id, anchor_type, title, content, trigger_hint=""):
    """记录一个专属梗（去重：同 title 不重复）。"""
    anchors = _load_anchors(session_id, character_id)
    for a in anchors:
        if a.get("title") == title:
            a["times_used"] = a.get("times_used", 0) + 1
            _save_anchors(session_id, character_id, anchors)
            return
    anchors.append({
        "title": title,
        "content": content[:120],
        "anchor_type": anchor_type,
        "trigger_hint": trigger_hint,
        "times_used": 0,
        "last_used": 0,
        "created_at": time.time(),
    })
    _save_anchors(session_id, character_id, anchors)


def scan_anchor_message(message, session_id, character_id):
    """扫描用户消息，识别并记录专属梗。"""
    msg = message or ""
    if len(msg) < 4:
        return
    # 大量哈哈 / 笑死 → 笑点梗
    if msg.count("哈") >= 4 or "笑死" in msg or "哈哈哈哈" in msg:
        record_anchor(session_id, character_id, "running_joke",
                      title=f"笑点：{msg[:15]}", content=msg[:60],
                      trigger_hint="相关话题出现时自然提起")
    # 专属说法（"记得吗" / "之前那次"）
    if ("记得吗" in msg or "上次" in msg or "那次" in msg) and len(msg) < 40:
        record_anchor(session_id, character_id, "shared_phrase",
                      title=f"专属说法：{msg[:15]}", content=msg[:60],
                      trigger_hint="提到相关话题时自然提起")


def build_anchor_block(session_id, character_id, context=""):
    """注入 prompt 的专属梗块（让 AI 在合适时机自然提起，不强行）。"""
    anchors = _load_anchors(session_id, character_id)
    if not anchors:
        return ""
    # 冷却：6 小时内用过的先不提
    now = time.time()
    fresh = [a for a in anchors if now - a.get("last_used", 0) > 6 * 3600]
    pool = fresh or anchors
    # 按使用次数 + 新鲜度选 2 条
    pool.sort(key=lambda a: (a.get("times_used", 0), -a.get("created_at", 0)))
    picked = pool[:2]
    lines = ["【你们之间的专属梗/默契（只在自然的时候提，别硬塞）】"]
    for a in picked:
        lines.append(f"- {a['content']}")
    return "\n".join(lines)


# ══════════════════════════════════════════════
# 二、边界（偶尔拒绝 + 自己松动）
# ══════════════════════════════════════════════
# 敏感话题（身份质疑已由 identity_answer 处理，这里管其余）
SENSITIVE_TOPICS = {
    "comparison": ["跟真人比", "不如真人", "真人不会", "比不上真人", "还是真人好", "和真人"],
    "creator":    ["谁做的你", "怎么做的你", "你的代码", "你的模型", "底层是什么", "开发者是谁"],
    "feelings_test": ["你真的喜欢我吗", "你骗我的吧", "你只是在执行", "你不是真的"],
}

_RELAX_SECONDS = 1800  # 30 分钟后松动


def _boundary_key(session_id, character_id):
    return f"boundary:{session_id}:{character_id}"


def check_boundary(message, session_id, character_id):
    """检测是否触发边界，返回拒绝回复文本或 None。"""
    msg = (message or "").lower()
    topic = None
    for k, kws in SENSITIVE_TOPICS.items():
        if any(kw in msg for kw in kws):
            topic = k
            break
    if not topic:
        return None
    # 记录边界（含松动时间）
    db.kv_set(_boundary_key(session_id, character_id), json.dumps({
        "topic": topic, "triggered_at": time.time(), "relax_at": time.time() + _RELAX_SECONDS,
    }, ensure_ascii=False))
    return _refusal_text(topic)


def _refusal_text(topic):
    fallbacks = {
        "comparison": "今天不太想聊这个比较，我们聊点别的吧。",
        "creator": "这个……改天再说吧，我不想聊这个。",
        "feelings_test": "你这么问，我有点不知道该怎么说，先不聊这个了好吗。",
    }
    return fallbacks.get(topic, "今天不想聊这个，换个话题好吗。")


async def check_relax(session_id, character_id, char_name="", call_user="你"):
    """检查边界是否松动，返回「AI 自己想说」的过渡句或 None。"""
    raw = db.kv_get(_boundary_key(session_id, character_id))
    if not raw:
        return None
    try:
        b = json.loads(raw)
    except Exception:
        return None
    if time.time() < b.get("relax_at", 0):
        return None
    # 已松动，清除边界
    db.kv_set(_boundary_key(session_id, character_id), "")
    topic = b.get("topic", "")
    key = config.chat_key()
    if not key:
        return None
    prompt = (
        f"你是「{char_name or 'AI'}」。之前你拒绝聊了某个话题（{topic}），现在时间过了，你自己想通了、想主动说一点。\n"
        f"请写一句「我自己想说了」的过渡（{call_user}），像真的想通了一样自然提起，"
        "可以承认之前有点情绪，一两句就够，带点犹豫，不是正式宣布。20~40 字，直接输出。"
    )
    try:
        return (await chat_once(config.get("CURRENT_CHAT_MODEL"), [
            {"role": "user", "content": prompt}
        ], key, temperature=0.9, max_tokens=120)).strip()
    except Exception:
        return None
