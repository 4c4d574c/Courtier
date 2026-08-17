#!/usr/bin/env python3
"""Dev runner: start all plugin servers locally in one terminal.

Reads ``plugins/plugin.env`` (if present) into the environment, then
launches every plugin that has a ``.venv`` and a ``plugin.yaml`` under
``plugins/`` as an independent TCP server on its manifest port
(``runtime.port``).  Logs are prefixed with the plugin name.

This is a development convenience only — no lifecycle management; Ctrl+C
stops everything.  Production uses docker-compose (Dockerfile.plugins)
or systemd units instead.

Usage:
    python scripts/dev-plugins.py            # all plugins
    python scripts/dev-plugins.py anydoc parse   # selected plugins
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_ROOT = REPO_ROOT / "plugins"


def _load_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser for plugins/plugin.env."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _plugin_port(plugin_yaml: Path) -> int | None:
    for line in plugin_yaml.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("port:"):
            try:
                return int(stripped.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _discover(selected: set[str]) -> list[tuple[str, Path, int]]:
    found: list[tuple[str, Path, int]] = []
    for manifest in sorted(PLUGINS_ROOT.rglob("plugin.yaml")):
        plugin_dir = manifest.parent
        name = plugin_dir.name
        if selected and name not in selected:
            continue
        if not (plugin_dir / ".venv" / "bin" / "python").is_file():
            print(f"[dev-plugins] skip {name}: no .venv (run `uv sync` in {plugin_dir})")
            continue
        port = _plugin_port(manifest)
        if port is None:
            print(f"[dev-plugins] skip {name}: no runtime.port in plugin.yaml")
            continue
        found.append((name, plugin_dir, port))
    return found


def _tee(stream, prefix: str) -> None:
    for raw in iter(stream.readline, b""):
        sys.stdout.write(f"[{prefix}] {raw.decode('utf-8', errors='replace')}")
        sys.stdout.flush()


def main() -> int:
    selected = set(sys.argv[1:])
    plugins = _discover(selected)
    if not plugins:
        print("[dev-plugins] nothing to start")
        return 1

    env = os.environ.copy()
    for key, value in _load_env_file(PLUGINS_ROOT / "plugin.env").items():
        env.setdefault(key, value)

    procs: list[subprocess.Popen] = []
    for name, plugin_dir, port in plugins:
        cmd = [
            str(plugin_dir / ".venv" / "bin" / "python"),
            "entry.py",
            "--listen",
            f"127.0.0.1:{port}",
        ]
        proc = subprocess.Popen(
            cmd,
            cwd=str(plugin_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        threading.Thread(target=_tee, args=(proc.stdout, name), daemon=True).start()
        procs.append(proc)
        print(f"[dev-plugins] {name} listening on 127.0.0.1:{port} (pid {proc.pid})")

    print("[dev-plugins] all started; Ctrl+C to stop")

    def _stop(*_):
        for proc in procs:
            proc.terminate()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    exit_code = 0
    for proc in procs:
        code = proc.wait()
        if code != 0:
            exit_code = code
            print(f"[dev-plugins] pid {proc.pid} exited with {code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
