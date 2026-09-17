"""
TTS 封装模块 · 支持 provider 抽象
当前支持：azure / edge-tts / minimax / volcengine / gptsovits / rvc / elevenlabs
TTS 失败不影响文字回复（try/except 全包）
"""
import os
import asyncio
import hashlib
import tempfile
import re
import time
import wave
from typing import Optional

from . import config as _config

# ── 音频缓存目录（避免同文本重复生成）
# ★ 2026-09-10 修复（通话"没声音 / 只剩文字"的原因之一）：
#   原来用 os.path.dirname(__file__)/tts_cache —— 打包版里 __file__ 指向
#   PyInstaller 的临时解压目录（_MEIPASS），会在两个地方坑人：
#     · 每次重启都被清空 → 缓存永远命中不了，每条语音都重新合成；
#     · 运行中若该临时目录被系统清理（3GB 的 _MEIxxxx 很容易被
#       "存储感知 / 磁盘清理"盯上），之后写音频就报
#       [Errno 2] No such file or directory →
#       通话 TTS 两条路（cosyvoice / edge-tts）同时全挂，只剩文字。
#   改到可写的持久化用户数据目录；并且写之前自愈式重建，
#   目录被删也能自己恢复，不必重启应用。
_CACHE_DIR = os.path.join(str(_config.DATA_DIR), "tts_cache")


def ensure_cache_dir() -> str:
    """确保缓存目录存在（运行中被删也能自愈）。返回目录路径。"""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except Exception as _e:
        print(f"[TTS] 缓存目录创建失败: {_e}", flush=True)
    return _CACHE_DIR


ensure_cache_dir()


# ── 预设音色库（前端下拉选择用）
# 格式：voice_key -> {label, provider, voice_id, speed, pitch, tags}
PRESET_VOICES = {
    # ── Edge-TTS 免费音色（本地开发/低成本）
    "edge_xiaoxiao":  {"label": "晓晓·温柔甜美", "provider": "edge-tts", "voice_id": "zh-CN-XiaoxiaoNeural", "speed": 1.05, "pitch": 1.05, "tags": ["女", "甜", "软妹"]},
    "edge_yunxi":     {"label": "云希·青年男声", "provider": "edge-tts", "voice_id": "zh-CN-YunxiNeural", "speed": 1.0, "pitch": 1.0, "tags": ["男", "温柔", "青年"]},
    "edge_yunjian":   {"label": "云健·低沉磁性", "provider": "edge-tts", "voice_id": "zh-CN-YunjianNeural", "speed": 0.95, "pitch": 0.95, "tags": ["男", "低沉", "御"]},
    "edge_xiaoyi":    {"label": "晓伊·活泼元气", "provider": "edge-tts", "voice_id": "zh-CN-XiaoyiNeural", "speed": 1.1, "pitch": 1.1, "tags": ["女", "活泼", "元气"]},
    "edge_xiaomo":    {"label": "晓墨·知性冷淡", "provider": "edge-tts", "voice_id": "zh-CN-XiaomoNeural", "speed": 0.95, "pitch": 0.95, "tags": ["女", "冷", "御姐"]},
    "edge_yunyang":   {"label": "云扬·阳光少年", "provider": "edge-tts", "voice_id": "zh-CN-YunyangNeural", "speed": 1.05, "pitch": 1.05, "tags": ["男", "阳光", "少年"]},
    "edge_xiaoshuang": {"label": "晓双·成熟稳重", "provider": "edge-tts", "voice_id": "zh-CN-XiaoshuangNeural", "speed": 0.95, "pitch": 1.0, "tags": ["女", "成熟", "温柔"]},
    # ── Azure 商用音色（换 provider 即激活）
    "azure_xiaochen": {"label": "晓辰·商务女声", "provider": "azure", "voice_id": "zh-CN-XiaochenNeural", "speed": 1.0, "pitch": 1.0, "tags": ["女", "商务", "专业"]},
    "azure_yunfeng":  {"label": "云枫·磁性男声", "provider": "azure", "voice_id": "zh-CN-YunfengNeural", "speed": 0.95, "pitch": 0.95, "tags": ["男", "磁性", "深沉"]},
    "azure_xiaohan":  {"label": "晓涵·温柔治愈", "provider": "azure", "voice_id": "zh-CN-XiaohanNeural", "speed": 1.0, "pitch": 1.05, "tags": ["女", "治愈", "温柔"]},
    # ── 自定义克隆音色（用户上传后自动注册进来）
    # "custom_xxx": {"label":"我的专属音色","provider":"clone","voice_id":"clone_xxx","speed":1.0,"pitch":1.0,"tags":["自定义"]}
}

# ── 新增 Provider 音色（CosyVoice3 / MiniMax / 讯飞 / 火山）
PRESET_VOICES.update({
    # CosyVoice3 本地
    "cosyvoice_default": {
        "label":    "CosyVoice默认女声",
        "provider": "cosyvoice",
        "voice_id": "中文女声",
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["本地", "自然", "情感丰富"],
    },
    "cosyvoice_male": {
        "label":    "CosyVoice默认男声",
        "provider": "cosyvoice",
        "voice_id": "中文男声",
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["本地", "自然", "男声"],
    },
    # MiniMax
    "minimax_female": {
        "label":    "MiniMax情感女声",
        "provider": "minimax",
        "voice_id": "female-shaonv",
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["云端", "情感", "MiniMax"],
    },
    "minimax_male": {
        "label":    "MiniMax情感男声",
        "provider": "minimax",
        "voice_id": "male-qn-qingse",
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["云端", "情感", "MiniMax"],
    },
    # 讯飞
    "xunfei_xiaoyan": {
        "label":    "讯飞晓燕",
        "provider": "xunfei",
        "voice_id": "xiaoyan",
        "speed":    50,
        "pitch":    50,
        "tags":     ["云端", "讯飞", "标准"],
    },
    "xunfei_aisjiuxu": {
        "label":    "讯飞爱斯久绪",
        "provider": "xunfei",
        "voice_id": "aisjiuxu",
        "speed":    50,
        "pitch":    50,
        "tags":     ["云端", "讯飞", "情感"],
    },
    # 火山TTS（字节）
    "volcengine_zh_female": {
        "label":    "火山情感女声",
        "provider": "volcengine",
        "voice_id": "zh_female_shuangkuaisisi_moon_bigtts",
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["云端", "火山", "情感"],
    },
    "volcengine_zh_male": {
        "label":    "火山情感男声",
        "provider": "volcengine",
        "voice_id": "zh_male_rap_moon_bigtts",
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["云端", "火山", "情感"],
    },
})


def get_preset_voices() -> list:
    """返回前端下拉列表用的音色列表"""
    voices = []
    for k, v in PRESET_VOICES.items():
        item = {"key": k, **v}
        tags = [str(x) for x in (item.get("tags") or [])]
        is_clone = (
            str(k).startswith(("clone_", "custom_"))
            or "克隆" in tags
            or "自定义" in tags
            or bool(item.get("ref_audio"))
        )
        if is_clone:
            item["is_clone"] = True
            provider = str(item.get("provider") or "").lower()
            item["clone_engine_label"] = {
                "cosyvoice": "CosyVoice3",
                "gptsovits": "GPT-SoVITS",
                "rvc": "RVC",
                "elevenlabs": "ElevenLabs",
            }.get(provider, provider or "自定义")
            if "克隆" not in tags:
                tags.append("克隆")
            if "自定义" not in tags:
                tags.append("自定义")
            item["tags"] = tags
        voices.append(item)
    return voices


def get_voice_cfg(voice_key: str) -> dict:
    """按 key 取音色配置，不存在返回空（降级只发文字）"""
    return PRESET_VOICES.get(voice_key, {})


# ★ 括号动作描写剥离：关闭「括号动作描写」时，所有 （…）/ (…) /【…】/[…]/｛…｝/{…} 一律移除
_BRACKET_PATTERNS = (
    r"（[^（）]*）",
    r"\([^()]*\)",
    r"【[^【】]*】",
    r"\[[^\[\]]*\]",
    r"｛[^｛｝]*｝",
    r"\{[^{}]*\}",
)
_BRACKET_PAIRS = (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"), ("｛", "｝"), ("{", "}"))


def strip_bracket_actions(text: str) -> str:
    """移除所有括号包裹的动作/神态/心理描写，只保留纯对话文本。

    用于「括号动作描写」开关关闭时（用户不希望看到 (低头玩手指) 这类描写）。
    连续执行两遍以处理简单的嵌套残留，并丢弃流式输出中未闭合的括号残尾
    （如"（轻轻"），避免半截描写被显示或朗读。
    """
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not value.strip():
        return ""
    for _ in range(2):
        for pattern in _BRACKET_PATTERNS:
            value = re.sub(pattern, " ", value)
    for opener, closer in _BRACKET_PAIRS:
        if value.count(opener) > value.count(closer):
            pos = value.rfind(opener)
            if pos >= 0:
                value = value[:pos]
    value = re.sub(r"[ \t]{2,}", " ", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip(" \n\t")


def prepare_tts_text(text: str) -> str:
    """Extract only dialogue that should be spoken aloud."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not value.strip():
        return ""
    value = re.sub(r"```[\s\S]*?```", " ", value)
    # Tool/action/sticker markers are UI/control metadata, never spoken.
    value = re.sub(r"\[ACTION\][\s\S]*?\[/ACTION\]", " ", value, flags=re.I)
    value = re.sub(r"\[(?:sticker|emoji|image|voice|audio)\s*:[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"\[/?(?:ACTION|SYSTEM|TOOL|MEMORY|STATE)[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"<\/?(?:think|thinking|analysis|action|tool|system)[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"<(?:think|thinking|analysis|action|tool|system)[^>]*>[\s\S]*?</(?:think|thinking|analysis|action|tool|system)>", " ", value, flags=re.I)

    labelled = re.compile(r"^\s*(?:对话|台词|说话|回复|口中|嘴上|说)\s*[：:]\s*(.+?)\s*$")
    dialogue_lines = []
    for line in value.split("\n"):
        match = labelled.match(line)
        if match:
            dialogue_lines.append(match.group(1))
    if dialogue_lines:
        value = "\n".join(dialogue_lines)

    hidden_line = re.compile(
        r"^\s*(?:[-*#>]\s*)?(?:内心(?:独白|想法|OS)?|心里(?:话|想法)?|"
        r"心里想|心理(?:活动|描写)?|行动|动作|神态|表情|旁白|状态|场景|系统(?:提示|消息)?|"
        r"AI\s*(?:正在|状态|心情))\s*[：:]",
        re.I,
    )
    value = "\n".join(line for line in value.split("\n") if not hidden_line.match(line))
    # Some models put hidden labels inline instead of at line start.
    value = re.sub(
        r"(?:内心(?:独白|想法|OS)?|心里(?:话|想法|想)?|心理(?:活动|描写)?|"
        r"行动|动作|神态|表情|旁白|状态|场景)\s*[：:][^\n。！？!?]*",
        " ",
        value,
        flags=re.I,
    )
    for pattern in (
        r"（[^（）]*）", r"\([^()]*\)", r"【[^【】]*】", r"\[[^\[\]]*\]",
        r"｛[^｛｝]*｝", r"\{[^{}]*\}",
    ):
        value = re.sub(pattern, " ", value)
    # Run a second pass for simple nested stage directions.
    value = re.sub(r"（[^（）]*）|\([^()]*\)|【[^【】]*】|\[[^\[\]]*\]|｛[^｛｝]*｝|\{[^{}]*\}", " ", value)
    # Streaming TTS may receive an unfinished bracket fragment like "（轻轻".
    # Drop the dangling fragment instead of reading it aloud.
    for opener, closer in (("（", "）"), ("(", ")"), ("【", "】"), ("[", "]"), ("｛", "｝"), ("{", "}")):
        if value.count(opener) > value.count(closer):
            pos = value.rfind(opener)
            if pos >= 0:
                value = value[:pos]
    value = re.sub(r"(?m)^\s*(?:[-*#>]\s*)+", "", value)
    value = re.sub(r"(?m)^\s*(?:对话|台词|说话|回复)\s*[：:]\s*", "", value)
    value = re.sub(r"\*{1,2}[^*\n]{1,40}\*{1,2}", " ", value)   # ★ 星号动作描写（*愣了一下* / **...**）
    value = value.replace("*", "").replace("__", "").replace("`", "")
    # ★ 清理 emoji 和波浪号等（CosyVoice 不认识这些字符，读到会发音乱/怪）
    value = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F\u200D\u20E3]+", " ", value)
    value = value.replace("~", "").replace("～", "").replace("…", "。")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\s*\n\s*", "，", value)
    return re.sub(r"，{2,}", "，", value).strip(" ，\n\t")


def prepare_visible_text(text: str, action_brackets: bool = True) -> str:
    """Clean control/meta text before showing or saving a chat bubble.

    Unlike ``prepare_tts_text`` this keeps ordinary dialogue punctuation and
    harmless parenthetical stage style when the user enabled it, but removes
    tool markers, hidden labels and raw inner-state blocks.

    ``action_brackets`` 为角色/请求级的「括号动作描写」开关（默认 True）：
      · True  —— 保留普通括号内容，仅剥离明确声明为内心/动作/旁白等隐藏标签的括号；
      · False —— 剥离**所有**括号内容，只留纯对话（用户已关闭括号动作描写）。
    """
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not value.strip():
        return ""
    value = re.sub(r"```[\s\S]*?```", " ", value)
    value = re.sub(r"\[ACTION\][\s\S]*?\[/ACTION\]", " ", value, flags=re.I)
    value = re.sub(r"\[/?(?:SYSTEM|TOOL|MEMORY|STATE)[^\]]*\]", " ", value, flags=re.I)
    value = re.sub(r"<(?:think|thinking|analysis|action|tool|system)[^>]*>[\s\S]*?</(?:think|thinking|analysis|action|tool|system)>", " ", value, flags=re.I)
    value = re.sub(r"<\/?(?:think|thinking|analysis|action|tool|system)[^>]*>", " ", value, flags=re.I)

    hidden_line = re.compile(
        r"^\s*(?:[-*#>]\s*)?(?:内心(?:独白|想法|OS)?|心里(?:话|想法|想)?|"
        r"心理(?:活动|描写)?|行动|动作|神态|表情|旁白|状态|场景|系统(?:提示|消息)?|"
        r"AI\s*(?:正在|状态|心情))\s*[：:]",
        re.I,
    )
    value = "\n".join(line for line in value.split("\n") if not hidden_line.match(line))

    # ★ 括号剥离必须排在下面的「行内标签清理」之前：
    #   否则 `（内心：好开心）你好` 里的标签文字会先被行内清理吃掉，
    #   只留下失配的左括号 —— 旧代码会把这条消息清成 "（"。
    if action_brackets:
        # 开关开启：只剥离明确声明为内心/动作/旁白等隐藏标签的括号，保留普通括号内容。
        value = re.sub(
            r"[（(【\[][^）)】\]]*(?:内心|心里|心理|行动|动作|神态|旁白|系统|状态)[^）)】\]]*[）)】\]]",
            " ",
            value,
            flags=re.I,
        )
    else:
        # ★ 开关关闭：剥离所有括号内容（动作/神态/心理描写），只留纯对话。
        #   旧逻辑只匹配含「内心/动作/神态…」关键词的括号，
        #   导致「（轻轻清了清嗓子，声音软软的带着点害羞）」这类描写漏网。
        value = strip_bracket_actions(value)

    # 行内隐藏标签（"内心：…" 之类）。★ 字符类排除右括号，避免吃掉括号内容造成括号失配。
    value = re.sub(
        r"(?:内心(?:独白|想法|OS)?|心里(?:话|想法|想)?|心理(?:活动|描写)?|"
        r"行动|动作|神态|旁白|状态|场景)\s*[：:][^\n。！？!?）)]*",
        " ",
        value,
        flags=re.I,
    )
    value = re.sub(r"\*{1,2}[^*\n]{1,40}\*{1,2}", " ", value)   # ★ 星号动作描写（*愣了一下* / **...**）
    value = value.replace("*", "").replace("__", "").replace("`", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip(" \n\t")


def apply_persona_to_voice_cfg(cfg: dict, style: str = "auto", personality: str = "", mode: str = "chat") -> dict:
    """将角色性格映射为稳定的发声底色，情绪参数会在此基础上继续叠加。"""
    cfg = dict(cfg or {})
    text = str(personality or "").lower()
    style = str(style or "auto").lower()
    if style == "auto":
        if any(k in text for k in ("温柔", "治愈", "体贴", "软", "安静")):
            style = "gentle"
        elif any(k in text for k in ("活泼", "元气", "开朗", "俏皮", "阳光")):
            style = "bright"
        elif any(k in text for k in ("低沉", "成熟", "稳重", "磁性")):
            style = "deep"
        elif any(k in text for k in ("清冷", "冷淡", "克制")):
            style = "cool"
        else:
            style = "natural"

    params = {
        "gentle":  (0.94, 1.01, "用贴近耳边、轻柔自然、略带气声的口语说，不要播音腔"),
        "bright":  (1.08, 1.06, "用明亮活泼、有笑意的年轻口语说，反应轻快但不要尖叫"),
        "deep":    (0.93, 0.94, "用沉稳磁性、松弛克制的自然口语说，不要刻意压嗓"),
        "cool":    (0.96, 0.97, "用清冷克制、短促自然的口语说，情绪藏在细微停顿里"),
        "natural": (1.00, 1.00, "像真人打电话一样自然松弛地说，避免新闻播报和客服腔"),
    }
    speed_mul, pitch_mul, instruct = params.get(style, params["natural"])
    if mode == "call":
        speed_mul *= 1.03
        instruct += "；这是实时电话，短句、轻停顿、及时回应"

    provider = str(cfg.get("provider") or "edge-tts").lower()
    cfg["speed"] = round(float(cfg.get("speed", 1.0) or 1.0) * speed_mul, 3)
    cfg["pitch"] = round(float(cfg.get("pitch", 1.0) or 1.0) * pitch_mul, 3)
    cfg["persona_voice_style"] = style
    if provider == "cosyvoice":
        cfg["persona_instruct"] = instruct
    elif provider == "azure":
        cfg.setdefault("style", {"gentle": "gentle", "bright": "cheerful", "deep": "calm", "cool": "calm"}.get(style, "general"))
        cfg.setdefault("style_degree", 0.75)
    return cfg


def _cache_key(text: str, voice_id: str, speed: float, pitch: float, variant: str = "") -> str:
    raw = f"{text}|{voice_id}|{speed}|{pitch}|{variant}"
    return hashlib.md5(raw.encode()).hexdigest()


# ── 情绪 → 参数变换表 ────────────────────────────────────────────────────────
# 格式：emotion: (speed_delta, pitch_delta, azure_style, edge_style_tag)
_EMOTION_PARAMS = {
    # 正向情绪
    "happy":       (+0.08, +0.05, "cheerful",     "cheerful"),
    "excited":     (+0.15, +0.10, "excited",       "excited"),
    "loving":      (-0.03, +0.05, "affectionate",  None),
    "tender":      (-0.05, +0.03, "gentle",        None),
    "playful":     (+0.10, +0.08, "friendly",      "friendly"),
    # 中性
    "calm":        ( 0.00,  0.00, "general",       None),
    # 负向情绪
    "worried":     (-0.05, -0.03, "whispering",    None),
    "sad":         (-0.10, -0.05, "sad",           "sad"),
    "upset":       (-0.08, -0.03, "whispering",    None),
    "angry":       (+0.12, -0.08, "angry",         "angry"),
    "cold":        (-0.08, -0.05, "disgruntled",   None),
    # 复合情绪
    "reconciling": (-0.03, +0.02, "gentle",        None),
}

# 强度阈值（intensity < 0.35 时情绪参数缩减50%，避免轻微情绪过度影响音色）
_INTENSITY_THRESHOLD = 0.35


def apply_emotion_to_voice_cfg(
    cfg: dict,
    emotion: str,
    intensity: float = 0.5
) -> dict:
    """
    把情绪状态映射到voice_cfg参数。
    各provider差异化处理：
    - CosyVoice/MiniMax：支持原生emotion字段
    - 讯飞/火山：通过speed/pitch模拟
    - edge-tts：通过style/speed/pitch模拟
    """
    cfg = dict(cfg)  # 浅拷贝，不污染原始配置
    provider = cfg.get("provider", "edge")

    # ── CosyVoice：用instruct模式传情感文本
    COSYVOICE_EMOTION_INSTRUCT = {
        "happy":    "用开心愉快的语气说",
        "sad":      "用难过低落的语气说",
        "angry":    "用生气的语气说",
        "fearful":  "用担忧的语气说",
        "surprised":"用惊喜的语气说",
        "calm":     "",
        "excited":  "用兴奋激动的语气说",
        "tender":   "用温柔轻声的语气说",
    }
    if provider == "cosyvoice":
        instruct = COSYVOICE_EMOTION_INSTRUCT.get(emotion, "")
        persona_instruct = str(cfg.get("persona_instruct") or "").strip()
        combined = "；".join(x for x in (persona_instruct, instruct) if x)
        if combined:
            cfg["emotion_tag"] = combined
        return cfg

    # ── MiniMax：直接传emotion字段
    if provider == "minimax":
        cfg["emotion"] = emotion
        return cfg

    # ── 火山：传emotion字段
    if provider == "volcengine":
        cfg["emotion"] = emotion
        return cfg

    # ── 讯飞/edge：speed+pitch模拟
    SPEED_MAP = {
        "happy":    1.15, "excited": 1.25, "angry":  1.20,
        "sad":      0.85, "fearful": 0.90, "calm":   1.00,
        "tender":   0.90, "loving": 0.92, "playful": 1.12,
        "worried":  0.92, "upset": 0.90, "cold": 0.92,
        "reconciling": 0.94, "surprised": 1.10,
    }
    PITCH_MAP = {
        "happy":    1.10, "excited": 1.15, "angry":  0.95,
        "sad":      0.90, "fearful": 0.95, "calm":   1.00,
        "tender":   1.05, "loving": 1.04, "playful": 1.08,
        "worried":  0.97, "upset": 0.96, "cold": 0.95,
        "reconciling": 1.02, "surprised": 1.10,
    }
    base_speed = float(cfg.get("speed", 1.0) or 1.0)
    base_pitch = float(cfg.get("pitch", 1.0) or 1.0)
    emotion_speed = SPEED_MAP.get(emotion, 1.0)
    emotion_pitch = PITCH_MAP.get(emotion, 1.0)
    blend = min(1.0, max(0.0, float(intensity)))
    cfg["speed"] = round(base_speed * (1 + (emotion_speed - 1) * blend), 3)
    cfg["pitch"] = round(base_pitch * (1 + (emotion_pitch - 1) * blend), 3)

    return cfg


async def generate_audio(
    text: str,
    voice_cfg: dict,
    base_url: str = ""
) -> Optional[str]:
    """
    输入文本 + voice 配置 → 输出音频 URL（相对路径）或 None（失败）
    voice_cfg 示例：{"provider":"azure","voice_id":"zh-CN-XiaoxiaoNeural","speed":1.0,"pitch":1.0}
    base_url：前端访问音频文件的 URL 前缀，如 "http://localhost:8000"
    """
    text = prepare_tts_text(text)
    if not text:
        return None
    # provider 优先级：voice_cfg 自带 > 全局 tts_provider（设置页可选）> edge-tts（免费兜底）
    provider = (voice_cfg.get("provider") or _config.tts_provider() or "edge-tts").lower()
    voice_id = voice_cfg.get("voice_id") or "zh-CN-XiaoxiaoNeural"
    speed    = float(voice_cfg.get("speed", 1.0))
    pitch    = float(voice_cfg.get("pitch", 1.0))

    # 缓存命中直接返回（只认非空文件：防止失败残留的 0 字节 mp3 污染缓存，导致前端播放 416）
    variant = "|".join(str(voice_cfg.get(k) or "") for k in (
        "emotion", "emotion_tag", "style", "style_degree", "persona_voice_style",
        "ref_audio", "prompt_text"
    ))
    key      = _cache_key(text, voice_id, speed, pitch, variant)
    # ★ CosyVoice 本地服务返回的是 PCM wav，用 .wav 扩展名（避免"wav 数据 + .mp3 头"让浏览器解码歧义）；
    #   其余 provider 均为 mp3。
    out_ext  = ".wav" if provider in ("cosyvoice", "aliyun") else ".mp3"
    out_file = os.path.join(_CACHE_DIR, f"{key}{out_ext}")
    if os.path.exists(out_file):
        if os.path.getsize(out_file) > 0:
            return f"{base_url}/tts_cache/{key}{out_ext}"
        # 空文件 = 上次生成失败残留，删掉后重新生成
        try:
            os.remove(out_file)
        except Exception:
            pass

    try:
        ensure_cache_dir()   # ★ 目录被删过也能自愈（打包版临时目录会被清理）
        if provider == "edge-tts":
            return await _gen_edge_tts(text, voice_id, speed, pitch, out_file, base_url, key)
        elif provider == "azure":
            return await _gen_azure(
                text, voice_id, speed, pitch, out_file, base_url, key,
                style=voice_cfg.get("style"),
                style_degree=float(voice_cfg.get("style_degree", 1.0))
            )
        elif provider == "minimax":
            return await _gen_minimax(text, voice_cfg, out_file)
        elif provider == "volcengine":
            return await _gen_volcengine(text, voice_cfg, out_file)
        elif provider == "cosyvoice":
            # ★ 2026-09-12 惰性策略：CosyVoice 没运行 → 本条不发语音（文字照常送达），
            #   同时后台拉起供后续消息恢复语音；服务在跑才真正合成。
            from . import cosyvoice_mgr
            if not cosyvoice_mgr.port_up():
                try:
                    await cosyvoice_mgr.ensure_async(0)
                except Exception:
                    pass
                print("[TTS/CosyVoice] 服务未运行：本条跳过语音（文字不丢），已后台拉起供后续恢复", flush=True)
                return None
            out = await _gen_cosyvoice(text, voice_cfg, out_file)
            if out:
                cosyvoice_mgr.touch()
            return out
        elif provider == "xunfei":
            return await _gen_xunfei(text, voice_cfg, out_file)
        elif provider == "gptsovits":
            return await _gen_gptsovits(text, voice_cfg, out_file, base_url, key)
        elif provider == "rvc":
            return await _gen_rvc(text, voice_cfg, out_file, base_url, key)
        elif provider == "elevenlabs":
            return await _gen_elevenlabs(text, voice_id, out_file, base_url, key)
        elif provider == "aliyun":
            return await _gen_aliyun(text, voice_cfg, out_file, base_url, key)
        else:
            print(f"[TTS] 未知 provider: {provider}", flush=True)
            return None
    except Exception as e:
        print(f"[TTS] 生成失败({provider}): {e}", flush=True)
        # 清理失败残留的空/残缺文件，避免下次命中空缓存导致 416
        try:
            if os.path.exists(out_file) and os.path.getsize(out_file) == 0:
                os.remove(out_file)
        except Exception:
            pass
        return None


# ── Edge-TTS（免费，无需 API Key，推荐本地开发用）
async def _gen_edge_tts(text, voice_id, speed, pitch, out_file, base_url, key):
    try:
        import edge_tts  # pip install edge-tts
    except ImportError:
        print("[TTS] edge-tts 未安装，请 pip install edge-tts", flush=True)
        return None
    rate  = f"+{int((speed-1)*100)}%" if speed >= 1 else f"-{int((1-speed)*100)}%"
    p_val = f"+{int((pitch-1)*100)}Hz" if pitch >= 1 else f"-{int((1-pitch)*100)}Hz"
    communicate = edge_tts.Communicate(text, voice=voice_id, rate=rate, pitch=p_val)
    await communicate.save(out_file)
    return f"{base_url}/tts_cache/{key}.mp3"


# ── Azure TTS（正式商用推荐），支持 speaking style 情绪
async def _gen_azure(text, voice_id, speed, pitch, out_file, base_url, key,
                     style=None, style_degree=1.0):
    import azure.cognitiveservices.speech as speechsdk  # pip install azure-cognitiveservices-speech
    speech_key    = _config.azure_speech_key()
    speech_region = _config.azure_speech_region() or "eastasia"
    if not speech_key:
        print("[TTS] 未配置 AZURE_SPEECH_KEY（可在设置页「语音合成 Key」填写）", flush=True)
        return None

    # 构建SSML
    rate_pct  = int((speed - 1.0) * 100)
    pitch_pct = int((pitch - 1.0) * 100)
    rate_str  = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
    pitch_str = f"+{pitch_pct}%" if pitch_pct >= 0 else f"{pitch_pct}%"

    # ★ 有style时用mstts:express-as标签
    if style:
        ssml = (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xmlns:mstts="http://www.w3.org/2001/mstts" xml:lang="zh-CN">'
            f'<voice name="{voice_id}">'
            f'<mstts:express-as style="{style}" styledegree="{style_degree}">'
            f'<prosody rate="{rate_str}" pitch="{pitch_str}">{text}</prosody>'
            f'</mstts:express-as></voice></speak>'
        )
    else:
        ssml = (
            f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            f'xml:lang="zh-CN"><voice name="{voice_id}">'
            f'<prosody rate="{rate_str}" pitch="{pitch_str}">{text}</prosody>'
            f'</voice></speak>'
        )
    cfg  = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
    cfg.speech_synthesis_voice_name = voice_id
    audio_cfg    = speechsdk.audio.AudioOutputConfig(filename=out_file)
    synthesizer  = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=audio_cfg)
    # ★ speak_ssml_async().get() 同步阻塞（Azure SDK 内部阻塞 IO），丢线程池避免卡事件循环
    result       = await asyncio.to_thread(lambda: synthesizer.speak_ssml_async(ssml).get())
    if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
        return f"{base_url}/tts_cache/{key}.mp3"
    print(f"[TTS] Azure 合成失败: {result.reason}", flush=True)
    return None


# ── MiniMax TTS（v2：支持emotion原生参数 + 流式）
async def _gen_minimax(text: str, cfg: dict, cache_path: str) -> str:
    """
    MiniMax T2A v2 API。
    需要：MINIMAX_API_KEY + MINIMAX_GROUP_ID（环境变量，或设置页「语音合成 Key」配置）
    """
    import httpx, os, base64

    api_key  = _config.minimax_api_key()
    group_id = _config.minimax_group_id()

    if not api_key or not group_id:
        print("[TTS/MiniMax] 未配置 MINIMAX_API_KEY / MINIMAX_GROUP_ID（可在设置页「语音合成 Key」填写）", flush=True)
        return await _fallback_tts(text, cache_path)

    voice_id = cfg.get("voice_id", "female-shaonv")
    speed    = float(cfg.get("speed", 1.0))
    pitch    = int((float(cfg.get("pitch", 1.0)) - 1.0) * 12)

    EMOTION_MAP = {
        "happy": "happy", "sad": "sad", "angry": "angry",
        "fearful": "fearful", "disgusted": "disgusted",
        "surprised": "surprised", "calm": "neutral",
    }
    emotion    = cfg.get("emotion", "calm")
    mm_emotion = EMOTION_MAP.get(emotion, "neutral")

    payload = {
        "model":   "speech-02-hd",
        "text":    text,
        "stream":  False,
        "voice_setting": {
            "voice_id":  voice_id,
            "speed":     speed,
            "pitch":     pitch,
            "emotion":   mm_emotion,
        },
        "audio_setting": {
            "sample_rate": 24000,
            "bitrate":     128000,
            "format":      "mp3",
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://api.minimaxi.chat/v1/t2a_v2?GroupId={group_id}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )

        data = resp.json()
        audio_b64 = (
            data.get("data", {}).get("audio")
            or data.get("audio_file")
            or ""
        )

        if audio_b64:
            audio_bytes = base64.b64decode(audio_b64)
            with open(cache_path, "wb") as f:
                f.write(audio_bytes)
            return _url_from_path(cache_path)
        else:
            print(f"[TTS/MiniMax] 响应异常: {data}", flush=True)
            return await _fallback_tts(text, cache_path)

    except Exception as e:
        print(f"[TTS/MiniMax] 异常: {e}", flush=True)
        return await _fallback_tts(text, cache_path)


# ── 火山引擎 TTS（v2：支持emotion + 流式）
async def _gen_volcengine(text: str, cfg: dict, cache_path: str) -> str:
    """
    火山引擎TTS HTTP接口。
    需要：VOLCENGINE_APP_ID + VOLCENGINE_ACCESS_TOKEN 环境变量（或设置页「语音合成 Key」配置）
    """
    import httpx, os, base64, json as _json, uuid

    app_id  = _config.volcengine_app_id()
    token   = _config.volcengine_access_token()
    cluster = _config.volcengine_cluster() or "volcano_tts"

    if not app_id or not token:
        print("[TTS/火山] 未配置 VOLCENGINE_APP_ID / ACCESS_TOKEN（可在设置页「语音合成 Key」填写）", flush=True)
        return await _fallback_tts(text, cache_path)

    voice_id = cfg.get("voice_id", "zh_female_shuangkuaisisi_moon_bigtts")
    speed    = float(cfg.get("speed", 1.0))
    pitch    = float(cfg.get("pitch", 1.0))

    emotion  = cfg.get("emotion", "")
    VOLC_EMOTION = {
        "happy": "happiness", "sad": "sadness",
        "angry": "anger", "fearful": "fear", "calm": "",
    }
    volc_emotion = VOLC_EMOTION.get(emotion, "")

    payload = {
        "app": {
            "appid":   app_id,
            "token":   token,
            "cluster": cluster,
        },
        "user":    {"uid": "voice_call_user"},
        "audio": {
            "voice_type":   voice_id,
            "encoding":     "mp3",
            "speed_ratio":  speed,
            "pitch_ratio":  pitch,
            "volume_ratio": 1.0,
        },
        "request": {
            "reqid":     str(uuid.uuid4()),
            "text":      text,
            "text_type": "plain",
            "operation": "query",
        }
    }
    if volc_emotion:
        payload["audio"]["emotion"] = volc_emotion

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://openspeech.bytedance.com/api/v1/tts",
                headers={
                    "Authorization": f"Bearer;{token}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )

        data = resp.json()
        audio_b64 = data.get("data", "")

        if audio_b64:
            audio_bytes = base64.b64decode(audio_b64)
            with open(cache_path, "wb") as f:
                f.write(audio_bytes)
            return _url_from_path(cache_path)
        else:
            print(f"[TTS/火山] 响应异常: {data}", flush=True)
            return await _fallback_tts(text, cache_path)

    except Exception as e:
        print(f"[TTS/火山] 异常: {e}", flush=True)
        return await _fallback_tts(text, cache_path)


# ── GPT-SoVITS 本地推理
async def _gen_gptsovits(text, voice_cfg, out_file, base_url, key):
    """GPT-SoVITS 本地推理（参考音频路径即 voice_id）"""
    import httpx
    api = os.environ.get("GPTSOVITS_API", "http://localhost:9880")
    ref = voice_cfg.get("ref_audio", "")
    params = {
        "refer_wav_path": ref,
        "prompt_text":    "",   # 参考音频文字（可空）
        "prompt_language": "zh",
        "text":           text,
        "text_language":  "zh",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{api}/", params=params)
        with open(out_file, "wb") as f:
            f.write(r.content)
        return f"{base_url}/tts_cache/{key}.mp3"
    except Exception as e:
        print(f"[TTS] GPT-SoVITS 推理失败: {e}", flush=True)
        return None


# ── RVC 本地推理（先用 edge-tts 合成底声，再 RVC 变声）
async def _gen_rvc(text, voice_cfg, out_file, base_url, key):
    """RVC 推理（edge-tts 底声 + RVC 变声）"""
    import httpx
    tmp = out_file + "_raw.mp3"
    base_key = key + "_raw"
    await _gen_edge_tts(text, "zh-CN-XiaoxiaoNeural", 1.0, 1.0, tmp, "", base_key)
    rvc_api = os.environ.get("RVC_API", "http://localhost:7865")
    ref     = voice_cfg.get("ref_audio", "")
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            with open(tmp, "rb") as af:
                r = await c.post(f"{rvc_api}/convert",
                                 files={"audio": af},
                                 data={"model": ref})
        with open(out_file, "wb") as f:
            f.write(r.content)
        return f"{base_url}/tts_cache/{key}.mp3"
    except Exception as e:
        print(f"[TTS] RVC 推理失败: {e}", flush=True)
        return None
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ── ElevenLabs 云端推理
async def _gen_elevenlabs(text, voice_id, out_file, base_url, key):
    """ElevenLabs 云端推理"""
    import httpx
    api_key = _config.elevenlabs_api_key()
    if not api_key:
        print("[TTS] 未配置 ELEVENLABS_API_KEY（可在设置页「语音合成 Key」填写）", flush=True)
        return None
    url  = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    body = {"text": text, "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json=body,
                             headers={"xi-api-key": api_key, "Content-Type": "application/json"})
        with open(out_file, "wb") as f:
            f.write(r.content)
        return f"{base_url}/tts_cache/{key}.mp3"
    except Exception as e:
        print(f"[TTS] ElevenLabs 推理失败: {e}", flush=True)
        return None


# ══════════════════════════════════════════════════════════
# CosyVoice3 本地流式TTS
# ══════════════════════════════════════════════════════════

async def _gen_cosyvoice(text: str, cfg: dict, cache_path: str) -> str:
    """
    CosyVoice3 本地API调用。
    假设本地跑了 CosyVoice3 HTTP API 服务（localhost:9881）。
    """
    import httpx, os

    cosyvoice_url = os.environ.get("COSYVOICE_API", "http://localhost:9881")
    voice_id      = cfg.get("voice_id", "中文女声")
    ref_audio     = cfg.get("ref_audio")
    prompt_text   = str(cfg.get("prompt_text") or "").strip()
    emotion_tag   = cfg.get("emotion_tag", "")

    # ★ cosyvoice 输出 .wav；降级 edge-tts 时是 mp3，必须写 .mp3 路径（否则 mp3 数据顶着 .wav 头）
    fallback_path = cache_path[:-4] + ".mp3" if cache_path.lower().endswith(".wav") else cache_path

    try:
        # ★ 超时放宽到 180s：CosyVoice 首次推理/零样本冷启动可能超过 30s（原来 30s 会 ReadTimeout 误失败）
        async with httpx.AsyncClient(timeout=180) as client:

            if ref_audio and os.path.exists(ref_audio):
                if not prompt_text:
                    print(
                        f"[TTS/CosyVoice] 克隆音色缺少 prompt_text，将降低相似度: {ref_audio}",
                        flush=True,
                    )
                print(
                    f"[TTS/CosyVoice] 使用 zero-shot 克隆音色: ref_audio={ref_audio} "
                    f"prompt_len={len(prompt_text)}",
                    flush=True,
                )
                with open(ref_audio, "rb") as f:
                    ref_bytes = f.read()
                resp = await client.post(
                    f"{cosyvoice_url}/inference_zero_shot",
                    data={
                        "tts_text":    text,
                        "prompt_text": prompt_text,
                        "stream":      "false",
                    },
                    files={
                        "prompt_wav": (
                            os.path.basename(ref_audio), ref_bytes, "application/octet-stream"
                        )
                    },
                )
            elif emotion_tag:
                resp = await client.post(
                    f"{cosyvoice_url}/inference_instruct2",
                    data={
                        "tts_text":      text,
                        "instruct_text": emotion_tag,
                        "spk_id":        voice_id,
                        "stream":        "false",
                    },
                )
            else:
                resp = await client.post(
                    f"{cosyvoice_url}/inference_sft",
                    data={
                        "tts_text": text,
                        "spk_id":   voice_id,
                        "stream":   "false",
                    },
                )

        if resp.status_code == 200 and resp.content:
            content_type = str(resp.headers.get("content-type") or "").lower()
            if resp.content[:1] == b"{" or "application/json" in content_type:
                print(
                    f"[TTS/CosyVoice] 返回非音频内容: {resp.text[:200]}",
                    flush=True,
                )
                return await _fallback_tts(text, fallback_path)
            with open(cache_path, "wb") as f:
                f.write(resp.content)
            return _url_from_path(cache_path)
        else:
            print(
                f"[TTS/CosyVoice] 失败: {resp.status_code} {resp.text[:100]}",
                flush=True
            )
            return await _fallback_tts(text, fallback_path)

    except Exception as e:
        print(f"[TTS/CosyVoice] 异常: {e}", flush=True)
        return await _fallback_tts(text, fallback_path)


async def _gen_cosyvoice_stream(text: str, cfg: dict, chunk_callback):
    """CosyVoice3 流式生成（语音通话专用）"""
    import httpx, os

    cosyvoice_url = os.environ.get("COSYVOICE_API", "http://localhost:9881")
    voice_id      = cfg.get("voice_id", "中文女声")
    ref_audio     = cfg.get("ref_audio")
    prompt_text   = str(cfg.get("prompt_text") or "").strip()
    emotion_tag   = cfg.get("emotion_tag", "")

    try:
        async with httpx.AsyncClient(timeout=180) as client:

            if ref_audio and os.path.exists(ref_audio):
                if not prompt_text:
                    print(
                        f"[TTS/CosyVoice/Stream] 克隆音色缺少 prompt_text，将降低相似度: {ref_audio}",
                        flush=True,
                    )
                print(
                    f"[TTS/CosyVoice/Stream] 使用 zero-shot 克隆音色: ref_audio={ref_audio} "
                    f"prompt_len={len(prompt_text)}",
                    flush=True,
                )
                with open(ref_audio, "rb") as f:
                    ref_bytes = f.read()
                req = client.stream(
                    "POST",
                    f"{cosyvoice_url}/inference_zero_shot",
                    data={"tts_text": text, "prompt_text": prompt_text, "stream": "true"},
                    files={
                        "prompt_wav": (
                            os.path.basename(ref_audio), ref_bytes, "application/octet-stream"
                        )
                    },
                )
            elif emotion_tag:
                req = client.stream(
                    "POST",
                    f"{cosyvoice_url}/inference_instruct2",
                    data={
                        "tts_text": text, "instruct_text": emotion_tag,
                        "spk_id": voice_id, "stream": "true",
                    },
                )
            else:
                req = client.stream(
                    "POST",
                    f"{cosyvoice_url}/inference_sft",
                    data={"tts_text": text, "spk_id": voice_id, "stream": "true"},
                )

            async with req as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    print(
                        f"[TTS/CosyVoice/Stream] 失败: {resp.status_code} {body[:200]!r}",
                        flush=True,
                    )
                    return
                async for chunk in resp.aiter_bytes(chunk_size=4096):
                    if chunk:
                        await chunk_callback(chunk)

    except Exception as e:
        print(f"[TTS/CosyVoice/Stream] 异常: {e}", flush=True)


# ══════════════════════════════════════════════════════════
# 讯飞 TTS（WebSocket协议）
# ══════════════════════════════════════════════════════════

async def _gen_xunfei(text: str, cfg: dict, cache_path: str) -> str:
    """
    讯飞TTS WebSocket接口。
    需要：XUNFEI_APP_ID + XUNFEI_API_KEY + XUNFEI_API_SECRET（环境变量，或设置页「语音合成 Key」配置）
    """
    import os, base64, json as _json, hmac, hashlib
    import websockets
    from datetime import datetime
    from urllib.parse import urlencode

    app_id     = _config.xunfei_app_id()
    api_key    = _config.xunfei_api_key()
    api_secret = _config.xunfei_api_secret()

    if not app_id or not api_key or not api_secret:
        print("[TTS/讯飞] 未配置 XUNFEI_APP_ID/KEY/SECRET（可在设置页「语音合成 Key」填写）", flush=True)
        return await _fallback_tts(text, cache_path)

    voice_id = cfg.get("voice_id", "xiaoyan")
    speed    = int(cfg.get("speed", 50))
    pitch    = int(cfg.get("pitch", 50))

    def _build_auth_url():
        now    = datetime.utcnow()
        date   = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
        host   = "tts-api.xfyun.cn"
        path   = "/v2/tts"
        sig_str = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
        sig_b64 = base64.b64encode(
            hmac.new(api_secret.encode(), sig_str.encode(), hashlib.sha256).digest()
        ).decode()
        auth = base64.b64encode(
            f'api_key="{api_key}", algorithm="hmac-sha256", '
            f'headers="host date request-line", signature="{sig_b64}"'.encode()
        ).decode()
        params = urlencode({"authorization": auth, "date": date, "host": host})
        return f"wss://{host}{path}?{params}"

    ws_url = _build_auth_url()

    req_body = _json.dumps({
        "common": {"app_id": app_id},
        "business": {
            "aue": "lame", "auf": "audio/L16;rate=16000",
            "vcn": voice_id, "speed": speed, "pitch": pitch,
            "volume": 50, "tte": "UTF8",
        },
        "data": {
            "status": 2,
            "text": base64.b64encode(text.encode("utf-8")).decode(),
        }
    })

    audio_chunks = []

    try:
        async with websockets.connect(ws_url) as ws:
            await ws.send(req_body)
            while True:
                raw  = await ws.recv()
                data = _json.loads(raw)
                code = data.get("code", -1)
                if code != 0:
                    print(f"[TTS/讯飞] 错误码: {code} {data.get('message')}", flush=True)
                    break
                audio_b64 = data.get("data", {}).get("audio", "")
                if audio_b64:
                    audio_chunks.append(base64.b64decode(audio_b64))
                status = data.get("data", {}).get("status", 0)
                if status == 2:
                    break

        if audio_chunks:
            with open(cache_path, "wb") as f:
                for chunk in audio_chunks:
                    f.write(chunk)
            return _url_from_path(cache_path)
        else:
            return await _fallback_tts(text, cache_path)

    except Exception as e:
        print(f"[TTS/讯飞] 异常: {e}", flush=True)
        return await _fallback_tts(text, cache_path)


# ══════════════════════════════════════════════════════════
# MiniMax 流式TTS
# ══════════════════════════════════════════════════════════

async def _gen_minimax_stream(text: str, cfg: dict, chunk_callback):
    """MiniMax 流式TTS（语音通话专用）"""
    import httpx, os, base64, json as _json

    api_key  = _config.minimax_api_key()
    group_id = _config.minimax_group_id()

    if not api_key or not group_id:
        print("[TTS/MiniMax/Stream] 未配置 MINIMAX_API_KEY / MINIMAX_GROUP_ID（可在设置页「语音合成 Key」填写）", flush=True)
        return

    voice_id = cfg.get("voice_id", "female-shaonv")
    speed    = float(cfg.get("speed", 1.0))
    pitch    = int((float(cfg.get("pitch", 1.0)) - 1.0) * 12)
    emotion  = cfg.get("emotion", "calm")
    EMOTION_MAP = {
        "happy": "happy", "sad": "sad", "angry": "angry",
        "calm": "neutral", "fearful": "fearful",
    }
    mm_emotion = EMOTION_MAP.get(emotion, "neutral")

    payload = {
        "model": "speech-02-hd",
        "text":  text,
        "stream": True,
        "voice_setting": {
            "voice_id": voice_id, "speed": speed,
            "pitch": pitch, "emotion": mm_emotion,
        },
        "audio_setting": {
            "sample_rate": 24000, "bitrate": 128000, "format": "mp3",
        }
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                f"https://api.minimaxi.chat/v1/t2a_v2?GroupId={group_id}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        chunk_data = _json.loads(raw)
                        audio_b64  = chunk_data.get("data", {}).get("audio") or ""
                        if audio_b64:
                            await chunk_callback(base64.b64decode(audio_b64))
                    except Exception:
                        continue

    except Exception as e:
        print(f"[TTS/MiniMax/Stream] 异常: {e}", flush=True)


# ══════════════════════════════════════════════════════════
# 火山TTS 流式
# ══════════════════════════════════════════════════════════

async def _gen_volcengine_stream(text: str, cfg: dict, chunk_callback):
    """火山TTS 流式（语音通话专用）"""
    import httpx, os, base64, json as _json, uuid

    app_id  = _config.volcengine_app_id()
    token   = _config.volcengine_access_token()
    cluster = _config.volcengine_cluster() or "volcano_tts"

    if not app_id or not token:
        print("[TTS/火山/Stream] 未配置 VOLCENGINE_APP_ID / ACCESS_TOKEN（可在设置页「语音合成 Key」填写）", flush=True)
        return

    voice_id = cfg.get("voice_id", "zh_female_shuangkuaisisi_moon_bigtts")

    payload = {
        "app":     {"appid": app_id, "token": token, "cluster": cluster},
        "user":    {"uid": "voice_call_stream"},
        "audio":   {
            "voice_type":  voice_id,
            "encoding":    "mp3",
            "speed_ratio": float(cfg.get("speed", 1.0)),
        },
        "request": {
            "reqid":     str(uuid.uuid4()),
            "text":      text,
            "operation": "submit",
        }
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream(
                "POST",
                "https://openspeech.bytedance.com/api/v1/tts",
                headers={
                    "Authorization": f"Bearer;{token}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            ) as resp:
                async for chunk in resp.aiter_bytes(4096):
                    if chunk:
                        await chunk_callback(chunk)

    except Exception as e:
        print(f"[TTS/火山/Stream] 异常: {e}", flush=True)


# ══════════════════════════════════════════════════════════
# 流式TTS统一入口
# ══════════════════════════════════════════════════════════

async def generate_audio_stream(text: str, voice_cfg: dict, chunk_callback):
    """
    流式TTS统一入口（语音通话专用）。
    provider自动分发到各自的流式函数。
    不支持流式的provider降级为普通生成后一次性回调。
    """
    text = prepare_tts_text(text)
    if not text:
        return
    provider = voice_cfg.get("provider") or _config.tts_provider() or "edge"

    if provider == "cosyvoice":
        await _gen_cosyvoice_stream(text, voice_cfg, chunk_callback)
    elif provider == "minimax":
        await _gen_minimax_stream(text, voice_cfg, chunk_callback)
    elif provider == "volcengine":
        await _gen_volcengine_stream(text, voice_cfg, chunk_callback)
    elif provider == "aliyun":
        await _gen_aliyun_stream(text, voice_cfg, chunk_callback)
    else:
        # 降级：普通生成后一次性回调
        import tempfile
        tmp = tempfile.mktemp(suffix=".mp3")
        url = await generate_audio(text, voice_cfg, "")
        if url:
            try:
                path = _path_from_url(url)
                with open(path, "rb") as f:
                    await chunk_callback(f.read())
            except Exception as e:
                print(f"[TTS/Stream/降级] 读取失败: {e}", flush=True)


# ══════════════════════════════════════════════════════════
# 通用工具函数
# ══════════════════════════════════════════════════════════

async def _fallback_tts(text: str, cache_path: str) -> str:
    """
    TTS失败时的降级：用edge-tts生成（免费，无需API Key）。
    如果edge-tts也失败，返回None（只发文字）。
    """
    try:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice="zh-CN-XiaoxiaoNeural")
        ensure_cache_dir()   # ★ 目录被删过也能自愈
        await communicate.save(cache_path)
        return _url_from_path(cache_path)
    except Exception as e:
        print(f"[TTS/Fallback] edge-tts降级也失败: {e}", flush=True)
        return None


def _url_from_path(cache_path: str) -> str:
    """把本地缓存路径转成URL相对路径"""
    filename = os.path.basename(cache_path)
    return f"/tts_cache/{filename}"


def _path_from_url(url: str) -> str:
    """把URL相对路径转成本地缓存路径"""
    filename = os.path.basename(url.split("?")[0])
    return os.path.join(_CACHE_DIR, filename)


# ══════════════════════════════════════════════════════════
# 阿里云百炼（DashScope）：云端 TTS + 声音复刻
#   合成：POST /api/v1/services/audio/tts/SpeechSynthesizer
#   流式：请求头加 X-DashScope-SSE: enable → SSE 事件流
#         （sentence-begin / sentence-synthesis[base64音频] / sentence-end）
#   复刻：POST /api/v1/services/audio/tts/customization
#         （Qwen-TTS 接口支持 base64 直传，桌面应用无需公网 URL）
# ══════════════════════════════════════════════════════════

_ALIYUN_TTS_URL = os.environ.get(
    "DASHSCOPE_TTS_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer")
_ALIYUN_CUSTOMIZE_URL = os.environ.get(
    "DASHSCOPE_CUSTOMIZE_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization")

# 未复刻时使用的云端系统音色（复刻后自动改用复刻音色）
_ALIYUN_DEFAULT_VOICE = "longanhuan_v3.6"
# 声音复刻的目标模型（复刻音色只能配这个模型合成）
_ALIYUN_REPLICA_MODEL = "qwen3-tts-vc-realtime-2026-01-15"


def _aliyun_plan(voice_cfg: dict):
    """决定云端合成用的 (model, voice)：
    有复刻音色 → 用复刻音色（声音才是"她"）；否则用配置模型 + 系统音色。
    """
    replica = _config.voice_replica_voice_id()
    if replica:
        return (_config.voice_replica_target_model() or _ALIYUN_REPLICA_MODEL), replica

    model = _config.aliyun_tts_model() or "qwen-audio-3.0-tts-flash"
    vid   = str(voice_cfg.get("voice_id") or "").strip()
    # 本地 CosyVoice 的音色名（"中文女声"）和本地文件路径在云端不认，回退系统音色
    if vid and not vid.startswith("中文") and ("/" not in vid and "\\" not in vid):
        return model, vid
    return model, _ALIYUN_DEFAULT_VOICE


def _aliyun_body(text: str, voice_cfg: dict) -> dict:
    model, voice = _aliyun_plan(voice_cfg)
    # ★ 语音通话真流式：voice_cfg 带 _stream_format="pcm" 时强制裸 PCM（逐块可播），
    #   wav 格式的流式输出首块带头后续是追加数据，前端没法逐块解码
    fmt = str(voice_cfg.get("_stream_format") or _config.aliyun_tts_format() or "wav").lower()
    return {
        "model": model,
        "input": {
            "text":  text,
            "voice": voice,
            "format": fmt,
            "sample_rate": 24000,
            "rate":   float(voice_cfg.get("speed", 1.0) or 1.0),
            "pitch":  float(voice_cfg.get("pitch", 1.0) or 1.0),
            "volume": int(voice_cfg.get("volume", 50) or 50),
        },
    }


async def _gen_aliyun(text: str, voice_cfg: dict, out_file: str,
                      base_url: str = "", key: str = "") -> Optional[str]:
    """阿里云百炼云端语音合成（非流式）"""
    api_key = _config.dashscope_api_key()
    if not api_key:
        print("[TTS/阿里云] 未配置 DASHSCOPE_API_KEY（设置页「阿里云百炼」填写）", flush=True)
        return None

    body = _aliyun_body(text, voice_cfg)

    # ★ 复刻音色（qwen3-tts-vc-realtime 系列）只能走 realtime WebSocket：
    #   老端点会回 InvalidParameter: url error。这里把流式 PCM 收齐后写成 wav 缓存。
    _model, _voice = _aliyun_plan(voice_cfg)
    if "realtime" in str(_model).lower():
        print(f"[TTS/阿里云/实时] 合成: model={_model} voice={_voice} text={text[:20]!r}",
              flush=True)
        pcm = bytearray()

        async def _collect(chunk: bytes):
            pcm.extend(chunk)

        try:
            n = await asyncio.wait_for(
                _qwen_tts_realtime(text, _model, _voice, voice_cfg, _collect), timeout=40.0)
        except Exception as e:
            print(f"[TTS/阿里云/实时] 异常: {type(e).__name__}: {e}", flush=True)
            return None
        if not n:
            print("[TTS/阿里云/实时] 未收到音频", flush=True)
            return None
        ensure_cache_dir()
        with wave.open(out_file, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(pcm))
        print(f"[TTS/阿里云/实时] 完成 {n}B -> {os.path.basename(out_file)}", flush=True)
        return _url_from_path(out_file)

    print(f"[TTS/阿里云] 合成: model={body['model']} voice={body['input']['voice']} "
          f"text={text[:20]!r}", flush=True)
    try:
        import httpx, base64
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(_ALIYUN_TTS_URL, json=body, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            })
            if r.status_code != 200:
                print(f"[TTS/阿里云] 失败 {r.status_code}: {r.text[:200]}", flush=True)
                return None
            data  = r.json()
            audio = ((data.get("output") or {}).get("audio") or {})
            url   = audio.get("url")          # 非流式返回音频 URL（24 小时有效）
            if url:
                ar = await c.get(url, timeout=60)
                if ar.status_code == 200 and ar.content:
                    with open(out_file, "wb") as f:
                        f.write(ar.content)
                    return _url_from_path(out_file)
            if audio.get("data"):
                with open(out_file, "wb") as f:
                    f.write(base64.b64decode(audio["data"]))
                return _url_from_path(out_file)
            print(f"[TTS/阿里云] 返回无音频: {str(data)[:200]}", flush=True)
            return None
    except Exception as e:
        print(f"[TTS/阿里云] 异常: {type(e).__name__}: {e}", flush=True)
        return None


# ══════════════════════════════════════════════════════════
# Qwen-TTS-Realtime（WebSocket）：复刻音色只能走这条
#   ★ 2026-09-10：复刻音色的模型是 qwen3-tts-vc-realtime 系列，
#     WebSocket 地址固定 wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=<model>，
#     旧实现把它 POST 到 /api/v1/services/audio/tts/SpeechSynthesizer（老 CosyVoice 端点），
#     服务端直接回 InvalidParameter: url error → 通话 TTS 每次都失败、只能退回本地/edge-tts。
#   实测（复刻音色 + commit 模式 + pcm/24k）：建连 0.33s、首块音频 0.60s、RTF≈0.98。
# ══════════════════════════════════════════════════════════
_ALIYUN_TTS_WS_URL = os.environ.get(
    "DASHSCOPE_TTS_WS_URL", "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")


async def _qwen_tts_realtime(text: str, model: str, voice: str, voice_cfg: dict,
                             on_pcm) -> int:
    """用 Qwen-TTS-Realtime 流式合成一句话，边收边把裸 PCM 交给 on_pcm(bytes)。

    返回累计收到的 PCM 字节数（0 表示没拿到音频）。
    """
    import base64 as _b64
    import json as _json
    import uuid as _uuid
    import websockets

    api_key = _config.dashscope_api_key()
    if not api_key:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY")

    url = f"{_ALIYUN_TTS_WS_URL}?model={model}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        ctx = websockets.connect(url, additional_headers=headers, open_timeout=12,
                                 ping_interval=None, max_size=None)
    except TypeError:      # 老版本 websockets 用 extra_headers
        ctx = websockets.connect(url, extra_headers=headers, open_timeout=12,
                                 ping_interval=None, max_size=None)

    def _eid() -> str:
        return "event_" + _uuid.uuid4().hex[:20]

    total = 0
    got_any = False
    async with ctx as ws:
        # 1) 等 session.created
        while True:
            ev = _json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
            if ev.get("type") == "error":
                raise RuntimeError(f"realtime 建连错误: {ev.get('error')}")
            if ev.get("type") == "session.created":
                break
        # 2) 配置会话（commit 模式：由我们决定何时开始合成，延迟最低）
        await ws.send(_json.dumps({
            "event_id": _eid(), "type": "session.update",
            "session": {
                "voice": voice,
                "mode": "commit",
                "language_type": "Chinese",
                "response_format": "pcm",
                "sample_rate": 24000,
            },
        }))
        while True:
            ev = _json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
            if ev.get("type") == "error":
                raise RuntimeError(f"session.update 失败: {ev.get('error')}")
            if ev.get("type") == "session.updated":
                break
        # 3) 送文本并提交合成
        await ws.send(_json.dumps({"event_id": _eid(),
                                   "type": "input_text_buffer.append", "text": text}))
        await ws.send(_json.dumps({"event_id": _eid(),
                                   "type": "input_text_buffer.commit"}))
        # 4) 收音频
        while True:
            try:
                ev = _json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            except asyncio.TimeoutError:
                break
            et = ev.get("type")
            if et == "response.audio.delta":
                chunk = _b64.b64decode(ev.get("delta") or "")
                if chunk:
                    total += len(chunk)
                    got_any = True
                    try:
                        await on_pcm(chunk)
                    except Exception as _cb_e:
                        print(f"[TTS/阿里云/实时] 回调失败: {_cb_e}", flush=True)
            elif et in ("response.audio.done", "response.done"):
                break
            elif et == "error":
                raise RuntimeError(f"合成错误: {ev.get('error')}")
        # 5) 收尾（不阻塞主流程）
        try:
            await ws.send(_json.dumps({"event_id": _eid(), "type": "session.finish"}))
        except Exception:
            pass
    return total if got_any else 0


async def _gen_aliyun_stream(text: str, voice_cfg: dict, chunk_callback):
    """阿里云百炼流式 TTS（语音通话专用）：边生成边推送音频块"""
    api_key = _config.dashscope_api_key()
    if not api_key:
        print("[TTS/阿里云/流式] 未配置 DASHSCOPE_API_KEY", flush=True)
        return

    model, voice = _aliyun_plan(voice_cfg)

    # ★ 复刻音色（qwen3-tts-vc-realtime 系列）必须走 realtime WebSocket
    if "realtime" in str(model).lower():
        t0 = time.time()
        first = [0.0]

        async def _cb(chunk: bytes):
            if not first[0]:
                first[0] = time.time() - t0
                print(f"[TTS/阿里云/实时] 首块 {first[0]:.2f}s (voice={voice})", flush=True)
            await chunk_callback(chunk)

        print(f"[TTS/阿里云/实时] 合成: model={model} text={text[:20]!r}", flush=True)
        try:
            n = await asyncio.wait_for(
                _qwen_tts_realtime(text, model, voice, voice_cfg, _cb), timeout=30.0)
            print(f"[TTS/阿里云/实时] 完成 {n}B 用时={time.time() - t0:.2f}s", flush=True)
        except asyncio.TimeoutError:
            print("[TTS/阿里云/实时] 超时30s", flush=True)
        except Exception as e:
            print(f"[TTS/阿里云/实时] 异常: {type(e).__name__}: {e}", flush=True)
        return

    body = _aliyun_body(text, voice_cfg)
    print(f"[TTS/阿里云/流式] 合成: model={body['model']} text={text[:20]!r}", flush=True)
    try:
        import httpx, base64, json as _json
        headers = {
            "Authorization":   f"Bearer {api_key}",
            "Content-Type":    "application/json",
            "X-DashScope-SSE": "enable",
        }
        async with httpx.AsyncClient(timeout=60) as c:
            async with c.stream("POST", _ALIYUN_TTS_URL, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread())[:200]
                    print(f"[TTS/阿里云/流式] 失败 {resp.status_code}: {err!r}", flush=True)
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        ev = _json.loads(payload)
                    except Exception:
                        continue
                    out = ev.get("output") or {}
                    b64 = (out.get("audio") or {}).get("data")
                    if b64:
                        try:
                            await chunk_callback(base64.b64decode(b64))
                        except Exception as _cb_e:
                            print(f"[TTS/阿里云/流式] 回调失败: {_cb_e}", flush=True)
                    if out.get("finish_reason") == "stop":
                        break
    except Exception as e:
        print(f"[TTS/阿里云/流式] 异常: {type(e).__name__}: {e}", flush=True)


# ── 声音复刻：上传参考音频 → 生成云端专属音色 ──────────────────────
async def create_voice_replica(audio_path: str, preferred_name: str = "guzi",
                               text: str = "") -> dict:
    """
    用助手的参考音频在云端复刻音色（base64 直传，桌面应用无需公网 URL）。
    返回 {"ok": True, "voice_id": "...", ...} 或 {"ok": False, "error": "..."}
    """
    api_key = _config.dashscope_api_key()
    if not api_key:
        return {"ok": False, "error": "未配置阿里云百炼 API Key"}
    if not audio_path or not os.path.exists(audio_path):
        return {"ok": False, "error": f"参考音频不存在: {audio_path}"}

    try:
        import base64, mimetypes
        with open(audio_path, "rb") as f:
            raw = f.read()
        if len(raw) > 20 * 1024 * 1024:
            return {"ok": False, "error": "参考音频过大（上限 20MB）"}
        mime     = mimetypes.guess_type(audio_path)[0] or "audio/wav"
        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode()
    except Exception as e:
        return {"ok": False, "error": f"读取音频失败: {e}"}

    target_model = _ALIYUN_REPLICA_MODEL
    # 音色名前缀只允许数字/字母/下划线（≤16字符），中文标签要先过滤掉
    safe_name = re.sub(r"[^0-9A-Za-z_]", "", str(preferred_name or ""))[:16] or "voice"
    body = {
        "model": "qwen-voice-enrollment",
        "input": {
            "action":       "create",
            "target_model": target_model,
            "audio":        {"data": data_url},
            "preferred_name": safe_name,
            "language":     "zh",
        },
    }
    if text:
        body["input"]["text"] = str(text)[:500]

    print(f"[复刻] 上传音频: {os.path.basename(audio_path)} {len(raw)}B", flush=True)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(_ALIYUN_CUSTOMIZE_URL, json=body, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            })
            data = r.json() if r.content else {}
            if r.status_code != 200:
                msg = str(data.get("message") or data.get("error") or r.text)[:300]
                print(f"[复刻] 失败 {r.status_code}: {msg}", flush=True)
                return {"ok": False, "error": msg}
            out   = data.get("output") or {}
            voice = out.get("voice") or ""
            if not voice:
                return {"ok": False, "error": f"未返回音色 ID: {str(data)[:200]}"}
            if out.get("fallback_mode"):
                print(f"[复刻] 质量提示: {out.get('fallback_reason')}", flush=True)
            _config.set_voice_replica(voice, target_model, audio_path)
            print(f"[复刻] 成功: voice={voice}", flush=True)
            return {
                "ok": True, "voice_id": voice, "target_model": target_model,
                "fallback": bool(out.get("fallback_mode")),
                "reason":   str(out.get("fallback_reason") or ""),
            }
    except Exception as e:
        print(f"[复刻] 异常: {type(e).__name__}: {e}", flush=True)
        return {"ok": False, "error": str(e)[:300]}


async def list_voice_replicas(page_size: int = 20) -> dict:
    """查询云端已复刻的音色列表"""
    api_key = _config.dashscope_api_key()
    if not api_key:
        return {"ok": False, "error": "未配置阿里云百炼 API Key", "voices": []}
    body = {
        "model": "qwen-voice-enrollment",
        "input": {"action": "list", "page_index": 0, "page_size": page_size},
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(_ALIYUN_CUSTOMIZE_URL, json=body, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            })
            data = r.json() if r.content else {}
            if r.status_code != 200:
                return {"ok": False,
                        "error": str(data.get("message") or r.text)[:200], "voices": []}
            return {"ok": True, "voices": (data.get("output") or {}).get("voice_list") or []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "voices": []}
