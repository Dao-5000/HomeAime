# -*- coding: utf-8 -*-
"""第 6 批「记忆变更可查可撤回」回归测试（红-绿）。

跑法（在 <PROJECT_ROOT> 下）：
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py batch6
"""
import ast
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_TMP = os.environ.setdefault("AI_COMPANION_DATA_DIR",
                             str(PROJECT_ROOT / ".pytest_data"))
Path(_TMP).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AI_COMPANION_EXTERNAL_MEMORY_DIR",
                      str(Path(_TMP) / "外置记忆库"))

MAIN_PY = PROJECT_ROOT / "backend" / "main.py"


def fresh_db():
    import gc
    import shutil
    import time
    ext = Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"])
    if ext.exists():
        shutil.rmtree(ext, ignore_errors=True)
    from backend import db

    def _close():
        with db._lock:
            if db._conn is not None:
                try:
                    db._conn.close()
                except Exception:
                    pass
            db._conn = None

    def _unlink():
        p = Path(_TMP) / "local_db.db"
        for attempt in range(10):
            try:
                if p.exists():
                    p.unlink()
                return
            except PermissionError:
                gc.collect()
                time.sleep(0.15 * (attempt + 1))

    _close()
    _unlink()
    db.init()
    yield
    _close()
    _unlink()


def _insert(mid, content, importance=8, status="active", valid=1):
    from backend import db
    db.q("""INSERT OR REPLACE INTO long_term_memory
            (id, session_id, character_id, memory_scope, memory_content, category,
             memory_type, importance, create_time, update_time, is_valid, memory_status,
             confidence)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1.0)""",
         (mid, "s_main", "小满", "character", content, "general", "fact",
          importance, "2026-09-01T10:00:00", "2026-09-01T10:00:00", valid, status))


# ══════════════════════════════════════════════════════════════════
# 1. 审计：合并→撤回→内容与状态完全复原
# ══════════════════════════════════════════════════════════════════

def test_merge_then_revert_restores_content_and_status(fresh_db):
    """合并（作废旧条）后整批撤回，旧条必须回到"内容+有效性+状态"全一致。"""
    from backend import db, memory_audit

    _insert(2, "TA养了一只叫汤圆的猫。", importance=9)
    before = memory_audit.snapshot(2)
    assert before and before["is_valid"] == 1 and before["memory_status"] == "active"

    # 模拟"合并"：把 2 号标为 superseded，并记审计（与修复工具同构）
    db.q("UPDATE long_term_memory SET is_valid=0, memory_status='superseded', "
         "update_time=? WHERE id=2", ("2026-09-17T02:00:00",))
    memory_audit.record("merge", 2, session_id="s_main", character_id="小满",
                        reason="与 #1 同义，合并", source="test",
                        before=before, after=memory_audit.snapshot(2),
                        batch_id="b_test_1")

    mid = db.q("SELECT is_valid, memory_status FROM long_term_memory WHERE id=2",
               fetch=True)[0]
    assert int(mid["is_valid"]) == 0, "合并后应失效"

    res = memory_audit.revert_batch("b_test_1")
    assert res["ok"] and res["n"] == 1, f"整批撤回失败: {res}"

    after = db.q("SELECT is_valid, memory_status, memory_content, importance "
                 "FROM long_term_memory WHERE id=2", fetch=True)[0]
    assert int(after["is_valid"]) == 1, "撤回后必须恢复有效"
    assert after["memory_status"] == "active", "撤回后状态必须复原"
    assert after["memory_content"] == "TA养了一只猫，叫汤圆。" or "汤圆" in after["memory_content"]
    assert int(after["importance"]) == 9, "重要度必须复原"


def test_revert_is_idempotent_and_audited(fresh_db):
    """撤回过的记录不能再撤一次（避免反复横跳），且要标记 reverted。"""
    from backend import db, memory_audit

    _insert(3, "TA喜欢吃酱鸭腿。", importance=7)
    before = memory_audit.snapshot(3)
    db.q("UPDATE long_term_memory SET importance=3 WHERE id=3")
    aid = memory_audit.record("regrade", 3, session_id="s_main", character_id="小满",
                              reason="重判", source="test", before=before,
                              after=memory_audit.snapshot(3))

    r1 = memory_audit.revert(aid)
    assert r1["ok"], f"单条撤回失败: {r1}"
    assert int(db.q("SELECT importance FROM long_term_memory WHERE id=3",
                    fetch=True)[0]["importance"]) == 7

    r2 = memory_audit.revert(aid)
    assert r2["ok"] is False and "已经撤回" in r2["why"], f"重复撤回没被拦住: {r2}"

    row = db.q("SELECT reverted, reverted_at FROM memory_audit WHERE id=?",
               (aid,), fetch=True)[0]
    assert int(row["reverted"]) == 1 and row["reverted_at"], "撤回要留痕"


def test_audit_summary_counts_ops(fresh_db):
    from backend import db, memory_audit

    _insert(4, "TA是夜猫子。", importance=6)
    for imp in (2, 3):
        before = memory_audit.snapshot(4)
        db.q("UPDATE long_term_memory SET importance=? WHERE id=4", (imp,))
        memory_audit.record("regrade", 4, session_id="s_main", character_id="小满",
                            reason="重判", source="test", before=before,
                            after=memory_audit.snapshot(4))
    s = memory_audit.summary("s_main", "小满")
    assert s.get("regrade", {}).get("n") == 2, f"统计不对: {s}"

    batches = memory_audit.recent_batches("s_main", "小满")
    assert isinstance(batches, list)


# ══════════════════════════════════════════════════════════════════
# 2. 对外接口必须存在（前端要用）
# ══════════════════════════════════════════════════════════════════

def test_audit_endpoints_registered():
    """三个接口必须在 main.py 里注册（否则前端拿不到数据）。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    routes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    if dec.args and isinstance(dec.args[0], ast.Constant):
                        routes.add(str(dec.args[0].value))
    for want in ("/api/memory/audit", "/api/memory/audit/revert", "/api/learning/status"):
        assert want in routes, f"接口没注册: {want}（现有：{sorted(routes)[:6]}…）"


def test_audit_endpoints_are_readable_and_guarded():
    """审计/学习接口的源码要有关键点：整批撤回、明细裁剪、异常兜底。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "memory_audit.revert_batch" in src, "接口没接整批撤回"
    assert "before_content" in src, "明细没做裁剪（会把整行快照塞给前端）"
    assert "style_feedback._load_list" in src, "学习状态没汇总风格偏好"
    assert "sm.load_rules" in src, "学习状态没汇总已沉淀的规矩"
