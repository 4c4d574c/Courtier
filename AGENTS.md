# AGENTS.md — Courtier Workspace

This file is the entry point for AI coding agents working in this repository. It describes the overall workspace, the two projects it contains, how to build and test them, and the conventions that keep the codebase consistent.

> **Scope note:** The working directory `/home/lmwl/Documents/docaudit/agent` is primarily the **Courtier** project. A separate, nested git repository named `pi/` also lives here. It is not a submodule and is tracked independently. See the `pi/` section below.

---

## 1. Project Overview

### 1.1 Courtier (primary project)

Courtier is a general-purpose AI agent platform with a pluggable domain-package architecture. The core engine is domain-agnostic; domain capabilities are added as self-contained packages that provide plugins, skills, rules, and localized prompts.

The first (and currently only) domain package is **docaudit**, which audits Chinese government documents against GB/T 9704-2012.

Key capabilities:

- FastAPI backend with Server-Sent Events (SSE) streaming.
- Agent runtime using a Think → Act → Observe loop.
- Plugin system based on JSON-RPC 2.0 over stdio (subprocess isolation).
- Skill system: Markdown documents with YAML frontmatter that define SubAgent workflows.
- Prompt engine using Jinja2 + YAML PromptBundles (zero hardcoded NL text in core).
- Vue 3 + Vite terminal-style web frontend.

### 1.2 Pi (nested independent project)

`pi/` is the **Pi agent harness**, a TypeScript/Node monorepo maintained at `https://github.com/earendil-works/pi`. It is present in this workspace as a reference and potential migration target (see `courtier/docs/architecture/pi-architecture-migration-plan.md`).

`pi/` is a separate git repository. Do not treat it as part of the Courtier commit history. It has its own `AGENTS.md`, `package.json`, and conventions.

---

## 2. Repository Layout

```
/home/lmwl/Documents/docaudit/agent/
├── AGENTS.md                 # This file
├── CLAUDE.md                 # Repo-wide guidance (Courtier-focused)
├── .gitignore                # Root ignores: .env_bak, .worktrees/
├── docs/                     # Courtier design specs and implementation plans
├── courtier/                 # Courtier backend + frontend + infrastructure
│   ├── pyproject.toml        # uv project manifest (Python 3.12+)
│   ├── uv.lock               # Locked Python dependencies
│   ├── main.py               # Uvicorn entry point + migration runner
│   ├── alembic.ini           # Database migration config
│   ├── docker-compose.yml    # MySQL, MinIO, ES, observability stack
│   ├── Dockerfile            # Production image (Node frontend + Python backend)
│   ├── .env.example          # Environment variable template
│   ├── courtier/             # Domain-agnostic agent engine
│   ├── domains/docaudit/     # docaudit domain package
│   ├── libs/shared/          # Cross-domain libraries (docannot, docmodels, plugin_sdk)
│   ├── libs/docaudit/        # docaudit-specific libraries (docparse, validator, content_compliance, doccorrector)
│   ├── plugins/shared/       # Cross-domain JSON-RPC plugins
│   ├── plugins/docaudit/     # docaudit JSON-RPC plugins
│   ├── webui/                # Vue 3 + Vite frontend
│   ├── tests/                # pytest suite
│   └── docs/                 # Courtier developer docs
└── pi/                       # Independent Pi agent harness monorepo
    ├── AGENTS.md             # Pi-specific agent rules
    ├── package.json          # npm workspaces root
    ├── biome.json            # Formatter/linter config
    ├── packages/ai/          # Unified multi-provider LLM API
    ├── packages/agent/       # Agent runtime
    ├── packages/coding-agent/# Interactive coding agent CLI
    ├── packages/tui/         # Terminal UI library
    └── packages/orchestrator/# Experimental orchestrator
```

---

## 3. Technology Stack

### Courtier

| Layer | Technology |
|-------|------------|
| Language | Python 3.12+ |
| Package/venv manager | uv |
| Web framework | FastAPI |
| Server | Uvicorn (factory: `courtier.agent.api.app:create_app`) |
| Database | MySQL 8.x + SQLAlchemy 2 async ORM |
| Migrations | Alembic |
| Search | Elasticsearch 8.x |
| Object storage | MinIO (S3-compatible) |
| LLM client | OpenAI-compatible API (configurable endpoint) |
| Observability | OpenTelemetry, Prometheus, Grafana, Langfuse |
| Frontend | Vue 3 + Vite + TypeScript |
| Frontend package manager | npm |
| Container runtime | Docker / Docker Compose |

### Pi

| Layer | Technology |
|-------|------------|
| Language | TypeScript 5.9+ |
| Runtime | Node.js >=22.19.0 (also supports Bun for binaries) |
| Package manager | npm (workspaces) |
| Build tool | `tsgo` (TypeScript native-preview compiler) |
| Linter/formatter | Biome |
| Test runner | Vitest + Node test runner (TUI package) |
| CLI | `@earendil-works/pi-coding-agent` (`pi`) |

---

## 4. Build, Run, and Test Commands

All commands assume you start in the project root (`/home/lmwl/Documents/docaudit/agent`).

### 4.1 Courtier backend

```bash
cd courtier

# Install/sync dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with real credentials for MySQL, MinIO, Elasticsearch, and LLM.

# Run database migrations
uv run alembic upgrade head

# Start the API server
uv run main.py

# Custom host/port or hot reload
uv run main.py --host 127.0.0.1 --port 8080 --reload

# Validate the docaudit domain package
uv run courtier validate-domain domains/docaudit/
```

> **Note on PYTHONPATH:** libs/, docmodels and the plugin SDK are installed into the venv as editable packages (see `[tool.uv.sources]` in `courtier/pyproject.toml`), so no manual `PYTHONPATH` is needed for `uv run` commands. Only direct `python` invocations outside `uv run` may still need it.

### 4.2 Courtier frontend (`courtier/webui/`)

```bash
cd courtier/webui

# Install dependencies
npm ci

# Start dev server (Vite, port 5173; proxies /api to localhost:8000)
npm run dev

# Type-check and build
npm run build

# Preview production build
npm run preview

# Run frontend tests (custom Node assertion scripts)
npm test
```

### 4.3 Courtier tests

```bash
cd courtier

# Run all tests
uv run pytest

# Run all tests excluding integration tests
uv run pytest -m "not integration"

# Run a single test file
uv run pytest tests/agent/test_loop.py -v

# Run a single test
uv run pytest tests/agent/test_loop.py::test_some_name -v

# With coverage
uv run pytest --cov=courtier --cov-report=term-missing
```

### 4.4 Courtier infrastructure

```bash
cd courtier

# Start MySQL, MinIO, Elasticsearch, and observability stack
# (the main app container is commented out by default)
docker-compose up -d

# Endpoints when running locally
# - Grafana:     http://localhost:3001
# - Langfuse:    http://localhost:3000
# - Prometheus:  http://localhost:9090
# - API docs:    http://localhost:8000/docs
```

### 4.5 Pi (nested project)

```bash
cd pi

# Install dependencies (lifecycle scripts are intentionally skipped)
npm install --ignore-scripts

# Build all packages
npm run build

# Lint, format, type-check, and run project checks
npm run check

# Run tests (non-e2e; use ./test.sh at the root)
./test.sh

# Run pi from sources
./pi-test.sh
```

> **Node version:** Pi requires Node.js >=22.19.0. Older Node versions will fail during build/test.

---

## 5. Architecture

### 5.1 Courtier backend layers

1. **API layer** (`courtier/agent/api/`)
   - `routes/` — HTTP handlers for sessions, files, auth, users, control, profile.
   - `sse_adapter.py` — Converts internal events to SSE chunks.
   - `file_store.py`, `session_store.py` — Persistence helpers.
   - `middleware/observability.py` — OpenTelemetry/Prometheus middleware.

2. **Service layer** (`courtier/agent/api/services/`)
   - `AgentService` — unified `build_agent` (single orchestrator builder for chat / uploaded-document / continuation sessions), `StreamService`, `FileService`, `SessionService`.

3. **Core engine layer** (`courtier/agent/`)
   - `core/` — Agent loop (`loop.py`), state (`state.py`), model client (`model.py`), context manager, guard logic, streaming, and the new event bus (`event_bus.py`, `events.py`).
   - `agents/` — Orchestrator (`orch.py`) and SubAgent runtime.
   - `runtime/` — `AgentRuntime` (sub-agent lifecycle) and `activation.py` (`DomainActivator`, per-session domain self-activation).
   - `tools/` — Tool registry, protocol, built-in tools (`activate_domain` meta-tool, artifact tools, echo, skill) and domain tools.
   - `skills/` — Skill registry and loader.
   - `artifacts/` — Artifact system.
   - `prompts/` — Jinja2 PromptEngine, PromptBundle, and core default templates (`defaults/{locale}/`, incl. the domain-agnostic `orchestrator.system_prompt`).
   - `permissions/`, `hooks/`, `memory/`, `telemetry/` — Cross-cutting concerns.

4. **Domain/business layer**
   - `domains/docaudit/` — Domain config, prompts, skills (Markdown + typed input schemas).
   - `libs/shared/` — Cross-domain installable libraries (`docannot`, `docmodels`, `plugin_sdk`).
   - `libs/docaudit/` — Domain-specific installable libraries (`docparse`, `validator`, `content_compliance`, `doccorrector`).
   - `plugins/shared/` — Cross-domain JSON-RPC plugins (`anydoc`, `search`, `annotate`, `template`).
   - `plugins/docaudit/` — Domain JSON-RPC plugins (`parse`; `format_audit`, `content_audit`, `plagiarism` under `audit/`).

### 5.2 Plugin vs Skill vs Library

| Concept | What it is | Where it lives | How it is invoked |
|---------|-----------|----------------|-------------------|
| **Plugin** | Atomic tool running in an isolated subprocess via JSON-RPC 2.0 over stdio | `plugins/shared/`, `plugins/<domain>/` | Registered in `ToolRegistry`; called by the agent loop |
| **Skill** | Task workflow defined as Markdown + YAML frontmatter | `domains/<domain>/skills/*.md` | Loaded by `load_skill(skill=..., task=...)` to create a generic `Agent` |
| **Library** | Code dependency bundled at build time | `libs/shared/`, `libs/<domain>/` | Imported by plugins |

### 5.3 Agent runtime

The runtime follows a Think → Act → Observe loop. During execution it emits events such as `session`, `think`, `act`, `observe`, `tool_result`, `token`, `usage`, and `complete`/`error`. These events are currently produced by callbacks in `loop.py` and are being migrated to an in-process `EventBus` (`courtier/agent/core/event_bus.py`, `events.py`). The `SSEAdapter` remains the consumer that forwards events to the web frontend.

**Unified session mode + domain gating:** all sessions (chat-only, uploaded document, continuation) run the same `OrchestratorAgent` built by `build_agent`. The orchestrator starts with only the shared plugin tools (`plugins/shared/`) visible; domain tools/skills are self-activated at runtime through the `activate_domain` meta-tool (`DomainActivator`), which registers the domain's SkillTools on the agent, injects its plugin proxies, and overlays the domain's `orchestrator.workflow_rules` onto the prompt pipeline. The active-domain set is persisted in the `SessionRecord` and replayed on per-request agent rebuilds. Document data-fetching: format audit → `parse_document`; content tasks / read-and-answer → `convert_document` Markdown (scanned PDFs and images fall back to OCR), reusing existing session text when present.

### 5.4 Pi architecture (nested reference project)

Pi is a separate agent harness built around:

- `packages/ai` — unified multi-provider LLM API.
- `packages/agent` — transport abstraction, state management, attachments.
- `packages/coding-agent` — interactive CLI coding agent with built-in tools.
- `packages/tui` — differential-rendering terminal UI library.
- `packages/orchestrator` — experimental orchestration layer.

Courtier has an architecture migration plan that maps Pi concepts (event bus, model backend, state machine, capability registry, memory hierarchy, conversation tree, guardrails) to Courtier components. See `courtier/docs/architecture/pi-architecture-migration-plan.md`.

---

## 6. Code Organization and Module Divisions

### Courtier Python source

```
courtier/courtier/
├── agent/
│   ├── agents/          # OrchestratorAgent, SubAgent runtime
│   ├── api/             # FastAPI app, routes, services, SSE, stores
│   ├── artifacts/       # Artifact registry, store, models
│   ├── core/            # Loop, state, model, context, guards, streaming, event bus
│   ├── hooks/           # Hook system
│   ├── memory/          # Memory abstractions
│   ├── permissions/     # Permission checks
│   ├── prompts/         # PromptEngine, PromptBundle, defaults/{locale}/ (core default templates)
│   ├── runtime/         # Agent runtime bridge
│   ├── skills/          # Skill registry/loader
│   ├── telemetry/       # OpenTelemetry tracer
│   └── tools/           # Tool registry, protocol, builtin/domain tools
├── cli/                 # CLI entry point (courtier validate-domain)
├── common/              # Shared utilities
├── config.py            # Settings and CourtierConfig
├── db/                  # SQLAlchemy models, CRUD, async DB helpers
├── domain/              # DomainPackage discovery and validation
├── es/                  # Elasticsearch client
├── plugin/              # PluginSystem, PluginRuntime, protocol types
└── storage/             # MinIO client
```

### Courtier domain package (docaudit)

```
courtier/domains/docaudit/
├── config/
│   ├── domain.yaml           # Domain metadata
│   └── prompts/{locale}/     # Domain-specific Jinja2 templates only (orchestrator.*, chat.system_prompt) + optional overrides
└── skills/                   # Skill Markdown files + schemas/ (typed sub-agent input models)
```

> Shared data models live in `libs/shared/docmodels/` (an installable package, not in the domain dir).
> Format/compliance rule JSON files ship inside the libraries that consume them:
> `libs/docaudit/validator/validator/rules/` and `libs/docaudit/content_compliance/content_compliance/rules/`.

### Courtier frontend

```
courtier/webui/
├── src/
│   ├── api/             # API client
│   ├── components/      # Vue components
│   ├── composables/     # useAgentSession, useAuth, useHistory, etc.
│   ├── constants/       # App constants
│   ├── directives/      # Custom directives
│   ├── router/          # vue-router config
│   ├── styles/          # Theme/styles
│   ├── types/           # TypeScript types
│   ├── utils/           # toolCalls.ts, session utils, markdown streaming
│   ├── views/           # Page-level views
│   ├── App.vue
│   └── main.ts
├── index.html
├── package.json
└── vite.config.ts
```

### Pi monorepo

```
pi/
├── packages/ai/src            # LLM providers, model discovery
├── packages/agent/src         # Agent runtime
├── packages/coding-agent/src  # CLI, modes, tools, export
├── packages/tui/src           # Terminal UI primitives
└── packages/orchestrator/src  # Experimental orchestrator
```

---

## 7. Development Conventions

### 7.1 Courtier

- **Python version:** 3.12+.
- **Package manager:** `uv` only. Do not use `pip` directly.
- **Formatting:** Black, line length 100.
- **Import sorting:** isort with Black profile, `known_first_party = ["courtier"]`.
- **Linting:** Ruff (`E`, `F`, `I`, `W`), target Python 3.12.
- **Type checking:** mypy (dev dependency).
- **Commits:** Conventional commits (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`).
- **Domain isolation:** Core must never import domain code. Domains are discovered at startup via `CourtierConfig`.
- **No hardcoded NL text in core:** All natural language is rendered by the Jinja2 PromptEngine from YAML templates. Domain-agnostic text (errors, context compaction, behavioral rules, tool invocation, subagent, welcome message) ships with core as full localized defaults in `courtier/prompts/defaults/{locale}/`; domain packages carry only domain-specific templates (`orchestrator.*`, `chat.system_prompt`) in `domains/<domain>/config/prompts/{locale}/` and may override any core default per key. Minimal English `FALLBACK_TEMPLATES` in `engine.py` are the last resort when a key is missing everywhere.
- **Plugins are subprocesses:** Each plugin has its own `pyproject.toml`, virtual environment, and `plugin.yaml` manifest.
- **Libraries are installable packages:** Every lib under `libs/shared/` and `libs/<domain>/` has its own `pyproject.toml` and is installed editable via `[tool.uv.sources]`. Plugins depend only on `courtier-plugin-sdk` + the libs they use — never on the `courtier` application package. The plugin SDK lives at `libs/shared/plugin_sdk/` (import name `courtier_plugin_sdk`).
- **Plugin data access via host services:** Plugins never hold DB/MinIO credentials. Data owned by the host (format templates, artifacts, object storage) is accessed through declared host services (`plugin.yaml` `dependencies.host_services` + `permissions`) over reverse JSON-RPC. Plugin-side endpoint config (LLM/CEC/ES) is injected via `plugin.yaml` `runtime.env` `${ENV:VAR}` passthroughs.

### 7.2 Pi

See `pi/AGENTS.md` for the full rule set. Key points:

- Node >=22.19.0.
- Top-level imports only; no inline/dynamic type imports.
- Use only erasable TypeScript syntax in checked code (no parameter properties, `enum`, `namespace`, `import =`, `export =`).
- External dependencies are pinned to exact versions.
- Run `npm run check` after code changes (not docs).
- Do not run the full vitest suite directly; use `./test.sh` for non-e2e tests.
- Commit only files you changed in the current session; never `git add -A`.

---

## 8. Testing Strategy

### Courtier

- **Unit tests:** `tests/agent/`, `tests/courtier/`, `tests/plugin/`, `tests/telemetry/`.
- **Domain tests:** `tests/domains/docaudit/`.
- **Integration tests:** Marked with `pytest.mark.integration` and excluded by default with `-m "not integration"`.
- **Frontend tests:** Custom Node scripts in `courtier/webui/scripts/` assert tool-call normalization, subagent tree rendering, session event handling, streaming markdown, etc.
- **Coverage:** Configured in `pyproject.toml` for `courtier`, `domains/docaudit`, `libs/shared`, `libs/docaudit`, `plugins`.

### Pi

- Package-level Vitest suites in `packages/*/test/`.
- `./test.sh` runs non-e2e tests; e2e tests activate only when endpoint/auth env vars are present.
- The coding-agent package uses a faux provider harness for suite tests; no real API keys.

---

## 9. Deployment and Operations

### Courtier

- **Production image:** `courtier/Dockerfile` builds the frontend and copies `dist/` to `/app/static`, then installs the Python backend with `uv sync --frozen --no-dev --no-editable`.
- **Entry point:** `uv run python -m uvicorn courtier.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000`.
- **Docker Compose:** `docker-compose.yml` provides MySQL, MinIO, Elasticsearch, Langfuse (web + worker), Prometheus, Grafana, and an OTLP collector. The `app` service is commented out by default. Tracing flows: app → OTLP collector (`:4317`) → Langfuse (`/api/public/otel`, authenticated via `LANGFUSE_AUTH_HEADER` in `.env`); Langfuse stores raw trace payloads in MinIO (`langfuse-events` bucket) and the ingestion queue in Redis (password-protected via `LANGFUSE_REDIS_PASSWORD`). Third-party images use the Huawei Cloud mirror (`swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/...`) where Docker Hub is slow.
- **Migrations:** Alembic runs automatically when starting via `main.py`.
- **Environment:** Key variables include `COURTIER_REPO_ROOT`, `COURTIER_DOMAIN_PACKAGES`, `COURTIER_LOCALE`, `COURTIER_UPLOAD_DIR`, `MYSQL_URL`, `ES_HOSTS`, `MINIO_ENDPOINT`, LLM endpoints, and OpenTelemetry settings.
- **Health check:** `GET /health`.
- **Metrics:** `GET /metrics` (Prometheus).

### Pi

- Published as npm packages under `@earendil-works/`.
- CI publishes via GitHub Actions OIDC trusted publishing.
- Release scripts (`npm run release:patch` / `release:minor`) bump lockstep versions, update changelogs, regenerate artifacts, and push a tag.
- See `pi/AGENTS.md` for detailed release and supply-chain rules.

---

## 10. Security Considerations

### Courtier

- **Authentication:** JWT-based auth with bcrypt password hashing. Default admin user is bootstrapped from `ADMIN_USER` / `ADMIN_PASSWORD` in `.env`.
- **Rate limiting:** `slowapi` rate limiter is installed on API routes.
- **CORS:** Configurable via `CORS_ORIGINS`; defaults are permissive for local development only.
- **Secrets:** `.env` contains credentials and API keys. It is gitignored; use `.env.example` as a template.
- **File uploads:** Stored under `COURTIER_UPLOAD_DIR` (default `./uploads`). Plugins access uploads through the configured upload path.
- **Plugin isolation:** Plugins run as separate subprocesses, but they execute with the same OS permissions as the host process. Sandboxing is not enforced by the plugin system itself.
- **Domain gating:** domain tools are visibility-filtered per session (see §5.3); activation state is persisted in `SessionRecord.active_domains`.
- **LLM endpoints:** Configurable via environment variables; never hardcode API keys.

### Pi

- Pi runs with the permissions of the launching user/process.
- Containerization/sandboxing patterns are documented in `pi/packages/coding-agent/docs/containerization.md` (Gondolin, Docker, OpenShell).
- Supply-chain hardening: pinned direct deps, lockfile ground truth, shrinkwrap for published CLI, `--ignore-scripts` by default.

---

## 11. Notes for AI Agents

- **Two projects, two contexts:** If you are asked to work on Courtier, stay in `courtier/` and use the root `CLAUDE.md` and this file. If you are asked to work on Pi, `cd pi/` and follow `pi/AGENTS.md`.
- **Do not mix commits:** `pi/` is an independent git repository. Never stage `pi/` files from the Courtier root with `git add -A`.
- **Run the right test command:**
  - Courtier Python: `uv run pytest` (or `uv run pytest -m "not integration"`).
  - Courtier frontend: `npm test` inside `courtier/webui/`.
  - Pi: `./test.sh` from `pi/`.
- **Check environment versions:** Courtier needs Python 3.12+ and `uv`. Pi needs Node >=22.19.0.
- **After Courtier code changes:** The local `courtier/CLAUDE.md` requires committing after every change with conventional commits. Review that file for the exact rule.
- **Documentation drift:** If you change build commands, architecture, or conventions, update `AGENTS.md` (root), `CLAUDE.md` (root), and `courtier/CLAUDE.md` as appropriate.
- **Migration in progress:** Courtier is incrementally adopting concepts from Pi (event bus, state machine, model backend abstraction, capability registry, memory hierarchy, conversation tree, guardrails). See `courtier/docs/architecture/pi-architecture-migration-plan.md` before modifying core agent loop or event/SSE code.
