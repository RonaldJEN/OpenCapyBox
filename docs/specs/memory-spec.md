# 分层记忆 (Memory) — Spec

## 1. 模块职责边界

- 用户记忆文件管理（SOUL / USER / MEMORY）与平台规则模板（AGENTS）
- 用户记忆文件 CRUD（DB 为权威源）；AGENTS.md 以平台模板为权威源
- 双写同步（DB <-> 沙箱）
- 嵌入向量生成与混合检索（BM25 + 向量 + RRF）
- 对话轮索引
- 新用户模板初始化
- **当前不负责**：记忆文件内容解析、后台候选提炼、自动合并 canonical Memory、Agent 行为控制

---

## 2. 数据模型

### user_memory 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK, autoincrement |
| user_id | String(100) | NOT NULL, indexed |
| file_type | String(20) | NOT NULL. 取值: `user_md`, `memory_md`, `soul_md`；`agents_md` 仅兼容旧数据，不再新写入 |
| content | Text | NOT NULL |
| version | Integer | default=1, NOT NULL（乐观锁） |
| updated_at | DateTime | default=now, onupdate=now |

### memory_embeddings 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK, autoincrement |
| user_id | String(100) | NOT NULL, indexed |
| file_path | String(255) | nullable |
| chunk_index | Integer | nullable |
| chunk_text | Text | NOT NULL |
| embedding | PostgreSQL pgvector `vector(2560)` | nullable（float 数组；不足 2560 维右侧补 0） |
| conversation_round_id | String(36) | nullable，FK → `rounds.id`，ON DELETE CASCADE |
| created_at | DateTime | default=now |

### user_skill_configs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK, autoincrement |
| user_id | String(100) | NOT NULL, indexed |
| skill_name | String(128) | NOT NULL；trim 后的稳定 Skill key，与 API/运行时 128 字符上限一致 |
| enabled | Boolean | default=True |
| updated_at | DateTime | default=now, onupdate=now |

---

## 3. API 契约

所有接口均需 Bearer Token 鉴权。

### GET /api/config/agent-files/{name}

- `name` 取值范围：`user` | `soul` | `memory`
- 成功响应 200：

```json
{
  "name": "user",
  "file_type": "user_md",
  "content": "...",
  "version": 3
}
```

- `AGENTS.md` 不属于用户配置 API；它由平台模板内部注入 system prompt 与沙箱。
- 错误 400：`"无效的文件名"`

### PUT /api/config/agent-files/{name}

- 请求体：

```json
{ "content": "新的记忆内容" }
```

- 成功响应 200：

```json
{
  "name": "user",
  "file_type": "user_md",
  "version": 4,
  "message": "ok"
}
```

- 错误 400（无效文件名）、409（版本冲突，来源 RuntimeError）
- 副作用：更新 DB -> 强制推送到沙箱 -> 失效 AgentPool 缓存；`MEMORY.md` 不会因此进入 system prompt
- 当前限制：该前端/API 写路径不重建 embedding；运行时受控工具与 dirty-file 回写路径才在 USER / MEMORY 内容变化时重建。因而 API 保存后的旧索引可能持续到下一次重建，该问题留待记忆方案重构时统一处理

### GET /api/config/skills

- 成功响应 200：

```json
{
  "skills": [
    {
      "key": "web_search",
      "name": "web_search",
      "display_name": "网页搜索",
      "description": "搜索互联网",
      "category": "builtin",
      "source": "official",
      "enabled": true
    }
  ],
  "skill_issues": [],
  "sandbox_status": "available",
  "inventory_state": "current",
  "inventory_discovered_at": "2026-07-17T10:00:00"
}
```

- `key` 是聊天 `preferred_skill_keys`、Skill 启停和运行时加载共同使用的稳定内部标识：trim 后必须非空且不超过 128 个 Unicode 字符；允许人类可读 Unicode、空格和括号，禁止 `/`、`\`、`?`、`#`、`%` 及 Unicode `C*` 控制/不可见类别字符
- `display_name` 只用于展示：依次读取 SKILL.md frontmatter 顶层 `display_name` / `display-name`、`metadata` 内同名字段，缺失或空白时回退 `name`；不得作为请求 key
- 合并 SkillLoader 发现结果 + UserSkillConfig 数据库状态
- `source` 为 `official` 或 `user`；`sandbox_status` 为 `not_created`、`available` 或 `unavailable`
- 用户 Skill 使用与当前 sandbox/Profile 代际绑定的最近一次完整 DB 快照，普通读取不等待远程扫描；`refresh=true` 或快照缺失时严格扫描，并把可用 `inventory_json` 与逐项诊断 `issues_json` 原子发布
- 单个 `SKILL.md` 读取/frontmatter/字段/key 异常或重复时隔离该项并返回 `skill_issues`，其余合法 Skill 继续可用；全部候选损坏时允许返回 `skills=[]` 与非空 `skill_issues`
- 可用用户 Skill 最多 256 项；每项 `display_name`、`description`、`sandbox_skill_dir` 分别不超过 1024、8192、1024 UTF-8 bytes，规范 inventory JSON 总量不超过 1 MiB。`skill_issues` 最多 256 项且 JSON 不超过 256 KiB，超出诊断截断但不影响合法 Skill
- 沙箱访问、目录枚举、代际确认或 DB 发布失败仍视为整个扫描失败，不得发布本次部分结果
- 沙箱未创建或暂不可用时仍返回 200 和官方 Skills；强制刷新失败时可保留并返回当前 sandbox/Profile 代际的旧快照，同时用 `inventory_state=stale` 与 `sandbox_status=unavailable` 标记降级
- 完整扫描成功且快照发布成功时才能把该扫描结果标记为 `current`。正常 CAS 竞争失败时必须重新读取并返回当前代际的 winner，方可标记 `current`；快照持久化抛错不是 CAS 竞争胜负，若安全旧快照存在只能标记 `stale`，否则为 `unavailable`，不得把旧快照或未发布扫描误报为 `current`

### PUT /api/config/skills/{skill_name}

- 请求体：

```json
{ "enabled": true }
```

- 成功响应 200：

```json
{
  "skill_name": "web_search",
  "enabled": true,
  "message": "ok"
}
```

- 启停仅更新 `UserSkillConfig` 并影响后续 LLM 请求的 Skill 元数据/按需加载；不会删除沙箱中的 Skill 文件

---

## 4. 行为语义与不变量

### 双写同步规则

| 场景 | 方向 | 行为 |
|---|---|---|
| 新用户 | template -> DB -> sandbox | `provision_default_files` 幂等 |
| Agent 运行时修改 | sandbox -> DB | 受控工具写入成功后即时同步；dirty flag 在 round 结束后兜底校验 |
| 前端编辑 | DB -> sandbox (force) | 覆写沙箱内容 |
| 新 Session Agent 创建 | sandbox-first | 沙箱有内容 -> 写回 DB；空则 DB -> sandbox |
| AGENTS.md | template -> sandbox / system prompt | 平台模板覆盖沙箱，不从沙箱或用户 DB 反向同步 |

### 运行时记忆工具

- `update_long_term_memory` 是保留的显式 `MEMORY.md` 管理工具，支持 `read` / `write` / `append`；它不是后台任务，也不由 token 阈值、上下文压缩或 Round 收尾自动调用
- `update_user` 以相同模式管理 `USER.md`；长期文件写入只允许发生在用户明确要求跨会话保存或修改 Agent 配置时
- `search_memory` 是只读 DB 检索，可返回长期文件 embedding 与既有 `conversation/*` 对话轮索引；检索结果不会自动回写 `MEMORY.md`
- `read_user` 只读 `USER.md`

### Dirty Flag 检测与兜底

- **工具名匹配**：`update_long_term_memory`、`update_user`
- **文件操作嗅探**：`apply_patch` 补丁目标包含记忆文件
- **即时同步**：受控工具成功写入根目录 USER / MEMORY / SOUL 后，立即调用 DB 同步；USER / MEMORY 内容变化时同步重建 embedding
- **AGENTS 保护**：根目录 AGENTS.md 由平台模板管理，受控文件工具拒绝写入，后台同步也不回写 DB
- **兜底同步**：每 round 结束后若 dirty -> 从 sandbox 读回 DB-backed 文件；仅内容实际变化时更新版本并重建 USER / MEMORY embedding
- **盲区**：bash 修改记忆文件不可检测（AGENTS.md 中禁止此行为，且后续模板同步会覆盖根 AGENTS.md）

### 乐观锁

- `upsert_memory_file` 支持 `expected_version` 参数
- 版本不匹配 -> 抛出 `RuntimeError` -> 前端收到 HTTP 409

### 混合检索

- **BM25 分词**：中文逐字，英文逐词（零外部依赖）
- **向量检索**：外部 embedding API（支持 model_registry + settings fallback）
- **向量存储维度**：`memory_embeddings.embedding` 固定为 2560 维；Embedding API 返回短向量时右侧补 0。
- **PostgreSQL 前置条件**：生产库必须安装 pgvector 扩展；扩展缺失时启动 / 迁移直接失败。
- **RRF 融合**：k=60，取 3x top_k 候选
- **时间衰减**：half_life=30 天，指数衰减；常驻文件（MEMORY / USER / SOUL / AGENTS.md）豁免
- **降级策略**：无 embedding API 时降级为纯 BM25

### 文本分块

- `chunk_size` = 512 tokens
- 双换行分段，小段合并

### 记忆文件模板

- 来源：`docs/sandbox_template/` 目录
- 初始化时剥离 YAML frontmatter

### System Prompt 构建

- 主会话默认 system prompt 的固定输入仅由用户 DB 中的 SOUL / USER 与平台 AGENTS.md 模板拼接；Cron 与子 Agent 的独立 override 规则见各自 spec
- 当前实现不将 `MEMORY.md` 内容拼入 system prompt；它继续以 DB 为权威源持久化，并可通过显式文件/记忆工具或 `search_memory` 按需读取
- 当前时间、时区、workspace 等 runtime context 由 LLM 请求组装层临时注入，不写入 SOUL / USER / MEMORY / AGENTS，也不回写用户 DB

### 写入与自动化边界

- 当前没有任何轮后、token 阈值或 compaction 联动的模型记忆写入流程；Round 收尾只同步本轮已经显式发生的 dirty 文件写入，并为本轮对话维护 `conversation/*` episodic embedding
- `conversation/*` 是可检索的对话索引，不是 canonical Memory，也不得被当作自动提炼结果写回 `MEMORY.md`
- 新建对话索引必须写 `conversation_round_id`，Session 删除经 Round 级联删除这些索引。升级前旧索引不回填，允许永久为 NULL，且不阻断旧 Session 删除。
- 后续若引入候选提炼，必须在会话真正空闲后由持久化后台 job 执行，允许明确 no-op，并在进入模型前过滤 compaction summary、AGENTS/developer 指令、测试夹具及临时任务状态
- 后续候选只能写 append-only staging；在 consolidator 去重、解决冲突并按 scope 晋升前，不得更新 canonical Memory。该 candidate/consolidator 管道当前尚未实现

---

## 5. 失败模式与错误处理

| 失败场景 | 处理方式 |
|---|---|
| Embedding API 失败 | 返回 None embeddings，降级为纯 BM25 |
| 沙箱同步失败 | warning 日志，不阻塞主流程 |
| 版本冲突 | `RuntimeError` -> HTTP 409 |
| 无效 file_type | `ValueError` / HTTP 400 |

---

## 6. 可观测性

- 新用户初始化日志
- 同步方向与结果日志
- Embedding API 调用失败 warning
- 检索结果数量日志

---

## 7. 非目标

- 不做记忆文件的版本历史（只有当前版本 + version 数字）
- 不做跨用户记忆共享
- 不做自动遗忘 / 过期
- 不做结构化知识图谱
- 不做记忆文件导入 / 导出
