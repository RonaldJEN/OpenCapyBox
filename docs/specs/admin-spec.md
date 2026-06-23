# 管理后台 API (Admin) — Spec

## 1. 模块职责边界

- 为管理员提供跨用户的全局运维视图。
- 提供概览、Session 监控、用户管理、沙箱管理、系统监控等聚合接口。
- 不负责业务执行（不创建会话、不驱动对话执行），仅做读侧聚合查询。

## 2. 鉴权与权限

- 所有端点都要求 Bearer Token。
- 所有端点都依赖 `get_current_admin_user`。
- 用户是否为管理员由 `auth_users.is_admin` 决定。`AUTH_ADMIN_USERS` 仅用于首次 bootstrap。

## 3. API 契约

### GET /api/admin/overview

- Query: `days`（1~90，默认 7）
- 响应：
  - `summary`: 用户总数、管理员数、session/round 概览、cron 概览、LLM 调用与 token、平均完成延迟
  - `trends`: 最近 N 天的 `rounds/tokens` 趋势

### GET /api/admin/rounds-tree

- Query: `limit`、`offset`、`status`、`user_id`、`search`
- 响应：按 `session` 聚合的树形数据，结构为 `session -> rounds -> steps`
  - `session` 层：会话维度统计（round 数、tokens、LLM 调用、压缩步数、最近 round 时间）
  - `round` 层：round 状态、耗时、token 与压缩指标
  - `step` 层：来自 `llm_call_records` 的 step 明细。
    - 首屏列表为轻量字段（step 索引、token、延迟、压缩指标、审阅状态等），不包含大文本原文字段。
    - 原文详情（`request_messages`/`response_content` 等）通过 `GET /api/admin/llm-call-records/{llm_record_id}` 按需加载。
    - 轻量字段与详情字段合并后，包含复盘所需的完整输入输出与压缩信息：
    - 消息细节：`request_messages`、`request_tools`、`response_content`、`response_thinking`、`response_tool_calls`、`response_error`
    - 时延与 token：`usage_prompt_tokens`、`usage_completion_tokens`、`usage_total_tokens`、`first_token_latency_s`、`completion_latency_s`
    - 压缩复盘字段：
      - `compaction_triggered`
      - `compaction_pre_tokens` / `compaction_post_tokens` / `compaction_tokens_saved`
      - `compaction_microcompact_compacted_messages`
      - `compaction_summary_generated_count` / `compaction_summary_reused_count` / `compaction_summary_quality_repair_count`
      - `compaction_emergency_truncate_dropped_rounds`
    - 人工审阅字段：`manual_review_status`
    - Token 口径：session / round 层的 `total_tokens` 均为对应范围内 `SUM(llm_call_records.usage_total_tokens)`，用于成本与用量统计；它不是会话累计上下文长度。排查上下文恢复稳定性时，应优先查看 step 层的 `usage_prompt_tokens`、`request_message_count` 与 `compaction_*` 字段。
  - 管理台展示语义（前端约定）：
    - Session 监控默认分页为每页 `5` 条 session，可在页面切换为 `5/10/15`。
    - step 详情默认先展示“管理员分析摘要”（中文），包含请求概览、响应概览、性能与压缩诊断、建议审阅结论。
    - 原始英文键作为“排障证据层”保留展示（例如 `request_messages`、`response_tool_calls`），用于精确定位问题。

### PUT /api/admin/llm-call-records/{llm_record_id}/review

- Body: `{ "manual_review_status": "没问题" | "有问题" }`
- 响应：`llm_record_id` 与最新 `manual_review_status`
- 语义：管理员可对 step 级 LLM 调用记录打标，支持“没问题/有问题”两种状态。

### GET /api/admin/llm-call-records/{llm_record_id}

- 响应：单条 step 的完整详情（含 `request_messages`、`request_tools`、`response_content`、`response_thinking`、`response_tool_calls` 等原文大字段）。
- 语义：用于 Session 监控中的 step 详情按需加载，减少 rounds-tree 首屏响应时间。

### GET /api/admin/users

- 响应：
  - `summary`: 用户总数、管理员数、活跃用户数、运行中用户数
  - `users`: 每个账号的 `user_id`、`username`、`auth_type`、`enabled`、角色（`admin/user`）、状态（`active/idle/running`）、会话/round/token/cron 指标、周/月 token 限额与用量、沙箱 Profile 与 DB 状态
- 语义：`status=running`、`running_rounds` 与 `summary.running_total` 同时参考 `rounds.status='running'` 和新鲜的 `user_run_locks.updated_at` 心跳；Agent 已持有用户运行锁但尚未写入 running round 时也必须显示为运行中。
- 沙箱字段：
  - `sandbox_profile_id`: 用户显式绑定的 Profile ID；`null` 表示走默认 Profile。
  - `sandbox_profile_name`: 当前期望 Profile 名称。
  - `sandbox_profile_source`: `explicit` / `default` / `missing` / `disabled`。
  - `sandbox_profile_error`: 显式绑定缺失或已禁用时返回错误说明；正常配置为 `null`。
  - `sandbox_id`: DB 记录的当前用户 sandbox id。
  - `sandbox_status`: DB 记录状态，非实时 OpenSandbox 探活。
  - `sandbox_needs_recreate`: active Profile 指纹与当前期望 Profile 不一致时为 `true`。

### POST /api/admin/users/simple

- Body: `{username, password, enabled?, is_admin?, token_limit_per_week?, token_limit_per_month?, sandbox_profile_id?}`
- 响应：创建后的用户基础信息。
- 语义：创建本地 simple 用户，密码只保存 hash；传入 `sandbox_profile_id` 时同时建立显式沙箱 Profile 绑定。

### POST /api/admin/users/ldap

- Body: `{user_id, username?, enabled?, is_admin?, token_limit_per_week?, token_limit_per_month?, sandbox_profile_id?}`
- 响应：创建后的用户基础信息。
- 语义：创建 LDAP 用户，不保存密码；统一登录时通过 `user_id` 匹配并使用目录密码验证；传入 `sandbox_profile_id` 时同时建立显式沙箱 Profile 绑定。

### GET /api/admin/sandbox-profiles

- 响应：`{profiles: AdminSandboxProfile[]}`。
- 字段：
  - `id`、`name`、`description`、`department`
  - `domain`、`protocol`、`api_key_set`、`use_server_proxy`
  - `is_default`、`enabled`、`version`
  - `bound_users`
  - `created_at`、`updated_at`
- 语义：列表状态来自 DB，不实时查询 OpenSandbox VM。

### POST /api/admin/sandbox-profiles

- Body: `{name, description?, department?, domain, api_key, protocol?, use_server_proxy?, enabled?}`
- 响应：创建后的 Profile。
- 约束：`name` 唯一；`api_key` 必填且不能为空；`protocol` 仅支持 `http` / `https`。镜像、资源限制、宿主存储根与容器挂载路径均使用全局配置，不按 Profile 自定义。MVP 仅保存 Profile 当前值和 `created_at` / `updated_at`，不保存配置变更审计历史。

### PATCH /api/admin/sandbox-profiles/{profile_id}

- Body: `{name?, description?, department?, domain?, protocol?, api_key?, use_server_proxy?}`。
- 响应：更新后的 Profile。
- 语义：修改连接字段会使 `version + 1`，后续用户 sandbox/Agent 会按新版本重建。MVP 不主动迁移文件或跨 OpenSandbox 后端清理旧 sandbox，旧 sandbox 依赖 OpenSandbox TTL 回收；后续和数据迁移方案一起设计主动清理、连接快照或 Profile revision history。空 `api_key` 表示不改现有密钥；不支持清空密钥；管理端不回显明文密钥，只返回 `api_key_set`。
- 记录：当前仅更新 Profile 行的 `updated_at` 与 `version`，不记录修改人、字段 diff 或历史连接配置。若要切换到新的 OpenSandbox 后端，建议新建 Profile 并重新分配用户，而不是原地修改既有 Profile 的 runtime 连接字段。

### PATCH /api/admin/sandbox-profiles/{profile_id}/default

- 响应：更新后的 Profile。
- 语义：将指定 Profile 设为全局默认；未显式绑定 Profile 的用户下次使用该默认 Profile。禁用 Profile 不能设为默认。

### PATCH /api/admin/sandbox-profiles/{profile_id}/enabled

- Body: `{enabled: bool}`
- 响应：更新后的 Profile。
- 语义：启用/禁用 Profile。默认 Profile 不能禁用。禁用已绑定 Profile 后，相关用户启动新任务会返回 409，管理员应重新分配。

### PATCH /api/admin/users/{user_id}/sandbox-profile

- Body: `{sandbox_profile_id: string|null, force_recreate?: bool}`
- 响应：用户最新沙箱 Profile 摘要。
- 语义：管理员为用户显式绑定非默认 Profile，或传 `null` 恢复使用默认 Profile；传入当前默认 Profile ID 时按 `null` 归一化处理。
- 约束：
  - 目标 Profile 必须存在且启用；默认 Profile 不作为显式绑定保存。
  - 用户有新鲜运行锁或 running cron run 且 `force_recreate=false` 时返回 409；`force_recreate=true` 表示管理员确认强制失效该用户 Agent/Sandbox 并切换。
  - 更新绑定前会失效该用户 AgentPool 缓存，并 best-effort kill 旧 sandbox / 清除旧 `user_sandboxes` 绑定；kill 失败只记录 warning，不阻断配置切换。旧 sandbox 若无法按 active Profile 指纹重新连接，不回退到新 Profile 或 `.env` 连接；容器依赖 OpenSandbox 空闲 TTL 回收，TTL 由 `SANDBOX_TIMEOUT_MINUTES` 控制，默认 60 分钟。
  - MVP 不做 sandbox 文件迁移；DB-backed 记忆文件会在新 sandbox 中重新同步。
- 记录：用户 Profile 分配记录保存在 `user_sandbox_configs`，包含 `updated_by`、`created_at`、`updated_at`，但不保存多版本分配历史。

### PATCH /api/admin/users/{user_id}/enabled

- Body: `{enabled: bool}`
- 语义：启用/禁用用户。禁用后已有 token 也会在下一次鉴权时失效。

### PATCH /api/admin/users/{user_id}/admin

- Body: `{is_admin: bool}`
- 语义：设置或取消管理员权限。

### PATCH /api/admin/users/{user_id}/token-limits

- Body: `{token_limit_per_week: int|null, token_limit_per_month: int|null}`
- 语义：设置用户周/月 token 限额；限额必须为非负整数，`NULL` 表示不限额，`0` 表示禁止发起模型调用。

### POST /api/admin/users/{user_id}/reset-password

- Body: `{password: str}`
- 语义：仅 simple 用户可重置本地密码；ldap 用户返回 400。

### DELETE /api/admin/users/{user_id}

- 响应：`{user_id: str, deleted: true}`
- 语义：不可恢复地删除 `auth_users` 账号记录，并清理该 `user_id` 归属的 session、round、conversation message、AG-UI event、LLM 调用记录、subagent graph edge、channel session binding、cron job / fire / run、memory、embedding、skill 配置、运行锁、取消请求、user_sandboxes 绑定与 sandbox 文件。
- 同名 `user_id` 删除后可以重新创建；新账号不得看到旧用户数据。
- 约束：管理员不能删除当前登录账号；存在新鲜 `user_run_locks.updated_at` 心跳代表的运行中用户任务或运行中的 cron run 时返回 409；sandbox 清理失败时返回 409 且不得删除用户。若待清理 sandbox 已在进程内缓存，可直接使用 live sandbox 对象清理；若需要按 `sandbox_id` 重新连接/恢复，而 active Profile 指纹无法解析或版本不匹配，则视为 sandbox 清理失败。删除用户比切换 Profile 更严格，因为同名 `user_id` 未来可重建，必须避免新账号继承旧持久化文件。

### GET /api/admin/system

- Query: `hours`（1~168，默认 24）
- 响应：
  - round 与 cron 的状态分布
  - LLM 时延（avg/p50/p95）
  - 压缩观测（调用次数、节省 token、质量修复、紧急截断）

## 4. 行为语义与不变量

- 除用户管理接口外，管理端查询仅返回聚合信息，不写业务数据。
- `rounds-tree` 端点按 session 维度分页，session 内 round 仍按 `created_at` 倒序。
- `auth_users` 是后台用户管理的事实源。
- 管理员不能禁用当前登录账号、不能取消自己的管理员权限、不能删除当前登录账号。
- 删除用户是硬删除语义；禁用用户才表示保留账号与数据但禁止登录。

## 5. 失败模式

- 非管理员访问：admin 接口统一返回 403。
- 鉴权缺失或 token 无效：返回 401。
- 删除用户时存在运行中任务、运行中 cron run 或 sandbox 清理失败：返回 409。

## 6. 可观测性

- 管理端关键指标优先复用现有事实源：`rounds`、`sessions`、`cron_jobs`、`cron_job_runs`、`llm_call_records`。
