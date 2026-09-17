# -*- coding: utf-8 -*-
"""
风格反馈学习（「📝 更新记忆」卡片后端）：
  用户批评 AI 的回应方式（"看到你这样回复我就恼火""别跟我讲道理"）时，
  一次轻量 LLM 提取 → 结构化为持久偏好 → 常驻注入后续每轮聊天，
  并向前端推「📝 更新记忆」卡片（用户亲眼看到 TA 把话记下来了）。

设计：
  - 粗筛（正则）命中不满信号才调 LLM 精提，省调用
  - 存储 kv（按角色隔离，最多 8 条，相似去重），常驻注入
  - 失败全程静默，不影响聊天主流程
"""
import re
import json
import difflib

from . import config, db

_COARSE = re.compile(
    r"烦死了?|恼火|火大|失望|敷衍|套路|说教|讲道理|分析什么|别这样回|你这样回复|"
    r"又说教|别分析|不想听你|套话|机器人|像AI|像 ai|像AI|"
    # ★ 2026-09-17 补：用户真实会说的"客服腔"一类批评。
    #   原正则漏了「你说话像客服」，而这正是本项目用户最常用来表达
    #   "回复太模板化"的说法（回归用例 test_coarse_hit_recognizes_criticism 抓到）。
    r"客服|官方腔|模板(?:化|腔)|公事公办|背课文|念稿|(?:太|好)?生硬|"
    r"冷冰冰|没有感情|不像人|像在念"
)

_MAX_KEEP = 8           # 每角色最多保留条数（新的顶旧的）
_SIMILAR_SKIP = 0.70    # 与已有条目相似度 ≥0.7 视为重复，不再记


def _kv_key(character_id: str) -> str:
    return f"style_feedback:{character_id or 'default'}"


def coarse_hit(user_text: str) -> bool:
    """粗筛：用户消息里是否有对回应方式不满的信号（零成本）。"""
    return bool(user_text and _COARSE.search(str(user_text)))


def _load_list(character_id: str) -> list:
    try:
        raw = db.kv_get(_kv_key(character_id))
        data = json.loads(raw) if raw else []
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_list(character_id: str, items: list) -> None:
    try:
        db.kv_set(_kv_key(character_id), json.dumps(items[-_MAX_KEEP:], ensure_ascii=False))
    except Exception:
        pass


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()


def guidance_block(character_id: str) -> str:
    """常驻注入块：TA 在意/反感的回应方式（从真实批评中学到的）。
    无记录返回空串。由 enrich_messages 每轮调用。"""
    items = _load_list(character_id)
    if not items:
        return ""
    lines = ["【TA 在意/反感的回应方式（TA 亲口批评后你学到的，务必遵守）】"]
    lines += [f"- {it}" for it in items]
    lines.append("违反上面任何一条，TA 都会真的难过——宁可话少，不要踩雷。")
    return "\n".join(lines)


def _is_duplicate(learned: str, items: list) -> bool:
    for it in items:
        if _similar(learned, it) >= _SIMILAR_SKIP:
            return True
    return False


_SYSTEM = """你是偏好提取器。用户刚批评了 AI 的回应方式（或表达了不满/失望）。
从用户的话里提取一条**具体、可执行**的回应方式偏好，让 AI 之后照做。

要求：
- learned 一句话写清：TA 不喜欢什么 +（如果能听出来）TA 希望什么样
- 必须具体（"不要在TA吐槽时讲道理或分析，要先站在TA这边"），
  不要空泛（"要好好回复"这种没用）
- 只提炼这一轮批评里能看到的问题，不要脑补

输出 JSON（只输出一个对象）：
{"learned": "一句话偏好（具体可执行）"}
"""


async def detect_and_learn(session_id: str, character_id: str, user_text: str,
                           recent_ai_text: str = "") -> dict:
    """检测批评 → LLM 提取偏好 → 存储。返回 {learned, card}；未命中返回 {}。

    card=True 表示这是新学到的（前端可弹「📝 更新记忆」卡片）；
    与已有条目重复时仍会刷新存储，但 card=False（不重复弹卡）。
    """
    out = {}
    if not coarse_hit(user_text):
        return out
    try:
        from .deepseek_api import chat_once
        key = config.api_key_for_model(config.memory_extract_model(character_id))
        if not key:
            return out
        recent = _load_list(character_id)
        _now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
        prompt = (
            f"（当前时间：{_now}）\n"
            f"TA 刚说了：{str(user_text or '')[:300]}\n"
            + (f"AI 刚才的回复：{str(recent_ai_text or '')[:300]}\n" if recent_ai_text else "")
            + "TA 在批评 AI 的回应方式。请提炼一条具体、可执行的回应方式偏好。"
        )
        raw = await chat_once(
            config.memory_extract_model(character_id),
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": prompt}],
            key, temperature=0.2, max_tokens=200,
        )
        raw = str(raw or "").strip()
        m = re.search(r"\{[^{}]*\}", raw, re.S)
        if not m:
            return out
        try:
            data = json.loads(m.group(0))
        except Exception:
            return out
        learned = str(data.get("learned") or "").strip()
        if not learned or len(learned) < 6:
            return out
        items = _load_list(character_id)
        if _is_duplicate(learned, items):
            return out
        items.insert(0, learned)
        _save_list(character_id, items)
        out = {"learned": learned, "card": True}
        print(f"[StyleFeedback] 新增风格偏好: {learned[:60]!r}", flush=True)
    except Exception as e:
        print(f"[StyleFeedback] 提取失败(静默): {e}", flush=True)
    return out
