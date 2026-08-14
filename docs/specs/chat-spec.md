# 聊天与 Agent 执行 (Chat) — Spec

> **模块归属**: `src/api/routes/chat.py`, `src/api/services/turn_orchestrator.py`, `src/api/services/agent_service.py`, `src/agent/agent.py`
> **最后更新**: 2026-07-17
> **状态**: Draft

---

## 目录

1. [模块职责边界](#1-模块职责边界)
2. [数据模型](#2-数据模型)
3. [API 契约](#3-api-契约)
4. [行为语义与不变量](#4-行为语义与不变量)
5. [失败模式与错误处理](#5-失败模式与错误处理)
6. [可观测性](#6-可观测性)
7. [非目标](#7-非目标)

---

## 1. 模块职责边界

### 本模块负责

| 职责 | 说明 |
|------|------|
| 消息发送与 SSE 流式响应 | 接收用户消息，启动 Agent 执行，通过 SSE 实时推送事件流 |
| Agent 执行生命周期 | 管理从启动到终态的完整流程：启动 → 工具调用 → 完成/中断/取消/错误 |
| SSE 订阅与断线重连 | 支持客户端重连后从指定 sequence 恢复事件流 |
| 执行中断与恢复 | Human-in-the-Loop：`ask_user` 补充输入及工具权限审批均可暂停执行并通过结构化 resolution 恢复 |
| 执行取消 | 支持主动取消当前单 worker 进程内正在运行的 Agent；跨 worker 投递不属于第一版能力 |
| 幂等性保证 | 前端重复提交相同 `idempotency_key` 不会产生多次执行 |
| AG-UI 事件生成与持久化 | 生成标准 AG-UI 事件并写入数据库，支持事后重放 |
| 上下文压缩 | 多级压缩策略，确保对话历史不超出模型上下文窗口 |
| 用户 token 限额门禁 | 在启动 send/resume run 前检查用户周/月 token 限额 |

### 本模块不负责

- 会话 CRUD（由 Session 模块处理）
- 文件操作（由 OpenSandbox 文件服务处理）
- 模型管理（由 Model Registry 处理）
- Cron 定时任务执行
- 技能（Skills）的注册与管理
- 用户权限配置与限额配置（由 Auth/Admin 模块处理）

---

## 2. 数据模型

### 2.1 `rounds` 表（别名：Run）

一条 Round 对应一次完整的 Agent 执行周期。用户每发送一条消息（或恢复一次中断）都会创建一条 Round。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | String(36) | PK | UUID，全局唯一 |
| `thread_id` | String(36) | FK → `sessions.id` (CASCADE), indexed | 所属线程（当前等同于 session） |
| `session_id` | String(36) | FK → `sessions.id` (CASCADE, `use_alter`), indexed | 所属会话 |
| `parent_run_id` | String(36) | nullable, indexed | 链接 interrupt → resume，指向被中断的 Round |
| `outcome` | String(20) | nullable | 执行结果：`success` / `interrupt` |
| `user_message` | Text | NOT NULL | 用户原始消息内容 |
| `user_attachments` | Text | nullable | JSON 序列化的附件列表 |
| `preferred_skills` | Text | nullable | JSON：`[{key, display_name}]`；普通 direct Round 在本次发送开始时解析出的有效“优先 Skill”展示快照，旧数据 `null` 按 `[]` 处理 |
| `final_response` | Text | nullable | Agent 最终文本响应 |
| `step_count` | Integer | default=0 | Agent 执行步数 |
| `status` | String(20) | default=`"running"` | 当前状态（见下方状态机） |
| `interrupt_payload` | Text | nullable | JSON：`{id, reason, payload, runtime_context?, preferred_skills_origin_user_message_id?}`；后两项由服务端写入 |
| `idempotency_key` | String(64) | nullable | 前端生成的幂等键 |
| `created_at` | DateTime | default=now, indexed | 创建时间 |
| `completed_at` | DateTime | nullable | 终态达成时间 |

**唯一约束**: `UniqueConstraint(session_id, idempotency_key)`

**Round 状态机**:

```
                    ┌─────────────┐
                    │   running   │ ← 初始状态
                    └──────┬──────┘
                           │
            ┌──────────────┼──────────────┬───────────────┐
            ▼              ▼              ▼               ▼
      ┌───────────┐ ┌───────────┐ ┌────────────┐ ┌────────────┐
      │ completed │ │  failed   │ │ interrupted│ │ cancelled  │
      └───────────┘ └───────────┘ └─────┬──────┘ └────────────┘
                                        │
                                        ▼
                                  ┌───────────┐
                                  │  resumed  │
                                  └───────────┘
```

**终态集合**:

| 集合名称 | 包含状态 | 用途 |
|----------|----------|------|
| `COMPLETE_TERMINAL` | `completed`, `failed`, `cancelled`, `max_steps_reached` | 判断 Round 是否已彻底结束（不可恢复） |
| `SUBSCRIBE_TERMINAL` | `completed`, `failed`, `interrupted`, `resumed`, `cancelled`, `max_steps_reached` | 判断 SSE 订阅是否应关闭连接 |

> **设计决策**: `interrupted` 和 `resumed` 被纳入 `SUBSCRIBE_TERMINAL` 但不在 `COMPLETE_TERMINAL` 中——中断态的 Round 虽然暂停了 SSE 推送，但仍可通过 resume 恢复执行。`resumed` 表示该 Round 已由后续 Round 接替，其 SSE 订阅也应关闭。

### 2.2 `agui_events` 表

持久化所有 AG-UI 事件，用于 SSE 重放（断线重连、订阅历史 Round）。

`CUSTOM synthetic_user_message` 事件只作为冷恢复顺序锚点；完整合成消息内容（尤其是 `read_image_file` 产生的 Data URL 图片上下文）必须先写入 `conversation_messages(is_synthetic=True)`，成功后才允许提交轻量 marker 到 `agui_events.payload`。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | Integer | PK, autoincrement | 自增主键 |
| `run_id` | String(36) | FK → `rounds.id` (CASCADE), NOT NULL | 所属 Round |
| `event_type` | String(50) | NOT NULL | 事件类型（22 种之一） |
| `timestamp` | Integer | nullable | 事件时间戳（毫秒） |
| `message_id` | String(36) | nullable | 关联的消息 ID |
| `tool_call_id` | String(36) | nullable | 关联的工具调用 ID |
| `payload` | Text | NOT NULL | 完整 JSON 事件体 |
| `sequence` | Integer | NOT NULL | 事件序号（Round 内递增） |
| `created_at` | DateTime | default=now | 写入时间 |

**索引**（共 5 个）:

1. `run_id` — 按 Round 查询所有事件
2. `event_type` — 按事件类型过滤
3. `(run_id, sequence)` — 断线重连时按序号范围查询
4. `message_id` — 按消息定位事件
5. `tool_call_id` — 按工具调用定位事件

### 2.3 `conversation_messages` 表

Agent 执行所需的对话历史。与 `agui_events` 不同，此表面向 LLM 上下文构建，而非前端事件回放。实时运行上下文（如当前时间、时区、workspace）不写入此表。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | Integer | PK | 自增主键 |
| `session_id` | String(36) | FK → `sessions.id`, indexed | 所属会话 |
| `round_id` | String(36) | nullable, indexed | 所属 Round |
| `sequence` | Integer | NOT NULL | 会话内全局序号 |
| `role` | String(20) | NOT NULL | `user` / `assistant` / `tool` |
| `content` | Text | NOT NULL | JSON 序列化的消息内容 |
| `is_summary` | Boolean | default=False | 是否为上下文压缩产生的摘要 |
| `is_synthetic` | Boolean | default=False | 是否为系统合成消息（如 max_steps 提醒） |
| `token_count` | Integer | nullable | 消息的预估 Token 数 |
| `created_at` | DateTime | default=now | 创建时间 |

**唯一约束**: `UniqueConstraint(session_id, sequence)`

`is_summary=True` 仅保留给 `legacy_120` 兼容读取。默认 `checkpoint_v1` 不累计逐轮摘要，也不把多代摘要拼进 provider 请求；累计替代上下文以 `context_checkpoints` 为事实源。

### 2.3.1 `context_checkpoints` 表

保存不可变的累计替代上下文。每个新 generation 都完整替换此前 checkpoint，而不是在其后叠加摘要。

| 字段 | 说明 |
|---|---|
| `checkpoint_id` / `generation` | checkpoint 身份与 session 内单调代际 |
| `source_round_id` | replacement 覆盖到的最后 Round；允许是 running/failed/cancelled |
| `source_message_sequence` / `source_event_sequence` | 在 source Round 内精确覆盖到的 conversation/event 游标 |
| `trigger_phase` | `pre_turn`、`mid_turn` 或 `model_downshift` |
| `summary_text` | 模型生成的原始 handoff summary |
| `replacement_messages_json` | 最新真实 user 原文（总计最多 20,000 近似 token）+ 一条 role=user synthetic summary |
| `source_token_count` / `replacement_token_count` | 压缩前后 token 估算 |

`schema_version=4`，checkpoint append-only，并在每次压缩完成时立即写入，不等待 Round 成功。旧 v1-v3 不做语义迁移，直接从权威 rounds/messages/events 重建。恢复顺序为 `最新 replacement + source Round 游标后的 suffix + 后续 Round`；最新 v4 无法解析、缺少 `source_round_id` 或 source Round 不属于当前权威主会话历史时，必须丢弃整个 replacement 并回到权威历史，不得拼接 `replacement + 完整历史`，也不回退旧 generation。`replacement_sha256` 仅保留为 nullable 兼容列，不再参与语义。

### 2.4 `llm_call_records` 表

持久化每次 LLM 调用（step 级）的输入输出快照，用于运行后审计与问题排查。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | Integer | PK, autoincrement | 自增主键 |
| `session_id` | String(36) | FK → `sessions.id`, NOT NULL, indexed | 所属会话 |
| `round_id` | String(36) | FK → `rounds.id` (CASCADE), NOT NULL, indexed | 所属 Round |
| `step_index` | Integer | NOT NULL | 普通 step 从 1 开始；compaction 使用 session run 内递减的负索引避免唯一键冲突 |
| `call_kind` | String(30) | NOT NULL | `agent_step` 或 `compaction` |
| `request_message_count` | Integer | nullable | 本次实际发送给 provider 的消息条数（若为 provider 快照则取 `messages` 长度） |
| `manual_review_status` | String(20) | NOT NULL, default=`没问题` | 人工后台标注结果，默认表示未发现问题 |
| `request_messages` | Text | NOT NULL | 实际发送给 provider 的请求快照（JSON，包含 provider/model/messages、request-only runtime context，必要时包含 system/tools/stream 等参数） |
| `request_tools` | Text | NOT NULL | 本次可用工具名称列表（JSON，用于快速检索；真实工具请求体以 `request_messages` 为准） |
| `response_content` | Text | nullable | LLM 返回文本 |
| `response_thinking` | Text | nullable | LLM 思考内容（若模型支持） |
| `response_tool_calls` | Text | nullable | LLM 返回的 tool_calls（JSON） |
| `response_error` | Text | nullable | LLM 调用失败时的错误文本 |
| `finish_reason` | String(50) | nullable | 结束原因 |
| `usage_prompt_tokens` | Integer | nullable | prompt token 数 |
| `usage_completion_tokens` | Integer | nullable | completion token 数 |
| `usage_total_tokens` | Integer | nullable | 总 token 数 |
| `first_token_latency_s` | Float | nullable | 从发起请求到收到首个流式 token 的耗时（秒） |
| `completion_latency_s` | Float | nullable | 从发起请求到本次 LLM 调用完成返回的总耗时（秒） |
| `compaction_triggered` | Boolean | NOT NULL, default=`false` | 本次普通调用前是否触发 Codex compaction |
| `compaction_pre_tokens` | Integer | nullable | 压缩前估算 token 数 |
| `compaction_post_tokens` | Integer | nullable | 压缩后估算 token 数 |
| `compaction_tokens_saved` | Integer | nullable | 本次压缩节省 token 数（`pre - post`） |
| `compaction_microcompact_compacted_messages` 等旧字段 | Integer | nullable | 仅保留数据库/API 兼容，Codex 路径固定为 0，不再代表运行阶段 |
| `history_strategy` | String(30) | nullable | `checkpoint_v1` 或 legacy 策略 |
| `checkpoint_id` | String(36) | nullable | 本次请求使用的 checkpoint |
| `history_payload_sha256` | String(64) | nullable | 实际请求消息规范 JSON 哈希 |
| `history_breakdown_json` | Text | nullable | real user、assistant、tool、synthetic、图片上下文、累计摘要的数量分解 |
| `created_at` | DateTime | default=now, indexed | 写入时间 |

**唯一约束**: `UniqueConstraint(round_id, step_index)`

**限额语义**: Chat 模块使用 `llm_call_records.usage_total_tokens` 通过 `sessions.user_id` 聚合用户本周/本月 token 用量，并与 `auth_users.token_limit_per_week` / `auth_users.token_limit_per_month` 比较。

### 2.5 `user_run_locks` 表

用户级执行并发 slot。确保每个用户同一时刻最多有 `AGENT_USER_CONCURRENCY_LIMIT` 个不同 session 在执行，且同一 session 仍只能有一个 active run。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `lock_id` | String(36) | PK | UUID，用于标识锁的持有者 |
| `user_id` | String(100) | NOT NULL, indexed | 锁所属用户 |
| `session_id` | String(36) | NOT NULL, indexed | 锁定的会话 |
| `slot` | Integer | NOT NULL | 用户内并发 slot 编号 |
| `created_at` | DateTime | NOT NULL | 锁创建时间 |
| `updated_at` | DateTime | NOT NULL, onupdate=now | 心跳刷新时间 |

**并发语义**: `Unique(user_id, slot)` 原子限制同一用户可占用的 slot 数，`Unique(user_id, session_id)` 保证同一会话不可重入。若所有 slot 已占用，返回 429。

### 2.6 `run_cancel_requests` 表

取消请求 append-only 审计表。第一版按单 worker 部署，取消投递由进程内 `RunCancelService` registry + per-run cancel token 完成；DB 行只记录审计与诊断线索，不承担跨 worker command delivery。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `request_id` | String(36) | PK | UUID，用于跟踪取消请求 |
| `session_id` | String(36) | NOT NULL, indexed | 目标会话 |
| `user_id` | String(100) | NOT NULL, indexed | 请求取消的用户 |
| `target_run_id` | String(36) | nullable, indexed | 目标 run；未命中本地 registry 时可为空 |
| `root_run_id` | String(36) | nullable, indexed | 根 run，用于 subagent/派生 run 审计 |
| `requested_after` | DateTime | nullable, indexed | cancel epoch；避免旧取消误杀后续新 run |
| `state` | String(20) | NOT NULL, default=`"requested"` | 取消状态（见下方状态机） |
| `requested_at` | DateTime | NOT NULL | 请求时间 |
| `acked_at` | DateTime | nullable | 本进程 registry 命中确认时间 |
| `completed_at` | DateTime | nullable | 取消完成时间 |
| `updated_at` | DateTime | NOT NULL | 最后更新时间 |

**取消状态机**:

```
requested  ──→  acked  ──→  completed
    │                           ▲
    └───────────────────────────┘
          (worker 死亡时直接跳到 completed)
```

### 2.7 `channel_session_bindings` 表

外部 channel peer 到内部 session 的绑定表。当前 Web SSE 入口仍以
`session_id` 为主；本表提供 typed turn/channel 边界中的持久化 binding。
`src/api/schemas/turn.py` 供 Web adapter、Cron adapter 和未来外部 channel
adapter 复用，未来外部 channel adapter 可用本表将 peer 映射到 session。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | String(36) | PK | 绑定 UUID |
| `user_id` | String(100) | FK → `auth_users.user_id` (CASCADE), indexed | 绑定所属用户 |
| `session_id` | String(36) | FK → `sessions.id` (CASCADE), indexed | 内部会话 |
| `channel` | String(50) | NOT NULL, indexed | 渠道标识，如 `web` / `cron` / 未来外部 channel |
| `account_id` | String(100) | nullable, indexed | 渠道账号或 bot 实例 |
| `peer_kind` | String(20) | NOT NULL | `web` / `direct` / `group` / `thread` / `cron` / `webhook` |
| `peer_id` | String(255) | NOT NULL | 渠道内对端标识 |
| `external_thread_id` | String(255) | nullable | 渠道线程标识 |
| `binding_key` | String(64) | NOT NULL, indexed | 对 `(channel, account_id, peer_kind, peer_id, external_thread_id)` 做规范 JSON 后的 SHA-256 |
| `reply_route_json` | Text | nullable | `ReplyRoute` 快照 |
| `metadata_json` | Text | nullable | adapter 元数据 |
| `created_at` / `updated_at` | DateTime | NOT NULL | 创建与更新时间 |

**唯一约束**: `Unique(user_id, binding_key)`。

**当前阶段语义**: `DeliveryService` 是 no-op 边界；`ChannelProjection` 只把
`ChannelMessageReplyRoute` 上的终态 `RUN_FINISHED` 投影为 `DeliveryIntent`，
不会发送外部网络消息。Web SSE 仍由 chat route 直接返回。

### 2.8 `interrupt_resolutions` 表

Human-in-the-Loop 中断恢复的结构化事实表，兼容 `ask_user` 与工具权限审批。它记录某个 `interrupt_id` 被哪一个 resume round 接管，以及热路径当时写入/拼接的 tool result 文本，用于冷启动后还原等价上下文，并为历史 API 提供不依赖展示文本的控制轮次身份。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `interrupt_id` | String(36) | PK | Human-in-the-Loop 中断 ID；工具审批时同时是 `tool_approval_requests.id`；同一个 interrupt 只能恢复一次 |
| `session_id` | String(36) | FK → `sessions.id` (CASCADE), NOT NULL, indexed | 所属会话 |
| `parent_round_id` | String(36) | FK → `rounds.id` (CASCADE), NOT NULL, indexed | 被中断并被接管的父 Round |
| `resume_round_id` | String(36) | FK → `rounds.id` (CASCADE), NOT NULL, indexed, unique | 承载恢复后执行的新 Round |
| `tool_call_id` | String(64) | nullable | 原交互/审批对应的 tool call ID；旧数据或异常 payload 缺失时可为空 |
| `answers_json` | Text | NOT NULL | 用户回答的结构化 JSON |
| `resume_user_message` | Text | NOT NULL | resume round 的内部文本；`ask_user` 为 Q/A，工具审批可为 `Tool approval: <resolution>`，但不得据此判断轮次类型 |
| `tool_result_content` | Text | NOT NULL | 热路径实际注入/拼接的 tool result 文本，例如用户回答或审批执行占位/拒绝结果 |
| `restore_strategy` | String(40) | nullable | resume 策略：`hot_replace` / `cold_replace` / `cold_fallback_user_message` / `approval_queued` |
| `fallback_reason` | Text | nullable | 降级原因；运行时 fallback 或后续冷启动 stitching 失败时写入/更新 |
| `created_at` | DateTime | default=now, indexed | 写入时间 |

**唯一约束**: `UniqueConstraint(resume_round_id)`

**语义约束**:

- 每个 `interrupt_id` 最多对应一条 resolution，防止同一中断被并发恢复成多个 child round。
- `parent_round_id` 必须与 `rounds.parent_run_id == parent_round_id` 的 `resume_round_id` 对齐；冷启动重建时若无法对齐，该 resolution 会被忽略并记录告警。
- 同时保存 `answers_json` 与 `tool_result_content` 是有意冗余：前者是结构化事实源，后者固定当时注入 LLM 的文本格式，避免 formatter 变更导致旧会话冷恢复 prompt 漂移。
- 历史 API 通过 `InterruptResolution.resume_round_id` JOIN `ToolApprovalRequest.id == InterruptResolution.interrupt_id` 返回 `RoundData.control_kind="tool_approval"`；普通 `ask_user` 与用户真实发送的 `Tool approval: ...` 文本均不带该标记。前端只允许依据 `control_kind` 合并/隐藏审批控制轮次。

---

## 3. API 契约

所有接口均需要 Bearer Token 认证。

### 3.1 `POST /api/chat/{session_id}/message/stream`

发送用户消息并启动 Agent 执行，返回 SSE 事件流。

#### 请求

```
POST /api/chat/{session_id}/message/stream
Authorization: Bearer <token>
Content-Type: application/json

{
  "content": ContentBlock[],
  "idempotency_key": "uuid-string",    // 可选
  "preferred_skill_keys": ["pdf", "data_analysis"], // 可选，本轮优先 Skill
  "thinking_mode": "enabled",          // 可选：provider_default / enabled / disabled
  "reasoning_effort": "max"             // 可选：当前模型声明的精确强度
}
```

**ContentBlock 类型**:

| 类型 | 结构 | 说明 |
|------|------|------|
| `text` | `{type: "text", text: string}` | 纯文本消息 |
| `image_url` | `{type: "image_url", image_url: {url: string}}` | 图片（base64 data URI 或 URL） |
| `video_url` | `{type: "video_url", video_url: {url: string}}` | 视频 |
| `file` | `{type: "file", file: {path: string, name?: string, mime_type?: string, size?: number}}` | 文件附件，`path` 为会话工作区相对路径 |

**`preferred_skill_keys` 契约与作用域**:

- 值为 Skill 的稳定内部 `key` 数组；最多 50 项。每项先 trim，空项忽略，其余必须不超过 128 个 Unicode 字符；为兼容官方 Skill，允许人类可读 Unicode、空格和括号，禁止 `/`、`\`、`?`、`#`、`%` 及 Unicode General Category 为 `C*` 的控制/格式化/不可见字符。服务端按首次出现顺序去重；其他非法 key 使请求校验失败。
- 该字段表达用户对**当前逻辑执行链**的偏好，不是强制工具调用指令。Agent 仅在与用户实际请求相关且本轮暴露 `get_skill` 时优先考虑这些 Skill；不得让 Skill 偏好覆盖用户请求。
- 每次新 `message/stream` 独立解析该字段。未传或传空数组表示本次发送没有 Skill 偏好；不得从同一 session 的前序普通消息继承。
- 服务端把请求转换为版本化上下文 `bsbox.preferred_skills.v1`（`{"mode":"preferred","keys":[...]}`），并在 run 启动前根据该用户当时可见且已启用的 Skill registry 重新解析。未知、已删除或已禁用的 key 被忽略，不导致整次发送失败。
- 普通 `message/stream` direct Round 把本次解析后仍有效的 Skill 按顺序固化为 `preferred_skills: [{"key":"...","display_name":"..."}]`。这是发送当时的不可变展示快照：`key` 为稳定内部标识，`display_name` 为当时的展示名；不得在读取历史时用最新 registry 改写。空选择或全部 key 无效时保存/返回空数组。
- direct 流的 `RUN_STARTED` 必须同步携带同一份权威快照 `preferredSkills: [{key, display_name}]`（包括显式空数组），让前端立即用解析结果替换 optimistic key；resume child 的 `RUN_STARTED.preferredSkills` 固定为空数组。重连订阅重放该事件时语义不变。
- `preferred_skills` 只表示该 direct Round 的请求级“优先考虑”偏好，不证明 Agent 已加载 Skill、调用 `get_skill` 或实际使用了该 Skill。每个后续独立 direct Round 只记录自己当次提交并解析出的快照，不从前一 Round 继承或累积。
- 解析后的上下文只投影到精确匹配的**原始 user message 锚点**的 provider 请求副本，作用于 `agent_step` / `tool_followup`；不得写回 `agent.messages`、`conversation_messages`、长期 system prompt，也不得注入标题生成或对话摘要请求。child resume 的回答消息不是该投影锚点。
- 该上下文是用户通过 UI 控件提交的请求元数据，不是用户正文。provider 请求必须同时携带系统级解释策略；模型不得仅因该上下文出现就声称“用户提到/点名/要求加载了这些 Skill”，也不得在与正文无关时主动复述选择。仅在用户询问选择状态或需要解释实际 Skill 动作时才可提及。
- 若本轮因 `ask_user` 或工具权限审批中断，原始请求 key 与服务端生成的 `preferred_skills_origin_user_message_id` 会同时写入热路径 pending interrupt 和持久化 `interrupt_payload`。`resume` 时按最新 registry 和启停状态重新解析，并把同一锚点原样继承到 child resume round；因此 R1 → R2 → R3 连续中断/恢复始终投影到 R1 的原始 user message，而不是逐级改为父 Round 的回答。该继承仅延续同一次被中断的执行意图，不得扩散到之后独立发送的新消息。
- resume child Round 运行时继续继承并重新解析原请求偏好，但自身 `preferred_skills` 必须为空，不重复展示父 direct Round 已记录的标签，也不得覆盖父 Round 的发送时快照。
- `preferred_skills_origin_user_message_id` 是服务端专用元数据，不属于 `bsbox.preferred_skills.v1` 的客户端 payload。发送和 resume 请求均不能提交或覆盖它；客户端在 runtime context 中夹带同名字段必须被忽略。旧中断记录缺少该字段时，仅可用命中中断的父 Round user message 作为兼容回退，并在下一次中断时持久化服务端确认的锚点。

**本轮推理选择契约**:

- `thinking_mode` 与 `reasoning_effort` 是发送瞬间的不可变快照，只覆盖当前逻辑执行链；两者均省略时必须把当前模型目录默认值物化到快照与 direct Round，不得用 `NULL / NULL` 延迟解析。`disabled` 必须清除并拒绝同时携带的强度。
- 后端先按 session 的精确 `model_id` 校验：仅 OpenAI 兼容且显式声明非空 `supported_reasoning_efforts` 的模型可切换；`disabled` 校验目录中的 `off`，无强度的 `enabled` 校验 `on`，具体强度精确命中同一有序目录，失效或伪造值在建立 SSE 前返回 400。`reasoning_effort` 中的 `off` / `on` 必须拒绝，它们只能通过 `thinking_mode` 表达。
- 归一化后写入独立版本化上下文 `bsbox.reasoning.v1`，不得混入 Skill 上下文；OpenAI 同步、流式、工具 follow-up、retry 和 failover 在同一 run 中都从 `ContextVar` 读取同一冻结快照。failover 不按备用模型白名单过滤或降级，但只能尝试能够编码该快照的客户端：`provider_default + null` 可跨 provider；显式开关或具体强度不能交给 Anthropic 客户端；OpenAI `thinking_wire_format=none` 只能承载具体 `reasoning_effort`，不能承载纯 On/Off。协议不兼容的备用模型必须跳过，继续尝试后续模型；若没有任何备用模型兼容，保留并抛出主模型的原始失败。
- direct Round 将最终 `thinking_mode` / `reasoning_effort` 持久化用于审计和历史恢复。若执行因 `ask_user` 或工具审批中断，child resume Round 从父 Round 继承同一快照；`resume` API 不接受新的推理选择。
- `enabled` / `disabled` 由模型 DB 的 `thinking_wire_format` 编码：DashScope 类网关使用 `enable_thinking=true|false`，DeepSeek 原生协议使用 `thinking: {type: enabled|disabled}`；`disabled` 同时移除强度，非空强度作为顶层 `reasoning_effort` 发送。选择 Off 不能退化成字段缺省。

**file block 注入语义**:

- `file` block 在进入 Agent 上下文前会映射为文本提示：`[附件文件] metadata={"name":"<name>","path":"<path>"}。文件已就绪；读取时必须逐字使用 metadata.path，不要根据文件名语义补空格或改写路径。`
- `metadata` 是由 `{name, path}` 经过 JSON 编码生成；Agent 读取附件时必须以 `metadata.path` 作为唯一事实源。
- 该提示是中性提示，不强制触发 `read_file` 调用；是否读取由当前任务意图决定。

**图片约束**:

- 模型必须支持图片（`supports_image=true`），否则拒绝
- 单张图片大小上限：20MB
- 总图片大小上限：50MB
- 单次消息图片数量上限：由模型配置 `max_images` 决定
- `read_image_file` 工具从沙箱读取图片后注入的视觉上下文必须遵守同一数量与 Data URL 大小上限。

#### 响应

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

SSE 事件流格式遵循 AG-UI 协议（详见 [第 4.6 节](#46-ag-ui-事件体系)）。

#### 错误码

| HTTP 状态码 | 含义 | 场景 |
|-------------|------|------|
| 404 | 会话不存在 | `session_id` 无效或不属于当前用户 |
| 400 | 本轮推理选择无效 | 模型不支持按轮控制，或强度不在模型白名单 |
| 410 | 会话已完成 | 会话处于终态，不再接受新消息 |
| 422 | 请求校验失败 | `content` 或 `preferred_skill_keys` 超出数量/长度限制、字段类型非法 |
| 429 | 当前运行任务数已达上限 | 用户 slot 已满，或同 session 已有 active run |
| 503 | 服务不可用 | DB 锁冲突等内部错误 |

#### 流内错误事件

当 Agent 执行过程中发生错误，不会返回 HTTP 错误码，而是通过 SSE 事件流推送错误事件：

| 错误事件 | 触发条件 |
|----------|----------|
| `AGENT_INIT_FAILED` | Agent 初始化失败（沙箱连接、历史加载、技能初始化等） |
| `ROUND_IN_PROGRESS` | 幂等键冲突：相同 `idempotency_key` 的 Round 已在执行中 |
| `INTERNAL_ERROR` | 其他内部错误 |

#### 并发控制机制

```
用户发送消息
    │
    ▼
INSERT INTO user_run_locks (lock_id, user_id, session_id, slot)
    │
    ├── 成功 → 获取 slot，启动 Agent
    │              │
    │              ├── 每 15s 心跳: UPDATE updated_at
    │              │
    │              └── Agent 终态 → DELETE FROM user_run_locks
    │
    └── slot 已满 / 同 session 已在跑 → 返回 429
```

### 3.2 `POST /api/chat/{session_id}/resume`

恢复被中断的 Agent 执行（Human-in-the-Loop 应答）。

#### 请求

```
POST /api/chat/{session_id}/resume
Authorization: Bearer <token>
Content-Type: application/json

{
  "interrupt_id": "string",
  "answers": {
    "question_key": "user_answer"
  }
}
```

#### 响应

SSE 事件流，格式与 `message/stream` 相同。
Agent 初始化和中断状态校验在 SSE generator 内执行；入口只做会话、token、用户并发 slot 等前置校验，避免请求级 DB Session 跨 Agent / sandbox 初始化长时间持有连接。

#### 恢复路径

| 路径 | 条件 | 行为 |
|------|------|------|
| **热路径** (Hot Path) | Agent 仍在内存中（未被 Pool 回收） | 将用户答案作为 `tool_result` 注入 → Agent 继续执行循环 |
| **冷实时路径** (Cold Resume Path) | AgentPool 已回收 pending interrupt，但本次用户正在提交 resume | 先用 `_restore_history()` 重建旧历史，再优先将用户答案回填到恢复出的 ask_user `tool_result`；若找不到可替换占位，退化为注入新 `user` 消息并记录 `fallback_reason` |
| **冷启动历史重建** (Cold Restore Path) | resume 已完成过，之后服务重启/AgentPool 回收再加载整个 session | 读取 `interrupt_resolutions`，把 child Q/A 回填到 parent ask_user `tool_result`，并跳过对应 child resume user 消息；若回填失败，则保留 child user 消息避免语义丢失 |

恢复请求都会创建新的 Round，其 `parent_run_id` 指向被 `interrupt_id` 命中的中断 Round，并在同一事务中写入 `interrupt_resolutions`。原 Round 只在命中该 `interrupt_id` 时迁移为 `resumed`，不得批量 resolve 同 session 的其他 interrupted rounds。

若父 Round 携带 `preferred_skill_keys` 生成的版本化 runtime context，热路径 pending interrupt 与持久化 `interrupt_payload` 都必须保留该请求上下文及服务端生成的原始 user message 锚点。`resume` 请求本身不接收新的 `preferred_skill_keys` 或锚点；服务端从中断快照恢复原始 key 和锚点，并按 resume 当时可见且已启用的 Skill registry 重新解析。恢复后仍不可用的 key 静默忽略，解析结果仅作用于新建的 child resume round；该 child 若再次中断，必须继续写回同一锚点，不能改成当前父 Round 的 user message。

父 Round 已固化的 `thinking_mode` / `reasoning_effort` 同样由服务端继承到 child resume Round，确保审批或回答之后的 provider follow-up 不会因为用户后来调整输入框选择而改变推理强度。

#### 错误码

| HTTP 状态码 | 含义 | 场景 |
|-------------|------|------|
| 404 | 会话不存在 | `session_id` 无效 |
| 429 | 当前运行任务数已达上限 | 用户 slot 已满，或同 session 已有 active run |
| 503 | 服务不可用 | 内部错误 |

返回 SSE 后的恢复期错误以 AG-UI `RUN_ERROR` 结束流：

| RUN_ERROR code | 含义 | 场景 |
|----------------|------|------|
| `NO_PENDING_INTERRUPT` | 没有待处理的中断 | 会话没有匹配的 pending interrupt，或中断 ID 已过期/已恢复 |
| `AGENT_INIT_FAILED` | Agent 初始化失败 | AgentPool 获取或初始化失败 |

### 3.3 `GET /api/chat/{session_id}/round/{round_id}/subscribe`

订阅指定 Round 的 AG-UI 事件流。用于断线重连或查看历史 Round 的事件回放。

#### 请求

```
GET /api/chat/{session_id}/round/{round_id}/subscribe?last_sequence=0
Authorization: Bearer <token>
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `last_sequence` | int | 0 | 客户端已接收的最大事件序号，服务端从此序号之后开始推送 |

#### 响应

SSE 事件流。

#### 行为分支

| Round 状态 | 行为 |
|------------|------|
| 已在终态 (`SUBSCRIBE_TERMINAL`) | 回放 `sequence > last_sequence` 的所有事件 → 关闭连接 |
| 仍在运行 (`running`) | 回放历史事件 → 切换到实时模式，从 subscriber queue 推送新事件 |

- 心跳间隔：15 秒
- 订阅超时：5 分钟（可通过 `SSE_SUBSCRIBE_TIMEOUT=300` 配置）
- 超时后推送 `RUN_ERROR(TIMEOUT)` 事件

#### 错误码

| HTTP 状态码 | 含义 |
|-------------|------|
| 404 | Round 不存在 |

流内错误：`TIMEOUT`（订阅超时）

### 3.4 `POST /api/chat/{session_id}/abort`

请求取消当前正在运行的 Agent 执行。

#### 响应

```json
// 常规即时取消（单 worker registry / 接口即时收敛）
{
    "status": "cancelled",
    "request_id": "uuid",
    "reason": "force_aborted"
}

// Worker 已死亡，直接清理
{
  "status": "cancelled",
  "request_id": "uuid",
  "reason": "worker_dead"
}

// init-window（有会话锁但尚未创建 round）即时解锁
{
    "status": "cancelled",
    "request_id": "uuid",
    "reason": "force_unlocked"
}
```

| `status` 值 | 含义 |
|--------------|------|
| `cancelled` + `reason: "force_aborted"` | 已将 running round 直接收敛为 cancelled，并立即释放会话锁 |
| `cancelled` + `reason: "worker_dead"` | 锁已超过 `SSE_SUBSCRIBE_TIMEOUT` 未刷新，判定 Worker 死亡，直接强制清理锁和 Round 状态 |
| `cancelled` + `reason: "force_unlocked"` | init-window（无 running round）也立即释放会话锁，允许用户立刻重发 |

> 约束：`cancelled(*)` 仅在目标会话锁确认已释放时返回；若释放失败（例如数据库锁冲突），接口返回 `HTTP 503`，不会返回“假成功”。

#### 错误码

| HTTP 状态码 | 含义 |
|-------------|------|
| 404 | 会话不存在 |
| 409 | 没有正在进行的执行 |
| 503 | 服务不可用 |

### 3.5 `GET /api/chat/{session_id}/abort/status`

查询当前取消请求的状态。

#### 响应

```json
{
  "session_id": "uuid",
  "state": "none" | "requested" | "acked" | "completed",
  "request_id": "uuid",
  "requested_at": "datetime",
  "acked_at": "datetime | null",
  "completed_at": "datetime | null",
  "running": true,
  "running_round_id": "uuid"
}
```

| `state` 值 | 含义 |
|-------------|------|
| `none` | 没有活跃的取消请求 |
| `requested` | 取消已请求，Worker 尚未确认 |
| `acked` | Worker 已确认取消，正在清理中 |
| `completed` | 取消已完成 |

#### 错误码

| HTTP 状态码 | 含义 |
|-------------|------|
| 404 | 会话不存在 |

### 3.6 `GET /api/sessions/{session_id}/history/v2`

按 Round/Step 结构返回会话历史。`HistoryResponseV2.rounds[]` 除既有 Round 字段外，必须包含：

```json
{
  "preferred_skills": [
    {"key": "pdf", "display_name": "PDF 处理"}
  ]
}
```

`preferred_skills` 类型为 `Array<{key: string, display_name: string}>`，并遵循以下投影规则：

- 普通 direct Round 返回该次 `message/stream` 在运行开始时解析并持久化的有效 Skill 展示快照；没有有效偏好或读取旧版 `null` 数据时返回 `[]`。
- resume child Round 固定返回 `[]`。其运行时虽然继承父逻辑执行链的偏好，但历史 UI 只在最初的 direct Round 用户消息旁展示一次，不能在每个 child 重复展示。
- 该数组是不可变的历史展示数据，不是 Skill 使用审计；不能据此声称 Skill 已加载、已调用或对结果有贡献。
- 各个独立 direct Round 的数组彼此隔离，不继承、不合并，也不根据当前启停状态、改名或删除情况重算。


---

## 4. 行为语义与不变量

### 4.1 Agent 执行循环

#### Turn 编排边界

Web send/resume/abort 入口先由 `WebChatAdapter` / `WebResumeAdapter` /
`WebCancelAdapter` 归一化为 typed turn contract，再交给 `TurnOrchestrator`
启动 prepared Agent run、注册 cancel token、维护 active runner、刷新
`UserRunLock` 心跳，并在终态释放锁和完成取消审计。`CronChannelAdapter`
提供 no-reply turn 规范化；未来外部 channel 通过 `ReplyRoute`、
`ChannelProjection` 与 no-op `DeliveryService` 接入，不改变当前 Web SSE
协议。

#### 基本流程

```
用户消息到达
    │
    ▼
获取 UserRunLock slot
    │
    ▼
初始化 Agent (Lazy Init)
    │  ├── 连接/恢复沙箱
    │  ├── 加载对话历史
    │  └── 初始化技能 (Skills)
    │
    ▼
刷新 Agent runtime messages
    │
    ▼
创建 Round (status=running)
    │
    ▼
┌─── Agent 主循环 (step 1..max_steps) ──────────────┐
│                                                     │
│  ① 取消检查 ──── 检测当前 run cancel token        │
│      │                                              │
│      ▼                                              │
│  ② 按最终 dispatch 形态压缩/预检后调用 LLM（流式） │
│      │  └── Producer-Consumer: LLM stream → Queue   │
│      ▼                                              │
│  ③ 取消检查                                         │
│      │                                              │
│      ▼                                              │
│  ④ 解析 LLM 响应                                   │
│      │                                              │
│      ├── 纯文本 → 发射 TEXT_MESSAGE 事件 → 结束循环 │
│      │                                              │
│      └── 工具调用 → 逐个执行:                       │
│           ⑤ 取消检查                                │
│           ⑥ 执行工具                                │
│           ⑦ 发射 TOOL_CALL/RESULT 事件              │
│           └── 回到循环顶部                          │
│                                                     │
└─────────────────────────────────────────────────────┘
    │
    ▼
完成 Round (status → 终态)
    │
    ▼
释放 UserRunLock
```

#### 关键参数

| 参数 | 默认值 | 可配置 | 说明 |
|------|--------|--------|------|
| `AGENT_MAX_STEPS` | 100 | 是 | 单次 Round 最大步数 |
| 用户并发上限 | 1 | 是 (`AGENT_USER_CONCURRENCY_LIMIT`) | 同一用户可同时运行的不同 session 数 |
| 心跳间隔 | 15s | 是 (`SSE_HEARTBEAT_INTERVAL`) | SSE 心跳与锁刷新间隔 |
| 工具超时 | 300s | 是 (`tool_timeout`) | 单个工具执行超时 |
| 流式块超时 | 100s | — | LLM 流式响应相邻 chunk 的最大间隔 |

#### Runtime 工具循环防护

每个 Round 在消息历史之外维护 run-local execution ledger；该 ledger 不参与摘要、provider 投影或 checkpoint，因此压缩不能让防护状态“失忆”。调用身份由稳定 tool ref 与规范化参数摘要组成，不使用每次都会变化的 `tool_call_id`；文件路径按 POSIX 分隔符归一化但保留大小写，审计只保存摘要，不保存敏感原始参数。

- 相同参数、相同错误连续返回 2 次后，第 3 次执行前阻止；中间成功执行不同调用会解除旧恢复态，不同错误视为策略发生变化。
- 只读工具允许“初读 + 复核”，第 3 次仍得到相同结果时阻止；相同成功的 mutating 工具下一次不得重复执行，`outcome_uncertain` 的副作用调用也不得自动重试。
- 完整观察到 `A → B → A → B`，且 A/B 均为相同只读调用、两次结果各自未变化后，下一次再次调用 A 或 B 时识别为无进展循环；不得在尚未观察第二次 B 的结果前预测性阻止。成功 mutation 会清空旧的读/搜索观察。声明为 polling 的工具使用独立有界阈值，但相同 uncertain 结果最多实际尝试 2 次。
- `bash` 命令文本不凭首个单词猜测读写属性；MCP annotation 只能把默认策略收紧，远端自称 `readOnlyHint=true` 不得放宽重试权限。带 `mode` 的记忆工具按具体调用区分 read 与 write/append。
- 文件变更工具（`write_file` / `edit_file`）额外按规范化路径记账：某路径上出现过 `outcome_uncertain` 写入后，该路径上的任何后续文件变更都被阻止，即使参数不同。解除条件唯一——对同一规范化路径调用 `read_file` 并得到确定结果（成功，或明确的 `File not found:`）；用 `bash cat` 等其他方式旁路校验不解除阻断。工具超时产生的 uncertain 写入必须在 synthetic tool result 中明确告知模型该路径需要 `read_file` 校验。
- 确定存在的缺失文件读取只解除它自己验证的那个路径的 uncertain 记账与对应恢复态，不得清空其他无关 pattern 的恢复态；只有成功调用才按既有规则收敛全局恢复态。
- 首次命中时不执行候选工具，但仍写入配对的 synthetic tool result，明确要求换策略，并给模型一次恢复机会；若下一步仍调用同一被拒绝 pattern，则为同一 assistant 批次的其余调用补齐 skipped result，发出 `RUN_ERROR(tool_loop_detected)` 并将 Round 置为失败；真正不同且成功的策略会解除该 pattern 的恢复态。
- Round 收尾不得额外调用模型提取或写入长期记忆；只允许同步本轮已由显式工具/文件操作产生的 dirty 配置文件，并按 [memory-spec.md](./memory-spec.md) 维护对话轮检索索引。

#### Producer-Consumer 模式

LLM 流式响应采用 `asyncio.Queue` 解耦：

- **Producer**: 消费 LLM 流式 chunk，写入 Queue
- **Consumer** (主循环): 从 Queue 读取 chunk，生成 AG-UI 事件，推送 SSE

这一设计使 LLM 流式输出不会阻塞事件处理，且 Producer 在 SSE 断开时仍可继续运行。

#### 取消检查点

Agent 在每个可能长时间等待的边界都读取或等待当前 run 的 `cancel_token`：

1. **Step 开始前**：每步循环入口。
2. **上下文压缩期间**：非流式 compaction provider 请求与 cancel token 竞争；取消时终止请求，且不得发布 replacement、checkpoint 或压缩审计记录。
3. **普通 LLM 流式请求期间**：Producer-Consumer 等待同时监听 cancel token，取消时终止 provider task。
4. **LLM 完成后、工具执行前**：LLM 响应解析完成时。
5. **每个工具执行前**：多工具调用时，每个工具执行前单独检查。

`abort` 命中本进程 registry 时立即置位 token，并写入 `run_cancel_requests` 审计行。DB 行不参与第一版取消投递。由 fallback 降窗触发的 compaction 使用同一个 token，不能绕过取消。

#### Max Steps 处理

- 倒数第 2 步（step == max_steps - 1）时，注入一条**合成提醒消息**（`is_synthetic=True`），告知 Agent 即将达到步数上限
- max_steps 耗尽时，发射 `RUN_FINISHED` 事件，`outcome="interrupt"`，附带 `result.reason="max_steps_reached"` 和可持久化的 `finalResponse`；后端 Round 状态落为 `max_steps_reached`

#### AgentPool 缓存与 runtime messages 一致性

`AgentPoolService` 只缓存可复用运行资源，包括沙箱连接、工具集合、LLM client 与 AgentService 实例；它不把 `agent.messages` 视为跨轮次、跨进程重启后的权威上下文。

每次 `send` / `resume` 真正启动 Agent run 前，`AgentService` 必须从 DB 权威历史重建本轮 runtime messages：

1. 保留当前 Agent 的 system prompt。
2. `checkpoint_v1` 优先读取最新 v4 replacement，并从精确 message/event 游标回放同轮 suffix 与后续 Round；无有效 v4 时从 `rounds` + `conversation_messages` + `agui_events` + `interrupt_resolutions` 重建完整权威历史。只有 `legacy_120` 回滚策略按消息条数裁剪。
3. 用重建结果替换本进程内旧 `agent.messages`，不得追加到旧热缓存后面。
4. 再注入本轮用户输入或 resume 答案后进入 LLM 调用。
5. LLM 调用前由请求组装层临时构造 provider-bound messages，并在副本的 system 消息前加入 request-only runtime context（当前时间、时区、workspace、平台执行语义等）；不得回写 `agent.messages` 或 `conversation_messages`。

因此，即使重启后命中本地 AgentPool 热缓存，LLM request 也必须基于 DB 中最新 conversation history 构造，并叠加本次请求生成的实时上下文。`llm_call_records.request_messages` 应能审计到刷新后的输入快照：上一轮已落库的用户纠错/确认信息不得因为 stale hot cache 缺失，当前时间也不得因为长生命周期 Agent 缓存而沿用旧值。

AgentPool 缓存失效不得中断仍在运行的 AgentService：配置更新、sandbox 代际切换、renew 失败或 TTL cleanup 遇到 running Agent 时，只能 detach 或标记懒失效；idle 后的下一次 `get_or_create` 再重建。这样可以保证新 run 不复用旧 system prompt，同时不误杀旧 run 正在执行的后台 bash 命令。

### 4.2 幂等性保证

#### 机制

```
前端生成 UUID → idempotency_key
    │
    ▼
INSERT INTO rounds (..., idempotency_key)
    │
    ├── 成功 → 正常执行
    │
    └── IntegrityError (唯一约束冲突)
         │
         ▼
    查询已有 Round
         │
         ├── 已在终态 → 返回已有 Round（前端可 subscribe 重放）
         │
         └── 仍在运行 → 重定向到 subscribe 模式
              └── 推送 ROUND_IN_PROGRESS 错误事件
```

#### 要求

- `idempotency_key` 由前端生成（UUID v4）
- 唯一约束作用域：`(session_id, idempotency_key)`
- 前端在网络重试时必须携带相同的 `idempotency_key`

### 4.3 并发控制

#### UserRunLock 生命周期

```
                   获取锁
                     │
        ┌────────────┼────────────┐
        │            │            │
        ▼            ▼            ▼
   正常完成      中断暂停      取消/失败
   DELETE lock   DELETE lock   DELETE lock
```

#### Worker 死亡检测

- 每 15s 心跳刷新 `user_run_locks.updated_at`（由 `SSE_HEARTBEAT_INTERVAL` 控制）
- 超过 `SSE_SUBSCRIBE_TIMEOUT`（默认 300s）未刷新 → 视为 stale lock；下一次获取用户 slot 时回收该锁并清理对应 session 的孤儿 running round
- `abort` 统一采用“接口即时收敛”策略：
    1. 写入 append-only 取消审计行（用于本进程 registry 命中状态与诊断）
    2. 若存在 running round，直接标记 `cancelled` 并补发 `RUN_FINISHED(outcome=interrupt)`
    3. 立即释放 `user_run_locks`（不等待执行 worker 自行结束）
    4. 完成取消请求状态（`completed`）
- `abort` 若命中本进程 active runner，仅向本地 cancel token 投递协作式取消信号并记录审计状态；接口不等待 runner 自行结束，也不执行 task-level `runner.cancel()`
- `abort` 检测到 Worker 死亡时返回 `reason=worker_dead`，其余即时收敛路径返回 `force_aborted/force_unlocked`
- 执行侧引入 abort-epoch 守卫：若 run 启动后检测到较新取消请求（`requested_at` 晚于 run 启动点），旧请求会被短路，避免 init-window 穿透创建新 round

> 说明：即时释放锁后，旧 worker 可能仍在协作式退出过程。系统通过 AgentPool detach、终态后丢弃迟到 AG-UI 事件、禁止终态 round 被覆写，避免 UI/回放/后续新 run 被旧 run 污染。

#### Per-User 并发 slot

系统保证 **同一用户同一时刻最多持有 `AGENT_USER_CONCURRENCY_LIMIT` 个活动锁**。默认值为 `1`，保持严格串行；配置为 `3` 时，同一用户最多可让 3 个不同 session 同时运行。

同一 `chat_session_id` 始终只能有一个 active run；即使用户并发上限大于 1，重复发送同一 session 仍会被 `Unique(user_id, session_id)` 拦截。

在“abort 即时释放锁”语义下，存在短时窗口：旧 run 正在退出而新 run 已启动（无锁重叠执行）。为避免状态污染，后端执行以下约束：

1. round 一旦进入终态（尤其 `cancelled`），后续迟到 AG-UI 事件一律丢弃
2. `complete_round` 不允许覆写终态 round
3. 订阅端以终态事件为准，不再回跳 `running`

- worker 心跳过期时仅回收对应 session 的孤儿 round，不影响同用户其他仍健康的并发 session。

#### DB 连接生命周期约束（强约束）

后端为 FastAPI + SQLAlchemy 同步 ORM + asyncio 混合模型，DB 连接必须**短持有**，否则会在单 worker 部署下耗尽连接池并阻塞整个 event loop。

规则：

1. **后台长轮询协程不得跨 `await asyncio.sleep` 持有 DB Session**。典型场景：
   - `TurnOrchestrator` 的 lock heartbeat guard：每轮单独 `with SessionLocal() as hb_db`，`sleep` 前必须释放。
   - `subscribe_to_round.heartbeat_and_poll`：每次增量回放查询单独 `with SessionLocal() as replay_db`，查完立即释放。
2. **PostgreSQL 使用 `QueuePool + pool_pre_ping`**：事实库仅支持 PostgreSQL，连接池容量必须覆盖请求级校验、SSE 后台 producer、TurnOrchestrator lock heartbeat 与事件重放的短生命周期 Session 峰值，避免同步 ORM 获取连接时阻塞 event loop。
3. **请求级 `db: Depends(get_db)` 在 SSE 流路径上只能用于入口校验**；进入 event_generator / producer 后，必须使用独立短生命周期 Session 做持久化。`subscribe_to_round` 完成初始 replay / 终态检查后，若要进入长时间队列等待，必须先结束请求级只读事务。
4. **AgentPool 热缓存不得持有请求级 DB Session**：缓存的 `AgentService` 使用独立的 `SessionLocal` factory / owned session，命中缓存时不得把当前请求的 `db: Depends(get_db)` rebind 到共享 Agent 上。Agent 初始化期间需要更新 `user_sandboxes` 或同步 memory 时，也必须使用独立短生命周期 Session；入口请求级 `db` 只能传递已预读出的标量值（如 `model_id` / `sandbox_id` / `round_count`），不得跨 sandbox / Agent 初始化 await 持有。
5. **AG-UI 事件持久化不得跨 Agent/LLM/tool 的 `await` 间隙保留未提交事务**；非 delta 事件写入后必须提交，delta 事件完成终态读取后也必须结束只读事务，run 收尾的只读状态/摘要查询也必须结束事务，避免 PostgreSQL 远端连接在 idle-in-transaction 状态被断开。`commit()` 后的 `refresh()` 会再次发起只读 `SELECT`，返回 ORM 对象前必须分离对象并结束该只读事务。
6. **请求清理阶段的连接断开只记录日志，不改变已完成响应**：若 `get_db` 在 `db.close()` 隐式 rollback 时发现 PostgreSQL 连接已被远端关闭，记录 warning 并丢弃该连接；业务查询/提交阶段的 DB 异常仍必须正常抛出。

不遵守上述规则会表现为：多并发会话切换/拉取时，`get_db` 从连接池获取连接超时，抛 `sqlalchemy.exc.TimeoutError: QueuePool limit ... connection timed out`。

### 4.4 SSE 事件持久化策略

不同类型的事件采用不同的持久化策略，以平衡实时性和写入性能：

#### 流式 Delta 事件 — 内存缓冲

以下事件在内存中缓冲，不逐条写入 DB：

- `TEXT_MESSAGE_CONTENT` — 文本 delta
- `THINKING_TEXT_MESSAGE_CONTENT` — 思考过程 delta
- `TOOL_CALL_ARGS` — 工具参数 delta

当对应的 `*_END` 事件到达时，触发**聚合写入**：将所有缓冲的 delta 合并为一条完整内容，与 END 事件一起写入 DB。

> **设计决策**: 流式 delta 可能有数十到数百条，逐条持久化会产生大量小写入。聚合后只写 2 条记录（合并的 CONTENT + END），大幅减少 DB 压力。

#### 关键生命周期事件 — 立即提交

以下事件在生成时立即写入 DB 并提交事务：

- `RUN_STARTED` / `RUN_FINISHED`
- `RUN_ERROR`
- `STEP_STARTED` / `STEP_FINISHED`

#### 其他事件 — 逐事件提交

除上述两类以外的非 delta 事件，在写入后立即提交。这样可以避免 SSE/Agent 的异步等待间隙持有 PostgreSQL 事务；流式高频 delta 仍通过内存聚合减少写入量。

### 4.5 上下文压缩

压缩行为对齐本地 Codex `compact.rs`。

#### Token 限制参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `token_limit` | `(context_window - max_tokens) * 80%` | 先为最大输出预留窗口，再对可用输入预算取 80%；可由模型配置调低，不能调高 |
| `context_window` | 128,000 | 模型上下文窗口大小 |
| `max_output_tokens` | 16,384 | 模型最大输出长度 |

#### 压缩流程

```
读取上一次真实 provider usage（冷启动时估算历史）
    │
    ├── ≤ token_limit → 不压缩
    │
    └── > token_limit
         │
         ▼
    同模型本地压缩：规范化完整历史 + 固定 checkpoint prompt
         │
         ├── 成功 → replacement：最新真实 users（20k）+ user-role summary
         │
         └── context overflow → 每次只删除一个最旧历史项、重新规范化并重试
```

pre-turn 压缩排除正在进入会话的当前 user，发布 replacement 后再把该消息原样接回。replacement 中保留的历史真实 user 严格按 Codex 只抽取 text block，忽略 image/audio/video 等媒体块，禁止把 data URL 或 base64 序列化为 checkpoint 文本；媒体语义由看过完整历史的 handoff summary 承接。mid-turn 只在仍要继续调用模型时触发，压缩请求能看到完整当前工具批次，但 replacement 不保护该批次，只依赖 handoff summary。工具输出在首次写入历史时按默认 10,000 bytes（1.2 倍序列化余量）做 UTF-8 中间截断。

压缩 provider 请求必须响应当前 run 的取消信号。取消胜出时中止正在进行的请求并直接结束 run；只有 provider 响应完成且取消仍未发生时，才允许持久化 checkpoint 并发布 replacement。

切换到更小 fallback 前，如原请求达到目标模型的 `(context_window - max_tokens) * 80%` 阈值，先用 primary 压缩；符合 Codex fallback 条件的失败再用目标模型压缩，context overflow 同样逐个删除最旧项。成功后立即写 checkpoint 并重建普通 fallback 请求；不再发生“少几个 token 就本地拒绝且未调用 fallback”。

#### 历史恢复投影边界（_restore_history）

默认 `checkpoint_v1` 的恢复顺序固定为：`checkpoint replacement + source Round 游标后的同轮 suffix + 后续 Round`。消息数不是预算单位，`agent_max_history_messages=120` 只服务 `legacy_120` 回滚开关。

对 `status=completed` 且事件无法重建 assistant 文本的 Round，仍按 `rounds.final_response`、普通 assistant conversation message 的顺序兜底。resume stitching 保持不变；压缩 replacement 不强保首轮 user。

**Resume resolution stitching**:

- `_rebuild_messages_from_events()` 会预载本 session 的 `interrupt_resolutions`，构建 `resolution_by_parent_round_id` 与 `resolution_by_resume_round_id` 两个索引。
- 遇到 `status=resumed` 的 parent round 时，按 `tool_call_id` 将 ask_user 占位 `tool_result` 替换为 `tool_result_content`。
- 替换成功后，对应 child resume round 的第一条匹配 `resume_user_message` 的真实 user 消息会被跳过；child round 的 assistant/tool 输出继续保留。
- 替换失败时，不跳过 child user 消息，并更新该 resolution 的 `fallback_reason`，避免冷恢复丢失用户回答。
- 连续 ask_user 链允许同一个 round 同时是上一个 interrupt 的 child、下一个 interrupt 的 parent；parent/child 两个索引互不冲突。

### 4.6 AG-UI 事件体系

系统生成 22 类标准 AG-UI 事件，分为 7 个分类：

#### 事件分类

| 分类 | 事件类型 | 说明 |
|------|----------|------|
| **Lifecycle** | `RUN_STARTED` | Agent 执行开始 |
| | `RUN_FINISHED` | Agent 执行完成（含 outcome） |
| | `RUN_ERROR` | Agent 执行出错 |
| | `STEP_STARTED` | 单步执行开始 |
| | `STEP_FINISHED` | 单步执行完成 |
| **Text** | `TEXT_MESSAGE_START` | 文本消息开始 |
| | `TEXT_MESSAGE_CONTENT` | 文本消息 delta（流式） |
| | `TEXT_MESSAGE_END` | 文本消息结束 |
| **Thinking** | `THINKING_TEXT_MESSAGE_START` | 思考过程开始 |
| | `THINKING_TEXT_MESSAGE_CONTENT` | 思考过程 delta（流式） |
| | `THINKING_TEXT_MESSAGE_END` | 思考过程结束 |
| **Tool** | `TOOL_CALL_START` | 工具调用开始 |
| | `TOOL_CALL_ARGS` | 工具参数 delta（流式） |
| | `TOOL_CALL_END` | 工具调用结束 |
| | `TOOL_CALL_RESULT` | 工具执行结果 |
| **State** | `STATE_SNAPSHOT` | 完整状态快照 |
| | `STATE_DELTA` | 状态增量更新（JSON Patch, RFC 6902） |
| | `MESSAGES_SNAPSHOT` | 消息列表快照 |
| **Custom** | `heartbeat` | 心跳保活 |
| | `title_updated` | 会话标题更新 |
| | 其他自定义事件 | 按需扩展 |

#### ID 生成规则

| ID 类型 | 格式 | 示例 |
|---------|------|------|
| `threadId` | Session UUID | `a1b2c3d4-...` |
| `runId` | Round UUID | `e5f6g7h8-...` |
| `messageId` | `msg_{runId}_{step}` | `msg_e5f6g7h8_3` |
| `toolCallId` | `tc_{runId}_{step}` | `tc_e5f6g7h8_3` |

### 4.7 Ask-User 中断与恢复

Human-in-the-Loop 机制允许 Agent 在执行过程中向用户提问并等待答复。

#### 中断流程

```
Agent 调用 ask_user 工具
    │
    ▼
保存中断状态到 Round:
  - interrupt_payload = {id, reason, payload, runtime_context?, preferred_skills_origin_user_message_id?}
  - status = "interrupted"
    │
    ▼
填充占位 tool_result（标记为待替换）
    │
    ▼
发射 TOOL_CALL_RESULT (ask_user 结果，含占位内容)
    │
    ▼
发射 RUN_FINISHED (outcome="interrupt")
    │
    ▼
释放 UserRunLock
```

#### 恢复流程

```
用户提交答案 → POST /resume
    │
    ▼
获取 asyncio.Lock (_resume_lock) ── 防止并发 resume
    │
    ▼
按 interrupt_id 定位被中断的 Round
    │
    ▼
创建新 Round (parent_run_id = 被中断的 Round)
    │
    ▼
将该 interrupted Round 状态设为 "resumed"
    │
    ▼
判断恢复路径:
    │
    ├── 热路径: Agent 仍在内存
    │     │
    │     ▼
    │   替换占位 tool_result 为用户答案
    │     │
    │     ▼
    │   Agent 继续执行循环
    │
        └── 冷实时路径: Agent pending interrupt 已丢失
          │
          ▼
                优先替换 _restore_history 重建出的 ask_user tool_result
                    │
                    ├── 替换成功: 继续执行循环
                    │
                    └── 替换失败: 将用户答案作为新 user 消息注入，并记录 fallback_reason
          │
          ▼
        启动新 Agent 执行
```

**Resolution 写入时机**:

- `POST /resume` 创建新 Round 时，同时写入 `interrupt_resolutions`。
- 定位被中断 Round 时，优先使用持久化 interrupt；热路径的 Agent pending interrupt 快照必须携带触发它的 `round_id`，用于覆盖 `RUN_FINISHED` 已发出但 DB 状态尚未提交为 interrupted 的窗口。
- `answers_json` 保存用户回答结构；`tool_result_content` 保存热路径实际注入 LLM 的文本。
- `restore_strategy` 记录本次 resume 采用 `hot_replace`、`cold_replace` 或 `cold_fallback_user_message`。
- `fallback_reason` 在运行时 fallback 时写入；若后续冷启动 stitching 发现已创建的 resolution 无法回填 parent tool result，也允许更新该字段。

**连续 ask_user 链规则**:

- parent round 用 `resolution_by_parent_round_id[parent.id]` 回填 tool result。
- child round 若 `resolution_by_resume_round_id[child.id]` 命中，仅跳过与 `resume_user_message` 完全匹配的 child resume Q/A user 消息。
- child round 的 assistant/tool 输出继续保留。
- 同一个 round 可以同时是上一段恢复链的 child、下一段恢复链的 parent，两个身份不冲突。

**并发保护**: `_resume_lock` 使用 `asyncio.Lock`，确保同一会话的 resume 请求不会并发执行。

**冷恢复占位归一化**:

- 当原中断轮次状态已为 `resumed` 且没有 resolution 可回填时，历史重建阶段会将旧的 ask_user 占位 `tool_result`（`[Awaiting user response]`）归一化为 `[Interrupt resolved in subsequent round]`，避免刷新后继续暴露过期等待状态。

### 4.8 LLM 调用快照持久化

每个 step 的 LLM 调用都会额外持久化到 `llm_call_records`：

1. 调用前：记录 provider 转换后的最终请求快照（与实际发包口径一致，包含 request-only runtime context）以及可用工具名称列表。
2. 调用后成功：记录 `content` / `thinking` / `tool_calls` / `finish_reason` / `usage`。
3. 调用后失败：记录 `response_error`。
4. 调用时：额外记录同一步的上下文压缩观测数据（`compaction_*`），用于排查长对话中的压缩收益与恢复稳定性问题。

`request_messages` 是审计用的实际发包记录，不是历史恢复源；其中的 request-only runtime context 只证明当次请求的执行环境，不得被 `_restore_history`、上下文压缩摘要或 conversation history 回放重新注入为长期消息。

约束与边界：

- 该表仅用于后端审计与排查，不参与 SSE 回放协议。
- 会话删除时与 Round/Events 一并清理。
- `step_index` 在同一 `round_id` 内唯一。

### 4.9 LLM Failover

#### Fallback 链

`models.yaml` 中可为模型配置 fallback 列表：

```yaml
models:
  - name: primary-model
    fallback:
      - fallback-model-1
      - fallback-model-2
```

#### Failover 流程

```
调用 Primary 模型
    │
    ├── 成功 → 返回
    │
    └── 失败 → 重试（指数退避）
         │
         ├── 重试成功 → 返回
         │
         └── 重试耗尽
              │
              ▼
         Callback: 重置流式状态，调整上下文窗口
              │
              ▼
         调用 Fallback 模型 1
              │
              ├── 成功 → 返回
              │
              └── 失败 → 调用 Fallback 模型 2
                   │
                   ├── 成功 → 返回
                   │
                   └── 所有 Fallback 失败
                        │
                        ▼
                   抛出 RetryExhaustedError
```

#### 重试参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `max_retries` | 3 | 每个模型最大重试次数 |
| `initial_delay` | 0.5s | 初始重试延迟 |
| `max_delay` | 30s | 最大重试延迟 |
| `max_increment` | 1.0s | 每次退避最大增量 |

#### Failover 策略

- **One-shot Failover**: 当 Fallback 模型成功后，下一次 LLM 调用仍优先尝试 Primary 模型
- 不会永久切换到 Fallback 模型

### 4.9 Lazy Agent Init

Agent 初始化采用延迟模式，确保 SSE 连接不会因初始化耗时而超时。

```
SSE 连接建立
    │
    ▼
立即开始发送心跳（每 15s）
    │
    ▼
异步并行初始化:
    ├── 连接/恢复沙箱
    ├── 加载对话历史
    └── 初始化技能 (Skills)
    │
    ├── 全部成功 → 发射 RUN_STARTED → 进入 Agent 循环
    │
    └── 任一失败 → 发射 AGENT_INIT_FAILED 事件 → 关闭连接
```

> **设计决策**: SSE 连接先建立再初始化，避免浏览器/网关因等待过久而断开连接。心跳在初始化期间就已开始发送。

### 4.10 Round 状态不变量

**核心不变量**: 每个 Round 最终都必须达到终态。

#### 实现保证

1. **Finally Block**: Agent 执行的 finally 块确保无论正常完成、异常、断开还是取消，Round 都会被设置为终态
2. **防止跨 Worker 覆写**: `complete_round` 在更新前检查 Round 当前状态——若已处于终态（`completed`/`failed`/`cancelled`/`resumed`），则跳过状态覆写。这防止了以下场景：
   - Worker A 执行 Round，被 abort 设为 `cancelled`
   - Worker A 的 finally 块随后执行，尝试将 Round 设为 `completed`
   - 检查发现 Round 已在终态，跳过更新
3. **Resume 竞争窗口处理**: 若 resume 已先将 parent round 标记为 `resumed`，旧 run 的迟到 `complete_round(status="interrupted")` 只允许补写展示元数据（如 `final_response`、`step_count`），不得把状态改回 `interrupted`。

---

## 5. 失败模式与错误处理

| 失败场景 | 检测方式 | 处理策略 | 用户感知 |
|----------|----------|----------|----------|
| **LLM 调用失败** | 异常捕获 + 重试耗尽 (`RetryExhaustedError`) | 发射 `RUN_ERROR` 事件，Round 标记 `failed` | SSE 收到错误事件，前端展示错误提示 |
| **Agent 初始化失败** | 初始化异常捕获 | 发射 `AGENT_INIT_FAILED` 事件，Round 标记 `failed` | 同上 |
| **工具执行超时** | `tool_timeout=300s` | 工具返回超时错误信息并标记 `outcome_uncertain=true`；Agent 继续执行，权限审计记 `unknown` 而非 `failed` | Agent 告知用户超时；远端副作用可能已经发生，不应把它当作确定失败 |
| **LLM 返回空响应** | 检测空内容 | 给予一次 nudge 机会（注入提示重新生成）；连续空响应 → `RUN_ERROR` | 首次空响应用户无感知；连续空响应收到错误 |
| **输出截断** | `finish_reason=length` | 自动重试一次（调整 prompt 或 context） | 用户无感知（自动恢复） |
| **SSE 断开** | 连接关闭检测 | Producer 继续运行在 `_active_runners` 中，等待客户端重连通过 subscribe 恢复 | 前端检测断开，自动重连并通过 subscribe 恢复事件流 |
| **Subscribe 超时** | 5 分钟定时器 | 发射 `RUN_ERROR(TIMEOUT)` 事件，关闭连接 | 前端收到超时事件，提示用户 |
| **resume 冷实时替换失败** | 未找到 ask_user `tool_call_id` 或 tool result 占位 | 写入 `interrupt_resolutions.fallback_reason`，将用户答案作为新 user 消息注入 | Agent 仍继续执行，冷恢复可通过 child user 消息保留语义 |
| **resume 冷启动 stitching 失败** | resolution 与 parent/child 不对齐，或 parent tool result 无法替换 | 不跳过 child resume user 消息，并更新/记录 `fallback_reason` | 刷新后不会丢失用户回答，但上下文形态会退化为 user Q/A |
| **用户主动 abort（任意 worker）** | `POST /abort` 被调用且会话处于 running / init-window | 接口直接收敛 round 并尝试释放锁；释放成功返回 `cancelled(force_aborted/force_unlocked)`；本地命中 runner 时投递协作式 cancel token | 释放成功后用户可立即重发 |
| **abort 收敛后释放锁失败** | `POST /abort` 执行到锁释放阶段但 DB 冲突/异常 | 返回 `HTTP 503`，不返回 `cancelled` 假成功 | 前端保持运行态或提示重试，避免误判“已可重发” |
| **abort 后旧 run 迟到输出** | round 已终态但仍收到后续 AG-UI 事件 | 丢弃迟到事件，不再入库，不污染 replay/UI | 前端状态保持 cancelled，不再回跳 running |
| **DB 锁冲突** | 数据库异常捕获 | 返回 HTTP 503 | 前端提示稍后重试 |
| **Worker 死亡** | 锁超时检测（> `SSE_SUBSCRIBE_TIMEOUT`） | abort 接口直接清理锁和 Round 状态 | 用户调用 abort 时得到即时响应 |

### SSE 断线重连详细流程

```
SSE 连接断开
    │
    ▼ (前端)
记录最后收到的 event sequence
    │
    ▼
GET /subscribe?last_sequence={last_seq}
    │
    ▼ (后端)
    ├── Round 仍在运行
    │     │
    │     ▼
    │   从 DB 回放 sequence > last_seq 的事件
    │     │
    │     ▼
    │   切换到实时模式（从 subscriber queue 推送）
    │
    └── Round 已结束
          │
          ▼
        从 DB 回放所有剩余事件 → 关闭连接
```

若初始 POST 在客户端收到响应头前断开，是否已创建 Round 属于歧义状态。前端不得重发 POST，而应在固定确认窗口内按原 `idempotency_key` 查询会话历史（当前最多 3 次）：命中 running Round 时立即从其 `round_id` 订阅，命中终态 Round 时直接收敛对应终态；该确认路径与正常 2xx 响应一样只发出一次 `stream_accepted`。

只有规定的 3 次 history 请求**全部成功**且每次均无匹配 Round，才能确认请求未被接受、触发一次接受前拒绝回调并恢复乐观清空的草稿。只要任一次 history 请求失败，确认窗口耗尽后仍必须保持 `ambiguous`：提示“暂时无法确认请求是否已受理，请刷新页面查看结果”，不得恢复草稿、不得提示重新发送，也不得以新幂等键重发。确定性的接受前 HTTP 4xx/5xx 不进入该歧义分支，可立即按拒绝处理。

用户在收到 POST 响应头前主动取消时，客户端立即中止 POST 并结束本地订阅，不查询 history、不发出接受/拒绝事件，也不恢复已乐观清空的草稿；请求在服务端是否落地仍未知，避免恢复后误重发。用户在等待 history 确认期间取消时，停止后续重试，忽略已在途 history 的迟到结果，不得因该结果订阅 Round 或恢复草稿。两种取消都保留“刷新查看”的安全边界，后续不得自动重发。

---

## 6. 可观测性

### 日志事件

| 日志事件 | 级别 | 包含信息 | 触发时机 |
|----------|------|----------|----------|
| Agent 执行开始 | INFO | `session_id`, `round_id`, `user_id`, 模型名称 | Round 创建时 |
| Agent 执行结束 | INFO | `session_id`, `round_id`, `status`, `step_count`, 耗时 | Round 到达终态时 |
| 上下文压缩触发 | INFO | 压缩级别, 压缩前/后 Token 数 | 每次压缩执行时 |
| LLM 重试 | WARNING | 模型名称, 错误信息, 重试次数, 延迟时间 | 每次重试时 |
| LLM Failover | WARNING | Primary 模型, Fallback 模型, 原始错误 | 切换 Fallback 时 |
| 取消请求状态变化 | INFO | `session_id`, `request_id`, 新状态 | 每次状态流转时 |
| resume resolution fallback | WARNING | `session_id`, `interrupt_id`, `parent_round_id`, `resume_round_id`, `tool_call_id`, `fallback_reason` | 冷实时替换失败或冷启动 stitching 失败时 |
| 工具执行耗时 | DEBUG | 工具名称, `session_id`, 耗时 | 每次工具执行完成时 |
| 心跳发送 | DEBUG | `session_id`, `round_id` | 每次心跳时 |
| Worker 死亡检测 | WARNING | `user_id`, `session_id`, 锁龄 | abort 检测到死锁时 |

---

## 7. 非目标

以下功能明确不在本模块范围内，不应在本模块中实现：

| 非目标 | 说明 |
|--------|------|
| 消息编辑/撤回 | 已发送的消息不支持修改或删除 |
| 多轮并行执行 | 每用户严格串行（per-user 锁），不支持同一用户同时运行多个 Agent |
| Token 用量计费 | 不跟踪 Token 消耗用于计费目的 |
| 对话分支/分叉 | 不支持从历史消息分叉出新的对话线路 |
| Agent 间通信 | Sub-Agent 共享沙箱但拥有独立历史，不支持 Agent 间直接消息传递 |
| 客户端工具执行 | 所有工具均在服务端执行，不支持将工具调用下发到客户端 |
