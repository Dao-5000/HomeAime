# -*- coding: utf-8 -*-
"""
唱歌总入口（结合现有架构）

对外：
- handle_sing_request(intent, on_send_audio, on_send_event, voice_cfg)
  通话中唱歌：普通唱（逐句流式）或合唱（DuetSession 你一句我一句）
- 歌词获取：LLM 召回 + kv 缓存（lyrics_fetcher）
"""
import asyncio
import logging

from .intent_detector import SingIntent
from .lyrics_fetcher import fetch_lyrics
from .singing_engine import sing_lyrics_stream
from .duet_manager import DuetSession

logger = logging.getLogger(__name__)


async def resolve_lyrics(intent: SingIntent) -> dict:
    if intent.song_name:
        result = await fetch_lyrics(intent.song_name, intent.artist, intent.style)
        if result:
            return result
        return None
    return await fetch_lyrics("", "", intent.style)


async def handle_sing_request(
    intent: SingIntent,
    on_send_audio,
    on_send_event,
    voice_cfg=None,
    on_finished=None,
    start_index: int = 0,
    should_stop=None,
):
    """
    处理唱歌请求（通话中）。
    on_send_audio(audio_bytes, meta)  推音频
    on_send_event(event_dict)         推事件
    start_index                       从第几句开始唱（用于被打断后「从断点续唱」）
    should_stop                       返回 True 时立即停止（被打断时不再白跑整首）
    返回 DuetSession（合唱时）或 None（普通唱）。
    """
    lyrics = await resolve_lyrics(intent)
    if not lyrics:
        await on_send_event({
            "type": "sing_error",
            "message": f"《{intent.song_name}》的歌词我想不起来了…" if intent.song_name else "我一下子想不起唱什么好…",
        })
        await on_send_event({"type": "speak_fallback", "text": "我记不清歌词了，你哼两句给我听听？"})
        return None

    # ★ 续唱：跳过已经唱过的句子；line_index 仍报原始下标，方便前端对齐进度
    all_lines = lyrics.get("lines") or []
    start = max(0, min(int(start_index or 0), max(len(all_lines) - 1, 0)))
    lines = all_lines[start:] or all_lines

    await on_send_event({
        "type": "sing_start",
        "song_name": lyrics.get("song_name", ""),
        "artist": lyrics.get("artist", ""),
        "is_duet": intent.is_duet,
        "total_lines": len(all_lines),
        "start_index": start,
    })

    if intent.is_duet:
        async def ai_sing_line(line, idx):
            async for seg in sing_lyrics_stream([line], voice_cfg):
                return seg.audio
            return None

        session = DuetSession(
            lines,
            on_ai_sing=ai_sing_line,
            on_send_audio=on_send_audio,
            on_send_event=on_send_event,
        )

        async def _run_duet():
            try:
                await session.start()
            finally:
                if on_finished:
                    try:
                        await on_finished(lyrics.get("song_name", ""), is_duet=True)
                    except Exception:
                        pass

        asyncio.create_task(_run_duet())
        return session

    # 普通唱：流式逐句推送
    async def _sing():
        try:
            async for seg in sing_lyrics_stream(lines, voice_cfg):
                # ★ 被打断：立刻停，别把整首歌都合成完（白白烧算力）
                if should_stop is not None:
                    try:
                        if should_stop():
                            await on_send_event({"type": "sing_interrupted"})
                            if on_finished:
                                try:
                                    await on_finished(lyrics.get("song_name", ""), is_duet=False)
                                except Exception:
                                    pass
                            return
                    except Exception:
                        pass
                await on_send_audio(seg.audio, {
                    "type": "singing_audio",
                    "line": seg.line,
                    "line_index": seg.line_index + start,
                    "is_last": seg.is_last,
                    "emotion": seg.emotion,
                    "pause_after": seg.pause_after,
                })
            await on_send_event({"type": "sing_end", "song_name": lyrics.get("song_name", "")})
            if on_finished:
                try:
                    await on_finished(lyrics.get("song_name", ""), is_duet=False)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[唱歌] 异常: {e}")
            try:
                await on_send_event({"type": "sing_error", "message": "唱着唱着出了点问题…"})
            except Exception:
                pass

    asyncio.create_task(_sing())
    return None
