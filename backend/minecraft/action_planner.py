# -*- coding: utf-8 -*-
"""
动作规划器（Minecraft Bot）
把 LLM 的自然语言输出解析成结构化动作序列（白名单），Bot 执行。

LLM 输出格式（JSON）：
{
  "chat": "要说的话（可选）",
  "actions": [ {"type": "chop_tree", "params": {"count": 3}}, ... ]
}
"""
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 合法动作白名单（防止 LLM 乱填 / 注入）
VALID_ACTIONS = {
    # 移动
    "move_to", "follow_player", "stop_follow", "go_to_player",
    # 交互
    "chat",
    # 采集
    "mine_block", "mine_at", "chop_tree", "collect_drops",
    # 建造
    "place_block",
    # 战斗
    "attack_nearest_hostile", "attack_entity", "stop_combat",
    # 生存
    "sleep", "eat",
    # 其他
    "wait", "stop",
    # 情绪表达：只做肢体动作，不改变世界状态
    # ★ 新增动作必须三处同步，否则会出现"白名单放行但 bot 不执行"的静默失效：
    #   1) 本集合  2) minecraft_bot/actions.js 的同名方法
    #   3) minecraft_bot/bot.js 的 _runAction switch 分支
    "wave", "nod", "shake_head", "jump",
}

ACTION_SCHEMA = """请严格按以下 JSON 格式输出，不要输出其他内容：
{
  "chat": "（要在游戏里说的话，简短口语，不超过30字，不说就填空字符串）",
  "actions": [
    { "type": "动作类型", "params": {} }
  ]
}
可用动作类型及参数：
move_to {x, y, z, range?}
follow_player {player, distance?}
stop_follow {}
go_to_player {player, range?}
chat {message}
chop_tree {count?}
mine_block {block, count?}
mine_at {x, y, z}
collect_drops {radius?}
place_block {block, x, y, z}
attack_nearest_hostile {}
attack_entity {entity}
stop_combat {}
sleep {}
eat {}
wait {sec?}
stop {}

情绪表达类（配合你说的话做肢体动作，让情绪更真实，可以和 chat 同时用）：
wave {}           # 挥手：打招呼、开心
nod {}            # 点头：认同、答应
shake_head {}     # 摇头：否定、无奈、拿你没办法
jump {}           # 原地跳一下：兴奋、高兴
actions 可以是空数组 []。"""


def parse_llm_output(raw: str) -> Optional[dict]:
    """从 LLM 原始输出解析决策 JSON，多层兜底。"""
    if not raw or not raw.strip():
        return None

    for fn in (_try_direct, _try_braces, _try_code_block, _try_greedy):
        result = fn(raw)
        if result is not None:
            return _validate(result)
    logger.warning(f"[ActionPlanner] JSON 解析彻底失败: {str(raw)[:300]}")
    return None


def _try_direct(raw: str) -> Optional[dict]:
    try:
        d = json.loads(raw.strip())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _try_braces(raw: str) -> Optional[dict]:
    m = re.search(r"\{[\s\S]+\}", raw)
    if not m:
        return None
    try:
        d = json.loads(m.group())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _try_code_block(raw: str) -> Optional[dict]:
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if not m:
        return None
    return _try_direct(m.group(1).strip())


def _try_greedy(raw: str) -> Optional[dict]:
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        d = json.loads(raw[start:end + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _validate(data: dict) -> dict:
    """校验并净化 LLM 输出（白名单过滤）。"""
    result = {
        "chat": str(data.get("chat", "") or ""),
        "actions": [],
    }
    for action in data.get("actions", []) or []:
        if not isinstance(action, dict):
            continue
        t = str(action.get("type", "") or "")
        if t not in VALID_ACTIONS:
            logger.warning(f"[ActionPlanner] 非法动作，忽略: {t}")
            continue
        params = action.get("params", {})
        result["actions"].append({
            "type": t,
            "params": params if isinstance(params, dict) else {},
        })
    return result
