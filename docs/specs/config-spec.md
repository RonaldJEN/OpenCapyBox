# Agent 配置与技能 (Config) — Spec

## 1. 模块职责边界

- Agent 记忆文件读写接口（对前端暴露）
- Skills 发现、启停管理
- Skill 渐进式加载（Progressive Disclosure）
- Agent 工具注册与装配（tool_factory）
- **不负责**：记忆内容语义理解、Skill 执行逻辑

## 2. 数据模型

复用 memory-spec 中的 `user_memory`、`user_skill_configs` 表，并使用 `user_skill_inventory_snapshots` 保存用户 Skill 最近一次完整扫描快照。

`user_skill_inventory_snapshots` 每用户至多一行，保存 sandbox/Profile 代际、仅含元数据的 `inventory_json`、revision 与扫描开始时间。无行表示从未成功完整扫描，`[]` 表示完整扫描成功且确实没有用户 Skill；Skill 正文和启停状态不得写入该快照。沙箱文件仍是用户 Skill 的事实源，DB 只服务于快速清单读取。

### Skill 元数据（文件系统）

- 来源：`src/agent/skills/` 目录下各子目录
- 每个 Skill 目录包含 `SKILL.md`（frontmatter 至少包含 `name`、`description`；其余字段可选）
- 用户自定义 Skill：沙箱中 `/home/user/skills/` 目录
- `name` 规范化后即稳定内部 `key`：先 trim，结果必须非空且不超过 128 个 Unicode 字符；为兼容现有官方 Skill，可包含人类可读 Unicode、空格和括号，但禁止 `/`、`\`、`?`、`#`、`%`，并禁止 Unicode General Category 为 `C*` 的控制、格式化/不可见、代理、私用或未分配字符。`user_skill_configs.skill_name` DB 列长度为 128，与 API/运行时上限一致。
- 展示名按以下优先级解析：frontmatter 顶层 `display_name` / `display-name` → `metadata` 对象内同名字段 → `name`。空白或缺失时回退 `name`；展示名不得用作 Skill key。

### 工具候选表（tool_factory）

| 工具类 | 名称 | 用途 |
|---|---|---|
| SandboxReadTool | read_file | 读沙箱文件（带 token 截断）|
| SandboxWriteTool | write_file | 写/创建沙箱文件 |
| SandboxEditTool | edit_file | 字符串替换编辑 |
| SandboxBashTool | bash | 执行命令（前台/后台）|
| SandboxBashOutputTool | bash_output | 获取后台进程输出 |
| SandboxBashKillTool | bash_kill | 终止后台进程 |
| SandboxSessionNoteTool | record_note | 会话内笔记 |
| RecallNoteTool | recall_note | 回忆笔记 |
| RecordDailyLog | record_memory | 记录日常记忆 |
| UpdateLongTermMemory | update_long_term_memory | 更新长期记忆 |
| SearchMemory | search_memory | 搜索记忆 |
| ReadUserProfile | read_user_profile | 读用户画像 |
| UpdateUserProfile | update_user | 更新用户画像 |
| ManageCron | manage_cron | 管理定时任务 |
| AskUserQuestion | ask_user | 人机交互中断 |
| SubAgentTool | sub_agent | 子 Agent 委托（创建 child Round + graph edge，父 Agent 等待结果） |
| GetSkillTool | get_skill | 渐进式加载 L2 |
| GLMSearchTool | glm_search | 搜索（条件加载）|
| GLMBatchSearchTool | glm_batch_search | 批量搜索（条件加载）|

## 3. API 契约

（与 memory-spec 共享端点，此处聚焦技能与工具装配）

### GET /api/config/skills

- **Query**: `refresh=true` 可显式要求连接/恢复沙箱并严格重扫；缺省为 `false`。
- **Response 200**: `{skills: [{key, name, display_name, description, category, source, enabled}], sandbox_status, inventory_state, inventory_discovered_at}`
- `key`: Skill 的稳定内部标识；聊天接口 `preferred_skill_keys`、Skill 启停和运行时 `get_skill` 均以该标识对齐。当前兼容实现中与 `name` 相同。
- `name`: Skill 元数据中的内部名称，保留供既有客户端兼容使用。
- `display_name`: 面向用户展示的名称；未配置时回退为 `name`。前端不得把该展示值作为请求 key。
- `source`: `official`（平台文件系统）或 `user`（用户沙箱）
- `sandbox_status`: `not_created` / `available` / `unavailable`
  - `not_created`: 用户尚无可连接的持久化沙箱，仅返回官方 Skills
  - `available`: 当前 sandbox/Profile 代际已有完整可用清单（来自本次严格扫描或匹配的 DB 快照），返回官方 Skills 与用户 Skills；命中快照时不代表本次做过实时探活
  - `unavailable`: 已有沙箱记录但本次连接、恢复或发现失败；接口仍返回 200 和官方 Skills，若当前代际仍有完整旧快照可同时返回用户 Skills 并标记 `inventory_state=stale`
- `inventory_state`: `current`（当前代际完整快照或刚发布扫描）/ `stale`（本次刷新失败但安全复用当前代际旧快照）/ `unavailable`（无可安全使用的用户清单）
- `inventory_discovered_at`: 当前返回快照对应的完整扫描开始时间；无用户清单时为 `null`
- 合并：SkillLoader 文件系统发现 + UserSkillConfig DB 状态
- 包含沙箱中用户自定义 Skill
- 沙箱发现采用部分成功语义：沙箱不可用不应阻断官方 Skills 清单
- sandbox/Profile 代际匹配且快照 JSON 完整时，缺省请求直接合并 DB 快照，不调用远程健康检查、恢复或扫描；损坏或代际不匹配的快照按 cache miss 处理，禁止跨 sandbox/Profile 泄露旧清单。
- 缺少可用快照或 `refresh=true` 时才以严格模式发现用户 Skills：若同 ID、同 Profile 的进程内缓存仍可直接完成严格扫描，则跳过冗余远程健康检查；无兼容缓存或直接扫描失败时，先调用 `recover_persisted_sandbox` 再重试扫描：
  - 只在控制面确认旧沙箱终止/失败/不存在，或 Profile 明确不匹配时允许候选重建并以 CAS 更新绑定；
  - 状态查询、connect、resume 或 Profile 指纹确认发生暂时性失败时不得重建，返回 `sandbox_status=unavailable`；
  - 远程恢复/发现前必须结束本地 DB 事务，完成远程 I/O 后再读取最新 `UserSkillConfig`，避免长事务占用连接并防止旧配置快照覆盖并发 toggle。
- 一次完整用户 Skill inventory 最多 256 项；每项 `display_name` 最多 1024 UTF-8 bytes、`description` 最多 8192 bytes、`sandbox_skill_dir` 最多 1024 bytes，规范 JSON 总量最多 1 MiB。任一项 key 非法、trim 后 key 重复、字段/数量/总量超限或元数据结构非法，都会使整次严格扫描失败；不得静默丢弃坏项后发布部分清单，也不得清空上次成功快照。
- 严格扫描必须携带扫描实际使用的不可变 `{sandbox_id, active_profile_id, active_profile_version}` 指纹；发布短事务对三项同时 CAS 后原子替换整份快照。成功空列表必须发布 `[]` 以移除已卸载项。扫描失败或部分读取失败不得清空旧快照。并发扫描按扫描开始时间防止较早但较慢的结果覆盖较新的结果。
  - 发布函数正常返回 CAS 失败表示另一代际或更新扫描胜出：请求必须重新读取当前代际 winner，且仅在读到该完整 winner 时返回 `inventory_state=current`；禁止返回输家的扫描结果或跨代际复用旧快照。
  - 发布事务抛出异常不等同于 CAS 竞争失败，也不能证明 DB 中已有 winner。此时重新读取到的当前代际旧快照只能作为 `stale` 降级并返回 `sandbox_status=unavailable`；无安全旧快照时返回 `inventory_state=unavailable`。未发布的本次扫描和旧快照均不得误报为 `current`。
- Agent 初始化与 `get_skill` miss 触发的严格完整扫描也更新同一快照。普通 GET 是 DB 清单读取；仅 cache miss 或 `refresh=true` 路径可能恢复/创建容器，因此不得预取强制刷新。

### PUT /api/config/skills/{skill_name}

- **Body**: `{enabled: bool}`
- **Response 200**: `{skill_name, enabled, message: "ok"}`
- 启停状态以 `UserSkillConfig` 为权威源，仅控制运行时是否向模型暴露/加载 Skill；禁用不删除沙箱中的 Skill 文件

## 4. 行为语义与不变量

### Skill 渐进式加载（Progressive Disclosure）

三级加载：

- **L1（元数据）**：每次 LLM 请求前按当前启停快照（30s TTL）生成技能名称和描述，作为请求级上下文拼接，不写入长期 system message；每项 name/description 仅归一化为单行（并对 name 内反引号转义），防止换行伪造条目或破坏 markdown。不做字符数或总 token 上限截断——受单项来源限制，实际体量远小于压缩阈值留出的余量，故不额外做 token 记账
- **L2（按需加载）**：Agent 调用 `get_skill(name)` → 读取完整 SKILL.md 内容
- **L3（资源解析）**：自动将 SKILL.md 中的相对路径解析为沙箱绝对路径

### 工具装配流程（create_agent_tools）

1. 从候选表实例化工具（lazy lambda）
2. 通过 `exclude` set 排除不需要的工具（如 cron worker 排除 AskUserQuestion、SubAgentTool 和 memory tools）
3. 条件加载搜索工具（检查 `BOCHA_SEARCH_APPCODE` env var）
4. 发现 official skills（文件系统）并保留完整 inventory → 严格发现沙箱 user skills → 成功后发布 DB inventory 快照
5. 注册 GetSkillTool（带 lazy push/read 回调）
6. 返回 `(tools, skill_loader)`

### Skill 启停语义

- SkillLoader 保留官方与用户 Skill 的完整 inventory；禁用项不从 inventory 删除，便于之后重新启用。
- `UserSkillConfig` 由 SkillLoader 按 TTL 缓存后重新读取（`refresh_disabled_skills`），窗口由 `SKILL_DISABLED_CACHE_TTL_SECONDS` 配置（默认 30s，`0` 表示每步实时查库）：**每步 LLM provider 请求的元数据热路径复用该快照**，避免每步一次 DB 查询；**按需 `get_skill` 的 push/read 守卫路径用 `force=True` 跳过 TTL 强一致读取**，保证运行途中禁用能在单次加载内被捕获。变更无需重建 Agent，元数据最迟在 TTL 内影响后续请求。
- `UserSkillConfig` 刷新失败时沿用最近一次成功的禁用集合，且失败不刷新 TTL 时间戳（下次调用即重试）；若启动后尚无成功快照，则按全部启用降级，保证与既有“配置查询失败时加载全部 Skills”语义一致。
- 禁用是 DB/逻辑状态，不删除或改写用户沙箱中的官方/用户 Skill 文件；重新启用后可复用既有文件，必要时再按需推送。
- 当前 MVP 按单 worker 部署，进程内 Skill inventory、推送标记与并发锁只保证单 worker 内一致性；跨 worker/副本的推送状态协调延后实现。

### Bash 工具共享状态

- SandboxBash / BashOutput / BashKill 共享 `_BackgroundCommandTracker` 实例
- 跟踪后台进程 ID / 状态
- 后台 bash 命令默认设置服务端最大运行时间 `SANDBOX_BACKGROUND_COMMAND_TIMEOUT_SECONDS=21600` 秒；`0` 表示不设置服务端 timeout，负数为非法配置

### 前端编辑记忆文件的副作用链

`PUT /api/config/agent-files/{name}`:

1. DB upsert（乐观锁）
2. Force push to sandbox
3. Invalidate AgentPool cache（下次请求重建 Agent with 新 system prompt）

失效语义：

- idle Agent 立即从 AgentPool 移除；若其 tracker 中仍有后台 bash 命令，按 AgentPool eviction 规则做清理。
- running Agent 不得被 close / interrupt。配置更新只标记该 session 懒失效；当前 run 自然结束后，下一次 `get_or_create` 必须重建 Agent，避免继续使用旧 system prompt。
- 当前时间、时区、workspace 等 runtime context 不属于用户可配置 Agent 文件；它们在每次 LLM provider request 组装时临时注入，因此不依赖 AgentPool 失效来刷新。

### 子 Agent Profile（sub_agent）

`sub_agent` 工具的 `subagent_type` 参数解析为一个 **profile**，决定子 Agent 的系统提示与工具集。Profile 定义在 `src/agent/subagent_profiles.py`（`PROFILES` + `resolve_profile()`）。

**核心不变量**：

- 子 Agent **不继承**父 Agent 的分层记忆（SOUL/AGENTS）作为系统提示，而是加载 profile 自带的精简系统提示（runner 通过 `AgentService(system_prompt_override=...)` 注入）。
- 子 Agent 一律禁用 `AskUserQuestionTool`（无人值守）与 `SubAgentTool`（防止无限嵌套）。
- 子 Agent 一律禁用 `ManageCronTool`。
- profile 通过 `tool_exclude` set 喂入 `create_agent_tools`，复用既有 exclude 机制。

**三个 profile**：

| profile | 定位 | 额外禁用工具（在公共禁用之外） |
| --- | --- | --- |
| `research` | 读 + 联网 + 抓取（bash），靠提示约束不主动改 workspace | `SandboxWriteTool`、`SandboxEditTool`、记忆写工具（`RecordDailyLog`/`UpdateLongTermMemory`/`UpdateUserProfile`） |
| `write` | 办公长任务产物工：创建/更新/修改/批注 workspace 文件 | 记忆写工具 |
| `general` | 兜底（默认） | 无 |

> 公共禁用 = `AskUserQuestionTool` + `SubAgentTool` + `ManageCronTool`。

**legacy 值映射**（向后兼容，大小写不敏感）：

- `research` / `explore` / `plan` / `review` → `research`
- `write` / `code` / `debug` → `write`
- 未知 / 空 / `general` → `general`

**graph edge**：`agent_type` 字段保留调用方传入的原始 `subagent_type`（用于展示），实际解析出的 profile 记录在 edge metadata 的 `profile` 字段。

**超时**：`SubAgentTool.execute_timeout = 0`，即子 Round 由自身步数上限管控，不受父 Agent 单次工具超时（`agent_tool_timeout`，默认 300s）拦截——避免 research 长抓取被中途 kill 留下不一致 child Round。

**委派触发指引（主 Agent 何时用 sub_agent）**：

- 工具层：`SubAgentTool.description` 写明"何时用 / 何时不用"——子任务产生大量一次性输出（联网抓取、长文档检索、批量产物）时委派以隔离上下文；需要与用户频繁来回或与当前上下文紧密迭代时不委派。
- 系统提示层：`AGENTS.md` 模板的"工具使用规则"含 `sub_agent` 小节与判断框架行，作为主 Agent 的委派策略默认注入。该文件由平台模板统一管理，不作为用户配置面板入口暴露。
- 子 Agent 与主 Agent 一样使用 request-only runtime context：profile system prompt 保持稳定，实时上下文只进入本次 provider request。

## 5. 失败模式与错误处理

- Skill 目录不存在 → 跳过，warning 日志
- `exclude` 中包含未知工具名 → warning 日志
- Skill push 失败 → 不阻塞工具注册
- Skill 启停配置刷新失败 → 记录 warning 并沿用最近成功状态；无历史状态时默认全部启用
- 用户沙箱发现失败 → GET Skills 返回官方清单并标记 `sandbox_status=unavailable`
- 搜索工具 env var 缺失 → 不注册搜索工具（静默降级）

## 6. 可观测性

- 工具注册列表日志
- Skill 发现结果日志
- exclude 警告日志
- Skill push 结果日志

## 7. 非目标

- 不做动态工具注册（需重启）
- 不做工具权限控制
- 不做 Skill 市场 / 安装
- 不做 Skill 版本管理
- 不做工具执行审计
