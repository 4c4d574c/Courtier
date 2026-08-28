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

/**
 * Pin an inner scroll container to its bottom while its content streams.
 *
 * Writes are rAF-coalesced (streaming markdown swaps the body's DOM every
 * frame — writing per mutation meant multiple forced layouts per frame) and
 * gated on the user staying near the bottom, matching the chat area's
 * follow-bottom semantics: scrolling up keeps the position readable instead
 * of being yanked back on every mutation.
 */
const AutoScrollDirective: Directive<
  HTMLElement,
  AutoScrollOptions | boolean | undefined
> = {
  mounted(el, binding) {
    const options = normalize(binding.value);
    (el as unknown as Record<string, unknown>).__autoScrollOptions = options;

    let atBottom = true;
    let rafId: number | null = null;

    const isNearBottom = () =>
      el.scrollTop + el.clientHeight >= el.scrollHeight - 30;

    function scheduleScrollWrite(): void {
      if (rafId !== null) return;
      rafId = requestAnimationFrame(() => {
        rafId = null;
        if (atBottom) {
          el.scrollTop = el.scrollHeight;
        }
      });
    }

    function onScroll(): void {
      // The write itself fires a scroll event; coalesce re-checks per frame.
      if (rafId === null) {
        rafId = requestAnimationFrame(() => {
          rafId = null;
          atBottom = isNearBottom();
        });
      }
    }

    if (options.enabled) {
      el.scrollTop = el.scrollHeight;
    }

    el.addEventListener("scroll", onScroll, { passive: true });

    const observer = new MutationObserver(() => {
      const opts = (el as unknown as Record<string, { enabled: boolean }>)
        .__autoScrollOptions;
      if (opts?.enabled) {
        scheduleScrollWrite();
      }
    });

    observer.observe(el, {
      childList: true,
      subtree: true,
      characterData: true,
    });

    (el as unknown as Record<string, unknown>).__autoScrollObserver = observer;
    (el as unknown as Record<string, unknown>).__autoScrollDetach = () => {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      el.removeEventListener("scroll", onScroll);
      observer.disconnect();
    };
  },

  updated(el, binding) {
    const options = normalize(binding.value);
    const prev = normalize(binding.oldValue);
    (el as unknown as Record<string, unknown>).__autoScrollOptions = options;

    if (options.enabled && !prev.enabled) {
      el.scrollTop = el.scrollHeight;
    }
  },

  unmounted(el) {
    const detach = (
      el as unknown as Record<string, (() => void) | undefined>
    ).__autoScrollDetach;
    detach?.();
  },
};

export default AutoScrollDirective;
