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
- `allow_private_network` / `allow_insecure_http`: **仅官方服务**可由管理员放开；个人服务恒为 `false`（公网 HTTPS）
- `required`: 官方服务是否强制为所有用户装配
- `version`: 单调递增，凭证/配置变更时自增，用于指纹与缓存
- `last_tested_at` / `last_tools_count` / `last_error`: 平台探针结果（官方服务无 per-user 快照）
- 唯一性：official 按 `name` 全局唯一；personal 按 `(owner_user_id, name)` 唯一

### `mcp_credentials` — 加密凭证
- `user_id IS NULL` 表示管理员配置的平台凭证；非空表示某用户对某服务的凭证
- `encrypted_secret`: 经 `secret_crypto.encrypt_secret`（Fernet v1 信封）加密；明文为 `bearer_token` 或 headers JSON
- 唯一性：`(server_id, user_id)` 唯一；平台凭证（user_id NULL）每服务至多一条

### `mcp_installations` — 用户装配
- `(server_id, user_id)` 唯一；`enabled` 表示是否向该用户的 Agent 暴露
- `credential_id`: 指向所选凭证（`ondelete=SET NULL`）

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
| POST | `/servers` | 创建个人服务（201） |
| PATCH | `/servers/{id}` | 编辑个人服务（`extra="forbid"`，禁止改网络策略/发布位） |
| DELETE | `/servers/{id}` | 删除个人服务 |
| PUT | `/servers/{id}/connection` | 更新凭证与 `enabled`（官方/个人通用） |
| POST | `/servers/{id}/test` | 服务端发起连接测试，返回 `McpTestResponse` |
| GET | `/servers/{id}/tools` | 列出快照工具与发布状态 |
| PUT | `/servers/{id}/tools/visibility` | 替换发布策略，需 `expected_revision` |
| POST | `/import` | 批量导入（`mcpServers` 对象，≤100 个） |
| GET | `/export` | 导出个人服务（**不含**凭证明文） |

### 3.2 管理员端 `/api/admin/mcp`（需管理员）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/servers` | 列出全部官方服务 |
| POST | `/servers` | 创建官方服务（201） |
| PATCH | `/servers/{id}` | 编辑官方服务（含 status/网络策略/required） |
| DELETE | `/servers/{id}` | 删除官方服务 |
| POST | `/servers/{id}/test` | 平台探针测试 |

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
- 个人服务：**强制公网 HTTPS**，禁止私网/内网地址与明文 HTTP
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
- Agent 池按代次失效；DB 代次为跨 worker 权威源，本地驱逐仅加速可见性
- 目录构建按 `(server_id, installation_id)` 稳定排序并分为 required、optional 两阶段；required 优先发现和计入预算，只有 required 集合自身违反 installation 数、总 deadline、工具数或累计字节限制时才抛 `McpRequiredServerUnavailable`
- optional 服务按稳定顺序整服务加入目录；发现失败、超时、模型工具名碰撞或加入后超过工具数/字节预算时，仅跳过该 optional 服务并记录脱敏 error，不得拖垮已验证的 required 目录
- 网络发现完成、写缓存前再次采样 config generation 并重读完整 effective-installation 集合；前后不一致即拒绝混合代际目录
- 成功贡献至少一个可见工具的连接会附带紧凑路由摘要（连接名 + 配置说明），由 Agent 仅在请求级 system 副本中列出；不得包含 URL、凭证或远端工具 schema，也不得写入长期消息历史

#### 4.3.1 `tool_search` 混合检索
- 候选集合的唯一权威来源是当前 Agent 已装配且 exposure 为 `DEFERRED` 的工具；数据库中的旧快照、已隐藏工具、其他用户或其他 Agent 的索引行不能自行成为候选
- 精确 `model_name` 始终优先；自然语言 query 使用字段加权 BM25 与 pgvector 余弦相似度并行排序，再以 RRF 融合。关键词匹配采用部分召回，不要求所有词同时命中
- 调用 embedding 前先校验 Agent 的 MCP catalog fingerprint，并按当前用户/session 批量过滤 DENY，以及非交互 Agent 无法处理的 ASK；异步检索返回后再次校验目录代次与权限，且丢弃不属于原候选集合的任何 ID，最后才应用 `limit` 并激活工具
- 快照事务只同步派生索引的文档与身份，不在锁内发起网络请求；缺失向量由检索服务通过 `FOR UPDATE SKIP LOCKED` 租约渐进生成，单次搜索最多预热 256 个文档、每个 embedding 请求最多 64 个文档，未变化的向量可跨快照刷新保留
- embedding 请求只发送用户 query 与上述有界检索文档；使用配置的 embedding 服务，因此其数据边界与记忆语义检索相同
- embedding 未配置、索引尚未完成、API/向量数据库失败或结果不合法时，稳定降级为纯关键词排序；向量写入不递增 MCP config version，避免 Agent 重建风暴

### 4.4 快照与执行绑定
- 每次成功发现刷新 `mcp_tool_snapshots`（`schema_hash` + `connection_fingerprint`）
- `connection_fingerprint` 为 NULL 的遗留快照不可用于执行回退
- `schema_hash` + `connection_fingerprint` 构成权限「记住选择」的绑定条件（见 tool-permission-spec §4.3）
- `tools/list` 网络 I/O 不持数据库锁；返回后按 server → installation 顺序获取行锁，锁内重新解析当前有效 server/credential/installation 并比较 discovery 时的 `execution_fingerprint`
- `replace_tool_snapshots` 是 CAS：仅当前指纹与全部待写快照身份一致时替换并返回 `true`；连接在 discovery 期间变化则返回 `false`，调用方必须抛 stale，且不得返回或缓存迟到的旧目录

### 4.5 测试探针的并发一致性
- 探针发起前提交并释放行锁，**绝不跨网络 I/O 持有锁**
- 探针返回后先重新计算目标指纹（server + credential），与探测前指纹不一致则丢弃结果并返回「配置在测试期间已变化」
- 写回 `last_tested_at` / `last_error` / 工具快照前，持有与写路径一致的锁直到提交：管理员探针锁官方服务行；用户探针先锁用户配额行，再严格按 server → installation 的顺序获取 `FOR UPDATE` 行锁，并在锁内重新解析有效凭证、重算 `execution_fingerprint`。用户配额锁与个人配置/connection 写入共用序列化点，server 行锁同时覆盖不获取用户配额锁的官方配置变更；server → installation 顺序也与 runtime 快照 CAS 一致，避免锁反转。任一目标行消失或指纹变化时丢弃迟到结果，以关闭「指纹校验通过后、提交前」并发修改导致陈旧快照写入新配置的窗口
- 个人路径的快照写入置于 SAVEPOINT（`begin_nested`）内：快照持久化失败时仅回滚 SAVEPOINT，保留外层事务与配额锁，随后仍在同一把锁下把脱敏错误写回 `last_error` 并提交；**绝不**用裸 `rollback` 丢锁后再重查写回

### 4.6 前端交互一致性
- 官方 connection 的 `auth_type` 由平台服务定义，用户端只读展示并始终提交服务当前类型；只有个人服务和管理员服务定义可编辑认证方式
- 已有凭证时，仅在 origin 与 `auth_type` 均未变化时提示“留空保留”；origin/auth 改变时明确提示旧凭证会被清除，若新类型仍需认证则保存前要求重新输入
- MCP 编辑、工具发布或删除请求保存中，所有 X、背景、取消和 Escape 关闭入口均禁用；不能向用户表示已放弃而让服务端请求继续提交
- MCP 连接、测试、导入、删除、启停及发布策略 mutation 成功后使权限工具目录失效；权限页面使用请求序号防止较慢旧响应覆盖新目录
- 用户连接列表与管理端目录列表同样采用单调请求序号：仅最后发起的请求可以更新列表、错误与 loading 状态，迟到响应不得覆盖新数据
- 导入结果分为全部成功（success）、部分成功（warning）和全部失败（error），不得把失败列表包装为绿色成功状态
- 管理端同一 server 的测试、保存、启停、删除互斥；pending 状态按请求集合计数，较早请求结束不得提前解锁仍在途操作。编辑表单 dirty 时，关闭 drawer 或切换管理模块前必须确认
- 对话框打开时将焦点限制在框内；焦点落在容器本身或框外时，Tab / Shift+Tab 分别回到首个 / 末个可聚焦元素，关闭后把焦点还给触发元素

## 5. 失败模式

| 场景 | 行为 |
|---|---|
| 个人服务填内网/HTTP URL | 创建/连接校验失败，返回 4xx，错误已脱敏 |
| 编辑服务改变 origin（scheme/host/port） | 自动清除该服务已存全部凭证，`credential_set=false`，重置测试状态 |
| 连接测试超时/鉴权失败 | `McpTestResponse.ok=false` + 脱敏 `error`，同时落 `last_error` |
| 发现部分失败 | 保留旧快照，不清空发布策略 |
| 迟到 discovery 的 execution fingerprint 已变化 | CAS 拒绝快照，结果不返回、不缓存，后续按新代次重试 |
| optional MCP 发现失败/超时/超预算/名称碰撞 | 跳过该整服务并记录脱敏错误；required 与其他 optional 继续可用 |
| required 集合自身超出目录限制或 deadline | 整体失败并抛 `McpRequiredServerUnavailable` |
| embedding 未配置、超时、返回无效向量或 pgvector 查询失败 | `tool_search` 降级为字段加权关键词排序，不影响 MCP 目录与工具执行 |
| 检索器返回隐藏、跨用户或不在当前 Agent 目录中的工具 ID | 丢弃该结果；不得展示、激活或执行 |
| embedding 等待期间 MCP catalog fingerprint 变化 | 本次不返回或激活旧 Agent 中的 MCP 工具，等待 Agent 按新目录重建 |
| `expected_revision` 不匹配 | 409，前端重载后需重新编辑 |
| 凭证损坏无法解密 | 该 installation 标记 configuration_error，工具不暴露（fail-closed） |
| 导入超过 100 个 | 校验失败，整体拒绝 |
| 单个导入项写发布策略失败 | 该项服务与发布策略在同一事务内回滚（每项一个事务），不留孤儿服务；名称保持可用以便重试；其余项目不受影响 |

## 6. 测试锚点

- [tests/test_mcp_catalog.py](../../tests/test_mcp_catalog.py)：目录 CRUD、发布策略、导入导出、凭证不回显
- [tests/test_mcp_runtime.py](../../tests/test_mcp_runtime.py)：连接、发现、指纹、并发、取消
- [tests/test_mcp_security.py](../../tests/test_mcp_security.py)：SSRF、header 校验、异常脱敏
- [tests/test_tool_search_hybrid.py](../../tests/test_tool_search_hybrid.py)：BM25、向量语义命中、RRF 与故障降级
- [tests/test_tool_exposure.py](../../tests/test_tool_exposure.py)：候选范围、权限前后校验、limit 与会话激活
