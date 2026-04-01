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

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
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

Define Cron jobs via `HEARTBEAT.md`, and your AI assistant runs them autonomously:

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
| ⏰ Cron | ManageCron | APScheduler-powered cron management |
| 🎒 Skills | GetSkill | 40+ dynamically loadable professional skills |
| 🔌 MCP | MCP Tools | Model Context Protocol tool integration |

### 🔌 MCP Tool Integration

OpenCapyBox supports external tool services via [MCP (Model Context Protocol)](https://modelcontextprotocol.io/). Configuration file at `src/agent/config/mcp.json`:

```json
{
  "mcpServers": {
    "my-mcp-server": {
      "description": "My MCP Server",
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@example/mcp-server"],
      "env": { "API_KEY": "your-key" },
      "disabled": false
    }
  }
}
```

- `type` supports `stdio` (local process) and `streamable-http` (remote HTTP)
- Set `"disabled": true` to temporarily disable an MCP service
- Use `MCP_CONFIG_PATH` env variable to customize the config file path

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

### File Panel

Browse sandbox files, preview and download Agent-generated artifacts.

![File Panel](docs/Capy-project-md/screenshots/文件面板.png)

### File Preview

Markdown rendered preview with source view and one-click download.

![File Preview](docs/Capy-project-md/screenshots/文件预览.png)

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
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

# === OpenSandbox (optional) ===
SANDBOX_DOMAIN=localhost:8080
SANDBOX_API_KEY=your-sandbox-key

# === Others (all have defaults) ===
# DATABASE_URL=sqlite:///./data/database/open_capy_box.db
# AGENT_MAX_STEPS=100
# AGENT_TOKEN_LIMIT=200000
```

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
│  Backend — FastAPI + SQLAlchemy + SQLite                         │
│  JWT Auth · Agent Pool · Memory Service · Cron Scheduler · SSE  │
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
│   └── DESIGN_SYSTEM.md          # Design system documentation
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
│  AGENTS.md  — Team collaboration rules      │
├─────────────────────────────────────────────┤
│  HEARTBEAT.md — What to do on schedule?     │
└─────────────────────────────────────────────┘
```

**Retrieval**: BM25 keyword + Embedding vector + RRF fusion + time decay. Automatically falls back to keyword-only search when Embedding is not configured.

All config files can be edited directly in the frontend **Agent Config Panel**.

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
DATABASE_URL=sqlite:///./data/database/open_capy_box.db
AUTH_SECRET_KEY=                        # Auto-derived if not set
AUTH_TOKEN_EXPIRE_MINUTES=720

# === Agent ===
AGENT_MAX_STEPS=100
AGENT_TOKEN_LIMIT=200000

# === SSE ===
SSE_HEARTBEAT_INTERVAL=15
SSE_SUBSCRIBE_TIMEOUT=300

# === Embedding ===
EMBEDDING_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
```

</details>

## 📖 Development Guide

### API Documentation

Full backend API docs at [docs/Capy-project-md/api.md](docs/Capy-project-md/api.md), covering all route request/response formats, AG-UI event type definitions, and data models. Frontend API reference at [docs/Capy-project-md/frontend.md](docs/Capy-project-md/frontend.md).

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
2. Register it in `_create_tools()` in `src/api/services/agent_service.py`
3. Write tests in `tests/`

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

- [ ] Skill ZIP package upload & install
- [ ] Multi-tenant permission system
- [ ] Session sharing & collaboration
- [ ] Skill marketplace
- [ ] WebSocket bidirectional communication
- [ ] Multi-language UI
- [ ] Agent workflow orchestration
- [ ] More model providers (Gemini, Claude direct, etc.)

---

<div align="center">

**If OpenCapyBox helps you, please give it a ⭐**

*Like a capybara — calm, friendly, and surprisingly capable.* 🛁

[Report Bug](https://github.com/RonaldJEN/OpenCapyBox/issues) · [Feature Request](https://github.com/RonaldJEN/OpenCapyBox/issues) · [Discussions](https://github.com/RonaldJEN/OpenCapyBox/discussions)

</div>
