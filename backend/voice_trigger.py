# -*- coding: utf-8 -*-
"""
语音触发判断（对齐「概率 + 场景 + 用户偏好」设计，替代"无条件发语音"）

三级判断：
  1. 用户偏好（持久化 kv）：用户说过"别发语音/发文字"→ 24h 内不发；说过"发语音"→ 提高概率
  2. 场景加成：深夜（22:00-02:00）提高概率；工作时间（9-18点）压低
  3. 基础概率（默认 35%）+ 随机掷骰

注意：用户「明确索要语音」（"发语音""想听你声音"）走 /api/voice/message 的
proactive=false 分支，无条件发，不经过本模块；本模块只管主动消息/离线回复等"AI 主动发"的场景。
"""
import json
import random
import time
from datetime import datetime

from . import db

BASE_PROB = 0.35          # 主动消息发语音基础概率
PREF_TTL = 24 * 3600      # 「想听语音」偏好有效期（秒）——临时兴致，一天后回落到概率模型
# ★ 2026-09-14：「不要语音/要文字」是**持续意图**，不设过期。
#   原实现两种模式共用 24h TTL，导致用户 9-02 说过「不要发语音」，
#   9-14 就静默失效（实测库里那条 mode=text 已过期 287 小时）→ 又发语音。
TEXT_PREF_NEVER_EXPIRES = True


def _pref_key(session_id, character_id):
    return f"voice_pref:{session_id}:{character_id}"


def set_voice_pref(session_id, character_id, mode):
    """记录用户语音偏好。mode: 'voice'（喜欢语音）/ 'text'（要文字）。"""
    if mode not in ("voice", "text"):
        return
    try:
        db.kv_set(_pref_key(session_id, character_id),
                  json.dumps({"mode": mode, "ts": time.time()}, ensure_ascii=False))
    except Exception as _e:
        print(f"[VoiceTrigger] set_voice_pref 失败(静默): {_e}", flush=True)


def _get_pref(session_id, character_id):
    try:
        raw = db.kv_get(_pref_key(session_id, character_id))
        if not raw:
            return None
        data = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(data, dict):
            return None
        # ★ 「要文字」是持续意图，不参与 TTL 过期（见文件头 TEXT_PREF_NEVER_EXPIRES）
        if TEXT_PREF_NEVER_EXPIRES and str(data.get("mode") or "") == "text":
            return data
        if time.time() - float(data.get("ts", 0) or 0) > PREF_TTL:
            return None
        return data
    except Exception:
        return None


def should_send_voice(session_id, character_id, base_prob=BASE_PROB):
    """判断本次主动消息/离线回复是否该发语音。返回 bool。

    优先级：用户偏好 > 场景（深夜/白天） > 基础概率。
    """
    # 1) 用户偏好（最高优先级）
    pref = _get_pref(session_id, character_id)
    if pref:
        mode = str(pref.get("mode") or "")
        if mode == "text":
            return False
        if mode == "voice":
            # 用户明确喜欢语音：固定较高概率，不受时间场景压制
            return random.random() < 0.70

    # 2) 场景：深夜提高、工作时间压低
    prob = float(base_prob)
    hour = datetime.now().hour
    if hour >= 22 or hour < 2:
        prob += 0.20
    elif 9 <= hour < 18:
        prob = min(0.30, prob)

    prob = max(0.0, min(0.90, prob))
    return random.random() < prob


def parse_voice_pref_command(text):
    """识别用户的语音偏好指令。返回 ('text'|'voice'|None)。

    只认明确指令，避免误伤普通聊天里的"语音"二字。
    """
    t = str(text or "").strip()
    if not t:
        return None
    # 要求发文字（别发语音/打字说/不方便听/不想听语音）
    if any(k in t for k in ("别发语音", "不要发语音", "别发声音", "不想听语音",
                            "不方便听", "不方便语音", "别语音", "不要语音",
                            "别念了", "别读了")):
        return "text"
    # ★ 补口语说法：用户实际说的是「宝你打字吧」这类，而原来的关键词只有
    #   "打字说/打字告诉我"，"你打字/打字吧"一个都匹配不上，指令等于没说。
    #   这里只收带语气/对象的组合词，不收单独的"打字"，避免误伤
    #   「我在打字」「打字好累」这类正常聊天。
    if any(k in t for k in ("发文字", "打字说", "打字告诉我", "用文字", "只发文字",
                            "你打字", "打字吧", "打字回", "文字就好", "文字就行",
                            "打个字", "用文字说")):
        return "text"
    # 要求发语音（发语音/想听你声音/说句话给我听）
    if any(k in t for k in ("发语音", "发条语音", "发声音", "想听你声音", "想听你的声音", "说句话给我听", "语音回我", "语音回复")):
        return "voice"
    return None
