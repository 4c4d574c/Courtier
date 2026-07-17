"""Test PluginSystem integration in app startup."""
from unittest.mock import AsyncMock, MagicMock, patch


def test_create_app_registers_plugin_system_on_state():
    """PluginSystem is stored on app.state.plugin_system after create_app()."""
    with patch("courtier.plugin.PluginSystem") as mock_ps_cls:
        mock_ps = MagicMock()
        mock_ps.start = AsyncMock()
        mock_ps.shutdown = AsyncMock()
        mock_ps_cls.return_value = mock_ps

        from courtier.agent.api.app import create_app

        app = create_app(sessions_dir="/tmp/test_sessions")

        assert hasattr(app.state, "tool_registry")
        assert hasattr(app.state, "plugin_system")
        assert app.state.plugin_system is mock_ps
        mock_ps_cls.assert_called_once()


def test_create_app_no_longer_registers_domain_tools():
    """DOMAIN_TOOLS import is not present in create_app()."""
    import inspect

    from courtier.agent.api.app import create_app

    source = inspect.getsource(create_app)
    assert "from ..tools.domain import DOMAIN_TOOLS" not in source
