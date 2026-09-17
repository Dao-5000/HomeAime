# -*- coding: utf-8 -*-
"""
歌词获取（结合现有架构）

- 优先：LLM 召回热门歌词（deepseek_api.chat_once，大多数热门歌 LLM 记得）
- kv 缓存 7 天（复用现有 SQLite kv，不用磁盘文件）
- 无歌名 → LLM 随机挑一首
"""
from .. import db, config


def _cache_key(song):
    return f"lyrics:{song}"


async def _llm_call(prompt, max_tokens=900):
    from ..deepseek_api import chat_once
    key = config.memory_key()
    if not key:
        return None
    try:
        return await chat_once(
            config.get("MEMORY_EXTRACT_MODEL", "deepseek-chat"),
            [{"role": "user", "content": prompt}],
            key, temperature=0.4, max_tokens=max_tokens,
        )
    except Exception:
        return None


def _parse_lyrics_response(raw):
    """解析 LLM 输出：去说明文字，每行一句歌词。"""
    text = str(raw or "").strip()
    if not text or "NOT_FOUND" in text or "不知道" in text:
        return []
    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        # 去掉 "歌词："/"歌名："/序号/书名号包裹等
        if not ln:
            continue
        ln = re_sub(ln)
        if len(ln) >= 2:
            lines.append(ln)
    return lines[:40]


def re_sub(ln):
    import re
    ln = re.sub(r"^(歌词|歌词内容|歌名|歌手)[:：]\s*", "", ln)
    ln = re.sub(r"^[0-9]+[\.、]\s*", "", ln)
    ln = re.sub(r"^[《〈「『]", "", ln)
    ln = re.sub(r"[》〉」』]$", "", ln)
    ln = re.sub(r"^.*?(?=歌词|歌词内容)[:：]?\s*", "", ln)
    return ln.strip()


async def _fetch_netease_lyrics(song_name="", artist=""):
    """方案C：LLM 记不全歌词时，用网易云公开接口拉真实歌词。"""
    import re as _re
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            q = f"{song_name} {artist}".strip() if artist else song_name
            r = await client.get(
                "https://music.163.com/api/search/get/web",
                params={"s": q, "type": 1, "limit": 1},
                headers={"Referer": "https://music.163.com"},
            )
            data = r.json()
            songs = (data.get("result") or {}).get("songs") or []
            if not songs:
                return None
            song_id = songs[0].get("id")
            song_title = songs[0].get("name") or song_name
            r2 = await client.get(
                "https://music.163.com/api/song/lyric",
                params={"id": song_id, "lv": 1, "kv": 1, "tv": -1},
                headers={"Referer": "https://music.163.com"},
            )
            lrc_data = r2.json()
            lrc = ((lrc_data.get("lrc") or {}).get("lyric") or "").strip()
            if not lrc:
                return None
            _meta_prefix = ("中文填词", "中文作词", "作词", "作曲", "编曲", "制作人", "混音", "吉他", "贝斯", "鼓", "键盘", "和声", "弦乐", "录音", "母带", "监制", "出品", "策划", "演唱", "采样", "填词", "原曲", "翻译", "歌手", "专辑", "纯音乐")
            _meta_contains = ("版权", "授权", "本作品", "原属", "纯音乐")
            lines = []
            for ln in lrc.split("\n"):
                m = _re.match(r"\[\d+:\d+(?:\.\d+)?\](.*)", ln.strip())
                if m:
                    txt = m.group(1).strip()
                    if not txt:
                        continue
                    if txt.startswith(_meta_prefix):
                        continue
                    if any(k in txt for k in _meta_contains):
                        continue
                    lines.append(txt)
            if len(lines) < 4:
                return None
            return {"song_name": song_title, "artist": artist, "lines": lines[:40], "source": "netease"}
    except Exception:
        return None


# ★ 预置歌词（用户指定要唱的歌，直接写死，不依赖网络/LLM，保证能唱完整）
_PRESET_LYRICS = {
    "把回忆拼好给你": {
        "song_name": "把回忆拼好给你",
        "artist": "王贰浪",
        "lines": [
            "我们之间的回忆", "全部都小心地收集", "我总是偷偷地哭泣", "像倦鸟失了归期",
            "但愿我相信的爱情", "结局紧握在我手心", "时光匆匆却没有遗失过去",
            "希望我们 有光明的未来", "还有能够装下星空的期待", "可现实为何让我感到如此懈怠",
            "总怀念相遇时我们无视落叶和人海", "是你让我勇敢不再像颗尘埃",
            "是你常帮我照料装着梦的盆栽", "每一天我们都是如此愉快",
            "一直到天色渐晚看着落日无奈离开", "我知道你爱集邮爱笑甚至爱发呆",
            "我知道你怕草虫还有夜晚的妖怪", "我喜欢你有一点心不在焉的状态",
            "看起来像个回到七岁时候的小孩", "该如何将我这份感情向你告白",
            "喜欢却又不敢爱", "直到整个宇宙", "在为我焦虑失神慌张之中醒来",
            "就像是黑暗过后黎明盛开", "我们之间的回忆", "全部都小心地收集",
            "我总是偷偷地哭泣", "像倦鸟失了归期", "但愿我相信的爱情",
            "结局紧握在我手心", "时光匆匆却没有遗失过去",
            "那天你在雨后街角答应接受我的爱", "那一刻我的世界有了色彩",
            "这一生无法忘记关于澄蓝色的你", "像一份礼物悄然呈现在我的境遇",
            "我们从清晨起玩一整天游戏", "到夜晚一起看我最爱的剧",
            "能够拥有这些已足够幸运", "我已经不再期待其他什么东西",
            "我们也经常争执互相不接电话",
        ],
    },
}


async def fetch_lyrics(song_name="", artist="", style="") -> dict:
    """
    获取歌词，返回 {"song_name","artist","lines":[...], "source":...} 或 None。
    """
    song = (song_name or "").strip()
    if not song:
        return await _pick_random_song(style)

    # ★ 预置歌词优先：命中直接返回（保证能唱完整，不依赖网络）
    if song in _PRESET_LYRICS:
        data = dict(_PRESET_LYRICS[song])
        data["source"] = "preset"
        return data

    # kv 缓存 7 天
    try:
        from .. import db as _db
        cached = _db.kv_get(_cache_key(song))
        if cached:
            import json
            data = json.loads(cached)
            if isinstance(data, dict) and data.get("lines"):
                data["source"] = "cache"
                return data
    except Exception:
        pass

    # LLM 召回
    artist_part = f"（{artist}）" if artist else ""
    style_part = f"，风格要{style}" if style else ""
    prompt = (
        f"请写出歌曲《{song}》{artist_part}的完整歌词。\n\n"
        f"要求：\n"
        f"- 只输出歌词正文，不要歌名、歌手名、时间戳、段落标记\n"
        f"- 每行一句歌词\n"
        f"- 按照歌曲原本的顺序写，副歌可以重复\n"
        f"- 如果记得不完整，写出你确定记得的部分（至少8行）\n"
        f"- 如果完全不知道这首歌，只回复 NOT_FOUND\n\n"
        f"直接输出歌词："
    )
    raw = await _llm_call(prompt)
    lines = _parse_lyrics_response(raw)
    if len(lines) < 4:
        # ★ 方案C：LLM 记不全 → 网易云真实歌词
        real = await _fetch_netease_lyrics(song, artist)
        if real:
            return real
        return None

    result = {
        "song_name": song,
        "artist": artist,
        "lines": lines,
        "source": "llm",
    }
    try:
        import json
        db.kv_set(_cache_key(song), json.dumps({"song_name": song, "artist": artist, "lines": lines}, ensure_ascii=False))
    except Exception:
        pass
    return result


async def _pick_random_song(style="") -> dict:
    """没指定歌名：LLM 随机挑一首（配合风格）。"""
    style_part = f"，{style}风格的" if style else ""
    prompt = (
        f"随机选一首你最喜欢的中文{style_part}热门歌曲，给出歌名和歌词。\n\n"
        f"输出格式：\n"
        f"歌名：xxx\n"
        f"歌手：xxx\n"
        f"歌词：\n"
        f"（每行一句，副歌可重复，至少10句）"
    )
    raw = await _llm_call(prompt, max_tokens=900)
    if not raw:
        return None
    lines = []
    song = ""
    artist = ""
    in_lyrics = False
    import re
    for ln in str(raw).split("\n"):
        ln = ln.strip()
        if ln.startswith("歌名"):
            song = re.sub(r"^歌名[:：]\s*", "", ln).strip()
        elif ln.startswith("歌手"):
            artist = re.sub(r"^歌手[:：]\s*", "", ln).strip()
        elif ln.startswith("歌词"):
            in_lyrics = True
        elif in_lyrics and ln:
            ln = re.sub(r"^[0-9]+[\.、]\s*", "", ln).strip()
            if len(ln) >= 2:
                lines.append(ln)
    if not song or len(lines) < 4:
        return None
    return {"song_name": song, "artist": artist, "lines": lines[:40], "source": "llm"}
