import { ref } from "vue";
import type { ChatFileItem } from "../types/chat";

export function useFilePreview() {
  const isOpen = ref(false);
  const currentFile = ref<ChatFileItem | null>(null);

  function open(file: ChatFileItem) {
    currentFile.value = file;
    isOpen.value = true;
  }

  function close() {
    isOpen.value = false;
    currentFile.value = null;
  }

  return { isOpen, currentFile, open, close };
}
