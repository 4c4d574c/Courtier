<template>
  <div class="user-message">
    <div v-if="!editing" class="user-message-body">
      <div class="user-message-bubble">
        <div class="user-message-text">{{ content }}</div>
      </div>
      <div class="user-message-actions">
        <button
          type="button"
          class="user-message-action"
          @click="copyContent"
        >
          {{ copied ? MESSAGES.CHAT_COPIED : MESSAGES.CHAT_COPY }}
        </button>
        <button
          v-if="canEdit"
          type="button"
          class="user-message-action"
          :disabled="!!editHint"
          :title="editHint || undefined"
          @click="startEdit"
        >
          {{ MESSAGES.CHAT_EDIT }}
        </button>
      </div>
    </div>

    <div v-else class="user-message-editor">
      <textarea
        ref="textareaRef"
        v-model="draft"
        class="user-message-textarea"
        rows="1"
        @input="autoResize"
        @keydown.enter="handleEnter"
        @keydown.esc.prevent="cancelEdit"
      ></textarea>
      <div class="user-message-editor-actions">
        <button
          type="button"
          class="user-message-editor-btn"
          @click="cancelEdit"
        >
          {{ MESSAGES.CHAT_EDIT_CANCEL }}
        </button>
        <button
          type="button"
          class="user-message-editor-btn user-message-editor-btn--primary"
          :disabled="!canSubmitEdit"
          @click="submitEdit"
        >
          {{ MESSAGES.CHAT_EDIT_CONFIRM }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import { MESSAGES } from "../../constants/messages";

interface Props {
  content: string;
  turnIndex: number;
  /** False while the session is running — hides the edit entry. */
  canEdit?: boolean;
  /** Non-empty disables editing with the reason shown as tooltip. */
  editHint?: string;
}

const props = withDefaults(defineProps<Props>(), {
  canEdit: false,
  editHint: "",
});

const emit = defineEmits<{
  "edit-submit": [payload: { turnIndex: number; text: string }];
}>();

// ---- copy ----

const copied = ref(false);
let copiedTimer: ReturnType<typeof setTimeout> | undefined;

async function copyContent() {
  try {
    await navigator.clipboard.writeText(props.content);
  } catch {
    // Clipboard API needs a secure context; fall back for plain-http LAN use.
    const el = document.createElement("textarea");
    el.value = props.content;
    el.style.position = "fixed";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    document.execCommand("copy");
    document.body.removeChild(el);
  }
  copied.value = true;
  clearTimeout(copiedTimer);
  copiedTimer = setTimeout(() => {
    copied.value = false;
  }, 1500);
}

// ---- inline edit ----

const editing = ref(false);
const draft = ref("");
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const canSubmitEdit = computed(() => {
  const text = draft.value.trim();
  return text.length > 0 && text !== props.content.trim();
});

function startEdit() {
  if (!props.canEdit || props.editHint) return;
  draft.value = props.content;
  editing.value = true;
  nextTick(() => {
    autoResize();
    const el = textareaRef.value;
    if (el) {
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }
  });
}

function cancelEdit() {
  editing.value = false;
  draft.value = "";
}

function submitEdit() {
  if (!canSubmitEdit.value) return;
  emit("edit-submit", { turnIndex: props.turnIndex, text: draft.value.trim() });
  editing.value = false;
  draft.value = "";
}

function autoResize() {
  const el = textareaRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 200) + "px";
}

function handleEnter(event: KeyboardEvent) {
  if (event.shiftKey || event.isComposing) return;
  event.preventDefault();
  submitEdit();
}
</script>

<style scoped>
.user-message {
  display: flex;
  justify-content: flex-end;
}

.user-message-body {
  max-width: 80%;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.user-message-bubble {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
  border-radius: var(--chat-radius-lg);
  padding: 12px 16px;
  font-size: 16px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Hover-revealed action row (ChatSidebar meatballs pattern).  The row is
   always rendered so the layout never shifts; it only becomes visible and
   clickable on hover. */
.user-message-actions {
  display: flex;
  gap: 4px;
  margin-top: 2px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s;
}

.user-message-body:hover .user-message-actions,
.user-message-actions:focus-within {
  opacity: 1;
  pointer-events: auto;
}

@media (hover: none) {
  .user-message-actions {
    opacity: 1;
    pointer-events: auto;
  }
}

.user-message-action {
  border: none;
  background: none;
  padding: 2px 6px;
  font-size: 13px;
  color: var(--chat-text-tertiary);
  cursor: pointer;
  border-radius: var(--chat-radius-md, 6px);
  transition: color 0.15s;
}

.user-message-action:hover:not(:disabled) {
  color: var(--chat-text-primary);
}

.user-message-action:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.user-message-editor {
  width: 100%;
  background: var(--chat-bg-hover);
  border-radius: var(--chat-radius-lg);
  padding: 10px 12px 8px;
}

.user-message-textarea {
  width: 100%;
  border: none;
  background: transparent;
  resize: none;
  outline: none;
  font-size: 16px;
  line-height: 1.6;
  color: var(--chat-text-primary);
  min-height: 28px;
  max-height: 200px;
  padding: 2px 4px;
  font-family: inherit;
}

.user-message-editor-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 6px;
}

.user-message-editor-btn {
  border: none;
  background: none;
  padding: 4px 10px;
  font-size: 14px;
  color: var(--chat-text-secondary);
  cursor: pointer;
  border-radius: var(--chat-radius-md, 6px);
  transition:
    color 0.15s,
    background 0.15s;
}

.user-message-editor-btn:hover {
  color: var(--chat-text-primary);
}

.user-message-editor-btn--primary {
  background: var(--chat-accent);
  color: var(--chat-accent-contrast, #fff);
}

.user-message-editor-btn--primary:hover:not(:disabled) {
  color: var(--chat-accent-contrast, #fff);
  filter: brightness(1.08);
}

.user-message-editor-btn--primary:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
