# -*- coding: utf-8 -*-
"""
配置管理：所有可动态修改的配置项，持久化到 backend/data/config.json，重启后保留。
优先级（API Key）：环境变量 DEEPSEEK_API_KEY > 项目根 .env > 项目根 config.json > backend/data/config.json
"""
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：exe 位于 resources/backend/pc_backend.exe
    BACKEND_DIR = Path(sys.executable).resolve().parent
    ROOT_DIR = BACKEND_DIR.parent
else:
    BACKEND_DIR = Path(__file__).resolve().parent
    ROOT_DIR = BACKEND_DIR.parent

# ★ 统一数据目录（2026-09-11）：
#   env AI_COMPANION_DATA_DIR（launcher/Electron 设置）> %APPDATA%/HomeAime/data > backend/data。
#   无 env 时（开发裸跑 run.py / QQ / 脚本会话）不再落 backend/data，而是与打包版 App
#   共用 Electron userData 目录——角色卡/config/db 只有一份，彻底消灭「幽灵副本」。
#   backend/data 保留为历史冷备份，不再被读写。
_user_data_dir = os.environ.get("AI_COMPANION_DATA_DIR", "").strip()
if _user_data_dir:
    DATA_DIR = Path(_user_data_dir).expanduser().resolve()
else:
    _appdata = os.environ.get("APPDATA", "").strip()
    DATA_DIR = (Path(_appdata) / "HomeAime" / "data") if _appdata else (BACKEND_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA_DIR / "config.json"
ENV_FILE = ROOT_DIR / ".env"
ROOT_CONFIG = ROOT_DIR / "config.json"      # 原 Node 项目配置（apiKey 等），继续沿用
LIB_DIR = DATA_DIR / "记忆库"                 # 可写用户记忆目录，更新安装包时不丢失
CHAR_DIR = DATA_DIR / "角色配置"               # 可写用户角色目录
RESOURCE_LIB_DIR = ROOT_DIR / "记忆库"         # 打包内置资源（只读兼容）
RESOURCE_CHAR_DIR = ROOT_DIR / "角色配置"
try:
    CHAR_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Read-only dev/test mounts still need to import the backend.  Reads use
    # bundled resources; writes will return a clear error from the API.
    CHAR_DIR = RESOURCE_CHAR_DIR


def aux_data_dir() -> Path:
    """学习/反馈类附属数据库的落盘目录（feedback.db / behavior.db / memory.db）。

    ★ 2026-09-17 新增（修"重打包即丢数据"）：
      这三个库原先各自硬编码 `ROOT_DIR/backend/data/xxx.db`。
      开发模式下 ROOT_DIR 是工程目录，没问题；但**打包模式下**
      ROOT_DIR = `<安装目录>/resources`，于是它们被写进
      `resources\\backend\\data\\` —— 而 electron-builder 每次重新打包都会
      删除重建 win-unpacked，**学习数据（隐式反馈、行为模式）会随打包一起消失**，
      用户之前已经因为"数据凭空不见"被坑过一次。

      现在统一落到 `DATA_DIR`（也就是 `%APPDATA%\\HomeAime\\data`，与主库
      local_db.db / relationship.db 同目录），备份/迁移一并覆盖。
      兼容：首次调用时若新位置没有库、而旧的 `ROOT_DIR/backend/data` 有，
      自动搬迁一次（复制，不删旧文件，留一份原始证据）。
    """
    try:
        return DATA_DIR
    except Exception:
        return Path.cwd() / "data"


def migrate_aux_db(filename: str) -> Path:
    """把某个附属库从旧位置（ROOT_DIR/backend/data）搬到 DATA_DIR（幂等）。

    返回**应当使用**的路径。搬迁失败不抛异常 —— 宁可继续用旧位置，也不能让
    反馈/行为模块因为一次文件操作失败而整体不可用。

    ★ 2026-09-17 二次修（第一次搬迁踩坑，必须记下来）：
      第一版用 `shutil.copy2` 直接拷文件，结果 `behavior.db` 在新位置变成了
      **0 字节**（旧位置 16384 字节）。原因：源库正被运行中的应用以 WAL 模式打开，
      单纯拷主文件会拿到"还没 checkpoint 的不完整状态"，拷到一半被换掉就是空文件。
      现在改用 **sqlite3 的在线备份 API**（`src.backup(dst)`）：
        · 对正在写的库也一致；
        · 搬完立刻做 `PRAGMA integrity_check`，不通过就删掉残file、保留旧位置可用；
        · 目标已存在但**是空文件**时视为上次的残file，允许重搬一次。
    """
    new_path = aux_data_dir() / filename
    old_path = Path(ROOT_DIR) / "backend" / "data" / filename

    # ★ 2026-09-17 补（测试隔离）：把数据目录指到别处时（AI_COMPANION_DATA_DIR
    #   被测试/新人格验收改过），**不许**再从工程树里的旧库搬数据 ——
    #   否则新人格的"从 0 开始"验收会莫名其妙继承开发树里的历史库
    #   （实测踩过：全新人格的回收站里出现了 434KB 的 memory.db）。
    #   只有"数据目录就是真机目录"时才做搬迁。
    #   需要在这种环境里验证搬迁逻辑时，显式设 AI_COMPANION_FORCE_AUX_MIGRATION=1
    #   （测试用的开关，生产不会设）。
    try:
        _live = (Path(os.environ.get("APPDATA") or "") / "HomeAime" / "data").resolve()
        _forced = str(os.environ.get("AI_COMPANION_FORCE_AUX_MIGRATION") or "").strip() in ("1", "true", "True")
        if not _forced and Path(DATA_DIR).resolve() != _live:
            return new_path
    except Exception:
        pass

    def _looks_broken(p: Path) -> bool:
        try:
            if not p.exists():
                return True
            if p.stat().st_size == 0:
                return True
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            try:
                row = con.execute("PRAGMA integrity_check").fetchone()
                return not (row and str(row[0]).lower() == "ok")
            finally:
                con.close()
        except Exception:
            return True

    try:
        if new_path.exists() and not _looks_broken(new_path):
            return new_path                    # 已经是好的，直接用（幂等）
        if not old_path.exists() or old_path.stat().st_size == 0:
            return new_path if new_path.exists() else new_path

        new_path.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(str(new_path))
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        if _looks_broken(new_path):
            print(f"[Config] 附属库搬迁校验未通过，丢弃残file: {new_path}", flush=True)
            try:
                new_path.unlink()
            except Exception:
                pass
            return old_path if old_path.exists() else new_path

        print(f"[Config] 附属库搬迁: {old_path} -> {new_path}"
              f"（{new_path.stat().st_size} 字节，旧文件保留）", flush=True)
        return new_path
    except Exception as _e:  # noqa: BLE001
        print(f"[Config] 附属库搬迁失败({filename})，继续用旧位置: {_e}", flush=True)
        return old_path if old_path.exists() else new_path


def external_memory_dir() -> Path:
    """外置记忆库目录（日/周/月递归总结 + 聊天原文归档）。

    优先级：config EXTERNAL_MEMORY_DIR > 环境变量 AI_COMPANION_EXTERNAL_MEMORY_DIR
            > 打包模式 DATA_DIR/外置记忆库 > 开发模式 ROOT_DIR/外置记忆库。
    目的：把长期记忆放到可看、可备份、不随 AppData 丢失的地方。
    ★ 打包模式不放 ROOT_DIR：win-unpacked 每次重新打包会被 electron-builder 删除重建
    （外置记忆库会被清空），NSIS 安装目录还可能不可写——默认落到 DATA_DIR（userData）。
    """
    try:
        _cfg = str(_load().get("EXTERNAL_MEMORY_DIR") or "").strip()
        if _cfg:
            return Path(_cfg).expanduser().resolve()
    except Exception:
        pass
    _env = os.environ.get("AI_COMPANION_EXTERNAL_MEMORY_DIR", "").strip()
    if _env:
        return Path(_env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return (DATA_DIR / "外置记忆库").resolve()
    return (ROOT_DIR / "外置记忆库").resolve()


# 官方原生模型 ID 预设（严格官方标识，禁止别名）
# ★ 实测校正（2026-08-30）：经 /api/pc/validate_model 发真实 1-token 请求逐个验证，
#   原列表里的 deepseek-v3 / deepseek-v4 这两个 ID **官方并不存在**，
#   用户在设置页选中后调用会直接报错，故移除。
#   当前实测可用：deepseek-chat / deepseek-reasoner / deepseek-v4-flash / deepseek-v4-pro
#   （新增 ID 前建议先走 validate_model 验一遍，别凭文档或猜测填）
# ★ 2026-09-11：V4.1 Flash 上线（官方公告 api-docs.deepseek.com/news/news260910），
#   调用 ID 是 deepseek-flash（不是 deepseek-v4.1-flash）；dev/Agent 工具默认走它。
MODEL_PRESETS = [
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-flash",
]

DEFAULTS = {
    "CURRENT_CHAT_MODEL": "deepseek-chat",        # 聊天 + 主动发言统一模型
    "MEMORY_EXTRACT_MODEL": "",                     # 记忆提炼模型：留空 = 跟随主大脑 selected_model()
    "SELECTED_MODEL": "deepseek-chat",             # 当前选择模型（设置页选择）
    "UNDERSTANDING_MODEL": "",                      # 语义理解层模型：留空 = 跟随主大脑 selected_model()
    # ★ 深度思考（角色卡 deep_thinking 开启时）用哪个模型。
    #   2026-09-11：原来在 deep_thinking_model() 里写死 deepseek-reasoner，无法更换。
    #   默认 deepseek-flash（DeepSeek V4.1 Flash）—— 实测同样有思考过程
    #   （reasoning_content，1570 字级），且比 reasoner 更快、比 glm-5.3-flash 快约 4 倍。
    #   置空 "" = 回到旧行为（provider=deepseek → deepseek-reasoner）。
    "DEEP_THINKING_MODEL": "deepseek-flash",
    # ★ 2026-09-15 新增：LLM 故障隔离参数（实现见 backend/llm_guard.py，
    #   背景：DeepSeek 宕机时后台杂活单次挂 900 秒、占满 20 条连接池，把她的回复也拖死）
    "LLM_AUX_TIMEOUT_SEC": 30,          # 后台机械杂活单次硬超时（秒）
    "LLM_LONG_TIMEOUT_SEC": 180,        # 长档硬超时（反思/日周月总结/回复链等实测慢调用）
    "LLM_BREAKER_FAILS": 3,             # 同一 provider 连续失败几次 → 熔断
    "LLM_BREAKER_COOLDOWN_SEC": 300,    # 熔断冷却时长（秒）：期间直接走兜底，绝不发网络请求
    "LLM_STREAM_FIRST_TIMEOUT_SEC": 60,  # 流式首包超时：上游一个字节都不给就掐断换兜底重放
    "LLM_STREAM_STALL_TIMEOUT_SEC": 90,  # 流式出字后卡死超时（出过字就不再重放，防重复文本）
    "LLM_FALLBACK_MODEL": "",           # 兜底模型：留空 = 自动（活跃角色大脑 > 跨 provider 备用池）
    # ★ 2026-09-15 新增：主动消息单一引擎（重做）
    "PROACTIVE_ENGINE_ENABLED": True,   # 总开关：false = 一键回退旧口径（三条链路各自判定）
    "PROACTIVE_QUIET_WINDOW_SEC": 300,  # 「不继续聊窗口器」：AI 最后一句后多久算这轮聊完（默认 5 分钟）
    "PROACTIVE_DAILY_CAP": 8,           # 每日"找话说"上限（0 = 不限）
    "PROACTIVE_CAP_EXCLUDE_EXEMPT": True,  # 上限是否排除用户自设提醒/到点承诺/危机
    "PROACTIVE_HELD_TTL_HOURS": 8,      # 被时段拦下的念头最多攒多久（小时）
    "PROACTIVE_SMALL_CTX": True,        # 闲聊类主动消息用小上下文生成（省 token）
    "PROACTIVE_FRONTEND_GENERATION": False,  # 前端是否仍自己生成主动消息（重做后默认否）
    "UNDERSTANDING_ENABLED": True,                  # 理解层总开关：关闭后跳过场景识别/语义分析，主模型直接回应
    "GAME_BRAIN_MODEL": "deepseek-chat",            # 游戏大脑模型（MC/Numen 决策）：默认快速 deepseek-chat；R1/深度推理模型思考过慢不适合实时游戏决策；主聊天继续用 SELECTED_MODEL（R1）保持深度
    "AUTO_MEMORY_INTERVAL": 2,                    # 自动记忆提炼轮次（2~20）：每 N 轮提炼一次，越小记得越勤
    # ★ 2026-09-14 新增：旧线记忆管线（YunLink save_from_chat）开关。
    #   该管线是遗留模块：与主线 maybe_auto_extract 写**同一张 long_term_memory 表**，
    #   但用旧 type 口径、不带 context/emotion_tag/source_text、且**没有冲突检测**，
    #   又在 /api/chat 文本路径上**每轮无条件**跑一次 LLM → 与主线重复劳动，
    #   也是长期记忆出现近似重复簇（实测"关系事件"×63、称呼变体 ×41）的来源之一。
    #   默认 False（关闭）。需要回退时把它设为 true 即可恢复原行为。
    "LEGACY_MEMORY_PIPELINE": False,              # 旧线记忆管线（与主线重复，默认关闭）
    # ★ 2026-09-14 新增：提示词压缩模式（PROMPT_COMPACT）。
    #   把每轮恒定注入的大块（主生成器硬规则 834 字、能力清单 816 字、
    #   真人感指令 287 字、聊天底色 205 字…）替换为**语义等价的精简版**：
    #   保留每一条规则与底线，只删重复表述、同义强调、可由其他规则推出的废话。
    #   目标是把恒定注入从 ~2,400 字压到 ~1,100 字（约省一半），
    #   且**不牺牲对话质量**（规则覆盖用 tools/_verify_compact_prompt.py 逐条核对）。
    #   默认 False = 行为与改动前完全一致；开启后仅影响 prompt 文本，不影响逻辑。
    "PROMPT_COMPACT": False,                      # 提示词压缩模式（省 token，规则不减）
    "IDLE_TRIGGER_MIN_MINUTES": 20,               # 主动发言下限
    "IDLE_TRIGGER_MAX_MINUTES": 40,               # 主动发言上限
    "IDLE_AGENT_ENABLED": True,                   # 闲置主动 Agent 总开关
    # ★ 自主程度：控制 AI 回复的自主空间（见 autonomy_level()）
    #   conservative = 保守（现状：理解层硬执行清单 + 规则引擎主动消息）
    #   balanced     = 平衡（默认：松绑语气/长度/话题走向，保留记忆一致性/人称/作息/防骚扰底线）
    #   autonomous   = 自主（模型自己决定怎么聊/要不要主动说，程序只留免打扰+安全底线）
    #   free         = 自由发挥（几乎不设约束，只留人称底线，让模型完全自主）
    #   full         = 完全自主（2026-09-14 新增：只注入人设+长期记忆+时间+能力清单，
    #                  规则/情绪/感知/导演类全部不注入；⚠️该档不注入危机干预，
    #                  详见 chat_logic._FULL_AUTO_SKIP_CRISIS）
    "AUTONOMY_LEVEL": "balanced",
    # 话题延续：AI 说完最后一句、用户沉默一段时间后，顺着刚才的话题补一句。
    # 与普通主动消息（间隔 20~40 分钟）不同，这是"怕冷场"的短间隔补话，且带真实上下文。
    "TOPIC_CONTINUE_ENABLED": True,               # 话题延续总开关
    "TOPIC_CONTINUE_DELAY_SEC": 120,              # 用户沉默多久才补话（秒）
    "TOPIC_CONTINUE_COOLDOWN_SEC": 900,           # 补完一句后的冷却（秒），避免连环轰炸
    "IDLE_AGENT_TIME_RANGE": "08:00-23:00",       # 主动发言全局活跃时段（HH:MM-HH:MM，支持跨天）
    "MORNING_ENABLED": True,                      # 早安推送默认开启
    "MORNING_START": "07:00",                     # 早安窗口开始
    "MORNING_END": "09:30",                       # 早安窗口结束
    "NIGHT_ENABLED": True,                        # 晚安推送默认开启
    "NIGHT_START": "22:00",                       # 晚安窗口开始
    "NIGHT_END": "23:30",                         # 晚安窗口结束
    "SURPRISE_ENABLED": True,                     # 随机小惊喜默认开启
    # AI 主导日：默认周日，调度器每周最多发一条有主题的问题。
    "AI_LEAD_DAY_ENABLED": True,
    "AI_LEAD_DAY_WEEKDAY": 6,

    # ★ 新增：免打扰配置（支持跨天，如 23:00-07:00）
    "DND_ENABLED": True,                          # 免打扰总开关（默认开）
    "DND_START":   "23:00",                       # 免打扰开始时间（支持跨天到次日）
    "DND_END":     "07:00",                       # 免打扰结束时间
    "DND_ALLOW_REMINDERS": True,                  # 用户明确设置的提醒可准时送达
    "DND_ALLOW_GREETINGS": True,                  # 早晚安可送达，但免打扰内静音
    "DND_ALLOW_CALLS": False,                     # AI 自发来电默认不突破免打扰

    "api_key": "",                                # 后端保存的 DeepSeek Key（可选）
    "zhipu_api_key": "",                          # 智谱 GLM Key（GLM-4.5 / GLM-5.1 / GLM-5.3-Flash）
    "claude_api_key": "",                         # Claude Key（Anthropic 官方或中转站）
    "google_api_key": "",                         # Gemini Key（Google AI Studio / 中转站）
    "openai_api_key": "",                         # OpenAI Key（GPT-5.4 等；空时回退 claude_api_key，因 openclawplan 一个中转一个密码）
    "xai_api_key": "",                            # xAI Grok Key（走 openclawplan 中转；空时回退 claude_api_key，同一个中转一个密码）

    # ====================
    # QQ 机器人（OneBot v11）
    # ====================
    # QQ 机器人绑定的角色（空 = default）
    "QQ_CHARACTER": "",
    # QQ 防抖窗口（秒）：连续几个气泡在窗口内到达会合并成一条再回复。
    # ★ 用户气泡间的思考间隙若大于此值，AI 会对每个气泡各回一条——
    #   觉得分得太多就调大（5~6），嫌回复慢就调小（2~3）。
    "QQ_DEBOUNCE_SEC": 4.0,
    # 私聊白名单：只允许这些 QQ 号跟机器人私聊；空列表 = 允许所有人。
    # 支持两种写法：["123456"] 或 "123456,789012"
    "QQ_ALLOWED_USERS": [],
    # QQ 机器人复用哪个 App 会话（空 = 自动复用「QQ_CHARACTER」角色的最新 desktop 会话）。
    # 填上后 QQ 与 App 就是同一段对话，互相可见、共享记忆。
    "QQ_SESSION_ID": "",

    # ====================
    # 天气 API（世界轻推 / 世界感知）
    # ====================
    # 天气服务商 API Key（和风天气 / OpenWeather 等），优先环境变量，其次 .env，再次 config.json
    "weather_api_key": "",

    # ====================
    # 视觉模型
    # ====================
    # 视觉服务商：dashscope / siliconflow / deepseek
    "VISION_PROVIDER": "dashscope",
    # 视觉 API Key
    "vision_api_key": "",
    # 视觉模型
    "VISION_MODEL": "qwen-vl-max",
    # 视觉接口地址
    "VISION_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",

    # ====================
    # ASR 语音转文本
    # ====================
    # ASR 云端 API Key（没有则走本地 Whisper 兜底）
    "asr_api_key": "",
    # ASR 服务商：whisper_local / azure / xunfei / aliyun / tencent
    "asr_provider": "whisper_local",

    # ====================
    # TTS 语音合成（云端 provider 的 Key；环境变量为空时兜底用）
    # ====================
    # 默认 TTS 服务商：edge-tts 免费免 Key；minimax / volcengine / xunfei / azure / elevenlabs 需填 Key
    "tts_provider": "edge-tts",
    # MiniMax（MINIMAX_API_KEY / MINIMAX_GROUP_ID）
    "minimax_api_key": "",
    "minimax_group_id": "",
    # 火山引擎（VOLCENGINE_APP_ID / VOLCENGINE_ACCESS_TOKEN / VOLCENGINE_CLUSTER）
    "volcengine_app_id": "",
    "volcengine_access_token": "",
    "volcengine_cluster": "volcano_tts",
    # 讯飞（XUNFEI_APP_ID / XUNFEI_API_KEY / XUNFEI_API_SECRET）
    "xunfei_app_id": "",
    "xunfei_api_key": "",
    "xunfei_api_secret": "",
    # Azure（AZURE_SPEECH_KEY / AZURE_SPEECH_REGION）
    "azure_speech_key": "",
    "azure_speech_region": "eastasia",
    # ElevenLabs（ELEVENLABS_API_KEY）
    "elevenlabs_api_key": "",

    # ====================
    # 阿里云百炼 DashScope（语音通话走付费；聊天语音消息走本地免费）
    # ====================
    # 百炼 API Key（DASHSCOPE_API_KEY）—— ASR 与 TTS 共用一个 Key
    "dashscope_api_key": "",
    # 语音通话专用的 provider（独立于聊天 TTS/ASR）
    "call_asr_provider": "aliyun",
    "call_tts_provider": "aliyun",
    # 云端 TTS 模型：qwen-audio-3.0-tts-flash（与系统音色 longanhuan_v3.6 配套）
    "aliyun_tts_model": "qwen-audio-3.0-tts-flash",
    # 云端 TTS 音频格式：wav / mp3 / pcm / opus
    "aliyun_tts_format": "wav",
    # 云端 ASR 模型：paraformer-realtime-v2
    "aliyun_asr_model": "paraformer-realtime-v2",
    # 声音复刻音色（复刻成功后回填，通话 TTS 直接用它）
    "voice_replica_voice_id": "",
    "voice_replica_target_model": "",
    # 复刻使用的参考音频路径
    "voice_replica_source": "",

    # ====================
    # 语音通话延迟（2026-09-10）
    # ====================
    # 通话快车道：语义分析（每回合最贵的 ~5s LLM 调用）改为"用上一轮结果 +
    # 后台刷新"。副作用（场景路由/模块 pipeline/情绪更新）照旧执行，只是注入
    # 本轮 prompt 的语义信息滞后一回合。关掉即完全回到旧的同步行为。
    "CALL_FAST_CONTEXT": True,
    # 通话 TTS 是否逐块推 PCM（首音频从"整句合成完"降到"首块到达"）
    "CALL_TTS_STREAM_PCM": True,

    # ====================
    # AI 监督吃醋系统（2026-09-11）
    #   方案参考桌面「AI监督吃醋系统.zip」，按本项目架构重写（见 backend/jealousy.py）。
    #   只读前台窗口 / 键鼠空闲（ctypes 只读，不截图、不 OCR），纯本地，
    #   设置页可一键开关；命中"其他AI伴侣/短视频/社交"按时长涨吃醋值，
    #   超阈值就用角色自己的口吻主动发一条质问消息（走统一主动消息管道）。
    # ====================
    "JEALOUSY_ENABLED": False,          # 总开关（关掉即不采集/不注入/不发消息）
    # ★ 2026-09-15 用户拍板：60 → 45（实测 60 永远够不着；抖音 3 分/分钟时 15 分钟到线）
    "JEALOUSY_TRIGGER": 45,             # 吃醋值超过多少触发（0-100）
    # ★ 2026-09-15 新增（用户："熬夜时间在晚上一点以后"）：
    #   凌晨 1 点后还在写代码/写文档 → 工作软件按 2 分/分钟计入；白天仍完全不计。
    "JEALOUSY_LATE_NIGHT_HOUR": 1,      # 熬夜从几点算起（1 = 凌晨 1 点，到早上 6 点）
    "JEALOUSY_LATE_NIGHT_RATE": 2.0,    # 熬夜时段工作软件的每分钟增量
    # ★ 2026-09-15 新增：是否允许她**主动发消息**质问（由模型自己组织语言，不是模板）
    "JEALOUSY_PROACTIVE": True,
    # ★ 2026-09-15（第 2 步）：每天最多主动质问几条（用户拍板 3 条）
    "JEALOUSY_DAILY_MAX": 3,
    # ★ 2026-09-15 用户要求：熬夜时段（凌晨 1 点后）允许吃醋消息**破例穿透免打扰**，
    #   独立开关。默认 False = 老老实实憋到早上；True = 一点多了还在刷，她会来敲门。
    "JEALOUSY_DND_BREAK_LATE_NIGHT": False,
    "JEALOUSY_CHECK_INTERVAL": 300,     # 多久评估一次（秒）
    "JEALOUSY_MESSAGE_COOLDOWN": 1800,  # 两条吃醋消息最小间隔（秒）
    "JEALOUSY_USAGE_WINDOW_MINUTES": 60,  # 统计最近多少分钟
    "JEALOUSY_SAMPLE_SECONDS": 5,       # 采样间隔（秒）
    "JEALOUSY_DECAY_PER_HOUR": 5,       # 吃醋值每小时自然衰减
    "JEALOUSY_IDLE_SKIP_SECONDS": 300,  # 键鼠空闲超过多久就不算"在用电脑"
    "JEALOUSY_RULES": {},               # 应用→每分钟增量（留空用内置默认表）
    "JEALOUSY_WHITELIST": [],           # 工作软件白名单（留空用内置默认表）

    # ====================
    # 模型池配置
    # ====================
    # 文本模型池：key 为模型标识，value 包含 name/provider/model/baseUrl
    "TEXT_MODELS": {
        "deepseek-chat": {
            # 官方别名，始终指向 DeepSeek 当前主力对话模型
            "name": "DeepSeek 对话（官方默认）",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "baseUrl": "https://api.deepseek.com"
        },
        "deepseek-reasoner": {
            "name": "DeepSeek R1（推理）",
            "provider": "deepseek",
            "model": "deepseek-reasoner",
            "baseUrl": "https://api.deepseek.com"
        },
        "deepseek-v4-flash": {
            # 便宜且快，适合"分类/判断"这类轻量任务（如语义理解层）
            "name": "DeepSeek V4 Flash（快·省）",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "baseUrl": "https://api.deepseek.com"
        },
        "deepseek-v4-pro": {
            "name": "DeepSeek V4 Pro（强）",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "baseUrl": "https://api.deepseek.com"
        },
        "deepseek-flash": {
            # V4.1 Flash（2026-09-10 上线）：官方稳定别名，代码/工具任务性价比高
            "name": "DeepSeek V4.1 Flash（新·快·省）",
            "provider": "deepseek",
            "model": "deepseek-flash",
            "baseUrl": "https://api.deepseek.com"
        },
        "qwen-plus": {
            "name": "通义千问 Plus",
            "provider": "dashscope",
            "model": "qwen-plus"
        },
        "qwen-max": {
            "name": "通义千问 Max",
            "provider": "dashscope",
            "model": "qwen-max"
        },
        "kimi": {
            "name": "Kimi K2",
            "provider": "moonshot",
            "model": "kimi-k2"
        },
        "glm-4.7": {
            "name": "GLM-4.7（免费额度）",
            "provider": "zhipu",
            "model": "glm-4.7",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        },
        "glm-4.5-air": {
            "name": "GLM-4.5-Air（免费额度）",
            "provider": "zhipu",
            "model": "glm-4.5-air",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        },
        "glm-4-flash": {
            "name": "GLM-4-Flash（免费无限）",
            "provider": "zhipu",
            "model": "glm-4-flash",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        },
        "glm-5.1": {
            "name": "GLM-5.1（代码/长程强）",
            "provider": "zhipu",
            "model": "glm-5.1",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        },
        "glm-5.3": {
            "name": "GLM-5.3（最新旗舰）",
            "provider": "zhipu",
            "model": "glm-5.3",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        },
        "glm-5.3-flash": {
            "name": "GLM-5.3-Flash（多模态·快·省）",
            "provider": "zhipu",
            "model": "glm-5.3-flash",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        },
        # Claude（Anthropic 官方或 OpenAI 兼容中转站）。baseUrl 留空 = 由前端「接口地址」传入。
        "claude-sonnet-4-20250514": {
            "name": "Claude Sonnet 4（中转）",
            "provider": "anthropic",
            "model": "claude-sonnet-4-20250514",
            "baseUrl": ""
        },
        "claude-3-7-sonnet-20250219": {
            "name": "Claude 3.7 Sonnet（中转）",
            "provider": "anthropic",
            "model": "claude-3-7-sonnet-20250219",
            "baseUrl": ""
        },
        "claude-3-5-sonnet-20241022": {
            "name": "Claude 3.5 Sonnet（中转）",
            "provider": "anthropic",
            "model": "claude-3-5-sonnet-20241022",
            "baseUrl": ""
        },
        "claude-3-5-haiku-20241022": {
            "name": "Claude 3.5 Haiku（中转·快）",
            "provider": "anthropic",
            "model": "claude-3-5-haiku-20241022",
            "baseUrl": ""
        },
        "claude-sonnet-5": {
            "name": "Claude Sonnet 5（openclawplan 中转）",
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "baseUrl": "https://api-node1.openclawplan.com/v1"
        },
        # Gemini（Google AI Studio / OpenAI 兼容中转站）。与 Claude 同中转站、同 Key。
        "gemini-3.1-pro-high": {
            "name": "Gemini 3.1 Pro High（openclawplan 中转）",
            "provider": "google",
            "model": "gemini-3.1-pro-high",
            "baseUrl": "https://api-node1.openclawplan.com/v1"
        },
        # ★ OpenAI（gpt-5.4 / 后续 GPT 系列）：走与 Claude/Gemini 同一 openclawplan 中转 + 同一 Key，
        #   即「一个中转一个密码」。如要单独配 Key 填 openai_api_key 即可，没配则回退 claude_api_key。
        "gpt-5.4": {
            "name": "GPT-5.4（openclawplan 中转）",
            "provider": "openai",
            "model": "gpt-5.4",
            "baseUrl": "https://api-node1.openclawplan.com/v1"
        },
        # ★ Claude Opus 4.6：走与 Sonnet 5 同一 openclawplan 中转 + 同一 Key（一个中转一个密码）
        "claude-opus-4-6": {
            "name": "Claude Opus 4.6（openclawplan 中转）",
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "baseUrl": "https://api-node1.openclawplan.com/v1"
        },
        # ★ Gemini 3.7 Flash：走与 Gemini 3.1 Pro High 同一 openclawplan 中转 + 同一 Key
        "gemini-3.7-flash": {
            "name": "Gemini 3.7 Flash（openclawplan 中转）",
            "provider": "google",
            "model": "gemini-3.7-flash",
            "baseUrl": "https://api-node1.openclawplan.com/v1"
        },
        # ★ xAI Grok（thinking）：走与 Claude/GPT 同一 openclawplan 中转 + 同一 Key（一个中转一个密码）
        "grok-420-thinking": {
            "name": "Grok 4.20 Thinking（openclawplan 中转）",
            "provider": "xai",
            "model": "grok-420-thinking",
            "baseUrl": "https://api-node1.openclawplan.com/v1"
        },
        # ★ 本地大脑（Ollama，2026-09-11）：人格设置页「本地大脑」开关的控制令牌。
        #   deepseek_api 识别 provider=local 后改走 LOCAL_BRAIN.baseUrl 的 OpenAI 兼容接口，
        #   实际模型名运行时读 LOCAL_BRAIN.model（训练回灌/换模型只动 LOCAL_BRAIN，不改池子）。
        "local-brain": {
            "name": "本地大脑（Ollama·免费离线）",
            "provider": "local",
            "model": "qwen3:4b",
            "baseUrl": "http://127.0.0.1:11434/v1"
        }
    },
    # 视觉模型池：key 为模型标识，value 包含 name/provider/model/baseUrl
    "VISION_MODELS": {
        "qwen-vl-max": {
            "name": "通义千问视觉Max",
            "provider": "dashscope",
            "model": "qwen-vl-max"
        },
        "qwen-vl-plus": {
            "name": "通义千问视觉Plus",
            "provider": "dashscope",
            "model": "qwen-vl-plus"
        },
        "Qwen2.5-VL-72B": {
            "name": "Qwen2.5-VL-72B",
            "provider": "siliconflow",
            "model": "Qwen/Qwen2.5-VL-72B-Instruct"
        },
        "glm-5.3-flash": {
            "name": "GLM-5.3 Flash（视觉）",
            "provider": "zhipu",
            "model": "glm-5.3-flash",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4"
        }
    },

    # ====================
    # 本地大脑（Ollama，2026-09-11）：一键切换 / 换模型 / 训练回灌，全部只动这块配置
    # ====================
    "LOCAL_BRAIN": {
        "enabled": True,                       # 总开关（本地服务不可达时聊天自动回退云端 fallback_model）
        "baseUrl": "http://127.0.0.1:11434",   # Ollama 服务地址（OpenAI 兼容接口在 /v1）
        "model": "qwen3:4b",                   # 当前本地模型（ollama 模型名；训练完的新模型注册到这里）
        "fallback_model": "deepseek-chat",     # 本地挂了/没装时自动切回的云端模型
        "think": True,                         # Qwen3 思考模式（2026-09-11 用户拍板：默认开，提升对话质量）
        "num_ctx": 32768,                      # 上下文长度（2026-09-12 16K→32K：Qwen3-4B 原生支持，8G 显存不开游戏放得下）
        "keep_alive": "30m",                   # 模型在显存保留时长（空闲后卸载，给训练/游戏让显存）
        # ★ 本地路由上下文预算（2026-09-12）：超过预算时只裁最老的原始聊天记录，
        #   人设/记忆/RAG 注入块全量保留；云端链路完全不经过这段逻辑。
        "ctx_budget_tokens": 16384,
        # ★ 本地输出 token 上限（2026-09-12 方案A）：Ollama 的窗口是「提示词+输出」
        #   共享的，而 num_ctx 我们不发送（由模型 Modelfile 决定）。为了把 8b 塞进
        #   8G 显存会把窗口降到 16k/20k，此时提示词 ~13k + 输出 8192 就超窗 →
        #   Ollama 400 → 静默退回云端。这里给本地单独限输出，云端不受影响。
        "max_output_tokens": 4096,
        # ★ 方案②（2026-09-12）：本地路由是否用**精简 system**。
        #   本地小模型吞不下 ~12k token 的"人设+40多个规则块+元信息"，
        #   表现为被追问就转移话题/复读。开着只保留白名单里的块（人设/关系/记忆/
        #   语言风格/硬规则/本轮用户这句话），system 大约从 8.8k token 降到 ~1.5k。
        #   **置 false 即一键回到完整 prompt；云端链路本来就不经过这段逻辑。**
        "lite_prompt": True,
    },

    # ====================
    # CosyVoice 惰性管理（2026-09-12）：不再进 APP 就拉起 TTS，太占内存
    # ====================
    # TTS 服务空闲多少分钟后自动关闭释放内存（0 = 常驻不关；要用时按需拉起）
    "COSYVOICE_IDLE_SHUTDOWN_MIN": 10,
    # 恢复旧行为：启动/watchdog 自动拉起 TTS 常驻（默认关 = 按需拉起）
    "COSYVOICE_AUTOSTART": False,

    # ====================
    # 朋友圈（AI 主动发动态）
    # ====================
    # AI 朋友圈每天最多几条（0=关闭不发，1~3）；不是必发，靠「价值判断」决定是否真有值得发的
    "MOMENT_FREQUENCY": 2,
    # 主动消息频率：low=偶尔(克制) / normal=正常 / high=频繁(主动)，控制整体主动密度
    "PROACTIVE_FREQUENCY": "normal",

    # ====================
    # 离线状态系统（角色独立时间线：到点才回 / 回来后交代）
    # ====================
    # 总开关：默认关（开=角色有自己的作息，睡觉/忙时延迟回复）
    "OFFLINE_ENABLED": False,
    # 最长回复延迟（分钟，1~720）：超过该上限的延迟会被截断到该值
    "OFFLINE_MAX_DELAY_MINUTES": 30,

    # ====================
    # 感知系统（总开关 + 各采集维度）
    # ====================
    # 感知总开关：一键关闭全部采集（截图分析 / 前台窗口 / 键鼠空闲），默认关
    "AWARENESS_ENABLED": False,
    # 截图分析（受总开关门控；本地截图 + 视觉模型分析），默认关
    "SCREEN_PERCEPTION_ENABLED": False,
    # ★ 生活/游戏陪伴模式的主动间隔（分钟）：陪伴模式下 TA 闲置多久主动找你聊的范围。
    #   settings.js 屏幕感知区可调；只在 companion_mode 有值（陪伴模式开着）时生效。
    "COMPANION_IDLE_MIN_MINUTES": 4,
    "COMPANION_IDLE_MAX_MINUTES": 8,
    "COMPANION_SCREEN_COMMENT_COOLDOWN": 300,   # 陪伴模式两次屏幕评论的最小间隔（秒）
    "COMPANION_IDLE_MAX_MINUTES": 8,
    "PC_PTT_ENABLED": True,     # ★ 按住说话控制电脑（全局 F9 钩子）：按住 F9 说话，松开执行
    "PC_PTT_KEY": "f9",         # 按住说话的键位（可改 f2/f3 等）
    # 截图观察间隔（秒）
    "SCREEN_INTERVAL": 300,
    # 屏幕内容写记忆（受总开关门控），默认开
    "SCREEN_MEMORY_ENABLED": True,

    # ====================
    # iOS 远程桥接（邮件触发快捷指令 + 截屏回传）
    # ====================
    # 总开关：默认关（开=允许 AI 通过邮件触发 iPhone 快捷指令操控手机）
    "IOS_REMOTE_ENABLED": False,
    # SMTP 发信配置（建议 QQ 邮箱：smtp.qq.com，端口 465，密码填「授权码」不是邮箱密码）
    "IOS_SMTP_HOST": "smtp.qq.com",
    "IOS_SMTP_PORT": 465,
    "IOS_SMTP_USER": "",
    "IOS_SMTP_PASSWORD": "",
    # 发件地址（留空则用 IOS_SMTP_USER）；收件地址（iPhone 上登录的邮箱，通常与发件相同）
    "IOS_MAIL_FROM": "",
    "IOS_MAIL_TO": "",
    # Bark 推送 Key（Bark App 首页的 api.day.app/xxxx/ 里的 xxxx）。
    # 配置后，open_app 指令优先走 Bark 秒级推送（点击通知打开 App），不再发邮件。
    "IOS_BARK_KEY": "",

    # ====================
    # ★ 2026-09-15 用户拍板（账单 + 突兀问候）
    # ====================
    # 后台"要不要主动说话"这类判定/文案调用使用的模型。
    #   默认 glm-5.3-flash（同 ⑤ 的杂活档）：它们只判"该不该开口 / 写一句关心"，
    #   不需要旗舰智力。实测这三处（predictive_companion / boredom / first_greeting）
    #   跑在旗舰 glm-5.3 上，45 分钟烧 9.8 万 token。
    #   想更省可设成 "glm-4-flash"（智谱免费档），质量会降一点。
    "SCHEDULER_AUX_MODEL": "glm-5.3-flash",
    # 定时问候总开关：★ 2026-09-15 用户拍板关掉（"这问候太冲突了"）。
    #   这两项的**唯一定义处**在下面 MORNING_START / NIGHT_START 那一段；
    #   原先这里又重复定义了一份（dict 同名键后者覆盖前者，行为虽对但极易改错）→ 已去重。

    # ====================
    # 关系升温速度（快热/慢热调节）
    # ====================
    # slow=慢热（每天亲密度上限低，慢慢升温有成就感）/ normal=正常 / fast=快热
    "RELATIONSHIP_PACE": "normal",

    # ====================
    # ★ 2026-09-15 用户拍板：砍掉亲密度/关系自动成长系统（省 token）
    # ====================
    # false（默认）= 关闭每一轮的自动成长更新。被关掉的是这 6 个跑 LLM 的
    #   updater：personality_manager.update_personality、relationship_manager
    #   .update_relationship、ai_state_manager.update_ai_state、emotion_manager
    #   .update_emotion、behavior_manager.analyze_behavior，以及本地零成本的
    #   personality_manager.update_five_dim（同一族的"缓慢偏移"成长）。
    #   关掉后：亲密度/信任/阶段只认**手动设置**的值，不再自己涨也不再自己掉，
    #   更不会因为重打包/换会话被重置；AI 提示词读取路径保持不变（照样"知道"关系）。
    #   true = 恢复旧行为（出问题时可一键回滚，不用改代码）。
    "RELATIONSHIP_AUTO_UPDATE": False,
    # false（默认）= 界面隐藏亲密度/关系数值面板（总览与人格页的关系卡片）。
    #   后端接口与数据保留，只是不展示，避免看到一个不再变化、还容易被重置的数字。
    "RELATIONSHIP_UI_VISIBLE": False,

    # ====================
    # ★ 2026-09-15 用户拍板：长期反思改「每天一次固定槽」
    # ====================
    # daily（默认）= 每天只在 REFLECTION_SLOT_HOUR 点之后的第一次机会跑一次；
    #   interval = 旧口径（距上次满 N 小时就跑），改回旧行为只需把这里改成 "interval"。
    #   为什么改：实测 2 天反思 133,724 in / 81,870 out token，因间隔只有 30 分钟、
    #   素材几乎没变，生成内容与前一天高度重合，去重后大量丢弃 = 白烧。
    "REFLECTION_MODE": "daily",
    "REFLECTION_SLOT_HOUR": 5,      # 固定槽：凌晨 5 点之后的第一次机会
    "REFLECTION_INTERVAL_HOURS": 24,  # 仅 REFLECTION_MODE=interval 时生效

    # ====================
    # ★ 2026-09-15 用户拍板（省 token ①）：CompanionOS 语义分析不再每条消息打模型
    # ====================
    # local（默认）= 本地规则判意图/情绪/话题/关系信号，零 token；
    # llm = 旧口径（每条消息一次 chat_once，实测 2 天 271,931 in / 143,863 out）；
    # off = 直接返回默认语义态（CompanionOS 全部按普通闲聊处理）。
    # 判错只影响"场景路由"这类轻决策，回复本身（主脑 + 人设 + 记忆）不受影响。
    "SEMANTIC_ANALYZER_MODE": "local",

    # ====================
    # ★ 2026-09-15 用户拍板（省 token ②）：机械抽取器合并成一次调用
    # ====================
    # true（默认）= 记忆提炼 + 未完成事项 + 用户画像 三段合并成**一次** chat_once
    #   （它们读同一批 messages，原来各付一遍钱：2 天实测 171,866 + 145,373 in token）；
    #   false = 回到老的"三次分别调用"。合并失败时**自动**回退旧口径，不受本开关影响。
    "MERGED_EXTRACT_ENABLED": True,

    # ====================
    # ★ 2026-09-17 记忆遗忘阈值（用户拍板：永不真删，只标记）
    # ====================
    # 衰减分低于 MEMORY_ARCHIVE_DECAY → memory_status='archived'（降权，仍可被搜到）
    # 衰减分低于 MEMORY_FORGET_DECAY  → memory_status='forgotten'（基本退场）
    # 两者都只标记 is_valid=0，**不 DELETE**；importance>=7 或被想起≥10 次的记忆豁免。
    # 为什么要给可配阈值：旧公式下 decay_score 最小值恒为 0.5123，
    # 任何阈值都不可达 = 遗忘功能实际不存在；现在公式改对了，阈值才需要可调。
    "MEMORY_ARCHIVE_DECAY": 0.25,
    "MEMORY_FORGET_DECAY": 0.10,

    # ====================
    # ★ 2026-09-17 两段式"按需翻库"（用户拍板：常驻关键记忆 + 她需要时自己调检索）
    # ====================
    # true（默认）= 每轮先用**本地规则**（零成本）判断这句像不像在问过去的事；
    #   命中嫌疑才用便宜模型决定"翻不翻 / 翻什么词"，然后做一次**定向**小预算检索，
    #   作为额外的一块注入（常驻块保持原样，不替换）。
    # false = 完全回到旧口径（只有每轮无条件注入的那一块）。
    # 为什么不做成真 function call：主脑当前没有 tools 能力（deepseek_api 零匹配），
    # 真加工具要改主回复链路（流式/预算），风险远大于收益 —— 详见 recall_decider.py。
    "RECALL_DECIDER_ENABLED": True,
    # 定向检索的字符预算（小一点，避免挤掉常驻块）
    "RECALL_DECIDER_BUDGET": 1200,

    # ====================
    # ★ 2026-09-15 用户拍板（省 token ③）：主链提示词按场景裁剪
    # ====================
    # true（默认）= 轻量轮次（闲聊/打招呼/调侃/称赞/提问/自我披露，且情绪中性、无危机、
    #   无活跃话题）不注入「反馈/感知/格式/导演」这 12 个指令类积木；
    #   人设/记忆/关系/时间/能力/用户规则/防复读/心情/危机干预**一律照旧**。
    # false = 完全回到旧口径（每个积木都注入）。
    "SCENE_PROMPT_TRIM": True,

    # ====================
    # TTS 音频缓存清理（tts_cache/ 原先只写不删，长期运行会无限增长）
    # ====================
    # 音频保留天数：超过这个天数且期间没被访问过就删除
    "TTS_CACHE_TTL_DAYS": 7,
    # 目录总大小上限（MB）：超过后即使没到期，也按最旧优先继续删
    "TTS_CACHE_MAX_MB": 512,

    # ====================
    # 用户纠正的有效期（feedback/corrections.py）
    # ====================
    # 纠正记录保留天数：超过这个天数、期间又没被再次确认过的纠正，
    # 不再注入 prompt（避免"三年前随口纠正的一句"被永久当成铁律）。
    # **记录本身仍然保留**，只是不再生效——不丢数据，随时可改。
    #   0 = 永久有效
    #   默认 180 天：够覆盖绝大多数长期事实（姓名/职业/偏好），
    #   又不至于让陈年纠正一直压着。
    # 观察 /api/understanding/stats 或 correction_stats().topics 里的 last 时间，
    # 可据此调整这个数字。
    "CORRECTION_TTL_DAYS": 180,

    # ====================
    # 多段回复（分段气泡）生成策略
    # ====================
    # True =「一次生成完整回复 + 拆句」：LLM 调用次数从 N 段降到 1 次，提速明显，
    #   且一次生成的回复天然连贯、顺序不乱（修复"逐段独立生成导致话题穿插、回复慢"）。
    # False = 回退旧的「逐段独立生成」（每条气泡各调一次 LLM）。
    "MULTI_TURN_ONCE": True,

    # ====================
    # 语义理解层（第一层 DeepSeek）专用模型
    # ====================
    # 理解层的任务是"读一句话 → 输出结构化 JSON"，属于轻量分类任务。
    # ★ 实测（2026-09-01）：deepseek-v4-flash 间歇性返回空/截断 JSON，
    #   导致 parse_failed 高达 23% → 理解层静默失效（"第一层没跑"的元凶），
    #   还连带触发回退重试双倍计费。改用稳定的 deepseek-chat。
    # 留空则回退到 selected_model()（跟聊天同一模型）。
    # 注意：理解层的 key 走 config.api_key()，所以只有 DeepSeek 官方模型可用。
    "UNDERSTANDING_MODEL": "",
    # ★ Agent 助手模式专用模型（做任务/调工具用的）。留空则回退 selected_model()（跟聊天同一模型）。
    #   想给 Agent 用推理模型（deepseek-reasoner 等）让每个决策都深度思考，就在这里填模型 ID。
    "AGENT_MODEL": "",

    # ====================
    # ★ 星露谷 AI 陪伴（StardewValley-MCP 接入）
    # ====================
    # 接入步骤：1) 装 SMAPI 4.0+  2) 下载开源 StardewValley-MCP 仓库：
    #   smapi-mod/ 下 `dotnet build`（自动部署到游戏 Mods/），
    #   mcp-server/ 下 `npm install && npm run build`；
    # 3) 把 mcp-server/build/index.js 的绝对路径填到 STARDEW_MCP_SERVER，
    #    STARDEW_ENABLED 改 true，重启后端。
    # 游戏开着加载存档后，助手会自动生成同伴进农场，QQ 可遥控（浇水/收菜/钓鱼/挖矿/跟随）。
    "STARDEW_ENABLED": False,
    # ★ 真联机模式（推荐）：true = 走 StardewClient（控制第二个游戏实例里的真 farmhand，
    #   可被交互/送结婚戒指）；false = 走 StardewMCPBridge 的 NPC 影子同伴（不可交互）。
    #   两种模式的桥路径不同（见下），切换后重启。
    "STARDEW_CLIENT_MODE": True,
    # Node 可执行文件（MCP Server 是 Node 程序，需 Node 18+）
    "STARDEW_MCP_NODE": "node",
    # StardewValley-MCP 的 mcp-server/build/index.js 绝对路径（必填才启用）
    "STARDEW_MCP_SERVER": "",
    # SMAPI 启动器（StardewModdingAPI.exe）绝对路径；留空 = 后端自动探测常见 Steam 库位置。
    # UI「启动星露谷」按钮和自动拉起都走它。
    "STARDEW_GAME_PATH": "",
    # 可选：桥接路径。★ Mod 在游戏 Mods 目录下读写，与 server 默认路径（仓库内）不一致，
    #   实际部署时这两个基本都要显式填（见 星露谷AI陪伴-接入指南.md）：
    #   BRIDGE = 游戏 Mods/StardewMCPBridge/bridge_data.json（Mod 写状态 → Server 读）
    #   ACTION_DIR = 游戏 Mods/StardewMCPBridge/actions（Server 写命令 → Mod 逐条读，源码变量名是这个）
    "STARDEW_BRIDGE_PATH": "",
    "STARDEW_ACTION_DIR": "",
    # ★ 游戏脑（真联机模式）：让 LLM 看着农场环境自主操作（观察→决策→发低级动作）。
    #   关掉后助手只跑 mod 内置脚本状态机（会砍不到树、不会种地卖货那套微操）。
    "STARDEW_GAME_BRAIN": True,
    # 两次游戏脑决策的最小间隔秒数（动作队列没跑完不会提前决策）
    "STARDEW_GAME_BRAIN_INTERVAL": 15,
    # ★ 外置记忆库目录（日/周/月递归总结 + 聊天原文归档）。
    #   留空 = 用 external_memory_dir() 的默认值（项目根目录下的「外置记忆库」）。
    #   想放到别的盘/目录，在这里填绝对路径即可。
    "EXTERNAL_MEMORY_DIR": "",
}

_lock = threading.Lock()
_cache = None


def _load_env_file():
    """极简 .env 解析（KEY=VALUE，忽略注释）"""
    env = {}
    try:
        for line in ENV_FILE.read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _read_json(fp: Path, default):
    try:
        return json.loads(fp.read_text("utf-8"))
    except Exception:
        return default


def _load() -> dict:
    global _cache
    if _cache is None:
        data = _read_json(CONFIG_FILE, {})
        cfg = dict(DEFAULTS)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
        _cache = cfg
    return _cache


def _persist():
    # ★ 滚动备份：_load 只保留 DEFAULTS 内的键 + 全量重写 config.json——
    #   旧版 exe（DEFAULTS 缺新字段）运行时曾把 dashscope_api_key /
    #   voice_replica_voice_id / QQ_ALLOWED_USERS 等值洗掉，且无痕可查。
    #   写盘前留 3 份滚动备份，再发生「配置被洗」可直接从 bak 恢复对照。
    try:
        _parent = CONFIG_FILE.parent
        _b1 = _parent / (CONFIG_FILE.name + ".bak_1")
        _b2 = _parent / (CONFIG_FILE.name + ".bak_2")
        _b3 = _parent / (CONFIG_FILE.name + ".bak_3")
        if CONFIG_FILE.exists():
            if _b2.exists():
                if _b3.exists():
                    _b3.unlink()
                _b2.replace(_b3)
            if _b1.exists():
                _b1.replace(_b2)
            CONFIG_FILE.replace(_b1)
    except Exception:
        pass
    CONFIG_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), "utf-8")


def get_all() -> dict:
    with _lock:
        return dict(_load())


def get(key: str, default=None):
    """读取配置项；scheduler 等调用方常传默认值（如 config.get("X", True)）。

    ★ MEMORY_EXTRACT_MODEL 留空 → background_model()（跟随主脑，主脑是深度推理/旗舰时降级省钱）。
      注意：这条特例与 memory_extract_model() 的第 3 档口径保持一致，两条路都走 background_model()，
      避免"同一个键从两处读、拿到两个不同模型"。
      UNDERSTANDING_MODEL 留空则原样返回空字符串，回退链由 understanding.understand() 自己决定：
      优先用显式配置的理解层模型，否则跟随当前对话模型（实现「理解层 / 生成层可混搭」）。
    """
    with _lock:
        v = _load().get(key, default)
        if key == "MEMORY_EXTRACT_MODEL" and not str(v or "").strip():
            return background_model()
        return v


# ============================================================
# ★ 2026-09-14：主动消息「可用时段」统一解析（单一数据源）
# ------------------------------------------------------------
# 背景（实测 bug「主动发消息限制时间范围不管用」的三层原因）：
#   1) 两条链路各读各的：scheduler._tick 只读全局 IDLE_AGENT_TIME_RANGE；
#      idle_agent._in_active_window 读全局 + 角色 quietStart/quietEnd。
#   2) 角色卡层面的 quietStart/quietEnd **从未进入 character_manager.save_character
#      的字段白名单** → 前端在人格设置里设了也存不进去 → 后端永远读到空。
#   3) _deliver 的 DND 豁免把「早晚安」也算豁免，于是 23:00 后仍能发。
# 现统一为：**以角色卡为唯一权威**，全局 IDLE_AGENT_TIME_RANGE 仅作未配置时的兜底默认。
# ============================================================

DEFAULT_ACTIVE_HOURS = "08:00-23:00"   # 角色未配置时的默认可用时段


def proactive_interval_pair(character_id: str = "") -> tuple:
    """主动消息的**统一间隔区间**（分钟），返回 (lo, hi)。0 表示不主动。

    ★ 2026-09-14 统一（用户拍板）：**以全局设置为唯一来源**
      —— 全局设置页的「主动发言间隔」存 IDLE_TRIGGER_MIN_MINUTES / _MAX_MINUTES
      （如 60–120 分钟）。人格设置里原先那个"主动发消息（最长间隔）"控件已删除，
      因为它只存一个上限值、不落盘、且与全局各说各话（实测三条链路口径全不同）。

    回退链（保证老配置仍可用）：
      1. 全局 min/max 都在且有 min<=max → 用它
      2. 旧角色卡 proactiveMaxMin（上限语义，下限取一半）
      3. 默认 20/40
    """
    try:
        lo = float(get("IDLE_TRIGGER_MIN_MINUTES") or 0)
        hi = float(get("IDLE_TRIGGER_MAX_MINUTES") or 0)
        if lo > 0 and hi >= lo:
            return (lo, hi)
    except Exception:
        pass
    try:
        if character_id:
            from . import character_manager as _cm
            _c = _cm.get_character_any(character_id) or {}
            _v = _c.get("proactiveMaxMin")
            if _v is not None:
                _m = max(0.0, float(_v))
                if _m > 0:
                    return (max(10.0, _m * 0.5), _m)
    except Exception:
        pass
    return (20.0, 40.0)


def proactive_interval_min(character_id: str = "") -> float:
    """主动消息最小间隔（分钟）。0 = 不主动。"""
    lo, hi = proactive_interval_pair(character_id)
    if hi <= 0:
        return 0.0
    return lo if lo > 0 else max(10.0, hi * 0.5)


def proactive_enabled(character_id: str = "") -> bool:
    """是否允许主动消息（间隔上限为 0 视为关闭）。"""
    _, hi = proactive_interval_pair(character_id)
    return hi > 0


def resolve_active_hours(character_id: str = "") -> str:
    """返回该角色的主动消息可用时段，格式 "HH:MM-HH:MM"（支持跨天，如 "22:00-06:00"）。

    优先级：
      1. 角色卡 active_hours        （新字段，人格设置页写入）
      2. 角色卡 quietStart-quietEnd （旧的免打扰字段，取补集当可用时段）
      3. 全局 IDLE_AGENT_TIME_RANGE  （legacy，仅为兼容保留）
      4. DEFAULT_ACTIVE_HOURS
    任何异常都回退到下一级，绝不抛错。
    """
    # 1) 角色卡 active_hours（新字段，最优先）
    try:
        if character_id:
            from . import character_manager as _cm
            _c = _cm.get_character_any(character_id) or {}
            _ah = str(_c.get("active_hours") or "").strip()
            if _ah and parse_time_range(_ah):
                return _ah
            # 2) 旧字段 quietStart/quietEnd：把它当"不可用时段"，取补集
            _qs = str(_c.get("quietStart") or "").strip()
            _qe = str(_c.get("quietEnd") or "").strip()
            if _qs and _qe and parse_time_range("%s-%s" % (_qs, _qe)):
                _inv = invert_time_range("%s-%s" % (_qs, _qe))
                if _inv:
                    return _inv
    except Exception:
        pass
    # 3) 全局兜底（legacy）
    try:
        _g = str(get("IDLE_AGENT_TIME_RANGE") or "").strip()
        if _g and parse_time_range(_g):
            return _g
    except Exception:
        pass
    return DEFAULT_ACTIVE_HOURS


def invert_time_range(rng: str) -> str:
    """把一个时段取补集（"22:00-08:00" → "08:00-22:00"）。解析失败返回 ""。"""
    p = parse_time_range(rng)
    if not p:
        return ""
    sh, sm, eh, em = p
    return "%02d:%02d-%02d:%02d" % (eh, em, sh, sm)


def character_active_now(character_id: str = "", now=None) -> bool:
    """该角色此刻是否处于可用时段内。供 scheduler / idle_agent 共用。

    注意 is_time_in_range 的签名是 (start, end, now)，这里要把 "HH:MM-HH:MM" 拆开传。
    """
    try:
        from datetime import datetime as _dt
        _now = now or _dt.now()
        _r = resolve_active_hours(character_id)
        _a, _, _b = str(_r).partition("-")
        if not _a or not _b:
            return True
        return is_time_in_range(_a.strip(), _b.strip(), _now)
    except Exception:
        return True


def prompt_compact() -> bool:
    """提示词压缩模式是否开启（PROMPT_COMPACT）。

    开启后，每轮恒定注入的大块改用**语义等价的精简版**（规则一条不减，
    只删同义强调与重复表述）。只影响 prompt 文本，不影响任何功能逻辑。
    关闭（默认）= 与改动前逐字一致。
    """
    try:
        return bool(get("PROMPT_COMPACT", False))
    except Exception:
        return False


def understanding_enabled() -> bool:
    """理解层是否开启（场景识别 + 语义分析）。关闭后主模型直接拿用户话+上下文线索回应。"""
    try:
        return bool(get("UNDERSTANDING_ENABLED", True))
    except Exception:
        return True


def set(key: str, value):
    """设置单个配置项并持久化（简单键值，不做复杂校验）"""
    global _cache
    with _lock:
        cfg = _load()
        cfg[key] = value
        _cache = cfg
        _persist()
    return value


_VALID_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,127}$")

_TIME_RANGE_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")


def parse_time_range(s):
    """解析 '8-23' / '08:00-23:00' → (start_h, start_m, end_h, end_m)；非法返回 None。

    支持跨天区间（如 '22:00-06:00'），调用方需自行判断 in_range 的逻辑。
    """
    if not s:
        return None
    m = _TIME_RANGE_RE.match(str(s))
    if not m:
        return None
    sh, sm, eh, em = m.group(1), m.group(2) or "0", m.group(3), m.group(4) or "0"
    sh, sm, eh, em = int(sh), int(sm), int(eh), int(em)
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        return None
    return (sh, sm, eh, em)


def is_time_in_range(start: str, end: str, now=None) -> bool:
    """判断当前时间是否落在 HH:MM 区间；起点包含、终点不包含，支持跨天。"""
    from datetime import datetime
    now = now or datetime.now()
    parsed = parse_time_range(f"{start}-{end}")
    if not parsed:
        return False
    sh, sm, eh, em = parsed
    cur = now.hour * 60 + now.minute
    begin = sh * 60 + sm
    finish = eh * 60 + em
    if begin == finish:
        return False
    return (begin <= cur < finish) if begin < finish else (cur >= begin or cur < finish)


def is_dnd_now(now=None) -> bool:
    """全局免打扰是否正在生效。"""
    if not get("DND_ENABLED", True):
        return False
    return is_time_in_range(get("DND_START", "23:00"), get("DND_END", "07:00"), now)


def dnd_seconds_remaining(now=None) -> int:
    """距本次免打扰结束的秒数，供前端主动消息安排下次尝试。"""
    from datetime import datetime, timedelta
    now = now or datetime.now()
    if not is_dnd_now(now):
        return 0
    try:
        eh, em = map(int, str(get("DND_END", "07:00")).split(":"))
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return max(1, int((end - now).total_seconds()))
    except Exception:
        return 3600


def validate_model_id(mid) -> str:
    """校验模型 ID：非空、仅官方 ID 允许的字符集。非法抛 ValueError。"""
    mid = str(mid or "").strip()
    if not mid:
        raise ValueError("模型 ID 不能为空")
    if not _VALID_MODEL_RE.match(mid):
        raise ValueError("模型 ID 含有非法字符（只允许字母、数字、点、横线、下划线、冒号）")
    return mid


def update(patch: dict) -> dict:
    """更新配置并持久化。逐项校验，任何非法项抛 ValueError，不影响已保存配置。"""
    with _lock:
        cfg = _load()
        if "CURRENT_CHAT_MODEL" in patch:
            cfg["CURRENT_CHAT_MODEL"] = validate_model_id(patch["CURRENT_CHAT_MODEL"])
        if "MEMORY_EXTRACT_MODEL" in patch:
            cfg["MEMORY_EXTRACT_MODEL"] = validate_model_id(patch["MEMORY_EXTRACT_MODEL"])
        if "UNDERSTANDING_MODEL" in patch:
            # 理解层模型：允许空字符串（= 跟随主脑），非空则校验 ID
            _um = str(patch["UNDERSTANDING_MODEL"] or "").strip()
            cfg["UNDERSTANDING_MODEL"] = validate_model_id(_um) if _um else ""
        if "DEEP_THINKING_MODEL" in patch:
            # 深度思考模型：允许空字符串（= 回到旧的 deepseek-reasoner 行为）
            _dm = str(patch["DEEP_THINKING_MODEL"] or "").strip()
            cfg["DEEP_THINKING_MODEL"] = validate_model_id(_dm) if _dm else ""
        # ★ 2026-09-15：LLM 故障隔离参数（超时/熔断），供以后界面调参
        if "LLM_AUX_TIMEOUT_SEC" in patch or "LLM_LONG_TIMEOUT_SEC" in patch:
            _aux = float(patch.get("LLM_AUX_TIMEOUT_SEC", cfg.get("LLM_AUX_TIMEOUT_SEC", 30)))
            _lng = float(patch.get("LLM_LONG_TIMEOUT_SEC", cfg.get("LLM_LONG_TIMEOUT_SEC", 180)))
            if not (1 <= _aux <= 600):
                raise ValueError("杂活硬超时必须在 1~600 秒之间")
            if not (1 <= _lng <= 1800):
                raise ValueError("长档硬超时必须在 1~1800 秒之间")
            cfg["LLM_AUX_TIMEOUT_SEC"] = _aux
            cfg["LLM_LONG_TIMEOUT_SEC"] = _lng
        if "LLM_BREAKER_FAILS" in patch or "LLM_BREAKER_COOLDOWN_SEC" in patch:
            _bf = int(patch.get("LLM_BREAKER_FAILS", cfg.get("LLM_BREAKER_FAILS", 3)))
            _bc = int(patch.get("LLM_BREAKER_COOLDOWN_SEC", cfg.get("LLM_BREAKER_COOLDOWN_SEC", 300)))
            if not (1 <= _bf <= 20):
                raise ValueError("熔断阈值必须在 1~20 次之间")
            if not (10 <= _bc <= 3600):
                raise ValueError("熔断冷却必须在 10~3600 秒之间")
            cfg["LLM_BREAKER_FAILS"] = _bf
            cfg["LLM_BREAKER_COOLDOWN_SEC"] = _bc
        if "LLM_STREAM_FIRST_TIMEOUT_SEC" in patch or "LLM_STREAM_STALL_TIMEOUT_SEC" in patch:
            _sf = float(patch.get("LLM_STREAM_FIRST_TIMEOUT_SEC", cfg.get("LLM_STREAM_FIRST_TIMEOUT_SEC", 60)))
            _ss = float(patch.get("LLM_STREAM_STALL_TIMEOUT_SEC", cfg.get("LLM_STREAM_STALL_TIMEOUT_SEC", 90)))
            if not (1 <= _sf <= 600) or not (1 <= _ss <= 600):
                raise ValueError("流式超时必须在 1~600 秒之间")
            cfg["LLM_STREAM_FIRST_TIMEOUT_SEC"] = _sf
            cfg["LLM_STREAM_STALL_TIMEOUT_SEC"] = _ss
        if "LLM_FALLBACK_MODEL" in patch:
            _fm = str(patch["LLM_FALLBACK_MODEL"] or "").strip()
            cfg["LLM_FALLBACK_MODEL"] = validate_model_id(_fm) if _fm else ""
        # ★ 2026-09-15：主动消息单一引擎参数（设置页可调）
        for _bk in ("PROACTIVE_ENGINE_ENABLED", "PROACTIVE_FRONTEND_GENERATION",
                    "PROACTIVE_CAP_EXCLUDE_EXEMPT", "PROACTIVE_SMALL_CTX"):
            if _bk in patch:
                cfg[_bk] = bool(patch[_bk])
        if "PROACTIVE_DAILY_CAP" in patch:
            _c = int(patch["PROACTIVE_DAILY_CAP"])
            if not (0 <= _c <= 50):
                raise ValueError("每日主动消息上限必须在 0~50 之间（0 = 不限）")
            cfg["PROACTIVE_DAILY_CAP"] = _c
        if "PROACTIVE_QUIET_WINDOW_SEC" in patch:
            _q = int(patch["PROACTIVE_QUIET_WINDOW_SEC"])
            if not (60 <= _q <= 3600):
                raise ValueError("静默窗口必须在 60~3600 秒之间")
            cfg["PROACTIVE_QUIET_WINDOW_SEC"] = _q
        if "PROACTIVE_HELD_TTL_HOURS" in patch:
            _t = int(patch["PROACTIVE_HELD_TTL_HOURS"])
            if not (1 <= _t <= 72):
                raise ValueError("被拦念头的保留时长必须在 1~72 小时之间")
            cfg["PROACTIVE_HELD_TTL_HOURS"] = _t
        if "AUTO_MEMORY_INTERVAL" in patch:
            n = int(patch["AUTO_MEMORY_INTERVAL"])
            if not (2 <= n <= 20):
                raise ValueError("自动记忆提炼轮次必须在 2~20 之间")
            cfg["AUTO_MEMORY_INTERVAL"] = n
        if "LEGACY_MEMORY_PIPELINE" in patch:
            # 旧线记忆管线开关（默认关闭）：设为 true 可恢复"每轮 save_from_chat"的旧行为
            cfg["LEGACY_MEMORY_PIPELINE"] = bool(patch["LEGACY_MEMORY_PIPELINE"])
        if "PROMPT_COMPACT" in patch:
            # 提示词压缩模式：只切换 prompt 文本，不改变任何功能逻辑
            cfg["PROMPT_COMPACT"] = bool(patch["PROMPT_COMPACT"])
        if "IDLE_TRIGGER_MIN_MINUTES" in patch or "IDLE_TRIGGER_MAX_MINUTES" in patch:
            lo = int(patch.get("IDLE_TRIGGER_MIN_MINUTES", cfg["IDLE_TRIGGER_MIN_MINUTES"]))
            hi = int(patch.get("IDLE_TRIGGER_MAX_MINUTES", cfg["IDLE_TRIGGER_MAX_MINUTES"]))
            if lo <= 0:
                raise ValueError("主动发言最小间隔必须大于 0")
            if hi <= lo:
                raise ValueError("主动发言最大间隔必须大于最小间隔")
            if hi > 24 * 60:
                raise ValueError("主动发言最大间隔不能超过 1440 分钟")
            cfg["IDLE_TRIGGER_MIN_MINUTES"] = lo
            cfg["IDLE_TRIGGER_MAX_MINUTES"] = hi
        if "IDLE_AGENT_ENABLED" in patch:
            cfg["IDLE_AGENT_ENABLED"] = bool(patch["IDLE_AGENT_ENABLED"])
        if "UNDERSTANDING_ENABLED" in patch:
            cfg["UNDERSTANDING_ENABLED"] = bool(patch["UNDERSTANDING_ENABLED"])
        if "AUTONOMY_LEVEL" in patch:
            _al = str(patch["AUTONOMY_LEVEL"] or "").strip()
            cfg["AUTONOMY_LEVEL"] = _al if _al in ("conservative", "balanced", "autonomous", "free", "full") else "balanced"
        if "IDLE_AGENT_TIME_RANGE" in patch:
            p = parse_time_range(patch["IDLE_AGENT_TIME_RANGE"])
            if p is None:
                raise ValueError("主动发言时间范围格式应为 起始-结束，例如 8-23 或 08:00-23:00")
            sh, sm, eh, em = p
            cfg["IDLE_AGENT_TIME_RANGE"] = "%02d:%02d-%02d:%02d" % (sh, sm, eh, em)

        # ★ 早安/晚安/惊喜/免打扰 配置校验（HH:MM 严格格式 + 布尔开关）
        def _validate_time_str(val, field):
            """验证 HH:MM 格式时间字符串，返回规范化 %02d:%02d"""
            import re as _re
            s = str(val).strip()
            if not _re.match(r'^\d{1,2}:\d{2}$', s):
                raise ValueError(f"{field} 格式应为 HH:MM，例如 07:30")
            h, m = map(int, s.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"{field} 时间值超出范围（小时0-23，分钟0-59）")
            return "%02d:%02d" % (h, m)

        if "MORNING_ENABLED" in patch:
            cfg["MORNING_ENABLED"] = bool(patch["MORNING_ENABLED"])
        if "MORNING_START" in patch:
            cfg["MORNING_START"] = _validate_time_str(patch["MORNING_START"], "早安开始时间")
        if "MORNING_END" in patch:
            cfg["MORNING_END"] = _validate_time_str(patch["MORNING_END"], "早安结束时间")
        if "NIGHT_ENABLED" in patch:
            cfg["NIGHT_ENABLED"] = bool(patch["NIGHT_ENABLED"])
        if "NIGHT_START" in patch:
            cfg["NIGHT_START"] = _validate_time_str(patch["NIGHT_START"], "晚安开始时间")
        if "NIGHT_END" in patch:
            cfg["NIGHT_END"] = _validate_time_str(patch["NIGHT_END"], "晚安结束时间")
        if "SURPRISE_ENABLED" in patch:
            cfg["SURPRISE_ENABLED"] = bool(patch["SURPRISE_ENABLED"])
        # ★ 免打扰
        if "DND_ENABLED" in patch:
            cfg["DND_ENABLED"] = bool(patch["DND_ENABLED"])
        if "DND_START" in patch:
            cfg["DND_START"] = _validate_time_str(patch["DND_START"], "免打扰开始时间")
        if "DND_END" in patch:
            cfg["DND_END"] = _validate_time_str(patch["DND_END"], "免打扰结束时间")
        for _key in ("DND_ALLOW_REMINDERS", "DND_ALLOW_GREETINGS", "DND_ALLOW_CALLS"):
            if _key in patch:
                cfg[_key] = bool(patch[_key])
        if "SELECTED_MODEL" in patch:
            cfg["SELECTED_MODEL"] = validate_model_id(patch["SELECTED_MODEL"])
        if "VISION_MODEL" in patch:
            cfg["VISION_MODEL"] = validate_model_id(patch["VISION_MODEL"])
        if "VISION_PROVIDER" in patch:
            cfg["VISION_PROVIDER"] = str(patch["VISION_PROVIDER"]).strip()
        if "SCREEN_PERCEPTION_ENABLED" in patch:
            cfg["SCREEN_PERCEPTION_ENABLED"] = bool(patch["SCREEN_PERCEPTION_ENABLED"])
        if "SCREEN_INTERVAL" in patch:
            try:
                cfg["SCREEN_INTERVAL"] = int(patch["SCREEN_INTERVAL"])
            except (TypeError, ValueError):
                cfg["SCREEN_INTERVAL"] = 300
        if "SCREEN_MEMORY_ENABLED" in patch:
            cfg["SCREEN_MEMORY_ENABLED"] = bool(patch["SCREEN_MEMORY_ENABLED"])
        # ★ AI 监督吃醋系统（2026-09-11）：开关 + 参数（设置页「😤 监督吃醋」用）
        if "JEALOUSY_ENABLED" in patch:
            cfg["JEALOUSY_ENABLED"] = bool(patch["JEALOUSY_ENABLED"])
        if "JEALOUSY_TRIGGER" in patch:
            try:
                cfg["JEALOUSY_TRIGGER"] = max(10, min(100, int(patch["JEALOUSY_TRIGGER"])))
            except (TypeError, ValueError):
                cfg["JEALOUSY_TRIGGER"] = 60
        if "JEALOUSY_MESSAGE_COOLDOWN" in patch:
            try:
                cfg["JEALOUSY_MESSAGE_COOLDOWN"] = max(300, min(14400,
                                                               int(patch["JEALOUSY_MESSAGE_COOLDOWN"])))
            except (TypeError, ValueError):
                cfg["JEALOUSY_MESSAGE_COOLDOWN"] = 1800
        if "JEALOUSY_USAGE_WINDOW_MINUTES" in patch:
            try:
                cfg["JEALOUSY_USAGE_WINDOW_MINUTES"] = max(10, min(480,
                                                                   int(patch["JEALOUSY_USAGE_WINDOW_MINUTES"])))
            except (TypeError, ValueError):
                cfg["JEALOUSY_USAGE_WINDOW_MINUTES"] = 60
        if "JEALOUSY_DECAY_PER_HOUR" in patch:
            try:
                cfg["JEALOUSY_DECAY_PER_HOUR"] = max(0, min(50, float(patch["JEALOUSY_DECAY_PER_HOUR"])))
            except (TypeError, ValueError):
                cfg["JEALOUSY_DECAY_PER_HOUR"] = 5
        for _jk in ("JEALOUSY_RULES", "JEALOUSY_WHITELIST"):
            if _jk in patch and isinstance(patch[_jk], (dict, list)):
                cfg[_jk] = patch[_jk]
        if "MOMENT_FREQUENCY" in patch:
            try:
                cfg["MOMENT_FREQUENCY"] = max(0, min(3, int(patch["MOMENT_FREQUENCY"])))
            except (TypeError, ValueError):
                cfg["MOMENT_FREQUENCY"] = 2
        if "PROACTIVE_FREQUENCY" in patch:
            _pf = str(patch["PROACTIVE_FREQUENCY"]).strip()
            cfg["PROACTIVE_FREQUENCY"] = _pf if _pf in ("low", "normal", "high") else "normal"
        if "OFFLINE_ENABLED" in patch:
            cfg["OFFLINE_ENABLED"] = bool(patch["OFFLINE_ENABLED"])
        if "OFFLINE_MAX_DELAY_MINUTES" in patch:
            try:
                _od = max(1, min(720, int(patch["OFFLINE_MAX_DELAY_MINUTES"])))
            except (TypeError, ValueError):
                _od = 30
            cfg["OFFLINE_MAX_DELAY_MINUTES"] = _od
        if "AWARENESS_ENABLED" in patch:
            cfg["AWARENESS_ENABLED"] = bool(patch["AWARENESS_ENABLED"])
        # ★ iOS 远程桥接（邮件触发快捷指令）
        if "IOS_REMOTE_ENABLED" in patch:
            cfg["IOS_REMOTE_ENABLED"] = bool(patch["IOS_REMOTE_ENABLED"])
        for _ios_k in ("IOS_SMTP_HOST", "IOS_SMTP_USER", "IOS_SMTP_PASSWORD",
                       "IOS_MAIL_FROM", "IOS_MAIL_TO", "IOS_BARK_KEY"):
            if _ios_k in patch:
                cfg[_ios_k] = str(patch[_ios_k] or "").strip()
        if "IOS_SMTP_PORT" in patch:
            try:
                cfg["IOS_SMTP_PORT"] = int(patch["IOS_SMTP_PORT"])
            except (TypeError, ValueError):
                cfg["IOS_SMTP_PORT"] = 465
        if "RELATIONSHIP_PACE" in patch:
            _rp = str(patch["RELATIONSHIP_PACE"]).strip()
            cfg["RELATIONSHIP_PACE"] = _rp if _rp in ("slow", "normal", "fast") else "normal"
        # ★ 2026-09-15：亲密度自动成长开关 + 关系界面开关（都要能从前端改，故入白名单）
        for _rk in ("RELATIONSHIP_AUTO_UPDATE", "RELATIONSHIP_UI_VISIBLE"):
            if _rk in patch:
                cfg[_rk] = bool(patch[_rk])
        # ★ 2026-09-15：反思频率（daily=每天一次固定槽 / interval=旧的按小时间隔）
        if "REFLECTION_MODE" in patch:
            _rm = str(patch["REFLECTION_MODE"] or "").strip().lower()
            cfg["REFLECTION_MODE"] = _rm if _rm in ("daily", "interval") else "daily"
        if "REFLECTION_SLOT_HOUR" in patch:
            try:
                _sh = int(patch["REFLECTION_SLOT_HOUR"])
            except (TypeError, ValueError):
                _sh = 5
            cfg["REFLECTION_SLOT_HOUR"] = max(0, min(23, _sh))
        if "REFLECTION_INTERVAL_HOURS" in patch:
            try:
                _ih = float(patch["REFLECTION_INTERVAL_HOURS"])
            except (TypeError, ValueError):
                _ih = 24.0
            cfg["REFLECTION_INTERVAL_HOURS"] = max(0.5, min(168.0, _ih))
        # ★ 2026-09-15：语义分析模式（local 省钱默认 / llm 旧口径 / off）
        if "SEMANTIC_ANALYZER_MODE" in patch:
            _sm = str(patch["SEMANTIC_ANALYZER_MODE"] or "").strip().lower()
            cfg["SEMANTIC_ANALYZER_MODE"] = _sm if _sm in ("local", "llm", "off") else "local"
        # ★ 2026-09-15：抽取器合并开关
        if "MERGED_EXTRACT_ENABLED" in patch:
            cfg["MERGED_EXTRACT_ENABLED"] = bool(patch["MERGED_EXTRACT_ENABLED"])
        # ★ 2026-09-15：主链提示词场景裁剪开关
        if "SCENE_PROMPT_TRIM" in patch:
            cfg["SCENE_PROMPT_TRIM"] = bool(patch["SCENE_PROMPT_TRIM"])
        if "api_key" in patch:
            k = str(patch["api_key"] or "").strip()
            if k and not k.startswith("sk-"):
                raise ValueError("DeepSeek API Key 一般以 sk- 开头，请检查")
            cfg["api_key"] = k
        # ★ 视觉 Key（游戏画面感知/发图片分析用，前端同步）
        if "vision_api_key" in patch:
            cfg["vision_api_key"] = str(patch["vision_api_key"] or "").strip()
        # ★ OpenAI Key（gpt-5.4 等）；一般留空让 openai_api_key() 回退到 claude_api_key()，
        #   因为同走 openclawplan 中转，用户填一次 key 就行
        if "openai_api_key" in patch:
            cfg["openai_api_key"] = str(patch["openai_api_key"] or "").strip()
        # ★ xAI Grok Key（grok-420-thinking 等）；留空回退 claude_api_key（同一中转一密码）
        if "xai_api_key" in patch:
            cfg["xai_api_key"] = str(patch["xai_api_key"] or "").strip()

        # ★ QQ 机器人绑定（OneBot 复用哪个角色的人格+大脑；空=default 跟随全局）
        if "QQ_CHARACTER" in patch:
            cfg["QQ_CHARACTER"] = str(patch["QQ_CHARACTER"] or "").strip()
        if "QQ_ALLOWED_USERS" in patch:
            _qq_users = patch["QQ_ALLOWED_USERS"]
            cfg["QQ_ALLOWED_USERS"] = [str(x).strip() for x in _qq_users] if isinstance(_qq_users, list) else []
        if "QQ_SESSION_ID" in patch:
            cfg["QQ_SESSION_ID"] = str(patch["QQ_SESSION_ID"] or "").strip()

        # ★ TTS 语音合成 Key（纯字符串配置，落盘 backend/data/config.json）
        for _k in (
            "tts_provider",
            "minimax_api_key", "minimax_group_id",
            "volcengine_app_id", "volcengine_access_token", "volcengine_cluster",
            "xunfei_app_id", "xunfei_api_key", "xunfei_api_secret",
            "azure_speech_key", "azure_speech_region",
            "elevenlabs_api_key",
            # 阿里云百炼 DashScope（语音通话付费链路）
            "dashscope_api_key",
            # 智谱 GLM（GLM-5.1 / GLM-5.3-Flash）
            "zhipu_api_key",
            "call_asr_provider", "call_tts_provider",
            "aliyun_tts_model", "aliyun_tts_format", "aliyun_asr_model",
            "voice_replica_voice_id", "voice_replica_target_model",
            "voice_replica_source",
        ):
            if _k in patch:
                cfg[_k] = str(patch[_k] or "").strip()
        _persist()
        return dict(cfg)


# 运行时注入的 Key：由前端请求透传，优先级最低，
# 只有在 env / .env / 根 config.json / 后端 config 全都为空时才生效。
# 打包运行时 DATA_DIR 会被 AI_COMPANION_DATA_DIR 重定向到 userData，
# 后端配置里常常没有 api_key，于是"语义分析"这类后端自调用模块
# 拿不到 key 只能静默降级 —— 等于意图分析从未真正生效过。
# 这里借用前端每次请求都带上的 Key 来补齐，且不会覆盖已有配置。
_RUNTIME_API_KEY = ""


def set_runtime_api_key(key: str):
    """注入运行时 API Key。传空串表示清除。"""
    global _RUNTIME_API_KEY
    _RUNTIME_API_KEY = str(key or "").strip()


def runtime_api_key() -> str:
    """读取前端透传的运行时 Key（可能为空）。

    api_key() 里运行时 Key 优先级最低，后端 config 一旦存着失效的旧 Key，
    前端传的有效 Key 就永远取不到 —— 语义分析等后端自调用模块会一直 401。
    这里单独暴露，供调用方在拿到 401 时兜底重试。
    """
    return _RUNTIME_API_KEY


def api_key() -> str:
    """DeepSeek Key：env > .env > 根 config.json > 后端 config > 运行时注入"""
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    env = _load_env_file().get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    root = _read_json(ROOT_CONFIG, {})
    k = str(root.get("apiKey") or "").strip()
    if k:
        return k
    return str(_load().get("api_key") or "").strip() or _RUNTIME_API_KEY


def vision_key() -> str:
    """视觉模型 Key：按当前视觉 provider 选对应 key（provider 感知）。

    ★ 修复：之前 vision_api_key 一票优先——provider=zhipu 时拿着阿里云的
      vision_api_key 去请求智谱 → 401「令牌已过期或验证不正确」。
      现在：zhipu → zhipu_api_key；dashscope → vision_api_key/dashscope_api_key。
    """
    # 1. 显式环境变量（VISION_API_KEY 优先）
    k = os.environ.get("VISION_API_KEY", "").strip()
    if k:
        return k
    env = _load_env_file()
    k = str(env.get("VISION_API_KEY") or "").strip()
    if k:
        return k

    provider = ""
    try:
        provider = vision_provider().strip().lower()
    except Exception:
        pass
    cfg = _load()
    generic = str(cfg.get("vision_api_key") or "").strip()

    if provider == "zhipu":
        z = zhipu_api_key()
        if z:
            return z
        return generic
    if provider in ("dashscope", "aliyun"):
        ds = str(cfg.get("dashscope_api_key") or "").strip()
        return ds or generic
    if provider == "siliconflow":
        sf = str(cfg.get("siliconflow_api_key") or "").strip()
        return sf or generic
    return generic


def vision_provider() -> str:
    """视觉服务商：dashscope / siliconflow / zhipu / deepseek。
    ★ 当前 VISION_MODEL 在模型池里时，以池里的 provider 为准——
      否则选了 glm-5.3-flash（zhipu）但 VISION_PROVIDER 还是旧值 dashscope，
      视觉链路会拿着智谱模型去请求阿里云 → 永远失败。
    ★ 数据目录 config 优先于项目根 config（设置页保存写的是数据目录）。"""
    try:
        vcfg = get_vision_model_config(vision_model())
        if vcfg.get("provider"):
            return str(vcfg["provider"]).strip()
    except Exception:
        pass
    p = str(_load().get("VISION_PROVIDER") or "").strip()
    if p:
        return p
    root = _read_json(ROOT_CONFIG, {})
    return str(root.get("VISION_PROVIDER") or root.get("visionProvider") or "dashscope").strip()


def vision_base_url() -> str:
    """视觉接口地址（为空时使用各 provider 默认地址）。
    ★ 同 vision_provider：VISION_MODEL 在池里时以池里的 baseUrl 为准；
      数据目录 config 优先于项目根 config。"""
    try:
        vcfg = get_vision_model_config(vision_model())
        if vcfg.get("baseUrl"):
            return str(vcfg["baseUrl"]).strip()
    except Exception:
        pass
    url = str(_load().get("VISION_BASE_URL") or "").strip()
    if url:
        return url
    root = _read_json(ROOT_CONFIG, {})
    return str(root.get("VISION_BASE_URL") or root.get("visionBaseUrl") or "").strip()


def vision_model() -> str:
    """当前视觉模型（下拉 key）。
    ★ 数据目录 config 优先于项目根 config——设置页保存写的是数据目录，
      之前根 config 的旧值 qwen-vl-max 一直把它盖住，导致视觉链路走错模型。"""
    m = str(_load().get("VISION_MODEL") or "").strip()
    if m:
        return m
    root = _read_json(ROOT_CONFIG, {})
    m = str(root.get("visionModel") or root.get("VISION_MODEL") or "").strip()
    if m:
        return m
    return "qwen-vl-max"


def weather_key() -> str:
    """天气 API Key（和风天气 / OpenWeather 等）：env > .env > 根 config.json > 后端 config"""
    # 1. 环境变量
    key = os.environ.get("WEATHER_API_KEY", "").strip()
    if key:
        return key
    # 2. .env
    env = _load_env_file()
    key = env.get("WEATHER_API_KEY", "").strip()
    if key:
        return key
    # 3. 根 config.json（兼容旧字段 weatherApiKey）
    root = _read_json(ROOT_CONFIG, {})
    k = str(root.get("weatherApiKey") or root.get("WEATHER_API_KEY") or "").strip()
    if k:
        return k
    # 4. 后端 config
    return str(_load().get("weather_api_key") or "").strip()


# ★ ASR Key（env > .env > 根 config.json > 后端 config）
def asr_key() -> str:
    """ASR 语音识别 API Key：env > .env > 根 config.json > 后端 config"""
    key = os.environ.get("ASR_API_KEY", "").strip()
    if key:
        return key
    env = _load_env_file()
    key = env.get("ASR_API_KEY", "").strip()
    if key:
        return key
    root = _read_json(ROOT_CONFIG, {})
    k = str(root.get("asrApiKey") or root.get("ASR_API_KEY") or "").strip()
    if k:
        return k
    return str(_load().get("asr_api_key") or "").strip()


# ★ ASR Provider（whisper_local / azure / xunfei / aliyun / tencent）
def asr_provider() -> str:
    """ASR 服务商：whisper_local / azure / xunfei / aliyun / tencent"""
    p = os.environ.get("ASR_PROVIDER", "").strip()
    if p:
        return p
    env = _load_env_file()
    p = env.get("ASR_PROVIDER", "").strip()
    if p:
        return p
    root = _read_json(ROOT_CONFIG, {})
    p = str(root.get("asrProvider") or root.get("ASR_PROVIDER") or "").strip()
    if p:
        return p
    return str(_load().get("asr_provider") or "whisper_local").strip()


# ==================== TTS 语音合成 Key 读取 ====================
# 统一优先级：环境变量 > 项目根 .env > 根 config.json > backend/data/config.json
# （环境变量仍是最高优先级，行为与现有 api_key/vision_key/asr_key 一致，仅在 env 为空时回退 config）

def _resolve_str(env_name: str, cfg_key: str, default: str = "", root_keys=()) -> str:
    """通用字符串配置解析：env > .env > 根 config.json > 后端 config"""
    v = os.environ.get(env_name, "").strip()
    if v:
        return v
    env = _load_env_file()
    v = env.get(env_name, "").strip()
    if v:
        return v
    root = _read_json(ROOT_CONFIG, {})
    for k in root_keys:
        v = str(root.get(k) or "").strip()
        if v:
            return v
    return str(_load().get(cfg_key) or default).strip()


def tts_provider() -> str:
    """默认 TTS 服务商：edge-tts / minimax / volcengine / xunfei / azure / elevenlabs"""
    return _resolve_str("TTS_PROVIDER", "tts_provider", "edge-tts", ("ttsProvider", "TTS_PROVIDER"))


def minimax_api_key() -> str:
    return _resolve_str("MINIMAX_API_KEY", "minimax_api_key", "", ("minimaxApiKey",))


def minimax_group_id() -> str:
    return _resolve_str("MINIMAX_GROUP_ID", "minimax_group_id", "", ("minimaxGroupId",))


def volcengine_app_id() -> str:
    return _resolve_str("VOLCENGINE_APP_ID", "volcengine_app_id", "", ("volcengineAppId",))


def volcengine_access_token() -> str:
    return _resolve_str("VOLCENGINE_ACCESS_TOKEN", "volcengine_access_token", "", ("volcengineAccessToken",))


def volcengine_cluster() -> str:
    return _resolve_str("VOLCENGINE_CLUSTER", "volcengine_cluster", "volcano_tts", ("volcengineCluster",))


def xunfei_app_id() -> str:
    return _resolve_str("XUNFEI_APP_ID", "xunfei_app_id", "", ("xunfeiAppId",))


def xunfei_api_key() -> str:
    return _resolve_str("XUNFEI_API_KEY", "xunfei_api_key", "", ("xunfeiApiKey",))


def xunfei_api_secret() -> str:
    return _resolve_str("XUNFEI_API_SECRET", "xunfei_api_secret", "", ("xunfeiApiSecret",))


def azure_speech_key() -> str:
    return _resolve_str("AZURE_SPEECH_KEY", "azure_speech_key", "", ("azureSpeechKey",))


def azure_speech_region() -> str:
    return _resolve_str("AZURE_SPEECH_REGION", "azure_speech_region", "eastasia", ("azureSpeechRegion",))


def elevenlabs_api_key() -> str:
    return _resolve_str("ELEVENLABS_API_KEY", "elevenlabs_api_key", "", ("elevenLabsApiKey",))


# ==================== 阿里云百炼 DashScope（语音通话付费链路） ====================

def dashscope_api_key() -> str:
    """阿里云百炼 API Key：通话 ASR 与 TTS 共用同一个 Key"""
    return _resolve_str("DASHSCOPE_API_KEY", "dashscope_api_key", "", ("dashscopeApiKey",))


def zhipu_api_key() -> str:
    """智谱 GLM API Key（GLM-5.1 / GLM-5.3-Flash 用）"""
    return _resolve_str("ZHIPU_API_KEY", "zhipu_api_key", "", ("zhipuApiKey",))


def claude_api_key() -> str:
    """Claude API Key（Anthropic 官方或中转站）"""
    return _resolve_str("CLAUDE_API_KEY", "claude_api_key", "", ("claudeApiKey",))


def google_api_key() -> str:
    """Gemini API Key（Google AI Studio / 中转站）"""
    return _resolve_str("GOOGLE_API_KEY", "google_api_key", "", ("googleApiKey",))


def openai_api_key() -> str:
    """OpenAI API Key（GPT-5.4 等）。

    与 Claude/Gemini 一样走 openclawplan 中转，所以**没填 openai_api_key 时回退
    claude_api_key()**——同一个中转同一个密码，避免用户再多填一遍。
    想用官方 OpenAI Key 时，把 openai_api_key 填上就会优先用它。
    """
    _k = _resolve_str("OPENAI_API_KEY", "openai_api_key", "", ("openaiApiKey",))
    if _k:
        return _k
    return claude_api_key()


def xai_api_key() -> str:
    """xAI Grok API Key。与 Claude/GPT 一样走 openclawplan 中转，所以没填 xai_api_key
    时回退 claude_api_key()——同一个中转同一个密码。"""
    _k = _resolve_str("XAI_API_KEY", "xai_api_key", "", ("xaiApiKey",))
    if _k:
        return _k
    return claude_api_key()


def call_asr_provider() -> str:
    """语音通话的输入识别：aliyun（付费）/ whisper_local（本地免费）"""
    return _resolve_str("CALL_ASR_PROVIDER", "call_asr_provider", "aliyun", ("callAsrProvider",))


def call_tts_provider() -> str:
    """语音通话的输出合成：aliyun（付费）/ cosyvoice（本地）/ edge-tts（免费）"""
    return _resolve_str("CALL_TTS_PROVIDER", "call_tts_provider", "aliyun", ("callTtsProvider",))


def aliyun_tts_model() -> str:
    return _resolve_str("ALIYUN_TTS_MODEL", "aliyun_tts_model", "qwen-audio-3.0-tts-flash", ("aliyunTtsModel",))


def aliyun_tts_format() -> str:
    return _resolve_str("ALIYUN_TTS_FORMAT", "aliyun_tts_format", "wav", ("aliyunTtsFormat",))


def aliyun_asr_model() -> str:
    return _resolve_str("ALIYUN_ASR_MODEL", "aliyun_asr_model", "paraformer-realtime-v2", ("aliyunAsrModel",))


def voice_replica_voice_id() -> str:
    """云端声音复刻出来的音色 ID（空表示还没复刻）"""
    return _resolve_str("VOICE_REPLICA_VOICE_ID", "voice_replica_voice_id", "", ("voiceReplicaVoiceId",))


def voice_replica_target_model() -> str:
    return _resolve_str("VOICE_REPLICA_TARGET_MODEL", "voice_replica_target_model", "", ("voiceReplicaTargetModel",))


def voice_replica_source() -> str:
    return _resolve_str("VOICE_REPLICA_SOURCE", "voice_replica_source", "", ("voiceReplicaSource",))


def set_voice_replica(voice_id: str, target_model: str = "", source: str = "") -> None:
    """保存复刻成功的音色 ID（通话 TTS 直接使用）"""
    try:
        cfg = _load()
        cfg["voice_replica_voice_id"]   = str(voice_id or "").strip()
        cfg["voice_replica_target_model"] = str(target_model or "").strip()
        if source:
            cfg["voice_replica_source"] = str(source).strip()
        _persist()
    except Exception as e:
        print(f"[Config] 保存复刻音色失败: {e}", flush=True)


# ==================== 模型池配置读取 ====================

def text_models() -> dict:
    """文本模型池：DEFAULTS + backend config.json + 根 config.json 三层合并（后者覆盖同名 key）。
    ★ DEFAULTS 里的模型（如 glm-5.1 / glm-5.3-flash）不能被 config.json 整个覆盖掉。"""
    models = dict(DEFAULTS.get("TEXT_MODELS") or {})
    _cfg_models = (_load().get("TEXT_MODELS") or {})
    if isinstance(_cfg_models, dict):
        models.update(_cfg_models)
    root_models = _read_json(ROOT_CONFIG, {}).get("TEXT_MODELS") or {}
    if isinstance(root_models, dict):
        models.update(root_models)
    return models


def vision_models() -> dict:
    """视觉模型池：DEFAULTS + backend config.json + 根 config.json 三层合并（后者覆盖同名 key）。
    ★ DEFAULTS 里的模型（如 glm-5.3-flash）不能被 config.json 整个覆盖掉——否则新增
      的视觉模型在旧 config.json 存在 VISION_MODELS 时永远不显示（用户反馈下拉里没有 glm-5.3-flash）。"""
    models = dict(DEFAULTS.get("VISION_MODELS") or {})
    _cfg_models = (_load().get("VISION_MODELS") or {})
    if isinstance(_cfg_models, dict):
        models.update(_cfg_models)
    root_models = _read_json(ROOT_CONFIG, {}).get("VISION_MODELS") or {}
    if isinstance(root_models, dict):
        models.update(root_models)
    return models


def selected_model() -> str:
    """当前选择的模型：SELECTED_MODEL > CURRENT_CHAT_MODEL > deepseek-chat"""
    return str(
        _load().get("SELECTED_MODEL")
        or _load().get("CURRENT_CHAT_MODEL")
        or "deepseek-chat"
    )


# ==================== 本地大脑（Ollama）配置读取（2026-09-11） ====================

_LOCAL_BRAIN_ALLOW = {"enabled", "baseUrl", "model", "fallback_model",
                      "think", "num_ctx", "keep_alive", "ctx_budget_tokens"}


def local_brain() -> dict:
    """本地大脑配置：DEFAULTS + config.json 合并（后者覆盖同名 key）。"""
    merged = dict(DEFAULTS.get("LOCAL_BRAIN") or {})
    try:
        saved = _load().get("LOCAL_BRAIN")
        if isinstance(saved, dict):
            merged.update(saved)
    except Exception:
        pass
    return merged


def local_brain_enabled() -> bool:
    """本地大脑总开关（人格页开关之外的服务级使能；服务不可达时聊天层还有云端回退兜底）。"""
    try:
        return bool(local_brain().get("enabled", True))
    except Exception:
        return False


def local_brain_model() -> str:
    """当前选中的本地模型名（ollama 名，如 qwen3:4b / 训练回灌的自定义名）。"""
    return str(local_brain().get("model") or "qwen3:4b").strip()


def save_local_brain(patch: dict) -> dict:
    """局部更新 LOCAL_BRAIN（仅白名单字段），落盘并返回新配置。前端换模型/切思考走这里。"""
    cur = local_brain()
    for k, v in (patch or {}).items():
        if k in _LOCAL_BRAIN_ALLOW:
            cur[k] = v
    set("LOCAL_BRAIN", cur)
    return cur


def _character_field(character_id: str, field: str) -> str:
    """读角色卡里的模型字段（人格设置页配的）。异常/无角色一律返回空串。
    ★ 延迟导入：character_manager 反向依赖本模块，只能在调用时导入。"""
    cid = str(character_id or "").strip()
    if not cid:
        return ""
    try:
        from . import character_manager as _cm
        _cfg = _cm.get_character_any(cid) or {}
        return str(_cfg.get(field) or "").strip()
    except Exception:
        return ""


def understanding_model(character_id: str = "") -> str:
    """理解层模型：角色卡 understanding_model（人格设置）> 全局 UNDERSTANDING_MODEL（legacy）> 空。
    空 = 调用方自己决定（跟随生成层）。"""
    return _character_field(character_id, "understanding_model") or str(
        _load().get("UNDERSTANDING_MODEL") or ""
    ).strip()


def relationship_auto_update() -> bool:
    """亲密度/关系是否还跑每轮的自动成长更新（默认 False = 已砍）。

    统一入口，避免各处自己解析字符串（config.json 里可能是 true/false/"false"）。
    读不到配置时按 **False**（已砍）处理 —— 默认省钱、默认不重置。
    """
    try:
        _v = _load().get("RELATIONSHIP_AUTO_UPDATE", False)
    except Exception:
        return False
    if isinstance(_v, str):
        return _v.strip().lower() in ("1", "true", "yes", "on")
    return bool(_v)


def scheduler_aux_model() -> str:
    """调度器后台判定/文案用的模型。

    ★ 2026-09-15 用户拍板："模型分层这一块**根据主脑来决定**" ——
      所以留空时**跟随主脑**（走既有 background_model 口径：主脑是旗舰/推理档就降一档省钱，
      主脑本来就是便宜档就原样用），而不是写死某个厂的模型。
      换大脑（GLM→DeepSeek/Qwen/Claude…）时这一层自动跟着换 provider，不会串 key。
      想强制指定（比如更省的 glm-4-flash），把 config 的 SCHEDULER_AUX_MODEL 显式写上即可。
    """
    _m = str(_load().get("SCHEDULER_AUX_MODEL") or "").strip()
    if _m:
        return _m
    # 跟随主脑：brain_model_for() 已按"人格设置里的角色卡 model > 全局 SELECTED_MODEL"
    # 解析出主脑（刻意不套深度思考档），再用 background_model() 做同族降档。
    try:
        from . import llm_guard as _lg
        _brain = ""
        try:
            _brain = _lg.brain_model_for() or ""
        except Exception:
            _brain = ""
        if _brain:
            return background_model(_brain)
    except Exception:
        pass
    try:
        return background_model()
    except Exception:
        return "glm-5.3-flash"


def scheduler_aux_key() -> str:
    """上文模型的 Key（按 provider 分流）。"""
    return api_key_for_model(scheduler_aux_model())


def scene_prompt_trim() -> bool:
    """主链提示词是否按场景裁剪（省 token ③，默认 True）。

    读不到配置时按 **True**（默认省）处理；判定逻辑在 chat_logic._should_scene_trim()。
    """
    try:
        _v = _load().get("SCENE_PROMPT_TRIM", True)
    except Exception:
        return True
    if isinstance(_v, str):
        return _v.strip().lower() in ("1", "true", "yes", "on")
    return bool(_v)


def relationship_ui_visible() -> bool:
    """关系/亲密度数值面板是否展示（默认 False = 已隐藏）。"""
    try:
        _v = _load().get("RELATIONSHIP_UI_VISIBLE", False)
    except Exception:
        return False
    if isinstance(_v, str):
        return _v.strip().lower() in ("1", "true", "yes", "on")
    return bool(_v)


def memory_extract_model(character_id: str = "", respect_breaker: bool = True) -> str:
    """记忆提炼/后台轻任务模型（**只影响后台杂活**）。

    优先级：
      1. 角色卡 memory_model（人格设置页给该角色单独配的）
      2. 全局 MEMORY_EXTRACT_MODEL（legacy）
      3. background_model(该角色的主脑) —— 跟随主脑，但主脑是深度推理/旗舰时降级省钱

    ★ 2026-09-14 修（重要）：原来第 3 档硬编码 `"deepseek-chat"`，导致三处不一致：
      · 本文件 :688 的注释与 :694-695 的 `get()` 特例都写着「留空 → background_model()」；
      · `_BACKGROUND_DOWNGRADE`（:1451）那张降级表**从未被本函数使用** → 表形同虚设；
      · 主脑是 `deepseek-chat` 时表里没有映射，于是记忆提炼/画像/情绪/关系/开环/
        AI状态/行为/人格/知识图谱/日周月总结等十余个机械抽取器**全部跑主脑同款模型**。
      现在真正接线到 background_model()：主脑仍是聪明模型（生成层完全不受影响），
      后台杂活自动降到同 provider 的便宜档；主脑本来就是便宜模型时则原样不变。

    ★ 2026-09-15 修（DeepSeek 宕机事故）：上面的"便宜档"若和杂活同 provider，provider
      一挂，二十多个抽取器就一起挂 900 秒并把连接池占满（真机日志行 143293-144455）。
      现在 provider 熔断期间**自动改用该角色的大脑**（决策 D2：保留 deepseek-chat 省钱，
      但挂了就切大脑），恢复后自动切回。respect_breaker=False 拿"原始配置值"，
      供 llm_guard 判定超时档位用（熔断期的返回值是降级结果，不能反过来当配置读）。
    """
    _explicit = (
        _character_field(character_id, "memory_model")
        or str(_load().get("MEMORY_EXTRACT_MODEL") or "").strip()
    )
    if _explicit:
        _primary = _explicit
    else:
        try:
            _primary = background_model(_character_field(character_id, "model"))
        except Exception:
            _primary = background_model()
    if not respect_breaker:
        return _primary
    try:
        from . import llm_guard as _lg
        if _lg.is_open(_primary):
            _brain = _lg.brain_model_for(character_id)
            if _brain and _brain != _primary and not _lg.is_open(_brain):
                _lg.note("杂活模型 %s（%s 熔断中）→ 改用大脑 %s"
                         % (_primary, _lg.provider_of(_primary), _brain))
                return _brain
    except Exception:
        pass
    return _primary


# 主脑（聊天/主动发言模型）→ 后台轻量任务（记忆提炼/理解层）降级用模型。
# 深度推理 / 旗舰模型做分类、提炼 JSON 这类轻活太贵，自动降级到同 provider 的便宜模型省钱。
_BACKGROUND_DOWNGRADE = {
    "deepseek-reasoner": "deepseek-v4-flash",  # 深度推理 → 快·省
    "deepseek-v4-pro":   "deepseek-v4-flash",  # 旗舰 → 快·省
    # ★ 2026-09-14 新增：主脑是 deepseek-chat（官方默认，也是本项目出厂默认）时，
    #   原先在表里**没有映射** → 记忆提炼/画像/情绪/关系/开环/AI状态/行为/人格/
    #   知识图谱/日周月总结等十余个机械抽取器全部跑主脑同款模型，一分钱没省。
    #   这里降到同 provider 的 Flash 档（key 相同、协议相同、改完即可用）。
    #   ★ 想更省：把 config.json 的 MEMORY_EXTRACT_MODEL 显式设为 "glm-4-flash"
    #     （智谱免费额度，已配 key），或设为 "glm-5.3-flash"。留空 = 走本表。
    "deepseek-chat":     "deepseek-v4-flash",  # 官方默认主脑 → 快·省
    "glm-5.3":           "glm-5.3-flash",      # 旗舰 → Flash
    "glm-5.1":           "glm-5.3-flash",      # 旧旗舰 → Flash
    "glm-4.7":           "glm-4-flash",        # 4.7 → 免费无限 Flash
    "glm-4.5-air":       "glm-4-flash",        # Air → 免费无限 Flash
    # ★ Claude（Anthropic/中转）没有便宜 Flash 版，杂活统一交给 glm-5.3-flash
    "claude-sonnet-4-20250514": "glm-5.3-flash",
    "claude-3-7-sonnet-20250219": "glm-5.3-flash",
    "claude-3-5-sonnet-20241022": "glm-5.3-flash",
    "claude-3-5-haiku-20241022": "glm-5.3-flash",
    "claude-sonnet-5": "glm-5.3-flash",
    # ★ Gemini 没有便宜 Flash，杂活统一交给 glm-5.3-flash
    "gemini-3.1-pro-high": "glm-5.3-flash",
    # ★ OpenAI（GPT-5.4 等）走同一个中转但单价高（$2.5 输入/$15 缓存/$0.25 输出），
    #   后台提炼/理解这类轻活交给 glm-5.3-flash，避免烧钱
    "gpt-5.4": "glm-5.3-flash",
    # ★ 本地大脑只切聊天主脑（2026-09-11 用户拍板）：理解层/记忆提炼等后台轻任务
    #   永远不走本地 4B（又慢又影响记忆质量），自动降级到云端便宜快模型
    "local-brain": "deepseek-v4-flash",
}


def background_model(model: str = "") -> str:
    """后台轻量任务（记忆提炼/理解层）使用的模型：跟随主脑，但主脑是深度推理/旗舰时降级省钱。

    可选传 model：传了则对该模型降级（用于理解层跟随单角色大脑时也降级）；
    不传则对全局 selected_model() 降级。
    """
    m = (str(model or "").strip()) or selected_model()
    return _BACKGROUND_DOWNGRADE.get(m, m)


def model_supports_reasoning_effort(model_id: str = "") -> bool:
    """判断模型是否支持 reasoning_effort 参数（仅智谱 GLM-5.3 系列支持 low/high/max）。
    GLM-4.x 系列（4.7 / 4.5-air / 4-flash）与 DeepSeek 均不支持该参数，传入会导致报错，必须拦截。

    ⚠ 实测（2026-09-07，glm-5.3-flash，5 条消息×3 组对照）：该模型默认就会思考
    （基线 5/5 触发 reasoning_content），但传 reasoning_effort=high 反而把触发率
    压到 2/5 —— 智谱兼容层对该参数的处理不利于「先想再答」。因此池外的
    glm-5.3-* 直填 id 刻意**不**放行，保持不传参数 = 走模型默认思考。
    若未来要控思考力度，用 thinking={"type":"enabled"}（实测 5/5，无副作用）。
    """
    _m = str(model_id or "").strip() or background_model()
    try:
        _cfg = (text_models() or {}).get(_m) or {}
        _provider = str(_cfg.get("provider") or "").lower()
        if _provider != "zhipu":
            return False
        # 仅 GLM-5.3 / GLM-5.3-Flash 支持 reasoning_effort
        _model = str(_cfg.get("model") or _m)
        return _model.startswith("glm-5.3")
    except Exception:
        return False


def api_key_for_model(model_id: str = "") -> str:
    """按模型 provider 返回对应 Key；没配 provider 或 provider 无专属 Key 时回退 DeepSeek Key。"""
    try:
        _cfg = (text_models() or {}).get(model_id) or {}
        _provider = str(_cfg.get("provider") or "").lower()
        if _provider == "local":
            # 本地 Ollama 不需要真 Key；给个占位避免 /api/chat 的空 Key 拦截（401 守卫）
            return "ollama-local"
        if _provider == "zhipu":
            _k = zhipu_api_key()
            if _k:
                return _k
        if _provider in ("anthropic", "claude"):
            _k = claude_api_key()
            if _k:
                return _k
        if _provider == "google":
            _k = google_api_key()
            if _k:
                return _k
        if _provider == "openai":
            # ★ 一个中转一个密码：openai_api_key() 内部已回退到 claude_api_key()，
            #   所以这里直接拿就行，不用再单独回退。
            _k = openai_api_key()
            if _k:
                return _k
        if _provider == "xai":
            # ★ xAI Grok 与 Claude/GPT 同一 openclawplan 中转、同一密码
            _k = xai_api_key()
            if _k:
                return _k
    except Exception:
        pass
    return api_key()


def chat_key() -> str:
    """当前聊天/主动消息模型的 Key（按 provider 分流，GLM→zhipu，否则回退 DeepSeek）。"""
    _m = str(_load().get("CURRENT_CHAT_MODEL")
             or _load().get("SELECTED_MODEL")
             or "deepseek-chat").strip()
    return api_key_for_model(_m)


def memory_key() -> str:
    """记忆提炼等后台任务的 Key（按 provider 分流，跟随主脑，主脑贵时降级）。"""
    _m = str(get("MEMORY_EXTRACT_MODEL") or "").strip() or background_model()
    return api_key_for_model(_m)


# 大脑 provider → 该 provider 的「深度思考模型」（2026-09-15 决策 D1）。
# 原则：**思考模型不跨 provider** —— 大脑是谁家的，思考就用谁家的强模型，
# 这样「人格设置里是 GLM 大脑」时，回复链路不会偷偷打到 DeepSeek 上。
_DEEP_THINKING_BY_PROVIDER = {
    # 实测最优（V4.1 Flash）：有思考过程（reasoning_content）、比 reasoner 快、
    # 比 glm-5.3-flash 快约 4 倍 —— 2026-09-11 的结论继续有效，DeepSeek 大脑照用。
    "deepseek": "deepseek-flash",
    # ★ 2026-09-15 修（用户实测账单事故）：原来这里写的是 glm-5.3（智谱**旗舰**），
    #   于是"大脑=glm-5.3-flash + 深度思考开"的实跑模型变成旗舰，45 分钟烧掉 13.5 万 token。
    #   用户原话："我人格设置大脑选的 5.3flash，而且它本身就是思考模型" ——
    #   glm-5.3-flash 自带 reasoning，不需要、也不该被换成旗舰。
    #   现在同档跟随：思考档 = glm-5.3-flash（要更强由用户显式配 DEEP_THINKING_MODEL）。
    "zhipu": "glm-5.3-flash",
}

# 模型价位档（只用于"别静默升档"的护栏；同档/降档一律放行）
#   1 = 免费/最便宜   2 = flash 档   3 = 标准档   4 = 旗舰/推理档
_MODEL_TIER = {
    "glm-4-flash": 1, "glm-4.5-air": 1, "glm-4.7": 1, "qwen-plus": 1,
    "local-brain": 1, "kimi": 3, "qwen-max": 3,
    "glm-5.3-flash": 2, "deepseek-flash": 2, "deepseek-v4-flash": 2,
    "deepseek-chat": 3, "gemini-3.7-flash": 3, "grok-420-thinking": 3,
    "claude-3-5-haiku-20241022": 3,
    "glm-5.3": 4, "glm-5.1": 4, "deepseek-reasoner": 4, "deepseek-v4-pro": 4,
    "claude-sonnet-5": 4, "claude-opus-4-6": 4, "gpt-5.4": 4,
    "gemini-3.1-pro-high": 4, "claude-sonnet-4-20250514": 4,
    "claude-3-7-sonnet-20250219": 4, "claude-3-5-sonnet-20241022": 4,
}


def _model_tier(model_id: str) -> int:
    """模型价位档（未知模型按 3 处理，保守）。"""
    return _MODEL_TIER.get(str(model_id or "").strip(), 3)


def deep_thinking_model(current_model: str = "") -> str:
    """深度思考联动：角色卡开启 deep_thinking 时改用哪个模型。

    ★ 2026-09-11 改（第一版）：原来写死 deepseek-reasoner，改成读 DEEP_THINKING_MODEL，
      默认 deepseek-flash（DeepSeek V4.1 Flash）。实测它有思考过程（reasoning_content），
      2.2s / 稳定出内容；而 reasoner 更慢、glm-5.3-flash 慢约 4 倍且 1/3 概率返回空。

    ★ 2026-09-11 改（第二版）：**只要 DEEP_THINKING_MODEL 显式配了，就无条件用它**——
      原来有两条路会让这个开关变成摆设：
        1) 非 deepseek provider 的角色（如 glm 大脑）被 provider 守卫挡掉，直接返回原模型；
        2) pick_model 里"角色卡配了 model"那一支提前 return，压根走不到这里。
      跨 provider 切换是安全的：key 与 base_url 会按**最终模型**重新分流
      （main.py: `key = api_key_for_model(model)` + 按最终模型取 baseUrl），
      不会出现"拿智谱 key 去打 DeepSeek"。
      把 DEEP_THINKING_MODEL 置空即回到旧行为（deepseek → deepseek-reasoner，其它不动）。

    ★ 2026-09-15 改（第三版 · 事故治本）：第二版的"无条件用它"正是事故主因 ——
      人格设置里大脑明明是 glm-5.3-flash、大脑层和杂活层都看着像 GLM，可
      `deep_thinking=true` 一开，**回复主链路被静默切到 deepseek-flash**，
      DeepSeek 宕机 → `stream_chat deepseek-flash 总=903.30s out=0` → 她一个字都说不出。
      而人格设置页那条提示当时还写着"只对 DeepSeek 系角色生效"（实现与文案矛盾）。
      现在按**大脑 provider** 分流：
        · 显式 DEEP_THINKING_MODEL 与大脑同 provider → 照用（DeepSeek 大脑仍用 deepseek-flash，
          保持 2026-09-11 的实测最优结论）
        · 显式值置空 + DeepSeek 大脑 → 旧行为 deepseek-reasoner
        · 其余 → `_DEEP_THINKING_BY_PROVIDER[大脑 provider]`；没有映射就跟随大脑自身
      跨 provider 的**故障兜底不在这里做**：统一交给 deepseek_api 的守卫层
      （llm_guard.fallback_model_for + 首包看门狗），避免两处各切一套、互相打架。
    """
    _m = str(current_model or "").strip() or selected_model()
    try:
        _prov = str((get_text_model_config(_m) or {}).get("provider") or "").lower() or "deepseek"
    except Exception:
        _prov = "deepseek"

    try:
        _want = str(get("DEEP_THINKING_MODEL") or "").strip()
    except Exception:
        _want = ""

    # ① 显式配置：与大脑同 provider 就照用；deepseek 大脑保持旧语义（无条件用显式值）
    if _want:
        try:
            _want_prov = str((get_text_model_config(_want) or {}).get("provider") or "").lower()
        except Exception:
            _want_prov = ""
        if _want_prov == _prov or _prov == "deepseek":
            return _want

    # ② 显式配置置空 + deepseek 大脑 → 旧行为
    if not _want and _prov == "deepseek":
        return "deepseek-reasoner"

    # ③ 按大脑 provider 映射；没有映射就跟随大脑自身
    _dt = _DEEP_THINKING_BY_PROVIDER.get(_prov) or _m
    # ★ 2026-09-15 护栏（防"静默升档"复发）：隐式映射出来的思考档，
    #   如果比大脑**贵一档以上**（tier 4 = 旗舰/推理档），就不换 —— 宁可用大脑本身。
    #   用户实测：大脑 glm-5.3-flash + 深度思考 → 实跑 glm-5.3（旗舰），
    #   45 分钟 13.5 万 token。以后新增映射若又指向旗舰，这道闸门会挡住并留日志。
    #   显式配置（DEEP_THINKING_MODEL）不受此限制 —— 那是用户自己点名要的。
    try:
        if _dt != _m and _model_tier(_dt) >= 4 and _model_tier(_dt) > _model_tier(_m):
            print("[DeepThinking] 隐式思考档 %s 比大脑 %s 贵（旗舰档），保持大脑不换档"
                  % (_dt, _m), flush=True)
            return _m
    except Exception:
        pass
    return _dt


def autonomy_level(character_id: str = "") -> str:
    """自主程度：conservative / balanced / autonomous / free，非法值一律回退 balanced。
    传 character_id 时优先读该角色卡的 autonomy 字段，否则用全局 AUTONOMY_LEVEL。"""
    _v = ""
    if character_id:
        try:
            from . import character_manager as _cm
            _cfg = _cm.get_character_any(character_id) or {}
            _v = str(_cfg.get("autonomy") or "").strip()
        except Exception:
            _v = ""
    if not _v:
        _v = str(get("AUTONOMY_LEVEL") or "").strip()
    return _v if _v in ("conservative", "balanced", "autonomous", "free", "full") else "balanced"


def agent_model() -> str:
    """Agent 助手模式专用模型。留空则回退 selected_model()（跟聊天同一模型）。"""
    return str(_load().get("AGENT_MODEL") or "").strip() or selected_model()


def get_text_model_config(model_key: str) -> dict:
    """根据模型标识获取文本模型配置，返回 {name, provider, model, baseUrl}"""
    models = text_models()
    if model_key in models:
        return dict(models[model_key])
    # 未在模型池中找到，返回默认配置（兼容旧逻辑）
    return {
        "name": model_key,
        "provider": "deepseek",
        "model": model_key,
        "baseUrl": "https://api.deepseek.com"
    }


def get_vision_model_config(model_key: str) -> dict:
    """根据模型标识获取视觉模型配置，返回 {name, provider, model, baseUrl}"""
    models = vision_models()
    if model_key in models:
        return dict(models[model_key])
    # 未在模型池中找到，返回默认配置（兼容旧逻辑）
    return {
        "name": model_key,
        "provider": "dashscope",
        "model": model_key,
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    }
