"""S1 数据模型：Task / StepResult / ToolResult（对应《接口契约-S0.md》§2）。

字段命名与契约一致（snake_case），to_dict() 即 API 返回体。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


def now_iso() -> str:
    """ISO 8601 本地时间（契约要求带时区，如 2026-09-23T10:00:00+0800）"""
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def new_task_id() -> str:
    return uuid.uuid4().hex


class TaskState(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    WAITING = "waiting"   # S3.3：等待用户输入（ask_user 澄清 / 审批弹窗）
    PAUSED = "paused"
    INTERRUPTED = "interrupted"  # S4.2：服务重启后残留任务被标记（executing/waiting/planning）
    DONE = "done"
    FAILED = "failed"


class StepStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class ToolResult:
    """工具统一返回（v0.3 定义落地）"""

    ok: bool
    content: str
    error: str | None = None
    is_denied: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "content": self.content,
            "error": self.error,
            "is_denied": self.is_denied,
            "metadata": self.metadata,
        }


@dataclass
class StepResult:
    """单步执行记录（契约 §2.2）"""

    turn_index: int
    step_index: int
    llm_thought: str | None = None
    tool_name: str | None = None
    tool_arguments: dict | None = None
    tool_result: ToolResult | None = None
    status: StepStatus = StepStatus.SUCCEEDED
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return {
            "turn_index": self.turn_index,
            "step_index": self.step_index,
            "llm_thought": self.llm_thought,
            "tool_name": self.tool_name,
            "tool_arguments": self.tool_arguments,
            "tool_result": self.tool_result.to_dict() if self.tool_result else None,
            "status": self.status.value,
            "timestamp": self.timestamp,
        }


@dataclass
class Task:
    """任务状态（契约 §2.1）"""

    task_id: str
    content: str
    state: TaskState = TaskState.PENDING
    turn: int = 0
    step: int = 0
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    error: str | None = None
    result: str | None = None
    sandbox_mode: str = "on-demand"  # 契约 §3.1：on-demand（按需确认）/ full-access（全部允许）

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content": self.content,
            "state": self.state.value,
            "turn": self.turn,
            "step": self.step,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "result": self.result,
            "sandbox_mode": self.sandbox_mode,
        }
