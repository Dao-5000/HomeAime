# -*- coding: utf-8 -*-
"""外置记忆库统一服务：定时触发（幂等）+ 开机补齐 + 检索注入。

设计要点：
- 全程幂等：state.json 记录「已归档到哪条 id」「已生成哪些日/周/月总结」，
  电脑反复开关机，每次 tick 都只补缺失的部分，不漏、不重复、不坏既有功能。
- 递归链：原文 → 日总结 → 周总结 → 月总结，逐层由 LLM 压缩。
"""
from datetime import datetime, timedelta

import asyncio

from . import paths, archive, summarizer, retriever

# 单次 tick 最多补几份日总结（补历史时防止一次打出几十个 LLM 请求）
MAX_DAILY_BACKFILL_PER_TICK = 5

# 单次 tick 最多补几份周总结（同上）
MAX_WEEKLY_BACKFILL_PER_TICK = 3

# 月总结最低门槛：本月至少这么多份周总结才生成。
# ★ 2026-09-13：只有 1~2 份周总结时，月总结的信息量是负的 —— 只是把周总结
#   再压一遍、白丢细节，却照样每轮注入 4000 字预算里占一块。实测线上
#   2026-08 的月总结输入就是「唯一的 W35 一份周总结」，纯重复。
MIN_WEEKS_FOR_MONTHLY = 3


async def tick(session_id: str, character_id: str) -> None:
    """scheduler 每轮调用：归档新消息 + 补齐缺失的日/周/月总结 + 维护原文索引。

    ★ 2026-09-13：全流程仍然"失败不影响主流程"，但**成功路径不再静默** ——
      实测改造前 backend_dev.log 里 `[ExtMemory]` 一条都没有，出了问题
      完全无法判断是"没归档""没总结"还是"根本没跑到"。现在每个环节都落
      结构化诊断到 DATA_DIR/memory_trace.jsonl，够定位到具体哪一步卡住。
    """
    archived = 0
    daily_built = []
    weekly_built = []
    monthly_built = ""
    index_result = {}

    try:
        # 1. 原文归档（把 chat_history 新消息写入文件）
        archived = archive.archive_new_messages(session_id, character_id) or 0
    except Exception as _e:
        print(f"[ExtMemory] 原文归档失败: {_e}", flush=True)

    try:
        # 2. 日总结：补齐「原文里存在、但还没生成日总结」的每一天
        #   ★ 2026-09-13 修复：原来只补「最近 3 天」（for i in range(1,4)），
        #     一旦 App 连续 4 天以上没开机，超窗的那几天就永远补不上了 ——
        #     原文还在（原文是 append-only、不会丢），但它永远进不了
        #     日→周→月 这条压缩链，等于长期记忆里凭空多了个洞。
        #     实测：线上 助手 原文从 8-27 起就有，日总结却只有 9-05 之后，
        #     而 9-05 之后的周总结、月总结正是建立在这些日总结之上。
        #   ★ 现在改为扫描原文目录里所有日期，缺哪天补哪天。
        #     幂等靠 daily_summarized 去重；再加每日上限，避免补历史时
        #     一次 tick 打出一堆 LLM 请求（剩下的留给下一个 tick）。
        done_daily = set(paths.load_state(character_id).get("daily_summarized") or [])
        raw_dir = paths.raw_dir(character_id)
        missing = []
        if raw_dir.exists():
            for fp in sorted(raw_dir.glob("*.md")):
                d = fp.stem
                if d in done_daily:
                    continue
                # 当天还在追加（原文是 append-only 的），留给明天再总结，
                # 否则会把只写了一半的今天压成日总结，后半天的内容就丢了。
                if d >= datetime.now().strftime("%Y-%m-%d"):
                    continue
                missing.append(d)
        # 从最早缺的开始补（顺序推进，周/月总结的递归链才有连续的地基）
        for d in missing[:MAX_DAILY_BACKFILL_PER_TICK]:
            try:
                if await summarizer.summarize_day(session_id, character_id, d):
                    paths.mark_done(character_id, "daily_summarized", d)
                    daily_built.append(d)
                else:
                    # 静默失败是这次排查最大的障碍：日总结生成不出来时
                    # 既没有异常也没有日志，只表现为"她记不住"。
                    print(f"[ExtMemory] 日总结未生成: {d}（LLM 返回空/无 key，下一轮重试）",
                          flush=True)
            except Exception as _de:
                print(f"[ExtMemory] 日总结异常: {d}: {_de}", flush=True)
        if missing:
            print("[ExtMemory] 日总结缺口 %d 天，本轮补 %d 天 %s"
                  % (len(missing), len(daily_built), daily_built), flush=True)
    except Exception as _e:
        print(f"[ExtMemory] 日总结环节失败: {_e}", flush=True)

    # 3. 周总结：补齐所有「有日总结但没有周总结」的周
    try:
        weekly_built = await _ensure_weekly(character_id)
        if weekly_built:
            print(f"[ExtMemory] 周总结补建 {weekly_built}", flush=True)
    except Exception as _e:
        print(f"[ExtMemory] 周总结环节失败: {_e}", flush=True)

    # 4. 月总结：上个月
    try:
        monthly_built = await _ensure_monthly(character_id) or ""
        if monthly_built:
            print(f"[ExtMemory] 月总结生成 {monthly_built}", flush=True)
    except Exception as _e:
        print(f"[ExtMemory] 月总结环节失败: {_e}", flush=True)

    # 5. 原文片段索引：增量补「新增/内容变了」的天（相关工作丢线程，不卡事件循环）
    #    ★ 2026-09-13 新增：这是"相关优先检索"的地基。没有它，外置记忆库
    #      只能按时间分层注入，和当前话题无关。索引落后不影响正确性，
    #      只是检索少几天 —— 所以这里静默、限量、可分多轮跑完。
    try:
        index_result = await asyncio.get_running_loop().run_in_executor(
            None, _build_index_quiet, character_id
        ) or {}
    except Exception as _e:
        print(f"[ExtMemory] 索引构建环节失败: {_e}", flush=True)

    # 结构化诊断（供事后统计；写失败不影响任何流程）
    # ★ index 字段同时带上「本轮构建报告」和「索引现状快照」——
    #   只记前者的后果实测过：报告里 chunks/days 全是 ?，看不出索引到底建到哪了。
    try:
        from . import trace as _trace
        try:
            from . import index as _idx
            _snap = _idx.stats(character_id)
        except Exception:
            _snap = {}
        _trace.record_ext_memory_tick(
            character_id, archived, daily_built, weekly_built,
            monthly_built,
            {**(index_result or {}), "snapshot": _snap}
        )
    except Exception:
        pass


def _build_index_quiet(character_id: str) -> dict:
    """线程里跑索引构建，任何异常都不外抛（索引只是可重建的加速结构）。返回诊断结果。"""
    try:
        from . import index as _idx
        r = _idx.build_index(character_id)
        # 只在本轮真干活 / 还有积压时打一条，避免每 5 分钟刷屏
        if r.get("built") or (r.get("pending") or 0) > 0:
            print(
                "[ExtMemory] 原文索引: 本轮建 %d 天 %s，待补 %d 天%s"
                % (len(r.get("built") or []), r.get("built") or "",
                   r.get("pending") or 0,
                   ("（" + r["reason"] + "）") if r.get("reason") else ""),
                flush=True,
            )
        return r
    except Exception as _e:
        print(f"[ExtMemory] 原文索引构建失败(静默): {_e}", flush=True)
        return {"reason": str(_e)}


async def _ensure_weekly(character_id: str) -> list:
    """补齐所有「有日总结但还没有周总结」的周（本周除外）。返回本轮补建的周号。

    ★ 2026-09-13：原来只算「上一自然周」（today - weekday - 7）这一周，
      停机超过一周时中间那些周永远不会被总结，而周总结是月总结唯一的输入，
      漏一周 = 那段时间在长期记忆里彻底消失。现在按 summarizer.weeks_of_days()
      扫全部缺口，逐周补（幂等靠 weekly_summarized；上限防一次打太多请求）。
    """
    built = []
    for wk in summarizer.weeks_of_days(character_id)[:MAX_WEEKLY_BACKFILL_PER_TICK]:
        start = summarizer.week_start(wk)
        if start is None:
            continue
        try:
            if await summarizer.summarize_week(character_id, start):
                paths.mark_done(character_id, "weekly_summarized", wk)
                built.append(wk)
            else:
                print(f"[ExtMemory] 周总结未生成: {wk}（LLM 返回空/无 key，下一轮重试）",
                      flush=True)
        except Exception as _e:
            print(f"[ExtMemory] 周总结异常: {wk}: {_e}", flush=True)
    return built


async def _ensure_monthly(character_id: str) -> str:
    """生成上个月的月总结。返回生成的月份键（没生成返回空串）。"""
    first_this_month = datetime.now().replace(day=1)
    last_month = first_this_month - timedelta(days=1)
    month_key = last_month.strftime("%Y-%m")
    done = set(paths.load_state(character_id).get("monthly_summarized") or [])
    if month_key in done:
        return ""
    # ★ 门槛：本月周总结不足 MIN_WEEKS_FOR_MONTHLY 份就不生成。
    #   否则「1 份周总结 → 1 份月总结」只是把同样的内容再压一遍，白丢细节
    #   （线上 2026-08 就是这个情况）。注意这里**不 mark_done**：
    #   等这个月补够周总结了，下次 tick 还会再来试一次。
    n_weeks = summarizer.count_week_files(character_id, month_key)
    if n_weeks < MIN_WEEKS_FOR_MONTHLY:
        print("[ExtMemory] 月总结跳过: %s 只有 %d 份周总结（需 >= %d），等补够再来"
              % (month_key, n_weeks, MIN_WEEKS_FOR_MONTHLY), flush=True)
        return ""
    if await summarizer.summarize_month(character_id, month_key):
        paths.mark_done(character_id, "monthly_summarized", month_key)
        return month_key
    print(f"[ExtMemory] 月总结未生成: {month_key}（LLM 返回空/无 key，下一轮重试）",
          flush=True)
    return ""


def build_memory_block(character_id: str, user_text: str = "", max_chars: int = 4000,
                       session_id: str = "") -> str:
    """给 enrich_messages 用的记忆块：相关原文检索优先，时间分层兜底。

    session_id 只用于诊断（把摘要块状态一起记录，便于事后判断"忘了"是
    记忆没检索到、还是摘要链断了）。
    """
    try:
        return retriever.build_memory_context(character_id, user_text, max_chars,
                                              session_id=session_id)
    except Exception as e:
        # 以前这里静默，导致"外置记忆块是空的"和"这行代码根本没跑"无法区分
        try:
            print(f"[ExtMemory] 记忆块构建失败: {e}", flush=True)
        except Exception:
            pass
        return ""


def list_archive(character_id: str) -> dict:
    """列出记忆库结构，供前端浏览页展示。"""
    try:
        return retriever.list_archive(character_id)
    except Exception:
        return {"原文": [], "日总结": [], "周总结": [], "月总结": []}
