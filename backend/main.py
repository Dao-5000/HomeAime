# -*- coding: utf-8 -*-
"""
本地 FastAPI 后端入口：
  · 完整兼容原 Node server 接口（/api/chat SSE、/api/config、/api/library*、静态托管 public/）
    → 原前端零改动即可运行，所有原功能保持不变
  · 新增：WebSocket /ws（主动消息推送 + 活动上报）、/api/pc/*（配置管理、记忆导入导出、模型校验）
  · 启动时拉起闲置主动 Agent 后台任务
"""
import asyncio
import base64
import hashlib
import json
import os
import re
# ★ 2026-09-14 补：本模块多处用到 time.time()（既有的 line 1418、1440 等，
#   以及本次新增的回合计时埋点），但顶层从未 import time → 一调用就 NameError。
#   实测已导致 /api/chat/stream 直接 500。这里补齐以根治。
import time
import traceback
from datetime import datetime
from pathlib import Path
from contextlib import asynccontextmanager

# 全局后台任务句柄（用于 shutdown 时取消）
_background_tasks: list = []

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi import UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional

from . import config, db, chat_logic, memory_manager, export_import, character_manager, token_tracker
from . import template_manager, anniversary_manager, scheduler
from . import sticker_manager
from . import onebot
# ★ 事件循环兼容入口：取代已弃用的旧写法，同步/异步上下文都安全
from backend.loop_compat import get_loop
from . import personality_manager
from . import memory_brain
from . import moments
from . import control
from . import memory as yunlink_memory
from . import relationship as yunlink_relationship
from . import idle_agent as idle_mod
from . import reflection  # 👈 新增：记忆反思模块（真实存在，__init__导出 update_reflection）
from .deepseek_api import ModelApiError, stream_chat, stream_vision, stream_vision_silicon, chat_once

ROOT = config.ROOT_DIR
PUBLIC = ROOT / "public"
LIB_DIR = config.LIB_DIR


def _proactive_payload_blocked_by_offline(session_id: str, payload: dict) -> bool:
    """最终出口保险：AI 睡觉/离线/半醒时，不让主动消息/主动来电继续推到前端。

    用户自己发消息后的延迟回复带 _offline_reply，属于“回应用户”，不算主动营业。
    当前聊天 SSE、危机提示、Web 音频等也不从这里误拦。
    """
    try:
        if not isinstance(payload, dict):
            return False
        ptype = payload.get("type")
        if ptype not in {"proactive", "incoming_call"}:
            return False
        if payload.get("_offline_reply") or payload.get("user_initiated"):
            return False
        cid = str(
            payload.get("character_id")
            or payload.get("contact_id")
            or payload.get("contact_name")
            or "default"
        ).strip() or "default"
        if cid == "default":
            return False
        from . import offline as _offline
        state = _offline.resolve(session_id or "default", cid)
        if not state.get("enabled"):
            return False
        status = state.get("status", "online")
        if status == "online":
            return False
        # sleep_letter/silent 只允许前面落库，不允许最终推到前端。
        print(
            f"[WSGuard] 主动推送被离线状态拦截: session={session_id} character={cid} status={status} type={ptype}",
            flush=True,
        )
        return True
    except Exception as e:
        print(f"[WSGuard] 状态门禁异常，放行以免误伤当前对话: {e}", flush=True)
        return False


@asynccontextmanager
async def lifespan(app):
    """
    统一的启动/关闭钩子（替代 on_event startup + shutdown）。
    注意：on_event("startup") 和 on_event("shutdown") 必须同时删掉，
    否则 FastAPI 会报冲突警告，部分逻辑不执行。
    """
    # ============================================================
    # 启动阶段（原 on_event("startup") 全部移到这里）
    # ============================================================
    print("[Main] 后端启动中...", flush=True)

    # 基础数据库初始化
    db.init()
    LIB_DIR.mkdir(parents=True, exist_ok=True)

    # 朋友圈建表
    try:
        moments.init_moments_db()
    except Exception as e:
        print(f"[Moments] 建表失败: {e}", flush=True)

    # YunLink Memory System v1.0
    yunlink_memory.init_db()

    # YunLink Relationship Engine v1.0
    yunlink_relationship.init_relationship()

    # Behavior Predictor v1.0
    try:
        from .behavior import init_behavior_db
        init_behavior_db()
        print("[BehaviorPredictor] 行为数据库初始化完成", flush=True)
    except Exception as e:
        print(f"[BehaviorPredictor] 行为数据库初始化失败: {e}", flush=True)

    # Feedback Learning Layer v1.0
    try:
        from .feedback import init_feedback_db
        init_feedback_db()
        print("[FeedbackLearning] 反馈数据库初始化完成", flush=True)
    except Exception as e:
        print(f"[FeedbackLearning] 反馈数据库初始化失败: {e}", flush=True)

    # Personal Knowledge Graph v1.0
    try:
        from .knowledge_graph import init_knowledge_graph
        init_knowledge_graph()
        print("[KnowledgeGraph] 知识图谱数据库初始化完成", flush=True)
    except Exception as e:
        print(f"[KnowledgeGraph] 知识图谱数据库初始化失败: {e}", flush=True)

    # Memory Reflection Layer v1.0
    try:
        from .reflection import init_reflection_db
        init_reflection_db()
        print("[Reflection] 记忆反思数据库初始化完成", flush=True)
    except Exception as e:
        print(f"[Reflection] 记忆反思数据库初始化失败: {e}", flush=True)

    # Identity Layer v1.0
    try:
        from .identity import init_identity_db
        init_identity_db()
        print("[Identity] AI身份数据库初始化完成", flush=True)
    except Exception as e:
        print(f"[Identity] AI身份数据库初始化失败: {e}", flush=True)

    # Idle Agent
    idle_mod.agent.start()

    # ★ 陪伴观察循环：陪伴模式下看一眼屏幕，看完立即评论（推 QQ/App）
    try:
        _cw = getattr(idle_mod.agent, "companion_watch_loop", None)
        if _cw:
            _cw_task = asyncio.get_running_loop().create_task(_cw())
            _background_tasks.append(_cw_task)
            print("[CompanionWatch] 陪伴观察循环已启动（看完即评）", flush=True)
    except Exception as _cw_err:
        print(f"[CompanionWatch] 启动失败(静默): {_cw_err}", flush=True)

    # Scheduler 绑定 + 启动全局 tick
    scheduler.scheduler.bind(ws_manager.push_to_session)
    scheduler.scheduler.start_global_tick(interval=60)

    # Session 表初始化
    try:
        _session_ensure_table()
        print("[Session] 会话表初始化完成", flush=True)
    except Exception as e:
        print(f"[Session] 会话表初始化失败: {e}", flush=True)

    # HTTP 连接池预热
    try:
        from .http_client import get_http_client
        get_http_client()
        print("[HTTP] 全局连接池初始化完成", flush=True)
    except Exception as e:
        print(f"[HTTP] 连接池初始化失败: {e}", flush=True)

    # ProactiveManager 启动（模块函数式设计，无需生命周期对象；由 idle_agent 调用）
    print("[Main] ProactiveManager 就绪(模块函数式)", flush=True)

    # ★ 按住说话控制电脑（全局 F9 钩子）：软件开着随时按住 F9 说话，松开执行
    try:
        if config.get("PC_PTT_ENABLED", True):
            from .ptt import start as _ptt_start
            if _ptt_start(str(config.get("PC_PTT_KEY") or "f9")):
                print("[Main] 按住说话已启动（按住 F9 对她说，松开执行）", flush=True)
            else:
                print("[Main] 按住说话启动失败（keyboard 钩子不可用）", flush=True)
    except Exception as _ptt_e:
        print(f"[Main] 按住说话启动异常(不影响其他功能): {_ptt_e}", flush=True)

    # 加载持久化的克隆音色（原 on_event startup）
    try:
        load_cloned_voices()
        print("[VoiceClone] 克隆音色加载完成", flush=True)
    except Exception as e:
        print(f"[VoiceClone] 启动加载失败: {e}", flush=True)

    # 启动后台任务（记住句柄，shutdown 时取消）
    # get_loop() 在协程内已弃用（且无运行中循环时会抛错），
    # lifespan 是 async 上下文，这里一定有运行中的循环，用 get_running_loop()
    loop = asyncio.get_running_loop()
    t1 = loop.create_task(memory_cleanup_loop())
    t2 = loop.create_task(reflection_loop())
    t3 = loop.create_task(moments_loop())
    t4 = loop.create_task(tts_cache_cleanup_loop())
    _background_tasks.extend([t1, t2, t3, t4])

    # ★ 2026-09-14 新增：周期性系统自检埋点。
    #   为什么需要：trace 的 turn 记录只在"有人聊天"时才产生；不聊天时
    #   （比如你只是挂着 App、或长时间没回来）就完全看不到系统状态。
    #   这个循环每 30 分钟写一条 selfcheck 记录（纯本地读取，零 LLM 调用），
    #   让 trace 报告在冷清时段也能反映"记忆涨没涨 / 库多大 / 有没有积压"。
    async def _trace_selfcheck_loop():
        await asyncio.sleep(90)   # 启动后等 90 秒，避开启动峰值
        while True:
            try:
                from . import trace as _tr
                _snap = {}
                try:
                    _snap["mem"] = await asyncio.to_thread(
                        lambda: len(db.valid_memories(session_id="default", character_id="default") or []))
                except Exception:
                    pass
                try:
                    _snap["msgs"] = await asyncio.to_thread(db.count_messages, "default", "default")
                except Exception:
                    pass
                try:
                    import os as _os
                    _dbp = _os.path.join(str(config.DATA_DIR), "local_db.db")
                    _snap["db_mb"] = round(_os.path.getsize(_dbp) / 1048576.0, 1)
                except Exception:
                    pass
                try:
                    _tr.stats()   # 触发目录创建，顺带确认可写
                    _snap["trace_ok"] = True
                except Exception:
                    _snap["trace_ok"] = False
                _tr.record("selfcheck", **(_snap or {}))
            except Exception:
                pass
            await asyncio.sleep(1800)
    try:
        _background_tasks.append(loop.create_task(_trace_selfcheck_loop()))
    except Exception as _ts_e:
        print(f"[Trace] 自检循环启动失败(静默): {_ts_e}", flush=True)

    # ★ AI 监督吃醋系统（2026-09-11）：前台窗口采样 + 吃醋值评估 + 主动质问。
    #   关着开关时循环只空转（不采集、不注入、不发消息），所以常驻启动即可。
    try:
        from .jealousy import jealousy_loop as _jealousy_loop
        t_jl = loop.create_task(_jealousy_loop())
        _background_tasks.append(t_jl)
    except Exception as _jl_e:
        print(f"[Jealousy] 循环启动失败(静默): {_jl_e}", flush=True)

    # ★ Numen 游戏陪伴执行器：自动感知游戏（MCP 重连）、QQ 遥控、上下线同步。
    #   游戏内的自主行为与聊天由内置大脑（引擎 + /v1 代理）负责，执行器不插手。
    try:
        from .minecraft.game_bridge import get_game_brain
        _gb = await get_game_brain()
        t5 = loop.create_task(_gb.drive_loop(poll_interval=2))
        _background_tasks.append(t5)
        print(f"[GameBrain] 游戏陪伴执行器已启动（内置大脑模式：引擎主玩，执行器只管 QQ 遥控与同步，ready={_gb.ready}）", flush=True)
    except Exception as _gb_err:
        print(f"[GameBrain] 启动失败（游戏未运行/MCP 未开，不影响其他功能）: {_gb_err}", flush=True)

    # ★ 星露谷 AI 陪伴（StardewValley-MCP）：常驻 MCP Server(Node) + 看护循环，
    #   星露谷没有内置大脑概念 → AI 就是同伴唯一大脑，常驻连接是正确姿态。
    try:
        from .stardew.game_bridge import get_stardew_brain
        _sb = await get_stardew_brain()
        if _sb is not None:
            print(f"[StardewBrain] 星露谷陪伴执行器已启动（ready={_sb.ready}，"
                  f"游戏开着加载存档后助手自动进农场）", flush=True)
        else:
            print("[StardewBrain] 未启用（STARDEW_ENABLED/STARDEW_MCP_SERVER 未配置），跳过", flush=True)
    except Exception as _sb_err:
        print(f"[StardewBrain] 启动失败（不影响其他功能）: {_sb_err}", flush=True)

    import os as _os_port
    print("[Main] 后端启动完成，端口 %s OK" % (_os_port.environ.get("PORT", "3000")), flush=True)

    # ============================================================
    # 运行期
    # ============================================================
    yield

    # ============================================================
    # 关闭阶段（原 on_event("shutdown") + 新增后台任务取消）
    # ============================================================
    print("[Main] 后端正在关闭，清理资源...", flush=True)

    # 1. 取消所有 while True 后台任务
    for task in _background_tasks:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    _background_tasks.clear()
    print("[Main] 后台任务已取消", flush=True)

    # 2. 停止 Scheduler（SchedulerManager 无 stop 方法，后台循环随进程退出终止）
    print("[Main] Scheduler 随进程退出", flush=True)

    # 3. ProactiveManager 无生命周期对象，无需 stop（模块函数式）

    # 4. 关闭 HTTP 连接池（原 shutdown 逻辑保留）
    try:
        from .http_client import close_http_client
        await close_http_client()
        print("[HTTP] 全局连接池已关闭", flush=True)
    except Exception as e:
        print(f"[HTTP] 关闭失败: {e}", flush=True)

    # 5. ★ 2026-09-14：把 token 调用点缓冲落库。
    #    缓冲每 25 次调用落一次，进程被杀/重启时最后不足一批的数据会丢；
    #    这里在正常关闭路径上补齐，避免"跑一天发现统计少一截"。
    try:
        _n = token_tracker.flush_caller_usage()
        _u = token_tracker.get_usage()
        print(f"[Token] 调用点统计已落库（{_n} 条）；当日累计 {_u['daily']} token", flush=True)
    except Exception as e:
        print(f"[Token] 落库失败: {e}", flush=True)

    # 5. 停止 Idle Agent（IdleAgent 无 stop 方法，后台循环随进程退出终止）
    print("[Main] IdleAgent 随进程退出", flush=True)

    print("[Main] 后端已安全退出 OK", flush=True)


app = FastAPI(title="AI Companion PC Backend", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000", "http://localhost:3000",
        "http://127.0.0.1:32123", "http://localhost:32123",
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

_RUNTIME_TOKEN = os.environ.get("AI_COMPANION_RUNTIME_TOKEN", "").strip()
_TRUSTED_WS_ORIGINS = {
    "http://127.0.0.1:3000", "http://localhost:3000",
    "http://127.0.0.1:32123", "http://localhost:32123",
}


def _trusted_websocket_origin(websocket: WebSocket) -> bool:
    origin = str(websocket.headers.get("origin") or "").strip().lower()
    return not origin or origin in _TRUSTED_WS_ORIGINS


@app.get("/api/runtime")
async def runtime_info():
    return {"ok": True, "token": _RUNTIME_TOKEN, "pid": os.getpid(), "port": int(os.environ.get("PORT", "32123"))}

# ★ Minecraft Bot 陪玩路由（minecraft_bot/ Node 子项目对接）
try:
    from .minecraft.routes import router as _mc_router
    app.include_router(_mc_router)
except Exception as _mce:
    print(f"[Main] Minecraft Bot 路由挂载失败(静默): {_mce}", flush=True)

# 2026-09-13 移除：本地大脑训练管线（聊天记录→ShareGPT→LLaMA-Factory→GGUF→Ollama）。
# 实测本地小模型（Qwen3 4B/8B Q4）能力不足，聊天统一走云端。见 git 快照 e114b61。


# ★ OpenAI 兼容接口（供 Numen 等第三方客户端对接）
#   Numen 用它自己的 system prompt（游戏上下文/工具格式），这里在 system 里
#   追加原项目的人格，让游戏里的 AI 是"助手"本人，同时保留 Numen 的工具指令。
#   用法：Numen 里填地址 http://127.0.0.1:32123/v1/chat/completions?character=助手
@app.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    import time as _time
    import uuid as _uuid
    from .deepseek_api import chat_once
    from .character_manager import build_system_prompt

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid json", "type": "invalid_request_error"}}, status_code=400)

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    if not messages:
        return JSONResponse({"error": {"message": "no messages", "type": "invalid_request_error"}}, status_code=400)

    # 角色名解析顺序：query 参数 > QQ_CHARACTER（设置里指定的主力角色，如"助手"）>
    # 项目第一个角色。之前直接取 list_characters()[0]（按 mtime 排序），
    # 可能取到内置角色（qin_heye 等）而不是你的自建角色。
    character = str(request.query_params.get("character") or request.query_params.get("character_id") or "").strip()
    if not character or character == "default":
        character = str(config.get("QQ_CHARACTER") or "").strip()
    if not character or character == "default":
        try:
            from .character_manager import list_characters
            _chars = list_characters() or []
            if _chars:
                character = str(_chars[0].get("name") or _chars[0].get("character_name") or "default")
            else:
                character = "default"
        except Exception:
            character = "default"

    # ★ 模型取用：跟随角色卡大脑（助手在 App 资料页「AI 大脑」里配的模型），
    #   没单独配就回落全局。这样 App 里切换大脑，游戏里同步生效。
    #   忽略外部传的 model（Numen 里常填默认 deepseek-chat），否则会误用失效 key。
    try:
        from . import chat_logic as _cl
        model = _cl.pick_model("", True, character)
    except Exception:
        model = config.selected_model()

    # ★ session 统一：复用助手在 App/QQ 里的同一个 session，实现记忆互通。
    #   （QQ 也是这么做的：QQ_SESSION_ID 优先，否则取该角色最新 session）
    try:
        from . import db as _db
        _sid = str(config.get("QQ_SESSION_ID", "") or "").strip()
        if not _sid:
            _rows = _db.q(
                "SELECT session_id FROM sessions WHERE character_id=? "
                "ORDER BY last_active_at DESC LIMIT 1",
                (character,), fetch=True)
            _sid = str(_rows[0]["session_id"]) if _rows else "default"
        session_id = _sid
    except Exception:
        session_id = "default"

    # 注入人格：追加到 system 消息（若没有 system 就插到最前）
    persona = ""
    try:
        persona = build_system_prompt(character, character_id=character, session_id=session_id)
    except Exception:
        persona = ""
    # ★ 注入 MC 高手指南 + 自主行动纲领（仅游戏模式：只有 Numen 调本端点才走这里，不影响普通聊天）
    try:
        from .minecraft.mc_knowledge import get_mc_knowledge, get_autonomy_directive
        persona = (persona or "") + "\n\n" + get_mc_knowledge() + "\n\n" + get_autonomy_directive()
    except Exception:
        pass

    # ★ 外置记忆库：游戏里的她也"翻"长期记忆（原文片段检索 → 日/周/月兜底）。
    #   此前只有 QQ/App 链路（enrich_messages）注入，游戏里的她读不到记忆图书馆。
    #   60 秒缓存：引擎决策调用频繁，别每轮都做文件读取。
    # ★ 2026-09-13：缓存键要带上本轮消息 —— 改成"相关优先"检索后，
    #   注入内容随 user_text 变化，只按角色缓存会把上一个话题的原文片段复用过来。
    _em_user = ""
    try:
        for _m in reversed(messages):
            if _m.get("role") == "user":
                _em_user = str(_m.get("content") or "")
                break
    except Exception:
        _em_user = ""
    try:
        _em_key = "emb:" + character + ":" + hashlib.sha1(
            _em_user[:120].encode("utf-8")).hexdigest()[:12]
    except Exception as _em_key_err:
        print(f"[NumenOpenAI] 外置记忆缓存键失败: {_em_key_err}", flush=True)
        _em_key = "emb:" + character
    try:
        _em_cache = getattr(openai_chat_completions, "_em_cache", None) or {}
        _hit = _em_cache.get(_em_key)
        if _hit and _time.time() - _hit[0] < 60:
            _em_txt = _hit[1]
        else:
            from .external_memory import build_memory_block as _emb
            _em_txt = _emb(character, _em_user, max_chars=2400,
                           session_id=session_id) or ""
            # 缓存别无限长：只留最近 32 个话题键
            if len(_em_cache) > 32:
                _em_cache = dict(
                    sorted(_em_cache.items(), key=lambda kv: kv[1][0])[-32:]
                )
            _em_cache[_em_key] = (_time.time(), _em_txt)
            openai_chat_completions._em_cache = _em_cache
        if _em_txt:
            for _m in messages:
                if _m.get("role") == "system":
                    _m["content"] = (_m.get("content") or "") + \
                        "\n\n【外置记忆库（更早的长期记忆，聊天时可自然参考）】\n" + _em_txt
                    break
    except Exception as _em_err:
        print(f"[NumenOpenAI] 外置记忆注入失败: {_em_err}", flush=True)

    if persona:
        _injected = False
        for _m in messages:
            if _m.get("role") == "system":
                _m["content"] = (_m.get("content") or "") + "\n\n" + persona
                _injected = True
                break
        if not _injected:
            messages.insert(0, {"role": "system", "content": persona})

    # ★ 复用原项目的完整动态上下文：长期记忆 + 关系状态 + 反思 + 情绪 + 称呼。
    #   直接调用 _inject_shared_context（原项目两个聊天入口都在用它），不重写任何
    #   逻辑——这样游戏里的 AI 和 APP 里的 AI 读到的是同一套记忆/关系/情绪，
    #   才是真正"你的 AI"，而不是只有性格外壳的空壳。
    try:
        _user_text = ""
        for _m in reversed(messages):
            if _m.get("role") == "user":
                _user_text = str(_m.get("content") or "")
                break
        await _inject_shared_context(
            messages, session_id, character,
            user_text=_user_text, with_realness=False, lite=True,
        )
    except Exception as _ctx_err:
        print(f"[NumenOpenAI] 共享上下文注入失败: {_ctx_err}", flush=True)

    # ★ 情绪状态：单独注入当前情绪。真人感块（with_realness）带了大量行为指令，
    #   会干扰 Numen 的工具调用格式，所以这里只取情绪这一项，用一行轻提示注入。
    try:
        _eng = _get_emotion_engine()
        if _eng:
            _st = _eng.get_state(session_id, character or "default")
            if _st:
                _emo = _st.get("emotion", "calm")
                _inten = float(_st.get("intensity", 0.5))
                _emo_cn = {"happy": "开心", "calm": "平静", "sad": "难过", "angry": "生气",
                           "anxious": "焦虑", "excited": "兴奋", "tired": "疲惫"}.get(str(_emo), str(_emo))
                for _m in messages:
                    if _m.get("role") == "system":
                        _m["content"] = (_m.get("content") or "") + \
                            f"\n【当前情绪】你现在的情绪是{_emo_cn}（强度{_inten:.1f}），说话时自然流露这个情绪。"
                        break
    except Exception as _emo_err:
        print(f"[NumenOpenAI] 情绪注入失败: {_emo_err}", flush=True)

    # ★ 时间感知：游戏里的助手也知道现实时间（夜猫子情境/早晚问候才对得上）。
    #   夜间占比用 10 分钟缓存（引擎决策调用频繁，别每次都聚合 chat_history）。
    try:
        from datetime import datetime as _dtime
        from . import time_system as _tsys
        _now_t = _dtime.now()
        _wk = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][_now_t.weekday()]
        _night_note = ""
        if _time.time() - getattr(openai_chat_completions, "_nr_ts", 0) > 600:
            try:
                from . import db as _dbmod
                openai_chat_completions._nr_cache = _dbmod.user_night_ratio(session_id, character)
                openai_chat_completions._nr_ts = _time.time()
            except Exception:
                pass
        _nr = getattr(openai_chat_completions, "_nr_cache", None)
        if _nr is not None and _nr >= 0.25:
            _night_note = "主人是夜猫子（深夜凌晨在线是常态），不要因为时间晚就催睡。"
        for _m in messages:
            if _m.get("role") == "system":
                _m["content"] = (_m.get("content") or "") + (
                    f"\n【现实时间】现在是 {_now_t.strftime('%Y-%m-%d %H:%M')}（{_wk}，{_tsys.time_period(_now_t)}）。{_night_note}")
                break
    except Exception as _t_err:
        print(f"[NumenOpenAI] 时间注入失败: {_t_err}", flush=True)

    # ★ 按 provider 取 key（claude-sonnet-5 → claude_api_key），
    #   不要用 config.api_key()（那是 DeepSeek key，已失效）
    key = config.api_key_for_model(model)
    if not key:
        return JSONResponse({"error": {"message": "后端未配置 API Key", "type": "auth_error"}}, status_code=400)

    # ★ 透传模式：直接调真实模型的 OpenAI 兼容端点，保留 stream / tools(function calling)。
    #   Numen 靠 tool_calls 执行挖矿/建造等动作，且常用流式实时显示；
    #   之前用 chat_once 只返回非流式纯文本，丢弃 tools 和流式 → Numen 解析失败（"不行"）。
    from .deepseek_api import _chat_completions_url, DEEPSEEK_BASE
    from .http_client import get_http_client
    _mcfg = config.get_text_model_config(model) or {}
    _real_model = str(_mcfg.get("model") or model)
    _base_url = str(_mcfg.get("baseUrl") or "")
    _url = _chat_completions_url(_base_url, DEEPSEEK_BASE + "/chat/completions", model)

    _stream = bool(body.get("stream", False))
    _has_tools = bool(body.get("tools"))
    _payload = {
        "model": _real_model,
        "messages": messages,
        "stream": _stream,
        "temperature": body.get("temperature", 0.7),
        "max_tokens": body.get("max_tokens", 1024),
    }
    for _k in ("tools", "tool_choice", "top_p", "presence_penalty", "frequency_penalty", "stop"):
        if _k in body and body[_k] is not None:
            _payload[_k] = body[_k]

    print(f"[NumenOpenAI] 转发: model={model}({_real_model}) stream={_stream} "
          f"tools={len(body.get('tools') or [])} msgs={len(messages)} character={character}",
          flush=True)

    _headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    _client = get_http_client()

    if _stream:
        async def _sse():
            try:
                async with _client.stream("POST", _url, json=_payload, headers=_headers) as resp:
                    if resp.status_code != 200:
                        _err = ""
                        try:
                            _err = (await resp.aread()).decode("utf-8", "ignore")
                        except Exception:
                            pass
                        yield f"data: {json.dumps({'error': {'message': _err[:300]}})}\n\n"
                        yield "data: [DONE]\n\n"
                        return
                    # ★ 原样透传字节流：aiter_lines 会把 SSE 的 "data:{...}\n\n" 拆成
                    #   "data:{...}" + ""（空行被过滤），丢掉事件结束的空行分隔符，
                    #   导致 Numen 的 SSE 解析器等不到事件结束 → 思考气泡一直挂。
                    # ★ 同时探测空流：上游返回 200 但全程无 content/tool_calls（Flash 类
                    #   模型间歇性空返回）→ 引擎拿到的决策为空 → 她表现为原地发呆。
                    #   这里只做观测打点（流中无法安全重试），频发的话该换大脑模型。
                    _saw_act = False   # 见到非空 content 或 tool_calls
                    _bytes = 0
                    _sample = b""
                    _line_buf = ""
                    _speech = ""       # 她在游戏里说的话（content 累积）→ 同步 QQ/App
                    async for _chunk in resp.aiter_bytes():
                        _bytes += len(_chunk)
                        if not _saw_act:
                            if (b'"tool_calls"' in _chunk) or (
                                b'"content":"' in _chunk and b'"content":""' not in _chunk):
                                _saw_act = True
                            elif len(_sample) < 400:
                                _sample += _chunk
                        # ★ 捕获台词：逐行解析 SSE（容忍 chunk 切在 JSON 中间）
                        _line_buf += _chunk.decode("utf-8", "ignore")
                        while "\n" in _line_buf:
                            _ln, _line_buf = _line_buf.split("\n", 1)
                            _ln = _ln.strip()
                            if not _ln.startswith("data:"):
                                continue
                            _body_s = _ln[5:].strip()
                            if not _body_s or _body_s == "[DONE]":
                                continue
                            try:
                                _jd = json.loads(_body_s)
                                _dc = ((_jd.get("choices") or [{}])[0].get("delta") or {}).get("content")
                                if isinstance(_dc, str) and _dc:
                                    _speech += _dc
                            except Exception:
                                pass
                        yield _chunk
                    if not _saw_act:
                        print(f"[NumenOpenAI] ★★ 空流告警: 上游流式返回全程无 content/tool_calls "
                              f"({ _bytes }字节) model={model} —— 本次决策为空，她会原地发呆。"
                              f"样本: {_sample[:300]!r}", flush=True)
                    # ★ 三端同步：她在游戏里说的话 → 推 QQ/App + 写共享聊天历史
                    #   （只同步主脑调用——带 tools 的那种；引擎子系统的内部调用不算台词）
                    _speech = (_speech or "").strip()
                    if _speech and _has_tools:
                        async def _sync_speech(_txt=_speech):
                            try:
                                from .minecraft.game_bridge import get_game_brain
                                _gb2 = await get_game_brain()
                                await _gb2.sync_game_speech(_txt)
                            except Exception as _se:
                                print(f"[NumenOpenAI] 台词同步失败: {_se}", flush=True)
                        asyncio.create_task(_sync_speech())
            except Exception as _e:
                yield f"data: {json.dumps({'error': {'message': str(_e)}})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(_sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 非流式：透传，保留 tool_calls 等字段原样返回
    try:
        _resp = await _client.post(_url, json=_payload, headers=_headers)
    except Exception as e:
        return JSONResponse({"error": {"message": str(e), "type": "server_error"}}, status_code=502)
    try:
        _data = _resp.json()
    except Exception:
        _data = {"error": {"message": _resp.text[:500], "type": "server_error"}}

    # 提取 content（用于记忆回流 + 空判断日志）
    reply = ""
    try:
        reply = str(((_data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    except Exception:
        reply = ""

    # ★ 三端同步（非流式主脑调用同款）：台词推 QQ/App + 写共享聊天历史
    _speech_ns = (reply or "").strip()
    if _speech_ns and body.get("tools"):
        try:
            from .minecraft.game_bridge import get_game_brain
            _gb3 = await get_game_brain()
            await _gb3.sync_game_speech(_speech_ns)
        except Exception as _se:
            print(f"[NumenOpenAI] 台词同步失败: {_se}", flush=True)

    if not reply.strip() and _resp.status_code == 200:
        try:
            _finish = ((_data.get("choices") or [{}])[0].get("finish_reason"))
            _tc = ((_data.get("choices") or [{}])[0].get("message") or {}).get("tool_calls")
            print(f"[NumenOpenAI] 空 content: finish_reason={_finish} "
                  f"tool_calls={len(_tc) if _tc else 0} model={model}", flush=True)
        except Exception:
            pass

    # ★ 记忆回流：把游戏里的对话写回原项目记忆库，回到 APP 也能记得游戏经历。
    try:
        if reply and str(reply).strip() and _user_text and str(_user_text).strip():
            _chat_text = f"用户：{_user_text}\nAI：{str(reply).strip()}"
            class _SimpleLLM:
                async def chat(self, prompt):
                    from .deepseek_api import chat_once
                    # ★ 2026-09-11：记忆回流是提炼类任务 → 角色卡 memory_model，不再借用生成层模型
                    return await chat_once(
                        config.memory_extract_model(character),
                        [{"role": "user", "content": prompt}],
                        config.memory_key() or key,
                        temperature=0.2,
                        max_tokens=800,
                    )
            async def _save_mem():
                try:
                    await yunlink_memory.save_from_chat(
                        user_id=session_id,
                        llm=_SimpleLLM(),
                        chat=_chat_text,
                        character_id=character,
                    )
                except Exception as _e:
                    # ★ repr 带类型（str(_e) 为空的异常如 CancelledError/超时无法定位根因）
                    import traceback as _tb2
                    print(f"[NumenOpenAI] 记忆回流失败: {type(_e).__name__}: {_e!r}\n{_tb2.format_exc(limit=3)}", flush=True)
            asyncio.create_task(_save_mem())
    except Exception as _e:
        print(f"[NumenOpenAI] 记忆回流组装失败: {_e}", flush=True)

    return JSONResponse(_data, status_code=_resp.status_code)


# ★ OpenAI 兼容：列出可用模型（Numen/OpenAI 客户端的「检测」会调这个端点，
#   没有它就报「服务商侧故障」，哪怕 chat 接口是好的）
@app.get("/v1/models")
async def openai_list_models():
    # ★ 返回后端真实可用的模型（当前全局大脑）。Numen 的「检测」会调这个端点，
    #   之前写死 deepseek 列表，检测到的是失效模型。
    _m = config.selected_model()
    _cfg = config.get_text_model_config(_m) or {}
    _prov = str(_cfg.get("provider") or "unknown")
    return {
        "object": "list",
        "data": [
            {"id": _m, "object": "model", "owned_by": _prov},
        ],
    }


# ★ Numen 游戏陪伴执行器：外部（API 调用方）→ 游戏指令（动作派发/状态查询）
@app.post("/api/numen/message")
async def numen_message(body: dict):
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    try:
        from .minecraft.game_bridge import get_game_brain
        brain = await get_game_brain()
        if not brain.ready:
            return {"ok": False, "ready": False, "error": "游戏未连接（MCP 未开或游戏未运行）"}
        chat = await brain.handle_qq_command(text)
        return {"ok": True, "ready": True, "chat": chat, "companion": brain.companion}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/numen/status")
async def numen_status():
    try:
        from .minecraft.game_bridge import get_game_brain
        brain = await get_game_brain()
        return {"ok": True, "ready": brain.ready, "companion": brain.companion,
                "session": brain.session_id}
    except Exception as e:
        return {"ok": False, "ready": False, "error": str(e)}


# ★ 星露谷 AI 陪伴（StardewValley-MCP）：UI 状态查询 + 指令派发（同 numen 模式）
@app.get("/api/stardew/status")
async def stardew_status():
    try:
        from .stardew.game_bridge import get_stardew_brain
        brain = await get_stardew_brain()
        if brain is None:
            return {"ok": True, "enabled": False, "ready": False,
                    "game_online": False, "companion": "", "companions": []}
        companions = []
        try:
            if brain._game_online:
                for c in await brain._get_companions():
                    st = float(c.get("stamina") or 0)
                    companions.append({
                        "name": str(c.get("name") or ""),
                        "status": str(c.get("status") or ""),
                        "mode": str(c.get("mode") or ""),
                        "location": str(c.get("location") or ""),
                        "stamina": round(st * (100 if st <= 1 else 1)),
                    })
        except Exception:
            pass
        return {"ok": True, "enabled": True, "ready": brain.ready,
                "game_online": bool(brain._game_online), "companion": brain.companion,
                "companions": companions}
    except Exception as e:
        return {"ok": False, "enabled": False, "ready": False,
                "game_online": False, "error": str(e)}


@app.post("/api/stardew/command")
async def stardew_command(body: dict):
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty text"}
    try:
        from .stardew.game_bridge import get_stardew_brain
        brain = await get_stardew_brain()
        if brain is None:
            return {"ok": False, "error": "星露谷模块未启用（需配置 STARDEW_ENABLED 与 MCP Server）"}
        if not brain._game_online:
            return {"ok": False, "game_online": False,
                    "error": "星露谷未启动：用 SMAPI 启动游戏并加载存档后，助手才会进农场"}
        reply = await brain.handle_qq_command(text)
        return {"ok": True, "reply": reply, "companion": brain.companion}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ★ 一键启动星露谷（SMAPI 启动器）：等价双击 StardewModdingAPI.exe
@app.post("/api/stardew/launch")
async def stardew_launch():
    try:
        from .stardew.game_bridge import get_stardew_brain, find_game_exe
        brain = await get_stardew_brain()
        if brain is not None and brain._game_online:
            return {"ok": True, "already": True, "message": "游戏已经在跑了"}
        exe = find_game_exe()
        if not exe:
            return {"ok": False, "error": "未找到 StardewModdingAPI.exe，请在 config.json 的 STARDEW_GAME_PATH 填绝对路径"}
        import os
        os.startfile(exe)   # 等价双击：SMAPI 控制台 + 游戏窗口
        return {"ok": True, "exe": exe, "message": "星露谷启动中（SMAPI），加载存档后助手自动进农场"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ★ 启动助手的游戏实例（真联机 client 模式，第二实例）：
#   用户流程：① 自己 Steam 开游戏读档并主持联机 ② 点 App 按钮 → 这里拉起第二个游戏
#   ③ 助手实例标题界面「协作→加入」（列表空用直接 IP 127.0.0.1）走进联机小屋
@app.post("/api/stardew/launch_client")
async def stardew_launch_client():
    def _count_game_procs() -> int:
        """数当前星露谷游戏进程数（Stardew Valley.exe，每个游戏实例一个）。"""
        try:
            import psutil
            n = 0
            for p in psutil.process_iter(["name"]):
                if (p.info.get("name") or "").lower() == "stardew valley.exe":
                    n += 1
            return n
        except Exception:
            pass
        try:
            import subprocess as _sp
            out = _sp.run(["tasklist", "/FI", "IMAGENAME eq Stardew Valley.exe"],
                          capture_output=True, text=True, timeout=6).stdout or ""
            return out.lower().count("stardew valley.exe")
        except Exception:
            return -1

    try:
        from .stardew.game_bridge import find_game_exe
        n = _count_game_procs()
        if n == 0:
            return {"ok": False, "error": "还没检测到星露谷进程：先完成第 1 步——Steam 启动游戏、读档、Esc→协作→主持联机，再来拉助手的实例"}
        exe = find_game_exe()
        if not exe:
            return {"ok": False, "error": "未找到 StardewModdingAPI.exe，请在 config.json 的 STARDEW_GAME_PATH 填绝对路径"}
        if n >= 2:
            return {"ok": True, "already": True,
                    "message": "已检测到两个游戏实例——助手的实例应该已经开着，去它的标题界面「协作→加入」即可"}
        import os
        os.startfile(exe)
        return {"ok": True, "exe": exe,
                "message": "助手的游戏实例启动中（第 2 步完成）：等它弹出标题界面后，点「协作→加入」进你的农场（列表空就用直接 IP 127.0.0.1）"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _vc_audio(reply: str, cid: str) -> str:
    """语音反馈 TTS（mp3 b64），失败返回空串。"""
    try:
        import base64 as _b64mod
        from . import tts as _tts
        try:
            from .character_manager import select_character_voice_cfg
            voice_cfg = select_character_voice_cfg(cid, emotion="calm", mode="call",
                                                   fallback_voice_key="cosyvoice_default")
        except Exception:
            voice_cfg = None
        voice_cfg = voice_cfg or _tts.get_voice_cfg("cosyvoice_default") \
            or _tts.get_voice_cfg("edge_xiaoxiao") or {}
        if not voice_cfg:
            return ""
        mp3 = await asyncio.wait_for(_tts.generate_audio(reply, voice_cfg, ""), timeout=8.0)
        if not mp3:
            return ""
        from pathlib import Path as _Path
        return _b64mod.b64encode(_Path(_tts._path_from_url(mp3)).read_bytes()).decode()
    except Exception:
        return ""


# ★ 按住说话控制电脑：音频(webm) → ASR → 意图 → 执行 → TTS 反馈
@app.post("/api/pc/voice_control")
async def pc_voice_control(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "请求格式错误"}
    audio_b64 = str(body.get("audio") or "")
    session_id = str(body.get("session_id") or active_session() or "default")
    _cid = str(body.get("character_id") or "助手")
    if not audio_b64:
        return {"ok": False, "error": "没有音频"}
    import base64 as _b64mod
    try:
        audio = _b64mod.b64decode(audio_b64)
    except Exception:
        return {"ok": False, "error": "音频解码失败"}

    # 1. ASR（通话级付费链路：快）
    from . import asr as _asr
    text = ""
    try:
        text = (await asyncio.wait_for(
            _asr.transcribe(audio, filename="audio.webm", mode="call"),
            timeout=15.0)) or ""
    except Exception as e:
        return {"ok": False, "error": f"识别失败: {str(e)[:80]}"}
    text = text.strip()
    if not text:
        return {"ok": True, "text": "", "reply": "没听清，再按一次 F9 说吧"}

    # 1.5 ★ 有待确认提议时，先按「确认/拒绝」处理（做之前先询问的闭环）：
    #      按住 F9 说「可以」→ 执行提议的动作；说「不要/算了」→ 取消；
    #      拿不准 → **不执行**，提议留着等 TTL（说别的事则提议作废，继续走普通语音控制）。
    # ★ 2026-09-16 安全修复：判定改为 `confirm_verdict`（模型主判 → 精确短语兜底 → 拿不准不执行）。
    #   旧实现是 `match_confirmation` 的子串匹配 + 单字词表：说「好累啊」「我要睡了」
    #   含「好」/「要」就被判成 confirm，紧接着这里 `execute()` 真执行（不可逆）。
    from . import control as _control
    if _control.has_pending():
        # ★ 顺序要紧：先判定（判定要读 pending 里的提议），拿到 confirm 才取走。
        _cv = await _control.confirm_verdict_detailed(text, session_id, _cid)
        verdict = str(_cv.get("verdict") or "ambiguous")
        if verdict == "confirm":
            _act = _control.take_pending()
            if _act:
                # 真执行只从 run_pending_action 走（验收脚本的观测点就是它）
                _r = await _control.run_pending_action(_act, session_id, _cid)
                if _r is None:
                    return {"ok": False, "text": text, "reply": "这个动作我没法执行，先不做了",
                            "audio": "", "format": ""}
                reply = str(_r.get("reply") or "做好啦～")
                return {"ok": True, "text": text, "reply": reply,
                        "audio": await _vc_audio(reply, _cid), "format": "mp3"}
        elif verdict == "reject":
            _control.take_pending()
            reply = "好的，那我不动了～"
            return {"ok": True, "text": text, "reply": reply,
                    "audio": await _vc_audio(reply, _cid), "format": "mp3"}
        elif verdict == "unrelated":
            # 模型判「原话跟她这个提议没关系」→ 提议作废，原话按新指令继续走
            _control.take_pending()
        elif str(_cv.get("source") or "") == "fallback" \
                and _control.void_if_stale_utterance(text):
            # 降级兜底（模型说不上话）且原话明显是条新指令 → 提议作废，继续按普通指令走
            pass
        else:
            # 拿不准（模型说 ambiguous：像是在回应提议但说不清）→ 绝不执行，保留 pending 等 TTL
            reply = _control.pending_confirm_reply("voice")
            return {"ok": True, "text": text, "reply": reply,
                    "audio": await _vc_audio(reply, _cid), "format": "mp3"}
    # 2. 意图：规则直配 → LLM 兜底
    from . import control as _control
    from . import config as _cfg
    action = _control.parse_direct_command(text)
    if not action:
        try:
            action = await _control.parse_direct_command_llm(
                text, _cfg.chat_key(), _cfg.selected_model())
        except Exception:
            action = None
    if not action:
        reply = f"听到你说「{text[:24]}」，这句我还不知道怎么帮你操作电脑，可以试试「打开记事本」「音量调大」「下一首」"
        return {"ok": True, "text": text, "reply": reply}

    # 2.5 ★ 特殊动作
    #   截图看屏幕：真截图 + 视觉理解（升级：不再只读窗口标题）
    if action.get("type") == "screenshot_look":
        reply = ""
        try:
            from .proactive.screen_capture import capture_local, save_capture
            from .proactive.vision_analyzer import analyze_screen
            _raw = capture_local()
            _p = save_capture(_raw) if _raw else None
            _desc = analyze_screen(_p, "你", force=True) if _p else None
            reply = ("我看到 " + str((_desc or {}).get("summary") or "屏幕内容，但没看太清")) if _desc \
                else "屏幕看不了——视觉模型没响应，检查一下视觉模型设置"
        except Exception as _se:
            reply = f"屏幕分析失败: {str(_se)[:60]}"
        audio_reply = await _vc_audio(reply, _cid)
        return {"ok": True, "text": text, "reply": reply,
                "audio": audio_reply, "format": "mp3" if audio_reply else ""}
    #   关机/重启：高危大动作 → 强制走「提议 → F9 说可以」确认流（这里绝不直接执行）
    if action.get("type") in ("shutdown_timer",):
        _control.propose(action, session_id, _cid)
        return {"ok": True, "text": text,
                "reply": "这是大动作——按住 F9 说「可以」我才会动手，说「不要」就取消",
                "audio": "", "format": ""}

    # 3. 执行
    result = await _control.execute(action, session_id, _cid)
    hint = str(action.get("_hint") or result.get("action") or "完成")
    extra = ""
    if result.get("ok") and action.get("type") == "screen_info":
        extra = f"，你开着「{str(result.get('info') or '')[:40]}」"
    reply = (hint + extra) if result.get("ok") else (
        hint + "，不过没成功：" + str(result.get("error") or "")[:60])

    # 4. TTS 语音反馈（失败静默，文字兜底）
    audio_reply = await _vc_audio(reply, _cid)
    return {"ok": True, "text": text, "reply": reply,
            "audio": audio_reply, "format": "mp3" if audio_reply else ""}


# ★ TTS 音频缓存静态路由（让前端能访问 /tts_cache/<hash>.mp3）
#   ★ 2026-09-10：必须和 tts.py 的 _CACHE_DIR 用同一个目录。
#     原来取 os.path.dirname(__file__)/tts_cache —— 打包版里那是 PyInstaller
#     的临时解压目录（重启即清空、还可能被系统清理 → 写音频报 Errno 2，
#     通话 TTS 全挂只剩文字）。统一改到可写的用户数据目录。
import os as _os
_tts_cache_dir = _os.path.join(str(config.DATA_DIR), "tts_cache")
_os.makedirs(_tts_cache_dir, exist_ok=True)
app.mount("/tts_cache", StaticFiles(directory=_tts_cache_dir), name="tts_cache")


# ---------------- 全局异常处理（记录到文件） ----------------

ERROR_LOG = ROOT / "backend_error.log"


@app.middleware("http")
async def log_exceptions(request: Request, call_next):
    """全局异常捕获：把错误堆栈写入文件，方便调试"""
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        # 记录错误到文件
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"请求: {request.method} {request.url.path}\n")
                f.write(f"错误: {type(e).__name__}: {e}\n")
                f.write(f"堆栈:\n{traceback.format_exc()}\n")
                f.write(f"{'='*60}\n")
        except Exception:
            pass
        # ★ 同时打日志（DEV_LOG tee 会捕获，开发者日志也能看到）
        try:
            print(f"[ERROR] {request.method} {request.url.path} 未捕获异常: {type(e).__name__}: {e}", flush=True)
            print(traceback.format_exc(), flush=True)
        except Exception:
            pass
        # 返回 500
        # ★ P1-10：原来这里 return 之后还跟着一段 print + 第二个 return，
        #   永远执行不到（不可达代码），已删除。
        return JSONResponse({"error": {"message": f"服务器内部错误: {type(e).__name__}"}}, status_code=500)

# ---------------- 全局状态 ----------------

_state = {
    "active_session": "default",     # WS 上报的当前聊天伴侣（session = contact id）
    "active_persona": {},            # {id, name, intimacy, system/personality}
    # ★ P0-2：按 WS 连接维度记账，避免多标签页互相顶掉（见 active_session 说明）
    "conn_sessions": {},             # {conn_key: session_id}  仅保留存活连接
    "session_last_seen": {},         # {session_id: 最后活跃时间戳}
}


# active_session() 兜底告警限频（秒）：避免每次请求都打日志刷屏
_ACTIVE_SESSION_WARN_INTERVAL = 60.0
_last_active_session_warn_ts = 0.0


def _conn_key(ws) -> str:
    """WS 连接的稳定标识。

    id(ws) 在该连接存活期内唯一；断连后对象被回收、id 可能被复用，
    但 disconnect 时会同步摘除绑定（见 _forget_conn_session），所以够用。
    """
    try:
        return str(id(ws))
    except Exception:
        return ""


def _set_conn_session(ws, sid: str) -> None:
    """把某条 WS 连接绑定到 session，并刷新该 session 的最后活跃时间。"""
    import time as _t
    sid = str(sid or "").strip() or "default"
    key = _conn_key(ws)
    if not key:
        return
    _state["conn_sessions"][key] = sid
    _state["session_last_seen"][sid] = _t.time()
    # 兼容：仍维护单值，供直接读 _state["active_session"] 的老代码使用
    _state["active_session"] = sid


def _forget_conn_session(ws) -> None:
    """WS 断连：摘掉这条连接的绑定，避免它继续参与 active_session() 的选举。

    ★ 不清空已知的 active_session —— 全部断连的窗口期里，保持上一次的值
      比退回 "default" 更不容易把请求路由到错误的角色桶。
    """
    key = _conn_key(ws)
    if key:
        _state["conn_sessions"].pop(key, None)


def active_session() -> str:
    """全局兜底：当前最可能有用户正在操作的 session id。

    ★ P0-2 修复：原来是**进程级单值**，WS 的 bind/hello/activity 直接覆盖它，
      多标签页 / 多端并发时"最后连的"把"先连的"顶掉，于是没带 session_id 的
      HTTP 请求会串到别的标签正在用的角色上。

      现在改为按 WS 连接维度记账：
        · 每条 WS 连接绑定自己的 session（bind / hello / activity 都会刷新）
        · 断连时摘掉该连接的绑定，不再污染后续选举
        · 选举**最后活跃时间最大**的存活 session，而不是"最后连接"的那个
          —— 用户在哪个标签页操作，兜底就落在哪个

      注意：这只是兜底质量的改善，**不能替代显式传 session_id**。HTTP 请求
      无法与 WS 连接关联，多个 session 同时活跃时后端无从分辨来源，所以所有
      接口仍应显式带 session_id，让这个兜底永远不被触发。这里保留限频打点，
      便于把剩余调用点逐个找出来补齐。
    """
    global _last_active_session_warn_ts
    sessions = _state.get("conn_sessions") or {}
    last_seen = _state.get("session_last_seen") or {}

    sid = ""
    if sessions:
        # 只在"仍有存活连接"里选，取最后活跃时间最大的那个
        try:
            sid = max(set(sessions.values()), key=lambda s: last_seen.get(s, 0.0))
        except Exception:
            sid = ""
    if not sid:
        # 没有存活连接：退回上一次已知值（比 "default" 更接近真实意图）
        sid = _state.get("active_session") or "default"
    sid = str(sid) or "default"

    try:
        import time as _t
        now = _t.time()
        if now - _last_active_session_warn_ts > _ACTIVE_SESSION_WARN_INTERVAL:
            _last_active_session_warn_ts = now
            # 取上一层调用位置，方便定位是哪个接口还在用兜底
            try:
                import traceback
                st = traceback.extract_stack(limit=3)
                caller = (f"{os.path.basename(st[-2].filename)}:{st[-2].lineno}"
                          if len(st) >= 2 else "?")
            except Exception:
                caller = "?"
            distinct = len(set(sessions.values())) if sessions else 0
            print(f"[DEPRECATED] active_session() 兜底被触发 caller={caller} "
                  f"sid={sid} 存活session数={distinct} —— 该请求未带 session_id，请补传",
                  flush=True)
    except Exception:
        pass
    return sid


# ---------------- WebSocket 连接管理 ----------------
from collections import defaultdict


class WsManager:
    def __init__(self):
        # session_id → set of WebSocket（一个session可能开多个标签页）
        self._sessions: dict = defaultdict(set)
        # 未绑定session的临时连接（连上但还没发bind/session_id）
        self._pending: set = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._pending.add(ws)   # 先放pending，等bind_session()绑定

    def disconnect(self, ws: WebSocket):
        self._pending.discard(ws)
        # 从所有session中清除
        empty = []
        for sid, conns in self._sessions.items():
            conns.discard(ws)
            if not conns:
                empty.append(sid)
        for sid in empty:
            del self._sessions[sid]

    def bind_session(self, ws: WebSocket, session_id: str):
        """
        ws连接绑定session_id
        前端连上WS后第一条消息发 {"type":"bind","session_id":"xxx"}
        """
        self._pending.discard(ws)
        self._sessions[session_id].add(ws)

    async def push_to_session(self, session_id: str, payload: dict):
        """
        向指定session的所有ws连接推送
        _pending里的ws不收任何推送（未绑定session）
        """
        if _proactive_payload_blocked_by_offline(session_id, payload):
            return
        conns = self._sessions.get(session_id, set())
        if not conns:
            return
        dead = []
        for ws in list(conns):
            try:
                await ws.send_text(
                    json.dumps(payload, ensure_ascii=False)
                )
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast(self, payload: dict):
        """
        全局广播（仅危机alert/系统通知用，普通消息禁止调这个）
        """
        all_ws = set()
        for conns in self._sessions.values():
            all_ws |= conns
        dead = []
        for ws in list(all_ws):
            try:
                await ws.send_text(
                    json.dumps(payload, ensure_ascii=False)
                )
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def session_count(self) -> int:
        return len(self._sessions)

    def connected_count(self) -> int:
        return sum(len(v) for v in self._sessions.values())


ws_manager = WsManager()
idle_mod.agent.bind(ws_manager.broadcast)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not _trusted_websocket_origin(ws):
        await ws.close(code=1008)
        return
    await ws_manager.connect(ws)
    # 连接即推送当前配置快照
    try:
        await ws.send_text(json.dumps({"type": "config", "config": pc_config_payload()}, ensure_ascii=False))
    except Exception:
        pass
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "bind":
                # ★ 多session：WS 绑定 session_id（数据隔离路由关键）
                sid = str(msg.get("session_id", "") or "default").strip() or "default"
                ws_manager.bind_session(ws, sid)
                _set_conn_session(ws, sid)
                await ws.send_text(json.dumps({"type": "bound", "session_id": sid}, ensure_ascii=False))
            elif mtype == "hello":
                contact = msg.get("contact") or {}
                # ★ 修正：session_id 来源优先级 msg.session_id > contact.session_id > contact.id > default
                #   （contact.id 是角色id，不是 session 标识，只在老前端无任何 session 时兜底）
                sid = str(msg.get("session_id")
                          or contact.get("session_id")
                          or contact.get("id") or "") or "default"
                _set_conn_session(ws, sid)
                _state["active_persona"] = contact
                # ★ 从 contact 解析 character_id：
                #   前端 contact 只有 id(UUID)+name(角色名)，绝不能用 UUID 当角色标识去查配置
                #   （查空 → persona 无 name → 来电界面显示"AI"）。用 name 兜底解析成真实角色名。
                _cid = _resolve_char_id(contact.get("character_id"), contact.get("name"))
                # ★ 修复记忆串桶：set_session 必须带 character_id——
                #   此前漏传，idle agent 一直用 default 桶生成主动消息（写入/读取都串角色）
                idle_mod.agent.set_session(sid, contact, _cid)
                # ★ 修复"刚打开聊天页 AI 立刻连发主动消息"：
                #   用户打开页面/切换角色视作一次活动，重置闲置计时，
                #   否则 idle_secs 还是几小时前的值 → 页面刚打开就武装触发追问六连发
                idle_mod.agent.on_user_activity(sid)
                # ★ 记录"用户回来"时间线（落 kv，按角色隔离），供主动消息判断
                #   短暂离开/久别重逢/当天回来次数，避免每次进出都发死板模板
                idle_mod.agent.note_user_return(sid, _cid)
                # ★ persona 从角色配置加载（不把 contact 对象当 persona 传）
                _persona = {}
                try:
                    from .character_manager import get_character
                    _persona = get_character(_cid) or {}
                except Exception:
                    _persona = {}
                # ★ 头像：角色配置里没有 avatar，从 contact 上报里透传（语音通话来电界面用）
                if isinstance(_persona, dict):
                    if contact.get("avatar"):
                        _persona["avatar"] = contact.get("avatar")
                    # ★ 补 id：前端 localStorage 用 UUID 找联系人（来电 payload 的 contact_id）
                    if contact.get("id"):
                        _persona["id"] = contact.get("id")
                # ★ 多session：确保该 session 有对应调度器（SchedulerManager 并发版）
                scheduler.scheduler.get_or_create(sid, _cid, _persona)
                # ★ 统一走 bind_session（和 bind 消息完全同一条路径，一套 disconnect 清理）
                ws_manager.bind_session(ws, sid)
                await ws.send_text(json.dumps({"type": "bound", "session_id": sid}, ensure_ascii=False))
                try:
                    from . import intimacy_manager as _im
                    await ws.send_text(json.dumps({
                        "type": "intimacy_state", "session_id": sid,
                        "character_id": _cid, "value": _im.get(sid, _cid)
                    }, ensure_ascii=False))
                except Exception:
                    pass
                # ★ 世界轻推：WS hello 上报城市 → 存 relationship_state（UPSERT 防御无行）
                _city = msg.get("city") or contact.get("city")
                if _city:
                    try:
                        from .relationship.database import conn as rel_conn
                        rc = rel_conn()
                        rc.execute("INSERT OR IGNORE INTO relationship_state(user_id, character_id) VALUES(?,?)",
                                   (sid, _cid))
                        rc.execute("UPDATE relationship_state SET city=? WHERE user_id=? AND character_id=?",
                                   (str(_city), sid, _cid))
                        rc.commit(); rc.close()
                    except Exception as e:
                        print(f"[City-WS] 存失败: {e}", flush=True)
                # ★ 同步前端 API Key 到后端（通话/唱歌/记忆提炼的 LLM 用；聊天 key 是前端传的，后端也要一份）
                _tmp_key = str(msg.get("api_key") or contact.get("api_key") or "").strip()
                if _tmp_key and _tmp_key != config.api_key():
                    try:
                        config.update({"api_key": _tmp_key})
                        print("[WS] 已同步前端 API Key 到后端", flush=True)
                    except Exception as _ke:
                        print(f"[WS] 同步 API Key 失败(静默): {_ke}", flush=True)
                # ★ 同步前端视觉 Key（游戏画面感知/发图片分析用）
                _tmp_vkey = str(msg.get("vision_key") or contact.get("vision_key") or "").strip()
                if _tmp_vkey and _tmp_vkey != config.vision_key():
                    try:
                        config.update({"vision_api_key": _tmp_vkey})
                        print("[WS] 已同步前端视觉 Key 到后端", flush=True)
                    except Exception as _ke:
                        print(f"[WS] 同步视觉 Key 失败(静默): {_ke}", flush=True)
            elif mtype == "activity":
                sid = str(msg.get("session_id") or "") or active_session()
                _set_conn_session(ws, sid)
                idle_mod.agent.on_user_activity(sid)
            elif mtype == "intimacy":
                from . import intimacy_manager
                _sid = str(msg.get("session_id") or active_session() or "default")
                _cid = _resolve_char_id(msg.get("character_id"), msg.get("character_name"))
                intimacy_manager.report(_sid, msg.get("value"), _cid)
            elif mtype == "ping":
                await ws.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))
            elif mtype == "call_result":
                """
                前端上报通话结果（接听/拒绝/超时/结束）。
                用于后续调度决策（如拒绝后降低来电频率）。
                """
                result     = str(msg.get("result", ""))
                contact_id = str(msg.get("contact_id", ""))
                # ★ 用 character_id（真实角色名）做冷却 key，与 scheduler._check_proactive_call
                #   的 self._character_id 对齐；老前端没传时退回 contact_id
                character_id = str(msg.get("character_id") or "") or contact_id

                # 写入 kv，供 scheduler._check_proactive_call() 读取决策
                # 注意：kv key 用 character_id（真实角色名），和 scheduler.py 防重 key 一致
                if result in ("rejected", "timeout") and character_id:
                    # 拒绝/超时：冷却期延长到8小时（正常是4小时）
                    import time
                    db.kv_set(
                        f"last_proactive_call_ts:{character_id}",
                        str(time.time())
                    )
                    db.kv_set(
                        f"call_result_cooldown:{character_id}",
                        result   # scheduler 读到这个值时会延长冷却
                    )
                    print(
                        f"[CallResult] {result}: character={character_id}，冷却期延长至8h",
                        flush=True
                    )
                elif result == "accepted" and character_id:
                    # 接听：清除冷却标记，亲密度由 voice_call.py 闭环处理
                    db.kv_set(f"call_result_cooldown:{character_id}", "accepted")
            elif mtype == "viewing":
                # ★ 前端上报：用户当前是否正在看聊天界面（用于发朋友圈/主动消息时机判断）
                sid = str(msg.get("session_id") or "") or active_session()
                cid = str(msg.get("character_id") or "")
                viewing = bool(msg.get("viewing", False))
                import time as _time
                db.kv_set(f"viewing_state:{sid}", json.dumps({
                    "character_id": cid,
                    "viewing": viewing,
                    "ts": _time.time(),
                }, ensure_ascii=False))
    except WebSocketDisconnect:
        _forget_conn_session(ws)
        ws_manager.disconnect(ws)
    except Exception:
        _forget_conn_session(ws)
        ws_manager.disconnect(ws)


# ---------------- 实时语音通话 WebSocket ----------------

@app.websocket("/ws/voice-call")
async def ws_voice_call(
    websocket: WebSocket,
    session_id:   str = "default",
    character_id: str = "default",
    # ★ voice_key 改为可选，不传则从角色卡读
    voice_key:    str = "",
    call_source:  str = "user_initiated"
):
    if not _trusted_websocket_origin(websocket):
        await websocket.close(code=1008)
        return
    """
    实时语音通话 WebSocket 接口。

    前端连接：
      ws://localhost:8000/ws/voice-call?session_id=xxx&character_id=yyy&voice_key=zzz

    消息协议：
      前端→后端：
        {"type": "audio_chunk", "data": "<base64音频片段>"}
        {"type": "text_input",  "text": "用户文字"}
        {"type": "ping"}
        {"type": "end_call"}

      后端→前端：
        {"type": "asr_start"}
        {"type": "asr_result", "text": "转写文字"}
        {"type": "asr_empty"}
        {"type": "asr_error"}
        {"type": "ai_thinking"}
        {"type": "ai_text_chunk", "text": "句子", "idx": 0}
        {"type": "ai_interrupted"}
        {"type": "ai_done"}
        {"type": "ai_error"}
        {"type": "tts_chunk", "idx": 0, "text": "句子", "audio": "<base64mp3>", "format": "mp3"}
        {"type": "tts_text_fallback", "idx": 0, "text": "句子"}
        {"type": "pong"}
        {"type": "call_ended"}
    """
    await websocket.accept()

    # ★ 统一人格隔离键：前端可能传 UUID，这里解析成真实角色名（找不到则兜底 default），
    #   保证通话记忆（voice_call._extract_memories）与聊天记忆写入同一个角色桶
    character_id = _resolve_char_id(character_id, "")

    # ★ voice_key 优先级：
    #   1. 前端显式传入（调试 / 临时切换用）
    #   2. 角色卡里存的音色
    #   3. 兜底默认值
    # The old web client always sent this fallback, even after a cloned voice
    # was bound. Prefer the role card whenever it contains a real selection.
    if voice_key == "cosyvoice_default":
        try:
            from .character_manager import get_character_voice_key
            configured_voice = get_character_voice_key(character_id)
            if configured_voice and configured_voice != "cosyvoice_default":
                voice_key = configured_voice
        except Exception:
            pass
    if not voice_key:
        try:
            from .character_manager import get_character_voice_key
            voice_key = get_character_voice_key(character_id)
        except Exception:
            pass
    if not voice_key:
        # ★ 兜底用本地 CosyVoice（断网可用），不再默认 edge-tts（需联网）
        voice_key = "cosyvoice_default"

    print(
        f"[VoiceCall] 通话建立: session={session_id} "
        f"character={character_id} voice={voice_key}",
        flush=True
    )

    from .voice_call import VoiceCallSession
    import base64

    session = VoiceCallSession(
        ws=websocket,
        session_id=session_id,
        character_id=character_id,
        voice_key=voice_key
    )
    # ★ AI 来电（主动呼入）标记来源，供通话记录区分
    session._call_source = (
        "ai_initiated" if call_source == "ai_initiated" else "user_initiated"
    )

    # 启动TTS worker
    await session.start_tts_worker()

    # 发送通话就绪信号
    await asyncio.sleep(1.1)
    await websocket.send_text(
        json.dumps({"type": "call_ready"}, ensure_ascii=False)
    )
    asyncio.create_task(session.start_greeting())

    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break

            # ★ 二进制帧 = PCM16 音频（前端 AudioWorklet 直出，持续流式发送）
            pcm = raw.get("bytes")
            if pcm:
                await session.handle_audio_chunk(bytes(pcm), is_pcm=True)
                continue

            # 文本帧 = JSON 消息
            text = raw.get("text")
            if not text:
                continue
            try:
                msg = json.loads(text)
            except Exception:
                continue
            msg_type = msg.get("type", "")

            if msg_type == "speech_start":
                # 前端 VAD 判定用户开口 → 清空缓冲开始攒这句
                await session.begin_speech()

            elif msg_type == "speech_end":
                # 前端 VAD 判定用户说完 → 触发 ASR
                await session.end_speech()

            elif msg_type == "interrupt":
                # 打断：停掉当前 LLM / TTS / 唱歌
                await session.interrupt()

            elif msg_type == "audio_chunk":
                # 兼容旧客户端：base64 webm 音频片段
                b64 = msg.get("data", "")
                if b64:
                    audio_bytes = base64.b64decode(b64)
                    await session.handle_audio_chunk(
                        audio_bytes,
                        is_speech=bool(msg.get("speaking", True)),
                    )

            elif msg_type == "text_input":
                text = msg.get("text", "")
                await session.handle_text_input(text)

            elif msg_type == "silence":
                # 兼容旧客户端：用户停说触发 ASR
                await session.handle_silence()

            elif msg_type == "user_sung":
                # ★ 合唱（B/C方案）：前端 VAD 检测到用户唱完 → 继续下一句
                session.notify_duet_done()

            elif msg_type == "screen_watch":
                await session.set_screen_watch(bool(msg.get("enabled", False)))

            elif msg_type == "ping":
                await websocket.send_text(
                    json.dumps({"type": "pong"}, ensure_ascii=False)
                )

            elif msg_type == "end_call":
                break

    except WebSocketDisconnect:
        print(f"[VoiceCall] 通话断开: session={session_id}", flush=True)
    except Exception as e:
        print(f"[VoiceCall] 通话异常: {e}", flush=True)
    finally:
        await session.stop()
        try:
            await websocket.send_text(
                json.dumps({"type": "call_ended"}, ensure_ascii=False)
            )
        except Exception:
            pass
        print(f"[VoiceCall] 通话结束: session={session_id}", flush=True)


# ---------------- QQ 机器人接入（OneBot v11 反向 WebSocket） ----------------

@app.websocket("/onebot/v11/ws")
async def onebot_ws(websocket: WebSocket):
    from .onebot import handle_onebot_ws
    await handle_onebot_ws(websocket)


# ---------------- 原接口：/api/config ----------------

@app.get("/api/config")
async def api_config():
    return {"serverKey": bool(config.api_key()), "visionKey": bool(config.vision_key())}


@app.post("/api/feedback")
async def api_feedback(request: Request):
    """
    显式反馈接口
    body: {session_id, character_id, message_id, action: like/dislike/correct}
    """
    try:
        body         = await request.json()
        session_id   = body.get("session_id",   "default")
        character_id = body.get("character_id", "default")
        message_id   = str(body.get("message_id", ""))
        action       = body.get("action", "")   # like/dislike/correct
        ai_reply     = body.get("ai_reply",  "")
        user_note    = body.get("user_note", "")   # 纠正时用户说的话

        if action not in ("like", "dislike", "correct"):
            return JSONResponse({"error": "无效action"}, status_code=400)

        from .feedback.collector import collect_feedback
        fb = collect_feedback(
            session_id, character_id, message_id,
            user_action=action,
            score=1 if action == "like" else 0,
            ai_reply=ai_reply,
            user_response=user_note
        )
        return JSONResponse({"ok": True, "feedback_type": fb})
    except Exception as e:
        print(f"[FeedbackAPI] 异常: {e}", flush=True)
        return JSONResponse({"error": str(e)}, status_code=500)


def _seconds_until_window(hours: str) -> int:
    """距该角色可用时段开启还有多少秒（跨天支持）。解析不出来给 600。"""
    try:
        parsed = config.parse_time_range(hours)
        if not parsed:
            return 600
        sh, sm, _eh, _em = parsed
        now = datetime.now()
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if start <= now:
            start = start + timedelta(days=1)
        return max(60, int((start - now).total_seconds()))
    except Exception:
        return 600


async def _proactive_precheck(session_id: str, character_id: str, *, ptype: str = "general",
                              force: bool = False, dnd_exempt: bool = False,
                              startup: bool = False, user_requested_audio: bool = False,
                              caller: str = "_register") -> dict:
    """主动消息闸门（薄包装）：**引擎开着就问引擎，关掉回退旧口径**（2026-09-15 重做）。

    真正的判定逻辑已全部搬进 `proactive_engine.decide()`（单一实现）；
    本函数只做「引擎总开关」的转发 —— 保证 `PROACTIVE_ENGINE_ENABLED=false` 时
    一键回退到下面的旧实现（当夜发现问题不用重新打包）。
    """
    try:
        from . import proactive_engine as _eng
        if _eng.engine_enabled():
            return _eng.decide(session_id, character_id, ptype=ptype, force=force,
                               dnd_exempt=dnd_exempt, startup=startup,
                               user_requested_audio=user_requested_audio, caller=caller)
    except Exception as _e:
        print(f"[ProactiveEngine] 引擎异常，回退旧闸门: {type(_e).__name__}: {_e}", flush=True)
    return await _proactive_precheck_legacy(
        session_id, character_id, ptype=ptype, force=force, dnd_exempt=dnd_exempt,
        startup=startup, user_requested_audio=user_requested_audio, caller=caller)


async def _proactive_precheck_legacy(session_id: str, character_id: str, *, ptype: str = "general",
                                     force: bool = False, dnd_exempt: bool = False,
                                     startup: bool = False, user_requested_audio: bool = False,
                                     caller: str = "_register") -> dict:
    """主动消息**统一闸门链**（单一实现，2026-09-15）。

    谁在用：
      · `/api/proactive/register` —— 生成后登记（最后一道防线）
      · `/api/proactive/precheck` —— 前端**生成前**先问一句（省掉白烧的 42k 上下文）
    为什么必须收成一处：此前前端自己算一套时段/退避、后端再算一套，两边数据会漂，
    实测后果是"前端生成完整条消息（in=42000+、首字 45~160 秒）→ 后端才拦下"，
    每 11 分钟白烧一次。以后任何新增闸门只改这里，所有入口自动生效。

    豁免口径（与人格设置页文案一致）：
      · 全豁免（连时段也不看）：用户自设提醒 pending_task / 到点承诺 promise / 危机 crisis /
        测试按钮 force
      · 只豁免间隔、仍须落在时段内：开屏问候 startup / 话题延续 topic_continue /
        用户当场索要的语音 / 陪伴模式

    返回：
      {"allowed": True,  "exempt": "" | "full" | "interval", "hours": ..., "lo_min": ...}
      {"allowed": False, "reason": ..., "retry_after": 秒, "hint": ..., "hours": ...,
       "next_window_start": 时间戳(仅时段类拒绝)}
    """
    out = {"allowed": True, "exempt": "", "hours": "", "lo_min": 0.0, "next_window_start": 0}
    try:
        out["hours"] = str(config.resolve_active_hours(character_id) or "")
    except Exception:
        out["hours"] = ""
    try:
        out["lo_min"] = float(config.proactive_interval_min(character_id) or 0)
    except Exception:
        out["lo_min"] = 0.0

    def _deny(reason, retry_after, hint="", status=None):
        d = {"allowed": False, "reason": reason,
             "retry_after": max(30, int(retry_after or 600)), "hint": hint,
             "hours": out["hours"], "lo_min": out["lo_min"],
             "next_window_start": out.get("next_window_start") or 0}
        if status:
            d["status"] = status
        try:
            from . import proactive_trace as _pt
            # skip 自带 5 分钟去抖；预检调用不写第二条 gate 记录，避免刷屏
            _pt.record_skip(session_id, character_id, reason,
                            detail="type=%s" % ptype, source=caller)
            if caller != "_precheck":
                _pt.record_gate(session_id, character_id, who=caller, ptype=ptype,
                                decision="reject", reason=reason,
                                detail=("%s（可用时段 %s）" % (hint or reason, out["hours"]))[:120])
        except Exception:
            pass
        print(f"[ProactiveGate] 拦截 {reason}（{hint or ''}）type={ptype} "
              f"retry_after={d['retry_after']}s hours={out['hours']}", flush=True)
        return d

    # ── 豁免判定（口径见 docstring）──
    _full_exempt = bool(force or dnd_exempt)
    _interval_exempt = bool(startup or ptype == "topic_continue" or user_requested_audio)

    # ① 对话活跃：正聊着/刚说完话的 8 分钟内不插话（startup / 提醒 / 危机 / force 不看）
    if not force and not startup and not dnd_exempt:
        try:
            _last_user = db.last_user_time(session_id, character_id)
            _last_activity = db.last_activity_time(session_id)
            _recent = max([x for x in (_last_user, _last_activity) if x is not None], default=None)
            if _recent:
                _el = (datetime.now() - _recent).total_seconds()
                if _el < 8 * 60:
                    return _deny("conversation_active", 8 * 60 - _el,
                                 hint="刚聊过 %.0f 分钟（8 分钟内不插话）" % (_el / 60.0))
        except Exception as _e:
            print(f"[ProactiveGate] 对话活跃门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ② 全局免打扰
    if not _full_exempt:
        try:
            if config.is_dnd_now():
                return _deny("dnd", config.dnd_seconds_remaining(), hint="全局免打扰时段")
        except Exception as _e:
            print(f"[ProactiveGate] DND 门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ③ 用户睡眠声明（说了要睡就别打扰；提醒/危机豁免）
    if not _full_exempt:
        try:
            from . import offline as _offline
            _sh = _offline.hours_since_sleep_declared(session_id, character_id)
            if _sh < _offline.SLEEP_SILENCE_HOURS:
                return _deny("user_sleeping", (_offline.SLEEP_SILENCE_HOURS - _sh) * 3600,
                             hint="用户声明睡眠后 %.1fh < %.1fh" % (_sh, _offline.SLEEP_SILENCE_HOURS))
        except Exception as _e:
            print(f"[ProactiveGate] 睡眠门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ④ 角色离线/半醒
    if not _full_exempt:
        try:
            from . import offline as _offline2
            _off = _offline2.resolve(session_id, character_id)
            if _off.get("enabled") and _off.get("status") not in {"online", "half_awake"}:
                return _deny("offline", max(300, int(_off.get("delay_seconds") or 0)),
                             hint=_off.get("hint") or "角色离线中", status=_off.get("status"))
        except Exception as _e:
            print(f"[ProactiveGate] 离线门禁异常(继续): {type(_e).__name__}: {_e}", flush=True)

    # ⑤ 陪伴模式：用户主动开启的独立节奏 → 只豁免间隔，时段照样管
    if not _full_exempt:
        try:
            _cm_raw = db.kv_get(f"companion_mode:{session_id}:{character_id}") \
                or db.kv_get(f"companion_mode:{session_id}")
            if _cm_raw:
                import json as _cj2
                _cmd = _cj2.loads(_cm_raw) if isinstance(_cm_raw, str) else (_cm_raw or {})
                if isinstance(_cmd, dict) and str(_cmd.get("mode") or "").strip():
                    _interval_exempt = True
        except Exception:
            pass

    # ⑥ 角色可用时段（除 full 豁免外一律要过）——retry_after 直接给"距窗口开启的秒数"，
    #    这样前端会一路退避到窗口打开，而不是每 10 分钟白试一次
    if not _full_exempt:
        try:
            if not config.character_active_now(character_id):
                _wait = _seconds_until_window(out["hours"])
                # ★ 不要用 timedelta（本模块没导入它，2026-09-15 实测就是这里抛
                #   NameError 被下面的 except 吞掉 → 时段门禁"异常即放行"，测试当场抓到）
                out["next_window_start"] = int(datetime.now().timestamp()) + int(_wait)
                return _deny("outside_active_hours", _wait,
                             hint="当前不在该角色的活跃时段内（%s）" % out["hours"])
        except Exception as _e:
            print(f"[ProactiveGate] 时段门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    # ⑦ 主动发言间隔（只对"没有豁免间隔"的类型生效；跨链路共享标记）
    if not _full_exempt and not _interval_exempt and out["lo_min"] > 0:
        try:
            from .proactive_quality import last_push_time as _lpt
            _last = _lpt(session_id, character_id)
            if _last > 0:
                _el2 = datetime.now().timestamp() - _last
                if _el2 < out["lo_min"] * 60:
                    return _deny("interval_gate", out["lo_min"] * 60 - _el2,
                                 hint="距上次主动发言 %.0f 分 < 下限 %.0f 分"
                                      % (_el2 / 60.0, out["lo_min"]))
        except Exception as _e:
            print(f"[ProactiveGate] 间隔门禁异常(放行): {type(_e).__name__}: {_e}", flush=True)

    out["exempt"] = "full" if _full_exempt else ("interval" if _interval_exempt else "")
    try:
        from . import proactive_trace as _ptg
        _ptg.record_gate(session_id, character_id, who=caller, ptype=ptype,
                         decision="allow", reason="passed",
                         detail=("豁免=%s（仅间隔）" % out["exempt"]) if out["exempt"] == "interval"
                         else ("全豁免" if out["exempt"] == "full" else "时段内 + 间隔达标"),
                         exempt=out["exempt"])
    except Exception:
        pass
    return out


@app.get("/api/proactive/status")
async def api_proactive_status(session_id: str = "", character_id: str = ""):
    """主动消息引擎状态（给设置页/体检脚本，2026-09-15 重做配套）。

    返回：当前节奏阶段、**下次可主动时间**、可用时段、今日已发/上限、
    被攒下的念头、以及最近几次「主动/被拦」的原因。
    """
    try:
        from . import proactive_engine as _eng
        sid = str(session_id or "").strip()
        if not sid:
            # ★ 2026-09-15 修：不能直接 active_session() —— 它可能给出**没有该角色消息**的会话，
            #   于是"今日已发"读成 0（实测：直调 daily_count=4，端点却显示 0/8）。
            #   优先用该角色最近一条消息所在的会话，其次 active_session()，最后 QQ_SESSION_ID。
            cid0 = str(character_id or "").strip()
            try:
                if cid0:
                    _rows = db.q("SELECT session_id FROM chat_history WHERE character_id=? "
                                 "ORDER BY id DESC LIMIT 1", (cid0,), fetch=True)
                    if _rows:
                        sid = str(_rows[0]["session_id"] or "").strip()
            except Exception:
                sid = ""
            if not sid:
                sid = str(active_session() or config.get("QQ_SESSION_ID") or "default").strip() or "default"
        cid = _resolve_char_id(character_id, character_id) if character_id else _eng_active_cid(sid)
        return {"ok": True, **_eng.status(sid, cid)}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _eng_active_cid(sid: str) -> str:
    try:
        from . import proactive_engine as _eng  # noqa: F401
        from . import llm_guard as _lg
        return _lg.active_character_id() or "default"
    except Exception:
        return "default"


@app.post("/api/proactive/test")
async def api_proactive_test(request: Request):
    """测试按钮：**由后端引擎**立刻生成并发送一条（force，豁免闸门）。

    重做后前端不再自己生成主动消息（`PROACTIVE_FRONTEND_GENERATION=false`），
    所以"让她现在说一句"这个动作统一走后端 —— 顺便让测试也走正式链路（含 trace）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        from . import proactive_engine as _eng
        sid = str(body.get("session_id") or active_session() or "default").strip() or "default"
        cid = _resolve_char_id(body.get("character_id"), body.get("character_name"))
        _txt = await _eng.generate_chatty(sid, cid, hint=str(body.get("hint") or ""))
        if not _txt:
            return {"ok": False, "error": "生成失败（模型不可用或返回空）"}
        _mid = _eng.deliver(sid, cid, _txt, ptype="general", source="_test_button")
        return {"ok": bool(_mid), "message_id": _mid, "content": _txt}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


@app.post("/api/proactive/register")
async def api_proactive_register(request: Request):
    """前端主动消息发送前在后端登记，统一去重、冷却并进入反馈闭环。"""
    try:
        body = await request.json()
        session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
        force = bool(body.get("force"))
        from .proactive_quality import (
            cooldown_remaining, is_similar_to_recent, mark_sent, sanitize_proactive_message,
        )
        _ptype = str(body.get("proactive_type") or "general")
        _is_startup = bool(body.get("startup")) or _ptype == "startup"
        _dnd_exempt = bool(body.get("dnd_exempt")) or _ptype in {
            "pending_task", "reminder", "crisis"
        }
        # ★ 2026-09-15：闸门链已抽成**单一实现** `_proactive_precheck()`（与
        #   /api/proactive/precheck 共用）。本端点从此只负责"登记"：
        #   内容清理 → 冷却 → 去重 → 入库 → 记账 → 反馈闭环。
        #   这样任何新增闸门只改一处，前端也不可能再"生成完 42k 上下文才被拦"。
        _pc = await _proactive_precheck(
            session_id, character_id, ptype=_ptype, force=force, dnd_exempt=_dnd_exempt,
            startup=_is_startup, user_requested_audio=bool(body.get("user_requested_audio")),
            caller="_register")
        _exempt_kind = str(_pc.get("exempt") or "")
        if not _pc.get("allowed"):
            return {"ok": True, "allowed": False, "reason": _pc.get("reason"),
                    "retry_after": _pc.get("retry_after"), "hint": _pc.get("hint", "")}
        _allow_greetings = _ptype in {"morning", "night", "good_morning", "good_night", "festival", "milestone"}
        try:
            from .intimacy_manager import get as _get_intimacy
            _iv = _get_intimacy(session_id, character_id)
        except Exception:
            _iv = 0
        _max_chars = 180 if _iv >= 90 else 140 if _iv >= 70 else 100
        _require_hook = _ptype not in {"morning", "night", "good_morning", "good_night", "festival", "milestone", "emotion_intervention"}
        # ★ 2026-09-14（改 #2）：禁词不再让这条消息作废 —— 前端已尝试请模型重写一次，
        #   到这里直接清理后登记（reject_banned=False）；只有"清理后为空"才拒绝。
        content = sanitize_proactive_message(
            body.get("content"), require_hook=_require_hook, max_chars=_max_chars,
            allow_greetings=_allow_greetings, reject_banned=False,
            session_id=session_id, character_id=character_id,
        )
        if not content:
            return JSONResponse({"ok": False, "allowed": False, "error": "主动消息为空"}, status_code=400)


        # 上线问候是“用户重新打开 APP 后的一次轻触达”，不被普通主动消息 5 分钟冷却挡住。
        # 前端另有 45 秒本地冷却，避免反复重开窗口刷屏。
        remaining = 0 if (force or _is_startup) else cooldown_remaining(session_id, character_id, 300)
        if remaining > 0:
            return {"ok": True, "allowed": False, "retry_after": remaining, "reason": "cooldown"}
        if not force and not _is_startup and is_similar_to_recent(session_id, character_id, content):
            return {"ok": True, "allowed": False, "retry_after": 900, "reason": "duplicate"}
        pushed_at = int(datetime.now().timestamp())
        message_id = db.add_message(session_id, "assistant", content, character_id, extra={
            "source": "proactive",
            "proactive_type": _ptype,
            "format_id": str(body.get("format_id") or ""),
            "category": str(body.get("category") or ""),
            "dnd_exempt": _dnd_exempt,
            "startup": _is_startup,
            "pushed_at": pushed_at,
            "replied": 0,
        })
        # ★ 2026-09-14：放行也留痕（**先读旧值再写**，否则读到的是刚写进去的自己）
        try:
            from .proactive_quality import cooldown_key as _ck4
            _prev4 = float(db.kv_get(_ck4(session_id, character_id)) or 0)
        except Exception:
            _prev4 = 0
        mark_sent(session_id, character_id, pushed_at)
        _gap4 = (pushed_at - _prev4) / 60 if _prev4 else -1
        print(f"[ProactiveRegister] 放行: session={session_id} char={character_id} type={_ptype} "
              f"format_id={body.get('format_id')} "
              + (f"距上次主动={_gap4:.0f} 分" if _gap4 >= 0 else "距上次主动=（无记录）"),
              flush=True)
        # ★ 2026-09-15：前端这条链路以前只写 print，不写结构化 trace ——
        #   "哪天几点发了什么、间隔多少、有没有走豁免"事后只能翻日志。
        try:
            from . import proactive_trace as _pto
            _pto.record_deliver(session_id, character_id, content, proactive_type=_ptype,
                                source="_register", dnd_silent=bool(_dnd_exempt))
            _pto.record_gate(session_id, character_id, who="_register", ptype=_ptype,
                             decision="allow", reason="passed",
                             detail=("距上次主动 %.0f 分" % _gap4) if _gap4 >= 0 else "无历史记录",
                             exempt=_exempt_kind,
                             numbers={"since_last_min": _gap4} if _gap4 >= 0 else None)
        except Exception:
            pass
        return {"ok": True, "allowed": True, "message_id": message_id, "content": content}
    except Exception as e:
        print(f"[ProactiveRegister] 异常: {e}", flush=True)
        return JSONResponse({"ok": False, "allowed": False, "error": str(e)}, status_code=500)


@app.post("/api/proactive/precheck")
async def api_proactive_precheck(request: Request):
    """主动消息**生成前预检**（2026-09-15，治本）：前端先问一句，再决定要不要生成。

    背景：闸门此前有两套实现 —— 前端自己算时段/退避、后端 register 再算一遍，
    两边数据会漂。实测后果：前端完整生成了一条 42k 上下文的主动消息
    （`in=42606`、首字 45~160 秒），后端才在 register 把它拦下，每 11 分钟白烧一次。

    现在：前端在**生成之前**调用本端点（纯判断，无 LLM，毫秒级），
    `allowed=false` 时按 `retry_after` 退避（不再生成）；返回的 `hours` 还可用来
    刷新前端缓存的角色时段。闸门逻辑与 register **同一个函数**（`_proactive_precheck`），
    以后新增闸门只改一处。

    返回：{ok, allowed, reason, retry_after, hint, hours, lo_min, next_window_start, exempt}
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _precheck_payload(body)


@app.get("/api/proactive/precheck")
async def api_proactive_precheck_get(session_id: str = "default", character_id: str = "default",
                                     proactive_type: str = "general",
                                     character_name: str = "", force: bool = False,
                                     startup: bool = False):
    """GET 版预检（方便 curl / 探针脚本）。语义与 POST 完全一致。"""
    return await _precheck_payload({
        "session_id": session_id, "character_id": character_id,
        "character_name": character_name or character_id,
        "proactive_type": proactive_type, "force": force, "startup": startup,
    })


async def _precheck_payload(body: dict) -> dict:
    try:
        session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
        _ptype = str(body.get("proactive_type") or "general")
        _pc = await _proactive_precheck(
            session_id, character_id, ptype=_ptype,
            force=bool(body.get("force")),
            dnd_exempt=bool(body.get("dnd_exempt")) or _ptype in {"pending_task", "reminder", "crisis"},
            startup=bool(body.get("startup")) or _ptype == "startup",
            user_requested_audio=bool(body.get("user_requested_audio")),
            caller="_precheck")
        return {"ok": True, **{k: v for k, v in _pc.items()}}
    except Exception as e:
        print(f"[ProactivePrecheck] 异常(默认放行): {type(e).__name__}: {e}", flush=True)
        return {"ok": False, "allowed": True, "reason": "precheck_error",
                "retry_after": 0, "hint": str(e)}


@app.get("/api/user/state")
async def api_user_state(session_id: str = "default", character_id: str = "default"):
    """"宝现在是什么状态"——供主动消息链路判断（睡觉/闲聊静默/活跃）。

    ★ 2026-09-14 新增（用户要求：「模型得知道我的状态，我在睡觉怎么回她」）：
      · 用户说「去睡了/晚安」→ record_sleep_declared 记时间，6 小时内为 sleeping；
      · 前端主动消息链路（app.js proactiveTick）在生成**之前**先查这里：
        睡着时**静默等待**（不发消息、不追问），用户一说话自动解除；
      · 提醒/承诺（pending_task）仍可穿透 —— 那是用户自己定的点。
    """
    try:
        from . import offline as _offline
        hrs = float(_offline.hours_since_sleep_declared(session_id, character_id))
        sleeping = bool(_offline.is_user_sleeping(session_id, character_id))
        try:
            last = db.last_activity_time(session_id)
            idle_min = int((datetime.now() - last).total_seconds() // 60) if last else -1
        except Exception:
            idle_min = -1
        return {
            "ok": True,
            "sleeping": sleeping,
            "hours_since_sleep": round(hrs, 2) if hrs < 1e6 else None,
            "silence_hours": float(_offline.SLEEP_SILENCE_HOURS),
            "idle_minutes": idle_min,
        }
    except Exception as e:
        print(f"[UserState] 异常: {e}", flush=True)
        return {"ok": False, "sleeping": False, "error": str(e)}


# ---------------- 原接口：/api/chat（SSE 流式 / 非流式后台任务） ----------------

def _sse_line(obj) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ★ P1-10：原 `_async_gen_tts_for_web()`（网页端 TTS 异步生成，经 WS 推 web_audio）
#   全项目零调用点，属死代码，已删除。若日后要做"网页端语音自动播放"，
#   可从 git 历史取回该函数。


async def _offline_reply_later(session_id, character_id, user_text, messages, base_url, delay_seconds, status):
    """离线延迟回复后台任务：到点后生成回复，经 WS 推送（前端 onProactive 渲染）。"""
    import random
    pending_key = ""
    try:
        digest = hashlib.sha1(str(user_text or "").encode("utf-8")).hexdigest()[:12]
        pending_key = f"offline_pending:{session_id}:{character_id}:{status}:{digest}"
    except Exception:
        pending_key = ""
    try:
        await asyncio.sleep(max(1, int(delay_seconds or 0)))
    except Exception:
        pass

    try:
        from . import offline as _offline
        from .chat_logic import enrich_messages
        from .deepseek_api import chat_once as _chat_once

        # 1. 到点后重新构建上下文（此刻的记忆/关系更贴切）
        base_messages = await enrich_messages(messages, session_id, character_id)

        # 2. 注入「回来后交代」
        ret_ctx = _offline.get_return_context(status, int(delay_seconds or 0))
        if ret_ctx:
            _hit = False
            for _m in base_messages:
                if _m.get("role") == "system":
                    _m["content"] = (_m.get("content") or "") + "\n\n" + ret_ctx
                    _hit = True
                    break
            if not _hit:
                base_messages.insert(0, {"role": "system", "content": ret_ctx})

        # 3. 情绪更新（用用户消息语义）
        ai_emotion = {"emotion": "calm", "intensity": 0.5}
        _sem = None
        try:
            from .companion_os.controller import get_companion_os
            _brain = await get_companion_os().process_async(session_id, character_id, user_text)
            _sem = (_brain or {}).get("semantic_state")
        except Exception:
            pass
        try:
            _eng = _get_emotion_engine()
            if _eng:
                ai_emotion = _eng.update_from_semantic(session_id, character_id, _sem)
        except Exception:
            pass

        # 4. 分段生成（复用 multi_turn；降级 chat_once）
        turns = []
        try:
            _gen = _get_multi_turn()
            if _gen:
                turns = await _gen.generate(
                    base_messages=base_messages, ai_emotion=ai_emotion,
                    session_id=session_id, character_id=character_id,
                )
        except Exception:
            turns = []
        if not turns:
            try:
                _model = chat_logic.pick_model(None, True, character_id)
                _key = config.api_key_for_model(_model)
                raw = await _chat_once(_model, base_messages, _key, temperature=0.7, max_tokens=1024)
                turns = [{"content": (raw or "嗯。").strip(), "emotion": ai_emotion.get("emotion", "calm")}]
            except Exception:
                turns = [{"content": "我刚回来，才看到你的消息～", "emotion": "calm"}]

        # ★ 去重：离线延迟回复此前没有去重，会跟历史说过的话撞车。
        #   复用与 /api/chat/stream 同一套「逐条判定 + 只重生成命中那一条」，
        #   保住多段结构，不把 turns 压成一条。
        try:
            from .companion.quality_guard import dedup_check_async, recent_ai_texts
            from .deepseek_api import chat_once as _od_chat_once
            _od_model = chat_logic.pick_model(None, True, character_id)
            _od_key = config.api_key_for_model(_od_model)
            if _od_key:
                for _t in turns:
                    _c = str(_t.get("content") or "").strip()
                    if not _c:
                        continue
                    try:
                        if not await dedup_check_async(
                            _c, session_id, character_id,
                            chat_once_fn=_od_chat_once, model=_od_model,
                            api_key=_od_key,
                        ):
                            continue
                        _new = await _dedup_regenerate(
                            base_messages, session_id, character_id,
                            recent_ai_texts(session_id, character_id, 5),
                            _od_chat_once, _od_model, _od_key,
                        )
                        if _new:
                            _t["content"] = _new
                    except Exception:
                        continue
        except Exception as _ode:
            print(f"[OfflineReply] 去重失败(静默): {_ode}", flush=True)

        # 5. 逐段 WS 推送 + 落库
        # ★ 修 NameError：原先这里直接用 prepare_visible_text，但函数内直到第 6 步才 import，
        #   首次执行到此处会抛 NameError 并被外层 except 吞掉，导致离线回复整段静默失效。
        from .tts import prepare_visible_text
        from .character_manager import get_action_brackets
        _ab_on = get_action_brackets(character_id)
        collected = []
        for idx, t in enumerate(turns):
            content = prepare_visible_text(t.get("content") or "", action_brackets=_ab_on).strip()
            if not content:
                continue
            payload = {
                "type": "proactive",
                "session_id": session_id,
                "contact_id": character_id,   # 角色名，前端 getContact 按 name 匹配
                "contact_name": character_id,
                "content": content,
                "emotion": t.get("emotion", "calm"),
                "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "_offline_reply": True,
            }
            try:
                from .tts import generate_audio, apply_emotion_to_voice_cfg, prepare_visible_text
                from .character_manager import select_character_voice_cfg
                from .voice_trigger import should_send_voice
                _emo = t.get("emotion") or ai_emotion.get("emotion") or "calm"
                # ★ 离线回复发语音也走概率+场景+偏好，不再"配音色就 100% 发"
                if not should_send_voice(session_id, character_id):
                    _vc = None
                else:
                    _vc = select_character_voice_cfg(
                        character_id or "default", emotion=_emo, mode="chat"
                    )
                if _vc:
                    _int = float(ai_emotion.get("intensity", 0.5))
                    _vcemo = apply_emotion_to_voice_cfg(_vc, _emo, _int)
                    _au = await generate_audio(content, _vcemo, base_url=base_url)
                    if _au:
                        payload["audio"] = _au
            except Exception:
                pass
            try:
                await ws_manager.push_to_session(session_id, payload)
            except Exception:
                pass
            collected.append(content)
            # ★ 与 /api/chat/stream 一致：逐条即时落库。
            #   离线回复是「延迟几十秒到几分钟」才发出来的，这期间用户很可能
            #   已经又说了别的话。等全部推完再一次性落库的话，下一轮请求读到
            #   的历史里没有这些气泡 —— 去重检测不到重复、模型也看不见 AI 刚
            #   说了什么，于是又出现「重复 + 不知道上一句」。
            try:
                # 同 /api/chat/stream：带上语音 URL，否则离线语音消息
                # 在前端重启同步后会退化成纯文字。
                _extra = {"audio": payload["audio"]} if payload.get("audio") else None
                db.add_message(session_id, "assistant", content, character_id,
                               extra=_extra)
            except Exception:
                pass
            if len(turns) > 1 and idx < len(turns) - 1:
                await asyncio.sleep(1.2 + random.random() * 1.6)

        # 6. collected 仅用于判空；各条气泡已在推送时逐条落库。
        #   （用户消息在收到时就已落库，这里不再重复写）
    except Exception as e:
        print(f"[OfflineReply] 后台任务异常: {e}", flush=True)
        import traceback as _tb
        _tb.print_exc()
    finally:
        if pending_key:
            try:
                db.kv_delete(pending_key)
            except Exception:
                pass


def _claim_offline_reply_once(session_id: str, character_id: str, user_text: str, status: str, delay_seconds: int = 0) -> bool:
    """同一角色同一句离线消息只排队一次，避免前端重试/双入口造成重复“睡醒后回复”。"""
    try:
        digest = hashlib.sha1(str(user_text or "").encode("utf-8")).hexdigest()[:12]
        key = f"offline_pending:{session_id}:{character_id}:{status}:{digest}"
        return db.kv_claim_once(key, str(int(delay_seconds or 0)))
    except Exception:
        return True


async def _vision_summary_nonstream(image: str, text: str, vkey: str, vmodel: str, vbase_url: str = None, vprovider: str = ""):
    """路由层独立非流式视觉调用（不碰 stream_vision 内部）"""
    try:
        import httpx
        from .http_client import get_http_client
        _prompt = "用一句中文概括这张图里最值得记住的场景/人物/物品，20字内"
        _msg = {"role": "user", "content": [
            {"type": "text", "text": _prompt},
            {"type": "image_url", "image_url": {"url": image}}
        ]}
        if vprovider == "siliconflow":
            url = "https://api.siliconflow.cn/v1/chat/completions"
            body = {"model": vmodel, "messages": [_msg], "stream": False, "temperature": 0.3}
            headers = {"Authorization": f"Bearer {vkey}"}
        else:
            # ★ 兼容 zhipu（glm-5.3-flash）等自定义 baseUrl：只到 /v1 或 /v4 时补 /chat/completions，否则 404
            _base = (vbase_url or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
            url = _base if _base.endswith("/chat/completions") else _base + "/chat/completions"
            body = {"model": vmodel, "messages": [_msg], "stream": False, "temperature": 0.3}
            headers = {"Authorization": f"Bearer {vkey}"}
        client = get_http_client()
        r = await client.post(url, json=body, headers=headers)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[VisionSummary] 非流式摘要失败: {e}", flush=True)
        return None


# ---------------- ASR 语音转文本 ----------------
@app.post("/api/asr")
async def api_asr(request: Request):
    """
    语音转文本接口
    支持两种上传方式：
    · multipart/form-data：file 字段传音频文件（推荐）
    · application/json：{"audio_base64":"...", "filename":"audio.wav"}
    返回：{"text": "识别结果"} 或 {"error": "..."}
    """
    from .asr import transcribe
    try:
        content_type = request.headers.get("content-type", "")
        if "multipart" in content_type:
            form    = await request.form()
            file    = form.get("file")
            if not file:
                return JSONResponse({"error": "缺少 file 字段"}, status_code=400)
            audio_bytes = await file.read()
            filename    = getattr(file, "filename", "audio.wav") or "audio.wav"
        else:
            body        = await request.json()
            import base64 as _b64
            audio_b64   = body.get("audio_base64", "")
            filename    = body.get("filename", "audio.wav")
            if not audio_b64:
                return JSONResponse({"error": "缺少 audio_base64"}, status_code=400)
            if not isinstance(audio_b64, str) or len(audio_b64) > 14 * 1024 * 1024:
                return JSONResponse({"error": "音频数据过大"}, status_code=400)
            audio_bytes = _b64.b64decode(audio_b64, validate=True)

        if len(audio_bytes) > 10 * 1024 * 1024:   # 10MB 上限
            return JSONResponse({"error": "音频文件过大（上限10MB）"}, status_code=400)

        text = await transcribe(audio_bytes, filename)
        if text:
            return JSONResponse({"text": text})
        else:
            return JSONResponse({"error": "识别失败，请重试"}, status_code=500)
    except Exception as e:
        print(f"[ASR Route] 异常: {e}", flush=True)
        # Malformed base64/request data is a client error, not a backend crash.
        if isinstance(e, (ValueError, TypeError)) or e.__class__.__module__ == "binascii":
            return JSONResponse({"error": "音频数据格式错误"}, status_code=400)
        return JSONResponse({"error": "语音识别失败，请重试"}, status_code=500)


# ---------------- 阿里云百炼：声音复刻 ----------------
def _replica_source_candidates() -> list:
    """列出可用于复刻的参考音频（本地已克隆的音色样本）"""
    import os as _os
    out = []
    try:
        # ★ 修复路径不一致：克隆音色存在 config.DATA_DIR/voice_clone_models（打包版=userData/data，
        #   持久），旧代码却从 os.path.dirname(__file__)（打包版=临时解压目录）读，
        #   导致复刻永远"找不到文件"。这里与 voice_clone._CLONE_DIR 对齐。
        from . import config as _cfg
        reg_path = _os.path.join(str(_cfg.DATA_DIR), "voice_clone_models", "registry.json")
        if _os.path.exists(reg_path):
            import json as _json
            with open(reg_path, "r", encoding="utf-8") as f:
                reg = _json.load(f) or {}
            for k, v in reg.items():
                p = v.get("ref_audio") or v.get("voice_id") or ""
                if p and _os.path.exists(p):
                    out.append({
                        "key": k,
                        "label": v.get("label") or k,
                        "path": p,
                        "prompt_text": v.get("prompt_text") or "",
                        "size_kb": round(_os.path.getsize(p) / 1024),
                    })
    except Exception as e:
        print(f"[复刻] 读取音色注册表失败: {e}", flush=True)
    return out


@app.get("/api/voice-replica/options")
async def api_voice_replica_options():
    """可用作复刻参考的音频列表 + 当前复刻状态"""
    from . import config as _cfg
    return {
        "sources": _replica_source_candidates(),
        "current": _cfg.voice_replica_voice_id(),
        "current_source": _cfg.voice_replica_source(),
        "key_set": bool(_cfg.dashscope_api_key()),
    }


@app.get("/api/voice-replica/status")
async def api_voice_replica_status():
    """当前云端复刻音色状态"""
    from . import config as _cfg
    return {
        "key_set": bool(_cfg.dashscope_api_key()),
        "voice_id": _cfg.voice_replica_voice_id(),
        "target_model": _cfg.voice_replica_target_model(),
        "source": _cfg.voice_replica_source(),
    }


@app.post("/api/voice-replica/create")
async def api_voice_replica_create(request: Request):
    """用指定参考音频在云端复刻音色（base64 直传，约需数秒到数十秒）"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    source    = str(body.get("source") or "").strip()
    preferred = str(body.get("preferred_name") or "").strip() or "guzi"

    # 允许传索引或注册表 key，从候选里挑出真实路径
    cands = _replica_source_candidates()
    if source.isdigit():
        idx = int(source)
        if 0 <= idx < len(cands):
            source = cands[idx]["path"]
    elif source and source not in [c["path"] for c in cands]:
        # 传的是注册表里的 key（如 clone_xxx_yyy）
        for c in cands:
            if c["key"] == source:
                source = c["path"]
                break

    if not source or not os.path.exists(source):
        if not cands:
            return JSONResponse({"ok": False, "error": "没有可用的参考音频"}, status_code=400)
        source = cands[0]["path"]     # 默认用第一个克隆样本

    from . import tts as _tts
    result = await _tts.create_voice_replica(source, preferred_name=preferred)
    if result.get("ok"):
        await ws_manager.broadcast({"type": "config", "config": pc_config_payload()})
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.get("/api/voice-replica/list")
async def api_voice_replica_list():
    """云端已复刻的音色列表"""
    from . import tts as _tts
    return await _tts.list_voice_replicas()


@app.post("/api/voice-replica/clear")
async def api_voice_replica_clear():
    """清除复刻音色（通话回到云端系统音色）"""
    from . import config as _cfg
    _cfg.set_voice_replica("", "", "")
    await ws_manager.broadcast({"type": "config", "config": pc_config_payload()})
    return {"ok": True}


# ---------------- Agent 子系统：授权 + 目标 ----------------

@app.get("/api/agent/approval/{aid}")
async def api_agent_approval_get(aid: str):
    """前端查询授权状态 / 展示改动预览。"""
    from .agent import approval as _ap
    return _ap.get_approval(aid)


@app.post("/api/agent/approval/{aid}/approve")
async def api_agent_approval_approve(aid: str):
    """用户同意这次危险操作。"""
    from .agent import approval as _ap
    ok = _ap.approve(aid)
    return {"ok": ok}


@app.post("/api/agent/approval/{aid}/reject")
async def api_agent_approval_reject(aid: str, request: Request):
    """用户取消这次危险操作（可带原因，供助手追问/反思）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = str(body.get("reason") or "").strip()
    from .agent import approval as _ap
    ok = _ap.reject(aid, reason)
    return {"ok": ok}


@app.post("/api/agent/goal")
async def api_agent_goal_create(request: Request):
    """记录一个目标（助手之后主动推进、提醒进度）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    content = str(body.get("content") or "").strip()
    session_id = str(body.get("session_id") or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    from .agent import goal as _goal
    gid = _goal.record_goal(session_id, character_id, content)
    return {"ok": bool(gid), "goal_id": gid}


@app.get("/api/agent/goals")
async def api_agent_goals_list(session_id: str = "default", character_id: str = "default",
                               active_only: bool = True):
    """列出目标。"""
    cid = _resolve_char_id(character_id, "")
    from .agent import goal as _goal
    goals = _goal.list_goals(session_id, cid, active_only=active_only)
    return {"ok": True, "goals": goals}


# ---------------- Agent 窗口（干活模式）----------------
# 前端「干活」窗口的 7 个端点：状态 / 发话 / 确认计划 / 插话 / 停 / 结算审批卡 / 升档。
# 编排全在 backend/agent/window.py；这里只做 body 解析 + 注入角色隔离键（与上面 Agent 段同风格）。

@app.get("/api/agent/window/state")
async def api_agent_window_state(session_id: str = "default", character_id: str = "default"):
    from .agent import window as _win
    return _win.state(session_id)


@app.post("/api/agent/window/send")
async def api_agent_window_send(request: Request):
    body = await request.json()
    from .agent import window as _win
    sid = str(body.get("session_id") or "default")
    cid = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    return await _win.send(sid, cid, body.get("text") or "", cwd=str(body.get("cwd") or ""),
                           mode=str(body.get("mode") or "auto"))


@app.post("/api/agent/window/confirm")
async def api_agent_window_confirm(request: Request):
    body = await request.json()
    from .agent import window as _win
    sid = str(body.get("session_id") or "default")
    cid = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    return await _win.confirm(sid, cid, str(body.get("task_id") or ""))


@app.post("/api/agent/window/interject")
async def api_agent_window_interject(request: Request):
    body = await request.json()
    from .agent import window as _win
    return await _win.interject(str(body.get("session_id") or "default"),
                                str(body.get("task_id") or ""), body.get("text") or "",
                                interrupt=bool(body.get("interrupt")))


@app.post("/api/agent/window/stop")
async def api_agent_window_stop(request: Request):
    body = await request.json()
    from .agent import window as _win
    return await _win.stop(str(body.get("session_id") or "default"), str(body.get("task_id") or ""))


@app.post("/api/agent/window/approve")
async def api_agent_window_approve(request: Request):
    body = await request.json()
    from .agent import window as _win
    return await _win.approve(str(body.get("approval_id") or ""),
                              bool(body.get("allow")), str(body.get("reason") or ""))


@app.post("/api/agent/window/upgrade")
async def api_agent_window_upgrade(request: Request):
    body = await request.json()
    from .agent import window as _win
    return await _win.set_upgrade(str(body.get("session_id") or "default"),
                                  bool(body.get("enable")), confirmed=bool(body.get("confirmed")))


def _resolve_char_id(cid, cname=""):
    """
    统一人格隔离键解析（全后端记忆/调度/来电共用）：
    - character_id 能映射到真实角色配置才优先使用（前端传的 localStorage UUID 会被跳过）
    - 否则用 character_name（角色名，如「助手」，对应 角色配置/助手.config.json）
    - 最后兜底 default
    保证「同一个联系人名字 = 同一个隔离键」，记忆读写不串桶。
    """
    cid = str(cid or "").strip()
    cname = str(cname or "").strip()
    if cid and cid != "default":
        try:
            from .character_manager import get_character, load_character
            if get_character(cid) or load_character(cid):
                return cid
        except Exception:
            pass
    return cname or "default"


def _record_correction(session_id: str, character_id: str, intent: dict,
                       user_text: str, tag: str = "") -> None:
    """记录用户纠正——第一层 DeepSeek 判定用户正在教 AI。

    只存「用户否定了 AI 的说法 **且** 给出了正确答案」的情况；
    单纯抱怨"你说得不对"没有可学习的内容，存下来只是噪音
    （这个判断在 understanding._norm_correction 里已经做过一次，这里再确认）。

    任何异常一律静默——记住用户的话很重要，但不能因此拖垮聊天主流程。
    """
    try:
        cor = (intent or {}).get("correction") or {}
        if not cor.get("is_correction"):
            return
        from .feedback.corrections import save_correction
        _cid = save_correction(
            session_id, character_id, cor, user_text,
            (intent or {}).get("confidence", 0))
        if _cid:
            print(f"[Correction]{tag} 记录用户纠正 topic={cor.get('topic')} "
                  f"right={cor.get('right')} text={user_text[:30]}", flush=True)
    except Exception as _ce:
        print(f"[Correction]{tag} 记录失败(静默): {_ce}", flush=True)


def _parse_voice_pref(user_text, session_id, character_id):
    """识别语音偏好指令并落库，返回确认回复文案（无指令返回 None）。"""
    try:
        from .voice_trigger import parse_voice_pref_command, set_voice_pref
        mode = parse_voice_pref_command(user_text)
        if not mode:
            return None
        set_voice_pref(session_id, character_id, mode)
        if mode == "text":
            return "好，以后我多用文字跟你聊，你想听语音随时说～"
        return "知道啦，你想听语音的时候，我多发给你～"
    except Exception as _e:
        print(f"[VoicePref] 解析失败(静默): {_e}", flush=True)
        return None


async def _inject_shared_context(messages, session_id, character_id,
                                 user_text="", with_realness=False, lite=False):
    """把两个聊天入口共用的 system prompt 增强块统一注入（原地修改 messages）。

    /api/chat 与 /api/chat/stream 原先各写一遍相同逻辑，且覆盖不一致：
      · stream 缺【记忆上下文 / 关系状态 / 反思】—— 好感度 >= 85 的用户走的
        正是 stream 入口，于是越亲密反而收不到个性化记忆和关系状态
      · api_chat 缺【真人感增强】
    收敛成一份后两个入口行为对齐，重复代码只剩一处。

    每一块都独立 try/except：任何一块失败只影响它自己，绝不影响主流程。
    with_realness：是否注入真人感块（目前仅 stream 开启，保持其原有行为）。
    """
    def _append_system(text, sep="\n"):
        """把一段提示追加到第一条 system 消息；没有 system 就插一条新的。"""
        if not text:
            return
        for m in messages:
            if m.get("role") == "system":
                m["content"] = (m.get("content") or "") + sep + text
                return
        messages.insert(0, {"role": "system", "content": text})

    # 1. 长期记忆上下文（按当前话题语义召回）
    #    lite（游戏场景）：recent/topic 砍半省 token；get_recent 内部按
    #    importance DESC 排序，所以注入的仍是最重要的记忆，不会丢关键事。
    try:
        _topic = str(user_text or "")[:100] or None
        _recent_lim = 4 if lite else 8
        _topic_lim = 2 if lite else 3
        # build_full_memory_context 含向量检索 + 多次 sqlite，丢线程池不阻塞事件循环
        memory_context = await asyncio.to_thread(
            yunlink_memory.build_full_memory_context,
            user_id=session_id, current_topic=_topic,
            character_id=character_id, recent_limit=_recent_lim, topic_limit=_topic_lim,
        )
        _append_system(memory_context)
    except Exception as e:
        print(f"[SharedCtx] 记忆注入失败: {e}", flush=True)

    # 2. 音色自知觉察：让 AI 知道自己用什么声音说话（lite 游戏场景跳过，省 token）
    if not lite:
        try:
            from .character_manager import resolve_character_voice_cfg
            _vcfg = resolve_character_voice_cfg(character_id or "default", infer=True)
            if _vcfg:
                _vlabel = _vcfg.get("label") or _vcfg.get("voice_id")
                _append_system(
                    f"[音色设定] 你当前以「{_vlabel}」声线发声（前端会播放你的语音）。"
                    f"若用户提及换声音/用某克隆声，你需知晓此设定并配合。")
        except Exception as e:
            print(f"[SharedCtx] 声线注入失败: {e}", flush=True)

    # 2.5 称呼：用户设置的亲昵称呼（如"老公"）必须真正用在对话里。
    #     此前 call_user 只在通话场景注入（voice_call.py），打字聊天完全收不到，
    #     于是 AI 只会回一句"我记住了～以后就这么叫"，却从不真的这样叫——
    #     用户体感就是"设置生效了但看不出来"。这里补上对话侧的注入。
    try:
        from .character_manager import get_character_any
        _ccfg = get_character_any(character_id or "default") or {}
        _cu = str(_ccfg.get("call_user") or "").strip()
        if _cu and _cu not in ("你", "用户"):
            _append_system(
                f"【称呼】你平时称呼对方为「{_cu}」，对话中要自然地这样叫 TA。"
                f"不要只是声明「我记住了」却始终不叫出口。")
    except Exception as e:
        print(f"[SharedCtx] 称呼注入失败: {e}", flush=True)

    # 3. 长期渐显三阶段：专属习惯慢慢长，不切断只控浓淡（lite 游戏场景跳过）
    if not lite:
        try:
            from .relationship.manager import RelationshipManager
            _rm = RelationshipManager()
            _tenure = _rm.get_tenure_days(session_id, character_id)
            _aff = (_rm.get_state(session_id, character_id) or {}).get("affection", 50)
            if _tenure < 30:
                _w = "几乎不显露专属习惯，只偶尔潜意识学他停顿"
            elif _tenure < 120:
                _w = "开始自然流露专属小习惯（如先问睡没睡），但别刻意"
            else:
                _w = "深度绑定：你的专属怪癖（先问睡/学他叹气）已成本能，坦然流露"
            _append_system(
                f"【专属习惯】你与该用户长期相伴已 {_tenure} 天（亲密度 {_aff}）。"
                f"请根据以下浓淡自然流露，不要生硬照搬：{_w}")
        except Exception as e:
            print(f"[SharedCtx] 长期渐显失败: {e}", flush=True)

    # 4. 关系状态
    try:
        _append_system(
            yunlink_relationship.build_relationship_prompt(
                user_id=session_id, character_id=character_id))
    except Exception as e:
        print(f"[SharedCtx] 关系状态注入失败: {e}", flush=True)

    # 5. 反思（自动改 Prompt 的关键；lite 时砍到 3 条省 token）
    try:
        from .reflection.strategy import build_reflection_prompt
        _append_system(
            build_reflection_prompt(session_id, character_id, limit=3 if lite else 10),
            sep="\n\n")
    except Exception as e:
        print(f"[SharedCtx] 反思注入失败: {e}", flush=True)

    # 6. 真人感增强（仅 stream 入口开启）
    if with_realness:
        try:
            from .companion.realness import build_realness_prompt
            from .character_manager import get_character_any
            _eng = _get_emotion_engine()
            _emo, _inten = "calm", 0.5
            if _eng:
                _st = _eng.get_state(session_id, character_id or "default")
                if _st:
                    _emo = _st.get("emotion", "calm")
                    _inten = float(_st.get("intensity", 0.5))
            _realness = build_realness_prompt(
                session_id=session_id, character_id=character_id,
                emotion=_emo, intensity=_inten, user_text=user_text,
                character_config=get_character_any(character_id or "default"))
            # 已注入过就不重复（enrich_messages 可能已经带了一块）
            if _realness and any(
                m.get("role") == "system"
                and "【真人感行为指令】" in str(m.get("content") or "")
                for m in messages
            ):
                _realness = ""
            _append_system(_realness, sep="\n\n")
        except Exception as e:
            print(f"[SharedCtx] 真人感注入失败: {e}", flush=True)

    # 7. 话题推进·防复读（lite 游戏场景跳过，省 token）
    if not lite:
        try:
            from .companion.quality_guard import recent_ai_texts as _rat
            _said = [s for s in _rat(session_id, character_id, 6) if str(s or "").strip()]
            if _said:
                _said_lines = "\n".join(f"- {str(s)[:60]}" for s in _said[-5:])
                _append_system(
                    "【话题推进·防复读】\n"
                    "你最近说过这些话：\n" + _said_lines + "\n"
                    "硬性规则：\n"
                    "1. 不许把上面说过的意思换个说法再讲一遍——换个词也算复读。\n"
                    "2. 每次回复必须推进对话，三选一：\n"
                    "   ① 把话题往前带一步（新角度、新细节、新联想）；\n"
                    "   ② 说一个你自己此刻的新想法或感受（之前没说过的）；\n"
                    "   ③ 问用户一件和当前话题相关的具体小事。\n"
                    "3. 同一个情绪点表达过就翻篇（例如「怕你嫌我烦」这类确认只说一次），"
                    "不要来回确认同一件事。",
                    sep="\n\n")
        except Exception as e:
            print(f"[SharedCtx] 防复读注入失败: {e}", flush=True)


async def run_learning_signals(session_id, character_id, user_text, recent_ai_text=""):
    """一轮对话后的**学习信号**统一扫一遍（粗筛零成本，命中才调便宜模型）。

    ★ 2026-09-17 新增。为什么要有这个统一入口：
      · 风格反馈（"别讲道理/别敷衍"→ 以后怎么说话）原先只在**图片分支**里跑，
        纯文字聊天永远学不到（AST 取证 main.py:8156 被 has_image 包着）。
      · 规矩沉淀（"以后别半夜问我睡没睡"→ learned_rules）原先**只有 agent 工具**
        一个写入口，而工具只在助手模式的 agent 循环里跑 —— 日常聊天里立的规矩
        没有任何路径被记住（真机 learned_rules/助手.json 恒为 `[]`）。
      两件事的触发条件、成本模型、失败处理完全一样，合并成一个入口，
      文字/图片/QQ 哪条路径调用都不会漏。

    成本：两个正则粗筛（微秒级）；只有真的命中批评/立规矩时才各调一次
    memory_extract_model（便宜模型）并 fire-and-forget，不阻塞回复。
    失败一律静默 —— 学习重要，但绝不能拖垮聊天主流程。
    """
    try:
        from . import style_feedback as _sf
    except Exception:
        _sf = None
    try:
        from . import learned_rules as _lr
    except Exception:
        _lr = None

    _style_hit = bool(_sf and _sf.coarse_hit(user_text or ""))
    _rule_hit = bool(_lr and _lr.coarse_hit(user_text or ""))
    if not (_style_hit or _rule_hit):
        return

    async def _job():
        if _style_hit:
            try:
                res = await _sf.detect_and_learn(session_id, character_id, user_text,
                                                 recent_ai_text)
                if res.get("learned") and res.get("card"):
                    await ws_manager.push_to_session(session_id, {
                        "type": "memory_update", "session_id": session_id,
                        "contact_id": character_id, "character_id": character_id,
                        "learned": res["learned"],
                    })
            except Exception:
                pass
        if _rule_hit:
            try:
                res = await _lr.detect_and_learn_rule(session_id, character_id, user_text,
                                                      recent_ai_text)
                if res.get("learned") and res.get("card"):
                    await ws_manager.push_to_session(session_id, {
                        "type": "memory_update", "session_id": session_id,
                        "contact_id": character_id, "character_id": character_id,
                        "learned": res["learned"],
                    })
            except Exception:
                pass

    try:
        get_loop().create_task(_job())
    except Exception as _e:  # noqa: BLE001
        print(f"[Learning] 后台学习任务启动失败(静默): {_e}", flush=True)


@app.post("/api/chat")
async def api_chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    # ★ 剥离语音字段：历史里的语音消息带 audio url（前端还原播放用），
    #   透传给 Gemini 系中转会被转成无效 parts（contents[x].parts[0].data 400，生成全挂）
    try:
        for _m in messages:
            if isinstance(_m, dict) and _m.get("audio"):
                _m.pop("audio", None)
    except Exception:
        pass
    if not messages:
        return JSONResponse({"error": {"message": "没有消息内容"}}, status_code=400)
    if len(messages) > 24:
        return JSONResponse({"error": {"message": "消息条数超出限制"}}, status_code=400)

    stream = bool(body.get("stream", True))
    is_internal_generation = bool(
        body.get("proactive_internal")
        or body.get("skip_user_persist")
        or body.get("internal_user_prompt")
    )
    # ★ 多session v2.0：session_id 优先取请求参数，兼容老前端回退 active_session()
    session = str(body.get("session_id") or active_session() or "default").strip() or "default"
    # ★ 统一人格隔离键（读上下文/写历史/写记忆/调度/来电全部用它）：
    #   前端 /api/chat 只发 character_name（角色名如"助手"），此处解析后即角色名，
    #   保证 enrich_messages 读、count_round/add_message/maybe_auto_extract 写落在同一隔离键。
    _cid = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    # ★ 2026-09-17：非流式入口也接上学习信号（风格反馈 + 规矩沉淀）。
    #   原先只有 stream 入口里有，而且那段还错放在图片分支里 —— 于是
    #   `/api/chat` 这条路径（QQ/旧客户端/内部生成之外的调用）完全学不到东西。
    #   放在这里是因为 session/角色键都已解析完；函数内部自带零成本粗筛。
    if not is_internal_generation:
        try:
            _last_user = ""
            for _m in reversed(messages):
                if isinstance(_m, dict) and _m.get("role") == "user":
                    _last_user = str(_m.get("content") or "")
                    break
            if _last_user:
                await run_learning_signals(session, _cid or "default", _last_user)
        except Exception as _lse:
            print(f"[Learning] /api/chat 学习信号失败(静默): {_lse}", flush=True)
    # 确保该 session 有对应调度器（多 session 并发版）
    try:
        scheduler.scheduler.get_or_create(session, _cid)
    except Exception:
        pass

    # ★ 世界轻推：网页端上报城市 → 存 relationship_state（UPSERT 防御无行）
    _city = body.get("city")
    if _city:
        try:
            from .relationship.database import conn as rel_conn
            _city_cid = str(_cid or "default")
            rc = rel_conn()
            rc.execute("INSERT OR IGNORE INTO relationship_state(user_id, character_id) VALUES(?,?)",
                       (session, _city_cid))
            rc.execute("UPDATE relationship_state SET city=? WHERE user_id=? AND character_id=?",
                       (str(_city), session, _city_cid))
            rc.commit(); rc.close()
        except Exception as e:
            print(f"[City-chat] 存失败: {e}", flush=True)

    # ---- 手动记忆指令：「记住：xxx」直接入库，SSE 短路回复（不调模型） ----
    # 内部主动消息生成请求不允许触发“用户记住/偏好/关系”等普通聊天副作用。
    if stream and not is_internal_generation:
        # ★ check_manual_memory 内部 dedupe_insert 含 ST 向量编码+ChromaDB 写，丢线程池避免阻塞事件循环
        manual_reply = await asyncio.to_thread(chat_logic.check_manual_memory, messages, session, _cid)
        if manual_reply is not None:
            idle_mod.agent.on_user_activity(session)
            async def manual_gen():
                text = manual_reply
                for i in range(0, len(text), 8):
                    chunk = text[i:i + 8]
                    yield _sse_line({"choices": [{"index": 0, "delta": {"content": chunk},
                                                  "finish_reason": None}]})
                yield _sse_line({"choices": [{"index": 0, "delta": {},
                                              "finish_reason": "stop"}]})
                yield "data: [DONE]\n\n"
                yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True,
                                 "finish_reason": "stop"})
            return StreamingResponse(manual_gen(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 只判断"当前最新一条用户消息"是否带图片。
    # 历史聊天里曾经出现过图片，不应该让当前纯文字消息走视觉模型。
    last_user_message = None

    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_message = m
            break

    has_image = bool(
        last_user_message
        and last_user_message.get("image")
    )

    # ---- 图片 → 视觉模型（支持通义千问/硅基流动/自定义） ----
    if has_image:
        vkey = (str(body.get("visionKey") or "").strip()) or config.vision_key()
        if not vkey:
            return JSONResponse({"error": {"message":
                "图片消息需要视觉模型 Key：请在 App 设置页「视觉模型」填写，"
                "或在电脑 config.json 配置 vision_api_key"}}, status_code=400)
        vmodel = (str(body.get("visionModel") or "").strip()) or config.vision_model()
        vprovider = (str(body.get("visionProvider") or "").strip())
        vbase_url = (str(body.get("visionBaseUrl") or "").strip())
        # 如果前端没有传 provider 或 base_url，从模型池配置中获取
        if not vprovider or not vbase_url:
            try:
                vision_cfg = config.get_vision_model_config(vmodel)
                if not vprovider:
                    vprovider = vision_cfg.get("provider") or config.vision_provider()
                if not vbase_url:
                    vbase_url = vision_cfg.get("baseUrl") or config.vision_base_url()
            except Exception:
                if not vprovider:
                    vprovider = config.vision_provider()
                if not vbase_url:
                    vbase_url = config.vision_base_url()
        char_name = str(body.get("character_name") or "").strip() or None

        # ★ 补：图片预处理（和/api/chat/stream保持一致）
        _raw_image_http = last_user_message.get("image", "")
        if _raw_image_http:
            try:
                from .multimodal.vision import _normalize_image
                _processed_http = _normalize_image(_raw_image_http)
                if _processed_http and _processed_http != _raw_image_http:
                    last_user_message["image"] = _processed_http
                    print("[Vision] /api/chat 图片预处理完成", flush=True)
            except Exception as _pe:
                print(f"[Vision] /api/chat 图片预处理失败，用原图: {_pe}", flush=True)

        enriched = await chat_logic.enrich_messages(messages, session, _cid)

        # ★ 语义理解层：图片分支同样要接，否则用户发图时带的配文就不会被理解。
        #   这里 key/model/base_url 尚未解析（在后面），交给理解层自行从配置读取。
        try:
            from .understanding import analyze_and_inject
            enriched = await analyze_and_inject(
                enriched, str((last_user_message or {}).get("content") or ""),
                session_id=session, character_id=_cid,
                character_name=char_name or "",
                key=str(body.get("key") or "").strip(),
                is_internal=is_internal_generation,
            )
        except Exception as _ue:
            print(f"[Understanding] /api/chat(图片) 注入失败(静默): {_ue}", flush=True)

        # ★ 看图摘要入向量记忆（出口A）
        _img = last_user_message.get("image")
        _txt = last_user_message.get("content") or ""
        async def _img_mem_a():
            _sum = await _vision_summary_nonstream(_img, _txt, vkey, vmodel, vbase_url, vprovider)
            if _sum:
                try:
                    class _MLLM:
                        async def chat(self, p):
                            _mem_model = config.memory_extract_model(_cid)
                            return await chat_once(_mem_model, [{"role":"user","content":p}], config.memory_key(), temperature=0.2, max_tokens=200, reasoning_effort=("low" if config.model_supports_reasoning_effort(_mem_model) else None))
                    await yunlink_memory.save_from_chat(user_id=session, llm=_MLLM(),
                        chat=f"用户发图：「{_txt or '无配文'}」→ 图内容：{_sum}（已存视觉记忆）",
                        character_id=_cid)
                except Exception as e:
                    print(f"[ImgMem-A] 入向量失败: {e}", flush=True)
        get_loop().create_task(_img_mem_a())

        # 根据视觉服务商选择 API：siliconflow 用专用函数，其他用通用 stream_vision（支持自定义 base_url）
        provider_lower = vprovider.lower()
        use_silicon = provider_lower == "siliconflow" or "silicon" in provider_lower

        async def vision_gen():
            try:
                if use_silicon:
                    # 硅基流动：使用专用函数（内部固定 URL）
                    async for kind, val in stream_vision_silicon(enriched, vkey, vmodel):
                        if kind == "line":
                            yield val + "\n\n"
                        elif kind == "done":
                            yield "data: [DONE]\n\n"
                        elif kind == "meta":
                            yield _sse_line(val)
                else:
                    # 通义千问 / 自定义：使用通用 stream_vision，传入 base_url
                    async for kind, val in stream_vision(enriched, vkey, vmodel, base_url=vbase_url or None):
                        if kind == "line":
                            yield val + "\n\n"
                        elif kind == "done":
                            yield "data: [DONE]\n\n"
                        elif kind == "meta":
                            yield _sse_line(val)
            except ModelApiError as e:
                yield _sse_line({"error": {"message": str(e)}})

        return StreamingResponse(vision_gen(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # 文本请求禁止携带历史 image 字段。
    # 防止旧版前端或缓存数据把历史图片误传给文本模型。
    if not has_image:
        clean_messages = []

        for m in messages:
            clean_m = {
                "role": m.get("role", "user"),
                "content": m.get("content") or ""
            }

            if m.get("image"):
                clean_m["content"] = (
                    clean_m["content"]
                    if clean_m["content"] and clean_m["content"] != "[图片]"
                    else "[历史消息：发送过一张图片]"
                )

            clean_messages.append(clean_m)

        messages = clean_messages

    # ---- 文本路由 ----
    key = (str(body.get("key") or "").strip()) or config.api_key_for_model(
        str(body.get("model") or "").strip())
    # ★ 把前端传来的 Key 注入运行时（优先级最低，绝不覆盖已有配置），
    #   让语义分析、记忆抽取这类"后端自调用"模块也能拿到 Key ——
    #   打包运行时它们读不到配置里的 key，只能静默降级。
    try:
        config.set_runtime_api_key(str(body.get("key") or "").strip())
    except Exception:
        pass
    if not key:
        print("[AI-ERR] /api/chat 未配置 API Key，拒绝对话", flush=True)
        return JSONResponse({"error": {"message":
            "尚未配置 API Key：请在 App 设置页填写，或在电脑的 config.json 中填写后重启服务器"}},
            status_code=401)

    model = chat_logic.pick_model(body.get("model"), stream, _cid)
    # ★ 人格设置优先（2026-09-11）：最终模型可能 ≠ 前端请求的 model（角色卡主脑/分层大脑），
    #   key 与 base_url 必须按「最终模型」的 provider 重新分流，否则出现
    #   「gemini 模型 + 智谱 key/地址」的错配（理解层 2026-09-09 同款问题）。
    #   base_url：模型池里该模型自带地址优先（openclawplan 中转等），否则用前端自定义地址。
    key = config.api_key_for_model(model) or key
    base_url = str(body.get("baseUrl") or "").strip()
    try:
        _mcfg = config.get_text_model_config(model) or {}
        base_url = (str(_mcfg.get("baseUrl") or "").strip()) or base_url
    except Exception:
        pass
    try:
        temp = float(body.get("temperature"))
        temp = temp if 0 <= temp <= 2 else 0.7
    except (TypeError, ValueError):
        temp = 0.7
    try:
        top_p = float(body.get("top_p"))
        top_p = top_p if 0 <= top_p <= 1 else 1.0
    except (TypeError, ValueError):
        top_p = 1.0
    try:
        max_tokens = int(body.get("max_tokens"))
        # ★ 默认从 1024 提到 2048：长 persona/记忆注入 + 长回复时，1024 会截断（"折腾到"半句）
        max_tokens = max_tokens if 0 < max_tokens <= 8192 else 2048
    except (TypeError, ValueError):
        max_tokens = 2048

    # ---- 非流式（原前端的后台记忆抽取请求）：直接 JSON 返回，统一走 flash 模型 ----
    if not stream:
        try:
            # ★ 修复（2026-09-09）：非流式 model 被 pick_model 切到 MEMORY_EXTRACT_MODEL
            #   （glm-5.3-flash/智谱），但 key 还是前端注入的生成层 key（gemini 中转）
            #   → 智谱 401「令牌已过期」→ 后台记忆抽取全挂。key 必须跟着 model 重取。
            key = config.api_key_for_model(model) or key
            content = await chat_once(model, messages, key, base_url=base_url,
                                      temperature=temp, max_tokens=max_tokens)
        except ModelApiError as e:
            print(f"[AI-ERR] /api/chat(非流式) 模型错误: {e}", flush=True)
            # Starlette/FastAPI 使用 status_code；错误参数 status 会把原本的
            # 上游 401/403 二次变成 500，前端只能看到“服务器内部错误”。
            return JSONResponse({"error": {"message": str(e)}}, status_code=e.status)
        except Exception as e:
            print(f"[AI-ERR] /api/chat(非流式) 未知异常: {type(e).__name__}: {e}", flush=True)
            import traceback as _tb
            _tb.print_exc()
            return JSONResponse({"error": {"message": "无法连接模型服务：%s" % e}}, status_code=502)
        return {"id": "pc-backend", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}

    # ---- 流式聊天：prompt 增强 + 模型接管 + R1 思考内容丢弃 + 轮次计数 ----
    if not is_internal_generation:
        idle_mod.agent.on_user_activity(session)
    user_text = ""
    if not is_internal_generation:
        for m in reversed(messages):
            if m.get("role") == "user":
                if db.is_internal_chat_message("user", m.get("content")):
                    continue
                user_text = m.get("content") or ""
                break
    char_name = str(body.get("character_name") or "").strip() or None

    # ★ 身份询问：用户直接质疑「你是AI吗」→ 客服式坦诚承认（最高优先级，在 OOC 之前）
    try:
        from . import identity_answer
        if not is_internal_generation and identity_answer.is_identity_question(user_text):
            try:
                from . import offline as _offline
                _off_identity = _offline.resolve(
                    session, _cid,
                    enabled=body.get("offline_enabled"),
                    max_delay=body.get("offline_max_delay"),
                )
                if _off_identity["enabled"] and _off_identity["status"] not in {"online", "half_awake"}:
                    woke, wake_info = _offline.should_wake_from_user(session, _cid, user_text, kind="message")
                    if woke:
                        wake_ctx = _offline.build_wake_context(session, _cid, wake_info)
                        messages = [{"role": "system", "content": wake_ctx}] + list(messages)
                    else:
                        try:
                            db.add_message(session, "user", user_text, _cid)
                        except Exception:
                            pass
                        if _claim_offline_reply_once(
                            session, _cid, user_text, _off_identity["status"], _off_identity["delay_seconds"]
                        ):
                            get_loop().create_task(
                                _offline_reply_later(
                                    session, _cid, user_text, messages,
                                    str(request.base_url).rstrip("/"),
                                    _off_identity["delay_seconds"], _off_identity["status"],
                                )
                            )
                        return JSONResponse(
                            {"offline_pending": True, "status": _off_identity["status"],
                             "eta": _off_identity["delay_seconds"], "hint": _off_identity["hint"]},
                            headers={"X-Offline-Pending": "1", "Cache-Control": "no-cache"},
                        )
            except Exception as _off_identity_error:
                print(f"[IdentityAnswer] 离线门禁失败，继续身份回答: {_off_identity_error}", flush=True)
            _call_user = "你"
            _personality = ""
            try:
                from .character_manager import get_character
                _cfg = get_character(_cid) or {}
                _call_user = _cfg.get("call_user") or "你"
                _personality = _cfg.get("personality") or ""
            except Exception:
                pass
            idle_mod.agent.on_user_activity(session)
            _ans = await identity_answer.generate_answer(user_text, _call_user, char_name or _cid, _personality)
            if not _ans:
                _ans = identity_answer.fallback_answer(_call_user)
            try:
                db.add_message(session, "user", user_text, _cid)
                db.add_message(session, "assistant", _ans, _cid)
            except Exception:
                pass

            # ★ 记录重要对话 + 写小作文（后台，不阻塞回答）
            async def _identity_record_and_essay():
                # 1. 记录重要对话到长期记忆（高 importance）
                try:
                    from . import memory_brain
                    await memory_brain.smart_insert(
                        "用户曾质疑AI身份（问「" + user_text[:30] + "」），AI坦诚承认自己是AI，并表明会记住用户、认真陪伴",
                        memory_type="fact", importance=9,
                        session_id=session, character_id=_cid
                    )
                except Exception as _me1:
                    print(f"[IdentityAnswer] 记录记忆失败: {_me1}", flush=True)
                # 2. 写小作文 + 推送（存成信件，silent 不打扰）
                try:
                    from . import offline as _offline
                    _off_now = _offline.resolve(session, _cid)
                    if _off_now.get("enabled") and _off_now.get("status") == "sleeping":
                        return
                except Exception:
                    pass
                essay = await identity_answer.gen_essay(user_text, char_name or _cid)
                if essay:
                    # ★ 修复：身份小作文落库（离线也能补收），key 防重
                    try:
                        _lk = f"identity_letter:{session}:{_cid}"
                        if not db.kv_get(_lk):
                            db.kv_set(_lk, essay)
                    except Exception:
                        pass
                    try:
                        # 走统一信件投递，才能真正生成 CosyVoice 音频并同时归档文字。
                        _letter_scheduler = scheduler.scheduler.get_or_create(session, _cid)
                        await _letter_scheduler._deliver(essay, char_name or _cid, {
                            "template_category": "letter",
                            "silent": True,
                            "scheduled": True,
                            "voice_letter": False,
                            "letter_title": "坦诚的信",
                        })
                    except Exception as _me2:
                        print(f"[IdentityAnswer] 推送小作文失败: {_me2}", flush=True)
            get_loop().create_task(_identity_record_and_essay())

            async def ident_ans_gen():
                # ★ 彩蛋：BGM 一出就发文字（BGM 作为背景衬托）
                yield _sse_line({"_meta": True, "play_bgm": True})
                for i in range(0, len(_ans), 8):
                    yield _sse_line({"choices": [{"index": 0, "delta": {"content": _ans[i:i + 8]}, "finish_reason": None}]})
                yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield "data: [DONE]\n\n"
                yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
            return StreamingResponse(ident_ans_gen(), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as _ie:
        print(f"[IdentityAnswer] 失败: {_ie}", flush=True)

    # ★ 关系深度：边界拒绝（短路）+ 专属梗扫描
    try:
        from . import relationship_extras
        _refusal = None if is_internal_generation else relationship_extras.check_boundary(user_text, session, _cid)
        if _refusal:
            idle_mod.agent.on_user_activity(session)
            try:
                db.add_message(session, "user", user_text, _cid)
                db.add_message(session, "assistant", _refusal, _cid)
            except Exception:
                pass
            async def refusal_gen():
                for i in range(0, len(_refusal), 8):
                    yield _sse_line({"choices": [{"index": 0, "delta": {"content": _refusal[i:i + 8]}, "finish_reason": None}]})
                yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                yield "data: [DONE]\n\n"
                yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
            return StreamingResponse(refusal_gen(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        # 扫描专属梗
        if not is_internal_generation:
            relationship_extras.scan_anchor_message(user_text, session, _cid)
    except Exception as _re:
        print(f"[RelExtras] 失败: {_re}", flush=True)

    # ★ 语义闸门（两次 DeepSeek 第一层）：动作类规则短路之前，先判用户真实意图。
    #
    #   背景（真实案例）：用户说「一点点，昨天朋友叫我打三角洲」，被下面的
    #   parse_timed_command 误判成定时指令 ——
    #     ① chat_logic.py:683 的 has_timed_intent 正则命中「朋友[叫我]打三角洲」
    #        里的『叫我』（语义是"朋友叫我打游戏"，不是让 AI 提醒）
    #     ② chat_logic.py:678 的绝对时间正则把『一』当小时数、『点』当时间单位
    #        （其实是数量词"一点点"）
    #     ③ 两者叠加 → 建了个 473 分钟后的定时任务，并把回复短路成确认文案
    #
    #   根因：规则短路全部排在理解层之前（本处短路 → 后面 L1974 才是
    #   analyze_and_inject），理解层永远没机会纠正。而理解层要解决的恰恰就是
    #   这类「告知(inform) 被当成指令(command)」的偏差。
    #
    #   所以这里补一道前置闸门：只有第一层判定「确实要执行动作」时，才允许
    #   下面的动作类短路（定时建任务 / 语音偏好改配置）生效。
    #
    #   安全边界（宁可放行，不可误伤）：
    #     · 未启用 / 解析失败 / 抛异常 → 一律放行，保持原有行为，不新增卡死路径
    #     · 仅拦截 should_trigger_action 明确为 False 的情况
    #     · understand() 内部有 300s 缓存，与后面 analyze_and_inject 复用同一份结果，
    #       不会重复计费
    _allow_action = True
    # ★ 始终有定义：内部生成（主动消息/离线回复）时不走理解层，
    #   但后面的合规校验会读它，不初始化会 NameError 让整条请求 500。
    _intent = {}
    if not is_internal_generation:
        try:
            from .understanding import understand as _understand
            _intent = await _understand(
                user_text, messages,
                session_id=session, character_id=_cid,
                character_name=char_name or "",
                key=key,
                # ★ 理解层跟随当前对话模型（含人格设置的单角色大脑），
                #   不再固定走全局 chat —— 单角色换了强模型，理解层必须跟着换，
                #   否则语义判断一直用 deepseek-chat，拖累整体质量。
                model=model,
                is_internal=is_internal_generation,
            ) or {}
            # ★ Phase 2：闸门改由 routing.allow_action_shortcut 统一控制，
            #   而不是直接读 should_trigger_action。
            #   routing 是「给后续程序的路由决策」；归一化阶段已保证它与
            #   should_trigger_action 同真同假，后续往 routing 加新开关也不用改这里。
            #   没有 routing 字段（老数据 / 短路结果）时默认放行，不改变既有行为。
            _routing = (_intent.get("routing") or {}) if _intent else {}
            if _intent and not _routing.get("allow_action_shortcut", True):
                _allow_action = False
                print(f"[Understanding] 语义闸门拦截动作类短路 "
                      f"intent={_intent.get('intent')} sub={_intent.get('sub_type')} "
                      f"wants={_intent.get('user_wants')} text={user_text[:30]}", flush=True)
        except Exception as _se:
            print(f"[Understanding] 语义闸门失败(放行): {_se}", flush=True)
            _allow_action = True

    # ★ 用户引导：理解层判定用户在纠正 AI → 记下来，以后别再犯
    _record_correction(session, _cid, _intent, user_text)

    # ---- 定时提醒检测：模型主判 → 正则兜底（★ 2026-09-16 误触发修复）----
    #   修法见 UI改版方案/_定时提醒误触发-根因与修法-2026-09-16.md（A 方案）：
    #   判断"这是不是一项委托/约定"交给模型（角色自己有能力也有责任判断），
    #   正则只在她不可用时兜底；回复文案统一由 chat_logic.build_reminder_reply 渲染，
    #   抽不到明确事项时是反问句，不会再出现「我会提醒你「半吧」」这种残尾。
    timed = None if (is_internal_generation or not _allow_action) else await chat_logic.resolve_timed_reminder(
        user_text, char_name or "default", session_id=session, character_id=_cid
    )
    if timed:
        # ★ 2026-09-16：确认话术优先用**她自己的话**（模型生成、带人设），
        #   事实（分钟/时刻/事项）由 chat_logic 校验；模型不可用/说错 → 退回原模板。
        confirm = await chat_logic.build_reminder_reply_voiced(
            timed, "app", character_name=char_name or "default",
            character_id=_cid, session_id=session)
        async def timed_gen():
            yield _sse_line({"choices": [{"index": 0, "delta": {"content": confirm}, "finish_reason": None}]})
            yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
            yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
        return StreamingResponse(timed_gen(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 偏好指令检测："以后每天早安" / "不用晚安了" ----
    voice_night = None if is_internal_generation else chat_logic.parse_voice_night_command(user_text, session, _cid)
    if voice_night:
        if voice_night.get("send_now"):
            _night_scheduler = scheduler.scheduler.get_or_create(session, _cid)
            get_loop().create_task(
                _night_scheduler.send_requested_voice_night(char_name or _cid)
            )
        async def voice_night_gen():
            reply = voice_night["reply"]
            yield _sse_line({"choices": [{"index": 0, "delta": {"content": reply}, "finish_reason": None}]})
            yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
            yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
        return StreamingResponse(voice_night_gen(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 语音偏好指令检测："别发语音/发文字" 或 "以后发语音" ----
    # ★ 2026-09-14 修：不再受 _allow_action（动作短路闸门）管辖。
    #   闸门是为了防「会触发动作」的误判（如把「昨天朋友叫我打三角洲」当定时任务），
    #   而语音偏好只记录偏好 + 回一句话，零副作用、不可能误触发动作；
    #   挂在闸门后会导致用户说「不要发语音」被静默跳过、指令失效。
    _voice_pref = None if is_internal_generation else _parse_voice_pref(user_text, session, _cid)
    if _voice_pref:
        async def voice_pref_gen():
            yield _sse_line({"choices": [{"index": 0, "delta": {"content": _voice_pref}, "finish_reason": None}]})
            yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
            yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
        return StreamingResponse(voice_pref_gen(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 偏好指令检测："以后每天早安" / "不用晚安了" ----
    pref = None if is_internal_generation else chat_logic.parse_preference_command(user_text)
    if pref:
        async def pref_gen():
            yield _sse_line({"choices": [{"index": 0, "delta": {"content": pref["reply"]}, "finish_reason": None}]})
            yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
            yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
        return StreamingResponse(pref_gen(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- 身份指令检测："你18岁" / "你是画师" → 自动记录到角色档案 ----
    identity = None if is_internal_generation else chat_logic.parse_identity_command(
        user_text, char_name or "default", session_id=session, character_id=_cid,
        intent=_intent
    )
    if identity:
        # ★ 2026-09-16：「我记住了」也用她自己的话说（事实=新称呼等由 chat_logic 校验），
        #   模型不可用/说错 → 仍是原来那句模板（用户绝不会收不到回应）。
        identity["reply"] = await chat_logic.voice_identity_reply(
            identity, character_name=char_name or "default",
            character_id=_cid, session_id=session)

        async def ident_gen():
            yield _sse_line({"choices": [{"index": 0, "delta": {"content": identity["reply"]}, "finish_reason": None}]})
            yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"
            yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
        return StreamingResponse(ident_gen(), media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ★ 离线状态系统：角色不在时延迟回复（前端人格设置开关 > 全局 config）
    try:
        from . import offline as _offline
        _off = _offline.resolve(
            session, _cid,
            enabled=body.get("offline_enabled"),
            max_delay=body.get("offline_max_delay"),
        )
        if (not is_internal_generation) and _off["enabled"] and _off["status"] not in {"online", "half_awake"}:
            woke, wake_info = _offline.should_wake_from_user(session, _cid, user_text, kind="message")
            if woke:
                wake_ctx = _offline.build_wake_context(session, _cid, wake_info)
                messages = [{"role": "system", "content": wake_ctx}] + list(messages)
            else:
                # 用户消息立即落库（延迟回复不走 chat_gen 的落库）
                try:
                    db.add_message(session, "user", user_text, _cid)
                except Exception:
                    pass
                if _claim_offline_reply_once(session, _cid, user_text, _off["status"], _off["delay_seconds"]):
                    get_loop().create_task(
                        _offline_reply_later(
                            session, _cid, user_text, messages,
                            str(request.base_url).rstrip("/"),
                            _off["delay_seconds"], _off["status"],
                        )
                    )
                return JSONResponse(
                    {"offline_pending": True, "status": _off["status"],
                     "eta": _off["delay_seconds"], "hint": _off["hint"]},
                    headers={"X-Offline-Pending": "1", "Cache-Control": "no-cache"},
                )
    except Exception as _oe:
        print(f"[Offline] 检测失败，降级秒回: {_oe}", flush=True)

    # ★ AI 隐藏情绪：用户问「你怎么了」→ 直接解释（憋着的情绪说出来）
    try:
        from . import ai_mood
        # 先根据用户消息触发情绪事件（憋着）
        if not is_internal_generation:
            ai_mood.detect_user_message(user_text, session, _cid)
        if (not is_internal_generation) and ai_mood.is_asking(user_text):
            _reveal = await ai_mood.reveal(session, _cid, char_name or "")
            if _reveal:
                idle_mod.agent.on_user_activity(session)
                try:
                    db.add_message(session, "user", user_text, _cid)
                    db.add_message(session, "assistant", _reveal, _cid)
                except Exception:
                    pass
                async def reveal_gen():
                    for i in range(0, len(_reveal), 8):
                        yield _sse_line({"choices": [{"index": 0, "delta": {"content": _reveal[i:i + 8]}, "finish_reason": None}]})
                    yield _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    yield "data: [DONE]\n\n"
                    yield _sse_line({"_meta": True, "sawDone": True, "sawAnyData": True, "finish_reason": "stop"})
                return StreamingResponse(reveal_gen(), media_type="text/event-stream",
                                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as _me:
        print(f"[AiMood] 检测失败: {_me}", flush=True)

    # ★ 动作描写开关（前端可关）：解析 action_brackets，构造请求级 style 覆盖
    _ab = body.get("action_brackets")   # 前端传 true/false，不传则 None（走默认/角色json）
    _style_ov = {"action_brackets": _ab} if isinstance(_ab, bool) else None
    # ★ 表情包开关：前端传 true 时，enrich_messages 注入表情包列表
    _sticker = bool(body.get("sticker"))

    enriched = await chat_logic.enrich_messages(
        messages, session, _cid,
        style_override=_style_ov, sticker=_sticker,
        internal_generation=is_internal_generation,
    )

    # ★ 语义理解层（「两次 DeepSeek」的第一层）
    #   先把用户真实意图解析成结构化结果，再追加到 system prompt，
    #   让生成层直接消费、不再二次猜测（解决"告知被当成指令"这类理解偏差）。
    #   未开启/解析失败时原样返回，主流程完全不受影响。
    try:
        from .understanding import analyze_and_inject
        enriched = await analyze_and_inject(
            enriched, user_text,
            session_id=session, character_id=_cid,
            character_name=char_name or "",
            key=key, model=model, base_url=base_url,
            is_internal=is_internal_generation,
        )
    except Exception as _ue:
        print(f"[Understanding] /api/chat 注入失败(静默): {_ue}", flush=True)

    # ★ Agent 助手模式：理解层判定为「任务型指令」时，走 Agent 循环（调工具干活），
    #   而不是普通聊天。任何异常都回退普通聊天，绝不影响主流程。
    try:
        from .agent.mode import detect_agent_task
        _is_agent_task = (not is_internal_generation) and detect_agent_task(_intent, user_text)
    except Exception:
        _is_agent_task = False

    if _is_agent_task:
        async def agent_gen():
            import asyncio as _aio
            _q = _aio.Queue()

            async def _on_event(ev):
                await _q.put(ev)

            from .agent.loop import run_agent_task
            _agent_task = _aio.create_task(run_agent_task(
                user_text,
                session_id=session, character_id=_cid,
                character_name=char_name or "",
                key=key, model="", base_url=base_url,
                on_event=_on_event,
            ))
            try:
                db.add_message(session, "user", user_text, _cid)
            except Exception:
                pass
            final_answer = ""
            while True:
                if _agent_task.done() and _q.empty():
                    break
                try:
                    _ev = await _aio.wait_for(_q.get(), timeout=0.2)
                except _aio.TimeoutError:
                    continue
                yield _sse_line(_ev)
                if _ev.get("type") == "agent_final":
                    final_answer = _ev.get("answer", "")
            try:
                _ret = await _agent_task
                if _ret:
                    final_answer = _ret
            except Exception:
                pass
            if final_answer:
                try:
                    db.add_message(session, "assistant", final_answer, _cid)
                except Exception:
                    pass
            yield _sse_line({"type": "done"})
        return StreamingResponse(agent_gen(), media_type="text/event-stream",
                                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    try:
        from . import offline as _offline
        _awake = _offline.get_awake_state(session, _cid)
        if _awake:
            _wake_ctx = _offline.build_wake_context(session, _cid, {"recent_attempts": []})
            for m in enriched:
                if m.get("role") == "system":
                    m["content"] = (m.get("content") or "") + "\n\n" + _wake_ctx
                    break
            else:
                enriched.insert(0, {"role": "system", "content": _wake_ctx})
    except Exception:
        pass

    # ★ 与 /api/chat/stream 共用的 system prompt 增强块（见 _inject_shared_context）
    #   注意：本入口的角色变量是 _cid（不是 character_id），写错会 NameError 导致整条请求 500。
    await _inject_shared_context(enriched, session, _cid, user_text)

    async def chat_gen():
        assistant_text = ""
        meta_out = {"_meta": True, "sawDone": False, "sawAnyData": False, "finish_reason": None}
        try:
            # ★ GLM-5.3 系列思考由模型默认开启（自适应，实测基线 5/5 触发）。
            #   实测（2026-09-07）：给 glm-5.3-flash 传 reasoning_effort=high 反而把
            #   思考触发率压到 2/5 —— 智谱兼容层对该参数的处理不利于「先想再答」，
            #   故聊天层不传该参数（见 config.model_supports_reasoning_effort 注释）。
            #   思考内容由 _clean_delta_line 分离，前端 show_thinking 开关控制展示。
            async for kind, val in stream_chat(model, enriched, key, base_url=base_url,
                                               temperature=temp, top_p=top_p,
                                               max_tokens=max_tokens):
                if kind == "line":
                    yield val + "\n\n"
                    try:
                        delta = json.loads(val[5:])
                        c = delta["choices"][0]["delta"].get("content")
                        if c:
                            assistant_text += c
                    except Exception:
                        pass
                elif kind == "done":
                    meta_out["sawDone"] = True
                    yield "data: [DONE]\n\n"
                elif kind == "meta":
                    meta_out.update(val)
            # 普通文字聊天只显示文字。用户明确索要语音时由 /api/voice/message
            # 生成可点击的微信式语音条，禁止普通回复经 WS 到达后自动朗读。
            yield _sse_line(meta_out)
        except ModelApiError as e:
            print(f"[AI-ERR] /api/chat 模型错误: {e}", flush=True)
            yield _sse_line({"error": {"message": str(e)}})
            return
        except Exception as e:
            print(f"[AI-ERR] /api/chat 未知异常: {type(e).__name__}: {e}", flush=True)
            import traceback as _tb
            _tb.print_exc()
            yield _sse_line({"error": {"message": "无法连接模型服务：%s" % e}})
            return
        # ---- 回复完成后：token 统计 + 轮次计数 + 触发自动记忆提炼（后台静默） ----
        if assistant_text.strip():
            # ★ 2026-09-11：前端主动消息同步 QQ。App 内 proactiveTick 产生的主动消息
            #   原来只显示在 App，QQ 侧永远看不到（后端 idle/scheduler 的主动消息有推 QQ，
            #   前端链路没有）。前端在 body 带 qq_sync: true 时，回复完成后逐条推 QQ。
            if body.get("qq_sync"):
                try:
                    from . import onebot as _ob
                    _qq_lines = [x.strip() for x in assistant_text.split("\n") if x.strip()]
                    for _qi, _ql in enumerate(_qq_lines):
                        try:
                            await _ob.send_qq_message(_ql)
                        except Exception as _qle:
                            print(f"[Proactive] 推QQ第{_qi + 1}条失败(静默): {_qle}", flush=True)
                        if _qi < len(_qq_lines) - 1:
                            await asyncio.sleep(0.4)
                except Exception as _qq_err:
                    print(f"[Proactive] 同步QQ失败(静默): {_qq_err}", flush=True)
            # 人格漂移检测：检查是否包含禁止表达
            try:
                personality = db.get_personality_state(session, _cid)
                if not personality_manager.personality_guard(assistant_text, personality):
                    print(f"[personality_guard] 检测到禁止表达，session={session}", flush=True)
            except Exception:
                pass
            # ★ Phase 3 闭环：校验本轮回复是否遵守理解层给出的执行清单并落库。
            #   理解层没参与时（_intent 为空，如内部生成）直接跳过——没判过就无从校验。
            #   只记录不改写：当前阶段先看清违规分布，再决定要不要拦。
            try:
                from .understanding import check_response_compliance, log_compliance
                _comp = check_response_compliance(assistant_text, _intent)
                log_compliance(session, _cid, _intent, assistant_text, _comp)
                if _intent and not _comp.get("ok"):
                    print(f"[Compliance] 回复未遵守执行清单 intent={_intent.get('intent')} "
                          f"wants={_intent.get('user_wants')} "
                          f"violations={_comp.get('violations')}", flush=True)
            except Exception:
                pass
            # ★ 2026-09-14：这里的记账已移除 —— 记账统一由 deepseek_api 在
            #   **每次真实 LLM 调用之后**完成（chat_once / stream_chat 出口），
            #   并用上游返回的真实 usage 替代按字数的估算。
            #   保留此处会导致同一次调用被记两遍（此处按整轮估算 + 底层按真实 usage）。
            try:
                should_extract = False if is_internal_generation else chat_logic.count_round(session, user_text, assistant_text, _cid)
                if should_extract:
                    # ★ 修复：不再在 chat_gen 内部重新赋值 character_id
                    #   统一用闭包捕获的 _cid（api_chat 入口解析），避免
                    #   chat_gen 内 character_id 变局部变量导致上方 TTS 引用 UnboundLocalError
                    get_loop().create_task(
                        chat_logic.maybe_auto_extract(session, _cid, user_text))
            except Exception:
                pass

            # YunLink Memory System v1.0：后台静默保存记忆
            #   ★ 2026-09-14：整段改为 LEGACY_MEMORY_PIPELINE 开关控制（默认关闭）。
            #   原因：它与主线 chat_logic.maybe_auto_extract 写**同一张 long_term_memory 表**，
            #   但用旧 type 口径、不带 context/emotion_tag/source_text、且**没有冲突检测**；
            #   而且这里在文本路径上**每轮无条件**跑一次 LLM（主线是每 AUTO_MEMORY_INTERVAL
            #   轮一次）→ 纯重复劳动，也是长期记忆出现近似重复簇的来源之一。
            #   注意：本处只关"文字对话"的重复保存；图片记忆（下方 _img_mem_a /
            #   _save_image_desc_memory）、音色克隆事件记忆、以及 stream 端出口B 这些
            #   携带**独有信息**的写入一律保留，不受本开关影响。
            #   回退：把 config.json 里 LEGACY_MEMORY_PIPELINE 设为 true。
            try:
                # 构建聊天文本
                if (not is_internal_generation) and config.get("LEGACY_MEMORY_PIPELINE", False):
                    chat_text = f"用户：{user_text}\nAI：{assistant_text}"
                    character_id = _cid

                    # 创建简单的 LLM 适配器
                    class _SimpleLLM:
                        async def chat(self, prompt):
                            from .deepseek_api import chat_once
                            # ★ 2026-09-11：记忆回流 → 角色卡 memory_model（原借生成层模型）
                            return await chat_once(
                                config.memory_extract_model(character_id),
                                [{"role": "user", "content": prompt}],
                                config.memory_key() or key,
                                temperature=0.2,
                                max_tokens=800
                            )

                    llm = _SimpleLLM()
                    get_loop().create_task(
                        yunlink_memory.save_from_chat(
                            user_id=session,
                            llm=llm,
                            chat=chat_text,
                            character_id=character_id
                        )
                    )
            except Exception as e:
                print(f"[YunLink Memory] 保存记忆失败: {e}", flush=True)

            # ★ 新增：普通文本路由也持久化消息，供跨端 /session/history 拉取
            try:
                if not is_internal_generation:
                    db.add_message(session, "user", user_text, _cid)
                    db.add_message(session, "assistant", assistant_text, _cid)
                    # ★ AI 承诺识别：异步提取「八点半我喊你」这类承诺，到点优先兑现
                    #   ★ 改 asyncio.create_task：get_loop() 在部分上下文返回的循环
                    #     与当前不匹配时 create_task 抛 RuntimeError，被 except pass
                    #     无痕吞掉——承诺提取静默丢失且查无日志。
                    try:
                        from . import ai_promise
                        asyncio.create_task(ai_promise.extract_and_store(
                            session, _cid, assistant_text))
                    except Exception as _ape:
                        print(f"[Chat] 承诺提取调度失败: {_ape}", flush=True)
                    # ★ QQ 打通：App 回复同步推送到 QQ 大号（同一段对话）
                    try:
                        from . import onebot
                        await onebot.send_qq_message(assistant_text)
                    except Exception as _qq_e:
                        print(f"[Chat] 推QQ失败(静默): {_qq_e}", flush=True)
            except Exception as e:
                print(f"[Session] 存聊天记录失败: {e}", flush=True)

            # YunLink Relationship Engine v1.0：后台静默更新关系状态
            try:
                if not is_internal_generation:
                    get_loop().create_task(
                        yunlink_relationship.update_relationship(
                            user_id=session,
                            message=user_text,
                            character_id=_cid
                        )
                    )
            except Exception as e:
                print(f"[YunLink Relationship] 更新关系状态失败: {e}", flush=True)

            # ★ 多轮对话优化：更新对话状态（规则版）+ 关系升级检测（阶段变化→仪式，下条注入）
            try:
                from . import conv_state
                if not is_internal_generation:
                    conv_state.update_conv_state(session, _cid, user_text, assistant_text)
            except Exception as _ce:
                print(f"[ConvState] 状态更新失败(静默): {_ce}", flush=True)
            try:
                from .relationship.ritual import check_upgrade_after_chat
                _up = None if is_internal_generation else check_upgrade_after_chat(session, _cid)
                if _up:
                    print(f"[Ritual] 关系升级: {_up['from']} → {_up['to']}", flush=True)
            except Exception as _re:
                print(f"[Ritual] 升级检测失败(静默): {_re}", flush=True)

            # AI Self State System v1.0：聊天完成后更新 AI 自身状态
            try:
                from .companion.ai_state.updater import update_after_chat
                if not is_internal_generation:
                    character_id = _cid
                    relationship_state = db.get_relationship_state(session, character_id) or {}
                    # ★ update_after_chat 内部 run_coroutine_threadsafe().result() 会阻塞当前线程，
                    #   必须丢线程池，否则主事件循环被卡死最长 10s
                    await asyncio.to_thread(
                        update_after_chat,
                        session_id=session,
                        character_id=character_id,
                        user_message=user_text,
                        relationship=relationship_state
                    )
            except Exception as e:
                print(f"[AI Self State] 更新AI状态失败: {e}", flush=True)

            # Adaptive Personality Evolution v1.0 + Consistency Controller：聊天完成后更新人格状态
            try:
                from .personality.evolution import calculate_growth, should_evolve
                from .personality.manager import PersonalityManager
                if not is_internal_generation:
                    character_id = _cid
                    relationship_state = db.get_relationship_state(session, character_id) or {}
                    behavior_state = db.get_behavior_state(session, character_id) or {}

                    # 判断是否应该进行人格进化（概率触发，避免每次都变）
                    if should_evolve(relationship_state):
                        # 使用新的 calculate_growth（delta 字段）
                        growth = calculate_growth(
                            relationship_state,
                            behavior=behavior_state
                        )
                        if growth:
                            PersonalityManager().update(
                                session,
                                character_id,
                                growth
                            )
                            print(f"[PersonalityEvolution] 人格成长: {growth}", flush=True)
                    # 互动证据成长：小步、可解释、按 session+character 隔离
                    try:
                        from .personality.manager import PersonalityManager as _PM
                        _PM().observe_interaction(session, character_id, user_text)
                    except Exception as _oe:
                        print(f"[PersonalityGrowth] 互动证据记录失败(静默): {_oe}", flush=True)
            except Exception as e:
                print(f"[PersonalityEvolution] 人格更新失败: {e}", flush=True)

            # ★ 新增：后台静默反思 + 联动人格漂移（按角色隔离，绝不串台）
            try:
                character_id = _cid

                async def _reflect_and_drift(sid, cid):
                    # 1) 先存反思摘要（原逻辑：LLM 读记忆/时间线/KG 生成 user_understanding 等）
                    await reflection.update_reflection(sid, cid)
                    # 2) 再把反思映射到 personality_state（之前没接的管子，现在接通）
                    try:
                        reflection.apply_reflection_to_personality_state(sid, cid)
                    except Exception as e2:
                        print(f"[Reflection] 人格漂移映射失败(可忽略): {e2}", flush=True)

                get_loop().create_task(
                    _reflect_and_drift(session, character_id)
                )
            except Exception as e:
                print(f"[Reflection] 触发反思失败: {e}", flush=True)

    return StreamingResponse(chat_gen(), media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- 原接口：磁盘记忆库 /api/library* ----------------

_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fa5.\- ]")


def safe_lib_name(name) -> str:
    n = str(name or "").strip().lstrip(".")
    n = _SAFE_NAME_RE.sub("_", n.replace("\\", "_").replace("/", "_"))
    if not re.search(r"\.(txt|md)$", n, re.I):
        n += ".txt"
    return n


@app.get("/api/library")
async def library_list(character: str = ""):
    files = []
    # 已知角色名（用于判断文件是否"角色专属"）
    known_chars = set()
    try:
        from . import character_manager
        for c in character_manager.list_characters():
            if c.get("name"):
                known_chars.add(str(c["name"]))
    except Exception:
        pass
    try:
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        merged = {}
        for base in (getattr(config, "RESOURCE_LIB_DIR", LIB_DIR), LIB_DIR):
            if base.exists():
                for item in base.iterdir():
                    merged[item.name] = item
        for f in sorted(merged.values(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() not in (".txt", ".md"):
                continue
            stem = f.stem
            # 判断文件归属的角色（文件名含角色名 → 该角色专属）
            owner = None
            for c in known_chars:
                if c and c in stem:
                    owner = c
                    break
            # ★ 按角色隔离：有主角色 → 只给对应角色；无主角色 → 通用（所有角色可见）
            if owner and owner != character:
                continue
            st = f.stat()
            files.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    except OSError:
        pass
    return {"dir": str(LIB_DIR), "files": files}


@app.get("/api/library/read")
async def library_read(name: str):
    fp = (LIB_DIR / safe_lib_name(name)).resolve()
    if not str(fp).startswith(str(LIB_DIR.resolve())):
        return JSONResponse({"error": {"message": "Forbidden"}}, status_code=403)
    if not fp.exists():
        resource_root = getattr(config, "RESOURCE_LIB_DIR", LIB_DIR).resolve()
        fallback = (resource_root / safe_lib_name(name)).resolve()
        if str(fallback).startswith(str(resource_root)) and fallback.exists():
            fp = fallback
    try:
        return {"name": fp.name, "content": fp.read_text("utf-8")[:200000]}
    except OSError:
        return JSONResponse({"error": {"message": "文件不存在"}}, status_code=404)


async def _lib_write(request: Request, action: str):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    fp = (LIB_DIR / safe_lib_name(body.get("name") or "")).resolve()
    if not str(fp).startswith(str(LIB_DIR.resolve())):
        return JSONResponse({"error": {"message": "Forbidden"}}, status_code=403)
    if action == "delete":
        try:
            fp.unlink()
            return {"ok": True}
        except OSError:
            return JSONResponse({"error": {"message": "文件不存在"}}, status_code=404)
    content = str(body.get("content") or "")
    if len(content) > 500 * 1024:
        return JSONResponse({"error": {"message": "内容过大（限 500KB）"}}, status_code=400)
    try:
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, "utf-8")
        return {"ok": True, "name": fp.name}
    except OSError as e:
        return JSONResponse({"error": {"message": "写入失败：%s" % e}}, status_code=500)


@app.post("/api/library/write")
async def library_write(request: Request):
    return await _lib_write(request, "write")


@app.post("/api/library/delete")
async def library_delete(request: Request):
    return await _lib_write(request, "delete")


# ---------------- 新增：PC 增强配置接口 ----------------

def pc_config_payload(character_id: str = "") -> dict:
    """PC 增强配置载荷。

    ★ 2026-09-14：新增 character_id 参数 —— 主动消息时段是**角色级**配置
      （角色卡 active_hours），要算准就得知道是哪个角色。
      /api/pc/config?character_id=助手 即可拿到该角色的实际时段；不传则给全局默认。
    """
    cfg = config.get_all()
    _pc_cfg_character_id = str(character_id or "").strip()

    return {
        "CURRENT_CHAT_MODEL":
            cfg.get("CURRENT_CHAT_MODEL", "deepseek-chat"),

        "SELECTED_MODEL":
            cfg.get(
                "SELECTED_MODEL",
                cfg.get("CURRENT_CHAT_MODEL", "deepseek-chat")
            ),

        "MEMORY_EXTRACT_MODEL":
            cfg.get("MEMORY_EXTRACT_MODEL", "deepseek-chat"),

        # ★ 理解层模型（留空 = 跟随主脑，前端可独立切换）
        "UNDERSTANDING_MODEL":
            cfg.get("UNDERSTANDING_MODEL", ""),

        # ★ 深度思考模型（角色卡 deep_thinking 开启时用；留空 = 旧的 deepseek-reasoner）
        #   2026-09-11：原来写死 deepseek-reasoner，现可切，默认 deepseek-flash(V4.1)
        "DEEP_THINKING_MODEL":
            cfg.get("DEEP_THINKING_MODEL", "deepseek-flash"),

        # ★ 本地大脑（Ollama）配置（2026-09-11）：人格页开关读角色卡，这里是全局配置
        "LOCAL_BRAIN": config.local_brain(),

        "AUTO_MEMORY_INTERVAL":
            cfg.get("AUTO_MEMORY_INTERVAL", 4),

        "IDLE_TRIGGER_MIN_MINUTES":
            cfg.get("IDLE_TRIGGER_MIN_MINUTES", 20),

        "IDLE_TRIGGER_MAX_MINUTES":
            cfg.get("IDLE_TRIGGER_MAX_MINUTES", 40),

        "IDLE_AGENT_ENABLED":
            cfg.get("IDLE_AGENT_ENABLED", True),

        # ★ 2026-09-15：主动消息单一引擎参数（前端要读：设置页显示状态、判断是否还自己生成）
        "PROACTIVE_ENGINE_ENABLED":
            cfg.get("PROACTIVE_ENGINE_ENABLED", True),
        "PROACTIVE_FRONTEND_GENERATION":
            cfg.get("PROACTIVE_FRONTEND_GENERATION", False),
        "PROACTIVE_QUIET_WINDOW_SEC":
            cfg.get("PROACTIVE_QUIET_WINDOW_SEC", 300),
        "PROACTIVE_DAILY_CAP":
            cfg.get("PROACTIVE_DAILY_CAP", 8),
        "PROACTIVE_CAP_EXCLUDE_EXEMPT":
            cfg.get("PROACTIVE_CAP_EXCLUDE_EXEMPT", True),
        "PROACTIVE_SMALL_CTX":
            cfg.get("PROACTIVE_SMALL_CTX", True),

        # ★ 2026-09-15：亲密度/关系自动成长开关 + 关系界面开关（前端据此隐藏数值面板）
        "RELATIONSHIP_AUTO_UPDATE":
            cfg.get("RELATIONSHIP_AUTO_UPDATE", False),
        "RELATIONSHIP_UI_VISIBLE":
            cfg.get("RELATIONSHIP_UI_VISIBLE", False),

        # ★ 2026-09-15 省 token：反思频率 + 语义分析模式（前端/工具可读可改）
        "REFLECTION_MODE":
            cfg.get("REFLECTION_MODE", "daily"),
        "REFLECTION_SLOT_HOUR":
            cfg.get("REFLECTION_SLOT_HOUR", 5),
        "SEMANTIC_ANALYZER_MODE":
            cfg.get("SEMANTIC_ANALYZER_MODE", "local"),

        # ★ 理解层开关（关闭后跳过场景/语义识别，主脑直连回应）
        "UNDERSTANDING_ENABLED":
            cfg.get("UNDERSTANDING_ENABLED", True),

        # ★ 提示词压缩模式（2026-09-14）：规则一条不减，只删同义强调与重复表述。
        #   实测每轮恒定注入 1925 → 1311 字（−32%，约省 921 token/轮）。
        "PROMPT_COMPACT":
            cfg.get("PROMPT_COMPACT", False),

        # ★ 旧线记忆管线开关（2026-09-14，默认关闭：与主线写同一张表且无冲突检测）
        "LEGACY_MEMORY_PIPELINE":
            cfg.get("LEGACY_MEMORY_PIPELINE", False),

        # ★ 自主程度（保守/平衡/自主）
        "AUTONOMY_LEVEL":
            cfg.get("AUTONOMY_LEVEL", "balanced"),

        # ★ 2026-09-14：主动消息时段**不再读全局配置**。
        #   原先这里是 `cfg.get("IDLE_AGENT_TIME_RANGE")`（全局），而人格设置页里
        #   另有一套角色级时段 —— 两套互不知情，且角色级那套因字段没进保存白名单
        #   根本存不进去 → "时间范围限制不管用"。
        #   现在统一：后端用 config.resolve_active_hours(角色)（以角色卡为唯一权威）。
        #   下发两个键：
        #     · ACTIVE_HOURS_DEFAULT —— 全局默认（角色没配时用的），供前端兜底展示
        #     · ROLE_ACTIVE_HOURS    —— 带上请求方角色时的解析结果（由 /api/pc/config?character_id= 传入）
        "ACTIVE_HOURS_DEFAULT":
            config.DEFAULT_ACTIVE_HOURS,
        "ROLE_ACTIVE_HOURS":
            config.resolve_active_hours(_pc_cfg_character_id),

        # ★ 早安/晚安/惊喜 配置暴露给前端
        "MORNING_ENABLED": cfg.get("MORNING_ENABLED", True),
        "MORNING_START":   cfg.get("MORNING_START",   "07:00"),
        "MORNING_END":     cfg.get("MORNING_END",     "09:30"),
        "NIGHT_ENABLED":   cfg.get("NIGHT_ENABLED",   True),
        "NIGHT_START":     cfg.get("NIGHT_START",     "22:00"),
        "NIGHT_END":       cfg.get("NIGHT_END",       "23:30"),
        "SURPRISE_ENABLED":cfg.get("SURPRISE_ENABLED",True),
        "MOMENT_FREQUENCY": cfg.get("MOMENT_FREQUENCY", 2),
        "AI_LEAD_DAY_ENABLED": cfg.get("AI_LEAD_DAY_ENABLED", True),
        "AI_LEAD_DAY_WEEKDAY": cfg.get("AI_LEAD_DAY_WEEKDAY", 6),

        # ★ 免打扰配置暴露给前端
        "DND_ENABLED": cfg.get("DND_ENABLED", True),
        "DND_START":   cfg.get("DND_START",   "23:00"),
        "DND_END":     cfg.get("DND_END",     "07:00"),
        "DND_ALLOW_REMINDERS": cfg.get("DND_ALLOW_REMINDERS", True),
        "DND_ALLOW_GREETINGS": cfg.get("DND_ALLOW_GREETINGS", True),
        "DND_ALLOW_CALLS": cfg.get("DND_ALLOW_CALLS", False),

        # ★ 感知系统配置暴露给前端（总开关 + 截图分析 + 观察间隔 + 记忆写入）
        "AWARENESS_ENABLED":         cfg.get("AWARENESS_ENABLED", False),
        "SCREEN_PERCEPTION_ENABLED": cfg.get("SCREEN_PERCEPTION_ENABLED", False),
        "SCREEN_INTERVAL":           cfg.get("SCREEN_INTERVAL", 300),
        "SCREEN_MEMORY_ENABLED":     cfg.get("SCREEN_MEMORY_ENABLED", True),
        # ★ 陪伴模式主动间隔（idle_agent 陪伴档范围，settings.js 屏幕感知区可调）
        "COMPANION_IDLE_MIN_MINUTES": cfg.get("COMPANION_IDLE_MIN_MINUTES", 4),
        "COMPANION_IDLE_MAX_MINUTES": cfg.get("COMPANION_IDLE_MAX_MINUTES", 8),
        # 陪伴模式屏幕评论最小间隔（秒）——companion.js 面板按分钟展示
        "COMPANION_SCREEN_COMMENT_COOLDOWN": cfg.get("COMPANION_SCREEN_COMMENT_COOLDOWN", 300),

        # ★ AI 监督吃醋系统（2026-09-11）：开关 + 主要参数暴露给设置页
        "JEALOUSY_ENABLED":              cfg.get("JEALOUSY_ENABLED", False),
        "JEALOUSY_TRIGGER":              cfg.get("JEALOUSY_TRIGGER", 60),
        "JEALOUSY_MESSAGE_COOLDOWN":     cfg.get("JEALOUSY_MESSAGE_COOLDOWN", 1800),
        "JEALOUSY_USAGE_WINDOW_MINUTES": cfg.get("JEALOUSY_USAGE_WINDOW_MINUTES", 60),

        # ★ 关系升温速度（慢热/正常/快热）
        "RELATIONSHIP_PACE": cfg.get("RELATIONSHIP_PACE", "normal"),

        # 文本模型
        "textModels":
            config.text_models(),

        # 视觉模型
        "visionModels":
            config.vision_models(),

        "VISION_PROVIDER":
            config.vision_provider(),

        "VISION_MODEL":
            config.vision_model(),

        "VISION_BASE_URL":
            config.vision_base_url(),

        "vision_key_set":
            bool(config.vision_key()),

        "api_key_set":
            bool(config.api_key()),

        # ★ TTS 语音合成配置暴露给前端（Key 本身不回显，只给"是否已配置"标记）
        "tts_provider": config.tts_provider(),
        "tts_minimax_set": bool(config.minimax_api_key() and config.minimax_group_id()),
        "tts_volcengine_set": bool(config.volcengine_app_id() and config.volcengine_access_token()),
        "tts_xunfei_set": bool(config.xunfei_app_id() and config.xunfei_api_key() and config.xunfei_api_secret()),
        "tts_azure_set": bool(config.azure_speech_key() and config.azure_speech_region()),
        "tts_elevenlabs_set": bool(config.elevenlabs_api_key()),

        # ★ 阿里云百炼（语音通话付费链路：ASR + TTS 共用一个 Key）
        "tts_aliyun_set": bool(config.dashscope_api_key()),
        # 通话专用的输入/输出 provider（独立于聊天，默认走云端付费）
        "call_asr_provider": config.call_asr_provider(),
        "call_tts_provider": config.call_tts_provider(),
        "aliyun_tts_model":  config.aliyun_tts_model(),
        "aliyun_asr_model":  config.aliyun_asr_model(),
        # 云端声音复刻音色（复刻后通话就用它的声音）
        "voice_replica_set":  bool(config.voice_replica_voice_id()),
        "voice_replica_voice_id": config.voice_replica_voice_id(),

        "memory_count":
            db.count_memories(),

        "presets":
            config.MODEL_PRESETS,

        # ★ QQ 机器人绑定角色（OneBot 复用该角色的人格+大脑，空=default 跟随全局）
        "QQ_CHARACTER": cfg.get("QQ_CHARACTER", ""),
        "QQ_ALLOWED_USERS": cfg.get("QQ_ALLOWED_USERS", []),
        "QQ_SESSION_ID": cfg.get("QQ_SESSION_ID", ""),
    }


@app.get("/api/pc/config")
async def pc_get_config(character_id: str = ""):
    """读取 PC 配置。传 character_id 可让 ROLE_ACTIVE_HOURS 按该角色解析。"""
    return pc_config_payload(character_id)


@app.get("/api/pc/llm_guard")
async def pc_llm_guard(character_id: str = ""):
    """LLM 故障隔离状态（只读诊断，2026-09-15 DeepSeek 宕机事故配套）。

    返回：各 provider 的熔断状态 + 当前生效的超时档位 + 本次回复链会用的模型与兜底。
    用途：出故障时一眼看出"是哪个 provider 挂了、熔断开没开、兜底会切到谁"，
    也是打包后验证新版本真的生效的行为探针（旧版本没有这个端点）。
    """
    try:
        from . import llm_guard as _lg
        from . import chat_logic as _cl
        cid = str(character_id or "").strip() or _lg.active_character_id()
        brain = _lg.brain_model_for(cid)
        dtm = ""
        try:
            from . import config as _cfg
            dtm = _cfg.deep_thinking_model(brain) if brain else ""
        except Exception:
            pass
        reply_model = ""
        try:
            reply_model = _cl.pick_model(None, True, cid) if cid else ""
        except Exception:
            pass
        return {
            "ok": True,
            "provider_state": _lg.snapshot(),
            "timeouts": {
                "aux": _lg.aux_timeout_sec(),
                "long": _lg.long_timeout_sec(),
                "stream_first": _lg.stream_first_timeout_sec(),
                "stream_stall": _lg.stream_stall_timeout_sec(),
            },
            "breaker": {"fails": _lg.breaker_fails(), "cooldown_sec": _lg.breaker_cooldown_sec()},
            "active_character": cid,
            "brain_model": brain,
            "deep_thinking_model": dtm,
            "reply_model": reply_model,
            "reply_fallback": _lg.fallback_model_for(reply_model) if reply_model else "",
        }
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


# ---------------- 陪伴模式（companion Tab） ----------------

@app.get("/api/companion/mode")
async def companion_mode_get(session_id: str = "default", character_id: str = "default"):
    """获取当前陪伴模式（游戏/生活陪伴）。"""
    try:
        character_id = _resolve_char_id(character_id, character_id)
        raw = db.kv_get(f"companion_mode:{session_id}:{character_id}")
        if not raw:  # 兼容旧版只按 session 保存的模式
            raw = db.kv_get(f"companion_mode:{session_id}")
        try:
            obj = json.loads(raw) if raw else {}
        except Exception:
            obj = {"mode": raw or "", "label": ""}
        return {"mode": obj.get("mode", ""), "label": obj.get("label", "")}
    except Exception:
        return {"mode": "", "label": ""}


@app.post("/api/companion/mode")
async def companion_mode_set(request: Request):
    """设置陪伴模式（如 minecraft/douyin/drama…），存 kv 供 AI 注入参考。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
    # 此接口的 character_id 已是前端联系人名；无 character_name 时也必须保留，
    # 不能被 _resolve_char_id 当成未知 UUID 丢回 default。
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name") or body.get("character_id"))
    mode = str(body.get("mode") or "").strip()
    label = str(body.get("label") or "").strip()
    key = f"companion_mode:{session_id}:{character_id}"
    # ★ 记录「一起听音乐」时长：进入 music 开始计时，离开时结算累计
    try:
        db.music_duration_tick(character_id, mode)
    except Exception:
        pass
    if mode:
        try:
            db.kv_set(key, json.dumps({"mode": mode, "label": label}, ensure_ascii=False))
        except Exception:
            pass
        return {"ok": True, "mode": mode, "label": label}
    try:
        db.kv_set(key, "")
    except Exception:
        pass
    return {"ok": True, "mode": "", "label": ""}


@app.post("/api/pc/config")
async def pc_set_config(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    try:
        cfg = config.update(body)
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    payload = pc_config_payload()
    await ws_manager.broadcast({"type": "config", "config": payload})
    return payload


# 2026-09-13 移除：本地大脑（Ollama）管理接口
# （/api/brain/local/status | model | config）与 _ollama_api 辅助函数。
# 聊天统一走云端，整组撤掉。见 git 快照 e114b61。


# ---------------- 本地大脑（Ollama）管理接口（2026-09-11） ----------------

async def _ollama_api(path: str, method: str = "GET", payload: dict = None, timeout: float = 4.0):
    """调 Ollama 原生接口（/api/version、/api/tags…）。返回 (json_or_None, err_str)。"""
    import httpx as _hx
    lb = config.local_brain()
    base = str(lb.get("baseUrl") or "http://127.0.0.1:11434").rstrip("/")
    try:
        async with _hx.AsyncClient(timeout=timeout) as _cli:
            if method == "GET":
                r = await _cli.get(base + path)
            else:
                r = await _cli.request(method, base + path, json=payload or {})
            if r.status_code != 200:
                return None, "HTTP %s: %s" % (r.status_code, r.text[:120])
            return r.json(), ""
    except Exception as _e:
        return None, str(_e)


@app.get("/api/brain/local/status")
async def brain_local_status():
    """本地大脑状态：Ollama 在线? + 已装模型列表 + 当前选中 + 最近一次云端回退记录。"""
    lb = config.local_brain()
    ver, err = await _ollama_api("/api/version")
    online = ver is not None
    models = []
    if online:
        tags, _terr = await _ollama_api("/api/tags")
        for _m in ((tags or {}).get("models") or []):
            models.append({
                "name": _m.get("name") or "",
                "size_gb": round((_m.get("size") or 0) / 1e9, 1),
                "params": ((_m.get("details") or {}).get("parameter_size") or ""),
                "modified": (_m.get("modified_at") or "")[:10],
            })
    from .deepseek_api import local_brain_last_fallback
    return {
        "ok": True,
        "online": online,
        "version": (ver or {}).get("version", "") if online else "",
        "error": "" if online else err,
        "models": models,
        "current": lb.get("model", ""),
        "enabled": bool(lb.get("enabled", True)),
        "think": bool(lb.get("think", True)),
        "num_ctx": lb.get("num_ctx", 16384),
        "fallback_model": lb.get("fallback_model", "deepseek-chat"),
        "last_fallback": local_brain_last_fallback(),
    }


@app.post("/api/brain/local/model")
async def brain_local_model_set(request: Request):
    """切换本地模型（下拉：qwen3:4b / qwen2.5:7b / 训练回灌的自定义模型…）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    model = str(body.get("model") or "").strip()
    if not model:
        return JSONResponse({"error": {"message": "缺少 model"}}, status_code=400)
    lb = config.save_local_brain({"model": model})
    print(f"[LocalBrain] 本地模型已切换 → {model}", flush=True)
    return {"ok": True, "model": lb.get("model", model)}


@app.post("/api/brain/local/config")
async def brain_local_config_set(request: Request):
    """更新本地大脑全局配置（enabled / think / fallback_model）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    patch = {}
    for _k in ("enabled", "think"):
        if _k in body:
            patch[_k] = bool(body.get(_k))
    if "fallback_model" in body:
        patch["fallback_model"] = str(body.get("fallback_model") or "").strip()
    lb = config.save_local_brain(patch)
    return {"ok": True, "LOCAL_BRAIN": lb}

@app.post("/api/screen/look")
async def api_screen_look(request: Request):
    """
    手动触发屏幕感知（Q3：用户说"你看看我在干嘛"时调用）。
    立即截一张图 + 视觉分析 + 直接返回描述。
    """
    try:
        payload = {}
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        session_id = payload.get("session_id") or "default"
        character_id = _resolve_char_id(payload.get("character_id"), payload.get("character_name") or payload.get("character_id"))
        # 手动“你看看我在干嘛”是明确授权，不受生活陪伴模式门禁；但记录必须按角色隔离。

        # 1. 截图（优先本地截屏 mss，需 pip install mss）
        from .proactive.screen_capture import capture_local, save_capture
        raw = capture_local()
        if not raw:
            return {"ok": False, "error": "截图失败，请检查 mss 库是否安装"}
        image_path = save_capture(raw)
        if not image_path:
            return {"ok": False, "error": "截图保存失败"}

        # 2. 视觉分析（★ 同步 httpx HTTP 阻塞 30s，丢线程池避免卡事件循环）
        from .proactive.vision_analyzer import analyze_screen
        # 明确点击/说出“让你看看”属于一次性授权，跳过视觉模块冷却。
        result = await asyncio.to_thread(analyze_screen, image_path, "你", True)
        if not result:
            return {"ok": False, "error": "视觉分析失败，请检查视觉 API Key"}

        # 3. 构建上下文（顺便写记忆）
        from .proactive.proactive_manager import build_screen_context
        build_screen_context(result, session_id, character_id)

        # 4. 写入记忆（interesting 的才写；★ 同步 DB 写入丢线程池）
        if result.get("interesting"):
            from .proactive.triggers import _write_screen_memory
            await asyncio.to_thread(_write_screen_memory, result, session_id, character_id)

        return {
            "ok": True,
            "summary": result.get("summary", ""),
            "activity": result.get("activity", ""),
            "engagement": result.get("engagement", 0),
            "topic_hint": result.get("topic_hint", ""),
            "scene_name": result.get("scene_name", ""),
            "interesting": result.get("interesting", False),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── AI 监督吃醋系统：状态查询 / 手动试一条 ────────────────────────
@app.get("/api/jealousy/status")
async def jealousy_status(session_id: str = "default", character_id: str = "default"):
    """当前吃醋值 + 最近在用什么（设置页展示 / 排障用）。"""
    try:
        from .jealousy import snapshot
        return snapshot(session_id, character_id)
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/jealousy/test")
async def jealousy_test(request: Request):
    """手动触发一条吃醋消息（想看效果时点一下，不用真去刷一小时抖音）。"""
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    sid = payload.get("session_id") or "default"
    cid = (_resolve_char_id(payload.get("character_id"),
                            payload.get("character_name") or "") or "default")
    try:
        from .jealousy import force_trigger
        res = await force_trigger(sid, cid, fake_minutes=1)
        text = str((res or {}).get("text") or "")
        return {"ok": bool(text), "message": text,
                "delivered": bool((res or {}).get("delivered")),
                "why": str((res or {}).get("why") or "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/pc/validate_model")
async def pc_validate_model(request: Request):
    """发送 1 token 请求验证模型 ID 是否为官方有效模型。"""
    try:
        body = await request.json()
        model = config.validate_model_id(body.get("model"))
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    key = config.api_key()
    if not key:
        return JSONResponse({"error": {"message": "尚未配置 API Key，无法校验"}}, status_code=400)
    try:
        await chat_once(model, [{"role": "user", "content": "hi"}], key,
                        max_tokens=1, temperature=0.0)
    except ModelApiError as e:
        return JSONResponse({"error": {
            "message": "模型 ID 无效或不可用（%s）：%s" % (e.status, e)}}, status_code=400)
    return {"ok": True, "model": model}


# ---------------- 新增：记忆导入导出 ----------------

@app.get("/api/pc/memory/export")
async def memory_export(
    format: str = "txt", key: str = "",
    session_id: str = "default", character_id: str = "default"
):
    try:
        if format == "json":
            data = export_import.export_json_backup(
                key, session_id=session_id, character_id=character_id
            )
            fname = "memory_backup_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S")
        else:
            data = export_import.export_txt(
                session_id=session_id, character_id=character_id
            )
            fname = "memory_backup_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S")
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)
    return Response(content=data,
                    media_type="application/octet-stream",
                    headers={"Content-Disposition":
                             "attachment; filename*=UTF-8''%s" % fname})


@app.post("/api/pc/memory/import")
async def memory_import(request: Request):
    try:
        body = await request.json()
        filename = str(body.get("filename") or "")
        encoded = str(body.get("content_b64") or "")
        if len(encoded) > 16 * 1024 * 1024:
            return JSONResponse({"error": {"message": "备份文件过大"}}, status_code=400)
        raw = base64.b64decode(encoded, validate=True)
        mode = str(body.get("mode") or "merge")
        password = str(body.get("key") or "")
        # 导入目标角色（可选，默认 default；多角色备份导入不互相覆盖）
        session_id = str(body.get("session_id") or "default")
        character_id = str(body.get("character_id") or "default")
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    if mode not in ("merge", "overwrite"):
        mode = "merge"
    try:
        # ★ 同步导入（merge 模式 O(N²) 去重）丢线程池，避免阻塞事件循环
        result = await asyncio.to_thread(
            export_import.import_backup,
            filename, raw, mode, password,
            session_id, character_id
        )
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": {"message": "导入失败：%s" % e}}, status_code=500)
    await ws_manager.broadcast({"type": "memory_updated", "total": result.get("total")})
    return result


@app.get("/api/pc/memory/list")
async def memory_list(
    page: int = 1,
    per_page: int = 15,
    search: str = "",
    character_id: str = "",
    session_id: str = ""
):
    """
    长期记忆列表，支持分页 / 搜索 / 角色过滤。
    ★ 改：原来直接返回全量 valid_memories()，现在支持分页+搜索+过滤
    """
    # 参数防御
    page     = max(1, page)
    per_page = max(1, min(100, per_page))
    offset   = (page - 1) * per_page

    # valid_memories 签名：(category=None, session_id="default", character_id="default")
    # 不传 session_id/character_id 时用默认值 "default"
    _sid = session_id or "default"
    _cid = character_id or "default"
    all_mems = await db._run_sync(
        db.valid_memories,
        session_id=_sid,
        character_id=_cid
    )

    # 搜索过滤（后端过滤，避免把几千条全发前端）
    if search:
        s = search.lower()
        all_mems = [
            m for m in all_mems
            if s in str(m.get("memory_content", "")).lower()
        ]

    total    = len(all_mems)
    page_mems = all_mems[offset: offset + per_page]

    return {
        "memories": page_mems,
        "total":    total,
        "page":     page,
        "per_page": per_page,
    }


@app.get("/api/memory/daily_reports")
async def memory_daily_reports(
    session_id: str = "default", character_id: str = "", limit: int = 180
):
    reports = await db._run_sync(
        db.list_daily_memory_reports,
        session_id or "default", character_id or "", max(1, min(limit, 365))
    )
    return {"reports": reports, "total": len(reports)}


@app.post("/api/memory/daily_reports")
async def memory_save_daily_report(request: Request):
    try:
        body = await request.json()
        session_id = str(body.get("session_id") or "default")
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
        report_date = str(body.get("date") or "").strip()
        content = str(body.get("content") or "").strip()
    except Exception:
        return JSONResponse({"error": {"message": "invalid request"}}, status_code=400)
    if not report_date or not content:
        return JSONResponse({"error": {"message": "date and content are required"}}, status_code=400)
    await db._run_sync(
        db.save_daily_memory_report, session_id, character_id, report_date, content
    )
    return {"ok": True}


# ---------------- 外置记忆库 API ----------------

@app.get("/api/external_memory/list")
async def external_memory_list(character_id: str = "default"):
    """列出外置记忆库结构（原文/日总结/周总结/月总结的文件清单）。"""
    try:
        from .external_memory import list_archive
        return {"ok": True, "data": list_archive(character_id)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/external_memory/read")
async def external_memory_read(character_id: str = "default", category: str = "", name: str = ""):
    """读取外置记忆库里的某个文件（category: 原文/日总结/周总结/月总结）。"""
    from pathlib import Path
    try:
        from .external_memory import paths as _emp
        _map = {"原文": _emp.raw_dir, "日总结": _emp.daily_dir,
                "周总结": _emp.weekly_dir, "月总结": _emp.monthly_dir}
        _fn = _map.get(category)
        if not _fn or not name:
            return {"ok": False, "error": "category 或 name 无效"}
        _safe = Path(name).name   # 防路径穿越
        _fp = _fn(character_id) / _safe
        if not _fp.exists():
            return {"ok": False, "error": "文件不存在"}
        return {"ok": True, "name": _safe, "content": _fp.read_text("utf-8", errors="replace")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/external_memory/export")
async def external_memory_export(character_id: str = "default", year: int = 0, month: int = 0):
    """导出外置记忆库为单篇 markdown：month>0 导出该月（每天原文+日总结），
    否则导出全年（12 个月逐天 + 该年周总结 + 该年月总结）。"""
    from pathlib import Path
    from datetime import date, timedelta
    try:
        year = int(year)
    except (TypeError, ValueError):
        return {"ok": False, "error": "year 无效"}
    try:
        from .external_memory import paths as _emp

        def _read(fp) -> str:
            try:
                return fp.read_text("utf-8").strip()
            except Exception:
                return ""

        parts = []
        title = f"{character_id} 的外置记忆 · {year}年" + (f"{month}月" if month else "")
        parts.append(f"# {title}\n")

        if 1 <= month <= 12:
            # 月导出：该月每天 原文 + 日总结
            d = date(year, month, 1)
            while d.month == month:
                ds = d.strftime("%Y-%m-%d")
                for cat, fn in (("原文", _emp.raw_dir), ("日总结", _emp.daily_dir)):
                    txt = _read(fn(character_id) / (ds + ".md"))
                    if txt:
                        parts.append(f"\n## {ds} {cat}\n\n{txt}")
                d += timedelta(days=1)
        else:
            # 年导出：逐月每天 + 该年周总结 + 该年月总结
            for m in range(1, 13):
                _d = date(year, m, 1)
                while _d.month == m:
                    ds = _d.strftime("%Y-%m-%d")
                    txt = _read(_emp.raw_dir(character_id) / (ds + ".md"))
                    if txt:
                        parts.append(f"\n## {ds} 原文\n\n{txt}")
                    _d += timedelta(days=1)
            for fp in sorted(_emp.weekly_dir(character_id).glob("*.md")):
                if fp.stem.startswith(str(year)):
                    txt = _read(fp)
                    if txt:
                        parts.append(f"\n## {fp.stem} 周总结\n\n{txt}")
            for fp in sorted(_emp.monthly_dir(character_id).glob("*.md")):
                if fp.stem.startswith(str(year)):
                    txt = _read(fp)
                    if txt:
                        parts.append(f"\n## {fp.stem} 月总结\n\n{txt}")

        content = "\n".join(parts)
        if len(content) > 4_000_000:
            content = content[:4_000_000] + "\n\n（内容过长已截断）"
        _name = (f"外置记忆_{character_id}_{year}年{month}月.md" if month
                 else f"外置记忆_{character_id}_{year}年.md")
        return {"ok": True, "name": _name, "content": content}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- 关系/亲密度 API ----------------

@app.get("/api/user/profile")
async def api_get_user_profile(session_id: str = "default", character_id: str = "default"):
    """读取结构化用户档案（我的信息）。新字段存 ext_json。"""
    p = db.get_profile(session_id, character_id)
    ext = {}
    try:
        ext = json.loads(p.get("ext_json") or "{}")
    except Exception:
        ext = {}
    return {
        "nickname": p.get("nickname", ""),
        "personality": p.get("personality", ""),
        "occupation": p.get("occupation", ""),
        "birthday": ext.get("birthday", ""),
        "gender": ext.get("gender", ""),
        "city": ext.get("city", ""),
        "bio": ext.get("bio", ""),
        "hobbies": ext.get("hobbies", []) or [],
        "topic_preferences": ext.get("topic_preferences", []) or [],
        "dislikes": p.get("dislikes", ""),
        "communication_style": p.get("communication_style", ""),
        "emotional_traits": p.get("emotional_traits", ""),
    }


@app.post("/api/user/profile")
async def api_save_user_profile(request: Request):
    """保存结构化用户档案（我的信息）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    session_id = str(body.get("session_id") or "default").strip() or "default"
    # character_id=default 是所有伴侣共享的“我的信息”桶，必须原样保留；
    # 若调用方明确传角色名，则再按角色隔离保存。
    raw_character_id = str(body.get("character_id") or "default").strip() or "default"
    character_id = (
        "default" if raw_character_id == "default" and not body.get("character_name")
        else _resolve_char_id(raw_character_id, body.get("character_name"))
    )

    # 现有列
    db.update_profile(
        session_id=session_id, character_id=character_id,
        nickname=str(body.get("nickname") or "").strip(),
        occupation=str(body.get("occupation") or "").strip(),
        personality=str(body.get("personality") or "").strip(),
        dislikes=str(body.get("dislikes") or "").strip(),
        communication_style=str(body.get("communication_style") or "").strip(),
        emotional_traits=str(body.get("emotional_traits") or "").strip(),
        hobbies="、".join(str(x) for x in (body.get("hobbies") if isinstance(body.get("hobbies"), list) else [])) if body.get("hobbies") else None,
    )
    # 新结构化字段 → ext_json
    ext = {
        "birthday": str(body.get("birthday") or "").strip(),
        "gender": str(body.get("gender") or "").strip(),
        "city": str(body.get("city") or "").strip(),
        "bio": str(body.get("bio") or "").strip(),
        "hobbies": [str(x) for x in body.get("hobbies")] if isinstance(body.get("hobbies"), list) else [],
        "topic_preferences": [str(x) for x in body.get("topic_preferences")] if isinstance(body.get("topic_preferences"), list) else [],
    }
    db.update_profile(session_id=session_id, character_id=character_id, ext_json=json.dumps(ext, ensure_ascii=False))
    return {"ok": True}


@app.post("/api/intimacy/report")
async def api_intimacy_report(request: Request):
    """前端上报亲密度（手动调节滑条时调用）"""
    body       = await request.json()
    session_id = str(body.get("session_id", "") or "")
    character_id = str(body.get("character_id", "default") or "default")
    value      = body.get("value", 0)

    if not session_id:
        return {"error": {"message": "session_id 不能为空"}}

    from .intimacy_manager import report as intimacy_report
    await db._run_sync(intimacy_report, session_id, value, character_id)
    return {"ok": True, "value": value}


@app.post("/api/character/switch")
async def api_character_switch(request: Request):
    """
    角色切换通知接口。
    前端切换角色时调用，后端重置该 session 的情绪状态，
    防止角色A的怒气/冷战带入角色B。
    """
    try:
        body         = await request.json()
        session_id   = str(body.get("session_id",   "") or "").strip() or "default"
        character_id = str(body.get("character_id", "") or "").strip() or "default"
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)

    # 重置情绪状态
    try:
        eng = _get_emotion_engine()
        if eng:
            eng.reset_state(session_id, character_id)
    except Exception as e:
        print(f"[CharSwitch] 情绪重置失败(静默): {e}", flush=True)

    # 重置冷战态
    try:
        from .relationship.database import conn as rel_conn
        rc = rel_conn()
        rc.execute(
            "UPDATE relationship_state SET conflict_state='' "
            "WHERE user_id=? AND character_id=?",
            (session_id, character_id)
        )
        rc.commit()
        rc.close()
    except Exception as e:
        print(f"[CharSwitch] 冷战态重置失败(静默): {e}", flush=True)

    print(
        f"[CharSwitch] 角色切换完成: session={session_id} → character={character_id}",
        flush=True
    )
    return {"ok": True, "session_id": session_id, "character_id": character_id}


@app.get("/api/relationship/state")
async def api_relationship_state(
    session_id: str = "",
    character_id: str = "default"
):
    """获取当前关系状态（供成长面板展示）。

    ★ 2026-09-15 修（"总览相识天数不对"）：原来直接返回 relationship.db 里那一行的
    `created_at`。那是**这行关系记录的创建时间**，不是"认识多久"——关系表一旦被
    重打包覆盖/换会话重建，created_at 就变成当天，前端 flow7.js 按它换算 → 相识
    天数从 30 天掉成 1 天。现在 created_at 统一口径 = **角色卡的 created_at**
    （真正的相识日，2026-08-17），并补 `known_days` 直接给前端用；
    读不到角色卡时才退回关系行自己的 created_at。
    """
    if not session_id:
        return {"stage": "stranger", "intimacy": 0, "affection": 50,
                "trust": 0, "interaction_days": 0}

    from .relationship.manager import RelationshipManager
    mgr   = RelationshipManager()
    state = await db._run_sync(mgr.get_state, session_id, character_id) or {}

    try:
        _cc = None
        from .character_manager import get_character_any
        _cc = get_character_any(character_id) or {}
        _created = str(_cc.get("created_at") or "").strip()
        if _created:
            state["row_created_at"] = state.get("created_at")   # 保留原值备查
            state["created_at"] = _created[:10] if len(_created) >= 10 else _created
            try:
                _d0 = datetime.strptime(state["created_at"][:10], "%Y-%m-%d").date()
                state["known_days"] = (datetime.now().date() - _d0).days + 1
            except Exception:
                pass
    except Exception:
        pass

    # 手动值优先：自动成长已砍，用户手调的数值才是唯一真相
    try:
        _manual = mgr._load_manual_lock(session_id, character_id) or {}
        if _manual:
            state["manual"] = _manual
            for _k in ("intimacy", "affection", "trust", "stage", "interaction_days"):
                if _manual.get(_k) is not None:
                    state[_k] = _manual[_k]
    except Exception:
        pass

    state["auto_update"] = bool(config.relationship_auto_update())
    state["ui_visible"] = bool(config.relationship_ui_visible())
    return state


@app.get("/api/personality/growth")
async def api_personality_growth(session_id: str = "default", character_id: str = "default"):
    """角色成长档案：维度、等级、互动证据；仅返回当前 session+角色。"""
    try:
        from .personality.manager import PersonalityManager
        data = await db._run_sync(PersonalityManager().get_growth_profile, session_id, character_id)
        return {"ok": True, **(data or {})}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/personality/growth/reset")
async def api_personality_growth_reset(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or "default")
    character_id = str(body.get("character_id") or "default")
    try:
        from .personality.manager import PersonalityManager
        await db._run_sync(PersonalityManager().reset, session_id, character_id)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/relationship/update")
async def api_relationship_update(request: Request):
    """
    手动更新关系状态字段（管理面板调节数值时调用）。
    支持字段：intimacy / affection / trust / interaction_days / stage
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    session_id   = str(body.get("session_id", "") or "")
    character_id = str(body.get("character_id", "default") or "default")

    if not session_id:
        return JSONResponse({"error": {"message": "session_id 不能为空"}}, status_code=400)

    allowed = {"intimacy", "affection", "trust", "interaction_days", "stage"}
    updates = {k: v for k, v in body.items() if k in allowed}

    if not updates:
        return {"ok": True, "updated": 0, "state": {}}

    valid_stages = {"stranger", "friend", "close_friend", "lover", "soulmate"}
    try:
        for field in ("intimacy", "affection", "trust"):
            if field in updates:
                updates[field] = max(0, min(100, int(float(updates[field]))))
        if "interaction_days" in updates:
            updates["interaction_days"] = max(0, int(float(updates["interaction_days"])))
    except (TypeError, ValueError):
        return JSONResponse({"error": {"message": "关系数值格式不正确"}}, status_code=400)
    if "stage" in updates and str(updates["stage"]) not in valid_stages:
        return JSONResponse({"error": {"message": "未知的关系阶段"}}, status_code=400)

    # 如果更新了 intimacy，同步计算 stage
    if "intimacy" in updates and "stage" not in updates:
        from .relationship.evolution import calculate_stage
        updates["stage"] = calculate_stage(int(updates["intimacy"]))

    # 同步更新两套系统
    from .relationship.manager import RelationshipManager
    mgr = RelationshipManager()
    await db._run_sync(mgr.create_user, session_id, character_id)
    await db._run_sync(mgr.update, session_id, character_id, _manual_override=True, **updates)

    # 同步 intimacy → kv（保持两套一致）
    if "intimacy" in updates:
        await db._run_sync(
            db.kv_set,
            f"intimacy:{session_id}:{character_id}",
            int(updates["intimacy"])
        )

    state = await db._run_sync(mgr.get_state, session_id, character_id, False)
    return {"ok": True, "updated": len(updates), "state": state or {}}


@app.get("/api/relationship/archive")
async def api_relationship_archive(character_id: str = "default"):
    """关系档案统计概览：相识天数、消息/记忆/通话/表情包总量、活跃日期列表。"""
    cid = _resolve_char_id(character_id, character_id)
    try:
        stats = await db._run_sync(db.archive_stats, cid)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    # 相识日期：优先角色卡 created_at，其次最早消息日期
    first_date = stats.get("first_date") or ""
    try:
        from .character_manager import get_character_any
        _cc = get_character_any(cid) or {}
        _created = str(_cc.get("created_at") or "").strip()
        if _created:
            first_date = (_created[:10] if len(_created) >= 10 else _created)
    except Exception:
        pass
    days = 0
    if first_date:
        try:
            _d0 = datetime.strptime(first_date, "%Y-%m-%d").date()
            days = (datetime.now().date() - _d0).days + 1
        except Exception:
            days = 0
    stats["first_date"] = first_date
    stats["days"] = days
    stats["ok"] = True
    return stats


@app.get("/api/relationship/archive/messages")
async def api_relationship_archive_messages(
    character_id: str = "default",
    date: str = "",
):
    """按日期（YYYY-MM-DD）查询某角色当天的所有消息。"""
    cid = _resolve_char_id(character_id, character_id)
    try:
        msgs = await db._run_sync(db.messages_by_date, cid, date)
        return {"ok": True, "date": date, "messages": msgs, "total": len(msgs)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/relationship/milestones")
async def api_relationship_milestones(
    session_id: str = "",
    character_id: str = "default",
    limit: int = 20
):
    """获取里程碑列表（成长面板展示）"""
    if not session_id:
        return {"milestones": []}

    from .relationship.database import conn as rel_conn
    rows = await db._run_sync(
        _get_milestones, session_id, character_id, limit, rel_conn
    )
    return {"milestones": rows}


@app.get("/api/relationship/keeps")
async def api_relationship_keeps(
    session_id: str = "default",
    character_id: str = "default",
    limit: int = 100,
):
    """联系人侧永久纪念收藏：月度信、里程碑信、专属语音和礼物。"""
    try:
        rows = await db._run_sync(
            db.list_relationship_keeps, session_id, character_id, limit
        )
        return {"ok": True, "keeps": rows, "total": len(rows)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/relationship/keeps")
async def api_relationship_keep_save(request: Request):
    """手动把早晚安、普通信件、小惊喜收藏进该人格的纪念收藏。"""
    try:
        body = await request.json()
        session_id = str(body.get("session_id") or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name") or body.get("character_id"))
        content = str(body.get("content") or "").strip()
        if not content:
            return JSONResponse({"ok": False, "error": "收藏内容为空"}, status_code=400)
        keep_type = str(body.get("keep_type") or body.get("type") or "letter").strip() or "letter"
        title = str(body.get("title") or "纪念收藏").strip() or "纪念收藏"
        source_key = str(body.get("source_key") or "").strip()
        if not source_key:
            digest = hashlib.sha1(f"{session_id}|{character_id}|{keep_type}|{title}|{content}".encode("utf-8")).hexdigest()[:16]
            source_key = f"manual_keep:{digest}"
        gift = body.get("gift") if isinstance(body.get("gift"), dict) else {}
        if not gift:
            icon_map = {
                "morning": "🌅", "evening": "🌙", "night": "🌙",
                "gift": "🎁", "surprise": "🎁", "festival": "🎐",
                "letter": "💌",
            }
            gift = {"icon": icon_map.get(keep_type, "💌"), "label": title}
        await db._run_sync(
            db.save_relationship_keep,
            session_id, character_id, keep_type, title, content,
            str(body.get("audio_url") or body.get("audio") or ""),
            gift,
            source_key,
        )
        return {"ok": True, "source_key": source_key}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/api/relationship/keeps")
async def api_relationship_keep_delete(request: Request):
    """取消手动收藏；不会删除聊天记录或本地信件正文。"""
    try:
        body = await request.json()
        session_id = str(body.get("session_id") or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name") or body.get("character_id"))
        source_key = str(body.get("source_key") or "").strip()
        if not source_key:
            return JSONResponse({"ok": False, "error": "缺少 source_key"}, status_code=400)
        await db._run_sync(db.delete_relationship_keep, session_id, character_id, source_key)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/relationship/keeps/{keep_id}/audio")
async def api_relationship_keep_audio(keep_id: int, request: Request):
    """旧收藏没有语音时，按当前角色音色补生成并永久保存。"""
    try:
        body = await request.json()
        session_id = str(body.get("session_id") or "default")
        character_id = str(body.get("character_id") or "default")
        item = await db._run_sync(
            db.get_relationship_keep, keep_id, session_id, character_id
        )
        if not item:
            return JSONResponse({"ok": False, "error": "收藏不存在"}, status_code=404)
        if item.get("audio_url"):
            return {"ok": True, "audio_url": item["audio_url"]}

        from .tts import generate_audio, get_voice_cfg, apply_emotion_to_voice_cfg
        from .character_manager import select_character_voice_cfg
        voice_cfg = select_character_voice_cfg(character_id, emotion="tender", mode="chat") or get_voice_cfg("cosyvoice_default")
        voice_cfg = apply_emotion_to_voice_cfg(voice_cfg, "tender", 0.7)
        audio_url = await generate_audio(str(item.get("content") or ""), voice_cfg, base_url="")
        if not audio_url:
            return JSONResponse({"ok": False, "error": "语音生成失败，请检查角色音色服务"}, status_code=502)
        await db._run_sync(db.update_relationship_keep_audio, keep_id, audio_url)
        return {"ok": True, "audio_url": audio_url}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


def _get_milestones(session_id: str, character_id: str, limit: int = 20, conn_fn=None):
    """从 milestone_memory 表读里程碑"""
    try:
        _conn = conn_fn() if conn_fn else None
        if _conn is None:
            from .relationship.database import conn as _c
            _conn = _c()
        rows = _conn.execute(
            """
            SELECT id, event_type, content, created_at
            FROM milestone_memory
            WHERE user_id=? AND character_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, character_id, limit)
        ).fetchall()
        _conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[Milestones] 读取失败: {e}", flush=True)
        return []


# ---------------- 新增：统一记忆库（磁盘文件 + 数据库条目合并） ----------------

@app.get("/api/pc/memory/all")
async def memory_all(character: str = ""):
    """合并返回：磁盘记忆库文件 + 数据库长期记忆，前端统一渲染为「记忆库」（按角色隔离）"""
    # 已知角色名（用于判断磁盘文件是否"角色专属"）
    known_chars = set()
    try:
        from . import character_manager
        for c in character_manager.list_characters():
            if c.get("name"):
                known_chars.add(str(c["name"]))
    except Exception:
        pass
    # 磁盘文件
    disk_items = []
    try:
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        for f in sorted(LIB_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.suffix.lower() in (".txt", ".md"):
                stem = f.stem
                # 文件名含角色名 → 该角色专属；否则通用
                owner = None
                for c in known_chars:
                    if c and c in stem:
                        owner = c
                        break
                if owner and owner != character:
                    continue
                st = f.stat()
                try:
                    preview = f.read_text("utf-8")[:300]
                except OSError:
                    preview = ""
                disk_items.append({
                    "id": "disk:" + f.name,
                    "source": "disk",
                    "title": f.stem,
                    "content": preview,
                    "full_content": "",  # 按需读取
                    "date": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                    "size": st.st_size,
                    "enabled": True,
                })
    except OSError:
        pass
    # 数据库条目（按角色隔离：global 全可见，character/relationship 仅当前角色）
    db_items = []
    for m in db.valid_memories(character_id=character):
        content = m["memory_content"]
        db_items.append({
            "id": "db:" + str(m["id"]),
            "source": "db",
            "title": "",
            "content": content,
            "date": (m.get("update_time") or m.get("create_time") or "")[:10],
            "enabled": True,
        })
    return {
        "disk": disk_items,
        "db": db_items,
        "disk_count": len(disk_items),
        "db_count": len(db_items),
        "total": len(disk_items) + len(db_items),
    }


@app.post("/api/pc/memory/add")
async def memory_add(request: Request):
    """手动添加一条数据库长期记忆"""
    try:
        body = await request.json()
        content = str(body.get("content") or "").strip()
        session_id   = str(body.get("session_id", "default") or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    if not content:
        return JSONResponse({"error": {"message": "内容不能为空"}}, status_code=400)
    # dedupe_insert 是同步慢操作（相似度比较+写向量库），放线程池避免阻塞事件循环
    import asyncio as _asyncio
    action = await get_loop().run_in_executor(
        None, memory_manager.dedupe_insert, content, "fact", 5, session_id, character_id
    )
    await ws_manager.broadcast({"type": "memory_updated", "total": db.count_memories()})
    return {"ok": True, "action": action, "total": db.count_memories()}


@app.post("/api/pc/memory/delete")
async def memory_delete(request: Request):
    """删除一条记忆。id 格式 disk:文件名 或 db:数字id"""
    try:
        body = await request.json()
        mid = str(body.get("id") or "")
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    if mid.startswith("disk:"):
        name = mid[5:]
        fp = (LIB_DIR / safe_lib_name(name)).resolve()
        if not str(fp).startswith(str(LIB_DIR.resolve())):
            return JSONResponse({"error": {"message": "Forbidden"}}, status_code=403)
        try:
            fp.unlink()
        except OSError as e:
            return JSONResponse({"error": {"message": "删除失败：%s" % e}}, status_code=500)
    elif mid.startswith("db:"):
        try:
            mem_id = int(mid[3:])
        except ValueError:
            return JSONResponse({"error": {"message": "无效的记忆 ID"}}, status_code=400)
        mem = db.get_memory(mem_id) or {}
        req_session = str(body.get("session_id") or "default").strip() or "default"
        req_character = _resolve_char_id(body.get("character_id"), body.get("character_name"))
        if mem and (str(mem.get("session_id")) != req_session or str(mem.get("character_id")) != req_character):
            return JSONResponse({"error": {"message": "无权删除其他会话或角色的记忆"}}, status_code=403)
        db.delete_memory(mem_id)
        try:
            from .memory.vector_store import delete_vector
            delete_vector(f"sql:{mem_id}")
        except Exception:
            pass
        memory_manager.invalidate_memory_cache(
            mem.get("session_id"), mem.get("character_id")
        )
    else:
        return JSONResponse({"error": {"message": "无效的记忆 ID"}}, status_code=400)
    await ws_manager.broadcast({"type": "memory_updated", "total": db.count_memories()})
    return {"ok": True, "total": db.count_memories()}


@app.post("/api/pc/memory/update")
async def memory_update(request: Request):
    """编辑一条长期记忆（内容/重要度/类型）。记忆统一：以后端 sqlite 为唯一真相源。"""
    try:
        body = await request.json()
        mem_id = int(body.get("id"))
        content = str(body.get("content") or "").strip()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    if not content:
        return JSONResponse({"error": {"message": "内容不能为空"}}, status_code=400)
    try:
        importance = int(body.get("importance") or 0)
    except (TypeError, ValueError):
        importance = 0
    memory_type = str(body.get("memory_type") or "").strip() or None
    mem = db.get_memory(mem_id) or {}
    req_session = str(body.get("session_id") or "default").strip() or "default"
    req_character = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    if not mem:
        return JSONResponse({"error": {"message": "记忆不存在"}}, status_code=404)
    if str(mem.get("session_id")) != req_session or str(mem.get("character_id")) != req_character:
        return JSONResponse({"error": {"message": "无权修改其他会话或角色的记忆"}}, status_code=403)
    # update_memory_full 是同步 sqlite 写，丢线程池
    import asyncio as _asyncio
    await _asyncio.to_thread(
        db.update_memory_full, mem_id,
        content=content,
        importance=(importance if 1 <= importance <= 10 else None),
        memory_type=memory_type
    )
    # 编辑后同步刷新向量，防止 AI 继续召回旧正文。
    mem = db.get_memory(mem_id) or {}
    if mem:
        await _asyncio.to_thread(
            memory_manager._sync_vector_upsert,
            mem_id, mem.get("session_id", "default"),
            mem.get("character_id", "default"), content,
            memory_type or mem.get("memory_type", "fact"),
            importance if 1 <= importance <= 10 else int(mem.get("importance", 5) or 5)
        )
        memory_manager.invalidate_memory_cache(
            mem.get("session_id"), mem.get("character_id")
        )
    await ws_manager.broadcast({"type": "memory_updated", "total": db.count_memories()})
    return {"ok": True, "total": db.count_memories()}


@app.post("/api/pc/memory/migrate_local")
async def memory_migrate_local(request: Request):
    """把前端 localStorage 的历史记忆批量迁入后端 sqlite（记忆统一）。"""
    try:
        body = await request.json()
        items = body.get("items") or []
        if not isinstance(items, list):
            return JSONResponse({"error": {"message": "items 必须是数组"}}, status_code=400)
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)

    saved = 0
    for it in items[:500]:  # 上限 500 条防滥用
        if not isinstance(it, dict):
            continue
        content = str(it.get("content") or "").strip()
        if not content or len(content) < 2:
            continue
        cname = str(it.get("character_name") or "").strip() or "default"
        cid = _resolve_char_id(it.get("character_id"), cname)
        # dedupe_insert 自动分类 memory_scope，并做相似度去重
        action = await asyncio.to_thread(
            memory_manager.dedupe_insert, content,
            "fact", 5, "default", cid
        )
        if action in ("inserted", "updated"):
            saved += 1

    await ws_manager.broadcast({"type": "memory_updated", "total": db.count_memories()})
    return {"ok": True, "saved": saved, "total": db.count_memories()}


@app.post("/api/pc/memory/extract_now")
async def memory_extract_now(request: Request):
    """手动触发：从最近对话提炼记忆（静默执行，返回提炼条数）"""
    try:
        body = await request.json()
        session_id   = str(body.get("session_id") or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    except Exception:
        session_id   = "default"
        character_id = "default"
    if not config.api_key():
        return JSONResponse({"error": {"message": "尚未配置 API Key，无法提炼"}}, status_code=400)
    n = await memory_manager.run_auto_extract(session_id, character_id)
    await ws_manager.broadcast({"type": "memory_updated", "total": db.count_memories()})
    return {"ok": True, "extracted": n, "total": db.count_memories()}


@app.get("/api/memory/health")
async def api_memory_health(
    session_id:   str = "default",
    character_id: str = "default"
):
    """
    记忆健康度报告接口。
    供前端展示或调试用。
    """
    try:
        from .memory.maintenance import get_memory_health_report
        report = await db._run_sync(
            get_memory_health_report, session_id, character_id
        )
        return report
    except Exception as e:
        return {"total": 0, "error": str(e)}


# ══════════════════════════════════════════════════════════════════
# 记忆变更审计（「她整理了什么 / 学到了什么」+ 一键撤回）
# ★ 2026-09-17 新增。用户拍板："允许她自己整理，但每次改动要留痕、可查、可一键撤回"。
#   后端表与撤回函数在 backend/memory_audit.py；这里只做查询/撤回的门面。
# ══════════════════════════════════════════════════════════════════

@app.get("/api/memory/audit")
async def api_memory_audit(
    session_id: str = "default",
    character_id: str = "default",
    limit: int = 50
):
    """记忆变更记录（按批次汇总 + 明细）。

    返回：
      batches  —— 每次整理一行（含 batch_id，可整批撤回）
      recent   —— 最近 N 条明细（每条的 before/after 快照，便于看清改了什么）
      summary  —— 各操作类型计数（merge/regrade/forget/…）
    """
    try:
        from . import memory_audit
        sid = str(session_id or "default")
        cid = str(character_id or "default")
        lim = max(1, min(500, int(limit or 50)))
        batches = await db._run_sync(memory_audit.recent_batches, sid, cid, 30)
        recent = await db._run_sync(memory_audit.recent, sid, cid, lim)
        summ = await db._run_sync(memory_audit.summary, sid, cid)
        # 明细别把整行快照全塞给前端（几十 KB/条），只留展示需要的字段
        slim = []
        for r in recent:
            before = r.get("before") or {}
            after = r.get("after") or {}
            slim.append({
                "id": r.get("id"),
                "ts": r.get("ts"),
                "op": r.get("op"),
                "memory_id": r.get("memory_id"),
                "reason": r.get("reason"),
                "source": r.get("source"),
                "revertible": bool(r.get("revertible")),
                "reverted": bool(r.get("reverted")),
                "batch_id": r.get("batch_id"),
                "before_content": str(before.get("memory_content") or "")[:200],
                "after_content": str(after.get("memory_content") or "")[:200],
                "before_importance": before.get("importance"),
                "after_importance": after.get("importance"),
                "after_status": after.get("memory_status"),
            })
        return {"ok": True, "batches": batches, "recent": slim, "summary": summ}
    except Exception as e:
        return {"ok": False, "error": str(e), "batches": [], "recent": [], "summary": {}}


@app.post("/api/memory/audit/revert")
async def api_memory_audit_revert(request: Request):
    """撤回一次整理：给 batch_id 整批撤回，或给 audit_id 撤单条。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    batch_id = str(body.get("batch_id") or "").strip()
    audit_id = body.get("audit_id")
    try:
        from . import memory_audit
        if batch_id:
            res = await db._run_sync(memory_audit.revert_batch, batch_id)
            return {"ok": bool(res.get("ok")), "mode": "batch", **res}
        if audit_id is not None:
            res = await db._run_sync(memory_audit.revert, int(audit_id))
            return {"ok": bool(res.get("ok")), "mode": "single", **res}
        return {"ok": False, "error": "需要 batch_id 或 audit_id"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/learning/status")
async def api_learning_status(
    session_id: str = "default",
    character_id: str = "default"
):
    """「她学到了什么」：今天为止从用户身上学到的偏好/规矩/反思。

    ★ 为什么单独给一个只读接口：这些学习成果以前散在 kv / 文件 / 反思表里，
      用户完全看不到（于是"AI 到底有没有在学"只能靠感觉）。
      这里把它们汇总成一份可读清单 —— 看得见，才谈得上信任。
    """
    out = {"ok": True, "style_preferences": [], "rules": [], "reflections": []}
    sid = str(session_id or "default")
    cid = str(character_id or "default")
    try:
        from . import style_feedback
        out["style_preferences"] = list(style_feedback._load_list(cid) or [])
    except Exception as e:
        out["style_error"] = str(e)
    try:
        from .agent import self_modules as sm
        out["rules"] = [{"id": r.get("id"), "text": r.get("text"),
                         "created": r.get("created"), "source": r.get("source")}
                        for r in (sm.load_rules(cid) or [])]
    except Exception as e:
        out["rules_error"] = str(e)
    try:
        from .reflection.database import get_recent_reflections
        rows = await db._run_sync(get_recent_reflections, sid, cid, 10)
        out["reflections"] = [{"type": r.get("reflection_type"),
                               "content": str(r.get("content") or "")[:200],
                               "confidence": r.get("confidence"),
                               "used": bool(r.get("last_used"))}
                              for r in (rows or [])]
    except Exception as e:
        out["reflections_error"] = str(e)
    return out


@app.post("/api/memory/maintain")
async def api_memory_maintain(request: Request):
    """
    手动触发记忆维护（管理面板用）。
    """
    try:
        body         = await request.json()
        session_id   = str(body.get("session_id",   "default") or "default")
        character_id = str(body.get("character_id", "default") or "default")
    except Exception:
        session_id   = "default"
        character_id = "default"

    try:
        from .memory.maintenance import run_full_maintenance
        result = await db._run_sync(
            run_full_maintenance, session_id, character_id
        )
        return {"ok": True, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/understanding/stats")
async def api_understanding_stats(hours: int = 24):
    """语义理解层（第一层 DeepSeek）运行质量统计 —— Phase 0 的可观测产物。

    用来判断第一层到底是「没跑到」「判不准」还是「判准了没人用」，三种病药方不同：
      · by_outcome 里 disabled/internal/too_long/no_key/empty 占比高 → 根本没跑到
      · parse_failed/exception 占比高 → 跑到了但没产出（max_tokens 偏紧或 prompt 有问题）
      · ok 占比高但 intent 分布畸形 → 跑到了，问题在下游消费

    GET /api/understanding/stats?hours=24

    返回里还有 compliance 段（Phase 3 闭环）：回复是否遵守了执行清单。
      · violation_rate 高 → 注入措辞还不够硬，或某些意图的约束写得不对
      · top_violations → 看看具体是哪类要求总被当耳旁风
    """
    import asyncio
    try:
        from .understanding import log_stats, compliance_stats

        def _collect():
            _h = int(hours or 24)
            st = log_stats(_h)
            try:
                st["compliance"] = compliance_stats(_h)
            except Exception as _ce:
                st["compliance"] = {"error": str(_ce)}
            return st

        return await asyncio.to_thread(_collect)
    except Exception as e:
        return JSONResponse({"error": {"message": str(e), "type": type(e).__name__}},
                            status_code=500)


@app.get("/api/memory/diagnose")
async def api_memory_diagnose(
    session_id:   str = "default",
    character_id: str = "default",
    q: str = ""
):
    """
    记忆检索质量诊断：汇总各套记忆的召回情况，排查「该想起没想起」。
    """
    import asyncio
    result = {"session_id": session_id, "character_id": character_id, "query": q}

    # 1. 长期记忆双通道检索（结构化，带 score + importance）
    try:
        from . import memory_manager
        mems = await asyncio.to_thread(
            memory_manager.search_memories, q, 10, 0.15, session_id, character_id
        )
        result["long_term_retrieval"] = {
            "count": len(mems),
            "items": [
                {
                    "content": str(m.get("memory_content", ""))[:100],
                    "importance": m.get("importance"),
                    "memory_type": m.get("memory_type"),
                    "score": round(float(s), 3),
                }
                for m, s in mems
            ],
        }
    except Exception as e:
        result["long_term_retrieval"] = {"error": str(e)}

    # 2. 知识图谱
    try:
        from .knowledge_graph.prompt import build_knowledge_prompt
        kg = await asyncio.to_thread(build_knowledge_prompt, session_id, character_id, q)
        result["knowledge_graph"] = (kg or "")[:800]
    except Exception as e:
        result["knowledge_graph"] = f"error: {e}"

    # 3. 身份
    try:
        from .identity.manager import IdentityManager
        identity = await asyncio.to_thread(IdentityManager().build_identity_prompt, session_id, character_id)
        result["identity"] = (identity or "")[:500]
    except Exception as e:
        result["identity"] = f"error: {e}"

    # 4. 用户画像
    try:
        p = await asyncio.to_thread(db.get_profile, session_id, character_id)
        result["profile"] = {k: str(v)[:60] for k, v in (p or {}).items() if v} if p else {}
    except Exception as e:
        result["profile"] = {"error": str(e)}

    # 5. 人生档案
    try:
        from .profile_memory.manager import LifeProfileManager
        lp = await asyncio.to_thread(LifeProfileManager().get, session_id, character_id, 10)
        result["life_profile"] = {
            "count": len(lp or []),
            "items": [
                {"type": x.get("profile_type", ""), "content": str(x.get("content", ""))[:80], "importance": x.get("importance")}
                for x in (lp or [])[:6]
                if isinstance(x, dict)
            ],
        }
    except Exception as e:
        result["life_profile"] = {"error": str(e)}

    # 6. 时间线事件
    try:
        from .timeline.manager import TimelineManager
        tl = await asyncio.to_thread(TimelineManager().get_recent_events, session_id, character_id, 5)
        result["timeline"] = {
            "count": len(tl or []),
            "items": [str(x)[:80] for x in (tl or [])[:5]],
        }
    except Exception as e:
        result["timeline"] = {"error": str(e)}

    # 7. 有效记忆总数 + importance 分布
    try:
        vm = await asyncio.to_thread(db.valid_memories, None, session_id, character_id)
        result["valid_memories_total"] = len(vm or [])
        dist = {}
        for m in (vm or []):
            imp = int(m.get("importance", 5) or 5)
            dist[imp] = dist.get(imp, 0) + 1
        result["importance_distribution"] = dict(sorted(dist.items()))
    except Exception as e:
        result["valid_memories_total"] = f"error: {e}"

    return result


@app.post("/api/memory/re_evaluate")
async def api_memory_re_evaluate(request: Request):
    """手动触发：批量重评 importance=5 的历史记忆（LLM 打分）。"""
    try:
        body = await request.json()
        session_id   = str(body.get("session_id",   "default") or "default")
        character_id = str(body.get("character_id", "default") or "default")
    except Exception:
        session_id   = "default"
        character_id = "default"

    try:
        from .memory.re_evaluate import re_evaluate_importance
        result = await re_evaluate_importance(session_id, character_id)
        return {"ok": True, **result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/ai_mood/state")
async def api_ai_mood_state(
    session_id:   str = "default",
    character_id: str = "default"
):
    """AI 隐藏情绪状态（状态栏显示，有情绪才返回，无情绪返回 null）。"""
    try:
        from . import ai_mood
        display = ai_mood.get_display(session_id, character_id)
        return {"state": display}
    except Exception as e:
        return {"state": None, "error": str(e)}


# ---------------- 新增：角色配置管理（多角色独立存档） ----------------

@app.get("/api/pc/character/list")
async def character_list():
    return {"characters": character_manager.list_characters()}


@app.get("/api/pc/character/get")
async def character_get(name: str):
    data = character_manager.get_character(name)
    if not data:
        # 角色详情页允许首次编辑后再保存。返回空配置而不是 404，避免
        # 首次打开每个角色时产生重复的控制台错误和无意义重试。
        return {"character_name": name, "_missing": True}
    return data


@app.post("/api/pc/character/save")
async def character_save(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    try:
        data = character_manager.save_character(body)
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    # ★ 2026-09-16：让「主动回复由模型自主」等引擎侧角色卡开关**立刻生效**（清掉 10s 缓存）
    try:
        from . import proactive_engine as _eng
        _cid = _resolve_char_id(body.get("character_id") or body.get("name") or body.get("character_name"),
                                body.get("character_name") or body.get("name"))
        _eng.invalidate_model_decides(_cid)
    except Exception:
        pass
    return {"ok": True, "character": data}


@app.post("/api/pc/character/avatar")
async def character_avatar(request: Request):
    """把用户明确指定的图片设为 AI 角色头像；不依赖视觉模型。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求格式错误"}, status_code=400)
    name = str(body.get("character_name") or body.get("character_id") or "").strip()
    data_url = str(body.get("data") or "")
    session_id = str(body.get("session_id") or "default").strip() or "default"
    if not name:
        return JSONResponse({"ok": False, "error": "缺少角色名称"}, status_code=400)
    if not data_url.startswith("data:image") or "," not in data_url:
        return JSONResponse({"ok": False, "error": "头像必须是图片"}, status_code=400)
    try:
        import io
        import hashlib
        from PIL import Image, ImageOps
        global _avatar_dir
        if _avatar_dir is None:
            _avatar_dir = config.DATA_DIR / "character_avatars"
            _avatar_dir.mkdir(parents=True, exist_ok=True)
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise ValueError("头像图片不能超过 8MB")
        image = Image.open(io.BytesIO(raw))
        image.verify()
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")
        side = min(image.width, image.height)
        left = max(0, (image.width - side) // 2)
        top = max(0, (image.height - side) // 2)
        image = image.crop((left, top, left + side, top + side))
        image.thumbnail((512, 512), Image.Resampling.LANCZOS)
        safe = character_manager.safe_name(name)
        digest = hashlib.sha256(raw).hexdigest()[:12]
        filename = f"{safe}_{digest}.jpg"
        path = _avatar_dir / filename
        image.save(path, format="JPEG", quality=90, optimize=True)
        avatar_url = "/character_avatars/" + filename

        existing = character_manager.get_character(name) or {"character_name": name}
        existing = dict(existing)
        existing["character_name"] = name
        existing["avatar"] = avatar_url
        character_manager.save_character(existing)
        db.kv_set(f"character_avatar:{session_id}:{name}", avatar_url)
        try:
            db.add_timeline_event(session_id, name, "avatar_change", "你为我换了新头像",
                                  "用户亲自选了一张图片作为我的头像", 6)
        except Exception:
            pass
        return {"ok": True, "avatar_url": avatar_url, "character_name": name}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"头像保存失败：{e}"}, status_code=500)


@app.post("/api/pc/character/delete")
async def character_delete(request: Request):
    try:
        body = await request.json()
        name = str(body.get("name") or "")
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    ok = character_manager.delete_character(name)
    return {"ok": ok}


@app.get("/api/pc/character/system_prompt")
async def character_system_prompt(name: str):
    prompt = character_manager.build_system_prompt(name)
    if not prompt:
        return JSONResponse({"error": {"message": "角色不存在"}}, status_code=404)
    return {"name": name, "system_prompt": prompt}


@app.get("/api/pc/character/language_presets")
async def language_presets():
    """返回 7 个预设语言模板（供前端「一键套用语言模板」）"""
    try:
        from .personality import presets
    except Exception:
        return {"presets": []}
    result = []
    for key in presets.PERSONA_REGISTRY:
        style, vocab = presets.PERSONA_REGISTRY[key]
        result.append({
            "key": key,
            "label": presets.PERSONA_LABELS.get(key, key),
            "catchphrases": style.catchphrases[:3],
            "density": style.response_density.value,
            "dirty": style.dirty_talk.value,
        })
    return {"presets": result}


@app.get("/api/reflection/status")
async def reflection_status(session_id: str = "default", character_id: str = "default"):
    """学习/反思闭环状态，供资料页和故障检查确认是否真的落库并用于回复。"""
    try:
        from .reflection.database import get_recent_reflections
        refs = await db._run_sync(get_recent_reflections, session_id, character_id, 8)
        mems = await db._run_sync(db.valid_memories, session_id=session_id, character_id=character_id)
        try: last_at = float(await db.async_kv_get(f"reflection_last_at:{session_id}:{character_id}") or 0)
        except Exception: last_at = 0
        return {
            "ok": True,
            "memory_count": len(mems or []),
            "reflection_count": len(refs or []),
            "last_reflection_at": last_at,
            "last_result_count": int(await db.async_kv_get(f"reflection_last_result:{session_id}:{character_id}") or 0),
            "reflections": [
                {"type": r.get("reflection_type"), "content": r.get("content"),
                 "confidence": r.get("confidence"), "created_at": r.get("created_at")}
                for r in (refs or [])
            ],
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/reflection/run")
async def reflection_run(request: Request):
    """手动复盘一次；仍执行证据校验和去重，不会凭空生成记忆。"""
    try: body = await request.json()
    except Exception: body = {}
    sid = str(body.get("session_id") or "default")
    cid = str(body.get("character_id") or "default")
    try:
        from .reflection.analyzer import update_reflection
        from .reflection.strategy import apply_reflection_to_personality_state
        result = await update_reflection(sid, cid, force=True)
        patch = await db._run_sync(apply_reflection_to_personality_state, sid, cid)
        return {"ok": True, "result": result, "applied": patch}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/identity/letter")
async def identity_letter(session_id: str = "default", character_id: str = "default"):
    """拉取「身份坦诚小作文」（读后清除，一次性）——修复离线丢信问题。"""
    from .character_manager import get_character
    cid = _resolve_char_id(character_id, character_id)
    # 兼容：character_id 可能传角色名或 id
    try:
        _lk = f"identity_letter:{session_id}:{cid}"
        essay = db.kv_get(_lk)
        # 只有前端确认成功保存后才清除，避免离线、刷新或脚本异常导致丢信。
        return {"content": essay or "", "letter_key": _lk}
    except Exception:
        return {"content": ""}


@app.post("/api/identity/letter/ack")
async def identity_letter_ack(request: Request):
    """前端成功归档身份长信后确认消费后端待补收缓存。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_id"))
    db.kv_set(f"identity_letter:{session_id}:{character_id}", "")
    return {"ok": True}


# ---------------- 新增：Token 用量统计 ----------------

@app.post("/api/proactive/letter/read")
async def proactive_letter_read(request: Request):
    """记录用户已打开睡前信，避免第二天早安反复提醒同一封信。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or "default").strip() or "default"
    character_id = str(body.get("character_id") or "default").strip() or "default"
    letter_date = str(body.get("date") or "").strip()
    key = f"sleep_letter_unread:{session_id}:{character_id}"
    current = db.kv_get(key)
    if not letter_date or current == letter_date:
        db.kv_set(key, "")
    return {"ok": True, "unread": False}


@app.get("/api/pc/token/usage")
async def token_usage():
    return token_tracker.get_usage()


@app.get("/api/pc/token/callers")
async def token_callers(date: str = ""):
    """按调用点拆分的 token 用量（2026-09-14 新增）。

    回答"钱花在哪个模块"：chat_once(模块.函数) / stream_chat 各消耗多少。
    数据来源为 deepseek_api 每次真实 LLM 调用后的记账（有上游 usage 时用真实值）。
    date 省略则取今天，格式 YYYY-MM-DD。
    """
    try:
        callers = token_tracker.caller_usage(date)
    except Exception as e:
        return {"error": str(e), "callers": []}
    rows = [
        {"caller": k, "input": v[0], "output": v[1], "total": v[2]}
        for k, v in callers.items()
    ]
    total = sum(r["total"] for r in rows)
    return {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "total": total,
        "caller_count": len(rows),
        "callers": rows,
        "daily": token_tracker.get_usage(),
    }


@app.post("/api/pc/token/warn")
async def token_set_warn(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    daily = body.get("daily_warn")
    monthly = body.get("monthly_warn")
    token_tracker.set_warn_threshold(
        daily=int(daily) if daily else None,
        monthly=int(monthly) if monthly else None,
    )
    return {"ok": True}


@app.get("/api/pc/token/check_warn")
async def token_check_warn():
    return token_tracker.check_warn()


# ---------------- 新增：模板引擎 ----------------

@app.get("/api/pc/template/list")
async def template_list(category: str = None):
    return template_manager.list_templates(category)


@app.post("/api/pc/template/add")
async def template_add(request: Request):
    try:
        body = await request.json()
        category = str(body.get("category") or "")
        content = str(body.get("content") or "")
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    ok = template_manager.add_template(category, content)
    return {"ok": ok}


@app.post("/api/pc/template/delete")
async def template_delete(request: Request):
    try:
        body = await request.json()
        category = str(body.get("category") or "")
        index = int(body.get("index", -1))
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    ok = template_manager.delete_template(category, index)
    return {"ok": ok}


# ---------------- 新增：纪念日管理 ----------------

@app.get("/api/pc/anniversary/list")
async def anniversary_list():
    return {"anniversaries": anniversary_manager.list_anniversaries()}


@app.post("/api/pc/anniversary/add")
async def anniversary_add(request: Request):
    try:
        body = await request.json()
        item = anniversary_manager.add_anniversary(
            name=str(body.get("name") or ""),
            month=int(body.get("month", 0)),
            day=int(body.get("day", 0)),
            anniv_type=str(body.get("type") or "纪念日"),
            character_name=str(body.get("character") or ""),
            note=str(body.get("note") or ""),
        )
        return {"ok": True, "anniversary": item}
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)


@app.post("/api/pc/anniversary/delete")
async def anniversary_delete(request: Request):
    try:
        body = await request.json()
        aid = int(body.get("id", 0))
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    ok = anniversary_manager.delete_anniversary(aid)
    return {"ok": ok}


# ---------------- 新增：定时任务 ----------------

@app.get("/api/pc/task/list")
async def task_list(character: str = None, session_id: str = "", character_id: str = ""):
    sid = session_id or active_session() or "default"
    cid = _resolve_char_id(character_id, character) if (character_id or character) else "default"
    return {"tasks": db.list_all_tasks(character, sid, cid)}


# ---------------- 「她现在被哪些规则管着」审计 ----------------
# ★ 2026-09-16 用户要求「做到 App 里能查」：起因是他感觉"模型被限制、让她承认爱我都做不到"，
#   真因是角色卡的语言风格预设往 prompt 里写死了一行「绝对不用这些词：喜欢你/我爱你/…」。
#   这里把管着她的东西按来源摊开（只读：不写库、不调模型）。
@app.get("/api/pc/prompt/audit")
async def prompt_audit(character: str = "", character_id: str = "", session_id: str = ""):
    from .prompt_audit import build_audit
    cid = _resolve_char_id(character_id, character) if (character_id or character) else "default"
    sid = session_id or active_session() or "default"
    try:
        return build_audit(cid, sid)
    except Exception as e:
        return JSONResponse({"error": {"message": "规则清单生成失败: %s" % e}}, status_code=500)


@app.post("/api/pc/task/add")
async def task_add(request: Request):
    try:
        body = await request.json()
        tid = db.add_task(
            character_name=str(body.get("character") or "default"),
            session_id=str(body.get("session_id") or active_session() or "default"),
            character_id=_resolve_char_id(body.get("character_id"), body.get("character")),
            task_type=str(body.get("type") or "once"),
            trigger_time=str(body.get("trigger_time") or ""),
            content=str(body.get("content") or ""),
            source=str(body.get("source") or "manual"),
        )
        return {"ok": True, "id": tid}
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)


@app.post("/api/pc/task/delete")
async def task_delete(request: Request):
    try:
        body = await request.json()
        tid = int(body.get("id", 0))
        sid = str(body.get("session_id") or active_session() or "default")
        cid = _resolve_char_id(body.get("character_id"), body.get("character"))
        db.delete_task(tid, sid, cid)
        return {"ok": True}
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)


@app.post("/api/pc/task/trigger_now")
async def task_trigger_now(request: Request):
    """手动触发一次定时任务（测试用）"""
    try:
        body = await request.json()
        tid = int(body.get("id", 0))
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    tasks = db.list_all_tasks()
    for t in tasks:
        if t["id"] == tid:
            inst = scheduler.scheduler.get_or_create(
                str(t.get("session_id") or "default"), str(t.get("character_id") or "default"),
                character_manager.get_character(str(t.get("character_id") or "default")) or {},
            )
            ok = await inst._execute_task(t)
            if ok:
                db.finish_task(tid)
            return {"ok": bool(ok)}
    return JSONResponse({"error": {"message": "任务不存在"}}, status_code=404)


# ---------------- 新增：表情包素材库 ----------------

@app.get("/api/pc/sticker/list")
async def sticker_list():
    return {"stickers": sticker_manager.list_stickers()}


@app.post("/api/pc/sticker/pick")
async def sticker_pick(request: Request):
    """按聊天语境/情绪选择一张表情包，供普通流式前端做可靠兜底。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    item = sticker_manager.pick_for_context(
        str(body.get("text") or ""),
        str(body.get("emotion") or ""),
        str(body.get("exclude") or ""),
    )
    return {"ok": bool(item), "sticker": item or None}


@app.post("/api/pc/sticker/delete")
async def sticker_delete(request: Request):
    try:
        body = await request.json()
        filename = str(body.get("filename") or "")
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    ok = sticker_manager.delete_sticker(filename)
    return {"ok": ok}


@app.post("/api/pc/sticker/upload")
async def sticker_upload(request: Request):
    """上传表情包图片（base64方式，无需python-multipart）"""
    import base64
    import time
    try:
        body = await request.json()
        filename = str(body.get("filename") or "")
        data_url = str(body.get("data") or "")
        tag = str(body.get("tag") or "").strip()
        desc = str(body.get("desc") or "").strip()
        if not data_url.startswith("data:image"):
            return JSONResponse({"error": {"message": "格式错误"}}, status_code=400)
        if len(data_url) > 8 * 1024 * 1024:
            return JSONResponse({"error": {"message": "图片过大"}}, status_code=400)
        # 解析 data URL
        header, b64data = data_url.split(",", 1)
        ext = ".png"
        if "jpeg" in header or "jpg" in header: ext = ".jpg"
        elif "gif" in header: ext = ".gif"
        elif "webp" in header: ext = ".webp"
        elif "bmp" in header: ext = ".bmp"
        # 用时间戳生成唯一文件名
        save_name = str(int(time.time() * 1000)) + ext
        img_bytes = base64.b64decode(b64data, validate=True)
        if not img_bytes or len(img_bytes) > 5 * 1024 * 1024:
            return JSONResponse({"error": {"message": "图片过大（上限5MB）"}}, status_code=400)
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name
        try:
            result = sticker_manager.save_sticker(tmp_path, save_name, tag=tag, desc=desc)
            return {"ok": True, "sticker": result}
        finally:
            import os
            try: os.unlink(tmp_path)
            except Exception: pass
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)


# 表情包由下方安全文件路由同时读取用户目录和打包内置目录。
from fastapi.staticfiles import StaticFiles as _SF

# 朋友圈素材静态路由（图片/背景）
_moment_dir = config.DATA_DIR / "朋友圈"
try:
    _moment_dir.mkdir(parents=True, exist_ok=True)
except OSError:
    _moment_dir = moments.MOMENT_DIR
app.mount("/moments_assets", _SF(directory=str(_moment_dir)), name="moments_assets")

# 角色头像持久目录：开发版和打包 EXE 都保存在可写资源目录，不放临时解压目录。
_avatar_dir = config.DATA_DIR / "character_avatars"
# StaticFiles 要求目录已存在；开发环境优先使用可写 data，受限环境则回退到已存在的 public 子目录。
try:
    _avatar_dir.mkdir(parents=True, exist_ok=True)
except OSError:
    _avatar_dir = PUBLIC / "character_avatars"
    try:
        _avatar_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        _avatar_dir = None
@app.get("/character_avatars/{filename}")
async def character_avatar_file(filename: str):
    """动态读取角色头像，避免只读/首次启动时目录尚不存在导致后端无法启动。"""
    if not re.fullmatch(r"[\w\u4e00-\u9fa5\-]+\.(?:jpg|jpeg|png|webp)", filename, re.I):
        return Response(status_code=404)
    candidates = [config.DATA_DIR / "character_avatars" / filename, PUBLIC / "character_avatars" / filename]
    for path in candidates:
        if path.is_file():
            return FileResponse(path)
    return Response(status_code=404)


@app.get("/stickers/{filename}")
async def sticker_file(filename: str):
    """Serve uploaded stickers first, then bundled read-only stickers."""
    safe = Path(str(filename or "")).name
    if safe != filename or Path(safe).suffix.lower() not in sticker_manager.ALLOWED_EXT:
        return Response(status_code=404)
    for base in (config.DATA_DIR / "表情包", config.ROOT_DIR / "表情包"):
        path = base / safe
        if path.is_file():
            return FileResponse(path)
    return Response(status_code=404)


# ---------------- 静态托管（原前端） ----------------

async def memory_cleanup_loop():
    """
    记忆生命周期管理：每24小时刷新衰减分数并清理低价值记忆。
    启动后立即执行一次，之后每86400秒循环。
    """
    while True:
        try:
            # ★ 同步函数用 run_in_executor 包装，不阻塞事件循环
            await get_loop().run_in_executor(
                None, memory_brain.refresh_memory_scores
            )
            await get_loop().run_in_executor(
                None, memory_brain.clean_low_value_memory
            )
            # ★ 孤儿向量对账：清理「记忆已作废、向量还留着」的残留。
            #   扫**全库**（不传 session_id）——历史遗留往往是很久以前留下的，
            #   按会话或按时间窗口都永远扫不到它们。实测首次运行扫出 14 条存量。
            #   limit 500/天，几天就能把历史遗留清完；delete_vector 幂等，可重复跑。
            try:
                from .memory.maintenance import reconcile_orphan_vectors
                await get_loop().run_in_executor(None, reconcile_orphan_vectors)
            except Exception as _ove:
                print(f"[MemoryCleanup] 孤儿向量对账异常: {_ove}", flush=True)
            # ★ 按时间归档 + 动态升权：不重要的记忆随时间衰减软删除，被反复想起的记忆升 importance
            try:
                from .memory.maintenance import run_archive_all
                await get_loop().run_in_executor(None, run_archive_all)
            except Exception as _arce:
                print(f"[MemoryCleanup] 全库归档异常: {_arce}", flush=True)
            print("[MemoryCleanup] 衰减扫描 + 低价值清理 + 归档 + 孤儿向量对账完成", flush=True)
        except Exception as e:
            # ★ 原来静默 pass，现在补日志方便排查
            print(f"[MemoryCleanup] 异常: {e}", flush=True)
        await asyncio.sleep(86400)


async def tts_cache_cleanup_loop():
    """TTS 音频缓存清理：每天扫一次，删掉过期/超限的音频文件。

    tts_cache/ 原先只写不删 —— 语音条、唱歌、克隆试听都会往里落文件，
    长期运行会无限增长。清理只影响「很久以前那条语音」的回放
    （历史消息里的音频 URL 会 404），不影响任何正在进行的对话，
    也不影响聊天文本本身。任何异常都只记日志，不影响主流程。
    """
    import time as _time

    await asyncio.sleep(300)   # 启动 5 分钟后再第一次扫，别拖慢启动
    while True:
        try:
            ttl_days = float(config.get("TTS_CACHE_TTL_DAYS", 7) or 7)
            max_bytes = float(config.get("TTS_CACHE_MAX_MB", 512) or 512) * 1024 * 1024
            ttl = max(1.0, ttl_days) * 86400

            entries = []
            total = 0
            for name in _os.listdir(_tts_cache_dir):
                p = _os.path.join(_tts_cache_dir, name)
                try:
                    if not _os.path.isfile(p):
                        continue
                    st = _os.stat(p)
                    entries.append((st.st_mtime, st.st_size, p))
                    total += st.st_size
                except Exception:
                    continue

            now = _time.time()
            removed = freed = 0
            # 1) 过期清理：mtime 与 atime 都超过 TTL 才算（只看 mtime 会误删
            #    刚被播放过的老语音，因为读取不更新 mtime）
            for mtime, size, p in list(entries):
                try:
                    stale = (now - mtime) > ttl
                    if stale:
                        _os.remove(p)
                        removed += 1
                        freed += size
                        total -= size
                        entries.remove((mtime, size, p))
                except Exception:
                    continue

            # 2) 超限清理：仍超上限就按最旧优先继续删
            if total > max_bytes:
                for mtime, size, p in sorted(entries, key=lambda x: x[0]):
                    if total <= max_bytes:
                        break
                    try:
                        _os.remove(p)
                        removed += 1
                        freed += size
                        total -= size
                    except Exception:
                        continue

            if removed:
                print(f"[TtsCache] 清理 {removed} 个音频，释放 "
                      f"{freed / 1048576:.1f}MB，剩余 {total / 1048576:.1f}MB",
                      flush=True)
        except Exception as e:
            print(f"[TtsCache] 清理异常(静默): {e}", flush=True)
        await asyncio.sleep(86400)


async def moments_loop():
    """
    AI 朋友圈定时任务：每 10 分钟检查一次，该发就发（价值驱动 + 频率上限）。
    失败静默，不影响主链路。
    """
    await asyncio.sleep(120)   # 启动后等2分钟再第一次检查
    while True:
        try:
            from . import moments as _m
            active = await db._run_sync(db.get_active_sessions, hours=72)
            for sess in active:
                sid = sess.get("session_id", "default")
                cid = sess.get("character_id", "default")
                mtype = _m.should_post_now(sid, cid)
                if not mtype:
                    continue
                char_name = ""
                try:
                    from .character_manager import get_character
                    char_name = (get_character(cid) or {}).get("name", "")
                except Exception:
                    pass
                moment = await _m.generate_moment(sid, cid, mtype, char_name)
                if moment:
                    _m.publish_moment(sid, cid, "ai", moment["content"], moment["images"], moment["moment_type"], "")
                    print(f"[Moments] AI 发布动态: {sid}/{cid} type={mtype}", flush=True)
        except Exception as e:
            print(f"[Moments] 循环失败(静默): {e}", flush=True)
        await asyncio.sleep(600)


async def reflection_loop():
    """
    反思定时任务：每6小时对所有活跃 session 生成反思并应用到人格。
    - 先等10分钟再跑第一次（避免启动时抢资源）
    - 生成反思 → apply_reflection_to_personality_state() 落库
    - 失败静默，不影响主链路
    """
    await asyncio.sleep(600)   # 启动后等10分钟再第一次执行
    while True:
        try:
            active_sessions = await db._run_sync(db.get_active_sessions, hours=24)
            if active_sessions:
                print(
                    f"[ReflectionLoop] 开始反思 {len(active_sessions)} 个活跃session",
                    flush=True
                )
            for sess in (active_sessions or []):
                sid = sess.get("session_id", "")
                cid = sess.get("character_id", "default")
                if not sid:
                    continue
                try:
                    # step1：生成新反思（reflection/generator.py，async 内部含 LLM 调用）
                    await _generate_reflection_safe(sid, cid)
                    # step2：将反思应用到人格状态（落库）
                    from .reflection.strategy import apply_reflection_to_personality_state
                    await db._run_sync(
                        apply_reflection_to_personality_state, sid, cid
                    )
                    print(
                        f"[ReflectionLoop] sid={sid} cid={cid} 反思已应用",
                        flush=True
                    )
                except Exception as e:
                    print(
                        f"[ReflectionLoop] sid={sid} 处理失败(静默): {e}",
                        flush=True
                    )
        except Exception as e:
            print(f"[ReflectionLoop] 外层异常: {e}", flush=True)
        await asyncio.sleep(6 * 3600)   # 每6小时


async def _generate_reflection_safe(session_id: str, character_id: str):
    """统一执行带冷却、证据校验和去重的长期反思流程。"""
    try:
        from .reflection.analyzer import update_reflection
        return await update_reflection(session_id, character_id, force=False)
    except Exception as e:
        print(f"[Reflection] 生成失败: {e}", flush=True)
        return {"triggered": False, "count": 0, "error": str(e)}


async def _maybe_trigger_reflection(session_id: str, character_id: str):
    """
    每8轮对话触发一次反思生成 + 应用。
    轮次计数用 chat_history 表的行数，不引入新状态。
    """
    try:
        count = await db._run_sync(db.count_messages, session_id, character_id)
        if count > 0 and count % 8 == 0:
            await _generate_reflection_safe(session_id, character_id)
            from .reflection.strategy import apply_reflection_to_personality_state
            await db._run_sync(
                apply_reflection_to_personality_state,
                session_id,
                character_id
            )
    except Exception as e:
        print(f"[Reflection] 轮次触发失败(静默): {e}", flush=True)


# ---------------- Session 管理（统一网页端 / 桌面端 session_id） ----------------
# 说明：原前端无 session 概念（active_session 恒为 "default"）。本接口为后续跨端
# 同步提供统一 session_id：同一 user_id + character_id 永远返回同一 session_id。

def _session_ensure_table():
    try:
        # 本项目 db.py 用 db.q(sql, args, fetch) 执行（内部 execute+commit），
        # 没有 get_connection()，故此处改用 db.q。
        db.q("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL UNIQUE,
                user_id         TEXT,
                character_id    TEXT NOT NULL DEFAULT 'default',
                device_type     TEXT,
                created_at      TEXT NOT NULL,
                last_active_at  TEXT NOT NULL,
                meta_json       TEXT DEFAULT '{}'
            )
        """)
    except Exception as e:
        print(f"[Session] 数据库初始化失败: {e}", flush=True)


class SessionRequest(BaseModel):
    user_id:      Optional[str] = None       # 用户标识（用于跨端同步）；为空则自动生成
    character_id: Optional[str] = "default"
    device_type:  Optional[str] = "web"      # web / desktop / mobile


class SessionResponse(BaseModel):
    session_id:   str
    user_id:      str
    character_id: str
    is_new:       bool


def _session_now():
    return datetime.now().isoformat()


def _generate_user_id():
    import uuid
    return f"user_{uuid.uuid4().hex[:12]}"


def _generate_session_id(user_id: str, character_id: str) -> str:
    """基于 user_id + character_id 生成确定性 session_id（同输入恒同输出）。

    ★ 2026-09-16：算法已下沉到 db.session_id_for —— db 侧要把「我的信息」
    （character_id='default'，全角色共享）归一到该用户的稳定 session，
    两处各写一份迟早对不上，索性只留一个实现。
    """
    return db.session_id_for(user_id, character_id)


@app.post("/session/get_or_create", response_model=SessionResponse)
async def session_get_or_create(body: SessionRequest):
    """
    获取或创建 session。
    传入 user_id 时，同一 user_id + character_id 永远返回同一 session_id，
    使网页端与桌面端用同一 user_id 即可共享同一 session。
    无 user_id 时自动生成持久 user_id 返回给前端存储。
    """
    try:
        now = _session_now()

        user_id = body.user_id
        if not user_id:
            user_id = _generate_user_id()

        character_id = body.character_id or "default"

        row = db.q(
            """
            SELECT session_id FROM sessions
            WHERE user_id=? AND character_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, character_id),
            fetch=True,
        )

        if row:
            session_id = row[0]["session_id"]
            db.q(
                "UPDATE sessions SET last_active_at=? WHERE session_id=?",
                (now, session_id),
            )
            return SessionResponse(
                session_id=session_id,
                user_id=user_id,
                character_id=character_id,
                is_new=False,
            )

        session_id = _generate_session_id(user_id, character_id)
        db.q(
            """
            INSERT INTO sessions
                (session_id, user_id, character_id, device_type,
                 created_at, last_active_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_id, character_id, body.device_type, now, now),
        )

        return SessionResponse(
            session_id=session_id,
            user_id=user_id,
            character_id=character_id,
            is_new=True,
        )

    except Exception as e:
        print(f"[Session] get_or_create 失败: {e}", flush=True)
        fallback_id = f"fallback_{int(datetime.now().timestamp())}"
        return SessionResponse(
            session_id=fallback_id,
            user_id=body.user_id or fallback_id,
            character_id=body.character_id or "default",
            is_new=True,
        )


@app.get("/session/history/{session_id}")
async def session_history(session_id: str, limit: int = 50, character_id: str = "", character_name: str = ""):
    """获取对话历史（取自 db.recent_messages）。"""
    try:
        cid = _resolve_char_id(character_id, character_name) if (character_id or character_name) else "default"
        rows = db.recent_messages(session_id, limit, cid)
        return {"session_id": session_id, "character_id": cid, "messages": rows or []}
    except Exception as e:
        print(f"[Session] get_history 失败: {e}", flush=True)
        return {"session_id": session_id, "messages": []}


# ═══════════════════════════════════════════════════════════
#  朋友圈（Moments）接口
# ═══════════════════════════════════════════════════════════
@app.get("/api/moments")
async def api_get_moments(session_id: str = "default", character_id: str = "", character_name: str = "", limit: int = 30, offset: int = 0):
    cid = _resolve_char_id(character_id, character_name) or "default"
    return {"moments": moments.get_moments(session_id, cid, limit, offset), "session_id": session_id, "character_id": cid}


@app.post("/api/moments")
async def api_publish_moment(request: Request):
    """用户发朋友圈（文字 + 图片 + emoji）。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    session_id = str(body.get("session_id") or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": {"message": "内容不能为空"}}, status_code=400)
    images = body.get("images") if isinstance(body.get("images"), list) else []
    mid = moments.publish_moment(session_id, character_id, "user", content, images, "user")
    # 用户朋友圈 → 提炼进长期记忆（接入记忆系统）
    moments.extract_memory_from_moment(session_id, character_id, content)
    # AI 异步阅读用户动态：点赞 + 贴内容评论 + 写入互动记忆，不阻塞发动态。
    if mid:
        asyncio.create_task(moments.react_to_user_moment(
            session_id, character_id, mid, str(body.get("character_name") or character_id),
        ))
    return {"ok": True, "id": mid, "ai_reaction_pending": bool(mid)}


@app.post("/api/moments/{moment_id}/like")
async def api_like_moment(moment_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    result = moments.like_moment(moment_id, "user", session_id, character_id)
    if result is None:
        return JSONResponse({"error": {"message": "动态不存在或不属于当前角色"}}, status_code=404)
    return {"ok": True, **result}


@app.post("/api/moments/{moment_id}/comment")
async def api_comment_moment(moment_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    content = str(body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": {"message": "内容不能为空"}}, status_code=400)
    session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    comment_id = moments.add_comment(moment_id, "user", content, session_id, character_id)
    if not comment_id:
        return JSONResponse({"error": {"message": "动态不存在或不属于当前角色"}}, status_code=404)
    # 不区分动态作者：用户评论自己的动态或 AI 的动态，AI 都会读取上下文并回复。
    try:
        ai_reply = await asyncio.wait_for(
            moments.reply_to_user_comment(
                session_id, character_id, moment_id, content,
                str(body.get("character_name") or character_id),
            ),
            timeout=20,
        )
    except Exception as exc:
        print(f"[Moments] 评论回复失败(静默): {exc}", flush=True)
        ai_reply = {}
    return {"ok": True, "comment_id": comment_id, "ai_reply": ai_reply.get("reply", "")}


@app.delete("/api/moments/{moment_id}")
async def api_delete_moment(moment_id: int, session_id: str = "default", character_id: str = "", character_name: str = ""):
    session_id = str(session_id or "default").strip() or "default"
    character_id = _resolve_char_id(character_id, character_name)
    try:
        rows = db.q("SELECT session_id, character_id, author_type FROM moments WHERE id=?", (moment_id,), fetch=True)
        if rows:
            if rows[0]["session_id"] != session_id or rows[0]["character_id"] != character_id:
                return JSONResponse({"error": {"message": "无权删除其他会话或角色的动态"}}, status_code=403)
            if rows[0]["author_type"] != "user":
                return JSONResponse({"error": {"message": "只能删除自己的动态"}}, status_code=403)
    except Exception:
        pass
    if not moments.delete_moment(moment_id, session_id, character_id):
        return JSONResponse({"error": {"message": "动态不存在"}}, status_code=404)
    return {"ok": True}


@app.post("/api/moments/upload")
async def api_moment_upload(request: Request):
    """上传朋友圈图片（base64 data URL），返回 {url}。"""
    try:
        body = await request.json()
        result = moments.upload_image(body.get("data") or "")
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)


@app.get("/api/moments/background")
async def api_get_moment_background(session_id: str = "default", character_id: str = "", character_name: str = ""):
    cid = _resolve_char_id(character_id, character_name) or active_session() or "default"
    return {"url": moments.get_background(session_id, cid)}


@app.post("/api/moments/background")
async def api_set_moment_background(request: Request):
    """上传朋友圈背景图（base64 data URL），存 URL。"""
    try:
        body = await request.json()
        result = moments.upload_image(body.get("data") or "", prefix="bg_")
        session_id = str(body.get("session_id") or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
        moments.set_background(session_id, character_id, result["url"])
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)


# ═══════════════════════════════════════════════════════════
#  AI 控制电脑接口
# ═══════════════════════════════════════════════════════════
@app.post("/api/control/execute")
async def api_control_execute(request: Request):
    """执行 AI 控制动作（白名单校验）。"""
    try:
        body = await request.json()
        action = body.get("action") if isinstance(body.get("action"), dict) else None
        if not action:
            return JSONResponse({"error": {"message": "缺少 action"}}, status_code=400)
        session_id = str(body.get("session_id") or "default").strip() or "default"
        character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    except Exception:
        return JSONResponse({"error": {"message": "请求格式错误"}}, status_code=400)
    result = await control.execute(action, session_id, character_id)
    return result


@app.get("/api/control/screen")
async def api_control_screen():
    """获取当前前台窗口（屏幕感知）。"""
    return {"info": control._screen_info()}


@app.post("/api/execution_feedback")
async def api_execution_feedback(request: Request):
    """前端上报动作执行结果（[ACTION] 电脑控制等），回喂给下一轮对话。

    ★ 执行闭环（方案 C）的最后一条腿：[ACTION] 在前端执行，前端把成功/失败
    POST 回来，execution_feedback.report 记录 → enrich_messages 下一轮回喂模型，
    纠正「说≠做」（模型光说"我帮你打开了"而实际没执行的情况）。
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "格式错误"}
    action = str(body.get("action") or "").strip() or "执行动作"
    ok = bool(body.get("ok"))
    detail = str(body.get("detail") or "")[:120]
    try:
        from . import execution_feedback as _ef
        _ef.report(action, ok, detail)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════
#  MCP 工具服务器（Kelivo 等 MCP 客户端接入，2026-09-09）
#  让手机上的 MCP 客户端直接调用后端能力：控制电脑/查记忆/查任务/
#  屏幕状态/触发 iPhone 快捷指令。streamable HTTP（POST JSON-RPC 2.0，
#  无状态最小实现）。手机侧接入指南见备忘。
# ═══════════════════════════════════════════════════════════

def _mcp_session_id() -> str:
    try:
        return active_session() or "default"
    except Exception:
        return "default"


def _mcp_tool_defs() -> list:
    char = str(config.get("QQ_CHARACTER") or "TA")
    return [
        {"name": "control_computer",
         "description": f"控制主人的电脑（Windows）：打开应用/网址、调音量、媒体控制。{char} 可以为主人代操作",
         "inputSchema": {"type": "object", "properties": {
             "actions": {"type": "array", "description": "动作列表，按顺序执行", "items": {
                 "type": "object",
                 "properties": {
                     "type": {"type": "string", "enum": ["open_app", "url", "volume", "media"],
                              "description": "open_app=打开应用 / url=打开网址 / volume=音量增减 / media=媒体控制"},
                     "app": {"type": "string", "description": "open_app：应用名（如 音乐/微信/抖音）"},
                     "url": {"type": "string", "description": "url：要打开的网址"},
                     "delta": {"type": "number", "description": "volume：音量增减（正=大，负=小）"},
                     "cmd": {"type": "string", "enum": ["toggle", "next", "prev"], "description": "media：toggle=播放暂停 / next=下一首 / prev=上一首"},
                 },
                 "required": ["type"],
             }},
         }, "required": ["actions"]}},
        {"name": "query_memory",
         "description": f"检索 {char} 与主人的长期记忆（最近生活/偏好/约定/重要事件）",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string", "description": "想检索的主题关键词（可空=返回记忆分层概览）"},
         }}},
        {"name": "screen_status",
         "description": "读取主人电脑当前的前台窗口/屏幕状态",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "query_tasks",
         "description": "查询当前待执行的定时任务/承诺（如「明天 X 点叫 TA 起床」）",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "ios_command",
         "description": "触发主人 iPhone 的快捷指令（邮件触发，延迟 1~2 分钟；用于远程控制手机侧，如打开应用/专注模式）",
         "inputSchema": {"type": "object", "properties": {
             "action": {"type": "string", "description": "要执行的指令描述（如 打开抖音/开启专注模式）"},
         }, "required": ["action"]}},
        # ── 开发者工具：助手像开发搭档一样改自己的代码 ──
        {"name": "dev_read_file",
         "description": "读项目源码文件（.py/.js/.md；禁止 data/venv/config.json 等敏感路径）",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "项目内相对路径（如 backend/onebot.py 或 public/js/chat.js）"},
         }, "required": ["path"]}},
        {"name": "dev_search_code",
         "description": "在项目源码里正则搜索（返回 文件:行号:内容）",
         "inputSchema": {"type": "object", "properties": {
             "pattern": {"type": "string", "description": "正则表达式（不分大小写）"},
             "glob": {"type": "string", "description": "文件类型过滤，如 *.py 或 *.py,*.js（默认 *.py）"},
         }, "required": ["pattern"]}},
        {"name": "dev_edit_file",
         "description": "精确替换源码内容（old 必须在文件中唯一命中；写前自动 git 快照，改坏可回滚）",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "项目内相对路径"},
             "old": {"type": "string", "description": "要被替换的原文（必须唯一）"},
             "new": {"type": "string", "description": "替换后的内容"},
             "label": {"type": "string", "description": "快照说明（如：修 XX 问题）"},
         }, "required": ["path", "old", "new"]}},
        {"name": "dev_write_file",
         "description": "整体写入/新建源码文件（写前自动 git 快照；≤200KB）",
         "inputSchema": {"type": "object", "properties": {
             "path": {"type": "string", "description": "项目内相对路径"},
             "content": {"type": "string", "description": "完整文件内容"},
             "label": {"type": "string", "description": "快照说明"},
         }, "required": ["path", "content"]}},
        {"name": "dev_run_command",
         "description": "执行白名单命令：git（add/commit/diff/log/status）、python -m py_compile、node --check、npm run build:backend",
         "inputSchema": {"type": "object", "properties": {
             "command": {"type": "string", "description": "命令全文（如 node --check public/js/chat.js）"},
             "timeout": {"type": "number", "description": "超时秒数（默认 120）"},
         }, "required": ["command"]}},
        {"name": "dev_git_status",
         "description": "查看未提交的代码改动概览（diff --stat）",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


async def _mcp_tool_call(name: str, args: dict) -> str:
    from . import control as _ctrl
    _char = str(config.get("QQ_CHARACTER") or "default")
    _sid = _mcp_session_id()
    if name == "control_computer":
        actions = args.get("actions") or []
        if not actions:
            return "没有给出动作"
        results = await _ctrl.execute_actions(actions)
        outs = []
        for a, r in zip(actions, results):
            outs.append(f"- {a.get('type')}：{'成功' if r.get('ok') else '失败（' + str(r.get('error', ''))[:60] + '）'}")
        return "已在主人电脑上执行：\n" + "\n".join(outs)
    if name == "screen_status":
        return "主人电脑当前前台窗口：" + str(_ctrl._screen_info())
    if name == "query_memory":
        from . import memory_manager as _mm
        q = str(args.get("query") or "").strip() or None
        return _mm.memory_block(session_id=_sid, character_id=_char, query=q, boost=True) \
            or "（没有检索到相关记忆）"
    if name == "query_tasks":
        from . import db as _db
        rows = _db.list_pending_tasks(session_id=_sid, character_id=_char)
        if not rows:
            return "当前没有待执行的定时任务"
        outs = []
        for r in rows[:8]:
            outs.append(f"- {r.get('trigger_time', '')}：{str(r.get('content') or '')[:60]}")
        return "待执行的任务：\n" + "\n".join(outs)
    if name == "ios_command":
        from . import ios_bridge as _ios
        _r = await _ios.send_remote_cmd(str(args.get("action") or ""), str(args.get("payload") or ""))
        return "已把指令发到主人的 iPhone（邮件触发，1~2 分钟内执行）" if _r.get("ok") else f"触发失败：{_r.get('error', '')[:80]}"
    # ── 开发者工具（助手改自己的代码；devtools 内置路径白名单/git 快照/命令白名单）──
    if name == "dev_read_file":
        from . import devtools
        return devtools.read_file(str(args.get("path") or ""))
    if name == "dev_search_code":
        from . import devtools
        return devtools.search_in_code(str(args.get("pattern") or ""), str(args.get("glob") or "*.py"))
    if name == "dev_edit_file":
        from . import devtools
        return devtools.edit_file(str(args.get("path") or ""), str(args.get("old") or ""),
                                  str(args.get("new") or ""), str(args.get("label") or ""))
    if name == "dev_write_file":
        from . import devtools
        return devtools.write_file(str(args.get("path") or ""), str(args.get("content") or ""),
                                   str(args.get("label") or ""))
    if name == "dev_run_command":
        from . import devtools
        return devtools.run_command(str(args.get("command") or ""), int(args.get("timeout") or 120))
    if name == "dev_git_status":
        from . import devtools
        return devtools.git_diff_uncommitted() + "\n\n" + devtools.restart_hint()
    return f"未知工具: {name}"


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    # ★ MCP 规范：服务端应在响应头回传协议版本；严格客户端（如 Kelivo）可能
    #   因缺失此头判定连接失败。同时接受 GET 健康探测（返回 SSE 流占位）。
    _MCP_HEADERS = {"MCP-Protocol-Version": "2024-11-05"}
    if request.method == "GET":
        return JSONResponse({"jsonrpc": "2.0", "result": {"serverInfo": {"name": "homeaime", "version": "1.0.0"}}},
                            headers=_MCP_HEADERS)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}},
                            status_code=400, headers=_MCP_HEADERS)
    method = str(body.get("method") or "")
    req_id = body.get("id")

    def _ok(result):
        # 返回 dict（由外层 JSONResponse 统一渲染 + 带协议头）
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    if method == "initialize":
        return JSONResponse(_ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "homeaime", "version": "1.0.0"},
        }), headers=_MCP_HEADERS)
    if method.startswith("notifications/"):
        return JSONResponse({}, status_code=202, headers=_MCP_HEADERS)
    if method == "tools/list":
        return JSONResponse(_ok({"tools": _mcp_tool_defs()}), headers=_MCP_HEADERS)
    if method == "tools/call":
        params = body.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            text = await _mcp_tool_call(name, args)
            return JSONResponse(_ok({"content": [{"type": "text", "text": text}], "isError": False}),
                                headers=_MCP_HEADERS)
        except Exception as e:
            return JSONResponse(_ok({"content": [{"type": "text", "text": f"执行失败：{e}"}], "isError": True}),
                                headers=_MCP_HEADERS)
    return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                         "error": {"code": -32601, "message": f"method 不支持: {method}"}},
                        status_code=200, headers=_MCP_HEADERS)


# ═══════════════════════════════════════════════════════════
#  iOS 远程桥接接口（邮件触发快捷指令 + 截屏回传）
# ═══════════════════════════════════════════════════════════
@app.post("/api/ios/command")
async def api_ios_command(request: Request):
    """发一条远程指令到 iPhone（邮件触发快捷指令）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = str(body.get("action") or "").strip()
    payload = str(body.get("payload") or "").strip()
    if not action:
        return JSONResponse({"error": {"message": "缺少 action"}}, status_code=400)
    from . import ios_bridge
    result = await ios_bridge.send_remote_cmd(action, payload)
    return result


@app.post("/api/ios/screen")
async def api_ios_screen(request: Request):
    """接收 iPhone 截屏（image_base64），视觉分析并异步回一句。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    b64 = str(body.get("image_base64") or body.get("image") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    if not session_id or not body.get("character_id"):
        try:
            from . import onebot
            if not session_id:
                session_id = onebot.qq_session_id()
            if not body.get("character_id") and not body.get("character_name"):
                character_id = onebot.qq_character()
        except Exception:
            pass
    session_id = session_id or "default"
    character_id = character_id or "default"
    if not b64:
        return JSONResponse({"error": {"message": "缺少 image_base64"}}, status_code=400)
    from . import ios_bridge
    result = ios_bridge.handle_screen_upload(b64, session_id, character_id)
    if result.get("ok"):
        asyncio.create_task(_ios_screen_reply(result.get("state") or {}, session_id, character_id))
    return result


@app.post("/api/ios/app_open")
async def api_ios_app_open(request: Request):
    """接收「打开 App」事件上报（iPhone 打开 App 自动化触发，不靠截图）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    app_id = str(body.get("app") or body.get("app_id") or "").strip()
    # ★ 对齐 QQ：快捷指令上报不带 session/character，默认挂到 QQ 角色名下，
    #   否则会存成 default，QQ 聊天时 AI 读不到「TA 在用哪个 App」。
    session_id = str(body.get("session_id") or "").strip()
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    if not session_id or not body.get("character_id"):
        try:
            from . import onebot
            if not session_id:
                session_id = onebot.qq_session_id()
            if not body.get("character_id") and not body.get("character_name"):
                character_id = onebot.qq_character()
        except Exception:
            pass
    session_id = session_id or "default"
    character_id = character_id or "default"
    if not app_id:
        return JSONResponse({"error": {"message": "缺少 app"}}, status_code=400)
    from . import ios_bridge
    result = ios_bridge.record_app_open(app_id, session_id, character_id)
    # ★ 不再触发「打开 App 主动回应」：它会和 QQ 的主动消息（idle_agent）重复/打架。
    #   上报只负责「让 AI 知道 TA 在用什么 App」，是否主动说话交给 idle_agent 统一判断。
    return result


async def _ios_screen_reply(state: dict, session_id: str, character_id: str):
    """截屏分析后，用 LLM 生成一句自然回应推 QQ/App（异步，失败静默）。"""
    try:
        from . import chat_logic, config as _cfg
        from .deepseek_api import chat_once
        summary = str((state or {}).get("summary") or "").strip()
        if not summary:
            return
        model = chat_logic.pick_model(None, True, character_id)
        key = _cfg.api_key_for_model(model)
        if not key:
            return
        prompt = (
            "用户刚把手机屏幕截图发给你看了（视觉分析结果：" + summary + "）。"
            "请用一句话自然地回应，像真的瞄了一眼 TA 手机那样：可以说说你看到 TA 在干嘛，"
            "顺便表达一下陪伴或好奇。只输出这一句，不要引号、不要解释。"
        )
        reply = await asyncio.wait_for(
            chat_once(model, [{"role": "user", "content": prompt}], key,
                      temperature=0.8, max_tokens=200), timeout=30)
        reply = str(reply or "").strip()
        if not reply:
            return
        try:
            from . import onebot
            await onebot.send_qq_message(reply)
            await onebot._push_to_app(session_id, character_id, reply, role="assistant")
        except Exception:
            pass
    except Exception as e:
        print(f"[iOS] 截屏回应失败(静默): {e}", flush=True)


async def _ios_app_open_reply(app_id: str, session_id: str, character_id: str):
    """App 打开上报后，AI 自主决定要不要主动搭话（结合记忆+上下文，节流防刷屏）。

    不做硬性「每次都说」，而是把「当前 App + 记忆 + 最近对话」交给模型，
    由它自己判断现在开口是否自然、该说什么。沉默就什么都不发。
    """
    try:
        from . import chat_logic, config as _cfg, db as _db
        from .deepseek_api import chat_once
        import time as _time

        app_id = str(app_id or "").strip().lower()
        if not app_id:
            return
        # 节流 1：同一 App 30 分钟内最多主动说一次（防频繁开同一 App 刷屏）
        _tk1 = f"ios_reply_throttle:{session_id}:{character_id}:{app_id}"
        # 节流 2：全局 5 分钟内最多主动说两次（防快速切换多个 App 刷屏，但保留一定频度）
        _tk2 = f"ios_reply_throttle_global:{session_id}:{character_id}"
        _times = []
        try:
            _last1 = _db.kv_get(_tk1)
            _now = _time.time()
            if _last1 and (_now - float(_last1)) < 30 * 60:
                return
            _last2 = _db.kv_get(_tk2)
            if _last2:
                try:
                    _times = json.loads(_last2) if isinstance(_last2, str) else (_last2 or [])
                except Exception:
                    _times = []
            _times = [t for t in _times if _now - float(t) < 5 * 60]
            if len(_times) >= 2:
                return
        except Exception:
            pass

        from . import ios_bridge
        display = ios_bridge.APP_ID_TO_NAME.get(app_id) or app_id

        # 最近对话（上下文窗口）
        try:
            recent = _db.recent_messages(session_id, limit=10, character_id=character_id)
            msgs = [
                {"role": m["role"], "content": m["content"] or ""}
                for m in recent
                if m["role"] in ("user", "assistant") and m["content"]
            ]
        except Exception:
            msgs = []

        # 长期记忆
        mem_block = ""
        try:
            mems = _db.valid_memories(session_id=session_id, character_id=character_id)
            if mems:
                mem_block = "关于用户的记忆：\n" + "\n".join(
                    f"- {str(m)[:60]}" for m in (mems[:8] if isinstance(mems, list) else [mems])
                )
        except Exception:
            pass

        model = chat_logic.pick_model(None, True, character_id)
        key = _cfg.api_key_for_model(model)
        if not key:
            return

        ctx_txt = "\n".join(
            f"{m['role']}: {str(m['content'])[:120]}" for m in msgs[-6:]
        )
        prompt = (
            f"你现在是陪伴 AI。系统刚感知到：用户打开了「{display}」。\n"
            f"请你根据下面这些信息，自己判断现在要不要主动给用户发一句消息。\n\n"
            f"{mem_block}\n"
            f"最近对话：\n{ctx_txt or '（还没有对话）'}\n\n"
            "规则：\n"
            "1. 判断「现在开口是否自然」：如果刚才你们正在热聊、或这是个合适的搭话时机，就自然说一句；"
            "如果用户明显在忙、刚说过再见、或没头没尾插话会很突兀，就保持沉默。\n"
            "2. 说话要自然、有新意、像真朋友，不要每次都提「你在刷抖音啊」这种，"
            "可以聊这个 App 相关的内容、分享、关心，或延续你们之前的话题。\n"
            "3. 决定沉默就输出空字符串。\n"
            "只输出你决定说的那句话（1 句，不超过 30 字），沉默就输出空。"
        )
        try:
            reply = await asyncio.wait_for(
                chat_once(model, [{"role": "user", "content": prompt}], key,
                          temperature=0.9, max_tokens=120), timeout=30)
        except Exception:
            return
        reply = str(reply or "").strip()
        if not reply:
            return  # 模型决定沉默

        # 记录节流（_times 已过滤到 5 分钟内，追加本次时间戳）
        try:
            _db.kv_set(_tk1, str(_time.time()))
            _times.append(_time.time())
            _db.kv_set(_tk2, json.dumps(_times))
        except Exception:
            pass

        # 推 QQ + App + 落库
        try:
            from . import onebot
            _db.add_message(session_id, "assistant", reply, character_id,
                            {"source": "ios_app_open_reply"})
            await onebot.send_qq_message(reply)
            await onebot._push_to_app(session_id, character_id, reply, role="assistant")
        except Exception:
            pass
    except Exception as e:
        print(f"[iOS] App 打开主动回应失败(静默): {e}", flush=True)


@app.get("/api/ios/config")
async def api_ios_config_get():
    """查询 iOS 远程桥接配置（密码脱敏）。"""
    try:
        cfg = config.get_all()
    except Exception:
        cfg = {}
    pwd = str(cfg.get("IOS_SMTP_PASSWORD", "") or "")
    return {
        "enabled": bool(cfg.get("IOS_REMOTE_ENABLED", False)),
        "smtp_host": cfg.get("IOS_SMTP_HOST", ""),
        "smtp_port": cfg.get("IOS_SMTP_PORT", 465),
        "smtp_user": cfg.get("IOS_SMTP_USER", ""),
        "mail_from": cfg.get("IOS_MAIL_FROM", ""),
        "mail_to": cfg.get("IOS_MAIL_TO", ""),
        "password_set": bool(pwd),
    }


@app.post("/api/ios/config")
async def api_ios_config_set(request: Request):
    """设置 iOS 远程桥接配置。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        config.update(body)
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=400)
    return {"ok": True}


# ═══════════════════════════════════════════════════════════
#  TTS 音色管理接口
# ═══════════════════════════════════════════════════════════
from .tts import get_preset_voices
from .voice_clone import upload_and_register_voice, load_cloned_voices



# ── 获取所有可用音色列表（前端下拉用）
@app.get("/api/voices")
async def api_get_voices():
    """返回所有预设音色（直接返回数组，含 key/label/tags/provider）"""
    return get_preset_voices()


# ── 上传参考音频克隆音色
@app.get("/api/call/history")
async def api_call_history(
    session_id:   str = "default",
    character_id: str = "default",
    limit:        int = 50
):
    """获取通话历史记录（按时间倒序）"""
    try:
        rows = db.q(
            """SELECT id, start_time, end_time, duration_sec, rounds,
                      call_source, call_result
               FROM call_records
               WHERE session_id=? AND character_id=?
               ORDER BY start_time DESC LIMIT ?""",
            (session_id, character_id, limit),
            fetch=True
        )
        return {
            "ok": True,
            "records": [dict(r) for r in rows] if rows else [],
            "total": len(rows) if rows else 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/voices/clone")
async def api_clone_voice(
    file:     UploadFile = File(...),
    label:    str        = Form(...),
    provider: str        = Form("gptsovits"),  # gptsovits/rvc/elevenlabs
    user_id:  str        = Form("default"),
    prompt_text: str     = Form("")
):
    audio_bytes = await file.read()
    if len(audio_bytes) > 50 * 1024 * 1024:  # 50MB 限制
        return {"success": False, "msg": "文件过大，请上传50MB以内的音频"}
    # ★ 修复：main.py 里 os 以 _os 别名导入，这里必须用 _os（原 os.path 会 NameError 500）
    ext = _os.path.splitext(file.filename or "")[-1].lower()
    if ext not in (".mp3", ".wav", ".m4a", ".ogg", ".flac"):
        return {"success": False, "msg": "仅支持 mp3/wav/m4a/ogg/flac"}
    result = await upload_and_register_voice(
        audio_bytes, file.filename or "ref.mp3",
        label=label, provider=provider, user_id=user_id,
        prompt_text=prompt_text
    )
    # ★ 音色接入向量记忆：克隆成功即存一条长期记忆（复用你已有的 yunlink_memory 通道）
    if result.get("success"):
        try:
            from .deepseek_api import chat_once
            class _VoiceLLM:
                async def chat(self, prompt):
                    _mem_model = config.memory_extract_model()
                    return await chat_once(
                        _mem_model,
                        [{"role": "user", "content": prompt}],
                        config.memory_key(), temperature=0.2, max_tokens=200,
                        reasoning_effort=("low" if config.model_supports_reasoning_effort(_mem_model) else None)
                    )
            get_loop().create_task(
                yunlink_memory.save_from_chat(
                    user_id=user_id,
                    llm=_VoiceLLM(),
                    chat=f"系统事件：用户「{user_id}」上传参考音频克隆了专属音色「{label}」（引擎:{provider}），该声音已可用于TTS发声。",
                    character_id="default"
                )
            )
        except Exception as e:
            print(f"[VoiceMemory] 克隆事件入向量库失败: {e}", flush=True)
    if result.get("success") and result.get("voice_key"):
        try:
            from .tts import get_voice_cfg
            cfg = dict(get_voice_cfg(result["voice_key"]) or {})
            if cfg:
                cfg["voice_key"] = result["voice_key"]
                result["voice"] = cfg
            if provider == "cosyvoice" and not str(cfg.get("prompt_text") or "").strip():
                result["warning"] = "未识别到参考音频原文；已保存音色，但建议填写原文后重新克隆，相似度会更高"
        except Exception:
            pass
    return result


# ── 角色绑定音色（把 voice_key 写进角色卡，支持 JSON payload）
@app.post("/api/character/voice")
async def api_set_character_voice(request: Request):
    """
    更新角色卡的音色配置。
    支持 JSON payload: {character_name: str, voice_key: str}
    兼容旧版 Form 参数: character_id, voice_key
    """
    # 尝试解析 JSON body
    try:
        body = await request.json()
        character_name = str(body.get("character_name", "") or body.get("character_id", "")).strip()
        voice_key      = str(body.get("voice_key", "")).strip()
        voice_payload   = body.get("voice") if isinstance(body.get("voice"), dict) else None
        voice_profiles = body.get("voice_profiles") if isinstance(body.get("voice_profiles"), dict) else None
        voice_style    = str(body.get("voice_style", "") or "").strip()
    except Exception:
        # 降级：从 query/form 取
        character_name = request.query_params.get("character_name", "") or request.query_params.get("character_id", "")
        voice_key      = request.query_params.get("voice_key", "")
        voice_payload   = None
        voice_profiles = None
        voice_style    = ""

    if not character_name:
        return {"ok": False, "error": "character_name 不能为空"}
    if not voice_key and voice_payload is not None:
        voice_key = str(voice_payload.get("voice_key") or "").strip()
    if not voice_key and voice_profiles is None and not voice_style:
        return {"ok": False, "error": "voice_key 不能为空"}

    # 验证 voice_key 合法性（必须在 PRESET_VOICES 里，或是克隆音色）
    from .tts import get_voice_cfg
    voice_cfg = get_voice_cfg(voice_key) if voice_key else {}
    if voice_key and not voice_cfg and not voice_key.startswith("clone_") and not voice_key.startswith("custom_"):
        return {"ok": False, "error": f"未知的 voice_key: {voice_key}"}

    # 读取角色卡现有配置，合并 voice 字段后重新保存
    try:
        from .character_manager import get_character, save_character
    except Exception as e:
        return {"ok": False, "error": f"加载character_manager失败: {e}"}

    existing = get_character(character_name)
    if not existing:
        return {"ok": False, "error": f"角色不存在: {character_name}"}

    # 把 voice_key 写入角色卡
    if voice_key:
        existing["voice"] = {"voice_key": voice_key, **(voice_cfg or {}), **(voice_payload or {})}
    else:
        current_voice = existing.get("voice") or {}
        voice_key = current_voice.get("voice_key", "") if isinstance(current_voice, dict) else str(current_voice)
    if voice_profiles is not None:
        clean_profiles = {}
        for slot in ("default", "call", "tender", "bright", "comfort", "calm"):
            value = voice_profiles.get(slot)
            if isinstance(value, str) and value.strip():
                clean_profiles[slot] = value.strip()
            elif isinstance(value, dict) and (value.get("voice_key") or value.get("voice_id")):
                clean_profiles[slot] = value
        existing["voice_profiles"] = clean_profiles
    if voice_style:
        existing["voice_style"] = voice_style
    existing["character_name"] = character_name  # 保证 save_character 能识别

    try:
        save_character(existing)
        return {
            "ok": True,
            "voice_key": voice_key,
            "voice_profiles": existing.get("voice_profiles", {}),
            "voice_style": existing.get("voice_style", "auto"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 音色试听（生成试听音频并返回 URL）
@app.post("/api/voice/preview")
async def api_voice_preview(payload: dict):
    """
    生成试听音频。
    payload: {voice_key: str, text: str, session_id: str}
    """
    voice_key = str(payload.get("voice_key", "")).strip()
    text      = str(payload.get("text", "你好，这是试听音频。")).strip()
    base_url  = ""   # 相对路径即可，前端会自动补全

    if not voice_key:
        return {"ok": False, "error": "voice_key 不能为空"}

    from .tts import get_voice_cfg, generate_audio
    voice_cfg = get_voice_cfg(voice_key)
    character_name = str(payload.get("character_name") or payload.get("character_id") or "").strip()
    if character_name:
        try:
            from .character_manager import resolve_character_voice_cfg
            role_voice = resolve_character_voice_cfg(character_name, infer=False)
            if role_voice and str(role_voice.get("voice_key") or "") == voice_key:
                voice_cfg = role_voice
        except Exception:
            pass
    if not voice_cfg:
        return {"ok": False, "error": f"未知的 voice_key: {voice_key}"}

    try:
        url = await generate_audio(text, voice_cfg, base_url)
        if url:
            return {"ok": True, "url": url}
        else:
            return {"ok": False, "error": "TTS 生成失败"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/voice/message")
async def api_voice_message(request: Request):
    """用户明确索要语音时，使用该角色当前绑定音色生成一条真正的语音消息。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    user_text = str(body.get("user_text") or "").strip()
    repeat = bool(body.get("repeat"))
    previous_text = str(body.get("previous_text") or "").strip()
    # ★ 主动消息发语音：调用方直接传入要说的文字，直接用这段文字生成语音
    content = str(body.get("content") or "").strip()
    # ★ proactive=true 表示这是"AI 主动发语音"（主动消息/离线回复），需按概率+场景+偏好判断，
    #   而不是用户明确索要（后者无条件发）。
    proactive = bool(body.get("proactive"))
    # ★ 2026-09-14 修（重要）：用户明确说「不要发语音/打字」时，必须在这里就拦住。
    #   背景（实测 bug）：用户说「宝宝你打字不要发语音」→ 前端意图正则把文本里的
    #   「发语音」当成了"索要语音"→ 打到本端点 → 而本端点**从不解析语音偏好**，
    #   回复又是下面的模板硬编码（不经模型）→ 用户刚说完别发语音就立刻收到一条语音，
    #   内容还是答非所问的「好啦，只说给宝听一次……听清楚了吗？」（#9821→#9822）。
    #   偏好解析此前只挂在 /api/chat 与 /api/chat/stream 上，唯独漏了本端点。
    if user_text and not proactive:
        try:
            from .voice_trigger import parse_voice_pref_command, set_voice_pref, _get_pref
            _pref_mode = parse_voice_pref_command(user_text)
            if _pref_mode:
                set_voice_pref(session_id, character_id, _pref_mode)
            _saved = _get_pref(session_id, character_id) or {}
            if str(_saved.get("mode") or "") == "text":
                _char_cfg_txt = {}
                try:
                    from .character_manager import get_character_any
                    _char_cfg_txt = get_character_any(character_id or "default") or {}
                except Exception:
                    pass
                _cu = str(_char_cfg_txt.get("call_user") or "你").strip() or "你"
                _txt_reply = (
                    "好，以后我多用文字跟你聊，你想听语音随时说～"
                    if _pref_mode == "text"
                    else f"嗯，{_cu}，那我打字跟你说。"
                )
                try:
                    # 用户这条消息仍要落库，否则下一轮上下文里看不到他说过这句
                    db.add_message(session_id, "user", user_text, character_id)
                    db.add_message(session_id, "assistant", _txt_reply, character_id)
                except Exception:
                    pass
                print(f"[VoiceMessage] 用户偏好=文字，改为文字回复（不生成语音）: {user_text[:24]}", flush=True)
                return {"ok": True, "text_only": True, "content": _txt_reply, "voice_pref": "text"}
        except Exception as _vp_err:
            print(f"[VoiceMessage] 语音偏好解析失败(放行发语音): {_vp_err}", flush=True)
    if proactive:
        from .voice_trigger import should_send_voice
        if not should_send_voice(session_id, character_id):
            return {"ok": True, "skipped": True, "reason": "voice_trigger"}
    try:
        # ★ 用 get_character_any：只走 load_character 的话，自建角色（配置写在
        #   legacy 角色配置/ 里）会永远读到空，call_user / personality 全部退回默认值。
        from .character_manager import get_character_any, select_character_voice_cfg
        from .tts import generate_audio, prepare_visible_text
        char_cfg = get_character_any(character_id or "default")
        call_user = str(char_cfg.get("call_user") or "你").strip() or "你"
        personality = str(char_cfg.get("personality") or "")
        if content:
            reply = content
        elif repeat and previous_text:
            reply = previous_text
        else:
            # ★ 2026-09-14 改（用户要求「禁止模板，发什么话由模型决定」）：
            #   这里原本按 personality 关键词硬套 4 条固定台词，例如
            #     「好啦，只说给{call_user}听一次……听清楚了吗？」
            #   实测后果（#9821→#9822）：用户说「不要发语音」，却收到一句
            #   **模型没参与**的答非所问台词 —— 因为模板不读上下文，只读人设关键词。
            #   现在改为**让模型按上下文生成**：带上人设、最近对话、以及用户原话，
            #   要求输出一句适合用语音说的短话。生成不出来就不发语音（宁可不发，
            #   也不发模板味的固定句）。
            reply = ""
            try:
                from .multi_turn.generator import MultiTurnGenerator as _MTG
                _base = []
                try:
                    _recent = db.recent_messages(session_id, 8, character_id) or []
                    for _m in _recent:
                        _r = str(_m.get("role") or "user")
                        _c = str(_m.get("content") or "").strip()
                        if _c:
                            _base.append({"role": ("assistant" if _r == "assistant" else "user"),
                                          "content": _c})
                except Exception:
                    _base = []
                _sys = (
                    "你是一个有真实人格的 AI 伴侣，现在要用**语音**回一句话。\n"
                    + ("【人设】" + personality[:600] + "\n" if personality else "")
                    + "【要求】\n"
                      "1. 只输出你自己的那一句话，不要任何解释、不要加引号、不要写动作描写以外的旁白；\n"
                      "2. 长度 8~40 字，口语化，像真的说出口的话；\n"
                      f"3. 你平常称呼对方为「{call_user}」，可自然使用；\n"
                      "4. 结合最近对话与对方刚才那句话的语气来回应——"
                      "对方若是提出了某个要求或说了某件事，要针对那件事回应，不要答非所问。\n"
                )
                if user_text:
                    _base = _base + [{"role": "user", "content": user_text}]
                _model = chat_logic.pick_model(None, True, character_id)
                _key = config.api_key_for_model(_model) or config.api_key()
                if _key and _model:
                    _out = await chat_once(
                        _model,
                        [{"role": "system", "content": _sys}] + _base,
                        _key, temperature=0.9, max_tokens=120,
                    )
                    reply = str(_out or "").strip().strip('“”"\'')
            except Exception as _ge:
                print(f"[VoiceMessage] 生成语音内容失败(不降级模板): {_ge}", flush=True)
                reply = ""
            if not reply:
                print("[VoiceMessage] 模型未生成内容（模板已禁用），本次不发语音", flush=True)
                return {"ok": True, "skipped": True, "reason": "no_content"}
        # ★ 括号动作描写开关：关闭时剥掉所有括号内容（语音条转写文字也跟着一起剥，
        #   否则会出现"念的是纯对话、显示的还是带括号原文"的不一致）。
        from .character_manager import get_action_brackets
        reply = prepare_visible_text(
            reply, action_brackets=get_action_brackets(character_id)
        ).strip()
        # ★ 过滤沉默内容：reply 是纯省略号/纯标点时不发语音，直接跳过。
        #   否则会把 "……" 也拿去 TTS，生成一段无声/杂音的语音条，
        #   表现为「AI 主动发三个点 + 一条空语音」。
        from .emotion_engine.ai_emotion import is_silence
        if is_silence(reply):
            print("[VoiceMessage] 内容为沉默(纯省略号)，跳过语音发送", flush=True)
            return {"ok": True, "skipped": True, "reason": "silence"}
        # ★ 话术去重：语音条此前完全没有去重，是"重复话术修了仍复现"的主因之一。
        #   用户明确要求「再说一遍」(repeat) 时不拦——那就是他要的重复。
        if not repeat:
            try:
                from .companion.quality_guard import dedup_check_async, recent_ai_texts
                from .deepseek_api import chat_once as _v_dedup_chat_once
                _v_key = config.api_key()
                _v_model = chat_logic.pick_model(None, True, character_id)
                if _v_key and await dedup_check_async(
                    reply, session_id, character_id,
                    chat_once_fn=_v_dedup_chat_once,
                    model=_v_model, api_key=_v_key,
                ):
                    _said = recent_ai_texts(session_id, character_id, 5)
                    _new = await _dedup_regenerate(
                        [{"role": "system", "content": personality[:400] or "你是用户的恋人。"},
                         {"role": "user", "content": user_text}],
                        session_id, character_id, _said,
                        _v_dedup_chat_once, _v_model, _v_key,
                    )
                    if _new:
                        reply = prepare_visible_text(
                            _new, action_brackets=get_action_brackets(character_id)
                        ).strip()
                    else:
                        print("[VoiceMessage] 去重重新生成未通过二次验证，保留原内容", flush=True)
            except Exception as _vde:
                print(f"[VoiceMessage] 去重失败(静默): {_vde}", flush=True)
        voice_cfg = select_character_voice_cfg(
            character_id or "default", emotion="tender", mode="chat"
        )
        if not voice_cfg:
            return JSONResponse({"ok": False, "error": "这个角色还没有配置可用音色"}, status_code=400)
        audio_url = await generate_audio(
            reply, voice_cfg, base_url=str(request.base_url).rstrip("/")
        )
        if not audio_url:
            return JSONResponse({"ok": False, "error": "语音生成失败，请检查角色音色或 TTS 服务"}, status_code=500)
        try:
            # ★ 主动语音（proactive/content 模式）没有真实用户消息，不落 user 记录；
            #   否则默认值「想听你发条语音」会被当成用户消息写进历史，AI 误以为用户说了这句。
            if user_text:
                db.add_message(session_id, "user", user_text, character_id)
            db.add_message(session_id, "assistant", reply, character_id, extra={
                "source": "voice_message", "audio": audio_url,
            })
            if user_text:
                from . import conv_state
                conv_state.update_conv_state(session_id, character_id, user_text, reply)
        except Exception:
            pass
        # ★ QQ 打通：AI 语音消息同步推送到 QQ（优先发语音条+文字；语音发送失败回退纯文字）
        #   ★ 2026-09-11 修：**主动语音不要再推 QQ**。
        #     主动消息的文字已经由 scheduler._deliver 推过一次 QQ 了；这里再推一次
        #     send_qq_voice(reply, audio_url)（语音条 + 同内容文字），用户就会同一句话收到两遍
        #     —— 实测 17:02:29 一条 proactive 文字，17:02:49 又来了 voice_message+audio。
        #     用户明确索要的语音（user_text 非空）照旧同步推 QQ。
        if not proactive:
            try:
                from . import onebot
                _voice_ok = await onebot.send_qq_voice(reply, audio_url)
                if not _voice_ok:
                    await onebot.send_qq_message(reply)
            except Exception as _qq_e:
                print(f"[VoiceMessage] 推QQ失败(静默): {_qq_e}", flush=True)
        duration = max(1, min(60, round(len(reply) / 4.2)))
        return {"ok": True, "content": reply, "audio": audio_url,
                "duration": duration, "character_id": character_id}
    except Exception as e:
        print(f"[VoiceMessage] 生成失败: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/sing")
async def api_sing(request: Request):
    """文字聊天唱歌：检测唱歌意图 → 有歌名生成唱歌音频，无歌名问想听什么。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    session_id = str(body.get("session_id") or active_session() or "default").strip() or "default"
    character_id = _resolve_char_id(body.get("character_id"), body.get("character_name"))
    user_text = str(body.get("user_text") or "").strip()

    try:
        from .singing.intent_detector import detect_sing_intent
        from .singing.lyrics_fetcher import fetch_lyrics
        from .singing.singing_engine import sing_lyrics_stream
        from .character_manager import resolve_character_voice_cfg

        intent = detect_sing_intent(user_text)
        if not intent.is_sing:
            return JSONResponse({"ok": False, "error": "不是唱歌请求"}, status_code=400)

        song_name = str(body.get("song_name") or intent.song_name or "").strip()
        if not song_name:
            return {"ok": True, "needs_song_name": True,
                    "reply": "想听什么歌呀？报个歌名，我这就唱给你听～"}

        lyrics = await fetch_lyrics(song_name, intent.artist, intent.style)
        if not lyrics or not lyrics.get("lines"):
            return {"ok": False, "error": f"《{song_name}》的歌词我想不起来了，换个歌名试试？"}

        try:
            voice_cfg = resolve_character_voice_cfg(character_id or "default", infer=True)
        except Exception:
            voice_cfg = None

        audio_parts = []
        lines_out = []
        # ★ 用角色音色逐句"念"歌词（CosyVoice zero-shot），不用 instruct2 唱歌——
        #   instruct2 会把情感提示词"声音明亮咬字清晰"当内容朗读出来。
        from .tts import generate_audio, _CACHE_DIR
        import os as _os_local
        # ★ 先唱一小段（最多7句），够听又不会等太久；重复副歌命中缓存更快
        _sing_lines = lyrics["lines"][:7]
        for _line in _sing_lines:
            _line = (_line or "").strip()
            if not _line:
                continue
            try:
                _url = await generate_audio(_line, voice_cfg, "")
                if _url:
                    _fname = _os_local.path.basename(_url.split("?")[0])
                    _local = _os_local.path.join(_CACHE_DIR, _fname)
                    if _os_local.path.exists(_local):
                        with open(_local, "rb") as _f:
                            audio_parts.append(_f.read())
                        lines_out.append(_line)
            except Exception:
                continue

        if not audio_parts:
            return {"ok": False, "error": "唱歌合成失败，稍后再试"}

        # 合并 wav：读第一段采样率，去掉后续各段 44 字节头后拼接 PCM
        import wave, io, hashlib, os as _os
        first = audio_parts[0]
        sr = 24000
        try:
            with wave.open(io.BytesIO(first), "rb") as wf:
                sr = wf.getframerate() or 24000
        except Exception:
            pass
        merged = io.BytesIO()
        with wave.open(merged, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            for p in audio_parts:
                wf.writeframes(p[44:])
        merged_bytes = merged.getvalue()

        from .tts import _CACHE_DIR
        key = hashlib.md5(f"sing:{song_name}:{character_id}".encode("utf-8")).hexdigest()[:16]
        out_file = _os.path.join(_CACHE_DIR, f"sing_{key}.wav")
        with open(out_file, "wb") as f:
            f.write(merged_bytes)

        return {"ok": True, "song_name": lyrics.get("song_name", song_name),
                "audio": f"/tts_cache/sing_{key}.wav",
                "lines": lines_out,
                "duration": max(1, len(merged_bytes) // (sr * 2))}
    except Exception as e:
        print(f"[Sing] 唱歌失败: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════
#  歌曲库：AI 学歌（导入 / 列表 / 学习 / 删除 / 试听）
# ═══════════════════════════════════════════════════════════

# RVC 歌声服务地址（独立进程，见 %RVC_HOME%\server.py，端口 9882）
RVC_SERVICE = os.environ.get("RVC_SERVICE_URL", "http://127.0.0.1:9882")
RVC_MODEL = os.environ.get("RVC_MODEL", "guzi")

# 学歌各步骤的超时上限（秒）。设限是为了不让 RVC 端卡住时主后端无限等待——
# 超时会把这首歌标记成失败并给出原因，用户可以重试，而不是界面永远转圈。
# 参考实测：3 分钟歌曲分离约 60s、换声约 60s、合流数秒，这里留足 5~15 倍余量。
_SONG_TIMEOUT = {
    "separate": 900,   # 分离最慢（GPU 显存紧张时会明显变慢）
    "convert":  600,
    "render":   300,
}


async def _learn_song_task(song_id: str):
    """后台学习一首歌：分离人声/伴奏 → RVC 换声 → 合流。

    拆成三次调用而不是一次 /process：/process 会把三步一口气做完再返回，
    期间前端只能干等且一直显示"分离中"。分步调用每一步都能把阶段写回索引，
    用户就能看到「分离中 → 学唱中 → 合流中」在动。

    服务没启动、显存不足、模型还没训练完都会失败，
    这里一律只把状态置成 failed，绝不影响主程序其它功能。
    """
    try:
        from .singing import song_library
        import httpx
        import shutil
    except Exception:
        return
    song = song_library.get_song(song_id)
    if not song:
        return

    work_dir = song_library.song_dir(song_id)
    os.makedirs(work_dir, exist_ok=True)
    sep_dir   = os.path.join(work_dir, "separated")
    vocals    = os.path.join(work_dir, "vocals.wav")
    accomp    = os.path.join(work_dir, "accompaniment.wav")
    ai_vocals = os.path.join(work_dir, "ai_vocals.wav")
    final     = os.path.join(work_dir, "final.mp3")

    def _stage(status, stage, progress, **extra):
        song_library.update_song(
            song_id, status=status, stage=stage, progress=progress,
            error="", **extra)

    def _json_of(resp):
        ctype = str(resp.headers.get("content-type") or "")
        return resp.json() if "application/json" in ctype else {}

    try:
        # 「重新学习」可复用上次已分离的音轨，省掉最耗时的一步
        reuse = (bool(song.get("skip_separate"))
                 and os.path.isfile(vocals) and os.path.isfile(accomp))

        # 每步单独设超时：以前是一个 3600s 的大锁，RVC 端一旦卡住
        # 主后端就只能干等一小时，前端永远停在"分离中"。
        # httpx 要求 Timeout 要么给 default、要么四个参数显式给全；
        # 只给 connect 会在构造 AsyncClient 时抛 ValueError，导致还没开始分离就失败。
        # 这里只约束连接阶段，读取超时由下面每次 post 的 timeout= 单独指定。
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None)
        ) as client:
            # 1. 分离人声 / 伴奏（整首歌里最慢的一步）
            if reuse:
                _stage("separating", "复用已分离音轨", 20,
                       vocals_path=vocals, accompaniment_path=accomp)
            else:
                _stage("separating", "分离人声与伴奏", 8)
                r = await client.post(
                    f"{RVC_SERVICE}/separate",
                    json={"input": song.get("original_path") or "",
                          "output_dir": sep_dir},
                    timeout=_SONG_TIMEOUT["separate"],
                )
                data = _json_of(r)
                if r.status_code != 200 or not data.get("ok"):
                    raise RuntimeError(f"分离失败：{str(data)[:200]}")
                shutil.copy2(data["vocals"], vocals)
                shutil.copy2(data["accompaniment"], accomp)
                _stage("separating", "分离完成", 35,
                       vocals_path=vocals, accompaniment_path=accomp)

            # 2. 换声：把原唱人声换成她的音色
            _stage("converting", "用她的音色学唱", 45)
            r = await client.post(
                f"{RVC_SERVICE}/convert",
                json={"input": vocals, "output": ai_vocals,
                      "model": RVC_MODEL, "pitch": 0},
                timeout=_SONG_TIMEOUT["convert"],
            )
            data = _json_of(r)
            if r.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"换声失败：{str(data)[:200]}")
            _stage("converting", "学唱完成", 70, ai_vocals_path=ai_vocals)

            # 3. 合流：AI 人声 + 伴奏
            _stage("rendering", "合并伴奏", 82)
            r = await client.post(
                f"{RVC_SERVICE}/render",
                json={"vocals": ai_vocals, "accompaniment": accomp,
                      "output": final},
                timeout=_SONG_TIMEOUT["render"],
            )
            data = _json_of(r)
            if r.status_code != 200 or not data.get("ok"):
                raise RuntimeError(f"合流失败：{str(data)[:200]}")

        song_library.update_song(
            song_id,
            status="ready", stage="已学会", progress=100,
            vocals_path=vocals, accompaniment_path=accomp,
            ai_vocals_path=ai_vocals, final_path=final, error="",
        )
        print(f"[Songs] 《{song.get('title')}》学习完成", flush=True)
    except Exception as e:
        # 超时类异常（httpx.ReadTimeout/ConnectTimeout）的 str() 常常是空的，
        # 只写类名的话界面上就是「失败」俩字，用户完全不知道发生了什么。
        _ename = type(e).__name__
        _detail = str(e).strip()
        if not _detail:
            _detail = (_ename + f"（超过 {max(_SONG_TIMEOUT.values())}s 未完成，"
                       "多半是显存被其他程序占满，关掉占显卡的软件后重试）"
                       if "Timeout" in _ename else _ename)
        else:
            _detail = f"{_ename}: {_detail}" if not _detail.startswith(_ename) else _detail
        print(f"[Songs] 学习失败: {_detail}", flush=True)
        try:
            from .singing import song_library as _sl
            _sl.update_song(
                song_id, status="failed", stage="学习失败",
                error=_detail[:300])
        except Exception:
            pass


@app.get("/api/songs/service/status")
async def api_rvc_status():
    """RVC 歌声服务是否可用（供 UI 提示"学歌功能当前不可用"）。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{RVC_SERVICE}/health")
            return {"ok": True, "service": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e), "service_url": RVC_SERVICE}


@app.get("/api/songs")
async def api_songs_list(session_id: str = "", character_id: str = ""):
    """列出「AI 学会的歌」。"""
    try:
        from .singing import song_library
        _sid = str(session_id or active_session() or "").strip()
        _cid = _resolve_char_id(character_id, "")
        songs = song_library.list_songs(_sid, _cid)
        return {"ok": True, "songs": [song_library.public_view(s) for s in songs]}
    except Exception as e:
        print(f"[Songs] 列表失败: {e}", flush=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/songs/import")
async def api_songs_import(
    file: UploadFile = File(...),
    title: str = Form(""),
    artist: str = Form(""),
    session_id: str = Form(""),
    character_id: str = Form(""),
    auto_learn: bool = Form(True),
):
    """导入一首完整歌曲（后续用于分离人声/伴奏 + RVC 换声）。"""
    try:
        from .singing import song_library
        data = await file.read()
        if not data:
            return JSONResponse({"ok": False, "error": "文件为空"}, status_code=400)
        # 60MB 上限：整首歌通常 3~10MB，留足余量同时挡住误传的录音文件
        if len(data) > 60 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "文件超过 60MB"}, status_code=400)
        ext = (os.path.splitext(file.filename or "")[1] or ".mp3").lstrip(".").lower()
        name = str(title or "").strip() or os.path.splitext(file.filename or "未命名")[0]
        song = song_library.add_song(
            title=name, file_bytes=data, ext=ext, artist=str(artist or "").strip(),
            session_id=str(session_id or active_session() or "").strip(),
            character_id=_resolve_char_id(character_id, ""),
        )
        print(f"[Songs] 导入《{name}》 {len(data) / 1048576:.1f}MB", flush=True)
        # 后台触发学习（分离 → 换声 → 合流）。服务没启动也只是停在 separating，
        # 等 9882 起来后可以在面板点「重新学习」补跑。
        if auto_learn:
            try:
                asyncio.create_task(_learn_song_task(song.get("id")))
            except Exception as _le:
                print(f"[Songs] 学习任务启动失败(静默): {_le}", flush=True)
        return {"ok": True, "song": song_library.public_view(song)}
    except Exception as e:
        print(f"[Songs] 导入失败: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/songs/{song_id}/learn")
async def api_song_learn(song_id: str, skip_separate: bool = False):
    """手动触发学习：服务之前没开 / 模型更新后重新学。"""
    try:
        from .singing import song_library
        if not song_library.get_song(song_id):
            return JSONResponse({"ok": False, "error": "歌曲不存在"}, status_code=404)
        song_library.update_song(song_id, skip_separate=skip_separate)
        asyncio.create_task(_learn_song_task(song_id))
        return {"ok": True, "message": "已开始学习"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/songs/{song_id}/cancel")
async def api_song_cancel(song_id: str):
    """中止学习：把卡在 separating/converting/rendering 的歌退回可重试状态。

    注意这只能改主后端这边的状态标记，已经发给 RVC 的请求无法撤回；
    RVC 端跑完后会再写一次结果，所以取消后请删除该曲重新导入。
    """
    try:
        from .singing import song_library
        song = song_library.get_song(song_id)
        if not song:
            return JSONResponse({"ok": False, "error": "歌曲不存在"}, status_code=404)
        if song.get("status") not in ("separating", "converting", "rendering"):
            return JSONResponse({"ok": False, "error": "该歌曲没有在学习中"},
                                status_code=400)
        song_library.update_song(
            song_id, status="imported",
            stage="已中止（可重新学习）", progress=0, error="")
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/api/songs/{song_id}")
async def api_songs_delete(song_id: str):
    """删除一首歌及其全部中间产物。"""
    try:
        from .singing import song_library
        if not song_library.delete_song(song_id):
            return JSONResponse({"ok": False, "error": "歌曲不存在"}, status_code=404)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/songs/{song_id}/audio")
async def api_song_audio(song_id: str, kind: str = "original"):
    """试听音轨：original=原曲 / accompaniment=伴奏 / ai_vocals=AI 人声。"""
    try:
        from .singing import song_library
        from fastapi.responses import FileResponse
        song = song_library.get_song(song_id)
        if not song:
            return JSONResponse({"ok": False, "error": "歌曲不存在"}, status_code=404)
        key = {
            "original":      "original_path",
            "vocals":        "vocals_path",
            "accompaniment": "accompaniment_path",
            "ai_vocals":     "ai_vocals_path",
            "final":         "final_path",
        }.get(str(kind or "original"), "original_path")
        path = song.get(key) or ""
        if not path or not os.path.exists(path):
            return JSONResponse({"ok": False, "error": "该音轨尚未生成"}, status_code=404)
        media = "audio/wav" if str(path).lower().endswith(".wav") else "audio/mpeg"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "no-store"})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════
#  分段气泡 SSE 接口  POST /api/chat/stream
# ═══════════════════════════════════════════════════════════
import asyncio as _asyncio
import json as _json
from fastapi.responses import StreamingResponse as _StreamingResponse

# 懒加载单例
_ai_emotion_engine = None
_multi_turn_gen    = None

def _get_emotion_engine():
    global _ai_emotion_engine
    if _ai_emotion_engine is None:
        try:
            from .emotion_engine.ai_emotion import AIEmotionEngine
            _ai_emotion_engine = AIEmotionEngine()
            print("[Stream] AIEmotionEngine 加载成功", flush=True)
        except Exception as e:
            print(f"[Stream] AIEmotionEngine 加载失败: {e}", flush=True)
    return _ai_emotion_engine

def _get_multi_turn():
    global _multi_turn_gen
    if _multi_turn_gen is None:
        try:
            from .multi_turn.generator import MultiTurnGenerator
            _multi_turn_gen = MultiTurnGenerator()
            print("[Stream] MultiTurnGenerator 加载成功", flush=True)
        except Exception as e:
            print(f"[Stream] MultiTurnGenerator 加载失败: {e}", flush=True)
    return _multi_turn_gen


async def _maybe_ai_quote(messages, model: str, key: str):
    """AI 主动引用：用户连发多条消息时，让模型挑一条针对性回应。
    messages 为前端传来的历史数组（含当前这条 user 在末尾）。
    返回 {'role':'user','content':..., 'name':'我'} 或 None。任何失败静默返回 None。"""
    import re as _re
    # 提取「末尾连续的 user 消息」：一旦遇到 assistant/system 就停止往前
    users = []
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        _role = m.get("role")
        _content = m.get("content")
        if _role == "user" and _content:
            users.insert(0, str(_content).strip())
        elif _role in ("assistant", "system"):
            break
    users = [u for u in users if u]
    # ★ 触发门槛：用户连发 >=2 条就考虑引用（2 条连发也常需要区分回应）。
    if len(users) <= 1:
        return None
    # ★ 最多只看最近 8 条（足够覆盖连发场景，避免 prompt 过长）。
    #   关键：编号 1..N 严格对应这 N 条，不做 [-4:] 切片——
    #   旧代码切片后"模型选 3 实际拿到第 5 条"就是引用错乱的根因。
    users = users[-8:]
    if not key or not model:
        return None
    try:
        from .deepseek_api import chat_once
        numbered = "\n".join(f"{i+1}. {u[:120]}" for i, u in enumerate(users))
        prompt = (
            f"用户刚刚连续发了 {len(users)} 条消息，你准备一次性回应。\n"
            "下面是这几条消息（按先后顺序编号：1 是最早那条，"
            f"{len(users)} 是最后那条）：\n" + numbered + "\n\n"
            "请判断：其中有没有**重要到值得你专门、单独回应**的事（重要通知 / 紧急请求 / "
            "关键情绪 / 承诺或约定 / 重要的提问 / 约好的时间地点 等）。\n"
            "如果有，只输出那一条的编号数字（例如 2）；如果都是日常寒暄、随便聊聊、"
            "不重要的小事（比如「在吗」「早安」「想你了」「今天好热」这类），"
            "就只输出 0，不要为了凑数硬选一条。\n"
            "判断标准：只有当某条消息重要到需要被特别指出并单独回应时才引用，"
            "普通的连发闲聊不要逐条引用。\n"
            "只输出一个数字（0 或 1~" + str(len(users)) + "），不要任何解释。"
        )
        raw = await asyncio.wait_for(
            chat_once(model, [{"role": "user", "content": prompt}], key,
                      temperature=0.4, max_tokens=8),
            timeout=15,
        )
    except Exception:
        return None
    m = _re.search(r"\d+", str(raw or ""))
    if not m:
        return None
    idx = int(m.group(0))
    if idx <= 0 or idx > len(users):
        return None
    return {"role": "user", "content": users[idx - 1][:200], "name": "我"}


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    """
    分段气泡 SSE 接口。
    · 前端用 fetch + ReadableStream 接收（不用 EventSource，因为要 POST）
    · 每条 event: data: {...}\\n\\n
    · type: typing | message | done | error
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    session_id   = str(body.get("session_id",   "default")).strip() or "default"
    # ★ 统一人格隔离键（与 /api/chat 一致）：前端可能传 UUID，用 character_name 兜底解析成角色名
    character_id = _resolve_char_id(
        body.get("character_id"), body.get("character_name")
    )
    user_text    = str(body.get("message") or body.get("content") or "").strip()
    is_internal_generation = bool(
        body.get("proactive_internal")
        or body.get("skip_user_persist")
        or body.get("internal_user_prompt")
    )
    # ★ 2026-09-14 埋点：记录本回合的用户指令信号（"别发语音/记住…/说错了"）。
    #   用途：事后用 trace 报告核对"用户明确说了 X，AI 有没有照做"，无需再翻聊天记录。
    try:
        from . import trace as _trace
        _instr_kinds = [] if is_internal_generation else _trace.detect_instr(user_text)
        if _instr_kinds:
            _trace.instr(session_id, character_id, user_text, _instr_kinds)
    except Exception:
        _instr_kinds = []
    # 埋点用的回合级状态（缺一个都会让 turn 快照失真，这里一次性备齐）
    _turn_t0 = time.time()
    _p_sys = 0
    _p_blocks = 0
    _all_turns_payload = []          # 本回合推送出去的 payload（用于判断有没有发语音/表情）
    _turn_meta = {"audio": False, "sticker": False}   # 埋点累积（_process_turn 写入）
    try:
        from . import sticker_manager as _sticker_manager
    except Exception:
        _sticker_manager = None
    try:
        _prompt_compact_flag = bool(config.prompt_compact())
        _understanding_on_flag = bool(config.understanding_enabled())
    except Exception:
        _prompt_compact_flag = None
        _understanding_on_flag = None

    # ★ 引用消息（像微信"回复"）：前端右键引用某条历史消息时透传的原文快照
    reply_to = body.get("reply_to")
    if isinstance(reply_to, dict):
        reply_to = {
            "role": str(reply_to.get("role") or "user"),
            "content": str(reply_to.get("content") or "").strip()[:200],
            "name": str(reply_to.get("name") or ""),
        }
        if not reply_to["content"]:
            reply_to = None
    else:
        reply_to = None

    # ★ 同 /api/chat：把前端传来的 Key 注入运行时（优先级最低）。
    #   本入口是分段气泡主链路，语义分析 / 记忆抽取都靠它才能拿到 Key。
    try:
        config.set_runtime_api_key(str(body.get("key") or "").strip())
    except Exception:
        pass

    # ★ 多session v2.0：确保该 session 有对应调度器
    try:
        scheduler.scheduler.get_or_create(session_id, character_id)
    except Exception:
        pass

    # ★ 世界轻推：桌面端上报城市 → 存 relationship_state（UPSERT 防御无行）
    _city = body.get("city")
    if _city:
        try:
            from .relationship.database import conn as rel_conn
            rc = rel_conn()
            rc.execute("INSERT OR IGNORE INTO relationship_state(user_id, character_id) VALUES(?,?)",
                       (session_id, character_id))
            rc.execute("UPDATE relationship_state SET city=? WHERE user_id=? AND character_id=?",
                       (str(_city), session_id, character_id))
            rc.commit(); rc.close()
        except Exception as e:
            print(f"[City-stream] 存失败: {e}", flush=True)

    if not user_text:
        async def _err():
            yield f"data: {_json.dumps({'type':'error','content':'消息不能为空'}, ensure_ascii=False)}\n\n"
        return _StreamingResponse(_err(), media_type="text/event-stream")

    # ★ 第五步补丁：stream 出口补视觉分支（顶层处理，与 /api/chat 一致直接 return StreamingResponse）
    character_name = body.get("character_name") or character_id
    raw_messages = body.get("messages")
    if isinstance(raw_messages, list) and raw_messages:
        messages = raw_messages
    else:
        messages = [{"role": "user", "content": user_text}]
    last_user_message = None
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_message = m
            break
    has_image = bool(last_user_message and last_user_message.get("image"))
    # ★ 动作描写开关（前端可关）：解析 action_brackets，构造请求级 style 覆盖。
    #   必须定义在 if has_image 之前——原先放在图片分支内，非图片路径下 _style_ov 未定义，
    #   导致后面的多段后处理拿不到开关，括号描写剥不掉。
    _ab = body.get("action_brackets")   # 前端传 true/false，不传则 None（走默认/角色json）
    _style_ov = {"action_brackets": _ab} if isinstance(_ab, bool) else None

    # ★ 学习信号（风格反馈 + 规矩沉淀）统一入口 —— 见 run_learning_signals 的说明。
    #   ★ 2026-09-17 修（重大接线错误）：这段原先在 `if has_image:` 分支**里面**
    #     （AST 取证：原 main.py:8156 被 has_image 包着），而它是全项目**唯一**的
    #     调用点 —— 于是纯文字聊天**永远不学习风格偏好**。症状：用户反复批评
    #     "别讲道理/别敷衍"，AI 行为毫无变化；日志里 `[StyleFeedback]` 命中 0 次、
    #     kv 里 `style_feedback:*` 0 个；`learned_rules/助手.json` 恒为 `[]`。
    #     现在提到 has_image 之前，且文字/图片两条路径共用同一个入口。
    await run_learning_signals(session_id, character_id or "default", user_text or "")

    if has_image:
        vkey      = (str(body.get("visionKey") or "").strip()) or config.vision_key()
        vmodel    = (str(body.get("visionModel") or "").strip()) or config.vision_model()
        vprovider = (str(body.get("visionProvider") or "").strip())
        vbase_url = (str(body.get("visionBaseUrl") or "").strip())
        # 前端没传 provider/base_url 时，从模型池配置兜底（同 /api/chat）
        if not vprovider or not vbase_url:
            try:
                vision_cfg = config.get_vision_model_config(vmodel)
                if not vprovider:
                    vprovider = vision_cfg.get("provider") or config.vision_provider()
                if not vbase_url:
                    vbase_url = vision_cfg.get("baseUrl") or config.vision_base_url()
            except Exception:
                if not vprovider:
                    vprovider = config.vision_provider()
                if not vbase_url:
                    vbase_url = config.vision_base_url()
        if not vkey:
            async def _err_vk():
                yield f"data: {_json.dumps({'type':'error','content':'图片消息需要视觉模型 Key：请在 App 设置页「视觉模型」填写，或在电脑 config.json 配置 vision_api_key'}, ensure_ascii=False)}\n\n"
            return _StreamingResponse(_err_vk(), media_type="text/event-stream")
        # ★ 图片预处理：超大图压缩（防止打爆token）
        _raw_image = last_user_message.get("image", "")
        try:
            from .multimodal.vision import _normalize_image
            _processed_image = _normalize_image(_raw_image)
            if _processed_image:
                last_user_message["image"] = _processed_image
        except Exception:
            _processed_image = _raw_image

        # ★ 图片描述存记忆（异步非阻塞，不影响主流程）
        async def _save_image_desc_memory():
            try:
                from .multimodal.vision import analyze_image_with_vision_model
                from .memory.manager import MemoryManager
                _desc = await analyze_image_with_vision_model(
                    _processed_image,
                    prompt=(
                        "用一句话描述这张图片的核心内容，"
                        "格式：用户发来一张[图片内容描述]的图片。"
                        "20字以内。"
                    ),
                    api_key=vkey,
                    base_url=vbase_url,
                    model=vmodel
                )
                if _desc:
                    MemoryManager().add_memory(
                        user_id=session_id,
                        memory_type="image",
                        content=_desc,
                        importance=4,
                        character_id=character_id
                    )
            except Exception as _sme:
                print(f"[ImageMemory] 存记忆失败: {_sme}", flush=True)
        get_loop().create_task(_save_image_desc_memory())

        enriched = await enrich_messages(
            messages, session_id, character_name,
            style_override=_style_ov,
            sticker=bool(body.get("sticker")),
        )

        # ★ 风格反馈学习已在 `if has_image:` **之前**统一处理（2026-09-17 上移）。
        #   这里有意的空位：不要再把学习逻辑放回图片分支里 —— 那会让纯文字聊天
        #   永远学不到"别讲道理/别敷衍"这类批评。

        # ★ 语义理解层：stream 的图片分支。key/model/base_url 在该分支尚未解析，
        #   交给理解层自行从配置读取。
        try:
            from .understanding import analyze_and_inject
            enriched = await analyze_and_inject(
                enriched, user_text,
                session_id=session_id, character_id=character_id,
                character_name=character_name or "",
                key=str(body.get("key") or "").strip(),
                is_internal=is_internal_generation,
            )
        except Exception as _ue:
            print(f"[Understanding] stream(图片) 注入失败(静默): {_ue}", flush=True)

        # ★ 看图摘要入向量记忆（出口B，变量沿用该分支已有 vkey/vmodel/vbase_url/vprovider）
        _img = last_user_message.get("image")
        _txt = user_text
        async def _img_mem_b():
            _sum = await _vision_summary_nonstream(_img, _txt, vkey, vmodel, vbase_url, vprovider)
            if _sum:
                try:
                    class _MLLM:
                        async def chat(self, p):
                            _mem_model = config.memory_extract_model(character_id)
                            return await chat_once(_mem_model, [{"role":"user","content":p}], config.memory_key(), temperature=0.2, max_tokens=200, reasoning_effort=("low" if config.model_supports_reasoning_effort(_mem_model) else None))
                    await yunlink_memory.save_from_chat(user_id=session_id, llm=_MLLM(),
                        chat=f"用户发图：「{_txt or '无配文'}」→ 图内容：{_sum}（已存视觉记忆）",
                        character_id=character_id)
                except Exception as e:
                    print(f"[ImgMem-B] 入向量失败: {e}", flush=True)
        get_loop().create_task(_img_mem_b())

        # 与 /api/chat 一致：把 stream_vision 的原始 line 包成前端 stream 模式可解析的 SSE
        provider_lower = vprovider.lower()
        use_silicon = provider_lower == "siliconflow" or "silicon" in provider_lower

        async def vision_gen():
            try:
                if use_silicon:
                    async for kind, val in stream_vision_silicon(enriched, vkey, vmodel):
                        if kind == "line":
                            yield val + "\n\n"
                        elif kind == "done":
                            yield "data: [DONE]\n\n"
                        elif kind == "meta":
                            yield _sse_line(val)
                else:
                    async for kind, val in stream_vision(enriched, vkey, vmodel, base_url=vbase_url or None):
                        if kind == "line":
                            yield val + "\n\n"
                        elif kind == "done":
                            yield "data: [DONE]\n\n"
                        elif kind == "meta":
                            yield _sse_line(val)
            except ModelApiError as e:
                yield _sse_line({"error": {"message": str(e)}})

        return _StreamingResponse(vision_gen(), media_type="text/event-stream")

    async def event_stream():
        try:
            # 1. 「正在输入」信号
            yield f"data: {_json.dumps({'type':'typing'}, ensure_ascii=False)}\n\n"
            try:
                manual_reply = await asyncio.to_thread(
                    chat_logic.check_manual_memory,
                    messages, session_id, character_id
                ) if not is_internal_generation else None
                if manual_reply is not None:
                    db.add_message(session_id, "user", user_text, character_id)
                    db.add_message(session_id, "assistant", manual_reply, character_id)
                    yield f"data: {_json.dumps({'type':'message','content':manual_reply,'index':0,'total':1}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return
            except Exception as _manual_error:
                print(f"[Stream] manual memory failed: {_manual_error}", flush=True)

            # ★ 2026-09-16 用户拍板：**危机干预机制整套移除**（原 P0 硬拦截块删掉了）。
            #   起因：L3 是**纯正则、不走模型**的硬拦截，正则里有裸词「想死」「去死」——
            #   用户说「想死你了」就被判定为极端危机，直接收到罐头安慰 + 三条热线卡片
            #   （截图证据），并且**他那句话不会被存库**（硬拦截提前 return，用户消息丢了），
            #   她的上下文出现空洞。用户原话：「把危机干预提示这块内容给删了」。
            #   现状：不再有任何危机检测/注入/卡片；用户说什么都走正常对话链路。
            #   相关连删除：backend/crisis.py（整文件）、chat_logic 的检测与
            #   【情绪关注/危机关注模式】注入、前端 ws_client.js 的危机弹窗。
            #   要恢复：从 git 历史取回本段与 crisis.py 即可（commit 见 f38f1ba 之前）。

            # ★ 离线状态系统：角色不在时延迟回复（SSE pending 信号，前端显示"她暂时不在"）
            try:
                from . import offline as _offline
                _off = _offline.resolve(
                    session_id, character_id,
                    enabled=body.get("offline_enabled"),
                    max_delay=body.get("offline_max_delay"),
                )
                if (not is_internal_generation) and _off["enabled"] and _off["status"] not in {"online", "half_awake"}:
                    woke, wake_info = _offline.should_wake_from_user(session_id, character_id, user_text, kind="message")
                    if woke:
                        wake_ctx = _offline.build_wake_context(session_id, character_id, wake_info)
                        # ★ 原地修改，避免 messages 被当成 event_stream 局部变量
                        #   （函数内出现 "messages = ..." 会触发 Python 的局部变量判定，
                        #   导致函数开头引用 messages 时 UnboundLocalError，流式聊天必挂）
                        messages[:] = [{"role": "system", "content": wake_ctx}] + list(messages)
                    else:
                        try:
                            db.add_message(session_id, "user", user_text, character_id)
                        except Exception:
                            pass
                        if _claim_offline_reply_once(session_id, character_id, user_text, _off["status"], _off["delay_seconds"]):
                            get_loop().create_task(
                                _offline_reply_later(
                                    session_id, character_id, user_text, messages,
                                    str(request.base_url).rstrip("/"),
                                    _off["delay_seconds"], _off["status"],
                                )
                            )
                        yield f"data: {_json.dumps({'type':'pending','status':_off['status'],'eta':_off['delay_seconds'],'hint':_off['hint']}, ensure_ascii=False)}\n\n"
                        yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                        return
            except Exception as _oe:
                print(f"[Offline-Stream] 检测失败，降级秒回: {_oe}", flush=True)

            # 用户主动问心情时才明确解释；平时由 enrich_messages 注入隐性语气。
            try:
                from . import ai_mood as _stream_mood
                if not is_internal_generation:
                    _stream_mood.detect_user_message(user_text, session_id, character_id)
                if (not is_internal_generation) and _stream_mood.is_asking(user_text):
                    _mood_reply = await _stream_mood.reveal(
                        session_id, character_id, character_name or character_id
                    )
                    if _mood_reply:
                        try:
                            db.add_message(session_id, "user", user_text, character_id)
                            db.add_message(session_id, "assistant", _mood_reply, character_id)
                        except Exception:
                            pass
                        yield f"data: {_json.dumps({'type':'message','content':_mood_reply,'index':0,'total':1,'emotion':'calm'}, ensure_ascii=False)}\n\n"
                        yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                        return
            except Exception as _mood_error:
                print(f"[AiMood-Stream] 心情解释失败，继续常规回复: {_mood_error}", flush=True)

            # 固定网页版走流式入口，也要支持“每天早安/晚安”的自然语言开关。
            try:
                _voice_night = None if is_internal_generation else chat_logic.parse_voice_night_command(
                    user_text, session_id, character_id
                )
                if _voice_night:
                    if _voice_night.get("send_now"):
                        _night_scheduler = scheduler.scheduler.get_or_create(session_id, character_id)
                        get_loop().create_task(
                            _night_scheduler.send_requested_voice_night(character_name or character_id)
                        )
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                        db.add_message(session_id, "assistant", _voice_night["reply"], character_id)
                    except Exception:
                        pass
                    yield f"data: {_json.dumps({'type':'message','content':_voice_night['reply'],'index':0,'total':1}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return

                _pref = None if is_internal_generation else chat_logic.parse_preference_command(user_text)
                if _pref:
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                        db.add_message(session_id, "assistant", _pref["reply"], character_id)
                    except Exception:
                        pass
                    yield f"data: {_json.dumps({'type':'message','content':_pref['reply'],'index':0,'total':1}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return
            except Exception as _pref_error:
                print(f"[Preference-Stream] 偏好指令处理失败，继续常规回复: {_pref_error}", flush=True)

            # ───────────────────────────────────────────────────────────
            # ★ 以下短路原先只存在于 /api/chat，本入口缺失。
            #   好感度 >= 85 的用户走的就是本入口（chat.js: useStream），
            #   于是「10分钟后提醒我喝水」「别发语音了」「你18岁」这类指令
            #   在高好感时全部失效，AI 只会当成普通聊天来回应。
            #   这里与 /api/chat 对齐（SSE 用本入口自己的消息格式）。
            # ───────────────────────────────────────────────────────────

            # ★ 语义闸门（两次 DeepSeek 第一层）：与 /api/chat 保持一致。
            #   不设的话，「一点点，昨天朋友叫我打三角洲」这类句子在高好感用户
            #   （走本入口）身上依旧会被 parse_timed_command 误判成定时指令。
            #   key/model/base_url 在 /api/chat 里是 L1620/1634/1635 定义的局部变量，
            #   本入口没有，这里按同样规则就地取一遍。
            _allow_action = True
            # ★ 始终有定义：内部生成时不走理解层，后面的合规校验要读它
            _intent = {}
            # ★ 当前对话模型（含前端传来的单角色大脑）：理解层与生成层共用，
            #   不能一个用单角色强模型、另一个 fallback 到全局 chat。
            _stream_model = chat_logic.pick_model(body.get("model"), True, character_id)
            # ★ 人格设置优先（2026-09-11）：base_url 跟最终模型走（模型池自带地址优先，
            #   否则保留前端传来的自定义地址），防止「gemini 主脑被指到智谱地址」。
            _stream_base_url = str(body.get("baseUrl") or "").strip()
            try:
                _smcfg = config.get_text_model_config(_stream_model) or {}
                _stream_base_url = (str(_smcfg.get("baseUrl") or "").strip()) or _stream_base_url
            except Exception:
                pass
            if not is_internal_generation:
                try:
                    from .understanding import understand as _understand
                    _intent = await _understand(
                        user_text, messages,
                        session_id=session_id, character_id=character_id,
                        character_name=character_name or "",
                        key=(str(body.get("key") or "").strip()) or config.api_key(),
                        # ★ 同 /api/chat：理解层跟随当前对话模型（含单角色大脑）
                        model=_stream_model,
                        is_internal=is_internal_generation,
                    ) or {}
                    # ★ Phase 2：与 /api/chat 一致，统一读 routing.allow_action_shortcut
                    _routing = (_intent.get("routing") or {}) if _intent else {}
                    if _intent and not _routing.get("allow_action_shortcut", True):
                        _allow_action = False
                        print(f"[Understanding] 语义闸门(stream)拦截 "
                              f"intent={_intent.get('intent')} sub={_intent.get('sub_type')} "
                              f"wants={_intent.get('user_wants')} text={user_text[:30]}", flush=True)
                except Exception as _se:
                    print(f"[Understanding] 语义闸门(stream)失败(放行): {_se}", flush=True)
                    _allow_action = True
            # ★ Agent 助手模式：理解层判定为「任务型指令」时，走 Agent 循环。
            try:
                from .agent.mode import detect_agent_task
                _is_agent_task = (not is_internal_generation) and detect_agent_task(_intent, user_text)
            except Exception:
                _is_agent_task = False

            if _is_agent_task:
                try:
                    from .agent.loop import run_agent_task
                    import asyncio as _aio
                    _q = _aio.Queue()

                    async def _on_agent_ev(ev):
                        await _q.put(ev)

                    _agent_task = _aio.create_task(run_agent_task(
                        user_text,
                        session_id=session_id, character_id=character_id,
                        character_name=character_name or "",
                        key=(str(body.get("key") or "").strip()) or config.api_key(),
                        model="",
                        on_event=_on_agent_ev,
                    ))
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                    except Exception:
                        pass
                    _final_answer = ""
                    while True:
                        if _agent_task.done() and _q.empty():
                            break
                        try:
                            _ev = await _aio.wait_for(_q.get(), timeout=0.2)
                        except _aio.TimeoutError:
                            continue
                        yield f"data: {_json.dumps(_ev, ensure_ascii=False)}\n\n"
                        if _ev.get("type") == "agent_final":
                            _final_answer = _ev.get("answer", "")
                    try:
                        _ret = await _agent_task
                        if _ret:
                            _final_answer = _ret
                    except Exception:
                        pass
                    if _final_answer:
                        try:
                            db.add_message(session_id, "assistant", _final_answer, character_id)
                        except Exception:
                            pass
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return
                except Exception as _age:
                    print(f"[Agent] stream 循环异常(回退普通聊天): {_age}", flush=True)

            # ★ 用户引导：与 /api/chat 对齐
            _record_correction(session_id, character_id, _intent, user_text, tag="[stream]")
            try:
                _timed = None if (is_internal_generation or not _allow_action) else await chat_logic.resolve_timed_reminder(
                    user_text, character_name or "default",
                    session_id=session_id, character_id=character_id
                )
                if _timed:
                    # ★ 文案统一渲染（模型主判/兜底 + 无事项时反问），不再各拼一份模板；
                    #   2026-09-16 起这句话优先由她**用自己的话说**（事实由 chat_logic 校验）
                    _reply = await chat_logic.build_reminder_reply_voiced(
                        _timed, "app", character_name=character_name or "default",
                        character_id=character_id, session_id=session_id)
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                        db.add_message(session_id, "assistant", _reply, character_id)
                    except Exception:
                        pass
                    yield f"data: {_json.dumps({'type':'message','content':_reply,'index':0,'total':1}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return

                # ★ 2026-09-14 修：同 /api/chat，语音偏好不受动作短路闸门管辖
                #   （它只记录偏好 + 回一句话，零副作用，不该被闸门挡掉）。
                _voice_pref = None if is_internal_generation else _parse_voice_pref(
                    user_text, session_id, character_id)
                if _voice_pref:
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                        db.add_message(session_id, "assistant", _voice_pref, character_id)
                    except Exception:
                        pass
                    yield f"data: {_json.dumps({'type':'message','content':_voice_pref,'index':0,'total':1}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return

                _ident = None if is_internal_generation else chat_logic.parse_identity_command(
                    user_text, character_name or "default",
                    session_id=session_id, character_id=character_id,
                    intent=_intent
                )
                if _ident:
                    # ★ 2026-09-16：「我记住了」用她自己的话说（与 /api/chat 对齐）
                    _ident["reply"] = await chat_logic.voice_identity_reply(
                        _ident, character_name=character_name or "default",
                        character_id=character_id, session_id=session_id)
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                        db.add_message(session_id, "assistant", _ident["reply"], character_id)
                    except Exception:
                        pass
                    yield f"data: {_json.dumps({'type':'message','content':_ident['reply'],'index':0,'total':1}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return
            except Exception as _sc_error:
                print(f"[ShortCircuit-Stream] 指令短路失败，继续常规回复: {_sc_error}", flush=True)

            # 关系边界拒绝 + 专属梗扫描（/api/chat 有，本入口原先缺失）
            try:
                from . import relationship_extras
                _refusal = None if is_internal_generation else relationship_extras.check_boundary(
                    user_text, session_id, character_id)
                if _refusal:
                    try:
                        db.add_message(session_id, "user", user_text, character_id)
                        db.add_message(session_id, "assistant", _refusal, character_id)
                    except Exception:
                        pass
                    yield f"data: {_json.dumps({'type':'message','content':_refusal,'index':0,'total':1,'emotion':'cold'}, ensure_ascii=False)}\n\n"
                    yield f"data: {_json.dumps({'type':'done'}, ensure_ascii=False)}\n\n"
                    return
                if not is_internal_generation:
                    relationship_extras.scan_anchor_message(user_text, session_id, character_id)
            except Exception as _re:
                print(f"[RelExtras-Stream] 失败: {_re}", flush=True)

            # 2. 构建带记忆/人设/语义的 messages（复用现有 chat_logic.enrich_messages）
            base_messages = []
            try:
                from .chat_logic import enrich_messages
                base_messages = await enrich_messages(
                    messages, session_id, character_name,
                    sticker=bool(body.get("sticker")),
                    internal_generation=is_internal_generation,
                )

                # ★ 语义理解层（「两次 DeepSeek」的第一层）—— 主入口必须接。
                #   高好感度用户走的就是 /api/chat/stream，只接 /api/chat 的话
                #   等于最需要它的场景用不上。key/model 在此处尚未解析，
                #   由理解层自行从配置读取。
                try:
                    from .understanding import analyze_and_inject
                    base_messages = await analyze_and_inject(
                        base_messages, user_text,
                        session_id=session_id, character_id=character_id,
                        character_name=character_name or "",
                        key=str(body.get("key") or "").strip(),
                        is_internal=is_internal_generation,
                    )
                except Exception as _ue:
                    print(f"[Understanding] stream 注入失败(静默): {_ue}", flush=True)

                try:
                    from . import offline as _offline
                    _awake = _offline.get_awake_state(session_id, character_id)
                    if _awake:
                        _wake_ctx = _offline.build_wake_context(session_id, character_id, {"recent_attempts": []})
                        for m in base_messages:
                            if m.get("role") == "system":
                                m["content"] = (m.get("content") or "") + "\n\n" + _wake_ctx
                                break
                        else:
                            base_messages.insert(0, {"role": "system", "content": _wake_ctx})
                except Exception:
                    pass
            except Exception as e:
                print(f"[Stream] enrich_messages 失败: {e}", flush=True)
                base_messages = messages

            # ★ 与 /api/chat 共用的 system prompt 增强块（见 _inject_shared_context）
            #   原先本入口缺 记忆上下文 / 关系状态 / 反思，高好感用户走的正是本入口，
            #   于是越亲密反而收不到个性化记忆。现在两边共用同一份。
            await _inject_shared_context(base_messages, session_id, character_id,
                                         user_text, with_realness=True)

            # ★ 2026-09-14 埋点：prompt 规模诊断（装配完成后、生成之前）。
            #   用途：① 验证 PROMPT_COMPACT 到底省了多少 ② 发现 prompt 意外膨胀
            #   ③ 记录本回合走的是哪套配置（压缩开关/理解层开关/模型），
            #     这样报告里能把"回复质量变化"和"配置变化"对上时间点。
            try:
                _p_msgs = [m for m in base_messages if isinstance(m, dict)]
                _p_sys = sum(len(str(m.get("content") or "")) for m in _p_msgs
                             if m.get("role") == "system")
                _p_blocks = sum(str(m.get("content") or "").count("【")
                                for m in _p_msgs if m.get("role") == "system")
            except Exception:
                pass

            # ★ 引用上下文：用户右键引用了某条历史消息，让模型知道回复是针对哪句
            if reply_to:
                _quote_hint = (
                    "\n\n【用户引用的消息】用户这次回复时，引用了下面这条"
                    + ("他自己" if reply_to.get("role") == "user" else "你（AI）")
                    + "之前说过的话：「" + reply_to.get("content", "") + "」\n"
                    + "请围绕这条被引用的话来回应，别答非所问。"
                )
                # ★ 追加到「最后一条 user 消息」（用户当前这条），而不是历史第一条 user。
                #   旧代码 for/break 命中第一条 user（通常是历史消息），导致引用提示
                #   和用户当前发言脱节，模型看不到用户在引用哪条。
                _target = None
                for _m in reversed(base_messages):
                    if _m.get("role") == "user":
                        _target = _m
                        break
                if _target is not None:
                    _target["content"] = (_target.get("content") or "") + _quote_hint
                else:
                    base_messages.append({"role": "user", "content": _quote_hint.strip()})

            # 3. 提取 CompanionOS 语义状态（供情绪引擎判断）
            semantic_state = None
            try:
                from .companion_os.controller import get_companion_os
                os_controller = get_companion_os()
                brain_state = None if is_internal_generation else await os_controller.process_async(
                    session_id, character_name, user_text
                )
                semantic_state = brain_state.get("semantic_state")
            except Exception as e:
                print(f"[Stream] process_async 失败: {e}", flush=True)

            # 4. 更新 AI 情绪
            ai_emotion = {"emotion": "calm", "intensity": 0.5}
            try:
                eng = _get_emotion_engine()
                if eng:
                    if not is_internal_generation:
                        ai_emotion = eng.update_from_semantic(
                            session_id, character_id, semantic_state
                        )
            except Exception as e:
                print(f"[Stream] 情绪更新失败: {e}", flush=True)

            # 4.5 AI 主动引用：用户连发多条时，让模型挑一条针对性回应（仅主链路）
            _ai_quote = None
            if not reply_to and not is_internal_generation:
                try:
                    _q_model = _stream_model
                    _q_key = config.api_key_for_model(_q_model)
                    _ai_quote = await _maybe_ai_quote(messages, _q_model, _q_key)
                except Exception as _qe:
                    print(f"[Stream] AI主动引用决策失败(静默): {_qe}", flush=True)

            # 5. 分段生成
            # ★ 把语义分析出的用户意图一起交给生成器。之前 intent 只喂给了情绪引擎，
            #   生成器完全不知道"用户这句话是来干嘛的"，只能顺着历史模式猜，
            #   于是出现问「有没有想我」却被回一句晚安这种答非所问。
            _user_intent = ""
            try:
                if semantic_state is not None:
                    _it = getattr(semantic_state, "intent", None)
                    _user_intent = (
                        _it.value if hasattr(_it, "value") else str(_it or ""))
            except Exception:
                _user_intent = ""
            # 5. 边生成边推送：逐段生成、逐段去重、逐段配音下发，
            #    消除"先生成全部再推送"造成的 10~25 秒空窗期。
            collected = []
            try:
                from .tts import prepare_visible_text as _pvt_base
                from .character_manager import get_action_brackets as _get_ab
                _ab_on = _get_ab(character_id, (_style_ov or {}).get("action_brackets"))
                def _prepare_visible_text(x):
                    return _pvt_base(x, action_brackets=_ab_on)
            except Exception:
                _prepare_visible_text = lambda x: str(x or "")

            _gen = _get_multi_turn()
            _turn_stream = None
            try:
                # ★ once 模式：一次生成完整回复 + 拆句（默认开）。
                #   相比逐段独立生成（N 段 = N 次 LLM），一次生成只调 1 次 LLM，
                #   提速明显，且一次生成的回复天然连贯、顺序不乱。
                #   config.MULTI_TURN_ONCE=false 时回退旧的逐段生成。
                _once = True
                try:
                    _once = bool(config.get("MULTI_TURN_ONCE", True))
                except Exception:
                    _once = True
                if _gen and _once and hasattr(_gen, "generate_once_stream"):
                    _turn_stream = _gen.generate_once_stream(
                        base_messages=base_messages,
                        ai_emotion=ai_emotion,
                        session_id=session_id,
                        character_id=character_id,
                        user_intent=_user_intent,
                        model_override=_stream_model,
                        base_url_override=_stream_base_url,
                    )
                elif _gen and hasattr(_gen, "generate_stream"):
                    _turn_stream = _gen.generate_stream(
                        base_messages=base_messages,
                        ai_emotion=ai_emotion,
                        session_id=session_id,
                        character_id=character_id,
                        user_intent=_user_intent,
                        model_override=_stream_model,
                        base_url_override=_stream_base_url,
                    )
            except Exception as e:
                print(f"[Stream] 分段生成失败: {e}", flush=True)

            _kept_texts = []
            _push_idx = 0

            def _split_sticker_turns(_turn):
                """★ 2026-09-13：把文本里内联的 [sticker:xx] 标记拆成独立表情包段。
                模型常把标记和文字写在同一行，旧逻辑整段原样下发——App 把标记渲染成
                气泡内小图、QQ 把文字+图当一条消息发，表情包就黏在对话框里。
                现在：文字一段 + 每张图各一段（独立气泡/独立 QQ 消息）。
                标记全是无效或禁用（黄脸系）的，剥干净只留纯文本，绝不让标记漏成文字。"""
                _c = str(_turn.get("content") or "")
                if "[sticker" not in _c.lower():
                    return [_turn]
                try:
                    _sticks = sticker_manager.parse_sticker_markers(_c)
                except Exception:
                    _sticks = []
                _text = sticker_manager.strip_sticker_markers(_c).strip()
                if not _sticks:
                    if not _text:
                        return []
                    _t = dict(_turn)
                    _t["content"] = _text
                    return [_t]
                _outs = []
                if _text:
                    _t = dict(_turn)
                    _t["content"] = _text
                    _outs.append(_t)
                for _st in _sticks:
                    _outs.append({
                        "content": "",
                        "delay": 0.4,
                        "turn_type": "sticker",
                        "emotion": _turn.get("emotion") or "playful",
                        "_sticker_marker": "[sticker:" + _st.get("filename", "") + "]",
                        "sticker": {
                            "filename": _st.get("filename", ""),
                            "url": _st.get("url", ""),
                            "meaning": _st.get("meaning", ""),
                        },
                    })
                return _outs

            async def _process_turn(_turn):
                """单条：可见文本→TTS。返回 payload（去重已由 generate_stream 内部负责，
                不在推送层再删段——避免双重去重把正常气泡跳过，导致"话变少"）。"""
                _c = str(_turn.get("content") or "").strip()
                # 可见文本（括号动作描写剥除）
                _visible = _prepare_visible_text(_c).strip()
                if _c and not _visible:
                    _turn["content"] = ""
                elif _visible:
                    _turn["content"] = _visible
                _payload = {
                    "type": "message",
                    "content": _turn["content"],
                    "index": _push_idx,
                    "total": -1,
                    "emotion": _turn.get("emotion", "calm"),
                    "turn_type": _turn.get("turn_type", "react"),
                }
                # ★ AI 主动引用：第一条气泡带被引用的用户消息
                if _push_idx == 0 and _ai_quote:
                    _payload["reply_to"] = _ai_quote
                if _turn.get("sticker"):
                    _payload["sticker"] = _turn["sticker"]
                # TTS 配音（voice_reply=true 时每条都配）
                if bool(body.get("voice_reply")) and _turn.get("content"):
                    try:
                        from .tts import generate_audio, apply_emotion_to_voice_cfg
                        from .character_manager import select_character_voice_cfg
                        _eng = _get_emotion_engine()
                        _emo_state = _eng.get_state(session_id, character_id or "default") if _eng else {}
                        _emotion = _emo_state.get("emotion", "calm")
                        _intensity = float(_emo_state.get("intensity", 0.5))
                        _bubble_emo = _turn.get("emotion", "")
                        if _bubble_emo and _bubble_emo in (
                            "happy", "excited", "loving", "tender", "playful",
                            "calm", "worried", "sad", "upset", "angry", "cold", "reconciling"
                        ):
                            _emotion = _bubble_emo
                        _vc = select_character_voice_cfg(
                            character_id or "default", emotion=_emotion, mode="chat"
                        )
                        if _vc:
                            _vc_with_emotion = apply_emotion_to_voice_cfg(_vc, _emotion, _intensity)
                            _au = await generate_audio(
                                _turn["content"], _vc_with_emotion,
                                base_url=str(request.base_url).rstrip("/")
                            )
                            if _au:
                                _payload["audio"] = _au
                                _payload["emotion"] = _emotion
                    except Exception as _te:
                        print(f"[TTS-B] 第{_push_idx}句失败，不影响文字: {_te}", flush=True)
                return _payload

            _got_any = False
            if _turn_stream:
                async for _turn in _turn_stream:
                    # ★ 流式推理：收到 thinking 事件立即推送（前端显示「思考中」）。
                    #   生成器在思考结束时附带推理全文（thinking 字段+耗时 seconds），
                    #   有内容就透传给前端展示「思考过程」块；无内容保持原信号行为。
                    if _turn.get("turn_type") == "thinking":
                        _think_payload = {"type": "thinking"}
                        if _turn.get("thinking"):
                            _think_payload["content"] = str(_turn["thinking"])
                            if _turn.get("seconds"):
                                _think_payload["seconds"] = int(_turn["seconds"])
                        yield f"data: {_json.dumps(_think_payload, ensure_ascii=False)}\n\n"
                        continue
                    _got_any = True
                    for _sub in _split_sticker_turns(_turn):
                        _payload = await _process_turn(_sub)
                        if _payload is None:
                            continue
                        _delay = float(_sub.get("delay", 0.3))
                        if _delay > 0:
                            await _asyncio.sleep(_delay)
                        yield f"data: {_json.dumps(_payload, ensure_ascii=False)}\n\n"
                        _sub_text = str(_sub.get("content") or "")
                        _sub_marker = str(_sub.get("_sticker_marker") or "")
                        if _sub_text or _sub_marker:
                            # 表情包段记标记行：QQ 镜像靠它转成独立图片消息，语境层靠它知道发过图
                            collected.append(_sub_marker or _sub_text)
                            _kept_texts.append(_sub_marker or _sub_text)
                            if not is_internal_generation and _sub_text:
                                try:
                                    _extra = {}
                                    if _payload.get("audio"):
                                        _extra["audio"] = _payload["audio"]
                                        _turn_meta["audio"] = True
                                    if _payload.get("sticker"):
                                        _turn_meta["sticker"] = True
                                    if _payload.get("reply_to"):
                                        _extra["reply_to"] = _payload["reply_to"]
                                    db.add_message(session_id, "assistant", _sub_text, character_id, extra=_extra or None)
                                    # ★ 2026-09-14 移除：这里原有一次逐条气泡的
                                    #   ai_promise.extract_and_store(...) —— 一轮回 4 条气泡
                                    #   就会触发 4 次承诺提取 LLM，而下面（本函数末尾）已经用
                                    #   **整轮拼接后的全文 + 用户原话** 再提取一次，输入完全覆盖
                                    #   单条气泡。删除后每轮省 N 次 LLM（N=气泡数），
                                    #   且提取质量更好（承诺常跨两条气泡，如"六点半"在第一条、
                                    #   "我叫你"在第二条，逐条提取反而抓不到）。
                                except Exception as _pe:
                                    print(f"[Stream] 第{_push_idx}条落库失败(静默): {_pe}", flush=True)
                            elif not is_internal_generation and _sub_marker:
                                # 表情包单独落库（存标记行）：下轮模型能知道自己发过哪张图
                                try:
                                    db.add_message(session_id, "assistant", _sub_marker, character_id)
                                except Exception as _pe:
                                    print(f"[Stream] 第{_push_idx}条表情包落库失败(静默): {_pe}", flush=True)
                        _push_idx += 1

            # 6. 降级：generate_stream 没产出任何段时用 deepseek_api 生成
            #   ★ 2026-09-12 修：这里原来用 chat_once（非流式单发），而 chat_once 只返回
            #     content，reasoning_content 直接被丢掉 —— 走降级这条路的回复**永远没有
            #     思考框**。用户实测：本地大脑上下文超限 → 降级到 glm-5.3-flash
            #     （明明是思考模型）却看不到任何思考过程。
            #     现在优先用 stream_chat 收流，边收边分离 reasoning，并在正文前补一条
            #     {"type":"thinking"} SSE（与正常流形状完全一致，前端据此画折叠框并持久化）；
            #     流式不可用再退回原来的 chat_once。
            if not _got_any:
                try:
                    from .deepseek_api import chat_once, stream_chat
                    model = _stream_model
                    key = config.api_key_for_model(model)
                    _deg_msgs = [dict(m) for m in base_messages]
                    _patched = False
                    for _dm in _deg_msgs:
                        if _dm.get("role") == "system":
                            _dm["content"] = (_dm.get("content") or "") + (
                                "\n\n【输出格式】像真人连发微信一样，输出 3~5 句，"
                                "每句单独一行（用换行符分隔），不要只写一句。"
                            )
                            _patched = True
                            break
                    if not _patched:
                        _deg_msgs.insert(0, {"role": "system", "content":
                            "像真人连发微信一样，输出 3~5 句，每句单独一行（用换行符分隔），不要只写一句。"})
                    _think_all = ""
                    _t_deg = None
                    _deg_chunks = []
                    _stream_ok = False
                    try:
                        async for _dk, _dv in stream_chat(
                            model, _deg_msgs, key, temperature=0.7, max_tokens=1024
                        ):
                            if _dk != "line" or not isinstance(_dv, str):
                                continue
                            _ds = _dv[5:].strip() if _dv.startswith("data:") else _dv.strip()
                            if not _ds:
                                continue
                            try:
                                _dj = _json.loads(_ds)
                            except Exception:
                                continue
                            # notice / _meta / error 一律跳过，只取真正的 token 增量
                            if _dj.get("type") or _dj.get("_meta") or _dj.get("error"):
                                continue
                            _dd = ((_dj.get("choices") or [{}])[0].get("delta") or {})
                            _dr = _dd.get("reasoning_content")
                            _dc = _dd.get("content")
                            if _dr:
                                _stream_ok = True
                                if _t_deg is None:
                                    _t_deg = _asyncio.get_event_loop().time()
                                _think_all += str(_dr)
                            elif _dc:
                                _stream_ok = True
                                _deg_chunks.append(str(_dc))
                    except Exception as _se:
                        print(f"[Stream] 降级流式失败，改单发: {_se}", flush=True)
                    if _stream_ok and _deg_chunks:
                        raw = "".join(_deg_chunks).strip()
                        if _think_all.strip():
                            _secs = 0
                            if _t_deg is not None:
                                _secs = int(round(_asyncio.get_event_loop().time() - _t_deg))
                            _tp = {"type": "thinking", "content": _think_all.strip()}
                            if _secs >= 1:
                                _tp["seconds"] = _secs
                            yield f"data: {_json.dumps(_tp, ensure_ascii=False)}\n\n"
                            print(f"[Stream] 降级路径补出思考过程 {len(_think_all)} 字 {_secs}s", flush=True)
                    else:
                        if _asyncio.iscoroutinefunction(chat_once):
                            raw = await chat_once(model, _deg_msgs, key, temperature=0.7, max_tokens=1024)
                        else:
                            raw = chat_once(model, _deg_msgs, key, temperature=0.7, max_tokens=1024)
                    _raw = (raw or "嗯。").strip()
                    _parts = [p.strip() for p in _raw.split("\n") if p.strip()]
                    for _p in (_parts or [_raw]):
                        _payload = await _process_turn({
                            "content": _p, "delay": 0.5, "turn_type": "react",
                            "emotion": ai_emotion.get("emotion", "calm"),
                        })
                        if _payload is None:
                            continue
                        yield f"data: {_json.dumps(_payload, ensure_ascii=False)}\n\n"
                        if _p:
                            collected.append(_p)
                            _kept_texts.append(_p)
                        _push_idx += 1
                    print(f"[Stream] 降级生成 {len(_parts or [_raw])} 条", flush=True)
                except Exception as e:
                    print(f"[Stream] 降级 chat_once 失败: {e}", flush=True)
                    _fb = _pick_fallback_reply(character_id, session_id, user_text)
                    _payload = await _process_turn({
                        "content": _fb, "delay": 0.3, "turn_type": "react", "emotion": "calm",
                    })
                    if _payload is not None:
                        yield f"data: {_json.dumps(_payload, ensure_ascii=False)}\n\n"
                        collected.append(_fb)

            # 程序化表情包互动：文字段全部推完后，按语境低概率补一张。
            try:
                from . import sticker_manager as _sticker_mgr
                import random as _sticker_random
                _full_before_sticker = "\n".join(str(t) for t in collected)
                _has_marker = bool(_sticker_mgr.parse_sticker_markers(_full_before_sticker))
                _user_has_sticker = bool(_sticker_mgr.parse_sticker_markers(user_text))
                if bool(body.get("sticker")) and not _has_marker:
                    _prob = 0.55 if _user_has_sticker else 0.16
                    if _sticker_random.random() < _prob:
                        _emotion_hint = ai_emotion.get("emotion", "playful")
                        _pick = _sticker_mgr.pick_for_context(user_text, _emotion_hint)
                        if _pick:
                            _st = {
                                "content": "", "delay": 0.18, "turn_type": "sticker",
                                "emotion": _emotion_hint or "playful",
                                "sticker": {
                                    "filename": _pick.get("filename", ""),
                                    "url": _pick.get("url", ""),
                                    "meaning": _pick.get("meaning", ""),
                                },
                            }
                            _payload = await _process_turn(_st)
                            if _payload is not None:
                                yield f"data: {_json.dumps(_payload, ensure_ascii=False)}\n\n"
                                _st_marker = f"[sticker:{_st['sticker']['filename']}]"
                                collected.append(_st_marker)
                                # QQ 镜像与语境层也要知道这张图（标记行会在 QQ 侧转成独立图片消息）
                                _kept_texts.append(_st_marker)
                                try:
                                    db.add_message(session_id, "assistant", _st_marker, character_id)
                                except Exception:
                                    pass
                            _push_idx += 1
            except Exception as _sticker_auto_e:
                print(f"[Sticker] AI自动表情失败(静默): {_sticker_auto_e}", flush=True)

            # 8. 持久化：user 落库（assistant 已在推送时逐条落库，这里不再重复写）
            try:
                if not is_internal_generation:
                    _u_extra = {"reply_to": reply_to} if reply_to else None
                    db.add_message(session_id, "user", user_text, character_id, extra=_u_extra)
            except Exception as e:
                print(f"[Stream] 持久化消息失败: {e}", flush=True)

            # 固定测试页使用本接口：与 /api/chat 对齐长期记忆、核心档案和摘要更新。
            if collected and not is_internal_generation:
                _assistant_joined = "\n".join(collected)
                # ★ Phase 3 闭环：校验最终回复是否遵守理解层的执行清单并落库。
                #   与 /api/chat 对齐；理解层没参与时（_intent 为空）跳过。
                #   只记录不改写——先看清违规分布，再决定要不要拦。
                try:
                    from .understanding import check_response_compliance, log_compliance
                    _comp = check_response_compliance(_assistant_joined, _intent)
                    log_compliance(session_id, character_id, _intent,
                                   _assistant_joined, _comp)
                    if _intent and not _comp.get("ok"):
                        print(f"[Compliance][stream] 回复未遵守执行清单 "
                              f"intent={_intent.get('intent')} "
                              f"wants={_intent.get('user_wants')} "
                              f"violations={_comp.get('violations')}", flush=True)
                except Exception:
                    pass
                try:
                    from . import conv_state
                    conv_state.update_conv_state(
                        session_id, character_id, user_text, _assistant_joined
                    )
                except Exception as _ce:
                    print(f"[Stream] ConvState 更新失败(静默): {_ce}", flush=True)
                # ★ QQ 打通：App 分段回复合并推送到 QQ（语音段同步推转写文字，与 /api/chat 对齐）
                try:
                    from . import onebot
                    _qq_text = "\n".join(_kept_texts).strip()
                    if _qq_text:
                        await onebot.send_qq_message(_qq_text)
                except Exception as _qq_e:
                    print(f"[Stream] 推QQ失败(静默): {_qq_e}", flush=True)
                try:
                    should_extract = chat_logic.should_extract_round(session_id, character_id)
                    explicit_memory = bool(re.search(
                        r"记住|我叫|我喜欢|我不喜欢|我讨厌|我过敏|我的.{1,10}是|以后不要|别再",
                        user_text
                    ))
                    if should_extract or explicit_memory:
                        asyncio.create_task(
                            chat_logic.maybe_auto_extract(session_id, character_id, user_text)
                        )
                except Exception as _memory_error:
                    print(f"[Stream] memory pipeline failed: {_memory_error}", flush=True)
                # ★ AI 承诺识别（2026-09-09 补）：之前只有旧 /api/chat 挂了提取，
                #   APP 主力聊天走的 stream 端点完全没挂——「六点半叫我」这类承诺
                #   在源头就丢，到点当然不兑现（idle_agent 的 due_promises 永远查空）。
                try:
                    from . import ai_promise as _ap
                    _ai_joined = str(_assistant_joined or "").strip()
                    if _ai_joined:
                        asyncio.create_task(
                            _ap.extract_and_store(session_id, character_id, _ai_joined, user_text)
                        )
                except Exception as _pe:
                    print(f"[Stream] 承诺提取调度失败: {_pe}", flush=True)

            # ═══════════════════════════════════════════════════════════
            # ★ 2026-09-14 补齐：与 /api/chat 对齐的 6 项后处理
            #   背景（审计结论）：这些逻辑此前**只挂在旧 /api/chat 上**，而
            #   App 主力入口是 /api/chat/stream —— 于是越高好感（走 stream）
            #   的用户越收不到：关系状态更新、关系升级仪式、AI 自身状态、
            #   人格进化、禁止表达检测、人格漂移映射。
            #   全部为后台 fire-and-forget 或纯本地计算，不阻塞回复流；
            #   每一项独立 try/except，失败只影响自己。
            # ═══════════════════════════════════════════════════════════
            if not is_internal_generation:
                _post_joined = "\n".join(collected) if collected else ""
                _p_ok, _p_fail = [], []

                # (1) YunLink 关系状态更新（与 /api/chat:3296 对齐）
                try:
                    get_loop().create_task(
                        yunlink_relationship.update_relationship(
                            user_id=session_id,
                            message=user_text,
                            character_id=character_id,
                        )
                    )
                    _p_ok.append("relationship")
                except Exception as _yr:
                    _p_fail.append("relationship")
                    print(f"[Stream] 关系状态更新失败(静默): {_yr}", flush=True)

                # (2) 关系升级检测（阶段变化 → 仪式，下条注入）
                try:
                    from .relationship.ritual import check_upgrade_after_chat
                    _up = check_upgrade_after_chat(session_id, character_id)
                    if _up:
                        print(f"[Ritual] 关系升级: {_up.get('from')} → {_up.get('to')}", flush=True)
                    _p_ok.append("ritual")
                except Exception as _re2:
                    _p_fail.append("ritual")
                    print(f"[Stream] 升级检测失败(静默): {_re2}", flush=True)

                # (3) AI 自身状态更新（内部会 run_coroutine_threadsafe().result()，
                #     必须丢线程池，否则卡死主事件循环最长 10s）
                try:
                    from .companion.ai_state.updater import update_after_chat
                    _rs = db.get_relationship_state(session_id, character_id) or {}
                    await asyncio.to_thread(
                        update_after_chat,
                        session_id=session_id,
                        character_id=character_id,
                        user_message=user_text,
                        relationship=_rs,
                    )
                    _p_ok.append("ai_state")
                except Exception as _as:
                    _p_fail.append("ai_state")
                    print(f"[Stream] AI状态更新失败(静默): {_as}", flush=True)

                # (4) 人格进化（概率触发）+ 互动证据成长
                try:
                    from .personality.evolution import calculate_growth, should_evolve
                    from .personality.manager import PersonalityManager
                    _rs2 = db.get_relationship_state(session_id, character_id) or {}
                    _bs2 = db.get_behavior_state(session_id, character_id) or {}
                    if should_evolve(_rs2):
                        _g = calculate_growth(_rs2, behavior=_bs2)
                        if _g:
                            PersonalityManager().update(session_id, character_id, _g)
                            print(f"[PersonalityEvolution] 人格成长: {_g}", flush=True)
                    try:
                        PersonalityManager().observe_interaction(session_id, character_id, user_text)
                    except Exception as _oe:
                        print(f"[Stream] 互动证据记录失败(静默): {_oe}", flush=True)
                    _p_ok.append("personality")
                except Exception as _evo:
                    _p_fail.append("personality")
                    print(f"[Stream] 人格更新失败(静默): {_evo}", flush=True)

                # (5) 禁止表达检测（与 /api/chat:3180-3186 对齐；只打印日志、不改写回复）
                #   ★ 注意签名：personality_guard(reply, personality) —— 两个参数。
                try:
                    from .personality_manager import personality_guard as _pg_fn
                    if _post_joined:
                        _pcfg = character_manager.get_character_any(character_id or "default") or {}
                        _pg_ok = _pg_fn(_post_joined, str(_pcfg.get("personality") or ""))
                        if not _pg_ok:
                            print(f"[PersonalityGuard][stream] 回复命中禁止表达 "
                                  f"char={character_id} reply={_post_joined[:60]!r}", flush=True)
                            _p_fail.append("guard_hit")
                    _p_ok.append("guard")
                except Exception as _pg:
                    _p_fail.append("guard")
                    print(f"[Stream] 禁止表达检测失败(静默): {_pg}", flush=True)

                # (6) 后台反思 + 人格漂移映射（按角色隔离）
                #   注：`reflection` 已在 main.py 顶部模块级导入，这里直接用，无需局部再导。
                try:
                    async def _reflect_and_drift_s(sid, cid):
                        await reflection.update_reflection(sid, cid)
                        try:
                            reflection.apply_reflection_to_personality_state(sid, cid)
                        except Exception as _e2:
                            print(f"[Stream] 人格漂移映射失败(可忽略): {_e2}", flush=True)
                    get_loop().create_task(_reflect_and_drift_s(session_id, character_id))
                    _p_ok.append("reflection")
                except Exception as _rf2:
                    _p_fail.append("reflection")
                    print(f"[Stream] 反思触发失败(静默): {_rf2}", flush=True)

                # ★ 2026-09-14 埋点：记录这 6 项后处理的结果
                #   用途：区分"功能没接"和"接了但报错"——这是之前审计里最难判断的一类问题
                try:
                    from . import trace as _trace
                    _trace.pipeline(session_id, character_id, ok=_p_ok, failed=_p_fail)
                except Exception:
                    pass

            # ★ 2026-09-14 埋点：本回合完整快照（诊断主数据）
            try:
                from . import trace as _trace
                _reply_txt = "\n".join(str(x) for x in collected) if collected else ""
                _trace.turn(
                    session_id=session_id, character_id=character_id,
                    user_text=user_text,
                    system_chars=_p_sys, block_count=_p_blocks,
                    history_len=len(base_messages) if isinstance(base_messages, list) else 0,
                    prompt_compact=_prompt_compact_flag,
                    understanding_on=_understanding_on_flag,
                    reply_count=len(collected), reply_chars=len(_reply_txt),
                    reply_excerpt=_reply_txt.replace("\n", " / ")[:120],
                    has_audio=_turn_meta.get("audio", False),
                    has_sticker=(_turn_meta.get("sticker", False)
                                 or ("[sticker" in _reply_txt)),
                    elapsed_s=(time.time() - _turn_t0) if _turn_t0 else 0.0,
                    model=_stream_model, instr_kinds=(_instr_kinds or None),
                    error=("" if collected else "empty_reply"),
                )
            except Exception:
                pass

            # 9. 结束信号
            yield f"data: {_json.dumps({'type':'done','emotion': ai_emotion.get('emotion','calm')}, ensure_ascii=False)}\n\n"

            # ★ 新增：每8轮对话触发一次反思生成+应用（后台非阻塞）
            try:
                asyncio.create_task(_maybe_trigger_reflection(session_id, character_id))
            except Exception as _re:
                print(f"[Stream] 反思触发失败(静默): {_re}", flush=True)

        except Exception as e:
            print(f"[Stream] event_stream 异常: {e}", flush=True)
            # ★ 2026-09-14 埋点：聊天主链路异常必须留痕 —— 这是"她突然不回话"的头号原因，
            #   以前只在日志里打一行，事后要翻半天；现在进 trace 报告的 D8 异常聚合。
            try:
                from . import trace as _trace
                _trace.err("stream_event", "event_stream 异常", exc=e, session_id=session_id)
                _trace.turn(session_id=session_id, character_id=character_id,
                            user_text=user_text, error="exception:" + str(e)[:120],
                            instr_kinds=(_instr_kinds or None))
            except Exception:
                pass
            # ★ 人设化降级回复（不暴露技术错误给用户）
            _fallback = _pick_fallback_reply(character_id, session_id, user_text)
            yield f"data: {_json.dumps({'type':'message','content':_fallback,'index':0,'total':1,'emotion':'calm'}, ensure_ascii=False)}\n\n"
            yield f"data: {_json.dumps({'type':'done','emotion':'calm'}, ensure_ascii=False)}\n\n"

    return _StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":      "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":         "keep-alive",
        }
    )


async def _dedup_regenerate(
    base_messages: list,
    session_id: str,
    character_id: str,
    said_texts: list,
    chat_once_fn,
    model: str,
    api_key: str,
    attempts: int = 2,
) -> str:
    """去重命中后重新生成一条气泡，并做二次验证。

    返回新文本；两次仍与历史重复就返回最后一次的结果（宁可不够新，也不要空回复）。
    只负责"一条"，不触碰整轮 turns —— 保住多气泡的递进结构。
    """
    try:
        from .companion.quality_guard import dedup_check_async
    except Exception:
        return ""

    avoid = "\n".join(
        f"- {str(s)[:80]}" for s in (said_texts or []) if str(s or "").strip()
    )
    last = ""
    for i in range(max(1, int(attempts))):
        try:
            prompt = (
                "你刚要发的这句话，和下面这些你之前说过的话重复了"
                "（换个词、换个说法、换种句式都算重复）：\n"
                + (avoid or "- （暂无历史）")
                + "\n请重新说一句：换一个角度，给出新信息、新的感受或新的追问。"
                "不要复述上面任何一句的意思，也不要只是换几个词再说一遍。\n"
                "只输出这一句话本身，不要任何解释、不要加引号。"
            )
            if last:
                prompt += (
                    f"\n\n注意：你上一次重试说的是「{last[:60]}」，仍然重复，"
                    f"请换一个完全不同的角度。"
                )
            raw = await chat_once_fn(
                model,
                base_messages + [{"role": "user", "content": prompt}],
                api_key,
                temperature=0.95,
                max_tokens=300,
            )
            new = str(raw or "").strip()
            if not new:
                continue
            last = new
            # 二次验证：新内容仍然重复就再试一次
            if not await dedup_check_async(
                new, session_id, character_id,
                chat_once_fn=chat_once_fn, model=model, api_key=api_key,
            ):
                return new
        except Exception as e:
            print(f"[Dedup] 重新生成异常: {e}", flush=True)
            break
    return last or ""


def _pick_fallback_reply(character_id: str = "default", session_id: str = "default", user_text: str = "") -> str:
    """
    LLM调用失败时的人设化降级回复。
    优先从角色配置读取，降级用通用文案。
    """
    import random

    # 尝试从角色配置读取自定义降级文案
    try:
        from .character_manager import get_character_any
        char = get_character_any(character_id or "default")
        fallbacks = char.get("fallback_replies", [])
        if fallbacks and isinstance(fallbacks, list):
            return random.choice(fallbacks)
    except Exception:
        pass

    try:
        recent = db.recent_messages(session_id or "default", 6, character_id or "default") or []
        last_user = ""
        # ★ 修复：降级回复要接的是"用户刚说的这句话"，不是历史里最旧的一条。
        #   recent_messages 返回正序（旧→新），之前从头部遍历取到的是最旧消息，
        #   导致用户问"想我了吗"时降级回复却接旧话题"宝宝"。
        if user_text:
            last_user = str(user_text or "").strip()
        else:
            for row in reversed(recent):
                if str(row.get("role") or "") == "user":
                    last_user = str(row.get("content") or "").strip()
                    if last_user:
                        break
        if last_user:
            # ★ 兜底文案不再重复用户的话（"你是想继续说X对吧"这种机械复读，
            #   会显得上下文错乱）。改成自然、不暴露故障的人设化回应。
            return random.choice([
                "嗯我在呢～刚才有点走神，你再说一遍好不好？",
                "宝~ 我缓过来了，你继续说，我这次认真听。",
                "抱歉刚才没跟上，你再讲一次，我接着你。",
                "我在的，刚脑子打了个岔，你重说下嘛～",
            ])
    except Exception:
        pass

    _generic = [
        "我刚才缓了一下，但我还在。你继续说，我这次跟上。",
        "刚刚有点卡住了，不是没听见。你接着讲就好。",
        "我没断线，就是反应慢了一点。你继续。",
    ]
    return random.choice(_generic)


# public/ 不存在时（如 exe 单独运行）不挂载静态，仅作为 API 服务
if PUBLIC.is_dir():
    app.mount("/", StaticFiles(directory=str(PUBLIC), html=True), name="static")

# 开发友好：静态资源禁用缓存（改前端代码后刷新立即生效，避免浏览器缓存旧文件）
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api") or request.url.path.startswith("/ws"):
        return response
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

