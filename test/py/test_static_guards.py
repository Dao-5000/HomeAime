# -*- coding: utf-8 -*-
"""静态守卫：同一模块里**重复定义同名函数**必须报错。

★ 为什么要有这条（2026-09-17 真实踩坑）：
  我给 `recall_decider.py` 修 `local_suspect` 时，编辑落成了**两份同名函数**，
  后面那份（旧逻辑）把前面那份静静盖掉了。表现极具迷惑性：
  源码肉眼看着是新的、`inspect.getsource` 却打出旧版本，
  测试红着而我在源码里找不到旧代码。
  Python 不报错、不警告 —— 只有"读文件的那一刻"能发现。
  成本极低（纯 AST），所以对所有后端模块都跑一遍。
"""
import ast
import collections
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BACKEND = PROJECT_ROOT / "backend"
# ★ 必须**剪枝遍历**，不能用 rglob：rglob 会进 venv / 模型目录
#   （backend\venv 几万个文件、voice_clone_models 全是几百 MB 的文件），
#   实测直接把整个测试套件拖到 5 分钟以上超时。
SKIP_DIRS = {"venv", "__pycache__", "_bak_20260914_opt", "build", "dist",
             "dist_old", "rvc_dataset", "tts_cache", "voice_clone_models",
             ".git", "node_modules", "tools"}


def _module_files():
    stack = [BACKEND]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if e.name in SKIP_DIRS:
                    continue
                stack.append(Path(e.path))
            elif e.name.endswith(".py"):
                yield Path(e.path)


def test_no_duplicate_top_level_functions():
    """同一文件里同名顶层函数/类只能出现一次。"""
    bad = []
    for path in _module_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{path.name}: 语法错误 {e}")
            continue
        names = [n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        dups = [k for k, v in collections.Counter(names).items() if v > 1]
        if dups:
            bad.append(f"{path.relative_to(PROJECT_ROOT)}: 重复定义 {dups}")
    assert not bad, "发现重复定义（后者会静默覆盖前者）：\n  " + "\n  ".join(bad)


def test_no_duplicate_methods_inside_classes():
    """类内部同名方法也只能出现一次（同类坑）。"""
    bad = []
    for path in _module_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = [n.name for n in node.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            dups = [k for k, v in collections.Counter(names).items() if v > 1]
            if dups:
                bad.append(f"{path.relative_to(PROJECT_ROOT)}::{node.name}: 重复方法 {dups}")
    assert not bad, "类内重复方法：\n  " + "\n  ".join(bad)


# ══════════════════════════════════════════════════════════════════
# 3. 只在启动链建的表，使用点必须能自愈（no such table）
# ══════════════════════════════════════════════════════════════════

def test_kg_and_feedback_have_table_self_heal():
    """KG / 反馈的表原本只在 main.py 启动时建。

    任何绕过那条启动链的入口（测试夹具、脚本、新人格验收、只 import chat_logic
    的新进程）都会撞 `no such table`，而那里只打一行日志 → 该功能静默停摆。
    真机现场：新人格端到端跑出
      `[KnowledgeGraph] 抽取触发失败: no such table: kg_extraction_cursor`
      `[FeedbackLearning] 反馈学习失败: no such table: ai_feedback`
    现在两处都要有"就地建表 + 重试"。
    """
    src = (PROJECT_ROOT / "backend" / "chat_logic.py").read_text(encoding="utf-8")
    assert "init_knowledge_graph" in src, "KG 缺表没有自愈（仍会静默停摆）"
    assert "已就地初始化" in src, "KG 自愈没有落地"
    # 反馈库自愈
    assert "from .feedback.database import init" in src, "反馈库缺表没有自愈"


def test_knowledge_graph_init_creates_cursor_table():
    """init_knowledge_graph() 必须真的建出 kg_extraction_cursor（自愈才有效）。

    ★ 刻意**不**动 os.environ / 不重载模块：那会污染同一进程里后续用例的
      singleton（db 的连接、config 的 DATA_DIR 都是 import 时定死的）。
      这里就用本套件共享的 .pytest_data 主库 —— 该函数幂等，建表无副作用。
    """
    from backend import db
    from backend.knowledge_graph import database as kgdb

    db.init()
    kgdb.init_knowledge_graph()
    rows = db.q("SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='kg_extraction_cursor'", fetch=True)
    assert rows, "init_knowledge_graph() 没能建出 kg_extraction_cursor"
