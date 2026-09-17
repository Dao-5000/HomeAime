# -*- coding: utf-8 -*-
"""
屏幕感知 4 功能闭环（扩展版）逻辑测试
====================================
覆盖本次扩展规范的验收点：

【真实 import 逻辑测试】
  T1  check_screen_context 中 system_hint 已定义（修复原规范 NameError）
  T2  _write_screen_memory 受 SCREEN_MEMORY_ENABLED 门控
  T3  记忆写入使用 session_id 作为 user_id 与 character_id
  T4  记忆内容含 session_id 与 topic_hint
  T5  高专注(>=75)且无话题 不写记忆
  T6  idle 活动返回 None 且不写记忆

【静态标记测试】（避免拉起整套后端运行时）
  S1  voice_call.py：call_hint 人格灵魂 + Q3 触发检测 + Q4 屏幕陪伴脚本
  S2  proactive_manager.py：capture_and_analyze_for_user
  S3  config.py：SCREEN_MEMORY_ENABLED 白名单
  S4  main.py：POST /api/screen/look 路由
  S5  voice_call.js：_pickBubble 角色分支（傲娇/温柔/活泼）
  S6  chat.js：👁 按钮 🔄 转圈 + #chat-inputbar + Chat.send
  S7  settings.js：toggle-switch + SCREEN_MEMORY_ENABLED + memSw
  S8  style.css：.toggle-switch / .toggle-slider
"""
import os
import sys
import types
import importlib.util

# 相对定位项目根：脚本在 backend/ 下，上级即项目根（避免硬编码路径导致换目录跑不了）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")

results = []
def check(name, ok, detail=""):
    results.append((bool(ok), name, detail))
    mark = "✓" if ok else "✗"
    extra = f"   -> {detail}" if (detail and not ok) else ""
    print(f"  {mark} {name}{extra}")

# ============================================================
# 真实 import：triggers（桩替身 config / memory，避免重依赖与联网）
# ============================================================
print("=" * 60)
print("真实逻辑测试：backend.proactive.triggers")
print("=" * 60)

# 1) 构造 backend / backend.config / backend.proactive 桩，使相对导入可解析
backend_pkg = types.ModuleType("backend")
backend_pkg.__path__ = [BACKEND]
backend_pkg.__package__ = "backend"
sys.modules["backend"] = backend_pkg

gate = {"SCREEN_MEMORY_ENABLED": True}
config_mod = types.ModuleType("backend.config")
config_mod.get = lambda k, d=None: gate.get(k, d)
sys.modules["backend.config"] = config_mod

proactive_pkg = types.ModuleType("backend.proactive")
proactive_pkg.__package__ = "backend.proactive"
proactive_pkg.__path__ = [os.path.join(BACKEND, "proactive")]
sys.modules["backend.proactive"] = proactive_pkg

# 2) 以 backend.proactive.triggers 名义从文件加载（函数内相对导入不会在加载期执行）
spec = importlib.util.spec_from_file_location(
    "backend.proactive.triggers",
    os.path.join(BACKEND, "proactive", "triggers.py"),
)
triggers = importlib.util.module_from_spec(spec)
sys.modules["backend.proactive.triggers"] = triggers
try:
    spec.loader.exec_module(triggers)
    loaded = True
except Exception as e:
    loaded = False
    import traceback
    check("加载 triggers.py", False, repr(e)); traceback.print_exc()

if loaded:
    # 3) 记忆管理器替身（记录调用，避免真实 embedding 联网）
    class FakeMgr:
        def __init__(self):
            self.calls = []
        def add_memory(self, **kw):
            self.calls.append(kw)
    fake = FakeMgr()
    triggers._get_memory_mgr = lambda: fake

    # --- T2/T3/T4：门控开启 + 有趣话题 + session_id ---
    fake.calls.clear()
    ss = {"session_id": "老公", "summary": "在debug一个bug", "activity": "coding",
          "engagement": 40, "interesting": True, "topic_hint": "在修登录接口"}
    try:
        triggers.check_screen_context(ss)
        ok_uid = any(c.get("user_id") == "老公" and c.get("character_id") == "老公"
                     for c in fake.calls)
        check("T3 记忆写入使用 session_id(老公) 作为 user_id/character_id", ok_uid,
              f"calls={fake.calls}")
        ok_ct = any("老公" in c.get("content", "") and "登录接口" in c.get("content", "")
                    for c in fake.calls)
        check("T4 记忆内容含 session_id 与 topic_hint", ok_ct,
              f"calls={fake.calls}")
        check("T2(a) 门控开启时写入记忆", len(fake.calls) > 0, f"calls={fake.calls}")
    except NameError as e:
        check("T1 system_hint 已定义（无 NameError）", False, f"NameError: {e}")
    except Exception as e:
        check("check_screen_context 运行异常", False, repr(e))

    # --- T2(b)：门控关闭 -> 不写 ---
    fake.calls.clear()
    gate["SCREEN_MEMORY_ENABLED"] = False
    triggers.check_screen_context(ss)
    check("T2(b) SCREEN_MEMORY_ENABLED=False 时跳过记忆写入", len(fake.calls) == 0,
          f"calls={fake.calls}")
    gate["SCREEN_MEMORY_ENABLED"] = True

    # --- T5：高专注(>=75)且无话题 -> 不写 ---
    fake.calls.clear()
    ss3 = {"session_id": "老公", "summary": "在写代码", "activity": "coding",
           "engagement": 90, "interesting": False, "topic_hint": ""}
    triggers.check_screen_context(ss3)
    check("T5 高专注(>=75)且无话题 不写记忆", len(fake.calls) == 0, f"calls={fake.calls}")

    # --- T1：system_hint 已定义（原规范 NameError 不复现） ---
    try:
        r = triggers.check_screen_context(
            {"session_id": "x", "summary": "看视频", "activity": "video",
             "engagement": 30, "interesting": True, "topic_hint": "在看剧"})
        check("T1 check_screen_context 不抛 NameError(system_hint已定义)", r is not None,
              f"returned={r}")
    except NameError as e:
        check("T1 check_screen_context 不抛 NameError", False, f"NameError: {e}")
    except Exception as e:
        check("T1 check_screen_context 运行异常", False, repr(e))

    # --- T6：idle 不触发且不写 ---
    fake.calls.clear()
    r6 = triggers.check_screen_context(
        {"session_id": "老公", "activity": "idle", "engagement": 10,
         "interesting": False, "summary": "锁屏"})
    check("T6 idle 活动返回 None 且不写记忆", r6 is None and len(fake.calls) == 0,
          f"r={r6}, calls={fake.calls}")

# ============================================================
# 静态标记测试：其余文件（读取为文本，验证关键实现点）
# ============================================================
def read(rel):
    p = os.path.join(ROOT, rel) if not rel.startswith(BACKEND) else rel
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()

print("\n" + "=" * 60)
print("静态标记测试")
print("=" * 60)

# S1 voice_call
try:
    vc = read("backend/voice_call.py")
    s1 = [
        ("call_hint 人格灵魂 personality_directive", "personality_directive" in vc),
        ("Q3 触发检测 instruction_hint", "instruction_hint" in vc),
        ("Q4 屏幕陪伴 screen_hint", "screen_hint" in vc),
        ("_get_screen_companion_scripts 方法", "def _get_screen_companion_scripts" in vc),
        ("Q3 触发短语『看看屏幕』", "看看屏幕" in vc),
        ("Q3 触发短语『陪我』", "陪我" in vc),
        ("call_hint 组装", "call_hint" in vc),
    ]
    for n, ok in s1:
        check("S1 " + n, ok)
except Exception as e:
    check("S1 读取 voice_call.py", False, repr(e))

# S2 proactive_manager
try:
    pm = read("backend/proactive/proactive_manager.py")
    s2 = [
        ("capture_and_analyze_for_user 函数", "def capture_and_analyze_for_user" in pm),
        ("调用 capture_local", "capture_local" in pm),
        ("调用 save_capture", "save_capture" in pm),
        ("调用 analyze_screen", "analyze_screen" in pm),
    ]
    for n, ok in s2:
        check("S2 " + n, ok)
except Exception as e:
    check("S2 读取 proactive_manager.py", False, repr(e))

# S3 config
try:
    cfg = read("backend/config.py")
    check("S3 SCREEN_MEMORY_ENABLED 进入 update 白名单",
          '"SCREEN_MEMORY_ENABLED" in patch' in cfg)
except Exception as e:
    check("S3 读取 config.py", False, repr(e))

# S4 main
try:
    m = read("backend/main.py")
    check("S4 POST /api/screen/look 路由存在", "api/screen/look" in m)
except Exception as e:
    check("S4 读取 main.py", False, repr(e))

# S5 voice_call.js
try:
    vj = read("public/js/voice_call.js")
    s5 = [
        ("_pickBubble 傲娇分支", "傲娇" in vj),
        ("_pickBubble 温柔分支", "温柔" in vj),
        ("_pickBubble 活泼分支", "活泼" in vj),
    ]
    for n, ok in s5:
        check("S5 " + n, ok)
except Exception as e:
    check("S5 读取 voice_call.js", False, repr(e))

# S6 chat.js
# ★ 修复：原 S6 块被误缩进进 S5 的 except 分支内，导致 S5 正常通过时 S6 永远不执行，
#   只有 S5 抛异常时 S6 才会跑。现移出为独立 try 块。
try:
    cj = read("public/js/chat.js")
    s6 = [
        ("👁 按钮 _showLoading 加载态（非硬编码）", "_showLoading" in cj),
        ("使用正确选择器 #chat-inputbar", "chat-inputbar" in cj),
        ("调用 Chat.send（非 sendMessage）", ("Chat?.send" in cj or "Chat.send(" in cj) and "Chat.sendMessage" not in cj),
        ("修复：用 API 返回 data.topic_hint", "topic_hint" in cj),
        ("修复：用 API 返回 data.activity", "activity" in cj),
        ("修复：按 activity 生成引导语 guide_map", "guide_map" in cj),
    ]
    for n, ok in s6:
        check("S6 " + n, ok)
except Exception as e:
    check("S6 读取 chat.js", False, repr(e))

# S7 settings.js
try:
    sj = read("public/js/settings.js")
    s7 = [
        ("屏幕组用 .toggle-switch 风格", "toggle-switch" in sj),
        ("记忆开关绑定 SCREEN_MEMORY_ENABLED", "SCREEN_MEMORY_ENABLED" in sj),
        ("memSw 记忆开关变量", "memSw" in sj),
        ("保存走 savePcConfig（非 /api/config）", "savePcConfig" in sj and "/api/config" not in sj),
    ]
    for n, ok in s7:
        check("S7 " + n, ok)
except Exception as e:
    check("S7 读取 settings.js", False, repr(e))

# S8 style.css
try:
    css = read("public/css/style.css")
    s8 = [
        (".toggle-switch 样式", ".toggle-switch" in css),
        (".toggle-slider 样式", ".toggle-slider" in css),
    ]
    for n, ok in s8:
        check("S8 " + n, ok)
except Exception as e:
    check("S8 读取 style.css", False, repr(e))

# ============================================================
# 汇总
# ============================================================
passed = sum(1 for ok, _, _ in results if ok)
total = len(results)
print("\n" + "=" * 60)
print(f"结果：{passed}/{total} 通过")
print("=" * 60)
if passed != total:
    print("失败项：")
    for ok, name, detail in results:
        if not ok:
            print(f"  ✗ {name}  {detail}")
    sys.exit(1)
print("全部通过 ✅")
