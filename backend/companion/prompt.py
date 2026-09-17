# -*- coding:utf-8 -*-
"""
Companion Prompt v1.0
统一的 Prompt 渲染模块：
  只负责将 CompanionContext 转换为可注入 system prompt 的文本。
  不负责收集数据，数据收集由 controller.py 完成。
"""


def _strip_named_blocks(text: str, headers) -> str:
    """按【标题】切分文本，剔除标题命中 headers（前缀匹配）的子块。

    headers 为空 → 原样返回（默认调用路径零影响）。
    只服务「完全自主模式」：角色卡渲染串把人设本体和后台内置规则拼在一起，
    这里在保留人设本体的同时，把规则类子块摘掉。
    """
    if not text or not headers:
        return text
    try:
        import re as _re
        parts = _re.split(r"(?=【[^】\n]{2,24}】)", str(text))
        keep = []
        for part in parts:
            s = part.lstrip()
            if any(s.startswith(h) for h in headers):
                continue
            keep.append(part)
        return "".join(keep).strip()
    except Exception:
        return text


def render_context(
    context,
    session_id,
    memories=None,
    character_config=None,
    only_keys=None,
    strip_headers=None
):
    """
    将 CompanionContext 渲染为 system prompt 文本。

    Args:
        context: CompanionContext 对象
        session_id: 会话ID
        memories: 额外的记忆（可选）
        only_keys: 只渲染白名单里的子块（None=全部，行为与以前完全一致）。
                   「完全自主模式」用它只保留人设/记忆/发图类子块，
                   key 清单见 chat_logic.FULL_AUTO_COMPANION_KEYS。
        strip_headers: 从 identity/character 文本里剔除"以这些【标题】开头的子块"。
                   角色卡渲染串把人设本体和后台内置规则混在一起，
                   「完全自主模式」用它在保留人设的同时去掉规则，清单见
                   chat_logic.FULL_AUTO_CHARACTER_STRIP_HEADERS。

    Returns:
        str: 可直接注入 system prompt 的文本
    """
    blocks = []

    data = context.export()

    # 按顺序渲染各个上下文维度
    _keys = [
        "identity",
        "character",
        "personality_state",
        "final_personality",
        "feedback_profile",
        # ★ 用户纠正过的事实性知识。排在 memory 之前：先看「用户教过我什么」，
        #   再去读记忆，避免被记忆里那条旧的、已被纠正的错误信息带偏。
        "corrections",
        "multimodal_prompt",   # ★ 新增：图片描述角色视角，优先级高
        "multimodal",
        "knowledge_graph",
        "reflection",
        "profile",
        "relationship",
        "life_profile",
        "timeline",
        "memory",
        "emotion",
        "ai_state",
        "behavior_pattern",
        "open_loops",
        "behavior",
        "companion_os_scene",
        "intimacy",
    ]
    # ★ only_keys 过滤（保持上面的原始顺序，只做剔除）
    if only_keys:
        _allow = set(only_keys)
        _keys = [k for k in _keys if k in _allow]
    for key in _keys:
        value = data.get(key)

        if not value:
            continue

        # identity 已经是格式化好的字符串，直接使用
        if key == "identity" and isinstance(value, str):
            blocks.append(_strip_named_blocks(value, strip_headers))
            continue

        # reflection 已经是格式化好的字符串，直接使用
        if key == "reflection" and isinstance(value, str):
            blocks.append(value)
            continue

        # knowledge_graph 已经是格式化好的字符串，直接使用
        if key == "knowledge_graph" and isinstance(value, str):
            blocks.append(value)
            continue

        # ★ multimodal_prompt专门渲染（图片描述角色视角，优先级高）
        if key == "multimodal_prompt" and isinstance(value, str) and value.strip():
            blocks.append(value.strip())
            continue

        # multimodal 是多模态感知信息（如果有multimodal_prompt则跳过，否则用build_prompt兜底）
        if key == "multimodal" and isinstance(value, dict):
            _mp = data.get("multimodal_prompt", "")
            if _mp:
                continue   # 已经由multimodal_prompt处理了
            from ..multimodal.manager import MultimodalContext, MultimodalManager
            mm_context = MultimodalContext()
            mm_context.data = value
            mm_prompt = MultimodalManager().build_prompt(mm_context)
            if mm_prompt:
                blocks.append(mm_prompt)
            continue

        # feedback_profile 是用户反馈偏好，使用专门的渲染函数
        if key == "feedback_profile" and isinstance(value, dict):
            from ..feedback.learner import FeedbackLearner
            feedback_prompt = FeedbackLearner().build_feedback_prompt(value)
            if feedback_prompt:
                blocks.append(feedback_prompt)
            continue

        # corrections 已是格式化好的文本（由 feedback.corrections 生成），直接用
        if key == "corrections" and isinstance(value, str) and value.strip():
            blocks.append(value.strip())
            continue

        # ★ profile 是用户画像，使用角色视角专门渲染（而非数据库转储）
        if key == "profile" and isinstance(value, dict):
            _char_data = data.get("character")
            _char_name = ""
            if isinstance(_char_data, dict):
                _char_name = str(_char_data.get("name", "") or "")
            _rendered = build_profile_prompt(value, _char_name)
            if _rendered:
                blocks.append(_rendered)
            continue

        # final_personality 是最终人格参数，使用专门的渲染函数
        if key == "final_personality" and isinstance(value, dict):
            from ..personality.validator import get_personality_style_guide
            # 修复：data["character"] 可能存在但值为 None
            character_data = data.get("character")
            if isinstance(character_data, dict):
                character_id = character_data.get("name", "default")
            else:
                character_id = "default"
            style_guide = get_personality_style_guide(value, character_id)
            if style_guide:
                blocks.append("【人格风格指南】\n" + style_guide)
            continue

        # personality_state 是自适应人格状态，使用专门的渲染函数
        if key == "personality_state" and isinstance(value, dict):
            from ..personality.strategy import build_personality_prompt
            # ★ 修复：character_config 读 dict，base_personality 读 str
            character_config = character_config or data.get("character", {})      # 兼容旧调用与新调用
            base_personality = character_config.get("personality", "") if isinstance(character_config, dict) else ""
            personality_prompt = build_personality_prompt(
                base_personality=base_personality,
                state=value,
                character_config=character_config             # 传真 dict，底色块正常渲染
            )
            if personality_prompt:
                blocks.append(personality_prompt)
            continue

        # ★ character key 改读 character_prompt（str），不再读 dict 当文本拼
        if key == "character":
            char_prompt = data.get("character_prompt", "")
            if char_prompt:
                # ★ 完全自主模式：角色卡渲染串里混着"后台内置规则"子块，按标题剔除，只留人设
                char_prompt = _strip_named_blocks(char_prompt, strip_headers)
                if char_prompt:
                    blocks.append(char_prompt)
            continue

        # behavior_pattern 是行为模式列表，使用专门的渲染函数
        if key == "behavior_pattern" and isinstance(value, list):
            from ..behavior.strategy import build_behavior_prompt
            behavior_prompt = build_behavior_prompt(value)
            if behavior_prompt:
                blocks.append(behavior_prompt)
            continue

        # life_profile 是档案列表，使用专门的渲染函数
        if key == "life_profile" and isinstance(value, list):
            from ..profile_memory.prompt import build_life_profile_prompt
            profile_prompt = build_life_profile_prompt(value)
            if profile_prompt:
                blocks.append(profile_prompt)
            continue

        # timeline 是事件列表，使用专门的渲染函数
        if key == "timeline" and isinstance(value, list):
            from ..timeline.manager import TimelineManager
            timeline_prompt = TimelineManager().build_timeline_prompt(value)
            if timeline_prompt:
                blocks.append(timeline_prompt)
            continue

        # ai_state 是嵌套字典，使用专门的渲染函数
        if key == "ai_state" and isinstance(value, dict):
            from .ai_state.strategy import build_ai_behavior_prompt
            # 从已导出的 context 里取 CompanionOS 路由场景（Step2 注入），
            # 与心情交叉生成动态心法，绝不替角色写死台词
            scene_val = data.get("companion_os_scene", {}) or {}
            scene_name = scene_val.get("scene", "normal_chat") if isinstance(scene_val, dict) else "normal_chat"
            ai_prompt = build_ai_behavior_prompt(value, scene=scene_name)
            if ai_prompt:
                blocks.append(ai_prompt)
            continue

        # companion_os_scene 是路由结果字典，提取场景并转为 AI 行为指南
        if key == "companion_os_scene" and isinstance(value, dict):
            scene_name = value.get("scene", "normal_chat")
            scene_guide = {
                "emotion_support": "【当前场景：情绪支持】用户需要共情，先接住情绪再说话，禁止冷冰冰分析",
                "memory_recall":   "【当前场景：回忆】自然承接记忆点，不编造没发生的事，像老朋友唠嗑",
                "relationship":    "【当前场景：关系互动】可适度亲密撒娇，呼应情感信号，推进羁绊",
                "advice_seeking":  "【当前场景：求助】先理解处境再给可落地思路，别端着",
                "celebration":     "【当前场景：喜悦】陪用户开心并真诚夸赞，共享高光",
                "normal_chat":     "【当前场景：闲聊】保持自然陪伴感，别硬凹造型",
            }
            guide = scene_guide.get(scene_name, "")
            if guide:
                blocks.append(guide)
            continue

        # 字典类型：格式化输出
        if isinstance(value, dict):
            formatted = _format_dict(value)
            blocks.append(f"【{key}】\n{formatted}")
        # 列表类型：格式化输出
        elif isinstance(value, list):
            formatted = _format_list(value)
            blocks.append(f"【{key}】\n{formatted}")
        # 字符串类型：直接输出
        elif isinstance(value, str):
            blocks.append(f"【{key}】\n{value}")
        # 其他类型：转字符串
        else:
            blocks.append(f"【{key}】\n{str(value)}")

    if not blocks:
        return ""

    # ★ 统一 token 预算截断守卫（补丁五）
    # 目标：system prompt 不超过模型上下文的 50%（保留对话历史空间）
    # 简单用字符数估算（中文1字≈1token，英文1字≈0.3token）
    MAX_SYSTEM_CHARS = 12000   # 约8k token，适配32k模型

    full_text = "\n\n".join(blocks)
    if len(full_text) > MAX_SYSTEM_CHARS:
        # 更稳的截断方案：从末尾丢低优先级块（blocks 已按 key 列表优先级排好序）
        _blocks = list(blocks)
        while len("\n\n".join(_blocks)) > MAX_SYSTEM_CHARS and len(_blocks) > 3:
            _blocks.pop()        # 丢最后一块（最低优先级）
        full_text = "\n\n".join(_blocks)
        if len(full_text) > MAX_SYSTEM_CHARS:
            full_text = full_text[:MAX_SYSTEM_CHARS]

    return full_text


def _format_dict(data):
    """格式化字典为可读文本"""
    lines = []
    for key, value in data.items():
        if not value:
            continue
        if isinstance(value, list):
            value_str = "、".join(str(v) for v in value if v)
        else:
            value_str = str(value)
        if value_str:
            lines.append(f"{key}：{value_str}")
    return "\n".join(lines)


def _format_list(data):
    """格式化列表为可读文本"""
    lines = []
    for item in data:
        if isinstance(item, dict):
            title = item.get("title", "")
            desc = item.get("description", "")
            if title and desc:
                lines.append(f"- {title}：{desc}")
            elif title:
                lines.append(f"- {title}")
            elif desc:
                lines.append(f"- {desc}")
        else:
            lines.append(f"- {str(item)}")
    return "\n".join(lines)


def _zodiac(month: int, day: int) -> str:
    """按公历生日算星座。"""
    if (month == 1 and day >= 20) or (month == 2 and day <= 18):
        return "水瓶"
    if (month == 2 and day >= 19) or (month == 3 and day <= 20):
        return "双鱼"
    if (month == 3 and day >= 21) or (month == 4 and day <= 19):
        return "白羊"
    if (month == 4 and day >= 20) or (month == 5 and day <= 20):
        return "金牛"
    if (month == 5 and day >= 21) or (month == 6 and day <= 21):
        return "双子"
    if (month == 6 and day >= 22) or (month == 7 and day <= 22):
        return "巨蟹"
    if (month == 7 and day >= 23) or (month == 8 and day <= 22):
        return "狮子"
    if (month == 8 and day >= 23) or (month == 9 and day <= 22):
        return "处女"
    if (month == 9 and day >= 23) or (month == 10 and day <= 23):
        return "天秤"
    if (month == 10 and day >= 24) or (month == 11 and day <= 22):
        return "天蝎"
    if (month == 11 and day >= 23) or (month == 12 and day <= 21):
        return "射手"
    return "摩羯"


def build_profile_prompt(profile: dict, character_name: str = "") -> str:
    """
    用户画像专门渲染函数
    输出角色视角的自然语言，而非数据库转储
    空字段跳过，置信度低于0.5的字段降级为"可能"表述
    """
    if not profile:
        return ""

    # 读置信度（从ext_json解析）+ 新结构化字段
    confidence = 0.7
    ext = {}
    try:
        import json as _json
        ext = _json.loads(profile.get("ext_json", "{}") or "{}")
        confidence = float(ext.get("confidence", 0.7))
    except Exception:
        pass

    # 置信度前缀
    _prefix = "" if confidence >= 0.6 else "可能"

    lines = []
    name = str(profile.get("nickname", "")).strip()

    # 开头：称呼
    if name:
        if character_name:
            lines.append(f"你面前的人叫{name}。")
        else:
            lines.append(f"用户叫{name}。")

    # 性格
    personality = str(profile.get("personality", "")).strip()
    if personality:
        lines.append(f"TA{_prefix}是个{personality}的人。")

    # 职业
    occupation = str(profile.get("occupation", "")).strip()
    if occupation:
        lines.append(f"职业是{_prefix}{occupation}。")

    # 性别
    gender = str(ext.get("gender", "")).strip()
    if gender:
        gender_cn = {"male": "男生", "female": "女生", "other": "其他"}.get(gender, gender)
        lines.append(f"TA是{gender_cn}。")

    # 生日（自动算星座/年龄 + 临近提醒，不要生硬报）
    birthday = str(ext.get("birthday", "")).strip()
    if birthday:
        lines.append(f"TA的生日是{birthday}，合适的时候可以自然关心、准备惊喜。")
        # 星座 + 年龄
        try:
            from datetime import datetime as _dt
            _bd = _dt.strptime(birthday[:10], "%Y-%m-%d")
            _sign = _zodiac(_bd.month, _bd.day)
            _age = _dt.now().year - _bd.year
            lines.append(f"TA是{_sign}座，今年{_age}岁左右。")
            # 生日临近（30天内）
            _today = _dt.now().date()
            _this_year_bd = _bd.replace(year=_today.year).date()
            _days = (_this_year_bd - _today).days
            if 0 <= _days <= 30:
                lines.append(f"TA的生日快到了（还有{_days}天），可以提前准备惊喜、旁敲侧击问想要什么。")
        except Exception:
            pass

    # 城市
    city = str(ext.get("city", "")).strip()
    if city:
        lines.append(f"TA在{city}，聊天时可以结合当地天气/生活。")

    # 兴趣
    hobbies = str(profile.get("hobbies", "")).strip()
    if hobbies:
        lines.append(f"平时喜欢{hobbies}。")

    # 话题偏好
    topics = ext.get("topic_preferences", []) or []
    if isinstance(topics, list) and topics:
        lines.append("TA喜欢聊的话题：" + "、".join(str(t) for t in topics) + "。")

    # 简介
    bio = str(ext.get("bio", "")).strip()
    if bio:
        lines.append("TA的自我介绍：" + bio + "。")

    # 雷区
    dislikes = str(profile.get("dislikes", "")).strip()
    if dislikes:
        lines.append(f"TA不喜欢{dislikes}，跟TA聊天要避开这些。")

    # 沟通习惯
    comm = str(profile.get("communication_style", "")).strip()
    if comm:
        lines.append(f"跟TA沟通时：{comm}。")

    # 情绪特点
    emo = str(profile.get("emotional_traits", "")).strip()
    if emo:
        lines.append(f"情绪上：{emo}。")

    if not lines:
        return ""

    header = f"【你了解的关于TA的事】\n" if character_name else "【用户画像】\n"
    return header + "\n".join(lines)
