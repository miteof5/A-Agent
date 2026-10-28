---
name: ActionAgent 控制台
description: 本地 AI Agent 的操作控制台，图纸·工程蓝图视觉世界
colors:
  paper: "#f4f5f1"
  paper-deep: "#eceeeb"
  panel: "#fafbf7"
  ink: "#23211c"
  ink-muted: "#4c4a41"
  ink-faint: "#5c594e"
  ink-ghost: "#6f6c60"
  line-hair: "#dadcd4"
  line-rule: "#bec1b8"
  line-outline: "#93968b"
  blueprint-blue: "#1e56c4"
  ok-green: "#2e7d32"
  error-red: "#c62828"
  warn-amber: "#a2651a"
  wait-violet: "#6a4fa3"
typography:
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"
    fontSize: "14px"
    lineHeight: 1.6
  mono:
    fontFamily: "Consolas, 'SF Mono', 'Cascadia Mono', Menlo, monospace"
    fontSize: "11px"
rounded:
  sm: "2px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
components:
  button-primary:
    backgroundColor: "{colors.blueprint-blue}"
    textColor: "#ffffff"
    rounded: "{rounded.sm}"
    padding: "0 18px"
    height: "28px"
  button-ghost:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink-muted}"
    rounded: "{rounded.sm}"
    padding: "0 12px"
    height: "28px"
  tool-part:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "6px 10px 6px 34px"
  input-dock:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.sm}"
    padding: "10px 12px 8px"
---

# Design System: ActionAgent 控制台

## Overview

**Creative North Star: "一张正在展开的工程图纸"**

ActionAgent 是本地运行的自驱动 AI Agent 的操作控制台。它的视觉世界借用了工程制图的全部纪律：白纸墨线的层次、等宽标注承载数据、编号零件与引线标注呈现每一次工具调用、会签与盖章呈现审批结果、标题栏承载任务元信息。这个世界的选择源于产品本质——Agent 的每一次真实动作都必须可观察、可审计、可干预，而工程图纸正是"精确、可追溯、无歧义"的视觉契约。

界面密度高而克制：hairline 结构线、1px 常规线、2px 主轮廓构成线宽层级；工程蓝（#1e56c4）是唯一交互色，只出现在可点、可选中、状态指示处；状态色遵循制图惯例——绿=通过、红=拒绝/错误、琥珀=待批/警告。控件全部是标准 web 组件（直角、细描边），不做自创手势，让工具消失进任务。夜间主题是"暗色图纸"：深蓝石墨纸底 + 提亮的墨线，由太阳高度角连续插值过渡。

**Key Characteristics:**
- 白纸/暗纸底 + 墨线层次，无渐变、无玻璃拟态、无大圆角（统一 2px）
- 等宽字体承载一切数据与标注；编号圆（直径 20px）标注工具调用
- 工程蓝是唯一 accent；状态色只做状态，不做装饰
- 工具调用=带引线的编号零件；审批=会签章（旋转 -6°）
- 顶部标题栏以表格化信息格呈现任务/状态/模型/Token

## Colors

冷图纸白为主底，墨线灰阶构建层次，工程蓝为唯一交互色，状态色按制图惯例。

### Primary
- **Blueprint Blue** (#1e56c4): 唯一交互色。用于发送/新建主按钮、选中会话描边、状态点（执行中）、链接、聚焦描边。hover 加深至 #17459e。

### Neutral
- **Cold Drawing Paper** (#f4f5f1): 页面主底。刻意偏冷灰白，规避暖奶油 AI 味，同时保持纸感。
- **Paper Deep** (#eceeeb): 辅助层。顶栏、侧栏、面板底色；代码块/标注底。
- **Panel** (#fafbf7): 浮层/卡片底。对话气泡、工具零件、输入区。
- **Ink** (#23211c): 主文字墨色。
- **Ink Muted** (#4c4a41): 次级文字。
- **Ink Faint** (#5c594e): 弱化文字（标签、参数），对比度 ≥4.5:1。
- **Ink Ghost** (#6f6c60): 最弱文字（时间戳、序号、placeholder），对比度 ≥4.5:1。
- **Line Hair / Line Rule / Line Outline** (#dadcd4 / #bec1b8 / #93968b): 三级线宽，分别对应 hairline 结构线、常规线、主轮廓。

### 状态色
- **Ok Green** (#2e7d32): 完成、执行成功、批准章。
- **Error Red** (#c62828): 失败、拒绝、停止。
- **Warn Amber** (#a2651a): 待审批、等待回答、暂停。
- **Wait Violet** (#6a4fa3): Agent 提问确认。

### Named Rules
**The One-Blue Rule.** 工程蓝只出现在可交互元素、选中态、执行状态与链接上；任何纯装饰用途都是越界。同一屏上蓝的占比通常低于 10%。

**The Line-Hierarchy Rule.** 线宽即层级：hairline（1px #dadcd4）用于结构分隔，常规（1px #bec1b8）用于控件描边，主轮廓（1.5-2px #93968b 或状态色）用于需要强调的边界（审批卡、危险操作）。不得用颜色深浅替代线宽层级。

## Typography

**Body Font:** 系统无衬线栈（-apple-system / Segoe UI / PingFang SC / Microsoft YaHei）
**Label/Mono Font:** Consolas / SF Mono / Cascadia Mono / Menlo

**Character:** 界面文字用"工程无衬线"——干净、中性、可扫描；所有数据、参数、编号、标签、时间戳用等宽字体，强化"这是一张图纸上的标注"的阅读体验。中文用系统中文回退，保证渲染。

### Hierarchy
- **Title**（600, 13-14px, 1.6）: 任务标题、主按钮文字。
- **Body**（400, 14px, 1.7）: 对话内容、Markdown 正文。
- **Label**（mono, 11px, 字距 .4-.6px, 大写）: 顶栏字段标签（任务/状态/模型/Token）、面板小标题（日志 LOG / 模型 MODEL）。
- **Data**（mono, 11-12px）: 参数、时间戳、Token 计数、日志行。

### Named Rules
**The Mono-Data Rule.** 数字、参数、路径、时间戳一律等宽；正文和解释性文字一律无衬线。两种字体各司其职，不得混用。

## Layout

三栏操作台：顶部标题栏（高 46px）+ 主体（左 rail 240px + 中央对话流 + 右面板 320px 可折叠）。

- 顶栏为表格化标题栏：品牌块、任务、状态（带状态点）、模型、Token 依次排布，单元格以 hairline 分隔——信息如图纸标题栏一样一栏一值。
- 左 rail 是"图纸清单"：新建任务按钮 + 任务条目（编号、状态点、标题、时间），条目选中以工程蓝描边。
- 中央对话流是执行日志：用户气泡右对齐，Agent 回答与过程块左对齐；输入区固定在底部，聚焦时工程蓝描边。
- 右面板以 tab 切换日志/模型；日志行以等宽小字展示，工具原始输出在此查看。
- 响应式：窄屏下右面板可折叠为竖条，左 rail 可收缩，中央保持可读密度。

## Elevation & Depth

**无阴影系统，用线宽与色层表达深度。** 这是图纸世界的核心纪律：平面即平面，层级靠描边、底色调和状态色表达，而非投影。唯一的阴影用于浮层菜单（下拉、toast）以获得与图纸面的分离，极轻（rgba(30,26,18,.10)）。

### Named Rules
**The Flat-By-Default Rule.** 表面静止时一律平。阴影只出现在需要"浮在图纸上方"的临时层（下拉菜单、toast）。

## Shapes

**2px 直角。** 全部控件、卡片、输入区使用 2px 圆角——制图语言拒绝圆润。特殊形状仅两处：编号圆（20px 圆形边框 + 等宽数字，标识工具调用零件）与审批章（旋转 -6° 的 2px 双色描边方框，标识会签结果）。

## Components

### Buttons
- **Shape:** 直角（2px），等宽字体，细描边。
- **Primary**（发送/新建任务）: 工程蓝底白字，高 28px，hover 加深，disabled 用墨灰。
- **Ghost**（顶栏按钮 模型/停止）: 面板底 + 墨线描边，hover 描边变工程蓝（危险按钮 hover 变错误红）。

### Tool Part（签名组件：工具调用零件）
- **组成:** 左侧引线（虚线）+ 编号圆（20px）+ 工具名标签（mono 蓝）+ 参数（mono 灰）+ 状态行（OK 绿 / FAIL 红 / 执行中墨灰）+ 可选摘要 + "查看原始输出"按钮。
- **状态:** 执行中（"⏳ 执行中…"墨灰）→ 完成（绿 ✓ / 红 ✗ / 权限拦截红）。

### Approval Card（签名组件：会签卡）
- **Shape:** 2px 琥珀描边 + 琥珀底（#fbf0dc），标题"⚠ 高危操作 · 会签审批"。
- **批准:** 按钮变"已批准，执行中…"，卡内盖绿色 APPROVED 章（旋转 -6°）。
- **拒绝:** 按钮变"已拒绝，跳过该操作"，盖红色 DENIED 章。
- **任务停止:** 按钮禁用并显示"⏸ 任务已停止"。

### Inputs
- **Style:** 面板底 + 1.5px 墨线描边，直角；内部 textarea 无边框。
- **Focus:** 描边变工程蓝 + 2px 蓝晕（rgba(30,86,196,.10)）。
- **Disabled:** 墨灰，不可点。

### Navigation
- 左 rail 会话条目：编号 + 状态点 + 标题 + 时间；hover 描边加深，active 工程蓝描边 + 蓝晕底。
- 删除按钮：hover 时出现，二次点击确认（红色"确认？"）。
- 模型管理：面板内分组列表，点选即时切换，busy 半透明禁用。

### Session Item（图纸清单条目）
- 编号（mono #01）、状态点（状态色 7px 圆）、标题（12.5px 两行截断）、时间（mono 11px 右对齐）；删除 ✕ 悬停出现。

## Do's and Don'ts

### Do:
- **Do** 用编号圆 + 引线标注呈现每一次工具调用——这是本系统区别于普通聊天的签名动作。
- **Do** 让审批结果以"盖章"收尾：批准盖 APPROVED 章、拒绝盖 DENIED 章，盖章即结论。
- **Do** 用等宽字体呈现所有数据（Token、参数、时间、日志、模型名）。
- **Do** 把状态色用在状态上：绿=通过、红=拒绝、琥珀=待批，且只做状态。
- **Do** 保持 2px 直角；圆角出现在 2px 以上即偏离图纸世界。
- **Do** 沿用三栏布局（rail + 对话流 + 面板），新功能优先放入现有区域。

### Don't:
- **Don't** 引入渐变、玻璃拟态、大圆角、插画或 emoji 装饰——它们属于另一种界面语言。
- **Don't** 让工程蓝进入纯装饰用途（图标填充、背景色块），它只服务交互与状态。
- **Don't** 用消费级"卡片堆叠"组织任务——任务属于图纸清单，一行一条。
- **Don't** 让文字低于 11px（功能文字）或正文低于 14px；等宽标注最小 11px。
- **Don't** 给静止表面加阴影；浮层（菜单/toast）之外一律平。
- **Don't** 用暖奶油色背景——图纸纸底保持冷灰白（#f4f5f1）。
