# -*- coding:utf-8 -*-
"""
Personality Manager v1.0
人格状态管理器：

  管理 AI 角色的动态人格状态。
  不修改原始角色配置，只在 personality_state 表中存储变化量。
"""
from .. import db


# 人格变化维度默认值
DEFAULT_PERSONALITY_STATE = {
    "softness": 0,      # 温柔度（0-20）
    "humor": 0,         # 幽默感（0-15）
    "initiative": 0,    # 主动性（0-20）
    "attachment": 0,    # 依恋感（0-30）
    "playfulness": 0,   # 调皮度（0-15）
    "vulnerability": 0, # 脆弱表达（0-10）
    "growth_interactions": 0,
    "growth_events": "[]",
    "last_growth_at": "",
    # 兼容旧版五维偏移字段
    "warmth_delta": 0,
    "dominance_delta": 0,
    "humor_delta": 0,
    "initiative_delta": 0,
    "attachment_delta": 0,
}


class PersonalityManager:
    """人格状态管理器"""

    def get_state(self, session_id, character_id="default"):
        """
        获取人格状态。

        Args:
            session_id: 会话ID
            character_id: 角色ID

        Returns:
            dict: 人格状态
        """
        try:
            state = db.get_personality_state(session_id, character_id)
        except Exception:
            state = {}

        if not state:
            return dict(DEFAULT_PERSONALITY_STATE)

        # 合并默认值，确保所有字段都存在
        result = dict(DEFAULT_PERSONALITY_STATE)
        for key in DEFAULT_PERSONALITY_STATE:
            if key in state:
                try:
                    result[key] = int(state.get(key, 0)) if key not in ("growth_events", "last_growth_at") else state.get(key, DEFAULT_PERSONALITY_STATE[key])
                except (ValueError, TypeError):
                    result[key] = 0

        # 六维成长与旧五维偏移共用同一份真实数据，避免两套人格各自增长。
        result["humor"] = max(0, int(state.get("humor_delta", result.get("humor", 0)) or 0))
        result["initiative"] = max(0, int(state.get("initiative_delta", result.get("initiative", 0)) or 0))
        result["attachment"] = max(0, int(state.get("attachment_delta", result.get("attachment", 0)) or 0))
        result["softness"] = max(int(result.get("softness", 0) or 0), max(0, int(state.get("warmth_delta", 0) or 0)))

        return result

    def update(self, session_id, character_id, changes):
        """
        更新人格状态（增量更新）。

        Args:
            session_id: 会话ID
            character_id: 角色ID
            changes: 变化字典 {softness: +1, initiative: +2}

        Returns:
            dict: 更新后的人格状态
        """
        from .evolution import apply_evolution_limits

        current = self.get_state(session_id, character_id)

        # 应用增量变化
        logical_to_physical = {
            "humor": "humor_delta",
            "initiative": "initiative_delta",
            "attachment": "attachment_delta",
        }
        for key, delta in changes.items():
            if key in current:
                try:
                    current[key] = current.get(key, 0) + int(delta)
                    physical = logical_to_physical.get(key)
                    if physical:
                        current[physical] = current.get(physical, 0) + int(delta)
                except (ValueError, TypeError):
                    pass

        # 反向同步：旧系统直接写 *_delta 时，档案页的六维也立即看到变化。
        if "humor_delta" in changes:
            current["humor"] = max(0, int(current.get("humor_delta", 0) or 0))
        if "initiative_delta" in changes:
            current["initiative"] = max(0, int(current.get("initiative_delta", 0) or 0))
        if "attachment_delta" in changes:
            current["attachment"] = max(0, int(current.get("attachment_delta", 0) or 0))

        # 应用变化限制
        current = apply_evolution_limits(current)

        # 保存到数据库
        try:
            db.update_personality_state(
                session_id,
                character_id,
                **current
            )
        except Exception as e:
            print(f"[PersonalityManager] 保存人格状态失败: {e}", flush=True)

        return current

    def observe_interaction(self, session_id, character_id="default", user_text="", user_reply_delay=0.0):
        """把互动证据转成极小、可解释的成长变化；不改写基础角色卡。"""
        import json
        from datetime import datetime
        text = str(user_text or "")
        old = db.get_personality_state(session_id, character_id) or {}
        changes = {}
        evidence = []
        if any(x in text for x in ("哈哈", "笑死", "逗", "开玩笑", "整活", "梗")):
            changes["playfulness"] = 1; changes["humor_delta"] = 1; evidence.append("你们经常互相逗趣")
        if any(x in text for x in ("谢谢你", "抱抱", "爱你", "想你", "辛苦了", "陪着我")):
            changes["softness"] = 1; changes["warmth_delta"] = 1; evidence.append("你表达了温柔或依赖")
        if any(x in text for x in ("难过", "害怕", "焦虑", "压力", "崩溃", "委屈")):
            changes["vulnerability"] = 1; changes["softness"] = 1; evidence.append("你向TA袒露了脆弱")
        if any(x in text for x in ("你说得对", "听你的", "你决定", "帮我安排")):
            changes["dominance_delta"] = 1; evidence.append("你愿意让TA多做决定")
        if user_reply_delay and user_reply_delay > 3600:
            changes["initiative"] = 1; evidence.append("你较久后回来，TA会更主动接住你")
        if not changes:
            changes = {"growth_interactions": 1}
        else:
            changes["growth_interactions"] = 1
        # 每轮最多增长 1 点，且总量有硬上限；重要事实不由此模块生成。
        current = self.update(session_id, character_id, changes)
        events = []
        try: events = json.loads(old.get("growth_events") or "[]")
        except Exception: events = []
        if evidence:
            events.append({"time": datetime.now().strftime("%Y-%m-%d"), "items": evidence, "changes": changes})
            events = events[-12:]
        db.update_personality_state(session_id, character_id,
                                    growth_interactions=int(current.get("growth_interactions", 0) or 0),
                                    growth_events=json.dumps(events, ensure_ascii=False),
                                    last_growth_at=datetime.now().isoformat(timespec="seconds"),
                                    _reason="interaction_growth")
        return current

    def get_growth_profile(self, session_id, character_id="default"):
        import json
        state = self.get_state(session_id, character_id)
        try: events = json.loads(state.get("growth_events") or "[]")
        except Exception: events = []
        dimensions = [
            ("softness", "温柔度", 20), ("humor", "幽默度", 15),
            ("initiative", "主动性", 20), ("attachment", "依恋感", 30),
            ("playfulness", "调皮度", 15), ("vulnerability", "脆弱表达", 10),
        ]
        return {"level": self.get_evolution_level(session_id, character_id),
                "interactions": int(state.get("growth_interactions", 0) or 0),
                "dimensions": [{"key": k, "label": label, "value": int(state.get(k, 0) or 0), "max": mx} for k, label, mx in dimensions],
                "events": events[-8:]}

    def reset(self, session_id, character_id="default"):
        """
        重置人格状态（恢复默认值）。

        Args:
            session_id: 会话ID
            character_id: 角色ID
        """
        try:
            db.update_personality_state(
                session_id,
                character_id,
                **DEFAULT_PERSONALITY_STATE
            )
        except Exception as e:
            print(f"[PersonalityManager] 重置人格状态失败: {e}", flush=True)

    def get_evolution_level(self, session_id, character_id="default"):
        """
        获取人格进化等级（0-100）。

        Args:
            session_id: 会话ID
            character_id: 角色ID

        Returns:
            int: 进化等级
        """
        state = self.get_state(session_id, character_id)

        # 计算综合进化等级
        total = (
            state.get("softness", 0) * 3 +
            state.get("humor", 0) * 2 +
            state.get("initiative", 0) * 3 +
            state.get("attachment", 0) * 2 +
            state.get("playfulness", 0) * 2 +
            state.get("vulnerability", 0) * 4
        )

        return min(100, total)
