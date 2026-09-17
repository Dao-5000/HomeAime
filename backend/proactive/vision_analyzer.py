# coding=utf-8
"""
视觉理解模块
- 调用云端 Qwen VL（复用 config.py 已有视觉配置）
- 解释截图内容，返回结构化状态描述
"""
import base64
import json
import re
import time
from typing import Optional

import httpx

from .. import config

# 调用间隔保护（秒），避免频繁请求浪费 token
_COOLDOWN = 30
_last_call = 0.0


def analyze_screen(image_path: str, user_name: str = "你", force: bool = False) -> Optional[dict]:
    """
    发送截图到云端视觉模型，返回分析结果。

    Returns:
        {
            "summary": str,        # 一句话描述用户在干啥
            "activity": str,        # activity类型：coding/browsing/video/idle/gaming/other
            "engagement": int,     # 参与度 0-100（高=专注，低=可能摸鱼）
            "interesting": bool,   # 是否值得主动打扰
            "topic_hint": str,    # 可用于主动聊天的切入话题
        }
    """
    global _last_call

    # 频率保护
    if not force and time.time() - _last_call < _COOLDOWN:
        return None
    _last_call = time.time()

    try:
        provider = config.vision_provider()      # ★ 现在会按 VISION_MODEL 自动从模型池解析（glm-5.3-flash → zhipu）
        vmodel_key = config.vision_model()
        api_key = config.vision_key()
        base_url = config.vision_base_url()

        if not api_key:
            print("[VisionAnalyzer] 未配置视觉 API Key，跳过", flush=True)
            return None

        # ★ 解析真实 model 名（下拉 key → 模型池里的真实 API model，
        #   如 Qwen2.5-VL-72B → Qwen/Qwen2.5-VL-72B-Instruct）
        try:
            _vcfg = config.get_vision_model_config(vmodel_key)
            model = str(_vcfg.get("model") or vmodel_key).strip() or vmodel_key
        except Exception:
            model = vmodel_key

        # 读取图片
        from pathlib import Path
        img_bytes = Path(image_path).read_bytes()
        b64_img = base64.b64encode(img_bytes).decode()

        prompt = f"""
你是一个贴心的AI陪伴助手，正在通过屏幕截图了解主人的状态。
请仔细观察截图，用中文回答以下问题：

1. 用户现在在做什么？（一句话具体描述，读出屏幕上的应用名/窗口标题/网页标题等文字）
2. 能识别出哪个应用、网页、视频或游戏吗？（读图上的 logo/标题/文字判断；不能确定就留空，严禁猜测）
3. 用户看起来专注程度如何？（低/中/高）
4. 这个场景有什么值得关心的地方？
5. 如果要自然陪伴，可以从什么具体话题切入？（尽量具体，比如提到了什么内容/在写什么/在看什么）

请以JSON格式返回：
{{"summary":"用户正在XXX","scene_name":"应用/网页/游戏名或空字符串","focus":"高/中/低","interesting_point":"XXX","topic_hint":"XXX"}}
只返回JSON，不要其他文字。
"""

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        image_content = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}}
        text_content = {"type": "text", "text": prompt}
        messages = [{"role": "user", "content": [image_content, text_content]}]

        def _full_url(base: str, default: str) -> str:
            base = (base or default).rstrip("/")
            return base if base.endswith("/chat/completions") else base + "/chat/completions"

        if provider == "dashscope":
            payload = {"model": model, "messages": messages, "max_tokens": 500}
            url = _full_url(base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")

        elif provider == "siliconflow":
            payload = {"model": model, "messages": messages, "max_tokens": 500}
            url = _full_url(base_url, "https://api.siliconflow.cn/v1/chat/completions")

        elif provider == "zhipu":
            payload = {"model": model, "messages": messages, "max_tokens": 500}
            url = _full_url(base_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")

        elif provider == "deepseek":
            payload = {"model": model, "messages": messages, "max_tokens": 500}
            url = _full_url(base_url, "https://api.deepseek.com/v1/chat/completions")

        else:
            print(f"[VisionAnalyzer] 不支持的视觉提供商: {provider}", flush=True)
            return None

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        # 提取 JSON
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            result = json.loads(m.group())
        else:
            result = {"summary": content[:100], "focus": "中", "interesting_point": "", "topic_hint": ""}
        # 防嵌套：有的模型把整个 JSON 又塞进 summary 字段，展平一层
        try:
            s = str(result.get("summary") or "")
            if s.strip().startswith("{"):
                inner = json.loads(s)
                if isinstance(inner, dict):
                    result.update(inner)
        except Exception:
            pass

        # 映射参与度
        focus_map = {"高": 85, "中": 50, "低": 20}
        engagement = focus_map.get(result.get("focus", "中"), 50)

        # 判断是否值得打扰（低参与度或有趣场景）
        interesting = result.get("interesting_point", "") != "" or engagement < 70

        return {
            "summary": result.get("summary", "未知"),
            "activity": _infer_activity(result.get("summary", "")),
            "scene_name": str(result.get("scene_name", "") or "").strip(),
            "engagement": engagement,
            "interesting": interesting,
            "topic_hint": result.get("topic_hint", ""),
        }

    except httpx.HTTPStatusError as e:
        print(f"[VisionAnalyzer] HTTP错误: {e.response.status_code} {e.response.text[:200]}", flush=True)
        return None
    except Exception as e:
        print(f"[VisionAnalyzer] 分析失败: {e}", flush=True)
        return None


def _infer_activity(summary: str) -> str:
    """从描述推断活动类型"""
    summary = summary.lower()
    if any(k in summary for k in ["代码", "ide", "vscode", "pycharm", "terminal", "编程"]):
        return "coding"
    if any(k in summary for k in ["视频", "youtube", "bilibili", "播放器", "直播"]):
        return "video"
    if any(k in summary for k in ["游戏", "游戏界面", "steam", "原神", "星露谷", "我的世界",
                                   "王者荣耀", "英雄联盟", "崩坏", "绝地求生", "cs2", "apex"]):
        return "gaming"
    if any(k in summary for k in ["浏览器", "网页", "chrome", "浏览器"]):
        return "browsing"
    if any(k in summary for k in ["待机", "桌面", "锁屏", "黑屏", "未操作"]):
        return "idle"
    return "other"
