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

获取当前用户的所有会话。

**请求**

```
GET /api/sessions/list
Authorization: Bearer <access_token>
```

| 参数       | 类型   | 必填 | 说明                  |
| ---------- | ------ | ---- | --------------------- |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

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
      "updated_at": "2025-01-14T10:35:00"
    }
  ]
}
```

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
      "user_message": "帮我创建一个 Python 文件",
      "user_attachments": [
        {
          "path": "images/demo.png",
          "name": "demo.png",
          "type": "image/png"
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

> 说明：当 `sandbox_use_server_proxy=True`（默认）时，由于 OpenSandbox proxy 会丢弃 GET query params 导致 `files.search` SDK 失败，后端会直接使用 `find` 命令列举文件，不再先调用 SDK 再回退。当 `sandbox_use_server_proxy=False`（直连模式）时，优先使用 SDK `files.search`，失败再降级为命令列举。接口始终返回 `200`。

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
      "modified": "2025-01-14T10:35:00",
      "type": "py"
    }
  ],
  "total": 1
}
```

> 说明：`type` 为文件扩展名（如 `py` / `pdf`），不是 MIME type。
> 在 `sandbox_use_server_proxy=True` 且走 `find` 回退分支时，列表接口无法稳定获取文件真实大小，`size` 可能为 `0`。

---

### 下载/预览文件

下载或预览会话沙箱中的文件。

**请求**

```
GET /api/sessions/{chat_session_id}/files/{file_path}?preview=<bool>
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                      |
| --------------- | ------ | ---- | ------------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）      |
| file_path       | string | 是   | 文件相对路径（Path 参数） |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |
| preview         | bool   | 否   | 是否预览模式，默认 false  |

**响应**

- `preview=false`: 返回文件流，Content-Disposition 为 attachment
- `preview=true`: 对于可预览文件（文本、图片、PDF），Content-Disposition 为 inline

**常见错误**

- `404 Not Found`：会话不存在，或文件不存在/不可读
- `400 Bad Request`：文件路径不合法（越界路径）
- `503 Service Unavailable`：沙箱不可用（连接/恢复失败）

**可预览的文件类型**

- text/* (文本文件)
- image/* (图片)
- application/pdf
- application/json
- application/xml

---

### 检查运行中会话

检查用户是否有正在运行的会话（单次 API 调用，避免 N+1 查询）。

**请求**

```
GET /api/sessions/running-session
Authorization: Bearer <access_token>
```

| 参数       | 类型   | 必填 | 说明                  |
| ---------- | ------ | ---- | --------------------- |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

```json
{
  "running_session_id": "550e8400-e29b-41d4-a716-446655440000",
  "round_id": "round-001"
}
```

或无运行中会话时：

```json
{
  "running_session_id": null,
  "round_id": null
}
```

**使用场景**

- 页面加载时检测是否有未完成的任务
- 多标签页同步状态
- 避免前端遍历所有会话的 N+1 查询问题

---

### 轮询会话状态

轻量级轮询，返回会话的轮次数量，供前端检测新消息。

**请求**

```
GET /api/sessions/{chat_session_id}/poll
Authorization: Bearer <access_token>
```

| 参数            | 类型   | 必填 | 说明                  |
| --------------- | ------ | ---- | --------------------- |
| chat_session_id | string | 是   | 会话 ID（Path 参数）  |
| user_id         | string | 是   | 用户 ID（由 Authorization Bearer Token 解析） |

**响应** `200 OK`

```json
{
  "round_count": 5
}
```

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
  "type": "application/pdf"
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
| idempotency_key  | string   | 否   | 幂等键（UUID），多 worker 下防止同一请求被重复处理。前端自动生成 |

**模型能力限制**

- 仅模型配置中 `supports_image=true` 的模型可接收 `image_url`
- `image_url` 数量不能超过模型 `max_images`
- 不支持图片/超限时返回 4xx 错误
- **图片大小限制**：单张图片 Data URL 上限 20MB，所有图片 Data URL 总量上限 50MB。超过时返回 `400` 错误
- **前端自动压缩**：图片在发送前会经过 Canvas 压缩（≤500KB JPEG），上传至沙箱的原始文件不受影响

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

- 同一用户同一时刻只允许一个运行中的任务
- 并发限制基于数据库行锁 `UserRunLock` 实现（跨 worker 生效），执行期间通过心跳保活
- 心跳超过 `sse_subscribe_timeout` 未更新时，锁被视为陈旧并自动回收
- 当存在运行中的任务时，接口返回 `429 Too Many Requests`

**幂等冲突（SSE 事件）**

当 `idempotency_key` 对应的 Round 已被另一个 worker 创建时，不返回 HTTP 错误，而是通过 SSE 推送 `RUN_ERROR` 事件：

```json
{
  "type": "RUN_ERROR",
  "code": "ROUND_IN_PROGRESS",
  "message": "<existing_round_id>"
}
```

前端收到后应使用 `message` 中的 `round_id` 转入 `subscribe` 恢复路径。

**错误响应**

| 状态码 | 说明 |
| ------ | ---- |
| 404    | 会话不存在 |
| 410    | 会话已完成（不可继续发送消息） |
| 429    | 当前用户已有运行中的任务（并发限制） |
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
| last_step       | int    | 否   | 已弃用，保留向下兼容，默认 0 |

**响应** `200 OK`

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
```

**行为说明**

1. **已终态轮次**（`completed` / `failed` / `interrupted` / `resumed` / `cancelled`）：立即返回补齐事件并关闭连接
2. **运行中的轮次**：
   - 先从 `agui_events` 表重放 `last_sequence` 之后的所有已持久化事件
   - 然后注册为订阅者接收后续实时事件
   - 轮次完成时发送 `RUN_FINISHED` 事件并关闭连接
3. **心跳**：每 15 秒发送 `CUSTOM` (heartbeat) 事件防止连接超时
4. **超时**：5 分钟无事件自动断开

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
| `CUSTOM`                                  | 自定义事件（心跳、标题更新等） |

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

### 中止 Agent 执行

中止正在进行的 Agent 执行。

在多 worker 部署下，该接口会先写入跨 worker 可见的取消请求（`requested`），随后由实际执行该任务的 worker 感知并触发本地取消令牌。Agent 会在下一个检查点（step 开始 / 工具执行前）退出，并通过 SSE 连接推送 `RUN_FINISHED(outcome=interrupt)`。

取消成功时，`RUN_FINISHED.result.reason` 为 `user_cancelled`，用于前端区分“用户取消”与“ask_user 中断”。

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
  "status": "cancellation_requested",
  "request_id": "uuid"
}
```

执行 worker 已死（心跳过期），直接收敛：

```json
{
  "status": "cancelled",
  "request_id": "uuid",
  "reason": "worker_dead"
}
```

**错误**

| 状态码 | 说明                           |
| ------ | -------------------------------- |
| 404    | 会话不存在 |
| 409    | 该会话没有正在进行的执行（无 running round 且锁不存在或已过期） |
| 503    | 取消请求写入失败（数据库繁忙等） |

### 查询取消请求状态（多 worker 可观测）

用于排查“取消是否已被执行 worker 感知”。接口返回取消请求状态机与当前运行态，便于控制台或运维面板展示。

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
| `requested` | 已收到取消请求，等待执行 worker 感知 |
| `acked`     | 执行 worker 已确认并触发本地取消 |
| `completed` | 该次运行已结束并完成收敛 |

**错误**

| 状态码 | 说明     |
| ------ | -------- |
| 404    | 会话不存在 |

---

### 恢复中断执行 (Human-in-the-Loop)

恢复被 `ask_user` 工具中断的 Agent 执行。返回 SSE 流。

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
  }
}
```

| 字段         | 类型              | 必填 | 说明 |
| ------------ | ----------------- | ---- | ---- |
| interrupt_id | string            | 是   | 中断 ID（来自 `RUN_FINISHED.interrupt.id`） |
| answers      | dict[string, string] | 是   | 用户回答（问题文本 → 选项值） |

**响应** `200 OK`

```
Content-Type: text/event-stream
```

事件类型与 `message/stream` 完全相同（AG-UI 协议）。恢复操作会创建一个新的 Round。

**恢复机制**

- **热恢复**：Agent 内存中仍持有中断状态，直接替换 `ask_user` 占位 tool_result 继续执行
- **冷恢复**：Agent 内存状态已丢失（如 AgentPool TTL 回收），退化为注入用户回答作为新消息继续对话
- 旧的 `interrupted` 轮次会被标记为 `resumed`

**并发限制**

与 `message/stream` 共享同一把用户锁，同一用户同时只能有一个 send 或 resume 在执行。

**错误响应**

| 状态码 | 说明 |
| ------ | ---- |
| 404    | 会话不存在 |
| 409    | 无待处理的中断 |
| 429    | 当前用户已有运行中的任务 |
| 503    | 服务暂时不可用 |

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
  "timestamp": 1699000000000
}
```

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

```json
{
  "type": "CUSTOM",
  "name": "heartbeat",
  "value": {"timestamp": 1699000070000},
  "timestamp": 1699000070000
}
```

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
  "default_model": "glm-5"
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
| status         | string | 状态：pending / running / completed / failed / cancelled / interrupted / resumed |
| created_at     | string | 创建时间                                     |
| completed_at   | string | 完成时间                                     |

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

跨 worker 用户级运行互斥锁。每个用户最多一把锁（主键 `user_id`），执行 worker 通过定时刷新 `updated_at` 实现心跳保活。

| 字段       | 类型     | 说明                            |
| ---------- | -------- | ------------------------------- |
| user_id    | string   | 用户 ID（主键）                 |
| session_id | string   | 当前持锁的会话 ID               |
| lock_id    | string   | 锁实例 UUID（防止 TOCTOU 误删） |
| created_at | datetime | 锁创建时间                      |
| updated_at | datetime | 最后心跳时间                    |

**心跳与过期**：`_cancel_request_watcher` 每 15s 刷新 `updated_at`。当 `updated_at` 超过 `sse_subscribe_timeout` 秒未更新时，锁被视为陈旧，新请求可回收并清理关联的孤儿 Round。

### RunCancelRequest（跨 worker 取消请求）

跨 worker 可见的取消请求状态机（`requested → acked → completed`）。每个会话最多一条记录（主键 `session_id`），新 abort 会覆盖旧记录。

| 字段         | 类型     | 说明                             |
| ------------ | -------- | -------------------------------- |
| session_id   | string   | 会话 ID（主键）                  |
| user_id      | string   | 用户 ID                          |
| request_id   | string   | 请求 UUID                        |
| state        | string   | requested / acked / completed    |
| requested_at | datetime | 请求时间                         |
| acked_at     | datetime | 执行 worker 确认时间（可选）     |
| completed_at | datetime | 运行结束时间（可选）             |

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

**Path 参数**: `name` — user / soul / agents / memory

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

**Response**:
```json
{
  "skills": [
    { "name": "docx", "description": "Word 文档处理", "category": "document", "enabled": true }
  ]
}
```

### 启用/禁用 Skill

```
PUT /api/config/skills/{skill_name}
Authorization: Bearer <access_token>
```

**Request Body**:
```json
{ "enabled": false }
```

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
    { "name": "daily_report", "cron_expr": "0 9 * * *", "description": "每天9点生成日报", "enabled": true }
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
      "started_at": "2026-03-30T09:00:00",
      "completed_at": "2026-03-30T09:01:30",
      "status": "success",
      "output": "日报已生成",
      "is_read": true,
      "artifacts": [{"name": "report.md", "size": 1024}],
      "run_workspace": "/mnt/user/cron/runs/uuid"
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

**说明**: 从 `cron_jobs` 表查找任务并后台执行（立即返回 `run_id`）。执行结果不会注入聊天 Session，用户通过消息中心查看。前端可通过 `GET /api/cron/runs/{run_id}` 轮询执行状态。

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
  "started_at": "2026-04-14T09:00:00",
  "completed_at": "2026-04-14T09:00:15",
  "status": "success",
  "output": "日报已生成",
  "is_read": false,
  "artifacts": [{"name": "report.md", "size": 2048}],
  "run_workspace": "/mnt/user/cron/runs/uuid-string"
}
```

**status 枚举**: `running` | `success` | `failed`

### 获取未读执行记录数

```
GET /api/cron/runs/unread-count
Authorization: Bearer <access_token>
```

**Response**:
```json
{ "count": 3 }
```

**说明**: 仅统计 `status = success` 且 `is_read = false` 的记录。

### 标记执行记录为已读

```
POST /api/cron/runs/mark-read?run_id=<optional>
Authorization: Bearer <access_token>
```

**说明**:
- 不传 `run_id`：将当前用户所有 `success` 且未读记录标记为已读。
- 传 `run_id`：仅标记指定 run（且必须属于当前用户、状态为 `success`）。

**Response**:
```json
{ "marked": 3 }
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
