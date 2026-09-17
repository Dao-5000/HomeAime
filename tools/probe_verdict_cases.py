# -*- coding: utf-8 -*-
"""用真模型逐个试候选原话，找出「哪句话会被判成提醒委托」（复现 tasks#26 的成因）。

同时验证：新加的「当下意图」规则在**真的被判为委托时**能不能把时间拉回此刻。
"""
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(r"%~dp0")
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["AI_COMPANION_DATA_DIR"] = str(Path(os.environ["APPDATA"]) / "HomeAime" / "data")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.ai_promise as ap  # noqa: E402

# 事故前后用户真实说过/可能说过的话（含 AI 自己那句"我不提睡的事"）
CANDIDATES = [
    ("我知道", "知道就好。\n都快三点了……我不提睡的事，你别自己往那上面想。\n就陪你这么待着。"),
    ("实则是想提醒我睡觉哈哈哈", ""),
    ("都快三点了……我不提睡的事，你别自己往那上面想", ""),
    ("该睡觉了", ""),
    ("我该睡了", ""),
]

print("判定用的 system prompt 里是否已含『当下意图』规则:",
      "当下意图" in ap._SYSTEM)


async def main():
    from datetime import datetime
    for u, a in CANDIDATES:
        try:
            v = await ap.extract_reminder_verdict(u, ai_reply=a, character_id="骨子")
        except Exception as e:
            print(f"\n用户={u!r} → 调用异常 {type(e).__name__}: {e}")
            continue
        if v is None:
            print(f"\n用户={u!r} → 模型层不可用(None，会降级正则)")
            continue
        print(f"\n用户={u!r}  (AI回复={'有' if a else '无'})")
        print(f"   is_reminder={v.get('is_reminder')} source={v.get('promise_source')!r}")
        if v.get("is_reminder"):
            print(f"   content={v.get('content')!r}")
            print(f"   trigger_time={v.get('trigger_time')!r} "
                  f"trigger_day={v.get('trigger_day')!r} rel={v.get('relative_minutes')!r}")
            # 走真实的换算 + 建任务护栏，看最终会排到什么时候
            from backend import chat_logic
            trig = chat_logic._trigger_from_verdict(v, u)
            print(f"   换算后触发时刻: {trig}")
            if trig:
                gap = (trig - datetime.now()).total_seconds() / 3600
                print(f"   距现在 {gap:.2f} 小时")


asyncio.run(main())
