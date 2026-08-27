"""ConfigService — the single owner of the effective Settings snapshot.

Phase 0 (env-only) semantics: the snapshot is built eagerly at import and
get_settings() always returns that same object.  replace()/subscribe() are
the hooks the DB-backed phase will drive; they are unit-tested here so the
contract is fixed before any caller exists.
"""

from courtier.config import ConfigService, Settings, get_settings


class TestEnvOnlySnapshot:
    def test_get_settings_returns_stable_identity(self):
        assert get_settings() is get_settings()

    def test_snapshot_is_a_settings_instance(self):
        # Import at call time: tests/courtier/test_config.py reloads the
        # config module, so a module-level class binding can go stale.
        import courtier.config as config

        assert isinstance(config.get_settings(), config.Settings)

    def test_service_builds_once(self):
        service = ConfigService()
        first = service.get()
        assert service.get() is first
        assert service.version == 1

    def test_version_zero_before_build(self):
        assert ConfigService().version == 0


class TestReplaceAndSubscribe:
    def _settings(self, **overrides) -> Settings:
        return Settings(**overrides)

    def test_replace_swaps_snapshot_and_bumps_version(self):
        service = ConfigService()
        service.get()
        new = self._settings(logger_level="DEBUG")
        version = service.replace(new)
        assert version == 2
        assert service.get() is new
        assert get_settings() is not new  # module service untouched

    def test_listeners_notified_in_order_with_new_snapshot(self):
        service = ConfigService()
        service.get()
        calls: list[tuple[str, int]] = []

        service.subscribe(lambda s, v: calls.append(("first", v)))
        service.subscribe(lambda s, v: calls.append(("second", v)))

        new = self._settings()
        version = service.replace(new)
        assert calls == [("first", version), ("second", version)]

    def test_failing_listener_does_not_block_others(self):
        service = ConfigService()
        service.get()
        seen: list[int] = []

        def boom(_s, _v):
            raise RuntimeError("listener down")

        service.subscribe(boom)
        service.subscribe(lambda _s, v: seen.append(v))

        service.replace(self._settings())
        assert seen == [2]

    def test_unsubscribe_stops_notifications(self):
        service = ConfigService()
        service.get()
        seen: list[int] = []
        unsubscribe = service.subscribe(lambda _s, v: seen.append(v))
        unsubscribe()
        service.replace(self._settings())
        assert seen == []

    def test_listener_can_unsubscribe_itself(self):
        """Unsubscribing from inside a listener must not corrupt the walk."""
        service = ConfigService()
        service.get()
        seen: list[int] = []

        def listener(_s, _v):
            seen.append(1)
            unsubscribe()

        unsubscribe = service.subscribe(listener)
        service.replace(self._settings())
        service.replace(self._settings())
        assert seen == [1]  # second replace: already unsubscribed
