# -*- coding:utf-8 -*-
"""
记忆大脑：
  记忆生命周期管理，包括衰减计算、冲突检测、智能插入、低价值清理。
  让长期记忆系统更智能，自动淘汰低价值记忆，保留重要记忆。
"""
import json
import math
from datetime import datetime

from . import db, config
from .deepseek_api import chat_once

# ══════════════════════════════════════════════════════════════════
# 冲突检测触发门槛（2026-09-13 实测标定）
#
# 旧值 0.85 对"记忆"这种短文本形态是**根本性错误**，实测数据：
#   · 「TA把AI部署到了本地，运行在TA的4060显卡上。」
#     vs「TA曾尝试本地部署AI但未成功…显存只有8G。」   → 0.2333  ← 明确冲突
#   · 「AI住在用户的4060里。」vs「AI运行在用户的4060上。」 → 0.8148  ← 同义改写
#   · 「用户喜欢喝奶茶。」vs「TA曾尝试本地部署AI但未成功。」 → 0.0833  ← 无关
# 0.85 的后果：真冲突对（0.23）**连检测都不会被调用**，同义对（0.81）也判不出重复。
# 而实测 LLM 本身判得很准 —— 部署① 判 conflict/keep=new、否定对照片判 conflict、
# 无关与同义都判不冲突。所以瓶颈只在门槛，不在模型。
# 取 0.20：高于"无关对"（0.083），低于"真冲突对"（0.2333），
# 代价是检测调用变多（每条新记忆多几次 LLM），换来矛盾记忆真能被作废。
# ══════════════════════════════════════════════════════════════════
CONFLICT_GATE = 0.20
# 每次插入时，取多少条"最相近"的旧记忆做冲突候选
CONFLICT_CANDIDATES = 25
# 单次批量冲突判定最多带几条进 prompt（太多会稀释注意力，也容易超 token）
CONFLICT_BATCH = 20

# ══════════════════════════════════════════════════════════════════
# ★ Q_recall 闭环（2026-09-13）：记忆可信度初值表
#
#   对应"识海"思路里的 Q_recall = ΣEvidenceWeight / (1+InferenceAmount)：
#   记忆不该默认全信。这里给每类记忆一个**初始可信度**（LLM 提炼本质是
#   对原话的一次"转述"，转述就有失真概率，类型越主观失真风险越高）。
#   · 写入侧：smart_insert / dedupe_insert 按 type 取初值，有 source_text
#     （溯源原话=原始证据）的加 0.05 —— 证据越足越可信，上限 0.98；
#   · 读取侧：search_memories 用 _confidence_factor 把低置信记忆温和降权；
#   · 注入侧：memory_block 给 conf<0.7 的记忆标「模糊印象」，让模型表达时
#     自带不确定性（"我印象里好像是…"），而不是把转述当铁律输出。
#   旧库存量行 confidence=1.0，所有因子都设计成 1.0 → 不改变行为。
# ══════════════════════════════════════════════════════════════════
TYPE_CONFIDENCE = {
    "relationship": 0.95,   # 关系记忆：高置信
    "rule":         0.95,   # 用户亲口立的行为规则：与关系同级（「记住：」直写路径）
    "emotion":      0.90,   # 情感记忆：高置信
    "event":        0.85,   # 事件记忆：较高置信
    "episode":      0.85,   # 经历/约定记忆：与 event 同置信
    "preference":   0.85,   # 偏好记忆：较高置信
    "fact":         0.75,   # 事实记忆：中等置信
    "general":      0.60,   # 普通记忆：低置信
}
TYPE_CONFIDENCE_DEFAULT = 0.75
# 溯源原话（原始证据）带来的可信度加成
SOURCE_EVIDENCE_BONUS = 0.05
# 低于该值的记忆在注入时被标注为"模糊印象"（输出侧措辞不确定）
LOW_CONFIDENCE_HEDGE = 0.70


def initial_confidence(memory_type: str, source_text: str = "") -> float:
    """按类型取初始可信度；有溯源原话（原始证据）时加分，封顶 0.98（永远留一点转述失真的余地）。"""
    conf = TYPE_CONFIDENCE.get(str(memory_type or ""), TYPE_CONFIDENCE_DEFAULT)
    if str(source_text or "").strip():
        conf += SOURCE_EVIDENCE_BONUS
    return round(min(0.98, conf), 2)


async def detect_conflict_batch(new_memory: str, olds: list, character_id="default"):
    """**一次调用**判定新记忆与多条旧记忆的关系。

    ★ 2026-09-13 为什么改成批量：实测"部署"这一个主题下有 31 条相似记忆，
      逐条调用 = 31 次 LLM；而且旧的逐条循环遇到第一条就 `break`，
      **结构上只能作废一条** —— 但"部署成功"这类错误事实有十几条，
      必须能一次作废多条。批量判定把成本压到 1 次调用，并且天然支持多作废。

    返回 {'supersede': [下标...], 'duplicate': [下标...], 'reason': str}
    失败/没 key 返回 None（调用方必须与"判定为空"区分开）。
    """
    items = [str(o.get("memory_content") or "") for o in (olds or [])]
    if not items:
        return {"supersede": [], "duplicate": [], "reason": ""}
    key = config.api_key_for_model(config.memory_extract_model(character_id))
    if not key:
        return None

    numbered = "\n".join("%d. %s" % (i + 1, t) for i, t in enumerate(items))
    prompt = (
        "下面是一条【新记忆】和若干条【已有记忆】（关于同一个用户/AI）。\n"
        "请**严格地**判断：哪些已有记忆被新记忆**取代**了，哪些是**重复**的。\n\n"
        "【新记忆】\n%s\n\n"
        "【已有记忆】\n%s\n\n"
        "【supersede 的严格定义】只有满足**全部**条件才放进去：\n"
        "  1) 旧记忆**明确断言了某件事已经发生/已经完成/当前就是这个状态**；\n"
        "  2) 新记忆明确说了**相反**的结论；\n"
        "  3) 二者说的是**同一件事**（不是相关话题）。\n"
        "  例：旧「AI已经部署到本地了」+ 新「本地部署没成功」→ supersede。\n\n"
        "【绝对不要放进 supersede 的情况】\n"
        "  · 计划/打算/考虑/想要（「打算本地部署」「考虑以后部署」）→ 只是意图，没有被取代；\n"
        "  · 感受/担心/希望/约定（「担心显卡带不动」「约定上线第一句话由AI教」）→ 与结论无关；\n"
        "  · 同一件事的过程记录（「凌晨四点还在部署」）→ 过程真实发生过；\n"
        "  · 主题只是相关（都提到部署）但说的不是同一件事。\n\n"
        "【duplicate 的定义】旧记忆与新记忆说的是**同一件事实**，措辞不同、不矛盾，"
        "留一条即可。\n\n"
        "保守优先：**拿不准就不要放**。宁少勿多 —— 错删一条真实记忆的代价，"
        "比漏掉一条过时记忆大得多。\n\n"
        "只输出 JSON，不要解释：\n"
        '{"supersede":[序号...],"duplicate":[序号...],"reason":"一句话"}'
        % (str(new_memory or "")[:400], numbered)
    )
    try:
        out = await chat_once(
            config.memory_extract_model(character_id),
            [{"role": "user", "content": prompt}],
            key, temperature=0.1, max_tokens=800,
        )
    except Exception as e:
        print(f"[MemoryBrain] 批量冲突判定失败: {e}", flush=True)
        return None

    raw = str(out or "").strip()
    try:
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
    except Exception:
        import re as _re
        m = _re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except Exception:
            return None
    if not isinstance(data, dict):
        return None

    def _idx(v):
        out = []
        for x in (v or []):
            try:
                i = int(x) - 1          # prompt 用 1-based
            except Exception:
                continue
            if 0 <= i < len(items):
                out.append(i)
        return sorted(set(out))

    return {
        "supersede": _idx(data.get("supersede")),
        "duplicate": _idx(data.get("duplicate")),
        "reason": str(data.get("reason") or "")[:200],
    }


def _candidate_memories(content, session_id, character_id) -> list:
    """取「可能与新记忆冲突」的候选旧记忆。

    ★ 2026-09-13 新增。原先用 `db.valid_memories()`，而它是
      `ORDER BY importance DESC, id DESC LIMIT 200` —— 只给出最重要的 200 条。
      该角色有 3300+ 条有效记忆，于是**与新记忆最像的那条只要不在前 200，
      就永远不会被比较**（实测：与新记忆相似度 0.4333 的同义条正好不在其中，
      冲突检测因此从没跑到过它）。
      这里改为按语义取候选：先向量检索最相近的 N 条，再回主库补全字段；
      向量不可用（未装 chromadb / 库损坏）时**回退** valid_memories，
      保证行为不退化。
    """
    cands = []
    # ① 语义候选（不受 importance 排序影响）
    try:
        from .memory.embedding import encode
        from .memory.vector_store import search_vector
        vec = encode(str(content or "")[:512])
        if vec:
            hits = search_vector(
                user_id=session_id,
                embedding=vec,
                limit=max(1, int(CONFLICT_CANDIDATES)),
                character_id=character_id,
            ) or []
            for h in hits:
                mid = h.get("id")
                if mid is None:
                    continue
                s = str(mid)
                if s.startswith("sql:"):
                    s = s[4:]
                try:
                    row = db.get_memory(int(s))
                except Exception:
                    row = None
                if row and int(row.get("is_valid", 1) or 0) == 1:
                    cands.append(row)
    except Exception:
        cands = []

    # ② 语义候选不可用 / 太少 → 回退原有取法（保证不退化）
    #   注意：valid_memories 不接受 limit 参数（内部固定 LIMIT 200），别传。
    if len(cands) < 5:
        try:
            fallback = db.valid_memories(
                session_id=session_id, character_id=character_id
            ) or []
        except Exception:
            fallback = []
        seen = {c.get("id") for c in cands}
        for m in fallback:
            if m.get("id") not in seen:
                cands.append(m)
            if len(cands) >= 200:
                break

    return cands


def memory_age_score(
    memory
):
    """
    记忆年龄评分：越新的记忆得分越高。
    使用指数衰减，半衰期约180天。
    ★ 主观时间（2026-09-13）：年龄按"心里的钟"折算——冲突/低落/久等时
      主观流速快（淡得快），高亲密度时流速慢（重要的人的记忆更保鲜）。
      读失败回落物理天数，行为不变。
    """
    ts = (
        memory.get("last_used")
        or
        memory.get("create_time")
    )

    if not ts:
        return 0.5

    try:
        dt = datetime.strptime(
            ts,
            "%Y-%m-%dT%H:%M:%S"
        )
    except Exception:
        return 0.5

    days = (
        datetime.now()
        -
        dt
    ).days

    try:
        from . import subjective_time
        days = subjective_time.subjective_days(
            days,
            str(memory.get("session_id") or "default"),
            str(memory.get("character_id") or "default"),
        )
    except Exception:
        pass

    return math.exp(
        -days / 180
    )


def calculate_decay(
    memory
):
    """
    计算记忆的综合衰减分数（0~1，越小越该退场）。

    ★ 2026-09-17 重构（原公式让"遗忘"在系统里不存在）：
      原式 = importance×0.5 + access×0.2 + age×0.3。
      importance/10 一项就占 0.5，于是**任何 importance≥6 的记忆分数永远 ≥0.3**，
      而 age 用半衰期 180 天的指数（一年前的老记忆仍有 0.13 的加成），
      实测线上最小值恒为 0.5123、2823/3790 恒为 1.0，
      归档阈值 0.25 永远达不到 —— "低价值淘汰/遗忘"这条链路等于没实现。

      新式 = 年龄衰减（按类型定半衰期）× 重要度因子 × 访问因子，三者相乘：
        · age  = e^(-λ·days)，λ 按类型（关系/情感慢、通用事实快）
        · 重要度因子：importance 越高，衰减越慢（1 分的记忆衰减最快）
        · 访问因子：被用过/被想起过的记忆整体上浮（复述巩固）
      这样"一年前、从没被用过、也不重要的流水记忆"才能真的沉下去；
      而 importance=10 且经常被想起的的核心记忆，分数依然保持在 0.5 以上。

    ★ 访问次数取 access_count 与 recall_count 的较大者：本项目的召回统计
      只写 access_count（db.touch_recall），而历史数据里 recall_count 才是
      真正被累加过的那个，两个都读才不会误判"从没被用过"。
    """
    importance = max(1, min(10, int(memory.get("importance", 5) or 5)))
    access = max(int(memory.get("access_count", 0) or 0),
                 int(memory.get("recall_count", 0) or 0))

    # ① 年龄衰减：按记忆类型定半衰期
    _LAMBDA = {
        "relationship": 0.0025,
        "emotion": 0.0040,
        "preference": 0.0050,
        "fact": 0.0090,
        "episode": 0.0120,
        "event": 0.0120,
        "behavior_rule": 0.0020,
        "general": 0.0140,
    }
    lam = _LAMBDA.get(str(memory.get("memory_type") or "fact"), 0.0120)

    # ② 重要度因子：importance 1 → 0.75 倍速衰减；10 → 0.25 倍速
    imp_factor = 1.25 - 0.10 * importance          # 1→1.15, 10→0.25
    effective_lambda = lam * max(0.25, imp_factor)

    raw = _decay_raw_by_days(memory, effective_lambda)

    # ③ 访问因子：用过就上浮（最多 +0.35），但绝不把老记忆顶回 1.0
    access_bonus = min(0.35, access * 0.035)
    return round(max(0.0, min(1.0, raw + access_bonus * (1.0 - raw))), 4)


def _decay_raw_by_days(memory, lam: float) -> float:
    """按"最后使用/创建时间"算 e^(-λ·days)（主观时间折算同 memory_age_score）。"""
    from math import exp
    ts = memory.get("last_used") or memory.get("create_time")
    if not ts:
        return 0.8
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        # 真机时间戳形如 2026-09-17T00:57:13；两种格式都容错
        try:
            dt = datetime.fromisoformat(str(ts)[:19])
        except Exception:
            return 0.8
    days = max(0, (datetime.now() - dt).days)
    try:
        from . import subjective_time
        days = subjective_time.subjective_days(
            days,
            str(memory.get("session_id") or "default"),
            str(memory.get("character_id") or "default"),
        )
    except Exception:
        pass
    return exp(-lam * days)


def refresh_memory_scores():
    """刷新**所有桶**有效记忆的衰减分数。

    ★ 2026-09-17 修（作用域错桶）：原先是 `db.valid_memories()` —— 不传参，
      默认 session='default'、character='default'，也就是只扫那个测试桶
      （真机实测只有 489 行、还被 LIMIT 200 砍到 200）；用户真实会话
      （3315 行）**从来不在衰减范围内**，所以线上 2823/3790 条 decay_score 恒为 1.0。
      现在用 all_active_memories() 走全库，按 id 分页避免一次性载入。
    """
    n = 0
    for mem in all_active_memories():
        try:
            score = calculate_decay(mem)
            db.q("UPDATE long_term_memory SET decay_score=? WHERE id=?",
                 (score, mem["id"]))
            n += 1
        except Exception as _e:  # noqa: BLE001
            print(f"[MemoryBrain] 衰减刷新失败 id={mem.get('id')}: {_e}", flush=True)
    return n


def all_active_memories(batch: int = 500):
    """全库有效记忆（跨所有 session/character），分批产出。

    为什么需要它：清理/衰减/重评这类**全局维护任务**以前都走
    `db.valid_memories()`，而它的默认桶是 (default, default) —— 维护任务
    因此只作用在测试桶上，真实记忆永远得不到维护。
    """
    last_id = 0
    while True:
        rows = db.q(
            "SELECT * FROM long_term_memory WHERE is_valid=1 AND id>? "
            "ORDER BY id ASC LIMIT ?",
            (last_id, int(batch)), fetch=True) or []
        if not rows:
            return
        for r in rows:
            d = dict(r)
            last_id = int(d["id"])
            yield d
        if len(rows) < batch:
            return


async def detect_conflict(
    old_memory,
    new_memory,
    character_id="default",
):
    """
    检测两个记忆是否冲突。
    返回：{conflict: bool, reason: str, keep: 'old'/'new'}
    """
    key = config.api_key_for_model(config.memory_extract_model(character_id))

    if not key:
        return None

    prompt = f"""
判断两个用户记忆是否冲突。

旧记忆：

{old_memory}

新记忆：

{new_memory}

输出JSON：

{{
"conflict":true/false,
"reason":"",
"keep":"old/new"
}}
"""

    result = await chat_once(
        config.memory_extract_model(character_id),
        [
            {
                "role": "user",
                "content": prompt
            }
        ],
        key,
        temperature=0.1,
        max_tokens=200
    )

    try:
        return json.loads(
            result
        )
    except Exception:
        return None


def _sync_vector_for_memory(memory_id, content, user_id, character_id,
                            memory_type="fact", importance=5) -> bool:
    """给刚写入主库的记忆同步一条向量。

    ★ P0-4：主库 long_term_memory 是唯一权威表，向量只是「可重建的索引」。
      写失败只会降低语义检索的召回率，绝不能影响记忆本身和对话主流程，
      所以这里全程吞异常、只打日志。
    """
    try:
        from .memory.embedding import encode
        # ★ P0-4：用 upsert_vector（原子、同 id 幂等），不用 add_vector ——
        #   重复同步同一条记忆时 add 会留下重复/失败，upsert 直接覆盖。
        from .memory.vector_store import upsert_vector
        vec = encode(content)
        if not vec:
            return False
        upsert_vector(
            memory_id=f"sql:{memory_id}",
            user_id=user_id,
            content=content,
            embedding=vec,
            metadata={"type": memory_type, "importance": importance},
            character_id=character_id,
        )
        return True
    except Exception as e:
        print(f"[MemoryBrain] 同步向量失败(静默): {e}", flush=True)
        return False


def _drop_vector_for_memory(memory_id) -> None:
    """记忆作废时同步删掉它的向量，避免留下「搜得到、但主库已失效」的孤儿。"""
    try:
        from .memory.vector_store import delete_vector
        delete_vector(f"sql:{memory_id}")
    except Exception:
        pass


async def smart_insert(
    content,
    memory_type="fact",
    importance=5,
    session_id="default",
    character_id="default",
    context="",
    emotion_tag="",
    source_text="",
):
    """
    智能插入记忆：
    1. 与现有记忆做相似度检测
    2. 相似度>0.85时做冲突检测
    3. 冲突时保留新记忆，归档旧记忆
    4. 不冲突且不重复时插入新记忆
    ★ 按 (session_id, character_id) 隔离读写，保证各人格记忆不串桶。
    ★ 信息单元：context（情境）/ emotion_tag（情绪）/ source_text（溯源）一并入库。
    """
    from . import memory_manager

    # ★ 2026-09-13 修复（深层根因）：候选集不能再靠 valid_memories。
    #   实测：`valid_memories` 是 `WHERE is_valid=1 ORDER BY importance DESC, id DESC LIMIT 200`
    #   —— 只返回**最重要的 200 条**，而该角色有 3300+ 条有效记忆。
    #   后果：与新记忆几乎同义的那条（相似度 0.4333）如果不在前 200，就**永远不会被比较**，
    #   冲突检测再准也没机会跑。这正是「部署成功 / 部署失败」两套相反记忆
    #   长期并存、且都被判定为 active+confidence 1.0 的真正原因。
    #   改为：先用向量语义检索取出**最相近的候选**（不受 importance 排序影响），
    #   再在候选内做冲突判定 —— 这才是"跟可能冲突的那几条比"。
    old_memories = _candidate_memories(content, session_id, character_id)

    # ★ 只把"确实够相关"的送进 prompt（门槛实测见 CONFLICT_GATE 注释），
    #   并按相似度降序取前 CONFLICT_BATCH 条 —— 一次调用判全部，成本恒定。
    scored = []
    for old in old_memories:
        s = memory_manager._relevance(content, old["memory_content"])
        if s > CONFLICT_GATE:
            scored.append((s, old))
    scored.sort(key=lambda x: -x[0])
    batch = [o for _, o in scored[:CONFLICT_BATCH]]

    _checked = len(batch)
    _superseded = []
    _skipped_no_verdict = 0

    if batch:
        verdict = await detect_conflict_batch(content, batch, character_id=character_id)
        if verdict is None:
            # 没 key / 解析失败 —— 必须与"判定为空"区分：这次什么都没查成
            _skipped_no_verdict = len(batch)
        else:
            for i in verdict.get("supersede", []):
                old = batch[i]
                _drop_vector_for_memory(old["id"])   # 同步删向量，避免"搜得到但主库已失效"
                _superseded.append(old["id"])
            # 判定为"重复"（同一事实、措辞不同）且**没有需要作废的**时，
            # 新记忆没必要再存一条 —— 旧的那条已经把这件事说清楚了。
            if verdict.get("duplicate") and not _superseded:
                try:
                    print("[MemoryBrain] 新记忆与已有记忆重复(%d 条)，跳过插入"
                          % len(verdict["duplicate"]), flush=True)
                except Exception:
                    pass
                return

    # 诊断：只在真的查过时段打印，避免每条记忆都刷屏
    if _checked:
        try:
            print(
                "[MemoryBrain] 批量冲突判定: 送检 %d 条，判定需作废 %d 条%s%s"
                % (_checked, len(_superseded),
                   ("  id=%s" % _superseded) if _superseded else "",
                   ("  无结论(没key/解析失败) %d 条" % _skipped_no_verdict)
                   if _skipped_no_verdict else ""),
                flush=True,
            )
        except Exception:
            pass

    # ★ 记忆作用域分类：global(用户基础信息) / character(角色专属) / relationship(关系私密)
    #   异常兜底 character（角色隔离），绝不让分类失败的记忆掉进 global 跨角色共享
    try:
        from .memory.scope_classifier import classify_and_normalize
        _scope, _scope_cid = classify_and_normalize(memory_type, content, character_id)
    except Exception:
        _scope, _scope_cid = "character", character_id

    # ★ 事务：作废旧记忆与写入新记忆必须同生共死。
    #   两步分开提交的话，中间一旦出错就是"旧的作废了、新的没写进来"，
    #   用户会凭空少一条记忆，而且没有任何报错提示。
    #   ★ 2026-09-13：作废改为**一次多条** —— "部署成功"这类错误事实在库里
    #     有十几条，旧的 `break` 结构只能作废一条，剩下的继续误导她。
    new_id = 0
    with db.transaction():
        for _oid in _superseded:
            try:
                db.invalidate_memory(_oid, reason="superseded")
            except Exception as _ie:
                print(f"[MemoryBrain] 作废旧记忆 {_oid} 失败: {_ie}", flush=True)

        new_id = db.insert_memory(
            content,
            memory_type=memory_type,
            importance=importance,
            session_id=session_id,
            character_id=_scope_cid,
            memory_scope=_scope,
            # ★ Q_recall 闭环：主提炼路径不再拿默认 1.0（"全信"），
            #   按类型+溯源给差异化初值，读取/注入侧才有东西可消费
            confidence=initial_confidence(memory_type, source_text),
            context=context,
            emotion_tag=emotion_tag,
            source_text=source_text,
        )

    # ★ P0-4 补全：同步生成向量。
    #   之前 smart_insert 只写 SQLite，向量库里根本没有这条记忆 ——
    #   主流程存下来的记忆（身份质疑、自动提炼…）在 hybrid_search 的语义
    #   分支里永远检索不到。这是"两套记忆互不可见"的另一半。
    if new_id:
        _sync_vector_for_memory(
            memory_id=new_id,
            content=content,
            user_id=session_id,
            character_id=_scope_cid,
            memory_type=memory_type,
            importance=importance,
        )
    # ★ 与 dedupe_insert 对齐：写入后主动清 memory_block 的 session 级缓存，
    #   否则新记忆最长 30s 内不会被注入（dedupe 路径一直有这一步，smart_insert 漏了）
    try:
        memory_manager.invalidate_memory_cache(session_id, character_id)
    except Exception:
        pass
    return new_id


def clean_low_value_memory():
    """把低价值记忆**标记**为归档（衰减分数 < 阈值 且 重要性 < 7）。

    Memory Importance Engine v1.0：重要记忆永久保留。

    ★ 2026-09-17 三处修改：
      ① 作用域：改用 all_active_memories()（原 db.valid_memories() 只扫
         (default, default) 测试桶，真实记忆永远不被清理）。
      ② 阈值可配（config MEMORY_FORGET_DECAY）并区分"归档"与"遗忘"：
         < 0.25 → archived（降权、仍可被搜到）；< 0.10 → forgotten（基本退场）。
      ③ 用户拍板：**永不真删**。这里只调 db.invalidate_memory（标记 is_valid=0
         + memory_status），行始终留在表里，随时可查可恢复。
    """
    try:
        archive_at = float(config.get("MEMORY_ARCHIVE_DECAY") or 0.25)
    except Exception:
        archive_at = 0.25
    try:
        forget_at = float(config.get("MEMORY_FORGET_DECAY") or 0.10)
    except Exception:
        forget_at = 0.10

    archived = forgotten = 0
    for mem in all_active_memories():
        importance = int(mem.get("importance", 5) or 5)
        # 重要记忆（importance>=7）永久保留，不被清理
        if importance >= 7:
            continue
        # 被反复想起过的记忆不清理（复述巩固；access/recall 取大者）
        access = max(int(mem.get("access_count", 0) or 0),
                     int(mem.get("recall_count", 0) or 0))
        if access >= 10:
            continue
        score = calculate_decay(mem)
        try:
            if score < forget_at:
                db.q("UPDATE long_term_memory SET is_valid=0, memory_status='forgotten', "
                     "decay_score=? WHERE id=?", (score, mem["id"]))
                forgotten += 1
            elif score < archive_at:
                db.q("UPDATE long_term_memory SET is_valid=0, memory_status='archived', "
                     "decay_score=? WHERE id=?", (score, mem["id"]))
                archived += 1
        except Exception as _e:  # noqa: BLE001
            print(f"[MemoryBrain] 低价值清理失败 id={mem.get('id')}: {_e}", flush=True)
    if archived or forgotten:
        print(f"[MemoryBrain] 低价值记忆处理: 归档 {archived} / 遗忘 {forgotten}"
              f"（阈值 archive<{archive_at} forget<{forget_at}；只标记不删除）", flush=True)
    return {"archived": archived, "forgotten": forgotten}
