"""SSE 转发插件：把内核事件（agent/*、tools/*）转成契约 SSE 事件，推给对应任务的 TaskRun。

S3.3 修复：按 task_id 路由（替代 S2.4 的全局单值 target）。
旧实现 set_target/clear_target 是全局单值，任务 A 被 stop 后其线程 finally 的
clear_target 会清掉任务 B 刚 set 的 target → B 的所有事件被丢弃（SSE 卡住）。
现在事件 data 带 task_id（Reactor 统一注入），本插件按 {task_id: publish} 字典转发。
"""

from __future__ import annotations

import threading

from ..kernel.plugin import AgentContext, BasePlugin

# 内核事件 → SSE 事件名（契约 §4）
_EVENT_MAP = {
    "agent/status": "status",
    "agent/token": "token",  # S3.1：LLM 流式 token 逐字输出
    "agent/thought": "thought",
    "tools/call": "tool_call",
    "tools/result": "tool_result",
    "agent/done": "done",
    "agent/error": "error",
    "agent/ask": "ask",  # S3.3：等待用户输入（澄清/审批）
    "agent/token_usage": "token_usage",  # 记忆/token 标识：每次 LLM 调用的 token 消耗
}


class SSERelayPlugin(BasePlugin):
    name = "sse_relay"

    def __init__(self):
        self._targets: dict[str, object] = {}  # task_id -> TaskRun.publish
        self._lock = threading.Lock()

    def set_target(self, task_id: str, publish) -> None:
        """绑定某任务的事件通道（TaskManager.start 时调用）。"""
        with self._lock:
            self._targets[task_id] = publish

    def clear_target(self, task_id: str) -> None:
        with self._lock:
            self._targets.pop(task_id, None)

    def setup(self, ctx: AgentContext) -> None:
        for kernel_event, sse_event in _EVENT_MAP.items():
            ctx.events.on(kernel_event, self._make_handler(sse_event))

    def _make_handler(self, sse_event: str):
        def handler(data):
            tid = (data or {}).get("task_id")
            if not tid:
                return  # 无 task_id 的事件不转发（防御）
            with self._lock:
                target = self._targets.get(tid)
            if target is None:
                return
            if sse_event == "tool_result":
                # S4.1：SSE 只发契约字段（content_summary ≤500），完整 content 走 events 表/日志接口
                slim = {
                    k: data[k]
                    for k in ("turn", "step", "ok", "content_summary", "is_denied", "task_id")
                    if k in data
                }
                target(sse_event, slim)
            else:
                target(sse_event, data)
        return handler
