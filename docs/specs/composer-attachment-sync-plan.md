# 对话附件大文件：沙箱同步进度与重试 — Plan

状态：已实施（2026-10-19 实施，2026-10-20 复审后修正）。实际行为以 `frontend-chat-spec`、`sessions-spec`、`sandbox-spec` 为准，与本计划的差异记录在本文末尾。

## 1. 目标

对话输入框（Composer）附件支持最大 200MB 的文件，"正在保存"阶段显示后端同步到沙箱的真实进度；连接断开、后端重启等情况下用户都能看到确定的结果并能重试，不再出现"卡在正在保存"或重试被 409 拒绝。

不在本次范围：浏览器到后端这一跳的断点续传；沙箱准备阶段（恢复 / 重建容器）的细粒度进度；独立 Workspace 上传接口。

## 2. 现状

对话附件要传两遍：

| 跳 | 路径 | 现状 |
| --- | --- | --- |
| 第一跳 | 浏览器 → 后端 `POST /api/composer-drafts/{draft_id}/attachments` | axios 字节进度到 100% 即显示"正在保存"（`frontend/src/components/ChatV2.tsx:968`）；整个请求固定 60 秒超时（`frontend/src/services/api.ts:162`） |
| 第二跳 | 后端 → OpenSandbox | `composer_draft_attachment_service.py:84-200`：整文件读入内存、sha256、写 ledger、`_ensure_sandbox`（可能恢复/重建容器）、`mkdir`、`files.write_file` 原样再传一遍、沙箱内 `wc -c` + `sha256sum` 校验，全部完成后才返回 |

- 第二跳走服务器到 OpenSandbox 的公网出口，是耗时主体；期间前端没有任何进度信息。
- SDK 的 `write_files` 接受文件对象并直接交给 httpx `files=`（`opensandbox/adapters/filesystem_adapter.py:260`），httpx 按 64KB 分块读取发送（`httpx/_multipart.py:120,214`）。这是采集第二跳进度的落点。
- `sandbox_service.py:157` 的 SDK `request_timeout=60s` 是 httpx 单次连接 / 读 / 写操作的等待上限，不是整个请求的总时限；数据持续流动就不会超时，真正停滞 60 秒才抛错。
- 真正的硬上限：浏览器端 60 秒总超时；nginx（`deploy/docker/Dockerfile.frontend:19-33`）`client_max_body_size 100M`、`proxy_read_timeout 300`。
- 前后端都没有对话附件大小上限。
- 重试：ledger 行状态仍为 `uploading` 时，同一 `attachment_id` 再次上传返回 409"草稿附件正在变更"（`composer_draft_attachment_service.py:106`）。浏览器超时放弃后后端仍在写，此时重试必然 409。
- 迟到写入者：最终把状态置为 `ready` 的更新按 `generation` 守卫；守卫失败时无条件把整个附件目录排入清理（`composer_draft_attachment_service.py:190-199`）。现在只有删除会推进 `generation`，所以这是对的；引入接管后必须改。
- 沙箱获取按用户加 `asyncio.Lock`（`sandbox_service.py:216-222`），同一用户不会并发创建容器。

## 3. 设计

### 3.1 进度采集（后端）

- 用 `io.BytesIO` 子类包住文件内容，重写 `read()`，每次被 httpx 读走一块就回调当前偏移。传给 `sandbox.files.write_file`（或测试替身的 `files.write`），SDK 与 httpx 不改。
- 回调节流：每秒最多写库一次，传完后再写一次最终值。写库用独立的 `with SessionLocal() as db` 单条 `UPDATE`，按 `attachment_id + state='uploading' + generation` 守卫，不命中就忽略（已被删除或接管）。
- 新列 `composer_draft_attachments.synced_bytes INTEGER`（可空，默认 0），通过 `database.py` 的 `_PENDING_COLUMNS` 启动补列。

### 3.2 心跳与陈旧判定（后端）

- 进度写库只在字节流动时发生，沙箱恢复 / 重建阶段没有进度。为了让"写入者是否还活着"的判定不依赖字节流动，第二跳全程（从 `_ensure_sandbox` 到最终状态更新）跑一个心跳任务，每 10 秒用独立 DB 会话刷新该行的 `updated_at`（同样按 state + generation 守卫），结束时取消。
- 陈旧判定：`state == 'uploading'` 且 `now - updated_at > 30s` 视为写入者已死（进程重启、崩溃）。阈值 30 秒 = 3 个心跳周期。
- 常量：`_SYNC_HEARTBEAT_INTERVAL = 10s`，`_SYNC_STALE_AFTER = 30s`，`_SYNC_PROGRESS_FLUSH_INTERVAL = 1s`。

### 3.3 临时文件 + 原子改名（后端）

- 写入目标改为同目录下的固定短名 `.g{generation}.part`（目录按附件独占，不在原文件名后加后缀以免超出 255 字节文件名上限）；沙箱内校验（`wc -c` + `sha256sum`）通过后 `mv -f` 到正式文件名 `{name}`，再做守卫更新置 `ready`。
- 两个写入者（接管后原写入者又活过来）各写各的 part 文件，内容由 sha256 保证一致，谁后 `mv` 都正确。
- 写入失败路径：不删除 part 文件，随草稿根目录在删除 / 24 小时到期时一并清理（见末尾差异 4）。

### 3.4 重试语义：附着或接管（后端）

`POST` 时 ledger 已存在、同一 `attachment_id` 且内容 sha256 与文件名相同：

| 现有状态 | 行为 |
| --- | --- |
| `ready` / `claimed` / `attached` | 幂等返回（现状） |
| `failed` | 新一代重写（现状） |
| `uploading` 且心跳新鲜 | **附着**：不发起第二次写入，HTTP 200 返回当前行（`status:"uploading"`、`synced_bytes`），由前端轮询等待 |
| `uploading` 且心跳陈旧 | **接管**：`generation + 1`，按新上传处理 |
| 其他（`deleting` 等） | 409（现状） |

内容或文件名不同仍返回 409"attachment_id 已对应另一份文件"（现状）。

### 3.5 迟到写入者的清理修正（后端）

最终守卫更新（置 `ready`）和失败守卫更新（置 `failed`）不命中时：

- 行已删除（`deleted_at` 非空）→ 把附件目录排入清理（现状）。
- 行未删除（被接管，新一代仍在写或已就绪）→ 不清理、不抛 409，直接返回当前行。否则会把接管者刚写好的文件删掉。

### 3.6 状态查询接口（后端）

- `GET /api/composer-drafts/{draft_id}/attachments/{attachment_id}`
- 响应同上传接口：`{attachment_id, draft_id, name, size, type, sha256, status, synced_bytes}`。
- `status` 映射：`ready/claimed/attached → "ready"`；`uploading` 且心跳新鲜 → `"uploading"`；`uploading` 且陈旧 → `"failed"`（服务端用自己的时钟判定，前端不猜）；其余原样。
- 行不存在、不属于当前用户或 `draft_id` 不符 → 404。
- 上传接口的响应同步加上 `synced_bytes`。

### 3.7 前端

- 类型：`ComposerAttachment` 新增 `syncProgress?: number`（百分比）。`uploadStatus` 取值不变。
- API 客户端：`uploadDraftAttachment` 该请求 `timeout: 0`（不再按固定时间放弃）；新增 `getDraftAttachmentStatus(draftId, attachmentId, signal)`。
- 上传任务（`ChatV2.tsx` 上传队列）：
  1. 第一跳进度到 100% 进入 `saving` 后，每 1 秒查一次状态接口，写入 `syncProgress = round(synced_bytes / size * 100)`；移除附件（AbortController）或任务结束即停止。
  2. `POST` 返回 `status:"ready"` → 就绪（现状）。
  3. `POST` 返回 `status:"uploading"`（附着）→ 保持 `saving`，继续轮询，直到状态接口返回 `ready`（置就绪）或 `failed`（置失败）。
  4. `POST` 抛错：响应体带后端 `detail`（FastAPI 4xx/5xx）→ 按现状置失败并显示该信息；没有响应（网络断开）或响应无 `detail`（nginx 502/504）→ 查一次状态：`ready` → 就绪；`uploading` → 保持 `saving` 轮询至终态；`failed` → 置失败，提示"同步到沙箱中断，请重试"。
  5. 重试（`handleRetryAttachment`）不变：只对 `error` 状态可用，重新 `POST`；后端按 3.4 决定附着或接管。
- 文案（`ChatInput.tsx:488`）：`saving` 且有 `syncProgress` → "同步到沙箱 42%"；没有数字时仍显示"正在保存"（沙箱准备阶段）。
- 大小上限：`frontend/src/utils/fileUtils.ts` 导出 `MAX_COMPOSER_FILE_BYTES = 200 * 1024 * 1024`；`handleFileUpload`（`ChatV2.tsx:1004-1024`）在图片校验之后过滤超限文件，提示"文件超过 200MB，请通过文件面板上传"（复用现有附件提示，约 3 秒消失），同批其他文件继续上传。

### 3.8 部署

`deploy/docker/Dockerfile.frontend` 与 `cicd/nginx.conf` 的 nginx：`client_max_body_size 205M`（multipart 头部余量）；`proxy_read_timeout` / `proxy_send_timeout` 提到 600（200MB 要求出口 ≥ 0.35MB/s）。生产若不用这份配置，需在实际 nginx 上做同样修改。

## 4. 失败模式

| 场景 | 后端 | 前端表现 |
| --- | --- | --- |
| 第二跳网络持续停滞 | httpx 60 秒写超时 → 置 `failed`、删 part → POST 返回 503 | "上传失败"，可重试 |
| 浏览器 / nginx 断开，后端继续写 | 不受影响，写完置 `ready` | 查状态后继续显示进度，写完变就绪；无需重传 |
| 后端进程在第二跳中途重启 | 行停在 `uploading`，心跳停止，30 秒后判陈旧 | 轮询在 30 秒内看到 `failed`，提示重试；重试接管 |
| 陈旧判定后原写入者又活过来 | 两者各写各的 part，守卫更新只让新一代生效；旧写入者不清理目录 | 无感知 |
| 用户在同步中移除附件 | DELETE 推进 generation 并排入清理（现状）；写入者守卫失败 → 行已删除 → 清理 | 卡片消失 |
| 文件超过 200MB | 不会到达后端 | 选文件时即提示 |
| 同一文件第二次选择（内容相同） | 幂等返回 `ready`（现状） | 直接就绪 |

## 5. 改动清单

| 层 | 文件 | 改动 |
| --- | --- | --- |
| 模型 | `src/api/models/composer_draft_attachment.py` | `synced_bytes` 列 |
| 模型 | `src/api/models/database.py` | `_PENDING_COLUMNS` 新增 `("composer_draft_attachments", "synced_bytes", "INTEGER")` |
| 服务 | `src/api/services/composer_draft_attachment_service.py` | 进度文件对象、节流写库、心跳任务、part + mv、附着 / 接管、迟到写入者清理修正、失败删 part、`status()`、`_serialize` 加 `synced_bytes` 与陈旧映射 |
| 路由 | `src/api/routes/composer_drafts.py` | `GET .../attachments/{attachment_id}` |
| 前端 | `frontend/src/types/index.ts` | `syncProgress` |
| 前端 | `frontend/src/services/api.ts` | 上传 `timeout: 0`；`getDraftAttachmentStatus` |
| 前端 | `frontend/src/components/ChatV2.tsx` | 轮询、附着、断连处理、200MB 拦截 |
| 前端 | `frontend/src/components/ChatInput.tsx` | 同步进度文案 |
| 前端 | `frontend/src/utils/fileUtils.ts` | `MAX_COMPOSER_FILE_BYTES` |
| 部署 | `deploy/docker/Dockerfile.frontend` | nginx 上限与超时 |

## 6. 测试

后端 `tests/test_composer_draft_attachments.py`（沿用 `db`、`upload_sandboxes` 夹具，`files.write` 替身改为读取传入的文件对象）：

1. 写入期间 `synced_bytes` 被写库，完成时等于 `size`；写入目标是 `.part`，命令序列为 mkdir → 校验 → mv。
2. 重试命中 `uploading` 且心跳新鲜：返回 `status:"uploading"` 与 `synced_bytes`，不触碰沙箱。
3. 重试命中 `uploading` 且心跳陈旧：`generation + 1`，正常写入并置 `ready`。
4. 迟到写入者守卫失败：行未删除 → 不入队清理、不抛错；行已删除 → 入队清理。
5. `GET` 状态：本人返回字段；他人或不存在 404；`uploading` + 陈旧 → `"failed"`。
6. 写入失败不删除 part，随目录清理；被接管的迟到写入者不清理目录。

前端 `frontend/src/__tests__/components/ChatV2.test.tsx`（`apiService` mock 增加 `getDraftAttachmentStatus`）：

1. 第一跳到 100% 后轮询并显示"同步到沙箱 42%"，POST 返回后显示"已就绪"且轮询停止。
2. POST 网络断开、状态接口先返回 `uploading` 后返回 `ready` → 卡片最终就绪，不重传。
3. POST 网络断开、状态接口返回 `failed` → 显示"上传失败"，可重试。
4. 超过 200MB 的文件被拒绝并提示，同批其他文件照常上传。

## 7. Spec 同步

- `docs/specs/frontend-chat-spec.md` 第 8 节（第 413 行附近）：`saving` 阶段轮询与文案、断连处理、附着语义、200MB 上限与提示。
- `docs/specs/sessions-spec.md`（第 19-24 行）：上传响应新增 `synced_bytes` 与 `status:"uploading"`；新增 `GET`；附着 / 接管规则；part + mv；迟到写入者清理规则。
- `docs/specs/sandbox-spec.md` §4 "Composer 草稿附件目录"：part 文件命名、心跳与 `synced_bytes`、清理规则修正。

## 8. 实施步骤与验证

每步完成后给出验证结果，再进入下一步。

1. 后端列与查询接口：模型、补列、`_serialize`、`GET`。验证：测试 5。
2. 后端第二跳：进度文件对象、节流写库、心跳任务、part + mv、失败删 part、清理修正。验证：测试 1、4、6。
3. 后端重试语义：附着 / 接管。验证：测试 2、3；全文件 `tests/test_composer_draft_attachments.py` 通过；按引用关系跑 `tests/test_sessions*.py` 等涉及草稿附件 claim 的文件；新进程 `import src.api.main`。
4. 前端：类型、API、轮询、附着、断连、文案、200MB 拦截。验证：前端测试 1-4；`ChatV2`、`ChatInput` 相关测试文件全部通过；`tsc --noEmit`。
5. nginx 与三份 spec。
6. 真实验证（隔离启动后端，关闭 cron / 清理 / 维护任务，用 test 账号，走原 OpenSandbox）：
   - 上传约 100MB 文件，另开线程每秒查状态，记录进度时间线与总耗时。
   - 上传中途主动断开客户端连接，确认后端继续写完、状态接口变 `ready`、再次 POST 幂等返回就绪。
   - 上传中途杀掉后端进程再重启，确认 30 秒内状态变 `failed`，再次 POST 接管并就绪，沙箱内只剩正式文件。
   每次约 100MB 公网出口流量，共约 300MB。
7. 暂存后按 `CLAUDE.local.md` 准则交 Codex 复审，核实结论后汇报。

## 9. 风险与待核实

- OpenSandbox 侧对单次 multipart 上传是否有大小限制未知，第 6 步用 100MB 验证；200MB 若失败需看沙箱侧日志，可能要在后端分块写入再 `cat`。
- 每个进行中的上传多占一个短连接用于心跳 / 进度写库（各自即用即还）；前端并发上限 3，影响可忽略。
- 后端整文件读入内存，httpx 分块发送不复制；200MB × 3 并发约 600MB，生产机可接受，本机实测只跑一个。
- 心跳任务与主流程各用独立 DB 会话，不共享请求级会话。
- Vite 开发代理没有超时限制，开发环境不受 nginx 值影响。

## 10. 规模估计

后端约 120 行 + 测试约 120 行；前端约 110 行 + 测试约 100 行；nginx 与 spec 约 30 行。合计约 450-500 行。

## 11. 实施调整（2026-10-20）

用户确认按本计划推进，并要求三处轻量修正、允许若干简化：

1. 200MB 上限三处对齐：后端 `_MAX_ATTACHMENT_BYTES` 超限返回 413；nginx `client_max_body_size 205M`（multipart 余量）；前端选文件时拦截。
2. 状态查询失败有出口：初始 404 与偶发网络错误连续重试，连续 5 次失败置为上传失败并提示"暂时无法确认，请重试"。
3. 旧请求不覆盖新结果：上传任务回写卡片前检查自己仍是该附件的当前任务（`uploadControllersRef` 身份）；用户重试后旧请求的迟到结果一律忽略。
4. 简化：连接断开（网络、nginx 超时、worker 重启）后不自动续接，上传请求出错即置失败，由用户重试；后端按附着 / 接管规则保证不重传、不 409。写入失败不立即删除 part 文件，随草稿根目录在删除 / 到期时清理（3.3 的 `rm -f` 不做）。
5. 其余（字节级进度、心跳 10 秒 / 陈旧 30 秒、part + `mv`、迟到写入者清理修正、`GET` 状态接口）按计划实施。
5. 复审修正（2026-10-20）：上传请求返回前轮询只显示进度、不定终态，终态只看上传请求；只有返回 `uploading`（附着）后才由轮询决定终态。part 文件改为固定短名；`synced_bytes` 列 `NOT NULL DEFAULT 0`；K8s 镜像的 `cicd/nginx.conf` 同步改为 205M / 600 秒。
