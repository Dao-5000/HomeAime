# -*- coding:utf-8 -*-
"""
Proactive Decision Engine v1.0
主动陪伴决策引擎：

  不替换现有 idle_agent.py，而是作为决策层接入。
  负责：
    - 是否应该打扰
    - 为什么现在联系
    - 联系主题
    - 语气强度
    - 调用哪个生成策略
"""
from datetime import datetime

from .context import ProactiveContext
from .triggers import (
    check_inactive,
    check_emotion,
    check_open_loop,
    check_relationship_milestone,
    check_time_context,
    check_screen_context,
)
from .scoring import calculate_score, should_send, get_tone_intensity


# 主动消息类型定义
PROACTIVE_TYPES = {
    "miss_you": "想念用户",
    "comfort": "安慰用户",
    "follow_up": "跟进之前事情",
    "memory": "回忆共同经历",
    "encourage": "鼓励用户",
    "check_in": "简单问候",
    "celebrate": "分享喜悦",
    "morning_greeting": "早晨问候",
    "lunch_reminder": "午间关心",
    "night_care": "夜间陪伴",
    "relationship_milestone": "关系里程碑",
}


class ProactiveDecisionEngine:
    """
    主动陪伴决策引擎：
    综合评估上下文，决定是否发送主动消息以及发送什么类型的消息。
    """

    THRESHOLD = 15

    def __init__(self, threshold=None):
        if threshold is not None:
            self.THRESHOLD = threshold

    def decide(self, context):
        """
        核心决策方法。

        Args:
            context: ProactiveContext 对象

        Returns:
            dict or None: 决策结果
        """
        candidates = []

        # 1. 久未聊天触发
        inactive_hours = context.last_chat.get("inactive_hours", 0) or 0
        result = check_inactive(inactive_hours)
        if result:
            candidates.append(result)

        # 2. 情绪状态触发
        result = check_emotion(context.emotion)
        if result:
            candidates.append(result)

        # 3. 未完成事项触发
        result = check_open_loop(context.open_loops)
        if result:
            candidates.append(result)

        # 4. 关系里程碑触发
        result = check_relationship_milestone(context.relationship)
        if result:
            candidates.append(result)

        # 5. 时间场景触发
        current_hour = datetime.now().hour
        result = check_time_context(current_hour)
        if result:
            candidates.append(result)

        # 6. 屏幕感知触发（新增）
        result = check_screen_context(context.screen_state)
        if result:
            candidates.append(result)

        if not candidates:
            return None

        # 选择优先级最高的触发
        best = max(candidates, key=lambda x: x.get("priority", 0))

        # 计算评分
        score = calculate_score(context, best)

        # 判断是否应该发送
        if not should_send(score, self.THRESHOLD):
            return None

        # 计算语气强度
        tone = get_tone_intensity(context, best)

        return {
            "trigger": best["type"],
            "trigger_name": PROACTIVE_TYPES.get(best["type"], best["type"]),
            "reason": best.get("reason", ""),
            "score": score,
            "priority": best.get("priority", 0),
            "tone": tone,
            "should_send": True,
        }

    def build_prompt(self, decision, context):
        """
        根据决策结果构建主动消息生成 Prompt。

        Args:
            decision: 决策结果字典
            context: ProactiveContext 对象

        Returns:
            str: Prompt 文本
        """
        trigger = decision.get("trigger", "")
        trigger_name = decision.get("trigger_name", "")
        tone = decision.get("tone", "normal")

        # 关系状态
        rel = context.relationship
        rel_text = ""
        if rel:
            rel_text = f"""
关系阶段：{rel.get('stage', '')}
亲密程度：{rel.get('closeness', 50)}/100
相处方式：{rel.get('relationship_style', '')}
用户喜欢的称呼：{rel.get('preferred_call', '')}
"""

        # 相关记忆
        mem_text = ""
        if context.memories:
            mem_text = "\n相关记忆：\n"
            for m in context.memories[:5]:
                if isinstance(m, dict):
                    mem_text += f"- {m.get('content', m)}\n"
                else:
                    mem_text += f"- {m}\n"

        # 未完成事项
        loop_text = ""
        if context.open_loops:
            loop_text = "\n未完成事项：\n"
            for loop in context.open_loops[:3]:
                title = loop.get("title", "")
                if title:
                    loop_text += f"- {title}\n"

        # 屏幕状态（新增）
        screen_text = ""
        if context.screen_state:
            st = context.screen_state
            screen_text = f"\n屏幕状态：{st.get('summary', '')}\n"
            if st.get('topic_hint'):
                screen_text += f"切入话题：{st.get('topic_hint')}\n"

        prompt = f"""
你是用户长期陪伴AI。

主动联系原因：
{trigger_name}（{trigger}）

{rel_text}
{mem_text}
{loop_text}
{screen_text}
语气强度：{tone}

要求：
1. 像真实聊天，不要像机器人定时提醒
2. 不解释为什么联系，不要说"根据记录"
3. 根据关系阶段调整亲密程度
4. 关系较浅时保持礼貌，关系深入后可以自然亲密
5. 生成1-3条短消息，每条8-35字
6. 直接输出消息，不要编号和解释
"""

        return prompt


# 全局单例
_engine_instance = None


def get_engine():
    """获取 ProactiveDecisionEngine 全局单例"""
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = ProactiveDecisionEngine()
    return _engine_instance
