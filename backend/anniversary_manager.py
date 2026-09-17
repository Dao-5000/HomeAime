# -*- coding: utf-8 -*-
"""
纪念日管理：用户自定义重要日期（生日、相识日、周年等），存在 JSON 文件。
调度器检测到当天是纪念日时，触发对应模板。
"""
import json
from pathlib import Path
from datetime import date
from . import config

ANNIV_FILE = config.ROOT_DIR / "纪念日.json"


def _load() -> list:
    if not ANNIV_FILE.exists():
        return []
    try:
        return json.loads(ANNIV_FILE.read_text("utf-8"))
    except Exception:
        return []


def _save(data: list):
    ANNIV_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def list_anniversaries() -> list:
    return _load()


def add_anniversary(name: str, month: int, day: int, anniv_type: str = "纪念日",
                    character_name: str = "", note: str = "") -> dict:
    data = _load()
    # ★ P1-9：原来用 len(data) + 1 作 id，删除过条目后会与现存 id 撞号：
    #   [1,2,3] 删掉 id=2 → 剩 [1,3]，len=2 → 新 id=3，与现存的 id=3 重复；
    #   此后 delete_anniversary(3) 会把两条一起删掉。改成取现存最大 id + 1。
    _max_id = 0
    for _x in data:
        try:
            _max_id = max(_max_id, int(_x.get("id") or 0))
        except (TypeError, ValueError):
            continue
    item = {
        "id": _max_id + 1,
        "name": name.strip(),
        "month": int(month),
        "day": int(day),
        "type": anniv_type.strip() or "纪念日",
        "character": character_name.strip(),
        "note": note.strip(),
    }
    data.append(item)
    _save(data)
    return item


def delete_anniversary(anniv_id: int) -> bool:
    data = _load()
    new_data = [x for x in data if x.get("id") != anniv_id]
    if len(new_data) < len(data):
        _save(new_data)
        return True
    return False


def today_anniversaries(today: date = None, character_name: str = "") -> list:
    """今天的纪念日列表，可按角色筛选"""
    today = today or date.today()
    data = _load()
    result = []
    for item in data:
        if item.get("month") == today.month and item.get("day") == today.day:
            if not character_name or not item.get("character") or item.get("character") == character_name:
                result.append(item)
    return result
