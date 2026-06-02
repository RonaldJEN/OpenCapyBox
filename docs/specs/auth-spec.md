# 认证鉴权 (Auth) — Spec

## 1. 模块职责边界

- 用户身份认证（simple 本地密码、LDAP 直连账号密码校验）
- JWT Token 签发与校验
- 全局鉴权依赖注入（`get_current_user` / `get_current_admin_user`）
- 用户开通状态、管理员标记、token 周/月限额的事实源
- **不负责**：OAuth、多租户、SSO 跳转、企业网关 Kerberos/NTLM 实现

## 2. 数据模型

### 2.1 `auth_users` 表

`auth_users` 是运行时用户与权限事实源。`.env` 中的 `SIMPLE_AUTH_USERS` / `AUTH_ADMIN_USERS` 仅用于首次 bootstrap。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | Integer | PK | 自增主键 |
| `user_id` | String(100) | UNIQUE, indexed | 稳定内部用户 ID，内部部署时建议等于域账号 |
| `username` | String(100) | UNIQUE, indexed | 登录名，第一版通常等于 `user_id` |
| `auth_type` | String(20) | `simple` / `ldap` | 认证类型 |
| `password_hash` | Text | nullable | simple 用户本地密码 hash；ldap 用户为空 |
| `enabled` | Boolean | NOT NULL | 是否允许登录；`false` 会让已有 token 失效 |
| `is_admin` | Boolean | NOT NULL | 是否管理员 |
| `token_limit_per_month` | BigInteger | nullable | 单月 token 上限；非负整数，`NULL` 表示不限 |
| `token_limit_per_week` | BigInteger | nullable | 单周 token 上限；非负整数，`NULL` 表示不限 |
| `last_login_at` | DateTime | nullable | 最近登录时间 |
| `token_generation` | Integer | NOT NULL, default 0 | 凭据代次；签发 JWT 时写入，校验时必须严格相等 |
| `created_by` | String(100) | nullable | 创建人 |
| `created_at` | DateTime | NOT NULL | 创建时间 |
| `updated_at` | DateTime | NOT NULL | 更新时间 |

### 2.2 Bootstrap

应用启动后若 `auth_users` 为空：

1. 读取 `SIMPLE_AUTH_USERS`，创建 `auth_type=simple` 用户。
2. 读取 `AUTH_ADMIN_USERS`，匹配用户设置 `is_admin=true`。
3. token 周/月限额默认为 `NULL`。

若 `auth_users` 非空，不再同步 `.env`，避免覆盖管理员后台修改。

PostgreSQL 部署使用事务级 advisory lock 串行化多 worker 首次初始化；锁获取或写入失败必须直接暴露，禁止静默降级或跳过。

JWT payload 包含 `user_id` 和 `gen`（凭据代次）。Token 过期时间：默认 43200 秒（12 小时）。密钥来源：`AUTH_SECRET_KEY` 环境变量；若为空，则通过 SHA-256(app_name) 派生确定性密钥，仅用于开发环境。

### 2.3 `auth_login_events` 表

`auth_login_events` 保存账号每次 Web 登录成功的审计历史，覆盖 simple 与 LDAP 登录。

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `id` | Integer | PK | 自增主键 |
| `user_id` | String(100) | indexed | 登录用户 ID；不做外键，删除账号后保留审计历史 |
| `username` | String(100) | NOT NULL | 登录时用户名快照 |
| `auth_type` | String(20) | NOT NULL | 登录用户认证类型：`simple` 或 `ldap` |
| `ip_address` | String(64) | nullable | 登录来源 IP |
| `user_agent` | Text | nullable | 浏览器 User-Agent |
| `login_at` | DateTime | indexed | 登录时间 |

IP 提取顺序：`X-Forwarded-For` 首个 IP → `X-Real-IP` → FastAPI `request.client.host`；写入前去除首尾空白并截断到 64 字符。部署时应确保后端仅接收可信反向代理流量，避免客户端直连伪造代理头。

### 2.4 LDAP 配置

LDAP 用户仍由 `auth_users` 表控制是否开通、是否启用、是否管理员。登录时 `/api/auth/login` 根据 `auth_type=ldap` 直接连接目录服务校验账号密码，不保存 LDAP 密码。

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LDAP_URLS` | LDAP 用户登录需要 | — | LDAP 地址列表，逗号分隔，按顺序主备尝试；生产环境建议使用 `ldaps://`，例如 `ldaps://ldap.example.local,ldaps://ldap-backup.example.local:636` |
| `LDAP_USER_DOMAIN` | 否 | — | LDAP 绑定域；填写 `example.local` 时使用 `username@example.local` 作为 bind 用户，不填则直接使用短账号 |

## 3. API 契约

### POST /api/auth/login

- **请求**：form-urlencoded `username`, `password`
- **响应 200**：`{user_id: str, access_token: str, token_type: "bearer", expires_in: 43200, role: "admin"|"user", is_admin: bool, message: str}`
- **错误 401**：`{"detail": "用户名或密码错误"}`
- **错误 403**：账号存在但 `enabled=false` 时返回 `账户已被禁用`
- **错误 503**：LDAP 用户登录时目录服务未配置或全部地址不可用
- **鉴权要求**：无（公开端点）

### GET /api/auth/me

- **请求**：Authorization header 携带 Bearer token
- **响应 200**：`{user_id, username, auth_type, enabled, role, is_admin, token_limit_per_week, token_limit_per_month, last_login_at, created_by, created_at, updated_at}`
- **错误 401**：token 无效或已过期
- **鉴权要求**：Bearer token，通过 `get_current_user` 注入

### GET /api/admin/users/{user_id}/login-events

- **请求**：query `limit`，默认 50，范围 1-200
- **响应 200**：`{user_id: str, events: [{id, user_id, username, auth_type, ip_address, user_agent, login_at}]}`
- **错误 403**：非管理员
- **错误 404**：用户不存在
- **鉴权要求**：管理员 Bearer token

## 4. 行为语义与不变量

- Token 过期时间默认 12 小时，不支持刷新
- `get_current_user` 是全局依赖，几乎所有端点（除 `/login`、`/models`、`/health`）都注入
- `get_current_user` 解码 JWT 后必须查询 `auth_users.enabled=true`
- `get_current_user` 解码 JWT 后必须校验 `gen == user.token_generation`（严格相等，无精度问题）
- `get_current_user` 解码 JWT 后必须校验 `iat >= auth_users.created_at`（按秒比较），避免同名账号硬删除后重建时旧 token 重新生效
- 禁用用户、重置密码时递增 `token_generation`，使所有旧 token 立即失效
- 登录时将当前 `token_generation` 写入 JWT `gen` 字段
- 删除用户为硬删除：删除 `auth_users` 账号记录，并清理该 `user_id` 的会话、AG-UI 事件、LLM 调用记录、Cron、记忆、Skill 配置、运行状态、沙箱绑定与沙箱文件
- 删除后的 `user_id` 可以重新创建；新账号不得继承旧用户的会话、Cron、记忆、沙箱文件或 token 使用记录
- 创建 LDAP 用户时对 `user_id` 执行 `normalize_domain_user` 规范化（去除域前缀/邮箱后缀），与 LDAP 登录口径一致
- `get_current_admin_user` 用于管理员路由，要求 `auth_users.is_admin=true`
- Settings 通过 `@lru_cache` 单例化，启动后不可变
- 若 `AUTH_SECRET_KEY` 为空，使用 SHA-256(app_name) 派生确定性密钥（跨重启一致），仅用于开发环境
- 若 `SIMPLE_AUTH_USERS` 为空，启动时输出 warning 日志
- simple 用户密码仅保存 PBKDF2 hash；ldap 用户不保存密码
- `/api/auth/login` 是唯一登录入口：先按归一化后的 `user_id` 查询 `auth_users`，再检查 `enabled`，最后按 `auth_type` 分流认证
- 用户登录成功后写入 `auth_login_events`；登录失败、账号禁用不写入该表
- 管理后台用户列表返回用户最近一次登录 IP，并支持按用户查看最近登录历史
- ldap 登录使用 `LDAP_URLS` 顺序尝试；某个地址连接失败时尝试下一个地址，simple bind 成功即视为鉴权成功，绑定失败则视为用户名或密码错误；生产部署建议配置 `ldaps://`

## 5. 失败模式与错误处理

- 密码错误 → 401，不区分"用户不存在"和"密码错误"（防枚举）
- simple 或 ldap 用户密码错误 → 401，错误文案一致
- 账号被禁用 → 403，提示账号已禁用
- LDAP 未配置或全部地址不可用 → 503
- Token 过期 → 401
- Token 签名无效 → 401
- 用户被禁用 → 401，已有 token 同步失效（通过 `token_generation` 递增机制）
- 用户被删除 → 账号行不存在，已有 token 在下一次鉴权查询 `auth_users` 时失效；`user_id` 可重新创建且不继承旧数据
- 无 Authorization header → 401

## 6. 可观测性

- 启动时日志：`AUTH_SECRET_KEY` 为空时 warning；`SIMPLE_AUTH_USERS` 为空时 warning
- 登录失败不单独打日志（依赖 FastAPI 默认 access log）

## 7. 非目标

- 不做开放注册（用户由 bootstrap 或管理员后台创建）
- 不做 Token 刷新
- 不做细粒度 RBAC / 权限分级
- 不做 SSO 跳转或网关回调
- 不在应用内实现 Kerberos/NTLM
- 不做多租户隔离
