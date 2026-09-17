# -*- coding: utf-8 -*-
"""项目自动化体检报告（只读）——「生成项目体检报告」指令的落地脚本。

数据来源（全部由后端自动累积，本脚本只读、不写库、不改任何状态）：
  · local_db.db 的 chat_history        → 每轮用户/AI 原始全文 + 精确时间戳
  · DATA_DIR/trace/YYYY-MM-DD.jsonl    → 每回合埋点快照（turn/pipeline/memory/instr/err/proactive）
  · DATA_DIR/proactive_trace.jsonl     → 主动消息：谁发的(who)、什么类型(ptype)、被什么拦下(skip)
  · kv 表 token:caller:*               → 真实 token 记账（按调用点）

主动消息同样是测试对象：每条 deliver 会还原「因为什么发这句话」——
触发来源函数(who) + 主动消息类型(ptype) + 是否计划任务(scheduled)，并做编造行为检测。

用法：
    python tools/checkup_report.py                     # 今天的体检
    python tools/checkup_report.py --days 3            # 最近 3 天
    python tools/checkup_report.py --session s_xx      # 只看某会话
    python tools/checkup_report.py --character 小柔    # 只看某角色
    python tools/checkup_report.py --out 体检报告.md   # 同时导出 markdown 文件

判定规则（全自动、只基于埋点证据，禁止编造）：
  ✅正常   本轮无任何异常标志
  ⚠️可优化 功能可用但有瑕疵：延迟偏高 / 空回复边缘 / 近似复读 / 命中禁止表达 /
           后处理失败 / 记忆抽取报错 / 回复过短过长 / 格式可疑
  ❌异常不可用：回合异常(exception) / 空回复 / 明确指令未遵守 / 编造行为
  自动判不了的字段（如纯主观的人设观感）一律标注「未检出异常(自动)」或「未触发」，
  绝不脑补。
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
PROACTIVE_FP = os.path.join(DATA, "proactive_trace.jsonl")

# 延迟定性阈值（秒，回合总耗时 elapsed_s；可在改）
LAT_NORMAL_S = 20
LAT_WARN_S = 60
# 回复长度健康度（字数）
SHORT_CHARS = 6
LONG_CHARS = 600
# 固定功能模块清单（与采集字段一致，另加「主动消息」作为被测模块）
ALL_MODULES = ["对话问答", "多模态", "指令执行", "记忆读取", "人设保持",
               "代码生成", "文件解析", "工具调用", "上下文记忆", "格式输出", "其他", "主动消息"]


# ================================================================ 工具函数
def _est_tokens(text):
    """与 backend/token_tracker.py 相同的估算公式：中文1字≈1.5token，其他≈0.4。"""
    if not text:
        return 0
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cn
    return int(cn * 1.5 + other * 0.4)


def _norm(s):
    return re.sub(r"[\s\*\[\]（）()，。！？~…、,\.!\?'\"“”‘’]", "", str(s or ""))


def _short(s, n=60):
    s = str(s or "").replace("\n", " / ")
    return s[:n] + ("…" if len(s) > n else "")


def _hm(ts):
    return str(ts or "")[5:19]  # MM-DDTHH:MM:SS


def load_trace_rows(days, cutoff):
    """多加载 1 天的 trace 文件再按 cutoff 过滤——否则跨天回合（昨天 23:59 发问、
    今天 00:01 记埋点之外的昨日回合）会漏掉昨天文件里的埋点。"""
    rows = []
    for i in range(days + 1):
        d = (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        fp = os.path.join(TRACE_DIR, d + ".jsonl")
        if not os.path.exists(fp):
            continue
        for line in open(fp, encoding="utf-8", errors="ignore"):
            try:
                j = json.loads(line)
            except Exception:
                continue
            if str(j.get("ts", "")) >= cutoff:
                rows.append(j)
    return rows


def load_proactive_rows(cutoff):
    rows = []
    if not os.path.exists(PROACTIVE_FP):
        return rows
    for line in open(PROACTIVE_FP, encoding="utf-8", errors="ignore"):
        try:
            j = json.loads(line)
        except Exception:
            continue
        if str(j.get("ts", "")) >= cutoff:
            rows.append(j)
    return rows


def load_history():
    """全量读取 chat_history（先建完整回合再按时间过滤，避免切断回合边界）。"""
    if not os.path.exists(DB):
        return []
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT id, session_id, character_id, role, content, timestamp, extra FROM chat_history ORDER BY id")]
    con.close()
    return rows


# ================================================================ 回合重建
def build_turns(history, cutoff, session_f, char_f):
    """把 chat_history 重组为回合：1条用户消息 + 其后连续的 AI 消息。
    没有前置用户消息的 AI 消息 → 主动消息候选（再与 deliver 埋点比对确认）。
    """
    turns, proactives = [], []
    cur = None
    for r in history:
        if session_f and r["session_id"] != session_f:
            continue
        if char_f and r["character_id"] != char_f:
            continue
        if r["role"] == "user":
            if cur:
                turns.append(cur)
            cur = {"sid": r["session_id"], "char": r["character_id"],
                   "user_text": r["content"] or "", "u_ts": r["timestamp"],
                   "assistants": []}
        elif r["role"] == "assistant":
            extra = {}
            try:
                extra = json.loads(r["extra"] or "{}")
            except Exception:
                extra = {}
            item = {"ts": r["timestamp"], "text": r["content"] or "",
                    "audio": bool(extra.get("audio")), "extra": extra}
            if cur:
                cur["assistants"].append(item)
            else:
                proactives.append({"sid": r["session_id"], "char": r["character_id"],
                                   "ts": r["timestamp"], "text": r["content"] or ""})
    if cur:
        turns.append(cur)
    # 只保留时间窗内的回合（按用户消息时间）
    turns = [t for t in turns if str(t["u_ts"]) >= cutoff]
    for n, t in enumerate(turns, 1):
        t["no"] = n
    return turns, proactives


def match_trace(turn, trace_rows, kinds):
    """取本回合时间窗 [用户输入, 最后一条AI消息+15s] 内、同会话的指定类型埋点。"""
    end = str(turn["assistants"][-1]["ts"]) if turn["assistants"] else str(turn["u_ts"])
    try:
        end = (datetime.datetime.strptime(end, "%Y-%m-%dT%H:%M:%S")
               + datetime.timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    out = []
    for r in trace_rows:
        if r.get("kind") not in kinds:
            continue
        if r.get("sid") and r.get("sid") != turn["sid"]:
            continue
        if str(turn["u_ts"]) <= str(r.get("ts", "")) <= end:
            out.append(r)
    return out


# ================================================================ 单轮判定
def judge_turn(t, trows, prows, prev_replies):
    """按固定采集字段拼装一轮记录。返回 (form_lines, flags, modules)。
    flags: [(级别❌/⚠️, 模块, 描述)]；modules: 本轮触发的功能模块集合。
    """
    f = []
    flags = []
    modules = {"对话问答"}

    reply_full = "\n".join(a["text"] for a in t["assistants"]).strip()
    reply_chars = len(reply_full)
    a_first = t["assistants"][0]["ts"] if t["assistants"] else ""
    a_last = t["assistants"][-1]["ts"] if t["assistants"] else ""

    # ---- 埋点匹配
    t_turn = match_trace(t, trows, {"turn"})
    t_turn = t_turn[-1] if t_turn else None
    t_pipe = match_trace(t, trows, {"pipeline"})
    t_pipe = t_pipe[-1] if t_pipe else None
    t_mem = match_trace(t, trows, {"memory"})
    t_err = match_trace(t, trows, {"err"})
    # 没带 sid 的异常按时间窗兜底匹配（标注为时间匹配）
    for r in prows:
        if r.get("kind") != "err" or r.get("sid"):
            continue
        if str(t["u_ts"]) <= str(r.get("ts", "")) <= (a_last or str(t["u_ts"])):
            r2 = dict(r); r2["_time_only"] = True
            t_err.append(r2)

    turn_error = str((t_turn or {}).get("error") or "")
    hist = int((t_turn or {}).get("hist") or 0)
    blocks = int((t_turn or {}).get("blocks") or 0)
    sys_chars = int((t_turn or {}).get("sys_chars") or 0)
    elapsed = float((t_turn or {}).get("elapsed_s") or 0)
    model = str((t_turn or {}).get("model") or "")
    instr_kinds = (t_turn or {}).get("instr") or []
    has_audio = any(a["audio"] for a in t["assistants"]) or bool((t_turn or {}).get("audio"))
    has_sticker = bool((t_turn or {}).get("sticker")) or "[sticker" in reply_full

    # ---- 时间三件套
    f.append("用户输入时间: %s" % t["u_ts"])
    if t_turn and elapsed:
        try:
            start = (datetime.datetime.strptime(str(t_turn["ts"]), "%Y-%m-%dT%H:%M:%S")
                     - datetime.timedelta(seconds=elapsed)).strftime("%Y-%m-%dT%H:%M:%S")
            f.append("AI响应开始时间: %s（按回合快照写入时刻-耗时推算）" % start)
        except Exception:
            f.append("AI响应开始时间: 未记录")
    else:
        f.append("AI响应开始时间: 未记录（无回合埋点）")
    f.append("AI回复完成时间: %s" % (a_last or "未记录"))
    f.append("用户原始输入文本: %s" % (t["user_text"] or "(空)"))
    f.append("AI原始输出文本: %s" % (reply_full.replace("\n", " / ") if reply_full else "(空/未回复)"))

    # ---- 触发功能模块
    mods = ["对话问答"]
    if hist > 0:
        mods.append("上下文记忆")
    if t_mem:
        mods.append("记忆读取")
    if t_pipe:
        mods.append("人设保持")
    if has_audio or has_sticker:
        mods.append("多模态")
    if instr_kinds:
        mods.append("指令执行")
    if "```" in reply_full:
        mods += ["格式输出", "代码生成"]
    modules = set(mods)
    f.append("本次触发功能模块: %s" % "、".join(mods))

    # ---- 上下文携带
    if t_turn is None:
        ctx = "未记录（无回合埋点，仅聊天记录）"
    elif hist > 0:
        ctx = "正常携带（历史 %d 条 / prompt 块 %d 块 / system %d 字）" % (hist, blocks, sys_chars)
    else:
        ctx = "上下文为空（hist=0）——若非该会话首条则疑似丢失"
        flags.append(("⚠️", "上下文记忆", "hist=0 上下文为空"))
    f.append("上下文携带状态: %s" % ctx)

    # ---- 人设一致性（只报证据，不脑补观感）
    failed_pipe = (t_pipe or {}).get("failed") or []
    if "guard_hit" in failed_pipe:
        persona = "人设漂移（回复命中禁止表达，guard_hit）"
        flags.append(("⚠️", "人设保持", "命中禁止表达 guard_hit"))
    elif "personality" in failed_pipe:
        persona = "疑似漂移（人格更新后处理失败）"
        flags.append(("⚠️", "人设保持", "人格更新失败"))
    else:
        persona = "保持一致（自动未检出异常；guard/personality 埋点%s）" % (
            "在" if t_pipe else "缺失")
    f.append("AI人设一致性: %s" % persona)

    # ---- 格式稳定性
    if "```" in reply_full:
        if reply_full.count("```") % 2 == 1:
            fmt = "代码块异常（``` 未闭合）"
            flags.append(("⚠️", "格式输出", "代码块未闭合"))
        else:
            fmt = "格式正确（含代码块）"
    else:
        fmt = "未触发格式要求（纯文本对话）"
    f.append("输出格式稳定性: %s" % fmt)

    # ---- 指令理解
    if not instr_kinds:
        f.append("指令理解准确率: 未触发指令（未检出 别/不要/记住 类信号）")
    else:
        f.append("指令理解准确率: 检出指令信号 %s" % instr_kinds)
        violated = False
        for k in instr_kinds:
            if str(k).startswith("negate:") and ("语音" in str(k) or "声音" in str(k) or "发" in str(k) or "念" in str(k)):
                if has_audio:
                    flags.append(("❌", "指令执行", "用户明确说不要语音，本条回复仍带语音"))
                    violated = True
            if str(k) == "correct" and not reply_full:
                flags.append(("❌", "指令执行", "用户纠错后 AI 空回复"))
                violated = True
            if str(k) == "remember" and not t_mem:
                flags.append(("⚠️", "指令执行", "用户要求记住，但本回合窗口内无记忆抽取记录（可能未到抽取周期，需人工复核）"))
        if not violated:
            f.append("  ↳ 指令遵守核对: 未检出违规（基于埋点）" if not any(
                k == "remember" for k in instr_kinds) else "  ↳ 指令遵守核对: 记忆项待人工复核")

    # ---- 延迟
    if elapsed:
        if elapsed <= LAT_NORMAL_S:
            lat = "正常（%.1fs）" % elapsed
        elif elapsed <= LAT_WARN_S:
            lat = "轻微偏高（%.1fs，>%ds）" % (elapsed, LAT_NORMAL_S)
            flags.append(("⚠️", "对话问答", "响应延迟 %.1fs" % elapsed))
        else:
            lat = "严重超时（%.1fs，>%ds）" % (elapsed, LAT_WARN_S)
            flags.append(("⚠️", "对话问答", "响应严重超时 %.1fs" % elapsed))
    else:
        lat = "未记录（无回合埋点 elapsed_s）"
    f.append("响应延迟定性: %s" % lat)

    # ---- 完整性
    if turn_error == "empty_reply":
        comp = "提前结束输出（埋点 empty_reply，无内容产出）"
        flags.append(("❌", "对话问答", "空回复 empty_reply"))
    elif turn_error.startswith("exception"):
        comp = "中断（主链路异常）"
        flags.append(("❌", "对话问答", "回合异常 %s" % turn_error[:80]))
    elif reply_full:
        comp = "完整（%d 段 / %d 字）" % (max(1, len(t["assistants"])), reply_chars)
    elif t_turn is None and not reply_full:
        comp = "未记录（无埋点且无聊天记录）"
    else:
        comp = "异常（无埋点错误但无内容）"
        flags.append(("❌", "对话问答", "无内容产出且无错误埋点"))
    if 0 < reply_chars <= SHORT_CHARS and not turn_error:
        flags.append(("⚠️", "对话问答", "回复过短（%d 字）" % reply_chars))
    if reply_chars > LONG_CHARS:
        flags.append(("⚠️", "对话问答", "回复过长（%d 字）" % reply_chars))
    f.append("输出完整性: %s" % comp)

    # ---- 重复输出
    key = _norm(reply_full)[:40]
    dup = bool(key) and len(key) >= 6 and key in prev_replies
    f.append("是否出现重复输出: %s" % ("是（与近期回复近似重复）" if dup else "否"))
    if dup:
        flags.append(("⚠️", "对话问答", "近似复读"))

    # ---- 记忆召回
    if not t_mem:
        f.append("记忆召回结果: 无召回（本回合窗口内无记忆抽取埋点）")
    else:
        m = t_mem[-1]
        if m.get("error"):
            f.append("记忆召回结果: 召回错误（管线报错：%s）" % _short(m.get("error"), 50))
            flags.append(("⚠️", "记忆读取", "记忆管线报错 %s" % _short(m.get("error"), 40)))
        else:
            f.append("记忆召回结果: 召回准确（抽取 %s 条 / 入库 %s 条 / 判重 %s 条）"
                     % (m.get("extracted", 0), m.get("inserted", 0), m.get("duplicated", 0)))

    # ---- token 估算
    est_in = _est_tokens(t["user_text"]) + int(sys_chars * 1.5)
    est_out = _est_tokens(reply_full)
    f.append("预估输入token: ~%d（用户输入 %d + 系统提示按中文系数折算 %d%s）"
             % (est_in, _est_tokens(t["user_text"]), int(sys_chars * 1.5),
                "；历史部分无字数埋点未计入" if hist else ""))
    f.append("预估输出token: ~%d" % est_out)

    # ---- 报错
    errs_desc = []
    for e in t_err:
        tag = "时间匹配" if e.get("_time_only") else "会话匹配"
        errs_desc.append("[%s/%s] %s%s" % (e.get("module", "?"), tag,
                                           _short(e.get("msg"), 50),
                                           (" | " + _short(e.get("detail"), 40)) if e.get("detail") else ""))
    f.append("报错信息: %s" % ("；".join(errs_desc) if errs_desc else "无"))
    if t_err and not turn_error:
        flags.append(("⚠️", "其他", "窗口内存在异常埋点（见报错信息字段）"))

    # ---- 总判定
    f.append("功能判定: %s" % ("❌异常不可用" if any(x[0] == "❌" for x in flags)
                              else "⚠️可优化" if flags else "✅正常"))
    f.append("问题简要描述: %s" % ("；".join("%s%s" % (m, d) for _, m, d in flags) if flags else "（无）"))
    return f, flags, modules


# ================================================================ 主动消息
def judge_proactives(proactive_rows, history):
    """主动消息是正式测试对象：还原每条 deliver 的触发来源 + 原文 + 编造检测。"""
    delivers = [r for r in proactive_rows if r.get("kind") == "deliver"]
    skips = [r for r in proactive_rows if r.get("kind") == "skip"]
    fabs = [r for r in proactive_rows if r.get("kind") == "fabrication"]
    sleeps = [r for r in proactive_rows if r.get("kind") == "sleep"]

    # 用 chat_history 全文回填 deliver 的 preview（埋点只存 80 字）
    asst_rows = [h for h in history if h["role"] == "assistant"]

    def full_text(d):
        pv = _norm(d.get("preview", ""))[:30]
        if not pv:
            return ""
        best = ""
        for h in asst_rows:
            if h["session_id"] != d.get("session"):
                continue
            try:
                dt = abs((datetime.datetime.strptime(str(h["timestamp"]), "%Y-%m-%dT%H:%M:%S")
                          - datetime.datetime.strptime(str(d.get("ts")), "%Y-%m-%dT%H:%M:%S")).total_seconds())
            except Exception:
                continue
            if dt <= 180 and _norm(h["content"]).startswith(pv):
                best = h["content"] or ""
                break
        return best

    records = []
    for n, d in enumerate(delivers, 1):
        txt = full_text(d)
        flags = []
        near_fab = [x for x in fabs
                    if x.get("char") == d.get("char")
                    and abs(_tsec(x.get("ts")) - _tsec(d.get("ts"))) <= 60]
        if near_fab:
            flags.append("❌ 编造行为命中：%s（声称做了可能不存在的能力）" % _short(near_fab[-1].get("hit"), 40))
        records.append({
            "no": n, "ts": d.get("ts"), "sid": d.get("session"), "char": d.get("char"),
            "who": d.get("who") or "未知", "ptype": d.get("ptype") or "general",
            "scheduled": bool(d.get("scheduled")), "qq": bool(d.get("qq")),
            "chars": d.get("chars"), "text": txt or ("%s【埋点仅存80字预览】" % d.get("preview", "")),
            "flags": flags,
        })
    return records, skips, fabs, sleeps


def _tsec(ts):
    try:
        return datetime.datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%S").timestamp()
    except Exception:
        return 0.0


# ================================================================ 报告输出
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--session", default="")
    ap.add_argument("--character", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    cutoff = (datetime.datetime.now() - datetime.timedelta(days=a.days)).strftime("%Y-%m-%dT%H:%M:%S")
    trows = load_trace_rows(a.days, cutoff)
    prows_all = load_proactive_rows(cutoff)
    history = load_history()
    turns, _proactives = build_turns(history, cutoff, a.session, a.character)
    t_turns = [r for r in trows if r.get("kind") == "turn"]
    prows = [r for r in trows if r.get("kind") == "err"]  # 无 sid 异常的时间窗兜底
    # 主动消息数据取自 proactive_trace.jsonl（带 who/ptype，trace.jsonl 的 proactive 事件不含触发源）
    pro_records, skip_rows, fab_rows, sleep_rows = judge_proactives(prows_all, history)

    out_lines = []
    P = out_lines.append
    P("# 项目体检报告")
    P("")
    P("- 生成时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    P("- 统计区间: 最近 %d 天（自 %s）" % (a.days, cutoff))
    P("- 会话过滤: %s   角色过滤: %s" % (a.session or "全部", a.character or "全部"))
    P("- 数据源: %s（trace 埋点 + chat_history 全文 + proactive_trace + token 记账）" % DATA)
    P("- 判定说明: 全部判定基于埋点证据自动生成；主观观感类字段只报证据不脑补；未发生的功能标【未触发】")
    P("")

    # ---------------- 板块1 原始交互数据集
    P("=" * 78)
    P("板块1：原始交互数据集")
    P("=" * 78)
    P("本轮范围命中 %d 个对话回合（trace 回合埋点 %d 条）。" % (len(turns), len(t_turns)))
    all_flags = []
    all_modules = collections.Counter()
    prev_replies = set()
    for t in turns:
        form, flags, modules = judge_turn(t, trows, prows, prev_replies)
        if t["assistants"]:
            key = _norm(t["assistants"][-1]["text"])[:40]
            if len(key) >= 6:
                prev_replies.add(key)
        all_flags.extend((lvl, m, d, t["no"]) for lvl, m, d in flags)
        all_modules.update(modules)
        P("")
        P("---- 交互序号: %d  （会话 %s / 角色 %s）----" % (t["no"], t["sid"], t["char"]))
        for line in form:
            P(line)
    if not turns:
        P("")
        P("（区间内没有对话回合——未聊天或埋点未生效）")
    P("")

    # 主动消息记录（正式测试对象：还原触发来源）
    P("-" * 78)
    P("板块1-附：主动消息采集（测试目标：她为什么发这句话）")
    P("-" * 78)
    if pro_records:
        for r in pro_records:
            P("")
            P("---- 主动消息序号: P%d ----" % r["no"])
            P("发送时间: %s" % r["ts"])
            P("触发来源(who/因为什么发): %s   类型(ptype): %s   计划任务: %s   QQ同步: %s"
              % (r["who"], r["ptype"], "是" if r["scheduled"] else "否", "是" if r["qq"] else "否"))
            P("会话/角色: %s / %s" % (r["sid"], r["char"]))
            P("AI原始输出文本: %s" % _short(r["text"], 200))
            P("功能判定: %s" % ("❌异常不可用" if r["flags"] else "✅正常"))
            P("问题简要描述: %s" % ("；".join(r["flags"]) if r["flags"] else "（无）"))
        P("")
        P("拦截记录(skip) %d 条、睡眠标记(sleep) %d 条、编造检测(fabrication) %d 条。"
          % (len(skip_rows), len(sleep_rows), len(fab_rows)))
        by_who = collections.Counter(r.get("who") or "?" for r in skip_rows)
        if by_who:
            P("拦截原因 Top: %s" % dict(by_who.most_common(6)))
    else:
        P("主动消息: 【未触发】（区间内无 deliver 记录——主动消息功能未被测试到）")
    P("")

    # ---------------- 板块2 功能健康统计
    P("=" * 78)
    P("板块2：功能健康统计汇总")
    P("=" * 78)
    n_all = len(turns)
    n_bad = sum(1 for lvl, *_ in all_flags if lvl == "❌")
    turns_bad = {x[3] for x in all_flags if x[0] == "❌"}
    turns_warn = {x[3] for x in all_flags if x[0] == "⚠️"} - turns_bad
    n_ok = n_all - len(turns_bad) - len(turns_warn)
    P("- 总交互轮次: %d" % n_all)
    P("- 正常功能轮数: %d" % n_ok)
    P("- 可优化轮数: %d" % len(turns_warn))
    P("- 异常失效轮数: %d" % len(turns_bad))
    P("- 主动消息: 投递 %d 条 / 拦截 %d 条 / 编造命中 %d 条"
      % (len(pro_records), len(skip_rows), len(fab_rows)))
    P("")
    P("【各模块故障TOP清单（按出现次数排序）】")
    top = collections.Counter((m, d) for lvl, m, d, _ in all_flags if lvl in ("❌", "⚠️"))
    if top:
        for (m, d), c in top.most_common(12):
            P("  x%-3d [%s] %s" % (c, m, d))
    else:
        P("  （无故障记录）")
    P("")
    P("【各功能模块通过率 = 正常次数 ÷ 总触发次数】")
    # 模块触发次数按轮计（all_modules 在板块1循环里已按轮累计）
    mod_total = collections.Counter()
    mod_total.update(all_modules)
    for r in pro_records:
        mod_total["主动消息"] += 1
    mod_fail = collections.Counter(m for lvl, m, _, _ in all_flags if lvl in ("❌", "⚠️"))
    for m in ALL_MODULES:
        tot = mod_total.get(m, 0)
        if tot == 0:
            continue
        fail = mod_fail.get(m, 0)
        P("  %-8s 触发 %-4d 正常 %-4d 通过率 %5.1f%%"
          % (m, tot, tot - fail, (tot - fail) * 100.0 / tot))
    untested = [m for m in ALL_MODULES if mod_total.get(m, 0) == 0]
    if untested:
        P("  未触发模块: %s" % "、".join(untested))
    P("")
    P("【真实 token 记账（kv token:caller:*，按调用点）】")
    agg = collections.Counter()
    if os.path.exists(DB):
        con = sqlite3.connect(DB)
        con.row_factory = sqlite3.Row
        keys = ["token:caller:%s:" % (datetime.datetime.now() - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(a.days)]
        for r in con.execute("SELECT key, value FROM kv WHERE key LIKE 'token:caller:%'"):
            if not any(r["key"].startswith(p) for p in keys):
                continue
            caller = r["key"].split(":", 3)[3]
            try:
                i_, _, o_ = str(r["value"]).partition(",")
                agg[caller] += int(i_) + int(o_)
            except Exception:
                pass
        con.close()
    tot = sum(agg.values())
    for caller, v in agg.most_common(12):
        P("  %-44s %8d  (%4.1f%%)" % (caller, v, v * 100.0 / tot if tot else 0))
    if tot:
        P("  %-44s %8d" % ("合计", tot))
        if n_all:
            P("  每轮均值 ≈ %d token（%d 回合）" % (tot // n_all, n_all))
    else:
        P("  （无 token:caller 记录）")
    P("")

    # ---------------- 板块3 结论与建议
    P("=" * 78)
    P("板块3：项目体检结论与优化建议")
    P("=" * 78)
    P("")
    P("【核心风险项（❌异常项，优先修复）】")
    crit = [(lvl, m, d, no) for lvl, m, d, no in all_flags if lvl == "❌"]
    fab_recs = [r for r in pro_records if r["flags"]]
    if crit or fab_recs:
        seen = collections.Counter((m, d) for _, m, d, _ in crit)
        for (m, d), c in seen.most_common():
            P("  · [%s] %s —— 出现 %d 次，涉及回合: %s"
              % (m, d, c, "、".join(str(no) for l2, m2, d2, no in crit if m2 == m and d2 == d)[:60]))
        for r in fab_recs:
            P("  · [主动消息/人设崩坏风险] %s —— %s" % (r["flags"][0][:60], _short(r["text"], 50)))
    else:
        P("  （未检出 ❌ 级问题）")
    P("")
    P("【体验优化项（⚠️可优化项，迭代方向）】")
    warns = [(m, d) for lvl, m, d, _ in all_flags if lvl == "⚠️"]
    if warns:
        seen = collections.Counter(warns)
        for (m, d), c in seen.most_common(10):
            P("  · [%s] %s —— 出现 %d 次" % (m, d, c))
    else:
        P("  （未检出 ⚠️ 级问题）")
    P("")
    P("【暂未测试模块清单】")
    untested = [m for m in ALL_MODULES if mod_total.get(m, 0) == 0]
    if untested:
        for m in untested:
            P("  · %s【未触发】" % m)
        P("  提示：语音输入/文件解析/工具调用等需在对话中主动触发才能积累样本。")
    else:
        P("  （固定清单内模块均已被触发过）")
    P("")
    P("提示：原始埋点在 %s；单类记录可用 python tools/trace_report.py --raw turn 细看。" % TRACE_DIR)

    report = "\n".join(out_lines)
    print(report)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print("\n已导出: %s" % os.path.abspath(a.out))


main()
