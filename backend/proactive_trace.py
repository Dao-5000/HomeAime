# -*- coding: utf-8 -*-
"""主动消息诊断日志（结构化 JSONL）。

为什么要有它（2026-09-13 实测）：
  用户睡觉期间被连发 20+ 条主动消息，排查时**查不出是哪条 check 发的** ——
  `[Scheduler]` 那次窗口里 0 条日志，`[IdleAgent]` 只有"跳过"记录，
  **成功投递完全静默**。和记忆链路改造前一模一样的问题：成功无声、失败才出声，
  出了问题只能靠猜。

记什么：
  · kind=deliver —— 真的发出去的一条（谁发的、什么类型、要不要 QQ、多长）
  · kind=skip    —— 被拦下的一条（哪一道门禁拦的、为什么）
  · kind=sleep   —— 睡眠标记的写入/清除（时间线，用来还原"她以为用户在干嘛"）

怎么用：
  DATA_DIR/proactive_trace.jsonl
  python backend/tools/memory_report.py            # 报告里会一并汇总
"""
import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()
_MAX_BYTES = 4 * 1024 * 1024
_ENABLED = True

# 同一 (角色, 原因) 的 skip 记录去抖：IdleAgent 每 ~30 秒检查一次，
# 6 小时静默会产生 700+ 条完全相同的记录，把有用信息淹掉。
# 每次投递尝试仍然会记 deliver，只有 skip 去抖。
_SKIP_DEBOUNCE_SEC = 300
_last_skip = {}


def _trace_path() -> Path:
    try:
        from . import config
        return Path(config.DATA_DIR) / "proactive_trace.jsonl"
    except Exception:
        return Path(os.path.expanduser("~")) / "proactive_trace.jsonl"


def _rotate(fp: Path) -> None:
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
    """追加一条记录。任何失败都静默 —— 诊断绝不能影响主动消息本身。"""
    if not _ENABLED:
        return
    try:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
        row.update(fields)
        line = json.dumps(row, ensure_ascii=False)
        with _LOCK:
            fp = _trace_path()
            _rotate(fp)
            with fp.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def caller(depth: int = 3) -> str:
    """取调用方函数名 —— 用来回答"这条到底是谁发的"。

    _deliver 被 20+ 个地方调用，以前只能看到"发了"，看不到"谁发的"。
    """
    try:
        import inspect
        fr = inspect.stack()[depth]
        return str(fr.function or "")
    except Exception:
        return ""


def _settings_snapshot(character_id: str) -> dict:
    """记录判定当时的有效设置（间隔上下限 / 可用时段）。

    ★ 为什么必须落盘（2026-09-15）：事后体检时若拿"今天的设置"去判"昨天的消息"，
      会得出完全错误的结论 —— 用户 13:33 才把时段改成 20:00-01:44，而那之前
      15:00 发的消息在原设置（09:00-01:44）下其实是合规的。
      把当时的值写进记录，历史判定才可信。
    """
    out = {}
    try:
        from . import config as _cfg
        _lo, _hi = _cfg.proactive_interval_pair(character_id)
        out["lo_min"] = float(_lo or 0)
        out["hi_min"] = float(_hi or 0)
    except Exception:
        pass
    try:
        out["win"] = str(_cfg.resolve_active_hours(character_id) or "")
    except Exception:
        pass
    return out


def record_deliver(session_id: str, character_id: str, msg: str, *,
                   proactive_type: str = "", scheduled: bool = False,
                   qq: bool = False, dnd_silent: bool = False,
                   source: str = "") -> None:
    """记录一条**真的发出去**的主动消息。"""
    try:
        record(
            "deliver",
            session=session_id,
            char=character_id,
            who=source or caller(),
            ptype=proactive_type or "general",
            scheduled=bool(scheduled),
            qq=bool(qq),
            dnd_silent=bool(dnd_silent),
            chars=len(str(msg or "")),
            preview=str(msg or "").replace("\n", " ")[:80],
            **_settings_snapshot(character_id),
        )
    except Exception:
        pass


def record_skip(session_id: str, character_id: str, reason: str, *,
                detail: str = "", source: str = "",
                debounce: bool = True) -> None:
    """记录一条**被拦下**的主动消息。

    debounce=True 时，同一 (角色, 原因) 每 5 分钟最多记一条 ——
    否则 6 小时的睡眠静默会灌进 700+ 条同样的记录。
    """
    try:
        key = (str(character_id), str(reason))
        now = time.time()
        if debounce:
            with _LOCK:
                last = _last_skip.get(key, 0)
                if now - last < _SKIP_DEBOUNCE_SEC:
                    return
                _last_skip[key] = now
        record(
            "skip",
            session=session_id,
            char=character_id,
            reason=reason,
            detail=str(detail or "")[:120],
            who=source or caller(),
        )
    except Exception:
        pass


def record_sleep(session_id: str, character_id: str, action: str,
                 hours: float = 0.0, note: str = "") -> None:
    """记录睡眠标记的写入/清除 —— 还原"她以为用户在干嘛"的时间线。"""
    try:
        record(
            "sleep",
            session=session_id,
            char=character_id,
            action=action,
            hours=round(float(hours or 0), 2),
            note=str(note or "")[:80],
        )
    except Exception:
        pass


def record_gate(session_id: str, character_id: str, *, who: str, ptype: str,
                decision: str, reason: str = "", detail: str = "",
                exempt: str = "", numbers: dict = None) -> None:
    """记录**每一次闸门裁决**（放行 or 拦截）及其数值依据（2026-09-15 新增）。

    为什么还要加一层（用户要求"后续主动消息来了就能知道更详细原因"）：
      已有 deliver/skip 只能回答"发了/被拦了"，回答不了：
        · 这次判定时，"距上次主动"到底是多少分钟？下限是多少？
        · 用户闲置多久？是不是刚聊完？
        · 是否走了豁免？豁免的是**间隔**还是**连时段一起豁免**？
      实测排查「设了 90-120 却 28 分钟又发一条」时，只能靠翻 print 日志拼时间线，
      而 idle_agent 的投递**连 deliver 都不记**，等于黑箱。
      gate 记录把判定依据结构化落盘，配合 tools/_watch_proactive_reasons.py 食用。

    decision: "allow" / "reject"
    exempt:   ""（无豁免）/ "full"（连时段也豁免）/ "interval"（只豁免间隔）
    numbers:  任意数值键值（idle_secs / since_last_min / lo_min / hi_min / in_window …）
    """
    try:
        row = {
            "session": session_id,
            "char": character_id,
            "who": str(who or "")[:40],
            "ptype": str(ptype or "")[:24],
            "decision": "allow" if str(decision) == "allow" else "reject",
            "reason": str(reason or "")[:40],
            "detail": str(detail or "")[:120],
        }
        if exempt:
            row["exempt"] = str(exempt)[:12]
        if numbers:
            row["n"] = {k: (round(v, 2) if isinstance(v, float) else v)
                        for k, v in dict(numbers).items() if v is not None}
        row.update(_settings_snapshot(character_id))
        record("gate", **row)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# 编造「我做过某事」的检测（2026-09-13 新增）
#
# 实测：用户在睡觉时收到「我把你桌面上的弹窗又突突了三个，连那个赖着不走的
# 广告都给毙了」—— 而**这个能力根本不存在**（agent/tools.py 里只有
# open_app/open_url/volume/screen_info/notify，control.py 里没有关弹窗），
# 主动消息链路也完全不走工具/执行反馈闭环。她只是在"演一个会关弹窗的角色"。
#
# 判据设计原则：**只抓带明确完成时、且指向真实世界操作**的说法。
#   · 主体词：弹窗/广告/后台/垃圾/缓存/文件/注册表/进程…（桌面侧的具体对象）
#   · 动作词：已完成的（帮你/给你 + 清/关/删/杀/毙/修/装/扫…）
# 刻意**不抓**情感类（"我陪你""我记得你"）和记忆类（"我记住了"），
# 否则会淹没在误报里，反而没人看。
# ══════════════════════════════════════════════════════════════════

import re as _re

_WORLD_OBJECT = (
    r"弹窗|广告|流氓软件|后台程序|后台进程|进程|缓存|临时文件|垃圾文件|注册表|"
    r"桌面|文件夹|磁盘|硬盘|C盘|D盘|驱动|病毒|木马|开机项|启动项"
)
# 完成态动作：必须带「了/掉/完/过」这类完成标记，才说明是"我做了"而不是
# "我想做/我能做/你桌面上有什么"。少了这个后缀会把"想知道你现在桌面上开着什么"
# 这种正常问句也抓进来（实测误报过）。
_DONE_ACT = (
    r"(?:突突|清掉|清理|清除|关掉|关闭|删掉|删除|毙掉|杀掉|杀死|"
    r"扫描|修复|装好|安装|卸载|整理|收拾|打包|备份)"
    r"(?:了|掉|完|过)"
)
_FAB_RE = _re.compile(
    # ① 对象在前："桌面上的弹窗…突突了"
    r"(?:" + _WORLD_OBJECT + r")[^。！？\n]{0,12}?(?:" + _DONE_ACT + r")"
    # ② 动作在前："帮你把缓存删掉了"
    + r"|(?:" + _DONE_ACT + r")[^。！？\n]{0,12}?(?:" + _WORLD_OBJECT + r")"
)


def detect_fabricated_action(msg: str) -> str:
    """返回命中的可疑片段（空串 = 没检测到）。

    注意：这是**启发式**，只能抓明说"我做了某项桌面操作"的句子。
    真要把这类问题根治，得让主动消息链路也走工具执行 + 执行反馈闭环
    （当前它什么都不走，纯文本生成）。
    """
    try:
        m = _FAB_RE.search(str(msg or ""))
        return m.group(0) if m else ""
    except Exception:
        return ""


def record_fabrication(session_id: str, character_id: str, msg: str,
                       hit: str, source: str = "") -> None:
    """记录一条"声称做了某事、但该能力可能不存在"的主动消息。"""
    try:
        record(
            "fabrication",
            session=session_id,
            char=character_id,
            hit=str(hit or "")[:60],
            who=source or caller(),
            preview=str(msg or "").replace("\n", " ")[:100],
        )
    except Exception:
        pass


def summarise(rows: list) -> dict:
    """汇总（给诊断报告用）：谁在发、被什么拦住、睡眠时间线、编造情况。"""
    deliv = [r for r in rows if r.get("kind") == "deliver"]
    skips = [r for r in rows if r.get("kind") == "skip"]
    sleeps = [r for r in rows if r.get("kind") == "sleep"]
    fabs = [r for r in rows if r.get("kind") == "fabrication"]

    by_who = {}
    by_type = {}
    for r in deliv:
        by_who[r.get("who") or "?"] = by_who.get(r.get("who") or "?", 0) + 1
        by_type[r.get("ptype") or "?"] = by_type.get(r.get("ptype") or "?", 0) + 1
    by_reason = {}
    for r in skips:
        by_reason[r.get("reason") or "?"] = by_reason.get(r.get("reason") or "?", 0) + 1

    return {
        "delivered": len(deliv),
        "skipped": len(skips),
        "sleep_events": len(sleeps),
        "fabrications": len(fabs),
        "by_who": sorted(by_who.items(), key=lambda kv: -kv[1]),
        "by_type": sorted(by_type.items(), key=lambda kv: -kv[1]),
        "by_reason": sorted(by_reason.items(), key=lambda kv: -kv[1]),
        "recent": deliv[-10:],
        "sleep_line": sleeps[-6:],
        "fab_recent": fabs[-5:],
    }
