<template>
  <div class="admin-page">
    <header class="admin-header">
      <h1 class="admin-heading">资源库</h1>
      <p class="admin-subtitle">上传文档到检索库，审核时可通过「搜索文档」引用</p>
    </header>

    <section class="admin-card resources-card">
      <div class="admin-section-head">
        <h2 class="admin-section-title">上传文档</h2>
      </div>
      <form class="resources-form" @submit.prevent="submitUpload">
        <div class="resources-form-row">
          <label class="resources-file">
            <input type="file" accept=".pdf,.docx,.txt,.md" @change="onFileChange" />
            <span>{{ form.file ? form.file.name : "选择文件（pdf / docx / txt / md，≤50MB）" }}</span>
          </label>
        </div>
        <div class="resources-form-grid">
          <div class="admin-field">
            <span>标题</span>
            <input v-model="form.title" type="text" placeholder="默认取文件名" />
          </div>
          <div class="admin-field">
            <span>作者</span>
            <input v-model="form.author" type="text" />
          </div>
          <div class="admin-field">
            <span>来源</span>
            <input v-model="form.source" type="text" placeholder="如：新华社、公安部" />
          </div>
          <div class="admin-field">
            <span>发布日期</span>
            <input v-model="form.publishDate" type="date" />
          </div>
          <div class="admin-field resources-form-wide">
            <span>标签（逗号分隔）</span>
            <input v-model="form.tags" type="text" placeholder="如：政策,通知,2026" />
          </div>
          <div v-if="isAdmin" class="admin-field">
            <span>可见性</span>
            <div class="visibility-toggle" role="radiogroup" aria-label="可见性">
              <button
                type="button"
                class="visibility-option"
                :class="{ 'visibility-option--active': form.visibility === 'public' }"
                role="radio"
                :aria-checked="form.visibility === 'public'"
                @click="form.visibility = 'public'"
              >
                公共
              </button>
              <button
                type="button"
                class="visibility-option"
                :class="{ 'visibility-option--active': form.visibility === 'personal' }"
                role="radio"
                :aria-checked="form.visibility === 'personal'"
                @click="form.visibility = 'personal'"
              >
                个人
              </button>
            </div>
            <p class="visibility-desc">
              {{ form.visibility === "public" ? "所有用户均可检索" : "仅自己可见可检索" }}
            </p>
          </div>
          <p v-else class="resources-hint">将上传到你的个人资源库，仅自己可见。</p>
        </div>
        <p v-if="uploadMsg" class="resources-msg" :class="uploadOk ? 'msg-ok' : 'msg-err'">
          {{ uploadMsg }}
        </p>
        <button type="submit" class="btn primary resources-submit" :disabled="!form.file || uploading">
          {{ uploading ? "上传中..." : "上传到资源库" }}
        </button>
      </form>
    </section>

    <section class="admin-card resources-card">
      <div class="admin-tabs resources-tabs">
        <button
          type="button"
          class="admin-tab"
          :class="{ 'admin-tab--active': activeTab === 'public' }"
          @click="switchTab('public')"
        >
          公共资源库
        </button>
        <button
          type="button"
          class="admin-tab"
          :class="{ 'admin-tab--active': activeTab === 'personal' }"
          @click="switchTab('personal')"
        >
          我的资源库
        </button>
      </div>
      <div class="resources-list-header">
        <h2 class="admin-section-title">已入库文档（{{ total }}）</h2>
        <input
          v-model="searchQuery"
          type="search"
          class="admin-search resources-search"
          placeholder="搜索标题 / 作者 / 来源 / 标签"
          @input="onSearchInput"
        />
      </div>
      <p v-if="listMsg" class="msg-err">{{ listMsg }}</p>
      <div v-if="loading" class="admin-empty-state">加载中...</div>
      <div v-else-if="items.length === 0" class="admin-empty-state">暂无资源</div>
      <ul v-else class="resources-list">
        <li v-for="item in items" :key="item.id" class="resources-item">
          <div class="resources-item-main">
            <div class="resources-item-title">
              <span class="badge" :class="item.visibility === 'public' ? 'badge-ok' : 'badge-accent'">
                {{ item.visibility === "public" ? "公共" : "个人" }}
              </span>
              {{ item.title }}
            </div>
            <div class="resources-item-meta">
              <span v-if="item.author">{{ item.author }}</span>
              <span v-if="item.source">· {{ item.source }}</span>
              <span v-if="item.publishDate">· {{ item.publishDate }}</span>
              <span v-if="item.tags" class="resources-item-tags">· {{ item.tags }}</span>
            </div>
            <div class="resources-item-meta resources-item-dim">
              {{ item.fileType }} · {{ formatSize(item.fileSize) }} ·
              {{ item.chunkCount }} 切片 · {{ formatDate(item.createdAt) }}
            </div>
          </div>
          <button
            v-if="canDelete(item)"
            class="action-btn action-btn--danger"
            type="button"
            :disabled="deletingId === item.id"
            @click="confirmDelete(item)"
          >
            {{ deletingId === item.id ? "删除中..." : "删除" }}
          </button>
        </li>
      </ul>
      <div v-if="items.length < total" class="resources-more">
        <button type="button" class="btn" @click="loadMore">加载更多</button>
      </div>
    </section>

    <div v-if="deleteConfirm.open" class="modal-backdrop" @click.self="deleteConfirm.open = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3 class="modal-title">确认删除</h3>
        <p class="modal-message">
          确定删除「{{ deleteConfirm.item?.title }}」？将同时删除其全部检索切片。
        </p>
        <div class="modal-actions">
          <button class="modal-btn" @click="deleteConfirm.open = false">取消</button>
          <button class="modal-btn modal-btn--danger" :disabled="deletingId !== null" @click="doDelete">
            {{ deletingId !== null ? "删除中..." : "删除" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { api, type ResourceSummary } from "../api/client";
import { useAuth } from "../composables/useAuth";

const { user } = useAuth();
const isAdmin = computed(() => user.value?.role === "admin");
const myUid = computed(() => user.value?.id ?? null);

const form = reactive({
  file: null as File | null,
  title: "",
  author: "",
  source: "",
  tags: "",
  publishDate: "",
  visibility: "public" as "public" | "personal",
});
const uploading = ref(false);
const uploadMsg = ref("");
const uploadOk = ref(false);

const activeTab = ref<"public" | "personal">("public");
const items = ref<ResourceSummary[]>([]);
const total = ref(0);
const loading = ref(true);
const searchQuery = ref("");
const deletingId = ref<number | null>(null);
const PAGE_SIZE = 50;

let searchTimer: ReturnType<typeof setTimeout> | null = null;

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement;
  form.file = input.files?.[0] ?? null;
}

function canDelete(item: ResourceSummary): boolean {
  if (isAdmin.value) return true;
  return item.visibility === "personal" && item.ownerId === myUid.value;
}

function switchTab(tab: "public" | "personal") {
  if (activeTab.value === tab) return;
  activeTab.value = tab;
  void loadList(true);
}

async function submitUpload() {
  if (!form.file) return;
  uploading.value = true;
  uploadMsg.value = "";
  try {
    const result = await api.uploadResource(form.file, {
      title: form.title || undefined,
      author: form.author || undefined,
      source: form.source || undefined,
      tags: form.tags || undefined,
      publishDate: form.publishDate || undefined,
      visibility: isAdmin.value ? form.visibility : undefined,
    });
    uploadMsg.value = `已入库：${result.title}（${result.chunkCount} 个切片，${
      result.visibility === "public" ? "公共" : "个人"
    }）`;
    uploadOk.value = true;
    form.file = null;
    form.title = "";
    await loadList(true);
  } catch (e: unknown) {
    uploadMsg.value = e instanceof Error ? e.message : "上传失败";
    uploadOk.value = false;
  } finally {
    uploading.value = false;
  }
}

async function loadList(reset = false) {
  if (reset) {
    items.value = [];
  }
  loading.value = true;
  try {
    const data = await api.listResources(
      searchQuery.value,
      reset ? 0 : items.value.length,
      PAGE_SIZE,
      activeTab.value,
    );
    total.value = data.total;
    items.value = reset ? data.items : [...items.value, ...data.items];
  } catch (e: unknown) {
    listMsg.value = e instanceof Error ? e.message : "加载失败，请稍后重试";
    if (!reset) items.value = [];
  } finally {
    loading.value = false;
  }
}

function loadMore() {
  void loadList(false);
}

function onSearchInput() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => void loadList(true), 300);
}

async function confirmDelete(item: ResourceSummary) {
  deleteConfirm.item = item;
  deleteConfirm.open = true;
}

const deleteConfirm = reactive<{ open: boolean; item: ResourceSummary | null }>({
  open: false,
  item: null,
});
const listMsg = ref("");

async function doDelete() {
  const item = deleteConfirm.item;
  if (!item) return;
  deleteConfirm.open = false;
  deletingId.value = item.id;
  listMsg.value = "";
  try {
    await api.deleteResource(item.id);
    items.value = items.value.filter((r) => r.id !== item.id);
    total.value -= 1;
  } catch (e: unknown) {
    listMsg.value = e instanceof Error ? e.message : "删除失败";
  } finally {
    deletingId.value = null;
  }
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function formatDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "";
}

onMounted(() => loadList(true));
</script>

<style scoped>
/* Card sections (upload form, document list). */
.resources-card {
  padding: 16px 20px 20px;
  margin-bottom: 20px;
}

.resources-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px 16px;
  margin-top: 12px;
}

.resources-form-wide {
  grid-column: 1 / -1;
}

.resources-file input {
  display: none;
}

.resources-file span {
  display: block;
  padding: 10px 12px;
  border: 1px dashed var(--chat-border);
  border-radius: var(--chat-radius-sm);
  color: var(--chat-text-secondary);
  font-size: 13px;
  cursor: pointer;
  background: var(--chat-bg-body);
  transition: border-color 150ms;
}

.resources-file span:hover {
  border-color: var(--chat-accent);
  color: var(--chat-text-primary);
}

/* Status messages inside the form read better with a top margin. */
.resources-form .msg-ok,
.resources-form .msg-err {
  margin: 12px 0 0;
}

.resources-hint {
  font-size: 12px;
  color: var(--chat-text-tertiary);
  margin: 0;
  align-self: end;
}

.visibility-toggle {
  display: inline-flex;
  /* .admin-field is a stretch-aligned column flex — keep the toggle hugging
     its buttons instead of spanning the full row. */
  align-self: flex-start;
  border: 1px solid var(--chat-border);
  border-radius: 8px;
  overflow: hidden;
  background: var(--chat-bg-body);
}

.visibility-option {
  padding: 6px 18px;
  border: none;
  background: transparent;
  font-size: 13px;
  color: var(--chat-text-secondary);
  cursor: pointer;
  transition:
    background 0.15s,
    color 0.15s;
}

.visibility-option + .visibility-option {
  border-left: 1px solid var(--chat-border);
}

.visibility-option--active {
  background: var(--chat-accent);
  color: var(--chat-accent-contrast);
}

.visibility-desc {
  font-size: 12px;
  color: var(--chat-text-tertiary);
  margin: 4px 0 0;
}

.resources-tabs {
  margin-bottom: 16px;
}

.resources-submit {
  margin-top: 16px;
  padding: 0 20px;
}

.resources-list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 4px;
}

/* The header search sits inside a card row, not a toolbar. */
.resources-search {
  flex: 0 1 260px;
}

.resources-list {
  list-style: none;
  display: flex;
  flex-direction: column;
}

.resources-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 0;
  border-top: 1px solid var(--chat-border);
}

.resources-item-title {
  font-size: 14px;
  color: var(--chat-text-primary);
  font-weight: 600;
}

.resources-item-meta {
  font-size: 12px;
  color: var(--chat-text-secondary);
  margin-top: 3px;
}

.resources-item-dim {
  color: var(--chat-text-tertiary);
}

.resources-more {
  text-align: center;
  padding-top: 12px;
  border-top: 1px solid var(--chat-border);
}
</style>
