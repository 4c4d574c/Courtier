import { reactive, ref, computed, watch } from "vue";
import type { Session, Turn } from "../types/agent";
import type { AgentEvent } from "../types/agent";
import { api } from "../api/client";
import { MESSAGES } from "../constants/messages";
import {
  createSessionEventHandlers,
  type MutableState,
  finalizeRunningOperations,
} from "./sessionEventHandlers";

export function useAgentSession() {
  const session = reactive<Session>({
    id: "",
    task: "",
    modelName: "",
    status: "completed",
    turns: [],
    steps: [],
    thoughts: [],
    stats: { tokensIn: 0, tokensOut: 0, elapsed: 0 },
    createdAt: Date.now(),
  });

  const currentSessionId = ref("");
  const eventSource = ref<EventSource | null>(null);
  const turnVersion = ref(0);
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
    turnVersion,
  }));

  // ---- connection lifecycle ----

  function connect(task: string, fileId?: string, fileName?: string) {
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
    state.currentThoughtTurn = 0;
    state.segmentIndex = 0;
    state.currentSegmentType = "observe";
    state.pendingSubagents = [];
    state.subagentThoughtCounters = {};

    const turn: Turn = {
      message: { role: "user", text: task, fileId, fileName, timestamp: Date.now() },
      steps: [],
    };
    session.turns.push(turn);
    state.currentTurn = session.turns[session.turns.length - 1];

    api
      .createEventSource({
        task,
        fileId: fileId || undefined,
        sessionId: currentSessionId.value || undefined,
      })
      .then((es: EventSource) => {
        if (generation !== connectGeneration) {
          // Superseded by a newer connect()/disconnect() — do not attach
          // a zombie stream.
          es.close();
          return;
        }
        es.onmessage = (e) => {
          reconnectCount = 0;
          try {
            const event: AgentEvent = JSON.parse(e.data);
            if (import.meta.env.DEV) {
              console.debug("[SSE]", event.type, event);
            }
            handlers.handleSessionEvent(event);
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
          // The access cookie may have expired during a long stream — try to
          // rotate it so the browser's automatic reconnect can re-authenticate.
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
      })
      .catch((err: unknown) => {
        if (generation !== connectGeneration) return;
        const msg = err instanceof Error ? err.message : "连接失败";
        session.status = "error";
        session.errorMessage = `无法连接审核引擎：${msg}`;
        finalizeRunningOperations(session, "error", "error", "连接失败");
      });
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
  }

  async function stop() {
    try {
      await api.stop(session.id || undefined);
    } catch (_err) {
      console.warn("远端停止请求失败，会话可能仍在后端运行", _err);
    }
    session.status = "completed";
    session.stopReason = "user";
    disconnect();
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
    if (session.status === "running") {
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
    eventSource.value?.close();
    eventSource.value = null;
  }

  const isRunning = computed(() => session.status === "running");

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
    isRunning,
    isCompacted,
    turnVersion,
  };
}
