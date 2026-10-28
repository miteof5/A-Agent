# Spec — 模型切换只保留输入框上拉窗口（v2）

> v1（三现象排查）已确认，本版按你的产品取舍重写：**删掉「⚙ 模型」整页切换入口，只保留输入框上拉窗口**。
> 范围：`src/static/index.html` 单文件。后端 `GET /api/v1/models`、`POST /api/v1/models/switch` **零改动**。
> 状态：**已落地（2026-10-28）** · 决策 1a / 2a / 3a 全部采纳 · 实施与验收记录见 §9

---

## 1. 这个取舍对原来三个根因的影响

| 原根因 | 删掉旧入口后 |
|---|---|
| RC-1 CSS 特异性锁死 `display`（D1 条状物 + D2 收不起） | **不变**，仍必须修 |
| RC-2 / RC-3 两个入口两套切换流程、三处各存一份当前模型 | **被删除动作自然消灭**——`doSwitch()` 和 `refreshModels()` 一起删掉后，全站只剩 `pickSheetModel()` 一条切换路径，不存在漂移的第二方 |
| D3「切换后选中不迁移」 | **仍会以单入口形式残留**：`pickSheetModel()` 成功后只改触发器文字，不重绘列表。删了旧入口不会自动修好它 |

一句话：**你的取舍砍掉了「需要抽象」的那一半问题**，剩下的是"一处显示开关 + 一处状态更新"。

---

## 2. 判断：够不上重构

三条判据，逐条都不成立：

1. **严格定义不成立。** 重构的前提是"外部可观察行为不变"。删掉一个用户可见入口，本身就是行为变更——这是功能收敛（减法），不是重构。
2. **规模与性质不成立。** 净变更约 **-75 行删除 + ~12 行修改**，全部落在同一个文件的四个相邻区域。不引入新抽象、不移动模块边界、不动数据流、不改接口。这是"删除 + 局部修补"。
3. **最关键的一条：删完之后，抽象反而变成过度设计。** v1 方案 C（抽 `ModelSwitcher`，让两个 UI 表面共享一份 state）存在的唯一理由是"两个消费者"。只剩一个消费者时，再套一层 observable/publisher 是给自己加维护成本。

**但有一条逻辑修复不能省**：删掉旧入口后，`sheetCurrent` 与 DOM 上的 `.active` 仍然是两个真相源。必须补上"切换成功后重绘选中"这一环，否则 D3 会以"单入口版"回来——现象一模一样，只是更难归因。

---

## 3. 结论：三件事，不是重构

| 类型 | 内容 | 量级 |
|---|---|---|
| 修 | RC-1：`#modelSheetMenu` 把 `display` 交还给 `.dropdown-menu` / `.open` | CSS 3 行 |
| 补 | `pickSheetModel()` 成功后原地迁移 `.active` / `.ms-check` | JS 5 行 |
| 删 | 旧入口的 HTML + CSS + JS + 监听器 + 死代码 | -75 行左右 |

---

## 4. 落地清单（含行号，当前工作区）

### 4.1 ⚠️ 删除顺序陷阱（必须先看这条）

`index.html:1385` 是 IIFE 顶层的裸调用：

```js
document.getElementById('modelBtn').addEventListener('click', openModelView);
```

如果**只删 HTML 不删这行**，`getElementById` 返回 `null`，`.addEventListener` 抛 `TypeError`，**IIFE 从这里往后整段中断**——`sendBtn`、`taskInput` 快捷键、模式下拉、外点关闭、会话列表加载、触发器初始化全部不执行。表现是"页面能打开但整个对话区是死的"，而且控制台只有一行错，很容易误判成后端问题。

→ **HTML 与 JS 必须在同一次改动里删干净。**

### 4.2 删除

| 位置 | 内容 |
|---|---|
| `:434` | `<button id="modelBtn" class="new-chat" ...>⚙ 模型</button>` |
| `:487-498` | `#modelView` 整块（`model-head` / `modelBackBtn` / `modelCurrent` / `modelList` / `model-tip`）+ **该块之后多出来的一个游离 `</div>`**（`:498`，现存的标签闭合残留，一并清掉） |
| `:291-304` | `.model-view` / `.model-head` / `.model-back` / `.model-current` / `.model-group` / `.model-row` / `.model-tip` 共 14 行 CSS |
| `:625-629` | `el` 映射里的 `modelBtn / modelView / modelBackBtn / modelCurrent / modelList` |
| `:666-714` | `openModelView()` / `closeModelView()` / `refreshModels()` / `doSwitch()`（含 `document.querySelector('.app').style.display` 那套整页替换逻辑） |
| `:1385-1386` | 两个监听器（见 4.1） |
| `:724-728` | `closeAllDropdowns()` —— 本来就没被调用过的死代码，触发器里手工抄了其中两行。顺手收敛成触发器内部的一行 |

删完 `grep -n "modelView\|modelBtn\|openModelView\|doSwitch\|refreshModels"` 应为空。

### 4.3 修改

**M1 · 显隐归位**（`#modelSheetMenu`，`:321-327`）

```css
/* 布局归 ID，显隐归 .dropdown-menu —— 两者不许混。加这行注释防下次再踩 */
#modelSheetMenu {
  min-width: 260px; max-width: 320px; right: 0; left: auto;
  padding: 6px; background: var(--surface-2);
  border: 1px solid var(--border);
  box-shadow: 0 10px 32px rgba(0,0,0,.28), 0 2px 8px rgba(0,0,0,.18);
  /* display 由 .dropdown-menu(/.open) 决定，此处禁止声明 */
}
#modelSheetMenu .ms-list { max-height: calc(60vh - 56px); overflow-y: auto; }
```

（原 `display:flex` + 靠 ID 撑 `max-height` 的做法，是 D1/D2 的直接成因，见 v1 报告 RC-1。）

**M2 · 切换后重绘选中**（`pickSheetModel()`，`:768-778`）

```js
async function pickSheetModel(name) {
  if (name === sheetCurrent) { closeModelSheet(); return; }
  if (sheetPending) return;                       // S2：防连点打两次 1-token 试水
  sheetPending = true;
  try {
    const r = await api.switchModel(name);
    sheetCurrent = r.current;
    setTriggerName(r.current);
    renderSheetSelection();                       // ← 新增：只改 aria/✓/active，不重建 DOM
    closeModelSheet();
    showToast('已切换为 ' + r.current, 'ok');
  } catch (err) {
    showToast('切换失败：' + (err.message || '未知错误'), 'error');
  } finally { sheetPending = false; }
}
```

`renderSheetSelection()` 遍历 `.ms-item`，按 `sheetCurrent` 切换 `.active` 与 `.ms-check` 的存在与否——**只读 `sheetCurrent` 这一个真相源**，不重新请求、不重建节点（顺带保住滚动位置）。

**M3 · 错误原因解包**（`api.switchModel`，`:574`）

后端 400 的体是 `{"detail":{"code","message"}}`（`api.py:320,324`），现在只读 `data.message`，所以全部失败都退化成「切换失败」四个字：

```js
const msg = data.detail?.message || data.detail?.code || data.message;
```

（不改后端，纯前端解包。）

**M4 · 开合状态收口**（触发器，`:1387-1398`）

`aria-expanded` 与 `open` 类在同一处理函数里一次写对，`closeModelSheet()` 反向同步；外点关闭与 `Esc` 复用同一函数（这两处现在共用 `closeModelSheet`，修好 M1 后自动恢复）。

---

## 5. 待拍板（三个，按影响排序）

> **已拍板（2026-10-28）：1a / 2a / 3a** —— 删掉死链、面板维持现状、不做键盘导航。

### 决策 1｜`.ms-config`「✏ 配置自定义模型」怎么办（**必须处理，现在是死链**）

它唯一的动作是 `closeModelSheet(); openModelView();`（`:761`）。旧入口删掉后它指向空。

- **1a 直接删** —— 面板更干净。后果：UI 里再没有任何地方提示"清单在 models.txt、换平台要改 base_url/api_key"，新人接手只能读文档。
- **1b 改成静态页脚** —— 换成不可点的小字提示（如「清单 models.txt · 切换即持久化」）。后果：多 1 行 CSS，省掉一个无效点击目标。
- **1c 做成极简说明面板** —— 点开显示清单路径/换平台步骤。后果：等于新做一个小功能，要额外处理二级面板的 `Esc` 层级，超出本次范围。

### 决策 2｜面板要承担多少信息职责（现在 12 个模型刚好铺得下）

管理视图删掉后，它是唯一能看全清单的地方。

- **2a 维持现状**：厂商分组 + 当前项 ✓。后果：清单涨到 25+ 就要滚很久，`60vh` 里翻。
- **2b 当前模型置顶**：置顶一行 + 下面按厂商分组（原位置不重复）。后果：约 10 行改动，清单变长时体验显著更好，代价是"同一模型出现两处"的语义要解释清楚。
- **2c 加搜索框**：后果：面板内要处理输入焦点与 `Esc` 的优先级（先关搜索还是先关面板），成本明显上升，且 12 个模型的规模下收益接近零。

### 决策 3｜键盘可达做到什么程度

`.ms-item` 是 `div`，无 `role`、无 `tabindex`，只能鼠标点。

- **3a 不做**：保留 `Esc` + 点击。后果：功能完整，键盘用户无法切换模型。
- **3b 最小版**：`role="menu"/"menuitem"` + ↑↓ 移动 + `Enter` 选中 + 打开时焦点进列表。后果：约 20 行，要处理"焦点离开时是否关闭"。

---

## 6. 验收标准

**A. 删除完整性**
1. `grep` 全文件无 `modelView / modelBtn / openModelView / closeModelView / refreshModels / doSwitch`。
2. 控制台**零报错**；`sendBtn`、`Ctrl+Enter`、模式下拉、会话列表、触发器初始化全部正常（即 4.1 那个陷阱没踩中）。
3. 左栏只剩「＋ 新建会话」，无「⚙ 模型」。
4. 页面 DOM 无游离空 div（`:498` 那个残留已清）。

**B. 开合（对应 D1/D2）**
5. 首屏未交互时 `#modelSheetMenu` computed `display === 'none'`，`height === 0`；输入框上方无任何游离条状物。
6. 点触发器 → 面板出现，`aria-expanded="true"`，右缘与触发器对齐；**再点 → 消失**，`aria-expanded="false"`。
7. 外点关闭 / `Esc` 关闭均生效。
8. 与模式下拉互斥（开一个，另一个自动关）。

**C. 切换（对应 D3）**
9. 点非当前模型 → toast 成功；**面板内 ✓ 与高亮当场迁移**；触发器文字同步。
10. 连点同一项 → 只产生一次 `POST`（Network 面板核对）。
11. 刷新页面 → 触发器与面板选中均等于服务端 `current`。
12. 把 `models.txt` 里某个模型名改坏后切换 → toast 显示后端 `detail.message` 的真实原因（而非「切换失败」四个字），选中态保持原样，面板仍可继续操作。

**D. 非回归**
13. `GET /api/v1/models`、`POST /api/v1/models/switch` 请求/响应/错误码不变。
14. `tests/test_model_switch.py` 8 项零改动全过。
15. 昼夜主题切换时面板配色跟随（只用 CSS 变量，无硬编码白底）。

---

## 7. 风险

| 风险 | 应对 |
|---|---|
| 4.1 的 `TypeError` 让整页交互失效 | HTML/JS 同一次改完，改完先看控制台 |
| 删 `.model-view` CSS 时误伤 —— `.model-group` 与面板的 `.ms-group` 名字相近容易看错 | 只删 `.model-*` 前缀那 14 行；`.ms-*` 一行不动 |
| M2 重绘漏掉"当前项"的 `ms-check` 节点增删（只切 class 不够，✓ 需要 create/remove） | 验收 9 用"切过去再切回来"双向验证 |
| 单个 HTML 文件 1465 行、无构建无测试 | 本次不动结构，改完手工跑 §6 全 15 条；要自动化再单独议 |

---

## 8. 不在本次范围

- `verify_model` 的 1-token 试水耗时是否缓存/异步。
- `models.txt` 的折叠、搜索、额度显示（决策 2 的影响再议）。
- 根目录 `agent-ui.html`（8 月旧副本，与现行差 2800 行）——建议删，避免下次照着它改错。

---

## 9. 实施与验收记录（2026-10-28）

**改动规模**：`src/static/index.html` +84 / −129（净 −45）。后端零改动。

| # | 改动 | 位置 |
|---|---|---|
| 1 | `#modelSheetMenu` 去掉 `display:flex`，显隐交还 `.dropdown-menu`；滚动改由 `.ms-list` 自己扛 | CSS |
| 2 | 删除 `.model-*` 14 行 CSS、`#modelView` 整块、左栏「⚙ 模型」、游离 `</div>` | HTML/CSS |
| 3 | 删除 `openModelView` / `closeModelView` / `refreshModels` / `doSwitch` 与两个监听器 | JS |
| 4 | 新增 `openModelSheet()` / `closeModeMenu()`，`closeModelSheet()` 同步 `aria-expanded`；触发器补 `aria-expanded="false"` | JS |
| 5 | 新增 `renderSheetSelection()`：选中态只认 `sheetCurrent`，原地迁移 ✓（不重建 DOM，滚动位置不丢） | JS |
| 6 | `pickSheetModel()` 切换成功后即时重绘 + `sheetPending` 并发守卫 | JS |
| 7 | 新增 `apiErrMsg()`，解开 `{"detail":{"code","message"}}` | JS |
| 8 | **计划外补充**：打开模式下拉时反向收起模型面板，双向互斥才成立 | JS |

**真实浏览器验收**（headless Chrome + CDP 驱动，非人工目测）：

| 检查 | 结果 |
|---|---|
| 首屏菜单 `display` / 高度 | `none` / `0px` —— 条状物消失 |
| 首屏触发器文字 | `qwen3.8-max`（说明 IIFE 跑到底，§4.1 陷阱未踩中） |
| 旧入口 / 旧视图残留 | `#modelBtn` `#modelView` 均不存在 |
| 打开 → 再点收起 | `block` → `none`，`aria` `true` → `false`，高度 483 → 0 |
| 面板位置 | 260×483，底边距触发器 6px，右缘对齐 |
| 双向互斥 | 点模式下拉 → 模型面板关、模式菜单开 |
| 外点 / `Esc` | 均关闭 |
| 切换后选中迁移 | active 与 ✓ 当场迁到 `qwen3.8-max-0902`，触发器同步，仅 1 次 POST |
| 点当前项 | 0 次 POST，仅关闭 |
| 失败路径 | toast 显示「额度不足(测试)」原文，面板保持打开，选中不变 |
| 控制台错误 | 无 |
| 后端回归 `tests/test_model_switch.py` | 8/8 通过（零改动） |

**遗留**：决策 2a / 3a 意味着清单涨到 25+ 时会出现"要滚很久"，键盘仍不可达。两者都在 §5 留了升级路径。

