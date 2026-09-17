# -*- coding: utf-8 -*-
"""第 7 批「定时提醒」回归测试（红-绿）——来自 2026-09-17 真机事故。

事故现场（用户亲眼看到）：
  02:50:50 骨子创建 tasks#26：「睡觉」→ 触发时间 **2026-09-18T02:50:00**
  02:50:50 她对用户说：「宝，1439分钟后也就是2026-09-18 02:50，该睡觉啦，我记着呢！」
  用户：「她又犯糊涂了，我记得这里是让模型判断什么时间的，而且我叫她取消还取消不了」

两个根因：
  ① 模型把"该睡觉了"理解成"明天这个点"，而代码对"同一时刻的明天"没有任何护栏；
  ② 取消**只有 API**（/api/pc/task/delete），聊天里说「取消」没有任何路径；
     连 agent 自己的 cancel_task 工具都只 UPDATE open_loops，根本不碰 tasks 表。

跑法（在 D:\\AI聊天项目桌面端\\AI聊天项目 下）：
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py batch7
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TMP = os.environ.setdefault("AI_COMPANION_DATA_DIR",
                             str(PROJECT_ROOT / ".pytest_data"))
Path(_TMP).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AI_COMPANION_EXTERNAL_MEMORY_DIR",
                      str(Path(_TMP) / "外置记忆库"))

SESS, CHAR = "s_main", "小满"


def fresh_db():
    import gc
    import shutil
    import time
    ext = Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"])
    if ext.exists():
        shutil.rmtree(ext, ignore_errors=True)
    from backend import db

    def _close():
        with db._lock:
            if db._conn is not None:
                try:
                    db._conn.close()
                except Exception:
                    pass
            db._conn = None

    def _unlink():
        p = Path(_TMP) / "local_db.db"
        for attempt in range(10):
            try:
                if p.exists():
                    p.unlink()
                return
            except PermissionError:
                gc.collect()
                time.sleep(0.15 * (attempt + 1))

    _close()
    _unlink()
    db.init()
    yield
    _close()
    _unlink()


def _make_reminder(content="睡觉", minutes_from_now=1439):
    from backend import db
    trig = datetime.now() + timedelta(minutes=minutes_from_now)
    return db.add_task(character_name=CHAR, session_id=SESS, character_id=CHAR,
                       task_type="once",
                       trigger_time=trig.strftime("%Y-%m-%dT%H:%M:%S"),
                       content=content, source="chat")


# ══════════════════════════════════════════════════════════════════
# 1. 护栏：明显不合理的落点不许建出来
# ══════════════════════════════════════════════════════════════════

def test_immediate_wording_clamps_absurd_24h_trigger(fresh_db):
    """「都快三点了…该睡觉了」这种当下语义，不许被排到 24 小时后。

    真机：#26 触发时间 = 建任务时间 + 1439 分钟（24 小时差 1 分），
    她对用户说「1439分钟后…该睡觉啦」。这里用当事那句话作 raw。
    """
    from backend import chat_logic
    now = datetime.now()
    res = chat_logic._create_reminder_task(
        now + timedelta(minutes=1439), "睡觉", CHAR, SESS, CHAR,
        raw="该睡觉了")
    assert res["minutes"] <= 5, (
        f"当下语义的提醒被排到了 {res['minutes']} 分钟后（应夹到即刻）")
    # 真的落库了，且触发时间就在几分钟内
    from backend import db
    row = db.q("SELECT trigger_time FROM tasks WHERE id=?", (res["task_id"],), fetch=True)[0]
    trig = datetime.strptime(str(row["trigger_time"])[:19], "%Y-%m-%dT%H:%M:%S")
    assert (trig - now).total_seconds() < 600, f"落库时间仍偏远: {trig}"


def test_casual_wording_without_immediate_marker_not_clamped(fresh_db):
    """反面：原话里没有"此刻"语义时不动它（护栏只在两条同时满足时生效）。

    例：「明天这个时候提醒我睡觉」—— 既没有"该/要/得…了"，也不该夹到即刻。
    """
    from backend import chat_logic
    now = datetime.now()
    res = chat_logic._create_reminder_task(
        now + timedelta(minutes=1439), "睡觉", CHAR, SESS, CHAR,
        raw="明天这个时候提醒我睡觉")
    assert res["minutes"] > 1200, f"没有此刻语义却被夹到 {res['minutes']} 分钟后"


def test_explicit_tomorrow_is_not_clamped(fresh_db):
    """明确说「明天这个时候提醒我」时**不许**动它（护栏只在两条同时满足时生效）。"""
    from backend import chat_logic
    now = datetime.now()
    res = chat_logic._create_reminder_task(
        now + timedelta(minutes=1439), "提醒我开会", CHAR, SESS, CHAR,
        raw="明天这个时候提醒我开会")
    assert res["minutes"] > 1200, f"明确的明天委托被误夹到 {res['minutes']} 分钟后"


def test_far_future_still_capped_at_30_days(fresh_db):
    from backend import chat_logic
    now = datetime.now()
    res = chat_logic._create_reminder_task(
        now + timedelta(days=90), "很远的事", CHAR, SESS, CHAR, raw="随便")
    assert res["minutes"] <= 30 * 24 * 60


# ══════════════════════════════════════════════════════════════════
# 2. 取消：自然语言说得动，agent 工具也碰得到 tasks 表
# ══════════════════════════════════════════════════════════════════

def test_coarse_detects_cancel_wording():
    from backend import chat_logic as cl
    for t in ("取消", "把这个取消掉", "别提醒我睡觉了", "不用提醒了",
              "算了别叫我了", "撤销刚才那个提醒"):
        assert cl.looks_like_reminder_cancel(t), f"没识别出取消意图: {t}"
    assert not cl.looks_like_reminder_cancel("今天天气不错")
    assert not cl.looks_like_reminder_cancel("取消订单的按钮在哪")  # 缺"提醒/这个"类对象


def test_cancel_pending_reminders_marks_cancelled(fresh_db):
    """说一句取消，pending 提醒必须真的变成 cancelled。"""
    from backend import chat_logic as cl, db
    tid = _make_reminder("睡觉", 1439)
    assert db.q("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)[0]["status"] == "pending"

    res = cl.cancel_pending_reminders(SESS, CHAR, "取消")
    assert res["cancelled"] == 1, f"没取消掉: {res}"
    assert db.q("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)[0]["status"] == "cancelled"


def test_cancel_by_keyword_only_touches_matching(fresh_db):
    """说「别提醒我睡觉」时只取消睡觉那条，别把别的提醒一起干掉。"""
    from backend import chat_logic as cl, db
    a = _make_reminder("睡觉", 1439)
    b = _make_reminder("吃药", 600)

    res = cl.cancel_pending_reminders(SESS, CHAR, "别提醒我睡觉了")
    assert res["cancelled"] == 1, f"应只取消 1 条: {res}"
    st = {r["id"]: r["status"] for r in
          db.q("SELECT id, status FROM tasks WHERE id IN (?,?)", (a, b), fetch=True)}
    assert st[a] == "cancelled" and st[b] == "pending", f"取消错了对象: {st}"


def test_resolve_timed_reminder_handles_cancel_without_model(fresh_db):
    """整条入口：说「取消」要返回取消结果 + 一句回复（不必调模型）。"""
    from backend import chat_logic as cl, db
    tid = _make_reminder("睡觉", 1439)
    out = asyncio.get_event_loop().run_until_complete(
        cl.resolve_timed_reminder("取消那个提醒", CHAR, session_id=SESS, character_id=CHAR))
    assert out and out.get("cancelled") == 1, f"入口没处理取消: {out}"
    assert out.get("reply"), "取消后应有一句回复"
    assert db.q("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)[0]["status"] == "cancelled"


def test_agent_cancel_tool_reaches_tasks_table(fresh_db):
    """agent 的 cancel_task 工具原先只碰 open_loops —— 必须也能取消 tasks。"""
    from backend import db
    from backend.agent import self_modules as sm
    tid = _make_reminder("睡觉", 1439)

    out = asyncio.get_event_loop().run_until_complete(
        sm._cancel_task({"keyword": "睡觉"},
                        {"character_id": CHAR, "session_id": SESS}))
    assert out.get("ok"), f"工具没取消成功: {out}"
    assert db.q("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)[0]["status"] == "cancelled", \
        "cancel_task 工具仍然没碰 tasks 表（'取消不掉'的根因）"


def test_cancel_does_not_touch_non_pending(fresh_db):
    """已发出(done)的任务不许被取消动作改写。"""
    from backend import db
    tid = _make_reminder("睡觉", 1439)
    db.update_task_status(tid, "done")
    assert db.cancel_task(tid, SESS, CHAR) is False
    assert db.q("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)[0]["status"] == "done"


# ══════════════════════════════════════════════════════════════════
# 3. 提示词层：教模型别把"当下意图"排到明天
# ══════════════════════════════════════════════════════════════════

def test_verdict_prompt_teaches_immediate_intent():
    """判定提示词必须写明"该睡觉了=此刻、relative 2 分钟"，而不是同一个钟点的明天。

    护栏是兜底（模型给错了才夹）；提示词才是正本清源 ——
    只有护栏的话，模型仍会先在对话里对用户说出"1439分钟后"那句错话。

    ★ 措辞更正（2026-09-17 用户质疑"这个不是交给模型判断吗为什么是靠 prompt"）：
      时机判断**确实**由模型做（`ai_promise.extract_reminder_verdict` 在线调 LLM）。
      这里改的不是"让模型更聪明"，而是**补上契约里缺失的一档**：
      `trigger_day` 的合法取值原本只有 today / tomorrow / YYYY-MM-DD / relative，
      而 `_resolve_trigger_time` 的规则是"today 且已过 → 顺延到明天"
      （ai_promise.py:378-380）。凌晨 2:50 说"该睡觉了"时，今天 02:50 已过，
      模型**没有任何一档能表达"就是此刻"**，只能落到 tomorrow。
      所以补的是契约槽位（复用已有的 relative），代码护栏只是第二层兜底。
    """
    src = (PROJECT_ROOT / "backend" / "ai_promise.py").read_text(encoding="utf-8")
    assert "当下意图" in src, "提示词没写『当下意图』规则（模型还会排到明天）"
    assert "该睡觉了" in src, "提示词没给出具体例子"
    assert '"relative"' in src and "relative_minutes 填 2" in src, "没教模型填 relative 2 分钟"
    # 契约确实缺"此刻"这一档，且顺延规则确实存在（本修复针对的就是这两点）
    assert "推断为已过的明天时段 → \"tomorrow\"" in src or "已过" in src, "没找到顺延规则"
    assert "timedelta(days=1)" in src, "顺延到明天的实现不见了（契约/换算改了，用例要跟着改）"


def test_bare_cancel_works_even_without_coarse_match(fresh_db):
    """裸「取消」必须能取消 —— 它过不了定时提醒的粗筛，但确实是取消意图。

    ★ 回归用例抓到过我自己的错：`looks_like_reminder_cancel('取消')` 返回 True，
      但取消分支放在粗筛**之后**，而「取消」不含"提醒/几点"这类词 → 过不了粗筛
      → 直接 return None → 判定与执行不一致，用户说「取消」仍然取消不掉。
    """
    from backend import chat_logic as cl, db
    tid = _make_reminder("睡觉", 1439)
    assert cl.looks_like_reminder_cancel("取消") is True
    assert not cl._REMINDER_COARSE.search("取消"), "前提：裸取消本来就过不了粗筛"
    out = asyncio.get_event_loop().run_until_complete(
        cl.resolve_timed_reminder("取消", CHAR, session_id=SESS, character_id=CHAR))
    assert out and out.get("cancelled") == 1, f"裸「取消」没生效: {out}"
    assert db.q("SELECT status FROM tasks WHERE id=?", (tid,), fetch=True)[0]["status"] == "cancelled"


def test_cancel_without_pending_does_not_create_task(fresh_db):
    """说「取消那个提醒」但没有 pending 时，**绝不能**反过来建一条提醒。"""
    from backend import chat_logic as cl, db
    out = asyncio.get_event_loop().run_until_complete(
        cl.resolve_timed_reminder("取消那个提醒", CHAR, session_id=SESS, character_id=CHAR))
    assert out is None, f"没有 pending 时应交回正常聊天，实际 {out}"
    rows = db.q("SELECT COUNT(*) c FROM tasks WHERE session_id=?", (SESS,), fetch=True)[0]["c"]
    assert rows == 0, f"「取消 X」被误当成新建任务了（建出 {rows} 条）"


def test_relative_zero_minutes_treated_as_now(fresh_db):
    """模型若填 relative 但 relative_minutes=0，要按"立刻"处理而不是明天的同一时刻。

    这是契约的边界：模型可能只填 trigger_day='relative' 而忘了分钟数。
    """
    from backend import chat_logic
    from datetime import datetime
    v = {"is_reminder": True, "content": "睡觉", "trigger_time": "",
         "trigger_day": "relative", "relative_minutes": 0}
    trig = chat_logic._trigger_from_verdict(v, "该睡觉了")
    assert trig is not None, "relative 但分钟数为 0 时不该返回 None"
    gap_h = (trig - datetime.now()).total_seconds() / 3600
    assert gap_h < 0.05, f"应理解为此刻，实际排在 {gap_h:.2f} 小时后（{trig}）"


# ══════════════════════════════════════════════════════════════════
# 4. 取证埋点：判定过程必须可追溯（否则事故复盘只能靠推断）
# ══════════════════════════════════════════════════════════════════

def test_reminder_verdict_is_traced():
    """每次"该不该建提醒"的判定都要落 trace：原话 + 模型原始输出 + 换算结果。

    事故教训：旧日志只有 `chat_once(...) out=74`，**没有输入也没有原始输出**，
    导致"到底哪句话触发、模型填了什么档"无法确证，只能推断。
    """
    from backend import trace as _trace
    _trace.reminder_verdict("s_main", "小满", "该睡觉了",
                            raw='{"is_promise": true, "trigger_day": "relative"}',
                            verdict={"is_reminder": True, "trigger_day": "relative",
                                     "relative_minutes": 2, "content": "睡觉"},
                            resolved="2026-09-17 03:06", minutes=2)
    fp = _trace._path()
    assert fp.exists(), "trace 文件没生成"
    text = fp.read_text(encoding="utf-8")
    assert "reminder_verdict" in text, "没有 reminder_verdict 记录"
    last = [ln for ln in text.splitlines() if "reminder_verdict" in ln][-1]
    for need in ("该睡觉了", "relative", "2026-09-17 03:06"):
        assert need in last, f"trace 缺字段 {need!r}: {last[:200]}"


def test_task_failed_notice_is_traced():
    """任务失败告知也要留痕（含"有没有真的告知到用户"）。"""
    from backend import trace as _trace
    _trace.task_failed("s_main", "小满", 99, "睡觉", reason="消息生成或推送失败",
                       attempts=3, notified=True)
    text = _trace._path().read_text(encoding="utf-8")
    last = [ln for ln in text.splitlines() if "task_failed" in ln][-1]
    assert '"task_id": 99' in last and "notified" in last, f"trace 不完整: {last[:200]}"


# ══════════════════════════════════════════════════════════════════
# 5. 失败告知：用户委托的提醒失败后，必须有人告诉他
# ══════════════════════════════════════════════════════════════════

def test_failed_reminder_notifies_user(fresh_db):
    """重试耗尽/内容不可投递时，要往聊天里发一条朴素告知（不装没事）。

    真机 25 条任务里 5 条 failed，用户完全不知道 —— 视角是"她答应了却没动静"。
    """
    from backend import scheduler as sch

    calls = []

    class _S(sch.Scheduler):
        async def _deliver(self, msg, name, meta):
            calls.append((msg, name, meta))
            return True

    s = _S()
    s._session, s._character_id = "s_main", "小满"
    task = {"id": 42, "content": "睡觉", "character_name": "小满",
            "session_id": "s_main", "character_id": "小满"}
    ok = asyncio.get_event_loop().run_until_complete(
        s._notify_task_failed(task, "消息生成或推送失败（已重试 3 次）", "s_main", "小满"))

    assert ok is True, "告知没发出去"
    assert len(calls) == 1, f"应只发一条告知，实际 {len(calls)}"
    msg, _name, meta = calls[0]
    assert "没发出去" in msg and "睡觉" in msg, f"告知文案没说清: {msg!r}"
    assert meta.get("proactive_type") == "task_failed_notice"
    assert meta.get("task_id") == 42


def test_failed_reminder_notice_is_plain_and_short(fresh_db):
    """失败告知必须是**朴素事实通报**，不许走模型润色（越润越容易又出错）。"""
    src = (PROJECT_ROOT / "backend" / "scheduler.py").read_text(encoding="utf-8")
    idx = src.index("async def _notify_task_failed")
    block = src[idx:idx + 2200]
    assert "chat_once" not in block, "失败告知里调了模型（应直接发固定文案）"
    assert "没发出去" in block, "文案不见了"


def test_pending_task_failure_paths_call_notice():
    """两条终态失败路径（重试耗尽 / 内容不可投递）都要接上告知。"""
    src = (PROJECT_ROOT / "backend" / "scheduler.py").read_text(encoding="utf-8")
    assert src.count("_notify_task_failed") >= 3, (
        "调用点不足：定义 1 处 + 重试耗尽 1 处 + 执行异常 1 处（+ 内容不可投递 1 处）")
    assert "attempts >= 3" in src, "重试耗尽判定不见了"
