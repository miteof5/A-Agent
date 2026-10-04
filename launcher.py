"""ActionAgent 一键启动器（S5.6 部署：桌面双击运行）。

双击 `启动ActionAgent.bat` → 本脚本完成：端口检查 → 后台起服务（pythonw，无黑窗口）
→ 健康检查（GET / 直到 200）→ 打开浏览器。黑窗口一闪而过，服务常驻后台。

配套：
    启动ActionAgent.bat   一键启动（推荐双击）
    停止ActionAgent.bat   停止 8000 端口上的服务
    python launcher.py --no-browser   仅启动不开浏览器（测试用）
    python launcher.py --stop         停止服务

环境变量 AA_NO_BROWSER=1 也可跳过开浏览器（给脚本/CI 用）。
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

# Windows 控制台默认 GBK，脚本内中文输出统一 UTF-8（配合 bat 里 chcp 65001）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001 - 非标准控制台时忽略
    pass

ROOT = Path(__file__).resolve().parent
PORT = 8000
URL = f"http://127.0.0.1:{PORT}/"
LOG_DIR = ROOT / "logs"
OUT_LOG = LOG_DIR / "server.out.log"
ERR_LOG = LOG_DIR / "server.err.log"
HEALTH_RETRIES = 25  # 启动后最多等 ~25s


def _port_listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _http_ok(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001 - 探测失败视为未就绪
        return False


def _find_pythonw() -> str:
    """用当前解释器同目录的 pythonw.exe（无控制台窗口）；没有则退回 python.exe。"""
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return exe if os.path.exists(exe) else sys.executable


def _start_service() -> subprocess.Popen:
    LOG_DIR.mkdir(exist_ok=True)
    pythonw = _find_pythonw()
    with open(OUT_LOG, "ab") as out, open(ERR_LOG, "ab") as err:
        return subprocess.Popen(
            [pythonw, "-m", "src.main"],
            cwd=str(ROOT),
            stdout=out,
            stderr=err,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _stop_service() -> tuple[bool, str]:
    """找 8000 端口 LISTENING 进程并 taskkill（与手动关闭一致）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10).stdout
    except Exception as e:  # noqa: BLE001
        return False, f"netstat 执行失败：{e}"
    pids: set[str] = set()
    for line in out.splitlines():
        if f":{PORT}" in line and "LISTENING" in line.upper():
            m = re.search(r"(\d+)\s*$", line.strip())
            if m:
                pids.add(m.group(1))
    if not pids:
        return False, f"8000 端口无监听，服务未在运行"
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, timeout=10)
    return True, f"已停止 {len(pids)} 个进程（pid: {', '.join(sorted(pids))})"


def _open_browser() -> None:
    if os.getenv("AA_NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(URL)
    except Exception:  # noqa: BLE001 - 开浏览器失败不致命
        print(f"无法自动打开浏览器，请手动访问 {URL}")


def main() -> int:
    ap = argparse.ArgumentParser(description="ActionAgent 一键启动/停止")
    ap.add_argument("--no-browser", action="store_true", help="仅启动不开浏览器")
    ap.add_argument("--stop", action="store_true", help="停止服务")
    args = ap.parse_args()

    if args.stop:
        ok, msg = _stop_service()
        print(msg)
        return 0 if ok else 1

    if _port_listening(PORT):
        if _http_ok():
            print(f"服务已在运行（{URL}），直接打开浏览器…")
        else:
            print(f"端口 {PORT} 被其他程序占用，无法启动。请先关闭占用进程后重试。")
            return 1
    else:
        print("启动服务中…")
        _start_service()
        ready = False
        for _ in range(HEALTH_RETRIES):
            time.sleep(1)
            if _http_ok():
                ready = True
                break
        if not ready:
            print("服务启动失败（健康检查超时）。请查看日志：logs\\server.err.log")
            return 1
        print("服务已就绪")

    if not args.no_browser:
        _open_browser()
    return 0


if __name__ == "__main__":
    sys.exit(main())
