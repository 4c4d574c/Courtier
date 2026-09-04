FROM node:22-slim AS frontend-build

WORKDIR /app/webui

COPY webui/package.json webui/package-lock.json ./
RUN npm ci

COPY webui/ .
RUN npm run build


FROM python:3.12-slim AS backend

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

# Pinned: floating latest on the production runtime path risks supply-chain drift.
COPY --from=ghcr.io/astral-sh/uv:0.11.4 /uv /usr/local/bin/uv

# Create non-root runtime user
RUN groupadd --gid 1000 courtier && \
    useradd --uid 1000 --gid courtier --shell /bin/bash --create-home courtier

WORKDIR /app

# Copy core, domain, libs, and plugin packages
COPY pyproject.toml uv.lock ./
COPY README.md ./
# The entrypoint (main.py) runs Alembic migrations before serving — without
# alembic.ini the container crash-loops instead of upgrading the schema.
COPY alembic.ini ./
COPY alembic/ alembic/
COPY courtier/ courtier/
COPY domains/ domains/
COPY shared/ shared/
COPY libs/ libs/
COPY plugins/ plugins/

# Install dependencies (non-editable install for production)
RUN uv sync --frozen --no-dev --no-editable

# Copy frontend build output
COPY --from=frontend-build /app/webui/dist /app/static

# Create runtime directories and assign ownership
RUN mkdir -p /app/uploads /app/.agent_logs && \
    chown -R courtier:courtier /app

USER courtier

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

ENV COURTIER_REPO_ROOT=/app
ENV UV_NO_SYNC=1

# Route through main.py: it applies Alembic migrations before serving.
# A bare uvicorn CMD produced "healthy" deployments with an empty schema.
CMD ["uv", "run", "python", "main.py", "--host", "0.0.0.0", "--port", "8000"]
