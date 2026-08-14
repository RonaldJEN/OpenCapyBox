# 前端 Chat Spec — 聊天 / SSE / 推理面板

> 父级：[frontend-spec.md](./frontend-spec.md) · 对应后端：[chat-spec.md](./chat-spec.md)

覆盖组件：`ChatV2.tsx`、`Round.tsx`、`ChatInput.tsx`、`ReasoningPanel.tsx`、`QuestionCard.tsx`。

## 1. 模块职责

- 发送用户消息（含附件、引用图片）
- 消费后端 SSE（AG-UI 事件）增量构建 `RoundData[]`
- 渲染消息流（user → reasoning → assistant）
- 选择并发送仅作用于当前逻辑执行链的优先 Skill
- 将助手回复中的会话文件引用抽取为回复底部的可点击文件卡片，同时保留 markdown 正文原样显示
- 处理中断/恢复：断连重连、ask_user 中断、用户主动取消
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
  final_response: string | null
  steps: StepData[]
  step_count: number
  status: 'running' | 'completed' | 'failed' | 'interrupted' | 'cancelled' | 'resumed' | 'max_steps_reached'
  created_at: string
  completed_at?: string
  interrupt?: InterruptDetails       // ask_user 中断
}

PreferredSkillSnapshot {
  key: string
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

SkillDraft {
  keys: string[]   // GET /api/config/skills 返回的稳定内部 key
  revision: number // 乐观清空与失败恢复的并发保护版本
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
- 渲染前必须按目标父目录查询当前 session 文件列表；只有命中同路径文件时才渲染文件卡片。
- 未命中或校验失败说明该文本只是助手描述、文件尚未生成或当前状态不可确认；此类引用必须直接隐藏，不展示文件卡片或错误提示。
- 点击时仍需二次校验目标文件存在；若已渲染卡片在点击时失效，不得打开预览，可提示用户文件不存在或尚未生成。
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

订阅断连且目标 round 仍为 `running` 时，前端最多静默重试 3 次；重试期间不得展示错误横幅。重试耗尽后才展示“订阅连接已断开，Agent 可能仍在运行。请刷新页面查看结果”，提醒用户刷新查看最终结果。

`_ROUND_TERMINAL_STATUSES` 必须与后端 `Round.SUBSCRIBE_TERMINAL_STATUSES` 保持一致。

#### 3.3.1 初始 POST 接受歧义状态机

初始 `message/stream` POST 在响应头到达前发生网络错误时，必须按以下状态机处理：

```text
pre_accept_pending
  ├─ 收到响应头 / stream_accepted ───────────────→ accepted
  ├─ 确定性 HTTP 4xx/5xx ───────────────────────→ definite_rejected
  ├─ 响应头前网络错误 ──────────────────────────→ ambiguous
  │    ├─ history 命中同 idempotency_key ───────→ accepted（订阅 running 或收敛终态）
  │    ├─ 3 次 history 全部成功且均无匹配 ──────→ definite_rejected
  │    └─ 3 次中任一次失败且最终未命中 ─────────→ ambiguous_unknown
  └─ 用户主动取消 ──────────────────────────────→ client_cancelled_unknown
```

- `ambiguous` 期间绝不重发 POST；固定使用原 `idempotency_key` 查询 history，当前确认预算为 3 次。
- 只有 3 次查询全部成功且都无匹配，才能调用一次 `onRejectedBeforeAccept`、恢复乐观清空的草稿并提示请求失败。任一次查询失败都会使“无匹配”证据不完整；预算耗尽后保持 `ambiguous_unknown`，提示刷新查看，禁止恢复草稿、提示重新发送或用新幂等键自动发送。
- 用户在响应头前取消：立即 abort POST，停止本地订阅，不启动 history 确认，不发出 `stream_accepted` / `RUN_ERROR` / 接受前拒绝回调，并保持乐观清空后的草稿，防止服务端其实已接受时重复发送。
- 用户在等待 history 时取消：停止后续确认，忽略在途 history 的迟到结果；即使迟到结果命中 running Round，也不得建立 subscribe。该路径同样不恢复草稿、不自动重发，用户只能刷新查看服务端事实。

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

### 3.9 本轮 Skill 偏好

#### 选择器交互

- `ChatInput` 仅在用户显式打开 Skill 选择器时加载普通 `GET /api/config/skills`，每次重新打开都从服务端 DB 快照刷新清单，列表只展示 `enabled === true` 的项目；不得在页面初始化时预取或使用 `refresh=true` 触发远程恢复。已有成功清单时采用 stale-while-refresh：立即展示旧清单并以轻量状态提示请求，不得重新用整面 loading 遮住列表。
- 当前实现不跨组件实例缓存清单。组件实例内关闭后尚未完成的同一请求可在重开时复用，避免重复远程恢复沙箱；请求完成后的下一次重开仍须发起新刷新。若以后增加更长生命周期缓存，缓存与进行中的请求必须按认证用户隔离，并在登录用户、token 身份或 Skill 启停状态变化时立即失效，禁止跨账号复用私有 Skill 名称、描述或启停状态。
- “尚未加载”“首次加载中”“已成功加载空列表”“后台刷新中”“首次加载失败”“后台刷新失败”必须是可区分状态。成功空列表或一次失败都不得因弹窗仍打开而触发自动请求循环；首次失败只能由用户显式重试，后台刷新失败须保留旧清单并提供重试入口；服务端返回 `inventory_state=stale` 时也必须保留清单并明确说明正在显示上次成功结果。
- 列表用 `display_name` 展示名称（缺失时回退 `name`），用 `key` 作为选择、去重和请求标识；不得把展示名称提交给后端。
- 搜索同时匹配 `display_name`、`name`、`key` 和 `description`，忽略大小写与首尾空白；每次重新打开选择器时清空上次搜索词。
- 选择项以可移除标签显示，最多选择 50 项；已达到上限时不得继续新增，但仍允许取消现有选择。每个可切换行必须通过 `aria-pressed` 暴露当前选中态。
- 桌面端选择器锚定在输入框上方，通过点击外部或按 `Escape` 关闭；移动端使用底部浮层并额外提供关闭按钮。关闭不清空已选择的 Skill。
- 文案必须明确“优先考虑，不强制调用”；是否加载 Skill 仍由 Agent 根据当前请求相关性决定。

#### 已发送消息展示

- `Round` 在普通 direct Round 的用户消息旁展示只读“优先 Skill”标签，数据源为该 Round 的 `preferred_skills`。标签显示持久化的 `display_name`，需要辅助提示时可同时暴露稳定 `key`；不得重新查询当前 Skill 清单来替换历史展示名。
- 标签表达“这次发送要求 Agent 优先考虑”，不表示该 Skill 已加载、被调用或实际参与生成结果。UI 不得使用“已使用”“已调用”等成功态文案或图标。
- `preferred_skills=[]` 时不渲染标签容器。resume child Round 即使运行时继承了父逻辑执行链的偏好也保持空数组，前端不得在 Q/A 或工具审批 child 用户消息旁重复展示；标签只出现在最初的 direct Round。
- 每个独立 direct Round 只展示自己当次发送的快照，不继承或合并前一 Round 的标签。后续 Skill 被禁用、改名或删除也不得改写已有历史标签。
- 新消息尚未拿到服务端 Round 数据时，可用本次 composer 快照做 optimistic 展示；收到 `RUN_STARTED.preferredSkills`（显式空数组也算权威结果）后立即替换，刷新/断线恢复再以 `history/v2` 的持久化快照为准，确保无效 key 被清除且实时视图与历史一致。

#### 会话草稿与发送

- Skill 选择是输入草稿的一部分，按 session key 隔离保存；切换会话不得把 A 会话选择带到 B 会话。尚未创建 session 时使用独立的新会话草稿，创建成功后须迁移到实际 session，后续恢复也以实际目标 session 为准。
- 正文与附件使用独立的 `MessageDraft` 按相同 session key 隔离；草稿包含稳定 `draftId` 与递增 `revision`。正文编辑、附件增删递增 revision，session key 迁移不得改变 draftId。
- 新会话仍以 `__new_session__` 作为客户端映射 key，但附件上传必须先取得真实 server session ID。上传等异步回调绑定发起时的 `draftId + serverSessionId`，不得根据回调执行时的当前活跃会话决定写入位置。
- 从 `__new_session__` 迁移到真实 session 时，MessageDraft 与 SkillDraft 必须在同一转换路径协调迁移；目标已有较新草稿或 draftId 已变化时，迟到响应不得覆盖或重新创建旧草稿。
- 发送时对当前草稿创建不可变快照，并将其作为 `preferred_skill_keys` 与正文、附件一并提交；空数组可省略。之后用户对选择器的编辑不得改变已经发出的请求。
- 提交发送时乐观清空该目标 session 的 Skill 草稿。服务端确认 SSE 已接受（`stream_accepted` 或 `RUN_STARTED`）后保持清空；执行已被接受后的流式失败、中断或取消不得恢复旧选择。
- composer 清空只影响下一条待发送草稿，不得删除或隐藏已经固化在当前 direct Round 用户消息旁的 `preferred_skills` 标签。
- 若 POST 在收到响应头前发生网络错误，前端须按 §3.3.1 用同一 `idempotency_key` 查询历史：匹配到 running/终态 Round 即视为已接受，补发一次 `stream_accepted`，随后立即订阅或收敛终态；从历史恢复的失败终态也必须携带真实 `threadId`、`runId` 和末事件序号。只有 3 次 history 均成功且均无匹配时才恢复发送快照并报请求失败；任一次 history 失败则保持歧义、草稿保持清空并提示刷新。确定性的 HTTP 4xx/5xx 仍立即恢复；恢复回调最多执行一次。
- 从 history 直接收敛已完成/失败终态时，必须先合成一个无 sequence 的 `RUN_STARTED`，携带该 Round 的 `preferred_skills`（旧数据按 `[]`），再派发 terminal；已知 run 的订阅终态兜底同样如此。Reducer 必须允许这个补偿事件按 server run id 命中已绑定 Round，以纠正 optimistic 展示名并清除无效 key。
- 失败恢复必须带 revision 保护：若乐观清空后用户没有新编辑，精确恢复快照；若用户已新增或移除选择，则保留当前编辑，并把快照中缺失的 key 按原顺序无重复合并，不能用旧快照覆盖新编辑。
- `ask_user` 或工具审批产生的 child resume round 由后端继承并重新解析原请求 Skill；前端 `resume` 不重复发送 `preferred_skill_keys`。用户之后独立发送的新消息只使用当时该 session 的新草稿。

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
  - 进行中：渲染醒目的 `正在思考` 卡片，显示实时耗时、当前最新 thinking 的最多 3 行滚动预览与 `查看活动` 入口；完整内容保留在活动抽屉。预览默认每 3 个动画帧跟随末尾，用户滚离底部后必须暂停自动跟随，回到底部后恢复；末尾用流式闪烁点表示仍在生成。
  - 完成态：渲染紧凑的 `已完成思考` 胶囊，显示整轮思考总耗时。
  - 点击 `查看活动` 或完成态胶囊后打开右侧活动抽屉；不得在主聊天区展开 thinking 详情。
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
- [ ] 普通进入长会话时定位到底部；A 滚到中间 → 切 B → 切回 A，A 仍定位到底部
- [ ] 搜索结果带 `match_round_id` 时定位到命中 round，而不是底部
- [ ] 首屏历史渲染无瀑布动画
- [ ] 底部跟随：用户滚离底部时新消息不强制滚动
- [ ] 用户滚离底部且新回复正在生成时，底部按钮显示 live reply 指示
- [ ] 助手文件提示行/路径保留在 markdown 正文，同时在回复底部渲染去重文件卡片；点击后打开 Files 抽屉并直接预览目标文件
- [ ] 代码块内的文件名/命令不触发文件卡片
- [ ] Skill 选择器仅在显式打开时加载并在重新打开时后台刷新；已有清单立即展示，未完成请求可复用，只展示 enabled 项
- [ ] Skill 清单不跨组件实例/账号复用，退出后切换账号不泄露上一账号的私有 Skill；成功空列表与请求失败均不产生自动重载循环，刷新失败保留旧清单
- [ ] Skill 使用 `display_name` 展示、`key` 提交；搜索重开清空；选择、标签移除、50 项上限、桌面点击外部/`Escape` 与移动端关闭按钮行为正确
- [ ] 普通 direct Round 在用户消息旁按 `preferred_skills` 展示只读标签；空数组不展示，resume child 不重复展示，文案不暗示 Skill 已加载或调用
- [ ] Skill 草稿按 session 隔离；A/B 会话切换互不污染，新会话创建后草稿迁移到实际 session
- [ ] 正文与附件草稿按 session 隔离；切回会话可恢复，迟到上传只更新其捕获的 `draftId + serverSessionId`
- [ ] 新会话的正文、附件与 Skill 协调迁移；重复或迟到的创建结果不得覆盖真实 session 下的较新草稿
- [ ] 发送携带快照中的 `preferred_skill_keys`，空选择不发送该字段；发送后目标 session 的选择清空
- [ ] composer 清空后已发送 Round 的 Skill 标签仍保留；刷新或断线恢复后以 `history/v2` 的持久化 `display_name` 快照还原，独立多轮不继承或累积
- [ ] 接受前 4xx/5xx 立即恢复 Skill 快照；网络歧义按同一幂等键查询 3 次 history，确认 Round 后不恢复，仅 3 次均成功且无匹配时恢复一次；任一次 history 失败则保持 ambiguous、提示刷新且不恢复/重发
- [ ] 响应头前取消会 abort POST 且不查 history、不恢复草稿；等待 history 时取消会忽略迟到结果、不建立 subscribe、不恢复草稿
- [ ] 接受前拒绝后若用户已修改 Skill 草稿，保留新编辑并无重复合并旧快照
- [ ] ask_user 与工具审批 resume 延续原请求的 Skill 偏好；下一条独立消息不继承

## 11. 已知易错点

1. 忘记在新的 useEffect 里加 `boundSessionId` 校验。
2. 新增 SSE 事件类型时忘记在 `services/api.ts` 的 dispatcher 注册。
3. `ToolResult` 很大时直接渲染导致卡顿 → 用 `TruncatedCodeBlock`。
4. `applyPatch` 失败时吞异常 → **必须 console.error**，否则 state 偷偷停更。
5. 取消后未清除 `sending` → 输入框卡死。
