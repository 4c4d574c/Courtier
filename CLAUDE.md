# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Layout

This is a monorepo for **Courtier**, a general-purpose AI agent platform with pluggable domain packages. The first official domain package, **docaudit**, provides Chinese government document auditing.

- `courtier/` — Python FastAPI backend (the main application).
- `webui/` — Vue 3 + Vite terminal-style frontend that talks to the backend via SSE.

Each subdirectory already has its own `CLAUDE.md` with more detailed local rules; this file covers repo-wide conventions and the "big picture".

## Common Development Commands

### Backend (`courtier/`)

Python 3.12+ is required. The project uses `uv` for dependency and virtual-environment management.

```bash
cd courtier

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with real MySQL/MinIO/Elasticsearch/LLM credentials.

# Run the API server
uv run main.py

# Or start uvicorn directly (libs/docmodels/SDK are editable venv packages;
# no manual PYTHONPATH needed)
uv run python -m uvicorn courtier.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

The FastAPI app is created by the factory `create_app()` in `courtier/agent/api/app.py`; `main.py` additionally runs Alembic migrations before starting uvicorn.

```bash
# Run all tests
uv run pytest

# Run a single test file
uv run pytest tests/agent/test_loop.py -v

# Run a single test
uv run pytest tests/agent/test_loop.py::test_some_name -v

# Exclude integration tests (tests that hit real databases/services)
uv run pytest -m "not integration"
```

Database migrations use Alembic:

```bash
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "describe change"
```

Docker Compose currently enables only the observability stack (OpenTelemetry Collector, Langfuse, Prometheus, Grafana). The main app, MySQL, Elasticsearch, MinIO, and PaddleOCR services are commented out:

```bash
# Observability only
docker-compose up -d
```

### Frontend (`tui/`)

```bash
cd tui

# Install dependencies
npm ci

# Start dev server (Vite, port 5173, proxies /api to localhost:8000)
npm run dev

# Type-check and build
npm run build

# Preview production build
npm run preview

# Run tool-call normalization tests
npm test
```

The test script (`scripts/test-tool-calls.mjs`) compiles `src/utils/toolCalls.ts` with `tsc` and runs assertions against the normalized tool-call/session models used by the UI.

## High-Level Architecture

### Backend

The backend is a FastAPI application organized into four layers:

1. **API layer** (`courtier/agent/api/`): thin HTTP handlers (`routes/`), SSE streaming (`sse_adapter.py`), file/session stores, and observability middleware.
2. **Service layer** (`courtier/agent/api/services/`): `AgentService`, `StreamService`, `FileService`, `SessionService`.
3. **Core engine layer** (`courtier/agent/`): Agent loop, PluginSystem, ToolRegistry, Artifact system, Context manager, Prompt pipeline, Hooks, Permissions, and Telemetry.
4. **Domain/business layer** (`libs/shared/` for reusable modules like `docannot`, `docmodels`; `libs/docaudit/` for domain-specific modules like `docparse`, `validator`, `content_compliance`, `doccorrector`).

Two extension mechanisms share the same `ToolRegistry`:

- **Plugins** (`plugins/`): isolated subprocesses communicating over JSON-RPC 2.0 on stdio. The host scans `plugins/shared/` (shared tools: `anydoc`, `search`, `annotate`, `template`) and `plugins/docaudit/` (domain tools: `parse_document` in `parse/`; `format_audit`, `content_audit`, `text_correction`, `plagiarism` in `audit/`). Plugins declare `type: tool` capabilities in `plugin.yaml`.

- **Skills** (`skills/`): Markdown documents defining SubAgent configurations. Each `skills/{name}.md` has YAML frontmatter (`tools`, `input_model`, etc.) and a natural-language body used as the sub-agent's system prompt. OrchestratorAgent calls `load_skill(skill=..., task=...)` to create a generic `Agent` that executes the Skill's workflow.

The agent runtime follows a Think → Act → Observe loop. Sessions stream events (`session`, `think`, `act`, `observe`, `tool_result`, `token`, `usage`, `complete`/`error`) over SSE to the frontend.

> **Plugin vs Skill**: Plugins provide atomic tools (`type: tool`). Skills define task workflows that orchestrate those tools via `load_skill`. See `docs/architecture/plugin-skill-boundary.md` for details.

### Frontend

`tui/` is a Vue 3 single-page application that renders a terminal-style UI for the audit agent:

- `src/App.vue` orchestrates `Terminal.vue`.
- `src/composables/useAgentSession.ts` manages the SSE connection and event normalization.
- `src/utils/toolCalls.ts` normalizes tool/subagent events and groups them into steps for display.
- `src/api/client.ts` uses a hard-coded `/api` base URL; the Vite dev server proxies `/api` to `http://localhost:8000`.

### Data & Infrastructure

- **MySQL 8.x** — primary persistence (SQLAlchemy async ORM).
- **Elasticsearch 8.x** — full-text search / RAG.
- **MinIO** — S3-compatible object storage for uploads and results.
- **PaddleOCR / PPStructureV3** — OCR for scanned PDFs and images (external service on port 8006).
- **LibreOffice** — PDF-to-DOCX conversion.

### Observability

- OpenTelemetry traces/metrics exported to the OTLP collector.
- Prometheus metrics endpoint at `/metrics`.
- Grafana on `localhost:3001` and Langfuse on `localhost:3000` when Docker Compose is running.

## Important Local Rules

- After modifying code in any directory, check whether the `CLAUDE.md` in that directory (or the repo root) needs updating. Update it when the change affects build/test commands, high-level architecture, project conventions, or any guidance future instances would need to stay productive.
- The backend subdirectory `courtier/CLAUDE.md` requires **committing after every code change** and using conventional commits (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`).
- If using git worktrees, merge the worktree branch back to `main` and clean up the worktree when finished.
- The frontend subdirectory `tui/CLAUDE.md` documents the "Editorial Noir" design system and component conventions for the static prototype.

## Notes

- The `Dockerfile` builds the frontend from `webui/` and copies the dist output to `/app/static`. The old `ui-vue/` / `tui/` references are obsolete.
- The plugin system scans plugins from the top-level `plugins/` directory. Shared plugins live under `plugins/shared/`; domain-specific plugins live under `plugins/<domain>/`. Each plugin has its own `plugin.yaml` manifest and may have its own `.venv`.
- Skill documents live under domain packages at `domains/<domain>/skills/*.md`. Each Skill declares its required tools in YAML frontmatter; the body is the sub-agent's system prompt. The `SkillRegistry` scans and pre-compiles them at startup. OrchestratorAgent calls `load_skill(skill="name", task="...")` to run one.
- Shared libraries (installable packages with their own `pyproject.toml`, wired editable via `[tool.uv.sources]`) live under `libs/shared/` and `libs/<domain>/`. Plugins depend only on `courtier-plugin-sdk` plus the libs they use — never on the `courtier` application package.
- `.env.example` ships with placeholder credentials and LLM endpoints; copy it to `.env` and replace all values for real use. Key Courtier-specific variables include `COURTIER_REPO_ROOT`, `COURTIER_DOMAIN_PACKAGES`, and `COURTIER_UPLOAD_DIR`.
