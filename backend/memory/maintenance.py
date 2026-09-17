# -*- coding: utf-8 -*-
"""
记忆健康维护模块 v1.0
职责：
  1. 批量更新 decay_score（定时衰减）
  2. 批量更新高频访问记忆的 importance（动态升权）
  3. 智能归档（decay低+访问少→软删除）
  4. 容量管理（超上限时清理最低价值记忆）
  5. 统计报告（打印当前记忆健康状态）

设计原则：
  - 所有操作幂等，重复跑不产生副作用
  - 关系/情感类记忆受保护，不参与归档
  - 静默执行，任何异常不影响主链路
"""
from .. import db
from .importance import calculate_decay_score, calculate_access_importance, should_archive

# ── 配置
MEMORY_HARD_LIMIT   = 500    # 单session记忆上限（超出触发容量清理）
MEMORY_TARGET       = 400    # 容量清理目标（清理到这个数量）
ARCHIVE_BATCH_SIZE  = 50     # 每次归档处理的记忆条数上限（避免单次耗时过长）


def run_decay_update(session_id: str, character_id: str = "default") -> int:
    """
    批量更新 decay_score。
    只处理 is_valid=1 的记忆，每次最多处理 ARCHIVE_BATCH_SIZE 条。

    Returns:
        int: 更新了多少条
    """
    updated = 0
    try:
        mems = db.valid_memories(
            session_id=session_id,
            character_id=character_id
        )

        for mem in mems[:ARCHIVE_BATCH_SIZE * 2]:
            new_decay = calculate_decay_score(mem)
            old_decay = float(mem.get("decay_score", 1.0) or 1.0)

            # 只在变化超过0.01时才写库（减少无效写入）
            if abs(new_decay - old_decay) > 0.01:
                db.q(
                    "UPDATE long_term_memory SET decay_score=? WHERE id=?",
                    (new_decay, mem["id"])
                )
                updated += 1

        if updated > 0:
            print(
                f"[MemMaintenance] decay_score 更新: "
                f"session={session_id} count={updated}",
                flush=True
            )
    except Exception as e:
        print(f"[MemMaintenance] decay更新失败(静默): {e}", flush=True)

    return updated


def run_importance_update(session_id: str, character_id: str = "default") -> int:
    """
    基于访问频率动态更新 importance。
    只升不降（保护重要性不被意外降低）。

    Returns:
        int: 更新了多少条
    """
    updated = 0
    try:
        mems = db.valid_memories(
            session_id=session_id,
            character_id=character_id
        )

        for mem in mems:
            new_imp = calculate_access_importance(mem)
            old_imp = int(mem.get("importance", 5) or 5)

            # 只升不降
            if new_imp > old_imp:
                db.update_memory_importance(mem["id"], new_imp)
                updated += 1

    except Exception as e:
        print(f"[MemMaintenance] importance更新失败(静默): {e}", flush=True)

    return updated


def run_archive(session_id: str, character_id: str = "default") -> int:
    """
    智能归档：把应该被遗忘的记忆软删除。
    关系/情感类记忆受保护不归档。

    Returns:
        int: 归档了多少条
    """
    archived = 0
    try:
        mems = db.valid_memories(
            session_id=session_id,
            character_id=character_id
        )

        batch = 0
        for mem in mems:
            if batch >= ARCHIVE_BATCH_SIZE:
                break

            if should_archive(mem):
                db.invalidate_memory(mem["id"], reason="archived")
                archived += 1
                batch += 1
                print(
                    f"[MemMaintenance] 归档记忆: "
                    f"id={mem['id']} "
                    f"type={mem.get('memory_type')} "
                    f"decay={mem.get('decay_score'):.2f} "
                    f"content={str(mem.get('memory_content',''))[:20]}",
                    flush=True
                )

    except Exception as e:
        print(f"[MemMaintenance] 归档失败(静默): {e}", flush=True)

    return archived


def run_capacity_check(session_id: str, character_id: str = "default") -> int:
    """
    容量管理：超过上限时清理最低价值记忆。
    清理策略：decay低 + importance低 + access少 → 优先清理。
    关系/情感类记忆受保护。

    Returns:
        int: 清理了多少条
    """
    cleaned = 0
    try:
        mems = db.valid_memories(
            session_id=session_id,
            character_id=character_id
        )

        count = len(mems)
        if count <= MEMORY_HARD_LIMIT:
            return 0

        print(
            f"[MemMaintenance] 记忆超上限: "
            f"session={session_id} count={count} limit={MEMORY_HARD_LIMIT}",
            flush=True
        )

        # 计算每条记忆的综合价值分
        def _value_score(m):
            decay      = float(m.get("decay_score",   1.0) or 1.0)
            importance = int(m.get("importance",       5)   or 5) / 10.0
            access     = min(1.0, int(m.get("access_count", 0) or 0) / 20.0)
            mem_type   = str(m.get("memory_type", "fact") or "fact")

            # 关系/情感记忆给最高价值，防止被清理
            type_protect = 1.0 if mem_type in ("relationship", "emotion") else 0.0

            return (
                decay      * 0.35
                + importance * 0.30
                + access     * 0.20
                + type_protect * 0.15
            )

        # 按价值从低到高排序
        sorted_mems = sorted(mems, key=_value_score)

        # 需要清理的数量
        need_clean = count - MEMORY_TARGET

        for mem in sorted_mems[:need_clean]:
            # 双重保护：关系/情感记忆绝对不清理
            if str(mem.get("memory_type", "")) in ("relationship", "emotion"):
                continue
            db.invalidate_memory(mem["id"], reason="archived")
            cleaned += 1

        if cleaned > 0:
            print(
                f"[MemMaintenance] 容量清理完成: "
                f"session={session_id} cleaned={cleaned} "
                f"remaining={count - cleaned}",
                flush=True
            )

    except Exception as e:
        print(f"[MemMaintenance] 容量检查失败(静默): {e}", flush=True)

    return cleaned


def reconcile_orphan_vectors(session_id: str = None,
                             character_id: str = None,
                             since_days: int = 0, limit: int = 500) -> int:
    """清理「记忆已作废、向量还留着」的孤儿向量。

    背景：记忆软删除（is_valid=0）时本应同步清向量，但两种情况会漏：
      1. 当初**写**向量就失败了——记忆层对向量写入是静默降级，不报错
      2. 删除过程中出错，只删了 SQLite 没删向量

    漏掉的结果：语义检索还能搜到这条记忆，但主库里它已经失效。
    这就是 P0-4 记录的「多套存储静默漂移」在删除侧的体现。
    实测首次运行时扫出 14 条历史遗留，说明这不是理论风险。

    Args:
        session_id/character_id: 为 None 时扫全库（定期维护用）
        since_days: 只处理最近 N 天被作废的；**0 = 不限时间**。
                    默认 0，因为历史遗留往往是很久以前留下的，
                    按时间窗口反而永远扫不到它们。
        limit: 单次最多处理条数，防止一次任务过重

    `delete_vector` 对不存在的 id 是安全的，所以重复执行没有副作用。

    Returns: 处理条数
    """
    import time as _time

    try:
        from .. import db
        from ..memory_manager import _delete_sql_vector
    except Exception:
        return 0

    try:
        sql = "SELECT id FROM long_term_memory WHERE is_valid=0"
        args = []
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(str(session_id))
        if character_id is not None:
            sql += " AND character_id=?"
            args.append(str(character_id))
        if since_days and since_days > 0:
            sql += " AND update_time>=?"
            args.append(_time.strftime(
                "%Y-%m-%d %H:%M:%S",
                _time.localtime(_time.time() - float(since_days) * 86400)))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = db.q(sql, tuple(args), fetch=True) or []
    except Exception:
        return 0

    n = 0
    for r in rows:
        try:
            _delete_sql_vector(r["id"])
            n += 1
        except Exception:
            continue
    if n:
        print(f"[MemMaintenance] 清理孤儿向量 {n} 条 session={session_id}", flush=True)
    return n


def run_full_maintenance(session_id: str, character_id: str = "default") -> dict:
    """
    完整维护流水线（按顺序执行）：
    1. 更新衰减分
    2. 更新访问重要性
    3. 智能归档
    4. 容量检查
    5. 孤儿向量对账（清理已作废记忆残留在向量库的条目）

    Returns:
        dict: 各步骤处理数量
    """
    result = {}

    result["decay_updated"]         = run_decay_update(session_id, character_id)
    result["importance_updated"]    = run_importance_update(session_id, character_id)
    result["archived"]              = run_archive(session_id, character_id)
    result["capacity_cleaned"]      = run_capacity_check(session_id, character_id)
    result["orphan_vectors_cleaned"] = reconcile_orphan_vectors(
        session_id, character_id)

    total = sum(result.values())
    if total > 0:
        print(
            f"[MemMaintenance] 维护完成: session={session_id} "
            f"结果={result}",
            flush=True
        )

    return result


def run_archive_all() -> int:
    """全库归档：遍历所有有记忆的 session，逐个跑动态升权 + 归档。

    由 memory_cleanup_loop 每天调用一次，让「不重要的记忆随时间衰减归档」真正生效。
    关系/情感类记忆在 run_archive 内有保护，不会被误删。
    """
    total = 0
    try:
        rows = db.q(
            "SELECT DISTINCT session_id, character_id FROM long_term_memory WHERE is_valid=1",
            fetch=True
        )
        for r in rows:
            try:
                sid = r["session_id"]
                cid = r["character_id"]
                run_decay_update(sid, cid)         # 先更新衰减分（否则 decay_score 一直是初始值，归档永不触发）
                run_importance_update(sid, cid)    # 再按访问频率动态升权
                total += run_archive(sid, cid)     # 最后归档低价值记忆
            except Exception:
                continue
    except Exception as e:
        print(f"[MemMaintenance] 全库归档失败(静默): {e}", flush=True)
    if total:
        print(f"[MemMaintenance] 全库归档完成: 归档 {total} 条", flush=True)
    return total


def get_memory_health_report(
    session_id: str,
    character_id: str = "default"
) -> dict:
    """
    记忆健康度报告（供前端展示或调试用）。

    Returns:
        dict: 健康度统计信息
    """
    try:
        mems = db.valid_memories(
            session_id=session_id,
            character_id=character_id
        )

        if not mems:
            return {
                "total": 0,
                "healthy": 0,
                "at_risk": 0,
                "critical": 0,
                "type_distribution": {},
                "avg_importance": 0.0,
                "avg_decay": 0.0,
            }

        healthy  = 0  # decay >= 0.6
        at_risk  = 0  # 0.3 <= decay < 0.6
        critical = 0  # decay < 0.3

        type_dist  = {}
        total_imp  = 0.0
        total_dec  = 0.0

        for m in mems:
            decay = float(m.get("decay_score", 1.0) or 1.0)
            imp   = int(m.get("importance", 5) or 5)
            mtype = str(m.get("memory_type", "fact") or "fact")

            if decay >= 0.6:
                healthy += 1
            elif decay >= 0.3:
                at_risk += 1
            else:
                critical += 1

            type_dist[mtype] = type_dist.get(mtype, 0) + 1
            total_imp += imp
            total_dec += decay

        n = len(mems)
        return {
            "total":             n,
            "healthy":           healthy,
            "at_risk":           at_risk,
            "critical":          critical,
            "near_limit":        n >= MEMORY_HARD_LIMIT * 0.8,
            "type_distribution": type_dist,
            "avg_importance":    round(total_imp / n, 2),
            "avg_decay":         round(total_dec / n, 4),
        }

    except Exception as e:
        print(f"[MemMaintenance] 健康报告失败: {e}", flush=True)
        return {"total": 0, "error": str(e)}
