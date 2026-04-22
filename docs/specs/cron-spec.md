# 定时任务 (Cron) — Spec

## 1. 模块职责边界

- Cron 作业定义与管理（CRUD：HTTP 表单与 Agent 工具共用）
- 结构化时间配置（`schedule`）↔ cron 表达式（`cron_expr`）转换
- 定时调度与执行（去中心化 worker）
- 执行历史查询与分页
- 手动触发
- 未读计数与显式已读标记（消息中心）
- 执行产物（artifacts）查看与下载
- 不负责：复杂调度策略（仅 5-field cron）

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
| created_at | DateTime | default=now |
| updated_at | DateTime | default=now, onupdate=now |

UniqueConstraint: (user_id, name)

**`schedule` 字段语义**：后端不反解析 `cron_expr` 来推导 `schedule`。存在以下三种状态：
- `schedule != null`：表单创建/修改 → 后端 `schedule_to_cron()` 双写 `cron_expr`。编辑时前端读 `schedule` 回显 SchedulePicker。
- `schedule == null` 且 `cron_expr != null`：Agent 工具 / 老数据 创建的作业。前端编辑时只读展示 `cron_expr`，需明示点“重新选择”才能进入 SchedulePicker。
- 两者均为 null：非法状态，创建/修改必须被后端拒绝（400）。

**`schedule` JSON 结构列5 种 kind**（完整定义见 `src/api/services/cron_schedule.py`）：
- `{kind: "daily", time: "HH:MM"}`
- `{kind: "weekdays", time: "HH:MM"}` —— 映射到 `day_of_week=0-4`（周一到周五）
- `{kind: "weekly", time: "HH:MM", days: number[]}` —— 0=周一 .. 6=周日（与 APScheduler `day_of_week` 一致）
- `{kind: "monthly", time: "HH:MM", dayOfMonth: 1–31}`
- `{kind: "interval", everyMinutes?: 1–59} 或 {everyHours?: 1–23}`（二选一）

### cron_fires 表 (去重表)

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| job_id | Integer | FK → cron_jobs.id, NOT NULL, indexed |
| scheduled_at | DateTime | NOT NULL, indexed |
| created_at | DateTime | default=now |

UniqueConstraint: (job_id, scheduled_at) — 跨 worker 去重的核心机制

### cron_job_runs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | String(36) | PK |
| user_id | String(100) | NOT NULL, indexed |
| job_name | String(100) | NOT NULL |
| cron_expr | String(50) | NOT NULL |
| started_at | DateTime | default=now |
| completed_at | DateTime | nullable |
| status | String(20) | default="running". Values: running/success/failed |
| output | Text | nullable |
| is_read | Boolean | default=False |
| artifacts | Text | nullable (JSON array) |
| run_workspace | String(500) | nullable |

## 3. API 契约

All require Bearer auth.

### GET /api/cron/jobs

- Response 200: `{jobs: [{id, name, cron_expr, schedule, description, content, enabled}]}`
  - `schedule` 为老数据时为 `null`；后端不反解析 `cron_expr`。

### POST /api/cron/jobs

- Body: `{name (1-100, [A-Za-z0-9_-]), description? (<=500), content? (<=8000), schedule?, cron_expr?, enabled?: bool=true}`
  - `schedule` 与 `cron_expr` 二选一（`schedule` 优先）；两者都未提供 → 400。
  - `cron_expr` 必须是 5 字段标准 cron；不是 → 400。
  - 重名 → 400。
- Response 201: `{job: <同上表项>}`
- Error 400 (校验失败), 503 (SQLite 写锁冲突，建议重试)

### PUT /api/cron/jobs/{name}

- Body: `{description? (<=500), content? (<=8000), schedule?, cron_expr?, enabled?}` —— 所有字段可选，省略则保持原值；`name` 不可改。
- `schedule` / `cron_expr` 同斶传入 → 以 `schedule` 为准；两者均不传 → 时间不变。
- Response 200: `{job: ...}`
- Error 404 (任务不存在), 400 (其他校验失败), 503 (SQLite 写锁冲突，建议重试)

### DELETE /api/cron/jobs/{name}

- Response 204 (空 body)。历史 `cron_job_runs` 保留；`cron_fires` 允许级联清理（按 `job_id` 删除），以保证删除可用。
- Error 404, 503 (SQLite 写锁冲突，建议重试)

### POST /api/cron/jobs/preview

- Body: `{schedule?, cron_expr?, n?: 1-20=5}` —— 二选一。
- Response 200: `{cron_expr: str, next_fires: ISO datetime[]}` —— 本地时区 naive datetime。
- 仅鉴权，不读/写任何以 user_id 为维度的数据。
- Error 400 (未提供 / 同时提供 / schedule 非法 / cron 表达式非法)

### GET /api/cron/runs

- Query: job_name?: str, limit: int (1-100, default 20), offset: int (>=0, default 0)
- Response 200: `{runs: [CronJobRun.to_dict()], total, offset, limit}`

### GET /api/cron/runs/unread-count

- Response 200: `{count: int}` — 统计 is_read=False（包含 success / failed，失败记录也需让用户看到）

### POST /api/cron/runs/mark-read

- Query: run_id?: str
  - 不传：当前用户所有未读记录全部标记为已读。
  - 传：仅标记归属于当前用户、且未读的该条记录（不存在/跨用户/已读 → marked=0）。
- Response 200: `{marked: int}`

### GET /api/cron/runs/{run_id}

- Response 200: CronJobRun.to_dict()
- Error 404

### GET /api/cron/runs/{run_id}/files

- Response 200: `{files: [...]}` — 从 DB artifacts 字段或实时沙箱扫描
- Error 404

### GET /api/cron/runs/{run_id}/files/{path:path}

- Response: 文件字节流, Content-Disposition: attachment
- 鉴权双通道：
  - `Authorization: Bearer <token>`（标准方式，优先生效）
  - `?token=<access_token>`（兜底，用于浏览器 `<a>` 直链下载，access log / Referer 会记录 token，仅在受控环境使用；缺失任何一种均返回 401）
- Error 401（未提供 token）, 404, 403 ("路径越界"), 503

### POST /api/cron/jobs/{job_name}/run

- Response 200: `{job_name, run_id, status: "accepted", message: "后台任务已执行"}`
- Error 404 ("任务不存在")
- 手动触发不写 cron_fires 去重表
- 共享 per-user 串行锁

## 4. 行为语义与不变量

### 时区

- **调度基准：本地时区**（`TIMEZONE_OFFSET` 环境变量，默认 UTC+8）。
- 用户配置 `"0 9 * * *"` 会在**本地 9 点**触发，而非 UTC 9 点。
- DB 中 `CronFire.scheduled_at` / `CronJobRun.started_at` 等字段均以**本地 naive datetime** 存储（由 `now_naive()` 产生）。
- APScheduler `CronTrigger` 构造时必须传 `timezone=get_timezone()`。

### 调度机制

- 仅借用 `apscheduler.triggers.cron.CronTrigger` 做单分钟语义匹配，**未使用任何 scheduler 主进程或 jobstore**（无 `AsyncIOScheduler` / 文件锁主节点）；持久化与去重全部走 DB。
- Worker 每分钟唤醒一次（对齐到本地分钟边界 + 0-2s 随机 jitter）
- 查询所有 enabled CronJob，通过一次性 `CronTrigger` 匹配当前分钟（本地时区）
- 匹配成功后 INSERT OR IGNORE 到 cron_fires 表
  - 插入成功 = 本 worker 赢得执行权
  - 插入失败 (UNIQUE 冲突) = 其他 worker 已认领
  - SQLite OperationalError (busy) → 本分钟调度丢失，下次 cron 命中再触发（同分钟不会重补）

### 执行前二次校验

- dispatch 查询结果是**内存快照**。在 `_run` 实际执行前，必须按 `job_id` 重新从 DB 拉取：
  - job 不存在 → skip（用户已删除）
  - `enabled == False` → skip（用户已禁用）
- 实现位置：`cron_worker._run_by_id`。
- 手动触发（`POST /api/cron/jobs/{name}/run`）路径不经此校验（路由层已在请求时校验 job 存在）。

### 执行流程

1. 获取 per-user lock（同一 worker 进程内同用户作业串行执行；跨 worker 不串行，详见下方「多 worker 并发模型」）
2. Resume 用户沙箱
3. 创建临时 Agent（排除 AskUserQuestionTool 和 memory tools）
4. Workspace = `{mount}/cron/runs/{run_id}`，通过 `mkdir -p {shlex.quote(...)}` 创建（防 mount 路径含空格时注入）；后续所有针对 workspace 的 shell 命令（artifact 扫描的 `find` 链）同样使用 `shlex.quote` 保护
5. `Agent.run_agui` 执行
6. 扫描 artifacts（fallback 链：GNU `find -printf '%p\t%s'` → `find -exec stat -c '%n\t%s'` → 仅路径 `find -type f`；最后一档 size 置 0）
7. 更新 run record（output 截断 10000 字符, artifacts JSON ≤ 64KB / 100 entries）

### 未读语义（is_read × status）

`is_read` 与 `status` 是**两个正交字段**，组合规则如下：

- `is_read` 默认 `False`，**所有终态记录都进入未读队列**，无论 `status=success` 还是 `status=failed`。
  - 失败也必须让用户感知：cron 静默失败是用户最难察觉的问题，强制计入未读是产品决策，不是实现细节。
- `running` 中的记录也是 `is_read=False`，但前端不应把它当成"待用户处理的消息"展示，仅作为执行进度。
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
- 两者竞争同一个 per-user lock（同一 worker 进程内），实际表现为**串行执行**（先拿到锁的先跑）。
- 跨 worker 触发的并发约束见下方「多 worker 并发模型」。

### 作业删除语义

- `DELETE /api/cron/jobs/{name}` 的目标是确保任务可删除：
  - `cron_job_runs` 历史保留（消息中心仍可展示过往执行）
  - `cron_fires` 允许按 `job_id` 级联清理（不影响历史展示）
- 级联清理仅影响去重键历史，不改变 `cron_job_runs` 的可见性与统计。

### 多 worker 并发模型

- `cron_fires` UNIQUE 约束保证：**同一 (job_id, scheduled_at) 在所有 worker 中只会被执行一次**。
- per-user 内存锁（`app.state.cron_user_locks`）只在**单个 worker 进程**内生效；多 uvicorn worker 部署下，每个进程持有独立 dict。
- 因此**不变量边界**：
  - 单 worker：同一用户的所有作业（自动调度 + 手动触发）严格串行。
  - 多 worker：同一用户的不同作业可能被不同 worker 抢到，**会并行执行**。
  - 同一作业（同 job_id 同分钟）在任何部署模式下都不会被并行触发（由 `cron_fires` 兜底）。
- 影响范围与缓解：
  - 用户沙箱：`get_or_resume` 是幂等的；并发 resume 同一沙箱由 OpenSandbox 层处理。
  - `cron_job_runs` 写入：每条记录有独立 `run_id`，不会互相覆盖。
  - 手动触发：`POST /jobs/{name}/run` 与同分钟自动调度可能在不同 worker 并行执行同一作业；调用方需自行接受这种语义。
- 如需严格全局串行：未来可在 `_run` 入口增加 DB 级 user lock（如 `INSERT INTO user_run_locks` 抢占），不在本期实现范围。

### 手动触发失败兜底

- `POST /api/cron/jobs/{name}/run` 在 `trigger_manual_run` 抛出**任意异常**（HTTPException 503、RuntimeError 等）时，必须把已预创建的 `running` 记录立刻标记为 `failed`，并写入失败原因（HTTPException 取 `detail`，其余取 `repr`）。
- 否则记录会一直挂着等 startup 1 小时清理 → 前端永久转圈。

### Dispatch 异常隔离

- `_dispatch_and_run` 的 per-job 循环必须 try/except 包裹整段（cron 解析、匹配、`_try_insert_fire`）。
- 单个 job 的异常仅记录 `logger.exception`，不能影响同分钟其他 job 的调度。

### 启动清理

- `main.py` startup：标记运行超过 1 小时的 cron run 为 `failed`，防止前端永久 running。

### 周期性清理

- Worker loop 内每日（当 `minute.hour == 0` 且日期与上次清理不同）触发 `_cleanup_old_fires()`。
- 删除 `cron_fires` 表中 `scheduled_at < now - cron_fire_max_age_days` 的记录（默认 7 天，通过 `settings.cron_fire_max_age_days` 配置；≤0 时禁用清理）。
- 删除操作幂等，多 worker 并发下由 SQLite 写锁串行化，不会产生冲突。
- worker 启动当天若已过 00:xx，本日不补清，最早次日 00:xx 触发。

### Worker 生命周期

- `start_cron_worker`：注册到 `app.state`。
- `stop_cron_worker`：取消 worker task，随后 `asyncio.wait(in_flight, timeout=30)` 优雅等待正在执行的 cron run，超时再强制 cancel。
- 错误恢复：loop 异常后 sleep 5s 重试。
- `_background_tasks` set 保留强引用，防止 `create_task` 返回的 Task 被 GC 回收。

## 5. 失败模式与错误处理

- 作业不存在 → 预创建的 run record 标记为 failed
- 沙箱 resume 失败 → run failed
- Agent 执行异常 → run failed, output 记录错误信息
- Artifact 扫描失败 → 降级（最终只返回路径）
- SQLite busy → 本分钟调度丢失（不重试同分钟，cron_fires 键不变时无法重新抢占）；下一次 cron 命中恢复正常
- CRUD 写接口遇到 SQLite `database is locked|busy` → 返回 503（瞬时冲突，调用方重试）
- 调度快照期间 job 被删除/禁用 → `_run_by_id` 二次校验 skip

## 6. 可观测性

- Worker 启动/停止日志
- 每分钟调度匹配结果日志
- 执行开始/完成/失败日志（含 job_name, run_id, user_id）
- Artifact 扫描 fallback 日志

## 7. 非目标

- 不做秒级调度（最小粒度 = 1 分钟）
- 不做作业依赖编排
- 不做执行重试（失败即终态）
- 不做分布式锁（依赖 SQLite UNIQUE 约束）
- 不做执行日志实时流式输出
- 不做 `cron_expr` 反解析 → `schedule`；老数据/工具创建的作业 `schedule=null`，前端只能读 `cron_expr` 只读展示。

## 8. 未来演进

### `cron_fires` → Redis（可选）

`cron_fires` 表语义上等价于一个带 TTL 的分布式锁，仅在 SQLite 单机部署下作为执行权仲裁凭证。若未来切多机部署或引入 Redis，可按以下路径迁移：

- 抽象 `FireArbiter` 接口：`try_acquire(job_id, minute) -> bool`
- 保留当前实现为 `SqliteFireArbiter`（`INSERT OR IGNORE`）
- 新增 `RedisFireArbiter`：`SET cron:fire:{job_id}:{minute} 1 NX PX <ttl_ms>`
  - TTL 按 cron 最小粒度设（如 120s），自动回收，无需历史行清理
- 按部署模式（单机 vs 多机）在启动时选择具体实现

迁移后 `cron_fires` 表与对应 Model 整体下线；`cron_jobs` / `cron_job_runs` 不受影响。
