# -*- coding:utf-8 -*-
"""
Feedback Learner v1.0
反馈学习器：

  根据用户反馈调整 AI 的行为策略：
    1. Memory 权重调整
    2. Relationship 策略调整
    3. Proactive 策略调整
    4. Personality 偏移调整
    5. 用户反馈偏好画像
"""
from .database import get_feedback_stats, get_recent_feedback, get_positive_rate
from .analyzer import analyze_feedback_patterns


class FeedbackLearner:
    """反馈学习器"""

    def __init__(self):
        pass

    def apply(self, feedback):
        """
        根据单条反馈生成调整建议。

        Args:
            feedback: 反馈类型

        Returns:
            dict: 调整建议
        """
        if feedback == "positive":
            return {
                "memory_weight": "+",
                "style": "keep",
                "proactive": "increase",
            }
        elif feedback == "negative":
            return {
                "style": "avoid",
                "proactive": "decrease",
            }
        elif feedback == "continued":
            return {
                "engagement": "+",
                "style": "keep",
            }
        elif feedback == "correction":
            return {
                "style": "adjust",
                "memory_weight": "update",
            }
        elif feedback == "silence":
            return {
                "proactive": "decrease",
                "style": "review",
            }
        return {}

    def get_recent_signal(self, session_id, character_id="default"):
        """
        读取最近一条反馈信号（positive/negative/neutral）
        供五维人格 delta 行为推算用
        """
        try:
            recent = get_recent_feedback(session_id, character_id, limit=1)
            if not recent:
                return {"last_signal": "neutral"}
            ft = str(recent[0].get("feedback_type", ""))
            if ft in ("positive", "continued"):
                return {"last_signal": "positive"}
            if ft in ("negative", "correction"):
                return {"last_signal": "negative"}
            return {"last_signal": "neutral"}
        except Exception:
            return {"last_signal": "neutral"}

    def get_profile(self, session_id, character_id="default"):
        """
        获取用户反馈偏好画像。

        Args:
            session_id: 会话ID
            character_id: 角色ID

        Returns:
            dict: 用户反馈偏好画像
        """
        profile = {
            "positive_rate": 0.5,
            "preferred_styles": [],
            "avoid_styles": [],
            "preferred_length": "normal",
            "preferred_tone": "neutral",
            "total_feedback": 0,
            "recommendations": [],
        }

        try:
            # 获取反馈统计
            stats = get_feedback_stats(session_id, character_id)
            total = sum(s.get("count", 0) for s in stats)
            profile["total_feedback"] = total

            # 正面反馈率
            profile["positive_rate"] = get_positive_rate(session_id, character_id)

            # 获取最近反馈，分析模式
            recent = get_recent_feedback(session_id, character_id, limit=50)
            if recent:
                patterns = analyze_feedback_patterns(recent)
                profile.update(patterns)

            # 生成建议
            profile["recommendations"] = self._generate_recommendations(profile)

        except Exception as e:
            print(f"[FeedbackLearner] 获取反馈画像失败: {e}", flush=True)

        return profile

    def _generate_recommendations(self, profile):
        """
        根据反馈画像生成建议。

        Args:
            profile: 反馈画像

        Returns:
            list: 建议列表
        """
        recommendations = []

        positive_rate = profile.get("positive_rate", 0.5)
        preferred_styles = profile.get("preferred_styles", [])
        avoid_styles = profile.get("avoid_styles", [])

        if positive_rate < 0.3:
            recommendations.append("用户正面反馈率较低，建议调整回复风格")
        elif positive_rate > 0.8:
            recommendations.append("用户反馈很好，保持当前风格")

        if preferred_styles:
            recommendations.append(f"用户更喜欢：{', '.join(preferred_styles)}")

        if avoid_styles:
            recommendations.append(f"用户不喜欢：{', '.join(avoid_styles)}")

        if not recommendations:
            recommendations.append("反馈数据不足，继续观察用户偏好")

        return recommendations

    def update_memory_weight(self, memory, feedback):
        """
        根据反馈调整记忆权重。

        Args:
            memory: 记忆字典
            feedback: 反馈类型

        Returns:
            int: 调整后的重要性
        """
        importance = int(memory.get("importance", 5))

        if feedback == "positive":
            importance = min(10, importance + 1)
        elif feedback == "negative":
            importance = max(1, importance - 1)

        return importance

    def update_proactive_weight(self, proactive_type, feedback):
        """
        根据反馈调整主动消息权重。

        Args:
            proactive_type: 主动消息类型
            feedback: 反馈类型

        Returns:
            float: 调整后的权重
        """
        base_weights = {
            "comfort": 0.7,
            "follow_up": 0.6,
            "miss_you": 0.5,
            "memory": 0.5,
            "encourage": 0.6,
            "casual": 0.4,
            "morning": 0.3,
        }

        weight = base_weights.get(proactive_type, 0.5)

        if feedback == "positive":
            weight = min(1.0, weight + 0.1)
        elif feedback == "negative":
            weight = max(0.1, weight - 0.15)
        elif feedback == "silence":
            weight = max(0.1, weight - 0.05)

        return weight

    def build_feedback_prompt(self, profile):
        """
        构建反馈偏好 Prompt。

        Args:
            profile: 反馈画像

        Returns:
            str: Prompt 文本

        ★ 2026-09-17：门槛从写死的 3 降到可配（默认 2）。
          原因：真机线上 `ai_feedback` 长期只有 1 行（隐式反馈写进了打包目录，
          换库后真实会话从 0 开始），门槛 3 意味着"要等好几轮才开始学"。
          2 条就能看出一点倾向，先把回路跑起来更重要 —— 画像本身也只在
          有 preferred/avoid/recommendations 时才输出内容，不会硬凑。
        """
        try:
            import os
            _min = int(os.environ.get("FEEDBACK_PROMPT_MIN", "2") or 2)
        except Exception:
            _min = 2
        if not profile or profile.get("total_feedback", 0) < _min:
            return ""

        lines = ["【用户反馈偏好】"]
        lines.append("根据长期互动反馈，用户更喜欢以下方式：")

        preferred = profile.get("preferred_styles", [])
        if preferred:
            for style in preferred:
                lines.append(f"- {_style_description(style)}")

        avoid = profile.get("avoid_styles", [])
        if avoid:
            lines.append("")
            lines.append("尽量避免：")
            for style in avoid:
                lines.append(f"- {_style_description(style)}")

        recommendations = profile.get("recommendations", [])
        if recommendations:
            lines.append("")
            for rec in recommendations[:3]:
                lines.append(f"- {rec}")

        lines.append("")
        lines.append("自然理解和运用，不要向用户提到反馈分析。")

        return "\n".join(lines)


def _style_description(style):
    """将风格标签转换为描述"""
    descriptions = {
        # 短句系列
        "short_warm":      "简短温柔（省略号/叠词）",
        "short_playful":   "简短活泼（多感叹问号）",
        "short_directive": "简短直接",
        "short_neutral":   "简短回应",
        # 中等系列
        "normal_warm":     "适度温柔",
        "normal_playful":  "适度活泼",
        "normal_directive": "适度给建议",
        "normal_neutral":  "适度回应",
        # 长句系列
        "long_warm":       "详细温柔",
        "long_playful":    "详细活泼",
        "long_directive":  "详细给建议",
        "long_neutral":    "详细回应",
    }
    return descriptions.get(style, style)
