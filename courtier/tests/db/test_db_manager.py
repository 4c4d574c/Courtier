"""Tests for AsyncDatabase and database utilities."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import make_url

from courtier.db.db_manager import AsyncDatabase, ensure_database_exists


class TestEnsureDatabaseExists:
    """Tests for ensure_database_exists."""

    async def test_skips_empty_url(self):
        """Empty db_url is a no-op."""
        await ensure_database_exists("")

    async def test_skips_unsupported_driver(self):
        """Non-asyncmy drivers are skipped with a debug log."""
        await ensure_database_exists("postgresql+asyncpg://user:pass@localhost/db")

    async def test_skips_missing_database_name(self):
        """URL without a database name is a no-op."""
        await ensure_database_exists("mysql+asyncmy://user:pass@localhost/")

    async def test_creates_database_when_missing(self):
        """CREATE DATABASE is executed when target db does not exist."""
        mock_conn = AsyncMock()
        mock_engine = MagicMock()
        mock_engine.connect = MagicMock()
        mock_engine.connect.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_engine.connect.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_engine.dispose = AsyncMock()

        with patch(
            "courtier.db.db_manager.create_async_engine", return_value=mock_engine
        ) as mock_create_engine:
            await ensure_database_exists(
                "mysql+asyncmy://root:pwd@127.0.0.1:3366/doc_audit"
            )

        mock_create_engine.assert_called_once()
        call_url = make_url(mock_create_engine.call_args[0][0])
        assert call_url.drivername == "mysql+asyncmy"
        assert call_url.host == "127.0.0.1"
        assert call_url.port == 3366
        assert call_url.database == ""
        assert call_url.password == "pwd"  # password must not be masked with '***'

        mock_conn.execute.assert_called_once()
        executed_sql = str(mock_conn.execute.call_args[0][0])
        assert "CREATE DATABASE IF NOT EXISTS `doc_audit`" in executed_sql
        mock_engine.dispose.assert_awaited_once()


class TestAsyncDatabase:
    """Tests for AsyncDatabase."""

    async def test_ensure_database_passes_unmasked_url(self):
        """ensure_database passes an unmasked URL to ensure_database_exists."""
        db_url = "mysql+asyncmy://root:pwd@127.0.0.1:3366/doc_audit"

        with patch(
            "courtier.db.db_manager.ensure_database_exists"
        ) as mock_ensure, patch(
            "courtier.db.db_manager.create_async_engine"
        ) as mock_create_engine:
            mock_engine = MagicMock()
            mock_engine.url = make_url(db_url)
            mock_create_engine.return_value = mock_engine

            db = AsyncDatabase(db_url)
            await db.ensure_database()

        mock_ensure.assert_awaited_once()
        passed_url = make_url(mock_ensure.await_args[0][0])
        assert passed_url.password == "pwd"
