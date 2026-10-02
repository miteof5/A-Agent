"""代理管理层（S5.4 联网）：Clash 优先 / v2rayN 兜底 / 自动拉起 / 故障转移。

设计（2026-10-03 与用户拍板）：
- 候选按序探测：Clash（HTTP 7890）→ v2rayN（SOCKS5 10808）。端口通 ≠ 可用，
  必须做真实 HTTP 连通测试（节点失效也会被识别），第一个测通的即当前代理。
- 拉起：候选未运行 → subprocess 启动其 GUI 程序 → 轮询等端口就绪（≤ startup_wait_sec）。
- 故障转移：当前代理请求失败时 failover() 强制切到下一个候选。
- 关闭策略：**不做自动关闭**（2026-10-03 晚拍板取消"空闲 5 分钟自动关闭"）——
  每次启动本质会弹出代理工具 GUI，用户习惯手动关闭；agent 只负责拉起，不负责关。
  注意：被用户手动关闭后，_launched 里的死 pid 记录会被 _prune_dead/_launch_if_needed
  清理并重新拉起，联网不会因此静默失效。
- 敏感信息：本模块只有端口/路径（无 key），proxy_config.json 可提交 git。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "proxies": [
        {
            "name": "clash",
            "type": "http",
            "host": "127.0.0.1",
            "port": 7890,
            "exe": r"C:\MyApp\Clash.for.Windows-0.20.16-ikuuu\Clash for Windows.exe",
        },
        {
            "name": "v2rayN",
            "type": "socks5",
            "host": "127.0.0.1",
            "port": 10808,
            "exe": r"C:\MyApp\v2rayN\v2rayN.exe",
        },
    ],
    "startup_wait_sec": 15,
}

# 连通性测试目标（轻量，返回 204 即可）
_PROBE_URL = "https://www.google.com/generate_204"


class ProxyManager:
    """代理探测、自动拉起与故障转移（线程安全：ensure/failover 可跨线程调用）。"""

    def __init__(self, config_path: str | None = None):
        self.cfg = _load_config(config_path)
        self._lock = threading.Lock()
        self._launched: dict[str, int] = {}  # name -> 自己拉起的进程 pid（仅用于死记录清理判断）
        self._preferred = 0  # 当前优先候选索引；failover 时 +1

    # ---- 对外 API ----
    def ensure_proxy(self) -> str | None:
        """返回当前可用代理 URL（http://... 或 socks5://...）；全部不可用返回 None。

        每次联网调用都会执行：清理已死拉起记录 → 按序探测/拉起 → 连通测试。
        """
        self._prune_dead()  # 先清掉已退出进程的记录（用户手动关闭等），避免"误以为已拉起"
        n = len(self.cfg["proxies"])
        if n == 0:
            return None
        for i in range(n):
            idx = (self._preferred + i) % n
            url = self._try_proxy(idx)
            if url:
                self._preferred = idx
                return url
        return None

    def failover(self) -> str | None:
        """当前代理请求失败时强制切到下一个候选并测试。"""
        with self._lock:
            self._preferred = (self._preferred + 1) % len(self.cfg["proxies"])
        return self.ensure_proxy()

    # ---- 内部 ----
    def _prune_dead(self) -> None:
        """清掉 _launched 中进程已退出的记录（用户手动关闭代理等场景）。

        关键：不清理的话 ensure_proxy 会以为"之前拉起的还在"，跳过重新拉起，
        导致用户退出代理后联网能力一直失败（2026-10-03 实测踩坑）。
        """
        with self._lock:
            dead = [name for name, pid in self._launched.items() if not _pid_alive(pid)]
            for name in dead:
                logger.info("代理进程已退出，清除拉起记录：%s", name)
                self._launched.pop(name, None)

    def _try_proxy(self, idx: int) -> str | None:
        p = self.cfg["proxies"][idx]
        url = _proxy_url(p)
        if not _port_open(p["host"], p["port"]):
            self._launch_if_needed(idx, p)
        if not _port_open(p["host"], p["port"]):
            logger.warning("代理 %s 端口未就绪：%s:%s", p["name"], p["host"], p["port"])
            return None
        if not _probe(url):
            logger.warning("代理 %s 连通性测试失败（节点可能失效）：%s", p["name"], url)
            return None
        logger.info("当前代理：%s (%s)", p["name"], url)
        return url

    def _launch_if_needed(self, idx: int, p: dict) -> None:
        """候选未运行 → 启动其 GUI 程序并轮询等端口就绪；记录 pid 供死记录清理。

        注意：_launched 里记录的 pid 若已退出（如用户手动关闭代理），必须清除记录
        并重新拉起——否则会误以为"已拉起过"而跳过，联网能力静默失效。
        """
        with self._lock:
            old_pid = self._launched.get(p["name"])
            if old_pid is not None:
                if _pid_alive(old_pid):
                    return  # 之前拉起的进程还活着，等端口就绪即可
                self._launched.pop(p["name"], None)  # 已退出 → 清记录，重新拉起
            exe = p.get("exe", "")
            if not exe or not Path(exe).exists():
                return
            try:
                proc = subprocess.Popen([exe], shell=False)
                self._launched[p["name"]] = proc.pid
                logger.info("自动拉起代理：%s (%s, pid=%d)", p["name"], exe, proc.pid)
            except OSError as e:
                logger.warning("拉起代理 %s 失败：%s", p["name"], e)

        deadline = time.time() + self.cfg.get("startup_wait_sec", 15)
        while time.time() < deadline:
            if _port_open(p["host"], p["port"]):
                return
            time.sleep(0.5)


def _load_config(config_path: str | None = None) -> dict:
    """读项目根 proxy_config.json（可提交，无敏感信息）；缺失/非法 → 内置默认。"""
    p = Path(config_path) if config_path else Path(__file__).resolve().parent.parent / "proxy_config.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            merged = dict(_DEFAULT_CONFIG)
            merged.update({k: v for k, v in data.items() if k in merged})
            return merged
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("proxy_config.json 解析失败，用内置默认：%s", e)
    return dict(_DEFAULT_CONFIG)


def _proxy_url(p: dict) -> str:
    scheme = "socks5" if p.get("type") == "socks5" else "http"
    return f"{scheme}://{p['host']}:{p['port']}"


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe(proxy_url: str) -> bool:
    """真实 HTTP 连通测试：走代理请求轻量 204 目标（端口通≠可用，节点失效也识别）。"""
    try:
        import httpx

        with httpx.Client(proxy=proxy_url, timeout=8.0) as client:
            resp = client.get(_PROBE_URL)
            return resp.status_code in (200, 204)
    except Exception as e:  # noqa: BLE001 - 探测失败一律视为不可用
        logger.debug("代理连通测试异常：%s", e)
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # Windows 下 os.kill(pid, 0) 做存在性检查
        return True
    except OSError:
        return False
