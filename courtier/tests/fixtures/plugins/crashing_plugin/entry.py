"""A plugin that registers successfully, then crashes immediately.

This triggers the immediate-crash circuit breaker in ProcessManager._on_crash,
which marks the plugin FATAL without restart retries (crashed within 5s of
reaching ACTIVE).
"""
from courtier.plugin.sdk import PluginRuntime


class CrashingPlugin(PluginRuntime):
    def register_capabilities(self):
        return []

    def _setup_handlers(self):
        pass


if __name__ == "__main__":
    import asyncio

    async def main():
        runtime = CrashingPlugin()
        # Start the runtime in background so it sends the register notification
        runtime_task = asyncio.create_task(runtime.run())
        # Give the runtime a moment to register
        await asyncio.sleep(0.2)
        # Now exit — the host will see a disconnect immediately after ACTIVE
        import sys

        sys.exit(1)

    asyncio.run(main())
