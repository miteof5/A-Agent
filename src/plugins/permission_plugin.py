"""权限插件（S2.3 → S2.4 插件化 → S3.5 审批弹窗）：挂 tools/pre-execute bail 事件点。

S2.3 里 Reactor 直接调用 PermissionPolicy；S2.4 起权限检查由 EventBus 驱动——
PermissionPlugin 监听 tools/pre-execute（bail 模式），返回非 None 即拦截（工具不执行）。

S3.5 审批弹窗（四态）：
- read-only：高危命令直接拒绝（DENIED，不弹窗）
- workspace-write：高危命令进入审批——发 agent/ask(type=approval) → 复用 S3.3 的
  human_input 挂起-响应通道阻塞等待用户审批 → 批准则放行执行，拒绝则 DENIED
- danger-full-access：全部放行
审批四态：waiting（等待审批）/ approved（批准执行）/ denied（拒绝跳过）/ aborted（任务停止）
"""

from __future__ import annotations

import logging

from ..kernel.plugin import AgentContext, BasePlugin
from ..models import ToolResult
from ..permissions import ApprovalOutcome, PermissionPolicy, describe_danger

logger = logging.getLogger(__name__)

# 批准关键词（前端审批卡片提交 approve / deny；这里兼容中文回答兜底）
_APPROVE_WORDS = {"approve", "批准", "同意", "允许", "确认", "是", "yes", "y", "执行"}
_DENY_WORDS = {"deny", "拒绝", "不同意", "不允许", "否", "no", "n", "不执行", "跳过"}


class PermissionPlugin(BasePlugin):
    name = "permission"

    def __init__(self, policy: PermissionPolicy | None = None):
        self.policy = policy or PermissionPolicy()
        self.ctx: AgentContext | None = None

    def setup(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        # 权限检查优先于其他 pre-execute 监听器
        ctx.events.on("tools/pre-execute", self._pre_execute, priority=100)

    def teardown(self) -> None:
        self.policy = None
        self.ctx = None

    def _pre_execute(self, tool_name: str, arguments: dict, sandbox_mode: str):
        """bail 拦截：DENIED → 拒绝 ToolResult；NEEDS_APPROVAL → 审批后决定；ALLOWED → None。"""
        outcome, reason = self.policy.check(sandbox_mode, tool_name, arguments)
        if outcome == ApprovalOutcome.ALLOWED:
            return None
        if outcome == ApprovalOutcome.NEEDS_APPROVAL:
            return self._ask_approval(arguments, sandbox_mode)
        logger.warning("权限拒绝 tool=%s sandbox=%s reason=%s", tool_name, sandbox_mode, reason)
        return ToolResult(
            ok=False,
            content="",
            error=reason,
            is_denied=True,
            metadata={"policy": sandbox_mode},
        )

    def _ask_approval(self, arguments: dict, sandbox_mode: str) -> ToolResult | None:
        """S3.5：高危命令审批弹窗。返回 None=放行执行；ToolResult(is_denied)=拒绝跳过。"""
        ctx = self.ctx
        if ctx is None or ctx.human_input is None:
            return ToolResult(
                ok=False,
                content="",
                error="没有可用的用户审批通道（任务未挂起或已结束）",
                is_denied=True,
            )
        command = (arguments.get("command") or "").strip()
        desc = describe_danger(command)  # S3.5：命令人话解释（如"删除文件（目标：xxx.txt）"）
        question = f"检测到高危操作，是否批准执行？\n\n命令作用：{desc}\n命令：{command}\n\n（批准执行 / 拒绝）"
        logger.warning("高危审批等待 task_id=%s desc=%s", ctx.human_input.task_id, desc)
        ctx.events.emit(
            "agent/ask",
            {
                "type": "approval",  # 区别于 ask_user 的 clarify
                "question": question,
                "task_id": ctx.human_input.task_id,
            },
        )
        try:
            answer = ctx.human_input.await_input(question)
        except InterruptedError:
            # 任务被用户停止（aborted 态）
            logger.warning("高危审批被任务停止打断 task_id=%s", ctx.human_input.task_id)
            return ToolResult(ok=False, content="", error="任务被用户停止，未获得审批")
        ans = (answer or "").strip().lower()
        if ans in _APPROVE_WORDS:
            logger.info("高危审批已批准 task_id=%s desc=%s", ctx.human_input.task_id, desc)
            return None  # approved：放行执行
        logger.warning("高危审批已拒绝 task_id=%s desc=%s", ctx.human_input.task_id, desc)
        return ToolResult(
            ok=False,
            content="",
            error=f"用户拒绝执行高危操作：{command[:120]}",
            is_denied=True,
            metadata={"policy": sandbox_mode, "approval": "denied"},
        )
