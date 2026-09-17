# -*- coding: utf-8 -*-
"""
歌曲库：管理「AI 学会的歌」
=========================

职责：导入整首歌、记录处理进度、存放分离/换声后的中间产物。

状态流转：
    imported ──▶ separating ──▶ separated ──▶ training ──▶ ready
         │            │             │             │
         └────────────┴─────────────┴─────────────┴──▶ failed

当前阶段（阶段一）只落地 imported 状态与文件/元数据管理，
separating / training 由后续接入 demucs 与 RVC 后推进。

存储结构：
    <DATA_DIR>/songs/
        index.json                  全量索引
        <song_id>/
            original.<ext>          用户导入的原曲
            vocals.wav              （后续）demucs 分离出的人声
            accompaniment.wav       （后续）伴奏
            ai_vocals.wav           （后续）RVC 换声后的人声
"""
import json
import os
import time
import uuid

SONG_STATUS = ("imported", "separating", "converting", "rendering", "ready", "failed")

# 学习处于这些状态时，前端会持续轮询刷新进度
SONG_BUSY_STATUS = ("separating", "converting", "rendering")


def _data_dir() -> str:
    try:
        from .. import config as _cfg
        root = str(getattr(_cfg, "DATA_DIR", "") or "")
    except Exception:
        root = ""
    if not root:
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return root


def songs_root() -> str:
    d = os.path.join(_data_dir(), "songs")
    os.makedirs(d, exist_ok=True)
    return d


def _index_path() -> str:
    return os.path.join(songs_root(), "index.json")


def song_dir(song_id: str) -> str:
    return os.path.join(songs_root(), str(song_id))


def _load_index() -> dict:
    p = _index_path()
    if not os.path.exists(p):
        return {"songs": []}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("songs"), list):
            return {"songs": []}
        return data
    except Exception:
        return {"songs": []}


def _save_index(data: dict):
    p = _index_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def list_songs(session_id: str = "", character_id: str = "") -> list:
    """列出歌曲；传入隔离键时只返回该角色的歌。"""
    songs = _load_index().get("songs") or []
    if session_id or character_id:
        songs = [
            s for s in songs
            if str(s.get("session_id") or "") == str(session_id or "")
            and str(s.get("character_id") or "") == str(character_id or "")
        ]
    songs.sort(key=lambda s: float(s.get("created_at") or 0), reverse=True)
    return songs


def get_song(song_id: str) -> dict:
    for s in _load_index().get("songs") or []:
        if str(s.get("id")) == str(song_id):
            return s
    return {}


def add_song(title: str, file_bytes: bytes, ext: str = "mp3",
             artist: str = "", session_id: str = "", character_id: str = "") -> dict:
    """导入一首歌：落盘原曲并登记索引。"""
    ext = (ext or "mp3").lower().lstrip(".")
    if ext not in ("mp3", "wav", "flac", "m4a", "ogg"):
        ext = "mp3"
    song_id = "song_" + uuid.uuid4().hex[:12]
    d = song_dir(song_id)
    os.makedirs(d, exist_ok=True)
    orig = os.path.join(d, f"original.{ext}")
    with open(orig, "wb") as f:
        f.write(file_bytes)
    now = time.time()
    song = {
        "id":            song_id,
        "title":         str(title or "").strip() or "未命名",
        "artist":        str(artist or "").strip(),
        "session_id":    str(session_id or ""),
        "character_id":  str(character_id or ""),
        "original_path": orig,
        "orig_ext":      ext,
        "size_bytes":    len(file_bytes),
        "status":        "imported",
        "stage":         "排队中",
        "progress":      0,
        "vocals_path":       "",
        "accompaniment_path": "",
        "ai_vocals_path":    "",
        "final_path":        "",
        "lyrics_path":       "",
        "duration":      0.0,
        "error":         "",
        "created_at":    now,
        "updated_at":    now,
    }
    data = _load_index()
    data.setdefault("songs", []).append(song)
    _save_index(data)
    return song


def update_song(song_id: str, **fields) -> dict:
    """更新歌曲字段（status / 产物路径 / 错误信息等）。"""
    data = _load_index()
    songs = data.setdefault("songs", [])
    for i, s in enumerate(songs):
        if str(s.get("id")) == str(song_id):
            s.update({k: v for k, v in fields.items() if k != "id"})
            s["updated_at"] = time.time()
            songs[i] = s
            _save_index(data)
            return s
    return {}


def delete_song(song_id: str) -> bool:
    """删除歌曲及其全部产物文件。"""
    import shutil
    data = _load_index()
    songs = data.get("songs") or []
    rest = [s for s in songs if str(s.get("id")) != str(song_id)]
    if len(rest) == len(songs):
        return False
    data["songs"] = rest
    _save_index(data)
    shutil.rmtree(song_dir(song_id), ignore_errors=True)
    return True


def public_view(song: dict) -> dict:
    """对外返回的字段（不暴露绝对路径）。"""
    if not song:
        return {}
    return {
        "id":       song.get("id", ""),
        "title":    song.get("title", ""),
        "artist":   song.get("artist", ""),
        "status":   song.get("status", "imported"),
        # stage = 当前这一步在做什么（"分离人声与伴奏"/"用她的音色学唱"/"合并伴奏"）
        # progress = 0~100，供前端画进度条
        "stage":    song.get("stage", "") or "",
        "progress": int(song.get("progress") or 0),
        "duration": round(float(song.get("duration") or 0), 1),
        "size_mb":  round(float(song.get("size_bytes") or 0) / 1048576, 2),
        "has_vocals":        bool(song.get("vocals_path")),
        "has_accompaniment": bool(song.get("accompaniment_path")),
        "has_ai_vocals":     bool(song.get("ai_vocals_path")),
        "has_final":         bool(song.get("final_path")),
        "error":     song.get("error", ""),
        "created_at": song.get("created_at", 0),
        "updated_at": song.get("updated_at", 0),
    }
