<template>
  <div
    :class="[
      'subagent-group',
      { 'subagent-group--collapsed': !groupExpanded },
    ]"
  >
    <!-- Status bar (clickable toggle for the whole sub-agent group) -->
    <div
      :class="['subagent-status-bar', runStatusClass]"
      role="button"
      tabindex="0"
      :aria-expanded="groupExpanded"
      :aria-label="`切换 ${run.name} 详情`"
      @click="toggleGroupExpanded"
      @keydown.enter.prevent="toggleGroupExpanded"
      @keydown.space.prevent="toggleGroupExpanded"
    >
      <span
        v-if="run.runStatus === 'running'"
        class="subagent-status-dot"
      ></span>
      <span
        v-else-if="run.runStatus === 'error'"
        class="subagent-status-icon subagent-status-icon--error"
        >✗</span
      >
      <span v-else class="subagent-status-icon subagent-status-icon--done"
        >✓</span
      >
      <span class="subagent-status-name">{{ run.displayName ?? run.name }}</span>
      <span class="subagent-kind-badge">子代理</span>
      <span class="subagent-status-label">
        {{ statusLabel }}
      </span>
      <span
        v-if="run.runStatus !== 'running' && run.tools.length"
        class="subagent-status-meta"
      >
        {{ run.tools.length }} 工具 · {{ totalDuration(run.tools).toFixed(1) }}s
      </span>
      <span class="subagent-group-toggle">{{
        groupExpanded ? "收起" : "展开"
      }}</span>
    </div>

    <!-- Collapsible body: task, reasoning, error, wrapper, tool list -->
    <Transition name="expand-height">
      <div v-if="groupExpanded" class="subagent-group-body">
        <!-- Task description (only when running) -->
        <Transition name="fade-slide">
          <div v-if="run.task && run.runStatus === 'running'" class="subagent-task">
            {{ taskPreview }}
          </div>
        </Transition>

        <!-- Error: one-line preview by default, expandable to full text -->
        <Transition name="fade-slide">
          <div v-if="run.error" class="subagent-error">
            <div
              class="subagent-error-preview"
              :role="isLongError ? 'button' : undefined"
              :tabindex="isLongError ? 0 : undefined"
              :aria-expanded="isLongError ? errorExpanded : undefined"
              @click="isLongError && (errorExpanded = !errorExpanded)"
              @keydown.enter.prevent="isLongError && (errorExpanded = !errorExpanded)"
              @keydown.space.prevent="isLongError && (errorExpanded = !errorExpanded)"
            >
              <span class="subagent-error-text">{{ errorPreview }}</span>
              <span v-if="isLongError" class="subagent-error-toggle">
                {{ errorExpanded ? "收起 ▲" : "展开 ▼" }}
              </span>
            </div>
            <Transition name="expand">
              <div v-if="isLongError && errorExpanded" class="subagent-error-full">
                {{ run.error }}
              </div>
            </Transition>
          </div>
        </Transition>

        <!-- Wrapper tool result -->
        <Transition name="fade-slide">
          <ToolCard
            v-if="run.wrapper"
            class="tool-card--subagent-wrapper"
            :tool="run.wrapper"
            :expanded="isExpanded(run.wrapper.id)"
            @toggle="toggle(run.wrapper.id)"
          />
        </Transition>

        <!-- Merged reasoning block -->
        <Transition name="fade-slide">
          <div v-if="combinedThoughtText" class="subagent-reasoning">
            <div
              :class="[
                'subagent-reasoning-header',
                {
                  'subagent-reasoning-header--streaming': isStreamingThought,
                },
              ]"
              role="button"
              tabindex="0"
              :aria-expanded="reasoningExpanded"
              @click="toggleReasoningExpanded"
              @keydown.enter.prevent="toggleReasoningExpanded"
              @keydown.space.prevent="toggleReasoningExpanded"
            >
              <span class="subagent-reasoning-label">
                <span v-if="isStreamingThought" class="thinking-dot"></span>
                子代理思考过程
              </span>
              <span class="subagent-reasoning-toggle">{{
                reasoningExpanded ? "收起 ▲" : "展开 ▼"
              }}</span>
            </div>
            <Transition name="expand">
              <div
                v-if="reasoningExpanded"
                v-auto-scroll="isStreamingThought"
                class="subagent-reasoning-body"
              >
                <div class="thought-block thought-block--subagent">
                  <StreamingMarkdown
                    class="thought-text markdown-content"
                    :content="combinedThoughtText"
                    :is-streaming="isStreamingThought"
                  />
                </div>
              </div>
            </Transition>
          </div>
        </Transition>

        <!-- Child tools/sub-agents -->
        <TransitionGroup v-if="run.children.length" name="list" tag="div" class="subagent-timeline">
          <template v-for="item in run.children">
            <ToolCard
              v-if="item.type === 'tool'"
              :key="displayItemKey(item)"
              class="tool-card--subagent-tool"
              :tool="item.tool"
              :expanded="isExpanded(item.tool.id)"
              @toggle="toggle(item.tool.id)"
            />

            <SubagentNode
              v-else
              :key="displayItemKey(item)"
              :run="item"
              :is-expanded="isExpanded"
              :toggle="toggle"
              :is-conclusion-expanded="isConclusionExpanded"
              :toggle-conclusion="toggleConclusion"
            />
          </template>
        </TransitionGroup>

        <!-- Collapsible conclusion (after all tools have finished) -->
        <Transition name="fade-slide">
          <div v-if="run.conclusion" class="subagent-conclusion">
            <div
              class="subagent-conclusion-header"
              role="button"
              tabindex="0"
              :aria-expanded="isConclusionExpanded(run.key)"
              @click="toggleConclusion(run.key)"
              @keydown.enter.prevent="toggleConclusion(run.key)"
              @keydown.space.prevent="toggleConclusion(run.key)"
            >
              <span class="subagent-conclusion-label">子代理结论</span>
              <span class="subagent-conclusion-toggle">{{
                isConclusionExpanded(run.key) ? "收起 ▲" : "展开 ▼"
              }}</span>
            </div>
            <Transition name="expand">
              <div
                v-if="isConclusionExpanded(run.key)"
                v-auto-scroll="run.runStatus === 'running'"
                class="subagent-conclusion-body"
              >
                <StreamingMarkdown
                  class="markdown-content"
                  :content="run.conclusion"
                  :is-streaming="run.runStatus === 'running'"
                />
              </div>
            </Transition>
          </div>
        </Transition>
      </div>
    </Transition>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ToolResult } from "../types/agent";
import type { SubagentToolDisplayItem } from "../utils/toolCalls";
import { displayItemKey } from "../utils/toolCalls";
import ToolCard from "./ToolCard.vue";
import StreamingMarkdown from "./StreamingMarkdown.vue";

interface Props {
  run: SubagentToolDisplayItem;
  isExpanded: (name: string) => boolean;
  toggle: (name: string) => void;
  isConclusionExpanded: (key: string) => boolean;
  toggleConclusion: (key: string) => void;
}

const props = defineProps<Props>();

// Whole group: expanded while the run is streaming, auto-collapses when the
// run finishes — unless the user has manually toggled it.  Restored sessions
// (status already final) start collapsed.
const groupExpanded = ref(props.run.runStatus === "running");
let groupToggled = false;
function toggleGroupExpanded() {
  groupToggled = true;
  groupExpanded.value = !groupExpanded.value;
}

// Reasoning block: expanded while the run is streaming, auto-collapses when
// the run finishes — unless the user has manually toggled it.
const reasoningExpanded = ref(props.run.runStatus === "running");
let reasoningToggled = false;
function toggleReasoningExpanded() {
  reasoningToggled = true;
  reasoningExpanded.value = !reasoningExpanded.value;
}
watch(
  () => props.run.runStatus,
  (status, prev) => {
    if (prev === "running" && status !== "running") {
      if (!groupToggled) groupExpanded.value = false;
      if (!reasoningToggled) reasoningExpanded.value = false;
    }
  },
);

const runStatusClass = computed(() => props.run.runStatus ?? "completed");

// The orchestrator embeds the full input data (the whole document) into the
// sub-agent task; while running, show only the leading instruction.
const TASK_PREVIEW_CHARS = 120;
const taskPreview = computed(() => {
  const task = props.run.task ?? "";
  return task.length > TASK_PREVIEW_CHARS
    ? `${task.slice(0, TASK_PREVIEW_CHARS)} …`
    : task;
});

const statusLabel = computed(() => {
  const status = props.run.runStatus;
  if (status === "running") return "审计中…";
  if (status === "error") return "失败";
  return "完成";
});

const combinedThoughtText = computed(() =>
  (props.run.thoughts ?? [])
    .filter((t) => t.text.trim() !== "")
    .map((t) => t.text)
    .join("\n\n"),
);

const isStreamingThought = computed(
  () => props.run.runStatus === "running" && combinedThoughtText.value.length > 0,
);

// Error block: collapsed to a single-line preview unless toggled.
const errorExpanded = ref(false);
const isLongError = computed(() => {
  const err = props.run.error ?? "";
  return err.length > 80 || err.includes("\n");
});
const errorPreview = computed(() => {
  const firstLine = (props.run.error ?? "").split("\n", 1)[0];
  const cut = firstLine.length > 80 ? firstLine.slice(0, 80) : firstLine;
  return isLongError.value ? `${cut} …` : cut;
});

function totalDuration(tools: ToolResult[]): number {
  return tools.reduce((sum, t) => sum + (t.duration ?? 0), 0);
}
</script>
