# -*- coding:utf-8 -*-
"""
Relation Types v1.0
关系类型定义：

  知识图谱中实体之间的关系类型。
"""


# 关系类型列表
RELATION_TYPES = [
    "works_at",       # 工作于
    "likes",          # 喜欢
    "dislikes",       # 不喜欢
    "knows",          # 认识/了解
    "participates",   # 参与
    "causes",         # 导致
    "related_to",     # 相关
    "important_for",  # 对...重要
    "lives_in",       # 居住于
    "studies",        # 学习
    "owns",           # 拥有
    "friends_with",   # 是朋友
    "family_of",      # 是家人
    "goal_of",        # 是...的目标
    "habit_of",       # 是...的习惯
    "skill_of",       # 是...的技能
    "interested_in",  # 对...感兴趣
    "affected_by",    # 受...影响
    "leads_to",       # 导致/通向
    "part_of",        # 是...的一部分
]


# 关系类型中文描述
RELATION_TYPE_NAMES = {
    "works_at": "工作于",
    "likes": "喜欢",
    "dislikes": "不喜欢",
    "knows": "认识",
    "participates": "参与",
    "causes": "导致",
    "related_to": "相关",
    "important_for": "对...重要",
    "lives_in": "居住于",
    "studies": "学习",
    "owns": "拥有",
    "friends_with": "是朋友",
    "family_of": "是家人",
    "goal_of": "是目标",
    "habit_of": "是习惯",
    "skill_of": "是技能",
    "interested_in": "感兴趣",
    "affected_by": "受影响",
    "leads_to": "导致",
    "part_of": "是一部分",
}


def is_valid_relation_type(relation_type):
    """检查关系类型是否有效"""
    return relation_type in RELATION_TYPES


def normalize_relation_type(relation_type):
    """规范化关系类型"""
    if not relation_type:
        return "related_to"

    relation_type = str(relation_type).lower().strip()

    # 常见别名映射
    aliases = {
        "工作": "works_at",
        "上班": "works_at",
        "喜欢": "likes",
        "爱": "likes",
        "不喜欢": "dislikes",
        "讨厌": "dislikes",
        "认识": "knows",
        "了解": "knows",
        "参与": "participates",
        "参加": "participates",
        "导致": "causes",
        "引起": "causes",
        "相关": "related_to",
        "有关": "related_to",
        "重要": "important_for",
        "居住": "lives_in",
        "住在": "lives_in",
        "学习": "studies",
        "学": "studies",
        "拥有": "owns",
        "有": "owns",
        "朋友": "friends_with",
        "家人": "family_of",
        "目标": "goal_of",
        "习惯": "habit_of",
        "技能": "skill_of",
        "兴趣": "interested_in",
        "影响": "affected_by",
        "导致": "leads_to",
        "部分": "part_of",
    }

    if relation_type in aliases:
        return aliases[relation_type]

    if relation_type in RELATION_TYPES:
        return relation_type

    return "related_to"


def get_relation_type_name(relation_type):
    """获取关系类型的中文名称"""
    return RELATION_TYPE_NAMES.get(relation_type, "相关")


def suggest_relation_type(source_type, target_type, context=""):
    """
    根据实体类型和上下文推测关系类型。

    Args:
        source_type: 源实体类型
        target_type: 目标实体类型
        context: 上下文

    Returns:
        str: 推测的关系类型
    """
    context = str(context or "").lower()

    # 用户 -> 公司
    if source_type == "user" and target_type == "company":
        if "工作" in context or "上班" in context:
            return "works_at"
        return "related_to"

    # 用户 -> 兴趣
    if source_type == "user" and target_type == "interest":
        return "likes"

    # 用户 -> 项目
    if source_type == "user" and target_type == "project":
        return "participates"

    # 用户 -> 地点
    if source_type == "user" and target_type == "place":
        if "住" in context or "家" in context:
            return "lives_in"
        return "related_to"

    # 项目 -> 情绪
    if source_type == "project" and target_type == "emotion":
        return "causes"

    # 情绪 -> 用户
    if source_type == "emotion" and target_type == "user":
        return "affected_by"

    # 默认
    return "related_to"
