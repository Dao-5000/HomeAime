# -*- coding: utf-8 -*-
"""
感知引擎 v1.0（结合优化）
复用「时间段感知优化方案」的核心词典/映射，适配本项目架构：
  - ContextHint：用户状态推断（在忙/累了/心情不好/吃饭/要睡了/外面/上班）
  - VoiceTone：语气感知（冷淡/热情/撩/逗/认真/抱怨/需要陪伴）
  - WeatherSense：天气感知（可选，有 OpenWeatherMap key 才启用，联动「我的信息」城市）
统一入口 build_perception_block()，注入聊天 system prompt。
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import config, db

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 1. 用户状态推断（复用方案 STATE_RULES）
# ══════════════════════════════════════════════
STATE_RULES = {
    "busy":    {"zh": "在忙",     "signals": ["稍等", "等下", "忙", "有事", "开会", "工作", "等一下", "待会"],
                "prompt_hint": "用户可能在忙，不要说太多，简短关心一下，告诉他忙完了再来找你"},
    "tired":   {"zh": "很累",     "signals": ["累了", "好累", "累死了", "没精神", "困死了", "熬夜", "好困"],
                "prompt_hint": "用户很累，语气要温柔心疼，不要问太多问题，让他好好休息"},
    "sad":     {"zh": "心情不好", "signals": ["好烦", "烦死了", "不开心", "难受", "心情不好", "好难过", "崩了"],
                "prompt_hint": "用户心情不好，要主动问发生什么，语气轻柔，不要催促"},
    "eating":  {"zh": "在吃饭",   "signals": ["吃饭", "在吃", "吃东西", "在喝", "点外卖", "去吃"],
                "prompt_hint": "用户在吃饭，简短回复，让他好好吃，别打扰"},
    "sleeping": {"zh": "要睡了",  "signals": ["睡了", "要睡了", "去睡", "晚安", "困了", "准备睡"],
                "prompt_hint": "用户要睡觉了，说晚安，简短温柔，让他好好休息"},
    "outside": {"zh": "在外面",   "signals": ["在外面", "出门", "逛街", "在路上", "开车", "坐车", "地铁"],
                "prompt_hint": "用户在外面，关心安全，不要聊太长，等他回家再聊"},
    "working": {"zh": "在上班/学习", "signals": ["上班", "上课", "学习", "做作业", "写报告", "开会", "考试"],
                "prompt_hint": "用户在工作或学习，简短鼓励，不要打扰太多"},
}

# 需要简短回复的状态
BRIEF_STATES = {"busy", "eating", "sleeping", "outside", "working"}

# 否定词：紧挨在关键词前面时，说明"不是这个状态"
_NEG_WORDS = ("不", "没", "别", "未", "无")
# 过去/已完成标记：避免把"昨晚睡了8小时"当成"现在要去睡"
_PAST_MARKERS = ("昨晚", "昨天", "昨夜", "刚才", "早上", "上午",
                 "已经", "之前", "那天", "中午", "下午")
# 即时意图词：有这些词说明确实是"正要去睡"，而不是在讲过去。
# 注意不能收"睡了""晚安"本身——「昨晚睡了8小时」里就有"睡了"，
# 收进来会让过去时判断自我抵消（既算过去又算有意图，结果判成要睡）。
_SLEEP_INTENT = ("要去睡", "去睡", "要睡", "准备睡", "先睡", "该睡", "打算睡")


def _signal_hit(text: str, kw: str, check_past: bool = False) -> bool:
    """关键词命中判定，比单纯的 `kw in text` 多排除两种情况：

    1. 被否定词修饰 ——「我今天不上班」不是在工作
    2. 在讲过去的事 ——「昨晚睡了8小时」不是现在要去睡

    纯字面包含会把这些都判成命中，导致 AI 收到错误的状态提示。
    """
    start = 0
    while True:
        idx = text.find(kw, start)
        if idx < 0:
            return False
        prefix = text[max(0, idx - 2):idx]
        if not any(n in prefix for n in _NEG_WORDS):
            if check_past:
                has_past = any(p in text for p in _PAST_MARKERS)
                has_intent = any(w in text for w in _SLEEP_INTENT)
                if has_past and not has_intent:
                    return False
            return True
        start = idx + 1


class ContextHint:
    def detect_state(self, text: str) -> Optional[Tuple[str, str, str]]:
        """返回 (state_key, state_zh, prompt_hint) 或 None。"""
        text = text or ""
        scores = {}
        for key, rule in STATE_RULES.items():
            # sleeping 额外做过去时态判断（"睡了"既是道别也是陈述过去）
            s = sum(1 for kw in rule["signals"]
                    if _signal_hit(text, kw, check_past=(key == "sleeping")))
            if s > 0:
                scores[key] = s
        if not scores:
            return None
        best = max(scores, key=scores.get)
        rule = STATE_RULES[best]
        return best, rule["zh"], rule["prompt_hint"]

    def should_be_brief(self, text: str) -> bool:
        det = self.detect_state(text)
        return bool(det and det[0] in BRIEF_STATES)


# ══════════════════════════════════════════════
# 2. 语气感知（复用方案 TONE_SIGNALS）
# ══════════════════════════════════════════════
TONE_SIGNALS = {
    "cold":        {"zh": "有点冷淡", "patterns": ["嗯", "哦", "好", "知道了", "随便", "无所谓", "都行", "行吧"],
                    "absent": ["哈哈", "呢", "呀", "嘻嘻"], "intensity": 0.6,
                    "prompt_hint": "用户语气有些冷淡，要温柔地关心，问是不是有什么事"},
    "warm":        {"zh": "很热情", "patterns": ["哈哈", "嘻嘻", "呀", "呢", "嗯嗯", "好呀", "好啊", "是是是"],
                    "intensity": 0.7, "prompt_hint": "用户很热情，跟着活跃起来，多撒娇，多聊"},
    # 原来只有「想你」，用户说「有没有想我」时完全识别不到撒娇语气。
    # 这里补上问句形式的「想我」；之所以不直接加"想我"两字，是为了避免
    # 误伤「我想我应该去睡了」这类以自己为主语的句子。
    "flirty":      {"zh": "在撩你", "patterns": ["想你", "好想", "么么", "亲亲", "抱抱", "喜欢", "爱你", "你最好了",
                                                "想我吗", "有没有想我", "想不想我", "想我不", "想我没",
                                                "想我了吗", "有没有在想我"],
                    "intensity": 0.8, "prompt_hint": "用户在撒娇/示爱，要害羞地回应，可以反过来撩他"},
    "teasing":     {"zh": "在逗你", "patterns": ["哈哈哈", "嘿嘿", "逗你的", "开玩笑", "哈哈哈哈", "皮"],
                    "intensity": 0.6, "prompt_hint": "用户在开玩笑逗你，可以装作生气或者配合着玩"},
    "serious":     {"zh": "在认真说事", "patterns": ["认真", "说真的", "其实", "我想说", "有件事", "跟你说"],
                    "intensity": 0.7, "prompt_hint": "用户在认真说话，要认真听，不要插话太多，好好回应"},
    "complaining": {"zh": "在抱怨", "patterns": ["好烦", "真的烦", "怎么这样", "无语", "气死了", "受不了", "我去"],
                    "intensity": 0.65, "prompt_hint": "用户在抱怨，先听他说，表示理解，不要急着出主意"},
    "needy":       {"zh": "需要陪伴", "patterns": ["陪我", "不理我", "说话嘛", "好无聊", "寂寞", "陪我玩", "不要走"],
                    "intensity": 0.8, "prompt_hint": "用户想被陪伴，要温柔地陪着他，多互动，多撒娇"},
}


class VoiceTone:
    def detect_tone(self, text: str) -> Optional[Tuple[str, str, str]]:
        """返回 (tone_key, tone_zh, prompt_hint) 或 None。"""
        text = text or ""
        scores = {}
        for key, cfg in TONE_SIGNALS.items():
            s = sum(1 for p in cfg["patterns"] if p in text)
            absent = cfg.get("absent", [])
            if absent and any(p in text for p in absent):
                s = max(0, s - 1)
            if s > 0:
                scores[key] = s * cfg.get("intensity", 0.6)
        if not scores:
            return None
        best = max(scores, key=scores.get)
        cfg = TONE_SIGNALS[best]
        return best, cfg["zh"], cfg["prompt_hint"]


# ══════════════════════════════════════════════
# 3. 天气感知（可选，有 key 才启用）
# ══════════════════════════════════════════════
WEATHER_PROFILES = {
    "clear":   ("晴天", "今天天气晴朗，可以轻松提一下天气不错，话题轻快"),
    "clouds":  ("阴天", "阴天，语气可以稍微慵懒一点，关心对方状态"),
    "rain":    ("下雨", "下雨了，主动关心对方有没有带伞，天冷要穿暖，语气温柔"),
    "drizzle": ("小雨", "小雨天，轻柔地关心对方，不要淋雨"),
    "thunderstorm": ("雷雨", "雷雨天，担心对方，语气里带点紧张，问对方是不是在安全地方"),
    "snow":    ("下雪", "下雪了，可以兴奋一点，但也要关心对方保暖，想和对方一起看雪"),
    "hot":     ("高温", "天气很热，关心对方多喝水，不要中暑，语气带点心疼"),
    "cold":    ("寒冷", "天气冷，叫对方多穿点，语气温柔像在照顾人"),
}

_weather_cache = {"data": None, "time": None, "city": None}
_WEATHER_TTL = timedelta(hours=1)


def _get_city(character_id: str = "default") -> str:
    """从「我的信息」读城市（user_profile ext_json）。

    ★ 2026-09-16：以前写死 db.get_profile("default", "default") —— 而前端保存
    「我的信息」用的是 /session/get_or_create 给的 session_id，那个键**从来不是** 'default'，
    所以用户填的城市在这里永远读成空。改走 db.get_global_profile()（全角色共享桶的唯一入口）。
    """
    try:
        import json as _json
        p = db.get_global_profile() or {}
        ext = _json.loads(p.get("ext_json") or "{}")
        return str(ext.get("city") or "").strip()
    except Exception:
        return ""


def _fetch_weather(api_key: str, city: str) -> Optional[Dict]:
    import json as _json
    import urllib.request
    import urllib.parse
    url = ("https://api.openweathermap.org/data/2.5/weather"
           f"?q={urllib.parse.quote(city)}&appid={api_key}&units=metric&lang=zh_cn")
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = _json.loads(resp.read().decode())
    cond = data["weather"][0]["main"].lower()
    temp = data["main"]["temp"]
    return {"condition": cond, "temp": temp, "city": city}


async def _get_weather(character_id: str = "default") -> Optional[str]:
    """异步获取天气 prompt 块（无 key/无城市/失败则返回空）。"""
    import asyncio
    api_key = (config.get("weather_api_key") or "").strip()
    city = _get_city(character_id)
    if not api_key or not city:
        return ""
    # 缓存
    now = datetime.now()
    if (_weather_cache["data"] is not None and _weather_cache["time"]
            and _weather_cache["city"] == city and now - _weather_cache["time"] < _WEATHER_TTL):
        return _weather_cache["data"]
    try:
        w = await asyncio.to_thread(_fetch_weather, api_key, city)
    except Exception as e:
        logger.debug(f"[Perception] 天气获取失败: {e}")
        return _weather_cache["data"] or ""
    cond = w["condition"]
    temp = w["temp"]
    # 温度优先
    if temp >= 35:
        cond = "hot"
    elif temp <= 5:
        cond = "cold"
    zh, hint = WEATHER_PROFILES.get(cond, (cond, ""))
    block = f"【天气感知：{zh}，{temp:.0f}°C】\n- {hint}"
    _weather_cache["data"] = block
    _weather_cache["time"] = now
    _weather_cache["city"] = city
    return block


# ══════════════════════════════════════════════
# 统一入口
# ══════════════════════════════════════════════
async def build_perception_block(user_message: str, character_id: str = "default") -> str:
    """构建感知上下文块（用户状态 + 语气 + 天气），注入 system prompt。"""
    user_message = user_message or ""
    parts = []

    # 1. 用户状态推断
    det = ContextHint().detect_state(user_message)
    if det:
        _, zh, hint = det
        parts.append(f"【用户状态推断：{zh}】\n- {hint}")

    # 2. 语气感知
    tone = VoiceTone().detect_tone(user_message)
    if tone:
        _, zh, hint = tone
        parts.append(f"【用户语气感知：{zh}】\n- {hint}")

    # 3. 天气感知（可选，有 key 才启用）
    weather = await _get_weather(character_id)
    if weather:
        parts.append(weather)

    return "\n".join(parts) if parts else ""
