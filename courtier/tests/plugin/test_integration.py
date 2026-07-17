"""Full integration tests: real subprocess lifecycle."""

from pathlib import Path

import pytest

from courtier.plugin import PluginSystem

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "plugins"


@pytest.mark.integration
class TestPluginSystemIntegration:
    """Tests using real subprocesses with the echo_plugin fixture."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Start echo_plugin, verify status, shut down."""
        ps = PluginSystem(plugins_dir=FIXTURES_DIR)

        status = await ps.start()
        assert "echo_plugin" in status
        assert status["echo_plugin"] == "ACTIVE"

        # get_status should work
        info = ps.get_status()
        assert info["echo_plugin"]["state"] == "ACTIVE"
        assert info["echo_plugin"]["version"] == "0.1.0"

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
