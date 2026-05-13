# OpenCapyBox Agent Gateway Refactor 完整计划 v5

## Summary

把 OpenCapyBox 从“Web Chat 路由直接驱动 Agent”重构为“Channel Adapter + Session Binding + Turn Orchestrator + Run Coordinator + Redis-backed AG-UI Event Bus”的多入口 Agent 平台。

核心取舍：

- 保留 CapyBox 的 `Round`、`agui_events`、DB replay 作为事实源。
- 引入 Redis，用于跨 worker 实时唤醒、AG-UI Pub/Sub fanout、cancel fast-path。
- Redis 不替代 DB 持久事实；Redis 丢消息不能导致 replay 数据丢失。
- 第一阶段 Web 行为零回归：前端 API/SSE 协议不变。
- 同一 session 永远串行。
- 同一用户不同 session 在 Phase B 后默认允许并发，`user` cap 默认 3，可配置回退到 1。
- 外部 channel 不直接碰 Agent、Round、AG-UI 表，只走 adapter/orchestrator/projection。
- Cron 不在早期阶段顺手迁移；等 Web/orchestrator/lane 稳定后再 channel 化。

## Hard Invariants

第一阶段必须守住这些不变量，除非单独开行为变更 PR 并同步前端/spec/test。

- Web HTTP 路径保持兼容：send/resume/abort/subscribe/running-session 语义不变。
- Web busy 错误码保持现有前端可理解语义；内部 `BusySessionError` 不直接改变 Web 契约。
- AG-UI 中 `threadId == session_id`，`runId == round_id`。
- `sequence` 继续是 per round 单调递增；`lastSequence` replay 语义不变。
- `RUN_STARTED` 时机与现有链路等价：只有实际受理 run 后才让前端进入 running。
- HTTP/SSE 断开不能杀掉后台 run；Web 断线后仍依赖 subscribe/replay 恢复。
- Web abort 成功返回前必须完成当前 Web 语义需要的同步收敛，让前端可以立即本地停止并释放发送态。
- failed round 保持 `RUN_ERROR` terminal 语义，不为 failed 合成 `RUN_FINISHED(outcome=interrupt)`。
- interrupted round 继续通过 `RUN_FINISHED(outcome="interrupt")` 表达；resume 后旧 round 标记为 `resumed`，新建 round 承载后续执行。
- 终态 round 的迟到非终态事件必须被丢弃，不能污染 `agui_events` replay。
- Web 不消费 `RunHandle.queue_state`；它只给排队型 channel 使用。

## Core Interfaces

### Reply Route

`ReplyRoute` 必须是 discriminated union，不允许裸 `dict`。

```python
ReplyRoute = Annotated[
    WebReplyRoute | ChannelMessageReplyRoute | NoReplyRoute,
    Field(discriminator="kind"),
]

class WebReplyRoute(BaseModel):
    kind: Literal["web_sse"] = "web_sse"
    session_id: str

class ChannelMessageReplyRoute(BaseModel):
    kind: Literal["channel_message"] = "channel_message"
    channel: str
    account_id: str | None
    peer_kind: Literal["direct", "group", "thread"]
    peer_id: str
    external_thread_id: str | None = None

class NoReplyRoute(BaseModel):
    kind: Literal["none"] = "none"
```

### Normalized Turn

```python
class NormalizedInboundTurn(BaseModel):
    channel: str
    user_id: str
    account_id: str | None = None
    peer_kind: Literal["web", "direct", "group", "thread", "cron", "webhook"]
    peer_id: str
    external_thread_id: str | None = None
    content: list[ContentBlock]
    attachments: list[Attachment] = []
    reply_route: ReplyRoute
    metadata: dict[str, Any] = {}
    idempotency_key: str | None = None

class NormalizedResumeTurn(BaseModel):
    channel: str
    user_id: str
    session_id: str
    interrupt_id: str
    answers: dict[str, str]
    reply_route: ReplyRoute
    metadata: dict[str, Any] = {}

class TurnCancelTarget(BaseModel):
    user_id: str
    session_id: str
    round_id: str | None = None
    channel: str = "web"
    reason: str = "user_cancelled"
```

### Run Handle And Queue State

```python
class QueueState(BaseModel):
    mode: Literal["none", "queued", "rejected"]
    policy: Literal["reject_if_busy", "queue_if_busy", "drop_if_busy"]
    position: int | None = None
    reason: str | None = None

class RunHandle(BaseModel):
    session_id: str
    round_id: str
    run_id: str
    reply_route: ReplyRoute
    queue_state: QueueState
    started_at: datetime
```

补充约束：

- `RunHandle.event_stream` 不放进 Pydantic 模型；由 Web adapter 使用 `AguiEventBus.subscribe(round_id)` 取得。
- `RunHandle.queue_state` 只给排队型 channel 使用，Web 不消费。
- Web 同 session busy 的 HTTP 映射要按现有前端契约保持兼容。

### Turn Orchestrator

```python
class TurnOrchestrator:
    async def submit_turn(turn: NormalizedInboundTurn) -> RunHandle: ...
    async def resume_turn(turn: NormalizedResumeTurn) -> RunHandle: ...
    async def cancel_turn(target: TurnCancelTarget) -> CancelResult: ...
```

`TurnOrchestrator` 负责：

- 解析 binding，得到内部 `session_id`。
- 校验 session/user/model/token limit。
- 获取 `AgentService`。
- 创建 Round。
- 设置 cancel token。
- 进入 `RunCoordinator`。
- 调用 `AgentService.chat_agui()` 或 `resume_agui()`。
- 将事件发布到 `AguiEventBus`。
- 返回 `RunHandle` 给 Web SSE 或外部 channel worker。

## Channel Layer

第一阶段只实现 Web channel。

- `WebChatAdapter`：把现有发送消息 HTTP 请求转成 `NormalizedInboundTurn`。
- `WebResumeAdapter`：把 resume 请求转成 `NormalizedResumeTurn`。
- `WebCancelAdapter`：把 abort 请求转成 `TurnCancelTarget`。
- Web SSE 仍原样返回 AG-UI event，不做 projection。
- Adapter 不创建 Round，不调用 Agent，不写 AG-UI 表。

后续外部平台适配器只负责：

- 平台鉴权和 webhook 校验。
- 平台消息转 `NormalizedInboundTurn`。
- 平台能力描述，如是否支持按钮、图片、线程、typing。
- `deliver(route, outbound_message)` 发送平台消息。

## Session Binding

新增 `channel_session_bindings` 表：

- `id`
- `user_id`
- `channel`
- `account_id`
- `peer_kind`
- `peer_id`
- `external_thread_id`
- `binding_key`
- `session_id`
- `reply_route_json`
- `default_model_id`
- `metadata_json`
- `created_at`
- `updated_at`

唯一约束使用非空 `binding_key`，不要直接依赖 nullable columns 的 unique 语义。

`binding_key` 由下面字段规范化后生成：

```text
channel | account_id_or_empty | peer_kind | peer_id | external_thread_id_or_empty
```

默认 binding 策略：

- Web：直接使用 URL 中的 `session_id`，binding 可懒创建。
- DM：每个外部用户一个 session。
- Group：每个群一个 session。
- Thread：每个外部 thread 一个 session。
- Cron/Webhook：默认独立 session，可后续绑定已有 session。

回复目标只来自 binding 和 `ReplyRoute`，模型不能决定回复到哪里。

## Redis-backed AG-UI Event Bus

新增 `AguiEventBus`，成为 AG-UI event 的唯一发布、重放、订阅、terminal 补发入口。

核心方法：

```python
publish(run_id, event) -> StoredEvent | None
replay(run_id, after_sequence=0) -> list[dict]
subscribe(run_id, after_sequence=0) -> AsyncIterator[dict]
ensure_terminal(run_id) -> bool
```

### Fact Source

DB 仍是事实源：

- `agui_events` 存完整 payload 和 sequence。
- `rounds` 存 run 状态、final response、interrupt payload。
- replay 永远从 DB 读取。
- Redis 只负责通知和快速唤醒。

### Publish Flow

1. `publish()` 检查 round 状态。
2. 如果 round 已终态且 event 是非终态事件，直接丢弃并记录 warning。
3. 在 DB 事务内写入 `agui_events`，分配 per round 单调递增 sequence。
4. 根据 event 更新 round 状态。
5. commit 成功后向 Redis channel 发布轻量通知。
6. Redis message 只包含 `run_id`、`sequence`、`event_type`、`created_at`。

### Subscribe Flow

1. `subscribe(run_id, after_sequence)` 先建立 Redis 订阅。
2. 调用 `ensure_terminal(run_id)` 做懒检查。
3. 从 DB replay `sequence > after_sequence` 的事件。
4. 进入实时阶段；收到 Redis 通知后按 cursor 从 DB 补拉。
5. 如果发现 sequence gap，直接从 DB 补齐缺口。
6. 保留周期性 DB catch-up，处理 Redis 瞬断或 Pub/Sub 消息丢失。
7. 如果 round 已终态且没有新事件，关闭订阅流。

### Redis Channels

建议 channel 命名：

```text
{REDIS_KEY_PREFIX}:agui:run:{run_id}
{REDIS_KEY_PREFIX}:cancel:session:{session_id}
```

### Terminal Ensure

`ensure_terminal` 触发方式：

- `subscribe()` 前懒检查。
- abort/cancel 收敛时主动写 terminal event。
- 显式 repair 命令：`capy repair-terminal-runs --since-hours <N>`。

重复补发防护：

- 先查 run 是否已有 `RUN_FINISHED` 或 `RUN_ERROR`。
- 事务内取 `MAX(sequence)+1` 写入。
- 单进程内对同一 `run_id` 加 mutex。
- 跨 worker 写前二次确认。
- 若历史异常产生重复 terminal event，subscribe 只认最早 sequence 并记录 warning。

`capy repair-terminal-runs` 应纳入部署流水线、系统 cron、Kubernetes CronJob 或平台定时任务，建议每 10-30 分钟跑一次，扫描最近 24 小时。

## Run Cancel Service

新增 `RunCancelService`，下沉当前 route 里的 cancel request 写入、ack、complete。

职责：

- abort 写入 DB `run_cancel_requests`，状态为 `requested`。
- 写入后发布 Redis cancel message，作为异 worker fast-path。
- 持有本地 runner 的 worker 收到 Redis cancel message 后立即 set cancel token。
- 执行 worker 仍轮询 DB cancel request，作为 Redis 失效兜底。
- Agent run 收敛后将 cancel request 标记为 `completed`。
- Web abort 成功返回前，必须完成当前 Web 语义需要的 round/lane/lock 同步收敛。

## Run Coordinator And Lanes

迁移分两段。

### Phase A

先抽 `RunCoordinator`，但保持旧语义：

- `user:<user_id>` cap=1。
- Web send/resume 行为不变。
- 不启用同用户多 session 并发。
- 只把锁逻辑从 route 下沉。

### Phase B

`TurnOrchestrator` 完成 binding/session 解析后，再切换到 lane 模型。

默认 lane：

- `session:<session_id>`：严格串行。
- `user:<user_id>`：并发上限默认 3，可配置。
- `memory:<user_id>`：长期记忆写入和 memory sync 串行。
- `skill:<user_id>`：skill 安装/更新串行。
- `sandbox_lifecycle:<user_id>`：sandbox resume/pause/renew 串行，不包住整段 Agent run。

冲突策略：

- Web send/resume：按现有 Web 契约立即拒绝。
- External channel：默认排队，并可发送“已加入队列”投影。
- Cron/Webhook：默认排队，可配置 `drop_if_busy`。
- Cancel：不排队，直接命中当前 running run 或写入 cancel request。

Phase B 可以再决定 lane coordinator 使用 Redis semaphore/Lua 还是 DB 锁；外部 channel 排队需要持久队列，不能只放 Redis 内存结构。

## Projection And Delivery

第一阶段只落接口，不实现持久重试队列。

职责边界：

- `ChannelProjection`：AG-UI event -> `OutboundMessage`，不做网络发送。
- `DeliveryService`：记录投递 attempt；第一阶段可内存记录，重启丢队列。
- `ChannelAdapter.deliver()`：调用平台 API，处理平台级错误。

默认 projection：

- assistant text：聚合后分块发送。
- tool call：可配置发送进度提示。
- thinking：默认不发送。
- interrupt：转按钮/问题卡/文本问答。
- success final：发送最终回答。
- error：发送平台友好失败消息。

失败策略：

- Web SSE 断开：不重试，依赖 AG-UI replay。
- transient error：指数退避重试。
- rate limit：按平台 `retry_after` 延迟。
- permanent error：记录失败，不重跑 Agent。
- final response 投递失败不改变 Round 状态；Agent 执行事实和平台投递事实分离。

持久 `delivery_attempts` 表等第一个真实外部 channel 接入时再新增。

## Config And Ops

新增配置建议：

```text
REDIS_URL=redis://localhost:6379/0
EVENT_BUS_BACKEND=redis
REDIS_KEY_PREFIX=opencapybox
RUN_COORDINATOR_USER_CAP=3
RUN_COORDINATOR_FORCE_USER_CAP_ONE=false
AGUI_REPAIR_TERMINAL_SINCE_HOURS=24
```

约束：

- 多 worker / production 必须使用 Redis。
- 如果 `EVENT_BUS_BACKEND=redis` 且 Redis 连接失败，服务启动应失败。
- `inmemory` backend 只允许单 worker dev/test 使用，不做生产静默 fallback。
- 环境变量必须同步 `.env.example` 与 `docs/Capy-project-md/env-reference.md`。

## Migration Plan

1. Phase 0：收敛 spec 和兼容性基线。新增/更新 `agui-event-bus-spec.md`、`turn-orchestrator-spec.md`、`lane-coordinator-spec.md`、`chat-spec.md`、`frontend-chat-spec.md`、`sessions-spec.md`、`cron-spec.md`。
2. Phase 1：抽出 Redis-backed `AguiEventBus`，保持 Web SSE/replay 行为不变。DB 继续作为事实源，Redis 负责跨 worker fanout。
3. Phase 2：抽出 `RunCancelService`。取消请求继续写 DB；新增 Redis cancel fast-path；保留 Web abort 的即时收敛语义。
4. Phase 3：抽出 `RunCoordinator Phase A`。保持旧 user cap=1，只把锁逻辑从 route 下沉。
5. Phase 4：同一 PR 落地 typed turn/reply route/run handle 与 `TurnOrchestrator`。Web routes 变为 adapter，HTTP/SSE 协议不变。
6. Phase 5：新增 `channel_session_bindings` 与 binding service。Web 懒创建 binding，使用非空 `binding_key` 处理唯一性。
7. Phase 6：切换 `RunCoordinator Phase B`。启用 session lane + user cap=3，可配置回退到 cap=1。
8. Phase 7：新增 projection/delivery 接口，delivery 暂不持久化。
9. Phase 8：Web 稳定后，再把 Cron 迁为 internal channel 或 `NoReplyRoute` channel。
10. Phase 9：将 `capy repair-terminal-runs` 纳入部署/运维定时任务。

## Test Plan

必须覆盖：

- Web send/resume/abort/subscribe/running-session 行为不变。
- SSE event 格式、heartbeat、`lastSequence` replay 不变。
- Redis Pub/Sub 跨 worker fanout：worker A 运行，worker B subscribe 能收到并从 DB replay。
- Redis 丢消息后 DB catch-up 能补齐。
- sequence gap 自动从 DB 补齐。
- `ensure_terminal` 只补一次 terminal。
- failed round 只补 `RUN_ERROR`，不合成错误的 `RUN_FINISHED`。
- abort 后迟到事件不写入终态 round。
- abort 成功后前端可立即重发的 Web 语义不回归。
- Redis cancel fast-path 能命中异 worker runner；Redis 失效时 DB cancel request 仍可兜底。
- 非法 `ReplyRoute` 被 schema 拒绝。
- binding 并发懒创建不会因 nullable unique 坑产生重复行。
- Phase A 保持 user cap=1。
- Phase B 同 session 串行、同用户不同 session 可并发、user cap 可回退为 1。
- 不同 session 的 workspace、Round、AG-UI event 不串流。
- memory/skill/sandbox lifecycle lane 串行保护共享资源。
- interrupted round 刷新后可恢复，resume 后旧 round 标记 resumed。
- projection 默认不发送 thinking。
- interrupt projection 能生成外部平台可表达的问题消息。
- transient/rate-limit/permanent delivery failure 被正确分类。
- final delivery failure 不改变 AG-UI run 终态，不触发 Agent 重跑。
- Cron 迁移前，现有 Cron 测试不因 Web 重构被无意改变；迁移时再补 internal channel/no reply route 测试。

## Spec Sync Checklist

必须同步：

- `docs/specs/chat-spec.md`
- `docs/specs/frontend-chat-spec.md`
- `docs/specs/sessions-spec.md`
- `docs/specs/cron-spec.md`
- `docs/Capy-project-md/env-reference.md`

建议新增：

- `docs/specs/agui-event-bus-spec.md`
- `docs/specs/turn-orchestrator-spec.md`
- `docs/specs/lane-coordinator-spec.md`
- `docs/specs/channel-adapter-spec.md`
- `docs/specs/projection-delivery-spec.md`

## Key Files

- `src/api/routes/chat.py`：最终只保留 Web HTTP/SSE adapter。
- `src/api/services/agui_event_bus.py`：新增 Redis-backed event bus。
- `src/api/services/run_cancel_service.py`：新增 cancel request 与 Redis fast cancel。
- `src/api/services/run_coordinator.py`：新增 Phase A/Phase B 并发控制。
- `src/api/services/turn_orchestrator.py`：新增 submit/resume/cancel 编排入口。
- `src/api/models/agui_event.py`：保持 DB replay 事实源。
- `src/api/services/history_service.py`：配合或承担迟到事件丢弃。
- `src/api/models/channel_session_binding.py`：新增 binding 表。
- `src/api/config.py`、`.env.example`、`docs/Capy-project-md/env-reference.md`：新增 Redis/EventBus/RunCoordinator 配置。

## Open Decisions

1. Web busy 第一阶段建议保持现有错误码；若要改为 409，应单独改 frontend/spec/test。
2. EventBus 第一阶段使用 Redis Pub/Sub + DB replay；如果后续需要持久消费组，再评估 Redis Streams。
3. Lane coordinator Phase B 使用 Redis semaphore/Lua 还是 DB 锁，需要在实现前结合 SQLite/Postgres 目标再定。
4. 外部 channel 排队是否持久化：真实接入前可以先定义接口；接入时必须有 DB 持久队列。
5. `capy repair-terminal-runs` 是 CLI、admin API 还是复用 cron worker 触发，需要在 Phase 9 定。

## Assumptions

- `sessions.id` 继续作为 AG-UI `threadId`。
- `rounds.id` 继续作为 AG-UI `runId`。
- `agui_events` 继续作为 Web replay 的事实源。
- 第一阶段不修改前端协议。
- 外部平台投递失败不触发 Agent 重跑。
- delivery 持久化等第一个真实外部 channel 接入时再做。
- terminal repair 由运维定时执行，不依赖人工记忆。
