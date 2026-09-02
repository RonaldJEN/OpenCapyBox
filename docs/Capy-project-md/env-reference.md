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
| `MOBILE_SSO_GATEWAY_BASE_URL` | 移动端必填 | — | 企业认证网关内部基础地址；后端使用浏览器 Cookie 调用 `curUser` |
| `MOBILE_SSO_CURRENT_USER_PATH` | 否 | `/base/sys/role/curUser` | 企业网关当前用户接口路径 |
| `MOBILE_SSO_TIMEOUT_SECONDS` | 否 | `10` | 后端请求企业认证网关的超时时间 |
| `MOBILE_SSO_GATEWAY_HEADER_NAME` | 否 | 空 | 企业网关要求的可选附加请求头名称；留空时不发送 |
| `MOBILE_SSO_GATEWAY_HEADER_VALUE` | 配置请求头名称时必填 | 空 | 企业网关附加请求头的值 |
| `MOBILE_AUTH_COOKIE_SECURE` | 否 | `true` | 移动端 HttpOnly Cookie 是否仅允许 HTTPS；仅本地 HTTP 联调设为 `false` |
| `LDAP_URLS` | LDAP 用户登录需要 | — | LDAP 地址列表，逗号分隔，按顺序主备尝试；生产环境建议使用 `ldaps://`，例如 `ldaps://ldap.example.local,ldaps://ldap-backup.example.local:636` |
| `LDAP_USER_DOMAIN` | 否 | — | LDAP 绑定域；填写 `example.local` 时使用 `username@example.local` 作为 bind 用户，不填则直接使用短账号 |

认证用户、启停状态、管理员权限、周/月 token 限额运行时均以 `auth_users` 表为事实源。`SIMPLE_AUTH_USERS` / `AUTH_ADMIN_USERS` 只负责空表首次初始化，后续请通过管理后台维护用户。

企业微信移动端不保存账号密码或 localStorage Token。浏览器经外层网关建立企业 Cookie，`POST /api/auth/mobile/session` 再由后端调用 `curUser` 验证域账号，并签发本系统 host-only HttpOnly Cookie。正式入口域名必须位于企业 Cookie 可覆盖的域下。

## API / 数据库 / CORS

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `API_PREFIX` | 否 | `/api` | API 路由前缀 |
| `DATABASE_URL` | 是（使用 `.env.example` 启动时） | `postgresql://postgres:postgres@localhost:5432/open_capy_box` | PostgreSQL 数据库连接串。当前 `.env.example` 使用占位模板，启动前必须替换为真实 PostgreSQL URL。 |
| `TEST_DATABASE_URL` | 是（运行 pytest 时） | 无 | pytest 集成测试 PostgreSQL 连接串；库名必须包含 `test` / `pytest` / `ci`，且禁止指向生产库。 |
| `CORS_ORIGINS` | 否 | `["http://localhost:3000","http://localhost:5173"]` | 允许的跨域源 |

PostgreSQL 目标库必须提前创建，并安装 `pgvector` 扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

未安装 pgvector 时，启动 / `init_db()` 会在扩展检查阶段直接失败。本地开发、测试与生产环境均按 PostgreSQL + pgvector 路径维护。

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
| `SANDBOX_API_KEY` | 是 | — | OpenSandbox API Key；生产环境必须配置 |
| `SANDBOX_IMAGE` | 否 | `code-interpreter-agent:v1.1.0` | 沙箱容器镜像；Office 在线预览需要 `python3`、GNU coreutils（`timeout/stat/head`）、LibreOffice/`soffice`、fontconfig 与中文字体 |
| `SANDBOX_PROTOCOL` | 否 | `http` | OpenSandbox 协议（http/https） |
| `SANDBOX_USE_SERVER_PROXY` | 否 | `true` | 是否使用服务器代理模式 |
| `SANDBOX_TIMEOUT_MINUTES` | 否 | `60` | 沙箱容器空闲超时（分钟），超时后容器被回收 |
| `SANDBOX_READY_TIMEOUT_SECONDS` | 否 | `120` | 等待沙箱容器启动就绪的最大时间（秒） |
| `SANDBOX_BACKGROUND_COMMAND_TIMEOUT_SECONDS` | 否 | `21600` | 后台 bash 命令服务端最大运行时间（秒）；`0` 表示不设置服务端 timeout，负数非法 |
| `SANDBOX_PERSISTENT_STORAGE_ENABLED` | 否 | `true` | 是否启用持久化存储挂载 |
| `SANDBOX_HOST_STORAGE_ROOT` | 否 | `/tmp/sandbox` | 宿主机持久化存储根路径 |
| `SANDBOX_STORAGE_MOUNT_PATH` | 否 | `/home/user` | 容器内挂载路径 |
| `WORKSPACE_QUOTA_BYTES` | 否 | `5368709120` | 单用户工作区总容量（active 文件，不含历史对象） |
| `WORKSPACE_HISTORY_QUOTA_BYTES` | 否 | `5368709120` | 单用户内容寻址历史对象软配额；只按唯一 SHA 对象计量，超限触发回收但不阻断正式文件保存 |
| `WORKSPACE_PREVIEW_CACHE_BYTES` | 否 | `536870912` | 单用户/Session Office PDF 派生缓存上限，超限按 LRU 回收完整缓存目录 |
| `WORKSPACE_MAX_FILE_BYTES` | 否 | `536870912` | 工作区单文件上限，必须小于等于总配额 |
| `WORKSPACE_MAX_ENTRIES` | 否 | `10000` | 单用户工作区条目上限 |
| `WORKSPACE_MUTATION_LEASE_SECONDS` | 否 | `120` | prepared mutation lease；过期后启动/首次写入按文件状态与 hash 对账 |
| `WORKSPACE_VERSION_RETENTION_COUNT` | 否 | `20` | 每个文件必须保留的最近检查点数量 |
| `WORKSPACE_VERSION_RETENTION_DAYS` | 否 | `30` | 检查点按时间保留的天数 |
| `WORKSPACE_DRAFT_BASE_RETENTION_DAYS` | 否 | `1` | 最近 autosave base 内容最长保留天数；过期后仍由 current/reference 保护 |
| `WORKSPACE_DRAFT_REVISION_RETENTION_COUNT` | 否 | `5` | 每个文件额外保留的最近未 checkpoint revision 数量，避免 autosave 形成无界历史 |
| `WORKSPACE_HISTORY_GC_INTERVAL_SECONDS` | 否 | `300` | 历史对象后台 reconciler/GC 周期 |
| `WORKSPACE_HISTORY_GC_BATCH_SIZE` | 否 | `100` | 单轮最多处理的 version 候选数 |

启动时系统会根据上述 OpenSandbox 连接环境变量创建一个默认 Sandbox Profile。之后管理员可在后台维护多个 Profile，并为用户显式分配；未分配用户继续使用默认 Profile。镜像、持久化存储根路径和容器挂载路径仍使用全局配置，不按 Profile 单独配置。

OpenSandbox execd 版本需包含上游修复 `e7f772fa`，以正确编码非 ASCII 下载文件名。

Office 派生预览在**用户沙箱镜像**内执行，不使用 API 镜像中的 LibreOffice。若自定义 `SANDBOX_IMAGE` 不具备对应依赖，普通 Markdown/HTML/图片预览不受影响，Office 派生请求返回 503；上线前必须用目标镜像实际转换一份中文 DOCX 与 PPTX。

## Agent / SSE

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `AGENT_MAX_STEPS` | 否 | `100` | Agent 单次 run 最大步数 |
| `AGENT_HISTORY_STRATEGY` | 否 | `checkpoint_v1` | 长程上下文策略；`checkpoint_v1` 使用累计替代 checkpoint，`legacy_120` 仅用于回滚 |
| `AGENT_MAX_HISTORY_MESSAGES` | 否 | `120` | `legacy_120` 的历史消息注入上限（条数）；默认 `checkpoint_v1` 不使用该值裁剪 |
| `AGENT_INIT_TIMEOUT_SECONDS` | 否 | `180` | Agent、Sandbox、Skills 与工具目录初始化的总墙钟期限；超时返回稳定错误并释放运行锁 |
| `AGENT_TOOL_TIMEOUT` | 否 | `300` | 单次工具执行超时（秒），0 表示不限。详见下方超时体系说明 |
| `AGENT_SUBAGENT_MAX_PARALLEL` | 否 | `3` | 同一父 Agent step 内最多并行执行的 `sub_agent` 数；`1` 表示串行 |
| `AGENT_USER_CONCURRENCY_LIMIT` | 否 | `1` | 同一用户允许同时运行的不同会话数 |
| `SKILL_DISABLED_CACHE_TTL_SECONDS` | 否 | `30` | Skill 启停快照复用窗口（秒），避免每步 LLM 请求都查库；改启停最迟约此值内生效，`0` 表示每步实时查库 |
| `TOOL_APPROVAL_EXECUTION_LEASE_SECONDS` | 否 | `120` | 审批已从 `approved` 派发为 `executing` 后的执行 lease；过期只收敛 unknown，不授权重试 |
| `TOOL_APPROVAL_LEASE_HEARTBEAT_SECONDS` | 否 | `30` | executing 审批续租心跳间隔，必须小于执行 lease |
| `TOOL_APPROVAL_RECONCILE_INTERVAL_SECONDS` | 否 | `30` | 过期 executing 审批对账周期 |
| `CRON_CLAIM_LEASE_SECONDS` | 否 | `120` | Cron queued run 被 worker claim 后的执行 lease；过期后按 phase 对账 |
| `CRON_CLAIM_HEARTBEAT_SECONDS` | 否 | `30` | Cron executing worker 续租间隔，应小于 claim lease |
| `CRON_RECONCILE_INTERVAL_SECONDS` | 否 | `30` | 过期 Cron claim 对账及 durable queued run 恢复周期 |
| `SSE_HEARTBEAT_INTERVAL` | 否 | `15` | SSE 心跳间隔（秒） |
| `SSE_SUBSCRIBE_TIMEOUT` | 否 | `300` | runtime 心跳陈旧阈值（秒），用于 UserRunLock 回收与 worker_dead 判定；不是订阅最大时长 |
| `AGUI_REPAIR_TERMINAL_SINCE_HOURS` | 否 | `24` | `scripts/repair_terminal_runs.py` 默认扫描窗口（小时） |
| `TIMEZONE_OFFSET` | 否 | `8` | UTC 偏移小时数（中国大陆常用 8） |

> 第一版 Agent runtime 依赖进程内 `AguiEventBus`、subscriber registry 和 per-run cancel token，正确性假设为单 worker。生产入口、容器和进程管理应按 `UVICORN_WORKERS=1` 部署；恢复多 worker 前必须引入外部 bus/lease 或 durable command queue。

`ask_user` 与工具审批固定采用 same-Round 暂停/恢复协议，不提供环境变量切换为其他 Round 语义。

## 超时体系说明

系统在不同层级设有超时保护，各司其职：

```
基础设施层
├── SANDBOX_TIMEOUT_MINUTES (60min)      沙箱容器空闲超时，超时后容器被回收
├── SANDBOX_READY_TIMEOUT_SECONDS (120s) 沙箱启动就绪等待
├── AGENT_INIT_TIMEOUT_SECONDS (180s)    Agent/Sandbox/工具初始化总墙钟期限
└── SANDBOX_BACKGROUND_COMMAND_TIMEOUT_SECONDS (21600s)
                                           后台 bash 命令服务端运行上限，0 表示不设置

网络传输层
└── SSE_HEARTBEAT_INTERVAL (15s)       SSE 心跳，防止连接被中间件/nginx 判定空闲

Runtime 存活判定
└── SSE_SUBSCRIBE_TIMEOUT (300s)       历史兼容命名；作为 UserRunLock 心跳陈旧阈值
                                       与 worker_dead 判定窗口，不关闭健康订阅

Agent 逻辑层
└── AGENT_TOOL_TIMEOUT (300s)          单次 tool.execute() 的 asyncio.wait_for 兜底
                                       防止沙箱 API 无响应导致 step 永久挂起
                                       SandboxBashTool 自身覆盖为 660s (SDK 层 600s + 余量)

审批执行层
├── TOOL_APPROVAL_EXECUTION_LEASE_SECONDS (120s)
│                                      仅 approved → executing 时创建；过期不重试
├── TOOL_APPROVAL_LEASE_HEARTBEAT_SECONDS (30s)
│                                      executing worker 的续租间隔
└── TOOL_APPROVAL_RECONCILE_INTERVAL_SECONDS (30s)
                                       将过期 executing 保守收敛为 unknown
```

工具级覆盖：每个 Tool 子类可通过 `execute_timeout` 类属性覆盖全局默认值（0 = 用全局默认）。

## 部署参数

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `UVICORN_WORKERS` | 否 | `1` | Uvicorn worker 数（Docker/K8s 部署时生效）；第一版必须保持单 worker |
| `LOG_LEVEL` | 否 | `info` | 日志级别 |

## 搜索与 Embedding

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `BOCHA_SEARCH_APPCODE` | 按需 | — | 博查搜索 AppCode |
| `EMBEDDING_API_KEY` | 否 | — | Embedding API Key（不填则降级为关键词检索） |
| `EMBEDDING_API_BASE` | 否 | `https://api.openai.com/v1` | Embedding API Base |
| `EMBEDDING_MODEL` | 否 | `text-embedding-3-small` | Embedding 模型名 |
