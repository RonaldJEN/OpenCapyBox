# 管理后台 API (Admin) — Spec

## 1. 模块职责边界

- 为管理员提供跨用户的全局运维视图。
- 提供概览、Session 监控、用户管理、沙箱管理、模型目录/权限、操作日志、系统监控等聚合与管理接口。
- 不负责业务执行（不创建会话、不驱动对话执行）；配置与账号类写操作仅改变管理事实源。
- 对管理员接口向管理员披露或处理数据的事实进行后端权威审计，不依赖浏览器点击事件。

## 2. 鉴权与权限

- 所有端点都要求 Bearer Token。
- 所有端点都依赖 `get_current_admin_user`。
- 用户是否为管理员由 `auth_users.is_admin` 决定。`AUTH_ADMIN_USERS` 仅用于首次 bootstrap。
- `/admin/login` 只是独立的前端登录入口，复用统一 `/api/auth/login` 与 Bearer Token，不创建管理员专用身份、密码或 LDAP 流程；后端管理员接口仍以 `get_current_admin_user` 作为最终权限边界。
- 管理动作必须显式声明稳定动作编码和审计等级。L1～L3 在管理员鉴权成功后、业务处理前持久化 `started`；写入失败返回 503 且不执行管理动作。L0 常规读取不写入 `admin_operation_logs`，仅进入短期 HTTP/应用日志。

## 3. API 契约

### GET /api/admin/overview

- Query: `days`（1~90，默认 7）
- 响应：
  - `summary`: 用户总数、管理员数、session/round 概览、cron 概览、LLM 调用与 token、平均完成延迟
  - `trends`: 最近 N 天的 `rounds/tokens` 趋势

### GET /api/admin/rounds-tree

- Query: `limit`、`offset`、`status`、`user_id`、`search`。`status` 允许 `all` 及完整 Round 状态：`running`、`waiting_interaction`、`completed`、`failed`、`cancelled`、`max_steps_reached`。
- 响应：按 `session` 聚合的分页列表，首屏不包含 round 树。
  - 顶层：`total_sessions`、`offset`、`limit`、`sessions`
  - `session` 层：`session_id`、`user_id`、`session_title`、`rounds_count`、`last_round_at`、`sum_step_count`、`total_tokens`、`llm_calls`、`error_calls`、`compaction_steps`、`total_duration_s`、`status`
  - Session `status` 按匹配 Round 集合投影，优先级固定为 `running > waiting_interaction > error > completed`。其中 `failed/cancelled/max_steps_reached` 聚合为 `error`；`completed` 在不存在更高优先级状态时聚合为 `completed`。因此含 pending `waiting_interaction` 的 Session 不得显示为 `completed`。
  - `rounds_loaded=false` 且 `rounds=[]`，表示需要通过 `GET /api/admin/sessions/{session_id}/rounds` 懒加载。
  - `total_duration_s` 为匹配 round 的耗时总和：已完成 round 使用 `completed_at - created_at`，未完成 round 使用当前时间近似。
  - Token 口径：session 层 `total_tokens` 为对应范围内 `SUM(llm_call_records.usage_total_tokens)`，用于成本与用量统计；它不是会话累计上下文长度。
  - 管理台展示语义（前端约定）：
    - Session 监控默认分页为每页 `5` 条 session，可在页面切换为 `5/10/15`。
    - 状态筛选必须提供上述完整 Round 状态集合，不能遗漏 `waiting_interaction` 或 `max_steps_reached`。
    - 筛选条件变化后前端必须折叠已展开 session，再按需重新加载当前列表中的 session rounds。

### GET /api/admin/sessions/{session_id}/rounds

- Query: `status`、`search`；`status` 取值与 `/rounds-tree` 相同。
- 响应：单个 Session 下的 round 列表与轻量 step 明细。
  - `session_id`
  - `rounds`: round 状态、耗时、token 与压缩指标、主/子 Agent 元数据、轻量 step 列表。
  - step 轻量字段来自 `llm_call_records`：step 索引、token、延迟、压缩指标、审阅状态等，不包含大文本原文字段。
  - 原文详情（`request_messages`/`response_content` 等）通过 `GET /api/admin/llm-call-records/{llm_record_id}` 按需加载。
  - 轻量字段与详情字段合并后，包含复盘所需的完整输入输出与压缩信息：
    - 消息细节：`request_messages`、`request_tools`、`response_content`、`response_thinking`、`response_tool_calls`、`response_error`
    - 时延与 token：`usage_prompt_tokens`、`usage_completion_tokens`、`usage_total_tokens`、`first_token_latency_s`、`completion_latency_s`
    - 压缩复盘字段：`call_kind`、`checkpoint_id`、`compaction_triggered`、`compaction_pre_tokens` / `compaction_post_tokens` / `compaction_tokens_saved`；旧 microcompact/quality-repair/emergency 字段只保留兼容且固定为 0
    - 人工审阅字段：`manual_review_status`
  - 管理台 step 详情默认先展示“管理员分析摘要”（中文），原始英文键作为“排障证据层”保留展示。

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

### POST /api/admin/users/export

- Body：`{user_ids: string[]}`，ID 不得为空或重复，最多 10000 个。
- 响应：后端按请求顺序重新查询用户并生成 UTF-8 CSV；任一用户不存在时返回 404。
- 语义：前端只提交当前可见用户 ID，不在浏览器本地拼接敏感用户数据；审计元数据仅保存导出人数。

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
- 约束：`name` 唯一；`api_key` 必填且不能为空；`protocol` 仅支持 `http` / `https`。镜像、资源限制、宿主存储根与容器挂载路径均使用全局配置，不按 Profile 自定义。Profile 表仅保存当前值；中央管理员操作日志记录变更字段名和密钥是否变化，但不保存连接值或密钥内容。

### PATCH /api/admin/sandbox-profiles/{profile_id}

- Body: `{name?, description?, department?, domain?, protocol?, api_key?, use_server_proxy?}`。
- 响应：更新后的 Profile。
- 语义：修改连接字段会使 `version + 1`，后续用户 sandbox/Agent 会按新版本重建。MVP 不主动迁移文件或跨 OpenSandbox 后端清理旧 sandbox，旧 sandbox 依赖 OpenSandbox TTL 回收；后续和数据迁移方案一起设计主动清理、连接快照或 Profile revision history。空 `api_key` 表示不改现有密钥；不支持清空密钥；管理端不回显明文密钥，只返回 `api_key_set`。
- 记录：Profile 行更新 `updated_at` 与 `version`；中央管理员操作日志记录操作者、目标与脱敏后的变更字段，不保存历史连接配置。若要切换到新的 OpenSandbox 后端，建议新建 Profile 并重新分配用户，而不是原地修改既有 Profile 的 runtime 连接字段。

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

### GET /api/admin/models

- 响应：`{models, settings}`。
- `models` 字段：`id`、`name`、`provider`、`api_base`、`model_name`、`max_tokens`、`context_window`、`auto_compact_token_limit`、`tool_output_truncation_bytes`、`reasoning_format`、`reasoning_split`、`enable_thinking`、`thinking_mode`、`thinking_wire_format`、`reasoning_effort`、`default_reasoning_level`、`supported_reasoning_efforts`、`supports_thinking`、`supports_image`、`max_images`、`supports_video`、`max_videos`、`enabled`、`tags`、`api_key_set`、`group_names`、`session_count`、`created_at`、`updated_at`。
- `settings` 字段：`default_model_id`、`cron_default_model_id`、`subagent_default_model_id`。

### POST /api/admin/models

- Body：完整模型配置（`model_id`、`display_name`、`provider`、`api_base`、`api_key`、`model_name`、token 窗口、reasoning、多模态能力、`enabled`、`tags`）。
- 语义：创建 DB 模型目录项，使用 `ModelConfig` 校验配置；成功后 reload model registry。
- 约束：`supported_reasoning_efforts` 最多 20 项，每项去除首尾空白后必须非空且不超过 40 字符；重复等级按首次出现位置去重，数据库、管理 API 与运行时目录必须使用同一份规范化结果；非 OpenAI provider 的 `thinking_wire_format` 在落库前强制归一化为 `none`。
- 失败：字段类型、数量、长度或空项等请求结构错误返回 422；通过结构校验但违反 `ModelConfig` 跨字段/协议不变量返回 400；重复 `model_id` 返回 409。

### PATCH /api/admin/models/settings

- Body：`{default_model_id, cron_default_model_id?, subagent_default_model_id?}`。
- 语义：更新普通对话、Cron、Subagent 默认模型；Cron/Subagent 为空时归一化为普通默认模型。
- 约束：默认模型必须存在且启用；成功后 reload model registry。

### PATCH /api/admin/models/{model_id}

- Body：模型配置增量字段；`api_key=null` 或缺省表示保留旧密钥。
- 语义：更新模型目录项并 reload model registry。
- 约束：推理等级列表遵循与创建相同的规范化和长度规则；provider 变为非 OpenAI 时 `thinking_wire_format` 立即归一化为 `none`；停用模型会从所有模型权限包中移除；请求结构错误返回 422，`ModelConfig` 跨字段/协议校验失败返回 400，模型不存在返回 404。

### DELETE /api/admin/models/{model_id}

- Query：`replacement_model_id?`
- 响应：`{model_id, deleted, replacement_model_id, sessions_reassigned, defaults_reassigned}`。
- 语义：删除模型目录项并 reload model registry。
- 约束：
  - 替换模型不能是当前模型，必须存在且启用。
  - 若模型正在作为普通/Cron/Subagent 默认模型使用，且未提供替换模型，返回 400。
  - 若历史 Session 使用该模型，且未提供替换模型，返回 409。
  - 提供替换模型时，迁移默认模型设置、`sessions.model_id` 与模型权限包绑定，再删除旧模型。

### GET /api/admin/model-permission-groups

- 响应：`{groups}`，包含默认权限包与自定义权限包。
- 默认权限包自动应用给所有普通用户，`bound_users` 为当前用户总数；自定义权限包 `bound_users` 为手动绑定用户数。

### POST /api/admin/model-permission-groups

- Body：`{name, description?}`。
- 语义：创建非默认模型权限包；名称唯一。

### PATCH /api/admin/model-permission-groups/{group_id}

- Body：`{name?, description?}`。
- 语义：更新权限包元信息；默认权限包不能重命名。

### PUT /api/admin/model-permission-groups/{group_id}/models

- Body：`{model_ids: string[]}`。
- 语义：整体替换权限包包含的模型。
- 约束：模型必须存在且启用；停用模型不能加入权限包。

### PUT /api/admin/model-permission-groups/{group_id}/users

- Body：`{user_ids: string[]}`。
- 语义：整体替换某权限包绑定的用户；默认权限包不得通过该接口手动绑定。

### PUT /api/admin/users/{user_id}/model-permission-groups

- Body：`{group_ids: string[]}`。
- 语义：整体替换用户额外绑定的模型权限包；默认权限包自动应用，不得出现在 `group_ids` 中。

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

### GET /api/admin/operation-logs

- Query：`from?`、`to?`、`action?`、`risk_level?`、`target_user_id?`、`session_id?`、`outcome?`、`cursor?`、`limit?`；`risk_level` 仅接受 `high` / `normal`，并与其他条件取交集。
- 默认范围：最近 24 小时；`limit` 默认 50、最大 200。
- 分页：按 `(started_at, id)` 倒序游标分页，响应为 `{items, next_cursor, has_more}`。
- 结果：每项包含管理员、稳定动作编码、派生风险级别、目标标识、Session/Step 标识、结果、HTTP 状态、来源 IP、User-Agent、Request ID、脱敏变更字段及脱敏补充信息。
- `audit_log.list` 属于 L1，每次查看或刷新操作日志都记录一次敏感查阅；本次请求对应的 `started` 行不进入本次查询结果，完成后可在后续查询中看到。

### GET /api/admin/operation-logs/export

- Query：与日志列表相同的筛选条件，不接受分页参数。
- 响应：UTF-8 CSV；超过 50000 条时返回 400，要求缩小筛选范围。
- 导出本身记录为 `audit_log.export`，审计元数据仅保存导出条数及是否使用风险级别筛选，不保存 CSV 内容。

稳定动作编码按模块分组：

- 概览与系统：`overview.read`、`system.read`。
- Session/Step：`session.list`、`session.search`、`session.view`、`step.view`、`step.review.update`。
- 用户：`user.list`、`user.login_history.view`、`user.create`、`user.enabled.update`、`user.admin.update`、`user.token_limits.update`、`user.model_groups.update`、`user.password.reset`、`user.delete`、`user.export`。
- 沙箱：`sandbox.list`、`sandbox.create`、`sandbox.update`、`sandbox.default.set`、`sandbox.enabled.update`、`user.sandbox.update`。
- 模型与权限包：`model.list`、`model.create`、`model.update`、`model.delete`、`model.settings.update`、`model_group.list`、`model_group.create`、`model_group.update`、`model_group.models.update`、`model_group.users.update`。
- MCP 与工具权限：`mcp.list`、`mcp.create`、`mcp.update`、`mcp.delete`、`mcp.test`、`mcp.personal_network_policy.list`、`mcp.personal_network_policy.update`、`tool_permission.list`、`tool_permission.create`、`tool_permission.update`、`tool_permission.delete`。
- 操作日志：`audit_log.list`、`audit_log.export`。

审计等级固定按动作编码派生，不写入业务正文或新增可变等级字段：

- L0 常规读取：`overview.read`、`system.read`、`sandbox.list`、`model.list`、`model_group.list`、`mcp.list`、`tool_permission.list`。成功或失败均不写入操作审计表。
- L1 敏感查阅：`session.list`、`session.search`、`session.view`、`user.list`、`user.login_history.view`、`audit_log.list`、`mcp.personal_network_policy.list`。
- L2 管理操作：除 L0、L1、L3 外的创建、更新、删除、重置、导出和外联测试动作，包括 `step.review.update` 与 `mcp.test`。
- L3 高危：仅 `step.view`，表示后端向管理员披露了用户会话步骤原文。

查询接口的兼容风险字段由上述等级派生：`high` 仅匹配 L3 的 `step.view`，`normal` 匹配其他已存操作日志。`/rounds-tree` 的普通列表与带非空 `search` 的查询分别在业务执行前确定为 L1 `session.list` 与 `session.search`。

### GET /api/admin/system

- Query: `hours`（1~168，默认 24）
- 响应：
  - round 与 cron 的状态分布
  - LLM 时延（avg/p50/p95）
  - 压缩观测（调用次数、节省 token、质量修复、紧急截断）
  - `database`: DB 运行态诊断。
    - `pool`: SQLAlchemy pool 类型、状态、size/checkin/checkout/overflow、配置的 pool size / max overflow / timeout / recycle、数据库名。
    - `activity`: 当前数据库 `pg_stat_activity` 按 state/wait event 聚合。
    - `blocked_locks`: 未授予锁数量。
    - `long_queries`: 超过 30 秒的 active query 样本（最多 20 条）。
    - 若数据库运行态查询失败，返回 `database.error`，但保留 pool 基础信息。

## 4. 行为语义与不变量

- 除用户、沙箱 Profile、模型目录/权限等配置管理接口外，管理端查询仅返回聚合信息，不写业务数据。
- `rounds-tree` 端点按 session 维度分页，round 与 step 通过 `/sessions/{session_id}/rounds` 按需加载；session 内 round 按 `created_at` 倒序。
- 同一 round 内的 LLM step 按调用记录的 `created_at, id` 正序返回；`step_index` 只表示普通步骤或压缩调用身份，负数压缩索引不得用于时间排序。
- `auth_users` 是后台用户管理的事实源。
- 管理员不能禁用当前登录账号、不能取消自己的管理员权限、不能删除当前登录账号。
- 删除用户是硬删除语义；禁用用户才表示保留账号与数据但禁止登录。
- `llm_models` 与 `llm_model_settings` 是运行时模型目录与默认模型的事实源；管理端模型写操作成功后必须 reload registry。
- 模型权限包变更是用户可见模型列表的事实源；普通用户只能看到默认权限包 + 额外绑定权限包中的启用模型。
- 每个 L1～L3 管理请求对应 `admin_operation_logs` 一行，终态为 `succeeded` 或 `failed`；终态更新失败时保留 `started`，页面显示“中断 / 结果未知”。L0 请求不创建审计行。
- 会话审计只到 Session 与 Step：展开 Session 记录 `session_id` 和返回 round 数量，查看 Step 原文记录 `session_id` 与 `step_record_id`，不生成 Round 级日志。
- 日志不设置指向用户、Session 或 Step 的级联外键；业务数据硬删除后审计证据继续保留。管理端不提供日志编辑或删除接口。
- 搜索词、会话标题、Prompt、回答、Thinking、工具参数、密码、API Key、Token、Cookie 与 Authorization 不得写入操作日志。

## 5. 失败模式

- 非管理员访问：admin 接口统一返回 403。
- 鉴权缺失或 token 无效：返回 401。
- 删除用户时存在运行中任务、运行中 cron run 或 sandbox 清理失败：返回 409。
- 删除模型时缺少必要替换模型：默认模型占用返回 400，历史 Session 占用返回 409。
- 模型权限包写入不存在或停用模型：返回 400。
- L1～L3 操作日志开始或终态持久化失败：返回 503；开始写入失败时业务处理不得执行。L0 不依赖操作审计表可用性。

## 6. 可观测性

- 管理端关键指标优先复用现有事实源：`rounds`、`sessions`、`cron_jobs`、`cron_job_runs`、`llm_call_records`。
- L1～L3 管理员操作日志不设置自动过期清理；如需删除，必须通过经过单独审批和留痕的数据治理流程执行。
