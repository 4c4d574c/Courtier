FROM node:22-slim AS frontend-build

WORKDIR /app/packages/webui

COPY packages/webui/package.json packages/webui/package-lock.json ./
RUN npm ci

COPY packages/webui/ .
RUN npm run build


FROM python:3.12-slim AS backend

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Create non-root runtime user
RUN groupadd --gid 1000 courtier && \
    useradd --uid 1000 --gid courtier --shell /bin/bash --create-home courtier

WORKDIR /app

# Copy core and domain packages plus legacy src/ compat shim
COPY pyproject.toml uv.lock ./
COPY README.md ./
COPY packages/ packages/
COPY src/ src/

# Install dependencies (non-editable install for production)
RUN uv sync --frozen --no-dev --no-editable

# Copy frontend build output
COPY --from=frontend-build /app/packages/webui/dist /app/static

# Create runtime directories and assign ownership
RUN mkdir -p /app/uploads /app/.agent_logs && \
    chown -R courtier:courtier /app

USER courtier

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/metrics >/dev/null || exit 1

ENV COURTIER_REPO_ROOT=/app

CMD ["uv", "run", "python", "-m", "uvicorn", "courtier.agent.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
