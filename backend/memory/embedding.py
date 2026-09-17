# -*- coding:utf-8 -*-
"""
Embedding 模块：
  将文本转换为向量表示，用于语义检索。
  如果依赖未安装，降级为简单的哈希向量，确保系统正常运行。

★ 模型必须是中文的：原先用的 all-MiniLM-L6-v2 是英文模型，对中文几乎没有
  语义区分度 —— 实测查询「他玩 MOBA 手游时心情怎么样」召回的是「害怕被抛弃」
  这类无关记忆，相似度只有 0.25（随机水平）。
  现改为 shibing624/text2vec-base-chinese（768 维，本地已有缓存）。

⚠ 换模型的代价：两个模型维度不同（384 vs 768），同一个 ChromaDB collection
  里不能混存，必须清空向量库重建。重建只需重跑一次全量 encode，不丢数据
  （记忆正文在主库，向量只是可重建的索引）。
"""
import os
import hashlib
import math

# ★ 离线优先（2026-09-17 修，位置很关键）：
#   模型已经在本地缓存里，任何一次"联网探测"都是纯粹的浪费 —— 而没有这行时，
#   huggingface_hub 会对 huggingface.co 发 HEAD 请求，国内网络超时后**重试 5 次、
#   间隔指数增长**（1/2/4/8/8 秒），一轮就是几十秒。
#   原先这些变量只在 `_load_model()` 里 `setdefault`，也就是**首次加载模型之后**
#   才生效 —— 而在它之前发生的任何 hub 请求（如 sentence_transformers 初始化时的
#   config 探测）都享受不到，实测会把一次 set_vector 拖到 90 秒以上
#   （本项目的测试套件就是被这个卡死的：batch6 的撤回用例触发向量写入 → 加载模型
#    → 联网重试 → 整套测试 5 分钟超时）。
#   放在模块导入时设置，早于任何 hub 调用；仍用 setdefault，用户显式设了就不覆盖。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# 可用环境变量 EMBEDDING_MODEL 覆盖（例如临时退回英文模型做对比）
DEFAULT_MODEL = "shibing624/text2vec-base-chinese"
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "").strip() or DEFAULT_MODEL

# 必须与所选模型一致：text2vec-base-chinese = 768，all-MiniLM-L6-v2 = 384。
# 降级用的哈希向量也按这个维度生成，保证同一个 collection 里维度统一。
EMBEDDING_DIM = 768

_model = None          # 常驻实例（CPU 优先：单条嵌入 CPU 更稳，免 CPU↔显存传输开销）
_gpu_model = None      # 批量实例（懒加载：仅批量重建时上 GPU）
_available = None


def is_available():
    """检查 sentence-transformers 是否可用"""
    global _available
    if _available is not None:
        return _available
    try:
        import sentence_transformers  # noqa: F401
        _available = True
    except ImportError:
        _available = False
    return _available


def _load_model(device: str):
    """加载指定设备的模型实例（含离线/镜像环境设置）。"""
    # 国内网络：默认走 hf-mirror 镜像（已设则不覆盖）
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    # ★ 离线优先：模型已缓存在本地，否则加载时会去 huggingface.co 做 HEAD
    #   探测（查 adapter_config.json），国内网络超时重试 5 次，每次多卡几十秒。
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME, device=device)
    print(f"[embedding] 已加载模型 {MODEL_NAME}（{EMBEDDING_DIM} 维，device={device}）", flush=True)
    return model


def get_model():
    """
    获取常驻模型实例（★ CPU 优先——单条实时嵌入走 CPU）。

    设备分工（2026-09-08 与用户确认的方案）：
      - 单条实时嵌入（每条消息的记忆写入/检索）→ CPU：免 CPU↔显存传输开销，
        且不与游戏/显示抢 GPU；单句 CPU 编码仅 10~30ms，足够。
      - 批量重建（外置记忆库全量重嵌、向量库修复）→ encode_batch()：GPU 快 10 倍以上。
    如果依赖未安装，返回 None。
    """
    global _model
    if _model is not None:
        return _model
    if not is_available():
        return None
    try:
        _model = _load_model("cpu")
        return _model
    except Exception as e:
        print(f"[embedding] 加载模型失败: {e}", flush=True)
        return None


def _get_gpu_model():
    """批量用 GPU 实例（懒加载：只在批量重建时占用显存）。GPU 不可用回退常驻实例。"""
    global _gpu_model
    if _gpu_model is not None:
        return _gpu_model
    try:
        import torch
        if torch.cuda.is_available():
            _gpu_model = _load_model("cuda")
            return _gpu_model
    except Exception as e:
        print(f"[embedding] GPU 实例加载失败（回退 CPU）: {e}", flush=True)
    _gpu_model = get_model()
    return _gpu_model


def encode(text):
    """
    将文本编码为向量。
    如果 sentence-transformers 可用，使用模型编码；
    否则使用简单的哈希向量作为降级方案。
    返回归一化的向量列表。
    """
    model = get_model()
    if model is not None:
        try:
            vector = model.encode(
                text,
                normalize_embeddings=True
            )
            return vector.tolist()
        except Exception as e:
            print(f"[embedding] 模型编码失败: {e}", flush=True)

    # 降级方案：使用哈希生成固定维度的伪向量
    return _hash_embedding(text)


def encode_batch(texts):
    """批量编码（★ GPU 优先）：重建/回填等批量场景用，一条列表进、向量列表出。

    GPU 批量的真实优势场景：几十~几千条一起算（传输开销被摊薄，快 10 倍以上）。
    GPU 不可用/加载失败 → 自动回退 CPU 实例；再失败 → 逐条哈希降级。
    """
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []
    model = _get_gpu_model()
    if model is not None:
        try:
            vectors = model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=64,
                show_progress_bar=False,
            )
            return [v.tolist() for v in vectors]
        except Exception as e:
            print(f"[embedding] 批量编码失败（回退逐条 CPU）: {e}", flush=True)
    return [_hash_embedding(t) for t in texts]


def _hash_embedding(text, dim=None):
    # 维度跟随所选模型，避免与真实向量混存导致 collection 维度冲突
    if dim is None:
        dim = EMBEDDING_DIM
    """
    简单的哈希向量降级方案。
    使用多个哈希函数生成固定维度的向量，并归一化。
    虽然不如真实 embedding，但能保证系统正常运行。
    """
    text = str(text or "")
    vector = [0.0] * dim

    # 使用多个种子生成哈希
    for seed in range(8):
        h = hashlib.md5(f"{seed}:{text}".encode()).hexdigest()
        # 将哈希值映射到向量维度
        for i in range(0, len(h), 2):
            if i + 1 < len(h):
                idx = int(h[i:i+2], 16) % dim
                val = (int(h[i+1], 16) - 7.5) / 7.5  # 归一化到 -1~1
                vector[idx] += val

    # 归一化
    norm = math.sqrt(sum(v * v for v in vector))
    if norm > 0:
        vector = [v / norm for v in vector]

    return vector


def cosine_similarity(vec1, vec2):
    """计算两个向量的余弦相似度"""
    if not vec1 or not vec2:
        return 0.0
    dot = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)
