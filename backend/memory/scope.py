# -*- coding:utf-8 -*-
"""
Memory Scope v1.0
记忆作用域常量定义：

  分三层：
    1. Global Memory：全局用户记忆，所有角色可见
    2. Character Memory：角色专属记忆，仅当前角色可见
    3. Relationship Memory：关系私密记忆，仅当前角色可见

  解决多角色AI伴侣的记忆隔离问题：
    - 用户和秦赫野的私密互动不会被时宁读取
    - 用户的基础信息（生日、职业、喜好）所有角色共享
"""

# 记忆作用域常量
GLOBAL = "global"
CHARACTER = "character"
RELATIONSHIP = "relationship"

# 合法的作用域列表
VALID_SCOPES = [
    GLOBAL,
    CHARACTER,
    RELATIONSHIP,
]

# 作用域描述
SCOPE_DESCRIPTIONS = {
    GLOBAL: "全局用户记忆，所有角色可见",
    CHARACTER: "角色专属记忆，仅当前角色可见",
    RELATIONSHIP: "关系私密记忆，仅当前角色可见",
}

# 作用域对应的 Prompt 标题
SCOPE_TITLES = {
    GLOBAL: "用户长期信息",
    CHARACTER: "与当前角色相关经历",
    RELATIONSHIP: "当前关系记忆",
}


def is_valid_scope(scope):
    """检查作用域是否合法"""
    return scope in VALID_SCOPES


def normalize_scope(scope):
    """规范化作用域，非法值默认 global"""
    if is_valid_scope(scope):
        return scope
    return GLOBAL
