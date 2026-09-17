# -*- coding: utf-8 -*-
"""第 5 批「AI 学习闭环」回归测试（红-绿）。

跑法（在 <PROJECT_ROOT> 下）：
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py batch5
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
Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"]).mkdir(parents=True, exist_ok=True)

MAIN_PY = PROJECT_ROOT / "backend" / "main.py"


def fresh_db():
    """干净主库 + 清掉外置记忆库状态（生成器夹具）。"""
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


# ══════════════════════════════════════════════════════════════════
# 1. 风格反馈必须走文字链路（AST 守卫，防回归）
# ══════════════════════════════════════════════════════════════════

def _style_feedback_chain():
    """找出 style_feedback.detect_and_learn 调用点，返回包着它的块链。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == "detect_and_learn":
                target = node.lineno
                break
    assert target, "找不到 style_feedback.detect_and_learn 的调用点"
    chain = []
    _walk(tree, target, chain)
    return target, chain


def _walk(node, line, chain):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.With, ast.Try,
                              ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(child, "end_lineno", child.lineno)
            if child.lineno <= line <= end:
                if isinstance(child, ast.If):
                    chain.append(ast.unparse(child.test)[:60])
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chain.append(f"def {child.name}")
                else:
                    chain.append(type(child).__name__)
                _walk(child, line, chain)
                return


def test_style_feedback_not_inside_image_branch():
    """风格学习不许再被 `if has_image:` 包着。

    真机取证（AST）：调用点原先在 `if has_image:` 内，而那是全项目唯一调用点，
    于是**纯文字聊天永远不学习风格偏好**（日志 [StyleFeedback] 命中 0 次）。
    这条用例是防回归的哨兵：谁把它挪回图片分支就变红。
    """
    line, chain = _style_feedback_chain()
    bad = [c for c in chain if "has_image" in c]
    assert not bad, (
        f"main.py:{line} 的风格学习又被包进了图片分支（{bad}）—— "
        f"纯文字聊天会再次学不到偏好。完整块链: {chain}")
    assert any(c.startswith("def ") for c in chain), "调用点应在函数体内"


def test_learning_signals_wired_into_both_endpoints():
    """两个聊天入口都要调学习信号（非流式原先完全没有）。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "async def run_learning_signals(" in src, "缺少统一学习入口"
    # 至少两处调用（stream + /api/chat）
    assert src.count("run_learning_signals(") >= 3, (
        "学习信号的调用点不足：应包含定义 1 处 + 两个入口各 1 处")


def test_learning_signals_uses_character_id():
    """学习要写进**角色**的桶，不能误用 character_name 之类的变量。"""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert 'run_learning_signals(session, _cid or "default", _last_user)' in src, (
        "非流式入口应传解析后的角色键 _cid")
    assert 'run_learning_signals(session_id, character_id or "default", user_text or "")' in src, (
        "流式入口应传 character_id")


# ══════════════════════════════════════════════════════════════════
# 2. 学习到的偏好必须真的进 prompt（写→读闭环）
# ══════════════════════════════════════════════════════════════════

def test_learned_style_preference_is_stored_and_injected(fresh_db):
    """学到一条偏好后：既落盘，又出现在注入块里。"""
    from backend import db, style_feedback as sf

    cid = "小满"
    assert sf.guidance_block(cid) == "", "没有记录时不该有不存在的注入块"

    items = ["不要在TA吐槽时讲道理，先站TA这边"]
    sf._save_list(cid, items)

    assert sf._load_list(cid) == items, "写侧没落盘"
    block = sf.guidance_block(cid)
    assert "讲道理" in block, f"学到的偏好没进注入块: {block!r}"


def test_coarse_hit_recognizes_criticism():
    """粗筛要能认出"批评回应方式"的话（否则学习永不触发）。"""
    from backend import style_feedback as sf

    for text in ("你别老讲道理", "不要敷衍我", "你说话像客服", "别分析了"):
        assert sf.coarse_hit(text), f"这句批评没被识别: {text}"
    assert not sf.coarse_hit("今天天气不错"), "正常闲聊不该触发学习"


# ══════════════════════════════════════════════════════════════════
# 3. learned_rules：写侧必须真的能落盘并被读回
# ══════════════════════════════════════════════════════════════════

def test_learned_rule_auto_learner_roundtrip():
    """她学到的规矩要能通过**自动学习入口**沉淀，并且下一轮被注入。

    原先唯一的写入口是 agent 工具（只在助手模式的 agent 循环里跑），
    日常聊天里立的规矩没有任何路径被记住（真机 learned_rules/助手.json 恒为 `[]`）。
    """
    from backend import learned_rules as lr
    from backend.agent import self_modules as sm

    cid = "测试小满"
    path = sm._rules_path(cid)
    if path.exists():
        path.unlink()

    try:
        assert sm.load_rules(cid) == []
        assert sm.learned_rules_block(cid) == "", "没有规则时不该注入空块"

        # 自动学习入口（不经过 agent 工具）
        added = lr.add_rule(cid, "用户不喜欢被连发消息，忙的时候合并成一条")
        assert added is True, "自动学习入口没写进去"

        # 重复的规矩不该重复沉淀
        assert lr.add_rule(cid, "用户不喜欢被连发消息，忙的时候合并成一条") is False

        rules = sm.load_rules(cid)
        assert rules and "合并成一条" in rules[0]["text"], f"写侧没落盘: {rules}"
        assert rules[0]["source"] == "auto", "自动学到的应标记 source=auto"
        block = sm.learned_rules_block(cid)
        assert "合并成一条" in block, f"读侧没注入: {block!r}"
    finally:
        try:
            path.unlink()
        except Exception:
            pass


def test_learned_rule_coarse_hit_recognizes_teaching():
    """粗筛要能认出"立规矩"的话，且不误伤普通闲聊。"""
    from backend import learned_rules as lr

    for text in ("以后别半夜问我睡没睡", "记住我不吃香菜", "下次不要催我",
                 "跟你说过多少次了"):
        assert lr.coarse_hit(text), f"这句立规矩没被识别: {text}"
    assert not lr.coarse_hit("今天好累啊"), "普通吐槽不该触发规矩学习"


def test_parse_rule_tolerates_wrappers():
    """模型输出常带 markdown 包裹，解析要能容错；临时要求要能拒掉。"""
    from backend import learned_rules as lr

    assert lr._parse_rule('{"rule": "以后别提我前女友", "is_long_term": true}') == "以后别提我前女友"
    assert lr._parse_rule('```json\n{"rule": "别催我睡觉", "is_long_term": true}\n```') == "别催我睡觉"
    assert lr._parse_rule('{"rule": "帮我查天气", "is_long_term": false}') == "", (
        "一次性要求不该沉淀成长期规矩")
    assert lr._parse_rule("不是JSON") == ""


# ══════════════════════════════════════════════════════════════════
# 4. 人格状态双键分叉：写进 default 桶的学习成果必须能读回来
# ══════════════════════════════════════════════════════════════════

def test_personality_state_falls_back_to_default_bucket(fresh_db):
    """同一 session 下学习成果写在 'default' 桶时，按角色读也必须能拿到。

    真机现象：personality_state 同时存在
      (s_93ceb989…, '助手')   —— 人设快照
      (s_93ceb989…, 'default') —— 反思/反馈写出的行为策略（warmth/humor/
                                 relationship_behavior）
    两边都在被写，而读侧只按 character_id 精确取一条 → 约一半人格学习读不到。
    """
    from backend import db

    sid, cid = "s_main", "小满"
    # 精确行：只有人设，没有任何学习成果
    db.update_personality_state(sid, cid, core_personality="温柔黏人", warmth_delta=0)
    # 学习成果被写进了 default 桶（历史调用方的行为）
    db.update_personality_state(sid, "default", warmth_delta=6, humor_delta=14,
                                relationship_behavior="不要连发消息，合并成一条")

    st = db.get_personality_state(sid, cid)
    assert int(st.get("warmth_delta") or 0) == 6, (
        f"default 桶里的学习成果必须能被读到，实际 {st.get('warmth_delta')}")
    assert int(st.get("humor_delta") or 0) == 14
    assert "合并成一条" in str(st.get("relationship_behavior") or "")
    # 人设类字段以精确行为准
    assert st.get("core_personality") == "温柔黏人", "精确行的人设不该被回落行覆盖"

    # 精确行自己已经有学习成果时，不许被 default 桶盖掉
    db.update_personality_state(sid, cid, warmth_delta=2)
    st2 = db.get_personality_state(sid, cid)
    assert int(st2.get("warmth_delta") or 0) == 2, "精确行有值时优先精确行"


# ══════════════════════════════════════════════════════════════════
# 5. 五维饱和：反馈必须永远推得动语气
# ══════════════════════════════════════════════════════════════════

def test_five_dim_stays_responsive_at_ceiling(fresh_db):
    """顶到上限后，新反馈仍必须改变数值（真机现场：五维全顶 ±20 → 学习永久失效）。"""
    import asyncio
    from backend import db, personality_manager as pm

    sid, cid = "s_main", "小满"
    # 先人为顶格（模拟真机现状）
    db.update_five_dim(sid, cid, warmth_delta=20, dominance_delta=-20,
                       humor_delta=20, initiative_delta=20, attachment_delta=20)
    before = db.get_personality_state(sid, cid)
    assert int(before["warmth_delta"]) == 20

    # 再来一次负反馈：温暖度必须下降（旧实现里 20 - 1 又被 clamp 回 20 → 纹丝不动）
    asyncio.get_event_loop().run_until_complete(
        pm.update_five_dim(sid, cid, feedback_signal="negative"))
    after = db.get_personality_state(sid, cid)
    assert int(after["warmth_delta"]) < 20, (
        f"顶格后负反馈应让温暖度下降，实际 {before['warmth_delta']} -> {after['warmth_delta']}")
    # ★ 主导度已在负侧：要断言的是"边界不粘连"，而不是某个具体数字。
    #   设计不变式（比逐字数值更该守）：
    #     · 不能钉死在 ±20（旧实现里 -20 收到负反馈后仍是 -20，因为 clamp 吃掉一切）；
    #     · 也不能因为回中趋势把负向信号吃成"变温和"（那是顺序写反的症状）。
    #   当前实现：-20 收到 -1 → 先叠加 -21 → 回中 → -17（绝对值变小 = 从边界松开）。
    before_dom = int(before["dominance_delta"])
    after_dom = int(after["dominance_delta"])
    assert after_dom != before_dom, (
        f"负反馈对主导度毫无影响（被钳死）：{before_dom} -> {after_dom}")
    assert abs(after_dom) < 20, f"仍在边界上粘着（应松开）：{after_dom}"
    assert abs(after_dom) <= 20, "仍受 ±20 钳制"


def test_five_dim_equilibrium_stays_away_from_ceiling(fresh_db):
    """反复的正向反思不许把维度顶到边界（要收敛在中间某处）。

    ★ 这是"越聊越像人"的核心：能变化、有区分度、且**不会锁死在极值**。
    标定依据：稳态 x* = delta/(1-D)。
      · D=0.98, delta=+2 → x*=100 → 撞上限 20（第一次修就是这个结果，等于没修）
      · D=0.80, delta=+2 → x*=10  → 顶不到边界
    """
    from backend import personality_manager as pm

    x = 20            # 从满格开始
    for _ in range(6):
        x = pm.apply_five_dim_delta(x, 2)
    assert x < 20, f"连续正向反思仍贴在上限：{x}"
    assert 5 <= x <= 18, f"收敛位置不合理（应在中间地带）：{x}"


def test_five_dim_decays_toward_baseline(fresh_db):
    """反馈停止后，维度要回到基础值（"像人"的遗忘），且是**渐进**的。

    ★ 标定更新（2026-09-17 第二次修）：衰减从 0.98 调到 0.80，
      因为 0.98 的平衡点在 100（撞上限），维度永远贴边、失去区分度。
      现在的回落曲线：20 → 16 → 13 → 10 → 8 → 6（渐进，不是一次掉光）。
    """
    import asyncio
    from backend import db, personality_manager as pm

    sid, cid = "s_main", "小满"
    db.update_five_dim(sid, cid, humor_delta=20)
    vals = []
    for _ in range(6):
        asyncio.get_event_loop().run_until_complete(
            pm.update_five_dim(sid, cid, feedback_signal="neutral"))
        vals.append(int(db.get_personality_state(sid, cid)["humor_delta"]))

    assert vals[0] < 20, f"没有新证据时应开始回落，实际 {vals}"
    assert vals[0] >= 15, f"回落要渐进（第一步不能掉太多），实际 {vals}"
    assert vals == sorted(vals, reverse=True), f"应当单调回落，实际 {vals}"
    assert vals[-1] < vals[0], f"应确实在回落，实际 {vals}"


def test_reflection_must_not_pin_five_dim_at_ceiling(fresh_db):
    """反思写入也不许把五维钉死在上限（否则"越聊越像人"变成"越聊越固定"）。

    ★ 真机现场（2026-09-17 03:46，用户聊了一轮之后）：
        personality_state('助手') = warmth 20 / dominance -20 / humor 20 /
                                   initiative 20 / attachment 20   ← 全顶格
      而我加在 `update_five_dim` 里的均值回归**根本没机会生效**：
      `reflection/strategy.py` 的 `apply_reflection_to_personality_state` 是**第二个
      写入者**，它自己算 `old + delta` 再 clamp 到 ±20 —— 而
      `apply_reflection_to_personality` 只产出**正**delta（温柔→+2、幽默→+2…），
      于是每次都往上顶、顶到 20 就锁死，永远回不来。
      多个写入者各写各的 = 谁也没法保证"有界且可回落"。
    """
    from backend import db
    from backend.reflection import database as rdb, strategy as rs

    sid, cid = "s_main", "小满"
    db.update_five_dim(sid, cid, warmth_delta=20, humor_delta=20)

    rdb.init_reflection_db()
    # 两条会触发 warmth_delta+2 / humor_delta+2 的策略反思
    rdb.save_reflection(sid, cid, "strategy_reflection",
                        "用户难过时要先共情、先安慰，别急着分析。", 0.9)
    rdb.save_reflection(sid, cid, "strategy_reflection",
                        "用户喜欢玩笑和接梗，语气轻松一点更好。", 0.9)

    patch = rs.apply_reflection_to_personality_state(sid, cid)
    st = db.get_personality_state(sid, cid)
    print("  反思 patch:", patch)
    print(f"  五维: warmth={st.get('warmth_delta')} humor={st.get('humor_delta')}")

    # 已经在上限时，反思不该把它继续钉在 20（应当有回落余地）
    assert int(st.get("warmth_delta") or 0) < 20 or int(st.get("humor_delta") or 0) < 20, (
        f"反思写入把五维钉死在上限：warmth={st.get('warmth_delta')} "
        f"humor={st.get('humor_delta')}（应允许回落）")


# ══════════════════════════════════════════════════════════════════
# 6. 隐式反馈闭环：采到 → 画像 → 进 prompt
# ══════════════════════════════════════════════════════════════════

def test_implicit_feedback_loop_end_to_end(fresh_db):
    """采集隐式反馈 → 生成画像 → 产出注入块（这条回路以前"只写不读"）。"""
    from backend.feedback import database as fbdb
    from backend.feedback.collector import collect_implicit_feedback
    from backend.feedback.learner import FeedbackLearner

    sid, cid = "s_main", "小满"
    fbdb.init()

    # 门槛内至少要 2 条；这里造 4 条"用户继续聊"的隐式反馈
    for i in range(4):
        collect_implicit_feedback(
            sid, cid, f"m{i}",
            ai_reply="嗯，我在听。然后呢？你想说什么都可以。",
            user_response="好呀那我们继续聊",
        )

    learner = FeedbackLearner()
    profile = learner.get_profile(sid, cid)
    assert profile.get("total_feedback", 0) >= 4, f"隐式反馈没入库: {profile}"

    block = learner.build_feedback_prompt(profile)
    assert block, f"采到反馈后应能产出注入块，实际空（profile={profile}）"
    assert "反馈" in block


def test_feedback_prompt_silent_when_too_little_data(fresh_db):
    """样本太少时不许硬编偏好（避免用一两条噪音误导语气）。"""
    from backend.feedback import database as fbdb
    from backend.feedback.learner import FeedbackLearner

    fbdb.init()
    learner = FeedbackLearner()
    empty = learner.get_profile("s_none", "小满")
    assert learner.build_feedback_prompt(empty) == "", "零反馈不该有注入块"