# 前端 Session Spec — 会话列表与切换

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[sessions-spec.md](./sessions-spec.md)

覆盖组件：`SessionList.tsx`、`App.tsx`（sessionId 顶层协调部分）。

## 1. 模块职责

- 会话列表拉取与展示
- 会话标题 + 讨论内容搜索
- 新建 / 删除 / 切换会话
- App 启动时检测运行中会话集合，并仅在首次恢复时自动选中第一个运行中会话
- 切换会话时通知下游组件（`ChatV2` 重新加载，`ModelSelector` 同步）

**不职责**：
- 会话内消息 → `ChatV2`（见 frontend-chat-spec）
- 会话重命名（标题由后端自动更新，前端仅在 `onTitleUpdated` 触发时刷新）

## 2. 顶层状态（App.tsx）

```ts
const [currentSessionId, setCurrentSessionId] = useState<string>('');
const [refreshTrigger, setRefreshTrigger] = useState(0);         // 触发 SessionList 重拉
const [executingSessionIds, setExecutingSessionIds] = useState<Set<string>>(() => new Set());
const [activeSlotSessionIds, setActiveSlotSessionIds] = useState<Set<string>>(() => new Set());
const [selectedModelId, setSelectedModelId] = useState<string>('');
```

## 3. 数据流

```
App 挂载
  → GET /api/sessions/running-sessions (reconcileRunningSessions, 立即一次 + 5s 周期)
      → App 标记全部运行中 session 与 active slot，首次 reconcile 返回运行态且当前无 session 时自动 setCurrentSessionId(first)

SessionList 挂载
  → GET /api/sessions/list             (loadSessions)

用户输入搜索词
  → 300ms debounce
  → GET /api/sessions/list?q=...        // 后端默认 keyword，保证侧栏即时搜索速度
  → stale guard 校验请求序号
  → SessionList 展示匹配 sessions / match_excerpt

用户点击会话
  → onSessionSelect(sid, {roundId?})
  → App 更新 currentSessionId + selectedModelId + scrollTarget
  → ChatV2 检测 sessionId 变化 → loadHistory
  → 普通会话点击（无 roundId）定位到最新消息
  → 若 search result 带 match_round_id，则滚动到对应 round 并短暂高亮

用户新建会话
  → onNewChat → App.setCurrentSessionId('')
  → ChatV2 显示欢迎页
  → 用户输入第一条消息 → onCreateSession(modelId) → POST /api/sessions
  → setCurrentSessionId(newSid) → sendMessage
```

## 4. 核心不变量

### 4.1 新会话"输入即创建"

**禁止**：点击"新建"立即创建空会话。

**正确**：点击"新建" → `setCurrentSessionId('')` → 欢迎页 → 用户输入第一条消息时才 `POST /api/sessions`。

原因：避免大量空会话污染列表。

### 4.2 运行中会话集合由 App 统一收敛

`SessionList` 不得请求 `/running-sessions`。运行态集合统一由 `App.reconcileRunningSessions` 维护：挂载后立即执行一次，之后 5s 周期执行。首次 reconcile 响应若返回运行态且 `currentSessionId` 为空时，可以自动选择第一个运行中会话；该自动选择机会只在首次 reconcile 响应中消耗一次。若首次响应为空，后续周期只同步运行态集合，不再自动切换当前会话，避免用户已在欢迎页或当前会话操作时被后台跳转打断。

```ts
useEffect(() => {
  void reconcileRunningSessions();
  const timer = setInterval(() => {
    void reconcileRunningSessions();
  }, RUNNING_SESSIONS_RECONCILE_INTERVAL_MS);
  return () => clearInterval(timer);
}, [reconcileRunningSessions]);
```

### 4.3 删除当前会话后清空

```ts
if (currentSessionId === sessionId) {
  onSessionSelect('');  // 清空，回到欢迎页
}
```

### 4.4 切换会话同步模型

```ts
onSessionSelect(session.id);
if (session.model_id && onModelChange) {
  onModelChange(session.model_id);   // ModelSelector 同步显示
}
```

原因：不同会话可能用不同模型，顶部 ModelSelector 必须反映当前会话的模型。

### 4.5 执行标记集合的清除时机

`executingSessionIds` 清除路径：
- `RUN_STARTED` / 本地发送开始 → ChatV2 调用 `onExecutionStart(sessionId)`，将 sid 加入集合。
- `RUN_FINISHED` → ChatV2 调用 `onExecutionEnd(sessionId)`（传 sid，精确清除）。
- **禁止**在切换会话时无条件清除 → 会误清其他正在运行的会话标记。

```ts
const handleExecutionStart = (sessionId: string) => {
  setExecutingSessionIds((prev) => new Set(prev).add(sessionId));
};

const handleExecutionEnd = (sessionId?: string) => {
  if (sessionId) {
    setExecutingSessionIds((prev) => {
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
  } else {
    setExecutingSessionIds(new Set());
  }
};
```

`activeSlotSessionIds` 保存后端 `/running-sessions` 返回或本地已启动的运行 slot。本地发送 / resume 在 HTTP 响应通过后、`RUN_STARTED` 到达前也必须先加入该集合；429 等请求级拒绝不得加入。若 ChatV2 加载当前 session 历史时尚未看到 `running` round，但该 session 仍在 `activeSlotSessionIds` 中，必须保留执行标记并禁用输入；这表示 Agent 初始化窗口，不能误判为空闲。进入该分支后 ChatV2 必须短间隔检查最新 `activeSlotSessionIds` prop/ref：slot 消失则清除执行态，slot 仍存在则重拉 history，直到看到 `running` round 并进入 `subscribeToRound` 订阅路径。ChatV2 不得为该探测额外请求 `/running-sessions`。

`App` 必须立即并周期性 reconcile `/running-sessions`，更新 `executingSessionIds` 与 `activeSlotSessionIds`。除首次恢复运行态外，不得自动切换当前会话。该收敛用于清理非当前会话后台完成后的侧栏执行标记。

## 5. 轮询契约

| 轮询 | 间隔 | 实现 |
|---|---|---|
| init-window 补偿探测 | 1.5s | `ChatV2` 在 active slot 但无 running round 时触发，通过最新 `activeSlotSessionIds` 判断是否重拉 history 或清标记 |
| running-sessions 后台收敛 | 5s | `App` 立即并周期调用 `/running-sessions`，同步运行态集合；仅首次 reconcile 返回运行态且当前为空时可自动跳转 |
| 会话列表刷新 | 30s | `SessionList` 内部 `setInterval` |
| Cron 未读计数 | 60s | `App.tsx` 内部 `setInterval`，调用 `getUnreadCount` |

触发列表重拉的其他入口：
- `refreshTrigger` 变化（新建/删除/标题更新时 +1）
- `currentSessionId` 变化（从 useEffect 依赖触发）
- `debouncedSearchQuery` 变化（搜索词 300ms 防抖后触发）

## 6. 搜索交互

- 搜索框位于 History 区域上方，使用 `Search` 图标、清空按钮和加载状态。
- 搜索使用单一后端 `q` 参数，不暴露也不保留搜索模式选择。
- 搜索输入只影响会话列表，不改变当前选中 session。
- 搜索为空时请求完整列表；搜索非空时调用 `GET /api/sessions/list?q=...`。
- 搜索结果由后端限制为最多 50 个 sessions，前端不做额外分页。
- 快速输入必须使用请求序号 stale guard，旧响应不得覆盖新结果。
- 搜索非空且无结果时显示"没有匹配的对话"。
- `match_type=user|assistant` 且有 `match_excerpt` 时，在 session 标题下展示一行摘要，前缀分别为"我的问题"、"Agent 回复"。
- 搜索结果带 `match_round_id` 时，点击 session 后 `ChatV2` 应定位到对应 round 并短暂高亮；无 `match_round_id` 的普通点击进入则定位到最新消息。

## 7. 折叠交互

- 打开右侧配置抽屉（AgentConfig/Skills/Cron）时自动折叠左侧栏：`setIsSidebarCollapsed(true)`。
- 关闭配置抽屉时恢复：`setIsSidebarCollapsed(false)`。
- 聊天区的 ArtifactsPanel 打开**不折叠**左侧栏（用户可能需要对照会话）。

折叠动画：`width: 260px ↔ 0` + `opacity: 1 ↔ 0`，`transition-all duration-300 ease-in-out`。

**注意**：这是**侧边栏折叠**，不是抽屉。右侧抽屉仍必须覆盖式（见 frontend-spec §5.6）。

## 8. 错误处理

| 错误 | 表现 |
|---|---|
| `getSessions` 失败 | `console.error`，显示"加载失败"空态 |
| `deleteSession` 失败 | `console.error`，不改变 UI |
| `getRunningSessions` 失败 | `console.error`，不影响正常流程 |

## 9. 测试清单

- [ ] 首次进入由 App 自动检测全部运行中会话，并切换到第一个运行中会话
- [ ] 切换会话时 `ModelSelector` 同步到新会话的 model_id
- [ ] 删除当前会话回到欢迎页
- [ ] 欢迎页输入第一条消息才创建会话
- [ ] A/B 多会话并行时，侧栏同时显示多个执行标记
- [ ] 非当前会话后台完成后，周期收敛会清理对应执行标记
- [ ] 本地 run 在 `RUN_STARTED` 前的 init-window 会保留执行标记；429 不污染执行标记
- [ ] ChatV2 init-window 补偿探测不额外请求 `/running-sessions`
- [ ] A 会话运行中切到 B 会话，A 的 `executingSessionIds` 标记不被误清
- [ ] 30s 后列表自动刷新
- [ ] 搜索输入 300ms 后调用 `getSessions(q)`
- [ ] 清空搜索恢复完整列表
- [ ] 用户消息 / Agent 回复命中展示 `match_excerpt`
- [ ] 搜索无结果展示"没有匹配的对话"
- [ ] 快速输入时旧响应不覆盖新结果
- [ ] 点击带 `match_round_id` 的搜索结果后定位到对应 round

## 10. 已知易错点

1. `useEffect(() => loadSessions(), [refreshTrigger, currentSessionId])` —— 切换会话也会触发 reload，属于期望行为，不要误删 `currentSessionId` 依赖。
2. 在 `onSessionSelect` 里做异步操作会阻塞 UI → 保持同步。
3. 运行中检测写成每次挂载都执行 → 切会话时 UI 闪跳。
