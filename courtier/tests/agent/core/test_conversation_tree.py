"""Tests for ConversationTree."""

import pytest

from courtier.agent.core.conversation_tree import ConversationTree
from courtier.agent.core.model import ModelResponse
from courtier.agent.core.state import AgentState, Message


class TestConversationTree:
    def test_from_messages_creates_root(self):
        msgs = (
            Message(role="system", content="You are an agent."),
            Message(role="user", content="Hello."),
        )
        tree = ConversationTree.from_messages(msgs)
        assert tree.root_id is not None
        root = tree.get(tree.root_id)
        assert root is not None
        assert root.messages == msgs
        assert root.parent_id is None

    def test_get_path_root(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        path = tree.get_path(tree.root_id)
        assert len(path) == 1

    def test_fork_creates_child_with_same_messages(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        child = tree.fork(tree.root_id, reason="try again")
        assert child.parent_id == tree.root_id
        assert child.messages == tree.get(tree.root_id).messages
        assert tree.get(tree.root_id).children == [child.node_id]

    def test_fork_unknown_node_raises(self):
        tree = ConversationTree()
        with pytest.raises(ValueError, match="Node not found"):
            tree.fork("missing")

    def test_append_turn_adds_child(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        turn_msg = (
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello"),
        )
        node = tree.append_turn(tree.root_id, turn_msg)
        assert node.parent_id == tree.root_id
        assert node.turn_index == 1
        assert node.messages == turn_msg
        assert tree.get_path(node.node_id)[-1] == node

    def test_rewind_returns_node(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        tree.append_turn(
            tree.root_id,
            (Message(role="user", content="hi"), Message(role="assistant", content="a")),
        )
        rewinded = tree.rewind(tree.root_id)
        assert rewinded.node_id == tree.root_id

    def test_get_messages_returns_target_node_messages(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        node = tree.append_turn(
            tree.root_id,
            (
                Message(role="user", content="hi"),
                Message(role="assistant", content="hello"),
            ),
        )
        msgs = tree.get_messages(node.node_id)
        assert len(msgs) == 2
        assert msgs[1].content == "hello"

    def test_leaf_nodes(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        child = tree.fork(tree.root_id)
        assert [n.node_id for n in tree.leaf_nodes()] == [child.node_id]

    def test_serialize_roundtrip_structure(self):
        tree = ConversationTree.from_messages((Message(role="user", content="hi"),))
        serialized = tree.serialize()
        assert serialized["root_id"] == tree.root_id
        assert tree.root_id in serialized["nodes"]

    def test_from_serialized_roundtrip(self):
        original = ConversationTree.from_messages((Message(role="user", content="hi"),))
        child = original.fork(original.root_id, reason="explore")
        restored = ConversationTree.from_serialized(original.serialize())

        assert restored.root_id == original.root_id
        assert set(restored.nodes.keys()) == set(original.nodes.keys())
        restored_child = restored.get(child.node_id)
        assert restored_child is not None
        assert restored_child.parent_id == original.root_id
        assert restored_child.metadata.get("fork_reason") == "explore"
        assert restored_child.messages[0].content == "hi"

    def test_from_serialized_with_tool_calls(self):
        from courtier.agent.core.model import ToolCall

        msgs = (
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(id="t1", name="echo", arguments={"text": "hi"}),
                ),
            ),
        )
        tree = ConversationTree.from_messages(msgs)
        restored = ConversationTree.from_serialized(tree.serialize())

        restored_msgs = restored.get_messages()
        assert len(restored_msgs) == 1
        assert restored_msgs[0].tool_calls is not None
        assert restored_msgs[0].tool_calls[0].name == "echo"
        assert restored_msgs[0].tool_calls[0].arguments == {"text": "hi"}


class TestAgentStateTreeIntegration:
    def test_initial_with_tree(self):
        state = AgentState.initial("task", system_prompt="sys", use_tree=True)
        assert state.tree is not None
        assert state.current_node_id is not None

    def test_record_turn_appends_node(self):
        state = AgentState.initial("task", system_prompt="sys", use_tree=True)
        state = state.add_thought(ModelResponse(content="ok"))
        recorded = state.record_turn()
        assert recorded.current_node_id != state.current_node_id
        tree = recorded.tree
        node = tree.get(recorded.current_node_id)
        assert node.turn_index == 1

    def test_fork_tree(self):
        state = AgentState.initial("task", use_tree=True)
        state = state.fork_tree(reason="explore")
        tree = state.tree
        node = tree.get(state.current_node_id)
        assert node.parent_id == tree.root_id
        assert node.metadata.get("fork_reason") == "explore"

    def test_rewind_tree_restores_messages(self):
        state = AgentState.initial("task", system_prompt="sys", use_tree=True)
        original_node_id = state.current_node_id
        state = state.add_thought(ModelResponse(content="ok"))
        state = state.record_turn()
        rewinded = state.rewind_tree(original_node_id)
        assert rewinded.current_node_id == original_node_id
        assert len(rewinded.messages) == 2  # system + user

    def test_without_tree_is_noop(self):
        state = AgentState.initial("task")
        assert state.tree is None
        assert state.record_turn() == state
        assert state.fork_tree() == state
