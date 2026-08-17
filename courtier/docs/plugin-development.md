# Courtier Plugin Development Guide

## Overview

Plugins are **standalone TCP servers** communicating with the host via
newline-delimited JSON-RPC 2.0. Each plugin runs as its own process —
locally via `scripts/dev-plugins.py`, in production as a docker-compose
service (`Dockerfile.plugins` targets) or a systemd unit — and may live on
a different machine than the host.

```
┌──────────────┐     JSON-RPC 2.0 over TCP    ┌──────────────┐
│  Courtier    │ ◄───────────────────────────► │   Plugin     │
│  Core (dials)│      (+ MinIO transfer bucket)│  (server)    │
└──────────────┘                               └──────────────┘
```

The host dials each plugin from `COURTIER_PLUGIN_ENDPOINTS`
(`name=host:port`). On connect, the plugin immediately sends
`plugin.register` (capabilities + shared token); the host verifies the
token and answers `plugin.auth` with its own copy. Until auth passes the
plugin rejects every other method. After that, `plugin.host_services` and
`plugin.runtime_context` notifications complete the handshake, and the
plugin's tools become callable.

## Quick Start

1. Create a plugin directory (`plugins/shared/my_tool/` for cross-domain
   tools, `plugins/<domain>/my_tool/` for domain tools):

```
plugins/shared/my_tool/
├── plugin.yaml     # Plugin manifest (policy root: timeouts, permissions)
├── entry.py        # Plugin entry point (SDK PluginRuntime subclass)
├── tools.py        # Tool implementation(s)
└── pyproject.toml  # Plugin dependencies
```

2. Write `plugin.yaml`:

```yaml
name: my_tool            # must equal the directory name
version: "1.0.0"
api: "2.0"               # standalone protocol (token auth handshake)
description: "What my tool does"
timeout_ms: 120000

runtime:
  port: 9201             # default listen port (--listen / COURTIER_PLUGIN_LISTEN override)
  env:                   # optional literal defaults (os.environ.setdefault)
    MY_TUNING_KNOB: "42"
```

3. Write `tools.py` and `entry.py`:

```python
# tools.py
from courtier_plugin_sdk import ToolResult

class MyTool:
    name = "do_something"
    display_name = "做点什么"
    description = "Perform a custom operation"
    parameters = {
        "type": "object",
        "properties": {"input_data": {"type": "string"}},
        "required": ["input_data"],
    }

    async def execute(self, **kwargs):
        return ToolResult(success=True, data={"result": kwargs["input_data"]})
```

```python
# entry.py
from courtier_plugin_sdk import PluginRuntime
from tools import MyTool

class MyPlugin(PluginRuntime):
    def _setup_handlers(self):
        self.register_tool(MyTool())

if __name__ == "__main__":
    import asyncio
    asyncio.run(MyPlugin().run())
```

4. Run it and wire the host:

```bash
# plugin side (or via scripts/dev-plugins.py which discovers it automatically)
cd plugins/shared/my_tool && uv sync
COURTIER_PLUGIN_TOKEN=dev-token .venv/bin/python entry.py   # listens on runtime.port

# host side (.env)
COURTIER_PLUGIN_TOKEN=dev-token
COURTIER_PLUGIN_ENDPOINTS=...,my_tool=127.0.0.1:9201
```

## Files in tool arguments (`file_params` + `resolve_file`)

Cross-machine deployments share no filesystem with the host. If your tool
takes a file path, declare it and resolve at runtime:

```python
from courtier_plugin_sdk import resolve_file

class ConvertTool:
    name = "convert"
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}}
    file_params = ["file_path"]   # registered as format: file-ref in the contract

    async def execute(self, file_path: str = ""):
        local = await resolve_file(file_path)   # minio:// → downloaded to the request workdir
        ...
```

At dispatch the host enforces the upload-dir sandbox (fail-closed), PUTs
the file to the `courtier-plugin-io` transfer bucket (content-addressed
key, no duplicate uploads), and sends the plugin a `minio://bucket/key`
reference. `resolve_file` downloads it with the plugin's restricted MinIO
account into a per-request temp dir that is removed when the call ends.
Plain local paths pass through (tests / same-machine dev).

## Producing files (`put_file` + `storage.presign_get`)

```python
from courtier_plugin_sdk import HostStorage, put_file
from courtier_plugin_sdk.files import parse_minio_ref

ref = await put_file(Path("/tmp/out.docx"))        # direct transfer-bucket upload
bucket, key = parse_minio_ref(ref)
receipt = await HostStorage(self.host_client).presign_get(bucket, key)
url = receipt["download_url"]                       # host-minted, for the user
```

Requires the manifest to declare `host_services: [storage]` and
`permissions: [read:storage]` (plus `write:storage` if you also use the
legacy base64 `storage.put`).

## Environment ownership

Plugins read their own environment; the host injects nothing. Every
variable a plugin consumes is documented in `plugins/plugin.env.example`.
Required at startup: `COURTIER_PLUGIN_TOKEN` (must match the host) and —
for file-transferring plugins — the restricted MinIO account
(`MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY`, provisioned by
`scripts/minio_plugin_io.py`, scoped to the transfer bucket only).

## Host services

Declared in `plugin.yaml` `dependencies.host_services` + `permissions`,
enforced by the host on every reverse call: `cache.persist/load/resolve/
micro_compact`, `artifact_store.*`, `storage.put` (legacy base64),
`storage.presign_get`, `template_store.get`. Access them through the SDK's
`host_service_client` (available after the handshake).

## Lifecycle & ops

- The host health-checks every 30s (`plugin.health`); three consecutive
  failures close the connection and the manager reconnects with
  exponential backoff (1s → 30s cap, forever). Restarting a plugin is
  the deployment layer's job (compose `restart: unless-stopped`).
- Admin actions map to connections, not processes: start = dial,
  stop = disconnect, restart = redial. Plugin logs live with the plugin
  (container logs / its own stdout) — the admin logs endpoint returns 410.
- `plugin.shutdown` closes the requesting connection; the server keeps
  running. Stop the process with SIGTERM (graceful) or your supervisor.

## Testing plugins

Unit-test tools by calling `tool.execute(...)` directly. For protocol
level tests, inject an in-process reader/writer pair (`runtime._reader` /
`runtime._writer` as an `asyncio.Queue` + writer double) — that path
skips the network and the auth gate. Loopback TCP examples live in
`tests/plugin/test_serve_mode.py` and `tests/plugin/test_integration.py`.
