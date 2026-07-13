# src/agent/telemetry/decorators.py
"""Non-invasive tracing decorators for agent methods.

Use these when the call chain is simple (single function call).
For complex multi-phase flows (e.g. agent_loop), use AgentTracer
context managers directly.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .tracer import AgentTracer


def traced_agent(agent_name: str):
    """Decorate an agent's run() method with an invoke_agent span."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            tracer = getattr(self, "_tracer", AgentTracer())
            session_id = kwargs.pop("session_id", "")
            with tracer.agent_span(
                agent_name=agent_name,
                session_id=session_id,
            ) as span:
                if args:
                    span.set_attribute(
                        "agent.input.args",
                        str(args)[:500],
                    )
                task = kwargs.get("task")
                if task:
                    span.set_attribute("agent.task", str(task)[:500])
                result = await func(self, *args, **kwargs)
                span.set_attribute(
                    "agent.output_preview",
                    str(result)[:300],
                )
                return result

        return wrapper

    return decorator


def traced_llm(model: str = "unknown", system: str = "openai"):
    """Decorate an LLM call method with a chat span."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = AgentTracer()
            with tracer.llm_span(model=model, system=system) as span:
                prompt = kwargs.get("prompt") or kwargs.get("messages")
                if prompt is not None:
                    tracer.log_prompt(span, str(prompt))
                result = await func(*args, **kwargs)
                usage = getattr(result, "usage", None)
                if usage:
                    tracer.set_token_usage(
                        span,
                        getattr(usage, "prompt_tokens", 0),
                        getattr(usage, "completion_tokens", 0),
                    )
                return result

        return wrapper

    return decorator


def traced_tool(tool_name: str):
    """Decorate a tool execution method with an execute_tool span."""

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            tracer = AgentTracer()
            with tracer.tool_span(tool_name, parameters=kwargs) as span:
                result = await func(*args, **kwargs)
                tracer.set_tool_result(span, result)
                return result

        return wrapper

    return decorator
