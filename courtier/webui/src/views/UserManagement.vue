<template>
  <div class="admin-page">
    <header class="admin-header">
      <div>
        <h1 class="admin-heading">用户管理</h1>
        <p class="admin-subtitle">管理账号、角色与登录状态</p>
      </div>
    </header>
    <p v-if="actionMsg.text" class="action-msg" :class="actionMsg.ok ? 'msg-ok' : 'msg-err'">
      {{ actionMsg.text }}
    </p>
    <div class="admin-toolbar">
      <input
        v-model="search"
        type="text"
        placeholder="搜索用户名或邮箱..."
        class="admin-search"
        @input="debouncedSearch"
      />
      <select v-model="roleFilter" @change="fetchUsers" class="admin-select">
        <option value="">全部角色</option>
        <option value="admin">管理员</option>
        <option value="auditor">普通用户</option>
      </select>
      <select v-model="statusFilter" @change="fetchUsers" class="admin-select">
        <option value="">全部状态</option>
        <option value="active">活跃</option>
        <option value="disabled">禁用</option>
        <option value="pending">待审批</option>
      </select>
    </div>

    <div class="admin-card">
      <table class="admin-table">
      <thead>
        <tr>
          <th>用户名</th>
          <th>邮箱</th>
          <th>角色</th>
          <th>状态</th>
          <th>注册时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="users.length === 0 && !loading">
          <td colspan="6" class="admin-empty">暂无用户数据</td>
        </tr>
        <tr v-for="u in users" :key="u.id">
          <td class="td-username">{{ u.username }}</td>
          <td class="td-email">{{ u.email || "-" }}</td>
          <td>
            <span class="badge" :class="'badge-' + u.role">{{
              u.role === "admin" ? "管理员" : "普通用户"
            }}</span>
          </td>
          <td>
            <span class="badge" :class="'badge-' + u.status">{{
              statusLabel(u.status)
            }}</span>
          </td>
          <td class="td-date">{{ formatDate(u.created_at) }}</td>
          <td class="td-actions">
            <select
              :value="u.role"
              :disabled="isSelf(u)"
              :title="isSelf(u) ? '不能修改自己的角色' : ''"
              @change="
                changeRole(u, ($event.target as HTMLSelectElement).value as AdminUser['role'])
              "
              class="action-select"
            >
              <option value="admin">管理员</option>
              <option value="auditor">普通用户</option>
            </select>
            <select
              :value="u.status"
              :disabled="isSelf(u)"
              :title="isSelf(u) ? '不能修改自己的状态' : ''"
              @change="
                changeStatus(u, ($event.target as HTMLSelectElement).value as AdminUser['status'])
              "
              class="action-select"
            >
              <option value="active">活跃</option>
              <option value="disabled">禁用</option>
            </select>
            <button @click="openResetModal(u)" class="action-btn">
              重置密码
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    </div>

    <div class="admin-pagination" v-if="total > pageSize">
      <button :disabled="page <= 1" @click="goPage(page - 1)">上一页</button>
      <span>第 {{ page }} / {{ totalPages }} 页（共 {{ total }} 条）</span>
      <button :disabled="page >= totalPages" @click="goPage(page + 1)">
        下一页
      </button>
    </div>

    <div v-if="pwModal.open" class="modal-backdrop" @click.self="pwModal.open = false">
      <div class="modal" role="dialog" aria-modal="true">
        <h3 class="modal-title">为 {{ pwModal.username }} 重置密码</h3>
        <input
          v-model="pwModal.password"
          type="password"
          class="modal-input"
          placeholder="新密码（至少8位）"
          @keydown.enter="confirmResetPassword"
        />
        <p v-if="pwModal.error" class="action-msg msg-err">{{ pwModal.error }}</p>
        <div class="modal-actions">
          <button class="modal-btn" @click="pwModal.open = false">取消</button>
          <button class="modal-btn modal-btn--primary" @click="confirmResetPassword" :disabled="pwModal.loading">
            {{ pwModal.loading ? "提交中..." : "确认" }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from "vue";
import { api } from "../api/client";
import type { AdminUser } from "../api/client";
import { useAuth } from "../composables/useAuth";
import { formatDate } from "../utils/date";

const { user } = useAuth();

const users = ref<AdminUser[]>([]);
const search = ref("");
const roleFilter = ref("");
const statusFilter = ref("");
const page = ref(1);
const total = ref(0);
const pageSize = 20;
const loading = ref(false);
const actionMsg = reactive({ text: "", ok: false });

const totalPages = computed(() =>
  Math.max(1, Math.ceil(total.value / pageSize)),
);

const currentUserId = computed(() => user.value?.id);

function isSelf(u: AdminUser): boolean {
  return u.id === currentUserId.value;
}

let searchTimer: ReturnType<typeof setTimeout> | undefined;
function debouncedSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    fetchUsers();
  }, 300);
}

onUnmounted(() => clearTimeout(searchTimer));

async function fetchUsers() {
  loading.value = true;
  try {
    const res = await api.listUsers({
      page: page.value,
      page_size: pageSize,
      search: search.value || undefined,
      role: roleFilter.value || undefined,
      status: statusFilter.value || undefined,
    });
    users.value = res.items;
    total.value = res.total;
  } catch (e: unknown) {
    actionMsg.text = e instanceof Error ? e.message : "获取用户列表失败";
    actionMsg.ok = false;
  } finally {
    loading.value = false;
  }
}

function goPage(p: number) {
  page.value = p;
  fetchUsers();
}

function statusLabel(s: string): string {
  const map: Record<string, string> = {
    active: "活跃",
    disabled: "禁用",
    pending: "待审批",
  };
  return map[s] || s;
}

function reportError(e: unknown, fallback: string) {
  actionMsg.text = e instanceof Error ? e.message : fallback;
  actionMsg.ok = false;
}

async function changeRole(u: AdminUser, role: AdminUser["role"]) {
  try {
    await api.updateUser(u.id, { role });
    u.role = role;
    actionMsg.text = `已将 ${u.username} 的角色改为${role === "admin" ? "管理员" : "普通用户"}`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "修改角色失败");
  }
}

async function changeStatus(u: AdminUser, status: AdminUser["status"]) {
  try {
    await api.updateUser(u.id, { status });
    u.status = status;
    actionMsg.text = `已将 ${u.username} 的状态改为${statusLabel(status)}`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    reportError(e, "修改状态失败");
  }
}

const pwModal = reactive({
  open: false,
  userId: 0,
  username: "",
  password: "",
  loading: false,
  error: "",
});

function openResetModal(u: AdminUser) {
  pwModal.open = true;
  pwModal.userId = u.id;
  pwModal.username = u.username;
  pwModal.password = "";
  pwModal.error = "";
}

async function confirmResetPassword() {
  if (pwModal.password.length < 8) {
    pwModal.error = "密码至少8位";
    return;
  }
  pwModal.loading = true;
  try {
    await api.updateUser(pwModal.userId, { password: pwModal.password });
    pwModal.open = false;
    actionMsg.text = `已为 ${pwModal.username} 重置密码`;
    actionMsg.ok = true;
  } catch (e: unknown) {
    pwModal.error = e instanceof Error ? e.message : "重置密码失败";
  } finally {
    pwModal.loading = false;
  }
}

onMounted(fetchUsers);
</script>

<style scoped>
.admin-header {
  margin-bottom: 16px;
}
.action-msg {
  margin: 0 0 12px;
  padding: 10px 14px;
  border-radius: var(--chat-radius-sm);
  font-size: 13px;
}
.msg-ok {
  background: color-mix(in srgb, var(--ok) 10%, transparent);
  color: var(--ok-dim);
}
.msg-err {
  background: color-mix(in srgb, var(--err) 10%, transparent);
  color: var(--err);
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
  width: min(400px, calc(100vw - 48px));
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
.modal-input {
  width: 100%;
  padding: 8px 10px;
  margin-bottom: 12px;
  border: 1px solid var(--chat-border);
  border-radius: var(--chat-radius-sm);
  background: var(--chat-bg-body);
  color: var(--chat-text-primary);
  font-size: 13px;
  box-sizing: border-box;
  outline: none;
  transition:
    border-color 150ms,
    box-shadow 150ms;
}
.modal-input:focus {
  border-color: var(--chat-accent);
  box-shadow: 0 0 0 2px var(--chat-accent-soft);
}
.modal-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
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
.modal-btn--primary {
  background: var(--chat-accent);
  border-color: var(--chat-accent);
  color: var(--chat-accent-contrast);
}
.modal-btn--primary:hover:not(:disabled) {
  background: var(--chat-accent-hover);
}
.modal-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
