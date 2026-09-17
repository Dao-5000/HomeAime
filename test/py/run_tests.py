# -*- coding: utf-8 -*-
"""零依赖测试运行器（不需要 pytest）。

用法（在 D:\\AI聊天项目桌面端\\AI聊天项目 下）：
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py            # 跑全部
    backend\\venv\\Scripts\\python.exe test\\py\\run_tests.py batch1     # 只跑匹配关键字的文件

设计取舍：本项目的打包环境不装 pytest（会进 PyInstaller 依赖），
所以这里自带一个极小的夹具/断言层，支持：
  · test_* 函数自动发现
  · 名为 fresh_db 的夹具（用例参数名里出现即注入）
  · monkeypatch 极简实现（setattr / 自动还原）
退出码：全绿 0，任意失败 1（可直接接 CI / 打包前检查）。
"""
import importlib.util
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ★ 隔离：任何测试都不许碰用户真实数据目录（必须在 import backend 之前设置）。
#   为什么不用系统临时目录：本仓库的测试可能在文件沙箱/受限环境里跑，
#   Temp 下 sqlite 建库会被拒（实测 "unable to open database file"）。
#   所以固定放在工程内 .pytest_data\ —— 在 workspace 里必定可写，且已 gitignore。
#   注意目录必须**建出来**：config.DATA_DIR 只是常量拼接，不会自己 mkdir。
_TEST_DATA_DIR = PROJECT_ROOT / ".pytest_data"
_TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AI_COMPANION_DATA_DIR", str(_TEST_DATA_DIR))
Path(os.environ["AI_COMPANION_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
# ★ 外置记忆库也必须一起隔离：开发模式下 config.external_memory_dir() 默认落在
#   ROOT_DIR\外置记忆库（= 工程目录），而归档游标 state.json 就存在那里 ——
#   不隔离的话测试会把"小满"的 state.json / 原文 写进工程树，
#   并且上一轮留下的游标会让下一轮误判"已经归档过"（实测踩过）。
os.environ.setdefault("AI_COMPANION_EXTERNAL_MEMORY_DIR",
                      str(_TEST_DATA_DIR / "外置记忆库"))
Path(os.environ["AI_COMPANION_EXTERNAL_MEMORY_DIR"]).mkdir(parents=True, exist_ok=True)
# ★ 离线优先：模型已缓存在本地，任何联网探测都会在无网/内网环境下变成
#   5 次指数退避重试（几十秒~几分钟），实测会把测试套件拖到超时。
#   backend/memory/embedding.py 模块导入时也会 setdefault，这里再兜一层，
#   保证在被 import 之前就已经是离线的（双重保险，不覆盖用户显式设置）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


class _MonkeyPatch:
    """极简 monkeypatch：记录原始值，测试结束后统一还原。"""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value, raising=True):
        had = hasattr(obj, name)
        old = getattr(obj, name, None)
        setattr(obj, name, value)
        self._undo.append((obj, name, had, old))

    def undo(self):
        for obj, name, had, old in reversed(self._undo):
            try:
                if had:
                    setattr(obj, name, old)
                else:
                    delattr(obj, name)
            except Exception:
                pass
        self._undo.clear()


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    keyword = argv[0].lower() if argv else ""
    files = sorted(p for p in HERE.glob("test_*.py"))
    if keyword:
        files = [p for p in files if keyword in p.name.lower()]
    if not files:
        print("没有找到测试文件")
        return 1

    passed, failed, errors = 0, [], []
    for path in files:
        print(f"\n=== {path.name} ===")
        try:
            mod = _load_module(path)
        except Exception:
            errors.append((path.name, "<import>", traceback.format_exc()))
            print(f"  IMPORT FAILED\n{traceback.format_exc()}")
            continue

        for name, fn in sorted(vars(mod).items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            # ★ 先打印再执行并 flush：某个用例卡死时，日志能告诉我们卡在谁身上
            #   （本轮真踩过：batch6 整文件卡死、`通过 N` 永远打不出来，
            #    没有这行就只能靠猜）。
            print(f"  ....  {name}", flush=True)
            kwargs = {}
            params = list(inspect.signature(fn).parameters)
            mp = None
            if "monkeypatch" in params:
                mp = _MonkeyPatch()
                kwargs["monkeypatch"] = mp
            if "fresh_db" in params:
                fixture = getattr(mod, "fresh_db")
                gen = fixture()
                next(gen)          # 进入夹具
                kwargs["fresh_db"] = None
            else:
                gen = None
            try:
                fn(**kwargs)
                print(f"  PASS  {name}")
                passed += 1
            except AssertionError as e:
                failed.append((path.name, name, str(e)))
                print(f"  FAIL  {name}\n        {e}")
            except Exception:
                errors.append((path.name, name, traceback.format_exc()))
                print(f"  ERROR {name}\n{traceback.format_exc()}")
            finally:
                if gen is not None:
                    try:
                        next(gen)
                    except StopIteration:
                        pass
                if mp is not None:
                    mp.undo()

    print("\n" + "=" * 62)
    print(f"通过 {passed} / 失败 {len(failed)} / 报错 {len(errors)}")
    for f, n, msg in failed:
        print(f"  FAIL  {f}::{n} — {msg}")
    for f, n, tb in errors:
        last = tb.strip().splitlines()[-1]
        print(f"  ERROR {f}::{n} — {last}")
    print("=" * 62)
    return 0 if not failed and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
