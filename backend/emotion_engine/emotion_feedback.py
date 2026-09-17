# -*- coding: utf-8 -*-
"""
情绪干预效果反馈（结合优化 · 简化闭环）
记录干预前后的情绪变化，评估策略效果，统计成功率。
统计结果暂存 kv（emotion_feedback_stats:*），供未来仪表盘/策略加权读取。
"""
import json
import time

from .. import db

EVAL_DELAY = 180  # 干预后 3 分钟评估


def record_intervention(session_id, character_id, strategy_id, strategy_name, mood_before, score_before):
    """记录一次情绪干预（在主动发消息时调用）。"""
    try:
        key = f"emotion_feedback_pending:{session_id}:{character_id}"
        db.kv_set(key, json.dumps({
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "mood_before": mood_before,
            "score_before": score_before,
            "ts": time.time(),
        }, ensure_ascii=False))
    except Exception as e:
        print(f"[EmotionFeedback] 记录干预失败: {e}", flush=True)


def evaluate(session_id, character_id, mood_after, score_after):
    """在追加情绪快照时调用：评估上次干预效果（超时且方向变化明显才统计）。"""
    key = f"emotion_feedback_pending:{session_id}:{character_id}"
    raw = db.kv_get(key)
    if not raw:
        return
    try:
        rec = json.loads(raw)
    except Exception:
        return
    if time.time() - rec.get("ts", 0) < EVAL_DELAY:
        return
    # 清除 pending，避免重复评估
    db.kv_set(key, "")
    delta = score_after - rec.get("score_before", 0.0)
    if abs(delta) < 0.1:
        return
    _update_stats(rec.get("strategy_id", ""), rec.get("strategy_name", ""), delta)
    print(f"[EmotionFeedback] 干预「{rec.get('strategy_name','')}」效果: {rec.get('mood_before','')} → {mood_after} (Δ{delta:+.2f})", flush=True)


def _update_stats(strategy_id, strategy_name, delta):
    if not strategy_id:
        return
    try:
        key = f"emotion_feedback_stats:{strategy_id}"
        raw = db.kv_get(key)
        stats = json.loads(raw) if raw else {"count": 0, "total_delta": 0.0, "positive": 0, "negative": 0, "name": strategy_name}
    except Exception:
        stats = {"count": 0, "total_delta": 0.0, "positive": 0, "negative": 0, "name": strategy_name}
    stats["count"] += 1
    stats["total_delta"] += delta
    if delta > 0.1:
        stats["positive"] += 1
    elif delta < -0.1:
        stats["negative"] += 1
    try:
        db.kv_set(key, json.dumps(stats, ensure_ascii=False))
    except Exception:
        pass
