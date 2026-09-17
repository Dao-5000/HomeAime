# -*- coding: utf-8 -*-
"""
AI 承诺兑现：
  识别 AI 自己说出口的「带具体时间的承诺/约定」（如「八点半我准时喊你」「明早叫你」），
  写入 open_loops（category=ai_promise），到点由 idle_agent 优先触发兑现，
  避免「AI 自己答应的事做不到」——这是最伤体感的穿帮。

设计：
  - 只在 AI 回复「疑似含承诺」时（正则粗筛）才调 LLM 精确提取，省 token。
  - 提取是后台异步任务，失败静默，绝不影响聊天主流程。
  - 复用现有 open_loops 表，不新建表；trigger_time 存「HH:MM」供到点判断。

★ 2026-09-16 控制器裁决①（一次委托，一条记录）——本模块只负责它自己那一半：
  · tasks      = 用户**委托的定时提醒**（唯一落库处，到点提醒由它负责）；
  · open_loops = **AI 自己的承诺** + **长期/循环约定**（**不做"一次性到点提醒"的落库**）。
  于是：① 同一件委托已落成 tasks 行时，这里不再补一次性 open_loops 行（`_timed_task_covers`）；
        ② 长期/循环约定（每天/每晚…）存"循环约定原文"，前台不建一次性 task
           （`is_recurring_agreement` / `recurring_phrase`，前台对应 chat_logic）。
★ 2026-09-16 控制器裁决②（一张判定表）：时段/日期语义不再在本模块自写，
  一律走 `chat_logic.resolve_hour_24` / `resolve_phrase_datetime`（见 resolve_trigger_phrase）。
"""
import json
import re
from datetime import datetime, timedelta

from . import config, db

# 粗筛：时间承诺 = 承诺动作词 + 时间词同时出现才值得调 LLM
# ★ 动词双向覆盖：AI 视角（叫你/提醒你）+ 用户委托视角（叫我/提醒我——
#   「1 点叫**我**起床」之前完全不命中粗筛，委托静默丢失 = 记不住的根因之一）
_PROMISE_VERB = re.compile(r"提醒|喊你|叫你|找你|催你|叫我|喊我|催我|找我|叫醒|记得|别忘了|带你去|带我去|陪你|帮你")
# ★ 时间词粗筛：阿拉伯数字时间（11点/23:00）+ 中文数字时间（十一点/八点）+ 相对/时段词。
#   之前缺「中文数字+点」（如"十一点提醒你"），导致 AI 用中文报时点时承诺根本进不了粗筛，
#   静默漏提取 → 承诺兑现/记忆双失效（「提醒 11 点睡觉却记不住」的提取层隐患）。
_TIME_HINT = re.compile(r"\d{1,2}\s*[点:：]\s*\d{0,2}|[零一二三四五六七八九十两]{1,3}\s*[点:：]|分钟后|小时后|明早|明晚|今晚|明天|中午|下午|晚上|夜里|凌晨|早上|傍晚|半|一刻")

# 粗筛：约定 = 双方约好某时间见面/聊天/一起做某事（如"九点我们在小院子见面"）
_AGREEMENT_VERB = re.compile(r"见面|碰头|碰面|见你|见一下|约你|约好|说好|讲好|约定|等你|找你|找我|一起|聚|见个面|去|逛|打|看|聊|玩")

# 粗筛：行为承诺 = 承诺某种行为约定/克制（等信号、不插嘴、先别回等），不带具体时间
_BEHAVIOR_PROMISE = re.compile(
    r"等你|等你的|等.*信号|不插嘴|别插嘴|先别回|先不回|别回我|我闭嘴|不回了|你发完|你说完|你讲完|"
    r"慢慢说|慢慢讲|我先不|等.*叫|发完.*叫|说完.*叫|讲完.*叫|我再回|我再说话|先听你"
)


def _looks_like_promise(text: str) -> bool:
    if not text:
        return False
    # 时间承诺：动作词 + 时间词
    if _PROMISE_VERB.search(text) and _TIME_HINT.search(text):
        return True
    # 约定：约定动词 + 时间词（"九点见面""明晚一起打游戏"）
    if _AGREEMENT_VERB.search(text) and _TIME_HINT.search(text):
        return True
    # 行为承诺：行为约定词
    return bool(_BEHAVIOR_PROMISE.search(text))


# ══════════════════════════════════════════════════════════════════
# ★ 2026-09-16 控制器裁决①：一次委托，一条记录 —— 两个库的分工
#   · tasks        = 用户**委托的定时提醒**（唯一落库处，到点提醒由它负责）；
#   · open_loops   = **AI 自己的承诺** + **长期/循环约定**（不做"一次性到点提醒"的落库）。
#   两条闸门：
#     ① `_timed_task_covers`：同一件委托已落成 tasks 行 → 这里不再补一次性 open_loops 行；
#     ② `is_recurring_agreement`：长期/循环约定由前台反向让路（不建一次性 task）。
# ══════════════════════════════════════════════════════════════════

# 「长期/循环约定」的写法。★ 只认**循环域真能兑现**的形式：`_recurring_hhmm` 支持
# `^每[天日]`，每晚/每早经 `_norm_trigger_phrase` 归一成每天晚上/每天早上。
# 「每周/每月」刻意不在内：`_is_due` 对它们只比时刻（会退化成每天触发），
# 与其写进错的循环语义，不如维持现状（前台仍按一次性处理）。
_RECURRING_AGREEMENT = re.compile(r"每[天日]|每晚|每早|天天")


def is_recurring_agreement(text: str) -> bool:
    """是不是「长期/循环约定」（每天/每日/每晚/每早/天天）→ 归 open_loops，前台不建一次性 task。"""
    if not text:
        return False
    return bool(_RECURRING_AGREEMENT.search(str(text)))


def recurring_phrase(text: str) -> str:
    """从句子里抽出「循环约定」短语：「以后每天两点半提醒我睡觉」→「每天两点半提醒我睡觉」。

    `_recurring_hhmm` / `resolve_trigger_phrase` 只认短语本身，整句虽也能解析，
    但存库要短（prompt 里会展示）。取「每/天天」开头到标点或空白为止的一段（≤10 字）。
    """
    m = re.search(r"(?:每[天日]|每晚|每早|天天)[^，,。.！!？?；;、\s]{0,10}", str(text or ""))
    return m.group(0) if m else ""


def _minute_key(trigger_time) -> str:
    """'2026-09-16T23:00:00' / '2026-09-16 23:00' → '2026-09-16 23:00'（分钟精度）；认不出给 ''。"""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{1,2}):(\d{2})", str(trigger_time or ""))
    if not m:
        return ""
    return "%s %02d:%s" % (m.group(1), int(m.group(2)), m.group(3))


def _timed_task_covers(session_id, character_id, content, title, trigger_time) -> bool:
    """tasks 里是否已有"同一件事 + 同一分钟"的定时行（裁决①：一次委托一条记录）。

    ★ content 与 title 两个候选都比：委托类 content 是纯事项（「睡觉」），
      模型偶尔把事项写进 title（「23:00 提醒用户睡觉」），只比一个会漏。
    ★ 不看 status：同事项同分钟的行无论 pending/done 都是**同一次委托**
      （add_task 的幂等键也按 session+时刻+content 去重），补记一条只会提醒两次。
    """
    want = _minute_key(trigger_time)
    if not want:
        return False
    cands = [c for c in (str(content or "").strip(), str(title or "").strip()) if len(c) >= 2]
    if not cands:
        return False
    try:
        rows = db.q("SELECT content, trigger_time FROM tasks WHERE session_id=? AND character_id=?",
                    (session_id, character_id), fetch=True) or []
    except Exception:
        return False
    for r in rows:
        _row = dict(r)
        _tc = str(_row.get("content") or "").strip()
        if len(_tc) < 2 or _minute_key(_row.get("trigger_time")) != want:
            continue
        for c in cands:
            if c == _tc or c in _tc or _tc in c:
                return True
    return False


_SYSTEM = """你是承诺提取器。判断这轮对话（用户的话 + AI 的回复）里是否包含「带时间的承诺/约定」。用户委托的提醒和 AI 自己的承诺都要提取：

【用户委托类承诺（★最重要，历史上最容易漏）】用户要求 AI 在某个时间做某事：
- 用户：「1 点叫我起床」→ 是（title：13:00 叫用户起床）
- 用户：「11 点提醒我睡觉」→ 是
- 用户：「五点喊我」→ 是
- 用户：「半小时后提醒我喝水」→ 是
- 用户：「以后每天两点半提醒我睡觉」→ 是（★每天约定，别漏；content：睡觉，trigger_time：02:30）
- AI 回复确认（"好呀""好的"）不影响判定，委托本身就是承诺
- ★ 只给了「用户说」、没有 AI 回复时（本轮 AI 还没开口）照样判——委托本身就是承诺

【时间承诺】AI 说它在未来某个时间点会主动做某事：
- AI："八点半我准时喊你" → 是
- AI："明早八点叫你起床" → 是
- AI："半小时后提醒你喝水" → 是

【约定（双方约好某时间见面/聊天/一起做某事）】
- "九点我们在小院子见面" / "明晚一起打游戏" → 是

【行为承诺】不带具体时间，但承诺了某种行为约定/克制（等、不插嘴、先不回复等）：
- "我等你把话说完" / "我先不回了，你发完叫我" → 是

不要提取这些（不是承诺，只是情绪/客套）：
- "我想你""我陪你""我这就去""好呀""嗯嗯"

【时间歧义推断（★结合给出的当前时间，必须做）】
- 会给出当前时间。对话里说的「X点」必须结合当前时间换算成正确的 24 小时制：
  - 当前 07:15，用户说「1 点叫我起床」→ 指今天下午 13:00（用户凌晨 7 点才睡要睡到下午），
    绝不是明天凌晨 01:00！
  - 当前 22:00，说「8 点提醒我」→ 指明天早上 08:00
  - 有「下午/中午/晚上/凌晨/早上/明早/明晚」字样时按字面
  - 拿不准就选「离当前时间最近的下一个该时刻」
- trigger_time 一律输出 24 小时制 HH:MM；trigger_day 按推断填 today/tomorrow

输出 JSON（只输出一个对象，不要数组、不要多余解释）：
{"is_promise": true/false, "promise_type": "time 或 behavior", "promise_source": "user_delegation / ai_promise / agreement / behavior", "trigger_time": "HH:MM（24小时制，仅 time 类型填；behavior 类型留空）", "trigger_day": "today / tomorrow / YYYY-MM-DD / relative（仅 time 类型填）", "relative_minutes": 整数（trigger_day=relative 时填「从现在起多少分钟后」，否则填 0）, "content": "用户要提醒的那件事本身（见下方规则）", "title": "AI 视角的一句话承诺（如：13:00 叫用户起床 / 等用户发完话再回复）", "importance": 1-10}

promise_source 填写规则（★ 决定系统建不建「提醒任务」，必须准确）：
- "user_delegation"：用户委托 AI 在某个时间做某事（提醒我 / 叫我 / 喊我 / 催我 / 记得叫我 / 以后每天几点…）——★最重要
- "ai_promise"：AI 自己说它在未来某时刻会主动做某事
- "agreement"：双方约好某时间见面/聊天/一起做某事（"九点我们在小院子见面"）
- "behavior"：不带时间的行为约定/克制（"我等你把话说完"）

content 填写规则（★ 只给 user_delegation 填，其余一律填空字符串）：
- 只填**用户要提醒的那件事本身**，2~12 字，如「睡觉」「喝水」「吃药」「交报告」「喂猫」；
- 不要带时间、不要带「提醒我/叫我」这类委托动词、不要带称呼与标点；
- ★「以后每天两点半提醒我睡觉」这种**每天约定**同样算 user_delegation，content 填「睡觉」、trigger_time 填「02:30」；
- 用户只说了「提醒我一下」这类**没有具体事项**的话 → content 填空字符串（系统会当场反问，不会瞎猜）。

trigger_day 填写规则（★重要，别漏日期，否则会记混）：
- 约定在今天 → "today"
- 「明早/明天/明晚」→ "tomorrow"
- 「X分钟后/X小时后/半小时后」这类相对时间 → "relative"，relative_minutes 填折算后的分钟数（半小时=30，1小时=60）
- 能推断出具体日期 → "YYYY-MM-DD"
- 只说了纯时间且按上面歧义规则推断为今天 → "today"；推断为已过的明天时段 → "tomorrow"

★ 没有时间的"当下意图"（2026-09-17 真机事故后新增，必须遵守）：
  「该睡觉了」「该吃饭了」「快去睡」「我得走了」「要迟到了」这类话是**此刻的意图**，
  不是"明天这个点"。这种情况 **trigger_day 填 "relative"、relative_minutes 填 2**，
  **绝对不要**填成同一个钟点的明天（真机事故：凌晨 2:50 用户说「该睡觉了」，
  被排成「明天 02:50 提醒睡觉」，1439 分钟后 —— 用户当场发现"她犯糊涂了"）。
  只有用户**明说**了明天/明晚/具体时刻，才排到未来。
"""


async def extract_and_store(session_id: str, character_id: str, ai_reply: str, user_text: str = ""):
    """异步识别本轮对话里的承诺（用户委托的提醒 + AI 自己的承诺），
    命中则写入 open_loops（category=ai_promise）。

    ★ user_text 不传时自动从 db 读最后一条用户消息——承诺的源头往往是用户的
      委托（「1 点叫我起床」「11 点提醒我睡觉」），只看 AI 回复会漏。
    """
    try:
        text = str(ai_reply or "").strip()
        if not text:
            return
        u_text = str(user_text or "").strip()
        if not u_text:
            try:
                _recent = db.recent_messages(session_id, limit=6, character_id=character_id) or []
                for _m in reversed(_recent):
                    if str(_m.get("role") or "") == "user":
                        u_text = str(_m.get("content") or "").strip()
                        break
            except Exception:
                u_text = ""
        # 粗筛：AI 回复 或 用户消息 任一含承诺信号（用户委托是承诺大头）
        if not (_looks_like_promise(text) or (u_text and _looks_like_promise(u_text))):
            return
        key = config.api_key_for_model(config.memory_extract_model(character_id))
        if not key:
            return
        from .deepseek_api import chat_once
        from datetime import datetime as _dt
        _now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
        _content = (
            f"（当前时间：{_now_str}）\n用户说：{u_text[-300:]}\nAI 回复：{text[-300:]}"
            if u_text else (f"（当前时间：{_now_str}）\nAI 说：" + text[-500:])
        )
        # ★ 空返回同模型重试（2026-09-09）：glm-5.3-flash 对本 prompt 偶发空返回
        #   （实测 18:11/18:12 连续 raw=""，同日 KnowledgeGraph/记忆整理同样空），
        #   承诺在提取环节蒸发 = 「六点半叫我」不兑现的直接原因。
        #   ★ 用户指定：提取模型固定 flash，不换模型、不用 chat 兜底——只重试。
        import asyncio as _aio
        _model = config.memory_extract_model(character_id)
        _msgs = [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _content}]
        raw = await chat_once(_model, _msgs, key, temperature=0.1, max_tokens=300)
        _retry = 0
        while not str(raw or "").strip() and _retry < 2:
            _retry += 1
            print(f"[AI承诺] 提取模型返回空，同模型重试 {_retry}/2", flush=True)
            try:
                await _aio.sleep(1)
                raw = await chat_once(_model, _msgs, key, temperature=0.1, max_tokens=300)
            except Exception as _re:
                print(f"[AI承诺] 重试失败: {_re}", flush=True)
                break
        data = _parse(raw)
        if not data or not data.get("is_promise"):
            # ★ 观测性：粗筛命中但 LLM 判非承诺/返回解析失败——之前完全静默，
            #   「十二点叫我睡觉」三轮全丢却查无此日志，根本没法定位断点
            print(f"[AI承诺] 粗筛命中但未识别(不记录): raw={str(raw)[:80]!r} "
                  f"u={str(u_text)[-60:]!r} ai={str(text)[-60:]!r}", flush=True)
            return
        title = str(data.get("title") or "").strip()
        if not title:
            print(f"[AI承诺] 识别为承诺但缺 title(不记录): {str(raw)[:100]}", flush=True)
            return
        promise_type = str(data.get("promise_type") or "time").strip()
        is_behavior = promise_type == "behavior"
        trigger_time = str(data.get("trigger_time") or "").strip() if not is_behavior else ""
        trigger_day = str(data.get("trigger_day") or "today").strip() if not is_behavior else ""
        relative_minutes = 0
        if not is_behavior:
            try:
                relative_minutes = int(data.get("relative_minutes") or 0)
            except Exception:
                relative_minutes = 0
        # ★ 修复（2026-09-04）：把「今天/明天/相对时间」换算成完整 "YYYY-MM-DD HH:MM"，
        #   避免「明早八点」只存 "08:00"、今天过了 8 点就被误判到点（时间记混）。
        # ★ 2026-09-16 裁决①（一次委托一条记录）：长期/循环约定（每天/每晚…）**不做一次性
        #   到点提醒的落库**——它归这里（open_loops），存"循环约定原文"（如「每天两点半」），
        #   `_recurring_hhmm` / `_is_due` / `due_promises` 靠它按天兑现（兑现只记 last_reminded）。
        #   前台 tasks 那条路对应地不再为循环约定建一次性任务（chat_logic._is_recurring_commission）。
        _rec_phrase = recurring_phrase(u_text) or recurring_phrase(text)
        if not is_behavior and trigger_time:
            if _rec_phrase and _recurring_hhmm(_rec_phrase):
                trigger_time = _rec_phrase
            else:
                trigger_time = _resolve_trigger_time(trigger_time, trigger_day, relative_minutes)
        importance = 0
        try:
            importance = int(data.get("importance") or 6)
        except Exception:
            importance = 6
        # ★ 2026-09-16 裁决①：同一件委托已经落成 tasks 行（前台路 = 用户委托的唯一落库处）时，
        #   这里**不得**再补一条一次性 open_loops 行——否则到点两条链路各提醒一次。
        if (not is_behavior and trigger_time
                and _timed_task_covers(session_id, character_id,
                                       data.get("content"), title, trigger_time)):
            print(f"[AI承诺] 同一委托已在 tasks（不重复写 open_loops）: "
                  f"{title} @ {trigger_time}", flush=True)
            return
        db.add_open_loop(
            session_id, title,
            description=("AI 主动行为约定（待遵守）" if is_behavior else "AI 主动承诺（待兑现）"),
            category=("behavior_promise" if is_behavior else "ai_promise"),
            importance=max(1, min(10, importance)),
            trigger_time=trigger_time,
            character_id=character_id,
        )
        print(f"[AI承诺] 已记录：{title} @ {trigger_time or ('行为约定' if is_behavior else '未定时间')}", flush=True)
    except Exception as e:
        print(f"[AI承诺] 提取失败(静默): {e}", flush=True)


def _parse_hhmm(text: str):
    r"""解析时间表述 → (h, mi)；失败返回 None。

    ★ 修复：旧正则 (\d{1,2})[点:：](\d{1,2}) 要求「点」后必须带分钟，
      「十二点叫我睡觉」提取出的 "12点"（整点无分钟）直接解析失败 →
      trigger_time 存空 → 承诺永不触发 = 用户视角「答应了却装忘」。
    现支持：12:00 / 12点 / 12点半 / 12点30 / 12点45分 / 12：05
    """
    t = str(text or "").strip()
    m = re.search(r"(\d{1,2})\s*[点:：]", t)
    if not m:
        return None
    try:
        h = int(m.group(1))
    except Exception:
        return None
    if not (0 <= h <= 23):
        return None
    rest = t[m.end():]
    if rest.startswith("半"):
        return (h, 30)
    if rest.startswith("一刻"):
        return (h, 15)
    m2 = re.match(r"\s*[:：]?\s*(\d{1,2})", rest)
    if m2:
        try:
            mi = int(m2.group(1))
        except Exception:
            return (h, 0)
        if 0 <= mi <= 59:
            return (h, mi)
    return (h, 0)


def _resolve_trigger_time(trigger_time: str, trigger_day: str, relative_minutes: int = 0) -> str:
    """把「HH:MM + 日期标记」换算成完整 "YYYY-MM-DD HH:MM"。

    支持：
    - relative：相对时间，用「当前时间 + relative_minutes 分钟」
    - today：今天 HH:MM；若今天已过，自动顺延到明天（修复"晚上说八点半"记混）
    - tomorrow：明天 HH:MM
    - YYYY-MM-DD：具体日期
    返回 "" 表示无法确定（不触发，但保留原文不丢）。
    """
    from datetime import timedelta
    now = datetime.now()
    # 相对时间
    _day_mark = str(trigger_day or "").strip()
    if _day_mark == "relative":
        # ★ 2026-09-17 修：relative 但 relative_minutes<=0（模型忘填/填 0）时，
        #   原先会**掉进下面的 HH:MM 解析**，而 relative 通常不带 trigger_time →
        #   解析失败 → 返回 "" → 调用方判定"时间认不出、不建任务" →
        #   用户委托的提醒**被静默丢掉**（回归用例抓到的）。
        #   语义上 "relative" 已经明确表示"从现在起"，所以缺分钟数就按"立刻"处理（2 分钟）。
        target = now + timedelta(minutes=max(2, int(relative_minutes or 0)))
        return target.strftime("%Y-%m-%d %H:%M")
    # 解析 HH:MM（兼容 12点 / 12点半 / 12:30）
    _t = _parse_hhmm(trigger_time)
    if not _t:
        print(f"[AI承诺] 触发时间无法解析: {trigger_time!r}（记为无触发时间，仅存档不调度）", flush=True)
        return ""
    h, mi = _t
    # 日期
    _day = _day_mark
    if _day == "tomorrow":
        day = now + timedelta(days=1)
    elif _day == "today" or not _day:
        day = now
    elif re.match(r"^\d{4}-\d{2}-\d{2}$", _day):
        try:
            day = datetime.strptime(_day, "%Y-%m-%d")
        except Exception:
            day = now
    else:
        day = now
    target = day.replace(hour=h, minute=mi, second=0, microsecond=0)
    # 今天且已过 → 顺延到明天（防止"晚上说八点半"被误判成今天已到点）
    if (_day == "today" or not _day) and target <= now:
        target = target + timedelta(days=1)
    return target.strftime("%Y-%m-%d %H:%M")


def _norm_trigger_phrase(t: str) -> str:
    """★ 2026-09-11：归一化口语时间短语。
    原 resolve_trigger_phrase 的时段正则只认「晚上」两字，「今晚11点」里的「晚」
    匹配不上 → 11 点没 +12 → 被当成「明天上午 11 点」，到点承诺全部落空。"""
    import re as _re
    t = str(t or "").strip()
    if not t:
        return t
    for _old, _new in (
        ("今晚", "今天晚上"), ("今夜", "今天晚上"), ("今早", "今天早上"),
        ("明晚", "明天晚上"), ("后晚", "后天晚上"),
        ("每晚", "每天晚上"), ("每晚饭", "每天晚饭"),
    ):
        if _old in t:
            t = _re.sub(_re.escape(_old), _new, t)
            break
    return t


def _minute_token(tok) -> int:
    """「点」后面的分钟 token → 0~59：半=30、一刻=15、30=30。

    ★ 2026-09-16：循环约定/承诺这条链路原来只认数字分钟，`int("半")` 直接抛异常
      → 「每天两点半」「每天凌晨两点半」整条解析失败（`_recurring_hhmm` 返回 None），
      循环约定退回 0 分甚至不认；用户说的"每天约定"（两点半）就是这么丢的精度。
    """
    t = str(tok or "").strip()
    if not t:
        return 0
    if t.startswith("半"):
        return 30
    if t.startswith("一刻"):
        return 15
    try:
        return int(t)
    except Exception:
        return 0


_CN_HOUR = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_hour(raw) -> int:
    """中文数字小时 → int；认不出返回 -1（「两」=2、「十二」=12、「二十三」=23）。

    ★ 2026-09-16：`_recurring_hhmm` 原来只认阿拉伯数字小时，
      「以后每天两点半提醒我睡觉」（_SYSTEM 里的原例）根本匹配不上 → 循环约定识别不了。
    """
    t = str(raw or "").strip()
    if t.isdigit():
        return int(t)
    if not t or any(ch not in _CN_HOUR for ch in t):
        return -1
    if "十" in t:
        left, _, right = t.partition("十")
        left_n = _CN_HOUR.get(left, 1) if left else 1
        right_n = _CN_HOUR.get(right, 0) if right else 0
        return left_n * 10 + right_n
    return _CN_HOUR.get(t, -1)


def _recurring_hhmm(phrase: str):
    """★ 2026-09-11：识别循环约定（每天/每日/每晚…）→ (hh, mi) 或 None。
    原逻辑把「每天23:00」当一次性时间顺延一天，循环约定永远只触发一次甚至从不。

    ★ 2026-09-16 裁决②（一张判定表，不是两张）：小时归属改用**与提醒链路同一张表**
      （`chat_logic.resolve_hour_24`）。旧实现自己写「晚上/夜里 +12、下午/中午 +12」，
      于是「每天晚上两点半」在循环链路算成 14:30、「每天半夜两点」按字面 02:00，
      与前台表（夜间型 1~5 点 = 次日凌晨）互相打架。
      循环约定只关心"每天的哪一个时刻"，`next_day`（次日 0 点档）对按天兑现没有意义，
      故只取小时、忽略跨天标记；时段判定所需线索（时段词/夜间活动词）由整句 ctx 提供。
    """
    import re as _re
    from . import chat_logic as _cl          # 局部 import：避免与 chat_logic 形成模块级循环
    t = _norm_trigger_phrase(str(phrase or "").strip())
    if not _re.match(r"^每[天日]", t):
        return None
    m = _re.search(r"(?P<h>\d{1,2}|[一二两三四五六七八九十]+)\s*[点:：:]\s*"
                   r"(?P<mi>半|一刻|\d{1,2})?", t)
    if not m:
        return None
    try:
        hh = _cn_hour(m.group("h"))
        mi = _minute_token(m.group("mi"))
    except Exception:
        return None
    if hh < 0:
        return None
    hh, _next_day = _cl.resolve_hour_24(hh, t, has_day=False)
    if not (0 <= hh <= 23 and 0 <= mi <= 59):
        return None
    return (hh, mi)


def resolve_trigger_phrase(phrase: str, now: datetime = None):
    """自然语言触发时间短语 → datetime；解析失败返回 None。

    ★ 2026-09-16 裁决②：这里**不再自己实现一份时段语义**——判定表只有一张，
      本函数只做口语归一化（今晚→今天晚上）后交给 `chat_logic.resolve_phrase_datetime`
      （与提醒链路前台兜底、_is_due 完全同一份实现）。旧实现在本函数里独立写了
      「下午/晚上 +12」，于是「晚上两点」= 当天 14:00、「今晚十二点」= 当天 12:00、
      「十二点半提醒我睡觉」= 当天 12:30，与前台那张表互相打架。

    支持（LLM 提取器/其他写入方存的原文）：
      今天上午11:00 / 明天早上8点 / 后天晚上9点30 / 大后天中午12点 / 两天后晚上八点 /
      晚上11点 / 明天11点 / 每天凌晨两点半（循环约定）/ YYYY-MM-DD HH:MM
    """
    t = _norm_trigger_phrase(str(phrase or "").strip())
    if not t:
        return None
    try:
        from . import chat_logic as _cl      # 局部 import：避免与 chat_logic 形成模块级循环
        return _cl.resolve_phrase_datetime(t, now)
    except Exception as e:
        print(f"[AI承诺] 时间短语解析失败(静默): {e}", flush=True)
        return None


def _is_due(trigger_time: str, now: datetime) -> bool:
    """判断承诺是否已到点。兼容三种格式：
    - "YYYY-MM-DD HH:MM"（标准，按时刻精确判断；过期 24h 内可补触发）
    - 自然语言短语（今天上午11:00 / 明天早上8点——open_loop_manager 提取器写入）
    - 旧格式 "HH:MM"（历史数据，按当天时分判断）
    """
    t = str(trigger_time or "").strip()
    if not t:
        return False
    # ★ 循环约定（每天/每晚…）：今天到了点就算 due（当次兑现由 due_promises 按
    #   last_reminded 去重，兑现后当天不再重复）
    _rec = _recurring_hhmm(t)
    if _rec:
        return (now.hour, now.minute) >= _rec
    # 新格式：完整日期时间
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2})[:：](\d{1,2})", t)
    if m:
        try:
            target = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)),
            )
            # ★ 过期保护：承诺到点后 24 小时内没兑现可补触发；超过 24 小时视为过期，
            #   不再翻出来（避免"三天前说八点半喊你"今天突然触发，反而像没脑子）。
            if now - target > timedelta(hours=24):
                return False
            return now >= target
        except Exception:
            return False
    # 自然语言短语（今天上午11:00 / 明天早上8点）
    resolved = resolve_trigger_phrase(t, now)
    if resolved is not None:
        if now - resolved > timedelta(hours=24):
            return False
        return now >= resolved
    # 旧格式：HH:MM（兼容历史数据，含 12点 / 12点半 整点表述）
    _t = _parse_hhmm(t)
    if not _t:
        return False
    return (now.hour, now.minute) >= _t


def _fetch_promises(session_id: str, character_id: str, limit: int = 50) -> list:
    """专用查询：只取「到点要兑现」类 pending 记录，按 trigger_time 排序。

    ★ 2026-09-09 修复：不再复用 get_open_loops——那个通用查询按
      importance DESC, id DESC LIMIT N，而表里 importance>=8 的 pending
      常年 80+ 条，新承诺（importance 6~8）永远挤不进前 N，
      「到点也轮不到兑现检查」就是「六点半叫我」第二次失败的根因之一。
    ★ category 放宽（2026-09-09 深夜）：open_loop_manager 提取器写的同类记录
      category 可能是 reminder/task（LLM 自选），只认 ai_promise 会漏掉它们
      ——「十一点叫醒用户」存成 reminder 就永远不兑现（用户实测 09-09 深夜）。
    """
    try:
        loops = db.q(
            "SELECT * FROM open_loops WHERE session_id=? AND character_id=? "
            "AND status='pending' AND category IN ('ai_promise','reminder','task','promise','agreement','event') "
            "ORDER BY id DESC LIMIT ?",
            (session_id, character_id, max(1, int(limit) * 10)), fetch=True
        ) or []
        # ★ 2026-09-11 三个修复：
        #   a) db.q 返回 sqlite3.Row，原代码直接 .get() 必然 AttributeError
        #      （被 idle_agent 的 try/except 静默吞掉）→ 到点承诺从来没兑现过；
        #   b) LIMIT 50 + ORDER BY trigger_time（ISO 排中文前）会把新承诺挤出窗口
        #      （实测 pending 323 条）→ 放大 10 倍按 id 倒序（新承诺优先）；
        #   c) category 白名单补 agreement/event（专属时间约定存的就是这两类）。
        return [dict(r) for r in loops]
    except Exception:
        return []


def due_promises(session_id: str, character_id: str) -> list:
    """返回「已到点、尚未兑现」的 AI 承诺列表（importance 高的在前）。"""
    loops = _fetch_promises(session_id, character_id, 50)
    now = datetime.now()
    _today = now.strftime("%Y-%m-%d")
    due = []
    for lp in loops:
        _tt = str(lp.get("trigger_time") or "")
        _rec = _recurring_hhmm(_tt)
        if _rec and str(lp.get("last_reminded") or "").startswith(_today):
            continue  # 循环约定今天已兑现过
        # ★ 旧账不翻：相对时间短语（"今天下午五点"）每天都会重新解析到当天，
        #   几天前创建且一直没兑现的旧承诺会天天补触发——创建超过 24h 的非循环承诺跳过
        #   （与 _still_relevant 的"旧账不翻"语义一致；循环约定豁免）。
        if not _rec:
            try:
                _ct = datetime.fromisoformat(str(lp.get("created_time") or ""))
                if now - _ct > timedelta(hours=24):
                    continue
            except Exception:
                pass
        if _is_due(_tt, now):
            due.append(lp)
    due.sort(key=lambda x: -(int(x.get("importance") or 0)))
    return due


def behavior_promise_block(session_id: str, character_id: str, limit: int = 6) -> str:
    """加载 AI 自己答应过的「行为约定」（behavior_promise），常驻注入 prompt。

    与时间承诺（八点半喊你）不同，行为约定（等你发完/不插嘴/先别回）没有时间点，
    必须每次聊天都带上，AI 才记得遵守——否则会出现「AI 答应等信号却还是插嘴」的穿帮。
    """
    try:
        # ★ 同 due_promises：专用查询（importance 排序会把行为约定挤出窗口）
        loops = db.q(
            "SELECT * FROM open_loops WHERE session_id=? AND character_id=? "
            "AND status='pending' AND category='behavior_promise' "
            "ORDER BY id DESC LIMIT ?",
            (session_id, character_id, max(1, int(limit) * 2)), fetch=True
        ) or []
        behaviors = [dict(r) for r in (loops or [])]
    except Exception:
        return ""
    if not behaviors:
        return ""
    behaviors = behaviors[:limit]
    lines = "\n".join("- " + str(lp.get("title") or "").strip() for lp in behaviors)
    return "【你答应过 TA 的事（行为约定，务必遵守，别自己食言）】\n" + lines


def _still_relevant(trigger_time: str, now: datetime) -> bool:
    """判断一个时间承诺是否还应该在 AI 的「记忆」里：
    - 未来的 → 保留
    - 过去 24 小时内的（到点还没兑现）→ 保留
    - 超过 24 小时 → 过滤（旧账不翻，避免「三天前说八点半喊你」今天还挂嘴边）
    旧格式 HH:MM 无法判断日期，保守保留。
    """
    t = str(trigger_time or "").strip()
    if not t:
        return True
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2})[:：](\d{1,2})", t)
    if m:
        try:
            from datetime import timedelta
            target = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)),
            )
            return (now - target) < timedelta(hours=24)
        except Exception:
            return True
    return True


def pending_promises_block(session_id: str, character_id: str, limit: int = 8) -> str:
    """加载 AI 尚未兑现的「时间承诺」（11 点喊你 / 明早叫你 / 九点一起打游戏等），常驻注入 prompt。

    这是「承诺提醒却想不起来」的根因修复：以前只有 behavior_promise（行为约定）常驻注入，
    time 类型的时间承诺只在到点由 idle_agent 触发兑现，聊天时 AI 完全不知道自己答应过什么，
    被用户问「你答应我什么来着」就当场穿帮、开始瞎编。
    """
    try:
        loops = _fetch_promises(session_id, character_id, 50)
    except Exception:
        return ""
    now = datetime.now()
    pending = []
    for lp in loops:
        if str(lp.get("category") or "") != "ai_promise":
            continue
        t = str(lp.get("trigger_time") or "").strip()
        if t and not _still_relevant(t, now):
            continue
        pending.append(lp)
    if not pending:
        return ""
    # importance 高的在前
    pending.sort(key=lambda x: -(int(x.get("importance") or 0)))
    pending = pending[:limit]
    lines = []
    for lp in pending:
        title = str(lp.get("title") or "").strip()
        tt = str(lp.get("trigger_time") or "").strip()
        lines.append("- " + title + (("（时间：" + tt + "）") if tt else ""))
    return "【你答应过 TA 的事（时间承诺，务必记得，被问起时能答上来，别装傻或瞎编）】\n" + "\n".join(lines)


def _parse(raw):
    if not raw:
        return None
    try:
        m = re.search(r"\{[\s\S]*\}", str(raw))
        data = json.loads(m.group(0)) if m else json.loads(str(raw))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# ★ 2026-09-16 新增：定时提醒「模型主判」入口（误触发修复 A 方案）
#   事故：用户说「哎呀再等我会哈 到时候聊到两点半吧」，chat_logic 的正则把
#   「到时候」当委托、把时间词之后的残尾「半吧」当事项，建了个 02:00 的提醒。
#   修法：判断权交给模型（同一个提取器 prompt），正则只在她不可用时兜底。
# ══════════════════════════════════════════════════════════════════

# 前台等待上限（秒）：超过就认为模型层不可用 → 调用方降级正则，绝不让聊天卡死。
_VERDICT_HARD_TIMEOUT = 6.0


async def extract_reminder_verdict(user_text: str, ai_reply: str = "",
                                   character_id: str = "default",
                                   session_id: str = ""):
    """模型主判：这一轮**用户**是不是在委托一件带时间的提醒/约定。

    与 extract_and_store 的分工（两者跑同一个 _SYSTEM 提取器，判定口径一致）：
      · extract_and_store：后台异步，命中就写 open_loops（AI 承诺兑现链路），失败静默；
      · 本函数：**前台有界等待**结果交给调用方去建 tasks，并且必须能区分
        「模型说不是」与「模型层不可用」——
          说不是  → 返回 {"is_reminder": False, ...}（**权威**：调用方绝不建任务）；
          不可用  → 返回 None（调用方降级走正则兜底）。
        这两者混为一谈就会退化成"模型一挂就乱建任务"，所以必须分开。

    返回 dict 字段：is_reminder / promise_source / content / trigger_time /
                    trigger_day / relative_minutes；模型层不可用返回 None。
    """
    try:
        u = str(user_text or "").strip()
        if not u:
            return None
        model = config.memory_extract_model(character_id)
        key = config.api_key_for_model(model)
        if not key:
            print("[提醒判定] 提取模型无可用 Key → 模型层不可用（降级正则）", flush=True)
            return None
        from .deepseek_api import chat_once
        a = str(ai_reply or "").strip()
        _now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        if a:
            _content = (f"（当前时间：{_now_str}）\n用户说：{u[-300:]}\nAI 回复：{a[-300:]}")
        else:
            # 前台判定发生在 AI 开口之前，只有用户的话——委托本身就是承诺，照样判
            _content = (f"（当前时间：{_now_str}）\n用户说：{u[-300:]}\n"
                        f"（本轮 AI 还没回复；用户在委托的话照样按委托判）")
        raw = await chat_once(model,
                              [{"role": "system", "content": _SYSTEM},
                               {"role": "user", "content": _content}],
                              key, temperature=0.1, max_tokens=300,
                              hard_timeout=_VERDICT_HARD_TIMEOUT)
        data = _parse(raw)
        if not data:
            print(f"[提醒判定] 模型返回无法解析 → 视作不可用（降级正则）: {str(raw)[:80]!r}",
                  flush=True)
            # ★ 取证：解析失败也要留档（否则"模型到底回了什么"事后无从查起）
            try:
                from . import trace as _trace
                _trace.reminder_verdict(session_id, character_id, u, raw=str(raw),
                                        error="模型输出无法解析，已降级正则")
            except Exception:
                pass
            return None
        ptype = str(data.get("promise_type") or "time").strip()
        source = str(data.get("promise_source") or "").strip()
        content = str(data.get("content") or "").strip()
        if not source:
            # 老输出没有 promise_source：只有明确给了事项才当作「用户委托」
            source = "user_delegation" if content else ""
        try:
            _rel = int(data.get("relative_minutes") or 0)
        except Exception:
            _rel = 0
        out = {
            "is_reminder": bool(data.get("is_promise")) and ptype != "behavior",
            "promise_source": source,
            "content": content,
            "trigger_time": str(data.get("trigger_time") or "").strip(),
            "trigger_day": str(data.get("trigger_day") or "").strip(),
            "relative_minutes": max(0, _rel),
        }
        # ★ 2026-09-17 取证：把「用户原话 + 模型原始 JSON + 解析结果 + 换算时刻」一起落 trace。
        #   起因见 trace.reminder_verdict 的说明：事故排查时发现旧日志既没输入也没原始输出，
        #   只能靠推断，复盘不出结论。
        try:
            from . import trace as _trace
            _resolved = ""
            _minutes = None
            try:
                _resolved = _resolve_trigger_time(out["trigger_time"], out["trigger_day"],
                                                  out["relative_minutes"]) or ""
                if _resolved:
                    _dt = datetime.strptime(_resolved, "%Y-%m-%d %H:%M")
                    _minutes = int((_dt - datetime.now()).total_seconds() / 60)
            except Exception:
                pass
            _trace.reminder_verdict(session_id, character_id, u, raw=str(raw), verdict=out,
                                    resolved=_resolved, minutes=_minutes)
        except Exception:
            pass
        return out
    except Exception as e:
        print(f"[提醒判定] 模型层异常 → 视作不可用（降级正则）: {e}", flush=True)
        return None
