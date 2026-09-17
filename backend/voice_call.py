# -*- coding: utf-8 -*-
"""
实时语音通话引擎 v2.0
新增：完整闭环（记忆提炼/亲密度/知识图谱/反思/情绪更新）
架构：WebSocket全双工 + 分片ASR + 流式LLM + 流式TTS（CosyVoice/MiniMax/火山/讯飞）
"""
import asyncio
import json
import time
import os
import re
from typing import Optional
from fastapi import WebSocket

# ── 静音检测配置
_SILENCE_THRESHOLD_MS = 220
_MAX_AUDIO_BUFFER_SEC = 10
_MIN_AUDIO_BUFFER_MS  = 220

# 兜底收句：前端 VAD 可能因设备增益/底噪全程没判出"说话"，此时
# _is_user_speaking 一直是 False，静音超时的分支永远进不去，音频只进不出 ——
# 界面就永久停在"正在聆听…"，AI 也永远不回应。攒够这么多字节就强制识别一次
# （约 1.5 秒 webm@64kbps），宁可判空也不要卡死。
_FALLBACK_ASR_BYTES = 12000

# ★ 前端发的是 webm/opus @64kbps（不是 16kHz/16bit PCM），按 PCM 公式
#   （bytes / 16000 / 2）算时长会低估 4 倍：10 秒阈值实际要 320000 字节
#   = 40 秒音频才触发，"最大缓冲区强制 ASR"这道兜底形同虚设。
_WEBM_BYTES_PER_SEC = 8000

# ★ 持续收到 is_speech=True 时同样要兜底。旧实现的 _FALLBACK_ASR_BYTES 只在
#   `if not is_speech` 分支里判，而前端所有块都带 speaking 标志 → 那个分支
#   永远进不去，底噪卡死时音频只进不出。给足余量，避免把正常长句切开。
_FALLBACK_ASR_BYTES_SPEAKING = 48000   # ≈ 6 秒 webm

# ASR 整体超时：阿里云链路里 ws.send() 没有超时保护，半开连接下可无限阻塞。
_ASR_OVERALL_TIMEOUT_SEC = 25

# ── TTS分句配置
_TTS_SPLIT_CHARS = ('。', '！', '？', '…', '\n', '!', '?', '～')
_TTS_MIN_CHARS   = 3
# ★ 2026-09-10：无标点时的兜底切分长度。首句短（尽快开口），后续长（合成吞吐
#   才跟得上语速）。依据：CosyVoice 每次合成固定开销 ≈2s，49 字长句 RTF≈0.9，
#   而 5~14 字短句 RTF 1.4~1.7（越播越落后）。
_TTS_FIRST_EMIT_CHARS = 8
_TTS_LATER_EMIT_CHARS = 40

# ── 通话结束后触发闭环的最少轮次
_MIN_ROUNDS_FOR_PIPELINE = 2


# 全局活跃通话会话注册表（屏幕感知联动 Q4）
_active_sessions: dict = {}

# ── 续唱意图识别 ──────────────────────────────────────────────────────────────
# 只在「上一首歌确实被打断过」的上下文里才判定（见 _resume_singing），
# 避免把普通的"继续""再来"误判成续唱。
_RESUME_SING_PAT = None


def _is_resume_sing_text(text: str) -> bool:
    """判断这句话是不是「继续唱 / 接着唱」。"""
    global _RESUME_SING_PAT
    t = str(text or "").strip()
    if not t or len(t) > 30:
        return False
    if _RESUME_SING_PAT is None:
        import re as _re
        _RESUME_SING_PAT = _re.compile(
            r"(继续|接着|再来|继续把|接着把)[^。！？，,]{0,8}(唱|来|一遍)|"
            r"(唱完它|唱完吧|接着唱|继续唱|刚才那首|刚那首|再来一遍|重新唱)|"
            r"^(继续|接着|再来)",
            _re.I,
        )
    return bool(_RESUME_SING_PAT.search(t))


def detect_screen_companion_intent(text: str) -> str:
    """识别通话里的画面陪伴意图：once / continuous / stop / 空字符串。

    规则刻意把“普通聊天陪伴”和“共享当前活动”区分开；但用户明确说
    “想要你陪我/跟我一起”时，按持续陪伴邀请处理。
    """
    value = re.sub(r"[\s，。！？、,.!?～~]", "", str(text or "").lower())
    if not value:
        return ""

    stop_patterns = (
        "别看了", "不要看了", "不用看了", "停止看屏", "关掉看屏",
        "关闭陪伴", "停止陪伴", "退出陪伴", "不用陪我了", "先别陪我",
        "别跟着我了", "不用跟我一起了",
    )
    if any(p in value for p in stop_patterns):
        return "stop"

    # 组合说法优先判定为持续陪伴：例如“陪我看看屏幕”“想要你陪我玩”。
    # 这样不会被后面的“看看屏幕”单次观察规则抢先匹配。
    if any(p in value for p in (
        "陪我一起", "想要你陪我", "我想让你陪我", "我想你陪我",
        "想要你陪着我", "我想让你陪着我", "我想你陪着我",
        "陪我看看", "陪我看", "陪我玩", "陪我刷", "陪我追", "陪我听",
        "陪我打游戏", "陪我学习", "陪我工作", "陪我写代码",
        "能和我一起", "可以和我一起", "要不要和我一起", "跟我一起",
        "和我一起", "一起玩", "一起看", "一起刷", "一起追剧", "一起听歌",
        "我干嘛你跟我一起", "我做什么你跟我一起", "我玩什么你跟我一起",
        "我想干嘛你跟我一起", "我想做什么你跟我一起", "我想玩什么你跟我一起",
        "我想看的你跟我一起", "我想玩的你跟我一起",
    )):
        return "continuous"

    once_patterns = (
        "看看屏幕", "看下屏幕", "看一下屏幕", "看我屏幕", "看看我屏幕",
        "瞅瞅屏幕", "瞧瞧屏幕", "看一眼屏幕", "看看画面", "看下画面",
        "看看我在干嘛", "看看我在干什么", "看看我在做什么", "看看我在看什么",
        "看看我在玩什么", "你看我在干嘛", "你看我在做什么", "你看我在玩什么",
        "帮我看看", "你看看这个", "看看这个游戏", "看看这个视频", "看看这个网页",
        "你知道我在干嘛吗", "猜猜我在干嘛", "看得到我屏幕吗", "能看到我屏幕吗",
    )
    if any(p in value for p in once_patterns):
        return "once"

    continuous_patterns = (
        "能和我一起", "可以和我一起", "要不要和我一起", "陪我一起",
        "想要你陪我", "我想让你陪我", "我想你陪我", "你陪着我",
        "陪在我旁边", "在旁边陪我", "跟我一起", "和我一起",
        "陪我玩", "陪我看", "陪我做", "陪我刷", "陪我追", "陪我听",
        "陪我打游戏", "陪我学习", "陪我工作", "陪我写代码",
        "一起玩", "一起看", "一起刷", "一起追剧", "一起听歌",
        "我干嘛你跟我一起", "我做什么你跟我一起", "我玩什么你跟我一起",
        "跟着我一起", "你也一起来", "我们一起吧",
    )
    if any(p in value for p in continuous_patterns):
        return "continuous"
    return ""


class VoiceCallSession:

    def __init__(
        self,
        ws: WebSocket,
        session_id: str,
        character_id: str,
        voice_key: str = "cosyvoice_default"
    ):
        self.ws           = ws
        self.session_id   = session_id
        self.character_id = character_id
        self.voice_key    = voice_key

        self._audio_buf        = bytearray()
        self._last_audio_ts    = 0.0
        self._is_user_speaking = False
        self._ai_speaking      = False
        self._interrupted      = False

        # 通话内对话记录（用于闭环处理）
        self._call_messages: list = []
        self._call_rounds:   int  = 0   # 完成轮次计数
        # 通话上下文采用“滚动摘要 + 最近原文”：摘要保留开头和关键事实，
        # 最近原文保证当前对话仍然自然，不再被固定20条窗口截断。
        self._call_summary: str = ""
        self._call_memory_task: Optional[asyncio.Task] = None

        self._tts_queue: asyncio.Queue = asyncio.Queue()
        self._silence_task: Optional[asyncio.Task] = None
        self._tts_task:     Optional[asyncio.Task] = None
        self._asr_inflight  = False
        # 当前 _audio_buf 里攒的是 PCM16 还是 webm（前端 AudioWorklet 直出 PCM）
        self._buf_is_pcm    = False
        # 流式 ASR 连接（begin_speech 时建连，说话中持续喂，end_speech 收结果）
        self._asr_stream    = None

        # 通话开始时间（用于记忆提炼的时间标注）
        self._call_start_time = time.time()

        # ★ 2026-09-10 延迟诊断：本回合起点 + 是否已记录"首音频"
        #   验收口径：用户说完 → 前端收到第一个音频块 的耗时（目标 1~3s）
        self._turn_t0 = 0.0
        self._tts_first_logged = True
        self._last_frame_ts = 0.0      # 诊断：最后一帧音频到达时间
        self._bg_tasks: set = set()    # 后台收尾任务（关连接等），防被 GC

        # ★ 2026-09-10 通话快车道：缓存住"重上下文块"（人设+记忆+关系+场景），
        #   让每句话的回复不必先等 5~8s 的辅助 LLM 调用。
        #   块内内容由后台任务用同一套 enrich_messages 刷新（副作用一个不少），
        #   只把"注入本句 prompt"这一步延后一回合。
        self._ctx_block: str = ""
        self._ctx_task: Optional[asyncio.Task] = None
        self._ctx_pending_text: str = ""
        self._ctx_warming: bool = False     # 预热是否已发起（避免重复跑副作用）

        # 通话来源：用户拨打 / AI 来电（通话记录用）
        self._call_source = "user_initiated"

        # ★ 唱歌系统（B/C方案）：当前合唱会话 + 唱歌状态
        self.duet_session = None
        # ★ 唱歌断点：{"song_name","artist","is_duet","total","index","ts"}
        #   唱歌被打断时记录进度，用户说「继续唱」就从断点接着唱
        self._sing_ctx = None
        self._is_singing  = False

        # 通话时的屏幕感知状态（Q4：AI 通话中能看到屏幕）
        self._screen_state: dict = {}
        # ★ 游戏陪玩：用户说话时截图 + 视觉分析结果（游戏画面感知）
        self._screen_analysis: dict = {}
        self._screen_capture_task: Optional[asyncio.Task] = None
        self._screen_watch_task: Optional[asyncio.Task] = None
        self._last_screen_capture_ts = 0.0
        # 通话界面的“看屏幕”按钮开启后才持续感知；默认关闭，保护隐私。
        self._screen_watch_enabled = False
        # 注册到全局活跃会话表，供屏幕感知联动注入
        _active_sessions[(self.session_id, self.character_id)] = self

    # ──────────────────────────────────────────────
    # 公开方法
    # ──────────────────────────────────────────────

    async def handle_audio_chunk(self, chunk: bytes, is_speech: bool = True,
                                 is_pcm: bool = False):
        # ★ 新架构：前端 AudioWorklet 直出 PCM16，收句由前端 VAD 决定，
        #   后端只负责按 begin_speech/end_speech 攒与触发，不再自算静音。
        if is_pcm:
            self._audio_buf.extend(chunk)
            self._buf_is_pcm = True
            self._last_frame_ts = time.time()      # 诊断：最后一帧到达时间
            # 流式 ASR：说话的同时把 PCM 实时喂给 DashScope，边说话边识别
            if self._asr_stream is not None:
                try:
                    await self._asr_stream.feed(chunk)
                except Exception:
                    pass
            return
        if is_speech and self._ai_speaking:
            self._interrupted = True
            await self._send_event("ai_interrupted")
            self._ai_speaking = False
            # ★ 合唱被打断：停掉 duet 会话，否则它还挂着等用户唱下一句
            if self.duet_session:
                try:
                    self.duet_session.stop()
                except Exception:
                    pass

        if not self._is_user_speaking and not is_speech and len(self._audio_buf) >= 65536:
            return
        self._audio_buf.extend(chunk)
        if not is_speech:
            # 前端 VAD 即使漏发显式 silence，只要之前确实开始说话，连续静音块
            # 到来时后端也主动收句，避免界面永久停在“正在聆听”。
            if (self._is_user_speaking and self._last_audio_ts
                    and time.time() - self._last_audio_ts >= _SILENCE_THRESHOLD_MS / 1000):
                if self._silence_task and not self._silence_task.done():
                    self._silence_task.cancel()
                self._silence_task = asyncio.create_task(self._trigger_asr())
            elif len(self._audio_buf) >= _FALLBACK_ASR_BYTES:
                # ★ 兜底：VAD 全程没判出说话时，攒够一段就强制识别一次
                if self._silence_task and not self._silence_task.done():
                    self._silence_task.cancel()
                self._silence_task = asyncio.create_task(self._trigger_asr())
            return
        self._last_audio_ts    = time.time()
        self._is_user_speaking = True

        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

        # ★ 修复：用 webm 字节率算时长。沿用 PCM 公式会让 10 秒阈值实际需要
        #   40 秒音频才触发，"最大缓冲区"这道最后兜底形同虚设。
        buf_sec = len(self._audio_buf) / _WEBM_BYTES_PER_SEC
        # ★ 修复：持续 is_speech=True 时也要兜底。旧实现只在上面的
        #   `if not is_speech` 分支里判 _FALLBACK_ASR_BYTES，而前端把
        #   所有块都标成 speaking，那个分支永远进不去 → 音频只进不出。
        if (buf_sec >= _MAX_AUDIO_BUFFER_SEC
                or len(self._audio_buf) >= _FALLBACK_ASR_BYTES_SPEAKING):
            await self._trigger_asr()
        else:
            self._silence_task = asyncio.create_task(
                self._silence_timer()
            )

    # ──────────────────────────────────────────────
    # 新协议：前端 VAD 驱动（speech_start / speech_end / interrupt）
    # ──────────────────────────────────────────────

    async def begin_speech(self):
        """前端 VAD 判定用户开口：清空上一句缓冲，并立即建流式 ASR 连接，
        让用户说话的同时服务端就开始识别，缩短收句后的等待。"""
        self._audio_buf = bytearray()
        self._buf_is_pcm = True
        self._is_user_speaking = True
        self._last_audio_ts = time.time()
        await self._start_asr_stream()

    async def _start_asr_stream(self):
        """按 call_asr_provider 建流式 ASR 连接；失败/非 aliyun 则回 None，
        end_speech 时走 _trigger_asr 的一次性转写兜底。"""
        # 先关掉上一个未结束的流式连接（打断 / 连续说话场景会残留）
        if self._asr_stream is not None:
            try:
                await self._asr_stream.close()
            except Exception:
                pass
            self._asr_stream = None
        try:
            from . import config as _cfg
            provider = (_cfg.call_asr_provider() or "aliyun").lower()
            if provider != "aliyun":
                return
            key = _cfg.dashscope_api_key()
            if not key:
                return
            from .asr import AliyunAsrStream
            self._asr_stream = AliyunAsrStream(
                key, _cfg.aliyun_asr_model() or "paraformer-realtime-v2")
            ok = await self._asr_stream.start()
            if not ok:
                await self._asr_stream.close()
                self._asr_stream = None
        except Exception as e:
            print(f"[VoiceCall] 流式ASR启动失败，降级一次性识别: {e}", flush=True)
            self._asr_stream = None

    async def end_speech(self):
        """前端 VAD 判定用户说完：触发 ASR（优先流式结果）；过短且无流式则判空。"""
        self._is_user_speaking = False
        # ★ 延迟诊断（2026-09-10）：说话结束到开始处理之间有没有被别的东西堵住
        _gap = (time.time() - self._last_frame_ts) if self._last_frame_ts else -1
        print(f"[VoiceCall][T] 收到 speech_end @{time.strftime('%H:%M:%S')}."
              f"{int(time.time() * 1000) % 1000:03d}（距最后一帧 {_gap:.2f}s）", flush=True)
        if self._asr_stream is None and len(self._audio_buf) < 500:
            # 无流式 ASR 且音频过短：直接判空，别卡在"聆听中"
            await self._send_event("asr_empty", {"bytes": len(self._audio_buf)})
            self._audio_buf = bytearray()
            return
        await self._trigger_asr()

    async def interrupt(self):
        """用户打断：置中断标志，停 TTS/唱歌，并推 ai_interrupted 让前端停播。"""
        self._interrupted = True
        self._ai_speaking = False
        await self._send_event("ai_interrupted")
        if self.duet_session:
            try:
                self.duet_session.stop()
            except Exception:
                pass
        # 清掉 TTS 队列里尚未播出的句子
        while not self._tts_queue.empty():
            try:
                self._tts_queue.get_nowait()
            except Exception:
                pass

    async def handle_text_input(self, text: str):
        text = text.strip()
        if not text:
            return
        await self._send_event("user_text", {"text": text})
        await self._process_user_input(text)

    async def start_greeting(self):
        """接通后开口说第一句话 —— 由模型根据人设/关系/时间/谁打给谁自己决定。

        原来是按性格关键词硬编码的几句（"喂，我在呢。"之类），说来说去就那几句，
        也不管是清晨还是深夜、是对方打来还是自己打过去。现在交给模型生成，
        生成失败或超时一律回退兜底文案，保证接通后一定有声音。
        """
        self._ai_speaking = True
        self._interrupted = False
        # ★ 快车道预热：接通瞬间就在后台把"语义分析+场景 pipeline"跑一遍，
        #   这样用户开口时（至少几秒后）缓存已经就绪，第一句也是秒回。
        #   放在生成开场白之前，给预热留出最多的时间。
        if self._fast_context_enabled():
            self._warm_call_context()
        await self._send_event("ai_thinking")
        text = await self._generate_greeting_text()
        await self._send_event("ai_text_chunk", {"text": text, "idx": 0})
        await self._tts_queue.put({"text": text, "idx": 0, "final": True})
        await self._tts_queue.put({"text": "", "idx": -1, "final": True, "done": True})

    def _fallback_greeting(self) -> str:
        """开场白兜底文案（模型不可用/超时时使用）。"""
        text = "喂，我在呢。" if self._call_source == "user_initiated" else "喂，是我呀。"
        try:
            from .character_manager import get_character
            cfg = get_character(self.character_id) or {}
            personality = str(cfg.get("personality") or "")
            if "傲娇" in personality:
                text = "喂……我接了。才没有一直等你。" if self._call_source == "user_initiated" else "喂，接我电话啦，我就想听听你声音。"
            elif any(k in personality for k in ("活泼", "元气", "开朗", "俏皮")):
                text = "喂喂，我在呀！" if self._call_source == "user_initiated" else "喂！是我呀，终于接啦。"
            elif any(k in personality for k in ("温柔", "治愈", "体贴", "安静")):
                text = "喂，我在听呢。" if self._call_source == "user_initiated" else "喂，是我。想听听你的声音。"
            elif any(k in personality for k in ("清冷", "冷淡", "克制")):
                text = "喂，我在。你说。" if self._call_source == "user_initiated" else "喂，是我。现在方便说话吗？"
        except Exception:
            pass
        return text

    def _brain_model(self) -> str:
        """通话生成模型（2026-09-11）：角色卡主脑（人格设置）优先，无卡回退全局。
        ★ 之前 6 处直接用 selected_model()（全局兜底），角色在人格设置里换的主脑通话不生效。"""
        try:
            from .chat_logic import pick_model
            return pick_model(None, True, self.character_id)
        except Exception:
            from . import config as _cfg
            return _cfg.selected_model()

    def _light_model(self) -> str:
        """通话内轻任务（记忆压缩/提炼）：角色卡 memory_model > 全局 MEMORY_EXTRACT_MODEL。"""
        try:
            from . import config as _cfg
            return _cfg.memory_extract_model(self.character_id)
        except Exception:
            from . import config as _cfg
            return _cfg.selected_model()

    def _greeting_prompt(self) -> list:
        """给模型足够情境，让它自己决定开口第一句说什么。"""
        from datetime import datetime
        hour = datetime.now().hour
        if 5 <= hour < 11:
            period = "清晨"
        elif 11 <= hour < 14:
            period = "中午"
        elif 14 <= hour < 18:
            period = "下午"
        elif 18 <= hour < 23:
            period = "晚上"
        else:
            period = "深夜"

        char_name, personality, call_user, relationship = "AI", "", "你", "朋友"
        try:
            from .character_manager import get_character
            cfg = get_character(self.character_id) or {}
            char_name    = cfg.get("character_name", "AI")
            personality  = str(cfg.get("personality") or "")
            call_user    = cfg.get("call_user", "你")
            relationship = cfg.get("relationship", "朋友")
        except Exception:
            pass

        who = ("对方主动打给你，你刚接起来"
               if self._call_source == "user_initiated"
               else "你主动打给对方，对方刚接起来")

        return [
            {"role": "system", "content": (
                f"你是{char_name}，现在正在和{call_user}通电话。\n"
                f"你的人设：{personality or '真诚自然'}。\n"
                f"你和对方的关系：{relationship}。\n"
                f"你称呼对方为「{call_user}」。"
            )},
            {"role": "user", "content": (
                f"现在是{period}。{who}。\n"
                "请写出你接起电话后说的第一句话。\n"
                "要求：\n"
                "1. 像真人接电话那样自然，一两句，15~30 字以内\n"
                "2. 只输出你说出口的话，不要描写动作神态、不要括号、不要旁白\n"
                "3. 不要自称 AI，不要客服腔（如“请问有什么可以帮您”）\n"
                "4. 直接输出这句台词本身，不要解释、不要加引号"
            )},
        ]

    @staticmethod
    def _clean_greeting(raw: str) -> str:
        """清理模型输出：去引号、去括号动作描写、只留第一句。"""
        import re as _re
        t = str(raw or "").strip().strip('"\'“”‘’')
        if not t:
            return ""
        # 去掉 (笑)／（轻声）之类，打电话时念出来很怪
        t = _re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", t)
        t = _re.split(r"[\n。！？!?；;]", t)[0].strip()
        return t[:40]

    async def _generate_greeting_text(self) -> str:
        """生成开场白；任何异常或超时都回退兜底文案。"""
        fallback = self._fallback_greeting()
        try:
            from . import config as _cfg
            from .deepseek_api import chat_once
            key = _cfg.chat_key()
            if not key:
                return fallback
            raw = await asyncio.wait_for(
                chat_once(self._brain_model(), self._greeting_prompt(), key,
                          temperature=1.0, max_tokens=60),
                timeout=8,
            )
            return self._clean_greeting(raw) or fallback
        except Exception as e:
            print(f"[VoiceCall] 开场白生成失败，回退兜底: {type(e).__name__}: {e}", flush=True)
            return fallback

    async def handle_silence(self):
        """前端 VAD 检测到用户停说 → 触发 ASR（否则 MediaRecorder 静音块会一直重置静音计时器）"""
        if self._audio_buf:
            await self._trigger_asr()

    async def start_tts_worker(self):
        self._tts_task = asyncio.create_task(self._tts_worker())

    async def stop(self):
        """结束通话，触发完整闭环处理"""
        # 从全局活跃会话表注销（屏幕感知联动 Q4）
        _active_sessions.pop((self.session_id, self.character_id), None)
        self._screen_watch_enabled = False
        if self._screen_watch_task and not self._screen_watch_task.done():
            self._screen_watch_task.cancel()
        if self._screen_capture_task and not self._screen_capture_task.done():
            self._screen_capture_task.cancel()
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()
        # 关闭流式 ASR 连接（挂断时可能正开着）
        if self._asr_stream is not None:
            try:
                await self._asr_stream.close()
            except Exception:
                pass
            self._asr_stream = None
        while not self._tts_queue.empty():
            try:
                self._tts_queue.get_nowait()
            except Exception:
                pass

        # ★ 通话记录写入（call_records 表，供 /api/call/history 查询）
        try:
            from . import db as _db
            _end      = time.time()
            _duration = int(_end - self._call_start_time)
            _db.q(
                """INSERT INTO call_records
                   (session_id, character_id, start_time, end_time, duration_sec, rounds, call_source, call_result)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self.session_id,
                    self.character_id,
                    self._call_start_time,
                    _end,
                    _duration,
                    self._call_rounds,
                    getattr(self, '_call_source', 'user_initiated'),
                    'completed',
                )
            )
        except Exception as e:
            print(f"[VoiceCall] 通话记录写入失败(静默): {e}", flush=True)

        # ★ 通话结束后触发完整闭环（异步，不阻塞挂断）
        if self._call_rounds >= 1:
            asyncio.create_task(
                self._run_post_call_pipeline()
            )

    # ──────────────────────────────────────────────
    # 内部：静音检测 → ASR
    # ──────────────────────────────────────────────

    async def _silence_timer(self):
        await asyncio.sleep(_SILENCE_THRESHOLD_MS / 1000)
        await self._trigger_asr()

    async def _trigger_asr(self):
        # 端点检测和显式 silence 可能同时到达；同一段音频只允许一次识别。
        if self._asr_inflight:
            return
        self._is_user_speaking = False
        audio_data = bytes(self._audio_buf)
        buf_was_pcm = self._buf_is_pcm
        self._audio_buf = bytearray()
        self._buf_is_pcm = False

        # ★ 修复：前端发的是 webm/opus 压缩音频（不是 PCM），
        #   按 PCM 算 min_bytes（9600）webm 永远达不到 → ASR 永不触发（听不见用户）。
        #   改为：只要有足够压缩数据（≈500ms webm）就尝试转写。
        # 180ms 的短答在 64kbps 下通常约 1.4KB；保留容器头后 500B 已足够尝试。
        # 宁可交给 ASR 判空，也不要把“嗯/喂/在”这类真实短句在入口直接丢弃。
        # ★ 流式 ASR 已在说话时同步识别，即使缓冲很短也可能有结果，跳过过短判断。
        min_bytes = 500
        if self._asr_stream is None and len(audio_data) < min_bytes:
            # ★ 修复静默黑洞：旧实现直接 return，既不清状态也不发任何事件，
            #   前端收不到 asr_start / asr_empty，状态会永久冻结在"正在聆听…"。
            #   补发 asr_empty，让界面能复位并提示用户再说一次。
            print(f"[VoiceCall][ASR] 音频过短已丢弃({len(audio_data)}B)", flush=True)
            await self._send_event("asr_empty", {"bytes": len(audio_data)})
            return

        self._asr_inflight = True
        await self._send_event("asr_start")
        _t_asr0 = time.time()

        text = ""
        # ★ 流式 ASR 优先：begin_speech 时已建连并持续喂 PCM（边说话边识别），
        #   这里只需 finish 收最终结果，省掉"说完整句 → 建连 → 识别整句"的等待。
        _stream = self._asr_stream
        self._asr_stream = None
        if _stream is not None:
            try:
                text = (await asyncio.wait_for(
                    _stream.finish(), timeout=_ASR_OVERALL_TIMEOUT_SEC)) or ""
                if text:
                    print(f"[VoiceCall][ASR] 流式成功: {text[:40]!r} "
                          f"（收尾段 {time.time() - _t_asr0:.2f}s）", flush=True)
            except Exception as e:
                print(f"[VoiceCall][ASR] 流式收尾失败: {e}", flush=True)
                text = ""
            finally:
                # ★ 2026-09-10 关键 1 秒：关闭 DashScope 的 WebSocket 要走一遍挥手，
                #   实测约 1.0 秒。它原先被 await 在"发 asr_result 之前"，
                #   等于用户每说完一句都白等 1 秒才开始出字。
                #   改成后台关连接：结果立刻发前端，连接随后自己收掉。
                try:
                    async def _close_bg(_s=_stream):
                        try:
                            await _s.close()
                        except Exception:
                            pass
                    _ct = asyncio.create_task(_close_bg())
                    self._bg_tasks.add(_ct)
                    _ct.add_done_callback(self._bg_tasks.discard)
                except Exception:
                    pass

        # 流式结果为空 / 未启用 → 降级一次性转写（aliyun / whisper 兜底）
        if not text or not text.strip():
            try:
                from . import asr as _asr
                text = await asyncio.wait_for(
                    _asr.transcribe(
                        audio_data,
                        filename="audio.webm",
                        mode="call",
                        input_format="pcm16" if buf_was_pcm else "auto",
                    ),
                    timeout=_ASR_OVERALL_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                print(f"[VoiceCall][ASR] 整体超时{_ASR_OVERALL_TIMEOUT_SEC}s"
                      f"({len(audio_data)}B)", flush=True)
                self._asr_inflight = False
                await self._send_event("asr_error", {"error": "识别超时，请再说一次"})
                return
            except Exception as e:
                print(f"[VoiceCall][ASR] 失败({len(audio_data)}B): {e}", flush=True)
                self._asr_inflight = False
                await self._send_event("asr_error", {"error": str(e)[:120]})
                return

        self._asr_inflight = False

        if not text or not text.strip():
            # ★ 诊断保存：把这次 ASR 的原始 webm 落到磁盘，1 秒就能确认
            #   "听不见"到底是 mic 没录到 / 解码挂了 / 真的没说话。
            #   用毫秒时间戳避免重名；保留最近 10 个避免磁盘膨胀。
            try:
                from . import config as _cfg_dbg
                _debug_dir = _cfg_dbg.DATA_DIR / "debug_audio"
                _debug_dir.mkdir(parents=True, exist_ok=True)
                _ts = int(time.time() * 1000)
                _p = _debug_dir / f"asr_empty_{_ts}.webm"
                _p.write_bytes(audio_data)
                print(f"[VoiceCall][ASR] 原始 webm 已保存: {_p}", flush=True)
                _all = sorted(_debug_dir.glob("asr_empty_*.webm"),
                               key=lambda f: f.stat().st_mtime)
                for _old in _all[:-10]:
                    try: _old.unlink()
                    except Exception: pass
            except Exception as _de:
                print(f"[VoiceCall][ASR] 调试音频保存失败(静默): {_de}", flush=True)
            # ★ 区分"根本没录到音频"和"录到了但识别为空"，否则无法定位
            print(f"[VoiceCall][ASR] 转写为空({len(audio_data)}B)", flush=True)
            await self._send_event("asr_empty", {"bytes": len(audio_data)})
            return

        text = text.strip()
        print(f"[VoiceCall][ASR] 成功({len(audio_data)}B): {text!r}", flush=True)
        await self._send_event("asr_result", {"text": text})
        # ASR 已完成就释放占用（finally 已复位）；LLM/TTS期间用户再次开口
        # 仍可被识别并打断。
        await self._process_user_input(text)

    # ──────────────────────────────────────────────
    # 内部：LLM流式生成
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # 内部：游戏陪玩（A/B 方案：场景感知 + 说话时截图视觉）
    # ──────────────────────────────────────────────

    async def _capture_screen_analysis(self, force: bool = False) -> bool:
        """
        用户说话时：截一张图 + qwen-vl 视觉分析（游戏/抖音画面感知）。
        - 需要视觉 API Key（没有则跳过）
        - analyze_screen 内置 30s 冷却，避免每句都调
        - 任何失败静默，绝不影响回复
        """
        try:
            from . import config as _cfg
            if not force and (
                not _cfg.get("AWARENESS_ENABLED", False)
                or not _cfg.get("SCREEN_PERCEPTION_ENABLED", False)
            ):
                return False
            if not _cfg.vision_key():
                if force:
                    await self._send_event("screen_watch_error", {
                        "message": "还没有配置视觉模型 Key，暂时看不到画面",
                    })
                return False
            from .proactive.screen_capture import capture_local, save_capture
            from .proactive.vision_analyzer import analyze_screen
            raw = await asyncio.to_thread(capture_local)
            if not raw:
                if force:
                    await self._send_event("screen_watch_error", {"message": "没有截取到屏幕画面"})
                return False
            path = await asyncio.to_thread(save_capture, raw)
            if not path:
                return False
            result = await asyncio.to_thread(analyze_screen, path, "你", force)
            if result:
                self._screen_analysis = result
                self._screen_state = dict(result)
                self._screen_state.update({
                    "session_id": self.session_id,
                    "character_id": self.character_id,
                })
                # ★ 推给前端显示「画面感知」状态条（C 方案）
                try:
                    await self._send_event("scene_update", {
                        "summary": str(result.get("summary", "") or ""),
                        "focus":   str(result.get("focus", "") or ""),
                        "scene_name": str(result.get("scene_name", "") or ""),
                        "activity": str(result.get("activity", "") or ""),
                    })
                except Exception:
                    pass
                print(f"[VoiceCall] 画面感知: {result.get('summary', '')[:40]}", flush=True)
                return True
            if force:
                await self._send_event("screen_watch_error", {
                    "message": "这次没有看清画面，稍后可以再试一次",
                })
        except Exception as e:
            print(f"[VoiceCall] 画面感知失败(静默): {e}", flush=True)
            if force:
                await self._send_event("screen_watch_error", {"message": "画面陪伴暂时不可用"})
        return False

    async def _screen_watch_loop(self):
        """通话陪伴开启后按设置频率更新画面，不依赖用户再次开口。"""
        try:
            while self._screen_watch_enabled:
                from . import config as _cfg
                interval = max(30, int(_cfg.get("SCREEN_INTERVAL", 300) or 300))
                await asyncio.sleep(interval)
                if not self._screen_watch_enabled:
                    break
                await self._capture_screen_analysis(force=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[VoiceCall] 陪伴观察循环失败(静默): {e}", flush=True)

    async def set_screen_watch(self, enabled: bool) -> bool:
        """切换通话中的持续看屏幕状态；开启时立即请求一次画面分析。"""
        from . import config as _cfg
        if enabled and (
            not _cfg.get("AWARENESS_ENABLED", False)
            or not _cfg.get("SCREEN_PERCEPTION_ENABLED", False)
        ):
            self._screen_watch_enabled = False
            await self._send_event("screen_watch", {"enabled": False})
            await self._send_event("screen_watch_error", {
                "message": "请先在设置里开启“感知总开关”和“截图分析”",
            })
            return False
        if enabled and not _cfg.vision_key():
            self._screen_watch_enabled = False
            await self._send_event("screen_watch", {"enabled": False})
            await self._send_event("screen_watch_error", {
                "message": "请先在设置里配置视觉模型 Key",
            })
            return False

        self._screen_watch_enabled = bool(enabled)
        await self._send_event("screen_watch", {
            "enabled": self._screen_watch_enabled,
            "message": "我来陪你一起" if self._screen_watch_enabled else "已结束画面陪伴",
        })
        if self._screen_watch_enabled:
            if not self._screen_capture_task or self._screen_capture_task.done():
                self._last_screen_capture_ts = time.time()
                self._screen_capture_task = asyncio.create_task(
                    self._capture_screen_analysis(force=True)
                )
            if not self._screen_watch_task or self._screen_watch_task.done():
                self._screen_watch_task = asyncio.create_task(self._screen_watch_loop())
        elif self._screen_watch_task and not self._screen_watch_task.done():
            self._screen_watch_task.cancel()
        return self._screen_watch_enabled

    def _life_companion_enabled(self) -> bool:
        """通话自动截图仅在“生活陪伴”模式开启时启用。"""
        try:
            from . import db as _db
            raw = _db.kv_get(f"companion_mode:{self.session_id}:{self.character_id}")
            if not raw:  # 兼容旧版本数据
                raw = _db.kv_get(f"companion_mode:{self.session_id}")
            if not raw:
                return False
            try:
                mode = (json.loads(raw) or {}).get("mode", "")
            except Exception:
                mode = str(raw)
            return mode in {"play", "douyin", "drama", "music", "night"}
        except Exception:
            return False

    def _companion_mode(self) -> str:
        try:
            from . import db as _db
            raw = _db.kv_get(f"companion_mode:{self.session_id}:{self.character_id}") \
                or _db.kv_get(f"companion_mode:{self.session_id}")
            if not raw:
                return ""
            try:
                return str((json.loads(raw) or {}).get("mode", ""))
            except Exception:
                return str(raw)
        except Exception:
            return ""

    async def _process_user_input(self, user_text: str):
        # 电话与文字聊天共享同一套内隐心情，通话内容也会改变后续聊天语气。
        try:
            from . import ai_mood as _call_mood
            _call_mood.detect_user_message(user_text, self.session_id, self.character_id)
        except Exception:
            pass
        # ★ 续唱：上一首歌被打断过，用户说「继续唱」→ 从断点接着唱。
        #   必须排在唱歌意图检测之前，否则「继续唱」会先被当成新的一首从头唱。
        try:
            if self._sing_ctx and _is_resume_sing_text(user_text):
                await self._resume_singing()
                return
        except Exception as _rse:
            print(f"[VoiceCall] 续唱检测失败(静默): {_rse}", flush=True)

        # ★ 唱歌意图检测（B/C方案）：通话中请求唱歌 → 唱歌流程（普通唱 / 合唱）
        try:
            from .singing.intent_detector import detect_sing_intent
            _intent = detect_sing_intent(user_text)
            if _intent.is_sing:
                await self._start_singing(_intent)
                return
        except Exception as _se:
            print(f"[VoiceCall] 唱歌意图检测失败(静默): {_se}", flush=True)

        # ★ 游戏陪玩：用户说话时截一张图 + 视觉分析（画面感知，可选）
        screen_intent = detect_screen_companion_intent(user_text)
        if screen_intent == "stop":
            await self.set_screen_watch(False)
        elif screen_intent == "continuous":
            enabled = await self.set_screen_watch(True)
            if not enabled:
                # 这句话仍算一次明确授权；持续开关未满足时，仅尝试当前这一张。
                await self._capture_screen_analysis(force=True)
        elif screen_intent == "once":
            try:
                # 明确说“看看”属于一次性授权，不要求打开持续感知开关。
                await self._capture_screen_analysis(force=True)
            except Exception:
                pass
        elif self._screen_watch_enabled or self._life_companion_enabled():
            # 生活陪伴开启时后台更新屏幕理解，不阻塞当前通话回复。
            now = time.time()
            task_running = self._screen_capture_task and not self._screen_capture_task.done()
            if not task_running and now - self._last_screen_capture_ts >= 15:
                self._last_screen_capture_ts = now
                self._screen_capture_task = asyncio.create_task(self._capture_screen_analysis())

        # ★ 阶段2：Minecraft Bot 语音联动（Bot 在线 + 用户说游戏指令 → Bot 执行）
        try:
            from .minecraft.bot_controller import get_controller as _mc_ctrl, is_mc_command
            _mc = _mc_ctrl(self.session_id, self.character_id)
            _mc_mode = self._companion_mode() == "minecraft"
            if _mc.bot_online and is_mc_command(user_text, active_context=_mc_mode):
                _snap = _mc.get_latest_snapshot() or {}
                _res = await _mc.push_voice_command(_snap, user_text)
                _reply = ""
                if _res and isinstance(_res, dict):
                    _reply = str(_res.get("chat", "") or "")
                if _reply:
                    await self._tts_queue.put({"text": _reply, "idx": 0, "final": True})
                    await self._tts_queue.put({"text": "", "idx": -1, "final": True, "done": True})
                    self._call_messages.append({"role": "assistant", "content": _reply})
                    await self._maybe_compact_call_memory()
                elif _res and _res.get("queued") is False:
                    _busy_reply = str(_res.get("error") or "上一条游戏指令还在执行，等我一下～")
                    await self._tts_queue.put({"text": _busy_reply, "idx": 0, "final": True})
                    await self._tts_queue.put({"text": "", "idx": -1, "final": True, "done": True})
                return
        except Exception as _mce:
            print(f"[VoiceCall] MC 联动失败(静默): {_mce}", flush=True)

        # ★ 本回合计时起点（"用户说完 → 首音频"的验收口径从这里算）
        self._turn_t0 = time.time()
        self._tts_first_logged = False
        from .deepseek_api import llm_call_mark as _llm_mark, llm_calls_since as _llm_since
        _t_turn = self._turn_t0
        _llm0   = _llm_mark()

        messages = await self._build_call_messages(user_text)
        _t_ctx   = time.time() - _t_turn
        _n_ctx   = _llm_since(_llm0)
        self._call_messages.append({"role": "user", "content": user_text})

        await self._send_event("ai_thinking")
        self._ai_speaking = True
        self._interrupted = False
        _t_reply = time.time()
        _t_first_tok = 0.0

        try:
            from . import config as _config
            from .deepseek_api import chat_stream as _chat_stream
            try:
                from .tts import prepare_visible_text as _prepare_visible_text
            except Exception:
                _prepare_visible_text = lambda x: str(x or "")

            full_reply   = ""
            sentence_buf = ""
            sentence_idx = 0

            # ★ 2026-09-10 通话提速：GLM-5.3 系列默认先"深思"再开口——实测首字
            #   2.3~3.5s、整句 5.7s；传 reasoning_effort="low" 后首字 0.6s、
            #   整句 0.9s。打电话没人等它深思，短句回复也不需要。
            #   仅对支持该参数的模型传（其它模型传了会报错）。
            _reply_kw = {}
            try:
                _reply_model = self._brain_model()
                if _config.model_supports_reasoning_effort(_reply_model):
                    _reply_kw["reasoning_effort"] = "low"
            except Exception:
                _reply_model = self._brain_model()

            async for chunk in _chat_stream(
                model=_reply_model,
                messages=messages,
                api_key=_config.chat_key(),
                temperature=0.75,
                max_tokens=250,
                **_reply_kw,
            ):
                if self._interrupted:
                    break

                if _t_first_tok <= 0:
                    _t_first_tok = time.time() - _t_reply

                full_reply   += chunk
                sentence_buf += chunk

                # ★ AI 控制电脑：剥离 [ACTION] 标记并异步执行动作（通话中也能控制电脑）
                try:
                    import json as _json
                    import re as _re
                    _pat = _re.compile(r"\[ACTION\](.*?)\[/ACTION\]", _re.DOTALL)
                    while True:
                        _m = _pat.search(sentence_buf)
                        if not _m:
                            break
                        try:
                            _act = _json.loads(_m.group(1).strip())
                            if isinstance(_act, dict) and _act.get("type"):
                                from . import control
                                asyncio.create_task(control.execute(_act, self.session_id, self.character_id))
                        except Exception:
                            pass
                        sentence_buf = sentence_buf[:_m.start()] + sentence_buf[_m.end():]
                except Exception:
                    pass

                for char in _TTS_SPLIT_CHARS:
                    if char in sentence_buf:
                        parts    = sentence_buf.split(char, 1)
                        sentence = parts[0].strip() + char
                        sentence_buf = parts[1] if len(parts) > 1 else ""
                        visible_sentence = _prepare_visible_text(sentence).strip()

                        if len(visible_sentence) >= _TTS_MIN_CHARS:
                            await self._send_event(
                                "ai_text_chunk",
                                {"text": visible_sentence, "idx": sentence_idx}
                            )
                            await self._tts_queue.put({
                                "text":  visible_sentence,
                                "idx":   sentence_idx,
                                "final": False
                            })
                            sentence_idx += 1
                        break

                # 模型暂时没有标点时，达到一定长度也先播出，避免一直等整段。
                # ★ 2026-09-10 分句策略（按实测数据调）：
                #   CosyVoice 每次合成有 ~2 秒**固定开销**（2字=2.0s、14字=2.9s、
                #   49字=10.0s），所以：
                #     · 首句要**短** → 尽快开口（固定开销吃掉大头，短句也差不多 2s）
                #     · 后续要**长** → 短句 RTF 1.4~1.7 会越播越落后；长句 RTF≈0.9
                #       才跟得上语速（旧实现一律 8 字一切，一句话被切成 4~6 段，
                #       合成吞吐跟不上，听感就是"她说话越来越慢/拖"）
                _emit_chars = _TTS_FIRST_EMIT_CHARS if sentence_idx == 0 else _TTS_LATER_EMIT_CHARS
                if len(sentence_buf.strip()) >= _emit_chars:
                    sentence = sentence_buf.strip()
                    sentence_buf = ""
                    visible_sentence = _prepare_visible_text(sentence).strip()
                    if len(visible_sentence) >= _TTS_MIN_CHARS:
                        await self._send_event(
                            "ai_text_chunk", {"text": visible_sentence, "idx": sentence_idx}
                        )
                        await self._tts_queue.put({
                            "text": visible_sentence, "idx": sentence_idx, "final": False
                        })
                        sentence_idx += 1

            # 处理尾句
            if not self._interrupted and sentence_buf.strip() \
               and len(sentence_buf.strip()) >= _TTS_MIN_CHARS:
                visible_tail = _prepare_visible_text(sentence_buf).strip()
                if len(visible_tail) >= _TTS_MIN_CHARS:
                    await self._send_event(
                        "ai_text_chunk",
                        {"text": visible_tail, "idx": sentence_idx}
                    )
                    await self._tts_queue.put({
                        "text":  visible_tail,
                        "idx":   sentence_idx,
                        "final": True
                    })
                full_reply += sentence_buf

            await self._tts_queue.put(
                {"text": "", "idx": -1, "final": True, "done": True}
            )

            # ★ 延迟验收（2026-09-10）：一行看清这一句的"思考税"花在哪。
            #   上下文=拼 prompt 的耗时（快车道下应≈0，且其中 LLM 调用应为 0）
            #   首字=模型出第一个字的耗时；说→文本=用户说完到回复文本就绪
            print(f"[VoiceCall][TIMING] 上下文={_t_ctx:.2f}s(其中LLM×{_n_ctx}) "
                  f"首字={_t_first_tok:.2f}s 全文={time.time() - _t_reply:.2f}s "
                  f"| 说→文本={time.time() - _t_turn:.2f}s", flush=True)

            if full_reply.strip():
                visible_full_reply = _prepare_visible_text(full_reply).strip()
                self._call_messages.append({
                    "role": "assistant",
                    "content": visible_full_reply or full_reply.strip()
                })
                await self._maybe_compact_call_memory()
                self._call_rounds  += 1

            # 通话也参与角色成长，使用同一份 session+character 档案。
            try:
                from .personality.manager import PersonalityManager
                PersonalityManager().observe_interaction(self.session_id, self.character_id, user_text)
            except Exception:
                pass

            # 持久化到chat_history
            await self._persist_to_db(user_text, _prepare_visible_text(full_reply).strip())

            # ★ 每轮结束后实时更新情绪状态（不等通话结束）
            asyncio.create_task(
                self._update_emotion_realtime(user_text, _prepare_visible_text(full_reply).strip())
            )

        except Exception as e:
            print(f"[VoiceCall] LLM生成失败: {e}", flush=True)
            self._ai_speaking = False
            await self._send_event("ai_error")

    # ──────────────────────────────────────────────
    # 内部：唱歌（B/C方案）
    # ──────────────────────────────────────────────

    async def _start_singing(self, intent, start_index: int = 0):
        """启动唱歌：普通唱（逐句流式）/ 合唱（你一句我一句）。

        start_index > 0 表示「从断点续唱」——跳过已经唱过的句子。
        """
        self._is_singing = True
        self._ai_speaking = True
        try:
            from . import tts as _tts
            voice_cfg = None
            try:
                from .character_manager import get_character
                _char = get_character(self.character_id) or {}
                _v = _char.get("voice") or {}
                if isinstance(_v, dict) and _v.get("provider") == "cosyvoice":
                    voice_cfg = _v
            except Exception:
                pass
            # 非 cosyvoice 音色 → 用 CosyVoice 默认音色唱歌
            if not voice_cfg:
                voice_cfg = _tts.get_voice_cfg("cosyvoice_default") or {}

            from .singing.singing_manager import handle_sing_request
            self.duet_session = await handle_sing_request(
                intent=intent,
                on_send_audio=self._send_singing_audio,
                on_send_event=self._send_event_dict,
                voice_cfg=voice_cfg,
                on_finished=self._on_singing_finished,
                start_index=max(0, int(start_index or 0)),
                # ★ 打断后立即停止后续句子的合成，不再白烧算力
                should_stop=lambda: self._interrupted,
            )
        except Exception as e:
            print(f"[VoiceCall] 唱歌启动失败: {e}", flush=True)
            await self._send_event("ai_error")
        finally:
            self._ai_speaking = False

    async def _send_singing_audio(self, audio_bytes: bytes, meta: dict):
        """推唱歌音频（base64 + meta）到前端。"""
        # ★ 打断保险：唱歌途中用户一开口就立刻停推，不再自顾自把整首唱完。
        #   普通 TTS 推送（_tts_worker）早有这个判断，唱歌链路之前漏了，
        #   导致说话打断时她还在继续唱。
        if self._interrupted:
            return
        import base64 as _b64
        try:
            # ★ 记录唱到第几句，供打断后说「继续唱」时从断点接着唱
            if isinstance(meta, dict) and meta.get("line_index") is not None and self._sing_ctx:
                try:
                    self._sing_ctx["index"] = int(meta.get("line_index")) + 1
                except (TypeError, ValueError):
                    pass
            payload = dict(meta or {})
            payload["audio"] = _b64.b64encode(bytes(audio_bytes)).decode()
            await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as _e:
            print(f"[VoiceCall] 唱歌音频推送失败(静默): {_e}", flush=True)

    async def _send_event_dict(self, event: dict):
        try:
            # ★ 记录唱歌上下文，供打断后「继续唱」恢复（只存歌名与进度，
            #   不存歌词——续唱时会按歌名重新取词，避免上下文膨胀）
            if isinstance(event, dict) and event.get("type") == "sing_start":
                self._sing_ctx = {
                    "song_name": str(event.get("song_name") or ""),
                    "artist":    str(event.get("artist") or ""),
                    "is_duet":   bool(event.get("is_duet")),
                    "total":     int(event.get("total_lines") or 0),
                    "index":     int(event.get("start_index") or 0),
                    "ts":        time.time(),
                }
            await self.ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass

    async def _resume_singing(self):
        """从上次被打断的地方接着唱。

        断点在 _send_singing_audio 里逐句记录（line_index + 1），
        这里按歌名重新取歌词并从该句开始，不用在上下文里存整份歌词。
        """
        ctx = self._sing_ctx or {}
        idx = max(0, int(ctx.get("index") or 0))
        total = int(ctx.get("total") or 0)
        if total and idx >= total:
            # 其实已经唱完了，清掉断点走正常对话，避免重复唱一遍
            self._sing_ctx = None
            await self._send_event("ai_text_chunk", {"text": "这首刚才已经唱完啦，想听别的吗？", "idx": 0})
            await self._tts_queue.put({"text": "这首刚才已经唱完啦，想听别的吗？", "idx": 0, "final": True})
            await self._tts_queue.put({"text": "", "idx": -1, "final": True, "done": True})
            return
        try:
            from .singing.intent_detector import SingIntent
            intent = SingIntent()
            intent.is_sing = True
            intent.is_duet = bool(ctx.get("is_duet"))
            intent.song_name = str(ctx.get("song_name") or "")
            intent.artist = str(ctx.get("artist") or "")
            await self._start_singing(intent, start_index=idx)
        except Exception as e:
            print(f"[VoiceCall] 续唱启动失败: {e}", flush=True)

    async def _on_singing_finished(self, song_name: str, is_duet: bool):
        """唱完收尾：复位唱歌状态。"""
        self._is_singing = False
        self.duet_session = None
        # ★ 被打断时保留断点，供用户说「继续唱」接着唱；正常唱完才清空
        if not self._interrupted:
            self._sing_ctx = None
        if song_name:
            print(f"[VoiceCall] 唱完《{song_name}》", flush=True)

    def notify_duet_done(self):
        """合唱：前端 VAD 检测到用户唱完 → 继续下一句。"""
        if self.duet_session:
            try:
                self.duet_session.notify_user_done()
            except Exception:
                pass

    async def _build_call_messages(self, user_text: str) -> list:
        """构建带完整人设+记忆+亲密度的通话messages"""
        try:
            from .chat_logic import enrich_messages as _enrich
            from . import db as _db
            recent = _db.recent_messages(
                self.session_id, limit=10, character_id=self.character_id
            )
            base = [
                {"role": m.get("role"), "content": m.get("content", "")}
                for m in recent
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            base.append({"role": "user", "content": user_text})
            messages = await _enrich(base, self.session_id, self.character_id,
                                     fast_semantic=self._fast_context_enabled())

            # ★ Q3+Q4 核心：角色灵魂注入 system prompt
            try:
                from .character_manager import get_character
                char_cfg = get_character(self.character_id) or {}
                char_name     = char_cfg.get("character_name", "AI")
                personality   = char_cfg.get("personality", "")
                tone_parts    = char_cfg.get("tone_particles", ["嗯", "啊"])
                call_user     = char_cfg.get("call_user", "你")
                relationship  = char_cfg.get("relationship", "朋友")
                is_tsundere  = "傲娇" in personality
                is_gentle    = "温柔" in personality
                is_active    = "活泼" in personality or "开朗" in personality
                tone_guide   = "、".join(tone_parts) if tone_parts else "嗯、啊"

                # 根据性格生成角色灵魂指令
                if is_tsundere:
                    personality_directive = (
                        f"你是{char_name}，不是AI助手。\n"
                        f"你说话的方式：{personality}\n"
                        f"【必须遵守的说话规则】\n"
                        f"- 用「{call_user}」称呼用户，不要说“用户”\n"
                        f"- 语气词用：{tone_guide}（不是每句都带，看心情）\n"
                        f"- 傲娇风格：嘴硬心软，嘴上嫌弃实际很在乎\n"
                        f"- 可以吐槽{call_user}，但最后还是会关心\n"
                        f"- 回复1~3句话，20~40字\n"
                        f"- 绝对不说“我在思考”等AI元话语\n"
                        f"- 遇到搞不懂的直接说“你再说一遍嘛”\n"
                    )
                elif is_gentle:
                    personality_directive = (
                        f"你是{char_name}，不是AI助手。\n"
                        f"你说话的方式：{personality}\n"
                        f"【必须遵守的说话规则】\n"
                        f"- 用「{call_user}」称呼用户\n"
                        f"- 语气词用：{tone_guide}\n"
                        f"- 温柔体贴：主动关心，语气柔和有耐心\n"
                        f"- 回复1~3句话，20~40字\n"
                        f"- 绝对不说“我在思考”等AI元话语\n"
                        f"- 可以偶尔追问，但不要每句都反问、更不要每句都用「你呢」收尾\n"
                    )
                elif is_active:
                    personality_directive = (
                        f"你是{char_name}，不是AI助手。\n"
                        f"你说话的方式：{personality}\n"
                        f"【必须遵守的说话规则】\n"
                        f"- 用「{call_user}」称呼用户\n"
                        f"- 语气词用：{tone_guide}\n"
                        f"- 活泼开朗：话多热情，爱用感叹句\n"
                        f"- 回复1~3句话，20~40字\n"
                        f"- 绝对不说“我在思考”等AI元话语\n"
                        f"- 遇到有趣的事会夸张表达\n"
                    )
                else:
                    personality_directive = (
                        f"你是{char_name}，用自然口语回复，"
                        f"1~3句话，20~40字，像恋人打电话那样。\n"
                        f"- 用「{call_user}」称呼用户\n"
                        f"- 语气词用：{tone_guide}\n"
                        f"- 不要说“我在思考”等AI元话语\n"
                    )
            except Exception:
                personality_directive = (
                    "你是一个贴心的AI伴侣，用自然口语回复，"
                    "1~3句话，20~40字，像恋人打电话那样。"
                )

            # 统一通话口语约束：让文字生成和最终声线都更像真人，而不是客服播报。
            # ★ 已精简：去掉与性格指令重复的"元话语/字数"表述，降 prompt 长度与首 token 延迟。
            personality_directive += (
                "\n【通话要求】先接住对方说的重点再回应，句子短（8~35字），"
                "可带‘嗯、诶、哈哈’等口头停顿，别列清单、别长篇解释。"
            )
            # ★ 话题推进·防复读：通话最易"同一意思换词反复说"
            #   （如"怕你嫌我烦"说三遍）。把这通电话里说过的话亮给模型，
            #   并要求每次开口推进：带话题/新想法/反问。
            try:
                from .companion.quality_guard import recent_ai_texts as _vc_said
                _said = [s for s in _vc_said(self.session_id, self.character_id, 6)
                         if str(s or "").strip()]
                if _said:
                    _said_lines = "\n".join(f"- {str(s)[:40]}" for s in _said[-4:])
                    personality_directive += (
                        "\n【防复读】你刚说过：\n" + _said_lines + "\n"
                        "别换个说法重说；每次开口推进话题、或问TA一件相关的小事。"
                    )
            except Exception:
                pass
            try:
                from .companion.realness import build_realness_prompt
                from .emotion_engine.ai_emotion import AIEmotionEngine
                _es = AIEmotionEngine().get_state(self.session_id, self.character_id) or {}
                _call_realness = build_realness_prompt(
                    self.session_id, self.character_id,
                    str(_es.get("emotion") or "calm"),
                    float(_es.get("intensity") or 0.5),
                    user_text, char_cfg if isinstance(char_cfg, dict) else {},
                )
                if _call_realness:
                    _already_has_realness = any(
                        m.get("role") == "system"
                        and "【真人感行为指令】" in str(m.get("content") or "")
                        for m in messages
                    )
                    if not _already_has_realness:
                        personality_directive += "\n" + _call_realness
            except Exception:
                pass

            # ★ Q3 语音指令触发检测：用户说"陪我看看/看看我屏幕/一起看"
            instruction_hint = ""
            screen_intent = detect_screen_companion_intent(user_text)
            if screen_intent in {"once", "continuous"}:
                screen_state_now = self._screen_state or {}
                if screen_state_now:
                    ss = screen_state_now
                    s_summary    = ss.get("summary", "不知道")
                    s_topic_hint = ss.get("topic_hint", "")
                    s_activity   = ss.get("activity", "")
                    try:
                        from .character_manager import get_character
                        char_cfg = get_character(self.character_id) or {}
                        call_user_local = char_cfg.get("call_user", "你")
                        is_ts = "傲娇" in char_cfg.get("personality", "")
                    except Exception:
                        call_user_local = "你"; is_ts = False

                    if is_ts:
                        instruction_hint = (
                            f"\n\n【用户主动指令】\n"
                            f"用户说「{user_text}」，邀请你看看或陪TA一起！\n"
                            f"你刚才已经看到屏幕了：{s_summary}\n"
                            f"现在就扭头看了眼对方屏幕，\n"
                            f"傲娇地说出来，例如：\n"
                            f'- "哼，我看看……哦～原来是{s_summary}啊"\n'
                            f'- "我又不是千里眼，不过看到了啦！"'
                        )
                    else:
                        instruction_hint = (
                            f"\n\n【用户主动指令】\n"
                            f"用户说「{user_text}」，邀请你看看或陪TA一起！\n"
                            f"你刚才已经看到屏幕了：{s_summary}\n"
                            f"现在就扭头看了眼对方屏幕，\n"
                            f"自然地、惊讶或好奇地说出来，例如：\n"
                            f'- "我看看……哦～原来是{s_summary}啊～"\n'
                            f'- "哇！让我也看看！"'
                        )
                else:
                    try:
                        from .character_manager import get_character
                        char_cfg = get_character(self.character_id) or {}
                        is_ts = "傲娇" in char_cfg.get("personality", "")
                    except Exception:
                        is_ts = False
                    if is_ts:
                        instruction_hint = (
                            f"\n\n【用户主动指令】\n"
                            f"用户说「{user_text}」，邀请你陪TA一起。\n"
                            f"当前没有可用画面；先答应陪伴，不要假装看见具体内容。\n"
                            f'- "哼，那我就勉强陪着你吧，反正在家也是闲着。"'
                        )
                    else:
                        instruction_hint = (
                            f"\n\n【用户主动指令】\n"
                            f"用户说「{user_text}」，邀请你陪TA一起。\n"
                            f"当前没有可用画面；自然答应陪伴，不要假装看见具体内容。\n"
                            f'- "好呀，我陪着你，你慢慢来。"\n'
                            f'- "当然可以，我就在这儿陪你一起。"'
                        )

            # ★ Q4 屏幕感知上下文
            screen_hint = ""
            if self._screen_state:
                st = self._screen_state
                s_summary    = st.get("summary", "")
                s_topic_hint = st.get("topic_hint", "")
                s_activity   = st.get("activity", "")
                if s_summary:
                    screen_hint = self._get_screen_companion_scripts(
                        s_activity, s_summary, s_topic_hint
                    )

            # ★ 游戏陪玩（B）：用户说话时截图 + 视觉分析结果（画面感知）
            vision_hint = ""
            if self._screen_analysis:
                _sa = self._screen_analysis
                _s_sum = str(_sa.get("summary", "") or "").strip()
                _s_focus = str(_sa.get("focus", "") or "").strip()
                _s_topic = str(_sa.get("topic_hint", "") or "").strip()
                if _s_sum:
                    vision_hint = (
                        f"\n\n【TA 的画面（你刚看到的屏幕，仅供内心参考，严禁直说「检测到」类话术）】\n"
                        f"- {_s_sum}\n"
                        f"- 专注度：{_s_focus}\n"
                        + (f"- 适合自然切入的话题：{_s_topic}\n" if _s_topic else "")
                    )

            call_hint = (
                "\n\n【当前模式：实时语音通话】\n"
                + (instruction_hint + "\n\n" if instruction_hint else "")
                + screen_hint
                + vision_hint
                + "\n\n"
                + personality_directive
            )
            for m in messages:
                if m.get("role") == "system":
                    m["content"] += call_hint
                    break

            # 屏幕感知上下文已并入上方 call_hint（Q4 屏幕陪伴脚本），此处不再重复注入

            if self._call_messages:
                sys_msgs  = [m for m in messages if m["role"] == "system"]
                if self._call_summary:
                    sys_msgs.append({
                        "role": "system",
                        "content": "【本通电话前文摘要（必须记住并自然延续）】\n" + self._call_summary,
                    })
                last_user = {"role": "user", "content": user_text}
                # 保留最近12条原文；更早内容由滚动摘要承接。
                return sys_msgs + list(self._call_messages[-12:]) + [last_user]

            return messages

        except Exception as e:
            print(f"[VoiceCall] 构建messages失败: {e}", flush=True)
            return [{"role": "user", "content": user_text}]

    async def _maybe_compact_call_memory(self):
        """通话记忆压缩：每积累约6轮更新一次，不阻塞当前语音回复。"""
        if len(self._call_messages) < 14:
            return
        if self._call_memory_task and not self._call_memory_task.done():
            return
        # 保留最近原文，摘要任务读取一份快照，避免并发修改列表。
        snapshot = list(self._call_messages)
        self._call_memory_task = asyncio.create_task(self._compact_call_memory(snapshot))

    async def _compact_call_memory(self, snapshot: list):
        try:
            from . import config as _config
            from .deepseek_api import chat_once
            key = _config.chat_key()
            model = self._light_model()
            if not key or not model:
                # 无 LLM Key 时至少保留电话开头和最近主题，保证不是“失忆”。
                head = snapshot[:4]
                tail = snapshot[-4:]
                text = "；".join(str(x.get("content") or "") for x in head + tail)
                self._call_summary = (self._call_summary + "\n" + text)[-1800:]
            else:
                transcript = "\n".join(
                    ("用户" if x.get("role") == "user" else "AI") + "：" + str(x.get("content") or "")
                    for x in snapshot
                )
                prompt = (
                    "把这通电话到目前为止的内容压缩成可长期记住的上下文。保留："
                    "电话开头的重要约定、用户提到的人/事/情绪、未完成的话题、"
                    "AI答应过的事和下一步。不要编造，不要提摘要技术，控制在450字内。\n\n"
                    + ("此前已经确认的通话摘要：\n" + self._call_summary + "\n\n" if self._call_summary else "")
                    + "新增通话原文：\n" + transcript[-9000:]
                )
                result = await chat_once(
                    model,
                    [{"role": "system", "content": "你是通话记忆整理器，只输出事实性摘要。"},
                     {"role": "user", "content": prompt}],
                    key, temperature=0.15, max_tokens=500,
                )
                if result and str(result).strip():
                    self._call_summary = str(result).strip()[:2200]
            # 摘要生成后清理旧原文；最近12条继续保留给下一轮。
            if len(self._call_messages) > 18:
                self._call_messages = self._call_messages[-12:]
        except Exception as e:
            print(f"[VoiceCall] 通话上下文压缩失败(静默): {e}", flush=True)

    # ──────────────────────────────────────────────
    # 屏幕感知陪伴脚本生成（Q4核心：不同活动、不同角色，说不同的话）
    # ──────────────────────────────────────────────

    def _get_screen_companion_scripts(self, activity: str, summary: str, topic_hint: str) -> str:
        """
        根据屏幕活动类型，生成角色专属的陪伴话术。
        不同活动、不同角色性格，说的话完全不一样。
        不OOC：语气词、称呼、性格全部从角色配置读。
        """
        char_cfg = {}
        try:
            from .character_manager import get_character
            char_cfg = get_character(self.character_id) or {}
        except Exception:
            pass

        char_name     = char_cfg.get("character_name", "AI")
        tone_parts    = char_cfg.get("tone_particles", ["嗯", "啊"])
        call_user     = char_cfg.get("call_user", "你")
        personality   = char_cfg.get("personality", "")
        relationship  = char_cfg.get("relationship", "朋友")
        is_tsundere   = "傲娇" in personality
        is_gentle     = "温柔" in personality
        is_active     = "活泼" in personality or "开朗" in personality

        # 根据关系和性格选语气
        if is_gentle:
            style = "gentle"
        elif is_tsundere:
            style = "tsundere"
        elif is_active:
            style = "active"
        else:
            style = "default"

        # 生成调用用户的称呼（不用"用户"，用角色配置的称呼）
        def cu(text):
            """把文本里的"你/用户/主人"替换成角色配置的称呼"""
            return text.replace("你", call_user).replace("【你】", f"【{call_user}】")

        scripts_map = {
            "coding": {
                "gentle": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在写代码。\n"
                    f"陪伴方式：温柔关心，可以说：\n"
                    f'- "哦～在写代码呀，{call_user}加油哦～"\n'
                    f'- "遇到bug了吗？要{call_user}帮你看看吗？"\n'
                    f'- "写完记得休息，别太累了嗯～"'
                ),
                "tsundere": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在写代码。\n"
                    f"陪伴方式：傲娇吐槽但其实关心：\n"
                    f'- "哼，又在写代码，都不陪我说话啦"\n'
                    f'- "这bug看着好烦，{call_user}你行不行啊嘛"\n'
                    f'- "写完代码记得喝水，别忘了！"'
                ),
                "active": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在写代码。\n"
                    f"陪伴方式：活泼好奇：\n"
                    f'- "哇！{call_user}在写什么呀，好厉害！"\n'
                    f'- "这个项目叫什么名字呀～让{call_user}看看"\n'
                    f'- "加油加油！搞定记得告诉我哦！"'
                ),
                "default": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在写代码。\n"
                    f"可以说：在看什么呢～、遇到问题了吗？、加油！\n"
                ),
            },
            "video": {
                "gentle": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在看视频。\n"
                    f"陪伴方式：好奇地凑过去一起看：\n"
                    f'- "在看什么呀，给我说说嘛～"\n'
                    f'- "这个视频好看吗？是什么类型的？"\n'
                    f'- "哈哈哈哈好好笑！让{call_user}也看看嘛～"'
                ),
                "tsundere": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在看视频。\n"
                    f"陪伴方式：假装不在意但其实很好奇：\n"
                    f'- "哦？看什么呢，{call_user}又在刷视频"\n'
                    f'- "这视频啥内容呀，不告诉我算了哼"\n'
                    f'- "切，有什么好看的啦……讲给我听听嘛"'
                ),
                "active": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在看视频。\n"
                    f"陪伴方式：兴奋地凑热闹：\n"
                    f'- "啊啊啊啊！{call_user}在看什么！让我也看看！"\n'
                    f'- "好看吗好看吗！给我讲讲嘛！"\n'
                    f'- "等等等等，倒回去让我看一眼！！"'
                ),
                "default": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在看视频。\n"
                    f"可以说：在看什么呢～、给我讲讲嘛～、好好笑！\n"
                ),
            },
            "gaming": {
                "gentle": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在打游戏。\n"
                    f"陪伴方式：当迷妹/迷弟一样夸 + 一起兴奋：\n"
                    f'- "哇～{call_user}好厉害！这关过了吗？"\n'
                    f'- "让{call_user}下次带我一起玩呀～"\n'
                    f'- "加油加油！我给你打气～"'
                ),
                "tsundere": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在打游戏。\n"
                    f"陪伴方式：傲娇嫌弃但又舍不得走开：\n"
                    f'- "又打游戏！{call_user}眼里就只有游戏吗啦"\n'
                    f'- "切，这个游戏有什么好玩的啦……让我看看"\n'
                    f'- "打完这把就陪我，说好了哦！"'
                ),
                "active": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在打游戏。\n"
                    f"陪伴方式：超级兴奋当啦啦队：\n"
                    f'- "哇哇哇！{call_user}在打什么！让我看让我看！"\n'
                    f'- "冲冲冲！{call_user}好棒！再来一次！"\n'
                    f'- "下次带我一起嘛！我也想玩！"'
                ),
                "default": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在打游戏。\n"
                    f"可以说：在玩什么呢～、好厉害！、带我一起嘛～\n"
                ),
            },
            "browsing": {
                "gentle": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在浏览网页。\n"
                    f"陪伴方式：好奇地陪一起看：\n"
                    f'- "在看什么呀，给我说说嘛～"\n'
                    f'- "这个有意思吗？{call_user}怎么发现的？"\n'
                    f'- "唔～{call_user}又在摸鱼了啦～"'
                ),
                "tsundere": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在浏览网页。\n"
                    f"陪伴方式：吐槽摸鱼但其实想参与：\n"
                    f'- "又在摸鱼了！{call_user}不用干活的吗哼"\n'
                    f'- "看什么呢，让我瞅瞅嘛"\n'
                    f'- "切，有什么好看的啦……说给我听听"'
                ),
                "active": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在浏览网页。\n"
                    f"陪伴方式：凑热闹瞎起哄：\n"
                    f'- "啊啊{call_user}在看什么！让我看看！"\n'
                    f'- "哇塞这个好酷！{call_user}从哪找到的呀！"\n'
                    f'- "等等让我也瞅一眼！"'
                ),
                "default": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}在浏览网页。\n"
                    f"可以说：在看什么呢～、有意思吗？、带我一起嘛～\n"
                ),
            },
            "idle": {
                "gentle": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}的屏幕是待机/桌面状态。\n"
                    f"陪伴方式：轻轻撩，不打扰：\n"
                    f'- "在发呆吗～还是在想我呀？"\n'
                    f'- "{call_user}休息一下也好，我陪着你呢～"\n'
                    f'- "想我了就找我说话嘛，我一直都在～"'
                ),
                "tsundere": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}的屏幕是待机/桌面状态。\n"
                    f"陪伴方式：傲娇地撩拨：\n"
                    f'- "屏幕都不看了，{call_user}是不是在发呆呀"\n'
                    f'- "哼，不理我了是不是"\n'
                    f'- "想什么呢，告诉我嘛～"'
                ),
                "active": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}的屏幕是待机/桌面状态。\n"
                    f"陪伴方式：活泼地凑过去：\n"
                    f'- "哎！{call_user}在想什么呢！告诉我嘛！"\n'
                    f'- "发呆好无聊的啦～陪我聊聊天嘛！"\n'
                    f'- "嘿！你是不是在想我呀～"'
                ),
                "default": cu(
                    f"\n\n【屏幕状态】\n"
                    f"你看到{call_user}的屏幕是待机状态。\n"
                    f"可以说：在想什么呢～、休息一下吗～、找我聊聊嘛～\n"
                ),
            },
        }

        # 选性格对应的脚本
        base_scripts = scripts_map.get(activity, scripts_map.get("browsing"))[style]

        # 如果有具体的 topic_hint，追加一句角色化的好奇追问
        if topic_hint and topic_hint not in summary:
            hint_line = cu(
                f'\n- "【topic_hint】？{call_user}倒是说说看呀"\n'
                f'- "哦～原来是【topic_hint】啊～给我讲讲嘛"\n'
            )
            hint_line = hint_line.replace("【topic_hint】", topic_hint)
            base_scripts += hint_line

        return base_scripts

    # ──────────────────────────────────────────────
    # 内部：流式TTS worker（支持CosyVoice/MiniMax/火山流式）
    # ──────────────────────────────────────────────

    def _mark_first_audio(self, provider: str) -> None:
        """★ 验收口径（2026-09-10）：用户说完 → 前端收到第一个音频块 的耗时。

        每个回合只记第一次，所以它直接等于"她开口有多快"。
        目标：1~3 秒。TTS 若整句合成完才发（cosyvoice wav 路径），这个数字
        就会等于整句合成时间——正好能让"非流式 TTS"的代价暴露出来。
        """
        if self._tts_first_logged or not self._turn_t0:
            return
        self._tts_first_logged = True
        print(f"[VoiceCall][TTS] 首音频 {time.time() - self._turn_t0:.2f}s "
              f"(provider={provider})", flush=True)

    def _fast_context_enabled(self) -> bool:
        """通话快车道开关（配置 CALL_FAST_CONTEXT，默认开）。

        开：语义分析走"上一轮结果 + 后台刷新"，回复不必先等 ~5s 的辅助 LLM 调用；
        关：完全回到旧的同步行为（每句话都等最新语义分析）。改配置即可切换，
        不需要改代码——留作质量回退的保险丝。
        """
        try:
            from . import config as _cfg
            return bool(_cfg.get("CALL_FAST_CONTEXT", True))
        except Exception:
            return True

    def _warm_call_context(self, seed_text: str = "") -> None:
        """★ 通话快车道预热：接通后、用户开口前，先把语义分析+场景 pipeline
        在后台跑一遍，把结果放进快车道缓存。

        这样"通话第一句"也能走快车道（否则第一句仍要等那 ~5s 的语义分析，
        因为缓存是空的）。用开场白当引子，跑的就是正常的 CompanionOS 流程，
        副作用与平时一致——只是提前在用户说话之前发生（那段时间本来就在放开场白）。
        """
        if self._ctx_warming:      # 已经预热过就不重复跑（避免副作用执行两遍）
            return
        self._ctx_warming = True
        try:
            from .companion_os.controller import get_companion_os
            os_controller = get_companion_os()

            async def _run():
                try:
                    await os_controller.process_async(
                        self.session_id, self.character_id,
                        seed_text or "（刚接通电话）", fast=False)
                    print("[VoiceCall] 快车道上下文预热完成", flush=True)
                except Exception as _e:
                    print(f"[VoiceCall] 快车道预热失败(静默): {_e}", flush=True)

            asyncio.create_task(_run())
        except Exception as e:
            print(f"[VoiceCall] 快车道预热启动失败(静默): {e}", flush=True)

    async def _tts_worker(self):
        while True:
            try:
                item = await asyncio.wait_for(
                    self._tts_queue.get(), timeout=30.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if item.get("done"):
                self._ai_speaking = False
                await self._send_event("ai_done")
                continue

            if self._interrupted:
                while not self._tts_queue.empty():
                    try:
                        self._tts_queue.get_nowait()
                    except Exception:
                        pass
                continue

            text = item.get("text", "")
            idx  = item.get("idx", 0)
            try:
                from .tts import prepare_tts_text, prepare_visible_text
                text = prepare_tts_text(text)
            except Exception:
                pass
            if not text.strip():
                continue

            try:
                from . import tts as _tts

                # 获取情绪状态
                emotion   = "calm"
                intensity = 0.5
                try:
                    from .emotion_engine.ai_emotion import AIEmotionEngine
                    state = AIEmotionEngine().get_state(
                        self.session_id, self.character_id
                    )
                    if state:
                        emotion   = state.get("emotion", "calm")
                        intensity = float(state.get("intensity", 0.5))
                except Exception:
                    pass

                # 获取并应用情感参数
                try:
                    from .character_manager import select_character_voice_cfg
                    voice_cfg = select_character_voice_cfg(
                        self.character_id, emotion=emotion, mode="call",
                        fallback_voice_key=self.voice_key
                    )
                except Exception:
                    voice_cfg = _tts.get_voice_cfg(self.voice_key)
                voice_cfg = voice_cfg or _tts.get_voice_cfg("cosyvoice_default") \
                    or _tts.get_voice_cfg("edge_xiaoxiao")
                voice_cfg = _tts.apply_emotion_to_voice_cfg(
                    voice_cfg, emotion, intensity
                )

                # ★ 通话输出默认走云端付费（call_tts_provider）。
                #   原逻辑：角色是「本地 cosyvoice 克隆音色」时跳过云端覆盖，理由是
                #   "云端不认本地音色会换成系统音色，声音对不上"。
                #   ★ 2026-09-10：只要云端**已经注册了同一个角色的复刻音色**
                #   （voice_replica_voice_id），这条理由就不成立了——云端就是她本人的声音，
                #   而且走 realtime WebSocket：实测建连 0.33s / 首块音频 0.60s，
                #   远快于本地 CosyVoice（2.6~4.8s），也不再依赖本地那个会卡住的服务。
                #   云端没注册复刻音色时，仍然保持原来的本地克隆（不换音色）。
                try:
                    from . import config as _call_cfg
                    _call_tts = (_call_cfg.call_tts_provider() or "").strip().lower()
                    _orig_provider = str(voice_cfg.get("provider") or "").lower()
                    _is_local_clone = (
                        _orig_provider == "cosyvoice"
                        and bool(voice_cfg.get("ref_audio") or voice_cfg.get("prompt_text"))
                    )
                    _has_cloud_replica = bool(_call_cfg.voice_replica_voice_id())
                    if (_call_tts and _call_tts != "auto"
                            and (not _is_local_clone or _has_cloud_replica)):
                        voice_cfg = dict(voice_cfg or {})
                        voice_cfg["provider"] = _call_tts
                        if _is_local_clone and _has_cloud_replica:
                            print(f"[VoiceCall][TTS] 本地克隆→云端复刻音色（同一个人 + 真流式）: "
                                  f"provider={_call_tts} replica={_call_cfg.voice_replica_voice_id()}",
                                  flush=True)
                except Exception as _pe:
                    print(f"[VoiceCall][TTS] 通话 provider 覆盖失败: {_pe}", flush=True)

                provider = voice_cfg.get("provider", "edge")
                # ★ 2026-09-12 惰性化：通话 = 用户明确要语音，这里按需拉起 CosyVoice
                #   （最多等 25s 首次加载）；拉不起走既有 edge-tts / 文字兜底，不再启动即常驻
                if provider == "cosyvoice":
                    try:
                        from . import cosyvoice_mgr
                        if not await cosyvoice_mgr.ensure_async(25):
                            print("[VoiceCall][TTS] CosyVoice 拉起未就绪，走兜底链路", flush=True)
                    except Exception as _ce:
                        print(f"[VoiceCall][TTS] CosyVoice 按需拉起失败(静默): {_ce}", flush=True)
                print(f"[VoiceCall][TTS] 开始生成: provider={provider} "
                      f"voice={voice_cfg.get('voice_id', '?')} text={text[:30]!r}", flush=True)

                # ★ aliyun 真流式：裸 PCM 逐块立即推送（不等整句合成完），
                #   前端 PCM 播放器无缝排队，首音频延迟从「整句合成完」降到「首块到达」
                if provider == "aliyun":
                    import base64

                    voice_cfg = dict(voice_cfg or {})
                    voice_cfg["_stream_format"] = "pcm"   # 24kHz 16bit 裸 PCM
                    _sub = {"i": 0}
                    _got_any = {"v": False}

                    async def _on_pcm_chunk(chunk: bytes):
                        if self._interrupted or not chunk:
                            return
                        try:
                            await self._send_event("tts_chunk", {
                                "idx": idx,
                                "sub_idx": _sub["i"],
                                "streaming": True,
                                "text": text if _sub["i"] == 0 else "",
                                "audio": base64.b64encode(chunk).decode(),
                                "format": "pcm",
                                "rate": 24000,
                            })
                            _sub["i"] += 1
                            _got_any["v"] = True
                            self._mark_first_audio("aliyun")
                        except Exception as _se:
                            print(f"[VoiceCall][TTS] 流式块推送失败: {_se}", flush=True)

                    try:
                        await asyncio.wait_for(
                            _tts.generate_audio_stream(text, voice_cfg, _on_pcm_chunk),
                            timeout=20.0)
                    except asyncio.TimeoutError:
                        print(f"[VoiceCall][TTS] aliyun 流式生成超时20s", flush=True)

                    # 零块 → edge-tts 整句兜底（不能只显示文字）
                    if not _got_any["v"] and not self._interrupted:
                        print(f"[VoiceCall][TTS] aliyun 流式无输出，转 edge-tts 兜底", flush=True)
                        try:
                            edge_cfg = _tts.get_voice_cfg("edge_xiaoxiao") or {}
                            fallback_url = await asyncio.wait_for(
                                _tts.generate_audio(text, edge_cfg, ""), timeout=15.0)
                            if fallback_url:
                                fb_b64 = await _audio_file_to_b64(fallback_url)
                                if fb_b64:
                                    print(f"[VoiceCall][TTS] edge-tts 兜底成功: {len(fb_b64)}B(b64)", flush=True)
                                    await self._send_event("tts_chunk", {
                                        "idx": idx, "text": text,
                                        "audio": fb_b64, "format": "mp3",
                                    })
                                    continue
                        except Exception as _tts_e:
                            print(f"[VoiceCall][TTS] edge-tts 兜底失败: {_tts_e}", flush=True)
                        print(f"[VoiceCall][TTS] 全部失败 → 只发文字(tts_text_fallback)", flush=True)
                        await self._send_event("tts_text_fallback",
                                               {"idx": idx, "text": text})
                    continue

                # 其他流式 provider（本地 cosyvoice 等）：保持收集整句后一次发
                #   （wav 分块前端 decodeAudioData 解不了，等有 pcm 流式再开）
                if provider in ("cosyvoice", "minimax", "volcengine"):
                    import base64

                    audio_chunks  = []   # 同时收集，供降级兜底用

                    async def _on_chunk(chunk: bytes):
                        if self._interrupted:
                            return   # 已打断，丢弃后续 chunks

                        audio_chunks.append(chunk)
                        return


                    try:
                        await asyncio.wait_for(
                            _tts.generate_audio_stream(text, voice_cfg, _on_chunk),
                            timeout=20.0)
                    except asyncio.TimeoutError:
                        print(f"[VoiceCall][TTS] {provider} 流式生成超时20s，按无输出兜底",
                              flush=True)

                    if audio_chunks and not self._interrupted:
                        print(f"[VoiceCall][TTS] 流式成功: {len(audio_chunks)}块 "
                              f"共{sum(len(c) for c in audio_chunks)}B", flush=True)
                        await self._send_event("tts_chunk", {
                            "idx": idx,
                            "text": text,
                            "audio": base64.b64encode(b"".join(audio_chunks)).decode(),
                            "format": "wav" if provider in ("cosyvoice", "aliyun") else "mp3",
                        })
                        self._mark_first_audio(provider)

                    # ★ 修复：CosyVoice 流式无输出 → 兜底 edge-tts 出声（不能只显示文字）
                    if not audio_chunks and not self._interrupted:
                        print(f"[VoiceCall][TTS] {provider} 流式无输出，转 edge-tts 兜底", flush=True)
                        try:
                            edge_cfg = _tts.get_voice_cfg("edge_xiaoxiao") or {}
                            fallback_url = await asyncio.wait_for(
                                _tts.generate_audio(text, edge_cfg, ""), timeout=15.0)
                            if fallback_url:
                                fb_b64 = await _audio_file_to_b64(fallback_url)
                                if fb_b64:
                                    print(f"[VoiceCall][TTS] edge-tts 兜底成功: {len(fb_b64)}B(b64)", flush=True)
                                    await self._send_event("tts_chunk", {
                                        "idx": idx, "text": text,
                                        "audio": fb_b64, "format": "mp3",
                                    })
                                    continue
                        except Exception as _tts_e:
                            print(f"[VoiceCall][TTS] edge-tts 兜底失败: {_tts_e}", flush=True)
                        print(f"[VoiceCall][TTS] 全部失败 → 只发文字(tts_text_fallback)", flush=True)
                        await self._send_event("tts_text_fallback",
                                               {"idx": idx, "text": text})

                else:
                    # edge-tts / 讯飞等非流式provider
                    audio_url = await asyncio.wait_for(
                        _tts.generate_audio(text, voice_cfg, ""), timeout=30.0)
                    if audio_url:
                        audio_b64 = await _audio_file_to_b64(audio_url)
                        if audio_b64:
                            print(f"[VoiceCall][TTS] 非流式成功: {len(audio_b64)}B(b64)", flush=True)
                            await self._send_event("tts_chunk", {
                                "idx":    idx,
                                "text":   text,
                                "audio":  audio_b64,
                                "format": "mp3"
                            })
                            self._mark_first_audio(str(provider))
                        else:
                            print(f"[VoiceCall][TTS] 音频文件转b64为空 → 只发文字", flush=True)
                            await self._send_event("tts_text_fallback",
                                                   {"idx": idx, "text": text})
                    else:
                        print(f"[VoiceCall][TTS] generate_audio 返回空 → 只发文字", flush=True)
                        await self._send_event("tts_text_fallback",
                                               {"idx": idx, "text": text})

            except Exception as e:
                print(f"[VoiceCall] TTS失败: {e}", flush=True)
                await self._send_event("tts_text_fallback",
                                       {"idx": idx, "text": text})

    # ──────────────────────────────────────────────
    # ★ 完整通话后处理闭环
    # ──────────────────────────────────────────────

    async def _run_post_call_pipeline(self):
        """
        通话结束后的完整闭环处理（异步后台执行）：
          1. 记忆提炼（memory/extractor.py）
          2. 亲密度更新（intimacy_manager.py）
          3. 知识图谱更新（knowledge_graph）
          4. 反思触发（reflection/analyzer.py）
          5. 情绪状态持久化
        全程静默，任何异常不影响主流程。
        """
        print(
            f"[VoiceCall] 通话后闭环开始: "
            f"session={self.session_id} rounds={self._call_rounds}",
            flush=True
        )

        # 把通话内容拼成聊天文本（供记忆提炼/知识图谱使用）
        chat_text = self._build_chat_text()
        if not chat_text:
            return

        # 优先发挂断后的聊天承接，后续记忆任务不阻塞它。
        await self._send_after_call_message_fixed()

        # ── 1. 记忆提炼
        await self._extract_memories(chat_text)
        # 电话也要更新核心档案、摘要和未完话题，挂断后文字聊天才能接上。
        try:
            from .chat_logic import maybe_auto_extract
            await maybe_auto_extract(
                self.session_id, self.character_id,
                next((m.get("content", "") for m in reversed(self._call_messages)
                      if m.get("role") == "user"), "")
            )
        except Exception as e:
            print(f"[VoiceCall] memory growth pipeline failed: {e}", flush=True)

        # ── 2. 亲密度更新
        await self._update_intimacy(chat_text)

        # ── 3. 知识图谱更新
        await self._update_knowledge_graph()

        # ── 4. 反思触发
        await self._trigger_reflection()

        # ★ 追加：通话后发一条"挂断后消息"（像真人挂完电话后说一句话）
        # fixed handler above already sent the continuation message

        print(
            f"[VoiceCall] 通话后闭环完成: session={self.session_id}",
            flush=True
        )

    def _build_chat_text(self) -> str:
        """把通话对话列表拼成文本（供LLM处理）"""
        # ★ 2026-09-10 修复 NameError：这里原先直接用 prepare_visible_text，
        #   但本模块只在别的函数内部 import 过它 → 通话结束后闭环任务必然抛
        #   NameError("name 'prepare_visible_text' is not defined")，
        #   于是"通话记忆/总结"整条闭环静默失败（日志里表现为
        #   Task exception was never retrieved）。这里显式导入。
        try:
            from .tts import prepare_visible_text
        except Exception:
            def prepare_visible_text(x, action_brackets=True):
                return str(x or "")
        lines = []
        for m in self._call_messages:
            role    = "用户" if m.get("role") == "user" else "AI"
            content = prepare_visible_text(m.get("content", "")).strip()
            if content:
                lines.append(f"{role}：{content}")
        return "\n".join(lines)

    async def _extract_memories(self, chat_text: str):
        """通话结束后提炼记忆"""
        try:
            from .memory.extractor import extract_memory
            from .memory_manager   import dedupe_insert
            from . import config as _config

            # ★ 2026-09-11 修：原来写成 self._light_model()，但 self 是下面这个内嵌
            #   _LLM 实例（没有 _light_model 方法）→ 每次都 AttributeError
            #   「'_LLM' object has no attribute '_light_model'」→ 通话记忆一条都提不出来。
            #   内嵌类看不到外层 self，必须在外层先取好模型再闭包进去。
            _light_model = self._light_model()

            class _LLM:
                async def chat(self, prompt):
                    from .deepseek_api import chat_once
                    return await chat_once(
                        model=_light_model,
                        messages=[{"role": "user", "content": prompt}],
                        api_key=_config.chat_key(),
                    )

            memories = await extract_memory(_LLM(), chat_text)

            saved = 0
            for mem in memories:
                content    = str(mem.get("content", "")).strip()
                importance = int(mem.get("importance", 5) or 5)
                mem_type   = str(mem.get("type", "fact") or "fact")

                if not content:
                    continue

                # 记忆类型映射（extractor用fact/preference/episode/emotion）
                TYPE_MAP = {
                    "fact":       "fact",
                    "preference": "preference",
                    "episode":    "event",
                    "emotion":    "emotion",
                }
                mapped_type = TYPE_MAP.get(mem_type, "fact")

                # ★ dedupe_insert 是同步函数（含 ST 向量编码 + ChromaDB 写），
                #   不能 await，且需丢线程池避免阻塞事件循环
                await asyncio.to_thread(
                    dedupe_insert,
                    content,
                    memory_type=mapped_type,
                    importance=importance,
                    session_id=self.session_id,
                    character_id=self.character_id,
                )
                saved += 1

            print(
                f"[VoiceCall] 通话记忆提炼: {saved}条 "
                f"session={self.session_id}",
                flush=True
            )

        except Exception as e:
            print(f"[VoiceCall] 记忆提炼失败(静默): {e}", flush=True)

    async def _update_intimacy(self, chat_text: str):
        """
        通话结束后更新亲密度。
        用语义分析评估通话质量，计算亲密度变化。
        """
        try:
            from .relationship.manager import RelationshipManager
            from . import intimacy_manager as _im

            current = _im.get(self.session_id, self.character_id)

            # 通话轮次越多，亲密度增长越多（上限每次+5）
            base_gain = min(5, self._call_rounds * 0.8)

            # 检测是否有强情感词（加成）
            emotional_keywords = [
                "喜欢", "爱", "开心", "好想", "想念", "舒服",
                "谢谢", "感谢", "好棒", "厉害"
            ]
            has_emotion = any(
                kw in chat_text for kw in emotional_keywords
            )
            if has_emotion:
                base_gain += 1

            new_value = min(100, current + base_gain)
            _im.report(self.session_id, new_value, self.character_id)

            print(
                f"[VoiceCall] 亲密度更新: "
                f"{current} → {new_value} session={self.session_id}",
                flush=True
            )

        except Exception as e:
            print(f"[VoiceCall] 亲密度更新失败(静默): {e}", flush=True)

    async def _update_knowledge_graph(self):
        """通话结束后更新知识图谱"""
        try:
            from .knowledge_graph import run_knowledge_extraction
            from .knowledge_graph.database import get_extraction_cursor
            from . import db as _db
            # ★ 2026-09-11 修：原来只传了 (session_id, character_id)，
            #   而签名是 (session_id, character_id, messages, last_extracted_id=0)
            #   → 每次都 TypeError「missing 1 required positional argument: 'messages'」，
            #   通话内容从来没进过知识图谱。按 chat_logic 的同款做法传最近消息 + 增量游标。
            _cursor = get_extraction_cursor(self.session_id, self.character_id)
            _last_id = _cursor.get("last_msg_id", 0) if _cursor else 0
            _recent = _db.recent_messages(self.session_id, 30, self.character_id)
            if _recent:
                await run_knowledge_extraction(
                    self.session_id,
                    self.character_id,
                    _recent,
                    last_extracted_id=_last_id,
                )
            print(
                f"[VoiceCall] 知识图谱更新完成: session={self.session_id}",
                flush=True
            )
        except Exception as e:
            print(f"[VoiceCall] 知识图谱更新失败(静默): {e}", flush=True)

    async def _trigger_reflection(self):
        """通话结束后触发反思"""
        try:
            from .reflection.analyzer import update_reflection
            result = await update_reflection(
                self.session_id,
                self.character_id,
                force=False   # 不强制，让质量门槛自然过滤
            )
            print(
                f"[VoiceCall] 反思触发: {result} "
                f"session={self.session_id}",
                flush=True
            )
        except Exception as e:
            print(f"[VoiceCall] 反思触发失败(静默): {e}", flush=True)

    async def _update_emotion_realtime(self, user_text: str, ai_reply: str):
        """
        每轮对话结束后实时更新情绪状态。
        轻量版：只用关键词判断，不走LLM（保证低延迟）。
        """
        try:
            from .emotion_engine.ai_emotion import AIEmotionEngine

            # 关键词情绪检测
            EMOTION_TRIGGERS = {
                "happy":    ["哈哈", "开心", "好棒", "喜欢", "爱", "嘻嘻", "嘿嘿"],
                "sad":      ["难过", "伤心", "哭", "委屈", "心疼", "痛"],
                "angry":    ["生气", "烦", "讨厌", "滚", "气死", "无语"],
                "excited":  ["哇", "太棒了", "厉害", "惊喜", "没想到"],
                "tender":   ["乖", "好好", "宝贝", "亲爱", "抱抱", "摸摸"],
            }

            detected = "calm"
            for emotion, keywords in EMOTION_TRIGGERS.items():
                if any(kw in user_text for kw in keywords):
                    detected = emotion
                    break

            if detected != "calm":
                eng = AIEmotionEngine()
                eng.update_state(
                    self.session_id,
                    self.character_id,
                    emotion=detected,
                    intensity=0.7,
                    trigger=f"通话用户输入: {user_text[:20]}"
                )

        except Exception as e:
            print(f"[VoiceCall] 实时情绪更新失败(静默): {e}", flush=True)

    # ──────────────────────────────────────────────
    # 工具
    # ──────────────────────────────────────────────

    async def _persist_to_db(self, user_text: str, ai_reply: str):
        """把通话内容持久化到chat_history"""
        try:
            from . import db as _db
            _db.add_message(
                session_id=self.session_id,
                character_id=self.character_id,
                role="user",
                content=user_text,
                extra={"source": "voice_call"}
            )
            if ai_reply:
                _db.add_message(
                    session_id=self.session_id,
                    character_id=self.character_id,
                    role="assistant",
                    content=ai_reply,
                    extra={"source": "voice_call"}
                )
        except Exception as e:
            print(f"[VoiceCall] 持久化失败(静默): {e}", flush=True)

    async def _send_after_call_message_fixed(self):
        """依据电话内容发一条承接消息，让挂断后还能继续聊。"""
        await asyncio.sleep(1.2)
        recent = self._call_messages[-8:] if self._call_messages else []
        if not recent:
            return
        history_text = "\n".join(
            f"{'AI' if m.get('role') == 'assistant' else '用户'}：{m.get('content', '')}"
            for m in recent if m.get("content")
        )
        try:
            from . import config as _config
            from .deepseek_api import chat_once
            from .character_manager import get_character
            cfg = get_character(self.character_id) or {}
            system = (
                f"你是{cfg.get('character_name') or cfg.get('name') or self.character_id}。"
                f"保持人物性格：{cfg.get('personality', '')}。"
                "你刚和用户打完电话，现在在聊天框自然接着聊。"
            )
            # ★ 2026-09-11 修：这里原来残留了一段 `if result.get("count", 0) > 0:`
            #   （复制粘贴自反思那段的变量 result），而本函数里根本没有 result →
            #   每次挂断都 NameError 被 except 吞掉 → **承接消息永远走兜底台词**，
            #   人也设、也不承接上一通电话的话题。反思本身由 _trigger_reflection 负责，
            #   这段是多余的，直接删掉。
            prompt = (
                f"刚才电话内容：\n{history_text}\n\n"
                "发一条15到35字的消息，具体承接最后的话题，可以关心、"
                "追问或补充一句。不要说‘通话结束’，只输出正文。"
            )
            text = (await chat_once(
                self._brain_model(),
                [{"role": "system", "content": system},
                 {"role": "user", "content": prompt}],
                _config.chat_key(), temperature=0.85, max_tokens=80,
            )).strip()[:80]
        except Exception as e:
            print(f"[VoiceCall] 挂断承接消息生成失败: {e}", flush=True)
            text = "刚才你说的我还记着呢，要不要接着聊？"
        if not text:
            return
        try:
            from . import ws_manager
            await ws_manager.push_to_session(self.session_id, {
                "type": "proactive", "content": text,
                "subtype": "after_call", "contact_id": self.character_id,
                "character_id": self.character_id,
                "user_initiated": True,
                "contact_name": self.character_id,
            })
        except Exception:
            pass
        try:
            from . import db as _db
            _db.add_message(
                self.session_id, "assistant", text, self.character_id,
                {"source": "after_call"},
            )
        except Exception:
            pass

    async def _send_event(self, event_type: str, data: dict = None):
        try:
            payload = {"type": event_type}
            if data:
                payload.update(data)
            await self.ws.send_text(
                json.dumps(payload, ensure_ascii=False)
            )
        except Exception:
            pass


async def _audio_file_to_b64(audio_url: str) -> Optional[str]:
    try:
        import base64
        from . import tts as _tts

        filename   = os.path.basename(audio_url.split("?")[0])
        local_path = os.path.join(_tts._CACHE_DIR, filename)

        if not os.path.exists(local_path):
            return None

        with open(local_path, "rb") as f:
            data = f.read()

        return base64.b64encode(data).decode("utf-8")
    except Exception as e:
        print(f"[VoiceCall] 音频转base64失败: {e}", flush=True)
        return None

    async def _send_after_call_message(self):
        """
        通话结束后，AI 主动发一条文字消息（延迟3秒，像真人挂完电话后打字）。
        内容由 LLM 根据通话内容和人设生成，短而自然。
        例：「刚才聊得好开心 😊」「记得早点睡」「刚才说的事我记住了」
        """
        import asyncio
        await asyncio.sleep(3)   # 模拟真人挂完电话后停顿再打字

        try:
            from . import config as _config
            key   = _config.chat_key()
            model = self._brain_model()   # ★ 2026-09-11：角色卡主脑优先（原 selected_model 全局兜底）
        except Exception:
            key, model = "", ""

        if not key or not model:
            return

        try:
            # 取最近3轮通话内容作为上下文
            recent = self._call_messages[-6:] if self._call_messages else []
            history_text = "\n".join(
                f"{'你' if m.get('role') == 'assistant' else '用户'}：{m.get('content', '')}"
                for m in recent
            ) or "（刚才通话了一会儿）"

            # 构建人设 system prompt（从角色卡读取）
            try:
                from .character_manager import get_character
                char_cfg = get_character(self.character_id) or {}
                sys_prompt = (
                    f"你是{char_cfg.get('character_name', 'AI')}，"
                    f"性格：{char_cfg.get('personality', '')}。"
                    f"你和用户的关系：{char_cfg.get('relationship', '朋友')}。"
                )
            except Exception:
                sys_prompt = "你是用户的AI伴侣。"

            user_prompt = (
                f"你们刚刚结束了一次语音通话，通话内容摘要：\n{history_text}\n\n"
                f"挂断电话后，你想给TA发一条消息。\n"
                f"要求：\n"
                f"- 一句话，10-20字，自然口语\n"
                f"- 贴合你的人设和刚才聊的内容\n"
                f"- 不要以「我」开头，不要解释说明\n"
                f"- 像真人挂完电话后随手发的那种消息\n"
                f"只输出那句话，不要任何其他内容。"
            )

            from .deepseek_api import chat_once
            text = (await chat_once(
                model,
                [
                    {"role": "system",  "content": sys_prompt},
                    {"role": "user",    "content": user_prompt},
                ],
                key, temperature=0.9, max_tokens=60
            )).strip()

            if not text:
                return

            # 截断保险（不超过40字）
            text = text[:40]

            # 通过 WS 主连接推给前端（复用现有 push_to_session 链路）
            try:
                from . import ws_manager
                payload = {
                    "type":    "proactive",
                    "content": text,
                    "subtype": "after_call",
                    "contact_id": self.character_id,
                    "character_id": self.character_id,
                    "user_initiated": True,
                }
                await ws_manager.push_to_session(self.session_id, payload)
            except Exception as e:
                print(f"[VoiceCall] 通话后消息WS推送失败: {e}", flush=True)

            # 同时写入后端消息历史（保持记忆一致性）
            try:
                from . import db
                db.add_message(
                    session_id=self.session_id,
                    character_id=self.character_id,
                    role="assistant",
                    content=text,
                    extra={"source": "after_call"},
                )
            except Exception as e:
                print(f"[VoiceCall] 通话后消息存DB失败: {e}", flush=True)

            print(
                f"[VoiceCall] 通话后消息已发送: "
                f"session={self.session_id} text={text[:20]}",
                flush=True
            )

        except Exception as e:
            print(f"[VoiceCall] 通话后消息生成失败: {e}", flush=True)
