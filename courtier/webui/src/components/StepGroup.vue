<template>
  <div class="step-group">
    <!-- Merged thought block for this step -->
    <Transition name="fade-slide">
      <div v-if="visibleThoughts.length" class="thought-segment">
        <div
          :class="[
            'thought-segment-header',
            { 'thought-segment-header--streaming': isRunning && isLastStep },
          ]"
          role="button"
          tabindex="0"
          :aria-expanded="thoughtsExpanded"
          @click="toggleThoughts"
          @keydown.enter.prevent="toggleThoughts"
          @keydown.space.prevent="toggleThoughts"
        >
          <span class="thought-segment-label">
            <span v-if="isRunning && isLastStep" class="thinking-dot"></span>
            思考过程
            <span v-if="isRunning && isLastStep" class="thought-status-badge"
              >分析中…</span
            >
          </span>
          <span class="thought-segment-toggle">{{
            thoughtsExpanded ? "收起 ▲" : "展开 ▼"
          }}</span>
        </div>
        <Transition name="expand">
          <div
            v-if="thoughtsExpanded"
            v-auto-scroll="isRunning && isLastStep"
            class="thought-segment-body"
          >
            <div class="thought-block">
              <StreamingMarkdown
                class="thought-text markdown-content"
                :content="combinedThoughtText"
                :is-streaming="isRunning && isLastStep"
              />
            </div>
          </div>
        </Transition>
      </div>
    </Transition>

    <!-- Tool / subagent items -->
    <TransitionGroup name="list" tag="div" class="step-items">
      <template v-for="item in displayItems">
        <ToolCard
          v-if="item.type === 'tool'"
          :key="displayItemKey(item)"
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
          :is-conclusion-expanded="isSubagentConclusionExpanded"
          :toggle-conclusion="toggleSubagentConclusion"
        />
      </template>
    </TransitionGroup>
    <!-- 步骤 verdict（中间结论）不在此渲染：buildProcessItems 将其作为
         assistant 消息渲染在该 step 圆角框之前（闭合前一个框）；在此处
         渲染会造成恢复历史会话时框内“结”块与框外文本重复。 -->
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { Step, Thought } from "../types/agent";
import { buildStepToolGroups, displayItemKey } from "../utils/toolCalls";
import { useToggleSet } from "../composables/useToggleSet";

import ToolCard from "./ToolCard.vue";
import StreamingMarkdown from "./StreamingMarkdown.vue";
import SubagentNode from "./SubagentNode.vue";

interface Props {
  step: Step;
  stepThoughts: Thought[];
  isRunning: boolean;
  isLastStep: boolean;
  isExpanded: (name: string) => boolean;
  toggle: (name: string) => void;
}

const props = defineProps<Props>();

const displayItems = computed(() =>
  buildStepToolGroups(props.step.tools, props.step.subagents),
);

const visibleThoughts = computed(() =>
  props.stepThoughts.filter((t) => t.text.trim() !== ""),
);

const combinedThoughtText = computed(() =>
  visibleThoughts.value.map((t) => t.text).join("\n\n"),
);

// Thought block: expanded while this step is the one currently streaming,
// auto-collapses when it finishes — unless the user has manually toggled it.
const thoughtsExpanded = ref(props.isRunning && props.isLastStep);
let thoughtsToggled = false;

function toggleThoughts() {
  thoughtsToggled = true;
  thoughtsExpanded.value = !thoughtsExpanded.value;
}

watch(
  () => props.isRunning && props.isLastStep,
  (active, wasActive) => {
    if (wasActive && !active && !thoughtsToggled) {
      thoughtsExpanded.value = false;
    }
  },
);

const {
  toggle: toggleSubagentConclusion,
  isExpanded: isSubagentConclusionExpanded,
} = useToggleSet({ defaultExpanded: true });
</script>
