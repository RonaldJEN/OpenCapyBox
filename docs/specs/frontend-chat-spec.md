# 前端 Chat Spec — 聊天 / SSE / 推理面板

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[chat-spec.md](./chat-spec.md)

覆盖组件：`ChatV2.tsx`、`Round.tsx`、`ChatInput.tsx`、`ReasoningPanel.tsx`、`QuestionCard.tsx`。

## 1. 模块职责

- 发送用户消息（含附件、引用图片）
- 消费后端 SSE（AG-UI 事件）增量构建 `RoundData[]`
- 渲染消息流（user → reasoning → assistant）
- 将助手回复中的会话文件引用抽取为回复底部的可点击文件卡片，同时保留 markdown 正文原样显示
- 处理中断/恢复：断连重连、ask_user 中断、用户主动取消
- 滚动控制：首次恢复 + 底部跟随

**不职责**：
- 会话 CRUD → `SessionList`
- 面板切换 → `App.tsx`

## 2. 数据模型（前端内存态）

```ts
// types/index.ts
RoundData {
  round_id: string
  parent_run_id?: string | null
  user_message: string
  user_attachments?: AttachmentInfo[]
  final_response: string
  steps: StepData[]
  step_count: number
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled' | 'resumed' | 'max_steps_reached'
  created_at: string
  completed_at?: string
  interrupt?: InterruptDetails       // ask_user 中断
}

StepData {
  step_number: number
  thinking?: string
  assistant_content?: string
  tool_calls: ToolCall[]       // { id?, name, input, started_at_ts?, ended_at_ts? }
  tool_results: ToolResult[]   // { tool_call_id?, success, content, error?, received_at_ts?, execution_time_ms? }
  status: string
  created_at?: string
  thinking_start_ts?: number
  thinking_end_ts?: number
  started_at_ts?: number
  finished_at_ts?: number
}

AgentState {
  status: 'idle' | 'running' | 'waiting' | 'error'
  lastUpdated: number
  // ...其余字段通过 JSON Patch 增量更新
}
```

### 2.1 助手文件引用卡片

`Round` 按原文渲染助手 markdown；`extractAssistantFiles()` 只从 fenced code block 外的可访问会话文件引用中抽取候选，在助手回复底部统一渲染去重文件卡片。解析不得删除、替换或隐藏正文里的路径文本。

识别范围：
- 仅解析 fenced code block 外的提示行或单一路径引用，用于生成底部文件卡片。
- 支持标签：`文件位置` / `文件路径` / `保存位置` / `已保存到` / `输出文件` / `生成文件` / `文件`。
- 支持助手正文中的反引号文件引用，例如 `` `DeepSeek_V4_解读.docx` ``。
- 行内反引号仅接受单一路径形态；含空白的命令片段（如 `` `python3 quick_sort.py` ``）保持 markdown。
- 支持当前 session 的绝对沙箱路径：`/home/user/sessions/{sessionId}/path/to/file.ext`。
- 支持相对路径：`path/to/file.ext`。
- 文件扩展名必须属于 `FilePreview` 当前处理的类型集合（文本、Markdown、HTML、代码、DOC/DOCX、CSV/XLS/XLSX、PPT/PPTX、图片、PDF）。

拒绝范围：
- 跨 session 的 `/home/user/sessions/{otherSessionId}/...`。
- URL、目录、含 `.` / `..` 段的路径、当前不可预览的扩展名。
- 代码块中的命令或文件名，例如 `python3 quick_sort.py`。

点击行为：
- 文件卡片位于助手 markdown 后方，不是正文内联替换。
- 文件卡片调用 `ChatV2` 的文件面板入口，打开 `ArtifactsPanel` 并传入目标文件。
- 点击前必须按目标父目录查询当前 session 文件列表；只有命中同路径文件时才打开 Files 抽屉。
- 未命中说明该文本只是助手描述或文件尚未生成，必须提示用户且不得打开预览。
- `ArtifactsPanel` 直接进入面板内 `FilePreview`，不走全屏预览弹窗。
- 用户上传附件仍沿用原有 `onPreviewAttachment` 全屏预览链路。

## 3. 核心不变量（Critical Invariants）

### 3.1 会话隔离（最关键）

**任何 SSE 回调、异步 fetch 的 setState 之前必须校验：**

```ts
const isStale = () => boundSessionId !== undefined && sessionIdRef.current !== boundSessionId;
if (isStale()) return;
```

`boundSessionId` 在 `createStreamCallbacks({ boundSessionId })` 工厂函数中闭包捕获；`sessionIdRef.current` 在 `sessionId` 变化时即时更新。

**违反后果**：A 会话消息污染 B 会话 UI。

### 3.2 RUN_STARTED 才通知执行中

`onRunStarted` 回调触发后才调用 `notifyExecutionStart()`，避免被拒请求（429 等）污染执行标记。

### 3.3 SSE 断连恢复流程

```
catch (SSE error)
  → GET /api/sessions/{sid}/history
    → 找到目标 round
      → 若 status ∈ {completed, failed, interrupted, resumed, cancelled, max_steps_reached}
          → 调用 _tryRecoverRoundFinished → onRunFinished → 结束
      → 若 status == running
          → subscribeToRound(sid, roundId, { lastSequence })
```

`_ROUND_TERMINAL_STATUSES` 必须与后端 `Round.SUBSCRIBE_TERMINAL_STATUSES` 保持一致。

### 3.4 幂等冲突走订阅

`sendMessage` 抛 `RoundExistsError(roundId, status)` 时：
- 不重发
- 直接 `subscribeToRound(sid, roundId)` 进入订阅

### 3.5 取消语义

用户点击取消：
1. 前端点击后必须立即取消当前订阅（`subscription.abort()`），防止后续迟到回调覆盖状态。
2. 本地将当前 `running` round 先收敛为取消态 `cancelled`（用于即时反馈），结束 `sending/resuming`，并立即恢复输入可用；不得等待 `/abort` HTTP 响应或 SSE 终态事件。
3. 进入 `stopping` 状态：输入框保持可编辑，但新的发送动作必须禁用，直到 `/abort` 返回，避免用户立即发送新问题时撞到后端尚未释放的 user/session lock。
4. 同步发起 POST `/api/chat/{sid}/abort`。
5. 若请求返回 409（会话已无运行任务）：按“已停止”处理，保持本地已收敛 UI。
6. 其他请求失败：重新拉取历史以恢复真实运行态，并提示停止请求失败。
7. 后端规范终态为 `RUN_FINISHED(outcome=interrupt, result.reason=user_cancelled)`，
  前端按 `isUserCancelledOutcome()` 识别为"已取消"，**不是错误**。

**判定**：`outcome === 'interrupt' && result?.reason === 'user_cancelled'`。outcome=interrupt 但无 reason 的保守处理为非取消。

### 3.6 ask_user 中断恢复

- `loadHistory()` 若发现最新 round `status === 'interrupted' && interrupt`：
  - `setPendingInterrupt(interrupt)` + `agentState.status = 'waiting'`
  - 渲染 `QuestionCard` 供用户回答
- 用户回答 → 继续调用后端 `resume` 接口（携带答案）。
- **放弃路径**：`QuestionCard` 提供关闭按钮（X），点击仅本地 `setPendingInterrupt(null)` 隐藏卡片，不调用任何后端接口。Round 保持 `interrupted` 终态，用户可直接发新消息开启新 round。

### 3.7 多 RUN 同一 Round

一个 round 可能包含多个 RUN（断线重连同一 round 时新增 RUN）。前端按 `runId` 区分，但 UI 内聚合在同一个 `RoundData.steps` 下。

### 3.8 Resume 后的 Round 关系

`ask_user` 中断恢复后，**不**复用旧 round。后端语义（见 `chat-spec.md` §Resume 流程，对应实现 `history_service.create_resume_round()` 与 `agent_service` 的 resume 入口）：

- 旧 `interrupted` round 的状态会被后端迁移为 `resumed`，并清除 `interrupt_payload`，以阻止刷新后重复弹出 `QuestionCard`。
- 同时新建一个 round（`parent_run_id` 指向旧 round）承载 resume 之后的步骤。

前端实现与之一致：

- `handleResumeSubmit` 在调用 `resumeStream` 前向 `rounds` 数组追加一个新的 `running` 占位 round（`user_message` 用 `Q:/A:` 拼接的回答摘要）。
- 旧 round 仍展示在历史中作为可读记录，但其状态在拉取历史时会是 `resumed`（不再是 `interrupted`）。前端断言、测试 fixture 与 UI 渲染分支需基于 `resumed` 而非 `interrupted`。
- 这样保证刷新页面后，从后端拉到的多 round 结构与本地实时状态一致。

## 4. 滚动策略

| 场景 | 时机 | 实现 |
|---|---|---|
| 会话切换首次加载 | 历史渲染完成前 | `useLayoutEffect` 同步设置 `scrollTop = savedTop` |
| 流式新内容 | rounds 长度变化 | 仅 `isAtBottom` 时 `scrollIntoView({ behavior: 'smooth' })` |
| 用户滚动 | `scroll` 事件 | 更新 `isAtBottom`（容差 100px），同时保存 `scrollPosBySessionRef[sid] = scrollTop` |

**禁止**：
- 在普通 `useEffect` 里设置 `scrollTop`（会有跳动）。
- 无视 `isAtBottom` 强制滚到底（抢用户滚轮）。

## 5. 动画约定

- 首次渲染历史：`disableInitialMotion = true`，加载完关闭一次性 flag。
- 实时新内容：启用 `animate-fade-in`。
- `suppressAutoScrollRef` 用于阻止首次渲染时的自动滚动到底。

## 6. 轮询契约

ChatV2 不做定时轮询。Cron 任务执行结果**不**注入聊天 Session，由用户在「Cron 消息中心」面板查看（见 frontend-panel-spec §6）。

## 7. 推理面板（ReasoningPanel）

### 7.1 数据转换

`transformToDisplayBlocks(steps: StepData[]): DisplayBlock[]`

- `ThinkingBlock`：连续 `type === 'thinking'` 合并。
- `ToolGroupBlock`：连续工具调用合并（**跨 step**），但新的 `ThinkingBlock` 必须切断上一组工具调用；即使同一个 step 同时包含 thinking 与新的 tool_call，也不得把该 tool_call 合并进 thinking 之前的工具组。
- `NarrativeBlock`：其他文本步骤。若流式正文与工具调用暂时落在同一个 step，前端也必须先结束当前 ToolGroupBlock，再按 NarrativeBlock 处理正文，避免正文到达后仍显示工具结果处理中。

### 7.2 工具描述

`getToolDescription(tool_name, tool_args)`：

| 工具 | 模板 | 截断策略 |
|---|---|---|
| `read_file` | `Read {path}` | Keep-Head |
| `write_file` | `Write {path}` | Keep-Head，内容 Keep-Tail |
| `edit_file` | `Edited {path}` | Keep-Head |
| `bash` | `Run \`{cmd}\`` | Keep-Head |
| `search_*` | `Search "{query}"` | Keep-Head |
| `sub_agent` | `委派子任务 {description 或 prompt 首行}` | Keep-Head |
| 其他 | `{tool_name}` | — |

### 7.3 分组摘要

`getGroupSummary(group: ToolGroupBlock)`：
- 完成态：`Edited 2 files, read a file`
- 运行态：`Reading src/app.py...`（Typewriter Preview）

### 7.4 活动入口与抽屉

- ThinkingBlock / ThinkingGroupBlock：在单个 round 内聚合为一个思考入口，不按 step 或工具分隔重复渲染多个入口。
  - 进行中：渲染醒目的 `正在思考` 卡片，显示实时耗时、当前最新 thinking 全文与 `查看活动` 入口；外部内容不得省略截断，末尾用流式闪烁点表示仍在生成。
  - 完成态：渲染紧凑的 `已完成思考` 胶囊，显示整轮思考总耗时。
  - 点击 `查看活动` 或完成态胶囊后打开右侧活动抽屉；不得在主聊天区展开 thinking 详情。
- ToolGroupBlock：完成态不在主聊天区直接渲染；工具摘要、工具项、输入输出、`✓ Done` 标记均在活动抽屉内展示。若 round 没有 thinking 但存在已完成工具活动，主聊天区渲染紧凑的 `已完成活动` 入口，仅用于打开活动抽屉。只有 round 仍处于 streaming 时，最新 ToolGroupBlock 的未返回工具结果才可视为运行中；若最新 ToolGroupBlock 仍在运行且没有正在流式的 thinking，主聊天区必须显示 `正在调用工具` 活动态卡片和工具摘要。终态 round 中缺少 tool_result 的工具调用不得显示 `正在调用工具`。若工具已返回但 round 仍处于 streaming，且下一段 thinking/正文尚未到达，主聊天区必须显示 `正在处理工具结果` 活动态卡片，避免 think/tool 与下一段 think 之间的空窗期看起来已经完成。
- ToolItem：在活动抽屉内 hover 显示展开箭头，点击查看工具入参/结果。
- `sub_agent` ToolItem 必须使用专用子任务胶囊展示：折叠态保持单行，仅显示 `委派子任务`、业务标题、`subagent_type` 与耗时；不得默认展开原始 JSON 入参。点击展开后展示任务 prompt、子任务输出/错误与可选 child run id，便于理解长耗时委派执行。child run id 等父子 run 元数据应优先来自结构化 metadata 或 `subagent_runs` 查询；从 `TOOL_CALL_RESULT.content` 文本中解析仅允许作为当前兼容兜底。

## 8. 附件上传

- 图片：前端用 `imageUtils.compressImage` 压缩后再发（阈值 1MB）。
- 其他文件：通过 `apiService.uploadFile` 上传到沙箱，返回 `AttachmentInfo`。
- 消息体：附件以 `ChatContentBlock[]` 形式发送（`image_url` / `file` 等类型）。

## 9. 错误处理

| 错误 | 来源 | UI 表现 |
|---|---|---|
| `HttpError(4xx)` | axios 拒绝 | 显示错误 banner，**不重试** |
| `HttpError(5xx)` | axios 拒绝 | 显示错误 banner，允许用户重试 |
| `RoundExistsError` | `sendMessage` 冲突 | 静默切到 subscribe |
| SSE 断开 | EventSource error | 走 §3.3 恢复 |
| `onStreamError(msg, code)` | 后端 AG-UI `RUN_ERROR` | 显示错误 banner，`setBusyFalse` |

## 10. 测试清单

- [ ] 切换会话时旧 SSE 事件不污染新会话（mock 迟到事件）
- [ ] 取消后 UI 显示"已取消"而非"错误"
- [ ] 取消成功后无需等待 SSE，即刻恢复输入并允许重发
- [ ] ask_user 中断恢复：刷新页面后 QuestionCard 正常显示
- [ ] SSE 断连后自动恢复（history API 查询终态/续订）
- [ ] 幂等冲突自动切 subscribe
- [ ] 滚动记忆：A 滚到中间 → 切 B → 切回 A，位置恢复
- [ ] 首屏历史渲染无瀑布动画
- [ ] 底部跟随：用户滚离底部时新消息不强制滚动
- [ ] 助手文件提示行/路径保留在 markdown 正文，同时在回复底部渲染去重文件卡片；点击后打开 Files 抽屉并直接预览目标文件
- [ ] 代码块内的文件名/命令不触发文件卡片

## 11. 已知易错点

1. 忘记在新的 useEffect 里加 `boundSessionId` 校验。
2. 新增 SSE 事件类型时忘记在 `services/api.ts` 的 dispatcher 注册。
3. `ToolResult` 很大时直接渲染导致卡顿 → 用 `TruncatedCodeBlock`。
4. `applyPatch` 失败时吞异常 → **必须 console.error**，否则 state 偷偷停更。
5. 取消后未清除 `sending` → 输入框卡死。
