"""工具抽象基类（v0.3 Tool 三要素：name / description / parameters / execute）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import ToolResult


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)  # JSON Schema

    def to_dict(self) -> dict:
        """转成 OpenAI tools 参数格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class BaseTool(ABC):
    spec: ToolSpec

    # 工具使用规则片段（v0.3 微内核前置：插件元数据的一部分）。
    # Reactor 组装 System Prompt 时按"当前启用的工具集合"动态注入，
    # 避免把所有工具的规则全部写死在提示词里（提示词爆炸问题）。
    prompt_fragment: str = ""

    @abstractmethod
    def execute(self, arguments: dict) -> ToolResult:
        """执行工具。所有工具必须返回 ToolResult，不得抛异常给主循环。"""
        raise NotImplementedError
