# 前端 Chat Spec — 聊天 / SSE / 推理面板

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[chat-spec.md](./chat-spec.md)

覆盖组件：`ChatV2.tsx`、`Round.tsx`、`ChatInput.tsx`、`SessionList.tsx`、`WorkspaceSidebarContent.tsx`、`WorkspaceFilesPanel.tsx`、`WorkspaceFilePicker.tsx`、`ReasoningPanel.tsx`、`QuestionCard.tsx`。

## 1. 模块职责

- 发送用户消息（含附件、引用图片）
- 消费后端 SSE（AG-UI 事件）增量构建 `RoundData[]`
- 渲染消息流（user → reasoning → assistant）
- 选择并发送仅作用于当前逻辑执行链的 Skill/MCP 统一偏好
- 从用户持久工作区选择冻结版本附件，并投影工作区资源变更
- 将后端结构化助手文件引用投影为回复底部卡片，并统一在聊天右侧文件面板打开
- 处理暂停/恢复：断连重连、same-Round ask_user/工具审批、用户主动取消
- 滚动控制：普通进入定位最新消息、搜索命中定位 round、流式底部跟随

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
  workspace_resources?: WorkspaceResource[]
  preferred_skills: PreferredSkillSnapshot[]
  preferred_mcp_connections: PreferredMcpConnectionSnapshot[]
  final_response: string | null
  steps: StepData[]
  step_count: number
  status: 'running' | 'waiting_interaction' | 'completed' | 'failed' | 'cancelled' | 'max_steps_reached'
  created_at: string
  completed_at?: string
  interrupt?: InterruptDetails       // same-Round 提问或审批
}

PreferredSkillSnapshot {
  key: string
  display_name: string
}

PreferredMcpConnectionSnapshot {
  server_id: string
  display_name: string
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

TurnPreferenceDraft {
  skillKeys: string[]     // GET /api/config/skills 返回的稳定内部 key
  mcpConnections: PreferredMcpConnectionSnapshot[] // client-only id + 冻结展示名
  revision: number        // 乐观清空与失败恢复的并发保护版本
}
```

`Round` 只在用户消息下直接展示 `created_at`，不依赖悬停：当天显示“今天 HH:mm”，最近 6 个日历日显示“星期X HH:mm”，同年更早显示“M月d日 HH:mm”，跨年显示“yyyy年M月d日 HH:mm”。助手区域不展示 `completed_at` 或其他完成时间；`completed_at` 仅保留为运行状态与历史审计数据。

### 2.1 左栏工作区与右侧文件 owner

- 右侧标签点击与左树打开复用 App 的 entry 导航入口，同步当前文件与 `entry/path` URL；关闭当前标签时以 replace 更新为相邻标签，关闭非当前标签不改变 URL，关闭最后一个标签清除 entry 深链。普通切标签保留编辑器实例和草稿，不额外请求 metadata。
- 工作区不是 Schedule/Skills/Data 同级 primary surface。桌面 `SessionList` 使用 WAI-ARIA `tablist` 在“会话 / 工作区”间切换；切换只替换左栏投影，`ChatV2` 始终保持挂载和可见。
- `/workspace` 是工作区 mode 的可恢复 URL，`/workspace?entry={entry_id}` 是文件深链；两者仍投影 `chat + workspace sidebar (+ WorkspaceFilesPanel)`，不得挂载第二套一级页面。点击工作区必须把 URL 归一到 `/workspace`；仅查看左侧会话列表不得清除工作区深链，选择具体会话或新建对话才归一回 `/`。从会话切回工作区必须立即刷新根目录、已展开目录和当前有效搜索，但不得卸载树或丢失展开、搜索、活动文件状态。硬刷新首帧按 pathname 同步选中 mode，不得等待 effect、会话清单或 entry 请求后再切换。
- entry 深链与应用内点击共用同一套 owner 切换规则：route effect 在调用栈内触发当前 Session dirty 内容抓取到持久 outbox，然后立即请求 entry，并在请求成功后一次性提交 target 与 URL。渲染目标必须从当前 URL entry 与已解析 target 的一致性同步派生：浏览器 back/forward 已到 B 但 B 尚未解析时，保留 A 的面板实例和草稿但整面隐藏 A，显示加载壳；成功后首帧只展示 B。解析失败必须先以 `replace` 回到 `/workspace` 或最后已提交 URL，再恢复对应 target，并用 `role=alert` 显示错误；任何时刻不得出现 URL 指向 B、右侧仍显示 A 的状态。不得等待远端保存，也不得因编辑器 handle 暂时缺失阻塞导航。
- `WorkspaceSidebarContent` 负责搜索、cursor 全量分页、lazy 文件树、新建目录/Markdown/XLSX、上传、重命名、拖拽移动和直接删除；树支持方向键和 Enter。mode 行右侧的顶部三点永远以工作区根目录为目标，新建、上传和刷新不得复用最近展开/点击的文件夹。文件行右侧三点通过 Portal 打开 140px 紧凑菜单，只含“重命名 / 删除”；文件夹行右侧三点打开 172px 目录菜单，为该文件夹提供“新建文件夹 / 新建 Markdown / 新建表格 / 上传文件 / 刷新”，在分隔线后保留“重命名 / 删除”。移动不重复提供菜单或 Dialog；任意层级文件/文件夹的“图标 + 名称”主交互面使用可聚焦的 `role=button` 与 Pointer Events，移动超过 6px 后进入拖拽态，并由 `window` 级 `pointermove/up/cancel` 持续跟踪离开源行后的指针，通过实际命中元素的 `data-workspace-drop-target` 选择目标；不能依赖会被原生按钮、文本选择或连续重渲染打断的 HTML5 `draggable`。进入拖拽态后，指针右下必须有 `pointer-events:none` 的固定浮层跟随，展示同一格式图标与文件/文件夹名称；位置更新按 animation frame 合并，不能推动文件树布局，松手或取消时立即移除。文件与文件夹均可直接拖入其他目录；移回根目录只使用拖拽时出现的显式根 drop zone，整个内容区不得充当隐形根目标。拖到当前父目录必须保持 no-op，连续移动必须逐次使用服务端返回的新 revision，不能在第三次拖拽时复用旧 entry。新建 Markdown/XLSX 不弹命名框，直接使用 `未命名.md|xlsx`（冲突时自然递增）并立即在右侧打开；只有文件夹保留紧凑命名 Dialog。文件重命名只编辑主文件名，原格式后缀固定展示并由前端自动拼回；不得要求用户重新输入后缀，也不得通过该入口改变文件格式。
- 工作区树与搜索结果使用独立于 `activeEntryId` 的稳定 `entry_id` 多选：checkbox 在 hover/focus 时出现，进入选择态后常驻；名称普通点击仍只打开/展开，`Shift` 选择当前可见范围，`Ctrl/Cmd` 切换，`Space` 切换焦点行，`Ctrl/Cmd+A` 只选择当前视图，`Escape` 清空。checkbox 获得焦点时也必须支持这些选择快捷键；anchor 不在当前可见视图时，下一次 Shift 退化为以当前项重建 anchor 的单选。选择状态只保存 ID，提交前从当前目录/搜索响应和已确认 mutation 响应组成的权威实体缓存按最高 revision 重新解析；每个目录与有效搜索作用域的新响应必须淘汰该作用域中已消失且未被更高 revision mutation 取代的旧实体，找不到的 ID 保留选择、显示失效状态并阻止提交，禁止回退到旧 entry 快照。目录代表一个子树根，明显的已选后代可在前端归并，但服务端 batch 是最终权威。新搜索开始、query 清空或 mode 切换时必须同步清旧 results、失效旧 request 并结束对应 searching；旧结果不得继续成为 Shift/Ctrl+A/batch 来源。底部批量栏展示“已选 N / 清除 / 删除”，一次确认后只发送一次 `POST /workspace/entries/delete-batch`，不得并发调用单项 DELETE；单项菜单删除也必须复用同一链路。同一 UI intent 只生成一个 idempotency key，网络失败、响应丢失和用户重试必须复用，只有成功、显式取消或选择范围改变后才能清除。删除确认说明“永久删除，无法恢复，未保存草稿也将丢弃”，不先保存即将删除的内容。服务端成功后按 affected_entry_ids 同步清除本地 outbox/retry/checkpoint 与迟到更新；失败保留草稿和选择。成功响应按 `affected_entry_ids` 一次性移除树/搜索缓存、关闭相关右侧标签并清理失效深链，成功用 `role=status`，冲突/失败保留选择并用 `role=alert`。不提供回收站、恢复、清空、清理进度轮询或旧删除 API 兼容。
- 展开目录的整个子树区域都是该目录的投放面：目录标题行以自身 `entry_id` 为目标，普通子文件行与子列表空隙继承父目录 `entry_id`；拖到其他文件上或文件之间都必须等价于拖到父目录标题。嵌套目录行仍以自身为目标，根级普通文件不得把整棵树变成隐形根目录投放区。命中父目录时，目录行与子列表区域使用同一轻量高亮。
- `SessionList` 顶部固定高度 `h-9` 搜索槽始终是“搜索会话”，切到工作区不得改变其 placeholder/value；primary nav 与 44px 轻量文字 tabs 的高度/Y 位置不随 mode 变化。输入搜索词首帧显示忙碌反馈且清空按钮始终可操作；清空时立即恢复最近一次完整列表缓存并使旧搜索响应失效，同时后台刷新无查询列表。会话 mode 右侧固定 32px `+` 新建入口，不显示 `HISTORY` 眉题，列表使用固定 48px 两级信息行。工作区搜索由 mode 行右侧 Search icon 打开 anchored popover；省略号菜单通过 Portal 从按钮右侧 8px 展开，宽 172px、字号 12px，Escape/点击外部/resize/scroll 均关闭且不占侧栏布局。
- 工作区文件树所有 flex owner 必须 `min-w-0` 并受 tabpanel 实际宽度约束；工作区 tabpanel 抵消侧栏通用 `p-4` 的左右 12px，仅保留 4px 外侧 gutter，把宽度优先留给二级文件名。根目录、一级目录和二级目录内的长目录/文件名都只截断文本，不得撑宽树、产生横向滚动、压缩类型图标或把行尾三点挤出侧栏；悬停截断名称时必须通过原生 `title` 展示完整名称。工作区树、附件选择器和右侧文件标签统一复用 `getFileIcon/getFileIconClass` 的格式族映射，文档、表格、演示、代码、图片、压缩包与其他文件不得退化为同一图标；图标使用固定尺寸和 `shrink-0`。行尾按钮右侧保留至少 12px 视觉间距。
- 工作区树同层文件与文件夹共用 28px 展开槽；子级 group 仅轻量内缩，选中底色随 group 移动，不使用竖向引导线或大块左留白。目录最多两层：根目录下可建一级文件夹，一级文件夹内可建二级文件夹，二级文件夹菜单不显示“新建文件夹”。收到带 authoritative entry 的 `REVISION_CONFLICT` 时同步刷新树行与当前操作对象；删除等破坏性动作保留确认框并要求基于最新 revision 再次确认，不能让“重试”继续提交旧 revision。
- 横向裁剪只能放在文件树滚动内容层；`WorkspaceSidebarContent` 根层必须允许垂直溢出，否则向上覆盖 mode 行的搜索/三点工具栏会被裁掉并失去点击能力。
- 会话或工作区请求 pending/reject 都不得用整栏 spinner 遮住稳定骨架。会话列表使用固定 48px 行形骨架并采用 8 秒导航级请求超时，超时/失败投影在对应 panel 内并提供重试；空会话、空工作区和搜索无结果在账户栏上方的剩余 panel 内水平/垂直居中。
- 点击工作区文件在聊天右侧 `WorkspaceFilesPanel` 打开多标签可编辑工作台；Markdown 相对资源必须走 authoritative workspace path API，不能回到当前 Markdown 文件内容。
- 工作区主文件预览 URL 包含 `preview=true` 与选中 entry 的 `version_id=current_version_id`，正文与编辑 base 使用同一版本；Office 转 PDF 在其上追加 `render=pdf`。Markdown 相对资源仍走 authoritative path API，下载不指定版本时读取请求开始时的当前 head。
- Session 与 Workspace 文件面板使用互斥 owner：`{scope:'session', id:sessionId, epoch}` 与 `{scope:'workspace', id:'persistent', epoch}`。覆盖 owner 前同步抓取原 owner dirty 内容到应用级 outbox，随后立即切换；远端失败由 outbox 重试且不得写入 chat runtime 或显示阻塞提示。
- `md` 以下从聊天顶栏“对话”入口把同一个常驻 `SessionList / WorkspaceSidebarContent` owner 投影为全屏 Sheet，关闭时只隐藏、不得再挂载第二套树；桌面隐藏实例与移动 Sheet 不能同时发请求或各自维护展开/搜索/选择状态。Sheet 使用 modal dialog 语义，打开时背景内容必须 `inert + aria-hidden`，Tab/Shift+Tab 在侧栏内部回绕，Escape/关闭按钮退出并恢复触发点焦点。不得恢复第五个工作区 primary nav。

### 2.2 助手文件引用卡片

`Round` 按原文渲染助手 markdown；文件身份只来自持久化的 `assistant_file_references`，不得从反引号、提示行、同名路径或工具结果文本猜测 namespace。未知或歧义的本地 Markdown 链接保持不可点击文本；正文内容不因卡片投影被删除或替换。

结构化引用固定分为：

- Session：`source/session_id/path/revision/ref_id`。只有 Chat 主 Agent 显式调用 `present_files` 才用 no-follow stat 读取当前 metadata 并产生卡片；`bash`、`bash_output`、`apply_patch` 和助手正文都不能自动产生展示引用，也不复制生成时字节。
- Workspace：`source/entry_id/path/revision/version_id/ref_id`。只接受成功正式 mutation；`workspace_change_proposed/conflict`、`NO_CHANGE`、delete 和内部审计记录不产生卡片。源 entry 存在时保护 version；显式删除 entry 后同时删除其版本与引用，不再提供历史兜底。
- 同一 Round 内 Session 按 `session_id + path`、Workspace 按 `entry_id` last-write-wins。子 Agent 不具备 `present_files`，只把产物路径报告给主 Agent，由主 Agent 决定最终交付；规范化 Workspace 引用不含原 mutation 的 `kind` 字段，父任务按 `user_id + entry_id + version_id` 校验并保护版本，不能因此丢弃引用或用同名 Session 副本替代。两种来源不按文件名合并。

点击与布局遵循一个入口、一套预览：

- 文件卡片位于助手 markdown 后方，使用最大宽度 520px、最小高度 52px 的紧凑附件行；整行是唯一点击目标，不提供“当前版本 / 生成时版本”双按钮。
- 点击 Workspace 卡先按原稳定 `entry_id` 读取当前 active 实体；存在则在右侧 `WorkspaceFilesPanel` 打开最新内容。不得按同名路径串到另一个 entry，也不得自动恢复或重建。
- 点击 Session 卡先按原 `session_id + path` 重新读取父目录 authoritative metadata，再在右侧 `ArtifactsPanel` 打开当前内容；历史 event 的旧 size/mtime/revision 不得直接命中预览缓存。
- Workspace 实体已删除时显示错误并停止，不请求历史 version 或按同名路径替换；Session 文件已删除时显示不可用，不回退历史副本。
- 恢复到工作区属于独立、用户明确触发的写操作。卡片点击和预览本身永远不得创建、恢复、移动或重命名文件。
- FilePreview 将 owner identity 与 content identity 分离：同一 current Workspace `entry_id` 的新 version 属于同一 owner，可在 clean/ready 后台刷新；`version_id/snapshot_path/revision/ref_id/preview URL` 只决定内容与缓存。captured 与 current 必须属于不同 owner，captured/read-only 模式禁止读取 outbox 草稿或沿用 current 正文。

## 3. 核心不变量（Critical Invariants）

### 3.1 会话隔离（最关键）

所有 chat transport 事件必须先包装成稳定归属的 envelope：

```ts
interface StreamEnvelope {
  ownerSessionId: string;
  clientRunKey: string;
  transportEpoch: number;
  connectionId: string;
  event: AGUIEvent;
}
```

`ChatRuntimeProvider.guardAndDispatch()` 只有在 registry 中的 current epoch 与 connection id 都匹配时才允许 reducer 更新对应 `ownerSessionId` 分区；旧 transport 的 finally、错误或迟到事件不得删除/覆盖新 transport。history 还必须通过 request id 与 stream watermark 防止旧快照覆盖新事件。

**违反后果**：A 会话消息污染 B 会话 UI。

### 3.2 接受边界与执行标记

`stream_accepted` 只可建立本地 active-slot/init-window 保护；direct run 以 `RUN_STARTED` 通知真正执行中，避免 429 或仅传输接受污染执行标记。same-Round resume 不发新 `RUN_STARTED`：`interaction_resolved` 才表示原 Round continuation 已启动；`interaction_requested` 则结束本段执行标记并进入 waiting UI。

### 3.3 SSE 断连恢复流程

```
catch (SSE error)
  → GET /api/sessions/{sid}/history/v2
    → 找到目标 round
      → 先完整投影 Round.steps / final_response / interrupt，再推进 lastSequence
      → 若 status ∈ {completed, failed, cancelled, max_steps_reached}
          → HISTORY_LOADED / authoritative recovery envelope → reducer 收敛终态
      → 若 status ∈ {running, waiting_interaction}
          → startSubscribeForRound(sid, roundId, lastSequence)
```

订阅断连且目标 Round 仍为 `running` 或 `waiting_interaction` 时，前端最多静默重试 3 次；重试期间不得展示错误横幅。waiting 订阅用于跨标签页接收 `interaction_resolved`、后续输出、取消和终态，不计为 `sending`。重试耗尽后才展示刷新提示。用户点击 Stop 时必须先清除该 Round 已安排但尚未执行的 retry timer，并使旧 transport identity 失效，再发起 abort；旧 timer 不得在取消窗口重建订阅或用较早 history 恢复 waiting。

history 的 `last_event_sequence` 只有在同一 snapshot 的 `steps/final_response/interrupt/status` 已完整投影后才能成为新 cursor；禁止保留局部本地 steps 却直接跳到服务端高水位。text / thinking / tool args delta 在 END 前是 live-only、没有 durable sequence；全局 cursor 变大只证明某个 durable 事件已提交，不证明每个交错 segment 都已写入 aggregate。只要本地仍有 dirty segment，history 必须逐 segment 证明其对应 projection 已包含本地前缀（工具参数以同 `tool_call_id` 的持久化调用为证），否则即使 cursor 更高也要保留本地 projection 与 buffer。direct / resume 内嵌 subscribe 若恢复出非终态，必须以结构化 handoff 把同一 `clientRunKey + roundId + cursor` 交给 Provider 继续 subscribe，不得合成 Round terminal `RUN_ERROR`。无 durable sequence 的 `SUBSCRIBE_FAILED` 属于 transport 控制错误；只有持久化或 history 权威恢复的 `RUN_ERROR` 才能终态化 Round。

一旦收到新的持久化 `interaction_requested`，它就是权威等待边界。即使紧接着断网且 history 查询也失败，前端也必须保留新卡片和 `waiting_interaction`，不得再合成 `RUN_ERROR` 覆盖它。

`_ROUND_TERMINAL_STATUSES` 必须与后端 `Round.SUBSCRIBE_TERMINAL_STATUSES` 保持一致。

#### 3.3.1 初始 POST 接受歧义状态机

初始 `message/stream` POST 在响应头到达前发生网络错误时，必须按以下状态机处理：

```text
pre_accept_pending
  ├─ 收到响应头 / stream_accepted ───────────────→ accepted
  ├─ 确定性 HTTP 4xx/5xx ───────────────────────→ definite_rejected
  ├─ 响应头前网络错误 ──────────────────────────→ ambiguous
  │    ├─ history 命中同 idempotency_key ───────→ accepted（订阅 running/waiting 或收敛终态）
  │    ├─ 3 次 history 全部成功且均无匹配 ──────→ definite_rejected
  │    └─ 3 次中任一次失败且最终未命中 ─────────→ ambiguous_unknown
  └─ 用户主动取消 ──────────────────────────────→ client_cancelled_unknown
```

- `ambiguous` 期间绝不重发 POST；固定使用原 `idempotency_key` 查询 history，当前确认预算为 3 次。
- 响应头已到达的确定性 4xx/5xx 直接进入 `definite_rejected`，不进入 history 确认或自动重发；5xx 只提供用户显式重试入口。
- 只有 3 次查询全部成功且都无匹配，才能调用一次 `onRejectedBeforeAccept`、恢复乐观清空的草稿并提示请求失败。任一次查询失败都会使“无匹配”证据不完整；预算耗尽后保持 `ambiguous_unknown`，提示刷新查看，禁止恢复草稿、提示重新发送或用新幂等键自动发送。
- 用户在响应头前取消：立即 abort POST，停止本地订阅，不启动 history 确认，不发出 `stream_accepted` / `RUN_ERROR` / 接受前拒绝回调，并保持乐观清空后的草稿，防止服务端其实已接受时重复发送。
- 用户在等待 history 时取消：停止后续确认，忽略在途 history 的迟到结果；即使迟到结果命中 running / waiting Round，也不得建立 subscribe。该路径同样不恢复草稿、不自动重发，用户只能刷新查看服务端事实。

### 3.4 幂等冲突走订阅

`sendMessage` 抛 `RoundExistsError(roundId, status)` 时：
- 不重发
- 直接 `subscribeToRound(sid, roundId)` 进入订阅

### 3.5 取消语义

用户点击取消：
1. 前端点击后必须立即取消当前订阅（`subscription.abort()`），防止后续迟到回调覆盖状态。
2. 本地将当前 `running` 或 `waiting_interaction` Round 先收敛为取消态 `cancelled`（用于即时反馈），结束 `sending/resuming` 并移除交互卡；不得等待 `/abort` HTTP 响应或 SSE 终态事件。
3. 进入 `stopping` 状态：输入框保持可编辑，但新的发送动作必须禁用，直到 `/abort` 返回，避免用户立即发送新问题时撞到后端尚未释放的 user/session lock。
4. 同步发起 POST `/api/chat/{sid}/abort`。
5. 若请求返回 409（会话已无运行任务）：按“已停止”处理，保持本地已收敛 UI。
6. 成功响应中的 `outcome_warning` 仅作为后端诊断信息，聊天页不再展示独立提示；取消状态保持成功。
7. 其他请求失败：重新拉取历史以恢复真实运行态，并提示停止请求失败。
8. 后端规范终态为 `RUN_FINISHED(outcome=interrupt, result.reason=user_cancelled)`，
  前端按 `isUserCancelledOutcome()` 识别为"已取消"，**不是错误**。

**判定**：`outcome === 'interrupt' && result?.reason === 'user_cancelled'`。outcome=interrupt 但无 reason 的保守处理为非取消。

#### 3.5.1 取消态 `final_response` 渲染规则

`RoundData.final_response` 类型为 `string | null`，取消态下允许为 `null`、空串或占位串。任何直接读取（如 `round.final_response.trim()`）都必须先处理 `null`。

`Round.tsx` 通过 `isCancelledResponseSentinel(content, status)` 判定占位符，只有 `status === 'cancelled'` 且 `content` 经 `NFKC` 归一化并 `trim()` 后精确等于 `"Cancelled"` 时才成立。据此的渲染约定：

| 场景 | `final_response` | 展示 |
|---|---|---|
| 正常完成 | 普通字符串 | 原样展示，显示复制按钮 |
| 取消前已有有效助手正文 | 有效正文 | 继续展示该正文（`final_response` 无效时回退到最后一个非占位 step 的 `assistant_content`） |
| 取消且完整正文即占位符 | `"Cancelled"` | 隐藏占位串，只展示"已取消" |
| 取消且从未生成正文 | `null` / 空串 | 只展示"已取消" |

补充约束：
- 仅 `cancelled` 状态套用 sentinel 隐藏规则；正常完成态即使正文恰好等于 `"Cancelled"` 也必须原样展示，不得隐藏。
- 取消态不显示复制按钮：`canCopyAssistantContent = status === 'completed' && !!final_response`。
- 回退取正文时，step 级 `assistant_content` 同样按 sentinel 规则过滤占位串。

### 3.6 Human-in-the-Loop 暂停与恢复

- `loadHistory()` 若发现 Round `status === 'waiting_interaction' && interrupt`：
  - 以原 `round_id` 恢复/绑定 runtime run，`agentState.status='waiting'`；
  - 渲染 `QuestionCard` 或工具审批卡；
  - 从 `last_event_sequence` 继续订阅同一 Round，等待其他标签页的动作。
- waiting 时普通发送必须禁用。卡片不得提供“只在本地隐藏”的 X；用户只能回答/审批，或点击 Stop 取消整个 Round，避免进入既不能回答也不能发送的死角。
- 提交回答时复用原 client run key 与 server `round_id`，不追加 optimistic child Round。收到 `interaction_resolved` 后清卡、把同一 Round 改回 `running` 并继续消费事件。
- resume transport 已消费 durable terminal 后，即使 reader 在 clean EOF 前再次报错，也必须成功 settle，保留 terminal、禁止回拉旧 waiting 快照。若显式收到 `interaction_resolved`，或在漏收该事件后由权威 history 确认同一 Round 已是 `running`，都表示 continuation 已不可逆启动；后续 history 连续失败，或返回与本次 resume 相同 `interaction_id` 的陈旧 waiting 快照时，都必须保持 running 并从最新 cursor 续订，不得恢复 resume 前捕获的问题卡。只有不同 `interaction_id` 的后续 `interaction_requested` 可以再次进入 waiting。
- HTTP 200 / 本地 `stream_accepted` 只是传输接受，不是 continuation 边界。若在 `interaction_resolved` 前收到 `NO_PENDING_INTERRUPT`、`RESUME_CONFLICT`、`INVALID_INTERACTION_RESPONSE`、`AGENT_INIT_FAILED` 等 `RUN_ERROR`，不得将原 Round 置 `failed`；应立即回拉 history，并按 waiting / running / 终态权威恢复。
- 上述控制面错误即使已成功恢复权威 history，也必须通过独立的非终态错误通道展示给用户；不得因为恢复成功而静默吞掉错误，也不得把错误重新派发为 Round terminal。
- `interaction_resolved` 后若又收到下一次 `interaction_requested`，以后任何网络错误都不能覆盖该新问题；卡片保留并继续走 waiting subscribe/history。

### 3.7 多传输阶段同一 Round

一个 same-Round 执行可经历 direct stream、waiting subscribe、resume stream 和重连 subscribe。所有服务端事件仍使用同一个 `runId == round_id`；前端以稳定 client run key 聚合到同一 `RoundData.steps`，并用 transport epoch / connection id 丢弃旧连接迟到回调。

### 3.8 Resume 后的 Round 关系

`ask_user` / 工具审批恢复始终复用原 Round：`waiting_interaction → running`，`round_id`、用户消息、Skill/MCP 展示快照和推理快照均不变，回答不生成新的聊天气泡或临时 child Round。

### 3.9 本轮 Skill / 数据连接偏好

#### 选择器交互

- 输入框底部使用唯一 `+` 根菜单，固定顺序为“工作区文件 → 上传文件 → 专家 Skills → 数据连接”；模型/推理等级仍常驻底栏。四个入口互斥，桌面端向上展开，移动端使用底部浮层；`Escape` 关闭并把焦点还给 `+`，点击外部关闭。
- 工作区选择器展示文件和文件夹并支持搜索、lazy 目录与多选；文件夹必须作为一个独立选择项保留稳定 `entry_id/kind`，不得在前端展开或复制后代文件。Composer 与历史消息均渲染单个文件夹卡片；选择时不创建 Session、不上传、不读取 Data URL。普通草稿只以 `entry_id` 去重，发送时 file block 提交 `source/entry_id/kind/name/mime_type/size`，不提交选择时的 revision/current_version/tree_revision；服务端在 Round 受理时对文件解析并冻结 latest durable head，对文件夹只校验 entry 并返回实时引用。只有文件的明确历史版本选择才额外提交 `version_id`。
- `workspace_resource_changed` 仍是正式 mutation 的内部事实与工作区失效信号，但 `Round` 不直接渲染 raw audit。服务端只在成功文件 mutation 上附加受保护的 `assistant_file_reference`，live reducer 与 history 以同一稳定身份投影卡片；deleted tombstone 移除同 Round 旧引用。proposed/conflict/change-set、系统路径和 `NO_CHANGE` 永不进入普通聊天 UI。
- clean 工作区标签收到更高 version 时，先后台读取新内容，再原子替换正文与版本；dirty 标签保留当前页面内存草稿和原 base version并继续远端保存，服务端自动三方合并，不显示“有新版本”、加载新版本、另存或放弃动作。网络歧义、5xx 和 mutation 尚在处理时由 outbox 使用原 key 重试；确定性终态失败丢弃 Workspace 草稿、恢复服务端 current head，并只在当前文件内显示可关闭错误。只有 authoritative 404 或显式删除 tombstone 时关闭文件。
- `ChatInput` 仅在用户显式进入 Skill 子菜单时加载普通 `GET /api/config/skills`，每次重新打开都从服务端 DB 快照刷新清单，列表只展示 `enabled === true` 的项目；不得在页面初始化时预取或使用 `refresh=true` 触发远程恢复。已有成功清单时采用 stale-while-refresh：立即展示旧清单并以轻量状态提示请求，不得重新用整面 loading 遮住列表。
- 数据连接子菜单延迟调用 `GET /api/mcp/servers`，只展示 `enabled && installation_id !== null && enabled_tools_count > 0` 的连接；提交稳定 `server.id`，展示 `name`、说明及官方/个人来源。搜索匹配 id/name/description，最多选择 20 项。
- 当前实现不跨组件实例缓存清单。组件实例内关闭后尚未完成的同一请求可在重开时复用，避免重复远程恢复沙箱；请求完成后的下一次重开仍须发起新刷新。若以后增加更长生命周期缓存，缓存与进行中的请求必须按认证用户隔离，并在登录用户、token 身份或 Skill 启停状态变化时立即失效，禁止跨账号复用私有 Skill 名称、描述或启停状态。
- “尚未加载”“首次加载中”“已成功加载空列表”“后台刷新中”“首次加载失败”“后台刷新失败”必须是可区分状态。成功空列表或一次失败都不得因弹窗仍打开而触发自动请求循环；首次失败只能由用户显式重试，后台刷新失败须保留旧清单并提供重试入口；服务端返回 `inventory_state=stale` 时也必须保留清单并明确说明正在显示上次成功结果。
- 列表用 `display_name` 展示名称（缺失时回退 `name`），用 `key` 作为选择、去重和请求标识；不得把展示名称提交给后端。
- 搜索同时匹配 `display_name`、`name`、`key` 和 `description`，忽略大小写与首尾空白；每次重新打开选择器时清空上次搜索词。
- 选择项以可移除标签显示，最多选择 50 项；已达到上限时不得继续新增，但仍允许取消现有选择。每个可切换行必须通过 `aria-pressed` 暴露当前选中态。
- 选择器关闭不清空已选项。上传 pending 只禁用“上传文件”行，不阻止用户编辑 Skill/MCP 偏好；整个 composer 禁用时才禁用 `+`。
- 文案必须明确软偏好：Skill“相关时优先考虑，不强制调用”；MCP“相关时优先检索，无匹配会自动回退”。默认始终联网，不提供联网开关。

#### 已发送消息展示

- `Round` 在用户名下方、用户正文上方展示独立资源胶囊，不再渲染“本轮优先 Skill/优先数据连接”说明行。Skill 使用暖色 Skill 图标，MCP 使用绿色数据库图标；两类可在同一行换行排列。
- 胶囊名称只读：Skill 来自 `preferred_skills[].display_name`，MCP 来自 `preferred_mcp_connections[].display_name`；title 可分别暴露稳定 `key` / `server_id`，不得查询当前目录改写历史名称。
- 胶囊只表达本轮 UI 选择，不表示 Skill 已加载或 MCP 已调用；不得使用“已使用”“已调用”等成功态文案。任一快照为 `[]` 时不渲染对应胶囊，same-Round resume 不新增重复资源行。
- 每个独立 direct Round 只展示自己当次发送的快照，不继承或合并前一 Round 的标签。后续 Skill 被禁用、改名或删除也不得改写已有历史标签。
- 新消息尚未拿到服务端 Round 数据时，可用本次 composer 快照做 optimistic 展示；收到 `RUN_STARTED.preferredSkills` / `preferredMcpConnections`（显式空数组也算权威结果）后分别替换，刷新/断线恢复再以 `history/v2` 的完整 Round 快照为准。

#### 会话草稿与发送

- Skill 与 MCP 选择合并为 `TurnPreferenceDraft {skillKeys, mcpConnections, revision}`，按 session key 隔离保存。MCP 客户端快照冻结 `{server_id, display_name}` 供 optimistic 首帧直接显示中文；HTTP 仍只从中映射 `server_id[]`，服务端 `RUN_STARTED/history` 继续权威覆盖。
- 正文与附件使用独立的 `MessageDraft` 按相同 session key 隔离；草稿包含稳定 `draftId` 与递增 `revision`。正文编辑、附件增删递增 revision，session key 迁移不得改变 draftId。
- 新会话仍以 `__new_session__` 作为客户端映射 key，但附件上传必须先取得真实 server session ID。上传等异步回调绑定发起时的 `draftId + serverSessionId`，不得根据回调执行时的当前活跃会话决定写入位置。
- 从 `__new_session__` 迁移到真实 session 时，MessageDraft 与 TurnPreferenceDraft 必须在同一转换路径协调迁移；目标已有较新草稿或 draftId 已变化时，迟到响应不得覆盖或重新创建旧草稿。
- 发送时冻结正文、附件与 TurnPreferenceDraft。若本轮附加的 Workspace 文件正 dirty，必须在发送请求前只等待这些 entry 的 outbox 保存；成功后再让服务端解析 current head，失败或保存回执为 stale 时不创建 Round并保留 composer 草稿。未作为本轮附件的 dirty 文件仍同步抓取到应用级 outbox并在后台保存，不阻断 Agent；这些非附件路径只进入 Agent 的 `pending_file_drafts`，前端不显示全局同步提示。不得传草稿正文。
- optimistic Round 中尚未取得服务端结果的 Workspace 文件不得进入 Session 文件预览；点击时显示“工作区附件正在准备，请稍后再打开”，不得把原 Workspace path 拼到 `/api/sessions/{id}/files/`。authoritative Round 返回 snapshot 后才按 captured/read-only 链路打开。文件夹卡片始终按稳定 entry_id 打开当前 Workspace 目录，不经过 Session 文件预览，也不承诺与发送时内容一致。
- 受理前的 `CUSTOM attachment_preparing {index,total,name,kind}` 与 heartbeat 只用于持续产生 SSE 数据、避免客户端把准备阶段误判为断网；前端不得把逐项计数投影给用户。两者都不得把 run 从 `starting` 推进到 `streaming`，也不得触发 `stream_accepted`。
- 欢迎页创建 Session、迁移草稿与清空 composer 必须通过同步 submission snapshot 收敛；stream accepted 前拒绝时原子恢复正文、附件和本轮偏好，不依赖 React state 提交时序。
- HTTP/SSE 响应头不代表 Round 已受理；只有 `RUN_STARTED` 或按幂等键查到 durable Round 才发布 `stream_accepted`。此前收到的无序 `RUN_ERROR` 必须恢复 submission snapshot。
- 提交发送时乐观清空目标 session 的两类偏好。服务端确认 SSE 已接受后保持清空；执行已接受后的流式失败、中断或取消不得恢复旧选择。
- composer 清空只影响下一条发送，不得删除或隐藏当前 direct Round 已固化的资源胶囊。
- 若 POST 在收到响应头前发生网络错误，前端须按 §3.3.1 用同一 `idempotency_key` 查询历史：匹配到 running/waiting/终态 Round 即视为已接受，补发一次 `stream_accepted`，随后立即订阅或收敛终态；从历史恢复的失败终态也必须携带真实 `threadId`、`runId` 和末事件序号。只有 3 次 history 均成功且均无匹配时才恢复发送快照并报请求失败；任一次 history 失败则保持歧义、草稿保持清空并提示刷新。确定性的 HTTP 4xx/5xx 仍立即恢复；恢复回调最多执行一次。
- 从 history 直接收敛终态时，先投影完整 `RUNTIME_HISTORY_SNAPSHOT`（同时含两份权威快照），再处理 terminal；不得合成伪 `RUN_STARTED`。live direct 的真实 `RUN_STARTED` 仍负责即时纠正 optimistic 标签。
- 失败恢复必须带 revision 保护：未发生新编辑时精确恢复；已有新编辑时分别对 Skill key 与 MCP server id 保序、去重、按各自上限合并，不能用旧快照覆盖新编辑。
- `ask_user` 或工具审批 continuation 由后端按原请求锚点重新解析统一偏好；前端 `resume` 不重复提交两个偏好字段，也不改写原 Round 展示快照。

#### 模型与本轮推理等级

- 输入框底部工具栏使用一个向上展开的组合触发器展示“模型名 + 当前推理等级”，交互层级与 DeepSeek Harness 对齐：根菜单包含“模型”和“推理等级”，推理子菜单严格按当前模型 `supported_reasoning_efforts` 的顺序展示；`off` / `on` 也是目录显式声明的等级，前端不得自行追加。模型目录的 `thinking_mode=provider_default` 时额外提供独立 `Default` 项；若目录默认还带具体强度，显示为 `Default (<level>)`，与显式选择同名等级区分。
- 触发器本身只显示模型名与推理等级，不挂能力徽章；模型能力说明（“支持深度思考”“支持图片（最多 N 张）”）保留在模型子菜单的每一项下方，不得因为改版而整体丢失。
- 会话标题栏不使用固定宽度占位元素来对齐右侧 `Files` 按钮；`Files` 按钮用 `ml-auto` 靠右，欢迎页无按钮时标题栏保持空行高度。
- 管理端新建 OpenAI 兼容模型时默认填入 `off, on` 且默认等级为 `on`；DeepSeek 等具有分级强度的模型可改为 `off, high, max`。请求协议由独立的 `thinking_wire_format` 技术项配置，不与用户可见等级混用。
- 新 session 可切换模型；已有 session 的模型保持锁定，但下一轮推理等级仍可编辑。切换模型时按新的模型目录默认值重置选择，绝不把上一模型的强度带过去。目录默认值是 `thinking_mode + reasoning_effort` 的完整二元组：初始化草稿时必须原样冻结，不能因为存在具体强度就把 `provider_default` 推断成 `enabled`。`Default` 始终映射回该完整二元组；显式具体档位映射为 `enabled + effort`，两种状态即使展示强度相同也必须可区分、可往返。
- 本轮推理选择属于 composer draft，必须按 `sessionId || __new_session__` 隔离；切换到使用同一模型的其他会话不得沿用当前选择，新 session 建立后随原 draft 一起迁移到真实 session id。
- 发送前冻结 `TurnReasoningSelection` 并随 `content` 一并提交为 `thinking_mode` / `reasoning_effort`。正在流式执行时继续修改输入框只影响下一条消息，不得改变已启动 run。
- 选择 `Off` 必须发送 `{thinking_mode: "disabled", reasoning_effort: null}`；选择 `Default` 发送目录完整默认二元组；选择显式具体档位发送 `{thinking_mode: "enabled", reasoning_effort: "<level>"}`。前端不得按模型名猜测档位或提交目录没有声明的值。
- 从推理等级或模型菜单提交选项后，焦点必须回到消息输入框，确保用户无需额外点击即可继续输入或按 Enter 发送；通过 Escape 取消菜单时仍将焦点退回菜单触发器。
- 服务端返回 400 时按普通确定性发送失败处理并恢复草稿；历史 `RoundData.thinking_mode` / `reasoning_effort` 是已发送轮次的审计快照，不反向覆盖当前输入框草稿。

## 4. 滚动策略

| 场景 | 时机 | 实现 |
|---|---|---|
| 普通进入会话 | 历史渲染完成后、浏览器绘制前 | `useLayoutEffect` 同步 `messagesEndRef.scrollIntoView({ behavior: 'auto' })`，直接定位最新消息 |
| 搜索命中进入 | `scrollTarget` 指向当前 session/round | 目标 round `scrollIntoView({ behavior: 'smooth', block: 'center' })` 并短暂高亮；不得先滚到底部 |
| 流式新内容 | rounds/steps 数量变化 | 仅 `isAtBottom` 时 `scrollIntoView({ behavior: hasNewContent ? 'smooth' : 'auto' })` |
| 用户滚动 | `scroll` 事件 | 更新 `isAtBottom` 与 `showScrollButton`（容差 100px），不记忆跨会话 `scrollTop` |
| 底部按钮 | 用户离开底部 | 点击后平滑滚到底；若有回复正在生成，按钮显示 live reply 指示 |

**禁止**：
- 普通进入会话时恢复上次浏览位置。用户点击历史会话的默认预期是看到最新状态；浏览器刷新后也必须保持同一语义。
- 在普通 `useEffect` 里设置初始滚动位置（会有跳动）。
- 无视 `isAtBottom` 强制滚到底（抢用户滚轮）。
- 搜索命中带 `scrollTarget` 时先滚到底再滚到目标 round。

## 5. 动画约定

- 首次渲染历史：`disableInitialMotion = true`，加载完关闭一次性 flag。
- 实时新内容：启用 `animate-fade-in`。
- `suppressAutoScrollRef` 用于阻止历史加载窗口内的流式自动跟随；历史加载完成后，普通进入显式定位到底部，搜索进入交给 `scrollTarget` 处理。
- 新会话首次发送时保持欢迎页，创建完成后直接进入对话，不显示额外的 bootstrap message 或 session handoff 动画。

## 6. 轮询契约

ChatV2 不做定时轮询。Cron 任务执行结果**不**注入聊天 Session，由用户在「日程管理」一级页的「执行记录」中查看（见 frontend-panel-spec §6）。

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
| `apply_patch` | `Update {path}` / `Update N files` | 从 Patch 头提取路径，历史旧工具继续兼容 |
| `present_files` | `Present {path}` / `Present N files` | 只展示显式交付路径 |
| `bash` | `Run \`{cmd}\`` | Keep-Head |
| `search_*` | `Search "{query}"` | Keep-Head |
| `sub_agent` | `委派子任务 {description 或 prompt 首行}` | Keep-Head |
| 其他 | `{tool_name}` | — |

### 7.3 分组摘要

`getGroupSummary(group: ToolGroupBlock)`：
- 完成态按真实工具语义聚合：`apply_patch` 使用 `Updated {path}` / `Updated N files`，`present_files` 使用 `Presented {path}` / `Presented N files`；历史 `edit_file` 继续使用 `Edited`，不得把 `present_files` 降级显示为 `Used a tool`
- 其他示例：`Edited 2 files, Read a file`
- 运行态：`Reading src/app.py...`（Typewriter Preview）

### 7.4 活动入口与抽屉

- ThinkingBlock / ThinkingGroupBlock：在单个 round 内聚合为一个思考入口，不按 step 或工具分隔重复渲染多个入口。
  - 进行中：渲染醒目的 `正在思考` 卡片，显示实时耗时、当前最新 thinking 的最多 3 行滚动预览与 `查看活动` 入口；完整内容保留在活动抽屉。预览默认每 3 个动画帧跟随末尾，用户滚离底部后必须暂停自动跟随，回到底部后恢复；末尾用流式闪烁点表示仍在生成。
  - 完成态：渲染紧凑的 `已完成思考` 胶囊，显示整轮思考总耗时。
  - 点击 `查看活动` 或完成态胶囊后打开右侧活动抽屉；不得在主聊天区展开 thinking 详情。
- 活动抽屉与全屏遮罩必须通过 React Portal 直接挂到 `document.body`，不得作为 Round/消息/聊天滚动容器的后代；遮罩和抽屉在垂直方向各 overscan 4px（`-inset-y-1` / `-top-1 -bottom-1`），覆盖 in-app viewport 的顶部偏移，避免出现横向白缝。
- ToolGroupBlock：完成态不在主聊天区直接渲染；工具摘要、工具项、输入输出、`✓ Done` 标记均在活动抽屉内展示。若 round 没有 thinking 但存在已完成工具活动，主聊天区渲染紧凑的 `已完成活动` 入口，仅用于打开活动抽屉。只有 round 仍处于 streaming 时，最新 ToolGroupBlock 的未返回工具结果才可视为运行中；若最新 ToolGroupBlock 仍在运行且没有正在流式的 thinking，主聊天区必须显示 `正在调用工具` 活动态卡片和工具摘要。终态 round 中缺少 tool_result 的工具调用不得显示 `正在调用工具`。若工具已返回但 round 仍处于 streaming，且下一段 thinking/正文尚未到达，主聊天区必须显示 `正在处理工具结果` 活动态卡片，避免 think/tool 与下一段 think 之间的空窗期看起来已经完成。
- ToolItem：在活动抽屉内 hover 显示展开箭头，点击查看工具入参/结果。
- `sub_agent` ToolItem 必须使用专用子任务胶囊展示：折叠态保持单行，仅显示 `委派子任务`、业务标题、`subagent_type` 与耗时；不得默认展开原始 JSON 入参。点击展开后展示任务 prompt、子任务输出/错误与可选 child run id，便于理解长耗时委派执行。child run id 等父子 run 元数据应优先来自结构化 metadata 或 `subagent_runs` 查询；从 `TOOL_CALL_RESULT.content` 文本中解析仅允许作为当前兼容兜底。

## 8. 附件上传

- 图片：前端用 `readFileAsDataUrl` 读取原始 Data URL 后发送，避免截图/OCR 场景因压缩降质；体积保护由后端单张 20MB、总量 50MB 限制负责。
- 其他文件：通过 `apiService.uploadFile` 上传到沙箱，返回 `AttachmentInfo`。
- 消息体：附件以 `ChatContentBlock[]` 形式发送（`image_url` / `file` 等类型）。

## 9. 错误处理

| 错误 | 来源 | UI 表现 |
|---|---|---|
| `HttpError(4xx)` | axios 拒绝 | definite rejection；显示错误 banner，**不自动重试** |
| `HttpError(5xx)` | axios 拒绝 | definite rejection；显示错误 banner，允许用户显式重试，但不自动重发 |
| `RoundExistsError` | `sendMessage` 冲突 | 静默切到 subscribe |
| direct 控制面 `INTERACTION_PENDING` | 陈旧标签页在已有 waiting Interaction 时发送普通消息 | 新 optimistic Round 不得终态化原 Round；回拉 history、恢复 waiting subscribe 与未受理草稿，并显示非终态冲突提示 |
| SSE 断开 | EventSource error | 走 §3.3 恢复 |
| resume 控制面 `RUN_ERROR` | `interaction_resolved` 前的 `NO_PENDING_INTERRUPT` / `RESUME_CONFLICT` / `INVALID_INTERACTION_RESPONSE` / `AGENT_INIT_FAILED` 等 | 显示请求错误但不终态化原 Round；回拉 history 恢复权威状态 |
| 运行期 `RUN_ERROR` | 新消息已建立 Round，或 resume 已越过 `interaction_resolved`，且事件带 durable sequence 或 history 已权威投影 failed | 将对应 Round 收敛为 failed，显示错误 banner |

## 10. 测试清单

- [ ] 切换会话时旧 SSE 事件不污染新会话（mock 迟到事件）
- [ ] 取消后 UI 显示"已取消"而非"错误"
- [ ] 取消成功后无需等待 SSE，即刻恢复输入并允许重发
- [ ] 取消成功返回 `outcome_warning`：取消状态不变，聊天页不展示重复的副作用提示
- [ ] ask_user / 工具审批暂停：刷新页面后同一 waiting Round 的卡片正常显示且继续订阅
- [ ] 另一个标签页回答或取消 waiting Round，本页通过 subscribe 收到 resolved、后续输出或终态，无需刷新
- [ ] resume 200 SSE 在 `interaction_resolved` 前返回控制面 RUN_ERROR：原 Round 不变为 failed，history 可恢复卡片/运行态/终态
- [ ] resume 控制面错误恢复 history 成功后仍显示请求错误，不生成 terminal RUN_ERROR
- [ ] resume 收到 durable terminal 后 reader reject 且 history 不可用：仍保持终态，不恢复旧问题卡或重建 waiting subscribe
- [ ] resume 已 resolved 未 terminal 且 history 全失败：保持 running，从 resolved cursor 续订
- [ ] resume 已 resolved 后 history 返回相同 `interaction_id` 的 waiting 快照：视为陈旧状态且不复活旧卡；不同 ID 的新 Interaction 仍可进入 waiting
- [ ] 陈旧标签页发送消息收到 `INTERACTION_PENDING`：恢复未受理草稿、waiting 卡片与订阅
- [ ] 收到下一次 `interaction_requested` 后立刻断网且 history 失败：新卡片仍保留，不被 RUN_ERROR 覆盖
- [ ] waiting subscribe 已安排 retry 时点击 Stop：旧 timer 不再建连，慢 abort 期间 UI 不回跳 waiting
- [ ] equal 或 unrelated higher cursor history 不覆盖尚未 END 的 text / thinking / tool args dirty segment；只有逐 segment 匹配的 server projection / aggregate 才可权威替换并推进 cursor
- [ ] waiting 卡片无纯本地关闭入口；回答与 Stop 均可离开等待态
- [ ] SSE 断连后自动恢复（history API 查询终态，running/waiting 续订）
- [ ] 幂等冲突自动切 subscribe
- [ ] 普通进入长会话时定位到底部；A 滚到中间 → 切 B → 切回 A，A 仍定位到底部
- [ ] 搜索结果带 `match_round_id` 时定位到命中 round，而不是底部
- [ ] 首屏历史渲染无瀑布动画
- [ ] 从 `/` 点击工作区后 URL 为 `/workspace`；硬刷新首帧仍为工作区，且会话请求永久 pending/reject 不出现整栏无限 spinner
- [ ] 会话/工作区 mode 行保持同一高度和 Y 位置；会话无 `HISTORY`，右侧 `+` 可新建；工作区搜索与省略号为不占布局的锚定浮层
- [ ] 顶部菜单在展开/点击任意文件夹后仍只向根目录新建、上传和刷新；文件夹三点菜单的创建、上传和刷新只作用于该文件夹
- [ ] 新建 Markdown/XLSX 不出现命名 Dialog，按未命名自然序列创建并立即打开右侧编辑器；新建文件夹才要求输入名称
- [ ] 底部跟随：用户滚离底部时新消息不强制滚动
- [ ] 用户滚离底部且新回复正在生成时，底部按钮显示 live reply 指示
- [ ] 助手文件卡只来自 durable `assistant_file_references`；普通反引号、提示行和代码块文件名均不创建卡片
- [ ] 点击卡片在既有右侧工作台打开当前稳定实体；Workspace 实体缺失时明确提示已删除，不回退旧版本，不出现遮罩弹窗或第二套预览壳
- [ ] Skill 选择器仅在显式打开时加载并在重新打开时后台刷新；已有清单立即展示，未完成请求可复用，只展示 enabled 项
- [ ] Skill 清单不跨组件实例/账号复用，退出后切换账号不泄露上一账号的私有 Skill；成功空列表与请求失败均不产生自动重载循环，刷新失败保留旧清单
- [ ] Skill 使用 `display_name` 展示、`key` 提交；搜索重开清空；选择、标签移除、50 项上限、桌面点击外部/`Escape` 与移动端关闭按钮行为正确
- [ ] `+` 根菜单包含上传文件、专家 Skills、数据连接；子菜单互斥，Escape/外部点击/焦点回退正确，上传 pending 不禁用偏好编辑
- [ ] 数据连接只在显式打开时加载，仅展示当前启用且有工具的 installation；按 `name/id/description` 搜索、提交 server id、20 项上限、清单不跨账号复用
- [ ] 普通 direct Round 在用户正文上方按 Skill/MCP 快照展示独立图标胶囊，无说明行；空数组不展示，same-Round resume 不重复，文案不暗示已使用
- [ ] `preferredMcpConnections=[]` 能清除 optimistic MCP 胶囊；历史刷新仍按冻结名称恢复
- [ ] TurnPreferenceDraft 按 session 隔离；A/B 会话的 Skill/MCP 均互不污染，新会话创建后统一迁移
- [ ] 正文与附件草稿按 session 隔离；切回会话可恢复，迟到上传只更新其捕获的 `draftId + serverSessionId`
- [ ] 新会话的正文、附件与 TurnPreferenceDraft 协调迁移；重复或迟到的创建结果不得覆盖真实 session 下的较新草稿
- [ ] 发送携带 `preferred_skill_keys` 与 `preferred_mcp_server_ids`，空选择省略；发送后目标 session 的两类选择清空
- [ ] composer 清空后已发送 Round 的资源胶囊仍保留；刷新或断线恢复后以 `history/v2` 的持久化 `display_name` 快照还原，独立多轮不继承或累积
- [ ] 接受前 4xx/5xx 立即恢复两类偏好快照；网络歧义按同一幂等键查询 3 次 history，确认 Round 后不恢复，仅 3 次均成功且无匹配时恢复一次
- [ ] 响应头前取消会 abort POST 且不查 history、不恢复草稿；等待 history 时取消会忽略迟到结果、不建立 subscribe、不恢复草稿
- [ ] 接受前拒绝后若用户已修改偏好草稿，分别保留 Skill/MCP 新编辑并无重复合并旧快照
- [ ] ask_user 与工具审批 resume 延续原请求的统一偏好；下一条独立消息不继承

## 11. 已知易错点

1. 新 transport 没有递增 epoch/替换 connection id，或旧 finally 未校验 identity 就清理新连接。
2. 新增 SSE 事件类型时只改解析层，忘记补 `chatRuntimeReducer` 与 history replay 投影。
3. `ToolResult` 很大时直接渲染导致卡顿 → 用 `TruncatedCodeBlock`。
4. `applyPatch` 失败时吞异常 → **必须 console.error**，否则 state 偷偷停更。
5. 取消后未清除 `sending` → 输入框卡死。
