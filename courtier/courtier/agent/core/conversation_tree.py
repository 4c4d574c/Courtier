"""ConversationTree — tree-structured conversation history.

Supports branching (fork), rewinding, and snapshots so that agent runs can
be replayed from any turn or explored in parallel without mutating the
original history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .protocol import from_openai_dict
from .state import Message


def _message_from_openai_dict(d: dict[str, Any]) -> Message:
    """Reconstruct a ``Message`` from an OpenAI-compatible dict.

    Delegates wire parsing to ``protocol.from_openai_dict`` (single canonical
    conversion). Note the semantic change: ``arguments`` were previously
    parsed with a strict ``json.loads`` that raised on malformed payloads;
    the unified parser is tolerant — an unparseable string yields a
    ``{"_parse_error": True, "raw": ...}`` sentinel instead of raising
    (same philosophy as ``model._parse_tool_arguments``).

    ``source`` is snapshot metadata, not part of the wire format, so it is
    read here rather than by ``from_openai_dict``.
    """
    chat = from_openai_dict(d)
    return Message(
        role=chat.role,
        content=chat.content,
        tool_calls=tuple(chat.tool_calls) if chat.tool_calls else None,
        tool_call_id=chat.tool_call_id,
        name=chat.name,
        source=d.get("source"),
    )


@dataclass
class ConversationNode:
    """A single node in the conversation tree.

    Each node corresponds roughly to one turn: it stores the messages and
    tool results produced up to that point, plus metadata for replay.
    """

    node_id: str = field(default_factory=lambda: uuid4().hex)
    parent_id: str | None = None
    turn_index: int = 0
    messages: tuple[Message, ...] = field(default_factory=tuple)
    tool_results: tuple[Any, ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)

    def add_child(self, node_id: str) -> None:
        """Record a child node id if not already present."""
        if node_id not in self.children:
            self.children.append(node_id)


@dataclass
class ConversationTree:
    """Tree-structured conversation history.

    The tree is intentionally kept as a plain in-memory dataclass so it can
    be serialized to JSON and stored in ``AgentState`` or a database without
    ORM coupling.
    """

    nodes: dict[str, ConversationNode] = field(default_factory=dict)
    root_id: str | None = None

    @classmethod
    def from_messages(
        cls,
        messages: tuple[Message, ...],
        metadata: dict[str, Any] | None = None,
    ) -> "ConversationTree":
        """Create a tree with a single root node containing *messages*."""
        root = ConversationNode(
            node_id=uuid4().hex,
            parent_id=None,
            turn_index=0,
            messages=messages,
            metadata=metadata or {},
        )
        return cls(nodes={root.node_id: root}, root_id=root.node_id)

    def get(self, node_id: str) -> ConversationNode | None:
        """Look up a node by id."""
        return self.nodes.get(node_id)

    def get_path(self, node_id: str) -> list[ConversationNode]:
        """Return the path from root to *node_id* (inclusive).

        The path is built by walking parent pointers, so it reflects the
        branch that produced the target node.
        """
        path: list[ConversationNode] = []
        current = self.get(node_id)
        visited: set[str] = set()
        while current is not None and current.node_id not in visited:
            path.append(current)
            visited.add(current.node_id)
            current = self.get(current.parent_id) if current.parent_id else None
        path.reverse()
        return path

    def get_messages(self, node_id: str | None = None) -> tuple[Message, ...]:
        """Return the merged message history along the path to *node_id*."""
        target_id = node_id or self.root_id
        if target_id is None:
            return ()
        path = self.get_path(target_id)
        if not path:
            return ()
        # The target node already contains the full accumulated messages for
        # its branch; earlier nodes in the path are ancestors with shorter
        # histories. Return the target node's messages directly.
        return path[-1].messages

    def fork(
        self,
        node_id: str,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ConversationNode:
        """Create a child branch starting from *node_id*.

        The child node inherits the parent's messages and tool results so it
        can be used as a drop-in replacement continuation point.
        """
        parent = self.get(node_id)
        if parent is None:
            raise ValueError(f"Node not found: {node_id!r}")

        child = ConversationNode(
            node_id=uuid4().hex,
            parent_id=parent.node_id,
            turn_index=parent.turn_index,
            messages=parent.messages,
            tool_results=parent.tool_results,
            metadata={**(metadata or {}), "fork_reason": reason},
        )
        parent.add_child(child.node_id)
        self.nodes[child.node_id] = child
        return child

    def rewind(self, node_id: str) -> ConversationNode:
        """Return the node at *node_id* as the new active continuation point.

        This does not delete downstream nodes; it simply returns the node so
        a caller can continue from it. Any new fork/append from this node
        will create a sibling branch rather than mutating the existing path.
        """
        node = self.get(node_id)
        if node is None:
            raise ValueError(f"Node not found: {node_id!r}")
        return node

    def append_turn(
        self,
        parent_node_id: str,
        messages: tuple[Message, ...],
        tool_results: tuple[Any, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> ConversationNode:
        """Append a new turn node as a child of *parent_node_id*.

        This is the primary mutation path during normal agent execution.
        """
        parent = self.get(parent_node_id)
        if parent is None:
            raise ValueError(f"Parent node not found: {parent_node_id!r}")

        node = ConversationNode(
            node_id=uuid4().hex,
            parent_id=parent.node_id,
            turn_index=parent.turn_index + 1,
            messages=messages,
            tool_results=tool_results,
            metadata=metadata or {},
        )
        parent.add_child(node.node_id)
        self.nodes[node.node_id] = node
        return node

    def leaf_nodes(self) -> list[ConversationNode]:
        """Return all nodes that have no children."""
        return [n for n in self.nodes.values() if not n.children]

    def serialize(self) -> dict[str, Any]:
        """Serialize the tree to a plain dict."""
        return {
            "root_id": self.root_id,
            "nodes": {
                node_id: {
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "turn_index": node.turn_index,
                    "messages": [m.to_openai_dict() for m in node.messages],
                    "tool_results": [str(r) for r in node.tool_results],
                    "metadata": node.metadata,
                    "children": list(node.children),
                }
                for node_id, node in self.nodes.items()
            },
        }

    @classmethod
    def from_serialized(cls, data: dict[str, Any]) -> "ConversationTree":
        """Reconstruct a tree from a serialized dict."""
        nodes: dict[str, ConversationNode] = {}
        for node_id, node_data in data.get("nodes", {}).items():
            nodes[node_id] = ConversationNode(
                node_id=node_data["node_id"],
                parent_id=node_data.get("parent_id"),
                turn_index=node_data.get("turn_index", 0),
                messages=tuple(
                    _message_from_openai_dict(m)
                    for m in node_data.get("messages", [])
                ),
                tool_results=tuple(node_data.get("tool_results", [])),
                metadata=node_data.get("metadata", {}),
                children=list(node_data.get("children", [])),
            )
        return cls(nodes=nodes, root_id=data.get("root_id"))
