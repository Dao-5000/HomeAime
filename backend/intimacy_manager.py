# -*- coding: utf-8 -*-
"""
亲密度联动：前端上报亲密度 → 后端持久化 → 注入 system prompt。
★ 改：与 relationship/manager.py 双向同步，不再是孤立 kv。
"""
from . import db
from .relationship.manager import RelationshipManager


def report(session_id: str, value, character_id: str = "default"):
    """
    前端上报亲密度快照（持久化，重启保留）。
    ★ 改：同时更新 relationship_state 表的 intimacy 字段，双向同步。
    """
    try:
        v = int(round(float(value)))
        v = max(0, min(100, v))
    except (TypeError, ValueError):
        return

    # ★ 写 kv（兼容原有调用）
    db.kv_set(f"intimacy:{session_id}:{character_id}", v)

    # ★ 同时写 relationship_state（接通两套系统）
    try:
        from .relationship.evolution import calculate_stage
        stage = calculate_stage(v)
        mgr   = RelationshipManager()
        mgr.create_user(session_id, character_id)
        mgr.update(session_id, character_id, intimacy=v, stage=stage)
    except Exception as e:
        print(f"[IntimacyManager] 同步关系表失败(静默): {e}", flush=True)


def get(session_id: str, character_id: str = "default") -> int:
    """
    读取亲密度。
    优先读 relationship_state 表；旧 kv 只作为兼容兜底。
    """
    # relationship_state 是权威来源，避免旧前端快照把设置页/关系引擎的新值拉回去。
    try:
        mgr = RelationshipManager()
        state = mgr.get_state(session_id, character_id, create=False)
        if state:
            v = int(state.get("intimacy", 0) or 0)
            db.kv_set(f"intimacy:{session_id}:{character_id}", v)
            return max(0, min(100, v))
    except Exception:
        pass

    # 兼容旧格式 kv（带角色优先，其次老的 session 全局键）
    for key in (f"intimacy:{session_id}:{character_id}", f"intimacy:{session_id}"):
        try:
            raw = db.kv_get(key)
            if raw is not None:
                return max(0, min(100, int(float(raw))))
        except (TypeError, ValueError):
            pass
    return 0


def sync_from_relationship(session_id: str, character_id: str = "default"):
    """
    从 relationship_state 表反向同步到 kv。
    用于确保两套系统数据一致（启动时或定时调用）。
    """
    try:
        mgr   = RelationshipManager()
        state = mgr.get_state(session_id, character_id, create=False)
        v     = int(state.get("intimacy", 0) or 0)
        db.kv_set(f"intimacy:{session_id}:{character_id}", v)
    except Exception as e:
        print(f"[IntimacyManager] 反向同步失败(静默): {e}", flush=True)


def style_block(session_id: str, character_id: str = "default") -> str:
    """根据亲密度生成注入 system prompt 的风格文本块。"""
    v = get(session_id, character_id)
    if v is None:
        return ""
    if v >= 80:
        return (
            f"【亲密度联动（当前 {v}/100：灵魂默契）】\n"
            "· 回复结尾自然延伸 2~3 句（分享自己的事/接话题/轻轻撒娇，按人设）；\n"
            "· 可以较频繁（约每 2 轮一次）自然提出一个具体的小问题，让对话流动；\n"
            "· 语气放松亲密，但绝不连环提问、不查户口。"
        )
    if v >= 50:
        return (
            f"【亲密度联动（当前 {v}/100：渐入佳境）】\n"
            "· 回复结尾自然延伸 1~2 句；\n"
            "· 约每 2~3 轮可自然提问一次；语气亲近但仍有分寸。"
        )
    if v >= 20:
        return (
            f"【亲密度联动（当前 {v}/100：还在熟悉）】\n"
            "· 回复相对简洁，延伸 0~1 句；\n"
            "· 偶尔（每 3 轮左右）一个自然的小问题即可，尊重边界。"
        )
    return (
        f"【亲密度联动（当前 {v}/100：初识）】\n"
        "· 回复简洁得体，不做长延伸，不主动追问私事。"
    )
