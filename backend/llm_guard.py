# -*- coding: utf-8 -*-
"""LLM provider 熔断 / 硬超时 / 失败降级 —— 2026-09-15 DeepSeek 宕机事故治本。

事故（真机日志 backend_dev.log 行 143293-144455）：
  角色卡「骨子」大脑是 glm-5.3-flash，但
    · 全局 DEEP_THINKING_MODEL=deepseek-flash → **回复主链路**打在 DeepSeek；
    · 角色卡 memory_model=deepseek-chat → 记忆/情绪/关系/画像/承诺/反思/日总结等
      二十多个后台抽取器全部打在 DeepSeek。
  DeepSeek 宕机后：单次非流式调用挂 **900.15s** 才回 200 空 body（×100+），
  流式回复链路 `stream_chat deepseek-flash 总=903.30s out=0`（一个字都没出）；
  20 条连接的共享池（http_client.max_connections=20）被挂起请求占满 →
  PoolTimeout 级联到别的调用 → `[OneBot] 生成失败` → 她只能答
  「（刚刚想事情卡住了，你再说一次～）」。

本模块提供三件事，全部集中在 deepseek_api 的两个入口处生效：
  1. **硬超时**：杂活 30s / 长档 180s / 流式首包 60s、中途卡死 90s（可配）
  2. **熔断**：同一 provider 连续 3 次「provider 侧故障」→ 熔断 5 分钟（可配），
     熔断期内不再发网络请求（快速失败，绝不占连接池）
  3. **降级**：主模型失败 → 自动用「当前活跃角色的大脑」重试一次；
     provider 已熔断 → 直接改用兜底模型

计数口径只认「provider 病了」：超时 / 连不上 / 5xx / 429 / 200+空 body 签名。
参数错误（400）、鉴权（401/403）、余额（402）**不**计入熔断——那是配置问题，
各自另有提示路径，混进来会让熔断误开。
"""
import asyncio
import time

try:
    import httpx
except Exception:      # pragma: no cover - httpx 是硬依赖，这里只为兜底不炸
    httpx = None

# 空 body 签名：真机实测 `finish_reason=None | usage={} | message={}`（200 但没内容）
FAIL_EMPTY = "empty-content"

_FAILS = {}            # provider -> {"fails": int, "open_until": float, "last_err": str}
_BRAIN_CACHE = {"ts": 0.0, "cid": "", "model": ""}
_BRAIN_TTL = 5.0
_ACTIVE_CACHE = {"ts": 0.0, "cid": ""}
_ACTIVE_TTL = 5.0


def _log(msg: str) -> None:
    try:
        print(f"[LLMGuard] {msg}", flush=True)
    except Exception:
        pass


def note(msg: str) -> None:
    """对外日志（守卫层用）：`[LLMGuard] ...`，DEV_LOG 一定抓得到。"""
    _log(msg)


def _cfg(key: str, default):
    try:
        from . import config
        v = config.get(key, default)
        return default if v in (None, "") else v
    except Exception:
        return default


def _num(key: str, default: float) -> float:
    try:
        return float(_cfg(key, default))
    except Exception:
        return float(default)


# ── 档位（秒）──────────────────────────────────────────────
# 下限只做"别配成 0/负数"的兜底；配置接口（config.update）另有 ≥1 秒的用户侧校验。
_MIN_TMO = 0.05


def aux_timeout_sec() -> float:
    """后台杂活单次硬超时：默认 30s（真机机械抽取普遍 0.4~7s）。"""
    return max(_MIN_TMO, _num("LLM_AUX_TIMEOUT_SEC", 30.0))


def long_timeout_sec() -> float:
    """长档硬超时：默认 180s。给「合法的慢杂活」用。

    真机实测（backend_dev.log）：`chat_once(generator.generate_reflection) glm-5.3-flash
    56.62s`（23:18）/ `28.07s`（08:50）、`chat_once(summarizer._llm) glm-5.3-flash 24.62s`。
    一刀切 30s 会把它们全部误杀 —— 所以反思/日周月总结/回复链显式声明长档。
    """
    return max(_MIN_TMO, _num("LLM_LONG_TIMEOUT_SEC", 180.0))


def stream_first_timeout_sec() -> float:
    """流式「首包」超时：默认 60s。超过就掐断并换兜底模型重放。

    之所以要它：真机出现过 `stream_chat deepseek-flash 总=903.30s out=0` ——
    上游 15 分钟不给任何字节，用户只看到「她不说话」。
    """
    return max(_MIN_TMO, _num("LLM_STREAM_FIRST_TIMEOUT_SEC", 60.0))


def stream_stall_timeout_sec() -> float:
    """流式「中途卡死」超时：默认 90s。已经在出字就不再重放（防重复文本）。"""
    return max(_MIN_TMO, _num("LLM_STREAM_STALL_TIMEOUT_SEC", 90.0))


def breaker_fails() -> int:
    try:
        return max(1, int(_num("LLM_BREAKER_FAILS", 3)))
    except Exception:
        return 3


def breaker_cooldown_sec() -> float:
    return max(1.0, _num("LLM_BREAKER_COOLDOWN_SEC", 300.0))


# 真机实测「合法的慢调用」（backend_dev.log 1157 条 chat_once 画像：成功调用里超过 30s 的
# 全部集中在这几个调用点）。命中的一律给长档，绝不因为"想快"而误杀它们。
#   generator.generate_reflection                max 84.8s / p90 56.6s
#   scheduler._send_morning_with_letter_reminder  max 56.2s
#   scheduler._check_conflict / _check_boredom / _check_narrative_arc / _send_onlogin  44.2 / 41.8 / 36.7 / 35.0s
#   summarizer._llm（日总结）                     max 24.6s（离 30s 太近，也算长档）
_SLOW_CALLERS = {
    "generator.generate_reflection",
    "summarizer._llm",
    "scheduler._send_morning_with_letter_reminder",
    "scheduler._check_conflict",
    "scheduler._check_boredom",
    "scheduler._check_narrative_arc",
    "scheduler._send_onlogin",
}

_CHORE_CACHE = {"ts": 0.0, "models": frozenset()}
_CHORE_TTL = 5.0


def chore_models() -> frozenset:
    """当前「后台杂活模型」集合（角色卡 memory_model / 全局 legacy / 主脑降级档）。

    刻意用 `respect_breaker=False`：熔断期间 config.memory_extract_model() 会返回大脑，
    那是**降级结果**，不能反过来当成"杂活模型"来判定超时档位（否则回复链会误拿 30s 档）。
    """
    now = time.time()
    if (now - _CHORE_CACHE["ts"]) < _CHORE_TTL and _CHORE_CACHE["models"]:
        return _CHORE_CACHE["models"]
    models = set()
    try:
        from . import config
        cid = active_character_id()
        for _cid in ([cid] if cid else []) + [""]:
            try:
                _m = str(config.memory_extract_model(_cid, respect_breaker=False) or "").strip()
                if _m:
                    models.add(_m)
            except Exception:
                pass
        _legacy = str(_cfg("MEMORY_EXTRACT_MODEL", "") or "").strip()
        if _legacy:
            models.add(_legacy)
    except Exception:
        pass
    _CHORE_CACHE.update({"ts": now, "models": frozenset(models)})
    return _CHORE_CACHE["models"]


def timeout_for(model_key: str, caller: str = "", explicit=None) -> float:
    """该次调用的硬超时（秒）。

    分档原则：**拿不准就给长档**（宁可慢一点，也绝不误杀正常的回复/长文提炼）；
    只有确定是"后台机械杂活"时才用 30s 快档 —— 那正是事故里挂 900 秒的那批调用。
    """
    if explicit:
        try:
            return max(1.0, float(explicit))
        except Exception:
            pass
    if str(caller or "") in _SLOW_CALLERS:
        return long_timeout_sec()
    try:
        # 本地 Ollama 大脑：模型要现加载、CPU 上很慢，绝不能用杂活档掐它
        if provider_of(model_key) == "local":
            return long_timeout_sec()
        if str(model_key or "").strip() in chore_models():
            return aux_timeout_sec()
    except Exception:
        pass
    return long_timeout_sec()


# ── provider 归属 ──────────────────────────────────────────
def provider_of(model_key: str) -> str:
    """模型 → provider（走模型池；查不到按 DeepSeek 兜底，与原 get_text_model_config 一致）。"""
    try:
        from . import config
        prov = str((config.get_text_model_config(model_key) or {}).get("provider") or "").lower()
        return prov or "deepseek"
    except Exception:
        return "deepseek"


def _has_key(model_key: str) -> bool:
    try:
        from . import config
        return bool(str(config.api_key_for_model(model_key) or "").strip())
    except Exception:
        return False


# ── 熔断 ───────────────────────────────────────────────────
def classify_failure(err) -> bool:
    """True = provider 侧故障（计入熔断）；False = 参数/鉴权/余额类（不计入）。"""
    if err is None:
        return False
    if isinstance(err, str):
        return err.strip().lower() in (FAIL_EMPTY.lower(), "empty", "stall", "timeout", "hang")
    if isinstance(err, asyncio.TimeoutError):
        return True
    if httpx is not None and isinstance(err, (httpx.TimeoutException, httpx.TransportError)):
        return True
    status = getattr(err, "status", None)
    if status is None:
        status = getattr(err, "status_code", None)
    if status is not None:
        try:
            s = int(status)
        except Exception:
            s = 0
        if s == 0:
            return True
        if s in (408, 409, 425, 429) or s >= 500:
            return True
        if 400 <= s < 500:
            return False
    return True


def is_open(model_key: str) -> bool:
    """该模型所属 provider 是否熔断中（冷却结束自动半开、允许试探）。"""
    st = _FAILS.get(provider_of(model_key))
    if not st:
        return False
    until = float(st.get("open_until") or 0.0)
    if until <= 0:
        return False
    if time.time() < until:
        return True
    st["open_until"] = 0.0
    st["fails"] = 0
    return False


def note_fail(model_key: str, err=None) -> None:
    if not classify_failure(err):
        return
    prov = provider_of(model_key)
    st = _FAILS.setdefault(prov, {"fails": 0, "open_until": 0.0, "last_err": ""})
    st["fails"] = int(st.get("fails") or 0) + 1
    st["last_err"] = (str(err)[:200] if isinstance(err, str)
                      else f"{type(err).__name__}: {err}"[:200])
    if st["fails"] >= breaker_fails() and float(st.get("open_until") or 0.0) <= time.time():
        st["open_until"] = time.time() + breaker_cooldown_sec()
        _log(f"{prov} 连续失败 {st['fails']} 次 → 熔断 {breaker_cooldown_sec():.0f}s"
             f"（最后一次：{st['last_err']}）")


def note_ok(model_key: str) -> None:
    st = _FAILS.get(provider_of(model_key))
    if st:
        st["fails"] = 0
        st["open_until"] = 0.0


def reset() -> None:
    _FAILS.clear()
    _BRAIN_CACHE.update({"ts": 0.0, "cid": "", "model": ""})
    _ACTIVE_CACHE.update({"ts": 0.0, "cid": ""})
    _CHORE_CACHE.update({"ts": 0.0, "models": frozenset()})


def snapshot() -> dict:
    now = time.time()
    out = {}
    for p, st in _FAILS.items():
        left = max(0.0, float(st.get("open_until") or 0.0) - now)
        out[p] = {"fails": st.get("fails"), "open_left": round(left, 1),
                  "last_err": st.get("last_err")}
    return out


# ── 降级目标解析 ───────────────────────────────────────────
def active_character_id() -> str:
    """最近说过话的那个角色（拿不到返回空串）。

    刻意**不** import main.active_session：那会把整个 main 模块拖进来（启动期慢、
    还可能形成循环依赖）。直接问库「最后一条聊天记录是谁」——对「该切谁的大脑」
    这个问题，最近说话的角色就是正确答案，且 QQ/主动消息/前端三条链路通吃。
    带 5 秒缓存：chat_once 每次调用都要解析兜底，不能每次都查库。
    """
    now = time.time()
    if (now - _ACTIVE_CACHE["ts"]) < _ACTIVE_TTL:
        return str(_ACTIVE_CACHE["cid"] or "")
    cid = ""
    try:
        from . import db as _db
        rows = _db.q("SELECT character_id FROM chat_history "
                     "ORDER BY id DESC LIMIT 1", (), fetch=True)
        if rows:
            # ★ db.q 用 sqlite3.Row（没有 .get()）——项目里已因这个踩过坑
            #   （backend.emotion_memory 就报过 "'sqlite3.Row' object has no attribute 'get'"），
            #   这里两种取值方式都兼容，避免"解析不到活跃角色"这种静默退化。
            _r = rows[0]
            try:
                cid = str(_r["character_id"] or "")
            except Exception:
                try:
                    cid = str(_r.get("character_id") or "")
                except Exception:
                    cid = ""
    except Exception:
        cid = ""
    _ACTIVE_CACHE.update({"ts": now, "cid": cid})
    return cid


def _brain_info(character_id: str = "") -> tuple:
    """返回 (大脑模型, 是否来自角色卡)。

    「来自角色卡」很关键：人格设置里明确设定的大脑 = 我们要保的那条命，
    哪怕它和出故障的模型同 provider 也优先切它（例如 glm-5.3 单模型不可用 → 回 glm-5.3-flash）；
    而全局默认值（selected_model）不算数 —— 那种情况下同 provider 大概率一起挂，
    应该优先去跨 provider 备用池找活路。
    """
    cid = str(character_id or "").strip()
    now = time.time()
    if cid and _BRAIN_CACHE["cid"] == cid and (now - _BRAIN_CACHE["ts"]) < _BRAIN_TTL:
        return str(_BRAIN_CACHE["model"] or ""), bool(_BRAIN_CACHE.get("from_card"))
    model, from_card = "", False
    try:
        if not cid:
            cid = active_character_id()
        if cid:
            from . import character_manager as _cm
            card = _cm.get_character_any(cid) or {}
            if not card.get("local_brain"):
                model = str(card.get("model") or "").strip()
                from_card = bool(model)
    except Exception:
        model, from_card = "", False
    if not model:
        try:
            from . import config
            model = str(config.selected_model() or "").strip()
        except Exception:
            model = ""
    if cid:
        _BRAIN_CACHE.update({"ts": now, "cid": cid, "model": model, "from_card": from_card})
    return model, from_card


def brain_model_for(character_id: str = "") -> str:
    """该角色的大脑模型（人格设置的角色卡 model）> 全局 SELECTED_MODEL。

    只认「人格设置里的大脑」——刻意**不**走 pick_model()，因为那条路会先套
    深度思考模型（`deepseek-flash`），拿它当兜底等于没兜。
    """
    return _brain_info(character_id)[0]


def fallback_model_for(model_key: str, brain: str = None) -> str:
    """失败时改用哪个模型（空串 = 没有可用兜底）。

    优先级（决策 D1/D2 的「切大脑」，但保证不会切到同一家已经挂掉的 provider）：
      1. 显式配置 LLM_FALLBACK_MODEL（可用时）
      2. **角色卡里明确设定的大脑**（不同模型、未熔断）—— 哪怕同 provider 也优先：
         典型是 glm-5.3 单模型不可用 → 回大脑 glm-5.3-flash
      3. 跨 provider 备用池：glm-5.3-flash（有智谱 key）> deepseek-v4-flash（有 DS key）
      4. 全局默认大脑（selected_model 这类"没有角色卡依据"的）—— 仅当它还没熔断
    调用方也可以直接传 brain= 指定（测试与上层显式决策用）。
    """
    m = str(model_key or "").strip()
    if not m:
        return ""
    prov = provider_of(m)

    explicit = str(_cfg("LLM_FALLBACK_MODEL", "") or "").strip()
    if explicit and explicit != m and provider_of(explicit) != prov and not is_open(explicit):
        return explicit

    if brain:
        b, from_card = str(brain).strip(), True
    else:
        b, from_card = _brain_info()
    # ② 角色卡大脑：不同模型 + 没熔断 → 直接用（同 provider 也行，见 docstring）
    if from_card and b and b != m and not is_open(b):
        return b
    # ③ 跨 provider 备用池
    for cand in ("glm-5.3-flash", "deepseek-v4-flash"):
        if cand == m or provider_of(cand) == prov or is_open(cand):
            continue
        if _has_key(cand):
            return cand
    # ④ 兜底：全局默认大脑（同 provider 也认，总好过原地等死）
    if b and b != m and not is_open(b):
        return b
    return ""
