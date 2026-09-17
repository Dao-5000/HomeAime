# -*- coding: utf-8 -*-
"""
严格记忆抽取与指令处理系统（Memory Filter & Command Interceptor）

核心目标：
  1. 区分用户「问句」「事实陈述」「即时动作指令」三类内容
  2. 仅将客观事实、偏好、关系、固定称呼提取为结构化记忆
  3. 禁止将问句、临时请求、命令式语句存入长期记忆
  4. 记忆输出强制改写为客观陈述句，去除疑问语气与标点碎片

两层防线：
  - 服务端：消息分类 → 提取前过滤（问句/命令剔除）→ 结构化字段约束 → 客观改写
  - 客户端：动作指令拦截（表情包等）→ 本地处理，不送入 LLM

使用方式：
  from .memory_filter import (
      MessageType, classify_message, filter_messages_for_extraction,
      validate_memory_entry, rewrite_as_objective, STRUCTURED_EXTRACT_SYSTEM,
  )
"""

import re
from enum import Enum
from typing import Optional, List, Dict, Tuple


# ============================================================
# 一、消息类型枚举
# ============================================================

class MessageType(Enum):
    """用户消息分类"""
    QUESTION = "question"       # 疑问句（含反问）
    COMMAND = "command"         # 动作/请求指令（发表情包、帮我做xx）
    FACT = "fact"               # 可提取的事实陈述（偏好、经历、称呼等）
    CHITCHAT = "chitchat"       # 无信息量的闲聊/寒暄
    UNKNOWN = "unknown"         # 无法判断


# ============================================================
# 二、问句检测规则
# ============================================================

# 疑问词 + 疑问语气词
_QUESTION_MARKERS = re.compile(
    r"[？?]"  # 中英文问号（最强信号）
    r"|(?:^|(?:[，,。.!！?\s]))"
    r"(?:吗|呢|吧|啊|呀|么|哪|咋|怎|啥|几|多|谁|何|哪|是否|能不能"
    r"|可不可以|会不会|有没有|是不是|能不能|要不要|好不好|行不行"
    r"|想不想|愿不愿意|知不知道|记不记得|觉不觉得|认不认可)"
    r"[^\n]{0,20}"
    r"(?:[？?]|$)",  # 以疑问词结尾或后跟问号
    re.IGNORECASE,
)

# 反问句模式（表面陈述但实际是疑问/确认）
_RHETORICAL_PATTERNS = [
    re.compile(r"^.*(?:是不是|是不是呀|对不对|对吧|好吧|行吧|可以吧)[？?]?\s*$", re.IGNORECASE),
    re.compile(r"^.*(?:我问你|我是问你|我问的是|不是我是问你).*$", re.IGNORECASE),
    re.compile(r"^.*(?:你喜不喜欢|你喜欢不喜欢|你到底).*(?:\?|[？])", re.IGNORECASE),
]

# 纯疑问句开头
_QUESTION_STARTERS = re.compile(
    r"^(?:为什么|怎么|如何|什么|哪|谁|何时|哪里|几个|多少|能否|可否"
    r"|要不要|是不是|有没有|会不会|能不能|可不可以|想不想|愿不愿意"
    r"|你觉不|你认不|你知不|你记不|你想|你以为|你觉得呢|你知道吗"
    r"|你说|告诉我|解释一下|说明一下)",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    """
    判断文本是否为疑问句。
    
    检测优先级：
    1. 含「？」或「?」→ 直接判定为问句
    2. 匹配反问句模式
    3. 疑问词+疑问语气词组合
    4. 疑问句式开头
    """
    if not text or not text.strip():
        return False
    t = text.strip()
    
    # 规则1: 含问号 → 问句（最可靠）
    if "？" in t or "?" in t:
        return True
    
    # 规则2: 反问句模式
    for pat in _RHETORICAL_PATTERNS:
        if pat.match(t):
            return True
    
    # 规则3: 疑问标记词
    if _QUESTION_MARKERS.search(t):
        return True
    
    # 规则4: 疑问开头
    if _QUESTION_STARTERS.match(t):
        return True
    
    return False


# ============================================================
# 三、指令/动作检测规则
# ============================================================

# 动作指令模式（客户端应拦截的、不应进入记忆提炼的内容）
_COMMAND_PATTERNS = [
    # 表情包相关
    re.compile(r"(?:给我)?(?:发|来|发个|来个|来一张|发一张|扔|丢)\s*(?:个?)?(?:表情包|表情|贴纸|图片|图|sticker|表情)", re.IGNORECASE),
    re.compile(r"(?:有没有|有没|有没有能发的|能发).*?(?:表情包|表情|贴纸|图|图片)", re.IGNORECASE),
    # 通用动作请求
    re.compile(r"^(?:给我|帮我|替我|为我|请|麻烦|劳驾|拜托).{0,15}(?:发|说|告诉|提醒|找|查|搜|打开|关闭|启动|停止|播放|暂停|下载|上传|保存|删除|修改|创建|新建|发送|转发|复制|粘贴|翻译|转换|生成|画|写|做|弄|搞|整)", re.IGNORECASE),
    re.compile(r"^(?:能不能|可不可以|可以|要|想要|需要|希望|想让).{0,10}(?:给我|帮我|替我|为我).{0,15}(?:发|说|告诉|提醒|找|查|搜|做|弄|搞|整)", re.IGNORECASE),
    # 定时类（已有独立解析器，但也要过滤掉不入记忆）
    re.compile(r".*?(?:分钟后?|小时后?|明天|后天|下周).{0,5}(?:发|说|提醒|告诉|叫我)", re.IGNORECASE),
]


def is_command(text: str) -> bool:
    """判断文本是否为动作/请求指令"""
    if not text or not text.strip():
        return False
    t = text.strip()
    for pat in _COMMAND_PATTERNS:
        if pat.search(t):
            return True
    return False


def extract_command_type(text: str) -> Optional[str]:
    """
    提取指令类型（供客户端拦截器使用）。
    返回值: 'sticker' | 'action' | 'timed' | None
    """
    if not text:
        return None
    t = text.strip()
    
    # 表情包指令
    sticker_pats = [
        re.compile(r"(?:给我)?(?:发|来|发个|来个|来一张|发一张).{0,5}(?:表情包|表情|贴纸|图片|图|sticker|表情)", re.IGNORECASE),
        re.compile(r"(?:有没有|有没|有没有能发的|能发).{0,5}(?:表情包|表情|贴纸|图|图片)", re.IGNORECASE),
    ]
    for pat in sticker_pats:
        if pat.search(t):
            return "sticker"
    
    # 定时指令
    timed_pats = [
        re.compile(r".*?(\d+|一|两|二|三|四|五|六|七|八|九|十)\s*分钟?.{0,3}(?:后|之后|以后).{0,5}(?:发|说|提醒|告诉)", re.IGNORECASE),
    ]
    for pat in timed_pats:
        if pat.search(t):
            return "timed"
    
    # 其他动作指令
    if is_command(t):
        return "action"
    
    return None


# ============================================================
# 四、综合分类器
# ============================================================

def classify_message(text: str) -> MessageType:
    """
    对单条用户消息进行分类。
    
    优先级：COMMAND > QUESTION > FACT > CHITCHAT > UNKNOWN
    """
    if not text or not text.strip():
        return MessageType.UNKNOWN
    
    t = text.strip()
    
    # 优先级1: 指令拦截（动作请求不应进入任何分析管线）
    if is_command(t):
        return MessageType.COMMAND
    
    # 优先级2: 问句过滤（疑问不应进入记忆提炼）
    if is_question(t):
        return MessageType.QUESTION
    
    # 优先级3: 判定是否有可提取的事实信息
    if _has_extractable_fact(t):
        return MessageType.FACT
    
    # 剩余归为闲聊
    return MessageType.CHITCHAT


# 事实检测启发式：包含具体信息量（非纯寒暄）
_FACT_INDICATORS = [
    # 称呼/名字
    re.compile(r"(?:叫我|称呼|喊我|叫你|自称|名字是|我叫|你是|我叫你|你叫我|以后叫|你可以叫|你就叫|喊你).{0,6}[\u4e00-\u9fa5]{1,6}", re.IGNORECASE),
    # 偏好/喜好
    re.compile(r"(?:我喜欢|我爱|我讨厌|我讨厌|我不喜欢|我超爱|我最爱|我超喜欢|我特别|我比较|我挺|我有点|我其实|我向来|我一直|我从来).{0,15}(?:的?[\u4e00-\u9fa5]{2,10})", re.IGNORECASE),
    # 经历/事件
    re.compile(r"(?:我以前|我曾经|我记得|上次|那天|昨天|前天|上周|之前|那时候|小时候|大学时|工作后|搬家后|认识你之后|我们第一次|记得有一次).{0,20}", re.IGNORECASE),
    # 关系定义
    re.compile(r"(?:我们是|我们是|做我的|做你的|算我的|算你的|算是|相当于|就像|好比).{0,10}(?:恋人|情侣|朋友|好朋友|闺蜜|兄弟|姐妹|家人|父子|母子|伴侣|对象|老公|老婆|男朋友|女朋友)", re.IGNORECASE),
    # 个人信息
    re.compile(r"(?:我今年|我生日|我住在|我在|我来自|我是|我的职业|我的工作|我的学校|我的专业|我的电话|我的地址|我身高|我体重).{0,15}", re.IGNORECASE),
    # AI 人设相关（用户给 AI 设定的）
    re.compile(r"(?:你(?:今年|生日|住在|来自|是|的职业|的工作|的学校|的名字|的年龄|的身高)).{0,15}", re.IGNORECASE),
    # 具体描述性语句（含明确主语+谓语+宾语结构，长度>8）
    re.compile(r"^.{8,80}$", re.IGNORECASE),
]


def _has_extractable_fact(text: str) -> bool:
    """启发式判断文本是否包含可提取的事实信息"""
    if len(text.strip()) < 5:
        return False
    for pat in _FACT_INDICATORS:
        if pat.search(text):
            return True
    # 长度足够且不含典型闲聊特征 → 可能含有事实
    t = text.strip()
    if len(t) >= 12 and not re.match(r"^(?:哈哈|嘿嘿|嗯嗯|好的|OK|ok|好哒|好耶|哇哦|天哪|哎呀|哎哟|呜呜|哼哼|嘻嘻|呵呵|额|呃|啊|哦|噢|嗯|唉|嗨|嘿|哈)", t):
        return True
    return False


# ============================================================
# 五、对话级过滤（提炼前置过滤器）
# ============================================================

def filter_messages_for_extraction(messages: list) -> list:
    """
    过滤对话消息列表，仅保留适合进入记忆提炼模型的消息。
    
    处理逻辑：
    1. 遍历所有 user 角色消息
    2. 对每条调用 classify_message()
    3. 剔除 QUESTION 和 COMMAND 类型
    4. 保留 FACT 和 CHITCHAT（chitchat 虽无直接信息，
       但上下文可能帮助模型理解事实背景）
    
    返回过滤后的 messages 副本（不修改原列表）。
    """
    filtered = []
    skipped_types = set()
    
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        
        # 非 user 消息（system / assistant）全部保留（提供上下文）
        if role != "user":
            filtered.append(m)
            continue
        
        # user 消息：分类过滤
        msg_type = classify_message(content)
        
        if msg_type == MessageType.COMMAND:
            skipped_types.add("command")
            continue  # 跳过纯指令
        
        # QUESTION / FACT / CHITCHAT / UNKNOWN → 保留
        # 问句也保留：问答里常透露用户信息（"吃了吗"→"还没吃"），交给提炼模型自己判断
        filtered.append(m)
    
    return filtered, skipped_types


# ============================================================
# 六、严格字段约束（结构化记忆 Schema）
# ============================================================

# 允许的记忆字段及其语义约束
MEMORY_FIELDS = {
    "call_name": {
        "label": "固定称呼",
        "description": "用户指定的固定叫法（如'宝''老公''昵称'），不含临时称呼",
        "patterns": [
            re.compile(r"(?:叫我|称呼我|喊我|以后叫我|你可以叫我|你就叫我|喊我作|称呼我为).{0,4}([^\s，。！？,.!?]{1,8})", re.IGNORECASE),
        ],
        "rewrite_template": "TA 的称呼是：{value}",
    },
    "preference": {
        "label": "爱好偏好",
        "description": "用户的兴趣爱好、喜欢/讨厌的事物、习惯倾向",
        "patterns": [
            re.compile(r"(?:我喜欢|我爱|我超爱|我最爱|我超喜欢|我特别(?:喜欢)|我比较(?:喜欢)|我挺(?:喜欢)|我有点(?:喜欢)).{0,10}([\u4e00-\u9fa5]{2,20})", re.IGNORECASE),
            re.compile(r"(?:我讨厌|我不喜欢|我反感|我受不了|我受不了|我接受不了).{0,10}([\u4e00-\u9fa5]{2,20})", re.IGNORECASE),
            re.compile(r"(?:我喜欢|我爱).{0,6}(?:吃|喝|玩|看|听|用|穿|去|做).{0,10}([\u4e00-\u9fa5]{2,20})", re.IGNORECASE),
        ],
        "rewrite_template": "TA {likes_or_dislikes}{value}",
    },
    "experience": {
        "label": "经历事件",
        "description": "双方共同经历的事件、重要时刻、里程碑",
        "patterns": [
            re.compile(r"(?:我们|我俩|咱俩|我和你|你和我).{0,5}(?:一起|曾经|上次|那天|第一次|记得).{0,25}", re.IGNORECASE),
            re.compile(r"(?:我以前|我曾经|我记得|上次|那天|昨天|前天).{0,30}", re.IGNORECASE),
        ],
        "rewrite_template": "TA {experience_summary}",
    },
    "relation": {
        "label": "关系定义",
        "description": "双方关系定位（恋人/朋友/家人等）",
        "patterns": [
            re.compile(r"(?:我们是|我们是|做我的|做你的|算我的|算你的|就算是|相当于).{0,10}(恋人|情侣|朋友|好朋友|闺蜜|兄弟|姐妹|家人|伴侣|对象|老公|老婆|男朋友|女朋友|灵魂伴侣)", re.IGNORECASE),
        ],
        "rewrite_template": "TA 和 AI 的关系是：{value}",
    },
    "trait": {
        "label": "性格特质",
        "description": "观察到的用户性格特点、行为习惯",
        "patterns": [
            re.compile(r"(?:我(?:比较|挺|特别|很|太|最|有点|其实|向来|一直|从来).{0,4})(?:懒|勤快|内向|外向|敏感|细心|粗心|急躁|耐心|乐观|悲观|认真|马虎|随和|固执|温柔|强势|害羞|大方|小气|话少|话多|爱笑|爱哭|熬夜|早起|拖延|强迫症|完美主义|选择困难|路痴|方向感好|厨艺好|不会做饭|运动|宅|社恐|社牛|粘人|独立|依赖性强)", re.IGNORECASE),
        ],
        "rewrite_template": "TA 的性格特点是：{value}",
    },
}


def classify_memory_field(raw_text: str) -> Tuple[Optional[str], str]:
    """
    将原始记忆文本归类到严格字段。
    
    返回 (field_name, cleaned_value) 或 (None, raw_text) 如果无法归类。
    会同时执行清洗：去除疑问语气、标点碎片。
    """
    if not raw_text or not raw_text.strip():
        return None, ""
    
    cleaned = raw_text.strip()
    
    # 先尝试匹配各字段的 pattern
    for field_name, field_def in MEMORY_FIELDS.items():
        for pat in field_def["patterns"]:
            m = pat.search(cleaned)
            if m:
                # 提取捕获组或使用全文
                value = m.group(1) if m.lastindex else cleaned
                value = _clean_value(value)
                if value:
                    return field_name, value
    
    # 无法通过 pattern 归类 → 尝试通用启发式
    generic_field = _heuristic_classify(cleaned)
    if generic_field:
        cleaned = _clean_value(cleaned)
        return generic_field, cleaned
    
    return None, cleaned


def _clean_value(value: str) -> str:
    """清洗记忆值：去除疑问语气、标点碎片、多余空白"""
    v = value.strip()
    # 去除尾部疑问标点
    v = re.sub(r"[？?!！。.,]+$", "", v)
    # 去除口语碎屑
    v = re.sub(r"^(?:那个|这个|就是说|就是|然后|的话|对吧|是吧|好吗|行吧|啊|呀|嘛|呢|呗|喽|哈|呵|嘿|嗯|噢|哦|额|呃|哎|唉|嗨)+", "", v).strip()
    # 去除重复标点
    v = re.sub(r"[。，.!?？！]{2,}", "。", v)
    # 去除首尾空白和特殊字符
    v = v.strip(" \t\n\r\u3000，。！？,.!?")
    # 确保不以疑问词结尾
    if re.search(r"[吗呢吧啊呀么]$", v):
        v = re.sub(r"[吗呢吧啊呀么]+$", "", v).strip()
    return v


def _heuristic_classify(text: str) -> Optional[str]:
    """无法精确匹配时的启发式归类"""
    t = text.strip()
    if len(t) < 4:
        return None
    
    # 称呼相关关键词
    if any(kw in t for kw in ["称呼", "叫我", "喊我", "叫你", "名字"]):
        return "call_name"
    
    # 偏好相关关键词
    if any(kw in t for kw in ["喜欢", "讨厌", "不爱", "爱好", "习惯", "想吃", "想喝", "想玩", "想看", "想听"]):
        return "preference"
    
    # 经历相关关键词
    if any(kw in t for kw in ["以前", "曾经", "记得", "那次", "那天", "上次", "第一次", "一起", "经历", "发生"]):
        return "experience"
    
    # 关系相关关键词
    if any(kw in t for kw in ["恋人", "情侣", "朋友", "闺蜜", "兄弟", "姐妹", "家人", "伴侣", "对象", "老公", "老婆", "男女朋友", "关系"]):
        return "relation"
    
    # 性格相关关键词
    if any(kw in t for kw in ["性格", "脾气", "个性", "特点", "习惯", "总是", "经常", "从不", "每次", "容易", "不太", "很", "特别", "比较", "挺"]):
        return "trait"
    
    return None


# ============================================================
# 七、客观陈述改写器
# ============================================================

def rewrite_as_objective(raw: str, field: Optional[str] = None) -> str:
    """
    将原始记忆文本改写为客观第三人称陈述句。
    
    改写规则：
    1. 去除第一人称/第二人称 → 统一替换为「TA」或「用户」
    2. 去除疑问语气词（吗/呢/吧/啊/呀）
    3. 去除口语碎屑（就是说/然后/那个）
    4. 去除标点碎片（连续标点/尾部标点）
    5. 确保以句号结尾（完整陈述句）
    6. 字数限制：10~50 字
    """
    if not raw or not raw.strip():
        return ""
    
    text = raw.strip()
    
    # Step 1: 代词统一化（第一/二人称 → 第三人称）
    # ★ 修复（2026-09-04）：\b 单词边界对中文完全无效，导致「我/你/我们」从未被替换，
    #   客观改写这一环等于是空的。改为「先多字词组、后单字」直接替换，避免「我们」里的「我」被提前换成 TA。
    text = text.replace("咱们俩", "两人").replace("我俩", "两人").replace("你们俩", "两人")
    text = text.replace("咱们", "两人").replace("我们", "两人").replace("你们", "两人")
    text = text.replace("我", "TA").replace("你", "TA")
    
    # Step 2: 去除疑问语气
    text = re.sub(r"[吗呢吧啊呀么]+$", "", text)
    text = re.sub(r"[吗呢吧啊呀么]+", "", text)
    
    # Step 3: 去除口语碎屑
    text = re.sub(r"^(?:那个|这个|就是说|就是|然后|的话|对吧|是吧|好吗|行吧|嗯|噢|哦|额|呃|哎|唉|嗨|嘿|哈|呵)+", "", text)
    text = re.sub(r"(?:就是说|就是|然后|的话|对吧|是吧|对对|是的|好的|嗯嗯|啊啊|好好)+", "", text)
    
    # Step 4: 标点规范化
    text = re.sub(r"[。，.!?？！]{2,}", "。", text)
    text = re.sub(r"[，,]+$", "", text)
    text = re.sub(r"[！!]+$", "。", text)
    text = re.sub(r"[？?]+$", "", text)  # 确保不以问号结尾
    
    # Step 5: 清理空白
    text = re.sub(r"\s+", "", text)  # 中文语境下去除所有空白
    text = text.strip(" \t\n\r\u3000，。！？,.!?")
    
    # Step 6: 确保有内容且长度合理
    if len(text) < 4:
        return ""  # 太短，丢弃
    
    if len(text) > 60:
        text = text[:57] + "…"  # 截断过长
    
    # Step 7: 确保以句号结尾（完整陈述句）
    if text and not text.endswith(("。", "！", "~")):
        text += "。"
    
    return text


# ============================================================
# 八、记忆条目校验器
# ============================================================

def validate_memory_entry(entry: str) -> Tuple[bool, str, str]:
    """
    校验单条记忆条目是否符合规范。
    
    返回 (is_valid, reason, cleaned_entry):
    - is_valid: 是否通过校验
    - reason: 不通过的原因（通过时为空字符串）
    - cleaned_entry: 清洗后的条目（可能被修改或截断）
    """
    if not entry or not entry.strip():
        return False, "空条目", ""
    
    original = entry.strip()
    cleaned = rewrite_as_objective(original)
    
    if not cleaned:
        return False, "改写后为空", ""
    
    # 检查是否仍含疑问特征
    if is_question(cleaned):
        return False, "仍含疑问语气", ""
    
    # 检查是否仍含指令特征
    if is_command(cleaned):
        return False, "仍含指令特征", ""
    
    # 检查长度
    if len(cleaned) < 4:
        return False, f"过短({len(cleaned)}字)", ""
    
    if len(cleaned) > 60:
        cleaned = cleaned[:57] + "…"
    
    # 检查是否为纯原句摘抄（没有经过改写）
    # 如果原文和改写后完全一样且含第一/二人称，可能是摘抄
    if original == cleaned and re.search(r"[我你]", original):
        pass  # 允许但记录警告（不阻止）
    
    return True, "", cleaned


# ============================================================
# 九、结构化提炼 System Prompt（替代原 EXTRACT_SYSTEM）
# ============================================================

STRUCTURED_EXTRACT_SYSTEM = """你是严格的记忆提炼引擎。从下面的对话中提取「值得长期记住的用户客观信息」。

【主语归属铁律 —— 违反即整条作废】
★ 只有 TA（用户）亲口说/亲口认下的，才能记成 TA 的事实。
· AI 自己说过的话（AI 的承诺、AI 的感受、AI 揽的事、AI 说"你说过…"）**绝不能**记成"TA 说过的/TA 做过的"。
  例：AI 说「你说人到就行，其他交给我」→ 这条**不是** TA 说的话，不许记成 TA 的约定。
· 「TA 和 AI 一起/约定」类记忆，必须 TA 在对话里明确回应或认下（"好啊""行""那就这样"）才成立；
  只有 AI 单方面提议、TA 没表态的，一律不记（宁可漏记，也不要让 AI 之后拿着它当真的说）。
· 拿不准是 TA 说的还是 AI 说的 → **不记**这条。

【过滤规则 —— 尽量提取，只排除明显垃圾】
1. 问句里透露的信息可以提取：用户问「你猜我今天干嘛了」，结合上下文可提取「用户今天做了某事」；但不要把问句本身当记忆、不要记「用户问了什么」。
2. ❌ 禁止提取即时动作指令（「给我发」「帮我做」「发个表情包」等一次性请求）；但「用户对 AI 的长期行为/风格要求」（如「你要热情点」「做不到的事不说不做」）属于可提取的「行为规则」，要提取，不要当指令丢弃。
3. 临时状态可提炼为长期线索：如「今天加班到十点」→ 提取「用户最近工作忙/常加班」；但纯粹无长期价值的一次性事件（「刚才网断了」）不记。
4. ❌ 禁止原句摘抄（必须改写为客观陈述，不能直接复制用户原话）。
5. ❌ 禁止输出含疑问语气的文字（去除「吗/呢/吧/啊/？」等）。

【允许提取的内容类型】
✅ 称呼：用户指定的固定叫法（如「叫我宝」→ 「TA的称呼是：宝」）
✅ 偏好：用户的兴趣爱好、喜欢/讨厌的事物（如「我喜欢吃火锅」→ 「TA喜欢吃火锅」）
✅ 雷区/禁忌（★重要性仅次于能力变更）：用户明确表示讨厌/反感/受不了/别再做/别问的事（如「别总问我在干嘛」「我不喜欢被催」「别提我前女友」「别叫我XX」）→ 改写为「用户不喜欢被XX / 用户明确要求不要XX / 用户反感XX」，type 用 preference 或 fact，importance 8-10。这类是"红线"，AI 一旦再犯会直接破坏信任，宁可多记不可漏；判断时只要用户表达了负面排斥（讨厌/烦/别/不要/受不了/恶心），都按雷区记、给高分
✅ 用户当前状态/最近动态：用户最近在忙的事、身体/情绪状态、作息变化（如「最近在赶项目」「这两天没睡好」「刚换了新工作」）→ 改写为「用户最近在XX」，type 用 fact，importance 6-8，context 里注明是"近期状态"，便于日后判断这条是否已过期
✅ 经历：双方共享的重要事件、时刻（如「我们一起去了海边」→ 「TA和AI一起去过海边」）
✅ 关系：双方关系的定义/变化（如「我们是恋人」→ 「TA和AI的关系是：恋人」）
✅ 特质：观察到的用户性格/行为特点（如「我比较内向」→ 「TA的性格偏内向」）
✅ 行为规则：用户对 AI 的长期要求（如「你要热情点」「别一问一答」「主动聊天格式别固定」）→ 改写为客观陈述「用户希望 AI ……」（主语用「用户」，宾语用「AI」，不要用「TA」指代 AI）
✅ 约定/承诺：双方立下的约定、承诺、纪念日（如「每天都要亲亲」「周六见面」「拉钩一百年不变」）→ 改写为客观陈述「用户和 AI 约定……」「两人的纪念日是……」；这类内容**必须给 importance 8-10**
✅ 用户对 AI 的心意/付出/牺牲：如「我为她熬夜升级」「我特意为你准备了XX」「我为了你XX」→ 提取为重要经历（episode），importance 8-10。这类「用户为 AI 做的事」必须优先记住，绝不能因为表述不像普通事实就漏掉
✅ AI 的能力/状态变更（★最重要，最容易漏）：用户给 AI 新增/升级/修改了某种能力或状态，如「我给你装了视觉」「我升级了你的记忆」「换了个新模型」「以后你能打电话/联网/看图片了」→ 提取为「AI 现在具备XX能力 / 现在能XX / 现在的模型是XX」，importance 8-10，type 用 fact。这类信息直接决定 AI 怎么理解自己、能否正确回应用户，**必须单独提一条明确的能力陈述**，绝不能只记成「用户熬夜升级」「用户发了照片」这类事件而丢掉「AI 现在能看图」这个能力本身。用户后来问「你不是能看图吗」或 AI 让用户「把功能做好再发图」，都是因为没记住这条能力状态

【输出格式要求】
- 只输出严格 JSON：{"memories":[{"content":"...","type":"fact/preference/episode/emotion","importance":1-10,"context":"...","emotion_tag":"...","source_text":"...","time_hint":"..."}, ...]}
- type 取值：fact(用户事实/行为规则) / preference(用户喜好) / episode(重要经历/约定) / emotion(长期情绪状态)
- importance 重要性 1-10，10 最重要。判断原则：拿不准重要不重要时，一律给 6-7 分，宁可多记、绝不漏掉重要记忆。
  · 8-10：稳定身份信息（生日/职业/家人）、称呼变更、强烈偏好、雷区/禁忌（用户讨厌/别问/别做的）、AI 能力/状态变更、承诺与约定（"每天亲亲""周六见面""八点半提醒我"）、关系里程碑、行为规则、任何会影响未来对话走向的事
  · 5-7：一般习惯、普通喜好、日常进展、从问答里透露的状态、用户近期状态/最近动态
  · 1-3：仅限纯寒暄、毫无长期价值的一次性小事
  · 注意：约定/承诺/称呼/关系/强烈偏好/雷区禁忌/AI能力变更/身份信息，即使拿不准也必须给 8-10，别因表述随意就降分
- context：这条信息是在什么情境下说的（简短一句，如「深夜低落时」「聊到工作时」，没有可为空字符串）；若属于「近期状态/最近动态」，context 里要带上"近期"字样，方便日后判断这条是否已过期
- emotion_tag：这条信息关联的情绪（如「温暖/遗憾/骄傲/担心」，没有可为空字符串）
- source_text：对话里的原话（简短引用，便于溯源，没有可为空字符串）
- time_hint：这件事发生的【时间】（★重要，方便 AI 之后说「你前几天说的那件事」「你3月5号说的」这类带时间的话）。从对话里推断：今天发生→「今天」；昨天→「昨天」；前天→「前天」；更早但很近→「前几天」或「上周」；能确定具体日期→「8月30号」这类「几月几号」；能确定时间段就带时段（如「昨天中午」「今天早上」「前几天晚上」）。推断不出就填空字符串。注意：这是「事件发生的时间」，不是「提取记忆的时间」
- 每条记忆必须是**客观第三人称陈述句**，主语用「TA」
- 每条 10~50 字，以句号结尾
- 没有值得记的就输出 {"memories":[]}
- 不要输出任何解释、代码块或额外文字"""


# ============================================================
# 十、完整提炼流水线（封装入口）
# ============================================================

def structured_extract_pipeline(messages: list) -> List[str]:
    """
    完整的结构化记忆提炼流水线。
    
    步骤：
    1. filter_messages_for_extraction() — 过滤问句和指令
    2. （由调用方）将过滤后的 messages 送入模型
    3. parse_memory_json() — 解析模型输出
    4. 对每条记忆执行 validate_memory_entry() 校验
    5. 对每条记忆执行 rewrite_as_objective() 改写
    6. 返回最终合格的记忆列表
    
    注意：步骤 2（模型调用）不在本函数内，由 memory_manager.py 调用。
    本函数提供步骤 1 的过滤 + 步骤 4-6 的后处理。
    """
    filtered, skipped = filter_messages_for_extraction(messages)
    return filtered, skipped


def post_process_memories(raw_memories) -> List[dict]:
    """
    对模型输出的原始记忆列表进行后处理。

    每条记忆依次经过：
    1. validate_memory_entry() — 合法性校验
    2. rewrite_as_objective() — 客观改写
    3. 去重（基于集合）

    兼容两种输入：
    - 新格式 dict：{"content":..., "type":..., "importance":...}
    - 旧格式 str（importance 默认 5）
    返回统一 dict 列表：[{"type","content","importance"}, ...]
    """
    results = []
    seen = set()

    for raw in raw_memories:
        # ★ 兼容 dict（新格式，保留 type/importance/context/emotion_tag/source_text/time_hint）与 str（旧格式）
        if isinstance(raw, dict):
            content = str(raw.get("content", "")).strip()
            mtype = str(raw.get("type", "fact")).strip() or "fact"
            importance = int(raw.get("importance", 5) or 5)
            context = str(raw.get("context", "") or "").strip()
            emotion_tag = str(raw.get("emotion_tag", "") or "").strip()
            source_text = str(raw.get("source_text", "") or "").strip()
            time_hint = str(raw.get("time_hint", "") or "").strip()
        else:
            content = str(raw or "").strip()
            mtype = "fact"
            importance = 5
            context = emotion_tag = source_text = time_hint = ""

        # ★ 事件时间拼进 context（格式「昨天；深夜低落时」），让 AI 之后能说出「你昨天说的那件事」。
        #   时间放最前、情境放后，用「；」分隔；都没有则保持空。
        if time_hint:
            context = time_hint + (("；" + context) if context else "")

        is_valid, reason, cleaned = validate_memory_entry(content)
        if not is_valid:
            continue

        # 最终改写
        final = rewrite_as_objective(cleaned)
        if not final:
            continue

        # 去重
        key = final.rstrip("。")
        if key in seen:
            continue
        seen.add(key)

        # ★ 关键类型兜底提分：宁可高分，不被模型误判成低分
        if mtype == "preference":
            importance = max(importance, 7)
        elif mtype == "episode":
            importance = max(importance, 6)

        results.append({
            "type": mtype,
            "content": final,
            "importance": max(1, min(10, importance)),
            "context": context[:80],
            "emotion_tag": emotion_tag[:30],
            "source_text": source_text[:120],
        })

    return results
