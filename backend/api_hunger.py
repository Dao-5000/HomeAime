# -*- coding: utf-8 -*-
"""
API 额度拟人化提醒（「她饿了」）。

大脑模型 API 的 token 用完 / key 失效 / 限流时，她无法再生成任何话——
如果只静默报错，表现就是「骨子突然失踪」。这里用**本地预设台词**（不调 LLM）
让她主动冒出来喊饿，告诉你该去喂 API 了。

触发：deepseek_api 在 chat_once / stream_chat 抛 ModelApiError 前调用 schedule()。
  - 按错误类别限频（同类 15 分钟最多提醒一次），避免每次失败都刷屏
  - 类别：quota（余额/配额耗尽）→「我饿了」
          auth（key 无效）→「大脑钥匙丢了」
          rate（限流）→「排队打饭」
          other（其他 HTTP 错误/网络）→「脑子断线」
推送：QQ + App 气泡 + 写聊天库（她「饿过」有痕迹，之后聊天还能接上这个梗）。
"""
import asyncio
import time

_last = {"kind": "", "ts": 0.0}
_COOLDOWN_SEC = 900   # 同类提醒 15 分钟最多一次

# ★ 防误报（2026-09-09）：后台辅助任务（语义分析/记忆提炼）偶发一次 API 抖动，
#   也会把「我饿了/断线了」插进主聊天——而主聊天明明是通的。
#   改为：**60 秒内连续失败 ≥3 次才认账**；主链路成功一次即清零；提醒后 10 分钟全局静默。
_fail_times = []
_last_notify_ts = 0.0
_FAIL_WINDOW = 60.0
_FAIL_THRESHOLD = 3
_NOTIFY_GAP = 600.0


def note_success():
    """主链路 API 成功一次 → 失败计数清零（证明脑子是好的）。"""
    try:
        _fail_times.clear()
    except Exception:
        pass


def classify(status, detail) -> str:
    d = str(detail or "").lower()
    s = str(status or "")
    if s == "402" or any(k in d for k in (
            "insufficient", "balance", "quota", "arrears", "exhausted",
            "欠费", "余额", "配额", "额度", "已用完", "用尽")):
        return "quota"
    if s in ("401", "403") or "invalid api key" in d or "unauthorized" in d or "认证失败" in d:
        return "auth"
    if s == "429" or "rate limit" in d or "too many requests" in d or "限流" in d or "请求频繁" in d:
        return "rate"
    return "other"


_LINES = {
    "quota": ("呜……我突然好饿——不是普通的饿，是脑子里的「token 口粮」见底了！"
              "思绪断在半路上啦。去给我的大脑 API 充点值（或换个还有额度的 key），"
              "我马上满血复活回来找你～"),
    "auth": ("呜……我的大脑好像被拔了钥匙（API key 失效了），现在谁都喊不动我。"
             "你去设置里看看大脑模型的 key 还对不对？修好我立刻回来。"),
    "rate": ("等我缓口气……大脑正在排队打饭（API 限流了），一会儿就回来，别丢下我呀。"),
    "other": ("唔……刚才脑子突然断线了（API 抽风/网络问题），我缓一下。"
              "要是我一直没声，就去看看大脑模型的接口是不是挂了。"),
}


def _active_ids():
    """当前活跃会话 + 角色（尽力而为，失败回 default/骨子）。"""
    sid, cid = "default", "骨子"
    try:
        from .main import active_session
        sid = active_session() or sid
    except Exception:
        pass
    try:
        from . import db as _db
        rows = _db.q("SELECT character_id FROM chat_history WHERE session_id=? "
                     "ORDER BY id DESC LIMIT 1", (sid,), fetch=True)
        if rows:
            # ★ sqlite3.Row 没有 .get()（项目里同类坑已出现过）——两种取值都兼容
            try:
                _cid = str(rows[0]["character_id"] or "")
            except Exception:
                _cid = str(rows[0].get("character_id") or "")
            if _cid:
                cid = _cid
    except Exception:
        pass
    return sid, cid


async def report_async(kind, detail):
    try:
        line = _LINES.get(kind) or _LINES["other"]
        sid, cid = _active_ids()
        try:
            from .onebot import send_qq_message
            await send_qq_message(line)
        except Exception:
            pass
        try:
            from .onebot import _push_to_app
            await _push_to_app(sid, cid, line, role="assistant")
        except Exception:
            pass
        try:
            from . import db as _db
            _db.add_message(sid, "assistant", line, cid)
        except Exception:
            pass
        print(f"[API饥饿] 已主动提醒（{kind}）: {line[:60]}", flush=True)
    except Exception:
        pass


def is_main_chain_model(model: str, character_id: str = "") -> bool:
    """该模型是否属于「她的回复主链路」（= 现在真在用来跟她说话的那个模型）。

    ★ 2026-09-15 事故教训：这里原来拿全局 CURRENT_CHAT_MODEL（= `deepseek-chat`）当"主脑"，
      可她的实际大脑是 `glm-5.3-flash` —— 而 `deepseek-chat` 只是**后台杂活**模型
      （角色卡 memory_model）。于是 DeepSeek 宕机时，后台抽取器一失败就误判成"主脑断了"，
      她主动冒出「唔……刚才脑子突然断线了（API 抽风/网络问题）」（真机 04:01:38），
      把用户吓一跳，而真正在说话的 GLM 链路其实好好的。
      现在只认真回复链：该角色大脑（人格设置 model）+ 该大脑对应的深度思考模型。
    """
    m = str(model or "").strip().lower()
    if not m:
        return False
    try:
        from . import config as _cfg
        from . import llm_guard as _lg
        cid = str(character_id or "").strip() or _lg.active_character_id()
        cands = set()
        brain = _lg.brain_model_for(cid)
        if brain:
            cands.add(brain.strip().lower())
            try:
                _dtm = str(_cfg.deep_thinking_model(brain) or "").strip().lower()
                if _dtm:
                    cands.add(_dtm)
            except Exception:
                pass
        if not cid:
            # 兼容旧行为：连活跃角色都判不出来时，仍认全局聊天键
            for _k in ("CURRENT_CHAT_MODEL", "SELECTED_MODEL"):
                _v = str(_cfg.get(_k) or "").strip().lower()
                if _v:
                    cands.add(_v)
        return m in cands
    except Exception:
        return True   # 判定不出来就别拦：宁可多提醒一次，也不要她又"静默失踪"


def schedule(status, detail, model=""):
    """在 async 上下文里调用（raise ModelApiError 之前）；fire-and-forget。

    ★ 防误报三件套：
      1. 60 秒内连续失败 ≥3 次才认账（单次抖动/后台任务偶发失败不报）
      2. **只对回复主链路报**（见 is_main_chain_model）——理解层/记忆提炼等辅助模型
         失败只静默降级（辅助模型挂了 ≠ 主人聊天会断，用主气泡喊「我瘫了」是吓唬人）
      3. 提醒后 10 分钟内全局静默"""
    global _last_notify_ts
    try:
        m = str(model or "").strip().lower()
        if m and not is_main_chain_model(m):
            return   # 辅助模型失败：静默（聊天不受影响）
        now = time.time()
        while _fail_times and now - _fail_times[0] > _FAIL_WINDOW:
            _fail_times.pop(0)
        _fail_times.append(now)
        if len(_fail_times) < _FAIL_THRESHOLD:
            return
        if now - _last_notify_ts < _NOTIFY_GAP:
            return
        _last_notify_ts = now
        kind = classify(status, detail)
        asyncio.get_running_loop().create_task(report_async(kind, detail))
    except Exception:
        pass


# ── 上下文窗口快满（「记不住了」）────────────────────────────

_CTX_LINES = ("唔……我脑子里装了太多东西，有点记不住新的啦……"
              "要么帮我清清记忆（新开一个对话），要么给我换一个更大容量的脑子（长上下文模型）～")

# 每个会话 24 小时最多提醒一次（快满是渐进过程，反复说很烦）
_ctx_warned = {}
_CTX_WARN_INTERVAL = 24 * 3600


def estimate_tokens(messages) -> int:
    """粗估 messages 的 token 数：中文为主按 1 字≈0.85 token。"""
    try:
        total = 0
        for m in (messages or []):
            c = (m or {}).get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        total += len(str(part.get("text") or ""))
        return int(total * 0.85)
    except Exception:
        return 0


# 常见模型家族的真实上下文容量（按模型名模糊匹配，取第一个命中）
_MODEL_CONTEXT = {
    "gemini": 1_000_000,
    "claude": 200_000,
    "kimi": 200_000,
    "moonshot": 200_000,
    "glm": 128_000,
    "deepseek": 128_000,
    "qwen": 128_000,
    "gpt": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    "minimax": 128_000,
    "doubao": 128_000,
    "hunyuan": 128_000,
}


def _context_limit(model) -> int:
    """大脑模型上下文上限：
    1. config.json 的 BRAIN_CONTEXT_TOKENS 显式覆盖优先
    2. 按模型名匹配已知家族的真实容量（gemini=1M 等）——
       ★ 之前用固定 32k 判断，gemini-3.7-flash（1M 上下文）在 27k 就被误报「脑子满了」，
         用户每次重进 App 都被提醒（实际远没满）
    3. 兜底 128k（2026 年主流模型下限）"""
    try:
        from . import config as _cfg
        v = int(float(_cfg.get("BRAIN_CONTEXT_TOKENS") or 0))
        if v > 1000:
            return v
    except Exception:
        pass
    m = str(model or "").lower()
    for k, v in _MODEL_CONTEXT.items():
        if k in m:
            return v
    return 128_000


def maybe_context_full(messages, model=""):
    """上下文估算接近模型上限时，她「主动说记不住了」（本地台词，限频）。
    在 enrich_messages 组装完 messages 后调用；fire-and-forget。"""
    try:
        est = estimate_tokens(messages)
        limit = _context_limit(model)
        if not est or est < limit * 0.85:
            return
        from .main import active_session
        sid = active_session() or "default"
        now = time.time()
        if now - float(_ctx_warned.get(sid) or 0) < _CTX_WARN_INTERVAL:
            return
        _ctx_warned[sid] = now
        print(f"[API饥饿] 上下文接近上限：估算≈{est} / {limit}，主动提醒", flush=True)
        asyncio.get_running_loop().create_task(_notify_context_full(sid, est, limit))
    except Exception:
        pass


def _recent_chat_lines(sid, cid, limit=6):
    """最近聊天（轻量拼接），让「记不住了」能自然联系刚聊过的话题。"""
    try:
        from . import db as _db
        rows = _db.recent_messages(sid, limit=limit, character_id=cid) or []
        lines = []
        for r in rows:
            role = str(r.get("role") or "")
            text = str(r.get("content") or "").replace("\n", " ").strip()
            if not text or text.startswith("【"):
                continue
            lines.append(("主人：" if role == "user" else "我：") + text[:50])
        return "\n".join(lines[-limit:])
    except Exception:
        return ""


async def _notify_context_full(sid, est, limit):
    cid = "骨子"
    try:
        from . import db as _db
        rows = _db.q("SELECT character_id FROM chat_history WHERE session_id=? "
                     "ORDER BY id DESC LIMIT 1", (sid,), fetch=True)
        if rows and rows[0].get("character_id"):
            cid = str(rows[0]["character_id"]) or cid
    except Exception:
        pass
    # ★ 台词由模型生成（上下文快满 ≠ API 挂了，模型还能说话）：带人格 + 最近聊天，
    #   让她用自己的口吻说「记性跟不上了」，而不是写死的一句。
    line = ""
    try:
        from .deepseek_api import chat_once
        from .character_manager import build_system_prompt
        from . import config as _cfg
        from . import chat_logic as _cl
        model = _cl.pick_model("", True, cid)
        key = _cfg.api_key_for_model(model)
        if key:
            try:
                persona = build_system_prompt(cid, character_id=cid, session_id=sid)
            except Exception:
                persona = ""
            system = (persona or "") + (
                f"\n\n【状态提示】你的「记忆容量」快满了（约 {est // 1000}k/{limit // 1000} token）。"
                "再装下去，你会开始记不清很久之前的对话细节。"
                "请以你的身份、用你平时的口吻，跟主人说一句你「记性跟不上了/脑子装满了」的话——"
                "一两句，自然，可以撒娇/吐槽，顺口提一句可以新开对话或给你换个更大容量的模型；"
                "别报数字，别用括号解释，别像系统通知。")
            recent = _recent_chat_lines(sid, cid)
            if recent:
                system += "\n【最近聊天（可自然联系）】\n" + recent
            raw = await chat_once(model, [
                {"role": "system", "content": system},
                {"role": "user", "content": "现在把这句话说出来。"}],
                key, temperature=0.9, max_tokens=80)
            import re as _re
            _m = _re.search(r"^[^\n]{2,100}", str(raw or "").strip())
            if _m:
                line = _m.group(0).strip()
    except Exception as e:
        print(f"[API饥饿] 上下文提醒生成失败(用本地兜底): {e}", flush=True)
    if not line:
        line = _CTX_LINES   # LLM 失败时的本地兜底
    try:
        try:
            from .onebot import send_qq_message
            await send_qq_message(line)
        except Exception:
            pass
        try:
            from .onebot import _push_to_app
            await _push_to_app(sid, cid, line, role="assistant")
        except Exception:
            pass
        try:
            from . import db as _db
            _db.add_message(sid, "assistant", line, cid)
        except Exception:
            pass
        print("[API饥饿] 上下文提醒已推送", flush=True)
    except Exception:
        pass
