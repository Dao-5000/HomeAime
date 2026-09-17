# -*- coding: utf-8 -*-
"""分层检索：按相关性取原文片段，时间分层兜底。

策略（2026-09-13 改造）：
- **相关优先**：先用当前这句消息去原文片段索引里做本地语义检索（零 API、毫秒级），
  命中就把那几段**原文**带出来（带日期 + 时间锚点）——名字、日期、原话都在原文里，
  比"月总结里一句概括"准得多。
- **分层兜底**：没命中（或索引还没建好）时，回落到原来的时间分层：
  近 7 天：日总结 + 原文（每篇从头部开始，别只给睡前那几句）
  7~30 天：周总结
  30 天以上：月总结
"""
from datetime import datetime, timedelta

from . import paths

# 检索命中时，原文片段的字符预算（约 800~1200 字，够 3~4 段）
RELEVANT_BUDGET_CHARS = 1200
# 最多带几段（每段还会带前后邻居，段数别多）
RELEVANT_TOP_K = 3


def _read(fp) -> str:
    try:
        return fp.read_text("utf-8")
    except Exception:
        return ""


def _recent_raw(character_id: str, days: int, per_day_chars: int) -> str:
    """近 N 天的原文（每篇截断）。

    ★ 每篇从**最早没被覆盖的部分**往后取，而不是取尾部：一天聊得多的时候，
    取尾等价于"只记得睡前那几句"，早上说的重要事永远进不了上下文。
    """
    now = datetime.now()
    parts = []
    for i in range(days):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        txt = _read(paths.raw_dir(character_id) / (d + ".md")).strip()
        if txt:
            parts.append(f"【{d} 原文】\n{txt[:per_day_chars]}")
    return "\n\n".join(parts)


def _recent_daily(character_id: str, days: int) -> str:
    now = datetime.now()
    parts = []
    for i in range(days):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        txt = _read(paths.daily_dir(character_id) / (d + ".md")).strip()
        if txt:
            parts.append(f"【{d} 日总结】\n{txt}")
    return "\n\n".join(parts)


def _weekly(character_id: str, limit: int = 12) -> str:
    parts = []
    for fp in sorted(paths.weekly_dir(character_id).glob("*.md"), reverse=True)[:limit]:
        txt = _read(fp).strip()
        if txt:
            parts.append(f"【{fp.stem} 周总结】\n{txt}")
    return "\n\n".join(parts)


def _monthly(character_id: str, limit: int = 12) -> str:
    parts = []
    for fp in sorted(paths.monthly_dir(character_id).glob("*.md"), reverse=True)[:limit]:
        txt = _read(fp).strip()
        if txt:
            parts.append(f"【{fp.stem} 月总结】\n{txt}")
    return "\n\n".join(parts)


def _index_state(character_id: str) -> dict:
    """索引现状（诊断用）。任何失败都返回空 dict。"""
    try:
        from . import index as _idx
        st = _idx.stats(character_id)
        return {
            "ready": bool(_idx.embedding_ready()),
            "chunks": st.get("chunks", 0),
            "days": st.get("days", 0),
            "pending_days": st.get("pending_days", 0),
            "model": st.get("model", ""),
        }
    except Exception:
        return {}


def _block_state(session_id: str, character_id: str) -> dict:
    """摘要块现状（诊断用）。

    ★ 2026-09-13 修正：这里原先把 limit **硬编码成 3**，与实际注入条数脱节 ——
      我把 BLOCK_INJECT_LIMIT 改成 2 之后，诊断仍显示 3 个区间，
      看日志会误判成"改没生效"。现在直接读 summary_manager 的配置，
      配置改到几就报几，诊断不再自己发明一个数。
    """
    try:
        from .. import db
        try:
            from ..summary_manager import BLOCK_INJECT_LIMIT as _lim
        except Exception:
            _lim = 3
        blocks = db.get_summary_blocks(session_id, character_id, int(_lim))
        return {
            "n_blocks": db.count_summary_blocks(session_id, character_id),
            "injected_limit": int(_lim),
            "recent_ranges": [
                "%s..%s" % (b.get("from_msg_id"), b.get("to_msg_id")) for b in blocks
            ],
        }
    except Exception:
        return {}


def build_memory_context(character_id: str, user_text: str = "",
                         max_chars: int = 4000, session_id: str = "") -> str:
    """拼一块「外置记忆库」上下文，供 enrich_messages 注入（大模型翻记忆）。

    相关优先：先按 user_text 检索原文片段；命中就以它为主，再用少量时间分层兜底。
    """
    # 1. 相关原文片段（命中才占预算）
    relevant = ""
    hits = []
    try:
        from . import index as _idx
        hits = _idx.search_with_neighbors(character_id, user_text,
                                          top_k=RELEVANT_TOP_K, span=1)
    except Exception:
        hits = []
    if hits:
        try:
            relevant = _format_hits(hits, RELEVANT_BUDGET_CHARS)
        except Exception:
            relevant = ""

    blocks = []
    if relevant:
        blocks.append(relevant)

    # 2. 时间分层兜底
    fallback_chars = 0
    if relevant:
        # 已经有精准原文了：日总结/周/月只留一点点做"最近整体脉络"，把预算让给片段
        tail_budget = max(600, max_chars - len(relevant))
        recent = "\n\n".join(x for x in (
            _recent_daily(character_id, 3),
            _weekly(character_id, 2),
        ) if x)
        if recent:
            tail = recent[:tail_budget]
            fallback_chars = len(tail)
            blocks.append(tail)
    else:
        recent = "\n\n".join(x for x in (
            _recent_daily(character_id, 7),
            _recent_raw(character_id, 7, 1200),
        ) if x)
        if recent:
            blocks.append(recent)
        weekly = _weekly(character_id, 8)
        if weekly:
            blocks.append(weekly)
        monthly = _monthly(character_id, 6)
        if monthly:
            blocks.append(monthly)

    text = "\n\n".join(blocks).strip()
    if not text:
        # 连兜底都没有（新角色/库空）——也要留一条记录，否则"没注入"无从判断
        try:
            from . import trace as _trace
            _trace.summarise_block(character_id, user_text, [], "",
                                   0, 0, _index_state(character_id),
                                   _block_state(session_id, character_id),
                                   session_id=session_id,
                                   internal=not str(user_text or "").strip())
        except Exception:
            pass
        return ""

    out = "【外置记忆库（你上下文之外的长期记忆，可参考）】\n" + text[:max_chars]

    # ★ 诊断：把"这次到底检索到什么、注入了多少"落盘。
    #   改造前这一层完全静默，出了问题只能靠"感觉她忘了"，没法区分
    #   「没检索到」「检索到但排错」「注入了但被裁掉」「根本没触发」。
    try:
        from . import trace as _trace
        _trace.summarise_block(
            character_id, user_text, hits, out,
            len(relevant), fallback_chars,
            _index_state(character_id),
            _block_state(session_id, character_id),
            session_id=session_id,
            internal=not str(user_text or "").strip(),
        )
    except Exception:
        pass

    return out


def _format_hits(hits: list, budget_chars: int = RELEVANT_BUDGET_CHARS) -> str:
    """把检索到的原文片段拼成注入块（带日期 + 时间锚点）。"""
    out = []
    used = 0
    for h in hits:
        span = h.get("first_time") or ""
        last = h.get("last_time") or ""
        if span and last and last != span:
            span = f"{span}-{last}"
        head = f"【{h['day']}{(' ' + span) if span else ''}】"
        piece = head + "\n" + str(h.get("text") or "").strip()
        if used + len(piece) > budget_chars and out:
            break
        out.append(piece)
        used += len(piece)

    if not out:
        return ""

    return (
        "【可能相关的过去对话原文（按当前话题检索到的原始记录；"
        "细节、名字、原话都在这里，直接用，别说成是回忆不起的模糊印象）】\n"
        + "\n\n".join(out)
    )


def build_relevant_block(character_id: str, user_text: str,
                         budget_chars: int = RELEVANT_BUDGET_CHARS) -> str:
    """按相关性取原文片段。没有任何命中时返回空串（调用方走时间兜底）。"""
    if not str(user_text or "").strip():
        return ""
    try:
        from . import index as _idx
        # 带前后邻居：片段是硬切的，关键那句可能正好落在边界、另一半在隔壁片段里
        hits = _idx.search_with_neighbors(character_id, user_text,
                                          top_k=RELEVANT_TOP_K, span=1)
    except Exception:
        return ""
    if not hits:
        return ""
    try:
        return _format_hits(hits, budget_chars)
    except Exception:
        return ""


def list_archive(character_id: str) -> dict:
    """列出记忆库结构，供前端浏览页展示。返回 {原文:[], 日总结:[], 周总结:[], 月总结:[]}。"""
    def _ls(d, reverse=True):
        if not d.exists():
            return []
        return sorted([f.name for f in d.glob("*.md")], reverse=reverse)

    return {
        "原文": _ls(paths.raw_dir(character_id)),
        "日总结": _ls(paths.daily_dir(character_id)),
        "周总结": _ls(paths.weekly_dir(character_id)),
        "月总结": _ls(paths.monthly_dir(character_id)),
    }
