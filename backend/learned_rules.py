# -*- coding: utf-8 -*-
"""习惯/规则学习（learned_rules 的**写侧**，2026-09-17 新增）。

## 为什么必须补
`agent/self_modules.py` 里 `learned_rules_block()`（读侧）早就接进了每轮 prompt
（`chat_logic.py:743-747`），`_learn_rule()` 工具也在，但**唯一的写入口是那个工具**，
而工具只跑在"助手模式"的 agent 循环里 —— 用户在日常聊天里说「以后别半夜问我睡没睡」
「记住我不吃香菜」时，这些话**没有任何路径被沉淀成规则**。
真机取证：`%APPDATA%\\HomeAime\\data\\learned_rules\\骨子.json` = `[]`（2 字节），
17.4 万行日志里 `learn_rule` / `learned_rules` 命中 **0** 次。

## 设计（与 style_feedback 同构，保持项目习惯）
  · 粗筛（正则、零成本）命中"教规矩"信号才调一次便宜模型精提
  · 提取结果写进 learned_rules（读侧已在注入），并推「📝 更新记忆」卡片
  · 失败全程静默，绝不打断聊天主流程
  · 与风格反馈的区别：风格反馈管"怎么说话"，这里管"以后要/不要做什么事"
"""
import json
import re

from . import config, db
from .agent import self_modules as _self_mod

# 粗筛：用户在"立规矩 / 教习惯 / 强调以后怎么做"
_COARSE = re.compile(
    r"以后(?:别|不要|不许|不准|记得|要|必须|都)|下次(?:别|不要|记得)|"
    r"记住|记着|听好|说好(?:了)?的|我们(?:说好|约好)|"
    r"我不(?:吃|喝|喜欢|要)你|别再|不要再|别再问|别老|别总|"
    r"说过多少(?:次|遍)|跟你说了多少|又忘|又忘(?:了)?"
)

_MAX_KEEP = 40
_SIMILAR_SKIP = 0.78

_SYSTEM = """你是"相处规矩"提取器。用户刚给 AI 立了一条**长期要遵守的规矩或习惯**。

要求：
- 只提取**可长期执行**的规矩/偏好（"以后别提我前女友"），不要提取一次性的临时要求（"现在帮我查天气"）
- 一句话写清：**不要做什么** 或 **以后要怎么做**，尽量用用户的原词
- 只写这一轮看得到的，不要脑补；听不出明确规矩就返回空

输出 JSON（只输出一个对象）：
{"rule": "一句话规矩（具体可执行）", "is_long_term": true}
"""


def _kv_key(character_id: str) -> str:
    return f"learned_rule_cursor:{character_id or 'default'}"


def coarse_hit(user_text: str) -> bool:
    """粗筛：这句话像不像在立规矩（零成本）。"""
    return bool(user_text and _COARSE.search(str(user_text)))


def load_rules(character_id: str) -> list:
    """直接复用 self_modules 的存储（读侧已在用它，别搞两套）。"""
    return _self_mod.load_rules(character_id)


def add_rule(character_id: str, text: str) -> bool:
    """新增一条规则；与已有条目相似则跳过。返回是否新增。"""
    text = str(text or "").strip()
    if not text:
        return False
    rules = load_rules(character_id)
    if any(_similar(text, str(r.get("text") or "")) >= _SIMILAR_SKIP for r in rules):
        return False
    new_id = max((int(r.get("id") or 0) for r in rules), default=0) + 1
    rules.append({
        "id": new_id,
        "text": text[:300],
        "created": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "source": "auto",
    })
    _self_mod._save_rules(character_id, rules[:_MAX_KEEP])
    return True


def _similar(a: str, b: str) -> float:
    import difflib
    try:
        return difflib.SequenceMatcher(None, a.strip(), b.strip()).ratio()
    except Exception:
        return 0.0


def _parse_rule(raw: str) -> str:
    """从模型输出里抠出 rule 字段（容忍 markdown 包裹与前后闲话）。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return ""
    try:
        data = json.loads(text[s:e + 1])
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    if data.get("is_long_term") is False:
        return ""
    return str(data.get("rule") or "").strip()


async def detect_and_learn_rule(session_id: str, character_id: str,
                                user_text: str, recent_ai_text: str = "") -> dict:
    """粗筛 → 便宜模型精提 → 沉淀成规则。返回 {learned, card}；未命中返回 {}。

    card=True 表示这是新学到的（前端弹「📝 更新记忆」卡片）。
    """
    out = {}
    if not coarse_hit(user_text):
        return out
    try:
        from .deepseek_api import chat_once
        key = config.api_key_for_model(config.memory_extract_model(character_id))
        if not key:
            return out
        import datetime as _dt
        prompt = (
            f"（当前时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M')}）\n"
            f"TA 刚说了：{str(user_text or '')[:300]}\n"
            + (f"AI 刚才的回复：{str(recent_ai_text or '')[:300]}\n" if recent_ai_text else "")
            + "判断这是不是在给 AI 立一条长期规矩，并提炼成一句话。"
        )
        raw = await chat_once(
            config.memory_extract_model(character_id),
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": prompt}],
            key, temperature=0.2, max_tokens=160,
        )
        rule = _parse_rule(raw)
        if not rule or len(rule) < 4:
            return out
        added = add_rule(character_id, rule)
        if added:
            print(f"[LearnedRule] 沉淀规矩 char={character_id}: {rule[:60]}", flush=True)
        return {"learned": rule, "card": bool(added)}
    except Exception as e:  # noqa: BLE001
        print(f"[LearnedRule] 学习失败(静默): {type(e).__name__}: {e}", flush=True)
        return out
