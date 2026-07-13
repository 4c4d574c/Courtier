# docaudit-agent 可观测性方案设计

> **方案：** 完整 OTel 手动插桩 + Langfuse Traces + Prometheus Metrics + Grafana Dashboard
> **日期：** 2026-06-09
> **参考：** agent_observability_framework_agnostic.md

---

## 1. 背景与现状

### 已有能力

| 能力 | 状态 | 说明 |
|------|------|------|
| `AuditLogger` | ✅ 已有 | 基于文件，按 agent/回合记录 JSON（LLM 请求/响应/工具调用） |
| `StructuredLogHandler` | ✅ 已有 | JSON Lines 格式结构化日志 |
| 集中式 logging 配置 | ✅ 已有 | 通过 `configure_logging()` 控制 |
| 分布式追踪 (Traces) | ❌ 缺失 | |
| 运行时指标 (Metrics) | ❌ 缺失 | |
| 跨进程 trace 传播 | ❌ 缺失 | |

### 核心痛点

- 无法追踪一次 audit 任务端到端耗时分布
- 无法定位哪个 sub-agent 导致任务失败
- 无法量化 token 消耗、LLM 调用延迟
- 跨 orchestrator → plugin 进程边界调用链路断裂
- 多 agent 协作时缺少统一的可视化视图

---

## 2. 目标

- **Traces：** 覆盖 agent loop、LLM 调用、工具执行、sub-agent 派发全链路
- **Metrics：** 请求量、延迟、token 用量、工具调用成功率
- **Logs：** 保留现有 AuditLogger，与 OTel traces 通过 trace_id/session_id 关联
- **跨进程：** orchestrator 与 plugin 子进程间通过 W3C TraceContext 标准传播
- **可视化：** Langfuse（LLM/Agent 专项）+ Grafana（通用 Dashboard）

---

## 3. 架构

```
┌─────────────────────────────────────────────────────────┐
│                    docaudit-agent                        │
│                                                          │
│  FastAPI ──→ OrchestratorAgent ──→ agent_loop           │
│                  │                    │                  │
│                  │         ┌─────────┴─────────┐        │
│                  │         │  think → gate →    │        │
│                  │         │  act → observe     │        │
│                  │         └─────────┬─────────┘        │
│                  │                   │                   │
│                  │    SubAgentRunner │                   │
│                  │    ┌──────────────┴──────────────┐   │
│                  │    │  ProxyAgent → JSONRPCClient  │   │
│                  │    │  (stdio to plugin subprocess) │  │
│                  │    └──────────────────────────────┘   │
│                  │                                       │
│  ┌───────────────┴───────────────────────────────────┐  │
│  │              OTel SDK (手动插桩)                    │  │
│  │  • agent_span / llm_span / tool_span / plugin_span │  │
│  │  • W3C TraceContext propagation (via JSON-RPC)     │  │
│  │  • Prometheus metrics (requests, latency, tokens)  │  │
│  └───────────────┬───────────────────────────────────┘  │
└──────────────────┼──────────────────────────────────────┘
                   │ OTLP gRPC (4317)
                   ▼
┌─────────────────────────────────────────────────────────┐
│              OTel Collector                              │
│  receivers: OTLP (4317/4318)                             │
│  processors: batch, memory_limiter                       │
│  exporters: otlp → Langfuse, prometheus (8888)           │
└────────┬────────────────────┬───────────────────────────┘
         │                    │
         ▼                    ▼
┌─────────────────┐  ┌──────────────────┐
│    Langfuse      │  │    Prometheus    │
│  (Traces + LLM)  │  │    (Metrics)     │
│  port 3000       │  │    port 9090     │
└────────┬─────────┘  └────────┬─────────┘
         │                     │
    ┌────┴─────┐               ▼
    │ Postgres │        ┌──────────────┐
    │ ClickHouse│        │   Grafana    │
    │ Redis    │        │  port 3001   │
    └──────────┘        └──────────────┘
```

### Span 层级

```
invoke_agent orchestrator (root span)
├── chat qwen (LLM call, turn 0)
│   ├── gen_ai.content.prompt
│   └── gen_ai.content.completion
├── dispatch_subagent format_audit (cross-process)
│   ├── chat qwen (subagent's LLM call, turn 0)
│   │   ├── execute_tool check_font
│   │   └── execute_tool check_margin
│   └── ...
├── dispatch_subagent content_audit
│   └── ...
├── execute_tool persist_output
└── ...
```

---

## 4. 核心 Telemetry 层

### 4.1 AgentTracer

核心类，所有方法直接调用 `opentelemetry.trace` API，无中间框架。

| Span 类型 | Context Manager | 操作名 | 关键属性 |
|-----------|----------------|--------|----------|
| Agent | `tracer.agent_span(name)` | `invoke_agent {name}` | agent.name, agent.session_id, agent.iterations, agent.total_tokens |
| LLM | `tracer.llm_span(model)` | `chat {model}` | gen_ai.request.model, gen_ai.usage.*, gen_ai.content.prompt/completion (events) |
| Tool | `tracer.tool_span(name)` | `execute_tool {name}` | gen_ai.tool.name, gen_ai.tool.parameters, gen_ai.tool.latency_ms, gen_ai.tool.status |
| SubAgent | `tracer.plugin_span(name)` | `dispatch_subagent {name}` | subagent.name, subagent.status, subagent.latency_ms |

每个 context manager 自动处理：
- 开始时间戳记录
- 异常时 `record_exception` + `set_status(ERROR)`
- 成功时 `set_status(OK)`

### 4.2 装饰器

```python
# 非侵入式插桩
@traced_agent("orchestrator")
@traced_llm(model="qwen3.6-27b")
@traced_tool("check_font")
```

适用场景：简单调用链路；复杂场景（agent_loop 多阶段）直接使用 context manager。

### 4.3 跨进程 TraceContext 传播

```
Orchestrator (父进程)
  │  1. tracer.plugin_span("format_audit") → span
  │  2. inject_context(carrier) → W3C TraceContext
  │  3. carrier 写入 JSON-RPC agent.run params["trace_context"]
  │  4. JSONRPCClient.stream("agent.run", params)
  ▼
Plugin subprocess (子进程)
  │  5. params["trace_context"] 提取
  │  6. parent_ctx = extract_context(carrier)
  │  7. tracer.start_as_current_span(..., context=parent_ctx)
  │     → Langfuse 中显示为正确父子关系
```

**关键点：** 利用 JSON-RPC params 传播（非 HTTP header），向后兼容——`trace_context` 为可选字段。

### 4.4 Metrics

| 指标 | 类型 | 标签 | 用途 |
|------|------|------|------|
| `agent_requests_total` | Counter | agent_name, status | 请求量 |
| `agent_latency_seconds` | Histogram | agent_name | 端到端延迟 |
| `llm_calls_total` | Counter | model, agent_name | LLM 调用次数 |
| `llm_token_usage_total` | Counter | model, direction | Token 消耗 |
| `tool_executions_total` | Counter | tool_name, status | 工具调用量 |
| `tool_latency_seconds` | Histogram | tool_name | 工具延迟 |
| `subagent_dispatch_total` | Counter | subagent_name, status | 子 agent 派发量 |
| `plugin_state_changes` | Gauge | plugin_name, state | 插件状态 |

端点：`GET /metrics`（Prometheus text format）

---

## 5. 代码改动

### 5.1 新增文件

| 文件 | 职责 | 估计行数 |
|------|------|----------|
| `src/agent/telemetry/__init__.py` | 公开 API 导出 | ~20 |
| `src/agent/telemetry/tracer.py` | `AgentTracer`，`init_telemetry()`，`get_tracer()` | ~350 |
| `src/agent/telemetry/metrics.py` | Prometheus 指标定义和辅助函数 | ~120 |
| `src/agent/telemetry/decorators.py` | `traced_agent` / `traced_llm` / `traced_tool` | ~150 |
| `src/agent/telemetry/context.py` | `inject_context()` / `extract_context()` | ~40 |
| `src/agent/api/middleware/observability.py` | FastAPI 中间件 | ~60 |
| `otel-collector-config.yaml` | OTel Collector 配置 | ~40 |
| `prometheus.yml` | Prometheus scrape 配置 | ~15 |

### 5.2 修改文件

| 文件 | 改动 | 影响 |
|------|------|------|
| `pyproject.toml` | 新增 4 个依赖 | 包依赖 |
| `src/config.py` | 新增 3 个 OTel 配置项 | ~10 行 |
| `src/agent/api/app.py` | 启动时 init_telemetry()；注册 middleware；挂载 /metrics | ~30 行 |
| `src/agent/core/loop.py` | agent span 包裹循环；llm span 包裹 think；tool span 包裹 act | ~60 行 |
| `src/agent/agents/orch.py` | orchestrator span 入口 | ~20 行 |
| `src/agent/agents/subagent/runner.py` | dispatch_subagent span；inject_context() | ~30 行 |
| `src/plugin/client.py` | params 传递 trace_context | ~5 行 |
| `src/plugin/proxies.py` | ProxyAgent 侧 extract_context() + 创建子 span | ~25 行 |
| `docker-compose.yml` | 新增 8 个可观测性服务 | ~100 行 |

### 5.3 改动原则

- **loop.py 改动最小化**：仅添加 span 包裹和属性设置，不改变控制流
- **装饰器优先用于独立函数**，context manager 用于复杂流程
- **向后兼容**：`trace_context` 为 JSON-RPC 可选参数
- **AuditLogger 不受影响**：OTel traces 与文件审计日志共存互补

---

## 6. 基础设施

### Docker Compose 新增服务

| 服务 | 镜像 | 端口 |
|------|------|------|
| otel-collector | `otel/opentelemetry-collector-contrib:0.115.0` | 4317, 4318, 8888 |
| langfuse | `langfuse/langfuse:3` | 3000 |
| langfuse-worker | `langfuse/langfuse:3` (worker) | — |
| postgres | `postgres:16-alpine` | — |
| clickhouse | `clickhouse/clickhouse-server:24` | — |
| redis | `redis:7-alpine` | — |
| prometheus | `prom/prometheus:v2.55.0` | 9090 |
| grafana | `grafana/grafana:11.3.0` | 3001 |

所有服务加入已有 `docaudit` 网络。

### 环境变量

```bash
# OTel
OTEL_SERVICE_NAME=docaudit-agent
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
OTEL_LOG_LEVEL=INFO

# Langfuse（Collector 配置中使用）
LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY}
LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY}
LANGFUSE_HOST=http://langfuse:3000

# Grafana
GRAFANA_PASSWORD=${GRAFANA_PASSWORD:-admin}
```

---

## 7. 与现有 AuditLogger 的关系

| 维度 | AuditLogger（现有） | OTel Traces（新增） |
|------|-------------------|-------------------|
| 存储格式 | 文件系统 JSON | Langfuse (ClickHouse + Postgres) |
| 查看方式 | 直接读文件 | Langfuse UI |
| 跨进程关联 | ❌ | ✅ 通过 TraceContext |
| 内容粒度 | 完整 prompt/completion | 摘要 + events（可脱敏） |
| Token 统计 | 回合级别 | Span 级别 + Prometheus |
| 保留策略 | 手动清理 | Langfuse 配置 |

两者共存：`AuditLogger` 保留用于详细审计和调试；OTel 用于实时监控、性能分析和跨进程追踪。通过 `session_id` 和 `trace_id` 关联。

---

## 8. 测试策略

### 单元测试

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/telemetry/test_tracer.py` | AgentTracer 四种 span 的创建/属性设置/异常处理；init_telemetry() 幂等性 |
| `tests/telemetry/test_metrics.py` | Counter/Histogram/Gauge 的 inc/observe/set 行为 |
| `tests/telemetry/test_decorators.py` | traced_agent / traced_llm / traced_tool 装饰器行为 |
| `tests/telemetry/test_context.py` | inject_context / extract_context 往返测试 |

### 集成测试

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/telemetry/test_cross_process.py` | 模拟 orchestrator → plugin 的 trace context 传播，验证父子 span 关系 |
| `tests/telemetry/test_api_middleware.py` | FastAPI middleware 的 span 创建、请求计数、/metrics 端点 |

### E2E 验证

- 启动完整 docker-compose（含可观测性服务）
- 提交一个 audit 任务，验证：
  - Langfuse UI 中可见完整 trace（含 sub-agent 层级）
  - Prometheus `/metrics` 端点可抓取指标
  - Grafana 展示 dashboard

---

## 9. 实施顺序

1. **基础设施层** — docker-compose 新增可观测性服务 + OTel Collector 配置
2. **Telemetry 封装层** — `src/agent/telemetry/` 全部新增文件
3. **核心集成** — loop.py → orch.py → runner.py → proxies.py → client.py
4. **API 层** — app.py middleware + /metrics 端点 + config.py 配置项
5. **验证** — 端到端测试确认 Langfuse/Prometheus/Grafana 数据通路正常
