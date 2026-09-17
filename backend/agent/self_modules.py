# -*- coding: utf-8 -*-
"""自我进化积木（2026-09-11）：骨子的"自我设任务 + 习得规则"能力。

用户提出的治本方案落地——之前约定/承诺类对话只把理解存成模糊中文短语
（trigger_time="今晚11点"），下游正则解析器替模型"再理解一遍"还理解错了。
现在把"理解→行动"直接接上：模型在对话/Agent 模式里自己结构化地设任务、
沉淀被教的习惯，做完的任务自动清除，长期习惯永久保留（积木模块）。

三层自改能力（本模块是轻量两层；重量级改代码走 devtools + 授权）：
  1. schedule_task —— 给自己设任务：一次性（到点兑现后自动 done 清除）
                       / 循环（每天HH:MM，每天兑现一次，永久保留直到取消）
  2. cancel_task   —— 取消自己设的任务
  3. learn_rule    —— 沉淀用户教的习惯/规则 → 注入每次对话 prompt（永久积木）
  4. forget_rule   —— 删除某条积木（按 id 或关键词）
  5. my_rules      —— 查看自己的积木清单

存储（都在统一数据目录内）：
  任务 → open_loops（category=reminder，trigger 存结构化 ISO 或 "每天HH:MM"，
         复用 ai_promise 的到点兑现链路：推 QQ + 推前端 + 人设口吻生成）
  规则 → DATA_DIR/learned_rules/{character}.json
"""
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .. import config as _config
from .. import db as _db


def _rules_path(character_id: str) -> Path:
    safe = re.sub(r'[\\/:*?"<>|]', "_", str(character_id or "default").strip()) or "default"
    return Path(_config.DATA_DIR) / "learned_rules" / f"{safe}.json"


def load_rules(character_id: str) -> list:
    """读取某角色的习得规则列表 [{id, text, created, source}]。"""
    try:
        return json.loads(_rules_path(character_id).read_text(encoding="utf-8")) or []
    except Exception:
        return []


def _save_rules(character_id: str, rules: list) -> None:
    fp = _rules_path(character_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(rules[:200], ensure_ascii=False, indent=2), encoding="utf-8")


MAX_RULES = 200


def learned_rules_block(character_id: str) -> str:
    """生成注入 prompt 的积木清单（无规则返回空串）。"""
    rules = load_rules(character_id)
    if not rules:
        return ""
    lines = "\n".join(f"- [{r.get('id')}] {str(r.get('text') or '').strip()}" for r in rules[:60])
    return ("【你已经学会的习惯与规则（用户教你的长期积木，务必遵守；"
            "若与现实冲突可用 forget_rule 删除后重学）】\n" + lines)


# ──────────────────── 工具实现（签名与 agent/tools.py 一致） ────────────────────

async def _schedule_task(args: dict, ctx: dict) -> dict:
    """给自己设任务：到点由承诺兑现链路主动找用户（推 App + 推 QQ）。

    time 支持三种写法（服务端负责解析并在结果里回显解释结果，模型可自查）：
      "每天23:00" / "每晚11点"          → 循环约定，永久保留
      "2026-09-12 08:00"               → 一次性，兑现后自动清除
      "明晚8点" / "下午3点半"           → 相对表述，按下一次该时刻解析
    """
    character_id = str((ctx or {}).get("character_id") or "default")
    session_id = str((ctx or {}).get("session_id") or "default")
    content = str(args.get("content") or "").strip()
    raw_time = str(args.get("time") or "").strip()
    if not content:
        return {"ok": False, "result": "缺少 content（到点要做什么）"}
    if not raw_time:
        return {"ok": False, "result": "缺少 time（如 每天23:00 / 2026-09-12 08:00 / 明晚8点）"}

    from ..ai_promise import resolve_trigger_phrase, _recurring_hhmm, _norm_trigger_phrase
    norm = _norm_trigger_phrase(raw_time)
    now = datetime.now()
    recurring = bool(_recurring_hhmm(norm))
    if recurring:
        hh, mi = _recurring_hhmm(norm)
        stored = f"每天{hh:02d}:{mi:02d}"
        explain = f"每天 {hh:02d}:{mi:02d}（循环约定，每天到点兑现一次，永久保留）"
    else:
        resolved = resolve_trigger_phrase(norm, now)
        if resolved is None:
            # 兜底：容忍 "HH:MM"
            m = re.match(r"^(\d{1,2})[:：](\d{2})$", norm)
            if m:
                resolved = now.replace(hour=int(m.group(1)), minute=int(m.group(2)))
        if resolved is None:
            return {"ok": False, "result": f"时间没解析出来：{raw_time}（请写成 每天23:00 / 明晚8点 / 2026-09-12 08:00 之一）"}
        if resolved <= now:
            resolved += timedelta(days=1)  # 已过 → 下一次该时刻
        stored = resolved.strftime("%Y-%m-%d %H:%M")
        explain = f"{stored}（一次性，兑现后自动清除）"

    _db.q(
        "INSERT INTO open_loops(session_id, character_id, title, description, category, "
        "status, importance, trigger_time, created_time, update_time) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (session_id, character_id, content[:120], f"(自我设定) {raw_time}",
         "reminder", "pending", 8, stored,
         now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds")),
    )
    row = _db.q("SELECT id FROM open_loops WHERE character_id=? AND title=? ORDER BY id DESC LIMIT 1",
                (character_id, content[:120]), fetch=True)
    try:
        lid = dict(row[0])["id"] if row else "?"
    except Exception:
        lid = "?"
    return {"ok": True, "result": f"已给自己设好任务#{lid}：{content}；触发：{explain}"}


async def _cancel_task(args: dict, ctx: dict) -> dict:
    """取消未完成事项 / 定时提醒。

    ★ 2026-09-17 修（重大缺口）：原实现只 UPDATE `open_loops`，
      **完全不碰 `tasks` 表** —— 而"到点主动发消息"走的是 tasks 表。
      后果（真机事故）：她给用户排了「明天 02:50 提醒睡觉」，用户让她取消，
      她调这个工具只会去找 open_loops，tasks 表那条纹丝不动，
      用户体感就是"我叫她取消还取消不了"。
      现在两张表都处理：open_loops（未完成事项）+ tasks（定时提醒）。
    """
    character_id = str((ctx or {}).get("character_id") or "default")
    session_id = str((ctx or {}).get("session_id") or "default")
    kw = str(args.get("keyword") or "").strip()
    task_id = args.get("id")
    if not kw and task_id is None:
        return {"ok": False, "result": "需要 id 或 keyword 之一"}

    _now = datetime.now().isoformat(timespec="seconds")
    n_loop = n_task = 0

    # ① 未完成事项
    try:
        if task_id is not None:
            _db.q("UPDATE open_loops SET status='cancelled', update_time=? "
                  "WHERE id=? AND character_id=? AND status='pending'",
                  (_now, int(task_id), character_id))
            n_loop += 1
        else:
            rows = _db.q(
                "SELECT id FROM open_loops WHERE character_id=? AND status='pending' "
                "AND title LIKE ? LIMIT 20",
                (character_id, f"%{kw}%"), fetch=True) or []
            for r in rows:
                _db.q("UPDATE open_loops SET status='cancelled', update_time=? WHERE id=?",
                      (_now, dict(r)["id"]))
                n_loop += 1
    except Exception as e:  # noqa: BLE001
        print(f"[SelfModules] 取消 open_loop 失败: {e}", flush=True)

    # ② 定时提醒（tasks 表）—— 原先完全没处理，就是"取消不掉"的根因
    try:
        if task_id is not None:
            if _db.cancel_task(int(task_id), session_id, character_id):
                n_task += 1
        else:
            # ★ 注意：db.list_pending_tasks() 的签名是 (character_name, session_id, character_id)，
            #   **没有 limit 参数**。我第一版传了 limit=50 → TypeError 被 except 吞掉 →
            #   工具表面"没找到可取消的"，实际是调用出错（回归用例抓到的）。
            for t in (_db.list_pending_tasks(session_id=session_id,
                                             character_id=character_id) or []):
                if kw and kw not in str(t.get("content") or ""):
                    continue
                if _db.cancel_task(int(t["id"]), session_id, character_id):
                    n_task += 1
    except Exception as e:  # noqa: BLE001
        print(f"[SelfModules] 取消定时任务失败: {e}", flush=True)

    if not (n_loop or n_task):
        return {"ok": False, "result": f"没找到可取消的{'含「%s」的' % kw if kw else ''}未完成事项或定时提醒"}
    parts = []
    if n_task:
        parts.append(f"定时提醒 {n_task} 条")
    if n_loop:
        parts.append(f"未完成事项 {n_loop} 条")
    return {"ok": True, "result": "已取消 " + "、".join(parts)}


async def _learn_rule(args: dict, ctx: dict) -> dict:
    """沉淀用户教的习惯/规则（永久积木，注入每次对话）。"""
    character_id = str((ctx or {}).get("character_id") or "default")
    text = str(args.get("rule") or "").strip()
    if not text:
        return {"ok": False, "result": "缺少 rule（要记住的习惯/规则内容）"}
    if len(text) > 300:
        text = text[:300]
    rules = load_rules(character_id)
    if any(str(r.get("text") or "").strip() == text for r in rules):
        return {"ok": True, "result": "这条规则已经在积木里了，没有重复添加"}
    new_id = (max((int(r.get("id") or 0) for r in rules), default=0)) + 1
    rules.append({"id": new_id, "text": text, "created": datetime.now().isoformat(timespec="seconds"),
                  "source": "user"})
    _save_rules(character_id, rules)
    return {"ok": True, "result": f"已沉淀为长期积木 #{new_id}：「{text}」（今后每次对话都会带上）"}


async def _forget_rule(args: dict, ctx: dict) -> dict:
    character_id = str((ctx or {}).get("character_id") or "default")
    rules = load_rules(character_id)
    rid = args.get("id")
    kw = str(args.get("keyword") or "").strip()
    if rid is not None:
        kept = [r for r in rules if int(r.get("id") or 0) != int(rid)]
    elif kw:
        kept = [r for r in rules if kw not in str(r.get("text") or "")]
    else:
        return {"ok": False, "result": "需要 id 或 keyword 之一"}
    if len(kept) == len(rules):
        return {"ok": False, "result": "没匹配到要删除的积木"}
    _save_rules(character_id, kept)
    return {"ok": True, "result": f"已删除 {len(rules) - len(kept)} 条积木，剩余 {len(kept)} 条"}


async def _my_rules(args: dict, ctx: dict) -> dict:
    character_id = str((ctx or {}).get("character_id") or "default")
    rules = load_rules(character_id)
    if not rules:
        return {"ok": True, "result": "（还没有沉淀任何积木）"}
    lines = "\n".join(f"#{r.get('id')} {r.get('text')}（{r.get('created')}）" for r in rules)
    return {"ok": True, "result": f"共 {len(rules)} 条积木：\n{lines}"}
