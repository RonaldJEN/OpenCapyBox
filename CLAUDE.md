# CLAUDE.md - OpenCapyBox AI 助手指南

本文档为使用 OpenCapyBox 项目的 AI 助手（如 Claude）提供核心约束与导航。
**始终使用中文回复用户。不要随便写操作手册。**

## ⚠️ 强制约束

### OpenSandbox 全量迁移（无本地 fallback）

1. **命令执行必须走 OpenSandbox**：使用 `sandbox.commands.run()`，不再依赖本地 subprocess/shared_env。
2. **文件操作必须走 OpenSandbox**：使用 `sandbox.files.*`（`read_file` / `write_file` / `search` / `read_bytes_stream`）。
3. **会话资源以 sandbox_id 为准**：DB 中 `user_sandbox` 表为恢复依据（connect/resume）。
4. **`shared_env` / `init_env` 属于历史机制**：新增代码禁止继续依赖这些逻辑。
5. **Agent Server 角色**：仅负责 LLM 推理、AG-UI 事件、鉴权与数据库持久化。

### 单元测试要求

**修改代码后必须补足单元测试。**

1. 新增功能：必须编写对应单元测试。
2. 修改现有代码：必须更新相关测试，确保覆盖修改逻辑。
3. 修复 Bug：必须添加回归测试，防止问题再次出现。
4. 测试位置：统一放在 `tests/` 目录。
5. CI/CD：PR 会自动运行测试，禁止带失败测试提交。

### API 文档维护要求

**只要改了接口，就必须同步 docs。**

1. 后端 API 修改：修改 `src/api/routes/` 后，必须更新 `docs/Capy-project-md/api.md`。
2. 前端 API 调用修改：修改 `frontend/src/services/api.ts` 或相关组件调用后，必须更新 `docs/Capy-project-md/frontend.md`。
3. 提交前校验：若改动涉及前后端接口，PR 必须同时包含 docs 变更。

### 文档单一事实源（SSOT）

1. **CLAUDE.md 只保留摘要与导航**，不再重复维护大段接口细节。
2. 后端接口细节以 `docs/Capy-project-md/api.md` 为准。
3. 前端接口映射以 `docs/Capy-project-md/frontend.md` 为准。
4. 架构与协议说明统一以 `docs/Capy-project-md/` 下文档为准。

### 前端交互与性能约定

1. 右侧 Files/Artifacts 必须是覆盖式抽屉（Overlay Drawer），禁止通过 `padding-right/width + transition-all` 挤压聊天区。
2. 抽屉动效优先 `transform/opacity`，避免布局属性动画。
3. 会话切换按 `sessionId` 记忆并恢复 `scrollTop`，仅底部时自动跟随。
4. 历史内容首次渲染禁用逐条动画，使用 `disableMotion` 控制。
5. 涉及交互策略改动前，先阅读 `frontend/DESIGN_SYSTEM.md` 并同步更新。

## 项目概述（摘要）

OpenCapyBox 是一个前后端分离的 Web 智能体平台，核心能力包括：

1. 多模型（Anthropic/OpenAI 协议）统一注册与切换。
2. OpenSandbox 隔离执行（命令、文件、会话资源）。
3. AG-UI 原生事件流（SSE、重放、断线恢复）。
4. 分层记忆（USER/MEMORY/SOUL/AGENTS/HEARTBEAT）。
5. Skills 动态加载与用户级启停。
6. Cron 定时任务与执行历史。

> 详细实现细节请不要在本文件重复维护，统一看 docs。

## 文档导航（权威来源）

1. 后端 API：`docs/Capy-project-md/api.md`
2. 前端 API 对照：`docs/Capy-project-md/frontend.md`
3. 系统架构：`docs/Capy-project-md/architecture.md`
4. 沙箱机制：`docs/Capy-project-md/sandbox.md`
5. AG-UI 协议：`docs/Capy-project-md/ag-ui-md/`
6. 前端设计规范：`frontend/DESIGN_SYSTEM.md`
7. 环境变量参考：`docs/Capy-project-md/env-reference.md`
8. 框架设计文档：`docs/Capy-project-md/design.md`

## 仓库关键路径（速查）

1. 后端入口：`src/api/main.py`
2. 后端配置：`src/api/config.py`
3. 模型注册表：`models.yaml` + `src/api/model_registry.py`
4. Agent 核心：`src/agent/agent.py`
5. 工具注册：`src/api/services/agent_service.py`（`_create_tools()`）
6. Agent 配置路由：`src/api/routes/config.py`（记忆文件、Skills 启停等）
7. 前端入口：`frontend/src/App.tsx`
8. 前端 API 客户端：`frontend/src/services/api.ts`
9. 测试目录：`tests/`

## 开发工作流（简版）

### 本地启动

```bash
# 安装依赖
uv sync
cd frontend && npm install

# 启动后端
uv run uvicorn src.api.main:app --reload --port 8000

# 启动前端
cd frontend && npm run dev
```

### 测试

```bash
pytest tests/ -v                    # 全量
pytest tests/test_xxx.py -v         # 单文件
pytest tests/ -k "test_name" -v     # 按名称匹配
```

> `pyproject.toml` 已配置 `asyncio_mode = "auto"`，异步测试函数直接 `async def` 即可，无需手动标注。
> 若修改了接口或调用映射，测试通过之外还必须更新 docs。

## AI 助手关键约定

### 代码修改指南

1. 使用 `uv`（不要使用 `pip`）。
2. 后端代码位于 `src/api/` 与 `src/agent/`。
3. 前端代码位于 `frontend/`。
4. 遵循架构：路由 -> 服务 -> 模型（后端）；组件 -> 服务 -> API（前端）。
5. 配置优于代码：模型在 `models.yaml`，环境在 `.env`。
6. 先读后改：修改前先读取并理解现有实现。

### 新功能最小检查清单

1. 代码变更完成。
2. 单元测试补齐并通过。
3. 若涉及接口：同步更新 `docs/Capy-project-md/api.md`。
4. 若涉及前端调用：同步更新 `docs/Capy-project-md/frontend.md`。
5. 变更说明可追溯（PR 中写清楚代码与文档同步关系）。

### 常见坑位

1. 不要混淆后端路径（是 `src/api/`，不是 `backend/`）。
2. 不要绕过工具接口直接实现 Agent 行为。
3. 不要修改 `src/agent/skills/` 子模块源码。
4. 不要提交敏感信息（`.env` 已忽略）。
5. 不要硬编码模型信息，统一走 Model Registry。

## 环境变量（最小集合）

完整变量以 `.env.example` 为准。常用最小集合：

```bash
# 必需
LLM_API_KEY=your-dashscope-key
SIMPLE_AUTH_USERS=demo:demo123

# OpenSandbox
SANDBOX_DOMAIN=localhost:8080
SANDBOX_API_KEY=your-sandbox-key

# 应用
API_PREFIX=/api
DATABASE_URL=sqlite:///./data/database/open_capy_box.db
AUTH_SECRET_KEY=replace-with-random-secret

# Agent/SSE
AGENT_MAX_STEPS=100
AGENT_MAX_HISTORY_MESSAGES=120
SSE_HEARTBEAT_INTERVAL=15
SSE_SUBSCRIBE_TIMEOUT=300
```

## 故障排查（简版）

1. 后端起不来：先检查 `.env`、端口占用、`LLM_API_KEY`。
2. 前端连不上：检查后端端口、Vite 代理、CORS。
3. 沙箱异常：检查 `SANDBOX_DOMAIN` 与 `SANDBOX_API_KEY`，查看 connect/resume/create 日志。
4. MCP 相关：当前默认不自动注册 MCP Tools，`src/agent/config/mcp.json` 仅作为配置入口。

---

**最后更新**: 2026-04-12
**项目版本**: 0.1.0
**维护者**: OpenCapyBox 团队
