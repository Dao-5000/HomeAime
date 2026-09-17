# -*- coding: utf-8 -*-
"""
模板引擎（**已停用预写文案**）：早安/晚安/节日/纪念日文案模板，支持动态变量渲染。

★★ 2026-09-14 用户拍板「发什么话由模型决定，禁止模板」：
   · 这里原本内置 4 类共 30 条预写文案（早安 8 条、晚安 8 条、节日 5 条、纪念日 5 条、
     惊喜 4 条），是"主动消息突然变成客服口吻/每条都一个模子"的来源；
   · 现在 **DEFAULT_TEMPLATES 全部分类为空**，render_template() 保留函数名但返回空串
     （等于"生成不出来就不发"，不会再有任何预写文案被发出去）；
   · 早晚安/节日/纪念日/惊喜的消息改由 scheduler._send_template / _gen_proactive_text
     交给模型按人设+上下文现场生成；
   · 本模块只保留 list/add/delete 供旧接口与管理页读写（当前前端无调用）。
"""
import json
from pathlib import Path

from . import config

TEMPLATE_DIR = config.ROOT_DIR / "模板"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

CATEGORIES = {
    "morning": "早安",
    "night": "晚安",
    "festival": "节日",
    "anniversary": "纪念日",
}

# ★ 预写文案已按用户要求整体删除（原来每个分类都有 5~8 条固定句子）。
#   留空的分类字典而不是删掉常量，是为了让旧调用方/旧接口拿到"空"而不是报错。
DEFAULT_TEMPLATES = {
    "morning": [],
    "night": [],
    "festival": [],
    "anniversary": [],
    "surprise": [],
}


def _file_path(category: str) -> Path:
    return TEMPLATE_DIR / (category + ".json")


def add_template(category: str, content: str) -> bool:
    if category not in CATEGORIES:
        return False
    templates = list_templates(category).get(category, [])
    templates.append(content.strip())
    _file_path(category).write_text(json.dumps(templates, ensure_ascii=False, indent=2), "utf-8")
    return True


def delete_template(category: str, index: int) -> bool:
    if category not in CATEGORIES:
        return False
    templates = list_templates(category).get(category, [])
    if 0 <= index < len(templates):
        templates.pop(index)
        _file_path(category).write_text(json.dumps(templates, ensure_ascii=False, indent=2), "utf-8")
        return True
    return False


def render_template(category: str, variables: dict = None) -> str:
    """【已停用】预写文案渲染 —— 永远返回空串。

    ★ 2026-09-14：不再从模板池里挑句子。主动消息的内容一律由模型生成
      （scheduler._send_template / _gen_proactive_text）。
      保留这个函数名是为了：万一还有旧代码路径按名字调用它，拿到的也是空串
      （调用方按"生成失败 → 本次不发"处理），而不是悄悄发出模板句。
    注意：**用户自己在 模板/*.json 里写的内容也不会被使用**（主动消息已全面改为模型生成）。
    """
    return ""


def list_templates(category: str = None) -> dict:
    """列出模板文件内容（管理接口用；主动消息已不再消费这些内容）。"""
    result = {}
    cats = [category] if category else list(CATEGORIES.keys())
    for cat in cats:
        fp = _file_path(cat)
        if fp.exists():
            try:
                result[cat] = json.loads(fp.read_text("utf-8"))
            except Exception:
                result[cat] = list(DEFAULT_TEMPLATES.get(cat, []))
        else:
            result[cat] = list(DEFAULT_TEMPLATES.get(cat, []))
    return result
