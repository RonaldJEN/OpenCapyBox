# 前端 Chat Spec — 聊天 / SSE / 行内执行过程

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[chat-spec.md](./chat-spec.md)

覆盖组件：`ChatV2.tsx`、`Round.tsx`、`ChatInput.tsx`、`SessionList.tsx`、`WorkspaceSidebarContent.tsx`、`WorkspaceFilesPanel.tsx`、`WorkspaceFilePicker.tsx`、`ReasoningPanel.tsx`、`QuestionCard.tsx`。

## 1. 模块职责

- 发送用户消息（含附件、引用图片）
- 消费后端 SSE（AG-UI 事件）增量构建 `RoundData[]`
- 渲染消息流（user → content/工具过程 → 最终答复）
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

history 的 `last_event_sequence` 只有在同一 snapshot 的 `steps/final_response/interrupt/status` 已完整投影后才能成为新 cursor；禁止保留局部本地 steps 却直接跳到服务端高水位。新主助手 text delta 已提交并带 durable sequence；旧协议 text 以及 thinking / tool args 在 END 前仍是 live-only、没有 durable sequence；全局 cursor 变大只证明某个 durable 事件已提交，不证明每个交错 segment 都已写入 aggregate。只要本地仍有 dirty segment，history 必须逐 segment 证明其对应 projection 已包含本地前缀（工具参数以同 `tool_call_id` 的持久化调用为证），否则即使 cursor 更高也要保留本地 projection 与 buffer。direct / resume 内嵌 subscribe 若恢复出非终态，必须以结构化 handoff 把同一 `clientRunKey + roundId + cursor` 交给 Provider 继续 subscribe，不得合成 Round terminal `RUN_ERROR`。无 durable sequence 的 `SUBSCRIBE_FAILED` 属于 transport 控制错误；只有持久化或 history 权威恢复的 `RUN_ERROR` 才能终态化 Round。

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

本节约定已发起停止请求后的行为；`ask_user` 等待回答时的聊天页不提供停止入口，见 §3.6。

用户点击停止时，先使旧 transport epoch 和 retry timer 失效，标记 `localOutputStopped`，立即停止本地输出。`cancelRequest` 独立记录 pending / confirmed / failed / unknown；不凭点击将业务 Round 改为 cancelled。

调用 `/api/chat/{sid}/abort` 时携带已知 `round_id`。成功响应返回 `round_id/round_status/admission_released`：只有目标权威终态和目标准入释放均已确认，才结束 stopping 并恢复发送。历史/运行列表用于补充确认；HTTP 409 本身不证明已取消。先完成的 Round 保持 completed，较新 Round 不受旧取消影响。

确认期间输入可编辑，发送和回答/审批动作禁用。失败或无法确定时显示“停止结果待确认”和重新确认入口；重新读历史，不复活旧正文订阅、不恢复草稿、不重发已受理请求。`outcome_warning` 保留后端诊断，不另作全局横幅。`RUN_FINISHED(outcome=interrupt, result.reason=user_cancelled)` 显示“已停止”，不作为执行错误。

#### 3.5.1 正文、终态说明与复制

- `projectRoundTranscript` 是步骤正文的唯一展示与复制投影。所有非空步骤正文按现有步骤顺序保留，不受工具调用、流式/完成/失败/取消状态影响。空白判断不改写正文；不同步骤的相同文本不去重。
- `final_response` 先按 `terminal_presentation.final_response_origin` 分流。确认的助手终稿及正常完成 legacy 终稿进入正文；取消态无来源且非占位的终稿保留原有兼容展示。仅当正文候选与最后一条步骤正文完全相等时作为别名显示一次，部分重叠仍保留。失败态来源未知的独立终稿放入“运行结束说明（旧版记录）”，不默认复制为回复。
- `RUN_ERROR` 保持现有终态和取消语义。真实错误记录独立的 Round 级来源与说明；已有助手终稿不因后续错误改标。历史使用可选终态来源投影；恢复合成的 `RUN_ERROR` 不构成原始来源证据，缺少来源时只合成通用失败状态。
- `assistant_content_source=text_message` 表示有文本事件依据，即使内容等于 `Cancelled` 或异常说明也保留。仅无来源 legacy 的 cancelled 占位串在 trim 后精确等于 `Cancelled` 时隐藏；不做 Unicode 归一化或错误关键词识别。
- “复制回复”仅复制共享投影的 `answerNodes`：有效最终别名、原生 final_answer、明确交付主体以及合法的最终响应回退，按消息顺序保留多段答复，不按相同文本去重。进展、interrupted/superseded尝试、工具、系统错误及运行说明不混入。尚无明确答复时隐藏主按钮，保留已有正文供阅读与选中复制；过程展开/收起不改变主复制范围。多最终别名仅部分恢复时使用完整回退一份，避免同时复制已恢复片段和全量终稿。
- 附件只来自合法 `assistant_file_references`，在正文条件外独立渲染。消息级 ErrorBoundary 只将出错消息降级为转义纯文本，其他消息、附件、复制继续可用。
- Markdown 块锚点为 `answer:<稳定正文节点>:<tag>:<offset或AST路径>`，节点由运行的幂等键（旧记录回退 Round ID）和 messageId 构成，legacy 使用步骤号；终稿兜底用独立键。完成/中断不换节点键；React key 和 DOM 锚点均保持身份。复用原阅读位置捕获/恢复算法。
- 工具投影仅输出按调用身份关联结果的工具项，行内组件按消息和工具的事件顺序展示；不再计算旧面板的思考分组、工具汇总或累计耗时。
- 使用 `assistant_messages` 保留服务端 messageId、step_number、first/last_sequence、content、state 和 content_committed；可选 `phase` 仅接受供应商明确的 `commentary/final_answer`。`final_message_id` 指向单段终稿，多段终稿使用有序 `final_message_ids`，别名全部可解析时不再重复渲染聚合 `final_response`。步骤仅作为 legacy 投影。同一步多条正文不共用一个身份，failover 旧段标 interrupted，替代关系未知时不推断 superseded。
- 历史的 transcript_coverage 和 status 以同次事件扫描为准；快照必须覆盖同消息的已提交 END 才能替换旧 live-only 尾段。新主正文增量采用提交后发布，显式 isAggregate=false；重连按 sequence 去重。思考和工具参数仍保留旧 END 聚合语义。
- 保证已提交并发布的主正文在消息 END 前进程退出后仍可回放；不保证模型尚未返回、数据库提交失败或旧协议未提交的内存尾段。

#### 3.5.2 文件打开意图

用户附件和助手文件的上层异步打开共用一个请求世代。新打开、关闭文件面板、切换会话/工作区目标及卸载使旧世代失效；成功、异常回退都须校验世代和会话归属后才能写 target、提示或重新开面板。关闭态发起的新打开仍然有效。失效只针对展示意图，不取消 `saveDirty` 或 Workspace 草稿保存。

### 3.6 Human-in-the-Loop 暂停与恢复

- `loadHistory()` 若发现 Round `status === 'waiting_interaction' && interrupt`：
  - 以原 `round_id` 恢复/绑定 runtime run，`agentState.status='waiting'`；
  - 渲染 `QuestionCard` 或工具审批卡；
  - 从 `last_event_sequence` 继续订阅同一 Round，等待其他标签页的动作。
- waiting 时普通发送必须禁用。卡片不得提供“只在本地隐藏”的 X；回答/审批继续复用同一 Round。
- `ask_user` 等待回答（`input_required`）时，聊天页不提供本轮的停止、取消或问答关闭入口；用户通过回答或跳过并提交继续原 Round。提交启动后恢复普通输入区及执行中的停止按钮。该约定仅限问答等待界面，工具审批等待、后端 abort 能力及跨端终态同步沿用原契约。
- `QuestionCard` 按 interaction ID 隔离答案状态，在组件入口解析原始题目数据，翻页、选项判断与提交共用解析结果。已保存题目若缺少有效选项，保留原题文并提示直接输入，不把题目顶层的 `label/description` 猜成选项；缺少题文时仅在卡片内报错，不能让整个聊天页崩溃。前后翻页保留选择和输入，提交仍以原题文作为答案键；新请求由后端按完整工具 schema 校验。
- 单选使用圆圈与选中圆点，多选使用方框与勾选，并按题目定义显示“单选/可多选”；页码显示 `1 / N`。普通页使用“跳过”，最后一页使用“跳过并提交”，两者均把本题记为无偏好。
- 问答卡独占当前输入区，普通输入框、附件和模型控件保持挂载并隐藏，草稿不变；不以负 margin 叠压输入框。卡片使用轻阴影，题目区可滚动、翻页和提交区常驻，卡片下方不附加提示或停止入口。提交启动后立即隐藏卡片并恢复普通输入区，运行摘要显示“正在继续”；后端确认前沿用该 waiting Round 的 interrupt 隐藏保留原答案，提交失败恢复问题时可继续编辑，不把恢复等待留在禁用卡片上。textarea 隐藏时不测量高度，恢复可见时重新计算，避免文字被裁切。文件全屏与子任务视图沿用原隐藏和焦点归属。
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

- 正文发送上限为 30,000 个 Unicode 码点，与后端单个 `text` 块限制一致。沿用发送前 trim，以实际提交正文计数；恰好 30,000 个允许发送，超过时提示当前长度和上限并保留草稿，不创建 Session 或发起消息请求。从附件恢复的原文、多次粘贴累计正文和手动输入使用同一限制；单次粘贴超过 1,000 个码点转附件的规则独立不变（见 §8）。

- Skill 与 MCP 选择合并为 `TurnPreferenceDraft {skillKeys, mcpConnections, revision}`，按 session key 隔离保存。MCP 客户端快照冻结 `{server_id, display_name}` 供 optimistic 首帧直接显示中文；HTTP 仍只从中映射 `server_id[]`，服务端 `RUN_STARTED/history` 继续权威覆盖。
- 正文与附件使用独立的 `MessageDraft` 按相同 session key 隔离；草稿包含稳定 `draftId` 与递增 `revision`。正文编辑、附件增删递增 revision，session key 迁移不得改变 draftId。
- 新会话以 `__new_session__` 作为客户端映射 key，上传只使用独立 `draftId + clientId`，不得创建 Session。异步回调绑定发起时的草稿和附件身份，不能写入当前活跃会话，也不能复活已移除卡片。只在 Enter/发送时创建真实 Session，再把本次附件 claim 到该 Session。
- 从 `__new_session__` 迁移到真实 session 时，MessageDraft 与 TurnPreferenceDraft 必须在同一转换路径协调迁移；目标已有较新草稿或 draftId 已变化时，迟到响应不得覆盖或重新创建旧草稿。
- 发送时冻结正文、附件与 TurnPreferenceDraft。若本轮附加的 Workspace 文件正 dirty，必须在发送请求前只等待这些 entry 的 outbox 保存；成功后再让服务端解析 current head，失败或保存回执为 stale 时不创建 Round并保留 composer 草稿。未作为本轮附件的 dirty 文件仍同步抓取到应用级 outbox并在后台保存，不阻断 Agent；这些非附件路径只进入 Agent 的 `pending_file_drafts`，前端不显示全局同步提示。不得传草稿正文。
- optimistic Round 中尚未取得服务端结果的 Workspace 文件不得进入 Session 文件预览；点击时显示“工作区附件正在准备，请稍后再打开”，不得把原 Workspace path 拼到 `/api/sessions/{id}/files/`。authoritative Round 返回 snapshot 后才按 captured/read-only 链路打开。文件夹卡片始终按稳定 entry_id 打开当前 Workspace 目录，不经过 Session 文件预览，也不承诺与发送时内容一致。
- 受理前的 `CUSTOM attachment_preparing {index,total,name,kind}` 与 heartbeat 只用于持续产生 SSE 数据、避免客户端把准备阶段误判为断网；前端不得把逐项计数投影给用户。两者都不得把 run 从 `starting` 推进到 `streaming`，也不得触发 `stream_accepted`。
- 欢迎页创建 Session、迁移草稿与清空 composer 必须通过同步 submission snapshot 收敛；stream accepted 前拒绝时原子恢复正文、附件和本轮偏好，不依赖 React state 提交时序。
- 点击发送并通过本地校验后，立即将冻结的正文、附件、模型与偏好展示到对话区，助手位置显示“正在准备请求...”；创建 Session、保存附加的 Workspace 草稿和附件 claim 均在此展示期间进行，输入框按钮不重复转圈。准备展示按草稿身份隔离并随 Session 迁移，附件仍使用本地/Workspace 预览；准备完成后一次性交给 runtime optimistic Round，不重复展示。准备失败撤下展示并恢复草稿，不创建假的服务端 Round、不提前发出 `stream_accepted`。
- HTTP/SSE 响应头不代表 Round 已受理；只有 `RUN_STARTED` 或按幂等键查到 durable Round 才发布 `stream_accepted`。此前收到的无序 `RUN_ERROR` 必须恢复 submission snapshot。
- 本地 Blob URL 由草稿/提交快照持有：受理前拒绝保留供恢复和重试；`stream_accepted` 时只释放本次快照的 URL 与本地附件引用，不能释放已切换到的其他草稿或下一轮新附件；移除和卸载同样按持有者释放。
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
- 新旧 Session 均可按轮选择模型。模型与推理等级按草稿隔离，默认模型来自 Session 最近受理轮次；新会话使用目录/本地默认。切换模型时按新的模型目录默认值重置推理选择，绝不把上一模型的强度带过去。目录默认值是 `thinking_mode + reasoning_effort` 的完整二元组：初始化草稿时必须原样冻结，不能因为存在具体强度就把 `provider_default` 推断成 `enabled`。`Default` 始终映射回该完整二元组；显式具体档位映射为 `enabled + effort`，两种状态即使展示强度相同也必须可区分、可往返。
- 发送同时冻结 `model_id`；等待本次准备/受理时保护正文与附件，受理后可以编辑下一条正文、添加附件、预选模型。执行中的 Run 和 same-Round resume 不受草稿修改影响；同一 Session 仍只运行一个 Round，不自动排队发送。所选模型由输入框内的模型选择器展示，不额外显示下一条消息的模型提示。历史助手标题显示 `model_display_name` 快照，旧记录为空时只显示“助手”。
- 本轮推理选择属于 composer draft，必须按 `sessionId || __new_session__` 隔离；切换到使用同一模型的其他会话不得沿用当前选择，新 session 建立后随原 draft 一起迁移到真实 session id。
- 首次可解析草稿时必须固化模型 ID 和完整推理二元组，即使用户从未手选。欢迎页的初值来自独立模型目录默认值，不能来自 App 最近查看会话的共享选择；已有 Session 必须先取得权威历史模型再初始化，未初始化期间禁止发送但可编辑正文/上传附件。目录或其他会话选择的迟到变化不能覆盖已有草稿快照。
- 发送前冻结 `TurnReasoningSelection` 并随 `content` 一并提交为 `thinking_mode` / `reasoning_effort`。正在流式执行时继续修改输入框只影响下一条消息，不得改变已启动 run。
- 选择 `Off` 必须发送 `{thinking_mode: "disabled", reasoning_effort: null}`；选择 `Default` 发送目录完整默认二元组；选择显式具体档位发送 `{thinking_mode: "enabled", reasoning_effort: "<level>"}`。前端不得按模型名猜测档位或提交目录没有声明的值。
- 从推理等级或模型菜单提交选项后，焦点必须回到消息输入框，确保用户无需额外点击即可继续输入或按 Enter 发送；通过 Escape 取消菜单时仍将焦点退回菜单触发器。
- 服务端返回 400 时按普通确定性发送失败处理并恢复草稿；历史 `RoundData.thinking_mode` / `reasoning_effort` 是已发送轮次的审计快照，不反向覆盖当前输入框草稿。

## 4. 滚动策略

| 场景 | 时机 | 实现 |
|---|---|---|
| 普通进入会话 | 历史渲染完成后、浏览器绘制前 | 阅读位置管理器在 useLayoutEffect 内直接定位最新消息 |
| 搜索命中进入 | `scrollTarget` 指向当前 session/round | 管理器平滑定位目标 round 中部并短暂高亮；不得先滚到底部 |
| 流式新内容 | 正文渲染或几何尺寸变化 | 跟随底部时直接保持末尾；阅读模式保持同一可见字符，不反复启动平滑动画追赶 token |
| 用户滚动 | 用户造成的 `scroll` 事件 | 以统一 2px 容差识别是否回到底部，否则捕获文字阅读锚点；程序恢复/平滑定位中间帧不改写用户意图 |
| 发送新问题 | Enter 或发送按钮通过本地发送校验 | 立即将阅读意图切为跟随底部，展示准备中/新一轮后继续跟随；不等待网络响应。Shift+Enter、输入法确认或未通过校验不触发 |
| 底部按钮 | 当前实际距底部超过 2px | 点击后平滑滚到底；若有回复正在生成，按钮显示 live reply 指示；实际到达底部或内容不足一屏时隐藏 |

“回到最新消息”按钮由聊天 pane 内、消息滚动区之后的输入区上沿承载，水平居中并浮在消息区底端；常态显示文字与向下箭头，生成中保留“新回复正在生成”提示。定位随聊天分栏宽度与输入区高度自然变化，禁止使用相对 viewport 的 fixed/right/bottom 偏移；不得落入 Session/Workspace 文件面板，聊天隐藏时随其一起隐藏。

按钮显隐依据当前 `scrollHeight - scrollTop - clientHeight`，不直接使用阅读/跟随意图。工具收起导致内容不足一屏时隐藏按钮，但保留阅读锚点；重新展开仍恢复原阅读位置，不把布局造成的到底误记为用户主动跟随。

`useChatReadingPosition` 是聊天容器唯一滚动写入者，统一管理首次进入、显式定位、流式跟随、文件布局恢复及 ResizeObserver 通知。容器关闭浏览器原生 overflow anchoring，避免双重补偿；用户 wheel/pointer/键盘输入可中止程序平滑定位。位置恢复不使用 timeout 或多帧猜测布局稳定。full 的隐藏阅读书签按 Session 隔离，具体语义见 Session 文件 spec。平滑定位尊重 prefers-reduced-motion。

**禁止**：
- 普通进入会话时恢复上次浏览位置。用户点击历史会话的默认预期是看到最新状态；浏览器刷新后也必须保持同一语义。
- 在普通 `useEffect` 里设置初始滚动位置（会有跳动）。
- 无视 `isAtBottom` 强制滚到底（抢用户滚轮）。
- 搜索命中带 `scrollTarget` 时先滚到底再滚到目标 round。

## 5. 动画约定

- 首次渲染历史：`disableInitialMotion = true`，加载完关闭一次性 flag。
- 实时新内容：启用 `animate-fade-in`。
- `suppressAutoScrollRef` 用于阻止历史加载窗口内的流式自动跟随；历史加载完成后，普通进入显式定位到底部，搜索进入交给 `scrollTarget` 处理。
- 新会话首次发送通过本地校验后，立即展示冻结的用户消息和“正在准备请求...”；创建会话及附件准备期间保留该展示，随后交接给 runtime optimistic Round，不重复展示消息或增加 session handoff 动画（见 §3“会话草稿与发送”）。

## 6. 轮询契约

ChatV2 不做定时轮询。Cron 任务执行结果**不**注入聊天 Session，由用户在「日程管理」一级页的「执行记录」中查看（见 frontend-panel-spec §6）。

## 7. 行内执行过程

过程与最终答案共享聊天正文列，文件面板保留原有 owner 与布局。ReasoningPanel 仅提供工具详情。

- 助手回复顶部保留原头像与“助手 · 模型名称”，名称取本轮 `model_display_name` 快照，缺失时仅显示“助手”；底部不重复模型名。
- 明确的 `commentary` 归入过程；收到原生 `final_answer` START 即收起过程并显示“正在回答”，所有有效正式答复段落留在外面，但不改变 Run 终态或停止按钮。未知 phase 不按长度、关键词推断；仍沿用活动尾段与终态别名规则。明确 `PRESENTED` 文件引用通过 `tool_call_id` 匹配 `present_files` 时，其同一步交付正文也留在过程外，防止主体简报被后续补充遮住；其他文件操作不能作为此依据。
- 运行期间默认展示最新一段未中断的普通 `content` 进展，以及其后最新步骤的工具组；较早仍在运行的工具继续可见，较早已结束或结果未知的工具归入此前过程。无进展正文时显示最新工具组。顺序依据现有消息/工具身份，不按文本长短或关键词猜测。正文不截断为标题、不套单段折叠；`reasoning_content/thinking` 不进入正文或主复制，模型没有生成 content 时不补写。
- 思考预览仅出现在状态栏：Round为running、run为streaming且当前thinking segment开放并有文本时，显示静态原子图标、“思考中”和最新单行片段；以100ms固定节奏采样最新180个字符，轻微过渡并遵守reduced-motion，不按字逐个排队播放、不从历史thinking补播。预览不向读屏器逐token播报，也不成为正文阅读锚点。没有有效片段时使用转圈“运行中”；停止、等待交互、终态或原生正式答复优先。普通正文segment已开放且有内容时也先隐藏旧思考预览，不改变其后台生命周期。
- STEP边界只清空当前thinking指针，保留恢复所需buffer；无timestamp的END仍结束预览，旧ID的迟到END不得关闭新片段。“思考中”和“运行中”都是同一running任务的展示状态，不影响取消、准入或终态。点击状态栏仍是原有过程展开入口。
- 工具详情默认折叠；完成后整个过程默认收为“处理了 X 秒”，最终答案留在外面。耗时使用 RUN_STARTED/终态时间戳，缺失时显示“处理过程”，不把并行工具耗时相加。
- “运行中”等状态摘要是唯一的过程展开入口：点击完整展开，再次点击回到最新进展，不额外显示“查看此前过程”按钮。手动回看状态跨新消息/步骤保持，不因新进展自动关闭；展开前由唯一滚动管理器 `beginReading` 固定当前阅读锚点，即使原来位于底部也不自动追随追加内容。所有历史仍保留，不删除、重排消息或改变复制范围。
- 收起的运行预览预留 `clamp(160px, 32vh, 240px)` 高度，工具数量变化时在区域内滚动，不推挤下方布局；新工具组从预览顶部开始，同组更新保持位置。只有流式正文时允许自然增高，展开完整过程、确认正式答复或文件交付时解除预留限制，避免裁切长答案。此处仅管理局部工具区滚动，外部聊天滚动仍归统一阅读管理器。
- 顶部状态与工具行主图标复用 ActivityIcon（16px、线宽1.75），主箭头14px，主文字统一14px/24px；运行/结束只切图形与状态，不切尺寸。命令代码13px、网站专用20px外圈保留。
- 原生正式答复 START 或进入终态时统一收起过程；运行中手动展开、工具焦点、上滚或旧搜索均不阻止这次收起。完成后允许再次展开，刷新历史默认收起；运行态刷新则恢复最新进展视图。主过程不持久化运行期展开偏好，工具二级详情仍可按用户+Round+工具身份保存 sessionStorage 偏好，不保存文本。
- 搜索 nonce 在目标正文存在时消费一次（包括已可见的活动尾段），必要时展开过程；旧搜索不在完成后再次触发。过程收起时，位于隐藏工具内的焦点回到摘要按钮；已移除正文的阅读锚点按消息身份映射到本轮摘要，由 useChatReadingPosition 继续单独负责滚动。
- 对没有原生 phase 的消息，RUN_FINISHED 前无法证明流式文本属于终稿。最新未知 phase 正文按当前进展保留可见，直到后续进展替换；这不赋予其正式答复或主复制资格。终态仍使用明确终稿别名，缺少可靠终稿时保留已有有效正文。
- 最终消息使用同一父列表和稳定 key；Markdown 渲染器在正文增量更新时保持类型稳定。语义阅读锚点沿用 `useChatReadingPosition`，不增加第二个滚动写入者。文件卡片独立可见；最终展示与主复制共用 `answerNodes`，收起或展开的进展均不进入主复制。
- 工具结果统一投影为 `success: true | false | null`：事件/历史顶层布尔 `success` 优先，其次顶层布尔 `isError` 反转，再读取旧 `content` JSON 对象中的布尔 `success/isError`；缺字段一律为 `null`，绝不因收到结果、`error` 存在或缺少、关键词推断成败。结构化 error 与原始 content 仅作为展示上下文保留。工具状态只依据该真实结果：成功为 completed，明确失败为 failed；终态缺失结果为 unknown，不显示“完成”。工具失败只在所属工具或子任务入口表达，不生成顶部“有工具执行失败”汇总提示。命令工具折叠为状态＋实际 command/cmd 单行预览，超长省略；仅预览压缩空白，详情与复制保留原文。展开使用一个 Shell 小框，命令、输出、独立错误均直接可见，取消输出的二次折叠；内部最高220px并可键盘滚动，含顶栏整框约260px。命令、输出可分别复制，失败允许重试；没有命令的输出读取/进程停止操作保留其动作名及原参数。MCP、网站和普通正文代码块沿用各自的展示。
- MCP 工具通过 START 的 `toolDisplay` 与 history 的 `tool_display` 保存调用时身份，折叠显示 `server_name · (tool_title || tool_name || toolCallName)`；展开显示完整调用标识和不同的原工具名、参数与输出。身份不从参数/结果或当前目录推断；旧记录只有模型名时原样显示，不统一退成“调用工具”。已收到的身份按 toolCallId 在同轮恢复合并中保留，权威历史有快照时优先使用历史。
- `glm_search/glm_batch_search` 使用专用网站行：按工具结果的结构化文本头解析 HTTP(S) 来源，按 hostname 去重后显示“已搜索 N 个网站”；展开显示域名胶囊与真实目标链接，搜索详情二次展开。图标来自工具返回的 siteIcon，缺失/加载失败用本地通用图标，不向额外图标服务发送域名。部分失败保留已获得的来源并明确标注，未确认与零结果分开。
- 消息、输入框、错误和交互卡共用 `.chat-column`（最大宽度 860px，含统一的 16px / 桌面 32px 左右内边距），消息区域与输入卡外边缘对齐。以用户栏现有位置与间距为基准：两边头像均为 28px，头像与内容列间距 12px；助手标题、处理状态、正文、文件和复制按钮统一放入右侧内容列，与用户标题、正文和附件左对齐。用户保留“你”标签和左对齐时间，正文不使用气泡底色，每轮助手顶部展示一次头像与身份，分段正文不重复标题。正文沿用主题的 15px / 1.7，工具与状态 14px，代码 13px；不在行内消息上重复覆盖字号。相邻可见过程行保留18px留白，连续命令行收紧至8px；隐藏行不占间距，正文首尾margin归零。表格只保留外层间距和横向分隔线，内部table margin为0，引用不叠加上下padding。按钮有键盘焦点、aria-expanded，窄屏胶囊换行，动画尊重reduced-motion。
- 用户附件排在正文上方；本地文件、工作区文件与文件夹和 Skill/数据资源共用紧凑单行标签样式（42px 高、14px 字号、24px 图标区），前置文件类型图标，图片在图标区显示认证缩略图。名称超长省略，完整名称、类型与文件大小放在悬停提示，多附件按可用宽度换行。旧文本附件沿用同尺寸但不补造预览身份。正文与附件各自换行，纯附件不绘制空正文；文件点击仍传原身份与索引。

### 子任务入口与独立详情

- 相邻 `sub_agent` 调用显示为紧凑任务组：单个与多个都保留同样的数量汇总、行高、字号和间距；展示真实标题、准备中/进行中/已完成/失败/已停止状态，完成数只按带 child 的 graph `status=completed` 计。父 `subagent_run_updated` 事件与 history 的 `subagent_tasks` 提供 `tool_call_id → child_run_id` 关联；不再从工具输出字符串猜身份。graph 已有 task 时其 status 是唯一 child 生命周期来源，`requested/running` 不得被工具启动成功覆盖。无 child 时，明确启动失败显示“启动失败”，明确启动成功显示“已发起，等待状态同步”，旧记录或不可靠结果显示“状态未知”；均不伪造 child。任务行本身是唯一入口：无 child 点击该行展开调用结果，有 child 点击该行进入独立详情；不得在行下增加“失败原因”或“调用结果”第二按钮。已确认的启动失败只显示简短原因，已有带堆栈的失败记录只取错误首行作为展示摘要，不用于状态推断；未知旧记录仍保留原始结果。
- 点击后在当前聊天区域进入只读子任务详情，不增加右侧活动栏或新会话。父聊天DOM与输入框保持挂载并隐藏，草稿、模型选择、附件归属保持；子任务使用单Round snapshot＋按该Round订阅，独立reducer，不能进入主ChatRuntimeProvider消息列表。
- 主history继续排除child。子订阅断线只读取对应Round snapshot；停止观察/返回/切换会话中止本地请求，不调用abortChat、不停止子任务。迟到快照与事件按视图身份和epoch拒绝。任务终态不被旧requested/running事件回退，非空child ID不被旧null清除。
- 顶部提供“返回主对话/返回上级子任务”与任务名，任务说明放次级details；正文、工具和最终复制复用现有Round展示，隐藏内部委派用户消息。每层阅读管理器拥有独立child身份，上层视图保持挂载；返回恢复原锚点及可见入口焦点，主任务完成不强制离开子视图。
- 子任务只复制自己的最终答复，主任务仍只复制主助手最终汇总。文件引用沿原预览接口、删除过滤和打开意图世代；切换父子视图使旧迟到打开意图失效，不取消保存。

### 搜索定位

搜索从同一持久化 AssistantMessageProjection 匹配完整消息，允许关键词跨 CONTENT 增量。排除其他用户、subagent 子运行、系统/错误文本；legacy 保持旧助手消息/终稿降级。返回 `match_round_id + match_message_id`，前端先恢复历史并展开目标过程，待目标 DOM 可见后定位到对应正文。阅读管理器在内容、布局与 ResizeObserver 通知中优先完成待定位目标，可见前不消费 nonce 或恢复到底部；新目标可替换旧平滑定位，完成后同一 nonce 不再抢回用户滚动。无 messageId 的旧结果沿用轮次定位；不按模型文本猜身份。

## 8. 附件上传

- 图片：前端用 `readFileAsDataUrl` 读取原始 Data URL 后发送，避免截图/OCR 场景因压缩降质；体积保护由后端单张 20MB、总量 50MB 限制负责。
- Composer 文件选中即建立稳定附件卡片和本地预览，最多三个上传并发；使用临时草稿上传接口，状态为等待、上传、保存、就绪、失败。Axios 字节进度达到 100% 只代表传输完成，后端保存成功才可发送；未知总量不显示伪百分比。失败只影响该文件，支持重试与移除，上传中可以继续输入或追加附件。
- 单次粘贴超过 1,000 个 Unicode 码点时，原样生成 UTF-8 `.txt` File（`text/plain;charset=utf-8`）；恰好 1,000 个不转换，按本次粘贴文本而非输入框累计长度判断。普通文字、Markdown 和代码统一保存为纯文本，不猜测代码语言或更换后缀；不调用模型、不改写换行/内容、不吞掉已有正文。卡片支持预览、字数、移除与恢复原文到光标位置。文件剪贴板优先且保留原文件名/类型，键盘连续输入不自动转换；中文输入法确认 Enter 不发送消息。该阈值不改变既有正文发送长度限制。
- 临时附件属于草稿，不进入持久 Workspace。发送前 claim 返回服务端权威 Session 路径，普通文件和图片元数据都携带 `composer_draft_attachment_id`；失败保留草稿以便重试，移除/到期由后端清理未被真实 Round 引用的文件。Session 文件面板的显式上传继续使用既有 Session 接口。
- claim 的 20 项限制是单批准备上限，不是消息附件数量上限；前端顺序分批并在全部准备成功后才提交消息。中途失败保留原草稿，重试使用同一 Session/draft/attachment ID，已成功批次依靠服务端幂等复用。
- 本地预览支持图片、文本/Markdown和PDF；其他格式在发送前提供原文件下载，发送后沿用既有 Session 预览。页面内切换会话保留草稿；刷新/关闭页面不承诺恢复未发送的本地正文与 File。服务器临时文件在24小时到期后进入异步清理，物理回收受既有沙箱绑定限制（见 sandbox-spec §4“Composer 草稿附件目录”）。
- Composer 的 TXT/LOG 本地预览与发送后的文件工作台复用纯文本展示：固定自动换行、系统字体、15px / 1.7 行高、24–32px 留白，不嵌套正文卡片，不提供换行/字体选项或专用工具栏；展示不改变附件字节、复制或恢复原文。
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
- [ ] 取消权威确认后 UI 显示“已停止”，确认失败与执行错误分开
- [ ] 确认目标终态与准入释放后无需等待 SSE 即可发送；确认前仅允许编辑输入
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
- [ ] 工具审批 waiting subscribe 已安排 retry 时点击 Stop：旧 timer 不再建连，慢 abort 期间 UI 不回跳 waiting
- [ ] equal 或 unrelated higher cursor history 不覆盖尚未 END 的 text / thinking / tool args dirty segment；只有逐 segment 匹配的 server projection / aggregate 才可权威替换并推进 cursor
- [ ] ask_user 等待时无停止、取消或关闭入口；回答或跳过并提交后恢复普通输入区及执行中的停止按钮
- [ ] 工具审批等待时无纯本地关闭入口；审批与 Stop 均可离开等待态
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
- [ ] 正文与附件草稿按 session 隔离；切回会话可恢复，迟到上传只更新其捕获的 `draftId + clientId`，已移除卡片不复活
- [ ] 新会话粘贴/上传/移除、未发送离开均不新增 Session；Enter 后创建、claim、受理失败能够重试
- [ ] 单次粘贴 1,000 个 Unicode 码点保持正文，1,001 个原样转 `.txt`，可预览/恢复；多文件部分失败、上传中追加、取消晚响应和图片立即预览可用
- [ ] 回答中可预选下一轮模型和编辑草稿；下一轮实际模型及历史快照一致，resume仍使用原轮模型
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
