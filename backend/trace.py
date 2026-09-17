# -*- coding: utf-8 -*-
"""HomeAime 运行追踪器（结构化数据收集，2026-09-14 新增）

目的：让"多聊天 → 自动积累可诊断数据 → 事后一眼看出问题"这条链闭合。
不用再靠翻 backend_dev.log 的散行日志猜。

设计原则（重要）
  1. **绝不影响主链路**：所有写入都是 try/except 全吞，任何失败静默。
  2. **不含大文本**：只记指标 + 短预览（<=120 字），避免文件爆炸、避免隐私堆积。
  3. **同步 append 单行 JSONL**：够快（微秒级），省去异步队列的复杂度与丢数据风险。
  4. **每天一个文件**：`DATA_DIR/trace/YYYY-MM-DD.jsonl`，超 8MB 自动滚动。

记录的事件类型
  turn        每回合完整快照（用户输入 → prompt 规模 → 回复 → 成本），诊断的主数据
  instr       用户指令信号（"别/不要/记住…"），用于事后核对 AI 有没有照做
  memory      记忆管线一次抽取的结果（抽出几条 / 插入几条 / 重复几条）
  pipeline    后台后处理的成功与失败（关系/AI状态/人格/反思…）
  proactive   主动消息投递或拦截（补充 proactive_trace 的视角）
  err         任何模块的异常（带模块名），用于快速定位"哪块在报错"

用法：
    from . import trace
    trace.turn(session_id=..., character_id=..., ...)
    trace.err("memory_manager", "抽取失败", exc=e)
"""
import json
import os
import re
import threading
import time
import traceback
from pathlib import Path

_LOCK = threading.RLock()
_MAX_BYTES = 8 * 1024 * 1024
_ENABLED = True
_ERR_DEDUP = {}          # (module, msg) -> 上次记录时间，避免刷屏
_ERR_DEDUP_SEC = 120

# 用户"不满/纠正/指令"信号词——这些回合最值得回头看
_INSTR_PAT = re.compile(
    r"(不要|别|不用|不许|不准|以后不要|别再)[^，。！？,!?]{0,6}"
    r"(发|说|做|用|叫|提|催|催我|语音|重复|念|读)"
)
_CORRECT_PAT = re.compile(r"(说错|又错|不对|不是这|你怎么|答非所问|听不懂|没听懂|重复|说过多少|错了|谁让你)")
_MEMO_PAT = re.compile(r"(记住|我叫|我喜欢|我不喜欢|我讨厌|我过敏|我的.{1,10}是|以后不要|别再)")


def _trace_dir() -> Path:
    try:
        from . import config
        d = Path(config.DATA_DIR) / "trace"
    except Exception:
        d = Path(os.path.expanduser("~")) / "HomeAime_trace"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def _path() -> Path:
    return _trace_dir() / (time.strftime("%Y-%m-%d") + ".jsonl")


def _rotate(fp: Path):
    try:
        if fp.exists() and fp.stat().st_size > _MAX_BYTES:
            bak = fp.with_suffix(".jsonl.1")
            try:
                if bak.exists():
                    bak.unlink()
            except Exception:
                pass
            fp.rename(bak)
    except Exception:
        pass


def record(kind: str, **fields) -> None:
    """追加一条轨迹。任何失败都静默——绝不因为埋点影响聊天。"""
    if not _ENABLED:
        return
    try:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
        for k, v in fields.items():
            if v is None or v == "" or v == [] or v == {}:
                continue
            row[k] = v
        line = json.dumps(row, ensure_ascii=False, default=str)
        if len(line) > 4000:
            line = line[:4000] + '"}'
        with _LOCK:
            fp = _path()
            _rotate(fp)
            with fp.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 具体事件

def instr(session_id: str, character_id: str, text: str, kinds: list) -> None:
    """用户指令/纠正信号。kinds 例：['negate_voice','correct','remember']"""
    record("instr", sid=session_id, char=character_id, kinds=kinds,
           text_excerpt=(text or "")[:120], text_len=len(text or ""))


def turn(session_id: str, character_id: str, user_text: str,
         system_chars: int = 0, block_count: int = 0, history_len: int = 0,
         prompt_compact: bool = None, understanding_on: bool = None,
         reply_count: int = 0, reply_chars: int = 0, reply_excerpt: str = "",
         has_audio: bool = False, has_sticker: bool = False,
         latency_ms: int = 0, model: str = "", elapsed_s: float = 0.0,
         instr_kinds: list = None, error: str = "") -> None:
    """一回合的完整快照。这是诊断主数据。"""
    record(
        "turn",
        sid=session_id, char=character_id,
        user_excerpt=(user_text or "")[:120], user_len=len(user_text or ""),
        sys_chars=system_chars, blocks=block_count, hist=history_len,
        compact=prompt_compact, understanding=understanding_on,
        reply_n=reply_count, reply_chars=reply_chars,
        reply_excerpt=(reply_excerpt or "")[:120],
        audio=has_audio or None, sticker=has_sticker or None,
        latency_s=round(float(latency_ms) / 1000.0, 2) if latency_ms else None,
        model=model or None, elapsed_s=round(float(elapsed_s), 2) if elapsed_s else None,
        instr=instr_kinds or None,
        error=(error or "")[:200] or None,
    )


def memory(session_id: str, character_id: str, extracted: int = 0,
           inserted: int = 0, duplicated: int = 0, superseded: int = 0,
           kinds: list = None, error: str = "") -> None:
    """记忆管线一次抽取的结果（每次 AUTO_MEMORY_INTERVAL 轮触发一次）。"""
    record("memory", sid=session_id, char=character_id,
           extracted=extracted, inserted=inserted, duplicated=duplicated,
           superseded=superseded or None, kinds=kinds or None,
           error=(error or "")[:200] or None)


def pipeline(session_id: str, character_id: str, ok: list = None,
             failed: list = None) -> None:
    """stream 出口的后处理批（1 关系 / 2 升级 / 3 AI状态 / 4 人格 / 5 表达守卫 / 6 反思）。"""
    record("pipeline", sid=session_id, char=character_id,
           ok=ok or None, failed=failed or None)


def proactive(kind: str, session_id: str, character_id: str, **fields) -> None:
    """主动消息侧（投递/拦截），与 proactive_trace 互补：这里带 session 便于回合关联。"""
    record("proactive", sub=kind, sid=session_id, char=character_id, **fields)


def reminder_verdict(session_id: str, character_id: str, user_text: str,
                     raw: str = "", verdict: dict = None, resolved: str = "",
                     minutes: int = None, error: str = "") -> None:
    """定时提醒的**判定取证**：把"输入 + 模型原始输出 + 换算结果"一起落盘。

    ★ 2026-09-17 新增。为什么必须有（真机事故的直接教训）：
      用户报「她又犯糊涂了」（凌晨 2:50 说"该睡觉了"被排成明天 02:50，1439 分钟后）。
      排查时发现日志里只有
        `[LLM] chat_once(ai_promise.extract_reminder_verdict) ... out=74`
      —— **不打印输入、也不打印模型原始输出**，于是"到底哪句话触发的、模型填了什么"
      全靠推断，没法给出结论。埋点缺一格，事故就无法复盘。

      现在每次判定都记：用户原话、模型原始 JSON、解析后的 is_reminder/时间档、
      换算出的触发时刻、距现在多少分钟。以后同类问题一眼可查。
    """
    try:
        v = verdict or {}
        record(
            "reminder_verdict",
            sid=session_id, char=character_id,
            u=(str(user_text or ""))[:200],
            raw=(str(raw or ""))[:300],
            is_reminder=v.get("is_reminder"),
            source=v.get("promise_source") or None,
            content=(str(v.get("content") or ""))[:80] or None,
            t_time=v.get("trigger_time") or None,
            t_day=v.get("trigger_day") or None,
            rel=v.get("relative_minutes") or None,
            resolved=resolved or None,
            minutes=minutes,
            error=(str(error or ""))[:160] or None,
        )
    except Exception:
        pass


def task_failed(session_id: str, character_id: str, task_id, content: str = "",
                reason: str = "", attempts=None, notified: bool = False) -> None:
    """定时任务**投递失败**的取证（含是否已告知用户）。

    ★ 2026-09-17 新增：真机 25 条任务里 5 条 failed，而用户完全不知道
      （视角就是"她答应了却没动静"）。这条埋点让"失败 + 有没有补告知"可查。
    """
    try:
        record("task_failed", sid=session_id, char=character_id,
               task_id=task_id, content=(str(content or ""))[:80] or None,
               reason=(str(reason or ""))[:160] or None,
               attempts=attempts, notified=True if notified else None)
    except Exception:
        pass


def err(module: str, msg: str, exc=None, session_id: str = "") -> None:
    """异常记录（同 (module,msg) 120 秒内只记一次，避免刷屏）。"""
    try:
        key = (module, str(msg)[:60])
        now = time.time()
        last = _ERR_DEDUP.get(key, 0)
        if now - last < _ERR_DEDUP_SEC:
            return
        _ERR_DEDUP[key] = now
        if len(_ERR_DEDUP) > 500:
            _ERR_DEDUP.clear()
        detail = ""
        if exc is not None:
            try:
                detail = "".join(traceback.format_exception_only(type(exc), exc))[:200]
            except Exception:
                detail = str(exc)[:200]
        record("err", module=module, msg=str(msg)[:200], detail=detail or None,
               sid=session_id or None)
    except Exception:
        pass


# ---------------------------------------------------------------- 文本信号识别

def detect_instr(text: str) -> list:
    """识别用户这句话里有没有"指令/纠正"信号，返回标签列表（供分析时定位回合）。

    优先级（实测修正）：**否定指令优先于记忆指令**。
    「你以后不要重复同一句话」同时含"以后不要"（记忆词表）与"不要重复"（否定指令），
    按原实现会被标成 remember —— 那是误判，它是一条行为约束，不是要记住的事实。
    """
    t = str(text or "")
    out = []
    if not t:
        return out
    try:
        neg = _INSTR_PAT.search(t)
        if neg:
            out.append("negate:" + (neg.group(2) or "")[:6])
        if _CORRECT_PAT.search(t):
            out.append("correct")
        # 只有没命中否定指令时，才把"以后不要/别再"之类的记忆词当记忆指令
        if not neg and _MEMO_PAT.search(t):
            out.append("remember")
        if "语音" in t or "声音" in t:
            out.append("mentions_voice")
    except Exception:
        pass
    return out


def stats(date_str: str = "") -> dict:
    """快速统计某天记录条数（供报告用）。"""
    day = date_str or time.strftime("%Y-%m-%d")
    fp = _trace_dir() / (day + ".jsonl")
    n = {}
    if not fp.exists():
        return {"file": str(fp), "counts": {}, "exists": False}
    try:
        with fp.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    k = json.loads(line).get("kind")
                except Exception:
                    continue
                n[k] = n.get(k, 0) + 1
    except Exception:
        pass
    return {"file": str(fp), "counts": n, "exists": True}
