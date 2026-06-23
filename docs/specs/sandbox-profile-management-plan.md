# 多 OpenSandbox 后端管理 — Final Plan / Spec

## 1. 目标

为管理员提供“沙箱管理”专区，注册多个 OpenSandbox VM/backend，并按部门或用户分配不同沙箱后端。用户不直接配置白名单；白名单仍由对应 OpenSandbox VM 所在网络环境控制。

典型分组：

- 运作部门
- 交易部门
- 投研部门
- IT 部门

## 2. 管理模型

### Sandbox Profile

一个 Sandbox Profile 表示一个 OpenSandbox 后端配置，通常对应一台 OpenSandbox VM。

可配置字段：

- 基础信息：`name`、`description`、`department`
- 连接信息：`domain`、`protocol`、`api_key`、`use_server_proxy`
- 管理状态：`is_default`、`enabled`、`version`

管理端创建 Profile 时 `api_key` 必填，不支持无 key 后端；编辑时空 `api_key` 表示保持现有密钥不变。

镜像、资源限制、宿主存储根路径和容器 `mount_path` 使用全局配置，不按 Profile 自定义。

系统启动时会根据当前 `.env` OpenSandbox 配置自动创建一个默认 Profile。未显式分配沙箱的用户走默认 Profile。

### User Assignment

用户有一个可选的 Profile 绑定：

- `sandbox_profile_id = null`：使用全局默认 Profile
- `sandbox_profile_id = <id>`：使用管理员指定的非默认 Profile

默认 Profile 由 `.env` OpenSandbox 配置 bootstrap，用户绑定不额外保存默认 Profile ID；如果管理端传入当前默认 Profile ID，后端归一化为 `null`。

管理员创建 simple / LDAP 用户时可以选择 Profile；之后也可以在用户管理表格里修改。

## 3. 生命周期行为

每个用户仍然最多一个 active sandbox。Profile 只决定这个用户的 sandbox 应该在哪个 OpenSandbox 后端创建/连接/恢复。

运行时流程：

1. 解析用户有效 Profile：显式绑定优先，否则默认 Profile。
2. `SandboxSessionService` 使用该 Profile 的连接信息创建或恢复 sandbox；镜像、资源和存储路径来自全局配置。
3. `user_sandboxes` 记录真实创建该 sandbox 时使用的 `active_profile_id` 和 `active_profile_version`。
4. 如果管理员修改了 Profile runtime 字段或切换了用户绑定，旧 sandbox 会被判定为需要重建。
5. 下次启动/恢复时，旧 Agent/Sandbox cache 会失效并按新 Profile 重建。

升级迁移时，已有 `user_sandboxes.sandbox_id` 但缺少 `active_profile_*` 的存量记录会回填为默认 Profile 的当前 `id/version`，避免旧 `.env` 后端创建的 sandbox 被误判为 stale。

会触发 `version + 1` 的 runtime 字段：

- `domain`
- `protocol`
- `api_key`
- `use_server_proxy`

非 runtime 字段如名称、描述、部门不会触发重建。

## 4. 管理端状态口径

管理端不做实时 OpenSandbox 探活，避免列表页卡顿或依赖外部 VM 状态。

后台展示的是 DB 口径：

- 用户当前期望 Profile：来自 `user_sandbox_configs` 或默认 Profile
- 用户已有 sandbox id/status：来自 `user_sandboxes`
- 是否需要重建：比较 `user_sandboxes.active_profile_*` 与期望 Profile 当前 `id/version`
- Profile 绑定人数：显式绑定人数 + 默认 Profile 覆盖的未绑定用户数

## 5. 文件迁移策略

MVP 不做跨 Profile 文件自动迁移。

原因：

- 不同 VM 的网络白名单、镜像和目录结构可能不同。
- 自动迁移会引入大文件传输、权限、失败回滚和数据一致性问题。

切换用户 Profile 时：

- 默认会 best-effort kill/清理旧 sandbox 缓存与绑定；kill 失败只记录 warning，不阻断 Profile 切换。
- 如果旧 sandbox 因 active Profile 指纹缺失、版本变化或 Profile 不存在而无法重新连接，不回退到用户当前期望 Profile 或 `.env` 连接；旧容器依赖 OpenSandbox 空闲 TTL 回收，TTL 由 `SANDBOX_TIMEOUT_MINUTES` 控制，默认 60 分钟。
- 用户记忆文件由 DB 同步到新 sandbox。
- sandbox 内临时文件、会话工作目录和用户手工生成文件不自动迁移。

后续如果要做迁移，应单独设计“导出旧 sandbox -> 上传新 sandbox -> 校验 -> 切换绑定”的异步任务。

TODO：Profile runtime 字段或默认 Profile 变更影响的是一组用户，MVP 不主动跨 OpenSandbox 后端清理这些用户的旧 sandbox；旧 sandbox 依赖 OpenSandbox 空闲 TTL 回收，TTL 由 `SANDBOX_TIMEOUT_MINUTES` 控制，默认 60 分钟。当前版本也不保存 sandbox 创建时的连接快照或 Profile revision history。后续做跨 Profile 数据迁移时，应一并设计旧 sandbox 主动清理、连接信息快照和失败回滚策略。

运维建议：需要切换到新的 OpenSandbox 后端时，应新建 Profile 并重新分配用户，不应原地修改既有 Profile 的 `domain`、`protocol`、`api_key`、`use_server_proxy` 来表达换后端。原地修改会使后续新 sandbox 使用新配置，但旧 sandbox 仍可能留在旧后端并依赖 TTL 回收。

删除用户时，如果既有 sandbox 仍在进程内缓存，可以直接使用 live sandbox 对象清理。若缓存未命中、必须按 `sandbox_id` 重新连接/恢复，则只使用 `user_sandboxes.active_profile_id/version` 指向的当前 Profile；如果 active 指纹缺失、Profile 不存在或版本已变化，因为没有历史连接快照，系统必须视为不可清理并阻止删除用户，而不是回退到用户当前期望 Profile。删除路径不能像 Profile 切换一样依赖 TTL，因为同名 `user_id` 可被重新创建，必须避免旧持久化目录被新账号继承。

## 6. OpenSandbox VM 创建后可修改项

应用层可修改 Profile 的连接信息；镜像、资源和宿主机存储根路径仍由全局配置管理。

记录口径：

- Profile 配置更新仅保留当前行的 `updated_at` 和 `version`，MVP 不记录修改人、字段 diff 或历史连接配置。
- 用户 Profile 分配保存在 `user_sandbox_configs`，当前记录包含 `updated_by`、`created_at`、`updated_at`，但不保存多版本分配历史。

已运行 sandbox 的行为：

- 修改名称/部门/描述：无需重建。
- 修改连接域名/API Key/协议：后续连接走新配置；已有 sandbox 若在旧后端，下次使用会按新 Profile 重建；MVP 不主动迁移文件或清理旧后端 sandbox。
- 修改全局镜像/存储配置不属于 Profile 管理范围，应按独立运维流程处理。
- 修改 VM 网络白名单：不通过应用配置，直接在 VM / OpenSandbox 所在网络层维护。

## 7. 已实现接口

- `GET /api/admin/sandbox-profiles`
- `POST /api/admin/sandbox-profiles`
- `PATCH /api/admin/sandbox-profiles/{profile_id}`
- `PATCH /api/admin/sandbox-profiles/{profile_id}/default`
- `PATCH /api/admin/sandbox-profiles/{profile_id}/enabled`
- `PATCH /api/admin/users/{user_id}/sandbox-profile`

用户管理接口返回新增字段：

- `sandbox_profile_id`
- `sandbox_profile_name`
- `sandbox_profile_source`
- `sandbox_profile_error`
- `sandbox_id`
- `sandbox_status`
- `sandbox_needs_recreate`

其中 `sandbox_profile_source` 可为 `explicit`、`default`、`missing`、`disabled`。显式绑定的 Profile 被删除或禁用时，管理端必须返回异常状态和 `sandbox_profile_error`，不能伪装成默认 Profile。

## 8. 约束

- 一个用户同一时间仍只有一个 sandbox。
- 默认 Profile 不能禁用。
- 禁用 Profile 后，已绑定用户不能继续启动新任务，管理员应重新分配。
- Profile API Key 当前保存为数据库字段；管理端创建 Profile 必须提供 API Key，只返回 `api_key_set`，不回显明文，不支持清空密钥。
- 管理端状态为 DB 状态，不承诺实时反映 VM 在线/离线。
