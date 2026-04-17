# 前端 Chat Spec — 聊天 / SSE / 推理面板

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[chat-spec.md](./chat-spec.md)

覆盖组件：`ChatV2.tsx`、`Round.tsx`、`ChatInput.tsx`、`ReasoningPanel.tsx`、`QuestionCard.tsx`。

## 1. 模块职责

- 发送用户消息（含附件、引用图片）
- 消费后端 SSE（AG-UI 事件）增量构建 `RoundData[]`
- 渲染消息流（user → reasoning → assistant）
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
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled' | 'resumed'
  user_text: string
  user_attachments: AttachmentInfo[]
  steps: StepData[]
  final_response?: string
  interrupt?: InterruptDetails       // ask_user 中断
  step_count?: number
}

StepData {
  type: 'thinking' | 'tool_call' | 'text'
  content: string
  tool_name?: string
  tool_args?: any
  tool_result?: any
  start_time?: number
  end_time?: number
}

AgentState {
  status: 'idle' | 'running' | 'waiting' | 'error'
  lastUpdated: number
  // ...其余字段通过 JSON Patch 增量更新
}
```

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
      → 若 status ∈ {completed, failed, interrupted, resumed, cancelled}
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
1. 前端立即 `abortController.abort()`（断开 SSE）。
2. POST `/api/sessions/{sid}/cancel`。
3. 后端发送 `RUN_FINISHED(outcome=interrupt, result.reason=user_cancelled)`。
4. `isUserCancelledOutcome()` 识别后，UI 显示"已取消"，**不是错误**。

**判定**：`outcome === 'interrupt' && result?.reason === 'user_cancelled'`。outcome=interrupt 但无 reason 的保守处理为非取消。

### 3.6 ask_user 中断恢复

- `loadHistory()` 若发现最新 round `status === 'interrupted' && interrupt`：
  - `setPendingInterrupt(interrupt)` + `agentState.status = 'waiting'`
  - 渲染 `QuestionCard` 供用户回答
- 用户回答 → 继续调用后端 `resume` 接口（携带答案）。

### 3.7 多 RUN 同一 Round

一个 round 可能包含多个 RUN（ask_user resume 后新增 RUN）。前端按 `runId` 区分，但 UI 内聚合在同一个 `RoundData.steps` 下。

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

| 轮询 | 间隔 | 触发 | 目的 |
|---|---|---|---|
| `pollSession(sid)` | 1s | `sessionId && !sending` | 检测系统注入的新 Round（如 Cron 执行结果） |

发现 `round_count > knownRoundCountRef.current` → `loadHistory()`。

**禁止**：SSE 订阅期间并行 `pollSession`。

## 7. 推理面板（ReasoningPanel）

### 7.1 数据转换

`transformToDisplayBlocks(steps: StepData[]): DisplayBlock[]`

- `ThinkingBlock`：连续 `type === 'thinking'` 合并。
- `ToolGroupBlock`：连续 `type === 'tool_call'` 合并（**跨 step**）。
- `NarrativeBlock`：其他文本步骤。

### 7.2 工具描述

`getToolDescription(tool_name, tool_args)`：

| 工具 | 模板 | 截断策略 |
|---|---|---|
| `read_file` | `Read {path}` | Keep-Head |
| `write_file` | `Write {path}` | Keep-Head，内容 Keep-Tail |
| `edit_file` | `Edited {path}` | Keep-Head |
| `bash` | `Run \`{cmd}\`` | Keep-Head |
| `search_*` | `Search "{query}"` | Keep-Head |
| 其他 | `{tool_name}` | — |

### 7.3 分组摘要

`getGroupSummary(group: ToolGroupBlock)`：
- 完成态：`Edited 2 files, read a file`
- 运行态：`Reading src/app.py...`（Typewriter Preview）

### 7.4 展开态

- ThinkingBlock：默认折叠，点击展开显示完整思考内容。
- ToolGroupBlock：默认展开，完成后显示 `✓ Done` 标记。
- ToolItem：hover 显示展开箭头，点击查看工具入参/结果。

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
- [ ] ask_user 中断恢复：刷新页面后 QuestionCard 正常显示
- [ ] SSE 断连后自动恢复（history API 查询终态/续订）
- [ ] 幂等冲突自动切 subscribe
- [ ] 滚动记忆：A 滚到中间 → 切 B → 切回 A，位置恢复
- [ ] 首屏历史渲染无瀑布动画
- [ ] 底部跟随：用户滚离底部时新消息不强制滚动

## 11. 已知易错点

1. 忘记在新的 useEffect 里加 `boundSessionId` 校验。
2. 新增 SSE 事件类型时忘记在 `services/api.ts` 的 dispatcher 注册。
3. `ToolResult` 很大时直接渲染导致卡顿 → 用 `TruncatedCodeBlock`。
4. `applyPatch` 失败时吞异常 → **必须 console.error**，否则 state 偷偷停更。
5. 取消后未清除 `sending` → 输入框卡死。
