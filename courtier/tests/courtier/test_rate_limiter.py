"""Rate limiter: per-user bucket key and the split /sessions budgets."""

from types import SimpleNamespace

from starlette.requests import Request

from courtier.agent.api.rate_limiter import limiter, user_or_ip_key
from courtier.agent.api.routes import sessions as sessions_routes


def _request(query_string: str = "", auth_user: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/sessions",
        "query_string": query_string.encode(),
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345),
    }
    request = Request(scope)
    if auth_user is not None:
        request.state.auth_user = auth_user
    return request


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
