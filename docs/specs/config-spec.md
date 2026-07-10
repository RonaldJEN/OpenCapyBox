# Agent 配置与技能 (Config) — Spec

## 1. 模块职责边界

- Agent 记忆文件读写接口（对前端暴露）
- Skills 发现、启停管理
- Skill 渐进式加载（Progressive Disclosure）
- Agent 工具注册与装配（tool_factory）
- **不负责**：记忆内容语义理解、Skill 执行逻辑

## 2. 数据模型

复用 memory-spec 中的 `user_memory` 和 `user_skill_configs` 表。

### Skill 元数据（文件系统）

- 来源：`src/agent/skills/` 目录下各子目录
- 每个 Skill 目录包含 `SKILL.md`（frontmatter 至少包含 `name`、`description`；其余字段可选）
- 用户自定义 Skill：沙箱中 `/home/user/skills/` 目录

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

- **Response 200**: `{skills: [{name, description, category, source, enabled}], sandbox_status}`
- `source`: `official`（平台文件系统）或 `user`（用户沙箱）
- `sandbox_status`: `not_created` / `available` / `unavailable`
  - `not_created`: 用户尚无可连接的持久化沙箱，仅返回官方 Skills
  - `available`: 沙箱可用，返回官方 Skills 与发现到的用户 Skills
  - `unavailable`: 已有沙箱记录但本次连接、恢复或发现失败；接口仍返回 200 和官方 Skills
- 合并：SkillLoader 文件系统发现 + UserSkillConfig DB 状态
- 包含沙箱中用户自定义 Skill
- 沙箱发现采用部分成功语义：沙箱不可用不应阻断官方 Skills 清单

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
4. 发现 official skills（文件系统）并保留完整 inventory → 发现沙箱 user skills
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
