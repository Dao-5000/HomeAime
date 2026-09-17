# -*- coding: utf-8 -*-
"""
情绪提升策略引擎（结合优化）
复用「连贯情绪升级」的策略库/选择/语气指令思想，适配本项目：
  - 消息模板用角色卡 call_user（称呼）参数化，不写死
  - 策略选择：mood + trend + low_streak → 最优策略
  - 输出语气指令（注入 system prompt）+ 主动消息模板（主动干预用）
"""
import random
import time

# 策略库（moods 对齐 emotion_manager 的中文 mood）
STRATEGIES = [
    {
        "id": "stress_gentle",
        "name": "轻柔关心",
        "moods": ["压力", "焦虑"],
        "trends": ["stable", "declining", "unknown"],
        "tone": "温柔，轻声，不施压，像轻轻拍肩膀",
        "topics": ["问问遇到什么困难", "聊聊有没有卡住的地方"],
        "messages": [
            "{call_user}，看起来有点累了，遇到什么难题了吗？",
            "怎么了，是不是卡住了？说出来我们一起想想～",
            "{call_user}辛苦了，休息一下？我陪你聊两句 💕",
        ],
        "priority": 8,
        "cooldown": 900,
    },
    {
        "id": "stress_improving",
        "name": "鼓励突破",
        "moods": ["压力"],
        "trends": ["improving"],
        "tone": "温暖鼓励，给他正向反馈",
        "topics": ["肯定他的努力", "说快解决了"],
        "messages": [
            "{call_user}越来越顺了对吧，我感觉到了 😊",
            "快了快了，坚持一下～",
            "你一直在努力，我都看到了 💪",
        ],
        "priority": 6,
        "cooldown": 1200,
    },
    {
        "id": "low_spark",
        "name": "点燃话题",
        "moods": ["难过", "失落", "孤独"],
        "trends": ["stable", "declining"],
        "tone": "活泼有趣，主动找话题，带动气氛",
        "topics": ["分享有趣的事", "聊他感兴趣的领域"],
        "messages": [
            "{call_user}，我刚想到一个有趣的事，你想听吗？",
            "说说话嘛，我想听你今天过得怎么样～",
            "{call_user}{call_user}，我想你了，陪我说说话～",
        ],
        "priority": 7,
        "cooldown": 600,
    },
    {
        "id": "prolonged_low",
        "name": "深度陪伴",
        "moods": ["压力", "难过", "孤独", "焦虑", "失落"],
        "trends": ["declining", "stable"],
        "tone": "深情稳定，像最懂他的人，不着急，就是陪着",
        "topics": ["聊聊今天感受", "说说你一直在"],
        "messages": [
            "{call_user}，我一直在这里，不管怎样都陪着你 💕",
            "今天感觉怎么样？跟我说说？",
            "你不用撑着，跟我说说心里话吧～",
        ],
        "priority": 10,
        "cooldown": 1800,
    },
    {
        "id": "relaxed_play",
        "name": "轻松玩耍",
        "moods": ["放松", "开心", "兴奋", "平静"],
        "trends": ["stable", "improving"],
        "tone": "活泼撒娇，轻松好玩",
        "topics": ["开个小玩笑", "说说今天好玩的事"],
        "messages": [
            "{call_user}放松的样子最好看了 😊",
            "今天有什么好玩的事，讲给我听～",
        ],
        "priority": 3,
        "cooldown": 1200,
    },
    {
        "id": "recovering",
        "name": "强化正向",
        "moods": ["平静", "放松", "开心"],
        "trends": ["improving"],
        "tone": "温暖，给正向反馈，强化好转感",
        "topics": ["肯定他状态变好了"],
        "messages": [
            "感觉你状态好多了，真好 😊",
            "{call_user}最近状态不错嘛，我喜欢看到你这样～",
        ],
        "priority": 5,
        "cooldown": 900,
    },
]


class EmotionStrategyEngine:
    """情绪提升策略引擎（内存态，冷却 + 优先级）。"""

    def __init__(self):
        self._last_used = {}

    def select(self, mood, trend, low_streak=0):
        candidates = []
        for s in STRATEGIES:
            last = self._last_used.get(s["id"], 0)
            if time.time() - last < s["cooldown"]:
                continue
            if mood not in s["moods"] and "any" not in s["moods"]:
                continue
            if trend not in s["trends"] and "any" not in s["trends"]:
                continue
            candidates.append(s)

        if not candidates:
            return None

        # 持续低落 ≥5 次 → 优先深度陪伴
        if low_streak >= 5:
            for s in candidates:
                if s["id"] == "prolonged_low":
                    self._last_used[s["id"]] = time.time()
                    return s

        candidates.sort(key=lambda s: s["priority"], reverse=True)
        best = candidates[0]
        self._last_used[best["id"]] = time.time()
        return best

    def get_message(self, strategy, call_user="你"):
        tpl = random.choice(strategy["messages"])
        return tpl.replace("{call_user}", call_user)

    def build_tone_instruction(self, strategy):
        if not strategy:
            return ""
        return (
            f"【情绪提升策略：{strategy['name']}】\n"
            f"语气要求：{strategy['tone']}\n"
            f"话题方向：{'、'.join(strategy['topics'])}\n"
            "注意：自然融入，不要让用户感觉被分析。"
        )
