<template>
  <!-- No click handlers here: clicks and keydowns bubble to the ToolCard
       root, which is the single toggle source. A row-level handler used to
       double-fire with the root's (emit + bubbled emit = instant collapse). -->
  <div class="tool-card-row" role="button" tabindex="0">
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
      <span class="tool-card-expand-icon"><AppIcon name="chevron-right" :size="12" /></span>
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
  <div v-if="hasIssueCounts" class="tool-card-issues">
    <span class="issue-count err">{{ issueErr }} 项错误</span>
    <span class="issue-count warn">{{ issueWarn }} 项警告</span>
    <span class="issue-count ok">{{ issueOk }} 项通过</span>
    <span v-if="issueUnchecked > 0" class="issue-count unchecked"
      >{{ issueUnchecked }} 项未检查</span
    >
  </div>
  <div v-else-if="hasIssues" class="tool-card-issues">
    <span class="issue-count err">{{ errorCount }} 项错误</span>
    <span class="issue-count warn">{{ warnCount }} 项警告</span>
    <span class="issue-count ok">{{ okCount }} 项通过</span>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted, watch } from "vue";
import type { ToolResult } from "../types/agent";
import { displayToolName } from "../utils/toolCalls";
import DataChip from "./DataChip.vue";
import AppIcon from "./AppIcon.vue";

interface Props {
  tool: ToolResult;
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
  // 优先展示 plugin.yaml 的 display_name（如"解析文档"），回退到工具名。
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
const errorCount = computed(() => (props.tool.status === "error" ? 1 : 0));
const warnCount = computed(() => (props.tool.status === "warning" ? 1 : 0));
const okCount = computed(() => (props.tool.status === "done" ? 1 : 0));

// 审核计数：后端 issueCounts 存在时优先于 status 派生渲染；
// 旧持久化会话/不发计数的工具回退到上面的 status 派生行为。
const issueCounts = computed(() => props.tool.issueCounts ?? null);
const hasIssueCounts = computed(() => issueCounts.value !== null);
const issueErr = computed(() => issueCounts.value?.err ?? 0);
const issueWarn = computed(() => issueCounts.value?.warn ?? 0);
const issueOk = computed(() => issueCounts.value?.ok ?? 0);
const issueUnchecked = computed(() => issueCounts.value?.unchecked ?? 0);
</script>
