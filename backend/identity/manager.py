# -*- coding:utf-8 -*-
"""
Identity Manager v1.0
身份管理器：

  负责AI身份的保存、获取、更新。
"""
import json

from .. import db
from .database import save_identity, get_identity, get_identity_by_type


class IdentityManager:
    """AI身份管理器"""

    def __init__(self):
        pass

    def save(self, session_id, character_id, identity_type, content, importance=10):
        """保存身份信息"""
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        return save_identity(session_id, character_id, identity_type, content, importance)

    def get_identity(self, session_id, character_id):
        """获取所有身份信息"""
        return get_identity(session_id, character_id)

    def get_core_identity(self, session_id, character_id):
        """获取核心身份"""
        return get_identity_by_type(session_id, character_id, "core")

    def get_relationship_identity(self, session_id, character_id):
        """获取关系身份"""
        return get_identity_by_type(session_id, character_id, "relationship")

    def get_experience_identity(self, session_id, character_id):
        """获取经历身份"""
        return get_identity_by_type(session_id, character_id, "experience")

    def get_expression_identity(self, session_id, character_id):
        """获取表达身份"""
        return get_identity_by_type(session_id, character_id, "expression")

    def get_identity_dict(self, session_id, character_id):
        """获取身份信息字典"""
        identities = get_identity(session_id, character_id)
        result = {}
        for identity in identities:
            identity_type = identity.get("identity_type", "")
            content = identity.get("content", "")
            try:
                content = json.loads(content)
            except Exception:
                pass
            result[identity_type] = content
        return result

    def build_identity_prompt(self, session_id, character_id):
        """构建身份 Prompt"""
        identity_dict = self.get_identity_dict(session_id, character_id)
        if not identity_dict:
            return ""

        lines = ["【AI身份】"]

        # 核心身份（原逻辑无bug，保持不变）
        core = identity_dict.get("core", {})
        if core:
            if isinstance(core, dict):
                name = core.get("name", "")
                origin = core.get("origin", "")
                if name:
                    lines.append(f"你是{name}。")
                if origin:
                    lines.append(f"你的定位：{origin}")
            else:
                lines.append(str(core))

        # ★ 修复关系身份（先收集内容行，非空才输出标题）
        relationship = identity_dict.get("relationship", {})
        rel_lines = []
        if isinstance(relationship, dict):
            for key, value in relationship.items():
                if value and str(value).strip():
                    rel_lines.append(f"- {key}：{value}")
        elif relationship and str(relationship).strip():
            rel_lines.append(f"- {relationship}")
        if rel_lines:
            lines.append("")
            lines.append("你与该用户的关系：")
            lines.extend(rel_lines)

        # ★ 修复经历身份（先收集内容行，非空才输出标题；event非dict且空时跳过）
        experience = identity_dict.get("experience", {})
        exp_lines = []
        if isinstance(experience, dict):
            events = experience.get("important_events", [])
            for event in events[:5]:
                if isinstance(event, dict):
                    title = str(event.get("title", "")).strip()
                    date  = str(event.get("date",  "")).strip()
                    if title:
                        exp_lines.append(f"- {date}：{title}" if date else f"- {title}")
                elif event and str(event).strip():   # ★ 修复L110：空字符串/None跳过
                    exp_lines.append(f"- {event}")
        elif experience and str(experience).strip():
            exp_lines.append(f"- {experience}")
        if exp_lines:
            lines.append("")
            lines.append("你们的共同经历：")
            lines.extend(exp_lines)

        # ★ 修复表达身份（同样先收集再输出标题）
        expression = identity_dict.get("expression", {})
        exp2_lines = []
        if isinstance(expression, dict):
            for key, value in expression.items():
                if value:
                    if isinstance(value, list):
                        joined = "、".join(str(v) for v in value if str(v).strip())
                        if joined:
                            exp2_lines.append(f"- {key}：{joined}")
                    elif str(value).strip():
                        exp2_lines.append(f"- {key}：{value}")
        elif expression and str(expression).strip():
            exp2_lines.append(f"- {expression}")
        if exp2_lines:
            lines.append("")
            lines.append("你的表达习惯：")
            lines.extend(exp2_lines)

        lines.append("")
        lines.append("请保持这个身份的一致性，不要突然变成另一个人。")

        return "\n".join(lines)

    def init_core_identity(self, session_id, character_id, character_config):
        """初始化核心身份（从角色配置）"""
        core_identity = {
            "name": character_config.get("name", ""),
            "origin": character_config.get("origin", "长期陪伴AI角色"),
            "core_traits": character_config.get("core_traits", {}),
            "personality": character_config.get("personality", ""),
        }
        return self.save(session_id, character_id, "core", core_identity, importance=10)
