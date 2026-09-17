# -*- coding: utf-8 -*-
"""能力探测：本机有没有能驱动 DSH 的「手」。

设计约束（来自探针实测，见 UI改版方案/Agent模式窗口-设计方案-2026-09-15.md §3.2）：
  · 必须用真 CLI：node + @deepseek-ai/dsh/lib/bin.js
  · DSH Desktop 的 dsh.cmd（Electron-as-node）会让 pwsh 工具静默失效 —— 判为不可用
  · 不写死路径：环境变量 > DSH_HOME > ~/.dsh
  · 结果缓存 60s，避免每次进窗口都扫盘
"""
import os
import shutil
import time

_CACHE = {"t": 0.0, "v": None}
_TTL = 60.0
_CLI_REL = os.path.join("profiles", "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js")


def _dsh_home() -> str:
    home = str(os.environ.get("DSH_HOME") or "").strip()
    if home:
        return home
    return os.path.join(os.path.expanduser("~"), ".dsh")


def _candidate_cli():
    """返回 (cli, reject_reason)。

    ★ R10（Task 1 审查发现）：`HOMEAIME_DSH_CLI` 只接受 **.js 入口**。
    .cmd/.bat/.exe/.ps1 是 DSH Desktop 的 Electron 外壳 —— 实测它会让 pwsh 工具静默失效，
    所以 override 不允许把它走私进来（否则全局约束"必须用真 CLI"就被绕过了）。
    """
    env = str(os.environ.get("HOMEAIME_DSH_CLI") or "").strip()
    if env:
        if not os.path.isfile(env):
            return "", ""
        if not env.lower().endswith(".js"):
            return "", ("你指定的 HOMEAIME_DSH_CLI 不是 .js 入口（%s）：.cmd/.exe 是 DSH Desktop 的 "
                        "Electron 外壳，会让命令工具静默失效，所以不能用。请指向 dsh 的 lib/bin.js。" % env)
        return env, ""
    p = os.path.join(_dsh_home(), _CLI_REL)
    return (p, "") if os.path.isfile(p) else ("", "")


def probe(force: bool = False) -> dict:
    """返回 {"ok", "node", "cli", "reason"}；ok=False 时 reason 可直接显示给用户。"""
    now = time.time()
    if not force and _CACHE["v"] and (now - _CACHE["t"]) < _TTL:
        return dict(_CACHE["v"])          # ★ 返回副本，避免调用方改到缓存
    node = shutil.which("node") or ""
    cli, reject = _candidate_cli()
    if not node:
        v = {"ok": False, "node": "", "cli": cli,
             "reason": "没找到 node —— 我的手要用 node 跑起来，先装 Node.js 再回来。"}
    elif reject:
        v = {"ok": False, "node": node, "cli": "", "reason": reject}
    elif not cli:
        v = {"ok": False, "node": node, "cli": "",
             "reason": ("没找到 DSH 的命令行入口（找的是 <DSH_HOME>/profiles/node_modules/"
                        "@deepseek-ai/dsh/lib/bin.js）—— 先装/启动 DSH Desktop 再回来。")}
    else:
        v = {"ok": True, "node": node, "cli": cli, "reason": ""}
    _CACHE["t"], _CACHE["v"] = now, v
    return dict(v)
