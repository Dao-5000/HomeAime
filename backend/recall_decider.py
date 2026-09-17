# -*- coding: utf-8 -*-
"""按需召回决策器（用户拍板的"两段式翻库"，2026-09-17）。

## 为什么这么设计
用户要的效果是"AI 自主翻记忆库"，但实测现状是**每轮无条件注入**：
主脑没有 function call 能力（`deepseek_api.py` 里 tools/function_call 零匹配），
真加工具要动主回复链路（流式输出、上下文预算都要改）。
所以采用两段式：
  第一段（本地规则，零成本）：这句话像不像"在问过去的事"？
  第二段（便宜模型，仅在嫌疑时）：**翻不翻、翻什么词、限定什么范围** → 定向检索。

好处：主回复链路一行不动；成本只有"命中嫌疑时"的一次便宜模型调用；
坏处：判断错就会漏翻 —— 所以本地规则刻意**宁滥勿缺**（多翻一次只是多花几分钱，
漏翻才是用户体感里的"她怎么不记得"）。

## 与旧路径的关系
`memory_block()` 的主注入**保留**（常驻：画像/权威事实/约定/近期摘要），
本模块只负责"额外再定向翻一次"，两者叠加而不是替换。
config `RECALL_DECIDER_ENABLED=false` 可一键回到旧口径。
"""
import json
import re

from . import config, db

# ── 第一段：本地粗筛（零成本，宁滥勿缺）────────────────────────────────
# 明确的"问过去"信号
_ASK_PAST = re.compile(
    r"还记得|记不记得|记不记|你忘了|忘了吗|想不想起来|"
    r"上次|上回|那天|那天你|之前(?:说|讲|提|聊)|以前(?:说|提|讲|聊)|"
    r"我们(?:说好|约好|约定|聊过)|你(?:答应|承诺)过?|"
    r"我(?:跟|和)你(?:说|讲)(?:过)?|(?:我)?(?:跟|和)你说过|告诉过你|我说过的|"
    r"什么时候(?:说|讲)|"
    r"第一次|最早|当初|当时"
)
# 话题突然切换的弱信号（"对了…"、"另外"、"话说"）
_TOPIC_SHIFT = re.compile(r"^(?:对了|另外|话说|顺便|哦对|忽然想起|突然想起)")

# 太短的寒暄不值得翻库。
# ★ 注意这里只列"纯语气词/问候"，**不要**用 `在吗|好|行` 这类会吞掉真问题的词：
#   回归用例抓过一次教训 —— 我原先写的 `^(?:…|在吗|好|行|…)[。！？~]*$` 会把
#   "你觉得我今天该早点睡吗" 里的"吗"吞掉（`[。！？!?~～\s]*` 没含"吗"，
#   但整串被 `^…$` 匹配失败后 ELSE 分支又要求"问句必须≥16字"，于是正常提问被拦）。
_TRIVIAL = re.compile(r"^(?:嗯+|哦+|啊+|哈+|嘿+|么么|亲亲|抱抱|晚安|早安|早|"
                      r"睡吧|吃饭了吗|在吗|好|好的|行|收到|ok|OK)"
                      r"[。！？!?~～\s]*$")
# 问句特征（短问句也要允许，别把正常提问当寒暄拦掉）
_QUESTION_TAIL = re.compile(r"[?？]|吗[。！？~～\s]*$|呢[。！？~～\s]*$|"
                            r"对不对[。！？~～\s]*$|是不是[。！？~～\s]*$")


def local_suspect(user_text: str, rounds_since_recall: int = 0) -> dict:
    """第一段：本地规则判断"有没有必要问模型"（零成本）。

    返回 {"suspect": bool, "hint": str, "reasons": [...]}
    刻意宽松：宁可多问一次模型（几分钱），也不要漏掉"她该记得却没记得"（体感伤害大）。
    """
    text = str(user_text or "").strip()
    reasons = []
    if not text:
        return {"suspect": False, "hint": "", "reasons": []}
    if _TRIVIAL.match(text):
        return {"suspect": False, "hint": "寒暄", "reasons": ["trivial"]}
    if _ASK_PAST.search(text):
        reasons.append("ask_past")
    if _TOPIC_SHIFT.match(text):
        reasons.append("topic_shift")
    if rounds_since_recall >= 12:
        reasons.append("long_since_recall")
    # 像问句就问一次（不再要求 ≥16 字 —— 中文里 10 个字已经能问清一件事）
    if _QUESTION_TAIL.search(text) and len(text) >= 8:
        reasons.append("question")
    return {"suspect": bool(reasons), "hint": ",".join(reasons), "reasons": reasons}


_SYSTEM = """你是一个"该不该翻记忆库"的判官。给你 TA 刚说的话和最近几句上下文。

判断：为了回好这一句，AI 是否需要去翻**过去的记忆/聊天原文**（不是常识、不是当前上下文里已有的事）。

需要翻的情况：
- TA 在问过去发生的事、约定、承诺、称呼、喜好、TA 说过的话
- TA 提到"上次/那天/以前"，而当前上下文没有细节
- 话题突然跳到一个以前聊过、但现在上下文里没有内容的话题

不需要翻的情况：
- 单纯寒暄、撒娇、情绪表达、当下的事
- 当前上下文里已经有的信息
- 常识问题

如果要翻，给出**1~2 个检索用短句**（像搜索关键词，别写整句问题）；
可以给时间线索（如"昨天/上周/上个月"）；不确定就给空。

只输出 JSON：
{"need": true, "queries": ["检索短句"], "time_hint": "", "why": "一句话理由"}
"""


def _parse_decision(raw: str) -> dict:
    """解析模型输出（容错 markdown 包裹 / 前后闲话）。"""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        data = json.loads(text[s:e + 1])
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    qs = data.get("queries")
    if isinstance(qs, str):
        qs = [qs]
    if not isinstance(qs, list):
        qs = []
    return {
        "need": bool(data.get("need")),
        # ★ 截断：检索词是拿去 embedding + SQL LIKE 的，模型偶尔会把整段话塞进来，
        #   不截会让定向检索变成一个又慢又没用的长句（回归用例抓到这个缺口）。
        "queries": [str(q).strip()[:120] for q in qs if str(q or "").strip()][:2],
        "time_hint": str(data.get("time_hint") or "").strip()[:20],
        "why": str(data.get("why") or "").strip()[:80],
    }


async def decide(session_id: str, character_id: str, user_text: str,
                 recent_messages: list = None, rounds_since_recall: int = 0) -> dict:
    """两段式决策。返回 {"need","queries","time_hint","why","skipped"}。

    · 第一段不过 → 直接 {"need": False, "skipped": "local"}（零成本）
    · 开关关闭 → {"need": False, "skipped": "disabled"}
    · 没有 key / 调用失败 → {"need": False, "skipped": "error"}（失败即不翻，
      绝不因为决策器坏了而影响主回复）
    """
    try:
        if not bool(config.get("RECALL_DECIDER_ENABLED", True)):
            return {"need": False, "queries": [], "skipped": "disabled"}
    except Exception:
        pass

    local = local_suspect(user_text, rounds_since_recall)
    if not local["suspect"]:
        return {"need": False, "queries": [], "skipped": "local",
                "why": local.get("hint") or ""}

    try:
        from .deepseek_api import chat_once
        model = config.memory_extract_model(character_id)
        # ★ 取 key 的口径与其它后台抽取器保持一致（deepseek_api.py 的既有写法）
        key = config.api_key_for_model(model)
        if not key:
            return {"need": False, "queries": [], "skipped": "nokey"}

        ctx_lines = []
        for m in (recent_messages or [])[-8:]:
            role = "TA" if m.get("role") == "user" else "AI"
            ctx_lines.append(f"{role}：{str(m.get('content') or '')[:120]}")
        prompt = (
            "【最近几句】\n" + ("\n".join(ctx_lines) if ctx_lines else "（无）")
            + f"\n\n【TA 刚说】{str(user_text or '')[:300]}\n\n"
            + f"（本地粗筛命中的信号：{local['hint']}）\n"
            + "请判断是否需要翻记忆库。"
        )
        raw = await chat_once(
            model,
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": prompt}],
            key, temperature=0.1, max_tokens=200,
            reasoning_effort=("low" if config.model_supports_reasoning_effort(model) else None),
        )
        out = _parse_decision(raw)
        if not out:
            return {"need": False, "queries": [], "skipped": "parse"}
        out["skipped"] = ""
        try:
            from . import trace as _trace
            _trace.memory(session_id, character_id,
                          recall_decision={"need": out["need"], "n_q": len(out["queries"]),
                                           "why": out.get("why", ""), "local": local["hint"]})
        except Exception:
            pass
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[RecallDecider] 决策失败(静默，按不翻处理): {type(e).__name__}: {e}", flush=True)
        return {"need": False, "queries": [], "skipped": "error"}


def build_targeted_block(session_id: str, character_id: str, queries: list,
                         limit: int = 1200) -> str:
    """按决策给的检索词做**定向**翻库（小预算，只带最相关的那几条）。

    ★ 必须挡掉"冷启动提示"：`memory_block()` 在没有任何命中时会返回一段
      **初次见面引导语**（"这是你和这个人的第一次对话…别假装已经认识对方"）。
      那是给新用户的兜底文案，如果被当成"她主动想起的往事"注进来，
      会直接和长期关系的事实打架（回归用例 test_targeted_block_empty_without_queries
      抓到的就是这个：检索词是空串/空格时照样返回了那段引导语）。
    """
    from . import memory_manager
    qs = [str(q or "").strip() for q in (queries or [])]
    qs = [q for q in qs if q][:2]
    if not qs:
        return ""
    seen, chunks = set(), []
    for q in qs:
        try:
            block = memory_manager.memory_block(limit=max(300, limit // len(qs)),
                                                query=q, session_id=session_id,
                                                character_id=character_id, boost=True)
        except Exception as e:  # noqa: BLE001
            print(f"[RecallDecider] 定向检索失败({q}): {e}", flush=True)
            continue
        if not block or block in seen:
            continue
        # 冷启动兜底文案不算"翻到的东西"
        if _is_cold_start_block(block):
            continue
        seen.add(block)
        chunks.append(block)
    if not chunks:
        return ""
    head = "【她主动想起的往事（刚才特意去翻的，自然使用，别说是查来的）】"
    return head + "\n" + "\n".join(chunks)


_COLD_START_MARKS = ("【初次见面】", "这是你和这个人的第一次对话", "不要假装已经认识对方")


def _is_cold_start_block(block: str) -> bool:
    b = str(block or "")
    return any(m in b for m in _COLD_START_MARKS)
