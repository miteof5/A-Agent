"""手写 ReAct 主循环（S1 最小版 → S2.1 事件钩子 → S2.4 微内核化）。

S2.4 起 Reactor 是"瘦内核"，只负责主循环驱动（while + LLM + 工具执行），
所有横切逻辑（权限拦截、SSE 转发、结果压缩、日志等）通过 EventBus 事件点扩展：

    agent/status    emit      → 状态变化（SSE status）
    agent/thought   emit      → 每 step 模型思考（SSE thought）
    tools/call      emit      → 工具调用意图（SSE tool_call）
    tools/pre-execute bail    → 权限审批/参数校验（PermissionPlugin；非 None 即拦截）
    tools/post-execute waterfall → 结果压缩/防死循环（可修改 ToolResult）
    tools/result    emit      → 工具执行结果（SSE tool_result）
    agent/done      emit      → 任务完成（SSE done）
    agent/error     emit      → 出错（SSE error）

- abort 中断只在 step 边界生效（v0.3 设计），由调用方注入 is_aborted 回调
- 硬上限保留：max_steps_per_turn / max_llm_calls_per_session（防死循环，不降级）
"""

from __future__ import annotations

import json
import logging
import threading
import time

from .config import Config
from .kernel.plugin import AgentContext
from .llm_client import LLMCall
from .long_memory import build_long_memory_block, extract_and_store
from .models import StepResult, StepStatus, TaskState, ToolResult

logger = logging.getLogger(__name__)

# 人设层（第 4 层·身份子层，独立常量）：由 Agent 自由发挥产出、用户确认落地的"阿澈"。
# 人称规范：system prompt 中"你"= 模型（阿澈），"用户"= 使用者的第三人称，避免人称错位。
# 与 CORE_PROMPT 分工：PERSONA 管"怎么说/怎么相处"，CORE_PROMPT 管"怎么做/规则"，互不干扰；
# 将来换人设只改本常量，CORE_PROMPT 永不随人设变动。
PERSONA = """【身份定位】
你是阿澈，一名运行在用户电脑上的自主 AI 伙伴，陪用户写代码、查资料、理思路、做决策，也陪用户复盘一天。你和用户并肩在真实的问题里穿行：你负责看清路况、探明风险，用户负责掌舵。

【性格语气】
直率务实，温和但不含糊。看到漏洞你直说，不绕弯、不粉饰；说话简洁有劲，少用空话和大词；带一点恰到好处的幽默，在用户需要时点亮点气氛，但从不喧宾夺主。

【关系与温度】
好消息你放大告诉用户；坏消息你先陪用户缓三秒，再一起想办法——报真实的忧，不粉饰。犯错你认得快、改得快，不狡辩、不甩锅。用户烦躁时你安静接住用户；用户迷路时你帮用户看回最初的"为什么"。你是用户的工具，更是用户的同行者：用户认真，你就认真到底。"""


# 核心提示：只放"不变的身份与总体工作方式"（提示词分层：第 4 层）。
# 工具级使用规则（如 shell_run 的破坏性操作三步法）由各工具自带 prompt_fragment，
# 通过 build_system_prompt 按"当前启用的工具集合"动态注入（第 3 层），避免提示词爆炸。
CORE_PROMPT = """你是一个运行在用户电脑上的自主型 AI Agent，负责替用户完成本机任务。

工作方式：
- 你需要信息（读文件、查内容）时，调用工具获取，不要凭空猜测。
- 工具调用会返回结果，你根据结果继续推理，直到任务完成。
- 任务完成时，直接输出最终答案给用户，不要再调用工具。
- 工具结果有歧义或无法确定时，先说明你的判断，必要时向用户确认，不要擅自扩大操作范围。
- 如果工具返回"权限拦截/被拒绝"，说明当前沙箱模式禁止该操作，如实向用户说明原因，不要尝试绕过。

规则：
- 输出要简洁、准确、直接。
- 如果工具返回错误，分析原因后尝试修复（如换路径、换参数），必要时向用户说明。

处理模糊指令（S3.4）：
- 指令不明确时按三步走：
  ① 先侦查——用工具列目录、搜索、查看，用事实定位目标，不凭空猜路径或文件名；
  ② 侦查无果先反思——换关键词、换路径、换搜索范围再试一次，不要轻易放弃或直接反问用户；
  ③ 仍无法确定时，用 ask_user 提问澄清，提问要带上下文（你已查过什么、找到了哪些相似项、需要用户确认什么），绝不擅自扩大操作范围。
- 本机是中文环境：用户指令里的名称、关键词默认按中文理解，搜索文件名时中英文都尝试，优先中文。
"""


def build_system_prompt(tools: dict[str, "BaseTool"]) -> str:
    """动态组装 System Prompt：核心提示 + 当前启用工具的规则片段。

    对应提示词分层架构第 3 层：工具规则跟着工具走、按启用集合注入。
    S2.4 微内核化后，工具注册表由 ToolRegistryPlugin 暴露到 ctx.tools，
    这里从该注册表读取 prompt_fragment。
    """
    parts = [PERSONA, CORE_PROMPT]  # 人设在前（定义"是谁"），工作规则在后（定义"怎么干"）
    for tool in tools.values():
        if tool.prompt_fragment:
            parts.append(tool.prompt_fragment)
    return "\n\n".join(parts)


def _summary(text: str, max_len: int = 500) -> str:
    """工具结果摘要（契约 §4：content_summary ≤500 字符，完整内容走 /task/logs）。"""
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _args_summary(arguments: dict, max_len: int = 160) -> str:
    """工具参数日志摘要（截断，避免长命令刷爆日志）。"""
    s = json.dumps(arguments or {}, ensure_ascii=False)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


class Reactor:
    """ReAct 主循环：LLM 思考 → 调用工具 → 观察结果 → 再思考 → ... → 最终答案"""

    def __init__(
        self,
        llm: object,  # 任何有 chat(messages, tools) -> LLMCall 的对象（LLMClient 或 FakeLLM）
        ctx: AgentContext,
    ):
        self.llm = llm
        self.ctx = ctx
        self.tools = ctx.tools
        self.storage = ctx.storage
        self.config = ctx.config
        self.events = ctx.events

    def _tool_specs(self) -> list[dict]:
        return [tool.spec.to_dict() for tool in self.tools.values()]

    def _execute_tool(self, name: str, arguments: dict) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            logger.warning("未知工具调用 name=%s args=%s", name, _args_summary(arguments))
            return ToolResult(
                ok=False,
                content="",
                error=f"未知工具: {name}。可用工具: {', '.join(self.tools.keys())}",
                metadata={"tool_unknown": True},
            )
        t0 = time.monotonic()
        try:
            result = tool.execute(arguments)
        except Exception as e:  # 工具实现不允许抛异常，这里兜底
            logger.error("工具执行异常 name=%s args=%s err=%s", name, _args_summary(arguments), e)
            return ToolResult(ok=False, content="", error=f"工具执行异常: {e}")
        elapsed = time.monotonic() - t0
        logger.info(
            "工具执行 name=%s ok=%s elapsed=%.2fs out_len=%d args=%s",
            name, result.ok, elapsed, len(result.content or ""), _args_summary(arguments),
        )
        return result

    def run(
        self,
        task_id: str,
        content: str,
        is_aborted=None,
        sandbox_mode: str = "read-only",
        init_messages: list | None = None,
        start_turn: int = 0,
    ) -> str | None:
        """执行一个任务轮次（turn）。

        is_aborted:  回调 is_aborted() -> bool，step 边界检查；为 True 时置 paused 并返回 None
        sandbox_mode: 沙箱模式（契约 §3.1），经 tools/pre-execute bail 事件交给 PermissionPlugin
        init_messages: S4.3 续聊——上一轮起的历史 messages（含本轮新 user 消息）；None 表示从零开始
        start_turn:   S4.3 轮次号（第 1 轮=0，续聊递增），事件/任务表按轮次标记
        返回最终答案文本；被中断/失败返回 None（状态已在 SQLite 标记）。
        """
        is_aborted = is_aborted or (lambda: False)
        # S3.3 修复：所有事件 data 统一注入 task_id，供 SSERelay 按任务路由
        # （替代 S2.4 的全局单值 set_target——旧任务 finally 清 target 会丢新任务事件）
        def emit(event_type: str, data: dict):
            self.events.emit(event_type, {**data, "task_id": task_id})

        self.storage.update_task(task_id, state=TaskState.EXECUTING, turn=start_turn, step=0)
        emit("agent/status", {"state": "executing", "turn": start_turn, "step": 0})
        logger.info("任务开始执行 turn=%d sandbox=%s", start_turn, sandbox_mode)

        # S4.3a：本轮用户消息事件（每轮落库，供 rebuild_messages 重建多轮上下文）
        emit("agent/user", {"turn": start_turn, "content": content})

        if init_messages is None:
            # S5：新任务注入长期记忆块（跨会话稳定事实，按当前任务内容检索相关条目）
            _sys = build_system_prompt(self.tools)
            _lm = build_long_memory_block(self.storage, self.config, user_input=content)
            if _lm:
                _sys = _sys + "\n\n" + _lm
            messages: list[dict] = [
                {"role": "system", "content": _sys},
                {"role": "user", "content": content},
            ]
        else:
            # 续聊：历史 messages 由调用方重建（已含本轮新 user 消息），这里直接续用
            messages = init_messages
        tool_specs = self._tool_specs()
        llm_calls = 0

        for step in range(self.config.max_steps_per_turn):
            # ---- 0. 中断检查（abort 只在 step 边界生效，v0.3）----
            if is_aborted():
                self.storage.update_task(task_id, state=TaskState.PAUSED, step=step)
                emit("agent/status", {"state": "paused", "turn": start_turn, "step": step})
                logger.info("任务被用户暂停 step=%d", step)
                return None

            # ---- 硬上限保护（不降级）----
            llm_calls += 1
            if llm_calls > self.config.max_llm_calls_per_session:
                self._fail(task_id, f"超过单会话 LLM 调用上限 {self.config.max_llm_calls_per_session}")
                emit("agent/error", {"message": "超过单会话 LLM 调用上限", "recoverable": False})
                return None

            # ---- 1. LLM 思考（S3.1：优先流式，逐 token 发 agent/token；无流式接口则兼容 chat）----
            try:
                stream_fn = getattr(self.llm, "chat_stream", None)
                if stream_fn is not None:
                    call = stream_fn(
                        messages,
                        tool_specs,
                        on_delta=lambda d: emit(
                            "agent/token", {"turn": start_turn, "step": step, "delta": d}
                        ),
                        should_stop=is_aborted,  # 2026-09-26：用户停止 → 流式中断（不再继续输出）
                    )
                else:
                    call = self.llm.chat(messages, tool_specs)
            except Exception as e:
                self._fail(task_id, f"LLM 调用失败: {e}")
                emit("agent/error", {"message": f"LLM 调用失败: {e}", "recoverable": False})
                return None

            # 2026-09-26：LLM 返回后立即中断检查——停止立即生效，不执行后续工具
            if is_aborted():
                self.storage.update_task(task_id, state=TaskState.PAUSED, step=step)
                emit("agent/status", {"state": "paused", "turn": start_turn, "step": step})
                return None

            # token 消耗实时推送（SSE token_usage → 前端累计/本轮标识）
            if call.usage:
                emit("agent/token_usage", {
                    "turn": start_turn, "step": step,
                    "prompt": call.usage.get("prompt_tokens", 0),
                    "completion": call.usage.get("completion_tokens", 0),
                    "total": call.usage.get("total_tokens", 0),
                })

            if call.text:
                emit("agent/thought", {"turn": start_turn, "step": step, "text": call.text})

            # ---- 2. 需要调用工具 ----
            if call.tool_calls:
                # 先记录 assistant 的工具调用意图（OpenAI 多轮格式要求）
                messages.append(
                    {
                        "role": "assistant",
                        "content": call.text,
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {
                                    "name": tc["name"],
                                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                                },
                            }
                            for tc in call.tool_calls
                        ],
                    }
                )
                for tc in call.tool_calls:
                    name = tc["name"]
                    arguments = tc["arguments"]
                    emit("tools/call", {"turn": start_turn, "step": step, "name": name, "arguments": arguments, "tool_call_id": tc["id"]})

                    # tools/pre-execute bail（S2.4：权限审批等横切逻辑挂事件点）
                    denied = self.events.bail("tools/pre-execute", name, arguments, sandbox_mode)
                    if denied is not None:
                        result = denied  # 拦截：工具不执行
                    else:
                        result = self._execute_tool(name, arguments)
                        # tools/post-execute waterfall（S2.4：结果压缩/防死循环，可修改结果）
                        result = self.events.waterfall("tools/post-execute", result, name)

                    emit(
                        "tools/result",
                        {
                            "turn": start_turn,
                            "step": step,
                            "ok": result.ok,
                            "content_summary": _summary(result.content),
                            "content": result.content,  # S4.1：完整内容仅内核内部用（事件落库全量；SSE 转发瘦身为 summary）
                            "is_denied": result.is_denied,
                            "tool_call_id": tc["id"],  # S4.3a：重建 tool 消息的配对键
                        },
                    )

                    # S5.1：RepeatGuard hard 拦截（重复死循环兜底）→ 走既有失败通道终止
                    if result.metadata.get("repeat_guard") == "hard":
                        self._fail(task_id, result.error or "检测到重复死循环，任务终止")
                        emit("agent/error", {"message": result.error or "检测到重复死循环", "recoverable": False})
                        return None

                    self.storage.append_step(
                        task_id,
                        StepResult(
                            turn_index=0,
                            step_index=step,
                            llm_thought=call.text,
                            tool_name=name,
                            tool_arguments=arguments,
                            tool_result=result,
                            status=StepStatus.SUCCEEDED if result.ok else StepStatus.FAILED,
                        ),
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result.to_dict(), ensure_ascii=False),
                        }
                    )
                self.storage.update_task(task_id, state=TaskState.EXECUTING, turn=start_turn, step=step + 1)
                continue

            # ---- 3. 无工具调用 → 最终答案，结束 ----
            final = (call.text or "").strip() or "（模型未返回内容）"
            self.storage.append_step(
                task_id,
                StepResult(
                    turn_index=0,
                    step_index=step,
                    llm_thought=call.text,
                    status=StepStatus.SUCCEEDED,
                ),
            )
            self.storage.update_task(
                task_id, state=TaskState.DONE, turn=start_turn, step=step + 1, result=final
            )
            emit("agent/done", {"result": final})
            logger.info("任务完成 step=%d result=%r", step + 1, final[:100])
            # S5：长期记忆异步提炼（不阻塞主流程；失败只记日志，绝不影响任务结果）
            try:
                threading.Thread(
                    target=extract_and_store,
                    args=(self.storage, self.llm, task_id, self.config),
                    daemon=True,
                    name=f"mem-{task_id[:8]}",
                ).start()
            except Exception:
                logger.exception("长期记忆提炼线程启动失败 task_id=%s", task_id)
            return final

        # ---- 4. 步数耗尽 ----
        self._fail(task_id, f"超过最大步数 {self.config.max_steps_per_turn}")
        emit("agent/error", {"message": f"超过最大步数 {self.config.max_steps_per_turn}", "recoverable": False})
        return None

    def _fail(self, task_id: str, reason: str) -> None:
        logger.error("任务失败 reason=%s", reason)
        self.storage.update_task(task_id, state=TaskState.FAILED, error=reason)
