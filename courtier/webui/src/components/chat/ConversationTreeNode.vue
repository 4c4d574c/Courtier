<template>
  <div class="tree-node" :style="{ paddingLeft: `${level * 12}px` }">
    <div :class="['tree-node-card', { current: isCurrent }]">
      <div class="tree-node-main">
        <span class="tree-node-dot" :class="{ current: isCurrent }"></span>
        <span class="tree-node-turn">Turn {{ node.turn_index }}</span>
        <span class="tree-node-id">{{ node.node_id.slice(0, 6) }}</span>
        <span v-if="forkReason" class="tree-node-reason" :title="forkReason">
          {{ forkReason }}
        </span>
      </div>
      <div class="tree-node-actions">
        <button
          class="tree-node-btn"
          type="button"
          title="从该节点创建分支"
          @click="$emit('fork', node.node_id)"
        >
          分支
        </button>
        <button
          class="tree-node-btn"
          type="button"
          title="回退到该节点"
          @click="$emit('rewind', node.node_id)"
        >
          回退
        </button>
      </div>
    </div>
    <div v-if="children.length" class="tree-children">
      <ConversationTreeNode
        v-for="child in children"
        :key="child.node_id"
        :node="child"
        :tree="tree"
        :current-node-id="currentNodeId"
        :level="level + 1"
        @fork="$emit('fork', $event)"
        @rewind="$emit('rewind', $event)"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type { ConversationTree, ConversationTreeNode } from "../../types/agent";

interface Props {
  node: ConversationTreeNode;
  tree: ConversationTree;
  currentNodeId?: string | null;
  level?: number;
}

const props = withDefaults(defineProps<Props>(), {
  level: 0,
});
defineEmits<{
  fork: [nodeId: string];
  rewind: [nodeId: string];
}>();

const isCurrent = computed(() => props.currentNodeId === props.node.node_id);

// Guard against runaway recursion on malformed tree data.
const MAX_TREE_DEPTH = 50;

const forkReason = computed(() => {
  const reason = props.node.metadata?.fork_reason;
  return typeof reason === "string" ? reason : "";
});

const children = computed(() => {
  if (props.level >= MAX_TREE_DEPTH) return [];
  return props.node.children
    .map((id) => props.tree.nodes[id])
    .filter((n): n is ConversationTreeNode => n != null);
});
</script>

<style scoped>
.tree-node {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tree-node-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-body);
  border: 1px solid var(--chat-border);
  font-size: 13px;
  transition:
    background 0.15s,
    border-color 0.15s;
}

.tree-node-card.current {
  background: rgba(79, 70, 229, 0.08);
  border-color: var(--chat-accent);
}

.tree-node-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.tree-node-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--chat-text-tertiary);
  flex-shrink: 0;
}

.tree-node-dot.current {
  background: var(--chat-accent);
}

.tree-node-turn {
  font-weight: 600;
  color: var(--chat-text-primary);
  flex-shrink: 0;
}

.tree-node-id {
  font-family: "JetBrains Mono", monospace;
  color: var(--chat-text-tertiary);
  font-size: 12px;
}

.tree-node-reason {
  color: var(--chat-text-secondary);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 120px;
}

.tree-node-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.tree-node-btn {
  padding: 2px 8px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-card);
  color: var(--chat-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s;
}

.tree-node-btn:hover {
  border-color: var(--chat-accent);
  color: var(--chat-accent);
}

.tree-children {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
</style>
