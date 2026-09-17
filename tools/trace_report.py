# -*- coding: utf-8 -*-
"""HomeAime 追踪数据自动诊断报告（只读）

把 trace/YYYY-MM-DD.jsonl + chat_history + token:caller 合并成一份**带结论**的报告：
不是罗列数据，而是按"有没有问题"给出诊断清单。

用法：
    python tools/trace_report.py                 # 今天的诊断
    python tools/trace_report.py --days 3        # 最近 3 天
    python tools/trace_report.py --session s_xx  # 只看某会话
    python tools/trace_report.py --raw turn      # 导出某类原始记录（turn/instr/memory/pipeline/err）

诊断项（每项给 [OK] / [注意] / [问题]）：
  D1  空回复率            AI 有没有"没说话"
  D2  指令遵守           用户说了"别/不要/记住"之后，AI 是否照做（最关键）
  D3  回复重复           近似复读率 + 最重复的句子
  D4  回复长度健康度      过短/过长比例
  D5  prompt 规模         system 字数、块数、压缩模式是否真的省了
  D6  记忆管线健康度      抽出/入库/重复 比例（"记忆为什么没涨"）
  D7  后处理可用性        6 项后处理哪些在报错（"功能没接"vs"接了但坏"）
  D8  异常聚合            按模块统计错误，指出最需要修的那块
  D9  token 成本          按调用点排行 + 每轮均值
  D10 主动消息            投递/拦截情况
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
TRACE_DIR = os.path.join(DATA, "trace")


def ok(c):
    return "[OK]  " if c else "[问题]"


def warn(c):
    return "[OK]  " if c else "[注意]"


def load_trace(days, session=None):
    rows = []
    for i in range(days):
        d = (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        fp = os.path.join(TRACE_DIR, d + ".jsonl")
        if not os.path.exists(fp):
            continue
        for line in open(fp, encoding="utf-8", errors="ignore"):
            try:
                j = json.loads(line)
            except Exception:
                continue
            if session and j.get("sid") != session:
                continue
            rows.append(j)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--session", default="")
    ap.add_argument("--raw", default="")
    a = ap.parse_args()

    print("数据目录:", DATA)
    print("追踪目录:", TRACE_DIR)
    rows = load_trace(a.days, a.session or None)
    kinds = collections.Counter(r.get("kind") for r in rows)
    print("记录: 共 %d 条  %s" % (len(rows), dict(kinds)))
    if not rows:
        print()
        print("!! 追踪目录里没有数据。可能原因：")
        print("   1) 后端还没重启（埋点在 2026-09-14 才加入，需重启并重新打包）")
        print("   2) 还没聊天（每回合才会写一条 turn）")
        print("   3) 数据目录不在 %s（可设 AI_COMPANION_DATA_DIR 指定）" % DATA)
        return

    if a.raw:
        print()
        print("=== 原始记录 kind=%s（最多 20 条）===" % a.raw)
        for r in [x for x in rows if x.get("kind") == a.raw][-20:]:
            print("  " + json.dumps(r, ensure_ascii=False)[:300])
        return

    turns = [r for r in rows if r.get("kind") == "turn"]
    errs = [r for r in rows if r.get("kind") == "err"]
    mems = [r for r in rows if r.get("kind") == "memory"]
    pipes = [r for r in rows if r.get("kind") == "pipeline"]
    instrs = [r for r in rows if r.get("kind") == "instr"]

    # ------------------------------------------------------------ D1 空回复
    print()
    print("=" * 76)
    print("D1 空回复率")
    print("=" * 76)
    if turns:
        empty = [t for t in turns if t.get("error") == "empty_reply" or not t.get("reply_n")]
        r = len(empty) / len(turns)
        print("  %s %d/%d 回合没产出内容（%.1f%%）" % (ok(r < 0.05), len(empty), len(turns), r * 100))
        for t in empty[:3]:
            print("     例: %s | user=%s" % (str(t.get("ts"))[5:16], str(t.get("user_excerpt"))[:40]))
    else:
        print("  (没有 turn 记录)")

    # ------------------------------------------------------------ D2 指令遵守
    print()
    print("=" * 76)
    print("D2 指令遵守（用户说「别/不要/记住」之后 AI 有没有照做）")
    print("=" * 76)
    con = sqlite3.connect(DB) if os.path.exists(DB) else None
    if con:
        con.row_factory = sqlite3.Row
    # 找出带指令的 turn，并取紧随其后的 AI 回复核对
    flagged = [t for t in turns if t.get("instr")]
    print("  带指令信号的回合: %d / %d" % (len(flagged), len(turns)))
    viol = []
    for t in flagged[-20:]:
        kinds_l = t.get("instr") or []
        uex = str(t.get("user_excerpt") or "")
        rex = str(t.get("reply_excerpt") or "")
        issue = None
        # 规则1：用户说不要语音 → AI 不该带 audio
        if any(k.startswith("negate:") and ("语音" in k or "声音" in k or "发" in k or "念" in k) for k in kinds_l):
            if t.get("audio"):
                issue = "用户要求别发语音，但本条回复带语音"
        # 规则2：用户说别说 X → 回复里不该立刻出现 X 的重复
        if any(k == "correct" for k in kinds_l) and not rex:
            issue = issue or "用户纠错后 AI 空回复"
        if issue:
            viol.append((t, issue))
    if viol:
        print("  %s 疑似未遵守 %d 例：" % ("[问题]", len(viol)))
        for t, why in viol[-8:]:
            print("     %s  user=%s" % (str(t.get("ts"))[5:16], str(t.get("user_excerpt"))[:40]))
            print("              → %s" % why)
            print("              AI: %s" % str(t.get("reply_excerpt"))[:60])
    else:
        print("  %s 未发现明确的指令未遵守（基于 turn 内证据）" % "[OK]  ")

    # 再用 chat_history 做一次跨回合核对（语音指令→下一条是否带 audio）
    if con:
        try:
            for r in con.execute(
                """SELECT id, session_id, character_id, content, timestamp FROM chat_history
                   WHERE role='user' AND (content LIKE '%不要发语音%' OR content LIKE '%别发语音%'
                        OR content LIKE '%打字不要%' OR content LIKE '%别语音%')
                   ORDER BY id DESC LIMIT 5"""):
                nxt = con.execute(
                    """SELECT content, extra FROM chat_history WHERE session_id=? AND character_id=?
                       AND id>? AND role='assistant' ORDER BY id LIMIT 1""",
                    (r["session_id"], r["character_id"], r["id"])).fetchone()
                if nxt:
                    has_audio = bool(nxt["extra"] and "audio" in str(nxt["extra"]))
                    print("  %s 跨回合核对 #%s「%s」→ 下一条%s" % (
                        ok(not has_audio), r["id"], (r["content"] or "")[:24],
                        "带语音 ★不合规★" if has_audio else "纯文字 合规"))
        except Exception as e:
            print("  跨回合核对失败:", e)

    # ------------------------------------------------------------ D3 重复
    print()
    print("=" * 76)
    print("D3 回复重复（近似复读）")
    print("=" * 76)
    if turns:
        norm = collections.Counter()
        for t in turns:
            c = re.sub(r"[\s\*\[\]（）()，。！？~…、,\.!\?]", "", str(t.get("reply_excerpt") or ""))
            if len(c) >= 6:
                norm[c[:40]] += 1
        dup = [(k, v) for k, v in norm.most_common(8) if v > 1]
        tot_dup = sum(v - 1 for _, v in dup)
        rate = tot_dup / len(turns) if turns else 0
        print("  %s 近似重复 %d 条（占 %.1f%%）" % (warn(rate < 0.15), tot_dup, rate * 100))
        for k, v in dup[:5]:
            print("     x%-3d %s" % (v, k[:52]))

    # ------------------------------------------------------------ D4 长度
    print()
    print("=" * 76)
    print("D4 回复长度健康度")
    print("=" * 76)
    if turns:
        lens = sorted(int(t.get("reply_chars") or 0) for t in turns)
        n = len(lens)
        short = sum(1 for x in lens if 0 < x <= 6)
        empt = sum(1 for x in lens if x == 0)
        long_ = sum(1 for x in lens if x > 200)
        print("  中位=%d 均值=%d 最长=%d" % (lens[n // 2], sum(lens) // n, lens[-1]))
        print("  %s 过短(<=6字) %d 条；空 %d 条；过长(>200字) %d 条"
              % (warn(short + empt <= n * 0.1), short, empt, long_))

    # ------------------------------------------------------------ D5 prompt 规模
    print()
    print("=" * 76)
    print("D5 prompt 规模（system 字数 / 块数 / 压缩模式）")
    print("=" * 76)
    if turns:
        by_compact = collections.defaultdict(list)
        for t in turns:
            by_compact[bool(t.get("compact"))].append((t.get("sys_chars") or 0, t.get("blocks") or 0))
        for mode, vals in sorted(by_compact.items()):
            syss = [v[0] for v in vals]
            blks = [v[1] for v in vals]
            tag = "压缩模式" if mode else "完整模式"
            print("  %s：%d 回合  system 中位=%d 字  块数中位=%d"
                  % (tag, len(vals), sorted(syss)[len(syss) // 2], sorted(blks)[len(blks) // 2]))
        if True in by_compact and False in by_compact:
            a_ = sorted(v[0] for v in by_compact[False])[len(by_compact[False]) // 2]
            b_ = sorted(v[0] for v in by_compact[True])[len(by_compact[True]) // 2]
            print("  %s 压缩模式实测省 %d 字/轮（%.0f%%）" % (ok(b_ < a_), a_ - b_, (a_ - b_) * 100.0 / a_ if a_ else 0))
        else:
            print("  [注意] 只观察到一种模式；想对比请分别聊一段再回来看")

    # ------------------------------------------------------------ D6 记忆管线
    print()
    print("=" * 76)
    print("D6 记忆管线健康度（抽出 / 入库 / 判重）")
    print("=" * 76)
    if mems:
        ex = sum(int(m.get("extracted") or 0) for m in mems)
        ins = sum(int(m.get("inserted") or 0) for m in mems)
        dup = sum(int(m.get("duplicated") or 0) for m in mems)
        print("  %d 次抽取：抽出 %d 条 / 入库 %d 条 / 判重跳过 %d 条" % (len(mems), ex, ins, dup))
        if ex:
            print("  %s 判重率 %.0f%%（偏高说明提炼重复或去重过严）"
                  % (warn(dup * 100.0 / ex < 60), dup * 100.0 / ex))
        if ex == 0:
            print("  [问题] 抽出 0 条 —— 提炼可能一直失败（看 D8 异常聚合）")
        kc = collections.Counter()
        for m in mems:
            for k, v in (m.get("kinds") or []):
                kc[k] += v
        if kc:
            print("  入库类型分布:", dict(kc.most_common(8)))
    else:
        print("  (还没有记忆抽取记录——AUTO_MEMORY_INTERVAL 轮才触发一次，多聊几轮)")

    # ------------------------------------------------------------ D7 后处理
    print()
    print("=" * 76)
    print("D7 后处理可用性（6 项：关系/升级/AI状态/人格/表达守卫/反思）")
    print("=" * 76)
    if pipes:
        okc = collections.Counter()
        failc = collections.Counter()
        for p in pipes:
            for k in (p.get("ok") or []):
                okc[k] += 1
            for k in (p.get("failed") or []):
                failc[k] += 1
        for k in ("relationship", "ritual", "ai_state", "personality", "guard", "reflection"):
            n_ok, n_f = okc.get(k, 0), failc.get(k, 0)
            print("  %s %-14s 成功 %-5d 失败 %d" % (ok(n_f == 0), k, n_ok, n_f))
        if failc.get("guard_hit"):
            print("  [注意] 禁止表达命中 %d 次（只记录不改写，属预期行为）" % failc["guard_hit"])
    else:
        print("  (还没有 pipeline 记录)")

    # ------------------------------------------------------------ D8 异常
    print()
    print("=" * 76)
    print("D8 异常聚合（按模块，指出最该修的一块）")
    print("=" * 76)
    if errs:
        bym = collections.Counter(e.get("module") or "?" for e in errs)
        print("  共 %d 条异常，按模块:" % len(errs))
        for m_, n in bym.most_common(10):
            print("    %-24s %d" % (m_, n))
        print("  --- 最近 5 条 ---")
        for e in errs[-5:]:
            print("    %s [%s] %s | %s" % (str(e.get("ts"))[5:16], e.get("module"),
                                          str(e.get("msg"))[:50], str(e.get("detail"))[:60]))
    else:
        print("  %s 没有记录到异常" % "[OK]  ")

    # ------------------------------------------------------------ D9 token
    print()
    print("=" * 76)
    print("D9 token 成本")
    print("=" * 76)
    if con:
        keys = ["token:caller:%s:" % (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(a.days)]
        agg = collections.Counter()
        for r in con.execute("SELECT key, value FROM kv WHERE key LIKE 'token:caller:%'"):
            if not any(r["key"].startswith(p) for p in keys):
                continue
            caller = r["key"].split(":", 3)[3]
            try:
                i_, _, o_ = str(r["value"]).partition(",")
                agg[caller] += int(i_) + int(o_)
            except Exception:
                pass
        tot = sum(agg.values())
        if tot:
            for caller, v in agg.most_common(12):
                print("    %-44s %8d  (%4.1f%%)" % (caller, v, v * 100.0 / tot))
            print("    %-44s %8d" % ("合计", tot))
            if turns:
                print("    每轮均值 ≈ %d token（%d 回合）" % (tot // max(1, len(turns)), len(turns)))
        else:
            print("  (无 token:caller 记录)")

    # ------------------------------------------------------------ D10 主动消息
    print()
    print("=" * 76)
    print("D10 主动消息")
    print("=" * 76)
    prows = [r for r in rows if r.get("kind") == "proactive"]
    if prows:
        send = collections.Counter(r.get("sub") for r in prows)
        print("  记录:", dict(send))
    else:
        fp = os.path.join(DATA, "proactive_trace.jsonl")
        if os.path.exists(fp):
            cut = (datetime.datetime.now() - datetime.timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%S")
            c = collections.Counter()
            for line in open(fp, encoding="utf-8", errors="ignore"):
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if str(j.get("ts", "")) >= cut:
                    c[j.get("kind")] += 1
            print("  proactive_trace.jsonl:", dict(c))
        else:
            print("  (无记录)")

    if con:
        con.close()

    print()
    print("=" * 76)
    print("提示：完整原始数据在 %s" % TRACE_DIR)
    print("      `--raw turn` 可导出某一类原始记录细看")
    print("=" * 76)


main()
