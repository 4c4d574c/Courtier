<template>
  <!--
    Single v-html — the renderer embeds the trailing span inside the
    last block element, so the DOM tree has a stable shape across updates.
    No v-if, no split elements, no layout jump.

    Per-frame trailing-only growth (plain characters appended to the last
    block) never re-renders the v-html at all: the live `.streaming-trailing`
    span is written directly via textContent — no new HTML string, no
    innerHTML re-parse, no DOMPurify pass (see P2-7 in
    docs/architecture/webui-streaming-perf-plan.md).
  -->
  <div ref="bodyRef" class="markdown-body" v-html="fullHtml" @click="onBodyClick"></div>
</template>

<script setup lang="ts">
import { ref, watch, onUnmounted } from "vue";
import { sanitizeHtml } from "../utils/markdown";
import { createStreamingRenderer } from "../utils/streamingMarkdown";

interface Props {
  content: string;
  isStreaming: boolean;
}

const props = defineProps<Props>();

// Emitted when a `[[n]]` citation marker is clicked, with its 1-based index.
const emit = defineEmits<{ citation: [index: number] }>();

// Per-instance renderer with private parse cache.
const renderer = createStreamingRenderer();

const bodyRef = ref<HTMLElement>();
// What the v-html binding currently renders — only reassigned on structural
// changes (trailing-only growth patches the live span instead).
const fullHtml = ref("");

function fullRender(text: string): void {
  fullHtml.value = sanitizeHtml(
    renderer.renderStreamingHtml(text, props.isStreaming),
  );
}

// ---- citation link clicks (event delegation — v-html cannot bind handlers) ----
function onBodyClick(event: MouseEvent) {
  const target = (event.target as HTMLElement | null)?.closest?.(
    ".citation-link",
  );
  if (!target) return;
  const raw = target.getAttribute("data-citation");
  const index = raw ? Number.parseInt(raw, 10) : NaN;
  if (Number.isInteger(index) && index > 0) {
    emit("citation", index);
  }
}

// ---- rAF batching: at most one apply per frame ----
const batchedContent = ref(props.content);
let rafId: number | null = null;

function scheduleBatch() {
  if (rafId !== null) return;
  rafId = requestAnimationFrame(() => {
    rafId = null;
    batchedContent.value = props.content;
  });
}

function cancelBatch() {
  if (rafId !== null) {
    cancelAnimationFrame(rafId);
    rafId = null;
  }
}

function applyContent(text: string): void {
  if (props.isStreaming) {
    // Fast path: trailing-only growth patches the live span. The renderer's
    // cache reflects exactly what the DOM shows (full renders and successful
    // patches both advance it), so a null result reliably means structural
    // change or no trailing span yet → full render.
    const trailing = renderer.tryPatchTrailingText(text);
    if (trailing !== null) {
      const span = bodyRef.value?.querySelector<HTMLElement>(
        ".streaming-trailing",
      );
      if (span) {
        // textContent is inherently safe — no sanitize pass needed.
        span.textContent = trailing;
        return;
      }
    }
  }
  fullRender(text);
}

watch(
  () => props.content,
  () => {
    if (props.isStreaming) {
      scheduleBatch();
    } else {
      cancelBatch();
      batchedContent.value = props.content;
    }
  },
);

watch(
  () => props.isStreaming,
  (streaming) => {
    if (!streaming) {
      cancelBatch();
      batchedContent.value = props.content;
    }
  },
);

watch(batchedContent, (text) => applyContent(text));

// Initial render — the batched watch only fires on changes.
fullRender(props.content);

onUnmounted(cancelBatch);
</script>
