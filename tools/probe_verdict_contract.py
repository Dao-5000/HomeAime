# -*- coding: utf-8 -*-
"""实证：时机判断到底是不是模型在做？契约缺档是不是真因？

做法：
  A. 用**当前**的 _SYSTEM 契约，拿事故原话调真模型，看它填什么；
  B. 把契约里的 trigger_day 合法值显式补上「now」，同样的输入再问一次；
  C. 对比两次输出 —— 若 B 变对，说明缺的是"契约里没有 now 这一档"，
     而不是"模型不会判断"。

真调 deepseek-chat，读真机 key（只读 config.json）。
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(r"D:\AI聊天项目桌面端\AI聊天项目")
sys.path.insert(0, str(PROJECT_ROOT))
_REAL = Path(os.environ["APPDATA"]) / "HomeAime" / "data"
os.environ["AI_COMPANION_DATA_DIR"] = str(_REAL)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend import config  # noqa: E402
import backend.ai_promise as ap  # noqa: E402
from backend.deepseek_api import chat_once  # noqa: E402

# 事故原话（用户当时说的是这一句，骨子回的"1439分钟后…"）
CASE = "都快三点了……我不提睡的事，你别自己往那上面想"

# 为了复现"当时是凌晨 2:50"的处境，把当前时间如实体现在 prompt 里
NOW = "2026-09-17 02:50"


async def ask(system_prompt: str, label: str):
    model = config.memory_extract_model("骨子")
    key = config.api_key_for_model(model)
    print(f"\n{'='*76}\n{label}\n{'='*76}")
    print(f"模型={model}  temp=0.1  当前时间={NOW}")
    print(f"用户原话={CASE!r}")
    raw = await chat_once(model,
                          [{"role": "system", "content": system_prompt},
                           {"role": "user",
                            "content": (f"（当前时间：{NOW}）\n用户说：{CASE}\n"
                                        f"（本轮 AI 还没回复；用户在委托的话照样按委托判）")}],
                          key, temperature=0.1, max_tokens=300)
    print("模型原始输出:", str(raw)[:400])
    return str(raw)


async def main():
    print("★ 事实核对：时机判断是模型在做 —— 判定走的是在线 LLM，不是正则")
    print(f"  extract_reminder_verdict 用的 system prompt 长度: {len(ap._SYSTEM)} 字")

    # A：当前契约
    a = await ask(ap._SYSTEM, "A. 当前契约（trigger_day 合法值没有 now）")
    try:
        da = json.loads(a[a.find("{"):a.rfind("}") + 1])
    except Exception:
        da = {}
    print(f"  → 解析: trigger_time={da.get('trigger_time')!r} "
          f"trigger_day={da.get('trigger_day')!r} rel={da.get('relative_minutes')!r}")

    # B：契约里显式给出 now 这一档
    patched = ap._SYSTEM.replace(
        '"trigger_day": "today / tomorrow / YYYY-MM-DD / relative（仅 time 类型填）"',
        '"trigger_day": "now / today / tomorrow / YYYY-MM-DD / relative（仅 time 类型填）"'
    )
    if patched == ap._SYSTEM:
        # 兜底：直接在 prompt 末尾追加一档说明
        patched = ap._SYSTEM + """
★ 补充：trigger_day 还允许填 "now" —— 表示「就是此刻/马上」（用户说「该睡觉了」
  「该走了」「要迟到了」这类没有具体时刻的当下意图时用这个，别填 tomorrow）。
"""
    b = await ask(patched, "B. 契约补上 now 这一档")
    try:
        db_ = json.loads(b[b.find("{"):b.rfind("}") + 1])
    except Exception:
        db_ = {}
    print(f"  → 解析: trigger_time={db_.get('trigger_time')!r} "
          f"trigger_day={db_.get('trigger_day')!r} rel={db_.get('relative_minutes')!r}")

    print(f"\n{'='*76}\n结论\n{'='*76}")
    print(f"A（原契约）trigger_day={da.get('trigger_day')!r}")
    print(f"B（补 now） trigger_day={db_.get('trigger_day')!r}")


asyncio.run(main())
