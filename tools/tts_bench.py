# -*- coding: utf-8 -*-
"""
CosyVoice 本地服务基准测量（任务1 GPU / 任务2 流式 共用回路）
用法: python tools/tts_bench.py [stream]
输出: first_byte_latency / total_time / audio_duration / rtf
"""
import asyncio
import struct
import sys
import time

import httpx

URL = "http://127.0.0.1:9881/inference_sft"
TEXT = "你好呀，我是你的AI伴侣。今天过得怎么样？有没有想我呀。我给你讲个笑话吧。有一只小鸭子，走路摇摇晃晃的。可爱极了。"

WAV_HEADER_SIZE = 44


def _wav_duration(total_bytes: int, header: bytes) -> float:
    """从 wav 头解析采样率/字节率，估算音频时长（秒）"""
    try:
        byte_rate = struct.unpack("<I", header[28:32])[0]
    except Exception:
        byte_rate = 22050 * 2
    if byte_rate <= 0:
        byte_rate = 22050 * 2
    return max(0.0, (total_bytes - WAV_HEADER_SIZE) / byte_rate)


async def bench(stream_flag: str):
    t0 = time.perf_counter()
    first_ts = None
    first_hdr = b""
    chunks = 0
    chunk_times = []
    total = 0
    async with httpx.AsyncClient(timeout=600) as c:
        async with c.stream(
            "POST", URL,
            data={"tts_text": TEXT, "spk_id": "中文女", "stream": stream_flag},
        ) as r:
            status = r.status_code
            async for ch in r.aiter_bytes(4096):
                if first_ts is None:
                    first_ts = time.perf_counter()
                    first_hdr = ch[:44]
                if stream_flag == "true":
                    # 每个以 RIFF 开头的传输段 = 一个新 wav 块（句子）的开始
                    if ch.startswith(b"RIFF"):
                        chunks += 1
                        chunk_times.append(time.perf_counter() - t0)
                    else:
                        # RIFF 头可能跨传输段边界，在段内搜索
                        idx = ch.find(b"RIFF")
                        if idx > 0 and len(ch) - idx >= 4:
                            chunks += 1
                            chunk_times.append(time.perf_counter() - t0)
                total += len(ch)
    t1 = time.perf_counter()

    dur = _wav_duration(total, first_hdr)
    first_lat = first_ts - t0 if first_ts else -1
    total_time = t1 - t0
    rtf = total_time / dur if dur > 0 else -1
    print(f"stream={stream_flag} status={status} bytes={total} wav_chunks={chunks}")
    if chunk_times:
        print("  chunk_arrival_ts = " + ", ".join(f"{t:.2f}" for t in chunk_times))
    print(f"  first_byte_latency = {first_lat:.3f}s")
    print(f"  total_time         = {total_time:.3f}s")
    print(f"  audio_duration     = {dur:.3f}s")
    print(f"  rtf                = {rtf:.3f}")


if __name__ == "__main__":
    flag = sys.argv[1] if len(sys.argv) > 1 else "false"
    asyncio.run(bench(flag))
