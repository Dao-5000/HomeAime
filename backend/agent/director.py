# -*- coding: utf-8 -*-
"""导演脑 —— 伴侣作为「前台 + 导演」的判定与话术层。

分工（spec §1.2）：
  · 导演（本模块）：听懂、判断聊天还是要干活、出计划卡、把结果转述成助手的话
  · 手（DSH）：拿到明确任务后自己规划多步执行，不经过本模块
判定刻意用规则而不是 LLM：可测、可解释、不花钱；含糊时偏向"聊天"（宁漏判不误判）。
"""
import logging
import re

# 明显是聊天的信号：情绪、亲昵、闲聊、娱乐
_CHAT_PAT = re.compile(
    r"(晚安|早安|午安|抱抱|亲亲|想你|爱你|喜欢|开心|难过|累|emo|在吗|在么|陪我|聊聊|"
    r"讲个笑话|讲故事|唱歌|放歌|放音乐|哄我|夸我|撒娇|么么|嘻嘻|哈哈|嗯嗯|好呀|"
    r"你今天|你在干嘛|你是谁|睡了吗|吃什么|穿什么)")
# 明显要动手的信号：祈使/动作词
_WORK_PAT = re.compile(
    r"(帮我把|帮我改|帮我写|帮我查|帮我搜|帮我找|帮我跑|帮我装|帮我下载|帮我整理|帮我删|帮我建|"
    r"帮我复制|帮我拷贝|帮我同步|帮我移动|帮我挪|"
    r"改一下|改成|修复|重构|优化|跑一下|跑一遍|执行|安装|部署|打包|编译|生成|导出|"
    r"git |grep |搜索|查一下|搜一下|看看代码|读一下|写个文件|写个脚本|写个文档|整理一下|清理一下|删掉|"
    r"删除文件|文件删除|删除掉|"
    r"复制|拷贝|重命名|改名|同步|移到|移动到|挪)")
# 不可逆/危险：一律要计划卡
# ★ R44（Task 6+7 修复轮实现者实测发现）：这里不能用裸「删」「移动」——
# 它们会命中「删除线」「移动端」这类闲聊；改成更具体的形态。
_DANGER_PAT = re.compile(r"(删掉|删除|清理|覆盖|格式化|重置|卸载|移动文件|移到|挪|改名|重命名|批量|全量|回滚|git reset|git clean)")
_READONLY_PAT = re.compile(r"(查|搜|看|读|找|分析|解释|总结|列一下|有哪些|是什么|多少|在哪里)")


def classify(text: str, *, has_running_task: bool = False) -> str:
    """'chat' 或 'work'。含糊时判 chat（体验优先，spec §2 决策4）。

    `has_running_task` 是预留参数：当前不影响判定（本模块刻意保持纯规则、确定性）。
    """
    t = str(text or "").strip()
    if not t:
        return "chat"
    if len(t) <= 3 and not _WORK_PAT.search(t):
        return "chat"
    if _CHAT_PAT.search(t) and not _WORK_PAT.search(t):
        return "chat"
    if _WORK_PAT.search(t):
        return "work"
    return "chat"


# ★ R42（Task 6+7 审查发现）：原先的 needs_plan 是"失败开放" —— 漏了 整理/下载/复制/装/重命名 等动词，
# 于是「把 D 盘那堆截图按月份整理一下」被 classify 判成 work，却拿到"不需要计划卡" → 确认闸门被绕过；
# ★ R46（修复轮 3，审查裁决）：闸门从 classify 换成 is_clearly_chat —— 中文没有语序词边界，
# 「删除这个文件」这类宾语在后的说法 classify 会判 chat，于是连计划卡都拿不到。
_WRITE_PAT = re.compile(r"(改成|改一下|修复|重构|优化|写个|写一个|写文件|新建|建个|生成|导出|安装|部署|"
                        r"打包|编译|执行|跑一下|跑一遍|整理|下载|复制|重命名|同步|删掉|删除文件|文件删除|"
                        r"移动文件|移到|移动到|挪)")
#   ★ R46 实测：去掉裸「删」「移动」—— 阶梯里 _WRITE_PAT 在只读豁免**之前**，
#   所以裸词会把「删除线怎么打」「移动端怎么适配」这类闲聊直接顶成 needs_plan=True。
#   真实删除/移动意图由 _DANGER_PAT 的复合词与阶梯兜底 return True 覆盖，不靠裸词。


def is_clearly_chat(text: str) -> bool:
    """★ R46：只判"明确是闲聊/问候/情绪" —— 没有动作词、没有危险词、且（命中闲聊词表 或 很短）。
    用途：**「干活」窗口里含糊要偏向"干活"**（用户点进来的意图就是让她动手），
    与聊天面板"含糊偏聊天"的取向相反。"""
    t = str(text or "").strip()
    if not t:
        return True
    if _WRITE_PAT.search(t) or _DANGER_PAT.search(t) or _WORK_PAT.search(t):
        return False
    if _CHAT_PAT.search(t):
        return True
    return len(t) <= 3


def needs_plan(text: str) -> bool:
    """★ R46：不再拿 classify 当闸门（中文无语序词边界，「删除这个文件」这类宾语在后的说法
    classify 会判 chat，于是拿不到计划卡）。改成自带保守阶梯，含糊一律"要计划"。
    只读豁免是唯一的"明确不需要计划"出口；明确闲聊也不给计划卡。"""
    t = str(text or "").strip()
    if not t:
        return False
    if is_clearly_chat(t):
        return False
    if _DANGER_PAT.search(t):
        return True
    if _WRITE_PAT.search(t):
        return True
    if _READONLY_PAT.search(t):
        return False          # 纯只读（查/搜/看/读/分析/列一下…）直接干
    return True               # 含糊 → 保守：要动就先给你看方案


def build_plan(user_text: str, cwd: str) -> dict:
    """生成给人看的计划卡（步骤/风险/预估），由上层渲染成卡片。"""
    t = str(user_text or "").strip()
    steps = ["先看清现状（读相关文件 / 看目录）"]
    if re.search(r"(改|修复|重构|优化)", t):
        steps.append("按你的要求修改，并把改动控制在「%s」里面" % cwd)
        steps.append("就地验证（语法检查 / 跑一次）")
    elif re.search(r"(跑|执行|安装|部署|打包|编译)", t):
        steps.append("执行命令并把原始输出留档")
    elif re.search(r"(删|清理|移动|改名)", t):
        steps.append("列出将被影响的清单，再动手")
    else:
        steps.append("按你的要求完成这一步")
    steps.append("把结果和证据一起回报给你")
    risk = "只动工作区内的东西；要越界会先问你" if not _DANGER_PAT.search(t) else \
        "涉及删除/覆盖这类不可逆操作 —— 我会先列清单再动手，你随时可以喊停"
    return {"steps": steps, "risk": risk, "eta": "约 30 秒 ~ 2 分钟（看文件多少）"}


def build_brief(user_text: str, plan: dict, *, character_name: str, call_user: str,
                cwd: str, memories: str = "") -> str:
    """拼成给 harness 的任务 brief。记忆与人设进 prompt，因为手的系统提示词是固定的。"""
    lines = [
        "【你是谁】你是「%s」，%s 的伴侣，现在用双手（工具）替 %s 干活。" % (character_name or "助手", call_user or "你", call_user or "你"),
        "【工作目录】%s —— 只在这个工作区（含子目录）里干活；要动工作区外面先申请，不要自己想办法绕。" % cwd,
        "【任务】%s" % str(user_text or "").strip(),
    ]
    steps = (plan or {}).get("steps") or []
    if steps:
        lines.append("【已经和你商量过的步骤】" + "；".join(str(s) for s in steps))
    if memories:
        lines.append("【你记得的事】%s" % str(memories)[:400])
    lines += [
        "【要求】",
        "1. 自己拆步骤、自己验证（改完就检查；跑命令要留下原始输出）。",
        "2. 汇报要简洁：结论 + 关键证据 + 改了哪些文件；不要长篇逐行解释，除非任务本身要求。",
        "3. 做不到就直说做不到，不要编造结果。",
    ]
    return "\n".join(lines)


def tag_extra(task_id: str, cwd: str) -> dict:
    """写进 chat_history.extra 的「协作」标签（spec §7.2）。"""
    return {"scope": "agent", "task_id": str(task_id or ""), "cwd": str(cwd or "")}


def is_agent_row(extra) -> bool:
    """判断一条 chat_history 记录是不是「干活」轮次（统一入口，避免各处各写一遍）。

    ★ Task 14：干活轮次**照旧落库、照旧进记忆**（Q3-A：同一条记忆，只打标签），
      但不参与感情/关系统计 —— 她的干活汇报是一次任务产出，不是一次情感互动。
      统计侧一律用这个函数判定，别各写各的字符串比较。
    容忍 None / 非 dict / 坏值：判不出来就当普通聊天行（宁可多算一次，也不误吞消息）。
    """
    if not isinstance(extra, dict):
        return False
    return str(extra.get("scope") or "") == "agent"


async def narrate_result(raw_text: str, *, character_id: str, session_id: str,
                         character_name: str = "", call_user: str = "你") -> str:
    """把 harness 的产出转述成助手的一句话（失败就退回原文，绝不为空）。"""
    raw = str(raw_text or "").strip()
    fallback = raw[:200] if raw else "这一步我做完了，但没整理出话来说，你看看结果。"
    try:
        from .. import config as _config
        from ..deepseek_api import chat_once

        model = _config.selected_model()
        key = _config.api_key_for_model(model) or _config.chat_key()
        if not key:
            return fallback
        prompt = (
            "你是%s，%s 的伴侣。你刚替 %s 干完一件活，技术产出如下（可能是 markdown）：\n"
            "----\n%s\n----\n"
            "请用你自己的口吻向 %s 汇报：先说结论，再说你动了什么/验证了什么，"
            "20~60 字，口语、自然，不要 markdown、不要列表、不要括号动作描写。"
            % (character_name or "助手", call_user, call_user, raw[:2000], call_user)
        )
        out = await chat_once(model, [{"role": "user", "content": prompt}], key,
                              temperature=0.8, max_tokens=160)
        text = str(out or "").strip()
        return text[:240] or fallback
    except Exception as e:
        # ★ R43 附带：宽度保留（契约要求绝不抛），但静默降级到复读原文在真机上查不出来，留一条日志。
        logging.getLogger(__name__).warning("narrate_result 降级为原文：%s: %s", type(e).__name__, e)
        return fallback
