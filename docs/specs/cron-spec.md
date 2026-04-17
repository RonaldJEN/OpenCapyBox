# 定时任务 (Cron) — Spec

## 1. 模块职责边界

- Cron 作业定义与管理
- 定时调度与执行（去中心化 worker）
- 执行历史查询与分页
- 手动触发
- 未读计数与已读标记（消息中心）
- 执行产物（artifacts）查看与下载
- 不负责：作业编辑 UI、复杂调度策略（仅 5-field cron）

## 2. 数据模型

### cron_jobs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK, autoincrement |
| user_id | String(100) | NOT NULL, indexed |
| name | String(100) | NOT NULL |
| cron_expr | String(50) | NOT NULL |
| description | Text | default="" |
| enabled | Boolean | default=True, NOT NULL |
| created_at | DateTime | default=now |
| updated_at | DateTime | default=now, onupdate=now |

UniqueConstraint: (user_id, name)

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

- Response 200: `{jobs: [{id, user_id, name, cron_expr, description, enabled, created_at, updated_at}]}`

### GET /api/cron/runs

- Query: job_name?: str, limit: int (1-100, default 20), offset: int (>=0, default 0)
- Response 200: `{runs: [CronJobRun.to_dict()], total, offset, limit}`

### GET /api/cron/runs/unread-count

- Response 200: `{count: int}` — 统计 status=success AND is_read=False

### POST /api/cron/runs/mark-read

- Query: run_id?: str (省略则标记全部未读)
- Response 200: `{marked: int}`

### GET /api/cron/runs/{run_id}

- Response 200: CronJobRun.to_dict()
- Error 404

### GET /api/cron/runs/{run_id}/files

- Response 200: `{files: [...]}` — 从 DB artifacts 字段或实时沙箱扫描
- Error 404

### GET /api/cron/runs/{run_id}/files/{path:path}

- Response: 文件字节流, Content-Disposition: attachment
- Error 404, 403 ("路径越界"), 503

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

- Worker 每分钟唤醒一次（对齐到本地分钟边界 + 0-2s 随机 jitter）
- 查询所有 enabled CronJob，通过 APScheduler CronTrigger 匹配当前分钟（本地时区）
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

1. 获取 per-user lock（同一用户的作业串行执行）
2. Resume 用户沙箱
3. 创建临时 Agent（排除 AskUserQuestionTool 和 memory tools）
4. Workspace = `{mount}/cron/runs/{run_id}`，通过 `mkdir -p {shlex.quote(...)}` 创建（防 mount 路径含空格时注入）
5. `Agent.run_agui` 执行
6. 扫描 artifacts（3 种 fallback: GNU `find -printf` → `stat -c` → path-only）
7. 更新 run record（output 截断 10000 字符, artifacts JSON ≤ 64KB / 100 entries）

### 手动触发与调度并发

- 手动触发不写 `cron_fires`，允许与同分钟的自动调度并存。
- 两者竞争同一个 per-user lock，实际表现为**串行执行**（先拿到锁的先跑）。

### 启动清理

- `main.py` startup：标记运行超过 1 小时的 cron run 为 `failed`，防止前端永久 running。

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
- 调度快照期间 job 被删除/禁用 → `_run_by_id` 二次校验 skip

## 6. 可观测性

- Worker 启动/停止日志
- 每分钟调度匹配结果日志
- 执行开始/完成/失败日志（含 job_name, run_id, user_id）
- Artifact 扫描 fallback 日志

## 7. 非目标

- 不做作业定义的 CRUD API（作业通过 Agent 工具管理，存 DB）
- 不做秒级调度（最小粒度 = 1 分钟）
- 不做作业依赖编排
- 不做执行重试（失败即终态）
- 不做分布式锁（依赖 SQLite UNIQUE 约束）
- 不做执行日志实时流式输出
