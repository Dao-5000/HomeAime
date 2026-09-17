# -*- coding: utf-8 -*-
"""聊天数据分析（只读）—— 供事后排查"她为什么这么回"、量化回复质量与 token 成本。

用法：
    python tools/chat_report.py                    # 今天的概览
    python tools/chat_report.py --days 3           # 最近 3 天
    python tools/chat_report.py --session s_xxx    # 只看某个会话
    python tools/chat_report.py --export out.json  # 导出原始数据供进一步分析
    python tools/chat_report.py --tail 40          # 附带最近 40 条对话原文

输出分七块：
  1 规模概览    消息数 / 角色 / 时间跨度
  2 回复质量    AI 回复长度分布 + **重复话术检测**（同一句说过多少次）
  3 合规风险    用户"纠正/否定/抱怨"类发言 → 问题回合定位
  4 疑似违规    用户明确指令（别/不要/记得…）之后 AI 是否照做
  5 token 成本  按天 + 按调用点（来自 token:caller:* 记账）
  6 主动消息    投递与拦截情况（来自 proactive_trace.jsonl）
  7 时间线      最近对话原文（--tail 控制条数）

★ 全程只读：不写库、不改配置、不动任何记忆。仅读取 local_db.db 与两个 jsonl 日志。
"""
import argparse
import collections
import datetime
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("AI_COMPANION_DATA_DIR") or os.path.join(os.environ.get("APPDATA", ""), "HomeAime", "data")
DB = os.path.join(DATA, "local_db.db")


def _conn():
    if not os.path.exists(DB):
        print("找不到数据库:", DB)
        sys.exit(1)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _since(days):
    return (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------- 1 规模概览
def sec_overview(cur, days, session=None):
    print("=" * 78)
    print("一、规模概览（最近 %d 天）" % days)
    print("=" * 78)
    sql = "SELECT session_id, character_id, COUNT(*) n FROM chat_history WHERE timestamp>=?"
    args = [_since(days)]
    if session:
        sql += " AND session_id=?"
        args.append(session)
    sql += " GROUP BY session_id, character_id ORDER BY n DESC"
    tot = 0
    for r in cur.execute(sql, args):
        tot += r["n"]
        print("  会话 %-26s 角色 %-10s %5d 条" % (r["session_id"], r["character_id"], r["n"]))
    if not tot:
        print("  (该区间没有消息)")
    print("  合计 %d 条" % tot)
    if session:
        return tot
    return tot


# ---------------------------------------------------------------- 2 回复质量
def sec_quality(cur, days, session=None):
    print()
    print("=" * 78)
    print("二、回复质量")
    print("=" * 78)
    sql = ("SELECT content, extra, timestamp, session_id, character_id FROM chat_history "
           "WHERE role='assistant' AND timestamp>=?")
    args = [_since(days)]
    if session:
        sql += " AND session_id=?"
        args.append(session)
    rows = list(cur.execute(sql, args))
    if not rows:
        print("  (无 AI 回复)")
        return rows
    lens = []
    for r in rows:
        c = (r["content"] or "").strip()
        # 表情包标记行与纯语音转写也计入长度，但单独标注
        lens.append(len(c))
    lens.sort()
    n = len(lens)
    def pct(p):
        return lens[min(n - 1, int(n * p))]
    print("  AI 回复 %d 条；长度 中位=%d 均值=%d p10=%d p90=%d 最长=%d"
          % (n, lens[n // 2], sum(lens) // n, pct(0.1), pct(0.9), lens[-1]))
    short = sum(1 for x in lens if x <= 6)
    long_ = sum(1 for x in lens if x > 200)
    print("  过短(<=6字) %d 条(%.1f%%)  过长(>200字) %d 条(%.1f%%)"
          % (short, short * 100.0 / n, long_, long_ * 100.0 / n))

    # 重复话术：同一句说过的次数
    print()
    print("  【重复话术 Top 12】（同一句/近似句出现次数，越多越像「复读」）")
    norm = collections.Counter()
    for r in rows:
        c = re.sub(r"[\s\*\[\]（）()，。！？~…、,\.!\?]", "", (r["content"] or ""))
        if len(c) >= 6:
            norm[c[:40]] += 1
    dup = [(k, v) for k, v in norm.most_common(12) if v > 1]
    if not dup:
        print("    (无重复，良好)")
    for k, v in dup:
        print("    x%-3d %s" % (v, k[:56]))
    tot_dup = sum(v - 1 for _, v in dup)
    print("    近似重复条数 %d（占 %.1f%%）" % (tot_dup, tot_dup * 100.0 / n))
    return rows


# ---------------------------------------------------------------- 3 合规风险
def sec_risk(cur, days, session=None):
    print()
    print("=" * 78)
    print("三、合规风险（用户纠正/否定/抱怨 → 定位问题回合）")
    print("=" * 78)
    pat = re.compile(r"不要|别这样|说错|又错|不对|不是这|你怎么|答非所问|听不懂|没听懂|重复|说过多少|"
                     r"让你|我叫你|叫你|为什么不|怎么又|别再|不用|错了")
    sql = ("SELECT id, role, content, timestamp, session_id, character_id FROM chat_history "
           "WHERE role='user' AND timestamp>=?")
    args = [_since(days)]
    if session:
        sql += " AND session_id=?"
        args.append(session)
    sql += " ORDER BY id"
    hits = []
    for r in cur.execute(sql, args):
        c = r["content"] or ""
        if pat.search(c):
            hits.append(r)
    print("  命中 %d 条用户消息（占总用户消息的一部分，仅作线索）" % len(hits))
    for r in hits[-15:]:
        print("    #%s %s  %s" % (r["id"], r["timestamp"][5:16], (r["content"] or "")[:46]))
        nxt = cur.execute(
            "SELECT role, content FROM chat_history WHERE session_id=? AND character_id=? AND id>? "
            "ORDER BY id LIMIT 2", (r["session_id"], r["character_id"], r["id"])).fetchall()
        for m in nxt:
            if m["role"] == "assistant":
                print("         → AI: %s" % (m["content"] or "")[:70])
                break
    return hits


# ---------------------------------------------------------------- 4 指令合规
def sec_instruction(cur, days, session=None):
    print()
    print("=" * 78)
    print("四、疑似指令未遵守（用户明确说了「别/不要/记得」之后）")
    print("=" * 78)
    rules = [
        ("不要发语音/打字", re.compile(r"(不要|别|不用)[^，。！？]{0,4}(语音|发声音)|打字(就行|就好|吧)|不要发语音")),
        ("不要重复", re.compile(r"(不要|别)(再)?(重复|说同样|复读)")),
        ("叫我/称呼", re.compile(r"叫我[^\s，。]{1,6}")),
        ("记住某事", re.compile(r"记住[：: ]?[^\n]{2,30}")),
        ("别催我/别管", re.compile(r"(别|不要)(催|管|烦)我")),
    ]
    sql = ("SELECT id, content, timestamp, session_id, character_id FROM chat_history "
           "WHERE role='user' AND timestamp>=?")
    args = [_since(days)]
    if session:
        sql += " AND session_id=?"
        args.append(session)
    sql += " ORDER BY id"
    users = list(cur.execute(sql, args))
    for label, pat in rules:
        hit = [r for r in users if pat.search(r["content"] or "")]
        if not hit:
            continue
        print("  【%s】命中 %d 次" % (label, len(hit)))
        for r in hit[-5:]:
            print("    #%s %s  %s" % (r["id"], r["timestamp"][5:16], (r["content"] or "")[:44]))
            # 看之后 3 条内 AI 是否照做（这里只列出 AI 实际回复，判断留给人/后续分析）
            nxt = cur.execute(
                "SELECT content FROM chat_history WHERE session_id=? AND character_id=? AND id>? AND role='assistant' "
                "ORDER BY id LIMIT 2", (r["session_id"], r["character_id"], r["id"])).fetchall()
            for m in nxt:
                print("         → AI: %s" % (m["content"] or "")[:70])


# ---------------------------------------------------------------- 5 token 成本
def sec_tokens(cur, days):
    print()
    print("=" * 78)
    print("五、token 成本")
    print("=" * 78)
    rows = cur.execute(
        "SELECT key, value FROM kv WHERE key LIKE 'token:date:%' ORDER BY key DESC LIMIT ?",
        (max(1, days),)).fetchall()
    if not rows:
        print("  (无 token:date 记录)")
    for r in rows:
        print("  %-24s %10s" % (r["key"].replace("token:date:", ""), r["value"]))
    print()
    print("  【按调用点拆分（最近 %d 天，来源 token:caller:*）】" % days)
    keys = ["token:caller:%s:" % (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(days)]
    agg = collections.Counter()
    for r in cur.execute("SELECT key, value FROM kv WHERE key LIKE 'token:caller:%'"):
        k = r["key"]
        if not any(k.startswith(p) for p in keys):
            continue
        caller = k.split(":", 3)[3]
        try:
            i_, _, o_ = str(r["value"]).partition(",")
            agg[caller] += int(i_) + int(o_)
        except Exception:
            pass
    if not agg:
        print("    (无记录——需后端跑过一段时间，且本次改动已加载)")
    tot = sum(agg.values())
    for caller, v in agg.most_common(20):
        print("    %-44s %8d  (%4.1f%%)" % (caller, v, v * 100.0 / tot if tot else 0))
    if tot:
        print("    %-44s %8d" % ("合计", tot))


# ---------------------------------------------------------------- 6 主动消息
def sec_proactive(days):
    print()
    print("=" * 78)
    print("六、主动消息投递与拦截（proactive_trace.jsonl）")
    print("=" * 78)
    fp = os.path.join(DATA, "proactive_trace.jsonl")
    if not os.path.exists(fp):
        print("  (无 proactive_trace.jsonl)")
        return
    cut = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    kinds = collections.Counter()
    delivers, skips = [], []
    for line in open(fp, encoding="utf-8", errors="ignore"):
        try:
            j = json.loads(line)
        except Exception:
            continue
        if str(j.get("ts", "")) < cut:
            continue
        kinds[j.get("kind")] += 1
        if j.get("kind") == "deliver":
            delivers.append(j)
        elif j.get("kind") == "skip":
            skips.append(j)
    print("  记录分布:", dict(kinds))
    print("  投递 %d 条 / 拦截 %d 条" % (len(delivers), len(skips)))
    byw = collections.Counter(d.get("who") or "?" for d in delivers)
    print("  投递来源 Top:", dict(byw.most_common(8)))
    byr = collections.Counter(s.get("reason") or "?" for s in skips)
    print("  拦截原因 Top:", dict(byr.most_common(8)))
    for d in delivers[-8:]:
        print("    %s [%s] %s字 %s" % (str(d.get("ts"))[5:16], d.get("ptype") or "-",
                                       d.get("chars") or 0, str(d.get("preview") or "")[:40]))


# ---------------------------------------------------------------- 7 时间线
def sec_timeline(cur, tail, session=None):
    print()
    print("=" * 78)
    print("七、最近 %d 条对话原文" % tail)
    print("=" * 78)
    sql = "SELECT id, role, content, timestamp, extra FROM chat_history"
    args = []
    if session:
        sql += " WHERE session_id=?"
        args.append(session)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(tail)
    for r in reversed(list(cur.execute(sql, args))):
        tag = "语音" if (r["extra"] and "audio" in (r["extra"] or "")) else ""
        who = "TA" if r["role"] == "user" else "AI"
        print("  #%-6s %s %s%s %s" % (r["id"], str(r["timestamp"])[5:16], who,
                                      ("[%s]" % tag) if tag else "", (r["content"] or "").replace("\n", " / ")[:96]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--session", default="")
    ap.add_argument("--tail", type=int, default=24)
    ap.add_argument("--export", default="")
    a = ap.parse_args()

    print("数据库:", DB)
    print("区间: 最近 %d 天%s" % (a.days, ("  会话=" + a.session) if a.session else ""))
    cur = _conn()
    sec_overview(cur, a.days, a.session or None)
    sec_quality(cur, a.days, a.session or None)
    sec_risk(cur, a.days, a.session or None)
    sec_instruction(cur, a.days, a.session or None)
    sec_tokens(cur, a.days)
    sec_proactive(a.days)
    sec_timeline(cur, a.tail, a.session or None)

    if a.export:
        out = {"generated": datetime.datetime.now().isoformat(), "days": a.days, "session": a.session}
        sql = "SELECT * FROM chat_history WHERE timestamp>=?"
        args = [_since(a.days)]
        if a.session:
            sql += " AND session_id=?"
            args.append(a.session)
        sql += " ORDER BY id"
        out["messages"] = [dict(r) for r in cur.execute(sql, args)]
        with open(a.export, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print()
        print("已导出 %d 条消息到 %s" % (len(out["messages"]), a.export))
    cur.close()


main()
