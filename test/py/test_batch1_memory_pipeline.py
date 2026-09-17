# -*- coding: utf-8 -*-
"""第 1 批「记忆链路地基」回归测试（红-绿）。

跑法（在 D:\\AI聊天项目桌面端\\AI聊天项目 下）：
    backend\\venv\\Scripts\\python.exe -m pytest test\\py\\test_batch1_memory_pipeline.py -q

★ 隔离：import backend.* 之前先把 AI_COMPANION_DATA_DIR 指到 pytest 临时目录，
  所以本文件永远不会碰用户真实数据（%APPDATA%\\HomeAime\\data）。
★ 每个用例先红后绿：这些断言描述的是**修好之后应有的行为**，
  在当前代码上必须失败；修完必须全绿。
"""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── 隔离数据目录（必须在 import backend 之前）──────────────────────────────
#   用 setdefault：run_tests.py 已经设过就复用它，保证 db.DB_PATH 与夹具一致。
#   为什么不用系统 Temp：沙箱/受限环境里 Temp 下建 sqlite 会被拒
#   （实测 "unable to open database file"），所以统一放工程内 .pytest_data\。
_TMP = os.environ.setdefault("AI_COMPANION_DATA_DIR",
                             str(PROJECT_ROOT / ".pytest_data"))
Path(_TMP).mkdir(parents=True, exist_ok=True)
# 外置记忆库同样隔离（见 run_tests.py 里的说明：开发模式默认落在工程树）
os.environ.setdefault("AI_COMPANION_EXTERNAL_MEMORY_DIR",
                      str(Path(_TMP) / "外置记忆库"))
Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"]).mkdir(parents=True, exist_ok=True)

from backend import db, config, memory_manager  # noqa: E402
from backend import memory_brain  # noqa: E402


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════

def fresh_db():
    """干净主库（每个用例独立，避免相互污染）。生成器夹具，run_tests.py 负责进入/退出。

    ★ 同时清掉「外置记忆库」目录：归档游标就存在那里的 state.json，
      上一轮跑测试留下的游标会让下一轮"以为已经归档过"（实测踩过）。
    """
    import shutil
    ext_dir = Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"])
    if ext_dir.exists():
        shutil.rmtree(ext_dir, ignore_errors=True)
    db_path = Path(_TMP) / "local_db.db"
    if db_path.exists():
        db_path.unlink()
    with db._lock:
        if db._conn is not None:
            try:
                db._conn.close()
            except Exception:
                pass
        db._conn = None
        db.init()
    yield
    with db._lock:
        if db._conn is not None:
            try:
                db._conn.close()
            except Exception:
                pass
        db._conn = None


def _insert(memory_id, content, *, importance=5, session="s_main", char="小满",
            scope="character", create_time="2026-09-01T10:00:00", mtype="fact",
            recall_count=0, access_count=0):
    """按指定 id 直接插一条记忆（id 可控，便于构造“旧记忆被挤出榜单”的场景）。"""
    db.q(
        """INSERT OR REPLACE INTO long_term_memory
           (id, session_id, character_id, memory_scope, memory_content, category,
            memory_type, importance, create_time, update_time, is_valid, memory_status,
            confidence, recall_count, access_count)
           VALUES(?,?,?,?,?,?,?,?,?,?,1,'active',1.0,?,?)""",
        (memory_id, session, char, scope, content, "general", mtype, importance,
         create_time, create_time, recall_count, access_count),
    )


# ══════════════════════════════════════════════════════════════════
# 1. 候选池：旧记忆不该被 importance 榜单挤掉
# ══════════════════════════════════════════════════════════════════

def test_candidates_do_not_drop_unimportant_recent_memory(fresh_db):
    """对话里刚说的事（importance 4），不能因为榜上全是高重要度旧记忆而消失。

    现状：valid_memories() 内部 LIMIT 200 ORDER BY importance DESC, id DESC，
         该会话 2769 条有效记忆里 92.8% 永远进不了打分。
    ★ 构造要点：新记忆的 id 取得**最小**（模拟"新记忆 id 小"的极端情况），
      这样「重要度榜」和「id DESC 榜」都取不到它，只有靠 id 倒序之外的
      近期通道（create_time 排序）才可能把它捞回来 —— 正是要测的那条通道。
    """
    _insert(1, "TA昨天说这周末想去看海", importance=4,
            create_time="2026-09-16T22:00:00")          # 最新的一条，id 最小
    for i in range(260):
        _insert(1000 + i, f"很久以前的高重要度记忆{i}", importance=10,
                create_time="2026-08-01T10:00:00")

    pool = db.recall_candidates(session_id="s_main", character_id="小满")
    contents = {m["memory_content"] for m in pool}
    assert "TA昨天说这周末想去看海" in contents, (
        "最近说的低重要度记忆必须进候选池（否则她永远想不起昨天的事）")
    assert len(pool) > 200, (
        f"候选池必须突破旧口径的 200 条上限，实际 {len(pool)}（旧代码恰好 200）")


def test_valid_memories_limit_is_configurable(fresh_db):
    """valid_memories 必须支持 limit，且默认值不能把活跃会话砍到 200 条。"""
    for i in range(300):
        _insert(2000 + i, f"记忆{i}", importance=10)
    assert len(db.valid_memories(session_id="s_main", character_id="小满",
                                 limit=280)) == 280
    assert len(db.valid_memories(session_id="s_main", character_id="小满",
                                 limit=10)) == 10


# ══════════════════════════════════════════════════════════════════
# 2. memory_block 缓存：换了问题不能复用上一条问题的记忆
# ══════════════════════════════════════════════════════════════════

def test_memory_block_cache_key_includes_query(fresh_db, monkeypatch):
    """30 秒内换问题，必须重新检索（现状：缓存键只有 session/character/boost）。"""
    calls = []

    def fake_search(query, top_k=8, session_id="default", character_id="default"):
        calls.append(query)
        return [({"id": 1, "memory_content": f"关于「{query}」的记忆", "importance": 8,
                  "memory_scope": "character", "memory_type": "fact",
                  "create_time": "2026-09-16T10:00:00", "confidence": 1.0,
                  "recall_count": 0, "access_count": 0, "decay_score": 1.0,
                  "session_id": session_id, "character_id": character_id}, 0.9)]

    monkeypatch.setattr(memory_manager, "search_memories", fake_search)
    memory_manager.invalidate_memory_cache()

    a = memory_manager.memory_block(query="我记得你喜欢吃酱鸭腿吗",
                                    session_id="s_main", character_id="小满")
    b = memory_manager.memory_block(query="你记得我养的那只猫叫什么吗",
                                    session_id="s_main", character_id="小满")

    assert calls == ["我记得你喜欢吃酱鸭腿吗", "你记得我养的那只猫叫什么吗"], (
        f"第二次提问必须重新检索，实际检索了 {calls}")
    assert "酱鸭腿" in a and "猫" in b, "两个问题必须拿到各自相关的记忆"


# ══════════════════════════════════════════════════════════════════
# 3. 打分：相关性必须主导（不能被静态属性压过）
# ══════════════════════════════════════════════════════════════════

def test_scoring_is_relevance_dominant(fresh_db, monkeypatch):
    """与当前提问相关的记忆，必须压过“重要度高但与提问无关”的那条。

    真正的行为检查（不依赖 embedding 模型）：问猫的名字，猫那条必须排第一。
    """
    import backend.memory.embedding as emb

    monkeypatch.setattr(emb, "encode", lambda text, *a, **k: None, raising=False)

    _insert(1, "TA对AI的爱深入骨髓，用「爱你骨子」表达极深的情感。", importance=10,
            recall_count=500, access_count=500, mtype="emotion")
    _insert(2, "TA养了一只叫汤圆的猫。", importance=5, mtype="fact")
    _insert(3, "TA喜欢吃酱鸭腿。", importance=5, mtype="preference")
    memory_manager.invalidate_memory_cache()

    scored = memory_manager.search_memories("我那只猫叫什么来着", top_k=3,
                                            session_id="s_main", character_id="小满")
    assert scored, "必须有命中"
    top = scored[0][0]["memory_content"]
    assert "汤圆" in top, f"问猫必须答猫，实际第一条是：{top!r}"


def test_relevance_weight_dominates_static_weights():
    """公式本身：同样相关度下可以比重要度，但相关度高必须能翻盘。"""
    src = (PROJECT_ROOT / "backend" / "memory_manager.py").read_text("utf-8")
    assert "relevance" in src
    # 抓 search_memories 里的权重行，确认相关性权重是最大的一项
    import re
    block = src[src.index("def search_memories"):src.index("def build_memory_query")]
    weights = {name: float(val) for name, val in
               re.findall(r"(relevance|importance|freshness|type_weight|decay_score)\s*\*\s*([0-9.]+)",
                          block)}
    assert weights.get("relevance", 0) >= 0.5, (
        f"相关性权重必须占主导，实际 {weights}")
    assert weights.get("relevance", 0) > max(
        v for k, v in weights.items() if k != "relevance"), (
        "相关性必须大于任何单个静态属性权重")


# ══════════════════════════════════════════════════════════════════
# 4. 外置原文检索：短提问不该靠字面命中称王
# ══════════════════════════════════════════════════════════════════

def test_short_query_does_not_let_lexical_dominate(monkeypatch):
    """「做吗」这种 2 字提问，不能让 lex=1.0 把语义无关的片段顶到第一。"""
    from backend.external_memory import index

    assert index.LEXICAL_WEIGHT <= 0.25, (
        f"短 query 场景下词面权重过高，实际 {index.LEXICAL_WEIGHT}")

    # 直接验证打分函数：vec 高但无词面 vs vec 低但词面满
    def score(vec, lex, q_len):
        return index._final_score(vec, lex, q_len)

    vec_good = score(0.62, 0.0, 2)     # 语义相关、没碰巧同字
    lex_only = score(0.39, 1.0, 2)     # 只是碰巧同字（现状会赢）
    assert vec_good > lex_only, (
        f"语义相关必须赢过纯字面命中：vec_good={vec_good} lex_only={lex_only}")


# ══════════════════════════════════════════════════════════════════
# 5. 注入预算：不能一个巨型片段挤爆整个预算
# ══════════════════════════════════════════════════════════════════

def test_injected_block_respects_hard_budget(fresh_db, monkeypatch):
    """memory_block(limit=1800) 的返回必须 ≤ 1800 字（现状会冲到 3000+）。"""
    for i in range(30):
        _insert(100 + i, "很长的记忆内容" * 20, importance=9)

    block = memory_manager.memory_block(limit=1800, session_id="s_main",
                                        character_id="小满")
    assert len(block) <= 1800, f"注入块超预算：{len(block)} 字"


# ══════════════════════════════════════════════════════════════════
# 6. 归档游标：不能跨会话跳号丢历史
# ══════════════════════════════════════════════════════════════════

def test_archive_cursor_is_per_session(fresh_db):
    """两个会话交错有新消息时，谁都不许被跳过（现状共用一个游标会永久丢行）。"""
    from backend.external_memory import archive, paths

    db.add_message("s_A", "user", "A 会话第一句", "小满")
    db.add_message("s_A", "assistant", "A 会话第一句的回复", "小满")
    db.add_message("s_B", "user", "B 会话第一句", "小满")
    db.add_message("s_A", "user", "A 会话第二句", "小满")

    n_first = archive.archive_new_messages("s_B", "小满")   # 先把 B 归档，游标会跳到 B 的最大 id
    assert n_first >= 1

    n_second = archive.archive_new_messages("s_A", "小满")
    raw = paths.raw_dir("小满")
    text = "\n".join(p.read_text("utf-8") for p in raw.glob("*.md")) if raw.exists() else ""

    assert "A 会话第一句" in text, (
        f"A 会话早于 B 的消息被跳号丢掉了（跨会话游标 bug）；n_first={n_first} "
        f"n_second={n_second} text={text[:200]!r}")
    assert "A 会话第二句" in text, (
        f"A 会话后续消息也没归档；n_second={n_second} text={text[:200]!r}")
    assert n_second >= 2, f"A 会话应补归档 2 条，实际 {n_second}；text={text[:200]!r}"


# ══════════════════════════════════════════════════════════════════
# 8. 向量通道：L2 距离→余弦的换算（通道B 能不能加分的根）
# ══════════════════════════════════════════════════════════════════

def test_l2_distance_to_cosine_conversion():
    """归一化向量的 L2 距离 d 与余弦的关系是 cos = 1 - d²/2。

    现状（必须变红的那种）：`similarity = 1.0 - min(d, 1.0)`，
    等于把 cos<0.5 的区间全部截成 0 —— 而调用方门槛是 vsim>=0.5 / >=0.75，
    于是通道B 的加分几乎永远加不上（真机 929 次 "Error loading hnsw index" 之外，
    即便正常也有这个换算错误）。
    """
    from backend.memory.vector_store import _l2_distance_to_cosine as conv

    # 完全相同的向量：d=0 → cos=1
    assert conv(0.0) == 1.0
    # 实测真机最近邻常见的 d≈1.0：旧换算给 0.0，正确值是 0.5
    assert abs(conv(1.0) - 0.5) < 1e-6, f"d=1.0 应换算成 0.5，实际 {conv(1.0)}"
    # text2vec 上"语义相关"常见 cos≈0.7 → d≈0.775
    assert 0.65 < conv(0.775) < 0.75, f"d=0.775 应约 0.70，实际 {conv(0.775)}"
    # d=1.5 → cos = 1 - 2.25/2 = -0.125 → 裁到 0（负余弦对"该不该加分"无意义）
    assert conv(1.5) == 0.0
    assert conv(1.2) > 0.0, "d=1.2 对应 cos=0.28，仍有区分度，不该被压成 0"
    # 越界裁剪
    assert conv(2.0) == 0.0 and conv(-0.5) == 1.0


def test_vector_store_treats_hnsw_error_as_corruption():
    """真机那条 929 次的报错必须被识别成损坏，否则永远不会自愈。"""
    from backend.memory.vector_store import _corruption_error

    real = ("Error executing plan: Error sending backfill request to compactor: "
            "Error constructing hnsw segment reader: Error creating hnsw segment "
            "reader: Error loading hnsw index")
    assert _corruption_error(Exception(real)) is True
    assert _corruption_error(Exception("Collection [abc] does not exist")) is False


# ══════════════════════════════════════════════════════════════════
# 9. personality_guard：签名容错（现状主链路每轮异常）
# ══════════════════════════════════════════════════════════════════

def test_personality_guard_accepts_str_and_dict():
    from backend import personality_manager as pm

    # dict：正常用法
    assert pm.personality_guard("这是说教式分析", {"forbidden_phrases": "说教"}) is False
    assert pm.personality_guard("正常回复", {"forbidden_phrases": "说教"}) is True
    # str：主链路历史用法（现状 AttributeError → 每轮 guard 失败）
    assert pm.personality_guard("这是说教式分析", "说教") is False
    assert pm.personality_guard("正常回复", "") is True
