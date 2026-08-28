/**
 * Coalescing throttle: at most one call per window, with a trailing call so
 * the last request in a burst still lands. Used for sidebar list refreshes
 * triggered by run-status events — several background sessions finishing in
 * quick succession must not fire a burst of list requests (the list route is
 * rate-limited; see courtier/agent/api/routes/sessions.py).
 */
export function createTrailingThrottle(
  fn: () => void | Promise<void>,
  windowMs: number,
): () => void {
  let lastInvokeAt = 0;
  let trailingTimer: ReturnType<typeof setTimeout> | null = null;

  function invoke(): void {
    lastInvokeAt = Date.now();
    void fn();
  }

  return () => {
    if (trailingTimer !== null) return;
    const elapsed = Date.now() - lastInvokeAt;
    if (elapsed >= windowMs) {
      invoke();
      return;
    }
    trailingTimer = setTimeout(() => {
      trailingTimer = null;
      invoke();
    }, windowMs - elapsed);
  };
}
