import { Marked, type Token } from "marked";

function escapeHtml(raw: string): string {
  if (typeof raw !== "string") return escapeHtml(String(raw ?? ""));
  return raw
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function unescapeHtml(escaped: string): string {
  if (typeof escaped !== "string") return unescapeHtml(String(escaped ?? ""));
  return escaped
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'");
}

const streamingMarked = new Marked();
const renderer = new streamingMarked.Renderer();
renderer.html = (token) => escapeHtml(token.text);
streamingMarked.use({ renderer });

// Block types whose *inline* tokens can be partially rendered.
const INLINE_CONTAINER_TYPES = new Set(["paragraph", "heading", "tablecell"]);

function hasInlineTokens(token: Token): token is Token & { tokens: Token[] } {
  const inlineTokens = (token as { tokens?: unknown }).tokens;
  return (
    INLINE_CONTAINER_TYPES.has(token.type) &&
    Array.isArray(inlineTokens) &&
    inlineTokens.length > 0
  );
}

// ---------------------------------------------------------------------------
// Cache — per renderer instance
// ---------------------------------------------------------------------------
interface ParseCache {
  /** Full source text last parsed. */
  text: string;
  /** Full rendered HTML (trailing span embedded in last block). */
  html: string;
  /** Offset in `text` where trailing portion starts. */
  trailingStart: number;
}

const STRUCTURAL_RE = /[`*_~[\]\\\n]/;

// ---------------------------------------------------------------------------
// Trailing span embedding
// ---------------------------------------------------------------------------
const TRAILING_OPEN = '<span class="streaming-trailing">';
const TRAILING_CLOSE = "</span>";

/**
 * Replace the text content of the LAST `<span class="streaming-trailing">`
 * in `oldHtml`.  The new trailing text is `newSource.slice(trailingStart)`.
 */
function patchTrailingSpan(
  oldHtml: string,
  newSource: string,
  trailingStart: number,
): string {
  const newTrailing = escapeHtml(newSource.slice(trailingStart));
  const openIdx = oldHtml.lastIndexOf(TRAILING_OPEN);
  if (openIdx === -1) return oldHtml;
  const contentStart = openIdx + TRAILING_OPEN.length;
  const closeIdx = oldHtml.indexOf(TRAILING_CLOSE, contentStart);
  if (closeIdx === -1) return oldHtml;
  return oldHtml.slice(0, contentStart) + newTrailing + oldHtml.slice(closeIdx);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export interface StreamingParts {
  completeHtml: string;
  trailingText: string;
}

/**
 * Create a streaming markdown renderer with private parse cache.
 */
export function createStreamingRenderer() {
  let cache: ParseCache | null = null;

  /**
   * Render to a single HTML string.  The trailing text is embedded as
   * `<span class="streaming-trailing">` *inside* the last block element,
   * so the DOM structure never changes — no v-if, no element appear/disappear.
   */
  function renderStreamingHtml(text: string, isStreaming: boolean): string {
    if (typeof text !== "string") text = String(text ?? "");
    if (!text) {
      cache = null;
      return "";
    }

    // ---- cache hit (streaming only) ----
    if (isStreaming && cache) {
      if (text === cache.text) return cache.html;

      if (text.startsWith(cache.text)) {
        const newChars = text.slice(cache.text.length);
        if (!STRUCTURAL_RE.test(newChars)) {
          // Only trailing text grew.  If the cached HTML has a trailing span,
          // patch it in-place.  Otherwise fall through to full re-parse
          // (needed when the previous state had no trailing text at all).
          if (cache.html.includes(TRAILING_OPEN)) {
            const html = patchTrailingSpan(
              cache.html,
              text,
              cache.trailingStart,
            );
            cache = { text, html, trailingStart: cache.trailingStart };
            return html;
          }
        }
      }
    }

    // ---- full re-parse ----
    const tokens = streamingMarked.lexer(text);
    if (tokens.length === 0) {
      cache = null;
      return "";
    }

    if (!isStreaming) {
      const html = streamingMarked.parser(tokens);
      cache = null;
      return html;
    }

    // ---- streaming: all blocks except last are complete.
    //      For the last block, render complete inline tokens and
    //      embed the trailing span inside the block. ----
    const completeBlocks = tokens.slice(0, -1);
    const lastBlock = tokens[tokens.length - 1];
    let completeHtml =
      completeBlocks.length > 0 ? streamingMarked.parser(completeBlocks) : "";
    let trailingText = "";

    if (hasInlineTokens(lastBlock)) {
      const inlineTokens = lastBlock.tokens;
      const lastInline = inlineTokens[inlineTokens.length - 1];

      if (lastInline.type === "text") {
        const completeInline = inlineTokens.slice(0, -1);
        trailingText = lastInline.raw;

        if (completeInline.length > 0) {
          // Render complete inline tokens, then embed trailing span before
          // the last block's closing tag.
          const partialBlock = { ...lastBlock, tokens: completeInline };
          let blockHtml = streamingMarked.parser([partialBlock]);
          blockHtml = embedBeforeLastClose(
            blockHtml,
            TRAILING_OPEN + escapeHtml(trailingText) + TRAILING_CLOSE,
          );
          completeHtml += blockHtml;
        } else {
          // No complete inline tokens — entire block is trailing.
          // Wrap in a paragraph so it participates in normal flow.
          // (When the block later completes, the parser will replace this.)
          completeHtml += `<p>${TRAILING_OPEN}${escapeHtml(trailingText)}${TRAILING_CLOSE}</p>`;
        }
      } else {
        // Last inline is structural (strong, em, etc.) — block fully complete.
        completeHtml += streamingMarked.parser([lastBlock]);
      }
    } else {
      // Non-inline block — trailing as raw text.
      trailingText = lastBlock.raw ?? "";
      if (trailingText) {
        completeHtml += `<p>${TRAILING_OPEN}${escapeHtml(trailingText)}${TRAILING_CLOSE}</p>`;
      }
    }

    const trailingStart = text.length - trailingText.length;
    const html = completeHtml;
    cache = { text, html, trailingStart };
    return html;
  }

  return { renderStreamingHtml };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Insert `insertion` right before the LAST closing block tag in `html`.
 * e.g. `<p>hello</p>` → `<p>hello<span>...</span></p>`
 */
function embedBeforeLastClose(html: string, insertion: string): string {
  const m = html.match(/<\/[a-z][a-z0-9]*>\s*$/);
  if (m) {
    const tag = m[0].trimEnd();
    const pos = html.lastIndexOf(tag);
    return html.slice(0, pos) + insertion + html.slice(pos);
  }
  return html + insertion;
}

// ---------------------------------------------------------------------------
// Legacy — keep existing APIs for tests and non-component callers.
// ---------------------------------------------------------------------------

export function renderStreamingParts(
  text: string,
  isStreaming: boolean,
): StreamingParts {
  const r = createStreamingRenderer();
  const html = r.renderStreamingHtml(text, isStreaming);
  const openIdx = html.lastIndexOf(TRAILING_OPEN);
  if (openIdx === -1) return { completeHtml: html, trailingText: "" };
  const contentStart = openIdx + TRAILING_OPEN.length;
  const closeIdx = html.indexOf(TRAILING_CLOSE, contentStart);
  if (closeIdx === -1) return { completeHtml: html, trailingText: "" };
  const trailingText = unescapeHtml(html.slice(contentStart, closeIdx));
  const completeHtml =
    html.slice(0, openIdx) + html.slice(closeIdx + TRAILING_CLOSE.length);
  return { completeHtml, trailingText };
}

function wrapTrailing(text: string, isStreaming: boolean): string {
  if (!text) return "";
  if (!isStreaming) return escapeHtml(text);
  return `${TRAILING_OPEN}${escapeHtml(text)}${TRAILING_CLOSE}`;
}

export function renderStreamingMarkdown(
  text: string,
  options: { isStreaming?: boolean } = {},
): string {
  const { completeHtml, trailingText } = renderStreamingParts(
    text,
    options.isStreaming ?? false,
  );
  return (
    completeHtml + wrapTrailing(trailingText, options.isStreaming ?? false)
  );
}
