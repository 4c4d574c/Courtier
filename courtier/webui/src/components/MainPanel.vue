<template>
  <div ref="containerRef" class="main-panel" aria-live="polite">
    <!-- Empty state: no turns yet -->
    <div v-if="turns.length === 0 && !isRunning" class="welcome-panel">
      <div class="welcome-icon">审</div>
      <h2 class="welcome-title">{{ MESSAGES.WELCOME_TITLE }}</h2>
      <p class="welcome-desc">{{ MESSAGES.WELCOME_DESC }}</p>
      <div class="welcome-examples">
        <span
          class="welcome-example"
          role="button"
          tabindex="0"
          @click="$emit('quickTask', '审核这份通知的格式规范')"
          @keydown.enter.prevent="$emit('quickTask', '审核这份通知的格式规范')"
          @keydown.space.prevent="$emit('quickTask', '审核这份通知的格式规范')"
          >格式审核</span
        >
        <span
          class="welcome-example"
          role="button"
          tabindex="0"
          @click="$emit('quickTask', '检查公文内容是否符合规范要求')"
          @keydown.enter.prevent="
            $emit('quickTask', '检查公文内容是否符合规范要求')
          "
          @keydown.space.prevent="
            $emit('quickTask', '检查公文内容是否符合规范要求')
          "
          >内容审核</span
        >
        <span
          class="welcome-example"
          role="button"
          tabindex="0"
          @click="$emit('quickTask', '全面审核这份文档')"
          @keydown.enter.prevent="$emit('quickTask', '全面审核这份文档')"
          @keydown.space.prevent="$emit('quickTask', '全面审核这份文档')"
          >全面审核</span
        >
      </div>
    </div>
    <!-- Loading state: running but no turns yet -->
    <div v-else-if="turns.length === 0 && isRunning" class="loading-panel">
      <div class="loading-pulse"></div>
      <p class="loading-text">{{ MESSAGES.LOADING_CONNECTING }}</p>
    </div>
    <template v-for="(turn, ti) in turns" :key="ti">
      <div class="chat-message chat-message--user">
        <div class="chat-bubble">
          {{ turn.message.text }}
          <div v-if="turn.message.fileName" class="chat-file-tag">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              width="12"
              height="12"
            >
              <path
                d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"
              />
              <polyline points="14 2 14 8 20 8" />
            </svg>
            {{ turn.message.fileName }}
          </div>
        </div>
      </div>
      <StepGroup
        v-for="(step, si) in turn.steps"
        :key="step.index"
        :step="step"
        :step-thoughts="thoughtsForStepWrapper(turn, si)"
        :is-running="isRunning"
        :is-last-step="ti === turns.length - 1 && si === turn.steps.length - 1"
        :is-expanded="isExpanded"
        :toggle="toggle"
      />
      <div v-if="turn.conclusion" class="conclusion">
        <!-- Collapsible header -->
        <div
          :class="[
            'conclusion-header',
            {
              'conclusion-header--streaming':
                isRunning && ti === turns.length - 1,
            },
          ]"
          role="button"
          tabindex="0"
          :aria-expanded="isConclusionExpanded(ti)"
          @click="toggleConclusion(ti)"
          @keydown.enter.prevent="toggleConclusion(ti)"
          @keydown.space.prevent="toggleConclusion(ti)"
        >
          <span class="conclusion-header-label">
            <span
              v-if="isRunning && ti === turns.length - 1"
              class="thinking-dot"
            ></span>
            {{ MESSAGES.CONCLUSION_LABEL }}
            <span
              v-if="isRunning && ti === turns.length - 1"
              class="thought-status-badge"
              >生成中…</span
            >
          </span>
          <span class="conclusion-header-toggle">{{
            isConclusionExpanded(ti) ? "收起 ▲" : "展开 ▼"
          }}</span>
        </div>
        <div v-if="isConclusionExpanded(ti)" class="conclusion-inner">
          <StreamingMarkdown
            class="conclusion-text markdown-content"
            :content="turn.conclusion ?? ''"
            :is-streaming="isRunning && ti === turns.length - 1"
          />
        </div>
        <div class="conclusion-stats">
          <span class="conclusion-stat">
            <span class="conclusion-stat-dim">输入</span>
            <span class="conclusion-stat-val">{{
              formatTokens(stats.tokensIn)
            }}</span>
          </span>
          <span class="conclusion-stat-sep">·</span>
          <span class="conclusion-stat">
            <span class="conclusion-stat-dim">输出</span>
            <span class="conclusion-stat-val">{{
              formatTokens(stats.tokensOut)
            }}</span>
          </span>
          <span class="conclusion-stat-sep">·</span>
          <span class="conclusion-stat">
            <span class="conclusion-stat-dim">步骤</span>
            <span class="conclusion-stat-val">{{ stepCount }}</span>
          </span>
          <span class="conclusion-stat-sep">·</span>
          <span class="conclusion-stat">
            <span class="conclusion-stat-dim">工具调用</span>
            <span class="conclusion-stat-val"
              >{{ doneTools }}/{{ totalTools }}</span
            >
          </span>
          <span v-if="issueCount > 0" class="conclusion-stat-sep">·</span>
          <span v-if="issueCount > 0" class="conclusion-stat">
            <span class="conclusion-stat-dim">发现问题</span>
            <span class="conclusion-stat-val conclusion-stat-val--warn">{{
              issueCount
            }}</span>
          </span>
        </div>
      </div>
      <!-- Error card -->
      <div
        v-if="errorMessage && ti === turns.length - 1"
        class="session-error-card"
      >
        <span class="session-error-icon">&#x2717;</span>
        <div>
          <span class="session-error-title">{{
            MESSAGES.SESSION_ERROR_TITLE
          }}</span>
          <div class="session-error-detail">{{ errorMessage }}</div>
        </div>
      </div>
      <!-- Stopped card -->
      <div
        v-else-if="
          stopReason === 'user' && ti === turns.length - 1 && !isRunning
        "
        class="session-stopped-card"
      >
        <span class="session-stopped-icon">&#x2298;</span>
        <div>
          <span class="session-stopped-title">{{
            MESSAGES.SESSION_STOPPED
          }}</span>
          <div class="session-stopped-detail">
            {{ MESSAGES.SESSION_STOPPED_DETAIL }}
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { watch, nextTick, onMounted, onUnmounted, ref } from "vue";
import type { Turn, Thought } from "../types/agent";
import StepGroup from "./StepGroup.vue";
import StreamingMarkdown from "./StreamingMarkdown.vue";
import { useAutoScroll } from "../composables/useAutoScroll";
import { thoughtsForStep } from "../utils/sessionUtils";
import { MESSAGES } from "../constants/messages";

import { formatTokens } from "../utils/formatting";

interface Props {
  turns: Turn[];
  turnVersion: number;
  thoughts: Thought[];
  isExpanded: (name: string) => boolean;
  toggle: (name: string) => void;
  stats: { tokensIn: number; tokensOut: number };
  stepCount: number;
  totalTools: number;
  doneTools: number;
  issueCount: number;
  errorMessage?: string;
  stopReason?: string;
  isRunning: boolean;
}

const props = defineProps<Props>();
// Per-turn conclusion collapse state: key = turn index, false = collapsed
const conclusionExpanded = ref<Record<number, boolean>>({});

function isConclusionExpanded(ti: number): boolean {
  return conclusionExpanded.value[ti] !== false;
}

function toggleConclusion(ti: number): void {
  conclusionExpanded.value = {
    ...conclusionExpanded.value,
    [ti]: !isConclusionExpanded(ti),
  };
}

const { containerRef, mount, unmount, forceScrollToBottom, watchForScroll } =
  useAutoScroll();

onMounted(() => mount());
onUnmounted(() => unmount());

function thoughtsForStepWrapper(turn: Turn, stepIndex: number): Thought[] {
  return thoughtsForStep(props.thoughts, turn, stepIndex);
}

// Auto-scroll when new content arrives (turnVersion increments)
watchForScroll(() => props.turnVersion);

// Auto-scroll when turns array grows (new user message)
watch(
  () => props.turns.length,
  () => {
    nextTick(forceScrollToBottom);
  },
);
</script>
