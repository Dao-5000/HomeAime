# -*- coding:utf-8 -*-
"""
未完成事项管理：
  从用户聊天中识别未来可能需要再次关注的事情（考试、面试、装修、等待结果等）。
  存入 open_loops 表，注入 system prompt 让 AI 在合适的时候自然提起。

★ 2026-09-11 重大修正（"她自己取的猫名字后来忘了"排查结论）：
  原来是**每轮从最近 20 条消息重新抽一遍、只 INSERT 不 UPDATE、也不与既有条目对照**，
  结果同一件事被反复抽出、互相矛盾、越滚越多 ——
  实测助手 513 条 pending，「猫取名」一件事就有 5 条：
     566 用户家中的白猫尚未取名       (19:03，已过时)
     567 用户养的猫已取名为汤圆       (19:15，正确)
     587 用户需为白猫取名并确认…      (20:50，错的)
     589 用户需确认猫的名字           (21:07，错的)
  而 20:50 之后抽出"错的"，是因为抽取器只看得到最近 20 条 ——
  19:00 那次取名早就滑出窗口了，它只看见她在问"是不是汤圆"。
  现在：把该会话**现有的 pending 条目连同 id 一起喂给模型**，要求它认领
  （reuse_id）或者判定已经了结（resolve），只有真正的新事才 INSERT。
"""
import json

from . import db, config
from .deepseek_api import chat_once


OPEN_LOOP_SYSTEM = """
你负责维护用户的「未完成事项」清单。

给你两样东西：
1) 【已有清单】—— 之前已经记录、还没了结的事项（带 id）；可能是空的。
2) 【最近对话】。

你的任务：从最近对话里找出**未来还需要再关注**的事，并且**优先认领已有清单里的条目**。

⚠ 最重要的规则：**先看已有清单，别重复记同一件事。**
- 如果这次说的事和已有清单里某条是**同一件事**（哪怕措辞不同、进展变了），
  必须填 "reuse_id": 那条的 id，**不要新增**。
- 如果同一件事在对话里**已经有了结果 / 已经办完 / 已经确定下来**，
  填 "reuse_id": 那条 id 且 "resolve": true，用来把那条关掉。
  （例：已有「用户家中的白猫尚未取名」，而对话里已经定下叫「汤圆」
   → reuse_id 指向"尚未取名"那条、resolve=true，并另出一条"用户养的猫已取名为汤圆"；
   或者直接把那条更新成正确状态。）
- **只有当已有清单里确实没有对应条目时**，才省略 reuse_id，作为新事项记录。

不要记录：普通闲聊、已经结束的小事、没有未来价值的信息。

输出 JSON 数组，每个元素：
{
"title":"",              // 一句话说清是什么事
"description":"",        // 补充：相关细节、下一步要问什么
"category":"",           // event / task / promise / behavior_promise
"importance":0,          // 0-10
"trigger_time":"",       // 什么时候该提起（如"下周""用户洗完澡后"），不确定就空
"reuse_id":0,            // ★ 认领已有条目时填它的 id，新事项填 0
"resolve":false          // ★ 这件事在对话里已经了结了 → true
}

只输出 JSON 数组本身，不要解释、不要 markdown 代码块。
"""


def apply_open_loops(data, session_id, character_id="default", existing=None) -> int:
    """把**已解析好的**未完成事项列表写库（★ 2026-09-15 从 extract_open_loops 里拆出来）。

    拆出来的原因（省 token ②）：画像/记忆/未完成事项读的是同一批 messages，合并成一次
    模型调用后，这一段"认领既有条目 → 更新 / 关闭 / 新增"的逻辑必须原样复用，
    否则合并版会出现重复条目。返回实际写入条数（新增+更新+关闭）。
    """
    if not isinstance(data, list):
        return 0
    if existing is None:
        try:
            existing = db.get_open_loops(session_id, character_id,
                                         limit=25, order="recent") or []
        except Exception:
            existing = []
    _valid_ids = {int(r.get("id")) for r in existing if r.get("id") is not None}
    _written = 0

    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue

        _imp = item.get("importance", 5)
        try:
            _imp = int(_imp)
        except Exception:
            _imp = 5

        # ── ② 认领：命中既有条目 → 就地更新（而不是再插一条）──
        _reuse = item.get("reuse_id") or 0
        try:
            _reuse = int(_reuse)
        except Exception:
            _reuse = 0

        if _reuse and _reuse in _valid_ids:
            if bool(item.get("resolve")):
                # 这件事在对话里已经了结了 → 关掉它
                try:
                    db.finish_open_loop(_reuse)
                    print(f"[OpenLoop] 认领并关闭 #{_reuse}: {title[:40]}", flush=True)
                    _written += 1
                except Exception as _fe:
                    print(f"[OpenLoop] 关闭失败(静默): {_fe}", flush=True)
            else:
                try:
                    db.update_open_loop(
                        _reuse,
                        title=title,
                        description=str(item.get("description", "") or ""),
                        category=str(item.get("category", "") or "event"),
                        importance=_imp,
                        trigger_time=str(item.get("trigger_time", "") or ""),
                        session_id=session_id,
                        character_id=character_id,
                    )
                    print(f"[OpenLoop] 认领并更新 #{_reuse}: {title[:40]}", flush=True)
                    _written += 1
                except Exception as _ue:
                    print(f"[OpenLoop] 更新失败(静默): {_ue}", flush=True)
            continue

        # ── ③ 真正的新事项才 INSERT ──
        db.add_open_loop(
            session_id,
            title,
            item.get("description", ""),
            item.get("category", "event"),
            _imp,
            item.get("trigger_time", ""),
            character_id
        )
        _written += 1
    return _written


async def extract_open_loops(
    session_id,
    messages,
    character_id="default"
):
    """从最近对话中提取未完成事项，★ 认领既有条目 → 更新，而不是无脑新增。"""
    key = config.memory_key()

    if not key:
        return

    text = "\n".join(
        [
            (
                "用户："
                if m["role"] == "user"
                else "AI："
            )
            +
            str(m.get("content", ""))
            for m in messages
        ]
    )

    # ── ① 把该会话现有的 pending 条目捞出来给模型认领（这是本次修正的核心）──
    #   ★ 必须 order="recent"：抽取器只看最近 20 条消息，能重复的必然是最近刚记的条目；
    #     按 importance 排会被一堆 9 分旧条目占满，近期条目（importance 3~6）进不了清单，
    #     模型看不到就会又新增一条 —— 实测「猫取名」那几条正是这样漏掉的。
    existing = []
    try:
        existing = db.get_open_loops(session_id, character_id,
                                     limit=25, order="recent") or []
    except Exception as _ge:
        print(f"[OpenLoop] 读既有条目失败(静默): {_ge}", flush=True)

    _existing_txt = "（暂无）"
    if existing:
        _existing_txt = "\n".join(
            f"[id={r.get('id')}] {str(r.get('title') or '').strip()}"
            + (f"（{str(r.get('description') or '')[:60]}）" if r.get("description") else "")
            for r in existing
        )

    user_content = (
        "【已有清单】\n" + _existing_txt
        + "\n\n【最近对话】\n" + text[-4000:]
    )

    result = await chat_once(
        config.memory_extract_model(character_id),
        [
            {
                "role": "system",
                "content": OPEN_LOOP_SYSTEM
            },
            {
                "role": "user",
                "content": user_content
            }
        ],
        key,
        temperature=0.2,
        max_tokens=800
    )

    # 模型偶尔会套 markdown 代码块 / 前后带闲话，抠出 JSON 数组
    raw = str(result or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    _s, _e = raw.find("["), raw.rfind("]")
    if _s >= 0 and _e > _s:
        raw = raw[_s:_e + 1]

    try:
        data = json.loads(raw)
    except Exception:
        return

    # ★ 2026-09-15：写库逻辑抽到 apply_open_loops（合并抽取器共用同一段写入逻辑）
    apply_open_loops(data, session_id, character_id, existing=existing)
