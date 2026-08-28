<template>
  <div class="resources-page">
    <div class="resources-container">
      <header class="resources-header">
        <div>
          <h1 class="resources-title">资源库</h1>
          <p class="resources-subtitle">上传文档到检索库，审核时可通过「搜索文档」引用</p>
        </div>
        <router-link to="/" class="resources-back"><AppIcon name="arrow-left" :size="14" />返回主页</router-link>
      </header>

      <section class="resources-upload">
        <h2 class="resources-section-title">上传文档</h2>
        <form class="resources-form" @submit.prevent="submitUpload">
          <div class="resources-form-row">
            <label class="resources-file">
              <input type="file" accept=".pdf,.docx,.txt,.md" @change="onFileChange" />
              <span>{{ form.file ? form.file.name : "选择文件（pdf / docx / txt / md，≤50MB）" }}</span>
            </label>
          </div>
          <div class="resources-form-grid">
            <div class="auth-field">
              <label>标题</label>
              <input v-model="form.title" type="text" placeholder="默认取文件名" />
            </div>
            <div class="auth-field">
              <label>作者</label>
              <input v-model="form.author" type="text" />
            </div>
            <div class="auth-field">
              <label>来源</label>
              <input v-model="form.source" type="text" placeholder="如：新华社、公安部" />
            </div>
            <div class="auth-field">
              <label>发布日期</label>
              <input v-model="form.publishDate" type="date" />
            </div>
            <div class="auth-field resources-form-wide">
              <label>标签（逗号分隔）</label>
              <input v-model="form.tags" type="text" placeholder="如：政策,通知,2026" />
            </div>
            <div v-if="isAdmin" class="auth-field">
              <label>可见性</label>
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
          <button type="submit" class="auth-submit resources-submit" :disabled="!form.file || uploading">
            {{ uploading ? "上传中..." : "上传到资源库" }}
          </button>
        </form>
      </section>

      <section class="resources-list-section">
        <div class="resources-tabs">
          <button
            type="button"
            class="resources-tab"
            :class="{ 'resources-tab--active': activeTab === 'public' }"
            @click="switchTab('public')"
          >
            公共资源库
          </button>
          <button
            type="button"
            class="resources-tab"
            :class="{ 'resources-tab--active': activeTab === 'personal' }"
            @click="switchTab('personal')"
          >
            我的资源库
          </button>
        </div>
        <div class="resources-list-header">
          <h2 class="resources-section-title">已入库文档（{{ total }}）</h2>
          <input
            v-model="searchQuery"
            type="search"
            class="resources-search"
            placeholder="搜索标题 / 作者 / 来源 / 标签"
            @input="onSearchInput"
          />
        </div>
        <p v-if="listMsg" class="resources-msg msg-err">{{ listMsg }}</p>
        <div v-if="loading" class="resources-empty">加载中...</div>
        <div v-else-if="items.length === 0" class="resources-empty">暂无资源</div>
        <ul v-else class="resources-list">
          <li v-for="item in items" :key="item.id" class="resources-item">
            <div class="resources-item-main">
              <div class="resources-item-title">
                <span
                  class="resources-item-badge"
                  :class="item.visibility === 'public' ? 'resources-item-badge--public' : 'resources-item-badge--personal'"
                >
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
              class="resources-item-delete"
              type="button"
              :disabled="deletingId === item.id"
              @click="confirmDelete(item)"
            >
              {{ deletingId === item.id ? "删除中..." : "删除" }}
            </button>
          </li>
        </ul>
        <div v-if="items.length < total" class="resources-more">
          <button type="button" class="resources-more-btn" @click="loadMore">加载更多</button>
        </div>
      </section>
    </div>

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
import AppIcon from "../components/AppIcon.vue";
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
  } catch {
    if (reset) items.value = [];
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
.resources-page {
  height: 100%;
  overflow-y: auto;
  background: var(--chat-bg-body);
  padding: 40px 16px 60px;
}

.resources-container {
  max-width: 760px;
  margin: 0 auto;
}

.resources-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 28px;
}

.resources-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 26px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0 0 4px;
}

.resources-subtitle {
  font-size: 13px;
  color: var(--chat-text-tertiary);
  margin: 0;
}

.resources-back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--chat-text-secondary);
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}

.resources-back:hover {
  color: var(--chat-text-primary);
}

.resources-upload,
.resources-list-section {
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: var(--chat-shadow);
  padding: 16px 20px 20px;
  margin-bottom: 20px;
}

.resources-section-title {
  font-family: "Noto Sans SC", sans-serif;
  font-size: 15px;
  font-weight: 600;
  color: var(--chat-text-primary);
  margin: 0 0 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--chat-border);
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

.resources-msg {
  font-size: 13px;
  padding: 10px 14px;
  border-radius: var(--chat-radius-sm);
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
  /* .auth-field is a stretch-aligned column flex — keep the toggle hugging
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
  display: flex;
  gap: 4px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--chat-border);
}

.resources-tab {
  padding: 8px 14px;
  border: none;
  background: transparent;
  font-size: 14px;
  color: var(--chat-text-secondary);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}

.resources-tab--active {
  color: var(--chat-accent);
  border-bottom-color: var(--chat-accent);
  font-weight: 600;
}

.resources-item-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  margin-right: 6px;
  vertical-align: 1px;
}

.resources-item-badge--public {
  background: color-mix(in srgb, var(--ok) 13%, transparent);
  color: var(--ok);
}

.resources-item-badge--personal {
  background: var(--chat-accent-soft);
  color: var(--chat-accent);
}

.msg-ok {
  background: color-mix(in srgb, var(--ok) 10%, transparent);
  color: var(--ok-dim);
}

.msg-err {
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
}

.resources-submit {
  width: auto;
  height: 32px;
  padding: 0 20px;
  margin-top: 16px;
  font-size: 13px;
}

.resources-list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 4px;
}

.resources-list-header .resources-section-title {
  border-bottom: none;
  padding-bottom: 0;
  margin-bottom: 0;
}

.resources-search {
  flex: 0 1 260px;
  padding: 7px 12px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  outline: none;
  transition:
    border-color 150ms,
    box-shadow 150ms;
}

.resources-search:focus {
  border-color: var(--chat-accent);
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}

.resources-empty {
  padding: 32px 0;
  text-align: center;
  color: var(--chat-text-tertiary);
  font-size: 13px;
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

.resources-item-delete {
  flex-shrink: 0;
  height: 28px;
  padding: 0 12px;
  border: 1px solid color-mix(in srgb, var(--err) 40%, transparent);
  border-radius: var(--chat-radius-sm);
  background: transparent;
  color: var(--err);
  font-size: 12px;
  cursor: pointer;
  transition: background 150ms;
}

.resources-item-delete:hover:not(:disabled) {
  background: color-mix(in srgb, var(--err) 12%, transparent);
}

.resources-item-delete:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.resources-more {
  text-align: center;
  padding-top: 12px;
  border-top: 1px solid var(--chat-border);
}

.resources-more-btn {
  height: 30px;
  padding: 0 16px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-size: 12px;
  cursor: pointer;
  transition: background 150ms;
}

.resources-more-btn:hover {
  background: var(--chat-bg-hover);
}

/* The upload form reuses auth.css classes; align them with the settings
   console scale (auth.css itself is shared with login/register). */
.auth-field label {
  font-size: 13px;
  color: var(--chat-text-secondary);
}

.auth-field input {
  height: 34px;
  padding: 0 10px;
  font-size: 13px;
  background: var(--chat-bg-body);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  color: var(--chat-text-primary);
  outline: none;
  transition:
    border-color 150ms,
    box-shadow 150ms;
}

.auth-field input:focus {
  border-color: var(--chat-accent);
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}

.auth-submit {
  height: 32px;
  background: var(--chat-accent);
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
}

/* ---- Delete confirm modal (settings pattern) ---- */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
}
.modal {
  width: min(420px, calc(100vw - 48px));
  padding: 20px;
  background: var(--chat-bg-card);
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-md);
  box-shadow: 0 8px 30px rgba(0, 0, 0, 0.18);
}
.modal-title {
  margin: 0 0 8px;
  font-size: 16px;
  color: var(--chat-text-primary);
}
.modal-message {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--chat-text-secondary);
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
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
  transition: background 150ms;
}
.modal-btn:hover:not(:disabled) {
  background: var(--chat-bg-hover);
}
.modal-btn--danger {
  background: transparent;
  border-color: color-mix(in srgb, var(--err) 40%, transparent);
  color: var(--err);
}
.modal-btn--danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--err) 12%, transparent);
}
.modal-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
