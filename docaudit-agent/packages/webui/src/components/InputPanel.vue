<template>
  <div class="input-section">
    <div v-if="error || fileError" class="input-error">
      {{ error || fileError }}
    </div>
    <div v-if="fileName" class="input-file-tag">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        width="12"
        height="12"
      >
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
      <span>{{ fileName }}</span>
      <span class="input-file-remove" @click="clearFile">×</span>
    </div>
    <div class="input-bar">
      <input
        ref="fileInput"
        type="file"
        accept=".pdf,.docx,.bmp,.jpg,.jpeg,.png,.gif,.tif,.tiff"
        style="display: none"
        @change="handleFileChange"
      />
      <button
        class="input-attach-btn"
        type="button"
        :aria-label="MESSAGES.UPLOAD_LABEL"
        @click="triggerFileUpload"
        :title="MESSAGES.UPLOAD_LABEL"
      >
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <line x1="12" y1="5" x2="12" y2="19" />
          <line x1="5" y1="12" x2="19" y2="12" />
        </svg>
      </button>
      <textarea
        ref="textareaRef"
        class="input-textarea"
        v-model="task"
        placeholder="输入审核任务描述，例如：审核这份通知的格式规范"
        rows="1"
        @input="autoResize"
        @keydown.enter.prevent="handleEnter"
      />
      <button
        class="input-send-btn"
        type="button"
        :aria-label="
          isRunning
            ? MESSAGES.STOP_LABEL
            : uploading
              ? MESSAGES.UPLOADING_LABEL
              : MESSAGES.SEND_LABEL
        "
        :disabled="!isRunning && (!canSubmit || uploading)"
        @click="handleClick"
        :title="
          isRunning
            ? MESSAGES.STOP_LABEL
            : uploading
              ? MESSAGES.UPLOADING_LABEL
              : MESSAGES.SEND_LABEL
        "
        :class="{ 'input-send-btn--running': isRunning }"
      >
        <svg
          v-if="isRunning"
          viewBox="0 0 24 24"
          fill="currentColor"
          stroke="none"
        >
          <rect x="4" y="4" width="16" height="16" rx="2" />
        </svg>
        <svg
          v-else-if="!uploading"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
        >
          <line x1="22" y1="2" x2="11" y2="13" />
          <polygon points="22 2 15 22 11 13 2 9 22 2" />
        </svg>
        <span v-else class="upload-spinner"></span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, nextTick } from "vue";
import { MESSAGES } from "../constants/messages";

const props = defineProps<{
  uploading?: boolean;
  error?: string;
  isRunning?: boolean;
}>();

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

function handleClick() {
  if (props.isRunning) {
    emit("stop");
    return;
  }
  handleSubmit();
}

function handleEnter(event: KeyboardEvent) {
  if (event.shiftKey) {
    return;
  }
  handleClick();
}

function handleSubmit() {
  if (!canSubmit.value) return;
  if (selectedFile.value) {
    emit("submit", task.value.trim(), selectedFile.value);
  } else {
    emit("submit", task.value.trim());
  }
  task.value = "";
  fileName.value = "";
  selectedFile.value = null;
  nextTick(() => {
    const el = textareaRef.value;
    if (el) {
      el.style.height = "auto";
    }
  });
}

function triggerFileUpload() {
  fileInput.value?.click();
}

const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50 MB

function handleFileChange(event: Event) {
  const target = event.target as HTMLInputElement;
  const file = target.files?.[0];
  if (file) {
    if (file.size > MAX_FILE_SIZE) {
      fileError.value = MESSAGES.FILE_TOO_LARGE(
        (file.size / 1024 / 1024).toFixed(1),
      );
      target.value = "";
      return;
    }
    fileName.value = file.name;
    selectedFile.value = file;
  }
  if (target) {
    target.value = "";
  }
}

function clearFile() {
  fileName.value = "";
  selectedFile.value = null;
  fileError.value = "";
}
</script>
