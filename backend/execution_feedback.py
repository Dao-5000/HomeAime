# -*- coding: utf-8 -*-
"""
方案 C：执行闭环。模型「说了要做某事」→ 后端执行 → 执行结果回喂给模型（下一轮）。

解决「模型幻觉：以为自己发了/做了，实际没有」的根本机制：
模型写完 [sticker:xxx] / [SONG] / [ACTION] 后由后端执行；执行成功与否，下一轮聊天
回喂给模型，让它逐步纠正「说≠做」的认知偏差——光说「我发了」「我做了」不算完成。

用法：
  - 执行器（onebot 发图、点歌、控制电脑等）调用 report() 记录结果；
  - enrich_messages 调用 build_feedback_block() 把结果回喂给模型（读完即清空，避免重复）。
"""
import threading

_lock = threading.Lock()
_feedback = []  # [{action, ok, detail}]，按时间追加


def report(action: str, ok: bool, detail: str = ""):
    """记录一次动作的执行结果。action 用中文名（如「发表情包」「点歌」）。"""
    global _feedback
    with _lock:
        _feedback.append({"action": action, "ok": bool(ok), "detail": str(detail or "")})
        if len(_feedback) > 5:  # 只保留最近 5 条
            _feedback = _feedback[-5:]


def build_feedback_block() -> str:
    """生成「上一轮动作执行结果」反馈块，读完即清空（反馈一次即可）。"""
    global _feedback
    with _lock:
        items = list(_feedback)
        _feedback = []
    if not items:
        return ""
    lines = ["【你上一次动作的执行结果（纠正「说≠做」）】"]
    for r in items:
        if r["ok"]:
            lines.append(f"- {r['action']}：已成功。")
        else:
            _detail = ("，" + r["detail"]) if r["detail"] else ""
            lines.append(
                f"- {r['action']}：没做成{_detail}。"
                f"以后别只用文字说「我发了」「我做了」就算完成，要么重试、要么如实告诉 TA 没做成。"
            )
    return "\n".join(lines)
