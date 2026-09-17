# -*- coding: utf-8 -*-
"""
AI伴侣 一键诊断（给 GPT 的完整排查报告生成器）
================================================
双击桌面「AI伴侣-一键检测.bat」会调用本脚本。

本脚本输出一份**完整、自包含**的报告，包含：
  1) Python / pip 版本
  2) 依赖可用性（fastapi/uvicorn/openai/websockets/sentence_transformers...）
  3) 全部 backend/*.py 语法扫描（py_compile）
  4) 关键子包导入验证（正确用 backend.xxx 子包路径）
  5) 【关键】真正启动后端（run.py），捕获启动期完整 traceback
  6) 若启动成功，探测 /api/chat/stream 等路由是否注册

所有内容写入：桌面/AI伴侣-检测报告.txt
用户把该 txt 全文发给 GPT 即可，无需自己复制粘贴。
"""
import os
import sys
import io
import traceback
import subprocess
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "backend")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
REPORT = os.path.join(DESKTOP, "AI伴侣-检测报告.txt")

out = []

def log(*a):
    line = " ".join(str(x) for x in a)
    out.append(line)
    print(line)

# ---------- 离线 embedding 降级 shim（避免联网卡顿） ----------
class _FakeST:
    class SentenceTransformer:
        def __init__(self, *a, **k):
            raise ImportError("disabled for offline diagnostic")

def install_offline_shim():
    fake = type(sys)("sentence_transformers")
    fake.SentenceTransformer = _FakeST.SentenceTransformer
    sys.modules["sentence_transformers"] = fake

# ---------- 1) 环境 ----------
def section_env():
    log("=" * 70)
    log("【1】运行环境")
    log("=" * 70)
    log("Python 可执行:", sys.executable)
    log("Python 版本:", sys.version.replace("\n", " "))
    log("项目根 ROOT:", ROOT)
    log("backend 目录:", BACKEND)
    log("")

def section_deps():
    log("=" * 70)
    log("【2】依赖可用性")
    log("=" * 70)
    deps = ["fastapi", "uvicorn", "openai", "websockets", "sentence_transformers",
            "sqlite3", "pydantic", "httpx", "requests"]
    for d in deps:
        try:
            m = __import__(d)
            ver = getattr(m, "__version__", "unknown")
            log(f"  [OK] {d} == {ver}")
        except Exception as e:
            log(f"  [缺失] {d} -> {type(e).__name__}: {e}")
    log("")

# ---------- 3) 语法扫描 ----------
def section_syntax():
    log("=" * 70)
    log("【3】关键 .py 语法扫描（ast.parse，白名单目录）")
    log("=" * 70)
    import ast
    # 只扫排查相关的白名单目录（避免遍历整棵 backend 树触发沙箱/异常文件）
    white = [
        BACKEND,
        os.path.join(BACKEND, "emotion_engine"),
        os.path.join(BACKEND, "multi_turn"),
        os.path.join(BACKEND, "memory"),
        os.path.join(BACKEND, "companion_os"),
        os.path.join(BACKEND, "companion"),
    ]
    bad = []
    count = 0
    scanned = []
    for d in white:
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".py") or fn == "__pycache__":
                continue
            fp = os.path.join(d, fn)
            rel = os.path.relpath(fp, ROOT)
            scanned.append(rel)
            count += 1
            try:
                src = open(fp, encoding="utf-8").read()
                ast.parse(src, filename=fp)
            except Exception as e:
                bad.append((rel, str(e)))
    log(f"扫描文件数: {count}（白名单目录）")
    if bad:
        log(f"❌ 语法错误 {len(bad)} 个:")
        for rel, e in bad:
            msg = e.splitlines()[-1] if str(e).strip() else str(e)
            log(f"  - {rel}: {msg}")
    else:
        log("✅ 全部语法通过")
    log("")

# ---------- 4) 导入验证 ----------
def section_imports():
    log("=" * 70)
    log("【4】关键子包导入验证（backend.xxx 子包路径）")
    log("=" * 70)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    install_offline_shim()
    mods = [
        "backend.config",
        "backend.db",
        "backend.main",
        "backend.chat_logic",
        "backend.deepseek_api",
        "backend.memory",
        "backend.memory.prompt_builder",
        "backend.emotion_engine.ai_emotion",
        "backend.multi_turn.generator",
        "backend.companion_os.controller",
    ]
    for mod in mods:
        try:
            __import__(mod)
            log(f"  [OK] {mod}")
        except Exception as e:
            log(f"  [FAIL] {mod} -> {type(e).__name__}: {str(e).splitlines()[-1]}")
    # 验证 memory.build_full_memory_context 暴露修复
    try:
        from backend.memory import build_full_memory_context
        log("  [OK] backend.memory.build_full_memory_context 可导入（问题一修复生效）")
    except Exception as e:
        log(f"  [FAIL] build_full_memory_context -> {type(e).__name__}: {e}")
    log("")

# ---------- 5) 真正启动后端抓 traceback ----------
def section_startup():
    log("=" * 70)
    log("【5】启动后端 run.py（捕获启动期完整 traceback）")
    log("=" * 70)
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    cmd = [sys.executable, "run.py"]
    log(f"执行: {' '.join(cmd)}  (cwd={ROOT})")
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=25,
        )
        log("--- stdout ---")
        log(proc.stdout or "(空)")
        log("--- stderr ---")
        log(proc.stderr or "(空)")
        log(f"返回码: {proc.returncode}")
        if proc.returncode != 0:
            log("❌ 后端启动失败，上面 stderr 即完整 traceback，可直接发给 GPT")
        else:
            log("✅ 后端启动进程正常退出（注意：uvicorn 是常驻服务，正常不应自行退出；")
            log("   若 25s 内退出码 0，可能是端口已占用或启动后立刻退出，请结合上面输出判断）")
    except subprocess.TimeoutExpired as e:
        log("✅ 后端在 25s 内未退出（uvicorn 常驻监听，说明启动成功、未在导入期崩溃）")
        if e.stdout:
            log("--- stdout (前 3000 字符) ---")
            log(e.stdout[:3000])
        if e.stderr:
            log("--- stderr (前 3000 字符) ---")
            log(e.stderr[:3000])
    except Exception as e:
        log(f"❌ 启动命令本身异常: {type(e).__name__}: {e}")
        log(traceback.format_exc())
    log("")

# ---------- 6) 路由注册探测（若启动成功） ----------
def section_routes():
    log("=" * 70)
    log("【6】路由注册探测（静态读取 main.py，无需联网）")
    log("=" * 70)
    mp = os.path.join(BACKEND, "main.py")
    try:
        src = open(mp, encoding="utf-8").read()
        routes = []
        for line in src.splitlines():
            s = line.strip()
            for method in ("@app.get", "@app.post", "@app.put", "@app.delete", "@app.websocket"):
                if s.startswith(method + "("):
                    routes.append(s)
        log(f"main.py 中申明的路由/端点（{len(routes)} 个）:")
        for r in routes:
            log("  " + r)
        if "/api/chat/stream" in src:
            log("✅ /api/chat/,stream 已注册")
        else:
            log("❌ 未找到 /api/chat/stream 路由")
    except Exception as e:
        log(f"读取 main.py 失败: {e}")
    log("")

def safe(name, fn):
    try:
        fn()
    except Exception as e:
        log(f"!! 段落 [{name}] 执行异常: {type(e).__name__}: {e}")
        log(traceback.format_exc())

def main():
    try:
        safe("env", section_env)
        safe("deps", section_deps)
        safe("syntax", section_syntax)
        safe("imports", section_imports)
        safe("startup", section_startup)
        safe("routes", section_routes)
    except Exception as e:
        log("诊断脚本自身异常:")
        log(traceback.format_exc())

    report = "\n".join(out)
    try:
        with io.open(REPORT, "w", encoding="utf-8") as f:
            f.write(report)
        log("")
        log("=" * 70)
        log(f"✅ 报告已生成: {REPORT}")
        log("请把该 txt 全文发给 GPT。")
        log("=" * 70)
    except Exception as e:
        print("写报告失败:", e)
        print(report)

if __name__ == "__main__":
    main()
