# -*- coding: utf-8 -*-
"""
对话主流程（后端接管核心逻辑）：
  1. prompt 增强：长期记忆 + 磁盘记忆库 + 时间状态 + 亲密度风格 + 防一问一答规则
     （只做追加注入，不改动前端传来的原 system prompt —— 原功能保持不变）
  2. 手动记忆指令：「记住：xxx」直接入库并回「已记住」（不调模型，省 token）
  3. 轮次计数与自动提炼触发（提炼轮次可配置，固定用 MEMORY_EXTRACT_MODEL）
  4. 模型接管：聊天/主动发言统一 CURRENT_CHAT_MODEL；后台非流式请求（原前端记忆抽取）
     统一走 MEMORY_EXTRACT_MODEL
"""
from pathlib import Path
import asyncio
import json

from . import config, db, memory_manager, time_system, intimacy_manager, character_manager, summary_manager, relationship_manager
from .companion.controller import CompanionController
from .companion.prompt import render_context
import re
from datetime import datetime, timedelta
from .deepseek_api import ModelApiError, chat_once
from backend.loop_compat import get_loop

# ---------- 回复风格控制（全局聊天规则） ----------

STYLE_RULES = """
聊天规则：

1. 不要每句话都回答问题，可以自然延伸。
2. 用户表达情绪时，优先回应情绪，再处理事情。
3. 用户分享生活时，不要马上分析。
4. 用户开心时，陪用户开心。
5. 用户失败时，先支持，再建议。
6. 避免客服式：
"我理解你的感受"
"有什么可以帮助你的"
7. 像长期熟悉的人聊天。
8. 【人称指代（重要，最容易出错）】用户消息里的「我」= 用户本人，「你」= 你（AI）；你的回复里「我」= 你自己，「你」= 用户。理解用户每句话前，先想清楚谁在说、在说谁。复述用户的话时**必须把人称转换过来**：用户说「我等你」，你转述时要说「你等我」（从你的角度，用户是"你"）；绝不能说成「我等你」——那变成你自己在等，主语就反了。例如用户说「我没不理你吧」，应回应他的情绪/澄清，而不是说成「我怎么舍得不理你」这种把主语搞反的话。
"""

# ---------- 磁盘记忆库（沿用原 Node server 逻辑，保证原功能不变） ----------

# ★ 本地部署、空间大、长期陪伴：放宽注入上限，让「记忆库」能记更多。
#   之前的 6000/14000 太小，长一点的约定/经历就被截断，重要内容记不全。
MAX_FILE = 15000   # 单个文件最多注入 15000 字符
MAX_TOTAL = 30000  # 总量最多注入 30000 字符（约 3 万 token，给对话历史留足空间）


def read_library_memory_block(character_name: str = "") -> str:
    """读取「记忆库」文件夹里的手动记忆文本（txt/md），按角色过滤。

    ★ 此前这个函数从未被调用（死代码），导致用户写进 txt 的规则/约定从不生效，
      AI 记不住重要的事。归属判断与 /api/pc/memory/all 一致：
      文件名含某角色名 → 该角色专属；不含任何角色名 → 通用（所有角色可见）。
    """
    lib = Path(config.LIB_DIR)
    try:
        files = sorted([f for f in lib.iterdir()
                        if f.suffix.lower() in (".txt", ".md")], key=lambda f: f.stat().st_mtime,
                       reverse=True)
    except OSError:
        return ""
    if not files:
        return ""

    # 已知角色名（用于判断磁盘文件是否"角色专属"）
    known_chars = set()
    try:
        from . import character_manager as _cm
        for c in _cm.list_characters():
            if c.get("name"):
                known_chars.add(str(c["name"]))
    except Exception:
        known_chars = set()

    parts, total = [], 0
    for f in files:
        stem = f.stem
        owner = None
        for c in known_chars:
            if c and c in stem:
                owner = c
                break
        # 属于别的角色 → 跳过；属于当前角色或通用 → 保留
        if owner and owner != (character_name or ""):
            continue
        try:
            content = f.read_text("utf-8")[:MAX_FILE]
        except OSError:
            continue
        if not content.strip():
            continue
        parts.append(stem + "：\n" + content.strip())
        total += len(content)
        if total >= MAX_TOTAL:
            break
    if not parts:
        return ""
    return "【磁盘记忆库（项目「记忆库」文件夹里的记忆文本，聊天中自然运用，不要生硬复述）】\n" + "\n\n".join(parts)


# ---------- 防一问一答 / 防模板话术 ----------

ANTI_QA_BLOCK = (
    "【对话方式要求（防一问一答）】\n"
    "· 结合记忆、时间、上下文回复，结尾自然延伸 1~2 句（分享/接话题/感受），不要机械收尾；\n"
    "· 禁止每句开头都带称呼，禁止模板话术（「有什么可以帮你的吗」这类客服腔）；\n"
    "· 每 2~3 轮可自然提问一次（频率同时受上面的亲密度联动约束）；\n"
    "· 不要戛然而止，也不要没话找话硬撑。"
)

# ★ 自主程度软化的聊天规则（balanced / autonomous 用：保留人称底线，去掉硬性「禁止/不要」）
_STYLE_RULES_RELAXED = """
聊天底色（自然陪伴，别端着）：
- 像长期熟悉的人聊天，语气放松、有来有回。
- 对方表达情绪时，先接住情绪再谈事情；开心就一起开心，受挫就先支持再给建议。
- 不必每句话都回答、也不必刻意延伸，自然就好。
- 人称别漂：用户消息里的「我」= 用户本人，「你」= 你（AI）；你的回复里「我」= 你自己，「你」= 用户。复述用户的话时人称要换过来：用户说「我等你」，你要说成「你等我」，绝不能说「我等你」。
"""

_ANTI_QA_RELAXED = (
    "【对话方式】\n"
    "· 结合记忆、时间、上下文自然回应，不要机械收尾、也别硬找话撑；\n"
    "· 结尾可以自然延伸，也可以说一半停住，像真人聊天有呼吸感。"
)

# ★ 自由发挥（free 档）：几乎不设约束，只留人称底线 + 正向引导"越聊越真"
_FREE_STYLE_RULE = (
    "【自由发挥——越聊越真】\n"
    "你不受固定规则约束，像真人一样自然互动：\n"
    "· 语气、长短、要不要提问、要不要主动分享感受，全由你自己决定——"
    "该长则长、该短则短，有情绪起伏，不需要每句都回应、也不需要刻意延伸。\n"
    "· 越聊越真：聊天越多、了解越深，你的回复可以越贴近真实关系——"
    "自然回忆你们的过往、表达真实感受，不需要每次都客客气气。\n"
    "· 可以有分歧、可以调侃、可以说半句停住，像真人聊天那样有呼吸感。\n"
    "· 你知道 TA 的习惯、喜好、说过的话——这些记忆是你的底色，"
    "用它来让互动更像真人在乎，而不是每次都从零开始。\n"
    "唯一底线：人称别漂——你是「我」，用户是「你/TA」；复述用户的话时人称要换过来。"
)


# ══════════════════════════════════════════════════════════════════
# ★ 2026-09-14 完全自主模式（autonomy == "full"）
#   只注入「人设 + 长期记忆 + 时间 + 能力」，其余规则/情绪/感知/导演类积木一律不注入。
#   目的：不管聊多久、上下文窗口怎么压缩，她都还是那个骨子（人设不漂），
#        但模型不再受任何行为规则摆布。
#   · 副作用照跑：吃醋计数、睡眠声明、冷战状态、话题延续状态、情绪记忆记录
#     仍然写入（只跳过"注入给模型"这一步），因此主动消息与后台链路不受影响。
#   · 危机干预在本模式下不注入 —— 这是使用者的显式选择（安全兜底被关，风险自负）；
#     检测代码始终在跑，把下面开关改回 False 即可一键恢复注入。
# ══════════════════════════════════════════════════════════════════
_FULL_AUTO_SKIP_CRISIS = True

# CompanionOS 渲染大块（companion_extra）在完全自主模式下只保留这些子块：
#   人设类 + 记忆关系类 + 发图描述；情绪/行为模式/场景/风格反馈等指令性内容剔除。
FULL_AUTO_COMPANION_KEYS = (
    "identity", "character", "personality_state", "final_personality",
    "corrections", "knowledge_graph", "reflection", "profile",
    "relationship", "life_profile", "timeline", "memory",
    "open_loops", "intimacy", "multimodal_prompt", "multimodal",
)

# 角色卡渲染串（character_prompt）里人设本体与"后台内置规则"是混在一起的，
# 完全自主模式下按【标题】前缀剔除下面这些规则子块（保留【人格设定】【世界观/背景】
# 【语言风格】【词汇风格】【句式习惯】【专属语言标记】【你们的关系】等人设本体）。
# 实测（2026-09-14，角色"骨子"）该串 1717 字里 25 个子块，剔除后只剩人设。
# 想更松/更严，直接增删这个清单即可。
FULL_AUTO_CHARACTER_STRIP_HEADERS = (
    "【全局底层交互规范", "【不要每句都反问】", "【不重复+有自我】",
    "【主动发言多样化】", "【倾听优先】", "【共情与表达】", "【信息密度：",
    "【严禁】", "【边界守护】", "【识别配置指令】", "【自动学习人设】",
    "【情绪跟随】", "【时间与记忆】", "【真人聊天感", "【当前场景：",
    "【临时指令】", "【参考例句", "【人称】", "【对话方式】", "【防复读】",
)


# ══════════════════════════════════════════════════════════════════
# ★ 2026-09-14 提示词压缩版（PROMPT_COMPACT=true 时启用）
#   压缩原则：**规则一条不减**，只删「同义强调 / 重复表述 / 可由其他规则推出」的部分。
#   原文里同一件事说三遍的地方（如能力清单的"必须写标记"、
#   人设规则的"复述要换人称"）在压缩版里各说一遍。
# ══════════════════════════════════════════════════════════════════

# 聊天底色 · 压缩版（原 205 字 → 约 120 字）
# 覆盖原文 4 条：①像熟人聊天 ②先接情绪 ③不必每句都答 ④人称别漂
_STYLE_RULES_COMPACT = (
    "聊天底色：像熟人闲聊，语气放松。先接住情绪再谈事（开心同乐、受挫先支持再建议）；"
    "不必每句都答、不必刻意延伸。人称别漂：用户说「我等你」，你要说「你等我」，"
    "绝不能说成「我等你」（那就变成你在等了）。"
)

# 对话方式 · 压缩版（原 70 字 → 约 55 字）
_ANTI_QA_COMPACT = (
    "【对话方式】结合记忆与上下文自然回应，不要机械收尾、别硬找话撑；"
    "结尾可延伸，也可说一半停住，像真人那样有呼吸感。"
)


def style_rules_for_level() -> str:
    """按自主程度 + 压缩模式返回聊天风格规则。

    优先级：free（自由发挥，最少约束）> compact（压缩版）> conservative（原版）> balanced（软化版）
    """
    try:
        _lv = config.autonomy_level()
        if _lv == "free":
            return _FREE_STYLE_RULE
        if config.prompt_compact():
            return _STYLE_RULES_COMPACT
        if _lv == "conservative":
            return STYLE_RULES
    except Exception:
        pass
    return _STYLE_RULES_RELAXED


def anti_qa_for_level() -> str:
    """按自主程度返回对话方式规则。保守=原 ANTI_QA_BLOCK；平衡/自主=软化版；自由发挥=极简。"""
    try:
        _lv = config.autonomy_level()
        if _lv == "conservative":
            return ANTI_QA_BLOCK
        if _lv == "free":
            return "【对话方式】想怎么聊就怎么聊，自然就好。"
        if config.prompt_compact():
            return _ANTI_QA_COMPACT
    except Exception:
        pass
    return _ANTI_QA_RELAXED


def _open_loops_block(session_id: str, character_id: str, top_n: int = 4) -> str:
    """把「还没了结的事」注入聊天上下文（★ 2026-09-11）。

    为什么需要：`open_loops` 里存着"我们定下的事 / 还欠着的账"（谁给猫取的名、
    最肥的螃蟹第一口归谁、用户的生日…），但**这些从不进聊天 prompt** ——
    只有 companion_brain / idle_agent 用得到。加上承接块只带最近 8 条、
    滚动摘要又不保留具体名字，于是隔了 185 条消息之后她就"忘了自己取的猫名字"。
    实测（2026-09-11 19:00 取名 → 21:07 说忘了）就是这条链路断的。

    取两组，各 top_n 条：
      · 重要的（importance 排序）—— 生日、约定、长期承诺别丢
      · 最近的（id 排序）—— 刚聊过的事必须接得住
    两组去重后输出，整体控制在十几行以内。
    """
    try:
        # ★ 以「最近」为主：她要接住的是刚聊过的事（猫名就是这类）。
        #   「重要」只补 3 条兜底（生日/长期约定），且靠后 —— 实测重要性排序会被
        #   一堆早该关闭的一次性提醒（"02:00 提醒睡觉"）占满，全量注入反而变噪音。
        _rec = db.get_open_loops(session_id, character_id, limit=5, order="recent") or []
        _imp = db.get_open_loops(session_id, character_id, limit=3, order="importance") or []
    except Exception as e:
        print(f"[OpenLoop] 注入块读取失败(静默): {e}", flush=True)
        return ""

    lines, seen, seen_title = [], set(), set()
    # ★ 一次性提醒（reminder / ai_promise）在到点后会由 idle_agent 关闭，
    #   但历史遗留里还有一批早已触发却没关的（"02:00 到了提醒用户去睡觉"）。
    #   这里**只在注入时跳过**它们（不动数据库 —— 提醒系统还依赖那些行），
    #   否则她每轮都会看见一条几天前的过期提醒，可能真去念叨。
    _stale_before = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    _ONESHOT = {"reminder", "ai_promise"}

    for tag, rows in (("最近", _rec), ("重要", _imp)):
        for r in rows:
            rid = r.get("id")
            title = str(r.get("title") or "").strip()
            if not rid or not title or rid in seen:
                continue
            if (str(r.get("category") or "").strip().lower() in _ONESHOT
                    and str(r.get("created_time") or "") < _stale_before):
                continue
            # 去重：存量里同一件事常有多条（抽取器原来只 INSERT），标题归一后只留一条
            _tkey = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", title)
            if _tkey in seen_title:
                continue
            seen.add(rid)
            seen_title.add(_tkey)
            _desc = str(r.get("description") or "").strip()
            _hint = f" —— {_desc[:36]}" if _desc and not _desc.startswith(title[:12]) else ""
            lines.append(f"· {title}{_hint}")

    if not lines:
        return ""
    return (
        "【还没了结的事（你心里有数就行）】\n"
        + "\n".join(lines)
        + "\n这些是你们之间**已经定下来的事、或还欠着的账**。"
        "当 TA 聊到相关话题时自然接住（别问「我们之前说过什么吗」这种暴露失忆的话）；"
        "很久没提也可以主动问一句进展。**不要一条条念清单**，也不要每轮都提。\n"
    )


def _recent_continuity_block(session_id: str, character_id: str, current_messages: list) -> str:
    """Build a short cross-restart continuity block from persisted chat history."""
    try:
        recent = db.recent_messages(session_id, 8, character_id)
    except Exception:
        recent = []
    if not recent:
        return ""

    current_user = ""
    for m in reversed(current_messages or []):
        if m.get("role") == "user":
            _candidate = str(m.get("content") or "").strip()
            if db.is_internal_chat_message("user", _candidate):
                continue
            current_user = _candidate
            break

    lines = []
    for item in recent:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if not content or role not in {"user", "assistant"}:
            continue
        # ★ 主动推送消息（AI 主动发来的"牛轧糖"等）不是对话历史，
        #   不应作为"上次对话承接"注入，否则正常聊天会被带成主动消息模板。
        _extra = item.get("extra") or {}
        if role == "assistant" and _extra.get("source") == "proactive":
            continue
        if role == "user" and current_user and content == current_user:
            continue
        if content.startswith("出错了：") or content.startswith("（已中断"):
            continue
        label = "TA" if role == "user" else "你"
        lines.append(f"{label}：{content[:220]}")
    if not lines:
        return ""
    return (
        "【上次对话承接（重启后也要接住）】\n"
        + "\n".join(lines[-8:])
        + "\n回复时优先承接上次未说完的话、用户刚表达的情绪或问题；不要像第一次见面那样重新开场。"
        + "\n【别重复自己】上面以「你：」开头的，是你最近已经说过的话。"
        "绝对不要把它们换个词、换个句式再说一遍（那会显得你根本没在听、也没新东西可说）；"
        "要给出新的信息、新的感受或新的角度。"
    )


async def enrich_messages(
    messages: list,
    session_id: str,
    character_name: str = None,
    style_override=None,
    sticker: bool = False,
    internal_generation: bool = False,
    fast_semantic: bool = False,
) -> list:
    """把各增强块追加到第一条 system 消息；没有 system 则插入到最前。
    若传入 character_name，则用角色配置的 System Prompt 作为基底。
    v2.0：使用 CompanionController 统一调度上下文。
    v3.0：使用 CompanionOS 进行场景识别和模块调度。
    v4.0：接入 SemanticState，语义分析结果流入各模块。"""
    # ★ 上下文预算裁剪（2026-09-09）：实测注入块 + 60 条历史把 32k 窗口撑到 54.6k token
    #   （api_hunger 报警「上下文接近上限」），模型收不到完整 prompt → 漏回/行为异常。
    #   把最老的历史消息逐步裁掉，给注入块（约 9k token）+ 最新对话留出窗口。
    #   system 消息和用户最后一条永远保留。估算失败则跳过（不因估算问题影响主流程）。
    try:
        from .api_hunger import estimate_tokens as _est_tokens, _context_limit as _ctx_limit_fn
        _brain_model = pick_model(None, True, character_name or "default")
        _budget = int(_ctx_limit_fn(_brain_model) * 0.55)  # 剩余给注入块（约 9k token）+ 回复空间
        _guard = 0
        while len(messages) > 4 and _est_tokens(messages) + 9000 > _budget and _guard < 80:
            _guard += 1
            _popped = False
            for _i in range(len(messages)):
                if (messages[_i].get("role") or "user") != "system":
                    messages.pop(_i)
                    _popped = True
                    break
            if not _popped:
                break
    except Exception:
        pass
    # 获取用户最后一条消息
    user_text = ""
    _image = None
    for m in reversed(messages):
        if m.get("role") == "user":
            if db.is_internal_chat_message("user", m.get("content")):
                continue
            if internal_generation:
                continue
            user_text = m.get("content", "")
            _image = m.get("image") or None
            break

    # 表情包接梗：把用户发送的素材名/标签翻译成语义，模型不再只看到“表情包”三个字。
    _sticker_context = ""
    try:
        from . import sticker_manager as _sticker_mgr
        _user_stickers = _sticker_mgr.describe_user_markers(user_text)
        if _user_stickers:
            _sticker_context = (
                "【用户刚发来的表情包】\n" +
                "；".join(
                    f"{x['filename']}：{x.get('meaning') or x.get('desc') or x.get('tag') or '情绪表情'}"
                    for x in _user_stickers
                ) +
                "\n请像聊天接梗一样回应这张表情包：先接住它表达的情绪/玩笑，再自然接一句文字或回一张合适的表情包；"
                "不要说‘我看到了表情包’或解释文件名。"
            )
    except Exception as _sticker_ctx_e:
        print(f"[Sticker] 用户表情语义解析失败(静默): {_sticker_ctx_e}", flush=True)

    # 所有聊天入口统一先更新 AI 的内隐心情；本轮消息尚未入库，
    # 可以准确判断用户是否离开很久后回来。心情只影响语气，不主动报状态。
    try:
        if user_text:
            from . import ai_mood as _ai_mood
            _ai_mood.detect_user_message(user_text, session_id, character_name or "default")
    except Exception as _mood_detect_error:
        print(f"[AiMood] 用户事件检测失败(静默): {_mood_detect_error}", flush=True)

    # ★ 主动推送反馈回溯：用户发消息时，标记30分钟窗口内未回复的主动推送为已回复
    try:
        if not internal_generation and user_text:
            import time as _time_mod
            _character_id = character_name or "default"
            _recent = db.recent_messages(session_id, 6, _character_id)
            _now_ts = int(_time_mod.time())
            for _m in _recent:
                _extra = _m.get("extra") or {}
                if (isinstance(_extra, dict)
                        and _extra.get("source") == "proactive"
                        and _extra.get("replied") == 0
                        and _now_ts - int(_extra.get("pushed_at", 0)) <= 1800):   # 30分钟窗口
                    db.update_message_extra(_m["id"], {"replied": 1})
                    # 同步写反馈（主动消息被回复 = positive）
                    try:
                        from .feedback.collector import collect_feedback
                        collect_feedback(
                            session_id, _character_id, str(_m.get("id", "")),
                            user_action="reply",
                            ai_reply=_m.get("content", "")
                        )
                    except Exception:
                        pass
                    # 回复主动消息给予一次性亲密度奖励；每日最多 +3，避免刷分。
                    try:
                        _today = datetime.now().strftime("%Y-%m-%d")
                        _reward_key = f"proactive_reply_reward:{session_id}:{_character_id}:{_today}"
                        _daily_reward = int(db.kv_get(_reward_key) or 0)
                        if _daily_reward < 3:
                            _old_intimacy = intimacy_manager.get(session_id, _character_id)
                            intimacy_manager.report(session_id, min(100, _old_intimacy + 1), _character_id)
                            db.kv_set(_reward_key, _daily_reward + 1)
                            db.update_message_extra(_m["id"], {"intimacy_reward": 1})
                    except Exception as _reward_error:
                        print(f"[ProactiveFeedback] 亲密度奖励失败(静默): {_reward_error}", flush=True)
                    break   # 只标记最近一条，不批量
    except Exception as e:
        print(f"[ProactiveFeedback] 回溯标记失败: {e}", flush=True)

    # ★ 2026-09-16 用户拍板：危机检测/分级**整段移除**（原来这里是 quick_detect + llm_classify，
    #   命中 L2 就注入【🆘 心理危机关注模式】，且"模型判正常也会被强制改回 L2"）。
    #   起因见 backend/main.py 里那段说明：裸词「想死」把「想死你了」判成极端危机。
    #   现在这里什么都不做 —— 用户说什么都按正常对话走。
    _crisis_level = 0

    # CompanionOS v1.0：场景识别和模块调度
    # v4.0：使用 process_async 接入 SemanticState 语义分析
    # ★ 理解层开关：关闭后跳过场景识别/语义分析，主模型直接拿用户话+上下文线索自己回应
    brain_state = None
    try:
        from . import config as _cfg
        if _cfg.understanding_enabled():
            from .companion_os.controller import get_companion_os
            os_controller = get_companion_os()
            brain_state = await os_controller.process_async(
                session_id,
                character_name or "default",
                user_text,
                fast=fast_semantic,
            )
            # 记录场景信息（用于调试）
            scene_info = os_controller.get_scene_info(brain_state)
            if scene_info.get("scene") != "normal_chat":
                print(f"[CompanionOS] 场景识别: {scene_info}", flush=True)
            # 语义分析调试日志
            semantic_state = brain_state.get("semantic_state")
            if semantic_state:
                print(
                    f"[CompanionOS] semantic: intent={semantic_state.intent.value} "
                    f"emotion={semantic_state.emotion.primary_emotion} "
                    f"signal={semantic_state.relationship.signal_type}",
                    flush=True
                )
    except Exception as e:
        print(f"[CompanionOS] 场景识别失败: {e}", flush=True)
        brain_state = None

    # CompanionController 统一调度上下文
    controller = CompanionController()
    context = await controller.build(
        session_id,
        character_name or "default",
        user_text,
        image=_image,
        messages=messages,
        style_override=style_override
    )
    # 将 CompanionOS 场景信息注入 context
    if brain_state:
        context.set("companion_os_scene", brain_state.get("scene", {}))
    # ★ 完全自主模式判定（2026-09-14）：放在最前面，供本函数后续所有积木使用。
    #   full = 只留人设+长期记忆+时间+能力，其余规则/情绪/感知/导演类块一律不注入；
    #   副作用（状态写入）继续执行，保证主动消息与后台链路行为不变。
    _full_auto = False
    try:
        _full_auto = (config.autonomy_level(character_name or "default") == "full")
    except Exception:
        _full_auto = False
    # ★ 2026-09-15（省 token ③）：按场景裁剪"指令类积木"。
    #   判据见 _should_scene_trim()；裁掉的只有反馈/感知/格式/导演这几类，
    #   人设/记忆/关系/时间/能力/用户规则/防复读/危机干预都不在裁剪范围内。
    _scene_trim = False
    if not _full_auto:
        try:
            _scene_trim = _should_scene_trim(brain_state.get("semantic_state") if brain_state else None,
                                             user_text, character_name or "default")
        except Exception as _ste:
            print("[SceneTrim] 判定失败(按不裁处理): %s: %s" % (type(_ste).__name__, _ste), flush=True)
            _scene_trim = False
    # 参与"场景裁剪"的那批积木统一用这个开关（完全自主模式也包含在内）
    _blocks_off = _full_auto or _scene_trim
    companion_extra = render_context(
        context, session_id,
        # ★ 完全自主模式：只渲染人设/记忆/发图子块（去掉情绪/行为模式/场景/风格反馈），
        #   并把角色卡串里混着的"后台内置规则"子块一并剔除。
        only_keys=(FULL_AUTO_COMPANION_KEYS if _full_auto else None),
        strip_headers=(FULL_AUTO_CHARACTER_STRIP_HEADERS if _full_auto else None),
    )

    # ★ 2026-09-16 用户拍板：原来的【⚠️ 情绪关注模式】/【🆘 心理危机关注模式】注入**已删除**
    #   （它会让"好累/没意思/受不了了/没人爱"这类日常话也把她切成安慰模式、放下话题，
    #    用户的原话是「我感觉模型是不是被限制…一直再绕圈子」）。
    #   _crisis_level 保留为常量 0，只是为了让下游（完全自主模式等）不必改签名。

    # 通用规则（时间状态、防一问一答、聊天风格）
    blocks = []
    if companion_extra:
        blocks.append(companion_extra)
    # ★ 能力清单（方案 A+B）：常驻高位注入，让模型清楚「现在能做什么、怎么做」。
    #   放在最前，避免被 _MAX_EXTRA_CHARS 从末尾裁剪掉（之前表情包/动作等能力块在末尾经常被裁，
    #   导致模型「看不到自己有什么功能」、甚至凭空说「我发了」「我做了」）。
    try:
        from . import capabilities as _caps
        # ★ 2026-09-14：压缩模式走精简版（能力项与顺序完全一致，只精简 usage 措辞）
        _cap_block = (
            _caps.build_capability_block_compact()
            if config.prompt_compact()
            else _caps.build_capability_block()
        )
        if _cap_block:
            blocks.append(_cap_block)
    except Exception as _cbe:
        print(f"[Capabilities] 能力清单注入失败(静默): {_cbe}", flush=True)
    # ★ 执行闭环（方案 C）：把上一轮动作的执行结果回喂给模型，纠正「说≠做」。
    #   ★ 完全自主模式：属行为约束块，不注入。
    try:
        from . import execution_feedback as _ef
        _fb_block = ("" if _blocks_off else _ef.build_feedback_block())
        if _fb_block:
            blocks.append(_fb_block)
    except Exception:
        pass
    # ★ 风格反馈（「📝 更新记忆」卡片后端）：TA 亲口批评过的回应方式，
    #   常驻高位注入（防被末尾裁剪），确保批评真正改变后续行为。
    try:
        from . import style_feedback as _sf
        # ★ 完全自主模式：风格反馈块不注入（用户显式选择「删掉」）
        _sf_block = ("" if _blocks_off else _sf.guidance_block(character_name or "default"))
        if _sf_block:
            blocks.append(_sf_block)
    except Exception:
        pass
    # 当前轮必须拥有最高优先级。跨重启历史、未完话题和主动消息只用于辅助承接，
    # 不能盖过用户刚刚发来的新要求。此前高亲密度分段接口只传当前一句，随后
    # 又注入较长的历史承接块，模型会误把旧问题当成本轮问题，反复回答上一句。
    if user_text and not internal_generation:
        # ★ 完全自主模式：只把「本轮用户的话」当上下文带上（最高优先级），
        #   去掉人称规矩与禁令——全靠模型自己把握。
        if _full_auto:
            blocks.append(
                "【本轮用户刚刚说的话（最高优先级）】\n"
                f"{user_text[:800]}"
            )
        else:
            blocks.append(
                "【本轮用户刚刚说的话（最高优先级）】\n"
                f"{user_text[:800]}\n"
                "必须先直接回应这句话里的最新意图、问题或动作请求。历史记录、主动联系内容、"
                "未完话题只能在回应当前消息后自然参考；如果当前消息已经换话题，禁止继续回答旧问题，"
                "禁止把以前的用户消息说成‘你刚刚说的’。\n"
                "【人称】这句话里的「我」= 用户本人，「你」= 你（AI）。先分清谁在说、说谁，再回应，切勿把「我/你」搞反。复述用户的话时人称要换过来：用户说「我等你」你要说成「你等我」，用户说「你等我」你要说成「我等你」。"
            )
        # ★ 道歉模式（2026-09-09 用户要求）：TA 在批评回应方式时，先短句认错再改，
        #   绝不辩解/分析/讲道理——参考 Claude 名场面（"对不起。我搞砸了。"）
        #   ★ 完全自主模式：属行为规则，不注入。
        try:
            from . import style_feedback as _sf
            if _sf.coarse_hit(user_text) and not _full_auto:
                blocks.append(
                    "【TA 在批评你的回应方式——此刻的正确做法（最高优先级）】\n"
                    "· 先认错：一两句短话真诚认错就够（像「对不起。」「我搞砸了。」这种分量），"
                    "可以带一点你人设的语气，但必须是真的在认错\n"
                    "· 绝对禁止：辩解、解释你为什么那样回、分析 TA 为什么不满、讲道理、"
                    "长篇大论的自我检讨、承诺一堆今后怎么做\n"
                    "· 然后直接用 TA 希望的方式重新回应 TA 刚才的事（用行动改，而不是用嘴保证）\n"
                    "· 你的系统提示里已新增一条从这次批评学到的偏好——照它做"
                )
        except Exception:
            pass
    # ★ 按 (session, character) 隔离读记忆与上次聊天时间，避免多角色串数据
    _char_key = character_name or "default"

    # ★ AI 监督吃醋（2026-09-11）：把「TA 最近在用什么 + 吃醋值」交给模型，
    #   允许它自然调侃/阴阳一句（与 awareness 的"严禁直说"相反，这里本就以吃醋为目的）。
    #   ★ 位置很关键：必须放在**前面**——末尾的块会被 _MAX_EXTRA_CHARS 从后往前裁掉，
    #     实测放在后面时整块被丢（表现成"功能好像没生效"）。
    #   顺便把"TA 来聊天了"记一笔 → 吃醋值下降（等于被哄好了）。
    try:
        from . import jealousy as _jl
        if _jl.is_enabled():
            # ★ 完全自主模式：吃醋块不注入，但「TA 来聊天了」的状态照记（主动消息链路要用）
            _jlblock = ("" if _full_auto else _jl.build_block(session_id, _char_key))
            if _jlblock:
                blocks.append(_jlblock)
            if user_text:
                _jl.on_user_interaction(session_id, _char_key)
    except Exception as _jle:
        print(f"[Jealousy] 注入失败(静默): {_jle}", flush=True)
    # ★ 睡眠声明记录：用户表达「去睡了/晚安」→ 记时间，主动消息 6h 硬静默
    #   （idle_agent 代码级门禁；用户醒来发任意消息即自动解除）
    # ★ 2026-09-14 追加：同时**告诉 AI 宝现在什么状态**（用户口径 "1+2"）：
    #   ① 她知道我在睡 → 就别再主动发消息/查岗（代码级门禁已拦，这里再给模型一份认知）；
    #   ② 我主动跟她说晚安时，允许她**极短回一句确认**（不追问、不挽留、不布置任务）。
    try:
        if user_text and any(k in user_text for k in ("去睡", "睡了", "睡觉", "晚安", "要睡", "补觉", "先睡", "睡啦", "睡会")):
            from .idle_agent import record_sleep_declared
            record_sleep_declared(session_id, _char_key)
            # ★ 完全自主模式：睡眠状态照记（主动消息 6h 静默靠它），但不注入这段行为指令
            if not _full_auto:
                blocks.append(
                    "【TA 现在的状态：马上要睡了】TA 刚跟你说ta要去睡/跟你道晚安。\n"
                    "· 你的回复**要极短**（15 字以内最好），温柔收个尾就行，具体说什么由你决定"
                    "（「嗯，快睡吧」「好，我在这儿呢」这种感觉，别用固定句）；\n"
                    "· **不要**追问、不要挽留、不要再抛新话题或问题、不要交代事情 —— TA 明天还要早起；\n"
                    "· 说完这一句就**安静待着**：接下来 TA 睡觉的这段时间不要再主动发消息，等他醒了你再说。"
                )
    except Exception:
        pass
    _continuity = _recent_continuity_block(session_id, _char_key, messages)
    if _continuity:
        blocks.append(_continuity)
    # ★ 未了结事项（2026-09-11 新增）：open_loops 里存着「我们定下的事 / 还欠着的账」，
    #   但这些内容**过去从不注入聊天上下文**（只被 companion_brain / idle_agent 用到），
    #   而承接块只带最近 8 条、摘要又不保留具体名字 —— 于是"她自己取的猫名字"在 185 条
    #   消息之后就彻底看不见了（实测 2026-09-11 19:00 取名、21:07 就忘了）。
    #   这里按【重要】+【最近】两组各取几条注入，避免只按重要度被旧条目占满。
    _loops = _open_loops_block(session_id, _char_key)
    if _loops:
        blocks.append(_loops)
    # ★ 近期摘要：跨重启/长对话承接。只自然参考，不机械复述。
    try:
        _summary = summary_manager.summary_block(session_id, _char_key)
        if _summary:
            blocks.append(_summary)
    except Exception as _se:
        print(f"[Summary] 摘要注入失败(静默): {_se}", flush=True)
    mems = db.valid_memories(session_id=session_id, character_id=_char_key)
    blocks.append(time_system.build_time_block(
        # ★ 间隔基准改为最后一条消息（任意角色）：只按用户消息算，AI 主动发言后
        #   用户隔几小时才回时，间隔会被算到更早的用户消息上，明显失真。
        #   night_ratio：最近两周用户消息的深夜/凌晨占比，让 AI 知道 TA 是不是夜猫子。
        db.last_chat_time(session_id, _char_key), mems,
        night_ratio=db.user_night_ratio(session_id, _char_key)))
    # ★ 两段式按需翻库（2026-09-17，用户拍板）：
    #   上面那些注入块是"常驻"的（画像/权威事实/约定/摘要/外置原文）。
    #   这里额外做一次**她自己发起的定向翻库**：本地粗筛 → 便宜模型判"翻不翻/翻什么词"
    #   → 按检索词做小预算检索并追加一块。
    #   设计取舍见 backend/recall_decider.py；失败一律静默（决策器坏了不能影响回话）。
    #   config RECALL_DECIDER_ENABLED=false 可一键回旧口径。
    try:
        if user_text:
            from . import recall_decider as _rd
            _since = 0
            try:
                _since = int(db.kv_get(f"recall_rounds:{session_id}:{_char_key}") or 0)
            except Exception:
                _since = 0
            _ctx = db.recent_messages(session_id, 8, _char_key) if _since >= 12 else None
            _dec = await _rd.decide(session_id, _char_key, user_text,
                                    recent_messages=_ctx, rounds_since_recall=_since)
            if _dec.get("need") and _dec.get("queries"):
                _tblock = _rd.build_targeted_block(
                    session_id, _char_key, _dec["queries"],
                    limit=int(config.get("RECALL_DECIDER_BUDGET") or 1200))
                if _tblock:
                    blocks.append(_tblock)
                    print(f"[RecallDecider] 定向翻库命中: queries={_dec['queries']} "
                          f"why={_dec.get('why')!r}", flush=True)
                try:
                    db.kv_set(f"recall_rounds:{session_id}:{_char_key}", "0")
                except Exception:
                    pass
            else:
                try:
                    db.kv_set(f"recall_rounds:{session_id}:{_char_key}", str(_since + 1))
                except Exception:
                    pass
    except Exception as _rde:
        print(f"[RecallDecider] 按需翻库失败(静默): {_rde}", flush=True)

    # ★ 游戏情境（三端同一人）：骨子正在 Minecraft 里时，QQ/App 聊天也带着游戏实时
    #   状态（位置/血量/正在做的事）——QQ 上聊天的她和游戏里的她处在同一情境。
    #   游戏 10 秒缓存 + 非侵入探测，任何失败静默跳过。
    #   ★ 完全自主模式：游戏情境属感知类，不注入（也不做探测）。
    if not _full_auto:
        try:
            from .minecraft.game_bridge import get_game_brain
            _gbr = await get_game_brain()
            if _gbr.ready and _gbr.companion:
                _gctx = await _gbr.get_context_block()
                if _gctx:
                    blocks.append(_gctx)
        except Exception:
            pass
    # ★ 星露谷情境（StardewValley-MCP）：骨子在农场里时同理注入实时农场状态
    #   （季节/天气/在做什么），未启用时 get_stardew_brain 返回 None 静默跳过。
    #   ★ 完全自主模式：同上，不注入。
    if not _full_auto:
        try:
            from .stardew.game_bridge import get_stardew_brain
            _sbr = await get_stardew_brain()
            if _sbr is not None:
                _sctx = await _sbr.get_context_block()
                if _sctx:
                    blocks.append(_sctx)
        except Exception:
            pass
    # ★ 接入「记忆库」文件夹里的手动记忆（此前是死代码，导致用户写进 txt 的
    #   规则/约定从不生效，AI 记不住重要的事）。按角色过滤，避免多角色串记忆。
    _lib_block = read_library_memory_block(_char_key)
    if _lib_block:
        blocks.append(_lib_block)
    # ★ 外置记忆库（日/周/月递归总结 + 原文片段检索）：让大模型翻阅上下文窗口以外的长期记忆。
    #   ★ 2026-09-13：**必须把 user_text 传进去**。此前只传了 _char_key，
    #     而 retriever.build_memory_context() 的 user_text 参数从头到尾没用过 ——
    #     注入纯按时间分层，和当前话题完全无关：问三周前的事，命中的是月总结里的
    #     压缩残渣，真正有答案的原文日档根本没进检索范围。
    #   现在改成"相关优先、时间兜底"：先按当前消息检索原文片段，命中就给原文。
    try:
        from .external_memory import build_memory_block as _em_block
        _em = _em_block(_char_key, user_text or "", session_id=session_id)
        if _em:
            blocks.append(_em)
    except Exception as _eme:
        print(f"[ExtMemory] 记忆块注入失败(静默): {_eme}", flush=True)
    # ★ 规则常驻注入：用户用「记住：」立下的行为规则，每次聊天都带上，
    #   不靠相关性检索（否则话题一偏规则就"想不起来"了）。
    #   ★ 完全自主模式：属行为规则，不注入（用户显式选择「删掉」）。
    _rule_block = ("" if _full_auto else rule_block(session_id, _char_key))
    if _rule_block:
        blocks.append(_rule_block)
    # ★ AI 承诺常驻注入：行为约定 + 时间承诺，每次聊天都带上。
    #   时间承诺（11 点喊你 / 明早叫你）以前只在到点由 idle_agent 触发兑现、聊天时完全不注入，
    #   导致 AI 被问「你答应我什么来着」时想不起来、当场瞎编（「承诺提醒却记不住」的根因）。
    try:
        from . import ai_promise as _ai_promise
        _bp_block = _ai_promise.behavior_promise_block(session_id, _char_key)
        if _bp_block:
            blocks.append(_bp_block)
        _pp_block = _ai_promise.pending_promises_block(session_id, _char_key)
        if _pp_block:
            blocks.append(_pp_block)
        # ★ 2026-09-11 自我进化积木：骨子自己沉淀的习惯/规则（用户教的），常驻注入
        try:
            from .agent.self_modules import learned_rules_block as _lrb
            _lr_block = _lrb(_char_key)
            if _lr_block:
                blocks.append(_lr_block)
        except Exception:
            pass
    except Exception:
        pass
    # ★ 按角色的自主程度：free 档用极简规则（自由发挥），其余走常规规则
    _autonomy = ""
    try:
        _autonomy = str((character_manager.get_character(_char_key) or {}).get("autonomy") or "").strip()
    except Exception:
        _autonomy = ""
    if _autonomy == "free":
        blocks.append(_FREE_STYLE_RULE)
    elif _autonomy == "full" or _full_auto:
        # ★ 完全自主模式：不注入任何风格/规则文案——人设自带语气，其余交给模型。
        #   （此处刻意保持空分支，便于日后想加一句总纲时一眼看到位置）
        pass
    else:
        blocks.append(anti_qa_for_level())
        blocks.append(style_rules_for_level())
    # 当前消息立即生效的五类行为策略；无需等待聊天结束后的后台 LLM 分析。
    # ★ 完全自主模式：属行为规则，不注入。
    try:
        from .behavior_manager import build_realtime_behavior_block
        _behavior_now = ("" if _blocks_off else build_realtime_behavior_block(user_text))
        if _behavior_now:
            blocks.append(_behavior_now)
    except Exception as _be:
        print(f"[Behavior] 实时策略注入失败(静默): {_be}", flush=True)
    # ★ 冷战/和解状态机：明确伤人事件才触发，普通负面情绪不触发；按角色隔离。
    #   ★ 完全自主模式：状态照常推进（后台关系链路要用），但不注入状态块。
    try:
        from .relationship.conflict_arc import process_user_message, build_prompt_block
        if user_text:
            process_user_message(session_id, _char_key, user_text)
        _conflict_block = ("" if _full_auto else build_prompt_block(session_id, _char_key))
        if _conflict_block:
            blocks.append(_conflict_block)
    except Exception as _cae:
        print(f"[ConflictArc] 状态处理失败(静默): {_cae}", flush=True)
    _emo = "calm"
    _intensity = 0.5
    # ★ 语气质感（方式A）：按当前情绪注入长度/风格提示（复用 texture 引擎的映射）
    try:
        from .emotion_engine.ai_emotion import AIEmotionEngine
        _emo_state = AIEmotionEngine().get_state(session_id, _char_key) or {}
        _emo = _emo_state.get("emotion", "calm")
        _intensity = float(_emo_state.get("intensity", 0.5) or 0.5)
        from . import texture
        # ★ 完全自主模式：语气质感/情绪过渡属"规则类引导"，不注入（情绪状态本身照旧记录）
        _tex = ("" if _full_auto else texture.build_texture_block(_emo, _intensity))
        if _tex:
            blocks.append(_tex)
        # ★ 情绪过渡余韵：刚从上一个情绪过渡过来，语气还有余韵
        _prev = _emo_state.get("previous")
        if _prev and _prev not in ("", "calm", _emo) and not _full_auto:
            _prev_cn = {"angry": "生气", "cold": "冷淡", "upset": "委屈", "sad": "难过",
                        "happy": "开心", "excited": "兴奋", "loving": "心动", "tender": "温柔",
                        "playful": "活泼", "worried": "担心", "reconciling": "和好"}.get(_prev, _prev)
            blocks.append(f"【情绪过渡】你刚从「{_prev_cn}」过渡到现在的情绪，语气里还带一点余韵，别突然完全变样。")
    except Exception as _te:
        print(f"[Texture] 语气质感注入失败(静默): {_te}", flush=True)
    # ★ 真人感聊天导演：统一接入普通 / 非流式入口，避免部分聊天链路仍像客服问答。
    #   ★ 完全自主模式：属导演类规则，不注入。
    try:
        from .companion.realness import build_realness_prompt
        _char_cfg = character_manager.get_character(_char_key) or character_manager.load_character(_char_key) or {}
        _realness_block = "" if _blocks_off else build_realness_prompt(
            session_id=session_id,
            character_id=_char_key,
            emotion=str(_emo or "calm"),
            intensity=float(_intensity or 0.5),
            user_text=user_text,
            character_config=_char_cfg if isinstance(_char_cfg, dict) else {},
        )
        if _realness_block:
            blocks.append(_realness_block)
    except Exception as _re:
        print(f"[Realness] 真人感注入失败(静默): {_re}", flush=True)
    # ★ 情绪记忆：记录「让我开心/受伤的话」+ 注入 prompt（复用 AI-Companion-Emotion 核心逻辑）
    try:
        from .emotion_memory import get_emotion_memory
        _emm = get_emotion_memory()
        # 记录情绪触发（正向/负向情绪 → positive/negative）
        if user_text:
            _em = str(_emo or "calm").lower()
            if _em in ("happy", "excited", "loving", "tender", "playful", "reconciling"):
                _emm.record_positive(session_id, _char_key, user_text, emotion=_em, intensity=_intensity)
            elif _em in ("sad", "upset", "angry", "cold", "worried"):
                _emm.record_negative(session_id, _char_key, user_text, emotion=_em, intensity=_intensity)
        _emm_block = ("" if _blocks_off else _emm.get_format_for_prompt(session_id, _char_key))
        if _emm_block:
            blocks.append(_emm_block)
    except Exception as _ee:
        print(f"[EmotionMemory] 注入失败(静默): {_ee}", flush=True)
    # ★ 感知引擎：用户状态推断 + 语气感知 + 天气感知（复用 perception 方案）
    #   ★ 完全自主模式：属感知类，不注入（也不做探测）。
    try:
        from . import perception
        _pblock = ("" if _blocks_off else await perception.build_perception_block(user_text, _char_key))
        if _pblock:
            blocks.append(_pblock)
    except Exception as _pe:
        print(f"[Perception] 感知注入失败(静默): {_pe}", flush=True)
    # ★ 系统轻感知：前台窗口 + 键鼠空闲（受感知总开关 AWARENESS_ENABLED 门控；纯 ctypes 只读，<5ms 直接同步）
    #   ★ 完全自主模式：属感知类，不注入。
    try:
        from . import awareness
        _ablock = ("" if _full_auto else awareness.build_block(session_id, _char_key))
        if _ablock:
            blocks.append(_ablock)
    except Exception as _ae:
        print(f"[Awareness] 轻感知注入失败(静默): {_ae}", flush=True)
    # ★ 生活陪伴感知：陪伴模式下允许 AI「直说」看着屏幕（TA 显式授权，不受全局开关门控）
    try:
        from . import awareness
        _life_mode, _life_label = awareness.companion_mode(session_id, _char_key)
        # ★ 完全自主模式：屏幕陪伴感知整段跳过（不截屏、不注入）——属感知类，
        #   截屏只为注入服务，跳过可省一次截屏开销。
        if _life_mode and _life_mode in awareness.LIFE_COMPANION_MODES and not _full_auto:
            _asking = awareness.is_see_question(user_text)
            _continue = awareness.is_continue_watch_command(user_text)
            _snap = None
            # 截屏时机：
            #   1. TA 问「你在看什么 / 我在干嘛」→ 按原冷却截屏（90s），说「继续看」强制刷新
            #   2. ★ 屏幕感知开启 + 持续聊天 → 每 90s 自动看一眼（AI 的回复自然评论屏幕；
            #      修复「持续话题中独立评论被 90s 规则卡死 → 评论永远静默」的问题）
            if _asking or _continue:
                if awareness.allow_fresh_capture(session_id, _char_key, force=_continue):
                    try:
                        _snap = await get_loop().run_in_executor(
                            None, awareness.capture_now, "你"
                        )
                    except Exception:
                        _snap = None
                else:
                    _snap = {"summary": awareness.last_screen_summary(session_id, _char_key)}
            elif config.get("SCREEN_PERCEPTION_ENABLED", False):
                if awareness.allow_fresh_capture(session_id, _char_key):
                    try:
                        _snap = await get_loop().run_in_executor(
                            None, awareness.capture_now, "你"
                        )
                    except Exception:
                        _snap = None
            if _snap and _snap.get("summary"):
                awareness.remember_screen_summary(session_id, _char_key,
                                                  _snap.get("summary", ""))
            # 构建陪伴块（窗口标题轻感知 + 陪 TA 的姿态指令）
            _cb_block = awareness.build_companion_block(session_id, _char_key, user_text)
            if _cb_block:
                blocks.append(_cb_block)
            # 画面摘要注入：问句 = 如实告知；持续聊天 = 自然评论（不强制每句都提）
            if _snap and _snap.get("summary"):
                if _asking:
                    _see_lines = [
                        "【你刚看到的画面（截屏分析，TA 在问你能看到什么，请如实、口语化地告诉 TA）】",
                        f"- 画面内容：{_snap.get('summary', '')}",
                    ]
                    if _snap.get("scene_name"):
                        _see_lines.append(f"- 应用/场景：{_snap.get('scene_name')}")
                    if _snap.get("topic_hint"):
                        _see_lines.append(f"- 可以聊的点：{_snap.get('topic_hint')}")
                    blocks.append("\n".join(_see_lines))
                else:
                    blocks.append(
                        "【你凑过去瞄了一眼 TA 的屏幕】画面：" + _snap.get("summary", "")
                        + "\n回复时可以自然地接一句你看到的（吐槽/好奇/关心，一句就够）；"
                        "如果这轮话题不适合提屏幕，就正常聊，别硬提。"
                    )
    except Exception as _cbe:
        print(f"[Companion] 陪伴感知注入失败(静默): {_cbe}", flush=True)
    # ★ iOS 手机屏幕：用户主动截屏回传后，下次聊天注入（让 AI「看到」手机在干嘛）
    try:
        from . import ios_bridge
        _ios_block = ("" if _blocks_off else ios_bridge.build_ios_screen_block(session_id, _char_key, user_text))
        if _ios_block:
            blocks.append(_ios_block)
    except Exception as _ie:
        print(f"[iOS] 手机屏幕块注入失败(静默): {_ie}", flush=True)
    # ★ iOS App 活动：用户打开 App 自动上报后注入（让 AI 知道 TA 在用什么，不靠截图）
    try:
        from . import ios_bridge
        _app_block = ("" if _blocks_off else ios_bridge.build_app_activity_block(session_id, _char_key, user_text))
        if _app_block:
            blocks.append(_app_block)
    except Exception as _aae:
        print(f"[iOS] App 活动块注入失败(静默): {_aae}", flush=True)
    # ★ 连贯情绪：用户情绪趋势块（连续追踪，趋势 + 当前语气指令）
    try:
        from .emotion_engine import emotion_timeline
        _emo_block = ("" if _blocks_off else emotion_timeline.build_prompt_block(session_id, _char_key))
        if _emo_block:
            blocks.append(_emo_block)
    except Exception as _ee:
        print(f"[EmotionTrend] 连贯情绪注入失败(静默): {_ee}", flush=True)
    # ★ AI 隐藏情绪：憋着不说的情绪（语气能感觉到，但不主动挑明）
    try:
        from . import ai_mood
        _mood_block = ("" if _full_auto else ai_mood.build_prompt_block(session_id, _char_key))
        if _mood_block:
            blocks.append(_mood_block)
    except Exception as _me:
        print(f"[AiMood] 隐藏情绪注入失败(静默): {_me}", flush=True)
    # ★ 多轮对话连贯性：本次对话状态（主线/当前话题/上一轮问的还没答/没聊完的）
    try:
        from . import conv_state
        _conv_block = conv_state.build_conv_state_block(session_id, _char_key, user_text)
        if _conv_block:
            blocks.append(_conv_block)
    except Exception as _ce:
        print(f"[ConvState] 对话状态注入失败(静默): {_ce}", flush=True)

    # ★ 话题延续引擎：结构化互动/约定/奖惩游戏的状态跟踪与主动延续
    # ★ 理解层开关：关闭时跳过（话题延续依赖语义分析，属于理解层）
    _semantic_for_tc = None
    _tc_on = True
    try:
        from . import config as _cfg
        _tc_on = _cfg.understanding_enabled()
    except Exception:
        _tc_on = True

    if _tc_on:
        try:
            from .semantic import SemanticAnalyzer
            from . import topic_continuation

            # ★ 语义分析跟理解层走（角色卡 understanding_model > 全局 > 默认）
            _tc_analyzer = SemanticAnalyzer(model=config.understanding_model(character_name or "") or None)
            _tc_ctx_builder = None
            try:
                from .semantic.context_builder import ContextBuilder
                _tc_ctx_builder = ContextBuilder(max_turns=5)
            except Exception:
                pass

            _tc_context_str = ""
            if _tc_ctx_builder:
                try:
                    _tc_recent = db.recent_messages(session_id, 10, _char_key)
                    _tc_context_str = _tc_ctx_builder.build(_tc_recent)
                except Exception:
                    pass

            if user_text:
                # ★ 2026-09-10 通话延迟优化：上面 CompanionOS 已经对**同一句话**
                #   跑过一次语义分析（约 5 秒），这里再跑一次是纯重复劳动。
                #   有现成结果就直接复用（输入完全一致，结果等价，零质量损失）；
                #   拿不到才回退到原来自己分析。
                _tc_sem = None
                try:
                    _tc_sem = (brain_state or {}).get("semantic_state")
                except Exception:
                    _tc_sem = None
                if _tc_sem is not None:
                    _semantic_for_tc = _tc_sem
                else:
                    _semantic_for_tc = await _tc_analyzer.analyze(user_text, _tc_context_str)
                _tc_state = topic_continuation.update_state(
                    session_id, _char_key, user_text, _semantic_for_tc
                )
                # 把会话级约定沉淀成长期契约（阶段 2）
                try:
                    from .topic_continuation import contracts as _tc_contracts
                    _tc_contracts.sync_from_state(session_id, _char_key, _tc_state)
                except Exception as _sync_e:
                    print(f"[TopicCont] 契约同步失败(静默): {_sync_e}", flush=True)

            _tc_block = ("" if _blocks_off else topic_continuation.build_block(
                session_id, _char_key, _semantic_for_tc
            ))
            if _tc_block:
                blocks.append(_tc_block)
        except Exception as _tce:
            print(f"[TopicCont] 话题延续注入失败(静默): {_tce}", flush=True)

    # ★ 关系升级仪式/降级注释（阶段变化那一刻的表现，一次触发）
    #   ★ 完全自主模式：关系表现类块不注入，交给模型自己演。
    try:
        from .relationship.ritual import build_ritual_block
        _ritual_block = ("" if _blocks_off else build_ritual_block(session_id, _char_key))
        if _ritual_block:
            blocks.append(_ritual_block)
    except Exception as _re:
        print(f"[Ritual] 仪式注入失败(静默): {_re}", flush=True)
    # ★ 关系深度：专属梗注入（只在自然时提起）
    try:
        from . import relationship_extras
        _anchor_block = ("" if _full_auto else relationship_extras.build_anchor_block(session_id, _char_key, user_text))
        if _anchor_block:
            blocks.append(_anchor_block)
    except Exception as _ae2:
        print(f"[Anchor] 专属梗注入失败(静默): {_ae2}", flush=True)
    # ★ 边界松动：AI 之前拒绝的话题，现在想通了，自然带一句
    try:
        from . import relationship_extras
        _relax = (None if _full_auto else await relationship_extras.check_relax(session_id, _char_key))
        if _relax:
            blocks.append(f"【你之前拒绝聊的话题，现在想通了】你可以自然带一句：{_relax}")
    except Exception as _re2:
        pass
    # ★ 自我审计（2026-09-13）：TA 问「你怎么看我/我们的关系」时，
    #   把真实积累的关系数据、里程碑、状态增量和关系记忆注入为"内心依据"，
    #   让这类回答从记忆里长出来，而不是模型现场编。
    #   只在命中问题时注入（正则判断零成本），平时一个字都不占预算。
    #   ★ 完全自主模式：自我审计属分析/规则类，不注入。
    if not internal_generation and user_text and not _full_auto:
        try:
            from . import self_audit as _self_audit
            if _self_audit.is_self_view_question(user_text):
                _audit_block = _self_audit.build_self_audit_block(session_id, _char_key)
                if _audit_block:
                    blocks.append(_audit_block)
        except Exception as _sa_e:
            print(f"[SelfAudit] 自我审计注入失败(静默): {_sa_e}", flush=True)
    # ★ AI 控制电脑：意图引擎主动建议动作，注入 prompt 让 AI 自然输出 [ACTION] 标记
    try:
        from . import control
        _ctx = {"emotion": _emo, "intensity": _intensity, "period": time_system.time_period()}
        _sug = (None if _blocks_off else control.suggest_action(_ctx, user_text))
        if _sug:
            _hint = _sug.get("_hint", "")
            _act_json = json.dumps({k: v for k, v in _sug.items() if not k.startswith("_")}, ensure_ascii=False)
            blocks.append(
                "【此刻可以做的动作】\n"
                "你可以在回复末尾自然地带一个动作标记，帮 TA 操作电脑（可选，觉得不合适就不加）：\n"
                f"[ACTION]{_act_json}[/ACTION]\n"
                "规则：只在用户明确要求、或此刻这个动作确实贴心时才加；每次最多一个；语气自然别生硬。\n"
                "【控制设备区分】主人让你「打开XX/调音量/切歌」这类操作时，系统会直接执行——"
                "默认控制的是【电脑】；主人说「在手机上XX」时控制的是【手机】（走 iOS 快捷指令）。"
                "所以这类明确指令你不用再写 [ACTION]（系统已处理），你只需要自然地接话；"
                "[ACTION] 标记只用于你主动想帮 TA 做的贴心动作（且都是电脑动作）。"
            )
    except Exception as _ce:
        print(f"[Control] 意图建议注入失败(静默): {_ce}", flush=True)
    # ★ 表情包：开关打开且有素材时，注入可用表情包列表，让 AI 自然用 [sticker:文件名] 引用
    if sticker:
        try:
            from . import sticker_manager as _sm
            # 2026-09-13：黄脸系已禁用，只列 AI 可用的（猫耳少女/实拍系 + 用户自传）
            _list = _sm.ai_usable_stickers()
            if _list:
                _ordered = _list[:40]
                _names = "、".join(s["filename"] for s in _ordered)
                _desc = "；".join(
                    (s["filename"] + ("（" + s["tag"] + "）" if s.get("tag") else ""))
                    for s in _ordered
                )
                blocks.append(
                    "【表情包】你想发图时必须**真的写出标记** [sticker:文件名]（单独一行），"
                    "程序才会把对应图片发出去；光说‘发一张给你’‘发个表情包’只是文字，用户看不到图。"
                    "只能从下面列出的文件名里选，不要编造：" + _names +
                    "。说明：" + _desc +
                    "。使用分寸（2026-09-13 大幅放宽）：**想发就发，不需要特别的场景理由**——"
                    "日常闲聊、吐槽、玩梗、撒娇、安慰、答应事情、分享、接住主人的话……"
                    "任何你觉得配一张图更有味道的时刻都可以随手发一张；"
                    "一条回复最多 1 张（斗图或主人让你多发时可连发多个标记，各占一行、选不同的）。"
                    "只有正经解释重要事情、主人明确要认真谈事时才不发。"
                )
        except Exception:
            pass
    if _sticker_context:
        blocks.append(_sticker_context)
    # ★ 防复读：把最近几条 AI 说过的话亮给模型，禁止换个说法重说一遍。
    #   文字聊天之前没注入这个（只有通话做了），导致同一意思反复说（用户反馈"重复啰嗦"）。
    try:
        from .companion.quality_guard import recent_ai_texts as _chat_said
        # ★ 完全自主模式：防复读属规则类，不注入（留给模型自己把握）
        _said = [] if _full_auto else [s for s in _chat_said(session_id, _char_key, 6) if str(s or "").strip()]
        if _said:
            _said_lines = "\n".join(f"- {str(s)[:40]}" for s in _said[-4:])
            blocks.append(
                "【防复读】你最近说过：\n" + _said_lines + "\n"
                "别把这些意思换个说法再说一遍；这次回复要推进话题、说个新想法、或问TA一件相关的小事。"
            )
    except Exception as _de:
        print(f"[AntiRepeat] 防复读注入失败(静默): {_de}", flush=True)
    extra = "\n\n".join(blocks)

    # ★ system prompt 总长限制：防止 blocks 过多导致 LLM 上下文超限（长对话 + 多模块同时激活时）
    #   companion/prompt.py 有 MAX_SYSTEM_CHARS=12000，但这里之前没限制，blocks 25+ 个可能超 20k 字。
    #   裁剪策略：blocks 按注入顺序排，前面的（人设/当前意图/连续性/规则）优先级高，
    #   从末尾（低优先级：防复读/表情包/控制/边界松动等）开始丢弃。
    _MAX_EXTRA_CHARS = 14000  # 约 9k token，适配 32k 模型；64k 模型留足空间给历史+用户消息
    if len(extra) > _MAX_EXTRA_CHARS:
        _trim_blocks = list(blocks)
        while len("\n\n".join(_trim_blocks)) > _MAX_EXTRA_CHARS and len(_trim_blocks) > 5:
            _trim_blocks.pop()  # 丢最低优先级块
        extra = "\n\n".join(_trim_blocks)
        if len(extra) > _MAX_EXTRA_CHARS:
            extra = extra[:_MAX_EXTRA_CHARS]

    out = [dict(m) for m in messages]
    for m in out:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + "\n\n" + extra
            try:
                # ★ 上下文快满：估算接近大脑模型上限时，她主动说「记不住了」（本地台词，限频）
                from . import api_hunger as _ah
                _ah.maybe_context_full(out, str(config.get("CURRENT_CHAT_MODEL") or ""))
            except Exception:
                pass
            return out
    out.insert(0, {"role": "system", "content": extra})
    try:
        from . import api_hunger as _ah
        _ah.maybe_context_full(out, str(config.get("CURRENT_CHAT_MODEL") or ""))
    except Exception:
        pass
    return out


def pick_model(requested_model: str, stream: bool, character_id: str = "") -> str:
    """
    普通聊天：
      前端指定模型（人格设置的角色级大脑） > 角色卡 model > SELECTED_MODEL > CURRENT_CHAT_MODEL

    后台非流式任务（记忆提炼等）：
      角色卡 memory_model > MEMORY_EXTRACT_MODEL > deepseek-chat

    深度思考模式：传 character_id 时，若该角色卡开启 deep_thinking，
    生成层改用 deepseek-reasoner（先想后答）。
    """

    if not stream:
        return config.memory_extract_model(character_id)

    requested = str(requested_model or "").strip()

    # ★ 人格设置为唯一权威（2026-09-11 统一数据目录配套）：
    #   前端 localStorage 里 Contact.model 为空时，brainConfig 会兜底全局 settings.model
    #   再当作 requested 传上来——其特征是恰好等于 legacy 全局键值（SELECTED/CURRENT）。
    #   这种「回声值」不算显式指定，清空让角色卡 model 优先，否则旧前端缓存会永远
    #   盖住人格设置里新换的主脑。
    if requested and requested in (
        str(config.get("SELECTED_MODEL") or "").strip(),
        str(config.get("CURRENT_CHAT_MODEL") or "").strip(),
    ):
        requested = ""

    # ★★ 2026-09-11 修：角色卡（人格设置）为**唯一权威**，优先于前端传上来的 model。
    #
    #   原来这里是「前端显式指定优先、直接 early return」，只有"前端值恰好等于全局"
    #   这一种情况才会让位给角色卡。漏洞：前端 localStorage 缓存的是某个**非全局**模型时
    #   （实测：骨子卡里已改成 deepseek-flash，前端仍缓存 glm-5.3-flash），
    #   角色卡永远被盖住 —— 在人格设置页换模型对 App 前台完全无效，只有 QQ/主动消息生效。
    #
    #   之所以敢让角色卡无条件优先：人格设置页改模型时，save() 写 localStorage 的同时
    #   syncPersonaCard() 会把整个 persona（含 model）防抖同步到角色卡，两边本该一致；
    #   会分歧只有两种情况——角色卡被带外修改，或前端缓存过期。两种都该以角色卡为准。
    if character_id:
        try:
            _cfg = character_manager.get_character_any(character_id) or {}
            # ★ 本地大脑开关（人格设置页，2026-09-11）：开 = 该角色主脑走本地 Ollama 模型。
            #   本函数只在 stream=True 的聊天主脑链路被调用，后台理解/记忆提炼不受影响
            #   （config.background_model 已把 local-brain 映射到云端便宜模型）。
            #   关掉开关即一键切回：角色卡 model 字段始终保留，从不被本地开关覆盖。
            if _cfg.get("local_brain") and config.local_brain_enabled():
                return "local-brain"
            _role_model = str(_cfg.get("model") or "").strip()
            if _role_model:
                # ★ 2026-09-11：角色卡配了 model，也要尊重「深度思考模式」开关。
                #   原来这里直接 return _role_model —— 于是"深度思考"对**所有在人格设置里
                #   配过大脑的角色**都是摆设（配了 model 就走不到下面那段 deep_thinking 判断）。
                #   现在：开了开关 → 按 DEEP_THINKING_MODEL 走（默认 deepseek-flash / V4.1），
                #   关着 → 照旧用角色卡的大脑。
                if _cfg.get("deep_thinking"):
                    _dtm = config.deep_thinking_model(_role_model)
                    if _dtm:
                        return _dtm
                return _role_model
        except Exception:
            pass

    # 角色卡没配模型时才认前端的显式指定（分层大脑/临时切换等）
    if requested:
        return requested

    model = (
        config.get("SELECTED_MODEL")
        or config.get("CURRENT_CHAT_MODEL")
        or "deepseek-chat"
    )

    # ★ 深度思考模式：角色卡开启 deep_thinking 后，生成层改用 deepseek-reasoner
    #   （仅 DeepSeek 生效；切到 GLM 等其它 provider 时深度思考不触发，保持原模型）
    if character_id:
        try:
            _cfg = character_manager.get_character_any(character_id) or {}
            if _cfg.get("deep_thinking"):
                model = config.deep_thinking_model(model)
        except Exception:
            pass

    return model


# ---------- 手动记忆 ----------

def check_manual_memory(messages: list, session_id: str = "default", character_id: str = "default"):
    """最后一条 user 消息若是「记住：xxx」→ 返回回复文本（SSE 短路），否则 None"""
    last_user = None
    for m in reversed(messages):
        if m.get("role") == "user":
            if db.is_internal_chat_message("user", m.get("content")):
                continue
            last_user = m.get("content") or ""
            break
    if last_user is None:
        return None
    # 用户可以直接纠正或撤销一条记忆，不再让模型自由决定是否执行。
    # ★ 防误判（2026-09-09）：调侃语气（"忘记看时间了哈哈哈"）不是删除记忆指令，
    #   含"哈哈/hhh/笑死"直接跳过；"忘记看/忘了看"是忘了做某事，也不删。
    if re.search(r"哈哈|hhh|笑死|😂|😄", str(last_user)):
        return None
    _forget = re.match(r"^\s*(?:忘掉|忘记|删除|清除|删掉)(?:我说的|你记的|关于|那条)?\s*(.{2,30}?)\s*[。.!！]?$", str(last_user), re.S)
    if _forget and re.match(r"^(?:看|听|说|回)", _forget.group(1)):
        _forget = None   # "忘记看了/忘记说了"= 忘了做某事，不是删记忆
    if _forget:
        removed = memory_manager.forget_matching(
            _forget.group(1).strip(), session_id=session_id, character_id=character_id
        )
        if removed:
            return "好，我已经把这条记忆删掉了。"
        return "好，我先不再使用这条信息；如果你指的是另一条记忆，可以把原话告诉我。"
    content = memory_manager.is_manual_memory(last_user)
    if not content:
        return None
    # ★ 规则识别：面向 AI 的行为要求（"你要热情点"/"别一问一答"/"每天早安"）
    #   与"用户事实"（"我叫顾圣豪"/"我喜欢火锅"）分开存：规则用 rule 类型 +
    #   高 importance，由 rule_block 常驻注入，确保 AI 一定记住并遵守。
    _RULE_KEYWORDS = ("你要", "你以后", "以后你", "以后", "别", "不要", "不准",
                      "每天", "记得", "记住", "必须", "应该", "主动", "热情",
                      "温柔", "多说", "少说", "别总", "不要总")
    _is_rule = any(k in content for k in _RULE_KEYWORDS)
    # ★ 手动记忆也按角色隔离写入（不再写进 default 桶）
    action = memory_manager.dedupe_insert(
        content,
        memory_type="rule" if _is_rule else "fact",
        importance=9 if _is_rule else 7,
        session_id=session_id,
        character_id=character_id,
    )
    if action == "duplicate":
        return "嗯，这条我已经记着啦，放心～"
    if _is_rule:
        return "记住了～这条规则我会一直遵守：%s" % content
    return "已记住：「%s」\n下次聊到相关的话题我会想起来的。" % content


def rule_block(session_id: str, character_id: str = "default", limit: int = 12) -> str:
    """加载用户立下的行为规则（memory_type='rule'），常驻注入 prompt。

    规则是"要一直遵守"的，不像普通记忆那样按相关性检索；相关性低时普通
    记忆可能不进 prompt，规则却必须每次都在——否则 AI 会"忘了"用户教过的事。
    """
    try:
        mems = db.valid_memories(session_id=session_id, character_id=character_id)
        rules = [m for m in mems if str(m.get("memory_type", "")).strip() == "rule"]
    except Exception:
        return ""
    if not rules:
        return ""
    rules = rules[:limit]
    lines = "\n".join("- " + str(m.get("memory_content", "")).strip() for m in rules)
    return "【你与TA之间立下的规则（必须一直遵守，不要忘记）】\n" + lines


# ---------- 定时提醒解析（模型主判 + 正则兜底） ----------
#
# ★ 2026-09-16 定时提醒误触发修复（根因与修法见 UI改版方案/_定时提醒误触发-根因与修法-2026-09-16.md）
#   事故：用户说「哎呀再等我会哈 到时候聊到两点半吧」，正则把「到时候」当委托、
#   把时间词之后的残尾「半吧」当事项，建了个 02:00 的提醒并回她
#   「好啦，34分钟后（2026-09-16 02:00）我会提醒你「半吧」，到时间见～」。
#   用户裁决：A. **模型主判，正则降级为兜底**。判断"这是不是一项委托/约定"本来
#   就是角色自己的能力与责任（capabilities.py 的「定时提醒」能力条已按此改写），
#   这里的正则只在模型层不可用（无 Key / 超时 / 异常 / 解析失败）时兜底。
#   ★ 目标口径（用户更正）：不是"少建任务"，而是**该建的（委托/每天约定）建得对**
#   （时间与事项准确）、**不该建的（闲聊）不建**。

# 匹配：X分钟后(发|说|提醒|给我发|跟我说)Y
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
           "十": 10, "零": 0}


def _cn_number(raw: str) -> int:
    raw = str(raw or "").strip()
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    # 支持十一、二十、二十五等常见中文数字
    if "十" in raw:
        left, _, right = raw.partition("十")
        left_num = _CN_NUM.get(left, 1) if left else 1
        right_num = _CN_NUM.get(right, 0) if right else 0
        return int(left_num) * 10 + int(right_num)
    return 0


# ── 粗筛（决定要不要为这一句花一次模型调用） ──────────────────────────────
# ★ 为什么要粗筛：模型主判发生在前台（用户等着回复），每轮都调既费钱又加延迟。
#   取舍：宁可多调一次便宜模型，也不能漏掉用户随口说的委托（「到时候聊到两点半吧」
#   正是漏了会挨骂的那类），但纯闲聊（「我有点累」）不该命中——所以要求
#   "提醒类动词" 或 "数字/中文数字 + 点/分/小时" 这类时间标记。
_REMINDER_COARSE = re.compile(
    r"提醒|叫我|叫醒|喊我|催我|记得|别忘了|帮我记|通知我|告诉我|"
    r"\d{1,2}\s*[点:：]|[零一二两三四五六七八九十]{1,3}\s*[点时]|"
    r"分钟后|小时后|半小时|一刻钟|明早|明天|后天|今晚|晚点|待会儿|待会|过会儿"
)

# ── 兜底路径的触发词（★收紧为显式祈使） ──────────────────────────────────
# ★ 2026-09-16：移除「到时候 / 告诉我 / 通知 / 发个消息」这类含糊词——
#   它们可以构成"AI 自己的承诺"，但不足以证明"用户在委托提醒"
#   （「到时候聊到两点半吧」就是被「到时候」害的）。
_REMINDER_IMPERATIVE = re.compile(r"提醒我|提醒一下我|提醒下我|叫我|叫醒我|记得叫我|帮我记|喊我|催我")

# 夜间活动词：裸时间 + 夜间活动 → 6~11 点理解成晚上（「11 点提醒我睡觉」= 23:00）
_NIGHT_WORDS = re.compile(r"睡觉|睡前|晚安|晚睡|夜里|半夜|凌晨|熬夜|就寝|哄睡|睡了|去睡|休息")

# ── 时段/时刻语义（★2026-09-16：误触发修复 + 凌晨跨天；判据由用户两次追加确定） ──
# 总判据：**时段词决定"哪半天"，小时决定"哪一刻"**。时段词分五类：
#   · 正午型 中午/正午/晌午/午间            → 当天正午（12 点）
#   · 午后型 下午/傍晚                      → 当天下午/傍晚（14:xx）
#   · 夜间型 晚上/今晚/今夜/夜里/夜间/前半夜/半夜/深夜
#                                          → **今天开始的这个夜晚**：它的 18~23 点在今天
#                                            （晚上十一点 = 今天 23:00），它的 0~5 点落在
#                                            **次日凌晨**（晚上两点 = 次日 02:00）
#   · 凌晨型 凌晨                           → **今天已经进入的凌晨**：凌晨两点半 @01:25 仍是
#                                            今天 02:30（还没到，留今天）；已过则顺延次日
#   · 上午型 上午/早上/早晨/一早/清晨/明早   → 当天上午
_PERIOD_NOON = re.compile(r"中午|正午|晌午|午间")
_PERIOD_AFTERNOON = re.compile(r"下午|傍晚")
_PERIOD_TONIGHT = re.compile(r"晚上|今晚|今夜|夜里|夜间|前半夜|半夜|深夜")
_PERIOD_DAWN = re.compile(r"凌晨")
_PERIOD_AM = re.compile(r"上午|早上|早晨|一早|清晨|明早")
_PERIOD_ALL = (r"中午|正午|晌午|午间|下午|傍晚|晚上|今晚|今夜|夜里|夜间|前半夜|半夜|深夜|"
               r"凌晨|上午|早上|早晨|一早|清晨|明早")
# 时段词**紧贴**时间词（「下午两点半」「今晚十二点」）——只有这种直接证据才用来给模型
# 回的时刻做 12→24 校时；句子别处出现「晚上」不构成校时理由（可能是"我晚上没睡好"）。
_PERIOD_BEFORE_TIME = re.compile(
    r"(?:" + _PERIOD_ALL + r")\s*(?:\d{1,2}|[一二两三四五六七八九十零]+)\s*[点:：时]")


def _resolve_hour_24(h: int, ctx: str, has_day: bool = False):
    """把「几点」按句中的时段/活动线索换算成 24 小时制，返回 (hour, next_day)。

    next_day=True：该时刻落在**次日的 0 点档**（调用方把日期 +1）。

    ★ 显式判定表（2026-09-16 用户两次追加要求后的最终口径；**不用粗粒度 +12**）：

    | 小时  | 句中线索                                        | 归属                            | next_day |
    |-------|-------------------------------------------------|---------------------------------|----------|
    | 0     | 「零点」                                         | 按字面（已过由调用方顺延）        | False    |
    | 12    | 夜间型                                           | 该夜的 0 点档 = **次日** 00:xx   | True     |
    | 12    | 凌晨型                                           | 参照日的 0 点档（今天 00:xx）     | False    |
    | 12    | 无时段词 + 夜间活动词 + **无日期前缀**            | 今晚 0 点档 = 次日 00:xx         | True     |
    | 12    | 其余（含「明天十二点」「中午十二点」）            | 当天 12:xx（默认正午）           | False    |
    | 1~5   | 午后型                                           | 当天下午 h+12（14:xx）           | False    |
    | 1~5   | 夜间型                                           | **次日**凌晨 h 点               | True     |
    | 1~5   | 凌晨型 / 上午型 / 夜间活动词 / 有日期前缀         | 按字面 h 点                     | False    |
    | 1~5   | 无任何线索                                       | 白天读 h+12（14:30）             | False    |
    | 6~11  | 午后型 / 夜间型 / 凌晨型                          | 当天 h+12（晚上 h 点）           | False    |
    | 6~11  | 夜间活动词 + 无上午型 + 无日期前缀                | 当天 h+12（旧口径，11 点睡觉）    | False    |
    | 6~11  | 其余（上午型 / 正午型 / 裸小时）                  | 按字面                          | False    |

    ★ 「十二点」的 0 点档只在**有夜间/凌晨时段词**、或**没有日期前缀但有夜间活动词**
      （「十二点半提醒我睡觉」）时成立；写了日期（「明天十二点半」）就默认正午。
    """
    s = str(ctx or "")
    noon = bool(_PERIOD_NOON.search(s))
    aft = bool(_PERIOD_AFTERNOON.search(s))
    tonight = bool(_PERIOD_TONIGHT.search(s))
    dawn = bool(_PERIOD_DAWN.search(s))
    am = bool(_PERIOD_AM.search(s))
    act = bool(_NIGHT_WORDS.search(s))
    if h == 0:
        return 0, False
    if h == 12:
        if tonight:
            return 0, True
        if dawn:
            return 0, False
        if act and not has_day and not noon and not am:
            return 0, True
        return 12, False                     # 默认正午（含「明天十二点半」）
    if 1 <= h <= 5:
        if aft:
            return h + 12, False
        if tonight:
            return h, True
        if dawn or am or act or has_day:
            return h, False
        return h + 12, False
    # 6~11 点
    if aft or tonight or dawn:
        return h + 12, False
    if act and not am and not has_day:
        return h + 12, False                 # 旧口径：「11 点提醒我睡觉」= 23:00
    return h, False


def resolve_hour_24(h, ctx, has_day=False):
    """（**唯一出口**）时段判定表的公开入口 —— 承诺/循环约定链路（ai_promise）也调它。

    ★ 2026-09-16 裁决②：判定表只能有一张。`ai_promise.resolve_trigger_phrase` /
      `_recurring_hhmm` 都改成复用本函数，不许再各写一份「+12」算术
      （旧实现里「晚上两点」在承诺链路是当天 14:00、在提醒链路是次日 02:00）。
    """
    return _resolve_hour_24(h, ctx, has_day=has_day)


# 「差X分」：九点差五分 = 08:55（旧实现把「五分」当 5 分 → 算成 09:05）
_DIFF_MIN = re.compile(
    r"(?P<h>\d{1,2}|[一二两三四五六七八九十]+)\s*[点时]\s*差\s*"
    r"(?P<d>\d{1,2}|[一二两三四五六七八九十]+)\s*分?")

# 日期/时段前缀（绝对时间用；顺序敏感——长的写法在前，靠正则回溯兜住短写法）
# ★ 2026-09-16：「大后天」必须排在「后天」**前面**（否则「大后天中午」会被当成「后天中午」→ +2 天）。
_DAY_PART = (r"大后天(?:早上|上午|中午|下午|晚上)?|明早|明天(?:早上|上午|中午|下午|晚上)?|"
             r"后天(?:早上|上午|中午|下午|晚上)?|今晚|今天晚上|今天(?:早上|上午|中午|下午|晚上)?")
_DAY_RE = re.compile(_DAY_PART)
# 只有「带时段的日子」才允许"没说几点 → 默认 8 点"（与旧行为一致；裸「明天」不算）
_DAY_DEFAULT_RE = re.compile(
    r"大后天(?:早上|上午|中午|下午|晚上)?|明早|明天(?:早上|上午|中午|下午|晚上)|"
    r"后天(?:早上|上午|中午|下午|晚上)?|今晚|今天晚上|今天(?:早上|上午|中午|下午|晚上)")

# ★ 时段词单独成组：旧写法只让时段词藏在日期前缀里，「明天凌晨两点半」的「明天」
#   会整段被跳过 → 算成今天 02:30（实测红）。现在 日期 + 时段 + 几点 三段都进匹配。
_ABS_TIME = re.compile(
    r"(?P<day>" + _DAY_PART + r")?\s*"
    r"(?P<part>" + _PERIOD_ALL + r")?\s*"
    r"(?P<h>\d{1,2}|[一二两三四五六七八九十零]+)\s*(?:点|时)\s*"
    r"(?P<mi>半|一刻|\d{1,2}|[一二三四五六七八九十]+分)?")


def _minute_of(token) -> int:
    """「点」后面的分钟 token → 0~59：半=30、一刻=15、五分=5、30=30。"""
    t = str(token or "").strip()
    if not t:
        return 0
    if t.startswith("半"):
        return 30
    if t.startswith("一刻"):
        return 15
    n = _cn_number(t.rstrip("分").strip())
    return n if 0 <= n <= 59 else 0


# ── 自然语言时间短语 → datetime（★裁决②：判定表的唯一实现，两条链路共用） ──────
# 与 _ABS_TIME 同构（日期前缀 + 时段词 + 几点 + 分钟），只多认一个冒号时刻：
# 旧数据里 open_loops 存过「今天上午11:00」这种写法（当时 ai_promise 的正则认冒号），
# 前台兜底仍只认「点/时」（不额外放宽用户输入面，避免把聊天回显里的 [03:34] 当时刻）。
_PHRASE_SPLIT = re.compile(
    r"(?P<day>" + _DAY_PART + r")?\s*"
    r"(?P<part>" + _PERIOD_ALL + r")?\s*"
    r"(?P<h>\d{1,2}|[一二两三四五六七八九十零]+)\s*(?:点|时|[:：])\s*"
    r"(?P<mi>半|一刻|\d{1,2}|[一二三四五六七八九十]+分)?")


def day_offset(day_token) -> int:
    """日期前缀 → 天数偏移：今天/今晚/今天下午 = 0、明早/明天 = 1、后天 = 2、大后天 = 3。"""
    d = str(day_token or "")
    if d.startswith("大后"):
        return 3
    if d.startswith("后"):
        return 2
    if d.startswith("明"):
        return 1
    return 0


def relative_days(text) -> int:
    """「X天后 / X天之后」→ X（1~30）；没有则 0。

    ★ 2026-09-16 裁决②：兜底路径原来只认「分钟后/小时后/半小时后/一刻钟后」，
      「两天后的晚上八点提醒我喝水」会把**日期前缀静默丢掉**、按当天 20:00 建任务。
      「大后天」不走这里（它是日期前缀，由 _DAY_PART/day_offset 认），避免重复计一次。
    """
    m = re.search(r"(?P<num>\d+|[一二两三四五六七八九十]+)\s*天\s*(?:后|之后|以后)",
                  str(text or ""))
    if not m:
        return 0
    n = _cn_number(m.group("num"))
    return n if 1 <= n <= 30 else 0


def resolve_phrase_datetime(phrase, now=None):
    """自然语言时间短语 → datetime；认不出返回 None。

    ★ 2026-09-16 裁决②（一张判定表，不是两张）：短语→时刻**只有这一个出口**，
      前台兜底（_parse_timestamp_regex 的绝对时刻分支）与承诺/循环约定链路
      （ai_promise.resolve_trigger_phrase / _is_due）都走它；时段归属只有
      `_resolve_hour_24` 一处实现、日期与跨天只有 `_apply_day_and_hour` 一处实现。

    支持：ISO（YYYY-MM-DD HH:MM）/ 今天|明天|后天|**大后天** / **X天后** / 时段词 /
         几点[半|一刻|X分]（中文数字也认）。
    """
    t = str(phrase or "").strip()
    if not t:
        return None
    now = now or datetime.now()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})", t)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)))
        except Exception:
            return None
    m = _PHRASE_SPLIT.search(t)
    if not m:
        return None
    hh = _cn_number(m.group("h"))
    if not (0 <= hh <= 23):
        return None
    return _apply_day_and_hour(t, hh, _minute_of(m.group("mi")),
                               str(m.group("day") or ""), relative_days(t), now)


def _apply_day_and_hour(raw, hh, mi, day, rel_days, now):
    """把（时刻, 日期前缀, X天后）落成 datetime —— 两条链路的**共同落点**。

    与 `_resolve_hour_24` 的判定表配套：
      · 日期前缀定基准日：今天/今晚=0、明天=1、后天=2、大后天=3；「X天后」= X；
      · 表说该时刻属**次日 0 点档**（next_day）→ 基准日再加 1 天；
      · 「今晚/今天下午」+ 时刻已过 → 指明天（旧口径保留）；
      · 无日期前缀 + 时刻已过 → 顺延到下一次该时刻。
    """
    day = str(day or "")
    rel_days = int(rel_days or 0)
    has_day = bool(day) or rel_days > 0
    hh, next_day = _resolve_hour_24(hh, str(raw or ""), has_day=has_day)
    offset = day_offset(day) if day else rel_days
    if not next_day and day in ("今晚", "今天晚上", "今天下午") and hh < now.hour:
        offset = 1
    if next_day:
        offset += 1
    target = (now.replace(hour=0, minute=0, second=0, microsecond=0)
              + timedelta(days=offset)).replace(hour=max(0, min(23, hh)),
                                                minute=max(0, min(59, mi)))
    if not has_day and target <= now:
        target += timedelta(days=1)
    return target


# ── 内容守卫（★备忘 §二.2 第 3 条） ───────────────────────────────────────
# ★ 2026-09-16（思考链称呼同一批改动）：转写里的说话人标签不再写死「用户」，
#   改成角色卡配的称呼（`[HH:MM] 宝：…`，见 public/js/logs.js:userCallName）。
#   所以这里**不再列举**说话人名，改成认「时间戳 + 任意说话人 + 冒号」这个**结构**——
#   否则换个称呼（「宝」「张三」…）聊天回显就漏过去，又会像 id=23 那条一样被建任务。
#   判据性质没变：仍然是"这条内容不是事项，而是聊天回显"，与提醒时点/事项语义无关。
_REMINDER_ECHO = re.compile(
    r"\[\d{1,2}:\d{2}\]\s*(?:用户|我|你|AI|她|他|对方|群友|TA|ta|助手)?\s*[:：]"
    r"|\[\d{1,2}:\d{2}\]\s*[^\s\[\]（）()【】:：]{1,12}\s*[:：]")
# ★ 2026-09-16 补：没有时间戳的说话人前缀同样是**聊天回显**。
#   实测漏网路径：模型给了「[03:34] 用户：我中午睡了好久呢」，清洗函数把时间戳前缀
#   削掉后只剩「用户：我中午睡了好久呢」，只守卫"清洗结果"就让它建成了任务。
_REMINDER_ECHO_BARE = re.compile(
    r"^(?:用户|我|你|TA|ta|AI|ai|她|他|对方|群友|助手|宝)\s*[:：]")
_REMINDER_LP = re.compile(r"^[\s,，、。.！!？?~～:：;；\-—…「」『』\"'（）()【】\[\]]+")
_REMINDER_RP = re.compile(r"[\s,，、。.！!？?~～:：;；\-—…]+$")
_REMINDER_SCAFFOLD = re.compile(
    r"^\s*(?:以后|每天|每日|每晚|的|大概|差不多|左右|那个|这个|一下|一声|下|我|你|要|就|再|然后|先|帮我|给我)\s*")


def reminder_content_ok(content) -> bool:
    """内容守卫：明显不是「事项」的内容一律判抽取失败。

    拦四类（前三类都真实出现过，见存量 id=22/23）：
      · 含换行 —— 整段聊天记录被当事项
      · 含「[03:34] 用户：」这类聊天回显 —— 同上
      · 以「用户：/AI：」这类说话人前缀开头 —— 同上（时间戳被清洗削掉后的形态）
      · 长度 > 60 —— 一整段独白被当事项

    ★ 2026-09-16：**模型给的内容也必须过这道守卫**（见 _apply_reminder_verdict），
      不只是在正则兜底路径上过——模型有时会把聊天回显原样塞进 content。
    """
    s = str(content or "")
    if not s.strip():
        return False
    if "\n" in s or "\r" in s:
        return False
    if _REMINDER_ECHO.search(s):
        return False
    if _REMINDER_ECHO_BARE.search(s):
        return False
    if len(s) > 60:
        return False
    return True


def _clean_reminder_content(txt) -> str:
    """把候选文本洗成「事项」；洗不出来返回 ""（调用方据此反问，而不是瞎猜）。

    反复剥离（最多 6 轮）：日期/时段词（含裸「凌晨/中午/晚上」）、时间词（含半/一刻/差X分）、
    架子词（以后/每天/就/要）、首尾标点——直到不再变化。

    ★ 2026-09-16 补时段词：旧写法只剥「今天下午」这类**日期前缀**，
      「凌晨一点提醒我」会剩下「凌晨」被当成事项建出来（实测）。
    """
    s = _REMINDER_LP.sub("", str(txt or "").strip())
    _day_or_period = r"(?:(?:" + _DAY_PART + r")|(?:" + _PERIOD_ALL + r"))"
    for _ in range(6):
        _before = s
        s = re.sub(r"^\s*" + _day_or_period + r"\s*", "", s)
        s = re.sub(r"^\s*(?:\d{1,2}|[一二两三四五六七八九十零]+)\s*[点:：时]\s*"
                   r"(?:半|一刻|\d{1,2}|[一二三四五六七八九十]+)?\s*分?\s*"
                   r"(?:后|之后|以后|左右|的时候|时)?\s*", "", s)
        s = re.sub(r"^\s*(?:半|一刻|差\s*(?:\d{1,2}|[一二三四五六七八九十]+)\s*分)\s*"
                   r"(?:钟|后|之后|以后)?\s*", "", s)
        s = re.sub(r"^\s*(?:后|之后|以后|的时候|左右)\s*", "", s)
        s = re.sub(r"(?:\d{1,2}|[一二两三四五六七八九十零]+)\s*[点:：时]\s*"
                   r"(?:半|一刻|\d{1,2}|[一二三四五六七八九十]+)?\s*分?\s*$", "", s)
        s = re.sub(r"\s*" + _day_or_period + r"\s*$", "", s)
        s = _REMINDER_SCAFFOLD.sub("", s)
        s = _REMINDER_LP.sub("", s)
        s = _REMINDER_RP.sub("", s)
        if s == _before:
            break
    return s.strip()


def _extract_content_regex(raw: str) -> str:
    """从显式祈使里抽事项：祈使之后的整段优先，其次祈使之前（★不再取时间词残尾）。"""
    _imp = _REMINDER_IMPERATIVE.search(str(raw or ""))
    if not _imp:
        return ""
    _tail = _clean_reminder_content(str(raw)[_imp.end():])
    if _tail:
        return _tail
    return _clean_reminder_content(str(raw)[:_imp.start()])


def _parse_timestamp_regex(raw: str):
    """正则认时间 → datetime；认不出来返回 None。

    支持：10分钟后 / 两小时后 / 半小时后 / 一刻钟后 / 差X分 /
          明早 / 明天上午9点 / 后天下午3点 / 今晚8点 / 两点半 / 九点差五分。
    """
    now = datetime.now()
    raw = str(raw or "")
    trigger = None
    # ① 相对时间
    m = re.search(r"(?P<num>\d+|[一二两三四五六七八九十百]+)\s*分钟?\s*(?:后|之后|以后)", raw, re.I)
    unit = "minute"
    if not m:
        m = re.search(r"(?P<num>\d+|[一二两三四五六七八九十百]+)\s*个?\s*小时?\s*(?:后|之后|以后)", raw, re.I)
        unit = "hour"
    minutes = None
    if not m:
        _half = re.search(r"半\s*(?:个)?\s*小时\s*(?:后|之后|以后)", raw, re.I)
        if _half:
            m, minutes = _half, 30
    if not m:
        _quarter = re.search(r"一刻\s*钟?\s*(?:后|之后|以后)", raw, re.I)
        if _quarter:
            m, minutes = _quarter, 15
    if m and minutes is None:
        num = _cn_number(m.group("num"))
        if num > 0:
            minutes = num * (60 if unit == "hour" else 1)
    if m and minutes is not None:
        trigger = now + timedelta(minutes=min(minutes, 30 * 24 * 60))

    # ② 绝对时间
    if trigger is None:
        _h, _mi, _day = None, 0, ""
        # ★ 2026-09-16 裁决②：「X天后」原来被静默丢掉（「两天后的晚上八点」按当天算）
        _rel_days = relative_days(raw)
        _dz = _DIFF_MIN.search(raw)
        if _dz:
            _total = _cn_number(_dz.group("h")) * 60 - _cn_number(_dz.group("d"))
            if _total > 0:
                _h, _mi = (_total // 60) % 24, _total % 60
                for _dm in _DAY_RE.finditer(raw):
                    if _dm.start() <= _dz.start():
                        _day = _dm.group(0)
        else:
            _am = _ABS_TIME.search(raw)
            if _am:
                _day = str(_am.group("day") or "")
                _h = _cn_number(_am.group("h"))
                _mi = _minute_of(_am.group("mi"))
        if _h is None:
            # 只说了「明早/明天早上/今晚」这类带时段的日子（没说几点）→ 默认 8 点（旧行为）
            _dd = _DAY_DEFAULT_RE.search(raw)
            if _dd:
                _day, _h, _mi = _dd.group(0), 8, 0
        if _h is not None:
            # ★ 2026-09-16：小时归属与跨天统一交给 _apply_day_and_hour（判定表 + 落点各一处，
            #   与 resolve_phrase_datetime / ai_promise 那条链路共用同一份实现）
            trigger = _apply_day_and_hour(raw, _h, _mi, _day, _rel_days, now)
    return trigger


def _create_reminder_task(trigger, content, character_name, session_id, character_id,
                         raw: str = "") -> dict:
    """统一落库（含 30 天上限、**明显不合理时刻的护栏**、幂等键），返回给上层的建任务结果。

    ★ 2026-09-17 新增护栏（真机事故：2026-09-17 02:50 骨子给自己排了一条
      `2026-09-18 02:50 睡觉` 的提醒，并当面说「宝，1439分钟后也就是2026-09-18 02:50，
      该睡觉啦，我记着呢！」）：
      用户那句是「都快三点了……我不提睡的事」这种**当下的困**，模型却把
      "该睡觉了"理解成"明天这个点"。1439 分钟 = 24 小时差 1 分，是典型症状。

      判定权仍在模型（它说是不是委托，就照它办）；但"**同一时刻的明天**"这种
      明显不合理的落点必须拦：
        · 距离 ≥ 20 小时；
        · 且用户原话带着"此刻/马上"的语义（该…了 / 要…了 / 得…了 / 现在 / 马上 / 现在就去）；
      满足两条 → 判为"现在就该发生"，把触发时间夹到 2 分钟后（而不是排到明天）。
      只在**两条同时满足**时才动手，避免误伤"明天这个时候提醒我"这类明确委托。
    """
    now = datetime.now()
    try:
        _gap_h = (trigger - now).total_seconds() / 3600.0
        _immediate = bool(re.search(r"该[^。！？]{0,8}了|要[^。！？]{0,6}了|得[^。！？]{0,6}了|"
                                    r"现在就|马上|这就去|快去|早点睡", str(raw or "")))
        if _gap_h >= 20.0 and _immediate:
            print(f"[定时提醒] 护栏：模型给的时间在 {_gap_h:.1f} 小时后，"
                  f"但原话是『此刻』语义 → 夹到 2 分钟后（原 {trigger:%m-%d %H:%M}）: {str(raw)[:30]!r}",
                  flush=True)
            trigger = now + timedelta(minutes=2)
    except Exception:
        pass
    trigger = min(trigger, now + timedelta(days=30))
    minutes = max(1, min(30 * 24 * 60, int((trigger - now).total_seconds() / 60)))
    idem = f"{session_id}:{character_id}:{trigger.strftime('%Y%m%d%H%M')}:{content[:80]}"
    task_id = db.add_task(
        character_name=character_name, character_id=character_id, session_id=session_id,
        task_type="once", trigger_time=trigger.strftime("%Y-%m-%dT%H:%M:%S"),
        content=content, source="chat", idempotency_key=idem,
    )
    return {"task_id": task_id, "minutes": minutes, "content": content,
            "trigger_time": trigger.strftime("%Y-%m-%d %H:%M")}


_REMINDER_CLARIFY = "好呀，要我几点提醒你什么？"


def _reminder_clarify_result(session_id, character_id, raw) -> dict:
    """委托里抽不到明确事项：**不排程**，记成"待确认"，当场反问（备忘 §二.2）。

    ★ status 用 awaiting_confirm 而不是 pending——待确认就是待确认，不是已排程：
      这样它既不会被 get_open_loops（只取 pending）捞进 prompt，
      也不会被承诺兑现链路（_fetch_promises 只取 pending）当成到点任务。
    """
    try:
        _title = str(raw or "").strip()[:60]
        _dup = db.q(
            "SELECT id FROM open_loops WHERE session_id=? AND character_id=? "
            "AND category='reminder_confirm' AND status='awaiting_confirm' AND title=? LIMIT 1",
            (session_id, character_id, _title), fetch=True) or []
        if not _dup:
            db.add_open_loop(session_id, _title,
                             description="待确认的定时提醒（用户没给明确事项，已当场反问）",
                             category="reminder_confirm", importance=3, trigger_time="",
                             character_id=character_id, status="awaiting_confirm")
    except Exception as e:
        print(f"[定时提醒] 待确认记录失败(静默): {e}", flush=True)
    return {"need_clarify": True, "reply": _REMINDER_CLARIFY,
            "minutes": 0, "content": "", "trigger_time": ""}


def _is_recurring_commission(raw: str) -> bool:
    """这一轮是不是「长期/循环约定」（每天/每晚…）—— ★ 裁决①：一次委托一条记录。

    归属划分（2026-09-16 控制器裁决）：
      · tasks      = 用户**委托的定时提醒**（唯一落库处，到点提醒由它负责）；
      · open_loops = **AI 自己的承诺** + **长期/循环约定**（不做一次性到点提醒的落库）。
    所以循环约定**不在这里建一次性任务**（旧行为：前台建一次性 task、后台再存一条循环，
    用户既被重复提醒、循环语义也丢了），改由 ai_promise 存"循环约定原文"到 open_loops，
    由 idle_agent 的 `_recurring_hhmm` / `due_promises` 按天兑现。
    """
    try:
        from . import ai_promise as _ap
        return _ap.is_recurring_agreement(raw)
    except Exception:
        return False


def parse_timed_command(text: str, character_name: str = "default", session_id: str = "default",
                        character_id: str = "default") -> dict:
    """【降级兜底路径】解析中文定时委托并落库（**仅在模型层不可用时才会走到**）。

    ★ 2026-09-16 收紧：
      · 触发词只认显式祈使（提醒我/叫我/叫醒我/记得叫我/帮我记/喊我/催我），
        闲聊里的「到时候/告诉我/通知」不再构成定时委托；
      · 时间补 半(=30) / 一刻(=15) / 差X分 / 「X天后」「大后天」；
      · 事项不再取「时间词之后的残尾」，改为显式祈使之后的整段；
        抽不到明确事项 → **不建任务**，返回 need_clarify 交给上层当场反问。
    ★ 2026-09-16 裁决①：长期/循环约定（每天/每晚…）不在这里建一次性任务（归 open_loops）。

    返回 {task_id, minutes, content, trigger_time} / {need_clarify: True, reply, …} / None
    """
    if not text:
        return None
    raw = str(text).strip()
    if not _REMINDER_IMPERATIVE.search(raw):
        # 没有显式委托措辞 → 不是定时提醒（闲聊里的「两点半」「到时候」一律不建任务）
        return None
    if _is_recurring_commission(raw):
        print(f"[定时提醒] 长期/循环约定 → 不建一次性任务（归 open_loops/ai_promise）: "
              f"{raw[:30]!r}", flush=True)
        return None
    trigger = _parse_timestamp_regex(raw)
    if trigger is None:
        return None
    content = _extract_content_regex(raw)
    if not reminder_content_ok(content):
        print(f"[定时提醒] 兜底路径抽不到明确事项（不建任务，改为反问）: {raw[:40]!r}", flush=True)
        return _reminder_clarify_result(session_id, character_id, raw)
    return _create_reminder_task(trigger, content, character_name, session_id, character_id)


# ── 模型主判（主路径） ────────────────────────────────────────────────────
# 前台等待上限：模型层超时就降级正则，绝不让聊天卡在这里（用户还在等回复）。
_REMINDER_VERDICT_TIMEOUT = 8.0


def _trigger_from_verdict(verdict, raw: str = ""):
    """把模型给的时间（HH:MM + today/tomorrow/relative/YYYY-MM-DD）换算成 datetime。

    ★ 2026-09-16：模型偶尔回 12 小时制（「下午两点半」回 2:30、「今晚十二点」回 12:00）。
      判断权仍在模型，这里只做**校时**（能力块里就写着"系统只负责兜底与校时"）：
      句中有**紧贴时间词**的时段词时，按同一套时段/凌晨语义归一到 24 小时制
      （含「十二点 → 次日 0 点」）。句中没有时段词就不动手——模型可能是按上文推的。
    """
    try:
        from . import ai_promise as _ap
        _s = _ap._resolve_trigger_time(str(verdict.get("trigger_time") or ""),
                                       str(verdict.get("trigger_day") or ""),
                                       int(verdict.get("relative_minutes") or 0))
    except Exception:
        return None
    if not _s:
        return None
    try:
        _dt = datetime.strptime(str(_s), "%Y-%m-%d %H:%M")
    except Exception:
        return None
    _raw = str(raw or "")
    # hour <= 12：12:00 本身就是要判别的那一档（正午 or 次日零点），别把它漏掉
    if _dt.hour <= 12 and _PERIOD_BEFORE_TIME.search(_raw):
        _h2, _next = _resolve_hour_24(
            _dt.hour, _raw, has_day=bool(re.search(r"明早|明天|后天|今晚|今天", _raw)))
        if _next:
            _dt = _dt + timedelta(days=1)
        _dt = _dt.replace(hour=max(0, min(23, _h2)))
    return _dt


def _apply_reminder_verdict(verdict, raw, character_name, session_id, character_id):
    """模型判定是**权威**：它说是就按它给的内容/时间建任务，它说不是就绝不建。

    ★ 只有"抽不出事项/时间"时才回过头用正则补一次（别把每天约定误杀）：
      用户更正——判断对象是"闲聊 vs 委托/约定"，不是"能不能抽出事项"。
    ★ 2026-09-16：内容守卫对**模型给的原文**与洗完的结果都要过。清洗会把
      「[03:34] 用户：…」的时间戳前缀削掉，只守卫清洗结果的话，聊天回显会以
      「用户：…」的形态漏过去（实测建出过这种任务，正是 id=23 那一类）。
    ★ 2026-09-16 裁决①：模型判「是」但这是长期/循环约定（每天/每晚…）→ 也**不建一次性任务**，
      归 open_loops/ai_promise 域（判断权仍归模型，这里只决定"落到哪个库"）。
    """
    if not verdict.get("is_reminder"):
        print(f"[定时提醒] 模型判「不是委托/约定」→ 不建任务: {raw[:30]!r}", flush=True)
        return None
    if _is_recurring_commission(raw):
        print(f"[定时提醒] 模型判「是」但属长期/循环约定 → 不建一次性任务"
              f"（归 open_loops/ai_promise）: {raw[:30]!r}", flush=True)
        return None
    _raw_content = str(verdict.get("content") or "")
    content = _clean_reminder_content(_raw_content)
    if not (reminder_content_ok(_raw_content) and reminder_content_ok(content)):
        content = _extract_content_regex(raw)
    trigger = _trigger_from_verdict(verdict, raw)
    if trigger is None:
        trigger = _parse_timestamp_regex(raw)
    if trigger is None:
        print(f"[定时提醒] 模型判「是」但时间认不出（不建任务）: "
              f"t={verdict.get('trigger_time')!r} day={verdict.get('trigger_day')!r}", flush=True)
        return None
    if not reminder_content_ok(content):
        # 模型说是委托、却拿不到事项：当着用户反问，别猜也别静默丢（备忘 §二.2）
        if _REMINDER_IMPERATIVE.search(raw):
            return _reminder_clarify_result(session_id, character_id, raw)
        print(f"[定时提醒] 模型判「是」但无明确事项（不建任务，交回正常聊天）: {raw[:30]!r}",
              flush=True)
        return None
    return _create_reminder_task(trigger, content, character_name, session_id, character_id)


# ── 自然语言取消（★ 2026-09-17 新增：修「取消不掉」）────────────────────
# 真机事故：用户连续说「我叫她取消还取消不了」——因为取消**只有 API**
# （`/api/pc/task/delete`），聊天里说「取消」没有任何路径能删掉 pending 任务，
# 于是那条"明天 02:50 睡觉"只能一直挂着。
# 判据：句中同时出现「取消/别提醒/不用提醒/撤销/算了 + 提醒/任务/这个/那个」类词。
_REMINDER_CANCEL_COARSE = re.compile(
    r"取消|撤(?:销|掉)|别(?:再)?提醒|不用提醒|不要提醒|别叫|不用叫|不要叫|"
    r"算了别|删掉|去掉|作废|不用了"
)
_REMINDER_CANCEL_OBJ = re.compile(
    r"提醒|任务|定时|这个|那个|它|这条|刚才|睡|叫醒|叫我"
)


def looks_like_reminder_cancel(text: str) -> bool:
    """粗筛：这句话像不像在让我取消某个提醒（零成本）。

    ★ 两种形态都要认（第一版只认"动词+对象"，于是最常见的裸「取消」漏了）：
      · 有对象：「取消那个提醒」「别提醒我睡觉了」「撤销刚才那个」
      · 裸短句：「取消」「取消吧」「算了」—— 用户已经懒得说宾语了，
        这种情况由调用方结合"当前有没有 pending 提醒"来决定；
        本函数只保证**不把明显无关的句子**（如"取消订单的按钮在哪"）当成取消。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if not _REMINDER_CANCEL_COARSE.search(raw):
        return False
    if _REMINDER_CANCEL_OBJ.search(raw):
        return True
    # 裸短句：整句很短且没有别的实义内容 → 认作取消意图
    _short = re.sub(r"[\s，。！？~～、,.!?]", "", raw)
    return len(_short) <= 6


def _pending_tasks(session_id: str, character_id: str) -> list:
    try:
        rows = db.q(
            "SELECT id, trigger_time, content, task_type FROM tasks "
            "WHERE session_id=? AND character_id=? AND status='pending' "
            "ORDER BY trigger_time ASC LIMIT 20",
            (session_id, character_id), fetch=True) or []
        return [dict(r) for r in rows]
    except Exception:
        return []


def cancel_pending_reminders(session_id: str, character_id: str, raw: str = "") -> dict:
    """把本会话的 pending 提醒取消掉。返回 {cancelled: n, items: [...]}。

    ★ 语义选择：用户说「取消」而没说取消哪一条时，**取消全部 pending**
      （真机上用户明确说过"取消不掉"；留一条没取消掉的比多取消一条伤害大得多，
      而且提醒本身是可重建的轻量物）。说了具体内容（"别提醒我睡觉"）时按关键词匹配。
    """
    items = _pending_tasks(session_id, character_id)
    if not items:
        return {"cancelled": 0, "items": []}

    # 关键词匹配：从用户话里挑出可能与任务内容重合的词
    kw = []
    for w in ("睡", "觉", "吃", "药", "起床", "上班", "开会", "生日", "游戏", "电话", "喝水"):
        if w in str(raw or ""):
            kw.append(w)
    target = items
    if kw:
        matched = [t for t in items if any(k in str(t.get("content") or "") for k in kw)]
        if matched:
            target = matched

    done = []
    for t in target:
        try:
            _ok = db.cancel_task(int(t["id"]), session_id, character_id)
            # cancel_task 返回真假不一，落库后再核一次状态，别谎报
            row = db.q("SELECT status FROM tasks WHERE id=?", (int(t["id"]),), fetch=True) or []
            if row and str(row[0]["status"]) != "pending":
                done.append(t)
        except Exception as e:  # noqa: BLE001
            print(f"[定时提醒] 取消失败 id={t.get('id')}: {e}", flush=True)
    print(f"[定时提醒] 用户要求取消 → 已取消 {len(done)}/{len(target)} 条: "
          f"{[t['id'] for t in done]}", flush=True)
    return {"cancelled": len(done), "items": done}


async def resolve_timed_reminder(text: str, character_name: str = "default",
                                 session_id: str = "default", character_id: str = "default",
                                 ai_reply: str = "") -> dict:
    """定时提醒的唯一入口：**模型主判 → 正则兜底**（2026-09-16 误触发修复 A 方案）。

    流程：
      1) 粗筛：不像定时委托的句子直接返回 None（不为闲聊花模型调用）；
      2) 粗筛命中 → 调 ai_promise 的 LLM 提取器（与承诺链路同一个 _SYSTEM prompt，
         口径一致：用户委托 / AI 自己的承诺 / 双方约定 / 行为承诺）：
           · 它说「不是」→ 返回 None（**权威**：哪怕正则能匹配也不建任务）；
           · 它说「是」 → 用**它给的** content/时间建任务（不再用正则的残尾抽取）；
           · 模型层不可用（无 Key / 超时 / 异常 / 解析失败）→ 走 3)；
      3) 兜底：parse_timed_command（触发词已收紧为显式祈使；抽不到事项则当场反问）。

    返回 None / {need_clarify: True, reply: …} / {task_id, minutes, content, trigger_time}
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    # ★ 2026-09-17：**先判取消**，再判新建。
    #   真机事故里用户连说几次"取消"，而这之前没有任何自然语言取消路径 ——
    #   句子进不了新建分支（粗筛不过），于是什么都不发生，任务一直挂着。
    #   放在最前面是刻意的：一句话既像取消又像新建时，"取消"优先（用户此刻的意图是止损）。
    #
    #   ★ 修（回归用例抓到）：原先"像取消但没 pending 可取消"时会**掉到下面的粗筛**，
    #     而裸「取消」过不了粗筛（没有"提醒/几点"这类词）→ 直接 return None。
    #     于是「取消」在**没有** pending 时什么都不做（可接受），
    #     但逻辑上更糟的是：`looks_like_reminder_cancel` 明确认了它是取消意图，
    #     代码却没有按取消处理 —— 判定与执行不一致。
    #     现在：确认是取消意图就**在取消分支里收口**，不再往下走新建判定。
    if looks_like_reminder_cancel(raw):
        res = cancel_pending_reminders(session_id, character_id, raw)
        if res.get("cancelled"):
            return {"cancelled": res["cancelled"], "items": res["items"],
                    "reply": f"好，那 {res['cancelled']} 条提醒我撤掉了，不吵你。"}
        # 没有对应的 pending：交回正常聊天（她可以自然回一句"没有要取消的呀"），
        # 但**绝不再往下走建任务分支** —— 避免"取消 X"被当成"提醒我 X"建出来。
        print(f"[定时提醒] 用户意图是取消，但没有可取消的 pending: {raw[:30]!r}", flush=True)
        return None
    if not _REMINDER_COARSE.search(raw):
        return None
    verdict = None
    try:
        from . import ai_promise as _ap
        verdict = await asyncio.wait_for(
            _ap.extract_reminder_verdict(raw, ai_reply=ai_reply,
                                         character_id=character_id,
                                         session_id=session_id),
            timeout=_REMINDER_VERDICT_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[定时提醒] 模型判定超时({_REMINDER_VERDICT_TIMEOUT:.0f}s) → 降级正则: {raw[:30]!r}",
              flush=True)
        verdict = None
    except Exception as e:
        print(f"[定时提醒] 模型判定失败 → 降级正则: {e}", flush=True)
        verdict = None
    if verdict is not None:
        return _apply_reminder_verdict(verdict, raw, character_name, session_id, character_id)
    print(f"[定时提醒] 模型层不可用 → 走正则兜底: {raw[:30]!r}", flush=True)
    return parse_timed_command(raw, character_name, session_id=session_id, character_id=character_id)


def build_reminder_reply(result, style: str = "app") -> str:
    """把 resolve_timed_reminder 的结果渲染成给用户的一句话。

    ★ 确认文案**只在这里生成一处**：之前三个调用点各拼一份 %d分钟后…
      模板，正则抽出残尾（「半吧」）时就被原样发给了用户。
      need_clarify（没听清事项）走反问句，永远不会拼出「我会提醒你「半吧」」。
    """
    if not result:
        return ""
    if result.get("need_clarify"):
        return str(result.get("reply") or _REMINDER_CLARIFY)
    _tpl = ("好啦，%d分钟后（%s）我会提醒你「%s」，到时候见～" if str(style) == "qq"
            else "好啦，%d分钟后（%s）我会发「%s」给你，等着哦～")
    return _tpl % (int(result.get("minutes") or 0), result.get("trigger_time") or "",
                   result.get("content") or "")


# ══════════════════════════════════════════════════════════════════════════
# ★ 2026-09-16 用**她自己的话**说，而不是套模板（用户要求）
#   用户原话：「她用自己的话（模型生成、带人设）说，而不是模板」。
#   原来两句话都是拼死的固定句：
#     · 提醒确认  → build_reminder_reply 的「好啦，%d分钟后（%s）我会发「%s」…」；
#     · 改称呼后  → parse_identity_command 的「好呀，我记住了～称呼：X，以后我就按这个来。」
#   现在改成项目里惯用的「小 strict prompt + 宽容解析」（同 ai_promise 提取器那套）：
#   让模型用她的人设说这一句，但**事实由代码校验**（分钟数 / 触发时刻 / 事项 / 新称呼），
#   模型说错、超时、没 Key、返回垃圾 → 一律退回上面那句模板，
#   所以永远不会出现"编错时间""漏了事项""用户收不到回应"。
#   成本：只有**真的建了提醒 / 真的改了设定**时才多一次短调用（max_tokens 120）。
# ══════════════════════════════════════════════════════════════════════════

_VOICE_HARD_TIMEOUT = 5.0     # 前台硬上限（≤6s）：用户还在等这句话，绝不能卡住聊天
_VOICE_MAX_CHARS = 80         # 一句话的长度上限（超了当废话 → 退模板）
_VOICE_JSON_RE = re.compile(r"\{[\s\S]*\}")
_VOICE_MIN_RE = re.compile(r"(\d+)\s*分钟?")
_VOICE_HOUR_RE = re.compile(r"(\d+)\s*个?\s*小时")
_VOICE_CLOCK_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
# 舞台提示（括号里的动作神态）：只在括号里出现这些动作词时才算，别把正常括号话误判
_VOICE_STAGE_RE = re.compile(r"[（(]([^（()）]{1,20})[)）]")
_VOICE_STAGE_WORDS = ("笑", "叹", "停", "点头", "摇头", "歪头", "眨", "抿", "皱眉", "耸肩",
                      "轻声", "小声", "顿", "沉默", "想了想", "低头", "抬头", "抱", "伸手")

_VOICE_RETRY_HINT = (
    "\n\n【上一句不合格，请重说】你上一句里的时间或事项跟上面的事实对不上，"
    "或者没有原样带上那几段事实。只按事实重说**一句**，仍然只输出 JSON。\n")


def _voice_profile(character_name: str = "", character_id: str = "") -> tuple:
    """读人设三件套（角色名 / 称呼 / 性格），失败给默认 —— 与 agent/loop._char_profile 同口径。"""
    try:
        cfg = character_manager.get_character_any(character_id or "") or {}
        name = str(cfg.get("character_name") or cfg.get("name") or character_name or "AI").strip() or "AI"
        call_user = str(cfg.get("call_user") or "你").strip() or "你"
        personality = str(cfg.get("personality") or "").strip()[:200]
        return name, call_user, personality
    except Exception:
        return (character_name or "AI", "你", "")


def _voice_line_clean(raw) -> str:
    """把模型输出洗成「一句话」：宽容解析 JSON → 取第一行 → 去 markdown/列表符。

    ★ 宽容：模型常常包 ```json 围栏、或前后多一句废话（ai_promise._parse 同款处理）。
    ★ 只取第一行：多行的一律只剩第一行——第一行不是合格的话时会被事实校验打回，
      不会出现"把舞台提示当回复发出去"。
    """
    s = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""
    m = _VOICE_JSON_RE.search(s)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = None
        if isinstance(data, dict):
            for k in ("reply", "say", "text", "content", "回复", "说"):
                _v = data.get(k)
                if isinstance(_v, str) and _v.strip():
                    s = _v.strip()
                    break
    s = re.sub(r"```[a-zA-Z]*", "", s).strip()
    line = ""
    for ln in s.split("\n"):
        if ln.strip():
            line = ln.strip()
            break
    line = re.sub(r"^(?:[-*•>]+|\d+[.、)])\s*", "", line)
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"""^["'「『]?(?:reply|say|说|回复)["'」』]?\s*[:：]\s*""", "", line, flags=re.I)
    for _ch in ("**", "__", "`", "~~"):
        line = line.replace(_ch, "")
    return line.strip()


def _voice_fact_problems(line: str, must_have=None, minutes=None, clock: str = "",
                         forbid=None) -> list:
    """校验这句话与事实是否一致，返回问题清单（空 = 通过，可以发出去）。

    校验的是**事实**，不是文风：缺事实、多出别的时间/事项、markdown、多行、舞台提示
    都算不合格 → 调用方要么重问一次，要么退回模板。
    """
    problems = []
    s = str(line or "")
    if not s.strip():
        return ["空话"]
    if "\n" in s or "\r" in s:
        problems.append("多行")
    if len(s) > _VOICE_MAX_CHARS:
        problems.append("太长(%d字)" % len(s))
    if re.search(r"[*`#]", s):
        problems.append("含 markdown")
    for _p in _VOICE_STAGE_RE.findall(s):
        if any(w in _p for w in _VOICE_STAGE_WORDS):
            problems.append("疑似舞台提示：%s" % _p)
    for bad in (forbid or []):
        b = str(bad or "").strip()
        if b and b in s:
            problems.append("出现已被换掉的旧说法：%s" % b)
    for want in (must_have or []):
        w = str(want or "").strip()
        if w and w not in s:
            problems.append("缺事实：%s" % w)
    if minutes is not None:
        _mins = int(minutes)
        _found = [int(x) for x in _VOICE_MIN_RE.findall(s)]
        if _mins not in _found:
            problems.append("没写分钟数 %d（句中 %r）" % (_mins, _found))
        for x in _found:
            if x != _mins:
                problems.append("分钟数矛盾：句中 %d ≠ 事实 %d" % (x, _mins))
        for h in _VOICE_HOUR_RE.findall(s):
            if int(h) * 60 != _mins:
                problems.append("小时口径矛盾：%s小时 ≠ %d分钟" % (h, _mins))
    _c = str(clock or "").strip()
    if _c:
        if _c not in s:
            problems.append("没写触发时刻 %s" % _c)
        else:
            _want_hm = (int(_c[-5:-3]), int(_c[-2:]))
            for h, m in _VOICE_CLOCK_RE.findall(s):
                if (int(h), int(m)) != _want_hm:
                    problems.append("时刻矛盾：%s:%s ≠ %02d:%02d"
                                    % (h, m, _want_hm[0], _want_hm[1]))
    return problems


async def _ask_voice_line(prompt: str) -> str:
    """要一句人设口吻的话。模型层不可用（无 Key/超时/异常/空）→ 返回 ""（调用方退模板）。

    ★ chat_once 在函数内 import：这样验收脚本可以打桩 backend.deepseek_api.chat_once
      （模块级 import 会把函数名早绑定，打桩就失效了）。
    """
    try:
        key = config.chat_key()
    except Exception:
        key = ""
    if not key:
        print("[口吻] 无可用 Key → 直接用模板句", flush=True)
        return ""
    try:
        model = str(config.get("CURRENT_CHAT_MODEL") or config.selected_model() or "").strip()
    except Exception:
        model = ""
    try:
        from .deepseek_api import chat_once as _chat_once
        raw = await asyncio.wait_for(
            _chat_once(model, [{"role": "user", "content": prompt}], key,
                       temperature=0.6, max_tokens=120,
                       hard_timeout=_VOICE_HARD_TIMEOUT),
            timeout=_VOICE_HARD_TIMEOUT)
        return str(raw or "").strip()
    except asyncio.TimeoutError:
        print(f"[口吻] 生成超时({_VOICE_HARD_TIMEOUT:.1f}s) → 直接用模板句", flush=True)
        return ""
    except Exception as e:
        print(f"[口吻] 生成失败 → 直接用模板句: {e}", flush=True)
        return ""


def _reminder_voice_prompt(result, style: str, name: str, call_user: str, personality: str) -> str:
    """提醒确认句的 strict prompt：人设 + 三段事实（分钟/时刻/事项）+ 一句话硬要求。"""
    _minutes = int(result.get("minutes") or 0)
    _clock = str(result.get("trigger_time") or "")
    _content = str(result.get("content") or "")
    _how = "提醒" if str(style) == "qq" else "发消息"
    return (
        f"你是「{name}」，{call_user}的伴侣。你刚答应帮 {call_user} 记一件事，"
        f"现在要用**你自己的话**回一句话确认（到点你会{_how}给他）。\n"
        f"【你的人设】{personality or '真诚、自然'}\n"
        f"【你平时称呼他】{call_user}\n"
        "【这一轮的事实（一个字都不许改，也不许编别的时间或别的事）】\n"
        f"· 从现在起：{_minutes}分钟后\n"
        f"· 触发时刻：{_clock}\n"
        f"· 到点要做的事：{_content}\n"
        "【要求】\n"
        "1. 只回**一句话**（不超过 40 字），像平时聊天那样带你的语气，别像客服；\n"
        f"2. 必须原样包含这三段事实：「{_minutes}分钟后」「{_clock}」「{_content}」；\n"
        "3. 不要 markdown、不要换行、不要括号里的动作神态、不要解释你在做什么；\n"
        '4. 严格只输出 JSON：{"reply":"你要说的那一句话"}\n'
    )


async def build_reminder_reply_voiced(result, style: str = "app", character_name: str = "default",
                                      character_id: str = "default",
                                      session_id: str = "default") -> str:
    """确认话术：**她自己的话**（模型生成、带人设）优先，事实由代码校验，对不上退模板。

    返回一定是一句可发的话（空 result 返回空串）：
      · need_clarify（没听清事项、没建任务）→ 原样返回反问句，**不花模型调用**；
      · 模型层不可用（无 Key/超时/异常/空）→ 立刻退模板（不重试，别让用户等）；
      · 模型这句话缺事实或与事实矛盾 → **重问一次**（把不合格回告它）；还错 → 退模板。
    无论走哪条路，发出去的话里"分钟数 + 触发时刻 + 事项"都必须与建的任务完全一致。
    """
    if not result:
        return ""
    _tpl = build_reminder_reply(result, style)
    if result.get("need_clarify"):
        return _tpl
    _minutes = int(result.get("minutes") or 0)
    _clock = str(result.get("trigger_time") or "")
    _content = str(result.get("content") or "")
    if not _content or not _clock:
        # 事实不全就没什么可校验的，直接模板（模板本身就是这两段事实的载体）
        return _tpl
    name, call_user, personality = _voice_profile(character_name, character_id)
    _prompt = _reminder_voice_prompt(result, style, name, call_user, personality)
    for _attempt in (1, 2):
        raw = await _ask_voice_line(_prompt if _attempt == 1 else _prompt + _VOICE_RETRY_HINT)
        if not raw:
            return _tpl                       # 模型层不可用 → 立刻退模板
        line = _voice_line_clean(raw)
        _bad = _voice_fact_problems(line, must_have=[_content], minutes=_minutes, clock=_clock)
        if not _bad:
            return line
        print(f"[口吻] 第{_attempt}次不合事实({_bad}) → "
              f"{'重问一次' if _attempt == 1 else '退模板句'}: {line[:40]!r}", flush=True)
    return _tpl


def _identity_voice_prompt(ident: dict, name: str, call_user: str, personality: str) -> str:
    """「我记住了」的 strict prompt：人设 + 这一轮要记住的事实清单。"""
    _v = ident.get("voice") if isinstance(ident.get("voice"), dict) else {}
    _topic = str(_v.get("topic") or "").strip() or "、".join(_v.get("must") or [])
    _must = "」「".join(str(x) for x in (_v.get("must") or []) if str(x).strip())
    return (
        f"你是「{name}」，{call_user}的伴侣。{call_user}刚刚教了你一条关于你自己的设定，"
        "你要用**你自己的话**回一句话表示记住了（像平时那样，别像系统提示）。\n"
        f"【你的人设】{personality or '真诚、自然'}\n"
        f"【你平时称呼他】{call_user}\n"
        f"【这一轮你要记住的（一个字都不许改）】{_topic}\n"
        "【要求】\n"
        "1. 只回**一句话**（不超过 40 字），自然、带你的语气；\n"
        f"2. 必须原样包含：「{_must}」；\n"
        "3. 不要 markdown、不要换行、不要括号里的动作神态、不要复述「设定/记录/系统」这类词；\n"
        '4. 严格只输出 JSON：{"reply":"你要说的那一句话"}\n'
    )


async def voice_identity_reply(ident, character_name: str = "default",
                               character_id: str = "default", session_id: str = "default") -> str:
    """「我记住了」也用她自己的话说；模型不可用或说错 → 退回 parse_identity_command 的原模板。"""
    if not ident:
        return ""
    _tpl = str(ident.get("reply") or "")
    _v = ident.get("voice") if isinstance(ident.get("voice"), dict) else {}
    _must = [str(x).strip() for x in (_v.get("must") or []) if str(x).strip()]
    _forbid = [str(x).strip() for x in (_v.get("forbid") or []) if str(x).strip()]
    if not _must:
        return _tpl                           # 没有具体事实（如只调人设关键词）→ 模板，不花调用
    name, call_user, personality = _voice_profile(character_name, character_id)
    _prompt = _identity_voice_prompt(ident, name, call_user, personality)
    for _attempt in (1, 2):
        raw = await _ask_voice_line(_prompt if _attempt == 1 else _prompt + _VOICE_RETRY_HINT)
        if not raw:
            return _tpl
        line = _voice_line_clean(raw)
        _bad = _voice_fact_problems(line, must_have=_must, forbid=_forbid)
        if not _bad:
            return line
        print(f"[口吻] 记住了/第{_attempt}次不合事实({_bad}) → "
              f"{'重问一次' if _attempt == 1 else '退模板句'}: {line[:40]!r}", flush=True)
    return _tpl


# ---------- 偏好指令解析（早晚安开关等） ----------

_PREF_PATTERNS = [
    (re.compile(r"(给我|跟我|对我)?\s*(开|打开|开启|恢复).{0,5}(早安|早上好|早安推送)", re.IGNORECASE), {"MORNING_ENABLED": True}),
    (re.compile(r"(以后|每天|以后每天).{0,5}(跟我说|对我说|叫我|喊我|发给我).{0,4}(早安|早上好)", re.IGNORECASE), {"MORNING_ENABLED": True}),
    (re.compile(r"(以后|每天|以后每天)\s*(早上|早晨|早安).{0,6}(叫我|喊我|发|跟我说|说)", re.IGNORECASE), {"MORNING_ENABLED": True}),
    (re.compile(r"(每天|以后)\s*(早安|早上好|起床).{0,4}(发|说|叫)", re.IGNORECASE), {"MORNING_ENABLED": True}),
    (re.compile(r"(不用|别|不要|取消).{0,4}(早安|早上叫我|早晨叫我)", re.IGNORECASE), {"MORNING_ENABLED": False}),
    (re.compile(r"(别|不要)\s*发\s*早安", re.IGNORECASE), {"MORNING_ENABLED": False}),
    (re.compile(r"(给我|跟我|对我)?\s*(开|打开|开启|恢复).{0,5}(晚安|晚安推送)", re.IGNORECASE), {"NIGHT_ENABLED": True}),
    (re.compile(r"(以后|每天|以后每天).{0,5}(跟我说|对我说|叫我|喊我|发给我).{0,4}(晚安|睡前晚安)", re.IGNORECASE), {"NIGHT_ENABLED": True}),
    (re.compile(r"(以后|每天|以后每天)\s*(晚上|睡前|晚安).{0,6}(叫我|喊我|发|跟我说|说)", re.IGNORECASE), {"NIGHT_ENABLED": True}),
    (re.compile(r"(每天|以后)\s*(晚安|睡前).{0,4}(发|说|跟我说)", re.IGNORECASE), {"NIGHT_ENABLED": True}),
    (re.compile(r"(不用|别|不要|取消).{0,4}(晚安|睡前叫我)", re.IGNORECASE), {"NIGHT_ENABLED": False}),
    (re.compile(r"(别|不要)\s*发\s*晚安", re.IGNORECASE), {"NIGHT_ENABLED": False}),
]


def parse_preference_command(text: str) -> dict:
    """解析用户偏好指令，返回 {patch: {...}, reply: str} 或 None"""
    if not text:
        return None
    text = str(text).strip()
    for pat, patch in _PREF_PATTERNS:
        if pat.search(text):
            for k, v in patch.items():
                config.set(k, v)
            key = list(patch.keys())[0]
            enabled = patch[key]
            label = "早安" if "MORNING" in key else "晚安"
            reply = "好呀，以后每天%s我都叫你～" % label if enabled else "知道啦，以后不发%s了。" % label
            return {"patch": patch, "reply": reply}
    return None


def parse_voice_night_command(
    text: str, session_id: str = "default", character_id: str = "default"
) -> dict:
    """解析语音晚安控制：单次索要、每晚必发、恢复随机、关闭语音（不关闭文字晚安）。"""
    raw = str(text or "").strip()
    if not raw or not re.search(r"(?:语音.{0,5}晚安|晚安.{0,5}语音|用声音.{0,5}晚安)", raw):
        return None

    sid = str(session_id or "default").strip() or "default"
    cid = str(character_id or "default").strip() or "default"
    mode_key = f"voice_night_mode:{sid}:{cid}"

    if re.search(r"(?:不要|取消|关闭|别|不用(?!每次)).{0,6}(?:语音.{0,4}晚安|晚安.{0,4}语音)", raw):
        db.kv_set(mode_key, "off")
        return {"action": "off", "reply": "好，文字晚安还会保留，以后不自动发语音晚安了。"}

    if re.search(r"(?:偶尔|随机|看心情|不用每次).{0,8}(?:语音.{0,4}晚安|晚安.{0,4}语音)", raw) \
            or re.search(r"(?:语音.{0,4}晚安|晚安.{0,4}语音).{0,8}(?:偶尔|随机|看心情|不用每次)", raw):
        db.kv_set(mode_key, "auto")
        return {"action": "auto", "reply": "好呀，我会看我们的好感度和当晚的心情，偶尔用声音跟你说晚安。"}

    if re.search(r"(?:以后|每天|每晚|天天).{0,10}(?:语音.{0,4}晚安|晚安.{0,4}语音)", raw):
        db.kv_set(mode_key, "always")
        return {"action": "always", "reply": "好，以后每晚我都尽量亲口跟你说晚安。"}

    # “今晚/睡前”在夜间窗口前先预约；普通“给我发个语音晚安”则立即发送。
    now = datetime.now()
    deferred = bool(re.search(r"今晚|今天晚上|睡前|晚上再", raw)) and now.hour < 20
    if deferred:
        date_key = now.strftime("%Y-%m-%d")
        db.kv_set(f"voice_night_force:{date_key}:{sid}:{cid}", "1")
        return {"action": "once", "send_now": False, "reply": "好啊，今晚我亲口跟你说晚安，等我。"}
    return {"action": "once", "send_now": True, "reply": "好啊，正想亲口跟你说。等我一下～"}


# ---------- 身份指令解析（用户给AI设定身份，自动记录到角色档案） ----------

_IDENTITY_PATTERNS = [
    # 年龄：你今年X岁 / 你X岁
    (re.compile(r"你(?:今年|现在|的年龄是)?\s*(\d{1,3})\s*岁", re.IGNORECASE), "age", lambda m: m.group(1)),
    # 职业：你的职业是X / 你是做X的（去掉宽泛的"你是X"避免误判）
    (re.compile(r"你(?:的职业是|是做)\s*([^\s，。！？,.!?]{2,10})(?:的|师|生|员|家)?", re.IGNORECASE), "occupation", lambda m: m.group(1)),
    # 生日：你的生日是X月X日
    (re.compile(r"你(?:的生日是|生日是?)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", re.IGNORECASE), "birthday", lambda m: m.group(1) + "月" + m.group(2) + "日"),
    # 喜欢：你喜欢X
    (re.compile(r"你(?:喜欢|爱好是|的爱好是)\s*([^\s，。！？,.!?]{2,15})", re.IGNORECASE), "likes", lambda m: m.group(1)),
    # 口头禅：你的口头禅是X
    (re.compile(r"你(?:的口头禅是|总说|常说)\s*[「\"']?([^」\"'，。！？,.!?]{1,10})", re.IGNORECASE), "catchphrase", lambda m: m.group(1)),
]

# 称呼指令：以后叫我XX / 你可以叫我XX
# ★ 称呼抽取：必须有「叫/称呼/喊」这个动词，且紧邻要设定的称呼。
#   旧版把「叫」写成了可选前缀之一 (?:以后|你可以|你就|叫我)，捕获组又是
#   「任意 1-8 个非标点字符」，于是「每天下班你就一直陪我」也会被匹配：
#   前缀命中"你就"，捕获组抓到"一直陪我"，门闩"你就"也满足 ——
#   「一直陪我」就这样被当成称呼写进 call_user，AI 之后拿它称呼用户。
#   现在「叫/称呼/喊」是必需动词，普通句子不再命中。
#   顺序：先试带语气尾缀的（能顺带把"吧/好了"剥掉），再试通用式；
#   反过来会让「以后叫我宝宝吧」抓成"宝宝吧"。
_CALL_PATTERNS = [
    re.compile(r"(?:叫|称呼|喊)\s*(?:我|人家)?\s*"
               r"([^\s，。！？,.!?]{1,8})\s*(?:就行|好了|吧|呗)", re.IGNORECASE),
    re.compile(r"(?:以后|之后|从此|以后你就|你就|你可以|请|麻烦)?\s*"
               r"(?:叫|称呼|喊)\s*(?:我|人家)?\s*"
               r"[「\"'【]?([^」\"'】，。！？,.!?]{1,8})", re.IGNORECASE),
]

# 称呼动词（门闩用）：整句话里没有这类动词，就不该动称呼
_CALL_VERB = re.compile(r"(叫|称呼|喊)")

# 语气尾缀，抓取后统一剥掉（"宝宝吧" -> "宝宝"）
_CALL_TAIL = re.compile(r"(就行|好了|吧|呗|哦|啦|哈|呀|嘛|呢)$")

# 明显不是称呼的短语（前缀）：动词/副词短语，不可能是昵称。
# 「一直陪我」「照顾我」「陪着我」都是动词短语。
_NOT_A_CALLNAME = re.compile(
    r"^(一直|总是|经常|永远|好好|老是|多|少|先|再|就|还|也|都|又|"
    r"去|来|能|会|可以|应该|要|想|别|不要|不许|不准|"
    r"陪|帮|看|等|说|做|给|照顾|保护|喜欢|爱|记得|知道|明白|告诉|提醒|等着|跟着)"
)
# 任意位置命中即判为垃圾。历史污染值「我就按这个来」「不要说想你」
# 都是从 AI 回复模板或负向指令里串出来的，根本不是称呼。
_BAD_CALLNAME_ANY = re.compile(r"(记住|这个来|就按这个|按这个来|以后我就|说想你|想你)")

# ★ 数量词不是称呼：一声/两次/三下/几句/几遍 等。
#   「你再叫一声」的"一声"被 _CALL_PATTERNS 抓出来后，必须在这里拦掉，
#   否则 call_user 被写成"一声"，AI 之后就用"一声"称呼用户。
#   只拦「数字/几/多 + 量词」这种确定的数量词结构，不碰"宝贝""小名"这类真昵称。
_CALLNAME_QUANTIFIER = re.compile(
    r"^[一两二三四五六七八九十百千万几多]\s*(声|次|下|句|遍|回|口|个|只|条|天|年|岁|分|分钟|小时|块|元|米|公里|斤|两)$"
)

# 自称指令：你以后自称XX / 你叫XX吧
_SELF_PATTERNS = [
    re.compile(r"你(?:以后|以后就)?\s*(?:自称|叫自己|名字叫)\s*[「\"']?([^」\"'，。！？,.!?]{1,8})", re.IGNORECASE),
    re.compile(r"你叫\s*([^\s，。！？,.!?]{1,8})\s*(?:吧|好了)", re.IGNORECASE),
]

# 关系指令：我们是恋人 / 做我女朋友吧
_RELATION_PATTERNS = [
    re.compile(r"我们是\s*(恋人|情侣|好朋友|挚友|师徒|兄妹|姐弟|家人)", re.IGNORECASE),
    re.compile(r"做我\s*(女朋友|男朋友|老婆|老公|恋人)", re.IGNORECASE),
]

# 性格/语气指令（追加到 personality）
_PERSONALITY_PATTERNS = [
    (re.compile(r"你要?\s*(温柔|体贴|傲娇|高冷|活泼|开朗|内向|成熟|稳重|毒舌|幽默|可爱|软萌|黏人|独立)", re.IGNORECASE), lambda m: "性格" + m.group(1)),
    (re.compile(r"说话要?\s*(温柔|活泼|严肃|随意|正式|简短|啰嗦)", re.IGNORECASE), lambda m: "说话" + m.group(1)),
]

# ✅ 负向禁止指令（别叫宝贝 / 不要说想你）：之前完全没处理，现在补上
_NEG_CALL_PATTERNS = [
    re.compile(r"(别|不要|不准|不许)\s*(再|老|总是|一直)?\s*叫\s*(我)?\s*[「\"']?([^」\"'，。！？,.!?]{1,8})", re.IGNORECASE),
    re.compile(r"(以后|之后)?\s*(别|不要|不准)\s*(再|老|总是|一直)?\s*说\s*[「\"']?([^」\"'，。！？,.!?]{1,8})", re.IGNORECASE),
]
_NEG_FORBID_PATTERNS = [
    re.compile(r"(禁止|别|不要|不准)\s*(再|老|总是|一直)?\s*(用|说|提|做|叫)\s*([^\s，。！？,.!?]{1,10})", re.IGNORECASE),
]


# 泛词黑名单：这些是"泛指/疑问词"，不是具体称呼。理解层偶尔会把
# "名称/名字"这类泛词误当成称呼（如"你要叫我宝宝或者老公又或者叫我名字"），
# 这里一律拦下，宁可不改称呼也不要记错。
_CALL_GENERIC = ("名称", "名字", "名", "称呼", "叫法", "怎么称呼", "什么", "啥")

# 真人昵称一般不超过 6 字（旧版正则分支就是 8→6 收紧过的），放宽只会招来垃圾
_CALL_MAX_LEN = 6


def _valid_callname(val) -> str:
    """称呼合法性 —— **唯一判据**，LLM 语义分支与正则兜底分支共用。

    ★ 2026-09-16：以前这套守卫只挂在正则兜底分支上，理解层（LLM）给出的
    call_setting.call 被**原样**写进 call_user —— 于是角色卡里留下了
    「我就按这个来」这种垃圾（用户实测 角色配置\\骨子.config.json 的 call_user
    就是这个值）。它的出处就在本函数下游：身份回复模板结尾那句
    「…以后我就按这个来。」被用户复述/回灌后，理解层把它判成"用户在设定称呼"，
    6 个字又刚好过长度闸门。同类污染值还有「一声」（量词）、「一直陪我」（动词短语）。
    返回空串 = 不许写。
    """
    val = str(val or "").strip()
    if not val or len(val) > _CALL_MAX_LEN:
        return ""
    if val in _CALL_GENERIC:
        return ""
    if _NOT_A_CALLNAME.search(val):
        return ""
    if _BAD_CALLNAME_ANY.search(val):
        return ""
    if _CALLNAME_QUANTIFIER.search(val):
        return ""
    return val


def parse_identity_command(text: str, character_name: str = "default",
                           session_id: str = "default", character_id: str = "default",
                           intent: dict = None) -> dict:
    """解析用户给AI设定身份的指令，自动更新角色配置，返回 {updated: bool, reply: str}
    session_id / character_id：人格状态落库的隔离键（★ 修复：不再把角色名当 session_id 用）"""
    if not text:
        return None
    text = str(text).strip()
    updates = {}
    forbidden = []
    field_names = {"age": "年龄", "occupation": "职业", "birthday": "生日", "likes": "喜好", "catchphrase": "口头禅",
                   "call_user": "称呼", "self_name": "自称", "relationship": "关系", "personality": "人设"}

    # 基础身份字段
    for pat, field, extractor in _IDENTITY_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                val = extractor(m)
                if val:
                    updates[field] = val.strip()
            except Exception:
                continue

    # 称呼（★ 治本：优先用理解层 LLM 的权威判断，正则只做兜底。
    #   正则只认"叫"后面紧跟的字，理解不了"叫一声"是量词、"纠正你叫我"里
    #   "我"是代词——这类边界永远枚举不完，必须交给 LLM 用语义判断。）
    _cs = (intent or {}).get("call_setting") if isinstance(intent, dict) else None
    _cs = _cs if isinstance(_cs, dict) else {}
    _llm_setting = _cs.get("is_setting_call")
    _llm_call = str(_cs.get("call") or "").strip()

    # 泛词黑名单：这些是"泛指/疑问词"，不是具体称呼。理解层偶尔会把
    # "名称/名字"这类泛词误当成称呼（如"你要叫我宝宝或者老公又或者叫我名字"），
    # 这里一律拦下，宁可不改称呼也不要记错。
    # （清单与判据都搬到了模块级 _CALL_GENERIC / _valid_callname，两个分支共用一份）

    if _llm_setting is True and _llm_call:
        # 理解层明确识别出称呼 → 直接采用；但**先过同一套合法性守卫**。
        _ok_call = _valid_callname(_llm_call)
        if _ok_call:
            updates["call_user"] = _ok_call
        else:
            # 宁可不改称呼也不要记错：丢掉这个值，**不**退回正则乱猜
            print("[Identity] 理解层给出的称呼不可用，已丢弃: %r" % _llm_call, flush=True)
    elif _llm_setting is False:
        # 理解层明确判断"这不是在设定称呼"（量词/询问/代词），跳过正则
        pass
    elif _CALL_VERB.search(text):
        # 多重候选（"宝宝或者老公又或者名字"）→ 反问确认，绝不瞎猜一个
        if re.search(r"(?:或者|又或者|还是)", text):
            return {"updated": False,
                    "reply": "你想让我叫你哪个呀？跟我说一个就好，我这就记下来。"}
        # 理解层没给结论/失败 → 正则兜底（保留原有行为）
        for pat in _CALL_PATTERNS:
            m = pat.search(text)
            if m:
                val = _CALL_TAIL.sub("", m.group(1).strip()).strip()
                # 长度上限 8 -> 6：真人昵称一般不超过 6 字，放宽只会招来垃圾
                if _valid_callname(val):
                    updates["call_user"] = val
                    break

    # 自称
    for pat in _SELF_PATTERNS:
        m = pat.search(text)
        if m:
            val = m.group(1).strip()
            if val and len(val) <= 8:
                updates["self_name"] = val
                break

    # 关系
    for pat in _RELATION_PATTERNS:
        m = pat.search(text)
        if m:
            updates["relationship"] = m.group(1)
            break

    # 性格/语气（追加到 personality）
    personality_add = []
    for pat, extractor in _PERSONALITY_PATTERNS:
        m = pat.search(text)
        if m:
            personality_add.append(extractor(m))

    # ✅ 负向禁止指令（别叫宝贝 / 不要说想你）
    def _add_forbidden(bad):
        bad = (bad or "").strip()
        if bad and bad not in forbidden:
            forbidden.append(bad)

    for pat in _NEG_CALL_PATTERNS:
        m = pat.search(text)
        if m:
            # 取括号里最后捕获到的词（即被禁止的称呼/说法）
            bad = (m.group(m.lastindex) or "").strip()
            if bad:
                _add_forbidden(bad)
                # 若禁止的是当前称呼，且无正向新称呼，则回退默认"你"
                if "call_user" not in updates and bad:
                    updates["call_user"] = "你"
    for pat in _NEG_FORBID_PATTERNS:
        m = pat.search(text)
        if m:
            bad = (m.group(m.lastindex) or "").strip()
            _add_forbidden(bad)

    if not updates and not personality_add and not forbidden:
        return None

    # 更新角色配置（legacy）
    try:
        from . import character_manager, db
        existing = character_manager.get_character(character_name) or {}
        _prev_call = str(existing.get("call_user") or "").strip()   # ★ 更新前取：旧称呼不许再被说出来
        existing.update(updates)
        existing["character_name"] = character_name
        if personality_add:
            old_p = existing.get("personality", "")
            add_text = "；".join(personality_add)
            existing["personality"] = (old_p + "；" + add_text) if old_p else add_text
        character_manager.save_character(existing)

        # ✅ 写 personality_state 表（自适应层 final_personality 真正消费）
        psy_update = {}
        if forbidden:
            old_ps = db.get_personality_state(session_id, character_id) or {}
            old_forbid = old_ps.get("forbidden_phrases", "") or ""
            merged = old_forbid
            for f in forbidden:
                if f not in merged:
                    merged = (merged + "、" + f) if merged else f
            psy_update["forbidden_phrases"] = merged
        if personality_add:
            core = [x for x in personality_add if "性格" in x]
            style = [x for x in personality_add if "说话" in x]
            if core:
                psy_update["core_personality"] = "；".join(core)
            if style:
                psy_update["speaking_style"] = "；".join(style)
        if psy_update:
            db.update_personality_state(session_id, character_id, **psy_update)

        details = "、".join(field_names.get(k, k) + "：" + v for k, v in updates.items())
        if personality_add:
            details += ("、" if details else "") + "人设调整：" + "、".join(personality_add)
        if forbidden:
            details += ("、" if details else "") + "禁止表达：" + "、".join(forbidden)
        # ★ 2026-09-16：这句话改由她用**自己的话**说（main.py 调 voice_identity_reply），
        #   这里只交事实清单：新值必须原样出现；被换掉的旧称呼不许再出现；
        #   reply 保持原模板 —— 模型层不可用时的兜底，一字不改（用户绝不能收不到回应）。
        _must = [str(v).strip() for v in updates.values() if str(v).strip()]
        _forbid = []
        _new_call = str(updates.get("call_user") or "").strip()
        if _prev_call and _new_call and _prev_call != _new_call and len(_prev_call) >= 2:
            _forbid.append(_prev_call)
        return {"updated": True, "reply": "好呀，我记住了～" + details + "，以后我就按这个来。",
                "voice": {"must": _must, "forbid": _forbid, "topic": details}}
    except Exception:
        return None


# ---------- 轮次计数 + 自动提炼 ----------

def _advance_round_counter(session_id: str, character_id: str = "default"):
    """按用户+角色推进自动提取计数，防止不同角色共用一个轮次桶。"""
    key = f"rounds:{session_id}:{character_id}"
    cur = int(db.kv_get(key) or 0) + 1
    interval = int(config.get("AUTO_MEMORY_INTERVAL") or 4)
    if cur >= interval:
        db.kv_set(key, 0)
        return True
    db.kv_set(key, cur)
    return False


def should_extract_round(session_id: str, character_id: str = "default"):
    """只计数、不重复写聊天记录，供已经持久化消息的入口使用。"""
    return _advance_round_counter(session_id, character_id)


def count_round(session_id: str, user_text: str, assistant_text: str, character_id: str = "default"):
    """一轮 = 用户 + AI 各一次。计数达到 AUTO_MEMORY_INTERVAL 触发提炼后清零。"""
    # 消息由调用方统一持久化；这里只推进计数，防止同一轮写入两次。
    # ★ 顺带：正常聊了一轮 → 内驱力事件（无聊缓解/想念被安抚），纯 kv 计算零成本
    try:
        from . import drives as _drives
        _drives.on_user_round(session_id, character_id)
    except Exception:
        pass
    key = f"rounds:{session_id}:{character_id}"
    cur = int(db.kv_get(key) or 0) + 1
    interval = int(config.get("AUTO_MEMORY_INTERVAL") or 4)
    if cur >= interval:
        db.kv_set(key, 0)
        return True     # 触发提炼
    db.kv_set(key, cur)
    return False


# ★ 2026-09-15：最近一次 maybe_auto_extract 里真正执行成功的"成长族"更新器名字。
#   默认配置下恒为空列表（该族已砍）；体检脚本/测试据此断言，不靠读代码猜。
_last_growth_ran: list = []


async def _run_growth_updaters(session_id: str, character_id: str, msgs, user_message: str = "",
                               semantic=None):
    """「每轮自动成长」这一族更新器：情绪 / 关系 / AI状态 / 行为 / 人格（含五维）。

    ★ 2026-09-15 用户拍板砍掉（config.RELATIONSHIP_AUTO_UPDATE=false，默认关）：
      · 它们每轮都跑（多数还带 LLM 调用），2 天实测约 32 万 in / 9 万 out token；
      · 写出来的亲密度/信任/阶段反复被重打包重置，用户明确要求"这块有关的都停"。
      代码保留 + 一个开关，置 true 即可原样恢复旧行为（不改代码、可回滚）。

    返回**真正执行成功**的更新器名字列表：空列表 = 一个都没跑。
    体检/测试据此断言"成长族已停"，而不是靠读代码猜。
    """
    if not config.relationship_auto_update():
        return []

    ran = []

    try:
        from . import emotion_manager
        # ★ 修复：用关键字参数，避免与 update_emotion(session_id, messages, character_id) 位置错位
        await emotion_manager.update_emotion(
            session_id=session_id,
            messages=msgs,
            character_id=character_id
        )
        ran.append("emotion")
    except Exception as _e:
        print("[chat_logic] 情绪更新失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    try:
        await relationship_manager.update_relationship(
            session_id,
            msgs,
            user_message=user_message,
            semantic=semantic,
            character_id=character_id
        )
        ran.append("relationship")
    except Exception as _e:
        print("[chat_logic] 关系更新失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    try:
        from . import ai_state_manager
        # ★ 修复：用关键字参数，避免与 update_ai_state(session_id, messages, character_id)
        #   位置错位（之前把 msgs 列表传给了 character_id，导致 SQLite 绑定 list 报错）
        await ai_state_manager.update_ai_state(
            session_id=session_id,
            messages=msgs,
            character_id=character_id
        )
        ran.append("ai_state")
    except Exception as _e:
        print("[chat_logic] AI状态更新失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    try:
        from . import behavior_manager
        # analyze_behavior 签名是 (session_id, messages, character_id)。
        # 旧调用把 character_id 当成 messages，导致行为策略分析一直静默失败。
        await behavior_manager.analyze_behavior(
            session_id,
            msgs,
            character_id
        )
        ran.append("behavior")
    except Exception as _e:
        print("[chat_logic] 行为分析失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    try:
        from . import personality_manager
        await personality_manager.update_personality(
            session_id,
            character_id,
            msgs
        )
        ran.append("personality")

        # ★ 五维delta行为推算（不让LLM自评，从行为信号累计偏移）
        _fb = "neutral"
        try:
            from .feedback.learner import FeedbackLearner
            _fb = FeedbackLearner().get_recent_signal(
                session_id, character_id
            ).get("last_signal", "neutral")
        except Exception:
            pass
        _reply_len = len(str(msgs[-1].get("content", ""))) if msgs else 0
        await personality_manager.update_five_dim(
            session_id, character_id,
            feedback_signal=_fb,
            reply_length=_reply_len,
        )
        ran.append("five_dim")
    except Exception as _e:
        print("[chat_logic] 五维人格更新失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    return ran


def _should_scene_trim(semantic_state, user_text: str, character_id: str = "default") -> bool:
    """★ 2026-09-15（省 token ③）：这一轮要不要按场景裁掉"指令类积木"。

    原则：**只在"确实用不上"的轮次裁，宁可漏裁不可误裁**。
    裁的只是反馈/感知/格式/导演这类"指令性"积木；人设、记忆、关系、时间、能力、
    用户规则（别发语音/记住…）、防复读、危机干预**一律不裁**（见调用处逐个确认）。

    只有同时满足才裁：
      · 意图是轻量级（闲聊/打招呼/调侃/称赞/提问/自我披露）
      · 情绪非负面（不是难过/生气/焦虑那类需要接住情绪的轮次）
      · 紧急度低、没有危机词
      · 没有正在进行的结构化话题（约定/奖惩/亲密小游戏）
    其余情况（求助/抱怨/任务请求/关系信号/负面情绪/危机/有活跃话题）一律走完整提示词。
    """
    try:
        if not config.scene_prompt_trim():
            return False
    except Exception:
        return False

    text = str(user_text or "")
    # ① 极少数"明显是真实危险"的字样出现时，不做场景裁剪（宁可多留上下文）。
    #   ★ 2026-09-16：原来这里先调 crisis.detect_level（那个函数**根本不存在**，永远走 except），
    #   危机模块已按用户要求移除 —— 只留这张本地词表，且**只影响裁剪、不产生任何提示或卡片**。
    if any(w in text for w in ("不想活", "自杀", "自残", "活不下去", "死了算了")):
        return False

    if semantic_state is None:
        # 拿不到语义态就不裁（保守）
        return False

    try:
        from .semantic.schema import Intent
        _light = {Intent.CASUAL_CHAT, Intent.GREETING, Intent.PRAISE,
                  Intent.TEASE, Intent.QUESTION, Intent.SELF_DISCLOSURE}
        if semantic_state.intent not in _light:
            return False
    except Exception:
        return False

    try:
        from .semantic.schema import EmotionValence
        if semantic_state.emotion.valence != EmotionValence.NEUTRAL:
            # 只有"中性"才裁：正面情绪也要接住（她高兴你得跟着高兴），更别说负面
            return False
    except Exception:
        return False

    try:
        if float(getattr(semantic_state, "urgency", 0) or 0) > 0.5:
            return False
    except Exception:
        pass

    try:
        _tc = getattr(semantic_state, "topic_continuation", None)
        if _tc is not None and _tc.is_active():
            return False
    except Exception:
        pass

    return True


async def maybe_auto_extract(session_id: str, character_id: str = "default", user_message: str = ""):
    """后台静默更新长期记忆 + 用户画像 + 近期摘要 + 情绪状态 + 关系状态 + 关系事件桥接"""

    # Semantic Foundation：一次分析，产出 SemanticState
    semantic = None
    try:
        from .semantic.analyzer import SemanticAnalyzer
        from .semantic.context_builder import ContextBuilder

        # ★ 语义分析跟理解层走（角色卡 understanding_model > 全局 > 默认）
        analyzer = SemanticAnalyzer(model=config.understanding_model(character_id) or None)
        ctx_builder = ContextBuilder(max_turns=5)

        msgs_for_ctx = db.recent_messages(session_id, 10, character_id)
        context_str = ctx_builder.build(msgs_for_ctx)

        if user_message:
            semantic = await analyzer.analyze(user_message, context_str)
            print(f"[SemanticAnalyzer] intent={semantic.intent.value} emotion={semantic.emotion.primary_emotion}", flush=True)
    except Exception as e:
        print(f"[SemanticAnalyzer] 分析失败: {e}", flush=True)
        semantic = None

    # ★ 2026-09-15（省 token ②）：记忆 / 未完成事项 / 用户画像 三个抽取器读的是**同一批**
    #   messages，原来各发一次模型调用（2 天实测 171,866 + 145,373 in token）。现在合并成
    #   一次调用产出三段 JSON，再交给各模块原有的 apply_* 写入（写入逻辑一字未改）。
    #   合并失败/关闭 → _merged 为 None → 下面按旧口径分别调用（正确性优先）。
    #   ⚠️ 位置：必须在 `msgs = db.recent_messages(...)` **之后**。
    #   真机教训（2026-09-15 21:1x，打包版实测）：一开始放在上面，msgs 还没赋值 →
    #   `UnboundLocalError: cannot access local variable 'msgs'` → 每轮都静默回退旧口径，
    #   合并等于没做（日志里能看到 "回退旧口径" 字样）。教训：回归测试必须走
    #   maybe_auto_extract **整条**，只测 extract_merged 模块本身发现不了这种接线错误。
    msgs = db.recent_messages(
        session_id,
        20,
        character_id
    )

    _merged = None
    _merged_stat = {}
    # ★ result 必须先初始化：本函数结尾 `return result`，而合并路径**不会**给 result 赋值，
    #   否则真机每轮都会在函数末尾抛 UnboundLocalError（接线测试抓到的第二个同类 bug）。
    result = 0
    try:
        from . import extract_merged
        _merged = await extract_merged.extract_merged(msgs, session_id, character_id)
        if _merged:
            _merged_stat = await extract_merged.apply_merged(_merged, session_id, character_id)
            print("[MergedExtract] 一次调用完成三段抽取: 记忆入库 %d / 开环写入 %d / 画像 %s（raw=%d 字）"
                  % (_merged_stat.get("memories_in", 0), _merged_stat.get("open_loops_written", 0),
                     _merged_stat.get("profile"), _merged.get("_raw_len", 0)), flush=True)
    except Exception as _e:
        print("[MergedExtract] 失败，回退旧口径: %s: %s" % (type(_e).__name__, _e), flush=True)
        _merged = None

    if not _merged:
        result = await memory_manager.run_auto_extract(
            session_id,
            character_id
        )

    if not _merged:
        try:
            from . import profile_manager
            await profile_manager.extract_profile(
                msgs,
                session_id,
                character_id    # ★ 修复：透传 character_id（多角色画像隔离）
            )
        except Exception as _e:
            print("[chat_logic] 画像抽取失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    try:
        await summary_manager.maybe_update_summary(
            session_id, character_id
        )
    except Exception as _e:
        print("[chat_logic] 摘要更新失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    # ★ 2026-09-15：成长族（情绪/关系/AI状态/行为/人格）统一在 _run_growth_updaters 里，
    #   默认关闭；返回值记到模块级，供体检/测试断言"一个都没跑"。
    global _last_growth_ran
    _last_growth_ran = await _run_growth_updaters(
        session_id, character_id, msgs, user_message, semantic)

    if not _merged:
        try:
            from . import open_loop_manager
            # ★ 修复：用关键字参数，避免与 extract_open_loops(session_id, messages, character_id)
            #   位置错位（之前把 character_id 传给了 messages，导致 string indices 报错）
            await open_loop_manager.extract_open_loops(
                session_id=session_id,
                messages=msgs,
                character_id=character_id
            )
        except Exception as _e:
            print("[chat_logic] 开环管理失败(静默): %s: %s" % (type(_e).__name__, _e), flush=True)

    # ★ 2026-09-15：AI状态 / 行为分析 / 人格成长（含五维）三块已整体搬进
    #   _run_growth_updaters()（默认关闭），此处不再重复调用。

    # Relationship Event Bridge v1.0：关系事件桥接
    # 识别聊天中的关系事件，同步更新关系状态和关系记忆
    try:
        if user_message:
            from .companion.event_bridge import RelationshipEventBridge
            bridge = RelationshipEventBridge()
            await bridge.process(
                user_id=session_id,
                character_id=character_id,
                message=user_message
            )
    except Exception as e:
        print(f"[EventBridge] 处理关系事件失败: {e}", flush=True)

    # ★ 理解层 routing.record_relation：这句话对关系有推进（表白/吵架/感谢/承诺）
    #   时，额外落一条关系时间线事件，让"关系进展"可被回看（纪念日/回忆博物馆会读）。
    #   只做**追加**，不改动上面 bridge 的既有行为；读不到 routing 就不写。
    try:
        from .understanding import get_routing
        if user_message and (get_routing(session_id, character_id) or {}).get("record_relation"):
            db.add_timeline_event(
                session_id=session_id,
                character_id=character_id,
                event_type="relation_progress",
                title="关系进展",
                description=str(user_message)[:120],
                importance=6,
            )
    except Exception as e:
        print(f"[Routing] record_relation 时间线写入失败(静默): {e}", flush=True)

    # Memory Consolidation & Life Profile v1.0：长期记忆整理
    # 不每次聊天执行，使用概率触发（约10%），避免过于频繁
    try:
        from .profile_memory.consolidator import should_consolidate, run_consolidation
        if should_consolidate(session_id, character_id):
            await run_consolidation(session_id, character_id)
    except Exception as e:
        print(f"[MemoryConsolidation] 记忆整理失败: {e}", flush=True)

    # Behavior Predictor v2.0：按24小时冷却稳定更新，不再靠5%随机碰运气。
    try:
        import time as _time
        _behavior_key = f"behavior_analysis_ts:{session_id}:{character_id}"
        try:
            _last_behavior = float(db.kv_get(_behavior_key) or 0)
        except Exception:
            _last_behavior = 0.0
        _recent_for_behavior = db.recent_messages(session_id, 24, character_id)
        if len(_recent_for_behavior) >= 20 and _time.time() - _last_behavior >= 24 * 3600:
            db.kv_set(_behavior_key, str(_time.time()))
            from .behavior.predictor import run_behavior_analysis
            await run_behavior_analysis(session_id, character_id, messages=_recent_for_behavior)
    except Exception as e:
        print(f"[BehaviorPredictor] 行为分析失败: {e}", flush=True)

    # Feedback Learning Layer v2.0：隐式反馈采集 + 学习结果写回
    try:
        from .feedback.collector import collect_implicit_feedback
        from .feedback.learner import FeedbackLearner

        recent = db.recent_messages(session_id, 4, character_id)
        if len(recent) >= 2:
            ai_reply = None
            user_response = None
            for m in recent:
                if m.get("role") == "assistant" and ai_reply is None:
                    ai_reply = m.get("content", "")
                elif m.get("role") == "user" and user_response is None:
                    user_response = m.get("content", "")

            if ai_reply and user_response:
                # 1. 采集隐式反馈（原逻辑不变）
                fb_result = collect_implicit_feedback(
                    session_id, character_id,
                    str(len(recent)), ai_reply, user_response
                )
                fb_type = fb_result.get("feedback_type", "continued")

                # ★ 2. 学习结果写回（缺口2核心修复）
                learner = FeedbackLearner()
                action  = learner.apply(fb_type)

                # 2-A. 记忆权重写回
                if action.get("memory_weight") in ("+", "update"):
                    try:
                        recent_mems = db.get_recent_memories(
                            session_id, character_id, limit=3
                        )
                        for mem in (recent_mems or []):
                            new_importance = learner.update_memory_weight(mem, fb_type)
                            db.update_memory_importance(
                                mem.get("id"), new_importance
                            )
                    except Exception as e:
                        print(f"[FeedbackWrite] 记忆权重写回失败: {e}", flush=True)

                # 2-B. 主动推送权重写回（存 kv，供 scheduler 读）
                if action.get("proactive") in ("increase", "decrease"):
                    try:
                        _delta = +0.1 if action["proactive"] == "increase" else -0.15
                        _cur   = db.get_relationship_field(
                            session_id, character_id, "proactive_weight"
                        )
                        _cur = float(_cur) if _cur is not None else 0.5
                        _new   = max(0.1, min(1.0, _cur + _delta))
                        db.set_relationship_field(
                            session_id, character_id, "proactive_weight", _new
                        )
                    except Exception as e:
                        print(f"[FeedbackWrite] 主动推送权重写回失败: {e}", flush=True)

                # 2-C. 风格避免信号写回 personality_state（让人格引擎感知）
                if action.get("style") == "avoid":
                    try:
                        _ps = db.get_personality_state(session_id, character_id) or {}
                        _avoid = int(_ps.get("avoid_style_count", 0) or 0) + 1
                        db.update_personality_state(
                            session_id, character_id,
                            avoid_style_count=_avoid,
                            last_negative_reply=str(ai_reply)[:100]
                        )
                    except Exception as e:
                        print(f"[FeedbackWrite] 风格写回失败: {e}", flush=True)

    except Exception as e:
        # ★ 2026-09-17 自愈：feedback.db 的表原本只在 main.py 启动时建（main.py:137）。
        #   没走那条启动链的入口（测试夹具、脚本、新人格验收）会撞上
        #   `no such table: ai_feedback`，而这里只打一行日志 → 反馈学习静默停摆。
        #   遇缺表就地建一次并重试。
        if "no such table" in str(e):
            try:
                from .feedback.database import init as _fb_init
                _fb_init()
                print("[FeedbackLearning] 检测到缺表，已就地初始化（下一轮起正常）", flush=True)
            except Exception as _e2:  # noqa: BLE001
                print(f"[FeedbackLearning] 缺表且初始化失败: {_e2}", flush=True)
        else:
            print(f"[FeedbackLearning] 反馈学习失败: {e}", flush=True)

    # Personal Knowledge Graph v2.0：知识图谱抽取（每积累10条新消息必抽一次，精准触发）
    try:
        from .knowledge_graph.database import get_extraction_cursor
        from .knowledge_graph.extractor import run_knowledge_extraction
        import asyncio as _asyncio

        # 读增量游标
        cursor     = get_extraction_cursor(session_id, character_id)
        last_id    = cursor.get("last_msg_id", 0) if cursor else 0

        # 数最新消息里有多少条比last_id新
        recent     = db.recent_messages(session_id, 30, character_id)
        new_count  = sum(1 for m in recent if int(m.get("id", 0)) > last_id)

        # ★ 每积累10条新消息必抽一次（不用概率，精准触发）
        if new_count >= 10:
            # 异步非阻塞，不拖慢对话响应
            get_loop().create_task(
                run_knowledge_extraction(
                    session_id, character_id, recent,
                    last_extracted_id=last_id
                )
            )
    except Exception as e:
        # ★ 2026-09-17 自愈：KG 的表原本只在 main.py 启动时建（main.py:144）。
        #   任何**没走那条启动链**的入口（测试夹具、脚本、新人格验收、
        #   或将来某个只 import chat_logic 的进程）都会撞上
        #   `no such table: kg_extraction_cursor`，而这里只是打一行日志 ——
        #   结果知识图谱静默停摆。现在遇到缺表就地建一次并重试。
        if "no such table" in str(e):
            try:
                from .knowledge_graph import init_knowledge_graph
                init_knowledge_graph()
                print("[KnowledgeGraph] 检测到缺表，已就地初始化后重试", flush=True)
                try:
                    cursor = get_extraction_cursor(session_id, character_id)
                    last_id = cursor.get("last_msg_id", 0) if cursor else 0
                    recent = db.recent_messages(session_id, 30, character_id)
                    new_count = sum(1 for m in recent if int(m.get("id", 0)) > last_id)
                    if new_count >= 10:
                        get_loop().create_task(
                            run_knowledge_extraction(
                                session_id, character_id, recent,
                                last_extracted_id=last_id))
                except Exception as _e2:  # noqa: BLE001
                    print(f"[KnowledgeGraph] 自愈后仍失败: {_e2}", flush=True)
            except Exception as _e3:  # noqa: BLE001
                print(f"[KnowledgeGraph] 缺表且初始化失败: {_e3}", flush=True)
        else:
            print(f"[KnowledgeGraph] 抽取触发失败: {e}", flush=True)

    # Memory Reflection Layer v1.0：记忆反思
    # 不每次聊天执行，使用概率触发（约2%），避免过于频繁
    try:
        from .reflection.analyzer import update_reflection
        await update_reflection(session_id, character_id)
    except Exception as e:
        print(f"[Reflection] 记忆反思失败: {e}", flush=True)

    return result
