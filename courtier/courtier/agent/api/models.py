"""API data models — frozen dataclasses for session records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict, cast

# "queued": 每用户并发满，FIFO 等待中（task-queue 计划 Task 4.1）。
# "interrupted": 服务重启清扫 running/queued 会话的终态标记（run 不跨进程存活）。
SessionStatus = Literal[
    "initial", "running", "paused", "completed", "error", "stopped", "queued", "interrupted"
]
ToolStatus = Literal["pending", "running", "done", "ok", "error", "warning"]
ToolCallKind = Literal["tool", "subagent_run"]
ToolCallScope = Literal["parent", "subagent"]

_VALID_CALL_KINDS: set[str] = {"tool", "subagent_run"}
_VALID_CALL_SCOPES: set[str] = {"parent", "subagent"}


class ToolCallClassification(TypedDict):
    call_kind: ToolCallKind
    call_scope: ToolCallScope
    subagent_name: str | None


def normalize_tool_call_classification(
    metadata: Mapping[str, Any] | None,
) -> ToolCallClassification:
    """Normalize internal tool-call classification metadata for API output."""
    metadata = metadata or {}
    raw_kind = metadata.get("call_kind")
    raw_scope = metadata.get("call_scope")
    raw_subagent = metadata.get("subagent_name")

    call_kind: ToolCallKind = (
        cast(ToolCallKind, raw_kind)
        if isinstance(raw_kind, str) and raw_kind in _VALID_CALL_KINDS
        else "tool"
    )
    call_scope: ToolCallScope = (
        cast(ToolCallScope, raw_scope)
        if isinstance(raw_scope, str) and raw_scope in _VALID_CALL_SCOPES
        else "parent"
    )
    subagent_name = raw_subagent if isinstance(raw_subagent, str) else None

    return {
        "call_kind": call_kind,
        "call_scope": call_scope,
        "subagent_name": subagent_name,
    }


_NUMERALS: dict[int, str] = {
    1: "壹",
    2: "贰",
    3: "叁",
    4: "肆",
    5: "伍",
    6: "陆",
    7: "柒",
    8: "捌",
    9: "玖",
    10: "拾",
}


def _numeral(n: int) -> str:
    return _NUMERALS.get(n, str(n))


@dataclass(frozen=True)
class ThoughtRecord:
    id: int
    text: str
    turn: int
    timestamp: float
    segment_index: int = 0
    segment_type: str = "observe"
    step_index: int | None = None
    turn_index: int = 0


@dataclass(frozen=True)
class SubagentThoughtRecord:
    """A single thought block inside a sub-agent's reasoning stream."""

    id: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubagentThoughtRecord:
        return cls(id=d["id"], text=d["text"])


@dataclass(frozen=True)
class SubagentToolRecord:
    """A tool execution result inside a sub-agent."""

    name: str
    skill: str = ""
    status: ToolStatus = "done"
    duration: float = 0.0
    summary: str = ""
    call_kind: str = "tool"
    handle_id: str | None = None
    issue_counts: dict[str, int] | None = None
    #: Chinese display name (registry display_name); None falls back to name.
    display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "skill": self.skill,
            "status": self.status,
            "duration": self.duration,
            "summary": self.summary,
            "callKind": self.call_kind,
        }
        if self.handle_id is not None:
            result["handleId"] = self.handle_id
        if self.issue_counts is not None:
            result["issueCounts"] = self.issue_counts
        if self.display_name is not None:
            result["displayName"] = self.display_name
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubagentToolRecord:
        return cls(
            name=d["name"],
            skill=d.get("skill", ""),
            status=d.get("status", "done"),
            duration=d.get("duration", 0.0),
            summary=d.get("summary", ""),
            call_kind=d.get("callKind", d.get("call_kind", "tool")),
            handle_id=d.get("handleId", d.get("handle_id")),
            issue_counts=d.get("issueCounts", d.get("issue_counts")),
            display_name=d.get("displayName", d.get("display_name")),
        )


@dataclass(frozen=True)
class ToolInfo:
    name: str
    skill: str
    status: ToolStatus
    duration: float
    summary: str
    id: str = ""  # client-side tool identifier, e.g. "tool-1"
    detail: Any = None  # ToolDetail: structured data or markdown content
    call_kind: ToolCallKind = "tool"
    call_scope: ToolCallScope = "parent"
    subagent_name: str | None = None
    display_name: str | None = None
    parent_subagent_name: str | None = None
    handle_id: str | None = None
    parent_handle_id: str | None = None
    skill_description: str = ""
    issue_counts: dict[str, int] | None = None
    #: Citation payload for search tools (search_documents hits).  Referenced
    #: from assistant conclusions via ``[[n]]`` markers (1-based hit index).
    citations: list[dict[str, Any]] | None = None
    #: Model-assigned call id — pairs results to their cards when several
    #: same-name tools run in one turn.
    tool_call_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "skill": self.skill,
            "status": self.status,
            "duration": self.duration,
            "summary": self.summary,
            "callKind": self.call_kind,
            "callScope": self.call_scope,
            "subagentName": self.subagent_name,
            "displayName": self.display_name,
            "parentSubagentName": self.parent_subagent_name,
            "handleId": self.handle_id,
            "parentHandleId": self.parent_handle_id,
            "skillDescription": self.skill_description,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        if self.issue_counts is not None:
            result["issueCounts"] = self.issue_counts
        if self.citations is not None:
            result["citations"] = self.citations
        if self.tool_call_id is not None:
            result["toolCallId"] = self.tool_call_id
        return result


@dataclass(frozen=True)
class SubagentRunRecord:
    """Persistent record of a sub-agent execution (mirrors frontend SubagentRun)."""

    name: str
    handle_id: str
    parent_handle_id: str | None = None
    task: str = ""
    status: str = "running"  # running | completed | error
    conclusion: str = ""
    error: str = ""
    thoughts: list[SubagentThoughtRecord] = field(default_factory=list)
    children: list[SubagentRunRecord] = field(default_factory=list)
    tools: list[SubagentToolRecord] = field(default_factory=list)
    #: Chinese display name from the skill frontmatter; None falls back to name.
    display_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "handleId": self.handle_id,
            "parentHandleId": self.parent_handle_id,
            "task": self.task,
            "status": self.status,
            "conclusion": self.conclusion,
            "error": self.error,
            "thoughts": [th.to_dict() for th in self.thoughts],
            "children": [ch.to_dict() for ch in self.children],
            "tools": [t.to_dict() for t in self.tools],
        }
        if self.display_name is not None:
            result["displayName"] = self.display_name
        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SubagentRunRecord:
        return cls(
            name=d["name"],
            handle_id=d.get("handleId", d.get("handle_id", "")),
            parent_handle_id=d.get("parentHandleId", d.get("parent_handle_id")),
            task=d.get("task", ""),
            status=d.get("status", "completed"),
            conclusion=d.get("conclusion", ""),
            error=d.get("error", ""),
            thoughts=[SubagentThoughtRecord.from_dict(th) for th in d.get("thoughts", [])],
            children=[SubagentRunRecord.from_dict(ch) for ch in d.get("children", [])],
            tools=[SubagentToolRecord.from_dict(t) for t in d.get("tools", [])],
            display_name=d.get("displayName", d.get("display_name")),
        )


@dataclass(frozen=True)
class StepRecord:
    index: int
    label: str
    skill: str
    tools: list[ToolInfo] = field(default_factory=list)
    verdict: str = ""
    subagents: list[SubagentRunRecord] = field(default_factory=list)
    turn_index: int = 0
    start_segment_index: int = 0
    end_segment_index: int | None = None

    @property
    def numeral(self) -> str:
        return _numeral(self.index)


def context_state_compacted(context_state: str) -> bool:
    """True when a persisted CompactState payload records a past compaction."""
    if not context_state:
        return False
    try:
        state = json.loads(context_state)
    except json.JSONDecodeError:
        return False
    return bool(state.get("has_compacted") or state.get("compact_count"))


@dataclass(frozen=True)
class SessionRecord:
    """Frozen session record (updated via replace())."""

    id: str
    task: str
    file_id: str
    file_name: str = ""
    model_name: str = ""
    # 最近一次运行所选的模型池条目 id（每次运行可换；空 = 标量模型）。
    # 前端据此预选模型选择器；逐轮模型记录在 turn_messages 条目里。
    last_model_id: str = ""
    status: SessionStatus = "running"
    steps: list[StepRecord] = field(default_factory=list)
    thoughts: list[ThoughtRecord] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: float = 0.0
    finished_at: float | None = None
    error_detail: str | None = None
    conclusion: str = ""
    messages_json: str = ""  # serialised AgentState.messages for multi-turn continuation
    artifact_snapshot: str = ""  # serialised ArtifactStore snapshot for full multi-turn restore
    context_state: str = ""  # serialised CompactState for cross-request compaction continuity
    tree_json: str = ""  # serialised ConversationTree for branching/replay
    current_node_id: str | None = None  # active node within the conversation tree
    owner: str = ""
    # Turn tracking — one entry per user turn for historical rendering.
    # turn_messages: [{"text": str, "timestamp": float}, ...]
    # turn_step_starts: [int, ...] — step index where each turn starts
    # turn_conclusions: [str, ...] — conclusion for each completed turn
    turn_messages: list[dict[str, Any]] = field(default_factory=list)
    turn_step_starts: list[int] = field(default_factory=list)
    turn_conclusions: list[str] = field(default_factory=list)
    # Per-turn artifact snapshots: entry i is the store snapshot captured when
    # turn i+1 begins (i.e. end of turn i), recorded by add_turn.  Invariant:
    # len(turn_artifact_snapshots) == len(turn_messages) - 1.  Edit-truncation
    # rolls the store back to entry N-1 when revoking turn N.  Sessions saved
    # before this field existed have an empty list (rollback falls back to
    # keeping the current snapshot).
    turn_artifact_snapshots: list[str] = field(default_factory=list)
    pinned: bool = False  # 置顶会话排在历史列表最前（按用户隔离的展示偏好）
    active_domains: list[str] = field(default_factory=list)  # 已激活领域（域门控）
    # 本会话内已获"会话级放行"的工具名（确认链路 approve_session），跨重启恢复。
    approved_tools: list[str] = field(default_factory=list)
    # 事件日志水位线：最近一次随内容变更落盘的 run 事件 seq。不变式
    # 「快照(event_seq=W) + 日志中 seq>W 的全部事件 ≡ 完整前端状态」由
    # SessionStore 的方法级 event_seq 打点与 RunEventLog 的 seq 分配共同保证
    # （见 run_event_log.py 模块注释）。跨 run 单调递增：续轮 run 的日志从
    # event_seq+1 起。legacy 会话缺省 0（运行中的会话必然由新代码创建）。
    event_seq: int = 0

    def to_summary_dict(self) -> dict[str, Any]:
        tool_count = sum(len(s.tools) for s in self.steps)
        issue_count = sum(
            1 for s in self.steps for t in s.tools if t.status in ("error", "warning")
        )
        return {
            "id": self.id,
            "task": self.task,
            "status": self.status,
            "modelName": self.model_name,
            "lastModelId": self.last_model_id,
            "createdAt": self.created_at,
            "stepCount": len(self.steps),
            "toolCount": tool_count,
            "issueCount": issue_count,
            "pinned": self.pinned,
        }

    def _step_dict(self, s: StepRecord) -> dict[str, Any]:
        result: dict[str, Any] = {
            "index": s.index,
            "numeral": s.numeral,
            "label": s.label,
            "skill": s.skill,
            "tools": [t.to_dict() for t in s.tools],
            "verdict": s.verdict,
            "turnIndex": s.turn_index,
            "startSegmentIndex": s.start_segment_index,
        }
        if s.end_segment_index is not None:
            result["endSegmentIndex"] = s.end_segment_index
        if s.subagents:
            result["subagents"] = [sa.to_dict() for sa in s.subagents]
        else:
            result["subagents"] = []
        return result

    def _build_turns(self) -> list[dict[str, Any]]:
        """Build turns from turn_messages + turn_step_starts + steps.

        Each turn has: message (user text), steps (the steps in that turn),
        and conclusion (per turn; only legacy sessions with no per-turn list
        at all fall back to the session-level conclusion on the last turn —
        a turn that errored/was stopped has an empty conclusion).
        """
        starts = self.turn_step_starts if self.turn_step_starts else [0]
        messages = (
            self.turn_messages
            if self.turn_messages
            else [{"text": self.task, "timestamp": self.created_at}]
        )
        conclusions = self.turn_conclusions
        num_turns = len(starts)
        turns: list[dict[str, Any]] = []
        for i, start_idx in enumerate(starts):
            end_idx = starts[i + 1] if i + 1 < len(starts) else len(self.steps)
            msg = messages[i] if i < len(messages) else {"text": "", "timestamp": self.created_at}
            if i < len(conclusions):
                turn_conclusion = conclusions[i]
            elif not conclusions and i == num_turns - 1:
                # Legacy sessions predate per-turn conclusions and recorded
                # only a session-level one, so the fallback applies when the
                # list is entirely absent.  A missing entry in an otherwise
                # populated list means the turn never completed (error/
                # stopped): its conclusion is empty, not the previous
                # turn's.
                turn_conclusion = self.conclusion
            else:
                turn_conclusion = ""
            turns.append(
                {
                    "message": {
                        "role": "user",
                        "text": msg.get("text", ""),
                        "timestamp": msg.get("timestamp", self.created_at),
                        "fileName": msg.get("fileName"),
                        "fileId": msg.get("fileId"),
                        # Message-level media attachments (kind/name/fileId);
                        # legacy turns keep the single fileName/fileId fields.
                        "attachments": msg.get("attachments", []),
                        "modelId": msg.get("modelId"),
                        "modelName": msg.get("modelName"),
                    },
                    "steps": [self._step_dict(s) for s in self.steps[start_idx:end_idx]],
                    "conclusion": turn_conclusion,
                }
            )
        return turns

    def to_detail_dict(self) -> dict[str, Any]:
        tree_data: dict[str, Any] | None = None
        if self.tree_json:
            try:
                tree_data = json.loads(self.tree_json)
            except json.JSONDecodeError:
                tree_data = None
        return {
            "id": self.id,
            "task": self.task,
            "modelName": self.model_name,
            "lastModelId": self.last_model_id,
            "status": self.status,
            "turns": self._build_turns(),
            "steps": [self._step_dict(s) for s in self.steps],
            "thoughts": [
                {
                    "id": th.id,
                    "text": th.text,
                    "turn": th.turn,
                    "timestamp": th.timestamp,
                    "segmentIndex": th.segment_index,
                    "segmentType": th.segment_type,
                    "stepIndex": th.step_index,
                    "turnIndex": th.turn_index,
                }
                for th in self.thoughts
            ],
            "stats": {
                "tokensIn": self.tokens_in,
                "tokensOut": self.tokens_out,
                "elapsed": (
                    round((self.finished_at - self.created_at), 1)
                    if self.finished_at and self.created_at
                    else 0.0
                ),
            },
            "conclusion": self.conclusion,
            "createdAt": self.created_at,
            "treeJson": tree_data,
            "currentNodeId": self.current_node_id,
            "activeDomains": list(self.active_domains),
            "approvedTools": list(self.approved_tools),
            "contextCompacted": context_state_compacted(self.context_state),
            "eventSeq": self.event_seq,
        }
