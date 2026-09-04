<template>
  <div class="memory-page">
    <div class="memory-container">
      <header class="memory-header">
        <div>
          <h1 class="memory-title">{{ isGlobal ? "全局记忆" : "我的记忆" }}</h1>
          <p class="memory-subtitle">
            {{
              isGlobal
                ? "全局共享层记忆：所有用户可读" + (isAdmin ? "，管理员可维护" : "")
                : "你的用户层记忆：仅自己可见，Agent 也会在对话中召回这里的内容"
            }}
          </p>
        </div>
        <router-link to="/" class="memory-back"><AppIcon name="arrow-left" :size="14" />返回主页</router-link>
      </header>

      <section class="memory-toolbar">
        <div class="memory-tabs">
          <button
            v-for="d in domainOptions"
            :key="d.value"
            type="button"
            class="memory-tab"
            :class="{ 'memory-tab--active': domainFilter === d.value }"
            @click="domainFilter = d.value"
          >
            {{ d.label }}
          </button>
        </div>
        <div class="memory-toolbar-right">
          <input
            v-model="searchQuery"
            type="search"
            class="memory-search"
            placeholder="搜索标题 / 内容"
            @input="onSearchInput"
          />
          <button
            v-if="canEdit"
            type="button"
            class="memory-btn memory-btn--primary"
            @click="openCreate"
          >
            新建记忆
          </button>
          <button
            v-if="canEdit && entries.length > 0"
            type="button"
            class="memory-btn memory-btn--danger-ghost"
            @click="clearConfirm = true"
          >
            清空全部
          </button>
        </div>
      </section>

      <p v-if="listMsg" class="memory-msg msg-err">{{ listMsg }}</p>
      <div v-if="loading" class="memory-empty">加载中...</div>
      <div v-else-if="entries.length === 0" class="memory-empty">暂无记忆条目</div>
      <ul v-else class="memory-list">
        <li v-for="item in entries" :key="item.id" class="memory-item">
          <div class="memory-item-main">
            <div class="memory-item-title">
              <span class="memory-item-badge">{{ domainLabel(item.domain) }}</span>
              {{ item.title }}
            </div>
            <div class="memory-item-content">{{ item.content }}</div>
            <div class="memory-item-meta">
              {{ item.updatedBy || item.createdBy || "—" }} · {{ formatDate(item.updatedAt) }}
            </div>
          </div>
          <div v-if="canEdit" class="memory-item-actions">
            <button type="button" class="memory-btn" @click="openEdit(item)">编辑</button>
            <button
              type="button"
              class="memory-btn memory-btn--danger-ghost"
              :disabled="deletingId === item.id"
              @click="deleteConfirm = { open: true, item }"
            >
              删除
            </button>
          </div>
        </li>
      </ul>
    </div>

    <!-- 新建 / 编辑 -->
    <div v-if="editor.open" class="modal-backdrop" @click.self="editor.open = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3 class="modal-title">{{ editor.id === null ? "新建记忆" : "编辑记忆" }}</h3>
        <div class="memory-form">
          <div class="auth-field">
            <label>标题</label>
            <input v-model="editor.title" type="text" placeholder="稳定且具体，便于后续寻址" />
          </div>
          <div class="auth-field">
            <label>领域包</label>
            <select v-model="editor.domain">
              <option value="common">通用</option>
              <option v-for="d in knownDomains" :key="d" :value="d">{{ d }}</option>
            </select>
          </div>
          <div class="auth-field">
            <label>内容</label>
            <textarea v-model="editor.content" rows="6" placeholder="记忆内容"></textarea>
          </div>
          <p v-if="editor.msg" class="memory-msg" :class="editor.ok ? 'msg-ok' : 'msg-err'">
            {{ editor.msg }}
          </p>
        </div>
        <div class="modal-actions">
          <button class="modal-btn" @click="editor.open = false">取消</button>
          <button
            class="modal-btn modal-btn--primary"
            :disabled="!editor.title.trim() || !editor.content.trim() || editor.saving"
            @click="saveEditor"
          >
            {{ editor.saving ? "保存中..." : "保存" }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除单条 -->
    <div v-if="deleteConfirm.open" class="modal-backdrop" @click.self="deleteConfirm.open = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3 class="modal-title">确认删除</h3>
        <p class="modal-message">确定删除「{{ deleteConfirm.item?.title }}」？删除后不可恢复。</p>
        <div class="modal-actions">
          <button class="modal-btn" @click="deleteConfirm.open = false">取消</button>
          <button class="modal-btn modal-btn--danger" :disabled="deletingId !== null" @click="doDelete">
            {{ deletingId !== null ? "删除中..." : "删除" }}
          </button>
        </div>
      </div>
    </div>

    <!-- 清空全部 -->
    <div v-if="clearConfirm" class="modal-backdrop" @click.self="clearConfirm = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3 class="modal-title">确认清空</h3>
        <p class="modal-message">
          确定清空{{ isGlobal ? "全局共享层" : "你的用户层" }}的全部 {{ entries.length }} 条记忆？删除后不可恢复。
        </p>
        <div class="modal-actions">
          <button class="modal-btn" @click="clearConfirm = false">取消</button>
          <button class="modal-btn modal-btn--danger" :disabled="clearing" @click="doClear">
            {{ clearing ? "清空中..." : "全部清空" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from "../components/AppIcon.vue";
import { computed, onMounted, reactive, ref } from "vue";
import { api, type MemoryEntry } from "../api/client";
import { useAuth } from "../composables/useAuth";

const props = defineProps<{ scope: "global" | "mine" }>();

const { user } = useAuth();
const isAdmin = computed(() => user.value?.role === "admin");
const isGlobal = computed(() => props.scope === "global");
const canEdit = computed(() => !isGlobal.value || isAdmin.value);

const entries = ref<MemoryEntry[]>([]);
const loading = ref(true);
const listMsg = ref("");
const searchQuery = ref("");
const domainFilter = ref("all");
const knownDomains = ref<string[]>([]);
const deletingId = ref<number | null>(null);
const clearing = ref(false);
const clearConfirm = ref(false);
const deleteConfirm = reactive<{ open: boolean; item: MemoryEntry | null }>({
  open: false,
  item: null,
});

const editor = reactive({
  open: false,
  id: null as number | null,
  title: "",
  content: "",
  domain: "common",
  saving: false,
  msg: "",
  ok: false,
});

const domainOptions = computed(() => [
  { value: "all", label: "全部" },
  { value: "common", label: "通用" },
  ...knownDomains.value.map((d) => ({ value: d, label: d })),
]);

function domainLabel(domain: string): string {
  return domain === "common" ? "通用" : domain;
}

function formatDate(value: string): string {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 16);
}

let searchTimer: ReturnType<typeof setTimeout> | null = null;
function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => void loadList(), 250);
}

async function loadList() {
  loading.value = true;
  listMsg.value = "";
  try {
    entries.value = await api.listMemory(
      props.scope,
      domainFilter.value === "all" ? "" : domainFilter.value,
      searchQuery.value,
    );
  } catch (e: unknown) {
    listMsg.value = e instanceof Error ? e.message : "加载失败";
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  Object.assign(editor, {
    open: true,
    id: null,
    title: "",
    content: "",
    domain: "common",
    saving: false,
    msg: "",
    ok: false,
  });
}

function openEdit(item: MemoryEntry) {
  Object.assign(editor, {
    open: true,
    id: item.id,
    title: item.title,
    content: item.content,
    domain: item.domain,
    saving: false,
    msg: "",
    ok: false,
  });
}

async function saveEditor() {
  editor.saving = true;
  editor.msg = "";
  try {
    const body = { title: editor.title.trim(), content: editor.content, domain: editor.domain };
    if (editor.id === null) {
      await api.upsertMemory(props.scope, body);
    } else {
      await api.updateMemory(props.scope, editor.id, body);
    }
    editor.open = false;
    await loadList();
  } catch (e: unknown) {
    editor.ok = false;
    editor.msg = e instanceof Error ? e.message : "保存失败";
  } finally {
    editor.saving = false;
  }
}

async function doDelete() {
  if (!deleteConfirm.item) return;
  deletingId.value = deleteConfirm.item.id;
  try {
    await api.deleteMemory(props.scope, deleteConfirm.item.id);
    deleteConfirm.open = false;
    await loadList();
  } catch (e: unknown) {
    listMsg.value = e instanceof Error ? e.message : "删除失败";
  } finally {
    deletingId.value = null;
  }
}

async function doClear() {
  clearing.value = true;
  try {
    await api.clearMemory(props.scope);
    clearConfirm.value = false;
    await loadList();
  } catch (e: unknown) {
    listMsg.value = e instanceof Error ? e.message : "清空失败";
  } finally {
    clearing.value = false;
  }
}

onMounted(async () => {
  try {
    knownDomains.value = (await api.listMemoryDomains()).domains;
  } catch {
    knownDomains.value = [];
  }
  await loadList();
});
</script>

<style scoped>
.memory-page {
  height: 100%;
  overflow-y: auto;
  background: var(--chat-bg-body);
  padding: 40px 16px 60px;
}
.memory-container {
  max-width: 820px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.memory-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.memory-title {
  margin: 0 0 4px;
  font-family: "Noto Sans SC", sans-serif;
  font-size: 26px;
  font-weight: 600;
  color: var(--chat-text-primary);
}
.memory-subtitle {
  margin: 0;
  font-size: 13px;
  color: var(--chat-text-tertiary);
}
.memory-back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--chat-text-secondary);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}
.memory-back:hover {
  color: var(--chat-text-primary);
}
.memory-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.memory-tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.memory-tab {
  border: 1px solid var(--chat-border);
  background: transparent;
  color: var(--chat-text-secondary);
  border-radius: 999px;
  padding: 5px 14px;
  font-size: 13px;
  cursor: pointer;
}
.memory-tab--active {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: var(--chat-accent-contrast, #fff);
}
.memory-toolbar-right {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.memory-search {
  min-width: 200px;
  padding: 6px 12px;
  border-radius: var(--chat-radius-sm);
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  font-size: 13px;
}
.memory-btn {
  height: 30px;
  border: 1px solid var(--chat-border);
  background: var(--chat-bg-card);
  color: var(--chat-text-primary);
  border-radius: var(--chat-radius-sm);
  padding: 0 14px;
  font-size: 13px;
  cursor: pointer;
}
.memory-btn--primary {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: var(--chat-accent-contrast, #fff);
}
.memory-btn--danger-ghost {
  color: var(--err);
  border-color: color-mix(in srgb, var(--err) 45%, transparent);
}
.memory-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.memory-msg {
  font-size: 13px;
  margin: 0;
  padding: 6px 10px;
  border-radius: var(--chat-radius-sm);
}
.memory-empty {
  text-align: center;
  opacity: 0.55;
  padding: 40px 0;
  font-size: 14px;
  color: var(--chat-text-tertiary);
}
.memory-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.memory-item {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  background: var(--chat-bg-card);
}
.memory-item-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.memory-item-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--chat-text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}
.memory-item-badge {
  font-size: 11px;
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
  white-space: nowrap;
}
.memory-item-content {
  font-size: 13px;
  color: var(--chat-text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.memory-item-meta {
  font-size: 12px;
  color: var(--chat-text-tertiary);
}
.memory-item-actions {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  flex-shrink: 0;
}
.memory-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-top: 12px;
}
.memory-form textarea {
  resize: vertical;
  font: inherit;
}
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  z-index: 300;
  display: flex;
  align-items: center;
  justify-content: center;
}
.modal {
  width: min(460px, calc(100vw - 48px));
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
  padding: 20px;
}
.modal-title {
  margin: 0 0 12px;
  font-size: 16px;
  color: var(--chat-text-primary);
}
.modal-message {
  font-size: 13px;
  color: var(--chat-text-secondary);
  margin: 0;
}
.modal-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  margin-top: 16px;
}
.modal-btn {
  height: 32px;
  padding: 0 14px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-size: 13px;
  cursor: pointer;
}
.modal-btn:hover:not(:disabled) {
  background: var(--chat-bg-hover);
}
.modal-btn--primary {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: var(--chat-accent-contrast, #fff);
}
.modal-btn--danger {
  color: var(--err);
  border-color: color-mix(in srgb, var(--err) 45%, transparent);
}
.modal-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
