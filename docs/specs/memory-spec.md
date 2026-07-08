# 分层记忆 (Memory) — Spec

## 1. 模块职责边界

- 用户记忆文件管理（SOUL / USER / MEMORY）与平台规则模板（AGENTS）
- 用户记忆文件 CRUD（DB 为权威源）；AGENTS.md 以平台模板为权威源
- 双写同步（DB <-> 沙箱）
- 嵌入向量生成与混合检索（BM25 + 向量 + RRF）
- 对话轮索引
- 新用户模板初始化
- **不负责**：记忆文件内容解析、Agent 行为控制

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
| created_at | DateTime | default=now |

### user_skill_configs 表

| 字段 | 类型 | 约束 |
|---|---|---|
| id | Integer | PK, autoincrement |
| user_id | String(100) | NOT NULL, indexed |
| skill_name | String(100) | NOT NULL |
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
- 副作用：更新 DB -> 强制推送到沙箱 -> 失效 AgentPool 缓存

### GET /api/config/skills

- 成功响应 200：

```json
{
  "skills": [
    {
      "name": "web_search",
      "description": "搜索互联网",
      "category": "builtin",
      "enabled": true
    }
  ]
}
```

- 合并 SkillLoader 发现结果 + UserSkillConfig 数据库状态

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

### Dirty Flag 检测与兜底

- **工具名匹配**：`record_memory`、`update_long_term_memory`、`update_user`
- **文件操作嗅探**：`write_file` / `edit_file` 目标为记忆文件
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

- 从 SOUL / USER / MEMORY 用户 DB 文件与平台 AGENTS.md 模板拼接
- Token 预算：`token_limit` 的 15%
- 低优先级记忆使用二分查找截断
- System Prompt 只包含稳定规则与记忆；当前时间、时区、workspace 等 runtime context 由 LLM 请求组装层临时注入，不写入 SOUL / USER / MEMORY / AGENTS，也不回写用户 DB。

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
