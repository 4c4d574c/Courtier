/**
 * useRunEvents — app-level singleton consuming the global run-events channel
 * (GET /api/events). Survives navigation: the connection lives at module
 * scope, opens while logged in, closes on logout/401.
 *
 * Sidebar badges read `statuses`; completion/error transitions for a session
 * other than the currently open one surface as toasts. On reconnect the
 * channel has no replay — consumers re-fetch the session list to realign
 * (handled here via the `onReconnect` hook).
 */
import { reactive, readonly } from "vue";
import { api } from "../api/client";

export interface RunStatusEntry {
  status: string;
  queuePosition?: number;
  conclusion?: string;
  tokensIn?: number;
  tokensOut?: number;
  /** monotonically increasing — lets watchers notice repeated transitions */
  seq: number;
}

export interface RunStatusEvent extends RunStatusEntry {
  sessionId: string;
}

type Listener = (event: RunStatusEvent) => void;

const statuses = reactive<Record<string, RunStatusEntry>>({});
const listeners = new Set<Listener>();
const reconnectListeners = new Set<() => void>();

let es: EventSource | null = null;
let seqCounter = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let stopped = false;

function dispatch(sessionId: string, entry: RunStatusEntry) {
  statuses[sessionId] = entry;
  const event: RunStatusEvent = { sessionId, ...entry };
  for (const fn of listeners) fn(event);
}

function open() {
  if (es || stopped) return;
  es = api.createGlobalEventsChannel();
  es.onmessage = (e) => {
    try {
      const payload = JSON.parse(e.data);
      if (payload?.type !== "run_status" || !payload.sessionId) return;
      dispatch(payload.sessionId, {
        status: payload.status,
        queuePosition: payload.queuePosition,
        conclusion: payload.conclusion,
        tokensIn: payload.tokensIn,
        tokensOut: payload.tokensOut,
        seq: ++seqCounter,
      });
    } catch (err) {
      console.warn("[run-events] bad payload:", err, e.data);
    }
  };
  es.onerror = () => {
    es?.close();
    es = null;
    if (stopped) return;
    // Re-fetch dependent state (the channel has no replay), then re-open.
    for (const fn of reconnectListeners) fn();
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        open();
      }, 3000);
    }
  };
}

export function useRunEvents() {
  function start() {
    stopped = false;
    open();
  }

  function stop() {
    stopped = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    es?.close();
    es = null;
    for (const key of Object.keys(statuses)) delete statuses[key];
  }

  function onRunStatus(fn: Listener): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  /** Fired after a channel error, before re-opening — refetch the session list. */
  function onReconnect(fn: () => void): () => void {
    reconnectListeners.add(fn);
    return () => reconnectListeners.delete(fn);
  }

  return {
    statuses: readonly(statuses) as Readonly<Record<string, RunStatusEntry>>,
    start,
    stop,
    onRunStatus,
    onReconnect,
  };
}
