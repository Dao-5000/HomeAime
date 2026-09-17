# -*- coding: utf-8 -*-
"""摘要块 ↔ 事实关联登记工具。

为什么需要它：
  摘要块是"只追加、不重写"的不可变记录 —— 有意的设计（避免递归压缩把专有名词
  越洗越丢）。代价实测暴露过：id=17 那块写着「AI 住在用户的电脑 4060 里」，
  而本地部署从未成功；库里错误记忆清理后，摘要块不会跟着更新，错误叙述
  继续被注入。

  这个工具建立"块依赖哪条事实"的关联，配合 db.refresh_summary_block_staleness()：
  关联事实一旦失效，块被标记 facts_stale=1，注入时降级为历史叙述而不是被复述。

为什么关联要人工确认、不做自动抽取：
  实测 LLM 批量判定**不可复现** —— 同输入同 temperature 两次运行给出不同结论
  （sweep 时 dry-run 判 13 条、apply 重跑判 20 条）。建立"失效即降级"的联动后，
  判错的后果是**把正常摘要块标注成过时**，所以关联只登记已人工确认的。

流程（两步，与 sweep 工具同构）：
    1) 候选：列出某块与各记忆的词面重叠，人工挑出真正相关的
    2) 登记：--link <块id> --facts 12,34,56
   3) 校验：--check  重算 stale 状态并打印

用法：
    python backend/tools/summary_link_facts.py --block 17
    python backend/tools/summary_link_facts.py --link 17 --facts 3698,3831,3841
    python backend/tools/summary_link_facts.py --check
"""
import argparse
import os
import re
import sys
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


def _grams(s: str) -> set:
    s = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", "", str(s or ""))
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def _overlap(a: str, b: str) -> float:
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(min(len(ga), len(gb)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--char", default="骨子")
    ap.add_argument("--block", type=int, default=0, help="看某个块的候选事实")
    ap.add_argument("--link", type=int, default=0, help="要登记的块 id")
    ap.add_argument("--facts", default="", help="逗号分隔的事实 id")
    ap.add_argument("--check", action="store_true", help="重算并打印 stale 状态")
    ap.add_argument("--min-overlap", type=float, default=0.10)
    args = ap.parse_args()

    d = _data_dir()
    os.environ["AI_COMPANION_DATA_DIR"] = str(d)
    from backend import db

    if args.link and args.facts.strip():
        ids = [int(x) for x in args.facts.replace(" ", "").split(",") if x.isdigit()]
        ok = db.link_summary_block_facts(args.link, ids)
        print("登记块 #%s ← 事实 %s : %s" % (args.link, ids, "成功" if ok else "失败"))
        st = db.refresh_summary_block_staleness(None)
        print("重算 stale:", st)
        return

    if args.check:
        print("=== 已登记关联的块 ===")
        rows = db.q(
            "SELECT id, from_msg_id, to_msg_id, linked_facts, facts_stale, stale_note "
            "FROM conversation_summary_block WHERE linked_facts IS NOT NULL AND linked_facts != '' ORDER BY id",
            fetch=True) or []
        if not rows:
            print("   （还没有登记任何关联）")
        for r in rows:
            print("   块 #%-3s %s..%s linked=%s stale=%s" % (r[0], r[1], r[2], r[3], r[4]))
            if r[5]:
                print("        note: %s" % r[5])
        print()
        print("重算:", db.refresh_summary_block_staleness(None))
        print()
        print("=== 当前 stale 的块 ===")
        for s in db.get_stale_summary_blocks():
            print("   块 #%s %s..%s  %s" % (s["id"], s["from_msg_id"], s["to_msg_id"], s.get("stale_note")))
        return

    if args.block:
        blk = db.q("SELECT id, from_msg_id, to_msg_id, summary, linked_facts FROM conversation_summary_block WHERE id=?",
                   (args.block,), fetch=True)
        if not blk:
            print("块 #%s 不存在" % args.block)
            return
        b = blk[0]
        text = str(b[3] or "")
        print("=== 块 #%s（%s..%s，%d 字）===" % (b[0], b[1], b[2], len(text)))
        print("   已登记关联:", b[4] or "(无)")
        print()
        print("=== 候选事实（按与块文本的词面重叠降序，仅作参考）===")
        mems = db.q(
            "SELECT id, memory_content, is_valid FROM long_term_memory WHERE character_id=? "
            "ORDER BY id DESC LIMIT 800", (args.char,), fetch=True) or []
        scored = []
        for m in mems:
            ov = _overlap(text, str(m[1] or ""))
            if ov >= args.min_overlap:
                scored.append((ov, m[0], m[2], str(m[1] or "")))
        scored.sort(reverse=True)
        print("   候选 %d 条（重叠 >= %.2f）:" % (len(scored), args.min_overlap))
        for ov, mid, valid, content in scored[:30]:
            print("     %.3f  id=%-5s valid=%s  %s" % (ov, mid, valid, content[:56]))
        print()
        print("挑出真正相关的，然后：--link %s --facts id1,id2,..." % args.block)
        return

    print("需要 --block / --link+--facts / --check 之一。")


if __name__ == "__main__":
    main()
