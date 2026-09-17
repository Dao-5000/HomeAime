# -*- coding: utf-8 -*-
"""记忆链路诊断报告：读 memory_trace.jsonl + backend_dev.log，直接给结论。

用法（项目 venv 的 python）：
    python backend/tools/memory_report.py              # 全量诊断
    python backend/tools/memory_report.py --tail 20    # 多看几条注入明细
    python backend/tools/memory_report.py --stats      # 只看命中率汇总（跑一周后用）

给的是"能不能用"的判断，不是原始数据堆砌 —— 每项都带结论和依据。
"""
import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _data_dir() -> Path:
    """解析 App 真实使用的数据目录。

    ★ 注意 external_memory_dir() 的优先级里，非打包模式会落到 ROOT_DIR/外置记忆库
      （源码目录），而不是 DATA_DIR —— 直接用 backend 的函数查会查到**另一个空库**，
      看到 chunks=0 误判成"索引没建"。所以这里显式跟随打包版 App 的口径：
      DATA_DIR/外置记忆库（config.json 的 EXTERNAL_MEMORY_DIR 为空时的既定行为）。
    """
    d = os.environ.get("AI_COMPANION_DATA_DIR", "").strip()
    if d:
        return Path(d)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "HomeAime" / "data"
    return Path.home() / ".homeaime" / "data"


def _archive_dir(data_dir: Path) -> Path:
    """外置记忆库根目录：优先显式配置，其次 App 的 DATA_DIR/外置记忆库。"""
    em = os.environ.get("AI_COMPANION_EXTERNAL_MEMORY_DIR", "").strip()
    if em:
        return Path(em)
    try:
        cfg = json.loads((data_dir / "config.json").read_text("utf-8"))
        v = str(cfg.get("EXTERNAL_MEMORY_DIR") or "").strip()
        if v:
            return Path(v).expanduser()
    except Exception:
        pass
    return data_dir / "外置记忆库"


def _load_trace(fp: Path) -> list:
    rows = []
    if not fp.exists():
        return rows
    for line in fp.read_text("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def _ok(b):
    return "✓" if b else "✗"


def _count_raw_days(archive_dir: Path) -> int:
    try:
        d = archive_dir / "骨子" / "原文"
        if not d.exists():
            # 角色名不固定时：取记忆库里第一个角色目录
            for c in archive_dir.iterdir():
                if c.is_dir() and (c / "原文").exists():
                    d = c / "原文"
                    break
        return len([f for f in d.glob("*.md")]) if d.exists() else 0
    except Exception:
        return 0


def _live_index_stats(archive_dir: Path) -> dict:
    """直接读索引 SQLite 库拿现状。

    ★ 为什么要直读、不走 backend.index.stats()：
      那个模块会连带导入 memory.embedding，而 embedding_ready() 会**真的加载
      768 维模型**（首次还联网探测 HF，实测卡住 2 分钟以上）。
      一个诊断脚本绝不能因为"看状态"就把模型拉起来。
    """
    out = {}
    try:
        import sqlite3
        # ★ 多角色时选「原文天数最多」的那个，别按字母序取第一个 ——
        #   实测 'default' 排在 '骨子' 前面，按字母序会读到只有 1 天的空角色库，
        #   报告显示"待补 0 天 ✓"却是错的角色，等于用错样本下结论。
        candidates = []
        for c in (sorted(archive_dir.iterdir()) if archive_dir.exists() else []):
            cand = c / ".index" / "archive_vectors.db"
            if c.is_dir() and cand.exists():
                n_raw = len(list((c / "原文").glob("*.md"))) if (c / "原文").exists() else 0
                candidates.append((n_raw, c.name, cand))
        if not candidates:
            return {"_error": "还没建索引库（.index/archive_vectors.db 不存在）"}
        candidates.sort(reverse=True)
        _, char_name, db = candidates[0]
        out["_char"] = char_name
        out["_chars_all"] = ["%s(%d天)" % (d, n) for n, d, _ in candidates]
        con = sqlite3.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
        out["db"] = str(db)
        try:
            out["chunks"] = con.execute("SELECT COUNT(*) FROM archive_chunk").fetchone()[0]
            out["days"] = con.execute("SELECT COUNT(*) FROM archive_day").fetchone()[0]
        except Exception as e:
            out["_error"] = "索引表读不到: %s" % e
        try:
            meta = {r[0]: r[1] for r in con.execute("SELECT k, v FROM archive_meta")}
            out["model"] = meta.get("model", "")
            out["dim"] = meta.get("dim", "")
        except Exception:
            pass
        # 待补天数 = 原文里有、archive_day 里没有的（排除今天，今天还在追加）
        try:
            import time as _t
            today = _t.strftime("%Y-%m-%d")
            raw = {f.stem for f in (archive_dir / out["_char"] / "原文").glob("*.md")}
            done = {r[0] for r in con.execute("SELECT day FROM archive_day")}
            out["pending_days"] = len([d for d in raw if d < today and d not in done])
            out["raw_days"] = len([d for d in raw if d < today])
        except Exception:
            pass
        con.close()
    except Exception as e:
        return {"_error": str(e)}
    return out


def _model_cached() -> bool:
    """embedding 模型是否已缓存到本地（只看文件，不加载模型）。"""
    try:
        hub = Path(os.path.expanduser("~")) / ".cache" / "huggingface" / "hub"
        if not hub.exists():
            return False
        return any(hub.glob("models--*text2vec*")) or any(hub.glob("models--*MiniLM*"))
    except Exception:
        return False


def _is_real_chat(b) -> bool:
    """真人聊天记录：有用户文本，且不是后台生成。"""
    q = str(b.get("query") or "").strip()
    return bool(q) and not b.get("internal")


def hitrate_stats(blocks: list, days: int = 7) -> dict:
    """按天汇总命中率 —— 跑一周后用它回答"该不该开门控"。

    ★ 为什么要按天看趋势，而不是只看总命中率：
      0% 命中可能是两件完全不同的事 ——
        (a) 索引还没建好（前 40 分钟，基建问题，等就行）
        (b) 检索真的没找到（调优问题，要改算法）
      只看总数会把 (a) 算进 (b)，得出"检索不行"的错误结论。
      按天看就能看出命中率是否随时间上升、何时稳定。
    """
    real = [b for b in blocks if _is_real_chat(b)]
    if not real:
        return {"n": 0}

    by_day = collections.OrderedDict()
    for b in real:
        day = str(b.get("ts") or "")[:10] or "?"
        by_day.setdefault(day, []).append(b)

    trend = []
    for day, items in by_day.items():
        hits = [x for x in items if (x.get("n_hits") or 0) > 0]
        trend.append({
            "day": day,
            "n": len(items),
            "hits": len(hits),
            "rate": 100.0 * len(hits) / len(items),
        })

    hits_all = [b for b in real if (b.get("n_hits") or 0) > 0]
    # 索引在建时的记录（pending_days>0）单独标出来，避免误算成"检索不行"
    still_building = [b for b in real if ((b.get("index") or {}).get("pending_days") or 0) > 0]

    def _avg(seq, key):
        vals = [int(b.get(key) or 0) for b in seq]
        return int(sum(vals) / len(vals)) if vals else 0

    return {
        "n": len(real),
        "hits": len(hits_all),
        "rate": 100.0 * len(hits_all) / len(real),
        "trend": trend[-days:],
        "during_build": len(still_building),
        "avg_injected": _avg(real, "injected_chars"),
        "avg_relevant": _avg(real, "relevant_chars"),
        "avg_fallback": _avg(real, "fallback_chars"),
        "miss_queries": [str(b.get("query") or "")[:44] for b in real
                         if (b.get("n_hits") or 0) == 0][-12:],
    }


def print_stats(stats: dict) -> None:
    print("=" * 66)
    print("⑦ 命中率统计（判断「该不该开门控」的依据）")
    if not stats.get("n"):
        print("   还没有真人聊天记录 —— 先在 App 里正常聊几句")
        return

    n, h, r = stats["n"], stats["hits"], stats["rate"]
    print("   真人聊天 %d 次，检索命中 %d 次（%.0f%%）" % (n, h, r))
    print()
    print("   按天趋势（命中率是否在上升 → 索引建完前后对比）：")
    for t in stats["trend"]:
        bar = "█" * int(round(t["rate"] / 10)) + "·" * (10 - int(round(t["rate"] / 10)))
        print("      %s  %2d 次  命中 %2d  %s %3.0f%%"
              % (t["day"], t["n"], t["hits"], bar, t["rate"]))

    if stats.get("during_build"):
        print()
        print("   ⚠ 其中 %d 次发生在「索引还没建完」时（pending_days>0）——"
              % stats["during_build"])
        print("     这些不该算进检索质量，看上面的趋势判断是否随索引完善而上升")

    print()
    print("   平均注入 %d 字（相关片段 %d + 时间兜底 %d）"
          % (stats["avg_injected"], stats["avg_relevant"], stats["avg_fallback"]))
    if stats["avg_fallback"] > stats["avg_relevant"]:
        print("   → 兜底占比大于相关片段：说明大部分轮次没命中，仍在付时间分层的 token")
    else:
        print("   → 相关片段占比更高：检索在正常工作")

    if stats.get("miss_queries"):
        print()
        print("   没命中的提问（该不该命中要人工看，这决定下一步调哪里）：")
        for q in stats["miss_queries"]:
            print("      · %s" % q)

    print()
    print("   ── 怎么用这张表做决定 ──")
    print("   命中率高且稳定 → 闲聊轮不注入是安全的，可以开门控省 token")
    print("   命中率低但提问都很泛（'在吗''嗯'）→ 本来就不该命中，门控照样安全")
    print("   命中率低且提问具体却没命中 → 先修检索（调门槛/权重），别急着开门控")


def print_proactive(data_dir: Path) -> None:
    """⑧ 主动消息诊断（2026-09-13 新增）。

    为什么单独一段：实测用户睡觉期间被连发 20+ 条、且其中一条编造了
    「我帮你关了弹窗」（该能力不存在），而当时**成功投递完全无日志**，
    查不出是哪条 check 发的。这一段回答「谁在发、被什么拦住、有没有编造」。
    """
    print()
    print("=" * 66)
    print("⑧ 主动消息诊断")
    fp = data_dir / "proactive_trace.jsonl"
    rows = _load_trace(fp)
    if not rows:
        print("   还没有记录（新加的诊断，要等下一次主动消息触发）")
        print("   文件: %s" % fp)
        return

    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from backend import proactive_trace as _pt
        s = _pt.summarise(rows)
    except Exception as e:
        print("   汇总失败: %s（原始记录 %d 条）" % (e, len(rows)))
        return

    print("   发出 %d 条 / 拦下 %d 条 / 睡眠事件 %d 次 / 疑似编造 %d 条"
          % (s["delivered"], s["skipped"], s["sleep_events"], s["fabrications"]))

    if s["by_who"]:
        print()
        print("   谁发的（成功投递以前查不到，现在能定位到具体 check）:")
        for who, n in s["by_who"][:10]:
            print("      %-30s %d 条" % (who, n))
    if s["by_type"]:
        print()
        print("   什么类型:")
        for t, n in s["by_type"][:10]:
            print("      %-30s %d 条" % (t, n))
    if s["by_reason"]:
        print()
        print("   被什么拦下:")
        for r, n in s["by_reason"][:8]:
            print("      %-30s %d 条（同原因 5 分钟内只记一条）" % (r, n))

    if s["sleep_line"]:
        print()
        print("   睡眠状态时间线（还原「她以为用户在干嘛」）:")
        for r in s["sleep_line"]:
            print("      %s  %-9s %s  %s" % (r.get("ts"), r.get("action"),
                                            r.get("hours"), r.get("note") or ""))

    if s["fabrications"]:
        print()
        print("   ⚠ 疑似编造「做过某事」（该能力可能不存在，目前只记录不拦截）:")
        for r in s["fab_recent"]:
            print("      %s  %s" % (r.get("ts"), (r.get("preview") or "")[:70]))
            print("         命中: %s" % (r.get("hit") or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tail", type=int, default=8, help="显示最近几条记忆注入明细")
    ap.add_argument("--stats", action="store_true", help="只看记忆命中率汇总")
    ap.add_argument("--proactive", action="store_true", help="只看主动消息诊断")
    args = ap.parse_args()

    d = _data_dir()
    fp = d / "memory_trace.jsonl"
    print("数据目录 : %s" % d)
    print("诊断文件 : %s  %s" % (fp, "存在" if fp.exists() else "**不存在**"))
    rows = _load_trace(fp)
    print("记录条数 : %d" % len(rows))
    print()

    blocks = [r for r in rows if r.get("kind") == "memory_block"]
    ticks = [r for r in rows if r.get("kind") == "ext_tick"]
    sumblk = [r for r in rows if r.get("kind") == "summary_block"]

    # --stats：只看命中率汇总（跑一周后用这个，输出短、直接给结论）
    if args.stats:
        print_stats(hitrate_stats(blocks))
        return
    # --proactive：只看主动消息诊断
    if args.proactive:
        print_proactive(d)
        return

    # ── 1. 外置记忆库 tick（归档/日周月总结/索引 有没有在推进）
    print("=" * 66)
    print("① 外置记忆库 tick（每 5 分钟一次）")
    if not ticks:
        print("   ✗ 没有任何 tick 记录 → 调度没跑到，或本次启动还没满 5 分钟")
    else:
        last = ticks[-1]
        idx = last.get("index") or {}
        snap = idx.get("snapshot") or {}
        print("   记录数 %d，最近一次: 归档 %s 条 / 日总结 %s 天 / 周 %s / 月 %s"
              % (len(ticks), last.get("archived"), last.get("daily"), last.get("weekly"),
                 last.get("monthly") or "无"))
        pend = snap.get("pending_days", idx.get("pending"))
        print("   %s 原文索引: %s 片段 / %s 天，待补 %s 天"
              % (_ok((pend or 0) == 0), snap.get("chunks", "?"), snap.get("days", "?"), pend))
        print("      本轮构建: built=%s skipped=%s pending=%s%s"
              % (idx.get("built"), idx.get("skipped"), idx.get("pending"),
                 ("  原因: " + str(idx.get("reason"))) if idx.get("reason") else ""))
        if snap.get("model"):
            print("      索引模型: %s（%s 维）" % (snap.get("model"), snap.get("dim")))

    # ── 2. 索引健康度
    print()
    print("② 原文索引健康度")
    arch = _archive_dir(d)
    snap = _live_index_stats(arch)
    if snap and not snap.get("_error"):
        print("   角色: %s   全部: %s" % (snap.get("_char"), snap.get("_chars_all")))
        print("   索引位置: %s" % snap.get("db"))
        print("   %s 本地 embedding 模型已缓存（未加载，只查文件）" % _ok(_model_cached()))
        print("   %s 索引片段数: %s（0 = 索引库是空的，检索永远不命中）"
              % (_ok((snap.get("chunks") or 0) > 0), snap.get("chunks")))
        print("   %s 已索引天数: %s / 原文 %s 天"
              % (_ok((snap.get("days") or 0) > 0), snap.get("days"), snap.get("raw_days")))
        pend = snap.get("pending_days") or 0
        print("   %s 待补天数: %s%s"
              % (_ok(pend == 0), pend,
                 "（>0 说明还在建，越老的事越可能还没进索引）" if pend else ""))
        print("   模型: %s (%s 维)" % (snap.get("model"), snap.get("dim")))
    elif blocks:
        ix = blocks[-1].get("index") or {}
        err = (snap or {}).get("_error") or "索引库不存在"
        print("   索引库直读失败: %s" % err)
        print("   %s embedding 可用: %s" % (_ok(ix.get("ready")), ix.get("ready")))
        print("   %s 索引片段数: %s" % (_ok((ix.get("chunks") or 0) > 0), ix.get("chunks")))
    else:
        err = (snap or {}).get("_error") or "索引库不存在"
        print("   ✗ 取不到索引状态: %s" % err)

    # ── 3. 检索是否生效（最关键）
    print()
    print("③ 相关检索是否生效（最关键）")
    # ★ 区分真人聊天 / 后台生成：后者本来就没有用户文本，检索必然为空，
    #   混在一起看会误判成"检索全废"（实测一开始就是 8 条空记录）。
    real = [b for b in blocks if str(b.get("query") or "").strip() and not b.get("internal")]
    idle = [b for b in blocks if not (str(b.get("query") or "").strip() and not b.get("internal"))]
    print("   注入次数 %d：真人聊天 %d 次 / 后台生成或空文本 %d 次"
          % (len(blocks), len(real), len(idle)))
    if not blocks:
        print("   ✗ 没有注入记录")
    elif not real:
        print("   ⚠ 还没有真人聊天记录（后台生成不算）→ 需要你实际聊一句才能判断检索")
    else:
        hit = [b for b in real if (b.get("n_hits") or 0) > 0]
        print("   真人聊天里命中 %d/%d 次（%.0f%%）"
              % (len(hit), len(real), 100.0 * len(hit) / max(1, len(real))))
        if not hit:
            print("   ✗ 一次都没命中 → 检索没起作用，仍在走时间分层兜底")
            print("     先看 ②：索引是空的？还是 embedding 没起来？")
        else:
            ok_inject = [b for b in hit if (b.get("relevant_chars") or 0) > 0]
            print("   %s 命中后确实注入了原文片段: %d/%d 次"
                  % (_ok(len(ok_inject) == len(hit)), len(ok_inject), len(hit)))
            mr = [b for b in hit if (b.get("injected_chars") or 0) < 300]
            if mr:
                print("   ⚠ 有 %d 次「命中但注入很短」（<300 字）→ 可能被 prompt 裁剪吃掉了"
                      % len(mr))

    # ── 4. 摘要块（是否在追加、迁移块在不在）
    print()
    print("④ 摘要链（只追加不重写）")
    if not sumblk:
        print("   · 还没追加过摘要块（每 %s 条消息才追加一次，属正常早期状态）" % 80)
    for s in sumblk[-3:]:
        print("   追加块 %s..%s（%d 字），累计 %d 块"
              % (s.get("from_id"), s.get("to_id"), s.get("block_chars"), s.get("total_blocks")))
    if blocks:
        bs = blocks[-1].get("blocks") or {}
        ranges = bs.get("recent_ranges") or []
        print("   库里共 %s 块；实际注入最近 %s 块，覆盖区间 %s"
              % (bs.get("n_blocks"), bs.get("injected_limit", "?"), ranges))
        if bs.get("n_blocks"):
            has_legacy = any(str(r).startswith("0..") for r in ranges)
            print("   %s 旧单行摘要迁移块(0..N)在位: %s%s"
                  % (_ok(has_legacy), has_legacy,
                     "" if has_legacy else "  ← 不在最近3块里，可能只是被新块挤出去了"))

    # ── 5. 最近几次注入明细（人话版）
    print()
    print("=" * 66)
    print("⑤ 最近 %d 次记忆注入明细（只看真人聊天）" % min(args.tail, len(real or blocks)))
    for b in (real or blocks)[-args.tail:]:
        print("   Q: %s" % (b.get("query") or "")[:56])
        print("      命中 %s 段 | 注入 %s 字（相关 %s + 兜底 %s）"
              % (b.get("n_hits"), b.get("injected_chars"),
                 b.get("relevant_chars"), b.get("fallback_chars")))
        for h in (b.get("hits") or [])[:3]:
            print("        %.3f (vec=%.3f lex=%.3f) %s %s | %s"
                  % (h.get("score") or 0, h.get("vec") or 0, h.get("lex") or 0,
                     h.get("day"), h.get("t") or "", (h.get("preview") or "")[:48]))

    # ── 6. 日志里的错误
    print()
    print("=" * 66)
    print("⑥ backend_dev.log 里与记忆相关的报错/提示")
    log = d / "backend_dev.log"
    if not log.exists():
        print("   （找不到 %s）" % log)
    else:
        keys = ("[ExtMemory]", "[Summary]", "[KnowledgeGraph]", "[CompanionController]")
        hits = []
        with log.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if any(k in line for k in keys):
                    hits.append(line.rstrip())
        if not hits:
            print("   没有相关日志行（可能本次启动还没触发，或日志在别处）")
        for ln in hits[-12:]:
            print("   " + ln[:150])

    # ── 7. 命中率统计（跑一周后直接看这段决定要不要开门控）
    print()
    print_stats(hitrate_stats(blocks))

    # ── 8. 主动消息诊断
    print_proactive(d)


if __name__ == "__main__":
    main()
