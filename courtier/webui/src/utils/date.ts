/** Shared date/time formatters for the webui (single source of truth). */

/** Format an ISO datetime string as a zh-CN date; "-" for empty/invalid input. */
export function formatDate(d: string): string {
  if (!d) return "-";
  const date = new Date(d);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleDateString("zh-CN");
}
