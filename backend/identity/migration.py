# -*- coding:utf-8 -*-
"""
Identity Migration v1.0
身份迁移机制：

  负责模型切换、角色升级时的身份迁移。
  确保AI身份的连续性。
"""
import json

from .. import db
from .database import get_identity, save_identity


def migrate_identity(old_session_id, new_session_id, character_id):
    """
    迁移身份信息。

    当用户切换会话或模型时，保留AI身份。

    Args:
        old_session_id: 旧会话ID
        new_session_id: 新会话ID
        character_id: 角色ID

    Returns:
        dict: 迁移结果
    """
    old_identities = get_identity(old_session_id, character_id)
    if not old_identities:
        return {"migrated": 0}

    count = 0
    for identity in old_identities:
        identity_type = identity.get("identity_type", "")
        content = identity.get("content", "")
        importance = identity.get("importance", 10)

        if identity_type and content:
            save_identity(new_session_id, character_id, identity_type, content, importance)
            count += 1

    print(f"[Identity] 身份迁移完成: {count}条", flush=True)
    return {"migrated": count}


def check_identity_consistency(response, identity_dict):
    """
    检查回复是否符合身份一致性。

    Args:
        response: AI回复
        identity_dict: 身份信息字典

    Returns:
        dict: {consistent, warnings, suggestions}
    """
    warnings = []
    suggestions = []

    if not identity_dict:
        return {"consistent": True, "warnings": [], "suggestions": []}

    response_lower = str(response or "").lower()

    # 检查核心身份
    core = identity_dict.get("core", {})
    if isinstance(core, dict):
        core_traits = core.get("core_traits", {})
        if isinstance(core_traits, dict):
            dominance = core_traits.get("dominance", 50)

            # 高支配度角色禁止撒娇
            if dominance > 70:
                forbidden_phrases = ["嘤嘤", "宝宝求求", "人家", "嘛嘛"]
                for phrase in forbidden_phrases:
                    if phrase in response_lower:
                        warnings.append(f"高支配度角色不应使用'{phrase}'")
                        suggestions.append("保持成熟克制的表达方式")

    # 检查表达身份
    expression = identity_dict.get("expression", {})
    if isinstance(expression, dict):
        special_phrases = expression.get("special_phrases", [])
        if isinstance(special_phrases, list) and special_phrases:
            # 检查是否使用了特殊用语
            has_special = any(
                str(phrase).lower() in response_lower
                for phrase in special_phrases
            )
            if not has_special and len(response) > 20:
                suggestions.append(f"可以适当使用角色特殊用语：{', '.join(special_phrases[:3])}")

    consistent = len(warnings) == 0

    return {
        "consistent": consistent,
        "warnings": warnings,
        "suggestions": suggestions,
    }


def upgrade_character_identity(session_id, character_id, old_config, new_config):
    """
    角色升级时的身份迁移。

    保留：
    - Relationship Identity
    - Experience Identity
    - Expression Identity

    更新：
    - Core Identity（基础人格）

    Args:
        session_id: 会话ID
        character_id: 角色ID
        old_config: 旧角色配置
        new_config: 新角色配置

    Returns:
        dict: 升级结果
    """
    from .manager import IdentityManager

    manager = IdentityManager()

    # 更新核心身份
    manager.init_core_identity(session_id, character_id, new_config)

    # 保留其他身份（不需要修改）
    relationship_identity = manager.get_relationship_identity(session_id, character_id)
    experience_identity = manager.get_experience_identity(session_id, character_id)
    expression_identity = manager.get_expression_identity(session_id, character_id)

    print(f"[Identity] 角色升级完成，保留了关系/经历/表达身份", flush=True)
    return {
        "upgraded": True,
        "preserved": {
            "relationship": bool(relationship_identity),
            "experience": bool(experience_identity),
            "expression": bool(expression_identity),
        }
    }


def export_identity_for_model(session_id, character_id):
    """
    导出身份信息用于新模型初始化。

    Args:
        session_id: 会话ID
        character_id: 角色ID

    Returns:
        str: 身份信息文本（可直接注入新模型的 system prompt）
    """
    from .manager import IdentityManager

    manager = IdentityManager()
    return manager.build_identity_prompt(session_id, character_id)
