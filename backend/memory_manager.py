# -*- coding: utf-8 -*-
"""
记忆管理：
  1. 手动记忆：识别「记住：xxx」前缀，直接入库
  2. 自动提炼：每 N 轮对话（N 可配置）把最近对话发给 MEMORY_EXTRACT_MODEL（固定 flash）提炼
     → 提炼前经过 memory_filter 过滤（剔除问句/指令），输出经结构化校验+客观改写
  3. 去重 / 冲突覆盖 / 新增：与现有有效记忆做相似度比较（difflib），相似则更新，否则插入
  4. 记忆加载：拼接进 system prompt
提炼过程完全静默，不打断正常对话。
"""
import difflib
import hashlib
import json
import re
import time as _time
from datetime import datetime

from . import config, db
from . import memory_brain
from .deepseek_api import chat_once, ModelApiError
from . import memory_filter


def _reasoning_effort_for(model: str, level: str) -> str:
    """仅智谱 GLM-5.3 系列返回思考强度档位，其他 provider 返回 None（不传该参数）。"""
    try:
        if config.model_supports_reasoning_effort(model):
            return level
    except Exception:
        pass
    return None


SIMILAR_THRESHOLD = 0.72   # 相似度阈值：>= 视为同一条记忆（更新而非新增）

# ★ per-session 记忆候选缓存。
#   ★ 2026-09-17 修：缓存键**必须含当前提问的指纹**。旧键是 (session, character, boost)，
#     不含 query —— 真机后果：30 秒内换问题（"嗯嗯"→"好看吗"）会直接命中上一条问题的
#     缓存，把上一轮的记忆原样塞给新问题。实测活会话有 109 次相邻召回落在 30 秒内。
#     TTL 从 30s 收到 8s：缓存只用来吸收"同一轮里被调用两次"的重复检索
#     （实测每轮 memory_block 至少跑两次），不再承担跨问题的复用。
_memory_block_cache: dict = {}   # key=(session_id, character_id, boost, query_fp) → (text, expire_ts)
_MEMORY_CACHE_TTL = 8           # 秒（记忆写入时由 invalidate_memory_cache 主动清）


def _query_fingerprint(query, user_message: str = "") -> str:
    """提问指纹：归一化后取摘要。空提问与非空提问必须落到不同键上。"""
    raw = " ".join(str(x or "") for x in (query, user_message)).strip()
    if not raw:
        return "∅"
    norm = re.sub(r"\s+", "", raw)[:200]
    return hashlib.md5(norm.encode("utf-8", "replace")).hexdigest()[:12]

MANUAL_RE = re.compile(r"^\s*(?:记住|記住)\s*[:：]\s*(.+?)\s*$", re.S)


def is_manual_memory(text: str):
    m = MANUAL_RE.match(str(text or ""))
    if m:
        return m.group(1).strip()
    return None


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _delete_sql_vector(memory_id):
    try:
        from .memory.vector_store import delete_vector
        delete_vector(f"sql:{memory_id}")
    except Exception:
        pass


def forget_matching(query: str, session_id: str = "default", character_id: str = "default") -> int:
    """撤销当前用户/角色下最匹配的记忆，并同步清理向量索引。"""
    query = str(query or "").strip()
    if not query:
        return 0
    scored = []
    for mem in db.valid_memories(session_id=session_id, character_id=character_id):
        content = str(mem.get("memory_content", "") or "")
        score = 1.0 if query in content or content in query else _similarity(query, content)
        if score >= 0.45:
            scored.append((score, mem))
    if not scored:
        return 0
    scored.sort(key=lambda item: item[0], reverse=True)
    targets = [m for score, m in scored if score >= 0.85] or [scored[0][1]]
    for mem in targets:
        db.delete_memory(mem["id"])
        _delete_sql_vector(mem["id"])
    invalidate_memory_cache(session_id, character_id)
    return len(targets)


def _sync_vector_upsert(
    memory_id,
    session_id: str,
    character_id: str,
    content: str,
    memory_type: str = "fact",
    importance: int = 5
) -> bool:
    """
    写 SQLite 后同步写 ChromaDB。
    - memory_id 为 0/None → 跳过（insert_memory 失败）
    - 任何异常静默处理，不影响主链路

    ★ P0-4：改用 upsert_vector（原子），不再 delete + add。
      拆成两步时，delete 成功而 add 失败会留下"SQLite 有、向量没了"的静默漂移，
      且没有任何机制能补回来。upsert 一步到位，不存在中间态。

      记忆的权威始终在 SQLite，向量只是可重建的索引：这里失败只降低语义检索
      召回率，绝不能反向影响对话主流程，所以仍吞异常，但把结果返回给调用方打点。
    """
    if not memory_id:
        return False
    try:
        from .memory.vector_store import upsert_vector
        from .memory.embedding import encode as vec_encode

        vec = vec_encode(content)
        if not vec:
            return False

        # ★ 命名空间前缀：本模块写的是主库 long_term_memory（sql），
        #   避免与其它数据源的 autoincrement id（都从 1 起）在同一个 ChromaDB
        #   collection 里互相覆盖/误删。
        vec_id = f"sql:{memory_id}"

        return bool(upsert_vector(
            memory_id=vec_id,
            user_id=session_id,
            content=content,
            embedding=vec,
            metadata={
                "type": memory_type,
                "importance": importance,
                "character_id": character_id,
            },
            character_id=character_id
        ))
    except Exception as e:
        print(f"[MemoryManager] 向量同步失败(静默): {e}", flush=True)
        return False

def dedupe_insert(
    content: str,
    memory_type: str = "fact",
    importance: int = 5,
    session_id: str = "default",
    character_id: str = "default",
    memory_scope: str = None,
    context: str = "",
    emotion_tag: str = "",
    source_text: str = "",
) -> str:
    """
    去重写入。
    - 与现有有效记忆比较相似度，相似则更新旧记忆（以新内容为准）
    - 否则新增
    - 写 SQLite 后同步写 ChromaDB 向量库
    返回：'duplicate' | 'updated' | 'inserted' | 'skipped'
    """
    content = content.strip()
    if not content or len(content) < 2:
        return "skipped"

    # ── Memory Scope 判断
    if memory_scope is None:
        try:
            from .memory.scope_classifier import classify_and_normalize
            memory_scope, scope_character_id = classify_and_normalize(
                memory_type, content, character_id
            )
        except Exception:
            # ★ 防记忆串桶：分类器异常时兜底 character（角色隔离），不进 global
            memory_scope = "character"
            scope_character_id = character_id
    else:
        try:
            from .memory.scope import normalize_scope
            memory_scope = normalize_scope(memory_scope)
        except Exception:
            pass
        scope_character_id = "default" if memory_scope == "global" else character_id

    # ── 去重检查：遍历已有有效记忆
    for mem in db.valid_memories(session_id=session_id, character_id=character_id):
        if _similarity(content, mem["memory_content"]) >= SIMILAR_THRESHOLD:
            if content == mem["memory_content"]:
                return "duplicate"
            # 相似但内容不同 → 以新内容为准，更新旧条目
            db.update_memory(mem["id"], content)
            # ★ 向量库同步更新
            _sync_vector_upsert(
                memory_id=mem["id"],
                session_id=session_id,
                character_id=character_id,
                content=content,
                memory_type=memory_type,
                importance=importance
            )
            invalidate_memory_cache(session_id, character_id)
            return "updated"

    # ── 新增
    # ★ Q_recall 闭环：置信度初值表统一收敛到 memory_brain.TYPE_CONFIDENCE
    #   （此前这里和 smart_insert 各一套口径，smart_insert 还根本没在用）
    initial_confidence = memory_brain.initial_confidence(memory_type, source_text)

    new_id = db.insert_memory(
        content,
        category="general",
        memory_type=memory_type,
        importance=importance,
        session_id=session_id,
        character_id=scope_character_id,
        memory_scope=memory_scope,
        confidence=initial_confidence,   # ★ 新增
        context=context,
        emotion_tag=emotion_tag,
        source_text=source_text,
    )
    # ★ 向量库同步写入
    _sync_vector_upsert(
        memory_id=new_id,
        session_id=session_id,
        character_id=scope_character_id,
        content=content,
        memory_type=memory_type,
        importance=importance
    )
    invalidate_memory_cache(session_id, character_id)
    return "inserted"


# ── 兼容层 ──────────────────────────────────────────────
# 早期版本 memory_manager 自己维护 SentenceTransformer 实例（_st_model_cache/_embedding_cache），
# 与 memory/embedding.py 各加载一份同样的 ~400MB 模型，且 _relevance 对每条记忆调一次
# 本地编码，是"回复慢"的最大瓶颈。现统一委托给 memory.embedding.encode（单一模型实例），
# 通道 A 不再做记忆侧 embedding（语义打分交给通道 B 的 ChromaDB 向量召回）。
from .memory.embedding import encode as _embed_encode


def _get_embedding(text: str):
    """
    获取文本向量（委托给 memory.embedding.encode）。
    ★ 早期版本曾是 memory_manager 自己的 SentenceTransformer 懒加载入口，
      现与 memory.embedding 共享同一模型实例，避免双模型加载。
    """
    if not text or not text.strip():
        return None
    return _embed_encode(text.strip()[:512])


def _cosine_sim(a, b) -> float:
    """纯Python余弦相似度（不依赖numpy）"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot  = sum(x * y for x, y in zip(a, b))
    na   = sum(x * x for x in a) ** 0.5
    nb   = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _relevance(query: str, memory: str) -> float:
    """
    字符重叠率打分（difflib）。
    语义相似度不再在这里算——交给通道 B（ChromaDB 向量库的 vector_boost）。

    历史：旧版本会对每条 memory 调一次本地 SentenceTransformer.encode，
    N 条记忆 = N 次本地 CPU 推理，是"回复慢"的最大瓶颈（memory_manager.search_memories）。
    现语义加权完全走向量库：search_memories 里已用 vector_boost[content] 给 SQL 命中项加分。
    """
    if not query or not memory:
        return 0.0
    return round(
        difflib.SequenceMatcher(
            None,
            str(query).lower()[:200],
            str(memory).lower()[:200]
        ).ratio(),
        4
    )


def _time_score(mem: dict) -> float:
    """记忆"新鲜度"得分（0~1），检索排序里占 10% 权重。

    ★ 2026-09-13 修复取值顺序：`last_used` 提到最前。
      原顺序是 `update_time or last_used or create_time`，实测后果：
        · 线上 `update_time` 填充率仅 3%、`last_used` 19%、`create_time` 100%；
        · 于是**约 81% 的记忆，新鲜度只由"创建时间"决定** ——
          "这条记忆最近被想起过"这个信号**根本没参与打分**。
      更糟的是历史遗留：`db.touch_recall()` 曾把 `update_time` 在召回时刷成当前时间
      （已在 db.py 修掉），使 `update_time` 变成"最后被想起"的伪信号，
      叠加下面的 days 阈值 → 旧记忆越被召回越"新鲜" → 越容易被再次召回。
      现在改为：先看"被用过/被想起过"，再看"内容改动时间"，最后才是创建时间。
    """
    ts = (
        mem.get("last_used")
        or mem.get("last_recalled")
        or mem.get("update_time")
        or mem.get("create_time")
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

    days = max(
        0,
        (datetime.now() - dt).days
    )

    if days <= 1:
        return 1.0

    if days <= 7:
        return 0.9

    if days <= 30:
        return 0.75

    if days <= 90:
        return 0.55

    return 0.35


MEMORY_TYPE_WEIGHT = {
    "relationship": 1.00,
    "rule": 1.00,        # 用户立下的行为规则：最高权重（由 rule_block 常驻注入）
    "emotion": 0.95,
    "event": 0.90,
    "episode": 0.90,   # 经历/约定：与 event 同权重（提炼输出用 episode，之前漏配导致只拿默认 0.75）
    "preference": 0.85,
    "fact": 0.80,
    "general": 0.70,
}


# ★ Q_recall 闭环·读取侧（2026-09-13）：置信度 → 检索权重因子
#   conf=1.0（旧库存量行/无标注）→ 1.0，行为不变；
#   conf=0.75 的新 fact → ×0.89，conf=0.60 的 general → ×0.82 —— 温和降权，
#   让"转述失真风险高"的记忆排到同类高置信记忆后面，而不是被丢弃。
def _confidence_factor(mem: dict) -> float:
    try:
        conf = float(mem.get("confidence", 1.0) or 1.0)
    except Exception:
        conf = 1.0
    conf = max(0.0, min(1.0, conf))
    return 0.55 + 0.45 * conf


def _importance_score(mem: dict) -> float:
    try:
        value = int(
            mem.get("importance", 5)
        )
    except Exception:
        value = 5

    value = max(
        1,
        min(10, value)
    )

    return value / 10.0

def search_memories(
    query: str,
    top_k: int = 8,
    min_score: float = 0.18,
    session_id: str = "default",
    character_id: str = "default"
) -> list:
    """
    双通道融合检索：
      通道A：SQLite 4因子加权（relevance/importance/freshness/type_weight）
      通道B：ChromaDB 向量召回，对通道A的结果做 score boost
             通道B独占命中（SQL未命中）且向量相似度 >= 0.6 时补救入结果
    返回：[(mem_dict, score), ...]，按 score 降序，最多 top_k 条
    """
    query = str(query or "").strip()
    # ★ 2026-09-17：候选池改分层召回（重要度/最近/高频/向量近邻四路合并）。
    #   旧写法 db.valid_memories() 只有 importance 前 200 条 → 活会话 92.8% 的记忆
    #   永远进不了下面的打分循环，"她记不住昨天说的话"由此而来。
    mems  = db.recall_candidates(session_id=session_id, character_id=character_id,
                                 query=query or None)

    if not mems:
        return []

    # ── 通道A：SQLite 4因子加权（原有逻辑，完整保留）
    scored_sql: list = []
    for mem in mems:
        content     = str(mem.get("memory_content", ""))
        relevance   = _relevance(query, content) if query else 0.25

        # ★ 访问频率影响重要性（高频访问自动升权）
        try:
            from .memory.importance import calculate_access_importance
            _dyn_imp = calculate_access_importance(mem)
        except Exception:
            _dyn_imp = int(mem.get("importance", 5) or 5)

        importance  = _dyn_imp / 10.0

        # ★ decay_score 影响总评分（衰减严重的记忆降权）
        decay_score = float(mem.get("decay_score", 1.0) or 1.0)
        freshness   = _time_score(mem)
        type_weight = MEMORY_TYPE_WEIGHT.get(str(mem.get("memory_type", "fact")), 0.75)

        # ★ 2026-09-17 改：**相关性主导**。
        #   旧公式 relevance*0.40 + importance*0.25 + freshness*0.10 + type*0.15 + decay*0.10，
        #   即 60% 的分数与"用户这句话"无关 → 打分退化成"重要度榜单"，
        #   实测 6 条 importance=10 的老记忆各被注入 279~518 次，其余 96.4% 从未被想起。
        #   现在 relevance 占 0.65；静态属性只在相关度接近时做决胜（freshness/importance 保留小权重）。
        #   仍乘置信度因子（Q_recall 读取侧）。
        score = (
            relevance    * 0.65
            + importance   * 0.15
            + freshness    * 0.10
            + type_weight  * 0.05
            + decay_score  * 0.05
        ) * _confidence_factor(mem)
        if score >= min_score:
            scored_sql.append((mem, score))

    # ── 通道B：ChromaDB 向量召回
    # vector_boost: content_str → cosine_similarity（0~1）
    vector_boost: dict = {}
    if query:
        try:
            from .memory.vector_store import search_vector
            from .memory.embedding import encode as vec_encode
            q_vec = vec_encode(query)
            if q_vec:
                vec_results = search_vector(
                    user_id=session_id,
                    embedding=q_vec,
                    limit=top_k * 2,
                    character_id=character_id
                )
                for vr in vec_results:
                    c   = str(vr.get("content", ""))
                    sim = float(vr.get("similarity", 0.0))
                    if c:
                        vector_boost[c] = sim
        except Exception as e:
            print(f"[MemoryManager] 向量通道异常(静默): {e}", flush=True)

    # ── 融合步骤1：对 SQL 命中的记忆按向量相似度加分
    boosted: list = []
    for mem, score in scored_sql:
        content = str(mem.get("memory_content", ""))
        vsim    = vector_boost.get(content, 0.0)
        if vsim >= 0.75:
            score = min(1.0, score + 0.15)
        elif vsim >= 0.5:
            score = min(1.0, score + 0.08)
        boosted.append((mem, score))

    # ── 融合步骤2：向量独占命中（SQL 未命中）的记忆补救
    sql_contents = {str(m.get("memory_content", "")) for m, _ in scored_sql}
    for content, vsim in vector_boost.items():
        if content in sql_contents or vsim < 0.6:
            continue
        for mem in mems:
            if str(mem.get("memory_content", "")) == content:
                importance = _importance_score(mem)
                freshness  = _time_score(mem)
                score = (vsim * 0.5 + importance * 0.3 + freshness * 0.2) * _confidence_factor(mem)
                if score >= min_score:
                    boosted.append((mem, score))
                break

    # ── 排序 + 截取
    boosted.sort(key=lambda x: x[1], reverse=True)
    top = boosted[:top_k]

    # ── 访问记录（touch + importance growth，原有逻辑）
    for mem, _ in top:
        try:
            db.touch_memory(mem["id"])
            from .memory.importance import calculate_importance_growth, update_importance
            growth = calculate_importance_growth(mem, used=True)
            update_importance(mem["id"], growth)
        except Exception:
            pass

    return top



def build_memory_query(
    current_message: str,
    recent_messages: list = None
) -> str:
    """
    升级版记忆查询构建
    原版：只取用户消息
    升级：用户消息（权重高）+ AI回复关键句（权重低，补充上下文）
    """
    user_parts = []
    ai_parts   = []

    if recent_messages:
        for m in recent_messages[-6:]:
            role    = m.get("role", "")
            text    = str(m.get("content", "")).strip()
            if not text:
                continue
            if role == "user":
                user_parts.append(text)
            elif role == "assistant":
                # AI回复只取前60字（捞关键承诺/提及的事，不要废话）
                ai_parts.append(text[:60])

    current_message = str(current_message or "").strip()
    if current_message:
        user_parts.append(current_message)

    # 用户消息优先，AI回复补充在后
    # 总长度限制2000（原来1500，适当放宽）
    query = "\n".join(user_parts)
    if ai_parts:
        query += "\n" + "\n".join(ai_parts)

    return query[-2000:]


def _build_cold_start_hint(session_id: str, character_id: str) -> str:
    """
    向量冷启动降级：新用户没有任何记忆时的引导性提示。
    让AI知道这是全新用户，主动去了解对方，而不是装作已经认识。

    策略：
    - 有对话历史但无记忆 → 说明记忆提炼还没跑，给AI简单的上下文提示
    - 完全没有历史 → 真正的第一次见面
    """
    try:
        # 检查是否有对话历史（有历史但无记忆 vs 真新用户）
        recent = db.recent_messages(session_id, 4, character_id)
        has_history = bool(recent and len(recent) > 0)
    except Exception:
        has_history = False

    if has_history:
        return (
            "【记忆提示】\n"
            "你和这个人已经聊过几句了，但还没有积累足够的长期记忆。\n"
            "自然地继续对话，通过聊天慢慢了解对方，不要假装已经很熟悉。"
        )
    else:
        return (
            "【初次见面】\n"
            "这是你和这个人的第一次对话。\n"
            "· 不要假装已经认识对方，也不要一上来就问一长串问题\n"
            "· 自然地打招呼，让对话有机地展开\n"
            "· 如果对方主动分享信息，认真记住并自然回应"
        )


# ══════════════════════════════════════════════════
# ★ 三路召回（记忆i存储优化）：场景类型加权 + recall_count 平滑惩罚
# ══════════════════════════════════════════════════

# 用户当前状态 → 优先召回的 memory_type（适配现有类型：emotion/event/preference/fact/general）
SCENE_TYPE_PRIORITY = {
    "倾诉":     ["emotion", "event"],
    "求助":     ["fact",    "event"],
    "深夜独白": ["emotion", "general"],
    "撒娇":     ["preference", "general"],
    "闲聊":     ["fact",    "preference"],
}


def _infer_scene(user_message: str) -> str:
    """从用户消息简单推断当前状态（关键词，无 IO）。"""
    msg = str(user_message or "").strip()
    if not msg:
        return "闲聊"
    if any(k in msg for k in ["好累", "难过", "伤心", "委屈", "压力", "焦虑",
                              "崩溃", "烦", "哭", "撑不住", "不开心", "低落"]):
        return "倾诉"
    if any(k in msg for k in ["怎么办", "帮帮我", "求助", "救命", "怎么解决", "教我", "支招"]):
        return "求助"
    if any(k in msg for k in ["睡不着", "凌晨", "熬夜", "失眠", "半夜"]):
        return "深夜独白"
    if any(k in msg for k in ["抱抱", "亲亲", "想你", "么么", "人家", "哼"]):
        return "撒娇"
    return "闲聊"


def _apply_three_way(scored: list, user_message: str = "",
                     session_id: str = "", character_id: str = "") -> list:
    """在基础召回分数上做修正：

    1. 场景类型命中 → 加权（第一优先 +0.15，第二 +0.08）
    2. 情绪共振（2026-09-13 新增）→ 记忆的情绪标签与"她此刻的心情"吻合时加分
    3. recall_count → 倒U曲线（2026-09-13 二次修正，见 _recall_count_factor）

    ★ 2 的由来：`emotion_tag` 字段早已在库里（实测 2471/3486 条有值，取值如
      温暖1119 / 期待242 / 甜蜜155 / 担心80…），但**从未参与任何打分或检索** ——
      只作为文本渲染进 prompt。这里把它接进排序，实现"情绪共振"：
      她心里难过时，更该想起那些被标记为"担心/心疼"的往事；
      心情好时，更容易想起"温暖/甜蜜"的片段。
    ★ 3 的由来：原先惩罚是 `0.9^min(rc,10)`，而线上实测 recall_count 被
      重复累加到了 5628（见 memory/retriever.search_memory 的修复说明），
      导致 70 条记忆（含 9 条 importance=10）被永久压到 0.35 分。
      现在：① 计数本身改成一轮一次；② 改为倒U曲线——
      被想起过 1~3 次的记忆**轻微强化**（复述巩固：经常回忆的记忆更容易
      再次触发，这才是人类记忆的方向），4~8 次起平滑回落，>8 次封顶 0.75
      （防"总提同一件事"，但不永久封杀核心记忆）。
    """
    scene = _infer_scene(user_message)
    prio  = SCENE_TYPE_PRIORITY.get(scene, [])
    emo_key = _current_emotion_key(session_id, character_id)
    out = []
    for mem, base in scored:
        score = float(base)
        mtype = str(mem.get("memory_type", "fact"))
        if mtype in prio:
            idx = prio.index(mtype)
            score += 0.15 - idx * 0.07
        # 情绪共振
        try:
            score += _emotion_resonance_bonus(mem, emo_key)
        except Exception:
            pass
        rc = int(mem.get("recall_count", 0) or 0)
        if rc > 0:
            score *= _recall_count_factor(rc)
        out.append((mem, score))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _recall_count_factor(rc: int) -> float:
    """被召回次数 → 倒U权重因子。

    倒U的依据：人类记忆里"复述"是双刃剑 —— 偶尔回忆会巩固记忆（越想越清晰），
    但被反复炒冷饭的记忆说明它正在被过度消费，该压一压让别的记忆有出场机会。
    1~3 次：×1.03/1.06/1.09（复述巩固区）
    4~8 次：×0.95 递减到 0.75（消费过度，逐渐回落）
    >8 次 ：0.75 封顶（防复读下限，不再加深；历史脏数据即使堆到几千也只到这）
    """
    try:
        rc = int(rc or 0)
    except Exception:
        return 1.0
    if rc <= 0:
        return 1.0
    if rc <= 3:
        return 1.0 + 0.03 * rc
    if rc <= 8:
        return 1.0 - 0.05 * (rc - 3)
    return 0.75


# ── 情绪共振：她此刻的心情 → 与之呼应的记忆情绪标签 ──────────────
#   键**必须**与 emotion_engine/ai_emotion.py:EMOTION_DECAY 的枚举一致，
#   实测线上出现过的取值：calm(14) / playful(2) / reconciling(1)。
#   该枚举共 13 个：angry / excited / playful / happy / reconciling / sad /
#   worried / cold / sulky / upset / tender / loving / calm
#   （第一版我漏了 reconciling/worried/cold/sulky/loving 五个，实测才补上）
#   值 = 该情绪下"更值得想起"的 emotion_tag 关键词（对应 long_term_memory.emotion_tag
#   的实际取值：温暖1119 / 期待242 / 甜蜜155 / 担心80 / 开心77 / 骄傲59 / 心疼52 / 亲密41）
EMOTION_RESONANCE = {
    # 正向
    "happy":       ("开心", "甜蜜", "温暖", "骄傲"),
    "excited":     ("期待", "兴奋", "开心", "甜蜜"),
    "tender":      ("温暖", "亲密", "甜蜜", "心疼"),
    "loving":      ("亲密", "甜蜜", "温暖"),
    "playful":     ("开心", "甜蜜", "调皮"),
    # 负向
    "upset":       ("委屈", "遗憾", "担心", "心疼"),
    "sad":         ("遗憾", "难过", "心疼"),
    "worried":     ("担心", "心疼", "体贴"),
    "angry":       ("委屈", "遗憾"),
    "cold":        ("遗憾", "亲密"),
    "sulky":       ("委屈", "亲密", "遗憾"),
    "reconciling": ("温暖", "亲密", "心疼"),   # 和好时想起温暖的事
    # 中性：不加权（保持行为与改前一致）
    "calm":        (),
}
RESONANCE_BONUS = 0.06


def _current_emotion_key(session_id=None, character_id=None) -> str:
    """读她此刻的情绪枚举（失败返回空串 → 情绪共振不生效，行为退化为原样）。"""
    try:
        from .emotion_engine.ai_emotion import AIEmotionEngine
        st = AIEmotionEngine().get_state(session_id or "default",
                                         character_id or "default")
        return str(st.get("emotion") or "").strip().lower()
    except Exception:
        return ""


def _emotion_resonance_bonus(mem: dict, emo_key: str) -> float:
    """记忆的情绪标签是否与当前心情呼应。命中给 RESONANCE_BONUS，否则 0。"""
    if not emo_key:
        return 0.0
    words = EMOTION_RESONANCE.get(emo_key) or ()
    if not words:
        return 0.0
    tag = str(mem.get("emotion_tag") or "")
    if not tag:
        return 0.0
    for w in words:
        if w in tag:
            return RESONANCE_BONUS
    return 0.0


def _count_mem_lines(text: str) -> int:
    """记忆块里以「- 」开头的行数 = 真实注入的记忆条数（冷启动提示为 0）。"""
    try:
        return sum(1 for _ln in str(text or "").splitlines() if _ln.startswith("- "))
    except Exception:
        return 0


def _log_recall(mode: str, n: int, session_id: str, character_id: str,
                boost: bool, query: str = "") -> None:
    """★ 2026-09-15 补（体检「记忆召回」维度可观测）。

    体检脚本此前把「记忆召回」判成【未触发】，根因不是没召回，而是**日志里没有任何
    召回痕迹** —— 主注入路径 memory_block() 全程静默。这里补一条稳定格式的日志行，
    体检脚本按 `[memory_manager] 记忆召回` 计数即可（n=0 也算一次尝试）。

    只在真正注入的那一次打印：mode ∈ {query, fallback, hit, cold-start}。
    """
    try:
        _q = str(query or "").replace("\n", " ").strip()
        if len(_q) > 40:
            _q = _q[:40] + "…"
        print("[memory_manager] 记忆召回 n=%d mode=%s boost=%s char=%s sess=%s query=%s"
              % (int(n), mode, bool(boost), character_id, session_id, _q or "-"), flush=True)
    except Exception:
        pass


def memory_block(
    limit: int = 5000,
    query: str = None,
    session_id: str = "default",
    character_id: str = "default",
    user_message: str = "",    # ★ 三路召回：场景加权
    boost: bool = False        # ★ 理解层 routing.need_memory：需要回忆过往时加强检索
) -> str:
    """
    构建注入 system prompt 的记忆文本块。
    - 有 query → 走 search_memories()（双通道融合检索）
    - 无 query → 按 importance + freshness 加权排序取 top8
    - ★ 三路加权：场景类型命中 + recall_count 平滑惩罚
    - 注入时带情境（context），记忆更有上下文
    - 按 scope 分组输出，LLM 友好格式
    - per-session TTL 8秒缓存

    boost=True（理解层判定"用户提到了以前的事/共同经历"）时扩大召回上限，
    让更多相关记忆有机会进入候选。
    ★ 注意：boost 只做**加强**；boost=False 时行为与改动前完全一致——
      绝不能因为理解层判 False 就少给或不给记忆，那会让 AI 直接失忆。
    """
    # ── TTL 缓存（★ 2026-09-17：键里加"这一轮的问题指纹"）
    #   旧设计键只含 session/character/boost，30 秒内换问题会命中上一条问题的缓存，
    #   把上一轮的记忆原样塞给新问题（实测活会话有 109 次相邻召回落在 30 秒内）。
    #   现在的键 = (session, character, boost, query指纹)：同轮重复调用能命中，
    #   换了问题必然重新检索；TTL 收到 8 秒，不再承担跨问题复用。
    _qfp   = _query_fingerprint(query, user_message)
    _ckey  = (session_id, character_id, bool(boost), _qfp)
    _cached = _memory_block_cache.get(_ckey)
    if _cached:
        _text, _exp = _cached
        if _time.time() < _exp:
            # ★ 2026-09-15 补（体检可观测）：缓存命中同样是"这一轮注入了记忆"，
            #   必须留痕，否则体检脚本按日志计数会漏掉 80%+ 的轮次。
            _log_recall("hit", _count_mem_lines(_text), session_id, character_id,
                        boost, query or user_message)
            return _text

    # ── 检索
    # ★ 长期陪伴：放宽检索条数，让更多相关记忆进入候选（boost 时再加强）
    _top_k = 20 if boost else 12
    _cap   = 16 if boost else 12
    if query:
        scored = search_memories(
            query,
            top_k=_top_k,
            session_id=session_id,
            character_id=character_id
        )
        mems = [m for m, _score in _apply_three_way(
            scored, user_message, session_id=session_id, character_id=character_id)][:_cap]
    else:
        # ★ 2026-09-17：无 query 的兜底同样走分层召回（原本是 importance 前 200 条，
        #   导致"昨天刚说的低重要度记忆"连兜底都进不来）。
        all_mems = db.recall_candidates(
            session_id=session_id,
            character_id=character_id
        )

        def _fallback_score(m):
            # ★ Q_recall 读取侧：无 query 的兜底排序同样让低置信记忆温和靠后
            return (_importance_score(m) * 0.5 + _time_score(m) * 0.5) * _confidence_factor(m)

        scored_fb = [(m, _fallback_score(m)) for m in all_mems]
        mems = [m for m, _s in _apply_three_way(
            scored_fb, user_message, session_id=session_id, character_id=character_id)][:_cap]

    if not mems:
        # ★ 冷启动降级：新用户没有记忆时，给AI一个引导性提示
        cold_start = _build_cold_start_hint(session_id, character_id)
        _memory_block_cache[_ckey] = (
            cold_start, _time.time() + _MEMORY_CACHE_TTL
        )
        _log_recall("cold-start", 0, session_id, character_id, boost, query or user_message)
        return cold_start

    # ★ 2026-09-15 补（体检可观测）：主注入路径留痕 —— 这一轮注入了 n 条记忆。
    #   放在召回统计之前：统计写失败也要留下"确实召回了"的证据。
    _log_recall("query" if query else "fallback", len(mems), session_id, character_id,
                boost, query or user_message)

    # ★ 召回统计（2026-09-13 补）：把这一轮**真正注入**的记忆登记为"被想起过"。
    #   为什么必须补在这里：全项目只有 `hybrid_search` 那条路会调
    #   `_update_last_used`，而它的调用方只有 prompt_builder 和
    #   multi_turn/generator —— **本函数（主注入路径）完全不经过它**。
    #   实测后果：聊了很多轮，线上 3491 条记忆的 `access_count` 仍全为 0、
    #   `last_recalled` 全空，连带 `calculate_decay` 的 access 项、
    #   `importance` 的"access_count>=10 永久保留"、`maintenance` 的访问加权
    #   —— 三处逻辑恒为 0，等于从未生效。
    #
    #   位置说明：放在 TTL 缓存之后（缓存命中会提前 return，不会重复计数），
    #   且只在真正选出 mems 时记 —— "被注入"即"被想起"。
    try:
        from . import db as _db
        _ids = [int(m["id"]) for m in mems if str(m.get("id") or "").isdigit()]
        if _ids:
            _db.touch_recall(_ids)
    except Exception as _te:
        print(f"[memory_manager] 召回统计写入失败(静默): {_te}", flush=True)

    # ── 按 scope 分组
    try:
        from .memory.scope import GLOBAL, CHARACTER, RELATIONSHIP, SCOPE_TITLES
    except Exception:
        GLOBAL       = "global"
        CHARACTER    = "character"
        RELATIONSHIP = "relationship"
        SCOPE_TITLES = {
            "global":       "关于这个人的记忆",
            "character":    "与当前角色相关的记忆",
            "relationship": "你们之间的记忆",
        }

    scope_groups = {GLOBAL: [], CHARACTER: [], RELATIONSHIP: []}
    total = 0
    _hedged = False   # 是否存在低置信记忆（决定要不要加"模糊印象"表达指令）

    for m in mems:
        content = str(m.get("memory_content", "")).strip()
        if not content:
            continue
        if total + len(content) > limit:
            break
        scope = str(m.get("memory_scope", GLOBAL))
        if scope not in scope_groups:
            scope = GLOBAL
        # ★ 情境增强（记忆i存储优化）：有 context 时带上，记忆更有上下文
        _ctx = str(m.get("context", "") or "").strip()
        line = content + (f"（情境：{_ctx}）" if _ctx else "")
        # ★ Q_recall 闭环·注入侧：低置信记忆标注"模糊印象"，
        #   让模型表达时自带不确定性，而不是把 LLM 转述当铁律说得笃定。
        try:
            _conf = float(m.get("confidence", 1.0) or 1.0)
        except Exception:
            _conf = 1.0
        if _conf < memory_brain.LOW_CONFIDENCE_HEDGE:
            line += "（这条你只有模糊印象，细节记不清）"
            _hedged = True
        scope_groups[scope].append(line)
        total += len(content)

    parts = []
    for scope in [GLOBAL, CHARACTER, RELATIONSHIP]:
        items = scope_groups[scope]
        if not items:
            continue
        title = SCOPE_TITLES.get(scope, scope)
        lines = "\n".join("- " + x for x in items)
        parts.append("【" + title + "】\n" + lines)

    if not parts:
        _memory_block_cache[_ckey] = ("", _time.time() + _MEMORY_CACHE_TTL)
        return ""

    result = (
        "以下内容是与你当前对话最相关的记忆。"
        "自然理解和运用，不要逐条复述，"
        "不要向用户提到记忆检索机制。\n\n"
        + "\n\n".join(parts)
    )
    if _hedged:
        result += (
            "\n\n【表达分寸】带「模糊印象」标注的记忆，"
            "提起时用「我印象里好像是…」「如果我没记错」这类不确定的口吻，"
            "记不清的细节就大方说不确定，别说得笃定。"
        )

    # ── 硬预算（★ 2026-09-17 补）
    #   上面的逐条累加判断只看单条 content 长度，标题/「（情境：…）」/低置信注解
    #   都不计入 total，所以调用方传 limit=1800 时实际返回可能冲到 3000+。
    #   调用方（外置记忆块、调度器 memory_hint 等）是按预算传参的，
    #   超预算会挤掉别的注入块。这里按调用方给的上限做最后一道裁剪，
    #   优先在换行处截断，尽量不切碎一条记忆。
    if limit and len(result) > limit:
        _cut = result.rfind("\n", 0, limit)
        result = result[:_cut if _cut > limit * 0.6 else limit]

    # ── 写缓存 + 容量控制
    _memory_block_cache[_ckey] = (result, _time.time() + _MEMORY_CACHE_TTL)
    if len(_memory_block_cache) > 200:
        _oldest = min(_memory_block_cache, key=lambda k: _memory_block_cache[k][1])
        del _memory_block_cache[_oldest]

    return result



def invalidate_memory_cache(session_id: str = None, character_id: str = None):
    """
    写入记忆后主动清缓存（避免刚存的记忆8秒内读不到）
    session_id=None时清全部缓存
    """
    global _memory_block_cache
    if session_id is None:
        _memory_block_cache = {}
        return
    keys_to_del = [
        k for k in _memory_block_cache
        if k[0] == session_id
        and (character_id is None or k[1] == character_id)
    ]
    for k in keys_to_del:
        del _memory_block_cache[k]


# 使用 memory_filter 的结构化 System Prompt（含严格过滤规则 + 字段约束 + 客观改写要求）
EXTRACT_SYSTEM = memory_filter.STRUCTURED_EXTRACT_SYSTEM


async def extract_from_dialog(messages: list, character_id: str = "default") -> list:
    """
    调用 MEMORY_EXTRACT_MODEL（固定 flash）提炼记忆，返回字符串列表。
    
    流水线：
    1. memory_filter.filter_messages_for_extraction() — 剔除问句和指令
    2. 将过滤后的对话送入模型
    3. parse_memory_json() — 解析模型输出 JSON
    4. memory_filter.post_process_memories() — 校验+改写+去重
    """
    key = config.memory_key()
    if not key:
        return []
    
    # 步骤1: 过滤问句和指令（核心修复：不再把问句/命令送入提炼模型）
    filtered_msgs, skipped_types = memory_filter.filter_messages_for_extraction(messages)
    
    # 如果过滤后只剩 system 消息或为空 → 无需调用模型
    user_msgs = [m for m in filtered_msgs if m.get("role") == "user"]
    if len(user_msgs) == 0:
        return []  # 全是问句/指令，没有可提取的事实
    
    # 构建过滤后的对话文本
    transcript = "\n".join(
        ("%s：%s" % ("TA" if m.get("role") == "user" else "AI", m.get("content", "")))
        for m in filtered_msgs if m.get("content")
    )
    if not transcript.strip():
        return []
    
    # ★ 修复：留空时不再把 None/空串当 model，而是回退到 background_model()（跟随主脑降级）。
    #   这样记忆提炼用便宜 flash，且 model 与 key（memory_key 已按 background_model 取）一致。
    _extract_model = config.memory_extract_model(character_id)
    out = await chat_once(
        _extract_model,
        [{"role": "system", "content": EXTRACT_SYSTEM},
         {"role": "user", "content": transcript[-12000:]}],
        key, temperature=0.2, max_tokens=2048,
        # ★ GLM 5.3 系列思考强度：记忆提炼是机械抽取，用 low 最省；DeepSeek 不支持则不传。
        reasoning_effort=(_reasoning_effort_for(_extract_model, "low")),
    )
    
    raw_memories = parse_memory_json(out)
    
    # 步骤4: 后处理校验 + 客观改写 + 去重
    final_memories = memory_filter.post_process_memories(raw_memories)
    
    return final_memories


def parse_memory_json(out: str) -> list:
    """容错解析模型输出里的 JSON（去代码块包裹、抓最大 {...}）。
    支持两种格式：
    1. 旧格式：["用户喜欢猫", "用户最近压力大"]
    2. 新格式：[{"type":"preference","content":"用户喜欢猫","importance":6}]
    返回统一格式：[{"type":"fact","content":"...","importance":5}, ...]
    """
    out = str(out or "").strip()
    if not out:
        return []

    cleaned = re.sub(
        r"```[\w]*\s*\n?",
        "",
        out
    ).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    candidate = (
        cleaned[start:end+1]
        if start >= 0 and end > start
        else cleaned
    )

    try:
        data = json.loads(candidate)
    except Exception:
        return []

    arr = data.get("memories")
    if not isinstance(arr, list):
        # ★ 兼容 glm-5.3-flash 偶尔输出的单数 key {"memory": [...]}
        arr = data.get("memory")

    if not isinstance(arr, list):
        return []

    result = []

    for item in arr:

        if isinstance(item, str):
            result.append({
                "type": "fact",
                "content": item[:200],
                "importance": 5
            })

        elif isinstance(item, dict):

            content = str(
                item.get("content", "")
            ).strip()

            if content:

                result.append({

                    "type":
                    item.get(
                        "type",
                        "fact"
                    ),

                    "content":
                    content[:200],

                    "importance":
                    int(
                        item.get(
                            "importance",
                            5
                        )
                    ),

                    # ★ 信息单元：情境/情绪/溯源/时间
                    "context":
                    str(
                        item.get("context", "") or ""
                    )[:150],

                    "emotion_tag":
                    str(
                        item.get("emotion_tag", "") or ""
                    )[:30],

                    "source_text":
                    str(
                        item.get("source_text", "") or ""
                    )[:200],

                    # ★ 事件发生时间（今天/昨天/前几天/8月30号 等），供 AI 组织带时间的表达
                    "time_hint":
                    str(
                        item.get("time_hint", "") or ""
                    )[:40],

                })

    return result


async def apply_extracted_memories(items: list, session_id: str, character_id: str = "default") -> int:
    """把**已解析好的**记忆条目写库（★ 2026-09-15 从 run_auto_extract 里拆出来）。

    拆出来的原因（省 token ②）：记忆/画像/未完成事项读同一批 messages，合并成一次模型
    调用后，这一段"逐条 smart_insert → 统计新增/重复 → trace 埋点"必须原样复用，
    保证合并版的入库行为与旧口径**完全一致**（含去重判定与向量同步）。
    返回真正入库条数。
    """
    _ins = _dup = 0
    _kinds = {}
    for it in (items or []):
        try:
            _ret = await memory_brain.smart_insert(
                it["content"],
                memory_type=it.get("type", "fact"),
                importance=it.get("importance", 5),
                session_id=session_id,
                character_id=character_id,
                # ★ 信息单元：情境/情绪/溯源
                context=it.get("context", ""),
                emotion_tag=it.get("emotion_tag", ""),
                source_text=it.get("source_text", ""),
            )
            if _ret:
                _ins += 1
                _k = str(it.get("type") or "fact")
                _kinds[_k] = _kinds.get(_k, 0) + 1
            else:
                _dup += 1
        except Exception as _sie:
            print(f"[MemExtract] smart_insert 失败: {_sie}", flush=True)
    try:
        from . import trace as _trace
        _trace.memory(session_id, character_id, extracted=len(items or []),
                      inserted=_ins, duplicated=_dup,
                      kinds=sorted(_kinds.items(), key=lambda x: -x[1]) or None)
    except Exception:
        pass
    return _ins


async def run_auto_extract(session_id: str, character_id: str = "default"):
    """
    自动提炼（后台静默执行）：
    取最近 AUTO_MEMORY_INTERVAL*2 条对话 → flash 提炼 → 去重/覆盖/新增入库。
    ★ 按 (session_id, character_id) 隔离：读该角色的对话、写该角色的记忆桶，
      保证各人格记忆不串（与 enrich_messages 读取用同一个 character_id）。
    """
    try:
        interval = int(config.get("AUTO_MEMORY_INTERVAL") or 4)
        msgs = db.recent_messages(session_id, interval * 4 + 8, character_id)
        items = await extract_from_dialog(msgs, character_id=character_id)
        # ★ 2026-09-15：入库逻辑抽到 apply_extracted_memories（合并抽取器共用同一段）
        await apply_extracted_memories(items, session_id, character_id)

        # ★ 2026-09-14 移除：这里原有一次 profile_manager.extract_profile(msgs, ...)，
        #   与调用方 chat_logic.maybe_auto_extract（chat_logic.py:1691）对**同一批 messages**
        #   的画像抽取完全重复 —— 每次自动提炼都白跑一次 LLM（实测 10 天 376 次里约占一半）。
        #   画像抽取统一由 maybe_auto_extract 负责，本函数只做记忆提炼 + 入库。
        #   如需单独调用本函数（不经 maybe_auto_extract），请自行在其后补一次画像抽取。

        return len(items)
    except (ModelApiError, Exception):
        return 0
