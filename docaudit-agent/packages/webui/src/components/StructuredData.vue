<template>
  <div class="structured-data">
    <div v-for="(value, key) in flattened" :key="key" class="sd-row">
      <span class="sd-key">{{ key }}:</span>
      <span class="sd-val">{{ formatValue(value) }}</span>
    </div>
    <div v-if="truncatedCount > 0" class="sd-truncated-hint">
      还有 {{ truncatedCount }} 个嵌套字段未展开
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";

/** Max depth for recursive flattening. Arrays are never recursed into. */
const MAX_DEPTH = 2;
/** Max length for string values before truncation. */
const MAX_STRING_LEN = 200;
/** Max total flattened keys before stopping. */
const MAX_KEYS = 80;

interface Props {
  data: Record<string, unknown>;
}

const props = defineProps<Props>();

const truncatedCount = ref(0);

const flattened = computed(() => {
  const result: Record<string, unknown> = {};
  truncatedCount.value = 0;

  function flatten(obj: unknown, prefix: string, depth: number) {
    if (obj === null || obj === undefined) {
      result[prefix] = null;
      return;
    }
    if (typeof obj !== "object") {
      result[prefix] = obj;
      return;
    }
    if (Array.isArray(obj)) {
      if (obj.length === 0) {
        result[prefix] = "[]";
      } else {
        result[prefix] = `[${obj.length} items]`;
      }
      return;
    }
    // Object — stop recursing at max depth
    if (depth >= MAX_DEPTH) {
      const keyCount = Object.keys(obj).length;
      result[prefix] = `{${keyCount} keys}`;
      return;
    }
    for (const [key, val] of Object.entries(obj)) {
      // Stop if we hit the max keys limit
      if (Object.keys(result).length >= MAX_KEYS) {
        truncatedCount.value += 1;
        return;
      }
      const newKey = prefix ? `${prefix}.${key}` : key;
      flatten(val, newKey, depth + 1);
    }
  }

  flatten(props.data, "", 0);
  return result;
});

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  if (typeof value === "string") {
    if (value.length > MAX_STRING_LEN) {
      return value.slice(0, MAX_STRING_LEN) + "…";
    }
    return value;
  }
  return JSON.stringify(value);
}
</script>
