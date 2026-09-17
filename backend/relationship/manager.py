# -*- coding:utf-8 -*-
"""
关系管理器：
  提供关系状态的增删改查功能，以及每次聊天后自动更新关系状态。
"""
from datetime import datetime
from .database import conn
from .emotion import calculate_affection, calculate_intimacy, calculate_trust, detect_emotion
from .evolution import calculate_stage
# 注意：SemanticState 改为函数内部导入，避免循环导入


class RelationshipManager:
    """关系管理器：提供关系状态的增删改查功能"""

    def _manual_lock_key(self, user_id, character_id="default"):
        """手动数值的存储键。★ 2026-09-15 改为**角色级**（不再带 user_id/session）。

        用户拍板：手动调的亲密度/好感/信任/阶段要按"角色"存一份，换会话、换 QQ
        绑定、重装都不该丢 —— 原键 `relationship_manual:{user_id}:{character_id}`
        把 session_id 混在里面，session 一变（或 QQ 重绑）读不到旧值 → 界面显示
        重置。新键只认角色；旧键在 _load_manual_lock 里做一次性迁移读取。
        """
        return f"relationship_manual:char:{character_id}"

    def _manual_lock_key_legacy(self, user_id, character_id="default"):
        """历史键（带 user_id），只用于首次迁移读取，不再写入。"""
        return f"relationship_manual:{user_id}:{character_id}"

    def _load_manual_lock(self, user_id, character_id="default"):
        try:
            import json
            from .. import db as _db
            raw = _db.kv_get(self._manual_lock_key(user_id, character_id))
            if raw:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            # 一次性迁移：老键有值就搬到角色级新键，之后只读新键
            legacy = _db.kv_get(self._manual_lock_key_legacy(user_id, character_id))
            if legacy:
                data = json.loads(legacy)
                if isinstance(data, dict) and data:
                    try:
                        _db.kv_set(self._manual_lock_key(user_id, character_id),
                                   json.dumps(data, ensure_ascii=False))
                        print("[RelationshipManager] 手动关系数值已迁移到角色级键: %s"
                              % self._manual_lock_key(user_id, character_id), flush=True)
                    except Exception:
                        pass
                    return data
            return {}
        except Exception:
            return {}

    def _save_manual_lock(self, user_id, character_id="default", fields=None):
        try:
            import json
            from .. import db as _db
            payload = self._load_manual_lock(user_id, character_id)
            payload.update(fields or {})
            payload["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _db.kv_set(
                self._manual_lock_key(user_id, character_id),
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            pass

    def create_user(
        self,
        user_id,
        character_id="default"
    ):
        """创建用户关系记录（如果不存在）"""
        db = conn()

        db.execute(
            """
            INSERT OR IGNORE INTO relationship_state
            (user_id, character_id)
            VALUES(?, ?)
            """,
            (user_id, character_id)
        )

        db.commit()
        db.close()

    def _apply_manual(self, state, user_id, character_id="default"):
        """把手动设定的数值叠加到状态上（**手动优先**）。

        ★ 2026-09-15：自动成长已砍，数据库里那一行可能是任何历史遗留值（甚至被重打包
        重置成 stranger/0）。用户手调的数值存在 kv（角色级键）里，是唯一真相 ——
        所有读路径（提示词、界面、纪念日、主动消息）都必须看到它，否则会出现
        「界面设了恋人，AI 还按陌生人说话」。只在有手动值时覆盖，没设过就原样返回。
        """
        try:
            manual = self._load_manual_lock(user_id, character_id) or {}
            if not manual:
                return state
            for _k in ("intimacy", "affection", "trust", "interaction_days",
                       "stage", "nickname", "city", "conflict_state"):
                if manual.get(_k) is not None:
                    state[_k] = manual[_k]
            state["is_manual"] = True
        except Exception:
            pass
        return state

    def get_state(
        self,
        user_id,
        character_id="default",
        create=True,
    ):
        """获取用户关系状态（手动设定的数值优先，见 _apply_manual）"""
        db = conn()

        row = db.execute(
            """
            SELECT *
            FROM relationship_state
            WHERE user_id=?
            AND character_id=?
            """,
            (user_id, character_id)
        ).fetchone()

        db.close()

        if row:
            return self._apply_manual(dict(row), user_id, character_id)

        # 修复：不要递归，直接创建后返回默认值
        if create:
            try:
                self.create_user(user_id, character_id)
            except Exception:
                pass

        # 返回默认状态，不递归
        return self._apply_manual({
            "user_id": user_id,
            "character_id": character_id,
            "stage": "stranger",
            "intimacy": 0,
            "affection": 50,
            "trust": 0,
            "interaction_days": 0,
            "last_interaction": None,
            "nickname": "",
            "conflict_state": ""
        }, user_id, character_id)

    def update(
        self,
        user_id,
        character_id="default",
        **kwargs
    ):
        """更新用户关系状态"""
        _skip_legacy_bridge = bool(kwargs.pop("_skip_legacy_bridge", False))
        _manual_override = bool(kwargs.pop("_manual_override", False))
        db = conn()

        fields = []
        values = []

        for k, v in kwargs.items():
            fields.append(f"{k}=?")
            values.append(v)

        # 更新时间
        fields.append("updated_at=?")
        values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        values.append(user_id)
        values.append(character_id)

        if fields:
            db.execute(
                f"""
                UPDATE relationship_state
                SET {','.join(fields)}
                WHERE user_id=?
                AND character_id=?
                """,
                values
            )

        db.commit()
        db.close()


        manual_fields = {}
        for _k in ("intimacy", "affection", "trust", "interaction_days", "stage"):
            if _k in kwargs:
                manual_fields[_k] = kwargs.get(_k)
        if _manual_override and manual_fields:
            self._save_manual_lock(user_id, character_id, manual_fields)

        if _skip_legacy_bridge:
            return

        # 桥接新关系表 → 旧关系状态表。旧表仍被部分上下文/真人感模块读取，
        # 手动设置关系后必须同步过去，避免前端显示恋人但提示词仍按陌生/默认处理。
        try:
            from .. import db as _main_db
            bridge = {}
            if "intimacy" in kwargs:
                bridge["closeness"] = max(0, min(100, int(float(kwargs.get("intimacy") or 0))))
            if "trust" in kwargs:
                bridge["trust"] = max(0, min(100, int(float(kwargs.get("trust") or 0))))
            if "stage" in kwargs:
                bridge["stage"] = kwargs.get("stage") or ""
            if bridge:
                _main_db.update_relationship_state(
                    user_id, character_id, _skip_new_bridge=True, **bridge
                )
        except Exception as _bridge_e:
            print(f"[RelationshipManager] 同步旧关系表失败(静默): {_bridge_e}", flush=True)

    def increment_interaction(
        self,
        user_id,
        character_id="default"
    ):
        """增加互动场次计数（如果是新的一天才 +1）。

        注意语义：interaction_days 实为"互动天数/场次"——同一天多次聊天只计 1，
        跨天才累加，所以它近似"来聊过的不同日子数"，并非真实日历跨度。
        真实相识跨度请用 get_tenure_days()（基于 created_at 计算日历天数）。
        """
        state = self.get_state(user_id, character_id)
        today = datetime.now().strftime("%Y-%m-%d")
        last = state.get("last_interaction", "")
        manual_lock = self._load_manual_lock(user_id, character_id)

        if not last or last[:10] != today:
            if "interaction_days" in manual_lock:
                self.update(
                    user_id,
                    character_id,
                    last_interaction=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                )
                return
            new_days = (state.get("interaction_days", 0) or 0) + 1
            self.update(
                user_id,
                character_id,
                interaction_days=new_days,
                last_interaction=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )
        else:
            self.update(
                user_id,
                character_id,
                last_interaction=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )

    def get_tenure_days(
        self,
        user_id,
        character_id="default"
    ):
        """真实相识日历天数（不是聊天次数）。

        直接用已有 created_at 列计算，不碰原表、不加列。
        比 interaction_days（互动场次）更准确地反映"养了多久"。
        """
        try:
            db = conn()
            row = db.execute(
                "SELECT created_at FROM relationship_state WHERE user_id=? AND character_id=?",
                (user_id, character_id)
            ).fetchone()
            db.close()
            if row and row[0]:
                # SQLite CURRENT_TIMESTAMP 形如 '2025-01-01 12:30:45'，
                # 个别环境可能带毫秒 '.123'，先剥掉再解析
                raw = str(row[0]).split(".")[0]
                d0 = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
                return max(0, (datetime.now() - d0).days)
        except Exception:
            pass
        return 0

    def set_nickname(
        self,
        user_id,
        nickname,
        character_id="default"
    ):
        """设置用户昵称"""
        self.update(user_id, character_id, nickname=nickname)

    def set_conflict(
        self,
        user_id,
        conflict_state,
        character_id="default"
    ):
        """设置冲突状态"""
        self.update(user_id, character_id, conflict_state=conflict_state)

    def reset(
        self,
        user_id,
        character_id="default"
    ):
        """重置关系状态"""
        db = conn()

        db.execute(
            """
            DELETE FROM relationship_state
            WHERE user_id=?
            AND character_id=?
            """,
            (user_id, character_id)
        )

        db.commit()
        db.close()

        self.create_user(user_id, character_id)


async def update_relationship(
    user_id,
    message,
    character_id="default",
    semantic=None
):
    """
    每次聊天后更新关系状态。
    1. 计算好感度变化
    2. 计算亲密度变化
    3. 计算信任度变化
    4. 更新关系阶段
    5. 更新互动天数

    v2.0：支持传入 SemanticState，基于语义理解计算；
          不传则自动降级为词表匹配。
    """
    # 延迟导入，避免循环导入
    try:
        from ..semantic import SemanticState
    except Exception:
        pass

    manager = RelationshipManager()

    # 确保用户存在
    manager.create_user(user_id, character_id)

    state = manager.get_state(user_id, character_id)

    # ★ 自然衰减：久不联系关系会降温（>3天开始，每天 -1.5，最多 -10）。
    # 用“结算截止日期”做幂等，避免同一段离线时间每次聊天都重复扣分。
    from datetime import datetime as _dt
    _last = state.get("last_interaction")
    if _last:
        try:
            _gap_days = (_dt.now() - _dt.strptime(str(_last)[:19], "%Y-%m-%d %H:%M:%S")).days
        except Exception:
            _gap_days = 0
        if _gap_days > 3:
            from .. import db as _db
            _today = _dt.now().date()
            _decay_key = f"intimacy_decay_applied_until:{user_id}:{character_id}"
            _applied_raw = _db.kv_get(_decay_key)
            try:
                _applied = _dt.strptime(str(_applied_raw)[:10], "%Y-%m-%d").date() if _applied_raw else None
            except Exception:
                _applied = None
            _effective_end = _today
            # 首次结算按离线间隔扣分；随后只在跨自然日时扣新增的天数。
            _days_to_charge = max(0, (_effective_end - _dt.strptime(str(_last)[:10], "%Y-%m-%d").date()).days - 3)
            _already_charged = 0 if _applied is None else max(0, (_applied - _dt.strptime(str(_last)[:10], "%Y-%m-%d").date()).days - 3)
            _new_days = max(0, _days_to_charge - _already_charged)
            _decay = min(10, int(_new_days * 1.5))
            if _decay:
                state["intimacy"] = max(0, int(state.get("intimacy", 0) or 0) - _decay)
            if _new_days > 0 or _applied is None:
                _db.kv_set(_decay_key, _today.strftime("%Y-%m-%d"))

    # 计算各项指标（支持 semantic 参数）
    affection = calculate_affection(
        message,
        state.get("affection", 50) or 50,
        semantic
    )

    intimacy = calculate_intimacy(
        message,
        state.get("intimacy", 0) or 0,
        semantic
    )

    # ★ 每日亲密度增长上限（防刷：一直发"爱你"不会无限涨）
    try:
        from ..db import kv_get, kv_set
        _today = _dt.now().strftime("%Y-%m-%d")
        _daily_key = f"intimacy_daily:{user_id}:{character_id}:{_today}"
        _today_gain = int(kv_get(_daily_key) or 0)
        _base = int(state.get("intimacy", 0) or 0)
        _gain = intimacy - _base
        # ★ 关系升温速度（快热/慢热调节）：慢热=每天上限低(有成就感) / 正常=15 / 快热=30
        _DAILY_CAP = 15
        try:
            from .. import config as _config
            _pace = _config.get("RELATIONSHIP_PACE", "normal")
            _DAILY_CAP = {"slow": 8, "normal": 15, "fast": 30}.get(_pace, 15)
        except Exception:
            pass
        if _gain > 0 and _today_gain >= _DAILY_CAP:
            intimacy = _base  # 已到今日上限，不再增长
        elif _gain > 0:
            _capped = min(_gain, _DAILY_CAP - _today_gain)
            intimacy = _base + _capped
            kv_set(_daily_key, _today_gain + _capped)
    except Exception as _ie:
        pass

    trust = calculate_trust(
        message,
        state.get("trust", 0) or 0,
        semantic
    )

    stage = calculate_stage(intimacy)

    # 检测情绪（支持 semantic 参数）
    emotion = detect_emotion(
        message,
        semantic
    )

    # 更新状态
    manual_lock = manager._load_manual_lock(user_id, character_id)
    update_kwargs = {
        "affection": affection,
        "intimacy": intimacy,
        "trust": trust,
        "stage": stage,
    }
    if manual_lock:
        for _k in ("affection", "intimacy", "trust", "stage"):
            if _k in manual_lock:
                update_kwargs[_k] = manual_lock[_k]

    manager.update(
        user_id,
        character_id,
        affection=update_kwargs["affection"],
        intimacy=update_kwargs["intimacy"],
        trust=update_kwargs["trust"],
        stage=update_kwargs["stage"],
        _skip_legacy_bridge=True
    )
    try:
        from .. import db as _main_db
        _main_db.kv_set(f"intimacy:{user_id}:{character_id}", update_kwargs["intimacy"])
    except Exception:
        pass

    # ★ 事件溯源（2026-09-13）：把"这次互动让关系改变了多少"作为增量记进时间线。
    #   对应"缘起"原则——状态不是凭空变的，每一点信任/亲密/好感的增减都应该
    #   能追溯到具体事件。节流：无里程碑级变化（阶段跨越）时，每 6 小时最多记一条；
    #   数值变化都不足 1 时完全不记（避免每次寒暄都刷事件）。
    try:
        _deltas = {}
        for _k in ("affection", "intimacy", "trust"):
            try:
                _old_v = int(float(state.get(_k, 0) or 0))
                _new_v = int(float(update_kwargs.get(_k, 0) or 0))
                if _new_v - _old_v != 0:
                    _deltas[_k] = _new_v - _old_v
            except Exception:
                pass
        _stage_changed = str(update_kwargs.get("stage") or "") != str(state.get("stage") or "")
        if _deltas or _stage_changed:
            _cooldown_key = f"state_delta_last:{user_id}:{character_id}"
            from .. import db as _main_db2
            _last_ts = str(_main_db2.kv_get(_cooldown_key) or "")
            _now_s = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            _hours_since = 1e9
            if _last_ts:
                try:
                    _hours_since = (_dt.now() - _dt.strptime(_last_ts[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600.0
                except Exception:
                    pass
            if _stage_changed or _hours_since >= 6.0:
                # 事件原因：语义分析能给出情绪信号时带一句，便于日后追溯
                _reason = ""
                try:
                    if semantic is not None:
                        _reason = str(getattr(semantic, "primary_emotion", "") or "")
                except Exception:
                    _reason = ""
                _payload = dict(_deltas)
                if _stage_changed:
                    _payload["stage"] = f"{state.get('stage') or '—'}→{update_kwargs.get('stage') or '—'}"
                if _reason:
                    _payload["reason"] = _reason
                _main_db2.add_timeline_event(
                    user_id, character_id,
                    event_type="state_change",
                    title="关系状态变化",
                    description=("；".join(
                        f"{_k} {_v:+d}" if isinstance(_v, int) else f"{_k} {_v}"
                        for _k, _v in _payload.items()
                    )) or "阶段变化",
                    importance=6 if _stage_changed else 4,
                    state_delta=_payload,
                )
                _main_db2.kv_set(_cooldown_key, _now_s)
    except Exception as _sde:
        print(f"[Relationship] 状态增量事件记录失败(静默): {_sde}", flush=True)

    # 更新互动天数
    manager.increment_interaction(user_id, character_id)

    # ★ 新增：里程碑检测（更新完状态后检测）
    new_state = manager.get_state(user_id, character_id)
    _check_and_save_milestone(user_id, character_id, new_state)

    return {
        "stage": update_kwargs["stage"],
        "intimacy": update_kwargs["intimacy"],
        "affection": update_kwargs["affection"],
        "trust": update_kwargs["trust"],
        "emotion": emotion
    }


def _check_and_save_milestone(user_id, character_id, state):
    """
    检测关系里程碑并存库。
    亲密度每到一个新阶段门槛时保存一条里程碑记录。
    防重：同一门槛只存一次（用 kv 记录已触发的门槛）。
    """
    from ..db import kv_get, kv_set
    from .database import conn

    THRESHOLDS = [
        (20,  "初识时刻",   "你们正式成为朋友了"),
        (40,  "亲密升温",   "彼此越来越亲近"),
        (70,  "恋人关系",   "感情进入恋人阶段"),
        (90,  "灵魂契合",   "两颗心几乎完全融合"),
    ]

    intimacy = int(state.get("intimacy", 0) or 0)

    for threshold, title, content in THRESHOLDS:
        kv_key = f"ms_triggered:{user_id}:{character_id}:{threshold}"
        already = kv_get(kv_key)
        if already:
            continue
        if intimacy >= threshold:
            try:
                db = conn()
                db.execute(
                    """
                    INSERT INTO milestone_memory
                    (user_id, character_id, event_type, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, character_id, title, content)
                )
                db.commit()
                db.close()
                kv_set(kv_key, "1")
                print(
                    f"[Milestone] 里程碑解锁: {user_id}/{character_id} → {title}",
                    flush=True
                )
            except Exception as e:
                print(f"[Milestone] 存库失败(静默): {e}", flush=True)
