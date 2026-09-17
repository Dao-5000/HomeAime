# -*- coding:utf-8 -*-
"""
Entity Types v1.0
实体类型定义：

  知识图谱中的实体类型，用于分类用户生活中的各种事物。
"""


# 实体类型列表
ENTITY_TYPES = [
    "user",        # 用户本人
    "person",      # 其他人（家人/朋友/同事）
    "company",     # 公司/组织
    "place",       # 地点
    "object",      # 物品
    "interest",    # 兴趣爱好
    "project",     # 项目/工作
    "emotion",     # 情绪/心理状态
    "skill",       # 技能
    "goal",        # 目标
    "habit",       # 习惯
    "event",       # 事件
    "concept",     # 概念/想法
    "media",       # 媒体（书/电影/音乐）
    "food",        # 食物
    "pet",         # 宠物
    "health",      # 健康相关
    "other",       # 其他
]


# 实体类型中文描述
ENTITY_TYPE_NAMES = {
    "user": "用户",
    "person": "人物",
    "company": "公司",
    "place": "地点",
    "object": "物品",
    "interest": "兴趣",
    "project": "项目",
    "emotion": "情绪",
    "skill": "技能",
    "goal": "目标",
    "habit": "习惯",
    "event": "事件",
    "concept": "概念",
    "media": "媒体",
    "food": "食物",
    "pet": "宠物",
    "health": "健康",
    "other": "其他",
}


def is_valid_entity_type(entity_type):
    """检查实体类型是否有效"""
    return entity_type in ENTITY_TYPES


def normalize_entity_type(entity_type):
    """规范化实体类型"""
    if not entity_type:
        return "other"

    entity_type = str(entity_type).lower().strip()

    # 常见别名映射
    aliases = {
        "人": "person",
        "人物": "person",
        "公司": "company",
        "组织": "company",
        "地点": "place",
        "地方": "place",
        "兴趣": "interest",
        "爱好": "interest",
        "项目": "project",
        "工作": "project",
        "情绪": "emotion",
        "心情": "emotion",
        "技能": "skill",
        "能力": "skill",
        "目标": "goal",
        "习惯": "habit",
        "事件": "event",
        "事情": "event",
        "概念": "concept",
        "想法": "concept",
        "媒体": "media",
        "书": "media",
        "电影": "media",
        "音乐": "media",
        "食物": "food",
        "吃的": "food",
        "宠物": "pet",
        "动物": "pet",
        "健康": "health",
        "身体": "health",
    }

    if entity_type in aliases:
        return aliases[entity_type]

    if entity_type in ENTITY_TYPES:
        return entity_type

    return "other"


def get_entity_type_name(entity_type):
    """获取实体类型的中文名称"""
    return ENTITY_TYPE_NAMES.get(entity_type, "其他")


def suggest_entity_type(name, context=""):
    """
    根据实体名称和上下文推测实体类型。

    Args:
        name: 实体名称
        context: 上下文

    Returns:
        str: 推测的实体类型
    """
    name = str(name or "").lower()
    context = str(context or "").lower()

    # 公司关键词
    company_keywords = ["公司", "集团", "科技", "有限", "股份", "厂", "店", "工作室"]
    if any(k in name for k in company_keywords):
        return "company"

    # 地点关键词
    place_keywords = ["市", "省", "区", "县", "镇", "村", "路", "街", "公园", "学校", "医院", "商场"]
    if any(k in name for k in place_keywords):
        return "place"

    # 兴趣关键词
    interest_keywords = ["喜欢", "爱好", "玩", "看", "听", "读", "学"]
    if any(k in context for k in interest_keywords):
        return "interest"

    # 项目关键词
    project_keywords = ["项目", "工作", "任务", "计划", "开发", "设计"]
    if any(k in context for k in project_keywords):
        return "project"

    # 情绪关键词
    emotion_keywords = ["开心", "难过", "焦虑", "压力", "累", "烦", "幸福"]
    if any(k in name for k in emotion_keywords) or any(k in context for k in emotion_keywords):
        return "emotion"

    # 默认
    return "other"
