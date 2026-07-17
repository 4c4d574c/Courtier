<template>
  <section class="tree-section">
    <h4 class="tree-section-title">
      会话分支
      <span v-if="nodeCount > 0" class="tree-count">{{ nodeCount }} 节点</span>
    </h4>
    <div v-if="rootNode" class="tree">
      <ConversationTreeNode
        :node="rootNode"
        :tree="tree || { root_id: null, nodes: {} }"
        :current-node-id="currentNodeId"
        :level="0"
        @fork="$emit('fork', $event)"
        @rewind="$emit('rewind', $event)"
      />
    </div>
    <p v-else class="tree-empty">当前会话没有分支历史</p>
  </section>
</template>

<script setup lang="ts">
import { computed } from "vue";
import type {
  ConversationTree,
  ConversationTreeNode as ConversationTreeNodeType,
} from "../../types/agent";
import ConversationTreeNode from "./ConversationTreeNode.vue";

interface Props {
  tree?: ConversationTree | null;
  currentNodeId?: string | null;
}

const props = defineProps<Props>();
defineEmits<{
  fork: [nodeId: string];
  rewind: [nodeId: string];
}>();

const rootNode = computed<ConversationTreeNodeType | null>(() => {
  if (!props.tree?.root_id) return null;
  return props.tree.nodes[props.tree.root_id] ?? null;
});

const nodeCount = computed(() =>
  props.tree?.nodes ? Object.keys(props.tree.nodes).length : 0,
);
</script>

<style scoped>
.tree-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tree-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--chat-text-secondary);
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0;
}

.tree-count {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--chat-bg-hover);
  color: var(--chat-text-tertiary);
}

.tree {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tree-empty {
  font-size: 13px;
  color: var(--chat-text-tertiary);
  margin: 0;
  padding: 4px 0;
}
</style>
