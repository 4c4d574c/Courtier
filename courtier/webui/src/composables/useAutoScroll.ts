import { ref, watch, nextTick, type Ref } from "vue";

export function useAutoScroll() {
  const containerRef = ref<HTMLElement>();
  let isAtBottom = true;
  // rAF-coalesced: scroll events fire per frame while streaming writes
  // scrollTop every flush — read the layout at most once per frame.
  let scrollRafId: number | null = null;

  function onScroll() {
    if (scrollRafId !== null) return;
    scrollRafId = requestAnimationFrame(() => {
      scrollRafId = null;
      const el = containerRef.value;
      if (!el) return;
      isAtBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    });
  }

  function cancelScrollRaf() {
    if (scrollRafId !== null) {
      cancelAnimationFrame(scrollRafId);
      scrollRafId = null;
    }
  }

  function scrollToBottom() {
    if (isAtBottom && containerRef.value) {
      containerRef.value.scrollTop = containerRef.value.scrollHeight;
    }
  }

  function forceScrollToBottom() {
    isAtBottom = true;
    if (containerRef.value) {
      containerRef.value.scrollTop = containerRef.value.scrollHeight;
    }
  }

  function mount() {
    const el = containerRef.value;
    if (el) {
      el.addEventListener("scroll", onScroll);
      return;
    }
    // The container may render later (v-if) — attach once it appears,
    // otherwise the listener would never be registered.
    const stop = watch(containerRef, (newEl) => {
      if (newEl) {
        newEl.addEventListener("scroll", onScroll);
        stop();
      }
    });
  }

  function unmount() {
    cancelScrollRaf();
    containerRef.value?.removeEventListener("scroll", onScroll);
  }

  /** Watch a ref or getter and auto-scroll on change. */
  function watchForScroll(source: Ref<unknown> | (() => unknown)) {
    watch(source, () => {
      nextTick(scrollToBottom);
    });
  }

  return {
    containerRef,
    scrollToBottom,
    forceScrollToBottom,
    mount,
    unmount,
    watchForScroll,
  };
}
