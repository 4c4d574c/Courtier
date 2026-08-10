import { ref } from "vue";

const THEME_KEY = "chat-theme";

// Light/dark theme is driven by the data-theme attribute on <html>; the
// choice persists in localStorage so it survives reloads and applies before
// login (auth pages) as well. The ref is module-level so every consumer
// (chat header, auth pages) sees the same current value.
const theme = ref<"light" | "dark">("light");

export function useTheme() {
  function initTheme() {
    const saved = localStorage.getItem(THEME_KEY);
    theme.value = saved === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme.value);
  }

  function toggleTheme() {
    theme.value = theme.value === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", theme.value);
    localStorage.setItem(THEME_KEY, theme.value);
  }

  return { theme, initTheme, toggleTheme };
}
