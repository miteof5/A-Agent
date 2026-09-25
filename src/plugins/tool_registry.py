"""工具注册表插件：把工具集合暴露到 ctx.tools（Reactor 从这里读工具与提示词片段）。"""

from __future__ import annotations

from ..kernel.plugin import AgentContext, BasePlugin


class ToolRegistryPlugin(BasePlugin):
    name = "tool_registry"

    def __init__(self, tools: dict):
        self.tools = tools

    def setup(self, ctx: AgentContext) -> None:
        ctx.tools = self.tools
