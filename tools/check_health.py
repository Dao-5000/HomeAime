# -*- coding: utf-8 -*-
"""
桌面健康检测 / 依赖检查脚本
================================
项目结构：backend/ 是根目录下的子包，内部大量使用相对导入（from .. import config）。
因此**不能** `cd backend && python -c "from memory import ..."`（会 ImportError: beyond top-level package）。
正确做法：把「项目根目录」加入 sys.path，再用 `backend.xxx` 子包路径导入。

本脚本完成三件事：
  1) 静态：所有关键 .py 语法扫描（py_compile）
  2) 导入：验证 backend 关键子包能否成功 import
  3) 逻辑：验证此前缺失的 memory.build_full_memory_context 已修复可用

运行方式（在项目根目录）：
  backend\\venv\\Scripts\\python.exe tools\\check_health.py
或在任意处：
  python tools/check_health.py   （脚本会自动定位项目根）
"""
import importlib
import os
import sys
import traceback

# ---------- 定位项目根 ----------
# 本文件位于 <root>/tools/check_health.py，根即上级目录
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "backend")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ---------- 离线降级：屏蔽 sentence_transformers 联网下载 ----------
# embedding.encode 首次调用会 SentenceTransformer("all-MiniLM-L6-v2") 联网拉 80MB。
# 在无网络/离线环境会卡 15s+ 并重试。检测脚本不需要真实向量，直接让 is_available() 返回 False，
# 走项目自带的哈希降级方案（_hash_embedding），完全离线、秒级返回。
class _FakeST:
    """让 `import sentence_transformers` 成功但不可用，迫使 embedding 降级。"""
    class SentenceTransformer:
        def __init__(self, *a, **k):
            raise ImportError("disabled for offline health-check")

def _install_offline_embedding_shim():
    fake = type(sys)("sentence_transformers")
    fake.SentenceTransformer = _FakeST.SentenceTransformer
    sys.modules["sentence_transformers"] = fake

_install_offline_embedding_shim()

# ---------- 检测结果收集 ----------
results = []  # (name, ok, detail)

def record(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "OK " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" -> {detail}" if detail else ""))

# ---------- 1) 语法扫描 ----------
def check_syntax():
    import py_compile
    targets = [
        os.path.join(BACKEND, "main.py"),
        os.path.join(BACKEND, "memory", "__init__.py"),
        os.path.join(BACKEND, "memory", "prompt_builder.py"),
        os.path.join(BACKEND, "emotion_engine", "ai_emotion.py"),
        os.path.join(BACKEND, "multi_turn", "generator.py"),
    ]
    bad = []
    for f in targets:
        try:
            py_compile.compile(f, doraise=True)
        except py_compile.PyCompileError as e:
            bad.append(os.path.relpath(f, ROOT) + ": " + str(e))
    ok = not bad
    record("syntax scan (5 core files)", ok, "; ".join(bad) if bad else "all compiled")
    return ok

# ---------- 2) 关键子包导入 ----------
def check_imports():
    checks = [
        ("backend.config", "config"),
        ("backend.memory", "memory pkg"),
        ("backend.memory.prompt_builder", "prompt_builder"),
        ("backend.emotion_engine.ai_emotion", "emotion_engine"),
        ("backend.multi_turn.generator", "multi_turn"),
    ]
    all_ok = True
    for mod, label in checks:
        try:
            m = importlib.import_module(mod)
            record(f"import {label}", True, mod)
        except Exception as e:
            all_ok = False
            record(f"import {label}", False, repr(e).split("\n")[0])
    return all_ok

# ---------- 3) memory.build_full_memory_context 修复验证 ----------
def check_memory_func():
    try:
        from backend.memory import build_full_memory_context
        has_on_pkg = "build_full_memory_context" in dir(importlib.import_module("backend.memory"))
        # 不真正调用 encode（离线已降级，但 hybrid_search 仍可能走数据库）；仅验证属性可访问 + 签名
        import inspect
        sig = inspect.signature(build_full_memory_context)
        detail = f"signature={sig}; exposed_on_pkg={has_on_pkg}"
        ok = has_on_pkg and "user_id" in sig.parameters
        record("memory.build_full_memory_context (问题一修复)", ok, detail)
        return ok
    except Exception as e:
        record("memory.build_full_memory_context (问题一修复)", False, repr(e).split("\n")[0])
        return False

# ---------- 4) 顺带确认 main.py 调用点存在 ----------
def check_main_call_site():
    path = os.path.join(BACKEND, "main.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    ok = "yunlink_memory.build_full_memory_context(" in src
    record("main.py uses build_full_memory_context", ok,
           "present" if ok else "NOT FOUND in main.py")
    return ok

# ---------- main ----------
def main():
    print("=" * 64)
    print("桌面健康检测  (root: %s)" % ROOT)
    print("=" * 64)
    s1 = check_syntax()
    s2 = check_imports()
    s3 = check_memory_func()
    s4 = check_main_call_site()

    print("-" * 64)
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"SUMMARY: {passed}/{total} checks passed")
    print("=" * 64)
    # 退出码：全过=0，否则=1（方便 bat/CI 判断）
    sys.exit(0 if (s1 and s2 and s3 and s4) else 1)

if __name__ == "__main__":
    main()
