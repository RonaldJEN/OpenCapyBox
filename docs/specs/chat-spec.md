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

| 职责                    | 说明                                                                                               |
| ----------------------- | -------------------------------------------------------------------------------------------------- |
| 消息发送与 SSE 流式响应 | 接收用户消息，启动 Agent 执行，通过 SSE 实时推送事件流                                             |
| Agent 执行生命周期      | 管理从启动到终态的完整流程：启动 → 工具调用 → 完成/中断/取消/错误                                |
| SSE 订阅与断线重连      | 支持客户端重连后从指定 sequence 恢复事件流                                                         |
| 执行暂停与恢复          | Human-in-the-Loop：`ask_user` 补充输入及工具权限审批暂停同一 Round，并通过结构化 resolution 恢复 |
| 执行取消                | 支持主动取消当前单 worker 进程内正在运行的 Agent；跨 worker 投递不属于第一版能力                   |
| 幂等性保证              | 前端重复提交相同`idempotency_key` 不会产生多次执行                                               |
| AG-UI 事件生成与持久化  | 生成标准 AG-UI 事件并写入数据库，支持事后重放                                                      |
| 上下文压缩              | 多级压缩策略，确保对话历史不超出模型上下文窗口                                                     |
| 用户 token 限额门禁     | 在启动 send/resume run 前检查用户周/月 token 限额                                                  |

### 本模块不负责

- 会话 CRUD（由 Session 模块处理）
- 工作区 mutation（由 WorkspaceService/WorkspaceMutationCoordinator 统一处理；OpenSandbox 只执行受控 I/O）
- 模型管理（由 Model Registry 处理）
- Cron 定时任务执行
- 技能（Skills）的注册与管理
- 用户权限配置与限额配置（由 Auth/Admin 模块处理）

---

## 2. 数据模型

### 2.1 `rounds` 表（别名：Run）

一条 Round 对应一次完整的逻辑执行周期。用户发送一条新消息会创建一条 Round；Human-in-the-Loop 恢复始终复用原 Round，不创建 child Round。

| 字段                 | 类型       | 约束                                                   | 说明                                                                                                                            |
| -------------------- | ---------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `id`               | String(36) | PK                                                     | UUID，全局唯一                                                                                                                  |
| `thread_id`        | String(36) | FK →`sessions.id` (CASCADE), indexed                | 所属线程（当前等同于 session）                                                                                                  |
| `session_id`       | String(36) | FK →`sessions.id` (CASCADE, `use_alter`), indexed | 所属会话                                                                                                                        |
| `parent_run_id`    | String(36) | nullable, indexed                                      | 子 Agent / 分支关系                                                                                                             |
| `outcome`          | String(20) | nullable                                               | 执行结果：`success` / `interrupt`；`interrupt` 仅用于取消或最大步数终止，Human-in-the-Loop waiting 不发终态 outcome       |
| `user_message`     | Text       | NOT NULL                                               | 用户原始消息内容                                                                                                                |
| `user_attachments` | Text       | nullable                                               | JSON 序列化的附件列表                                                                                                           |
| `preferred_skills` | Text       | nullable                                               | JSON：`[{key, display_name}]`；普通 direct Round 在本次发送开始时解析出的有效“优先 Skill”展示快照，没有数据时按 `[]` 处理 |
| `preferred_mcp_connections` | Text | nullable                                             | JSON：`[{server_id, display_name}]`；普通 direct Round 解析出的“优先数据连接”展示快照，没有数据时按 `[]` 处理              |
| `final_response`   | Text       | nullable                                               | Agent 最终文本响应                                                                                                              |
| `step_count`       | Integer    | default=0                                              | Agent 执行步数                                                                                                                  |
| `status`           | String(20) | default=`"running"`                                  | 当前状态（见下方状态机）                                                                                                        |
| `idempotency_key`  | String(64) | nullable                                               | 前端生成的幂等键                                                                                                                |
| `created_at`       | DateTime   | default=now, indexed                                   | 创建时间                                                                                                                        |
| `completed_at`     | DateTime   | nullable                                               | 终态达成时间                                                                                                                    |

**唯一约束**: `UniqueConstraint(session_id, idempotency_key)`

**Round 状态机**:

```
                         ┌─────────────┐
                         │   running   │ ← 初始状态
                         └──────┬──────┘
                                │
                     请求交互   │   完成 / 失败 / 取消
                                ▼
                    ┌─────────────────────┐
                    │ waiting_interaction │
                    └──────────┬──────────┘
                               │ 回答被 continuation 接管
                               └──────────────→ running
```

**终态集合**:

| 集合名称               | 包含状态                                                        | 用途                                                                        |
| ---------------------- | --------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `COMPLETE_TERMINAL`  | `completed`, `failed`, `cancelled`, `max_steps_reached` | 判断 Round 是否已彻底结束（不可恢复）                                       |
| `SUBSCRIBE_TERMINAL` | `completed`, `failed`, `cancelled`, `max_steps_reached` | 判断 SSE 订阅是否应关闭连接                                                 |
| `QUIESCENT`          | `waiting_interaction`                                         | 当前没有 producer，但 Round 可由同一 runId 恢复；不是终态，订阅必须继续有效 |

> **设计决策**: `waiting_interaction` 不发 `RUN_FINISHED`，也不结束该 Round；回答后 continuation 继续使用同一 `runId`。

### 2.2 `agui_events` 表

持久化所有 AG-UI 事件，用于 SSE 重放（断线重连、订阅历史 Round）。

`CUSTOM synthetic_user_message` 事件只作为冷恢复顺序锚点；完整合成消息内容（尤其是 `read_image_file` 产生的 Data URL 图片上下文）必须先写入 `conversation_messages(is_synthetic=True)`，成功后才允许提交轻量 marker 到 `agui_events.payload`。

| 字段             | 类型       | 约束                                   | 说明                     |
| ---------------- | ---------- | -------------------------------------- | ------------------------ |
| `id`           | Integer    | PK, autoincrement                      | 自增主键                 |
| `run_id`       | String(36) | FK →`rounds.id` (CASCADE), NOT NULL | 所属 Round               |
| `event_type`   | String(50) | NOT NULL                               | 事件类型（22 种之一）    |
| `timestamp`    | Integer    | nullable                               | 事件时间戳（毫秒）       |
| `message_id`   | String(36) | nullable                               | 关联的消息 ID            |
| `tool_call_id` | String(36) | nullable                               | 关联的工具调用 ID        |
| `payload`      | Text       | NOT NULL                               | 完整 JSON 事件体         |
| `sequence`     | Integer    | NOT NULL                               | 事件序号（Round 内递增） |
| `created_at`   | DateTime   | default=now                            | 写入时间                 |

**索引**（共 5 个）:

1. `run_id` — 按 Round 查询所有事件
2. `event_type` — 按事件类型过滤
3. `(run_id, sequence)` — 断线重连时按序号范围查询
4. `message_id` — 按消息定位事件
5. `tool_call_id` — 按工具调用定位事件

### 2.3 `conversation_messages` 表

Agent 执行所需的对话历史。与 `agui_events` 不同，此表面向 LLM 上下文构建，而非前端事件回放。实时运行上下文（如当前时间、时区、workspace）不写入此表。

| 字段             | 类型       | 约束                          | 说明                                    |
| ---------------- | ---------- | ----------------------------- | --------------------------------------- |
| `id`           | Integer    | PK                            | 自增主键                                |
| `session_id`   | String(36) | FK →`sessions.id`, indexed | 所属会话                                |
| `round_id`     | String(36) | nullable, indexed             | 所属 Round                              |
| `sequence`     | Integer    | NOT NULL                      | 会话内全局序号                          |
| `role`         | String(20) | NOT NULL                      | `user` / `assistant` / `tool`     |
| `content`      | Text       | NOT NULL                      | JSON 序列化的消息内容                   |
| `is_summary`   | Boolean    | default=False                 | 是否为上下文压缩产生的摘要              |
| `is_synthetic` | Boolean    | default=False                 | 是否为系统合成消息（如 max_steps 提醒） |
| `token_count`  | Integer    | nullable                      | 消息的预估 Token 数                     |
| `created_at`   | DateTime   | default=now                   | 创建时间                                |

**唯一约束**: `UniqueConstraint(session_id, sequence)`

`is_summary=True` 仅保留给 `legacy_120` 兼容读取。默认 `checkpoint_v1` 不累计逐轮摘要，也不把多代摘要拼进 provider 请求；累计替代上下文以 `context_checkpoints` 为事实源。

### 2.3.1 `context_checkpoints` 表

保存不可变的累计替代上下文。每个新 generation 都完整替换此前 checkpoint，而不是在其后叠加摘要。

| 字段                                                    | 说明                                                                               |
| ------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `checkpoint_id` / `generation`                      | checkpoint 身份与 session 内单调代际                                               |
| `source_round_id`                                     | replacement 覆盖到的最后 Round；允许是 running/failed/cancelled                    |
| `source_message_sequence` / `source_event_sequence` | 在 source Round 内精确覆盖到的 conversation/event 游标                             |
| `trigger_phase`                                       | `pre_turn`、`mid_turn` 或 `model_downshift`                                  |
| `summary_text`                                        | 模型生成的原始 handoff summary                                                     |
| `replacement_messages_json`                           | 最新真实 user 原文（总计最多 20,000 近似 token）+ 一条 role=user synthetic summary |
| `source_token_count` / `replacement_token_count`    | 压缩前后 token 估算                                                                |

`schema_version=4`，checkpoint append-only，并在每次压缩完成时立即写入，不等待 Round 成功。旧 v1-v3 不做语义迁移，直接从权威 rounds/messages/events 重建。恢复顺序为 `最新 replacement + source Round 游标后的 suffix + 后续 Round`；最新 v4 无法解析、缺少 `source_round_id` 或 source Round 不属于当前权威主会话历史时，必须丢弃整个 replacement 并回到权威历史，不得拼接 `replacement + 完整历史`，也不回退旧 generation。`replacement_sha256` 仅保留为 nullable 兼容列，不再参与语义。

### 2.4 `llm_call_records` 表

持久化每次 LLM 调用（step 级）的输入输出快照，用于运行后审计与问题排查。

| 字段                                                    | 类型       | 约束                                            | 说明                                                                                                                                      |
| ------------------------------------------------------- | ---------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                                                  | Integer    | PK, autoincrement                               | 自增主键                                                                                                                                  |
| `session_id`                                          | String(36) | FK →`sessions.id`, NOT NULL, indexed         | 所属会话                                                                                                                                  |
| `round_id`                                            | String(36) | FK →`rounds.id` (CASCADE), NOT NULL, indexed | 所属 Round                                                                                                                                |
| `step_index`                                          | Integer    | NOT NULL                                        | 普通 step 从 1 开始；compaction 使用 session run 内递减的负索引避免唯一键冲突                                                             |
| `call_kind`                                           | String(30) | NOT NULL                                        | `agent_step` 或 `compaction`                                                                                                          |
| `request_message_count`                               | Integer    | nullable                                        | 本次实际发送给 provider 的消息条数（若为 provider 快照则取`messages` 长度）                                                             |
| `manual_review_status`                                | String(20) | NOT NULL, default=`没问题`                    | 人工后台标注结果，默认表示未发现问题                                                                                                      |
| `request_messages`                                    | Text       | NOT NULL                                        | 实际发送给 provider 的请求快照（JSON，包含 provider/model/messages、request-only runtime context，必要时包含 system/tools/stream 等参数） |
| `request_tools`                                       | Text       | NOT NULL                                        | 本次可用工具名称列表（JSON，用于快速检索；真实工具请求体以`request_messages` 为准）                                                     |
| `response_content`                                    | Text       | nullable                                        | LLM 返回文本                                                                                                                              |
| `response_thinking`                                   | Text       | nullable                                        | LLM 思考内容（若模型支持）                                                                                                                |
| `response_tool_calls`                                 | Text       | nullable                                        | LLM 返回的 tool_calls（JSON）                                                                                                             |
| `response_error`                                      | Text       | nullable                                        | LLM 调用失败时的错误文本                                                                                                                  |
| `finish_reason`                                       | String(50) | nullable                                        | 结束原因                                                                                                                                  |
| `usage_prompt_tokens`                                 | Integer    | nullable                                        | prompt token 数                                                                                                                           |
| `usage_completion_tokens`                             | Integer    | nullable                                        | completion token 数                                                                                                                       |
| `usage_total_tokens`                                  | Integer    | nullable                                        | 总 token 数                                                                                                                               |
| `first_token_latency_s`                               | Float      | nullable                                        | 从发起请求到收到首个流式 token 的耗时（秒）                                                                                               |
| `completion_latency_s`                                | Float      | nullable                                        | 从发起请求到本次 LLM 调用完成返回的总耗时（秒）                                                                                           |
| `compaction_triggered`                                | Boolean    | NOT NULL, default=`false`                     | 本次普通调用前是否触发 Codex compaction                                                                                                   |
| `compaction_pre_tokens`                               | Integer    | nullable                                        | 压缩前估算 token 数                                                                                                                       |
| `compaction_post_tokens`                              | Integer    | nullable                                        | 压缩后估算 token 数                                                                                                                       |
| `compaction_tokens_saved`                             | Integer    | nullable                                        | 本次压缩节省 token 数（`pre - post`）                                                                                                   |
| `compaction_microcompact_compacted_messages` 等旧字段 | Integer    | nullable                                        | 仅保留数据库/API 兼容，Codex 路径固定为 0，不再代表运行阶段                                                                               |
| `history_strategy`                                    | String(30) | nullable                                        | `checkpoint_v1` 或 legacy 策略                                                                                                          |
| `checkpoint_id`                                       | String(36) | nullable                                        | 本次请求使用的 checkpoint                                                                                                                 |
| `history_payload_sha256`                              | String(64) | nullable                                        | 实际请求消息规范 JSON 哈希                                                                                                                |
| `history_breakdown_json`                              | Text       | nullable                                        | real user、assistant、tool、synthetic、图片上下文、累计摘要的数量分解                                                                     |
| `created_at`                                          | DateTime   | default=now, indexed                            | 写入时间                                                                                                                                  |

**唯一约束**: `UniqueConstraint(round_id, step_index)`

**限额语义**: Chat 模块使用 `llm_call_records.usage_total_tokens` 通过 `sessions.user_id` 聚合用户本周/本月 token 用量，并与 `auth_users.token_limit_per_week` / `auth_users.token_limit_per_month` 比较。

### 2.5 `user_run_locks` 表

用户级执行并发 slot。确保每个用户同一时刻最多有 `AGENT_USER_CONCURRENCY_LIMIT` 个不同 session 在执行，且同一 session 仍只能有一个 active run。

| 字段           | 类型        | 约束                   | 说明                     |
| -------------- | ----------- | ---------------------- | ------------------------ |
| `lock_id`    | String(36)  | PK                     | UUID，用于标识锁的持有者 |
| `user_id`    | String(100) | NOT NULL, indexed      | 锁所属用户               |
| `session_id` | String(36)  | NOT NULL, indexed      | 锁定的会话               |
| `slot`       | Integer     | NOT NULL               | 用户内并发 slot 编号     |
| `created_at` | DateTime    | NOT NULL               | 锁创建时间               |
| `updated_at` | DateTime    | NOT NULL, onupdate=now | 心跳刷新时间             |

**并发语义**: `Unique(user_id, slot)` 原子限制同一用户可占用的 slot 数，`Unique(user_id, session_id)` 保证同一会话不可重入。若所有 slot 已占用，返回 429。

### 2.6 `run_cancel_requests` 表

取消请求 append-only 审计表。第一版按单 worker 部署，取消投递由进程内 `RunCancelService` registry + per-run cancel token 完成；DB 行只记录审计与诊断线索，不承担跨 worker command delivery。

| 字段                | 类型        | 约束                              | 说明                                   |
| ------------------- | ----------- | --------------------------------- | -------------------------------------- |
| `request_id`      | String(36)  | PK                                | UUID，用于跟踪取消请求                 |
| `session_id`      | String(36)  | NOT NULL, indexed                 | 目标会话                               |
| `user_id`         | String(100) | NOT NULL, indexed                 | 请求取消的用户                         |
| `target_run_id`   | String(36)  | nullable, indexed                 | 目标 run；未命中本地 registry 时可为空 |
| `root_run_id`     | String(36)  | nullable, indexed                 | 根 run，用于 subagent/派生 run 审计    |
| `requested_after` | DateTime    | nullable, indexed                 | cancel epoch；避免旧取消误杀后续新 run |
| `state`           | String(20)  | NOT NULL, default=`"requested"` | 取消状态（见下方状态机）               |
| `requested_at`    | DateTime    | NOT NULL                          | 请求时间                               |
| `acked_at`        | DateTime    | nullable                          | 本进程 registry 命中确认时间           |
| `completed_at`    | DateTime    | nullable                          | 取消完成时间                           |
| `updated_at`      | DateTime    | NOT NULL                          | 最后更新时间                           |

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

| 字段                            | 类型        | 约束                                           | 说明                                                                                         |
| ------------------------------- | ----------- | ---------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `id`                          | String(36)  | PK                                             | 绑定 UUID                                                                                    |
| `user_id`                     | String(100) | FK →`auth_users.user_id` (CASCADE), indexed | 绑定所属用户                                                                                 |
| `session_id`                  | String(36)  | FK →`sessions.id` (CASCADE), indexed        | 内部会话                                                                                     |
| `channel`                     | String(50)  | NOT NULL, indexed                              | 渠道标识，如`web` / `cron` / 未来外部 channel                                            |
| `account_id`                  | String(100) | nullable, indexed                              | 渠道账号或 bot 实例                                                                          |
| `peer_kind`                   | String(20)  | NOT NULL                                       | `web` / `direct` / `group` / `thread` / `cron` / `webhook`                       |
| `peer_id`                     | String(255) | NOT NULL                                       | 渠道内对端标识                                                                               |
| `external_thread_id`          | String(255) | nullable                                       | 渠道线程标识                                                                                 |
| `binding_key`                 | String(64)  | NOT NULL, indexed                              | 对`(channel, account_id, peer_kind, peer_id, external_thread_id)` 做规范 JSON 后的 SHA-256 |
| `reply_route_json`            | Text        | nullable                                       | `ReplyRoute` 快照                                                                          |
| `metadata_json`               | Text        | nullable                                       | adapter 元数据                                                                               |
| `created_at` / `updated_at` | DateTime    | NOT NULL                                       | 创建与更新时间                                                                               |

**唯一约束**: `Unique(user_id, binding_key)`。

**当前阶段语义**: `DeliveryService` 是 no-op 边界；`ChannelProjection` 只把
`ChannelMessageReplyRoute` 上的终态 `RUN_FINISHED` 投影为 `DeliveryIntent`，
不会发送外部网络消息。Web SSE 仍由 chat route 直接返回。

### 2.8 `agent_interactions` 表

Human-in-the-Loop 的 runtime-neutral 事实源。一条记录描述同一 Round 内的一次提问或工具审批请求；回答不会创建新的用户 Round。

| 字段                               | 类型        | 约束                                    | 说明                                                                                                                      |
| ---------------------------------- | ----------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `id`                             | String(36)  | PK                                      | interaction id；工具审批时与`tool_approval_requests.id` 相同                                                            |
| `session_id`                     | String(36)  | FK →`sessions.id` (CASCADE), indexed | 所属会话                                                                                                                  |
| `round_id`                       | String(36)  | FK →`rounds.id` (CASCADE), indexed   | 暂停并继续的同一个 Round                                                                                                  |
| `kind`                           | String(32)  | NOT NULL                                | `user_input` / `tool_approval`                                                                                        |
| `tool_call_id`                   | String(64)  | nullable, indexed                       | 对应模型 tool call                                                                                                        |
| `status`                         | String(20)  | NOT NULL                                | `pending` → `answered`；Round 终态收敛时也可为 `cancelled` / `failed`                                            |
| `request_payload`                | Text        | NOT NULL                                | `interaction_requested.value` 的规范 JSON                                                                               |
| `answer_payload`                 | Text        | nullable                                | 已接受回答的规范 JSON；有值、status 仍为 pending 且`continuation_started_at` 为空时表示 continuation 可恢复             |
| `tool_result_content`            | Text        | nullable                                | 注入模型历史的冻结文本                                                                                                    |
| `external_request_id`            | String(128) | nullable                                | 外部 runtime 的请求关联 ID                                                                                                |
| `claim_token`                    | String(64)  | nullable                                | continuation 所有权围栏；不是工具执行授权                                                                                 |
| `claim_lease_expires_at`         | DateTime    | nullable                                | continuation 可回收租约                                                                                                   |
| `continuation_started_at`        | DateTime    | nullable, indexed                       | 与 durable`interaction_resolved` / `Round → running` 同事务写入；一旦有值，claim 过期只能 fenced failed，不得 repark |
| `requested_at` / `resolved_at` | DateTime    |                                         | 请求与最终解决时间                                                                                                        |
| `created_at` / `updated_at`    | DateTime    | NOT NULL                                | 审计时间                                                                                                                  |

**不变量**：

- 同一 Round 最多有一条 `status=pending` 的 Interaction。
- 创建 Interaction、把 Round 改为 `waiting_interaction`、持久化 `CUSTOM interaction_requested` 必须在同一数据库事务提交；不得留下“history 显示等待但 subscribe 无事件”的崩溃窗口。
- 接受回答后先保持 `status=pending`，由 continuation 取得可续租 claim；只有回答已跨过按 kind 定义的最终持久化完成边界后才转 `answered`。
- `continuation_started_at` 为空时，进程在启动 continuation 前崩溃可释放/回收 claim，并把 Round 原子停回 `waiting_interaction`。该字段已有值时，恢复路径必须写 durable `RUN_ERROR` 并将 Round/Interaction 收敛 failed；不得重新显示旧问题卡。工具 `executing` 仍须先按 execution lease 收敛 unknown，绝不自动重试。
- 所有复合写入固定锁序为 `Round → AgentInteraction → ToolApprovalRequest`，避免 PostgreSQL 反向锁序死锁。

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
  "preferred_mcp_server_ids": ["server-uuid"], // 可选，本轮优先数据连接
  "thinking_mode": "enabled",          // 可选：provider_default / enabled / disabled
  "reasoning_effort": "max"             // 可选：当前模型声明的精确强度
}
```

**ContentBlock 类型**:

| 类型          | 结构                                                                                       | 说明                                    |
| ------------- | ------------------------------------------------------------------------------------------ | --------------------------------------- |
| `text`      | `{type: "text", text: string}`                                                           | 纯文本消息                              |
| `image_url` | `{type: "image_url", image_url: {url: string}}`                                          | 图片（base64 data URI 或 URL）          |
| `video_url` | `{type: "video_url", video_url: {url: string}}`                                          | 视频                                    |
| `file`      | `{type: "file", file: SessionFile \| WorkspaceFile}` | Session 文件或用户持久工作区引用 |

```ts
type SessionFile = {
  source?: "session"; path: string; name?: string; mime_type?: string; size?: number;
};
type WorkspaceFile = {
  source: "workspace"; entry_id: string;
  version_id?: string;
  kind?: "file" | "directory"; name?: string; mime_type?: string; size?: number;
};
```

- 客户端不得为 `source="workspace"` 提交 `path/revision/tree_revision/manifest_sha256`。普通选择只提交稳定 `entry_id`，服务端在 Round 受理时解析该用户当时的 current entry；只有用户明确选择文件历史版本时才提交 `version_id`，文件夹不得提交。客户端名称、类型和大小只是 optimistic 展示提示，不能作为正文或所有权事实。
- Workspace **文件**在创建 Round 前复制到当前 Session 的隐藏 `.workspace-snapshots/<entry>/<snapshot>/`。省略 `version_id` 时冻结受理时 current head，显式 `version_id` 时冻结该不可变版本；staging 成功后持久化同一 capture 的 `entry revision/version_id/version_sequence/path/name/sha256/size`。禁止把历史正文 version 与另一个时点的 entry metadata 拼成同一附件。
- Workspace **文件夹**不复制后代文件，也不生成 manifest；服务端只校验稳定 `entry_id` 仍指向 active directory，并生成 `workspace://entry/<entry_id>` 实时引用。Agent 每次通过 `workspace_list(parent_id=<entry_id>)` 读取当时的当前目录，需要文件正文时再对目标文件调用 `workspace_stage`。附件中的 `revision/tree_revision` 仅是受理时审计值，不提供前后内容一致性；移动后稳定 `entry_id` 继续有效，永久删除后不可用。目录不接受 `version_id`。
- 服务端在处理每个 Workspace 附件前发送非持久化 `CUSTOM attachment_preparing {index,total,name,kind}`，长步骤之间继续发送 heartbeat。该事件只表示受理前准备进度，不等价于 `RUN_STARTED` 或 `stream_accepted`。任一附件或 Round admission 失败时解除已生成的文件临时引用，并用 durable cleanup job 删除本次文件 snapshots；纯文件夹发送不产生 snapshot 清理工作。

**本轮 Skill / MCP 偏好契约与作用域**:

- 值为 Skill 的稳定内部 `key` 数组；最多 50 项。每项先 trim，空项忽略，其余必须不超过 128 个 Unicode 字符；为兼容官方 Skill，允许人类可读 Unicode、空格和括号，禁止 `/`、`\`、`?`、`#`、`%` 及 Unicode General Category 为 `C*` 的控制/格式化/不可见字符。服务端按首次出现顺序去重；其他非法 key 使请求校验失败。
- `preferred_mcp_server_ids` 是稳定 MCP server id 数组（当前 DB 生成 UUID）；最多 20 项，每项 trim 后最长 36 个 ASCII 字符，只允许字母、数字、`.`、`_`、`:`、`-`，空项忽略并按首次出现顺序去重。控制字符、内部空白和其他字符返回 422。客户端不提交 installation、凭证或展示名。
- 两个字段都是当前逻辑执行链的软偏好，不是强制工具调用或权限白名单。Skill 仅在暴露 `get_skill` 时投影；MCP 仅在暴露 `mcp_tool_search` 时投影。首个相关 MCP 检索把首选连接展示名带入 query；没有合适工具时允许回退其他已启用连接。未选择 MCP 时维持默认联网与自动路由。
- Web Adapter 把二者合并为唯一的 `bsbox.turn_preferences.v1`：`{"mode":"preferred","skill_keys":[...],"mcp_server_ids":[...]}`。附件沿用独立 `ContentBlock` 输入链路并按下方 file block 语义转换，不得重复写入该偏好上下文。
- run 启动前，Skill 按该用户当前 registry 解析；MCP 只从当前 Agent 的 `McpCatalogSnapshot.connections` 解析。未知、已禁用、发现失败或没有可见工具的项被忽略，不导致整次发送失败。
- direct Round 分别固化 `preferred_skills: [{key, display_name}]` 与 `preferred_mcp_connections: [{server_id, display_name}]`。两份展示快照均不可变，读取历史时不得用当前 registry/catalog 改写。
- direct `RUN_STARTED` 同时携带 `preferredSkills` 与 `preferredMcpConnections`，显式空数组也是权威结果，用于替换或清除 optimistic 标签；same-Round resume 不发新的 `RUN_STARTED`。
- Skill/MCP 的通用调用规则与选择后提示固定在平台 `AGENTS.md`；动态 Skill/MCP 清单只携带可用项元数据，不得夹带调用说明，也不得因本轮选择动态改写 system message。provider 请求只把紧凑 `<ui_context>` 前置到精确匹配的原始 user message 请求副本；不得写回 `agent.messages`、`conversation_messages` 或标题/摘要/记忆请求。属性值是数据标签而非指令，正文始终优先。
- Skill 只有在 `get_skill` 成功后、连接只有在真实远程 MCP 工具调用成功后才能声称“已使用”；成功的 `mcp_tool_search` 只代表发现工具。UI 选择本身不构成使用审计。
- 若产生 Interaction，服务端把唯一 `runtime_context` 与 `turn_preferences_origin_user_message_id` 写入热 pending state 和持久化 request payload，并先清除 producer 夹带的同名值。resume 按当时 registry/catalog 重新解析运行偏好，连续暂停始终锚定最初 user message。
- same-Round continuation 不改写原 Round 的两份展示快照。`turn_preferences_origin_user_message_id` 是服务端专用元数据，不属于客户端 payload，发送和 resume 均不能提交或覆盖。

**本轮推理选择契约**:

- `thinking_mode` 与 `reasoning_effort` 是发送瞬间的不可变快照，只覆盖当前逻辑执行链；两者均省略时必须把当前模型目录默认值物化到快照与 direct Round，不得用 `NULL / NULL` 延迟解析。`disabled` 必须清除并拒绝同时携带的强度。
- 后端先按 session 的精确 `model_id` 校验：仅 OpenAI 兼容且显式声明非空 `supported_reasoning_efforts` 的模型可切换；`disabled` 校验目录中的 `off`，无强度的 `enabled` 校验 `on`，具体强度精确命中同一有序目录，失效或伪造值在建立 SSE 前返回 400。`reasoning_effort` 中的 `off` / `on` 必须拒绝，它们只能通过 `thinking_mode` 表达。
- 归一化后写入独立版本化上下文 `bsbox.reasoning.v1`，不得混入 `turn_preferences` 上下文；OpenAI 同步、流式、工具 follow-up、retry 和 failover 在同一 run 中都从 `ContextVar` 读取同一冻结快照。failover 不按备用模型白名单过滤或降级，但只能尝试能够编码该快照的客户端：`provider_default + null` 可跨 provider；显式开关或具体强度不能交给 Anthropic 客户端；OpenAI `thinking_wire_format=none` 只能承载具体 `reasoning_effort`，不能承载纯 On/Off。协议不兼容的备用模型必须跳过，继续尝试后续模型；若没有任何备用模型兼容，保留并抛出主模型的原始失败。
- direct Round 将最终 `thinking_mode` / `reasoning_effort` 持久化用于审计和历史恢复。same-Round continuation 沿用该快照；`resume` API 不接受新的推理选择。
- `enabled` / `disabled` 由模型 DB 的 `thinking_wire_format` 编码：DashScope 类网关使用 `enable_thinking=true|false`，DeepSeek 原生协议使用 `thinking: {type: enabled|disabled}`；`disabled` 同时移除强度，非空强度作为顶层 `reasoning_effort` 发送。选择 Off 不能退化成字段缺省。

**file block 注入语义**:

- `file` block 在进入 Agent 上下文前只映射为当前 user message 的选择元数据：普通文件使用 `[附件文件] metadata={"name":"<name>","path":"<path>"}`，目录使用 `[附件文件夹] metadata={"name":"<name>","path":"<path>","kind":"directory"}`。如何读取附件的通用调用规则固定在平台 `AGENTS.md`，不得因附件选择动态改写 system message。
- `metadata` 是由 `{name, path}` 经过 JSON 编码生成；Agent 读取附件时必须以 `metadata.path` 作为唯一事实源。
- Workspace 文件的 `metadata.path` 是服务端生成的 Session snapshot；Workspace 文件夹的 `metadata.path` 是 `workspace://entry/<entry_id>` 实时引用，必须用 `workspace_list(parent_id=workspace_entry_id)` 展开，不能交给 Session 文件工具枚举。附加的 `source/workspace_entry_id/workspace_path/workspace_revision/workspace_version_id/workspace_version_sequence/workspace_reference_mode` 用于说明读取方式和来源，不能把原 workdir 绝对路径交给模型。
- Round 的文件附件持久化 `source/entry_id/revision/version_id/version_sequence/origin_path/snapshot_path/sha256`；目录持久化 `kind=directory/is_directory=true/reference_mode=live/tree_revision`，不含 `snapshot_path/manifest_sha256`。这些是选择审计，不是已删除文件的恢复来源；文件预览固定 snapshot/version，目录卡片按稳定 entry_id 打开当前目录，entry 永久删除后 history 和当前客户端投影均过滤该项。“在工作区打开”只是次级动作。
- 该提示是中性提示，不强制触发 `read_file` 调用；是否读取由当前任务意图决定。

**工作区工具语义**:

- “当前 Session 目录”专指本轮 Session/Cron 临时执行目录；“Workspace/工作区”专指跨会话持久文件区，模型可见提示和工具描述不得再把前者称作 Workspace。只有原始用户请求明确要求访问持久工作区时才允许调用 `workspace_*`；项目名、产品名、缺少资料、追求真实性和安全只读都不构成授权。Workspace 文件附件授权读取其 Session 冻结副本；文件夹附件授权只读访问所选 entry 的当前子树，写操作仍须由原始请求明确授权。子 Agent 只有在父任务明确委派持久 Workspace 访问时才能调用这些工具。
- `workspace_list`、`workspace_stage` 为读取能力；`workspace_publish`、`workspace_create_directory` 为编辑能力；`workspace_move`、`workspace_delete` 为管理能力。
- `workspace_list` 支持可选 `cursor`，原样透传服务端分页游标；每页默认 50 项，工具声明上限 100 项。返回 `next_cursor` 非空且需要更多结果时，保持 `parent_id/query/limit` 不变并携带该游标继续请求；不自动拉取全部结果。
- `workspace_stage` 把指定 file version、目录 tree revision 或当前 head 复制到当前 Session/Cron 目录；模型携带的观察 revision 已过期时，工具内部自动重取一次当前 head，不把正常的人类并发编辑暴露成循环重试。目录保留层级与空子目录。成功结果中的绝对 `snapshot_path` 供文件工具使用，相对 `publish_source_path` 供 `workspace_publish` 使用，并返回实际 `revision/base_version_id/tree_revision`；`read_file/apply_patch` 的相对路径基准为当前 Session 目录。
- `workspace_publish` 仅接受当前 Session/Cron 目录内的普通文件，先把冻结提案写入用户内 SHA 内容对象并建立 `change_set_proposal/base` 显式引用，再由统一发布入口校验 base/current version、claim、配额与 journal。已有文件先校验 DB head、不可变 head 对象与 active 物化文件：active 命中旧版本时从 head 修复，出现未入库新内容时先静默吸收为 `web` head；三方合并的 current 始终读取内部 head 对象，不读取用户命名的 active 路径。base 未变化时直接 apply；变化时对 Markdown/TXT、CSV、XLSX 自动三方合并，不同位置同时保留、同一位置保留当前正式文件（人的内容）。无法可靠合并时保持当前正式内容，提案仅留作内部审计，不向普通用户发出决策请求。
- proposed/conflict/failed 只产生标准 `TOOL_CALL_RESULT` 并保存在服务端审计中，不发送 `CUSTOM workspace_change_proposed/workspace_change_conflict` 给普通客户端。并发 conflict 由 Workspace maintenance 继续收敛；不可恢复的 head/proposal 读取或校验错误进入 failed 终态并保留提案；调用取消或 worker 丢失继续依赖 prepared journal。只有成功正式 mutation 后才持久化 `CUSTOM workspace_resource_changed`，不得从工具文本或助手正文正则推断工作区产物。

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

| HTTP 状态码 | 含义                   | 场景                                                                    |
| ----------- | ---------------------- | ----------------------------------------------------------------------- |
| 404         | 会话不存在             | `session_id` 无效或不属于当前用户                                     |
| 400         | 本轮推理选择无效       | 模型不支持按轮控制，或强度不在模型白名单                                |
| 410         | 会话已完成             | 会话处于终态，不再接受新消息                                            |
| 422         | 请求校验失败           | `content`、`preferred_skill_keys` 或 `preferred_mcp_server_ids` 超出数量/长度限制、字段类型非法 |
| 429         | 当前运行任务数已达上限 | 用户 slot 已满，或同 session 已有 active run                            |
| 503         | 服务不可用             | DB 锁冲突等内部错误                                                     |

#### 流内错误事件

当 Agent 执行过程中发生错误，不会返回 HTTP 错误码，而是通过 SSE 事件流推送错误事件：

| 错误事件                | 触发条件                                                                                                                         |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `AGENT_INIT_FAILED`   | Agent 初始化失败（沙箱连接、历史加载、技能初始化等）                                                                             |
| `AGENT_INIT_TIMEOUT`  | Agent 初始化超过配置的总墙钟期限；取消初始化任务并释放运行锁                                                                      |
| `ROUND_IN_PROGRESS`   | 幂等键冲突：相同`idempotency_key` 的 Round 已在执行中                                                                          |
| `INTERACTION_PENDING` | 当前 session 已有同一 Round 的 pending Interaction；这是未创建新 Round 的控制面拒绝，客户端必须恢复权威 waiting 状态与未受理草稿 |
| `INTERNAL_ERROR`      | 其他内部错误                                                                                                                     |

无 durable sequence 的流内错误只结束当前 HTTP transport，不自动证明任何既有 Round 已终态。客户端必须依据错误类型与 `history/v2` 区分控制面拒绝、订阅故障和真正的运行终态。

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
Agent 初始化和中断状态校验在 SSE generator 内执行；入口只做会话、token、用户并发 slot 等前置校验并在返回流前结束请求级事务。generator 只接收预读标量，Agent 初始化与取消复核使用独立短生命周期 Session，禁止请求级 DB Session 跨 Agent / sandbox 初始化长时间持有连接。

响应继续使用原 `runId == round_id`，不再发送第二个 `RUN_STARTED`。持久化的 `CUSTOM interaction_resolved` 是 continuation 已接管原 Round 的 wire 边界；HTTP 200 或客户端本地 `stream_accepted` 只表示传输已建立，不表示 Round 已恢复运行。

#### 恢复路径

| 路径                        | 条件             | 行为                                                                                                                        |
| --------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **same-Round 热路径** | Agent 仍在内存中 | 在`agent_interactions` 幂等冻结答案 → continuation claim → 持久化 `interaction_resolved` → 原 Round 恢复 `running` |
| **same-Round 冷路径** | AgentPool 已回收 | 从 Round 事件与`agent_interactions` 重建占位，将冻结的 `tool_result_content` 回填后继续同一 `runId`                   |

恢复不创建新 Round。回答、claim、`interaction_resolved` 以及后续 Agent 事件都属于原 Round。

若原 Round 携带 Skill/MCP 偏好生成的 `bsbox.turn_preferences.v1`，pending Interaction 必须保留这个唯一 runtime context 及服务端生成的原始 user message 锚点。`resume` 不接收新的偏好或锚点；服务端按 resume 当时的 Skill registry 与 MCP catalog 重新解析运行偏好，但两份展示快照仍属于原 direct Round。连续交互始终锚定最初用户消息，不得改成回答文本。

原 Round 已固化的 `thinking_mode` / `reasoning_effort` 在 continuation 中保持不变，确保审批或回答之后的 provider follow-up 不会因为用户后来调整输入框选择而改变推理强度。

#### 错误码

| HTTP 状态码 | 含义                   | 场景                                         |
| ----------- | ---------------------- | -------------------------------------------- |
| 404         | 会话不存在             | `session_id` 无效                          |
| 429         | 当前运行任务数已达上限 | 用户 slot 已满，或同 session 已有 active run |
| 503         | 服务不可用             | 内部错误                                     |

返回 SSE 后的恢复期错误以 AG-UI `RUN_ERROR` 结束流：

| RUN_ERROR code                   | 含义                                    | 场景                                                      |
| -------------------------------- | --------------------------------------- | --------------------------------------------------------- |
| `NO_PENDING_INTERRUPT`         | 没有待处理的中断                        | 会话没有匹配的 pending interrupt，或中断 ID 已过期/已恢复 |
| `RESUME_CONFLICT`              | 回答与持久化事实冲突                    | 并发恢复已获胜，或同一 interaction 收到不同答案           |
| `INVALID_INTERACTION_RESPONSE` | 回答格式或枚举值不符合 Interaction kind | 例如工具审批缺少`answers.approval`，或值不在允许枚举中  |
| `AGENT_INIT_FAILED`            | Agent 初始化失败                        | AgentPool 获取或初始化失败                                |
| `AGENT_INIT_TIMEOUT`           | Agent 初始化超时                        | 初始化超过 `AGENT_INIT_TIMEOUT_SECONDS`，取消并释放运行锁 |
| `USER_ABORT`                   | 恢复初始化期间被取消                    | 较新的 abort 已经收敛该会话                               |
| `INTERNAL_ERROR`               | continuation 尚未启动的内部错误         | 服务端保留或恢复权威 waiting 状态                         |

在 `interaction_resolved` 之前收到上述 `RUN_ERROR` 属于 **resume 控制面错误**，不得把原 `waiting_interaction` Round 改为 `failed`。客户端必须查询 `history/v2`，按权威的 `waiting_interaction` / `running` / 终态恢复。越过 `interaction_resolved` 后的运行异常也必须先与原 Round 原子提交为带 durable sequence 的 `RUN_ERROR`；无 sequence 的 adapter/transport 错误仍只能触发 history 恢复，不能单独终态化 Round。

### 3.3 `GET /api/chat/{session_id}/round/{round_id}/subscribe`

订阅指定 Round 的 AG-UI 事件流。用于断线重连或查看历史 Round 的事件回放。

#### 请求

```
GET /api/chat/{session_id}/round/{round_id}/subscribe?last_sequence=0
Authorization: Bearer <token>
```

| 参数              | 类型 | 默认值 | 说明                                                   |
| ----------------- | ---- | ------ | ------------------------------------------------------ |
| `last_sequence` | int  | 0      | 客户端已接收的最大事件序号，服务端从此序号之后开始推送 |

#### 响应

SSE 事件流。

#### 行为分支

| Round 状态                         | 行为                                                                                                                          |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| 已在终态 (`SUBSCRIBE_TERMINAL`)  | 回放`sequence > last_sequence` 的所有事件 → 关闭连接                                                                       |
| 仍在运行 (`running`)             | 回放历史事件 → 切换到实时模式，从 subscriber queue 推送新事件                                                                |
| 暂停等待 (`waiting_interaction`) | 回放（至少包含持久化的`interaction_requested`）→ 保持订阅，等待其他标签页的 `interaction_resolved`、后续输出、取消或终态 |

- 心跳间隔：15 秒
- 服务端不设置应用级订阅最大时长；连接持续到 Round 终态、客户端断开或基础设施关闭
- `SSE_SUBSCRIBE_TIMEOUT` 是 UserRunLock/runtime 心跳陈旧判定阈值，不关闭健康的 running/waiting 订阅
- 首次 replay 与 subscriber queue 注册之间必须有无缝 handoff：若终态在首次 replay 后提交，`ensure_terminal` 检出终态时仍须从最新已投递 sequence 二次 replay 并把 terminal 发给客户端；只有客户端 cursor 已包含该 terminal 时才允许直接 clean EOF

#### 错误码

| HTTP 状态码 | 含义         |
| ----------- | ------------ |
| 404         | Round 不存在 |

订阅建立后还可能收到无 durable sequence 的 `RUN_ERROR(code=SUBSCRIBE_FAILED)`；它只表示当前 transport 失败，客户端必须按最后 sequence 重连或回拉 history，不得把 Round 置为 failed。只有带 durable sequence 的 Round `RUN_FINISHED` / `RUN_ERROR`，或由权威 history 投影出的终态，才能终态化客户端 Round；heartbeat 不改变状态。

### 3.4 `POST /api/chat/{session_id}/abort`

请求取消当前正在运行的 Agent 执行。

#### 响应

```json
// 常规即时取消（单 worker registry / 接口即时收敛）
{
    "status": "cancelled",
    "request_id": "uuid",
    "reason": "force_aborted",
    "outcome_warning": null
}

// Worker 已死亡，直接清理
{
  "status": "cancelled",
  "request_id": "uuid",
  "reason": "worker_dead",
  "outcome_warning": null
}

// init-window（有会话锁但尚未创建 round）即时解锁
{
    "status": "cancelled",
    "request_id": "uuid",
    "reason": "force_unlocked",
    "outcome_warning": null
}
```

| `status` 值                                | 含义                                                                                    |
| -------------------------------------------- | --------------------------------------------------------------------------------------- |
| `cancelled` + `reason: "force_aborted"`  | 已将 running round 直接收敛为 cancelled，并立即释放会话锁                               |
| `cancelled` + `reason: "worker_dead"`    | 锁已超过`SSE_SUBSCRIBE_TIMEOUT` 未刷新，判定 Worker 死亡，直接强制清理锁和 Round 状态 |
| `cancelled` + `reason: "force_unlocked"` | init-window（无 running round）也立即释放会话锁，允许用户立刻重发                       |

> 约束：`cancelled(*)` 仅在目标会话锁确认已释放时返回；若释放失败（例如数据库锁冲突），接口返回 `HTTP 503`，不会返回“假成功”。

`abort` 同时覆盖 `running`、`waiting_interaction` 和 init-window。取消 waiting Round 时必须把 pending Interaction 以及尚未 dispatch 的审批 `requested` / `approved` 一并收敛，并向已注册 subscriber fanout 同一 durable 终态。REST 响应只通过 nullable `outcome_warning` 暴露风险：能证明尚未 dispatch 时为 `null`，审批为 `executing` / `unknown` 或普通 running Round 无法证明尚未派发时返回保守警告。durable `RUN_FINISHED.result` 另以 `outcomeUncertain` 布尔值表达同一判断。风险判断、审批收敛、Round terminal event 必须在同一 `Round → AgentInteraction → ToolApprovalRequest` 锁事务内完成，不能先无锁读取再写终态。

#### 错误码

| HTTP 状态码 | 含义               |
| ----------- | ------------------ |
| 404         | 会话不存在         |
| 409         | 没有正在进行的执行 |
| 503         | 服务不可用         |

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

| `state` 值  | 含义                          |
| ------------- | ----------------------------- |
| `none`      | 没有活跃的取消请求            |
| `requested` | 取消已请求，Worker 尚未确认   |
| `acked`     | Worker 已确认取消，正在清理中 |
| `completed` | 取消已完成                    |

#### 错误码

| HTTP 状态码 | 含义       |
| ----------- | ---------- |
| 404         | 会话不存在 |

### 3.6 `GET /api/sessions/{session_id}/history/v2`

按 Round/Step 结构返回会话历史。`HistoryResponseV2.rounds[]` 除既有 Round 字段外，必须包含：

```json
{
  "preferred_skills": [
    {"key": "pdf", "display_name": "PDF 处理"}
  ],
  "preferred_mcp_connections": [
    {"server_id": "server-uuid", "display_name": "东方财富数据"}
  ],
  "assistant_file_references": [
    {
      "ref_id": "workspace:entry-uuid:version-uuid",
      "source": "workspace",
      "entry_id": "entry-uuid",
      "version_id": "version-uuid",
      "path": "reports/result.xlsx",
      "workspace_path": "reports/result.xlsx",
      "name": "result.xlsx",
      "revision": "2",
      "size": 1024,
      "type": "xlsx"
    }
  ]
}
```

`preferred_skills` 与 `preferred_mcp_connections` 分别是 Skill、MCP 连接的不可变展示快照，并遵循以下投影规则：

- 普通 direct Round 返回该次 `message/stream` 在运行开始时解析并持久化的两份有效展示快照；任一没有有效偏好、字段为空或损坏时独立返回 `[]`。
- same-Round continuation 仍是原 direct Round，因此继续返回原先冻结的展示快照，不新增重复标签。
- 这些数组不是使用审计；不能据此声称 Skill 已加载或 MCP 连接已被真实调用。
- 各个独立 direct Round 的快照彼此隔离，不继承、不合并，也不根据当前启停状态、改名或删除情况重算。
- 文件卡只使用 `assistant_file_references`：Session 引用只来自主 Agent 显式调用 `present_files` 后记录的当前路径，Workspace 引用来自成功 mutation 内受保护的 immutable version。Session 引用不复制正文，文件被覆盖后打开最新内容，被删除后提示不可用。history 不再返回旧 `workspace_resources/workspace_change_sets` 兼容字段。

当 Round 为 `waiting_interaction` 时，`history/v2` 必须从 pending `agent_interactions` 投影 `interrupt={id, reason, payload}`，并与已持久化的 `interaction_requested` 指向同一 interaction id。读取历史会先处理过期 continuation claim：仅 `continuation_started_at` 为空的 pre-start 项可停回 waiting；已持久化 `interaction_resolved` 的 started continuation 必须写 durable `RUN_ERROR` 并收敛 failed。工具审批若已跨过 dispatch 且结果未知，还必须先按 execution lease 收敛 `unknown`，绝不自动重放。

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

| 参数                | 默认值 | 可配置                                | 说明                                |
| ------------------- | ------ | ------------------------------------- | ----------------------------------- |
| `AGENT_MAX_STEPS` | 100    | 是                                    | 单次 Round 最大步数                 |
| 用户并发上限        | 1      | 是 (`AGENT_USER_CONCURRENCY_LIMIT`) | 同一用户可同时运行的不同 session 数 |
| 心跳间隔            | 15s    | 是 (`SSE_HEARTBEAT_INTERVAL`)       | SSE 心跳与锁刷新间隔                |
| 工具超时            | 300s   | 是 (`tool_timeout`)                 | 单个工具执行超时                    |
| Agent 初始化超时    | 180s   | 是 (`AGENT_INIT_TIMEOUT_SECONDS`)   | Sandbox、Skills 与工具目录初始化总墙钟期限 |
| 流式块超时          | 100s   | —                                    | LLM 流式响应相邻 chunk 的最大间隔   |

#### Runtime 工具循环防护

每个 Round 在消息历史之外维护 run-local execution ledger；该 ledger 不参与摘要、provider 投影或 checkpoint，因此压缩不能让防护状态“失忆”。调用身份由稳定 tool ref 与规范化参数摘要组成，不使用每次都会变化的 `tool_call_id`；文件路径按 POSIX 分隔符归一化但保留大小写，审计只保存摘要，不保存敏感原始参数。

- 相同参数、相同错误连续返回 2 次后，第 3 次执行前阻止；中间成功执行不同调用会解除旧恢复态，不同错误视为策略发生变化。
- 只读工具允许“初读 + 复核”，第 3 次仍得到相同结果时阻止；相同成功的 mutating 工具下一次不得重复执行，`outcome_uncertain` 的副作用调用也不得自动重试。
- 完整观察到 `A → B → A → B`，且 A/B 均为相同只读调用、两次结果各自未变化后，下一次再次调用 A 或 B 时识别为无进展循环；不得在尚未观察第二次 B 的结果前预测性阻止。成功 mutation 会清空旧的读/搜索观察。声明为 polling 的工具使用独立有界阈值，但相同 uncertain 结果最多实际尝试 2 次。
- `bash` 命令文本不凭首个单词猜测读写属性；MCP annotation 只能把默认策略收紧，远端自称 `readOnlyHint=true` 不得放宽重试权限。带 `mode` 的记忆工具按具体调用区分 read 与 write/append。
- 文件变更工具 `apply_patch` 额外按规范化路径记账：单目标 Patch 的写入结果不确定后，同一路径的后续文件变更被阻止，直到 `read_file` 得到确定结果；多文件 Patch 的相同调用不得自动重放。
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
2. `checkpoint_v1` 优先读取最新 v4 replacement，并从精确 message/event 游标回放同轮 suffix 与后续 Round；无有效 v4 时从 `rounds` + `conversation_messages` + `agui_events` + `agent_interactions` 重建完整权威历史。只有 `legacy_120` 回滚策略按消息条数裁剪。
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
幂等预检：按 (session_id, idempotency_key) 查已有 Round
    │
    ├── 命中 → 直接 ROUND_IN_PROGRESS，不执行任何副作用
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
         └── running / waiting_interaction → 重定向到 subscribe 模式
              └── 推送 ROUND_IN_PROGRESS 错误事件
```

#### 要求

- `idempotency_key` 由前端生成（UUID v4）
- 唯一约束作用域：`(session_id, idempotency_key)`
- `prepare_chat_round` 必须在任何副作用之前完成幂等预检。Workspace 文件附件落盘会复制文件并分配 uuid 后缀目录，重试若先落盘会留下孤儿快照；源 revision 已推进时还会先抛 `REVISION_CONFLICT` 而不是返回既有 Round。文件夹实时引用只查 DB，不复制正文。
- 预检只覆盖串行重试；相同 key 的真并发仍由唯一约束兜底，输方已落盘的文件 snapshot 由 admission 失败清理链路回收。
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
- 超过 `SSE_SUBSCRIBE_TIMEOUT`（默认 300s）未刷新 → 视为 stale lock；下一次获取用户 slot 时先检查 continuation ownership：仍有效的 continuation claim 或工具 execution lease 保留原 lock/slot；两类 lease 都已过期且 Round 为 `running` 时一律视为已越过 started 边界并收敛 durable failed，不得停回 `waiting_interaction`。过期的 `executing` 工具必须先收敛 `unknown`，再以工具审批保守文案终态化；没有 continuation 的孤儿 running Round 按普通失败终态收敛
- `abort` 统一采用“接口即时收敛”策略：
  1. 写入 append-only 取消审计行（用于本进程 registry 命中状态与诊断）
  2. 若存在 running 或 waiting_interaction Round，原子收敛其 Interaction/未 dispatch 审批并标记 `cancelled`，持久化并 fanout `RUN_FINISHED(outcome=interrupt)`
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
2. terminal producer 只能返回 `RunCompletionService` 在 Round 锁下确认的 committed terminal，不允许把迟到的原始 Agent event 发给 direct SSE
3. 订阅端以权威终态事件为准，不再回跳 `running`

回收 stale `UserRunLock` 时，删除旧锁、收敛其孤儿 Round、分配同一 user 的新锁必须受同一个 ownership mutex/epoch 保护。PostgreSQL 使用 session-level advisory lock 时，获取锁、期间所有 commit 与最终 unlock 必须固定在同一条物理连接；不得让普通 ORM Session 在 commit 后把持锁连接退回连接池。每次分配 slot 前都必须先完成该用户所有无 surviving lock running / waiting Round 的 active-session 分类，不能依赖“本次刚好删除到 stale 行”或 Round UUID 顺序：任一 parent 的 active claim / execution lease 都保护同 session 的 running subagent child，并作为一个虚拟占用计入并发容量。第二阶段才可处理非 active session：waiting pre-start claim 过期时先在 Round 锁下清除 token/lease 并提交 fence；started 或 dispatch-crossed lease 过期时先保守收敛后才能分配。若本次删除的旧锁仍对应 active work，必须恢复原 lock/slot；该恢复只占用原 slot，在 `AGENT_USER_CONCURRENCY_LIMIT > 1` 且仍有空 slot 时不得阻塞同一用户的其他 session。

rolling startup 清理采用同一判定：heartbeat CAS 删除 stale lock 后，若细粒度 continuation / execution lease 仍有效，必须在终态化前原样恢复该 lock/slot；即使锁行已被更早的崩溃清理器删掉，也必须把 active work 视为受保护，而不能按 generic orphan 杀死。waiting pre-start claim 过期先清 token/lease；started / dispatch-crossed lease 过期才允许终态化，且过期 `executing` 工具先转 `unknown` 再写工具审批保守终态。任一 session 在二次检查时发现新鲜锁，只能跳过该 session；不得回滚此前其他 session 已完成的 stale lock 删除或 claim 清理，返回的清理计数必须与已提交事实一致。

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

| 参数                  | 默认值                                  | 说明                                                                     |
| --------------------- | --------------------------------------- | ------------------------------------------------------------------------ |
| `token_limit`       | `(context_window - max_tokens) * 80%` | 先为最大输出预留窗口，再对可用输入预算取 80%；可由模型配置调低，不能调高 |
| `context_window`    | 128,000                                 | 模型上下文窗口大小                                                       |
| `max_output_tokens` | 16,384                                  | 模型最大输出长度                                                         |

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

pre-turn 压缩排除正在进入会话的当前 user，发布 replacement 后再把该消息原样接回。replacement 中保留的历史真实 user 严格按 Codex 只抽取 text block，忽略 image/audio/video 等媒体块，禁止把 data URL 或 base64 序列化为 checkpoint 文本；媒体语义由看过完整历史的 handoff summary 承接。mid-turn 只在仍要继续调用模型时触发，压缩请求能看到完整当前工具批次，但 replacement 不保护该批次，只依赖 handoff summary。工具输出在首次写入历史时按默认 42,667 bytes 配置和 1.2 倍序列化余量做 UTF-8 中间截断，默认正文预算为 51,200 bytes。

压缩 provider 请求必须响应当前 run 的取消信号。取消胜出时中止正在进行的请求并直接结束 run；只有 provider 响应完成且取消仍未发生时，才允许持久化 checkpoint 并发布 replacement。

切换到更小 fallback 前，如原请求达到目标模型的 `(context_window - max_tokens) * 80%` 阈值，先用 primary 压缩；符合 Codex fallback 条件的失败再用目标模型压缩，context overflow 同样逐个删除最旧项。成功后立即写 checkpoint 并重建普通 fallback 请求；不再发生“少几个 token 就本地拒绝且未调用 fallback”。

#### 历史恢复投影边界（_restore_history）

默认 `checkpoint_v1` 的恢复顺序固定为：`checkpoint replacement + source Round 游标后的同轮 suffix + 后续 Round`。消息数不是预算单位，`agent_max_history_messages=120` 只服务 `legacy_120` 回滚开关。

对 `status=completed` 且事件无法重建 assistant 文本的 Round，仍按 `rounds.final_response`、普通 assistant conversation message 的顺序兜底。压缩 replacement 不强保首轮 user。

**Human-in-the-Loop 历史投影**:

- same-Round 通过同一 Round 内的 `interaction_requested` / `interaction_resolved` 重建占位及真实 tool result；不会插入额外 user message。
- 工具审批的最终 `TOOL_CALL_RESULT` 替换预派发占位；已跨 dispatch 且结果未知时不得从历史自动重试。

### 4.6 AG-UI 事件体系

系统生成 22 类标准 AG-UI 事件，分为 7 个分类：

#### 事件分类

| 分类                | 事件类型                          | 说明                                                      |
| ------------------- | --------------------------------- | --------------------------------------------------------- |
| **Lifecycle** | `RUN_STARTED`                   | Agent 执行开始                                            |
|                     | `RUN_FINISHED`                  | Agent 执行完成（含 outcome）                              |
|                     | `RUN_ERROR`                     | Agent 执行出错                                            |
|                     | `STEP_STARTED`                  | 单步执行开始                                              |
|                     | `STEP_FINISHED`                 | 单步执行完成                                              |
| **Text**      | `TEXT_MESSAGE_START`            | 文本消息开始                                              |
|                     | `TEXT_MESSAGE_CONTENT`          | 文本消息 delta（流式）                                    |
|                     | `TEXT_MESSAGE_END`              | 文本消息结束                                              |
| **Thinking**  | `THINKING_TEXT_MESSAGE_START`   | 思考过程开始                                              |
|                     | `THINKING_TEXT_MESSAGE_CONTENT` | 思考过程 delta（流式）                                    |
|                     | `THINKING_TEXT_MESSAGE_END`     | 思考过程结束                                              |
| **Tool**      | `TOOL_CALL_START`               | 工具调用开始                                              |
|                     | `TOOL_CALL_ARGS`                | 工具参数 delta（流式）                                    |
|                     | `TOOL_CALL_END`                 | 工具调用结束                                              |
|                     | `TOOL_CALL_RESULT`              | 工具执行结果                                              |
| **State**     | `STATE_SNAPSHOT`                | 完整状态快照                                              |
|                     | `STATE_DELTA`                   | 状态增量更新（JSON Patch, RFC 6902）                      |
|                     | `MESSAGES_SNAPSHOT`             | 消息列表快照                                              |
| **Custom**    | `heartbeat`                     | 心跳保活                                                  |
|                     | `title_updated`                 | 会话标题更新                                              |
|                     | `interaction_requested`         | 同一 Round 已持久化提问/审批并进入`waiting_interaction` |
|                     | `interaction_resolved`          | 回答已由同一`runId` 的 continuation 接管                |
|                     | `tool_approval_resume`          | 同一 Round 的审批结果即将回填原工具占位                   |
|                     | `workspace_resource_changed`    | 受控工作区 mutation 已提交；raw audit 只做失效信号，成功文件可内嵌结构化助手引用 |
|                     | `assistant_file_referenced`     | Agent 通过 `present_files` 明确展示当前 Session 文件      |
|                     | 其他自定义事件                    | 按需扩展                                                  |

#### Human-in-the-Loop CUSTOM wire schema

```json
{
  "type": "CUSTOM",
  "name": "interaction_requested",
  "value": {
    "interactionId": "interaction-uuid",
    "runId": "original-round-uuid",
    "kind": "user_input | tool_approval",
    "toolCallId": "tool-call-id",
    "payload": {"questions": []}
  },
  "sequence": 12
}
```

```json
{
  "type": "CUSTOM",
  "name": "interaction_resolved",
  "value": {
    "interactionId": "interaction-uuid",
    "runId": "original-round-uuid",
    "toolCallId": "tool-call-id",
    "toolResultContent": "frozen model-visible result",
    "resolution": "answered"
  },
  "sequence": 13
}
```

`interactionId`、`runId`、`kind` 与 `toolCallId` 是关联字段；`payload` 为 kind-specific 结构。`toolResultContent` 是历史重建所需的冻结文本，不得包含解密后的敏感工具参数。两种事件都必须进入 `agui_events` 并参与 sequence 重放；`heartbeat` 和仅传输层的 `stream_accepted` 不是持久化事实。

工具审批 continuation 在最终结果前还会持久化 `CUSTOM tool_approval_resume`，其 `value={toolCallId}` 指向原审批工具调用。历史重建据此让紧随其后的匹配 `TOOL_CALL_RESULT` 替换预派发占位，而不是追加第二份工具结果。该 marker 不创建新 Round，也不是 Interaction 的完成边界；只有匹配的最终 `TOOL_CALL_RESULT` 持久化后，工具审批 Interaction 才转为 `answered`。

#### Workspace resource CUSTOM wire schema

```json
{
  "type": "CUSTOM",
  "name": "workspace_resource_changed",
  "value": {
    "entry_id": "entry-uuid",
    "operation": "UPDATED",
    "path": "reports/result.md",
    "name": "result.md",
    "kind": "file",
    "revision": 2,
    "mutation_id": "mutation-uuid",
    "toolCallId": "tool-call-id",
    "assistant_file_reference": {
      "ref_id": "workspace:entry-uuid:version-uuid",
      "source": "workspace",
      "entry_id": "entry-uuid",
      "version_id": "version-uuid",
      "path": "reports/result.md",
      "workspace_path": "reports/result.md",
      "name": "result.md",
      "revision": "2",
      "size": 42,
      "type": "md"
    }
  },
  "sequence": 21
}
```

该事件只能由成功的 WorkspaceService mutation 产生，必须与 `WorkspaceMutation` 的 entry、revision 和 actor context 对应。失败、后台重试或仅保留 current 的 change set 不得冒充资源更新。普通前端不渲染 raw mutation；只有经过服务端版本保护的 `assistant_file_reference` 才进入 Round 卡片。实时 reducer 与 `history/v2` 使用同一稳定 identity 去重，DELETED 移除同 Round 旧引用；事件不包含文件正文或宿主绝对路径。

Session `assistant_file_referenced` 只能由主 Agent 的 `present_files` 产生，必须携带 `ref_id/source=session/session_id/path/revision/name/size/type/toolCallId`。服务端验证目标是当前 Session 内真实存在的普通文件后持久化当前路径引用，不复制或冻结内容；重新打开时读取该路径的最新内容，文件已删除则提示不可用。`bash`、`bash_output`、`apply_patch` 和助手正文不得自动产生展示引用。

#### ID 生成规则

| ID 类型        | 格式                   | 示例               |
| -------------- | ---------------------- | ------------------ |
| `threadId`   | Session UUID           | `a1b2c3d4-...`   |
| `runId`      | Round UUID             | `e5f6g7h8-...`   |
| `messageId`  | `msg_{runId}_{step}` | `msg_e5f6g7h8_3` |
| `toolCallId` | `tc_{runId}_{step}`  | `tc_e5f6g7h8_3`  |

### 4.7 Human-in-the-Loop 暂停与恢复

Human-in-the-Loop 机制允许 Agent 在执行过程中向用户提问或请求工具审批。默认语义是暂停并继续同一个逻辑 Round。

#### same-Round 暂停流程

```
Agent 调用 ask_user 工具
    │
    ▼
同一事务提交：
  - agent_interactions(status=pending)
  - Round.status = waiting_interaction
  - Round.step_count 记入当前已完成 step（interaction_requested 是其隐式完成边界）
  - CUSTOM interaction_requested
    │
    ▼
结束当前 producer 并释放 UserRunLock；不发 RUN_FINISHED，Round 仍可恢复
```

内存中可以保留模型占位 tool result，但默认 wire 不向前端发送待替换的 `TOOL_CALL_RESULT`。`interaction_requested` 是 UI 显示卡片、订阅重放和当前 step 已完成的权威边界；后续真实 `STEP_FINISHED` 只补齐事件序列，不得把 `step_count` 再加一次。若该事件缺失，history 重建也必须把包含 `interaction_requested` 的 step 投影为 completed。

首条消息在此处暂停仍必须完成标题生成生命周期：waiting 被视为本段流已稳定落库，服务端等待标题任务完成并把 `CUSTOM title_updated` 追加到原 SSE；若原客户端已断开，标题任务不得被取消，仍须落库并向 waiting Round 的现有 subscriber 投递临时标题事件。

#### same-Round 恢复流程

```
用户提交答案 → POST /resume
    │
    ▼
Round → Interaction 固定锁序校验 waiting + pending
    │
    ▼
按问题定义顺序冻结 answer_payload / tool_result_content（重复同答案幂等）
    │
    ▼
continuation 取得可续租 claim；此时 Round 仍 waiting_interaction
    │
    ▼
同一围栏事务：校验 claim + Round → running
             + 持久化 CUSTOM interaction_resolved（runId 仍为原 Round）
    │
    ▼
按 Interaction kind 回填 tool result；继续 Agent loop
claim 完成前所有 durable / ephemeral / terminal 写均须校验同一 token
    ├─ user_input：下一条可恢复的 durable Agent 边界
    │              + Interaction → answered
    └─ tool_approval：匹配原 tool_call_id 的最终 TOOL_CALL_RESULT
                     + Interaction → answered
```

**恢复不变量**：

- 相同键值的多问题答案即使 JSON key 顺序不同也必须幂等；展示/模型文本按原问题定义顺序冻结，不能依赖客户端对象插入顺序。
- `interaction_resolved` 之前的失败释放 claim 并保留/恢复 `waiting_interaction`；客户端回拉 history，不把控制面 `RUN_ERROR` 当作原 Round 终态。
- `interaction_resolved` 一旦与 `Round → running` 原子提交，后续启动或运行异常必须保留 ownership fence，并把原 Round 与 durable `RUN_ERROR` 原子收敛为 failed；不得再释放 claim、回停 waiting 后发送无 sequence 的运行期错误。
- `interaction_resolved` 尚未提交时，过期 continuation claim 可被其他 worker 围栏式回收并恢复 waiting；`interaction_resolved` 已提交后，started claim 过期必须收敛 durable failed，不得重新认领或恢复旧问题卡。若旧 worker 继续输出，token/epoch 守卫必须阻止其写入。
- AG-UI sequence 唯一冲突只能回滚本次 event/fence savepoint；不得撤销同一外层事务中已经 flush 的 Interaction、Round waiting 投影或 synthetic conversation message，再单独提交事件。
- EventBus 因持锁后发现 Round 已终态而拒写时，必须返回专用终态抑制信号；调用方不得把它与正常 ephemeral `None` 混同，不得继续累计正文、写 conversation history、递增 step 或把原事件发给 direct SSE。
- 用户 abort、UserRunLock liveness 丢失和 continuation ownership 丢失是三类独立信号。后两者不得伪装成 `user_cancelled`；旧 worker 必须静默退出并交由 durable recovery 收敛。
- ask_user 回答属于模型可见的真实用户信息，但在 same-Round 模式不新增一个聊天 user bubble；历史通过 requested/resolved 事件重建该 tool result。
- 工具审批的 resolution 是控制决策，不作为 user message；其两阶段执行状态见 [tool-permission-spec.md](tool-permission-spec.md)。

**连续交互规则**：每次新的 `interaction_requested` 替换 pending 卡片，但所有事件继续写入同一个 Round 的递增 sequence；收到新请求后即使网络断开，客户端也必须保留该卡片并通过 subscribe/history 恢复，不得合成失败终态。

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

| 参数              | 值   | 说明                 |
| ----------------- | ---- | -------------------- |
| `max_retries`   | 1    | 每个模型最大重试次数；SDK 内层重试固定关闭 |
| `initial_delay` | 0.5s | 初始重试延迟         |
| `max_delay`     | 30s  | 最大重试延迟         |
| `max_increment` | 1.0s | 每次退避最大增量     |

#### Failover 策略

- **One-shot Failover**: 当 Fallback 模型成功后，下一次 LLM 调用仍优先尝试 Primary 模型
- 不会永久切换到 Fallback 模型

### 4.10 Lazy Agent Init

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

> **设计决策**: SSE 连接先建立再初始化，避免浏览器/网关因等待过久而断开连接。心跳在初始化期间发送，但不得绕过 `AGENT_INIT_TIMEOUT_SECONDS` 无限保活。

### 4.11 Round 状态不变量

**核心不变量**: 每个 Round 最终都必须达到终态；`waiting_interaction` 只是可恢复的 quiescent 暂停态，不是完成态。

#### 实现保证

1. **Finally Block**: producer 正常完成、异常或取消时收敛终态；合法的 Interaction 暂停则保留 `waiting_interaction`，等待回答或 abort 后再终态化。
2. **防止跨 Worker 覆写**: 所有 terminal event 都必须在 Round 锁下经 `RunCompletionService` 完成。若 Round 已处于终态（`completed`/`failed`/`cancelled`/`max_steps_reached`），执行侧只能返回既有 committed terminal 或静默退出，禁止把迟到的原始 Agent terminal 发给 direct SSE。这防止了以下场景：
   - Worker A 执行 Round，被 abort 设为 `cancelled`
   - Worker A 的 finally 块随后执行，尝试将 Round 设为 `completed`
   - 检查发现 Round 已在终态，跳过更新
3. **Resume 竞争窗口处理**: same-Round continuation 使用 interaction claim token 与 run/abort epoch 围栏陈旧 worker；`interaction_resolved` 提交后不得重新显示原 Interaction。
4. **同步收敛唤醒订阅**: abort、startup cleanup、worker-dead 等同步终态写入必须在 durable event 提交后 fanout 到现有 subscriber，不能只落库让连接永远 heartbeat。

---

## 5. 失败模式与错误处理

| 失败场景                                     | 检测方式                                                                                                                                               | 处理策略                                                                                                                                    | 用户感知                                                         |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| **LLM 调用失败**                       | 异常捕获 + 重试耗尽 (`RetryExhaustedError`)                                                                                                          | 发射`RUN_ERROR` 事件，Round 标记 `failed`                                                                                               | SSE 收到错误事件，前端展示错误提示                               |
| **新消息 Agent 初始化失败**            | 初始化异常捕获                                                                                                                                         | 发射`AGENT_INIT_FAILED`，已创建的 Round 标记 `failed`                                                                                   | 前端展示错误                                                     |
| **resume 在 continuation 前失败**      | `interaction_resolved` 尚未持久化即收到 `NO_PENDING_INTERRUPT` / `RESUME_CONFLICT` / `INVALID_INTERACTION_RESPONSE` / `AGENT_INIT_FAILED` 等 | 视为控制面错误，不终态化原 Round；释放 claim/slot并保留权威 waiting 或并发 winner 状态                                                      | 前端回拉 history 后恢复卡片、运行态或既有终态                    |
| **工具执行超时**                       | `tool_timeout=300s`                                                                                                                                  | 工具返回超时错误信息并标记`outcome_uncertain=true`；Agent 继续执行，权限审计记 `unknown` 而非 `failed`                                | Agent 告知用户超时；远端副作用可能已经发生，不应把它当作确定失败 |
| **LLM 返回空响应**                     | 检测空内容                                                                                                                                             | 给予一次 nudge 机会（注入提示重新生成）；连续空响应 →`RUN_ERROR`                                                                         | 首次空响应用户无感知；连续空响应收到错误                         |
| **输出截断**                           | `finish_reason=length`                                                                                                                               | 自动重试一次（调整 prompt 或 context）                                                                                                      | 用户无感知（自动恢复）                                           |
| **SSE 断开**                           | 连接关闭检测                                                                                                                                           | Producer 继续运行在`_active_runners` 中，等待客户端重连通过 subscribe 恢复                                                                | 前端检测断开，自动重连并通过 subscribe 恢复事件流                |
| **Subscribe 基础设施断连**             | 连接关闭/网络错误                                                                                                                                      | 服务端 producer 不因 Web consumer 断开而取消；客户端按最后 sequence 重连 running/waiting Round                                              | 短暂断线自动恢复；无应用级 5 分钟 TIMEOUT                        |
| **same-Round continuation 启动崩溃**   | `interaction_resolved` 尚未提交时 claim 过期或显式释放                                                                                               | 安全回收后停回`waiting_interaction`；工具审批仅允许重放 `requested` / `approved` / 可投影 denial，不重放 `executing`                | 刷新后仍可继续或看到保守 unknown                                 |
| **same-Round continuation 启动后失败** | `interaction_resolved` 已提交，但 queue、Agent 首次迭代或后续运行失败                                                                                | 保留 continuation fence，把原 Round 与 durable`RUN_ERROR` 原子收敛为 failed；不得回停 waiting 后发送 transport error                      | 所有客户端最终看到同一 failed 终态                               |
| **用户主动 abort（任意 worker）**      | `POST /abort` 命中 running / waiting_interaction / init-window                                                                                       | 原子收敛 Round、Interaction 和未 dispatch 审批，释放锁并 fanout terminal；本地 runner 同时收到 cancel token                                 | 释放成功后用户可立即重发                                         |
| **abort 收敛后释放锁失败**             | `POST /abort` 执行到锁释放阶段但 DB 冲突/异常                                                                                                        | 返回`HTTP 503`，不返回 `cancelled` 假成功                                                                                               | 前端保持运行态或提示重试，避免误判“已可重发”                   |
| **abort 后旧 run 迟到输出**            | round 已终态但仍收到后续 AG-UI 事件                                                                                                                    | 持 Round 锁后二次检查并发出专用终态抑制；不入库、不写 conversation history、不累计本地 step，也不向原 direct SSE / subscriber fanout 原事件 | 前端状态保持 cancelled，不再回跳 running                         |
| **DB 锁冲突**                          | 数据库异常捕获                                                                                                                                         | 返回 HTTP 503                                                                                                                               | 前端提示稍后重试                                                 |
| **Worker 死亡**                        | 锁超时检测（>`SSE_SUBSCRIBE_TIMEOUT`）                                                                                                               | abort 接口直接清理锁和 Round 状态                                                                                                           | 用户调用 abort 时得到即时响应                                    |

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
    ├── Round 为 running / waiting_interaction
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

若初始 POST 在客户端收到响应头前断开，是否已创建 Round 属于歧义状态。前端不得重发 POST，而应在固定确认窗口内按原 `idempotency_key` 查询会话历史（当前最多 3 次）：命中 running / waiting_interaction Round 时立即从其 `round_id` 订阅，命中终态 Round 时直接收敛；该确认路径与正常 2xx 响应一样只发出一次 `stream_accepted`。

只有规定的 3 次 history 请求**全部成功**且每次均无匹配 Round，才能确认请求未被接受、触发一次接受前拒绝回调并恢复乐观清空的草稿。只要任一次 history 请求失败，确认窗口耗尽后仍必须保持 `ambiguous`：提示“暂时无法确认请求是否已受理，请刷新页面查看结果”，不得恢复草稿、不得提示重新发送，也不得以新幂等键重发。确定性的接受前 HTTP 4xx/5xx 不进入该歧义分支，可立即按拒绝处理。

用户在收到 POST 响应头前主动取消时，客户端立即中止 POST 并结束本地订阅，不查询 history、不发出接受/拒绝事件，也不恢复已乐观清空的草稿；请求在服务端是否落地仍未知，避免恢复后误重发。用户在等待 history 确认期间取消时，停止后续重试，忽略已在途 history 的迟到结果，不得因该结果订阅 Round 或恢复草稿。两种取消都保留“刷新查看”的安全边界，后续不得自动重发。

---

## 6. 可观测性

### 日志事件

| 日志事件         | 级别    | 包含信息                                                       | 触发时机           |
| ---------------- | ------- | -------------------------------------------------------------- | ------------------ |
| Agent 执行开始   | INFO    | `session_id`, `round_id`, `user_id`, 模型名称            | Round 创建时       |
| Agent 执行结束   | INFO    | `session_id`, `round_id`, `status`, `step_count`, 耗时 | Round 到达终态时   |
| 上下文压缩触发   | INFO    | 压缩级别, 压缩前/后 Token 数                                   | 每次压缩执行时     |
| LLM 重试         | WARNING | 模型名称, 错误信息, 重试次数, 延迟时间                         | 每次重试时         |
| LLM Failover     | WARNING | Primary 模型, Fallback 模型, 原始错误                          | 切换 Fallback 时   |
| 取消请求状态变化 | INFO    | `session_id`, `request_id`, 新状态                         | 每次状态流转时     |
| 工具执行耗时     | DEBUG   | 工具名称,`session_id`, 耗时                                  | 每次工具执行完成时 |
| 心跳发送         | DEBUG   | `session_id`, `round_id`                                   | 每次心跳时         |
| Worker 死亡检测  | WARNING | `user_id`, `session_id`, 锁龄                              | abort 检测到死锁时 |

---

## 7. 非目标

以下功能明确不在本模块范围内，不应在本模块中实现：

| 非目标         | 说明                                                            |
| -------------- | --------------------------------------------------------------- |
| 消息编辑/撤回  | 已发送的消息不支持修改或删除                                    |
| 多轮并行执行   | 每用户严格串行（per-user 锁），不支持同一用户同时运行多个 Agent |
| Token 用量计费 | 不跟踪 Token 消耗用于计费目的                                   |
| 对话分支/分叉  | 不支持从历史消息分叉出新的对话线路                              |
| Agent 间通信   | Sub-Agent 共享沙箱但拥有独立历史，不支持 Agent 间直接消息传递   |
| 客户端工具执行 | 所有工具均在服务端执行，不支持将工具调用下发到客户端            |

工作区文件显式删除后，其 WorkspaceFileVersion 和 round_attachment 引用一起移除，历史投影不再展示该文件引用；不回退已删除条目的历史正文。删除事件携带 DELETED 与 affected_entry_ids。
