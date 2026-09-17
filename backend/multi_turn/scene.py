# -*- coding: utf-8 -*-
"""场景识别 + 递进骨架

多气泡的意义不该是"把同一件事换个说法重复几遍"，而是层层深入：
先锚定具体画面，再说感受，再连到现在，最后落到心意或行动上。

原实现只按「情绪 + 好感度」决定条数，话题大纲还要求 >=3 条才启用，
所以日常对话（calm 模板仅 2 条）几乎没有递进空间，容易变成
"回应一句 + 追问一句"就结束，或者换个说法再来一遍。

这里补上场景这一层：先识别场景，再给出该场景的递进层次，
生成器按层分配每条气泡的任务，让第二条天然比第一条更深一层。

任何异常都返回 None / 保守值，调用方静默降级为原有逻辑。
"""

# 按优先级排列，命中即止
SCENES = [
    {
        "id": "comfort",
        "name": "安慰",
        # 需要用户是在说自己，否则只是提到情绪词
        "self_ref": True,
        "keywords": ("难过", "伤心", "好累", "崩溃", "焦虑", "压力好大",
                     "痛苦", "孤独", "无助", "失败", "撑不住", "想哭",
                     "难受", "委屈", "睡不着"),
        "layers": (
            "承认他的感受（不说「别难过」，不追问原因）",
            "说一个你记得的、和他有关的温暖细节",
            "给一个极小的、现在就能做的动作（喝水/深呼吸/靠一会儿）",
            "表达陪伴，让他知道你在",
        ),
        "min_turns": 3,
        "max_turns": 4,
    },
    {
        "id": "recall",
        "name": "回忆/告白",
        "self_ref": False,
        "keywords": ("纪念", "回忆", "记得吗", "那时候", "第一次", "一路走来",
                     "喜欢你", "爱你", "告白", "在一起", "当时我们", "以前你"),
        "layers": (
            "锚定一个具体的画面（时间/地点/细节，越具体越好）",
            "说那个瞬间你真实的感受",
            "把那件事连到现在的你们",
            "落到一句心意或承诺上",
        ),
        "min_turns": 3,
        "max_turns": 5,
    },
    {
        "id": "low_mood",
        "name": "低落陪伴",
        "self_ref": False,
        "keywords": ("没意思", "不想动", "算了", "随便", "好烦", "没劲"),
        "layers": (
            "安静地在场，不追问怎么了",
            "告诉他不说话也没关系",
            "提议一个安静的陪伴方式（听歌/发呆/就这么待着）",
        ),
        "min_turns": 2,
        "max_turns": 4,
    },
    {
        "id": "cute",
        "name": "撒娇",
        "self_ref": False,
        "keywords": ("抱抱", "亲亲", "想你了", "陪我", "乖乖", "坏蛋",
                     "不理你了", "哄我", "摸摸"),
        "layers": (
            "先轻轻不满（哼 / 才想起我）",
            "说出你真正想要的（陪你 / 抱抱 / 听你说）",
            "软下来，露出口是心非的在意",
        ),
        "min_turns": 2,
        "max_turns": 3,
    },
    {
        "id": "upset",
        "name": "闹别扭",
        "self_ref": False,
        "keywords": ("滚", "烦死了", "闭嘴", "别烦我", "讨厌你", "无所谓",
                     "你走开", "不想理你"),
        "layers": (
            "先冷淡地回应一句",
            "说出你哪里不舒服（不说教、不人身攻击）",
            "留一个台阶，让他知道你还是在意",
        ),
        "min_turns": 2,
        "max_turns": 4,
    },
]

# 判定"在说自己"的第一人称
_SELF_PRONOUNS = ("我", "俺", "人家", "自己")
_SPLIT_RE = None


def _split_sentences(text):
    """按标点分句，用于判断「我」和情绪词是否落在同一句里。"""
    global _SPLIT_RE
    if _SPLIT_RE is None:
        import re
        _SPLIT_RE = re.compile(r"[，。！？、；\n…~～!?.,;]")
    return [s for s in _SPLIT_RE.split(text) if s]


def _is_self_reference(text, keywords):
    """用户是在说自己，还是只是提到某个情绪词。

    「我今天好累」→ 说自己；「他今天好累」→ 不是。
    """
    for seg in _split_sentences(text):
        for sp in _SELF_PRONOUNS:
            if sp in seg:
                for kw in keywords:
                    if kw in seg:
                        return True
    return False


def detect(user_text, emotion=""):
    """识别场景；未命中返回 None。任何异常都降级返回 None。"""
    try:
        text = str(user_text or "")
        if not text.strip():
            return None
        for scene in SCENES:
            kws = scene.get("keywords") or ()
            if not any(k in text for k in kws):
                continue
            # 安慰类要求"在说自己"，否则只是提到情绪词，交给后面的低落陪伴
            if scene.get("self_ref") and not _is_self_reference(text, kws):
                continue
            return scene
        return None
    except Exception:
        return None


# ── 日常闲聊兜底场景 ────────────────────────────────────────────────
# 刻意不进 SCENES 列表：detect() 遍历时遇到空 keywords 会直接跳过，
# 而且一旦进列表就可能抢掉上面 5 个具名场景（它们优先级更高但依赖关键词命中）。
# 改为由生成器在「一个具名场景都没命中」时显式取用，优先级天然排在末位。
# 日常闲聊（calm 模板只有 2 条）此前没有递进骨架，只能「回应 + 追问」，这里补上纵深。
CASUAL_SCENE = {
    "id": "casual",
    "name": "日常闲聊",
    "layers": (
        "接住用户刚说的这件事，给出你真实的反应（觉得好笑 / 意外 / 有共鸣都行）",
        "顺着这件事说出你自己的感受，或一段相关的经历",
        "补一个具体细节或画面，让这件事更实在",
        "自然地再往前走一步：联想到相关的小事，或说出你现在的念头",
        "自然收住；只有真的好奇时才轻轻追问一句",
    ),
    "min_turns": 3,
    "max_turns": 5,
}


def casual_scene() -> dict:
    """返回日常闲聊兜底场景；任何异常返回 {}，由调用方降级为无场景。"""
    try:
        return dict(CASUAL_SCENE)
    except Exception:
        return {}


def suggest_turns(scene, affection=50, intimacy=50, restrained=False):
    """场景命中的目标条数。

    仍然受好感度约束：关系浅时不硬凑长篇，关系深时才放开到场景上限。
    短消息/严肃话题等 restrained 场景一律收敛，避免啰嗦。
    """
    try:
        lo = int(scene.get("min_turns") or 2)
        hi = int(scene.get("max_turns") or lo)
        if restrained:
            return max(2, lo - 1)
        level = max(int(affection or 0), int(intimacy or 0))
        if level >= 85:
            return hi
        if level >= 70:
            return max(lo, hi - 1)
        return lo
    except Exception:
        return 2


def assign_layer(scene, turn_index, total_turns):
    """把递进层次分配到每条气泡上，保证层次单调递增、不回退。

    条数不超过层数时一一对应；条数更多时，多出来的都归最后一层——
    最后一层通常是心意/承诺/陪伴，多说一条比在开头原地打转更自然
    （按比例均分会让前两条落在同一层，开头就重复，正是要避免的情况）。
    """
    try:
        layers = list(scene.get("layers") or ())
        if not layers:
            return ""
        return layers[min(int(turn_index), len(layers) - 1)]
    except Exception:
        return ""
