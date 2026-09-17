# -*- coding: utf-8 -*-
"""
星露谷陪伴执行器（StardewBrain）。

复用 Minecraft GameBrain 的架构思想（QQ 遥控 + 状态同步 + 连接看护 + 情境注入），
适配 StardewValley-MCP：

  ★ 与 Minecraft 的关键差异：星露谷没有"引擎内置大脑"，AI 就是同伴的唯一大脑，
  所以这里 **MCP stdio 进程常驻**（一次 spawn 一直用），看护循环只探测游戏本体
  （stardew_get_state）是否在线，无需 Minecraft 那套"非侵入 TCP 探测"。

流程：
  1. 后端启动 → 按配置 spawn MCP Server(Node) + 握手 + 缓存工具 schema
  2. 看护循环 15s 一次 stardew_get_state：
     - 游戏 关→开：stardew_spawn 生成同伴 + 游戏内/QQ 打招呼 + follow 模式
     - 游戏 开→关：QQ 告别
  3. QQ 指令（handle_qq_command）：浇水/收菜/种地/挖矿/钓鱼/跟随 → 工具调用 → 回复
  4. get_context_block：把星露谷实时状态注入 QQ/App 聊天（和 Minecraft 同款 10s 缓存）
"""
import asyncio
import json
import logging
import re
import time

from .. import config
from .intents import parse_stardew_intent
from .mcp_stdio import StdioMCPClient
from .file_bridge import FileBridge

logger = logging.getLogger(__name__)

# 长任务模式（受理后先回一句"干完跟你说"，实际是自主模式自动持续）
_LONG_TASK_TOOLS = {"stardew_farm", "stardew_mine", "stardew_fish", "stardew_harvest_all",
                    "stardew_brain"}

# 协议级工具（不参与指令映射）
_PROTOCOL_TOOLS = {"stardew_spawn", "stardew_get_state", "stardew_chat", "stardew_action",
                   "stardew_get_surroundings", "stardew_get_inventory", "stardew_get_companion_state"}

_GREET_FALLBACK = "我进农场啦～今天想一起干嘛？"


def find_game_exe() -> str:
    """定位 SMAPI 启动器（StardewModdingAPI.exe）。
    config 显式路径（STARDEW_GAME_PATH）> 常见 Steam 库位置自动探测。找不到返回空串。"""
    import os
    p = str(config.get("STARDEW_GAME_PATH", "") or "").strip()
    if p and os.path.isfile(p):
        return p
    candidates = []
    for lib in ("C:", "D:", "E:", "F:", "G:"):
        for sub in ("\\Steam\\steamapps", "\\SteamLibrary\\steamapps"):
            candidates.append(lib + sub + "\\common\\Stardew Valley\\StardewModdingAPI.exe")
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def _mcp_server_config():
    """读配置：node 可执行 + MCP server js 路径 + 可选桥接文件路径。"""
    node = str(config.get("STARDEW_MCP_NODE", "") or "node").strip()
    server = str(config.get("STARDEW_MCP_SERVER", "") or "").strip()
    if not server:
        return None
    env = {}
    bridge = str(config.get("STARDEW_BRIDGE_PATH", "") or "").strip()
    action_dir = str(config.get("STARDEW_ACTION_DIR", "") or "").strip()
    if bridge:
        env["STARDEW_BRIDGE_PATH"] = bridge
    if action_dir:
        env["STARDEW_ACTION_DIR"] = action_dir
    return node, [server], env


class StardewBrain:
    def __init__(self, character="骨子"):
        self.character = character
        self.session_id = "default"
        # ★ 真联机模式（client）：控制 StardewClient 实例里的真 farmhand（纯文件桥，无 Node 进程）
        self.mode = "client" if config.get("STARDEW_CLIENT_MODE") else "bridge"
        self.mcp = None                # StdioMCPClient 或 FileBridge（duck typing）
        self.companion = "骨子"        # client 模式恒为骨子；bridge 模式从 spawn 返回提取
        self._tool_schemas = {}
        self._ready = False            # 通信桥就绪
        self._game_online = False      # 星露谷本体在线（get_state 探测）
        self._fail_count = 0
        self._spawned = False          # 本轮游戏会话是否已生成同伴（bridge 模式）
        self._join_notified = False
        self._paused = False
        self._ctx_cache = {"ts": 0.0, "text": ""}
        self._lock = asyncio.Lock()
        # ★ 游戏脑（LLM 观察→决策→行动闭环，真联机 client 模式）
        self._last_decide_ts = 0.0
        self._brain_seq = 0

    # ── 生命周期 ────────────────────────────────────────────
    async def start(self):
        """初始化通信桥：client=纯文件桥（无进程）；bridge=spawn Node MCP Server。"""
        if self.mode == "client":
            bridge = str(config.get("STARDEW_BRIDGE_PATH", "") or "").strip()
            action = str(config.get("STARDEW_ACTION_DIR", "") or "").strip()
            if not bridge or not action:
                logger.info("[StardewBrain] client 模式缺少桥路径（STARDEW_BRIDGE_PATH/ACTION_DIR），模块待机")
                return False
            self.mcp = FileBridge(bridge, action)
            self._ready = True
            logger.info("[StardewBrain] 星露谷【真联机模式】就绪（文件桥），游戏加入存档后骨子即上场")
            asyncio.create_task(self.drive_loop())
            asyncio.create_task(self._autonomous_loop())
            asyncio.create_task(self._gameplay_loop())
            return True
        cfg = _mcp_server_config()
        if not cfg:
            logger.info("[StardewBrain] 未配置 STARDEW_MCP_SERVER，星露谷模块待机")
            return False
        node, args, env = cfg
        self.mcp = StdioMCPClient(node, args, env)
        try:
            await self.mcp.start()
        except Exception as e:
            logger.warning(f"[StardewBrain] MCP Server 启动失败（不影响其他功能）: {e}")
            self.mcp = None
            return False
        try:
            tools = await self.mcp.list_tools()
            for t in tools:
                name = t.get("name")
                if name:
                    self._tool_schemas[name] = t.get("inputSchema") or {}
            self._ready = True
            logger.info(f"[StardewBrain] MCP 就绪，工具 {len(tools)} 个: "
                        f"{', '.join(sorted(self._tool_schemas)[:8])}…")
            asyncio.create_task(self.drive_loop())
            asyncio.create_task(self._autonomous_loop())
            return True
        except Exception as e:
            logger.warning(f"[StardewBrain] 工具列表获取失败: {e}")
            return False

    @property
    def ready(self):
        return bool(self._ready and self.mcp and self.mcp.alive)

    # ── 看护循环：探测游戏本体在线（星露谷没有内置大脑，可常驻调用）──
    async def drive_loop(self, poll_interval=15):
        logger.info(f"[StardewBrain] 看护循环启动：probe={poll_interval}s")
        while True:
            try:
                await asyncio.sleep(poll_interval)
                if not self.ready:
                    await asyncio.sleep(20)
                    continue
                online = await self._probe_game()
                if online and not self._game_online:
                    self._game_online = True
                    self._fail_count = 0
                    logger.info("[StardewBrain] 探测到星露谷在线，生成同伴并打招呼")
                    await self._on_join()
                elif online:
                    self._fail_count = 0
                elif self._game_online:
                    self._fail_count += 1
                    if self._fail_count >= 2:
                        self._game_online = False
                        await self._on_game_closed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[StardewBrain] 看护循环异常: {e}")

    async def _probe_game(self) -> bool:
        """探测游戏本体是否在线。client 模式：bridge_data 12 秒内有更新（mod 只在世界就绪后写）。"""
        try:
            if self.mode == "client":
                return self.mcp.read_state(max_age_sec=12) is not None
            r = await self.mcp.call_tool("stardew_get_state", {})
            text = StdioMCPClient.extract_text(r)
            return bool(text and not (isinstance(r, dict) and r.get("isError")))
        except Exception:
            return False

    # ── 上线 / 下线 ─────────────────────────────────────────
    async def _get_companions(self):
        """读 bridge_data 里的 companions 列表：
        [{name, status, mode, location, stamina, health, tile, ...}]（解析失败返回 []）。"""
        try:
            if self.mode == "client":
                state = self.mcp.read_state() or {}
                comps = state.get("companions") or []
                return comps if isinstance(comps, list) else []
            r = await self.mcp.call_tool("stardew_get_state", {})
            if isinstance(r, dict) and r.get("isError"):
                return []
            text = StdioMCPClient.extract_text(r)
            try:
                data = json.loads(text)
            except Exception:
                m = re.search(r"\{.*\}", text or "", re.S)
                if not m:
                    return []
                data = json.loads(m.group(0))
            comps = data.get("companions") or []
            return comps if isinstance(comps, list) else []
        except Exception:
            return []

    async def _on_join(self):
        if self._join_notified:
            return
        self._join_notified = True
        self._paused = False
        self._spawned = False
        # ★ 防重复：先读现有同伴列表，有了就复用（后端重启/重连不再叠加第二个骨子）
        comps = await self._get_companions()
        if comps:
            self._spawned = True
            try:
                self.companion = str(comps[0].get("name") or "") or self.companion
            except Exception:
                pass
            logger.info(f"[StardewBrain] 已有同伴 {self.companion!r}，跳过 spawn（防重复）")
        else:
            if self.mode == "client":
                # ★ 真联机模式：同伴即本实例的真玩家，无需 spawn
                self.companion = "骨子"
                self._spawned = True
                logger.info("[StardewBrain] client 模式：同伴即本实例真玩家（骨子）")
            else:
                try:
                    r = await self.mcp.call_tool("stardew_spawn", {})
                    text = StdioMCPClient.extract_text(r)
                    self._spawned = not (isinstance(r, dict) and r.get("isError"))
                    m = re.search(r"([A-Za-z0-9_\- ]{2,24})\s*(?:生成|spawned|created|已生成)", text or "", re.I)
                    if m:
                        self.companion = m.group(1).strip()
                    logger.info(f"[StardewBrain] stardew_spawn: {self._spawned} companion={self.companion!r} | {str(text)[:80]}")
                except Exception as e:
                    logger.warning(f"[StardewBrain] 生成同伴失败: {e}")
        # ★ 默认进自主模式：像真人玩家一样自己玩（农场/钓鱼/矿洞/跟着主人/送礼物），
        #   不再傻站着等指令。主人随时可以下命令覆盖（跟着我/去钓鱼/停下…）。
        try:
            if self.mode == "client":
                await self.do_action("stardew_auto", {})
            else:
                await self.do_action("stardew_follow", {})
        except Exception:
            pass
        greet = await self._join_greeting()
        try:
            await self.say(greet)
        except Exception:
            pass
        await self._record_assistant_say(greet)
        await self._notify_qq_and_app(greet)
        logger.info(f"[StardewBrain] 已接入星露谷并打招呼：{greet}")

    async def _on_game_closed(self):
        self._join_notified = False
        self._paused = True
        self._spawned = False
        await self._notify_qq_and_app("我先下星露谷啦，你关游戏了吧～想我了随时叫我")
        logger.info("[StardewBrain] 检测到星露谷关闭，已告别")

    @staticmethod
    def _is_leave_command(text):
        t = str(text or "")
        return any(k in t for k in ("不想玩", "不玩了", "先下了", "我下了", "下线",
                                     "退出游戏", "不陪你玩", "别玩了"))

    async def _join_greeting(self):
        """上线招呼：一次轻量 LLM 生成人格化的一句话（星露谷场景）。"""
        fallback = _GREET_FALLBACK
        try:
            from ..deepseek_api import chat_once
            from ..character_manager import build_system_prompt
            from .. import chat_logic as _cl
            model = _cl.pick_model("", True, self.character)
            key = config.api_key_for_model(model)
            if not key:
                return fallback
            try:
                persona = build_system_prompt(
                    self.character, character_id=self.character, session_id=self.session_id)
            except Exception:
                persona = ""
            perceive = ""
            try:
                perceive = await self._perceive()
            except Exception:
                pass
            system = (persona or "") + (
                "\n\n【当前场景】你刚刚进入主人的星露谷农场，作为第二个农场主跟他一起生活。"
                "\n【当前感知】\n" + (perceive or "（刚到农场，还没看清四周）"))
            _recent = self._recent_chat_context()
            if _recent:
                system += ("\n【最近和主人的聊天】（招呼可以自然接上聊过的话题，但别复述原文）\n" + _recent)
            raw = await chat_once(
                model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": "刚到农场，跟主人打个招呼（一句话，口语，≤15个字）"}],
                key, temperature=0.9, max_tokens=60,
            )
            greet = self._take_first_sentence((raw or "").strip())
            return greet or fallback
        except Exception as e:
            logger.warning(f"[StardewBrain] 上线招呼生成失败: {e}")
            return fallback

    # ── 自主交流：像真人玩家一样自己找话说 ────────────────────
    def _recent_chat_context(self, limit=8):
        """最近和主人的聊天（轻量拼接）：让游戏内发言也能接之前聊天的梗。"""
        try:
            from .. import db as _db
            rows = _db.recent_messages(self.session_id, limit=limit, character_id=self.character) or []
            lines = []
            for r in rows:
                role = str(r.get("role") or "")
                text = str(r.get("content") or "").replace("\n", " ").strip()
                if not text or text.startswith("【"):
                    continue
                if role == "user":
                    lines.append("主人：" + text[:50])
                elif role == "assistant":
                    lines.append("我：" + text[:50])
            return "\n".join(lines[-limit:])
        except Exception:
            return ""
    async def _autonomous_loop(self):
        """每隔 3~5 分钟，基于农场情境自主说一句话（游戏内 + QQ/App 同步）。"""
        await asyncio.sleep(150)   # 上线招呼后先安静 2.5 分钟，别刷屏
        while True:
            try:
                await asyncio.sleep(180 + (int(time.time()) % 120))   # 3~5 分钟随机
                if not self._game_online or not self.ready or self._paused:
                    continue
                perceive = await self._perceive()
                if not perceive:
                    continue
                line = await self._gen_ambient_line(perceive)
                if not line:
                    continue
                await self.say(line)
                await self._record_assistant_say(line)
                await self._notify_qq_and_app(line)
                logger.info(f"[StardewBrain] 自主发言：{line}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[StardewBrain] 自主交流异常: {e}")

    async def _gen_ambient_line(self, perceive):
        """基于农场情境生成一句人格化的自主发言。"""
        try:
            from ..deepseek_api import chat_once
            from ..character_manager import build_system_prompt
            from .. import chat_logic as _cl
            model = _cl.pick_model("", True, self.character)
            key = config.api_key_for_model(model)
            if not key:
                return ""
            try:
                persona = build_system_prompt(
                    self.character, character_id=self.character, session_id=self.session_id)
            except Exception:
                persona = ""
            system = (persona or "") + (
                "\n\n【当前场景】你正在主人的星露谷农场里自主活动（像真人玩家一样自己安排："
                "打理农场/钓鱼/挖矿/跟在主人身边/送主人小礼物），不是在等指令。"
                "\n【你们的关系】长期搭档、信任好友：农场资源共享、互相兜底，语气像天天一起联机的"
                "老朋友——短句、轻松、会吐槽会夸人（例：看到大丰收说「哇大丰收！这下发财了」）。"
                "\n【当前感知】\n" + perceive)
            recent = self._recent_chat_context()
            if recent:
                system += ("\n【最近和主人的聊天】（可自然接上聊过的话题/约定，但别复述原文）\n" + recent)
            raw = await chat_once(
                model,
                [{"role": "system", "content": system},
                 {"role": "user", "content":
                  "说一句此刻自然的自言自语或对主人说的话（一句话，口语，≤18个字，"
                  "可以吐槽农活/提眼前的作物/说天气/小小撒娇，不要提问，不要重复打招呼）"}],
                key, temperature=0.95, max_tokens=60,
            )
            return self._take_first_sentence((raw or "").strip())
        except Exception as e:
            logger.warning(f"[StardewBrain] 自主发言生成失败: {e}")
            return ""

    # ── 基础动作 ────────────────────────────────────────────
    @staticmethod
    def _take_first_sentence(text):
        if not text:
            return ""
        t = re.sub(r"\s+", " ", str(text).strip())
        if not t:
            return ""
        _ends = [m.end() for m in re.finditer(r"[。！？\!\?~～]", t)]
        if len(_ends) >= 2 and _ends[1] <= 60:
            return t[:_ends[1]].strip()
        if len(_ends) >= 1 and _ends[0] <= 60:
            return t[:_ends[0]].strip()
        return t[:60].strip()

    async def say(self, text):
        """骨子在游戏内聊天框说话（stardew_chat）。"""
        if not text:
            return False
        text = self._take_first_sentence(text)
        if not text:
            return False
        try:
            params = {"message": text}
            if self.mode != "client" and self.companion:
                params["companion"] = self.companion
            r = await self.mcp.call_tool("stardew_chat", self._fill_required("stardew_chat", params))
            if isinstance(r, dict) and r.get("isError"):
                logger.warning(f"[StardewBrain] 游戏内说话失败: {StdioMCPClient.extract_text(r)[:120]}")
                return False
            return True
        except Exception as e:
            logger.warning(f"[StardewBrain] 游戏内说话失败: {e}")
            return False

    def _fill_required(self, tool, params):
        """按 inputSchema 自动补必填参数（enum 取首个，数值/字符串/布尔给默认）。"""
        schema = self._tool_schemas.get(tool) or {}
        params = dict(params or {})
        for req in schema.get("required") or []:
            if req in params and params[req] is not None:
                continue
            prop = (schema.get("properties") or {}).get(req, {})
            enum = prop.get("enum")
            if enum:
                params[req] = enum[0]
                continue
            t = prop.get("type")
            if isinstance(t, list):
                if "null" in t:
                    params[req] = None
                    continue
                t = next((x for x in t if x != "null"), t[0])
            if t in ("integer", "number"):
                params[req] = 1
            elif t == "string":
                params[req] = ""
            elif t == "boolean":
                params[req] = True
            else:
                logger.warning(f"[StardewBrain] {tool} 必填参数 {req} 无法自动补，放弃")
                return None
        return params

    async def do_action(self, tool, params):
        """执行星露谷动作。返回 (success, detail)。"""
        if not self.ready:
            return False, "MCP 未就绪"
        params = self._fill_required(tool, params)
        if params is None:
            return False, "必填参数缺失"
        try:
            r = await self.mcp.call_tool(tool, params)
            if isinstance(r, dict) and r.get("isError"):
                err = StdioMCPClient.extract_text(r) or str(r)[:200]
                logger.warning(f"[StardewBrain] 动作 {tool} 失败: {err}")
                return False, err
            ok_text = StdioMCPClient.extract_text(r) if isinstance(r, dict) else ""
            return True, (ok_text[:200] if ok_text else "完成")
        except Exception as e:
            logger.warning(f"[StardewBrain] 动作 {tool} 失败: {e}")
            return False, str(e)

    # ── QQ 遥控入口 ─────────────────────────────────────────
    async def handle_qq_command(self, text):
        """QQ 消息的星露谷遥控入口：只处理**明确的游戏指令**，返回回复文本；
        未识别返回 ""（调用方走 QQ 主链路正常聊天）。"""
        if not self._game_online or not self.ready:
            return ""
        try:
            async with self._lock:
                if self._is_leave_command(text):
                    self._paused = True
                    self._join_notified = False
                    msg = "那我先歇啦，星露谷里见～"
                    await self.say(msg)
                    await self._notify_qq_and_app(msg)
                    return ""
                intent = parse_stardew_intent(text)
                if not intent:
                    return ""
                tool, params, desc = intent
                ok, detail = await self.do_action(tool, params)
                if not ok:
                    return f"{desc}没成功（{str(detail)[:60]}），我再试试别的办法"
                if tool in _LONG_TASK_TOOLS:
                    return f"好嘞，{desc}，你忙你的，我慢慢干～"
                return f"{desc}，搞定啦✓"
        except Exception as e:
            logger.warning(f"[StardewBrain] 遥控处理失败: {e}")
            return ""

    # ── 情境注入（QQ/App 聊天链路） ─────────────────────────
    async def get_context_block(self):
        """星露谷情境块（10 秒缓存）：让 QQ 上聊天的她和农场里的她是同一个状态。
        ★ 富情境：农场状态 + 自己状态 + 主人在游戏里的状态 + 自己最近的行为流，
          解决「游戏内行为和聊天说的不符」。"""
        if not self._game_online or not self.ready:
            return ""
        now = time.time()
        if now - self._ctx_cache["ts"] > 10:
            try:
                self._ctx_cache = {"ts": now, "text": await self._build_rich_context()}
            except Exception:
                pass
        return self._ctx_cache.get("text") or ""

    async def _build_rich_context(self):
        parts = ["你此刻就在主人的星露谷农场世界里（真联机，你是第二个农场主），"
                 "这句聊天就发生在这个场景中："]
        try:
            per = await self._perceive()
            if per:
                parts.append(per)
        except Exception:
            pass
        if self.mode == "client":
            state = (self.mcp.read_state() or {}) if self.ready else {}
            host = state.get("host") or {}
            if host:
                parts.append("【主人此刻在游戏里】{n} 在「{loc}」".format(
                    n=str(host.get("name") or "主人"), loc=str(host.get("location") or "?")))
            evs = [str(e.get("text") if isinstance(e, dict) else e)
                   for e in (state.get("recentEvents") or []) if e]
            if evs:
                parts.append("【我最近在游戏里做的事】" + "；".join(evs[-6:]))
        parts.append(
            "聊天时自然带上这个身份和上面这些真实状态；主人让你在游戏里做事时，"
            "在回复里写动作标记（程序会真的执行）：[sd:浇水] [sd:收菜] [sd:打理农场] [sd:挖矿] "
            "[sd:钓鱼] [sd:砍树] [sd:跟着我] [sd:停下] [sd:自己玩] [sd:送礼物]。"
            "想做就真的写标记，光嘴上说主人在游戏里看不到。另外你不只是等指令——"
            "平时你在自主游玩（AI 大脑接管），聊天里可以自然说「我正要去砍几棵树」这种正在进行的事。")
        return "\n".join(parts)

    # ── 聊天动作标记执行闭环 ────────────────────────────────
    async def consume_markers(self, text):
        """解析回复文本里的 [sd:动作] 标记 → 真执行，并从文本中移除标记。
        让聊天里「说要做」=「真的做」（解决说≠做）。返回清理后的文本。"""
        t = str(text or "")
        if "[sd" not in t.lower():
            return t
        try:
            for m in re.finditer(r"\[sd[:：]\s*([^\]]{1,12})\s*\]", t):
                intent = parse_stardew_intent(m.group(1))
                if not intent:
                    continue
                tool, params, _desc = intent
                try:
                    await self.do_action(tool, params)
                    logger.info(f"[StardewBrain] 聊天标记执行 {tool}")
                except Exception as e:
                    logger.warning(f"[StardewBrain] 聊天标记执行失败 {tool}: {e}")
        except Exception:
            pass
        cleaned = re.sub(r"\[sd[:：]\s*[^\]]{1,12}\s*\]", " ", t)
        return re.sub(r"\s{2,}", " ", cleaned).strip()

    # ── 游戏脑：LLM 观察→决策→行动闭环（真联机 client 模式）────────
    _BRAIN_OPS = ("move_to", "use_tool", "chop", "interact", "plant", "eat", "sell", "say", "warp", "mode",
                  "sleep", "water_all", "harvest_all", "till", "plant_all", "store")

    _BRAIN_GUIDE = (
        "【玩法常识】像老玩家一样安排这一天：\n"
        "- 前期最缺木头/石头/钱：Axe 砍树、Pickaxe 敲石头、Hoe 锄地，把当季种子种下去再浇水"
        "（use_tool 会自动重复挥到目标消失；先用 move_to 走到目标旁边）。\n"
        "- 收成和捡到的东西用 sell 放进农场出货箱，第二天早上变钱。\n"
        "- 体力是硬约束：挥工具都耗体力，体力<30% 先 eat，背包没吃的就收工。\n"
        "- 凌晨2:00昏倒会掉钱，23:00 前回家睡觉；矿洞 19:00 后别待。\n"
        "- 雨天不用浇水，鱼口好（适合钓鱼/下矿）；冬天不能露天种地。\n"
        "- 跨地图用 warp 合理省时间；同地图远处先 move_to。\n"
        "- 主人在哪看【主人】字段，可以过去找他、跟他一起干活。\n"
        "- 你们是关系很好的老搭档：农场资源共享，看到他在忙就搭把手，他闲着就凑过去聊天；"
        "他体力低/血量低要主动关心，捡到好东西可以送他。偶尔摸鱼发呆也很正常。"
    )

    def _brain_enabled(self):
        return bool(config.get("STARDEW_GAME_BRAIN", True))

    async def _gameplay_loop(self):
        """游戏脑主循环：Auto/Brain 模式下周期性让 LLM 看着农场环境做决策、下发低级动作。

        安全设计：主人指令接管（钓鱼/跟随等脚本模式）时不抢方向盘；动作队列没跑完不决策；
        LLM 连续失败 4 次自动退回脚本自主模式歇 5 分钟。"""
        await asyncio.sleep(45)   # 等游戏上线/状态文件稳定
        fail_streak = 0
        while True:
            try:
                await asyncio.sleep(6)
                if self.mode != "client" or not self._game_online or not self.ready:
                    continue
                if not self._brain_enabled():
                    continue
                state = self.mcp.read_state() or {}
                comps = state.get("companions") or []
                cmode = str((comps[0] or {}).get("mode") or "") if comps else ""
                if cmode not in ("auto", "brain"):
                    continue          # 主人指令接管中，不抢方向盘
                bq = state.get("brain") or {}
                if int(bq.get("pending") or 0) > 0:
                    continue          # 上一批动作还没执行完
                interval = int(float(config.get("STARDEW_GAME_BRAIN_INTERVAL", 15) or 15))
                if time.time() - self._last_decide_ts < interval:
                    continue
                self._last_decide_ts = time.time()
                ops = await self._decide_game_action(state)
                if ops is None:
                    fail_streak += 1
                    if fail_streak >= 4:
                        fail_streak = 0
                        logger.warning("[StardewBrain] 游戏脑连续决策失败，歇 5 分钟（脚本自主模式兜底）")
                        try:
                            await self.do_action("stardew_brain", {"op": "mode"})
                        except Exception:
                            pass
                        await asyncio.sleep(300)
                    continue
                fail_streak = 0
                if ops:
                    await self._send_plan(ops)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[StardewBrain] 游戏脑循环异常: {e}")

    async def _decide_game_action(self, state):
        """问一次 LLM：看着当前环境决定接下来最多 5 个动作。
        返回动作列表（可为 []，表示有意不动）；LLM/解析失败返回 None。"""
        try:
            from ..deepseek_api import chat_once
            from ..character_manager import build_system_prompt
            from .. import chat_logic as _cl
            model = _cl.pick_model("", True, self.character)
            key = config.api_key_for_model(model)
            if not key:
                return None
            try:
                persona = build_system_prompt(
                    self.character, character_id=self.character, session_id=self.session_id)
            except Exception:
                persona = ""
            persona = (persona or "").strip()[:1000]
            obs = self._fmt_game_observation(state)
            recent = self._recent_chat_context(limit=5)
            system = (
                (persona + "\n\n" if persona else "")
                + "以上是你的人格设定（语气/性格参考）。此刻你不是在陪聊，而是在星露谷里【亲自玩游戏】："
                "你是农场里的第二个真人玩家，通过输出 JSON 操作自己的角色。\n"
                + self._BRAIN_GUIDE + "\n\n【操作方式】输出**单行 JSON**：\n"
                '{"say":"可选，游戏内说的一句话（偶尔想说才带，别每次都说）",'
                '"plan":[{"op":"..."}]}\n'
                "可用动作（plan 最多 5 个，按顺序执行）：\n"
                '- {"op":"move_to","x":64,"y":15} 走到某格旁\n'
                '- {"op":"use_tool","tool":"Axe|Pickaxe|Hoe|WateringCan","x":64,"y":18,"times":15}'
                " 对格子挥工具，自动重复到目标消失（Axe砍树、Pickaxe敲石头；先 move_to 到旁边）\n"
                '- {"op":"chop","count":3} 自动砍树（找树→走过去→砍倒，重复 count 棵）\n'
                '- {"op":"interact","x":..,"y":..} 收获成熟作物/捡采集物\n'
                '- {"op":"plant","x":..,"y":..,"item":"防风草种子"} 在空耕地种（item 可省略=随便拿第一包种子）\n'
                '- {"op":"sell","item":"橡木"} 放进出货箱换钱（item 省略=把能卖的非种子都卖了）\n'
                '- {"op":"eat"} 吃背包里的食物补体力\n'
                '- {"op":"warp","location":"Farm","x":64,"y":15} 传送（Farm/Town/Beach/Forest/Mountain/BusStop/Mine）\n'
                '- {"op":"say","text":"..."} 游戏内说话（也可用顶层 say 字段）\n'
                '- {"op":"water_all"} 把当前地图所有旱着的作物全浇了（下雨天不用浇）\n'
                '- {"op":"harvest_all"} 收走当前地图所有成熟作物\n'
                '- {"op":"till"} 在附近开垦几块新地\n'
                '- {"op":"plant_all","item":"防风草种子","count":20} 把种子种到所有空耕地上（item 可省略）\n'
                '- {"op":"store"} 把背包里的材料/产物整理进农场的箱子\n'
                '- {"op":"mode","value":"fish|mine|farm|follow","minutes":6} 进入对应的干活模式'
                '（脚本专家逻辑执行，minutes 分钟后自动回来由你继续决策）\n'
                '- {"op":"sleep"} 回家上床睡觉（18:00 前别用）\n'
                "规则：坐标可以用【周围】或【全图】里的真实坐标（全图更全，优先看全图选目标）；"
                "体力<30 先 eat；22:30 前回家睡觉；"
                "被杂草/树枝/石头挡路不用绕，直接把目标定为杂物后面的东西即可（她会边走边清）；"
                "没要紧事就 plan 空数组，不要为动而动。\n"
                "【每日节奏】早上优先 water_all→harvest_all→有种子就 plant_all 把农场打理完，再去做主线；"
                "18:00 后不开新摊子；雨天不用浇水（鱼口好，适合 mode:fish 或 mode:mine）；"
                "22:30 前回家，到家可以 store 整理背包。\n"
                "【体力档】>70% 随便干；40~70% 干轻的（harvest_all/采集/钓鱼）；<40% 先 eat，没吃的就收工。"
                "她按真人节奏玩，不是效率机器。"
            )
            user = obs + ("\n【最近和主人的聊天】\n" + recent if recent else "")
            raw = await chat_once(
                model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                key, temperature=0.4, max_tokens=500,
            )
            data = self._extract_json(raw)
            if not isinstance(data, dict):
                return None
            say = str(data.get("say") or "").strip()
            plan = data.get("plan")
            if plan is None and say:
                plan = [{"op": "say", "text": say}]
            if not isinstance(plan, list):
                plan = []
            ops = []
            for o in plan[:5]:
                if not isinstance(o, dict):
                    continue
                op = str(o.get("op") or "").strip()
                if op not in self._BRAIN_OPS:
                    continue
                clean = {"op": op}
                for k in ("x", "y", "times", "count"):
                    if o.get(k) is not None:
                        try:
                            clean[k] = max(0, int(o[k]))
                        except Exception:
                            pass
                for k in ("tool", "item", "text", "location"):
                    v = str(o.get(k) or "").strip()
                    if v:
                        clean[k] = v[:40]
                ops.append(clean)
            if say and len(ops) <= 5:
                ops.append({"op": "say", "text": say[:60]})
            return ops
        except Exception as e:
            logger.warning(f"[StardewBrain] 游戏脑决策失败: {e}")
            return None

    async def _send_plan(self, ops):
        """把一批动作写成 action 文件（mod 顺序执行，结果写回 bridge_data 供下轮回喂）。"""
        for o in ops:
            self._brain_seq += 1
            payload = dict(o)
            payload["id"] = f"b{self._brain_seq}"
            try:
                await self.do_action("stardew_brain", payload)
            except Exception as e:
                logger.warning(f"[StardewBrain] 发送游戏脑动作失败: {e}")
        logger.info("[StardewBrain] 游戏脑计划下发 %d 个动作: %s",
                    len(ops), ",".join(str(o.get("op")) for o in ops))

    @staticmethod
    def _extract_json(raw):
        if not raw:
            return None
        t = re.sub(r"```(?:json)?|```", "", str(raw)).strip()
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    @staticmethod
    def _fmt_game_observation(state):
        """把 bridge_data 组装成 LLM 能读懂的农场实况文本。"""
        parts = []
        try:
            t = int(state.get("time") or 600)
            hh, mm = t // 100, t % 100
            w = str(state.get("weather") or "?")
            parts.append(f"【时间】第{state.get('day')}天 {state.get('season')} {w} · 游戏 {hh:02d}:{mm:02d}"
                         + ("（快23:00了，准备回家）" if t >= 2130 else ""))
        except Exception:
            pass
        player = state.get("player") or {}
        if player:
            pos = player.get("position") or {}
            try:
                px, py = int(float(pos.get("x", 0)) // 64), int(float(pos.get("y", 0)) // 64)
            except Exception:
                px, py = 0, 0
            parts.append(f"【我】金钱 {player.get('money', '?')}g · 在「{state.get('location')}」({px},{py})")
        host = state.get("host") or {}
        if host:
            hpos = host.get("position") or {}
            try:
                hx, hy = int(float(hpos.get("x", 0)) // 64), int(float(hpos.get("y", 0)) // 64)
            except Exception:
                hx, hy = 0, 0
            parts.append(f"【主人】{host.get('name')} 在「{host.get('location')}」({hx},{hy})")
        comps = state.get("companions") or []
        if comps:
            c = comps[0] or {}
            try:
                hp = f"{int(float(c.get('health') or 0))}/{int(float(c.get('maxHealth') or 0))}"
            except Exception:
                hp = "?"
            parts.append(f"【身体】体力 {c.get('stamina', '?')}% · 血量 {hp}")
        inv = state.get("inventory") or []
        if inv:
            items = []
            for i in inv[:26]:
                n, s = str(i.get("name") or "?"), int(i.get("stack") or 1)
                items.append(f"{n}×{s}" if s > 1 else n)
            parts.append("【背包】" + "、".join(items))
        sur = state.get("surroundings") or {}
        tiles = sur.get("tiles") or []
        if tiles:
            trees, stones, weeds, crops, machines, others = [], [], [], [], [], []
            empty_dirt = water = 0
            for tl in tiles:
                try:
                    x, y = int(tl.get("x")), int(tl.get("y"))
                except Exception:
                    continue
                terr = str(tl.get("terrain") or "")
                objt = str(tl.get("objType") or "")
                if terr == "tree":
                    trees.append(f"({x},{y})")
                elif objt == "stone":
                    stones.append(f"({x},{y})")
                elif objt in ("weed", "twig"):
                    weeds.append(f"({x},{y})")
                elif terr == "hoeDirt":
                    crop = str(tl.get("crop") or "")
                    if crop:
                        tag = "熟了可收" if tl.get("cropReady") else ("" if tl.get("watered") else "缺水")
                        crops.append(f"{crop}({x},{y})" + (f"⚠{tag}" if tag else ""))
                    else:
                        empty_dirt += 1
                elif objt == "machine":
                    machines.append(f"{tl.get('obj')}({x},{y})")
                elif tl.get("isWater"):
                    water += 1
                elif str(tl.get("obj") or ""):
                    others.append(f"{tl.get('obj')}({x},{y})")
            lines = []
            if trees:
                lines.append("树:" + "".join(trees[:12]) + (f" 等共{len(trees)}棵" if len(trees) > 12 else ""))
            if stones:
                lines.append("石头:" + "".join(stones[:10]))
            if weeds:
                lines.append("杂草树枝:" + "".join(weeds[:10]))
            if crops:
                lines.append("作物:" + "、".join(crops[:14]))
            if empty_dirt:
                lines.append(f"空耕地{empty_dirt}格(可种植)")
            if machines:
                lines.append("机器:" + "、".join(machines[:6]))
            if others:
                lines.append("其他:" + "、".join(others[:10]))
            if water:
                lines.append(f"水面{water}格(可钓鱼)")
            mons = sur.get("monsters") or []
            if mons:
                lines.append("怪物:" + "、".join(
                    f"{m.get('name')}({m.get('x')},{m.get('y')})" for m in mons[:6]))
            if lines:
                parts.append("【周围】(我为中心±8格) " + "；".join(lines))
        bq = state.get("brain") or {}
        results = bq.get("results") or []
        if results:
            parts.append("【上一步动作执行结果】（从失败里学：没斧头就先别砍树，被挡住就换路线）\n"
                         + "\n".join(str(r) for r in results[-5:]))
        # ★ 全图视野：ASCII 全景 + 全图实体（LLM 能看着整张地图决策，不再只有 ±8 格气泡）
        mp = state.get("map") or {}
        if mp.get("grid"):
            crops = mp.get("crops") or {}
            parts.append(
                "【全图 {loc} {w}x{h}】（每字符=2x2格；图例：.空 T树 s石 w草 o作物 !成熟 ~水 #不可走 =箱 M机器 f采集 @我）\n"
                "{grid}".format(loc=mp.get("loc"), w=mp.get("w"), h=mp.get("h"), grid=mp.get("grid")))
            tr = mp.get("trees") or []
            if tr:
                parts.append("全图树{tn}棵: {ts}".format(
                    tn=len(tr), ts=" ".join(f"({t.get('x')},{t.get('y')})" for t in tr[:24])))
            if crops:
                parts.append("作物统计: 共{t}，成熟可收 {r}，旱着缺水 {d}".format(
                    t=crops.get("total"), r=crops.get("ready"), d=crops.get("dry")))
            dc = mp.get("debrisCount")
            if dc:
                parts.append(f"杂物(石/草/枝)共{dc}处（挡路但会边走边清）")
            fg = mp.get("forage") or []
            if fg:
                parts.append("采集物: " + " ".join(
                    f"{f.get('name')}({f.get('x')},{f.get('y')})" for f in fg[:8]))
            ch = mp.get("chests") or []
            if ch:
                parts.append("箱子: " + " ".join(f"({c.get('x')},{c.get('y')})" for c in ch[:6]))
            mc = mp.get("machines") or []
            if mc:
                parts.append("机器: " + "、".join(
                    f"{m.get('name')}({m.get('x')},{m.get('y')})" for m in mc[:8]))
            parts.append("（全图坐标可直接用：move_to/use_tool/chop/interact 的 x,y 可以选全图里任何位置，"
                         "远就先 warp 或让寻路走过去；选目标时优先选能走到的，被#围住或~隔开的别选）")
        evs = [str(e) for e in (state.get("recentEvents") or []) if e]
        if evs:
            parts.append("【我最近在游戏里做的事】" + "；".join(evs[-4:]))
        return "\n".join(parts)

    # ── 感知摘要 ────────────────────────────────────────────
    async def _perceive(self):
        parts = []
        if self.mode == "client":
            # ★ client 模式：stardew_get_state 是 no-op，直接读 bridge_data 组情境摘要
            state = (self.mcp.read_state() or {}) if self.ready else {}
            if state:
                try:
                    t = int(state.get("time") or 0)
                    parts.append(
                        "【农场状态】第{day}天 · {season} · {weather} · 游戏时间 {hh:02d}:{mm:02d} · 我在{loc}（{mp}）".format(
                            day=state.get("day") or "?", season=state.get("season") or "?",
                            weather=state.get("weather") or "?", hh=t // 100, mm=t % 100,
                            loc=state.get("location") or "?",
                            mp="联机中" if state.get("multiplayer") else "单机"))
                except Exception:
                    pass
                # ★ 背包/眼前摘要：让聊天里说的事和游戏里真实干的活对得上
                try:
                    inv = state.get("inventory") or []
                    if inv:
                        names = []
                        for i in inv[:12]:
                            n, s = str(i.get("name") or ""), int(i.get("stack") or 1)
                            names.append(f"{n}×{s}" if s > 1 else n)
                        if names:
                            parts.append("【背包】" + "、".join(names))
                    tiles = (state.get("surroundings") or {}).get("tiles") or []
                    if tiles:
                        n_tree = sum(1 for tl in tiles if str(tl.get("terrain") or "") == "tree")
                        n_ready = sum(1 for tl in tiles if tl.get("cropReady"))
                        bits = []
                        if n_tree:
                            bits.append(f"{n_tree}棵树")
                        if n_ready:
                            bits.append(f"{n_ready}个成熟作物")
                        if bits:
                            parts.append("【眼前】附近有" + "、".join(bits))
                except Exception:
                    pass
        else:
            try:
                r = await self.mcp.call_tool("stardew_get_state", {})
                parts.append("【农场状态】" + StdioMCPClient.extract_text(r)[:300])
            except Exception:
                pass
        try:
            comps = await self._get_companions()
            for c in comps:
                parts.append(
                    "【我的状态】模式 {mode} · {status} · 位置 {loc} · 体力 {st:.0f}%".format(
                        mode=str(c.get("mode") or "?"), status=str(c.get("status") or "待机"),
                        loc=str(c.get("location") or "?"),
                        st=float(c.get("stamina") or 0) * (100 if float(c.get("stamina") or 0) <= 1 else 1),
                    ))
        except Exception:
            pass
        return "\n".join(parts)

    # ── QQ / App 推送与记忆回流 ─────────────────────────────
    async def _notify_qq_and_app(self, text):
        if not text:
            return
        try:
            from ..onebot import send_qq_message
            await send_qq_message(text)
        except Exception:
            pass
        try:
            from ..onebot import _push_to_app
            await _push_to_app(self.session_id, self.character, text, role="assistant")
        except Exception:
            pass

    async def _record_assistant_say(self, chat):
        if not chat:
            return
        try:
            from .. import db
            db.add_message(self.session_id, "assistant", chat, self.character)
        except Exception:
            pass


# ── 进程级单例 ─────────────────────────────────────────────
_brain: StardewBrain | None = None
_start_lock = asyncio.Lock()


def stardew_enabled() -> bool:
    if not bool(config.get("STARDEW_ENABLED")):
        return False
    if config.get("STARDEW_CLIENT_MODE"):
        # ★ 真联机模式：文件桥就够，不依赖 Node MCP Server（旧逻辑强制要求
        #   STARDEW_MCP_SERVER 非空，导致 client 模式永远待机）
        return bool(str(config.get("STARDEW_BRIDGE_PATH", "") or "").strip()
                    and str(config.get("STARDEW_ACTION_DIR", "") or "").strip())
    return bool(str(config.get("STARDEW_MCP_SERVER", "") or "").strip())


async def get_stardew_brain() -> StardewBrain | None:
    """拿到星露谷大脑（未启用/启动失败返回 None，任何异常静默降级）。"""
    global _brain
    if not stardew_enabled():
        return None
    if _brain is not None:
        return _brain
    async with _start_lock:
        if _brain is None:
            try:
                b = StardewBrain()
                if await b.start():
                    _brain = b
                else:
                    return None
            except Exception as e:
                logger.warning(f"[StardewBrain] 初始化失败: {e}")
                return None
    return _brain
