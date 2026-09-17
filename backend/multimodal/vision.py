# -*- coding:utf-8 -*-
"""
Vision Analyzer v2.0
图片理解分析器（空壳变真肉）：

  调用视觉模型分析图片，提取场景、物体、情绪等信息。
  支持本地文件路径 / base64 data URL / http URL。
  超大图自动压缩到1024px以内，避免打爆token。
"""
import os
import base64
from typing import Optional

# ── 图片尺寸上限（超出压缩，避免打爆token）
MAX_IMAGE_DIMENSION = 1024   # px
MAX_BASE64_BYTES    = 1024 * 1024 * 3   # 3MB base64上限


def _resize_image_if_needed(image_path: str) -> str:
    """
    图片尺寸超限时压缩到MAX_IMAGE_DIMENSION以内
    返回处理后的base64 data URL
    优先用Pillow，没有则原图返回
    """
    try:
        from PIL import Image
        import io

        img = Image.open(image_path)
        w, h = img.size

        if max(w, h) > MAX_IMAGE_DIMENSION:
            ratio = MAX_IMAGE_DIMENSION / max(w, h)
            new_w = int(w * ratio)
            new_h = int(h * ratio)
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # 转JPEG压缩（PNG转JPEG体积通常缩减60%）
        buf = io.BytesIO()
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return f"data:image/jpeg;base64,{b64}"

    except ImportError:
        # 没有Pillow，原图base64
        with open(image_path, "rb") as f:
            raw = f.read()
        ext = os.path.splitext(image_path)[1].lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                "png": "image/png", "gif": "image/gif",
                "webp": "image/webp"}.get(ext, "image/jpeg")
        b64 = base64.b64encode(raw).decode("utf-8")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"[Vision] 图片预处理失败: {e}", flush=True)
        return None


def _normalize_image(image) -> Optional[str]:
    """
    统一图片输入为URL字符串
    支持：本地文件路径 / base64 data URL / http URL
    """
    if not image:
        return None

    s = str(image).strip()

    # 已经是 data URL
    if s.startswith("data:image"):
        # 检查base64体积
        b64_part = s.split(",", 1)[-1] if "," in s else ""
        if len(b64_part) > MAX_BASE64_BYTES:
            # 超限：写临时文件再压缩
            try:
                import tempfile
                raw = base64.b64decode(b64_part)
                suffix = ".jpg"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                result = _resize_image_if_needed(tmp_path)
                os.unlink(tmp_path)
                return result
            except Exception:
                pass
        return s

    # 本地文件路径
    if os.path.exists(s):
        return _resize_image_if_needed(s)

    # http URL（直接用，让视觉API自己取）
    if s.startswith("http://") or s.startswith("https://"):
        return s

    return None


async def analyze_image_with_vision_model(
    image,
    prompt: str = None,
    api_key: str = None,
    base_url: str = None,
    model: str = None
) -> str:
    """
    调用视觉模型分析图片，返回描述文本
    优先用传入的参数，降级用config配置
    """
    if not image:
        return ""

    img_url = _normalize_image(image)
    if not img_url:
        return ""

    # 读配置
    try:
        from .. import config as _cfg
        _key      = api_key      or _cfg.vision_key()
        _base_url = base_url     or _cfg.vision_base_url()
        _model    = model        or _cfg.vision_model()
    except Exception:
        return ""

    if not _key:
        return ""

    _prompt = prompt or (
        "请用中文简洁描述这张图片的内容，包括：\n"
        "1. 场景/环境（室内/室外/工作/生活等）\n"
        "2. 主要内容或人物状态\n"
        "3. 图片传达的情绪或氛围\n"
        "4. 如果有文字，简要说明文字内容\n"
        "控制在100字以内，自然描述，不要分点。"
    )

    try:
        import httpx

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text",      "text": _prompt},
                    {"type": "image_url", "image_url": {"url": img_url}}
                ]
            }
        ]

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {_key}",
                    "Content-Type":  "application/json"
                },
                json={
                    "model":       _model,
                    "messages":    messages,
                    "max_tokens":  256,
                    "temperature": 0.1,
                }
            )
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

    except Exception as e:
        print(f"[Vision] 视觉模型调用失败: {e}", flush=True)
        return ""


async def analyze_image(image) -> dict:
    """
    图片综合分析入口（供MultimodalManager.process_async()调用）
    返回结构和原来保持兼容，但description不再是空字符串
    """
    result = {
        "scene":       "unknown",
        "objects":     [],
        "emotion_hint":"neutral",
        "has_people":  False,
        "has_text":    False,
        "brightness":  "unknown",
        "description": "",
        "analyzed":    False,
    }

    if not image:
        return result

    img_url = _normalize_image(image)
    if not img_url:
        return result

    # 调视觉模型获取描述
    desc = await analyze_image_with_vision_model(image)
    if not desc:
        return result

    result["description"] = desc
    result["analyzed"]    = True

    # 从描述文本推断结构化字段（简单关键词，不再另调API）
    # 场景推断
    scene_map = {
        "办公": "office", "工作": "office", "电脑": "office", "桌子": "office",
        "家": "home", "客厅": "home", "卧室": "home", "沙发": "home",
        "餐厅": "restaurant", "吃饭": "restaurant", "食物": "restaurant", "菜": "restaurant",
        "户外": "outdoor", "公园": "outdoor", "街道": "outdoor", "天空": "outdoor",
        "学校": "school", "课": "school", "书": "school",
    }
    for kw, scene in scene_map.items():
        if kw in desc:
            result["scene"] = scene
            break

    # 人物推断
    result["has_people"] = any(k in desc for k in ["人", "脸", "表情", "他", "她", "男", "女"])

    # 文字推断
    result["has_text"] = any(k in desc for k in ["文字", "字", "标语", "写着", "显示"])

    # 情绪推断
    positive_words = ["开心", "笑", "阳光", "温暖", "愉快", "明亮", "活力"]
    negative_words = ["难过", "哭", "暗", "压抑", "疲惫", "沉重", "焦虑"]
    pos_count = sum(1 for w in positive_words if w in desc)
    neg_count = sum(1 for w in negative_words if w in desc)
    if pos_count > neg_count:
        result["emotion_hint"] = "positive"
    elif neg_count > pos_count:
        result["emotion_hint"] = "negative"

    return result


def detect_scene(image):
    """检测图片场景（兼容旧接口，返回unknown）"""
    return "unknown"


def detect_objects(image):
    """检测图片中的物体（兼容旧接口，返回空列表）"""
    return []


def detect_face_emotion(image):
    """检测人脸情绪（兼容旧接口）"""
    return {"has_face": False, "emotion": "unknown", "confidence": 0.0}


def extract_image_text(image):
    """提取图片中的文字（兼容旧接口，返回空字符串）"""
    return ""
