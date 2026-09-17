# -*- coding: utf-8 -*-
"""
唱歌意图识别（结合现有架构，贴合中文聊天）

从用户消息判断是否在请求唱歌、唱什么、是否合唱。
关键词组合优先（避免"别唱了"这种误判）。
"""
import re

# 唱歌触发词（组合优先，按长度降序匹配）
_SING_PHRASES = [
    "唱首歌吧", "唱首歌", "唱一首歌", "唱一首", "唱首", "来一首", "来首",
    "给我唱", "唱给我", "唱个歌", "唱个", "唱一下", "唱出来", "唱起来",
    "唱吧", "会唱", "能唱", "唱一唱", "哼一首", "哼一段", "吼一嗓子",
    "献唱", "唱两句", "唱歌", "唱",
]
# 合唱触发
_DUET_PHRASES = ["一起唱", "合唱", "陪我唱", "跟我唱", "和我唱", "一起合唱", "对唱"]
# 通话中触发（消息场景也可）
_CALL_PHRASES = ["打电话", "打过来", "来电话"]

_SONG_BRACKET = re.compile(r"[《〈「『]([^》〉」』]{1,20})[》〉」』]")
_SONG_AFTER = re.compile(r"(?:唱|来|来一首|来首|哼|哼一首)\s*[《〈「『]?([\u4e00-\u9fffA-Za-z0-9]{1,20})[》〉」』]?(?:这首|那首|的歌|给我|吧|呀|嘛|哦|~|～|！|！|。)?$")
_ARTIST = re.compile(r"([\u4e00-\u9fff]{2,6})(?:唱|作|原唱)?的?歌")


class SingIntent:
    def __init__(self):
        self.is_sing = False
        self.is_duet = False
        self.song_name = ""
        self.artist = ""
        self.style = ""          # 轻柔/激情/温柔...
        self.trigger_call = False


def detect_sing_intent(text: str) -> SingIntent:
    intent = SingIntent()
    msg = str(text or "").strip()
    if not msg:
        return intent

    # 排除明显否定
    if re.search(r"(别|不要|不用|不|别唱|算了|不用唱)", msg) and "唱" not in msg.replace("别唱", ""):
        pass

    has_sing = any(p in msg for p in _SING_PHRASES)
    # 排除"别唱了""不想唱""唱什么唱"等否定语境
    if has_sing and any(n in msg for n in ["别唱", "不唱", "不想唱", "别给我唱", "不用唱", "唱什么唱", "别吼", "别哼"]):
        # 但"唱首伤心的"这种仍算
        if not any(p in msg for p in ["唱首", "唱一首", "来一首", "唱个"]):
            has_sing = False
    # 排除"你唱的/唱得不好听"等评价语境（评价AI唱功不是请求，除非带"再唱/再来"）
    if has_sing and re.search(r"你唱(的|得)", msg):
        if not re.search(r"(再唱|再来|唱首|唱一首|继续唱|接着唱)", msg):
            has_sing = False

    if not has_sing:
        return intent

    intent.is_sing = True
    intent.trigger_call = any(p in msg for p in _CALL_PHRASES)

    # 合唱
    if any(p in msg for p in _DUET_PHRASES):
        intent.is_duet = True

    # 歌名：书名号优先
    m = _SONG_BRACKET.search(msg)
    if m:
        intent.song_name = m.group(1).strip()
    else:
        # "唱XX" 结尾提取
        m2 = _SONG_AFTER.search(msg)
        # 只排除单字"首""个"开头的纯无意义（"首歌""个歌"），多字词先提取再去前缀判断
        if m2 and not m2.group(1).startswith(("首", "个")):
            intent.song_name = m2.group(1).strip()
        # 去掉常见虚词尾巴
        if intent.song_name:
            intent.song_name = re.sub(r"(给我|吧|呀|嘛|哦|呢|~|～|！|！|。)$", "", intent.song_name).strip()
            # 去掉数量前缀："一段/一首/两句/一下/一个/一次/一会" 等
            intent.song_name = re.sub(r"^(一段|一首|两句|几句|一句|一下|一个|一次|一会|一会儿)", "", intent.song_name).strip()
            # ★ 二次过滤：去掉前缀后仍为"歌""唱歌"等无意义词，清空歌名让AI询问
            if intent.song_name in ("歌", "唱歌", ""):
                intent.song_name = ""
            # "XX的歌" → 视为歌手（没指定具体歌），歌名清空让 LLM 随机挑这位歌手的热歌
            m_gs = re.match(r"^([\u4e00-\u9fff]{2,6})的歌$", intent.song_name)
            if m_gs:
                intent.artist = m_gs.group(1)
                intent.song_name = ""

    # 歌手："XX的歌" / "XX唱的"
    m3 = _ARTIST.search(msg)
    if m3 and m3.group(1) and intent.song_name and m3.group(1) not in intent.song_name:
        intent.artist = m3.group(1)

    # 风格
    for kw in ["轻柔", "轻轻", "温柔", "甜甜", "撒娇", "软", "慢一点", "慢"]:
        if kw in msg:
            intent.style = "轻柔"
            break
    if not intent.style:
        for kw in ["激情", "嗨", "燃", "快", "摇滚", "大声"]:
            if kw in msg:
                intent.style = "激情"
                break
    if not intent.style:
        for kw in ["伤感", "难过", "悲伤", "悲", "哭", "忧郁", "低沉"]:
            if kw in msg:
                intent.style = "伤感"
                break

    return intent
