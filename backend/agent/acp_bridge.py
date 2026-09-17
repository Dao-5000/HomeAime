# -*- coding: utf-8 -*-
"""ACP 桥 —— 一个「工作目录 + 权限档 + 伴侣会话」对应一个 harness 进程。

不变量：
  · 进程 cwd 决定会话工作区；沙箱根取进程 cwd（配置来源，真机未单独证伪 —— 真机实测
    workspace-write 下 pwsh 连工作区内的文件访问也会被沙箱拦，见 task-5-report §8.1）
  · 一条桥 = 一个消费者（工作目录 + 权限档 + 伴侣会话三者定一条桥）；同一时刻只允许一个
    prompt（ACP 硬约束），第二个 prompt 会被**明确拒绝**（BridgeBusy），由上层排队或先 cancel
  · 审批走项目既有 approval.py，保证前端复用现成的审批卡与 /api/agent/approval 端点
"""
import asyncio
import os

from . import approval as _approval
from .acp_client import AcpClient
from .acp_events import ToolCallRegistry, map_update
from .capability import probe as _probe

_BRIDGES: dict = {}
_LOCK = asyncio.Lock()

#: 同一时刻只允许一个 prompt（ACP 硬约束）—— 上层（Task 8）的排队逻辑认这个异常类
BUSY_ERR = ("这条桥正在跑上一个任务（ACP 一条会话同一时刻只允许一个 prompt）："
            "要插话请先 cancel() 再发，或者在上层排队等它说完。")


class BridgeBusy(RuntimeError):
    """同一条桥上并发发起第二个 prompt：明确拒绝，绝不静默覆盖前一个的 on_event/busy。"""


def _key(cwd: str, permission_mode: str, session_id: str = "default") -> str:
    """桥的注册键：一个消费者（工作目录 + 权限档 + 伴侣会话）一条桥。"""
    return "%s|%s|%s" % (os.path.abspath(cwd), permission_mode or "workspace-write",
                         session_id or "default")


def _argv(cli: str, patch: str) -> list:
    argv = ["node", cli, "--profile", "acp"]
    if patch:
        argv += ["--patch", patch]
    return argv


def _pick_option(params: dict, kind: str, fallback: str) -> str:
    """按 kind 从 params.options 里挑 optionId；挑不到才回落硬编码。

    真机实测 id 就是 allow-once / reject-once，但 id 是服务端给的，不该写死
    （写死以后服务端一改名，表现就是"人点了同意却没生效"）。
    """
    try:
        for o in (params or {}).get("options") or []:
            if isinstance(o, dict) and str(o.get("kind") or "") == kind:
                oid = str(o.get("optionId") or "")
                if oid:
                    return oid
    except Exception:
        pass
    return fallback


class AcpBridge:
    def __init__(self, cwd: str, *, patch: str = "", permission_mode: str = "workspace-write",
                 log=print, session_id: str = "default", character_id: str = "default"):
        self.cwd = os.path.abspath(cwd)
        self.patch = patch
        self.permission_mode = permission_mode or "workspace-write"
        self.log = log
        self.session_id = session_id
        self.character_id = character_id
        self.client = None
        self.acp_session_id = ""
        self.config_options = []
        self.reg = ToolCallRegistry()
        self._busy = False
        self._on_event = None
        self._log_tail = []          # harness/客户端最近几条日志：失败时拼进异常给人看原文
        self._approvals = set()      # 本桥创建过的审批 id（stop() 要把还挂着的结算掉）
        self._reg_key = ""           # 登记进 _BRIDGES 用的键（stop() 按它注销）

    # ---------- 日志与失败原因 ----------
    def _log(self, msg):
        """接住客户端/harness 的日志尾巴，再转给上层日志（失败原因要能给人看原文）。"""
        try:
            s = str(msg)
            self._log_tail.append(s)
            del self._log_tail[:-10]
        except Exception:
            s = ""
        try:
            self.log(s)
        except Exception:
            pass

    def _why(self) -> str:
        """失败原因：把 harness 给的原文拼出来（Task 8 的降级卡要显示给用户）。"""
        try:
            tail = [s for s in self._log_tail[-4:] if str(s).strip()]
            return "；".join(tail) if tail else "harness 没有给出更多信息"
        except Exception:
            return "harness 没有给出更多信息"

    # ---------- 生命周期 ----------
    async def start(self):
        cap = _probe()
        if not cap.get("ok"):
            raise RuntimeError(cap.get("reason") or "没有可用的手")
        env = {}
        if self.permission_mode:
            env["DSH_PERMISSION_MODE"] = self.permission_mode
        self.client = AcpClient(_argv(cap["cli"], self.patch), cwd=self.cwd, env=env,
                                on_notification=self._on_notification,
                                on_request=self._on_request, log=self._log)
        await self.client.start()
        # ★ 进程一起来就登记：`stop_all()` 才收得到「直接 new 出来的桥」（真机校验脚本就是这么 new 的）
        self._reg_key = _key(self.cwd, self.permission_mode, self.session_id)
        _BRIDGES[self._reg_key] = self
        try:
            res = await self.client.request("initialize", {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "homeaime-agent-window", "version": "1.0"},
            }, timeout=120)
            if not isinstance(res, dict):
                raise RuntimeError("ACP 握手失败：%s" % self._why())
            try:
                await self.client.request("authenticate", {"methodId": "none"}, timeout=30)
            except Exception:
                pass
            return res
        except Exception:
            # ★ 握手失败必须当场收掉这个刚起好的进程 —— 否则上层每次重试都会攒下一个活着
            #   但不干活的 node（"起不来"的那一次握手失败尤其容易漏）。
            await self.stop()
            raise

    @property
    def alive(self) -> bool:
        return bool(self.client) and self.client.alive

    @property
    def busy(self) -> bool:
        return self._busy

    async def _settle_pending(self, reason: str) -> int:
        """把本桥创建过、还挂着的审批全部判拒 —— 绝不留「没人等的待批卡」。"""
        n = 0
        for aid in list(self._approvals):
            try:
                if _approval.reject(aid, reason):
                    n += 1
            except Exception:
                pass
            self._approvals.discard(aid)
        return n

    async def stop(self):
        # ★ R31 ①：先结算本桥的待批审批 —— 必须在 client.stop() **之前**，这样权限应答还发得出去。
        #   否则 client.stop() 取消处理它的后台任务，而 CancelledError 不归 except Exception 管，
        #   审批会永远停在 pending：之后 approval.list_pending 会把一张死卡递给窗口。
        if await self._settle_pending("桥已停止"):
            try:
                await asyncio.sleep(0.05)      # 给应答一个送出去的机会
            except Exception:
                pass
        try:
            if self.client:
                await self.client.stop()
        except Exception:
            pass
        finally:
            # ★ R31 ②：关进程期间还可能有新的权限请求挤进来 —— 再扫一遍，别留幽灵卡
            await self._settle_pending("桥已停止")
            if self._reg_key and _BRIDGES.get(self._reg_key) is self:   # 别把后来者顶掉
                _BRIDGES.pop(self._reg_key, None)

    # ---------- 会话 ----------
    async def ensure_session(self, key: str = "default") -> str:
        if self.acp_session_id:
            return self.acp_session_id
        res = await self.client.request("session/new", {"cwd": self.cwd, "mcpServers": []}, timeout=120)
        if not isinstance(res, dict) or not res.get("sessionId"):
            raise RuntimeError("session/new 失败：%s" % self._why())
        self.acp_session_id = str(res["sessionId"])
        self.config_options = res.get("configOptions") or []
        return self.acp_session_id

    async def set_model(self, provider: str, model: str) -> bool:
        """切干活用的模型（不影响伴侣主脑）。"""
        try:
            sid = await self.ensure_session()
            res = await self.client.request("session/set_config_option", {
                "sessionId": sid, "configId": "model", "value": [provider, model]}, timeout=60)
            return isinstance(res, dict)
        except Exception as e:
            self.log("[ACP] 切模型失败: %r" % (e,))
            return False

    # ---------- 跑任务 ----------
    async def prompt(self, text: str, on_event=None) -> str:
        # ★ R32：第二个 prompt 必须被明确拒绝。静默接受会覆盖 _on_event/_busy，
        #   而且先结束的那个会在 finally 里把后一个的状态清掉（事件串台、busy 撒谎）。
        if self._busy:
            raise BridgeBusy(BUSY_ERR)
        self._busy = True                 # 先占坑：这里到下一个 await 之间不会被插队
        try:
            sid = await self.ensure_session()
            self._on_event = on_event
            res = await self.client.request("session/prompt", {
                "sessionId": sid, "prompt": [{"type": "text", "text": str(text or "")}]}, timeout=1800)
            return str((res or {}).get("stopReason") or "")
        finally:
            self._on_event = None
            self._busy = False

    async def cancel(self):
        if not self.acp_session_id:
            return                        # 还没建会话 = 没有可取消的东西，别发空 sessionId 出去
        try:
            await self.client.notify("session/cancel", {"sessionId": self.acp_session_id})
        except Exception as e:
            self.log("[ACP] cancel 失败: %r" % (e,))

    # ---------- 事件与审批 ----------
    async def _on_notification(self, frame):
        if frame.get("method") != "session/update":
            return
        ev = map_update(frame.get("params") or {}, self.reg)
        if ev and self._on_event:
            try:
                await self._on_event(ev)
            except Exception:
                pass

    async def _on_request(self, frame):
        """session/request_permission → 项目既有审批流 → allow-once / reject-once。"""
        if frame.get("method") != "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        params = frame.get("params") or {}
        tc = params.get("toolCall") or {}
        tid = str(tc.get("toolCallId") or "")
        info = self.reg.get(tid)          # ★ 审批包不带 rawInput，必须回查
        args = info.get("rawInput") or {}
        tool = info.get("tool") or "越界操作"
        summary = _approval_summary(tool, args)
        ap = _approval.create_approval(self.session_id, self.character_id, tool, summary,
                                      {"path": str(args.get("file_path") or args.get("path") or ""),
                                       "command": str(args.get("command") or ""),
                                       "content": str(args.get("content") or "")[:500],
                                       # ★ R29：她申请升档时带的理由与目标档位 —— 审批卡要能显示
                                       "sandbox_permissions": str(args.get("sandbox_permissions") or ""),
                                       "justification": str(args.get("justification") or "")})
        aid = str(ap.get("id") or "")
        self._approvals.add(aid)
        if self._on_event:
            try:
                await self._on_event({"type": "agent_approval", "approval": _approval.get_approval(aid)})
            except Exception:
                pass
        # 正常结算就把它从「待收尾」表里去掉；被取消（CancelledError）时留着，交给 stop() 收尾
        status = await _approval.wait_for_approval(aid)
        self._approvals.discard(aid)
        allow = (status == "approved")
        return {"outcome": {"outcome": "selected",
                            "optionId": _pick_option(params, "allow_once" if allow else "reject_once",
                                                     "allow-once" if allow else "reject-once")}}


def _approval_summary(tool: str, args: dict) -> str:
    try:
        a = args or {}
        p = str(a.get("file_path") or a.get("path") or "").strip()
        if p:
            return "要在工作区外动文件：%s" % p
        if a.get("command"):
            return "要跑一条越界命令：%s" % str(a["command"])[:80]
        return "要执行一个越界操作：%s" % (tool or "未知")
    except Exception:
        return "要执行一个越界操作"


async def get_bridge(cwd: str, permission_mode: str = "workspace-write", *, patch: str = "",
                     session_id: str = "default", character_id: str = "default", log=print) -> AcpBridge:
    """按「工作目录 + 权限档 + 伴侣会话」取桥 —— 一条桥只服务一个伴侣会话（R32）。

    同一个 cwd 的不同伴侣会话必须各拿一条桥：否则两个窗口共用同一条 harness 会话，
    事件互串、审批卡会归属到最后那个调用者。
    """
    key = _key(cwd, permission_mode, session_id)
    async with _LOCK:
        br = _BRIDGES.get(key)
        if br and br.alive:
            br.session_id, br.character_id = session_id, character_id
            return br
        br = AcpBridge(cwd, patch=patch, permission_mode=permission_mode, log=log,
                       session_id=session_id, character_id=character_id)
        await br.start()
        _BRIDGES[key] = br
        return br


async def stop_all():
    for key, br in list(_BRIDGES.items()):
        try:
            await br.stop()
        except Exception:
            pass
        _BRIDGES.pop(key, None)


async def stop_bridges(permission_mode: str = "") -> int:
    """只关**指定权限档**的桥（permission_mode 空串 = 全部，等价于 stop_all 的范围）。

    ★ 收尾修复：`set_upgrade(False)`（前端「改回项目级」）从前调的是 `stop_all()` ——
    那会把**所有会话**的桥一起停：别人正在飞的 `session/prompt` 当场以
    `RuntimeError: client stopped` 结束，任务落 failed，`_finish` 又把 agent_error 推进用户聊天
    （别人的待批审批也被一并判拒）。收回升档只该收回「放开的那条桥」，而桥的键里本来就带权限档
    （见 _key：cwd|permission_mode|session_id），按档筛就够了。
    `stop_all()` 的语义（关掉全部）保持原样，其它调用点不受影响。

    返回真正关掉的桥数。
    """
    n = 0
    for key, br in list(_BRIDGES.items()):
        if permission_mode and str(getattr(br, "permission_mode", "") or "") != str(permission_mode):
            continue
        try:
            await br.stop()
            n += 1
        except Exception:
            pass
        _BRIDGES.pop(key, None)
    return n


def widened_busy(session_id: str = "default") -> bool:
    """本会话有没有活正跑在「放开档」的桥上（`set_upgrade(False)` 的拒绝判据）。

    判据取桥自己的 `busy`（AcpBridge.prompt 在飞时为真）而不是任务表：任务是不是真在跑，
    桥最清楚；任务表里的 running 还包含"等重试"这类并没有 prompt 在飞的窗口。
    """
    sid = str(session_id or "default")
    for br in list(_BRIDGES.values()):
        try:
            if str(getattr(br, "permission_mode", "") or "") != "danger-full-access":
                continue
            if str(getattr(br, "session_id", "") or "default") != sid:
                continue
            if getattr(br, "busy", False):
                return True
        except Exception:
            continue
    return False
