# -*- coding: utf-8 -*-
"""
双模式 —— 伴侣模式 / 助手模式。

默认是伴侣模式（恋人助手）。当用户的消息是"明确的任务型指令"时，
自动切到助手模式走 Agent 循环（调工具干活）；其余照旧走聊天。

设计约束：宁可漏判也不误判 —— 只有"非常像任务"才进 Agent，
否则都走普通聊天，绝不影响助手的聊天体验。
"""
import re


# 任务触发词：仅当理解层「没跑起来」时兜底用，必须是很明确的祈使，宁可漏判不误判
_TASK_TRIGGERS = (
    "帮我查", "帮我搜", "帮我写", "帮我下载", "帮我找", "帮我看看",
    "帮我执行", "帮我运行", "帮我安装", "帮我打开",
    "查一下", "搜一下", "查天气",
    "写个文件", "写个文档", "生成文件", "保存到",
)

# 排除词：这些是现有聊天能力，不要误判成 Agent 任务
_EXCLUDE = ("唱歌", "讲故事", "讲笑话", "语音", "打电话", "发图片", "表情包",
            "放歌", "放音乐", "刷抖音", "看视频", "陪我", "聊天", "晚安", "早安")

# 目标宣言触发词：用户说"我想坚持XX"时，识别为长期目标
_GOAL_TRIGGERS = ("我想坚持", "我要坚持", "我想养成", "我要养成", "我打算", "我决定",
                  "我想每天", "我要每天", "我要开始", "我想开始", "我要早睡", "我想早睡",
                  "我要减肥", "我想减肥", "我要戒烟", "我想戒烟", "我想学", "我要学")


def detect_goal_declaration(text: str) -> str:
    """识别目标宣言（我想坚持XX / 我要养成XX），返回目标内容或空字符串。"""
    t = str(text or "").strip()
    if not t or len(t) > 60:
        return ""
    if any(k in t for k in _GOAL_TRIGGERS):
        return t
    return ""


def detect_agent_task(intent: dict, text: str) -> bool:
    """判断是否进入助手模式（走 Agent 循环）。

    优先信任第一次 DeepSeek（understanding 层）的判断：
    - 理解层判定「command + 明确要求执行动作」才进 Agent；
    - 理解层没跑起来（降级）时，才退回更严格的关键词兜底。
    """
    try:
        t = str(text or "").strip()
        if not t:
            return False
        # 显式排除已有聊天能力（唱歌/语音/陪伴等走现有短路）
        if any(k in t for k in _EXCLUDE):
            return False
        # ★ 2026-09-11：代码相关请求直通 Agent——理解层常把「看看你的代码」判成闲聊，
        # 导致她说"看不到代码"（她只有进 Agent 模式才拿得到 search_code/read_source 等工具）。
        if re.search(r"(代码|源码|程序逻辑)", t) and re.search(r"(看|读|查|翻|改|修|优化|分析|检查|重构|编译|跑)", t):
            return True
        # 积木工具的显式请求（学规则/设任务）也直通
        if any(k in t for k in ("learn_rule", "schedule_task", "给自己设", "记成规则", "沉淀成规则")):
            return True
        # 目标宣言直接进 Agent（由 loop 里记录目标）
        if detect_goal_declaration(t):
            return True
        # 理解层有结果：以它为准（第一次 DeepSeek 起作用）
        if intent and intent.get("intent"):
            if intent.get("intent") != "command":
                return False
            # 用理解层归一化后的 should_trigger_action：
            #   只有「用户明确要求做某事」才 true，confidence<60 已被强制置 false
            if not intent.get("should_trigger_action"):
                return False
            # 低置信度不触发（宁漏判不误判）
            if int(intent.get("confidence") or 0) < 60:
                return False
            return True
        # 理解层降级（没跑/没 key/解析失败）：严格关键词兜底
        return len(t) >= 4 and any(k in t for k in _TASK_TRIGGERS)
    except Exception:
        return False
