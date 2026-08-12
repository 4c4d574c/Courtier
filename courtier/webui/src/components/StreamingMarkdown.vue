<template>
  <!--
    Single v-html — the renderer embeds the trailing span inside the
    last block element, so the DOM tree has a stable shape across updates.
    No v-if, no split elements, no layout jump.
  -->
  <div class="markdown-body" v-html="sanitizedHtml" @click="onBodyClick"></div>
</template>

<script setup lang="ts">
import { computed, ref, watch, onUnmounted } from "vue";
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

// ---- rAF batching: at most one re-parse per frame ----
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

onUnmounted(cancelBatch);

// ---- single-pass: render + sanitize ----
const sanitizedHtml = computed(() => {
  const rawHtml = renderer.renderStreamingHtml(
    batchedContent.value,
    props.isStreaming,
  );
  return sanitizeHtml(rawHtml);
});
</script>
