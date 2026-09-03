"""AgentState — immutable state machine for the agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from .execution_result import ExecutionResult
from .content_parts import MessageContent, ensure_parts_allowed
from .model import ModelResponse, ToolCall

if TYPE_CHECKING:
    from .conversation_tree import ConversationNode, ConversationTree

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
    """A single conversation message.

    ``content`` is a plain string except for user messages carrying media
    attachments, which hold a ``TextPart``/``MediaPart`` list (see
    ``content_parts``).
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: MessageContent = None
    tool_calls: tuple[ToolCall, ...] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    source: Literal["reminder", "inline", "hint", None] = None

    def __post_init__(self) -> None:
        ensure_parts_allowed(self.role, self.content)

    def to_openai_dict(self) -> dict[str, object]:
        """Convert to OpenAI-compatible dict.

        Delegates to ``protocol.to_openai_dict`` (the single canonical wire
        conversion). The internal ``source`` field is not part of the wire
        format and is never emitted.
        """
        from .protocol import ChatMessage, to_openai_dict

        return to_openai_dict(
            ChatMessage(
                role=self.role,
                content=self.content,
                tool_calls=list(self.tool_calls) if self.tool_calls else None,
                tool_call_id=self.tool_call_id,
                name=self.name,
            )
        )


class AgentState(BaseModel, frozen=True):
    """Immutable agent state — every step produces a new instance."""

    status: AgentStatus = "idle"
    messages: tuple[Message, ...] = ()
    current_step: int = 0
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ExecutionResult, ...] = ()
    max_steps: int = 20
    termination_reason: str | None = None
    agent_name: str = ""
    tree: Any | None = None
    current_node_id: str | None = None
    transition_id: str | None = None

    @classmethod
    def initial(
        cls,
        task: str,
        system_prompt: str = "",
        max_steps: int = 20,
        *,
        use_tree: bool = False,
        agent_name: str = "",
    ) -> "AgentState":
        """Create the initial state with system and user messages."""
        messages: list[Message] = []
        if system_prompt:
            messages.append(Message(role="system", content=system_prompt))
        messages.append(Message(role="user", content=task))
        state = cls(
            status="idle",
            messages=tuple(messages),
            max_steps=max_steps,
            agent_name=agent_name,
        )
        if use_tree:
            from .conversation_tree import ConversationTree

            tree = ConversationTree.from_messages(state.messages)
            state = state.model_copy(
                update={
                    "tree": tree,
                    "current_node_id": tree.root_id,
                }
            )
        return state

    def is_terminal(self) -> bool:
        """Check if the state represents a terminal condition."""
        return self.status in ("completed", "blocked", "error")

    def add_thought(
        self,
        response: ModelResponse,
        *,
        set_status: bool = True,
    ) -> "AgentState":
        """Return new state with the model's response added to history.

        Args:
            response: The model response to record.
            set_status: When False, status/termination_reason are left unchanged
                so the caller can drive transitions through ``AgentStateMachine``.
                Max-steps termination is still enforced because it is a budget
                guard, not a state-machine transition.
        """
        if self.is_terminal():
            return self

        new_messages = list(self.messages)

        # If the model returned tool calls, add an assistant message with them.
        if response.tool_calls:
            next_step = self.current_step + 1
            hit_max_steps = next_step >= self.max_steps
            # When the max-steps budget cuts the turn short, the pending tool
            # calls are dropped without ever producing results. Strip them
            # from the stored assistant message so the persisted history stays
            # a valid OpenAI message sequence (no dangling tool_calls) when
            # the session is resumed.
            msg_tool_calls = None if hit_max_steps else tuple(response.tool_calls)
            new_messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=msg_tool_calls,
                )
            )
            update: dict[str, Any] = {
                "messages": tuple(new_messages),
                "tool_calls": tuple(response.tool_calls),
                "current_step": next_step,
            }
            if hit_max_steps:
                update["status"] = "completed"
                update["tool_calls"] = ()
                update["termination_reason"] = "max_steps"
            elif set_status:
                update["status"] = "waiting_for_tool"
            return self.model_copy(update=update)

        # No tool calls — model gave a final text response or empty
        if response.content:
            new_messages.append(
                Message(role="assistant", content=response.content)
            )

        # Check max_steps
        next_step = self.current_step + 1
        update = {
            "messages": tuple(new_messages),
            "current_step": next_step,
        }
        if next_step >= self.max_steps:
            update["status"] = "completed"
            update["termination_reason"] = "max_steps"
        elif set_status:
            update["status"] = "completed"
            update["termination_reason"] = "completed"
        return self.model_copy(update=update)

    def add_observation(
        self,
        results: tuple[ExecutionResult, ...],
        *,
        set_status: bool = True,
    ) -> "AgentState":
        """Return new state with tool/sub-agent results added to history.

        Args:
            results: Tool execution results, one per pending tool call.
            set_status: When False, status is left unchanged so the caller can
                drive the transition through ``AgentStateMachine``.
        """
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

        update: dict[str, Any] = {
            "messages": tuple(new_messages),
            "tool_results": results,
            "tool_calls": (),
        }
        if set_status:
            update["status"] = "observing"
        return self.model_copy(update=update)

    def blocked(
        self,
        reason: str,
        *,
        set_status: bool = True,
    ) -> "AgentState":
        """Return new state blocked by permission gate."""
        update: dict[str, Any] = {"termination_reason": reason}
        if set_status:
            update["status"] = "blocked"
        return self.model_copy(update=update)

    def errored(
        self,
        reason: str,
        *,
        set_status: bool = True,
    ) -> "AgentState":
        """Return new state after an unrecoverable error."""
        update: dict[str, Any] = {"termination_reason": reason}
        if set_status:
            update["status"] = "error"
        return self.model_copy(update=update)

    def to_openai_messages(self) -> list[dict[str, object]]:
        """Convert messages to OpenAI-compatible format.

        Drops ``tool_calls`` from assistant messages whose calls have no
        matching tool-result message (turns cut short by max_steps or a
        guardrail/permission block), so the serialized history always
        satisfies the OpenAI message-sequence contract when a session is
        resumed.
        """
        answered_ids = {
            m.tool_call_id
            for m in self.messages
            if m.role == "tool" and m.tool_call_id is not None
        }
        result: list[dict[str, object]] = []
        for m in self.messages:
            if m.role == "assistant" and m.tool_calls:
                if not all(tc.id in answered_ids for tc in m.tool_calls):
                    m = Message(role=m.role, content=m.content, name=m.name)
            result.append(m.to_openai_dict())
        return result

    def record_turn(
        self,
        messages: tuple[Message, ...] | None = None,
        tool_results: tuple[ExecutionResult, ...] | None = None,
    ) -> "AgentState":
        """Append the current turn to the conversation tree if one exists.

        This is a no-op when ``tree`` is None, preserving backward compatibility.
        """
        if self.tree is None or self.current_node_id is None:
            return self

        tree: ConversationTree = self.tree
        node = tree.append_turn(
            parent_node_id=self.current_node_id,
            messages=messages if messages is not None else self.messages,
            tool_results=tool_results if tool_results is not None else self.tool_results,
            metadata={
                "turn_index": self.current_step,
                "status": self.status,
            },
        )
        return self.model_copy(update={"current_node_id": node.node_id})

    def fork_tree(self, reason: str = "") -> "AgentState":
        """Fork the current tree node and position the state at the child."""
        if self.tree is None or self.current_node_id is None:
            return self

        tree: ConversationTree = self.tree
        child = tree.fork(self.current_node_id, reason=reason)
        return self.model_copy(update={"current_node_id": child.node_id})

    def rewind_tree(self, node_id: str) -> "AgentState":
        """Position the state at an existing tree node (for replay / branching)."""
        if self.tree is None:
            return self

        tree: ConversationTree = self.tree
        node: ConversationNode | None = tree.rewind(node_id)
        if node is None:
            return self
        return self.model_copy(
            update={
                "current_node_id": node.node_id,
                "messages": node.messages,
                "tool_results": node.tool_results,
            }
        )
