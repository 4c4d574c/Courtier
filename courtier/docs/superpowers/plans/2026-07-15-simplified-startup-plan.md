# 简化后端启动入口实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在项目根目录新增 `main.py`，支持 `uv run main.py [--host] [--port] [--reload]` 一键启动后端，并自动执行数据库迁移。

**架构：** 根目录 `main.py` 在启动时把 monorepo 源码路径注入 `sys.path`，解析命令行参数，调用 `alembic.command.upgrade` 迁移数据库，最后调用 `uvicorn.run` 启动 FastAPI 应用工厂。

**技术栈：** Python 3.12+, uv, uvicorn, alembic, click/argparse

---

## 文件清单

| 文件 | 职责 |
|------|------|
| `courtier/main.py` | 新增：后端启动入口，设置 `sys.path`、解析参数、跑迁移、启动 uvicorn |
| `courtier/tests/test_main.py` | 新增：测试 `sys.path` 注入、参数解析、迁移调用、uvicorn 调用 |
| `courtier/CLAUDE.md` | 修改：更新启动命令示例为 `uv run main.py` |
| `courtier/README.md` | 修改：更新启动命令示例为 `uv run main.py` |

---

### 任务 1：编写 `main.py` 启动入口

**文件：**
- 创建：`courtier/main.py`
- 测试：`courtier/tests/test_main.py`

**说明：** 实现根目录启动脚本，自动设置 `PYTHONPATH`、解析参数、迁移数据库、启动服务。

- [ ] **步骤 1：编写失败的测试**

```python
"""Tests for the root main.py startup entrypoint."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


class TestMainPathSetup:
    def test_injects_monorepo_paths(self):
        """main.py adds courtier source dirs to sys.path."""
        root = _project_root()
        expected_paths = [
            str(root),
            str(root / "domains" / "docaudit"),
            str(root / "libs" / "shared"),
            str(root / "libs" / "docaudit"),
        ]

        with patch.dict(sys.modules, {"main": None}), patch.object(
            sys, "path", list(sys.path)
        ):
            import importlib
            import main

            importlib.reload(main)

        for p in expected_paths:
            assert p in sys.path


class TestMainArgumentParsing:
    def test_defaults(self):
        """Default host/port/reload values are correct."""
        from main import _parse_args

        args = _parse_args([])
        assert args.host == "0.0.0.0"
        assert args.port == 8000
        assert args.reload is False

    def test_custom_values(self):
        """Custom flags override defaults."""
        from main import _parse_args

        args = _parse_args(["--host", "127.0.0.1", "--port", "8080", "--reload"])
        assert args.host == "127.0.0.1"
        assert args.port == 8080
        assert args.reload is True


class TestMainStartupFlow:
    def test_runs_migrations_and_starts_uvicorn(self):
        """main() runs alembic upgrade then uvicorn.run with factory app."""
        with patch("main.alembic_command") as mock_alembic, patch(
            "main.uvicorn_run"
        ) as mock_uvicorn:
            from main import main

            main(["--host", "127.0.0.1", "--port", "9000"])

        mock_alembic.upgrade.assert_called_once()
        config_arg = mock_alembic.upgrade.call_args[0][0]
        assert config_arg.config_file_name.endswith("alembic.ini")
        assert mock_alembic.upgrade.call_args[0][1] == "head"

        mock_uvicorn.assert_called_once()
        call_kwargs = mock_uvicorn.call_args.kwargs
        assert call_kwargs["host"] == "127.0.0.1"
        assert call_kwargs["port"] == 9000
        assert call_kwargs["factory"] is True
        assert call_kwargs["app"] == "courtier.agent.api.app:create_app"
```

- [ ] **步骤 2：运行测试验证失败**

运行：

```bash
cd /home/lmwl/Documents/docaudit/agent/courtier
uv run pytest tests/test_main.py -v
```

预期：多个 FAIL，报错 `ModuleNotFoundError: No module named 'main'` 或 `_parse_args` 未定义。

- [ ] **步骤 3：编写最少实现代码**

创建 `courtier/main.py`：

```python
"""Courtier backend startup entrypoint.

Usage:
    uv run main.py
    uv run main.py --host 127.0.0.1 --port 8080 --reload
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig


def _inject_monorepo_paths() -> None:
    """Add courtier source directories to sys.path.

    This removes the need to set PYTHONPATH manually when running from the
    project root.
    """
    project_root = Path(__file__).resolve().parent
    paths = [
        str(project_root),
        str(project_root / "domains" / "docaudit"),
        str(project_root / "libs" / "shared"),
        str(project_root / "libs" / "docaudit"),
    ]
    # Prepend so these take priority over any other installed packages.
    for path in reversed(paths):
        if path not in sys.path:
            sys.path.insert(0, path)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the backend server."""
    parser = argparse.ArgumentParser(description="Courtier backend server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind socket to this host")
    parser.add_argument("--port", type=int, default=8000, help="Bind socket to this port")
    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="Enable auto-reload on code changes",
    )
    return parser.parse_args(argv)


def _run_migrations() -> None:
    """Apply all pending Alembic migrations."""
    project_root = Path(__file__).resolve().parent
    alembic_cfg = AlembicConfig(str(project_root / "alembic.ini"))
    alembic_command.upgrade(alembic_cfg, "head")


def main(argv: list[str] | None = None) -> None:
    """Run migrations and start the uvicorn server."""
    _inject_monorepo_paths()
    args = _parse_args(argv)
    _run_migrations()
    uvicorn.run(
        "courtier.agent.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
```

- [ ] **步骤 4：运行测试验证通过**

运行：

```bash
uv run pytest tests/test_main.py -v
```

预期：4 个测试全部 PASS。

- [ ] **步骤 5：手动验证服务器启动**

运行：

```bash
uv run main.py --host 127.0.0.1 --port 8000
```

预期：
- 终端显示 `Running upgrade ... -> head`
- 8 个插件注册成功
- `Uvicorn running on http://127.0.0.1:8000`
- 按 Ctrl+C 正常退出

- [ ] **步骤 6：Commit**

```bash
git add courtier/main.py courtier/tests/test_main.py
git commit -m "feat: add root main.py startup entrypoint

Add courtier/main.py so the backend can be started with:
  uv run main.py

Supports --host, --port, --reload and automatically runs
alembic upgrade head before starting uvicorn."
```

---

### 任务 2：更新 CLAUDE.md 启动命令

**文件：**
- 修改：`courtier/CLAUDE.md:63-64`

- [ ] **步骤 1：修改启动命令示例**

把：

```bash
# 运行 API 服务
PYTHONPATH=courtier:domains/docaudit:libs/shared:libs/docaudit \
  uv run python -m uvicorn courtier.agent.api.app:create_app --factory --host 0.0.0.0 --port 8000
```

改成：

```bash
# 运行 API 服务
uv run main.py

# 自定义监听地址或启用热重载
uv run main.py --host 127.0.0.1 --port 8080 --reload
```

- [ ] **步骤 2：Commit**

```bash
git add courtier/CLAUDE.md
git commit -m "docs: simplify startup command in CLAUDE.md"
```

---

### 任务 3：更新 README.md 启动命令

**文件：**
- 修改：`courtier/README.md:109-117`

- [ ] **步骤 1：修改启动命令示例**

找到 README 中类似下面的命令：

```bash
PYTHONPATH=courtier:domains/docaudit:libs/shared:libs/docaudit \
  uv run python -m uvicorn courtier.agent.api.app:create_app \
  --factory --host 0.0.0.0 --port 8000
```

改成：

```bash
uv run main.py
```

如果 README 里有其他重复出现的相同长命令，一并替换。

- [ ] **步骤 2：Commit**

```bash
git add courtier/README.md
git commit -m "docs: simplify startup command in README.md"
```

---

## 自检

**1. 规格覆盖度：**
- `uv run main.py` 启动 → 任务 1 步骤 3
- 支持 `--host`/`--port`/`--reload` → 任务 1 步骤 1/3
- 自动跑迁移 → 任务 1 步骤 3 的 `_run_migrations`
- 更新文档 → 任务 2、任务 3

**2. 占位符扫描：**
- 无 "TODO"/"待定"/"后续实现"
- 测试代码和实现代码均为完整可运行代码
- 命令和预期输出明确

**3. 类型一致性：**
- `_parse_args` 返回 `argparse.Namespace`
- `main(argv: list[str] | None = None)` 签名一致
- `uvicorn.run` 参数名与 argparse 解析结果一致

---

## 执行方式

计划已完成并保存到 `docs/superpowers/plans/2026-07-15-simplified-startup-plan.md`。

**两种执行方式：**

1. **子代理驱动（推荐）** - 每个任务调度一个新的子代理，任务间进行审查，快速迭代
2. **内联执行** - 在当前会话中使用 executing-plans 执行任务，批量执行并设有检查点

选哪种方式？
