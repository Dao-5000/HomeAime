# -*- coding: utf-8 -*-
"""主动消息的统一质量守卫与跨前后端冷却。

★ 2026-09-14 用户拍板「禁止模板，发什么话由模型决定」后，本模块**不再改写任何文案**：
  · 删除了 BANNED_REPLACEMENTS（"在干嘛"→"这会儿手头忙不忙" 这类固定句替换）；
  · 删除了 _HOOK_PHRASES / _pick_hook（没问号就追加"，你呢？" 的固定钩子）。
  两者都是"程序说人话"，会让每条主动消息都带同一股模板味。
  现在这里只做：检测禁词（命中交给调用方**请模型重写**）、清格式、控长度、控问句数、
  统一冷却与相似度去重。
"""
import re
import time
from difflib import SequenceMatcher

from . import db

# 空话/查岗式表达：命中就请模型重写（不程序改写）
# ★ 「想你了」不在其列 —— 用户明确允许主动消息"直接表达思念"。
BANNED_WORDS = ("在干嘛", "吃了吗")
# 早晚安/节日/纪念日等问候场景里这几个词是正当的（allow_greetings=True 时放行）
GREETING_WORDS = ("早安", "晚安")


def banned_hits(text: str, allow_greetings: bool = False) -> list:
    """返回命中的禁词列表（不做任何改写）。"""
    s = str(text or "")
    words = list(BANNED_WORDS)
    if not allow_greetings:
        words += list(GREETING_WORDS)
    out = []
    for w in words:
        if w in s and w not in out:
            out.append(w)
    return out


def _trim_complete(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    last = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"), cut.rfind("\n"))
    return (cut[:last + 1] if last >= int(max_chars * 0.55) else cut).strip()


def sanitize_proactive_message(message: str, require_hook: bool = True,
                               max_chars: int = 100, replace_banned: bool = False,
                               allow_greetings: bool = False, reject_banned: bool = False,
                               session_id: str = "", character_id: str = "") -> str:
    """清理主动消息：去 think 块/引号/多余空白 → 控长度 → 统一问号 → 最多一个问句。

    参数说明（保持旧签名兼容）：
    · require_hook   —— 现在只表示"最多 1 个问号"；**不再追加任何固定钩子**。
    · replace_banned —— 已废弃（固定句替换是模板）。传 True 也只做检测。
    · allow_greetings—— 问候场景放行"早安/晚安"。
    · reject_banned  —— ★ 2026-09-14 用户口径（改 #2）：命中禁词**默认照样发**
      （真人不至于因为说了一句"在干嘛"就闭嘴）。传 True 才判不合格返回空串；
      调用方（生成侧）应先尝试请模型重写一次，重写不成再照发。
    """
    text = re.sub(r"<think>[\s\S]*?</think>", "", str(message or ""), flags=re.I)
    text = text.strip(' \t\r\n"“”')
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _trim_complete(text, max_chars)
    if not text:
        return ""
    text = re.sub(r"[?？]", "？", text)
    if require_hook:
        first = text.find("？")
        if first >= 0:
            # 只保留第一个问句，其余问号变句号（不再补固定钩子）
            text = text[:first + 1] + text[first + 1:].replace("？", "。")
    text = _trim_complete(text, max_chars).strip()
    if not text:
        return ""
    # 禁词：默认只检测不拦（用户口径：重写一次不成也照发）；reject_banned=True 才判不合格
    if reject_banned and banned_hits(text, allow_greetings=allow_greetings):
        return ""
    return text


def cooldown_key(session_id: str, character_id: str) -> str:
    return f"last_proactive_push:{session_id or 'default'}:{character_id or 'default'}"


def session_push_key(session_id: str) -> str:
    """会话级「上次主动发言」键（不含 character）。

    ★ 2026-09-14 新增（实测根因修复）：同一会话里不同链路记账用的 character_id 不一致 ——
      idle_agent 大量写 'default'（26 条里 25 条），前端 register / scheduler 写真实角色名。
      于是"上次主动发言时间"被拆成两把钥匙，各闸门只看自己那把：
      前端闸门读 :助手 → 看不到 idle 刚发的那条 → "距上次"被低估甚至为 0
      → 实测 0.8 / 1.4 / 2.3 / 3.0 / 6.3 / 8.4 分钟连发（26h 内 9 对 <10 分钟）。
      会话级键把三条链路绑到同一条时间线上：谁发过，别人都看得见。
    """
    return f"last_proactive_push:{session_id or 'default'}"


def push_keys(session_id: str, character_id: str) -> list:
    """记账要写的全部键 / 查询要一起看的全部键（具体 → default → 会话级）。"""
    keys = [cooldown_key(session_id, character_id)]
    default_key = cooldown_key(session_id, "default")
    if default_key not in keys:
        keys.append(default_key)
    sk = session_push_key(session_id)
    if sk not in keys:
        keys.append(sk)
    return keys


def last_push_time(session_id: str, character_id: str) -> float:
    """该会话**最近一次**主动发言的时间戳（跨 character_id 取最大）。

    所有节流闸门都该读它，而不是只读自己那把钥匙。
    """
    newest = 0.0
    for key in push_keys(session_id, character_id):
        try:
            v = float(db.kv_get(key) or 0)
        except (TypeError, ValueError):
            v = 0.0
        if v > newest:
            newest = v
    return newest


def cooldown_remaining(session_id: str, character_id: str, cooldown_seconds: int = 300) -> int:
    last = last_push_time(session_id, character_id)
    if last <= 0:
        return 0                      # 没有记录 = 没有冷却（不是"整窗口冷却"）
    return max(0, int(cooldown_seconds - (time.time() - last)))


def mark_sent(session_id: str, character_id: str, pushed_at=None):
    """记录一次主动发言：**三把钥匙一起写**，任何链路/任何 character 都看得到。"""
    ts = pushed_at or time.time()
    for key in push_keys(session_id, character_id):
        try:
            db.kv_set(key, ts)
        except Exception:
            pass


def is_similar_to_recent(session_id: str, character_id: str, message: str,
                         lookback: int = 8, threshold: float = 0.75) -> bool:
    normalized = re.sub(r"\s+", "", message or "")[:150]
    if not normalized:
        return True
    for row in reversed(db.recent_messages(session_id, lookback * 3, character_id)):
        extra = row.get("extra") or {}
        if not isinstance(extra, dict) or extra.get("source") != "proactive":
            continue
        old = re.sub(r"\s+", "", row.get("content") or "")[:150]
        if old and SequenceMatcher(None, normalized, old).ratio() >= threshold:
            return True
    return False
