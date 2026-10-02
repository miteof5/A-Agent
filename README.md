# ActionAgent

**本地运行的自主型 AI Agent**：接收自然语言任务 → 自主规划 → 在电脑上真实执行（Shell / 文件）→ 错误自愈 → 完成目标。核心竞争力 = **电脑操作执行力**。

## 功能特性

- **自主 ReAct 主循环**：手写实现，turn/step 双层循环 + 8 个事件扩展点（业务逻辑全部插件化，核心循环保持"瘦"）
- **工具调用**：Shell 执行（PowerShell、输出 64KB 有界、超时进程树清理）、**文件读取**（file_view 多格式：文本/GBK 回退/PDF/docx/xlsx，magic bytes 自动分派）、**文件写入**（file_write：UTF-8 + 回读校验 + JSON 校验 + replace 精准编辑 + 审批）、**联网**（web_search 搜索线索 + web_fetch 正文精读，自动代理 Clash/v2rayN 故障转移 + 空闲回收）、`ask_user` 人机交互
- **权限沙箱两档**：`on-demand`（按需确认：普通命令放行 + 高危命令弹窗审批）/ `full-access`（全部允许）+ 命令作用解释
- **插件微内核**：EventBus 四模式（emit / bail / parallel / waterfall），BasePlugin + AgentContext，可独立开发、按需加载、可替换
- **事件溯源 + 短期记忆**：SQLite events 全量落库 → 服务重启恢复（interrupted 标记）→ 同会话多轮对话 / 断点续跑
- **RepeatGuard 防死循环**：双通道检测（同工具同参数 / 同结果），soft 温和纠偏喂回 LLM，hard 终止任务
- **便捷切换模型**：`models.txt` 清单（按厂商分组、多模态最后）+ 前端「⚙ 模型」管理视图，运行时热切换（base_url/api_key 不变，只换模型名）；切换前自动校验（额度/存在性），失败自动回滚；切换结果持久化，重启不丢
- **前端（单文件）**：对话式界面（会话列表 / 思考过程折叠 / 审批卡 / 问答卡 / 日志面板 / 模型管理），SSE 流式 token + 状态轮询兜底
- **长期记忆（S5）**：跨会话稳定事实存 SQLite `memory` 表——任务完成后**异步提炼**（价值过滤→LLM 提炼 add/update/skip，去重判断合并进同一次调用）→ 新任务开始时**检索注入** ≤5 条（S5.1：FTS5 关键词检索——标识符/数字/路径/≥3字中文子串命中，bm25 相关优先 + 活跃度兜底/补足；零命中退回活跃度）；宁缺毋滥，允许空提炼；**主题语义可插拔**：分类定义/提炼规则/注入标题收在 `LONG_MEMORY_SCHEMA`（换主题只改配置，机制零改动）
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
| 存储 | SQLite（事件溯源 append-only + 长期记忆 FTS5） |
| LLM | OpenAI 兼容接口（默认阿里云百炼），`models.txt` 清单多模型热切换 |
| 文件解析 | pdfplumber（PDF）/ python-docx（Word）/ openpyxl（Excel），可选依赖缺库降级 |
| 前端 | 单文件 HTML（原生 JS + EventSource） |

## 快速开始

```powershell
# 1. 创建 conda 环境（Python 3.12）
conda create -n ActionAgent python=3.12

# 2. 安装依赖
pip install -r requirements.txt
# 文件解析库（可选，缺库时 PDF/docx/xlsx 读取返回说明文案，不影响其他功能）
pip install pdfplumber python-docx openpyxl

# 3. 配置 LLM（从环境变量读取；也可复制 `.env.example` 为 `.env` 填写，二者等效）
$env:AA_LLM_API_KEY = "sk-xxx"          # 或系统变量 DASHSCOPE_API_KEY
$env:AA_LLM_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 3.5 联网搜索（可选；不配则 web_search 不可用，其余功能不受影响）
$env:AA_SEARCH_API_KEY = "tvly-xxx"     # Tavily API Key（https://tavily.com 注册）

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
- `AA_LONG_MEMORY_INJECT_LIMIT`（5，每次注入的长期记忆条数）· `AA_LONG_MEMORY_MAX_CHARS`（100，每条注入字数上限）· `AA_LONG_MEMORY_SCHEMA`（JSON，可选覆写主题语义：分类/提炼规则/注入标题，缺省键保留默认）
- `AA_TOOL_MAX_BYTES`（65536，file_view/file_write 共用输出/写入上限）
- `AA_SEARCH_API_KEY`（Tavily Key，联网搜索）· `AA_SEARCH_BASE_URL`（搜索 API 地址，默认 `https://api.tavily.com`）
- `AA_SUMM_MODEL` / `AA_SUMM_BASE_URL` / `AA_SUMM_API_KEY`（压缩小模型三件套，**不填自动复用主模型**；换小模型只改 `AA_SUMM_MODEL`）

文件能力说明：
- **读**：`file_view` 按文件头自动识别——文本（UTF-8→GB18030 回退，GBK 不乱码）/ PDF（pdfplumber 逐页）/ Word（python-docx 段落+表格）/ Excel（openpyxl 逐 sheet）；解析库缺失时对应格式返回说明文案
- **写**：`file_write`（path/content/mode=overwrite|append|replace）统一 UTF-8，写入后回读校验，JSON 内容额外校验；`mode=replace` 精准编辑（old_text 定位、唯一匹配才替换，覆盖改/删/插，对齐 Claude Code Edit 工具设计）；`on-demand` 模式下写文件弹窗审批（全量允许直接写入）

联网能力说明（S5.4 + S5.5）：
- **搜**：`web_search`（query + max_results≤10）→ Tavily API，返回标题/链接/来源/摘要（每条摘要截断 200 字），只读、两档权限均放行、回答须带链接引用
- **读**：`web_fetch`（url + max_chars）精读网页正文——标准库 HTMLParser 提取（优先 article/main、跳过 nav/header/footer 噪声），**长正文自动走压缩小模型**（`compress_text`，默认压到 400 字内；短正文直接返回不浪费调用），响应体限 2MB；与 web_search 配合构成"搜索→精读"完整链路
- **代理链路**：`src/proxy_manager.py` + `proxy_config.json`（可提交）——Clash（HTTP 7890）优先、v2rayN（SOCKS5 10808）兜底；**每次联网前自动探测/拉起**（候选未运行即拉起 GUI 并等端口就绪）、真实连通测试（端口通≠可用）、当前失效自动 failover；**不做自动关闭**（拉起会弹出代理工具窗口，由用户手动关闭；被手动关闭后下次联网会自动重新拉起）
- **压缩小模型**：`AA_SUMM_*` 配置，未设置回退主模型；记忆压缩（summarize）与网页正文压缩（compress_text）共用此通道

模型生效优先级：**显式 `AA_LLM_MODEL` > 上次切换持久化（`.current_model`）> 代码默认值 > 清单第一个**。

## 项目结构

```
├── src/
│   ├── kernel/          # 微内核：EventBus / BasePlugin / AgentContext
│   ├── plugins/         # permission / sse_relay / event_store / repeat_guard / tool_registry
│   ├── tools/           # shell_run / file_view（多格式读取）/ file_write（安全写入）/ web_search（联网搜索）/ ask_user
│   ├── proxy_manager.py # S5.4 代理管理层：Clash 优先/v2rayN 兜底/自动拉起/空闲关闭
│   ├── static/          # index.html 单文件前端（含模型管理视图）
│   ├── main.py          # 启动入口
│   ├── api.py           # REST 路由 + SSE 事件流
│   ├── reactor.py       # ReAct 主循环
│   ├── runtime.py       # 后台任务线程管理
│   ├── rebuild.py       # events → LLM messages 重建器（多轮上下文）
│   ├── model_registry.py # models.txt 清单解析 + 当前模型持久化
│   ├── memory.py        # 滚动记忆压缩（三层摘要 + 触发/重建）
│   ├── storage.py       # SQLite（tasks/steps/events + 长期记忆 memory 表）
│   ├── long_memory.py   # S5 长期记忆：价值过滤 + 提炼落库 + 注入块
│   └── llm_client.py    # OpenAI 兼容客户端（流式 + usage + set/verify_model + summarize/compress_text + extract_memories）
├── tests/               # RepeatGuard + 模型切换 + 长期记忆测试（FakeLLM，无需真实 Key）
├── models.txt           # 可用模型清单（按厂商分组、多模态最后；换平台重写此文件）
└── *.md                 # 规划 / 契约 / 交接文档（见下）
```

## 文档索引

| 文档 | 内容 |
|---|---|
| `项目状态-交接文档.md` | 当前进度 / 决策记录 / 踩坑记录（新对话先读它） |
| `接口契约-S0.md` | REST / SSE / 数据模型契约（v1.11，含模型清单/切换/文件能力/replace 精准编辑） |
| `开发预案-全栈实施路线.md` | 实施顺序与方法论（活预案） |
| `项目规划-v0.3-精炼版.md` | 架构定稿（四层模型 / 微内核 / 插件拆分） |
| `S5-硬化上线-任务规划.md` | S5 硬化规划 + 部署方案（含桌面一键启动） |

## 路线图

- ✅ S0–S4：契约先行 → 最小闭环 → 流式 + 微内核 → 前端体验 → 数据与多轮对话（**已完成**）
- ✅ S5.1 / S5.2：RepeatGuard 防死循环 + 日志可观测（**已完成**）
- ✅ 模型切换：models.txt 清单 + 校验回滚 + 持久化 + 前端管理视图（**已完成**）
- ✅ 滚动记忆压缩 + Token 标识：三层短期记忆 + 实时消耗显示（**已完成**）
- ✅ 长期记忆（S5）：提炼→去重→注入 全链路（**已完成**）
- ✅ 检索注入（S5.1）：FTS5 关键词命中 + 活跃度兜底（**已完成**）
- ✅ 文件能力（S5.3）：file_view 多格式读取（PDF/Word/Excel/GBK）+ file_write 安全写入 + replace 精准编辑（**已完成**）
- ✅ 联网能力（S5.4 + S5.5）：web_search（Tavily）+ web_fetch 正文精读（长文走小模型压缩）+ 自动代理（Clash 优先/v2rayN 兜底/拉起/空闲关闭）（**已完成**）
- ⏸ 部署：health 检查 + 一键启动脚本（方案已定，用户暂停）
- 🔜 补强方向：文件快照回滚 → 浏览器自动化（Playwright）→ 鼠标键盘 GUI → 环境感知 → 长期记忆
