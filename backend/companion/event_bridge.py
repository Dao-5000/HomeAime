# -*- coding:utf-8 -*-
"""
Relationship Event Bridge v1.0
关系事件桥接层：

  负责：聊天事件 -> 关系影响
  不负责：记忆保存、关系计算
  调用已有模块：relationship.manager、memory_manager、db

  连接：
    memory_manager.py
        |
    relationship/
        |
    idle_agent.py

  实现：
    聊天事件
      ↓
    事件识别
      ↓
    关系影响评估
      ↓
    同步更新：
      1. long_term_memory（关系记忆）
      2. relationship_state（关系状态）
      3. proactive上下文（通过关系状态间接影响）
"""
from ..relationship.manager import RelationshipManager
from ..memory_manager import dedupe_insert
from .. import db


class RelationshipEventBridge:
    """
    关系事件桥接层：
    识别聊天中的关系事件，评估影响，同步更新关系状态和记忆。
    """

    def __init__(self):
        self.relationship = RelationshipManager()

    def detect_event_type(self, message):
        """
        简单规则层事件识别。
        后续可以替换为 LLM 分类。

        Args:
            message: 用户消息文本

        Returns:
            list: 事件类型列表
        """
        message = str(message or "")
        events = []

        # 分享个人信息
        personal_words = [
            "小时候", "家人", "童年", "过去", "秘密", "第一次",
            "我家", "我妈", "我爸", "我朋友", "以前的"
        ]
        for w in personal_words:
            if w in message:
                events.append("personal_share")
                break

        # 情绪依赖/信任
        emotional_words = [
            "只有你", "陪陪我", "想和你说", "告诉你",
            "跟你说", "只跟你", "相信你", "依赖你"
        ]
        for w in emotional_words:
            if w in message:
                events.append("emotional_trust")
                break

        # 亲密互动
        intimacy_words = [
            "想你", "爱你", "抱抱", "亲亲", "么么哒",
            "宝贝", "宝宝", "亲爱的", "喜欢你"
        ]
        for w in intimacy_words:
            if w in message:
                events.append("intimate")
                break

        # 冲突/负面
        conflict_words = [
            "讨厌你", "不理你", "分手", "生气了", "烦你",
            "不想理你", "滚", "闭嘴"
        ]
        for w in conflict_words:
            if w in message:
                events.append("conflict")
                break

        # 承诺/约定
        promise_words = [
            "约定", "答应", "保证", "以后", "下次",
            "永远", "一直", "承诺"
        ]
        for w in promise_words:
            if w in message:
                events.append("promise")
                break

        return events

    def calculate_impact(self, event_type):
        """
        计算事件对关系的影响。

        Args:
            event_type: 事件类型

        Returns:
            dict: 影响值（trust/intimacy/affection/memory_importance）
        """
        impact = {}

        if event_type == "personal_share":
            impact = {
                "trust": 2,
                "intimacy": 1,
                "memory_importance": 8
            }
        elif event_type == "emotional_trust":
            impact = {
                "trust": 3,
                "intimacy": 2,
                "memory_importance": 9
            }
        elif event_type == "intimate":
            impact = {
                "affection": 2,
                "intimacy": 2,
                "memory_importance": 7
            }
        elif event_type == "conflict":
            impact = {
                "trust": -3,
                "intimacy": -2,
                "affection": -5,
                "memory_importance": 6
            }
        elif event_type == "promise":
            impact = {
                "trust": 2,
                "intimacy": 1,
                "memory_importance": 7
            }

        return impact

    async def process(self, user_id, character_id, message):
        """
        处理聊天事件，更新关系状态和记忆。

        Args:
            user_id: 用户ID
            character_id: 角色ID
            message: 用户消息

        Returns:
            list: 处理结果列表
        """
        events = self.detect_event_type(message)

        if not events:
            return None

        result = []

        # 获取当前关系状态
        state = self.relationship.get_state(user_id, character_id)

        for event in events:
            impact = self.calculate_impact(event)

            if not impact:
                continue

            # ==================
            # 1. 更新关系状态
            # ==================
            update = {}

            for key in ["trust", "intimacy", "affection"]:
                if key in impact:
                    old = state.get(key, 0) or 0
                    new_val = max(0, min(100, old + impact[key]))
                    update[key] = new_val
                    # 更新本地 state，避免多个事件叠加时用旧值
                    state[key] = new_val

            if update:
                try:
                    self.relationship.update(
                        user_id,
                        character_id,
                        **update
                    )
                except Exception as e:
                    print(f"[EventBridge] 更新关系状态失败: {e}", flush=True)

            # ==================
            # 2. 写入关系记忆
            # ==================
            event_descriptions = {
                "personal_share": "用户主动分享个人经历和隐私",
                "emotional_trust": "用户表达了对AI的情感依赖和信任",
                "intimate": "用户与AI进行了亲密互动",
                "conflict": "用户与AI发生了冲突或表达了不满",
                "promise": "用户与AI之间形成了约定或承诺",
            }

            memory_text = (
                f"关系事件：{event_descriptions.get(event, event)}。"
                f"用户消息：{str(message)[:100]}"
            )

            try:
                dedupe_insert(
                    memory_text,
                    memory_type="relationship",
                    importance=impact.get("memory_importance", 6),
                    session_id=user_id,
                    character_id=character_id,
                )
            except Exception as e:
                print(f"[EventBridge] 写入关系记忆失败: {e}", flush=True)

            result.append({
                "event": event,
                "impact": impact
            })

        # ==================
        # 3. 长期关系时间线提取（Relationship Timeline v1.0）
        # ==================
        try:
            from ...timeline.extractor import extract_and_save_event
            await extract_and_save_event(
                session_id=user_id,
                character_id=character_id,
                chat_text=message
            )
        except Exception as e:
            print(f"[EventBridge] 时间线事件提取失败: {e}", flush=True)

        return result


# 全局单例
_bridge_instance = None


def get_bridge():
    """获取 RelationshipEventBridge 全局单例"""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = RelationshipEventBridge()
    return _bridge_instance
