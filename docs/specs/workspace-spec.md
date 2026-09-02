# 用户持久工作区 Spec

## 1. 边界与存储位置

- 每个用户的工作区固定为当前 Sandbox Profile 持久挂载中的 `${SANDBOX_STORAGE_MOUNT_PATH}/workdir`，默认 `/home/user/workdir`。
- Session 仍使用 `sessions/{session_id}`，Cron 仍使用 `cron/runs/{run_id}`；Agent 的执行目录不得改成全局 workdir。
- 工作区是平台固定能力，不设置功能开关；`SANDBOX_PERSISTENT_STORAGE_ENABLED=false` 时返回 `WORKSPACE_PERSISTENCE_DISABLED`，不得把临时容器目录冒充持久工作区。
- 非空工作区禁止任何会调用当前 destructive sandbox kill/清挂载路径的 Profile 切换、force recreate 或 stale-profile rebuild；实现保留持久卷的重建路径前不得绕过。

- 删除是永久操作：没有回收站、撤销删除或旧协议兼容。删除条目同时移除它的历史版本、相关内容引用与提案；聊天历史不为该工作区文件保留快照兜底。其他独立 Session 文件和未删除条目的共享对象不受影响。

## 2. 数据与路径安全

- `user_workspaces` 保存 Profile 指纹、根路径、正式文件与历史对象的独立容量/已用量、条目配额、全局 revision、最近 GC 时间与 `active/draining` 生命周期状态。`draining` 先阻止新 mutation，再等待或收敛已有 claim，之后才能进入 destructive Profile rebuild/switch。
- `workspace_entries` 只保存 active 条目：稳定 ID、父级、名称、相对路径、类型、SHA/size、revision、current_version/head_blob、tree_revision；不再有 trashed 状态与恢复位置字段。
- `workspace_content_objects` 是用户内内容寻址对象库：同一用户同一 SHA-256 只允许一个 active 对象，路径固定为 `.opencapybox/objects/sha256/<前两位>/<sha256>/content`。对象发布必须先完整写入临时文件、复核 SHA/size，再以 no-clobber 原子发布；active 工作文件与对象禁止共享 inode。不同用户不得共享对象或用量。
- `workspace_file_versions` 保存 revision/checkpoint 元数据：`version_id/entry_id/parent_version_id/blob_id/sha256/size/actor/run_id/checkpoint_kind/retained_until/state`。每次真实内容 revision 都有稳定 base，但只有初始创建、导入、AI/Cron 发布、恢复、Round 引用和 Web idle/关闭检查点进入普通历史列表；无内容变化不得新建 revision。恢复旧内容必须创建新的 head，不能回写历史事实。
- `current_version_id/head_blob_id -> workspace_content_objects` 是自动合并的 current 内容真相；用户命名的 active 文件只是该 head 的可编辑物化副本，合并器不得从 active 路径流式读取 current。每次合并先比对 entry、head 对象与物化文件的 SHA/size：物化文件命中旧版本时从 head 原子修复；命中任何历史均不存在的新内容时，先以 `web` actor 和 file claim 静默吸收为新 head，再基于该版本重新合并。
- `workspace_content_references` 是内容保护真相，显式记录 `entry_head`、`round_attachment`、`change_set_base/proposal`、`checkpoint_pin` 等 active 引用；`pinned` 仅为兼容投影，GC 不得只依赖布尔值推断引用。
- `workspace_claims` 在文件系统副作用前冻结 file/tree/path 范围、owner token 与 generation，并以 heartbeat 续租。文件或目录删除与同范围写入/移动互斥；不再有全局回收站 namespace claim。
- `workspace_change_sets` 保存 Chat/Cron 基于某一 `base_version_id` 产生的冻结修改、自动合并结果与来源上下文。它是审计/恢复记录，不是待用户处理的队列；自动 apply 先以独立 claim 和 owner generation 把行 CAS 为 `applying`，reject 只接受尚未进入 `applying` 的状态，所有终态写回再次校验 owner，禁止 reject 后继续落盘。可重试并发进入内部 `conflict` 后由 maintenance 继续收敛，不可继续的内容/版本损坏进入终态 `failed` 并保留 proposal 与错误信息，禁止永久悬挂或要求普通用户决策。
- 升级前遗留或准备中断的 change-set proposal 由 maintenance 幂等迁移，包括已经 `applied/rejected/failed` 的终态记录且不得改变原 status：先复核临时文件 SHA/size，再发布内容对象，并把 `proposal_blob_id + proposal_path + change_set_proposal` 引用同事务提交；终态引用同时进入既有 retention 语义，只有该提交成功后才删除原临时文件。删除失败保留 `proposal_temp_path`，maintenance 即使面对仅有终态 legacy 行也必须再次选中该用户并重试，成功删除后才清除此字段。
- `workspace_mutations` 保留不含正文的 append-only journal。删除前取得 claims 并提交 prepared，记录冻结的根/后代 IDs、路径、revision、容量差额和幂等请求摘要；物理删除成功后一次 DB finalization 删除条目/版本/引用，结算容量并转 completed/DELETED。
- 启动、首次写入和 maintenance 对过期 prepared 对账。普通写入/移动按旧/新 hash 和路径判断；delete_many 已经确认删除意图，接管全部 claims 后幂等删除剩余物理根与平台快照，再完成 DB finalization。删除不可回滚为用户可恢复文件，也不重放模型任务。
- 所有用户路径只接受相对 POSIX 路径；拒绝绝对路径、`..`、反斜杠、NUL、空组件和 `.opencapybox` 系统目录。沙箱内通过逐段 `O_NOFOLLOW`/dirfd 操作拒绝符号链接逃逸。
- 持久工作区目录最多两层；创建目录以及移动含子目录的目录树都必须在服务边界校验最终深度，超限返回 `DIRECTORY_DEPTH_LIMIT`。文件可创建、上传或移动到第二层目录。
- 写入使用同文件系统 temp + `fsync` + `os.replace`；Session/Workspace/Cron 间复制在沙箱内分块完成。HTTP 上传按 4 MiB 有界块写入沙箱 temp parts，不在 API 进程聚合整个文件。
- 目录枚举和元数据搜索只读数据库，不恢复 Sandbox。内容读取和 mutation 优先复用 ID/Profile 匹配且在短 TTL 内确认健康的缓存实例；TTL 到期只做一次轻量健康检查，只有不健康或绑定变化才进入串行生命周期恢复。不得让每个文件操作都排队等待 Sandbox 生命周期锁。
- claim 的领取、续租、最终确认均为短数据库事务；Sandbox I/O 期间不得持有请求级事务或行锁。长复制按固定周期续租。每个物理 scope 在 `.opencapybox/mutation-fences/` 保留按 scope hash 命名的 `flock + owner generation`；最终 replace/rename/unlink 所在的同一沙箱脚本必须在持锁期间验证该 generation。lease 接管先在 DB 推进 owner，再在执行任何对账/重放前取得相同 scope locks 并把文件系统 fence 推进到新 generation；旧 owner 晚到时只能收到 `MUTATION_FENCED`。DB finalize 仍再次验证 owner token + generation。
- 目录移动/删除先取得 tree claim 并冻结全部后代；其间不能新建、写入或移动后代。目录删除与单文件删除共用 delete_entries_batch。

## 3. REST API

所有端点位于 `/api/workspace`，只访问当前认证用户：

- `GET /entries`：只枚举 active 条目，支持目录懒加载、搜索与 cursor 分页，不提供 include_trashed。
- `GET /entries/{entry_id}`：按当前认证用户的稳定 ID 读取 active 元数据；已删除或不存在返回 404。
- `GET /entries/{entry_id}/versions`、`GET /versions/{version_id}/content`：读取当前用户可见的版本历史和固定版本内容；固定版本只通过 version 内容端点读取，entry 内容端点不再接受 `version_id`。删除 entry 时其版本一并移除。固定版本同样支持 `preview=true&render=pdf`，供统一右侧工作台只读预览 Office 历史内容。
- `POST /entries/{entry_id}/checkpoint`：把指定 entry revision/current_version 原子提升为 Web 检查点；重复提交同一 current version 幂等，不重新复制内容。
- `POST /entries/{entry_id}/restore`：将指定历史版本内容发布为一个新的当前版本，保留 `restored_from_version_id` 审计。
- `POST /directories`、`POST /files`、`POST /uploads`：新建文件夹、Markdown/XLSX 与流式上传；同名返回 409，不自动改名。
- `GET /entries/{entry_id}/content`：只下载/预览当前 head，响应带 revision ETag、`X-Workspace-Revision` 与 `X-Workspace-Version`；固定历史版本统一通过 `GET /versions/{version_id}/content` 读取。`preview=true&render=pdf` 从同一不可变 head 对象派生 DOC/DOCX/PPT/PPTX PDF；`render` 未配 `preview=true` 返回 400。
- `GET /content?path=<relative>&preview=true`：仅按当前用户 active metadata 解析 Markdown 相对资源；可同样使用 `render=pdf`。
- `PUT /entries/{entry_id}/content`：原始字节正文，必须携带 `If-Match`，并在已有内容版本时携带 `X-Workspace-Base-Version`；仅 MD/Markdown/TXT/CSV/XLSX 可在线写回。CSV 必须是无 NUL 的有效 UTF-8；XLSX 与 Session 编辑共用同一套有界 OOXML 校验，覆盖 ZIP 条目路径、重复/加密条目、CRC、总解压大小、必要 XML、Content Types、根 `officeDocument`、workbook sheet 清单和 worksheet target。校验失败在 mutation/物理副作用前返回 422 `INVALID_CSV` / `INVALID_XLSX`。base 落后时服务端自动三方合并并以 `auto_merged=true` 回执。
- `PATCH /entries/{entry_id}`：使用 expected revision 移动/重命名。
- `POST /entries/delete-batch`：统一处理单项与批量永久删除，使用 `{items:[{entry_id,expected_revision}],idempotency_key}`，最多 200 个选择项，祖先覆盖已选后代。成功返回 `{status:"DELETED",mutation_id,affected_entry_ids,root_count,entry_count}`，不返回可恢复条目。
- `POST /imports/session-file`：服务端复核 Session 所属用户与 opaque source revision，再在沙箱内复制稳定版本。

Entry response 固定为 `{entry_id,parent_id,name,kind,path,size_bytes,mime_type,sha256,revision,current_version_id,tree_revision,status,created_at,updated_at}`；mutation response 另含 `auto_merged`。不可自动收敛的结构/所有权错误仍使用稳定 code 并附 authoritative `entry/current_version_id`；源 Session 变化返回 412 与 `current_revision`。

## 4. 内部服务契约

`WorkspaceService(db, sandbox_service=None)` 负责读取与文件操作，`WorkspaceMutationCoordinator` 负责 claim、change set、journal、版本发布和恢复；Web/Chat/Cron 共用同一个 mutation coordinator：

- `list_entries` / `get_entry` / `get_entry_by_path`
- `create_directory` / `create_file` / `upload_file_stream` / `write_content`
- `move_entry` / `delete_entry` / `delete_entries_batch`
- `delete_entries_batch(items,idempotency_key,...)`：按用户、稳定 ID 和 expected revision 验证全范围，归并父子选择，取得 file/tree claims；远端单脚本 no-follow 预检全部根后原地 unlink/rmdir，并清理对应旧 read 快照和 Session/Cron 的按 entry_id 命名平台快照。已不存在的物理路径只在该已认领 journal 重试中幂等跳过。幂等响应只来自冻结 journal，条目已删除或同名重建不改变原结果。
- `stage_entry(entry_id, expected_revision/version_id/tree_revision, destination_root, ...)`：文件冻结为指定不可变版本或调用时 current head，并分别返回 capture 时 entry revision、`version_id` 与 `version_sequence`；不能用当前 entry revision冒充历史正文的版本序号。聊天普通附件省略所有观察 revision，工具显式携带的观察 revision 过期时自动以 current head 重取一次。文件夹冻结调用时完整 descendant manifest，在最终目标同父目录的唯一 `.incoming-*` 中构建并完成 SHA/manifest 复核与原子 no-clobber rename；失败清理 incoming 且不占最终名称。
- `publish_sandbox_file(...)`：从受控 Session/Cron 执行根生成冻结 change set，并自动发布。覆盖前先在 file claim 下收敛物化文件与内部 head，current 固定从不可变 head 对象读取；base 落后时对 Markdown/TXT、CSV、XLSX 做三方合并，同一行/同一单元格由当前正式内容（人的版本）胜出，不同位置的 AI 修改合入。创建目标只校验同名路径仍不存在，不进入三方合并；无法可靠拆分的形状保持当前正式内容，冻结提案仍留在审计记录中。同一 Agent Round 已对 entry 发起 publish 后禁止 move/delete；同一 Round 已 move/delete 腾出的原路径禁止再 publish 同名新 entry，彻底封死“移动/删除后重建”绕过覆盖的路径。
- `import_session_file(...)`：显式 Session 文件保存到工作区。
- 自动发布直接使用已校验的不可变 proposal 内容对象，不在 Session/Cron 的 `.workspace-change-sets` 下复制平台副本；取消恢复仍依赖现有 change set/reference/journal。升级前的此类副本应按已完成 mutation 的来源路径及摘要一次性核验清理，不删除用户独立源文件。
- `restore_version(...)`：把旧版本内容作为新的 head 发布，不修改旧版本。
- change set 只作为内部审计和 maintenance 自动恢复状态，不提供普通用户 REST 列表、内容下载、apply 或 reject 接口。

Chat/Cron 调用方负责按 `none/read/edit/manage` 决定是否暴露相应工具；核心服务仍统一做用户边界、路径、revision、配额、幂等和 mutation audit。

## 5. 配额、读取与失败语义

- `WORKSPACE_QUOTA_BYTES`、`WORKSPACE_HISTORY_QUOTA_BYTES`、`WORKSPACE_PREVIEW_CACHE_BYTES`、`WORKSPACE_MAX_FILE_BYTES`、`WORKSPACE_MAX_ENTRIES` 必须大于 0，且单文件上限不超过正式文件配额。正式文件、用户内唯一历史对象和可删除派生缓存分别计量；不得让版本对象或 PDF 缓存绕过所有容量边界。
- OpenSandbox 内容读取固定使用 `read_bytes_stream` 流式响应；流建立失败必须 fail closed，不得静默降级为 API 进程内的全量读取。
- 普通预览/下载直接流式读取已校验归属的不可变 head/version 对象，正文、大小和版本身份来自同一版本；不再把可变 active 文件或共享 `.opencapybox/read/{entry}/content` 当作请求快照，不增加整文件复制或全量 SHA。Session/Cron 跨目录 stage 仍按原协议分块复制和校验。
- 旧 `.opencapybox/read` 快照及 .snapshot.lock 随直接删除清理；普通读取不再创建它们。
- 普通读取不吸收或修复 active 物化文件的服务外修改；仍由既有写入/发布入口执行物理 head 收敛。指定版本不可用时明确失败，不能返回另一版本正文并沿用旧编辑基线。
- 同一幂等键只产生一个 mutation/目标文件。普通 create/upload/write/move/restore/import mutation 只按 `user_id + idempotency_key + operation` 识别重试：`prepared` 返回带 mutation 身份的 `MUTATION_IN_PROGRESS`，`completed` 返回原 journal 中冻结的 entry/revision/version/auto_merged，`failed` 重放原始 status/code/message 且不得伪装成仍在处理；复用于其他 operation 返回 `IDEMPOTENCY_KEY_REUSED`。调用方必须为每个新操作意图和每一代新正文生成新 key，只能在原请求参数与正文均未改变且结果仍未确定的网络重试中复用旧 key。批量删除和 change set 另有请求指纹校验，同 key 携带不同冻结范围或提案时返回 `IDEMPOTENCY_KEY_REUSED`；批量删除的幂等响应不依赖当前 entry 是否已删除或发生同名重建。
- 单个文件 mutation 原子；一次 Agent/Cron 任务的多个成功 mutation 不因后续步骤失败而回滚。
- 工作区不设“同一时间只允许一个 mutation”的全局锁；不重叠 scope 可并行。相同 file/path 或祖先/后代 tree scope 在文件系统副作用前由 claim 串行化，容量校验计入所有 active prepared mutation 的正向预留。
- Web 保存同时携带 entry revision 与编辑开始时的 base version；不存在或属于其他 entry 的 base 必须稳定拒绝，只有同 entry 的已知 version 明确进入 `pruned` 后才允许按人类草稿优先降级。Chat/Cron publish 携带 base version。base 不一致时统一走格式感知三方合并，不采用整文件“最后写入者胜出”：Web 草稿与 AI 正式内容冲突时人的草稿胜出，AI 提案与人的正式内容冲突时人的正式内容胜出。
- 自动合并读取失败不得伪装成 `applied/current_wins`：内部 head/proposal 缺失、摘要不符或 Sandbox 流建立失败进入 `failed` 终态并保留两边内容；只在明确的并发 revision/claim 变化时进入后台可重试状态。`CancelledError`/worker 丢失保留可恢复 journal，由 maintenance 接续，不得把取消误判为内容损坏。
- claim lease 不能只写一次固定 deadline：活动 owner 必须 heartbeat；reconciler 只接管 lease 与 heartbeat 均过期的 claim。执行过副作用但所有权不明的操作进入 `unknown/reconciliation_conflict`，不自动重放。
- `_prepare` 不得在 Sandbox 网络 I/O 期间持有 `user_workspaces FOR UPDATE` 或占用活动 DB 事务；配额只在写入 prepared journal 前用短事务预留，文件系统 I/O 在该提交之后并行执行。`ensure_root` 只用于首次/空工作区初始化，不在每次预览、移动或写入前重复运行。
- Session 文件列表、预览、上传等前端请求也必须在 Sandbox 网络 I/O 前归还请求级 DB 连接；Agent/Cron 运行锁存在时，这些被动请求只能连接当前持久绑定，禁止创建新 Sandbox 或改写用户绑定。Agent/Cron 工作区工具始终使用该次运行启动时取得的 Sandbox 实例，不跟随前端恢复切换容器代际。
- 新建空 Markdown 直接在沙箱内用一次 no-follow 原子脚本创建，不上传 0 字节临时文件；普通写入成功后 temp 已被 `os.replace` 消耗，不再额外发送无效清理命令，只有安装失败才清理残留。
- 物理根/平台快照删除成功后才在同一 finalization 扣减 used_bytes/entry_count，保证只扣一次。删除同时移除该条目的全部版本/引用和相关提案，零引用对象立即尝试 GC；同次删除的对象批量取得 claims、查询共享引用，以一个批量沙箱脚本清理对象及空目录，再以一个批量脚本删除这些内容 SHA 派生的全部 Office PDF 缓存，最后一次结算历史容量，不逐对象往返沙箱。清理中断由已有对象 GC 重试；共享对象及其缓存必须复核其他条目/版本/提案引用后才删除。
- Web outbox 的每次成功保存仍推进 revision/current version，保证 CAS 与三方合并 base 精确；连续 autosave 不自动成为用户历史检查点。前端在停止编辑 30 秒、关闭标签/面板或切换 owner 时提交 checkpoint；持续编辑最长每 5 分钟提交一次 checkpoint。当前 head 即使尚未 checkpoint 也必须持久且受 `entry_head` 引用保护。
- 历史 GC 永远保护 current head、active content reference、进行中的 mutation/change set，以及最近 `WORKSPACE_VERSION_RETENTION_COUNT` 个检查点；同时保留 `WORKSPACE_VERSION_RETENTION_DAYS` 天内的检查点。未 checkpoint 的 autosave revision 只保留最近 `WORKSPACE_DRAFT_REVISION_RETENTION_COUNT` 个且不超过 `WORKSPACE_DRAFT_BASE_RETENTION_DAYS`，更老的 base 缺失时由人类草稿优先的合并降级保证不丢稿。超过独立历史软配额时只淘汰保护集合之外的最老内容，不得阻断正式文件保存；保护内容本身超配额时记录诊断并等待引用释放。
- GC 使用 `materialized -> pruning -> pruned` 两阶段状态和 workspace claim：先冻结候选并提交，再删除零引用对象，最后清空 version 的 `blob_id/content_path` 并保留 SHA、大小、actor、时间等审计。进程中断后 reconciler 按 DB 状态与对象存在性继续收敛，禁止先删 DB 后盲删文件。
- 删除 entry 不受 Round/checkpoint 引用阻止：该条目的版本及引用一并删除，历史卡片/附件不再投影它，也不能按旧 version_id 读取或恢复。保留的 mutation journal 只有路径、ID、SHA、大小等审计数据，不保留文件正文。
- Office PDF 是按 `源 SHA + 扩展名 + renderer version` 命中的派生缓存，不属于正式文件或历史对象。Workspace 不可变 head/version 使用数据库中已校验的 SHA/size 在读取源 Office 前查缓存；命中时不得再次复制、哈希源文件或调用 LibreOffice，未命中才读取一次并复核 SHA/size。每次命中/发布更新访问时间；超过 `WORKSPACE_PREVIEW_CACHE_BYTES` 时在缓存锁外按 LRU 删除最旧完整 cache directory，绝不删除 `.incoming-*`、活动 lock 或当前返回项。

## 6. 验收重点

- 路径遍历、双重分隔、symlink/TOCTOU、跨用户 entry/path 猜测均 fail closed。
- 两个 Web/Chat/Cron writer 同改一文件时由 file claim 串行发布；后到者必须基于冻结 base 与最新 current 重新合并，不能直接覆盖。
- active 中文文件路径即使无法通过 OpenSandbox stream 读取，只要实体 SHA/size 与内部 head 一致，自动合并仍必须从内部对象完成；不得通过 delete/重建文件绕过。
- 实体文件命中旧 head 时自动恢复当前内部 head；实体文件出现未入库新内容时先生成一版 `web` head，再合并 AI 非重叠修改。两条路径结束后 DB SHA、head 对象 SHA 与实体文件 SHA 必须一致。
- 两个 writer 使用同一 base version 时允许依次返回成功，但第二个成功必须是三方合并后的新版本，不能出现两个未经合并的同 revision PUT 都返回 200。每次回执后 DB 当前版本、元数据 hash 与物理内容必须一致。
- 父目录 move/delete 与后代 create/write/move 并发时只能一侧取得重叠 scope claim；不得出现物理文件已随目录移动而 DB 仍记录旧路径。
- 两个 writer 同时改不同条目时都能完成，不返回工作区级 `MUTATION_IN_PROGRESS`；并发 finalizer 的容量和全局 revision 增量必须合并。
- 覆盖、移动、历史版本发布及直接删除在物理成功而 DB 提交失败后由 prepared journal 收敛。
- 批量直接删除在第 N 个根删除后中断，必须继续持有 durable claims，接管后只完成冻结范围；DB 一次删除所有后代及历史，容量只扣一次。后来的同名新 entry 不属于旧 journal。
- Session 上传和编辑响应提供 `v1:<size>:<mtime_ns>` opaque revision；导入期间源变化返回 412。
- pause、服务重启和保留持久卷的 sandbox 恢复后文件、entry_id 与 revision 不变。
- 工作区根级文件与会话卡片使用同一内容左边距；只有目录保留展开箭头占位，普通文件不得用空白箭头把名称整体向右推。
- 左侧切到会话列表再返回工作区时，已打开标签、entry 深链、目录展开状态和聊天滚动位置保持不变；返回动作同时刷新根目录、当前展开目录和有效搜索结果，不能继续展示隐藏期间的旧实体投影。选择具体 Session 或显式关闭才切换 owner。
- Markdown/CSV/XLSX 每次真实编辑立即把完整草稿交给当前页面生命周期内的应用级内存 outbox；关闭标签、面板或切换 owner 时先同步触发一次内容抓取，然后立即完成用户动作，远端保存由 outbox 串行执行。网络歧义、5xx 和 `MUTATION_IN_PROGRESS` 只使用原 key 指数退避；确定性 4xx 或幂等 `failed` 立即停止重试、丢弃本地 Workspace 草稿、重新读取服务端 current head，并在该文件内持续提示“修改未保存，已恢复最近保存版本”。页面刷新、崩溃或关闭允许丢失尚未远端成功的 Workspace 草稿；不得自动换 key、强制覆盖或显示全局同步警告。
- 聊天流、Round 状态或其他父级状态重渲染时，已打开的工作区预览不得重新请求或重新初始化；只有文件身份、内容版本或 owner 变化才能触发内容重载。同一 `current_version_id` 的重命名/移动只更新标签和路径，不丢草稿、不误报内容冲突；但首次内容尚未 ready 时必须按新路径继续加载，不得卡在空白预览。
- AI/Cron 更新当前已打开的 clean 文件时，只有已存在 ready 内容才进入后台刷新，编辑器在新内容 ready 后原子切换正文与 version；首次加载期间的 revision 变化继续使用稳定 loading，不得提前显示空内容或类型降级卡。PPTX 的 ready 以 PDF.js 成功打开文档并读取第一页为准，刷新失败继续保留旧 deck；旧自动保存或预览响应按 content generation/request id 丢弃。dirty 文件继续保留本地草稿与原 base version并自动保存；服务端把 AI 的非重叠修改合入，重叠位置保留人的草稿，成功后再原位刷新合并版本。
- 普通用户界面不显示 change set、冲突队列、“AI 修改待确认”、查看提案、发布、拒绝、加载新版本或放弃草稿动作；审计记录只供系统恢复和排障。
- 普通 Chat wire 不发送 `workspace_change_proposed/workspace_change_conflict` 决策事件；未完成状态只通过工具内部文本告知 Agent “后台重试、不要删除或重建”，成功正式 mutation 才发送 `workspace_resource_changed`。
- 助手工作区文件卡只按原 entry_id 打开；404 时显示已删除，不读历史副本、不恢复、不按同名新条目替代。
- Workspace-origin 历史附件在源 entry 存在时保持固定版本；显式删除该 entry 后移除附件投影及该 entry 的平台快照，不保留历史兜底。
- 相同内容连续保存、不同 entry 复制相同文件或恢复到已有 SHA 时，用户内只增加 version/reference 元数据，不增加第二份物理对象或 `history_used_bytes`；不同用户仍各自保存。
- 取消发生在对象发布、版本 finalization 或 GC 删除任一屏障时，重试/reconciler 后必须收敛为一个对象、一个 head 和可解释的 version 状态，不得遗留无 DB 所属对象或已删内容仍标 `materialized`。
- Chat/Cron 在各自 execution root 产生冻结修改；base 未变化时直接发布，包括 CSV 新增行和 XLSX 新增工作表，不能因三方合并器不支持结构变化而丢弃提案内容；base 已变化时自动三方合并并保留人的重叠内容。Cron 不重放整个任务，只允许统一发布入口对冻结提案收敛一次。
- 本期桌面端完成上述自动合并、持久草稿与永久删除确认；移动端交互与层级问题另行设计，不作为本期验收范围。

- 删除确认必须明确说明不可恢复及未保存草稿也将丢弃；不先保存将被删除的内容。服务端成功后统一 tombstone 进入 Chat runtime reducer，按 `affected_entry_ids` 清理所有 Session/Round 的 Workspace 附件与助手引用，并关闭后代标签、清理目录/搜索/entry-only 深链、本地 outbox、按 entry/version 建立的预览缓存以及对应 `.workspace-snapshots/<entry>/` 的文件工作台目录/标签，屏蔽迟到 history、保存和刷新事件；失败时保留原草稿与选择。
- 项目验收固定四个独立 Terra：真实浏览器高频操作、只读数据库事实、物理文件及真实格式解析、独立只读真实 Sandbox 扫描。
