<template>
  <!-- No click handlers here: clicks and keydowns bubble to the
       tool-card-header wrapper in ToolCard, which is the single toggle
       source. A row-level handler used to double-fire with the wrapper's
       (emit + bubbled emit = instant collapse). -->
  <div class="tool-card-row">
    <span class="tool-card-icon" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
      </svg>
    </span>
    <span class="tool-card-title">
      <span
        :class="[
          'tool-card-name',
          { 'tool-card-name--cancelled': tool.status === 'cancelled' },
        ]"
        >{{ displayName }}</span
      >
      <span v-if="hasError" class="tool-card-badge tool-card-badge--error"
        >错误</span
      >
      <span v-if="badgeLabel" :class="['tool-card-badge', badgeClass]">
        {{ badgeLabel }}
      </span>
    </span>
    <div class="tool-card-meta">
      <span v-if="tool.status === 'running'" class="tool-card-running-badge">
        {{ tool.progress || "执行中…" }}
      </span>
      <span
        v-else-if="tool.status === 'pending'"
        class="tool-card-running-badge"
        >等待中</span
      >
      <span
        v-if="tool.status === 'running' && tool.startTime"
        class="tool-card-time"
        >{{ liveDuration.toFixed(1) }}s</span
      >
      <span v-else-if="tool.duration" class="tool-card-time"
        >{{ tool.duration.toFixed(1) }}s</span
      >
      <span class="tool-card-expand-icon"><DisclosureChevron :open="open" :size="12" /></span>
    </div>
  </div>
  <div v-if="tool.skill && tool.skillDescription" class="tool-card-description">
    {{ tool.skillDescription }}
  </div>
  <div v-if="!hasIssueCounts && !hasIssues" class="tool-card-data">
    <DataChip
      v-for="(chip, i) in parsedChips"
      :key="i"
      :label="chip.label"
      :value="chip.value"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from "vue";
import type { ToolResult } from "../types/agent";
import { displayToolName } from "../utils/toolCalls";
import DataChip from "./DataChip.vue";
import DisclosureChevron from "./DisclosureChevron.vue";

interface Props {
  tool: ToolResult;
  /** Disclosure state, owned by the ToolCard root. */
  open?: boolean;
}

const props = defineProps<Props>();
const liveDuration = ref(props.tool.duration ?? 0);
let timer: ReturnType<typeof setInterval> | null = null;

function startTimer() {
  if (timer !== null) clearInterval(timer);
  timer = null;
  if (props.tool.status !== "running" || !props.tool.startTime) return;
  liveDuration.value = (Date.now() - props.tool.startTime) / 1000;
  // 2Hz: k parallel running cards each re-render on every tick — 10Hz on
  // top of streaming renders was measurable. Skip ref updates while the
  // document is hidden (background intervals fire throttled but would
  // still queue renders for when the tab returns).
  timer = setInterval(() => {
    if (props.tool.startTime) {
      if (!document.hidden) {
        liveDuration.value = (Date.now() - props.tool.startTime) / 1000;
      }
    } else {
      clearTimer();
    }
  }, 500);
}

function clearTimer() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

onMounted(() => {
  if (props.tool.status === "running" && props.tool.startTime) {
    startTimer();
  }
});

watch(
  () => [props.tool.status, props.tool.startTime] as const,
  ([status, startTime]) => {
    if (status === "running" && startTime) {
      startTimer();
    } else {
      clearTimer();
    }
  },
);

onUnmounted(clearTimer);
const displayName = computed(() => {
  // 优先展示 plugin.yaml 的 display_name(如"格式解析")，回退到工具名。
  if (props.tool.displayName) {
    const name = props.tool.displayName;
    return props.tool.skill ? `${props.tool.skill} (${name})` : name;
  }
  const name = displayToolName(props.tool);
  return props.tool.skill ? `${props.tool.skill} (${name})` : name;
});

const badgeLabel = computed(() => {
  if (props.tool.status === "cancelled") return "已中断";
  if (props.tool.callKind === "subagent_run") return "子代理运行";
  if (props.tool.callScope === "subagent") return "子代理工具";
  if (props.tool.skill) return "技能";
  return "";
});

const badgeClass = computed(() => {
  if (props.tool.status === "cancelled") return "tool-card-badge--cancelled";
  if (props.tool.callKind === "subagent_run")
    return "tool-card-badge--subagent-run";
  if (props.tool.callScope === "subagent")
    return "tool-card-badge--subagent-tool";
  if (props.tool.skill) return "tool-card-badge--skill";
  return "";
});

const parsedChips = computed(() => {
  if (!props.tool.summary) return [];
  const chips: { label: string; value: string }[] = [];
  const pairs = props.tool.summary.split(",");
  for (const pair of pairs) {
    const parts = pair.split(":").map((s) => s.trim());
    if (parts.length === 2) {
      chips.push({ label: parts[0], value: parts[1] });
    }
  }
  return chips;
});

const hasIssues = computed(
  () => props.tool.status === "warning" || props.tool.status === "error",
);
const hasIssueCounts = computed(() => props.tool.issueCounts != null);

// 标题栏只在工具调用出错时给出一个低调的「错误」徽标；计数/警告/通过等
// 明细留在展开视图中，避免折叠态过于扎眼。issueCounts 存在时优先于 status。
const hasError = computed(
  () =>
    props.tool.status === "error" ||
    (props.tool.issueCounts?.err ?? 0) > 0,
);
</script>
