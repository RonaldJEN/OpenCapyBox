# OpenCapyBox API 文档

> **Base URL**: `http://localhost:8000/api`
> **版本**: 0.1.0
> **协议**: AG-UI (Agent User Interaction Protocol)

## 概述

OpenCapyBox 提供 RESTful API 接口，支持用户认证、会话管理、智能对话、模型查询、配置管理和定时任务。

**v0.1.0**: 流式对话 API 采用 AG-UI 协议，提供标准化的事件类型和丰富的状态管理功能。

---

## 目录

- [通用说明](#通用说明)
- [认证 API](#认证-api)
- [会话管理 API](#会话管理-api)
- [对话 API](#对话-api)
- [AG-UI 事件类型](#ag-ui-事件类型)
- [模型管理 API](#模型管理-api)
- [配置管理 API](#配置管理-api)
- [定时任务 API](#定时任务-api)
- [数据模型](#数据模型)

---

## 通用说明

### 认证方式

所有需要认证的接口通过 `Authorization: Bearer <access_token>` 传递身份：

```
GET /api/sessions/list
Authorization: Bearer <access_token>
```

### 错误响应

```json
{
  "detail": "错误信息描述"
}
```

| HTTP 状态码 | 说明                       |
| ----------- | -------------------------- |
| 400         | 请求参数错误               |
| 401         | 认证失败                   |
| 404         | 资源不存在                 |
| 410         | 会话已完成（不可继续对话） |
| 500         | 服务器内部错误             |

---

## 认证 API

### 登录

用户登录认证。

**请求**

```
POST /api/auth/login
Content-Type: application/x-www-form-urlencoded
```

| 参数     | 类型   | 必填 | 说明   |
| -------- | ------ | ---- | ------ |
| username | string | 是   | 用户名 |
| password | string | 是   | 密码   |

**响应** `200 OK`

```json
{
  "user_id": "demo",
  "access_token": "<jwt-token>",
  "token_type": "bearer",
  "expires_in": 43200,
  "message": "登录成功"
}
```

**错误**

| 状态码 | 说明             |
| ------ | ---------------- |
| 401    | 用户名或密码错误 |

---

### 获取当前用户信息

获取当前登录用户的信息。

**请求**

```
GET /api/auth/me
Authorization: Bearer <access_token>
```

| Header        | 类型   | 必填 | 说明      |
| ------------- | ------ | ---- | --------- |
| Authorization | string | 是   | Bearer 令牌 |

**响应** `200 OK`

```json
{
  "user_id": "demo",
  "username": "demo"
}
```

**错误**

| 状态码 | 说明       |
| ------ | ---------- |
| 404    | 用户不存在 |

---

## 会话管理 API

### 创建会话

创建一个新的对话会话。

**请求**

```
POST /api/sessions/create?model_id=<optional_model_id>
Authorization: Bearer <access_token>
```

| 参数       | 类型   | 必填 | 说明                  |
| ---------- | ------ | ---- | --------------------- |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |
| model_id   | string | 否   | 模型 ID（不传则使用默认模型） |

**响应** `200 OK`

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "model_id": "default-model",
  "message": "会话创建成功"
}
```

**说明**

- 创建会话时会尝试创建沙箱并初始化 Agent
- 文件操作在沙箱文件系统中进行（默认目录为 `/home/user`，可通过后端配置修改挂载根目录）

---

### 获取会话列表

获取当前用户的所有会话，可按标题或用户消息正文搜索。

**请求**

```
GET /api/sessions/list?q=<optional_search_query>
Authorization: Bearer <access_token>
```

| 参数       | 类型   | 必填 | 说明                  |
| ---------- | ------ | ---- | --------------------- |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |
| q         | string | 否   | 搜索词；匹配会话标题和讨论内容 |

**响应** `200 OK`

```json
{
  "sessions": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "user_id": "demo",
      "status": "active",
      "title": "新会话",
      "created_at": "2025-01-14T10:30:00",
      "updated_at": "2025-01-14T10:35:00",
      "model_id": "default-model",
      "match_type": "assistant",
      "match_excerpt": "Agent 回复里提到了 PostgreSQL 部署和索引方案",
      "match_round_id": "round-001"
    }
  ]
}
```

**搜索说明**

- `q` 为空或全空格时返回完整列表。
- 搜索匹配会话标题、用户消息和 Agent 回复；用户消息以 `rounds.user_message` 的前端可见文本为准，避免匹配 Agent 内部上下文 / 附件提示 / Data URL。
- Agent 回复搜索 `conversation_messages(role=assistant).content`，并查询 `rounds.final_response` 作为历史兜底；搜索不包含 tool / summary / synthetic 内容。
- `match_type` 可能为 `title` / `user` / `assistant`。
- 排序优先级为 title → user → assistant；同级内按更新时间倒序。
- 非空搜索最多返回 50 个 sessions；后端会在 DB 侧为每个 session 取最佳命中并截断候选。
- `match_excerpt` 在用户消息或 Agent 回复命中时返回短摘要。
- `match_round_id` 在命中具体轮次时返回，前端可用于打开会话后定位。
- 搜索使用 PostgreSQL 兼容的轻量 `ILIKE ... ESCAPE`，`%`、`_`、`\` 按普通字符处理。

---

### 获取会话历史

获取指定会话的轮次历史（基于 Round/Step 结构）。

**请求**

```
GET /api/sessions/{chat_session_id}/history/v2
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "rounds": [
    {
      "round_id": "round-001",
      "idempotency_key": "550e8400-e29b-41d4-a716-446655440000",
      "last_event_sequence": 8,
      "user_message": "帮我创建一个 Python 文件",
      "user_attachments": [
        {
          "path": "images/demo.png",
          "name": "demo.png",
          "type": "image/png"
        }
      ],
      "preferred_skills": [
        {
          "key": "pdf",
          "display_name": "PDF 处理"
        }
      ],
      "preferred_mcp_connections": [
        {
          "server_id": "server-uuid",
          "display_name": "东方财富数据"
        }
      ],
      "final_response": "已经为你创建了 hello.py 文件",
      "steps": [
        {
          "step_number": 1,
          "thinking": "用户需要创建一个 Python 文件...",
          "assistant_content": null,
          "tool_calls": [
            {
              "name": "WriteTool",
              "input": {"path": "hello.py", "content": "print('Hello')"}
            }
          ],
          "tool_results": [
            {
              "success": true,
              "content": "文件已创建",
              "error": null
            }
          ],
          "status": "completed",
          "created_at": "2025-01-14T10:30:05"
        }
      ],
      "step_count": 1,
      "status": "completed",
      "created_at": "2025-01-14T10:30:00",
      "completed_at": "2025-01-14T10:30:10"
    }
  ],
  "total": 1
}
```

`preferred_skills` 与 `preferred_mcp_connections` 始终为数组。普通 direct Round 返回发送时解析并持久化的 Skill / MCP 展示快照；任一空选择、全部无效或损坏时独立返回 `[]`。`display_name` 是发送当时的不可变展示名，历史读取不会因后续改名、禁用或删除而重算。

这些字段只说明本轮软偏好，不代表 Skill 已加载或 MCP 已真实调用。same-Round continuation 继续使用原 Round 的冻结快照，不新增额外 Q/A 或审批消息。

`status=waiting_interaction` 表示该 Round 暂停等待 `ask_user` 回答或工具审批，不是终态。此时 `interrupt` 由 pending `agent_interactions` 投影，形如 `{id, reason, payload}`；刷新后客户端应显示卡片，并用该 Round 的 `round_id` 与 `last_event_sequence` 继续订阅。

---

### 更新会话标题

更新指定会话的标题。

**请求**

```
PATCH /api/sessions/{chat_session_id}/title
Authorization: Bearer <access_token>
Content-Type: application/json
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**请求体**

```json
{
  "title": "Python 开发讨论"
}
```

| 字段  | 类型   | 必填 | 说明                 |
| ----- | ------ | ---- | -------------------- |
| title | string | 是   | 新标题（1-255 字符） |

**响应** `200 OK`

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "demo",
  "status": "active",
  "title": "Python 开发讨论",
  "created_at": "2025-01-14T10:30:00",
  "updated_at": "2025-01-14T10:40:00"
}
```

---

### 删除会话

删除指定会话及其所有相关数据。

**请求**

```
DELETE /api/sessions/{chat_session_id}
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

```json
{
  "message": "会话已删除"
}
```

**说明**

- 会同时删除会话的消息历史
- 清理 Agent 缓存
- 删除前会尝试连接/恢复沙箱并清理挂载目录中的所有用户文件，然后销毁容器。若沙箱已过期不可达，仅删除数据库记录，持久化文件可能残留于宿主机

---

### 获取会话文件列表

获取指定会话沙箱中的文件列表。

说明：会话默认使用持久化挂载目录（默认 `/home/user`，可由后端配置 `sandbox_storage_mount_path` 调整）。当旧 sandbox 被回收后，系统会自动重建新 sandbox 并复用同一会话存储目录，因此后续上传/生成的文件可继续读取。

> 说明：后端在用户沙箱内使用 Python `os.listdir/stat` 枚举指定目录。沙箱连接、目录命令或响应解析失败会强制重连一次；重试仍失败返回 `503`，只有成功读取出的空目录才返回 `200 + files=[]`。

**请求**

```
GET /api/sessions/{chat_session_id}/files
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

```json
{
  "files": [
    {
      "name": "hello.py",
      "path": "hello.py",
      "size": 0,
      "modified": "2025-01-14T10:35:00+00:00",
      "type": "py",
      "is_directory": false
    }
  ],
  "total": 1
}
```

> 说明：`type` 为文件扩展名（如 `py` / `pdf`），不是 MIME type；`modified` 为带时区的 UTC ISO 8601。

---

### 下载/预览文件

下载或预览会话沙箱中的文件。

**请求**

```
GET /api/sessions/{chat_session_id}/files/{file_path}?preview=<bool>&render=<pdf|null>
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                      |
| --------------- | ------ | ---- | ------------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）      |
| file_path       | string | 是   | 文件相对路径（Path 参数） |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |
| preview         | bool   | 否   | 是否预览模式，默认 false  |
| render          | string | 否   | 派生渲染格式；当前仅支持 `pdf`，且要求 `preview=true` |
| edit            | bool   | 否   | Markdown/CSV/XLSX 编辑必须为 true，正文与编辑基线成对读取 |
| base_token      | string | 否   | 配合 edit=true 读取指定不可变基线，接纳自动合并结果 |

**响应**

- `preview=false`: 返回文件流，Content-Disposition 为 attachment
- `preview=true`: 对于可预览文件（文本、图片、PDF），Content-Disposition 为 inline
- `edit=true`：从 Session 隐藏缓存读取不可变正文，返回 `X-Session-File-Revision`、`X-Session-File-Modified`、`X-Session-Edit-Base`；不得将目录 metadata 与另一版本正文拼作编辑基线。
- `preview=true&render=pdf`: DOC/DOCX/PPT/PPTX 在用户 OpenSandbox 内完成有界快照、内容 hash、LibreOffice 转换与原子缓存发布，返回流式 `application/pdf`；API 主机不读取 Office 源内容，转换缓存属于 session 隐藏目录且不出现在文件列表中

**常见错误**

- `404 Not Found`：会话不存在，或文件不存在/不可读
- `400 Bad Request`：文件路径不合法（越界路径）
- `413 Payload Too Large`：Office 源文件超过 50 MiB，或派生 PDF 超过 100 MiB
- `415 Unsupported Media Type`：请求派生渲染的文件类型不受支持
- `422 Unprocessable Entity`：Office 文件损坏或沙箱内转换失败
- `503 Service Unavailable`：沙箱不可用或镜像缺少 Office renderer
- `504 Gateway Timeout`：Office 转换超时

**可预览的文件类型**

- text/* (文本文件)
- image/* (图片)
- application/pdf
- application/json
- application/xml

---

### 保存可编辑的 Session 文件

以乐观版本令牌原子保存当前 Session 内的 Markdown、UTF-8 CSV 或 XLSX。旧二进制 XLS/ET 仅支持只读预览。

**请求**

```http
PUT /api/sessions/{chat_session_id}/files/{file_path}
Authorization: Bearer <access_token>
Content-Type: application/json
```

Markdown 请求体：

```json
{
  "content": "# 报告\n",
  "expected_revision": "v1:10:1787709600000000000",
  "edit_base_token": "<编辑读取响应中的 X-Session-Edit-Base>",
  "save_id": "<本代草稿稳定的 UUID>"
}
```

CSV/XLSX 使用 `content_base64` 传递完整文件字节，并携带相同的 revision/base/save_id。远端内容变化时按原始基线自动三方合并，重叠修改沿用用户草稿优先；`save_id` 重试返回原回执。Agent 运行不阻塞保存。旧客户端不带基线时仅 strict CAS，不能以新 revision 重放旧草稿。

**响应** `200 OK`

返回 `{name, path, size, modified, type, is_directory, revision, edit_base_token, session_auto_merged}`。自动合并后按回执 token 读取固定正文，正文与新基线一并接纳；期间有续写则保留原基线继续保存。Session 基线与回执是有界临时缓存，不创建 Workspace 历史账本。

**常见错误**

- `409 Conflict`：`SESSION_EDIT_RETRY` 表示提交期间再次变化，可重试；基线缺失/非法或结构不可合并必须保留草稿。旧客户端 revision 不符返回 `SESSION_FILE_REVISION_CONFLICT`。
- `413 Payload Too Large`：Markdown 超过 5 MiB，或 CSV/XLSX 超过 20 MiB
- `415 Unsupported Media Type`：文件不是 Markdown、CSV 或 XLSX；XLS/ET 为只读
- `422 Unprocessable Entity`：请求体、UTF-8 CSV 或 OOXML XLSX 结构无效
- `503 Service Unavailable`：沙箱不可用

---

### 检查运行中会话集合

检查用户当前正在运行的会话集合（单次 API 调用，避免 N+1 查询）。当 `AGENT_USER_CONCURRENCY_LIMIT > 1` 时，可能返回多个不同 session。仅返回 `updated_at` 未超过 `SSE_SUBSCRIBE_TIMEOUT` 的新鲜运行 slot。

**请求**

```
GET /api/sessions/running-sessions
Authorization: Bearer <access_token>
```

| 参数       | 类型   | 必填 | 说明                  |
| ---------- | ------ | ---- | --------------------- |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

```json
{
  "running_sessions": [
    {
      "session_id": "550e8400-e29b-41d4-a716-446655440000",
      "round_id": "round-001"
    },
    {
      "session_id": "660e8400-e29b-41d4-a716-446655440000",
      "round_id": null
    }
  ]
}
```

`round_id` 为 `null` 表示该 session 已占用新鲜运行 slot，但仍处于 Agent 初始化窗口、尚未创建 running round。无运行中会话时：

```json
{
  "running_sessions": []
}
```

**使用场景**

- 页面加载时检测是否有未完成的任务
- 多标签页同步状态
- 避免前端遍历所有会话的 N+1 查询问题

---

### 上传文件

上传文件到会话沙箱目录（默认 `/home/user`，可由后端配置调整，且为持久化挂载）。

**请求**

```
POST /api/sessions/{chat_session_id}/upload
Authorization: Bearer <access_token>
Content-Type: multipart/form-data
```

| 参数            | 类型   | 必填 | 说明                    |
| --------------- | ------ | ---- | ----------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）    |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |
| file            | File   | 否   | 上传的文件（Form 参数） |

**响应** `200 OK`

```json
{
  "name": "document.pdf",
  "path": "document.pdf",
  "size": 102400,
  "modified": "2025-01-14T10:40:00",
  "type": "application/pdf",
  "revision": "v1:102400:1736822400000000000"
}
```

**说明**

- 如果文件名已存在，会自动添加序号（如 document_1.pdf）
- 支持任意文件类型

**错误响应**

- `400 Bad Request`：未选择文件（`file` 为空）
- `404 Not Found`：会话不存在
- `503 Service Unavailable`：沙箱不可用（连接/恢复失败）
- `500 Internal Server Error`：文件保存失败

---

## 用户持久工作区 API

所有端点使用 Bearer auth，且只能解析当前用户的 `entry_id`、相对路径和 Session。工作区位于当前 Sandbox Profile 持久挂载的 `workdir`；未启用持久挂载时返回 `503 WORKSPACE_PERSISTENCE_DISABLED`。

### 条目列表与稳定 ID

```http
GET /api/workspace/entries?parent_id=<directory-entry-id>&q=<name-or-path>&cursor=<opaque>&limit=100
GET /api/workspace/entries/{entry_id}
```

列表响应：

```json
{
  "items": [
    {
      "entry_id": "...",
      "parent_id": null,
      "name": "report.md",
      "kind": "file",
      "path": "reports/report.md",
      "size_bytes": 1024,
      "mime_type": "text/markdown",
      "sha256": "...",
      "revision": 3,
      "current_version_id": "...",
      "tree_revision": 1,
      "status": "active",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "next_cursor": null,
  "workspace_revision": 12
}
```

列表只包含 active 条目；跨用户或已删除 ID 返回 404。

### 创建、上传与编辑

```http
POST /api/workspace/directories
POST /api/workspace/files
POST /api/workspace/uploads
PUT  /api/workspace/entries/{entry_id}/content
PATCH /api/workspace/entries/{entry_id}
```

- directory body：`{parent_id,name,idempotency_key?}`。
- file body：`{parent_id,name,file_type:"markdown"|"xlsx",idempotency_key?}`；空 XLSX 包含 `Sheet1`。
- upload 使用 multipart `file,parent_id?,idempotency_key?`，API 以有界块传到沙箱临时文件，不聚合整个文件。
- content PUT 使用原始字节正文，必须携带 `If-Match: "<revision>"`；缺失返回 428。Markdown/TXT 最大 5 MiB，CSV/XLSX 最大 20 MiB。CSV 必须是无 NUL 的有效 UTF-8；XLSX 与 Session 在线编辑共用有界 OOXML/ZIP 关系图校验。无效正文在创建 mutation 或写入沙箱前返回 422 `INVALID_CSV` / `INVALID_XLSX`。
- 有 current version 时同时携带 `X-Workspace-Base-Version`。每次真实内容变化推进 revision/current version，但相同 SHA 返回 `NO_CHANGE`，不创建新对象。
- PATCH body：`{parent_id?,name?,expected_revision,idempotency_key?}`。

变更响应统一为：

```json
{"status":"CREATED|UPDATED|NO_CHANGE|MOVED","entry":{},"mutation_id":"..."}
```

同名不自动加序号。409 `NAME_CONFLICT` / `REVISION_CONFLICT` 的 `detail.entry` 是当前 authoritative 目标，客户端必须据此重新选择、改名或显式覆盖。

普通 Workspace mutation 的幂等身份是 `当前用户 + idempotency_key + operation`。同 operation 重复提交时，`prepared` 返回 `409 MUTATION_IN_PROGRESS` 及 mutation 身份，`completed` 返回第一次 journal 中冻结的成功回执，`failed` 返回第一次冻结的原始失败；同一 key 用于其他 operation 返回 `409 IDEMPOTENCY_KEY_REUSED`。客户端必须为每个新操作意图及每一代新正文生成新 key，只有在请求参数和正文完全未改变且结果仍未确定的网络重试中复用原 key。批量删除和内部 change set 额外校验请求指纹，不能用同一 key 改变删除范围或提案内容。

### Revision、检查点与固定版本

```http
POST /api/workspace/entries/{entry_id}/checkpoint
GET  /api/workspace/entries/{entry_id}/versions
GET  /api/workspace/versions/{version_id}/content
POST /api/workspace/entries/{entry_id}/restore
```

- checkpoint body：`{expected_revision,version_id,checkpoint_kind:"web_idle"|"web_close"|"web_periodic"}`。它只提升已经保存的 current version，不重新上传或复制文件。
- Web autosave revision 继续作为 CAS/三方合并 base，但不自动进入普通历史列表；停止编辑 30 秒、关闭/切换 owner 或持续编辑满 5 分钟时提升 checkpoint。
- 初始创建、Session 导入、Chat/Cron 发布和恢复旧版直接生成 checkpoint。恢复旧版会发布新的 current version，旧记录保持不可变。
- 版本内容按用户内 SHA-256 存入 `.opencapybox/objects/sha256/`；相同用户相同 SHA 只占一份物理内容，不跨用户去重。Round 附件和进行中的 change set 使用显式 content reference 防止 GC。

### 内容预览、下载与 Markdown 相对资源

```http
GET /api/workspace/entries/{entry_id}/content?preview=true
GET /api/workspace/content?path=<relative-posix-path>&preview=true
GET /api/workspace/entries/{entry_id}/content?preview=true&render=pdf
GET /api/workspace/versions/{version_id}/content?preview=true
```

- entry 内容端点只冻结当前 head，带 `ETag: "<revision>"`、`X-Workspace-Revision` 和 `X-Workspace-Version`；未传 `preview=true` 时按附件下载。固定历史版本只通过 version 内容端点读取，使用 version ID 作为 ETag；两个端点都直接流式读取相应不可变对象。
- path 端点只解析当前用户 active metadata，用于 Markdown 相对图片/链接；拒绝绝对路径、`..`、反斜杠和系统目录。
- `render=pdf` 仅支持预览模式，未带 `preview=true` 返回 400；DOC/DOCX/PPT/PPTX 在沙箱内派生 PDF，其他类型返回 415。Workspace 派生文件位于 `.opencapybox/derived/office/`，按源 SHA 和 renderer version 复用并在独立缓存上限内按 LRU 回收；不可变 Workspace 内容先按数据库 SHA/size 查缓存，命中时不再读取源 Office。
- 内容与派生 PDF 都通过 OpenSandbox `read_bytes_stream` 流式响应；流建立失败返回 `SANDBOX_READ_FAILED`，不得静默改为全量内存读取。

### Session 文件存入工作区

```http
POST /api/workspace/imports/session-file
Content-Type: application/json
```

```json
{
  "session_id": "...",
  "source_path": "reports/result.pdf",
  "source_revision": "v1:1024:1736822400000000000",
  "destination_parent_id": null,
  "destination_name": "result.pdf",
  "conflict_policy": "fail",
  "expected_destination_revision": null,
  "idempotency_key": "..."
}
```

服务端重新验证 Session 所属用户、相对路径和 opaque source revision，并在沙箱内复制稳定快照。源文件已变化返回 `412 SOURCE_REVISION_CONFLICT` 与 `current_revision`；同 hash 返回 `NO_CHANGE`；覆盖必须使用 `conflict_policy=overwrite` 和冲突响应中的目标 revision。

### 直接删除

```http
POST /api/workspace/entries/delete-batch
```

单项和批量删除统一使用 `{items:[{entry_id,expected_revision}],idempotency_key}`；最多 200 个选择项，目录覆盖已选后代。删除请求会冻结 items/revision 指纹，同一 key 携带不同范围返回 `409 IDEMPOTENCY_KEY_REUSED`。响应 `{status:"DELETED",mutation_id,affected_entry_ids,root_count,entry_count}`。无单项 DELETE、回收站、恢复删除或 operations 轮询接口。

删除前取得 file/tree claims 并持久化 prepared journal，物理成功后一次性移除 entries、其全部 versions/references/相关提案并结算正式容量。零引用对象立即尝试 GC，并清理该内容 SHA 派生的 Office PDF 缓存；失败由 maintenance 重试。仍被其他文件或版本共享的对象及缓存不误删。删除对象不再受对话引用保留，不提供历史快照兜底。幂等重试返回冻结结果，不会作用于后来创建的同名文件。

---

## 对话 API

### 发送消息（流式）

发送消息并流式返回步骤信息（Server-Sent Events）。

**请求**

```
POST /api/chat/{chat_session_id}/message/stream
Authorization: Bearer <access_token>
Content-Type: application/json
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**请求体**

```json
{
  "idempotency_key": "550e8400-e29b-41d4-a716-446655440000",
  "preferred_skill_keys": ["pdf", "data_analysis"],
  "preferred_mcp_server_ids": ["server-uuid"],
  "pending_file_drafts": [{"source":"session","path":"reports/current.md"}],
  "content": [
    {
      "type": "text",
      "text": "帮我分析这个截图"
    },
    {
      "type": "image_url",
      "image_url": {
        "url": "data:image/png;base64,...."
      },
      "file": {
        "path": "uploads/screenshot.png",
        "name": "screenshot.png",
        "mime_type": "image/png"
      }
    },
    {
      "type": "file",
      "file": {
        "path": "data.csv",
        "name": "data.csv",
        "mime_type": "text/csv"
      }
    }
  ]
}
```

**content block 类型说明**

- `text`: 文本块，字段 `text`
- `image_url`: 图片块，字段 `image_url.url`（支持 URL 或 Data URL）；可选 `file` 元数据用于历史附件预览恢复
- `file`: 附件块，字段 `file.path/name/mime_type`
- `video_url`: 预留类型，当前默认不开放（由模型能力配置控制）

| 字段             | 类型     | 必填 | 说明 |
| ---------------- | -------- | ---- | ---- |
| content          | array    | 是   | 内容块数组（见上方类型说明） |
| idempotency_key  | string   | 否   | 幂等键（UUID），防止同一请求被重复处理。前端自动生成 |
| preferred_skill_keys | string[] | 否 | 本次逻辑执行链优先考虑的 Skill 稳定内部 `key`；最多 50 项，每项最多 128 个字符 |
| preferred_mcp_server_ids | string[] | 否 | 本次逻辑执行链优先考虑的 MCP server id；最多 20 项，每项最多 36 个字符 |
| pending_file_drafts | object[] | 否 | Agent 启动时仍在 outbox 同步的文件身份 `{source:"session"|"workspace",path}`；最多20项，只含相对路径，不含草稿正文 |

两个字段都表达软偏好而非强制调用或权限白名单。服务端按首次出现顺序去重；Skill 按当前启用清单解析，MCP 只从当前 Agent 实际 catalog connections 解析。未知、已删除、已禁用或没有可见工具的项被忽略，偏好不会从前序独立消息继承。未选择 MCP 时仍默认联网并自动路由。

偏好字段合并为内部 `bsbox.turn_preferences.v1`，来自 UI 控件而非用户正文；附件仍是 `content` block，不进入偏好上下文。`pending_file_drafts` 独立生成 user-authority 的 `bsbox.pending_file_drafts.v1` 请求级上下文，提醒 Agent 对相关路径重读并披露可能暂时只能看到最后保存版；服务端不把客户端上报的同步状态提升为系统事实。Agent 不得主动复述偏好选择，且只有 Skill 加载或真实远程 MCP 工具调用成功后才能声称已使用。

普通 direct Round 会分别固化 `preferred_skills` 与 `preferred_mcp_connections`，并由 `RUN_STARTED` 和 `history/v2` 返回。same-Round resume 按当前 registry/catalog 重解析运行偏好，但不改写原展示快照。

有效 key 经 trim 后必须非空且不超过 128 个 Unicode 字符；兼容人类可读 Unicode、空格和括号，禁止 `/`、`\`、`?`、`#`、`%` 及 Unicode `C*` 控制/不可见类别字符。请求数组中的空白项会被忽略，其他非法 key 返回校验错误。

**模型能力限制**

- 仅模型配置中 `supports_image=true` 的模型可接收 `image_url`
- `image_url` 数量不能超过模型 `max_images`
- 不支持图片/超限时返回 4xx 错误
- **图片大小限制**：单张图片 Data URL 上限 20MB，所有图片 Data URL 总量上限 50MB。超过时返回 `400` 错误
- **前端发送原图 Data URL**：图片发送给视觉模型时保留原始 Data URL，避免截图/OCR 场景因 JPEG 压缩降质；体积保护由上述后端大小限制负责

**响应** `200 OK`

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

**事件类型**

流式消息接口采用 AG-UI 协议，详细事件类型说明见 [AG-UI 事件类型](#ag-ui-事件类型) 章节。

**事件流示例**

```
RUN_STARTED → STATE_SNAPSHOT → THINKING_TEXT_MESSAGE_START → THINKING_TEXT_MESSAGE_CONTENT* → THINKING_TEXT_MESSAGE_END → TEXT_MESSAGE_START → TEXT_MESSAGE_CONTENT* → TEXT_MESSAGE_END → TOOL_CALL_START → TOOL_CALL_ARGS → TOOL_CALL_END → TOOL_CALL_RESULT → STATE_DELTA → RUN_FINISHED
```

**并发限制说明**

- 同一用户同一时刻最多允许 `AGENT_USER_CONCURRENCY_LIMIT` 个不同 session 运行，默认 1
- 同一 session 同一时刻仍只允许一个运行中的任务
- 并发限制基于数据库 slot 锁 `UserRunLock` 实现，执行期间通过心跳保活
- 心跳超过 `sse_subscribe_timeout` 未更新时，锁被视为陈旧并自动回收
- 当存在运行中的任务时，接口返回 `429 Too Many Requests`

**幂等冲突（SSE 事件）**

当 `idempotency_key` 对应的 Round 已存在时，不返回 HTTP 错误，而是通过 SSE 推送 `RUN_ERROR` 事件：

```json
{
  "type": "RUN_ERROR",
  "code": "ROUND_IN_PROGRESS",
  "message": "<existing_round_id>"
}
```

前端收到后应使用 `message` 中的 `round_id` 转入 `subscribe` 恢复路径。

若当前 session 已有同一 Round 的 pending Interaction，HTTP 流会返回无 sequence 的 `RUN_ERROR(code=INTERACTION_PENDING)`。它表示本条普通消息未被接受，不是原 waiting Round 的终态；客户端必须恢复未受理草稿，查询 `history/v2` 并继续订阅权威 waiting Round。

**错误响应**

| 状态码 | 说明 |
| ------ | ---- |
| 404    | 会话不存在 |
| 410    | 会话已完成（不可继续发送消息） |
| 429    | 当前用户运行任务数已达上限（并发限制） |
| 503    | 服务暂时不可用（数据库异常等系统错误） |

---

### 订阅轮次更新

订阅运行中轮次的实时更新（用于断线恢复）。

**请求**

```
GET /api/chat/{chat_session_id}/round/{round_id}/subscribe?last_sequence=<int>
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                             |
| --------------- | ------ | ---- | -------------------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）             |
| round_id        | string | 是   | 轮次 ID（Path 参数）             |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |
| last_sequence   | int    | 否   | 客户端已收到的最后事件序列号，默认 0（从头重放） |

**响应** `200 OK`

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

**行为说明**

1. **已终态轮次**（`completed` / `failed` / `cancelled` / `max_steps_reached`）：立即返回补齐事件并关闭连接
2. **运行中的轮次**（`running`）：
   - 先从 `agui_events` 表重放 `last_sequence` 之后的所有已持久化事件
   - 然后注册为订阅者接收后续实时事件
   - 轮次完成时发送 `RUN_FINISHED` 事件并关闭连接
3. **暂停等待的轮次**（`waiting_interaction`）：重放包含 `interaction_requested` 的历史事件后保持订阅；其他标签页回答/审批时会收到同一 runId 的 `interaction_resolved`、后续输出，取消时收到终态
4. **心跳**：每 15 秒发送 `CUSTOM` (heartbeat) 事件防止连接超时
5. **生命周期**：服务端不设置应用级订阅最大时长；连接持续到 Round 终态、客户端断开或基础设施关闭。`SSE_SUBSCRIBE_TIMEOUT` 是 runtime/UserRunLock 陈旧判定阈值，不用于关闭健康订阅

**事件类型**

订阅接口现在支持完整的 AG-UI 事件类型，与流式发送消息接口相同：

| 事件类型                                    | 说明                           |
| ------------------------------------------- | ------------------------------ |
| `MESSAGES_SNAPSHOT`                       | 历史消息快照                   |
| `STATE_SNAPSHOT` / `STATE_DELTA`        | 状态快照/增量更新              |
| `TEXT_MESSAGE_START/CONTENT/END`          | 文本消息流式事件               |
| `THINKING_TEXT_MESSAGE_START/CONTENT/END` | 思维链流式事件                 |
| `TOOL_CALL_START/ARGS/END/RESULT`         | 工具调用流式事件               |
| `STEP_STARTED/FINISHED`                   | 步骤开始/完成事件              |
| `RUN_FINISHED` / `RUN_ERROR`            | 运行完成/错误事件              |
| `CUSTOM`                                  | 自定义事件（心跳、标题更新、`interaction_requested/resolved` 等） |

订阅基础设施异常时可能收到无 sequence 的 `RUN_ERROR(code=SUBSCRIBE_FAILED)`。该事件只结束当前订阅 transport，不能把 Round 标为 failed；客户端应从最后 sequence 重连或回拉 `history/v2`。只有带 durable sequence 的 Round terminal，或 history 权威终态，才能终态化本地 Round。

**使用场景**

- 页面刷新后恢复运行中任务的**实时流式进度**
- 多标签页/多设备同步查看执行状态
- SSE 连接断开后的自动重连

**客户端取消订阅**

当客户端切换会话或关闭页面时，应主动取消订阅以释放后端资源：

```typescript
// 前端使用 AbortController 取消订阅
const subscription = apiService.subscribeToRound(sessionId, roundId, callbacks);
// 保存 abort 函数
const abortSubscription = subscription.abort;

// 切换会话时调用
abortSubscription();
```

后端会在客户端断开连接时自动清理订阅者队列，但主动取消可以更快释放资源。

---

### 查询 Subagent Run Graph

查询某个 run 所属的 subagent graph。`round_id` 可以是 root run，也可以是任意 descendant run。

当前 `sub_agent` 工具会创建同步 child Agent run：父 Agent 等待 child 完成，child transcript 作为独立 Round 持久化，并通过 `subagent_runs` 与父 run 相连。本接口查询这些持久化 graph 边。

**请求**

```
GET /api/chat/{chat_session_id}/round/{round_id}/subagent-graph
Authorization: Bearer <access_token>
```

**响应** `200 OK`

```json
{
  "session_id": "session-1",
  "root_run_id": "root-run",
  "requested_run_id": "child-run",
  "nodes": [
    {"run_id": "root-run", "kind": "root", "status": "completed"},
    {"run_id": "child-run", "kind": "subagent", "status": "completed"}
  ],
  "edges": [
    {
      "edge_id": "edge-id",
      "parent_run_id": "root-run",
      "child_run_id": "child-run",
      "tool_call_id": "tc-agent-1",
      "agent_type": "research",
      "status": "completed",
      "prompt": "Read docs and summarize"
    }
  ]
}
```

`idempotency_key` 用于客户端在 `/message/stream` 已被接受但尚未收到 `runId` 时，通过 history/v2 精确恢复同一次请求对应的 round。
`last_event_sequence` 用于 running / waiting Round 的 SSE 订阅续接；前端已用 history 展示过的事件不应再从 `last_sequence=0` 重放。

---

### 中止 Agent 执行

中止正在进行的 Agent 执行。

第一版按单 worker 部署。该接口会写入 append-only 取消审计行，并通过进程内 run registry 命中当前 run 的取消令牌；接口同时将 `running` 或 `waiting_interaction` Round 立即收敛为 `cancelled` 并释放会话锁，允许用户马上重发。

取消成功时，`RUN_FINISHED.result.reason` 为 `user_cancelled`。取消 waiting Round 会同时收敛 pending Interaction 与尚未 dispatch 的 `requested` / `approved` 审批，并唤醒已注册 subscriber。REST 响应只返回 nullable `outcome_warning`：能证明尚未 dispatch 时为 `null`；审批为 `executing` / `unknown`，或普通 running Round 无法证明尚未派发时返回保守警告。durable `RUN_FINISHED.result` 才包含对应的 `outcomeUncertain` 布尔值。风险判定和 cancelled terminal 必须属于同一锁事务，避免判定后又发生 dispatch。

**请求**

```
POST /api/chat/{chat_session_id}/abort
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

正常情况：

```json
{
  "status": "cancelled",
  "request_id": "uuid",
  "reason": "force_aborted",
  "outcome_warning": null
}
```

执行 worker 已死（心跳过期），直接收敛：

```json
{
  "status": "cancelled",
  "request_id": "uuid",
  "reason": "worker_dead",
  "outcome_warning": null
}
```

若取消事务发现审批已进入 `executing` / `unknown`，或普通 running Round 无法证明尚未派发远端调用，`outcome_warning` 返回保守警告文本；同一文本也写入 durable `RUN_FINISHED.result.warning`，并令 `result.outcomeUncertain=true`。

**错误**

| 状态码 | 说明                           |
| ------ | -------------------------------- |
| 404    | 会话不存在 |
| 409    | 该会话没有可取消的 running/waiting Round，且锁不存在或已过期 |
| 503    | 取消请求写入失败（数据库繁忙等） |

### 查询取消请求状态

用于排查取消审计记录与当前运行态。第一版取消投递依赖单 worker 进程内 registry；DB 行只做审计与诊断。

**请求**

```
GET /api/chat/{chat_session_id}/abort/status
Authorization: Bearer <access_token>
```

**响应** `200 OK`

```json
{
  "session_id": "session-uuid",
  "state": "acked",
  "request_id": "req-uuid",
  "requested_at": "2026-04-16T10:00:00",
  "acked_at": "2026-04-16T10:00:01",
  "completed_at": null,
  "running": true,
  "running_round_id": "round-uuid"
}
```

`state` 含义：

| 值        | 说明 |
| --------- | ---- |
| `none`      | 尚未记录取消请求 |
| `requested` | 已收到取消请求，但未命中本进程 registry |
| `acked`     | 已命中本进程 registry 并触发本地取消 |
| `completed` | 该次运行已结束并完成收敛 |

**错误**

| 状态码 | 说明     |
| ------ | -------- |
| 404    | 会话不存在 |

---

### 恢复暂停执行 (Human-in-the-Loop)

回答 `ask_user` 或解决工具审批，并恢复 Agent 执行。默认继续同一个逻辑 Round，返回 SSE 流。

**请求**

```
POST /api/chat/{chat_session_id}/resume
Authorization: Bearer <access_token>
Content-Type: application/json
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**请求体**

```json
{
  "interrupt_id": "interrupt-uuid",
  "answers": {
    "你想用什么语言？": "Python",
    "需要测试吗？": "是"
  },
  "pending_file_drafts": [{"source":"workspace","path":"报告.md"}]
}
```

| 字段         | 类型              | 必填 | 说明 |
| ------------ | ----------------- | ---- | ---- |
| interrupt_id | string            | 是   | 来自 `CUSTOM interaction_requested.value.interactionId` |
| answers      | dict[string, string] | 是   | 用户回答（问题文本 → 选项值） |
| pending_file_drafts | object[] | 否 | continuation 启动时仍在同步的文件身份，语义与 direct send 相同 |

工具审批复用同一 wire，`answers` 必须为 `{"approval":"allow_once|allow_session|allow_always|deny"}`。审批值按 trim/lower 规范化；同一 canonical resolution 的重试幂等，不同 resolution 返回控制面冲突。顶层 `resolution` 不是公开请求字段。

`resume` 不接收 `preferred_skill_keys` 或 `preferred_mcp_server_ids`，也不允许覆盖服务端保存的原始用户消息锚点。服务端从 Interaction request 继承统一 turn preferences；连续多次暂停/恢复仍锚定最初 user message，并按本次 resume 时的 Skill registry 与 MCP catalog 重新解析。

**响应** `200 OK`

```
Content-Type: text/event-stream
```

事件类型与 `message/stream` 相同。响应继续使用原 `runId == round_id`，不会创建新 Round，也不会再发一次 `RUN_STARTED`；持久化 `CUSTOM interaction_resolved` 表示 continuation 已经接管原 Round。

**恢复机制**

- **same-Round 热恢复**：在 `agent_interactions` 幂等冻结答案，取得 continuation claim，将 `interaction_resolved` 与原 Round 从 `waiting_interaction` 改回 `running` 原子提交，并回填 tool result。
- **same-Round 冷恢复**：Agent 内存状态丢失时，从原 Round 的事件与 Interaction 重建占位，仍继续同一 runId；不把回答降级为新的聊天消息。
- **工具审批**：允许决定先 `requested → approved` 持久化；只有到工具调用边界才 `approved → executing` 并生成执行 claim/lease。派发前崩溃可恢复，派发后结果未知绝不自动重试。

**并发限制**

与 `message/stream` 共享同一套用户 slot 锁；同一用户最多同时运行 `AGENT_USER_CONCURRENCY_LIMIT` 个不同 session，同一 session 不可重入。

**错误响应**

`resume` 返回 SSE 后，初始化/竞争错误也可能以 AG-UI `RUN_ERROR` 结束当前 HTTP 流。

| 状态码 | 说明 |
| ------ | ---- |
| 404    | 会话不存在 |
| 429    | 当前用户运行任务数已达上限 |
| 503    | 服务暂时不可用 |

| RUN_ERROR code | 说明 |
| -------------- | ---- |
| `NO_PENDING_INTERRUPT` | 无待处理的中断，或中断 ID 已过期/已恢复 |
| `RESUME_CONFLICT` | 回答与已持久化答案冲突，或并发恢复已获胜 |
| `INVALID_INTERACTION_RESPONSE` | 回答格式或枚举值与 Interaction kind 不匹配；例如审批值非法 |
| `AGENT_INIT_FAILED` | Agent 初始化失败 |
| `USER_ABORT` | resume 初始化期间已被较新的 abort 取消 |
| `INTERNAL_ERROR` | continuation 启动前内部错误 |

若上述 `RUN_ERROR` 出现在 `interaction_resolved` 之前，它是 **控制面错误**，不是原 waiting Round 的失败终态。客户端必须查询 `history/v2`，按权威 waiting/running/terminal 恢复，并在恢复成功后仍显示本次请求错误。越过 `interaction_resolved` 后的运行异常必须先与原 Round 原子持久化为带 sequence 的 durable `RUN_ERROR`；无 sequence 的 adapter/transport 错误仍不能单独终态化 Round。

---

## AG-UI 事件类型

流式 API 采用 AG-UI (Agent User Interaction Protocol) 协议，定义了 22 种标准化事件类型。

### 事件分类

| 类别         | 事件数 | 用途                 |
| ------------ | ------ | -------------------- |
| 生命周期事件 | 5      | 跟踪 Agent 运行进度  |
| 文本消息事件 | 4      | 流式传输聊天内容     |
| 思考过程事件 | 3      | 流式传输 AI 思考过程 |
| 工具调用事件 | 5      | 工具执行状态和结果   |
| 状态管理事件 | 3      | 同步应用状态         |
| 活动事件     | 2      | 执行进度展示         |
| 特殊事件     | 2      | 自定义扩展           |

### 生命周期事件

#### RUN_STARTED

Agent 运行开始。

```json
{
  "type": "RUN_STARTED",
  "threadId": "session-uuid",
  "runId": "run-uuid",
  "preferredSkills": [
    {"key": "pdf", "display_name": "PDF 处理"}
  ],
  "preferredMcpConnections": [
    {"server_id": "server-uuid", "display_name": "东方财富数据"}
  ],
  "timestamp": 1699000000000
}
```

普通 direct Round 的 `preferredSkills` / `preferredMcpConnections` 是与该 Round 持久化数据相同的两份权威展示快照；任一没有有效选择时也显式返回 `[]`。same-Round resume 不发新的 `RUN_STARTED`，原快照不变。字段只表示请求级偏好，不代表 Skill 已加载或 MCP 已真实调用。

#### RUN_FINISHED

Agent 运行结束。

```json
{
  "type": "RUN_FINISHED",
  "threadId": "session-uuid",
  "runId": "run-uuid",
  "result": {
    "finalResponse": "文件分析完成...",
    "stepCount": 3,
    "roundId": "round-001"
  },
  "outcome": "success",
  "timestamp": 1699000100000
}
```

`outcome="interrupt"` 仅用于用户取消或 `max_steps_reached` 等终止结果；Human-in-the-Loop 进入 `waiting_interaction` 时不发送 `RUN_FINISHED`。

#### RUN_ERROR

Agent 运行错误。

```json
{
  "type": "RUN_ERROR",
  "message": "对话执行失败",
  "code": "ExecutionError",
  "timestamp": 1699000050000
}
```

#### STEP_STARTED / STEP_FINISHED

步骤开始/结束。

```json
{
  "type": "STEP_STARTED",
  "stepName": "step_1",
  "timestamp": 1699000010000
}
```

### 文本消息事件

采用 Start → Content* → End 三阶段流式模式。

#### TEXT_MESSAGE_START

```json
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_001",
  "role": "assistant",
  "timestamp": 1699000020000
}
```

#### TEXT_MESSAGE_CONTENT

```json
{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_001",
  "delta": "文件分析",
  "timestamp": 1699000021000
}
```

#### TEXT_MESSAGE_END

```json
{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_001",
  "timestamp": 1699000025000
}
```

### 思考过程事件（扩展）

用于流式传输 AI 的思考过程（thinking/reasoning）。

#### THINKING_TEXT_MESSAGE_START

```json
{
  "type": "THINKING_TEXT_MESSAGE_START",
  "messageId": "thinking_001",
  "timestamp": 1699000015000
}
```

#### THINKING_TEXT_MESSAGE_CONTENT

```json
{
  "type": "THINKING_TEXT_MESSAGE_CONTENT",
  "messageId": "thinking_001",
  "delta": "用户需要分析文件...",
  "timestamp": 1699000016000
}
```

#### THINKING_TEXT_MESSAGE_END

```json
{
  "type": "THINKING_TEXT_MESSAGE_END",
  "messageId": "thinking_001",
  "timestamp": 1699000019000
}
```

### 工具调用事件

采用 Start → Args* → End → Result 四阶段模式。

#### TOOL_CALL_START

```json
{
  "type": "TOOL_CALL_START",
  "toolCallId": "tc_001",
  "toolCallName": "ReadTool",
  "parentMessageId": "msg_001",
  "timestamp": 1699000030000
}
```

#### TOOL_CALL_ARGS

```json
{
  "type": "TOOL_CALL_ARGS",
  "toolCallId": "tc_001",
  "delta": "{\"path\": \"data.csv\"}",
  "timestamp": 1699000031000
}
```

#### TOOL_CALL_END

```json
{
  "type": "TOOL_CALL_END",
  "toolCallId": "tc_001",
  "timestamp": 1699000032000
}
```

#### TOOL_CALL_RESULT

```json
{
  "type": "TOOL_CALL_RESULT",
  "messageId": "result_001",
  "toolCallId": "tc_001",
  "content": "文件内容: id,name,value\n1,foo,100...",
  "role": "tool",
  "timestamp": 1699000035000
}
```

### 状态管理事件

用于同步 Agent 内部状态到前端。

#### STATE_SNAPSHOT

完整状态快照。

```json
{
  "type": "STATE_SNAPSHOT",
  "snapshot": {
    "currentStep": 0,
    "totalSteps": null,
    "status": "running",
    "toolLogs": [],
    "lastUpdated": 1699000005000
  },
  "timestamp": 1699000005000
}
```

#### STATE_DELTA

增量状态更新（JSON Patch RFC 6902）。

```json
{
  "type": "STATE_DELTA",
  "delta": [
    {"op": "replace", "path": "/currentStep", "value": 2},
    {"op": "replace", "path": "/lastUpdated", "value": 1699000040000}
  ],
  "timestamp": 1699000040000
}
```

#### MESSAGES_SNAPSHOT

消息历史快照（用于断线恢复）。

```json
{
  "type": "MESSAGES_SNAPSHOT",
  "messages": [
    {"id": "msg_001", "role": "assistant", "content": "文件分析完成..."},
    {"id": "tool_001", "role": "tool", "toolCallId": "tc_001", "content": "..."}
  ],
  "timestamp": 1699000050000
}
```

### 特殊事件

#### CUSTOM

自定义扩展事件。

```json
{
  "type": "CUSTOM",
  "name": "title_updated",
  "value": {
    "sessionId": "session-uuid",
    "title": "CSV 数据分析"
  },
  "timestamp": 1699000060000
}
```

首条消息若很快进入 `waiting_interaction`，标题生成仍继续：标题最终落库，原 SSE 尚在时追加该事件；客户端已断开时向 waiting subscriber 投递更新，不因暂停或断连取消标题任务。

```json
{
  "type": "CUSTOM",
  "name": "heartbeat",
  "value": {"timestamp": 1699000070000},
  "timestamp": 1699000070000
}
```

Human-in-the-Loop 请求事件（持久化并参与 sequence 重放）：

```json
{
  "type": "CUSTOM",
  "name": "interaction_requested",
  "value": {
    "interactionId": "interaction-uuid",
    "runId": "original-round-uuid",
    "kind": "user_input",
    "toolCallId": "tool-call-id",
    "payload": {"questions": []}
  },
  "sequence": 12
}
```

`kind` 为 `user_input` 或 `tool_approval`，`payload` 是对应 kind 的结构化请求。创建该事件、pending `AgentInteraction` 和 `Round.status=waiting_interaction` 必须同事务提交。

同一 Round continuation 接管事件：

```json
{
  "type": "CUSTOM",
  "name": "interaction_resolved",
  "value": {
    "interactionId": "interaction-uuid",
    "runId": "original-round-uuid",
    "toolCallId": "tool-call-id",
    "toolResultContent": "frozen model-visible result",
    "resolution": "answered"
  },
  "sequence": 13
}
```

`toolResultContent` 用于可重复的历史重建，不得包含解密后的敏感工具参数。客户端收到该事件后将同一 Round 从 waiting 转回 running；仅 HTTP 200 或本地 `stream_accepted` 不具有此语义。

工具审批在最终结果前还会持久化同一 Round 的回填 marker：

```json
{
  "type": "CUSTOM",
  "name": "tool_approval_resume",
  "value": {"toolCallId": "tool-call-id"},
  "sequence": 14
}
```

历史重建用该 marker 将随后匹配的 `TOOL_CALL_RESULT` 替换到原审批占位，避免追加重复工具结果。marker 本身不完成 Interaction；只有最终匹配的 `TOOL_CALL_RESULT` 持久化后，工具审批 Interaction 才转为 `answered`。

### ID 体系说明

| ID 类型    | 格式                         | 说明             |
| ---------- | ---------------------------- | ---------------- |
| threadId   | UUID                         | 对应 session_id  |
| runId      | UUID                         | 对应 round_id    |
| messageId  | `msg_{runId}_{stepNumber}` | 消息唯一标识     |
| toolCallId | `tc_{runId}_{stepNumber}`  | 工具调用唯一标识 |

---

## 模型管理 API

### 获取模型列表

列出所有可用模型（不含敏感字段如 `api_key` / `api_base`）。

**请求**

```
GET /api/models
```

**响应** `200 OK`

```json
{
  "models": [
    {
      "id": "glm-5",
      "name": "智谱 GLM-5",
      "provider": "openai",
      "supports_thinking": true,
      "max_tokens": 32768,
      "tags": ["thinking"]
    }
  ],
  "default_model": "glm-5",
  "subagent_default_model": "mimo"
}
```

---

### 查询单个模型

**请求**

```
GET /api/models/{model_id}
```

| 参数     | 类型   | 必填 | 说明                 |
| -------- | ------ | ---- | -------------------- |
| model_id | string | 是   | 模型 ID（Path 参数） |

**响应** `200 OK`

```json
{
  "id": "glm-5",
  "name": "智谱 GLM-5",
  "provider": "openai",
  "supports_thinking": true,
  "max_tokens": 32768,
  "tags": ["thinking"]
}
```

**错误**

| 状态码 | 说明                       |
| ------ | ---------------------------- |
| 404    | 模型不存在或已停用，返回可用模型列表 |

---

## 数据模型

### Session（会话）

| 字段       | 类型     | 说明                              |
| ---------- | -------- | --------------------------------- |
| id         | string   | 会话 ID（UUID）                   |
| user_id    | string   | 用户 ID                           |
| status     | string   | 状态：active / paused / completed |
| title      | string   | 会话标题                          |
| created_at | datetime | 创建时间                          |
| updated_at | datetime | 最后更新时间                      |

### Message（消息）

| 字段       | 类型     | 说明                            |
| ---------- | -------- | ------------------------------- |
| id         | string   | 消息 ID                         |
| session_id | string   | 所属会话 ID                     |
| role       | string   | 角色：user / assistant / system |
| content    | string   | 消息内容                        |
| created_at | datetime | 创建时间                        |

### Round（对话轮次）

| 字段           | 类型   | 说明                                         |
| -------------- | ------ | -------------------------------------------- |
| round_id       | string | 轮次 ID                                      |
| user_message   | string | 用户消息                                     |
| final_response | string | 最终响应                                     |
| steps          | Step[] | 执行步骤列表                                 |
| step_count     | int    | 步骤数量                                     |
| status         | string | 状态：running / waiting_interaction / completed / failed / cancelled / max_steps_reached |
| created_at     | string | 创建时间                                     |
| completed_at   | string | 完成时间                                     |

### AgentInteraction（同 Round 人机交互）

| 字段 | 类型 | 说明 |
| ---- | ---- | ---- |
| id | string | Interaction ID；工具审批时也等于 approval request ID |
| round_id | string | 暂停并继续的同一个 Round |
| kind | string | `user_input` / `tool_approval` |
| tool_call_id | string | 对应工具调用 |
| status | string | `pending` / `answered` / `cancelled` / `failed` |
| request_payload | object | `interaction_requested.value` 的持久化结构 |
| answer_payload | object | 已接受的规范答案；有值但仍 pending 且尚未 started 时表示 continuation 可恢复 |
| claim_token / claim_lease_expires_at | string / datetime | continuation 围栏与可回收 lease；不等于工具执行 claim |
| continuation_started_at | datetime | 与 durable `interaction_resolved` 同事务写入；有值后 lease 过期只能将原 Round 收敛 failed |

同一 Round 最多一条 pending Interaction。创建 Interaction、Round 进入 waiting、当前完成 step 计数与 `interaction_requested` 事件必须同事务提交；复合审批写入固定采用 `Round → AgentInteraction → ToolApprovalRequest` 锁序。只有 `continuation_started_at` 为空的过期 claim 可以恢复 waiting；started continuation 必须以 durable `RUN_ERROR` 终态化。

### Step（执行步骤）

| 字段              | 类型         | 说明                        |
| ----------------- | ------------ | --------------------------- |
| step_number       | int          | 步骤序号                    |
| thinking          | string       | 思考过程（可选）            |
| assistant_content | string       | 助手内容（可选）            |
| tool_calls        | ToolCall[]   | 工具调用列表                |
| tool_results      | ToolResult[] | 工具结果列表                |
| status            | string       | 状态：streaming / completed |
| created_at        | string       | 创建时间                    |

### ToolCall（工具调用）

| 字段  | 类型   | 说明     |
| ----- | ------ | -------- |
| name  | string | 工具名称 |
| input | object | 输入参数 |

### ToolResult（工具结果）

| 字段    | 类型   | 说明             |
| ------- | ------ | ---------------- |
| success | bool   | 是否成功         |
| content | string | 结果内容         |
| error   | string | 错误信息（可选） |

### UserRunLock（用户运行锁）

用户级运行 slot。每个用户最多持有 `AGENT_USER_CONCURRENCY_LIMIT` 个不同 session 的运行 slot；同一 session 同一时刻只能持有一个 slot。执行 worker 通过定时刷新 `updated_at` 实现心跳保活。

| 字段       | 类型     | 说明                            |
| ---------- | -------- | ------------------------------- |
| lock_id    | string   | 锁实例 UUID（主键，用于 owner 校验释放） |
| user_id    | string   | 用户 ID                         |
| session_id | string   | 当前持锁的会话 ID               |
| slot       | int      | 用户内并发 slot 编号            |
| created_at | datetime | 锁创建时间                      |
| updated_at | datetime | 最后心跳时间                    |

**约束**：`lock_id` 为主键；`Unique(user_id, slot)` 限制用户并发配额；`Unique(user_id, session_id)` 保证同一会话不可重入。

**心跳与过期**：`TurnOrchestrator` 的 lock heartbeat guard 每 15s 刷新 `updated_at`。当 `updated_at` 超过 `sse_subscribe_timeout` 秒未更新时，slot 被视为陈旧，新请求可回收并清理该 session 关联的孤儿 Round。

### RunCancelRequest（取消审计）

取消请求 append-only 审计表（`requested → acked → completed`）。第一版运行时按单 worker 部署，取消投递由进程内 `RunCancelService` registry + per-run cancel token 完成；DB 行只做审计与诊断，不承担跨 worker command delivery。

| 字段            | 类型     | 说明                                      |
| --------------- | -------- | ----------------------------------------- |
| request_id      | string   | 请求 UUID（主键）                         |
| session_id      | string   | 会话 ID                                   |
| user_id         | string   | 用户 ID                                   |
| target_run_id   | string   | 精确取消目标 run（可选）                  |
| root_run_id     | string   | 根 run（可选）                            |
| requested_after | datetime | 防误杀守卫，避免 abort 后新建 run 被串扰 |
| state           | string   | requested / acked / completed             |
| requested_at    | datetime | 请求时间                                  |
| acked_at        | datetime | 本地 registry 命中确认时间（可选）        |
| completed_at    | datetime | 运行结束时间（可选）                      |

### ChannelSessionBinding（Channel 会话绑定）

外部 channel peer 到内部 session 的绑定表。当前 Web SSE 路径仍以
`session_id` 为入口；该表提供 typed turn/channel 边界中的持久化 binding。
`NormalizedInboundTurn` / `ReplyRoute` / `DeliveryIntent` 等 turn contract 给
Web adapter、Cron adapter 和未来外部 channel adapter 复用，未来外部 channel
adapter 可用本表将 peer 映射到 session。

| 字段               | 类型     | 说明                                      |
| ------------------ | -------- | ----------------------------------------- |
| id                 | string   | 绑定 UUID（主键）                         |
| user_id            | string   | 用户 ID                                   |
| session_id         | string   | 内部会话 ID                               |
| channel            | string   | 渠道标识，如 web / cron / 未来外部 channel |
| account_id         | string   | 渠道账号或 bot 实例（可选）               |
| peer_kind          | string   | web / direct / group / thread / cron / webhook |
| peer_id            | string   | 渠道内对端标识                            |
| external_thread_id | string   | 渠道线程标识（可选）                      |
| binding_key        | string   | 规范化 channel peer 后的 SHA-256          |
| reply_route_json   | string   | `ReplyRoute` 快照（可选）                 |
| metadata_json      | string   | adapter 元数据（可选）                    |
| created_at         | datetime | 创建时间                                  |
| updated_at         | datetime | 最后更新时间                              |

**约束**：`Unique(user_id, binding_key)`；删除用户或 session 时级联清理。
当前 `DeliveryService` 是 no-op 边界，外部网络消息发送不属于本阶段能力。

### SubagentRun（Subagent 图边）

父 run 到子 agent run 的有向边。`sub_agent` 工具触发时，服务层会创建 `subagent_runs` edge、创建 child `Round(parent_run_id=parent_run_id)`，并在 child run 结束后更新 edge 状态、输出或错误。

`sub_agent` 工具参数为：

| 字段          | 类型   | 必填 | 说明                                      |
| ------------- | ------ | ---- | ----------------------------------------- |
| prompt        | string | 是   | 完整委托任务提示                          |
| subagent_type | string | 否   | 子 agent profile，默认 `general`（可选 `research`/`write`/`general`） |
| description   | string | 否   | 人类可读的任务摘要                        |

运行边界：聊天 Agent 默认注册该工具；父 Agent 可以在同一步发起多个 `sub_agent` tool call，连续的 `sub_agent` 调用会按 `AGENT_SUBAGENT_MAX_PARALLEL` 做有界并行执行，父 Agent 写回消息历史和父 SSE 的工具结果仍保持原始 tool call 顺序。`subagent_type` 解析为 profile（见 `src/agent/subagent_profiles.py`），决定子 Agent 的系统提示与工具集；子 Agent **不继承**父 Agent 记忆，而是加载 profile 自带的精简系统提示。legacy 值映射：plan/review/explore→research，code/debug→write，未知/空→general。Cron Agent 与 child Agent 均排除该工具，避免无人值守递归和 subagent 自递归；三个 profile 也统一排除 `manage_cron`。child Agent 使用 `models.yaml` 的 `subagent_default_model`，默认不提供 `ask_user` / `sub_agent`。`SubAgentTool.execute_timeout = 0`：child Round 由自身步数上限管控，不受父 Agent 单次工具超时（`agent_tool_timeout`）拦截。child AG-UI events 写入 child round，不转发到父 SSE；父 Agent 只收到一个包含 child run id、edge id、状态和最终输出的工具结果。

| 字段          | 类型     | 说明                                      |
| ------------- | -------- | ----------------------------------------- |
| id            | string   | Edge UUID（主键）                         |
| user_id       | string   | 用户 ID                                   |
| session_id    | string   | 会话 ID                                   |
| root_run_id   | string   | 图的顶层 run                              |
| parent_run_id | string   | 触发 subagent 的父 run                    |
| child_run_id  | string   | 子 agent 对应的 Round（可选，唯一）       |
| tool_call_id  | string   | 父 run 中触发 subagent 的 tool call（可选） |
| agent_name    | string   | 子 agent 名称（可选）                     |
| agent_type    | string   | 子 agent 类型（可选）                     |
| model_id      | string   | 子 agent 模型（可选；不传则使用 `models.yaml` 的 `subagent_default_model`） |
| description   | string   | 任务摘要（可选）                          |
| prompt        | string   | 完整任务提示                              |
| status        | string   | requested / running / completed / failed / cancelled |
| output        | string   | 子 agent 最终输出（可选）                 |
| error         | string   | 子 agent 错误（可选）                     |

### FileInfo（文件信息）

| 字段     | 类型   | 说明                 |
| -------- | ------ | -------------------- |
| name     | string | 文件名               |
| path     | string | 相对路径             |
| size     | int    | 文件大小（字节）     |
| modified | string | 修改时间（ISO 格式） |
| type     | string | MIME 类型            |

---

## 配置管理 API

### 读取 Agent 配置文件

```
GET /api/config/agent-files/{name}
Authorization: Bearer <access_token>
```

**Path 参数**: `name` — user / soul / memory

`AGENTS.md` 由平台模板统一管理，不通过用户配置 API 暴露；请求 `agents` 会返回 400。

**Response**:
```json
{
  "name": "user",
  "file_type": "user_md",
  "content": "# Alice\n偏好：深色模式",
  "version": 3
}
```

### 更新 Agent 配置文件

```
PUT /api/config/agent-files/{name}
Authorization: Bearer <access_token>
```

**Request Body**:
```json
{ "content": "# Updated content" }
```

**Response**:
```json
{ "name": "user", "file_type": "user_md", "version": 4, "message": "ok" }
```

### 获取 Skill 列表

```
GET /api/config/skills
Authorization: Bearer <access_token>
```

可选 `refresh=true` 强制连接/恢复沙箱并执行一次完整严格扫描；缺省直接读取与当前 sandbox/Profile 代际匹配的 DB 清单快照。

**Response**:
```json
{
  "skills": [
    { "key": "docx", "name": "docx", "display_name": "Word 文档", "description": "Word 文档处理", "category": "document", "source": "official", "enabled": true },
    { "key": "my-skill", "name": "my-skill", "display_name": "我的 Skill", "description": "用户自定义能力", "category": "user", "source": "user", "enabled": true }
  ],
  "sandbox_status": "available",
  "inventory_state": "current",
  "inventory_discovered_at": "2026-07-17T10:00:00"
}
```

`key` 是稳定内部标识，用于 `preferred_skill_keys`、启停和运行时加载；`display_name` 仅用于展示。SKILL.md 的展示名按顶层 `display_name` / `display-name`、`metadata` 内同名字段、`name` 的顺序回退。

`source` 为 `official` 或 `user`。`sandbox_status` 为：

- `not_created`：尚无持久化沙箱，仅返回官方 Skills；
- `available`：当前 sandbox/Profile 代际已有完整清单（匹配的 DB 快照或本次严格扫描），不表示本次做过实时探活；
- `unavailable`：既有沙箱本次不可连接、恢复或发现，仍返回 200 和官方 Skills；若当前代际有旧快照，也可附带用户 Skills 并标记 `inventory_state=stale`。

普通请求不访问远程沙箱。仅当快照缺失或显式 `refresh=true` 时才连接/恢复并扫描；控制面确认旧代际终止、失败、不存在或 Profile 明确不匹配时，该刷新路径可能创建替代沙箱并以 CAS 更新绑定。

用户 Skill 完整清单最多 256 项；每项 `display_name`、`description`、`sandbox_skill_dir` 分别上限 1024、8192、1024 UTF-8 bytes，清单规范 JSON 总量上限 1 MiB。重复/非法 key、非法元数据或任何容量超限都会使整次严格扫描失败，不会发布部分清单。

### 启用/禁用 Skill

```
PUT /api/config/skills/{skill_name}
Authorization: Bearer <access_token>
```

**Request Body**:
```json
{ "enabled": false }
```

**Response**:
```json
{ "skill_name": "docx", "enabled": false, "message": "ok" }
```

启停以数据库逻辑状态为准，后续 LLM 请求按 30s TTL 缓存重新求值（最迟约 30s 内生效）；禁用不会物理删除沙箱中的 Skill 文件。当前 MVP 按单 worker 部署，跨 worker/副本的 Skill 推送状态协调延后实现。

---

## 定时任务 API

### 获取 CronJob 任务列表（DB 驱动）

```
GET /api/cron/jobs
Authorization: Bearer <access_token>
```

**Response**:
```json
{
  "jobs": [
    {
      "name": "daily_report",
      "cron_expr": "0 9 * * *",
      "description": "每天9点生成日报",
      "enabled": true,
      "rule_version": 1,
      "definition_version": 2
    }
  ]
}
```

### 获取执行历史（分页）

```
GET /api/cron/runs?job_name=<optional>&limit=20&offset=0
Authorization: Bearer <access_token>
```

**Response**:
```json
{
  "runs": [
    {
      "id": "uuid",
      "job_name": "daily_report",
      "cron_expr": "0 9 * * *",
      "queued_at": "2026-03-30T08:59:59",
      "started_at": "2026-03-30T09:00:00",
      "completed_at": "2026-03-30T09:01:30",
      "status": "success",
      "phase": "terminal",
      "attempt_count": 1,
      "error_code": null,
      "output": "日报已生成",
      "is_read": true,
      "artifacts": [{"name": "report.md", "size": 1024}],
      "run_workspace": "/mnt/user/cron/runs/uuid",
      "workspace_changes": [{"entry_id": "entry-id", "operation": "updated", "revision": 3}]
    }
  ],
  "total": 42,
  "offset": 0,
  "limit": 20
}
```

### 手动触发任务

```
POST /api/cron/jobs/{job_name}/run
Authorization: Bearer <access_token>
```

**说明**: 从 `cron_jobs` 冻结 prompt 与 definition version，先创建 `status=queued` 的 durable run，再由 worker claim/lease 执行（接口立即返回 `run_id`）。Cron 不按用户串行排队，手动触发不写 `cron_fires`。执行结果不会注入聊天 Session，用户通过消息中心查看。

**Response**:
```json
{ "job_name": "daily_report", "run_id": "uuid-string", "status": "accepted", "message": "后台任务已执行" }
```

### 查询单条执行记录

```
GET /api/cron/runs/{run_id}
Authorization: Bearer <access_token>
```

**说明**: 查询指定 `run_id` 的执行记录状态，用于前端轮询任务执行进度。

**Response**:
```json
{
  "id": "uuid-string",
  "job_name": "daily_report",
  "cron_expr": "0 9 * * *",
  "queued_at": "2026-04-14T08:59:59",
  "started_at": "2026-04-14T09:00:00",
  "completed_at": "2026-04-14T09:00:15",
  "status": "success",
  "phase": "terminal",
  "attempt_count": 1,
  "error_code": null,
  "output": "日报已生成",
  "is_read": false,
  "artifacts": [{"name": "report.md", "size": 2048}],
  "run_workspace": "/mnt/user/cron/runs/uuid-string",
  "workspace_changes": []
}
```

**status 枚举**: `queued` | `running` | `success` | `failed` | `conflict` | `unknown`。`claim_token`、worker id 与 lease 时间属于服务端 ownership fence，不在 API 中返回。

### 获取未读执行记录数

```
GET /api/cron/runs/unread-count
Authorization: Bearer <access_token>
```

**Response**:
```json
{ "count": 3 }
```

**说明**: 统计当前用户所有 `is_read = false` 的记录（不按 `status` 过滤，包含 `running` / `success` / `failed`）。

### 标记执行记录为已读

```
POST /api/cron/runs/mark-read?run_id=<optional>
Authorization: Bearer <access_token>
```

**说明**:
- 不传 `run_id`：将当前用户所有未读记录标记为已读（不按 `status` 过滤）。
- 传 `run_id`：仅标记指定 run（必须属于当前用户，不根据 `status` 过滤）。

**Response**:
```json
{ "marked": 3, "unread_count": 0 }
```

### 获取执行产物文件列表

```
GET /api/cron/runs/{run_id}/files
Authorization: Bearer <access_token>
```

**说明**: 返回该次执行在沙箱中生成的文件列表（从 DB `artifacts` 字段读取）。

**Response**:
```json
{
  "files": [
    { "name": "report.md", "size": 1024 },
    { "name": "data.csv", "size": 4096 }
  ]
}
```

### 下载执行产物文件

```
GET /api/cron/runs/{run_id}/files/{file_path}
Authorization: Bearer <access_token>
```

**说明**: 从沙箱下载指定执行产物文件。包含路径遍历保护。

**Response**: 文件内容流（`application/octet-stream`）

---

## 附录

### API 前缀配置

默认 API 前缀为 `/api`，可通过环境变量 `API_PREFIX` 修改。

### 交互式文档

- Swagger UI: `http://localhost:8000/api/docs`
- ReDoc: `http://localhost:8000/api/redoc`

### 健康检查

```
GET /health
```

响应：

```json
{
  "status": "healthy",
  "version": "0.1.0"
}
```
