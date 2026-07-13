"""AgentState — immutable state machine for the agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from .model import ModelResponse, ToolCall
from .execution_result import ExecutionResult

AgentStatus = Literal[
    "idle", "thinking", "waiting_for_tool", "observing",
    "completed", "blocked", "error",
]


def _json_default(obj: object) -> str:
    """JSON 序列化失败时的回退处理，截断过长内容。"""
    if isinstance(obj, bytes):
        return f"<bytes:{len(obj)}>"
    try:
        return repr(obj)[:500]
    except Exception:
        return "[non-serializable]"


@dataclass(frozen=True)
class Message:
    """A single conversation message."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    source: Literal["reminder", "inline", None] = None

    def to_openai_dict(self) -> dict[str, object]:
        """Convert to OpenAI-compatible dict."""
        d: dict[str, object] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        return d


class AgentState(BaseModel, frozen=True):
    """Immutable agent state — every step produces a new instance."""

    status: AgentStatus = "idle"
    messages: tuple[Message, ...] = ()
    current_step: int = 0
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ExecutionResult, ...] = ()
    max_steps: int = 20
    termination_reason: str | None = None

    @classmethod
    def initial(
        cls,
        task: str,
        system_prompt: str = "",
        max_steps: int = 20,
    ) -> "AgentState":
        """Create the initial state with system and user messages."""
        messages: list[Message] = []
        if system_prompt:
            messages.append(Message(role="system", content=system_prompt))
        messages.append(Message(role="user", content=task))
        return cls(
            status="idle",
            messages=tuple(messages),
            max_steps=max_steps,
        )

    def is_terminal(self) -> bool:
        """Check if the state represents a terminal condition."""
        return self.status in ("completed", "blocked", "error")

    def add_thought(self, response: ModelResponse) -> "AgentState":
        """Return new state with the model's response added to history."""
        if self.is_terminal():
            return self

        new_messages = list(self.messages)

        # If the model returned tool calls, add an assistant message with them.
        if response.tool_calls:
            next_step = self.current_step + 1
            tool_calls = tuple(response.tool_calls) if response.tool_calls else None
            new_messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=tool_calls,
                )
            )
            if next_step >= self.max_steps:
                return self.model_copy(
                    update={
                        "status": "completed",
                        "messages": tuple(new_messages),
                        "tool_calls": (),
                        "current_step": next_step,
                        "termination_reason": "max_steps",
                    }
                )
            return self.model_copy(
                update={
                    "status": "waiting_for_tool",
                    "messages": tuple(new_messages),
                    "tool_calls": tuple(response.tool_calls),
                    "current_step": next_step,
                }
            )

        # No tool calls — model gave a final text response or empty
        if response.content:
            new_messages.append(
                Message(role="assistant", content=response.content)
            )

        # Check max_steps
        if self.current_step + 1 >= self.max_steps:
            return self.model_copy(
                update={
                    "status": "completed",
                    "messages": tuple(new_messages),
                    "current_step": self.current_step + 1,
                    "termination_reason": "max_steps",
                }
            )

        return self.model_copy(
            update={
                "status": "completed",
                "messages": tuple(new_messages),
                "current_step": self.current_step + 1,
                "termination_reason": "completed",
            }
        )

    def add_observation(self, results: tuple[ExecutionResult, ...]) -> "AgentState":
        """Return new state with tool/sub-agent results added to history."""
        if self.is_terminal():
            return self

        if len(results) != len(self.tool_calls):
            raise ValueError(
                f"Result count mismatch: {len(results)} results for "
                f"{len(self.tool_calls)} tool calls"
            )

        new_messages = list(self.messages)
        inline_instructions: list[str] = []  # collect for batch injection after tool messages

        for tool_call, result in zip(self.tool_calls, results):
            # Check for inline skill instructions that should be surfaced as a
            # user message (task to execute) rather than embedded in the tool
            # result (which implies "work completed").
            inline_instruction: str | None = None
            if result.metadata:
                inline_instruction = result.metadata.get("inline_instruction")

            try:
                obs = result.to_observation_dict()
                # Strip inline_instruction from the tool message — it will be
                # injected as a separate role=user message below.
                if (
                    inline_instruction
                    and "metadata" in obs
                    and isinstance(obs["metadata"], dict)
                ):
                    obs["metadata"] = {
                        k: v
                        for k, v in obs["metadata"].items()
                        if k != "inline_instruction"
                    }
                result_data = json.dumps(
                    obs,
                    ensure_ascii=False,
                    default=_json_default,
                )
            except (TypeError, ValueError):
                result_data = json.dumps(
                    {
                        "success": result.success,
                        "actor_type": result.actor_type,
                        "actor_name": result.actor_name,
                        "error": result.error,
                    },
                    ensure_ascii=False,
                )

            new_messages.append(
                Message(
                    role="tool",
                    content=result_data,
                    tool_call_id=tool_call.id,
                    name=tool_call.name,
                )
            )

            if inline_instruction:
                inline_instructions.append(inline_instruction)

        # Inject all inline skill instructions AFTER all tool messages.
        # OpenAI requires assistant(tool_calls) → tool × N without
        # intervening user messages.  Inline instructions land as a batch
        # of user messages after the tool-result block so the model treats
        # them as a new task to execute, not as completed tool output.
        for instruction in inline_instructions:
            new_messages.append(
                Message(
                    role="user",
                    content=instruction,
                    source="inline",
                )
            )

        return self.model_copy(
            update={
                "status": "observing",
                "messages": tuple(new_messages),
                "tool_results": results,
                "tool_calls": (),
            }
        )

    def blocked(self, reason: str) -> "AgentState":
        """Return new state blocked by permission gate."""
        return self.model_copy(
            update={
                "status": "blocked",
                "termination_reason": reason,
            }
        )

    def errored(self, reason: str) -> "AgentState":
        """Return new state after an unrecoverable error."""
        return self.model_copy(
            update={
                "status": "error",
                "termination_reason": reason,
            }
        )

    def to_openai_messages(self) -> list[dict[str, object]]:
        """Convert messages to OpenAI-compatible format."""
        return [m.to_openai_dict() for m in self.messages]
