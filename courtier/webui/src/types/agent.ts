/** Tool execution status */
export type ToolStatus = 'pending' | 'running' | 'done' | 'error' | 'warning' | 'cancelled'

/** Tool call record kind */
export type ToolCallKind = 'tool' | 'subagent_run'

/** Tool call execution scope */
export type ToolCallScope = 'parent' | 'subagent'

/** Tool detail when expanded */
export type ToolDetail =
  | { type: 'structured'; data: Record<string, unknown> }
  | { type: 'markdown'; content: string }

/** Individual tool result */
export interface ToolResult {
  id: string
  name: string
  skill: string
  skillDescription?: string
  status: ToolStatus
  callKind: ToolCallKind
  callScope: ToolCallScope
  subagentName: string | null
  handleId?: string | null
  parentHandleId?: string | null
  segmentIndex?: number
  duration?: number
  summary?: string
  detail?: ToolDetail
  startTime?: number
  progress?: string
}

/** Real-time thinking token */
export interface Thought {
  id: number
  text: string
  turn: number
  turnIndex?: number
  segmentIndex?: number
  segmentType?: 'observe' | 'tool_result'
  stepIndex?: number
  source?: string
  timestamp: number
}

export interface SubagentThought {
  id: number
  text: string
}

/** Runtime state of a sub-agent execution */
export interface SubagentRun {
  name: string
  handleId: string
  parentHandleId?: string | null
  task: string
  status: 'running' | 'completed' | 'error'
  conclusion?: string
  error?: string
  thoughts?: SubagentThought[]
  /** Nested sub-agents spawned by this sub-agent */
  children?: SubagentRun[]
  /** Tools executed by this sub-agent */
  tools?: ToolResult[]
  /** Optional wrapper tool result representing this sub-agent run */
  wrapper?: ToolResult
}

/** One audit step (one tool_calls decision) */
export interface Step {
  index: number
  numeral: string
  label: string
  skill: string
  tools: ToolResult[]
  verdict?: string
  subagents?: SubagentRun[]
  turnIndex?: number
  startSegmentIndex?: number
  endSegmentIndex?: number
}

/** A single conversation message */
export interface Message {
  role: 'user'
  text: string
  fileName?: string
  fileId?: string
  timestamp: number
}

/** One turn: user message + agent response */
export interface Turn {
  message: Message
  steps: Step[]
  conclusion?: string
}

/** OpenAI-compatible message stored in a conversation tree node */
export interface ConversationTreeMessage {
  role: string
  content?: string | null
  tool_calls?: Array<{
    id: string
    function: { name: string; arguments: string }
  }>
  tool_call_id?: string | null
  name?: string | null
  source?: string | null
}

/** A single node in the conversation tree */
export interface ConversationTreeNode {
  node_id: string
  parent_id: string | null
  turn_index: number
  messages: ConversationTreeMessage[]
  tool_results: string[]
  metadata: Record<string, unknown>
  children: string[]
}

/** Serialized conversation tree returned by the backend */
export interface ConversationTree {
  root_id: string | null
  nodes: Record<string, ConversationTreeNode>
}

/** Full session */
export interface Session {
  id: string
  task: string
  modelName: string
  status: 'running' | 'paused' | 'completed' | 'error'
  errorMessage?: string
  stopReason?: 'user'
  conclusion?: string
  turns: Turn[]
  steps: Step[]
  thoughts: Thought[]
  stats: {
    tokensIn: number
    tokensOut: number
    elapsed: number
  }
  createdAt: number
  /** Runtime/debug events captured during the session */
  guardEvents?: RuntimeEvent[]
  hintEvents?: RuntimeEvent[]
  modelEvents?: RuntimeEvent[]
  loopCompleted?: RuntimeEvent
  /** Serialized conversation tree for branching/replay */
  treeJson?: ConversationTree | null
  currentNodeId?: string | null
}

/** History list item (lightweight) */
export interface SessionSummary {
  id: string
  task: string
  status: Session['status']
  createdAt: number
  stepCount: number
  toolCount: number
  issueCount: number
  modelName?: string
}

/** Runtime event emitted by backend for guard/model/hint/loop lifecycle */
export interface RuntimeEvent {
  type: 'guard_triggered' | 'hint_injected' | 'model_selected' | 'model_fallback' | 'loop_completed'
  layer?: string
  guardName?: string
  action?: 'allow' | 'log' | 'block'
  reason?: string
  hintType?: string
  text?: string
  model?: string
  backend?: string
  strategy?: string
  status?: string
  terminationReason?: string
  totalSteps?: number
}

/** SSE event from backend */
export interface AgentEvent {
  type: 'think' | 'act' | 'observe' | 'token' | 'tool_result' | 'tool_start' | 'tool_progress' | 'usage' | 'complete' | 'error' | 'session' | 'subagent_start' | 'subagent_think' | 'subagent_token' | 'subagent_tool_result' | 'subagent_conclusion' | 'subagent_end' | 'stopped' | 'conclusion_token' | 'guard_triggered' | 'hint_injected' | 'model_selected' | 'model_fallback' | 'loop_completed'
  detail?: string
  text?: string
  name?: string
  summary?: string
  conclusion?: string
  sessionId?: string
  modelName?: string
  skill?: string
  skillDescription?: string
  detail_data?: ToolDetail
  displayName?: string | null
  callKind?: ToolCallKind
  callScope?: ToolCallScope
  subagentName?: string | null
  parentSubagentName?: string | null
  handleId?: string | null
  parentHandleId?: string | null
  task?: string
  toolName?: string
  toolStatus?: 'ok' | ToolStatus
  toolDuration?: number
  toolSummary?: string
  result?: unknown
  tokens_in?: number
  tokens_out?: number
  tokensIn?: number
  tokensOut?: number
  status?: 'ok' | ToolStatus
  duration?: number
  progress?: {
    status: string
    message: string
    detail: Record<string, unknown> | null
  }
  layer?: string
  guardName?: string
  action?: 'allow' | 'log' | 'block'
  reason?: string
  hintType?: string
  model?: string
  backend?: string
  strategy?: string
  terminationReason?: string
  totalSteps?: number
  /** Structured think payload: tool names announced for the current step. */
  toolCalls?: string[]
  /** Structured think payload: marks a free-form text response. */
  textResponse?: boolean
  /** Structured act payload: tool names being executed. */
  tools?: string[]
}
