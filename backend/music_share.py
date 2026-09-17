# -*- coding: utf-8 -*-
"""
QQ 点歌：搜网易云歌曲 → 返回歌曲信息，供发网易云音乐卡片（OneBot music 段 type=163）。

★ 用网易云公开搜索接口（无需登录/加密），失败时返回空。
纯新增模块，不改动任何现有逻辑。
"""
import json
import urllib.parse
import urllib.request

SEARCH_API = "https://music.163.com/api/search/get/web"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://music.163.com/",
}


def search_song(keyword: str, limit: int = 5) -> list:
    """搜网易云歌曲。返回 [{id, name, artist}]，失败返回 []。"""
    if not keyword or not str(keyword).strip():
        return []
    q = urllib.parse.urlencode({
        "s": str(keyword).strip(), "type": 1, "offset": 0, "limit": int(limit),
    })
    url = f"{SEARCH_API}?{q}"
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        data = json.loads(raw)
        songs = ((data or {}).get("result") or {}).get("songs") or []
        out = []
        for s in songs:
            artists = s.get("artists") or []
            out.append({
                "id": str(s.get("id") or ""),
                "name": str(s.get("name") or ""),
                "artist": str((artists[0] or {}).get("name") or "") if artists else "",
            })
        return out
    except Exception as e:
        print(f"[Music] 搜歌失败: {e}", flush=True)
        return []


def pick_song(keyword: str) -> dict:
    """搜歌并挑一首最合适的。返回 {id, name, artist} 或 None。
    优先选「歌名完全等于关键词」的结果（避免搜到翻唱/remix），否则取第一条。"""
    songs = search_song(keyword, limit=5)
    if not songs:
        return None
    kw = str(keyword or "").strip().lower()
    for s in songs:
        if s["name"].lower() == kw:
            return s
    return songs[0]
