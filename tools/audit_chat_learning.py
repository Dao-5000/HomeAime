# -*- coding: utf-8 -*-
"""建立聊天数据基线 + 确认当前运行版包含哪些修复（随时可重跑对比）。

输出三部分：
  ① 水位线：当前最后一条消息 id / 时间、各表计数 —— 之后用同一脚本对比增量
  ② 学习链路的"有没有东西可学"：style_feedback / learned_rules / 反思 / 纠正 / 五维
  ③ 运行版能力指纹：本机进程 + 打包 exe 时间戳 + trace 里是否已出现本次新增的埋点
"""
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DATA = Path(os.environ["APPDATA"]) / "HomeAime" / "data"
DB = DATA / "local_db.db"
SESS, CHAR = "s_93ceb989b56d9ea9a5835a03", "助手"

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()

print("=" * 76)
print(f"① 水位线   {datetime.now():%Y-%m-%d %H:%M:%S}")
print("=" * 76)
last = c.execute("""SELECT id, role, timestamp, substr(content,1,60) t
                    FROM chat_history WHERE session_id=? AND character_id=?
                    ORDER BY id DESC LIMIT 1""", (SESS, CHAR)).fetchone()
print("最后一条消息:", dict(last) if last else "无")
print("本会话消息总数:", c.execute(
    "SELECT COUNT(*) FROM chat_history WHERE session_id=? AND character_id=?",
    (SESS, CHAR)).fetchone()[0])
for tbl, where in (("long_term_memory", "is_valid=1 AND memory_status='active'"),
                   ("memory_reflection", "1=1"), ("open_loops", "status='pending'"),
                   ("tasks", "status='pending'"), ("user_corrections", "1=1"),
                   ("understanding_log", "1=1")):
    try:
        n = c.execute(f"SELECT COUNT(*) FROM {tbl} WHERE {where}").fetchone()[0]
        print(f"  {tbl:<22} {n}")
    except Exception as e:
        print(f"  {tbl:<22} 读不到: {e}")
print("  最近 3 条记忆写入:",
      [dict(r) for r in c.execute(
          "SELECT id, substr(memory_content,1,40) t, create_time FROM long_term_memory "
          "WHERE session_id=? ORDER BY id DESC LIMIT 3", (SESS,))])
con.close()

print()
print("=" * 76)
print("② 学习链路现状（有没有东西可学/学到）")
print("=" * 76)
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
c = con.cursor()
kv = {}
for k, v in c.execute("SELECT key, value FROM kv WHERE key LIKE 'style_feedback:%'"):
    kv[k] = v
print("style_feedback（学到的相处偏好）:", kv if kv else "（空 —— 说明还没触发风格学习）")
try:
    cols = [r[1] for r in c.execute("PRAGMA table_info(user_corrections)")]
    print("user_corrections 列:", cols)
    if cols:
        sel = ", ".join(cols[:6])
        print("最近 5 条纠正:", [dict(r) for r in c.execute(
            f"SELECT {sel} FROM user_corrections ORDER BY id DESC LIMIT 5")])
except Exception as e:
    print("user_corrections 读取失败:", e)
print("personality_state（五维）:")
for r in c.execute("""SELECT session_id, character_id, warmth_delta, dominance_delta,
                             humor_delta, initiative_delta, attachment_delta, updated_time
                      FROM personality_state
                      WHERE session_id=? ORDER BY character_id""", (SESS,)):
    print("   ", dict(r))
print("ai_feedback 计数:")
try:
    fb = DATA / "feedback.db"
    if fb.exists():
        fc = sqlite3.connect(f"file:{fb}?mode=ro", uri=True)
        for r in fc.execute("SELECT feedback_type, COUNT(*) FROM ai_feedback GROUP BY 1"):
            print("   ", tuple(r))
        fc.close()
    else:
        print("    （feedback.db 不存在）")
except Exception as e:
    print("    读不到:", e)
con.close()

print()
print("=" * 76)
print("③ 运行版能力指纹（哪些修复已经在你用的这版里）")
print("=" * 76)
exe = Path(r"%~dp0\release_new\win-unpacked\resources\backend\pc_backend.exe")
if exe.exists():
    print(f"打包 exe: {exe.stat().st_size} 字节  mtime={datetime.fromtimestamp(exe.stat().st_mtime):%Y-%m-%d %H:%M:%S}")
trace_dir = DATA / "trace"
kinds = {}
today = trace_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
if today.exists():
    import json
    for ln in today.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            o = json.loads(ln)
            kinds[o.get("kind")] = kinds.get(o.get("kind"), 0) + 1
        except Exception:
            pass
print("今天 trace 事件类型:", kinds if kinds else "（无）")
print("  reminder_verdict 已出现:", "reminder_verdict" in kinds,
      "（= 本次新增的判定埋点在跑）")
print("  task_failed 已出现:", "task_failed" in kinds)
