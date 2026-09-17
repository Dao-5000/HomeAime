# -*- coding: utf-8 -*-
"""
用户画像管理：
  从用户聊天中自动提取稳定用户画像（性格、职业、兴趣、沟通习惯、雷区、情绪特点）
  存入 user_profile 表，注入 system prompt 让 AI 更了解用户。
"""
import json

from . import db
from .deepseek_api import chat_once
from . import config


PROFILE_SYSTEM_V2 = """
从用户聊天记录中提取稳定的用户画像信息。

提取规则：
- 只提取用户明确表达或高度确信的信息
- 不确定的内容宁可留空，不要猜测
- 每条信息只写核心要点，不超过30字

输出JSON（所有字段均可为空字符串）：
{
  "nickname":            "",   # 用户自称的名字/昵称
  "personality":         "",   # 性格特点（如：温柔但固执，容易焦虑）
  "occupation":          "",   # 职业/学业（如：AI广告从业者，大三学生）
  "hobbies":             "",   # 兴趣爱好（如：看电影、打游戏、健身）
  "dislikes":            "",   # 雷区/明确不喜欢的事（如：被催促、说教）
  "communication_style": "",   # 沟通习惯（如：直接，偶尔用梗，喜欢被认可）
  "emotional_traits":    "",   # 情绪特点（如：容易焦虑但会自我调节）
  "confidence":          0.8   # 本次提取整体置信度 0.0-1.0（信息越明确越高）
}

只输出JSON，不要解释。
"""

# 兼容旧引用
PROFILE_SYSTEM = PROFILE_SYSTEM_V2


def apply_profile(data: dict, session_id, character_id="default", msg_count: int = 0) -> bool:
    """把**已解析好的**画像字典写库（★ 2026-09-15 从 extract_profile 里拆出来）。

    拆出来的原因（省 token ②）：画像/记忆/未完成事项三个抽取器读的是**同一批** messages、
    各自产出一份 JSON、写不同的表 —— 完全可以合并成一次模型调用（见 extract_merged.py）。
    本函数只负责"数据 → 库"这一段，所以合并版和旧的单独版共用同一段落库逻辑，
    不存在"合并后写入行为变了"的风险。
    """
    try:
        if not isinstance(data, dict) or not data:
            return False
        data = dict(data)
        confidence = float(data.pop("confidence", 0.7) or 0.7)
        import time as _time
        ext = {
            "confidence": confidence,
            "source":     "llm_extract",
            "extract_at": int(_time.time()),
            "msg_count":  int(msg_count or 0),
        }
        db.update_profile(
            session_id,
            character_id,    # ★ 修复：传 character_id（多角色隔离）
            ext_json=json.dumps(ext, ensure_ascii=False),
            **data
        )
        return True
    except Exception as e:
        print(f"[Profile] 写库失败: {e}", flush=True)
        return False


async def extract_profile(messages, session_id, character_id="default"):
    """从最近对话中提取用户画像，更新到数据库（按角色隔离）。"""
    key = config.memory_key()
    if not key:
        return

    text = "\n".join(
        [
            f"{m['role']}:{m.get('content', '')}"
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
    )
    if not text.strip():
        return

    _profile_model = config.memory_extract_model(character_id)
    result = await chat_once(
        _profile_model,
        [
            {
                "role": "system",
                "content": PROFILE_SYSTEM_V2
            },
            {
                "role": "user",
                "content": text[-4000:]
            }
        ],
        key,
        temperature=0.1,    # ★ 提取任务用低温度
        max_tokens=512,     # ★ 7字段够用
        # ★ 修复（2026-09-04）：同 SemanticAnalyzer，glm-5.3-flash 不传 reasoning_effort
        #   会 reasoning 占满 token 导致 content 空 → 画像提取失败。
        reasoning_effort=("low" if config.model_supports_reasoning_effort(_profile_model) else None),
    )

    try:
        # 兼容LLM输出带```json```包裹
        _raw = (result or "").strip()
        if _raw.startswith("```"):
            _raw = _raw.split("```")[1]
            if _raw.startswith("json"):
                _raw = _raw[4:]
        data = json.loads(_raw.strip())

        # ★ 2026-09-15：落库逻辑抽到 apply_profile（合并抽取器共用同一段写入逻辑）
        apply_profile(data, session_id, character_id, msg_count=len(messages))
    except Exception as e:
        print(f"[Profile] 提取解析失败: {e}", flush=True)
