"""代理管理层（S5.4 联网）：Clash 优先 / v2rayN 兜底 / 自动拉起 / 空闲超时关闭。

设计（2026-10-03 与用户拍板）：
- 候选按序探测：Clash（HTTP 7890）→ v2rayN（SOCKS5 10808）。端口通 ≠ 可用，
  必须做真实 HTTP 连通测试（节点失效也会被识别），第一个测通的即当前代理。
- 拉起：候选未运行 → subprocess 启动其 GUI 程序 → 轮询等端口就绪（≤ startup_wait_sec）。
- 故障转移：当前代理请求失败时 failover() 强制切到下一个候选。
- 关闭策略：只有"agent 自己拉起的"才在空闲超时（默认 5 分钟）后关闭；
  用户手动运行的（启动时端口已在监听）绝不主动关，避免干扰用户正在使用的代理。
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
    "idle_timeout_sec": 300,
    "startup_wait_sec": 15,
}

# 连通性测试目标（轻量，返回 204 即可）
_PROBE_URL = "https://www.google.com/generate_204"


class ProxyManager:
    """代理生命周期与故障转移。

    线程安全：ensure/failover/mark_used/release_idle 可跨线程调用
    （web_search 主线程执行，release_idle 顺带在每次搜索前检查）。
    """

    def __init__(self, config_path: str | None = None):
        self.cfg = _load_config(config_path)
        self._lock = threading.Lock()
        self._launched: dict[str, int] = {}  # name -> 自己拉起的进程 pid（供空闲关闭）
        self._last_used = 0.0
        self._preferred = 0  # 当前优先候选索引；failover 时 +1

    # ---- 对外 API ----
    def ensure_proxy(self) -> str | None:
        """返回当前可用代理 URL（http://... 或 socks5://...）；全部不可用返回 None。"""
        n = len(self.cfg["proxies"])
        if n == 0:
            return None
        for i in range(n):
            idx = (self._preferred + i) % n
            url = self._try_proxy(idx)
            if url:
                self._preferred = idx
                self._mark_used()
                return url
        return None

    def failover(self) -> str | None:
        """当前代理请求失败时强制切到下一个候选并测试。"""
        with self._lock:
            self._preferred = (self._preferred + 1) % len(self.cfg["proxies"])
        return self.ensure_proxy()

    def mark_used(self) -> None:
        self._mark_used()

    def release_idle(self) -> None:
        """空闲超时关闭"自己拉起的"代理；用户手动开的绝不关。"""
        with self._lock:
            if not self._launched:
                return
            idle = time.time() - self._last_used
            timeout = self.cfg.get("idle_timeout_sec", 300)
            if idle < timeout:
                return
            for name, pid in list(self._launched.items()):
                if _pid_alive(pid):
                    _kill_tree(pid)
                    logger.info("代理空闲超时已关闭：%s (pid=%d, 空闲 %.0fs)", name, pid, idle)
                self._launched.pop(name, None)

    # ---- 内部 ----
    def _mark_used(self) -> None:
        with self._lock:
            self._last_used = time.time()

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
        """候选未运行 → 启动其 GUI 程序并轮询等端口就绪；记录 pid 供空闲关闭。"""
        with self._lock:
            if p["name"] in self._launched:
                return
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


def _kill_tree(pid: int) -> None:
    """按 PID 杀整个进程树（agent 自己拉起的 GUI + 内核子进程）。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("关闭代理进程失败 pid=%d：%s", pid, e)
