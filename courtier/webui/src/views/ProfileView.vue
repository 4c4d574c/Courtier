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
            user?.role === "admin" ? "管理员" : "审计员"
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
            class="profile-msg"
            :class="emailForm.ok ? 'msg-ok' : 'msg-err'"
          >
            {{ emailForm.msg }}
          </p>
          <button
            type="submit"
            class="auth-submit"
            :disabled="emailForm.loading"
            style="width: auto; padding: 0 24px"
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
            class="profile-msg"
            :class="pwForm.ok ? 'msg-ok' : 'msg-err'"
          >
            {{ pwForm.msg }}
          </p>
          <button
            type="submit"
            class="auth-submit"
            :disabled="pwForm.loading"
            style="width: auto; padding: 0 24px"
          >
            {{ pwForm.loading ? "更新中..." : "修改密码" }}
          </button>
        </form>
      </div>

      <div class="profile-footer">
        <router-link to="/">← 返回主页</router-link>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive } from "vue";
import { useAuth } from "../composables/useAuth";
import { api } from "../api/client";

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
  min-height: 100vh;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 80px;
  background: var(--paper);
}

.profile-card {
  width: 440px;
  max-width: 90vw;
}

.profile-heading {
  font-family: "Noto Serif SC", serif;
  font-size: 24px;
  color: var(--ink);
  margin: 0 0 32px;
}

.profile-section {
  margin-bottom: 28px;
  padding-bottom: 28px;
  border-bottom: 1px solid #e0dbd0;
}

.profile-section-title {
  font-family: "Noto Serif SC", serif;
  font-size: 18px;
  color: var(--ink-dim);
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
  font-size: 16px;
  color: var(--ink-dim);
}

.profile-value {
  font-size: 16px;
  color: var(--ink);
  font-weight: 500;
}

.profile-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.profile-msg {
  font-size: 15px;
  margin: 0;
}

.msg-ok {
  color: var(--ok);
}
.msg-err {
  color: var(--err);
}

.profile-footer {
  margin-top: 8px;
}

.profile-footer a {
  color: var(--ink-dim);
  font-size: 16px;
  text-decoration: none;
}

.profile-footer a:hover {
  color: var(--ink);
}
</style>
