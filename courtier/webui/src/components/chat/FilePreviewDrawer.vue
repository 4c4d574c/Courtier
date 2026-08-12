<template>
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div v-if="isOpen" class="file-drawer-backdrop" @click="$emit('close')">
        <Transition name="drawer-slide">
          <div
            v-if="isOpen"
            class="file-drawer"
            role="dialog"
            aria-modal="true"
            :aria-labelledby="titleId"
            @click.stop
          >
            <div class="file-drawer-header">
              <h3 :id="titleId" class="file-drawer-title" :title="headerTitle">
                {{ headerTitle }}
              </h3>
              <div class="file-drawer-actions">
                <a
                  v-if="downloadUrl"
                  class="file-drawer-action"
                  :href="downloadUrl"
                  :download="downloadName"
                >
                  {{ MESSAGES.CHAT_PREVIEW_DOWNLOAD }}
                </a>
                <button
                  class="file-drawer-close"
                  type="button"
                  :aria-label="MESSAGES.CHAT_PREVIEW_CLOSE"
                  @click="$emit('close')"
                >
                  ×
                </button>
              </div>
            </div>

            <div class="file-drawer-body">
              <!-- Citation mode: highlight card → click to open full PDF -->
              <template v-if="citation">
                <div v-if="previewMode === 'card'" class="citation-view">
                  <button
                    type="button"
                    class="citation-card"
                    :disabled="loadingPdf"
                    @click="openPdf"
                  >
                    <div class="citation-card-title">{{ citation.title || "引用来源" }}</div>
                    <div class="citation-card-body">
                      <template v-if="citation.highlight?.length">
                        <p
                          v-for="(snippet, i) in citation.highlight"
                          :key="i"
                          class="citation-card-snippet"
                          v-html="snippetHtml(snippet)"
                        ></p>
                      </template>
                      <p v-else class="citation-card-snippet citation-card-plain">
                        {{ citation.chunkText || "（无引用片段）" }}
                      </p>
                    </div>
                  </button>
                  <p class="citation-hint">
                    {{ loadingPdf ? "正在加载完整文件…" : "点击卡片查看完整文件" }}
                  </p>
                </div>
                <iframe
                  v-else
                  :src="pdfUrl"
                  class="file-drawer-frame"
                  :title="citation.title || '文件预览'"
                ></iframe>
              </template>

              <!-- Uploaded-file mode (existing behavior) -->
              <template v-else-if="file">
                <img
                  v-if="isImage(file.mimeType)"
                  :src="file.url"
                  :alt="file.name"
                  class="file-drawer-image"
                />
                <iframe
                  v-else-if="isPdf(file.mimeType)"
                  :src="file.url"
                  class="file-drawer-frame"
                  :title="file.name"
                ></iframe>
                <div v-else class="file-drawer-unsupported">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="48" height="48">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                  </svg>
                  <p>{{ MESSAGES.CHAT_PREVIEW_UNSUPPORTED }}</p>
                  <a v-if="file?.url" class="file-drawer-download-link" :href="file.url" :download="file.name">
                    {{ MESSAGES.CHAT_PREVIEW_DOWNLOAD }}
                  </a>
                </div>
              </template>
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { api } from "../../api/client";
import type { CitationHit } from "../../types/agent";
import type { ChatFileItem } from "../../types/chat";
import { MESSAGES } from "../../constants/messages";
import { sanitizeHtml } from "../../utils/markdown";

const titleId = "file-drawer-title";

interface Props {
  isOpen: boolean;
  file: ChatFileItem | null;
  /** When set, the drawer shows the citation card → full PDF preview flow. */
  citation?: CitationHit | null;
}

const props = defineProps<Props>();
const emit = defineEmits<{ close: [] }>();

// Citation preview state: card first, full PDF after the card is clicked.
const previewMode = ref<"card" | "pdf">("card");
const pdfUrl = ref("");
const originalUrl = ref<string | null>(null);
const loadingPdf = ref(false);
const pdfError = ref("");

watch(
  () => props.citation,
  () => {
    previewMode.value = "card";
    pdfUrl.value = "";
    originalUrl.value = null;
    loadingPdf.value = false;
    pdfError.value = "";
  },
);

const headerTitle = computed(() => {
  if (props.citation) return props.citation.title || "引用来源";
  return props.file?.name ?? "";
});

const downloadUrl = computed(() => {
  if (props.citation) return originalUrl.value ?? (pdfUrl.value || "");
  return props.file?.url ?? "";
});

const downloadName = computed(() => {
  if (props.citation) return props.citation.title || "引用来源";
  return props.file?.name ?? "";
});

function isImage(mime: string): boolean {
  return mime.startsWith("image/");
}

function isPdf(mime: string): boolean {
  return mime === "application/pdf";
}

/**
 * Render an ES highlight snippet with `<em>` emphasis converted to `<mark>`
 * (sanitized — the snippets originate from indexed document text).
 */
function snippetHtml(snippet: string): string {
  const converted = snippet
    .replace(/<em>/g, "<mark>")
    .replace(/<\/em>/g, "</mark>");
  return sanitizeHtml(converted);
}

async function openPdf() {
  if (!props.citation) return;
  const resourceId = props.citation.resourceId;
  if (resourceId == null) {
    pdfError.value = "该引用无关联的资源文件";
    return;
  }
  loadingPdf.value = true;
  pdfError.value = "";
  try {
    const result = await api.getResourcePdfUrl(resourceId);
    pdfUrl.value = result.url;
    originalUrl.value = result.originalUrl ?? null;
    previewMode.value = "pdf";
  } catch (err: unknown) {
    pdfError.value = err instanceof Error ? err.message : "加载文件失败";
  } finally {
    loadingPdf.value = false;
  }
}

watch(
  () => props.isOpen,
  (open) => {
    document.body.style.overflow = open ? "hidden" : "";
  },
  { immediate: true },
);

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape" && props.isOpen) {
    emit("close");
  }
}

onMounted(() => window.addEventListener("keydown", onKeydown));
onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  document.body.style.overflow = "";
});
</script>

<style scoped>
/* Drawer shell + uploaded-file modes — unchanged from the original component */
.file-drawer-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.4);
  z-index: 200;
  display: flex;
  justify-content: flex-end;
}

.file-drawer {
  width: var(--chat-drawer-width);
  max-width: 100%;
  height: 100%;
  background: var(--chat-bg-card);
  display: flex;
  flex-direction: column;
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.12);
}

.file-drawer-header {
  flex-shrink: 0;
  height: 56px;
  padding: 0 16px;
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.file-drawer-title {
  flex: 1;
  font-size: 16px;
  font-weight: 600;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 0;
}

.file-drawer-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.file-drawer-action {
  font-size: 15px;
  color: var(--chat-accent);
  text-decoration: none;
}

.file-drawer-action:hover {
  text-decoration: underline;
}

.file-drawer-close {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  font-size: 26px;
  line-height: 1;
  cursor: pointer;
  border-radius: var(--chat-radius-md);
}

.file-drawer-close:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.file-drawer-body {
  flex: 1;
  overflow: auto;
  padding: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.file-drawer-image {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: var(--chat-radius-md);
}

.file-drawer-frame {
  width: 100%;
  height: 100%;
  border: none;
  border-radius: var(--chat-radius-md);
}

.file-drawer-unsupported {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  text-align: center;
  color: var(--chat-text-secondary);
}

.file-drawer-download-link {
  padding: 8px 16px;
  border-radius: var(--chat-radius-md);
  background: var(--chat-accent);
  color: #fff;
  text-decoration: none;
  font-size: 15px;
}

/* Citation mode */
.citation-view {
  width: 100%;
  max-width: 560px;
  margin: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.citation-card {
  display: block;
  width: 100%;
  text-align: left;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md, 8px);
  background: var(--chat-bg-surface);
  overflow: hidden;
  cursor: pointer;
  padding: 0;
  transition: box-shadow 0.15s ease, border-color 0.15s ease;
}

.citation-card:hover:not(:disabled) {
  border-color: var(--chat-accent);
  box-shadow: var(--chat-shadow-md, 0 2px 12px rgba(0, 0, 0, 0.12));
}

.citation-card:disabled {
  opacity: 0.6;
  cursor: default;
}

.citation-card-title {
  padding: 10px 14px;
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
  background: var(--chat-bg-subtle, rgba(0, 0, 0, 0.03));
  border-bottom: 1px solid var(--chat-border);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.citation-card-body {
  padding: 12px 14px;
}

.citation-card-snippet {
  margin: 0 0 8px;
  font-size: 14px;
  line-height: 1.7;
  color: var(--chat-text-secondary);
}

.citation-card-snippet:last-child {
  margin-bottom: 0;
}

.citation-card-snippet :deep(mark) {
  background: var(--chat-highlight, #ffe58f);
  color: inherit;
  border-radius: 2px;
  padding: 0 1px;
}

.citation-card-plain {
  color: var(--chat-text-tertiary);
}

.citation-hint {
  margin: 0;
  font-size: 12px;
  text-align: center;
  color: var(--chat-text-tertiary);
}

.drawer-fade-enter-active,
.drawer-fade-leave-active {
  transition: opacity 0.2s ease;
}

.drawer-fade-enter-from,
.drawer-fade-leave-to {
  opacity: 0;
}

.drawer-slide-enter-active,
.drawer-slide-leave-active {
  transition: transform 0.25s ease;
}

.drawer-slide-enter-from,
.drawer-slide-leave-to {
  transform: translateX(100%);
}

@media (max-width: 768px) {
  .file-drawer {
    width: 100%;
  }
}
</style>
