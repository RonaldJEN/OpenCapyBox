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
- 每个 Skill 目录包含 `SKILL.md`（frontmatter: name, description, category, dir）
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

- **Response 200**: `{skills: [{name, description, category, enabled}]}`
- 合并：SkillLoader 文件系统发现 + UserSkillConfig DB 状态
- 包含沙箱中用户自定义 Skill

### PUT /api/config/skills/{skill_name}

- **Body**: `{enabled: bool}`
- **Response 200**: `{skill_name, enabled, message: "ok"}`

## 4. 行为语义与不变量

### Skill 渐进式加载（Progressive Disclosure）

三级加载：

- **L1（元数据）**：技能名称和描述注入 system prompt，Agent 知道有哪些技能
- **L2（按需加载）**：Agent 调用 `get_skill(name)` → 读取完整 SKILL.md 内容
- **L3（资源解析）**：自动将 SKILL.md 中的相对路径解析为沙箱绝对路径

### 工具装配流程（create_agent_tools）

1. 从候选表实例化工具（lazy lambda）
2. 通过 `exclude` set 排除不需要的工具（如 cron worker 排除 AskUserQuestion、SubAgentTool 和 memory tools）
3. 条件加载搜索工具（检查 `BOCHA_SEARCH_APPCODE` env var）
4. 发现 official skills（文件系统）→ 过滤 disabled skills → 发现沙箱 user skills
5. 注册 GetSkillTool（带 lazy push/read 回调）
6. 返回 `(tools, skill_loader)`

### Bash 工具共享状态

- SandboxBash / BashOutput / BashKill 共享 `_BackgroundCommandTracker` 实例
- 跟踪后台进程 ID / 状态

### 前端编辑记忆文件的副作用链

`PUT /api/config/agent-files/{name}`:

1. DB upsert（乐观锁）
2. Force push to sandbox
3. Invalidate AgentPool cache（下次请求重建 Agent with 新 system prompt）

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
- 系统提示层：`AGENTS.md` 模板的"工具使用规则"含 `sub_agent` 小节与判断框架行，作为主 Agent 的委派策略默认注入（用户可编辑）。

## 5. 失败模式与错误处理

- Skill 目录不存在 → 跳过，warning 日志
- `exclude` 中包含未知工具名 → warning 日志
- Skill push 失败 → 不阻塞工具注册
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
