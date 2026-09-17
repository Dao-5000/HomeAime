# -*- coding: utf-8 -*-
"""
DeepSeek / 视觉模型 API 客户端（仅负责文本生成，不含任何业务逻辑）

关键点：
1. 流式调用，输出 SSE data 行（与原 Node server 相同协议，前端零改动）。
2. ★ 彻底忽略 reasoning_content：deepseek-reasoner（R1）流里的思考内容只提取 content，
   重建后的 delta 只包含 content 字段，思考内容绝不推送给前端。
3. 流末尾由调用方追加 _meta 行（前端 chat.js 依赖该行判断截断）。
"""
import asyncio
import json
import logging
import re
import sys
import time

import httpx

from .http_client import get_http_client

# 统一日志：print 同时进 stdout（DEV_LOG tee 会捕获），方便开发者日志排查 AI 失败
def _log_err(msg):
    try:
        print(f"[AI-ERR] {msg}", flush=True)
    except Exception:
        pass


def _hunger(status, detail, model=""):
    """★ API 额度/故障拟人化提醒（「她饿了」）：本地台词主动告知用户，不调 LLM。
    只对主聊天模型报（辅助模型失败静默）；fire-and-forget。"""
    try:
        from . import api_hunger
        api_hunger.schedule(status, detail, model=model)
    except Exception:
        pass

DEEPSEEK_BASE = "https://api.deepseek.com"
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=None)


def _infer_base_url(model: str) -> str:
    """根据模型 provider 从模型池推断 base_url；拿不到就返回空串（走默认地址）。"""
    try:
        from . import config as _cfg
        return str((_cfg.get_text_model_config(model) or {}).get("baseUrl") or "")
    except Exception:
        return ""


def _chat_completions_url(base_url: str, default_url: str, model: str = "") -> str:
    """把 base_url 规范化为 /chat/completions 完整地址。
    支持：
    - 空字符串 → 先按 model 从模型池推断 baseUrl，再退回 default_url
    - 已包含 /chat/completions → 直接使用
    - 其他 → 自动追加 /chat/completions
    """
    url = str(base_url or "").strip()

    if not url:
        url = _infer_base_url(model)

    if not url:
        return default_url

    url = url.rstrip("/")

    if url.endswith("/chat/completions"):
        return url

    return url + "/chat/completions"


# ★ 推理模型（deepseek-reasoner/R1 等）思考极耗 token：小额度会把思考截断，
#   导致"思考没展开就答完"、回复显得过快且深度不足。统一放大额度让思考充分展开。
_REASONER_MIN_TOKENS = 8192

# ★ 用户明确要求：思考保持 max 不降档（2026-09-08）——曾试探过给 glm-5.3 默认压
#   reasoning_effort=low（能省时但明显变笨），已按用户要求移除，勿再加默认降档。
#   实测备忘：glm-5.3 系列"始终思考"不可关闭（HTTP 400 1210）；reasoning_effort=low
#   → 2.9s/正文 178 字；默认 max → 45s+ 深度推理。代价是慢，换来深度。


def _is_reasoner_model(model: str) -> bool:
    """判断是否为推理模型（思考极耗 token，需要大 max_tokens）。"""
    m = str(model or "").lower()
    if "reasoner" in m or "thinking" in m:
        return True
    # OpenAI o1/o3 系列（o1、o1-mini、o3、o3-mini 等）
    if m in ("o1", "o3") or m.startswith("o1-") or m.startswith("o3-"):
        return True
    # ★ 2026-09-12 补：名字里没有 reasoner/thinking、但**始终思考**的模型。
    #   上面第 82 行的注释早就写明"glm-5.3 系列始终思考不可关闭"，可判断函数却
    #   没把 glm 系列算进来 → 大额度保护对它完全失效。实测 glm-5.3-flash：
    #     max_tokens=240 → reasoning_tokens=238，finish_reason=length，content=""
    #     max_tokens=800 → reasoning_tokens=798，仍然 content=""
    #     max_tokens=4000 → finish_reason=stop，正常吐出 153 字正文
    #   而调用方普遍写 `if not msg: return False` —— 不报错、不发送、静默空转：
    #   _check_sleep_guard 每 20~35 秒重试一次，两小时 300+ 次 glm 调用零产出，
    #   而所有这些调用走的都是「角色卡主脑」，于是主动消息（早安/晚安/惊喜/催睡）
    #   全线失效却一声不响。
    #   max_tokens 只是上限、不额外计费，宁可放宽也不要再出现空白回复。
    if m.startswith("glm-5") or m.startswith("glm-z1"):
        return True
    if m.startswith("deepseek-v4") or m == "deepseek-flash":
        return True
    return False


# ==================== 本地大脑（Ollama）路由（2026-09-11） ====================
# ★ 设计：人格设置页「本地大脑」开关只是让 pick_model 返回模型池的 "local-brain"
#   （provider=local）。全项目 LLM 流量本就汇聚在 stream_chat / chat_once，在这两个
#   入口识别 provider=local 改走 Ollama 的 OpenAI 兼容接口（/v1/chat/completions），
#   两个聊天入口（/api/chat 与 /api/chat/stream）+ 思考展示 + 语音通话即全自动生效。
# ★ 思考归一化：Qwen3 思考内容兼容两种形态——Ollama 新版放 delta.reasoning /
#   reasoning_content 字段（直接映射）；旧版/兼容层内联 <think>...</think>（增量剥离）。
#   统一转成 reasoning_content delta 行，前端「已思考 N 秒」照常工作。
# ★ 一键切回的兜底半边：本地服务连不上/模型没装（首个数据块之前失败）时，
#   自动切回 LOCAL_BRAIN.fallback_model 云端模型，并给前端发 notice 提示（对话不中断）。


class LocalBrainUnavailable(Exception):
    """本地大脑不可用（服务没起/模型没装/接口报错）→ stream_chat/chat_once 自动回退云端。"""


_LOCAL_FALLBACK_INFO = {}   # 最近一次自动回退记录（/api/brain/local/status 读它展示）


def _local_brain_cfg() -> dict:
    try:
        from . import config as _cfg
        return _cfg.local_brain() or {}
    except Exception:
        return {}


# ★ 2026-09-12 方案②：本地路由的「精简 system」白名单
#   背景：实测本地路由要吞 ~12k token 的 system（人设 + 40 多个规则块 + 记忆 + 关系
#   + 感知 + 元信息）。对 8B Q4 来说「读懂 1.2 万 token 的规则、同时满足人格/多气泡/
#   表情包/逻辑一致」是超纲的 —— 表现就是被追问时只会说软话、转移话题、复读。
#   证据：同一份 prompt，glm-5.3-flash 能接住逻辑（"你都叫我去睡了还怎么聊到天亮啊"
#   这种矛盾它答得上），本地 8b 答不上；但同一模型在简单情境下（#9202）又能出好内容
#   → 不是不会，是**复杂时崩溃**。
#   做法：只保留"人设 / 身份 / 关系 / 记忆 / 语言风格 / 硬规则 / 本轮用户这句话"，
#   把元信息、内部状态、规则噪音删掉。**只在本地路由生效，云端一行不碰。**
_LOCAL_LITE_KEEP = {
    # 人设与身份
    "人格设定", "角色长期人格保持", "与你相处的模式", "与用户的相处模式",
    "始终保持助手的底色", "基于长期互动的人格偏移（覆盖基础设定）",
    # 称呼与对用户的了解
    "称呼", "你了解的关于TA的事",
    # 记忆与关系（长时记忆必须留，否则她会"忘事"）
    "用户长期记忆", "当前关系记忆", "共同经历时间线", "与当前角色相关经历",
    "上次对话承接（重启后也要接住）", "当前关系状态",
    # 语言风格
    "专属语言标记", "词汇风格", "句式习惯", "情绪表达方式", "专属习惯",
    # 硬规则（含 [sticker:]/[SONG] 标记规则，删了她就发不出表情包）
    "严禁", "别重复自己", "边界守护", "关键",
    # 场景与样例
    "感知信息", "参考例句 - 深夜场景", "本轮用户刚刚说的话（最高优先级）",
}


def _slim_system_for_local(messages: list) -> list:
    """把 system 消息按白名单精简（**只给本地路由用**；云端完全不经过这里）。

    结构：system content 是一串以「【标题】」分节的块。第一个块是角色头 + 主设定，
    永远保留；其余按 _LOCAL_LITE_KEEP 白名单保留，不在名单里的一律丢掉。
    任何异常都原样放行，绝不影响主流程。
    """
    import re as _re
    try:
        out = []
        for m in messages or []:
            if not isinstance(m, dict) or m.get("role") != "system":
                out.append(m)
                continue
            text = str(m.get("content") or "")
            if not text:
                out.append(m)
                continue
            parts = [p for p in _re.split(r"(?=【)", text) if p.strip()]
            kept = []
            for i, p in enumerate(parts):
                if i == 0:
                    kept.append(p)          # 角色头 + 主设定，永远保留
                    continue
                mm = _re.match(r"【([^】]{1,24})】", p)
                title = mm.group(1).strip() if mm else ""
                if title and title in _LOCAL_LITE_KEEP:
                    kept.append(p)
            slim = "".join(kept).strip()
            if slim:
                nm = dict(m)
                nm["content"] = slim
                out.append(nm)
            else:
                out.append(m)
        return out
    except Exception:
        return messages


def _local_max_tokens(cfg: dict, think: bool, max_tokens: int) -> int:
    """本地路由的输出 token 上限。★ 只作用于本地，云端完全不经过这里。

    背景（2026-09-12 方案A：8b 塞进 8G 显存）：
      · Ollama 的窗口是「提示词 + 输出」共享的，而 num_ctx 我们并不发送，
        实际由模型 Modelfile 决定（qwen3 系列默认 32768）。
      · 原来 think=True 时一律把 max_tokens 顶到 _REASONER_MIN_TOKENS=8192 ——
        那是按 32k 窗口定的。一旦为了塞进显存把窗口降到 16k/20k，
        「提示词 ~13k + 输出 8k」就超窗 → Ollama 直接 400 → 静默退回云端，
        本地部署形同虚设（实测日志里 7 次就是这么挂的）。
      · 所以这里加一道闸：保留「思考别被截断」的初衷（仍抬到 8192），
        但不超过 LOCAL_BRAIN.max_output_tokens，且至少给提示词留一半窗口。
    """
    try:
        if think and max_tokens < _REASONER_MIN_TOKENS:
            max_tokens = _REASONER_MIN_TOKENS
        cap = int(cfg.get("max_output_tokens") or 0)
        if cap > 0 and max_tokens > cap:
            max_tokens = cap
        win = int(cfg.get("num_ctx") or 0)
        if win > 0:
            hard = max(512, int(win * 0.5))   # 至少一半窗口留给提示词
            if max_tokens > hard:
                max_tokens = hard
    except Exception:
        pass
    return max_tokens


def _is_local_model(model: str) -> bool:
    """该模型标识是否指向本地大脑（模型池 provider=local/ollama，或就是 local-brain 令牌）。"""
    m = str(model or "").strip()
    if m == "local-brain":
        return True
    try:
        from . import config as _cfg
        provider = str((_cfg.get_text_model_config(m) or {}).get("provider") or "").lower()
        return provider in ("local", "ollama")
    except Exception:
        return False


def local_brain_last_fallback() -> dict:
    """最近一次本地→云端自动回退记录（无则空 dict）。"""
    return dict(_LOCAL_FALLBACK_INFO or {})


def _local_fallback_target(reason: str = ""):
    """本地不可用时的云端回退三元组 (model, key, base_url)，并记录回退信息供状态接口展示。"""
    cfg = _local_brain_cfg()
    fb = str(cfg.get("fallback_model") or "").strip() or "deepseek-chat"
    if _is_local_model(fb):
        fb = "deepseek-chat"   # 防呆：回退目标也被配成本地 → 强制官方默认
    key, burl = "", ""
    try:
        from . import config as _cfg
        key = _cfg.api_key_for_model(fb)
        burl = str((_cfg.get_text_model_config(fb) or {}).get("baseUrl") or "")
    except Exception:
        pass
    _LOCAL_FALLBACK_INFO.update({
        "time": time.strftime("%m-%d %H:%M:%S"),
        "reason": str(reason)[:160],
        "to": fb,
    })
    print(f"[LocalBrain] 不可用，自动切回云端 → {fb}（{reason}）", flush=True)
    return fb, key, burl


def _local_notice_sse(message: str, detail: str = "") -> str:
    obj = {"type": "notice", "message": message}
    if detail:
        obj["detail"] = str(detail)[:160]
    return "data: " + json.dumps(obj, ensure_ascii=False)


# ---- 本地路由上下文裁剪（2026-09-12）----
# ★ 只在 _stream_local / _chat_once_local 里调用，云端分支完全不经过——
#   云端的超长上下文行为保持原样（超了走云端自己的报错/降级逻辑）。
# 策略：system 消息（人设/记忆/RAG 注入）全量保留，只从最老开始裁 user/assistant
# 原始聊天，最近若干轮与最后一条消息必留。被裁掉的久远细节由记忆系统按话题
# 检索补回（注入块本来就在 system 里），模型不需要硬读全部历史。


def _local_msg_tokens(m: dict) -> int:
    """粗估单条消息 token 数：CJK ≈0.7 token/字，ASCII ≈0.25 token/字符（按 Qwen 词表量级）。"""
    try:
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(str(p.get("text") or "") for p in c if isinstance(p, dict))
        c = str(c or "")
    except Exception:
        c = ""
    if not c:
        return 8
    cjk = sum(1 for ch in c if "\u4e00" <= ch <= "\u9fff")
    other = len(c) - cjk
    return int(cjk * 0.7 + other * 0.25) + 8


def _trim_messages_for_local(messages: list, budget_tokens: int = 16384) -> list:
    """把超预算的本地请求裁到预算内；不超预算原样返回（零开销）。绝不裁 system。"""
    try:
        msgs = list(messages or [])
        if not msgs:
            return msgs
        total = sum(_local_msg_tokens(m) for m in msgs)
        if total <= budget_tokens:
            return messages

        sys_msgs = [m for m in msgs if m.get("role") == "system"]
        rest = [m for m in msgs if m.get("role") != "system"]
        sys_cost = sum(_local_msg_tokens(m) for m in sys_msgs)
        budget_left = budget_tokens - sys_cost
        if budget_left < 1500:
            # system（人设+记忆）本身就把预算吃光了：不动裁刀原样返回，
            # 让自然失败走既有回退链路（绝不裁人设/记忆）
            # ★ 2026-09-12：这里原来完全静默 —— 实测本地大脑每次都因为
            #   "request (44555 tokens) exceeds the available context size (16384)"
            #   回退云端，而日志里看不到任何裁剪线索，只能靠 Ollama 的 400 反推。
            #   现在把规模打出来：sys≈多少 token、总共多少，一眼看出差多远。
            print(f"[LocalBrain] 系统提示本身已占 ≈{sys_cost} token（总≈{total}），"
                  f"预算 {budget_tokens} 装不下 → 不裁剪，直接走云端回退", flush=True)
            return msgs

        # 从最新往回保留，最后一条消息（当前输入）必留
        kept_rev = []
        for m in reversed(rest):
            cost = _local_msg_tokens(m)
            if kept_rev and budget_left - cost < 0:
                break
            budget_left -= cost
            kept_rev.append(m)
        kept = list(reversed(kept_rev))

        dropped = len(rest) - len(kept)
        if dropped <= 0:
            return msgs
        marker = {"role": "system", "content":
                  f"（更早的 {dropped} 条聊天记录已省略；上面的长期记忆与摘要就是你对久远往事的印象，"
                  f"请自然地延续对话，不要提起「记录被省略」这件事。）"}
        print(f"[LocalBrain] 上下文裁剪：总≈{total} 超预算 {budget_tokens}，"
              f"保留 system({sys_cost}) + 最近 {len(kept)} 条，裁掉最老 {dropped} 条", flush=True)
        return sys_msgs + [marker] + kept
    except Exception as e:
        print(f"[LocalBrain] 裁剪异常(原样放行): {e}", flush=True)
        return messages


async def _strip_inline_think(gen):
    """包一层云端回退流：把 content 里内联的 <think>…</think> 剥成 reasoning_content。
    本地大脑开了思考模式后回退云端，用户预期一致地看到「思考→回答」；
    云端模型不输出内联 <think> 时本包装是零开销直通。"""
    splitter = _ThinkSplitter()
    async for kind, val in gen:
        if kind != "line" or not isinstance(val, str) or not val.startswith("data:"):
            yield kind, val
            continue
        try:
            j = json.loads(val[5:].strip())
        except Exception:
            yield kind, val
            continue
        if j.get("type") or j.get("_meta") or j.get("error"):
            yield kind, val   # notice/_meta/error 原样透传
            continue
        try:
            c0 = (j.get("choices") or [{}])[0]
            delta = c0.get("delta") or {}
            c = delta.get("content") or ""
            if not c:
                yield kind, val
                continue
            r, c2 = splitter.feed(c)
            if r:
                ln = _local_sse(reasoning=r, model=j.get("model", ""))
                if ln:
                    yield "line", ln
            if c2:
                ln = _local_sse(content=c2, finish=c0.get("finish_reason"),
                                model=j.get("model", ""))
                if ln:
                    yield "line", ln
        except Exception:
            yield kind, val
    rest = splitter.flush()
    if rest:
        ln = _local_sse(reasoning=rest if splitter.in_think else "",
                        content="" if splitter.in_think else rest)
        if ln:
            yield "line", ln


class _ThinkSplitter:
    """把内联 <think>...</think> 从 content 里增量剥离（标签被切在 chunk 边界也正确）。
    feed(piece) → (reasoning_piece, content_piece)。Ollama 新版思考在独立字段，本解析器
    只在 content 出现 <think> 时才真正起作用，纯文本零开销直通。"""

    _OPEN, _CLOSE = "<think>", "</think>"

    def __init__(self):
        self.in_think = False
        self.buf = ""

    def _partial_len(self, tag: str) -> int:
        """buf 尾部是否恰好是 tag 的前缀（标签可能被切在两个 chunk 之间）。"""
        for l in range(min(len(tag) - 1, len(self.buf)), 0, -1):
            if self.buf.endswith(tag[:l]):
                return l
        return 0

    def feed(self, piece: str):
        if not piece:
            return "", ""
        self.buf += piece
        out_r = out_c = ""
        while True:
            tag = self._CLOSE if self.in_think else self._OPEN
            i = self.buf.find(tag)
            if i >= 0:
                if self.in_think:
                    out_r += self.buf[:i]
                else:
                    out_c += self.buf[:i]
                self.buf = self.buf[i + len(tag):].lstrip("\n")
                self.in_think = not self.in_think
                continue
            keep = self._partial_len(tag)
            if keep:
                tail, self.buf = self.buf[-keep:], self.buf[:-keep]
            else:
                tail, self.buf = "", self.buf
            if self.in_think:
                out_r += self.buf
            else:
                out_c += self.buf
            self.buf = tail
            break
        return out_r, out_c

    def flush(self):
        """流结束：缓冲里剩余的全是未闭合区间的内容。"""
        rest, self.buf = self.buf, ""
        return rest


def _local_sse(content: str = "", reasoning: str = "", finish=None, model: str = ""):
    """本地 chunk → 与 _clean_delta_line 相同形状的 SSE 行；空内容返回 None。"""
    delta = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    if not delta and finish is None:
        return None
    obj = {
        "id": "local-brain", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(obj, ensure_ascii=False)


async def _stream_local(messages: list, *, temperature: float, top_p: float,
                        max_tokens: int):
    """Ollama OpenAI 兼容流式。输出与 stream_chat 完全相同的
    ('line', sse) / ('done', None) / ('meta', dict) 协议。
    首个数据块之前失败（连不上/非200）→ 抛 LocalBrainUnavailable，由 stream_chat 回退云端。"""
    cfg = _local_brain_cfg()
    base = str(cfg.get("baseUrl") or "http://127.0.0.1:11434").rstrip("/")
    url = base + "/v1/chat/completions"
    model_name = str(cfg.get("model") or "qwen3:4b")
    think = bool(cfg.get("think", True))
    max_tokens = _local_max_tokens(cfg, think, max_tokens)
    messages = _sanitize_messages(messages)
    # ★ 方案②：本地路由精简 system（白名单），开关 LOCAL_BRAIN.lite_prompt
    if bool(cfg.get("lite_prompt", True)):
        messages = _slim_system_for_local(messages)
    # ★ 本地专用上下文裁剪（只影响本地路由；人设/记忆全保，裁最老原始聊天）
    try:
        messages = _trim_messages_for_local(messages, int(cfg.get("ctx_budget_tokens") or 16384))
    except Exception:
        pass
    payload = {"model": model_name, "messages": messages, "stream": True,
               "temperature": temperature, "top_p": top_p, "max_tokens": max_tokens}
    if think:
        payload["think"] = True   # 新版 Ollama 认识；旧版忽略未知字段，无害
    client = get_http_client()
    _llm_count_call("stream_local")
    _t0 = time.time()
    _t_first = [0.0]
    splitter = _ThinkSplitter()
    saw_done = False
    saw_any = False
    finish_reason = None
    try:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise LocalBrainUnavailable("HTTP %s: %s" % (resp.status_code, text[:200]))
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip("\r")
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        saw_done = True
                        yield ("done", None)
                        continue
                    try:
                        j = json.loads(body)
                    except Exception:
                        continue
                    c0 = (j.get("choices") or [{}])[0]
                    fin = c0.get("finish_reason")
                    if fin:
                        finish_reason = fin
                    delta = c0.get("delta") or c0.get("message") or {}
                    r = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    c = delta.get("content") or ""
                    if c:
                        # 兼容内联 <think> 形态；reasoning 字段已有内容时以字段为准
                        r2, c = splitter.feed(c)
                        r = r or r2
                    if r:
                        saw_any = True
                        ln = _local_sse(reasoning=r, model=model_name)
                        if ln:
                            yield ("line", ln)
                    if c:
                        saw_any = True
                        if not _t_first[0]:
                            _t_first[0] = time.time() - _t0
                        ln = _local_sse(content=c, finish=fin, model=model_name)
                        if ln:
                            yield ("line", ln)
                    elif fin:
                        ln = _local_sse(finish=fin, model=model_name)
                        if ln:
                            yield ("line", ln)
            # 流收尾：缓冲可能还压着未闭合的思考/正文
            if splitter.buf:
                rest = splitter.flush()
                ln = _local_sse(reasoning=rest if splitter.in_think else "",
                                content="" if splitter.in_think else rest,
                                model=model_name)
                if ln:
                    yield ("line", ln)
    except httpx.TransportError as e:
        raise LocalBrainUnavailable("连不上本地 Ollama（%s）" % e)
    print(f"[LLM] stream_local {model_name} 首字={_t_first[0]:.2f}s 总={time.time() - _t0:.2f}s",
          flush=True)
    yield ("meta", {"_meta": True, "sawDone": saw_done, "sawAnyData": saw_any,
                    "finish_reason": finish_reason})


async def _chat_once_local(messages: list, *, temperature: float, max_tokens: int,
                           frequency_penalty=None, presence_penalty=None) -> str:
    """本地 Ollama 非流式调用；不可用时自动回退云端 chat_once 并返回云端内容。"""
    cfg = _local_brain_cfg()
    base = str(cfg.get("baseUrl") or "http://127.0.0.1:11434").rstrip("/")
    url = base + "/v1/chat/completions"
    model_name = str(cfg.get("model") or "qwen3:4b")
    think = bool(cfg.get("think", True))
    max_tokens = _local_max_tokens(cfg, think, max_tokens)
    messages = _sanitize_messages(messages)
    # ★ 方案②：本地路由精简 system（白名单），开关 LOCAL_BRAIN.lite_prompt
    if bool(cfg.get("lite_prompt", True)):
        messages = _slim_system_for_local(messages)
    # ★ 本地专用上下文裁剪（只影响本地路由）
    try:
        messages = _trim_messages_for_local(messages, int(cfg.get("ctx_budget_tokens") or 16384))
    except Exception:
        pass
    payload = {"model": model_name, "messages": messages, "stream": False,
               "temperature": temperature, "max_tokens": max_tokens}
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty
    if presence_penalty is not None:
        payload["presence_penalty"] = presence_penalty
    if think:
        payload["think"] = True
    client = get_http_client()
    _llm_count_call("chat_once_local")
    _t0 = time.time()
    try:
        resp = await client.post(url, json=payload)
    except httpx.TransportError as e:
        fb, k, b = _local_fallback_target("连不上本地 Ollama（%s）" % e)
        _out = await chat_once(fb, messages, k, base_url=b,
                               temperature=temperature, max_tokens=max_tokens)
        # 回退结果同样剥内联思考（本地思考开着时保持体验一致）
        return re.sub(r"<think>.*?(?:</think>|$)", "", _out or "", flags=re.S).lstrip("\n")
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        fb, k, b = _local_fallback_target("HTTP %s: %s" % (resp.status_code, str(detail)[:120]))
        return await chat_once(fb, messages, k, base_url=b,
                               temperature=temperature, max_tokens=max_tokens)
    j = resp.json()
    msg = (j.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    if "<think>" in content:
        # 非流式返回里内联的思考块整段剥掉（含未闭合的残尾）
        content = re.sub(r"<think>.*?(?:</think>|$)", "", content, flags=re.S).lstrip("\n")
    try:
        from . import api_hunger as _ah
        _ah.note_success()   # 本地主链路成功同样给「她饿了」误报计数清零
    except Exception:
        pass
    print(f"[LLM] chat_once(local:{model_name}) {time.time() - _t0:.2f}s", flush=True)
    return content


class ModelApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _parse_stream_usage(payload: str):
    """从单条 SSE data 里取 usage（仅在 stream_options.include_usage 时上游才给）。

    返回 (input_tokens, output_tokens)；没有 usage 字段时返回 None（与「全 0」区分开，
    避免把"上游没报"记成"消耗为 0"）。
    """
    try:
        j = json.loads(payload)
    except Exception:
        return None
    u = j.get("usage")
    if not isinstance(u, dict) or not u:
        return None
    try:
        from .token_tracker import parse_usage
        return parse_usage(u)
    except Exception:
        return None


def _estimate_args_tokens(messages, content: str) -> tuple:
    """上游未报 usage 时的估算回退：输入按 messages 估，输出按正文字数估。"""
    try:
        from .token_tracker import estimate_messages_tokens as _emt, _estimate_tokens as _et
        return _emt(messages or []), _et(content or "")
    except Exception:
        return 0, 0


def _log_and_record_llm(where: str, model: str, elapsed: float, inp, outp):
    """统一出口：打一条带 token 数的 [LLM] 行 + 记账。

    inp/outp 为上游返回的真实 usage；为 None 表示上游没报（调用方已用估算值兜底）。
    任何失败都静默——记账绝不能影响主链路。
    输出格式固定，便于事后 grep 统计：
      [LLM] chat_once(mod.func) model 1.23s in=1234 out=56 total=1290
    """
    _exact = inp is not None or outp is not None
    _in, _out = int(inp or 0), int(outp or 0)
    try:
        print("[LLM] %s %s %.2fs in=%d out=%d total=%d%s"
              % (where or "?", model, elapsed, _in, _out, _in + _out, "" if _exact else " (est)"),
              flush=True)
    except Exception:
        pass
    try:
        from . import token_tracker as _tt
        if _in or _out:
            _tt.record_usage(_in, _out, model)
            _tt.record_caller(where or "?", _in, _out)
    except Exception:
        pass
    return _in, _out


def _clean_delta_line(payload: str):
    """解析 SSE data，分离 content（最终回答）和 reasoning_content（思考过程）。
    返回 (content_data_or_None, reasoning_data_or_None, finish_reason)。
    reasoning 用 delta.type="thinking" 标记，前端 chat_stream.js 识别后显示「已思考 N 秒」。
    """
    try:
        j = json.loads(payload)
    except Exception:
        return None, None, None
    c0 = (j.get("choices") or [{}])[0]
    finish = c0.get("finish_reason")
    delta = c0.get("delta") or {}
    content = delta.get("content")
    reasoning = delta.get("reasoning_content")

    base = {
        "id": j.get("id", ""),
        "object": "chat.completion.chunk",
        "created": j.get("created", 0),
        "model": j.get("model", ""),
    }

    content_line = None
    if content:
        cleaned = dict(base)
        cleaned["choices"] = [{
            "index": 0,
            "delta": {"content": content},
            "finish_reason": finish,
        }]
        content_line = "data: " + json.dumps(cleaned, ensure_ascii=False)

    reasoning_line = None
    if reasoning:
        cleaned = dict(base)
        cleaned["choices"] = [{
            "index": 0,
            "delta": {"reasoning_content": reasoning},
            "finish_reason": None,
        }]
        reasoning_line = "data: " + json.dumps(cleaned, ensure_ascii=False)

    if not content_line and not reasoning_line and finish is None:
        return None, None, finish
    return content_line, reasoning_line, finish


def _sanitize_messages(messages: list) -> list:
    """★ 出站消息清洗（2026-09-09 v2）：剔除损坏/不可达的图片 part。
    实测两种 400 根因（Gemini 中转把图转成 inline_data）：
    1. base64 为空/本地路径的图 → inline_data 空 → 400；
    2. http(s) 图链（本机 127.0.0.1 / QQ 图床过期签名）→ 中转下载失败 → 空 data → 400。
    聊天历史里图片本来就被换成文本占位，生成层只对「本轮 base64 图」有意义
    （视觉理解走独立的 vision_analyzer 通道，不走这里）。
    → 只保留有效 base64 data URL（中转无需网络下载）；其余图片一律剔除，
      图片全坏时退化为 [图片] 占位文本，保住消息角色序列。"""
    try:
        out = []
        removed = 0
        for m in messages or []:
            if isinstance(m, dict) and "image" in m:
                # ★ 剔除消息顶层的非标准 image 字段：主聊天的图片理解走视觉分支
                #   （图片描述文字进上下文），生成层 messages 带着顶层 image 会被
                #   Gemini 中转转成空 data part → 400。chat_logic/main 在更早阶段
                #   已消费过该字段，此处移除不影响任何功能。
                m = {k: v for k, v in m.items() if k != "image"}
            if not isinstance(m, dict) or not isinstance(m.get("content"), list):
                out.append(m)
                continue
            parts = []
            for part in m["content"]:
                # ★ 容错粒度 = 单个 part：任何意外结构只损失该 part，绝不整体放弃清洗
                #   （v1 的整体 try/except 曾因前端 image_url 为字符串格式抛 AttributeError
                #    → 整个清洗静默失效 → 坏图原样透传 → 400 照旧）
                try:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        iu = part.get("image_url")
                        u = ""
                        if isinstance(iu, dict):
                            u = str(iu.get("url") or "")
                        elif isinstance(iu, str):
                            u = iu    # 兼容 image_url 直接是字符串的写法
                        ok = False
                        if u.startswith("data:"):
                            b64 = u.split(",", 1)[1] if "," in u else ""
                            ok = len(b64) >= 100    # 有效 base64；空/截断的剔除
                        # http(s) 图链一律剔除：中转下载不可控（内网地址/过期签名 → 400）
                        if ok:
                            parts.append(part)
                        else:
                            removed += 1
                        continue
                    parts.append(part)
                except Exception:
                    removed += 1
                    continue
            if not parts:
                parts = [{"type": "text", "text": "[图片]"}]
            out.append({**m, "content": parts})
        if removed:
            print(f"[Sanitize] 已剔除 {removed} 个坏图 part（防 Gemini 中转 400）", flush=True)
            print(f"[Sanitize] 清洗后结构: {_messages_shape(out)}", flush=True)
        return out
    except Exception:
        return messages


def _align_model_key(model: str, api_key: str) -> str:
    """★ key-模型自动对齐（2026-09-09 治本）：
    后台十几处模块（行为分析/五维人格/记忆提炼/自主发言/承诺…）各自传 key 调
    不同模型——传错（如生成层 key 去调智谱）→ 401「令牌已过期」静默降级，
    逐调用点修不完。规则：模型池里该模型有明确 provider、且该 provider 的
    **专属 key 存在** 时强制对齐；自定义中转/Ollama/无专属 key 的场景原样放行
    （google_api_key 为空时不替换 gemini 的中转 key，保护正常链路）。"""
    try:
        if not model:
            return api_key
        from . import config
        cfg = (config.text_models() or {}).get(model) or {}
        provider = str(cfg.get("provider") or "").strip().lower()
        if not provider:
            return api_key
        right = ""
        if provider == "zhipu":
            right = config.zhipu_api_key() or ""
        elif provider == "deepseek":
            # ★ 2026-09-12 补：这里原来**整个漏掉了 deepseek** —— 而 deepseek 恰好是
            #   全项目用得最多的 provider（记忆提炼/理解层/后台分析全是 deepseek-chat）。
            #   后果：角色卡是智谱大脑（ai_provider=glm）时，理解层与各后台模块会把
            #   「生成层的智谱 key」（0d6afc24ce...agX1）传进来调 deepseek-chat，
            #   这条 if-chain 没有 deepseek 分支 → right 保持空 → 原样返回智谱 key
            #   → api.deepseek.com 收到智谱 key → 401「Authentication Fails,
            #   Your api key: ****agX1 is invalid」→ 理解层静默降级（用户误判成"key 又失效"）。
            #   实测：两个 key **各自都有效**（各自请求各自 200），只有交叉使用才 401。
            #   这里只纠正"形似串台"的情况：DeepSeek 的 key 一律以 sk- 开头，
            #   凡是非 sk- 的 key（智谱是 <32hex>.<16>）或空 key 才替换，
            #   避免把用户自定义中转的 key 覆盖掉。
            _dk = config.api_key() or ""
            if _dk and not str(api_key or "").strip().startswith("sk-"):
                right = _dk
        elif provider in ("anthropic", "claude"):
            right = config.claude_api_key() or ""
        elif provider == "google":
            right = config.google_api_key() or ""
        elif provider == "openai":
            right = config.openai_api_key() or ""
        elif provider == "xai":
            right = config.xai_api_key() or ""
        if right and right != api_key:
            return right
        return api_key
    except Exception:
        return api_key


def _messages_shape(messages: list) -> str:
    """消息结构的紧凑摘要（诊断 400 用）：role + content 形态 + 每个 part 的类型。"""
    try:
        parts = []
        for m in messages or []:
            if not isinstance(m, dict):
                parts.append(type(m).__name__)
                continue
            extra = "".join(f"+{k}" for k in m.keys()
                            if k not in ("role", "content", "name"))
            c = m.get("content")
            if isinstance(c, list):
                inner = "|".join(
                    (str(p.get("type")) if isinstance(p, dict) else type(p).__name__)
                    for p in c)
                parts.append(f"{m.get('role')}:[{inner}]{extra}")
            else:
                parts.append(f"{m.get('role')}:str({len(str(c or ''))}){extra}")
        return " || ".join(parts)
    except Exception:
        return "<shape dump failed>"


async def _stream_single(model: str, messages: list, api_key: str, *, base_url: str = "",
                         temperature: float = 0.7, top_p: float = 1.0, max_tokens: int = 2048,
                         reasoning_effort: str = None):
    """
    流式聊天。异步生成器：yield ('line', sse_line) 或 ('done',) 或最后 ('meta', meta_dict)。
    上游错误抛 ModelApiError。

    ★ 2026-09-15：本函数是"真正发请求"的那一层，**不要直接调用**——
      对外入口是下面的 stream_chat 守卫层（首包看门狗 + provider 熔断 + 兜底重放）。

    reasoning_effort 为可选（仅智谱 GLM-5.3 系列支持 low/high/max）：仅在显式传入时加入 payload，
    让聊天生成层也能「先想再答」；DeepSeek 等不支持该参数的模型，调用方不要传。
    """
    # ★ 本地大脑路由（2026-09-11）：provider=local → Ollama OpenAI 兼容接口。
    #   先探首个数据块：连接层失败（服务没起/模型没装）在产出一个块之前就会抛出，
    #   此时自动切回云端模型并发 notice 提示前端——对话不中断，一键切回的兜底半边。
    if _is_local_model(model):
        _gen = _stream_local(messages, temperature=temperature, top_p=top_p,
                             max_tokens=max_tokens)
        try:
            _first = await _gen.__anext__()
        except StopAsyncIteration:
            return
        except LocalBrainUnavailable as _e:
            _fb_model, _fb_key, _fb_base = _local_fallback_target(str(_e))
            yield ("line", _local_notice_sse("本地大脑没响应，这条已临时切回云端", str(_e)))
            # 回退流也包一层 <think> 剥离：本地思考开着时，回退云端体验保持一致
            async for _k, _v in _strip_inline_think(
                stream_chat(_fb_model, messages, _fb_key, base_url=_fb_base,
                            temperature=temperature, top_p=top_p,
                            max_tokens=max_tokens, reasoning_effort=reasoning_effort)
            ):
                yield _k, _v
            return
        try:
            yield _first
            async for _k, _v in _gen:
                yield _k, _v
        finally:
            await _gen.aclose()
        return

    url = _chat_completions_url(
        base_url,
        DEEPSEEK_BASE + "/chat/completions",
        model,
    )
    messages = _sanitize_messages(messages)   # ★ 剔除坏图 part（防 Gemini 中转 400）
    api_key = _align_model_key(model, api_key)   # ★ key-模型对齐（治本 401）
    # ★ 推理模型放大 token 额度，避免思考被截断（回复过快/深度不足）
    if _is_reasoner_model(model) and max_tokens < _REASONER_MIN_TOKENS:
        max_tokens = _REASONER_MIN_TOKENS
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # ★ 2026-09-14：让上游在流末尾补一个带 usage 的块（OpenAI 兼容标准参数）。
        #   不加这个，流式调用拿不到任何 token 数 → 主力入口永远无法记账。
        #   若某家中转不认这个参数会返回 400，_stream_retry_without_usage 会自动去掉重试。
        "stream_options": {"include_usage": True},
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    saw_done = False
    saw_any_data = False
    finish_reason = None
    _stream_usage = [None, None]   # ★ 2026-09-14：流式 usage（上游末块给）
    _acc_len = [0]                 # ★ 正文字数累计，供"上游没报 usage"时估算输出侧
    client = get_http_client()
    _llm_count_call("stream_chat")
    _t_llm = time.time()
    _t_llm_first = [0.0]
    async with client.stream("POST", url, json=payload,
                             headers={"Authorization": "Bearer " + api_key}) as resp:
        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "replace")
            try:
                detail = json.loads(text).get("error", {}).get("message", text)
            except Exception:
                detail = text
            # ★ 2026-09-14 兼容兜底：部分中转/老版本不认 stream_options → 400 且报
            #   "stream_options"/"unknown"/"unsupported"。此时去掉该参数重试一次，
            #   绝不因为"想记账"而让流式对话整条失败（只是这次拿不到 usage）。
            _so_bad = ("stream_options" in str(detail).lower()
                       or ("unknown" in str(detail).lower() and "param" in str(detail).lower())
                       or ("unsupported" in str(detail).lower()))
            if resp.status_code == 400 and _so_bad and "stream_options" in payload:
                _log_err(f"stream_chat 上游不认 stream_options，去掉后重试：{detail[:160]}")
                payload.pop("stream_options", None)
                # 不报 _hunger：这不是模型/额度问题，只是参数兼容性
                async for _k, _v in stream_chat(
                    model, messages, api_key, base_url=base_url,
                    temperature=temperature, top_p=top_p, max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                ):
                    yield _k, _v
                return
            _log_err(f"stream_chat 模型返回错误 HTTP {resp.status_code}: {detail[:300]}")
            if resp.status_code == 400:
                # ★ 400 时打印请求结构（诊断中转转换问题：哪个 part 变成了坏 data）
                print(f"[AI-ERR] 400 请求结构: {_messages_shape(messages)}", flush=True)
            _hunger(resp.status_code, detail, model)
            raise ModelApiError(resp.status_code, "模型返回错误（%s）：%s" % (resp.status_code, detail))
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip("\r")
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    saw_done = True
                    yield ("done", None)
                    continue
                # ★ usage 只在末块出现，且该块的 choices 通常为空 →
                #   必须在 _clean_delta_line 之前单独取，否则会被丢掉。
                _u = _parse_stream_usage(body)
                if _u:
                    _stream_usage[0], _stream_usage[1] = _u
                content_line, reasoning_line, finish = _clean_delta_line(body)
                if finish:
                    finish_reason = finish
                if reasoning_line:
                    saw_any_data = True
                    yield ("line", reasoning_line)
                if content_line:
                    saw_any_data = True
                    if not _t_llm_first[0]:
                        _t_llm_first[0] = time.time() - _t_llm
                    # ★ 累积正文字数：上游若没回 usage，输出侧按这个估算（否则恒为 0）
                    try:
                        _acc_len[0] += len(
                            ((json.loads(content_line[5:]).get("choices") or [{}])[0]
                             .get("delta") or {}).get("content") or "")
                    except Exception:
                        pass
                    yield ("line", content_line)
        # 残留缓冲（最后一行可能缺换行）
        tail = buf.strip("\r").strip()
        if tail.startswith("data:"):
            body = tail[5:].strip()
            if body == "[DONE]":
                saw_done = True
                yield ("done", None)
            else:
                content_line, reasoning_line, finish = _clean_delta_line(body)
                if finish:
                    finish_reason = finish
                if reasoning_line:
                    saw_any_data = True
                    yield ("line", reasoning_line)
                if content_line:
                    saw_any_data = True
                    if not _t_llm_first[0]:
                        _t_llm_first[0] = time.time() - _t_llm
                    # ★ 累积正文字数：上游若没回 usage，输出侧按这个估算（否则恒为 0）
                    try:
                        _acc_len[0] += len(
                            ((json.loads(content_line[5:]).get("choices") or [{}])[0]
                             .get("delta") or {}).get("content") or "")
                    except Exception:
                        pass
                    yield ("line", content_line)
    # ★ 流式调用耗时（2026-09-10）：首字延迟 + 总时长，通话延迟验收用。
    # ★ 2026-09-14：补 token 用量 + 记账。首字延迟与 token 数放同一行，
    #   既能看到"哪个调用慢"，也能看到"哪个调用贵"。
    _s_in, _s_out = _stream_usage[0], _stream_usage[1]
    if _s_in is None:
        # 上游没回 usage（未开 include_usage 或中转不支持）：估算兜底。
        # 输入侧按 messages 估；输出侧按**实际收到的正文字数**估（累积于 _acc_len）。
        _est_in, _ = _estimate_args_tokens(messages, "")
        try:
            from .token_tracker import _estimate_tokens as _et
            _est_out = _et("中" * _acc_len[0])   # 流式正文以中文为主，按中文字符估
        except Exception:
            _est_out = 0
        _s_in, _s_out = _est_in, _est_out
    try:
        print(f"[LLM] stream_chat {model} 首字={_t_llm_first[0]:.2f}s 总={time.time() - _t_llm:.2f}s "
              f"in={_s_in} out={_s_out} total={int(_s_in or 0) + int(_s_out or 0)}"
              f"{'' if _stream_usage[0] is not None else ' (est,无输入侧)'}", flush=True)
    except Exception:
        pass
    try:
        from . import token_tracker as _tt
        if _s_in or _s_out:
            _tt.record_usage(int(_s_in or 0), int(_s_out or 0), model)
            _tt.record_caller("stream_chat", int(_s_in or 0), int(_s_out or 0))
    except Exception:
        pass
    yield ("meta", {"_meta": True, "sawDone": saw_done, "sawAnyData": saw_any_data,
                    "finish_reason": finish_reason})


async def _iter_guarded(model: str, messages: list, api_key: str, **kw):
    """给 `_stream_single` 套一层「首包/中途卡死」看门狗（逐块计时）。

    ★ 2026-09-15 事故：DeepSeek 宕机时 `stream_chat deepseek-flash 总=903.30s out=0` ——
      上游 15 分钟不给一个字节，她没有报错、没有兜底、就那么静默着，用户只看到"她不说话"。
      现在：首包超过 LLM_STREAM_FIRST_TIMEOUT_SEC（默认 60s）就掐断，交给守卫层换兜底重放；
      已经在出字后的卡死用更宽松的 LLM_STREAM_STALL_TIMEOUT_SEC（默认 90s），且**不重放**
      （重放会把已经吐出去的话再吐一遍）。
    """
    from . import llm_guard as _lg
    _gen = _stream_single(model, messages, api_key, **kw)
    _it = _gen.__aiter__()
    _any = False
    while True:
        _tmo = _lg.stream_stall_timeout_sec() if _any else _lg.stream_first_timeout_sec()
        try:
            _kind, _payload = await asyncio.wait_for(_it.__anext__(), timeout=_tmo)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as _te:
            _lg.note_fail(model, _te)
            _log_err(f"stream_chat 上游 {_tmo:.0f}s 一个字节都没给（{'出字后卡死' if _any else '首包未到'}）"
                     f" → 掐断（模型 {model}）")
            try:
                await _gen.aclose()
            except Exception:
                pass
            _hunger(504, f"stream no-data {_tmo:.0f}s", model)
            raise ModelApiError(504, "上游 %.0f 秒无数据（首包看门狗）" % _tmo)
        if _kind == "line":
            _any = True
        yield _kind, _payload


async def stream_chat(model: str, messages: list, api_key: str, *, base_url: str = "",
                      temperature: float = 0.7, top_p: float = 1.0, max_tokens: int = 2048,
                      reasoning_effort: str = None, fallback_model="auto"):
    """流式聊天入口（**守卫层**）：首包看门狗 + provider 熔断 + 未出字即失败时换模型重放。

    为什么要有这一层（2026-09-15 事故）：回复主链路走的是流式。DeepSeek 宕机时
    `stream_chat deepseek-flash 总=903.30s out=0`：一个字节都没出，用户只看到"她不说话"，
    日志里连一句错误都没有（因为它在等）。现在：
      · 任一数据块超过首包档（默认 60s）→ 掐断 → 记 provider 失败 → 用兜底模型整条重放
      · 已经出过字再失败 → 不重放（防重复文本），照旧抛 ModelApiError 让上层提示
      · provider 熔断中 → 直接走兜底，不再发网络请求

    fallback_model="auto"（默认）走 llm_guard 解析；"" 关闭重放；也可指定模型名。
    """
    from . import llm_guard as _lg
    _primary = str(model or "")
    _fb = (_lg.fallback_model_for(_primary) if fallback_model == "auto"
           else str(fallback_model or ""))
    if _lg.is_open(_primary):
        if not _fb:
            raise ModelApiError(503, "%s 已熔断（%s），暂无可用兜底模型"
                                % (_lg.provider_of(_primary), _lg.snapshot().get(_lg.provider_of(_primary))))
        _lg.note("回复链 %s 所属 %s 熔断中 → 直接改用 %s"
                 % (_primary, _lg.provider_of(_primary), _fb))
        _primary, _fb = _fb, ""

    _kw = dict(base_url=base_url, temperature=temperature, top_p=top_p,
               max_tokens=max_tokens, reasoning_effort=reasoning_effort)
    _started = False
    _err0 = ""
    try:
        async for _kind, _payload in _iter_guarded(_primary, messages, api_key, **_kw):
            if _kind == "line":
                _started = True
            yield _kind, _payload
        _lg.note_ok(_primary)
        return
    except Exception as _e:
        _lg.note_fail(_primary, _e)
        _err0 = "%s: %s" % (type(_e).__name__, _e)
        if _started or not _fb:
            raise

    # 未出字就失败 → 换兜底模型整条重放（换模型时丢掉调用方 base_url，按最终模型解析地址）
    _lg.note("回复链 %s 未出字即失败（%s）→ 改用 %s 重放" % (_primary, _err0, _fb))
    _kw2 = dict(_kw)
    _kw2["base_url"] = ""
    try:
        async for _kind2, _payload2 in _iter_guarded(_fb, messages, api_key, **_kw2):
            if _kind2 == "line":
                _started = True
            yield _kind2, _payload2
    except Exception as _e2:
        _lg.note_fail(_fb, _e2)
        raise ModelApiError(0, "回复链两条路都失败：%s → %s；兜底 %s → %s"
                            % (_primary, _err0, _fb, _e2))
    _lg.note_ok(_fb)
    _lg.note("回复链兜底成功：%s → %s" % (_primary, _fb))


async def chat_stream(model: str, messages: list, api_key: str, *, base_url: str = "",
                      temperature: float = 0.7, top_p: float = 1.0, max_tokens: int = 1024,
                      reasoning_effort: str = None):
    """
    兼容旧版接口：yield 纯文本 chunk（语音通话 voice_call 流式生成用）。
    底层复用 stream_chat，把 ('line', text) 协议转成纯文本迭代器。

    reasoning_effort：透传给 stream_chat（仅智谱 GLM-5.3 系列支持 low/high/max）。
    语音通话传 "low" 可以显著降低首字延迟（实测 3.5s → 0.6s）。
    """
    async for kind, payload in stream_chat(
        model, messages, api_key,
        base_url=base_url, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
    ):
        if kind == "line" and payload:
            # stream_chat 的 line 是给 HTTP SSE 转发层使用的完整
            # `data: {...}` 行；语音通话需要的则是纯文本。旧实现把整行
            # JSON 当作 AI 回答交给字幕和 TTS，导致界面显示 data:{...}
            # 且语音无法正常合成。
            raw = str(payload).strip()
            if raw.startswith("data:"):
                raw = raw[5:].strip()
            try:
                data = json.loads(raw)
                choice = (data.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield content
            except Exception:
                # 不把协议原文泄漏到通话字幕/TTS；无法解析时跳过该块。
                continue
        elif kind in ("done", "meta"):
            if kind == "done":
                continue
            break


# ── LLM 调用计数（通话延迟诊断用）────────────────────────────────
# ★ 2026-09-10：语音通话"回复慢"的主因是每句话前面串行跑了好几次辅助 LLM 调用
#   （语义分析 / 理解层 / 危机检测 …）。这里做个极轻量的全局计数，
#   让通话每回合能打出一行「上下文里用掉几次 LLM、花了多少秒」，
#   改造前后的效果可量化对比。计数本身无锁（CPython 下自增足够精确）。
_LLM_CALL_COUNT = [0]


def llm_call_mark() -> int:
    """取计数快照，配合 llm_calls_since() 统计一段区间内的 LLM 调用次数。"""
    return _LLM_CALL_COUNT[0]


def llm_calls_since(mark: int) -> int:
    try:
        return max(0, _LLM_CALL_COUNT[0] - int(mark or 0))
    except Exception:
        return 0


def _llm_count_call(where: str = "") -> None:
    _LLM_CALL_COUNT[0] += 1


def _llm_caller() -> str:
    """谁发起的这次调用（用于通话延迟归因：直接定位到具体模块/函数）。"""
    try:
        f = sys._getframe(2)
        mod = (f.f_globals.get("__name__") or "?").rsplit(".", 1)[-1]
        return f"{mod}.{f.f_code.co_name}"
    except Exception:
        return "?"


async def _chat_once_single(model: str, messages: list, api_key: str, *, base_url: str = "",
                            temperature: float = 0.3, max_tokens: int = 1024,
                            frequency_penalty: float = None, presence_penalty: float = None,
                            reasoning_effort: str = None,
                            _where: str = "", _diag: dict = None) -> str:
    """★ 真正发请求的那一层（**不要直接调用**——对外入口是下面的 chat_once 守卫）。
    非流式一次性调用（记忆提炼 / 主动发言内部生成 / 模型校验用）。只返回 content。

    frequency_penalty / presence_penalty 为可选（OpenAI 兼容接口支持，范围 -2.0~2.0）：
    仅在显式传入时加入 payload，用于多段回复抑制重复词/鼓励新信息点。

    reasoning_effort 为可选（仅智谱 GLM-5.3 系列支持 low/high/max）：
    仅在显式传入时加入 payload；DeepSeek 等模型不支持，调用方不要传。

    _where / _diag 仅供上层守卫使用：_where 保留原始调用点名字（日志格式不变），
    _diag 回填"200 + 空 body"签名（守卫据此判定 provider 是否病了）。
    """
    # ★ 本地大脑路由（2026-09-11）：provider=local → Ollama；不可用自动回退云端。
    if _is_local_model(model):
        return await _chat_once_local(
            messages, temperature=temperature, max_tokens=max_tokens,
            frequency_penalty=frequency_penalty, presence_penalty=presence_penalty)

    url = _chat_completions_url(
        base_url,
        DEEPSEEK_BASE + "/chat/completions",
        model,
    )
    messages = _sanitize_messages(messages)   # ★ 剔除坏图 part（防 Gemini 中转 400）
    api_key = _align_model_key(model, api_key)   # ★ key-模型对齐（治本 401）
    # ★ 推理模型放大 token 额度，避免思考被截断
    if _is_reasoner_model(model) and max_tokens < _REASONER_MIN_TOKENS:
        max_tokens = _REASONER_MIN_TOKENS
    payload = {"model": model, "messages": messages, "stream": False,
               "temperature": temperature, "max_tokens": max_tokens}
    if frequency_penalty is not None:
        payload["frequency_penalty"] = frequency_penalty
    if presence_penalty is not None:
        payload["presence_penalty"] = presence_penalty
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    client = get_http_client()
    _llm_count_call("chat_once")
    _llm_where = _where or _llm_caller()
    _t_llm = time.time()
    try:
        resp = await client.post(url, json=payload, headers={"Authorization": "Bearer " + api_key})
    except Exception as e:
        # ★ 网络层失败（连不上/超时）同样触发「她饿了」提醒
        _hunger("net", str(e))
        raise
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        _log_err(f"chat_once 模型返回错误 HTTP {resp.status_code}: {str(detail)[:300]}")
        if resp.status_code == 400:
            # ★ 400 时打印请求结构（诊断中转转换问题）
            print(f"[AI-ERR] 400 请求结构: {_messages_shape(messages)}", flush=True)
        _hunger(resp.status_code, detail, model)
        raise ModelApiError(resp.status_code, "模型返回错误（%s）：%s" % (resp.status_code, detail))
    j = resp.json()
    msg = (j.get("choices") or [{}])[0].get("message") or {}
    content = msg.get("content") or ""
    # ★ 2026-09-12 兜底重试：万一碰到 _is_reasoner_model 没认出来的"隐藏推理模型"，
    #   只要它确实思考了（有 reasoning_content）却没吐出正文，就用推理额度再要一次。
    #   这一层是为了让将来新增的模型不会再复现"静默空转"。
    if not content and str(msg.get("reasoning_content") or "").strip():
        _finish0 = (j.get("choices") or [{}])[0].get("finish_reason")
        _log_err(
            f"chat_once 模型 {model} 只输出思考没输出正文 "
            f"(finish_reason={_finish0}, 已给 {max_tokens} tokens) → 放大额度重试一次"
        )
        payload["max_tokens"] = max(int(_REASONER_MIN_TOKENS), int(max_tokens) * 4)
        try:
            resp2 = await client.post(
                url, json=payload, headers={"Authorization": "Bearer " + api_key}
            )
            if resp2.status_code == 200:
                j2 = resp2.json()
                msg2 = (j2.get("choices") or [{}])[0].get("message") or {}
                if (msg2.get("content") or "").strip():
                    j, msg = j2, msg2
                    content = msg.get("content") or ""
        except Exception as e:
            _log_err(f"chat_once 放大额度重试异常: {str(e)[:200]}")
    if content:
        try:
            from . import api_hunger as _ah
            _ah.note_success()   # ★ 主链路成功一次 → 饥饿误报计数清零
        except Exception:
            pass
    if not content:
        # ★ 关键诊断：content 为空时静默吞掉是最难排查的（不抛异常）。
        #   常见原因：Gemini/Claude 走中转 OpenAI 兼容接口时
        #   - finish_reason=length（max_tokens 不够）
        #   - finish_reason=safety/recitation（被过滤）
        #   - reasoning 模型只输出 reasoning_content，content 字段为 None
        # 打印 finish_reason + 整条 message 摘要，下次触发就能秒定位。
        # ★ 2026-09-12 修：这里原来写的是 `_logger.warning(...)`，但本模块**从来没有
        #   定义过 _logger** → NameError 被外层 `except Exception: pass` 吞掉，
        #   于是"最关键的空回复诊断"从来没往日志写过一行，害得这次排查绕了一大圈。
        #   改用 _log_err（print + flush），DEV_LOG 一定捕获得到。
        try:
            _finish = (j.get("choices") or [{}])[0].get("finish_reason")
            _usage = j.get("usage") or {}
            # ★ 2026-09-15：把「200 但空 body」的签名交给上层守卫。
            #   真机事故里 DeepSeek 就是这么挂的：HTTP 200 + finish_reason=None + usage={} +
            #   message={}，既不报错也不给内容——守卫靠这个签名判定 provider 病了并开熔断。
            if _diag is not None:
                try:
                    _diag["empty_sig"] = bool(_finish is None and not _usage and not str(
                        msg.get("reasoning_content") or "").strip())
                    _diag["finish_reason"] = _finish
                except Exception:
                    pass
            _log_err(
                f"chat_once 模型 {model} 返回空 content | "
                f"finish_reason={_finish} | usage={_usage} | "
                f"message={str(msg)[:400]}"
            )
        except Exception:
            pass
    # ★ 单次调用耗时（2026-09-10）：通话延迟排障要能一眼看出"是哪个调用在吃时间"。
    #   以前只有 httpx 的 INFO 行（没有耗时），定位 5 秒级的慢调用全靠猜。
    # ★ 2026-09-14：同一行补上真实 token 用量 + 记账（原先全项目只有 main.api_chat
    #   一处记账，主力入口 /api/chat/stream 与十余个后台抽取器完全没有账）。
    _u_in, _u_out = _parse_stream_usage(json.dumps(j, ensure_ascii=False)) or (None, None)
    if _u_in is None:
        _u_in, _u_out = _estimate_args_tokens(messages, content)
    _log_and_record_llm("chat_once(%s)" % _llm_where, model, time.time() - _t_llm, _u_in, _u_out)
    return content


async def chat_once(model: str, messages: list, api_key: str, *, base_url: str = "",
                    temperature: float = 0.3, max_tokens: int = 1024,
                    frequency_penalty: float = None, presence_penalty: float = None,
                    reasoning_effort: str = None,
                    fallback_model="auto", hard_timeout: float = None) -> str:
    """非流式调用入口（**守卫层**）：硬超时 + provider 熔断 + 失败自动降级。

    为什么必须要有这一层（2026-09-15 事故，真机日志行 143293-144455）：
      全项目二十多个后台抽取器（记忆/画像/情绪/关系/开环/AI状态/行为/人格/知识图谱/
      日周月总结/承诺/反思…）都汇到本入口。DeepSeek 宕机时它们单次挂 **900 秒**、
      把 http_client 的 20 条共享连接占满 → PoolTimeout 级联到连 GLM 的调用都被拖死
      → `[OneBot] 生成失败` → 她只能答「（刚刚想事情卡住了，你再说一次～）」。
    现在：硬超时掐断 → 记一次 provider 失败 → 自动用「当前活跃角色的大脑」重试一次
      （llm_guard.fallback_model_for）→ 连续 3 次失败熔断该 provider 5 分钟，
      熔断期内直接换兜底（不再发网络请求、不再占连接池）。

    参数：
      fallback_model: "auto"（默认，走 llm_guard 解析）/ ""（关闭降级）/ 指定模型名
      hard_timeout:   None（默认，按 llm_guard.timeout_for 分档）/ 具体秒数
    返回值/异常语义与改动前完全一致（失败照样抛，空 content 照样返回 ""），
    只是"挂死"被换成了"快速失败 + 尽量换一条活路"。
    """
    from . import llm_guard as _lg

    _primary = str(model or "")
    _where = _llm_caller()          # 在守卫层取，日志里仍是真正的调用点名字
    _tmo = _lg.timeout_for(_primary, caller=_where, explicit=hard_timeout)
    _fb = (_lg.fallback_model_for(_primary) if fallback_model == "auto"
           else str(fallback_model or ""))

    async def _try(_m: str, _use_caller_base: bool):
        """返回 (content, 是否 provider 侧故障)；异常照常向上抛。"""
        _diag = {}
        _out = await asyncio.wait_for(
            _chat_once_single(
                _m, messages, api_key,
                base_url=(base_url if _use_caller_base else ""),
                temperature=temperature, max_tokens=max_tokens,
                frequency_penalty=frequency_penalty, presence_penalty=presence_penalty,
                reasoning_effort=reasoning_effort, _where=_where, _diag=_diag),
            timeout=_tmo)
        if _diag.get("empty_sig"):
            _lg.note_fail(_m, _lg.FAIL_EMPTY)
            return _out, True
        _lg.note_ok(_m)
        return _out, False

    # ① provider 已熔断：绝不发网络请求（这正是当初把连接池占满的动作）
    if _lg.is_open(_primary):
        if not _fb:
            raise ModelApiError(503, "%s 已熔断（%s），暂无可用兜底模型"
                                % (_lg.provider_of(_primary), _lg.snapshot().get(_lg.provider_of(_primary))))
        _lg.note(f"杂活 {_primary} 所属 {_lg.provider_of(_primary)} 熔断中 → 直接改用 {_fb}")
        _out, _sick = await _try(_fb, False)
        return _out

    # ② 正常调用
    try:
        _out, _sick = await _try(_primary, True)
        if not _sick:
            return _out
        _err = _lg.FAIL_EMPTY
    except Exception as _e:
        _lg.note_fail(_primary, _e)
        _err = _e

    if not _fb:
        if isinstance(_err, BaseException):
            raise _err
        raise ModelApiError(0, "上游返回空内容（%s）" % _primary)

    # ③ 降级重试一次（换模型时丢掉调用方给的 base_url，改由模型池按最终模型解析地址，
    #    否则会出现「拿 GLM 模型去打 DeepSeek 地址」）
    _lg.note("%s 失败（%s）→ 改用 %s 重试一次" % (_primary, _err, _fb))
    try:
        _out2, _sick2 = await _try(_fb, False)
    except Exception as _e2:
        _lg.note_fail(_fb, _e2)
        raise
    if _sick2:
        _lg.note("%s 兜底也返回空内容" % _fb)
        return _out2
    _lg.note("兜底成功：%s → %s" % (_primary, _fb))
    return _out2


async def stream_vision(messages: list, vision_key: str, vision_model: str, *, base_url: str = None, temperature: float = 0.7):
    """
    图片消息 → 视觉模型（兼容 OpenAI 格式，支持通义千问/硅基流动/自定义）。
    base_url 为空时使用默认 DASHSCOPE_URL。
    """
    url = _chat_completions_url(
        base_url,
        DASHSCOPE_URL
    )
    payload_msgs = []
    for m in messages:
        if m.get("image"):
            payload_msgs.append({
                "role": m["role"],
                "content": [
                    {"type": "text", "text": m.get("content") or "请描述这张图片并回复我"},
                    {"type": "image_url", "image_url": {"url": m["image"]}},
                ],
            })
        else:
            payload_msgs.append({"role": m["role"], "content": m.get("content") or ""})
    payload = {"model": vision_model, "messages": payload_msgs, "stream": True,
               "temperature": temperature}
    saw_done = False
    saw_any_data = False
    finish_reason = None
    client = get_http_client()
    async with client.stream("POST", url, json=payload,
                             headers={"Authorization": "Bearer " + vision_key}) as resp:
        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "replace")
            try:
                detail = json.loads(text).get("error", {}).get("message", text)
            except Exception:
                detail = text
            _log_err(f"stream_vision 模型返回错误 HTTP {resp.status_code}: {detail[:300]}")
            raise ModelApiError(resp.status_code, "模型返回错误（%s）：%s" % (resp.status_code, detail))
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip("\r")
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    saw_done = True
                    yield ("done", None)
                    continue
                try:
                    j = json.loads(body)
                    finish_reason = ((j.get("choices") or [{}])[0].get("finish_reason")) or finish_reason
                    if ((j.get("choices") or [{}])[0].get("delta") or {}).get("content"):
                        saw_any_data = True
                except Exception:
                    pass
                yield ("line", "data: " + body)
        tail = buf.strip("\r").strip()
        if tail.startswith("data:"):
            yield ("line", "data: " + tail[5:].strip())
    yield ("meta", {"_meta": True, "sawDone": saw_done, "sawAnyData": saw_any_data,
                    "finish_reason": finish_reason})


# ==================== 硅基流动视觉模型（AI眼睛）====================
SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"


async def stream_vision_silicon(messages: list, api_key: str, model: str, *, temperature: float = 0.7):
    """
    图片消息 → 硅基流动视觉模型（如 deepseek-ai/DeepSeek-OCR）。
    兼容 OpenAI 格式，支持 image_url。
    """
    payload_msgs = []
    for m in messages:
        if m.get("image"):
            payload_msgs.append({
                "role": m["role"],
                "content": [
                    {"type": "text", "text": m.get("content") or "请描述这张图片并回复我"},
                    {"type": "image_url", "image_url": {"url": m["image"]}},
                ],
            })
        else:
            payload_msgs.append({"role": m["role"], "content": m.get("content") or ""})
    payload = {"model": model, "messages": payload_msgs, "stream": True,
               "temperature": temperature, "max_tokens": 2048}
    saw_done = False
    saw_any_data = False
    finish_reason = None
    client = get_http_client()
    async with client.stream("POST", SILICONFLOW_URL, json=payload,
                             headers={"Authorization": "Bearer " + api_key}) as resp:
        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "replace")
            try:
                detail = json.loads(text).get("error", {}).get("message", text)
            except Exception:
                detail = text
            _log_err(f"stream_vision_silicon 模型返回错误 HTTP {resp.status_code}: {detail[:300]}")
            raise ModelApiError(resp.status_code, "硅基流动视觉模型返回错误（%s）：%s" % (resp.status_code, detail))
        buf = ""
        async for chunk in resp.aiter_text():
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip("\r")
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    saw_done = True
                    yield ("done", None)
                    continue
                # _clean_delta_line 返回三元组 (content, reasoning, finish)，只取 content
                content_line, _reasoning_line, finish = _clean_delta_line(body)
                if finish:
                    finish_reason = finish
                if content_line:
                    saw_any_data = True
                    yield ("line", content_line)
        tail = buf.strip("\r").strip()
        if tail.startswith("data:"):
            content_line, _reasoning_line, finish = _clean_delta_line(tail[5:].strip())
            if finish:
                finish_reason = finish
            if content_line:
                saw_any_data = True
                yield ("line", content_line)
    yield ("meta", {"_meta": True, "sawDone": saw_done, "sawAnyData": saw_any_data,
                    "finish_reason": finish_reason})
