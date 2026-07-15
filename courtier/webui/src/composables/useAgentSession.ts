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
        es.onmessage = (e) => {
          reconnectCount = 0;
          try {
            const event: AgentEvent = JSON.parse(e.data);
            handlers.handleSessionEvent(event);
          } catch (_err) {
            console.warn("Failed to parse SSE data:", _err);
            session.errorMessage = "数据解析错误，请刷新页面重试";
          }
        };

        es.onerror = () => {
          if (session.status === "completed" || session.status === "error") {
            es.close();
            return;
          }
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

  function disconnect() {
    eventSource.value?.close();
    eventSource.value = null;
  }

  const isRunning = computed(() => session.status === "running");

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
    newSession,
    stop,
    isRunning,
    turnVersion,
  };
}
