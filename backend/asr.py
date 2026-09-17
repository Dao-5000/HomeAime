# -*- coding: utf-8 -*-
"""
ASR 语音转文本模块
provider 优先级：配置 asr_provider → 无 key 自动降级 whisper_local
失败只返回 None，不崩主流程
"""
import os
import tempfile
import asyncio
from typing import Optional

from .http_client import get_http_client
from backend.loop_compat import get_loop


def _explain_cloud_error(e: Exception, where: str) -> None:
    """把云端 ASR 的异常翻译成"能直接照着做"的一句话。

    ★ 2026-09-10：通话"识别不到我说的话"的最常见原因是百炼(DashScope) Key
      无效/过期 —— 云端直接回 HTTP 401，日志里只有一行
      `InvalidStatus: server rejected WebSocket connection: HTTP 401`，
      看不出该去改什么。这里把 401 单独识别出来并给出可执行的提示。
    """
    txt = f"{type(e).__name__}: {e}"
    low = txt.lower()
    if "401" in txt or "unauthorized" in low or "invalidstatus" in low:
        _mark_cloud_key_bad()
        print(f"[ASR/{where}] ★ 百炼(DashScope) Key 被拒绝(HTTP 401)：Key 无效或已过期。"
              f"请到「设置 → 语音/通话」重新填写百炼 API Key"
              f"（真实 Key 形如 sk- + 32 位十六进制），"
              f"或把 call_asr_provider 改成 whisper_local 直接用本地免费识别。"
              f"（已熔断 {int(_CLOUD_KEY_BAD_COOLDOWN)} 秒：这段时间直接走本地识别）"
              f"原始报错: {txt}", flush=True)
    else:
        print(f"[ASR/{where}] 异常: {txt}", flush=True)


# ★ 云端 Key 被判无效后的熔断：避免用户每说一句话都先撞一次 401、
#   白等一个建连超时，最后才落到本地识别（体验上就是"反应特别慢/识别不到"）。
_CLOUD_KEY_BAD_COOLDOWN = 600.0     # 秒
_cloud_key_bad_until = 0.0


def _mark_cloud_key_bad(seconds: float = _CLOUD_KEY_BAD_COOLDOWN) -> None:
    global _cloud_key_bad_until
    import time as _t
    _cloud_key_bad_until = _t.time() + float(seconds)


def _cloud_key_is_bad() -> bool:
    """云端 Key 是否处于"刚被拒过"的熔断窗口内。"""
    import time as _t
    return _t.time() < _cloud_key_bad_until


async def transcribe(audio_bytes: bytes, filename: str = "audio.wav",
                     mode: str = "chat", input_format: str = "auto") -> Optional[str]:
    """
    入口：bytes → 转写文本 or None
    filename 用于判断格式（wav/mp3/webm/ogg）

    mode 决定走哪条路（★ 通话付费、聊天本地）：
      "chat" 聊天里发的语音消息 → asr_provider()     默认 whisper_local（本地免费）
      "call" 语音通话         → call_asr_provider() 默认 aliyun（云端付费）

    input_format：
      "auto"  按 filename/内容自动解码（webm/wav/mp3/...）
      "pcm16" 已是 16kHz/16bit/单声道 PCM 裸流（前端 AudioWorklet 直出），跳过解码

    云端 provider 失败一律降级本地 Whisper，保证通话不中断。
    """
    from . import config

    if mode == "call":
        provider = (config.call_asr_provider() or "aliyun").lower()
        key      = config.dashscope_api_key() if provider == "aliyun" else config.asr_key()
    else:
        provider = (config.asr_provider() or "whisper_local").lower()
        key      = config.asr_key()

    # 没配 key 强制本地
    if not key and provider != "whisper_local":
        print(f"[ASR] 未配置 {provider} 的 Key，降级本地 Whisper", flush=True)
        provider = "whisper_local"

    # ★ 云端 Key 刚被判无效（401）→ 熔断窗口内直接走本地，别再每句都撞一次 401
    if provider == "aliyun" and _cloud_key_is_bad():
        print("[ASR] 百炼 Key 刚被拒(401)，熔断期内直接走本地 Whisper；"
              "改好 Key 后会自动恢复云端识别", flush=True)
        provider = "whisper_local"

    # ★ whisper 兜底需要 WAV 容器；PCM16 裸流先包一层 WAV 头
    def _whisper_input():
        if input_format == "pcm16":
            return _pcm16_to_wav(audio_bytes), "audio.wav"
        return audio_bytes, filename

    try:
        if provider == "whisper_local":
            wav, fname = _whisper_input()
            return await _whisper_local(wav, fname)

        if provider == "aliyun":
            text = await _aliyun_asr(audio_bytes, key, input_format=input_format)
            if text:
                return text
            print("[ASR] 阿里云识别为空/失败，降级本地 Whisper", flush=True)
            wav, fname = _whisper_input()
            return await _whisper_local(wav, fname)

        if provider == "azure":
            return await _azure_asr(audio_bytes, key)
        if provider == "xunfei":
            return await _xunfei_asr(audio_bytes, key)

        print(f"[ASR] 未知 provider: {provider}，降级本地 Whisper", flush=True)
        return await _whisper_local(audio_bytes, filename)
    except Exception as e:
        print(f"[ASR] 转写失败({provider}): {e}", flush=True)
        # 云端异常时也兜底一次本地，避免通话直接失声
        if provider != "whisper_local":
            try:
                return await _whisper_local(audio_bytes, filename)
            except Exception:
                pass
        return None


# ── 本地 Whisper（免费兜底，首次自动下载 base 模型约150MB）
# ★ 模型常驻缓存：通话/接口首次调用不再等 10~30s 加载，预热后秒级识别
_whisper_model = None
_whisper_model_lock = None  # 延迟初始化（避免 import 时建锁的跨模块问题）
_whisper_model_source = ""  # 模型来自哪个目录/来源（自检与排障用）


def _get_whisper_model():
    global _whisper_model, _whisper_model_lock, _whisper_model_source
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_model_lock is None:
        import threading
        _whisper_model_lock = threading.Lock()
    with _whisper_model_lock:
        if _whisper_model is None:
            import os as _os
            _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            _os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            _os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            from faster_whisper import WhisperModel
            import sys as _sys
            _module_dir = _os.path.dirname(_os.path.abspath(__file__))
            _candidates = [
                _os.path.join(_module_dir, "models", "whisper-base"),
                _os.path.join(getattr(_sys, "_MEIPASS", ""), "models", "whisper-base"),
                _os.path.join(_os.path.dirname(getattr(_sys, "executable", "")), "models", "whisper-base"),
            ]
            _local_dir = next((p for p in _candidates if p and _os.path.exists(_os.path.join(p, "model.bin"))), "")
            if _local_dir:
                _whisper_model = WhisperModel(_local_dir, device="cpu", compute_type="int8")
                _whisper_model_source = f"本地 {_local_dir}"
            else:
                _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
                _whisper_model_source = "HuggingFace base（没找到 models/whisper-base）"
            print("[ASR] Whisper 模型已加载（本地 whisper-base）", flush=True)
    return _whisper_model


def probe_local_asr() -> dict:
    """本地识别链路自检（启动时跑一次，结果打进日志）。

    ★ 2026-09-10 血泪教训：环境坏了（onnxruntime 的 DLL 加载失败 / ffmpeg 取不到 /
      models/whisper-base 被清掉）时，只有等真正打来电话才暴露；而且老日志会把它
      误报成「Whisper 未安装」。启动就自检，坏了当场在日志里看见。
    """
    rep = {"whisper_model": False, "model_source": "", "onnxruntime": False,
           "silero_vad": "", "ffmpeg": "", "errors": []}
    try:
        _get_whisper_model()
        rep["whisper_model"] = _whisper_model is not None
        rep["model_source"] = _whisper_model_source
    except Exception as e:
        rep["errors"].append(f"模型加载失败 {type(e).__name__}: {e}")

    try:
        import onnxruntime  # noqa: F401  （silero VAD 依赖它）
        rep["onnxruntime"] = True
    except Exception as e:
        rep["errors"].append(f"onnxruntime 导入失败 {type(e).__name__}: {e}")

    try:
        import faster_whisper as _fw
        _vad = os.path.join(os.path.dirname(os.path.abspath(_fw.__file__)),
                            "assets", "silero_vad_v6.onnx")
        if os.path.exists(_vad):
            rep["silero_vad"] = _vad
        else:
            rep["silero_vad"] = f"缺失: {_vad}"
            rep["errors"].append(f"silero VAD 模型缺失: {_vad}")
    except Exception as e:
        rep["errors"].append(f"faster_whisper 导入失败 {type(e).__name__}: {e}")

    try:
        rep["ffmpeg"] = _find_ffmpeg() or ""
        if not rep["ffmpeg"]:
            rep["errors"].append("找不到 ffmpeg（webm/opus 兜底解码会失效）")
    except Exception as e:
        rep["errors"].append(f"ffmpeg 定位失败 {type(e).__name__}: {e}")

    return rep


def format_probe(rep: dict) -> str:
    """自检结果 → 单行摘要 + 问题明细。"""
    out = [f"本地识别自检: 模型={'OK' if rep.get('whisper_model') else '失败'}"
           f"({rep.get('model_source') or '-'})"
           f" onnxruntime(VAD)={'OK' if rep.get('onnxruntime') else '失败'}"
           f" ffmpeg={'OK' if rep.get('ffmpeg') else '缺失'}"]
    for e in rep.get("errors") or []:
        out.append("  ! " + str(e))
    if rep.get("errors"):
        out.append("  ! 本地识别链路不完整：云端识别失败时可能兜不住，请把以上内容发给开发者")
    return "\n".join(out)


def _ffmpeg_to_wav(audio_bytes: bytes) -> bytes:
    """同步调用 ffmpeg 把任意音频转成 16kHz 单声道 wav。

    打包版关键修复：faster-whisper 内部用 PyAV 解码 webm，若 PyAV DLL 缺失
    或解码异常会静默返回空文本。先用我们打包进来的 ffmpeg 二进制把音频
    转成 wav，whisper 读 wav 走标准 libavcodec 链路更稳。
    复用 _find_ffmpeg() 的解析（系统 PATH → _MEIPASS → imageio_ffmpeg），
    在任何环境里都能找到 ffmpeg。
    """
    import subprocess as _sp
    import tempfile as _tf
    exe = _find_ffmpeg()
    if not exe:
        return b""
    src = dst = None
    try:
        with _tf.NamedTemporaryFile(suffix=".audio", delete=False) as f:
            f.write(audio_bytes); src = f.name
        dst = src + ".wav"
        proc = _sp.run(
            [exe, "-y", "-i", src, "-f", "wav",
             "-ac", "1", "-ar", "16000", dst],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=30,
        )
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            with open(dst, "rb") as f:
                return f.read()
    except Exception:
        pass
    finally:
        for p in (src, dst):
            if p:
                try: os.unlink(p)
                except OSError: pass
    return b""


async def _whisper_local(audio_bytes: bytes, filename: str) -> Optional[str]:
    import traceback as _tb
    try:
        import asyncio, functools
        # 优先 faster-whisper（快4倍），没装就用原版 openai-whisper
        try:
            # 国内网络：默认走 hf-mirror 镜像（已设则不覆盖）
            import os as _os
            _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            _os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
            _os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            def _run():
                suffix = os.path.splitext(filename)[-1] or ".wav"
                # ★ 打包版关键修复：webm/opus/ogg 等压缩格式直接喂给
                #   faster-whisper 时，它内部用 PyAV 解码；若 PyAV DLL 缺失
                #   或解码异常，会静默返回空文本。先用我们打包进来的 ffmpeg
                #   子进程把音频转成 wav，whisper 读 wav 走 libavcodec 标准链路更稳。
                #
                #   注意：必须用新变量 file_bytes 装解码后的数据，不能 reassign
                #   闭包里的 audio_bytes —— 否则 Python 会把整个 _run 里的
                #   audio_bytes 当成局部变量、未初始化就报错 UnboundLocalError，
                #   异常被外层 except 吞掉、表现为"whisper 始终返回空"。
                file_bytes = audio_bytes
                if suffix in (".webm", ".ogg", ".opus", ".m4a", ".mp4"):
                    try:
                        wav_bytes = _ffmpeg_to_wav(file_bytes)
                        if wav_bytes and len(wav_bytes) > 44:
                            file_bytes = wav_bytes
                            suffix = ".wav"
                            print(f"[ASR] webm→wav 兜底解码成功({len(wav_bytes)}B)", flush=True)
                        else:
                            print(f"[ASR] webm→wav 兜底失败，保持原格式", flush=True)
                    except Exception as _fe:
                        print(f"[ASR] webm→wav 兜底异常(静默): {_fe}", flush=True)
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                    f.write(file_bytes); tmp = f.name
                try:
                    model = _get_whisper_model()   # ★ 复用常驻模型
                    # 通话优先低延迟：单束搜索比默认 beam search 更快；
                    # VAD 同时裁掉头尾静音，减少误识别和无效计算。
                    # 通话短句优先：关闭时间戳/历史条件，缩短 VAD 尾部等待。
                    # faster-whisper 版本不同，vad_parameters 可能不被支持，故做兼容回退。
                    # ★ 通话宁可慢一点也要准：base 模型配 beam_size=1 时，中文短句
                    #   常被识别成空或胡话（表现就是"AI 听不懂你在说什么"）。
                    #   beam_size 提到 5，并用中文 initial_prompt 引导简体中文输出。
                    transcribe_kwargs = dict(
                        language="zh",
                        beam_size=5,
                        best_of=1,
                        temperature=0.0,
                        vad_filter=True,
                        condition_on_previous_text=False,
                        without_timestamps=True,
                        initial_prompt="以下是简体中文的普通话对话。",
                    )
                    transcribe_kwargs["vad_parameters"] = {"min_silence_duration_ms": 220}
                    # 逐级降级：老版本 faster-whisper 不认 vad_parameters / initial_prompt，
                    # 逐个摘掉重试，避免引导参数把整段转写搞挂。
                    def _transcribe_with(kwargs):
                        segs, _ = model.transcribe(tmp, **kwargs)
                        return "".join(s.text for s in segs).strip()

                    text_out = ""
                    for _attempt in range(4):
                        try:
                            text_out = _transcribe_with(transcribe_kwargs)
                            break
                        except TypeError:
                            if "vad_parameters" in transcribe_kwargs:
                                transcribe_kwargs.pop("vad_parameters", None)
                            elif "initial_prompt" in transcribe_kwargs:
                                transcribe_kwargs.pop("initial_prompt", None)
                            else:
                                raise
                        except Exception as _e:
                            # ★ 2026-09-10 修复（通话"识别不到我说的话"的真凶之一）：
                            #   旧实现只在报错文本含 NO_SUCHFILE 时才降级关 VAD。
                            #   但"开 VAD 的这次转写"抛异常的原因是同一类故障的一大片：
                            #     · silero_vad_v6.onnx 缺失      → ONNXRuntimeError: NO_SUCHFILE
                            #     · onnxruntime 的 DLL 加载失败   → ImportError
                            #     · VAD 把整段真人语音裁空        → 空结果
                            #   只认 NO_SUCHFILE 的结果：其它情况异常一路抛到最外层，
                            #   被外层 `except ImportError` 误报成
                            #   「本地 Whisper 未安装，请 pip install faster-whisper」
                            #   ——明明 faster-whisper 打进去了，通话却彻底哑掉。
                            #   现在：只要开着 VAD 就失败，一律关掉 VAD 再试一次
                            #   （不做 VAD 裁剪照样能转写，只是首尾带一点静音，
                            #     远好于整条链路瘫痪）。
                            if transcribe_kwargs.get("vad_filter"):
                                print(f"[ASR] 开 VAD 的转写失败，改用无 VAD 重试: "
                                      f"{type(_e).__name__}: {_e}", flush=True)
                                transcribe_kwargs["vad_filter"] = False
                                transcribe_kwargs.pop("vad_parameters", None)
                                continue
                            raise

                    # ★ VAD 裁剪过狠会把真实语音整段裁掉：实测 12KB 音频（约1.5秒
                    #   真人说话）经 Silero VAD 过滤后返回空文本，且每次都空——
                    #   表现为"聆听中→通话中"循环、永远没有识别结果。
                    #   转写为空且开着 VAD 时，关闭 VAD 原样重试一次。
                    if not text_out and transcribe_kwargs.get("vad_filter"):
                        try:
                            _retry_kw = dict(transcribe_kwargs)
                            _retry_kw["vad_filter"] = False
                            _retry_kw.pop("vad_parameters", None)
                            text_out = _transcribe_with(_retry_kw)
                            if text_out:
                                print(f"[ASR] VAD裁空兜底成功: {text_out[:40]!r}", flush=True)
                            else:
                                print(f"[ASR] 关闭VAD重试仍为空(音频{len(audio_bytes)}B)", flush=True)
                        except Exception as _ve:
                            print(f"[ASR] 关闭VAD重试失败(静默): {_ve}", flush=True)
                    return text_out
                finally:
                    os.unlink(tmp)
            return await get_loop().run_in_executor(None, _run)
        except ImportError as _imp:
            # ★ 2026-09-10 修复：旧实现走到这里就 `import whisper`，失败即报
            #   「本地 Whisper 未安装，请：pip install faster-whisper」——
            #   把真正的原因（到底哪个模块/DLL 没导进来）整个吞掉了，
            #   日志指向完全错误的方向，是这次通话排障最大的坑。
            #   现在分三步：① 清导入缓存重试一次（打包版解压目录/DLL 偶发
            #   不可用，重试常常就好）② 退原版 openai-whisper ③ 都不行才报告，
            #   并且一定带上真实堆栈。
            print(f"[ASR] faster-whisper 导入失败: {type(_imp).__name__}: {_imp}", flush=True)
            try:
                import importlib
                importlib.invalidate_caches()
                return await get_loop().run_in_executor(None, _run)
            except ImportError as _imp2:
                print(f"[ASR] 清缓存重试仍导入失败: {type(_imp2).__name__}: {_imp2}", flush=True)
                print("[ASR] faster-whisper 真实堆栈：\n" + _tb.format_exc(), flush=True)
            try:
                import whisper
            except ImportError:
                print("[ASR] 本地识别不可用：faster-whisper 与 openai-whisper 都导不进来"
                      "（真原因见上面的堆栈，而不是「没装 faster-whisper」这么简单）",
                      flush=True)
                return None
            def _run2():
                suffix = os.path.splitext(filename)[-1] or ".wav"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                    f.write(audio_bytes); tmp = f.name
                try:
                    model = whisper.load_model("base")
                    result = model.transcribe(tmp, language="zh")
                    return result["text"].strip()
                finally:
                    os.unlink(tmp)
            return await get_loop().run_in_executor(None, _run2)
    except ImportError:
        # 兜底：_run / _run2 内部还在抛 ImportError（例如关掉 VAD 之后
        # 仍缺某个 C 扩展）。打印真实堆栈，绝不再谎报"未安装"。
        print("[ASR] 本地 Whisper 链路 ImportError（真实堆栈）:\n" + _tb.format_exc(), flush=True)
        return None


# ── Azure Speech（商用推荐，和 TTS 同一个 Key）
async def _azure_asr(audio_bytes: bytes, key: str) -> Optional[str]:
    import azure.cognitiveservices.speech as speechsdk
    region = os.environ.get("AZURE_SPEECH_REGION", "eastasia")
    cfg    = speechsdk.SpeechConfig(subscription=key, region=region)
    cfg.speech_recognition_language = "zh-CN"
    suffix = ".wav"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp = f.name
        # 文件已关闭（delete=False 保留在磁盘），识别放线程池（★ recognize_once 同步阻塞）
        audio_cfg  = speechsdk.audio.AudioConfig(filename=tmp)
        recognizer = speechsdk.SpeechRecognizer(speech_config=cfg, audio_config=audio_cfg)
        result     = await asyncio.to_thread(recognizer.recognize_once)
        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            return result.text.strip()
        print(f"[ASR] Azure 识别失败: {result.reason}", flush=True)
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ── 讯飞 ASR（国内低延迟）
async def _xunfei_asr(audio_bytes: bytes, key: str) -> Optional[str]:
    import httpx, base64, hashlib, hmac, time, json
    app_id  = os.environ.get("XUNFEI_APP_ID", "")
    api_sec = os.environ.get("XUNFEI_API_SECRET", "")
    if not app_id:
        print("[ASR] 讯飞未配置 XUNFEI_APP_ID", flush=True)
        return None
    # 讯飞短句识别 REST 接口
    url  = "https://iat-api.xfyun.cn/v2/iat"
    body = {
        "common":  {"app_id": app_id},
        "business": {"language": "zh_cn", "domain": "iat", "accent": "mandarin"},
        "data": {
            "status": 3,
            "format": "audio/L16;rate=16000",
            "encoding": "raw",
            "audio": base64.b64encode(audio_bytes).decode()
        }
    }
    client = get_http_client()
    r = await client.post(url, json=body,
                          headers={"Authorization": f"Basic {base64.b64encode(f'{app_id}:{key}'.encode()).decode()}"})
    data = r.json()
    if data.get("code") == 0:
        return data.get("data", {}).get("result", {}).get("ws", [{}])[0].get("cw", [{}])[0].get("w", "")
    print(f"[ASR] 讯飞失败: {data.get('message')}", flush=True)
    return None


# ── 音频解码：任意格式 → PCM16 16kHz 单声道（Paraformer 要求）────────────

def _decode_with_av(data: bytes) -> bytes:
    """PyAV 解码（纯 Python，打包后也可用，不依赖 ffmpeg 可执行文件）"""
    import io as _io
    import av
    container = av.open(_io.BytesIO(data), mode="r")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    out = bytearray()
    try:
        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                out.extend(resampled.to_ndarray().tobytes())
        for resampled in resampler.resample(None):
            out.extend(resampled.to_ndarray().tobytes())
    finally:
        try:
            container.close()
        except Exception:
            pass
    return bytes(out)


def _find_ffmpeg() -> str:
    """定位可用的 ffmpeg 可执行文件。

    优先级：系统 PATH → PyInstaller 冻结目录 → imageio-ffmpeg 自带二进制。
    很多用户机器没装系统 ffmpeg，PyAV 又在某些环境下解码失败，
    此时若拿不到任何 ffmpeg，兜底解码就永远返回空（通话听不见）。
    """
    import shutil as _shutil
    import sys as _sys

    exe = _shutil.which("ffmpeg")
    if exe:
        return exe

    # ★ PyInstaller 冻结环境：imageio_ffmpeg 用 importlib.resources 定位
    #   binaries/ 目录，而模块被压进 PYZ 后取不到真实文件系统路径
    #   （as_file() 的临时目录在 with 退出后即被清理），
    #   必须直接从 _MEIPASS 按相对路径找，否则打包版兜底解码静默失效。
    meipass = getattr(_sys, "_MEIPASS", "")
    if meipass:
        try:
            import glob as _glob
            hits = sorted(_glob.glob(
                os.path.join(meipass, "imageio_ffmpeg", "binaries", "ffmpeg*.exe")))
            if hits and os.path.exists(hits[0]):
                return hits[0]
        except Exception:
            pass

    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and os.path.exists(bundled):
            return bundled
    except Exception:
        pass
    return ""


async def _decode_with_ffmpeg(data: bytes) -> bytes:
    """ffmpeg 命令行兜底解码"""
    exe = _find_ffmpeg()
    if not exe:
        return b""
    src = dst = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
            f.write(data)
            src = f.name
        dst = src + ".pcm"
        proc = await asyncio.create_subprocess_exec(
            exe, "-y", "-i", src, "-f", "s16le", "-ac", "1", "-ar", "16000", dst,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
        if proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            with open(dst, "rb") as f:
                return f.read()
    except Exception as e:
        print(f"[ASR] ffmpeg 转码失败: {e}", flush=True)
    finally:
        for p in (src, dst):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    return b""


def _pcm16_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """PCM16 裸流 → WAV（加 44 字节标准头），供 whisper 等需要容器的解码器使用。"""
    import struct
    if not pcm:
        return b""
    data_size = len(pcm) - (len(pcm) % 2)   # 对齐偶数字节
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE", b"fmt ", 16,
        1, channels, sample_rate, sample_rate * channels * 2,
        channels * 2, 16, b"data", data_size,
    )
    return header + pcm[:data_size]


async def _to_pcm16k(audio_bytes: bytes) -> bytes:
    """任意格式音频（webm/opus/wav/mp3）→ PCM16 16kHz 单声道"""
    if not audio_bytes:
        return b""
    try:
        pcm = await asyncio.to_thread(_decode_with_av, audio_bytes)
        if pcm:
            return pcm
    except Exception as e:
        print(f"[ASR] PyAV 解码失败: {e}", flush=True)
    return await _decode_with_ffmpeg(audio_bytes)


# ── 阿里云 ASR（DashScope Paraformer 实时语音识别 · WebSocket 流式）────────
async def _aliyun_asr(audio_bytes: bytes, key: str,
                       input_format: str = "auto") -> Optional[str]:
    """
    协议要点（官方踩坑总结，顺序与字段都不能改）：
      run-task → 【直接发二进制 PCM 帧，不能带 continue-task】
      → finish-task（payload.input 必须存在，否则任务不结束）
      → 收 result-generated（payload.output.sentence.text）→ task-finished

    input_format == "pcm16" 时，audio_bytes 已是 16k/16bit/mono PCM 裸流，
    跳过本地解码，直接作为 PCM 帧发给 DashScope。
    """
    import json as _json
    import uuid as _uuid
    import time as _time

    try:
        import websockets
    except ImportError:
        print("[ASR/阿里云] 缺少 websockets 库（pip install websockets）", flush=True)
        return None

    if input_format == "pcm16":
        pcm = audio_bytes
    else:
        pcm = await _to_pcm16k(audio_bytes)
    if not pcm:
        print("[ASR/阿里云] 音频解码为空，无法识别", flush=True)
        return None

    from . import config as _cfg
    model = _cfg.aliyun_asr_model() or "paraformer-realtime-v2"
    # DashScope 实时语音识别 WebSocket：
    #   - 端点：/api-ws/v1/inference   （/api/v1/services/audio/asr/transcription 是 REST 路径，会 400）
    #   - 鉴权：Authorization: Bearer {key} header  （?token= query 参数不会被接受，会 401）
    url = os.environ.get(
        "DASHSCOPE_ASR_WS",
        "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
    )
    headers = {"Authorization": f"Bearer {key}"}
    task_id  = _uuid.uuid4().hex
    print(f"[ASR/阿里云] 开始识别: model={model} pcm={len(pcm)}B", flush=True)

    text_out, last_text = "", ""
    try:
        _connect = websockets.connect
        try:
            ctx = _connect(url, additional_headers=headers, open_timeout=10,
                           ping_interval=None, max_size=None)
        except TypeError:
            ctx = _connect(url, extra_headers=headers, open_timeout=10,
                           ping_interval=None, max_size=None)

        async with ctx as ws:
            # 1) 开启任务
            await ws.send(_json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": model,
                    "parameters": {
                        "sample_rate": 16000,
                        "format": "pcm",
                        "max_sentence_silence": 500,
                        "language_hints": ["zh"],
                    },
                    "input": {},
                },
            }))

            # 2) 等 task-started（最多 10s）
            started  = False
            deadline = _time.time() + 10
            while _time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.5, deadline - _time.time()))
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    ev = _json.loads(msg)
                except Exception:
                    continue
                event = (ev.get("header") or {}).get("event")
                if event == "task-started":
                    started = True
                    break
                if event == "task-failed":
                    print(f"[ASR/阿里云] 任务开启失败: "
                          f"{(ev.get('header') or {}).get('error_message')}", flush=True)
                    return None
            if not started:
                print("[ASR/阿里云] 未收到 task-started", flush=True)
                return None

            # 3) 直接发二进制 PCM 帧（3200B = 100ms @16k/16bit 单声道）
            for i in range(0, len(pcm), 3200):
                await ws.send(pcm[i:i + 3200])

            # 4) 结束任务（payload.input 不能省）
            await ws.send(_json.dumps({
                "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            }))

            # 5) 收结果直到 task-finished（最多 10s）
            deadline = _time.time() + 10
            while _time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.3, deadline - _time.time()))
                except asyncio.TimeoutError:
                    break
                if isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    ev = _json.loads(msg)
                except Exception:
                    continue
                event = (ev.get("header") or {}).get("event")
                if event == "result-generated":
                    sentence = ((ev.get("payload") or {}).get("output") or {}).get("sentence") or {}
                    t = sentence.get("text") or ""
                    if t:
                        last_text = t          # 兜底：最终结果丢失时用它
                    if (sentence.get("end_time") or 0) > 0 or sentence.get("sentence_end"):
                        text_out += t
                        last_text = ""
                elif event == "task-finished":
                    break
                elif event == "task-failed":
                    print(f"[ASR/阿里云] 识别失败: "
                          f"{(ev.get('header') or {}).get('error_message')}", flush=True)
                    break
    except Exception as e:
        _explain_cloud_error(e, "阿里云")
        return None

    result = (text_out or last_text).strip()
    print(f"[ASR/阿里云] 结果: {result[:50]!r}", flush=True)
    return result or None


class AliyunAsrStream:
    """DashScope 实时 ASR 流式连接（降低通话延迟的核心）。

    与 _aliyun_asr 一次性识别不同，这里把建连 + run-task 提前到用户刚开始
    说话时（begin_speech），说话的过程中持续 feed PCM，服务端同步识别；
    用户说完（end_speech）时只需 finish-task 收最终结果，省掉"说完整句 →
    建连 → 发整句 → 识别整句"的串行等待（约 1~1.5 秒）。

    用法：
        stream = AliyunAsrStream(key, model)
        ok = await stream.start()      # 建连 + run-task + 等 task-started
        await stream.feed(pcm)         # 边说话边喂（可多次）
        text = await stream.finish()   # finish-task + 收结果
        await stream.close()
    """

    def __init__(self, key: str, model: str = "paraformer-realtime-v2"):
        self.key = key
        self.model = model
        self._ctx = None
        self.ws = None
        self.task_id = None
        self._connected = False
        self._text_out = ""
        self._last_text = ""
        self._fed_bytes = 0        # 累计喂进去的 PCM 字节（诊断：喂帧是否落后）

    @property
    def ready(self) -> bool:
        return self._connected and self.ws is not None

    async def start(self) -> bool:
        import json as _json
        import uuid as _uuid
        import time as _time
        # ★ Key 刚被判无效的熔断期内不再建连：省掉每句话一次 401 的无效等待，
        #   调用方（voice_call）会落到一次性本地 Whisper 兜底。
        if _cloud_key_is_bad():
            return False
        import websockets
        self.task_id = _uuid.uuid4().hex
        url = os.environ.get(
            "DASHSCOPE_ASR_WS",
            "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        )
        headers = {"Authorization": f"Bearer {self.key}"}
        try:
            self._ctx = websockets.connect(
                url, additional_headers=headers, open_timeout=10,
                ping_interval=None, max_size=None)
        except TypeError:
            self._ctx = websockets.connect(
                url, extra_headers=headers, open_timeout=10,
                ping_interval=None, max_size=None)
        try:
            self.ws = await self._ctx.__aenter__()
        except Exception as e:
            _explain_cloud_error(e, "流式")
            self.ws = None
            return False

        await self.ws.send(_json.dumps({
            "header": {"action": "run-task", "task_id": self.task_id, "streaming": "duplex"},
            "payload": {
                "task_group": "audio", "task": "asr", "function": "recognition",
                "model": self.model,
                "parameters": {
                    "sample_rate": 16000, "format": "pcm",
                    # ★ 500 → 300：服务端收句更快（配合 finish() 里的补静音，
                    #   通话"说完→出字"实测从 1.9s 降到 ~0.5s）
                    "max_sentence_silence": 300, "language_hints": ["zh"],
                },
                "input": {},
            },
        }))

        deadline = _time.time() + 10
        while _time.time() < deadline:
            try:
                msg = await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.5, deadline - _time.time()))
            except asyncio.TimeoutError:
                break
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                ev = _json.loads(msg)
            except Exception:
                continue
            event = (ev.get("header") or {}).get("event")
            if event == "task-started":
                self._connected = True
                return True
            if event == "task-failed":
                return False
        return False

    async def feed(self, pcm: bytes):
        """喂 PCM 帧；连接未就绪时静默丢弃（音频已由调用方缓冲，降级路径会兜底）。"""
        if not self.ready:
            return
        try:
            for i in range(0, len(pcm), 3200):
                await self.ws.send(pcm[i:i + 3200])
            self._fed_bytes += len(pcm)
        except Exception:
            self._connected = False

    async def finish(self) -> str:
        import json as _json
        import time as _time
        if not self.ready:
            return ""
        t0 = _time.time()
        # ★ 2026-09-10 收尾提速：先补 300ms 静音，再发 finish-task。
        #   实测同一段真人音频：直接 finish → 收尾 0.40s；补静音 → 0.05~0.10s。
        #   原因：服务端靠 VAD 的静音判定收句，补静音让它立刻收句，
        #   否则 finish-task 之后还要等它自己的静音计时（通话里这 1~2 秒就是
        #   "说完话到她开口"之间那段空白）。
        try:
            await self.ws.send(b"\x00" * (16000 * 2 * 300 // 1000))   # 300ms @16k/16bit
        except Exception:
            pass
        try:
            await self.ws.send(_json.dumps({
                "header": {"action": "finish-task", "task_id": self.task_id, "streaming": "duplex"},
                "payload": {"input": {}},
            }))
        except Exception:
            return ""
        deadline = _time.time() + 10
        t_sent = _time.time()
        n_ev = 0
        while _time.time() < deadline:
            try:
                msg = await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.3, deadline - _time.time()))
            except asyncio.TimeoutError:
                break
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                ev = _json.loads(msg)
            except Exception:
                continue
            n_ev += 1
            event = (ev.get("header") or {}).get("event")
            if event == "result-generated":
                sentence = ((ev.get("payload") or {}).get("output") or {}).get("sentence") or {}
                t = sentence.get("text") or ""
                if t:
                    self._last_text = t
                if (sentence.get("end_time") or 0) > 0 or sentence.get("sentence_end"):
                    self._text_out += t
                    self._last_text = ""
            elif event in ("task-finished", "task-failed"):
                break
        # ★ 收尾耗时诊断（2026-09-10）：通话"说完 → 出字"里 ASR 占了多少，
        #   以及服务端到底收到多少音频（喂帧是否落后）都看这一行。
        print(f"[ASR/流式] 收尾: 已喂={self._fed_bytes}B "
              f"准备={t_sent - t0:.2f}s 等待结果={_time.time() - t_sent:.2f}s "
              f"事件数={n_ev} 文本={len((self._text_out or self._last_text).strip())}字",
              flush=True)
        return (self._text_out or self._last_text).strip()

    async def close(self):
        try:
            if self._ctx is not None:
                await self._ctx.__aexit__(None, None, None)
        except Exception:
            pass
        self._ctx = None
        self.ws = None
        self._connected = False
