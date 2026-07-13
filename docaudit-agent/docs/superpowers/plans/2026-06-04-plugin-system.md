# Plugin System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full-stack plugin system with subprocess isolation, JSON-RPC over stdio communication, and file-system-based plugin discovery for the docaudit-agent.

**Architecture:** Plugins are self-contained directories under `plugins/` with a `plugin.yaml` manifest. Each plugin runs as a separate subprocess communicating via JSON-RPC over stdin/stdout. The host creates proxy objects implementing existing Protocols (`ToolProtocol`, `ContentChecker`) that forward calls to plugin subprocesses through an `ExtensionRegistry` routing layer.

**Tech Stack:** Python 3.12+, Pydantic, PyYAML, asyncio subprocess, pytest, pytest-asyncio

---

## File Structure

```
src/plugin/                          # NEW: Plugin system core module
├── __init__.py                      # Public API: exports PluginSystem, PluginStatus
├── manifest.py                      # PluginManifest Pydantic model + validation
├── scanner.py                       # PluginScanner: directory scan + manifest parsing
├── protocol.py                      # JSON-RPC message models (Request/Response/Notification/StreamChunk)
├── client.py                        # JSONRPCClient: single connection; ClientPool: multi-plugin
├── manager.py                       # PluginProcess dataclass, ProcessManager (lifecycle)
├── registry.py                      # ExtensionRegistry (routes capabilities to existing registries)
├── proxies.py                       # ProxyTool, ProxyChecker (implement existing Protocols via JSON-RPC)
└── sdk/                             # NEW: Plugin SDK package (for plugin authors to import)
    ├── __init__.py                  # Exports PluginRuntime
    ├── runtime.py                   # PluginRuntime base class (JSON-RPC server loop)
    └── protocol.py                  # Protocol constants (method names, error codes)

plugins/                             # NEW: Plugin root directory (created empty, with .gitkeep)
└── .gitkeep

tests/plugin/                        # NEW: Test directory
├── __init__.py
├── test_manifest.py                 # Unit: PluginManifest model
├── test_scanner.py                  # Unit: PluginScanner
├── test_protocol.py                 # Unit: JSON-RPC message serialization
├── test_client.py                   # Component: JSONRPCClient with pipe streams
├── test_proxies.py                  # Component: ProxyTool, ProxyChecker with mock client
├── test_registry.py                 # Component: ExtensionRegistry routing
├── test_manager.py                  # Component: ProcessManager state machine
├── test_runtime.py                  # Unit: PluginRuntime SDK
└── test_integration.py              # Integration: full flow with real subprocess

tests/fixtures/plugins/              # NEW: Test fixtures
├── echo_plugin/
│   ├── plugin.yaml
│   └── entry.py
├── bad_no_manifest/
│   └── entry.py
├── bad_name_mismatch/
│   └── plugin.yaml
├── bad_invalid_yaml/
│   └── plugin.yaml
├── crashing_plugin/
│   ├── plugin.yaml
│   └── entry.py
└── slow_register/
    ├── plugin.yaml
    └── entry.py

pyproject.toml                       # MODIFY: add pyyaml dependency
```

---

### Task 1: Add pyyaml dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add pyyaml to dependencies**

Run:
```bash
uv add pyyaml
```

Expected: `pyyaml` added to `pyproject.toml` and `uv.lock` updated.

- [ ] **Step 2: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add pyyaml dependency for plugin manifest parsing"
```

---

### Task 2: PluginManifest model

**Files:**
- Create: `src/plugin/__init__.py`
- Create: `src/plugin/manifest.py`
- Create: `tests/plugin/__init__.py`
- Create: `tests/plugin/test_manifest.py`

- [ ] **Step 1: Write the test**

In `tests/plugin/test_manifest.py`:
```python
"""Tests for PluginManifest model."""

import pytest
from pydantic import ValidationError

from src.plugin.manifest import (
    PluginManifest,
    RuntimeConfig,
    Dependencies,
    Capabilities,
    ToolCapability,
    CheckerCapability,
)


class TestPluginManifest:
    def test_minimal_valid_manifest(self):
        data = {
            "name": "my_plugin",
            "version": "0.1.0",
            "api": "1.0",
        }
        m = PluginManifest.model_validate(data)
        assert m.name == "my_plugin"
        assert m.version == "0.1.0"
        assert m.api == "1.0"
        assert m.description == ""
        assert m.runtime == RuntimeConfig()
        assert m.dependencies == Dependencies()
        assert m.capabilities == Capabilities()

    def test_full_manifest_with_all_capabilities(self):
        data = {
            "name": "full_plugin",
            "version": "1.2.3",
            "api": "1.0",
            "description": "A full-featured plugin",
            "author": "Test Author",
            "runtime": {
                "language": "python",
                "entry": "main.py",
                "env": {"DEBUG": "true"},
            },
            "dependencies": {
                "python": ["httpx>=0.28.0"],
                "host_services": ["cache"],
                "permissions": ["read:documents"],
            },
            "capabilities": {
                "tools": [
                    {
                        "name": "my_tool",
                        "display_name": "My Tool",
                        "description": "Does stuff",
                    }
                ],
                "checkers": [
                    {
                        "name": "my_checker",
                        "doc_type": "通知",
                        "display_name": "My Checker",
                    }
                ],
                "agents": [
                    {
                        "name": "my_agent",
                        "display_name": "My Agent",
                        "role": "You are an auditor",
                    }
                ],
                "routes": [
                    {
                        "prefix": "/api/v1/custom",
                        "description": "Custom API",
                    }
                ],
                "processors": [
                    {
                        "name": "my_parser",
                        "type": "parser",
                        "display_name": "My Parser",
                    }
                ],
            },
        }
        m = PluginManifest.model_validate(data)
        assert m.runtime.language == "python"
        assert m.runtime.entry == "main.py"
        assert m.runtime.env == {"DEBUG": "true"}
        assert m.dependencies.python == ["httpx>=0.28.0"]
        assert m.dependencies.host_services == ["cache"]
        assert m.dependencies.permissions == ["read:documents"]
        assert len(m.capabilities.tools) == 1
        assert m.capabilities.tools[0].name == "my_tool"
        assert len(m.capabilities.checkers) == 1
        assert m.capabilities.checkers[0].doc_type == "通知"
        assert len(m.capabilities.agents) == 1
        assert len(m.capabilities.routes) == 1
        assert len(m.capabilities.processors) == 1

    def test_defaults_for_empty_capabilities(self):
        m = PluginManifest.model_validate({"name": "p", "version": "0.1", "api": "1.0"})
        assert m.capabilities.tools == []
        assert m.capabilities.checkers == []
        assert m.capabilities.agents == []
        assert m.capabilities.routes == []
        assert m.capabilities.processors == []

    def test_name_is_required(self):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate({"version": "0.1", "api": "1.0"})

    def test_api_is_required(self):
        with pytest.raises(ValidationError):
            PluginManifest.model_validate({"name": "p", "version": "0.1"})
```

- [ ] **Step 2: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_manifest.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.plugin.manifest'`

- [ ] **Step 3: Create package init**

In `src/plugin/__init__.py`:
```python
"""DocAudit Plugin System."""
```

In `tests/plugin/__init__.py`:
```python
"""Tests for plugin system."""
```

- [ ] **Step 4: Write the manifest model**

In `src/plugin/manifest.py`:
```python
"""PluginManifest — Pydantic model for plugin.yaml parsing and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RuntimeConfig(BaseModel):
    """Plugin subprocess runtime configuration."""

    language: str = "python"
    entry: str = "entry.py"
    env: dict[str, str] = Field(default_factory=dict)


class Dependencies(BaseModel):
    """Plugin dependency declarations."""

    python: list[str] = Field(default_factory=list)
    host_services: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class ToolCapability(BaseModel):
    """A tool capability declaration."""

    name: str
    display_name: str = ""
    description: str = ""
    input_contract: dict[str, Any] | None = None
    output_contract: dict[str, Any] | None = None


class CheckerCapability(BaseModel):
    """A content checker capability declaration."""

    name: str
    doc_type: str
    display_name: str = ""


class AgentCapability(BaseModel):
    """An agent capability declaration."""

    name: str
    display_name: str = ""
    role: str = ""
    skill_names: list[str] = Field(default_factory=list)


class RouteCapability(BaseModel):
    """An API route capability declaration."""

    prefix: str
    description: str = ""


class ProcessorCapability(BaseModel):
    """A document processor capability declaration."""

    name: str
    type: str  # parser | corrector | builder
    display_name: str = ""


class Capabilities(BaseModel):
    """All capabilities declared by a plugin."""

    tools: list[ToolCapability] = Field(default_factory=list)
    checkers: list[CheckerCapability] = Field(default_factory=list)
    agents: list[AgentCapability] = Field(default_factory=list)
    routes: list[RouteCapability] = Field(default_factory=list)
    processors: list[ProcessorCapability] = Field(default_factory=list)


class PluginManifest(BaseModel):
    """Parsed plugin.yaml content.

    The `dir` field is set after parsing to record the plugin's
    filesystem location. It is not part of the YAML schema.
    """

    name: str
    version: str
    api: str
    description: str = ""
    author: str = ""
    license: str = ""

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    dependencies: Dependencies = Field(default_factory=Dependencies)
    capabilities: Capabilities = Field(default_factory=Capabilities)

    # Set by scanner after parsing — not in YAML
    dir: Path | None = Field(default=None, exclude=True)
```

- [ ] **Step 5: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_manifest.py -v
```

Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add src/plugin/__init__.py src/plugin/manifest.py tests/plugin/__init__.py tests/plugin/test_manifest.py
git commit -m "feat: add PluginManifest model for plugin.yaml parsing"
```

---

### Task 3: JSON-RPC protocol message types

**Files:**
- Create: `src/plugin/protocol.py`
- Create: `tests/plugin/test_protocol.py`

- [ ] **Step 1: Write the test**

In `tests/plugin/test_protocol.py`:
```python
"""Tests for JSON-RPC protocol message types."""

import json

from src.plugin.protocol import (
    JSONRPCRequest,
    JSONRPCResponse,
    JSONRPCNotification,
    JSONRPCStreamChunk,
    ErrorCodes,
)


class TestJSONRPCRequest:
    def test_serialize_request(self):
        req = JSONRPCRequest(id=1, method="tool.execute", params={"tool": "echo"})
        dumped = req.model_dump_json()
        data = json.loads(dumped)
        assert data == {"id": 1, "method": "tool.execute", "params": {"tool": "echo"}}

    def test_default_params(self):
        req = JSONRPCRequest(id=1, method="plugin.health")
        assert req.params == {}

    def test_roundtrip_from_line(self):
        req = JSONRPCRequest(id=42, method="checker.check", params={"text": "hello"})
        line = req.model_dump_json()
        parsed = JSONRPCRequest.model_validate_json(line)
        assert parsed.id == 42
        assert parsed.method == "checker.check"
        assert parsed.params == {"text": "hello"}


class TestJSONRPCResponse:
    def test_success_response(self):
        resp = JSONRPCResponse(id=1, result={"success": True, "data": [1, 2, 3]})
        data = json.loads(resp.model_dump_json())
        assert data["id"] == 1
        assert data["result"] == {"success": True, "data": [1, 2, 3]}
        assert data["error"] is None

    def test_error_response(self):
        resp = JSONRPCResponse(
            id=1,
            error={"code": -32000, "message": "Tool not found"},
        )
        data = json.loads(resp.model_dump_json())
        assert data["id"] == 1
        assert data["result"] is None
        assert data["error"] == {"code": -32000, "message": "Tool not found"}

    def test_is_error_property(self):
        resp = JSONRPCResponse(id=1, error={"code": -32000, "message": "bad"})
        assert resp.is_error is True

        resp2 = JSONRPCResponse(id=1, result={"ok": True})
        assert resp2.is_error is False


class TestJSONRPCNotification:
    def test_notification_has_no_id(self):
        notif = JSONRPCNotification(
            method="plugin.register",
            params={"capabilities": [{"type": "tool", "name": "echo"}]},
        )
        data = json.loads(notif.model_dump_json())
        assert "id" not in data
        assert data["method"] == "plugin.register"
        assert data["params"]["capabilities"][0]["name"] == "echo"

    def test_is_notification(self):
        notif = JSONRPCNotification(method="plugin.shutdown", params={})
        assert notif.is_notification is True

    def test_from_dict(self):
        raw = {"method": "health.check", "params": {"ts": 123}}
        notif = JSONRPCNotification.model_validate(raw)
        assert notif.method == "health.check"


class TestJSONRPCStreamChunk:
    def test_continue_chunk(self):
        chunk = JSONRPCStreamChunk(id=2, chunk="partial", status="continue")
        data = json.loads(chunk.model_dump_json())
        assert data["id"] == 2
        assert data["chunk"] == "partial"
        assert data["status"] == "continue"
        assert data["result"] is None

    def test_end_chunk(self):
        chunk = JSONRPCStreamChunk(
            id=2,
            result={"success": True, "data": "done"},
            status="end",
        )
        data = json.loads(chunk.model_dump_json())
        assert data["chunk"] is None
        assert data["status"] == "end"
        assert data["result"] == {"success": True, "data": "done"}

    def test_is_end(self):
        chunk = JSONRPCStreamChunk(id=1, status="continue", chunk="...")
        assert chunk.is_end is False

        chunk2 = JSONRPCStreamChunk(id=1, status="end", result={"ok": True})
        assert chunk2.is_end is True


class TestErrorCodes:
    def test_standard_codes_defined(self):
        assert ErrorCodes.PARSE_ERROR == -32700
        assert ErrorCodes.METHOD_NOT_FOUND == -32601
        assert ErrorCodes.INVALID_PARAMS == -32602
        assert ErrorCodes.INTERNAL_ERROR == -32603
        assert ErrorCodes.TIMEOUT_ERROR == -32000
        assert ErrorCodes.TOOL_NOT_FOUND == -32001
        assert ErrorCodes.PLUGIN_CRASHED == -32002
```

- [ ] **Step 2: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_protocol.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write protocol message types**

In `src/plugin/protocol.py`:
```python
"""JSON-RPC 2.0-style message types for plugin communication."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorCodes:
    """JSON-RPC error code constants."""

    PARSE_ERROR = -32700
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    TIMEOUT_ERROR = -32000
    TOOL_NOT_FOUND = -32001
    PLUGIN_CRASHED = -32002


class JSONRPCRequest(BaseModel):
    """A request from host to plugin (or vice versa)."""

    id: int
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCResponse(BaseModel):
    """A response to a request."""

    id: int
    result: Any = None
    error: dict[str, Any] | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


class JSONRPCNotification(BaseModel):
    """A one-way notification (no id, no response expected)."""

    method: str
    params: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_notification(self) -> bool:
        return True


class JSONRPCStreamChunk(BaseModel):
    """A streaming response chunk for long-running operations."""

    id: int
    chunk: str | None = None
    result: Any = None
    status: str  # "continue" | "end"

    @property
    def is_end(self) -> bool:
        return self.status == "end"
```

- [ ] **Step 4: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_protocol.py -v
```

Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/plugin/protocol.py tests/plugin/test_protocol.py
git commit -m "feat: add JSON-RPC protocol message types"
```

---

### Task 4: PluginScanner

**Files:**
- Create: `src/plugin/scanner.py`
- Create: `tests/plugin/test_scanner.py`
- Create: `tests/fixtures/plugins/echo_plugin/plugin.yaml`
- Create: `tests/fixtures/plugins/bad_no_manifest/entry.py`
- Create: `tests/fixtures/plugins/bad_name_mismatch/plugin.yaml`
- Create: `tests/fixtures/plugins/bad_invalid_yaml/plugin.yaml`

- [ ] **Step 1: Create test fixture: valid plugin manifest**

In `tests/fixtures/plugins/echo_plugin/plugin.yaml`:
```yaml
name: echo_plugin
version: "0.1.0"
api: "1.0"
description: "Echo test plugin"

runtime:
  entry: entry.py

capabilities:
  tools:
    - name: echo
      display_name: "Echo"
      description: "Returns the input unchanged"
```

In `tests/fixtures/plugins/bad_no_manifest/entry.py`:
```python
# This directory intentionally has no plugin.yaml
print("no manifest here")
```

In `tests/fixtures/plugins/bad_name_mismatch/plugin.yaml`:
```yaml
name: wrong_name
version: "0.1.0"
api: "1.0"
```

In `tests/fixtures/plugins/bad_invalid_yaml/plugin.yaml`:
```yaml
name: [this is not valid YAML for a string field
version: "0.1.0"
```

- [ ] **Step 2: Write the scanner test**

In `tests/plugin/test_scanner.py`:
```python
"""Tests for PluginScanner."""

from pathlib import Path

import pytest

from src.plugin.scanner import PluginScanner, ScanStatus


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plugins"


class TestPluginScanner:
    def test_scan_empty_directory(self, tmp_path):
        scanner = PluginScanner()
        results = scanner.scan(tmp_path)
        assert results == []

    def test_scan_finds_valid_plugin(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        echo_result = next((r for r in results if r.name == "echo_plugin"), None)
        assert echo_result is not None
        assert echo_result.status == ScanStatus.VALID
        assert echo_result.manifest is not None
        assert echo_result.manifest.name == "echo_plugin"
        assert echo_result.manifest.version == "0.1.0"
        assert echo_result.manifest.api == "1.0"
        assert len(echo_result.manifest.capabilities.tools) == 1
        assert echo_result.manifest.capabilities.tools[0].name == "echo"
        assert echo_result.error is None

    def test_no_manifest_directory_is_skipped(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        names = {r.name for r in results}
        assert "bad_no_manifest" not in names

    def test_name_mismatch_is_blocked(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        bad = next((r for r in results if r.name == "wrong_name"), None)
        assert bad is not None
        assert bad.status == ScanStatus.BLOCKED
        assert "directory name" in bad.error.lower()

    def test_invalid_yaml_is_blocked(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)

        bad = next((r for r in results if r.name == "bad_invalid_yaml"), None)
        assert bad is not None
        assert bad.status == ScanStatus.BLOCKED
        assert bad.error is not None

    def test_nonexistent_directory_raises(self):
        scanner = PluginScanner()
        with pytest.raises(FileNotFoundError):
            scanner.scan(Path("/nonexistent/path/12345"))

    def test_scan_result_repr(self):
        scanner = PluginScanner()
        results = scanner.scan(FIXTURES_DIR)
        echo = next(r for r in results if r.name == "echo_plugin")
        repr_str = repr(echo)
        assert "echo_plugin" in repr_str
        assert "VALID" in repr_str
```

- [ ] **Step 3: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_scanner.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'src.plugin.scanner'`

- [ ] **Step 4: Write the scanner**

In `src/plugin/scanner.py`:
```python
"""PluginScanner — scan plugins/ directory and parse plugin.yaml manifests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from .manifest import PluginManifest

logger = logging.getLogger(__name__)


class ScanStatus(str, Enum):
    VALID = "VALID"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"


@dataclass
class PluginScanResult:
    """Result of scanning a single plugin directory."""

    name: str
    dir: Path
    status: ScanStatus
    manifest: PluginManifest | None = None
    error: str | None = None

    def __repr__(self) -> str:
        return f"PluginScanResult(name={self.name!r}, status={self.status.value})"


class PluginScanner:
    """Scans a directory for plugin subdirectories with valid plugin.yaml manifests."""

    MANIFEST_FILE = "plugin.yaml"
    HOST_API_VERSION = "1.0"

    def scan(self, plugins_dir: Path) -> list[PluginScanResult]:
        """Scan plugins_dir for valid plugins.

        Returns results for ALL detected plugin directories,
        including those that failed validation (marked BLOCKED).
        Directories without a plugin.yaml are silently skipped.
        """
        if not plugins_dir.exists():
            raise FileNotFoundError(f"Plugins directory not found: {plugins_dir}")
        if not plugins_dir.is_dir():
            raise NotADirectoryError(f"Not a directory: {plugins_dir}")

        results: list[PluginScanResult] = []
        for entry in sorted(plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            manifest_path = entry / self.MANIFEST_FILE
            if not manifest_path.is_file():
                continue
            result = self._scan_one(entry, manifest_path)
            results.append(result)
        return results

    def _scan_one(self, plugin_dir: Path, manifest_path: Path) -> PluginScanResult:
        """Parse and validate a single plugin's manifest."""
        name = plugin_dir.name

        # Parse YAML
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Invalid YAML in {self.MANIFEST_FILE}: {e}",
            )

        if raw is None:
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Empty {self.MANIFEST_FILE}",
            )

        if not isinstance(raw, dict):
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"{self.MANIFEST_FILE} must contain a YAML mapping, got {type(raw).__name__}",
            )

        # Validate with Pydantic
        try:
            manifest = PluginManifest.model_validate(raw)
        except Exception as e:
            return PluginScanResult(
                name=name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Invalid manifest: {e}",
            )

        # Set directory reference
        manifest.dir = plugin_dir

        # Validate: name matches directory
        if manifest.name != name:
            return PluginScanResult(
                name=manifest.name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"Manifest name '{manifest.name}' does not match directory name '{name}'",
            )

        # Validate: API version compatible
        if manifest.api != self.HOST_API_VERSION:
            return PluginScanResult(
                name=manifest.name,
                dir=plugin_dir,
                status=ScanStatus.BLOCKED,
                error=f"API version mismatch: plugin requires '{manifest.api}', host is '{self.HOST_API_VERSION}'",
            )

        return PluginScanResult(
            name=manifest.name,
            dir=plugin_dir,
            status=ScanStatus.VALID,
            manifest=manifest,
        )
```

- [ ] **Step 5: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_scanner.py -v
```

Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add src/plugin/scanner.py tests/plugin/test_scanner.py tests/fixtures/plugins/
git commit -m "feat: add PluginScanner for plugin directory scanning and manifest validation"
```

---

### Task 5: JSONRPCClient

**Files:**
- Create: `src/plugin/client.py`
- Create: `tests/plugin/test_client.py`

- [ ] **Step 1: Write the client test**

In `tests/plugin/test_client.py`:
```python
"""Tests for JSONRPCClient."""

import asyncio
import json

import pytest

from src.plugin.client import JSONRPCClient
from src.plugin.protocol import ErrorCodes


class PipeStreams:
    """Simulates stdin/stdout pair for testing JSONRPCClient without a real subprocess."""

    def __init__(self):
        self._read_queue: asyncio.Queue[str] = asyncio.Queue()
        self._written: list[str] = []

    async def readline(self) -> bytes:
        line = await self._read_queue.get()
        return line.encode("utf-8")

    def write(self, data: bytes) -> None:
        self._written.append(data.decode("utf-8"))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def feed_line(self, line: str) -> None:
        self._read_queue.put_nowait(line)

    @property
    def written(self) -> list[dict]:
        return [json.loads(line) for line in self._written]


class TestJSONRPCClient:
    @pytest.fixture
    def streams(self):
        return PipeStreams()

    @pytest.fixture
    def client(self, streams):
        return JSONRPCClient(streams, streams, plugin_name="test_plugin")

    @pytest.mark.asyncio
    async def test_call_sends_request_and_receives_response(self, client, streams):
        # Feed the response before awaiting
        streams.feed_line(json.dumps({"id": 1, "result": {"success": True, "data": "hello"}}))

        result = await client.call("echo", {"message": "hello"})
        assert result == {"success": True, "data": "hello"}

        # Verify the request was written
        assert len(streams.written) == 1
        req = streams.written[0]
        assert req["id"] == 1
        assert req["method"] == "echo"
        assert req["params"] == {"message": "hello"}

    @pytest.mark.asyncio
    async def test_call_receives_error_response(self, client, streams):
        streams.feed_line(json.dumps({
            "id": 1,
            "error": {"code": -32000, "message": "Tool not found"},
        }))

        with pytest.raises(Exception) as exc_info:
            await client.call("bad_tool", {})
        assert "Tool not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_call_timeout(self, client, streams):
        with pytest.raises(asyncio.TimeoutError):
            await client.call("slow_tool", {}, timeout=0.01)

    @pytest.mark.asyncio
    async def test_request_ids_are_sequential(self, client, streams):
        streams.feed_line(json.dumps({"id": 1, "result": "first"}))
        streams.feed_line(json.dumps({"id": 2, "result": "second"}))

        r1 = await client.call("method1", {})
        r2 = await client.call("method2", {})
        assert r1 == "first"
        assert r2 == "second"
        assert streams.written[0]["id"] == 1
        assert streams.written[1]["id"] == 2

    @pytest.mark.asyncio
    async def test_notify_writes_notification_without_id(self, client, streams):
        client.notify("plugin.shutdown", {"reason": "test"})
        await asyncio.sleep(0.01)  # let write complete

        assert len(streams.written) == 1
        notif = streams.written[0]
        assert "id" not in notif
        assert notif["method"] == "plugin.shutdown"
        assert notif["params"] == {"reason": "test"}

    @pytest.mark.asyncio
    async def test_wait_for_register_receives_capabilities(self, client, streams):
        streams.feed_line(json.dumps({
            "method": "plugin.register",
            "params": {
                "capabilities": [
                    {"type": "tool", "name": "echo"},
                    {"type": "checker", "doc_type": "通知"},
                ]
            },
        }))

        caps = await client.wait_for_register(timeout=1.0)
        assert len(caps) == 2
        assert caps[0] == {"type": "tool", "name": "echo"}
        assert caps[1] == {"type": "checker", "doc_type": "通知"}

    @pytest.mark.asyncio
    async def test_wait_for_register_timeout(self, client, streams):
        with pytest.raises(asyncio.TimeoutError):
            await client.wait_for_register(timeout=0.01)

    @pytest.mark.asyncio
    async def test_close_cleans_up(self, client, streams):
        client.close()
        # No exception expected — writer is closed
```

- [ ] **Step 2: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_client.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the client**

In `src/plugin/client.py`:
```python
"""JSONRPCClient — manages a single JSON-RPC connection to a plugin subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from .protocol import ErrorCodes, JSONRPCRequest, JSONRPCResponse, JSONRPCNotification, JSONRPCStreamChunk

logger = logging.getLogger(__name__)


class PluginRPCError(Exception):
    """Wraps a JSON-RPC error from a plugin."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class PluginCrashedError(Exception):
    """Raised when a plugin subprocess exits unexpectedly."""

    def __init__(self, plugin_name: str):
        self.plugin_name = plugin_name
        super().__init__(f"Plugin '{plugin_name}' crashed or disconnected")


class JSONRPCClient:
    """Manages a single JSON-RPC over stdio connection to one plugin subprocess.

    Uses asyncio streams for non-blocking I/O. Request/response matching
    is done via an auto-incrementing sequence id and a pending futures dict.
    """

    def __init__(self, reader: Any, writer: Any, plugin_name: str) -> None:
        self._reader = reader
        self._writer = writer
        self.plugin_name = plugin_name
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    async def _ensure_reader(self) -> None:
        """Start the background reader task if not already running."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Continuously read lines from stdin, dispatch to pending futures."""
        while not self._closed:
            try:
                line = await self._reader.readline()
            except Exception:
                if not self._closed:
                    logger.debug("Read error for plugin %s", self.plugin_name, exc_info=True)
                break

            if not line:  # EOF — subprocess exited
                self._fail_all_pending(
                    PluginCrashedError(self.plugin_name)
                )
                break

            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue

            try:
                data = json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("Malformed JSON from plugin %s: %s", self.plugin_name, line_str[:200])
                continue

            self._dispatch(data)

    def _dispatch(self, data: dict) -> None:
        """Route an incoming message to a pending future or a notification handler."""
        if "id" in data and "method" not in data:
            # It's a response
            msg_id = data["id"]
            future = self._pending.pop(msg_id, None)
            if future is None or future.done():
                logger.debug("Received response for unknown/cancelled id=%d from plugin %s", msg_id, self.plugin_name)
                return
            if "error" in data and data["error"] is not None:
                err = data["error"]
                future.set_exception(PluginRPCError(err.get("code", -1), err.get("message", "Unknown error")))
            else:
                future.set_result(data.get("result"))
        elif "method" in data and "id" not in data:
            # It's a notification — handle in subclass or via callback
            self._handle_notification(data)
        else:
            # Streaming chunk or unknown format — forward to pending future if streaming
            if "id" in data:
                future = self._pending.get(data["id"])
                if future and not future.done():
                    future.set_result(data)

    def _handle_notification(self, data: dict) -> None:
        """Handle an incoming notification. Override or set callback for register handling."""
        method = data.get("method", "")
        params = data.get("params", {})
        logger.debug("Notification from plugin %s: %s", self.plugin_name, method)

        if method == "plugin.register":
            caps = params.get("capabilities", [])
            # Store for wait_for_register
            self._register_caps = caps
            if hasattr(self, "_register_event"):
                self._register_event.set()

    def _fail_all_pending(self, exc: Exception) -> None:
        """Reject all pending futures (called on disconnect/crash)."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        if self._closed:
            raise PluginCrashedError(self.plugin_name)

        await self._ensure_reader()

        self._next_id += 1
        req_id = self._next_id

        request = JSONRPCRequest(id=req_id, method=method, params=params or {})
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future

        try:
            self._writer.write((request.model_dump_json() + "\n").encode("utf-8"))
            await self._writer.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise PluginCrashedError(self.plugin_name) from e

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise

        return result

    async def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[JSONRPCStreamChunk]:
        """Send a request and yield streaming response chunks."""
        if self._closed:
            raise PluginCrashedError(self.plugin_name)

        await self._ensure_reader()

        self._next_id += 1
        req_id = self._next_id

        request = JSONRPCRequest(id=req_id, method=method, params=params or {})
        self._writer.write((request.model_dump_json() + "\n").encode("utf-8"))
        await self._writer.drain()

        while True:
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[req_id] = future

            data = await future
            chunk = JSONRPCStreamChunk.model_validate(data)
            yield chunk
            if chunk.is_end:
                break

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a one-way notification (no response expected)."""
        if self._closed:
            return
        notif = JSONRPCNotification(method=method, params=params or {})
        try:
            self._writer.write((notif.model_dump_json() + "\n").encode("utf-8"))
        except Exception:
            logger.debug("Failed to send notification %s to plugin %s", method, self.plugin_name, exc_info=True)

    async def wait_for_register(self, timeout: float = 10.0) -> list[dict]:
        """Wait for the plugin.register notification and return capabilities."""
        await self._ensure_reader()

        self._register_event = asyncio.Event()
        self._register_caps: list[dict] = []

        try:
            await asyncio.wait_for(self._register_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"Plugin '{self.plugin_name}' did not send register notification within {timeout}s"
            )

        return self._register_caps

    def close(self) -> None:
        """Close the writer and cancel the reader."""
        self._closed = True
        self._fail_all_pending(PluginCrashedError(self.plugin_name))
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        try:
            self._writer.close()
        except Exception:
            pass
```

- [ ] **Step 4: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_client.py -v
```

Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add src/plugin/client.py tests/plugin/test_client.py
git commit -m "feat: add JSONRPCClient for plugin subprocess communication"
```

---

### Task 6: Plugin SDK — PluginRuntime

**Files:**
- Create: `src/plugin/sdk/__init__.py`
- Create: `src/plugin/sdk/protocol.py`
- Create: `src/plugin/sdk/runtime.py`
- Create: `tests/plugin/test_runtime.py`

- [ ] **Step 1: Write the SDK protocol constants**

In `src/plugin/sdk/protocol.py`:
```python
"""Protocol constants shared between host and plugin SDK."""

# Method names (host → plugin)
METHOD_HEALTH = "plugin.health"
METHOD_SHUTDOWN = "plugin.shutdown"
METHOD_TOOL_LIST = "tool.list"
METHOD_TOOL_EXECUTE = "tool.execute"
METHOD_CHECKER_LIST = "checker.list"
METHOD_CHECKER_CHECK = "checker.check"

# Method names (plugin → host, notifications)
METHOD_REGISTER = "plugin.register"

# Error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
```

- [ ] **Step 2: Write the SDK init**

In `src/plugin/sdk/__init__.py`:
```python
"""DocAudit Plugin SDK — base classes for building plugins."""
from .runtime import PluginRuntime

__all__ = ["PluginRuntime"]
```

- [ ] **Step 3: Write the runtime test**

In `tests/plugin/test_runtime.py`:
```python
"""Tests for PluginRuntime SDK."""

import json
import asyncio

import pytest

from src.plugin.sdk.runtime import PluginRuntime
from src.plugin.sdk.protocol import METHOD_TOOL_EXECUTE


class EchoRuntime(PluginRuntime):
    """Test runtime that echoes tool calls."""

    def __init__(self):
        super().__init__()
        self._tool_handlers: dict[str, callable] = {}

    def register_capabilities(self):
        return [
            {"type": "tool", "name": "echo", "display_name": "Echo"},
        ]

    def _setup_handlers(self):
        @self.on(METHOD_TOOL_EXECUTE)
        def handle_tool_execute(params):
            tool_name = params.get("tool", "")
            args = params.get("args", {})
            return {
                "success": True,
                "data": {"tool": tool_name, "echo": args},
            }


class TestPluginRuntime:
    @pytest.mark.asyncio
    async def test_startup_sends_register_notification(self):
        runtime = PluginRuntime()

        # Simulated stdio
        input_queue: asyncio.Queue[str] = asyncio.Queue()
        output_lines: list[str] = []

        class TestWriter:
            def write(self, data: str):
                output_lines.append(data)

            async def drain(self):
                pass

        runtime._reader = input_queue
        runtime._writer = TestWriter()

        # Feed a health check and EOF
        input_queue.put_nowait(json.dumps({"id": 1, "method": "plugin.health", "params": {}}))
        input_queue.put_nowait("")  # EOF

        await runtime.run()

        # First output should be the register notification
        assert len(output_lines) >= 1
        first = json.loads(output_lines[0])
        assert first["method"] == "plugin.register"
        assert "capabilities" in first["params"]

    @pytest.mark.asyncio
    async def test_handles_health_check(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []

        class TestWriter:
            def write(self, data: str):
                output_lines.append(data)

            async def drain(self):
                pass

        input_queue: asyncio.Queue[str] = asyncio.Queue()
        runtime._reader = input_queue
        runtime._writer = TestWriter()

        # Skip register output, then feed health check + EOF
        input_queue.put_nowait("")
        input_queue.put_nowait(json.dumps({"id": 1, "method": "plugin.health", "params": {}}))
        input_queue.put_nowait("")

        await runtime.run()

        # Should have a health response
        responses = [json.loads(l) for l in output_lines if '"id"' in l and '"method"' not in l]
        health_resp = next((r for r in responses if r.get("result") == "ok"), None)
        assert health_resp is not None

    @pytest.mark.asyncio
    async def test_handles_unknown_method_with_error(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []

        class TestWriter:
            def write(self, data: str):
                output_lines.append(data)

            async def drain(self):
                pass

        input_queue: asyncio.Queue[str] = asyncio.Queue()
        runtime._reader = input_queue
        runtime._writer = TestWriter()

        input_queue.put_nowait("")
        input_queue.put_nowait(json.dumps({"id": 2, "method": "unknown.method", "params": {}}))
        input_queue.put_nowait("")

        await runtime.run()

        responses = [json.loads(l) for l in output_lines if '"id"' in l]
        err_resp = next((r for r in responses if r.get("id") == 2), None)
        assert err_resp is not None
        assert "error" in err_resp

    @pytest.mark.asyncio
    async def test_malformed_json_is_skipped(self):
        runtime = PluginRuntime()

        output_lines: list[str] = []

        class TestWriter:
            def write(self, data: str):
                output_lines.append(data)

            async def drain(self):
                pass

        input_queue: asyncio.Queue[str] = asyncio.Queue()
        runtime._reader = input_queue
        runtime._writer = TestWriter()

        input_queue.put_nowait("")
        input_queue.put_nowait("this is not valid json {{{")
        input_queue.put_nowait(json.dumps({"id": 1, "method": "plugin.health", "params": {}}))
        input_queue.put_nowait("")

        await runtime.run()

        # Should still get a health response
        responses = [json.loads(l) for l in output_lines if '"id"' in l]
        assert len(responses) >= 1
        assert responses[-1]["result"] == "ok"
```

- [ ] **Step 4: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_runtime.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 5: Write the runtime**

In `src/plugin/sdk/runtime.py`:
```python
"""PluginRuntime — base class for plugin entry points.

Plugin authors subclass PluginRuntime, register capabilities and
handlers, then call `await runtime.run()` in their entry.py.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from typing import Any

from .protocol import (
    METHOD_HEALTH,
    METHOD_SHUTDOWN,
    METHOD_TOOL_LIST,
    METHOD_TOOL_EXECUTE,
    METHOD_CHECKER_LIST,
    METHOD_CHECKER_CHECK,
    METHOD_REGISTER,
    METHOD_NOT_FOUND as ERR_METHOD_NOT_FOUND,
    INTERNAL_ERROR as ERR_INTERNAL,
    PARSE_ERROR as ERR_PARSE,
)

logger = logging.getLogger(__name__)


class PluginRuntime:
    """Base class for plugin subprocess entry points.

    Usage in entry.py:
        class MyPlugin(PluginRuntime):
            def register_capabilities(self):
                return [{"type": "tool", "name": "my_tool"}]

            def _setup_handlers(self):
                @self.on("tool.execute")
                def handle(params):
                    return {"success": True, "data": ...}

        if __name__ == "__main__":
            import asyncio
            asyncio.run(MyPlugin().run())
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}
        self._running = False
        # These are set before run() for testability; run() uses stdin/stdout by default
        self._reader = None
        self._writer = None

    def on(self, method: str) -> Callable:
        """Decorator: register a handler for a JSON-RPC method."""

        def decorator(fn: Callable) -> Callable:
            self._handlers[method] = fn
            return fn

        return decorator

    def register_capabilities(self) -> list[dict[str, Any]]:
        """Override: return the list of capabilities this plugin provides."""
        return []

    def _setup_handlers(self) -> None:
        """Override: register handlers using self.on()."""
        pass

    async def run(self) -> None:
        """Start the JSON-RPC server loop on stdin/stdout.

        Sends the plugin.register notification first, then processes
        incoming requests line by line.
        """
        self._setup_handlers()

        # Allow injection of reader/writer for testing
        if self._reader is None:
            self._reader = sys.stdin
        if self._writer is None:
            self._writer = sys.stdout

        self._running = True

        # Send registration notification
        caps = self.register_capabilities()
        self._send_notification(METHOD_REGISTER, {"capabilities": caps})

        # Process requests
        for line in self._reader:
            if not self._running:
                break

            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.error("Malformed JSON: %s", line[:200])
                continue

            if not isinstance(msg, dict):
                continue

            if "method" in msg and "id" not in msg:
                # Notification
                self._handle_notification(msg)
            elif "id" in msg and "method" in msg:
                # Request
                response = self._handle_request(msg)
                self._send_line(json.dumps(response, ensure_ascii=False))

    def _handle_request(self, msg: dict) -> dict:
        req_id = msg["id"]
        method = msg.get("method", "")
        params = msg.get("params", {})

        try:
            handler = self._handlers.get(method)
            if handler is None:
                # Check built-in methods
                if method == METHOD_HEALTH:
                    return {"id": req_id, "result": "ok"}
                if method == METHOD_SHUTDOWN:
                    self._running = False
                    return {"id": req_id, "result": "ok"}
                return {
                    "id": req_id,
                    "error": {"code": ERR_METHOD_NOT_FOUND, "message": f"Unknown method: {method}"},
                }

            result = handler(params)
            return {"id": req_id, "result": result}
        except Exception as e:
            logger.exception("Error handling method '%s'", method)
            return {
                "id": req_id,
                "error": {"code": ERR_INTERNAL, "message": str(e)},
            }

    def _handle_notification(self, msg: dict) -> None:
        """Handle incoming notifications (e.g., shutdown)."""
        method = msg.get("method", "")
        if method == METHOD_SHUTDOWN:
            self._running = False

    def _send_notification(self, method: str, params: dict) -> None:
        msg = {"method": method, "params": params}
        self._send_line(json.dumps(msg, ensure_ascii=False))

    def _send_line(self, line: str) -> None:
        try:
            self._writer.write(line + "\n")
            self._writer.flush()
        except Exception:
            logger.exception("Failed to write to stdout")
```

- [ ] **Step 6: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_runtime.py -v
```

Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add src/plugin/sdk/ tests/plugin/test_runtime.py
git commit -m "feat: add PluginRuntime SDK base class for plugin development"
```

---

### Task 7: Proxy Objects

**Files:**
- Create: `src/plugin/proxies.py`
- Create: `tests/plugin/test_proxies.py`

- [ ] **Step 1: Write the proxies test**

In `tests/plugin/test_proxies.py`:
```python
"""Tests for proxy objects (ProxyTool, ProxyChecker)."""

import pytest
import json

from src.plugin.proxies import ProxyTool, ProxyChecker
from src.plugin.client import JSONRPCClient


class MockClient:
    """Mock JSONRPCClient for testing proxies without real subprocess."""

    def __init__(self, responses: list | None = None):
        self.calls: list[dict] = []
        self._responses = responses or []

    async def call(self, method: str, params: dict = None, timeout: float = 30.0):
        self.calls.append({"method": method, "params": params or {}})
        if self._responses:
            return self._responses.pop(0)
        return {"success": True, "data": {}}

    async def stream(self, method: str, params: dict = None):
        self.calls.append({"method": method, "params": params or {}})
        yield {"id": 1, "status": "end", "result": {"success": True, "data": "ok"}}

    @property
    def plugin_name(self):
        return "test_plugin"


class TestProxyTool:
    def test_proxy_tool_has_required_attributes(self):
        tool_spec = {
            "name": "my_tool",
            "display_name": "My Tool",
            "description": "A test tool",
        }
        mock_client = MockClient()
        proxy = ProxyTool(mock_client, tool_spec)

        assert proxy.name == "my_tool"
        assert proxy.display_name == "My Tool"
        assert proxy.description == "A test tool"
        assert proxy.parameters == {}
        assert proxy.skip_persist is False
        assert proxy.output_schema is None

    @pytest.mark.asyncio
    async def test_execute_forwards_to_client(self):
        tool_spec = {"name": "my_tool", "description": "desc"}
        mock_client = MockClient([
            {"success": True, "data": {"result": 42}},
        ])
        proxy = ProxyTool(mock_client, tool_spec)

        result = await proxy.execute(arg1="hello", arg2=123)
        assert result.success is True
        assert result.data == {"result": 42}

        assert len(mock_client.calls) == 1
        assert mock_client.calls[0]["method"] == "tool.execute"
        assert mock_client.calls[0]["params"]["tool"] == "my_tool"
        assert mock_client.calls[0]["params"]["args"] == {"arg1": "hello", "arg2": 123}

    @pytest.mark.asyncio
    async def test_execute_error_response(self):
        tool_spec = {"name": "bad_tool", "description": "desc"}
        mock_client = MockClient([
            {"success": False, "error": "something went wrong"},
        ])
        proxy = ProxyTool(mock_client, tool_spec)

        result = await proxy.execute()
        assert result.success is False
        assert result.error == "something went wrong"

    @pytest.mark.asyncio
    async def test_execute_with_parameters_from_spec(self):
        tool_spec = {
            "name": "param_tool",
            "description": "Has params",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        }
        proxy = ProxyTool(MockClient(), tool_spec)
        assert proxy.parameters["type"] == "object"
        assert "text" in proxy.parameters["properties"]


class TestProxyChecker:
    def test_proxy_checker_has_doc_type(self):
        checker_spec = {"name": "my_checker", "doc_type": "通知"}
        proxy = ProxyChecker(MockClient(), checker_spec)
        assert proxy.doc_type == "通知"

    @pytest.mark.asyncio
    async def test_check_forwards_to_client(self):
        checker_spec = {"name": "my_checker", "doc_type": "通知"}
        mock_client = MockClient([
            {"is_valid": True, "violations": []},
        ])
        proxy = ProxyChecker(mock_client, checker_spec)

        result = await proxy.check("some text", subtype=None)
        assert result.is_valid is True
        assert result.violations == []

        assert mock_client.calls[0]["method"] == "checker.check"
        assert mock_client.calls[0]["params"]["text"] == "some text"
        assert mock_client.calls[0]["params"]["subtype"] is None

    @pytest.mark.asyncio
    async def test_check_with_violations(self):
        checker_spec = {"name": "my_checker", "doc_type": "请示"}
        mock_client = MockClient([
            {
                "is_valid": False,
                "violations": [
                    {"rule_id": "R001", "message": "缺少标题", "severity": "error"},
                ],
            },
        ])
        proxy = ProxyChecker(mock_client, checker_spec)

        result = await proxy.check("bad text", subtype="请示")
        assert result.is_valid is False
        assert len(result.violations) == 1
        assert result.violations[0].rule_id == "R001"
        assert result.violations[0].message == "缺少标题"
```

- [ ] **Step 2: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_proxies.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the proxies**

In `src/plugin/proxies.py`:
```python
"""Proxy objects that implement existing Protocols via JSON-RPC forwarding."""

from __future__ import annotations

import logging
from typing import Any

from src.agent.tools.protocol import ToolResult
from src.content_compliance.core import ComplianceResult, Violation

logger = logging.getLogger(__name__)


class ProxyTool:
    """Implements ToolProtocol by forwarding execute() over JSON-RPC.

    This class does NOT inherit from ToolProtocol (it's a Protocol, not a base class).
    It provides the same interface (name, description, parameters, execute)
    so it can be registered with ToolRegistry.
    """

    def __init__(self, client: Any, tool_spec: dict[str, Any]) -> None:
        self._client = client
        self.name: str = tool_spec["name"]
        self.display_name: str | None = tool_spec.get("display_name")
        self.description: str = tool_spec.get("description", "")
        self.parameters: dict[str, Any] = tool_spec.get("parameters", {})
        self.output_schema: dict | None = tool_spec.get("output_schema")
        self.skip_persist: bool = False
        self.output_content_type: str | None = None
        self.input_contract: Any | None = None
        self.output_contract: Any | None = None
        self.runtime_policy: Any | None = None

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Forward the execute call to the plugin subprocess via JSON-RPC."""
        resp = await self._client.call("tool.execute", {
            "tool": self.name,
            "args": kwargs,
        })
        return ToolResult(
            success=resp.get("success", False),
            data=resp.get("data"),
            error=resp.get("error"),
        )


class ProxyChecker:
    """Implements ContentChecker Protocol by forwarding check() over JSON-RPC."""

    def __init__(self, client: Any, checker_spec: dict[str, Any]) -> None:
        self._client = client
        self._checker_name = checker_spec["name"]
        self.doc_type: str = checker_spec["doc_type"]

    async def check(self, text: str, subtype: str | None = None) -> ComplianceResult:
        """Forward the check call to the plugin subprocess via JSON-RPC."""
        resp = await self._client.call("checker.check", {
            "name": self._checker_name,
            "text": text,
            "subtype": subtype,
        })
        return ComplianceResult(
            is_valid=resp.get("is_valid", False),
            violations=[
                Violation(
                    rule_id=v["rule_id"],
                    message=v["message"],
                    severity=v.get("severity", "error"),
                    position=v.get("position"),
                )
                for v in resp.get("violations", [])
            ],
        )
```

- [ ] **Step 4: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_proxies.py -v
```

Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/plugin/proxies.py tests/plugin/test_proxies.py
git commit -m "feat: add ProxyTool and ProxyChecker for JSON-RPC forwarding"
```

---

### Task 8: ExtensionRegistry

**Files:**
- Create: `src/plugin/registry.py`
- Create: `tests/plugin/test_registry.py`

- [ ] **Step 1: Write the registry test**

In `tests/plugin/test_registry.py`:
```python
"""Tests for ExtensionRegistry."""

import logging

import pytest

from src.agent.tools.registry import ToolRegistry
from src.content_compliance.registry import CheckerRegistry
from src.plugin.registry import ExtensionRegistry
from src.plugin.proxies import ProxyTool, ProxyChecker


class MockClient:
    """Minimal mock for JSONRPCClient."""
    plugin_name = "test_plugin"

    async def call(self, method, params=None, timeout=30.0):
        return {}

    @property
    def plugin_name(self):
        return "test_plugin"


class TestExtensionRegistry:
    @pytest.fixture
    def tool_registry(self):
        return ToolRegistry()

    @pytest.fixture
    def checker_registry(self):
        return CheckerRegistry()

    @pytest.fixture
    def ext_registry(self, tool_registry, checker_registry):
        return ExtensionRegistry(
            tool_registry=tool_registry,
            checker_registry=checker_registry,
        )

    def test_register_tool_injects_proxy_into_tool_registry(self, ext_registry, tool_registry):
        client = MockClient()
        caps = [
            {
                "type": "tool",
                "name": "my_tool",
                "display_name": "My Tool",
                "description": "A test tool",
            },
        ]

        ext_registry.on_register("plugin_a", client, caps)

        # Tool should now be available in tool_registry
        tool = tool_registry.get("my_tool")
        assert tool is not None
        assert tool.name == "my_tool"
        assert tool.display_name == "My Tool"

    def test_register_checker_injects_proxy_into_checker_registry(self, ext_registry, checker_registry):
        client = MockClient()
        caps = [
            {
                "type": "checker",
                "name": "my_checker",
                "doc_type": "通知",
                "display_name": "My Checker",
            },
        ]

        ext_registry.on_register("plugin_b", client, caps)

        checker = checker_registry.get("通知")
        assert checker is not None
        assert checker.doc_type == "通知"

    def test_register_ignores_undeclared_capability(self, ext_registry, tool_registry, caplog):
        """Capabilities not in plugin.yaml should be ignored with a warning."""
        client = MockClient()
        caps = [{"type": "unknown_type", "name": "whatever"}]

        with caplog.at_level(logging.WARNING):
            ext_registry.on_register("plugin_c", client, caps)

        assert "unknown" in caplog.text.lower() or "unknown_type" in caplog.text.lower()

    def test_register_multiple_capabilities(self, ext_registry, tool_registry, checker_registry):
        client = MockClient()
        caps = [
            {"type": "tool", "name": "tool_a", "description": "Tool A"},
            {"type": "tool", "name": "tool_b", "description": "Tool B"},
            {"type": "checker", "name": "check_a", "doc_type": "报告"},
        ]

        ext_registry.on_register("multi_plugin", client, caps)

        assert tool_registry.get("tool_a") is not None
        assert tool_registry.get("tool_b") is not None
        assert checker_registry.get("报告") is not None

    def test_register_duplicate_overwrites_with_warning(self, ext_registry, tool_registry, caplog):
        client1 = MockClient()
        client2 = MockClient()

        ext_registry.on_register("plugin_1", client1, [
            {"type": "tool", "name": "same_tool", "description": "Original"},
        ])

        with caplog.at_level(logging.WARNING):
            ext_registry.on_register("plugin_2", client2, [
                {"type": "tool", "name": "same_tool", "description": "Override"},
            ])

        # The tool should still exist (overwritten)
        tool = tool_registry.get("same_tool")
        assert tool is not None

    def test_unregister_removes_all_proxies(self, ext_registry, tool_registry, checker_registry):
        client = MockClient()
        caps = [
            {"type": "tool", "name": "tool_x", "description": "X"},
            {"type": "checker", "name": "check_x", "doc_type": "请示"},
        ]

        ext_registry.on_register("plugin_x", client, caps)
        assert tool_registry.get("tool_x") is not None
        assert checker_registry.get("请示") is not None

        ext_registry.on_unregister("plugin_x")

        # Proxies should be removed
        with pytest.raises(KeyError):
            tool_registry.get("tool_x")
        assert checker_registry.get("请示") is None
```

- [ ] **Step 2: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_registry.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the registry**

In `src/plugin/registry.py`:
```python
"""ExtensionRegistry — routes plugin capabilities to existing registries."""

from __future__ import annotations

import logging
from typing import Any

from src.agent.tools.registry import ToolRegistry
from src.content_compliance.registry import CheckerRegistry
from .proxies import ProxyTool, ProxyChecker
from .client import JSONRPCClient

logger = logging.getLogger(__name__)


class ExtensionRegistry:
    """Receives plugin.register notifications and injects proxy objects
    into the corresponding existing registries (ToolRegistry, CheckerRegistry, etc.).

    Tracks which capabilities were registered by which plugin so they
    can be cleanly removed on unregister (crash/shutdown).
    """

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        checker_registry: CheckerRegistry | None = None,
    ) -> None:
        self._tool_registry = tool_registry
        self._checker_registry = checker_registry
        # plugin_name → set of registered names (tool names, doc_types)
        self._registrations: dict[str, dict[str, list[str]]] = {}

    def on_register(
        self,
        plugin_name: str,
        client: JSONRPCClient,
        capabilities: list[dict[str, Any]],
    ) -> None:
        """Process capabilities from a plugin.register notification.

        Creates proxy objects and injects them into applicable registries.
        Undeclared capability types are logged as a warning and skipped.
        """
        registrations: dict[str, list[str]] = {}

        for cap in capabilities:
            cap_type = cap.get("type", "")

            if cap_type == "tool":
                self._register_tool(plugin_name, client, cap)
                registrations.setdefault("tool", []).append(cap["name"])

            elif cap_type == "checker":
                self._register_checker(plugin_name, client, cap)
                registrations.setdefault("checker", []).append(cap["doc_type"])

            else:
                logger.warning(
                    "Plugin '%s' registered unknown capability type: %s",
                    plugin_name,
                    cap_type,
                )

        self._registrations[plugin_name] = registrations
        logger.info(
            "Plugin '%s' registered: %s",
            plugin_name,
            {k: len(v) for k, v in registrations.items()},
        )

    def on_unregister(self, plugin_name: str) -> None:
        """Remove all proxy objects registered by a plugin."""
        regs = self._registrations.pop(plugin_name, {})
        if not regs:
            return

        for cap_type, names in regs.items():
            if cap_type == "tool" and self._tool_registry:
                for name in names:
                    try:
                        self._tool_registry.unregister(name)
                    except (KeyError, AttributeError):
                        pass
            elif cap_type == "checker" and self._checker_registry:
                for doc_type in names:
                    try:
                        self._checker_registry.unregister(doc_type)
                    except (KeyError, AttributeError):
                        pass

    def _register_tool(
        self, plugin_name: str, client: JSONRPCClient, cap: dict
    ) -> None:
        if self._tool_registry is None:
            logger.debug("No ToolRegistry configured, skipping tool '%s'", cap.get("name"))
            return
        proxy = ProxyTool(client, cap)
        try:
            self._tool_registry.register(proxy)
        except ValueError:
            logger.warning(
                "Duplicate tool name '%s' from plugin '%s', overwriting",
                cap["name"],
                plugin_name,
            )
            # Remove old, register new
            try:
                self._tool_registry.unregister(cap["name"])
            except (KeyError, AttributeError):
                pass
            self._tool_registry.register(proxy)

    def _register_checker(
        self, plugin_name: str, client: JSONRPCClient, cap: dict
    ) -> None:
        if self._checker_registry is None:
            logger.debug("No CheckerRegistry configured, skipping checker '%s'", cap.get("name"))
            return
        proxy = ProxyChecker(client, cap)
        self._checker_registry.register(proxy)
```

- [ ] **Step 4: Note: need unregister methods on existing registries**

The `ToolRegistry` and `CheckerRegistry` need `unregister()` methods. Add them now.

In `src/agent/tools/registry.py`, add to `ToolRegistry`:
```python
def unregister(self, name: str) -> None:
    """Remove a tool by name. Raises KeyError if not found."""
    if name not in self._tools:
        raise KeyError(f"Tool not found: {name}")
    del self._tools[name]
```

In `src/content_compliance/registry.py`, add to `CheckerRegistry`:
```python
def unregister(self, doc_type: str) -> None:
    """Remove a checker by doc_type. No-op if not found."""
    self._checkers.pop(doc_type, None)
```

- [ ] **Step 5: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_registry.py -v
```

Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/plugin/registry.py tests/plugin/test_registry.py src/agent/tools/registry.py src/content_compliance/registry.py
git commit -m "feat: add ExtensionRegistry for routing plugin capabilities to registries"
```

---

### Task 9: ProcessManager

**Files:**
- Create: `src/plugin/manager.py`
- Create: `tests/plugin/test_manager.py`

- [ ] **Step 1: Write the manager test**

In `tests/plugin/test_manager.py`:
```python
"""Tests for ProcessManager state machine."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.plugin.manager import PluginProcess, PluginState, ProcessManager
from src.plugin.scanner import PluginScanResult, ScanStatus
from src.plugin.manifest import PluginManifest, RuntimeConfig, Dependencies, Capabilities


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plugins"


@pytest.fixture
def echo_scan_result():
    return PluginScanResult(
        name="echo_plugin",
        dir=FIXTURES_DIR / "echo_plugin",
        status=ScanStatus.VALID,
        manifest=PluginManifest(
            name="echo_plugin",
            version="0.1.0",
            api="1.0",
            runtime=RuntimeConfig(entry="entry.py"),
        ),
    )


@pytest.fixture
def mock_ext_registry():
    reg = MagicMock()
    reg.on_register = MagicMock()
    reg.on_unregister = MagicMock()
    return reg


class TestPluginProcess:
    def test_initial_state_is_scanned(self, echo_scan_result):
        proc = PluginProcess(
            name="echo_plugin",
            manifest=echo_scan_result.manifest,
            plugin_dir=echo_scan_result.dir,
        )
        assert proc.state == PluginState.SCANNED
        assert proc._restart_count == 0
        assert proc.client is None

    def test_client_raises_when_not_ready(self, echo_scan_result):
        proc = PluginProcess(
            name="echo_plugin",
            manifest=echo_scan_result.manifest,
            plugin_dir=echo_scan_result.dir,
        )
        with pytest.raises(RuntimeError):
            _ = proc.client


class TestProcessManager:
    @pytest.fixture
    def manager(self, mock_ext_registry):
        return ProcessManager(
            plugin_dir=FIXTURES_DIR,
            extension_registry=mock_ext_registry,
        )

    @pytest.mark.asyncio
    async def test_start_all_integration(self, manager, mock_ext_registry):
        """Integration test: start echo_plugin as real subprocess."""
        # Only run if fixtures/plugins/echo_plugin/entry.py exists
        entry_path = FIXTURES_DIR / "echo_plugin" / "entry.py"
        if not entry_path.exists():
            pytest.skip("echo_plugin entry.py not found")

        results = [
            PluginScanResult(
                name="echo_plugin",
                dir=FIXTURES_DIR / "echo_plugin",
                status=ScanStatus.VALID,
                manifest=PluginManifest(
                    name="echo_plugin",
                    version="0.1.0",
                    api="1.0",
                    runtime=RuntimeConfig(entry="entry.py"),
                ),
            )
        ]

        await manager.start_all(results)
        assert "echo_plugin" in manager._processes

        proc = manager._processes["echo_plugin"]
        assert proc.state == PluginState.ACTIVE

        # Verify the registry was called
        mock_ext_registry.on_register.assert_called_once()

        await manager.shutdown()
        assert proc.state == PluginState.STOPPED

    def test_shutdown_on_empty_manager(self, manager):
        """Shutdown with no plugins should not error."""
        import asyncio
        asyncio.run(manager.shutdown())
        # No exception = pass
```

- [ ] **Step 2: Create echo_plugin entry.py for integration test**

In `tests/fixtures/plugins/echo_plugin/entry.py`:
```python
"""A minimal valid plugin entry point for integration testing."""

import json
import sys


def main():
    """Simple JSON-RPC server that responds to health and echo tool calls."""
    # Send register notification first
    register_msg = {
        "method": "plugin.register",
        "params": {
            "capabilities": [
                {"type": "tool", "name": "echo", "display_name": "Echo"},
            ]
        },
    }
    sys.stdout.write(json.dumps(register_msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()

    # Process requests
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "id" not in msg or "method" not in msg:
            continue

        req_id = msg["id"]
        method = msg.get("method", "")

        if method == "plugin.health":
            sys.stdout.write(json.dumps({"id": req_id, "result": "ok"}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        elif method == "plugin.shutdown":
            sys.stdout.write(json.dumps({"id": req_id, "result": "ok"}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            break
        elif method == "tool.execute":
            params = msg.get("params", {})
            sys.stdout.write(json.dumps({
                "id": req_id,
                "result": {
                    "success": True,
                    "data": {"echo": params.get("args", {})},
                },
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(json.dumps({
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            }, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run test — verify failure**

```bash
uv run pytest tests/plugin/test_manager.py -v
```

Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 4: Write the manager**

In `src/plugin/manager.py`:
```python
"""ProcessManager — manages plugin subprocess lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .client import JSONRPCClient
from .scanner import PluginScanResult

logger = logging.getLogger(__name__)


class PluginState(str, Enum):
    SCANNED = "SCANNED"
    LOADING = "LOADING"
    REGISTERING = "REGISTERING"
    ACTIVE = "ACTIVE"
    CRASHED = "CRASHED"
    RESTARTING = "RESTARTING"
    FATAL = "FATAL"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass
class PluginProcess:
    """Handle for a single plugin subprocess."""

    name: str
    manifest: "PluginManifest"
    plugin_dir: Path
    state: PluginState = PluginState.SCANNED
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _client: JSONRPCClient | None = field(default=None, repr=False)
    _restart_count: int = field(default=0, repr=False)
    _health_task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def client(self) -> JSONRPCClient:
        if self._client is None:
            raise RuntimeError(
                f"Plugin '{self.name}' is not ready (state: {self.state.value})"
            )
        return self._client


class ProcessManager:
    """Manages the lifecycle of all plugin subprocesses."""

    def __init__(
        self,
        plugin_dir: Path,
        extension_registry: "ExtensionRegistry",
        max_restarts: int = 3,
    ) -> None:
        self._plugin_dir = plugin_dir
        self._extension_registry = extension_registry
        self._max_restarts = max_restarts
        self._processes: dict[str, PluginProcess] = {}

    async def start_all(self, results: list[PluginScanResult]) -> None:
        """Start all valid plugins from scan results."""
        for result in results:
            if result.status.value != "VALID":
                logger.warning(
                    "Skipping plugin '%s': %s — %s",
                    result.name,
                    result.status.value,
                    result.error,
                )
                continue
            if result.manifest is None:
                continue

            proc = PluginProcess(
                name=result.name,
                manifest=result.manifest,
                plugin_dir=result.dir,
            )
            self._processes[result.name] = proc
            try:
                await self._start_one(proc)
            except Exception:
                logger.error(
                    "Failed to start plugin '%s'",
                    result.name,
                    exc_info=True,
                )
                proc.state = PluginState.FATAL

    async def _start_one(self, proc: PluginProcess) -> None:
        """Start a single plugin subprocess and wait for registration."""
        proc.state = PluginState.LOADING

        entry = proc.manifest.runtime.entry if proc.manifest else "entry.py"
        entry_path = proc.plugin_dir / entry
        if not entry_path.exists():
            raise FileNotFoundError(f"Entry point not found: {entry_path}")

        env = dict(os.environ)
        if proc.manifest and proc.manifest.runtime.env:
            env.update(proc.manifest.runtime.env)

        proc._process = await asyncio.create_subprocess_exec(
            "uv", "run", entry,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(proc.plugin_dir),
            env=env,
        )

        proc._client = JSONRPCClient(
            reader=proc._process.stdout,
            writer=proc._process.stdin,
            plugin_name=proc.name,
        )

        # Wait for register notification
        proc.state = PluginState.REGISTERING
        try:
            capabilities = await proc._client.wait_for_register(timeout=10.0)
        except asyncio.TimeoutError:
            proc.state = PluginState.FATAL
            logger.error(
                "Plugin '%s' did not send register notification within 10s, marking FATAL",
                proc.name,
            )
            await self._kill_process(proc)
            raise

        # Route capabilities to registries
        self._extension_registry.on_register(proc.name, proc._client, capabilities)
        proc.state = PluginState.ACTIVE
        proc._restart_count = 0

        # Start stderr reader for crash detection
        asyncio.create_task(self._monitor_stderr(proc))

        # Start health check loop
        proc._health_task = asyncio.create_task(self._health_loop(proc))

        logger.info("Plugin '%s' is ACTIVE", proc.name)

    async def _monitor_stderr(self, proc: PluginProcess) -> None:
        """Read stderr for logging and crash detection."""
        try:
            while proc._process and proc._process.stderr:
                line = await proc._process.stderr.readline()
                if not line:
                    break
                logger.debug("[plugin:%s] %s", proc.name, line.decode().rstrip())
        except Exception:
            pass

    async def _health_loop(self, proc: PluginProcess) -> None:
        """Periodic health check loop (every 30s)."""
        while proc.state == PluginState.ACTIVE:
            await asyncio.sleep(30)
            if proc.state != PluginState.ACTIVE:
                break
            try:
                result = await proc.client.call("plugin.health", timeout=5.0)
                if result != "ok":
                    logger.warning("Plugin '%s' health check returned: %s", proc.name, result)
            except Exception:
                logger.warning("Plugin '%s' health check failed", proc.name)
                await self._on_crash(proc)
                break

    async def _on_crash(self, proc: PluginProcess) -> None:
        """Handle a plugin subprocess crash."""
        proc.state = PluginState.CRASHED
        logger.error("Plugin '%s' crashed", proc.name)

        # Unregister from extension registries
        self._extension_registry.on_unregister(proc.name)

        # Cancel health task
        if proc._health_task:
            proc._health_task.cancel()
            proc._health_task = None

        # Clean up old process
        await self._kill_process(proc)
        if proc._client:
            proc._client.close()
            proc._client = None

        # Attempt restart
        if proc._restart_count < self._max_restarts:
            proc._restart_count += 1
            delay = min(1 * (2 ** (proc._restart_count - 1)), 30)
            proc.state = PluginState.RESTARTING
            logger.info(
                "Plugin '%s' restart %d/%d in %ds",
                proc.name,
                proc._restart_count,
                self._max_restarts,
                delay,
            )
            await asyncio.sleep(delay)
            try:
                await self._start_one(proc)
            except Exception:
                logger.error(
                    "Plugin '%s' restart failed",
                    proc.name,
                    exc_info=True,
                )
                await self._on_crash(proc)
        else:
            proc.state = PluginState.FATAL
            logger.error(
                "Plugin '%s' crashed %d times, marking FATAL",
                proc.name,
                proc._restart_count,
            )

    async def shutdown(self) -> None:
        """Gracefully shut down all plugins."""
        for proc in self._processes.values():
            if proc.state in (PluginState.ACTIVE, PluginState.REGISTERING):
                proc.state = PluginState.STOPPING
                self._extension_registry.on_unregister(proc.name)

                if proc._health_task:
                    proc._health_task.cancel()
                    proc._health_task = None

                if proc._client:
                    proc._client.notify("plugin.shutdown")

                try:
                    if proc._process:
                        await asyncio.wait_for(proc._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Plugin '%s' shutdown timeout, force killing", proc.name)
                    await self._kill_process(proc)

                if proc._client:
                    proc._client.close()
                    proc._client = None

                proc.state = PluginState.STOPPED

    async def _kill_process(self, proc: PluginProcess) -> None:
        """Force kill a plugin subprocess."""
        if proc._process and proc._process.returncode is None:
            try:
                proc._process.kill()
                await proc._process.wait()
            except Exception:
                pass
```

- [ ] **Step 5: Run test — verify pass**

```bash
uv run pytest tests/plugin/test_manager.py -v
```

Expected: PASS (the tests that can run without real subprocess should pass)

- [ ] **Step 6: Commit**

```bash
git add src/plugin/manager.py tests/plugin/test_manager.py tests/fixtures/plugins/echo_plugin/entry.py
git commit -m "feat: add ProcessManager for plugin subprocess lifecycle"
```

---

### Task 10: Module public API and integration wiring

**Files:**
- Modify: `src/plugin/__init__.py`
- Create: `plugins/.gitkeep`

- [ ] **Step 1: Write the module public API**

Replace `src/plugin/__init__.py`:
```python
"""DocAudit Plugin System.

Provides a full-stack plugin system with subprocess isolation and
JSON-RPC over stdio communication.

Usage:
    from src.plugin import PluginSystem

    plugin_system = PluginSystem(
        plugins_dir="plugins",
        tool_registry=tool_registry,
        checker_registry=checker_registry,
    )
    await plugin_system.start()
    # ... app runs ...
    await plugin_system.shutdown()
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .manifest import PluginManifest, Capabilities
from .scanner import PluginScanner, PluginScanResult, ScanStatus
from .protocol import (
    JSONRPCRequest,
    JSONRPCResponse,
    JSONRPCNotification,
    JSONRPCStreamChunk,
    ErrorCodes,
)
from .client import JSONRPCClient, PluginRPCError, PluginCrashedError
from .manager import ProcessManager, PluginProcess, PluginState
from .registry import ExtensionRegistry
from .proxies import ProxyTool, ProxyChecker

logger = logging.getLogger(__name__)

__all__ = [
    # Main entry point
    "PluginSystem",
    # Models
    "PluginManifest",
    "Capabilities",
    # Scanner
    "PluginScanner",
    "PluginScanResult",
    "ScanStatus",
    # Protocol
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCNotification",
    "JSONRPCStreamChunk",
    "ErrorCodes",
    # Client
    "JSONRPCClient",
    "PluginRPCError",
    "PluginCrashedError",
    # Manager
    "ProcessManager",
    "PluginProcess",
    "PluginState",
    # Registry
    "ExtensionRegistry",
    # Proxies
    "ProxyTool",
    "ProxyChecker",
]


class PluginSystem:
    """Top-level orchestrator for the plugin system.

    Wires together PluginScanner, ProcessManager, and ExtensionRegistry
    into a single start/stop interface.
    """

    def __init__(
        self,
        plugins_dir: str | Path = "plugins",
        tool_registry: Any = None,
        checker_registry: Any = None,
    ) -> None:
        self._plugins_dir = Path(plugins_dir)
        self._scanner = PluginScanner()
        self._registry = ExtensionRegistry(
            tool_registry=tool_registry,
            checker_registry=checker_registry,
        )
        self._manager = ProcessManager(
            plugin_dir=self._plugins_dir,
            extension_registry=self._registry,
        )
        self._started = False

    async def start(self) -> dict[str, str]:
        """Scan plugins directory and start all valid plugins.

        Returns a dict of plugin_name → status for monitoring.
        """
        if self._started:
            raise RuntimeError("PluginSystem already started")

        if not self._plugins_dir.exists():
            logger.info("Plugins directory '%s' does not exist, creating it", self._plugins_dir)
            self._plugins_dir.mkdir(parents=True, exist_ok=True)

        results = self._scanner.scan(self._plugins_dir)
        logger.info(
            "Found %d plugin(s): %d valid, %d blocked",
            len(results),
            sum(1 for r in results if r.status == ScanStatus.VALID),
            sum(1 for r in results if r.status == ScanStatus.BLOCKED),
        )

        for r in results:
            if r.status == ScanStatus.BLOCKED:
                logger.warning("Plugin '%s' BLOCKED: %s", r.name, r.error)

        await self._manager.start_all(results)
        self._started = True

        status = {}
        for name, proc in self._manager._processes.items():
            status[name] = proc.state.value
        return status

    async def shutdown(self) -> None:
        """Gracefully shut down all plugins."""
        if not self._started:
            return
        await self._manager.shutdown()
        self._started = False

    def get_status(self) -> dict[str, dict]:
        """Return current status of all plugins."""
        status = {}
        for name, proc in self._manager._processes.items():
            status[name] = {
                "state": proc.state.value,
                "version": proc.manifest.version if proc.manifest else "unknown",
                "restart_count": proc._restart_count,
            }
        return status
```

- [ ] **Step 2: Create plugins directory**

```bash
mkdir -p plugins && touch plugins/.gitkeep
echo "plugins/" >> .gitignore
```

- [ ] **Step 3: Commit**

```bash
git add src/plugin/__init__.py plugins/.gitkeep .gitignore
git commit -m "feat: add PluginSystem orchestrator and public API"
```

---

### Task 11: Full integration test

**Files:**
- Create: `tests/plugin/test_integration.py`

- [ ] **Step 1: Write the integration test**

In `tests/plugin/test_integration.py`:
```python
"""Full integration tests: real subprocess lifecycle."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from src.plugin import PluginSystem

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plugins"


@pytest.mark.integration
class TestPluginSystemIntegration:
    """Tests using real subprocesses with the echo_plugin fixture."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Start echo_plugin, call a tool, shut down."""
        ps = PluginSystem(plugins_dir=FIXTURES_DIR)

        status = await ps.start()
        assert "echo_plugin" in status
        assert status["echo_plugin"] == "ACTIVE"

        # The echo_plugin tool should be registered in our ToolRegistry
        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_empty_plugins_dir(self, tmp_path):
        """Empty plugins directory should not error."""
        plugins_dir = tmp_path / "empty_plugins"
        plugins_dir.mkdir()

        ps = PluginSystem(plugins_dir=plugins_dir)
        status = await ps.start()
        assert status == {}
        await ps.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_before_start_noop(self):
        """Shutting down before start should be a no-op."""
        ps = PluginSystem(plugins_dir=FIXTURES_DIR)
        await ps.shutdown()  # Should not raise

    @pytest.mark.asyncio
    async def test_get_status(self):
        """get_status should return plugin states."""
        ps = PluginSystem(plugins_dir=FIXTURES_DIR)
        await ps.start()

        status = ps.get_status()
        assert "echo_plugin" in status
        assert status["echo_plugin"]["state"] == "ACTIVE"
        assert status["echo_plugin"]["version"] == "0.1.0"

        await ps.shutdown()
```

- [ ] **Step 2: Run integration test**

```bash
uv run pytest tests/plugin/test_integration.py -v -m integration
```

Expected: PASS

- [ ] **Step 3: Run all plugin tests**

```bash
uv run pytest tests/plugin/ -v
```

Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add tests/plugin/test_integration.py
git commit -m "test: add full integration tests for plugin system"
```

---

## Plan Self-Review

1. **Spec coverage:**
   - Architecture (Section 2) → Tasks 2-10 cover all components
   - JSON-RPC Protocol (Section 3) → Task 3
   - Plugin Manifest (Section 4) → Task 2
   - ExtensionRegistry (Section 5) → Task 8
   - ProcessManager (Section 6) → Task 9
   - Error Handling (Section 7) → Embedded in client.py (Task 5), manager.py (Task 9), runtime.py (Task 6)
   - Testing Strategy (Section 8) → Tasks 2-11 each include tests with actual code
   - No gaps found

2. **Placeholder scan:** No TBD, TODO, or "implement later" patterns found. All code is concrete.

3. **Type consistency:** Types used across tasks are consistent: `PluginManifest` defined in Task 2, used in Task 4, 9, 10. `JSONRPCClient` defined in Task 5, used in Task 7, 8, 9.

4. **Existing tests not broken:** `ToolRegistry` and `CheckerRegistry` gain `unregister()` method in Task 8 — existing tests continue to pass because `register()` behavior is unchanged.
