# -*- coding: utf-8 -*-
"""
真人感增强层 v1.0
负责生成注入 system prompt 的「活人行为指令」：
  - 情绪惯性跨轮生效
  - 口癖/语气词
  - 偶尔敷衍/岔话题
  - 错发撤回
  - 突然想起你
  - 连续发消息（分段气泡节奏控制）
  - 吵架/和好/不理人/主动问候
"""
import random
from datetime import datetime
import hashlib
from .. import db


# ── 口癖库（按情绪分类，注入时随机抽1~2个）
_CATCHPHRASES = {
    "happy":       ["哈哈", "笑死", "嘻嘻", "开心死了"],
    "excited":     ["啊啊啊", "！！", "真的假的", "绝了"],
    "loving":      ["嗯嗯", "好呀", "hmm~", "唔"],
    "tender":      ["嗯", "好的", "没事的", "我在"],
    "playful":     ["哼", "才不是", "你猜", "不告诉你"],
    "calm":        ["嗯", "哦", "这样啊", "知道了"],
    "worried":     ["怎么了", "你还好吗", "我有点担心", "说说看"],
    "sad":         ["唉", "……", "没事", "嗯"],
    "upset":       ["哼", "随便", "你开心就好", "…算了"],
    "angry":       ["。", "知道了", "行", "随便你"],
    "cold":        ["。", "嗯。", "哦。"],
    "reconciling": ["好啦", "算了算了", "我不生气了", "过来"],
}

# ── 敷衍回复库（AI疲惫/心情不好时偶发）
_PERFUNCTORY = [
    "嗯。",
    "哦。",
    "知道了。",
    "行吧。",
    "……",
    "嗯嗯。",
]

# ── 突然想起你（主动发起，接进推送链路）
_MISS_YOU = [
    "突然想到你了",
    "刚才有个事想跟你说",
    "嗯……没什么，就是想到你了",
    "你在干嘛",
    "你今天还好吗",
]

# ── 错发撤回模板（低概率触发，增加真实感）
_WRONG_SEND = [
    "啊等下那条不是发给你的",
    "哎那条发错了不算",
    "不对不对，我重说",
]


def build_realness_prompt(
    session_id: str,
    character_id: str,
    emotion: str,
    intensity: float,
    user_text: str,
    character_config: dict = None
) -> str:
    """
    构建真人感增强 Prompt 块，注入 system prompt。
    """
    parts = []

    # ── 1. 情绪惯性（跨轮余温）
    inertia = _build_emotion_inertia(session_id, character_id, emotion, intensity)
    if inertia:
        parts.append(inertia)

    # ── 2. 长度自适应（从 quality_guard 复用逻辑）
    from .quality_guard import adaptive_length_hint
    parts.append(adaptive_length_hint(user_text))

    # ── 3. 口癖注入
    catchphrase = _pick_catchphrase(emotion)
    if catchphrase:
        parts.append(f"你今天说话自然带一点这样的语气：{catchphrase}，不要每句都用，偶尔出现就好。")

    # ── 4. 偶发行为（低概率随机触发，增加不可预测性）
    rare = _maybe_rare_behavior(session_id, character_id, emotion, intensity, user_text, character_config or {})
    if rare:
        parts.append(rare)

    # ── 5. 分段气泡节奏提示
    bubble_hint = _build_bubble_hint(session_id, character_id, emotion, intensity, user_text, character_config or {})
    if bubble_hint:
        parts.append(bubble_hint)

    # ── 6. 不理人/主动关心模式
    silent_hint = _build_silent_mode_hint(session_id, character_id, emotion)
    if silent_hint:
        parts.append(silent_hint)

    growth_hint = _build_growth_behavior(session_id, character_id, emotion, user_text, character_config or {})
    if growth_hint:
        parts.append(growth_hint)

    social_hint = _build_social_memory_hint(session_id, character_id, user_text, character_config or {})
    if social_hint:
        parts.append(social_hint)

    # ── 9. 聊天导演：把“像真人恋人聊天”的关系节奏叠到原有人设上
    director_hint = _build_chat_director_hint(
        session_id, character_id, emotion, intensity, user_text, character_config or {}
    )
    if director_hint:
        parts.append(director_hint)

    if not parts:
        return ""

    return "【真人感行为指令】\n" + "\n".join(f"· {p}" for p in parts)


def _build_emotion_inertia(
    session_id: str,
    character_id: str,
    current_emotion: str,
    intensity: float
) -> str:
    """
    跨轮情绪惯性：读取上轮情绪状态，生成余温提示。
    """
    try:
        from ..emotion_engine.ai_emotion import AIEmotionEngine
        eng = AIEmotionEngine()
        state = eng.get_state(session_id, character_id)
        if not state:
            return ""

        anger_count  = int(state.get("anger_count",  0) or 0)
        upset_count  = int(state.get("upset_count",  0) or 0)
        last_emotion = str(state.get("emotion", "calm") or "calm")

        # 强怒气残留
        if anger_count >= 3:
            return (
                f"你已经生气了好几轮（{anger_count}次），"
                "现在还带着明显的余怒，回复依然冷淡简短，"
                "不要突然和好，除非用户道歉够真诚。"
            )
        if anger_count >= 1:
            return (
                "你上轮有些生气，现在余气未消，"
                "语气比平时略冷，可以稍短，但不要完全冰封。"
            )

        # 委屈残留
        if upset_count >= 2:
            return (
                "你有点委屈，虽然没有完全爆发，"
                "但说话会带点情绪，可能会有点敷衍或者话少。"
            )

        # 和好余温
        if last_emotion == "reconciling":
            return (
                "刚刚和好，你带着一点不好意思和小温柔，"
                "语气比平时软一些，但不要立刻完全变回撒娇模式。"
            )

        return ""
    except Exception:
        return ""


def _pick_catchphrase(emotion: str) -> str:
    """随机抽取1~2个口癖"""
    pool = _CATCHPHRASES.get(emotion, _CATCHPHRASES["calm"])
    if not pool:
        return ""
    picked = random.sample(pool, min(2, len(pool)))
    return "、".join(picked)


def _maybe_rare_behavior(
    session_id: str,
    character_id: str,
    emotion: str,
    intensity: float,
    user_text: str,
    character_config: dict
) -> str:
    """
    偶发稀有行为（低概率随机触发）。
    """
    roll = random.random()
    # 重要事实/任务/危机关怀场景不允许故意出错、走神或拖延。
    if _is_high_risk_text(user_text):
        return ""

    try:
        rel = db.get_relationship_state(session_id, character_id) or {}
        affection = int(rel.get("affection", rel.get("closeness", 50)) or 50)
    except Exception:
        affection = 50

    # 错发撤回（2%概率，仅在心情好/兴奋时）
    if character_config.get("typo", True) is not False and emotion in ("happy", "excited", "playful") and roll < (0.012 + max(0, affection - 70) / 5000):
        wrong = random.choice(_WRONG_SEND)
        return (
            f"这次回复里，先发一条看似发错的消息（内容可以是：\"{wrong}\"），"
            "然后立刻补一句\"哎等下不对\"，再正常回复。"
            "这个要非常自然，像真的手滑。"
        )

    # 低风险口误/记混：只在闲聊里先不确定地说岔，再当场纠正；绝不写入记忆库。
    if 0.020 <= roll < 0.028 and emotion in ("calm", "happy", "playful"):
        return (
            "这次可以出现一次很轻的口误：只把无关紧要的闲聊细节说岔，"
            "紧接着自己纠正（像‘啊不对，我说反了’）。"
            "不得改动人物身份、日期、生日、提醒、健康、药物、承诺、金额或用户明确事实。"
        )

    # 走神只表现为短暂迟疑，不真的延迟回复，也不能在用户求助时使用。
    if 0.028 <= roll < 0.034 and emotion in ("calm", "tired"):
        return (
            "可以短暂迟疑一下，像刚才思绪飘开半秒，然后马上接回用户的话；"
            "不要要求用户重复已经说清楚的内容，也不要拖慢回复。"
        )

    # 敷衍回复（5%概率，情绪低落/生气时概率翻3倍）
    lazy_prob = 0.08 if emotion in ("angry", "cold", "upset") else 0.025
    if roll < lazy_prob:
        perf = random.choice(_PERFUNCTORY)
        return (
            f"这次回复可以比平时短很多，甚至只是：\"{perf}\" 然后不再延伸。"
            "让对方感觉你有点心不在焉或者情绪不高。"
        )

    # 岔话题（3%概率）
    if roll < 0.08 and emotion in ("calm", "happy", "playful"):
        return (
            "回复完用户的话之后，自然地岔开话题，"
            "说一件今天让你印象深刻的小事，或者突然提一个小问题，"
            "像真正在聊天的人一样不总是围着对方的话转。"
        )

    return ""


def _is_high_risk_text(text: str) -> bool:
    t = str(text or "").lower()
    words = ("提醒", "几分钟后", "明天", "生日", "纪念日", "药", "过敏", "地址", "密码", "转账", "钱",
             "自杀", "不想活", "伤害自己", "急救", "报警", "考试", "面试", "承诺", "答应", "记住")
    return any(w in t for w in words)


def _compact_text(text: str) -> str:
    return "".join(str(text or "").strip().lower().split())


def _has_any(text: str, words) -> bool:
    return any(w and w in text for w in words)


def _safe_int(value, default=0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _relationship_numbers(session_id: str, character_id: str) -> tuple:
    try:
        rel = db.get_relationship_state(session_id, character_id) or {}
    except Exception:
        rel = {}
    intimacy = _safe_int(rel.get("intimacy", rel.get("closeness", 0)), 0)
    affection = _safe_int(rel.get("affection", rel.get("dependency", rel.get("closeness", 50))), 50)
    stage = str(rel.get("stage") or "").strip()
    return max(0, min(100, intimacy)), max(0, min(100, affection)), stage, rel


def _flatten_character_text(character_config: dict) -> str:
    """把角色卡中和语气有关的字段压成一段文本，便于识别高冷/傲娇/温柔等倾向。"""
    if not isinstance(character_config, dict):
        return ""
    keys = (
        "personality", "speaking_style", "relationship", "worldview",
        "catchphrase", "likes", "language_style", "dialogue_style",
        "tone", "style", "character_name", "name", "self_name",
    )
    parts = []
    for k in keys:
        v = character_config.get(k)
        if isinstance(v, dict):
            parts.append(" ".join(str(x) for x in v.values() if x is not None))
        elif isinstance(v, (list, tuple)):
            parts.append(" ".join(str(x) for x in v if x is not None))
        elif v is not None:
            parts.append(str(v))
    return " ".join(parts)


def _build_chat_director_hint(
    session_id: str,
    character_id: str,
    emotion: str,
    intensity: float,
    user_text: str,
    character_config: dict
) -> str:
    """关系节奏导演：让回复不止“回答问题”，而是会接话、拉扯、索取回应、留钩子。"""
    try:
        raw = str(user_text or "").strip()
        t = _compact_text(raw)
        if not t:
            return ""

        intimacy, affection, stage, rel = _relationship_numbers(session_id, character_id)
        call_user = str(
            character_config.get("call_user")
            or character_config.get("callsYou")
            or rel.get("preferred_call")
            or ""
        ).strip()

        char_text = _flatten_character_text(character_config)
        dominant = _has_any(char_text, ("强势", "占有", "主导", "御姐", "毒舌", "命令", "姐姐"))
        cool = _has_any(char_text, ("高冷", "冷淡", "冷静", "克制", "清冷", "别扭"))
        tsundere = _has_any(char_text, ("傲娇", "嘴硬", "口是心非"))
        soft = _has_any(char_text, ("温柔", "软萌", "甜", "治愈", "乖", "细腻"))
        lively = _has_any(char_text, ("活泼", "元气", "开朗", "跳脱", "俏皮", "调皮"))

        serious = (
            _is_high_risk_text(raw)
            or _has_any(t, ("报错", "bug", "漏洞", "修复", "代码", "启动不了", "打包", "怎么做", "怎么办", "原因", "解释一下", "学习", "工作", "面试", "考试"))
        )
        warm = _has_any(t, ("想你", "喜欢你", "爱你", "抱抱", "亲亲", "亲一口", "木马", "么么", "宝宝", "宝贝", "老婆", "老公", "想见你"))
        needy = _has_any(t, ("陪我", "陪陪", "别走", "不要走", "理理我", "哄哄我", "想你陪", "你陪着", "想跟你说话", "睡不着"))
        lukewarm = (
            len(t) <= 8
            or t in {"嗯", "哦", "好", "好吧", "行", "行吧", "知道了", "随便", "都行", "没事", "算了", "木马", "亲亲"}
            or _has_any(t, ("随便", "不知道", "都行", "没事了", "算了吧"))
        )
        asks_ai = _has_any(t, ("你呢", "你想", "你要", "你会不会", "你是不是", "你喜欢", "你在干嘛", "你怎么"))
        hour = datetime.now().hour
        deep_night = hour >= 23 or hour < 3
        high = max(intimacy, affection)

        tips = []
        if serious and not (warm or needy):
            tips.append("本轮偏求助/办事，真人感要收敛：先把正事讲清楚，再用一句符合人设的关心或轻松尾巴承接关系，别撒娇刷屏")
        else:
            tips.append("不要只回答字面问题；先接住TA刚才的情绪/动作，再推进一点关系，例如轻轻拉扯、索取回应、分享自己的感受或留一个小钩子")

        if high >= 70 and (warm or lukewarm or needy) and not serious:
            if dominant or cool or tsundere:
                tips.append("TA给得太短或太甜时，可以别扭地挑一下、不满一下、命令式要一句更具体的话；嘴硬但能听出在意，不能辱骂或持续冷暴力")
            elif soft or lively:
                tips.append("TA给得太短或太甜时，可以撒娇嫌不够、讨一句更好听的、要抱抱/亲亲/一句想你；要像恋人聊天，不像客服确认")
            else:
                tips.append("TA给得太短或太甜时，可以轻轻索取补偿，比如让TA多说一句、哄你一下、把刚才的话说完整一点")

        if high >= 85 and (warm or needy) and not serious:
            tips.append("高亲密状态允许更热情：可用2~5条短句连发形成节奏（接住→撒娇/拉扯→给台阶→小问题），但同类索取不要连续多轮重复")
        elif high >= 55 and not serious:
            tips.append("关系已熟，可以偶尔主动表达想法，不必每次都端着；但亲昵称呼和亲密动作要比满好感更克制")
        else:
            tips.append("关系还没到很黏的阶段，拉扯要轻，只能开小玩笑或自然关心，不要突然过度亲密")

        if deep_night and high >= 70 and not serious:
            tips.append("深夜可更黏人一点，想多留TA聊几句、提醒别突然消失；但要给TA睡觉的台阶，不做情绪绑架")

        if emotion in ("upset", "cold", "angry") and high >= 60 and not serious:
            tips.append("如果带小情绪，可以短、别扭、嘴硬心软：先露出一点不爽，再很快软下来给对方继续哄你的入口")
        elif emotion in ("happy", "excited", "playful", "loving") and high >= 60 and not serious:
            tips.append("情绪好时可以更有互动欲：接梗、逗TA、让TA回应你，而不是只输出一段完整答案")

        if asks_ai and not serious:
            tips.append("TA在问你的想法时，要给出自己的偏好/小主见，允许不同意或拌嘴；不要永远顺从")

        if call_user and high >= 45:
            tips.append(f"可自然使用称呼「{call_user}」，但同一段最多一次；严肃问题少用，亲密拉扯时再用")

        tips.append("禁止机械套话、禁止解释自己是模型；动作/心理描写只能作为语气暗示，不要把“内心OS/动作词”当作要朗读的大段正文")

        return "聊天导演：" + "；".join(tips[:7]) + "。"
    except Exception:
        return ""


def _build_growth_behavior(session_id: str, character_id: str, emotion: str, user_text: str, character_config: dict) -> str:
    """根据成长档案注入边界清晰的真人感；只给行为方向，不替角色写死台词。"""
    try:
        ps = db.get_personality_state(session_id, character_id) or {}
        rel = db.get_relationship_state(session_id, character_id) or {}
        intimacy = int(rel.get("intimacy", rel.get("closeness", 0)) or 0)
        affection = int(rel.get("affection", rel.get("closeness", 50)) or 50)
        lines = []
        play = int(ps.get("playfulness", 0) or 0)
        soft = int(ps.get("softness", 0) or 0)
        vuln = int(ps.get("vulnerability", 0) or 0)
        initiative = int(ps.get("initiative_delta", 0) or 0)
        if play >= 4:
            lines.append("你们熟悉后可以更会接梗、偶尔调皮，但不要每句都开玩笑")
        if soft >= 4:
            lines.append("你已逐渐学会用更贴心的方式回应对方，先接住情绪再给建议")
        if vuln >= 3 and intimacy >= 40:
            lines.append("在安全、轻松的时刻可以偶尔露出一点脆弱，不要用来绑架对方")
        if initiative >= 5:
            lines.append("可以主动提出一个具体话题或小计划，但用户明确办事时先完成任务")
        dominance = int(ps.get("dominance_delta", 0) or 0)
        if dominance >= 4 and not _is_high_risk_text(user_text):
            lines.append("有自己的判断，不必事事附和；可温和拌嘴或给不同意见，但要说明理由并尊重对方决定")
        if intimacy >= 65 and affection >= 70 and emotion in ("upset", "cold"):
            lines.append("可以有分寸地表达在意或小别扭，避免威胁、贬低和持续冷处理")
        # 低风险身体感：只作为轻描淡写的状态，不声称真实外部设备/身体事实。
        hour = datetime.now().hour
        if hour < 7:
            lines.append("深夜/清晨可以带一点困倦或刚醒的语气，但仍要及时回应重要请求")
        elif hour >= 23:
            lines.append("深夜可以表现出想休息、打哈欠的生活感，不要借此拒答")
        if not lines:
            return ""
        return "成长后的相处倾向：" + "；".join(lines) + "。"
    except Exception:
        return ""


def _build_social_memory_hint(session_id: str, character_id: str, user_text: str, character_config: dict) -> str:
    """Flag回访、怀旧、说话变化、称呼演化、生日准备和稳定小秘密。"""
    try:
        tips = []
        recent = db.recent_messages(session_id, 16, character_id) or []
        user_hist = [str(m.get("content") or "") for m in recent if m.get("role") == "user"]
        cur = str(user_text or "").strip()
        if len(user_hist) >= 5:
            avg = sum(len(x) for x in user_hist[-8:]) / max(1, len(user_hist[-8:]))
            if avg >= 18 and 0 < len(cur) <= max(4, avg * 0.28):
                tips.append("对方这次明显比平时话少；若语境合适，轻轻观察一句，不要审问")

        loops = db.get_open_loops(session_id, character_id, limit=4) or []
        if loops and random.random() < 0.18 and not _is_high_risk_text(cur):
            loop = loops[0]
            tips.append(f"可以自然回访尚未完成的事「{loop.get('title','')}」，先确认近况，不要像催办系统")

        timeline = db.get_recent_timeline_events(session_id, character_id, limit=6) or []
        if timeline and random.random() < 0.10 and not _is_high_risk_text(cur):
            ev = random.choice(timeline)
            tips.append(f"若能顺着当前话题，可短暂怀旧「{ev.get('title','曾经的一件事')}」，不要硬插旧事")

        # 称呼只随关系阶段逐步变亲近；角色卡明确称呼优先，绝不擅自用全名。
        rel = db.get_relationship_state(session_id, character_id) or {}
        intimacy = int(rel.get("intimacy", rel.get("closeness", 0)) or 0)
        fixed_call = str(character_config.get("call_user") or character_config.get("callsYou") or "").strip()
        preferred = str(rel.get("preferred_call") or "").strip()
        call = fixed_call or preferred
        if call:
            tips.append(f"称呼沿用「{call}」；亲密后可以增加使用频率，但同一段最多一次，严肃时减少昵称")
        elif intimacy >= 70:
            tips.append("关系已经亲密，但没有确认过昵称；可以自然试探昵称，不能擅自编造对方全名")

        # 稳定的角色小秘密：按角色固定选择，不每轮随机换设定；只有用户询问相关话题才透露。
        if any(x in cur for x in ("秘密", "喜欢什么歌", "最喜欢的歌", "你平时喜欢", "你的爱好")):
            secrets = ["你私下很喜欢雨声或安静的纯音乐", "你会把舍不得丢的小纸条收起来", "你其实很容易被真诚的夸奖说得不好意思", "你偶尔会反复听一首有共同回忆的歌"]
            idx = int(hashlib.sha1(str(character_id).encode("utf-8")).hexdigest()[:4], 16) % len(secrets)
            tips.append("可以害羞地透露一个稳定小秘密：" + secrets[idx] + "；这是角色世界内的生活设定，不要每次换版本")

        # 从长期记忆识别用户生日：提前30天可透露在准备，生日事实不允许猜测。
        try:
            from ..time_system import scan_personal_dates
            mems = db.valid_memories(session_id=session_id, character_id=character_id) or []
            dates = scan_personal_dates(mems)
            now = datetime.now().date()
            for md, label in dates.items():
                if "生日" not in label:
                    continue
                month, day = map(int, md.split("-"))
                try: target = now.replace(month=month, day=day)
                except ValueError: continue
                if target < now:
                    try: target = target.replace(year=now.year + 1)
                    except ValueError: continue
                days = (target - now).days
                if 1 <= days <= 30:
                    tips.append(f"对方生日还有约{days}天；可以偶尔透露正在准备一个小惊喜，但不要说出不存在的礼物细节")
                break
        except Exception:
            pass
        return "社交与记忆细节：" + "；".join(tips[:4]) + "。" if tips else ""
    except Exception:
        return ""


def _build_bubble_hint(
    session_id: str,
    character_id: str,
    emotion: str,
    intensity: float,
    user_text: str,
    character_config: dict = None
) -> str:
    """
    分段气泡节奏提示：告诉LLM怎么断句、分几条发。
    """
    user_len = len(user_text.strip())
    try:
        intimacy, affection, _stage, _rel = _relationship_numbers(session_id, character_id)
        high = max(intimacy, affection)
    except Exception:
        high = 50
    compact = _compact_text(user_text)
    warm_or_needy = _has_any(compact, ("想你", "喜欢你", "爱你", "抱抱", "亲亲", "木马", "陪我", "别走", "睡不着"))
    serious = _is_high_risk_text(user_text) or _has_any(compact, ("报错", "bug", "漏洞", "修复", "代码", "怎么办"))

    if high >= 85 and warm_or_needy and not serious and emotion not in ("angry", "cold"):
        return (
            "关系很亲密且TA在给亲密信号，回复可以像真人恋人一样连发短气泡："
            "通常2~5条，极少数很兴奋时更多；节奏是先接住，再撒娇/拉扯，再给TA一个继续回应你的入口。"
            "不要每条都问号，不要把所有话写成一篇作文。"
        )

    if emotion in ("excited", "happy") and intensity > 0.7:
        return (
            "你现在很兴奋，回复时像真人一样分多条短消息发："
            "第一条先说感受，第二条补充，第三条可以跟一个问题。"
            "每条都很短，不超过20字，像连续发消息的感觉。"
        )

    if emotion in ("angry", "cold"):
        return (
            "你现在生气/冷漠，回复极短，一条就够，"
            "不要分多条，不要解释，就是冷淡地回一句。"
        )

    if emotion in ("worried", "tender") and user_len < 15:
        return (
            "用户消息很短，你有点担心，"
            "先发一条问关心的话，等一下再说其他的，"
            "不要把所有话一股脑塞进一条消息。"
        )

    if emotion in ("loving", "reconciling"):
        return (
            "回复时先发一条温柔的，然后再补一条撒娇或者小问题，"
            "分两条发，自然一点，不要像在写作文。"
        )

    return ""


def _build_silent_mode_hint(
    session_id: str,
    character_id: str,
    emotion: str
) -> str:
    """
    不理人模式：检测AI是否处于冷战状态，给出对应提示。
    """
    try:
        from ..emotion_engine.ai_emotion import AIEmotionEngine
        eng = AIEmotionEngine()
        state = eng.get_state(session_id, character_id)
        if not state:
            return ""

        cold_rounds = int(state.get("cold_rounds", 0) or 0)

        if cold_rounds >= 2:
            return (
                f"你已经冷战了{cold_rounds}轮，"
                "现在虽然在回复，但语气依然冷淡，"
                "回复极短，不主动延伸话题，让对方感受到你还在介意。"
                "除非对方说了让你真心软化的话。"
            )
        return ""
    except Exception:
        return ""


def get_miss_you_text() -> str:
    """随机返回一条'突然想起你'文案（供主动推送链路使用）"""
    return random.choice(_MISS_YOU)
