# -*- coding: utf-8 -*-
"""
语言风格 Schema（优化语言方案 · 结合落地版）

把「说话风格」从一段自由文本，结构化成可注入 prompt 的参数。
8 层维度（方案原版）+ 3 个补充维度（单句篇幅 / 错别字率 / 换行分段）。

设计原则（结合现有架构）：
1. 只做数据定义，不做 prompt 组装（组装在 style_injector.py）。
2. 字段全部带默认值，保证老数据 / 缺字段也能安全反序列化。
3. 词库全部预设、用户只选人格；脏话默认关闭。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Dict, Optional


# ==================== 枚举定义 ====================

class DirtyTalkStyle(Enum):
    OFF        = "off"         # 关闭
    CUTE       = "cute"        # 娇嗔式
    BUDDY      = "buddy"       # 损友式
    RAW        = "raw"         # 粗犷式
    LITERARY   = "literary"    # 文艺式骂人
    DIALECT    = "dialect"     # 方言腔


class DialectFlavor(Enum):
    NONE       = "none"
    SICHUAN    = "sichuan"     # 川渝
    CANTONESE  = "cantonese"   # 粤语
    NORTHEAST  = "northeast"   # 东北
    HOKKIEN    = "hokkien"     # 闽南


class EmotionExpressStyle(Enum):
    DIRECT     = "direct"      # 直说
    HIDDEN     = "hidden"      # 藏着
    METAPHOR   = "metaphor"    # 用比喻
    DEFLECT    = "deflect"     # 转移话题


class ResponseDensity(Enum):
    TALKATIVE  = "talkative"   # 话多型
    PRECISE    = "precise"     # 精准型
    BLANK      = "blank"       # 留白型
    DIVERGENT  = "divergent"   # 发散型
    DETAIL     = "detail"      # 细节控


def _enum_value(v, enum_cls, default):
    """把任意输入规整成枚举值（容忍 str / Enum / None）。"""
    if v is None:
        return default
    if isinstance(v, enum_cls):
        return v
    try:
        return enum_cls(str(v))
    except (ValueError, TypeError):
        return default


def _int_clamp(v, lo, hi, default):
    """把任意输入规整成 [lo, hi] 区间整数。"""
    try:
        return max(lo, min(hi, int(v)))
    except (ValueError, TypeError):
        return default


# ==================== 情绪风格映射 ====================

@dataclass
class EmotionStyleMap:
    """每种情绪下的说话方式（值为中文风格关键词，非枚举，方便 prompt 直读）"""
    happy_style: str = "叠词"        # 叠词/感叹/淡淡/肢体感
    sad_style: str = "隐藏"          # 直说/隐藏/转移/碎碎念
    angry_style: str = "冷字"        # 冷字/爆发/反问/消失
    coquettish_style: str = "叠词"   # 叠词/反向/无理取闹/沉默等哄
    shy_style: str = "否认"          # 否认/转移/攻击回去


# ==================== 场景回应策略 ====================

@dataclass
class SceneResponseStrategy:
    """不同场景下的回应策略（值为中文风格关键词）"""
    when_user_venting: str = "陪着听"     # 打断/陪着听/提问深挖
    when_user_asking: str = "直接答"      # 直接答/反问回去/答了又问
    when_user_silent: str = "等着"        # 追问/等着/自己说别的
    when_user_short: str = "同样短回"     # 同样短回/展开聊/反问
    when_praised: str = "否认"            # 否认/接受/反夸/岔开
    when_attacked: str = "怼回去"         # 认怂/怼回去/沉默/撒娇化解


# ==================== 核心语言风格 ====================

@dataclass
class LanguageStyle:
    """完整语言风格定义（8 层 + 3 补充维度）"""

    # ── 层1：节奏感 ──────────────────────────────
    send_in_parts: bool = False            # 是否分多条发（制造打字感）
    thinking_opener: bool = False          # 开头用省略号制造思考感
    topic_switch: str = "自然过渡"         # 突然跳/自然过渡/拒绝切换

    # ── 层2：句式偏好 ─────────────────────────────
    rhetorical_rate: int = 2               # 反问频率 0-5
    omit_subject: bool = True              # 省略主语
    half_sentence: bool = False            # 故意说一半
    echo_user_words: bool = True           # 重复对方的词
    inversion_habit: bool = False          # 倒装习惯
    self_qa: bool = False                  # 自问自答

    # ── 层3：词汇色彩 ─────────────────────────────
    literary_level: int = 1                # 文艺浓度 0-5
    internet_slang_level: int = 2          # 网络用语密度 0-5
    dialect: DialectFlavor = DialectFlavor.NONE
    english_mix_level: int = 0             # 英文夹杂 0-5
    kaomoji_level: int = 0                 # 颜文字 0-5
    emoji_level: int = 1                   # emoji 0-5

    # ── 层4：情绪表达 ─────────────────────────────
    emotion_styles: EmotionStyleMap = field(default_factory=EmotionStyleMap)

    # ── 层5：脏话系统（默认关闭） ──────────────────
    dirty_talk: DirtyTalkStyle = DirtyTalkStyle.OFF

    # ── 层6：回应策略 ─────────────────────────────
    scene_strategy: SceneResponseStrategy = field(default_factory=SceneResponseStrategy)

    # ── 层7：信息密度 ─────────────────────────────
    response_density: ResponseDensity = ResponseDensity.PRECISE

    # ── 层8：专属语言标记 ─────────────────────────
    catchphrases: List[str] = field(default_factory=list)        # 口头禅 1-3 个
    signature_patterns: List[str] = field(default_factory=list)  # 标志句式
    forbidden_words: List[str] = field(default_factory=list)     # 绝对不用的词
    signature_punctuation: str = "。"                              # 标志标点
    address_style: str = "你"                                     # 称呼对方

    # ── 补充维度 A：单句篇幅 ──────────────────────
    prefer_length: str = "medium"           # short/medium/long

    # ── 补充维度 B：错别字/口误率 ─────────────────
    typo_rate: int = 0                      # 0-5（真人感来源，默认 0=不打错字）

    # ── 补充维度 C：换行/分段习惯 ─────────────────
    line_break_habit: bool = False          # 爱把一句话拆成几行发（微信辨识度）

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "LanguageStyle":
        """从角色卡里的 language_style 字典安全构造（缺字段用默认值）。"""
        if not isinstance(data, dict):
            return cls()

        def _bool(v, d):
            return bool(v) if v is not None else d

        def _str(v, d):
            return str(v) if v not in (None, "") else d

        def _str_list(v):
            if not v:
                return []
            if isinstance(v, str):
                return [s.strip() for s in v.replace("、", ",").replace("，", ",").split(",") if s.strip()]
            if isinstance(v, (list, tuple)):
                return [str(x).strip() for x in v if str(x).strip()]
            return []

        return cls(
            send_in_parts=_bool(data.get("send_in_parts"), False),
            thinking_opener=_bool(data.get("thinking_opener"), False),
            topic_switch=_str(data.get("topic_switch"), "自然过渡"),
            rhetorical_rate=_int_clamp(data.get("rhetorical_rate"), 0, 5, 2),
            omit_subject=_bool(data.get("omit_subject"), True),
            half_sentence=_bool(data.get("half_sentence"), False),
            echo_user_words=_bool(data.get("echo_user_words"), True),
            inversion_habit=_bool(data.get("inversion_habit"), False),
            self_qa=_bool(data.get("self_qa"), False),
            literary_level=_int_clamp(data.get("literary_level"), 0, 5, 1),
            internet_slang_level=_int_clamp(data.get("internet_slang_level"), 0, 5, 2),
            dialect=_enum_value(data.get("dialect"), DialectFlavor, DialectFlavor.NONE),
            english_mix_level=_int_clamp(data.get("english_mix_level"), 0, 5, 0),
            kaomoji_level=_int_clamp(data.get("kaomoji_level"), 0, 5, 0),
            emoji_level=_int_clamp(data.get("emoji_level"), 0, 5, 1),
            emotion_styles=EmotionStyleMap(**{k: v for k, v in (data.get("emotion_styles") or {}).items()
                                              if k in EmotionStyleMap.__dataclass_fields__}),
            dirty_talk=_enum_value(data.get("dirty_talk"), DirtyTalkStyle, DirtyTalkStyle.OFF),
            scene_strategy=SceneResponseStrategy(**{k: v for k, v in (data.get("scene_strategy") or {}).items()
                                                    if k in SceneResponseStrategy.__dataclass_fields__}),
            response_density=_enum_value(data.get("response_density"), ResponseDensity, ResponseDensity.PRECISE),
            catchphrases=_str_list(data.get("catchphrases")),
            signature_patterns=_str_list(data.get("signature_patterns")),
            forbidden_words=_str_list(data.get("forbidden_words")),
            signature_punctuation=_str(data.get("signature_punctuation"), "。"),
            address_style=_str(data.get("address_style"), "你"),
            prefer_length=_str(data.get("prefer_length"), "medium"),
            typo_rate=_int_clamp(data.get("typo_rate"), 0, 5, 0),
            line_break_habit=_bool(data.get("line_break_habit"), False),
        )

    def to_dict(self) -> dict:
        """序列化回角色卡（枚举转 value，dataclass 递归转 dict）。"""
        d = asdict(self)
        d["dialect"] = self.dialect.value
        d["dirty_talk"] = self.dirty_talk.value
        d["response_density"] = self.response_density.value
        return d


# ==================== 词库定义 ====================

@dataclass
class VocabBank:
    """人格专属词库（全部预设，用户不可编辑）"""
    filler_words: List[str] = field(default_factory=list)         # 语气词库
    exclamations: List[str] = field(default_factory=list)         # 情感感叹词
    transition_words: List[str] = field(default_factory=list)     # 转折词
    agreement_words: List[str] = field(default_factory=list)      # 认同词
    denial_words: List[str] = field(default_factory=list)         # 否定词
    coquettish_words: List[str] = field(default_factory=list)     # 撒娇词库
    comfort_words: List[str] = field(default_factory=list)        # 安慰词库
    dirty_words: List[str] = field(default_factory=list)          # 脏话词库（默认关闭）
    dialect_words: List[str] = field(default_factory=list)        # 方言词库
    metaphor_templates: List[str] = field(default_factory=list)   # 比喻句模板
    scene_examples: Dict[str, List[str]] = field(default_factory=dict)  # 场景例句库

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "VocabBank":
        if not isinstance(data, dict):
            return cls()

        def _lst(k):
            v = data.get(k)
            if isinstance(v, list):
                return [str(x).strip() for x in v if str(x).strip()]
            return []

        scene = {}
        for k, v in (data.get("scene_examples") or {}).items():
            if isinstance(v, list):
                scene[str(k)] = [str(x) for x in v if str(x)]
        return cls(
            filler_words=_lst("filler_words"),
            exclamations=_lst("exclamations"),
            transition_words=_lst("transition_words"),
            agreement_words=_lst("agreement_words"),
            denial_words=_lst("denial_words"),
            coquettish_words=_lst("coquettish_words"),
            comfort_words=_lst("comfort_words"),
            dirty_words=_lst("dirty_words"),
            dialect_words=_lst("dialect_words"),
            metaphor_templates=_lst("metaphor_templates"),
            scene_examples=scene,
        )

    def to_dict(self) -> dict:
        return asdict(self)
