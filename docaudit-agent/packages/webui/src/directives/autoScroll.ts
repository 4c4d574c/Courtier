import type { Directive } from "vue";

interface AutoScrollOptions {
  enabled?: boolean;
}

function normalize(value: unknown): { enabled: boolean } {
  if (typeof value === "boolean") {
    return { enabled: value };
  }
  if (value && typeof value === "object") {
    return { enabled: (value as AutoScrollOptions).enabled ?? false };
  }
  return { enabled: false };
}

function scrollToBottom(el: HTMLElement): void {
  el.scrollTop = el.scrollHeight;
}

const AutoScrollDirective: Directive<
  HTMLElement,
  AutoScrollOptions | boolean | undefined
> = {
  mounted(el, binding) {
    const options = normalize(binding.value);
    (el as unknown as Record<string, unknown>).__autoScrollOptions = options;

    if (options.enabled) {
      scrollToBottom(el);
    }

    const observer = new MutationObserver(() => {
      const opts = (el as unknown as Record<string, { enabled: boolean }>)
        .__autoScrollOptions;
      if (opts?.enabled) {
        scrollToBottom(el);
      }
    });

    observer.observe(el, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    (el as unknown as Record<string, unknown>).__autoScrollObserver = observer;
  },

  updated(el, binding) {
    const options = normalize(binding.value);
    const prev = normalize(binding.oldValue);
    (el as unknown as Record<string, unknown>).__autoScrollOptions = options;

    if (options.enabled && !prev.enabled) {
      scrollToBottom(el);
    }
  },

  unmounted(el) {
    const observer = (
      el as unknown as Record<string, MutationObserver | undefined>
    ).__autoScrollObserver;
    if (observer) {
      observer.disconnect();
    }
  },
};

export default AutoScrollDirective;
