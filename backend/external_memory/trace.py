# -*- coding: utf-8 -*-
"""记忆链路诊断日志（结构化 JSONL）。

为什么要有它（2026-09-13）：
  改造前整条记忆链路**几乎全静默** —— 实测 backend_dev.log 里
  `[ExtMemory]` 0 条、摘要 0 条、检索 0 条。出了问题只能靠"感觉她忘了"，
  没法定位是「没检索到」「检索到了但排错」「注入了但被裁掉」还是「根本没触发」。
  用户也不可能为了调一个问题聊几十轮。

记什么（每次注入一行，JSONL，方便直接统计）：
  · 当前这句话
  · 相关检索命中几段、每段的日期/分数/词面分、是否命中同一段
  · 实际注入多少字、有没有走时间兜底
  · 索引状态（可用吗、还差几天、共几段）
  · 摘要链状态（块数、当前注入的摘要长度）

怎么用：
  python -c "import json;[print(json.dumps(json.loads(l),ensure_ascii=False)) for l in open(r'...\\memory_trace.jsonl',encoding='utf-8')]"
  或直接看 DATA_DIR/memory_trace.jsonl
"""
import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()
_MAX_BYTES = 4 * 1024 * 1024    # 单文件上限，超了滚动一格（保留 .1）
_ENABLED = True


def _trace_path() -> Path:
    try:
        from .. import config
        return Path(config.DATA_DIR) / "memory_trace.jsonl"
    except Exception:
        return Path(os.path.expanduser("~")) / "memory_trace.jsonl"


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
    """追加一条诊断记录。任何失败都静默 —— 诊断绝不能影响主流程。"""
    if not _ENABLED:
        return
    try:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind}
        for k, v in fields.items():
            row[k] = v
        line = json.dumps(row, ensure_ascii=False)
        with _LOCK:
            fp = _trace_path()
            _rotate(fp)
            with fp.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def summarise_block(character_id: str, query: str, hits: list, injected: str,
                    relevant_chars: int, fallback_chars: int,
                    index_state: dict, block_state: dict,
                    session_id: str = "", internal: bool = False) -> None:
    """记录一次「外置记忆块」的组装结果 —— 这是诊断"她为什么忘了"的主线。

    internal/空 query 的记录要能一眼区分出来：主动消息、内部生成这些链路
    本来就没有用户文本，检索必然为空，混在真人聊天的记录里会误导判断
    （实测一开始就是 8 条空记录，看起来像"检索全废"，其实是后台生成）。
    """
    try:
        record(
            "memory_block",
            char=character_id,
            session=session_id or "",
            internal=bool(internal),
            query=str(query or "")[:120],
            n_hits=len(hits or []),
            hits=[
                {
                    "day": h.get("day"),
                    "seq": h.get("seq"),
                    "score": h.get("score"),
                    "vec": h.get("vec"),
                    "lex": h.get("lex"),
                    "t": "%s-%s" % (h.get("first_time") or "", h.get("last_time") or ""),
                    "preview": str(h.get("text") or "").replace("\n", " ")[:60],
                }
                for h in (hits or [])[:5]
            ],
            relevant_chars=int(relevant_chars or 0),
            fallback_chars=int(fallback_chars or 0),
            injected_chars=len(injected or ""),
            index=index_state or {},
            blocks=block_state or {},
        )
    except Exception:
        pass


def record_summary_block(session_id: str, character_id: str, from_id: int,
                         to_id: int, block_chars: int, merged_chars: int,
                         total_blocks: int) -> None:
    """记录一次摘要块追加（确认"只追加不重写"真的在跑）。"""
    record(
        "summary_block",
        session=session_id,
        char=character_id,
        from_id=int(from_id or 0),
        to_id=int(to_id or 0),
        block_chars=int(block_chars or 0),
        merged_chars=int(merged_chars or 0),
        total_blocks=int(total_blocks or 0),
    )


def record_ext_memory_tick(character_id: str, archived: int, daily_built: list,
                           weekly_built: list, monthly_built: str,
                           index_result: dict) -> None:
    """记录一次外置记忆库 tick 的结果（确认归档/总结/索引真的在推进）。"""
    record(
        "ext_tick",
        char=character_id,
        archived=int(archived or 0),
        daily=len(daily_built or []),
        daily_days=list(daily_built or [])[:10],
        weekly=len(weekly_built or []),
        weekly_keys=list(weekly_built or [])[:10],
        monthly=monthly_built or "",
        index=index_result or {},
    )
