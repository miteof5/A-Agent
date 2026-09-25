"""插件基类与插件管理器（v0.3 §4：Lifecycle + Service 注册的最小版）。

- BasePlugin：name + setup(ctx)（挂载，注册事件/能力）+ teardown()（卸载清理）
- AgentContext：插件能力聚合入口（ctx.events / ctx.config / ctx.storage / ctx.tools），
  插件通过 ctx 互访，不直接 import 对方模块
- PluginManager：register（按名注册，重复名拒绝）→ start（逐个 setup，失败回滚）
  → collect_tools（汇总所有插件暴露的工具注册表）→ stop（逆序 teardown）
"""

from __future__ import annotations

import threading

from .eventbus import EventBus


class BasePlugin:
    """插件基类。子类必须定义 name 并可选实现 setup / teardown。"""

    name: str = ""

    def setup(self, ctx: "AgentContext") -> None:
        """挂载：注册事件监听、暴露能力到 ctx。"""

    def teardown(self) -> None:
        """卸载：清理副作用。"""


class AgentContext:
    """能力聚合入口：所有插件共享一个 ctx。"""

    def __init__(self, config=None, storage=None):
        self.config = config
        self.storage = storage
        self.events = EventBus()
        self.plugins = PluginManager()
        self.tools: dict = {}  # 工具注册表（ToolRegistryPlugin 挂载）

    def start(self) -> None:
        self.plugins.start(self)

    def stop(self) -> None:
        self.plugins.stop()


class PluginManager:
    def __init__(self):
        self._plugins: dict[str, BasePlugin] = {}
        self._started: list[BasePlugin] = []
        self._lock = threading.Lock()

    def register(self, plugin: BasePlugin) -> None:
        if not plugin.name:
            raise ValueError("插件必须定义 name")
        with self._lock:
            if plugin.name in self._plugins:
                raise ValueError(f"插件重复注册: {plugin.name}")
            self._plugins[plugin.name] = plugin

    def start(self, ctx: AgentContext) -> None:
        """按注册顺序 setup；任一失败则回滚已启动的插件。"""
        with self._lock:
            for plugin in self._plugins.values():
                try:
                    plugin.setup(ctx)
                except Exception:
                    # 回滚：逆序 teardown 已 setup 的插件，再抛出
                    for done in reversed(self._started):
                        try:
                            done.teardown()
                        except Exception:
                            pass
                    raise
                self._started.append(plugin)

    def stop(self) -> None:
        with self._lock:
            for plugin in reversed(self._started):
                try:
                    plugin.teardown()
                except Exception:
                    pass
            self._started.clear()

    def get(self, name: str) -> BasePlugin | None:
        with self._lock:
            return self._plugins.get(name)

    def collect_tools(self) -> dict:
        """汇总所有插件暴露的工具（plugin.tools: dict[name, BaseTool]）。"""
        tools: dict = {}
        with self._lock:
            for plugin in self._plugins.values():
                plugin_tools = getattr(plugin, "tools", None)
                if plugin_tools:
                    tools.update(plugin_tools)
        return tools
