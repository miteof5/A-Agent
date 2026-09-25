"""插件层（S2.4）：ToolRegistry / Permission / SSERelay。

对应 v0.3 插件目录 MVP 第一批：工具注册表、PermissionPlugin（依赖 EventBus）、
SSE 转发（把内核事件转成契约 SSE 事件，替代 S2.1 的 emit 回调直传）。
"""
