# -*- coding:utf-8 -*-
"""
Memory Scope Classifier v1.0
记忆作用域分类器：

  自动判断记忆应该归属哪个作用域：
    - global：用户基础信息（生日、职业、喜好等）
    - character：与特定角色相关的经历
    - relationship：关系私密记忆（约定、秘密、昵称等）

  使用关键词规则进行初步分类，后续可升级为 LLM 分类。
"""
from .scope import GLOBAL, CHARACTER, RELATIONSHIP, normalize_scope


# 关系私密关键词
RELATIONSHIP_WORDS = [
    "第一次", "我们", "约定", "秘密", "昵称", "一起",
    "专属", "暗号", "纪念日", "表白", "恋爱", "接吻",
    "抱抱", "亲亲", "想你", "爱你", "宝贝", "亲爱的",
    "老公", "老婆", "恋人", "情侣", "约会",
]

# 角色专属关键词
CHARACTER_WORDS = [
    "秦赫野", "时宁", "沈西洲", "助手",
    "他", "她", "这个角色", "那个人",
]

# 全局记忆类型（默认为 global）
GLOBAL_MEMORY_TYPES = [
    "fact", "preference", "event",
]

# 角色/关系记忆类型
CHARACTER_MEMORY_TYPES = [
    "relationship", "emotion",
]


def classify_scope(memory_type, content, character_id="default"):
    """
    根据记忆类型和内容自动判断作用域。

    Args:
        memory_type: 记忆类型（fact、preference、relationship、emotion等）
        content: 记忆内容
        character_id: 角色ID

    Returns:
        str: 作用域（global/character/relationship）
    """
    content = str(content or "")
    memory_type = str(memory_type or "fact")

    # 1. 关系私密关键词 → relationship（最高优先级，防止串）
    for word in RELATIONSHIP_WORDS:
        if word in content:
            return RELATIONSHIP

    # 2. 内容提到当前角色名 → character（角色专属）
    if character_id and character_id != "default" and character_id in content:
        return CHARACTER

    # 3. 明确的用户基础信息 → global（所有角色可见）
    #    ★ 收紧（防记忆串桶）：只保留真正中性的基础信息词。
    #      曾包含"喜欢/讨厌/爱喝/爱看/爱好/工作/住在/养了/有只/名字是"等宽词，
    #      导致"称呼偏好：可叫宝/圣豪""用户喜欢喝牛奶"这类与特定角色互动中
    #      形成的私人偏好被误判为 global，被所有新建角色读到（记忆串桶）。
    #      偏好/习惯类内容现在默认落到第 4 步的 character（角色隔离）。
    _global_hints = [
        "生日", "真名", "过敏", "职业", "家乡", "年龄", "星座",
    ]
    for w in _global_hints:
        if w in content:
            return GLOBAL

    # 4. 默认 character（角色专属，防止串数据）
    return CHARACTER


def classify_and_normalize(memory_type, content, character_id="default"):
    """
    分类并规范化作用域。

    Args:
        memory_type: 记忆类型
        content: 记忆内容
        character_id: 角色ID

    Returns:
        tuple: (scope, character_id)
    """
    scope = classify_scope(memory_type, content, character_id)
    scope = normalize_scope(scope)

    # global 记忆的 character_id 设为 "default"
    if scope == GLOBAL:
        return scope, "default"

    return scope, character_id


# 后续可扩展：LLM 分类器
async def classify_scope_with_llm(memory_type, content, character_id="default"):
    """
    使用 LLM 进行更精确的作用域分类（预留接口）。

    Args:
        memory_type: 记忆类型
        content: 记忆内容
        character_id: 角色ID

    Returns:
        str: 作用域
    """
    # 目前使用规则分类，后续可升级为 LLM 分类
    return classify_scope(memory_type, content, character_id)
