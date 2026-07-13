# Courtier — General-Purpose AI Agent Platform

A domain-agnostic AI agent platform with a pluggable architecture. The core engine provides an agent runtime, tool orchestration, plugin system, and skill framework — with zero domain knowledge. Domain-specific capabilities are delivered as self-contained packages (plugins + skills + rules + i18n prompts).

The first official domain package, **docaudit**, provides Chinese government document auditing based on GB/T 9704-2012.

## Architecture

```
courtier/
├── packages/
│   ├── core/                       # Domain-agnostic agent engine
│   │   └── src/courtier/
│   │       ├── agent/              # Agent loop, orchestrator, subagent runtime
│   │       ├── plugin/             # JSON-RPC 2.0 plugin system (subprocess isolation)
│   │       ├── skills/             # Skill registry and config (Markdown + YAML)
│   │       ├── prompts/            # PromptEngine — Jinja2 templates, zero hardcoded text
│   │       ├── domain/             # DomainLoader — package discovery and validation
│   │       ├── cli/                # CLI tools (courtier validate-domain)
│   │       ├── storage/            # MinIO object storage client
│   │       ├── es/                 # Elasticsearch client
│   │       ├── db/                 # MySQL / SQLAlchemy async ORM
│   │       └── api/                # FastAPI app, SSE streaming, middleware
│   │
│   ├── domains/
│   │   └── docaudit/               # Chinese government document audit domain
│   │       ├── plugins/            # parse, search, annotate, plagiarism, validator, …
│   │       ├── skills/             # format_audit, content_audit, plagiarism, full_audit
│   │       ├── rules/              # Format rules, compliance rules (JSON)
│   │       ├── config/
│   │       │   ├── domain.yaml     # Domain metadata
│   │       │   └── prompts/        # Locale-specific prompt templates (zh-CN, en-US)
│   │       └── models/             # Domain data models
│   │
│   └── webui/                      # Vue 3 terminal-style frontend
│
└── docs/                           # Architecture docs, developer guides
```

## Key Design Principles

- **Zero hardcoded NL text in core** — All natural language lives in locale-specific PromptBundle YAML, rendered via Jinja2
- **Zero domain imports in core** — Core never imports domain code; domains are discovered at startup
- **Pluggable domains** — Add a new domain by creating a directory with plugins, skills, rules, and prompts. Zero core code changes.
- **Plugin isolation** — Each plugin runs as an independent subprocess communicating via JSON-RPC 2.0 over stdio
- **Skill orchestration** — Skills are Markdown documents with YAML frontmatter; the orchestrator dispatches them to sub-agents

## Tech Stack

| Component | Technology |
|-----------|------------|
| Backend | FastAPI (Python 3.12+) |
| Agent Runtime | Think → Act → Observe loop with SSE streaming |
| Plugin Protocol | JSON-RPC 2.0 over stdio |
| Skill Config | Markdown + YAML frontmatter |
| Prompt Engine | Jinja2 + YAML PromptBundle |
| Database | MySQL 8.x (SQLAlchemy async ORM) |
| Search | Elasticsearch 8.x |
| Object Storage | MinIO (S3-compatible) |
| Observability | OpenTelemetry, Prometheus, Grafana, Langfuse |
| Frontend | Vue 3 + Vite |
| Package Manager | uv |

## Quick Start

### Prerequisites

- Python 3.12+
- MySQL 8.x
- MinIO
- Elasticsearch 8.x
- (Optional) PaddleOCR service for scanned PDFs
- (Optional) LibreOffice for PDF-to-DOCX conversion

### Setup

```bash
# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with real credentials

# Run database migrations
uv run alembic upgrade head

# Validate the domain package
PYTHONPATH=packages/core/src:packages/domains/docaudit:packages/domains/docaudit/plugins \
  uv run courtier validate-domain packages/domains/docaudit/
```

### Run the API Server

```bash
PYTHONPATH=packages/core/src:packages/domains/docaudit:packages/domains/docaudit/plugins \
  uv run python -m uvicorn courtier.agent.api.app:create_app \
  --factory --host 0.0.0.0 --port 8000
```

API docs: `http://localhost:8000/docs`

### Run Tests

```bash
# All tests (excluding integration)
uv run pytest -m "not integration"

# Single test file
uv run pytest tests/courtier/domain/test_loader.py -v

# With coverage
uv run pytest --cov=packages/core/src/courtier --cov-report=term-missing
```

## Configuration

### Domain Packages

Configure which domain packages to load via environment variables:

```bash
# .env
COURTIER_DOMAIN_PACKAGES=docaudit
COURTIER_LOCALE=zh-CN
```

Add a new domain by creating a package under `packages/domains/` and adding its name to `COURTIER_DOMAIN_PACKAGES`.

### CLI

```bash
courtier validate-domain packages/domains/<name>/
```

Validates: domain.yaml schema, prompt locale coverage, plugin manifests, and skill files.

## Adding a New Domain

```
packages/domains/my-domain/
├── config/
│   ├── domain.yaml          # name, locales, requires_plugins, requires_services
│   └── prompts/
│       └── zh-CN/           # orchestrator.yaml, behavioral.yaml, chat.yaml, …
├── plugins/
│   └── my_tool/
│       ├── plugin.yaml
│       └── entry.py
├── skills/
│   └── my_skill.md
├── rules/
│   └── my_rules.json
└── pyproject.toml
```

See `docs/domain-package-guide.md`, `docs/plugin-development.md`, and `docs/skill-authoring.md` for detailed guides.

## docaudit Domain

The built-in `docaudit` domain package provides Chinese government document auditing:

- **Document Parsing** — PDF, DOCX, scanned images (OCR pipeline)
- **Format Audit** — GB/T 9704-2012 compliance checking
- **Content Audit** — Political compliance, confidentiality, policy consistency
- **Text Correction** — Spelling, grammar, terminology fixes
- **Plagiarism Detection** — Paragraph-level similarity with dynamic IQR thresholding
- **Annotation Export** — Results exported as annotated DOCX or PDF

## Observability

```bash
# Start the observability stack
docker-compose up -d
```

- **Grafana**: `http://localhost:3001`
- **Langfuse**: `http://localhost:3000`
- **Prometheus metrics**: `/metrics`

## License

Internal project — contact the maintainers for usage terms.
