# Courtier Plugin Development Guide

## Overview

Plugins are standalone subprocesses communicating via JSON-RPC 2.0 over stdio.
Each plugin provides one or more tools that the agent can invoke.

## Architecture

```
┌──────────────┐     JSON-RPC 2.0     ┌──────────────┐
│  Courtier    │ ◄──────────────────► │   Plugin     │
│  Core        │      stdin/stdout     │  (subprocess)│
└──────────────┘                      └──────────────┘
```

The core launches each plugin as a subprocess. Communication uses
newline-delimited JSON-RPC 2.0 messages over stdin/stdout.

## Quick Start

1. Create a plugin directory under your domain's `plugins/`:

```
my-domain/plugins/my_tool/
├── plugin.yaml     # Plugin manifest
├── entry.py        # Plugin entry point
└── pyproject.toml  # Plugin dependencies (optional)
```

2. Write `plugin.yaml`:

```yaml
name: my_tool
version: "1.0"
type: tool
description: "My custom tool for document processing"
timeout_ms: 120000
tools:
  - name: do_something
    description: "Perform a custom operation"
    parameters:
      type: object
      properties:
        input_data:
          type: string
          description: "Input data to process"
      required: [input_data]
```

3. Write `entry.py`:

```python
"""Plugin entry point — JSON-RPC 2.0 over stdio."""
import json
import sys


def handle_request(method: str, params: dict) -> dict:
    """Dispatch JSON-RPC method calls."""
    if method == "health":
        return {"status": "ok", "dependencies": {}}
    if method == "tools/list":
        return {"tools": [...]}  # Your tools
    if method == "do_something":
        data = params.get("input_data", "")
        result = process(data)
        return {"result": result}
    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}


def process(data: str) -> str:
    """Your tool logic here."""
    return f"Processed: {data}"


def main():
    """Main loop — read JSON-RPC requests from stdin, write responses to stdout."""
    for line in sys.stdin:
        try:
            request = json.loads(line.strip())
            response = handle_request(
                request.get("method", ""),
                request.get("params", {}),
            )
            response["jsonrpc"] = "2.0"
            response["id"] = request.get("id")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            error_response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if "request" in dir() else None,
                "error": {"code": -32603, "message": str(exc)},
            }
            sys.stdout.write(json.dumps(error_response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
```

## plugin.yaml Reference

| Field | Required | Type | Default | Description |
|-------|----------|------|---------|-------------|
| name | Yes | string | — | Unique plugin identifier |
| version | Yes | string | — | Semantic version |
| type | Yes | string | — | Must be "tool" |
| description | No | string | "" | Human-readable description |
| timeout_ms | No | integer | 120000 | Default tool timeout in ms |
| tools | Yes | object[] | — | Tool definitions |

### Tool Definition

| Field | Required | Type | Description |
|-------|----------|------|-------------|
| name | Yes | string | Tool name (snake_case) |
| description | Yes | string | What the tool does |
| parameters | Yes | object | JSON Schema for parameters |

## JSON-RPC Methods

### Standard Methods

| Method | Direction | Purpose |
|--------|-----------|---------|
| health | Core → Plugin | Health check |
| tools/list | Core → Plugin | List available tools |
| `<tool_name>` | Core → Plugin | Execute a tool |

### Health Check

The `health` method is called periodically. Respond with:

```json
{"status": "ok", "dependencies": {}}
```

If a plugin fails 3 consecutive health checks, the core restarts it.

### Error Codes

| Code | Meaning |
|------|---------|
| -32601 | Method not found |
| -32603 | Internal error |
| -32000 | Tool execution error |

## SDK

Courtier provides a Python SDK for plugin development:

```python
from courtier_plugin_sdk import PluginRuntime

runtime = PluginRuntime()

@runtime.tool(name="my_tool")
def my_tool(params: dict) -> dict:
    return {"result": "done"}

runtime.run()
```

The SDK handles JSON-RPC framing, health checks, and error formatting.

## Testing Plugins

```bash
# Test plugin health
echo '{"jsonrpc":"2.0","id":1,"method":"health"}' | python entry.py

# Test tool execution
echo '{"jsonrpc":"2.0","id":2,"method":"do_something","params":{"input_data":"test"}}' | python entry.py
```
