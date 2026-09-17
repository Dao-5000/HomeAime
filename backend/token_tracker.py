# -*- coding: utf-8 -*-
"""
Token 用量统计：本地记录当日/当月消耗，支持阈值告警。
用 kv 表存储，key 格式：token:date:YYYY-MM-DD, token:month:YYYY-MM

★ 2026-09-14 扩展：新增「调用点维度」统计（token:caller:{日期}:{调用点}）。
  背景：原先 record_usage 只有调用方 main.api_chat 一处，主力入口
  /api/chat/stream 完全没记账，成本优化省了多少只能靠推算。
  现在 deepseek_api 在**每次真实 LLM 调用后**统一记账（并用上游返回的真实
  usage 替代估算），因此调用点维度可以直接回答"钱花在哪个模块"。
"""
import threading
from collections import defaultdict
from datetime import datetime

from . import db

# 默认告警阈值（token 数），用户可在配置里覆盖
DEFAULT_DAILY_WARN = 50000
DEFAULT_MONTHLY_WARN = 1000000

# 调用点累计：内存里累加，每 _CALLER_FLUSH_EVERY 次调用才落库一次
# （chat_once 一天可达数千次，逐次写 SQLite 会拖慢主链路）
_CALLER_FLUSH_EVERY = 25
_caller_buf = defaultdict(lambda: [0, 0])   # caller -> [input, output]
_caller_lock = threading.Lock()


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数：中文 1 字≈1.5 token，英文/数字 1 字符≈0.4 token"""
    if not text:
        return 0
    cn = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other = len(text) - cn
    return int(cn * 1.5 + other * 0.4)


def estimate_messages_tokens(messages: list) -> int:
    """估算输入消息的 token 数"""
    total = 0
    for m in messages:
        total += _estimate_tokens(m.get("content", "") or "")
        total += _estimate_tokens(m.get("role", "") or "")
    return total


def parse_usage(usage) -> tuple:
    """把上游返回的 usage 解析成 (input_tokens, output_tokens)。

    兼容各家字段差异：OpenAI/DeepSeek 用 prompt_tokens/completion_tokens，
    Anthropic 中转可能给 input_tokens/output_tokens，智谱带 *_tokens 同名字段。
    reasoning_tokens 已含在 completion_tokens 内，不重复计。
    """
    if not isinstance(usage, dict):
        return 0, 0
    def _p(*names):
        for n in names:
            v = usage.get(n)
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str) and v.strip().isdigit():
                return int(v)
        return 0
    return (_p("prompt_tokens", "input_tokens", "promptTokenCount"),
            _p("completion_tokens", "output_tokens", "candidatesTokenCount"))


def record_caller(caller: str, input_tokens: int, output_tokens: int):
    """按调用点累计 token：内存累加 + 落库（**只写增量，避免重复累加**）。

    ★ 落库时机：某个调用点**首次出现时立即落库**（否则查询接口读不到新调用点），
      之后每 _CALLER_FLUSH_EVERY 次调用补一次。
    ★ 关键正确性：缓冲里存的是"**尚未落库的增量**"，每次落库后必须清零，
      否则同一批数据会被重复加到 kv 上 → 用量虚增（自检时实测到 150 被写成 250）。
    任何失败都静默。
    """
    if not caller:
        return
    try:
        with _caller_lock:
            _seen = _caller_buf.get("__seen__")
            if _seen is None:
                _seen = set()
                _caller_buf["__seen__"] = _seen
            _first_time = caller not in _seen
            if _first_time:
                _seen.add(caller)
            buf = _caller_buf.get(caller)
            if buf is None:
                buf = [0, 0]
                _caller_buf[caller] = buf
            buf[0] += int(input_tokens or 0)
            buf[1] += int(output_tokens or 0)
            n = _caller_buf.get("__n__")
            if n is None:
                n = [0]
                _caller_buf["__n__"] = n
            n[0] += 1
            due = _first_time or (n[0] % _CALLER_FLUSH_EVERY == 0)
            if not due:
                return
            # 取出增量并清零（保持 __seen__ 不动，否则会重复触发首次落库）
            snapshot = {}
            for k, v in _caller_buf.items():
                if k in ("__n__", "__seen__"):
                    continue
                if v[0] or v[1]:
                    snapshot[k] = [v[0], v[1]]
                    _caller_buf[k] = [0, 0]
        _write_caller_snapshot(snapshot)
    except Exception:
        pass


def _write_caller_snapshot(snapshot: dict):
    """把快照累加进 kv（token:caller:{日期}:{调用点}）。"""
    day = datetime.now().strftime("%Y-%m-%d")
    for _caller, (ci, co) in snapshot.items():
        if not (ci or co):
            continue
        key = "token:caller:%s:%s" % (day, _caller)
        try:
            prev = db.kv_get(key) or "0,0"
            pi, _, po = str(prev).partition(",")
            db.kv_set(key, "%d,%d" % (int(pi or 0) + ci, int(po or 0) + co))
        except Exception:
            pass


def flush_caller_usage() -> int:
    """把内存缓冲里的调用点累计立即落库，返回落库条数。

    正常路径：某调用点首次出现即落库，之后每 _CALLER_FLUSH_EVERY 次补一次。
    本函数用于同进程内查询前强制同步、以及进程正常退出时补齐最后不足一批的数据。
    """
    try:
        with _caller_lock:
            snapshot = {}
            for k, v in _caller_buf.items():
                if k in ("__n__", "__seen__"):
                    continue
                if v[0] or v[1]:
                    snapshot[k] = [v[0], v[1]]
                    _caller_buf[k] = [0, 0]
            if not snapshot:
                return 0
        _write_caller_snapshot(snapshot)
        return len(snapshot)
    except Exception:
        return 0


def caller_usage(date_str: str = "") -> dict:
    """读取某天各调用点的 token 用量。返回 {caller: (input, output, total)}，按总量降序。

    调用前会先把内存缓冲落库，保证读数不滞后。
    """
    flush_caller_usage()
    day = date_str or datetime.now().strftime("%Y-%m-%d")
    out = {}
    try:
        rows = db.q("SELECT key, value FROM kv WHERE key LIKE ?", ("token:caller:%s:%%" % day,), fetch=True) or []
        for r in rows:
            try:
                k = r["key"] if isinstance(r, dict) else r[0]
                v = r["value"] if isinstance(r, dict) else r[1]
                caller = str(k).split(":", 3)[3]
                pi, _, po = str(v).partition(",")
                ci, co = int(pi or 0), int(po or 0)
                out[caller] = (ci, co, ci + co)
            except Exception:
                continue
    except Exception:
        pass
    return dict(sorted(out.items(), key=lambda kv: -kv[1][2]))


def record_usage(input_tokens: int, output_tokens: int, model: str = ""):
    """记录一次调用的 token 用量"""
    now = datetime.now()
    date_key = "token:date:" + now.strftime("%Y-%m-%d")
    month_key = "token:month:" + now.strftime("%Y-%m")
    total = input_tokens + output_tokens

    cur_date = int(db.kv_get(date_key) or 0)
    db.kv_set(date_key, cur_date + total)

    cur_month = int(db.kv_get(month_key) or 0)
    db.kv_set(month_key, cur_month + total)

    # 记录模型维度
    if model:
        model_key = "token:model:" + model + ":" + now.strftime("%Y-%m")
        cur_model = int(db.kv_get(model_key) or 0)
        db.kv_set(model_key, cur_model + total)


def get_usage() -> dict:
    """获取当日/当月用量"""
    now = datetime.now()
    date_key = "token:date:" + now.strftime("%Y-%m-%d")
    month_key = "token:month:" + now.strftime("%Y-%m")
    daily = int(db.kv_get(date_key) or 0)
    monthly = int(db.kv_get(month_key) or 0)

    daily_warn = int(db.kv_get("token:warn:daily") or DEFAULT_DAILY_WARN)
    monthly_warn = int(db.kv_get("token:warn:monthly") or DEFAULT_MONTHLY_WARN)

    return {
        "daily": daily,
        "monthly": monthly,
        "daily_warn": daily_warn,
        "monthly_warn": monthly_warn,
        "daily_pct": round(daily / daily_warn * 100, 1) if daily_warn else 0,
        "monthly_pct": round(monthly / monthly_warn * 100, 1) if monthly_warn else 0,
        "date": now.strftime("%Y-%m-%d"),
        "month": now.strftime("%Y-%m"),
    }


def set_warn_threshold(daily: int = None, monthly: int = None):
    """设置告警阈值"""
    if daily is not None:
        db.kv_set("token:warn:daily", max(1000, int(daily)))
    if monthly is not None:
        db.kv_set("token:warn:monthly", max(10000, int(monthly)))


def check_warn() -> dict:
    """检查是否触发告警，返回 {warn: bool, level: 'daily'/'monthly'/None, message: str}"""
    usage = get_usage()
    if usage["daily_pct"] >= 100:
        return {"warn": True, "level": "daily",
                "message": "今日 token 用量已达阈值 %d，建议减少对话或切换低成本模型" % usage["daily_warn"]}
    if usage["monthly_pct"] >= 100:
        return {"warn": True, "level": "monthly",
                "message": "本月 token 用量已达阈值 %d，建议关注 API 额度" % usage["monthly_warn"]}
    if usage["daily_pct"] >= 80:
        return {"warn": True, "level": "daily_80",
                "message": "今日 token 用量已达阈值的 80%%，注意控制"}
    return {"warn": False, "level": None, "message": ""}
