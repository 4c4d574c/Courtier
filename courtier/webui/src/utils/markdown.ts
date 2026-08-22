import { marked } from "marked";
import DOMPurify from "dompurify";

// Non-DOM environments (node-side test harnesses) expose dompurify's bare
// factory instead of a window-bound instance; sanitization is then a no-op.
const purifier =
  typeof (DOMPurify as { sanitize?: unknown }).sanitize === "function"
    ? DOMPurify
    : null;

export function sanitizeHtml(html: string): string {
  if (typeof html !== "string") return sanitizeHtml(String(html ?? ""));
  return purifier ? purifier.sanitize(html) : html;
}

export function renderMarkdown(content: string): string {
  if (typeof content !== "string") return renderMarkdown(String(content ?? ""));
  const result = marked.parse(content, { async: false });
  if (typeof result !== "string") {
    console.error("marked.parse returned non-string", result);
    return "";
  }
  return sanitizeHtml(result);
}
