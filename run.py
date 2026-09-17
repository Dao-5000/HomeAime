# -*- coding: utf-8 -*-
"""
后端启动入口
python run.py 或 PyInstaller 打包后的 exe
"""
import os
# ★ 修复向量模型卡60秒：使用 HuggingFace 镜像站，避免国内访问超时
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
# ★ 模型已缓存在本地（all-MiniLM-L6-v2 / whisper-base），强制离线优先。
#   光设镜像站不够：SentenceTransformer 加载时仍会去 huggingface.co 做 HEAD 探测
#   （查 adapter_config.json），国内网络直接 [WinError 10060] 超时并重试 5 次
#   （1/2/4/8s），于是"记忆写入 / 语义检索"每次都要多卡几十秒。
#   用 setdefault：确实需要联网下载新模型时，仍可用环境变量覆盖。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import sys
import signal

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

# ★ 启动自清理（2026-09-09）：单文件模式每次启动解压 ~4.7GB 到 %TEMP%\_MEI*，
#   正常退出会自清，但强杀（任务管理器/热替换 taskkill /F）不会——
#   实测一夜堆积 79GB 把 C 盘塞满。这里清掉「其他进程遗留」的孤儿 _MEI
#   目录（>24h 未更新），跳过本进程自己的解压目录。
try:
    import shutil as _shutil
    import time as _time
    if getattr(sys, "frozen", False):
        _meipass_cur = os.path.abspath(getattr(sys, "_MEIPASS", "") or "-")
        _temp_root = os.environ.get("TEMP", "")
        for _name in os.listdir(_temp_root):
            if not _name.startswith("_MEI"):
                continue
            _d = os.path.join(_temp_root, _name)
            try:
                if os.path.abspath(_d) == _meipass_cur:
                    continue
                # ★ 2026-09-11 改用「重命名探测」：原 24h-mtime 阈值拦不住短时间堆积——
                #   实测一夜 15 个孤儿 × 4.7GB 又把 C 盘塞满。孤儿目录可改名 → 删除；
                #   活实例的解压目录被运行中的 exe 锁住 → rename 直接失败 → 跳过。
                _renamed = _d + "_purge"
                try:
                    os.rename(_d, _renamed)
                except OSError:
                    continue  # 占用中 = 活实例
                _shutil.rmtree(_renamed, ignore_errors=True)
            except Exception:
                continue
except Exception:
    pass

import uvicorn
from backend.main import app

# ── 日志 tee：print 同时写文件 + uvicorn 日志进文件（网页版/桌面版共享开发者日志）──
# 注意：不能替换 sys.stdout（uvicorn 的 logging.dictConfig 依赖它，会崩）
_DEV_LOG = os.environ.get("DEV_LOG", "").strip()
if not _DEV_LOG:
    # ★ 默认落盘到数据目录：打包版（pc_backend.exe）是 GUI 无控制台，print 直接丢，
    #   出问题（图片分析失败 / 生成失败 / 理解层降级）无从排查。
    #   DEV_LOG 环境变量仍可覆盖到自定义路径。
    try:
        from backend import config as _bcfg
        _DEV_LOG = os.path.join(str(_bcfg.DATA_DIR), "backend_dev.log")
    except Exception:
        _DEV_LOG = ""
if _DEV_LOG:
    try:
        os.makedirs(os.path.dirname(_DEV_LOG), exist_ok=True)
        _log_fh = open(_DEV_LOG, "a", encoding="utf-8", buffering=1)
        # ★ 重定义 builtins.print（而非 run.py 模块内 print），让所有模块
        #   （onebot.py 的 [OneBot] 报错、chat_logic、understanding 等）的 print
        #   都 tee 到日志文件。之前只重定义模块内 print，导致 OneBot 报错落不了盘。
        import builtins
        _orig_print = builtins.print
        def _tee_print(*args, **kwargs):
            kwargs.pop("flush", None)
            try:
                _orig_print(*args, **kwargs)
            except Exception:
                pass
            try:
                _orig_print(*args, file=_log_fh, flush=True)
            except Exception:
                pass
        builtins.print = _tee_print
        # uvicorn 日志（INFO/ERROR/traceback）进文件：在 uvicorn.run 前配好，且不碰 sys.stdout
        import logging
        _file_handler = logging.FileHandler(_DEV_LOG, encoding="utf-8")
        _file_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("uvicorn").addHandler(_file_handler)
        logging.getLogger("uvicorn.error").addHandler(_file_handler)
        logging.getLogger("uvicorn.access").addHandler(_file_handler)
        # ★ root logger 接管：让 bot_controller / onebot / scheduler / idle_agent
        #   等用 logging.getLogger(__name__) 的 info/error 也能落盘。
        #   之前只接了 uvicorn，所以 [MCBot] / [OneBot] 之类的报错"无声失败"，
        #   排查时完全看不到。
        if not any(getattr(h, '_ai_companion_root_fh', False) for h in logging.getLogger().handlers):
            _root_fh = logging.FileHandler(_DEV_LOG, encoding="utf-8")
            _root_fh.setLevel(logging.INFO)
            _root_fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
            _root_fh._ai_companion_root_fh = True
            logging.getLogger().addHandler(_root_fh)
        if logging.getLogger().level == logging.NOTSET or logging.getLogger().level > logging.INFO:
            logging.getLogger().setLevel(logging.INFO)
    except Exception as _e:
        print(f"[run.py] DEV_LOG 初始化失败: {_e}", flush=True)


def _handle_exit(signum, frame):
    print(f"[run.py] 收到退出信号 {signum}，准备关闭...", flush=True)
    raise SystemExit(0)


if __name__ == "__main__":
    # ★ PyInstaller onefile 必备：依赖（chromadb/comtypes 等）在 Windows spawn 子进程时，
    #   子进程会重新执行打包的 exe——没有 freeze_support 就会跑出第二个完整后端
    #   （症状：pc_backend.exe 双进程、端口互踢、状态分裂）。
    import multiprocessing
    multiprocessing.freeze_support()

    signal.signal(signal.SIGINT,  _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    # ★ 2026-09-12 惰性化：CosyVoice 不再启动时拉起/预热（原先这里 spawn + 预热两段
    #   导致每次进 APP 就常驻吃内存）。改由 backend/cosyvoice_mgr.py 在真正要用语音时
    #   （气泡配音/通话/唱歌/克隆）按需拉起，空闲自动关闭；拉不起就不发语音，文字照常。
    #   要恢复"启动即常驻"旧行为：config.json 加 "COSYVOICE_AUTOSTART": true。

    port = int(os.environ.get("PORT", "32123"))
    print(f"[run.py] 启动后端，端口 {port}", flush=True)

    # ★ ASR 预热：后台线程预加载 whisper 模型，通话/接口首次识别不再等 10~30s
    try:
        import threading

        def _prewarm_asr():
            try:
                from backend.asr import _get_whisper_model
                _get_whisper_model()
                print("[run.py] ASR whisper 模型预热完成", flush=True)
                # ★ 2026-09-10：启动自检（onnxruntime / silero VAD / ffmpeg / 模型来源）。
                #   通话"识别不到我说的话"时，一半原因出在这条链路的环境上，
                #   而旧日志只会误报"Whisper 未安装"——自检让问题在启动时就暴露。
                try:
                    from backend.asr import probe_local_asr, format_probe
                    for _ln in format_probe(probe_local_asr()).splitlines():
                        print(f"[run.py] {_ln}", flush=True)
                except Exception as _pe:
                    print(f"[run.py] ASR 自检失败(静默): {_pe}", flush=True)
            except Exception as _e:
                print(f"[run.py] ASR 预热失败(静默): {_e}", flush=True)

        threading.Thread(target=_prewarm_asr, daemon=True).start()
    except Exception as _e:
        print(f"[run.py] ASR 预热启动失败(静默): {_e}", flush=True)

    # ★ 2026-09-12：CosyVoice 启动预热已随惰性化一并移除（原预热会在启动时
    #   加载 0.5B 模型常驻内存）。现在首次需要语音时由 cosyvoice_mgr 拉起并加载。

    # ★ uvicorn 异常保护：任何未捕获异常都落盘 traceback，避免静默退出（用户测着测着后端没了）
    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="info",
            access_log=False,
        )
    except SystemExit:
        raise
    except BaseException as _e:
        import traceback
        traceback.print_exc()
        print(f"[run.py] 后端异常退出: {_e}", flush=True)
        raise
