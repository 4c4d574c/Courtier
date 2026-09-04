<template>
  <div class="profile-page">
    <div class="profile-card">
      <h1 class="profile-heading">个人设置</h1>

      <div class="profile-section">
        <div class="profile-field">
          <label>用户名</label>
          <span class="profile-value">{{ user?.username }}</span>
        </div>
        <div class="profile-field">
          <label>角色</label>
          <span class="badge" :class="'badge-' + user?.role">{{
            user?.role === "admin" ? "管理员" : "普通用户"
          }}</span>
        </div>
      </div>

      <div class="profile-section">
        <h2 class="profile-section-title">修改邮箱</h2>
        <form @submit.prevent="updateEmail" class="profile-form">
          <div class="auth-field">
            <label for="email">新邮箱</label>
            <input id="email" v-model="emailForm.email" type="email" />
          </div>
          <p
            v-if="emailForm.msg"
            :class="emailForm.ok ? 'msg-ok' : 'msg-err'"
          >
            {{ emailForm.msg }}
          </p>
          <button
            type="submit"
            class="auth-submit profile-btn"
            :disabled="emailForm.loading"
          >
            {{ emailForm.loading ? "更新中..." : "更新邮箱" }}
          </button>
        </form>
      </div>

      <div class="profile-section">
        <h2 class="profile-section-title">修改密码</h2>
        <form @submit.prevent="updatePassword" class="profile-form">
          <div class="auth-field">
            <label for="currentPassword">当前密码</label>
            <input
              id="currentPassword"
              v-model="pwForm.current"
              type="password"
              required
            />
          </div>
          <div class="auth-field">
            <label for="newPassword">新密码</label>
            <input
              id="newPassword"
              v-model="pwForm.newPw"
              type="password"
              required
              minlength="8"
            />
          </div>
          <div class="auth-field">
            <label for="confirmNewPassword">确认新密码</label>
            <input
              id="confirmNewPassword"
              v-model="pwForm.confirm"
              type="password"
              required
            />
          </div>
          <p
            v-if="pwForm.msg"
            :class="pwForm.ok ? 'msg-ok' : 'msg-err'"
          >
            {{ pwForm.msg }}
          </p>
          <button
            type="submit"
            class="auth-submit profile-btn"
            :disabled="pwForm.loading"
          >
            {{ pwForm.loading ? "更新中..." : "修改密码" }}
          </button>
        </form>
      </div>

      <div v-if="user?.role !== 'admin'" class="profile-section profile-deletion">
        <h2 class="profile-section-title">注销账号</h2>
        <template v-if="deletion.request">
          <p class="profile-deletion-note">
            注销申请审核中，账号期间可正常使用。批准后将清除你的全部个人数据
            （会话、上传文件、个人资源、审核记录、记忆等），且用户名将被释放，操作不可恢复。
          </p>
          <button
            type="button"
            class="auth-submit profile-danger-btn profile-btn"
            :disabled="deletion.loading"
            @click="cancelDeletion"
          >
            {{ deletion.loading ? "处理中..." : "撤回注销申请" }}
          </button>
        </template>
        <template v-else>
          <p class="profile-deletion-note">
            提交后需管理员审核，批准前账号可正常使用。批准后将清除你的全部个人数据，
            用户名将被释放，操作不可恢复。
          </p>
          <form @submit.prevent="submitDeletion" class="profile-form">
            <div class="auth-field">
              <label for="deletionPassword">登录密码确认</label>
              <input
                id="deletionPassword"
                v-model="deletion.password"
                type="password"
                required
                autocomplete="current-password"
              />
            </div>
            <p v-if="deletion.msg" :class="deletion.ok ? 'msg-ok' : 'msg-err'">
              {{ deletion.msg }}
            </p>
            <button
              type="submit"
              class="auth-submit profile-danger-btn profile-btn"
              :disabled="deletion.loading || !deletion.password"
            >
              {{ deletion.loading ? "提交中..." : "申请注销账号" }}
            </button>
          </form>
        </template>
      </div>

      <div class="profile-footer">
        <router-link to="/" class="profile-back"><AppIcon name="arrow-left" :size="14" />返回主页</router-link>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from "../components/AppIcon.vue";
import { onMounted, reactive } from "vue";
import { useAuth } from "../composables/useAuth";
import { api, type DeletionRequest } from "../api/client";

const { user } = useAuth();

const emailForm = reactive({ email: "", loading: false, msg: "", ok: false });
const pwForm = reactive({
  current: "",
  newPw: "",
  confirm: "",
  loading: false,
  msg: "",
  ok: false,
});

async function updateEmail() {
  emailForm.msg = "";
  if (!emailForm.email) {
    emailForm.msg = "请输入新邮箱";
    emailForm.ok = false;
    return;
  }
  emailForm.loading = true;
  try {
    await api.updateProfile({ email: emailForm.email });
    emailForm.msg = "邮箱更新成功";
    emailForm.ok = true;
  } catch (e: unknown) {
    emailForm.msg = e instanceof Error ? e.message : "更新失败";
    emailForm.ok = false;
  } finally {
    emailForm.loading = false;
  }
}

const deletion = reactive({
  password: "",
  loading: false,
  msg: "",
  ok: false,
  request: null as DeletionRequest | null,
});

async function loadDeletionRequest() {
  try {
    deletion.request = (await api.getMyDeletionRequest()).request;
  } catch {
    deletion.request = null;
  }
}

async function submitDeletion() {
  deletion.msg = "";
  deletion.loading = true;
  try {
    await api.submitDeletionRequest(deletion.password);
    deletion.password = "";
    deletion.ok = true;
    deletion.msg = "注销申请已提交，等待管理员审核";
    await loadDeletionRequest();
  } catch (e: unknown) {
    deletion.ok = false;
    deletion.msg = e instanceof Error ? e.message : "提交失败";
  } finally {
    deletion.loading = false;
  }
}

async function cancelDeletion() {
  deletion.loading = true;
  try {
    await api.cancelDeletionRequest();
    deletion.request = null;
    deletion.ok = true;
    deletion.msg = "已撤回注销申请";
  } catch (e: unknown) {
    deletion.ok = false;
    deletion.msg = e instanceof Error ? e.message : "撤回失败";
  } finally {
    deletion.loading = false;
  }
}

onMounted(loadDeletionRequest);

async function updatePassword() {
  pwForm.msg = "";
  if (pwForm.newPw !== pwForm.confirm) {
    pwForm.msg = "两次密码不一致";
    pwForm.ok = false;
    return;
  }
  if (pwForm.newPw.length < 8) {
    pwForm.msg = "密码至少8位";
    pwForm.ok = false;
    return;
  }
  pwForm.loading = true;
  try {
    await api.updateProfile({
      current_password: pwForm.current,
      new_password: pwForm.newPw,
    });
    pwForm.msg = "密码修改成功";
    pwForm.ok = true;
    pwForm.current = "";
    pwForm.newPw = "";
    pwForm.confirm = "";
  } catch (e: unknown) {
    pwForm.msg = e instanceof Error ? e.message : "修改失败";
    pwForm.ok = false;
  } finally {
    pwForm.loading = false;
  }
}
</script>

<style scoped>
.profile-page {
  /* html/body/#app are overflow:hidden for the chat layout, so this page
     must scroll internally — otherwise tall content (and the footer back
     link) is clipped and unreachable. */
  height: 100%;
  overflow-y: auto;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: 80px 0 40px;
  background: var(--chat-bg-body);
}

.profile-card {
  width: 440px;
  max-width: 90vw;
}

.profile-heading {
  font-family: "Noto Serif SC", serif;
  font-size: 26px;
  color: var(--chat-text-primary);
  margin: 0 0 32px;
}

.profile-section {
  margin-bottom: 28px;
  padding-bottom: 28px;
  border-bottom: 1px solid var(--chat-border);
}

.profile-section-title {
  font-family: "Noto Serif SC", serif;
  font-size: 20px;
  color: var(--chat-text-secondary);
  margin: 0 0 16px;
  font-weight: 600;
}

.profile-field {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
}

.profile-field label {
  font-size: 18px;
  color: var(--chat-text-secondary);
}

.profile-value {
  font-size: 18px;
  color: var(--chat-text-primary);
  font-weight: 500;
}

.profile-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.profile-btn {
  width: auto;
  padding: 0 24px;
}

.profile-footer {
  margin-top: 8px;
}

.profile-footer a {
  color: var(--chat-text-secondary);
  font-size: 18px;
  text-decoration: none;
}

.profile-footer a:hover {
  color: var(--chat-text-primary);
}
.profile-back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--chat-text-secondary);
  text-decoration: none;
  font-size: 13px;
}

.profile-deletion-note {
  font-size: 16px;
  color: var(--chat-text-tertiary);
  margin: 0 0 14px;
  line-height: 1.6;
}

.profile-danger-btn {
  background: var(--err);
  border-color: var(--err);
  color: var(--err-contrast);
}

.profile-back:hover {
  color: var(--chat-text-primary);
}
</style>
