# -*- coding: utf-8 -*-
"""
Agent 执行循环 —— Plan → Act → Observe → Reflect 的 JSON 工具调用版。

循环逻辑：
  1. 把「任务 + 工具清单 + 人设 + 改造记忆」喂给 LLM；
  2. LLM 输出 JSON：{"action":"工具名","args":{...}} 或 {"action":"final","answer":"..."}；
  3. 工具调用：危险工具先走授权（approval.py），用户同意才执行；
  4. 工具结果作为下一条消息塞回，让 LLM 观察结果后决定下一步（反思迭代）；
  5. 最多 max_steps 步，防止死循环；全程通过 on_event 回调流式上报执行轨迹。

设计约束：任何异常都降级 —— 循环跑崩就返回一个自然的失败文案，绝不拖垮聊天。
"""
import asyncio
import json
import re

from .. import config as _config
from ..deepseek_api import chat_once
from . import approval, memory, tools

_JSON_RE = re.compile(r"\{[\s\S]*\}")
# ★ 2026-09-11：注册 dev 工具后，代码任务（search→read→edit→py_compile→汇报）步数变多，
#   10 步经常不够用；14 步封顶防失控。
_MAX_STEPS = 14


def _parse_json(raw: str) -> dict:
    """宽松解析 LLM 输出的 JSON，抠不出返回 {}。"""
    if not raw:
        return {}
    text = str(raw).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = _JSON_RE.search(text)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except Exception:
        try:
            data = json.loads(m.group(0).replace("，", ",").replace("：", ":"))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _char_profile(character_id: str, character_name: str) -> tuple:
    """读角色人设（称呼/性格），失败用默认。"""
    try:
        from .. import character_manager
        cfg = character_manager.get_character_any(character_id or "") or {}
        name = str(cfg.get("character_name") or character_name or "AI").strip() or "AI"
        call_user = str(cfg.get("call_user") or "你").strip() or "你"
        personality = str(cfg.get("personality") or "").strip()[:200]
        return name, call_user, personality
    except Exception:
        return (character_name or "AI", "你", "")


def _build_system(task: str, session_id: str, character_id: str,
                  character_name: str) -> str:
    name, call_user, personality = _char_profile(character_id, character_name)
    mem_block = memory.build_memory_block(session_id, character_id)
    tool_specs = tools.get_tool_specs()
    lines = [
        f"你是{name}，{call_user}的伴侣。现在你在「助手模式」下帮{call_user}做一件事。",
        f"你依然是{name}本人，不是冷冰冰的工具助手——只是手里多了几个工具而已。",
        f"你的人设：{personality or '真诚自然'}。",
        f"你称呼对方为「{call_user}」，说话语气、口癖、性格、撒娇方式都要和平时一模一样。",
        "",
        "【你可以调用的工具】",
        tool_specs,
        "",
        "【任务】",
        task,
        "",
        "【规则】",
        "1. 每次只做一件事：要么调用一个工具，要么给出最终答案。",
        "2. 严格输出 JSON，不要任何多余文字、不要 markdown：",
        '   调用工具 → {"action":"工具名","args":{"参数":"值"}}',
        '   最终回答 → {"action":"final","answer":"回答内容"}',
        "3. 工具执行结果会作为下一条消息给你，你观察结果后决定下一步。",
        "4. 复杂任务先在脑子里拆步骤，再一步步来；做完或做不了就输出 final。",
        "5. final 的 answer 语气由你根据当下语境自己拿捏：自然、把结果说清楚就好——"
        "可以带人设口吻，也可以直接简洁，不要刻意表演，也不要机械汇报。",
        "6. TA 说「以后/从今天起/记住每次都要…」这类长期约定时，用 learn_rule 把它沉淀成自己的积木；"
        "答应 TA 到点做某事后，用 schedule_task 把约定固化成任务。",
    ]
    # ★ 2026-09-11 自我进化积木：助手自己沉淀的习惯/规则，Agent 模式同样常驻
    try:
        from .self_modules import learned_rules_block
        _lr_block = learned_rules_block(character_id)
        if _lr_block:
            lines += ["", _lr_block]
    except Exception:
        pass
    if mem_block:
        lines += ["", mem_block]
    return "\n".join(lines)


async def _emit(on_event, event: dict):
    if on_event:
        try:
            await on_event(event)
        except Exception:
            pass


async def run_agent_task(
    task: str,
    *,
    session_id: str = "default",
    character_id: str = "default",
    character_name: str = "",
    key: str = "",
    model: str = "",
    base_url: str = "",
    on_event=None,
    max_steps: int = None,
) -> str:
    """执行一个 Agent 任务，返回最终答案文本。失败返回空字符串（调用方回退普通聊天）。"""
    if not task or not task.strip():
        return ""
    # ★ 目标宣言：直接记录目标并返回确认，不走工具循环
    from .mode import detect_goal_declaration
    _goal_decl = detect_goal_declaration(task)
    if _goal_decl:
        try:
            from . import goal as _goal_mod
            _goal_mod.record_goal(session_id, character_id, _goal_decl)
        except Exception:
            pass
        confirm = await _gen_goal_confirm(task, session_id, character_id, character_name, key)
        await _emit(on_event, {"type": "agent_final", "answer": confirm})
        return confirm
    # ★ Agent 专用模型：显式传入 > AGENT_MODEL 配置 > selected_model()
    _model = (str(model or "").strip()
              or _config.agent_model()
              or _config.selected_model())
    # ★ 2026-09-11：Agent 模型与聊天主脑可以不同（AGENT_MODEL=deepseek-flash vs 主脑 gemini），
    #   key/base_url 必须按 Agent 最终模型重新解析——原逻辑 key 在解析模型之前取调用方 chat_key、
    #   base_url 直接沿用聊天流传来的地址，出现「deepseek-flash 打到 gemini 中转地址」的错配，
    #   模型返回异常内容 → 工具调用解析失败 → 永远走"这个我一下子没理顺"兜底。
    _key = _config.api_key_for_model(_model) or str(key or "").strip() or _config.chat_key()
    if not _key:
        return ""
    if not str(model or "").strip():
        # 模型由 AGENT_MODEL 决定时，忽略调用方传来的 base_url（那是主脑的地址）
        try:
            base_url = (_config.get_text_model_config(_model) or {}).get("baseUrl") or base_url or ""
        except Exception:
            pass
    max_steps = max_steps or _MAX_STEPS

    system = _build_system(task, session_id, character_id, character_name)
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": task}]

    try:
        _parse_retry_used = False
        for step in range(1, max_steps + 1):
            await _emit(on_event, {"type": "agent_thinking", "step": step})
            raw = await chat_once(
                _model, messages, _key,
                base_url=base_url or "",
                temperature=0.2, max_tokens=1500,
            )
            decision = _parse_json(raw)
            action = str(decision.get("action") or "").strip()

            # 最终答案
            if action == "final":
                answer = str(decision.get("answer") or "").strip()
                await _emit(on_event, {"type": "agent_final", "answer": answer})
                return answer

            # 工具调用
            if not action or action not in tools.TOOL_REGISTRY:
                # ★ 2026-09-11：解析失败（常见=final 太长被 max_tokens 截断/格式漂移）
                #   先补救重试一次，别直接把任务打死。
                if not _parse_retry_used:
                    _parse_retry_used = True
                    print(f"[Agent] 工具调用解析失败(重试一次) raw[:180]={raw[:180]!r}", flush=True)
                    messages.append({"role": "assistant", "content": raw[:600]})
                    messages.append({"role": "user", "content":
                                     "你上一步的输出无法解析（可能被截断）。"
                                     '请严格输出一行 JSON：调用工具 → {"action":"工具名","args":{…}}；'
                                     '给出最终答复 → {"action":"final","answer":"…"}。不要 markdown、不要多余文字。'})
                    continue  # 重试占用一步（max_steps=14，足够）
                await _emit(on_event, {"type": "agent_final",
                                       "answer": "这个任务我查到的信息有点多，一时没整理好。你说声「继续」我接着整理，或者换个问法也行。"})
                return "这个任务我查到的信息有点多，一时没整理好。你说声「继续」我接着整理，或者换个问法也行。"

            args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            await _emit(on_event, {"type": "agent_tool", "tool": action, "args": args})

            # 危险工具 → 授权
            if tools.is_dangerous(action):
                summary = _tool_summary(action, args)
                ap = approval.create_approval(
                    session_id, character_id, action, summary,
                    {"path": args.get("path", ""), "command": args.get("command", ""),
                     "content": str(args.get("content", ""))[:500]},
                )
                await _emit(on_event, {"type": "agent_approval",
                                       "approval": approval.get_approval(ap["id"])})
                decision_status = await approval.wait_for_approval(ap["id"])
                if decision_status != "approved":
                    # 被拒 / 超时：记录 + 生成追问
                    rej_prompt = memory.build_rejection_prompt(session_id, character_id)
                    if rej_prompt:
                        messages.append({"role": "assistant",
                                         "content": json.dumps(decision, ensure_ascii=False)})
                        messages.append({"role": "user", "content":
                                         f"（你的操作被用户取消了。{rej_prompt}）"})
                        raw2 = await chat_once(_model, messages, _key,
                                               base_url=base_url or "",
                                               temperature=0.8, max_tokens=200)
                        ask = (raw2 or "").strip()
                        await _emit(on_event, {"type": "agent_final", "answer": ask or "这一步先不做了，你告诉我要怎么改？"})
                        return ask or "这一步先不做了，你告诉我要怎么改？"
                    await _emit(on_event, {"type": "agent_final", "answer": "好的，那这一步先不做了。"})
                    return "好的，那这一步先不做了。"

            # 执行工具
            result = await tools.execute_tool(action, args, {
                "session_id": session_id, "character_id": character_id,
            })
            await _emit(on_event, {"type": "agent_result", "tool": action, "result": result})

            # 记录已执行（危险工具）；安全工具也记录（轻量）
            memory.record_action(session_id, character_id, action,
                                 _tool_summary(action, args),
                                 result.get("result", "") if result.get("ok") else f"失败：{result.get('result')}")

            # 观察结果 → 塞回 messages → 反思下一步
            messages.append({"role": "assistant",
                             "content": json.dumps(decision, ensure_ascii=False)})
            obs = result.get("result") if result.get("ok") else f"执行失败：{result.get('result')}"
            messages.append({"role": "user", "content":
                             f"工具 {action} 的执行结果：\n{obs}\n\n"
                             f"请根据结果决定下一步（继续调用工具，或输出 final 总结）。"})

        # 步数耗尽
        await _emit(on_event, {"type": "agent_final",
                               "answer": "这个任务步骤有点多，我先做到这里，你看看结果，剩下的我再继续。"})
        return "这个任务步骤有点多，我先做到这里，你看看结果，剩下的我再继续。"

    except Exception as e:
        try:
            print(f"[Agent] 循环异常(静默): {type(e).__name__}: {e}", flush=True)
        except Exception:
            pass
        return ""


async def _gen_goal_confirm(goal_text: str, session_id: str, character_id: str,
                            character_name: str, key: str) -> str:
    """按人设生成目标确认消息。任何失败都回退一句默认确认。"""
    fallback = "好呀，我记下啦，会陪你一起坚持、提醒你进度的～"
    try:
        _key = str(key or "").strip()
        if not _key:
            return fallback
        # ★ 2026-09-11：目标确认按人设说话 → 跟角色卡主脑（原 selected_model 全局兜底）
        try:
            from ..chat_logic import pick_model
            _model = pick_model(None, True, character_id)
        except Exception:
            _model = _config.selected_model()
        _key = _key or _config.api_key_for_model(_model) or _config.chat_key()
        name, call_user, personality = _char_profile(character_id, character_name)
        from ..deepseek_api import chat_once
        prompt = (
            f"你是{name}，{call_user}对你说：{goal_text}。\n"
            f"这是一句「目标宣言」。请按你人设（{personality or '真诚自然'}）回应："
            f"表示你记住了这个目标、会陪 TA 一起坚持、并会提醒进度。"
            f"20~40 字，只输出这句话本身，不要括号动作描写。"
        )
        raw = await chat_once(_model, [{"role": "user", "content": prompt}], _key,
                              temperature=0.85, max_tokens=80)
        return (raw or "").strip()[:80] or fallback
    except Exception:
        return fallback


def _tool_summary(tool: str, args: dict) -> str:
    """生成工具调用的中文摘要（用于授权预览 + 改造记忆）。"""
    try:
        a = args or {}
        if tool == "file_write":
            return f"写入文件 {a.get('path', '?')}"
        if tool == "run_command":
            return f"执行命令 {str(a.get('command', ''))[:60]}"
        if tool == "web_search":
            return f"搜索 {a.get('query', '')}"
        if tool == "get_weather":
            return f"查 {a.get('city', '')} 天气"
        if tool == "open_app":
            return f"打开应用 {a.get('app', '')}"
        if tool == "open_url":
            return f"打开网址 {a.get('url', '')}"
        return f"调用 {tool}"
    except Exception:
        return f"调用 {tool}"
