# 定时任务 (Cron) — Spec

## 1. 模块职责边界

- Cron 作业定义与管理（CRUD：HTTP 表单与 Agent 工具共用）
- 结构化时间配置（`schedule`）↔ cron 表达式（`cron_expr`）转换
- 定时调度与执行（去中心化 worker）
- 执行历史查询与分页
- 手动触发
- 未读计数与显式已读标记（消息中心）
- 执行产物（artifacts）查看与下载
- 无人值守运行的持久工作区能力与 lease fencing
- Fire、queued run、claim/heartbeat/reconcile 的耐久执行语义
- 执行前遵守 `auth_users.enabled` 用户开通状态
- 不负责：复杂调度策略（仅 5-field cron）
- 不负责：用户周/月 token 限额扣减或门禁；Cron 执行不占用 Auth/Admin token 限额

## 2. 数据模型

### cron_jobs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK, autoincrement |
| user_id | String(100) | NOT NULL, indexed |
| name | String(100) | NOT NULL |
| cron_expr | String(50) | NOT NULL。所有调度/匹配以此字段为准 |
| schedule | Text | nullable。结构化时间配置的 JSON 源事实（仅用于表单回显）。老数据/Agent 工具创建的作业可为 NULL |
| description | Text | default=""（列表显示名） |
| content | Text | NOT NULL default=""（传给 Agent 的执行提示词）。老数据为空时 runner 回退到 description |
| enabled | Boolean | default=True, NOT NULL |
| rule_version | Integer | default=1, NOT NULL。执行时间实际变化时递增 |
| definition_version | Integer | default=1, NOT NULL。cron 或 prompt 变化时递增 |
| created_at | DateTime | default=now |
| updated_at | DateTime | default=now, onupdate=now |

UniqueConstraint: (user_id, name)

**`schedule` 字段语义**：后端不反解析 `cron_expr` 来推导 `schedule`。存在以下三种状态：
- `schedule != null`：表单创建/修改 → 后端 `schedule_to_cron()` 双写 `cron_expr`。编辑时前端读 `schedule` 回显 SchedulePicker。
- `schedule == null` 且 `cron_expr != null`：Agent 工具 / 老数据 创建的作业。前端编辑时只读展示 `cron_expr`，需明示点“重新选择”才能进入 SchedulePicker。
- 两者均为 null：非法状态，创建/修改必须被后端拒绝（400）。

**`schedule` JSON 结构列5 种 kind**（完整定义见 `src/api/services/cron_schedule.py`）：
- `{kind: "daily", time: "HH:MM"}`
- `{kind: "weekdays", time: "HH:MM"}` —— 映射到标准 Cron `1-5`（周一到周五）
- `{kind: "weekly", time: "HH:MM", days: number[]}` —— 标准 Cron 数值：0=周日、1=周一、…、6=周六
- `{kind: "monthly", time: "HH:MM", dayOfMonth: 1–31}`
- `{kind: "interval", everyMinutes?: 1–59} 或 {everyHours?: 1–23}`（二选一）

### cron_fires 表 (去重表)

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| job_id | Integer | FK → cron_jobs.id, NOT NULL, indexed |
| scheduled_at | DateTime | NOT NULL, indexed |
| rule_version | Integer | NOT NULL。认领时的规则版本 |
| definition_version | Integer | NOT NULL。入队时的完整定义版本 |
| run_id | String(36) | UNIQUE。与 Fire 同事务创建的 queued run |
| created_at | DateTime | default=now |

UniqueConstraint: (job_id, scheduled_at) — 跨 worker 去重的核心机制

### cron_job_runs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, indexed |
| job_id | Integer | nullable, indexed。历史快照，不设 FK，删除 job 后保留 |
| fire_id | String(36) | nullable, UNIQUE。手动运行为空 |
| job_name | String(100) | NOT NULL |
| cron_expr | String(50) | NOT NULL |
| rule_version | Integer | nullable。执行采用的规则版本 |
| definition_version | Integer | nullable。冻结定义版本 |
| definition_snapshot | Text | nullable。冻结的 job id/name/cron/prompt JSON |
| scheduled_at | DateTime | nullable。计划触发时间 |
| trigger_source | String(20) | default="scheduled"。scheduled/manual |
| queued_at | DateTime | 入队时间 |
| started_at | DateTime | nullable。成功 claim 后写入 |
| completed_at | DateTime | nullable |
| status | String(20) | queued/running/success/failed/conflict/unknown |
| phase | String(20) | queued/preparing/executing/publishing/terminal |
| claim_token / claim_worker_id | String | nullable。执行所有权 fence |
| claim_lease_expires_at / heartbeat_at | DateTime | nullable。可续租执行 lease |
| sandbox_id | String(100) | nullable。claim/Agent dispatch 冻结的 OpenSandbox 实例 ID；内部执行身份，不对普通客户端暴露 |
| attempt_count | Integer | default=0。pre-start 重排也使用同一 run id |
| error_code | String(80) | nullable。机器可读错误分类 |
| output | Text | nullable |
| is_read | Boolean | default=False |
| artifacts | Text | nullable (JSON array) |
| run_workspace | String(500) | nullable |
| workspace_changes | Text | nullable。统一发布入口已 applied 的工作区变更 JSON，≤100 项/64KiB |
| workspace_change_sets | Text | nullable。内部审计/恢复用的冻结 change set 摘要，≤100 项/64KiB；普通前端不渲染待确认入口 |

## 3. API 契约

All require Bearer auth.

### GET /api/cron/jobs

- Response 200: `{jobs: [{id, name, cron_expr, schedule, description, content, enabled, rule_version, definition_version}]}`
  - `schedule` 为老数据时为 `null`；后端不反解析 `cron_expr`。

### POST /api/cron/jobs

- Body: `{name (1-100, [A-Za-z0-9_-]), description? (<=500), content? (<=8000), schedule?, cron_expr?, enabled?: bool=true}`
  - `schedule` 与 `cron_expr` 二选一（`schedule` 优先）；两者都未提供 → 400。
  - `cron_expr` 必须是 5 字段标准 cron，且从当前时间起至少存在一次未来触发；语法非法或永不触发（如 `0 0 31 2 *`）→ 400，禁止写入 DB。
  - 重名 → 400。
- Response 201: `{job: <同上表项>}`
- Error 400 (校验失败), 503 (PostgreSQL 写冲突：死锁 / 序列化失败，建议重试)

### PUT /api/cron/jobs/{name}

- Body: `{description? (<=500), content? (<=8000), schedule?, cron_expr?, enabled?}` —— 所有字段可选，省略则保持原值；`name` 不可改。
- cron/prompt 任一实际变化都递增 `definition_version`；只有执行时间变化递增 `rule_version`。
- `schedule` / `cron_expr` 同斶传入 → 以 `schedule` 为准；两者均不传 → 时间不变。
- Response 200: `{job: ...}`
- Error 404 (任务不存在), 400 (其他校验失败), 503 (PostgreSQL 写冲突：死锁 / 序列化失败，建议重试)

### DELETE /api/cron/jobs/{name}

- Response 204 (空 body)。历史 `cron_job_runs` 保留；`cron_fires` 允许级联清理（按 `job_id` 删除），以保证删除可用。
- Error 404, 503 (PostgreSQL 写冲突：死锁 / 序列化失败，建议重试)

### POST /api/cron/jobs/preview

- Body: `{schedule?, cron_expr?, n?: 1-20=5}` —— 二选一。
- Response 200: `{schedule_text: str, cron_expr: str, next_fires: ISO datetime[]}` —— 本地时区 naive datetime。
- 仅鉴权，不读/写任何以 user_id 为维度的数据。
- Error 400 (未提供 / 同时提供 / schedule 非法 / cron 表达式非法)

### GET /api/cron/runs

- Query: job_name?: str, limit: int (1-100, default 20), offset: int (>=0, default 0)
- Response 200: `{runs: [CronJobRun.to_dict()], total, offset, limit}`；run 包含 queued/status/phase/attempt/error/workspace_changes，但不暴露 claim token。

### GET /api/cron/runs/unread-count

- Response 200: `{count: int}` — 统计 is_read=False（包含 success / failed，失败记录也需让用户看到）

### POST /api/cron/runs/mark-read

- Query: run_id?: str
  - 不传：当前用户所有未读记录全部标记为已读。
  - 传：仅标记归属于当前用户、且未读的该条记录（不存在/跨用户/已读 → marked=0）。
- Response 200: `{marked: int, unread_count: int}`。更新 `is_read` 与重新统计当前用户 `is_read=False` 在同一事务完成；App 级未读 store 是唯一前端 count owner，消息中心不保留第二份 count。

### GET /api/cron/runs/{run_id}

- Response 200: CronJobRun.to_dict()
- Error 404

### GET /api/cron/runs/{run_id}/files

- Response 200: `{files: [...]}` — 从 DB artifacts 字段或实时沙箱扫描
- `.workspace-change-sets` 是旧平台发布暂存目录：扫描、已存 artifacts 投影均排除，直接下载该目录下路径返回 404；普通用户自行生成的运行产物不受影响。
- Error 404, 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）

### GET /api/cron/runs/{run_id}/files/{path:path}

- Response: 文件字节流, Content-Disposition: attachment
- 鉴权双通道：
  - `Authorization: Bearer <token>`（标准方式，优先生效）
  - `?token=<access_token>`（兜底，用于浏览器 `<a>` 直链下载，access log / Referer 会记录 token，仅在受控环境使用；缺失任何一种均返回 401）
- Error 401（未提供 token）, 404, 403 ("路径越界"), 409（沙箱 Profile 配置冲突，如绑定后端不存在/禁用）, 503

### POST /api/cron/jobs/{job_name}/run

- Response 200: `{job_name, run_id, status: "accepted", message: "后台任务已执行"}`
- Error 404 ("任务不存在")
- 手动触发不写 cron_fires 去重表
- 先冻结定义并创建 `status=queued` 的 durable run，再提交 worker claim；不按用户串行排队

### Agent `manage_cron` 契约

- actions：`add` / `update` / `remove` / `list` / `toggle` / `history`。
- `add` 接受 `content`；未给 content 时 runner 回退 description。
- `update` 可修改 description/content/schedule/cron，至少提供一个字段；值未变化时 definition_version 不递增。
- HTTP 与 Agent 工具共用 CronService 校验、definition_version 和 user_id 隔离，不能形成第二套规则。

## 4. 行为语义与不变量

### 时区

- **调度基准：本地时区**（`TIMEZONE_OFFSET` 环境变量，默认 UTC+8）。
- 用户配置 `"0 9 * * *"` 会在**本地 9 点**触发，而非 UTC 9 点。
- DB 中 `CronFire.scheduled_at` / `CronJobRun.started_at` 等字段均以**本地 naive datetime** 存储（由 `now_naive()` 产生）。

### Cron 标准

- 全链路只接受项目定义的 Linux/Vixie 五字段数字语法，由 `CronEngine` 统一负责校验、匹配和未来时间计算：字段支持 `*`、数字、范围、列表以及 `*/N` / `A-B/N` 步进。
- 创建或修改调度规则时，服务层必须在任何 DB 写入前计算至少一次未来触发；无法产生未来时间的表达式不得保存，HTTP 与 Agent 工具遵循同一校验。
- 明确拒绝 croniter 扩展语法（包括 `R`、`L`、`W`、`#`、`?`）和英文月份/星期名，防止随机或扩展表达式造成保存预览与 worker 逐分钟匹配不一致。
- 星期字段采用 `0/7=周日，1=周一，…，6=周六`；例如 `1-5` 为周一至周五，`2-6,0` 为周二至周日。
- 日（day-of-month）与星期（day-of-week）同时受限时采用标准 Cron 的 OR 语义。
- 不提供 APScheduler 星期编号或其他旧规则兼容层；数据库中的 `cron_expr` 直接按上述标准解释。

### 调度机制

- `CronEngine` 基于 `croniter` 做单分钟语义匹配和未来时间计算；持久化与去重全部走 DB。
- Worker 每分钟唤醒一次（对齐到本地分钟边界 + 0-2s 随机 jitter）。若同进程事件循环被阻塞导致醒来时发现漏过分钟，补扫最近 `cron_dispatch_catch_up_max_minutes` 分钟（默认 60），并以原始分钟写入 `cron_fires.scheduled_at`；进程启动前的历史时间不回补
- 查询所有 enabled CronJob，通过 `CronEngine.matches()` 匹配当前分钟（本地时区）
- 匹配成功后在同一 PostgreSQL 事务内执行 `INSERT ... ON CONFLICT DO NOTHING` 写入 cron_fires，并创建唯一 `status=queued` 的 CronJobRun；任何一项失败都回滚
  - 插入成功 = 本 worker 赢得该分钟的入队权；`cron_fires.run_id` 与 queued run 立即成为 durable 事实
  - 插入失败 (UNIQUE 冲突) = 其他 worker 已认领
  - PostgreSQL 写冲突（SQLSTATE `40001` / `40P01`）→ 当前 worker 放弃本次抢占；若无其他 worker 成功认领，则等下一次 cron 命中再触发（同分钟不会重补）

### 执行前二次校验

- queued run 冻结 enqueue 时的 prompt 与 definition_version。claim 后、实际执行前必须按 `job_id` 重新复核：
- dispatch 轻量快照后若只有 definition_version 前进而 rule_version 未变，enqueue 使用数据库当前最新定义，不能因此静默漏掉该分钟；rule_version 已变化则不再按旧时间入队。
  - job 不存在 → skip
  - `cron_jobs.enabled == False` → skip
  - `cron_jobs.rule_version` 与认领时版本不一致 → skip
  - `auth_users` 中没有对应用户 → skip
  - `auth_users.enabled == False` → skip
- 实现位置：`cron_worker._run` 和 `run_cron_job()`。
- 手动触发（`POST /api/cron/jobs/{name}/run`）不写 `cron_fires`，但 `run_cron_job()` 执行入口仍必须校验 `auth_users` 用户存在且启用；用户在触发后被禁用/删除时，预创建的 run 记录应转为 `failed`。

### 执行流程

1. Worker 对 queued run 行加锁，写 running/preparing、随机 claim_token、worker_id、lease 与 attempt_count，并把已有 `UserSandbox.sandbox_id` 冻结到 run
2. 若 run 已有 `sandbox_id`，只能 `get_existing` 连接并续租该实例；实例不可恢复时本轮失败，禁止 `get_or_resume` 创建替代代际。首次尚无绑定时允许创建一次，但必须在 Agent dispatch 前受 claim fence 写回 `CronJobRun.sandbox_id`
3. Workspace = `{mount}/cron/runs/{run_id}`；普通产物仍写入此 scratch
4. AgentService 始终暴露完整持久工作区工具，由任务 prompt 决定是否使用及操作目标；每次工作区工具调用都注入 `assert_cron_workspace_lease` fence
5. phase 进入 executing，`Agent.run_agui` 执行；heartbeat 周期续租 claim
6. phase 进入 publishing；扫描 scratch artifacts。Workspace 工具先在 run scratch 冻结 change set，再由统一发布入口校验 base/current version并自动三方合并；最终正式版本记入 `workspace_changes`，change set 只作内部审计/恢复
7. 仅 claim_token 仍匹配的 worker 可写 terminal、清理 claim 并提交 output/artifacts/workspace_changes

### 记忆与配置文件边界

- Cron Agent 不提供 `update_long_term_memory` / `search_memory` / `read_user` / `update_user`，避免定时执行器默认具备对话记忆检索、整理和专用写入能力。
- Cron Agent 不提供 `ask_user` 或 `sub_agent`：定时任务无人值守，不能等待用户输入，也不能递归创建子 Agent run。
- Cron Round 收尾不启动任何模型记忆提取或 canonical Memory 写入流程。
- Cron Agent 复用共享的 `apply_patch` 文本变更工具。若补丁修改根目录 `{mount}/USER.md`、`{mount}/MEMORY.md`、`{mount}/SOUL.md`，则按 [memory-spec.md](./memory-spec.md) 的「受控工具即时同步」语义同步回 DB；`{mount}/AGENTS.md` 由平台模板统一管理，只读且不回写用户 DB。
- 上述根目录配置写入只用于任务明确要求更新用户画像、长期记忆或 Agent 配置的场景；普通执行产物仍必须保存在 run workspace，并只作为 cron artifacts 扫描和展示。

### 持久工作区能力

- 定时任务不配置、持久化或冻结任务级工作区权限；前端、HTTP API 与 `manage_cron` 均不提供权限字段。
- 每次 Cron 运行都获得完整工作区工具，可读取并冻结创建、更新、移动、重命名和直接删除（永久删除，不产生可恢复提案）；是否访问、操作目标与具体动作完全由用户保存的任务 prompt 决定，不采用任务预绑定文件列表。正式工作区仍只能由统一发布入口自动合并后修改。
- 工作区是平台固定能力；底层未启用持久挂载时由 WorkspaceService 返回 `WORKSPACE_PERSISTENCE_DISABLED`，不得静默隐藏工具或降级为临时目录。
- Cron Agent 注册标准 `bash` / `bash_output` / `bash_kill` 与通用文件工具，允许按任务 prompt 执行 Skill 脚本、Python、Node 和其他命令；默认工作目录仍是 run scratch。
- 产品契约信任模型遵循 system prompt：普通产物写入 run scratch，持久工作区的读取与变更走 WorkspaceTool，不能用 Bash 或通用文件工具绕过 revision、配额、lease fence 与 mutation audit。这里是模型行为约束，不是容器级文件系统隔离。
- WorkspaceService mutation/change set 的 `actor="cron"`，context 同时记录 `cron_run_id=ToolRuntimeContext.thread_id` 与内部 `round_id`；每个提案使用 tool-call 幂等键并保存 base version。
- `workspace_changes` 只记录已 applied 的持久变更；base 已变化时统一发布入口做格式感知三方合并，同一行/单元格保留人的正式内容，不同位置合入 Cron 修改。`workspace_change_sets` 的提案内容进入用户内 SHA 对象并由显式 reference 保护，终态后仅按审计保留期保存；调用取消/worker 丢失由 Workspace maintenance 继续 apply，而不是等待人工或重跑 Cron。普通前端不显示“待确认”或发布/拒绝按钮；run scratch 文件继续走 `artifacts`。
- 自动合并无法可靠拆分时保持当前正式内容并将原因写入 change set 审计；Cron run 仍按 Agent 执行事实收敛，不等待人工输入，也不重放整个任务。
- 即时 change recorder 失败不得否认已经成功的 WorkspaceMutation；独立 reconciler 从 `workspace_mutations.cron_run_id` 日志幂等补齐 CronJobRun.workspace_changes。

### 未读语义（is_read × status）

`is_read` 与 `status` 是**两个正交字段**，组合规则如下：

- `is_read` 默认 `False`，**所有终态记录都进入未读队列**，无论 `status=success` 还是 `status=failed`。
  - 失败也必须让用户感知：cron 静默失败是用户最难察觉的问题，强制计入未读是产品决策，不是实现细节。
- `queued/running` 中的记录也是 `is_read=False`，但前端不应把它当成"待用户处理的消息"展示，仅作为执行进度。
- 未读 → 已读的转换路径**仅有一条**：用户主动调用 `POST /api/cron/runs/mark-read`（带或不带 `run_id`）。
  - 系统不做任何自动已读：不因查看详情、不因下载 artifact、不因 TTL 过期。
- 未读计数（`GET /api/cron/runs/unread-count`）严格等价于 `WHERE user_id=? AND is_read=False`，**不再过滤 status**。
  - 历史上曾过滤 `status=success`，会导致失败任务永远不进入未读、用户毫无感知；该行为已废弃，不允许回退。

不变量：
1. 一条 run 记录被标记 `is_read=True` 后，不存在任何路径会把它改回 `False`。
2. 未读计数 = 详情列表中 `is_read=False` 的条目数（前后端口径一致）。
3. 失败记录的可见性等价于成功记录的可见性，二者在未读体系内对等。

### 手动触发与调度并发

- 手动触发不写 `cron_fires`，允许与同分钟的自动调度并存。
- 手动触发绑定提交时的 `job_id`、`rule_version`、`definition_version` 与完整定义快照；后台真正开始前任务被删除重建或调度规则已变化时失败，prompt 后续变化不改写已入队 run。
- 手动触发与自动调度均不按用户串行排队；提交后立即进入后台执行。
- 跨 worker 触发的同一作业同分钟去重约束见下方「多 worker 并发模型」。

### 作业删除语义

- `DELETE /api/cron/jobs/{name}` 的目标是确保任务可删除：
  - `cron_job_runs` 历史保留（消息中心仍可展示过往执行）
  - `cron_fires` 允许按 `job_id` 级联清理（不影响历史展示）
- 级联清理仅影响去重键历史，不改变 `cron_job_runs` 的可见性与统计。

### 多 worker 并发模型

- `cron_fires` UNIQUE 约束保证：**同一 (job_id, scheduled_at) 在所有 worker 中只会被执行一次**。
- `CronJobRun.status=queued` 是 durable queue；worker 通过行锁将其 claim 为 running，claim_token/lease 是之后所有状态写与工作区副作用的 ownership fence。
- 因此**不变量边界**：
  - 单 worker：同一用户的不同作业（自动调度 + 手动触发）可以并行执行。
  - 多 worker：同一用户的不同作业也可以并行执行。
  - 同一作业（同 job_id 同分钟）在任何部署模式下都不会被并行触发（由 `cron_fires` 兜底）。
- 影响范围与缓解：
  - 用户沙箱：每条 claimed run 使用 durable `sandbox_id` 作为代际身份；后来的用户绑定或进程内 cache 不得替换正在执行的实例。Session 列表/预览/上传等被动请求发现 fresh Cron claim 时也只能连接该 run 的 frozen ID。
  - `cron_job_runs` 写入：每条记录有独立 `run_id`，不会互相覆盖。
  - 手动触发：`POST /jobs/{name}/run` 与同分钟自动调度可能并行执行同一作业；调用方需自行接受这种语义。
- 如需重新引入严格全局串行：未来可在 `_run` 入口增加 DB 级 user lock（如 `INSERT INTO user_run_locks` 抢占），不在本期实现范围。

### 手动触发失败兜底

- `POST /api/cron/jobs/{name}/run` 在 `trigger_manual_run` 抛出**任意异常**（HTTPException 503、RuntimeError 等）时，必须把已预创建的 `queued/running` 记录立刻标记为 `failed`。
- 否则记录会一直挂着直到下次 startup 统一清理 → 前端长期转圈。

### Dispatch 异常隔离

- `_dispatch_and_run` 的 per-job 循环必须 try/except 包裹整段（cron 解析、匹配、`_try_insert_fire`）。
- 单个 job 的异常仅记录 `logger.exception`，不能影响同分钟其他 job 的调度。

### Lease 对账与启动恢复

- startup 与独立 reconciler 都只处理 lease 过期的 running run；新鲜 lease 属于其他存活 worker，不得修改。
- `CronJobRun.id` 同时是内部 Cron Session.id；startup 在扫描孤儿 Round 前必须把 `status=running + claim_token 非空 + claim_lease_expires_at > now` 的 run id 加入 `protected_session_ids`，不能先误杀关联 Round 再对账 Cron lease。
- `phase=preparing` 且存在旧 claim_token：尚未越过 Agent dispatch，可清 claim、恢复 queued，并使用同一 run_id 重新 claim。
- `phase=executing/publishing`，或没有 durable claim 的旧 running 行：可能已经产生副作用，必须收敛 `status=unknown`、`error_code=worker_lease_expired_after_start`，绝不自动重放。
- startup 不再 blanket fail 所有 running Cron；rolling worker 启动不能误杀另一 worker 的新鲜执行。

### 周期性清理

- Worker loop 内每日（当 `minute.hour == 0` 且日期与上次清理不同）触发 `_cleanup_old_fires()`。
- 删除 `cron_fires` 表中 `scheduled_at < now - cron_fire_max_age_days` 的记录（默认 7 天，通过 `settings.cron_fire_max_age_days` 配置；≤0 时禁用清理）。
- 删除操作幂等，多 worker 并发清理同一批过期记录也不会改变最终结果。
- worker 启动当天若已过 00:xx，本日不补清，最早次日 00:xx 触发。

### Worker 生命周期

- `start_cron_worker`：先对账过期 claim、恢复 queued run，再注册 dispatch loop 与独立 reconciler loop 到 `app.state`。
- `stop_cron_worker`：取消 worker task，随后 `asyncio.wait(in_flight, timeout=30)` 优雅等待正在执行的 cron run，超时再强制 cancel。
- 错误恢复：loop 异常后 sleep 5s 重试。
- `_background_tasks` set 保留强引用，防止 `create_task` 返回的 Task 被 GC 回收。

### Claim 配置

| 环境变量 | 默认值 | 约束与语义 |
|---|---:|---|
| `CRON_CLAIM_LEASE_SECONDS` | `120` | queued run 被 worker claim 后的执行租约，必须大于 0。 |
| `CRON_CLAIM_HEARTBEAT_SECONDS` | `30` | executing worker 的续租周期，必须大于 0 且小于 claim lease。 |
| `CRON_RECONCILE_INTERVAL_SECONDS` | `30` | 过期 claim 对账与 durable queued run 恢复周期，必须大于 0。 |

## 5. 失败模式与错误处理

- 作业不存在 → 预创建的 run record 标记为 failed
- 沙箱 resume 失败 → run failed
- Agent 执行异常 → run failed, output 记录错误信息
- pre-start claim 过期 → 同 run_id 重新 queued；post-start lease 过期 → unknown 且不重放
- 工作区工具调用时 claim/lease 不匹配 → fail closed，不执行 WorkspaceService
- Artifact 扫描失败 → 降级（最终只返回路径）
- PostgreSQL 写冲突（SQLSTATE `40001` / `40P01`）→ 本 worker 放弃本分钟抢占；下一次 cron 命中恢复正常
- CRUD 写接口遇到 PostgreSQL 死锁 / 序列化失败 → 返回 503（瞬时冲突，调用方重试）
- 调度快照期间 job 被删除/禁用 → `_run_by_id` 二次校验 skip

## 6. 可观测性

- Worker 启动/停止日志
- 每分钟调度匹配结果日志
- 执行开始/完成/失败日志（含 job_name, run_id, user_id）
- Artifact 扫描 fallback 日志

## 7. 非目标

- 不做秒级调度（最小粒度 = 1 分钟）
- 不做作业依赖编排
- 不做越过 Agent dispatch 后的整体执行重试；只恢复明确处于 preparing 的同一 durable run
- 不引入外部分布式锁（依赖 `cron_fires` 的 PostgreSQL UNIQUE 约束）
- 不做执行日志实时流式输出
- 不做 `cron_expr` 反解析 → `schedule`；老数据/工具创建的作业 `schedule=null`，前端只能读 `cron_expr` 只读展示。

## 8. 未来演进

### `cron_fires` → Redis（可选）

`cron_fires` 不再只是短期 TTL 键：它与 queued CronJobRun 的同事务创建共同关闭 Fire→spawn 崩溃窗口。迁移 Redis 前必须先提供等价的 durable enqueue 原子性；不能只把 UNIQUE 替换为 `SET NX` 后保留分裂写入。

- 推荐让 PostgreSQL queued run/outbox 继续作为事实源，Redis 只负责唤醒 worker；同事务内创建 run 后再异步投递通知。
- 若彻底移除 cron_fires，应先为 scheduled run 建立等价的 `(job_id, scheduled_at)` 唯一约束，再以 run 行本身承担去重与 queue intent。
- 只有支持跨 PostgreSQL/Redis 原子提交或可证明的 outbox relay 后，才允许 Redis `SET NX` 成为仲裁实现。

删除工具为 workspace_delete：直接删除文件/目录及其工作区历史，不提供恢复。已提交删除通过 DELETED 与 affected_entry_ids 失效前端，不按路径绑定后来同名文件。
