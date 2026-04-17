# 认证鉴权 (Auth) — Spec

## 1. 模块职责边界

- 用户身份认证（用户名/密码）
- JWT Token 签发与校验
- 全局鉴权依赖注入（`get_current_user`）
- **不负责**：RBAC、OAuth、多租户

## 2. 数据模型

无专用数据库表。认证用户通过环境变量 `SIMPLE_AUTH_USERS` 配置，格式为 `user:pass,user2:pass2`。由 `Settings.get_auth_users()` 解析。JWT payload 包含 `user_id`。Token 过期时间：43200 秒（12 小时）。密钥来源：`AUTH_SECRET_KEY` 环境变量；若为空，则通过 SHA-256(app_name) 派生确定性密钥——仅用于开发环境，启动时输出 warning 日志。

## 3. API 契约

### POST /api/auth/login

- **请求**：form-urlencoded `username`, `password`
- **响应 200**：`{user_id: str, access_token: str, token_type: "bearer", expires_in: 43200, message: str}`
- **错误 401**：`{"detail": "用户名或密码错误"}`
- **鉴权要求**：无（公开端点）

### GET /api/auth/me

- **请求**：Authorization header 携带 Bearer token
- **响应 200**：`{user_id: str, username: str}`
- **错误 401**：token 无效或已过期
- **鉴权要求**：Bearer token，通过 `get_current_user` 注入

## 4. 行为语义与不变量

- Token 过期时间固定 12 小时，不支持刷新
- `get_current_user` 是全局依赖，几乎所有端点（除 `/login`、`/models`、`/health`）都注入
- Settings 通过 `@lru_cache` 单例化，启动后不可变
- 若 `AUTH_SECRET_KEY` 为空，使用 SHA-256(app_name) 派生确定性密钥（跨重启一致），仅用于开发环境
- 若 `SIMPLE_AUTH_USERS` 为空，启动时输出 warning 日志

## 5. 失败模式与错误处理

- 密码错误 → 401，不区分"用户不存在"和"密码错误"（防枚举）
- Token 过期 → 401
- Token 签名无效 → 401
- 无 Authorization header → 401

## 6. 可观测性

- 启动时日志：`AUTH_SECRET_KEY` 为空时 warning；`SIMPLE_AUTH_USERS` 为空时 warning
- 登录失败不单独打日志（依赖 FastAPI 默认 access log）

## 7. 非目标

- 不做用户注册（用户通过 env var 配置）
- 不做 Token 刷新
- 不做 RBAC / 权限分级
- 不做 OAuth / SSO
- 不做多租户隔离
- 不做密码加密存储（明文对比，因为用户列表在 env var）
