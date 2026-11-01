"""S4.3a：从 events 事件流重建 LLM 多轮 messages（短期记忆的"取用"侧）。

events（原料/录像）→ OpenAI messages（成品/上下文）：

    agent/user      → user 消息（每轮用户输入，S4.3a 起落库）
    agent/step_end  → assistant 消息（S6.3 起是**唯一真相**：content + reasoning + model + kind）
    agent/thought   → 旧任务降级路径（任务里没有 step_end 时才用；与 step_end 是超集关系，
                      两者同时存在时只认 step_end，否则内容会重复一遍）
    tools/call      → assistant.tool_calls（含 tool_call_id，S4.3a 起落库）
    tools/result    → tool 消息（按 tool_call_id 配对；缺 id 防御性跳过——旧任务降级）
                      消息体统一走 models.tool_message_content()，与实时循环同源
    agent/done      → assistant 收尾消息（**仅当该 step 没有 step_end 答案时才用**）
    agent/error     → assistant 错误说明（失败任务续聊时告知原因）
    agent/status / agent/ask → 跳过（status 无重建价值；ask 问题已在 tools/call 参数里）

分组规则：一个 LLM 调用 = 1 条 step_end + N 条 tools/call + N 条 tools/result，
组结束时（下一个 user/step_end/thought/done/error）先 flush assistant（含全部 tool_calls），
再 flush tool 消息——保证 OpenAI 多轮格式（assistant.tool_calls 必须在 tool 消息之前）。

回传规则（Spec §5.6）：跨轮默认**丢弃 reasoning**（D4=A）。只有当能力表标了
`must_echo_cross_turn` 且本次开了思考模式时才原样带回——DeepSeek 思考模式 + tools
的场景漏了会直接 400，而别的厂商带了同样可能 400，所以由能力表三态决定，不写死。

返回 None 表示无 events（空任务/旧任务），由调用方决定是否降级。
"""

from __future__ import annotations

import json
import logging

from .capabilities import needs_echo, resolve
from .config import Config
from .long_memory import build_long_memory_block
from .memory import SUMMARY_EVENT, build_memory_block, maybe_compact
from .models import tool_message_content
from .reactor import build_system_prompt

logger = logging.getLogger(__name__)

# 旧任务（无 step_end）的 thought 降级分组用：没有 model 字段时按当前模型判定回传
_LEGACY = "__legacy__"


def rebuild_messages(
    storage, tools: dict, task_id: str, config: Config | None = None, llm=None
) -> list[dict] | None:
    """从 events 重建该任务的 LLM 消息列表（滚动记忆压缩版）。

    结构（分层短期记忆）：
    1. system prompt（工具规则）
    2. 摘要块 system 消息（可选）：第 3 层 LLM 段落摘要 + 第 2 层逐轮 Q&A
    3. 最近 memory_full_turns 轮完整重建（第 1 层，原全量逻辑只作用于窗口内）

    有 llm 时先触发 maybe_compact（轮完成、下一轮开始前压缩）。
    无 events 返回 None。
    """
    config = config or Config()
    if llm is not None:
        maybe_compact(storage, llm, task_id, config)  # 可能新增 memory/summary 事件
    # rebuild 优化（定向读取）：不整段拉会话历史——
    #   - 窗口轮完整事件（最近 N 轮原文，第 1 层）
    #   - 全部轮次的 user/done/error + memory/summary（Q+A 原料 + 摘要，均为小事件）
    #     供 build_memory_block 组装第 2、3 层（done 已预存 question → Q+A 零遍历）
    tr = storage.turn_range(task_id)
    if tr is None:
        return None
    mn, mx = tr
    full_start = max(mn, mx - config.memory_full_turns + 1)
    window_events = storage.list_events(task_id, min_turn=full_start)
    qa_events = storage.list_events(
        task_id, types=["agent/user", "agent/done", "agent/error", SUMMARY_EVENT]
    )
    memory_block = build_memory_block(window_events + qa_events, config)  # 摘要块（第 2、3 层）

    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(tools)}
    ]
    # S5：续聊同样注入长期记忆块（按最近一轮用户输入检索相关条目，在短期记忆块之前）
    last_user = storage.last_user_content(task_id)
    long_block = build_long_memory_block(storage, config, user_input=last_user)
    if long_block:
        messages.append({"role": "system", "content": long_block})
    if memory_block:
        messages.append({"role": "system", "content": memory_block})

    has_step_end = any(ev["type"] == "agent/step_end" for ev in window_events)
    pending_assistant: dict | None = None  # {"content", "tool_calls", "reasoning", "model"}
    pending_tools: list[dict] = []
    answered_turns: set[int] = set()  # 已有 step_end 答案的轮次 → done 不再追加正文
    cap_cache: dict[str, object] = {}

    def cap_of(model: str | None):
        """按 step 自己的 model 解析能力（会话可能中途换过模型，不能只看当前模型）。"""
        name = model or config.llm_model
        if name not in cap_cache:
            cap_cache[name] = resolve(
                name,
                config.llm_base_url,
                table_path=getattr(config, "capabilities_file", None),
                models_file=getattr(config, "models_file", None),
            )
        return cap_cache[name]

    def flush_group():
        """先 assistant（含 tool_calls）后 tool，保证格式合法。"""
        nonlocal pending_assistant, pending_tools
        if pending_assistant is not None:
            content = pending_assistant["content"]
            tool_calls = pending_assistant["tool_calls"]
            if content or tool_calls:  # 空组不产生消息（否则会插入无意义的空 assistant）
                msg: dict = {"role": "assistant", "content": content}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                reasoning = pending_assistant.get("reasoning")
                if reasoning:
                    model = pending_assistant.get("model")
                    cap = cap_of(model)
                    if needs_echo(cap, thinking_enabled=getattr(config, "thinking_enabled", False)):
                        # 字段名与解析时一致（候选表首项）；记进日志以便核对膨胀
                        field = (getattr(cap, "reasoning_fields", None) or ["reasoning_content"])[0]
                        msg[field] = reasoning
                        logger.info(
                            "重建回传 reasoning task_id=%s model=%s field=%s len=%d",
                            task_id, model or _LEGACY, field, len(reasoning),
                        )
                messages.append(msg)
            pending_assistant = None
        if pending_tools:
            messages.extend(pending_tools)
            pending_tools = []

    for ev in window_events:
        t, p = ev["type"], ev["payload"]
        if t == "agent/user":
            flush_group()
            messages.append({"role": "user", "content": p.get("content") or ""})
        elif t == "agent/step_end":
            # S6.3：一次 LLM 调用 = 一组。这是唯一真相，content/reasoning/model 都在里面
            flush_group()
            pending_assistant = {
                "content": p.get("content") or "",
                "tool_calls": [],
                "reasoning": p.get("reasoning") or None,
                "model": p.get("model") or None,
            }
            if p.get("kind") == "answer" and p.get("content"):
                answered_turns.add(p.get("turn"))
        elif t == "agent/thought":
            if has_step_end:
                continue  # 与 step_end 是超集关系，都认就会把同一段内容放两遍
            flush_group()  # 新思考 = 新一组
            pending_assistant = {"content": p.get("text") or "", "tool_calls": [],
                                 "reasoning": None, "model": None}
        elif t == "tools/call":
            tc_id = p.get("tool_call_id")
            if tc_id:
                if pending_assistant is None:
                    pending_assistant = {"content": "", "tool_calls": [],
                                         "reasoning": None, "model": None}
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
                        "content": tool_message_content(
                            ok=p.get("ok"),
                            content=p.get("content"),
                            error=p.get("error"),
                            is_denied=p.get("is_denied", False),
                            metadata=p.get("metadata"),
                        ),
                    }
                )
        elif t == "agent/done":
            flush_group()
            # S6.3：done 现在带 turn 了，而答案正文已经由该 step 的 step_end 给出。
            # 不再判断就等于同一段答案在上下文里出现两次（此前没出现，纯粹是因为
            # done 缺 turn 被 min_turn 当 NULL 滤掉了——靠巧合，不靠设计）。
            if p.get("turn") in answered_turns:
                continue
            messages.append({"role": "assistant", "content": p.get("result") or "（任务完成）"})
        elif t == "agent/error":
            flush_group()
            messages.append({"role": "assistant", "content": "（上次执行出错）" + (p.get("message") or "")})
    flush_group()
    return messages
