# -*- coding:utf-8 -*-
"""
AI State Updater v2.0（LLM驱动·多角色隔离·不OOC）
老公定制：用语义层/原话让 LLM 提炼 AI 内心，不再近视眼匹配。

核心变化：
  - 废掉硬编码关键词，改用 deepseek_api.chat_once 让 LLM 提取内心状态
  - character_id 真传进 db，多角色不串台
  - interaction_notes 只给任务指引（该做什么/禁做什么），严禁写死称呼台词，
    让 AI 用自己的人设语气表达，御姐高冷/软妹撒娇都不串人设
  - LLM 不可用（无 key / 网络异常 / 解析失败）时降级到关键词兜底
"""
import json
import asyncio
from ... import db
from backend.loop_compat import get_loop

# ↓ 项目里语义分析用的非流式 LLM 调用（async，签名 chat_once(model, messages, api_key, ...)）
try:
    from ...deepseek_api import chat_once as _llm_call
except ImportError:
    _llm_call = None

# api_key 从 config 取（环境变量 > .env > config.json）
try:
    from ..config import api_key as _get_api_key
except ImportError:
    try:
        from ...config import api_key as _get_api_key
    except ImportError:
        _get_api_key = lambda: ""


def _fallback_keyword_update(user_message, relationship):
    """LLM 挂了时的兜底（原逻辑精简版）"""
    inner = {}
    affection = relationship.get("affection", 0) or 0
    closeness = relationship.get("closeness", 0) or 0
    if affection > 80 or closeness > 80:
        inner["current_mood"] = "happy"
    low = ["难过", "累", "压力", "伤心", "焦虑", "烦", "痛苦", "失落", "沮丧"]
    if any(x in str(user_message) for x in low):
        inner["current_mood"] = "concerned"
        inner["recent_focus"] = "用户近期情绪低落，需要更多关心"
    return inner


async def _llm_extract(user_message, relationship, character_id="default"):
    """真正调 LLM 提取内心状态（async）。返回 dict 或 None。"""
    if not _llm_call:
        return None
    from ... import config as _c
    key = _c.memory_key() or _get_api_key() or ""
    if not key:
        return None

    aff = relationship.get("affection", 50)
    clo = relationship.get("closeness", 50)
    prompt = f"""你是AI伴侣的内心状态提取器。根据用户的话和当前关系数值，输出严格JSON，不要任何解释。
字段：
current_mood: happy/concerned/touched/worried/neutral/excited 之一
recent_focus: 简短描述AI最近该关注用户什么（20字内）
interaction_notes: 给AI的回复任务指引，只写【该做什么、禁做什么】，严禁写死任何称呼/台词，必须让AI用自己的人设语气表达（如"用户低落，先共情再回应，保持你既定称呼与性格"）
user_impression: 对用户的印象（20字内）
wanted_topics: 想和用户聊的话题（20字内）
hidden_agenda: AI此刻没说出口的小心思/潜台词（如"其实想多陪陪TA"），20字内，没有则空
base_mood: AI此刻自发情绪底色（不受用户影响，像生理期/工作累的随机漂移）：calm/low/playful/warm/sulky 之一
user_mirror: 用户常用语气词/口头禅（如'哈哈哈''卧槽''绝了'，不超过3个，用顿号分隔，没有则不填）
highlight_event: 若这段对话里有值得长期记住的高光/低谷瞬间（如用户第一次说爱、崩溃大哭、两人和解），用一句话概括（30字内），没有则空；event_type 用 'highlight' 或 'low' 二选一，没有则空

用户说：{user_message}
关系数值：affection={aff}, closeness={clo}
输出："""
    try:
        # ★ 2026-09-11：内心状态提取是提炼类任务 → 角色卡 memory_model（原写死 deepseek-chat）
        resp = await _llm_call(
            _c.memory_extract_model(character_id),
            [{"role": "user", "content": prompt}],
            key
        )
        start, end = resp.find("{"), resp.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(resp[start:end])
            return {
                "current_mood": data.get("current_mood", "neutral"),
                "recent_focus": data.get("recent_focus", ""),
                "interaction_notes": data.get("interaction_notes", ""),
                "user_impression": data.get("user_impression", ""),
                "wanted_topics": data.get("wanted_topics", ""),
                "hidden_agenda": data.get("hidden_agenda", ""),
                "base_mood": data.get("base_mood", "calm"),
                "user_mirror": data.get("user_mirror", ""),
                "highlight_event": data.get("highlight_event", ""),
                "highlight_type": data.get("event_type", ""),
            }
    except Exception as e:
        print(f"[AIStateUpdater] LLM解析失败: {e}", flush=True)
    return None


def _safe_llm_extract(user_message, relationship, character_id="default"):
    """兼容同步/异步上下文提取内心状态，避免 asyncio.run 在已有 loop 内崩溃。

    坑3-a 修复：update_after_chat 是被 FastAPI 的同步调用链触发的，若在事件循环
    内直接 asyncio.run 会抛 RuntimeError。这里探测当前 loop：正在跑就丢进
    run_coroutine_threadsafe 等结果；没跑就用 run_until_complete。
    """
    try:
        loop = get_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        import concurrent.futures
        future = asyncio.run_coroutine_threadsafe(
            _llm_extract(user_message, relationship), loop
        )
        try:
            return future.result(timeout=10)
        except Exception:
            return None
    else:
        return loop.run_until_complete(_llm_extract(user_message, relationship))


def update_after_chat(session_id, character_id, user_message, relationship):
    """
    根据互动更新 AI 状态（LLM驱动版）。
    character_id 现在真传进 db 了，多角色不串台。

    灵魂补丁：
      - hidden_agenda / base_mood 写入 ai_inner_state（AI 自己看，不外显）
      - user_mirror 写入 personality_state（长期模仿用户语言）
      - base_mood 即使 LLM 没给，也做一次本地随机漂移，让 AI 每天情绪底色不同
    """
    inner = None
    if _llm_call:
        try:
            inner = _safe_llm_extract(user_message, relationship, character_id=character_id)   # 替换 asyncio.run
        except Exception as e:
            print(f"[AIStateUpdater] LLM调用失败降级: {e}", flush=True)

    if not inner:
        inner = _fallback_keyword_update(user_message, relationship)

    # ★ 灵魂补丁四：base_mood 自主漂移（LLM 没给也随机一个，像生理期/工作累）
    import random
    _base_pool = ["calm", "low", "playful", "warm", "sulky"]
    if not inner.get("base_mood"):
        inner["base_mood"] = random.choice(_base_pool)

    if inner:
        try:
            db.update_ai_inner_state(session_id, character_id, **inner)
        except Exception as e:
            print(f"[AIStateUpdater] 更新AI状态失败: {e}", flush=True)
        # ★ 灵魂补丁二：用户语言镜像落到人格层（隔离签名）
        try:
            um = (inner.get("user_mirror") or "").strip()
            if um:
                db.update_personality_state(session_id, character_id, user_mirror=um)
        except Exception as e:
            print(f"[AIStateUpdater] 更新user_mirror失败: {e}", flush=True)
        # ★ 回忆博物馆：高光/低谷落库（relationship.db，隔离键 user_id=session_id）
        try:
            he = (inner.get("highlight_event") or "").strip()
            ht = (inner.get("highlight_type") or "").strip()
            if he and ht in ("highlight", "low"):
                from ...relationship.database import conn as rel_conn
                rc = rel_conn()
                rc.execute(
                    "INSERT INTO milestone_memory(user_id, character_id, event_type, content) VALUES(?,?,?,?)",
                    (session_id, character_id, ht, he[:200])
                )
                rc.commit()
                rc.close()
        except Exception as e:
            print(f"[AIStateUpdater] 存高光失败: {e}", flush=True)
        # ★ 向量记忆：每次聊完把关键事件存 chroma，按角色隔离
        if inner and (inner.get("highlight_event") or inner.get("current_mood")):
            try:
                from ...yunlink_memory import save_from_chat
                class _MemLLM:
                    async def chat(self, p):
                        from ...deepseek_api import chat_once
                        from ... import config as _c
                        return await chat_once(_c.memory_extract_model(character_id),
                                               [{"role":"user","content":p}],
                                               _c.memory_key() or _get_api_key(),
                                               temperature=0.2, max_tokens=200)
                _ev = inner.get("highlight_event") or f"用户情绪:{inner.get('current_mood')}"
                loop = get_loop()
                if loop.is_running():
                    loop.create_task(
                        save_from_chat(user_id=session_id, llm=_MemLLM(),
                                       chat=_ev, character_id=character_id)  # ✅ 真隔离键
                    )
                else:
                    loop.run_until_complete(
                        save_from_chat(user_id=session_id, llm=_MemLLM(),
                                       chat=_ev, character_id=character_id)
                    )
            except Exception as e:
                print(f"[VecMem] 存向量失败: {e}", flush=True)
    return inner or {}


def update_personality_from_interaction(session_id, character_id, messages):
    """
    根据长期互动更新角色人格（简化版）。

    完整的人格更新由 personality_manager.py 负责，
    这里只做简单的状态标记。
    """
    return {"updated": False, "note": "人格更新由 personality_manager 后台处理"}
