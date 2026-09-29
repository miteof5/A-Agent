# ActionAgent

**本地运行的自主型 AI Agent**：接收自然语言任务 → 自主规划 → 在电脑上真实执行（Shell / 文件）→ 错误自愈 → 完成目标。核心竞争力 = **电脑操作执行力**。

## 功能特性

- **自主 ReAct 主循环**：手写实现，turn/step 双层循环 + 8 个事件扩展点（业务逻辑全部插件化，核心循环保持"瘦"）
- **工具调用**：Shell 执行（PowerShell、输出 64KB 有界、超时进程树清理）、文件查看、`ask_user` 人机交互
- **权限沙箱两档**：`on-demand`（按需确认：普通命令放行 + 高危命令弹窗审批）/ `full-access`（全部允许）+ 命令作用解释
- **插件微内核**：EventBus 四模式（emit / bail / parallel / waterfall），BasePlugin + AgentContext，可独立开发、按需加载、可替换
- **事件溯源 + 短期记忆**：SQLite events 全量落库 → 服务重启恢复（interrupted 标记）→ 同会话多轮对话 / 断点续跑
- **RepeatGuard 防死循环**：双通道检测（同工具同参数 / 同结果），soft 温和纠偏喂回 LLM，hard 终止任务
- **便捷切换模型**：`models.txt` 清单（按厂商分组、多模态最后）+ 前端「⚙ 模型」管理视图，运行时热切换（base_url/api_key 不变，只换模型名）；切换前自动校验（额度/存在性），失败自动回滚；切换结果持久化，重启不丢
- **前端（单文件）**：对话式界面（会话列表 / 思考过程折叠 / 审批卡 / 问答卡 / 日志面板 / 模型管理），SSE 流式 token + 状态轮询兜底
- **滚动记忆压缩**：三层短期记忆——最近 5 轮完整原文 + 逐轮 Q+A 规则摘要 + 每 10 轮一次 LLM 段落摘要（events 表全量保留原文，压缩只影响上传量）；续聊时自动触发，上下文不随轮次线性膨胀
- **Token 消耗标识**：对话页实时显示「累计消耗 Σ + 本轮上传 ↑」（SSE 推送，即时更新），压缩时提示瘦身效果
- **日志可观测**：标准 logging，`logs/agent.log` 10MB×5 轮转，`task=task-xxxx` 自动串联，uvicorn 日志同文件

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12 |
| Web | FastAPI（端口 8000） |
| 主循环 | 手写 ReAct（不用 LangGraph） |
| 插件 | 自研微内核（借鉴 Cordis / DSH 思想） |
| 存储 | SQLite（事件溯源 append-only） |
| LLM | OpenAI 兼容接口（默认阿里云百炼），`models.txt` 清单多模型热切换 |
| 前端 | 单文件 HTML（原生 JS + EventSource） |

## 快速开始

```powershell
# 1. 创建 conda 环境（Python 3.12）
conda create -n ActionAgent python=3.12

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置 LLM（从环境变量读取）
$env:AA_LLM_API_KEY = "sk-xxx"          # 或系统变量 DASHSCOPE_API_KEY
$env:AA_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 4. 启动
python -m src.main
# 浏览器打开 http://127.0.0.1:8000
```

**模型切换（不用重启）**：浏览器左侧「⚙ 模型」→ 点选即切。模型清单在 `models.txt`（`#` 开头为厂商分组，多模态放最后）。

**换平台**：改 `AA_LLM_BASE_URL` / `AA_LLM_API_KEY` + 重写 `models.txt`，其他代码零改动。

环境变量覆盖（可选）：
- `AA_LLM_MODEL`（显式指定模型，优先级最高）
- `AA_MODELS_FILE`（模型清单文件，默认 `models.txt`）
- `AA_DB_PATH`（SQLite 路径）· `AA_MAX_STEPS`（50）· `AA_MAX_LLM_CALLS`（200）
- `AA_REPEAT_SOFT_LIMIT` / `AA_REPEAT_HARD_LIMIT`（3/3）
- `AA_MEMORY_FULL_TURNS`（5，完整上下文保留轮数）· `AA_MEMORY_SUMMARY_CHUNK`（10，攒满多少条触发总结）· `AA_MEMORY_SUMMARY_CHARS`（400，摘要字数上限）
- `AA_TOOL_MAX_BYTES`（65536）

模型生效优先级：**显式 `AA_LLM_MODEL` > 上次切换持久化（`.current_model`）> 代码默认值 > 清单第一个**。

## 项目结构

```
├── src/
│   ├── kernel/          # 微内核：EventBus / BasePlugin / AgentContext
│   ├── plugins/         # permission / sse_relay / event_store / repeat_guard / tool_registry
│   ├── tools/           # shell_run / file_view / ask_user
│   ├── static/          # index.html 单文件前端（含模型管理视图）
│   ├── main.py          # 启动入口
│   ├── api.py           # REST 路由 + SSE 事件流
│   ├── reactor.py       # ReAct 主循环
│   ├── runtime.py       # 后台任务线程管理
│   ├── rebuild.py       # events → LLM messages 重建器（多轮上下文）
│   ├── model_registry.py # models.txt 清单解析 + 当前模型持久化
│   ├── memory.py        # 滚动记忆压缩（三层摘要 + 触发/重建）
│   ├── storage.py       # SQLite（tasks/steps/events）
│   └── llm_client.py    # OpenAI 兼容客户端（流式 + usage + set/verify_model + summarize）
├── tests/               # RepeatGuard 测试 + 模型切换测试（FakeLLM，无需真实 Key）
├── models.txt           # 可用模型清单（按厂商分组、多模态最后；换平台重写此文件）
└── *.md                 # 规划 / 契约 / 交接文档（见下）
```

## 文档索引

| 文档 | 内容 |
|---|---|
| `项目状态-交接文档.md` | 当前进度 / 决策记录 / 踩坑记录（新对话先读它） |
| `接口契约-S0.md` | REST / SSE / 数据模型契约（v1.5，含模型清单/切换） |
| `开发预案-全栈实施路线.md` | 实施顺序与方法论（活预案） |
| `项目规划-v0.3-精炼版.md` | 架构定稿（四层模型 / 微内核 / 插件拆分） |
| `S5-硬化上线-任务规划.md` | S5 硬化规划 + 部署方案（含桌面一键启动） |

## 路线图

- ✅ S0–S4：契约先行 → 最小闭环 → 流式 + 微内核 → 前端体验 → 数据与多轮对话（**已完成**）
- ✅ S5.1 / S5.2：RepeatGuard 防死循环 + 日志可观测（**已完成**）
- ✅ 模型切换：models.txt 清单 + 校验回滚 + 持久化 + 前端管理视图（**已完成**）
- ✅ 滚动记忆压缩 + Token 标识：三层短期记忆 + 实时消耗显示（**已完成**）
- ⏸ S5.3：部署（health 检查 + 一键启动脚本，方案已定）
- 🔜 补强方向：文件快照回滚 → 浏览器自动化（Playwright）→ 鼠标键盘 GUI → 环境感知 → 长期记忆
