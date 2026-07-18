<div align="center">

# 🛁 OpenCapyBox

**Your AI assistant lives in a safe box — Sandboxed · Memory-Equipped · Skill-Pluggable · Beginner-Friendly**

```
   ╭━━━━━━━━━━━━╮
    ┃ OpenCapyBox┃
   ┃    ∩  ∩    ┃
   ┃   (◕ ᴥ ◕)  ┃
   ┃  ～～～～～  ┃
   ╰━━━━━━━━━━━━╯
```

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![TypeScript 5.2+](https://img.shields.io/badge/TypeScript-5.2+-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React 18](https://img.shields.io/badge/React-18.2-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

[Features](#-features) · [Screenshots](#-screenshots) · [Quick Start](#-quick-start) · [Architecture](#-architecture) · [Skills](#-skill-system) · [Memory](#-memory-system) · [Deployment](#-deployment-guide) · [Contributing](#-contributing)

**[中文文档](README_cn.md)**

</div>

---

## About

**OpenCapyBox** is an open-source full-stack AI agent platform. Like a capybara chilling in the water, your AI assistant lives safely inside a sandboxed container — executing code, processing documents, searching the web, managing files, while continuously building memory and learning new skills.

### Why OpenCapyBox?

| | Capybara Trait | OpenCapyBox Capability |
|---|---|---|
| 🛁 | Soaks in safe waters | One **OpenSandbox container** per user, fully isolated |
| 🧠 | Great memory, knows all friends | **Layered memory system** (USER.md / MEMORY.md / SOUL.md), learns as you use it |
| 🤝 | Friends with everyone | **Multi-model compatible** (Qwen / GLM / Kimi / DeepSeek / MiniMax), hot-swap anytime |
| 🎒 | Can carry anything | **40+ pluggable skills**, enable official skills in one click or upload custom ones |
| ⏰ | Scheduled routines | **Cron task system**, AI autonomously runs periodic jobs |
| 🌐 | Chill but reliable | Full sandbox-isolated execution, **beginner-friendly**, all operations visible in the UI |

## ✨ Features

### 🔀 Hot-Swap Multi-Model Support

Declarative registration via `models.yaml`, supporting both Anthropic and OpenAI protocols — no code changes needed:

| Model | Protocol | Platform | Features |
|-------|----------|----------|----------|
| Qwen3.5-plus | OpenAI | Alibaba DashScope | Chain-of-thought, multimodal |
| GLM-4.7 / GLM-5 | OpenAI | Alibaba DashScope | Chain-of-thought |
| Kimi-2.5 | OpenAI | Alibaba DashScope | Chain-of-thought, multimodal |
| DeepSeek-V3.2 | OpenAI | Alibaba DashScope | Chain-of-thought, long context |
| MiniMax-M2 | Anthropic | MiniMax | Native thinking |

<details>
<summary>💡 Want to add a new model? Just add an entry in <code>models.yaml</code> (click to expand)</summary>

```yaml
  my-new-model:
    display_name: "My New Model"
    provider: openai              # openai or anthropic
    api_base: "https://api.example.com/v1"
    api_key: "${MY_API_KEY}"      # References env variable from .env
    model_name: "my-model-name"
    max_tokens: 32768
    reasoning_format: reasoning_content  # none / reasoning_content / anthropic_thinking
    reasoning_split: true
    enable_thinking: true
    supports_image: false
    enabled: true
    tags: [thinking]
```

See the header comments in [`models.yaml`](models.yaml) for full configuration reference.

</details>

### 🛡️ One Sandbox Per User

- Each user gets an isolated **OpenSandbox** container
- All code execution, file operations, and shell commands run inside the container
- Persistent storage mount — no data loss
- File upload/download/search all proxied through the sandbox — **users don't need to worry about the internals**

### 🧠 Memory That Grows

| File | Purpose | Plain English |
|------|---------|---------------|
| `USER.md` | User profile — your preferences and habits | "It remembers what you like" |
| `MEMORY.md` | Long-term memory — accumulated knowledge | "It remembers what you've talked about" |
| `SOUL.md` | Personality definition — tone and style | "Its personality is shaped by you" |

Supports **BM25 keyword + vector semantic + RRF fusion** hybrid retrieval — the more you use it, the better it understands you.

### 🎨 Beginner-Friendly Interface

- **Claude warm-tone design** — soft colors, content-first
- **Streaming output** — thinking process and tool calls visible in real-time
- **Skill Manager** — category tags, toggle-style enable/disable
- **Agent Config Panel** — directly edit SOUL.md / USER.md / MEMORY.md
- **Cron Dashboard** — task list + execution history at a glance
- **File Panel** — preview and download sandbox files

### ⏰ Scheduled Tasks

Define Cron jobs via the `manage_cron` tool, and your AI assistant runs them autonomously:

- Visual task dashboard in the frontend
- Manual trigger / pause support
- Full execution history

### 🔧 Rich Built-in Tools

| Category | Tools | Description |
|----------|-------|-------------|
| 📁 File Ops | Read / Write / Edit | Read, write, and string-replace edit files in sandbox |
| 💻 Shell | Bash / BashOutput / BashKill | Execute commands in container, background process support |
| 🔍 Web Search | GLMSearch / BatchSearch | Bocha search engine, parallel batch search |
| 🧠 Memory | RecordDailyLog / SearchMemory | Layered persistent memory + hybrid retrieval |
| 📝 Session Notes | SessionNote / RecallNote | Cross-turn context preservation |
| ⏰ Cron | ManageCron | DB-backed cron worker |
| 🎒 Skills | GetSkill | 40+ dynamically loadable professional skills |
| 🔌 MCP | MCP Tools | Model Context Protocol tool integration |

### 🔌 MCP Tool Integration

OpenCapyBox supports external tools through [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) and currently supports **Streamable HTTP only**. Connections are stored in the database; the server no longer launches MCP processes from a local `mcp.json`:

- **Official MCP** servers are created, tested, published, and disabled by administrators. They may use a platform credential and may be marked platform-required; ordinary connections are user-enabled and may use a credential override, while required connections are automatically enabled and cannot be disabled by users.
- **Personal MCP** servers are owned and visible only by the user. Public HTTPS is required by default.
- **`mcp.json`** is an import/export format for personal MCP entries, not the runtime source of truth. Exports always omit tokens and headers.

```json
{
  "mcpServers": {
    "my-mcp-server": {
      "description": "My MCP Server",
      "type": "streamable-http",
      "url": "https://mcp.example.com/mcp",
      "disabled": false
    }
  }
}
```

- Credentials are encrypted at rest with an independent `MCP_SECRET_KEY`, and API responses never echo secrets. Production startup rejects missing, example, short, or reused signing/encryption keys and public example accounts.
- Each official or personal connection has an independent per-tool allowlist/denylist. This controls publication to the Agent, not execution permission. Discovered tools receive stable model-visible names; ownership, connection state, and the latest publication policy are rechecked before every call. `tools/call` has an independent non-disableable wall-clock deadline; an indeterminate result after the dispatch boundary is marked `unknown` and is never retried automatically.
- MCP tools use Deferred exposure by default: remote schemas are omitted from the initial request and loaded on demand through `tool_search`. Hidden tools and permission-`DENY` tools are excluded from discovery results.
- For users with effective MCP connections, the catalog fingerprint rotates every `MCP_CATALOG_REFRESH_SECONDS` (300 seconds by default), so the next Agent build fetches `tools/list` again even when the URL and credential are unchanged. If discovery fails, stored schema snapshots are not exposed as executable tools; the failure remains retryable.
- MCP connectivity/publication and tool permissions are separate domains: availability determines whether a tool is visible, while `ALLOW / ASK / DENY` decides whether a call may run. Official and personal MCP tools both default to `ALLOW`; users may still require approval or deny execution, and administrator rules form a ceiling users cannot relax.

## 📸 Screenshots

### Main Chat Interface

Streaming conversation + AI thinking process unfolding in real-time, tool calls fully visible.

![Main Chat Interface](docs/Capy-project-md/screenshots/主聊天界面.png)

### Skill Manager

Category tag filtering, toggle-style enable/disable, easily manage 40+ official skills.

![Skill Manager](docs/Capy-project-md/screenshots/官方skill管理.png)

### Agent Config Panel

Directly edit SOUL.md / USER.md / MEMORY.md to shape your AI assistant's personality and memory.

![Agent Config Panel](docs/Capy-project-md/screenshots/Agent配置面板.png)

### Cron Dashboard

Task list + execution history, manual trigger and status tracking support.

![Cron Dashboard](docs/Capy-project-md/screenshots/定时任务看板.png)

![Cron Dashboard](docs/Capy-project-md/screenshots/定时任务列表.png)

### File Panel

Browse sandbox files, preview and download Agent-generated artifacts.

![File Panel](docs/Capy-project-md/screenshots/文件面板.png)

### File Preview

Markdown rendered preview with source view and one-click download.

![File Preview](docs/Capy-project-md/screenshots/文件预览.png)

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Node.js 16+
- [uv](https://github.com/astral-sh/uv) (Python package manager)
- [OpenSandbox](https://github.com/alibaba/OpenSandbox) (optional, sandbox execution environment)

### 1. Clone and Install

```bash
git clone https://github.com/RonaldJEN/OpenCapyBox.git
cd OpenCapyBox

# Install Python dependencies
uv sync

# Install frontend dependencies
cd frontend && npm install && cd ..
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with at minimum:

```bash
# === Required ===
LLM_API_KEY=your-dashscope-key           # Alibaba DashScope unified key
SIMPLE_AUTH_USERS=demo:demo123           # Login users (format: user:pass,user2:pass2)

# === Database ===
# Replace the placeholder from .env.example before starting the backend.
# The PostgreSQL database must exist and have pgvector installed:
#   CREATE EXTENSION IF NOT EXISTS vector;
DATABASE_URL=postgresql://user:password@host:5432/opencapybox

# === OpenSandbox (optional) ===
SANDBOX_DOMAIN=localhost:8080
SANDBOX_API_KEY=your-sandbox-key

# === Others ===
# AGENT_MAX_STEPS=100
# AGENT_TOKEN_LIMIT=200000
```

The PostgreSQL URL in `.env.example` is only a template. Configure a real PostgreSQL database and enable the `pgvector` extension before running `uv run uvicorn ...`; otherwise startup or database initialization will fail.

### 3. Start Services

```bash
# Start backend (port 8000)
uv run uvicorn src.api.main:app --reload --port 8000

# In a new terminal, start frontend (port 3000)
cd frontend && npm run dev
```

Open http://localhost:3000 and log in with `demo` / `demo123`.

### Docker Deployment

```bash
cd deploy/docker

# Set up environment variables
cp ../../.env.example ../../.env
# Edit .env and fill in your API Key

# Start
docker-compose up -d

# View logs
docker-compose logs -f
```

## 🏗️ Architecture

### AG-UI Protocol

OpenCapyBox uses **AG-UI (Agent User Interaction Protocol)** for frontend-backend communication. AG-UI is an event-driven protocol designed for AI Agent scenarios, defining 22 standardized event types (lifecycle, text messages, thinking process, tool calls, state management, etc.), streamed to the frontend via SSE. Compared to traditional request-response patterns, it enables Agent's multi-step reasoning, tool calls, and chain-of-thought to be presented to users **in real-time, incrementally**, with `lastSequence` reconnection support to ensure no events are lost after SSE disconnection.

> 📖 Detailed protocol docs at [docs/Capy-project-md/ag-ui-md/](docs/Capy-project-md/ag-ui-md/)

```
┌──────────────────────────────────────────────────────────────────┐
│  Frontend — React 18 + TypeScript + Vite + TailwindCSS          │
│  Session Mgmt · Streaming Render · Model Switch · Skill Mgmt    │
├──────────────┬───────────────────────────────────────────────────┤
│              │  REST API + SSE (AG-UI Event Protocol)            │
├──────────────▼───────────────────────────────────────────────────┤
│  Backend — FastAPI + SQLAlchemy + PostgreSQL + pgvector         │
│  JWT Auth · Turn Orchestration · Agent Pool · Memory · Cron     │
├──────────────┬───────────────────────────────────────────────────┤
│              │  Agent ↔ LLM Provider / OpenSandbox               │
├──────────────▼───────────────────────────────────────────────────┤
│  Agent Core — Python Async Execution Engine                      │
│  Multi-step Reasoning · Tool Calls · Token Cache · Summarize    │
├──────────────┬──────────────────┬────────────────────────────────┤
│              ▼                  ▼                                 │
│  LLM Providers               OpenSandbox                         │
│  Qwen / GLM / Kimi /         Containerized Code Execution       │
│  DeepSeek / MiniMax           One Sandbox Per User               │
└──────────────────────────────────────────────────────────────────┘
```

### Turn Orchestration

The chat backend splits each conversational turn into three responsibilities: the **gateway** only translates protocol, the **orchestrator** owns the full lifecycle, and the **runtime** guarantees consistency. Each layer does exactly one thing and never crosses boundaries, with PostgreSQL as the single source of truth.

```mermaid
flowchart TB
    Client["Frontend / future external channels"]

    subgraph Gateway["① Gateway + Adapter (thin translation)"]
        G["HTTP/SSE encode · auth · heartbeat keepalive"]
        A["Web Adapter: normalize requests from any channel into one Turn contract"]
    end

    subgraph Orchestrator["② TurnOrchestrator (lifecycle brain)"]
        O["create/resume Round · run lock · cancel token<br/>background producer: execution decoupled from connection"]
    end

    subgraph Runtime["③ In-process runtime (consistency)"]
        BUS["EventBus<br/>event persistence + replay/subscribe"]
        RCS["CompletionService<br/>single terminal entry"]
        CANCEL["CancelService<br/>precise cancellation"]
    end

    DB[("PostgreSQL · single source of truth")]

    Client --> Gateway
    Gateway --> Orchestrator
    Orchestrator --> Runtime
    Runtime --> DB
    Orchestrator -.->|execution independent of connection| Client
```

**What each layer buys you**

| Layer | Problem it solves | Benefit |
| --- | --- | --- |
| ① Gateway + Adapter | Protocol details (HTTP/SSE, auth, field formats) tangled with business logic | Business layer is transport-agnostic; adding a new channel (WeChat, Slack, etc.) means writing one adapter — zero changes to the orchestrator |
| ② TurnOrchestrator | Round creation, locks, cancellation, background tasks scattered across routes | Lifecycle is centralized; **execution is decoupled from the SSE connection** — the run keeps going when the browser disconnects, and reconnects resume losslessly |
| ③ Runtime trio | Event interleaving, duplicate terminal commits, mis-targeted cancels under concurrency | EventBus guarantees replay consistency; CompletionService makes terminals single-entry; CancelService hits exactly the current run and never kills a new one |

> 📐 Detailed contracts and failure modes at [docs/specs/chat-spec.md](docs/specs/chat-spec.md)

### Project Structure

```
OpenCapyBox/
├── src/
│   ├── agent/                    # Agent core engine
│   │   ├── agent.py              # Main loop (token cache, context summary, event gen)
│   │   ├── event_emitter.py      # AG-UI event emitter
│   │   ├── llm/                  # LLM clients (Anthropic / OpenAI protocols)
│   │   ├── tools/                # Tool implementations (sandbox file/shell/memory/search/cron/MCP)
│   │   ├── skills/               # 40+ loadable skills (git submodule)
│   │   └── schema/               # Data models & AG-UI event definitions
│   │
│   └── api/                      # FastAPI backend
│       ├── main.py               # App entry point
│       ├── config.py             # pydantic-settings configuration
│       ├── routes/               # API routes (auth/chat/sessions/models/cron/config)
│       ├── services/             # Business logic (agent/sandbox/history/memory/cron)
│       ├── models/               # SQLAlchemy ORM models
│       └── schemas/              # Pydantic request/response models
│
├── frontend/                     # React frontend
│   ├── src/
│   │   ├── components/           # UI components (ChatV2/SessionList/ArtifactsPanel/...)
│   │   ├── services/             # API clients
│   │   ├── utils/                # Message parsing/content chunking/file handling
│   │   └── types/                # TypeScript types
│   └── (design system moved to docs/specs/frontend-spec.md)
│
├── tests/                        # Python tests (30+ test files)
├── docs/                         # Project documentation
├── deploy/                       # Docker + deployment scripts
├── models.yaml                   # LLM model registry
├── pyproject.toml                # Python project configuration
└── .env.example                  # Environment variable template
```

## 🎒 Skill System

### Official Skill Library

Skills follow the Agent Skills Spec — each Skill is a standalone folder containing a `SKILL.md`. Users can enable/disable skills via the frontend **Skill Manager**:

| Category | Example Skills | Description |
|----------|---------------|-------------|
| 📄 Documents | docx, pdf, xlsx, pptx, nano-pdf | Document parsing and generation |
| 💻 Coding | coding-agent, git, github, playwright | Coding assistant and version control |
| 🎨 Design | canvas, frontend-design, tailwind-design-system | UI/UX design assistance |
| 🧠 Meta | skill-creator, self-improving, reflection, memory | Self-evolution and reflection |
| 🔍 Other | oracle, brainstorming, proactive-agent, session-logs | Toolbox |

### Custom Skills

> 🚧 Coming soon: Install custom Skills by uploading ZIP packages via the frontend

Currently, you can register new skills by placing Skill folders in the `src/agent/skills/` directory.

## 🧠 Memory System

OpenCapyBox's layered memory makes your AI assistant understand you better over time:

```
┌─────────────────────────────────────────────┐
│  SOUL.md    — Who am I? (Personality)       │
├─────────────────────────────────────────────┤
│  USER.md    — Who are you? (User Profile)   │
├─────────────────────────────────────────────┤
│  MEMORY.md  — What have we discussed?       │
│               (Long-term Memory)            │
├─────────────────────────────────────────────┤
│  AGENTS.md  — Platform behavior template    │
└─────────────────────────────────────────────┘
```

**Retrieval**: BM25 keyword + Embedding vector + RRF fusion + time decay. Automatically falls back to keyword-only search when Embedding is not configured.

The frontend **Agent Config Panel** can edit SOUL.md / USER.md / MEMORY.md. AGENTS.md is managed by the platform template and is not exposed as a user-editable config file.

## 🚢 Deployment Guide

### Nginx Reverse Proxy

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        root /var/www/opencapybox/frontend/dist;
        try_files $uri $uri/ /index.html;
    }

    location /api {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;              # Required for SSE
    }
}
```

### Environment Variables Reference

<details>
<summary>Click to expand full environment variable list</summary>

```bash
# === Required ===
LLM_API_KEY=                           # Alibaba DashScope unified key
SIMPLE_AUTH_USERS=demo:demo123         # Auth users

# === Optional: LLM ===
MINIMAX_API_KEY=                       # MiniMax dedicated key
EMBEDDING_API_KEY=                     # Embedding key (falls back to BM25 if empty)

# === Optional: Tools ===
BOCHA_SEARCH_APPCODE=                  # Bocha search AppCode

# === OpenSandbox ===
SANDBOX_DOMAIN=localhost:8080
SANDBOX_API_KEY=
SANDBOX_IMAGE=code-interpreter-agent:v1.1.0
SANDBOX_PROTOCOL=http
SANDBOX_TIMEOUT_MINUTES=60
SANDBOX_PERSISTENT_STORAGE_ENABLED=true

# === Application ===
DEBUG=false
CORS_ORIGINS=["http://localhost:3000"]
DATABASE_URL=postgresql://user:password@host:5432/opencapybox
# PostgreSQL must have pgvector installed: CREATE EXTENSION IF NOT EXISTS vector;
# Optional pytest integration database, never point this at production:
TEST_DATABASE_URL=postgresql://user:password@host:5432/opencapybox_test
AUTH_SECRET_KEY=<32+ random characters> # Required in production
MCP_SECRET_KEY=<different 32+ random characters> # Required in production and must differ from AUTH_SECRET_KEY
AUTH_TOKEN_EXPIRE_MINUTES=720

# === Agent ===
UVICORN_WORKERS=1
AGENT_MAX_STEPS=100
AGENT_TOKEN_LIMIT=200000
AGENT_USER_CONCURRENCY_LIMIT=1

# === SSE ===
SSE_HEARTBEAT_INTERVAL=15
SSE_SUBSCRIBE_TIMEOUT=300

# === Embedding ===
EMBEDDING_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
```

</details>

## 📖 Development Guide

### Spec-Driven Documentation

Each module has a standalone spec covering data models, API contracts, behavior semantics, failure modes, and observability:

| Spec | Scope |
|---|---|
| [auth-spec.md](docs/specs/auth-spec.md) | Authentication |
| [sessions-spec.md](docs/specs/sessions-spec.md) | Session management |
| [chat-spec.md](docs/specs/chat-spec.md) | Chat / Agent execution / SSE streaming |
| [cron-spec.md](docs/specs/cron-spec.md) | Cron scheduled tasks |
| [memory-spec.md](docs/specs/memory-spec.md) | Layered memory system |
| [sandbox-spec.md](docs/specs/sandbox-spec.md) | Sandbox interaction |
| [models-spec.md](docs/specs/models-spec.md) | Model registry & switching |
| [config-spec.md](docs/specs/config-spec.md) | Agent config & skills |
| [subagent-run-graph-spec.md](docs/specs/subagent-run-graph-spec.md) | Subagent run graph |
| [frontend-spec.md](docs/specs/frontend-spec.md) | Frontend overview & design system |
| [frontend-chat-spec.md](docs/specs/frontend-chat-spec.md) | Frontend chat / SSE / reasoning panel |
| [frontend-session-spec.md](docs/specs/frontend-session-spec.md) | Frontend session list & switching |
| [frontend-panel-spec.md](docs/specs/frontend-panel-spec.md) | Frontend drawer panels |

AG-UI protocol details at [docs/Capy-project-md/ag-ui-md/](docs/Capy-project-md/ag-ui-md/).

### Running Tests

```bash
# Python backend tests
uv run pytest tests/ -v

# With coverage
uv run pytest tests/ -v --cov=src

# Frontend tests
cd frontend && npm run test
```

### Adding New Tools

1. Create a tool class in `src/agent/tools/`, inheriting from the `Tool` base class
2. Register it in `create_agent_tools()` in `src/api/services/tool_factory.py`
3. Write tests in `tests/`
4. Update the relevant spec in `docs/specs/`

```python
from src.agent.tools.base import Tool, ToolResult

class MyTool(Tool):
    @property
    def name(self) -> str:
        return "my_tool"

    @property
    def description(self) -> str:
        return "Tool description"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"param": {"type": "string"}},
            "required": ["param"]
        }

    async def execute(self, param: str) -> ToolResult:
        return ToolResult(success=True, content="Result")
```

### Commit Convention

```
<type>(<scope>): <description>

feat(agent): add new search tool
fix(frontend): fix message scroll jitter
docs(api): update Cron API documentation
```

## 🤝 Contributing

All forms of contribution are welcome!

1. Fork this repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'feat: add amazing feature'`
4. Push the branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

**Contribution areas**: Bug fixes · New tools/skills · New model adapters · UI improvements · Documentation · Performance optimization · i18n

## 📄 License

This project is licensed under the [Apache License 2.0](LICENSE).

## 🙏 Acknowledgments

- [FastAPI](https://fastapi.tiangolo.com/) — High-performance async web framework
- [React](https://react.dev/) — Modern frontend framework
- [OpenSandbox](https://github.com/alibaba/OpenSandbox) — Alibaba's open-source secure sandbox execution environment
- [Anthropic](https://www.anthropic.com/) / [OpenAI](https://openai.com/) — LLM API protocols
- [DashScope](https://dashscope.aliyuncs.com/) — Alibaba Cloud model service platform
- [TailwindCSS](https://tailwindcss.com/) — Utility-first CSS framework
- [Vite](https://vitejs.dev/) — Next-generation frontend build tool

## 🗺️ Roadmap

### ⚡ Performance & Scalability

- [ ] **Parallel subagent execution** — subagents run serially today; plan to schedule multiple child tasks concurrently
- [ ] **Multi-worker deployment + Redis sync** — introduce Redis for cross-worker event fanout / cancel registry / run locks / sandbox-profile cache invalidation, lifting the single-worker assumption
- [ ] **Cross-profile data migration** — design export/import, validation, rollback, and audit for moving sandbox files, cron artifacts, user-generated files, and third-party skills between OpenSandbox backends
- [ ] Pluggable EventBus: upgrade the in-process bus to a backend choice (in-process / Redis Pub-Sub)
- [ ] Caching layer for session & memory retrieval

### 🔌 Channels & Integrations

- [ ] External channel adapters (WeChat / DingTalk / Slack, etc.) — reusing the TurnOrchestrator layer
- [ ] WebSocket bidirectional communication
- [ ] More model providers (Gemini, Claude direct, etc.)

### 🎒 Skills & Ecosystem

- [ ] Skill ZIP package upload & install
- [ ] Skill marketplace
- [ ] Agent workflow orchestration

### 👥 Collaboration & Multi-tenancy

- [ ] Multi-tenant permission system
- [ ] Session sharing & collaboration
- [ ] Multi-language UI

---

<div align="center">

**If OpenCapyBox helps you, please give it a ⭐**

*Like a capybara — calm, friendly, and surprisingly capable.* 🛁

[Report Bug](https://github.com/RonaldJEN/OpenCapyBox/issues) · [Feature Request](https://github.com/RonaldJEN/OpenCapyBox/issues) · [Discussions](https://github.com/RonaldJEN/OpenCapyBox/discussions)

</div>
