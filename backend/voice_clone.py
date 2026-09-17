"""
本地音色克隆模块
支持：
  1. GPT-SoVITS（本地免费，音质最好）
  2. RVC（本地免费，适合说话音色）
  3. ElevenLabs Voice Clone（云端，商用精度高）
用户上传 WAV/MP3 参考音频 → 训练/注册 → 生成 voice_id → 写入 PRESET_VOICES
"""
import os
import uuid
import json
import asyncio
from typing import Optional
from .tts import PRESET_VOICES
from . import config as _config

# 克隆音色存储目录：存到持久数据目录（打包版走 AI_COMPANION_DATA_DIR）。
# 不能用 os.path.dirname(__file__) —— PyInstaller 解压到临时 _MEIXXXX 目录，重启路径失效，
# 导致角色引用的克隆音色全部丢失，回退成官方参考音色。
_CLONE_DIR = os.path.join(str(getattr(_config, "DATA_DIR", "") or os.path.dirname(__file__)), "voice_clone_models")
os.makedirs(_CLONE_DIR, exist_ok=True)

# 克隆音色注册表持久化路径
_CLONE_REGISTRY = os.path.join(_CLONE_DIR, "registry.json")


def _load_registry() -> dict:
    if os.path.exists(_CLONE_REGISTRY):
        try:
            with open(_CLONE_REGISTRY, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_registry(registry: dict):
    with open(_CLONE_REGISTRY, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


async def upload_and_register_voice(
    audio_bytes: bytes,
    filename: str,
    label: str,
    provider: str = "gptsovits",  # gptsovits / rvc / elevenlabs
    user_id: str = "default",
    prompt_text: str = ""
) -> dict:
    """
    上传参考音频 → 克隆注册 → 返回 voice_key
    provider: gptsovits（本地） / elevenlabs（云端）
    """
    voice_key = f"clone_{user_id}_{uuid.uuid4().hex[:8]}"
    ext       = os.path.splitext(filename)[-1] or ".mp3"
    ref_path  = os.path.join(_CLONE_DIR, f"{voice_key}{ext}")

    # 1. 保存参考音频
    with open(ref_path, "wb") as f:
        f.write(audio_bytes)

    # CosyVoice zero-shot requires the transcript of the uploaded reference.
    # An empty prompt makes the local service use its bundled demo sentence,
    # which sounds like the default voice instead of the uploaded speaker.
    prompt_text = str(prompt_text or "").strip()
    if provider == "cosyvoice":
        try:
            if not prompt_text:
                from . import asr as _asr
                prompt_text = (await _asr.transcribe(audio_bytes, filename=filename) or "").strip()
        except Exception as exc:
            print(f"[VoiceClone] reference transcription failed: {exc}", flush=True)
        if not prompt_text:
            print("[VoiceClone] 未识别到参考音频原文，仍继续保存参考音频，后续可手动补填 prompt_text", flush=True)

    # 2. 按 provider 注册
    voice_id = None
    if provider == "gptsovits":
        voice_id = await _register_gptsovits(voice_key, ref_path)
    elif provider == "rvc":
        voice_id = await _register_rvc(voice_key, ref_path)
    elif provider == "elevenlabs":
        voice_id = await _register_elevenlabs(voice_key, label, ref_path)

    elif provider == "cosyvoice":
        voice_id = await _register_cosyvoice(voice_key, ref_path)

    else:
        voice_id = voice_key  # 直接用参考音频路径做 voice_id（最简降级）

    if not voice_id:
        return {"success": False, "msg": "克隆失败，请检查音频质量（建议10~30秒干净人声）"}

    # 3. 写入运行时 PRESET_VOICES
    cfg = {
        "label":    label,
        "provider": provider,
        "voice_id": voice_id,
        "speed":    1.0,
        "pitch":    1.0,
        "tags":     ["自定义", "克隆"],
        "ref_audio": ref_path  # gptsovits/rvc 推理时需要参考音频
    }
    if prompt_text:
        cfg["prompt_text"] = prompt_text
    PRESET_VOICES[voice_key] = cfg

    # 4. 持久化到 registry.json（服务重启后重新加载）
    registry = _load_registry()
    registry[voice_key] = cfg
    _save_registry(registry)

    return {
        "success": True,
        "voice_key": voice_key,
        "label": label,
        "voice": {"voice_key": voice_key, **cfg},
        "warning": (
            "未识别到参考音频原文；已保存音色，但建议填写原文后重新克隆，相似度会更高"
            if provider == "cosyvoice" and not prompt_text else ""
        ),
    }


def load_cloned_voices():
    """服务启动时把持久化克隆音色加载回 PRESET_VOICES"""
    registry = _load_registry()
    for k, v in registry.items():
        if k not in PRESET_VOICES:
            PRESET_VOICES[k] = v
    print(f"[VoiceClone] 加载 {len(registry)} 个克隆音色", flush=True)


# ── GPT-SoVITS 本地推理注册
async def _register_gptsovits(voice_key: str, ref_path: str) -> Optional[str]:
    """
    GPT-SoVITS 本地部署时，参考音频即是 voice_id（推理时带入路径）
    需要本地跑 GPT-SoVITS API 服务（默认 http://localhost:9880）
    """
    gptsovits_url = os.environ.get("GPTSOVITS_API", "http://localhost:9880")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{gptsovits_url}/")
        if r.status_code == 200:
            print(f"[VoiceClone] GPT-SoVITS 在线，注册 {voice_key}", flush=True)
            return ref_path  # 推理时用 ref_path 作为参考音频
    except Exception:
        pass
    print("[VoiceClone] GPT-SoVITS 服务不在线，降级存参考音频路径", flush=True)
    return ref_path


# ── RVC 本地推理注册
async def _register_rvc(voice_key: str, ref_path: str) -> Optional[str]:
    """RVC 本地服务注册，类似 GPT-SoVITS"""
    rvc_url = os.environ.get("RVC_API", "http://localhost:7865")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{rvc_url}/")
        if r.status_code == 200:
            return ref_path
    except Exception:
        pass
    return ref_path


# ── ElevenLabs 云端克隆
async def _register_elevenlabs(voice_key: str, label: str, ref_path: str) -> Optional[str]:
    api_key = _config.elevenlabs_api_key()
    if not api_key:
        print("[VoiceClone] 未配置 ELEVENLABS_API_KEY（可在设置页「语音合成 Key」填写）", flush=True)
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            with open(ref_path, "rb") as af:
                r = await c.post(
                    "https://api.elevenlabs.io/v1/voices/add",
                    headers={"xi-api-key": api_key},
                    data={"name": label},
                    files={"files": (os.path.basename(ref_path), af, "audio/mpeg")}
                )
        data = r.json()
        vid  = data.get("voice_id")
        if vid:
            print(f"[VoiceClone] ElevenLabs 克隆成功: {vid}", flush=True)
            return vid
    except Exception as e:
        print(f"[VoiceClone] ElevenLabs 克隆失败: {e}", flush=True)
    return None


# ── CosyVoice3 零样本克隆注册
async def _register_cosyvoice(voice_key: str, ref_path: str) -> Optional[str]:
    """
    CosyVoice3 零样本克隆注册。
    不需要训练，直接存参考音频路径，推理时带入做零样本克隆。
    需要本地跑 CosyVoice3 API（localhost:9881）。
    """
    # ★ 2026-09-12 惰性化：克隆注册是用户主动操作，这里按需拉起 CosyVoice（最多等 40s）；
    #   拉不起也不阻塞——参考音频照常落库，服务上线后自动生效。
    try:
        from . import cosyvoice_mgr
        await cosyvoice_mgr.ensure_async(40)
    except Exception:
        pass
    cosyvoice_url = os.environ.get("COSYVOICE_API", "http://localhost:9881")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{cosyvoice_url}/")
        if r.status_code == 200:
            print(
                f"[VoiceClone] CosyVoice3 在线，注册零样本克隆: {voice_key}",
                flush=True
            )
    except Exception:
        print(
            "[VoiceClone] CosyVoice3 服务不在线，"
            "已存参考音频（上线后自动生效）",
            flush=True
        )
    # CosyVoice3零样本克隆不需要预训练，ref_path即是voice_id
    return ref_path
