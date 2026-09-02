/** Tool execution status */
export type ToolStatus = 'pending' | 'running' | 'done' | 'error' | 'warning' | 'cancelled'

/** Issue counts reported by audit tools (from backend metadata.issue_counts) */
export interface IssueCounts {
  err: number
  warn: number
  ok: number
  unchecked?: number
}

/** Tool call record kind */
export type ToolCallKind = 'tool' | 'subagent_run'

/** Tool call execution scope */
export type ToolCallScope = 'parent' | 'subagent'

/** Tool detail when expanded */
export type ToolDetail =
  | { type: 'structured'; data: Record<string, unknown> }
  | { type: 'markdown'; content: string }

/**
 * One search_documents hit carried to the frontend as citation data.
 * Assistant conclusions reference these with `[[n]]` markers (1-based index
 * into the hits of the most recent search_documents result before the message).
 */
export interface CitationHit {
  resourceId?: number | null
  documentId?: number | null
  title?: string
  docType?: string
  /** Full chunk text (server-truncated) shown in the citation card */
  chunkText?: string
  /** ES highlight snippets (`<em>…</em>`-wrapped), preferred for display */
  highlight?: string[]
  chunkNo?: number | null
  paragraphIndex?: number | null
}

/** Individual tool result */
export interface ToolResult {
  id: string
  name: string
  /** plugin.yaml display_name(如"格式解析")，优先于 name 展示 */
  displayName?: string | null
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
  /** 审核计数（err/warn/ok/unchecked），有值时优先于 status 派生渲染 */
  issueCounts?: IssueCounts
  detail?: ToolDetail
  /** search_documents hits for `[[n]]` citation markers */
  citations?: CitationHit[]
  /** 模型分配的工具调用 id——同名单工具并行时用于结果↔卡片精确配对 */
  toolCallId?: string | null
  /** 该次搜索命中的绝对引用编号偏移（首个 hit 对应 [[offset+1]]） */
  citationOffset?: number | null
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
  /** 中文展示名（来自 skill frontmatter display_name），优先于 name 展示 */
  displayName?: string | null
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
  /** 模型池条目 id：会话最近一次运行所用（选择器预选用；空 = 标量模型） */
  lastModelId?: string
  status: 'running' | 'paused' | 'completed' | 'error' | 'queued' | 'interrupted'
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
  /** Live context-compaction notices (not persisted across restore). */
  compactions?: CompactionNotice[]
  /**
   * 后端持久化的压缩标志（detail 接口返回）：发生过 full compaction 的会话
   * 历史已被改写为摘要，无法按轮干净切分，编辑重发入口对其关闭。
   * 运行中的实时压缩经 compactions 体现；两者任一成立即视为已压缩。
   */
  contextCompacted?: boolean
  /** 运行时标志：full compaction 正在进行（LLM 总结中），结束后清除。
   *  仅运行时存在，恢复会话不填充。 */
  compacting?: boolean
  /**
   * 运行中缓冲：已流式收到但尚未定性的结论文本。observe 时后端发
   * step_verdict 事件把它转正为对应 step 的中间结论；轮次结束时剩余
   * 部分成为该轮最终结论。仅运行时存在，恢复会话不填充。
   */
  pendingVerdict?: string
  /**
   * pendingVerdict 的归属边界：这段文本将转正为当前 think 对应 step 的
   * verdict，渲染在该 step 工具框之前；边界记录该 step 的前一个 step
   * 的 index（流式路径下 text_response 会先创建占位 step，故不能简单
   * 取已有 step 总数）。边界之后创建的 step 渲染在文本下方的新框中。
   * 与 pendingVerdict 同生命周期，仅运行时存在。
   */
  pendingVerdictAfterStepIndex?: number
  /**
   * 事件日志水位线（detail 接口返回）：本快照内容对应的后端事件 seq。
   * 恢复到运行中会话时，以它为 since attach，重放恰好不重不漏。
   */
  eventSeq?: number
  /**
   * 运行时标志：本会话当前处于服务端排队（每用户并发满，FIFO 等待）。
   * queued 事件置位、首个运行事件（think/tool_start 等）清除。
   */
  queuePosition?: number

  /** 确认链路：挂起中的工具确认（confirmation_requested 事件 / 详情回放） */
  pendingConfirmations?: PendingConfirmation[]
  /** refusal 重试耗尽：当前模型可能不可用的横幅文案（refusal_exhausted 事件） */
  refusalNotice?: string
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
  /** 置顶会话排在历史列表最前 */
  pinned?: boolean
  /** 服务端排队位置（前面还有 N 个任务）；仅 status === 'queued' 时存在 */
  queuePosition?: number
  /** 确认链路：该会话挂起中的工具确认数（>0 时侧栏行显示"待确认"） */
  pendingConfirmations?: number
}

/** 一次挂起中的工具确认（工具名级三选：仅本次 / 本会话放行 / 拒绝） */
export interface PendingConfirmation {
  confirmationId: string
  toolName: string
  message: string
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
  /** model_selected: 池条目 id（空 = 标量模型） */
  modelId?: string
  backend?: string
  strategy?: string
  status?: string
  terminationReason?: string
  totalSteps?: number
}

/** Context compaction notice (backend triggered a full compaction). */
export interface CompactionNotice {
  text: string
  turnIndex: number
  timestamp: number
}

/** SSE event from backend */
export interface AgentEvent {
  type: 'think' | 'act' | 'observe' | 'token' | 'tool_result' | 'tool_start' | 'tool_progress' | 'usage' | 'complete' | 'error' | 'session' | 'subagent_start' | 'subagent_think' | 'subagent_token' | 'subagent_tool_result' | 'subagent_conclusion' | 'subagent_end' | 'stopped' | 'conclusion_token' | 'step_verdict' | 'guard_triggered' | 'hint_injected' | 'model_selected' | 'model_fallback' | 'loop_completed' | 'context_compacted' | 'context_compacting' | 'queued' | 'resync' | 'confirmation_requested' | 'confirmation_resolved' | 'think_retry' | 'refusal_exhausted'
  detail?: string
  text?: string
  /** confirmation_requested/resolved: 确认 id、工具名、提示文案与裁决结果 */
  confirmationId?: string
  toolName?: string
  message?: string
  decision?: string
  stepIndex?: number
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
  toolStatus?: 'ok' | ToolStatus
  toolDuration?: number
  toolSummary?: string
  /** 工具结果计数（tool_result 与 subagent_tool_result 均可能携带） */
  issueCounts?: IssueCounts
  /** search_documents 命中的引用载荷（tool_result 事件携带） */
  citations?: CitationHit[]
  /** 工具调用 id（tool_start / tool_result 事件携带） */
  toolCallId?: string | null
  /** think 事件公告的调用 id 列表（与 toolCalls 同序） */
  toolCallIds?: string[]
  /** 该次搜索命中的绝对引用编号偏移（tool_result 事件携带） */
  citationOffset?: number
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
  /** model_selected: 池条目 id（空 = 标量模型） */
  modelId?: string
  backend?: string
  strategy?: string
  terminationReason?: string
  totalSteps?: number
  /** Structured think payload: tool names announced for the current step. */
  toolCalls?: string[]
  /** Structured think payload: per-tool Chinese display names. */
  displayNames?: Record<string, string | null>
  /** Structured think payload: marks a free-form text response. */
  textResponse?: boolean
  /** Structured act payload: tool names being executed. */
  tools?: string[]
  /** queued 事件：在服务端 FIFO 队列中的位置（前面还有 N 个任务） */
  position?: number
}
