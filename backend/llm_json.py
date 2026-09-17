"""LLM 返回 JSON 的容错解析（2026-09-12）。

背景
----
项目里原本有 5 处「让模型只输出 JSON → json.loads」的写法，全都长这样：

    cleaned = str(result or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    data = json.loads(cleaned)

三个坑，任何一个都会让**整批**抽取结果被静默丢弃（只打一行日志，不抛异常）：

1. `max_tokens` 给小了 → 输出被截断 → `json.loads` 抛 "Unterminated string"。
   实测 KnowledgeGraph（max_tokens=1000）每天因此丢 5~10 批；timeline 的
   max_tokens 更是只有 300，几乎没有一次能完整输出。
2. `str.strip("`")` 剥的是**首尾所有**反引号——末尾围栏缺失（被截断）时，
   它会把正文开头的反引号也一起啃掉，反而更容易解析失败。
   （日志里 `result=```j` 这种现场就是它留下的。）
3. 模型偶尔在 JSON 前后附一句解释，直接 loads 就废了。

这里做三件事：剥围栏 → 抠出最外层 {...} / [...] → 解析失败时按
"能救多少救多少"补全括号重试。彻底失败返回 None，调用方保持原有兜底行为。
"""

import json

__all__ = ["loads_llm_json", "strip_code_fence"]


def strip_code_fence(text: str) -> str:
    """去掉 ```json ... ``` 围栏。围栏缺失/被截断时按现状返回，不硬剥反引号。"""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    nl = t.find("\n")
    if nl == -1:
        # 只有一行，形如 "```json" 或 "```" —— 没有正文
        return ""
    body = t[nl + 1:]
    end = body.rfind("```")
    if end != -1:
        body = body[:end]
    return body.strip()


def _outer_spans(text: str):
    """产出最外层 {...} / [...] 的候选切片（最外层优先）。"""
    for op, cl in (("{", "}"), ("[", "]")):
        i = text.find(op)
        j = text.rfind(cl)
        if i != -1 and j > i:
            yield text[i:j + 1]


def _repair_truncated(text: str):
    """把被 max_tokens 截断的 JSON 补成合法 JSON：丢掉尾部不完整元素、补齐括号。

    返回修补后的字符串；无法修补返回 None。
    """
    stack = []
    in_str = False
    esc = False
    safe = 0        # 最后一个"可以安全截断"的位置（元素刚结束 / 逗号之前）
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return text[:i + 1]     # 顶层已闭合，本身就是完整的
            safe = i + 1
        elif ch == ",":
            safe = i                    # 逗号不属于它前面的那个元素
    if not stack:
        return None

    head = text.rstrip()
    if in_str or head.endswith(":") or head.endswith(","):
        head = text[:safe].rstrip()
    while head.endswith(","):
        head = head[:-1].rstrip()

    # 在裁好的 head 上重算未闭合括号，再补上
    stack2 = []
    in_str2 = False
    esc2 = False
    for ch in head:
        if in_str2:
            if esc2:
                esc2 = False
            elif ch == "\\":
                esc2 = True
            elif ch == '"':
                in_str2 = False
            continue
        if ch == '"':
            in_str2 = True
        elif ch in "{[":
            stack2.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack2:
                stack2.pop()
    if in_str2:
        head += '"'
    head += "".join(reversed(stack2))
    return head or None


def loads_llm_json(text):
    """容错解析 LLM 输出的 JSON。成功返回 dict / list，彻底失败返回 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    unfenced = strip_code_fence(raw)

    candidates = []
    for base in (raw, unfenced):
        if not base:
            continue
        candidates.append(base)
        candidates.extend(_outer_spans(base))

    seen = set()
    for cand in candidates:
        cand = cand.strip()
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            return json.loads(cand)
        except Exception:
            pass
        repaired = _repair_truncated(cand)
        if repaired:
            try:
                return json.loads(repaired)
            except Exception:
                pass
    return None
