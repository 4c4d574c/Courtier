"""EchoAgent — minimal agent for closed-loop verification."""

from __future__ import annotations

from typing import Any

from courtier.common.behavioral_rules import PERIODIC_REMINDER, PRE_TURN_REMINDER

from ..core.model import ModelClient, ModelResponse, ToolCall
from ..tools.builtin.echo import EchoTool
from .base import Agent


def create_echo_agent(model: ModelClient | None = None) -> Agent:
    """Create an EchoAgent for testing.

    If no model is provided, a MockModelClient is used that automatically
    calls the echo tool with the user's message text.
    """
    if model is None:
        # Mock model that extracts text from user message and calls echo
        model = _DefaultEchoModel()

    return Agent(
        name="EchoAgent",
        role=(
            "You are an echo agent. When the user asks you to echo something, "
            "use the echo tool to repeat their text back. After getting the "
            "echo result, respond with 'Echoed: <text>'."
        ),
        tools=[EchoTool()],
        model=model,
    )


class _DefaultEchoModel(ModelClient):
    """A deterministic model for EchoAgent testing.

    This model is intentionally limited in scope: it performs exactly one
    echo tool call followed by a text response. It is NOT a general-purpose
    model and should only be used in echo_agent tests and demos.

    Extracts text from the last user message and creates an echo tool call
    on the first invocation, then returns a final text response.
    """

    def __init__(self) -> None:
        super().__init__()
        self._call_count = 0

    @property
    def model_name(self) -> str:
        return "echo-mock"

    @property
    def temperature(self) -> float:
        return 0.0

    async def close(self) -> None:
        """No-op: echo model has no resources to release."""
        return

    async def generate(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> ModelResponse:
        self._call_count += 1

        if self._call_count == 1:
            # First call: return echo tool call
            last_user_content = ""
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if content not in (PRE_TURN_REMINDER, PERIODIC_REMINDER):
                        last_user_content = content
                        break

            # Verify the echo tool is registered before generating a call for it.
            # Tool schemas use OpenAI format: {"type": "function", "function": {"name": ...}}
            tool_names = set()
            for t in tools or []:
                if isinstance(t, dict):
                    func = t.get("function", {})
                    if isinstance(func, dict):
                        tool_names.add(func.get("name", ""))
            if "echo" not in tool_names:
                # Echo tool not present — fall back to plain text response
                return ModelResponse(
                    content=f"Echoed: {last_user_content}", tool_calls=[]
                )

            return ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="echo_1",
                        name="echo",
                        arguments={"text": last_user_content},
                    )
                ],
            )

        # Subsequent calls: return final text response
        return ModelResponse(content="Echo completed.", tool_calls=[])

    async def generate_stream_full(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_token: Any = None,
        on_content_token: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Streaming variant — delegates to generate().

        Note: Emits the complete response text in a single ``on_content_token``
        call rather than token-by-token. Tests relying on incremental streaming
        behavior should use a real model client instead.
        """
        response = await self.generate(messages, tools=tools, **kwargs)
        text = response.content or ""
        if on_content_token and text:
            await on_content_token(text)
        return response
