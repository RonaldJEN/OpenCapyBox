# 工具执行权限 (Tool Permission) — Spec

## 1. 模块职责边界

- 对每一次工具调用裁决 `ALLOW` / `ASK` / `DENY`（内置工具与 MCP 工具统一）
- 权限规则的分层管理：平台（managed）/ 用户 / 会话三级作用域
- `ASK` 命中时的**持久化单次执行审批**（durable single-execution approval）
- 审批的执行租约（lease）、幂等围栏（claim token）与对账（reconciler）
- 追加式审计（append-only audit）
- **不负责**：MCP 连接/发现（属 [mcp-spec.md](mcp-spec.md)）、工具本身的业务逻辑

> 权限域仅以稳定 UUID 引用 MCP 服务；连接/鉴权状态不属于本域，本域只记录「这次调用是否可运行」。

## 2. 数据模型

见 [src/api/models/tool_permission.py](../../src/api/models/tool_permission.py)。

### `tool_permission_rules` — 权限规则
- `scope_type`: `platform` | `user` | `session`；`scope_id`: 对应 user_id / session_id（platform 为 NULL）
- `provider`: `builtin` | `mcp`；`server_id`: MCP 规则必填、builtin 必须为 NULL
- `tool_name`: 精确名或显式通配 `*`；空字符串/纯空白非法。管理端表单初始值为 `*`，但不得把管理员清空后的值静默回退为通配规则
- `effect`: `allow` | `ask` | `deny`
- `priority`: 同特异度下的次级排序（-10000..10000）
- `managed`: `true` = 平台管控规则（天花板语义）
- `conditions_json`: NULL = 无条件；非 NULL 携带 `{version, schema_hash, connection_fingerprint}` 绑定
- `enabled` / `expires_at`: 启停与过期

### `tool_approval_requests` — 审批记录
- `id`: 同时是 AG-UI interrupt id
- `(run_id, tool_call_id)` 唯一（防重复审批）
- `arguments_encrypted` / `result_encrypted`: 参数与结果均加密存储；`arguments_hash` 供审计
- `schema_hash` / `connection_fingerprint`: 审批发起时的工具身份证据（durable）
- `status`: `requested` → `executing` / `denied` → `executed` / `failed` / `unknown`
- `resolution`: `allow_once` | `allow_session` | `allow_always` | `deny`
- `execution_claim_token` + `execution_lease_expires_at`: 执行幂等围栏与租约

### `tool_permission_audits` — 审计（追加式）
- 记录 `effect` / `outcome` / `matched_rule_id` / `arguments_hash` / `reason`
- 主键在 PostgreSQL 用 `BigInteger` 自增，SQLite 用 `Integer`

## 3. API 契约

### 3.1 用户端 `/api/permissions`（需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/rules` | 列出平台 managed 规则（只读）与该用户的非 managed 规则 |
| POST | `/rules` | 创建用户级规则（MCP 需校验服务可访问） |
| PUT | `/rules/selection` | **手动设置某个精确工具**的 ALLOW/ASK/DENY，原子替换该工具的用户级规则（见 §4.6） |
| DELETE | `/rules/selection` | **恢复默认**：单事务删除该精确工具的全部非托管用户规则（见 §4.6） |
| PUT | `/rules/selection/batch` | **批量**对多个精确工具应用同一 effect，单事务原子替换（见 §4.7） |
| PATCH | `/rules/{id}` | 修改 effect/priority/description/enabled/expires_at |
| DELETE | `/rules/{id}` | 删除用户级规则 |
| GET | `/tools` | 工具清单 + 每个工具的当前裁决 `effect` 与 `matched_rule_id` |

- `GET /tools` 汇总内置工具与该用户 installation 作用域内的 MCP 快照工具（均默认 `allow`），并按 §4.1 裁决
- `GET /rules` 中 `managed=true` 的平台规则仅用于展示权限天花板，用户端不得 PATCH/DELETE；用户端只可管理 `scope_type=user`、`scope_id` 为自己的非 managed 规则
- 变更规则后调用 `invalidate_user_async` 让 Agent 池尽快生效（DB `policy_version` 为权威源）

### 3.2 管理员端 `/api/admin/tool-permissions`（需管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `` | 列出全部平台 managed 规则 |
| POST | `` | 创建平台 managed 规则 |
| PATCH | `/{id}` | 修改平台 managed 规则 |
| DELETE | `/{id}` | 删除平台 managed 规则 |

### 3.3 审批解决路径

审批**不经独立 REST 端点**，而是通过 chat 的 resume-interrupt 机制解决：审批 `id` 即 interrupt id，前端在审批卡选择后以 resume payload 携带 `resolution`（`ToolApprovalResolutionPayload`，见 [src/api/schemas/tool_permission.py](../../src/api/schemas/tool_permission.py)）。

审批解决是**控制决策而非用户聊天输入**：它已由 `tool_approval_requests` / interrupt resolution / 审计记录持久化，且获批工具结果在历史重建时拼接回父 round。因此：
- **不**将 `resolution` 持久化为 `role="user"` 会话消息（区别于 `ask_user`，后者答案是真实用户输入）；不会伪装成聊天气泡，也不进入重建后的模型上下文。
- resume round 可保留 `Tool approval: <resolution>` 作为内部可读文本，但**不得依据文本内容识别控制轮次**。历史 API 通过 `InterruptResolution.resume_round_id` 与对应 `ToolApprovalRequest` 的结构化关系返回 `RoundData.control_kind="tool_approval"`；前端仅依据该字段拼接轮次和隐藏控制气泡。用户真实发送同名文本时仍是普通聊天消息。

## 4. 行为语义与不变量

### 4.1 裁决优先级（见 `_evaluate_tool_permission_from_loaded`）
1. **平台 managed DENY**：硬拒绝，最高优先，任何下级不可放开
2. **本地规则（user/session，非 managed）**：先取最高**特异度**，特异度相同再取最**严格** effect
   - 特异度 `_specificity` = `(scope_rank, server_match, tool_match, priority)`；`scope_rank`: platform<user<session
   - 严格度 `_restrictiveness`: allow(1) < ask(2) < deny(3)
3. 无本地规则时：平台 managed ALLOW → allow；否则用 provider 默认（builtin=allow，mcp=allow）
4. **平台 managed ASK 是天花板**：下级可收紧到 DENY，但不可放宽到 ALLOW（ALLOW 会被抬回 ASK）

### 4.2 条件匹配（无效授权不命中，限制规则保守生效）
- `conditions_json` NULL = 无条件适用
- 解析/校验失败：该条件 **ALLOW 规则不参与裁决**，ASK/DENY 仍保守适用，避免损坏的限制规则被意外移除
- 有条件时要求 `schema_hash` 相等；MCP 还要求 `connection_fingerprint` 相等；绑定不匹配的条件 ALLOW 同样不参与裁决
- 条件规则不参与裁决后，最终结果继续由其他适用规则及 provider 默认策略决定；由于 MCP 默认 `allow`，没有其他限制规则时最终仍为 `ALLOW`

### 4.3 审批解决与「记住选择」
- `allow_once`：仅本次执行，不建规则
- `allow_session`：建 **session 作用域** ALLOW 规则
- `allow_always`：建 **user 作用域** ALLOW 规则
- `deny`：状态转 `denied`，不执行
- MCP 的记住选择会写入绑定条件 `{schema_hash, connection_fingerprint}`；仅当当前 installation 的 `server_id` / `execution_fingerprint` 与快照 `schema_hash` / `connection_fingerprint` 全部一致时才建规则（`remember_binding_valid`），否则只执行本次不建规则（fail-closed，避免绑定漂移）

### 4.4 执行幂等与租约
- `claim_approval_request` 用**状态守卫 UPDATE**（`status='requested'` CAS）保证恰有一个执行者从 `requested` 转 `executing`/`denied`
- 执行者持 `execution_claim_token` + 租约；`finish_approval_request` 需 `with_for_update` + claim token 匹配才可完成
- 租约过期**不授权重试**（远程副作用可能已发生）；reconciler 将过期 `executing` 置为 `unknown`
- 终态：`executed`（成功）/ `failed`（失败）/ `unknown`（结果不确定或租约过期）
- Agent 层单工具超时同样视为结果不确定：`ToolResult.outcome_uncertain=true`，权限审计记 `unknown`，不得误记为确定失败

### 4.5 暴露语义
- `DENY` 的工具**不向模型暴露**（连 schema 都不给）
- `ASK` 暴露但执行前中断征求确认
- `ALLOW` 直接执行

### 4.6 手动设置（`PUT /rules/selection` → `replace_user_tool_selection`）
- 用户在权限设置里对某个**精确工具**选 ALLOW/ASK/DENY，语义是对该工具**用户级规则的原子替换**：
  - 在同一事务内，先删除当前用户、相同精确 `provider`/`server_id`/`tool_name` 的**全部非托管用户规则**（含此前审批产生的、绑定工具版本的条件授权），再创建**一条无条件、永久、启用**的用户规则。
  - 删除范围**不含**：平台规则、会话规则、其他用户规则、通配符（`*`）规则、其他工具规则。
  - 失败整体回滚，成功后刷新该用户 Agent 权限缓存。
- 用户手动设置的优先级高于其此前针对同一精确工具产生的所有用户级审批授权：手动一次即清掉残留的条件授权。
- PostgreSQL 在任何删除/插入前，按 `user_id + provider + server_id + tool_name` 获取事务级 `pg_advisory_xact_lock`；锁随事务提交/回滚自动释放，使首次选择（尚无规则行可锁）及恢复默认也能跨 worker 串行化。
- 「审批授权 · 绑定工具版本」标签只在 `tool.matched_rule_id` 指向一条**当前生效的用户级条件授权**时显示；历史存在但未命中（如工具版本已变）的条件规则不再显示该标签。
- **恢复默认（`DELETE /rules/selection` → `clear_user_tool_selection`）**：在单一事务内删除该用户对该精确工具的**全部非托管用户规则**（同§4.6 删除范围），一次性完成物理清理并失效 Agent 权限缓存。前端**不得**用多个独立 DELETE 逐条删除（那会在部分失败时留下脏状态）。该端点**不重新校验 MCP 可访问性**，以便用户清理已删除/未发布服务的残留规则。
- **不做启动期数据迁移**：已有残留条件规则先由命中判断隐藏标签，用户下次手动修改或恢复默认（删除该工具全部用户规则）时完成物理清理。

### 4.7 批量设置（`PUT /rules/selection/batch` → `replace_user_tool_selections`）
- 对传入的多个精确工具应用**同一** effect，每个工具采用与 §4.6 相同的原子替换语义；所有 delete/insert 共一个事务，任何失败整体回滚，不留部分状态。
- 批量操作先去重锁身份，并按稳定的 `(provider, server_id, tool_name)` 顺序获取全部 advisory lock 后再开始写入，避免两个交叉批量请求因锁顺序不同而死锁；响应规则顺序仍保持请求顺序。
- 路由先对所有 MCP 项做访问校验，全部通过后才开始变更；任一项不可访问则 404 且不改动任何规则。
- `items` 不得重复（同 provider/server_id/tool_name），上限 500 项；重复或超限返回 422。
- **平台策略天花板由前端处理**：前端在发送前跳过被 managed ASK/DENY 天花板挡住、无法放宽到目标 effect 的工具（与单个操作时按钮禁用一致），并提示「N 个被平台策略跳过」；后端只对收到的项应用。

## 5. 配置项

见 [src/api/config.py](../../src/api/config.py)：
- `tool_approval_execution_lease_seconds`（默认 120）
- `tool_approval_lease_heartbeat_seconds`（默认 30）
- `tool_approval_reconcile_interval_seconds`（默认 30）

## 6. 失败模式

| 场景 | 行为 |
|---|---|
| 并发解决同一审批 | CAS 只放一个成功，其余抛「already resolved」 |
| 执行中 worker 崩溃 | 租约到期后 reconciler 置 `unknown`，不自动重试 |
| 完成时 claim token 不匹配 | 拒绝完成（陈旧 worker 被围栏挡下） |
| 规则条件损坏或绑定不匹配 | 条件 ALLOW 不参与裁决，ASK/DENY 保守生效；最终结果按其他适用规则及 provider 默认策略计算 |
| MCP 服务被删但审批未决 | 保留原始身份为证据，执行前重新校验归属/可用性 |
| 记住选择时绑定已漂移 | 只执行本次，不建规则 |
| 手动设置无权访问/已停用的 MCP | selection 接口拒绝（404），不改动任何规则 |
| 手动设置事务中途失败 | 整体回滚，旧规则完整保留，无部分新状态 |
| 同一用户并发设置/恢复同一精确工具 | PostgreSQL 事务级 advisory lock 串行执行，提交后保持单一手动选择语义 |
| 批量设置含不可访问 MCP / 重复项 | 404 / 422，不改动任何规则 |

## 7. 测试锚点

- [tests/test_tool_permissions.py](../../tests/test_tool_permissions.py)：裁决优先级、条件匹配、审批状态机、租约与对账，以及真实 PostgreSQL 双事务对 advisory lock 阻塞与提交后放行的回归验证
- [tests/test_tool_exposure.py](../../tests/test_tool_exposure.py)：DENY 不暴露、ASK/ALLOW 暴露语义
