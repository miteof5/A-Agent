"""EventBus：插件间解耦通信的事件总线（v0.3 §4 / §7B.1 四模式）。

- emit(event, *args)：广播。所有监听器同步执行，忽略返回值（UI 通知、进度）。
- bail(event, *args)：串行拦截。按优先级执行，第一个返回非 None 的监听器短路，
  返回该值（权限审批、参数校验——拦截用）。
- parallel(event, *args)：并行。所有监听器并发执行，返回结果列表（当前无使用者，机制就位）。
- waterfall(event, initial, *args)：瀑布流。每个监听器接收上一个的返回值并可修改，
  返回最终值（上下文注入、结果压缩）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field


@dataclass(order=True)
class _Listener:
    priority: int = field(default=0, compare=True)
    order: int = field(default=0, compare=False)  # 注册序号，同优先级保序
    handler: object = field(default=None, compare=False)


class EventBus:
    def __init__(self):
        self._listeners: dict[str, list[_Listener]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    # ---- 注册 ----

    def on(self, event: str, handler, priority: int = 0) -> None:
        """注册监听器。priority 越大越先执行（同优先级按注册顺序）。"""
        with self._lock:
            self._seq += 1
            self._listeners.setdefault(event, []).append(
                _Listener(priority=priority, order=self._seq, handler=handler)
            )
            self._listeners[event].sort()

    def off(self, event: str, handler) -> None:
        with self._lock:
            if event not in self._listeners:
                return
            self._listeners[event] = [
                x for x in self._listeners[event] if x.handler != handler
            ]

    def _snapshot(self, event: str) -> list[_Listener]:
        with self._lock:
            return list(self._listeners.get(event, []))

    # ---- 四模式派发 ----

    def emit(self, event: str, *args) -> None:
        """广播：所有监听器同步执行，忽略返回值。"""
        for ln in self._snapshot(event):
            ln.handler(*args)

    def bail(self, event: str, *args):
        """串行拦截：第一个返回非 None 就停止，返回该值；全部放行则 None。"""
        for ln in self._snapshot(event):
            value = ln.handler(*args)
            if value is not None:
                return value
        return None

    def parallel(self, event: str, *args) -> list:
        """并行：所有监听器并发执行，返回结果列表（按注册顺序）。"""
        listeners = self._snapshot(event)
        if not listeners:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(listeners))) as pool:
            futures = [pool.submit(ln.handler, *args) for ln in listeners]
            return [f.result() for f in futures]

    def waterfall(self, event: str, initial, *args):
        """瀑布流：每个监听器接收上一个返回值，返回新值传给下一个。"""
        value = initial
        for ln in self._snapshot(event):
            result = ln.handler(value, *args)
            if result is not None:
                value = result
        return value
