# -*- coding:utf-8 -*-
"""
人格稳定器管理：
  根据角色设定和长期互动，总结AI应该保持的表达方式，
  包括核心性格、说话方式、常用表达、禁止表达、情绪表达、相处模式。
  注入 system prompt 让 AI 保持人格一致性，不会突然变成客服或百科助手。
"""
import json

from . import db, config
from .deepseek_api import chat_once


PERSONALITY_SYSTEM = """
你负责维护AI伴侣的人格一致性。

根据角色设定和长期互动，
总结AI应该保持的表达方式。

输出JSON：

{
"core_personality":"",
"speaking_style":"",
"favorite_phrases":"",
"forbidden_phrases":"",
"emotional_expression":"",
"relationship_behavior":""
}

要求：

core_personality:
固定性格，例如：
温柔、调皮、成熟、理性

speaking_style:
语言习惯，例如：
短句、多表情、偶尔撒娇

favorite_phrases:
常用表达

forbidden_phrases:
禁止出现的客服化表达

emotional_expression:
表达开心、担心、生气的方式

relationship_behavior:
面对用户时的相处模式

不要改变已有稳定人格。
不要生成夸张设定。
"""


async def update_personality(
    session_id,
    character_id="default",
    messages=None
):
    """根据最近对话更新人格稳定器，保持人格一致性。"""
    key = config.memory_key()

    if not key:
        return

    old = db.get_personality_state(
        session_id, character_id
    )

    text = "\n".join(
        (
            "用户："
            if m.get("role") == "user"
            else "AI："
        )
        +
        str(
            m.get(
                "content",
                ""
            )
        )
        for m in messages[-20:]
    )

    result = await chat_once(
        config.memory_extract_model(character_id),
        [
            {
                "role": "system",
                "content": PERSONALITY_SYSTEM
            },
            {
                "role": "user",
                "content":
                "已有人格：\n"
                +
                json.dumps(
                    old,
                    ensure_ascii=False
                )
                +
                "\n\n互动：\n"
                +
                text
            }
        ],
        key,
        temperature=0.2,
        max_tokens=600
    )

    try:
        data = json.loads(
            result
        )

        db.update_personality_state(
            session_id, character_id,
            **data
        )

    except Exception:
        pass


def personality_guard(
    reply,
    personality
):
    """
    人格漂移检测：检查回复中是否包含禁止表达。
    返回 True 表示通过检测，False 表示包含禁止表达需要重新生成。

    ★ 2026-09-17 修：第二个参数**同时接受 dict 和 str**。
      · dict：`{"forbidden_phrases": "说教、客服腔"}`（非流式链路 main.py:3646 的用法）
      · str ：直接就是禁止词串（流式链路 main.py:9141 的用法）
      之前只处理 dict，而流式链路传的是 `str(角色卡.get("personality"))` ——
      每轮 `AttributeError: 'str' object has no attribute 'get'` 被 except 吞掉，
      结果**主力聊天链路上人格漂移检测 100% 失效**（trace 里每条 pipeline 都是
      `failed:["guard"]`）。容忍两种类型后，这条检测才真正开始工作。
    """
    if not personality:
        return True

    if isinstance(personality, dict):
        forbidden = personality.get("forbidden_phrases", "")
    elif isinstance(personality, str):
        # 字符串按"禁止词串"理解（与 dict 里 forbidden_phrases 的语义一致）
        forbidden = personality
    else:
        return True

    if not forbidden or not isinstance(forbidden, str):
        return True

    for word in forbidden.replace(",", "、").replace("，", "、").split("、"):
        word = word.strip()
        if word and word in reply:
            return False

    return True


# ── 五维的"回中趋势"：**单一权威**（所有写入者必须走这里）──────────────
# ★ 为什么单独抽出来（2026-09-17 真机取证）：
#   我原先只在 `update_five_dim` 里加了均值回归，结果真机五维**仍然全顶在 ±20**
#   （warmth 20 / dominance -20 / humor 20 / initiative 20 / attachment 20，
#    updated_time 就在刚刚）。原因是存在**第二个写入者**：
#   `reflection/strategy.py: apply_reflection_to_personality_state` 自己算
#   `old + delta` 再 clamp 到 ±20 —— 而反思只产出**正**delta，
#   于是每次都往上顶、顶到 20 就锁死，回中趋势完全被绕过。
#   两个写入者各写各的 = 谁都不保证"有界且可回落"。
#   现在把规则收成一处，两边都调它。
# ★ 2026-09-17 标定（第二次修）：光"加回中趋势"不够，还得让**平衡点明显低于上限**。
#   算平衡点：稳态时 x ≈ D·x + delta  →  x* = delta/(1-D)
#     · D=0.98, delta=+2 → x* = 100 → 撞上限 20 → 等于没修（第一次就是这么失败的）
#     · D=0.80, delta=+2 → x* = 10    → 顶不到边界，维度真的有区分度
#     · 负向信号 delta≈-2 → x* = -10，两侧对称
#   所以取 D=0.80。副作用是"遗忘"更快（约 3 次无信号更新回到一半），
#   对"越聊越像人"是好事：口味变了，语气也该跟着回来。
#   ★ 同时把取整从 `int()`（向下截断）改成**四舍五入**：
#     截断会让 19 ✗ 0.8 = 15.2 还好，但 1 ✗ 0.8 = 0.8 → int → 1 → 小值永远掉不下去，
#     形成"1 的地板"。四舍五入才能让衰减对小值也生效。
FIVE_DIM_DECAY = 0.80      # 每次更新先向 0 收缩（稳态 ≈ ±10，远离 ±20 边界）
FIVE_DIM_MIN, FIVE_DIM_MAX = -20, 20


def revert_five_dim(value: int) -> int:
    """把某个维度的当前值向 0 收缩一步（乘法收缩：小值影响小、大值影响大）。"""
    try:
        v = float(value or 0)
    except Exception:
        v = 0.0
    # 四舍五入而非截断：截断会在 ±1 处形成"掉不下去"的地板
    return int(v * FIVE_DIM_DECAY + (0.5 if v >= 0 else -0.5))


def clamp_five_dim(value: int) -> int:
    return max(FIVE_DIM_MIN, min(FIVE_DIM_MAX, int(value or 0)))


def apply_five_dim_delta(current: int, delta: int) -> int:
    """标准一步更新：**先叠加本轮增量、再回中、最后钳制**。

    顺序很关键（2026-09-17 第三次修正）：
      · 先回中再叠加（原顺序）会让"新证据"被衰减吃掉一部分 ——
        实测 -20 收到 -1（负反馈）后变成 -17，**方向上反而变温和了**，
        用户看到的就是"越批评越不在乎"，很反直觉。
      · 先叠加再回中：本轮信号**全量生效**，回中作用于"累积出来的极端值"。
        这才是"长期缓慢漂移"的本意 —— 单次信号永远算数，极端值才需要持续证据维持。

    所有影响五维的代码都必须调它 —— 这样"越聊越像人"才是
    **缓慢漂移 + 可回落 + 单次信号有效**，而不是被某个写入者钉死在边界上。
    """
    return clamp_five_dim(revert_five_dim(int(current or 0) + int(delta or 0)))


async def update_five_dim(
    session_id: str,
    character_id: str = "default",
    feedback_signal: str = "neutral",   # positive/negative/neutral
    reply_length: int = 0,              # AI本次回复字数
    user_response_time: float = 0.0,    # 用户本次回复间隔秒数（0=未知）
    proactive_replied: bool = False,    # 主动推送是否被回复
):
    """
    五维人格delta从用户行为反馈推算，不让LLM自评
    每次对话后调用，累计偏移量（有上下限）

    五维定义：
    warmth_delta:      温暖度（正=更温柔体贴，负=更冷淡）
    dominance_delta:   主导度（正=更主动引导，负=更顺从跟随）
    humor_delta:       幽默度（正=更爱开玩笑，负=更认真）
    initiative_delta:  主动性（正=更频繁主动找用户，负=被动等待）
    attachment_delta:  依恋度（正=更黏人，负=更独立）
    """
    old = db.get_personality_state(session_id, character_id) or {}

    # 读旧值（默认0）
    warmth     = int(old.get("warmth_delta",     0) or 0)
    dominance  = int(old.get("dominance_delta",  0) or 0)
    humor      = int(old.get("humor_delta",      0) or 0)
    initiative = int(old.get("initiative_delta", 0) or 0)
    attachment = int(old.get("attachment_delta", 0) or 0)

    # ── 推算规则（每次微小偏移，长期积累才有效果）

    # 1. feedback信号
    if feedback_signal == "positive":
        warmth     += 1
        attachment += 1
    elif feedback_signal == "negative":
        warmth     -= 1
        dominance  -= 1   # 负反馈→降低主导倾向

    # 2. 用户回复速度（越快说明越投入）
    if 0 < user_response_time <= 30:       # 30秒内秒回
        attachment += 1
        humor      += 1   # 活跃对话→幽默感上升
    elif user_response_time > 3600:        # 1小时以上才回
        initiative += 1   # 用户慢回→AI主动性补偿上升
        attachment -= 1   # 依恋度微降（对方不够热情）

    # 3. AI回复长度（越长越主导）
    if reply_length > 200:
        dominance  += 1
    elif reply_length < 30:
        dominance  -= 1

    # 4. 主动推送被回复
    if proactive_replied:
        initiative += 1
        attachment += 1

    # ── 回中趋势（★ 2026-09-17 新增，修"饱和"）
    #   真机现场：骨子那行五维**全部顶在 ±20**（warmth 20 / dominance -20 /
    #   humor 20 / initiative 20 / attachment 20），而 clamp 上限就是 ±20 ——
    #   于是任何新的反馈都推不动它，学习对语气**永久失去影响**；
    #   注入侧又只看"绝对值≥5"（character_manager._inject_five_dim），
    #   所以注入出来的其实是一组恒定常量。
    #   加一个温和的均值回归：每次更新先向 0 收缩 2%，再叠加本轮增量。
    #   · 想顶到 ±20 需要**持续**的正反馈，而不是一次性刷上去；
    #   · 反馈若停止（比如用户不再秒回），维度会慢慢回到基础值 —— "像人"的遗忘；
    #   · 仍保留 ±20 的硬钳制，极端漂移不会失控。
    #   为什么用乘法收缩而不是减法：乘法对小值影响小、对大值影响大，
    #   正好是"越极端越难维持"的直觉，也更不容易在 0 附近抖动。
    #   ★ 规则本体已抽到模块级 `apply_five_dim_delta`（单一权威），这里只调用它，
    #     避免"反思那条写入者绕过回中趋势"的事故重演。
    warmth     = apply_five_dim_delta(warmth, 0)
    dominance  = apply_five_dim_delta(dominance, 0)
    humor      = apply_five_dim_delta(humor, 0)
    initiative = apply_five_dim_delta(initiative, 0)
    attachment = apply_five_dim_delta(attachment, 0)

    # ── 写回db（只写五维字段，不触碰6个文本字段）
    db.update_five_dim(
        session_id, character_id,
        warmth_delta=warmth,
        dominance_delta=dominance,
        humor_delta=humor,
        initiative_delta=initiative,
        attachment_delta=attachment,
    )
