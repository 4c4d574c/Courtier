<template>
  <div class="admin-page">
    <header class="admin-header">
      <h1 class="admin-heading">{{ isGlobal ? "全局记忆" : "我的记忆" }}</h1>
      <p class="admin-subtitle">
        {{
          isGlobal
            ? "全局共享层记忆：所有用户可读" + (isAdmin ? "，管理员可维护" : "")
            : "你的用户层记忆：仅自己可见，Agent 也会在对话中召回这里的内容"
        }}
      </p>
    </header>

    <section class="memory-toolbar">
      <div class="admin-tabs">
        <button
          v-for="d in domainOptions"
          :key="d.value"
          type="button"
          class="admin-tab"
          :class="{ 'admin-tab--active': domainFilter === d.value }"
          @click="domainFilter = d.value"
        >
          {{ d.label }}
        </button>
      </div>
      <div class="memory-toolbar-right">
        <input
          v-model="searchQuery"
          type="search"
          class="admin-search"
          placeholder="搜索标题 / 内容"
          @input="onSearchInput"
        />
        <button v-if="canEdit" type="button" class="btn primary" @click="openCreate">
          新建记忆
        </button>
        <button
          v-if="canEdit && entries.length > 0"
          type="button"
          class="btn danger"
          @click="clearConfirm = true"
        >
          清空全部
        </button>
      </div>
    </section>

    <p v-if="listMsg" class="msg-err">{{ listMsg }}</p>
    <div v-if="loading" class="admin-empty-state">加载中...</div>
    <div v-else-if="entries.length === 0" class="admin-empty-state">暂无记忆条目</div>
    <ul v-else class="memory-list">
      <li v-for="item in entries" :key="item.id" class="memory-item">
        <div class="memory-item-main">
          <div class="memory-item-title">
            <span class="badge badge-accent">{{ domainLabel(item.domain) }}</span>
            {{ item.title }}
          </div>
          <div class="memory-item-content">{{ item.content }}</div>
          <div class="memory-item-meta">
            {{ item.updatedBy || item.createdBy || "—" }} · {{ formatDate(item.updatedAt) }}
          </div>
        </div>
        <div v-if="canEdit" class="memory-item-actions">
          <button type="button" class="btn" @click="openEdit(item)">编辑</button>
          <button
            type="button"
            class="btn danger"
            :disabled="deletingId === item.id"
            @click="deleteConfirm = { open: true, item }"
          >
            删除
          </button>
        </div>
      </li>
    </ul>

    <!-- 新建 / 编辑 -->
    <div v-if="editor.open" class="modal-backdrop" @click.self="editor.open = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3 class="modal-title">{{ editor.id === null ? "新建记忆" : "编辑记忆" }}</h3>
        <div class="memory-form">
          <div class="admin-field">
            <span>标题</span>
            <input v-model="editor.title" type="text" placeholder="稳定且具体，便于后续寻址" />
          </div>
          <div class="admin-field">
            <span>领域包</span>
            <select v-model="editor.domain">
              <option value="common">通用</option>
              <option v-for="d in knownDomains" :key="d" :value="d">{{ d }}</option>
            </select>
          </div>
          <div class="admin-field">
            <span>内容</span>
            <textarea v-model="editor.content" rows="6" placeholder="记忆内容"></textarea>
          </div>
          <p v-if="editor.msg" :class="editor.ok ? 'msg-ok' : 'msg-err'">
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
.memory-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}
.memory-toolbar-right {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
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
.memory-form .admin-field {
  margin-bottom: 0;
}
/* Memory content is prose, not code — keep the editor textarea in the
   UI font instead of the shared field's monospace. */
.memory-form .admin-field textarea {
  font-family: inherit;
  resize: vertical;
  font-size: 13px;
}
</style>
