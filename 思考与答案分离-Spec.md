# Spec — 思考与答案分离（v1）

> 问题：同一段文本在界面上出现两次，答案先"闪现"再被收起，然后重新出现一遍。
> 决策：**方案 B**（通道分离 + 开启 thinking + reasoning 回传），分四批落地。
> 范围：`llm_client.py` / `reactor.py` / `event_store_plugin.py` / `sse_relay_plugin.py` / `api.py` / `rebuild.py` / `memory.py` / `static/index.html` / 新增 `capabilities.json`
> 状态：**待实施** · 决策见 §3

---

## 1. 问题（有实证，不是推测）

| 层 | 位置 | 现象 |
|---|---|---|
| 后端 | `reactor.py:212-219` | 每次 LLM 调用都逐字发 `agent/token`，**含最后一次** |
| 后端 | `reactor.py:242-243` | 同一次调用结束再发 `agent/thought` 全文 |
| 后端 | `reactor.py:320-334` | 结束再发 `agent/done`，`result` 又是同一段 |
| 前端 | `index.html:1091-1108` | `token` 到达即建/续「思考（第 N 步）」块 |
| 前端 | `index.html:1074-1089` | `thought` 到达按 `lastThoughtKey` 覆盖去重 |
| 前端 | `index.html:1120-1122` | `done` 到达 `addAgentResult` 再渲染一份 |
| 前端 | `index.html:1146-1148` | `onFinished` → `collapseAllProc()` 把刚流出的答案收起 |

**实证**（任务 `be15f2c273174925bbcc699fddb4e50a`）：
- events `id=1034`（`agent/thought`, turn=1, step=4）与 `id=1035`（`agent/done.result`）**文本完全相同，各 1442 字符**。
- step 0~3 只有 `agent/token_usage` + `tools/call`，**没有 thought**（该次调用 `call.text` 为空）→ 实时流过的 token 在重放里整段消失。

---

## 2. 根因：两条原生通道被合成了一条

`llm_client.py:338` 只读 `delta.content`，`:342` 只读 `delta.tool_calls`，**`delta.reasoning_content` 被丢弃**；非流式的 `:296` 同样只取 `msg.content`。

模型端本来是两个字段（DeepSeek 官方流式样例就是开两个 buffer），到这里被合并成一条 `content`；前端再试图把这一条拆成"思考"和"答案"——**重复是这次合并的必然结果，不是渲染代码写错**。

三个下游各自被这个缺失咬到：

| 下游 | 后果 | 位置 |
|---|---|---|
| 界面 | 最后一步的 content 既是"说的话"又是"答案"，渲染两次 | 见 §1 |
| `rebuild.py` | 之所以没重复，是因为 `agent/done` **payload 里没有 `turn`**，被 `list_events` 的 `min_turn` 过滤条件（`json_extract(...) >= ?` 遇 NULL 判假）顺手滤掉 | `storage.py:497-502`、`rebuild.py:89-95/125-127` |
| `memory.py` | 同一条 NULL 规则走向反面：`if turn is None: continue`，**"结论"和"错误"被静默丢弃**，摘要里只剩思考 | `memory.py:99-106` |

也就是说：现在这套"不重复"是靠一个 SQL NULL 副作用偶然维持的。给 `done` 补上 `turn`（一个正常改动）就会当场暴露成两条连续相同的 assistant 消息。

---

## 3. 决策记录

| 决策 | 选项 | 结论 |
|---|---|---|
| D1 改动范围 | A 只做通道分离，暂不开 thinking / B 通道分离 + 开 thinking + 回传 | **B** |
| D2 规则粒度 | 按厂商 / 按模型 / 按「网关 + 模型」三层继承 | 「网关 + 模型」，见 §5.5 |
| D3 落库策略 | 一次性迁移旧数据 / **写新读旧** | 写新读旧，见 §5.4 |
| D4 reasoning 跨轮处理 | A 一律丢弃 / B 摘要成普通文本 / C 只装参数不启用 | **A**，见 §5.6 R2-R3 |
| D5 过程框收起时的 `is-past` 块 | 显示一行摘要 / 完全隐藏 | **显示一行摘要**（可点开），见 §5.7 |
| D6 reasoning 参与滚动记忆压缩输入？ | 参与 / 不参与 | **不参与**（省 token；压缩摘要只取 content 与工具结论） |
| D7 `agent-ui.html` 旧副本 | 保留 / 删除 | **删除**（未跟踪，无独有内容，现行版超它 2800 行） |
| D8 跨轮回传的配置粒度（P3 补充） | 网关级 `when_thinking` / **只给 DeepSeek 组** | **只给 DeepSeek 组**：Qwen 开思考但丢弃不 400，只有 DeepSeek 明确要求回传。网关级会让所有模型都付这份 token，与 D4=A 冲突 |

**D1 的分歧对照（留档）**：

- 我的选择是 A，理由是风险隔离——回传缺失是 400 硬故障，UI 重复是软问题，先修软的、把硬的单独验证。
- 选 B 的成本更优的一点：若最终一定要 B，分两步意味着 events 要兼容两轮（先加 kind、再加 reasoning），历史数据背两遍包袱。一次做到位更省。
- 选 B 才拿得到的东西：`content` 通道装的是"模型对用户说的话"，`reasoning_content` 才是"它怎么想的"。现在界面上的「💭 思考」从来不是思维链。
- **分歧消解在次序上**：B 分四批落地，第一批（P1）恰好是 A 的形态。见 §8。

---

## 4. 目标 / 非目标

**目标**
1. 一段文本只渲染一次，落点由数据决定，不由前端猜。
2. 答案块从创建到终态**零 DOM 移动**；过程块随 step 结束被收走。
3. 实时与重放走同一套渲染逻辑，同一任务两次打开内容一致。
4. 换模型（网关/厂商/模型任一维度）不改代码，只改配置或靠探测。

**非目标**
- 不做流式增量 Markdown（见 §5.7 的明确取舍）。
- 不重做过程框的折叠交互与视觉（沿用 `proc-box`）。
- 不为旧任务回填 reasoning（拿不到）。

---

## 5. 设计

### 5.1 三条不变量（整份 Spec 的约束，实现时逐条对）

- **I1**：一个 step 的 `content` 只渲染一次。落点 = 该 step 结束时**是否带 tool_calls**：带 → 收进过程框；不带 → 留在原地成为答案。
- **I2**：`reasoning` 与 `content` 从创建起就是两个容器、两个 id、两个位置，永不互相覆盖。
- **I3**：`agent/done` 只改状态，**不传输正文**。

### 5.2 解析层（`llm_client.py`）

新增回调与字段：

```python
chat_stream(..., on_delta=None, on_reasoning_delta=None)
LLMCall(text, reasoning, tool_calls, usage, finish_reason)
```

- `delta` 解析顺序：按能力表的 `reasoning_fields` 逐个尝试（默认 `["reasoning_content", "reasoning"]`），命中即走 `on_reasoning_delta`，**不混进 `text_parts`**。
- 顺手修掉已经咬过人的洞：`temperature=0.2` 硬编码在 `:278` 和 `:317`。改为按能力表下发，不支持就不传。kimi-k3 的 400（events `id=1012`）就是这条造成的。
- `verify_model()`（`:116-140`）目前只发 `max_tokens=1`，**不带 temperature、不带 tools、不开 thinking**，所以能通过试水却在真任务里炸。改为真实口径探测，结果写回能力表缓存。

### 5.3 事件模型

```
agent/step_start        {turn, step, model}
agent/reasoning_delta   {turn, step, delta}
agent/content_delta     {turn, step, delta}          ← 原 agent/token 改名
agent/step_end          {turn, step, model, content, reasoning, tool_calls, finish_reason}
agent/done              {turn, step, result}          ← 补 turn/step（当前缺失）
tools/call, tools/result                              ← 不变
```

- `turn` + `step` 本身就是 id，不再引入额外 messageId。
- `agent/thought` **停止发送**（新任务不再产生），但读取侧继续认（§5.4）。
- `model` 必须逐 step 落库：`.current_model` 是全局的且切换即时生效（`api.py:321`），同一个 task 的上下文可能混着不同模型的产物。当前 `reactor.py` / 各 plugin / `tasks` / `events` 表**完全没有 model 字段**，这是 P0 要补的前置项。

### 5.4 落库策略：写新读旧，不做数据迁移

| 事件 | 落库 | 理由 |
|---|---|---|
| `reasoning_delta` / `content_delta` | 否 | 太碎，无重建价值 |
| `step_end` | **是（全量）** | 唯一真相：content + reasoning + tool_calls + model |
| `maybe_compact` 的 `memory/summary` | 是 | 不变 |

`events.payload` 是 JSON，加字段**不需要 ALTER TABLE**。旧任务照旧可打开，读取侧规则：

```
优先认 agent/step_end
没有 step_end 时降级：agent/thought → kind 按"该 step 后继是否紧跟 tools/call"推断
```

这样历史会话一条都不用迁移，也不需要写迁移脚本。

### 5.5 能力表（`capabilities.json`）

三层继承，解析顺序 `models > groups > gateways > 保守默认`。**key 的维度是「网关 + 模型」，不是厂商**：同一个 `deepseek-v4.1-flash` 走官方 API 和走百炼，字段透传行为可以完全不同，决定变量是 `base_url` 后面那层兼容实现。

```json
{
  "gateways": {
    "dashscope": {
      "match": "dashscope.aliyuncs.com",
      "reasoning_fields": ["reasoning_content"],
      "reasoning_param": {"thinking": {"type": "enabled"}},
      "echo_reasoning_with_tools": true,
      "must_echo_cross_turn": false,
      "supports_temperature": true
    }
  },
  "groups": {
    "阿里云·通义千问": {"supports_tools": true},
    "Kimi": {"supports_temperature": false},
    "DeepSeek": {"supports_temperature": true}
  },
  "models": {
    "kimi-k3": {"supports_temperature": false},
    "deepseek-v4.1-flash": {"must_echo_cross_turn": "when_thinking"},
    "qwen3.8-omni-flash-realtime": {"supported": false, "reason": "实时语音，不走 chat.completions + tools"}
  }
}
```

`must_echo_cross_turn` 是三态而非布尔：`false` / `true` / `"when_thinking"`。第三态是必须的——DeepSeek 的跨轮回传要求只在开思考模式时成立，关掉后传反而多花 token。用布尔会把"没开 thinking"的情况也当成要回传。

两条硬约定：

1. **不要把能力塞进 `models.txt`**。它的职责是"给前端拉清单"，混进能力就是第二个真相源——`模型切换-Spec.md` D3（`sheetCurrent` 与 DOM `.active`）刚为这个坑付过账。启动时校验：清单里每个模型都要解析得出能力，解析不出就自动探测一次。
2. `supported: false` **不等于从清单删除**。实时多模态模型留在清单里可见但不可选，比静默消失好诊断。

### 5.6 回传规则（400 的真实边界）

规则的关键分界不是"厂商"，而是**请求里有没有 `tools`**。同一家厂商在两条路径上可以给出相反的规则——DeepSeek 的旧 `deepseek-reasoner`（不支持 function call）**带了 reasoning 就 400**，而它的思考模式 + tools **不带 reasoning 就 400**。所以下表按「轮内 / 跨轮」拆开，两个窗口的规则互相独立。

| 规则 | 窗口 | 内容 |
|---|---|---|
| R1 | 轮内 | 同一 turn 内，若该 step 有 reasoning 且 `echo_reasoning_with_tools=true` 且本次请求带 tools → **必须原样带回** |
| R2 | 跨轮 | **默认丢弃**（D4=A）。重建历史 turn 的 assistant 消息时，reasoning 一律不进上下文 |
| R3 | 跨轮例外 | 网关/模型标 `must_echo_cross_turn=true` 时（DeepSeek 思考模式 + tools：官方要求"后续所有请求完整回传"）→ **跨轮也必须原样带**，否则 400。此时该任务的上下文体积按 reasoning 原文计，必须在日志里可见，不能静默膨胀 |
| R4 | 跨轮 | 被滚动记忆压缩掉的轮次不重建 assistant 消息 → 无需回传，不触发 400 |
| R5 | 轮内 | `echo_reasoning_with_tools=false`（如 OpenAI o 系列）→ **绝不要带**，带了同样可能 400 |
| R6 | — | 能力未知 → 用网关默认值；网关也未知 → 丢弃 reasoning 并记 warning |
| R7 | — | **自愈**：调用失败且错误信息含 `reasoning` 关键词 → 按 R1-R3 的相反方向重试一次，并把结论写回能力表 |

**R2 的代价要说清**：跨轮丢弃后，模型看不到"上一轮为什么这么判断"。这是有意接受的取舍——工具结果与最终结论已经承载了决策的外部痕迹，「为什么」只在同一轮内对继续推理有用。真要跨轮保留，走 D4-B 的摘要化路径（未采纳）。

**R7 是这套设计的兜底**：配置必然过期，所以必须有"错了能自己改回来"的路径。因为规则可以往两个方向错（该带没带、不该带却带了），自愈不能写死"剥离后重试"，只能写"反向重试"。

### 5.7 渲染层（`index.html`）

**块的生命周期**

| 事件 | 动作 |
|---|---|
| `reasoning_delta` | 在 `proc-box` 内建/续 `r-{turn}-{step}` 块。**创建即定位，永不移动** |
| `content_delta` | 建/续 `activeBlock`，插入在 `proc-box` **之前**（即对话流末尾、过程框上方） |
| `step_end` (带 tool_calls) | `activeBlock` 移入 `proc-box`，加 `is-past`，正文折叠成一行摘要 |
| `step_end` (无 tool_calls) | `activeBlock` 原地加 `is-answer`，标记终态，清空引用 |
| `done` | 只收尾：折叠过程框、放开输入。**不再 `addAgentResult`**；仅当不存在 answer 块时兜底渲染 |

关键点：**移动只发生在"带 tool_calls 的 step"，答案块零移动**。用户看到的是"模型说了句话 → 说完去调工具，那句话被收进过程区"；最后一句说完就留在原地。

**Markdown 渲染时机（明确取舍）**

流式期间用纯文本（`escapeHtml`），`step_end` 时一次性 `renderMarkdown`。接受一次重排，因为增量 markdown 每帧重解析整段造成的抖动比一次重排更难看。旧实现也是这个取舍，不改。

**要删掉的脆弱状态**

- `state.lastThoughtKey` / `lastThoughtBlock` 这套"猜是不是同一条"的对齐机制，整体换成 `Map<id, el>`。
- `index.html:1298-1299` 重复的 `replay: false`（对象字面量重复键，说明这块被反复 patch 过，顺手清掉）。
- `index.html:589` 的 types 列表含 `token`，但 token 从不落库重放 → 死分支。

---

## 6. 迁移与兼容

| 项 | 处理 |
|---|---|
| 旧 events（无 step_end / 无 model / 无 kind） | 读取侧降级推断，不迁移。历史会话的"思考"退化为原 `thought` 文本 |
| `agent/done` 补 `turn` 后的连带影响 | **必须同批处理**：补 turn 后 `rebuild.py` 会同时拿到 thought 与 done → 两条相同 assistant 消息。所以 `rebuild.py` 要同步改成"只认 step_end，忽略 done 的正文" |
| `memory.py` 的 `turn is None` 跳过 | 改为按事件类型取"结论"，不再依赖 turn 是否存在 |
| 正在跑的任务 | 改动需重启服务，重启会杀掉运行线程（`interrupt-notice` 兜底已有）。不碰运行中的数据 |
| `.current_model` 是新格式吗 | 否，仍是模型名，零改动 |

---

## 7. 风险登记

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| **400：缺 reasoning 回传** | DeepSeek V4 + tools + 历史 assistant 缺 `reasoning_content` | R1 + R5；**P2 必须带一条真实多轮带工具的回归测试** |
| **400：参数不支持** | kimi-k3 的 temperature（已发生）、o 系列只接受 `temperature=1` | 能力表 + `verify_model` 口径修正 |
| **上下文膨胀** | 每轮 reasoning 都回传，长会话 token 成本上升 | 已有 `memory.py` 压缩；需实测每轮 reasoning 平均字数，定是否要把 reasoning 排除在压缩输入外 |
| **字段名漂移** | 网关升级、换聚合网关 | `reasoning_fields` 列表 + R4/R5 |
| **静默丢失** | 模型给了 reasoning 但解析层没读（当前状态） | P2 的验收项：必须能在 events 里 grep 到 reasoning |

---

## 8. 实施顺序（四批，每批可独立验收）

| 批次 | 内容 | 用户可见变化 |
|---|---|---|
| **P0 前置** | events 补 `model`；`done` 补 `turn`/`step`；新建 `capabilities.json`（仅声明，不接入） | 无 |
| **P1 止血（= A 的形态）** | 渲染层：`activeBlock` + `step_end` 定级的**退化版**（靠 tool_calls 推断）；`done` 只收尾 | **重复立即消失**，答案不再闪现被收起 |
| **P2 通道** | 解析层：reasoning 回调 + 能力表接入 + `verify_model` 口径修正 + `step_end` 落库 | 过程框出现真思维链 |
| **P3 同源** | 重放改读 `step_end`；`rebuild.py` / `memory.py` 适配 | 实时与历史一致；结论不再丢 |

P1 单独上线是安全的：此时 UI 仍走"按 tool_calls 定级"，与 B 的最终形态同一套渲染码，P2/P3 只是给它喂更准的数据。

### 8.1 P1 落地记录（2026-11-01）

**改动面：`src/static/index.html` 单文件，后端零改动。**

| 项 | 实现 | 与 §5.7 的差异 |
|---|---|---|
| 块生命周期 | `state.liveBlock = {key, el, body, turn, step, text, demoted}`，`streamKey(turn, step)` 定位 | 一致 |
| 定级信号 | **不靠 step_end**：`tool_call` 到达 → 当前块定级为"过程"并移入过程区；`done` 到达 → 残留块就地升级为答案 | P1 阶段尚无 step_end，改用两条已有信号。**块生命周期不变，P2 只换触发源** |
| `done` | 有活块 → `finalizeLiveBlock`（原地 markdown 重排）；无活块 → 兜底 `addAgentResult` | 一致 |
| 重放去重 | `stripDupFromProc()`：done 时若过程区**最后一块**与答案完全同文，删掉它 | §5.7 未写。解决重放路径"过程框里也有答案"（P3 之前的临时守卫） |
| 中断/停止 | 残留块收进过程区，不升级为答案（半句话不算答案） | §5.7 未覆盖 |
| 删除的脆弱状态 | 实时路径不再用 `lastThoughtKey` 猜"是不是同一条"；`lastThoughtBlock` 仅服务重放 | 一致 |

**P0 的 `done` 补 `turn`/`step` 本轮故意没做。** 理由：一旦补上，`rebuild.py` 就会同时拿到 `thought` 与 `done` —— 现在之所以没重复，是因为 `done` 没有 `turn`，被 `storage.list_events` 的 `min_turn` 条件（`json_extract(...) >= ?` 遇 NULL 判假）顺手滤掉了。P1 不需要该字段，故与 §6 说的 `rebuild` 改造一起推到 P3，避免先造故障再补。

**验证方式（新增 `tests/render_layer.mjs`）**：把 `index.html` 的最大内联 script 抽出来，在最小 DOM 桩里回放真实 SSE 序列，直接数 DOM 里的答案块个数。5 个场景 14 条断言全过：

| 场景 | 断言 |
|---|---|
| 实时（0 步说话+调工具，1 步直接回答） | 答案块恰 1 个；答案文本正确；过程区无同文块；第 0 步叙述入过程 |
| 重放（只有 thought/tool_call/done） | 答案块恰 1 个；过程区同文块被删 |
| 中断（流到一半被停止） | 半句话不算答案；收进过程区 |
| 多步（前几步无 narration，末步回答） | 答案块恰 1 个；3 个工具块按序入过程 |
| 多轮（turn0 完成后 turn1） | 2 个答案块顺序正确；过程框只给有工具的轮；过程区无答案 |

为什么不用浏览器截图：这里要断言的是"同一段文本只出现一次"这种结构性事实，DOM 计数比截图可靠，也能进 CI。

### 8.2 P2 落地记录（2026-11-01，TDD）

**先写测试，跑红，再写实现**（`tests/test_capabilities.py` 8 条、`tests/test_reasoning_channel.py` 7 条、`tests/render_layer.mjs` 扩到 7 场景 24 断言）。

| 文件 | 改动 |
|---|---|
| `src/capabilities.py`（新） | 四层继承解析 `probe > models > groups > gateways > 保守默认`；`needs_echo()` 三态；`save_probe()` 自愈写回；`audit()` 启动校验 |
| `capabilities.json`（新） | dashscope 网关 + 5 个分组 + 3 条模型例外（kimi-k3 / omni ×2） |
| `src/config.py` | 新增 `capabilities_file`（`AA_CAPABILITIES_FILE`） |
| `src/llm_client.py` | `LLMCall.reasoning`；`on_reasoning_delta` 回调；候选字段逐个试（`reasoning_content`→`reasoning`）；`temperature` 按能力下发；`verify_model` 改为真实口径（带 tools + temperature，`max_tokens` 8） |
| `src/reactor.py` | 新增 `agent/reasoning_delta` / `agent/content_delta` / `agent/step_end`（落库，含 model + kind）|
| `src/plugins/event_store_plugin.py` | `agent/step_end` 落库；逐字 delta 仍不落库 |
| `src/plugins/sse_relay_plugin.py` | 三个新事件映射；旧 `agent/token` 映射保留 |
| `src/api.py` | 重放：有 `step_end` 的任务跳过 `agent/thought`（两者是超集关系，都放会重复） |
| `src/static/index.html` | `🧠 思维链` 独立块（按 turn-step 累加）；`step_end` 驱动重放的过程区；`content_delta` 与 `token` 同义 |

**四件本轮故意没做的事**，都是为了避免"先造故障再补"：

1. **没开 thinking。** 能力表的 `thinking_param` 目前是 `null`（只描述、不启用）。开了就要处理回传，而回传要改 `rebuild.py`（P3）——现在开，第二轮续聊必 400。
2. **`done` 仍没有 `turn`/`step`。** 理由同 §8.1：补了 `rebuild` 立刻拿到两条相同 assistant 消息。
3. **`rebuild.py` / `memory.py` 仍读 `agent/thought`。** `step_end` 已落库但还没被消费（P3 切换）。**这导致"写两份"的现状多持续一批**——`thought` 与 `step_end.content` 内容相同，是冗余存储，不是重复渲染。
4. **`capabilities.json` 只描述、未接入请求参数生成**（`thinking_param` 未下发）。除 `temperature` 与 `verify_model` 外，其余开关都在等 P3。

**P2 的一个可见变化（非 thinking 相关）**：切模型时的试水口径变了。`kimi-k3` 这类不接受 `temperature` 的模型不会再到任务里 400——试水阶段就不带该参数；被能力表标记 `supported:false` 的模型（`qwen3.8-omni-flash-realtime`）在切换时直接被拒，并给出原因。

### 8.3 P3 落地记录（2026-11-01，TDD）

**先写测试，跑红（`Config.__init__() got an unexpected keyword argument 'thinking_enabled'`），再写实现。** `tests/test_p3_same_source.py` 10 条，覆盖本批全部四件事。

| 文件 | 改动 |
|---|---|
| `src/config.py` | 新增 `thinking_enabled`（`AA_THINKING`，默认开）；与能力表分工：能力表说"怎么开"，配置说"开不开" |
| `src/llm_client.py` | `_thinking_kwargs()` 下发 `extra_body`；`LLMCall.reasoning_field` 记录命中的字段名（回传必须同名）；`_create()` 统一入口 + **R7 自愈**（思考参数被拒 → 去掉重试并写回能力表） |
| `src/capabilities.py` | `probe_path_for()`：探测结果与能力表同目录（测试不再污染项目根）；`resolve()` 默认从该目录读 probe |
| `src/reactor.py` | `_assistant_message()` 作为 assistant 消息**唯一出口**（轮内回传 R1 只实现一次）；`done` 补 `turn`/`step`；**停发 `agent/thought`**（与 `step_end` 是超集关系，双写纯冗余） |
| `src/rebuild.py` | 以 `agent/step_end` 为唯一真相；`thought` 降级仅在没有 step_end 的任务上生效；`done` 在该轮已有答案 step_end 时**跳过正文**；跨轮回传按 `needs_echo()` 三态 |
| `src/memory.py` | `_render_turns` 读 `step_end`（`思考/结论` 两行），有 step_end 时不渲染 thought、不渲染 done；reasoning 不进压缩输入（D6） |
| `capabilities.json` | 网关下发 `thinking_param: {enable_thinking: true}`；Kimi/GLM/多模态组置 `null`（没证据就不猜）；`must_echo_cross_turn` 只给 DeepSeek 组（D8） |
| `.env.example` | 补 `AA_THINKING` 说明 |

**三处必须一起上，缺一个就是 400**：开 thinking（`thinking_param`）→ 轮内必须回传（`_assistant_message`）→ 跨轮按能力表回传（`rebuild`）。这也是 P2 没开开关的原因。

**一句话解释"为什么现在才开"**：`agent/done` 补了 `turn` 之后，`rebuild` 的 `min_turn` 过滤不再把它当 NULL 滤掉。如果不同批改 `rebuild` 让它在有 `step_end` 答案时跳过，上下文里立刻出现两条内容相同的 assistant 消息——**之前没重复是因为 `done` 缺 `turn` 的巧合**。测试 `[9/9]` 就是锁这条。

**R7 自愈的方向**：不是"剥离后重试"写死，而是"去掉 `extra_body` 重试"。`thinking_param` 是猜的（社区约定，非标准），猜错不该让任务失败；但**回传缺失**类的 400 不能靠这条兜，那属于 `rebuild` 的规则，只能靠能力表 + D8 的粒度控制。

**已知代价（要盯的）**：开思考后每轮多一次 reasoning 的 token 成本；DeepSeek 组跨轮回传会让上下文按 reasoning 原文计。`rebuild` 里已加 `重建回传 reasoning ... len=N` 日志，膨胀在日志里可见，不是静默的。

**验证**：`tests/test_p3_same_source.py` 10/10；其余 8 个 Python 测试全绿；`tests/render_layer.mjs` 23 断言 0 失败；真实库副本上 `be15f2c2` / `3bb17f1b` 重建无重复、无 reasoning 字段（旧任务没有该字段，符合"写新读旧"）。

---

### 8.4 P4 落地记录（2026-11-01，TDD）：首次发送出现两条用户气泡

**现象**：新会话里发出第一条消息，同一句话上下相邻出现两条用户气泡；切换会话或退出重进后又只剩一条。

**根因（同一条消息被画了两遍，不是重复落库）**：

1. `runTask()` 在 `createTask` 返回后手动 `addUserMsg(content)` —— 乐观插入，为了让用户立刻看到自己说的话（网络往返期间界面不能空着）。
2. 紧接着 `stream.connect(task_id, {...})` 没传 `replay`，默认 `replay=true`。
3. 服务端 `_sse_generator` 从 `events` 表重放，而 `agent/user` 在 `reactor.run()` 第一行就已落库 → 流里第一个事件就是 `event: user`（实测：`{"turn":0,"content":"帮我搞清楚桌面的..."}`）。
4. 前端 `case 'user'` 又 `addUserMsg` 一次 → 第二条。

**为什么换会话/重进就好**：`openSession()` 先 `emptyChat()`（DOM 清空）再纯重放，只有重放这一个来源；`continueChat()` 传了 `replay: false`。只有 `runTask()` 是"本地画一条 + 重放再画一条"的双来源。

**修法**：给 user 气泡一个身份——`turn`。`addUserMsg(text, turn)` 记 `state.userTurns`，同一轮只画一次（`emptyChat` 时清空）。三个调用点统一带 turn：`runTask`（`data.turn ?? 0`）、`continueChat`（`r.data.turn`）、重放 `case 'user'`（`d.turn`）。

选"去重"而不是"`runTask` 直接 `replay: false`"：后者会把 `createTask` 返回 → SSE 建连这段窗口里落库的事件（`agent/user`、极快任务的 `step_end`）永久丢掉，窗口小但确实存在；前者保留重放兜底，只是不许重复画。将来谁再把 replay 打开也不会复发。

**测试**：`tests/render_layer.mjs` 新增场景 8/9/10（乐观插入+重放撞车 → 1 条；纯重放两轮同文 → 2 条，证明去重键是轮次不是文本；续聊单来源 → 1 条）。先跑红（`断言 用户气泡恰 1 条 : FAIL 2`），再改实现转绿，现 27 断言 0 失败。

---

### 8.5 P5 落地记录（2026-11-01）：去掉过程性占位文案

**这两处文字没有功能，只是提示**——但承载它们的是 `emptyChat()`，这个函数有实职：清 DOM + `liveBlock` / `reasoningBlocks` / `lastThoughtBlock` / `lastThoughtKey` / `userTurns` 五个引用归零。**删文案可以，删调用会出 bug**（流式块指向已删除节点；上一轮修的重复气泡立刻复发）。

| 位置 | 改动 |
|---|---|
| `emptyChat(msg)` | `msg` 为空时不插入占位节点，只清 DOM + 重置引用。顺带修掉一个隐患：旧实现无参调用会在界面上显示字面量 `undefined` |
| `openSession()` | 去 `加载会话历史…`（重放走本地 SSE，通常瞬间完成） |
| `runTask()` | 去 `正在提交任务…` / `正在执行…`；两处 `emptyChat` 合并为一处（第二次清的是已经空的 DOM） |

**保留的**：`resetToNewChat()` 的空会话欢迎语（是引导，不是过程状态）、顶部状态 pill（⏳ 排队中 / ⚡ 执行中 / ✓ 完成）、发送按钮的 `执行中…`（按钮自身状态）。状态反馈由这三处承担，对话流里不再有阶段文案。

**测试**：`tests/render_layer.mjs` 场景 11，先跑红（`断言 无占位节点 : FAIL 1`，且 dump 里能看到 `div.chat-empty :: "undefined"`），再转绿。现 30 断言 0 失败。

---

## 9. 验收清单

**P0**
- [ ] 新任务的 `step_end` 事件里能读到 `model`
- [ ] `done` 的 payload 含 `turn`/`step`
- [ ] `rebuild_messages()` 对**有 done 的旧任务**不产生两条相同 assistant 消息

**P1（已完成 2026-11-01，见 §8.1）**
- [x] 实时：最后一步的文本只出现一次；不再有"闪现后收起"
- [x] 历史会话：展开过程框，看不到答案那段（`stripDupFromProc` 兜底）
- [x] 带 4 个工具调用的任务，过程框里 4 个工具块顺序与调用顺序一致
- [x] 答案块在流式全程位置不变（同一节点，只做一次 markdown 重排）
- [x] 回归用例 `tests/render_layer.mjs`，5 场景 14 断言全绿
- [ ] **待人工确认**：浏览器里跑一次真实任务，确认滚动与折叠观感符合预期

**P2（通道层已完成 2026-11-01，见 §8.2；开关未开）**
- [x] `reasoning` 与 `content` 在解析层就是两条通道（测试 [4/7][5/7]）
- [ ] 开 thinking 后 `events` 里能 grep 到 reasoning 内容 —— **结构性条件已在 P3 具备**（`capabilities.json` 网关下发 `enable_thinking`），待真实任务确认
- [x] 过程框里 reasoning 与工具块交替出现，顺序与真实调用一致（渲染用例场景 6）
- [x] 切到 `kimi-k3` 不再 400（temperature 按能力表下发，测试 [6/7]）
- [x] `verify_model` 用真实口径探测，不支持 temperature 时自愈写回能力表（测试 [7/7]）
- [x] `step_end` 落库且带 `model` + `kind`（测试 [1/5][2/5]）
- [ ] **待人工确认**：跑一个真实任务，确认 SSE 里 `content_delta` 到达、界面行为与 P1 一致

**P3（已完成 2026-11-01，见 §8.3）**
- [x] 同一任务两次打开，内容与顺序一致（实时、重放、重建三处共用 `step_end`，不再各写一份）
- [x] `done` 的 payload 含 `turn`/`step`（测试 [9/9]）
- [x] 补了 `turn` 之后 `rebuild_messages()` 仍只产生一条 assistant 答案（同一测试的后半段）
- [x] `rebuild` 认 `step_end`；旧任务自动降级走 `thought`（测试 [5/6][6/6]）
- [x] 跨轮回传按 `must_echo_cross_turn` 三态走：开则带、关则丢（测试 [7/9]）
- [x] 思考参数被网关拒绝时自愈 + 写回能力表（测试 [10/10]）
- [x] `agent/thought` 停发，双写冗余消除（`test_reasoning_channel` [3/5]）
- [x] `tests/` 全绿（9 个 Python + 渲染层 23 断言）
- [ ] **待人工确认**：真实任务续聊第 2 轮不 400（reasoning 回传正确）
- [ ] **待人工确认**：压缩过历史的长会话续聊不 400

**P4（已完成 2026-11-01，见 §8.4）**
- [x] 首轮发送只出现一条用户气泡（乐观插入与重放按 `turn` 去重）
- [x] 切换会话重放两轮同文提问仍是两条（去重键是轮次，不是文本）
- [x] `tests/render_layer.mjs` 27 断言 0 失败
- [ ] **待人工确认**：浏览器强刷后发一条新消息，确认只有一条气泡

**P5（已完成 2026-11-01，见 §8.5）**
- [x] 打开历史会话不再出现"加载会话历史…"
- [x] 首次发送不再出现"正在提交任务…/正在执行…"
- [x] `emptyChat()` 无参调用不产生占位节点（也不再吐出 `undefined`）
- [x] `tests/render_layer.mjs` 30 断言 0 失败

---

## 10. 未决

~~过程框收起时的 `is-past` 块~~ → **已决 D5：显示一行摘要，可点开**。改动点：`index.html` 的 `collapseAllProc`，收起时正文替换为「第 N 步 · 工具名 · 结果状态」的一行摘要，不再显示正文首行。

~~reasoning 是否参与滚动记忆压缩~~ → **已决 D6：不参与**。`memory.py` 的 `_render_turns` 只取 content、工具调用名与工具结果的状态/摘要，reasoning 在压缩输入里完全跳过。副作用：压缩过的轮次无需回传 reasoning，R4 天然成立。

~~`agent-ui.html` 旧副本~~ → **已决 D7：删除**。

**新增未决（下一轮讨论）**

- **工具结果的上下文策略**。实测 `tools/result` 占事件正文体积 **61.1%**，是当前最大的膨胀源；且 `rebuild.py:120` 喂给模型的是完整 `content`，而 `content_summary`（≤500，实测只占 content 的 26.9%）完全没被用上。同时 `_summary()` 是无差别头部截断（`reactor.py:92-97`），对 `shell_run`（错误在尾部）、`web_fetch`（正文在中部）都不合适。方案待定，见下轮讨论。
