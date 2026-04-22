# CLAUDE.md - OpenCapyBox AI 助手指南

本文档为使用 OpenCapyBox 项目的 AI 助手（如 Claude）提供核心约束与导航。
**始终使用中文回复用户。不要随便写操作手册。**

## 指令优先级与通用行为准则（补充）

为降低常见 LLM 编码失误，补充以下通用行为准则。

### 优先级（发生冲突时）

1. 项目强制约束（尤其：禁止防御性编程、改动后必须补测试、接口/行为变更必须同步 spec）。
2. 本节通用行为准则。
3. 其他建议性实践。

### 1) 先思考，再编码

1. 禁止不加说明地假设；先写明关键假设。
2. 存在多种合理解释时，先给出分歧点，不得静默拍板。
3. 发现更简单方案时，要主动提出并说明取舍。
4. 需求不清晰时先澄清，再实现。

### 2) 简单优先

1. 只实现用户当前明确要求，不做额外功能扩展。
2. 不为一次性代码引入抽象层。
3. 不提前做未被要求的“灵活性/可配置性”。
4. 不为不可能场景补兜底逻辑（与“禁止防御性编程”一致）。

### 3) 手术式改动

1. 只改和需求直接相关的代码。
2. 不顺手重构、不顺手改格式、不顺手改注释。
3. 保持原有代码风格与边界。
4. 只清理“本次改动引入”的无用代码；历史遗留问题记录后单独处理。

### 4) 目标驱动与可验证闭环

1. 任务要转成可验证目标，不接受“看起来能跑”。
2. 修 Bug：先构造复现，再修复，再回归验证。
3. 改行为：先补/改测试，再让测试通过。
4. 多步骤任务先写简短计划，每步都给验证方式。

## ⚠️ 强制约束-编码规范-第一原则:禁止防御性编程

### 第一原则:禁止防御性编程

1.严禁一切非必要的防御性编程。这是本项目最高优先级的编码规范，违反即为严重缺陷。
具体要求:
2.禁止写try-except吞掉异常。如果代码出错，必须让它暴露出来，而不是静默失败让人反复排查禁止对参数做多余的3.None/空值检查。调用方有责任传正确的参数，不要在被调用方做无意义的保护
4.禁止ifxis not None/or""/or0等兜底写法，除非业务逻辑明确要求
5.禁止写“以防万一"的代码。每一行代码必须有明确的、当下就需要的理由
6.出错就让它崩。崩溃信息比静默失败有价值一万倍
7.先让功能跑通，再考虑边界情况。不要在功能都没验证的时候就开始“防御”单元测试要求

**修改代码后必须补足单元测试。**

1. 新增功能：必须编写对应单元测试。
2. 修改现有代码：必须更新相关测试，确保覆盖修改逻辑。
3. 修复 Bug：必须添加回归测试，防止问题再次出现。
4. 测试位置：统一放在 `tests/` 目录。
5. CI/CD：PR 会自动运行测试，禁止带失败测试提交。

### Spec 文档维护要求

**只要改了接口或行为，就必须同步对应 spec。**

1. 后端 API / 服务层修改：必须更新 `docs/specs/` 下对应模块的 spec 文件。
2. 前端 API 调用修改：若涉及契约变更，同步更新对应 spec。
3. 提交前校验：若改动涉及接口或行为语义，PR 必须同时包含 spec 变更。

### 文档单一事实源（SSOT）

1. **CLAUDE.md 只保留摘要与导航**，不再重复维护大段接口细节。
2. **`docs/specs/` 下的 spec 文件是各模块的权威源**（数据模型、API 契约、行为语义、失败模式）。
3. AG-UI 协议说明以 `docs/Capy-project-md/ag-ui-md/` 为准。
4. 前端规范与设计体系以 `docs/specs/frontend-spec.md` 为准（及 chat/session/panel 子 spec）。
5. 环境变量参考以 `docs/Capy-project-md/env-reference.md` 为准。

### 前端交互与性能约定

1. 右侧 Files/Artifacts 必须是覆盖式抽屉（Overlay Drawer），禁止通过 `padding-right/width + transition-all` 挤压聊天区。
2. 抽屉动效优先 `transform/opacity`，避免布局属性动画。
3. 会话切换按 `sessionId` 记忆并恢复 `scrollTop`，仅底部时自动跟随。
4. 历史内容首次渲染禁用逐条动画，使用 `disableMotion` 控制。
5. 涉及交互策略改动前，先阅读 `docs/specs/frontend-spec.md` 与对应子 spec，并同步更新。

## 项目概述（摘要）

OpenCapyBox 是一个前后端分离的 Web 智能体平台，核心能力包括：

1. 多模型（Anthropic/OpenAI 协议）统一注册与切换。
2. OpenSandbox 隔离执行（命令、文件、会话资源）。
3. AG-UI 原生事件流（SSE、重放、断线恢复）。
4. 分层记忆（USER/MEMORY/SOUL/AGENTS）。
5. Skills 动态加载与用户级启停。
6. Cron 定时任务与执行历史。

> 详细实现细节请不要在本文件重复维护，统一看 docs。

## 文档导航（权威来源）

### Spec 文件（各模块权威源）

| Spec                                    | 覆盖范围                   |
| --------------------------------------- | -------------------------- |
| `docs/specs/auth-spec.md`             | 认证鉴权                   |
| `docs/specs/sessions-spec.md`         | 会话管理                   |
| `docs/specs/chat-spec.md`             | 聊天 / Agent 执行 / SSE 流 |
| `docs/specs/cron-spec.md`             | 定时任务                   |
| `docs/specs/memory-spec.md`           | 分层记忆                   |
| `docs/specs/sandbox-spec.md`          | 沙箱交互                   |
| `docs/specs/models-spec.md`           | 模型注册与切换             |
| `docs/specs/config-spec.md`           | Agent 配置与技能           |
| `docs/specs/frontend-spec.md`         | 前端总规范与设计体系       |
| `docs/specs/frontend-chat-spec.md`    | 前端聊天 / SSE / 推理面板  |
| `docs/specs/frontend-session-spec.md` | 前端会话列表与切换         |
| `docs/specs/frontend-panel-spec.md`   | 前端抽屉类面板             |

### 其他文档

1. AG-UI 协议：`docs/Capy-project-md/ag-ui-md/`
2. 前端规范与设计体系：`docs/specs/frontend-spec.md`（及 frontend-chat-spec / frontend-session-spec / frontend-panel-spec）
3. 环境变量参考：`docs/Capy-project-md/env-reference.md`

## 仓库关键路径（速查）

1. 后端入口：`src/api/main.py`
2. 后端配置：`src/api/config.py`
3. 模型注册表：`models.yaml` + `src/api/model_registry.py`
4. Agent 核心：`src/agent/agent.py`
5. 工具注册：`src/api/services/tool_factory.py`（`create_agent_tools()`）
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
3. 若涉及接口或行为语义：同步更新 `docs/specs/` 下对应 spec 文件。
4. 变更说明可追溯（PR 中写清楚代码与 spec 同步关系）。

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

**最后更新**: 2026-04-22
**项目版本**: 0.1.0
**维护者**: OpenCapyBox 团队
