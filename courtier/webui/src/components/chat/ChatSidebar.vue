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
        v-if="isAdmin"
        class="chat-sidebar-section"
        :class="{ 'chat-sidebar-section--collapsed': adminCollapsed }"
      >
        <button
          class="chat-sidebar-section-toggle"
          type="button"
          :aria-expanded="!adminCollapsed"
          @click="toggleAdmin"
        >
          <span class="chat-sidebar-section-title">管理</span>
          <svg
            class="chat-sidebar-section-chevron"
            :class="{
              'chat-sidebar-section-chevron--collapsed': adminCollapsed,
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
          <nav
            v-if="!adminCollapsed"
            class="chat-sidebar-nav chat-sidebar-collapsible"
          >
          <router-link to="/admin/users" class="chat-sidebar-nav-item">
            {{ MESSAGES.CHAT_USER_MANAGE }}
          </router-link>
          <router-link to="/admin/approvals" class="chat-sidebar-nav-item">
            {{ MESSAGES.CHAT_APPROVALS }}
          </router-link>
          <router-link to="/admin/extensions" class="chat-sidebar-nav-item">
            插件与技能
          </router-link>
          <router-link to="/resources" class="chat-sidebar-nav-item">
            资源库
          </router-link>
          </nav>
        </Transition>
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
        <div v-if="loading" class="chat-sidebar-loading">{{ MESSAGES.LOADING }}</div>
        <div v-else-if="sessions.length === 0" class="chat-sidebar-empty">
          {{ MESSAGES.CHAT_NO_HISTORY }}
        </div>
        <ul v-else class="chat-sidebar-list">
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
                  @click.stop="toggleMenu(session.id)"
                >
                  ⋯
                </button>
              </span>
            </template>
            <div
              v-if="openMenuId === session.id"
              class="chat-sidebar-item-menu"
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
        <div v-if="hiddenCount > 0" class="chat-sidebar-more">
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
        <div
          v-if="openMenuId"
          class="chat-sidebar-menu-overlay"
          @click="closeMenu"
        ></div>
          </div>
        </Transition>
      </div>

      <!-- Non-admins have no 管理 section; keep their resource library entry. -->
      <div v-if="!isAdmin" class="chat-sidebar-footer">
        <router-link to="/resources" class="chat-sidebar-footer-item">
          资源库
        </router-link>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import type { SessionSummary } from "../../types/agent";
import { MESSAGES } from "../../constants/messages";
import logoUrl from "../../assets/logo.png";

interface Props {
  sessions: SessionSummary[];
  loading: boolean;
  isOpen: boolean;
  userRole?: string;
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

// Long history lists render the first batch with a show-more expander
// instead of mounting every session row (GET /sessions returns them all).
const INITIAL_VISIBLE_SESSIONS = 20;
const SESSIONS_BATCH = 20;
const visibleCount = ref(INITIAL_VISIBLE_SESSIONS);
const visibleSessions = computed(() => props.sessions.slice(0, visibleCount.value));
const hiddenCount = computed(() => Math.max(0, props.sessions.length - visibleCount.value));

function showMoreSessions() {
  visibleCount.value += SESSIONS_BATCH;
}

// Meatballs menu: only one open at a time; an invisible overlay closes it
// on any outside click (no global listeners needed).
const openMenuId = ref<string | null>(null);

function toggleMenu(id: string) {
  openMenuId.value = openMenuId.value === id ? null : id;
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

const ADMIN_COLLAPSED_KEY = "chat-sidebar-admin-collapsed";
const adminCollapsed = ref(localStorage.getItem(ADMIN_COLLAPSED_KEY) === "1");

function toggleAdmin() {
  adminCollapsed.value = !adminCollapsed.value;
  localStorage.setItem(ADMIN_COLLAPSED_KEY, adminCollapsed.value ? "1" : "0");
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

/* Show-more: the list dissolves into a fade gradient and a centered pill
   button sits below it. The negative margin pulls the fade over the list's
   bottom edge; the fade ignores pointer events so items stay clickable. */
.chat-sidebar-more {
  position: relative;
  flex-shrink: 0;
  display: flex;
  justify-content: center;
  margin-top: -22px;
  padding-top: 22px;
}

.chat-sidebar-more-fade {
  position: absolute;
  inset: 0 0 auto 0;
  height: 22px;
  background: linear-gradient(to bottom, transparent, var(--chat-bg-card));
  pointer-events: none;
}

.chat-sidebar-more-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 12px;
  border: 1px solid var(--chat-border);
  border-radius: 999px;
  background: var(--chat-bg-card);
  color: var(--chat-text-secondary);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  box-shadow: var(--chat-shadow);
  transition:
    background 0.15s,
    border-color 0.15s,
    color 0.15s;
}

.chat-sidebar-more-btn:hover {
  background: var(--chat-bg-hover);
  border-color: var(--chat-text-tertiary);
  color: var(--chat-text-primary);
}

.chat-sidebar-more-chevron {
  width: 12px;
  height: 12px;
}

.chat-sidebar-more-count {
  min-width: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--chat-bg-hover);
  color: var(--chat-text-tertiary);
  font-size: 11px;
  line-height: 16px;
  text-align: center;
  font-variant-numeric: tabular-nums;
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
