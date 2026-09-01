<template>
  <div class="tool-card-detail">
    <div v-if="tool.detail?.type === 'structured'" class="structured-data">
      <StructuredData :data="tool.detail.data" />
    </div>
    <div
      v-else-if="tool.detail?.type === 'markdown' && tool.status === 'running'"
      :class="['markdown-content', { 'tool-card-preview--clamped': isLongMarkdown }]"
    >
      <StreamingMarkdown :content="tool.detail.content" :is-streaming="true" />
    </div>
    <div
      v-else-if="tool.detail?.type === 'markdown'"
      :class="['markdown-content', { 'tool-card-preview--clamped': isLongMarkdown }]"
      v-html="renderMarkdown(tool.detail.content)"
    ></div>
    <div v-else class="structured-data">
      <!-- 状态不在此重复：标题栏徽标已表达。 -->
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
          @click="toggleErrorExpanded()"
        >
          {{ errorExpanded ? "收起" : "查看全文" }}
          <DisclosureChevron :open="errorExpanded" :size="14" />
        </button>
      </div>
      <div v-else-if="tool.summary && isLongSummary" class="sd-row">
        <span class="sd-key">结果:</span>
        <div class="tool-card-summary-preview tool-card-summary-preview--clamped">
          {{ tool.summary }}
        </div>
      </div>
      <div v-else-if="tool.summary" class="sd-row">
        <span class="sd-key">结果:</span>
        <span class="sd-val">{{ tool.summary }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import DisclosureChevron from "./DisclosureChevron.vue";
import { useDisclosure } from "../composables/useDisclosure";
import { computed } from "vue";
import type { ToolResult } from "../types/agent";
import StreamingMarkdown from "./StreamingMarkdown.vue";
import StructuredData from "./StructuredData.vue";
import { renderMarkdown } from "../utils/markdown";

interface Props {
  tool: ToolResult;
}

const props = defineProps<Props>();

// Error summaries carry the full exception text; clamp long ones by default.
const { isOpen: errorExpanded, toggle: toggleErrorExpanded } = useDisclosure();
const isLongError = computed(
  () => (props.tool.summary?.length ?? 0) > 160,
);

// 成功结果只做预览：超过阈值才加截断类，保证渐隐只出现在真被裁掉内容时
// （渐隐作用于整个盒子，短内容若也套类会把最后一行无意义地淡出）。
const MARKDOWN_PREVIEW_CHARS = 600;
const SUMMARY_PREVIEW_CHARS = 240;
const isLongMarkdown = computed(() => {
  const content =
    props.tool.detail?.type === "markdown" ? props.tool.detail.content : "";
  return content.length > MARKDOWN_PREVIEW_CHARS;
});
const isLongSummary = computed(
  () => (props.tool.summary?.length ?? 0) > SUMMARY_PREVIEW_CHARS,
);
</script>
