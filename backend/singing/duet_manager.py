# -*- coding: utf-8 -*-
"""
合唱管理器（你一句我一句）

- 歌词按句分配：AI 唱奇数句（示范），用户唱偶数句
- AI 唱完一句 → 通知前端"该你了" → 等用户唱完（前端 VAD 检测静音 → user_sung）
- 用户超时（20s）→ AI 代唱这句，不冷场、不卡死
- 不抢歌词：AI 绝不提前唱用户的句子
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

USER_SING_TIMEOUT = 20.0


class DuetSession:
    def __init__(self, lines, on_ai_sing, on_send_audio, on_send_event, user_timeout=USER_SING_TIMEOUT):
        self.lines = [l for l in (lines or []) if str(l).strip()]
        self.on_ai_sing = on_ai_sing
        self.on_send_audio = on_send_audio
        self.on_send_event = on_send_event
        self.user_timeout = user_timeout
        self._user_done = asyncio.Event()
        self._stopped = False

    def notify_user_done(self):
        """前端检测到用户唱完（VAD）→ 继续。"""
        self._user_done.set()

    def stop(self):
        self._stopped = True
        self._user_done.set()

    async def start(self):
        lines = self.lines
        total = len(lines)
        try:
            await self.on_send_event({
                "type": "duet_start",
                "total_lines": total,
                "message": "我先唱，你跟着来~",
            })
            for i, line in enumerate(lines):
                if self._stopped:
                    break
                if i % 2 == 0:
                    # AI 唱
                    await self.on_send_event({"type": "duet_ai_turn", "line": line, "line_index": i, "total_lines": total})
                    audio = await self.on_ai_sing(line, i)
                    if audio:
                        await self.on_send_audio(audio, {
                            "type": "singing_audio", "line": line, "line_index": i,
                            "turn": "ai", "is_last": (i == total - 1),
                        })
                    await asyncio.sleep(0.4)   # 唱完稍作停顿，给用户反应
                else:
                    # 用户唱：等前端 VAD 通知
                    self._user_done.clear()
                    await self.on_send_event({
                        "type": "duet_user_turn", "line": line, "line_index": i,
                        "total_lines": total, "message": f"该你啦~ 唱：{line}",
                    })
                    try:
                        await asyncio.wait_for(self._user_done.wait(), timeout=self.user_timeout)
                    except asyncio.TimeoutError:
                        logger.info(f"[合唱] 用户第{i}句超时，AI代唱")
                        await self.on_send_event({"type": "duet_user_timeout", "line_index": i})
                        audio = await self.on_ai_sing(line, i)
                        if audio:
                            await self.on_send_audio(audio, {
                                "type": "singing_audio", "line": line, "line_index": i,
                                "turn": "ai_helping", "is_last": (i == total - 1),
                            })
                    await asyncio.sleep(0.3)
            if not self._stopped:
                await self.on_send_event({"type": "duet_end", "message": "唱完了~ 好听吗？😊"})
        except Exception as e:
            logger.error(f"[合唱] 异常: {e}")
            try:
                await self.on_send_event({"type": "sing_error", "message": "合唱出了点问题…"})
            except Exception:
                pass
