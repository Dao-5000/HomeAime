# -*- coding:utf-8 -*-
"""
Decision Layer v1.0
决策层：

  根据场景决定哪些模块需要参与，以及模块的优先级。

  模块列表：
    - character：角色人格（始终参与）
    - memory：长期记忆（始终参与）
    - relationship：关系状态（始终参与）
    - emotion：用户情绪（情绪支持场景）
    - ai_state：AI自身状态（情绪支持/关系场景）
    - timeline：共同经历（回忆/关系场景）
    - personality：人格状态（关系场景）
    - behavior：行为模式（普通聊天）
    - feedback：反馈偏好（始终参与，数据足够时）
    - open_loops：未完成事项（跟进场景）
"""


# 场景对应的模块配置
SCENE_MODULES = {
    "emotion_support": {
        "required": ["character", "memory", "relationship", "emotion", "ai_state"],
        "optional": ["feedback", "behavior"],
        "priority": ["character", "emotion", "ai_state", "relationship", "memory"],
    },
    "memory_recall": {
        "required": ["character", "memory", "relationship", "timeline"],
        "optional": ["feedback", "emotion"],
        "priority": ["character", "timeline", "memory", "relationship"],
    },
    "relationship": {
        "required": ["character", "memory", "relationship", "personality", "timeline", "ai_state"],
        "optional": ["feedback", "emotion"],
        "priority": ["character", "relationship", "personality", "ai_state", "timeline", "memory"],
    },
    "advice_seeking": {
        "required": ["character", "memory", "relationship"],
        "optional": ["feedback", "behavior", "emotion"],
        "priority": ["character", "memory", "relationship", "behavior"],
    },
    "celebration": {
        "required": ["character", "memory", "relationship", "emotion", "ai_state"],
        "optional": ["feedback", "timeline"],
        "priority": ["character", "emotion", "ai_state", "relationship", "memory"],
    },
    "normal_chat": {
        "required": ["character", "memory", "relationship"],
        "optional": ["feedback", "behavior", "emotion", "ai_state"],
        "priority": ["character", "relationship", "memory", "behavior"],
    },
    "absence": {
        "required": ["character", "memory", "relationship", "timeline", "ai_state"],
        "optional": ["feedback", "behavior", "emotion"],
        "priority": ["character", "relationship", "timeline", "ai_state", "memory"],
    },
    "long_absence": {
        "required": ["character", "memory", "relationship", "timeline", "ai_state", "emotion"],
        "optional": ["feedback", "behavior"],
        "priority": ["character", "relationship", "timeline", "emotion", "ai_state", "memory"],
    },
    "follow_up": {
        "required": ["character", "memory", "relationship", "open_loops"],
        "optional": ["feedback", "emotion", "timeline"],
        "priority": ["character", "open_loops", "relationship", "memory"],
    },
    "casual_proactive": {
        "required": ["character", "memory", "relationship"],
        "optional": ["feedback", "behavior", "ai_state", "timeline"],
        "priority": ["character", "relationship", "memory", "behavior"],
    },
}

# 默认模块配置
DEFAULT_MODULES = {
    "required": ["character", "memory", "relationship"],
    "optional": ["feedback", "behavior", "emotion", "ai_state", "timeline", "personality", "open_loops"],
    "priority": ["character", "relationship", "memory", "behavior", "emotion"],
}


class CompanionDecision:
    """决策层"""

    def __init__(self):
        self.scene_modules = SCENE_MODULES

    def decide(self, route, context=None):
        """
        根据场景决定参与的模块。

        Args:
            route: 路由结果 {scene, confidence, ...}
            context: 上下文信息

        Returns:
            dict: 决策结果 {modules, priority, scene, weights}
        """
        scene = route.get("scene", "normal_chat")
        confidence = route.get("confidence", 0.5)

        # 获取场景对应的模块配置
        config = self.scene_modules.get(scene, DEFAULT_MODULES)

        required = config.get("required", [])
        optional = config.get("optional", [])
        priority = config.get("priority", required)

        # 计算模块权重
        weights = {}
        for i, module in enumerate(priority):
            weights[module] = max(0.1, 1.0 - i * 0.1)

        # 可选模块权重较低
        for module in optional:
            if module not in weights:
                weights[module] = 0.3

        # 所有参与的模块（必需 + 可选）
        all_modules = list(dict.fromkeys(required + optional))

        return {
            "scene": scene,
            "confidence": confidence,
            "modules": all_modules,
            "required_modules": required,
            "optional_modules": optional,
            "priority": priority,
            "weights": weights,
        }

    def decide_updates(self, route, context=None):
        """
        决定聊天结束后需要更新哪些状态。

        Args:
            route: 路由结果
            context: 上下文

        Returns:
            list: 需要更新的模块列表
        """
        scene = route.get("scene", "normal_chat")

        # 基础更新模块
        updates = ["memory", "relationship", "feedback"]

        # 场景特定更新
        if scene in ["emotion_support", "celebration"]:
            updates.append("emotion")
            updates.append("ai_state")

        if scene in ["relationship", "celebration"]:
            updates.append("personality")

        if scene in ["memory_recall", "relationship"]:
            updates.append("timeline")

        if scene == "advice_seeking":
            updates.append("behavior")

        # 去重
        return list(dict.fromkeys(updates))

    def get_module_description(self, module):
        """
        获取模块描述。

        Args:
            module: 模块名

        Returns:
            str: 模块描述
        """
        descriptions = {
            "character": "角色人格设定",
            "memory": "长期记忆检索",
            "relationship": "关系状态",
            "emotion": "用户情绪状态",
            "ai_state": "AI自身状态",
            "timeline": "共同经历时间线",
            "personality": "自适应人格状态",
            "behavior": "用户行为模式",
            "feedback": "用户反馈偏好",
            "open_loops": "未完成事项",
        }
        return descriptions.get(module, module)
