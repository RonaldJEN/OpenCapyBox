# 环境变量参考

完整示例见项目根目录 `.env.example`。本文档说明各变量的用途、默认值及关联组件。

---

## 基础与认证

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `APP_NAME` | 否 | `OpenCapyBox Backend` | 应用名称 |
| `DEBUG` | 否 | `false` | 调试模式 |
| `SIMPLE_AUTH_USERS` | 首次初始化需要 | — | 首次 bootstrap 本地 simple 用户，格式：`user:pass,user2:pass2`。`auth_users` 表非空后不再同步该变量 |
| `AUTH_ADMIN_USERS` | 首次初始化需要 | `admin` | 首次 bootstrap 管理员用户名列表，格式：`admin,user2`，需出现在 `SIMPLE_AUTH_USERS` 中。运行时管理员权限以 `auth_users.is_admin` 为准 |
| `AUTH_SECRET_KEY` | 是 | 派生密钥（启动告警） | JWT 签名密钥，建议 32+ 随机字符串 |
| `AUTH_TOKEN_EXPIRE_MINUTES` | 否 | `720` | JWT 过期时间（分钟） |
| `ENTERPRISE_SSO_ENABLED` | 否 | `false` | 是否启用企业 SSO 入口。社区版默认不内置具体企业域认证实现，需部署方接入身份验证器适配器 |
| `ENTERPRISE_SSO_GATEWAY_URL` | 企业 SSO 按需 | — | 企业统一认证网关地址，如 `http://gateway-host/infra-auth` |
| `ENTERPRISE_SSO_JWT_SECRET` | 企业 SSO 按需 | — | 企业 SSO 网关 JWT 签名密钥，具体用途由企业部署方的适配器实现决定 |

认证用户、启停状态、管理员权限、周/月 token 限额运行时均以 `auth_users` 表为事实源。`SIMPLE_AUTH_USERS` / `AUTH_ADMIN_USERS` 只负责空表首次初始化，后续请通过管理后台维护用户。

## API / 数据库 / CORS

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `API_PREFIX` | 否 | `/api` | API 路由前缀 |
| `DATABASE_URL` | 是（使用 `.env.example` 启动时） | 未配置时应用内默认 `sqlite:///./data/database/open_capy_box.db` | 数据库连接串。当前 `.env.example` 使用 PostgreSQL 模板，启动前必须替换为真实 PostgreSQL URL。 |
| `TEST_DATABASE_URL` | 否 | `sqlite:///./data/database/open_capy_box_test.db` | pytest 集成测试数据库连接串；使用 PostgreSQL 时库名必须包含 `test` / `pytest` / `ci`，且禁止指向生产库。 |
| `CORS_ORIGINS` | 否 | `["http://localhost:3000","http://localhost:5173"]` | 允许的跨域源 |

PostgreSQL 目标库必须提前创建，并安装 `pgvector` 扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

未安装 pgvector 时，启动 / `init_db()` 会在扩展检查阶段直接失败。若仅做本地 SQLite 开发，可改用 `sqlite:///./data/database/open_capy_box.db`，但生产与迁移脚本按 PostgreSQL + pgvector 路径维护。

## 模型能力（Model Registry）

模型参数（provider / api_base / max_tokens 等）在 `models.yaml` 中配置。此处仅填写 API Key，供 `models.yaml` 的 `${ENV_VAR}` 引用。

| 变量 | 必填 | 说明 |
|------|------|------|
| `LLM_API_KEY` | 按需 | DashScope 统一密钥（Qwen / GLM / DeepSeek） |
| `MINIMAX_API_KEY` | 按需 | MiniMax 模型专用密钥 |
| `GEMINI_API_KEY` | 按需 | Google Gemini 密钥 |
| `LLM_API_BASE` | 否 | 供 `models.yaml` 引用的默认 API Base |
| `LLM_MODEL` | 否 | 供 `models.yaml` 引用的默认模型 ID |
| `LLM_PROVIDER` | 否 | 供 `models.yaml` 引用的默认 Provider |

## OpenSandbox

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `SANDBOX_DOMAIN` | 按需 | — | OpenSandbox 服务地址，如 `localhost:8080` |
| `SANDBOX_API_KEY` | 按需 | — | OpenSandbox API Key |
| `SANDBOX_IMAGE` | 否 | `code-interpreter-agent:v1.1.0` | 沙箱容器镜像 |
| `SANDBOX_PROTOCOL` | 否 | `http` | OpenSandbox 协议（http/https） |
| `SANDBOX_USE_SERVER_PROXY` | 否 | `true` | 是否使用服务器代理模式 |
| `SANDBOX_TIMEOUT_MINUTES` | 否 | `60` | 沙箱容器空闲超时（分钟），超时后容器被回收 |
| `SANDBOX_READY_TIMEOUT_SECONDS` | 否 | `120` | 等待沙箱容器启动就绪的最大时间（秒） |
| `SANDBOX_PERSISTENT_STORAGE_ENABLED` | 否 | `true` | 是否启用持久化存储挂载 |
| `SANDBOX_HOST_STORAGE_ROOT` | 否 | `/tmp/sandbox` | 宿主机持久化存储根路径 |
| `SANDBOX_STORAGE_MOUNT_PATH` | 否 | `/home/user` | 容器内挂载路径 |

## Agent / SSE

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `AGENT_MAX_STEPS` | 否 | `100` | Agent 单次 run 最大步数 |
| `AGENT_MAX_HISTORY_MESSAGES` | 否 | `120` | 历史消息注入上限（条数） |
| `AGENT_TOOL_TIMEOUT` | 否 | `300` | 单次工具执行超时（秒），0 表示不限。详见下方超时体系说明 |
| `SSE_HEARTBEAT_INTERVAL` | 否 | `15` | SSE 心跳间隔（秒） |
| `SSE_SUBSCRIBE_TIMEOUT` | 否 | `300` | SSE 订阅超时（秒） |
| `TIMEZONE_OFFSET` | 否 | `8` | UTC 偏移小时数（中国大陆常用 8） |

## 超时体系说明

系统在不同层级设有超时保护，各司其职：

```
基础设施层
├── SANDBOX_TIMEOUT_MINUTES (60min)    沙箱容器空闲超时，超时后容器被回收
└── SANDBOX_READY_TIMEOUT_SECONDS (120s) 沙箱启动就绪等待

网络传输层
├── SSE_HEARTBEAT_INTERVAL (15s)       SSE 心跳，防止连接被中间件/nginx 判定空闲
└── SSE_SUBSCRIBE_TIMEOUT (300s)       前端订阅事件流的最大等待

Agent 逻辑层
└── AGENT_TOOL_TIMEOUT (300s)          单次 tool.execute() 的 asyncio.wait_for 兜底
                                       防止沙箱 API 无响应导致 step 永久挂起
                                       SandboxBashTool 自身覆盖为 660s (SDK 层 600s + 余量)
```

工具级覆盖：每个 Tool 子类可通过 `execute_timeout` 类属性覆盖全局默认值（0 = 用全局默认）。

## 部署参数

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `UVICORN_WORKERS` | 否 | `2` | Uvicorn worker 数（Docker/K8s 部署时生效） |
| `LOG_LEVEL` | 否 | `info` | 日志级别 |

## 搜索与 Embedding

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `BOCHA_SEARCH_APPCODE` | 按需 | — | 博查搜索 AppCode |
| `EMBEDDING_API_KEY` | 否 | — | Embedding API Key（不填则降级为关键词检索） |
| `EMBEDDING_API_BASE` | 否 | `https://api.openai.com/v1` | Embedding API Base |
| `EMBEDDING_MODEL` | 否 | `text-embedding-3-small` | Embedding 模型名 |
