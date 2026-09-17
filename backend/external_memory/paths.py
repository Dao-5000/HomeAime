# -*- coding: utf-8 -*-
"""外置记忆库的目录路径与状态管理。"""
import json
import sys
from pathlib import Path

from .. import config


def base_dir() -> Path:
    """外置记忆库根目录（统一走 config.external_memory_dir，含打包模式 DATA_DIR 兜底）。"""
    try:
        return Path(config.external_memory_dir())
    except Exception:
        return config.ROOT_DIR / "外置记忆库"


def char_dir(character_id: str) -> Path:
    cid = str(character_id or "default").strip() or "default"
    return base_dir() / cid


def raw_dir(character_id: str) -> Path:
    return char_dir(character_id) / "原文"


def daily_dir(character_id: str) -> Path:
    return char_dir(character_id) / "日总结"


def weekly_dir(character_id: str) -> Path:
    return char_dir(character_id) / "周总结"


def monthly_dir(character_id: str) -> Path:
    return char_dir(character_id) / "月总结"


def state_path(character_id: str) -> Path:
    return char_dir(character_id) / "state.json"


def ensure_dirs(character_id: str) -> None:
    for d in (raw_dir(character_id), daily_dir(character_id),
              weekly_dir(character_id), monthly_dir(character_id)):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def load_state(character_id: str) -> dict:
    p = state_path(character_id)
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def save_state(character_id: str, state: dict) -> None:
    try:
        ensure_dirs(character_id)
        state_path(character_id).write_text(
            json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def mark_done(character_id: str, key: str, value: str) -> None:
    """在 state.json 里记录「已生成」，用于开机幂等 / 断点续传。"""
    state = load_state(character_id)
    lst = state.get(key) or []
    if value not in lst:
        lst.append(value)
        state[key] = lst
        save_state(character_id, state)
