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
import { api } from "../api/client"
import type { SseFetchClient } from "../utils/sseStream";

export interface RunStatusEntry {
  status: string;
  queuePosition?: number;
  conclusion?: string;
  tokensIn?: number;
  tokensOut?: number;
  /** 确认链路：挂起中的工具确认数（undefined = 未变） */
  pendingConfirmations?: number;
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

let es: SseFetchClient | null = null;
let seqCounter = 0;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectDelay = 3000;
let stopped = false;

function dispatch(sessionId: string, entry: RunStatusEntry) {
  statuses[sessionId] = entry;
  const event: RunStatusEvent = { sessionId, ...entry };
  for (const fn of listeners) fn(event);
}

function open() {
  if (es || stopped) return;
  const client = api.createGlobalEventsChannel();
  es = client;
  // onopen fires when the connection is established — the reliable
  // "healthy again" signal (server heartbeats are SSE comment lines, which
  // the browser never dispatches to onmessage, so a quiet-but-healthy
  // channel would otherwise keep the 30s backoff forever).
  client.onopen = () => {
    if (reconnectDelay !== 3000) {
      reconnectDelay = 3000;
      // The channel has no replay — realign from the authoritative list.
      for (const fn of reconnectListeners) fn();
    }
  };
  client.onmessage = (e) => {
    try {
      const payload = JSON.parse(e.data);
      if (payload?.type !== "run_status" || !payload.sessionId) return;
      dispatch(payload.sessionId, {
        status: payload.status,
        queuePosition: payload.queuePosition,
        conclusion: payload.conclusion,
        tokensIn: payload.tokensIn,
        tokensOut: payload.tokensOut,
        pendingConfirmations: payload.pendingConfirmations,
        seq: ++seqCounter,
      });
    } catch (err) {
      console.warn("[run-events] bad payload:", err, e.data);
    }
  };
  client.onerror = () => {
    client.close();
    es = null;
    if (stopped) return;
    // Exponential backoff (3s → cap 30s): a long outage must not turn
    // into a 3s SOS loop of reconnect attempts.
    const delay = reconnectDelay;
    reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        open();
      }, delay);
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

  /** Fired once after the channel is healthy again (first message after a
   *  reconnect) — realign the session list from the authoritative source. */
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
