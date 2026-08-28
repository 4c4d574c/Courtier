<template>
  <div class="tool-card-detail">
    <div v-if="tool.detail?.type === 'structured'" class="structured-data">
      <StructuredData :data="tool.detail.data" />
    </div>
    <div
      v-else-if="tool.detail?.type === 'markdown' && tool.status === 'running'"
      class="markdown-content"
    >
      <StreamingMarkdown :content="tool.detail.content" :is-streaming="true" />
    </div>
    <div
      v-else-if="tool.detail?.type === 'markdown'"
      class="markdown-content"
      v-html="renderMarkdown(tool.detail.content)"
    ></div>
    <div v-else class="structured-data">
      <div class="sd-row">
        <span class="sd-key">状态:</span>
        <span class="sd-val">{{ statusLabel }}</span>
      </div>
      <div v-if="tool.summary && tool.status === 'error'" class="sd-error">
        <div
          :class="[
            'sd-error-text',
            {
              'sd-error-text--clamped': isLongError && !errorExpanded,
              'sd-error-text--scroll': isLongError && errorExpanded,
            },
          ]"
        >
          {{ tool.summary }}
        </div>
        <button
          v-if="isLongError"
          type="button"
          class="sd-error-toggle"
          @click="errorExpanded = !errorExpanded"
        >
          {{ errorExpanded ? "收起" : "查看全文" }}
          <AppIcon :name="errorExpanded ? 'chevron-up' : 'chevron-down'" :size="12" />
        </button>
      </div>
      <div v-else-if="tool.summary" class="sd-row">
        <span class="sd-key">结果:</span>
        <span class="sd-val">{{ tool.summary }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from "./AppIcon.vue";
import { computed, ref } from "vue";
import type { ToolResult } from "../types/agent";
import StreamingMarkdown from "./StreamingMarkdown.vue";
import StructuredData from "./StructuredData.vue";
import { renderMarkdown } from "../utils/markdown";
import { STATUS_LABELS } from "../constants/messages";

interface Props {
  tool: ToolResult;
}

const props = defineProps<Props>();

const statusLabel = computed(
  () => STATUS_LABELS[props.tool.status] ?? props.tool.status,
);

// Error summaries carry the full exception text; clamp long ones by default.
const errorExpanded = ref(false);
const isLongError = computed(
  () => (props.tool.summary?.length ?? 0) > 160,
);
</script>
