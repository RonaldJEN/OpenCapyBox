# 前端 API 使用对照表

> 本文档记录前端实际使用的后端 API 接口及对应的代码位置

## API 使用情况

### 认证 API

| 接口 | 前端使用 | 调用位置 |
|------|----------|----------|
| `POST /api/auth/login` | ✅ | `src/services/api.ts:82` (`login` 方法) |
| `GET /api/auth/me` | ❌ 未使用 | - |

### 会话管理 API

| 接口 | 前端使用 | 调用位置 |
|------|----------|----------|
| `POST /api/sessions/create` | ✅ | `src/services/api.ts:98` (`createSession` 方法) |
| `GET /api/sessions/list` | ✅ | `src/services/api.ts:106` (`getSessions` 方法) |
| `GET /api/sessions/running-session` | ✅ | `src/services/api.ts:178` (`getRunningSession` 方法) |
| `GET /api/sessions/{id}/history` | ✅ | `src/services/api.ts:114` (`getSessionHistory` 方法) |
| `GET /api/sessions/{id}/history/v2` | ✅ | `src/services/api.ts:163` (`getSessionHistoryV2` 方法) |
| `PATCH /api/sessions/{id}/title` | ❌ 未使用 | - |
| `DELETE /api/sessions/{id}` | ✅ | `src/services/api.ts:124` (`deleteSession` 方法) |

### 文件管理 API

| 接口 | 前端使用 | 调用位置 |
|------|----------|----------|
| `GET /api/sessions/{id}/files` | ✅ | `src/services/api.ts:294` (`getSessionFiles` 方法) |
| `GET /api/sessions/{id}/files/{path}` | ✅ | `src/services/api.ts:323` (`downloadFile` 方法)<br>`src/components/FilePreview.tsx:48,67,92,132,222` (预览/下载) |
| `POST /api/sessions/{id}/upload` | ✅ | `src/services/api.ts:307` (`uploadFile` 方法) |

> 备注：`POST /api/sessions/{id}/upload` 在未选择文件时后端会返回 `400`（`未选择文件`），前端应显示友好错误提示。

### 对话 API

| 接口 | 前端使用 | 调用位置 |
|------|----------|----------|
| `POST /api/chat/{id}/message` | ✅ | `src/services/api.ts:137` (`sendMessage` 方法) |
| `POST /api/chat/{id}/message/v2` | ✅ | `src/services/api.ts:152` (`sendMessageV2` 方法) |
| `POST /api/chat/{id}/message/stream` | ✅ | `src/services/api.ts:173-338` (`sendMessageStreamV2` 方法) |
| `GET /api/chat/{id}/round/{round_id}/subscribe` | ✅ | `src/services/api.ts:345-454` (`subscribeToRound` 方法，支持 AbortController 取消)<br>`src/components/ChatV2.tsx:126-232` (断线恢复，支持切换会话时自动取消订阅) |

### 工具指标 API

| 接口 | 前端使用 | 调用位置 |
|------|----------|----------|
| `GET /api/metrics/{id}/stats` | ✅ | `src/services/api.ts:339` (`getSessionMetricsStats` 方法) |
| `GET /api/metrics/{id}/recent` | ✅ | `src/services/api.ts:347` (`getRecentMetrics` 方法) |
| `GET /api/metrics/{id}/trends` | ✅ | `src/services/api.ts:357` (`getToolPerformanceTrends` 方法) |

---

## 未使用的后端接口（共 2 个）

以下接口在后端已实现但前端未调用：

1. **`GET /api/auth/me`** - 获取当前用户信息
2. **`PATCH /api/sessions/{id}/title`** - 更新会话标题

---

## 关键前端文件

| 文件 | 说明 |
|------|------|
| `src/services/api.ts` | API 服务封装，所有 API 调用的主要入口 |
| `src/components/FilePreview.tsx` | 文件预览组件，直接调用文件下载/预览接口 |
| `src/components/Login.tsx` | 登录页面 |
| `src/components/SessionList.tsx` | 会话列表管理，支持自动折叠和 CSS Transition 动画 |
| `src/components/ChatV2.tsx` | 聊天界面：配合左侧折叠与右侧覆盖式抽屉（Overlay Drawer），实现按会话记忆/恢复滚动位置，欢迎页「输入即创建会话」 |
| `src/components/ReasoningPanel.tsx` | 推理面板（Claude Display Blocks 风格），含 ThinkingBlock / ToolGroupBlock / DoneMarker 等子组件 |
| `src/utils/displayBlocks.ts` | Display Blocks 转换层：将 StepData[] 聚合为 DisplayBlock[]，含工具描述、diff 统计、thinking 计时 |
| `src/components/MetricsDashboard.tsx` | 指标仪表盘 |
| `src/App.tsx` | 主应用组件，协调左右面板折叠状态，提供 `onCreateSession` 回调给 ChatV2 |

---

## 重要功能说明

### 0. 欢迎页「输入即创建会话」(Type-to-Start) 🆕

**问题**：首次进入应用时，用户必须先点击左上角「+」按钮创建会话，然后才能输入问题，体验割裂。

**解决方案（现行实现）**：
- 欢迎页（无会话选中时）直接包含完整输入框，与对话页风格一致。
- 用户输入文字后按 Enter，自动在后台创建会话并发送消息。
- 使用 `pendingMessageRef` 暂存消息 → 调用 `onCreateSession` → 父组件设置 `sessionId` → `useEffect` 检测到 `sessionId` 变更后自动发送。
- 创建失败时恢复输入内容并显示错误提示。
- 快捷建议按钮（如「帮我写一个 Python 爬虫」）点击后填入输入框，Enter 触发同样的自动创建流程。

**相关代码**：
- `src/App.tsx` - `handleCreateSessionForChat` 回调，传递给 ChatV2 作为 `onCreateSession` prop
- `src/components/ChatV2.tsx` - `pendingMessageRef`、`creatingSession` 状态、`sendMessageForSession` 提取方法
- `src/components/ChatV2.tsx` - 欢迎页统一渲染（含欢迎内容 + 输入区 + 错误提示）

### 1. 交互设计升级 (Interaction Upgrade) 🆕

**问题**：右侧 Files 抽屉打开/关闭时，如果通过改变中间区域宽度/内边距来“避让”（例如 `pr-[380px]` + `transition-all`），会触发布局重排（reflow），在长对话/复杂 Markdown 时造成明显卡顿。

**解决方案（现行实现）**：
- **Left-Collapse**：右侧 Files 面板打开时，左侧 Session 面板自动折叠（宽度/透明度过渡）。
- **Overlay Drawer**：右侧 Files 面板采用覆盖式抽屉（`transform` 滑入），不再挤压中间聊天区。
- **Click-to-close Backdrop**：抽屉下方提供轻量遮罩，点击空白处关闭；不锁定聊天区滚动。

**相关代码**：
- `src/App.tsx` - 状态中枢，传递 `onPanelToggle` 控制左侧折叠
- `src/components/ChatV2.tsx` - 移除动态 `pr-[380px]` / `max-w` 挤压逻辑，聊天区保持稳定宽度
- `src/components/ArtifactsPanel.tsx` - 覆盖式抽屉（`translate-x-*`）+ 遮罩点击关闭（不锁滚动）
- `src/components/SessionList.tsx` - 左侧栏折叠过渡实现

### 2. 会话切换滚动体验 (Scroll Restoration) 🆕

**问题**：切换会话时，默认渲染从顶部开始，再通过 JS 自动滚动到底部，容易出现“先在顶部闪一下再跳到底部”的视觉跳动；同时也会打断用户上次阅读位置。

**解决方案（现行实现）**：
- **Per-Session Scroll Memory**：按 `sessionId` 记录聊天区 `scrollTop`。
- **Restore on First Render**：会话历史加载完成后优先恢复上次 `scrollTop`，避免跳动。
- **Follow Only When At Bottom**：后续流式更新仅当用户位于底部时才自动跟随滚动。

**相关代码**：
- `src/components/ChatV2.tsx` - `scrollPosBySessionRef` / `pendingRestoreScrollRef`，以及智能滚动逻辑

### 3. 推理面板 — Claude Display Blocks 风格 🆕

**问题**：旧版推理面板采用编号步骤列表（Step 1 / Step 2），信息密度低、不符合主流 AI 产品体验。

**解决方案（现行实现）**：
重构为 Claude 官方 Display Blocks 模式：
- **ThinkingBlock**：可折叠的 "思考 3s >" 按钮，带实时计时器
- **ThinkingGroupBlock**：多个连续 thinking 合并为 "思考 3次" 可折叠分组
- **ToolGroupBlock**：跨步骤连续工具调用合并为分组，显示摘要（"Edited 2 files, Read a file"），含 DoneMarker
- **ToolItem**：单个工具调用行，含智能描述、diff 统计（+X -Y）、可展开详情
- **转换层** `displayBlocks.ts`：将 StepData[] → DisplayBlock[]，职责分离

**时间戳全链路**：
- 后端 AG-UI 事件携带 `timestamp`、`executionTimeMs`
- SSE 传输层透传到前端回调
- StepData 新增 `thinking_start_ts`、`thinking_end_ts`、`started_at_ts`、`finished_at_ts`
- ToolCall 新增 `started_at_ts`、`ended_at_ts`；ToolResult 新增 `received_at_ts`、`execution_time_ms`

**相关代码**：
- `src/utils/displayBlocks.ts` - 核心转换层（`transformToDisplayBlocks`、`getToolDescription`、`getGroupSummary` 等）
- `src/components/ReasoningPanel.tsx` - 渲染层（ThinkingBlockView、ToolGroupBlockView、ToolItemView、DoneMarker）
- `src/types/index.ts` - StepData / ToolCall / ToolResult 新增时间戳字段；StreamCallbacks / SubscribeCallbacks 回调签名增加 timestamp 参数

### 3. SSE 订阅取消机制

**问题**：切换会话时旧的 SSE 订阅未取消，导致后端订阅者数量累积。

**解决方案**：
- `api.ts` 中的 `subscribeToRound` 方法现在返回 `{ promise, abort }` 对象
- 使用 `AbortController` 支持取消 fetch 请求
- `ChatV2.tsx` 在 `useEffect` 的 cleanup 函数中调用 `abort()` 取消订阅

**相关代码**：
- `src/services/api.ts:345-454` - subscribeToRound 实现
- `src/components/ChatV2.tsx:41-65` - 订阅管理和清理

### 刷新后运行中会话恢复

**问题**：浏览器刷新后，运行中会话的动画不显示。

**解决方案**：
- `SessionList.tsx` 调用 `getRunningSession` API 检测运行中会话（单次查询，避免 N+1）
- 通过 `onRunningSessionDetected` 回调通知父组件
- `App.tsx` 自动设置 `executingSessionId` 并选择该会话

**相关代码**：
- `src/services/api.ts:178` - getRunningSession 方法
- `src/components/SessionList.tsx:59-69` - checkForRunningSession 函数
- `src/App.tsx:29-37` - handleRunningSessionDetected 处理函数

### 订阅流式事件恢复 (Subscription Stream Recovery) 🆕

**问题**：刷新浏览器后，虽然能检测到运行中的轮次并订阅，但无法接收实时的流式更新（thinking、文本内容、工具调用等）。

**解决方案**：
- 后端 `emit()` 辅助函数：同时向原始流队列和所有订阅者发送 AG-UI 事件
- 前端 `SubscribeCallbacks` 扩展：支持完整的流式事件回调（文本消息、思维链、工具调用、步骤事件）
- `ChatV2.tsx` 订阅处理：实时更新 rounds 状态以反映流式内容

**相关代码**：
- `src/api/routes/chat.py` - `emit()` 函数实现广播
- `src/services/api.ts:459-540` - subscribeToRound 流式事件处理
- `src/components/ChatV2.tsx:248-440` - 订阅回调中的流式事件处理

### TypeScript 类型定义

**新增类型**（`src/types/index.ts`）：
- `RunningSessionResponse` - 运行中会话响应
- `StreamCallbacks` - SSE 流式回调类型
- `SubscribeCallbacks` - 订阅回调类型（含流式事件回调）
- `SubscriptionResult` - 订阅结果类型（包含 promise 和 abort）

**类型使用**：
- `sendMessageStreamV2` 使用 `StreamCallbacks` 替代内联类型
- `subscribeToRound` 使用 `SubscribeCallbacks` 和 `SubscriptionResult`
- 回调中的 `step` 参数使用 `StepData` 类型
- 回调中的 `toolCalls` 参数使用 `ToolCall[]` 类型

**SubscribeCallbacks 流式事件回调**：
- `onTextMessageStart/Content/End` - 文本消息流式事件
- `onThinkingStart(messageId, timestamp?)/Content/End(messageId, timestamp?)` - 思维链流式事件（含时间戳）
- `onToolCallStart(toolCallId, toolName, parentMessageId?, timestamp?)/Args/End(toolCallId, timestamp?)/Result(messageId, toolCallId, content, timestamp?, executionTimeMs?)` - 工具调用流式事件（含时间戳和执行耗时）
- `onStepStarted(stepName, timestamp?)/Finished(stepName, timestamp?)` - 步骤开始/完成事件（含时间戳）
