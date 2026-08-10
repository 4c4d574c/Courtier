<template>
  <div class="input-area">
    <div class="input-card">
      <div v-if="error || fileError" class="input-area-error">
        {{ error || fileError }}
      </div>
      <div v-if="fileName" class="input-area-file">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
          <polyline points="14 2 14 8 20 8" />
        </svg>
        <span>{{ fileName }}</span>
        <button class="input-area-file-remove" type="button" @click="clearFile">×</button>
      </div>

      <textarea
        ref="textareaRef"
        v-model="task"
        class="input-area-textarea"
        :placeholder="MESSAGES.CHAT_PLACEHOLDER"
        rows="1"
        @input="autoResize"
        @keydown.enter="handleEnter"
      />

      <div class="input-card-toolbar">
        <input
          ref="fileInput"
          type="file"
          :accept="ALLOWED_EXTS"
          style="display: none"
          @change="handleFileChange"
        />
        <button
          class="input-area-attach"
          type="button"
          :title="MESSAGES.CHAT_ATTACH"
          @click="fileInput?.click()"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
        </button>
        <div class="input-card-toolbar-right">
          <span v-if="modelName" class="input-area-model">{{ MESSAGES.CHAT_MODEL }}: {{ modelName }}</span>
          <button
            class="input-area-send"
            type="button"
            :aria-label="isRunning ? MESSAGES.CHAT_STOP : MESSAGES.CHAT_SEND"
            :disabled="!isRunning && (!canSubmit || uploading)"
            @click="handleClick"
          >
            <svg v-if="isRunning" viewBox="0 0 24 24" fill="currentColor">
              <rect x="4" y="4" width="16" height="16" rx="2" />
            </svg>
            <svg v-else-if="!uploading" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="19" x2="12" y2="5" />
              <polyline points="5 12 12 5 19 12" />
            </svg>
            <span v-else class="input-area-spinner"></span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick } from "vue";
import { ALLOWED_EXTS, ALLOWED_EXTENSIONS, MAX_FILE_SIZE } from "../../constants/fileUpload";
import { MESSAGES } from "../../constants/messages";

interface Props {
  modelName?: string;
  uploading?: boolean;
  error?: string;
  isRunning?: boolean;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  submit: [task: string, file?: File];
  stop: [];
}>();

const task = ref("");
const fileError = ref("");
const fileName = ref("");
const selectedFile = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const textareaRef = ref<HTMLTextAreaElement | null>(null);

const canSubmit = computed(() => task.value.trim().length > 0);

function autoResize() {
  const el = textareaRef.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 120) + "px";
}

function handleEnter(event: KeyboardEvent) {
  if (event.shiftKey || event.isComposing) return;
  event.preventDefault();
  handleClick();
}

function handleClick() {
  if (props.isRunning) {
    emit("stop");
    return;
  }
  if (!canSubmit.value) return;
  emit("submit", task.value.trim(), selectedFile.value || undefined);
  task.value = "";
  clearFile();
  nextTick(() => {
    const el = textareaRef.value;
    if (el) el.style.height = "auto";
  });
}

function handleFileChange(event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (!file) return;
  // The accept attribute is only a picker hint — enforce the whitelist here.
  const ext = `.${file.name.split(".").pop()?.toLowerCase() ?? ""}`;
  if (!ALLOWED_EXTENSIONS.includes(ext)) {
    fileError.value = `不支持的文件类型：${ext}`;
    target.value = "";
    return;
  }
  if (file.size > MAX_FILE_SIZE) {
    fileError.value = MESSAGES.FILE_TOO_LARGE((file.size / 1024 / 1024).toFixed(1));
    target.value = "";
    return;
  }
  fileName.value = file.name;
  selectedFile.value = file;
  fileError.value = "";
  target.value = "";
}

function clearFile() {
  fileName.value = "";
  selectedFile.value = null;
  fileError.value = "";
}
</script>

<style scoped>
.input-area {
  flex-shrink: 0;
  padding: 12px 16px 16px;
  display: flex;
  justify-content: center;
}

/* Elevated card: textarea on top, toolbar row at the bottom. Slightly wider
   than the message column (800px), still centered. */
.input-card {
  width: min(880px, 100%);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-lg);
  padding: 14px 16px 10px;
  background: var(--chat-bg-card);
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.06);
}

.input-area-error {
  margin-bottom: 8px;
  padding: 8px 12px;
  border-radius: var(--chat-radius-md);
  background: rgba(239, 68, 68, 0.1);
  color: #ef4444;
  font-size: 15px;
}

.input-area-file {
  display: inline-flex;
  align-self: flex-start;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  padding: 6px 10px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-hover);
  font-size: 15px;
  color: var(--chat-text-secondary);
}

.input-area-file-remove {
  border: none;
  background: transparent;
  color: var(--chat-text-tertiary);
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
}

.input-area-file-remove:hover {
  color: #ef4444;
}

.input-area-textarea {
  width: 100%;
  border: none;
  background: transparent;
  resize: none;
  outline: none;
  font-size: 16px;
  line-height: 1.5;
  color: var(--chat-text-primary);
  min-height: 72px;
  max-height: 120px;
  padding: 2px 4px;
}

.input-area-textarea::placeholder {
  color: var(--chat-text-tertiary);
}

.input-card-toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
}

.input-area-attach,
.input-area-send {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  flex-shrink: 0;
}

.input-area-attach {
  background: transparent;
  color: var(--chat-text-secondary);
}

.input-area-attach:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.input-area-attach svg {
  width: 20px;
  height: 20px;
}

.input-area-model {
  font-size: 13px;
  color: var(--chat-text-tertiary);
  flex-shrink: 0;
  display: none;
}

@media (min-width: 640px) {
  .input-area-model {
    display: inline;
  }
}

/* Right cluster: model label immediately left of the send button. */
.input-card-toolbar-right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.input-area-send {
  background: var(--chat-accent);
  color: var(--chat-accent-contrast);
  border-radius: 50%;
}

.input-area-send:hover:not(:disabled) {
  background: var(--chat-accent-hover);
}

.input-area-send:disabled {
  background: var(--chat-bg-hover);
  color: var(--chat-text-tertiary);
  cursor: not-allowed;
}

.input-area-send svg {
  width: 18px;
  height: 18px;
}

.input-area-spinner {
  width: 16px;
  height: 16px;
  border: 2px solid color-mix(in srgb, var(--chat-accent-contrast) 30%, transparent);
  border-top-color: var(--chat-accent-contrast);
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
