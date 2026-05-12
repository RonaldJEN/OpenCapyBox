# 管理后台 API (Admin) — Spec

## 1. 模块职责边界

- 为管理员提供跨用户的全局运维视图。
- 提供概览、Session 监控、用户管理、系统监控等聚合接口。
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
  - `users`: 每个账号的 `user_id`、`username`、`auth_type`、`enabled`、角色（`admin/user`）、状态（`active/idle/running`）、会话/round/token/cron 指标、周/月 token 限额与用量
- 语义：`status=running`、`running_rounds` 与 `summary.running_total` 同时参考 `rounds.status='running'` 和新鲜的 `user_run_locks.updated_at` 心跳；Agent 已持有用户运行锁但尚未写入 running round 时也必须显示为运行中。

### POST /api/admin/users/simple

- Body: `{username, password, enabled?, is_admin?, token_limit_per_week?, token_limit_per_month?}`
- 响应：创建后的用户基础信息。
- 语义：创建本地 simple 用户，密码只保存 hash。

### POST /api/admin/users/ldap

- Body: `{user_id, username?, enabled?, is_admin?, token_limit_per_week?, token_limit_per_month?}`
- 响应：创建后的用户基础信息。
- 语义：创建企业域账号用户，不保存密码；内部 SSO 通过 `user_id` 匹配。

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
- 语义：删除 `auth_users` 账号记录；历史 session / round / LLM 审计记录保留。
- 约束：管理员不能删除当前登录账号。

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

## 5. 失败模式

- 非管理员访问：admin 接口统一返回 403。
- 鉴权缺失或 token 无效：返回 401。

## 6. 可观测性

- 管理端关键指标优先复用现有事实源：`rounds`、`sessions`、`cron_jobs`、`cron_job_runs`、`llm_call_records`。
