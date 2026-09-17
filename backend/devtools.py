# -*- coding: utf-8 -*-
"""
开发者工具（助手改代码的"手"）：
  让助手在聊天里像开发搭档一样：查代码 → 改代码 → 快照 → 重启验证 → 汇报。
  由 MCP 工具层（main.py /mcp）暴露给模型调用。

安全设计（★每一层都不可绕过）：
  1. 路径白名单：只允许操作项目目录内；.git / 数据目录 / 配置（含 key）/ venv 绝对禁改
  2. 写前快照：每次 write 之前自动 git add+commit（改坏了随时可回滚）
  3. 大小限制：单次写入 ≤200KB；读取 ≤400KB
  4. 执行命令白名单：只允许 git/py_compile/node --check/npm run build:backend 等安全命令
  5. 重启走受控接口（不暴露任意进程操作）
"""
import os
import re
import subprocess
import sys
import threading

# ★ 源码根目录（2026-09-10）：打包版里本模块的 __file__ 指向 _MEI 解压临时目录，
#   不能用它算项目根——frozen 时源码根取环境变量 HOMEAIME_SOURCE_ROOT 或默认开发路径；
#   开发模式取 backend/..。
from . import config as _config

if getattr(sys, "frozen", False):
    ROOT = os.path.abspath(os.environ.get(
        "HOMEAIME_SOURCE_ROOT",
        r"%~dp0"))
else:
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 绝对禁区（路径含这些成分一律拒绝）
_FORBIDDEN = (
    ".git", "venv", "node_modules", "__pycache__",
    "data",          # 运行数据目录（含 db/config/key）
    "dist", "build", "release", "release2", "release_new",
    "config.json", ".env", "*.db", "*.exe", "*.log",
    "表情包", "朋友圈", "模板", "记忆库", "tts_cache", "screen_captures",
)

# 允许的命令前缀（白名单，逐前缀匹配）
_ALLOWED_CMD = (
    "git add", "git commit", "git diff", "git log", "git status", "git show",
    "git checkout -- ", "git restore ",
    r"backend\venv\Scripts\python.exe -m py_compile",
    "backend/venv/Scripts/python.exe -m py_compile",
    "python -m py_compile",
    "node --check",
    "npm run build:backend",
)


def _safe_path(rel_path: str) -> str:
    """相对路径 → 项目内绝对路径；越界/禁区抛 ValueError。"""
    rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
    if not rel:
        raise ValueError("路径为空")
    if any(seg in rel for seg in (".git", "venv", "node_modules", "__pycache__")):
        raise ValueError(f"禁止访问的路径成分: {rel}")
    if rel.startswith(("data/", "dist/", "build/", "release")) or "/data/" in rel:
        raise ValueError(f"禁止访问数据/构建目录: {rel}")
    if re.search(r"config\.json$|\.env$|\.db$|\.exe$|\.log$|\.sqlite", rel, re.I):
        raise ValueError(f"禁止访问配置/二进制/日志文件: {rel}")
    full = os.path.normpath(os.path.join(ROOT, rel))
    if not full.startswith(os.path.normpath(ROOT)):
        raise ValueError("路径越界（项目目录之外）")
    return full


def _git(args: str) -> str:
    """在项目根目录跑 git 命令，返回 stdout（失败抛异常）。"""
    r = subprocess.run(f'git {args}', shell=True, cwd=ROOT,
                       capture_output=True, timeout=60, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {args} 失败: {r.stderr[:200]}")
    return (r.stdout or "").strip()


_lock = threading.Lock()


def git_snapshot(label: str = "", rel_path: str = "") -> str:
    """写前快照：提交当前改动。rel_path 提供时只提交该文件（避免 add -A 卷入
    其他进程/线程正在写的文件造成竞态）。返回快照 hash 或说明。"""
    with _lock:
        try:
            if rel_path:
                _git(f'add "{rel_path}"')
            else:
                _git("add -A")
            msg = f"快照（改代码前）: {label}"[:200] if label else "快照（改代码前）"
            r = subprocess.run(
                f'git -c user.name="homeaime" -c user.email="dev@local" commit -m "{msg}"',
                shell=True, cwd=ROOT, capture_output=True, timeout=60,
                encoding="utf-8", errors="replace")
            out = (r.stdout or "")
            if "nothing to commit" in out:
                return "无改动，无需快照"
            import re as _re
            m = _re.search(r"\[[\w/-]+ ([0-9a-f]{7,})\]", out)
            return ("快照 " + m.group(1)) if m else "已提交"
        except Exception as e:
            return f"快照失败(不阻断): {e}"


def git_diff_uncommitted() -> str:
    try:
        return _git("diff HEAD --stat") or "（无未提交改动）"
    except Exception as e:
        return f"diff 失败: {e}"


def read_file(rel_path: str, max_bytes: int = 400 * 1024) -> str:
    """读项目内文件文本（限 400KB）。"""
    fp = _safe_path(rel_path)
    if not os.path.isfile(fp):
        raise FileNotFoundError(f"文件不存在: {rel_path}")
    size = os.path.getsize(fp)
    if size > max_bytes:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(max_bytes // 2)
        return f"（文件 {size//1024}KB 过大，只返回前半部分）\n" + head
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def write_file(rel_path: str, content: str, snapshot_label: str = "") -> str:
    """写项目内文件（自动先 git 快照）。返回快照说明。"""
    fp = _safe_path(rel_path)
    content = str(content or "")
    if len(content.encode("utf-8")) > 200 * 1024:
        raise ValueError("单次写入超过 200KB，请拆分")
    snap = git_snapshot(snapshot_label or f"写 {rel_path} 之前")
    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return f"{snap}；已写入 {rel_path}（{len(content)} 字符）"


def edit_file(rel_path: str, old: str, new: str, snapshot_label: str = "") -> str:
    """精确替换（old 必须唯一命中，同 Edit 工具语义）。自动先 git 快照。"""
    fp = _safe_path(rel_path)
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()
    old_s = str(old or "")
    cnt = src.count(old_s)
    if cnt == 0:
        raise ValueError("old 内容在文件中未找到（可能已改过或匹配不上）")
    if cnt > 1:
        raise ValueError(f"old 内容命中 {cnt} 处，不唯一——请扩大上下文再替换")
    snap = git_snapshot(snapshot_label or f"改 {rel_path} 之前")
    with open(fp, "w", encoding="utf-8", newline="\n") as f:
        f.write(src.replace(old_s, str(new or "")))
    return f"{snap}；已修改 {rel_path}"


def search_in_code(pattern: str, glob_hint: str = "*.py") -> str:
    """在项目源码里正则搜索（限 .py/.js/.md，跳过禁区目录）。返回 行号: 内容 列表。"""
    pat = re.compile(str(pattern or ""), re.I)
    if not pattern:
        raise ValueError("搜索词为空")
    # "*,*.py" 之类的写法统一转成后缀集合（endswith 匹配）
    exts = tuple(
        ("." + x.strip().lstrip("*").lstrip(".")) if x.strip() else ".py"
        for x in str(glob_hint or "*.py").split(",")
    )
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        if any(seg in rel_dir for seg in (".git", "venv", "node_modules", "__pycache__",
                                          "data", "dist", "build", "release", "表情包",
                                          "朋友圈", "模板", "记忆库", "tts_cache")):
            dirnames[:] = []
            continue
        for fn in filenames:
            if not fn.endswith(exts):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > 1024 * 1024:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if pat.search(line):
                            rel = os.path.relpath(fp, ROOT).replace("\\", "/")
                            hits.append(f"{rel}:{i}: {line.strip()[:120]}")
                            if len(hits) >= 40:
                                return "\n".join(hits)
            except Exception:
                continue
    return "\n".join(hits) if hits else "（无匹配）"


def run_command(command: str, timeout: int = 120) -> str:
    """执行白名单内的命令（编译检查/git/npm build:backend 等）。"""
    cmd = str(command or "").strip()
    if not any(cmd.startswith(pre) or cmd == pre.strip() for pre in _ALLOWED_CMD):
        raise ValueError(f"命令不在白名单内：只允许 git/py_compile/node --check/npm run build:backend")
    r = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True,
                       timeout=min(max(int(timeout or 120), 10), 600),
                       encoding="utf-8", errors="replace")
    out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr.strip() else "")).strip()
    return f"exit={r.returncode}\n{out[-4000:]}"


def restart_hint() -> str:
    """返回重启说明（不直接执行重启——由用户/上层决定）。"""
    return ("代码改完后生效方式：后端 .py 改动需重启 HomeAime（exe 重新打包或热替换）；"
            "前端 public/js 改动刷新页面即可（强刷 Ctrl+F5）。"
            "npm run build:backend 已在白名单里，可以自己跑。")
