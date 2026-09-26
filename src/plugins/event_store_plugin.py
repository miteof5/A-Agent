"""事件溯源落库插件（S4.1）：把内核事件点按顺序写入 events 表，形成短期记忆的持久化形态。

与 SSERelayPlugin 的分工：
- SSERelay：内核事件 → SSE 转发（实时展示，payload 瘦身——tool_result 只发 summary）
- 本插件：内核事件 → SQLite events 表（**全量 payload**——thought/tool_call 完整参数/
  tool_result 完整 content/ask 问答对/done/error/status，token 除外）

落库后即获得"可重建"能力（S4.3 从 events 拼回 LLM 上下文继续执行），
历史重放源也从 steps 升级为 events（SSE 侧见 api.py）。
"""

from __future__ import annotations

import threading

from ..kernel.plugin import AgentContext, BasePlugin

# 落库事件全集（除 token 外全量——token 太碎，落库无重建价值）
_STORE_EVENTS = (
    "agent/user",    # S4.3a：每轮用户输入（重建多轮上下文的关键）
    "agent/status",
    "agent/thought",
    "tools/call",
    "tools/result",
    "agent/ask",
    "agent/done",
    "agent/error",
)


class EventStorePlugin(BasePlugin):
    name = "event_store"

    def __init__(self):
        self._storage = None

    def setup(self, ctx: AgentContext) -> None:
        self._storage = ctx.storage
        for ev in _STORE_EVENTS:
            ctx.events.on(ev, self._make_handler(ev))

    def _make_handler(self, ev_type: str):
        def handler(data):
            tid = (data or {}).get("task_id")
            if not tid or self._storage is None:
                return  # 无 task_id 的事件不落库（防御，与 SSERelay 一致）
            # 2026-09-26 修复：seq 由 DB MAX(seq)+1 生成（原内存 _seqs 服务重启即清零，
            # 导致续聊事件 seq 与历史冲突 → rebuild 消息乱序 → LLM 400 tool_calls 无响应）
            self._storage.create_event_next(tid, ev_type, data)

        return handler
