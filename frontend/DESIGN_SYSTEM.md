# AgentSkills 设计体系 v2.0

Claude 暖色调文档流风格，聚焦内容可读性与视觉宁静感。

## 1. 核心设计理念

- **风格定位**: Claude 暖色调文档流 (Warm Document Flow)
- **视觉关键词**: 温暖(Warm)、克制(Restrained)、清晰(Clean)、专注(Focused)
- **用户体验**: "内容优先"，零气泡、左对齐文档流布局，减少视觉噪声

## 2. 布局与结构 (Pattern)

采用 **Claude 文档流** 布局 — 用户和助手消息均左对齐，无聊天气泡。

- **消息布局**: 文档流式左对齐，用户/助手通过小圆形头像 + 角色标签区分
- **间距**: 8px 网格系统，消息间用细分隔线而非间距
- **容器**: 主内容区 `max-w-3xl`，输入框同宽
- **响应式**: 移动优先 (Mobile-first)
- **侧边栏**: 左侧 260px 可折叠
- **右侧面板**: 覆盖式抽屉（Overlay Drawer），不挤压主内容区

## 3. 色彩系统 (Color Palette)

基于 Claude 暖色系定制，全局使用 `claude-*` Tailwind token。

| Token | 色值 | 用途 |
|-------|------|------|
| `claude-bg` | `#FAF9F6` | 页面背景 |
| `claude-surface` | `#F3F1EB` | 侧边栏、卡片填充 |
| `claude-input` | `#F5F3EE` | 输入框背景 |
| `claude-text` | `#1A1915` | 主要文字 |
| `claude-secondary` | `#6B6560` | 次要文字 |
| `claude-muted` | `#A39E95` | 占位符、辅助信息 |
| `claude-accent` | `#D4A574` | 强调色、品牌色 |
| `claude-border` | `#E8E5DE` | 边框 |
| `claude-border-strong` | `#D4D0C8` | 悬停边框 |
| `claude-hover` | `#F0EDE6` | 悬停背景 |
| `claude-success` | `#16A34A` | 成功 |
| `claude-error` | `#DC2626` | 错误 |
| `claude-warning` | `#D97706` | 警告 |

**暗黑模式**: 暂不处理，后续可扩展。

## 4. 排版系统 (Typography)

系统字体栈 + Fira Code 代码字体

- **正文/标题**: 系统字体栈 `system-ui, -apple-system, sans-serif`
- **代码**: `Fira Code, monospace`
- **字重**: 标题 `font-medium`（非 bold），正文 `font-normal`

```javascript
fontFamily: {
  sans: ['system-ui', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', 'sans-serif'],
  mono: ['"Fira Code"', 'monospace'],
}
```

## 5. 组件风格指南

### 消息 (Messages)
- **用户消息**: 左对齐，`w-7 h-7` 圆形头像 `bg-claude-text text-white`，角色标签"你"
- **助手消息**: 左对齐，`w-7 h-7` 圆形头像 `bg-claude-accent/20 text-claude-accent`，角色标签"助手"
- **分隔**: 轮次间 `border-b border-claude-border/50`

### 按钮 (Buttons)
- **Primary**: `bg-claude-text text-white rounded-xl hover:bg-claude-text/90`
- **Ghost**: `hover:bg-claude-hover text-claude-secondary`
- **Send**: 圆形 `w-8 h-8 rounded-full bg-claude-text text-white`

### 输入框 (Inputs)
- **聊天输入**: 胶囊形 `rounded-3xl`，`bg-white border border-claude-border`，聚焦时 `ring-claude-accent`
- **表单输入**: `rounded-xl border-claude-border focus:border-claude-accent`

### 代码块 (Code Blocks)
- **容器**: `rounded-2xl border border-claude-border`
- **头部**: `bg-claude-surface` + 语言标签 `text-claude-muted`
- **代码**: VS Code Dark Plus 主题

### 推理面板 (Reasoning Panel) — Claude 风格

推理面板已重构为 Claude 官方风格，采用 **Display Blocks** 模式而非编号列表。

#### 核心组件结构
| 组件 | 文件 | 职责 |
|------|------|------|
| `ReasoningPanel` | `ReasoningPanel.tsx` | 外层容器，调用 `transformToDisplayBlocks` 转换 |
| `ThinkingBlockView` | `ReasoningPanel.tsx` | "思考 3s >" 可折叠 thinking 块，带实时计时器 |
| `ToolGroupBlockView` | `ReasoningPanel.tsx` | "Edited 2 files, read a file" 工具分组块 |
| `ToolItemView` | `ReasoningPanel.tsx` | 单个工具调用行：图标 + 描述 + diff 统计 + 可展开详情 |
| `DoneMarker` | `ReasoningPanel.tsx` | ✓ Done 完成标记 |
| `TruncatedCodeBlock` | `ReasoningPanel.tsx` | 可折叠代码块 |

#### 转换层 (`displayBlocks.ts`)
- 将 `StepData[]` 转换为 `DisplayBlock[]`（ThinkingBlock / ToolGroupBlock / NarrativeBlock）
- **跨 Step 合并**：连续的工具调用步骤合并为一个 ToolGroupBlock
- **智能描述**：`getToolDescription()` 生成自然语言描述（"Read src/app.py"、"Run \`npm test\`"）
- **分组摘要**：`getGroupSummary()` 生成 "Edited 2 files, read a file" 风格摘要
- **Diff 统计**：从 edit 工具结果中提取 `+X -Y` 变更统计

#### 样式约定
- **无外框容器**：不使用 `rounded-xl border` 包裹整个面板
- **ThinkingBlock**: `inline-flex` 按钮，`Lightbulb` 图标，展开后左侧边框 `border-claude-accent/30`
- **ToolGroupBlock**: `Zap` 图标，默认展开，完成后显示 `DoneMarker`
- **ToolItem**: hover 时显示展开箭头，diff 统计用 `text-green-600`/`text-red-500` 着色
- **动画**: 遵循 `disableMotion` flag，使用 `animate-fade-in`

#### 文字语言约定
- **工具描述和摘要**: 采用英文（Claude 官方风格），如 "Read src/app.py"、"Edited 2 files"、"Done"
- **UI 标签 / 提示文字**: 采用中文，如 "思考 3s"、"正在分析请求..."、"输入"、"输出"、"展开全部"
- **设计理由**: 工具名、文件路径、命令等技术内容本身是英文，保持英文描述更自然简洁，避免中英混合的不适感

### 交互效果 (Micro-interactions)
- **Hover**: 颜色/透明度变化，避免布局偏移
- **Transition**: `duration-200 ease-in-out`（非 `transition-all`）
- **Active**: `active:scale-95`
- **加载动画**: 3 个小圆点 `animate-dot-pulse`，弃用 4 点 thought-flow

## 6. 交互设计模式 (Interaction Patterns) 🆕

### 6.1 覆盖式抽屉布局 (Overlay Drawer + Left Collapse)

用于解决多面板并存时的布局拥挤与性能问题，采用“**左收右开、右侧覆盖**”的策略，避免中间聊天区因动态 `padding/width` 过渡触发布局重排（reflow）。

- **触发条件**: 当右侧面板（Files/Artifacts）打开时。
- **行为逻辑**:
  1. **左侧栏 (Sidebar)**: 自动折叠（Width -> 0, Opacity -> 0），释放横向空间。
  2. **中间区 (Main Chat)**: 保持稳定宽度（不做 `max-w`/`padding-right` 的动态避让动画），减少长列表渲染抖动。
  3. **右侧栏 (Panel)**: 以 Overlay（覆盖）方式滑出，动画只作用于 `transform`（`translateX`）。
  4. **Backdrop（遮罩）**: 抽屉下方提供轻量遮罩，支持点击空白处关闭；**不锁定聊天滚动**。

- **推荐动效参数**:
  - Drawer: `transition-transform duration-300 ease-out`
  - Backdrop: `transition-opacity duration-200`
  - 避免：`transition-all` + `width/padding/max-width` 动画

- **实现参考**:
  ```tsx
  // ArtifactsPanel.tsx（覆盖式抽屉）
  className={`fixed right-0 ... transition-transform duration-300 ease-out ${isOpen ? 'translate-x-0' : 'translate-x-full'}`}

  // Backdrop（点击关闭，不锁滚动）
  <div className={`fixed inset-0 transition-opacity duration-200 ${isOpen ? 'opacity-100' : 'opacity-0'}`} onClick={onClose} />
  ```

### 6.2 会话滚动记忆 (Per-Session Scroll Restoration) 🆕

用于提升“多会话切换”的连续性：用户回到某个会话时，应恢复到上次阅读位置，避免出现先渲染顶部再跳转的视觉跳动。

- **原则**:
  - **记忆优先**：按 `sessionId` 记忆 `scrollTop`。
  - **恢复在首帧**：历史消息渲染完成后优先恢复 `scrollTop`（避免跳）。
  - **底部跟随**：仅当用户位于底部时，新内容才自动跟随滚动。

- **实现建议**:
  - 保存位置：在 scroll 事件中写入 `scrollTop`（可用内存 ref 或 localStorage）。
  - 恢复位置：在首次渲染完成后的下一帧设置 `container.scrollTop = savedTop`。

### 6.3 实时反馈预览 (Typewriter Preview)

解决长耗时操作 (AI思考/生成) 的等待焦虑，提供透明且有趣的实时反馈。

- **应用场景**: 推理面板 (Reasoning Panel)、日志输出、状态栏。
- **视觉样式**: 打字机效果 + 闪烁光标 + 淡色文字。
- **截断策略 (Truncation Strategy)**:
  - **Keep-Head (保留头部)**: 适用于关键信息在开头的操作。
    - *场景*: Search (query), Bash (cmd), Read File (path)。
    - *效果*: `search: "react hooks..."`
  - **Keep-Tail (保留尾部)**: 适用于追加生成型操作，模拟实时产出。
    - *场景*: Write File (content), Edit File (diff)。
    - *效果*: `...import { useState } from 'react';` (字符向左滚动)

## 7. 前端开发清单 (Pre-Delivery Checklist)

- [ ] **图标**: 禁止使用 Emoji 作为 UI 图标，统一使用 Heroicons 或 Lucide React
- [ ] **光标**: 所有可点击元素必须添加 `cursor-pointer`
- [ ] **反馈**: 所有交互元素必须有 Hover 和 Focus 状态
- [ ] **图片**: 所有 `img` 标签必须包含有意义的 `alt` 属性
- [ ] **性能**: 图片使用 WebP 格式，并设置 `loading="lazy"`
- [ ] **无障碍**: 确保文本对比度满足 WCAG AA 标准 (4.5:1)
- [ ] **动画**: 重视 `prefers-reduced-motion` 设置
- [ ] **交互**: 检查长耗时操作是否有 loading 或实时预览反馈

---
*Generated by UI/UX Pro Max for AgentSkills*
