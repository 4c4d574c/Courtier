<template>
  <div class="admin-page">
    <header class="admin-header">
      <div>
        <h1 class="admin-heading">审批中心</h1>
        <div class="admin-tabs">
          <button
            type="button"
            class="admin-tab"
            :class="{ 'admin-tab--active': activeTab === 'register' }"
            @click="activeTab = 'register'"
          >
            注册审批
          </button>
          <button
            type="button"
            class="admin-tab"
            :class="{ 'admin-tab--active': activeTab === 'deletion' }"
            @click="activeTab = 'deletion'"
          >
            注销申请
          </button>
        </div>
      </div>
    </header>
    <p v-if="actionMsg.text" :class="actionMsg.ok ? 'msg-ok' : 'msg-err'">
      {{ actionMsg.text }}
    </p>
    <div class="admin-card" v-if="activeTab === 'register' && items.length > 0">
      <table class="admin-table">
      <thead>
        <tr>
          <th>用户名</th>
          <th>邮箱</th>
          <th>注册时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td class="td-username">{{ item.username }}</td>
          <td class="td-email">{{ item.email || "-" }}</td>
          <td class="td-date">{{ formatDate(item.created_at) }}</td>
          <td class="td-actions">
            <button
              @click="approve(item)"
              class="action-btn action-btn--ok"
              :disabled="item._loading"
            >
              批准
            </button>
            <button
              @click="reject(item)"
              class="action-btn action-btn--danger"
              :disabled="item._loading"
            >
              拒绝
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>
    <div class="admin-card" v-else-if="activeTab === 'deletion' && deletionItems.length > 0">
      <table class="admin-table">
      <thead>
        <tr>
          <th>用户名</th>
          <th>申请时间</th>
          <th>发起方</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in deletionItems" :key="item.id">
          <td class="td-username">{{ item.username ?? `用户#${item.userId}` }}</td>
          <td class="td-date">{{ formatDate(item.createdAt || "") }}</td>
          <td>{{ item.requestedBy === "self" ? "本人申请" : "管理员" }}</td>
          <td class="td-actions">
            <button
              @click="deleteConfirm = item"
              class="action-btn action-btn--ok"
              :disabled="item._loading"
            >
              批准注销
            </button>
            <button
              @click="rejectDeletion(item)"
              class="action-btn action-btn--danger"
              :disabled="item._loading"
            >
              拒绝
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>
    <div v-else-if="!loading" class="admin-card">
      <div class="admin-empty-state">
        <p>{{ activeTab === "register" ? "暂无待审批的注册申请" : "暂无待审批的注销申请" }}</p>
      </div>
    </div>
  </div>

  <div v-if="deleteConfirm" class="modal-backdrop" @click.self="deleteConfirm = null">
    <div class="modal" role="dialog" aria-modal="true">
      <h3 class="modal-title">确认批准注销</h3>
      <p class="modal-message">
        将不可逆地清除 {{ deleteConfirm.username ?? `用户#${deleteConfirm.userId}` }}
        的会话、文件、记忆等全部数据并注销账号。此操作不可恢复。
      </p>
      <div class="modal-actions">
        <button class="action-btn" @click="deleteConfirm = null">取消</button>
        <button
          class="action-btn action-btn--danger"
          :disabled="deleteConfirm._loading"
          @click="
            approveDeletion(deleteConfirm);
            deleteConfirm = null;
          "
        >
          确认注销
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from "vue";
import { api, type DeletionRequestItem } from "../api/client";
import { formatDate } from "../utils/date";

interface ApprovalItem {
  id: number;
  username: string;
  email: string;
  created_at: string;
  _loading?: boolean;
}

const items = ref<(ApprovalItem & { _loading?: boolean })[]>([]);
const activeTab = ref<"register" | "deletion">("register");
const deletionItems = ref<(DeletionRequestItem & { _loading?: boolean })[]>([]);
const loading = ref(false);
const actionMsg = reactive({ text: "", ok: false });

function reportError(e: unknown, fallback: string) {
  actionMsg.text = e instanceof Error ? e.message : fallback;
  actionMsg.ok = false;
}

async function approve(item: ApprovalItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.approveUser(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
    actionMsg.text = `已批准 ${item.username} 的注册申请`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "批准失败");
  } finally {
    item._loading = false;
  }
}

async function reject(item: ApprovalItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.rejectUser(item.id);
    items.value = items.value.filter((i) => i.id !== item.id);
    actionMsg.text = `已拒绝 ${item.username} 的注册申请`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "拒绝失败");
  } finally {
    item._loading = false;
  }
}

async function loadDeletionRequests() {
  try {
    const res = await api.listDeletionRequests();
    deletionItems.value = res.items.map((i) => ({ ...i, _loading: false }));
  } catch (e: unknown) {
    reportError(e, "获取注销申请失败");
  }
}

const deleteConfirm = ref<
  (DeletionRequestItem & { _loading?: boolean }) | null
>(null);

async function approveDeletion(item: DeletionRequestItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    const { receipt } = await api.approveDeletionRequest(item.id);
    deletionItems.value = deletionItems.value.filter((i) => i.id !== item.id);
    const failures = Object.keys(receipt.failures || {}).length;
    actionMsg.text = `已注销 ${item.username ?? `用户#${item.userId}`}（清除 ${Object.values(receipt.counts || {}).reduce((a, b) => a + b, 0)} 项${failures ? `，${failures} 项外部清理失败已记录` : ""}）`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "批准注销失败");
  } finally {
    item._loading = false;
  }
}

async function rejectDeletion(item: DeletionRequestItem & { _loading?: boolean }) {
  item._loading = true;
  try {
    await api.rejectDeletionRequest(item.id);
    deletionItems.value = deletionItems.value.filter((i) => i.id !== item.id);
    actionMsg.text = `已拒绝 ${item.username ?? `用户#${item.userId}`} 的注销申请`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "拒绝失败");
  } finally {
    item._loading = false;
  }
}

onMounted(async () => {
  loading.value = true;
  try {
    const res = await api.listApprovals();
    items.value = res.items.map((i) => ({ ...i, _loading: false }));
  } catch (e: unknown) {
    reportError(e, "获取审批列表失败");
  } finally {
    loading.value = false;
  }
  // Independent API — a registrations failure must not hide deletion
  // requests (and vice versa).
  await loadDeletionRequests();
});
</script>

<style scoped>
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 60;
}
.modal {
  background: var(--bg-elevated, #fff);
  border-radius: 10px;
  padding: 18px 20px;
  max-width: 430px;
  width: calc(100% - 40px);
}
.modal-title {
  margin: 0 0 8px;
  font-size: 15px;
}
.modal-message {
  margin: 0 0 14px;
  font-size: 13px;
  line-height: 1.6;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
/* Tabs sit under the heading inside the header cluster. */
.admin-tabs {
  margin-top: 10px;
}
</style>
