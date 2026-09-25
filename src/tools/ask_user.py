"""ask_user 工具（S3.3）：Agent 需要用户澄清/补充信息时主动提问。

机制（挂起-响应）：
1. execute 发 agent/ask 事件（SSE 转成 ask 事件推给前端）
2. 任务状态置 waiting（由 TaskRun.await_input 内部完成）
3. 阻塞等待 ctx.human_input.await_input(question)（用户回答或 stop）
4. 用户回答经 POST /task/respond 提交 → 回答作为 tool_result 返回给 LLM，主循环继续

这是 S3.3 人机交互机制的"唯一真实消费者"；S3.4 在此基础上强化
提示词引导与模糊指令场景打磨；S3.5 审批弹窗复用同一等待通道。
"""

from __future__ import annotations

from ..kernel.plugin import AgentContext
from ..models import ToolResult
from .base import BaseTool, ToolSpec

_PROMPT_FRAGMENT = """使用 ask_user 工具的时机（S3.4 强化）：
- 任务有歧义、缺少必要信息、或你无法确定目标时，用 ask_user 提问，不要凭空猜测、不要擅自行动。
- 提问前必须先侦查：列目录、搜索文件名、查看相似项，能自己确认的先自己确认，把问题缩小到"只有用户才能回答"的范围。
- 提问要带上下文并尽量给选项：告诉用户你已查过什么、找到了哪些相似项（列出名字）、需要他确认什么，
  例如"桌面上找到‘截图1.png’和‘截图2.png’，是要删除这两个吗？"——不要只问"请提供文件名"这种开放式问题。
- 一次只问一个问题，用中文提问。"""


class AskUserTool(BaseTool):
    spec = ToolSpec(
        name="ask_user",
        description=(
            "向用户提问以获取澄清或补充信息（如确认目标、选择方案、补充参数）。"
            "仅在先侦查仍无法确定时调用；提问须带已侦查的上下文并给出可选项；"
            "用户回答后你会收到回答并继续任务。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "向用户提出的问题（要具体）"},
            },
            "required": ["question"],
        },
    )
    prompt_fragment = _PROMPT_FRAGMENT

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx

    def execute(self, arguments: dict) -> ToolResult:
        question = (arguments.get("question") or "").strip()
        if not question:
            return ToolResult(ok=False, content="", error="question 必填")
        if self.ctx.human_input is None:
            return ToolResult(
                ok=False,
                content="",
                error="当前没有可用的用户输入通道（任务未挂起或已结束）",
            )

        # 1. 通知前端：有澄清问题（SSE ask 事件）
        #    注意：必须带 task_id（S3.3 修复——SSERelay 按 task_id 路由事件）
        self.ctx.events.emit(
            "agent/ask",
            {
                "type": "clarify",
                "question": question,
                "task_id": self.ctx.human_input.task_id,
            },
        )

        # 2. 阻塞等待用户回答（TaskRun.await_input 内部置 waiting；stop 会中断等待）
        try:
            answer = self.ctx.human_input.await_input(question)
        except InterruptedError:
            return ToolResult(ok=False, content="", error="任务被用户停止，未获得回答")

        # 3. 回答作为工具结果返回，LLM 继续推理
        return ToolResult(ok=True, content=answer or "（用户未提供内容）")
