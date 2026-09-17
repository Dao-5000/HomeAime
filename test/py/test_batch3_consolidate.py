# -*- coding: utf-8 -*-
"""第 3 批「整理 / 提炼 / 遗忘」回归测试（红-绿）。

跑法（在 D:\\AI聊天项目桌面端\\AI聊天项目 下）：
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py batch3
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
Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"]).mkdir(parents=True, exist_ok=True)


def fresh_db():
    """干净主库 + 清掉外置记忆库状态。生成器夹具。"""
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
                # Windows：句柄释放有延迟（我们刚关连接，杀软/索引器也可能握着）
                gc.collect()
                time.sleep(0.15 * (attempt + 1))

    import gc
    _close()
    _unlink()
    db.init()
    yield
    _close()
    _unlink()


# ══════════════════════════════════════════════════════════════════
# 1. 人生档案模板：必须能 format（真机 88 次全失败的那个 KeyError）
# ══════════════════════════════════════════════════════════════════

def test_consolidate_prompt_can_format():
    """模板必须能被 str.format 填充；未转义花括号会让整理模块每次必抛。"""
    from backend.profile_memory.consolidator import CONSOLIDATE_PROMPT

    filled = CONSOLIDATE_PROMPT.format(memory="记忆A", timeline="事件B")
    assert "记忆A" in filled and "事件B" in filled
    # 格式化后 JSON 示例要还原成单花括号（否则给模型看的示例是坏的）
    assert '"type": "life_phase"' in filled
    assert "{{" not in filled and "}}" not in filled


def test_consolidate_prompt_format_does_not_raise_keyerror():
    """用真实长度文本再跑一遍，确认不会因示例花括号抛 KeyError。"""
    from backend.profile_memory.consolidator import CONSOLIDATE_PROMPT

    try:
        CONSOLIDATE_PROMPT.format(memory="x" * 100, timeline="y" * 100)
    except KeyError as e:  # noqa: PERF203
        raise AssertionError(f"模板仍有未转义占位符: {e}") from e


# ══════════════════════════════════════════════════════════════════
# 2. 画像：空值不许覆盖已有内容
# ══════════════════════════════════════════════════════════════════

def test_profile_empty_fields_do_not_wipe_existing(fresh_db):
    """模型返回空串时，不许把已有画像字段清空。

    真机现象：user_profile.id=17 的 nickname/hobbies 全为空串，
    而 updated_time 是刚刚 —— 每轮"画像更新成功"其实是在反复抹掉内容。
    """
    from backend import db

    db.update_profile("s_main", "小满", nickname="宝", hobbies="劈木头、钻矿洞")
    before = db.get_profile("s_main", "小满")
    assert before and before.get("nickname") == "宝"

    # 模型这次返回空串（很常见：这轮没有新信息）
    db.update_profile("s_main", "小满", nickname="", hobbies="", occupation="")
    after = db.get_profile("s_main", "小满")
    assert after.get("nickname") == "宝", (
        f"空值不该覆盖已有昵称，实际 {after.get('nickname')!r}")
    assert after.get("hobbies") == "劈木头、钻矿洞", (
        f"空值不该覆盖已有爱好，实际 {after.get('hobbies')!r}")


# ══════════════════════════════════════════════════════════════════
# 3. 遗忘：衰减必须真的能走到归档阈值
# ══════════════════════════════════════════════════════════════════

def test_decay_can_reach_archive_threshold():
    """一条"两年前、没人用过、也不重要"的流水记忆，必须能沉到归档线以下。

    真机现象：decay_score 最小值恒为 0.5123、2823/3790 恒为 1.0，
    归档阈值（0.25）永远达不到 —— "遗忘"这件事在系统里不存在。
    根因：旧公式 importance×0.5 + access×0.2 + age×0.3，
    光 importance/10 一项就占 0.5，任何 importance≥6 的记忆分数永不低于 0.3。
    """
    from backend import memory_brain
    from backend.memory.importance import should_archive, calculate_decay_score

    trivia = {
        "importance": 4, "access_count": 0, "recall_count": 0,
        "create_time": "2024-09-01T00:00:00", "last_used": None,
        "memory_type": "episode", "session_id": "s", "character_id": "c",
    }
    d = memory_brain.calculate_decay(trivia)
    assert d < 0.25, f"两年前的无关流水应低于归档线 0.25，实际 {d:.4f}"
    assert calculate_decay_score(trivia) < 0.25, (
        f"importance 引擎的衰减分也应沉下去，实际 {calculate_decay_score(trivia):.4f}")

    # 昨天刚说的同类记忆不该被衰减掉
    fresh = {**trivia, "create_time": "2026-09-16T00:00:00"}
    assert memory_brain.calculate_decay(fresh) > 0.6, "新记忆不能被误判为可遗忘"

    # 重要且常用的记忆必须留着
    core = {"importance": 10, "access_count": 50, "recall_count": 30,
            "create_time": "2024-01-01T00:00:00", "last_used": "2026-09-16T10:00:00",
            "memory_type": "relationship", "session_id": "s", "character_id": "c"}
    assert memory_brain.calculate_decay(core) > 0.5, "核心记忆不能被衰减掉"
    assert should_archive({**trivia, "decay_score": d}) is True
    assert should_archive({**core, "decay_score": memory_brain.calculate_decay(core)}) is False


# ══════════════════════════════════════════════════════════════════
# 3b. 全局维护任务必须覆盖真实桶（不能只扫 default/default）
# ══════════════════════════════════════════════════════════════════

def test_maintenance_scans_all_buckets(fresh_db):
    """衰减刷新必须覆盖所有 session/character，而不是只扫 (default, default)。

    真机现象：memory_brain.refresh_memory_scores() 调 db.valid_memories() 不传参，
    默认桶只有 489 行（还被 LIMIT 200 砍），用户真实会话 3315 行完全没被维护。
    """
    from backend import db, memory_brain

    db.q("""INSERT INTO long_term_memory
            (id, session_id, character_id, memory_scope, memory_content, category,
             memory_type, importance, create_time, update_time, is_valid, memory_status,
             confidence, decay_score)
            VALUES
            (1,'default','default','character','测试桶的一条','general','fact',5,
             '2024-01-01T00:00:00','2024-01-01T00:00:00',1,'active',1.0,1.0),
            (2,'s_real','小满','character','真实会话的一条老记忆','general','fact',4,
             '2024-01-01T00:00:00','2024-01-01T00:00:00',1,'active',1.0,1.0)""")

    n = memory_brain.refresh_memory_scores()
    assert n >= 2, f"两行都该被刷新，实际刷新 {n} 行"
    row = db.q("SELECT decay_score FROM long_term_memory WHERE id=2", fetch=True)[0]
    assert float(row["decay_score"]) < 0.25, (
        f"真实会话的老记忆 decay_score 也必须被算出来，实际 {row['decay_score']}")


# ══════════════════════════════════════════════════════════════════
# 4. 遗忘是"标记"不是"删除"
# ══════════════════════════════════════════════════════════════════

def test_forget_marks_never_deletes(fresh_db):
    """清理低价值记忆只许标记，不许 DELETE（用户拍板：永不真删）。"""
    from backend import db, memory_brain

    db.q("""INSERT INTO long_term_memory
            (id, session_id, character_id, memory_scope, memory_content, category,
             memory_type, importance, create_time, update_time, is_valid, memory_status,
             confidence, decay_score)
            VALUES(1,'s_main','小满','character','很久以前的一句废话','general',
                   'episode',1,'2025-01-01T00:00:00','2025-01-01T00:00:00',1,'active',1.0,0.01)""")
    before = db.q("SELECT COUNT(*) c FROM long_term_memory", fetch=True)[0]["c"]

    memory_brain.clean_low_value_memory()

    rows = db.q("SELECT id, is_valid, memory_status FROM long_term_memory WHERE id=1", fetch=True)
    assert len(rows) == 1, "行被真删了 —— 必须只标记不删除"
    assert int(rows[0]["is_valid"]) == 0, "低价值记忆应被标记为无效"
    after = db.q("SELECT COUNT(*) c FROM long_term_memory", fetch=True)[0]["c"]
    assert after == before, "总行数不该变化"


# ══════════════════════════════════════════════════════════════════
# 5. 反思回流：注入过的反思要留下"被用过"的痕迹
# ══════════════════════════════════════════════════════════════════

def test_reflection_injection_records_last_used(fresh_db):
    """反思被真正注入 prompt 后，last_used 必须被写上。

    真机现象：916/916 条 reflection 的 last_used 全为 NULL，
    `touch_reflection()` 零调用 —— 反思库只进不出，无法"越用越准"。
    """
    from backend import db
    from backend.reflection import database as rdb
    from backend.reflection import strategy as rs

    rdb.init_reflection_db()
    rid = rdb.save_reflection("s_main", "小满", "strategy_reflection",
                              "用户忙的时候不要连发消息，合并成一条更有效。", 0.8)
    assert rid

    block = rs.build_reflection_prompt("s_main", "小满", limit=10)
    assert "不要连发消息" in block, "反思应该被注入"

    row = db.q("SELECT last_used FROM memory_reflection WHERE id=?", (rid,), fetch=True)[0]
    assert row["last_used"], "注入过的反思必须记 last_used（否则无从判断哪条有用）"


def test_rank_reflections_prefers_used_ones():
    """用过的反思要排在从没用过的前面（让有效的沉淀上浮）。"""
    from backend.reflection.strategy import rank_reflections

    rows = [
        {"id": 1, "last_used": None, "confidence": 0.95, "created_at": "2026-09-16"},
        {"id": 2, "last_used": "2026-09-16T10:00:00", "confidence": 0.50, "created_at": "2026-09-10"},
    ]
    assert [r["id"] for r in rank_reflections(rows)] == [2, 1]
