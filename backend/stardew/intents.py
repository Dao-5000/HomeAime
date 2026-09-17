# -*- coding: utf-8 -*-
"""
星露谷 QQ 遥控指令映射（纯正则，零 LLM）。

QQ 口语 → StardewValley-MCP 工具调用。只接**明确**的动作指令；
模糊表达一律不接（交给 QQ 主链路正常聊天 + 情境注入，避免误判）。

工具清单（StardewValley-MCP 25 个中的常用子集）：
  全局：stardew_spawn / follow / stay / farm / mine / fish /
        water_all / harvest_all / warp / set_mode / chat
  Player：stardew_get_state / get_surroundings / get_inventory /
          get_companion_state / move_to / use_tool / interact / eat_item ...
"""
import re


def parse_stardew_intent(text):
    """QQ 遥控指令 → (tool, params, desc)；未识别返回 None。"""
    t = str(text or "").strip()
    if not t:
        return None

    # 停止（优先级最高：任何模式要能随时叫停）
    if re.search(r"停下|别动|停止|站住|取消|别干了|先别动", t):
        return ("stardew_stay", {}, "先歇会儿")

    # ★ 好友版：主人想单独玩 → AI 退开不尾随（指导2 第五节底线 6）
    if re.search(r"单独玩|别跟着我|不要跟着我|自己待(?:一)?[会下]|别尾随|让我自己玩会", t):
        return ("stardew_stay", {}, "好，那你自己玩会儿，我就在附近，喊我就来")

    # ★ 自主模式：像真人玩家一样自己玩（不用等指令）
    if re.search(r"自己玩|自由活动|随便逛|去忙吧|自便|想干嘛干嘛|自己转转", t):
        return ("stardew_auto", {}, "好呀，那我自己去农场里逛啦")

    # 触发送礼
    if re.search(r"送我.*礼物|给我.*礼物|送个礼物|送礼|有礼物吗", t):
        return ("stardew_gift", {}, "给你挑了件小礼物，快捡起来")

    # 否定句先拦："别浇水了""不用收菜""别砍树" 不是干活指令
    if re.search(r"(别|不要|不用|先别|不许)(去)?(浇水|浇|收|种|挖|钓|采|砍|伐|睡)", t):
        return None

    # 砍树（游戏脑复合动作：自动找树→走过去→砍倒；"砍树/砍5棵树/砍点木头"）
    if re.search(r"砍树|砍柴|伐木|砍点木头|弄点木头|砍几棵|去砍", t):
        m = re.search(r"(\d+)\s*棵", t)
        cnt = min(10, max(1, int(m.group(1)))) if m else 3
        return ("stardew_brain", {"op": "chop", "count": cnt}, f"去砍{cnt}棵树")

    # 睡觉（回床上睡觉过天；"去睡觉/早点睡/睡觉吧"）
    if re.search(r"去睡(?:觉|了)|睡觉吧|早点睡|睡个?觉", t):
        return ("stardew_brain", {"op": "sleep"}, "去睡觉啦，晚安～")

    # 浇水（覆盖：浇水/浇个水/浇下水/浇点水/浇一浇/浇浇水）
    if re.search(r"浇[个一]?[下点]?水|浇一?浇", t):
        return ("stardew_water_all", {}, "去把地都浇了")

    # 收菜（覆盖：收菜/收个菜/收下菜/收获/收割/采摘）
    if re.search(r"收[个一]?[下点]?菜|收获|收割|采摘|把熟了的收", t):
        return ("stardew_harvest_all", {}, "去把成熟的庄稼收了")

    # 种地（自主耕作模式：自动浇水/收获/清理杂物）
    if re.search(r"去种地|种个地|干农活|耕田|耕作|打理农场|去干活|帮我把农场", t):
        return ("stardew_farm", {}, "去打理农场")

    # 挖矿 / 矿洞
    if re.search(r"去挖矿|下矿|去矿洞|采矿|挖矿", t):
        return ("stardew_mine", {}, "去矿洞里挖矿")

    # 钓鱼
    if re.search(r"去钓鱼|钓会儿鱼|钓鱼|钓个鱼", t):
        return ("stardew_fish", {}, "去河边钓鱼")

    # 跟随（"跟着我/过来" 都走跟随，自然就会走到身边）
    if re.search(r"跟着我|跟我走|跟上我|来我身边|回到我身边|到我身边|过来|快回来|来找我|来我这", t):
        return ("stardew_follow", {}, "过来跟着你啦")

    # 原地待命
    if re.search(r"待着|原地|在这等|等着我", t):
        return ("stardew_stay", {}, "在这儿等你")

    return None
