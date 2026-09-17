# -*- coding:utf-8 -*-
"""
向量存储模块：
  使用 ChromaDB 持久化向量数据库。
  如果 chromadb 未安装，降级为内存中的简单向量存储，
  确保系统正常运行（重启后数据会丢失，但不影响功能）。

★ 向量库只是「可重建的索引」，记忆权威在主库 long_term_memory（SQLite）。
  实测损坏形态：chroma.sqlite3 元数据完好，但 segment 目录被清空（杀毒/清理/
  中断写入），此后 add/query/delete 全部报 "Error loading hnsw index" 且永不
  自愈 —— 本模块内置自愈：首次访问用只读探针把损坏暴露出来 → 自动删集合
  （元数据层，不触碰损坏的 hnsw 段）→ 从主库全量重嵌入。重建不丢任何记忆。
"""
import gc
import math
import shutil
import threading
import time as _time
from pathlib import Path
from .. import config

# ★ 向量库路径跟随数据目录：与主库 local_db.db 同在 DATA_DIR，
#   打包版 AI_COMPANION_DATA_DIR 重定向到 userData 时一起搬过去。
#   旧实现写死 ROOT_DIR/backend/data，打包版会把可写数据落进安装目录。
VECTOR_DIR = config.DATA_DIR / "memory_vectors"
_active_dir = VECTOR_DIR          # 兜底重建时若目录删不掉，切换到兄弟目录绕开

COLLECTION_NAME = "yunlink_memory"

_client = None
_collection = None
_available = None

# 内存降级存储
_memory_store = []

# ── 自愈状态 ──
_probe_done = False            # 本进程是否已做过只读探针（只探一次，避免每条消息都探）
_rebuild_lock = threading.RLock()
_rebuilding = False
_last_rebuild_ts = 0.0
_rebuild_attempts = 0
_last_rebuild_total = 0         # 最近一次重建的主库有效记忆总数（打日志用）
_MIN_REBUILD_INTERVAL = 300    # 两次自动重建最小间隔（秒），防止反复重建打转
_MAX_REBUILD_ATTEMPTS = 3      # 每进程自动重建上限，超过就永久降级（内存/关键词检索）

# 命中即视为存储损坏（小写匹配）。含维度不匹配：换了 embedding 模型
# （384→768）后旧向量全部作废，同样只有重建一条路。
_CORRUPTION_MARKS = (
    "hnsw",                      # Error loading hnsw index / constructing hnsw segment reader
    "segment reader",
    "loading hnsw index",
    "error executing plan",
    "compaction",
    "corrupt",
    "dimensionality",
    "does not match collection",
    "invaliddimension",
)


def is_available():
    """检查 chromadb 是否可用"""
    global _available
    if _available is not None:
        return _available
    try:
        import chromadb  # noqa: F401
        _available = True
    except ImportError:
        _available = False
    return _available


def _corruption_error(e) -> bool:
    """判断异常是否属于向量库存储损坏（需要重建才能恢复的那种）"""
    msg = str(e).lower()
    return any(m in msg for m in _CORRUPTION_MARKS)


def _new_client(path: Path):
    import chromadb
    from chromadb.config import Settings
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(path),
        # allow_reset：兜底重建需要 reset() 清库；顺带关掉遥测上报
        settings=Settings(allow_reset=True, anonymized_telemetry=False),
    )


def _get_client():
    """获取 ChromaDB 客户端（延迟初始化）"""
    global _client
    if _client is not None:
        return _client
    if not is_available():
        return None
    try:
        _client = _new_client(_active_dir)
        return _client
    except Exception as e:
        print(f"[vector_store] ChromaDB 初始化失败: {e}", flush=True)
        return None


def _probe_collection(collection) -> None:
    """只读探针：发一次最小查询，强制加载 hnsw 段。

    get_or_create_collection 只碰元数据，段损坏时它不报错，
    直到 query/add 才炸 —— 主动探一次，把修复提前到首次使用，
    而不是让每条消息都报一遍错。空集合也能查，无额外开销。
    """
    from .embedding import EMBEDDING_DIM
    collection.query(query_embeddings=[[0.0] * int(EMBEDDING_DIM)], n_results=1)


def _get_collection():
    """获取或创建记忆集合（含首次访问损坏探针）"""
    global _collection, _probe_done
    if _collection is not None:
        return _collection
    client = _get_client()
    if client is None:
        return None
    try:
        _collection = client.get_or_create_collection(
            name=COLLECTION_NAME
        )
    except Exception as e:
        print(f"[vector_store] 获取集合失败: {e}", flush=True)
        if _corruption_error(e) and _recover_if_corrupt(e):
            return _collection
        return None
    if not _probe_done:
        _probe_done = True
        try:
            _probe_collection(_collection)
            # ★ 数量对账：向量数落后于主库有效记忆数 = 索引不完整。
            #   覆盖三种情形：空库（新装/路径迁移）、chroma 自愈后部分丢失
            #   （hnsw 段损坏时它只能从 sqlite 队列救回未压实的那部分）、
            #   历史写入失败的存量缺口。后台一次性补齐，不阻塞首次请求。
            if _collection.count() < _db_valid_memory_count():
                threading.Thread(
                    target=_backfill_once, name="vector-backfill", daemon=True
                ).start()
        except Exception as e:
            if _corruption_error(e):
                # ★ 2026-09-17 修（关键）：探针异常也必须走损坏判定。
                #   真机实测：探针抛的是 "Error loading hnsw index" ——
                #   完全命中 _CORRUPTION_MARKS（含 "hnsw"），却因为这里原先把
                #   "非损坏异常不折腾" 写在前面，直接被当成瞬时错误放过去了：
                #   _collection 保持有效 → 每条消息的 search_vector 都失败 →
                #   通道B 永远 0 命中（日志里 929 次 "向量搜索失败: ... Error loading
                #   hnsw index"），语义检索这条腿等于**从上线起就是断的**。
                print(f"[vector_store] 探针发现向量库损坏: {e}", flush=True)
                _collection = None
                if rebuild_collection(f"探针检测到存储损坏: {e}"):
                    return _get_collection()
                return None
            # 非损坏异常（瞬时错误等）不折腾，交给各操作的降级路径
            print(f"[vector_store] 探针查询失败(不当作损坏): {e}", flush=True)
    return _collection


def _db_valid_memory_count() -> int:
    """主库 long_term_memory 有效记忆数（对账基准；异常时返回 0 = 不触发回填）"""
    try:
        from .. import db
        rows = db.q(
            "SELECT COUNT(*) AS c FROM long_term_memory WHERE is_valid=1",
            fetch=True,
        )
        return int(rows[0]["c"]) if rows else 0
    except Exception:
        return 0


_backfill_started = False


def _backfill_once():
    """向量索引缺口回填（每进程至多一次；失败静默，不影响主流程）"""
    global _backfill_started
    with _rebuild_lock:
        if _backfill_started:
            return
        _backfill_started = True
    try:
        print("[vector_store] 向量索引落后于主库，开始后台全量回填（upsert 幂等，只补缺）...", flush=True)
        n = _reembed_all()
        print(f"[vector_store] 回填完成：{_last_rebuild_total} 条中嵌入 {n} 条", flush=True)
    except Exception as e:
        print(f"[vector_store] 回填失败(静默): {e}", flush=True)


def rebuild_collection(reason: str = "unknown") -> bool:
    """重建向量库：清掉损坏集合，从主库 long_term_memory 全量重嵌入。

    权威数据在 SQLite，向量只是索引 —— 重建不丢任何记忆，只重算向量。
    成功返回 True（调用方可安全重试原操作），失败返回 False（继续内存降级）。
    """
    global _collection, _client, _rebuilding, _last_rebuild_ts, _rebuild_attempts, _probe_done, _active_dir

    with _rebuild_lock:
        if _rebuilding:
            return False
        now = _time.time()
        if _rebuild_attempts >= _MAX_REBUILD_ATTEMPTS:
            print("[vector_store] 自动重建次数已达上限，保持降级运行", flush=True)
            return False
        if _last_rebuild_ts and now - _last_rebuild_ts < _MIN_REBUILD_INTERVAL:
            print("[vector_store] 距上次重建过近，跳过本次自动重建", flush=True)
            return False
        _rebuilding = True
        _rebuild_attempts += 1
        _last_rebuild_ts = now
        try:
            print(f"[vector_store] ★ 开始重建向量库（原因: {reason}）", flush=True)

            # ① 优先走 ChromaDB 元数据层清理（不加载损坏的 hnsw 段）
            client = _client
            _collection = None
            if client is not None:
                try:
                    client.delete_collection(COLLECTION_NAME)
                except Exception:
                    try:
                        client.reset()   # 整库清空（本项目只有一个集合，等价）
                    except Exception as e:
                        print(f"[vector_store] 元数据层清理失败: {e}", flush=True)
                        client = None

            # ② 兜底：元数据层进不去 → 释放句柄后整目录删除，
            #    并**换新路径**重建 —— chroma 的 SharedSystemClient 按路径缓存
            #    进程内 system，同路径重开可能拿到已持死句柄的旧实例；
            #    换路径同时也绕开被占用删不掉的旧目录
            if client is None:
                gc.collect()
                _client = None
                shutil.rmtree(_active_dir, ignore_errors=True)
                _active_dir = config.DATA_DIR / f"memory_vectors_rebuilt{int(now)}"
                print(f"[vector_store] 元数据层不可用，切换新目录 {_active_dir}", flush=True)
                try:
                    _client = _new_client(_active_dir)
                except Exception as e:
                    print(f"[vector_store] 重建客户端失败: {e}", flush=True)
                    return False

            # ③ 从主库全量重嵌入
            n = _reembed_all()
            _probe_done = True
            print(f"[vector_store] ★ 向量库重建完成：{n}/{_last_rebuild_total} 条记忆已重新嵌入", flush=True)
            return True
        finally:
            _rebuilding = False


def _reembed_all() -> int:
    """从主库 long_term_memory（唯一权威表）全量重嵌入，返回成功条数。

    ★ 批量编码（2026-09-08）：全部正文一次送 encode_batch（GPU 优先，千条级
      从分钟级降到秒级），编码完成后再逐条写入向量。
    """
    global _last_rebuild_total
    from .. import db
    from .embedding import encode_batch

    rows = db.q(
        "SELECT id, session_id, character_id, memory_type, memory_content, importance "
        "FROM long_term_memory WHERE is_valid=1 ORDER BY id",
        fetch=True,
    ) or []
    _last_rebuild_total = len(rows)

    # 1. 批量编码（一次前向，GPU 批量优势场景）
    items = []
    for r in rows:
        content = str(r["memory_content"] or "").strip()
        if content:
            items.append((r, content))
    vectors = encode_batch([c for _, c in items]) if items else []

    # 2. 逐条写入向量（编码失败的条目跳过，不阻断重建）
    n = 0
    for (r, content), vec in zip(items, vectors):
        if not vec:
            continue
        cid = str(r["character_id"] or "default")
        ok = upsert_vector(
            memory_id=f"sql:{r['id']}",
            user_id=str(r["session_id"] or "default"),
            content=content,
            embedding=vec,
            metadata={
                "type": str(r["memory_type"] or "fact"),
                "importance": int(r["importance"] or 5),
                "character_id": cid,
            },
            character_id=cid,
        )
        if ok:
            n += 1
            if n % 100 == 0:
                print(f"[vector_store] 重建进度: {n}/{len(rows)}", flush=True)
    return n


def _recover_if_corrupt(e) -> bool:
    """操作报错时判断是否存储损坏；是则自动重建。返回是否值得重试原操作。"""
    if not _corruption_error(e):
        return False
    return rebuild_collection(f"操作失败: {e}")


def add_vector(
    memory_id,
    user_id,
    content,
    embedding,
    metadata=None,
    character_id="default"
):
    """
    添加向量到向量库。
    如果 chromadb 可用，使用持久化存储；
    否则使用内存存储。
    """
    collection = _get_collection()

    if collection is not None:
        for _attempt in (1, 2):
            try:
                collection.add(
                    ids=[str(memory_id)],
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[{
                        "user_id": user_id,
                        "character_id": character_id,
                        **(metadata or {})
                    }]
                )
                return True
            except Exception as e:
                if _attempt == 1 and _recover_if_corrupt(e):
                    collection = _get_collection()
                    if collection is not None:
                        continue
                print(f"[vector_store] 添加向量失败: {e}", flush=True)
                break

    # 降级：内存存储
    _memory_store.append({
        "id": str(memory_id),
        "user_id": user_id,
        "character_id": character_id,
        "content": content,
        "embedding": embedding,
        "metadata": metadata or {}
    })
    return True


def upsert_vector(
    memory_id,
    user_id,
    content,
    embedding,
    metadata=None,
    character_id="default"
):
    """写入或更新一条向量（幂等、原子语义）。

    ★ P0-4：更新已有向量必须走**单条 upsert**，不能拆成 delete + add。
      拆开会留下非原子窗口：delete 成功、add 失败时，主库 long_term_memory
      （唯一权威）里记忆还在，向量却没了 —— 语义检索再也搜不到它，且没有任何
      机制能把它补回来，这就是「多套存储静默漂移」的主要来源。

      ChromaDB 原生支持 upsert（同 id 覆盖，不存在则新建），优先用它；
      版本不支持 / 走内存降级时，再退回 delete+add（至少在单进程内一次完成）。
      存储损坏（hnsw 段坏/维度不匹配）时自动重建后重试一次。

    返回：True 成功 / False 失败（调用方只打日志，绝不影响主流程）
    """
    vec_id = str(memory_id)
    meta = {
        "user_id": user_id,
        "character_id": character_id,
        **(metadata or {})
    }

    collection = _get_collection()
    if collection is not None:
        for _attempt in (1, 2):
            try:
                _upsert = getattr(collection, "upsert", None)
                if callable(_upsert):
                    _upsert(
                        ids=[vec_id],
                        embeddings=[embedding],
                        documents=[content],
                        metadatas=[meta],
                    )
                    return True
                # 旧版无 upsert：delete + add（仅当 upsert 不可用）
                delete_vector(vec_id)
                collection.add(
                    ids=[vec_id],
                    embeddings=[embedding],
                    documents=[content],
                    metadatas=[meta],
                )
                return True
            except Exception as e:
                if _corruption_error(e):
                    if _attempt == 1 and _recover_if_corrupt(e):
                        collection = _get_collection()
                        if collection is not None:
                            continue
                    print(f"[vector_store] upsert 失败(存储损坏未恢复): {e}", flush=True)
                    break
                # 非损坏错误：保留旧行为 —— 退回 delete + add 再试一次
                if callable(getattr(collection, "upsert", None)):
                    print(f"[vector_store] upsert 失败，退回 delete+add: {e}", flush=True)
                try:
                    delete_vector(vec_id)
                    collection.add(
                        ids=[vec_id],
                        embeddings=[embedding],
                        documents=[content],
                        metadatas=[meta],
                    )
                    return True
                except Exception as e2:
                    if _attempt == 1 and _recover_if_corrupt(e2):
                        collection = _get_collection()
                        if collection is not None:
                            continue
                    print(f"[vector_store] 兜底 add 失败: {e2}", flush=True)
                    break

    # ③ 降级：内存存储（同样要幂等，不能追加出重复 id）
    global _memory_store
    _memory_store = [item for item in _memory_store if item["id"] != vec_id]
    _memory_store.append({
        "id": vec_id,
        "user_id": user_id,
        "character_id": character_id,
        "content": content,
        "embedding": embedding,
        "metadata": metadata or {}
    })
    return True


def search_vector(
    user_id,
    embedding,
    limit=5,
    character_id="default"
):
    """
    搜索相似向量。
    返回按相似度排序的记忆列表。

    ★ 2026-09-17 修（相似度换算是错的）：
      集合建库时 vector_index 的 space 是 **l2**（见 chroma collection schema），
      而本项目的向量都是 `normalize_embeddings=True` 归一化过的（embedding.py:110）。
      归一化向量的 L2 距离与余弦的准确关系是：cos = 1 - d²/2，**d 的范围是 [0, 2]**。
      旧换算 `similarity = 1.0 - min(distance, 1.0)` 却把 d>1 一律截成 0.0 ——
      而 d>1 恰好对应 cos < 0.5，也就是"大部分语义相关的记忆"这个区间：
      调用方 `memory_manager.search_memories` 的加分门槛是 vsim>=0.5 / >=0.75，
      于是这个加法**几乎永远加不上**（真机 trace 里 vec 值全都在 0.3~0.8 之间徘徊，
      正是被截断后的残值）。现在换成正确换算，排序与阈值才第一次有意义。
    """
    collection = _get_collection()

    if collection is not None:
        for _attempt in (1, 2):
            try:
                result = collection.query(
                    query_embeddings=[embedding],
                    n_results=limit,
                    where={
                        "$and": [
                            {"user_id": user_id},
                            {"character_id": character_id}
                        ]
                    }
                )

                memories = []
                if result and result.get("documents"):
                    for i, text in enumerate(result["documents"][0]):
                        distance = result["distances"][0][i] if result.get("distances") else 0
                        # ChromaDB(l2) → 余弦相似度：cos = 1 - d²/2（归一化向量）
                        similarity = _l2_distance_to_cosine(distance)
                        memories.append({
                            "content": text,
                            "distance": distance,
                            "similarity": similarity,
                            "id": result["ids"][0][i] if result.get("ids") else None
                        })
                return memories
            except Exception as e:
                # ★ 2026-09-17：重试前先判损坏并自愈。原先只在第 1 次尝试时 recover，
                #   而 "Error loading hnsw index" 这种损坏重试多少次都一样 ——
                #   真机实测 929 次失败里没有一次触发重建。
                if _corruption_error(e) and _recover_if_corrupt(e):
                    collection = _get_collection()
                    if collection is not None:
                        continue
                print(f"[vector_store] 向量搜索失败: {e}", flush=True)
                _note_degraded(f"query 失败: {e}")
                break

    # 降级：内存搜索（余弦相似度）。
    # ★ 注意：_memory_store 只在 chromadb 不可用时才会有内容；chroma 可用但
    #   collection 处于降级状态时这里是**空表** → 返回空 = 本轮没有任何语义候选。
    #   这种"静默的 0 命中"以前完全查不出来（没有日志、没有计数），
    #   所以下面会打一条一次性 warning，让"语义检索是不是活着"可观测。
    results = []
    for item in _memory_store:
        if item["user_id"] != user_id or item["character_id"] != character_id:
            continue
        similarity = _cosine_similarity(embedding, item["embedding"])
        results.append({
            "content": item["content"],
            "distance": 1.0 - similarity,
            "similarity": similarity,
            "id": item["id"]
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)
    if not results and collection is None:
        _note_degraded("向量库不可用（内存降级表为空）")
    return results[:limit]


def _l2_distance_to_cosine(distance) -> float:
    """L2 距离 → 余弦相似度（归一化向量：‖a-b‖² = 2 - 2·cos，即 cos = 1 - d²/2）。

    d 的理论范围是 [0, 2]（d=0 同向、d=2 反向），余弦范围 [-1, 1]。
    这里把负余弦裁到 0.0：调用方的门槛是 vsim>=0.5 / >=0.75，
    负值对"该不该加分"没有意义，裁掉可以避免下游拿到负分做乘法。
    """
    try:
        d = float(distance)
    except Exception:
        return 0.0
    if d <= 0:
        return 1.0
    cos = 1.0 - (d * d) / 2.0
    if cos < 0.0:
        return 0.0
    if cos > 1.0:
        return 1.0
    return cos


_degraded_warned = False


def _note_degraded(why: str) -> None:
    """向量通道降级只报一次（每进程），让"语义检索哑了"在日志里看得见。"""
    global _degraded_warned
    if _degraded_warned:
        return
    _degraded_warned = True
    print(f"[vector_store] ⚠ 向量通道降级：本轮起语义候选为空（{why}）"
          f" —— 记忆召回退化为纯词面/重要度，需重建向量库", flush=True)


def delete_vector(memory_id):
    """删除向量"""
    collection = _get_collection()
    if collection is not None:
        for _attempt in (1, 2):
            try:
                collection.delete(ids=[str(memory_id)])
                return True
            except Exception as e:
                if _attempt == 1 and _recover_if_corrupt(e):
                    collection = _get_collection()
                    if collection is not None:
                        continue
                print(f"[vector_store] 删除向量失败: {e}", flush=True)
                break

    # 降级：内存删除
    global _memory_store
    _memory_store = [
        item for item in _memory_store
        if item["id"] != str(memory_id)
    ]
    return True


def _cosine_similarity(vec1, vec2):
    """计算余弦相似度"""
    if not vec1 or not vec2:
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(a * a for a in vec2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)
