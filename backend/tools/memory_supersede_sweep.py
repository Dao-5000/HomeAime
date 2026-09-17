# -*- coding: utf-8 -*-
"""一次性矛盾记忆清理（supersede sweep）。

用途：把库里**已经被现实取代、但仍是 active** 的旧记忆批量作废。
典型场景（2026-09-13 实测）：用户明确说过"本地部署没成功、电脑带不动"，
但库里还留着 18 条"AI 已经部署到本地 / 住在 4060 上 / 今天刚完成部署" ——
都 active、confidence 1.0、还被检索注入，导致她张口就说"我住在你 4060 上"。

为什么需要这个工具：`memory_brain.smart_insert` 的冲突判定**只在写入新记忆时触发**，
对**存量**矛盾无能为力（旧记忆不会被重新检查）。所以存量数据需要这样一次性清理。

安全性设计：
  · 默认 **dry-run**，只打印将要作废的清单，不写库；`--apply` 才真正执行。
  · ★ **判据不可复现**（2026-09-13 实测教训）：LLM 批量判定是概率性的，
    同输入同 temperature 两次运行结果不同 —— 实测 dry-run 判 13 条、
    紧接着 apply 重跑判出 20 条，多出来的 7 条里包含「尚未部署成功」
    这类**正确的事实**，差点把对的删掉。
    所以现在 dry-run **会把判定结果落盘**，`--apply` 默认**读取该结果执行**，
    不再重新调用 LLM；除非显式 `--rerun` 要求重新判定。
  · 也支持 `--ids` 直接指定要作废的 id（用于人工复核后精确执行）。
  · 作废时**同时删向量**，避免留下"向量库搜得到、主库已失效"的孤儿。
  · 整体包在一个事务里，并自动备份主库（sqlite backup API，一致性快照）。

用法：
    # 1) 先 dry-run，判定结果会存到 <DATA_DIR>/sweep_plan.json
    python backend/tools/memory_supersede_sweep.py --char 助手 --topic 部署
    # 2) 复核清单后执行（复用上面的判定，不再重新问 LLM）
    python backend/tools/memory_supersede_sweep.py --char 助手 --topic 部署 --apply
    # 精确定制：直接给 id
    python backend/tools/memory_supersede_sweep.py --char 助手 --ids 3553,3561,3620 --apply
"""
import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _data_dir() -> Path:
    d = os.environ.get("AI_COMPANION_DATA_DIR", "").strip()
    if d:
        return Path(d)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return Path(appdata) / "HomeAime" / "data"
    return Path.home() / ".homeaime" / "data"


def _backup_db(db_path: Path) -> Path:
    """用 sqlite 的 backup API 做一致性快照（热库也安全）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = db_path.with_name(db_path.name + f".bak_sweep_{stamp}")
    src = sqlite3.connect(str(db_path))
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst


def summarize_invalidated(con, ids):
    rows = con.execute(
        "SELECT id, memory_status, is_valid, substr(memory_content,1,60) "
        "FROM long_term_memory WHERE id IN (%s)" % ",".join("?" * len(ids)),
        tuple(ids),
    ).fetchall()
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="助手", help="角色 id")
    ap.add_argument("--topic", default="", help="用于筛候选记忆的关键词（如 部署）")
    ap.add_argument("--ids", default="", help="直接指定要作废的记忆 id（逗号分隔），跳过 LLM 判定")
    ap.add_argument("--fact", default="", help="当前事实（判据）；留空用默认模板")
    ap.add_argument("--apply", action="store_true", help="真正执行作废（默认只 dry-run）")
    ap.add_argument("--rerun", action="store_true",
                    help="apply 时强制重新判定（默认复用 dry-run 落盘的结论，保证一致）")
    ap.add_argument("--limit", type=int, default=60, help="最多送检多少条候选")
    args = ap.parse_args()

    d = _data_dir()
    db_path = d / "local_db.db"
    os.environ["AI_COMPANION_DATA_DIR"] = str(d)
    plan_path = d / "sweep_plan.json"
    print("数据目录 : %s" % d)
    print("模式     : %s" % ("**真实执行**" if args.apply else "dry-run（只打印，不写库）"))
    print()

    from backend import db, memory_brain

    # ── 路径 A：--ids 直接指定（人工复核后精确执行，零 LLM 波动）
    if args.ids.strip():
        try:
            ids = [int(x) for x in args.ids.replace(" ", "").split(",") if x]
        except Exception:
            print("--ids 格式不对，应为逗号分隔的数字")
            return
        rows = db.q(
            "SELECT id, memory_content FROM long_term_memory "
            "WHERE is_valid=1 AND id IN (%s) ORDER BY id" % ",".join("?" * len(ids)),
            tuple(ids), fetch=True,
        ) or []
        sup = [{"id": r[0], "memory_content": r[1]} for r in rows]
        print("按 --ids 指定，现仍有效且将被作废: %d 条（指定 %d 个）" % (len(sup), len(ids)))
        for o in sup:
            print("   id=%-5s %s" % (o["id"], o["memory_content"][:58]))
        if not sup:
            print("没有可作废的，退出。")
            return
        if not args.apply:
            print()
            print("dry-run。加 --apply 执行。")
            return
        _do_apply(db, memory_brain, db_path, sup, args.char)
        return

    if not args.topic.strip():
        print("需要 --topic 或 --ids 之一。")
        return

    rows = db.q(
        "SELECT id, memory_content FROM long_term_memory "
        "WHERE is_valid=1 AND character_id=? AND memory_content LIKE ? ORDER BY id",
        (args.char, "%" + args.topic + "%"),
        fetch=True,
    ) or []
    olds = [{"id": r[0], "memory_content": r[1]} for r in rows]
    print("候选（含「%s」的有效记忆）: %d 条" % (args.topic, len(olds)))
    if not olds:
        print("没有候选，退出。")
        return
    olds = olds[: args.limit]

    fact = args.fact.strip() or (
        "用户当前的事实是：%s 这件事**没有成功/当前状态是未完成**，"
        "凡声称已经完成或当前就是成功状态的记忆都已被取代。" % args.topic
    )

    # ── 判定：apply 且未要求 rerun 时，复用 dry-run 落盘的结论
    sup = None
    if args.apply and not args.rerun and plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text("utf-8"))
            if plan.get("topic") == args.topic and plan.get("char") == args.char:
                sup = plan.get("supersede") or []
                print("复用 dry-run 的判定结果（%s，共 %d 条），**不重新问 LLM**"
                      % (plan.get("saved_at", "?"), len(sup)))
                print("   原因：LLM 批量判定不可复现，重跑会得到不同结果（实测 13 → 20 条）")
        except Exception as e:
            print("读取已有判定失败(%s)，改为重新判定" % e)

    if sup is None:
        print("判据     : %s" % fact[:80])
        print()
        print("送检 %d 条给 LLM 判定…" % len(olds))
        verdict = asyncio.run(
            memory_brain.detect_conflict_batch(fact, olds, character_id=args.char)
        )
        if verdict is None:
            print("判定失败（没 key / 解析失败），未做任何修改。")
            return
        sup = [olds[i] for i in verdict.get("supersede", [])]
        dup = [olds[i] for i in verdict.get("duplicate", [])]
        keep = [o for i, o in enumerate(olds)
                if i not in verdict.get("supersede", []) and i not in verdict.get("duplicate", [])]
        print("判定理由 : %s" % verdict.get("reason"))
        print()
        print("=" * 70)
        print("将被**作废**（supersede）: %d 条" % len(sup))
        for o in sup:
            print("   id=%-5s %s" % (o["id"], o["memory_content"][:58]))
        print()
        print("判定为重复（duplicate，保留现状不改）: %d 条" % len(dup))
        for o in dup:
            print("   id=%-5s %s" % (o["id"], o["memory_content"][:58]))
        print()
        print("无需处理（保留）: %d 条" % len(keep))
        for o in keep:
            print("   id=%-5s %s" % (o["id"], o["memory_content"][:58]))
        # ★ 落盘：apply 时复用同一份结论，避免"验证过的结果被重掷骰子"
        try:
            plan_path.write_text(json.dumps({
                "char": args.char, "topic": args.topic, "fact": fact,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "supersede": sup,
                "duplicate_ids": [o["id"] for o in dup],
                "keep_ids": [o["id"] for o in keep],
            }, ensure_ascii=False, indent=2), "utf-8")
            print()
            print("判定已存盘 -> %s（apply 时会复用它）" % plan_path.name)
        except Exception as e:
            print("判定存盘失败: %s" % e)

    if not sup:
        print()
        print("没有需要作废的，退出。")
        return

    if not args.apply:
        print()
        print("=" * 70)
        print("以上为 dry-run。确认无误后加 --apply 执行（会复用这份判定）。")
        return

    # 执行前校验：只作废**当前仍有效**的
    _do_apply(db, memory_brain, db_path, sup, args.char)


def _do_apply(db, memory_brain, db_path, sup, char):
    """真正执行作废：备份 → 事务内软删除 → 删向量 → 复核。"""
    check = []
    for o in sup:
        r = db.q("SELECT is_valid FROM long_term_memory WHERE id=?", (o["id"],), fetch=True)
        if r and int(r[0][0] or 0) == 1:
            check.append(o)
    skipped = len(sup) - len(check)
    sup = check
    if skipped:
        print("跳过已失效的 %d 条" % skipped)
    if not sup:
        print("没有需要作废的，退出。")
        return

    print()
    print("将作废 %d 条：" % len(sup))
    for o in sup:
        print("   id=%-5s %s" % (o["id"], o["memory_content"][:58]))

    bak = _backup_db(db_path)
    print()
    print("已备份主库 -> %s" % bak.name)

    ok_ids, fail_ids = [], []
    with db.transaction():
        for o in sup:
            try:
                db.invalidate_memory(o["id"], reason="superseded")
                ok_ids.append(o["id"])
            except Exception as e:
                fail_ids.append((o["id"], str(e)))
    for mid in ok_ids:
        try:
            memory_brain._drop_vector_for_memory(mid)
        except Exception as e:
            print("   删向量失败 id=%s: %s" % (mid, e))

    print()
    print("=" * 70)
    print("已作废 %d 条%s" % (len(ok_ids), ("，失败 %d 条" % len(fail_ids)) if fail_ids else ""))
    for mid, err in fail_ids:
        print("   失败 id=%s: %s" % (mid, err))

    con = sqlite3.connect("file:%s?mode=ro" % str(db_path).replace("\\", "/"), uri=True)
    print()
    print("复核（前 30 条）：")
    for r in summarize_invalidated(con, ok_ids)[:30]:
        print("   id=%-5s status=%-9s valid=%s  %s" % (r[0], r[1], r[2], r[3][:48]))
    con.close()
    print()
    print("要回滚：从备份 %s 恢复即可。" % bak.name)


if __name__ == "__main__":
    main()
