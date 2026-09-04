"""Rate limiter: per-user bucket key and the split /sessions budgets."""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from courtier.agent.api.rate_limiter import limiter, user_or_ip_key
from courtier.agent.api.routes import sessions as sessions_routes


def _request(
    query_string: str = "",
    auth_user: str | None = None,
    client: tuple[str, int] = ("127.0.0.1", 12345),
    xff: str | None = None,
) -> Request:
    headers = [(b"host", b"testserver")]
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions",
        "query_string": query_string.encode(),
        "headers": headers,
        "client": client,
    }
    request = Request(scope)
    if auth_user is not None:
        request.state.auth_user = auth_user
    return request


class TestClientIpTrustedProxy:
    @pytest.fixture(autouse=True)
    def _configure(self, monkeypatch):

        self.settings = SimpleNamespace(
            trusted_proxies="10.0.0.0/8, 172.16.5.9"
        )
        monkeypatch.setattr(
            "courtier.config.get_settings", lambda: self.settings
        )

    def test_untrusted_peer_ignores_xff(self):
        # A spoofed header from a non-proxy peer must not mint buckets.
        req = _request(client=("8.8.8.8", 9999), xff="1.2.3.4")
        assert user_or_ip_key(req) == "8.8.8.8"

    def test_trusted_proxy_uses_rightmost_xff_entry(self):
        req = _request(client=("10.1.2.3", 9999), xff="spoofed, 203.0.113.7")
        assert user_or_ip_key(req) == "203.0.113.7"

    def test_trusted_proxy_without_xff_falls_back_to_peer(self):
        req = _request(client=("10.1.2.3", 9999))
        assert user_or_ip_key(req) == "10.1.2.3"

    def test_cidr_member_is_trusted(self):
        req = _request(client=("10.255.0.1", 9999), xff="198.51.100.9")
        assert user_or_ip_key(req) == "198.51.100.9"

    def test_empty_setting_disables_xff_trust(self, monkeypatch):
        self.settings.trusted_proxies = ""
        req = _request(client=("10.1.2.3", 9999), xff="203.0.113.7")
        assert user_or_ip_key(req) == "10.1.2.3"

    def test_invalid_entries_ignored(self, monkeypatch):
        from types import SimpleNamespace as _NS

        monkeypatch.setattr(
            "courtier.config.get_settings",
            lambda: _NS(trusted_proxies="not-an-ip, 10.0.0.0/8"),
        )
        req = _request(client=("10.9.9.9", 9999), xff="198.51.100.1")
        assert user_or_ip_key(req) == "198.51.100.1"


class TestUserOrIpKey:
    def test_authenticated_user_gets_user_bucket(self):
        assert user_or_ip_key(_request(auth_user="alice")) == "user:alice"

    def test_users_behind_one_ip_get_separate_buckets(self):
        alice = user_or_ip_key(_request(auth_user="alice"))
        bob = user_or_ip_key(_request(auth_user="bob"))
        assert alice != bob

    def test_unauthenticated_falls_back_to_ip(self):
        key = user_or_ip_key(_request())
        assert key == "127.0.0.1"

    def test_empty_sub_falls_back_to_ip(self):
        assert user_or_ip_key(_request(auth_user="")) == "127.0.0.1"


def _registered_limit_values(func_name: str) -> list[str]:
    """Raw limit strings slowapi registered for a route handler."""
    registered = limiter._route_limits.get(
        f"{sessions_routes.__name__}.{func_name}", []
    )
    return [str(item.limit) for item in registered]


class TestSplitBudgets:
    def test_list_route_has_list_budget(self):
        assert any(
            limit.startswith("60 ") for limit in _registered_limit_values("handle_sessions")
        )

    def test_stream_route_has_stream_budget(self):
        # Run start also absorbs native EventSource auto-reconnect replays.
        assert any(
            limit.startswith("30 ")
            for limit in _registered_limit_values("handle_session_stream")
        )
