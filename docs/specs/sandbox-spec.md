# 沙箱交互 (Sandbox) — Spec

## 1. 模块职责边界

- OpenSandbox 生命周期管理（创建、连接、恢复、暂停、销毁、续期）
- 沙箱缓存（per-user 内存缓存）
- Skills 文件推送
- 用户自定义 Skill 发现
- 路径安全校验
- 不负责：沙箱内命令执行逻辑（由 Agent 工具负责）、沙箱集群管理

## 2. 数据模型

### user_sandboxes 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, UNIQUE, indexed |
| sandbox_id | String(100) | nullable |
| status | String(20) | default="active". Values: active/paused |
| created_at | DateTime | default=now |
| updated_at | DateTime | default=now, onupdate=now |

一个用户最多一个沙箱（UNIQUE 约束）。

## 3. API 契约

沙箱模块没有独立 API 端点，作为内部服务被 sessions/chat/cron 路由调用。

对外暴露的接口通过 SandboxSessionService 类：

- `create(user_id) -> Sandbox`
- `get_or_resume(user_id, sandbox_id) -> Sandbox` — 级联恢复: 内存缓存 -> 查询状态 -> connect/resume -> fallback create
- `pause(user_id) -> bool`
- `kill(user_id, sandbox_id) -> bool` — 先清理 mount 目录（rm -rf），再 kill 容器；用户删除路径必须在硬删除账号前调用
- `renew(user_id) -> bool`
- `push_skills(user_id, skills_dir) -> bool` — 批量上传（排除 node_modules, __pycache__, .git, .venv）
- `push_skill(user_id, skills_dir, skill_name) -> bool` — 单个推送，幂等（跟踪已推送集合）
- `discover_sandbox_skills(user_id, official_names) -> list[dict]`
- `read_sandbox_skill_content(user_id, skill_dir) -> str|None`

## 4. 行为语义与不变量

### 单例模式

- SandboxSessionService 通过 `__new__` 实现类级单例
- 内部缓存 `_cache: dict[str, Sandbox]` (user_id -> Sandbox)
- 已推送技能跟踪 `_pushed_skills: dict[str, set[str]]`

### 生命周期级联

get_or_resume 的恢复链：

1. 内存缓存命中 + 健康检查通过 -> 返回
2. 查询沙箱状态 -> Running: connect / Paused: resume / 其他: create
3. 所有路径失败 -> fallback create（新沙箱）

### sandbox_id 持久化（fallback create 路径）

- `get_or_resume` 走到 fallback create 后会自动调用 `_persist_sandbox_id_if_exists(user_id, new_id, previous_id=...)`：
  - 仅 `update` 已存在的 `user_sandbox` 行（同时把 `status` 重置为 `active`）
  - 不主动 `INSERT`（首次创建路径由 `sessions` / `agent_pool_service` 显式写入）
  - `new_id == previous_id` 时短路返回，不触发任何 DB 操作
- 目的：避免调用方持有失效旧 id → 反复 fallback create → 沙箱泄漏。
- 调用方（cron / sessions / agent_pool）无需再各自手动回写 `user_sandbox.sandbox_id`。

### 暂停策略

- 仅当用户所有 session 都从 AgentPool TTL 过期时才触发 pause
- pause 调用后必须 sandbox.close()（finally block）
- DB status 更新为 "paused"

### 卷挂载

- 用户存储卷名: SHA-1(user_id) 为隔离命名
- host_path 使用 `_user_storage_host_path` 方法安全化 user_id
- host_path 基于 `user_id` 稳定生成；删除用户时必须先成功 `kill()` 清理挂载文件与容器，再删除 `user_sandboxes` 和账号数据。若 `kill()` 返回 False，删除用户必须失败，避免同名新用户继承旧文件。

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
- kill 时沙箱不可达或挂载目录清理失败 -> 返回 False；用户删除路径不得继续清理 DB 账号数据
- mkdir 失败 -> get_or_resume 的重试机制
- Skill 推送失败 -> 返回 False，warning 日志

## 6. 可观测性

- 沙箱创建/连接/恢复/暂停/销毁日志（含 sandbox_id, user_id）
- 健康检查失败日志
- Skill 推送结果日志
- 缓存命中/miss 日志

## 7. 非目标

- 不做多沙箱 per user（始终 1:1）
- 不做沙箱资源限制配置 API（硬编码 cpu=1, memory=2Gi）
- 不做沙箱快照/克隆
- 不做沙箱网络策略配置 API
- 不做沙箱监控指标采集
- 不做跨用户沙箱共享
