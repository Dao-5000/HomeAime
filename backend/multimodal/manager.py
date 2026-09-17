# -*- coding:utf-8 -*-
"""
Multimodal Manager v2.0
多模态统一管理器：

  负责整合文本、语音、图片、环境等多维度感知信息，
  生成统一的 MultimodalContext 供 CompanionOS 使用。
  v2.0：图片分析真正接入视觉模型，图片描述角色视角渲染。
"""
from .text import analyze_text
from .voice import analyze_voice
from .vision import analyze_image
from .environment import get_environment
from backend.loop_compat import get_loop


class MultimodalContext:
    """多模态上下文数据结构"""

    def __init__(self):
        self.data = {
            "text": {},
            "voice": {},
            "vision": {},
            "environment": {},
        }

    def update(self, key, value):
        """更新某个维度的感知数据"""
        self.data[key] = value

    def get(self, key, default=None):
        """获取某个维度的感知数据"""
        return self.data.get(key, default)

    def export(self):
        """导出完整的多模态上下文"""
        return self.data

    def get_emotion_hint(self):
        """综合各维度获取情绪提示"""
        hints = []

        # 文本情绪
        text_data = self.data.get("text", {})
        if text_data.get("emotion_hint"):
            hints.append(text_data["emotion_hint"])

        # 语音情绪
        voice_data = self.data.get("voice", {})
        if voice_data.get("emotion"):
            hints.append(voice_data["emotion"])

        # 图片情绪
        vision_data = self.data.get("vision", {})
        if vision_data.get("emotion_hint"):
            hints.append(vision_data["emotion_hint"])

        # 环境影响
        env_data = self.data.get("environment", {})
        if env_data.get("period") == "night":
            hints.append("可能疲惫")

        if not hints:
            return "neutral"

        # 简单投票：取出现最多的情绪
        from collections import Counter
        counter = Counter(hints)
        return counter.most_common(1)[0][0]

    def get_summary(self):
        """获取多模态感知摘要（用于 Prompt）"""
        parts = []

        # 环境
        env = self.data.get("environment", {})
        if env:
            time_str = env.get("time", "")
            period = env.get("period", "")
            if time_str:
                parts.append(f"当前时间：{time_str}（{period}）")

        # 文本情绪
        text = self.data.get("text", {})
        if text.get("emotion_hint"):
            emotion_map = {
                "positive": "用户情绪积极",
                "negative": "用户情绪低落",
                "neutral": "用户情绪平稳",
            }
            parts.append(emotion_map.get(text["emotion_hint"], ""))

        # 语音
        voice = self.data.get("voice", {})
        if voice.get("emotion"):
            parts.append(f"语音情绪：{voice['emotion']}")
            if voice.get("speech_rate"):
                parts.append(f"语速：{voice['speech_rate']}")

        # 图片
        vision = self.data.get("vision", {})
        if vision.get("scene"):
            parts.append(f"场景：{vision['scene']}")
        if vision.get("objects"):
            parts.append(f"物体：{', '.join(vision['objects'][:3])}")

        return "\n".join([p for p in parts if p])


class MultimodalManager:
    """多模态管理器"""

    def __init__(self):
        pass

    def process(
        self,
        message=None,
        audio_path=None,
        image=None,
        include_environment=True,
    ):
        """
        同步版process（兼容旧调用）。
        注意：image 分析是 async，同步版里用 asyncio 跑，可能有线程问题。
        推荐在 FastAPI 里用 process_async()。
        """
        context = MultimodalContext()

        # 文本分析
        if message:
            try:
                text_result = analyze_text(message)
                context.update("text", text_result)
            except Exception as e:
                print(f"[Multimodal] 文本分析失败: {e}", flush=True)

        # 语音分析
        if audio_path:
            try:
                voice_result = analyze_voice(audio_path)
                context.update("voice", voice_result)
            except Exception as e:
                print(f"[Multimodal] 语音分析失败: {e}", flush=True)

        # 图片分析（async，同步版用asyncio跑）
        if image:
            try:
                import asyncio as _asyncio
                try:
                    loop = get_loop()
                    if loop.is_running():
                        import concurrent.futures
                        future = _asyncio.run_coroutine_threadsafe(
                            analyze_image(image), loop
                        )
                        vision_result = future.result(timeout=10)
                    else:
                        vision_result = loop.run_until_complete(analyze_image(image))
                except RuntimeError:
                    vision_result = _asyncio.run(analyze_image(image))
                context.update("vision", vision_result)
            except Exception as e:
                print(f"[MultimodalManager] vision分析失败: {e}", flush=True)
                context.update("vision", {"description": "", "analyzed": False})

        # 环境信息
        if include_environment:
            try:
                env_result = get_environment()
                context.update("environment", env_result)
            except Exception as e:
                print(f"[Multimodal] 环境获取失败: {e}", flush=True)

        return context

    async def process_async(
        self,
        message=None,
        audio_path=None,
        image=None,
        include_environment=True
    ):
        """
        异步版process（推荐在FastAPI里用这个，避免线程问题）
        """
        context = MultimodalContext()

        # 文本分析
        if message:
            try:
                text_result = analyze_text(message)
                context.update("text", text_result)
            except Exception as e:
                print(f"[Multimodal] 文本分析失败: {e}", flush=True)

        # 语音分析
        if audio_path:
            try:
                voice_result = analyze_voice(audio_path)
                context.update("voice", voice_result)
            except Exception as e:
                print(f"[Multimodal] 语音分析失败: {e}", flush=True)

        # 图片分析（async，直接await）
        if image:
            try:
                vision_result = await analyze_image(image)
                context.update("vision", vision_result)
            except Exception as e:
                print(f"[MultimodalManager] vision分析失败: {e}", flush=True)
                context.update("vision", {"description": "", "analyzed": False})

        # 环境信息
        if include_environment:
            try:
                env_result = get_environment()
                context.update("environment", env_result)
            except Exception as e:
                print(f"[Multimodal] 环境获取失败: {e}", flush=True)

        return context

    def build_prompt(self, context: MultimodalContext) -> str:
        """
        升级版build_prompt：图片描述单独渲染，不和其他感知混在一起
        图片描述用角色视角自然语言，让LLM像"看到了"一样感知
        """
        parts = []

        # ── 图片描述（优先级最高，单独成块）
        vision = context.get("vision", {})
        if vision.get("analyzed") and vision.get("description"):
            desc  = vision["description"]
            scene = vision.get("scene", "unknown")
            has_p = vision.get("has_people", False)
            emo   = vision.get("emotion_hint", "neutral")

            # 自然语言描述（角色视角）
            _img_lines = [f"【TA刚发来一张图片】\n图片内容：{desc}"]

            # 补充结构化信息（LLM参考用）
            _hints = []
            if scene != "unknown":
                _scene_cn = {
                    "office": "工作/办公场景",
                    "home":   "居家场景",
                    "restaurant": "餐饮/美食场景",
                    "outdoor": "户外场景",
                    "school": "学校/学习场景",
                }.get(scene, scene)
                _hints.append(f"场景：{_scene_cn}")
            if has_p:
                _hints.append("图中有人")
            if emo == "positive":
                _hints.append("整体氛围积极")
            elif emo == "negative":
                _hints.append("整体氛围低落")

            if _hints:
                _img_lines.append("（" + "，".join(_hints) + "）")

            _img_lines.append(
                "请自然地回应这张图片，可以评价/好奇/共情，"
                "不要提「我看到图片」「根据图片」等机械表述。"
            )
            parts.append("\n".join(_img_lines))

        # ── 其他感知信息（原有逻辑，去掉vision部分）
        summary = context.get_summary()
        if summary:
            # get_summary()里vision那行单独过滤掉（已单独处理）
            summary_lines = [
                line for line in summary.split("\n")
                if not line.startswith("视觉") and not line.startswith("图片") and not line.startswith("场景") and not line.startswith("物体")
            ]
            summary_clean = "\n".join(summary_lines).strip()
            if summary_clean:
                parts.append(
                    "【感知信息】\n以下是当前感知到的环境和用户状态，"
                    "自然理解和运用，不要向用户提到感知分析。\n"
                    + summary_clean
                )

        return "\n\n".join(parts) if parts else ""
