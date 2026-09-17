# -*- coding:utf-8 -*-
"""
Memory Importance Engine v1.0
记忆重要性动态计算引擎：

  输入：记忆使用情况
  输出：动态权重更新
  影响：检索排序 + 衰减速度 + 保留策略

  核心思想：
    - 记忆不是静态的，重要性会随使用情况动态变化
    - 被频繁使用、用户重复提及、强情绪事件、关系事件的记忆会提升重要性
    - 高重要性记忆会被永久保留，低价值记忆会被自动清理
"""
from .. import db
import math
from datetime import datetime


def calculate_importance_growth(
    memory,
    used=False,
    mentioned=False,
    emotional=False,
    relationship_event=False
):
    """
    计算记忆重要性增长值。

    Args:
        memory: 记忆字典
        used: AI是否使用了这条记忆
        mentioned: 用户是否再次提及
        emotional: 是否涉及强情绪事件
        relationship_event: 是否是关系事件

    Returns:
        float: 重要性增长值
    """
    score = 0

    # AI使用一次（轻微提升）
    if used:
        score += 0.2

    # 用户再次提及（明显提升）
    if mentioned:
        score += 1

    # 强情绪事件（显著提升）
    if emotional:
        score += 1.5

    # 关系事件（最大提升）
    if relationship_event:
        score += 2

    return score


def update_importance(memory_id, growth):
    """
    更新记忆的重要性分数。

    Args:
        memory_id: 记忆ID
        growth: 增长值（可以是负数，表示衰减）

    Returns:
        int or None: 更新后的重要性分数
    """
    memory = db.get_memory(memory_id)

    if not memory:
        return None

    old = int(memory.get("importance", 5))

    new = old + growth

    # 限制在 1-10 范围内
    new = max(1, min(10, round(new)))

    # 只有变化时才更新
    if new != old:
        db.update_memory_importance(memory_id, new)

    return new


def should_preserve_memory(memory):
    """
    判断记忆是否应该被永久保留（不被自动清理）。

    Args:
        memory: 记忆字典

    Returns:
        bool: 是否应该永久保留
    """
    importance = int(memory.get("importance", 5))

    # 重要性 >= 7 的记忆永久保留
    if importance >= 7:
        return True

    # 关系类型记忆永久保留
    memory_type = str(memory.get("memory_type", "fact"))
    if memory_type == "relationship":
        return True

    # 高访问次数记忆永久保留
    access_count = int(memory.get("access_count", 0))
    if access_count >= 10:
        return True

    return False


def get_importance_level(importance):
    """
    获取重要性等级描述。

    Args:
        importance: 重要性分数 1-10

    Returns:
        str: 等级描述
    """
    if importance >= 9:
        return "核心记忆"
    elif importance >= 7:
        return "重要记忆"
    elif importance >= 5:
        return "普通记忆"
    elif importance >= 3:
        return "次要记忆"
    else:
        return "临时记忆"


def batch_update_importance(memories, used_ids=None, mentioned_ids=None):
    """
    批量更新记忆重要性。

    Args:
        memories: 记忆列表
        used_ids: 被使用的记忆ID列表
        mentioned_ids: 被用户提及的记忆ID列表

    Returns:
        dict: 更新结果 {memory_id: new_importance}
    """
    used_ids = used_ids or []
    mentioned_ids = mentioned_ids or []

    results = {}

    for mem in memories:
        mem_id = mem.get("id")
        if not mem_id:
            continue

        used = mem_id in used_ids
        mentioned = mem_id in mentioned_ids

        if used or mentioned:
            growth = calculate_importance_growth(
                mem,
                used=used,
                mentioned=mentioned
            )
            new_importance = update_importance(mem_id, growth)
            results[mem_id] = new_importance

    return results


def calculate_decay_score(mem: dict) -> float:
    """
    计算记忆衰减分（0~1）。
    基于艾宾浩斯遗忘曲线：decay = e^(-λt)
    λ 由记忆类型决定（关系/情感记忆衰减慢，一般事实衰减快）

    Returns:
        float: 新的 decay_score（0~1），越小越应该被归档
    """
    TYPE_LAMBDA = {
        "relationship": 0.003,   # 关系记忆：极慢衰减（约330天减半）
        "emotion":      0.005,   # 情感记忆：慢衰减（约200天减半）
        "event":        0.008,   # 事件记忆：中等衰减（约125天减半）
        "preference":   0.006,   # 偏好记忆：慢衰减
        "fact":         0.012,   # 事实记忆：较快衰减（约83天减半）
        "general":      0.015,   # 普通记忆：快衰减（约66天减半）
    }

    mem_type = str(mem.get("memory_type", "fact") or "fact")
    lam      = TYPE_LAMBDA.get(mem_type, 0.012)

    # 取最后使用时间，没有则用创建时间
    ts = (
        mem.get("last_used")
        or mem.get("update_time")
        or mem.get("create_time")
    )

    if not ts:
        return 0.8  # 无时间信息，给中等衰减

    try:
        dt   = datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
        days = max(0, (datetime.now() - dt).days)
    except Exception:
        return 0.8

    # ★ 主观时间（2026-09-13）：衰减由"心里的钟"驱动，不是物理钟——
    #   冲突/低落/久等时主观时间流速快（记忆淡得快），高亲密度时流速慢
    #   （关于重要的人的记忆更保鲜）。读失败回落物理天数，行为不变。
    try:
        from .. import subjective_time
        days = subjective_time.subjective_days(
            days,
            str(mem.get("session_id") or "default"),
            str(mem.get("character_id") or "default"),
        )
    except Exception:
        pass

    # 访问次数加成：每次访问重置衰减（加强记忆效果）
    access = int(mem.get("access_count", 0) or 0)
    access_bonus = min(0.3, access * 0.03)  # 最多+0.3加成

    raw = math.exp(-lam * days)
    return round(min(1.0, raw + access_bonus), 4)


def calculate_access_importance(mem: dict) -> int:
    """
    基于访问频率动态计算记忆重要性。
    访问越频繁 → 重要性越高（用户反复提到的事更重要）。
    在原有 importance 基础上叠加，上限10。

    Returns:
        int: 新的 importance 值（1~10）
    """
    base       = int(mem.get("importance", 5) or 5)
    access     = int(mem.get("access_count", 0) or 0)

    # 访问次数加成阶梯
    if access >= 20:
        bonus = 3
    elif access >= 10:
        bonus = 2
    elif access >= 5:
        bonus = 1
    else:
        bonus = 0

    return min(10, max(1, base + bonus))


def should_archive(mem: dict) -> bool:
    """
    判断记忆是否应该被归档（软删除）。
    满足以下任一条件：
    1. decay_score < 0.15 且 access_count < 3（几乎被遗忘且从未被回忆）
    2. importance <= 2 且 decay_score < 0.3（重要性极低且严重衰减）
    3. memory_type == 'general' 且 decay_score < 0.2（普通记忆严重衰减）
    
    核心保护（永不归档）：
    - 关系/情感记忆
    - importance >= 6（称呼/偏好/约定/身份等重要记忆）
    - access_count >= 3（反复被想起的核心记忆）
    """
    decay      = float(mem.get("decay_score", 1.0) or 1.0)
    importance = int(mem.get("importance", 5) or 5)
    access     = int(mem.get("access_count", 0) or 0)
    mem_type   = str(mem.get("memory_type", "fact") or "fact")

    # ★ 核心保护：永不归档
    if mem_type in ("relationship", "emotion"):
        return False
    if importance >= 6:  # 称呼/偏好/约定/身份等重要记忆永不归档
        return False
    if access >= 3:     # 反复被检索/想起的核心记忆永不归档
        return False

    if decay < 0.25 and access < 3:
        return True
    if importance <= 2 and decay < 0.35:
        return True
    if mem_type == "general" and decay < 0.3:
        return True

    return False
