# -*- coding: utf-8 -*-
"""聊天原文归档：把 chat_history 里新增的消息按天追加写入「原文/YYYY-MM-DD.md」。

断点续传：state.json 记录 archive_until_id（已归档到 chat_history 的哪条 id），
每次只归档 id 大于该断点的新消息，电脑反复开关机也不会漏、不会重复。

★★ 2026-09-17 修（P0：跨会话跳号永久丢历史）
   原实现**只有一个** archive_until_id，却按 (session_id, character_id) 过滤查询，
   而同一个角色名下会挂多个 session（真机实测骨子有 8 个：主 session 10237 行，
   外加 default / _routetest / _agenttest / s_trim_off_0915 / s_78d73b5d / …）。
   只要某个 tick 用别的 session 跑一次，游标就被抬到**那个 session 的最大 id**，
   其它 session 里 id 更小的未归档消息**再也不会被归档**（id>游标这条永远不成立）。
   实测后果：09-13 当天 523 行只归档了 87 行，**436 行（83%）永久查不到**；
   全库 5.4% 的历史在任何原文文件里都不存在 —— 用户问那天白天的事她永远答不上。

   现在：游标按 session 分开存（archive_until_id:<session_id>），
   并兼容读取旧的单一游标（首次升级时把旧值当作该 session 的起点，不重复归档已有内容）。
"""
from datetime import datetime

from .. import db
from . import paths


def _cursor_key(session_id: str) -> str:
    return "archive_until_id:" + str(session_id or "default")


def _day_of(ts) -> str:
    ts = str(ts or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts[:19], fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _fmt_time(ts) -> str:
    ts = str(ts or "").strip()
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except Exception:
        return ""


def archive_new_messages(session_id: str, character_id: str) -> int:
    """把 chat_history 里未归档的消息写入原文文件。返回本次归档条数。"""
    state = paths.load_state(character_id)
    # ★ 按 session 取各自的游标；旧的单一游标只作为**首次升级**的起点。
    until_id = int(state.get(_cursor_key(session_id))
                   or state.get("archive_until_id") or 0)
    try:
        rows = db.q(
            "SELECT id, role, content, timestamp FROM chat_history "
            "WHERE session_id=? AND character_id=? AND id>? ORDER BY id ASC LIMIT 2000",
            (session_id, character_id, until_id), fetch=True
        ) or []
    except Exception:
        return 0
    if not rows:
        return 0

    # db.q 返回 sqlite3.Row，统一转 dict 方便 .get 取值
    rows = [dict(r) for r in rows]

    paths.ensure_dirs(character_id)
    raw_dir = paths.raw_dir(character_id)

    by_day = {}
    for r in rows:
        by_day.setdefault(_day_of(r.get("timestamp") or ""), []).append(r)

    for day, msgs in by_day.items():
        fp = raw_dir / (day + ".md")
        lines = []
        if fp.exists():
            lines.append("")
        for m in msgs:
            role = "用户" if str(m.get("role") or "") == "user" else "AI"
            t = _fmt_time(m.get("timestamp") or "")
            content = str(m.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"## {t}" if t else "##")
            lines.append(f"{role}：{content}")
        if lines:
            try:
                with fp.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
            except Exception:
                pass

    new_until = int(rows[-1].get("id") or until_id)
    # ★ 只推进**本 session**的游标；保留旧键不动（其它 session 的兼容起点还要读它）。
    state[_cursor_key(session_id)] = new_until
    paths.save_state(character_id, state)
    return len(rows)
