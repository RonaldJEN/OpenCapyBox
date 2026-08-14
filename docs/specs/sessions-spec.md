# 会话管理 (Sessions) — Spec

## 1. 模块职责边界

**负责：**

- 会话 CRUD（创建、列表、删除、标题更新）
- 文件浏览与下载（代理沙箱文件操作）
- 文件上传
- 运行状态查询（running-sessions）
- 历史记录查询（Round/Step 结构化）

**不负责：**

- 消息发送、Agent 执行、SSE 流

## 2. 数据模型

### sessions 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, indexed |
| title | String(255) | nullable |
| status | String(20) | default="active", indexed。取值：active / paused / completed |
| model_id | String(50) | nullable, indexed |
| created_at | DateTime | default=now, indexed |
| updated_at | DateTime | default=now, onupdate=now |

别名：Thread（AG-UI 协议中 session = thread）

`match_type` / `match_excerpt` 是列表搜索响应的派生字段，不落库。

## 3. API 契约

所有端点均需 Bearer auth。

### POST /api/sessions/create

- Query: `model_id: str | None`
- Response 200: `{session_id, model_id, message}`
- 创建 Session 行 + 触发 AgentPoolService 缓存预热

### GET /api/sessions/list

- Query: `q: str | None`（可选；搜索会话标题或讨论内容）
- Response 200: `{sessions: [{id, user_id, status, created_at, updated_at, title, model_id, match_type?, match_excerpt?, match_round_id?}]}`
- `q` 为空或全空格时返回完整列表；非空时仅返回当前用户匹配的 sessions
- 搜索范围：`sessions.title` + `rounds.user_message` + `conversation_messages(role=assistant).content` + `rounds.final_response`
- 用户消息只搜索 `rounds.user_message` 这种前端可见文本，不搜索 `conversation_messages(role=user).content` 中的 Agent 内部上下文 / 附件提示 / Data URL
- Agent 回复搜索 `conversation_messages(role=assistant).content`，排除 `tool` / `summary` / `synthetic`；`rounds.final_response` 作为历史重建兜底
- 搜索使用 PostgreSQL 兼容的 `ILIKE ... ESCAPE` 轻量匹配，用户输入中的 `\`、`%`、`_` 必须转义为普通字符
- 非空搜索最多返回 50 个 sessions；各搜索来源在 DB 侧按 session 取最佳命中并截断，避免侧栏搜索拉回无界历史
- `match_type` 取值：`title` / `user` / `assistant`
- `match_round_id` 在命中具体轮次时返回，供前端切换 session 后定位
- 排序优先级：title → user → assistant；同级内按更新时间倒序

### GET /api/sessions/{id}/history/v2

- Response 200:

```json
{
  "session_id": "...",
  "rounds": [RoundData],
  "total": 0
}
```

其中 `RoundData`:

```json
{
  "round_id": "...",
  "parent_run_id": "... | null",
  "idempotency_key": "... | null",
  "last_event_sequence": 0,
  "user_message": "...",
  "user_attachments": [],
  "thinking_mode": "enabled | disabled | provider_default | null",
  "reasoning_effort": "max | null",
  "final_response": "...",
  "steps": [StepData],
  "step_count": 0,
  "status": "...",
  "created_at": "...",
  "completed_at": "...",
  "interrupt": null
}
```

`idempotency_key` 由客户端发送消息时生成；history/v2 必须返回该字段，供 accepted 但尚未收到 `runId` 的断线恢复路径按因果标识定位本次 round，不能用时间窗口猜测旧 round。
`last_event_sequence` 是该 round 已持久化 AG-UI 事件的最大 sequence；前端在 history 已经重建 running round 后订阅 SSE 时必须从该 sequence 之后接续，避免重复消费已展示事件。
`history/v2` 只返回主聊天流可见 round；被 `subagent_runs.child_run_id` 指向的 child round 属于 sidechain，不得作为普通用户/助手对话返回。注意不能用 `parent_run_id != null` 过滤，因为 ask_user resume round 也有 `parent_run_id` 且必须保留在主聊天流。

其中 `StepData`:

```json
{
  "step_number": 1,
  "thinking": "...",
  "assistant_content": "...",
  "tool_calls": [
    {
      "id": "...",
      "name": "...",
      "input": {},
      "started_at_ts": 1710000000000,
      "ended_at_ts": 1710000000100
    }
  ],
  "tool_results": [
    {
      "tool_call_id": "...",
      "success": true,
      "content": "...",
      "error": null,
      "received_at_ts": 1710000000300,
      "execution_time_ms": 200
    }
  ],
  "status": "...",
  "created_at": "...",
  "thinking_start_ts": 1710000000000,
  "thinking_end_ts": 1710000000100,
  "started_at_ts": 1710000000000,
  "finished_at_ts": 1710000000400
}
```

`*_ts` 字段均为 AG-UI 事件时间戳（毫秒），用于前端恢复历史时重建思考、工具调用和工具结果耗时；旧事件缺少对应时间戳时字段为 `null` 或省略。

- Error 404

### PATCH /api/sessions/{id}/title

- Body: `{title: str}`（1-255 字符）
- Response 200: SessionResponse
- Error 404
- 注意：前端目前不使用此端点，标题由后端通过 CUSTOM SSE 事件自动生成

### DELETE /api/sessions/{id}

- Response 200: `{message: "会话已删除"}`
- Error 404
- 级联删除：Round (CASCADE) → AGUIEventLog (CASCADE)、ConversationMessage、LLMCallRecord、AgentPoolService 缓存移除
- 沙箱清理：尝试删除 workspace 目录，沙箱过期时仅删 DB 记录

### GET /api/sessions/{id}/files

- Query: `path: str = ""`（相对子目录）
- Response 200: `{files: [{name, path, size, modified, type, is_directory}], total}`
- `modified` 为带显式时区偏移的 ISO 8601 时间字符串，当前统一返回 UTC（如 `2026-05-08T02:30:00+00:00`）。
- Error 404, 403（"路径越界"）, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）
- 当 `sandbox_use_server_proxy=True` 时使用 `find` 命令替代 SDK

### GET /api/sessions/{id}/files/{path:path}

- Query: `preview: bool = False`
- Response: 文件字节流，`Content-Disposition` = attachment（下载）或 inline（预览）
- 可预览类型：text/\*、image/\*、PDF、JSON、XML
- Error 404, 400（"文件路径不合法"）, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）, 503（"沙箱不可用"）

### GET /api/sessions/running-sessions

- Response 200: `{running_sessions: [{session_id: str, round_id: str|null}]}`
- `running_sessions` 返回当前用户所有持有新鲜 `UserRunLock` slot 的 session；新鲜判定为 `updated_at >= now - SSE_SUBSCRIBE_TIMEOUT`。
- `round_id=null` 表示仍处于 Agent 初始化窗口，尚未写入 running round。
- 单次查询避免 N+1

### POST /api/sessions/{id}/upload

- Body: multipart file
- Response 200: `{name, path, size, modified, type, is_directory}`
- `modified` 为带显式时区偏移的 ISO 8601 时间字符串，当前统一返回 UTC（如 `2026-05-08T02:30:00+00:00`）。
- Error 400（"未选择文件"）, 404, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）, 503, 500
- 上传文件落盘在 session 根目录，文件名经过 `_sanitize_filename` 清洗：主文件名部分保留 Unicode 字母/数字（含中文）、下划线、连字符、点号；空格、括号等特殊字符替换为下划线并合并连续下划线；扩展名部分保留原样。若清洗后同名文件已存在，追加 `_1`、`_2` 等序号，避免覆盖。

## 4. 行为语义与不变量

- 一个 Session = 一个 AG-UI Thread
- Session 删除是级联的：rounds → agui_events → conversation_messages → llm_call_records 全部删除
- 文件路径校验：必须在沙箱 mount 路径内（`is_within_sandbox_root`），否则 403
- running-sessions 查询通过 `UserRunLock` + `Session` + `Round` 实现单次查询，返回所有活跃且未超过 `SSE_SUBSCRIBE_TIMEOUT` 的 slot
- 历史 v2 通过 AGUI 事件动态重建 Step 结构，而非直接查表

## 5. 失败模式与错误处理

- 沙箱 Profile 配置冲突（如绑定后端不存在/禁用）时文件操作保留服务层 409；普通沙箱连接不可用时文件操作返回 503
- 沙箱过期时删除会话：DB 记录正常删除，沙箱文件可能残留（日志记录）
- 文件读取支持 fallback（SDK → base64 命令）
- Session 不属于当前用户时返回 404（不暴露是否存在）

## 6. 可观测性

- 会话创建/删除有日志
- 沙箱文件操作失败有 warning 日志
- 文件下载 fallback 路径有日志

## 7. 非目标

- 不做会话分享/协作
- 不做会话导出
- 不做会话归档
- 不做文件版本管理
- 不做大文件分片上传
