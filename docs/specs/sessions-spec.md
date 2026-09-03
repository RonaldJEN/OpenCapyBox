# 会话管理 (Sessions) — Spec

## 1. 模块职责边界

**负责：**

- 会话 CRUD（创建、列表、删除、标题更新）
- 文件浏览与下载（代理沙箱文件操作）
- 文件上传
- 运行状态查询（running-sessions）
- 历史记录查询（Round/Step 结构化）

**不负责：**

- 消息发送、Agent 执行、SSE 流

## 2. 数据模型

### sessions 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, indexed |
| title | String(255) | nullable |
| status | String(20) | default="active", indexed。取值：active / paused / completed |
| model_id | String(50) | nullable, indexed |
| created_at | DateTime | default=now, indexed |
| updated_at | DateTime | default=now, onupdate=now |

别名：Thread（AG-UI 协议中 session = thread）

`match_type` / `match_excerpt` 是列表搜索响应的派生字段，不落库。

## 3. API 契约

所有端点均需 Bearer auth。

### POST /api/sessions/create

- Query: `model_id: str | None`
- Response 200: `{session_id, model_id, message}`
- 创建 Session 行 + 触发 AgentPoolService 缓存预热

### GET /api/sessions/list

- Query: `q: str | None`（可选；搜索会话标题或讨论内容）
- Response 200: `{sessions: [{id, user_id, status, created_at, updated_at, title, model_id, match_type?, match_excerpt?, match_round_id?}]}`
- `q` 为空或全空格时返回完整列表；非空时仅返回当前用户匹配的 sessions
- 搜索范围：`sessions.title` + `rounds.user_message` + `conversation_messages(role=assistant).content` + `rounds.final_response`
- 用户消息只搜索 `rounds.user_message` 这种前端可见文本，不搜索 `conversation_messages(role=user).content` 中的 Agent 内部上下文 / 附件提示 / Data URL
- Agent 回复搜索 `conversation_messages(role=assistant).content`，排除 `tool` / `summary` / `synthetic`；`rounds.final_response` 作为历史重建兜底
- 搜索使用 PostgreSQL 兼容的 `ILIKE ... ESCAPE` 轻量匹配，用户输入中的 `\`、`%`、`_` 必须转义为普通字符
- 非空搜索最多返回 50 个 sessions；各搜索来源在 DB 侧按 session 取最佳命中并截断，避免侧栏搜索拉回无界历史
- `match_type` 取值：`title` / `user` / `assistant`
- `match_round_id` 在命中具体轮次时返回，供前端切换 session 后定位
- 排序优先级：title → user → assistant；同级内按更新时间倒序

### GET /api/sessions/{id}/history/v2

- Response 200:

```json
{
  "session_id": "...",
  "rounds": [RoundData],
  "total": 0
}
```

其中 `RoundData`:

```json
{
  "round_id": "...",
  "parent_run_id": "... | null",
  "idempotency_key": "... | null",
  "last_event_sequence": 0,
  "user_message": "...",
  "user_attachments": [],
  "thinking_mode": "enabled | disabled | provider_default | null",
  "reasoning_effort": "max | null",
  "final_response": "...",
  "steps": [StepData],
  "step_count": 0,
  "status": "running | waiting_interaction | completed | failed | cancelled | max_steps_reached",
  "created_at": "...",
  "completed_at": "...",
  "interrupt": null
}
```

`idempotency_key` 由客户端发送消息时生成；history/v2 必须返回该字段，供 accepted 但尚未收到 `runId` 的断线恢复路径按因果标识定位本次 round，不能用时间窗口猜测旧 round。
`last_event_sequence` 是该 Round 已持久化 AG-UI 事件的最大 sequence；前端在 history 已经重建 `running` 或 `waiting_interaction` Round 后订阅 SSE 时必须从该 sequence 之后接续，避免重复消费已展示事件。

当 `status=waiting_interaction` 时，`interrupt` 必须由该 Round 的 pending `agent_interactions` 投影：`id` 为 Interaction id，`reason` 按 kind 映射为 `input_required` / `human_approval`，`payload` 包含 kind-specific 请求和 `tool_call_id`。same-Round 路径不生成额外 Q/A Round。

history 读取前会处理过期 continuation claim：仅 `continuation_started_at` 为空的 pre-start ask_user / 审批可停回 `waiting_interaction`；started continuation 必须生成 durable `RUN_ERROR` 并收敛 failed，不得恢复旧卡。工具审批若已处于 `executing`，还必须先按 execution lease 收敛 unknown，绝不自动重放。

`history/v2` 只返回主聊天流可见 Round；被 `subagent_runs.child_run_id` 指向的 child Round 属于 sidechain，不得作为普通用户/助手对话返回。不能只用 `parent_run_id != null` 过滤，因为该字段还可表达非 subagent 的分支关系；sidechain 身份以 `subagent_runs.child_run_id` 为准。

其中 `StepData`:

```json
{
  "step_number": 1,
  "thinking": "...",
  "assistant_content": "...",
  "tool_calls": [
    {
      "id": "...",
      "name": "...",
      "input": {},
      "started_at_ts": 1710000000000,
      "ended_at_ts": 1710000000100
    }
  ],
  "tool_results": [
    {
      "tool_call_id": "...",
      "success": true,
      "content": "...",
      "error": null,
      "received_at_ts": 1710000000300,
      "execution_time_ms": 200
    }
  ],
  "status": "...",
  "created_at": "...",
  "thinking_start_ts": 1710000000000,
  "thinking_end_ts": 1710000000100,
  "started_at_ts": 1710000000000,
  "finished_at_ts": 1710000000400
}
```

`*_ts` 字段均为 AG-UI 事件时间戳（毫秒），用于前端恢复历史时重建思考、工具调用和工具结果耗时；旧事件缺少对应时间戳时字段为 `null` 或省略。

- Error 404

### PATCH /api/sessions/{id}/title

- Body: `{title: str}`（1-255 字符）
- Response 200: SessionResponse
- Error 404
- 注意：前端目前不使用此端点，标题由后端通过 CUSTOM SSE 事件自动生成

### DELETE /api/sessions/{id}

- Response 200: `{message: "会话已删除"}`
- Error 404
- 数据删除：移除 Session、Round 及其 events/messages/LLM calls/interactions/approvals；带 `conversation_round_id` 的新对话向量随 Round 删除。旧向量允许 ownership 为空，不回填且不阻断删除。
- 物理删除：数据库事务同时写入精确 Sandbox cleanup job；Session 立即删除，no-follow worker 幂等删除 `sessions/{id}`。Sandbox 暂不可用时保留 job 重试，不再丢失清理意图。

### GET /api/sessions/{id}/files

- Query: `path: str = ""`（相对子目录）
- Response 200: `{files: [{name, path, size, modified, type, is_directory, revision}], total}`，其中文件 `revision` 固定为 `v1:<size>:<mtime_ns>` opaque identity。
- `modified` 为带显式时区偏移的 ISO 8601 时间字符串，当前统一返回 UTC（如 `2026-05-08T02:30:00+00:00`）。
- 只有成功枚举出的零项才返回 `200 + files=[]`；沙箱连接、目录命令或 JSON 解析失败会强制重连一次，仍失败返回 503，不得伪装成空目录。
- 列表/预览/下载/上传都是被动 Sandbox consumer：请求先在 DB 中冻结本次访问的 owner 绑定并释放请求连接，再执行 Sandbox 网络 I/O。存在新鲜 Agent `UserRunLock` 或 Cron claim 时，只允许 `get_existing` 连接 owner 已冻结的 `sandbox_id`；绑定尚未冻结、互相冲突或实例不可恢复时返回 503，禁止 `get_or_resume` 创建替代实例或改写 `UserSandbox`。同一请求的重试必须复用首次冻结的绑定。
- Error 404, 403（"路径越界"）, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）, 503（沙箱或目录读取不可用）
- 目录枚举统一在用户沙箱内执行 Python `os.listdir/stat`，不依赖 proxy 模式下会丢 query 参数的 `files.search`

### GET /api/sessions/{id}/files/{path:path}

- Query:
  - `preview: bool = False`
  - `render: "pdf" | null = null`；仅在 `preview=true` 时有效
  - `edit: bool = False`；当前 Session 的 Markdown/CSV/XLSX 编辑读取必须传 `edit=true`。在同一次沙箱快照中读取正文、SHA-256 和文件 revision，响应正文来自该不可变副本，响应头为 `X-Session-File-Revision`、`X-Session-File-Modified`、`X-Session-Edit-Base`，禁止从目录 metadata 猜正文版本。
  - `base_token: str | null`；配合 `edit=true` 读取指定编辑基线的固定正文，用于接纳合并回执。基线签名绑定用户、Session、相对路径、SHA、大小和 mtime；不允许编辑 captured/system 路径。
- Response: 文件字节流，`Content-Disposition` = attachment（下载）或 inline（预览）
- 可预览类型：text/\*、image/\*、PDF、JSON、XML
- `render=pdf`：仅接受 DOC/DOCX/PPT/PPTX。在**用户 OpenSandbox 内**以单一进程完成源文件的 50 MiB 有界快照、SHA-256 与复制，API 主机不得读取或执行不可信 Office 内容；随后在沙箱内调用 LibreOffice。派生 PDF 缓存在 session 根目录下的隐藏 `.opencapybox-preview/{content_hash}/`，不得出现在文件列表中，并随 session 删除。
- `.assistant-artifacts/{round}/...` 仅承载 `present_files` 迁移前已经持久化的旧 Session 助手引用，不再为新引用创建。history 可原样返回旧引用的 `snapshot_path`；当前文件缺失时，旧卡片仍可只读回退到生成时快照，且不得据此创建或恢复当前文件。新 `present_files` 引用只记录当前 Session 路径，不复制正文；文件被覆盖后打开最新内容，被删除后提示不可用。隐藏目录不参与普通枚举，既有快照可使用 immutable cache header，并随 Session 删除。
- 派生预览以文件内容 hash + 扩展名 + renderer 版本为缓存键；同内容重复预览直接复用 PDF。
- 相同内容通过沙箱内原子目录锁收敛为一次转换；每请求使用唯一 scratch/LibreOffice profile，先验证临时 PDF 的 `%PDF-` magic 与大小，再原子发布，禁止命中 partial cache。
- 请求取消或 shell/SDK 超时不得再次取消 `.incoming-*` 与 LibreOffice profile 的清理；清理和锁释放完成后才能传播取消。每次 Office 快照还必须在同一隐藏缓存根下删除严格匹配 `.incoming-<32位小写十六进制>` 且超过 300 秒的中断残留，不跟随 symlink、不触碰内容 hash 缓存目录或当前请求 scratch。
- Office 源文件最大 50 MiB、派生 PDF 最大 100 MiB；shell `timeout -k` 与 OpenSandbox SDK timeout 双重限制转换时间。成功响应优先流式读取派生 PDF。
- 失败只影响当前预览，不改变 Session/Round 状态。
- Error 404（源文件不存在）, 400（"文件路径不合法" 或未开启 preview 却请求 render）, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）, 413（源或派生预览文件过大）, 415（不支持的派生格式）, 422（文档损坏/转换失败）, 503（沙箱或 renderer 不可用）, 504（转换超时）

### PUT /api/sessions/{id}/files/{path:path}

- 支持 `.md/.markdown` 的 UTF-8 `content`，以及 `.csv/.xlsx` 的 `content_base64`；旧二进制 `.xls`、`.et` 与其余扩展名返回 415，只允许下载/只读预览。
- 新编辑请求携带与正文成对取得的 `expected_revision`、`edit_base_token`，以及每代草稿稳定的 `save_id`。文件内容变化时复用 Workspace 三方合并算法：base 为打开时正文，current 为本次用户草稿，proposal 为远端当前正文；互不重叠的改动保留，重叠按既有算法用户草稿优先。最终在单文件锁内校验合并所依据的当前 SHA/size/mtime，再原子替换；并发变化有界重算，持续变化返回可重试 `SESSION_EDIT_RETRY`。受控 Session `apply_patch` 对已有源文件携带读取时观察到的 SHA，对 Add 与 Move 目标使用 must-not-exist，并在共享锁内完成 CAS；上下文失配或并发变化必须安全失败。任意 Bash/外部直接写文件不属于此协作锁协议。
- 基线和幂等回执仅保存在 Session 隐藏 `.opencapybox-edit/{bases,receipts,locks}`，不创建 Workspace entry/version/reference/checkpoint。基线是独立复制的不可变字节，使用现有预览缓存预算和草稿基线保留天数清理，回执另限 4 MiB，Session 删除时一起删除；它们不是永久历史。重复 `save_id` 和相同请求返回原回执，不重复应用合并。基线不可用或文件结构不支持合并时显式保留草稿，不得只推进 revision 后覆盖。
- 未携带 `edit_base_token` 的旧客户端仍使用 strict CAS；版本不一致返回 `409 {code:"SESSION_FILE_REVISION_CONFLICT", message, current, current_revision}`。旧草稿只有正文等于当前正文，或原 revision 与新读取基线一致时才能自动恢复；不能给未知来源旧草稿补一个新 revision 强写。
- `UserRunLock` 不阻止 Session 在线写回，不得以用户级或 Session 级运行锁扩大为整区只写门槛。
- Markdown 最大 5 MiB；电子表格解码后最大 20 MiB。CSV 必须是无 NUL 的有效 UTF-8；XLSX 必须是完整且有界的 OOXML ZIP，校验必要 XML、条目路径、重复/加密条目、CRC、总解压大小，以及 Content Types、根 `officeDocument`、workbook sheet 清单和 worksheet target 组成的完整关系图；Base64、文本编码、容器结构或关系图无效返回 422。
- Response 200: `{name, path, size, modified, type, is_directory, revision, edit_base_token, session_auto_merged}`。`session_auto_merged=true` 时客户端按回执 token 读取固定合并正文，将正文和基线一起接纳；读取失败仍保留草稿并使用相同 save_id 重试。`modified` 仅用于展示。

### GET /api/sessions/running-sessions

- Response 200: `{running_sessions: [{session_id: str, round_id: str|null}]}`
- `running_sessions` 返回当前用户所有持有新鲜 `UserRunLock` slot 的 session；新鲜判定为 `updated_at >= now - SSE_SUBSCRIBE_TIMEOUT`。
- `round_id=null` 表示仍处于 Agent 初始化窗口，尚未写入 running round。
- `waiting_interaction` 已释放 slot，因此不属于 running-sessions；客户端是否展示问题卡必须以 history/Interaction 为准，不能因该集合不含 session 而清除 waiting UI。
- 单次查询避免 N+1

### POST /api/sessions/{id}/upload

- Body: multipart file
- Response 200: `{name, path, size, modified, type, is_directory}`
- `modified` 为带显式时区偏移的 ISO 8601 时间字符串，当前统一返回 UTC（如 `2026-05-08T02:30:00+00:00`）。
- Error 400（"未选择文件"）, 404, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）, 503, 500
- 上传文件落盘在 session 根目录，文件名经过 `_sanitize_filename` 清洗：主文件名部分保留 Unicode 字母/数字（含中文）、下划线、连字符、点号；空格、括号等特殊字符替换为下划线并合并连续下划线；扩展名部分保留原样。若清洗后同名文件已存在，追加 `_1`、`_2` 等序号，避免覆盖。

## 4. 行为语义与不变量

- 一个 Session = 一个 AG-UI Thread
- Session 删除是级联的：rounds → agui_events / conversation_messages / llm_call_records / agent_interactions / tool_approval_requests 全部删除
- 文件路径校验：必须在沙箱 mount 路径内（`is_within_sandbox_root`），否则 403
- running-sessions 查询通过 `UserRunLock` + `Session` + `Round` 实现单次查询，返回所有活跃且未超过 `SSE_SUBSCRIBE_TIMEOUT` 的 slot；它不等于“有未解决 Interaction 的 session 集合”
- 历史 v2 通过 AGUI 事件动态重建 Step 结构，并结合 `agent_interactions` 投影 waiting interrupt；`interaction_requested` / `interaction_resolved` 以同一 Round sequence 重建 tool result

## 5. 失败模式与错误处理

- 沙箱 Profile 配置冲突（如绑定后端不存在/禁用）时文件操作保留服务层 409；普通沙箱连接不可用时文件操作返回 503
- 沙箱过期时删除会话：DB 记录正常删除，沙箱文件可能残留（日志记录）
- 文件读取支持 fallback（SDK → base64 命令）
- Session 不属于当前用户时返回 404（不暴露是否存在）

## 6. 可观测性

- 会话创建/删除有日志
- 沙箱文件操作失败有 warning 日志
- 文件下载 fallback 路径有日志

## 7. 非目标

- 不做会话分享/协作
- 不做会话导出
- 不做会话归档
- 不做文件版本管理
- 不做大文件分片上传

Workspace-origin 附件在工作区 entry 删除后不再从历史返回；文件附件按 entry_id 命名的平台 `.workspace-snapshots` 随工作区直接删除清理。文件夹附件只保存实时 entry 引用，不在 Session 中复制目录。独立 Session 产物不受影响。
