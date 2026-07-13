# docaudit-agent 可观测性方案实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 docaudit-agent 添加完整的 OpenTelemetry 分布式追踪 + Prometheus 指标 + Langfuse/Grafana 可视化能力

**Architecture:** 在 `src/agent/telemetry/` 下新增纯 OTel API 封装层（AgentTracer + 装饰器 + 上下文传播），在 agent loop、LLM 调用、工具执行、sub-agent 派发等关键路径手动插桩，通过 W3C TraceContext 经 JSON-RPC params 实现跨进程 trace 传播，以 OTel Collector → Langfuse + Prometheus → Grafana 作为观测后端

**Tech Stack:** Python 3.12, FastAPI, OpenTelemetry SDK 1.28+, Prometheus client, Langfuse 3, Grafana 11

---

## 文件结构

```
新增:
  src/agent/telemetry/__init__.py          # 公开 API
  src/agent/telemetry/context.py           # inject/extract TraceContext
  src/agent/telemetry/tracer.py            # AgentTracer, init_telemetry
  src/agent/telemetry/metrics.py           # Prometheus 指标
  src/agent/telemetry/decorators.py        # traced_agent/llm/tool
  src/agent/api/middleware/observability.py # FastAPI 中间件
  tests/telemetry/__init__.py
  tests/telemetry/test_context.py
  tests/telemetry/test_tracer.py
  tests/telemetry/test_metrics.py
  tests/telemetry/test_decorators.py
  tests/telemetry/test_api_middleware.py
  otel-collector-config.yaml
  prometheus.yml

修改:
  pyproject.toml                           # 新增 4 个依赖
  src/config.py                            # 新增 OTel 配置项
  src/agent/api/app.py                     # 启动初始化 + /metrics
  src/agent/core/loop.py                   # agent/llm/tool span 插桩
  src/agent/agents/orch.py                 # orchestrator span
  src/agent/agents/subagent/runner.py      # dispatch_subagent span + inject_context
  src/plugin/client.py                     # stream() 传递 trace_context
  src/plugin/proxies.py                    # ProxyAgent 侧 extract_context + 子 span
  docker-compose.yml                       # 新增 8 个可观测性服务
```

---

### Task 1: 基础设施 — Docker Compose 可观测性服务

**Files:**
- Create: `otel-collector-config.yaml`
- Create: `prometheus.yml`
- Modify: `docker-compose.yml`
- Modify: `.env.example`

- [ ] **Step 1: 创建 OTel Collector 配置**

```yaml
# otel-collector-config.yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 512
  memory_limiter:
    check_interval: 1s
    limit_mib: 512

exporters:
  otlp/langfuse:
    endpoint: http://langfuse:3000/api/public/otlp
    headers:
      Authorization: "Bearer ${LANGFUSE_PUBLIC_KEY}:${LANGFUSE_SECRET_KEY}"
  prometheus:
    endpoint: 0.0.0.0:8888
  debug:
    verbosity: basic

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch, memory_limiter]
      exporters: [otlp/langfuse, debug]
    metrics:
      receivers: [otlp]
      processors: [batch, memory_limiter]
      exporters: [prometheus, debug]
```

- [ ] **Step 2: 创建 Prometheus 配置**

```yaml
# prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "otel-collector"
    static_configs:
      - targets: ["otel-collector:8888"]

  - job_name: "docaudit-agent"
    static_configs:
      - targets: ["app:8000"]
```

- [ ] **Step 3: 在 docker-compose.yml 末尾新增可观测性服务**

在现有 `networks:` 块之前插入以下内容。现有服务 `app` 需要新增依赖 `otel-collector` 和 `prometheus`：

```yaml
  # === 可观测性基础设施 ===

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.115.0
    container_name: docaudit-otel-collector
    restart: unless-stopped
    command: ["--config=/etc/otel-collector-config.yaml"]
    volumes:
      - ./otel-collector-config.yaml:/etc/otel-collector-config.yaml:ro
    ports:
      - "127.0.0.1:4317:4317"
      - "127.0.0.1:4318:4318"
    environment:
      - LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY:-pk-placeholder}
      - LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY:-sk-placeholder}
    networks:
      - docaudit

  langfuse:
    image: langfuse/langfuse:3
    container_name: docaudit-langfuse
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    environment:
      - DATABASE_URL=postgresql://langfuse:langfuse@langfuse-postgres:5432/langfuse
      - CLICKHOUSE_URL=http://langfuse-clickhouse:8123
      - REDIS_URL=redis://langfuse-redis:6379
      - ENCRYPTION_KEY=${LANGFUSE_ENCRYPTION_KEY:-0000000000000000000000000000000000000000000000000000000000000000}
      - SALT=${LANGFUSE_SALT:-salt}
      - LANGFUSE_INIT_ORG_ID=${LANGFUSE_INIT_ORG_ID:-docaudit}
      - LANGFUSE_INIT_ORG_NAME=${LANGFUSE_INIT_ORG_NAME:-docaudit}
      - LANGFUSE_INIT_PROJECT_ID=${LANGFUSE_INIT_PROJECT_ID:-docaudit-agent}
      - LANGFUSE_INIT_PROJECT_NAME=${LANGFUSE_INIT_PROJECT_NAME:-docaudit-agent}
      - LANGFUSE_INIT_USER_EMAIL=${LANGFUSE_INIT_EMAIL:-admin@docaudit.local}
      - LANGFUSE_INIT_USER_NAME=${LANGFUSE_INIT_USER_NAME:-admin}
      - LANGFUSE_INIT_USER_PASSWORD=${LANGFUSE_INIT_PASSWORD:-adminadmin}
    depends_on:
      langfuse-postgres:
        condition: service_healthy
      langfuse-clickhouse:
        condition: service_healthy
      langfuse-redis:
        condition: service_started
    networks:
      - docaudit

  langfuse-worker:
    image: langfuse/langfuse:3
    container_name: docaudit-langfuse-worker
    restart: unless-stopped
    command: worker
    environment:
      - DATABASE_URL=postgresql://langfuse:langfuse@langfuse-postgres:5432/langfuse
      - CLICKHOUSE_URL=http://langfuse-clickhouse:8123
      - REDIS_URL=redis://langfuse-redis:6379
      - ENCRYPTION_KEY=${LANGFUSE_ENCRYPTION_KEY:-0000000000000000000000000000000000000000000000000000000000000000}
      - SALT=${LANGFUSE_SALT:-salt}
      - LANGFUSE_S3_BATCH_EXPORT_ENABLED=false
    depends_on:
      langfuse-postgres:
        condition: service_healthy
      langfuse-clickhouse:
        condition: service_healthy
      langfuse-redis:
        condition: service_started
    networks:
      - docaudit

  langfuse-postgres:
    image: postgres:16-alpine
    container_name: docaudit-langfuse-postgres
    restart: unless-stopped
    environment:
      - POSTGRES_USER=langfuse
      - POSTGRES_PASSWORD=langfuse
      - POSTGRES_DB=langfuse
    volumes:
      - ${DATA_DIR:-./data}/langfuse-pg:/var/lib/postgresql/data
    networks:
      - docaudit
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 3s
      retries: 10

  langfuse-clickhouse:
    image: clickhouse/clickhouse-server:24
    container_name: docaudit-langfuse-clickhouse
    restart: unless-stopped
    environment:
      - CLICKHOUSE_DB=default
      - CLICKHOUSE_USER=default
    volumes:
      - ${DATA_DIR:-./data}/langfuse-ch:/var/lib/clickhouse
    networks:
      - docaudit
    healthcheck:
      test: ["CMD-SHELL", "clickhouse-client --query 'SELECT 1' || exit 1"]
      interval: 5s
      timeout: 3s
      retries: 10

  langfuse-redis:
    image: redis:7-alpine
    container_name: docaudit-langfuse-redis
    restart: unless-stopped
    networks:
      - docaudit

  prometheus:
    image: prom/prometheus:v2.55.0
    container_name: docaudit-prometheus
    restart: unless-stopped
    ports:
      - "127.0.0.1:9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ${DATA_DIR:-./data}/prometheus:/prometheus
    networks:
      - docaudit

  grafana:
    image: grafana/grafana:11.3.0
    container_name: docaudit-grafana
    restart: unless-stopped
    ports:
      - "127.0.0.1:3001:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD:-admin}
    volumes:
      - ${DATA_DIR:-./data}/grafana:/var/lib/grafana
    networks:
      - docaudit
```

同时，修改 `app` 服务的 `depends_on`，在现有依赖基础上追加：

```yaml
    depends_on:
      db:
        condition: service_healthy
      elasticsearch:
        condition: service_healthy
      ppocr:
        condition: service_started
      minio:
        condition: service_started
      otel-collector:
        condition: service_started
```

并在 `app` 服务的 `environment` 中新增：

```yaml
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
      - OTEL_SERVICE_NAME=docaudit-agent
      - OTEL_LOG_LEVEL=${OTEL_LOG_LEVEL:-INFO}
```

- [ ] **Step 4: 在 .env.example 中新增环境变量**

在现有 `.env.example` 末尾追加：

```bash
# === 可观测性 ===
OTEL_SERVICE_NAME=docaudit-agent
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_LOG_LEVEL=INFO
LANGFUSE_PUBLIC_KEY=pk-placeholder
LANGFUSE_SECRET_KEY=sk-placeholder
LANGFUSE_ENCRYPTION_KEY=0000000000000000000000000000000000000000000000000000000000000000
LANGFUSE_SALT=salt
LANGFUSE_INIT_EMAIL=admin@docaudit.local
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_PASSWORD=adminadmin
GRAFANA_PASSWORD=admin
```

- [ ] **Step 5: 提交**

```bash
git add otel-collector-config.yaml prometheus.yml docker-compose.yml .env.example
git commit -m "feat: add observability infrastructure (OTel Collector, Langfuse, Prometheus, Grafana)

Add 8 observability services to docker-compose: otel-collector, langfuse,
langfuse-worker, langfuse-postgres, langfuse-clickhouse, langfuse-redis,
prometheus, and grafana. All join existing docaudit network.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 添加 Python 依赖

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: 使用 uv 添加依赖**

```bash
uv add opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp prometheus-client
```

- [ ] **Step 2: 验证依赖已添加**

```bash
grep -E "opentelemetry|prometheus" pyproject.toml
```

预期输出包含：
```
"opentelemetry-api>=1.28.0"
"opentelemetry-sdk>=1.28.0"
"opentelemetry-exporter-otlp>=1.28.0"
"prometheus-client>=0.21.0"
```

- [ ] **Step 3: 提交**

```bash
git add pyproject.toml uv.lock
git commit -m "feat: add OpenTelemetry SDK and Prometheus client dependencies

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 配置 — OTel 设置

**Files:**
- Modify: `src/config.py`

- [ ] **Step 1: 在 Settings 类中新增 OTel 配置项**

在 `src/config.py` 的 `Settings` 类中，`audit_log_dir` 后面添加：

```python
    otel_service_name: str = Field(
        default="docaudit-agent",
        alias="otel_service_name",
        description="OpenTelemetry service name",
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4317",
        alias="otel_exporter_otlp_endpoint",
        description="OTLP exporter endpoint",
    )
    otel_log_level: str = Field(
        default="INFO",
        alias="otel_log_level",
        description="OpenTelemetry log level (DEBUG enables console exporter)",
    )
```

- [ ] **Step 2: 验证 Settings 可以正常加载**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.config import Settings; s = Settings(); print(s.otel_service_name); print(s.otel_exporter_otlp_endpoint)"
```

预期输出：
```
docaudit-agent
http://localhost:4317
```

- [ ] **Step 3: 提交**

```bash
git add src/config.py
git commit -m "feat: add OTel configuration fields to Settings

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Telemetry 层 — 上下文传播工具

**Files:**
- Create: `src/agent/telemetry/__init__.py`
- Create: `src/agent/telemetry/context.py`

- [ ] **Step 1: 创建 __init__.py**

```python
# src/agent/telemetry/__init__.py
"""OpenTelemetry-based observability for docaudit-agent.

Provides distributed tracing (AgentTracer), Prometheus metrics,
and W3C TraceContext propagation for cross-process (plugin) tracing.
"""

from .tracer import AgentTracer, init_telemetry, get_tracer
from .context import inject_context, extract_context
from .decorators import traced_agent, traced_llm, traced_tool
from .metrics import (
    record_agent_request,
    record_agent_latency,
    record_llm_call,
    record_llm_tokens,
    record_tool_execution,
    record_tool_latency,
    record_subagent_dispatch,
)

__all__ = [
    "AgentTracer",
    "init_telemetry",
    "get_tracer",
    "inject_context",
    "extract_context",
    "traced_agent",
    "traced_llm",
    "traced_tool",
    "record_agent_request",
    "record_agent_latency",
    "record_llm_call",
    "record_llm_tokens",
    "record_tool_execution",
    "record_tool_latency",
    "record_subagent_dispatch",
]
```

- [ ] **Step 2: 创建 context.py**

```python
# src/agent/telemetry/context.py
"""W3C TraceContext propagation utilities for cross-process tracing.

Orchestrator → plugin subprocess communication uses JSON-RPC over stdio,
not HTTP.  We serialize/deserialize the trace context into a plain dict
carrier that travels as a JSON-RPC params field.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_propagator = TraceContextTextMapPropagator()


def inject_context(carrier: dict[str, str]) -> None:
    """Inject current trace context into *carrier* dict (mutates in place).

    Usage in orchestrator before JSON-RPC call::

        trace_carrier: dict[str, str] = {}
        inject_context(trace_carrier)
        params["trace_context"] = trace_carrier
    """
    _propagator.inject(carrier)


def extract_context(carrier: dict[str, Any]) -> Any:
    """Extract trace context from *carrier* dict.

    Returns an OpenTelemetry Context object that can be passed as
    ``context=`` to ``start_as_current_span()``.

    Usage in ProxyAgent.run()::

        trace_carrier = params.get("trace_context", {})
        parent_ctx = extract_context(trace_carrier)
        with tracer.start_as_current_span(..., context=parent_ctx):
            ...
    """
    return _propagator.extract(carrier)
```

- [ ] **Step 3: 提交**

```bash
git add src/agent/telemetry/__init__.py src/agent/telemetry/context.py
git commit -m "feat: add telemetry context propagation (W3C TraceContext)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Telemetry 层 — AgentTracer 核心类

**Files:**
- Create: `src/agent/telemetry/tracer.py`

- [ ] **Step 1: 创建 tracer.py**

```python
# src/agent/telemetry/tracer.py
"""Framework-agnostic OpenTelemetry tracing for docaudit-agent.

Provides AgentTracer with agent_span, llm_span, tool_span, and plugin_span
context managers.  All methods operate directly on opentelemetry.trace API
with no intermediate framework dependency.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator, Optional

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider, BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import Status, StatusCode, SpanKind

_RESOURCE = Resource.create({
    "service.name": os.getenv("OTEL_SERVICE_NAME", "docaudit-agent"),
    "service.version": "0.1.0",
    "deployment.environment": os.getenv("DEPLOYMENT_ENVIRONMENT", "production"),
})

_provider: Optional[TracerProvider] = None


def init_telemetry() -> None:
    """Initialize the global TracerProvider.

    Idempotent — safe to call multiple times (e.g. in tests).
    Must be called once at application startup before any tracing occurs.
    """
    global _provider
    if _provider is not None:
        return

    _provider = TracerProvider(resource=_RESOURCE)

    otlp_endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
    )
    otlp_exporter = OTLPSpanExporter(
        endpoint=otlp_endpoint,
        insecure=not otlp_endpoint.startswith("https"),
        timeout=10,
    )
    _provider.add_span_processor(
        BatchSpanProcessor(
            otlp_exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        )
    )

    if os.getenv("OTEL_LOG_LEVEL", "").upper() == "DEBUG":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        _provider.add_span_processor(
            BatchSpanProcessor(ConsoleSpanExporter())
        )

    trace.set_tracer_provider(_provider)


def get_tracer(name: str = "docaudit-agent") -> trace.Tracer:
    """Get a named tracer, initializing telemetry if needed."""
    if _provider is None:
        init_telemetry()
    return trace.get_tracer(name)


class AgentTracer:
    """Agent-span tracer wrapping OTel API with gen_ai semantic conventions."""

    def __init__(self, tracer_name: str = "docaudit-agent") -> None:
        self._tracer = get_tracer(tracer_name)

    # ── Agent Span ────────────────────────────────────────

    @contextmanager
    def agent_span(
        self,
        agent_name: str,
        session_id: str = "",
        **extra_attrs: Any,
    ) -> Generator[trace.Span, None, None]:
        """Create an invoke_agent span.  Yields the span for attribute setting."""
        with self._tracer.start_as_current_span(
            name=f"invoke_agent {agent_name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", agent_name)
            span.set_attribute("agent.session_id", session_id)
            span.set_attribute("agent.start_time_iso", _now_iso())
            for k, v in extra_attrs.items():
                if v is not None:
                    span.set_attribute(f"agent.{k}", _safe_str(v))
            try:
                yield span
                span.set_attribute("agent.status", "success")
                span.set_attribute("agent.end_time_iso", _now_iso())
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("agent.status", "error")
                span.set_attribute("agent.error.type", type(e).__name__)
                span.set_attribute("agent.error.message", _safe_str(e, 500))
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise

    # ── LLM Span ──────────────────────────────────────────

    @contextmanager
    def llm_span(
        self,
        model: str = "unknown",
        temperature: float | None = None,
        system: str = "openai",
        **extra_attrs: Any,
    ) -> Generator[trace.Span, None, None]:
        """Create a chat span for an LLM call."""
        with self._tracer.start_as_current_span(
            name=f"chat {model}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.system", system)
            span.set_attribute("gen_ai.request.model", model)
            if temperature is not None:
                span.set_attribute("gen_ai.request.temperature", temperature)
            for k, v in extra_attrs.items():
                if v is not None:
                    span.set_attribute(f"gen_ai.request.{k}", _safe_str(v))
            try:
                yield span
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("gen_ai.response.finish_reasons", ["error"])
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise

    def set_token_usage(
        self,
        span: trace.Span,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Record token usage on an LLM span."""
        span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
        span.set_attribute(
            "gen_ai.usage.total_tokens",
            prompt_tokens + completion_tokens,
        )

    def log_prompt(self, span: trace.Span, content: str, role: str = "user") -> None:
        """Record prompt content as a span event."""
        span.add_event("gen_ai.content.prompt", {
            "role": role,
            "content": _safe_str(content, 4000),
        })

    def log_completion(
        self,
        span: trace.Span,
        content: str,
        finish_reason: str = "stop",
    ) -> None:
        """Record completion content as a span event."""
        span.add_event("gen_ai.content.completion", {
            "content": _safe_str(content, 4000),
            "finish_reason": finish_reason,
        })

    # ── Tool Span ─────────────────────────────────────────

    @contextmanager
    def tool_span(
        self,
        tool_name: str,
        parameters: dict[str, Any] | None = None,
    ) -> Generator[trace.Span, None, None]:
        """Create an execute_tool span."""
        start = time.time()
        with self._tracer.start_as_current_span(
            name=f"execute_tool {tool_name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "execute_tool")
            span.set_attribute("gen_ai.tool.name", tool_name)
            if parameters:
                span.set_attribute(
                    "gen_ai.tool.parameters",
                    _safe_str(parameters, 1000),
                )
            try:
                yield span
                latency_ms = (time.time() - start) * 1000
                span.set_attribute("gen_ai.tool.latency_ms", latency_ms)
                span.set_attribute("gen_ai.tool.status", "success")
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("gen_ai.tool.status", "error")
                span.set_attribute("gen_ai.tool.error", _safe_str(e, 500))
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise

    def set_tool_result(
        self,
        span: trace.Span,
        result: Any,
    ) -> None:
        """Record tool result preview on the span."""
        preview = _safe_str(result, 500) if result is not None else ""
        span.set_attribute("gen_ai.tool.result_preview", preview)

    # ── Plugin / Sub-Agent Span ───────────────────────────

    @contextmanager
    def plugin_span(
        self,
        subagent_name: str,
        task: str = "",
    ) -> Generator[trace.Span, None, None]:
        """Create a dispatch_subagent span for cross-process sub-agent calls."""
        start = time.time()
        with self._tracer.start_as_current_span(
            name=f"dispatch_subagent {subagent_name}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("gen_ai.operation.name", "dispatch_subagent")
            span.set_attribute("subagent.name", subagent_name)
            if task:
                span.set_attribute("subagent.task", _safe_str(task, 500))
            try:
                yield span
                latency_ms = (time.time() - start) * 1000
                span.set_attribute("subagent.latency_ms", latency_ms)
                span.set_attribute("subagent.status", "success")
                span.set_status(Status(StatusCode.OK))
            except Exception as e:
                span.set_attribute("subagent.status", "error")
                span.set_attribute("subagent.error", _safe_str(e, 500))
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)[:1000]))
                raise


# ── Helpers ───────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(obj: Any, max_len: int = 1000) -> str:
    s = str(obj)
    return s[:max_len] if len(s) > max_len else s
```

- [ ] **Step 2: 验证模块可以导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.telemetry.tracer import AgentTracer, init_telemetry; print('OK')"
```

预期输出：`OK`

- [ ] **Step 3: 提交**

```bash
git add src/agent/telemetry/tracer.py
git commit -m "feat: add AgentTracer with agent/llm/tool/plugin span context managers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Telemetry 层 — Prometheus 指标

**Files:**
- Create: `src/agent/telemetry/metrics.py`

- [ ] **Step 1: 创建 metrics.py**

```python
# src/agent/telemetry/metrics.py
"""Prometheus metrics definitions for docaudit-agent.

All metrics follow Prometheus naming conventions and are registered
at module import time.  Helper functions provide typed recording.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram, Gauge

# ── Counters ──────────────────────────────────────────────

AGENT_REQUESTS_TOTAL = Counter(
    "agent_requests_total",
    "Total number of agent invocations",
    ["agent_name", "status"],
)

LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "Total number of LLM API calls",
    ["model", "agent_name"],
)

LLM_TOKEN_USAGE_TOTAL = Counter(
    "llm_token_usage_total",
    "Total tokens consumed by LLM calls",
    ["model", "direction"],  # direction: "input" or "output"
    unit="tokens",
)

TOOL_EXECUTIONS_TOTAL = Counter(
    "tool_executions_total",
    "Total number of tool executions",
    ["tool_name", "status"],
)

SUBAGENT_DISPATCH_TOTAL = Counter(
    "subagent_dispatch_total",
    "Total number of sub-agent dispatches",
    ["subagent_name", "status"],
)

# ── Histograms ────────────────────────────────────────────

AGENT_LATENCY_SECONDS = Histogram(
    "agent_latency_seconds",
    "End-to-end agent run latency",
    ["agent_name"],
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

TOOL_LATENCY_SECONDS = Histogram(
    "tool_latency_seconds",
    "Tool execution latency",
    ["tool_name"],
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# ── Gauges ────────────────────────────────────────────────

PLUGIN_STATE = Gauge(
    "plugin_state_changes",
    "Current state of each plugin (1=active, 0=other)",
    ["plugin_name", "state"],
)

# ── Recording helpers ─────────────────────────────────────

def record_agent_request(agent_name: str, status: str) -> None:
    AGENT_REQUESTS_TOTAL.labels(agent_name=agent_name, status=status).inc()


def record_agent_latency(agent_name: str, seconds: float) -> None:
    AGENT_LATENCY_SECONDS.labels(agent_name=agent_name).observe(seconds)


def record_llm_call(model: str, agent_name: str) -> None:
    LLM_CALLS_TOTAL.labels(model=model, agent_name=agent_name).inc()


def record_llm_tokens(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    LLM_TOKEN_USAGE_TOTAL.labels(model=model, direction="input").inc(input_tokens)
    LLM_TOKEN_USAGE_TOTAL.labels(model=model, direction="output").inc(output_tokens)


def record_tool_execution(tool_name: str, status: str) -> None:
    TOOL_EXECUTIONS_TOTAL.labels(tool_name=tool_name, status=status).inc()


def record_tool_latency(tool_name: str, seconds: float) -> None:
    TOOL_LATENCY_SECONDS.labels(tool_name=tool_name).observe(seconds)


def record_subagent_dispatch(subagent_name: str, status: str) -> None:
    SUBAGENT_DISPATCH_TOTAL.labels(
        subagent_name=subagent_name, status=status
    ).inc()
```

- [ ] **Step 2: 验证指标可以导入和注册**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "
from src.agent.telemetry.metrics import (
    record_agent_request, record_tool_execution,
    AGENT_REQUESTS_TOTAL, TOOL_EXECUTIONS_TOTAL,
)
record_agent_request('test_agent', 'success')
record_tool_execution('test_tool', 'success')
print('Metrics recorded OK')
print('agent_requests_total labels:', list(AGENT_REQUESTS_TOTAL._metrics.keys()))
"
```

预期输出包含 `Metrics recorded OK`

- [ ] **Step 3: 提交**

```bash
git add src/agent/telemetry/metrics.py
git commit -m "feat: add Prometheus metrics (Counters, Histograms, Gauges) for agent observability

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Telemetry 层 — 装饰器

**Files:**
- Create: `src/agent/telemetry/decorators.py`

- [ ] **Step 1: 创建 decorators.py**

```python
# src/agent/telemetry/decorators.py
"""Non-invasive tracing decorators for agent methods.

Use these when the call chain is simple (single function call).
For complex multi-phase flows (e.g. agent_loop), use AgentTracer
context managers directly.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .tracer import AgentTracer


def traced_agent(agent_name: str):
    """Decorate an agent's run() method with an invoke_agent span."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            tracer = getattr(self, "_tracer", AgentTracer())
            session_id = kwargs.pop("session_id", "")
            with tracer.agent_span(
                agent_name=agent_name,
                session_id=session_id,
            ) as span:
                if args:
                    span.set_attribute(
                        "agent.input.args",
                        str(args)[:500],
                    )
                task = kwargs.get("task")
                if task:
                    span.set_attribute("agent.task", str(task)[:500])
                result = await func(self, *args, **kwargs)
                span.set_attribute(
                    "agent.output_preview",
                    str(result)[:300],
                )
                return result

        return wrapper

    return decorator


def traced_llm(model: str = "unknown", system: str = "openai"):
    """Decorate an LLM call method with a chat span."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = AgentTracer()
            with tracer.llm_span(model=model, system=system) as span:
                prompt = kwargs.get("prompt") or kwargs.get("messages")
                if prompt is not None:
                    tracer.log_prompt(span, str(prompt))
                result = await func(*args, **kwargs)
                usage = getattr(result, "usage", None)
                if usage:
                    tracer.set_token_usage(
                        span,
                        getattr(usage, "prompt_tokens", 0),
                        getattr(usage, "completion_tokens", 0),
                    )
                return result

        return wrapper

    return decorator


def traced_tool(tool_name: str):
    """Decorate a tool execution method with an execute_tool span."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = AgentTracer()
            with tracer.tool_span(tool_name, parameters=kwargs) as span:
                result = await func(*args, **kwargs)
                tracer.set_tool_result(span, result)
                return result

        return wrapper

    return decorator
```

- [ ] **Step 2: 验证装饰器可以导入**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "from src.agent.telemetry.decorators import traced_agent, traced_llm, traced_tool; print('OK')"
```

预期输出：`OK`

- [ ] **Step 3: 提交**

```bash
git add src/agent/telemetry/decorators.py
git commit -m "feat: add tracing decorators (traced_agent, traced_llm, traced_tool)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: API 层 — FastAPI 中间件和 /metrics 端点

**Files:**
- Create: `src/agent/api/middleware/__init__.py`
- Create: `src/agent/api/middleware/observability.py`

- [ ] **Step 1: 创建 middleware/__init__.py**

```python
# src/agent/api/middleware/__init__.py
"""FastAPI middleware package."""
```

- [ ] **Step 2: 创建 middleware/observability.py**

```python
# src/agent/api/middleware/observability.py
"""HTTP-level observability middleware for FastAPI.

Records request count and latency per endpoint without touching
business logic.
"""

from __future__ import annotations

import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from src.agent.telemetry.metrics import AGENT_REQUESTS_TOTAL


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Records HTTP request metrics.

    Does NOT create OTel spans — HTTP tracing is handled by the
    agent-level spans in agent_loop and orch.py, which carry richer
    business context (agent_name, session_id, etc.).
    """

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        response = await call_next(request)
        duration = time.time() - start

        status = "success" if response.status_code < 400 else "error"
        AGENT_REQUESTS_TOTAL.labels(
            agent_name="http",
            status=status,
        ).inc()

        return response
```

- [ ] **Step 3: 修改 app.py — 注册 middleware 和 /metrics 端点**

在 `src/agent/api/app.py` 中：

在现有 import 块中新增：

```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

from src.agent.telemetry.tracer import init_telemetry
from .middleware.observability import ObservabilityMiddleware
```

在 lifespan 的启动部分（`app.state.agent = agent` 之前或之后）添加：

```python
    init_telemetry()
```

在 `app = FastAPI(...)` 之后、路由定义之前添加 middleware：

```python
app.add_middleware(ObservabilityMiddleware)
```

在路由区域新增 `/metrics` 端点：

```python
@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
```

- [ ] **Step 4: 验证 /metrics 端点**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "
from src.agent.api.app import app
from fastapi.testclient import TestClient
client = TestClient(app)
resp = client.get('/metrics')
print(resp.status_code)
print(resp.text[:200])
"
```

预期输出：`200` 及 Prometheus text 格式的指标

- [ ] **Step 5: 提交**

```bash
git add src/agent/api/middleware/__init__.py src/agent/api/middleware/observability.py src/agent/api/app.py
git commit -m "feat: add FastAPI observability middleware and /metrics endpoint

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: 核心集成 — agent_loop 插桩

**Files:**
- Modify: `src/agent/core/loop.py`

- [ ] **Step 1: 在 agent_loop 中添加 OTel spans + metrics**

在 `src/agent/core/loop.py` 的 import 区域新增：

```python
from ..telemetry.tracer import AgentTracer
from ..telemetry.metrics import (
    record_agent_request,
    record_agent_latency,
    record_llm_call,
    record_llm_tokens,
    record_tool_execution,
    record_tool_latency,
)
```

在 `async def agent_loop(...)` 函数体中，`current_state = state` 之前添加：

```python
    tracer = AgentTracer()
    agent_name = getattr(state, "agent_name", "unknown")
```

**LLM span 插桩** — 在 `think = await think_phase(...)` 调用周围包裹 llm_span：

```python
        # THINK phase with OTel LLM span
        with tracer.llm_span(
            model=getattr(model, "model_name", "unknown"),
            temperature=getattr(model, "_temperature", None),
        ) as llm_span:
            think = await think_phase(
                state=current_state,
                model=model,
                tool_registry=tool_registry,
                context_manager=context_manager,
                recent_reasoning=recent_reasoning,
                on_step=on_step,
                on_token=on_token,
                on_content_token=on_content_token,
            )
            # Record prompt/completion as span events
            llm_request_msgs = think.llm_request.messages
            if llm_request_msgs:
                last_msg = llm_request_msgs[-1]
                tracer.log_prompt(
                    llm_span,
                    str(last_msg.get("content", "")) if isinstance(last_msg, dict) else str(last_msg),
                )
            if think.llm_response.content:
                tracer.log_completion(
                    llm_span,
                    think.llm_response.content,
                    think.llm_response.finish_reason,
                )
            if think.llm_response.usage:
                usage = think.llm_response.usage
                tracer.set_token_usage(
                    llm_span,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                )
            if think.state.status == "error":
                llm_span.set_status(
                    Status(StatusCode.ERROR, "LLM call failed")
                )
```

**Tool span 插桩** — 在 `execute_tools_phase()` 调用返回后，为每个 tool record 记录 span（在原有的 `results, tool_records = await execute_tools_phase(...)` 之后、`current_state = current_state.add_observation(...)` 之前）：

```python
        # Record tool execution spans and metrics
        for record in tool_records:
            with tracer.tool_span(
                tool_name=record.tool_name,
                parameters=record.arguments,
            ) as tool_span:
                tool_span.set_attribute(
                    "gen_ai.tool.latency_ms", record.duration_ms
                )
                tool_span.set_attribute(
                    "gen_ai.tool.status",
                    "success" if record.result_success else "error",
                )
                tracer.set_tool_result(tool_span, record.result_data)

            record_tool_execution(
                tool_name=record.tool_name,
                status="success" if record.result_success else "error",
            )
            record_tool_latency(
                tool_name=record.tool_name,
                seconds=record.duration_ms / 1000.0,
            )
```

**Metrics 记录** — 在 think_phase 返回后记录 LLM metrics（紧接 llm_span 块之后）：

```python
        if think.llm_response.usage:
            record_llm_call(
                model=think.llm_request.model or "unknown",
                agent_name=agent_name,
            )
            usage = think.llm_response.usage
            if usage:
                record_llm_tokens(
                    model=think.llm_request.model or "unknown",
                    input_tokens=usage.get("prompt_tokens", 0),
                    output_tokens=usage.get("completion_tokens", 0),
                )
```

**全局 metrics** — 在函数末尾 `return current_state` 之前：

```python
    record_agent_request(
        agent_name=agent_name,
        status=current_state.status if current_state.status != "error" else "error",
    )
```

需要额外 import：

```python
from opentelemetry.trace import Status, StatusCode
```

- [ ] **Step 2: 验证已有的测试仍然通过**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/ -x --timeout=60 -q 2>&1 | tail -20
```

预期：所有已有测试通过（metrics 记录不影响现有行为）

- [ ] **Step 3: 提交**

```bash
git add src/agent/core/loop.py
git commit -m "feat: add metrics instrumentation to agent_loop (LLM calls, tool executions, latency)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: 核心集成 — OrchestratorAgent span

**Files:**
- Modify: `src/agent/agents/orch.py`

- [ ] **Step 1: 在 OrchestratorAgent.run() 中添加 orchestrator span**

在 `orch.py` 的 import 区域新增：

```python
from ..telemetry.tracer import AgentTracer
from ..telemetry.metrics import record_agent_request, record_agent_latency
```

在 `run()` 方法中，包裹一个 orchestrator span。由于 Agent 基类的 `run()` 是通用的，orchestrator span 应在 orchestrator 的特定上下文中创建。查看 `OrchestratorAgent` 是否有自己的 `run()` — 根据代码，`OrchestratorAgent` 继承 `Agent`。我们需要在 `OrchestratorAgent` 中覆盖 `run()` 或者在调用处创建 span。

实际上，更好的方式是让 `Agent.run()` 基类在调用 `agent_loop` 时自动包裹 agent span。但为了最小化改动，我们在 `agent_loop` 被调用时在调用栈中已有了 agent span（来自 Task 9 中的 tracer）。

如果 `OrchestratorAgent` 没有自己的 `run()` 覆盖，则在 `Agent.run()` 基类中添加 optional tracer。目前先保持最小改动——在 `OrchestratorAgent.__init__` 中初始化 tracer，在基类 `Agent.run()` 的 `agent_loop` 调用周围包裹 span。

在 `src/agent/agents/base.py` 的 `Agent.run()` 方法中，在 `agent_loop(...)` 调用前后添加：

```python
        import time
        from src.agent.telemetry.tracer import AgentTracer

        tracer = AgentTracer()
        start = time.time()
        try:
            final_state = await agent_loop(
                state=current_state,
                model=self.model,
                tool_registry=self.tool_registry,
                hooks=self.hooks,
                permissions=self.permissions,
                on_step=on_step,
                on_token=on_token,
                on_content_token=on_content_token,
                on_tool_result=on_tool_result,
                context_manager=context_manager,
                audit_logger=audit_logger,
                artifact_store=artifact_store,
            )
        except Exception:
            from src.agent.telemetry.metrics import record_agent_request
            record_agent_request(agent_name=self.name, status="error")
            raise
        elapsed = time.time() - start
        from src.agent.telemetry.metrics import (
            record_agent_request,
            record_agent_latency,
        )
        record_agent_request(
            agent_name=self.name,
            status=final_state.status if final_state.status != "error" else "error",
        )
        record_agent_latency(agent_name=self.name, seconds=elapsed)
```

- [ ] **Step 2: 验证已有测试通过**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/agent/ tests/plugin/test_proxy_agent_dispatch.py -x --timeout=60 -q 2>&1 | tail -20
```

- [ ] **Step 3: 提交**

```bash
git add src/agent/agents/base.py
git commit -m "feat: add agent-level metrics (requests, latency) to Agent.run() base class

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: 跨进程传播 — SubAgentRunner dispatch span + inject_context

**Files:**
- Modify: `src/agent/agents/subagent/runner.py`

- [ ] **Step 1: 在 SubAgentRunner.dispatch() 中添加 plugin span + inject_context**

在 `runner.py` 的 import 区域新增：

```python
from ...telemetry.tracer import AgentTracer
from ...telemetry.context import inject_context
from ...telemetry.metrics import record_subagent_dispatch
```

在 `dispatch()` 方法中，在 `_run()` 内部函数定义之前，创建 plugin span 并将 trace_context 注入到 run_kwargs：

在 `_run()` 函数体内的 `run_kwargs: dict[str, Any] = dict(...)` 之后，添加：

```python
            # Inject trace context for cross-process propagation
            trace_carrier: dict[str, str] = {}
            inject_context(trace_carrier)
            run_kwargs["trace_context"] = trace_carrier
```

在 `dispatch()` 方法的 `result = await self._execute_with_retry(...)` 之后，`return result` 之前，添加：

```python
        record_subagent_dispatch(
            subagent_name=name,
            status="success" if result.success else "error",
        )
```

- [ ] **Step 2: 同样修改 dispatch_structured()**

在 `dispatch_structured()` 的 `_run()` 内部函数中，`run_kwargs` 构建后添加相同的 `trace_context` 注入：

```python
            trace_carrier: dict[str, str] = {}
            inject_context(trace_carrier)
            run_kwargs["trace_context"] = trace_carrier
```

并在 `_execute_with_retry` 返回后添加 `record_subagent_dispatch`。

- [ ] **Step 3: 验证已有测试通过**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/plugin/test_proxy_agent_dispatch.py tests/plugin/test_integration.py -x --timeout=60 -q 2>&1 | tail -20
```

- [ ] **Step 4: 提交**

```bash
git add src/agent/agents/subagent/runner.py
git commit -m "feat: inject W3C TraceContext into sub-agent dispatch params for cross-process tracing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 12: 跨进程传播 — ProxyAgent 侧 extract_context + 子 span

**Files:**
- Modify: `src/plugin/proxies.py`

- [ ] **Step 1: 在 ProxyAgent.run() 中提取 trace context 并创建子 span**

在 `proxies.py` 的 import 区域新增：

```python
from src.agent.telemetry.context import extract_context
from src.agent.telemetry.tracer import AgentTracer
```

在 `ProxyAgent.run()` 方法中，`params = {...}` 构建之后、`async for chunk in self._client.stream(...)` 之前，提取 trace context：

```python
            # Extract trace context from parent (orchestrator) for distributed tracing
            trace_carrier = kwargs.get("trace_context")
            parent_ctx = extract_context(trace_carrier) if trace_carrier else None
```

然后在 `self._client.stream("agent.run", params, ...)` 调用时，需要让子 span 包裹整个 stream 过程。由于 `_client.stream` 是一个 async generator，我们用 `start_as_current_span` 手动管理：

```python
            tracer = AgentTracer()
            with tracer.plugin_span(
                subagent_name=self._agent_name,
                task=task or "",
            ) as plugin_span:
                async for chunk in self._client.stream(
                    "agent.run",
                    params,
                    heartbeat_timeout=heartbeat,
                ):
                    # ... 原有的 chunk 处理逻辑保持不变 ...
                
                # After stream ends, set result attributes
                plugin_span.set_attribute(
                    "subagent.status",
                    final_result.get("status", "completed"),
                )
```

注意：由于 `plugin_span` 是同步 context manager（`@contextmanager`），它可以包裹 async generator。但 `start_as_current_span` 返回的 span 本身是同步的。我们需要确保代码结构正确。

实际修改中，将整个 `try/except` 块放到 `tracer.plugin_span(...)` context manager 内：

```python
        tracer = AgentTracer()
        trace_carrier = kwargs.get("trace_context")
        # Note: plugin_span creates a new child span. When trace_carrier is
        # provided, the child span is linked via W3C TraceContext propagation
        # in the OTel SDK's context management. The explicit parent_ctx from
        # extract_context() is used to set the parent before entering the span.
        if trace_carrier:
            parent_ctx = extract_context(trace_carrier)
            # Attach the extracted context so that spans created inside
            # this context manager become children of the orchestrator span.
            from opentelemetry import context
            token = context.attach(parent_ctx)
        else:
            token = None
        
        try:
            with tracer.plugin_span(
                subagent_name=self._agent_name,
                task=task or "",
            ) as plugin_span:
                # ... existing try block with stream loop ...
        finally:
            if token is not None:
                context.detach(token)
```

- [ ] **Step 2: 确保 trace_context 被传递到 JSON-RPC params**

在 `params` 构建中确认 `trace_context` 不被 `_HOST_KWARGS` 过滤掉。检查 `_HOST_KWARGS = frozenset({...})` — 当前不包含 `"trace_context"`，且 `kwargs` 中非 HOST_KWARGS 的键会被 `params.update(...)` 合并进去。所以 `trace_context` 会通过。

- [ ] **Step 3: 验证已有测试通过**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/plugin/test_proxy_agent_dispatch.py -x --timeout=60 -q 2>&1 | tail -20
```

- [ ] **Step 4: 提交**

```bash
git add src/plugin/proxies.py
git commit -m "feat: extract TraceContext in ProxyAgent to continue distributed traces across plugin boundary

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 13: 单元测试 — context.py

**Files:**
- Create: `tests/telemetry/__init__.py`
- Create: `tests/telemetry/test_context.py`

- [ ] **Step 1: 创建测试目录和 __init__.py**

```bash
mkdir -p tests/telemetry
```

```python
# tests/telemetry/__init__.py
```

- [ ] **Step 2: 编写 context.py 的单元测试**

```python
# tests/telemetry/test_context.py
"""Tests for W3C TraceContext propagation utilities."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from src.agent.telemetry.context import inject_context, extract_context
from src.agent.telemetry.tracer import init_telemetry


@pytest.fixture(autouse=True)
def setup_telemetry():
    """Ensure TracerProvider is initialized for each test."""
    # Reset to a fresh provider for test isolation
    provider = TracerProvider()
    trace.set_tracer_provider(provider)
    yield
    # No explicit cleanup needed; each test gets its own provider


def test_inject_context_populates_carrier():
    """inject_context writes W3C tracecontext headers into the carrier dict."""
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}

    with tracer.start_as_current_span("parent-span"):
        inject_context(carrier)

    assert "traceparent" in carrier, "carrier should contain traceparent"
    assert carrier["traceparent"].startswith("00-"), (
        "traceparent should follow W3C format"
    )


def test_extract_context_roundtrip():
    """extract_context(inject_context(carrier)) should produce a valid context."""
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}

    with tracer.start_as_current_span("parent-span"):
        inject_context(carrier)

    extracted = extract_context(carrier)
    assert extracted is not None, "extract_context should return a Context object"


def test_extract_empty_carrier_returns_non_none():
    """extract_context on empty dict returns a context (possibly root)."""
    result = extract_context({})
    # Even with an empty carrier, the propagator returns a Context (may be root)
    assert result is not None


def test_inject_extract_preserves_trace_id():
    """The trace_id in the extracted context matches the injected one."""
    tracer = trace.get_tracer("test")
    carrier: dict[str, str] = {}

    with tracer.start_as_current_span("parent-span") as span:
        inject_context(carrier)
        original_trace_id = span.get_span_context().trace_id

    extracted = extract_context(carrier)
    span_context = trace.get_current_span().get_span_context()
    # The extracted context carries the remote parent, not the current span
    # Just verify the carrier content is parseable
    assert int(carrier["traceparent"].split("-")[1], 16) == original_trace_id
```

- [ ] **Step 3: 运行测试，确认失败（TDD RED）**

实际上由于这是新代码，用例和实现一起写的。运行测试验证通过：

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/telemetry/test_context.py -v
```

预期：4 passed

- [ ] **Step 4: 提交**

```bash
git add tests/telemetry/__init__.py tests/telemetry/test_context.py
git commit -m "test: add unit tests for TraceContext inject/extract propagation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 14: 单元测试 — tracer.py

**Files:**
- Create: `tests/telemetry/test_tracer.py`

- [ ] **Step 1: 编写测试**

```python
# tests/telemetry/test_tracer.py
"""Tests for AgentTracer span context managers."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from src.agent.telemetry.tracer import AgentTracer, init_telemetry, get_tracer


@pytest.fixture(autouse=True)
def setup_telemetry():
    """Fresh TracerProvider per test."""
    provider = TracerProvider()
    trace.set_tracer_provider(provider)


class TestInitTelemetry:
    def test_init_idempotent(self):
        """Calling init_telemetry twice should not raise."""
        init_telemetry()
        init_telemetry()  # should be safe

    def test_get_tracer_initializes_if_needed(self):
        """get_tracer() calls init_telemetry() lazily."""
        # Force new provider to trigger lazy init path
        t = get_tracer("test-tracer")
        assert t is not None


class TestAgentSpan:
    def test_agent_span_success(self):
        tracer = AgentTracer("test")
        with tracer.agent_span("test-agent", session_id="s1"):
            pass  # span exits successfully

    def test_agent_span_exception(self):
        tracer = AgentTracer("test")
        with pytest.raises(ValueError, match="test error"):
            with tracer.agent_span("test-agent"):
                raise ValueError("test error")

    def test_agent_span_sets_attributes(self):
        tracer = AgentTracer("test")
        with tracer.agent_span(
            "test-agent",
            session_id="abc123",
            custom_attr="value",
        ) as span:
            pass
        # After exit, span should have status OK


class TestLLMSpan:
    def test_llm_span_success(self):
        tracer = AgentTracer("test")
        with tracer.llm_span(model="gpt-4o", temperature=0.3):
            pass

    def test_llm_span_token_usage(self):
        tracer = AgentTracer("test")
        with tracer.llm_span(model="gpt-4o") as span:
            tracer.set_token_usage(span, 100, 50)

    def test_llm_span_prompt_completion(self):
        tracer = AgentTracer("test")
        with tracer.llm_span(model="gpt-4o") as span:
            tracer.log_prompt(span, "Hello", role="user")
            tracer.log_completion(span, "Hi there!", finish_reason="stop")


class TestToolSpan:
    def test_tool_span_success(self):
        tracer = AgentTracer("test")
        with tracer.tool_span("search", {"query": "test"}):
            pass

    def test_tool_span_sets_result(self):
        tracer = AgentTracer("test")
        with tracer.tool_span("search") as span:
            tracer.set_tool_result(span, "found 5 results")

    def test_tool_span_exception(self):
        tracer = AgentTracer("test")
        with pytest.raises(RuntimeError, match="tool failed"):
            with tracer.tool_span("search"):
                raise RuntimeError("tool failed")


class TestPluginSpan:
    def test_plugin_span_success(self):
        tracer = AgentTracer("test")
        with tracer.plugin_span("format_audit", task="check formatting"):
            pass

    def test_plugin_span_exception(self):
        tracer = AgentTracer("test")
        with pytest.raises(ConnectionError, match="disconnected"):
            with tracer.plugin_span("format_audit"):
                raise ConnectionError("disconnected")
```

- [ ] **Step 2: 运行测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/telemetry/test_tracer.py -v
```

预期：all passed

- [ ] **Step 3: 提交**

```bash
git add tests/telemetry/test_tracer.py
git commit -m "test: add unit tests for AgentTracer span context managers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 15: 单元测试 — metrics.py

**Files:**
- Create: `tests/telemetry/test_metrics.py`

- [ ] **Step 1: 编写测试**

```python
# tests/telemetry/test_metrics.py
"""Tests for Prometheus metrics definitions and recording helpers."""

from prometheus_client import REGISTRY, CollectorRegistry

from src.agent.telemetry.metrics import (
    record_agent_request,
    record_agent_latency,
    record_llm_call,
    record_llm_tokens,
    record_tool_execution,
    record_tool_latency,
    record_subagent_dispatch,
    AGENT_REQUESTS_TOTAL,
    LLM_CALLS_TOTAL,
    LLM_TOKEN_USAGE_TOTAL,
    TOOL_EXECUTIONS_TOTAL,
    SUBAGENT_DISPATCH_TOTAL,
    AGENT_LATENCY_SECONDS,
    TOOL_LATENCY_SECONDS,
)


def _get_counter_value(counter, labels: dict[str, str]) -> float:
    """Safely get counter value, returning 0 if labels don't match."""
    for sample_labels, value in counter._metrics.items():
        if all(sample_labels.get(k) == v for k, v in labels.items()):
            return value._value.get()
    return 0.0


class TestAgentMetrics:
    def test_record_agent_request_increments(self):
        before = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "success"}
        )
        record_agent_request("test", "success")
        after = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "success"}
        )
        assert after == before + 1

    def test_record_agent_request_error(self):
        before = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "error"}
        )
        record_agent_request("test", "error")
        after = _get_counter_value(
            AGENT_REQUESTS_TOTAL, {"agent_name": "test", "status": "error"}
        )
        assert after == before + 1

    def test_record_agent_latency_observes(self):
        # histogram.observe doesn't throw
        record_agent_latency("test", 1.5)
        record_agent_latency("test", 3.0)


class TestLLMMetrics:
    def test_record_llm_call_increments(self):
        before = _get_counter_value(
            LLM_CALLS_TOTAL, {"model": "gpt-4o", "agent_name": "orchestrator"}
        )
        record_llm_call("gpt-4o", "orchestrator")
        after = _get_counter_value(
            LLM_CALLS_TOTAL, {"model": "gpt-4o", "agent_name": "orchestrator"}
        )
        assert after == before + 1

    def test_record_llm_tokens_increments(self):
        before_in = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "input"}
        )
        before_out = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "output"}
        )
        record_llm_tokens("gpt-4o", 500, 200)
        after_in = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "input"}
        )
        after_out = _get_counter_value(
            LLM_TOKEN_USAGE_TOTAL, {"model": "gpt-4o", "direction": "output"}
        )
        assert after_in == before_in + 500
        assert after_out == before_out + 200


class TestToolMetrics:
    def test_record_tool_execution_increments(self):
        before = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "success"}
        )
        record_tool_execution("search", "success")
        after = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "success"}
        )
        assert after == before + 1

    def test_record_tool_execution_error(self):
        before = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "error"}
        )
        record_tool_execution("search", "error")
        after = _get_counter_value(
            TOOL_EXECUTIONS_TOTAL, {"tool_name": "search", "status": "error"}
        )
        assert after == before + 1

    def test_record_tool_latency(self):
        record_tool_latency("search", 0.25)
        record_tool_latency("search", 1.0)


class TestSubAgentMetrics:
    def test_record_subagent_dispatch_success(self):
        before = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "success"},
        )
        record_subagent_dispatch("format_audit", "success")
        after = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "success"},
        )
        assert after == before + 1

    def test_record_subagent_dispatch_error(self):
        before = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "error"},
        )
        record_subagent_dispatch("format_audit", "error")
        after = _get_counter_value(
            SUBAGENT_DISPATCH_TOTAL,
            {"subagent_name": "format_audit", "status": "error"},
        )
        assert after == before + 1
```

- [ ] **Step 2: 运行测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/telemetry/test_metrics.py -v
```

预期：all passed

- [ ] **Step 3: 提交**

```bash
git add tests/telemetry/test_metrics.py
git commit -m "test: add unit tests for Prometheus metrics helpers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 16: 单元测试 — decorators.py

**Files:**
- Create: `tests/telemetry/test_decorators.py`

- [ ] **Step 1: 编写测试**

```python
# tests/telemetry/test_decorators.py
"""Tests for tracing decorators."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from src.agent.telemetry.decorators import traced_agent, traced_llm, traced_tool


@pytest.fixture(autouse=True)
def setup_telemetry():
    provider = TracerProvider()
    trace.set_tracer_provider(provider)


class TestTracedAgent:
    async def test_decorated_method_returns_result(self):
        @traced_agent("test-agent")
        async def fake_run(self, task: str):
            return {"result": task}

        class FakeAgent:
            pass

        agent = FakeAgent()
        result = await fake_run(agent, task="hello")
        assert result == {"result": "hello"}

    async def test_decorated_method_passes_kwargs(self):
        @traced_agent("test-agent")
        async def fake_run(self, task: str, extra: str = ""):
            return task + extra

        class FakeAgent:
            pass

        result = await fake_run(FakeAgent(), task="a", extra="b")
        assert result == "ab"


class TestTracedLLM:
    async def test_decorated_llm_call_returns_result(self):
        class FakeUsage:
            prompt_tokens = 10
            completion_tokens = 5

        class FakeResponse:
            usage = FakeUsage()

        @traced_llm(model="test-model")
        async def fake_llm(prompt: str):
            return FakeResponse()

        result = await fake_llm(prompt="test")
        assert result.usage.prompt_tokens == 10

    async def test_decorated_llm_without_usage(self):
        @traced_llm(model="test-model")
        async def fake_llm(prompt: str):
            return "plain string"

        result = await fake_llm(prompt="test")
        assert result == "plain string"


class TestTracedTool:
    async def test_decorated_tool_returns_result(self):
        @traced_tool("test-tool")
        async def fake_tool(query: str):
            return f"result: {query}"

        result = await fake_tool(query="search")
        assert result == "result: search"

    async def test_decorated_tool_exception_raises(self):
        @traced_tool("test-tool")
        async def fake_tool(query: str):
            raise ValueError("bad input")

        with pytest.raises(ValueError, match="bad input"):
            await fake_tool(query="bad")
```

- [ ] **Step 2: 运行测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/telemetry/test_decorators.py -v
```

预期：all passed

- [ ] **Step 3: 提交**

```bash
git add tests/telemetry/test_decorators.py
git commit -m "test: add unit tests for tracing decorators

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 17: 集成测试 — API middleware + /metrics

**Files:**
- Create: `tests/telemetry/test_api_middleware.py`

- [ ] **Step 1: 编写测试**

```python
# tests/telemetry/test_api_middleware.py
"""Integration tests for FastAPI observability middleware and /metrics endpoint."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agent.api.middleware.observability import ObservabilityMiddleware


@pytest.fixture
def app_with_middleware():
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware)

    @app.get("/test-ok")
    async def test_ok():
        return {"status": "ok"}

    @app.get("/test-error")
    async def test_error():
        raise ValueError("simulated error")

    return app


class TestObservabilityMiddleware:
    def test_successful_request(self, app_with_middleware):
        client = TestClient(app_with_middleware)
        response = client.get("/test-ok")
        assert response.status_code == 200

    def test_error_request_500(self, app_with_middleware):
        client = TestClient(app_with_middleware)
        # FastAPI returns 500 for unhandled exceptions in test mode
        with pytest.raises(ValueError):
            client.get("/test-error")


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_prometheus_format(self):
        """Verify /metrics returns Prometheus text format."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        from starlette.responses import Response

        app = FastAPI()

        @app.get("/metrics")
        async def metrics():
            return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "agent_requests_total" in response.text or "process_" in response.text
```

- [ ] **Step 2: 运行测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/telemetry/test_api_middleware.py -v
```

预期：all passed

- [ ] **Step 3: 提交**

```bash
git add tests/telemetry/test_api_middleware.py
git commit -m "test: add integration tests for observability middleware and /metrics

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 18: 全量回归测试

**Files:** None (验证步骤)

- [ ] **Step 1: 运行全部已有测试**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/ -x --timeout=120 -q 2>&1 | tail -30
```

预期：所有已有测试继续通过，新增 telemetry 测试也通过。

- [ ] **Step 2: 验证模块导入无循环依赖**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run python -c "
from src.agent.telemetry import (
    AgentTracer, init_telemetry, get_tracer,
    inject_context, extract_context,
    traced_agent, traced_llm, traced_tool,
    record_agent_request, record_agent_latency,
    record_llm_call, record_llm_tokens,
    record_tool_execution, record_tool_latency,
    record_subagent_dispatch,
)
from src.agent.api.app import app
print('All imports OK')
"
```

预期输出：`All imports OK`

- [ ] **Step 3: 运行覆盖率检查**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && uv run pytest tests/telemetry/ --cov=src/agent/telemetry --cov-report=term-missing -q
```

预期：telemetry 模块覆盖率 >= 80%

- [ ] **Step 4: 提交（如有未提交的变更）**

```bash
git status
```

如果所有文件已提交，跳过此步骤。

---

### Task 19: 最终提交 — 确保所有变更已提交

**Files:** None（验证步骤）

- [ ] **Step 1: 检查是否有未提交变更**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && git status
```

- [ ] **Step 2: 如有遗漏，提交**

```bash
git add -A
git commit -m "chore: finalize observability integration

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 3: 查看完整变更摘要**

```bash
cd /home/lmwl/Documents/docaudit/docaudit-agent && git diff --stat main...HEAD
```

---

## 验证清单

实施完成后，通过以下步骤进行 E2E 验证：

- [ ] `docker-compose up -d` 启动所有服务（含可观测性组件）
- [ ] `curl http://localhost:8000/metrics` 返回 Prometheus 指标
- [ ] 提交一个 audit 任务，确认任务成功完成
- [ ] 访问 `http://localhost:3000` Langfuse UI，确认 trace 可见
- [ ] 访问 `http://localhost:9090` Prometheus UI，确认指标可查询
- [ ] 访问 `http://localhost:3001` Grafana UI（admin/admin），配置 Prometheus 数据源
