# -*- coding: utf-8 -*-
"""
Minecraft Bot HTTP 路由（挂到现有 main.py）
  POST /api/bot/decide            Bot 推送感知快照 → LLM 决策 → 返回动作序列
  POST /api/bot/decide/background 主动决策 → 进 background 队列（异步）
  POST /api/bot/event             Bot 上报事件（action_done/death 等）
  GET  /api/bot/command           Bot 轮询：优先 urgent（语音）再 background
  GET  /api/bot/status            查询 Bot 状态（调试/前端展示）
"""
import asyncio
import time
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter(prefix="/api/bot", tags=["minecraft-bot"])


def _ctrl(body=None, session_id=None, character_id=None):
    from .bot_controller import get_controller, get_active_binding
    body = body or {}
    sid = session_id or body.get("session_id")
    cid = character_id or body.get("character_id")
    if not sid and not cid:
        sid, cid = get_active_binding()
    return get_controller(sid or "default", cid or "default")


@router.post("/bind")
async def bot_bind(body: Dict[str, Any]):
    """生活页选择 Minecraft 时，把 Bot 绑定到当前 session + 角色，并自动启动 bot。

    根据当前角色名生成拼音用户名（MC 不允许中文，如"助手"→"guzi"），
    从 body 拿局域网端口，用 subprocess 拉起 minecraft_bot，
    实现「APP 内一键让 AI 人格进游戏」，而非手动跑一个独立 bot。
    """
    from .bot_controller import set_active_binding, start_bot_process, _to_pinyin
    from .. import character_manager

    sid = str(body.get("session_id") or "default")
    cid = str(body.get("character_id") or "default")
    ctrl = set_active_binding(sid, cid)

    # 角色名 → 拼音用户名
    username = "ai"
    try:
        _cfg = character_manager.get_character(cid) or {}
        _name = _cfg.get("name") or _cfg.get("character_name") or cid
        username = _to_pinyin(_name)
    except Exception:
        username = _to_pinyin(cid)

    # 局域网端口（前端传入；缺省回落 25565，通常由用户开世界后告知）
    try:
        port = int(body.get("port") or 25565)
    except (TypeError, ValueError):
        port = 25565

    started = start_bot_process(username, port)

    return {"ok": True, "session_id": ctrl.session_id, "character_id": ctrl.character_id,
            "username": username, "port": port, "started": started}


@router.post("/decide")
async def bot_decide(body: Dict[str, Any]):
    try:
        ctrl = _ctrl(body)
        result = await ctrl.decide(
            snapshot=body.get("snapshot") or {},
            trigger=str(body.get("trigger", "proactive")),
            player_message=str(body.get("player_message", "") or ""),
        )
        return result
    except Exception as e:
        return {"chat": "", "actions": [], "error": str(e)}


@router.post("/decide/background")
async def bot_decide_background(body: Dict[str, Any]):
    try:
        ctrl = _ctrl(body)
        asyncio.create_task(
            ctrl.push_background_decision(
                body.get("snapshot") or {},
                str(body.get("trigger", "proactive")),
            )
        )
        return {"queued": True}
    except Exception:
        return {"queued": False}


@router.post("/event")
async def bot_event(body: Dict[str, Any]):
    try:
        _ctrl(body).handle_event(str(body.get("event", "")), body.get("data") or {})
        return {"ok": True}
    except Exception:
        return {"ok": False}


@router.get("/command")
async def bot_command(session_id: str = "", character_id: str = ""):
    try:
        result = await _ctrl(session_id=session_id, character_id=character_id).pop_command()
        return result if result else {}
    except Exception:
        return {}


@router.get("/status")
async def bot_status(session_id: str = "", character_id: str = ""):
    try:
        ctrl = _ctrl(session_id=session_id, character_id=character_id)
        if ctrl.last_heartbeat and time.time() - ctrl.last_heartbeat > 30:
            ctrl.bot_online = False
        snap = ctrl.get_latest_snapshot()
        return {
            "online": ctrl.bot_online,
            "is_busy": ctrl.is_busy,
            "current_action": ctrl._current_action,
            "player_name": ctrl.memory.player_name,
            "game_stage": (snap or {}).get("self", {}).get("dimension", ""),
            "known_locations": len(ctrl.memory.known_locations),
            "player_prefs": ctrl.memory.player_prefs,
            "urgent_queue_size": ctrl._urgent_queue.qsize(),
            "background_queue_size": ctrl._background_queue.qsize(),
            "last_heartbeat": ctrl.last_heartbeat,
            "heartbeat_age_sec": round(max(0.0, time.time() - ctrl.last_heartbeat), 1) if ctrl.last_heartbeat else None,
            "session_id": ctrl.session_id,
            "character_id": ctrl.character_id,
        }
    except Exception:
        return {"online": False, "is_busy": False}
