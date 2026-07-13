import { computed } from "vue";
import { useToggleSet } from "./useToggleSet";

export function useToolCard() {
  const {
    expanded: expandedCards,
    toggle,
    isExpanded,
    expandAll: _expandAll,
    closeAll,
  } = useToggleSet();

  function expandAll(names: string[]) {
    _expandAll(names);
  }

  const anyExpanded = computed(() => expandedCards.value.size > 0);

  return {
    toggle,
    isExpanded,
    expandedCards,
    expandAll,
    closeAll,
    anyExpanded,
  };
}
