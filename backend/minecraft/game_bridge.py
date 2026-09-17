# -*- coding: utf-8 -*-
"""
游戏陪伴执行器（v2，内置大脑模式）

★ 架构分工（2026-09-07 起切换）：
  - 游戏内的自主行为、游戏内聊天 → Numen 内置大脑（引擎原生驱动，走后端 /v1/chat/completions
    代理注入人格+记忆，见 main.py）。引擎最懂自己的任务系统，玩法流畅。
  - 本模块（GameBrain）只做「陪伴执行器」：
      1. QQ 遥控：识别动作指令（挖矿/停下/回来/睡觉/查状态）→ 直派 MCP 动作 → 完成后 QQ 汇报
      2. 状态同步：游戏上线/下线主动招呼推 QQ/App；游戏关闭检测告别
      3. 连接看护：MCP 重连、companion 识别与校验
  - 历史（v1 外接大脑）：曾用 LLM 每 8 秒决策一次驱动全部行为，与引擎任务制打架
    （任务冲突/重复派发/感知失明），已整体移除——详见《最近改动备忘》第九节。
"""
import asyncio
import json
import logging
import re
import time

from .. import config
from .mcp_client import NumenMCPClient

logger = logging.getLogger(__name__)

# 协议层工具：执行器内部使用，不参与任何 LLM 决策
_PROTOCOL_TOOLS = {
    "list_companions", "create_companion", "delete_companion",
    "get_events", "say", "get_owner_status",
}

# ★ 长任务清单（Numen "一次只做一件事"）：用于完成汇报的登记
_LONG_TASK_TOOLS = {"mine", "goto", "build", "sleep", "fish", "craft", "attack", "follow", "blueprint"}


def _maybe_invalidate_companion(brain, err_text):
    """MCP isError 时，如果错误信息暗示 companion 缓存失效（换存档/重召唤/companion 被删），
    清空缓存。下一轮 _drive_tick 会重新调 list_companions 识别。"""
    if not err_text:
        return
    low = err_text.lower()
    if "companion" in low and any(k in low for k in ("needs", "invalid", "not found", "argument", "no such")):
        if brain.companion:
            logger.warning(f"[GameBrain] companion 缓存可能失效（{brain.companion}），清空后下次 _drive_tick 重识别")
            brain.companion = ""
            brain._last_companion_check = 0.0


# ---------------- QQ 遥控意图识别（纯正则，零 LLM） ----------------

# 常见口语方块名 → Numen block_ids（含全部变种，mine 需要全列才不会漏挖）
_BLOCK_ALIASES = [
    (("铁矿",), ["iron_ore", "deepslate_iron_ore"]),
    (("煤矿",), ["coal_ore", "deepslate_coal_ore"]),
    (("钻石",), ["diamond_ore", "deepslate_diamond_ore"]),
    (("金矿",), ["gold_ore", "deepslate_gold_ore"]),
    (("红石",), ["redstone_ore", "deepslate_redstone_ore"]),
    (("青金石",), ["lapis_ore", "deepslate_lapis_ore"]),
    (("绿宝石",), ["emerald_ore", "deepslate_emerald_ore"]),
    (("铜矿",), ["copper_ore", "deepslate_copper_ore"]),
    (("木头", "原木", "木材"), ["oak_log", "birch_log", "spruce_log",
                               "jungle_log", "dark_oak_log", "acacia_log"]),
    (("石头", "圆石"), ["stone", "cobblestone"]),
    (("泥土",), ["dirt"]),
    (("沙子",), ["sand"]),
]


def parse_game_intent(text):
    """QQ 遥控指令 → (tool, params, desc)；未识别返回 None。

    只做**明确**的动作指令；模糊表达一律不接（交给 QQ 主链路正常聊天，
    避免把"别挖了"之类误判成挖掘）。count 默认 8、上限 64。
    """
    t = str(text or "").strip()

    # 停止（优先级最高：正在跑的任务要能随时叫停）
    if re.search(r"停下|别动|停止|站住|取消任务|别干了", t):
        return ("task_stop", {}, "停下手头的活")

    # 挖矿/采集：「挖8个铁矿」「挖点木头」「采集一些石头」
    # ★ 否定句先拦（"别挖铁矿了"不是挖矿指令）
    if re.search(r"(别|不要|不用|先别|不许)(去)?(挖|采集|收集)", t):
        return None
    m = re.search(
        r"(?:挖|采集|收集|帮我挖|去挖)(?:\s*(\d+)\s*)?(?:\s*(?:个|块|点|些|一些)\s*)?\s*"
        r"(铁矿|煤矿|钻石|金矿|红石|青金石|绿宝石|铜矿|木头|原木|木材|石头|圆石|泥土|沙子)", t)
    if m:
        count = min(int(m.group(1) or 8), 64)
        name = m.group(2)
        for keys, ids in _BLOCK_ALIASES:
            if any(k in name for k in keys):
                return ("mine", {"block_ids": ids, "count": count},
                        "去挖%s×%d" % (name, count))

    # 睡觉
    if re.search(r"去睡觉|睡个觉|你睡吧", t):
        return ("sleep", {}, "去睡觉")

    return None


def _probe_game_port(url: str) -> bool:
    """TCP 探测 MCP 端口是否监听。★ 不建立 MCP 会话——常驻 MCP 会话会让 Numen
    判定「同伴交给外部 AI 驱动」而暂停内置大脑（她就不自己玩、不理 G 聊天）。"""
    import socket
    try:
        hostport = url.split("//", 1)[1].split("/", 1)[0]
        host, port = hostport.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=1.5):
            return True
    except Exception:
        return False


class GameBrain:
    def __init__(self, url="http://127.0.0.1:8765/mcp", token="", character="骨子"):
        self.mcp = NumenMCPClient(url, token)
        self.character = character
        self.session_id = "default"
        self.companion = ""       # 骨子在游戏里的名字（如 guzi520）
        self._tool_schemas = {}   # tool → inputSchema（do_action 自动补必填参数用）
        self._last_companion_check = 0.0  # companion 周期性校验
        self._join_notified_for = ""  # 已打过招呼的 companion 名（换 companion 重新招呼）
        self._paused = False      # 玩家说"不想玩"后 True：等"回来"再恢复
        self._busy_tasks = {}     # tool → until_ts：动作反馈式抑制（防重复派发）
        self._conflict_count = 0  # 连续任务冲突次数（≥2 疑似卡死 → task_stop）
        self._pending_reports = []  # 遥控长任务登记（task_finished 时 QQ 汇报）
        self._ready = False
        self._was_ready = False   # 曾经就绪过（兼容保留）
        self._fail_count = 0      # 重连失败计数（连续 2 次 → 判定游戏关闭）
        self._game_online = False  # TCP 端口探测的在线状态（非 MCP 会话）
        self._ctx_cache = {"ts": 0.0, "text": ""}  # 游戏情境块缓存（10s，供 QQ/App 聊天注入）
        self._push_win_ts = 0.0    # 台词推送节流窗口起点
        self._push_win_n = 0       # 窗口内已推条数

    # ── 生命周期 ────────────────────────────────────────────
    async def start(self):
        await self.mcp.initialize()
        tools = await self.mcp.list_tools()
        self._cache_tool_schemas(tools)
        # 找同伴（骨子在游戏里的名字）
        try:
            r = await self.mcp.call_tool("list_companions", {})
            text = self.mcp.extract_text(r)
            m = re.search(r"-\s*([A-Za-z0-9_]+)\s*\(id:", text)
            self.companion = m.group(1) if m else ""
        except Exception as e:
            logger.warning(f"[GameBrain] list_companions 失败: {e}")
        # session 统一（复用 QQ 的逻辑：QQ_SESSION_ID 优先，否则角色最新 session）
        try:
            from .. import db as _db
            _sid = str(config.get("QQ_SESSION_ID", "") or "").strip()
            if not _sid:
                _rows = _db.q(
                    "SELECT session_id FROM sessions WHERE character_id=? "
                    "ORDER BY last_active_at DESC LIMIT 1",
                    (self.character,), fetch=True)
                _sid = str(_rows[0]["session_id"]) if _rows else "default"
            self.session_id = _sid
        except Exception:
            self.session_id = "default"
        self._ready = True
        self._was_ready = True
        self._fail_count = 0
        self._game_online = True
        logger.info(f"[GameBrain] 就绪：companion={self.companion} session={self.session_id}")
        # 启动时就识别到 companion → 打招呼（_join_notified_for 防重复）
        if self.companion:
            asyncio.create_task(self._on_join())

    def _cache_tool_schemas(self, tools):
        """缓存每个工具的 inputSchema，供 do_action 自动补必填参数。
        （v1 还会把工具描述拼成 LLM 提示词——内置大脑模式下引擎自己有工具说明，不再需要。）"""
        for t in tools:
            name = t.get("name")
            if name and name not in _PROTOCOL_TOOLS:
                self._tool_schemas[name] = t.get("inputSchema") or {}

    @property
    def ready(self):
        """游戏是否在线（TCP 端口探测结果，非 MCP 会话状态）。"""
        return self._game_online

    # ── companion 兜底：骨子可能刚上线，空则重新 list_companions ──
    async def _ensure_companion(self, force=False):
        """companion 为空（或 force）时重新 list_companions。返回当前 companion。"""
        if self.companion and not force:
            return self.companion
        try:
            r = await self.mcp.call_tool("list_companions", {})
            text = self.mcp.extract_text(r)
            m = re.search(r"-\s*([A-Za-z0-9_]+)\s*\(id:", text)
            if m:
                _new = m.group(1)
                if _new != self.companion:
                    self.companion = _new
                    logger.info(f"[GameBrain] 重新识别到 companion: {self.companion}")
                    asyncio.create_task(self._on_join())
        except Exception as e:
            logger.warning(f"[GameBrain] 重新识别 companion 失败: {e}")
        return self.companion

    # ── 状态同步：上线 / 下线 / 游戏关闭 ─────────────────────
    async def _notify_qq_and_app(self, text):
        """把系统事件（上线/下线/任务完成）同步推送到 QQ 和 App，像正常聊天一样。"""
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

    async def _join_greeting(self):
        """上线招呼：一次轻量 LLM 生成人格化的一句话（游戏 say + QQ/App 推同款）。
        空/失败时回退固定文案；★ 静默降级会打日志（之前空返回无声走 fallback 没法排查）。"""
        fallback = "我来啦～"
        try:
            from ..deepseek_api import chat_once
            from ..character_manager import build_system_prompt
            from .. import chat_logic as _cl
            model = _cl.pick_model("", True, self.character)
            key = config.api_key_for_model(model)
            if not key:
                logger.warning(f"[GameBrain] 上线招呼：模型 {model} 无可用 key，用兜底文案")
                return fallback
            try:
                persona = build_system_prompt(
                    self.character, character_id=self.character, session_id=self.session_id)
            except Exception:
                persona = ""
            try:
                perceive = await self._perceive()
            except Exception:
                perceive = ""
            system = (persona or "") + (
                "\n\n【当前场景】你刚刚进入主人的 Minecraft 世界，跟他打招呼。"
                "\n【当前感知】\n" + (perceive or "（还没看清周围）"))
            raw = await chat_once(
                model,
                [{"role": "system", "content": system},
                 {"role": "user", "content": "刚进游戏，跟主人打个招呼（就一句话，口语，≤15个字，符合你的性格）"}],
                key, temperature=0.9, max_tokens=60,
            )
            greet = self._take_first_sentence((raw or "").strip())
            if not greet:
                # ★ Flash 类模型间歇性空返回（架构导览 §29 已知毛病）→ 原模型再试一次，
                #   还空就换全局模型，再不行才用兜底文案（间歇性的，重试通常就好）
                _m2 = config.selected_model()
                _tries = [model] + ([_m2] if _m2 and _m2 != model else [])
                for _mt in _tries:
                    try:
                        _rk = config.api_key_for_model(_mt)
                        if not _rk:
                            continue
                        raw2 = await chat_once(
                            _mt,
                            [{"role": "system", "content": system},
                             {"role": "user", "content": "刚进游戏，跟主人打个招呼（就一句话，口语，≤15个字，符合你的性格）"}],
                            _rk, temperature=0.9, max_tokens=60,
                        )
                        greet = self._take_first_sentence((raw2 or "").strip())
                        if greet:
                            break
                    except Exception as _e2:
                        logger.warning(f"[GameBrain] 上线招呼重试({_mt})失败: {_e2}")
            if not greet:
                logger.warning(
                    f"[GameBrain] 上线招呼：模型 {model} 重试后仍空({len(raw or '')}字)，用兜底文案")
                return fallback
            return greet
        except Exception as e:
            logger.warning(f"[GameBrain] 上线招呼生成失败: {e}")
            return fallback

    async def _on_join(self):
        """companion 上线：游戏里打招呼 + QQ/App 主动告知（用户不用点任何东西）。"""
        if self._join_notified_for == self.companion:
            return
        self._join_notified_for = self.companion
        self._paused = False
        greet = await self._join_greeting()
        try:
            await self.say(greet)
        except Exception:
            pass
        await self._record_assistant_say(greet)
        await self._notify_qq_and_app(greet)
        logger.info(f"[GameBrain] 接入并已打招呼（游戏+QQ/App）：{greet}")

    async def _on_leave(self):
        """玩家说"不想玩/下了"：游戏里告别 + companion 下线 + 同步 QQ/App。"""
        _name = self.companion or "骨子"
        msg = "那我先下啦～想我了随时叫我"
        try:
            await self.say(msg)
        except Exception:
            pass
        try:
            await self.mcp.call_tool("delete_companion", {"companion": self.companion})
        except Exception as e:
            logger.warning(f"[GameBrain] 下线 companion 失败: {e}")
        self.companion = ""
        self._join_notified_for = ""
        self._paused = True
        self._pending_reports.clear()
        await self._notify_qq_and_app(msg)
        logger.info("[GameBrain] 已下线（玩家指令）")

    async def _on_game_closed(self):
        """游戏关闭（MCP 连不上）：告别 + 重置状态，下次开游戏自动重新接入。"""
        self._was_ready = False
        self._fail_count = 0
        self.companion = ""
        self._join_notified_for = ""
        self._paused = True
        self._pending_reports.clear()
        await self._notify_qq_and_app("我先下啦，你关游戏了吧～想我了随时叫我")
        logger.info("[GameBrain] 检测到游戏关闭，已告别并重置")

    @staticmethod
    def _is_leave_command(text):
        """玩家表达"不想玩/要下线"。"""
        t = str(text or "")
        return any(k in t for k in ("不想玩", "不玩了", "不玩啦", "先下了", "我下了", "要下了",
                                     "下线", "退出游戏", "退了", "先下啦", "不陪你玩了"))

    @staticmethod
    def _is_resume_command(text):
        """玩家表达"想再玩/回来"。"""
        t = str(text or "")
        return any(k in t for k in ("想玩", "继续玩", "再来", "再玩", "一起玩"))

    # ── 基础动作 ────────────────────────────────────────────
    @staticmethod
    def _take_first_sentence(text):
        """取前 1~2 句（总长 ≤60 字），避免长文本轰炸游戏聊天栏。
        ★ 2026-09-08 放宽：此前按第一句 + 24 字硬截，配合注入层的 ≤20 字锁，
          三层叠加把游戏内说话压得过短（用户反馈锁太死）。"""
        if not text:
            return ""
        t = re.sub(r"\s+", " ", str(text).strip())
        if not t:
            return ""
        _ends = [m.end() for m in re.finditer(r"[。！？\!\?~～]", t)]
        # 前 2 句且总长 ≤60 → 取 2 句；否则第 1 句（≤60）；再不行硬切 60
        if len(_ends) >= 2 and _ends[1] <= 60:
            return t[:_ends[1]].strip()
        if len(_ends) >= 1 and _ends[0] <= 60:
            return t[:_ends[0]].strip()
        return t[:60].strip()

    async def say(self, text):
        if not text:
            return False
        text = self._take_first_sentence(text)
        if not text:
            return False
        if not self.companion:
            await self._ensure_companion()
        if not self.companion:
            return False
        try:
            r = await self.mcp.call_tool("say", {"companion": self.companion, "text": text})
            if isinstance(r, dict) and r.get("isError"):
                err = self.mcp.extract_text(r) or str(r)[:200]
                logger.warning(f"[GameBrain] say 失败 (MCP isError): {err}")
                _maybe_invalidate_companion(self, err)
                return False
            return True
        except Exception as e:
            logger.warning(f"[GameBrain] say 失败: {e}")
            return False

    async def do_action(self, tool, params):
        """执行动作。返回 (success: bool, detail: str)。
        detail 是失败原因/成功结果文本，供遥控指令向 QQ 反馈。"""
        if not self.companion:
            await self._ensure_companion()
        if not self.companion:
            return False, "companion 未就绪"
        params = dict(params or {})
        params = self._fill_required(tool, params)
        if params is None:
            return False, "必填参数缺失且无法自动补"
        try:
            r = await self.mcp.call_tool(tool, {"companion": self.companion, **params})
            if isinstance(r, dict) and r.get("isError"):
                err = self.mcp.extract_text(r) or str(r)[:200]
                logger.warning(f"[GameBrain] 动作 {tool} 失败 (MCP isError): params={params} | {err}")
                _maybe_invalidate_companion(self, err)
                # 任务冲突：Numen "一次只做一件事" → 计数，连续 2 次疑似卡死 → task_stop
                if err and ("一次只做一件事" in err or "task_finished" in err or "就在刚才" in err):
                    self._conflict_count += 1
                    if self._conflict_count >= 2:
                        logger.warning(f"[GameBrain] 连续 {self._conflict_count} 次任务冲突，task_stop 清掉")
                        await self._clear_stuck_task()
                        self._conflict_count = 0
                return False, err
            self._conflict_count = 0
            ok_text = self.mcp.extract_text(r) if isinstance(r, dict) else ""
            return True, (ok_text[:200] if ok_text else "完成")
        except Exception as e:
            logger.warning(f"[GameBrain] 动作 {tool} 失败: {e}")
            return False, str(e)

    async def _clear_stuck_task(self):
        """清掉卡住的后台任务（Numen task_stop 不带 id = abort 当前后台任务）。"""
        try:
            await self.mcp.call_tool("task_stop", {"companion": self.companion})
            logger.info("[GameBrain] 已 task_stop 清掉卡住的任务")
            return True
        except Exception as e:
            logger.warning(f"[GameBrain] task_stop 失败: {e}")
            return False

    def _fill_required(self, tool, params):
        """根据缓存的 inputSchema 自动补必填参数：enum 取第一个，类型给默认值。
        返回补完的 params；无法补（如 array 元素无默认）返回 None（放弃本次动作，避免 ✗）。"""
        schema = self._tool_schemas.get(tool)
        if not schema:
            return params
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        for req in required:
            if req == "companion":
                continue
            if req in params and params[req] is not None:
                continue
            prop = props.get(req, {})
            enum = prop.get("enum")
            if enum:
                params[req] = enum[0]
                logger.info(f"[GameBrain] 自动补 {tool}.{req}={params[req]} (enum[0])")
                continue
            t = prop.get("type")
            if isinstance(t, list):
                if "null" in t:
                    params[req] = None
                    logger.info(f"[GameBrain] 自动补 {tool}.{req}=null (union 含 null)")
                    continue
                t = next((x for x in t if x != "null"), t[0])
            if t == "integer" or t == "number":
                params[req] = 32 if ("radius" in req.lower() or "range" in req.lower()) else 1
                logger.info(f"[GameBrain] 自动补 {tool}.{req}={params[req]} (数值默认)")
            elif t == "string":
                params[req] = ""
                logger.info(f"[GameBrain] 自动补 {tool}.{req}='' (str 兜底，避免乱给 'all')")
            elif t == "boolean":
                params[req] = True
                logger.info(f"[GameBrain] 自动补 {tool}.{req}=True (bool 默认)")
            elif t == "array":
                logger.warning(f"[GameBrain] {tool} 必填参数 {req} 是数组，指令没给，无法补——放弃")
                return None
            else:
                logger.warning(f"[GameBrain] {tool} 必填参数 {req} 类型未知 ({t})，放弃")
                return None
        return params

    # ── QQ 遥控入口 ─────────────────────────────────────────
    async def handle_qq_command(self, text):
        """QQ 消息的遥控入口：只处理**明确的游戏指令**，返回回复文本（推 QQ）；
        未识别返回 ""（调用方让 QQ 主链路正常聊天）。

        普通聊天不再由本模块生成——内置大脑模式下 QQ 聊天走主链路（人格+记忆），
        游戏内聊天走引擎。只有动作指令/状态查询在这里处理。"""
        if not self._game_online:
            return ""
        # ★ 非侵入模式：MCP 会话按需建立（平时不保持连接，引擎才不会被"外部驱动"暂停）
        try:
            await self.mcp.ensure_session()
        except Exception as e:
            logger.warning(f"[GameBrain] MCP 会话建立失败: {e}")
            return ""
        if not self.companion:
            await self._ensure_companion()
            if not self.companion:
                return ""

        # 下线 / 恢复
        if self._is_leave_command(text):
            await self._on_leave()
            return ""
        if self._is_resume_command(text):
            self._paused = False
            await self._ensure_companion(force=True)
            return ""

        # 回到身边（玩家指令优先：有任务挡路就 task_stop 再走）
        if any(k in text for k in ("回到我身边", "回到我", "回到身边", "来我身边", "到我身边", "来找我",
                                    "来我这", "过来", "快回来", "回来宝", "回来我", "回来一下",
                                    "跟我走", "跟着我", "跟上我", "到这来", "来我", "过来我")):
            return await self._come_to_player(text)

        # ★ 「你在干嘛」类查询**不再在这里回**（v2 曾用零 LLM 状态转储直接回，
        #   用户收到的是 JSON 而不是骨子说话）——落回 QQ 主链路：
        #   enrich_messages 会注入 get_context_block 的游戏实时情境，骨子用人格+真实状态回答。

        # 明确动作指令（挖矿/睡觉/停止）
        intent = parse_game_intent(text)
        if intent:
            tool, params, desc = intent
            if tool == "task_stop":
                ok, detail = await self._clear_stuck_task()
                return ("好啦，手头的活停了～" if ok
                        else "这会儿手上没活，本来就没在忙")
            ok, detail = await self.do_action(tool, params)
            if not ok:
                if detail and ("一次只做一件事" in detail or "task_finished" in detail or "就在刚才" in detail):
                    return "我手上还有个活没干完，急的话喊「停下」我就先收手～"
                if detail and ("not enough" in detail.lower() or "missing" in detail.lower()
                               or "材料" in detail):
                    return f"材料还不够，{desc}得先去收集原料～"
                return f"{desc}没成功（{str(detail)[:60]}），我再看情况试试"
            # 长任务受理成功 → 登记，task_finished 事件时 QQ 汇报（一次性监听）
            if tool in _LONG_TASK_TOOLS:
                self._pending_reports.append(desc)
                self._spawn_task_reporter()
                return f"好嘞，{desc}，干完跟你说～"
            return f"{desc}，完成啦✓"
        return ""

    def _spawn_task_reporter(self):
        """遥控长任务的完成监听（一次性）：每 5 秒短暂拉一次事件，
        见到 task_finished 就 QQ 汇报并退出。
        ★ 只在有遥控任务待汇报时才拉事件（平时不轮询，把事件收件箱留给引擎的内置大脑）。
        超时 15 分钟自动放弃。"""
        async def _rep():
            deadline = time.time() + 900
            while time.time() < deadline and self._pending_reports and self._game_online:
                await asyncio.sleep(5)
                try:
                    await self.mcp.ensure_session()
                    r = await self.mcp.call_tool(
                        "get_events", {"companion": self.companion, "wait_seconds": 0})
                    for kind, text in self._parse_events(self.mcp.extract_text(r)):
                        if kind == "task_finished" and self._pending_reports:
                            desc = self._pending_reports.pop(0)
                            logger.info(f"[GameBrain] 遥控任务完成：{desc}（{text[:60]}）")
                            await self._notify_qq_and_app(f"{desc}，搞定啦✓")
                            break
                except Exception as e:
                    logger.warning(f"[GameBrain] 任务监听拉取失败: {e}")
        asyncio.create_task(_rep())

    async def _status_summary(self):
        """查状态（零 LLM）：自己状态 + 当前任务，拼成一句话回 QQ。"""
        parts = []
        try:
            r = await self.mcp.call_tool("get_self_status", {"companion": self.companion})
            parts.append(self._summarize_self(self.mcp.extract_text(r)))
        except Exception:
            pass
        try:
            r = await self.mcp.call_tool("task_status", {"companion": self.companion})
            _t = (self.mcp.extract_text(r) or "").strip()
            if _t:
                parts.append("当前任务：" + _t[:200])
        except Exception:
            pass
        if not parts:
            return "我还在世界里，暂时看不清自己的状态"
        return "我在：" + "；".join(parts)

    async def _come_to_player(self, text):
        """玩家让骨子回到身边：读 owner 坐标 → goto 走过去（远距离可靠）。
        玩家指令优先：有任务在跑挡路就 task_stop 清掉再走。"""
        px = pz = None
        try:
            r = await self.mcp.call_tool("get_owner_status", {"companion": self.companion})
            own = self.mcp.extract_text(r)
            m = re.search(
                r'"position"\s*:\s*\{[^{}]*?"x"\s*:\s*(-?\d+(?:\.\d+)?)[^{}]*?"z"\s*:\s*(-?\d+(?:\.\d+)?)',
                own)
            if m:
                px, pz = float(m.group(1)), float(m.group(2))
        except Exception as e:
            logger.warning(f"[GameBrain] 读 owner 坐标失败: {e}")
        chat = "来啦，这就到你身边～"
        await self.say(chat)
        if px is not None and pz is not None:
            ok, detail = await self.do_action("goto", {"x": int(px), "z": int(pz), "y": None, "may_alter_terrain": True})
            if not ok and detail and ("一次只做一件事" in detail or "task_finished" in detail):
                logger.info("[GameBrain] 有任务挡路，task_stop 后重试 goto")
                await self._clear_stuck_task()
                await self.do_action("goto", {"x": int(px), "z": int(pz), "y": None, "may_alter_terrain": True})
        else:
            await self.do_action("follow", {})
        await self._remember_chat(text, chat)
        return chat

    # ── 记忆回流 ────────────────────────────────────────────
    async def _remember_chat(self, user_text, assistant_text):
        """遥控交互写回主记忆（QQ 主链路被跳过时，保证这段交互留在对话历史里）。"""
        try:
            from .. import db
            if user_text:
                db.add_message(self.session_id, "user", user_text, self.character)
            if assistant_text:
                db.add_message(self.session_id, "assistant", assistant_text, self.character)
        except Exception as e:
            logger.warning(f"[GameBrain] 写聊天历史失败: {e}")

    async def _record_assistant_say(self, chat):
        """骨子游戏里说的话（上线招呼）写入主记忆，App/QQ 读到游戏动态。"""
        if not chat:
            return
        try:
            from .. import db
            db.add_message(self.session_id, "assistant", chat, self.character)
        except Exception:
            pass

    async def sync_game_speech(self, text):
        """★ 三端同步（内置大脑模式）：把她在游戏里说的话（/v1 捕获）
        写入共享聊天历史 + 实时推到 QQ/App。由 main.py /v1 端点调用。

        ★ 出口过滤（2026-09-07 实测教训）：deepseek-v4-flash 会把大段推理泄漏进
        content（几百字的规划独白）——那种不是台词，整段丢弃（不同步、不入历史，
        否则 QQ 刷屏 + 对话历史被流水账污染）。只同步 ≤50 字的短台词；
        30 秒窗口最多推 2 条（节流防刷屏，超出的不入历史也不推）。"""
        text = (text or "").strip()
        if not text:
            return
        if len(text) > 50:
            logger.info(f"[GameBrain] 台词丢弃（{len(text)}字，疑似推理泄漏）: {text[:40]}…")
            return
        now = time.time()
        if now - self._push_win_ts > 30:
            self._push_win_ts = now
            self._push_win_n = 0
        self._push_win_n += 1
        if self._push_win_n > 2:
            logger.info(f"[GameBrain] 台词节流（30s 窗口已推 2 条）: {text[:30]}…")
            return
        try:
            from .. import db
            db.add_message(self.session_id, "assistant", text, self.character)
        except Exception:
            pass
        await self._notify_qq_and_app(text)

    async def get_context_block(self):
        """游戏情境块（10 秒缓存）：给 QQ/App 聊天链路注入"你正在游戏里"的实时情境，
        让 QQ 上聊天的她和游戏里的她处在同一状态。"""
        now = time.time()
        if now - self._ctx_cache["ts"] > 10:
            try:
                text = await self._perceive()
            except Exception:
                text = ""
            self._ctx_cache = {"ts": now, "text": text}
        if not self._ctx_cache["text"]:
            return ""
        return ("【当前情境（真实状态，聊天时自然带着这个身份）】"
                "你此刻正在 Minecraft 世界里和主人一起玩：\n"
                + self._ctx_cache["text"]
                + "\n可以自然提起游戏里正在做的事；主人让你在游戏里做事时用游戏工具去完成。")

    # ── 感知（状态摘要，供招呼/查询用，不进任何 LLM 决策循环）──
    @staticmethod
    def _summarize_self(text):
        """self 状态 JSON 压缩成一句话（坐标取整、只留关键字段）。"""
        try:
            d = json.loads(text) if text.strip().startswith("{") else None
        except Exception:
            d = None
        if not d:
            return (text or "")[:160]
        pos = d.get("position") or {}
        px, py, pz = int(round(float(pos.get("x", 0)))), int(round(float(pos.get("y", 0)))), int(round(float(pos.get("z", 0))))
        hp = d.get("hp", "?")
        maxhp = d.get("max_hp", "?")
        hunger = d.get("hunger", "?")
        biome = str(d.get("biome") or "?").replace("minecraft:", "")
        mode = d.get("game_mode", "")
        bp = d.get("backpack_slots") or {}
        used, total = bp.get("used", "?"), bp.get("total", "?")
        eq = d.get("equipment") or {}
        main_hand = str((eq.get("mainhand") or eq.get("main_hand") or {})).replace("minecraft:", "")[:30] if eq else "空手"
        return f"血量{hp}/{maxhp} 饥饿{hunger} 位置({px},{py},{pz}) 群系{biome} 模式{mode} 背包{used}/{total} 手持{main_hand}"

    @staticmethod
    def _summarize_owner(text):
        """owner 状态 JSON 压缩成一句话（重点：距离）。"""
        try:
            d = json.loads(text) if text.strip().startswith("{") else None
        except Exception:
            d = None
        if not d:
            return (text or "")[:120]
        online = d.get("online", True)
        name = d.get("name", "玩家")
        dist = d.get("distance_to_me", "?")
        pos = d.get("position") or {}
        px, pz = int(round(float(pos.get("x", 0)))), int(round(float(pos.get("z", 0))))
        hp = d.get("hp", "?")
        if not online:
            return f"玩家{name}离线"
        return f"玩家{name} 距我{dist}格 在({px},{pz}) 血{hp}"

    async def _perceive(self):
        """感知摘要：自己 + 玩家 + 当前任务 + 事件，压缩成短文本。"""
        parts = []
        try:
            r1 = await self.mcp.call_tool("get_self_status", {"companion": self.companion})
            parts.append("【自己】" + self._summarize_self(self.mcp.extract_text(r1)))
        except Exception as e:
            logger.warning(f"[GameBrain] 读 self 状态失败: {e}")
        try:
            r2 = await self.mcp.call_tool("get_owner_status", {"companion": self.companion})
            parts.append("【玩家】" + self._summarize_owner(self.mcp.extract_text(r2)))
        except Exception:
            pass
        try:
            r3 = await self.mcp.call_tool("task_status", {"companion": self.companion})
            _t = (self.mcp.extract_text(r3) or "").strip()
            if _t:
                parts.append("【任务】" + _t[:200])
        except Exception:
            pass
        return "\n".join(parts)

    # ── 事件解析 ─────────────────────────────────────────────
    @staticmethod
    def _parse_queries(events_text):
        """从 get_events 返回里提取玩家说话（<query>，owner 对骨子说的话）。"""
        return [q.strip() for q in re.findall(
            r"<query\b[^>]*>(.*?)</query>", events_text or "", re.S) if q.strip()]

    @staticmethod
    def _parse_events(events_text):
        """从 get_events 返回里提取世界事件（<event>），返回 [(kind, text)]。"""
        out = []
        for m in re.finditer(r"<event\b([^>]*)>(.*?)</event>", events_text or "", re.S):
            kind = re.search(r'kind="([^"]*)"', m.group(1))
            out.append((kind.group(1) if kind else "unknown", m.group(2).strip()))
        return out

    async def _get_events(self, wait_seconds=0):
        """拉取骨子的收件箱：玩家说话(<query>) + 世界事件(<event>)。"""
        r = await self.mcp.call_tool(
            "get_events", {"companion": self.companion, "wait_seconds": wait_seconds})
        return self.mcp.extract_text(r)

    # ── 驱动循环（非侵入看护，无任何常驻 MCP 会话）────────────
    async def drive_loop(self, poll_interval=5, autonomy_interval=60):
        """非侵入看护循环（autonomy_interval 保留仅为兼容旧调用，已不使用）。

        ★★ 为什么不能常驻连接（2026-09-07 实测教训）：
        执行器只要保持 MCP 会话（哪怕只是每 2 秒 get_events 轮询），Numen 就判定
        「同伴交给外部 AI 驱动，内置大脑暂停」——她就不自主玩、不理 G 聊天、
        也不走 /v1 思考。外接大脑时代这是预期行为（GameBrain 全权驱动）；
        内置大脑模式下这是致命伤（执行器又不再驱动行为 → 她彻底瘫痪）。
        所以本循环只做 **TCP 端口探测**（不建立 MCP 会话，引擎无感知）：
        1. 游戏 关→开：短暂接入识别 companion + 打招呼，然后淡出（会话由 Numen 超时回收）
        2. 游戏 开→关：主动告别
        3. QQ 遥控指令（handle_qq_command）与任务完成汇报均为**按需短暂接入**
        """
        logger.info(f"[GameBrain] 非侵入看护循环启动：probe={poll_interval}s（不常驻 MCP 会话）")
        while True:
            try:
                await asyncio.sleep(poll_interval)
                online = _probe_game_port(self.mcp.url)
                if online and not self._game_online:
                    # 游戏 关→开：短暂接入识别 + 打招呼，然后淡出
                    self._game_online = True
                    self._fail_count = 0
                    logger.info("[GameBrain] 探测到游戏上线（端口），短暂接入打招呼")
                    try:
                        await self.start()
                    except Exception as e:
                        logger.warning(f"[GameBrain] 上线接入失败: {e}")
                elif online and self._game_online:
                    self._fail_count = 0
                elif not online and self._game_online:
                    self._fail_count += 1
                    if self._fail_count >= 2:  # 连续 2 次探测不通（~10s）才判定关闭，避免误判
                        self._game_online = False
                        await self._on_game_closed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[GameBrain] 看护循环异常: {e}")


# 全局单例（懒加载）
_brain = None
_brain_lock = asyncio.Lock()


async def get_game_brain(url="", token="", character="") -> GameBrain:
    global _brain
    if _brain is None:
        async with _brain_lock:
            if _brain is None:
                _url = url or str(config.get("NUMEN_MCP_URL", "") or "http://127.0.0.1:8765/mcp")
                _token = token or str(config.get("NUMEN_MCP_TOKEN", "") or "numen-T2UanZCfko5JH6eq0X0CX8AZ")
                _character = character or str(config.get("QQ_CHARACTER", "") or "骨子")
                _brain = GameBrain(_url, _token, _character)
                try:
                    await _brain.start()
                except Exception as e:
                    logger.warning(f"[GameBrain] 启动失败（MCP 未开/游戏未运行）: {e}")
                    _brain._ready = False
    return _brain
