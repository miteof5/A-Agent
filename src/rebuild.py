"""S4.3a：从 events 事件流重建 LLM 多轮 messages（短期记忆的"取用"侧）。

events（原料/录像）→ OpenAI messages（成品/上下文）：

    agent/user      → user 消息（每轮用户输入，S4.3a 起落库）
    agent/thought   → assistant 文本（与同组 tools/call 合并为带 tool_calls 的 assistant 消息）
    tools/call      → assistant.tool_calls（含 tool_call_id，S4.3a 起落库）
    tools/result    → tool 消息（按 tool_call_id 配对；缺 id 防御性跳过——旧任务降级）
    agent/done      → assistant 收尾消息（最终答案，让下一轮知道上一轮结论）
    agent/error     → assistant 错误说明（失败任务续聊时告知原因）
    agent/status / agent/ask → 跳过（status 无重建价值；ask 问题已在 tools/call 参数里）

分组规则：一个 LLM 调用 = 1 条 thought + N 条 tools/call + N 条 tools/result，
组结束时（下一个 user/thought/done/error）先 flush assistant（含全部 tool_calls），
再 flush tool 消息——保证 OpenAI 多轮格式（assistant.tool_calls 必须在 tool 消息之前）。

返回 None 表示无 events（空任务/旧任务），由调用方决定是否降级。
"""

from __future__ import annotations

import json

from .reactor import build_system_prompt


def rebuild_messages(storage, tools: dict, task_id: str) -> list[dict] | None:
    """从 events 重建该任务的完整 LLM 消息列表。无 events 返回 None。"""
    events = storage.list_events(task_id)
    if not events:
        return None

    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(tools)}
    ]
    pending_assistant: dict | None = None  # {"content": str, "tool_calls": [...]}
    pending_tools: list[dict] = []

    def flush_group():
        """先 assistant（含 tool_calls）后 tool，保证格式合法。"""
        nonlocal pending_assistant, pending_tools
        if pending_assistant is not None:
            msg: dict = {"role": "assistant", "content": pending_assistant["content"]}
            if pending_assistant["tool_calls"]:
                msg["tool_calls"] = pending_assistant["tool_calls"]
            messages.append(msg)
            pending_assistant = None
        if pending_tools:
            messages.extend(pending_tools)
            pending_tools = []

    for ev in events:
        t, p = ev["type"], ev["payload"]
        if t == "agent/user":
            flush_group()
            messages.append({"role": "user", "content": p.get("content") or ""})
        elif t == "agent/thought":
            flush_group()  # 新思考 = 新一组
            pending_assistant = {"content": p.get("text") or "", "tool_calls": []}
        elif t == "tools/call":
            tc_id = p.get("tool_call_id")
            if tc_id:
                if pending_assistant is None:
                    pending_assistant = {"content": "", "tool_calls": []}
                pending_assistant["tool_calls"].append(
                    {
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": p.get("name") or "",
                            "arguments": json.dumps(p.get("arguments") or {}, ensure_ascii=False),
                        },
                    }
                )
        elif t == "tools/result":
            tc_id = p.get("tool_call_id")
            if tc_id:
                pending_tools.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(
                            {
                                "ok": p.get("ok"),
                                "content": p.get("content") or "",
                                "is_denied": p.get("is_denied", False),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
        elif t == "agent/done":
            flush_group()
            messages.append({"role": "assistant", "content": p.get("result") or "（任务完成）"})
        elif t == "agent/error":
            flush_group()
            messages.append({"role": "assistant", "content": "（上次执行出错）" + (p.get("message") or "")})
    flush_group()
    return messages
