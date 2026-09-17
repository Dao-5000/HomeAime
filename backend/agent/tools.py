# -*- coding: utf-8 -*-
"""
工具库 —— Agent 的"手脚"。

工具分两类：
  · 安全工具：只读、无副作用（搜索、查天气、读文件、看屏幕），直接执行；
  · 危险工具：有副作用（写文件、执行命令），执行前必须由 loop.py 走授权（approval.py）。

设计约束：
  · 每个工具 = { name, description, params, dangerous, run }
  · run(args, ctx) -> {"ok": bool, "result": str, ...}，任何异常都不外抛，返回 ok=False；
  · 免费接口（DuckDuckGo / wttr.in）零额外 Key；
  · 文件读写一律限制在 workspace 目录内，不碰用户其它文件。
"""
import asyncio
import os
import re
import subprocess
from pathlib import Path

from .. import config as _config

# 安全工作区：Agent 只能读写这个目录（用户可后续扩权）
WORKSPACE = Path(getattr(_config, "DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "workspace"


def _ensure_workspace():
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _safe_path(rel: str) -> Path:
    """把用户/Agent 给的路径规整到 workspace 内，禁止 ../ 越界。"""
    p = Path(str(rel or "").strip().replace("\\", "/"))
    if p.is_absolute():
        p = Path(*p.parts[1:])  # 去掉盘符/根，按相对处理
    resolved = (WORKSPACE / p).resolve()
    if not str(resolved).startswith(str(WORKSPACE.resolve())):
        raise ValueError("路径越界，只允许在 workspace 内读写")
    return resolved


# ══════════════════════════════════════════════════
# 工具实现
# ══════════════════════════════════════════════════

async def _web_search(args: dict, ctx: dict) -> dict:
    """DuckDuckGo HTML 免费接口，返回前几条结果（标题 + 链接 + 摘要）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "result": "缺少搜索关键词"}
    try:
        import httpx
        url = "https://html.duckduckgo.com/html/"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.post(url, data={"q": query},
                             headers={"User-Agent": "Mozilla/5.0"})
        html = r.text
        # 结果块：<a class="result__a" href="...">标题</a>  + 摘要
        links = re.findall(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
        snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
        out = []
        for i, (href, title) in enumerate(links[:5]):
            title = re.sub(r"<[^>]+>", "", title).strip()
            snip = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
            if not title:
                continue
            # 去掉 ddg 的跳转前缀，还原真实 url
            m = re.search(r"uddg=([^&]+)", href)
            real = m.group(1) if m else href
            try:
                from urllib.parse import unquote
                real = unquote(real)
            except Exception:
                pass
            out.append(f"{i + 1}. {title}\n   {real}\n   {snip}")
        if not out:
            return {"ok": True, "result": f"没有搜到「{query}」的相关结果"}
        return {"ok": True, "result": "\n\n".join(out)}
    except Exception as e:
        return {"ok": False, "result": f"搜索失败：{type(e).__name__}: {e}"}


async def _get_weather(args: dict, ctx: dict) -> dict:
    """wttr.in 免费天气接口。"""
    city = str(args.get("city") or "").strip()
    if not city:
        return {"ok": False, "result": "缺少城市名"}
    try:
        import httpx
        url = f"https://wttr.in/{city}?format=j1&lang=zh"
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
        data = r.json()
        cur = (data.get("current_condition") or [{}])[0]
        area = (data.get("nearest_area") or [{}])[0]
        area_name = ((area.get("areaName") or [{}])[0] or {}).get("value", city)
        desc = (cur.get("lang_zh") or [{}])[0].get("value") if cur.get("lang_zh") else cur.get("weatherDesc", [{}])[0].get("value", "")
        temp = cur.get("temp_C", "")
        feels = cur.get("FeelsLikeC", "")
        hum = cur.get("humidity", "")
        wind = cur.get("windspeedKmph", "")
        return {
            "ok": True,
            "result": (
                f"{area_name} 现在：{desc}，气温 {temp}°C（体感 {feels}°C），"
                f"湿度 {hum}%，风速 {wind} km/h"
            ),
        }
    except Exception as e:
        return {"ok": False, "result": f"查天气失败：{type(e).__name__}: {e}"}


async def _file_read(args: dict, ctx: dict) -> dict:
    """读 workspace 内的文件（只读，安全）。"""
    try:
        p = _safe_path(args.get("path"))
        if not p.exists():
            return {"ok": False, "result": f"文件不存在：{args.get('path')}"}
        if p.stat().st_size > 200 * 1024:
            return {"ok": False, "result": "文件太大（>200KB），无法整体读取"}
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "result": content}
    except Exception as e:
        return {"ok": False, "result": f"读文件失败：{type(e).__name__}: {e}"}


async def _file_write(args: dict, ctx: dict) -> dict:
    """写文件到 workspace（危险：有副作用，loop.py 会先走授权）。"""
    try:
        p = _safe_path(args.get("path"))
        content = str(args.get("content") or "")
        p.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再替换，避免写一半损坏
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        return {"ok": True, "result": f"已写入 {p.relative_to(WORKSPACE)}（{len(content)} 字）"}
    except Exception as e:
        return {"ok": False, "result": f"写文件失败：{type(e).__name__}: {e}"}


async def _read_source(args: dict, ctx: dict) -> dict:
    """读项目源码（只读，用于 AI 检查/分析自己的代码）。支持按行范围分段读取。
    ★ 2026-09-11：打包版 __file__ 指向 PyInstaller _MEI 解压临时目录（里面没有 .py 源码），
    原实现永远返回「文件不存在」——这就是"她有工具却看不到代码"的根因之一。
    现在改用 devtools.ROOT（HOMEAIME_SOURCE_ROOT > 打包默认开发路径），并复用其路径白名单。"""
    try:
        from .. import devtools as _dt
        rel = str(args.get("path") or "").strip().replace("\\", "/").lstrip("/")
        fp = None
        try:
            fp = Path(_dt._safe_path(rel))    # 相对源码根（走白名单；_safe_path 返回 str）
            if not fp.is_file():
                fp = None
        except Exception:
            fp = None
        if fp is None:
            try:
                fp = Path(_dt._safe_path("backend/" + rel))   # 兼容只给文件名：自动补 backend/
                if not fp.is_file():
                    fp = None
            except Exception:
                fp = None
        if fp is None:
            return {"ok": False, "result": f"文件不存在：{rel}（项目根或 backend/ 下都没找到；可用 search_code 先定位）"}
        text = fp.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)
        try:
            offset = max(0, int(args.get("offset") or 0))
        except Exception:
            offset = 0
        try:
            limit = max(1, min(int(args.get("limit") or 200), 300))
        except Exception:
            limit = 200
        offset = min(offset, max(0, total - 1))
        chunk = lines[offset:offset + limit]
        shown = "\n".join(f"{offset + i + 1}: {ln}" for i, ln in enumerate(chunk))
        header = f"文件 {rel}（共 {total} 行，显示第 {offset + 1}~{min(offset + limit, total)} 行）\n"
        return {"ok": True, "result": header + shown}
    except Exception as e:
        return {"ok": False, "result": f"读源码失败：{type(e).__name__}: {e}"}


async def _search_code(args: dict, ctx: dict) -> dict:
    """在项目源码里正则搜索（复用 devtools 白名单，跳过禁区目录）。"""
    try:
        from .. import devtools as _dt
        out = _dt.search_in_code(str(args.get("pattern") or ""),
                                 str(args.get("glob") or "*.py"))
        return {"ok": True, "result": str(out)[:4000] or "（无匹配）"}
    except Exception as e:
        return {"ok": False, "result": f"搜索失败：{type(e).__name__}: {e}"}


async def _dev_edit_file(args: dict, ctx: dict) -> dict:
    """精确替换项目源码片段（old 必须唯一命中；自动 git 快照；危险：需用户授权）。"""
    try:
        from .. import devtools as _dt
        out = _dt.edit_file(str(args.get("path") or ""), str(args.get("old") or ""),
                            str(args.get("new") or ""), str(args.get("label") or "改代码之前"))
        return {"ok": True, "result": out + "\n" + _dt.restart_hint()}
    except Exception as e:
        return {"ok": False, "result": f"改代码失败：{type(e).__name__}: {e}"}


async def _dev_write_file(args: dict, ctx: dict) -> dict:
    """整文件写入项目源码（自动 git 快照；危险：需用户授权）。"""
    try:
        from .. import devtools as _dt
        out = _dt.write_file(str(args.get("path") or ""), str(args.get("content") or ""),
                             str(args.get("label") or "写文件之前"))
        return {"ok": True, "result": out + "\n" + _dt.restart_hint()}
    except Exception as e:
        return {"ok": False, "result": f"写文件失败：{type(e).__name__}: {e}"}


async def _dev_run_command(args: dict, ctx: dict) -> dict:
    """执行白名单命令（git 只读 / py_compile / node --check / npm run build:backend）。"""
    try:
        from .. import devtools as _dt
        out = _dt.run_command(str(args.get("command") or ""), int(args.get("timeout") or 120))
        return {"ok": True, "result": str(out)[:4000]}
    except Exception as e:
        return {"ok": False, "result": f"命令执行失败：{type(e).__name__}: {e}"}


async def _dev_git_status(args: dict, ctx: dict) -> dict:
    """查看源码仓库状态与未提交改动（只读）。"""
    try:
        from .. import devtools as _dt
        return {"ok": True, "result": _dt.git_diff_uncommitted()}
    except Exception as e:
        return {"ok": False, "result": f"git 状态获取失败：{type(e).__name__}: {e}"}


async def _run_command(args: dict, ctx: dict) -> dict:
    """执行命令行（危险：有副作用，loop.py 会先走授权）。带 30s 超时 + 输出截断。"""
    command = str(args.get("command") or "").strip()
    if not command:
        return {"ok": False, "result": "缺少命令"}
    if len(command) > 2000:
        return {"ok": False, "result": "命令过长"}
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "result": "命令执行超时（>30s）已终止"}
        out = (stdout or b"").decode("utf-8", "replace").strip()
        err = (stderr or b"").decode("utf-8", "replace").strip()
        result = out or err or "(无输出)"
        if len(result) > 3000:
            result = result[:3000] + "\n…(已截断)"
        return {"ok": proc.returncode == 0, "result": result}
    except Exception as e:
        return {"ok": False, "result": f"命令执行失败：{type(e).__name__}: {e}"}


async def _control_action(args: dict, ctx: dict, atype: str) -> dict:
    """复用 control.py 的电脑控制动作（打开应用/网址/音量/媒体/通知/看屏幕/打字）。"""
    try:
        from .. import control
        action = {"type": atype}
        action.update(args)
        res = await control.execute(action, ctx.get("session_id", "default"), ctx.get("character_id", "default"))
        if res.get("ok"):
            return {"ok": True, "result": res.get("action") or res.get("info") or "已执行"}
        return {"ok": False, "result": res.get("error") or "执行失败"}
    except Exception as e:
        return {"ok": False, "result": f"控制电脑失败：{type(e).__name__}: {e}"}


from .self_modules import (
    _schedule_task, _cancel_task, _learn_rule, _forget_rule, _my_rules,
)


# ══════════════════════════════════════════════════
# 工具注册表
# ══════════════════════════════════════════════════
# params：给 LLM 看的参数说明（字段名 -> 说明）
TOOLS = {
    "web_search": {
        "name": "web_search",
        "description": "联网搜索，获取新闻、资料、事实等信息",
        "params": {"query": "搜索关键词"},
        "dangerous": False,
        "run": _web_search,
    },
    "get_weather": {
        "name": "get_weather",
        "description": "查询指定城市的实时天气",
        "params": {"city": "城市名，如 杭州、北京"},
        "dangerous": False,
        "run": _get_weather,
    },
    "file_read": {
        "name": "file_read",
        "description": "读取工作区内某个文件的内容",
        "params": {"path": "工作区内的相对文件路径"},
        "dangerous": False,
        "run": _file_read,
    },
    "file_write": {
        "name": "file_write",
        "description": "在工作区内写入/创建文件（会覆盖原内容）",
        "params": {"path": "工作区内的相对文件路径", "content": "要写入的完整内容"},
        "dangerous": True,
        "run": _file_write,
    },
    "read_source": {
        "name": "read_source",
        "description": "读取项目源码（backend 目录下的文件），用于检查/分析自己的代码",
        "params": {"path": "backend 下的相对路径，如 agent/loop.py", "offset": "起始行号（可选，默认0）", "limit": "读取行数（可选，默认200，最多300）"},
        "dangerous": False,
        "run": _read_source,
    },
    "search_code": {
        "name": "search_code",
        "description": "在整个项目源码里正则搜索（跳过禁区目录），返回 文件:行号: 内容，先用它定位要看的代码",
        "params": {"pattern": "正则或关键词", "glob": "文件后缀，默认 *.py，可 *.js,*.md"},
        "dangerous": False,
        "run": _search_code,
    },
    "dev_edit_file": {
        "name": "dev_edit_file",
        "description": "修改项目源码：把 old 精确替换为 new（old 必须在文件中唯一命中），自动先 git 快照，改坏可回滚",
        "params": {"path": "项目内相对路径，如 backend/onebot.py", "old": "要替换的原文片段", "new": "替换后的内容", "label": "快照说明（可空）"},
        "dangerous": True,
        "run": _dev_edit_file,
    },
    "dev_write_file": {
        "name": "dev_write_file",
        "description": "整文件写入项目源码（覆盖，自动先 git 快照）；小改动优先用 dev_edit_file",
        "params": {"path": "项目内相对路径", "content": "完整文件内容", "label": "快照说明（可空）"},
        "dangerous": True,
        "run": _dev_write_file,
    },
    "dev_run_command": {
        "name": "dev_run_command",
        "description": "执行白名单命令：git add/commit/diff/log/status、python -m py_compile、node --check、npm run build:backend",
        "params": {"command": "白名单内命令", "timeout": "超时秒数（可空）"},
        "dangerous": True,
        "run": _dev_run_command,
    },
    "dev_git_status": {
        "name": "dev_git_status",
        "description": "查看源码仓库未提交改动（改了哪些文件），只读",
        "params": {},
        "dangerous": False,
        "run": _dev_git_status,
    },
    "schedule_task": {
        "name": "schedule_task",
        "description": "给自己设任务：到点主动找 TA（推 App 和 QQ）。time 支持 每天23:00（循环约定，永久保留）/ 2026-09-12 08:00（一次性，兑现后自动清除）/ 明晚8点（相对表述）。答应 TA 一件到点要做的事后，应当立即用它把约定固化",
        "params": {"content": "到点要做什么", "time": "每天23:00 或 2026-09-12 08:00 或 明晚8点"},
        "dangerous": False,
        "run": _schedule_task,
    },
    "cancel_task": {
        "name": "cancel_task",
        "description": "取消自己设的任务（按 id 或标题关键词）",
        "params": {"id": "任务id（可选）", "keyword": "标题关键词（可选）"},
        "dangerous": False,
        "run": _cancel_task,
    },
    "learn_rule": {
        "name": "learn_rule",
        "description": "把 TA 教你的习惯/规则沉淀成自己的长期积木（今后每次对话都会自动带上，直到被删除）。TA 说「以后/从今天起/记住每次都要…」这类长期约定时使用",
        "params": {"rule": "规则内容（一句话）"},
        "dangerous": False,
        "run": _learn_rule,
    },
    "forget_rule": {
        "name": "forget_rule",
        "description": "删除某条已学会的积木规则（按 id 或关键词），用于规则过时或 TA 说不用了",
        "params": {"id": "积木id（可选）", "keyword": "关键词（可选）"},
        "dangerous": False,
        "run": _forget_rule,
    },
    "my_rules": {
        "name": "my_rules",
        "description": "查看自己当前的所有积木规则",
        "params": {},
        "dangerous": False,
        "run": _my_rules,
    },
    "run_command": {
        "name": "run_command",
        "description": "在命令行执行一条命令（用于安装、运行脚本等）",
        "params": {"command": "要执行的完整命令"},
        "dangerous": True,
        "run": _run_command,
    },
    "open_app": {
        "name": "open_app",
        "description": "打开某个应用（仅限白名单内的应用）",
        "params": {"app": "应用名，如 音乐/记事本/计算器/浏览器"},
        "dangerous": False,
        "run": lambda a, c: _control_action(a, c, "open_app"),
    },
    "open_url": {
        "name": "open_url",
        "description": "用浏览器打开一个网址",
        "params": {"url": "完整 http/https 网址"},
        "dangerous": False,
        "run": lambda a, c: _control_action(a, c, "url"),
    },
    "volume": {
        "name": "volume",
        "description": "调节音量（正数调大，负数调小）",
        "params": {"delta": "音量变化值，如 10 或 -10"},
        "dangerous": False,
        "run": lambda a, c: _control_action(a, c, "volume"),
    },
    "screen_info": {
        "name": "screen_info",
        "description": "查看当前前台窗口标题（你在看什么）",
        "params": {},
        "dangerous": False,
        "run": lambda a, c: _control_action(a, c, "screen_info"),
    },
    "notify": {
        "name": "notify",
        "description": "弹一个系统通知",
        "params": {"title": "通知标题", "body": "通知内容"},
        "dangerous": False,
        "run": lambda a, c: _control_action(a, c, "notify"),
    },
}

# ★ 别名：agent/__init__.py 与 loop.py 都用 TOOL_REGISTRY 这个名字，
#   但注册表实际叫 TOOLS。之前漏了这个别名导致
#   "cannot import name 'TOOL_REGISTRY'" → scheduler 的目标提醒静默失败。
TOOL_REGISTRY = TOOLS


def get_tool_specs() -> str:
    """生成给 LLM 看的工具清单（放进 system prompt，让模型知道有哪些工具可用）。"""
    lines = []
    for name, t in TOOLS.items():
        p = "、".join(f"{k}({v})" for k, v in (t.get("params") or {}).items())
        p = p or "无参数"
        danger = "（危险，需用户确认）" if t.get("dangerous") else ""
        lines.append(f"- {name}{danger}：{t['description']}；参数：{p}")
    return "\n".join(lines)


async def execute_tool(name: str, args: dict, ctx: dict) -> dict:
    """按名字执行工具。未知工具 / 异常一律返回 ok=False，绝不外抛。"""
    tool = TOOLS.get(name)
    if not tool:
        return {"ok": False, "result": f"未知工具：{name}"}
    try:
        _ensure_workspace()
        res = await tool["run"](args or {}, ctx or {})
        if not isinstance(res, dict):
            res = {"ok": True, "result": str(res)}
        res.setdefault("ok", True)
        return res
    except Exception as e:
        return {"ok": False, "result": f"工具 {name} 执行异常：{type(e).__name__}: {e}"}


def is_dangerous(name: str) -> bool:
    tool = TOOLS.get(name)
    return bool(tool and tool.get("dangerous"))
