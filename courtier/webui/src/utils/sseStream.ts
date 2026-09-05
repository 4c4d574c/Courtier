/**
 * Fetch-based SSE transport.
 *
 * Replaces the browser EventSource where headers (Authorization,
 * Last-Event-ID) or POST bodies are needed.  The wire format is unchanged:
 * the server still emits standard `id:`/`data:` frames.  The client parses
 * frames incrementally and mimics EventSource semantics closely enough for
 * the composables:
 *
 * - `onmessage({ data })` per complete frame
 * - `onerror()` on connection trouble; `readyState` reaches CLOSED (2) when
 *   no further retries happen (initial-connect failure, or mid-stream break
 *   with `reconnect: false`)
 * - with `reconnect: true` the client re-issues the same GET with the
 *   Last-Event-ID header after a backoff, firing `onerror` per failure
 *   (exactly like a native EventSource retry loop)
 */

export const SSE_CONNECTING = 0;
export const SSE_OPEN = 1;
export const SSE_CLOSED = 2;

export interface SseFetchOptions {
  method?: "GET" | "POST";
  /** JSON-serializable body (POST only). */
  body?: unknown;
  /** Initial Last-Event-ID header (resume watermark). */
  lastEventId?: string;
  /** Re-issue the same request after mid-stream errors. */
  reconnect?: boolean;
  /** Extra headers (e.g. Authorization). */
  headers?: Record<string, string>;
  onOpen?: () => void;
}

export class SseFetchClient {
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onopen: (() => void) | null = null;
  readyState: number = SSE_CONNECTING;

  private closed = false;
  private controller: AbortController | null = null;
  private lastEventId: string;

  constructor(
    private url: string,
    private opts: SseFetchOptions = {},
  ) {
    this.lastEventId = opts.lastEventId ?? "";
    void this.loop(true);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.readyState = SSE_CLOSED;
    this.controller?.abort();
  }

  private failClosed(): void {
    this.readyState = SSE_CLOSED;
    this.onerror?.();
  }

  private async loop(first: boolean): Promise<void> {
    let delay = 1000;
    while (!this.closed) {
      this.controller = new AbortController();
      this.readyState = SSE_CONNECTING;
      let opened = false;
      try {
        const headers: Record<string, string> = {
          Accept: "text/event-stream",
          ...this.opts.headers,
        };
        if (this.lastEventId) headers["Last-Event-ID"] = this.lastEventId;
        const init: RequestInit = {
          method: this.opts.method ?? "GET",
          headers,
          credentials: "include",
          signal: this.controller.signal,
        };
        if (this.opts.body !== undefined) {
          headers["Content-Type"] = "application/json";
          init.body = JSON.stringify(this.opts.body);
        }
        const res = await fetch(this.url, init);
        if (!res.ok || !res.body) {
          // Non-2xx at open: EventSource fails the connection permanently.
          this.failClosed();
          return;
        }
        opened = true;
        this.readyState = SSE_OPEN;
        this.onopen?.();

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let sep: number;
          while ((sep = buffer.indexOf("\n\n")) !== -1) {
            const raw = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const dataLines: string[] = [];
            let id = this.lastEventId;
            for (const line of raw.split("\n")) {
              if (line.startsWith("id:")) id = line.slice(3).trim();
              else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
              // ":"-prefixed comments (heartbeats) are ignored.
            }
            if (dataLines.length) {
              this.lastEventId = id;
              this.onmessage?.({ data: dataLines.join("\n") });
            }
          }
        }
        // Server closed the stream (terminal event + seal) — done.
        this.readyState = SSE_CLOSED;
        return;
      } catch {
        if (this.closed) return;
        // Network-level error; fall through to retry handling.  The
        // controller is kept until close()/the next iteration so close()
        // can still abort a stalled body read.
      }
      if (!opened && first) {
        // Initial connect failure: like EventSource, no retry is coming.
        this.failClosed();
        return;
      }
      this.readyState = SSE_CONNECTING;
      this.onerror?.();
      if (!this.opts.reconnect) {
        this.readyState = SSE_CLOSED;
        return;
      }
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 30_000);
      first = false;
    }
  }
}
