# -*- coding: utf-8 -*-
"""
表情包素材库：本地图片管理，AI 可在回复中引用 [sticker:文件名] 标记。
前端解析标记渲染为图片。
"""
import json
import random
import re
import shutil
from pathlib import Path
from . import config

STICKER_DIR = config.DATA_DIR / "表情包"
_RESOURCE_STICKER_DIR = config.ROOT_DIR / "表情包"
try:
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Keep bundled stickers readable when the installation is read-only.
    STICKER_DIR = _RESOURCE_STICKER_DIR

# 允许的图片格式
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 表情包元数据文件（记录标签、描述）
META_FILE = STICKER_DIR / "_meta.json"

# ★ 黄脸表情包（内置第一批卡通黄脸系）——2026-09-13 用户要求禁用：
#   AI 不再发这些图（prompt 不列、解析标记时忽略、自动选图跳过），
#   只发猫耳少女/实拍系和用户后来上传的表情包。
#   先"禁用"而非删除：素材还在、管理页还看得到，想恢复从名单里去掉即可。
YELLOW_FACE_STICKERS = frozenset({
    "happy_laugh.png", "smile.png", "giggle.png", "shy.png", "love.png",
    "miss_you.png", "hug.png", "kiss.png", "surprised.png", "confused.png",
    "sad_cry.png", "wronged.png", "angry.png", "speechless.png", "proud.png",
    "cheer.png", "ok.png", "goodnight.png", "act_cute.png", "awkward.png",
    "scared.png", "cool.png", "thinking.png", "thanks.png", "sorry.png",
})


def is_disabled(filename: str) -> bool:
    return str(filename or "") in YELLOW_FACE_STICKERS


def _load_meta() -> dict:
    """合并内置素材目录与用户数据目录的 _meta.json。

    内置那套表情包自带语义标签（tag/desc），写死在内置目录里随程序分发；
    用户自己上传的表情包标签写在用户数据目录。
    两个来源都要生效，后读的用户目录优先（用户改了标签以用户为准）。
    """
    out = {}
    for base in (_RESOURCE_STICKER_DIR, STICKER_DIR):
        mf = base / "_meta.json"
        if not mf.exists():
            continue
        try:
            data = json.loads(mf.read_text("utf-8"))
            if isinstance(data, dict):
                out.update(data)
        except Exception:
            continue
    return out


def _save_meta(data: dict):
    META_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def list_stickers() -> list:
    """列出所有表情包，含元数据"""
    meta = _load_meta()
    result = []
    merged = {}
    for base in (_RESOURCE_STICKER_DIR, STICKER_DIR):
        if base.exists():
            for item in base.iterdir():
                merged[item.name] = item
    for f in sorted(merged.values()):
        if f.is_file() and f.suffix.lower() in ALLOWED_EXT and not f.name.startswith("_"):
            info = meta.get(f.name, {})
            tag = str(info.get("tag", "") or "").strip()
            desc = str(info.get("desc", "") or "").strip()
            # 没有人工标签时用文件名兜底，至少让模型知道这是哪张素材；
            # 中文/英文语义文件名会直接成为可用含义，纯数字文件名则标为普通表情包。
            fallback = f.stem.replace("_", " ").replace("-", " ").strip()
            if not tag and fallback and not fallback.isdigit():
                tag = fallback
            result.append({
                "filename": f.name,
                "url": "/stickers/" + f.name,
                "size": f.stat().st_size,
                "tag": tag,
                "desc": desc,
                "meaning": desc or tag or "表达当下情绪、用于接梗",
                "disabled": f.name in YELLOW_FACE_STICKERS,
            })
    return result


def ai_usable_stickers() -> list:
    """AI 能用的表情包 = 全部素材去掉禁用的黄脸系。"""
    return [s for s in list_stickers() if not s.get("disabled")]


def save_sticker(source_path: str, filename: str, tag: str = "", desc: str = "") -> dict:
    """保存上传的表情包"""
    filename = filename.strip()
    if not filename:
        raise ValueError("文件名不能为空")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("不支持的格式：" + ext)
    dest = STICKER_DIR / filename
    shutil.copy2(source_path, str(dest))
    meta = _load_meta()
    meta[filename] = {"tag": tag, "desc": desc}
    _save_meta(meta)
    return {"filename": filename, "url": "/stickers/" + filename}


def delete_sticker(filename: str) -> bool:
    """删除表情包"""
    f = STICKER_DIR / filename
    if f.exists() and f.suffix.lower() in ALLOWED_EXT:
        f.unlink()
        meta = _load_meta()
        if filename in meta:
            del meta[filename]
            _save_meta(meta)
        return True
    return False


def parse_sticker_markers(text: str) -> list:
    """从 AI 回复中解析 [sticker:文件名] 标记，返回 [{filename, url}]。

    ★ 兜底：模型常写中文语义（[sticker:开心]）而非文件名，这里用 resolve_sticker
      把中文情绪词解析成真实文件名（happy_laugh.png），否则"开心"不是文件、永远发不出图。

    ★ 2026-09-12 修：原来正则只认 ASCII 冒号 `[sticker:`，模型写全角冒号
      `[sticker：委屈]` 时**既解析不出也剥不掉**，于是整段标记漏成文字发到 QQ
      （实测 NapCat 日志里 23 条真实发送带着字面 "[sticker:傲娇]"）。
      现在半角/全角冒号、冒号前后空格都认。
    """
    import re
    pattern = re.compile(r"\[sticker\s*[:：]\s*([^\]]+)\]", re.I)
    result = []
    for m in pattern.finditer(text):
        fname = m.group(1).strip()
        if not fname:
            continue
        real = resolve_sticker(fname)
        if real:
            result.append({"filename": real, "url": "/stickers/" + real})
    return result


def strip_sticker_markers(text: str) -> str:
    """移除回复中的表情包标记（纯文本部分）。

    ★ 2026-09-12：与 parse 同步支持全角冒号；并且结尾用 `*` 而不是 `+`，
      这样模型写出残缺的 `[sticker：]`（没写名字）也能被清掉，不会漏成文字。
    """
    import re
    return re.sub(r"\[sticker\s*[:：][^\]]*\]", "", text, flags=re.I).strip()


def get_sticker(filename: str) -> dict:
    """安全取得单张表情包及其交流语义，不允许路径穿越。"""
    safe_name = Path(str(filename or "")).name
    for item in list_stickers():
        if item["filename"] == safe_name:
            return item
    return {}


def resolve_sticker(name: str) -> str:
    """把模型写的 [sticker:xxx] 里的 xxx 解析成真实文件名。

    模型经常写中文语义（如 [sticker:开心]、[sticker:猫猫]）而不是文件名，
    直接按文件名找不到就发不出图。这里逐级兜底：
      0) 叠词去重（"猫猫"→"猫"）
      1) 文件名精确匹配（含去掉扩展名）
      2) tag 精确匹配
      3) tag 包含匹配（"开心" ⊂ "开心大笑"）
      4) desc 包含匹配
    返回真实文件名；匹配不到返回空串。
    ★ 只在「AI 可用」的素材里解析：禁用的黄脸系解析不到，标记会被剥掉、图发不出去。
    """
    name = str(name or "").strip()
    if not name:
        return ""
    stickers = ai_usable_stickers()
    # 0. 候选名：原名 + 叠词去重（"猫猫"→"猫"）
    candidates = [name]
    if len(name) == 2 and name[0] == name[1]:
        candidates.append(name[0])
    for cand in candidates:
        # 1. 文件名精确匹配
        for item in stickers:
            if item["filename"] == cand:
                return item["filename"]
        stem = cand.rsplit(".", 1)[0].lower()
        for item in stickers:
            if item["filename"].lower().startswith(stem):
                return item["filename"]
        # 2. tag 精确匹配
        for item in stickers:
            if str(item.get("tag") or "").strip() == cand:
                return item["filename"]
        # 3. tag 包含匹配
        for item in stickers:
            if cand in str(item.get("tag") or ""):
                return item["filename"]
        # 4. desc 包含匹配
        for item in stickers:
            if cand in str(item.get("desc") or ""):
                return item["filename"]
    return ""


def describe_user_markers(text: str) -> list:
    """把用户发来的标记转成模型可理解的“接梗”语义。"""
    out = []
    for marker in parse_sticker_markers(text):
        item = get_sticker(marker.get("filename", ""))
        if item:
            out.append({
                "filename": item["filename"],
                "tag": item.get("tag", ""),
                "desc": item.get("desc", ""),
                "meaning": item.get("meaning", "表达当下情绪、用于接梗"),
            })
    return out


def pick_for_context(text: str = "", emotion: str = "", exclude: str = "") -> dict:
    """根据对话/情绪挑一张合适的表情包；无标签时仍可从素材库自然随机。"""
    stickers = [
        x for x in list_stickers()
        if x.get("filename") != exclude and not x.get("disabled")
    ]
    if not stickers:
        return {}
    value = (str(text or "") + " " + str(emotion or "")).lower()

    # 组按「具体场景 -> 通用情绪」排列，先命中先返回。
    # 第二个串是「按代表性排序」的情绪关键词：越靠前权重越大。
    # 旧版是等权计数，结果"抱抱"因为描述里同时有「安慰」「抱抱」两个词，
    # 反而盖过了只命中「难过」一个词的 sad_cry —— 难过时发的却是抱抱图。
    groups = [
        ("晚安 睡 困 累 休息 sleepy tired",
         "晚安 困 睡 zzz 打盹"),
        ("谢谢 感谢 多谢 thanks",
         "谢谢 感谢 感激"),
        ("对不起 抱歉 错了 原谅 sorry",
         "对不起 抱歉 认错 低头"),
        ("加油 鼓励 打气 你可以",
         "加油 鼓励 打气"),
        ("撒娇 求抱 要抱 抱抱",
         "撒娇 求抱"),
        ("害怕 吓 恐怖 scared afraid",
         "害怕 吓 发抖"),
        ("害羞 脸红 shy",
         "害羞 脸红"),
        ("酷 帅 厉害 墨镜 cool",
         "酷 墨镜 帅"),
        # 注意：clue 用"好的"而不是"好"——"好"太常见，
        # 「今天好难过」里的"好"会把这条误触发。
        ("好的 没问题 听你的 答应 可以 ok",
         "好的 答应 ok"),
        ("喜欢 想你 爱你 心动 loving tender 亲",
         "喜欢 心动 想你 爱 亲 抱"),
        ("难过 委屈 哭 伤心 难受 sad upset worried",
         "哭 难过 伤心 委屈 心疼 安慰 抱抱"),
        ("生气 无语 过分 讨厌 哼 angry cold",
         "生气 无语 白眼 嫌弃 哼"),
        ("疑问 什么 为什么 懵 不懂 curious 惊讶 震惊",
         "疑问 问号 懵 不懂 震惊 惊讶"),
        ("开心 好笑 笑 哈哈 嘻嘻 太逗 乐 happy playful excited",
         "笑 哈哈 开心 大笑 偷笑 得意 快乐 好笑 耶"),
    ]

    for clues, kw_line in groups:
        if not any(w in value for w in clues.split()):
            continue
        keywords = kw_line.split()
        total = len(keywords)
        scored = []
        for item in stickers:
            semantic = " ".join(
                str(item.get(k, ""))
                for k in ("tag", "desc", "meaning", "filename")
            ).lower()
            score = 0
            for i, kw in enumerate(keywords):
                if kw.lower() in semantic:
                    score += (total - i)      # 越靠前权重越大
            if score:
                scored.append((score, random.random(), item))
        if scored:
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return scored[0][2]

    # 没有明显情绪时退回中性表情。旧版这里直接 random.choice 全库，
    # 会随机发出「大哭」「生气」这种完全不合时宜的图。
    neutral = [
        s for s in stickers
        if any(k in (str(s.get("filename", "")) + str(s.get("tag", ""))).lower()
               for k in ("smile", "ok", "thinking", "微笑", "好的"))
    ]
    if neutral:
        return random.choice(neutral)
    return random.choice(stickers)
