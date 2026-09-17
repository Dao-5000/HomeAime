# -*- coding: utf-8 -*-
"""
AI伴侣启动器
功能：
1. 杀掉残留进程（按名字）
2. 强杀占用端口的进程（按端口号，兜底）
3. 启动 AI伴侣.exe
"""
import os
import sys
import time
import subprocess

# ============================================================
# 路径配置（根据你的实际目录调整）
# ============================================================
APP_DIR      = os.path.dirname(os.path.abspath(__file__))
EXE_PATH     = os.path.join(APP_DIR, "AI伴侣.exe")
USER_DATA_DIR = os.path.join(APP_DIR, "user_data")
BACKEND_PORT  = 3000


def kill_process(name: str):
    """按进程名强杀"""
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True, text=True, timeout=5
        )
        if "SUCCESS" in result.stdout or "成功" in result.stdout:
            print(f"[Launcher] 已杀掉进程: {name}")
    except Exception as e:
        print(f"[Launcher] 杀进程 {name} 失败(静默): {e}")


def kill_port(port: int):
    """强杀占用指定端口的进程（兜底）"""
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5
        )
        killed = set()
        for line in result.stdout.splitlines():
            if f":{port}" in line and (
                "LISTENING" in line or "ESTABLISHED" in line
            ):
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit() and pid != "0" and pid not in killed:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", pid],
                        capture_output=True, timeout=5
                    )
                    killed.add(pid)
                    print(f"[Launcher] 已杀掉占用端口 {port} 的进程 PID={pid}")
    except Exception as e:
        print(f"[Launcher] 端口 {port} 清理失败(静默): {e}")


def main():
    os.makedirs(USER_DATA_DIR, exist_ok=True)

    print("[Launcher] 清理残留进程...")

    # 按名字杀（覆盖所有可能的打包名）
    for name in ["AI伴侣.exe", "pc_backend.exe", "run.exe", "backend.exe"]:
        kill_process(name)

    # 按端口杀（兜底，确保端口释放）
    kill_port(BACKEND_PORT)
    kill_port(BACKEND_PORT + 1)  # 备用端口

    print("[Launcher] 等待进程退出...")
    time.sleep(1.5)

    # 启动主程序
    print(f"[Launcher] 启动 {EXE_PATH}")
    try:
        subprocess.Popen(
            [EXE_PATH, f"--user-data-dir={USER_DATA_DIR}"],
            cwd=APP_DIR,
            shell=False
        )
        print("[Launcher] 启动成功 ✅")
    except Exception as e:
        log_path = os.path.join(APP_DIR, "launcher_error.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"启动失败: {e}\n")
            f.write(f"EXE_PATH: {EXE_PATH}\n")
        print(f"[Launcher] 启动失败: {e}，详情见 launcher_error.log")


if __name__ == "__main__":
    main()
