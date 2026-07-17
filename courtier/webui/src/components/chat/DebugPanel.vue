<template>
  <aside :class="['debug-panel', { open }]">
    <div class="debug-panel-header">
      <span class="debug-panel-title">调试面板</span>
      <button
        class="debug-panel-close"
        type="button"
        title="关闭调试面板"
        @click="$emit('close')"
      >
        ✕
      </button>
    </div>
    <div class="debug-panel-body">
      <section class="debug-section">
        <h4 class="debug-section-title">
          模型事件
          <span v-if="modelEventList.length" class="debug-count">{{ modelEventList.length }}</span>
        </h4>
        <ul v-if="modelEventList.length" class="debug-list">
          <li v-for="(evt, i) in modelEventList" :key="`m-${i}`" class="debug-item">
            <span :class="['debug-badge', evt.type === 'model_fallback' ? 'fallback' : 'primary']">
              {{ evt.type === 'model_fallback' ? 'fallback' : 'selected' }}
            </span>
            <span v-if="evt.backend" class="debug-backend">{{ evt.backend }}</span>
            <span v-if="evt.strategy" class="debug-strategy">{{ evt.strategy }}</span>
            <span v-if="evt.reason" class="debug-reason">{{ evt.reason }}</span>
          </li>
        </ul>
        <p v-else class="debug-empty">暂无模型事件</p>
      </section>

      <section class="debug-section">
        <h4 class="debug-section-title">
          提示注入
          <span v-if="hintEventList.length" class="debug-count">{{ hintEventList.length }}</span>
        </h4>
        <ul v-if="hintEventList.length" class="debug-list">
          <li v-for="(evt, i) in hintEventList" :key="`h-${i}`" class="debug-item">
            <span v-if="evt.hintType" class="debug-hint-type">{{ evt.hintType }}</span>
            <span v-if="evt.text" class="debug-text">{{ evt.text }}</span>
          </li>
        </ul>
        <p v-else class="debug-empty">暂无提示注入事件</p>
      </section>

      <section class="debug-section">
        <h4 class="debug-section-title">循环完成</h4>
        <div v-if="loopCompleted" class="debug-loop">
          <span class="debug-loop-status">{{ loopCompleted.status }}</span>
          <span v-if="loopCompleted.terminationReason" class="debug-loop-reason">
            {{ loopCompleted.terminationReason }}
          </span>
          <span v-if="loopCompleted.totalSteps != null" class="debug-loop-steps">
            steps: {{ loopCompleted.totalSteps }}
          </span>
        </div>
        <p v-else class="debug-empty">会话尚未结束</p>
      </section>

      <ConversationTreePanel
        :tree="treeJson"
        :current-node-id="currentNodeId"
        @fork="$emit('fork', $event)"
        @rewind="$emit('rewind', $event)"
      />
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { ConversationTree, RuntimeEvent } from "../../types/agent";
import ConversationTreePanel from "./ConversationTreePanel.vue";

interface Props {
  open: boolean;
  modelEvents?: RuntimeEvent[];
  hintEvents?: RuntimeEvent[];
  loopCompleted?: RuntimeEvent | null;
  treeJson?: ConversationTree | null;
  currentNodeId?: string | null;
}

const props = defineProps<Props>();
defineEmits<{
  close: [];
  fork: [nodeId: string];
  rewind: [nodeId: string];
}>();

const modelEventList = computed(() => props.modelEvents ?? []);
const hintEventList = computed(() => props.hintEvents ?? []);
</script>

<style scoped>
.debug-panel {
  width: 0;
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-left: 1px solid var(--chat-border);
  overflow: hidden;
  transition: width 0.25s ease;
  display: flex;
  flex-direction: column;
}

.debug-panel.open {
  width: 320px;
}

.debug-panel-header {
  height: 56px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  border-bottom: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
}

.debug-panel-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.debug-panel-close {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--chat-text-secondary);
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  font-size: 14px;
}

.debug-panel-close:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.debug-panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.debug-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.debug-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--chat-text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
}

.debug-count {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--chat-bg-hover);
  color: var(--chat-text-tertiary);
}

.debug-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
}

.debug-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-body);
  border: 1px solid var(--chat-border);
  font-size: 13px;
}

.debug-badge {
  flex-shrink: 0;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
}

.debug-badge.primary {
  color: var(--chat-accent);
  background: rgba(183, 28, 46, 0.08);
  border: 1px solid rgba(183, 28, 46, 0.18);
}

.debug-badge.fallback {
  color: var(--warn);
  background: rgba(184, 122, 14, 0.08);
  border: 1px solid rgba(184, 122, 14, 0.22);
}

.debug-backend,
.debug-strategy,
.debug-hint-type {
  font-family: "JetBrains Mono", monospace;
  color: var(--chat-text-primary);
}

.debug-reason,
.debug-text,
.debug-loop-reason {
  color: var(--chat-text-secondary);
  word-break: break-all;
}

.debug-empty {
  font-size: 13px;
  color: var(--chat-text-tertiary);
  margin: 0;
  padding: 4px 0;
}

.debug-loop {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-body);
  border: 1px solid var(--chat-border);
  font-size: 13px;
}

.debug-loop-status {
  font-weight: 600;
  color: var(--chat-text-primary);
  text-transform: uppercase;
}

.debug-loop-steps {
  font-family: "JetBrains Mono", monospace;
  color: var(--chat-text-tertiary);
}

@media (max-width: 768px) {
  .debug-panel.open {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 80vw;
    max-width: 320px;
    z-index: 200;
  }
}
</style>
