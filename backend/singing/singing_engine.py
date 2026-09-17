# -*- coding: utf-8 -*-
"""
唱歌合成引擎（结合现有架构）

- 歌词情感分析（C方案）：每句歌词 → climax/tender/sad/rap/normal
- 逐句调用 CosyVoice instruct2（情感指令驱动，接现有 tts.py 的 COSYVOICE_API）
- 双缓冲流式：第 N 句播放时 N+1 句已在合成，消除句间停顿
- 句间停顿（呼吸感）：情感对应停顿 + 每 4 句段落停顿
- fallback：CosyVoice 失败 → edge-tts 朗读（现有 tts.generate_tts）
"""
import asyncio
import os
import re
from typing import Optional, AsyncGenerator, List

from .. import config as _cfg

# CosyVoice API（与 tts.py 共用 env，默认 9881）
COSYVOICE_URL = os.environ.get("COSYVOICE_API", "http://localhost:9881")


class SingingSegment:
    def __init__(self, line, audio, line_index, is_last, emotion="normal", pause_after=0.3):
        self.line = line
        self.audio = audio
        self.line_index = line_index
        self.is_last = is_last
        self.emotion = emotion
        self.pause_after = pause_after


# ── 情感分析（C方案核心）───────────────────────────
_CLIMAX_KWS = ["爱", "永远", "一辈子", "心", "痛", "泪", "不能没有", "只有你", "全世界", "最"]
_SAD_KWS    = ["泪", "哭", "离开", "再见", "消失", "孤独", "寂寞", "失去", "错过", "伤口", "破碎", "遗憾"]
_TENDER_KWS = ["轻轻", "悄悄", "慢慢", "温柔", "微笑", "陪", "靠", "抱", "暖", "慢慢来"]


def analyze_emotion(line: str, line_index: int, total: int) -> str:
    """轻量情感分析（纯规则，零成本）。"""
    is_climax_pos = total >= 4 and line_index >= total * 0.6
    if "!" in line or "！" in line or any(k in line for k in _CLIMAX_KWS):
        if is_climax_pos or "！" in line:
            return "climax"
    if any(k in line for k in _SAD_KWS):
        return "sad"
    if any(k in line for k in _TENDER_KWS):
        return "tender"
    if len(line) >= 10 and len(set(line)) / max(1, len(line)) < 0.55:
        return "rap"
    if is_climax_pos and len(line) >= 6:
        return "climax"
    return "normal"


# 情感 → CosyVoice instruct 指令（描述性演唱，比 <|singing|> hack 更稳）
_EMOTION_INSTRUCT = {
    "climax": "用饱满激昂的情感演唱这句歌词，声音有力，带一点颤音，像歌曲高潮部分那样全力投入，音量稍大，尾音拉长",
    "tender": "用轻柔温柔的声音演唱这句歌词，像对耳边轻声诉说，气息柔和，咬字清晰但不用力，带一丝甜美感",
    "sad":    "用低沉略带忧郁的情感演唱这句歌词，语速稍慢，声音微微哽咽，尾音轻轻下沉，带着不舍",
    "rap":    "用rap说唱的方式演绎这句歌词，节奏感强，咬字清晰有力，语速偏快，有明显的律动感",
    "normal": "用自然流畅的方式演唱这句歌词，声音明亮，咬字清晰，情感真诚，不过分用力",
}

# 情感对应句后停顿（秒）
_EMOTION_PAUSE = {"climax": 0.6, "tender": 0.4, "sad": 0.5, "rap": 0.2, "normal": 0.3}
_PHRASE_PAUSE = 0.45   # 每4句加段落停顿


def clean_lyric_lines(lines):
    seen = set()
    out = []
    for line in lines:
        line = str(line or "").strip()
        if not line:
            continue
        if re.match(r"^\[.*\]$", line):
            continue
        if re.match(r"^[\W\d\s]+$", line):
            continue
        if len(line) < 2:
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


async def _cosyvoice_sing(line: str, instruct_text: str, spk_id: str) -> Optional[bytes]:
    """调用 CosyVoice instruct2 唱歌（情感指令驱动）。CPU 合成慢，超时给足 300s。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{COSYVOICE_URL}/inference_instruct2",
                data={
                    "tts_text":      line,
                    "instruct_text": instruct_text,
                    "spk_id":        spk_id,
                    "stream":        "false",
                },
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
            print(f"[Singing/CosyVoice] 失败: {resp.status_code} {resp.text[:100]}", flush=True)
    except Exception as e:
        print(f"[Singing/CosyVoice] 异常: {e}", flush=True)
    return None


async def _fallback_tts(line: str) -> Optional[bytes]:
    """CosyVoice 不可用 → edge-tts 朗读歌词（读回音频 bytes）。"""
    try:
        from ..tts import generate_audio, get_voice_cfg, _CACHE_DIR
        import os as _os
        cfg = get_voice_cfg("edge_xiaoxiao") or {}
        url = await generate_audio(line, cfg, "")
        if not url:
            return None
        fname = _os.path.basename(url.split("?")[0])
        local = _os.path.join(_CACHE_DIR, fname)
        if _os.path.exists(local):
            with open(local, "rb") as f:
                return f.read()
    except Exception as _e:
        print(f"[Singing] fallback TTS 失败(静默): {_e}", flush=True)
    return None


def _spk_id_for(voice_cfg):
    """从 voice_cfg 取 CosyVoice 音色；非 cosyvoice 或无配置 → 默认女声。"""
    if voice_cfg and voice_cfg.get("provider") == "cosyvoice":
        return voice_cfg.get("voice_id") or "中文女声"
    return "中文女声"


async def synthesize_line(line: str, emotion: str, spk_id: str) -> Optional[bytes]:
    """合成一句歌词（CosyVoice 情感唱 → fallback 朗读）。"""
    # ★ 2026-09-12 惰性化：唱歌是用户主动触发的语音需求，这里按需拉起 CosyVoice
    #   （最多等 40s 首次加载）；拉不起走既有朗读兜底。
    try:
        from .. import cosyvoice_mgr
        await cosyvoice_mgr.ensure_async(40)
    except Exception:
        pass
    instruct = _EMOTION_INSTRUCT.get(emotion, _EMOTION_INSTRUCT["normal"])
    audio = await _cosyvoice_sing(line, instruct, spk_id)
    if not audio:
        audio = await _fallback_tts(line)
    return audio


async def sing_lyrics_stream(
    lines: List[str],
    voice_cfg=None,
) -> AsyncGenerator[SingingSegment, None]:
    """
    双缓冲流式唱歌：合成超前播放一句，逐句 yield。
    情感分析 + 句间停顿（呼吸感）。
    """
    clean = clean_lyric_lines(lines)
    total = len(clean)
    if total == 0:
        return

    spk_id = _spk_id_for(voice_cfg)
    buffer: asyncio.Queue = asyncio.Queue(maxsize=2)

    async def _synthesize_one(i, line):
        emotion = analyze_emotion(line, i, total)
        pause = _EMOTION_PAUSE.get(emotion, 0.3)
        if i > 0 and i % 4 == 0:
            pause += _PHRASE_PAUSE
        # ★ 串行合成：CosyVoice 单模型不支持并发推理，一句合成完再下一句（更稳）
        audio = await synthesize_line(line, emotion, spk_id)
        if audio:
            await buffer.put(SingingSegment(
                line=line, audio=audio, line_index=i,
                is_last=(i == total - 1),
                emotion=emotion, pause_after=pause,
            ))
        else:
            print(f"[Singing] 第{i}句合成失败跳过: {line[:12]}", flush=True)

    async def _producer():
        for i, line in enumerate(clean):
            try:
                await _synthesize_one(i, line)
            except Exception:
                print(f"[Singing] 第{i}句合成异常跳过", flush=True)
        await buffer.put(None)

    producer_task = asyncio.create_task(_producer())
    try:
        while True:
            seg = await buffer.get()
            if seg is None:
                break
            yield seg
    finally:
        producer_task.cancel()
        try:
            await producer_task
        except asyncio.CancelledError:
            pass
