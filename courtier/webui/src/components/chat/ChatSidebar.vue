<template>
  <aside class="chat-sidebar" :class="{ 'chat-sidebar--open': isOpen }">
    <div class="chat-sidebar-inner">
      <div class="chat-sidebar-header">
        <div class="chat-sidebar-logo">
          <span class="chat-sidebar-logo-mark"
            ><img :src="logoUrl" alt="审衡"
          /></span>
          <span class="chat-sidebar-logo-text">审衡</span>
        </div>
        <button
          class="chat-sidebar-new"
          type="button"
          @click="$emit('new-session')"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 5v14M5 12h14" />
          </svg>
          {{ MESSAGES.CHAT_NEW_SESSION }}
        </button>
      </div>

      <div
        class="chat-sidebar-section chat-sidebar-section--history"
        :class="{ 'chat-sidebar-section--collapsed': historyCollapsed }"
      >
        <button
          class="chat-sidebar-section-toggle"
          type="button"
          :aria-expanded="!historyCollapsed"
          @click="toggleHistory"
        >
          <span class="chat-sidebar-section-title">{{
            MESSAGES.CHAT_HISTORY_TITLE
          }}</span>
          <svg
            class="chat-sidebar-section-chevron"
            :class="{
              'chat-sidebar-section-chevron--collapsed': historyCollapsed,
            }"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
        <Transition
          :css="false"
          @before-enter="onCollapseBeforeEnter"
          @enter="onCollapseEnter"
          @after-enter="onCollapseAfterEnter"
          @before-leave="onCollapseBeforeLeave"
          @leave="onCollapseLeave"
        >
          <div
            v-if="!historyCollapsed"
            class="chat-sidebar-collapsible chat-sidebar-collapsible--history"
          >
        <div v-if="listError" class="chat-sidebar-list-error" role="status">
          {{ listError }}
        </div>
        <div v-if="loading" class="chat-sidebar-loading">{{ MESSAGES.LOADING }}</div>
        <div v-else-if="sessions.length === 0" class="chat-sidebar-empty">
          {{ MESSAGES.CHAT_NO_HISTORY }}
        </div>
        <ul
          v-else
          ref="listEl"
          class="chat-sidebar-list"
          @scroll="updateAtBottom"
        >
          <li
            v-for="session in visibleSessions"
            :key="session.id"
            class="chat-sidebar-item"
            @click="$emit('select', session.id)"
          >
            <input
              v-if="editingId === session.id"
              ref="renameInput"
              v-model="editingText"
              class="chat-sidebar-item-rename"
              @click.stop
              @keydown.enter.prevent="commitRename"
              @keydown.esc.prevent="cancelRename"
              @blur="commitRename"
            />
            <template v-else>
              <span class="chat-sidebar-item-task">
                <span
                  v-if="session.status === 'running'"
                  class="chat-sidebar-item-spinner"
                  aria-hidden="true"
                >
                  <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8">
                    <circle cx="8" cy="8" r="6" opacity="0.25" />
                    <path d="M14 8A6 6 0 0 0 8 2" stroke-linecap="round" />
                  </svg>
                </span>
                <svg
                  v-if="session.pinned"
                  class="chat-sidebar-item-pin"
                  viewBox="0 0 16 16"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    d="M9.5 1.3 8 2.8l4.2 4.2 1.5-1.5L9.5 1.3zM7 4 3.6 7.4l.7.7L8 4.7 7 4zm2 2-4.6 4.6-1.9 3.9.7.7 3.9-1.9L11.7 8 9 6z"
                  />
                </svg>
                {{ session.task }}
              </span>
              <span class="chat-sidebar-item-right">
                <span
                  v-if="session.status === 'queued'"
                  class="chat-sidebar-item-queue"
                  >+{{ session.queuePosition ?? 0 }}</span
                >
                <button
                  class="chat-sidebar-item-menu-btn"
                  :class="{
                    'chat-sidebar-item-menu-btn--open': openMenuId === session.id,
                  }"
                  type="button"
                :aria-label="MESSAGES.CHAT_MENU_MORE"
                @click.stop="toggleMenu(session.id, $event)"
                >
                  <AppIcon name="dots" :size="14" />
                </button>
              </span>
            </template>
            <div
              v-if="openMenuId === session.id"
              class="chat-sidebar-item-menu"
              :class="{ 'chat-sidebar-item-menu--up': menuUp }"
              @click.stop
            >
              <button
                type="button"
                class="chat-sidebar-item-menu-item"
                @click="startRename(session)"
              >
                {{ MESSAGES.CHAT_MENU_RENAME }}
              </button>
              <button
                type="button"
                class="chat-sidebar-item-menu-item"
                @click="togglePin(session)"
              >
                {{ session.pinned ? MESSAGES.CHAT_MENU_UNPIN : MESSAGES.CHAT_MENU_PIN }}
              </button>
              <button
                type="button"
                class="chat-sidebar-item-menu-item chat-sidebar-item-menu-item--danger"
                @click="onMenuDelete(session.id)"
              >
                {{ MESSAGES.CHAT_MENU_DELETE }}
              </button>
            </div>
          </li>
        </ul>
        <Transition name="more-reveal">
          <div v-if="hiddenCount > 0 && atBottom" class="chat-sidebar-more">
            <div class="chat-sidebar-more-fade" aria-hidden="true"></div>
            <button type="button" class="chat-sidebar-more-btn" @click="showMoreSessions">
              <svg
                class="chat-sidebar-more-chevron"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                aria-hidden="true"
              >
                <path d="M6 9l6 6 6-6" />
              </svg>
              {{ MESSAGES.CHAT_SHOW_MORE }}
              <span class="chat-sidebar-more-count">{{ hiddenCount }}</span>
            </button>
          </div>
        </Transition>
        <div
          v-if="openMenuId"
          class="chat-sidebar-menu-overlay"
          @click="closeMenu"
        ></div>
          </div>
        </Transition>
      </div>

      <!-- Sidebar footer: resource-library entry (non-admins only — admins
           have it under 管理) and the user menu moved here from the header
           (bottom-left corner). -->
      <div class="chat-sidebar-footer">
        <router-link v-if="!isAdmin" to="/admin/resources" class="chat-sidebar-footer-item">
          资源库
        </router-link>
        <div class="chat-sidebar-user">
          <div class="chat-sidebar-user-main" @click="userMenuOpen = !userMenuOpen">
            <span class="chat-sidebar-user-name">{{ username || "用户" }}</span>
          </div>
          <router-link
            :to="settingsTarget"
            class="chat-sidebar-user-gear"
            title="设置"
            aria-label="设置"
            @click.stop
          >
            <AppIcon name="gear" :size="17" />
          </router-link>
          <div v-if="userMenuOpen" class="chat-sidebar-user-menu">
            <div
              class="chat-sidebar-user-menu-item chat-sidebar-theme-row"
              @click.stop="themeMenuOpen = !themeMenuOpen"
            >
              <AppIcon class="chat-sidebar-theme-icon" name="contrast" :size="16" />
              <span class="chat-sidebar-theme-label">{{ MESSAGES.CHAT_THEME }}</span>
              <AppIcon class="chat-sidebar-theme-caret" :class="{ 'chat-sidebar-theme-caret--open': themeMenuOpen }" name="caret-down" :size="14" />
            </div>
            <div v-if="themeMenuOpen" class="chat-sidebar-theme-options">
              <button
                class="chat-sidebar-user-menu-item chat-sidebar-theme-option"
                type="button"
                @click.stop="pickTheme('light')"
              >
                <svg class="chat-sidebar-theme-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <circle cx="12" cy="12" r="5" />
                  <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
                </svg>
                <span>{{ MESSAGES.CHAT_THEME_LIGHT }}</span>
                <svg v-if="theme === 'light'" class="chat-sidebar-theme-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              </button>
              <button
                class="chat-sidebar-user-menu-item chat-sidebar-theme-option"
                type="button"
                @click.stop="pickTheme('dark')"
              >
                <svg class="chat-sidebar-theme-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
                <span>{{ MESSAGES.CHAT_THEME_DARK }}</span>
                <svg v-if="theme === 'dark'" class="chat-sidebar-theme-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
                  <path d="M20 6L9 17l-5-5" />
                </svg>
              </button>
            </div>
            <router-link to="/profile" class="chat-sidebar-user-menu-item" @click.stop>
              {{ MESSAGES.CHAT_SETTINGS }}
            </router-link>
            <button class="chat-sidebar-user-menu-item" type="button" @click.stop="logout">
              {{ MESSAGES.CHAT_LOGOUT }}
            </button>
          </div>
        </div>
        <div
          v-if="userMenuOpen"
          class="chat-sidebar-menu-overlay"
          @click="userMenuOpen = false"
        ></div>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { SessionSummary } from "../../types/agent";
import { MESSAGES } from "../../constants/messages";
import { useTheme } from "../../composables/useTheme";
import logoUrl from "../../assets/logo.png";
import AppIcon from "../AppIcon.vue";

interface Props {
  sessions: SessionSummary[];
  loading: boolean;
  /** Non-empty = last list refresh failed; old sessions stay rendered. */
  listError?: string | null;
  isOpen: boolean;
  userRole?: string;
  /** Display name for the bottom-left user menu. */
  username?: string;
}

const props = defineProps<Props>();
const emit = defineEmits<{
  "new-session": [];
  select: [id: string];
  delete: [id: string];
  rename: [payload: { id: string; task: string }];
  pin: [payload: { id: string; pinned: boolean }];
  logout: [];
}>();

const isAdmin = computed(() => props.userRole === "admin");

// Bottom-left user menu (moved from the header). The invisible overlay
// closes it on any outside click — same pattern as the meatballs menus.
const userMenuOpen = ref(false);
const themeMenuOpen = ref(false);
const { theme, setTheme } = useTheme();

// Gear entry: admins land in the settings shell (redirects to 用户管理);
// regular users go straight to the only section they can access.
const settingsTarget = computed(() => (isAdmin.value ? "/admin" : "/admin/resources"));

function pickTheme(value: "light" | "dark") {
  setTheme(value);
  userMenuOpen.value = false;
  themeMenuOpen.value = false;
}

function logout() {
  userMenuOpen.value = false;
  themeMenuOpen.value = false;
  emit("logout");
}

// Long history lists render the first batch with a show-more expander
// instead of mounting every session row (GET /sessions returns them all).
const INITIAL_VISIBLE_SESSIONS = 20;
const SESSIONS_BATCH = 20;
const visibleCount = ref(INITIAL_VISIBLE_SESSIONS);
const visibleSessions = computed(() => props.sessions.slice(0, visibleCount.value));
const hiddenCount = computed(() => Math.max(0, props.sessions.length - visibleCount.value));

// The expander stays out of the way until the user scrolls the current
// batch to its end; scrolling back up hides it again.
const listEl = ref<HTMLElement | null>(null);
const atBottom = ref(false);

function updateAtBottom() {
  const el = listEl.value;
  // A batch that fits without scrolling counts as "at the bottom".
  atBottom.value = !el || el.scrollHeight - el.scrollTop - el.clientHeight <= 4;
}

watch([listEl, visibleSessions, () => props.loading], () => nextTick(updateAtBottom));

function showMoreSessions() {
  visibleCount.value += SESSIONS_BATCH;
}

// Meatballs menu: only one open at a time; an invisible overlay closes it
// on any outside click (no global listeners needed).
const openMenuId = ref<string | null>(null);
// Menus anchored near the bottom of the scrollable list would be clipped
// away by its overflow — flip those upward instead.
const menuUp = ref(false);

function toggleMenu(id: string, event: Event) {
  if (openMenuId.value === id) {
    closeMenu();
    return;
  }
  const item = (event.currentTarget as HTMLElement).closest(
    ".chat-sidebar-item",
  ) as HTMLElement | null;
  const list = listEl.value;
  menuUp.value =
    !!item &&
    !!list &&
    list.getBoundingClientRect().bottom - item.getBoundingClientRect().bottom < 120;
  openMenuId.value = id;
}

function closeMenu() {
  openMenuId.value = null;
}

// History section can be folded to make room for the entries below it;
// the collapsed state persists across reloads.
const HISTORY_COLLAPSED_KEY = "chat-sidebar-history-collapsed";
const historyCollapsed = ref(
  localStorage.getItem(HISTORY_COLLAPSED_KEY) === "1",
);

function toggleHistory() {
  historyCollapsed.value = !historyCollapsed.value;
  localStorage.setItem(
    HISTORY_COLLAPSED_KEY,
    historyCollapsed.value ? "1" : "0",
  );
  if (historyCollapsed.value) closeMenu();
}

// Height animation for the fold/unfold of dynamic-height sections. CSS can
// transition max-height; JS supplies the measured endpoints. The history
// list is layout-capped (it scrolls internally when long), so the leave
// hook freezes the *visible* height rather than scrollHeight.
const COLLAPSE_DURATION = 250; // ms — keep in sync with the CSS transition

function onCollapseBeforeEnter(el: Element) {
  const e = el as HTMLElement;
  e.style.maxHeight = "0px";
  e.style.opacity = "0";
}

function onCollapseEnter(el: Element, done: () => void) {
  const e = el as HTMLElement;
  void e.offsetHeight; // force reflow so the transition picks up the change
  e.style.maxHeight = `${e.scrollHeight}px`;
  e.style.opacity = "1";
  setTimeout(done, COLLAPSE_DURATION);
}

function onCollapseAfterEnter(el: Element) {
  const e = el as HTMLElement;
  // Release the cap so layout (flex/shrink) sizes the element again.
  e.style.maxHeight = "";
  e.style.opacity = "";
}

function onCollapseBeforeLeave(el: Element) {
  const e = el as HTMLElement;
  e.style.maxHeight = `${e.offsetHeight}px`;
}

function onCollapseLeave(el: Element, done: () => void) {
  const e = el as HTMLElement;
  void e.offsetHeight;
  e.style.maxHeight = "0px";
  e.style.opacity = "0";
  setTimeout(done, COLLAPSE_DURATION);
}

// Inline rename: the title swaps to an input; Enter/blur commits, Esc cancels.
const editingId = ref<string | null>(null);
const editingText = ref("");
const renameInput = ref<HTMLInputElement[] | null>(null);

function startRename(session: SessionSummary) {
  closeMenu();
  editingId.value = session.id;
  editingText.value = session.task;
  nextTick(() => {
    const el = renameInput.value?.[0];
    el?.focus();
    el?.select();
  });
}

function commitRename() {
  const id = editingId.value;
  if (!id) return;
  editingId.value = null;
  const task = editingText.value.trim();
  const original = props.sessions.find((s) => s.id === id)?.task;
  if (task && task !== original) {
    emit("rename", { id, task });
  }
}

function cancelRename() {
  editingId.value = null;
}

function togglePin(session: SessionSummary) {
  closeMenu();
  emit("pin", { id: session.id, pinned: !session.pinned });
}

function onMenuDelete(id: string) {
  closeMenu();
  emit("delete", id);
}
</script>

<style scoped>
.chat-sidebar {
  width: var(--chat-sidebar-width);
  flex-shrink: 0;
  background: var(--chat-bg-card);
  border-right: 1px solid var(--chat-border);
  height: 100%;
  overflow: hidden;
  transition:
    width 0.25s ease,
    transform 0.25s ease;
}

/* Collapsed (desktop): width animates to zero; the fixed-width inner
   wrapper keeps the content from squishing during the transition. */
.chat-sidebar:not(.chat-sidebar--open) {
  width: 0;
  border-right-color: transparent;
}

.chat-sidebar-inner {
  width: var(--chat-sidebar-width);
  height: 100%;
  display: flex;
  flex-direction: column;
}

.chat-sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--chat-border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.chat-sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
}

.chat-sidebar-logo-mark {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 2px solid var(--chat-accent);
  border-radius: var(--chat-radius-sm);
  overflow: hidden;
}

.chat-sidebar-logo-mark img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.chat-sidebar-logo-text {
  font-size: 20px;
  font-weight: 600;
  color: var(--chat-text-primary);
}

.chat-sidebar-new {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 12px;
  border-radius: var(--chat-radius-md);
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  font-size: 15px;
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-new:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-new svg {
  width: 16px;
  height: 16px;
}

.chat-sidebar-section {
  padding: 12px 12px 4px;
}

/* History section fills the remaining height; the list scrolls inside it. */
.chat-sidebar-section--history {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* Collapsed: the section shrinks to just the toggle row. */
.chat-sidebar-section--history.chat-sidebar-section--collapsed {
  flex: 0 0 auto;
}

/* Fold/unfold animation: JS hooks drive inline max-height endpoints, this
   provides the timing. Duration must match COLLAPSE_DURATION in the script. */
.chat-sidebar-collapsible {
  overflow: hidden;
  transition:
    max-height 0.25s ease,
    opacity 0.2s ease;
}

/* History variant: fills the section so the list inside keeps scrolling. */
.chat-sidebar-collapsible--history {
  position: relative;
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

.chat-sidebar-section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--chat-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 8px;
  padding: 0 4px;
}

/* Section header doubles as the fold/unfold toggle. */
.chat-sidebar-section-toggle {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0;
  margin-bottom: 8px;
  border: none;
  background: transparent;
  cursor: pointer;
}

.chat-sidebar-section-toggle .chat-sidebar-section-title {
  margin-bottom: 0;
}

.chat-sidebar-section--collapsed .chat-sidebar-section-toggle {
  margin-bottom: 0;
}

.chat-sidebar-section-toggle:hover .chat-sidebar-section-title,
.chat-sidebar-section-toggle:hover .chat-sidebar-section-chevron {
  color: var(--chat-text-secondary);
}

.chat-sidebar-section-chevron {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  margin-right: 4px;
  color: var(--chat-text-tertiary);
  transition: transform 0.15s;
}

.chat-sidebar-section-chevron--collapsed {
  transform: rotate(-90deg);
}

.chat-sidebar-loading,
.chat-sidebar-empty {
  padding: 12px 4px;
  font-size: 15px;
  color: var(--chat-text-secondary);
}

.chat-sidebar-list-error {
  margin: 0 4px 8px;
  padding: 6px 10px;
  border: 1px solid var(--chat-border);
  border-left: 3px solid #d97706;
  border-radius: var(--chat-radius-xs, 6px);
  background: var(--chat-bg-elevated);
  color: var(--chat-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.chat-sidebar-list {
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}

.chat-sidebar-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-item-task {
  flex: 1;
  min-width: 0;
  font-size: 15px;
  color: var(--chat-text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.chat-sidebar-item-pin {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: 4px;
  vertical-align: -1px;
  color: var(--chat-accent);
}

/* Live run indicator in front of the title (status goes live via the
   global run-events channel, not just the mount-time list snapshot). */
.chat-sidebar-item-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  margin-right: 4px;
  vertical-align: -1px;
  color: var(--chat-accent);
  animation: chat-sidebar-spin 0.9s linear infinite;
}

.chat-sidebar-item-spinner svg {
  width: 100%;
  height: 100%;
  display: block;
}

@keyframes chat-sidebar-spin {
  to {
    transform: rotate(360deg);
  }
}

.chat-sidebar-item-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

.chat-sidebar-item-queue {
  font-size: 10px;
  color: #60a5fa;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

.chat-sidebar-item-menu-btn {
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 0 2px;
  border: none;
  background: transparent;
  color: var(--chat-text-tertiary);
  font-size: 18px;
  line-height: 1;
  border-radius: var(--chat-radius-sm);
  opacity: 0;
  cursor: pointer;
  transition:
    opacity 0.15s,
    background 0.15s;
}

/* Show-more: floats over the bottom of the list (absolutely positioned, so
   the scrollbar keeps the full height). The fade spans the whole overlay and
   reaches full opacity before the button row, masking the session text
   underneath. The wrapper ignores pointer events; only the button is
   clickable — rows in the fade strip stay clickable. */
.chat-sidebar-more {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  display: flex;
  justify-content: center;
  padding-top: 28px;
  pointer-events: none;
}

.chat-sidebar-more-fade {
  position: absolute;
  inset: 0;
  background: linear-gradient(to bottom, transparent, var(--chat-bg-card) 60%);
  pointer-events: none;
}

.chat-sidebar-more-btn {
  position: relative;
  pointer-events: auto;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 14px;
  border: none;
  border-radius: var(--chat-radius-md);
  background: transparent;
  color: var(--chat-text-tertiary);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s;
}

.chat-sidebar-more-btn:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-secondary);
}

.chat-sidebar-more-chevron {
  width: 12px;
  height: 12px;
}

.chat-sidebar-more-count {
  color: var(--chat-text-tertiary);
  opacity: 0.75;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.more-reveal-enter-active,
.more-reveal-leave-active {
  transition:
    opacity 0.15s ease,
    transform 0.15s ease;
}

.more-reveal-enter-from,
.more-reveal-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

.chat-sidebar-item:hover .chat-sidebar-item-menu-btn,
.chat-sidebar-item-menu-btn--open {
  opacity: 1;
}

.chat-sidebar-item-menu-btn:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

/* Touch devices have no hover — keep the meatballs always visible. */
@media (hover: none) {
  .chat-sidebar-item-menu-btn {
    opacity: 1;
  }
}

.chat-sidebar-item-menu {
  position: absolute;
  top: 34px;
  right: 8px;
  z-index: 11;
  min-width: 104px;
  display: flex;
  flex-direction: column;
  padding: 4px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
}

.chat-sidebar-item-menu--up {
  top: auto;
  bottom: 34px;
}

.chat-sidebar-item-menu-item {
  padding: 7px 10px;
  border: none;
  background: transparent;
  text-align: left;
  font-size: 14px;
  color: var(--chat-text-primary);
  border-radius: var(--chat-radius-sm);
  cursor: pointer;
}

.chat-sidebar-item-menu-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-item-menu-item--danger {
  color: #ef4444;
}

.chat-sidebar-item-menu-item--danger:hover {
  background: rgba(239, 68, 68, 0.1);
}

.chat-sidebar-menu-overlay {
  position: fixed;
  inset: 0;
  z-index: 10;
}

.chat-sidebar-item-rename {
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  font-size: 15px;
  color: var(--chat-text-primary);
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-accent);
  border-radius: var(--chat-radius-sm);
  outline: none;
}

.chat-sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sidebar-nav-item {
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  font-size: 15px;
  color: var(--chat-text-primary);
  text-decoration: none;
  transition: background 0.15s;
}

.chat-sidebar-nav-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-footer {
  margin-top: auto;
  border-top: 1px solid var(--chat-border);
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sidebar-footer-item {
  padding: 8px 10px;
  border-radius: var(--chat-radius-md);
  font-size: 15px;
  color: var(--chat-text-primary);
  text-decoration: none;
  text-align: left;
  background: transparent;
  border: none;
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-footer-item:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-user {
  position: relative;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 4px 4px 0;
  border-radius: var(--chat-radius-md);
}

.chat-sidebar-user-main {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 6px;
  border-radius: var(--chat-radius-md);
  cursor: pointer;
}

.chat-sidebar-user-main:hover {
  background: var(--chat-bg-hover);
}

.chat-sidebar-user-gear {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  border-radius: var(--chat-radius-md);
  color: var(--chat-text-secondary);
  transition: background 0.15s, color 0.15s;
}

.chat-sidebar-user-gear:hover {
  background: var(--chat-bg-hover);
  color: var(--chat-text-primary);
}

.chat-sidebar-user-gear svg {
  width: 17px;
  height: 17px;
}

.chat-sidebar-user-name {
  font-size: 15px;
  color: var(--chat-text-primary);
}

/* Opens upward — the menu sits at the very bottom of the viewport. */
.chat-sidebar-user-menu {
  position: absolute;
  bottom: calc(100% + 6px);
  left: 0;
  min-width: 140px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
  overflow: hidden;
  z-index: 100;
}

.chat-sidebar-user-menu-item {
  display: block;
  width: 100%;
  padding: 10px 14px;
  font-size: 15px;
  color: var(--chat-text-primary);
  text-decoration: none;
  text-align: left;
  background: transparent;
  border: none;
  cursor: pointer;
  transition: background 0.15s;
}

.chat-sidebar-user-menu-item:hover {
  background: var(--chat-bg-hover);
}

/* 界面主题 row: icon + label + caret, expands its options accordion-style
   (the sidebar's overflow:hidden clips a flyout, so the options render
   inline under the row like the reference submenu, indented). */
.chat-sidebar-theme-row {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.chat-sidebar-theme-label {
  flex: 1;
}

.chat-sidebar-theme-caret {
  font-size: 10px;
  color: var(--chat-text-secondary);
  transition: transform 0.15s;
}

.chat-sidebar-theme-caret--open {
  transform: rotate(180deg);
}

.chat-sidebar-theme-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  color: var(--chat-text-secondary);
}

.chat-sidebar-theme-options {
  display: flex;
  flex-direction: column;
  padding: 2px 0 4px 10px;
}

.chat-sidebar-theme-option {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  cursor: pointer;
  border-radius: var(--chat-radius-sm, 4px);
}

.chat-sidebar-theme-option span {
  flex: 1;
  text-align: left;
}

.chat-sidebar-theme-check {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  color: var(--chat-accent);
}

@media (max-width: 768px) {
  .chat-sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 100;
    transform: translateX(-100%);
  }
  .chat-sidebar--open {
    transform: translateX(0);
  }
}
</style>
