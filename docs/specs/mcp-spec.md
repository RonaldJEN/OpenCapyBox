# MCP 数据连接 (MCP) — Spec

## 1. 模块职责边界

- 官方（管理员发布）与个人（用户自建）**Streamable HTTP** MCP 服务的注册、编辑、删除
- 连接凭证的加密存储与选择（Bearer / 自定义 Headers）
- 服务端发起连接、工具发现（`tools/list`）与工具快照持久化
- 工具**发布策略**（Tool Visibility）：控制哪些远程工具向 Agent 暴露
- 跨 worker 的目录版本号（config version）用于缓存失效
- 连接测试探针（管理员探测官方服务、用户探测个人服务）
- **不负责**：工具**执行**是否放行（属 `tool-permission-spec.md`）、Agent 侧工具装配细节（属 `config-spec.md`）

> 仅支持 `streamable-http` 传输；不支持 stdio / SSE / websocket。

## 2. 数据模型

所有表定义见 [src/api/models/mcp.py](../../src/api/models/mcp.py)。凭证与目录元数据物理分表，避免任何序列化路径意外泄漏密钥。

### `mcp_servers` — 服务定义
- `source`: `official` | `personal`；约束：official 必须 `owner_user_id IS NULL`，personal 必须非空
- `status`: `draft` | `published` | `disabled`（仅 official 有意义；个人服务恒为可用）
- `auth_type`: `none` | `bearer` | `headers`
- `allow_private_network` / `allow_insecure_http`: **仅官方服务**可逐服务放开；个人服务恒为 `false`，其例外统一由全局个人 MCP 网络白名单裁决
- `required`: 官方服务是否强制为所有用户装配
- `version`: 单调递增，凭证/配置变更时自增，用于指纹与缓存
- `last_tested_at` / `last_tools_count` / `last_error`: 平台探针结果（官方服务无 per-user 快照）
- `description`: 连接能力摘要，由 §4.3.3 的生成器根据连接名与当前已发布的工具集派生；用户与管理员均不可编辑。创建与官方编辑 payload 中的 `description` 被忽略（兼容旧客户端），个人编辑为 `extra="forbid"`，传入返回 422
- `description_source_hash`: 生成当前摘要所依据的来源指纹 `sha256(连接名, 工具集)`（固定 64 字符）；NULL 表示尚未生成
- 唯一性：official 按 `name` 全局唯一；personal 按 `(owner_user_id, name)` 唯一

### `mcp_credentials` — 加密凭证
- `user_id IS NULL` 表示管理员配置的平台凭证；非空表示某用户对某服务的凭证
- `encrypted_secret`: 经 `secret_crypto.encrypt_secret`（Fernet v1 信封）加密；明文为 `bearer_token` 或 headers JSON
- 唯一性：`(server_id, user_id)` 唯一；平台凭证（user_id NULL）每服务至多一条

### `mcp_installations` — 用户装配
- `(server_id, user_id)` 唯一；`enabled` 表示是否向该用户的 Agent 暴露
- `credential_id`: 指向所选凭证（`ondelete=SET NULL`）
- `network_authorization_json`: 最近一次成功激活时命中个人网络白名单的非敏感证据（scheme、hostname、固定解析地址和命中规则）；普通公网 HTTPS 为 NULL

### `mcp_personal_network_policies` — 个人 MCP 网络白名单
- 单例 `scope_key=global`，保存规范化的域名后缀与 IPv4/IPv6 CIDR 列表、策略版本及更新人
- 域名后缀按 DNS 标签边界匹配自身和全部子域；域名统一 IDNA 小写，CIDR 必须为 canonical network，合计最多 100 条

### `mcp_tool_visibility` — 发布策略（按 installation）
- 无行 = 发布全部远程工具
- `enabled_tools_json` NULL = 无允许列表（默认全发）；`"[]"` = 不发布任何工具
- `disabled_tools_json` 停用列表**始终优先**
- `revision`: 乐观并发版本，保存时需 `expected_revision` 匹配

### `mcp_tool_snapshots` — 工具快照（按 installation）
- `(installation_id, tool_name)` 唯一
- `schema_hash`: 工具 input schema 的哈希，用于权限条件绑定
- `connection_fingerprint`: 产出该快照的端点/凭证目标指纹；**NULL 为遗留快照，永不用于回退执行**

### `mcp_tool_search_indexes` — 派生检索索引（按 installation）
- `(installation_id, tool_name)` 为稳定主键；与 installation 级联删除，但与执行热路径的快照表物理分离
- `search_document` 仅由有界的工具名、标题、连接名、连接说明和工具说明组成；不包含 URL、凭证或 input schema
- `search_document_hash` + `schema_hash` + `connection_fingerprint` 绑定索引内容与当前可执行工具身份；任一变化都会原子清空旧向量
- `embedding_model_fingerprint` + `embedded_document_hash` 防止模型切换或旧文档向量被复用
- `claim_token` / `lease_expires_at` / `retry_after` 用于跨 worker 的索引生成租约和失败退避；迟到结果必须通过 token 与全部身份条件后才可写入

### `mcp_config_versions` — 目录代次
- `scope_key`: `"global"` 或 `"user:<user_id>"`；单键设计规避不同数据库 NULL 唯一性差异

## 3. API 契约

### 3.1 用户端 `/api/mcp`（需登录）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/servers` | 列出该用户可见的官方 + 个人服务，返回 `{servers, config_version}` |
| POST | `/servers` | 以停用状态创建个人服务（201）；`description` 被忽略 |
| PATCH | `/servers/{id}` | 编辑个人服务（`extra="forbid"`，传 `description` 或网络策略/发布位返回 422） |
| DELETE | `/servers/{id}` | 删除个人服务 |
| PUT | `/servers/{id}/connection` | 暂存凭证或停用连接；普通启用仅接受当前指纹已有有效快照的兼容请求 |
| POST | `/servers/{id}/test` | 服务端发起连接测试，返回 `McpTestResponse` |
| POST | `/servers/{id}/activate` | 对当前或请求中拟提交的凭证执行发现，并原子写入快照与启用状态 |
| GET | `/servers/{id}/tools` | 列出快照工具与发布状态 |
| PUT | `/servers/{id}/tools/visibility` | 替换发布策略，需 `expected_revision` |
| POST | `/import` | 批量导入（`mcpServers` 对象，≤100 个）；条目中的 `description` 仅为兼容旧导出文件而接受，不写入。导入逐项串行发现，耗时随条目数增长，前端对该请求使用 600 秒超时（与 `deploy/docker/Dockerfile.frontend` 中 nginx 的 `proxy_read_timeout` 一致） |
| GET | `/export` | 导出个人服务（**不含**凭证明文；`description` 为生成的能力摘要） |

### 3.2 管理员端 `/api/admin/mcp`（需管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/servers` | 列出全部官方服务 |
| POST | `/servers` | 创建官方服务（201）；`description` 被忽略 |
| PATCH | `/servers/{id}` | 编辑官方服务（含 status/网络策略/required）；`description` 被忽略 |
| DELETE | `/servers/{id}` | 删除官方服务 |
| POST | `/servers/{id}/test` | 平台探针测试 |
| GET | `/personal-network-policy` | 读取个人 MCP 域名/CIDR 白名单 |
| PUT | `/personal-network-policy` | 原子替换白名单，并返回受影响停用连接数 |

### 3.3 关键响应字段

- `McpServerResponse.credential_set`: 是否已配置凭证（**凭证明文永不回显**）
- `McpServerResponse.header_names`: 仅回显 header 键名，不含值
- `McpTestResponse`: `{ok, tools_count, latency_ms, error}`
- `McpToolListResponse`: `{server_id, installation_id, tools_count, enabled_tools_count, enabled_tools, disabled_tools, visibility_revision, tools[]}`

### 3.4 凭证输入契约（`_CredentialInput`）

- `bearer_token` 与 `headers` 不能同时设置
- `bearer_token` 仅当 `auth_type=bearer`；`headers` 仅当 `auth_type=headers`
- `headers` 经 `validate_mcp_headers` 校验（禁止危险头 / 控制字符）
- `clear_credential=true` 清除已存凭证

## 4. 行为语义与不变量

### 4.1 安全（SSRF 与凭证）
- 个人服务默认强制公网 HTTPS；命中管理员域名后缀或 CIDR 白名单时才允许私网地址与 HTTP。空白名单保持原有公网 HTTPS 行为
- 域名规则（如 `company.cc.com`）匹配自身及 `*.company.cc.com`，不匹配 `evilcompany.cc.com`；CIDR 规则按每个实际解析地址裁决
- HTTP 例外要求逐个解析地址满足：命中 CIDR（无论该地址公网与否，视为管理员显式放行），或命中域名后缀且该地址本身为私网地址。域名命中但解析到公网地址时，仍强制该地址走 HTTPS，不获得明文 HTTP 例外
- localhost、云元数据、loopback、link-local、multicast、unspecified、保留地址以及 IPv6 transition/mapped 地址是个人服务不可覆盖的永久拒绝范围
- 连接前 DNS 解析 + `_PinnedNetworkBackend` 固定已解析地址，防 DNS 重绑定
- 官方服务可由管理员显式放开 `allow_private_network` / `allow_insecure_http`
- **凭证绑定 origin**：编辑服务（官方与个人一致）时，凡 `scheme` / `hostname` / 有效端口（origin）发生变化，即清除该服务已存的全部凭证（平台凭证与所有 per-user 覆盖），避免旧凭证被发往新主机；仅路径变化视为同 origin，保留凭证。同请求内若同时提供新凭证，则先清旧再装新。
- 所有 MCP 异常经 `sanitize_mcp_exception` 脱敏：精确替换全部 header 值 + Bearer/URL 正则 + 压平控制字符
- MCP 工具结果转为文本时，audio、embedded resource、未来未知块及 `structuredContent` 的 JSON fallback 必须递归剥离所有层级的隐藏 `_meta`；显式 text block 正文保持原样
- 凭证以 `MCP_SECRET_KEY` 派生密钥加密；`DEBUG=false` 时该密钥必配、≥32 字符且不得等于 `AUTH_SECRET_KEY`（见 [config-spec.md](config-spec.md)）

### 4.2 工具发布（Visibility）
- 停用列表 > 允许列表：同名同时出现时不发布
- `enabled_tools=None` = 发现即发布；`[]` = 不发布任何
- 名称为精确、大小写敏感的远程标识；未知名允许存在以便重发现后恢复
- 保存需 `expected_revision` 匹配当前 `revision`，否则 409（前端会重载最新策略）

### 4.3 目录与缓存
- 官方目录变更自增 `global` 代次；个人变更自增 `user:<id>` 代次
- 个人网络白名单变化自增 `global` 代次，并纳入个人 installation 的 execution fingerprint；激活探测前后策略版本不一致时拒绝迟到结果
- Agent 池按代次失效；DB 代次为跨 worker 权威源，本地驱逐仅加速可见性
- 目录构建按 `(server_id, installation_id)` 稳定排序并分为 required、optional 两阶段；required 优先发现和计入预算，只有 required 集合自身违反 installation 数、总 deadline、工具数或累计字节限制时才抛 `McpRequiredServerUnavailable`
- optional 服务按稳定顺序整服务加入目录；发现失败、超时、模型工具名碰撞或加入后超过工具数/字节预算时，仅跳过该 optional 服务并记录脱敏 error，不得拖垮已验证的 required 目录
- 网络发现完成、写缓存前再次采样 config generation 并重读完整 effective-installation 集合；前后不一致即拒绝混合代际目录。比较时忽略连接说明（§4.3.3 的摘要可能由本次构建中较早完成的发现在后台写回），最终 `connections` 取重读后的说明
- 成功贡献至少一个可见工具的连接会附带紧凑路由摘要（连接名 + §4.3.3 的能力摘要），由 Agent 仅在请求级 system 副本中列出；不得包含 URL、凭证或远端工具 schema，也不得写入长期消息历史

#### 4.3.1 `mcp_tool_search` 混合检索
- 候选集合的唯一权威来源是当前 Agent 已装配且 exposure 为 `DEFERRED` 的工具；数据库中的旧快照、已隐藏工具、其他用户或其他 Agent 的索引行不能自行成为候选
- 精确 `model_name` 始终优先；自然语言 query 使用 jieba 搜索模式分词后的字段加权 BM25（sparse）与 pgvector 余弦相似度（dense）并行排序，再以加权 RRF 融合。关键词匹配采用部分召回，不要求所有词同时命中
- 通用检索层不得写入特定 MCP、行业、工具或自然语言意图词表，也不得以业务关键词白名单/黑名单过滤候选；新 MCP 仅依赖其注册名称、连接说明、工具标题和工具描述参与同一套检索
- 当前 RRF 参数由三组 Recall@5 样本联合网格搜索得到：`k=10`、sparse 权重 `0.25`、dense 权重 `1.0`；sparse 主要提升与 dense 同时命中的精确候选，避免长工具说明仅凭词频垄断 Top 5
- 调用 embedding 前先校验 Agent 的 MCP catalog fingerprint，并按当前用户/session 批量过滤 DENY，以及非交互 Agent 无法处理的 ASK；异步检索返回后再次校验目录代次与权限，且丢弃不属于原候选集合的任何 ID，最后才应用 `limit` 并激活工具
- 快照事务只同步派生索引的文档与身份，不在锁内发起网络请求；目录注册或刷新成功后，后台任务预热 jieba 工具元数据缓存，并通过 `FOR UPDATE SKIP LOCKED` 租约生成缺失的工具文档向量，每个 embedding 请求最多 8 个文档，未变化的向量可跨快照刷新保留
- 在线搜索不重新生成工具文档向量；只有完整索引可用时才为当前 query 生成一次 embedding 并执行 dense 检索。embedding 使用配置的共享服务，因此其数据边界与记忆语义检索相同
- embedding 未配置、索引尚未完成、API/向量数据库失败或结果不合法时，稳定降级为纯关键词排序；向量写入不递增 MCP config version，避免 Agent 重建风暴

#### 4.3.2 本轮优先数据连接
- `preferred_mcp_server_ids` 只从当前 Agent 已装配的 `McpCatalogSnapshot.connections` 解析；不得从陈旧 DB installation、管理页列表或其他用户目录补入候选。该 summary 仅包含成功贡献至少一个当前可见工具的连接。
- 选择是请求级软路由偏好，不过滤 MCP 目录、候选集合、工具权限或审批，也不改变 execution fingerprint。未选择时维持默认联网和现有自动 `mcp_tool_search` 行为。
- Web Adapter 把 MCP server ids 与 Skill keys 合并为唯一 `bsbox.turn_preferences.v1`；provider 请求只收到一个紧凑 `<ui_context>`。请求相关且暴露 `mcp_tool_search` 时，Agent 首次检索应把首选连接展示名带入 query；首选连接无合适工具时允许回退其他启用连接。
- direct Round 固化 `preferred_mcp_connections=[{server_id, display_name}]`，`RUN_STARTED.preferredMcpConnections` 与 `history/v2` 投影同一快照。该快照不是远程调用审计；只有真实 MCP 工具调用成功后才可声称连接已使用，成功 discovery 不算使用。
- Interaction 持久化原 requested context；resume 按当时 catalog 重新解析运行偏好。waiting 期间停用、删除或发现失败的连接不再参与 continuation，但不得改写原 Round 展示快照。
- 无效/不可用 server id 被忽略而不使发送失败；首选连接没有检索结果时继续执行普通跨连接检索。客户端不得提交 installation id、凭证或展示名。

#### 4.3.3 连接能力摘要
- 不变量：`mcp_servers.description` = f(连接名, 当前已发布的快照工具集)。它是 §4.3 路由摘要与 §4.3.1 检索文档中"连接说明"的唯一来源。该不变量允许延迟收敛：个人连接改名时立即按现有快照重算；官方连接没有平台快照，改名后旧摘要保留到下一次管理员测试
- 单一触发点：任何写快照或改变其发布范围的路径在提交之后都只调用 `schedule_capability_summary(server_id)`，包括 `POST /servers/{id}/test`、`POST /servers/{id}/activate`（含导入逐项激活）、运行时目录发现（Agent 构建与 300 秒刷新）、个人连接改名以及个人连接的 `PUT /servers/{id}/tools/visibility`。因此老连接在下一次被模型使用时自动补齐摘要，远端工具变化后也会自动重算；探测失败不触发
- 官方服务没有平台凭证下的快照，只由管理员 `POST /admin/mcp/servers/{id}/test` 把本次探针发现的工具清单连同该探针在事务内写入的 `last_tested_at` 传入生成；用户凭证发现到的工具可能含私有数据，运行时与用户探针对官方服务一律不触发
- 来源与指纹：个人服务读 owner installation 的 `mcp_tool_snapshots` 并按 §4.2 发布策略过滤（摘要只描述模型实际能用的工具）；官方服务用探针传入的全部工具（发布策略按用户生效，共享说明无法按人裁剪）；工具按名称排序后连同连接名计算 `description_source_hash`，与已存值相同则不调用模型。快照为空或工具全部被隐藏时不生成，也不清空：此时该连接不会出现在路由摘要里（§4.3 只列出贡献了可见工具的连接），卡片上保留的旧摘要只是展示残留，下次有可见工具时按新指纹重算。提示词最多含 80 个工具、单条工具说明截 300 字
- 模型：`get_model_registry().get_default()`（即模型设置中的默认模型）。摘要是归纳任务，不需要推理：对 OpenAI 协议、`thinking_wire_format` 能编码开关且（若声明了推理等级列表）包含 `off` 的模型，以 `thinking_mode=disabled` 且不带 `reasoning_effort` 调用；其他模型（Anthropic、`thinking_wire_format=none`、等级列表里没有 `off`）沿用目录配置原样调用，不得因改写配置而让生成一直失败。`current_run_context` 置空，不继承任何会话的推理覆盖。失败不写指纹，下一次发现自然重试；重试频率由 300 秒目录刷新与"仅使用中的连接才发现"限定，不另加退避
- 输出：一行中文，固定格式「{一句话用途}。能做：{3–6 个动词短语}。适合：{3–5 个提问关键词}。」；写回前压成一行并截到 500 字（与请求级 system 提示块的截断一致，检索文档截 1024 字节）
- 后台执行：生成是后台任务，请求路径不等待；同一 server 同时只跑一个，运行中再来的调度在结束后重放一次；进程内模型并发上限 2，单次预算 20 秒（`SUMMARY_TIMEOUT_SECONDS`）。模型调用期间不持任何数据库锁
- 写回：`FOR UPDATE NOWAIT` 锁 server 行（后台任务与其余同步数据库代码一样跑在事件循环上，绝不等待行锁；行被编辑、探针或另一次摘要占用时立即失败并记日志，下一次发现按指纹重试），然后校验来源仍是当前来源：个人服务重新计算「当前连接名 + 快照」指纹并与生成所用指纹比对，官方服务重新计算「当前连接名 + 探针工具」指纹并要求 `last_tested_at` 仍是该探针的代次；不一致（期间工具集、连接名已变或有更新的探针）或已被同指纹结果写过即放弃，迟到的旧结果永不覆盖更新来源的摘要；否则只写 `description` 与指纹
- 摘要是路由文本而非执行配置：写回**不**抬升 `version` 或目录代次，因此不会使正在构建的目录判为 stale，也不会让运行中 Agent 的 `mcp_tool_search` 拒绝当前目录；新摘要最迟在下一次 300 秒目录刷新随 Agent 重建进入路由摘要，并在该次快照写入时进入检索文档（与 §4.3.1 向量写入不递增代次的原则一致）
- 失败：模型未配置、调用失败、超时或返回空文本时只记录日志（仅异常类型，不含异常正文，避免网关回显的请求文本或密钥进入日志）并保留旧说明；下一次发现按指纹重试

### 4.4 快照与执行绑定
- 每次成功发现刷新 `mcp_tool_snapshots`（`schema_hash` + `connection_fingerprint`）
- `connection_fingerprint` 为 NULL 的遗留快照不可用于执行回退
- `schema_hash` + `connection_fingerprint` 构成权限「记住选择」的绑定条件（见 tool-permission-spec §4.3）
- `tools/list` 网络 I/O 不持数据库锁；返回后按 server → installation 顺序获取行锁，锁内重新解析当前有效 server/credential/installation 并比较 discovery 时的 `execution_fingerprint`
- `replace_tool_snapshots` 是 CAS：仅当前指纹与全部待写快照身份一致时替换并返回 `true`；连接在 discovery 期间变化则返回 `false`，调用方必须抛 stale，且不得返回或缓存迟到的旧目录
- 每次 `tools/call` 使用独立 Streamable HTTP session。连接、TLS 或 initialize 在 dispatch 前失败时按配置进行指数退避尝试：默认共 3 次、最多 10 次（均含首次尝试），并始终受外层 discovery / call 墙钟期限约束；进入 `session.call_tool` 后即视为可能已发送，响应 SSE 断开、超时或取消均不得重放业务调用
- transport 退出与 HTTP client 关闭使用有界清理；断流 session ID 不得被后续调用复用。下一次调用必须重新解析 DNS、建立连接并 initialize
- `tools/list` 是只读发现操作，响应流中断时可用全新 session 整体重试；只有完整成功的结果才能进入快照 CAS
- Agent 冷重建后若模型调用当前目录中存在但未激活的 deferred 工具，允许按精确名称复用同一发现、目录新鲜度与权限检查来恢复；触发恢复的旧调用仍被阻止，完整 schema 只从下一模型步骤开始暴露

### 4.5 测试探针的并发一致性
- 探针发起前提交并释放行锁，**绝不跨网络 I/O 持有锁**
- 探针返回后先重新计算目标指纹（server + credential），与探测前指纹不一致则丢弃结果并返回「配置在测试期间已变化」
- 写回 `last_tested_at` / `last_error` / 工具快照前，持有与写路径一致的锁直到提交：管理员探针锁官方服务行；用户探针先锁用户配额行，再严格按 server → installation 的顺序获取 `FOR UPDATE` 行锁，并在锁内重新解析有效凭证、重算 `execution_fingerprint`。用户配额锁与个人配置/connection 写入共用序列化点，server 行锁同时覆盖不获取用户配额锁的官方配置变更；server → installation 顺序也与 runtime 快照 CAS 一致，避免锁反转。任一目标行消失或指纹变化时丢弃迟到结果，以关闭「指纹校验通过后、提交前」并发修改导致陈旧快照写入新配置的窗口
- 个人路径的快照写入置于 SAVEPOINT（`begin_nested`）内：快照持久化失败时仅回滚 SAVEPOINT，保留外层事务与配额锁，随后仍在同一把锁下把脱敏错误写回 `last_error` 并提交；**绝不**用裸 `rollback` 丢锁后再重查写回

### 4.6 前端交互一致性
- 用户启用可选连接时，卡片开关、官方连接设置、个人 MCP 新建/编辑必须采用同一原子激活入口：服务端先对当前执行目标执行 `tools/list`，再在同一用户配额锁、服务锁和 installation 锁下复核执行指纹、持久化快照并提交 `enabled=true`。配置在探测期间变化时整次激活失败，连接保持关闭；`POST /test` 只负责诊断和刷新快照，不承担启用提交。
- 普通创建、编辑、connection 更新与导入接口不得绕过上述约束制造“已启用但未发现”的连接。导入默认启用项时也必须先以关闭状态落库，再逐项原子激活；探测失败的项目保留为关闭状态并返回明确错误。连接目标变化后，卡片计数、工具管理、权限清单与运行时都只能读取当前执行指纹的快照，过期快照一律按待重新发现处理。
- 激活或测试进行中，同一连接的启停、测试、编辑、工具发布与删除入口互斥；激活中的卡片明确显示“正在连接并发现工具”，不得先乐观渲染为已启用。
- 官方 connection 的 `auth_type` 由平台服务定义，用户端只读展示并始终提交服务当前类型；只有个人服务和管理员服务定义可编辑认证方式
- 已有凭证时，仅在 origin 与 `auth_type` 均未变化时提示“留空保留”；origin/auth 改变时明确提示旧凭证会被清除，若新类型仍需认证则保存前要求重新输入
- MCP 编辑、工具发布或删除请求保存中，所有 X、背景、取消和 Escape 关闭入口均禁用；不能向用户表示已放弃而让服务端请求继续提交
- MCP 连接、测试、导入、删除、启停及发布策略 mutation 成功后使权限工具目录失效；权限页面使用请求序号防止较慢旧响应覆盖新目录
- 用户连接列表与管理端目录列表同样采用单调请求序号：仅最后发起的请求可以更新列表、错误与 loading 状态，迟到响应不得覆盖新数据（管理端目录与白名单分用两个序号，见下文摘要刷新一条）
- 导入结果分为全部成功（success）、部分成功（warning）和全部失败（error），不得把失败列表包装为绿色成功状态
- 个人 MCP 新建/编辑与管理端服务表单不提供说明输入；卡片与目录列表单行展示生成的能力摘要并以 `title` 提供全文，为空时个人连接提示测试或启用后自动生成，官方连接提示由管理员测试连接后生成
- 摘要在后台生成，列表响应不等待它：用户端在测试、启用（含连接设置内启用）、仅改名保存、个人连接保存工具发布设置或导入成功后，管理端在测试成功后，于 5、12、25、45 秒各重新拉取一次列表（用户端该刷新为静默刷新：不进入加载态、不清除正在显示的操作提示、失败不产生提示；离开保活的连接页即取消剩余刷新）（覆盖 20 秒单次预算、一次重放与并发 2 下的短暂排队；导入很多条时更长的排队由用户手动刷新兜底），新摘要和改名后重算的摘要由此出现在卡片上；再次操作重置计时。用户端与管理端的静默刷新在有全量加载在途时直接跳过，不抢占用户发起的加载的请求序号。管理端目录列表有独立请求序号，由全量加载和目录刷新共用，迟到的任一方都不得覆盖更新的列表；白名单、其错误与 loading 只属于全量加载，用另一个序号。测试连接返回后只做用户发起的目录刷新（不让路、失败报错；探针最长 15 秒，期间在白名单里输入的草稿不得被回填清掉，在途全量加载稍后返回的白名单与 loading 收尾也照常生效）；后台摘要刷新同样只拉目录，但在全量加载在途时让路且失败静默
- 用户端和管理端的 MCP 成功及错误反馈统一显示 3 秒并可手动关闭，遵循 frontend-spec §4.5.1 的暂停规则；部分成功/未启用警告保留手动关闭。连接测试失败必须使用错误视觉与 `role="alert"`，不得复用绿色成功 toast；提示隐藏不改变连接测试结果或发布限制。
- 数据连接是 `/connections` 一级页面，不归设置中心所有；首次访问后页面实例保活。离开该页面时必须清理一次性 MCP 反馈，返回时不得重新展示上一次操作结果；存在未保存编辑时，站内 PUSH/Back/Forward 与浏览器关闭均必须先拦截确认
- 管理端同一 server 的测试、保存、启停、删除互斥；pending 状态按请求集合计数，较早请求结束不得提前解锁仍在途操作。编辑表单 dirty 时，关闭 drawer 或切换管理模块前必须确认
- 管理端白名单按行编辑域名后缀与 CIDR。收紧策略时用已保存的固定解析证据重新裁决：仅停用失去全部授权的个人连接，保留服务与凭证；替代规则仍覆盖时保持启用。实时 DNS 漂移在测试、发现或调用入口再次 fail-closed 并原子停用
- 对话框打开时将焦点限制在框内；焦点落在容器本身或框外时，Tab / Shift+Tab 分别回到首个 / 末个可聚焦元素，关闭后把焦点还给触发元素

## 5. 失败模式

| 场景 | 行为 |
|---|---|
| 个人服务填未命中白名单的内网/HTTP URL | 创建、激活或连接校验失败，返回 4xx，错误已脱敏 |
| 管理员收紧个人网络白名单 | 仅停用失去授权的个人 installation，保留连接定义与凭证；不自动重新启用 |
| 编辑服务改变 origin（scheme/host/port） | 自动清除该服务已存全部凭证，`credential_set=false`，重置测试状态 |
| 连接测试超时/鉴权失败 | `McpTestResponse.ok=false` + 脱敏 `error`，同时落 `last_error` |
| 发现部分失败 | 保留旧快照，不清空发布策略 |
| 迟到 discovery 的 execution fingerprint 已变化 | CAS 拒绝快照，结果不返回、不缓存，后续按新代次重试 |
| optional MCP 发现失败/超时/超预算/名称碰撞 | 跳过该整服务并记录脱敏错误；required 与其他 optional 继续可用 |
| required 集合自身超出目录限制或 deadline | 整体失败并抛 `McpRequiredServerUnavailable` |
| embedding 未配置、超时、返回无效向量或 pgvector 查询失败 | `mcp_tool_search` 降级为字段加权关键词排序，不影响 MCP 目录与工具执行 |
| 检索器返回隐藏、跨用户或不在当前 Agent 目录中的工具 ID | 丢弃该结果；不得展示、激活或执行 |
| embedding 等待期间 MCP catalog fingerprint 变化 | 本次不返回或激活旧 Agent 中的 MCP 工具，等待 Agent 按新目录重建 |
| 连接 / TLS / initialize 在 dispatch 前失败 | 新 session 指数退避重试；耗尽后返回“连接暂时不可用，可安全重试” |
| `tools/call` dispatch 后响应 SSE 中断 | 关闭并丢弃当前 session，返回 outcome unknown，绝不自动重试该调用；后续调用使用全新 session |
| `tools/list` 响应中断 | 丢弃本次不完整结果，以全新 session 有界重试，不写半截快照 |
| Agent 冷重建后调用已知 deferred 工具 | 精确再发现成功则下一步骤恢复；隐藏、DENY、已删除或旧目录工具继续不可用 |
| `expected_revision` 不匹配 | 409，前端重载后需重新编辑 |
| 凭证损坏无法解密 | 该 installation 标记 configuration_error，工具不暴露（fail-closed） |
| 导入超过 100 个 | 校验失败，整体拒绝 |
| 能力摘要生成失败或超过 20 秒预算（默认模型未配置、调用异常、空返回、过慢） | 保留旧 `description`，已提交的激活/测试结果不受影响；下次发现按指纹重试 |
| 生成期间工具集或连接名又变化 | 写回时来源指纹不一致即放弃；运行中收到的新调度在结束后重放一次，按新来源生成 |
| 单个导入项写发布策略失败 | 该项服务与发布策略在同一事务内回滚（每项一个事务），不留孤儿服务；名称保持可用以便重试；其余项目不受影响 |

## 6. 测试锚点

- [tests/test_mcp_catalog.py](../../tests/test_mcp_catalog.py)：目录 CRUD、发布策略、导入导出、凭证不回显
- [tests/test_mcp_runtime.py](../../tests/test_mcp_runtime.py)：连接、发现、指纹、并发、取消
- [tests/test_mcp_security.py](../../tests/test_mcp_security.py)：SSRF、header 校验、异常脱敏
- [tests/test_tool_search_hybrid.py](../../tests/test_tool_search_hybrid.py)：BM25、向量语义命中、RRF 与故障降级
- [tests/test_tool_exposure.py](../../tests/test_tool_exposure.py)：候选范围、权限前后校验、limit 与会话激活
- [tests/test_mcp_summary_service.py](../../tests/test_mcp_summary_service.py)：能力摘要按快照派生、来源指纹守卫、输入边界、输出规整与后台调度去重
