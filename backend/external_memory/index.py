# -*- coding: utf-8 -*-
"""外置记忆库的「原文片段」本地检索索引。

为什么需要它（2026-09-13 实测）：
  `retriever.build_memory_context()` 收到的 user_text 从头到尾没用过，注入纯按时间
  分层：近 7 天原文 + 日/周/月总结拼满 4000 字。于是
    · 费 token：闲聊轮也照付整块；
    · 不准：问三周前的事，命中的是月总结里被压过的残渣，
      而**真正有答案的原文日档根本没进检索范围**（原文只按天取尾部 1200 字）。
  这里把原文日档切成带时间戳的片段、建本地向量索引，让「相关的那几段原文」
  能被直接取出来 —— 名字、日期、原话都在原文里，准确性天然就高。

设计要点：
  · 复用项目已有的本地中文 embedding（memory.embedding，768 维，CPU 单条 10~30ms），
    不引入新依赖、不调用任何外部 API，零 token 成本。
  · 索引存 SQLite（跟项目其它库一样，无并发写风险），存在外置记忆库目录下的
    `.index/archive_vectors.db` —— 可随时删掉重建。
  · **按天重建**：原文日档是 append-only 的，当天文件一直在追加。用「内容 sha1」
    判断某天变没变，变了就整日重切重建（先删该日所有片段再插），避免同一段内容
    被嵌入多份、把真正相关的那段挤出 top-k。
  · 索引只是「可重建的加速结构」：任何一步失败都返回空，调用方回落到原来的
    时间分层检索，绝不影响主流程。
"""
import hashlib
import json
import re
import sqlite3
import struct
import threading
import time

from . import paths

# 片段长度（字符）：一段大约 3~6 条消息，既够看出前因后果，又不至于一次带进太多噪音
CHUNK_CHARS = 420
# 相邻片段重叠：防止关键那句正好被切在边界上，两边都读不全
CHUNK_OVERLAP = 100
# 单次检索最多返回几个片段
SEARCH_TOP_K = 4
# 相似度下限：低于这个值直接丢（兜底用，真正起作用的是下面的相对门槛）
MIN_SCORE = 0.30
# 相对门槛：只保留「和最佳命中差不超过这么多」的片段。
# ★ 为什么必须有：text2vec-base-chinese 这类对比学习模型有"高相似度地板" ——
#   实测完全无关的片段也能到 0.49（相关的是 0.53），绝对阈值根本切不开。
RELATIVE_GAP = 0.08
# 词面命中在最终排序里的权重（其余给向量语义）
# ★ 0.45 → 0.30：实测"提问"和"事实陈述"常常没有词面重叠
#   （问「给猫取的名字是啥」，原文说「叫汤圆怎么样」），
#   词面权重给太高会把"只是碰巧带同一个字"的片段顶上来，反而压掉真正相关的那段。
# ★ 2026-09-17 再次下调 0.30 → 0.20，并改为**按提问长度自适应**（见 short_query_lex_weight）：
#   实测真机 trace 里 22.2% 的提问 ≤4 个字（"做吗""我想""嗯嗯"），
#   2-gram 特征在这种长度下几乎必然命中一切 → lex=1.0 把 vec=0.39 的
#   09-08 图片描述顶到第一名。短提问必须交给语义，长提问词面才可信。
LEXICAL_WEIGHT = 0.20
# 短提问（≤ 这个字数）时词面权重再压一档：此时 2-gram 覆盖率没有区分度
SHORT_QUERY_CHARS = 6
SHORT_QUERY_LEX_WEIGHT = 0.08
# 命中"用户指的那一天"时加的权重 —— 用来压过泛化的老片段
RECENCY_BOOST = 0.06
# 当天（还在追加的原文）最短重建间隔（秒）。原文每来一条消息就变一次 sha1，
# 不节流会每个 tick 都重建一遍。
TODAY_REBUILD_SEC = 600

_LOCK = threading.RLock()
_DB_NAME = "archive_vectors.db"
_SCHEMA_VERSION = 1


# ══════════════════════════════════════════════════════════════════
# 依赖检查：embedding 不可用时不要建索引（哈希降级向量没有语义，建了反而误导）
# ══════════════════════════════════════════════════════════════════

def embedding_ready() -> bool:
    """真·语义模型是否可用（哈希降级不算）。"""
    try:
        from ..memory import embedding as _emb
        return _emb.get_model() is not None
    except Exception:
        return False


def embedding_model_name() -> str:
    try:
        from ..memory import embedding as _emb
        return str(getattr(_emb, "MODEL_NAME", "") or "")
    except Exception:
        return ""


def _dim() -> int:
    try:
        from ..memory import embedding as _emb
        return int(getattr(_emb, "EMBEDDING_DIM", 768) or 768)
    except Exception:
        return 768


# ══════════════════════════════════════════════════════════════════
# 索引库
# ══════════════════════════════════════════════════════════════════

def db_path(character_id: str):
    return paths.char_dir(character_id) / ".index" / _DB_NAME


def _connect(character_id: str):
    fp = db_path(character_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(fp), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS archive_chunk (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            seq INTEGER NOT NULL,
            first_time TEXT DEFAULT '',
            last_time TEXT DEFAULT '',
            text TEXT NOT NULL,
            vec BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_day ON archive_chunk(day);
        CREATE TABLE IF NOT EXISTS archive_day (
            day TEXT PRIMARY KEY,
            sha1 TEXT NOT NULL,
            chunks INTEGER NOT NULL DEFAULT 0,
            built_time TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS archive_meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );
        """
    )
    return conn


def _meta_get(conn, key: str) -> str:
    try:
        row = conn.execute("SELECT v FROM archive_meta WHERE k=?", (key,)).fetchone()
        return str(row["v"]) if row else ""
    except Exception:
        return ""


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO archive_meta(k, v) VALUES(?,?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )


# ══════════════════════════════════════════════════════════════════
# 切块
# ══════════════════════════════════════════════════════════════════

# 原文格式（archive.py 写入）：
#   ## 14:32
#   用户：……
#   AI：……
_MSG_RE = re.compile(r"^##\s*(\d{2}:\d{2})?\s*$")

# ★ 这些是**发给模型的系统指令**，不是聊天内容 —— 但会被当成"用户/AI 消息"写进
#   原文归档，检索时原样带回来就纯是噪音（实测注入里混进了【发语音】[VOICE]、
#   【音乐分享能力】[SONG] 这种整段指令）。建索引时直接剔除。
_INSTRUCTION_MARKS = (
    "【音乐分享能力】", "【发语音】", "【表情包】", "【能力清单】", "【防复读】",
    "【本轮用户刚刚说的话", "【你可以调用的工具】", "【任务】", "【规则】",
    "【当前场景", "【外置记忆库", "【近期经历摘要", "【知识", "【时间",
)
_INSTRUCTION_BRACKETS = (
    "[SONG]", "[/SONG]", "[VOICE]", "[/VOICE]", "[STICKER]", "[/STICKER]",
)


def _is_instruction(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    if t.startswith(_INSTRUCTION_MARKS):
        return True
    # 指令块常整段塞在一条消息里：开头是标记、或含两个以上 [XXX] 控制标记
    if sum(t.count(b) for b in _INSTRUCTION_BRACKETS) >= 2:
        return True
    return False


def _dedupe_lines(blocks: list) -> list:
    """去掉相邻重复行 —— 片段重叠 + 邻居扩展会把同一条消息带出来多次。"""
    seen = set()
    out = []
    for line in blocks:
        key = line.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def _parse_messages(text: str) -> list:
    """原文文本 → [{'t': 'HH:MM', 'body': '用户：…\\nAI：…'}, ...]"""
    msgs = []
    cur_t = ""
    buf = []
    for line in str(text or "").splitlines():
        m = _MSG_RE.match(line.strip())
        if m:
            if buf:
                msgs.append({"t": cur_t, "body": "\n".join(buf).strip()})
                buf = []
            cur_t = m.group(1) or ""
            continue
        if line.strip():
            buf.append(line.rstrip())
    if buf:
        msgs.append({"t": cur_t, "body": "\n".join(buf).strip()})
    # 剔除系统指令块（不是聊天内容），再剔除空消息
    msgs = [m for m in msgs if m["body"] and not _is_instruction(m["body"])]
    return msgs


def chunk_text(text: str) -> list:
    """把一天的原文切成带时间信息的片段。返回 [{'text','first_time','last_time'}, ...]"""
    msgs = _parse_messages(text)
    if not msgs:
        # 解析不出来（格式变了）时退化成按长度硬切，保证索引不至于全空
        raw = str(text or "").strip()
        if not raw:
            return []
        out = []
        step = max(1, CHUNK_CHARS - CHUNK_OVERLAP)
        for i in range(0, len(raw), step):
            piece = raw[i:i + CHUNK_CHARS].strip()
            if piece:
                out.append({"text": piece, "first_time": "", "last_time": ""})
        return out

    chunks = []
    cur = []
    cur_len = 0
    for msg in msgs:
        cur.append(msg)
        cur_len += len(msg["body"]) + 8
        if cur_len >= CHUNK_CHARS:
            chunks.append(cur)
            # 重叠：保留最后一条消息，避免关键句被切在边界
            cur = cur[-1:]
            cur_len = sum(len(x["body"]) + 8 for x in cur)
    if cur:
        chunks.append(cur)

    out = []
    for group in chunks:
        lines = []
        for m in group:
            for ln in str(m["body"]).splitlines():
                # 一条消息内部也可能夹着指令行（归档会把注入块拼进同一条）
                if _is_instruction(ln):
                    continue
                lines.append((f"【{m['t']}】" if m["t"] else "") + ln)
        lines = _dedupe_lines(lines)
        body = "\n".join(lines).strip()
        if not body:
            continue
        times = [m["t"] for m in group if m["t"]]
        out.append({
            "text": body,
            "first_time": times[0] if times else "",
            "last_time": times[-1] if times else "",
        })
    return out


# ══════════════════════════════════════════════════════════════════
# 建索引
# ══════════════════════════════════════════════════════════════════

def _pack(vec) -> bytes:
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])


def _unpack(blob: bytes):
    n = len(blob) // 4
    return struct.unpack("<%df" % n, blob)


def _day_sha1(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()


def _read_day(character_id: str, day: str) -> str:
    try:
        return (paths.raw_dir(character_id) / (day + ".md")).read_text("utf-8")
    except Exception:
        return ""


def build_index(character_id: str, force: bool = False,
                max_days: int = 2, max_seconds: float = 25.0) -> dict:
    """增量重建索引：只处理「新增或内容变了」的天。

    max_days / max_seconds：单次调用的工作量上限（挂在 5 分钟一次的 tick 上，
    历史补齐分几轮跑完，不占着启动时间）。索引落后不影响正确性，只是检索少几天。

    返回 {'built': [...], 'skipped': int, 'pending': int, 'reason': str}
    """
    result = {"built": [], "skipped": 0, "pending": 0, "reason": ""}
    if not embedding_ready():
        result["reason"] = "embedding 不可用（缺 sentence-transformers 或模型未缓存）"
        return result

    raw_dir = paths.raw_dir(character_id)
    if not raw_dir.exists():
        result["reason"] = "没有原文目录"
        return result

    model_name = embedding_model_name()
    dim = _dim()

    with _LOCK:
        try:
            conn = _connect(character_id)
        except Exception as e:
            result["reason"] = f"打不开索引库: {e}"
            return result

        try:
            # ★ 换过 embedding 模型 → 维度/语义都变了，旧向量全部作废，整库重建
            if _meta_get(conn, "model") != model_name or _meta_get(conn, "dim") != str(dim):
                if _meta_get(conn, "model") or _meta_get(conn, "dim"):
                    conn.execute("DELETE FROM archive_chunk")
                    conn.execute("DELETE FROM archive_day")
                    result["reason"] = "embedding 模型/维度变化，已清空重建"
                _meta_set(conn, "model", model_name)
                _meta_set(conn, "dim", str(dim))

            # ★ 2026-09-13 改：**今天也要进索引**。
            #   原先 `if day >= today: continue` 跳过当天，理由是"原文还在追加、
            #   等它定稿再总结"—— 但那条理由是给**日总结**的，不是给检索的：
            #   实测用户问「昨天刚跟你说…」时检索不到当天内容，
            #   而"我今天上午刚说的"恰好落在当天 → 表现成"最近的事不记得"。
            #   索引本来就是按内容 sha1 增量重建的，当天追加会自动触发重建，
            #   代价只是当天多重建几次（每次几秒），换来"今天的事能搜到"。
            days = []
            today = time.strftime("%Y-%m-%d")
            for fp in sorted(raw_dir.glob("*.md")):
                day = fp.stem
                txt = _read_day(character_id, day)
                if not txt.strip():
                    continue
                sha = _day_sha1(txt)
                row = conn.execute(
                    "SELECT sha1, built_time FROM archive_day WHERE day=?", (day,)
                ).fetchone()
                if row and row["sha1"] == sha and not force:
                    result["skipped"] += 1
                    continue
                # ★ 当天节流：原文是 append-only 的，每来一条消息就变一次 sha1。
                #   不节流的话每个 tick（5 分钟）都重建当天，白烧算力。
                #   隔 TODAY_REBUILD_SEC 才重建一次，平衡"最近的事搜得到"和开销。
                if day == today and row and not force:
                    try:
                        _last = time.mktime(time.strptime(
                            str(row["built_time"] or ""), "%Y-%m-%dT%H:%M:%S"))
                        if (time.time() - _last) < TODAY_REBUILD_SEC:
                            result["skipped"] += 1
                            continue
                    except Exception:
                        pass
                days.append((day, txt, sha))

            if not days:
                conn.commit()
                return result

            # ★ 日期越新越优先：当天每次追加都会让它的 sha1 变化、需要重建，
            #   如果按日期升序处理，"今天"永远排在最后 —— 每轮配额（默认 2 天）
            #   都被没建过的历史日期吃掉，今天反而一直进不了索引，
            #   正是"最近的事搜不到"的直接原因。按新→旧排序让今天先建。
            days.sort(key=lambda x: x[0], reverse=True)

            pending = max(0, len(days) - max_days)
            result["pending"] = pending
            todo = days[:max_days]
            deadline = time.time() + max(0.0, max_seconds)

            from ..memory.embedding import encode_batch as _encode_batch

            for day, txt, sha in todo:
                if time.time() > deadline and result["built"]:
                    result["pending"] += 1
                    break
                chunks = chunk_text(txt)
                if not chunks:
                    continue
                try:
                    vecs = _encode_batch([c["text"] for c in chunks])
                except Exception as e:
                    result["reason"] = f"嵌入失败: {e}"
                    break
                if len(vecs) != len(chunks):
                    result["reason"] = "嵌入数量不匹配"
                    break

                # ★ 整日替换：先删该日全部片段再插，避免「删了一半、插了一半」
                #   留下重复片段污染检索结果。
                #   注意**不要手写 BEGIN** —— Python 的 sqlite3 默认在 DML 前隐式开启
                #   事务，再显式 BEGIN 会报 "cannot start a transaction within a
                #   transaction"（实测踩过），整段删除+插入就全部落空。
                #   默认隔离级别下，DELETE 到 commit() 之间本来就在同一个事务里。
                conn.execute("DELETE FROM archive_chunk WHERE day=?", (day,))
                conn.executemany(
                    "INSERT INTO archive_chunk"
                    "(day, seq, first_time, last_time, text, vec) VALUES(?,?,?,?,?,?)",
                    [
                        (day, i, c["first_time"], c["last_time"], c["text"], _pack(v))
                        for i, (c, v) in enumerate(zip(chunks, vecs))
                    ],
                )
                conn.execute(
                    "INSERT INTO archive_day(day, sha1, chunks, built_time) VALUES(?,?,?,?) "
                    "ON CONFLICT(day) DO UPDATE SET sha1=excluded.sha1, "
                    "chunks=excluded.chunks, built_time=excluded.built_time",
                    (day, sha, len(chunks), time.strftime("%Y-%m-%dT%H:%M:%S")),
                )
                conn.commit()
                result["built"].append(day)

            conn.commit()
        except Exception as e:
            result["reason"] = f"建索引异常: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return result


def pending_days(character_id: str) -> int:
    """还有多少天没进索引（前端/日志用）。"""
    try:
        today = time.strftime("%Y-%m-%d")
        raw_dir = paths.raw_dir(character_id)
        if not raw_dir.exists():
            return 0
        conn = _connect(character_id)
        done = {r["day"] for r in conn.execute("SELECT day FROM archive_day")}
        conn.close()
        n = 0
        for fp in raw_dir.glob("*.md"):
            if fp.stem < today and fp.stem not in done:
                n += 1
        return n
    except Exception:
        return 0


# ══════════════════════════════════════════════════════════════════
# 检索
# ══════════════════════════════════════════════════════════════════

def _cosine(a, b) -> float:
    """两个已归一化向量的余弦（本地 embedding 都是 normalize_embeddings=True）。"""
    n = min(len(a), len(b))
    return sum(a[i] * b[i] for i in range(n))


_STOP = set("的了是我你他她它们在有和就都也还吗呢吧啊哦嗯这那什么怎么一个不没要会能"
            "说想问去来到好看听对给把被让很太真")


# 问句/口语虚词：抽词面特征前先去掉，否则
# 「给猫取的名字**是什么**」的特征会被"什么/是"带偏，抓不到真正的"猫""取""名"。
_Q_NOISE = (
    "是什么", "是啥", "是哪个", "有哪些", "有没有", "还记得", "记得吗", "你知道",
    "告诉我", "请问", "怎么", "怎样", "如何", "为什么", "多少", "哪个", "哪里",
    "什么时候", "我们", "咱们", "你们", "他们", "我的", "你的", "他的",
    "了吗", "呢", "吧", "啊", "呀", "嘛", "么", "哦", "嗯", "那个", "这个",
)


def _bigrams(text: str) -> set:
    """中文按 2-gram 取词面特征（不引 jieba，零依赖）。

    单字噪音太大（"我""的"命中一切），单字以外的连续 2 字足以抓住
    「猫」「屏幕共享」这类真正有区分度的词面线索。
    """
    s = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", "", str(text or ""))
    if len(s) < 2:
        return {s} if s and s not in _STOP else set()
    grams = {s[i:i + 2] for i in range(len(s) - 1)}
    return {g for g in grams if g not in _STOP}


def _query_features(query: str) -> tuple:
    """把查询拆成 (主特征, 兜底特征)。

    主特征：去掉问句词后剩下的连续片段里的 2-gram（例如「给猫取的名字是什么」
            → 去掉"是什么" → 「给猫取的名字」→ {给猫, 猫取, 取的, 的名, 名字}）。
            "猫取"这种跨词 gram 虽然怪，但它让「我们一起给它取一个」「草稿拟几版」
            这类片段能被词面命中到 —— 这正是提问与陈述用词不一致时的补桥。
    兜底特征：原始查询的 2-gram，主特征全落空时用。
    """
    raw = str(query or "")
    cleaned = raw
    for w in _Q_NOISE:
        cleaned = cleaned.replace(w, " ")
    primary = _bigrams(cleaned)
    fallback = _bigrams(raw)
    return (primary or fallback), fallback


def _lexical_score(q_grams: set, text: str) -> float:
    """查询词面在片段里的覆盖率（0~1）。"""
    if not q_grams:
        return 0.0
    t = _bigrams(text)
    if not t:
        return 0.0
    return len(q_grams & t) / float(len(q_grams))


# ★ 时间锚点护栏（2026-09-13 实测新增）
#   实测发现：「今天晚饭吃什么了」这种**短问句 + 近期时间词**的查询，
#   检索会把三周前的晚饭对话捞上来（泛化聊天语气让 vec 分不低）。
#   问"今天"却注入上周的原文，恰恰会造成"记混"—— 本末倒置。
#   所以：查询带明确近期时间词时，只接受近 _RECENT_WINDOW_DAYS 天的片段；
#   一个都不剩就让调用方回落时间分层兜底（那本来就是近期的）。
_RECENT_WORDS = (
    "今天", "今日", "昨天", "昨晚", "今早", "今天晚上", "刚才", "刚刚",
    "这会儿", "现在", "今儿", "今晚", "待会", "等下", "明天",
)
_RECENT_WINDOW_DAYS = 7

# ★ 相对时间词 → 具体是哪一天/哪几天（2026-09-13 新增）
#   实测根因：用户说「昨天刚跟你说还没部署成功」，护栏只知道"要搜近 7 天"，
#   但**没把"昨天"解读成 9-12** —— 于是 7 天里的 9-06~9-11 照样赢，
#   用户要的那一天（9-12）连前 10 都进不去，表现成"最近的事不记得"。
#   key = 词，value = (起始偏移天, 结束偏移天)，相对今天；0=今天，-1=昨天。
_REL_DAY_RANGES = (
    ("前天", (-2, -2)),
    ("昨天", (-1, -1)),
    ("昨晚", (-1, -1)),
    ("昨夜", (-1, -1)),
    ("今早", (0, 0)),
    ("今儿", (0, 0)),
    ("今天", (0, 0)),
    ("今日", (0, 0)),
    ("今晚", (0, 0)),
    ("刚才", (0, 0)),
    ("刚刚", (0, 0)),
    ("这会儿", (0, 0)),
    ("现在", (0, 0)),
    ("今天早上", (0, 0)),
    ("今天下午", (0, 0)),
    ("今天晚上", (0, 0)),
    ("上个星期", (-14, -7)),
    ("上周", (-14, -7)),
    ("上个月", (-60, -30)),
    ("前几天", (-6, -2)),
    ("前几天", (-6, -2)),
    ("这两天", (-2, 0)),
    ("最近", (-7, 0)),
)


def _day_shift(days: int) -> str:
    """相对今天偏移 N 天的日期字符串；算不出来返回空串。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
    except Exception:
        return ""


def parse_time_window(query: str) -> tuple:
    """把查询里的相对时间词解读成具体日期区间。

    返回 (lo, hi)，都是 'YYYY-MM-DD'；没有可解读的时间词时返回 ('', '')。
    多个时间词同时出现时取**最窄**的那个（"昨天"比"最近"更明确）。
    """
    q = str(query or "")
    best = None
    for word, (a, b) in _REL_DAY_RANGES:
        if word in q:
            span = b - a
            if best is None or span < best[0]:
                best = (span, a, b)
    if best is None:
        return ("", "")
    lo = _day_shift(best[1])
    hi = _day_shift(best[2])
    return (lo, hi)


def has_recent_anchor(query: str) -> bool:
    """查询里是否带明确指向近期的时间词。"""
    q = str(query or "")
    return any(w in q for w in _RECENT_WORDS)


def _day_floor(days: int) -> str:
    """N 天前的日期字符串（YYYY-MM-DD）；算不出来返回空串（等于不限制）。"""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    except Exception:
        return ""


def query_lex_weight(query: str) -> float:
    """按提问长度决定词面权重。

    ★ 2026-09-17 新增。真机 trace 实测：22.2% 的提问 ≤4 个字，
    这种长度下 2-gram 覆盖率毫无区分度（"做吗"能命中任何含"做"或"吗"的片段），
    而 text2vec 的余弦中位数只有 0.555、向量本身区分度也弱，
    于是"碰巧同字 + 向量地板分"的片段会稳定压过真正相关的那段。
    短提问交给语义、长提问才让词面参与，是这里唯一能拿到的校准手段。
    """
    q = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", "", str(query or ""))
    if len(q) <= SHORT_QUERY_CHARS:
        return SHORT_QUERY_LEX_WEIGHT
    return LEXICAL_WEIGHT


def _final_score(vec: float, lex: float, query_len: int = 99) -> float:
    """融合分：向量语义 (1-w) + 词面覆盖 w，w 按提问长度自适应。"""
    try:
        q_len = int(query_len)
    except Exception:
        q_len = 99
    w = SHORT_QUERY_LEX_WEIGHT if q_len <= SHORT_QUERY_CHARS else LEXICAL_WEIGHT
    return float(vec) * (1.0 - w) + float(lex) * w


def search(character_id: str, query: str, top_k: int = SEARCH_TOP_K,
           min_score: float = MIN_SCORE) -> list:
    """按语义 + 词面命中取最相关的原文片段。

    返回 [{'day','text','score','vec','lex','first_time','last_time','start','end'}, ...]

    ★ 为什么要混词面：纯向量在这类中文句向量上区分度不够（见文件顶部常量注释）。
      混入词面命中后，「猫」「屏幕共享」这种真关键词能把真正相关的那段顶上来，
      而完全不带共同词的片段即便向量分不低也会被压下去。
    """
    q = str(query or "").strip()
    if not q:
        return []
    if not embedding_ready():
        return []
    fp = db_path(character_id)
    if not fp.exists():
        return []

    try:
        from ..memory.embedding import encode as _encode
        qv = _encode(q[:512])
    except Exception:
        return []
    if not qv:
        return []

    q_grams, q_fallback = _query_features(q)
    _ql = len(re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", "", q))

    rows = []
    with _LOCK:
        try:
            conn = _connect(character_id)
            rows = conn.execute(
                "SELECT day, seq, first_time, last_time, text, vec FROM archive_chunk"
            ).fetchall()
            conn.close()
        except Exception:
            return []

    scored = []
    for r in rows:
        try:
            v = _cosine(qv, _unpack(r["vec"]))
        except Exception:
            continue
        lex = _lexical_score(q_grams, r["text"])
        if lex <= 0.0 and q_fallback:
            # 主特征一个都没命中（问句词去掉后太短/太怪）时用原始特征兜底
            lex = _lexical_score(q_fallback, r["text"]) * 0.7
        final = _final_score(v, lex, _ql)
        if final < min_score:
            continue
        scored.append({
            "day": r["day"],
            "seq": int(r["seq"] or 0),
            "text": r["text"],
            "score": round(float(final), 4),
            "vec": round(float(v), 4),
            "lex": round(float(lex), 4),
            "first_time": r["first_time"] or "",
            "last_time": r["last_time"] or "",
        })

    if not scored:
        return []

    # ★ 时间锚点护栏：查询带"今天/昨天/刚才"这类近期词时，丢掉太老的片段。
    #   否则问"今天晚饭吃了啥"会注入三周前的晚饭对话 —— 那不是"记得"，是"记混"。
    #   全被丢掉时返回空，让调用方回落时间分层兜底（那条路本来就是近期的）。
    #
    # ★ 2026-09-13 增强：从"只筛近 7 天"升级为"**解读 + 加权 + 兜底**"。
    #   根因实测：用户说「昨天刚跟你说还没部署成功」，护栏知道要筛近 7 天，
    #   却没把"昨天"解读成 9-12 —— 7 天里的 9-06~9-11 照样赢，
    #   用户真正要的那一天连前 10 都进不去，表现成"最近的事不记得"。
    day_lo, day_hi = parse_time_window(q)
    if day_lo:
        # ① 命中用户指的那一天（或那天区间）→ 加分，让它压过泛化的老片段
        in_window = [s for s in scored if day_lo <= str(s["day"]) <= (day_hi or day_lo)]
        if in_window:
            for s in in_window:
                s["score"] = round(min(1.0, float(s["score"]) + RECENCY_BOOST), 4)
                s["time_hit"] = True
        else:
            # ② 用户指的那天**没有片段**（比如今天还没进索引）→
            #    退回 7 天窗口，但至少不要答出三周前的内容
            floor = _day_floor(_RECENT_WINDOW_DAYS)
            if floor:
                within = [s for s in scored if str(s["day"]) >= floor]
                if within:
                    scored = within
                else:
                    return []
    elif has_recent_anchor(q):
        floor = _day_floor(_RECENT_WINDOW_DAYS)
        if floor:
            within = [s for s in scored if str(s["day"]) >= floor]
            if within:
                scored = within
            else:
                return []

    scored.sort(key=lambda x: x["score"], reverse=True)
    best = scored[0]["score"]

    # 相对门槛：只留和最佳命中接近的（挡住"地板分"片段）
    kept = [s for s in scored if (best - s["score"]) <= RELATIVE_GAP] or scored[:1]

    # 同一天最多取 2 段：避免一天的内容把整个片段预算占满（那又变成"只记得某一天"）
    out = []
    per_day = {}
    for item in kept:
        d = item["day"]
        if per_day.get(d, 0) >= 2:
            continue
        per_day[d] = per_day.get(d, 0) + 1
        out.append(item)
        if len(out) >= int(top_k):
            break
    return out


def search_with_neighbors(character_id: str, query: str,
                          top_k: int = SEARCH_TOP_K,
                          span: int = 1) -> list:
    """在 search() 基础上带上**前后各 span 个片段**（同一事务里的上下文）。

    片段是硬切的，关键那句可能正好落在边界、另一半在邻居里。
    带邻居能让模型看到完整的前因后果，代价是片段变长 —— 调用方控制预算。
    """
    hits = search(character_id, query, top_k=top_k)
    if not hits:
        return []
    out = []
    with _LOCK:
        try:
            conn = _connect(character_id)
            for h in hits:
                rows = conn.execute(
                    "SELECT seq, first_time, last_time, text FROM archive_chunk "
                    "WHERE day=? ORDER BY seq",
                    (h["day"],),
                ).fetchall()
                # 用 seq 直接定位命中那段，再拼上前后邻居
                idx_pos = None
                for i, r in enumerate(rows):
                    if int(r["seq"] or 0) == int(h.get("seq") or 0):
                        idx_pos = i
                        break
                if idx_pos is None:
                    out.append(dict(h))
                    continue
                group = rows[max(0, idx_pos - span): idx_pos + span + 1]
                # 相邻片段是有意重叠的（防止关键句被切在边界），拼起来时要去重，
                # 否则同一条消息会连着出现两遍，白占注入预算还把模型绕晕。
                merged = "\n".join(_dedupe_lines(
                    [ln for r in group for ln in str(r["text"]).splitlines()]
                ))
                item = dict(h)
                item["text"] = merged
                item["first_time"] = group[0]["first_time"] or h["first_time"]
                item["last_time"] = group[-1]["last_time"] or h["last_time"]
                out.append(item)
            conn.close()
        except Exception:
            return [dict(h) for h in hits]
    return out


def stats(character_id: str) -> dict:
    """索引状态（诊断用）。"""
    try:
        conn = _connect(character_id)
        chunks = conn.execute("SELECT COUNT(*) c FROM archive_chunk").fetchone()["c"]
        days = conn.execute("SELECT COUNT(*) c FROM archive_day").fetchone()["c"]
        meta = {
            r["k"]: r["v"] for r in conn.execute("SELECT k, v FROM archive_meta")
        }
        conn.close()
        return {
            "chunks": int(chunks),
            "days": int(days),
            "model": meta.get("model", ""),
            "dim": meta.get("dim", ""),
            "pending_days": pending_days(character_id),
            "db": str(db_path(character_id)),
        }
    except Exception as e:
        return {"error": str(e)}
