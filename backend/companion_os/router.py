# -*- coding:utf-8 -*-
"""
Context Router v1.0
场景路由器：

  判断当前聊天属于什么场景，决定哪些模块需要参与。

  场景类型：
    - emotion_support：情绪支持（用户表达负面情绪）
    - memory_recall：记忆回忆（用户提到过去的事情）
    - relationship：关系互动（用户表达亲密/喜欢）
    - advice_seeking：寻求建议（用户问问题/求助）
    - casual_chat：普通闲聊
    - absence：久未联系（主动消息场景）
"""


# 场景关键词映射
SCENE_KEYWORDS = {
    "emotion_support": [
        "难过", "累", "压力", "焦虑", "伤心", "痛苦", "崩溃",
        "烦", "郁闷", "失落", "沮丧", "绝望", "孤独", "寂寞",
        "不开心", "心情不好", "想哭", "委屈", "生气", "愤怒",
        "疲惫", "没精神", "提不起劲", "迷茫", "无助",
    ],
    "memory_recall": [
        "记得", "以前", "上次", "那时候", "曾经", "过去",
        "小时候", "那年", "当时", "之前", "回忆", "想起",
        "还记得吗", "你还记得", "那次",
    ],
    "relationship": [
        "喜欢你", "想你", "爱你", "抱抱", "亲亲", "么么",
        "在一起", "男朋友", "女朋友", "恋人", "约会",
        "想念", "舍不得", "依赖", "陪伴",
    ],
    "advice_seeking": [
        "怎么办", "怎么", "如何", "建议", "推荐", "帮我",
        "应该", "可不可以", "能不能", "请教", "问一下",
        "选哪个", "哪个好",
    ],
    "celebration": [
        "开心", "高兴", "成功", "通过", "录取", "升职",
        "生日", "纪念日", "庆祝", "太好了", "太棒了",
        "赢了", "完成", "搞定",
    ],
}


class ContextRouter:
    """场景路由器"""

    def __init__(self):
        self.scene_keywords = SCENE_KEYWORDS

    def route(self, message, context=None):
        """
        判断当前消息属于什么场景。

        Args:
            message: 用户消息
            context: 上下文信息（可选，包含最近消息等）

        Returns:
            dict: 路由结果 {scene, confidence, matched_keywords}
        """
        msg = str(message or "").lower()

        if not msg:
            return {
                "scene": "normal_chat",
                "confidence": 0.5,
                "matched_keywords": [],
            }

        # 计算每个场景的匹配度
        scene_scores = {}
        matched = {}

        for scene, keywords in self.scene_keywords.items():
            score = 0
            matched_keywords = []
            for keyword in keywords:
                if keyword in msg:
                    score += 1
                    matched_keywords.append(keyword)
            if score > 0:
                scene_scores[scene] = score
                matched[scene] = matched_keywords

        # 如果没有匹配到任何场景，检查是否是普通闲聊
        if not scene_scores:
            return {
                "scene": "normal_chat",
                "confidence": 0.6,
                "matched_keywords": [],
            }

        # 选择得分最高的场景
        best_scene = max(scene_scores, key=scene_scores.get)
        best_score = scene_scores[best_scene]

        # 计算置信度
        confidence = min(0.5 + best_score * 0.15, 0.95)

        return {
            "scene": best_scene,
            "confidence": confidence,
            "matched_keywords": matched.get(best_scene, []),
        }

    def route_proactive(self, inactive_hours=0, emotion=None, open_loops=None):
        """
        主动消息场景路由。

        Args:
            inactive_hours: 未活跃小时数
            emotion: 当前情绪状态
            open_loops: 未完成事项

        Returns:
            dict: 路由结果
        """
        # 长时间未联系
        if inactive_hours >= 72:
            return {
                "scene": "long_absence",
                "confidence": 0.9,
                "reason": "用户3天以上未聊天",
            }
        elif inactive_hours >= 24:
            return {
                "scene": "absence",
                "confidence": 0.8,
                "reason": "用户1天以上未聊天",
            }

        # 情绪触发
        if emotion:
            mood = emotion.get("mood", "")
            intensity = emotion.get("intensity", 0)
            if mood in ["sad", "anxious", "压力", "难过", "焦虑"] and intensity >= 7:
                return {
                    "scene": "emotion_support",
                    "confidence": 0.85,
                    "reason": "用户近期情绪较强",
                }

        # 未完成事项
        if open_loops:
            return {
                "scene": "follow_up",
                "confidence": 0.75,
                "reason": "有值得跟进的事情",
            }

        return {
            "scene": "casual_proactive",
            "confidence": 0.5,
            "reason": "普通主动问候",
        }
