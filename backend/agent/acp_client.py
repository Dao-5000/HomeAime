# -*- coding: utf-8 -*-
"""ACP stdio 客户端 —— 起子进程 + 换行分帧 JSON-RPC 收发。

设计要点：
  · 一律 asyncio 子进程 + 管道；Windows 上默认 Proactor 事件循环即可
  · 三个分发口：response 结算挂起请求；notification 进 on_notification；
    服务端请求（如 session/request_permission）进 on_request 并把返回值回填
★ R23（Task 4 实现者发现并如实上报）：本模块**不是**"任何异常都不外抛"——
  `request()` 在超时时**故意抛 `asyncio.TimeoutError`**（调用方需要区分"超时"与"返回空"），
  `_send()` 在进程已死时抛 `RuntimeError`，挂起请求在进程断开时以 `RuntimeError` 结算。
  真正"绝不外抛"的只有三个分发回调内部的异常（各自吞掉并打日志）。
★ R24（Task 4 审查发现）：服务端请求在**独立任务**里跑（见 `_handle_request`），
  所以请求处理器内部可以放心 `await client.request()`（读循环没被它占住）。
  **但通知处理器不行** —— 通知是在读循环里内联 await 的（为了保住轨迹顺序），
  在里面 `await client.request()` 会死锁：响应只有这个循环会读，循环却正等着它返回。
"""
import asyncio
import json
import time

from .acp_codec import classify, decode, encode


class AcpClient:
    def __init__(self, argv, cwd, env=None, on_notification=None, on_request=None, log=print):
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self.on_notification = on_notification
        self.on_request = on_request
        self.log = log
        self.proc = None
        self._next_id = 0
        self._pending = {}
        self._tasks = []
        self._closed = False

    # ---------- 生命周期 ----------
    async def start(self):
        import os
        env = dict(os.environ)
        if self.env:
            env.update({str(k): str(v) for k, v in self.env.items()})
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks.append(asyncio.create_task(self._read_stdout()))
        self._tasks.append(asyncio.create_task(self._read_stderr()))

    @property
    def alive(self) -> bool:
        return bool(self.proc) and self.proc.returncode is None

    async def stop(self, timeout: float = 15.0):
        self._closed = True
        try:
            if self.alive:
                try:
                    self.proc.stdin.write(encode({"jsonrpc": "2.0", "method": "shutdown", "params": {}}))
                    await self.proc.stdin.drain()
                except Exception:
                    pass
                try:
                    self.proc.stdin.close()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=timeout)
                except Exception:
                    self.proc.kill()
                    try:
                        await self.proc.wait()
                    except Exception:
                        pass
        finally:
            self._tasks = [t for t in self._tasks if not t.done()]   # 丢掉已完成/已回收的任务
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                try:
                    # ★ R24 配套：取消后必须等它们真的结束，否则解释器退出时会报
                    #   "Task was destroyed but it is pending!"。return_exceptions 保证
                    #   被取消任务的 CancelledError 不会从这里冒出去。
                    await asyncio.gather(*self._tasks, return_exceptions=True)
                except Exception:
                    pass
            self._tasks = []
            self._fail_pending("client stopped")

    def _fail_pending(self, why: str):
        for _rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError(why))
        self._pending.clear()

    # ---------- 读循环 ----------
    async def _read_stdout(self):
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                frame = decode(line)
                if frame is None:
                    continue
                await self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.log("[ACP] 读 stdout 异常: %r" % (e,))
        finally:
            self._fail_pending("ACP 进程已断开")

    async def _read_stderr(self):
        try:
            while True:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                txt = line.decode("utf-8", "replace").rstrip()
                if txt:
                    self.log("[ACP stderr] " + txt[:400])
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _dispatch(self, frame: dict):
        kind = classify(frame)
        if kind == "response":
            fut = self._pending.pop(frame.get("id"), None)
            if fut and not fut.done():
                fut.set_result(frame)
            return
        if kind == "notification":
            if self.on_notification:
                try:
                    await self.on_notification(frame)
                except Exception as e:
                    self.log("[ACP] 通知回调异常: %r" % (e,))
            return
        if kind == "request":
            # ★ R24（Task 4 审查发现）：服务端请求**必须另起任务**处理 ——
            # 审批是"人等人的"（可能几十秒），若在唯一的读循环里 await，
            # 期间所有 session/update 都被堵住 → 轨迹/状态条全冻结。
            # 另起任务后读循环继续收帧；且请求处理器里再调 client.request() 也不会死锁。
            self._spawn(self._handle_request(frame))
            return

    def _spawn(self, coro):
        """起一个后台任务（服务端请求处理），做完就自我回收，避免 _tasks 无限增长。"""
        t = asyncio.create_task(coro)
        self._tasks.append(t)
        t.add_done_callback(self._reap)
        return t

    def _reap(self, task):
        try:
            self._tasks.remove(task)
        except ValueError:
            pass

    async def _handle_request(self, frame: dict):
        """★ R24：独立任务里跑 on_request，再把它返回的结果原样回填。"""
        result = None
        if self.on_request:
            try:
                result = await self.on_request(frame)
            except Exception as e:
                self.log("[ACP] 服务端请求处理异常: %r" % (e,))
                result = None
        if result is None:
            result = {"outcome": {"outcome": "cancelled"}}
        await self._send({"jsonrpc": "2.0", "id": frame.get("id"), "result": result})

    # ---------- 发送 ----------
    async def _send(self, obj: dict):
        if not self.alive:
            raise RuntimeError("ACP 进程已退出")
        self.proc.stdin.write(encode(obj))
        await self.proc.stdin.drain()

    async def notify(self, method: str, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def request(self, method: str, params=None, timeout: float = 240) -> dict | None:
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        t0 = time.time()
        try:
            await self._send(msg)          # ★ R25：发送本身也可能抛（进程已死 → RuntimeError），必须在 try 内
            frame = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)   # ★ R25：超时 / 调用方取消 / 发送失败 都不留悬挂条目
        dt = time.time() - t0
        if "error" in frame:
            self.log("[ACP] %s 错误 (%.1fs): %s" % (method, dt, json.dumps(frame["error"], ensure_ascii=False)[:300]))
            return None
        return frame.get("result")
