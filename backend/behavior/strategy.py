# -*- coding:utf-8 -*-
"""
Behavior Strategy v1.0
行为策略模块：

  读取行为模式，生成 Prompt 块，并为主动陪伴提供策略建议。
"""
from .database import get_patterns


def get_behavior_patterns(session_id, character_id="default", limit=5):
    """
    获取用户的行为模式。

    Args:
        session_id: 会话ID
        character_id: 角色ID
        limit: 返回数量

    Returns:
        list: 行为模式列表
    """
    try:
        return get_patterns(session_id, character_id, limit)
    except Exception as e:
        print(f"[BehaviorStrategy] 获取行为模式失败: {e}", flush=True)
        return []


def build_behavior_prompt(patterns):
    """
    将行为模式转换为 Prompt 文本。

    Args:
        patterns: 行为模式列表

    Returns:
        str: Prompt 文本
    """
    if not patterns:
        return ""

    lines = []
    for p in patterns:
        pattern = p.get("pattern", "")
        pattern_type = p.get("pattern_type", "")
        confidence = p.get("confidence", 0)

        if pattern:
            line = f"- {pattern}"
            if confidence >= 0.8:
                line += "（高置信度）"
            lines.append(line)

    if not lines:
        return ""

    return (
        "【用户行为模式】\n"
        "以下是根据长期互动总结的用户行为规律，"
        "用于理解用户习惯和选择合适的沟通方式。"
        "自然理解和运用，不要向用户提到行为分析。\n"
        + "\n".join(lines)
    )


def get_proactive_strategy(patterns):
    """
    根据行为模式获取主动陪伴策略建议。

    Args:
        patterns: 行为模式列表

    Returns:
        dict: 策略建议 {trigger, instruction, best_time}
    """
    if not patterns:
        return {
            "trigger": "casual",
            "instruction": "自然闲聊",
            "best_time": None,
        }

    trigger = "casual"
    instruction = "自然闲聊"
    best_time = None

    for p in patterns:
        pattern_type = p.get("pattern_type", "")
        pattern = p.get("pattern", "").lower()

        # 压力期 → 安慰
        if pattern_type == "stress_period" or "压力" in pattern:
            trigger = "comfort"
            instruction = "用户可能处于压力期，主动关心情绪状态，语气轻柔"
            break

        # 时间模式 → 最佳主动时间
        if pattern_type == "time_pattern":
            if "晚上" in pattern or "深夜" in pattern:
                best_time = "evening"
            elif "上午" in pattern:
                best_time = "morning"

        # 沟通模式 → 沟通策略
        if pattern_type == "communication_pattern":
            if "简短" in pattern:
                instruction = "用户喜欢简短沟通，主动消息不要太长"
            elif "详细" in pattern or "倾诉" in pattern:
                instruction = "用户喜欢详细倾诉，可以主动引导分享"

    return {
        "trigger": trigger,
        "instruction": instruction,
        "best_time": best_time,
    }


def should_proactive_now(patterns, current_hour=None):
    """
    判断当前是否是主动陪伴的好时机。

    Args:
        patterns: 行为模式列表
        current_hour: 当前小时（0-23），None则自动获取

    Returns:
        tuple: (should_proactive, reason)
    """
    from datetime import datetime

    if current_hour is None:
        current_hour = datetime.now().hour

    for p in patterns:
        pattern_type = p.get("pattern_type", "")
        pattern = p.get("pattern", "")

        if pattern_type == "time_pattern":
            # 如果用户通常在晚上聊天，当前是晚上 → 好时机
            if ("晚上" in pattern or "深夜" in pattern) and 18 <= current_hour <= 24:
                return True, "用户通常在晚上活跃，当前是好时机"
            if ("上午" in pattern) and 8 <= current_hour <= 12:
                return True, "用户通常在上午活跃，当前是好时机"

    return False, "当前不是用户通常活跃的时间"


def predict_next_need(patterns, now=None):
    """把稳定行为模式转换成可执行的下一步预测，供主动调度使用。"""
    from datetime import datetime
    now = now or datetime.now()
    best = {"kind": "", "confidence": 0.0, "reason": "", "suggestion": ""}
    for item in patterns or []:
        pattern = str(item.get("pattern") or "")
        ptype = str(item.get("pattern_type") or "")
        confidence = float(item.get("confidence") or 0.0)
        candidate = None
        if ptype == "stress_period" or "压力" in pattern or "焦虑" in pattern:
            candidate = {
                "kind": "comfort", "confidence": confidence,
                "reason": pattern, "suggestion": "轻轻问近况，先陪伴，不直接给建议",
            }
        elif ptype == "time_pattern":
            active_now = False
            if ("晚上" in pattern or "深夜" in pattern) and 19 <= now.hour <= 23:
                active_now = True
            if "上午" in pattern and 8 <= now.hour <= 12:
                active_now = True
            if ("周末" in pattern or "周六" in pattern or "周日" in pattern) and now.weekday() >= 5:
                active_now = active_now or 18 <= now.hour <= 23
            if active_now:
                candidate = {
                    "kind": "conversation", "confidence": confidence,
                    "reason": pattern, "suggestion": "用户可能想找人聊聊，用具体小问题自然开场",
                }
        elif ptype == "communication_pattern" and any(k in pattern for k in ("倾诉", "详细", "长聊")):
            candidate = {
                "kind": "deep_chat", "confidence": confidence,
                "reason": pattern, "suggestion": "给用户一个容易展开、没有标准答案的问题",
            }
        if candidate and candidate["confidence"] > best["confidence"]:
            best = candidate
    return best
