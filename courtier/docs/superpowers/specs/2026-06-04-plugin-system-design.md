# Plugin System Design

**Date:** 2026-06-04
**Status:** Draft
**Author:** Hu Lan

## 1. Overview

Design a full-stack plugin system for `docaudit-agent` that allows third-party developers to extend the system with new tools, agents, content checkers, document processors, and API routes.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope | Full-stack (tools, agents, checkers, processors, routes) | User requirement |
| Distribution | File-system directory (`plugins/`) | Consistent with existing skills system; no PyPI publishing needed |
| Declaration | Manifest file (`plugin.yaml`) + auto-discovery | Explicit metadata for validation; directory structure for convention |
| Isolation | Subprocess per plugin | Crash safety, dependency isolation |
| Communication | stdio JSON-RPC (LSP-like) | Simple, debuggable, no network stack, auto-cleanup on parent exit |

### References

- [LangBot 4.0 Plugin System](https://dev.to/rockchinq/deep-dive-into-the-langbot-plugin-system-process-isolation-event-driven-hooks-component-2b4m) — production-validated subprocess + JSON-RPC pattern
- [IBM ContextForge Plugin Framework](https://ibm.github.io/mcp-context-forge/1.0.0/architecture/plugins/) — MCP-native plugin specification
- [Python plugin architecture best practices](https://discuss.python.org/t/best-practices-for-managing-dynamic-imports-in-plugin-based-architecture/99482)

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      主进程 (Host)                          │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Plugin   │  │ Extension │  │ JSON-RPC │  │ Process    │  │
│  │ Scanner  │  │ Registry  │  │ Client   │  │ Manager    │  │
│  │          │  │           │  │ Pool     │  │            │  │
│  │ scans    │  │ routes    │  │ manages  │  │ lifecycle: │  │
│  │ plugins/ │  │ caps to   │  │ comms    │  │ spawn/     │  │
│  │ dirs     │  │ registries│  │          │  │ health/    │  │
│  └──────────┘  └──────────┘  └──────────┘  │ kill       │  │
│                                             └────────────┘  │
│  现有模块（不变）                                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │ToolRegistry│ │CheckerReg│  │FastAPI   │  ...             │
│  └──────────┘  └──────────┘  └──────────┘                  │
└─────────────────────────────────────────────────────────────┘
          │  stdio JSON-RPC          │  stdio JSON-RPC
          ▼                          ▼
┌──────────────────┐    ┌──────────────────┐
│  Plugin A 子进程  │    │  Plugin B 子进程  │
│                  │    │                  │
│  ┌────────────┐  │    │  ┌────────────┐  │
│  │ JSON-RPC   │  │    │  │ JSON-RPC   │  │
│  │ Server     │  │    │  │ Server     │  │
│  └────────────┘  │    │  └────────────┘  │
│  ┌────────────┐  │    │  ┌────────────┐  │
│  │ Tool/Agent │  │    │  │ Checker    │  │
│  │ impl       │  │    │  │ impl       │  │
│  └────────────┘  │    │  └────────────┘  │
└──────────────────┘    └──────────────────┘
```

### Core Components

| Component | Location | Responsibility |
|-----------|----------|---------------|
| `PluginScanner` | Host | Scan `plugins/` directory, parse `plugin.yaml`, validate |
| `ExtensionRegistry` | Host | Receive plugin register notifications, route capabilities to existing registries |
| `JSONRPCClientPool` | Host | Manage JSON-RPC connections to plugin subprocesses, request/response matching |
| `ProcessManager` | Host | Subprocess spawn, health check, graceful shutdown, crash restart |
| `PluginRuntime` (SDK) | Plugin side | Base class for plugin entry point, wraps JSON-RPC server and registration logic |

### Directory Structure

```
docaudit-agent/
├── plugins/                          # Plugin root directory
│   └── my_auditor/                   # Single plugin
│       ├── plugin.yaml               # Manifest file
│       ├── entry.py                  # Entry point: JSON-RPC server
│       └── ...                       # Plugin-specific code
├── src/
│   └── plugin/                       # Plugin system core (new)
│       ├── __init__.py
│       ├── scanner.py                # PluginScanner
│       ├── registry.py               # ExtensionRegistry
│       ├── client.py                 # JSONRPCClient / ClientPool
│       ├── manager.py                # ProcessManager
│       ├── protocol.py               # JSON-RPC message types
│       ├── proxies.py                # Proxy objects (ProxyTool, ProxyChecker, ProxyAgent)
│       └── sdk/                      # Plugin SDK (for plugin authors)
│           ├── __init__.py
│           ├── runtime.py            # PluginRuntime base class
│           └── protocol.py           # Protocol constants
```

## 3. JSON-RPC Protocol

### 3.1 Message Format (JSON-RPC 2.0 style)

Communication over stdio (stdin/stdout), one JSON object per line.

**Request:**
```json
{
  "id": 1,
  "method": "tool.execute",
  "params": {
    "tool": "check_format",
    "args": {"text": "...", "subtype": "通知"}
  }
}
```

**Success Response:**
```json
{
  "id": 1,
  "result": {
    "success": true,
    "data": {"is_valid": true, "violations": []}
  }
}
```

**Error Response:**
```json
{
  "id": 1,
  "error": {"code": -32000, "message": "Tool not found: check_format"}
}
```

**Notification (no response needed):**
```json
{
  "method": "plugin.register",
  "params": {
    "capabilities": [
      {"type": "tool", "name": "check_format", "display_name": "格式检查"},
      {"type": "checker", "doc_type": "通知"}
    ]
  }
}
```

**Streaming Response (long-running tasks like agent.run):**
```json
{"id": 2, "chunk": "partial result", "status": "continue"}
{"id": 2, "chunk": "more...", "status": "continue"}
{"id": 2, "result": {"success": true, "data": {...}}, "status": "end"}
```

### 3.2 Method Namespace

All methods use `domain.action` format, mapping to existing interfaces:

| Method | Direction | Maps To | Description |
|--------|-----------|---------|-------------|
| `plugin.register` | P→H notification | — | Plugin declares capabilities after startup |
| `plugin.health` | H→P | — | Health check ping/pong |
| `plugin.shutdown` | H→P notification | — | Notify plugin to gracefully exit |
| `tool.list` | H→P | `ToolProtocol` | List tools and their schemas |
| `tool.execute` | H→P | `ToolProtocol.execute()` | Execute a tool |
| `checker.list` | H→P | `ContentChecker` | List checkers |
| `checker.check` | H→P | `ContentChecker.check()` | Run compliance check |
| `agent.list` | H→P | `Agent` | List agents |
| `agent.run` | H→P | `Agent.run()` | Streaming, run an agent |
| `route.list` | H→P | FastAPI routes | List API routes |
| `route.handle` | H→P | — | Proxy HTTP request to plugin |
| `processor.list` | H→P | Parser/Corrector | List document processors |
| `processor.process` | H→P | — | Execute document processing |

### 3.3 Connection Lifecycle

```
Host startup
  │
  ├─ Scan plugins/*/plugin.yaml, validate manifests
  │
  ├─ For each enabled plugin:
  │    ├─ spawn: uv run entry.py (subprocess starts)
  │    ├─ Wait for plugin.register notification
  │    ├─ Inject registered capabilities into corresponding registries
  │    └─ Mark state: ACTIVE
  │
  │  Runtime:
  │    ├─ Periodic plugin.health ping (30s interval)
  │    ├─ On-demand tool.execute / checker.check / agent.run calls
  │    └─ Subprocess crash → auto-restart + re-register
  │
  │  Host shutdown:
  │    └─ Send plugin.shutdown → wait for exit → force kill (5s timeout)
```

## 4. Plugin Manifest (plugin.yaml)

```yaml
# plugin.yaml — required manifest at plugin root
# e.g., plugins/format_auditor/plugin.yaml

# ── Basic Info ──
name: format_auditor              # Unique ID, must match directory name
version: "0.1.0"                  # SemVer
api: "1.0"                        # Target host API version
description: "公文格式审计插件"
author: "..."
license: MIT

# ── Runtime Config ──
runtime:
  language: python                # Currently python only
  entry: entry.py                 # Entry point relative to plugin dir
  env:                            # Optional: plugin-specific env vars
    MY_API_KEY: "${ENV:MY_API_KEY}"  # Injected from host env

# ── Dependencies ──
dependencies:
  python:                         # PyPI dependencies
    - "httpx>=0.28.0"
    - "lxml"
  host_services:                  # Host services the plugin needs
    - "cache"                     # CacheStore
    - "artifact_store"            # ArtifactStore
  permissions:                    # Required permissions
    - "read:documents"
    - "write:artifacts"

# ── Capabilities ──
capabilities:

  tools:                          # Tools (ToolProtocol)
    - name: check_format
      display_name: "格式检查"
      description: "检查公文格式是否符合规范"
      input_contract:
        fields:
          - name: text
            type: string
            required: true
      output_contract:
        outputs:
          - artifact_type: FormatAuditReport
            role: audit_result

  checkers:                       # Content checkers (ContentChecker)
    - name: sensitive_word_check
      doc_type: "通知"
      display_name: "敏感词检查"

  agents:                         # Agents (Agent/AuditorAgent)
    - name: my_custom_auditor
      display_name: "自定义审计员"
      role: "你是一个专业的内容审计员..."
      skill_names:
        - content_audit

  routes:                         # API routes (FastAPI)
    - prefix: /api/v1/plugins/custom
      description: "自定义统计接口"

  processors:                     # Document processors
    - name: custom_parser
      type: parser                 # parser | corrector | builder
      display_name: "自定义文档解析器"
```

### Manifest Validation Rules

- `api` version compatibility checked at scan time; incompatible plugins marked BLOCKED
- `name` must match directory name; mismatch → BLOCKED
- `runtime.entry` must exist; missing → BLOCKED
- All declared capabilities must have at minimum a `name` field
- Undeclared capabilities in the register notification are ignored with a WARNING

## 5. ExtensionRegistry — Capability Routing

The glue layer between plugin subprocesses and existing registries. Core mechanism: **Proxy objects** that implement existing Protocols but forward calls over JSON-RPC.

### 5.1 Proxy Pattern

```
┌──────────────────────────────────────────────────────────────────┐
│                        Host Process                              │
│                                                                  │
│  ToolRegistry                    ExtensionRegistry               │
│  ┌────────────┐                  ┌─────────────────┐             │
│  │ EchoTool   │ ← local tool     │                 │             │
│  │            │                  │  on_register()   │             │
│  │ ProxyTool  │ ← proxy ←────────│    constructs    │             │
│  │            │                  │    proxy objects │             │
│  └────────────┘                  │    and injects   │             │
│                                  │    them into     │             │
│  CheckerRegistry                 │    registries    │             │
│  ┌────────────┐                  └─────────────────┘             │
│  │ RuleBased  │ ← local                ▲                        │
│  │ Checker    │                       │ JSON-RPC                │
│  │            │                       │ Client Pool             │
│  │ Proxy      │ ← proxy ←─────────────┤                        │
│  │ Checker    │                                                    │
│  └────────────┘                                                    │
└──────────────────────────────────────────────────────────────────┘
```

### 5.2 Proxy Object Implementations

Proxy objects implement the corresponding Protocol (`ToolProtocol`, `ContentChecker`, `Agent`) and forward calls over JSON-RPC:

- **`ProxyTool`** — implements `ToolProtocol`; `execute()` → `tool.execute` JSON-RPC call
- **`ProxyChecker`** — implements `ContentChecker` Protocol; `check()` → `checker.check` JSON-RPC call
- **`ProxyAgent`** — implements Agent; `run()` → `agent.run` streaming JSON-RPC call
- **`ProxyRoute`** — wraps FastAPI route handling

### 5.3 Key Design Decisions

1. **Lazy connection** — no JSON-RPC call is made until a proxy method is actually invoked
2. **Serialization round-trips through Pydantic/dataclass** — `ToolResult`, `ComplianceResult` remain the source of truth; proxies serialize/deserialize at the JSON boundary
3. **Error passthrough** — plugin errors map directly to `ToolResult(success=False)` or appropriate exceptions
4. **Capability dedup** — same-named tools from different plugins: last registered wins with WARNING (consistent with existing `ToolRegistry.register()`)

## 6. ProcessManager — Lifecycle Management

### 6.1 Plugin State Machine

```
                    ┌──────────┐
                    │ SCANNED  │  plugin.yaml parsed successfully
                    └────┬─────┘
                         ↓
                    ┌──────────┐
              ┌─────│ LOADING  │  Subprocess starting
              │     └────┬─────┘
              │          ↓
              │     ┌──────────┐
              │     │REGISTERING│  Waiting for plugin.register notification
              │     └────┬─────┘
              │          ↓
              │     ┌──────────┐
              │     │  ACTIVE  │  Running, accepting requests ←──┐
              │     └────┬─────┘                                 │
              │          │                                       │
              │          ├─ Crash/timeout ──┐                    │
              │          │                 ↓                    │
              │          │          ┌──────────┐                │
              │          │          │  CRASHED │                │
              │          │          └────┬─────┘                │
              │          │               │                       │
              │          │          ┌──────────┐                │
              │          │          │RESTARTING│ Auto-restart    │
              │          │          └────┬─────┘                │
              │          │               │ max 3 retries         │
              │          │               ↓                       │
              │          │          ┌──────────┐                │
              │          │          │  FATAL   │ Exceeded retry  │
              │          │          └──────────┘ limit           │
              │          │                                       │
              │          ↓                                       │
              │     ┌──────────┐                                 │
              └─────│ STOPPING │  Shutdown signal received       │
                    └────┬─────┘                                 │
                         ↓                                       │
                    ┌──────────┐                                 │
                    │ STOPPED  │  Subprocess exited              │
                    └──────────┘                                 │
```

### 6.2 Key Decisions

| Decision | Rationale |
|----------|-----------|
| Start all plugins at host startup | Known full picture; lazy loading adds complexity with minimal benefit |
| Auto-restart on crash, max 3 times | Transient failures self-heal; persistent failures shouldn't loop forever |
| Exponential backoff: 1s→2s→4s→...→30s | Avoid rapid restart cycles |
| Health check every 30s | Balance detection speed vs. communication overhead. Stderr EOF gives real-time crash detection |
| Unregister on crash | Proxy objects removed from registries; subsequent calls get clear errors, not timeouts |
| Graceful shutdown: 5s timeout then force kill | Prevent zombie processes |

### 6.3 ProcessManager Core Logic

- `start_all(plugins)` — iterate manifests, spawn subprocesses, wait for register
- `_start_one(proc)` — ensure deps, spawn `uv run entry.py`, create JSONRPCClient, wait for register notification
- `_on_crash(proc)` — unregister, auto-restart with backoff, or mark FATAL after exceeding retry limit
- `shutdown()` — send shutdown to all plugins, wait 5s, force kill stragglers

## 7. Error Handling

### 7.1 Exception Classification

| Error Type | Handling | Plugin State |
|------------|----------|--------------|
| Protocol error (malformed JSON, unknown method) | Return JSON-RPC error, log WARNING | Active (unchanged) |
| Business error (tool execution failed) | Wrap as `ToolResult(success=False)` | Active (unchanged) |
| Timeout (single call exceeds limit) | Return timeout error to caller | Active (unchanged) |
| Process exit (crash/OOM/killed) | ProcessManager detects stdout EOF, triggers restart | CRASHED → RESTARTING or FATAL |

### 7.2 Timeout Strategy

| Call Type | Default Timeout | Configurable | Rationale |
|-----------|----------------|--------------|-----------|
| `tool.execute` | 30s | Yes | Most tools are synchronous |
| `checker.check` | 10s | Yes | Rule checking is fast |
| `agent.run` | Unlimited | Yes | Streaming; heartbeat timeout at 60s of silence |
| `plugin.health` | 5s | No | Fast detection, fixed |
| `plugin.register` | 10s | No | Plugin init should be fast |

### 7.3 Partial Failure Scenarios

- **Some plugins fail to load at startup** → ERROR log, other plugins start normally. Failed plugins show FATAL status via `/api/plugins/status`
- **Plugin crashes mid-call** → in-flight calls receive `PluginCrashedError`; callers may retry after restart
- **Plugin registers undeclared capability** → ignored with WARNING. Only `plugin.yaml`-declared capabilities are honored
- **Plugin times out on register notification** → FATAL after 10s with clear error message
- **Two plugins register same-named tool** → last registered wins with WARNING (consistent with existing behavior)

### 7.4 Plugin-Side Exception Isolation

The `PluginRuntime` SDK base class wraps the JSON-RPC server loop with exception protection:
- Malformed JSON lines are skipped with error logging (process stays alive)
- Unhandled exceptions in handlers return error responses (process stays alive)
- Only fatal errors (SystemExit, unrecoverable state) cause process exit

### 7.5 Error Message Hierarchy

**User/caller-facing:**
- "格式检查失败：缺少必填字段 'title'"
- "插件 'my_auditor' 不可用，正在重启中，请稍后重试"
- "插件通信超时（30s），请检查任务是否过于复杂"

**Developer/ops-facing (logs):**
- `ERROR Plugin 'my_auditor' crashed (signal=11, exit_code=-11), restart 1/3 in 1s`
- `WARNING Plugin 'format_auditor' registered undeclared capability: checker.my_checker`
- `ERROR Plugin 'bad_plugin' did not send register notification within 10s, marking FATAL`

## 8. Testing Strategy

### 8.1 Test Layers

```
┌─────────────────────────────────────────────┐
│  Integration (real subprocess + uv run)      │
│  - Full flow: start → register → call → stop │
│  - Crash recovery: kill subprocess, verify   │
│    auto-restart                              │
│  - Timeout scenarios: block plugin, verify   │
│    timeout handling                          │
├─────────────────────────────────────────────┤
│  Component (in-memory JSON-RPC)              │
│  - Proxy object correctness and error mapping│
│  - ExtensionRegistry routing logic           │
│  - ProcessManager state machine transitions  │
├─────────────────────────────────────────────┤
│  Unit (pure functions and types)             │
│  - PluginScanner manifest parsing            │
│  - JSON-RPC message serialization            │
│  - PluginManifest model validation           │
└─────────────────────────────────────────────┘
```

### 8.2 Test Fixtures

```
tests/fixtures/plugins/
├── echo_plugin/                     # Minimal valid plugin
│   ├── plugin.yaml
│   └── entry.py                     # Echo tool + 1 checker
├── bad_no_manifest/                 # No manifest (should be skipped)
│   └── entry.py
├── bad_invalid_yaml/                # Malformed manifest
│   └── plugin.yaml                  # Intentionally broken YAML
├── crashing_plugin/                 # Crashes on startup
│   ├── plugin.yaml
│   └── entry.py                     # raise SystemExit(1)
├── slow_register/                   # Register timeout
│   ├── plugin.yaml
│   └── entry.py                     # time.sleep(30)
└── timeout_tool/                    # Tool execution timeout
    ├── plugin.yaml
    └── entry.py                     # Tool that never returns
```

### 8.3 Key Test Cases

| Scenario | Layer | Verification |
|----------|-------|-------------|
| Scan empty plugins directory | Unit | Empty list, no exception |
| Manifest name ≠ directory name | Unit | BLOCKED + clear error |
| API version incompatible | Unit | BLOCKED + "requires API v2, host is v1" |
| Start echo_plugin | Integration | ACTIVE state, tool callable |
| Call echo tool | Integration | Proxy round-trips correctly, result matches |
| Call checker.check | Integration | ComplianceResult deserialized correctly |
| Two plugins, same tool name | Component | Last registered wins, WARNING logged |
| Kill subprocess → restart | Integration | Successful restart, counter reset |
| Crash 3 times consecutively → FATAL | Integration | No further restarts, FATAL state |
| Register timeout 10s | Integration | FATAL + clear error message |
| Tool execution timeout | Component | `ToolResult(success=False, error="timeout")` |
| Streaming agent.run | Integration | Chunks arrive sequentially, final end |
| Graceful shutdown | Integration | Subprocess receives shutdown, exits cleanly |
| Shutdown timeout force kill | Integration | 5s timeout then kill, no zombie |

### 8.4 Relationship to Existing Tests

- Existing module tests (`ToolRegistry`, `CheckerRegistry`, `Agent`) remain unchanged
- New plugin module tests go in `tests/plugin/`
- Plugin system does not change existing public API behavior

## 9. Open Questions

1. **Plugin venv management**: Should each plugin get its own `.venv` in the plugin directory, or use a shared venv with all plugin deps merged? (Recommendation: per-plugin venv for dependency isolation)
2. **Plugin API versioning**: How to handle API version bumps in the host? (Recommendation: start with `api: "1.0"`, bump when protocol methods change; host can support multiple API versions simultaneously)
3. **Plugin configuration**: Should plugins have their own config files, or use environment variables only? (Recommendation: env vars in plugin.yaml for now; revisit when needed)
