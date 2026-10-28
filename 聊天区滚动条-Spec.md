# Spec — 聊天区滚动条视觉改造

> 范围：`src/static/index.html` 的 CSS 段（本次仅 CSS，未动 JS）
> 后端 / 接口 / 数据 零改动
> 状态：**已落地** —— 决策 1b / 2a / 3a / 4a，净 +24 行（§10 有实测验收数据）

---

## 1. 问题

截图里聊天流右侧那条滚动条是**浏览器默认样式**（Windows 亮色系统滚动条），落在深色界面上像贴了一条白胶带。三个具体毛病：

1. **太宽**：实测本机默认宽 **15px**，占掉内容区 15px 且视觉存在感极强。
2. **配色不跟主题**：轨道是系统亮灰，页面主题靠 CSS 变量在昼夜之间连续插值，滚动条完全不受控。
3. **离窗口边缘零内缩**：`.chat-flow` 在这个布局下总是顶到视口右缘（`margin-left:240` + 可用宽度 < `max-width:996`），滑块直接贴着窗口边，看起来不像"组件的一部分"，而像"浏览器的零件"。

---

## 2. 现状测绘

页面共 6 个会滚动的容器，目前只有 1 个做过定制：

| # | 容器 | 行号 | 滚动方向 | 现状 |
|---|---|---|---|---|
| 1 | `.chat-flow` 聊天流 | 96 | 纵向 | 原生 15px（截图里那条） |
| 2 | `.sess-list` 会话列表 | 44 | 纵向 | 原生 15px |
| 3 | `#taskInput` 输入框 | 222 | 纵向（超 240px 后） | 原生 15px |
| 4 | `.raw-box` 工具原文框 | 174 | 纵向（超 200px 后） | 原生 15px |
| 5 | `.md-body pre` 代码块 | 128 | 横向 | 原生 15px |
| 6 | `#modelSheetMenu .ms-list` 模型面板 | 316-320 | 纵向 | **已定制**：6px / 透明轨道 / `rgba(128,128,128,.35)` 圆角 3px / hover `.55` |

第 6 项那 4 行就是现成的目标样式，只是没推广。**本次改造本质是"把已有的一套样式抽成全局规则"**，而不是新造一套。

---

## 3. 关键约束（本机实测，Chrome 156）

用 headless Chrome 直接量 `offsetWidth - clientWidth`，结论如下：

| 写法 | 实测滚动条宽度 |
|---|---|
| 什么都不写（原生） | **15px** |
| 只写 `::-webkit-scrollbar { width:6px }` | **6px** |
| 只写 `scrollbar-width: thin; scrollbar-color: ...` | **10px** |
| 两套同时写 | **10px** ← 标准属性赢，webkit 伪元素被整段忽略 |

**所以两套不能混用。**这是本 spec 最容易踩的坑：如果照着"css scrollbar 美化"的博客把标准属性和伪元素一起写上，得到的是 10px 方块滑块，比现在还难看。

本机特性支持（`CSS.supports` 实测）：

- `selector(::-webkit-scrollbar)` → **true**（所以 `@supports not selector(...)` 这段只会命中 Firefox，不会污染 Chrome）
- `scrollbar-gutter: stable` → **true**
- `scrollbar-color` → **true**

**采用策略**：主体用 `::-webkit-scrollbar`（能圆角、能 hover、能透明轨道，Chrome/Edge/Safari 全覆盖）；Firefox 用 `@supports not selector(::-webkit-scrollbar)` 包一层标准属性兜底。两套互不干扰。

---

## 4. 目标

- 滚动条从"系统零件"变成"界面的一部分"：细、半透明、圆角、跟主题走。
- 六个容器**长相一致**，不再一处精致一处原生。
- 不牺牲可用性：任何时候都能看出"还有内容没读完"、能判断当前位置。
- 不引发布局跳动。

---

## 5. 决策记录（四个，均已拍板）

**选定：1b（10px 带 2px 内缩）· 2a（常驻）· 3a（`scrollbar-gutter: stable`）· 4a（只改聊天区）**

### 决策 1｜滑块形态与配色

| 选项 | 形态 | 代价 |
|---|---|---|
| **1a** | **6px 贴边**：宽 6px、`rgba(128,128,128,.32)`、圆角 3px、轨道透明、hover `.55`。与模型面板现状**完全一致** | 占地最小（内容只少 6px）；但滑块紧贴视口右缘，仍有"贴在窗口边框上"的观感 |
| **1b** | **10px 带 2px 内缩**：`::-webkit-scrollbar{width:10px}` + thumb `border:2px solid transparent; background-clip:content-box`，视觉可见 6px、两侧各留 2px 呼吸 | 观感最精致（滑块浮在内容外沿、不贴边）；代价是**多占 4px 布局**，且模型面板那处要跟着改宽度，否则两处粗细不一 |
| **1c** | **8px 折中**：宽 8px 贴边、圆角 4px | 介于两者之间；但既没拿到内缩的精致感，又比 1a 多占 2px |

配色三选同用一套：静止 `rgba(128,128,128,.32)`、hover `.55`、轨道透明。
**是否新增 `--scroll-thumb` 主题变量**（让滑块跟着昼夜插值走，DAY/NIGHT/TWO_PHASE 三处各加两行）：
- 不加：半透明灰在亮/暗两套底色上都能成立，零成本，与模型面板一致。
- 加：暗色下可以更暗、亮色下更淡，颜色随 30s 一次的主题插值渐变；多 6 行代码 + 2 个主题键。

### 决策 2｜显隐策略

| 选项 | 行为 | 代价 |
|---|---|---|
| **2a 常驻** | 一直可见（半透明） | 最好判断"还剩多少"；但静止时也在，视觉噪音最大 |
| **2b 悬停显形** | 鼠标不在聊天区时 thumb 透明，移入才淡入（纯 CSS `.chat-flow:hover::-webkit-scrollbar-thumb`） | 界面最干净，接近 macOS 手感；但**流式输出时你不动鼠标，就看不见进度条** |
| **2c 滚动时显形** | 滚动才淡入，停 1.2s 后淡出（需 ~10 行 JS 加 class） | 最接近原生 macOS；代价是引入 JS 状态 + 定时器，且键盘/程序滚动也要一并监听 |

### 决策 3｜是否常占位

现在滚动条出现/消失会让内容区宽度在 15px（改后 6px）之间跳。流式输出时"消息刚好撑出滚动条"的瞬间，整片气泡会横向抖一下。

- **3a 开 `scrollbar-gutter: stable`**：永远预留 6px，内容不再跳。代价：没滚动条时右侧也空出 6px（肉眼几乎无感），气泡右缘固定内缩 6px。
- **3b 保持动态**：不预留，观感更"满"。代价：流式输出时会看到横向跳动，改窄后幅度从 15px 降到 6px 但**依然存在**。

### 决策 4｜覆盖范围

- **4a 只改聊天区**（`.chat-flow`）：精准解决截图问题，改动最小。代价：侧栏会话列表、输入框、代码块仍是原生样式，同屏并列时反差明显。
- **4b 全站统一**（6 个容器全改）：观感一致，且顺手把模型面板那 4 行重复规则一起收敛。代价：面更大，要看一眼代码块横向滚动条（需要额外设 `height`）和 `.raw-box` 深底上的对比度。

---

## 6. 参数规格（已按 1b/4a 落地）

因为选了 **4a（只改聊天区）**，规则全部挂在 `.chat-flow` 前缀下，模型面板自己那 4 行保持不动——不新增全局规则，也就不存在"全局规则覆盖模型面板"的风险。实际写入 `src/static/index.html`（紧接 `.chat-flow` 定义之后）：

```css
.chat-flow { scrollbar-gutter: stable; }          /* 3a */
.chat-flow::-webkit-scrollbar { width: 10px; }    /* 1b：轨道 10px */
.chat-flow::-webkit-scrollbar-track { background: transparent; }
.chat-flow::-webkit-scrollbar-thumb {
  background-color: rgba(128,128,128,.32);
  border: 2px solid transparent;                  /* 内缩靠透明确实边界 */
  background-clip: content-box;                   /* 把底色裁到 6px 内容盒 */
  border-radius: 999px;
}
.chat-flow::-webkit-scrollbar-thumb:hover { background-color: rgba(128,128,128,.55); }

@supports not selector(::-webkit-scrollbar) {     /* 仅 Firefox 命中 */
  .chat-flow { scrollbar-width: thin; scrollbar-color: rgba(128,128,128,.45) transparent; }
}
```

三个必须写清的点（都是实测踩出来的，不是理论推演）：

1. **`border-radius` 不能写 3px。** 圆角先作用在 border box 上，2px 边框会把内圆角吃掉 2px（3−2=1px），滑块看起来几乎是方的。写 `999px` 让浏览器自己夹到 3px（内容盒 6px 的一半）。
2. **透明边框单独不够。** 实测 `border:2px solid transparent` 不加 `background-clip` 时，背景仍会画到边框底下，视觉上还是 10px 宽；必须配 `background-clip: content-box` 才真内缩。
3. **hover 必须用 `background-color` 而不是 `background` 简写。** 简写会把 `background-clip` 重置成 `border-box`，hover 瞬间滑块从 6px 胀成 10px。

因为 4a 不改别处，代码块 `pre` 的横向 `height` 和 `-corner` 透明这次都不需要——`overflow-y` 之外的横向滚动条不在改动范围内。

---

## 7. 验收标准

**A. 尺寸**
1. `.chat-flow` 的 `offsetWidth - clientWidth` 等于目标值（1a/4a 路线为 6，1b 为 10）。
2. `.md-body pre` 横向滚动条高度同样为 6px（不是 15px）。—— 4a 路线下**不适用**，横向滚动条不在范围内。
3. 六个容器实测宽度一致（决策 4b 时）。—— 4a 路线下改为反向验证：**未被改动的容器仍是原生 15px**。

**B. 视觉**
4. 轨道 `background` 计算值为 `rgba(0,0,0,0)`（透明），页面底色的直接透出，无白条。
5. thumb 圆角可见；hover 时颜色变深（计算值从 `.32` 系到 `.55` 系），且**宽度不得变化**。
6. 横竖交汇处（`pre` 同时双向溢出时）无白角。—— 4a 路线下不适用。
7. 切到白天主题后 thumb 仍可辨识（与聊天区底色对比度 ≥ 1.5:1），不消失。

**C. 布局**
8. 开决策 3a 时：向会话追加消息直到出现滚动条，`.msg-agent` 的 `getBoundingClientRect().right` **前后不变**。
9. `.chat-input` 位置、输入框宽度不受影响。

**D. 非回归**
10. 模型面板（`#modelSheetMenu`）外观不劣化——若走全局规则，删掉它自己那 4 行后视觉应与改前一致。
11. Console 无报错；`tests/test_model_switch.py` 8 项照常通过（本次不碰 JS）。
12. Firefox（若有）下为 `thin` 细条而非 15px 原生（验证 `@supports` 守卫写对）。

---

## 8. 风险

| 风险 | 说明 / 应对 |
|---|---|
| 标准属性与伪元素混写 | §3 已实测：混写会让伪元素整段失效、退回 10px 方块。**必须二选一**，Firefox 用 `@supports` 隔离 |
| macOS 上自定义滚动条会从"悬浮自动隐藏"变成"常驻占位" | 系统行为，无法规避。你当前在 Windows，影响为零；若以后上 Mac 需重评决策 2 |
| Windows 高对比度模式 | 系统会强制覆盖滚动条颜色，此时以系统为准（可接受，不额外处理） |
| 6px 在 125%/150% 缩放下为 7.5/9 物理像素 | 圆角 3px 仍成立，但 1px 级的 2px 内缩（1b）在高缩放下会被抹平，需实机看 |
| 隐藏滚动条 ≠ 不可滚 | 决策 2b/2c 下仍可用滚轮/触控板/键盘滚动，只是不可见。若选 2c，应保证键盘聚焦时也显现 |
| 单文件 1396 行、无构建 | 改动集中在 CSS 一个段落；改完用 §7 的 `offsetWidth-clientWidth` 逐项实测，不靠肉眼 |

---

## 9. 不在本次范围

- 滚动条以外的滚动体验（惯性、`scroll-behavior: smooth`、滚动到底部的"新消息"提示）。
- 聊天流的虚拟滚动（消息很多时的性能）。
- 主题系统的昼夜插值算法本身。

---

## 10. 落地记录与实测验收（2026-10-28）

改动：`src/static/index.html` 紧接 `.chat-flow` 之后新增 24 行 CSS（含 1 行误用提示注释），**未动 JS、未动后端**。

验证手段：headless Chrome（`--force-device-scale-factor=1`）+ 自写 CDP 客户端点真实页面，
并用 PIL 对截图做**像素级**测量（不靠肉眼）：先纵向扫出滑块所在行，再在该行横向数连续像素。
脚本：`_scroll_verify.py` / `_scroll_iso.py`（在 `C:\Users\Chinese\WorkBuddy\2026-10-27-23-06-50`）。

| 验收项 | 实测值 | 结论 |
|---|---|---|
| 占位宽（`offsetWidth-clientWidth`） | 未溢出 10px / 溢出后 10px | ✅ |
| 溢出前后内容右缘 | 1326px → 1326px，位移 **0** | ✅ 3a 生效 |
| 滑块可见宽度（像素） | x=1376..1381，**6px** | ✅ |
| 两侧留白（像素） | 左 2px / 右 2px | ✅ 1b 生效 |
| 静止色 | (54,55,57)，= `.32` 灰叠底色 (20,21,25) 的理论值 54 | ✅ |
| hover 色 / 宽度 | (79,79,81) ≈ `.55` / 仍 6px，未胀宽 | ✅ |
| 模型面板 `.ms-list` | 仍为 6px（自己的规则未被影响） | ✅ 非回归 |
| 裸滚动容器（无 class） | 15px 原生、`scrollbar-width:auto` | ✅ 无全局泄漏 |
| Console | 无报错 | ✅ |
| `tests/test_model_switch.py` | 8/8 通过 | ✅ |
| 白天主题（`--bg #f7f8fa`） | 底色 (247,248,250)、滑块 (208,209,210)、**对比度 1.44:1** | ⚠️ 低于 1.5 门限 |

**唯一未达标项**：白天主题下 1.44:1（差 0.06）。原因是 `.32` 的半透明灰在白底上对比天然偏低。
若要拉到 1.5 以上，把静止透明度从 `.32` 改到 `.38` 即可（实测该值在白天为 1.54:1，夜间同步变亮，观感仍在"克制"范围内）；
`hover` 的 `.55` 不用动。这是一行改动，未擅自实施。

另一个副作用值得知道：`scrollbar-gutter: stable` 预留的是**滚动条宽度 10px**（不是可见的 6px），
所以聊天区内容宽度从"有滚动条时 885px"变成恒定 **870px**；内容右缘因此固定，流式输出不再抖。
