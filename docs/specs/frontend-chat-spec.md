# 前端 Chat Spec — 聊天 / SSE / 推理面板

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[chat-spec.md](./chat-spec.md)

覆盖组件：`ChatV2.tsx`、`Round.tsx`、`ChatInput.tsx`、`ReasoningPanel.tsx`、`QuestionCard.tsx`。

## 1. 模块职责

- 发送用户消息（含附件、引用图片）
- 消费后端 SSE（AG-UI 事件）增量构建 `RoundData[]`
- 渲染消息流（user → reasoning → assistant）
- 选择并发送仅作用于当前逻辑执行链的 Skill/MCP 统一偏好
- 将助手回复中的会话文件引用抽取为回复底部的可点击文件卡片，同时保留 markdown 正文原样显示
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
- 文件卡片使用单行紧凑附件行：最大宽度 520px、最小高度 52px；桌面端按文件名内容收缩且最小宽度 280px，移动端占满可用宽度。文件类型由左侧类型图标和文件名扩展名表达，不重复显示 category / extension / size 元数据行。
- 文件名使用与正文同级的 15px 中等字重（500），不得用偏小粗体制造虚假层级；中文、英文和长文件名都保持稳定基线。
- 整行是唯一点击目标，不在右侧重复放置“查看”、面板或外链符号。长文件名必须在 `min-w-0` 容器内单行截断，移动端宽度不得溢出。
- 非图片文件使用统一的实心蓝文件标，内部白色符号区分文档/代码/表格/演示/压缩包；不得使用低对比灰色线框或不同视觉家族的图标。图标为装饰，按钮自身保留完整 `aria-label`。
- 文件卡片调用 `ChatV2` 的 Session 文件入口，打开聊天/文件双工作区并把目标文件传给 `ArtifactsPanel`。
- 渲染前必须按目标父目录查询当前 session 文件列表；只有命中同路径文件时才渲染文件卡片。
- 未命中或校验失败说明该文本只是助手描述、文件尚未生成或当前状态不可确认；此类引用必须直接隐藏，不展示文件卡片或错误提示。
- 文件卡片已经由父目录查询确认存在；点击时必须在同一前端提交内打开文件工作台并投影目标标签，不得再用目录网络请求阻塞首帧。若文件随后失效，由目标标签自己的预览请求显示局部错误，不得回写 Round/Session 状态。
- `ArtifactsPanel` 直接进入面板内 `FilePreview`，不走全屏预览弹窗。
- 用户上传附件仍沿用原有 `onPreviewAttachment` 全屏预览链路。

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

- 输入框底部使用唯一 `+` 根菜单，固定包含“上传文件”“专家 Skills”“数据连接”；模型/推理等级仍常驻底栏。三个入口互斥，桌面端向上展开，移动端使用底部浮层；`Escape` 关闭并把焦点还给 `+`，点击外部关闭。
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
- 发送时冻结正文、附件与 TurnPreferenceDraft，并分别提交 `preferred_skill_keys`、`preferred_mcp_server_ids`；空数组省略。之后编辑不得改变在途请求。
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
- [ ] 底部跟随：用户滚离底部时新消息不强制滚动
- [ ] 用户滚离底部且新回复正在生成时，底部按钮显示 live reply 指示
- [ ] 助手文件提示行/路径保留在 markdown 正文，同时在回复底部渲染去重的单行紧凑文件卡片；图标表达格式、不重复渲染元数据行，点击后打开 Session 文件工作台并直接预览目标文件
- [ ] 代码块内的文件名/命令不触发文件卡片
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
