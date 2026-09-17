# -*- coding:utf-8 -*-
"""
模型配置中心：统一管理文本模型和视觉模型的配置。
每个模型包含 name、model、base_url、provider 等信息。
"""

TEXT_MODELS = {

    "deepseek-chat": {
        "name": "DeepSeek V3",
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com"
    },

    "deepseek-reasoner": {
        "name": "DeepSeek R1",
        "model": "deepseek-reasoner",
        "base_url": "https://api.deepseek.com"
    },

    "qwen-plus": {
        "name": "通义千问 Plus",
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    },

    "qwen-max": {
        "name": "通义千问 Max",
        "model": "qwen-max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    },

    "glm-4.5": {
        "name": "GLM 4.5",
        "model": "glm-4.5"
    },

    "kimi-k2": {
        "name": "Kimi K2",
        "model": "kimi-k2"
    }

}


VISION_MODELS = {

    "qwen-vl-max": {
        "name": "通义千问视觉Max",
        "provider": "dashscope",
        "model": "qwen-vl-max"
    },

    "qwen-vl-plus": {
        "name": "通义千问视觉Plus",
        "provider": "dashscope",
        "model": "qwen-vl-plus"
    },

    "qwen2.5-vl-72b": {
        "name": "Qwen2.5-VL-72B",
        "provider": "siliconflow",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct"
    }

}


def get_model(name):
    """根据模型名称获取配置，先查文本模型，再查视觉模型，都没有返回 None"""
    if name in TEXT_MODELS:
        return TEXT_MODELS[name]

    if name in VISION_MODELS:
        return VISION_MODELS[name]

    return None


def get_text_model(name):
    """获取文本模型配置"""
    return TEXT_MODELS.get(name)


def get_vision_model(name):
    """获取视觉模型配置"""
    return VISION_MODELS.get(name)


def list_text_models():
    """返回所有文本模型列表"""
    return [{"key": k, **v} for k, v in TEXT_MODELS.items()]


def list_vision_models():
    """返回所有视觉模型列表"""
    return [{"key": k, **v} for k, v in VISION_MODELS.items()]
