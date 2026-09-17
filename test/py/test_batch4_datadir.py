# -*- coding: utf-8 -*-
"""第 4 批「数据归属」回归测试：学习库必须落在 DATA_DIR，不许进打包目录。

跑法（在 D:\\AI聊天项目桌面端\\AI聊天项目 下）：
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py batch4
"""
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


def _in_data_dir(p: Path) -> bool:
    try:
        Path(p).resolve().relative_to(Path(_TMP).resolve())
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════
# 1. 三个附属库都必须落在 DATA_DIR
# ══════════════════════════════════════════════════════════════════

def test_aux_dbs_live_in_data_dir():
    """feedback.db / behavior.db / memory.db 都必须在 DATA_DIR 下。

    真机取证：它们原先硬编码 `ROOT_DIR/backend/data/`，打包模式下
    ROOT_DIR = <安装目录>/resources，而 electron-builder **每次重打包都会删除重建**
    win-unpacked → 学习数据（隐式反馈、行为模式）随打包一起消失。
    """
    from backend import config
    from backend.feedback import database as fb_db
    from backend.behavior import database as bh_db
    from backend.memory import database as mem_db

    for name, path in (("feedback.db", fb_db.DB_PATH),
                       ("behavior.db", bh_db.DB_PATH),
                       ("memory.db", mem_db.DB_PATH)):
        assert _in_data_dir(path), (
            f"{name} 落在 DATA_DIR 之外：{path}（重打包会丢）")
        assert str(path).endswith(name)

    # DATA_DIR 本身就是可写的数据目录
    assert config.aux_data_dir().resolve() == Path(_TMP).resolve()


# ══════════════════════════════════════════════════════════════════
# 2. 旧位置的库要被自动搬迁（幂等、不丢数据）
# ══════════════════════════════════════════════════════════════════

def test_migrate_aux_db_copies_from_legacy_location():
    """旧位置(ROOT_DIR/backend/data)有库时，要搬到新位置且内容一致、幂等。

    ★ 需要显式打开 AI_COMPANION_FORCE_AUX_MIGRATION：默认在"数据目录≠真机目录"时
      是**禁止**搬迁的（防测试/新人格验收继承工程树历史库）。
      本用例要验的正是搬迁逻辑本身，所以显式开闸。
    ★ 用完必须还原环境变量：本跑器没有 pytest 的 monkeypatch，
      用例间共享 os.environ —— 不还原会让后续用例把库写到别处
      （真踩过：feedback.db 用例读到 38 行真实数据、agent 取消用例找不到刚建的任务）。
    """
    import sqlite3
    from backend import config

    _saved = os.environ.get("AI_COMPANION_FORCE_AUX_MIGRATION")
    os.environ["AI_COMPANION_FORCE_AUX_MIGRATION"] = "1"
    legacy_dir = Path(config.ROOT_DIR) / "backend" / "data"
    legacy = legacy_dir / "migrate_probe.db"
    target = Path(_TMP) / "migrate_probe.db"
    for p in (legacy, target):
        try:
            p.unlink()
        except Exception:
            pass

    legacy_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(legacy))
    con.execute("CREATE TABLE t(x TEXT)")
    con.execute("INSERT INTO t VALUES('旧库里的一条数据')")
    con.commit()
    con.close()

    try:
        out = config.migrate_aux_db("migrate_probe.db")
        assert Path(out).resolve() == target.resolve(), f"应返回新位置，实际 {out}"
        assert target.exists(), "旧库没有被搬过来"
        con = sqlite3.connect(str(target))
        rows = con.execute("SELECT x FROM t").fetchall()
        con.close()
        assert rows and rows[0][0] == "旧库里的一条数据", "搬迁后内容不对"
        assert legacy.exists(), "旧文件应保留（留一份原始证据）"

        # 幂等：再跑一次不能覆盖新库
        con = sqlite3.connect(str(target))
        con.execute("INSERT INTO t VALUES('新库里新增的')")
        con.commit()
        con.close()
        config.migrate_aux_db("migrate_probe.db")
        con = sqlite3.connect(str(target))
        n = con.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        con.close()
        assert n == 2, "重复搬迁把新库覆盖了（应幂等）"
    finally:
        # ★ 还原环境变量（本跑器没有 monkeypatch，用例间共享 os.environ）
        if _saved is None:
            os.environ.pop("AI_COMPANION_FORCE_AUX_MIGRATION", None)
        else:
            os.environ["AI_COMPANION_FORCE_AUX_MIGRATION"] = _saved
        for p in (legacy, target):
            try:
                p.unlink()
            except Exception:
                pass


def test_migrate_skipped_when_data_dir_is_not_live():
    """测试/临时数据目录下**不许**从工程树搬旧库（否则新人格验收会继承历史数据）。

    真实踩坑：新人格端到端验收的临时目录里出现了 434KB 的 `memory.db`，
    来自工程树 `backend/data`——"全新人格"于是不再全新，
    "从 0 开始越用越懂你"这条结论就被污染了。
    """
    import sqlite3
    from backend import config

    # ★ 本用例验的是"默认禁止搬迁"，所以必须把上一个用例开的闸关掉
    #   （同进程共享 os.environ，用例间会串 —— 这正是它第一次跑失败的原因）
    os.environ.pop("AI_COMPANION_FORCE_AUX_MIGRATION", None)
    legacy_dir = Path(config.ROOT_DIR) / "backend" / "data"
    legacy = legacy_dir / "isolation_probe.db"
    target = Path(_TMP) / "isolation_probe.db"
    for p in (legacy, target):
        try:
            p.unlink()
        except Exception:
            pass

    legacy_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(legacy))
    con.execute("CREATE TABLE t(x TEXT)")
    con.execute("INSERT INTO t VALUES('工程树里的历史数据')")
    con.commit()
    con.close()

    try:
        out = Path(config.migrate_aux_db("isolation_probe.db")).resolve()
        assert out == target.resolve(), f"应指向数据目录，实际 {out}"
        assert not target.exists(), (
            "数据目录不是真机目录时不该搬迁 —— 否则临时环境会继承历史库")
    finally:
        for p in (legacy, target):
            try:
                p.unlink()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
# 3. 附属库要真的能建表读写（搬迁后不残废）
# ══════════════════════════════════════════════════════════════════

def test_feedback_db_writable_after_move():
    """附属库必须可写、且留在 DATA_DIR。

    ★ 断言要**精确到本次写入的那一行**：.pytest_data 里的 feedback.db 会跨轮次累积
      （夹具只重置 local_db.db），用"第一条是不是 positive"会被历史行顶掉
      （实测：失败信息里出现 continued 46 / positive 46 这种历史统计）。
    """
    from backend.feedback import database as fb

    fb.init()
    fb.save_feedback("s_main", "小满", "m_check", "positive", score=1,
                     context="测试", ai_reply="回复", user_response="嗯")
    rows = fb.get_recent_feedback("s_main", "小满", limit=10) or []
    mine = [r for r in rows if r.get("message_id") == "m_check"]
    assert mine, f"刚写的反馈没读回来；最近 10 条={[r.get('message_id') for r in rows]}"
    assert mine[0]["feedback_type"] == "positive"
    assert _in_data_dir(fb.DB_PATH), "feedback.db 必须留在 DATA_DIR"


def test_migrate_replaces_broken_empty_target():
    """目标位置是 0 字节残file 时必须重搬（第一次搬迁真踩过这个坑）。

    真机现场：`behavior.db` 在新位置变成 0 字节（旧位置 16384 字节）——
    源库正被运行中的应用以 WAL 打开，`shutil.copy2` 拷到的是不完整状态。
    现在用 sqlite 在线备份 API + integrity_check，并且允许覆盖空残file。
    """
    os.environ["AI_COMPANION_FORCE_AUX_MIGRATION"] = "1"
    import sqlite3
    from backend import config

    legacy_dir = Path(config.ROOT_DIR) / "backend" / "data"
    legacy = legacy_dir / "migrate_broken.db"
    target = Path(_TMP) / "migrate_broken.db"
    for p in (legacy, target):
        try:
            p.unlink()
        except Exception:
            pass

    legacy_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(legacy))
    con.execute("CREATE TABLE t(x TEXT)")
    con.execute("INSERT INTO t VALUES('应该被搬过去的数据')")
    con.commit()
    con.close()

    target.write_bytes(b"")            # 制造 0 字节残file
    assert target.stat().st_size == 0

    try:
        out = config.migrate_aux_db("migrate_broken.db")
        assert Path(out).resolve() == target.resolve()
        assert target.stat().st_size > 0, "空残file 没有被重搬覆盖"
        con = sqlite3.connect(str(target))
        rows = con.execute("SELECT x FROM t").fetchall()
        con.close()
        assert rows and rows[0][0] == "应该被搬过去的数据", "重搬后数据不对"
    finally:
        for p in (legacy, target):
            try:
                p.unlink()
            except Exception:
                pass
