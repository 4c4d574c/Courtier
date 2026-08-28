"""Alembic 迁移的离线断言（不连真实数据库）。

用 op 记录器替换迁移模块内的 ``alembic.op`` 代理，直接调用
upgrade()/downgrade() 并断言发出的 DDL 操作；同时校验版本链衔接。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic" / "versions"

# The migration this file's upgrade/downgrade tests describe.
_NEW_REVISION = "43e68af4536c"
_PREV_REVISION = "b71c4f0e9d23"


def _load_migration(revision: str) -> ModuleType:
    """按 revision 前缀从 versions 目录加载迁移模块。"""
    (path,) = _VERSIONS_DIR.glob(f"{revision}_*.py")
    module_name = f"test_alembic_{revision}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 登记到 sys.modules，避免 dataclass 等按模块名回查时失败
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


class _OpRecorder:
    """记录迁移发出的 op 调用；未显式拦截的操作退化为通用记录。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def drop_column(self, table_name: str, column_name: str, **kw) -> None:
        self.calls.append(("drop_column", table_name, column_name))

    def add_column(self, table_name: str, column) -> None:
        self.calls.append(("add_column", table_name, column))

    def __getattr__(self, name: str):
        def _record(*args, **kw):
            self.calls.append((name, args, kw))

        return _record


class TestRevisionChain:
    def test_new_revision_links_to_previous_head(self):
        new = _load_migration(_NEW_REVISION)
        prev = _load_migration(_PREV_REVISION)
        assert new.revision == _NEW_REVISION
        assert new.down_revision == _PREV_REVISION
        assert prev.revision == _PREV_REVISION

    def test_migration_chain_has_single_head(self):
        """加载全部迁移构建版本链：链上必须恰好一个 head（不绑定特定版本，
        新增迁移天然通过）。"""
        revisions: dict[str, str | None] = {}
        for path in sorted(_VERSIONS_DIR.glob("*.py")):
            module = _load_migration(path.name.split("_")[0])
            revisions[module.revision] = (
                module.down_revision if isinstance(module.down_revision, str) else None
            )
        referenced_as_parent = {d for d in revisions.values() if d is not None}
        heads = set(revisions) - referenced_as_parent
        assert len(heads) == 1, f"expected a single head, got {sorted(heads)}"


class TestDropResidualColumnsUpgrade:
    def test_upgrade_drops_both_columns(self):
        migration = _load_migration(_NEW_REVISION)
        recorder = _OpRecorder()
        migration.op = recorder  # type: ignore[attr-defined]

        migration.upgrade()

        assert ("drop_column", "documents", "user_id") in recorder.calls
        assert ("drop_column", "pages", "save_path") in recorder.calls

    def test_downgrade_restores_columns_with_server_defaults(self):
        migration = _load_migration(_NEW_REVISION)
        recorder = _OpRecorder()
        migration.op = recorder  # type: ignore[attr-defined]

        migration.downgrade()

        add_calls = [c for c in recorder.calls if c[0] == "add_column"]
        by_table = {c[1]: c[2] for c in add_calls}
        assert set(by_table) == {"documents", "pages"}

        user_id_col = by_table["documents"]
        assert user_id_col.name == "user_id"
        assert not user_id_col.nullable
        assert user_id_col.server_default.arg == ""
        assert user_id_col.type.length == 512

        save_path_col = by_table["pages"]
        assert save_path_col.name == "save_path"
        assert not save_path_col.nullable
        assert save_path_col.server_default.arg == "默认路径"
        assert save_path_col.type.length == 512
