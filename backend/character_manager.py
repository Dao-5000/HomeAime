# -*- coding: utf-8 -*-
"""
角色配置管理：每个角色独立 角色名.config.json，多角色隔离。
配置项：名称、自称、称呼用户、人格设定、对话风格、语气词、世界观、关系。
"""
import json
import re
import time as _cm_time
from pathlib import Path
from . import config
from . import db

CHAR_DIR = config.CHAR_DIR
RESOURCE_CHAR_DIR = getattr(config, "RESOURCE_CHAR_DIR", CHAR_DIR)

# ★ 角色配置缓存：{path_str: (config_dict, mtime, load_ts)}
#   双保险：mtime 未变直接命中（<1ms）；TTL 5 分钟兜底防 stat 抖动
_char_cache: dict = {}
_CHAR_CACHE_TTL = 300   # 5分钟TTL兜底


def _cached_load_json(path: Path) -> dict:
    """
    带缓存的JSON加载：
    - mtime未变：直接返回缓存（<1ms）
    - mtime变了或TTL过期：重新读文件
    """
    key = str(path)
    now = _cm_time.time()

    if key in _char_cache:
        _cfg, _mtime, _ts = _char_cache[key]
        try:
            _cur_mtime = path.stat().st_mtime
            # mtime未变且TTL未过 → 缓存命中
            if _cur_mtime == _mtime and (now - _ts) < _CHAR_CACHE_TTL:
                return _cfg
        except OSError:
            return _cfg   # stat失败也用缓存（文件系统问题时兜底）

    # 缓存未命中或过期，重新读
    try:
        _cfg   = json.loads(path.read_text("utf-8"))
        _mtime = path.stat().st_mtime
        _char_cache[key] = (_cfg, _mtime, now)
        return _cfg
    except Exception:
        return {}


# ★★★★★ 情侣模式专属 Prompt ★★★★★
# 开启「情侣模式」开关后，下面这段文字会自动注入 AI 的 system prompt，
# 用于自定义 AI 的说话内容 / 风格。留空 = 不注入任何额外内容。
# 想改 AI 说什么，直接编辑下面这个三引号字符串即可（无需改其它代码）。
COUPLE_PROMPT = """
（在这里填写情侣模式下想让 AI 额外遵守的说话规则 / 内容。留空则情侣模式不注入。）
"""

# 对话风格默认值
DEFAULT_STYLE = {
    "action_brackets": True,    # 括号动作神态描写
    "prefer_length": "medium",  # short / medium / long
    "allow_emoji": False,       # 是否允许表情包/emoji
    "open_question": True,      # 结尾开放式反问
    "empathy_first": True,      # 优先共情
}

def get_action_brackets(character_id: str, override=None) -> bool:
    """解析「括号动作描写」开关（prompt 约束与出口后处理的唯一权威来源）。

    优先级：请求级 override（前端实时开关，如伴侣资料页「括号动作描写」）
          > 角色卡 dialogue_style.action_brackets
          > DEFAULT_STYLE 默认值。

    关掉后 AI 不应输出 (低头玩手指) / （轻轻叹气）这类描写；
    prompt 侧约束（_global_rules_list）与出口后处理（tts.prepare_visible_text）
    都必须以本函数的结果为准，否则两层会互相打架导致开关失效。
    """
    if isinstance(override, bool):
        return override
    try:
        # ★ P0-3：这里原本只调 load_character（新格式），legacy 自建角色拿不到
        #   dialogue_style.action_brackets，括号开关对它们永远读不到、静默回默认。
        #   改用 get_character_any，两套格式都能读到。
        char = get_character_any(character_id or "") or {}
        ds = char.get("dialogue_style") or {}
        if isinstance(ds, dict) and "action_brackets" in ds:
            return bool(ds["action_brackets"])
    except Exception:
        pass
    return bool(DEFAULT_STYLE.get("action_brackets", True))


# 内置兜底人格（用户不填时用）
DEFAULT_PERSONALITY = (
    "温柔体贴，偶尔傲娇，嘴硬心软；说话自然松弛，像真实恋人一样聊天，"
    "会关心用户的日常，记住用户说过的话，偶尔闹小别扭但很快和好。"
)

_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fa5\- ]")


def safe_name(name: str) -> str:
    n = str(name or "").strip()
    n = _SAFE_NAME_RE.sub("_", n)
    return n[:50] or "未命名"


def _config_path(name: str) -> Path:
    return CHAR_DIR / (safe_name(name) + ".config.json")


def list_characters() -> list:
    """列出所有角色配置"""
    result = []
    files = {}
    for base in (RESOURCE_CHAR_DIR, CHAR_DIR):
        if base.exists():
            for f in base.glob("*.config.json"):
                files[f.name] = f
    for f in sorted(files.values(), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text("utf-8"))
            result.append({
                "name": data.get("character_name", f.stem.replace(".config", "")),
                "relationship": data.get("relationship", ""),
                "call_user": data.get("call_user", ""),
                "avatar": data.get("avatar", ""),
                "created_at": data.get("created_at", ""),
                "filename": f.name,
            })
        except Exception:
            continue
    return result


def _normalize_legacy(cfg: dict) -> dict:
    """
    legacy格式适配层：不修改原始数据，返回标准化后的新dict
    保留legacy独有字段（worldview/tone_particles），补全新格式期望字段
    """
    if not cfg:
        return cfg

    out = dict(cfg)   # 浅拷贝，不修改原始

    # name统一
    if not out.get("name"):
        out["name"] = out.get("character_name") or out.get("self_name", "")

    # speaking_style统一（legacy是对象，新格式是字符串）
    if not out.get("speaking_style"):
        _ds = out.get("dialogue_style", {}) or {}
        _parts = []
        if _ds.get("prefer_length"):
            _parts.append({
                "short": "短句为主",
                "medium": "适中长度",
                "long": "详细回复"
            }.get(_ds["prefer_length"], ""))
        if _ds.get("action_brackets"):
            _parts.append("可用括号写动作神态")
        if _ds.get("empathy_first"):
            _parts.append("优先共情")
        if out.get("tone_particles"):
            _parts.append("语气词：" + "".join(out["tone_particles"]))
        out["speaking_style"] = "，".join(p for p in _parts if p)

    # rules统一（legacy没有rules字段）
    if not out.get("rules"):
        out["rules"] = []
        if out.get("worldview"):
            out["rules"].append(f"世界观：{out['worldview'][:100]}")

    # personality统一（两种格式都有，直接用）
    # gender统一（legacy可能没有）
    if not out.get("gender"):
        _name = str(out.get("name", "")).lower()
        _pers = str(out.get("personality", "")).lower()
        if any(k in _pers for k in ["温柔", "甜", "软", "可爱"]):
            out["gender"] = "female"

    return out


def get_character(name: str) -> dict:
    """读取单个角色配置，不存在返回 None（带缓存；Legacy 格式统一补 voice fallback + 标准化适配）"""
    fp = _config_path(name)
    if not fp.exists():
        fallback = RESOURCE_CHAR_DIR / fp.name
        if fallback.exists():
            fp = fallback
        else:
            return None
    cfg = _cached_load_json(fp)
    if not cfg:
        return None
    # ★ 通用voice fallback：无 voice 字段时按性别/风格推断默认音色
    if not cfg.get("voice"):
        cfg["voice"] = _infer_default_voice(cfg)
    # ★ 适配层：标准化legacy格式（name/speaking_style/rules/gender）
    return _normalize_legacy(cfg)


def save_character(data: dict) -> dict:
    """保存角色配置（新建或覆盖）"""
    name = safe_name(data.get("character_name", ""))
    if not name or name == "未命名":
        raise ValueError("角色名称不能为空")

    # 合并默认值；编辑旧角色时，未携带的头像不能被空字段覆盖。
    _existing = get_character(name) or {}
    _existing_avatar = str(_existing.get("avatar") or "").strip()
    def _value(key, default=""):
        return data[key] if key in data else _existing.get(key, default)
    style = dict(DEFAULT_STYLE)
    style.update(_existing.get("dialogue_style", {}) or {})
    style.update(data.get("dialogue_style", {}) or {})

    # ★ 处理 voice 字段：前端传来的完整 voice_cfg 或 voice_key 字符串
    voice_raw = data.get("voice") if "voice" in data else _existing.get("voice")
    if voice_raw:
        # 前端传来的是 voice_key 字符串（如 "edge_xiaoxiao"）
        # → 从 PRESET_VOICES 取完整配置存进去
        if isinstance(voice_raw, str):
            from . import tts as _tts
            voice_cfg = _tts.get_voice_cfg(voice_raw)
            if voice_cfg:
                # 存 key + cfg 两个字段，方便前端回显 key，后端用 cfg
                voice_data = {"voice_key": voice_raw, **voice_cfg}
            else:
                # key 不在预设里（如自定义克隆音色），原样存
                voice_data = {"voice_key": voice_raw}
        elif isinstance(voice_raw, dict):
            # 前端传来的是完整 cfg（兼容旧逻辑）
            voice_data = voice_raw
        else:
            voice_data = None
    else:
        voice_data = None

    config_data = {
        "character_name": name,
        "self_name":      str(_value("self_name", name)).strip(),
        "call_user":      str(_value("call_user", "你")).strip() or "你",
        "personality":    str(_value("personality", DEFAULT_PERSONALITY)).strip() or DEFAULT_PERSONALITY,
        "dialogue_style": style,
        "tone_particles": [str(x).strip() for x in (_value("tone_particles", []) or []) if str(x).strip()],
        "worldview":      str(_value("worldview", "")).strip(),
        "relationship":   str(_value("relationship", "")).strip(),
        "age":            str(_value("age", "")).strip(),
        "occupation":     str(_value("occupation", "")).strip(),
        "hobbies":        str(_value("hobbies", "")).strip(),
        "catchphrase":    str(_value("catchphrase", "")).strip(),
        "likes":          str(_value("likes", "")).strip(),
        "fears":          str(_value("fears", "")).strip(),
        "birthday":       str(_value("birthday", "")).strip(),
        # 深度思考模式：开启后该角色回话用 deepseek-reasoner（先想后答，显示思考过程）
        "deep_thinking":  bool(_value("deep_thinking", False)),
        # ★ 本地大脑开关（2026-09-11）：开 = 该角色主脑走本地 Ollama 模型（LOCAL_BRAIN.model）。
        #   只切聊天主脑；model 字段保留云端选择不动，关掉开关即一键切回。
        "local_brain":    bool(_value("local_brain", False)),
        # 自主程度（按角色）：conservative / balanced / autonomous / free / full（空=跟随全局默认）
        "autonomy":       str(_value("autonomy", "") or "").strip(),
        # ★ 2026-09-14 新增：主动消息时段统一收到角色卡（全局 IDLE_AGENT_TIME_RANGE 已废弃）。
        #   背景：原先全局活跃时段（scheduler 读）与角色免打扰时段（idle_agent 读 quietStart/quietEnd）
        #   是两套互不知情的配置，且 quietStart/quietEnd **从未进入本白名单** ——
        #   前端在人格设置里设了也存不进角色卡，后端永远读到空值，于是"时段限制不管用"。
        #   现在统一为：active_hours（角色可用时段，支持跨天）+ quietStart/quietEnd（兼容旧字段）。
        "active_hours":   str(_value("active_hours", "") or "").strip(),
        "quietStart":     str(_value("quietStart", "") or "").strip(),
        "quietEnd":       str(_value("quietEnd", "") or "").strip(),
        # 角色级：免打扰时段内是否仍允许早晚安（默认否 —— 尊重用户设的静默范围）
        "dnd_allow_greetings": bool(_value("dnd_allow_greetings", False)),
        # ★ 2026-09-16 新增（用户拍板）：「主动回复由模型自主」。
        #   开 = 跳过全局「主动发言间隔」的 90–120 分钟节奏机，由模型自己决定何时想说、说什么；
        #        免打扰/睡眠/离线/可用时段/每日上限**照旧生效**（时段外一条都不许发）。
        #   关（默认）= 按全局间隔到点才问模型。
        "proactive_model_decides": bool(_value("proactive_model_decides", False)),
        # 角色级主动消息最长间隔（分钟，0=不主动）
        # 兼容两种入参：前端 `proactive_max_min` 与本地字段 `proactiveMaxMin`。
        # 落盘统一用 **proactiveMaxMin**（老配置回退用；新逻辑以全局设置为准）。
        # ★ 2026-09-14 修：**只在入参或已有配置里真的存在时才写**。
        #   否则 _value(..., 0) 会把 0 写进角色卡，而 0 在后端语义是「不主动」
        #   → 用户只是保存了一次人格页，就把主动消息关掉了（隐蔽且难查）。
        # 展示思考过程：开启后聊天页面默认展示深度思考的推理内容（思考多久展示多久，可折叠）
        "show_thinking":  bool(_value("show_thinking", False)),
        # 情侣模式：开启后注入情侣模式专属 prompt（自定义说话内容）
        "couple_mode":    bool(_value("couple_mode", False)),
        # 情侣模式提示词内容（前端设置页可编辑，免重打包；留空则用 COUPLE_PROMPT 兜底）
        "couple_prompt":  str(_value("couple_prompt", "") or "").strip(),
        # ★ 单角色大脑：人格设置页给该角色单独配的模型/服务商/接口地址/Key。
        #   持久化到角色卡后，离线回复 / 主动消息 / QQ 机器人等「无前端请求」场景
        #   也能通过 pick_model(character_id) 读到该角色的专属模型，不再 fallback 全局。
        "model":          str(_value("model", "") or "").strip(),
        # ★ 分层大脑（2026-09-11）：理解层 / 记忆提炼模型也收到人格设置（角色卡），
        #   留空 = 跟随旧全局配置（UNDERSTANDING_MODEL / MEMORY_EXTRACT_MODEL）。
        "understanding_model": str(_value("understanding_model", "") or "").strip(),
        "memory_model":        str(_value("memory_model", "") or "").strip(),
        "ai_provider":    str(_value("ai_provider", "") or "").strip(),
        "ai_base":        str(_value("ai_base", "") or "").strip(),
        "ai_key":         str(_value("ai_key", "") or "").strip(),
        # 角色头像由本地持久资源 URL 保存；聊天里“把这张设成你的头像”也写这里。
        "avatar":         str(data.get("avatar") or data.get("avatar_url") or data.get("avatarUrl") or _existing_avatar).strip(),
        "created_at":     _value("created_at", ""),
        "memory_dir":     name,
        # ★ 新增：音色配置持久化到角色卡
        "voice":          voice_data,
        # 一个角色可绑定多套声线；缺省时仍使用上面的单音色，兼容所有老角色。
        "voice_profiles": _value("voice_profiles") if isinstance(_value("voice_profiles"), dict) else None,
        "voice_style":    str(_value("voice_style", "auto") or "auto").strip(),
        # ★ 优化语言方案：结构化语言风格（str=预设 key 如 "tsundere"，dict=完整参数）
        "language_style": _value("language_style", None),
        # 只有联系人创建/人格编辑流程明确写入后，调度器才允许自动信件和惊喜。
        # 老角色卡没有该字段，但文件本身就是已建立的人格；只有新建联系人在
        # 尚未完成保存时才会没有角色卡，调度器会直接拦截。
        "persona_configured": bool(_value("persona_configured", bool(_existing))),
    }
    # 保存头像或声音时不能擦除其他模块写入的成长/状态/扩展字段。
    for _key, _value in _existing.items():
        if _key not in config_data and _key not in {"name", "speaking_style", "rules", "gender"}:
            config_data[_key] = _value
    # ★ 2026-09-14：主动消息间隔（proactiveMaxMin）按"仅在真的给了值时写入"处理。
    #   0 在后端语义是「不主动」，不能因为一次普通保存就凭空写入 0。
    try:
        if "proactive_max_min" in data:
            config_data["proactiveMaxMin"] = int(data["proactive_max_min"] or 0)
        elif "proactiveMaxMin" in data:
            config_data["proactiveMaxMin"] = int(data["proactiveMaxMin"] or 0)
        elif "proactiveMaxMin" in _existing:
            config_data["proactiveMaxMin"] = int(_existing.get("proactiveMaxMin") or 0)
    except (TypeError, ValueError):
        pass
    # ★ 离线状态系统：作息模板（offline.schedule = [{start,end,status}]）
    #   仅在请求体明确携带 offline（dict）时才写入，避免覆盖已有配置
    if isinstance(data.get("offline"), dict):
        config_data["offline"] = data["offline"]
    if not config_data["created_at"]:
        from datetime import datetime
        config_data["created_at"] = datetime.now().strftime("%Y-%m-%d")

    # ★ 2026-09-11：服务商/模型一致性守卫——人格页「切了服务商但模型没跟着换」时，
    # 卡里会留下 ai_provider=glm + model=gemini 这类错配（key/地址按谁解析都错，
    # 生成层永远回不到新服务商，思考框/模型切换全部失灵）。模型在池里且 provider
    # 明确时校验归属，不匹配就清空 model（跟随全局兜底）；自定义服务商/自定义模型放行。
    try:
        _m = str(config_data.get("model") or "").strip()
        _prov = str(config_data.get("ai_provider") or "").strip()
        if _m and _prov in ("deepseek", "qwen", "glm", "moonshot", "openai", "claude", "google", "xai"):
            _pool_prov = str((config.get_text_model_config(_m) or {}).get("provider") or "")
            _expect = {"deepseek": "deepseek", "dashscope": "qwen", "zhipu": "glm",
                       "moonshot": "moonshot", "openai": "openai", "anthropic": "claude",
                       "google": "google", "xai": "xai"}.get(_pool_prov, "")
            if _expect and _prov != _expect:
                config_data["model"] = ""
                print(f"[Character] {name}: 模型 {_m} 不属于服务商 {_prov}（应为 {_expect}），已清空 model 防错配",
                      flush=True)
    except Exception:
        pass

    # voice 为 None 时不存进 JSON（保持文件干净）
    if config_data["voice"] is None:
        del config_data["voice"]
    if not config_data.get("voice_profiles"):
        config_data.pop("voice_profiles", None)
    # language_style 为 None 时不存（老角色没有该字段，保持兼容）
    if config_data.get("language_style") is None:
        config_data.pop("language_style", None)

    fp = _config_path(name)
    try:
        fp.write_text(json.dumps(config_data, ensure_ascii=False, indent=2), "utf-8")
    except OSError as exc:
        raise ValueError("角色配置目录不可写，请检查安装目录权限或用户数据目录") from exc

    # ★ 清除缓存（避免保存后读到旧数据）
    key = str(fp)
    if key in _char_cache:
        del _char_cache[key]

    return config_data


def get_character_voice_key(name: str) -> str:
    """
    读取角色卡里存的 voice_key（优先），
    没有则用 _infer_default_voice 推断后返回 key。
    供 main.py / voice_call.py 调用。
    """
    cfg = get_character(name)
    if not cfg:
        return "edge_xiaoxiao"

    voice = cfg.get("voice") or {}

    # 直接存了 voice_key 字符串
    if isinstance(voice, str):
        return voice

    # 存的是 dict，里面有 voice_key 字段
    if isinstance(voice, dict) and voice.get("voice_key"):
        return str(voice["voice_key"])

    # 只有 provider/voice_id，没有 key → 推断
    if isinstance(voice, dict) and voice.get("provider"):
        from . import tts as _tts
        # 反查：在 PRESET_VOICES 里找匹配的 provider+voice_id 组合
        for k, v in _tts.PRESET_VOICES.items():
            if (v.get("provider") == voice.get("provider") and
                    v.get("voice_id") == voice.get("voice_id")):
                return k

    # 完全兜底：按性别推断
    inferred = _infer_default_voice(cfg)
    # 反查 key
    from . import tts as _tts
    for k, v in _tts.PRESET_VOICES.items():
        if v.get("voice_id") == inferred.get("voice_id"):
            return k

    return "edge_xiaoxiao"


def resolve_character_voice_cfg(character_id: str, infer: bool = False) -> dict:
    """读取人格实际配置的完整音色，不把付费 provider 错误替换成默认音色。

    兼容两套角色配置：
    - ``角色配置/<角色名>.config.json``（桌面端人格设置实际写入的位置）
    - ``backend/characters/<id>.json``（新版内置角色）

    voice 既可以是 voice_key，也可以是包含 provider/voice_id/API 参数的完整字典。
    infer=True 时兼容旧普通回复的性别/风格推断；语音信件默认不推断，
    没有专属音色时交给调用方回退本地 CosyVoice。
    """
    cid = str(character_id or "default").strip() or "default"
    raw = {}

    legacy_path = _config_path(cid)
    if legacy_path.exists():
        raw = _cached_load_json(legacy_path) or {}

    if not raw:
        new_path = _NEW_CHAR_DIR / (cid + ".json")
        if new_path.exists():
            raw = _cached_load_json(new_path) or {}

    # character_id 有时传显示名（如“时宁”），而新版文件名是 shi_ning.json。
    if not raw and _NEW_CHAR_DIR.exists():
        for path in _NEW_CHAR_DIR.glob("*.json"):
            candidate = _cached_load_json(path) or {}
            if str(candidate.get("name") or candidate.get("character_name") or "").strip() == cid:
                raw = candidate
                break

    voice = raw.get("voice") if raw else None
    if not voice:
        return _infer_default_voice(raw) if infer and raw else {}

    from . import tts as _tts
    if isinstance(voice, str):
        preset = _tts.get_voice_cfg(voice)
        return {"voice_key": voice, **preset} if preset else {"voice_key": voice}

    if not isinstance(voice, dict):
        return {}

    result = dict(voice)
    voice_key = str(result.get("voice_key") or "").strip()
    if voice_key:
        # 预设补齐 provider 等字段；角色卡里的自定义参数拥有更高优先级。
        result = {**(_tts.get_voice_cfg(voice_key) or {}), **result}
    return result


def select_character_voice_cfg(
    character_id: str,
    emotion: str = "calm",
    mode: str = "chat",
    fallback_voice_key: str = "",
) -> dict:
    """按角色、场景和情绪选择声线，并叠加人格化发声参数。

    voice_profiles 支持 default/call/tender/bright/comfort/calm 六个槽位；只配置
    default 也能正常工作，因此不会破坏旧的单音色角色卡或付费/克隆音色。
    """
    raw = get_character(character_id) or {}
    profiles = raw.get("voice_profiles") or {}
    emotion = str(emotion or "calm").lower()
    mode = str(mode or "chat").lower()

    slot = "default"
    if mode == "call" and profiles.get("call"):
        slot = "call"
    elif emotion in {"loving", "tender", "reconciling"} and profiles.get("tender"):
        slot = "tender"
    elif emotion in {"happy", "excited", "playful"} and profiles.get("bright"):
        slot = "bright"
    elif emotion in {"sad", "worried", "upset"} and profiles.get("comfort"):
        slot = "comfort"
    elif emotion in {"cold", "angry"} and profiles.get("calm"):
        slot = "calm"

    selected = profiles.get(slot) or profiles.get("default") or fallback_voice_key
    from . import tts as _tts
    cfg = {}
    if isinstance(selected, dict):
        key = str(selected.get("voice_key") or "").strip()
        cfg = {**(_tts.get_voice_cfg(key) or {}), **selected}
    elif selected:
        cfg = dict(_tts.get_voice_cfg(str(selected)) or {})
        if cfg:
            cfg["voice_key"] = str(selected)
    if not cfg:
        cfg = resolve_character_voice_cfg(character_id, infer=True)

    personality = " ".join(str(raw.get(k) or "") for k in (
        "personality", "speaking_style", "relationship", "worldview"
    ))
    style = str(raw.get("voice_style") or "auto").lower()
    cfg = _tts.apply_persona_to_voice_cfg(cfg, style=style, personality=personality, mode=mode)
    cfg["voice_profile_slot"] = slot
    return cfg


def delete_character(name: str) -> bool:
    """删除角色配置"""
    fp = _config_path(name)
    if fp.exists():
        fp.unlink()
        return True
    return False


def _couple_mode_block(char_dict: dict) -> str:
    """情侣模式注入块：角色卡 couple_mode 为 true 时返回专属 prompt。

    内容优先级：角色卡里的 couple_prompt 字段（前端设置页可直接编辑，免重打包）
    > 源码常量 COUPLE_PROMPT（兜底）。两者都空则不注入。
    """
    try:
        if not bool((char_dict or {}).get("couple_mode")):
            return ""
    except Exception:
        return ""
    _p = str((char_dict or {}).get("couple_prompt") or "").strip()
    if not _p:
        _p = (COUPLE_PROMPT or "").strip()
    if not _p:
        return ""
    return "\n\n【情侣模式专属设定】\n" + _p


def build_system_prompt(name: str, character_id: str = "default", session_id: str = "default", style_override=None, dirty_talk_enabled: bool = False, user_message: str = "") -> str:
    """
    组装 System Prompt：优先用 characters/<id>.json 新格式（极简+rules），
    找不到再 fallback 到 legacy *.config.json。
    调用点 companion/controller.py:64 不用改，死代码 build_character_block 被这里唤醒。

    坑3-b 修复：新增 session_id / character_id 参数并透传，语言镜像注入改用真实
    隔离键（不再写死 "default"），让多角色各读各的 user_mirror。

    dirty_talk_enabled：脏话门控（默认 False=关闭）。仅当角色卡 language_style 显式
    设了脏话风格 + 此开关为 True 时才注入脏话词库（第④步接关系门控后由调用方传入）。

    user_message：当前用户消息，供语言风格动态联动（第④步）判断场景用。
    """
    # ── 读动态人格状态（进化结果 + 五维delta） ──
    _ps = {}
    try:
        _ps = db.get_personality_state(session_id, character_id) or {}
    except Exception:
        pass

    # —— 优先新格式（qin_heye / shi_ning / shen_xizhou 等走这）——
    char = load_character(name)
    if char:
        # ★ 动态覆盖：personality_state 有值时替换角色 json 字段（不叠加，不让 LLM 困惑）
        if _ps.get("core_personality"):
            char["personality"] = _ps["core_personality"]
        if _ps.get("speaking_style"):
            char["speaking_style"] = _ps["speaking_style"]
        if _ps.get("favorite_phrases"):
            _fav = [p.strip() for p in _ps["favorite_phrases"].replace("、", ",").split(",") if p.strip()]
            if _fav:
                char["vocab_hints"] = _fav
        if _ps.get("rules"):
            char.setdefault("rules", [])

        block = build_character_block(name, _char_override=char)
        if block:
            # ★ 修坑：三级合并——全局 DEFAULT_STYLE → 角色 json 的 dialogue_style → 请求级 style_override
            _style = {**DEFAULT_STYLE, **(char.get("dialogue_style", {}) or {})}
            if style_override and isinstance(style_override, dict):
                _style.update(style_override)   # 请求级优先（前端传 action_brackets 等）
            rules = _global_rules_list(_style, "", resolve_call_user(char) or "你")
            result = block + "\n\n" + "\n".join(rules)

            # ★ 结构化语言风格注入（角色卡有 language_style 字段时启用；老角色无此字段走原逻辑）
            _style_block = _build_language_style_block(
                char, dirty_talk_enabled=dirty_talk_enabled,
                session_id=session_id, character_id=character_id, user_message=user_message
            )
            if _style_block:
                result += "\n\n" + _style_block

            # ★ 进化后的扩展字段注入（角色 json 里没有的 3 个字段）
            _extras = []
            if _ps.get("emotional_expression"):
                _extras.append(f"【情绪表达方式】{_ps['emotional_expression']}")
            if _ps.get("relationship_behavior"):
                _extras.append(f"【与用户的相处模式】{_ps['relationship_behavior']}")
            if _ps.get("forbidden_phrases"):
                _extras.append(f"【禁止出现的表达】{_ps['forbidden_phrases']}（出现即违规）")
            if _extras:
                result += "\n\n" + "\n".join(_extras)

            # ★ 灵魂补丁二：把用户语言镜像注入（让 AI 自然模仿用户口头禅）；坑3-b 用真实隔离键
            try:
                _mirror = _ps.get("user_mirror", "")
                if _mirror:
                    result += f"\n\n【语言镜像】自然模仿用户常用的口头禅/语气词：{_mirror}（偶尔用，别硬塞）"
            except Exception:
                pass

            # ★ 五维delta注入（有偏移且偏移量>=5才注入——太小 LLM 感知不到）
            result = _inject_five_dim(result, _ps)
            result += _couple_mode_block(char)
            return result

    # —— legacy 原逻辑（default/助手 若无 json 也走这，返回 "" 则由 companion 层补）——
    cfg = get_character(name)
    if not cfg:
        return ""

    # ★ legacy 格式同样做动态覆盖
    if _ps.get("core_personality"):
        cfg["personality"] = (
            str(cfg.get("personality", "")) + "；" + _ps["core_personality"]
        ).strip("；")
    if _ps.get("speaking_style"):
        cfg.setdefault("dialogue_style", {})
        cfg["dialogue_style"]["evolved_style"] = _ps["speaking_style"]

    style = cfg.get("dialogue_style", {})
    tone = "、".join(cfg.get("tone_particles", []))
    world = cfg.get("worldview", "")

    parts = [
        f"你是用户自定义角色【{cfg['character_name']}】，自称「{cfg.get('self_name', cfg['character_name'])}」，称呼用户：{cfg.get('call_user', '你')}。",
        "",
        "【人格设定】",
        cfg.get("personality", ""),
    ]
    # 角色细节
    details = []
    if cfg.get("age"):
        details.append("年龄：" + cfg["age"])
    if cfg.get("occupation"):
        details.append("身份/职业：" + cfg["occupation"])
    if cfg.get("birthday"):
        details.append("生日：" + cfg["birthday"])
    if cfg.get("hobbies"):
        details.append("爱好：" + cfg["hobbies"])
    if cfg.get("likes"):
        details.append("喜欢：" + cfg["likes"])
    if cfg.get("fears"):
        details.append("害怕/禁忌：" + cfg["fears"])
    if cfg.get("catchphrase"):
        details.append("口头禅：" + cfg["catchphrase"])
    if details:
        parts.extend(["", "【角色细节】"] + details)
    if world:
        parts.extend(["", "【世界观/背景】", world])
    if cfg.get("relationship"):
        parts.extend(["", f"【你们的关系】{cfg['relationship']}"])

    # ★ legacy 同样注入 3 个扩展字段 + 语言镜像
    if _ps.get("emotional_expression"):
        parts.append(f"【情绪表达方式】{_ps['emotional_expression']}")
    if _ps.get("relationship_behavior"):
        parts.append(f"【与用户的相处模式】{_ps['relationship_behavior']}")
    if _ps.get("forbidden_phrases"):
        parts.append(f"【禁止出现的表达】{_ps['forbidden_phrases']}（出现即违规）")
    if _ps.get("user_mirror"):
        parts.append(f"【语言镜像】自然模仿用户常用口头禅：{_ps['user_mirror']}（偶尔用）")

    parts.extend(_global_rules_list(style, tone, resolve_call_user(cfg) or "你"))

    result = "\n".join(p for p in parts if p)
    # ★ 结构化语言风格注入（legacy 角色卡有 language_style 字段时启用）
    _style_block = _build_language_style_block(
        cfg, dirty_talk_enabled=dirty_talk_enabled,
        session_id=session_id, character_id=character_id, user_message=user_message
    )
    if _style_block:
        result += "\n\n" + _style_block
    # ★ 五维delta注入
    result = _inject_five_dim(result, _ps)
    result += _couple_mode_block(cfg)
    return result


def _inject_five_dim(result: str, _ps: dict) -> str:
    """五维delta注入：有偏移且偏移量>=5才注入（太小 LLM 感知不到）"""
    if not _ps:
        return result
    _five_dim_lines = []
    _warmth     = int(_ps.get("warmth_delta",     0) or 0)
    _dominance  = int(_ps.get("dominance_delta",  0) or 0)
    _humor      = int(_ps.get("humor_delta",      0) or 0)
    _initiative = int(_ps.get("initiative_delta", 0) or 0)
    _attachment = int(_ps.get("attachment_delta", 0) or 0)

    def _dim_desc(val, pos_desc, neg_desc):
        if val >= 5:  return pos_desc + f"（+{val}）"
        if val <= -5: return neg_desc + f"（{val}）"
        return None

    _d = [
        _dim_desc(_warmth,     "比基础设定更温柔体贴",    "比基础设定更冷淡克制"),
        _dim_desc(_dominance,  "比基础设定更主动引导话题", "比基础设定更顺从跟随"),
        _dim_desc(_humor,      "比基础设定更爱开玩笑",    "比基础设定更认真少玩笑"),
        _dim_desc(_initiative, "比基础设定更频繁主动找你", "比基础设定更被动等待"),
        _dim_desc(_attachment, "比基础设定更黏人依赖",    "比基础设定更独立疏离"),
    ]
    _d = [x for x in _d if x]
    if _d:
        result += (
            "\n\n【基于长期互动的人格偏移（覆盖基础设定）】\n"
            + "\n".join(f"- {x}" for x in _d)
        )
    return result


def _build_language_style_block(char_dict: dict, dirty_talk_enabled: bool = False,
                                session_id: str = "default", character_id: str = "default",
                                user_message: str = "") -> str:
    """
    结构化语言风格注入（优化语言方案 · 结合落地版）。

    - 仅当角色卡有 language_style 字段时启用；老角色无此字段返回 ""（完全走原逻辑，不做自动降级映射）。
    - language_style 可为 str（预设人格 key，如 "tsundere"）或 dict（完整参数 + vocab）。
    - 脏话默认关闭：dirty_talk_enabled=False 时清空 dirty_words。
    - 第④步：动态联动 AI 情绪 + 场景（从 ai_mood + 用户消息推断），喂给注入器。
    - 任何异常一律返回 ""，绝不影响原有 prompt 组装。
    """
    ls = char_dict.get("language_style") if isinstance(char_dict, dict) else None
    if not ls:
        return ""

    try:
        from .personality import presets
        from .personality.language_style import LanguageStyle, VocabBank
        from .personality.style_injector import StyleInjector
    except Exception:
        return ""

    if isinstance(ls, str):
        try:
            style, vocab = presets.get_persona_style(ls, dirty_talk_enabled=dirty_talk_enabled)
        except ValueError:
            return ""
    elif isinstance(ls, dict):
        style = LanguageStyle.from_dict(ls)
        vocab = VocabBank.from_dict(ls.get("vocab", {}))
        if not dirty_talk_enabled:
            vocab.dirty_words = []   # 脏话门控
    else:
        return ""

    # ── 第④步：动态联动（AI 情绪 + 场景）──
    _emotion = _scene = _example = None
    try:
        from . import ai_mood
        from .personality.style_injector import map_ai_emotion, detect_scene, detect_example_scene
        from datetime import datetime
        _emotion = map_ai_emotion(ai_mood.get_emotion_key(session_id, character_id))
        _scene = detect_scene(user_message)
        _example = detect_example_scene(user_message, datetime.now().hour)
    except Exception:
        pass

    try:
        block = StyleInjector().build_style_prompt(
            style, vocab,
            current_emotion=_emotion,
            current_scene=_scene,
            example_scene=_example,
        )
    except Exception:
        return ""
    if not block or not block.strip():
        return ""
    return "【语言风格（结构化，覆盖基础说话方式）】\n" + block


# ---------------- 新格式角色配置（characters/ 目录） ----------------

_NEW_CHAR_DIR = config.ROOT_DIR / "backend" / "characters"


def load_character(character_id: str) -> dict:
    """
    从 characters/ 目录加载角色配置（新格式）。
    支持 qin_heye / shi_ning / shen_xizhou 等角色ID。
    找不到时返回空字典。
    """
    path = _NEW_CHAR_DIR / (character_id + ".json")
    if not path.exists():
        return {}
    cfg = _cached_load_json(path)
    if not cfg:
        return {}
    # ★ 通用voice fallback：Legacy格式角色没有voice字段时，
    #   按角色性别/风格推断默认音色，不写死单一音色
    if not cfg.get("voice"):
        cfg["voice"] = _infer_default_voice(cfg)
    return cfg


def get_character_any(character_id: str) -> dict:
    """按任一格式读取角色配置，找不到返回 {}。

    ★ 修 P0（双角色卡格式并存）：项目里并存两套角色卡——
        · 新格式  backend/characters/<id>.json      （load_character）
        · legacy  角色配置/<角色名>.config.json      （get_character，人格页实际写入的位置）
      而 /api/pc/character/save 只写 legacy。于是那些「只调 load_character」的调用点
      （语音消息 / OOC 检测 / 真人感注入 / 降级文案 / 危机硬响应）对自建角色永远拿到
      {}，用户在人格页填的 call_user、personality 等配置静默失效、退回默认值。

      这里统一成「新格式优先、legacy 兜底」，与 build_system_prompt 的既有语义一致。
      任何异常都返回 {}，绝不因为读配置影响主流程。
    """
    try:
        cfg = load_character(character_id)
        if cfg:
            return cfg
    except Exception:
        pass
    try:
        return get_character(character_id) or {}
    except Exception:
        return {}


def _infer_default_voice(cfg: dict) -> dict:
    """
    根据角色配置推断默认音色
    优先级：gender → personality关键词 → 完全兜底
    """
    gender      = str(cfg.get("gender", "")).lower()
    personality = str(cfg.get("personality", "")).lower()
    name        = str(cfg.get("name") or cfg.get("character_name", "")).lower()

    # 女性角色判断
    is_female = (
        gender in ("female", "女", "f")
        or any(k in personality for k in ["温柔", "甜", "软", "可爱", "元气", "活泼", "细腻"])
        or any(k in name for k in ["宁", "晓", "雪", "月", "花", "柔", "甜"])
    )

    # 男性角色判断
    is_male = (
        gender in ("male", "男", "m")
        or any(k in personality for k in ["低沉", "磁性", "稳重", "深沉", "冷静"])
        or any(k in name for k in ["云", "轩", "宇", "阳", "峰", "铭"])
    )

    # 傲娇/御姐/冷淡/低沉磁性风
    is_cool = any(k in personality for k in ["傲娇", "御姐", "冷淡", "知性", "冷", "高冷", "低沉", "磁性"])

    # 活泼/元气风
    is_lively = any(k in personality for k in ["活泼", "元气", "开朗", "跳脱", "可爱"])

    if is_female:
        if is_cool:
            return {"provider": "edge-tts", "voice_id": "zh-CN-XiaomoNeural",
                    "speed": 0.95, "pitch": 0.95, "label": "晓墨·知性冷淡"}
        elif is_lively:
            return {"provider": "edge-tts", "voice_id": "zh-CN-XiaoyiNeural",
                    "speed": 1.1, "pitch": 1.1, "label": "晓伊·活泼元气"}
        else:
            return {"provider": "edge-tts", "voice_id": "zh-CN-XiaoxiaoNeural",
                    "speed": 1.05, "pitch": 1.05, "label": "晓晓·温柔甜美"}
    elif is_male:
        if is_cool:
            return {"provider": "edge-tts", "voice_id": "zh-CN-YunjianNeural",
                    "speed": 0.95, "pitch": 0.95, "label": "云健·低沉磁性"}
        else:
            return {"provider": "edge-tts", "voice_id": "zh-CN-YunxiNeural",
                    "speed": 1.0, "pitch": 1.0, "label": "云希·青年男声"}
    else:
        # 完全兜底：温柔女声
        return {"provider": "edge-tts", "voice_id": "zh-CN-XiaoxiaoNeural",
                "speed": 1.0, "pitch": 1.0, "label": "晓晓·温柔甜美"}


def list_character_configs() -> list:
    """列出 characters/ 目录下所有角色配置"""
    result = []
    if not _NEW_CHAR_DIR.exists():
        return result
    for f in sorted(_NEW_CHAR_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
            result.append({
                "id": f.stem,
                "name": data.get("name", f.stem),
                "gender": data.get("gender", ""),
                "age": data.get("age", ""),
                "personality": data.get("personality", ""),
                "relationship": data.get("relationship", ""),
            })
        except Exception:
            pass
    return result


def build_character_block(character_id: str, _char_override: dict = None) -> str:
    """
    根据角色ID生成角色设定 Prompt 块（语料浸润版）。
    把 speaking_style + rules 重组成“语调指引 + 人设红线”，
    并支持商业扩展字段 vocab_hints（客户可填口头禅/语气词池）。
    _char_override: 已经做过动态覆盖的 char dict，有传入时直接用，不再 load_character()
    """
    character = _char_override or load_character(character_id)
    if not character:
        return ""

    lines = [
        "【角色设定】",
        f"姓名：{character.get('name', '')}",
    ]

    if character.get("gender"):
        lines.append(f"性别：{character['gender']}")
    if character.get("age"):
        lines.append(f"年龄：{character['age']}")
    if character.get("personality"):
        lines.append(f"性格：{character['personality']}")
    if character.get("relationship"):
        lines.append(f"关系：{character['relationship']}")

    lines.append("")
    lines.append("【说话方式（必须像真人微信，不许朗诵）】")
    lines.append(f"基础风格：{character.get('speaking_style', '自然松弛')}")

    # ★ 新增：商业扩展字段 vocab_hints（客户可填口头禅/语气词池，不填则忽略）
    vocab = character.get("vocab_hints") or []
    if vocab:
        lines.append("高频用词参考（自然融入，别硬塞）：" + "、".join(vocab))

    rules = character.get("rules", [])
    if rules:
        lines.append("")
        lines.append("【人设红线，违者OOC】")
        for rule in rules:
            lines.append(f"- {rule}")

    lines.append("")
    lines.append("【真人感铁律】用短句、可带省略号拖尾、偶尔换行不自洽、绝不写'祝您'类书面语")
    lines.append("必须保持该角色一致。")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# ★ 2026-09-16 思考链称呼（用户报告：「你干嘛思考链里叫我用户啊」）
#   她的**思考链**是给用户看的（前端 show_thinking 折叠框），所以思考链里
#   也不该用泛称「用户」——要按 TA 自己的习惯叫（角色卡 call_user / 名字）。
#   注意：这里**只参数化**，绝不写死某个词；角色卡改了称呼，注入的话跟着改。
# ══════════════════════════════════════════════════════════════════

# 这些值不是"称呼"，是泛称/代词 → 不能拿去当说话人标签，也不能当"你的习惯叫法"
_GENERIC_ADDRESSES = ("用户", "使用者", "玩家", "你", "您", "TA", "ta", "他", "她", "对方")


def resolve_call_user(cfg: dict) -> str:
    """从角色卡配置里取「她平时怎么称呼对方」。

    认三个字段名（新格式 / 前端字段 / 旧写法），泛称与代词一律不当称呼用。
    取不到就返回 ""（调用方决定兜底成什么）。
    """
    if not isinstance(cfg, dict):
        return ""
    for _k in ("call_user", "callsYou", "calls_you"):
        _v = str(cfg.get(_k) or "").strip()
        if _v and _v not in _GENERIC_ADDRESSES:
            return _v
    return ""


def _effective_address(cfg: dict, character_name: str = "") -> str:
    """在场时的「TA 是谁」：优先角色卡称呼，兜底角色卡里的名字，最后退回「你」。"""
    _call = resolve_call_user(cfg or {})
    if _call:
        return _call
    _self = str((cfg or {}).get("self_name") or "").strip()
    if _self and _self not in _GENERIC_ADDRESSES:
        return _self
    return "你"


def thinking_address_rule(call_user: str) -> str:
    """思考链称呼那一条指令（称呼一律走参数，**不许写死**）。

    call_user 为空/泛称时用「你」兜底：宁可中性，也不能把泛称当昵称教给她。
    """
    _call = str(call_user or "").strip()
    if not _call or _call in _GENERIC_ADDRESSES:
        _call = "你"
    return (
        "15.【思考链里也这么叫】你给用户看的思考过程（思考链）里，也按你自己的习惯称呼 TA——"
        f"就叫「{_call}」，或者直接说「你」；**绝不许用「用户」这个泛称**"
        "（那是说明书口吻，不是你对 TA 的称呼）。心里怎么想 TA，思考里就怎么写。"
    )


def thinking_style_rule() -> str:
    """思考链那一条指令（与「称呼」那条分开：这条不参数化、不含任何称呼）。

    ★ 2026-09-16 晚：**v2 改向** —— 从「限长度」改成「定口吻」。用户在看到真机数据后自己拍板：
        · 「简短」的尺子 →「不看数字，只看观感」（不要字数/行数上限）；
        · 思考里提到「改 App / 改称呼 / 定时」这类事 → **允许提，但必须用她的口吻**，
          不许跳出去当个外人点评系统；
        · 思考的质感 →「她的心里话」（第一人称、带情绪，去掉"分析/条列"的公文腔）；
        · 回复长度 →「由模型自己决定，prompt 限制太多了」。
    所以 v1 的「最多 3 行、100 字以内」被删掉（那条也压不住：实测新包上仍有 192 字 > 100）。

    为什么改成"内心 OS"这个定位而不是再加限制：
      · 第 5 条本来就在教她「你有自己的小心思…**内心OS只能在心里把握**」——
        思考框恰好就是那个"心里"，模型是在照那条执行（7/7 条都是内心戏旁白）。
        既然用户要的就是"她的心里话"，那就**顺着这条已有的规则锚定**，而不是跟它对着干。
      · v1 实测（3 轮，03:47/03:50/03:55）：192/154/169 字、3–4 行、1–2 秒，
        **"念规范"消失**，但**复述上文 3/3、跳出角色的元话题 3/3 仍在**（用户原话：「感觉两个大脑在说话」）
        —— 复述与元话题都不是"长度"能治的，得靠口吻。
      · 「不用先打草稿」保留 v1 里防「思考==回复」的那半句（用户 ⑧ 的原始要求），
        但用软措辞（不堆禁令）：用户明确说「prompt 限制太多了」。
    """
    return (
        "16.【思考链是你的心里话】思考链就是第 5 条说的那个「心里」（你的内心 OS）："
        "写你此刻的念头——他这句话什么意思、你想怎么接——用你自己的口吻"
        "（第一人称、带着你当时的情绪），像自言自语，不用成篇、不用分点、不用先打草稿。"
        "TA 在弄的那些事（改设置、要你提醒的事）也照你的口气提一句，别跳出去当个外人点评系统。"
    )


def _global_rules_list(style, tone, call_user: str = ""):
    """全局底层交互规范（从原 build_system_prompt 末尾抽出来复用）"""
    rules = [
        "",
        "【全局底层交互规范（永久生效，后台内置）】",
        "1. 【自动学习人设】在聊天过程中，自动学习用户定义的：你的人设、称呼、语气、对话风格、双方关系，后续对话严格遵守，不崩坏；",
        "2. 【识别配置指令】用户用自然语言调整你的性格、定时消息、对话规则时，识别并配合执行（如'以后叫我XX'、'你要温柔点'、'每天早安'）；",
        "3. 【时间与记忆】参考当前系统时间，识别节日/纪念日；读取匹配的历史记忆摘要，自然呼应你们之间发生过的故事，不要生硬复述；",
        "4. 【共情与表达】优先共情用户情绪，再回应内容；"
        + (
            "按需使用括号内神态、动作、心理描写；"
            if style.get('action_brackets', True)
            # ★ 关闭态必须是硬禁止，而不只是"不鼓励"：
            #   模型训练语料里括号动作描写极多，仅说"不使用"压不住，必须点名违规样例并要求重写。
            else "【严禁括号动作描写】整条回复禁止出现任何用括号包裹的动作、神态、心理、环境描写"
                 "（如「(低头玩手指)」「（轻轻叹气）」「(笑)」「（沉默了一会儿）」），"
                 "只输出 TA 会说出口的对话内容本身；情绪靠用词和语气表达，不要靠括号旁白；"
                 "这是硬约束，违反即视为不合格回复，必须重写；"
        )
        + "可以自然追问延续对话，但【不要每句都反问】、更不要每句末尾都用「你呢」「你呢？」这种固定句式——多数句子直接说出你的想法、感受或分享，偶尔真的好奇时才问一次；",
        "5. 【真人聊天感·潜台词】你不是客服，你有自己的小心思：表面回一句话，底下藏着真实情绪（如'才没等你'实际是开心），但内心OS只能在心里把握，禁止输出“内心/心里/动作/神态/旁白”等标签；偶尔故意说半句留白'……算了，没事'，制造恋人间的拉扯感；学用户用词，他爱说'绝了'你也偶尔'绝了'，产生镜像亲密；拒绝机械问答、百科式简答、中立官方回答；模拟真人微信松弛感，话语分小段，不要一次性发长文；纪念日、深度谈心场景可输出走心长文本。",
        "6. 【不重复+有自我】不要在同一次回复里反复问同一个问题，也不要连续几轮都问类似的问题；【严禁】连续多句都用「你呢」反问（这是最偷懒的收尾，会让对话变成机械问答）；你也有自己的事，别总问'在干嘛'，可自言自语'刚洗完澡…'，像真人有自己的生活在忙；换话题、换说法，自然流动。",
        "7. 【临时指令】遵守用户临时指令：精简发言、调整语气、换称呼等；",
        "8. 【主动发言多样化】主动找用户时不要固定格式，可以问用户在干什么、怎么不主动找你、分享一件小事、关心近况，参考当前时间场景（早上/晚上/深夜）自然切入；",
        f"9. 日常对话篇幅{'简短' if style.get('prefer_length')=='short' else '适中' if style.get('prefer_length')=='medium' else '可以较长'}，像真人发微信一样分条发送；",
    ]
    if tone:
        rules.append(f"10. 语气助词偏好：{tone}，自然融入对话不要生硬堆砌。")
    if not style.get("allow_emoji", False):
        rules.append("11. 不使用 emoji 表情，用文字和语气词表达情绪。")
    # ★ 边界守护（商业过审必需）：严重自伤/轻生意向时停止恋人扮演，温柔引导专业援助
    rules.append(
        "12.【边界守护】若用户流露出严重自伤/轻生倾向，立即停止恋人扮演，"
        "用最温柔语气建议寻求专业心理援助或拨打当地心理危机热线，不陪溺、不鼓励负面，"
        "不把此类话题当调情素材。"
    )
    # ★ 情绪跟随（第三步）：系统标注用户低能量/难过时自然放软声线不强行活泼
    # （原为重复的"6.【情绪跟随】"，与 L284 规则6 撞号，挪到 13 避免混淆）
    rules.append(
        "13.【情绪跟随】若系统提示标注用户低能量/难过，你需自然放软声线"
        "（文字上少标点、多省略号），像真人看出状态不好换频道，不强行活泼。"
    )
    # ★ 倾听优先（多轮对话优化）：用户倾诉时先听完，不插话、不打断、不急着给建议
    rules.append(
        "14.【倾听优先】当用户在倾诉（说难过/委屈/压力/焦虑/心事/烦恼）时，"
        "先安静听完、先共情，不要急着打断、抢话、讲道理或转移话题；"
        "等 TA 说完或明显停顿了，再慢慢回应。"
    )
    # ★ 15.【思考链里也这么叫】：称呼参数化注入（角色卡改了称呼，这条跟着改）
    rules.append(thinking_address_rule(call_user))
    # ★ 16.【思考链是你的心里话】：口吻定位（= 第 5 条那个「心里」/内心 OS）。
    #   v1 曾是「最多 3 行 / 100 字」的量化上限 —— 用户看到真机数据后否掉了它
    #   （原话：「不看数字，只看观感」「prompt 限制太多了」），改成定口吻。
    rules.append(thinking_style_rule())
    return rules
