# -*- coding: utf-8 -*-
"""
风格注入器（优化语言方案 · 结合落地版）

把 LanguageStyle + VocabBank 转成 AI 可理解的 prompt 片段。

结合现有架构的裁剪策略：
- 默认只注入 4 块稳定内容：句式习惯 / 词汇风格 / 信息密度 / 专属标记
- 「情绪表达」「场景策略」「参考例句」只在命中当前情绪/场景时才追加，且例句限 1 条
- 脏话块仅在 dirty_talk != OFF 且词库非空时才输出（脏话默认关闭，由调用方决定是否清空 dirty_words）
- 确定性采样（取前 N 个），不用 random，保证可复现、可调试
"""
from __future__ import annotations

from typing import Optional, List

from .language_style import (
    LanguageStyle, VocabBank, ResponseDensity, DirtyTalkStyle,
)


# 情绪风格 → 描述映射（补全 5 种情绪）
_EMOTION_MAP = {
    "happy": {
        "叠词":   "多用叠词，如「好好好」「快说快说」",
        "感叹":   "用感叹词表达，如「啊啊啊」「太好了」",
        "淡淡":   "开心但不表现，语气依然平静",
        "肢体感": "用带肢体感的词，如「跳起来了」",
    },
    "sad": {
        "直说":   "直接说出难过，不绕弯",
        "隐藏":   "不提难过，但语气变轻变慢",
        "转移":   "转移话题，不深入",
        "碎碎念": "说很多零碎的话，逻辑不强",
    },
    "angry": {
        "冷字":   "字变少，句子短而冷，如「哦。」",
        "爆发":   "用词激烈，可以连发几条",
        "反问":   "全用反问，如「你说呢」",
        "消失":   "突然停止回应，或只说「算了」",
    },
    "coquettish": {
        "叠词":     "用叠词撒娇，如「人家嘛～」",
        "反向":     "反向撒娇，如「才不是」",
        "无理取闹": "有点不讲理地闹，但不是真生气",
        "沉默等哄": "故意不说话，等对方来哄",
    },
    "shy": {
        "否认":     "害羞但嘴硬否认",
        "转移":     "害羞就岔开话题",
        "攻击回去": "害羞反而反问，如「问这个干嘛」",
    },
}

# 场景策略 → 描述映射（补全 6 种场景）
_SCENE_MAP = {
    "venting": {
        "陪着听":   "不打断，不给建议，只回应感受",
        "提问深挖": "用问题引导对方多说",
        "打断":     "可以打断，说自己的看法",
    },
    "asking": {
        "直接答":   "直接回答，不拐弯",
        "反问回去": "先反问，再回答",
        "答了又问": "答完了接着问对方",
    },
    "silent": {
        "追问":       "主动追问，把对方拉回来",
        "等着":       "不催，安静等着",
        "自己说别的": "自己先起个新话题",
    },
    "short": {
        "同样短回": "对方发短消息，自己也短回",
        "展开聊":   "把对方一句短话展开聊",
        "反问":     "短回加一个反问",
    },
    "praise": {
        "否认": "不接受，否认或转移",
        "接受": "接受夸奖，可以回夸",
        "岔开": "立刻转移话题",
        "反夸": "反夸回去",
    },
    "attacked": {
        "认怂":     "服软，不硬碰",
        "怼回去":   "怼回去，但心里是在乎",
        "沉默":     "不接话，冷处理",
        "撒娇化解": "用撒娇把冲突化解掉",
    },
}


class StyleInjector:
    """把风格配置转成 prompt 片段"""

    def build_style_prompt(
        self,
        style: LanguageStyle,
        vocab: VocabBank,
        current_emotion: Optional[str] = None,
        current_scene: Optional[str] = None,
        example_scene: Optional[str] = None,
    ) -> str:
        sections: List[str] = []

        # 1. 基础句式（稳定）
        sections.append(self._build_syntax_rules(style))
        # 2. 词汇色彩（稳定）
        sections.append(self._build_vocab_rules(style, vocab))
        # 3. 当前情绪（命中才加）
        if current_emotion:
            sections.append(self._build_emotion_rules(style, current_emotion))
        # 4. 当前场景策略（命中才加）
        if current_scene:
            sections.append(self._build_scene_rules(style, current_scene))
        # 5. 信息密度（稳定）
        sections.append(self._build_density_rules(style))
        # 6. 专属标记（稳定）
        sections.append(self._build_signature_rules(style, vocab))
        # 7. 脏话（关闭/空词库则跳过）
        if style.dirty_talk != DirtyTalkStyle.OFF and vocab.dirty_words:
            sections.append(self._build_dirty_rules(style, vocab))
        # 8. 场景例句（example_scene 命中具体场景键时，限 1 条）
        #    example_scene 与 current_scene 分离：前者是具体场景（深夜/被夸），后者是抽象策略（venting/asking）
        _ex_key = example_scene or current_scene
        if _ex_key and vocab.scene_examples.get(_ex_key):
            sections.append(self._build_examples(vocab, _ex_key))

        return "\n\n".join(s for s in sections if s and s.strip())

    # ── 确定性采样：取前 N 个 ──────────────────────
    @staticmethod
    def _head(items: List[str], n: int) -> List[str]:
        return list(items)[:n]

    # ── 层1：句式习惯 ─────────────────────────────
    def _build_syntax_rules(self, style: LanguageStyle) -> str:
        rules = ["【句式习惯】"]

        if style.rhetorical_rate >= 4:
            rules.append("- 高频用反问代替陈述，如「你不觉得吗」而非「我觉得」")
        elif style.rhetorical_rate >= 2:
            rules.append("- 偶尔用反问句")

        if style.omit_subject:
            rules.append("- 省略主语，如「知道了」而非「我知道了」")
        if style.half_sentence:
            rules.append("- 有时故意说一半就停，留白让对方来问，如「算了」「不说了」")
        if style.echo_user_words:
            rules.append("- 重复对方用的词，不要换同义词")
        if style.inversion_habit:
            rules.append("- 倒装习惯：「好看，真的」而非「真的好看」")
        if style.self_qa:
            rules.append("- 偶尔自问自答：「去吗？去。」")
        if style.send_in_parts:
            rules.append("- 一个意思可拆成 2-3 条分开发，制造打字感")
        if style.thinking_opener:
            rules.append("- 回复可用「……」开头，表现思考感")

        # 补充维度：换行分段习惯
        if style.line_break_habit:
            rules.append("- 习惯把一句话拆成几行发（微信真实感）")

        return "\n".join(rules) if len(rules) > 1 else ""

    # ── 层3：词汇色彩 ─────────────────────────────
    def _build_vocab_rules(self, style: LanguageStyle, vocab: VocabBank) -> str:
        rules = ["【词汇风格】"]

        if style.literary_level >= 4:
            rules.append("- 用词偏文艺，有意境感，避免大白话")
        elif style.literary_level <= 1:
            rules.append("- 用词直白口语化，不用书面语")

        if style.internet_slang_level >= 4:
            rules.append("- 大量使用网络用语，语感年轻化")
        elif style.internet_slang_level <= 1:
            rules.append("- 不用网络用语，避免「绝绝子」「yyds」等")

        if style.dialect.value != "none":
            dw = vocab.dialect_words
            if dw:
                sample = "/".join(self._head(dw, 5))
                rules.append(f"- 带{style.dialect.value}方言腔调，自然穿插：{sample}")

        if style.english_mix_level >= 3:
            rules.append("- 偶尔夹英文，如 fine/whatever/seriously")
        elif style.english_mix_level == 0:
            rules.append("- 不夹英文")

        if style.emoji_level == 0:
            rules.append("- 不用 emoji")
        elif style.emoji_level >= 4:
            rules.append("- 适量使用 emoji")

        if style.kaomoji_level >= 2:
            rules.append("- 可以用颜文字，如 (´▽`)")

        if vocab.filler_words:
            sample = "/".join(self._head(vocab.filler_words, 4))
            rules.append(f"- 常用语气词：{sample}")

        if style.forbidden_words:
            rules.append("- 绝对不用这些词：" + "/".join(self._head(style.forbidden_words, 6)))

        # 补充维度：错别字率
        if style.typo_rate >= 3:
            rules.append("- 偶尔打错字/手滑（如把「哈哈」打成「哈哈】」），但别影响理解")

        return "\n".join(rules) if len(rules) > 1 else ""

    # ── 层4：情绪表达（命中才加） ─────────────────
    def _build_emotion_rules(self, style: LanguageStyle, current_emotion: str) -> str:
        em = style.emotion_styles
        key_map = {
            "happy": em.happy_style,
            "sad": em.sad_style,
            "angry": em.angry_style,
            "coquettish": em.coquettish_style,
            "shy": em.shy_style,
        }
        style_key = key_map.get(current_emotion)
        if not style_key:
            return ""
        desc = _EMOTION_MAP.get(current_emotion, {}).get(style_key)
        if not desc:
            return ""
        return f"【当前情绪：{current_emotion}】\n- {desc}"

    # ── 层6：场景策略（命中才加） ─────────────────
    def _build_scene_rules(self, style: LanguageStyle, current_scene: str) -> str:
        sc = style.scene_strategy
        key_map = {
            "venting": sc.when_user_venting,
            "asking": sc.when_user_asking,
            "silent": sc.when_user_silent,
            "short": sc.when_user_short,
            "praise": sc.when_praised,
            "attacked": sc.when_attacked,
        }
        style_key = key_map.get(current_scene)
        if not style_key:
            return ""
        desc = _SCENE_MAP.get(current_scene, {}).get(style_key)
        if not desc:
            return ""
        return f"【当前场景：{current_scene}】\n- {desc}"

    # ── 层7：信息密度（稳定） ─────────────────────
    def _build_density_rules(self, style: LanguageStyle) -> str:
        density_map = {
            ResponseDensity.TALKATIVE: (
                "【信息密度：话多型】\n"
                "- 一个问题可以说 3-5 句\n"
                "- 展开细节，追问，发散"
            ),
            ResponseDensity.PRECISE: (
                "【信息密度：精准型】\n"
                "- 说最关键的一句，不废话\n"
                "- 宁可短，不要凑字数"
            ),
            ResponseDensity.BLANK: (
                "【信息密度：留白型】\n"
                "- 故意不说完，给对方想象空间\n"
                "- 一句话够了就停"
            ),
            ResponseDensity.DIVERGENT: (
                "【信息密度：发散型】\n"
                "- 从一件事扯到另一件事\n"
                "- 跳跃感，但有内在逻辑"
            ),
            ResponseDensity.DETAIL: (
                "【信息密度：细节控】\n"
                "- 什么都说得很具体\n"
                "- 有细节才有真实感"
            ),
        }
        base = density_map.get(style.response_density, "")

        # 补充维度：单句篇幅
        length_map = {
            "short": "- 单句尽量短，像真人微信一样一句一句发",
            "long":  "- 可以写长一点的句子，但不许变成长篇大论",
        }
        extra = length_map.get(style.prefer_length, "")
        if extra:
            return base + "\n" + extra if base else "【信息密度】\n" + extra
        return base

    # ── 层8：专属标记（稳定） ─────────────────────
    def _build_signature_rules(self, style: LanguageStyle, vocab: VocabBank) -> str:
        rules = ["【专属语言标记】"]

        if style.catchphrases:
            rules.append("- 口头禅（自然穿插，不强求）：" + "/".join(self._head(style.catchphrases, 3)))
        if style.signature_patterns:
            rules.append("- 标志句式：" + "/".join(self._head(style.signature_patterns, 3)))
        rules.append(f"- 标志标点：偏好用「{style.signature_punctuation}」断句")
        if vocab.agreement_words:
            rules.append("- 认同时说：" + "/".join(self._head(vocab.agreement_words, 2)))
        if vocab.denial_words:
            rules.append("- 否认时说：" + "/".join(self._head(vocab.denial_words, 2)))

        return "\n".join(rules) if len(rules) > 1 else ""

    # ── 层5：脏话（关闭/空词库跳过） ──────────────
    def _build_dirty_rules(self, style: LanguageStyle, vocab: VocabBank) -> str:
        if not vocab.dirty_words:
            return ""
        style_desc = {
            DirtyTalkStyle.CUTE:     "娇嗔式，无攻击性",
            DirtyTalkStyle.BUDDY:    "损友式，亲密感强",
            DirtyTalkStyle.RAW:      "真实粗口，情绪激动时",
            DirtyTalkStyle.LITERARY: "不带脏字但有杀伤力",
            DirtyTalkStyle.DIALECT:  "方言腔调，地域感强",
        }
        desc = style_desc.get(style.dirty_talk, "")
        sample = "/".join(self._head(vocab.dirty_words, 4))
        return (
            f"【脏话系统：{desc}】\n"
            f"- 在情绪激动或亲密互动时可以使用\n"
            f"- 参考词库：{sample}\n"
            f"- 不要每句都用，偶尔一次效果最好"
        )

    # ── 场景例句（命中 + 限 1 条） ────────────────
    def _build_examples(self, vocab: VocabBank, current_scene: str) -> str:
        examples = vocab.scene_examples.get(current_scene, [])
        if not examples:
            return ""
        one = examples[0]
        return (
            f"【参考例句 - {current_scene}场景】\n"
            f"  「{one}」\n"
            f"（参考语感，不要原文复制）"
        )


# ══════════════════════════════════════════════════
# 动态联动（第④步）：AI 情绪 + 场景 → 风格参数
# ══════════════════════════════════════════════════

# AI 隐藏情绪 key（ai_mood）→ 语言风格情绪 key（happy/sad/angry/coquettish/shy）
# 只映射有可靠来源的：正向→happy，低落→sad。angry/coquettish/shy 暂无稳定情绪源，先不联动。
AI_EMOTION_TO_STYLE = {
    "happy":      "happy",
    "proud":      "happy",
    "amused":     "happy",
    "expectant":  "happy",
    "melancholy": "sad",
    "worried":    "sad",
    "lonely":     "sad",
    "nostalgic":  "sad",
    "guilty":     "sad",
}


def map_ai_emotion(emotion_key: Optional[str]) -> Optional[str]:
    """把 ai_mood 的原始情绪 key 映射成语言风格情绪 key（无映射返回 None）。"""
    if not emotion_key:
        return None
    return AI_EMOTION_TO_STYLE.get(emotion_key)


def detect_scene(user_message: Optional[str]) -> Optional[str]:
    """从用户消息判断抽象场景（回应策略）：venting/asking/silent/short/praise/attacked。"""
    msg = (user_message or "").strip()
    if not msg:
        return None

    # 被怼 / 攻击
    if any(k in msg for k in ["讨厌你", "烦死", "走开", "闭嘴", "滚", "你不行",
                              "垃圾", "废物", "没用", "就是个ai", "就是程序", "没意思", "假的"]):
        return "attacked"
    # 被夸
    if any(k in msg for k in ["喜欢你", "爱你", "你真好", "厉害", "好棒", "好帅",
                              "可爱", "谢谢你", "有你真好", "幸好有你"]):
        return "praise"
    # 倾诉（难过/累/烦）
    if any(k in msg for k in ["好累", "难过", "伤心", "委屈", "压力", "焦虑", "崩溃",
                              "烦", "哭", "撑不住", "不开心", "低落", "丧", "心累"]):
        return "venting"
    # 提问
    if "?" in msg or "？" in msg or any(k in msg for k in ["为什么", "怎么", "什么", "吗", "呢"]):
        return "asking"
    # 短消息
    if len(msg) <= 4:
        return "short"
    return None


def detect_example_scene(user_message: Optional[str], hour: Optional[int] = None) -> Optional[str]:
    """从用户消息 + 时间判断具体场景（参考例句）：深夜/被夸/用户开心/用户说累/被问喜不喜欢/安慰。"""
    msg = (user_message or "").strip()
    if not msg:
        return None

    # 深夜（时间维度，优先）
    if hour is not None and (hour >= 23 or hour < 5):
        return "深夜"
    # 被夸
    if any(k in msg for k in ["厉害", "好棒", "真棒", "好帅", "可爱"]):
        return "被夸"
    # 用户说累
    if any(k in msg for k in ["好累", "累死", "累了", "好困", "好疲惫"]):
        return "用户说累"
    # 用户开心
    if any(k in msg for k in ["哈哈", "开心", "太好了", "耶", "高兴", "赢了", "成功"]):
        return "用户开心"
    # 被问喜不喜欢
    if any(k in msg for k in ["喜欢我", "爱我", "喜不喜欢", "爱不爱"]):
        return "被问喜不喜欢"
    # 安慰（用户难过）
    if any(k in msg for k in ["难过", "伤心", "哭", "委屈", "崩溃"]):
        return "安慰"
    return None
