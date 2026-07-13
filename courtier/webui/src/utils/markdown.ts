import { marked } from "marked";
import DOMPurify from "dompurify";

export function sanitizeHtml(html: string): string {
  if (typeof html !== "string") return sanitizeHtml(String(html ?? ""));
  return DOMPurify.sanitize(html);
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
