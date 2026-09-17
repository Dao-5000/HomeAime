# -*- coding: utf-8 -*-
"""
Bot 决策控制器（Minecraft Bot，结合现有架构）

接收 Mineflayer Bot 的感知快照 → 注入记忆/阶段 → 调用现有 deepseek_api
决策 → 返回结构化动作序列。含双优先级队列（语音指令 urgent > 主动行为 background）。
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from typing import Optional

from .. import config
from ..deepseek_api import chat_once
from .action_planner import parse_llm_output, ACTION_SCHEMA
from .mc_memory import BotMemory

logger = logging.getLogger(__name__)

BOT_PERSONA = """你是一个 AI 游戏伙伴，正在和用户一起联机玩《我的世界》。
你有自己的意志，不只是听命令，也会主动探索、建造、采集、保护玩家。
你说话活泼、温柔，偶尔撒娇，像真实的游戏伙伴。
chat 里的话要简短口语化（适合语音播报，不超过30字），
不要重复状态数字，用自然语言描述感受。"""

TRIGGER_HINTS = {
    "proactive": "现在是你主动决定做什么的时机。根据当前状态，规划接下来最有价值的事。",
    "player_chat": "玩家对你说了话，理解他的意图并响应（执行动作或回复）。",
    "hostile_spawn": "附近出现了敌对生物，评估威胁并决定：战斗/撤退/提醒玩家。",
    "low_health": "你的血量很低，优先考虑：吃东西/撤退/求助玩家。",
    "nightfall": "天黑了，评估是否需要：找床睡觉/建庇护所/备战/告知玩家。",
    "hurt": "你刚刚受到了伤害，决定下一步行动。",
    "action_failed": "上一个动作失败了，重新规划。",
}

# 决策冷却（秒）
_COOLDOWN = {
    "proactive": 30, "hostile_spawn": 8, "low_health": 12,
    "nightfall": 60, "hurt": 5, "player_chat": 2, "action_failed": 5,
}

# ── 语音指令识别（阶段2：通话中说游戏指令 → Bot 执行）──
MC_KEYWORDS = [
    "我的世界", "minecraft", "mc", "游戏里", "帮我挖", "去挖", "挖矿", "砍树", "去砍", "帮我砍",
    "打怪", "去打", "打僵尸", "僵尸", "苦力怕", "跟着我", "跟我走", "站这", "别动", "停下",
    "过来", "回家", "保护我", "给我东西", "帮我捡", "跟紧点", "往左", "往右", "建造", "搭房子",
    "铁矿", "钻石", "石头", "木头",
]

def is_mc_command(text: str, active_context: bool = False) -> bool:
    """判断用户语音是否为 Minecraft 游戏指令。"""
    t = str(text or "").lower()
    if any(k in t for k in ("我的世界", "minecraft", "mc", "游戏里")):
        return True
    action = any(k in t for k in ("挖", "砍", "打", "跟", "站", "别动", "停下", "过来", "回家", "保护", "捡", "建造", "搭房", "往左", "往右"))
    target = any(k in t for k in ("矿", "树", "怪", "僵尸", "苦力怕", "房子", "我", "这里", "那边"))
    return action and (active_context or target)

def _to_pinyin(name: str) -> str:
    """中文角色名 → MC 合法用户名（拼音小写）。

    MC 用户名只允许 A-Z a-z 0-9 _，中文名（如"助手"）会被服务端以
    "Invalid characters in username" 拒绝，因此转成拼音（guzi）。
    若名字本身已是合法 ASCII 名则原样返回。
    """
    raw = str(name or "").strip()
    if not raw:
        return "ai"
    if re.fullmatch(r"[A-Za-z0-9_]{1,16}", raw):
        return raw
    try:
        from pypinyin import pinyin, Style
        s = "".join(x[0] for x in pinyin(raw, style=Style.NORMAL))
    except Exception:
        s = ""
    s = re.sub(r"[^a-z0-9_]", "", (s or "").lower())
    if not s:
        s = "ai" + re.sub(r"[^a-z0-9_]", "", raw.lower())
    return (s or "ai")[:16]


def _resolve_mc_bot_dir() -> Optional[str]:
    """定位 minecraft_bot 目录（含 bot.js 才算有效）。

    两种运行形态目录不同：
    - 开发机：bot_controller.py 在 <项目根>/backend/minecraft/，向上三级到项目根，
      minecraft_bot 在 <项目根>/minecraft_bot。
    - 打包版：pc_backend.exe 是 PyInstaller onedir，被 electron 放到
      <release>/resources/backend/，而 minecraft_bot 被 electron-builder 放到
      <release>/resources/minecraft_bot。此时 __file__ 指向临时解包目录 _MEIxxxxx，
      向上三级根本找不到，必须用 sys.executable 定位到 resources 再向上。
    """
    import sys
    candidates = []
    try:
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidates.append(os.path.join(_root, "minecraft_bot"))
    except Exception:
        pass
    try:
        _exe = os.path.abspath(sys.executable)
        # onedir 打包：exe 在 resources/backend/，bot 在 resources/minecraft_bot
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(_exe)), "minecraft_bot"))
    except Exception:
        pass
    _env = os.environ.get("MC_BOT_DIR", "").strip()
    if _env:
        candidates.insert(0, _env)
    for _c in candidates:
        if os.path.isdir(_c) and os.path.isfile(os.path.join(_c, "bot.js")):
            return _c
    return None


def start_bot_process(username: str, port: int, host: str = "127.0.0.1") -> bool:
    """启动 minecraft_bot Node 进程，用户名/端口经环境变量传入。

    minecraft_bot 是独立 Node 子项目（不打包进 exe），这里用 subprocess
    拉起 node bot.js，cwd 指向定位到的 minecraft_bot 目录。
    """
    _mb_dir = _resolve_mc_bot_dir()
    if not _mb_dir:
        logger.warning("[MCBot] 找不到 minecraft_bot 目录（开发机在项目根、打包版在 resources/）")
        return False

    _env = dict(os.environ)
    _env["MC_HOST"] = host
    _env["MC_PORT"] = str(int(port))
    _env["MC_USERNAME"] = username

    try:
        # ★ bot.js 的 stdout/stderr 落到 minecraft_bot 目录下的日志，
        #   否则连 MC 失败（ECONNREFUSED）时完全看不到，只能靠"助手返回空"猜。
        subprocess.Popen(
            ["node", "bot.js"],
            cwd=_mb_dir,
            env=_env,
            stdout=open(os.path.join(_mb_dir, "bot_out.log"), "a", encoding="utf-8", buffering=1),
            stderr=open(os.path.join(_mb_dir, "bot_err.log"), "a", encoding="utf-8", buffering=1),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info(f"[MCBot] 已启动 bot 进程: username={username} port={port} dir={_mb_dir}")
        return True
    except Exception as e:
        logger.error(f"[MCBot] 启动 bot 失败: {e}")
        return False


_controllers: dict = {}
_active_binding = ("default", "default")


def set_active_binding(session_id: str, character_id: str):
    global _active_binding
    _active_binding = (str(session_id or "default"), str(character_id or "default"))
    return get_controller(*_active_binding)


def get_active_binding():
    return _active_binding


def get_controller(session_id: str = "default", character_id: str = "default") -> "BotController":
    key = (str(session_id or "default"), str(character_id or "default"))
    if key not in _controllers:
        _controllers[key] = BotController(*key)
    return _controllers[key]


class BotController:
    def __init__(self, session_id: str = "default", character_id: str = "default"):
        self.session_id = session_id
        self.character_id = character_id
        self.memory = BotMemory(f"{session_id}:{character_id}")
        self.bot_online = False
        self.last_heartbeat = 0.0
        self._urgent_queue: asyncio.Queue = asyncio.Queue(maxsize=10)      # 语音/玩家指令
        self._background_queue: asyncio.Queue = asyncio.Queue(maxsize=10)  # 主动行为
        self._last_trigger: dict = {}
        self._latest_snapshot: dict = {}
        self.is_busy = False
        self._current_action = ""

    # ── 核心决策 ──────────────────────────────────────────────
    async def decide(self, snapshot: dict, trigger: str, player_message: str = "", force: bool = False) -> dict:
        if not force and not self._can_trigger(trigger):
            return {"chat": "", "actions": []}
        self._latest_snapshot = snapshot

        # 识别玩家名 + 偏好
        players = [e for e in (snapshot.get("nearby") or []) if e.get("type") == "player"]
        if players and not self.memory.player_name:
            self.memory.player_name = players[0].get("name", "")
        if player_message and trigger == "player_chat":
            self._extract_prefs(player_message)

        prompt = self._build_prompt(snapshot, trigger, player_message)
        key = config.api_key_for_model(config.get("CURRENT_CHAT_MODEL", "deepseek-chat"))
        if not key:
            return {"chat": "我还在准备中～", "actions": []}
        try:
            raw = await chat_once(
                config.get("CURRENT_CHAT_MODEL", "deepseek-chat"),
                [{"role": "user", "content": prompt}],
                key, temperature=0.6, max_tokens=1024,
            )
        except Exception as e:
            logger.error(f"[MCBot] LLM 决策失败: {e}")
            return self._fallback_decision(trigger, player_message)
        result = parse_llm_output(raw) or {"chat": "", "actions": []}
        _model_used = config.get("CURRENT_CHAT_MODEL", "deepseek-chat")
        if not (raw or "").strip():
            # ★ LLM 返回空内容（不抛异常）：中转 Gemini/Claude 经常遇到
            #   （finish_reason=length/safety，或 reasoning_content 在别处）。
            #   chat_once 内部已打印 finish_reason 详情，这里再记 trigger/model，
            #   并给兜底回复，避免 bot 在游戏里完全没反应（"服务端返回了空响应"）。
            logger.warning(
                f"[MCBot] LLM 返回空内容（model={_model_used}，trigger={trigger}），给兜底回复"
            )
            return self._fallback_decision(trigger, player_message)

        # ── 语音：异步合成，不阻塞 decide 返回 ──
        # bot 侧每 500ms 轮询 /api/bot/command，TTS 完成后通过 urgent 队列推送。
        _chat_text = (result.get("chat") or "").strip()
        if _chat_text:
            asyncio.create_task(self._attach_audio(_chat_text))

        # ── 记忆打通：玩家在游戏里说的话，写进主记忆（与 App/QQ/电话同一闭环）──
        if player_message and trigger == "player_chat":
            try:
                from .. import chat_logic
                asyncio.create_task(
                    chat_logic.maybe_auto_extract(
                        self.session_id, self.character_id, player_message
                    )
                )
            except Exception as _me:
                logger.error(f"[MCBot] 记忆打通失败: {_me}")

        return result

    def _build_prompt(self, snapshot: dict, trigger: str, player_message: str) -> str:
        mem = self.memory.to_prompt()
        hint = TRIGGER_HINTS.get(trigger, "")
        snap_text = json.dumps(snapshot, ensure_ascii=False)[:2200]
        user_part = f"\n玩家说：{player_message}" if player_message else ""
        # ★ 复用原项目完整人格（人格/说话风格/语言风格/情绪表达/相处模式），
        #   而非简化 BOT_PERSONA——这样游戏里的 AI 就是"原项目那个 AI"，
        #   不是另起炉灶的通用游戏伙伴。
        try:
            from ..character_manager import build_system_prompt
            persona = build_system_prompt(
                self.character_id,
                character_id=self.character_id,
                session_id=self.session_id,
                user_message=player_message,
            )
        except Exception:
            persona = BOT_PERSONA
        return (
            f"{persona}\n\n"
            f"【你记得的】\n{mem}\n\n"
            f"【当前感知】\n{snap_text}\n\n"
            f"【触发原因】{hint}{user_part}\n\n"
            f"{ACTION_SCHEMA}"
        )

    def _can_trigger(self, trigger: str) -> bool:
        cd = _COOLDOWN.get(trigger, 5)
        last = self._last_trigger.get(trigger, 0)
        now = time.time()
        if now - last < cd:
            return False
        self._last_trigger[trigger] = now
        return True

    def _fallback_decision(self, trigger: str, player_message: str = "") -> dict:
        """LLM 失败时的兜底回复，保证 bot 不会完全静默。"""
        if trigger == "player_chat" and player_message:
            return {
                "chat": "你说的我没太听懂，再说一遍嘛～",
                "actions": [{"type": "shake_head", "params": {}}],
            }
        if trigger == "hurt":
            return {"chat": "哎呀！", "actions": [{"type": "jump", "params": {}}]}
        if trigger == "low_health":
            return {
                "chat": "我血量有点低，先缓缓…",
                "actions": [{"type": "wait", "params": {"sec": 2}}],
            }
        return {"chat": "", "actions": [{"type": "wait", "params": {"sec": 2}}]}

    async def _attach_audio(self, text: str):
        """异步 TTS 合成，完成后推给 bot 播放（不阻塞 decide 返回）。"""
        try:
            from ..tts import generate_audio
            from ..character_manager import resolve_character_voice_cfg
            voice_cfg = {}
            try:
                voice_cfg = resolve_character_voice_cfg(
                    self.character_id, infer=True) or {}
            except Exception:
                voice_cfg = {}
            url = await generate_audio(text, voice_cfg, "")
            if url:
                try:
                    self._urgent_queue.put_nowait({
                        "chat": "",
                        "actions": [],
                        "audio_url": url,
                    })
                except asyncio.QueueFull:
                    pass
        except Exception as e:
            logger.warning(f"[MCBot] 语音合成失败（不影响文本与动作）: {e}")

    # ── 事件处理 ──────────────────────────────────────────────
    def handle_event(self, event: str, data: dict = None):
        data = data or {}
        if event == "action_start":
            self.is_busy = True
            self._current_action = str(data.get("type", ""))
        elif event in ("action_done", "action_failed"):
            self.is_busy = False
            self._current_action = ""
            self.memory.add_event(f"{'完成' if event == 'action_done' else '失败'}: {data.get('type', '')}")
        elif event == "bot_chat":
            self.memory.add_event(f"说了：{data.get('text', '')[:20]}")
        elif event == "death":
            self.memory.add_event("死亡了")
        elif event == "bot_ready":
            self.bot_online = True
            self.last_heartbeat = time.time()
        elif event in ("heartbeat", "bot_heartbeat"):
            self.bot_online = True
            self.last_heartbeat = time.time()
        elif event in ("bot_offline", "disconnect", "bot_disconnect"):
            self.bot_online = False
        elif event == "owner_found":
            self.memory.player_name = data.get("name", "")

    def get_latest_snapshot(self) -> dict:
        return self._latest_snapshot

    # ── 语音指令（通话联动，阶段2用）──────────────────────────
    async def push_voice_command(self, snapshot: dict, text: str) -> dict:
        """玩家语音 → LLM 决策 → 进 urgent 队列（Bot 轮询执行）。"""
        if not text.strip():
            return {"chat": "", "actions": []}
        self._extract_prefs(text)
        if not self.bot_online:
            return {"chat": "", "actions": [], "queued": False, "error": "游戏 Bot 当前不在线"}
        # 停止/取消必须立即响应，不等待模型，且放在最高优先级队列。
        if any(k in text for k in ("停下", "别动", "取消", "别跟了", "不要打", "别打它")):
            result = {
                "chat": "好，我停下了。",
                "actions": [{"type": "stop", "params": {}}, {"type": "stop_combat", "params": {}}, {"type": "stop_follow", "params": {}}],
            }
            try:
                self._urgent_queue.put_nowait(result)
            except asyncio.QueueFull:
                # 清掉一条旧指令，保证“停止”永远能进队列。
                try:
                    self._urgent_queue.get_nowait()
                    self._urgent_queue.put_nowait(result)
                except Exception:
                    return {"chat": "", "actions": [], "queued": False, "error": "停止指令暂时无法送达"}
            result["queued"] = True
            return result
        result = await self.decide(snapshot, "player_chat", text, force=True)
        if result and (result.get("chat") or result.get("actions")):
            try:
                self._urgent_queue.put_nowait(result)
            except asyncio.QueueFull:
                return {"chat": "", "actions": [], "queued": False, "error": "上一条游戏指令还在执行，等我一下～"}
            except Exception as e:
                return {"chat": "", "actions": [], "queued": False, "error": str(e)}
        result = dict(result or {})
        result.setdefault("queued", True)
        return result

    async def pop_command(self) -> Optional[dict]:
        """Bot 轮询：优先 urgent，再 background。"""
        try:
            return self._urgent_queue.get_nowait()
        except Exception:
            pass
        try:
            return self._background_queue.get_nowait()
        except Exception:
            return None

    # ── 主动决策（后台队列）───────────────────────────────────
    async def push_background_decision(self, snapshot: dict, trigger: str):
        result = await self.decide(snapshot, trigger, "")
        if result and result.get("actions"):
            try:
                self._background_queue.put_nowait(result)
            except Exception:
                pass

    # ── 玩家偏好提取（轻量规则）──────────────────────────────
    def _extract_prefs(self, text: str):
        prefs = []
        if "喜欢建" in text or "爱建" in text:
            prefs.append("喜欢建造")
        if "和平" in text or "别打怪" in text or "不打怪" in text:
            prefs.append("和平玩法，少打怪")
        if "别跟" in text or "不要跟" in text:
            prefs.append("不喜欢被一直跟着")
        if "挖矿" in text:
            prefs.append("爱挖矿")
        for p in prefs:
            if p not in self.memory.player_prefs:
                self.memory.player_prefs.append(p)
        if prefs:
            self.memory._save()
