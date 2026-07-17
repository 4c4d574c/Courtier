/** Shared date/time formatters for the webui (single source of truth). */

/** Format an ISO datetime string as a zh-CN date; "-" for empty/invalid input. */
export function formatDate(d: string): string {
  if (!d) return "-";
  const date = new Date(d);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleDateString("zh-CN");
}

/** Format a millisecond timestamp as "M/D HH:mm". */
export function formatDateTime(ts: number): string {
  const d = new Date(ts);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.getHours()}:${String(
    d.getMinutes(),
  ).padStart(2, "0")}`;
}
