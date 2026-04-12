# 框架设计文档

本文档覆盖 OpenCapyBox 核心子系统的设计决策、数据流与关键实现细节。

---

## 1. Agent 循环与上下文管理

### 1.1 主循环结构（run_agui）

`Agent.run_agui()` 是整个 Agent 引擎的核心，采用 **反应式循环 + 生产者-消费者** 模式：

```
while step < max_steps:
    STEP_STARTED
    ├── LLM generate_stream() → asyncio.Queue
    │   ├── producer 协程：消费流式 Chunk，推入 Queue
    │   └── consumer 循环：从 Queue 取出，emit THINKING / TEXT_MESSAGE_CONTENT
    ├── 解析 tool_calls
    │   ├── 有 tool_calls → 逐个执行 → emit TOOL_CALL_START/END
    │   └── 无 tool_calls → 循环结束
    ├── 检测 ask_user 中断 → 提前退出
    STEP_FINISHED
```

- **asyncio.Queue 解耦**：LLM 流式生成在 producer 协程中进行，主循环通过 Queue 消费。这允许在 LLM 生成过程中实时检查 `cancel_token`。
- **max_steps 硬顶**：默认 50（Agent 实例级），可通过 `AGENT_MAX_STEPS` 环境变量调整到 100。超限后 emit `RUN_FINISHED`。

### 1.2 多层上下文管理流水线

当对话 token 数超过 `token_limit` 时，按 Level 2 → 3 → 4 渐进式压缩。每层后重新估算，够了就停。

| Level | 名称 | 是否调 LLM | 触发条件 | 行为 |
|-------|------|-----------|---------|------|
| 2 | Microcompact | 否 | `estimated > token_limit` | 将超过 4000 字符的旧 `tool_result` 替换为占位符；清除旧 `thinking` 块。安全边界：保留最近 2 个 user round 不压缩 |
| 3 | LLM Summarization | 是 | Level 2 后仍超限 | 调用 LLM 将每个 user round 的执行过程（assistant + tool 消息）汇总为一条 summary 消息 |
| 4 | Emergency Truncation | 否 | Level 3 后仍超过硬顶 | 直接丢弃最老的 user round（最多 3 轮），至少保留最后 1 个 user round |

**关键常量**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `token_limit` | 80,000 | 触发 Level 2 的软限 |
| `context_window` | 128,000 | 模型总上下文窗口 |
| `max_output_tokens` | 16,384 | 单次输出上限 |
| `_hard_ceiling` | `context_window - max_output_tokens - 3000` | Level 4 硬顶（下界 8192） |
| `_MICROCOMPACT_CHAR_THRESHOLD` | 4,000 | tool result 压缩阈值（字符） |

### 1.3 Token 估算

`_estimate_tokens()` 使用字符数 / 2.5 的粗估（≈ tiktoken 精度的 90%），带缓存。图片固定 1000 tokens，视频 5000 tokens。`force_recalculate=True` 可强制刷新。

---

## 2. LLM 抽象与弹性

### 2.1 Provider 策略

`LLMClient` 是统一入口，内部按 provider 委托给 `AnthropicClient` 或 `OpenAIClient`：

```
LLMClient
├── from_model_config(config)  ← 推荐：ModelConfig 驱动，零硬编码
└── __init__(api_key, provider, ...)  ← 向后兼容
    └── _client: LLMClientBase (AnthropicClient | OpenAIClient)
```

模型行为（reasoning_format、reasoning_split、enable_thinking、max_tokens）全部由 `models.yaml` 中的 `ModelConfig` 驱动，代码中无 `model.startswith()` 分支。

### 2.2 Failover 机制

`models.yaml` 中可配置 `fallback` 列表。调用链：

1. **主模型** → 重试耗尽（`RetryExhaustedError`）
2. **通知回调** → `failover_notify(model_id, context_window, max_tokens)` → Agent 重置流式状态、动态调整上下文窗口
3. **Fallback 模型** → 按列表顺序依次尝试
4. 全部失败 → 抛出 `RetryExhaustedError`

设计要点：
- **One-shot failover**：切换到 fallback 后不修改 `self._client`，下次调用仍优先尝试主模型（允许主模型恢复）
- **Failback 客户端缓存**：`_fallback_clients` 避免每次 failover 重建 HTTP 连接
- **动态上下文调整**：failover 时 Agent 根据 fallback 模型的 `context_window` 和 `max_tokens` 重新设定限制，防止上下文溢出

### 2.3 重试策略

`RetryConfig` 实现带 `max_increment` 上限的指数退避：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_retries` | 3 | 最大重试次数 |
| `initial_delay` | 0.5s | 初始延迟 |
| `max_delay` | 30s | 最大延迟 |
| `exponential_base` | 2.0 | 退避底数 |
| `max_increment` | 1.0s | 每步最大增量上限（防止延迟暴涨） |

`max_increment` 的作用：纯指数退避（0.5 → 1 → 2 → 4 → 8s）增长过猛，`max_increment=1.0` 会将增量截断为 `min(increment, 1.0)`，实际序列变为 0.5 → 1.0 → 1.5 → 2.0 → 2.5s。

### 2.4 流式超时

`STREAM_CHUNK_TIMEOUT = 100s`（OpenAI client）。单次 Chunk 间等待超限视为流式中断，触发重试。

---

## 3. SSE 事件流与断线重连

### 3.1 心跳保活

`_sse_with_heartbeat` 在 Agent 初始化或长工具执行期间持续发送 `CUSTOM` 类型的心跳事件（间隔 `SSE_HEARTBEAT_INTERVAL = 15s`），防止 nginx / 负载均衡器以 30s 空闲超时断开连接。

### 3.2 Lazy Agent 初始化

SSE 连接建立后，先发心跳维持连接，同时异步执行 Agent 初始化（沙箱创建、历史恢复、技能推送等）。初始化完成后无缝切换到 Agent 事件流。

### 3.3 断线重连（Subscribe）

```
客户端断线
    ↓
GET /subscribe?lastSequence=N
    ↓
服务端查询 agui_events 表中 sequence > N 的已持久化事件
    ↓
重放历史事件 → 无缝衔接到实时事件流
```

关键设计：**SSE 断开不终止 Agent**。producer 任务在 `_active_runners` dict 中持续运行，通过 `_broadcast_to_subscribers` 将后续事件推送给重连的订阅者。

### 3.4 事件持久化

每个 AG-UI 事件在 emit 后由 `HistoryService.save_agui_event()` 写入 `agui_events` 表（含 `run_id` + 单调递增 `sequence`），供 subscribe 重放和前端历史查询使用。

---

## 4. 幂等性保障

### 4.1 问题

网络不稳定时前端可能重复发送同一条用户消息，导致同一 Round 被创建两次。

### 4.2 方案

- 前端为每条消息生成 UUID 作为 `idempotency_key`
- DB 层 `rounds` 表设置 `UniqueConstraint(session_id, idempotency_key)`
- `HistoryService.create_round()` 捕获 `IntegrityError`，返回已存在的 Round 记录
- API 层据此将重复请求重定向到 `/subscribe` 模式（接入已在运行的事件流）

结果：无论前端重试多少次，同一条消息只产生一个 Round、一次 Agent 执行。

---

## 5. Ask-User 中断/恢复

### 5.1 中断流

```
Agent 循环中调用 ask_user 工具
    ↓
Agent 检测到 ASK_USER_TOOL_NAME → 中止当前步骤
    ↓
设置 Agent 状态为 interrupted
    ↓
emit RUN_FINISHED(outcome="interrupt") + InterruptDetails
    ↓
前端渲染中断卡片，等待用户输入
```

### 5.2 恢复流

```
用户在前端回答问题
    ↓
POST /resume (携带 parentRunId + 用户回答)
    ↓
Agent.resume_from_interrupt()
    ├── 将用户回答格式化为 tool_result 注入对话历史
    ├── 清除 interrupted 状态
    └── 从上次 tool_call 位置继续循环
    ↓
新的 SSE 事件流开始
```

`parentRunId` 关联中断与恢复的两次 run，确保前端能正确关联上下文。

---

## 6. Sub-Agent 机制

### 6.1 设计

采用 **层次 Agent (Hierarchical Agents)** 模式。主 Agent 可通过 `sub_agent` 工具派生子 Agent 执行特定任务。

### 6.2 实现

- 子 Agent 在 `sub_agent_tool.py` 中初始化独立的 `Agent` 实例
- 共享父 Agent 的沙箱环境（同一 sandbox 会话）
- 独立维护自身消息历史
- 支持并行执行多个子 Agent（通过 `asyncio.gather`）

### 6.3 事件映射

子 Agent 的事件通过 `ReasoningPanel` 在前端渲染为折叠式推理面板，与主对话流区隔。

---

## 7. Agent 池与生命周期

### 7.1 AgentPoolService（单例）

```
AgentPoolService
├── _cache: Dict[session_id, AgentService]    TTL 缓存
├── _create_locks: Dict[session_id, Lock]     并发创建锁
├── _user_sessions: Dict[user_id, Set[session_id]]  用户会话追踪
└── _ttl: 3600s
```

### 7.2 生命周期

| 阶段 | 触发点 | 行为 |
|------|--------|------|
| 创建 | `get_or_create()` | per-session Lock 防止重复并行初始化；创建 AgentService → 初始化 Agent → 同步记忆 → 推送技能 |
| 访问 | 每次 chat 请求 | `_touch()` 刷新 TTL |
| 过期 | 定时清理 / 显式 remove | 仅当用户的所有会话均过期时，才触发沙箱 `pause` |

### 7.3 沙箱竞态预防

问题：清理协程可能在 Agent 创建过程中 pause 沙箱。

方案：
- `_create_locks` 保证同一会话不并行创建
- 清理前检查沙箱状态，跳过正在创建中的会话
- `get_or_resume` 遇到 mkdir 失败时自动重试（沙箱可能刚从 paused 恢复）

---

## 8. 记忆系统

### 8.1 文件类型

| 文件 | DB file_type | 用途 |
|------|-------------|------|
| `SOUL.md` | `soul_md` | Agent 人格定义、行为准则 |
| `AGENTS.md` | `agents_md` | Agent 可用工具和使用规则 |
| `MEMORY.md` | `memory_md` | 长期记忆、经验积累 |
| `USER.md` | `user_md` | 用户画像、偏好、授权 |
| `HEARTBEAT.md` | `heartbeat_md` | Cron 定时任务定义 |

### 8.2 同步策略

存储双写：DB（`user_memory` 表）为权威持久层，沙箱（`/home/user/`）为运行时工作副本。

```
新用户首次登录
    模板文件 (docs/sandbox_template/) → DB → 沙箱

Agent 运行时修改记忆
    沙箱 (Agent 写入) → DB (dirty flag 触发回写)

用户前端页面编辑保存
    DB (前端写入) → 沙箱 (force 推送)

新会话创建 Agent
    沙箱优先：沙箱有内容 → 回写 DB; 沙箱无内容 → DB 推送到沙箱
```

### 8.3 sync_to_sandbox 沙箱优先策略

`MemoryService.sync_to_sandbox()` 通过 `force` 参数区分两种场景：

| 场景 | 调用点 | `force` | 行为 |
|------|--------|---------|------|
| Agent 创建时自动同步 | `agent_pool_service._do_create_agent()` | `False` | 沙箱有文件 → 保留沙箱版本，回写 DB；沙箱无文件 → 写入 DB 版本 |
| 用户前端页面保存 | `config.py PUT /agent-files/{name}` | `True` | 无条件 DB → 沙箱推送 |

**设计原因**：Agent 可能在沙箱中丰富了记忆文件（如 SOUL.md 从 45 行默认模板扩充为 263 行），如果 dirty flag 未正确触发或进程崩溃导致回写失败，下次创建 Agent 时不应用旧的 DB 版本覆盖沙箱中更完整的内容。

### 8.4 dirty flag 机制

`AgentService._run_round_stream()` 中通过 `_dirty_memory` 标记检测记忆文件是否被修改：

- **工具名匹配**：`record_memory`、`update_long_term_memory`、`update_user` → 直接标记 dirty
- **文件操作嗅探**：`write_file` / `edit_file` 的参数中包含记忆文件名 → 标记 dirty
- **盲区**：Agent 通过 `bash` 命令修改记忆文件时无法检测（`AGENTS.md` 中已禁止此行为）

每轮对话结束后，若 dirty flag 为 True，`_post_round_tasks()` 调用 `_sync_memory_to_db()` 将所有记忆文件从沙箱读回 DB。

### 8.5 BOOTSTRAP 引导流程

新用户首次使用时，系统在沙箱中写入 `BOOTSTRAP.md` 引导文件，指导 Agent 完成初始人格建立：

- `provision_sandbox_templates()` 仅在用户无任何对话记录时执行
- 沙箱中已存在该文件时跳过（幂等）
- Agent 完成引导后自行删除 BOOTSTRAP.md

### 8.6 新用户初始化时序

```
AgentPoolService._do_create_agent()
├── AgentService.initialize_agent()
│   └── _provision_default_files_if_needed()
│       └── MemoryService.provision_default_files()   // DB: 仅 is_new_user 时写入默认模板
├── MemoryService.sync_to_sandbox(force=False)        // 沙箱优先同步
└── MemoryService.provision_sandbox_templates()        // 写入 BOOTSTRAP.md（仅新用户）
```

---

## 9. 关键代码索引

| 子系统 | 核心文件 | 职责 |
|--------|---------|------|
| Agent 循环 | `src/agent/agent.py` | run_agui、上下文管理、工具执行 |
| LLM 抽象 | `src/agent/llm/llm_wrapper.py` | Provider 委托、Failover、重试 |
| OpenAI 客户端 | `src/agent/llm/openai_client.py` | 流式生成、STREAM_CHUNK_TIMEOUT |
| Anthropic 客户端 | `src/agent/llm/anthropic_client.py` | Claude 协议适配 |
| 重试机制 | `src/agent/retry.py` | RetryConfig、指数退避 |
| 事件发射器 | `src/agent/event_emitter.py` | AG-UI 事件生成 |
| SSE 路由 | `src/api/routes/chat.py` | 心跳、lazy init、subscribe |
| Agent 池 | `src/api/services/agent_pool_service.py` | TTL 缓存、并发锁、沙箱生命周期 |
| Agent 服务 | `src/api/services/agent_service.py` | 桥接层：工具创建、dirty flag、历史恢复 |
| 历史服务 | `src/api/services/history_service.py` | 事件持久化、幂等 Round、delta 聚合 |
| 记忆服务 | `src/api/services/memory_service.py` | provision / sync / search |
| Ask-User 工具 | `src/agent/tools/ask_user_tool.py` | 中断/恢复协议 |
| Sub-Agent | `src/agent/tools/sub_agent/` | 子 Agent 并行执行 |
| 模型注册表 | `src/api/model_registry.py` + `models.yaml` | ModelConfig 驱动的模型管理 |

---

**最后更新**: 2026-04-12
