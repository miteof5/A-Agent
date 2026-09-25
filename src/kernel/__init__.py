"""微内核（S2.4）：EventBus 四模式 + AgentContext + PluginManager。

对应 v0.3 §4 微内核设计（借鉴 Cordis）的 MVP 切片：
- 5 必须：Context / Service 注册 / Inject 依赖检查 / Lifecycle+Effect / EventBus
  → S2.4 只落 Context + 插件注册 + Lifecycle（setup/teardown）+ EventBus；
    Inject 依赖检查留到出现插件间依赖时再加（当前插件无依赖）
- EventBus 四模式：emit（广播）/ bail（串行拦截）/ parallel（并行）/ waterfall（瀑布流可改数据）
- 3 简化：不做 Fiber 状态机、不做热更新、事件派发按需注册
"""
