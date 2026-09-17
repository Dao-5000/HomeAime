# -*- coding: utf-8 -*-
"""
记忆导入导出：
  导出：① AES 加密 JSON（结构化，密钥用户设置）② 纯文本 TXT（每条一行，UTF-8 带 BOM 防乱码）
  导入：按文件扩展名自动识别（.json / .txt），支持「合并」或「覆盖」两种模式。
"""
import base64
import hashlib
import json
import os
from datetime import datetime

from . import db

try:
    from Crypto.Cipher import AES          # pycryptodome
    from Crypto.Util.Padding import pad, unpad
    _HAS_AES = True
except ImportError:
    _HAS_AES = False

EXPORT_FORMAT_JSON = "json"
EXPORT_FORMAT_TXT = "txt"


def _derive_key(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def export_json_encrypted(password: str, session_id: str = "default", character_id: str = "default") -> bytes:
    """加密 JSON 导出：{format, iv, data(base64 AES-CBC), count, exported_at}"""
    if not _HAS_AES:
        raise RuntimeError("缺少加密库：请 pip install pycryptodome")
    if not password or len(password) < 4:
        raise ValueError("加密密钥至少 4 个字符")
    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    plain = json.dumps({
        "app": "ai-companion-pc",
        "kind": "long_term_memory",
        "session_id": session_id,
        "character_id": character_id,
        "memories": mems,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False).encode("utf-8")
    iv = os.urandom(16)
    cipher = AES.new(_derive_key(password), AES.MODE_CBC, iv)
    data = base64.b64encode(cipher.encrypt(pad(plain, AES.block_size))).decode("ascii")
    doc = {
        "format": "aes-json",
        "iv": base64.b64encode(iv).decode("ascii"),
        "data": data,
        "count": len(mems),
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")


def export_json_backup(password: str = "", session_id: str = "default", character_id: str = "default") -> bytes:
    """Export a restorable structured backup; encrypt only when a key is supplied."""
    mems = db.valid_memories(session_id=session_id, character_id=character_id)
    payload = {
        "app": "ai-companion-pc", "kind": "memory_backup_v2",
        "session_id": session_id, "character_id": character_id,
        "memories": mems,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not password:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if not _HAS_AES:
        raise RuntimeError("missing encryption library: pycryptodome")
    if len(password) < 4:
        raise ValueError("backup key must contain at least 4 characters")
    plain = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    iv = os.urandom(16)
    cipher = AES.new(_derive_key(password), AES.MODE_CBC, iv)
    doc = {
        "format": "aes-json",
        "iv": base64.b64encode(iv).decode("ascii"),
        "data": base64.b64encode(cipher.encrypt(pad(plain, AES.block_size))).decode("ascii"),
        "count": len(mems),
        "exported_at": payload["exported_at"],
    }
    return json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")


def export_txt(session_id: str = "default", character_id: str = "default") -> bytes:
    """纯文本导出：每条记忆一行（UTF-8 带 BOM，Windows 记事本打开不乱码）"""
    mems = [m["memory_content"] for m in db.valid_memories(session_id=session_id, character_id=character_id)]
    text = "\n".join(mems)
    return ("\ufeff" + text + ("\n" if text else "")).encode("utf-8")


def _decrypt_json(password: str, raw: bytes) -> list:
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict) or doc.get("format") != "aes-json":
        raise ValueError("不是有效的加密记忆备份文件")
    iv = base64.b64decode(doc["iv"])
    data = base64.b64decode(doc["data"])
    cipher = AES.new(_derive_key(password), AES.MODE_CBC, iv)
    plain = unpad(cipher.decrypt(data), AES.block_size)
    obj = json.loads(plain.decode("utf-8"))
    mems = obj.get("memories")
    if not isinstance(mems, list):
        raise ValueError("备份内容结构异常")
    return [x if isinstance(x, dict) else str(x).strip() for x in mems if str(x).strip()]


def _parse_txt(raw: bytes) -> list:
    """纯文本导入：每行一条，空行忽略；兼容 UTF-8 BOM / UTF-16 / GBK"""
    for enc in ("utf-8-sig", "utf-16", "gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        raise ValueError("无法识别文件编码")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def import_backup(filename: str, raw: bytes, mode: str, password: str = "",
                  session_id: str = "default", character_id: str = "default") -> dict:
    """
    导入记忆。filename 决定解析方式（.json 加密 / 其他按纯文本）。
    mode: 'merge' 合并去重 | 'overwrite' 清空后导入
    session_id / character_id: 导入目标角色（多角色隔离，不误删其他角色数据）
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".json":
        doc = json.loads(raw.decode("utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("memories"), list):
            items = doc["memories"]
        else:
            items = _decrypt_json(password or "", raw)
    else:
        items = _parse_txt(raw)
    if not items:
        raise ValueError("文件里没有可导入的记忆")

    if mode == "overwrite":
        # ★ 只清空目标 (session_id, character_id) 的记忆，不再 DELETE 全表
        db.replace_all_memories(
            [x.get("memory_content", "") if isinstance(x, dict) else str(x) for x in items],
            session_id=session_id, character_id=character_id
        )
        return {"mode": "overwrite", "imported": len(items), "total": len(items)}

    added = skipped = 0
    from .memory_manager import dedupe_insert   # 延迟导入避免循环依赖
    for it in items:
        content = it.get("memory_content", "") if isinstance(it, dict) else str(it)
        if not str(content).strip():
            skipped += 1
            continue
        action = dedupe_insert(
            str(content),
            memory_type=(it.get("memory_type", "fact") if isinstance(it, dict) else "fact"),
            importance=(it.get("importance", 5) if isinstance(it, dict) else 5),
            session_id=session_id, character_id=character_id,
            memory_scope=(it.get("memory_scope") if isinstance(it, dict) else None),
            context=(it.get("context", "") if isinstance(it, dict) else ""),
            emotion_tag=(it.get("emotion_tag", "") if isinstance(it, dict) else ""),
            source_text=(it.get("source_text", "") if isinstance(it, dict) else ""),
        )
        if action in ("inserted", "updated"):
            added += 1
        else:
            skipped += 1
    total = len(db.valid_memories(session_id=session_id, character_id=character_id))
    return {"mode": "merge", "imported": added, "skipped": skipped, "total": total}
