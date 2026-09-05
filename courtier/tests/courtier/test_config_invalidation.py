"""Hot-reload seams added in Phase 0: client invalidation hooks and the
RunManager settings listener.  Nothing fires them in production yet (the
env-only ConfigService never replaces the snapshot); these tests fix the
seam contract before the DB-backed phases drive it."""

from types import SimpleNamespace

import courtier.es.client as es_client
import courtier.storage.client as storage_client
from courtier.agent.api.services.run_manager import RunManager


class _FakeEsClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self):
        self.closed = True


class _FakeSettings:
    def __init__(self, **kwargs):
        self.es_hosts = kwargs.get("es_hosts", "http://es:9200")
        self.es_username = kwargs.get("es_username", "")
        self.es_password = kwargs.get("es_password", "")
        self.minio_endpoint = kwargs.get("minio_endpoint", "minio:9000")
        self.minio_access_key = kwargs.get("minio_access_key", "")
        self.minio_secret_key = kwargs.get("minio_secret_key", "")
        self.minio_secure = kwargs.get("minio_secure", False)


class TestEsClientInvalidation:
    def test_invalidate_closes_and_rebuilds_from_new_settings(self, monkeypatch):
        old = _FakeEsClient()
        built: list[tuple[list[str], dict]] = []

        def fake_es(hosts, **kwargs):
            built.append((hosts, kwargs))
            return _FakeEsClient()

        monkeypatch.setattr(es_client, "_es_client", old)
        monkeypatch.setattr(es_client, "_write_index_cache", "courtier_chunks_v1")
        monkeypatch.setattr(es_client, "_write_index_cache_ts", 123.0)
        monkeypatch.setattr(es_client, "Elasticsearch", fake_es)
        monkeypatch.setattr(
            es_client, "_get_settings", lambda: _FakeSettings(es_hosts="http://new-es:9200")
        )

        es_client.invalidate_es_client()

        assert old.closed is True
        assert es_client._write_index_cache is None
        assert es_client._write_index_cache_ts == 0.0

        rebuilt = es_client.get_es_client()
        # The rebuilt client is a _FakeEsClient because Elasticsearch is
        # monkeypatched — restore the real singleton so later tests in the
        # same process (e.g. test_es_backend_integration) don't build against
        # the patched constructor with empty hosts.
        monkeypatch.undo()
        es_client.invalidate_es_client()
        assert rebuilt is not old
        assert built == [((["http://new-es:9200"]), {"request_timeout": 30})]

    def test_invalidate_without_client_is_noop(self, monkeypatch):
        monkeypatch.setattr(es_client, "_es_client", None)
        es_client.invalidate_es_client()
        assert es_client._es_client is None


class TestMinioClientInvalidation:
    def test_invalidate_drops_singleton(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(storage_client, "_client", sentinel)

        storage_client.invalidate_minio_client()
        assert storage_client._client is None

    def test_rebuild_uses_current_settings(self, monkeypatch):
        built: list[str] = []

        def fake_minio(endpoint, **kwargs):
            built.append(endpoint)
            return object()

        monkeypatch.setattr(storage_client, "_client", None)
        monkeypatch.setattr(storage_client, "Minio", fake_minio)
        monkeypatch.setattr(
            storage_client, "_get_settings", lambda: _FakeSettings(minio_endpoint="new-minio:9000")
        )

        storage_client.get_minio_client()
        assert built == ["new-minio:9000"]


class TestRunManagerApplySettings:
    def _settings(self, **overrides):
        base = {
            "run_grace_seconds": 600,
            "run_log_max_events": 50_000,
            "run_log_max_bytes": 8 * 1024 * 1024,
            "max_runs_per_user": 3,
            "max_total_runs": 20,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    def _manager(self, settings):
        return RunManager(session_store=None, settings=settings)

    def test_limits_re_copied(self):
        manager = self._manager(self._settings())
        assert manager._max_per_user == 3

        manager.apply_settings(self._settings(max_runs_per_user=7, max_total_runs=9))
        assert manager._max_per_user == 7
        assert manager._max_total == 9
        assert manager._grace_seconds == 600

    def test_settings_reference_re_pointed(self):
        first = self._settings()
        manager = self._manager(first)
        assert manager._settings is first

        second = self._settings()
        manager.apply_settings(second)
        assert manager._settings is second
