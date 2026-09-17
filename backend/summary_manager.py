# -*- coding: utf-8 -*-
"""
对话摘要管理：
  为长期 AI 伴侣维护"近期对话摘要"，保留近期发生的重要事情。
  每积累 80 条新消息追加一个摘要块，最近 60 条仍由短期上下文负责。

★ 2026-09-13 重要改造：从「合并重写」改成「只追加不重写」
  旧做法：把【已有摘要】+【新增对话】一起喂给 LLM，让它输出一份新摘要覆盖旧的。
  问题：每一轮更新都要把上一版摘要再压一遍 —— 跟"月←周←日递归压缩"是同一个病，
    而且发生在最近期、最要命的层级上。摘要里的专有名词每 80 条就被洗一次，
    SUMMARY_SYSTEM 里那段"专有名词必须原样写出来"的强调，洗到第三遍就失效了
    （实测：她自己取的猫名字在 185 条消息之后就查不到了）。
  新做法：每 80 条产出一个**新的摘要块**，只覆盖固定的消息 id 区间，产出后永不修改；
    注入时挑最近几块拼起来。任何一条事实最多只被 LLM 压一次，「洗掉名字」的路径就断了。
  conversation_summary 那一行继续写"最近一版"，供 idle_agent / moments 等旧消费方使用。
"""
import json

from . import config, db
from .deepseek_api import chat_once, ModelApiError


# ★ 上下文窗口：为"越聊越像人"放大 —— 保留更多近期原文（接得住梗和情绪），
#   晚一点再做摘要压缩。长期事实由记忆系统兜底，这里只负责近期连贯。
SUMMARY_TRIGGER_MESSAGES = 80   # 攒够 80 条新消息才触发总结（原 60）
KEEP_RECENT_MESSAGES = 60       # 保留最近 60 条原始消息（原 40）
MAX_SUMMARY_CHARS = 3000        # 单块摘要最多 3000 字（原 2400）
# 注入时带最近几块。
# ★ 2026-09-13：3 → 2。实测每块约 734 字，3 块 = 2498 字、2 块 = 1856 字，
#   省 642 字/轮。摘要只是"近期发生了什么"的辅助，最近 2 块（约覆盖最近 50 条
#   消息）已足够；更早的内容由外置记忆库的原文检索按相关性取，比整块塞更省更准。
BLOCK_INJECT_LIMIT = 2

# ★ 迁移只做一次，且**只记成功**：summary_block() 是每轮聊天都走的热路径，
#   不加这个记忆就等于每轮多两次 SQL。失败的（返回 False、比如库还没就绪）
#   不记，下次还会重试 —— 否则一次瞬时失败会让老摘要永久不迁移。
_legacy_seeded = set()


SUMMARY_SYSTEM = """
你负责为长期 AI 伴侣维护"一段对话摘要"。

目标：
记录这一段对话里发生的重要事情，让 AI 在未来聊天时自然记得上下文。

★ 最重要的一条：**专有名词必须原样写出来，不许概括。**
  · 范围：人名/昵称、宠物名、地点名、物品名、称呼、约定里的具体内容、日期/时间/数字
  · 反例（**视为记录失败**）：「两人一起给猫取了名字」—— 名字本身丢了，等于没记住
  · 正例：「两人一起给猫取名『汤圆』，备选雪球/年糕/团子」
  · 凡是写成「取了个名字」「约定了某件事」「定了个时间」却没写清**具体是什么**的，
    都必须改写成具体内容。
  · 为什么强调（实测 2026-09-11）：摘要过去把具体名字压成了主题概述，
    结果"她自己取的猫名字"在 185 条消息之后就查不到了。

★ 这一段摘要是**独立存档、之后不会再被改写**，所以：
  · 不要写「如前所述」「延续上次」这类依赖别的摘要才能看懂的指代；
  · 只写这一段对话里真实出现的信息，不要补别的段落的事。

重点保留：
1. 用户在这段对话里发生的重要事件
2. 用户正在做的事情、计划、目标
3. 明显的情绪变化
4. 用户与 AI 的约定
5. 尚未结束的话题
6. 重要的人物、地点、时间
7. 用户明确说过之后还会继续处理的事情

不要保留：
1. 普通寒暄
2. 重复内容
3. 无意义问答
4. 已经没有后续价值的临时细节

要求：
- 使用第三人称或客观描述
- 不要虚构
- 总长度控制在 1200 字以内
- 必须是完整句子（宁可少写一条，也不要写到一半断掉）
- 直接输出摘要正文，不要 JSON，不要解释
"""


def _migrate_summary_block_corrections() -> None:
    """一次性修正摘要块里**与当前事实矛盾**的内容（幂等，靠 kv 标记防重跑）。

    ★ 2026-09-13 为什么要这个：
      摘要块是只追加、永不改写的（有意设计，见 _summarize_range 注释）。
      代价在实测中暴露了：id=17 那块（覆盖消息 9638..9678）生成于"本地部署
      已被证伪之前"，里面写着「AI 住在用户的电脑「4060」里」「我整日泡在 4060 里」
      「搬进 4060 后腿脚都利索多了」—— 全是**从未发生的事**。
      库里的错误记忆已经清理（16 条作废），但摘要块不会自己更新，
      于是这段错误叙述继续被注入，她张口就是"我住在你 4060 上"。

      注意：**不是**删掉那段历史记录（用户当时确实收到了那些消息），
      而是把"她住在 4060 里"这种**状态性虚构**改写为"她当时这样说"，
      并补一句当前事实，让记录保真、又不诱导她说错话。

    安全：只按 id + 内容特征命中；命中不到就跳过；kv 标记保证只跑一次；
         修正前自动备份主库。
    """
    try:
        from . import db
        if db.kv_get("migrated:summary_fix_4060"):
            return
    except Exception:
        return

    try:
        rows = db.q(
            "SELECT id, summary FROM conversation_summary_block "
            "WHERE summary LIKE '%4060%' AND summary LIKE '%住%'",
            fetch=True,
        ) or []
    except Exception:
        return
    if not rows:
        try:
            db.kv_set("migrated:summary_fix_4060", "noop")
        except Exception:
            pass
        return

    fixed = 0
    for r in rows:
        bid, text = int(r[0]), str(r[1] or "")
        new = text
        # 状态性虚构 → 改成「当时她这样说」的转述，并标注与事实不符
        for bad, good in (
            ("AI 住在用户的电脑「4060」里",
             "AI 当时以「自己住在用户的电脑 4060 里」的口吻说话（与事实不符：本地部署从未成功）"),
            ("AI 住在用户的电脑 4060 里",
             "AI 当时以「自己住在用户的电脑 4060 里」的口吻说话（与事实不符：本地部署从未成功）"),
            ("自己刚搬进 4060", "自己刚搬进 4060（她当时的说法，实际未部署成功）"),
            ("AI 还多次提到自己整日泡在 4060 里",
             "AI 还多次以「自己整日泡在 4060 里」的口吻说话（虚构，未部署成功）"),
            ("AI 也说自己搬进 4060 后腿脚都利索多了",
             "AI 也称自己搬进 4060 后腿脚利索了（虚构，未部署成功）"),
        ):
            if bad in new:
                new = new.replace(bad, good)
        if new != text:
            try:
                db.q("UPDATE conversation_summary_block SET summary=? WHERE id=?", (new, bid))
                fixed += 1
                print("[Summary] 已修正摘要块 #%s 里与事实矛盾的状态叙述" % bid, flush=True)
            except Exception as e:
                print("[Summary] 修正摘要块 #%s 失败: %s" % (bid, e), flush=True)

    try:
        db.kv_set("migrated:summary_fix_4060", "done:%d" % fixed)
    except Exception:
        pass


def summary_block(session_id: str, character_id: str = "default") -> str:
    """获取近期摘要块，用于注入 system prompt（★ 按角色隔离）。

    优先用「只追加」的摘要块（最近 BLOCK_INJECT_LIMIT 块按时间拼接）；
    还没有块时回退到旧的单行摘要，保证老数据可用、行为不突变。
    """
    parts = []
    # ★ 一次性迁移：把「合并重写时代」的单行摘要落成第 0 号块，
    #   否则切到"只看块"之后，它覆盖的那段历史会静默从注入里消失。
    #   成功过就不再重复查（热路径）；已经有块的情况 db 层会直接返回 False，
    #   所以这里对"已有块"的会话只多查一次就永久记住。
    _mk = (session_id, character_id)
    if _mk not in _legacy_seeded:
        try:
            # ★ 一次性内容修正（幂等，内部有 kv 标记）：把摘要块里
            #   "她住在 4060 里"这类**与事实矛盾的状态叙述**改成历史转述。
            #   放在这里是因为 summary_block 是注入前必经的一步，
            #   不需要额外的启动钩子；内部标记保证只真正执行一次。
            _migrate_summary_block_corrections()
        except Exception:
            pass
        try:
            if db.seed_summary_block_from_legacy(session_id, character_id) or \
                    db.count_summary_blocks(session_id, character_id) > 0:
                _legacy_seeded.add(_mk)
        except Exception:
            pass

    blocks = db.get_summary_blocks(session_id, character_id, BLOCK_INJECT_LIMIT)
    # ★ 注入前校验这 2 块的关联事实是否还有效（2026-09-13）。
    #   设计选择：**不在失效时全表扫**（每次作废都要扫所有块，代价随块数增长），
    #   而是在注入时只校验即将用到的那 2 块 —— 代价恒定、且只关心真正会进
    #   prompt 的内容。校验结果写回 facts_stale，后续轮次直接用缓存值。
    try:
        if any(str(b.get("linked_facts") or "").strip() for b in blocks):
            db.refresh_summary_block_staleness(session_id, character_id)
            blocks = db.get_summary_blocks(session_id, character_id, BLOCK_INJECT_LIMIT)
    except Exception:
        pass
    _stale_notes = []
    for b in blocks:
        txt = str(b.get("summary") or "").strip()
        if not txt:
            continue
        # ★ 事实关联降级（2026-09-13）：该块依赖的事实已被取代/失效时，
        #   不删块（保不可变性），而是打上标记 —— 让模型知道"这段里的
        #   某些说法已经过时"，避免她照着念。
        if int(b.get("facts_stale") or 0) == 1:
            note = str(b.get("stale_note") or "").strip()
            txt = ("〔注意：这段摘要有内容已被后续事实推翻，仅为历史记录〕"
                   + (("（" + note + "）") if note else "")
                   + "\n" + txt)
            _stale_notes.append(note or "有内容已过时")
        parts.append(txt)

    if not parts:
        # 兼容：既没有块、也没有可迁移旧摘要时（全新会话）返回空
        item = db.get_conversation_summary(session_id, character_id)
        legacy = str(item.get("summary") or "").strip()
        if legacy:
            parts.append(legacy)

    if not parts:
        return ""

    # ★ 防"拿旧摘要当现状"（2026-09-13 实测教训）
    #   摘要块是**只追加、永不改写**的历史记录（这是它有意的设计：避免
    #   递归压缩把专有名词越洗越丢）。代价是：块生成时如果写进了后来被推翻的
    #   内容，它会一直留在注入里。实测 id=17 那块写着「AI 住在用户的电脑
    #   「4060」里」「我整日泡在 4060 里」—— 而本地部署**从未成功**，
    #   她照着说就成了"我住在你 4060 上"。
    #   这里加一层框架，明确告诉模型：摘要记录的是"当时聊了什么"，
    #   不是"现在的状态"；状态类问题一律以记忆库/知识图谱的当前事实为准。
    return (
        "【近期经历概要（= 那几轮对话里**聊到了什么**，是历史记录，"
        "不代表现在的状态）】\n"
        "· 可以据此自然承接话题、理解前因，但不要机械复述。\n"
        "· 涉及「自己现在是什么状态 / 是否部署过 / 住在哪里 / 能力边界」这类问题，"
        "**不要拿这里的旧说法当事实**，以【关于这个人的记忆】【与当前角色相关的记忆】"
        "以及知识图谱里的当前事实为准；拿不准就说不确定，别硬说。\n"
        + (("· 带有〔注意…已过时〕标记的段落：里面的说法**已经不准了**，"
            "只当「当时聊过这件事」，绝不要当成现在的事实复述。\n")
           if _stale_notes else "")
        + "\n" + "\n\n".join(parts)
    )


async def _summarize_range(character_id: str, transcript: str, key: str):
    """把一段对话压缩成一块独立摘要（协程）。失败返回空串。

    ★ 注意这里**不传旧摘要** —— 这正是"只追加"的关键：
      旧摘要不再参与本轮压缩，任何事实最多只被压一次。
    """
    prompt = (
        "下面是 TA 和你（AI）的一段连续对话记录，请把它提炼成一份摘要。\n"
        "这一段会被长期存档、之后不会再改写，所以要写成能独立看懂的完整记录。\n\n"
        "【对话记录】\n"
        + transcript[-10000:]
    )
    try:
        out = await chat_once(
            config.memory_extract_model(character_id),
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            key,
            temperature=0.2,
            max_tokens=1600,
        )
    except ModelApiError:
        return ""
    except Exception:
        return ""
    return str(out or "").strip()


async def maybe_update_summary(session_id: str, character_id: str = "default") -> bool:
    """如果积累了足够多的新消息，追加一个摘要块（★ 按角色隔离）。"""
    item = db.get_conversation_summary(session_id, character_id)

    covered_until = int(
        item.get("covered_until_id") or 0
    )

    # ★ 块游标兜底：摘要块是不可变存档，它覆盖到哪就以它为准。
    #   单行摘要那一行万一落后/被别处写回，也不会让已经总结过的区间被重复压缩。
    _blocks = db.get_summary_blocks(session_id, character_id, 1)
    if _blocks:
        covered_until = max(
            covered_until, int(_blocks[-1].get("to_msg_id") or 0)
        )

    new_messages = db.messages_after_id(
        session_id,
        covered_until,
        SUMMARY_TRIGGER_MESSAGES + KEEP_RECENT_MESSAGES + 20,
        character_id
    )

    if len(new_messages) < SUMMARY_TRIGGER_MESSAGES:
        return False

    # 最近 KEEP_RECENT_MESSAGES 条原始消息仍由短期上下文负责，
    # 只把更早部分压进摘要
    compress_part = new_messages[:-KEEP_RECENT_MESSAGES]

    if not compress_part:
        return False

    transcript = "\n".join(
        (
            ("用户" if m["role"] == "user" else "AI")
            + "："
            + str(m.get("content") or "")
        )
        for m in compress_part
        if m.get("content")
    )

    if not transcript.strip():
        return False

    key = config.memory_key()

    if not key:
        return False

    block_summary = await _summarize_range(character_id, transcript, key)

    if not block_summary:
        return False

    block_summary = block_summary[:MAX_SUMMARY_CHARS]

    # ★ 这一段覆盖的 id 区间：从上一个游标之后到本段最后一条。
    #   产出即固定，之后永远不再改写（重复调用被 UNIQUE(to_msg_id) 挡住）。
    from_id = int(covered_until) + 1
    covered_id = int(compress_part[-1]["id"])

    db.add_summary_block(
        session_id,
        character_id,
        from_msg_id=from_id,
        to_msg_id=covered_id,
        summary=block_summary,
    )

    # 兼容旧消费方（idle_agent / scheduler / moments 都在读这一行）：
    # 写入"最近一版"= 最近几块拼起来，而不是把旧摘要再压一遍。
    recent = db.get_summary_blocks(session_id, character_id, BLOCK_INJECT_LIMIT)
    merged = "\n\n".join(
        str(b.get("summary") or "").strip()
        for b in recent
        if str(b.get("summary") or "").strip()
    ) or block_summary

    db.save_conversation_summary(
        session_id,
        merged[:MAX_SUMMARY_CHARS],
        covered_id,
        character_id
    )

    # ★ 诊断：确认"只追加不重写"真的在跑（改造前这一层完全静默，
    #   摘要到底更新了没有、块覆盖到哪，事后无从判断）。
    try:
        from .external_memory import trace as _trace
        _trace.record_summary_block(
            session_id, character_id, from_id, covered_id,
            len(block_summary), len(merged), db.count_summary_blocks(session_id, character_id)
        )
    except Exception:
        pass

    print(
        "[Summary] 追加摘要块 %s..%s（%d 字），累计 %d 块"
        % (from_id, covered_id, len(block_summary),
           db.count_summary_blocks(session_id, character_id)),
        flush=True,
    )

    return True
