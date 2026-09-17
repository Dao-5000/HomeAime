# -*- coding: utf-8 -*-
"""「她现在被哪些规则管着」审计器（只读，不改任何状态、不写库、不调模型）。

为什么有这个模块（用户 2026-09-16 原话）：
  · 「我感觉模型是不是被限制，我让她承认爱我都做不到一直再绕圈子」——
    真因是角色卡的 `language_style="tsundere"` 预设往她 prompt 里写死了一行
    「绝对不用这些词：喜欢你/我爱你/当然/没问题」+「用户提问→反问回去」「故意说一半就停」；
  · 用户拍板要「做到 App 里能查」。
所以这里把"管着她的东西"按来源摊开：全局规范逐条、语言风格预设的真实生效项、
影响她的开关现状、注入了哪几块各多少字，以及**已经删掉的限制**（危机干预）。

设计原则：
  · 只读：只用 character_manager.build_system_prompt（纯字符串拼装）+ 预设表 + config 读取；
  · 不猜：清单里的每一项都能指到「哪个文件的哪条规则」；
  · 好读：给界面用的结构（rules / style / switches / blocks / removed）。
"""


def _preset_info(character_id: str) -> dict:
    """角色卡里 language_style 预设的**真实生效项**（禁用词/句式/场景策略/标志句式…）。"""
    try:
        from . import character_manager as cm
        cfg = cm.get_character_any(character_id) or {}
    except Exception:
        cfg = {}
    ls = cfg.get("language_style") if isinstance(cfg, dict) else None
    if not ls:
        return {"preset": "", "label": "", "note": "角色卡没有设置语言风格预设（走通用规范）"}
    out = {"preset": ls if isinstance(ls, str) else "(自定义 dict)", "label": "", "raw": ls}
    try:
        from .personality import presets as _p
        from .personality.style_injector import StyleInjector
        if isinstance(ls, str):
            style, vocab = _p.get_persona_style(ls, dirty_talk_enabled=False)
            out["label"] = (_p.PERSONA_LABELS or {}).get(ls, "")
        else:
            from .personality.language_style import LanguageStyle, VocabBank
            style = LanguageStyle.from_dict(ls)
            vocab = VocabBank.from_dict(ls.get("vocab", {}))
        out["forbidden_words"] = list(getattr(style, "forbidden_words", []) or [])
        _ss = getattr(style, "scene_strategy", None)
        if _ss is not None:
            out["scene_strategy"] = {
                "用户倾诉时": getattr(_ss, "when_user_venting", ""),
                "用户提问时": getattr(_ss, "when_user_asking", ""),
                "用户沉默时": getattr(_ss, "when_user_silent", ""),
                "用户说话很短时": getattr(_ss, "when_user_short", ""),
                "被夸时": getattr(_ss, "when_praised", ""),
                "被怼时": getattr(_ss, "when_attacked", ""),
            }
        out["signature_patterns"] = list(getattr(style, "signature_patterns", []) or [])
        out["catchphrases"] = list(getattr(style, "catchphrases", []) or [])
        out["denial_words"] = list(getattr(vocab, "denial_words", []) or [])
        out["prefer_length"] = getattr(style, "prefer_length", "")
        out["injected_text"] = StyleInjector().build_style_prompt(style, vocab)
    except Exception as e:
        out["error"] = str(e)
    return out


def _switches(character_id: str) -> dict:
    """影响她的开关现状（每一项都说明"开着会发生什么"）。"""
    try:
        from . import character_manager as cm
        cfg = cm.get_character_any(character_id) or {}
    except Exception:
        cfg = {}
    try:
        from . import config as _cfg
        def _g(k, d=None):
            try:
                return _cfg.get(k, d)
            except Exception:
                return d
    except Exception:
        def _g(k, d=None):
            return d

    def _b(v):
        return "开" if v else "关"

    out = {}
    out["深度思考（她会先想再答）"] = _b(cfg.get("deep_thinking"))
    out["思考链展示（前端折叠框）"] = _b(cfg.get("show_thinking"))
    out["完全自主模式（autonomy）"] = str(cfg.get("autonomy") or "跟随全局")
    out["主动消息由模型决定"] = _b(cfg.get("proactive_model_decides"))
    out["本地大脑（Ollama）"] = _b(cfg.get("local_brain"))
    out["括号动作描写"] = _b((cfg.get("dialogue_style") or {}).get("action_brackets", True))
    out["场景裁剪（省 token，会裁掉部分积木）"] = _b(_g("SCENE_PROMPT_TRIM", False))
    out["主动消息总开关"] = _b(_g("IDLE_AGENT_ENABLED", True))
    out["理解层（语义分析）"] = _b(_g("UNDERSTANDING_ENABLED", True))
    out["危机干预（情绪/危机关注模式 + 热线卡片 + 硬拦截）"] = (
        "已整套移除（2026-09-16 用户拍板；原 L3 纯正则硬拦截会把「想死你了」判成极端危机）"
    )
    out["模型"] = {
        "主脑": str(cfg.get("model") or _g("SELECTED_MODEL", "")),
        "深度思考档": str(_g("DEEP_THINKING_MODEL", "")),
        "记忆提炼": str(_g("MEMORY_EXTRACT_MODEL", "")),
    }
    return out


def _rules_from_prompt(prompt: str) -> list:
    """把组装好的人设串里的「全局底层交互规范」逐条抽出来（编号 + 原文）。"""
    out = []
    import re as _re
    for ln in str(prompt or "").splitlines():
        m = _re.match(r"^(\d+)\.\s*(.+)$", ln.strip())
        if m:
            out.append({"no": int(m.group(1)), "text": m.group(2).strip()})
    return out


def _blocks_from_prompt(prompt: str) -> list:
    """按【标题】把注入块切开，给出每块字数（让你看出"她被多少东西管着"）。"""
    import re as _re
    text = str(prompt or "")
    idx = [m.start() for m in _re.finditer(r"【[^】]{1,20}】", text)]
    if not idx:
        return [{"name": "(整段)", "chars": len(text), "preview": text[:80]}]
    out = []
    for i, start in enumerate(idx):
        end = idx[i + 1] if i + 1 < len(idx) else len(text)
        seg = text[start:end]
        name = _re.match(r"【[^】]{1,20}】", seg).group(0)
        out.append({"name": name, "chars": len(seg), "preview": seg[:80].replace("\n", " ")})
    return out


def build_audit(character_id: str = "default", session_id: str = "default") -> dict:
    """组装"她现在被哪些规则管着"。只读：不写库、不调模型。"""
    from datetime import datetime
    try:
        from . import character_manager as cm
        prompt = cm.build_system_prompt(character_id, character_id=character_id,
                                        session_id=session_id) or ""
    except Exception as e:
        prompt = ""
        _err = str(e)
    rules = _rules_from_prompt(prompt)
    style = _preset_info(character_id)
    audit = {
        "character": character_id,
        "session_id": session_id,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "prompt_chars": len(prompt),
        "rules": rules,
        "style": style,
        "switches": _switches(character_id),
        "blocks": _blocks_from_prompt(prompt),
        "removed": [
            "危机干预（情绪关注模式 / 危机关注模式注入）—— 2026-09-16 移除",
            "危机热线卡片 + 前端强制弹窗 + L3 纯正则硬拦截 —— 2026-09-16 移除",
            "傲娇预设的禁用词（喜欢你 / 我爱你 / 当然 / 没问题）—— 2026-09-16 清空",
        ],
        "note": "本清单只读：它不写库、不调模型；要改哪一条，指到对应来源文件即可。",
    }
    return audit
