# -*- coding: utf-8 -*-
"""Agent 窗口的任务编排。

职责分层：
  · window.py —— 任务生命周期（计划 → 确认 → 跑 → 事件 → 收尾）与状态机
  · acp_bridge.py —— 只负责与 harness 进程讲话
  · director.py —— 只负责判定与话术
状态机：idle → awaiting_confirm → running → done/failed/stopped

两条不变量（Task 5 复评确立，改这里前先读）：
  1. 桥按「工作目录 + 权限档 + 伴侣会话」隔离（`get_bridge(..., session_id=…)`）——
     每个调用点都必须把 session_id/character_id 传进去，少传 = 新建一条桥、
     cancel/插话打在空会话上（静默无效，还多一个 node 进程）。
  2. ACP 一条会话同一时刻只允许一个 prompt —— 上层必须排队；桥上已有 prompt 在飞时
     `prompt()` 抛 `BridgeBusy`，那**不是失败**，是"排队"（R35）。
"""
import asyncio
import os
import time
import uuid

from .. import db
from . import capability, director
from .acp_bridge import (BridgeBusy, get_bridge,            # ★ R35：BridgeBusy 必须显式接住
                         stop_bridges, widened_busy)        # ★ 收尾修复：收档只关放开档的桥

_TASKS: dict = {}                 # task_id -> dict
_SESSION_LAST: dict = {}          # session_id -> task_id
_LOCK = asyncio.Lock()            # 建桥/切档这类跨 await 的序列化（状态机自身在事件循环里串行改）
PERSONA_OVERLAY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona_overlay.yml")  # ★ R3
_KV_CWD = "agent_window_cwd"
_KV_MODE = "agent_window_permission_mode"
_KV_MODEL = "agent_window_model"
_MAX_KEEP = 20
_RETRY_MAX = 3                    # ★ R52：BridgeBusy 的重试上限（有界，绝不无限重发）
_RETRY_DELAY = 2.0                # ★ R52：每次重试前等这么久（另一位消费者通常这时候就收尾了）
#: ★ R52：任务"在飞"的状态（排空 / 有界重试窗口内都算）—— send 的前置判断与 _prune 都认它
_IN_FLIGHT = ("running", "awaiting_confirm", "queued")


def _cwd_for(session_id: str) -> str:
    """工作目录：会话级 → 全局 kv → 默认（用户项目根）。"""
    v = str(db.kv_get(_KV_CWD + ":" + str(session_id)) or db.kv_get(_KV_CWD) or "").strip()
    if v:
        return v
    try:
        from .. import config as _config
        # ★ 真机核对：config 里是 ROOT_DIR（Path），没有 PROJECT_ROOT —— 两者都认，别裸下标。
        root = getattr(_config, "PROJECT_ROOT", "") or getattr(_config, "ROOT_DIR", "")
        return str(root or "").strip() or os.getcwd()
    except Exception:
        return os.getcwd()


def _mode_for(session_id: str) -> str:
    return str(db.kv_get(_KV_MODE + ":" + str(session_id)) or "workspace-write")


def state(session_id: str) -> dict:
    cap = capability.probe()
    tid = _SESSION_LAST.get(session_id)
    task = _TASKS.get(tid) or {}
    return {
        "capability": cap,
        "cwd": _cwd_for(session_id),
        "permission_mode": _mode_for(session_id),
        "model": str(db.kv_get(_KV_MODEL + ":" + str(session_id)) or ""),
        "busy": task.get("status") == "running" and not task.get("_retry_scheduled"),
        # ★ R60（复评 Minor2）：把下划线开头的内部记账（_top_ran/_await_retry/_retry_scheduled…）
        #   挡在 API 外面 —— 它们是状态机实现细节，前端拿到只会被误用
        "task": {k: v for k, v in task.items()
                 if k != "events" and not str(k).startswith("_")} if task else None,
        "events": list(task.get("events") or [])[-200:],
        "config_options": list(task.get("config_options") or []),
    }


async def send(session_id: str, character_id: str, text: str, *, cwd: str = "", mode: str = "auto") -> dict:
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "kind": "chat", "reply": ""}
    # ★ R35（Task 5 复评）：本会话已有任务在跑 → 这次直接进那条任务的队列（排队插话），
    # 绝不并发发第二次 prompt（桥会抛 BridgeBusy，前端会看到"失败"）
    # ★ R52：判据改成 _IN_FLIGHT（running/awaiting_confirm/queued）——
    #   queued 是"在飞但还没排上"（BridgeBusy 重试窗口），漏掉它就会起第二个任务再撞一次 BridgeBusy
    _running = [x for x in _TASKS.values()
                if x.get("session_id") == session_id and x.get("status") in _IN_FLIGHT]
    if _running:
        t0 = _running[-1]
        t0["queue"].append(text)
        return {"ok": True, "kind": "queued", "task_id": t0["task_id"], "plan": None,
                "reply": "", "pending": len(t0["queue"])}
    if cwd:
        db.kv_set(_KV_CWD + ":" + str(session_id), cwd)
        db.kv_set(_KV_CWD, cwd)
    real_cwd = _cwd_for(session_id)
    # ★ R46：「干活」窗口里含糊要偏向干活 —— 只有"明确是闲聊/问候"才当聊天，
    # 其余（含「删除这个文件」这类 classify 抓不到的语序）一律进 agent。
    kind = "chat" if director.is_clearly_chat(text) else "work"
    if mode == "force_work":
        kind = "work"
    if kind == "chat":
        return {"ok": True, "kind": "chat", "task_id": "", "plan": None, "reply": ""}

    task_id = "t-%s" % uuid.uuid4().hex[:12]
    need_plan = director.needs_plan(text) and mode != "force_run"
    plan = director.build_plan(text, real_cwd) if need_plan else None
    task = {
        "task_id": task_id, "session_id": session_id, "character_id": character_id,
        "text": text, "cwd": real_cwd, "status": "awaiting_confirm" if need_plan else "running",
        "plan": plan, "events": [], "created_at": time.time(), "queue": [],
        "result": "", "error": "", "used": None, "size": None, "tool_calls": 0,
    }
    _TASKS[task_id] = task
    _SESSION_LAST[session_id] = task_id
    _prune()
    if need_plan:
        return {"ok": True, "kind": "plan", "task_id": task_id, "plan": plan, "reply": ""}
    asyncio.create_task(_run(task_id))
    return {"ok": True, "kind": "started", "task_id": task_id, "plan": None, "reply": ""}


async def confirm(session_id: str, character_id: str, task_id: str) -> dict:
    t = _TASKS.get(str(task_id or ""))
    if not t or t["session_id"] != session_id:
        return {"ok": False, "error": "任务不存在"}
    if t["status"] != "awaiting_confirm":
        return {"ok": False, "error": "任务不在待确认状态"}
    t["status"] = "running"
    asyncio.create_task(_run(task_id))
    return {"ok": True, "task_id": task_id}


async def _run(task_id: str):
    t = _TASKS.get(task_id)
    if not t:
        return
    key = t["session_id"] + "#agent"      # 轨迹走独立会话，不污染主聊天（spec §7.1）
    t["started_at"] = time.time()
    # ★ R52（Task 8 审查，Important）：整轮排空结束前**不许**落终态 ——
    #   老写法在第一个 prompt 一回来就置 done/stopped，于是 state.busy 在活还没干完时就是 False：
    #   send 的前置判断漏过 → 同一条桥上起第二个任务 → BridgeBusy；interject 也会答"任务没在跑"。
    t["status"] = "running"
    t.pop("_await_retry", None)           # ★ R52：这一轮真的在跑，上一轮的"等重试"标记作废
    t.pop("_retry_scheduled", None)
    # 注意：_top_ran 不在这里清 —— 它标记"顶部这条这一轮已跑成"，重试时据此跳过顶部只补队列
    try:
        br = await get_bridge(t["cwd"], _mode_for(t["session_id"]),
                              patch=PERSONA_OVERLAY,        # ★ R3：必须传，否则她说话不像助手
                              session_id=t["session_id"], character_id=t["character_id"])
        t["config_options"] = list(getattr(br, "config_options", []) or [])
        await br.ensure_session()
        model = str(db.kv_get(_KV_MODEL + ":" + str(t["session_id"])) or "")
        if model and "|" in model:
            p, m = model.split("|", 1)
            await br.set_model(p, m)

        async def on_event(ev):
            t["events"].append(ev)
            if ev.get("type") == "agent_tool":
                t["tool_calls"] = int(t.get("tool_calls") or 0) + 1
            if ev.get("type") == "agent_usage":
                t["used"], t["size"] = ev.get("used"), ev.get("size")
            if ev.get("type") == "agent_message" and str(ev.get("text") or "").strip():
                t["result"] = str(ev["text"])
            try:
                from ..main import ws_manager
                await ws_manager.push_to_session(t["session_id"], dict(ev, task_id=task_id))
            except Exception:
                pass

        brief = director.build_brief(
            t["text"], t.get("plan") or {}, character_name="", call_user="你", cwd=t["cwd"],
            memories=_memories(t["session_id"], t["character_id"]))
        # ★ R35（Task 5 复评）：桥上已有 prompt 在飞时**不能当失败**，也**绝不能静默丢**
        # ★ R60（复评）：`_top_ran` 必须**真的被读** —— 它标记"这一轮顶部那条已经跑成"。
        #   排空途中某条队列消息撞 BridgeBusy → 重试重新进 _run；若顶部已经跑成，
        #   这里**不能再发一次**（旧代码只写不读 → 队首在 harness 上跑两遍：重复副作用 + 双倍 token）。
        if not t.get("_top_ran"):
            try:
                stop = await br.prompt(brief, on_event=on_event)
            except BridgeBusy as e:
                # ★ R58：**不把 brief 塞回队列**（旧写法这么做 → 每次重试都重建 brief，字符串已经变了，
                #   队里那条就成了"幽灵 brief"，之后被当用户消息再跑一遍：连续两次 busy 实测发 4 次 prompt）。
                #   这一轮没跑成，重试重新进 _run，照样从 t["text"] 重建 brief —— 结构上就没有幽灵了。
                await _retry_or_exhaust(task_id, t, str(e))
                return
            t["stop_reason"] = stop
            t["_top_ran"] = True          # ★ R60：顶部真的跑成了；重试只补队列，不再跑它
        # ★ R35/R52：队列任何收尾之后都接着跑 —— 但用户喊了停就不再自动跑（stop_intent）；
        #   正在等重试时也不排空（_await_retry），交给 _retry_later
        while t.get("queue") and not t.get("stop_intent") and not t.get("_await_retry"):
            nxt = t["queue"].pop(0)
            # ★ R52 Critical：prompt() 返回的是 **stopReason**（"end_turn"），不是正文！
            #   老写法 `t["result"] = await br.prompt(...)` 会把 "end_turn" 当成她的话
            #   写进聊天记录 + #agent 行 + record_action。正文只从 agent_message 事件取。
            try:
                stop = await br.prompt(nxt, on_event=on_event)
            except BridgeBusy as e:
                t["queue"].insert(0, nxt)     # ★ R58：放回队首的是**用户原文**（不是 brief），别丢
                await _retry_or_exhaust(task_id, t, str(e))
                return
            t["stop_reason"] = stop
        # ★ R52/R58：终态在排空**之后**才算 —— 排空期间 status 一直是 running（busy 不撒谎）
        # ★ R60：`_top_ran` 由 `_settle()` 在这一轮真正结束时清掉（生命周期内它必须一直可读）
        if t.get("stop_intent"):
            # 用户喊停：不当结论落库（正文可能只是半句），队列也被 stop() 清过了
            _settle(t, "stopped")
        elif t["queue"]:
            _settle(t, "queued")              # 还有没跑完的（重试窗口）
        else:
            reason = str(t.get("stop_reason") or "")
            _settle(t, "stopped" if reason == "cancelled" else ("done" if reason == "end_turn" else "failed"))
    except Exception as e:
        t["status"] = "failed"
        t["error"] = "%s: %s" % (type(e).__name__, e)
        # ★ R58：失败路径也把"等重试/顶部已跑"的记账收拾干净，别留下挡住后续消息的残迹
        t.pop("_top_ran", None)
        t.pop("_await_retry", None)
        t.pop("_retry_scheduled", None)
    finally:
        t["ended_at"] = time.time()
        await _finish(t, key)


async def _retry_or_exhaust(task_id: str, t: dict, err: str) -> None:
    """★ R58：BridgeBusy 之后的统一出口 —— 有界重试；预算用完就给可见终态且**不丢消息**。

    老写法在这里置 queued 就 return，谁都不会再拉它（send 的前置判断、interject、confirm、
    stop、_prune 各有一条不认 queued 的路），那条消息等于静默没了。
    """
    t["error"] = str(err)
    t["status"] = "queued"
    t.pop("_await_retry", None)
    t["attempts"] = int(t.get("attempts") or 0) + 1
    if t["attempts"] <= _RETRY_MAX:
        # ★ 注意顺序：_retry_scheduled **必须留到 _finish 看过之后**（_finish 靠它判断
        #   "这次尝试没跑成、别记改造记忆"）。它会在 _run 下次进来时被清掉。
        t["_retry_scheduled"] = True
        asyncio.create_task(_retry_later(task_id))
        return
    await _exhaust(task_id, t)


async def _retry_later(task_id: str):
    t = _TASKS.get(task_id)
    if not t:
        return
    if not t.get("stop_intent"):
        # ★ R58：只有还在等的时候才需要等；用户已经喊停就不必再等（stop-on-queued 的死锁在这里解掉）
        try:
            await asyncio.sleep(_RETRY_DELAY)
        except Exception:
            pass
    t = _TASKS.get(task_id)
    if not t:
        return
    if t.get("status") in ("done", "failed", "stopped", "cancelled"):
        return                            # 别人已经把它收尾了，别再插一脚
    if t.get("stop_intent"):
        # ★ R58（复评）：这里必须给**真终态**再走 —— 老写法直接 return，状态永远挂在 queued：
        #   stop 之后 interject 答"任务没在跑"、stop 答"任务已结束"、state.busy=False，
        #   而 queued 在 _IN_FLIGHT 里 → 该会话**之后每一条 send 都被排在它后面**（静默被吞，无 API 可救）。
        _settle(t, "stopped")
        await _finish(t, t["session_id"] + "#agent")
        return
    await _run(task_id)


async def _exhaust(task_id: str, t: dict) -> None:
    """★ R58：重试预算用完 —— 给可见终态，并**把还没跑的用户消息迁移到新任务**。

    老写法直接 return：任务变 failed（已不在 _IN_FLIGHT），用户在这段窗口里发的消息
    就永久挂在一条**够不着**的队列上（stop/interject/send 都不再理它）。
    """
    err = str(t.get("error") or "桥一直忙")
    t.pop("_top_ran", None)
    _settle(t, "failed")
    t["error"] = "%s（已重试 %d 次仍未排上）" % (err, _RETRY_MAX)
    left = [str(x) for x in (t.get("queue") or []) if str(x or "").strip()]
    if not left:
        return
    t["queue"] = []
    try:
        nid = await _migrate(session_id=t["session_id"], character_id=t["character_id"],
                             texts=left, cwd=t["cwd"])
        t["error"] += "；其中还没跑的 %d 条已转到新任务 %s，正在继续" % (len(left), nid)
    except Exception as e:
        t["error"] += "；其中还没跑的 %d 条没能接手：%s: %s" % (len(left), type(e).__name__, e)


async def _migrate(*, session_id: str, character_id: str, texts: list, cwd: str) -> str:
    """把没人接手的用户消息搬到一条新任务上继续跑（绝不让它们变成够不着的孤儿）。"""
    texts = [str(x) for x in (texts or []) if str(x or "").strip()]
    if not texts:
        raise ValueError("没有要迁移的消息")
    tid = "t-%s" % uuid.uuid4().hex[:12]
    _TASKS[tid] = {
        "task_id": tid, "session_id": session_id, "character_id": character_id,
        "text": texts[0], "cwd": cwd or _cwd_for(session_id), "status": "running",
        "plan": None, "events": [], "created_at": time.time(), "queue": texts[1:],
        "result": "", "error": "", "used": None, "size": None, "tool_calls": 0, "migrated": True,
    }
    _SESSION_LAST[session_id] = tid
    _prune()
    asyncio.create_task(_run(tid))
    return tid


def _forget_last(t: dict) -> None:
    """★ 折进来的第 3 条：`_SESSION_LAST` 不许指着一条已经结束的任务。

    state() 是 `tid = _SESSION_LAST.get(session_id)` 再取 `_TASKS[tid]` —— 指针留在一条
    永远不会再动的任务上，窗口读到的就是一张死卡。
    """
    sid = str(t.get("session_id") or "")
    if sid and _SESSION_LAST.get(sid) == t.get("task_id"):
        _SESSION_LAST.pop(sid, None)


def _settle(t: dict, status: str) -> None:
    """★ R58：落状态时把"等重试/重试已排"这些记账一并收拾干净。

    不收干净 = 死锁源：任务已经不在 _IN_FLIGHT 了，残留标记还会挡住后续消息
    （send 的前置判断、_prune、interject 都按这些标记做决定）。
    ★ R60：`_top_ran` 也在这里清 —— 它的生命周期就是"本轮排空结束前一直可读"，
      到这一刻（真的落终态）这一轮才算结束。
    注意：**不清 queue** —— 队列里是用户的话，退场时要么跑掉、要么被 _exhaust 迁移，绝不静默丢。
    """
    t["status"] = status
    t.pop("_await_retry", None)
    t.pop("_retry_scheduled", None)
    t.pop("_top_ran", None)


def _memories(session_id: str, character_id: str) -> str:
    try:
        from .memory import build_memory_block
        return build_memory_block(session_id, character_id) or ""
    except Exception:
        return ""


# ★ R66：终态 → WS 事件名。聊天里的「她正在干活」联动卡**只认这三个事件收卡**，
#   而 ACP 的 map_update 从不发它们（只发 thinking / message / tool / result / usage / approval）——
#   卡因此建得出来、收不掉，任务干完（乃至用户亲手按停）之后聊天里还挂着「她正在干活 · 第 N 步」，
#   只有重进聊天（Chat.rerender 重建 #chat-body）才消失。所以必须在**结算**这一处补推。
_TERMINAL_EVENT = {"done": "agent_final", "stopped": "agent_stopped",
                   "cancelled": "agent_stopped", "failed": "agent_error"}


async def _push_agent_terminal(t: dict) -> None:
    """把「这次活结束了」推回主会话（与 proactive 汇报同一条 WS 路）。

    幂等：同一个任务只推一次（先打标再推 —— 推失败也不重复推，重进聊天能自愈）。
    只对真正的终态发（done/stopped/cancelled/failed）；running/queued/awaiting_confirm 不是结束。
    """
    etype = _TERMINAL_EVENT.get(str(t.get("status") or ""))
    if not etype or t.get("_terminal_pushed"):
        return
    t["_terminal_pushed"] = True
    payload = {"type": etype, "task_id": t.get("task_id"),
               "session_id": t.get("session_id"), "character_id": t.get("character_id")}
    if etype == "agent_error" and str(t.get("error") or "").strip():
        payload["text"] = str(t["error"])          # 失败原因：她那边的窗口也要看得见
    try:
        from ..main import ws_manager
        await ws_manager.push_to_session(t["session_id"], payload)
    except Exception:
        pass


async def _finish(t: dict, agent_session: str):
    """收尾：转述 → 落库（打标签）→ proactive 推回聊天。

    ★ R52/R58：还没真跑成的尝试（BridgeBusy 排队等重试）**什么都不写**，也**不盖 ended_at**
      —— 它并没有结束（老写法在这里给未完成的尝试盖了结束时间，时间线上就成了假的）。
    ★ R58：用户喊停那一轮不当结论落库、也不进 #agent 轨迹（半句不是结论）。
    """
    if t.get("_await_retry") or t.get("_retry_scheduled"):
        return                            # 还没跑成/还在等重试：连 ended_at 都不盖（它没结束）
    # ★ R66：终止事件必须在所有 early-return **之前**推 —— 「没产出」「用户喊停」这些路
    #   同样是一次活的结束（而且恰恰是最容易留下一张永久陈旧卡的两种）。
    await _push_agent_terminal(t)
    try:
        raw = t.get("result") or t.get("error") or ""
        if not str(raw).strip():
            return                        # 没有任何产出/错误：没有可汇报的东西，不留空行
        if t.get("stop_intent"):
            return                        # 用户喊停：不转述（省一次 LLM）、不落库、不写轨迹
        say = await director.narrate_result(raw,
                                           character_id=t["character_id"], session_id=t["session_id"],
                                           character_name="", call_user="你")
        t["say"] = say
        if t["status"] in ("done", "stopped"):
            db.add_message(t["session_id"], "assistant", say, t["character_id"],
                           extra=director.tag_extra(t["task_id"], t["cwd"]))
            try:
                from ..main import ws_manager
                await ws_manager.push_to_session(t["session_id"], {
                    "type": "proactive", "role": "assistant", "content": say,
                    "session_id": t["session_id"], "character_id": t["character_id"],
                    "task_id": t["task_id"]})
            except Exception:
                pass
        if t["status"] in ("done", "failed"):
            db.add_message(agent_session, "assistant", raw,
                           t["character_id"], extra=director.tag_extra(t["task_id"], t["cwd"]))
        try:
            from . import memory as _agent_memory
            _agent_memory.record_action(t["session_id"], t["character_id"], "agent_task",
                                        (t.get("text") or "")[:80], say)
        except Exception:
            pass
    except Exception:
        pass


async def interject(session_id: str, task_id: str, text: str, *, interrupt: bool = False) -> dict:
    t = _TASKS.get(str(task_id or ""))
    if not t or t["session_id"] != session_id:
        return {"ok": False, "error": "任务不存在"}
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "空消息"}
    # ★ R60（复评 Minor1）：用户已经喊停（stop 进行中）时**不能收下**——
    #   `stop()` 会清队列，这时候收下的文本注定被丢；旧代码只看 status（此刻还可能是 running/queued）
    #   于是静默返回 ok=True，用户以为说了。宁可明确报错。
    if t.get("stop_intent"):
        return {"ok": False, "error": "任务已停止，这句没有发出去"}
    if t["status"] != "running":
        return {"ok": False, "error": "任务没在跑"}
    if interrupt:
        # ★ R35（Task 5 复评）：get_bridge 现在按 (cwd, 权限档, session_id) 隔离 ——
        # 少传 session_id 会**新建一条桥**，cancel 打在空会话上 = 静默无效（还多一个进程）
        # ★ R52：patch 也要传 —— 现在复用路径虽然忽略它，但将来桥被回收/恢复时
        #   少传就会静默丢掉人设（R3 失效），所以每个调用点都保持一致的实参
        br = await get_bridge(t["cwd"], _mode_for(session_id), patch=PERSONA_OVERLAY,
                              session_id=session_id, character_id=t.get("character_id") or "default")
        await br.cancel()                 # ACP 一次只允许一个 prompt：要插话必须先停
        t["queue"].insert(0, text)
        return {"ok": True, "mode": "interrupt"}
    t["queue"].append(text)
    return {"ok": True, "mode": "queue", "pending": len(t["queue"])}


async def stop(session_id: str, task_id: str) -> dict:
    t = _TASKS.get(str(task_id or ""))
    if not t or t["session_id"] != session_id:
        return {"ok": False, "error": "任务不存在"}
    if t["status"] == "awaiting_confirm":
        # ★ 折进来的第 3 条：取消**还没开工**的计划也必须走终态。老写法只 `status="cancelled"` 就 return：
        #   ① 不发终止事件 ⇒ 前端那张计划卡永远收不掉（后端 confirm 只会回「任务不在待确认状态」）；
        #   ② 不盖 ended_at ⇒ 任务表里留一条"永远没结束"的记录；
        #   ③ _SESSION_LAST 还指着这条永远不会再动的任务；
        #   ④ 不清 t["queue"]（running/queued 分支会清）⇒ 计划卡挂着时主人补的那句话成了搁死文本
        #      （cancelled 不在 _IN_FLIGHT 里，send 再也不会把它排出去）。
        dropped = [str(x) for x in (t.get("queue") or [])]
        t["queue"] = []            # 与 running/queued 分支一致：他说停就真停（R58：留着只会卡住状态）
        t["stop_intent"] = True    # 用户喊停：_finish 据此不转述、不落库（省一次 LLM，也不留半句结论）
        _settle(t, "cancelled")
        t["ended_at"] = time.time()
        await _finish(t, t["session_id"] + "#agent")   # 里面推带 task_id 的终态事件（cancelled → agent_stopped）
        _forget_last(t)            # 别让 _SESSION_LAST 指着一条已经结束的任务
        res = {"ok": True}
        if dropped:
            # 丢掉的是**用户的话**：必须回报给调用方（静默丢是 R58 那类 Critical 问题）
            res["dropped"] = len(dropped)
            res["dropped_texts"] = dropped
        return res
    if t["status"] in ("running", "queued"):
        # ★ R52：先立"用户说停"的意图再 cancel —— 排空循环认这个旗标，
        #   否则 cancel 之后队列里剩下的还会被自动跑掉（他说停就得真停）
        # ★ R58：顺手清空队列（用户的话已确认不再跑，留着只会把状态卡在 queued）；
        #   _retry_later 醒来见 stop_intent 会立刻落 stopped 终态，不留死锁
        t["stop_intent"] = True
        t["queue"] = []
        br = await get_bridge(t["cwd"], _mode_for(session_id), patch=PERSONA_OVERLAY,
                              session_id=session_id, character_id=t.get("character_id") or "default")
        await br.cancel()
        return {"ok": True}
    return {"ok": False, "error": "任务已结束"}


async def approve(approval_id: str, allow: bool, reason: str = "") -> dict:
    """approval_id = agent/approval.py 生成的审批 id（前端审批卡 data-aw-approve 就是它）。"""
    from . import approval as _approval
    aid = str(approval_id or "")
    if not aid:
        return {"ok": False, "error": "缺 approval_id"}
    ok = _approval.approve(aid) if allow else _approval.reject(aid, reason)
    return {"ok": bool(ok)}


async def set_upgrade(session_id: str, enable: bool, *, confirmed: bool = False) -> dict:
    """升档是二段式：先要一次确认，确认后才真正切换（并另起进程，见 spec §6.3）。

    ★ 收尾修复：收回升档原来调 `stop_all()` —— 把**所有会话**的桥全停了：别人正在飞的
    `session/prompt` 当场 `RuntimeError: client stopped`，任务落 failed，`_finish` 再把那条
    agent_error 推进用户聊天（别的会话挂着的审批也被判拒）。前端那句「改回项目级」不该有这种副作用。
    现在只关「放开档」的桥（`stop_bridges("danger-full-access")`，别的档一条都不动）；
    本会话若还有活正跑在放开档的桥上，就**拒绝并说清**：不动桥、也不改档（档位照实留在放开档上，
    绝不假装收回了 —— 这是权限，说错了他会以为已经安全）。
    """
    if enable and not confirmed:
        return {"ok": True, "need_confirm": True,
                "message": "放开权限意味着她可以动工作区外的文件、跑任意命令。要继续吗？"}
    mode = "danger-full-access" if enable else "workspace-write"
    if not enable and widened_busy(session_id):
        return {"ok": False,
                "error": "她还有一件活正跑在放开档上 —— 先按「停」再收回；这次没有改档，权限仍是放开的。"}
    db.kv_set(_KV_MODE + ":" + str(session_id), mode)
    if not enable:
        await stop_bridges("danger-full-access")   # 只关放开的那条桥，别人的活还在跑
    return {"ok": True, "need_confirm": False, "permission_mode": mode}


def _prune():
    """★ R52：在飞的任务（running / awaiting_confirm / queued）一律不清理 ——
    老写法只护 running，会把等人的计划卡和正在重试排队的任务清掉（用户看到的卡突然消失）。"""
    if len(_TASKS) <= _MAX_KEEP:
        return
    for tid in sorted(_TASKS, key=lambda k: _TASKS[k].get("created_at") or 0)[:len(_TASKS) - _MAX_KEEP]:
        if _TASKS[tid].get("status") not in _IN_FLIGHT:
            _TASKS.pop(tid, None)
