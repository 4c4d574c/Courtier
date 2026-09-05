import { reactive, ref, computed, watch } from "vue";
import type { AttachmentMeta, Session, Step, Turn } from "../types/agent";
import type { AgentEvent } from "../types/agent";
import { api } from "../api/client";
import { MESSAGES } from "../constants/messages";
import { SSE_CLOSED, type SseFetchClient } from "../utils/sseStream";
import { useModelPool } from "./useModelPool";
import {
  createSessionEventHandlers,
  type MutableState,
  finalizeRunningOperations,
} from "./sessionEventHandlers";

/** Largest `tool-N` suffix across the snapshot's tools — a re-attached stream
 *  continues the live run's id counter so replayed events mint ids that cannot
 *  collide with the tools the snapshot restored. */
function maxSnapshotToolId(steps: Step[]): number {
  // The id counter is global across top-level tools AND nested subagent
  // trees — a late subagent tool result can mint an id greater than every
  // top-level tool's, so scanning only step.tools lets replayed events
  // collide with restored nodes.
  let max = 0;
  const scanTool = (tool: { id?: string }) => {
    const n = Number(/^tool-(\d+)$/.exec(tool.id ?? "")?.[1] ?? 0);
    if (n > max) max = n;
  };
  const scanNode = (node: {
    id?: string;
    tools?: Array<{ id?: string }>;
    children?: unknown[];
  }) => {
    scanTool(node);
    for (const child of node.children ?? []) scanNode(child as never);
  };
  for (const step of steps) {
    for (const tool of step.tools ?? []) {
      scanTool(tool);
      const node = tool as { subagents?: unknown[] };
      for (const sub of node.subagents ?? []) scanNode(sub as never);
    }
  }
  return max;
}

export function useAgentSession() {
  const session = reactive<Session>({
    id: "",
    task: "",
    modelName: "",
    lastModelId: "",
    status: "completed",
    turns: [],
    steps: [],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: Date.now(),
  });

  const currentSessionId = ref("");
  const eventSource = ref<SseFetchClient | null>(null);
  let reconnectCount = 0;
  const MAX_RECONNECTS = 3;
  // Incremented on every connect()/disconnect() — a pending createEventSource
  // promise from a superseded connect is closed instead of being attached,
  // preventing zombie streams when connect/stop race each other.
  let connectGeneration = 0;

  const state: MutableState = {
    currentTurn: null,
    currentTurnIndex: 0,
    currentStepIndex: 0,
    segmentIndex: 0,
    currentSegmentType: "observe",
    currentThoughtTurn: 0,
    observedSinceLastStep: false,
    thoughtIdCounter: 0,
    toolIdCounter: 0,
    subagentThoughtCounters: {},
    pendingSubagents: [],
  };

  const handlers = createSessionEventHandlers(() => ({
    state,
    session,
  }));

  // ---- token-event coalescing ----
  // Per-token SSE events each trigger a full message-list rebuild + VDOM diff
  // (the dominant streaming cost; see docs/architecture/webui-streaming-perf-plan.md).
  // Buffer the high-frequency append-only events and replay them in order on a
  // frame-aligned flush so the UI re-renders at most once per frame instead of
  // once per SSE message. Non-buffered events flush the buffer first to keep
  // the replay order identical to arrival order; teardown drops the buffer so
  // a pending flush can never write a stale session's tokens into a reset one.
  const BUFFERED_EVENT_TYPES = new Set([
    "token",
    "conclusion_token",
    "subagent_token",
    "subagent_think",
  ]);
  let bufferedEvents: AgentEvent[] = [];
  let flushRafId: number | null = null;
  let flushTimerId: ReturnType<typeof setTimeout> | null = null;

  function flushBufferedEvents(): void {
    if (flushRafId !== null) {
      cancelAnimationFrame(flushRafId);
      flushRafId = null;
    }
    if (flushTimerId !== null) {
      clearTimeout(flushTimerId);
      flushTimerId = null;
    }
    if (bufferedEvents.length === 0) return;
    const events = bufferedEvents;
    bufferedEvents = [];
    for (const ev of events) handlers.handleSessionEvent(ev);
  }

  function dropBufferedEvents(): void {
    if (flushRafId !== null) {
      cancelAnimationFrame(flushRafId);
      flushRafId = null;
    }
    if (flushTimerId !== null) {
      clearTimeout(flushTimerId);
      flushTimerId = null;
    }
    bufferedEvents = [];
  }

  function scheduleBufferedFlush(): void {
    if (flushRafId !== null || flushTimerId !== null) return;
    // Backstop first: rAF is suspended in background tabs, the timer keeps
    // the stream live there (browser throttles it to ≥1s — acceptable).
    flushTimerId = setTimeout(() => {
      flushTimerId = null;
      if (flushRafId !== null) {
        cancelAnimationFrame(flushRafId);
        flushRafId = null;
      }
      flushBufferedEvents();
    }, 50);
    if (typeof requestAnimationFrame === "function") {
      flushRafId = requestAnimationFrame(() => {
        flushRafId = null;
        if (flushTimerId !== null) {
          clearTimeout(flushTimerId);
          flushTimerId = null;
        }
        flushBufferedEvents();
      });
    } else {
      // Non-DOM environments (node-side test harnesses): flush next tick so
      // `await tick()`-style assertions observe the applied events.
      clearTimeout(flushTimerId);
      flushTimerId = setTimeout(() => {
        flushTimerId = null;
        flushBufferedEvents();
      }, 0);
    }
  }

  function dispatchSessionEvent(event: AgentEvent): void {
    if (BUFFERED_EVENT_TYPES.has(event.type)) {
      bufferedEvents.push(event);
      scheduleBufferedFlush();
      return;
    }
    flushBufferedEvents();
    handlers.handleSessionEvent(event);
  }

  // High-frequency streaming events are dropped from the dev SSE log —
  // per-token console.debug is itself a dev-mode jank source (serialization
  // + console rendering for hundreds of events per second).
  const TOKEN_QUIET_LOG_TYPES = new Set([
    "token",
    "conclusion_token",
    "subagent_token",
    "subagent_think",
  ]);

  // ---- connection lifecycle ----

  /**
   * Wire an EventSource's message/error handlers into the session event
   * pipeline. Shared by the initiating stream (connect) and the re-attach
   * stream (attachToRunningSession) — replays are the exact same event
   * sequence the live stream would have delivered, so one handler set
   * covers both.
   */
  function wireEventSource(es: SseFetchClient, _generation: number) {
    es.onmessage = (e) => {
      reconnectCount = 0;
      try {
        const event: AgentEvent = JSON.parse(e.data);
        if (import.meta.env.DEV && !TOKEN_QUIET_LOG_TYPES.has(event.type)) {
          console.debug("[SSE]", event.type, event);
        }
        dispatchSessionEvent(event);
      } catch (_err) {
        console.warn("Failed to handle SSE data:", _err, e.data);
        session.errorMessage = "数据解析错误，请刷新页面重试";
      }
    };

    es.onerror = () => {
      if (session.status === "completed" || session.status === "error") {
        es.close();
        return;
      }
      // CLOSED means the browser has given up (initial connect failure:
      // expired cookie, 429, backend restart) — no native retry is coming,
      // and without handling this the UI stays "running" forever.
      if (es.readyState === SSE_CLOSED) {
        es.close();
        if (currentSessionId.value) {
          // The run may have started server-side: reload the authoritative
          // snapshot (mirrors the attach path's CLOSED handling). The
          // generation guard keeps a late snapshot from clobbering a
          // session the user switched to meanwhile.
          const generation = connectGeneration;
          void api
            .loadSession(currentSessionId.value)
            .then((fresh) => {
              if (!fresh || generation !== connectGeneration) return;
              restoreSession(fresh);
            })
            .catch(() => {
              session.status = "error";
              session.errorMessage = MESSAGES.CONNECTION_LOST;
              finalizeRunningOperations(session, "error", "error", "连接中断");
            });
          return;
        }
        session.status = "error";
        session.errorMessage = MESSAGES.CONNECTION_LOST;
        finalizeRunningOperations(session, "error", "error", "连接中断");
        return;
      }
      // Transient error: the browser retries natively — rotate the cookie
      // so the automatic reconnect can re-authenticate.
      void api.refreshToken().catch(() => {});
      reconnectCount++;
      if (reconnectCount >= MAX_RECONNECTS) {
        session.status = "error";
        session.errorMessage = MESSAGES.CONNECTION_LOST;
        finalizeRunningOperations(session, "error", "error", "连接中断");
        es.close();
      }
    };
  }

  function connect(
    task: string,
    fileId?: string,
    fileName?: string,
    editTurn?: number,
    fileIds?: string[],
    attachments?: AttachmentMeta[],
  ) {
    disconnect();
    const generation = ++connectGeneration;
    reconnectCount = 0;
    const isNewSession = !currentSessionId.value;

    if (isNewSession) {
      session.task = task;
      session.id = "";
      session.turns = [];
      session.steps = [];
      session.thoughts = [];
      session.stats = { tokensIn: 0, tokensOut: 0, elapsed: 0 };
      session.modelName = "";
      session.conclusion = undefined;
      session.compactions = [];
      session.contextCompacted = false;
      session.compacting = false;
      session.pendingVerdict = "";
      session.pendingVerdictAfterStepIndex = 0;
      session.createdAt = Date.now();
      state.thoughtIdCounter = 0;
      state.toolIdCounter = 0;
      state.subagentThoughtCounters = {};
      state.currentTurnIndex = 0;
    }

    // Per-turn reset — always runs, whether new or continued session.
    session.status = "running";
    session.errorMessage = undefined;
    session.stopReason = undefined;
    session.compacting = false;
    session.pendingVerdict = "";
    session.pendingVerdictAfterStepIndex = 0;
    state.currentTurnIndex++;
    state.currentStepIndex = 0;
    state.observedSinceLastStep = false;
    session.refusalNotice = undefined;
    state.currentThoughtTurn = 0;
    state.segmentIndex = 0;
    state.currentSegmentType = "observe";
    state.pendingSubagents = [];
    state.subagentThoughtCounters = {};

    const turn: Turn = {
      message: {
        role: "user",
        text: task,
        fileId,
        fileName,
        attachments,
        timestamp: Date.now(),
      },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = session.turns[session.turns.length - 1];

    // The fetch client starts streaming immediately; the generation check
    // closes a zombie stream if a newer connect()/disconnect() superseded us.
    const es = api.createEventSource({
      task,
      fileId: fileId || undefined,
      fileIds: fileIds?.length ? fileIds.join(",") : undefined,
      sessionId: currentSessionId.value || undefined,
      editTurn,
      modelId: useModelPool().selectedModelId.value || undefined,
    });
    if (generation !== connectGeneration) {
      es.close();
      return;
    }
    wireEventSource(es, generation);
    eventSource.value = es;
  }

  function newSession() {
    disconnect();
    currentSessionId.value = "";
    session.id = "";
    session.status = "completed";
    session.task = "";
    session.turns = [];
    session.steps = [];
    state.pendingSubagents = [];
    session.thoughts = [];
    session.conclusion = undefined;
    session.pendingVerdict = "";
    session.pendingVerdictAfterStepIndex = 0;
    session.compactions = [];
    session.contextCompacted = false;
    session.compacting = false;
    session.stats = { tokensIn: 0, tokensOut: 0, elapsed: 0 };
    session.modelName = "";
    session.createdAt = Date.now();
    // Cross-session residue: these would otherwise leak from the session
    // the user just left (refusal banner, confirm cards, queue badge,
    // guard/hint events, error banner).
    session.refusalNotice = undefined;
    session.pendingConfirmations = [];
    session.queuePosition = undefined;
    session.errorMessage = undefined;
    session.stopReason = undefined;
    session.guardEvents = [];
    session.hintEvents = [];
    state.thoughtIdCounter = 0;
    state.toolIdCounter = 0;
    state.subagentThoughtCounters = {};
    state.currentThoughtTurn = 0;
    state.currentTurn = null;
    state.currentTurnIndex = 0;
    state.segmentIndex = 0;
    state.currentSegmentType = "observe";
    state.currentStepIndex = 0;
    state.observedSinceLastStep = false;
  }

  function restoreSession(loaded: Session) {
    disconnect();
    currentSessionId.value = loaded.id || "";
    session.id = loaded.id || "";
    session.task = loaded.task || "";
    session.modelName = loaded.modelName || "";
    session.lastModelId = loaded.lastModelId || "";
    // Per-session provenance: preselect the model this session last used.
    useModelPool().preselectModel(loaded.lastModelId);
    session.status = loaded.status || "completed";
    session.turns = loaded.turns || [];
    session.steps = loaded.steps || [];
    session.thoughts = loaded.thoughts || [];
    session.stats = loaded.stats || { tokensIn: 0, tokensOut: 0, elapsed: 0 };
    session.conclusion = loaded.conclusion;
    session.compactions = [];
    session.contextCompacted = loaded.contextCompacted ?? false;
    session.compacting = false;
    session.pendingVerdict = "";
    // Not persisted per session — stale values from the previous session
    // must not bleed into this one.
    session.refusalNotice = undefined;
    session.guardEvents = [];
    session.hintEvents = [];
    session.pendingVerdictAfterStepIndex = 0;
    session.errorMessage = loaded.errorMessage;
    session.stopReason = loaded.stopReason;
    session.createdAt = loaded.createdAt || Date.now();
    state.currentTurn = null;
    state.currentTurnIndex = loaded.turns?.length ?? 0;
    state.currentStepIndex = 0;
    state.segmentIndex = 0;
    state.currentSegmentType = "observe";
    state.currentThoughtTurn = 0;
    state.observedSinceLastStep = false;
    state.thoughtIdCounter = 0;
    state.toolIdCounter = 0;
    state.subagentThoughtCounters = {};
    state.pendingSubagents = [];
    session.eventSeq = loaded.eventSeq;
    session.queuePosition = undefined;
    // 确认链路：详情载荷携带挂起中的确认（重连恢复用），事件重放兜底。
    session.pendingConfirmations = loaded.pendingConfirmations ?? [];

    // Still running server-side (left the page mid-run): re-attach to the
    // live stream. The server replays events after the snapshot's watermark
    // (eventSeq) then streams live — replay is the exact same event sequence
    // the uninterrupted stream would have delivered. In that stream the
    // events land in the turn connect() pushed locally; here the turn comes
    // from the snapshot, so the pipeline's alignment state must be rebuilt
    // from it — otherwise replayed steps/thoughts never attach to the turn
    // and the restored page shows no streaming content.
    if (session.status === "running" || session.status === "queued") {
      const lastStep = session.steps[session.steps.length - 1];
      const lastThought = session.thoughts[session.thoughts.length - 1];
      state.currentTurn = session.turns[session.turns.length - 1] ?? null;
      state.currentStepIndex = lastStep?.index ?? 0;
      state.segmentIndex = lastThought?.segmentIndex ?? 0;
      state.currentSegmentType = lastThought?.segmentType ?? "observe";
      state.currentThoughtTurn = lastThought?.turn ?? 0;
      // Snapshot tools/thoughts keep the ids minted by the live run —
      // continue the counters past them so replayed events cannot collide.
      state.toolIdCounter = maxSnapshotToolId(session.steps);
      state.thoughtIdCounter = Math.max(
        0,
        ...session.thoughts.map((thought) => thought.id),
      );
      attachToRunningSession(loaded.id, loaded.eventSeq ?? 0);
    }
  }

  /**
   * Re-attach to a session whose run is still active server-side.
   *
   * Fallbacks (each degrade gracefully to "reload snapshot, re-attach"):
   *  - 404: the run just ended or its grace period expired → reload the
   *    snapshot and render it as a finished session.
   *  - resync event: the watermark was evicted from the bounded log → close,
   *    reload the snapshot once, and re-attach from its fresh watermark.
   */
  function attachToRunningSession(sessionId: string, since: number) {
    disconnect();
    const generation = ++connectGeneration;
    reconnectCount = 0;

    const es = api.attachSessionEvents(sessionId, since);
    if (generation !== connectGeneration) {
      es.close();
      return;
    }
    // Wire attach-specific handlers directly — they wrap the shared pipeline
    // (intercepting resync / 404-open-failure) instead of composing with
    // wireEventSource's onmessage, which it would overwrite.
    let resynced = false;
    es.onmessage = (e) => {
      reconnectCount = 0;
      let parsed: AgentEvent;
      try {
        parsed = JSON.parse(e.data) as AgentEvent;
      } catch (_err) {
        console.warn("Failed to handle SSE data:", _err, e.data);
        session.errorMessage = "数据解析错误，请刷新页面重试";
        return;
      }
      if (parsed.type === "resync") {
        dropBufferedEvents();
        es.close();
        if (!resynced && generation === connectGeneration) {
          resynced = true;
          void api
            .loadSession(sessionId)
            .then((fresh) => {
              if (generation !== connectGeneration || !fresh) return;
              restoreSession(fresh);
            })
            .catch(() => {
              session.errorMessage = MESSAGES.ATTACH_FAILED;
              finalizeRunningOperations(session, "error", "error", "接续失败");
            });
        }
        return;
      }
      if (import.meta.env.DEV && !TOKEN_QUIET_LOG_TYPES.has(parsed.type)) {
        console.debug("[SSE]", parsed.type, parsed);
      }
      dispatchSessionEvent(parsed);
    };

    es.onerror = () => {
      if (session.status === "completed" || session.status === "error") {
        es.close();
        return;
      }
      // The run vanished (404 at open → readyState CLOSED, no HTTP status to
      // read from EventSource): reload the snapshot and stop streaming.
      if (es.readyState === SSE_CLOSED) {
        es.close();
        if (generation !== connectGeneration) return;
        void api
          .loadSession(sessionId)
          .then((fresh) => {
            if (generation !== connectGeneration || !fresh) return;
            restoreSession(fresh);
          })
          .catch(() => {
            session.status = "error";
            session.errorMessage = MESSAGES.ATTACH_FAILED;
            finalizeRunningOperations(session, "error", "error", "接续失败");
          });
        return;
      }
      // Transient error: the browser retries natively (with Last-Event-ID).
      void api.refreshToken().catch(() => {});
      reconnectCount++;
      if (reconnectCount >= MAX_RECONNECTS) {
        session.status = "error";
        session.errorMessage = MESSAGES.CONNECTION_LOST;
        finalizeRunningOperations(session, "error", "error", "连接中断");
        es.close();
      }
    };

    eventSource.value = es;
  }

  /** 确认链路：裁决一条挂起的工具确认。乐观撤卡，失败回滚为待确认。 */
  async function resolveConfirmation(
    confirmationId: string,
    decision: "approve" | "approve_session" | "deny",
  ) {
    const sid = session.id;
    if (!sid) return;
    const backup = session.pendingConfirmations ?? [];
    session.pendingConfirmations = (session.pendingConfirmations ?? []).filter(
      (c) => c.confirmationId !== confirmationId,
    );
    try {
      await api.resolveConfirmation(sid, confirmationId, decision);
    } catch (err) {
      // Roll back only in the session the decision belonged to, and only
      // when it might still be pending (e.g. a network blip) — an
      // "already resolved" answer means the card is correctly gone.
      const msg = err instanceof Error ? err.message : String(err);
      if (session.id === sid && !/already/i.test(msg)) {
        // Merge instead of overwrite: cards that arrived while the request
        // was in flight must survive the rollback of this one card.
        const current = session.pendingConfirmations ?? [];
        const currentIds = new Set(current.map((c) => c.confirmationId));
        session.pendingConfirmations = [
          ...current,
          ...backup.filter((c) => !currentIds.has(c.confirmationId)),
        ];
        session.errorMessage = msg;
      }
    }
  }

  async function stop() {
    // The await below may outlast this session: switching sessions swaps
    // `session` contents in place. Guard with the generation so a finished
    // stop() cannot clobber the newly restored session or kill its stream.
    const gen = connectGeneration;
    const stoppedSessionId = session.id;
    try {
      await api.stop(session.id || undefined);
    } catch (_err) {
      console.warn("远端停止请求失败，会话可能仍在后端运行", _err);
      return;
    }
    if (gen !== connectGeneration || session.id !== stoppedSessionId) return;
    session.status = "completed";
    session.stopReason = "user";
    // The SSE `stopped` frame races this POST and disconnect() may swallow
    // it — settle running tool cards locally (idempotent if it did arrive).
    finalizeRunningOperations(session, "cancelled", "error", "用户已停止");
    disconnect();
  }

  /**
   * Edit-resend: drop turn `turnIndex` and everything after it locally (the
   * server truncates its side via the editTurn param), then re-run the turn
   * with the edited text.  The edited turn keeps its original upload — edit
   * is text-only.
   */
  function editAndResend(turnIndex: number, text: string) {
    if (!currentSessionId.value) return;
    if (isRunning.value) return;
    if (isCompacted.value) {
      session.errorMessage = MESSAGES.CHAT_EDIT_COMPACTED_HINT;
      return;
    }
    if (turnIndex < 0 || turnIndex >= session.turns.length) return;

    const edited = session.turns[turnIndex];
    const fileId = edited.message.fileId;
    const fileName = edited.message.fileName;
    // Edit-resend keeps the turn's media attachments (same fileIds).
    const editedAttachments = edited.message.attachments ?? [];
    const editedFileIds = editedAttachments.map((a) => a.fileId);

    session.turns = session.turns.slice(0, turnIndex);
    // Rebuild turn-derived top-level state from the surviving turns.
    session.steps = session.turns.flatMap((t) => t.steps);
    // thought.turnIndex is 1-based (connect increments before pushing), so
    // keep everything at or below the surviving last turn.
    session.thoughts = session.thoughts.filter(
      (t) => (t.turnIndex ?? 0) <= turnIndex,
    );
    const lastTurn = session.turns[session.turns.length - 1];
    session.conclusion = lastTurn?.conclusion;
    // Runtime/debug events belong to the revoked turns.
    session.guardEvents = [];
    session.hintEvents = [];
    session.errorMessage = undefined;
    session.stopReason = undefined;
    if (turnIndex === 0) {
      // The conversation title is the first turn's text.
      session.task = text;
    }

    // connect() increments currentTurnIndex and pushes the fresh turn.
    state.currentTurnIndex = turnIndex;
    connect(
      text,
      fileId,
      fileName,
      turnIndex,
      editedFileIds.length ? editedFileIds : undefined,
      editedAttachments.length ? editedAttachments : undefined,
    );
  }

  async function forkSession(nodeId?: string, reason?: string) {
    if (!session.id) return;
    try {
      const result = await api.forkSession(session.id, nodeId, reason);
      const loaded = await api.loadSession(session.id);
      if (loaded) {
        restoreSession(loaded);
      }
      return result;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      session.errorMessage = `分支失败：${msg}`;
      console.warn("forkSession failed:", err);
    }
  }

  async function rewindSession(nodeId: string) {
    if (!session.id) return;
    try {
      const result = await api.rewindSession(session.id, nodeId);
      const loaded = await api.loadSession(session.id);
      if (loaded) {
        restoreSession(loaded);
      }
      return result;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      session.errorMessage = `回退失败：${msg}`;
      console.warn("rewindSession failed:", err);
    }
  }

  async function compactContext() {
    if (!session.id) {
      session.errorMessage = "暂无可压缩的上下文（新会话还没有历史）";
      return;
    }
    if (isRunning.value) {
      session.errorMessage = "运行中无法压缩上下文，请等待完成或停止后再试";
      return;
    }
    try {
      const result = await api.compactSession(session.id);
      session.compactions = [
        ...(session.compactions ?? []),
        {
          text:
            `上下文已手动压缩（${result.beforeMessages} 条消息 → ` +
            `${result.afterMessages} 条，约 ${result.beforeTokens} → ` +
            `${result.afterTokens} tokens）`,
          turnIndex: state.currentTurnIndex,
          timestamp: Date.now(),
        },
      ];
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      session.errorMessage = `压缩上下文失败：${msg}`;
      console.warn("compactContext failed:", err);
    }
  }

  function disconnect() {
    // Invalidate any pending createEventSource resolution so a superseded
    // connect cannot attach its stream afterwards.
    connectGeneration++;
    // A pending coalesced flush must never fire after the caller resets
    // session state — it would write the previous session's tokens into it.
    dropBufferedEvents();
    eventSource.value?.close();
    eventSource.value = null;
  }

  // Queued counts as "running" for interaction gating: the stop button must
  // work (it dequeues server-side) and editing stays blocked (backend 409).
  const isRunning = computed(
    () => session.status === "running" || session.status === "queued",
  );

  // Edit-resend gate: the session history is no longer turn-addressable once
  // a full compaction rewrote it into a summary — either persisted
  // (contextCompacted, survives restore) or live this session (compactions).
  const isCompacted = computed(
    () =>
      (session.contextCompacted ?? false) ||
      (session.compactions?.length ?? 0) > 0,
  );

  // Keep currentSessionId in sync so subsequent turns reuse the same session.
  watch(
    () => session.id,
    (id) => {
      if (id) currentSessionId.value = id;
    },
  );

  return {
    session,
    connect,
    disconnect,
    newSession,
    restoreSession,
    stop,
    forkSession,
    rewindSession,
    compactContext,
    editAndResend,
    isRunning,
    isCompacted,
    resolveConfirmation,
  };
}
