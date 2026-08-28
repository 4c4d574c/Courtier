import { ref } from "vue";
import type { CitationHit } from "../types/agent";
import type { ChatFileItem } from "../types/chat";

export function useFilePreview() {
  const isOpen = ref(false);
  const currentFile = ref<ChatFileItem | null>(null);
  const currentCitation = ref<CitationHit | null>(null);

  function open(file: ChatFileItem) {
    currentFile.value = file;
    currentCitation.value = null;
    isOpen.value = true;
  }

  /** Open the drawer in citation mode (search hit → card + PDF preview). */
  function openCitation(citation: CitationHit | undefined) {
    if (!citation) return;
    currentCitation.value = citation;
    currentFile.value = null;
    isOpen.value = true;
  }

  /**
   * Collapse the panel. Content refs are intentionally kept so the panel
   * doesn't blank out during the slide-out animation — the next open()
   * replaces them anyway.
   */
  function close() {
    isOpen.value = false;
  }

  return { isOpen, currentFile, currentCitation, open, openCitation, close };
}
