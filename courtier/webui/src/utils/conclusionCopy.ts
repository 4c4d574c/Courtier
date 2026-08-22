import type { CitationHit } from "../types/agent";
import type { CitationIndex } from "../types/chat";
import { MESSAGES } from "../constants/messages";
import { renderMarkdown } from "./markdown";

export interface ConclusionCopyPayload {
  /** 纯文本层：规范化后的 Markdown 源文本（含来源附录） */
  text: string;
  /** 富文本层：同一段内容的消毒 HTML（粘贴 Word/邮件保留格式） */
  html: string;
}

/** 正文引用标记 [[n]] 在复制时规范为可见编号 [n]。 */
const CITATION_MARKER = /\[\[(\d+)\]\]/g;

/**
 * 把最终结论组装成剪贴板双格式负载。纯函数：标记规范化 + 来源附录，
 * HTML 复用应用统一的 marked + DOMPurify 渲染管线。
 */
export function buildConclusionCopy(
  content: string,
  citations?: CitationIndex,
): ConclusionCopyPayload {
  const body = content.replace(CITATION_MARKER, "[$1]");
  const text = body + sourcesAppendix(citations);
  return { text, html: renderMarkdown(text) };
}

/**
 * 来源附录按真实引用编号呈现（citationOffset 累计编号可能跳号），
 * 不重新连续计数——否则正文 [n] 与附录对不上。
 */
function sourcesAppendix(citations?: CitationIndex): string {
  if (!citations || citations.list.length === 0) return "";
  const hits = numberedHits(citations);
  if (!hits.length) return "";
  const lines = hits.map(
    ([n, hit]) => `- [${n}] ${sourceLabel(hit)}`,
  );
  return `\n\n**${MESSAGES.CHAT_CONCLUSION_SOURCES}**\n${lines.join("\n")}`;
}

function numberedHits(citations: CitationIndex): Array<[number, CitationHit]> {
  const numbers = [...citations.byNumber.keys()].sort((a, b) => a - b);
  if (numbers.length > 0) {
    return numbers.flatMap((n) => {
      const hit = citations.byNumber.get(n);
      return hit ? [[n, hit] as [number, CitationHit]] : [];
    });
  }
  // 老事件没有 citationOffset 时 byNumber 为空，回退合并顺序编号。
  return citations.list.map((hit, i) => [i + 1, hit]);
}

function sourceLabel(hit: CitationHit): string {
  const title = hit.title?.trim();
  if (title) {
    const docType = hit.docType?.trim();
    return docType ? `${title}（${docType}）` : title;
  }
  const id = hit.documentId ?? hit.resourceId;
  return id != null ? `文档 #${id}` : MESSAGES.CITATION_UNTITLED;
}

/**
 * 写入剪贴板，优先双格式（富文本目标拿 html，纯文本目标拿 text）。
 * 返回是否成功写入 html 层；plain-http 内网等非 secure context 场景
 * 逐级降级到纯文本（与 UserMessage 的降级约束一致）。
 */
export async function writeRichClipboard(
  payload: ConclusionCopyPayload,
): Promise<boolean> {
  if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    try {
      await navigator.clipboard.write([
        new ClipboardItem({
          "text/html": new Blob([payload.html], { type: "text/html" }),
          "text/plain": new Blob([payload.text], { type: "text/plain" }),
        }),
      ]);
      return true;
    } catch {
      // 权限拒绝/非 secure context 等，落到纯文本路径。
    }
  }
  try {
    await navigator.clipboard.writeText(payload.text);
    return false;
  } catch {
    copyViaExecCommand(payload.text);
    return false;
  }
}

function copyViaExecCommand(text: string): void {
  const el = document.createElement("textarea");
  el.value = text;
  el.style.position = "fixed";
  el.style.opacity = "0";
  document.body.appendChild(el);
  el.select();
  document.execCommand("copy");
  document.body.removeChild(el);
}
