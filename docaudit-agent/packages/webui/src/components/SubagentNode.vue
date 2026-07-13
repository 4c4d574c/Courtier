<template>
  <div
    :class="[
      'subagent-group',
      { 'subagent-group--collapsed': !isGroupExpanded(run.key) },
    ]"
  >
    <!-- Status bar (clickable toggle for the whole sub-agent group) -->
    <div
      :class="['subagent-status-bar', runStatusClass]"
      role="button"
      tabindex="0"
      :aria-expanded="isGroupExpanded(run.key)"
      :aria-label="`切换 ${run.name} 详情`"
      @click="toggleGroup(run.key)"
      @keydown.enter.prevent="toggleGroup(run.key)"
      @keydown.space.prevent="toggleGroup(run.key)"
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
      <span class="subagent-status-name">{{ run.name }}</span>
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
        isGroupExpanded(run.key) ? "收起" : "展开"
      }}</span>
    </div>

    <!-- Collapsible body: task, reasoning, error, wrapper, tool list -->
    <Transition name="expand-height">
      <div v-if="isGroupExpanded(run.key)" class="subagent-group-body">
        <!-- Task description (only when running) -->
        <Transition name="fade-slide">
          <div v-if="run.task && run.runStatus === 'running'" class="subagent-task">
            {{ run.task }}
          </div>
        </Transition>

        <!-- Error -->
        <Transition name="fade-slide">
          <div v-if="run.error" class="subagent-error">
            {{ run.error }}
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
              :aria-expanded="isReasoningExpanded(run.key)"
              @click="toggleReasoning(run.key)"
              @keydown.enter.prevent="toggleReasoning(run.key)"
              @keydown.space.prevent="toggleReasoning(run.key)"
            >
              <span class="subagent-reasoning-label">
                <span v-if="isStreamingThought" class="thinking-dot"></span>
                子代理思考过程
              </span>
              <span class="subagent-reasoning-toggle">{{
                isReasoningExpanded(run.key) ? "收起 ▲" : "展开 ▼"
              }}</span>
            </div>
            <Transition name="expand">
              <div
                v-if="isReasoningExpanded(run.key)"
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
              :is-reasoning-expanded="isReasoningExpanded"
              :toggle-reasoning="toggleReasoning"
              :is-conclusion-expanded="isConclusionExpanded"
              :toggle-conclusion="toggleConclusion"
              :is-group-expanded="isGroupExpanded"
              :toggle-group="toggleGroup"
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
import { computed } from "vue";
import type { ToolResult } from "../types/agent";
import type { SubagentToolDisplayItem } from "../utils/toolCalls";
import { displayItemKey } from "../utils/toolCalls";
import ToolCard from "./ToolCard.vue";
import StreamingMarkdown from "./StreamingMarkdown.vue";

interface Props {
  run: SubagentToolDisplayItem;
  isExpanded: (name: string) => boolean;
  toggle: (name: string) => void;
  isReasoningExpanded: (key: string) => boolean;
  toggleReasoning: (key: string) => void;
  isConclusionExpanded: (key: string) => boolean;
  toggleConclusion: (key: string) => void;
  isGroupExpanded: (key: string) => boolean;
  toggleGroup: (key: string) => void;
}

const props = defineProps<Props>();

const runStatusClass = computed(() => props.run.runStatus ?? "completed");

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

function totalDuration(tools: ToolResult[]): number {
  return tools.reduce((sum, t) => sum + (t.duration ?? 0), 0);
}
</script>
