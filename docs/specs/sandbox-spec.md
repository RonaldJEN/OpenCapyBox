# 沙箱交互 (Sandbox) — Spec

## 1. 模块职责边界

- OpenSandbox 生命周期管理（创建、连接、恢复、暂停、销毁、续期）
- 沙箱缓存（per-user 内存缓存）
- 多 OpenSandbox 后端 Profile 解析与路由
- Skills 文件推送
- 用户自定义 Skill 发现
- 路径安全校验
- 不负责：沙箱内命令执行逻辑（由 Agent 工具负责）、OpenSandbox VM 网络白名单变更、跨 VM 文件自动迁移

## 2. 数据模型

### user_sandboxes 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, UNIQUE, indexed |
| sandbox_id | String(100) | nullable |
| active_profile_id | String(36) | nullable, indexed |
| active_profile_version | Integer | nullable |
| status | String(20) | default="active". Values: active/paused |
| created_at | DateTime | default=now |
| updated_at | DateTime | default=now, onupdate=now |

一个用户最多一个沙箱（UNIQUE 约束）。

`active_profile_id` / `active_profile_version` 记录该 sandbox 实际创建或恢复时使用的 Profile。管理员调整用户 Profile 或修改 Profile runtime 字段后，当前期望 Profile 与 active Profile 不一致时，缓存与 Agent 会被判定为 stale，并在下一次使用时重建。

### sandbox_profiles 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| name | String(100) | NOT NULL, UNIQUE, indexed |
| description | Text | nullable |
| department | String(100) | nullable |
| domain | String(255) | NOT NULL |
| protocol | String(10) | default="http" |
| api_key | Text | nullable（兼容存量/启动默认值；管理端创建 Profile 必填） |
| use_server_proxy | Boolean | default=true |
| is_default | Boolean | indexed |
| enabled | Boolean | indexed |
| version | Integer | default=1 |
| created_at / updated_at | DateTime | default=now |

一个 Profile 对应一个 OpenSandbox 后端/VM。系统启动时会根据 `.env` OpenSandbox 配置确保存在一个默认 Profile。

### user_sandbox_configs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, UNIQUE, indexed |
| sandbox_profile_id | String(36) | nullable, indexed |
| updated_by | String(100) | nullable |
| created_at / updated_at | DateTime | default=now |

`sandbox_profile_id = null` 表示用户走全局默认 Profile；非空表示管理员显式分配非默认 Profile。默认 Profile ID 不作为显式绑定保存，传入时归一化为 `null`。

## 3. API 契约

沙箱模块没有独立 API 端点，作为内部服务被 sessions/chat/cron 路由调用。

对外暴露的接口通过 SandboxSessionService 类：

- `create(user_id) -> Sandbox`
- `get_or_resume(user_id, sandbox_id) -> Sandbox` — 级联恢复: 内存缓存 -> 查询状态 -> connect/resume -> fallback create
- `get_or_resume_with_persisted_id(user_id, sandbox_id) -> tuple[Sandbox, str|None]`
- `pause(user_id) -> bool`
- `kill(user_id, sandbox_id) -> bool` — 先清理 mount 目录（rm -rf），再 kill 容器；用户删除路径必须在硬删除账号前调用
- `renew(user_id) -> bool`
- `get_mount_path(user_id=None) -> str` — 全局统一容器挂载路径
- `get_current_profile_fingerprint(user_id) -> tuple[str, int]`
- `get_cached_profile_fingerprint(user_id) -> tuple[str|None, int|None]`
- `cached_is_current(user_id, sandbox_id=None) -> bool`
- `push_skills(user_id, skills_dir) -> bool` — 批量上传（排除 node_modules, __pycache__, .git, .venv）
- `push_skill(user_id, skills_dir, skill_name) -> bool` — 单个推送，幂等（跟踪已推送集合）
- `discover_sandbox_skills(user_id, official_names) -> list[dict]`
- `read_sandbox_skill_content(user_id, skill_dir) -> str|None`

## 4. 行为语义与不变量

### 单例模式

- SandboxSessionService 通过 `__new__` 实现类级单例
- 内部缓存 `_cache: dict[str, Sandbox]` (user_id -> Sandbox)
- 内部缓存同步记录 Profile 指纹：`_cache_profile_ids`、`_cache_profile_versions`、`_cache_mount_paths`
- 已推送技能跟踪 `_pushed_skills: dict[str, set[str]]`

### 生命周期级联

get_or_resume 的恢复链：

1. 解析用户有效 Sandbox Profile（显式绑定优先，否则默认 Profile）
2. 内存缓存命中 + sandbox_id/Profile 指纹匹配 + 健康检查通过 -> 返回
3. 持久化 active Profile 指纹匹配时才查询旧沙箱状态 -> Running: connect / Paused: resume / 其他: create；指纹不匹配直接创建新 sandbox
4. 所有路径失败 -> fallback create（新沙箱）

创建、连接、恢复使用当前有效 Profile 的 `domain/protocol/api_key/use_server_proxy` 连接 OpenSandbox。按既有 `sandbox_id` 执行 kill/清理时，若进程内仍有 live cached sandbox 对象，可以直接使用该对象清理；若需要根据 `sandbox_id` 重新连接/恢复，则只能使用 `user_sandboxes.active_profile_id` + `active_profile_version` 对应的 Profile。此时若指纹缺失、Profile 不存在、版本已变化或 DB 查询失败，`kill()` 必须返回不可清理，不得回退到当前用户有效 Profile 或 `.env` 默认后端。调用方必须按业务风险处理该返回值：删除用户路径必须阻断；管理员切换用户 Profile 路径允许继续切换并依赖 OpenSandbox TTL 回收旧 sandbox。管理端创建 Profile 时 `api_key` 必填，不支持无 key 后端；镜像、资源限制、宿主存储根和容器 `mount_path` 使用全局配置，不按 Profile 自定义。

MVP 说明：Profile runtime 字段、默认 Profile 变更或用户 Profile 重新分配导致旧 sandbox 指纹过期时，系统在下次使用时按新 Profile 重建，不主动跨 OpenSandbox 后端迁移旧文件。若旧 sandbox 未命中 live cache 且无法按 active Profile 指纹重新连接，系统不尝试回退连接；旧 sandbox 依赖 OpenSandbox 空闲 TTL 回收，TTL 由 `SANDBOX_TIMEOUT_MINUTES` 控制，默认 60 分钟。当前版本不保存 sandbox 创建时的连接快照或 Profile revision history。后续若实现跨 Profile 数据迁移，应同步设计旧 sandbox 的主动清理、连接快照和失败回滚方案。

运维建议：需要切换到新的 OpenSandbox 后端时，应新建 Profile 并重新分配用户，不应原地修改既有 Profile 的 runtime 连接字段来表达换后端。

### Sandbox Profile 版本

以下字段改变会使 `sandbox_profiles.version + 1`，并触发现有 sandbox/Agent 判定为需要重建：

- `domain`
- `protocol`
- `api_key`
- `use_server_proxy`

名称、描述、部门、启用状态、默认标记不改变 version。

Profile 配置更新仅保留当前行的 `updated_at` 和 `version`，MVP 不记录修改人、字段 diff 或历史连接配置。用户 Profile 分配当前记录包含 `updated_by`、`created_at`、`updated_at`，但不保存多版本分配历史。

### sandbox_id 持久化（fallback create 路径）

- `get_or_resume` 走到 fallback create 后会自动调用 `_persist_sandbox_id_if_exists(user_id, new_id, previous_id=...)`：
  - 仅 `update` 已存在的 `user_sandbox` 行（同时把 `status` 重置为 `active` 并写入 active Profile 指纹）
  - 不主动 `INSERT`（首次创建路径由 `sessions` / `agent_pool_service` 显式写入）
  - `new_id == previous_id` 时短路返回，不触发任何 DB 操作
- 目的：避免调用方持有失效旧 id → 反复 fallback create → 沙箱泄漏。
- `get_or_resume_with_persisted_id` 会在同一 user lifecycle lock 内完整 upsert `user_sandbox`；调用方若使用裸 `get_or_resume`，首次创建或 cron 无记录场景仍需显式插入/回写当前 `sandbox_id` 与 active Profile 指纹。

### AgentPool sandbox 代际一致性

- `AgentPoolService` 缓存 Agent 时必须记录该 Agent 绑定的 `sandbox_id`。
- `AgentPoolService` 缓存 Agent 时必须记录该 Agent 绑定的 Profile 指纹。
- 当前用户级 `sandbox_id` 判定必须同时比较 `SandboxSessionService.get_sandbox_id(user_id)` 与调用方从 `user_sandboxes` 读出的 `sandbox_id`；当 DB 中的持久化 id 与进程内缓存冲突时，DB id 用于触发旧 Agent 失效，并清理本地旧 sandbox 缓存后重建。
- 热缓存命中时，若 cached Agent 的 `sandbox_id` 或 Profile 指纹与当前用户级 sandbox 不一致，不得返回旧 Agent；必须移除该 session 缓存并重建。
- 用户级 sandbox fallback create 或跨 worker 持久化为新 `sandbox_id` 后，同用户旧 Agent 必须懒失效或主动失效，避免工具继续请求 OpenSandbox 已不存在的旧 sandbox。
- AgentPool 普通失效（配置更新、renew 失败、sandbox 代际切换等）必须区分 running / idle：
  - idle Agent 可以立即 evict；evict 时优先 interrupt 该 Agent tracker 中仍在运行的后台 bash 命令，再释放 AgentService 本地资源。
  - running Agent 不得被 close / interrupt；只能从热缓存 detach，或标记懒失效，等待当前 run 自然退出后再重建。
- 该约束不表示同用户 Agent run 串行化；`AGENT_USER_CONCURRENCY_LIMIT` 仍只限制同时运行的不同 session 数。处在额度内、且绑定当前 `sandbox_id` 的多个 Agent 可以并发运行。
- 若同用户 cached Agent 数超过 `AGENT_USER_CONCURRENCY_LIMIT` 形成资源压力，只能优先失效旧且 idle 的 Agent；不得为了缓存收敛移除仍在运行的 session。
- `renew(user_id)` 失败时应失效该用户全部已缓存 Agent；一用户一 sandbox 架构下，其他 session 大概率也持有同一失效 sandbox 对象。正在创建但尚未进入 Agent 缓存的 session 占位不得被删除。
- AgentPool TTL cleanup 不得移除仍在运行的 Agent；运行中的过期 session 保留到运行结束后的下一次安全清理。

### 后台 bash 命令生命周期

- `SandboxBashTool(run_in_background=True)` 必须把 OpenSandbox command id 记录到 session 级 `_BackgroundCommandTracker`，供 `bash_output` / `bash_kill` 使用。
- 后台命令默认带服务端运行上限 `SANDBOX_BACKGROUND_COMMAND_TIMEOUT_SECONDS=21600` 秒；`0` 表示不设置服务端 timeout，负数配置启动即失败。
- Agent idle eviction / session 删除 / 用户删除等明确清理路径应 best-effort interrupt tracker 中仍在运行的后台命令，避免失去 tracker 后形成孤儿进程。
- 普通配置失效不得为了清理 tracker 中断正在执行的 Agent run；running Agent 只能懒失效。

### 前台 bash 命令 timeout 与错误诊断

- `SandboxBashTool` 前台命令的 `timeout` 参数默认 10 秒、最大 600 秒；测试、构建、安装等长命令应由模型显式传 `timeout=300` 或 `timeout=600`。
- `pytest --timeout` 只限制单个测试用例，不改变沙箱前台命令的 timeout；工具描述与 `timeout` 参数 schema 必须明确提示这一点。
- OpenSandbox `execution.error.traceback` 应返回给 Agent 作为诊断信息，但必须限长；长 traceback 默认最多保留 8 行、2000 字符。
- 当 traceback 中包含 `signal: killed` 时，截断逻辑必须保留该关键信号，并在工具错误中提示本次前台命令 timeout，方便模型下一轮改为显式 timeout 或后台执行。

### 暂停策略

- 仅当用户所有 session 都从 AgentPool TTL 过期时才触发 pause
- pause 调用后必须 sandbox.close()（finally block）
- DB status 更新为 "paused"

### 卷挂载

- 用户存储卷名: SHA-1(user_id) 为隔离命名
- host_path 使用 `_user_storage_host_path` 方法安全化 user_id
- host_path 基于全局 `SANDBOX_HOST_STORAGE_ROOT` + `user_id` 稳定生成；删除用户时必须先成功 `kill()` 清理挂载文件与容器，再删除 `user_sandboxes` 和账号数据。若 `kill()` 返回 False，删除用户必须失败，避免同名新用户继承旧文件。
- 管理员切换用户 Profile 时，旧 sandbox 清理是 best-effort：kill 失败只记录 warning，仍清空旧 `user_sandboxes` 绑定并完成切换；旧 OpenSandbox 容器由 `SANDBOX_TIMEOUT_MINUTES` 对应的空闲 TTL 回收。
- 容器内 mount 路径全局统一，不允许按 Profile 自定义。
- 切换 Profile 不做跨 VM 文件迁移；DB-backed 记忆文件会在新 sandbox 创建后重新同步。

### 路径安全

- `is_within_sandbox_root(path, mount_path)` — 路径包含检查
- `to_sandbox_relative_path(path, mount_path)` — 绝对->相对转换
- mount 路径默认 `/home/user`

### Skill 推送

- 批量推送遍历目录，排除 node_modules/__pycache__/.git/.venv
- 单个推送通过 SKILL.md frontmatter 的 dir 字段定位
- 已推送集合跟踪避免重复

## 5. 失败模式与错误处理

- 沙箱创建失败 -> RuntimeError
- 沙箱连接失败（已销毁）-> 级联到 create
- 沙箱 resume 失败 -> 级联到 create
- kill 时沙箱不可达、挂载目录清理失败，或未命中 live cached sandbox 且既有 sandbox 的 active Profile 指纹无法解析/版本不匹配 -> 返回 False；用户删除路径不得继续清理 DB 账号数据；管理员切换用户 Profile 路径可继续切换，并将旧 sandbox 交给 OpenSandbox TTL 回收
- mkdir 失败 -> get_or_resume 的重试机制
- Skill 推送失败 -> 返回 False，warning 日志
- 用户绑定的 Profile 不存在或被禁用 -> 409
- 默认 Profile 被禁用 -> 409（管理端不允许禁用默认 Profile）
- Profile 解析/数据库查询异常 -> 直接失败，不回退到当前用户有效 Profile 或 `.env` 默认后端

## 6. 可观测性

- 沙箱创建/连接/恢复/暂停/销毁日志（含 sandbox_id, user_id）
- Profile 解析、缓存失效、重建日志（含 profile id/version）
- 健康检查失败日志
- Skill 推送结果日志
- 缓存命中/miss 日志

## 7. 非目标

- 不做多沙箱 per user（始终 1:1）
- 不做沙箱快照/克隆
- 不做应用内网络白名单编辑；白名单由对应 OpenSandbox VM / 网络层维护
- 不做沙箱监控指标采集
- 不做跨用户沙箱共享
- 不做跨 Profile 文件自动迁移
