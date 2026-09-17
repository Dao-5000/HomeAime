# -*- coding: utf-8 -*-
"""第 4b 批「两段式按需翻库」回归测试（红-绿）。

跑法（在 <PROJECT_ROOT> 下）：
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


# ══════════════════════════════════════════════════════════════════
# 1. 本地粗筛：宁滥勿缺（漏翻才是体感里的"她不记得"）
# ══════════════════════════════════════════════════════════════════

def test_local_suspect_catches_past_references():
    from backend import recall_decider as rd

    for text in ("你还记得我家猫叫什么吗", "上次我们说的那个约定呢",
                 "你答应我什么来着", "那天你说要给我买奶茶",
                 "我以前跟你说过我讨厌什么吗", "对了，那个事怎么样了"):
        r = rd.local_suspect(text)
        assert r["suspect"], f"这句明明在问过去的事，粗筛漏了: {text}（{r}）"


def test_local_suspect_skips_trivial_chatter():
    """纯寒暄不该触发模型调用（省成本）；但不许把正常提问也拦掉。"""
    from backend import recall_decider as rd

    for text in ("嗯嗯", "哈哈", "晚安", "在吗", "么么", "抱抱", "好", "睡吧"):
        assert not rd.local_suspect(text)["suspect"], f"寒暄不该触发翻库: {text}"

    # 长一点的正常提问仍然要允许（宁滥勿缺）
    assert rd.local_suspect("你觉得我今天该早点睡吗")["suspect"], "正常长提问不该被拦"


def test_local_suspect_fires_after_long_gap():
    """很久没翻过库 → 给一次机会（避免长对话里记忆彻底沉底）。"""
    from backend import recall_decider as rd

    assert rd.local_suspect("今天怎么样", rounds_since_recall=15)["suspect"] is True
    assert rd.local_suspect("今天怎么样", rounds_since_recall=1)["suspect"] is False


# ══════════════════════════════════════════════════════════════════
# 2. 开关与失败降级：绝不因为决策器坏了影响回话
# ══════════════════════════════════════════════════════════════════

def test_decider_disabled_returns_no_need(monkeypatch):
    from backend import config, recall_decider as rd

    real_get = config.get
    monkeypatch.setattr(config, "get", lambda k, d=None: (False if k == "RECALL_DECIDER_ENABLED"
                                                          else real_get(k, d)))
    import asyncio
    out = asyncio.get_event_loop().run_until_complete(
        rd.decide("s", "c", "你还记得我家猫叫什么吗"))
    assert out["need"] is False and out["skipped"] == "disabled"


def test_decider_without_key_is_safe(monkeypatch):
    """没有 key 时不许抛异常，按"不翻"处理。"""
    import asyncio
    from backend import config, recall_decider as rd

    monkeypatch.setattr(config, "api_key_for_model", lambda m: None)
    out = asyncio.get_event_loop().run_until_complete(
        rd.decide("s", "c", "你还记得我家猫叫什么吗"))
    assert out["need"] is False
    assert out["skipped"] in ("nokey", "error")


def test_decide_parse_tolerates_wrappers():
    from backend import recall_decider as rd

    out = rd._parse_decision('```json\n{"need": true, "queries": ["猫的名字", "养猫"],'
                             ' "time_hint": "上个月", "why": "问过去的事"}\n```')
    assert out["need"] is True and out["queries"] == ["猫的名字", "养猫"]
    assert out["time_hint"] == "上个月"

    # 字符串形式的 queries 也要容错；超量截断到 2 条
    out2 = rd._parse_decision('{"need": true, "queries": "单条检索词"}')
    assert out2["queries"] == ["单条检索词"]
    out3 = rd._parse_decision('{"need": true, "queries": ["a","b","c","d"]}')
    assert out3["queries"] == ["a", "b"]

    assert rd._parse_decision("不是JSON") == {}


# ══════════════════════════════════════════════════════════════════
# 3. 定向块：有检索词才翻，没词不翻
# ══════════════════════════════════════════════════════════════════

def test_targeted_block_empty_without_queries():
    from backend import recall_decider as rd

    assert rd.build_targeted_block("s", "c", []) == ""
    assert rd.build_targeted_block("s", "c", [""]) == ""


def test_chat_loop_calls_decider():
    """接线哨兵：chat_logic 的注入链里必须真的调用决策器（否则等于没做）。"""
    src = (PROJECT_ROOT / "backend" / "chat_logic.py").read_text(encoding="utf-8")
    assert "recall_decider" in src, "chat_logic 没接上按需翻库"
    assert "build_targeted_block" in src, "chat_logic 没有用定向块"
