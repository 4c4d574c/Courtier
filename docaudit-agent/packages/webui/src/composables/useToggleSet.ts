import { ref, type Ref } from "vue";

export interface UseToggleSetOptions {
  /** When true, every key is expanded by default and the internal set tracks
   *  keys that have been explicitly collapsed. */
  defaultExpanded?: boolean;
}

/** A reactive Set<string>-based toggle for collapsible UI widgets. */
export function useToggleSet(options: UseToggleSetOptions = {}): {
  expanded: Ref<Set<string>>;
  toggle(key: string): void;
  isExpanded(key: string): boolean;
  expandAll(keys: Iterable<string>): void;
  closeAll(keys?: Iterable<string>): void;
} {
  const defaultExpanded = options.defaultExpanded ?? false;
  const expanded = ref<Set<string>>(new Set());

  function toggle(key: string): void {
    const next = new Set(expanded.value);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
    }
    expanded.value = next;
  }

  function isExpanded(key: string): boolean {
    return defaultExpanded ? !expanded.value.has(key) : expanded.value.has(key);
  }

  function expandAll(keys: Iterable<string>): void {
    expanded.value = defaultExpanded ? new Set() : new Set(keys);
  }

  function closeAll(keys?: Iterable<string>): void {
    expanded.value = defaultExpanded ? new Set(keys ?? []) : new Set();
  }

  return { expanded, toggle, isExpanded, expandAll, closeAll };
}
