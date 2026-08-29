import { ref, watch, type Ref } from "vue";

/**
 * Shared expand/collapse state for one collapsible block.
 *
 * With `active`, the block follows the app-wide streaming semantics: open
 * while `active()` is true, auto-collapses when activity ends — unless the
 * user has manually toggled, after which their choice stands. Without it,
 * a plain disclosure. The indicator is DisclosureChevron (right=collapsed,
 * down=open); keyed multi-item state stays in useToggleSet.
 */
export interface Disclosure {
  isOpen: Ref<boolean>;
  toggle(): void;
  set(open: boolean): void;
}

export function useDisclosure(options: { active?: () => boolean } = {}): Disclosure {
  const isOpen = ref(options.active ? options.active() : false);
  let userToggled = false;

  function toggle(): void {
    userToggled = true;
    isOpen.value = !isOpen.value;
  }

  function set(open: boolean): void {
    isOpen.value = open;
  }

  if (options.active) {
    watch(options.active, (now, was) => {
      if (was && !now && !userToggled) isOpen.value = false;
    });
  }

  return { isOpen, toggle, set };
}
