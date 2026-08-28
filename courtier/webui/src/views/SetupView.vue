<template>
  <div class="setup-page">
    <div class="setup-card">
      <h1>系统初始化</h1>
      <p class="setup-hint">
        尚未创建管理员账户。请设置首个管理员；创建后本页将永久关闭，日常管理请使用「用户管理」。
      </p>

      <form class="setup-form" @submit.prevent="submit">
        <label class="setup-field">
          <span>用户名</span>
          <input
            v-model="username"
            class="setup-input"
            autocomplete="username"
            placeholder="3-64 位，字母/数字/_.-"
            required
          />
        </label>
        <label class="setup-field">
          <span>邮箱（可选）</span>
          <input v-model="email" class="setup-input" type="email" autocomplete="email" />
        </label>
        <label class="setup-field">
          <span>密码</span>
          <input
            v-model="password"
            class="setup-input"
            type="password"
            autocomplete="new-password"
            placeholder="至少 8 位"
            required
            minlength="8"
          />
        </label>
        <label class="setup-field">
          <span>确认密码</span>
          <input
            v-model="confirm"
            class="setup-input"
            type="password"
            autocomplete="new-password"
            required
            minlength="8"
          />
        </label>
        <label v-if="needsSetupKey" class="setup-field">
          <span>初始化令牌（COURTIER_SETUP_KEY）</span>
          <input v-model="setupKey" class="setup-input" type="password" />
        </label>

        <p v-if="error" class="setup-error">{{ error }}</p>

        <button class="setup-submit" type="submit" :disabled="submitting">
          {{ submitting ? "创建中…" : "创建管理员" }}
        </button>
      </form>

      <p v-if="done" class="setup-done">
        管理员已创建。
        <router-link to="/login" class="setup-login-link">前往登录<AppIcon name="arrow-right" :size="14" /></router-link>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from "../components/AppIcon.vue";
import { onMounted, ref } from "vue";

import { api } from "../api/client";

const username = ref("");
const email = ref("");
const password = ref("");
const confirm = ref("");
const setupKey = ref("");
const needsSetupKey = ref(false);
const error = ref("");
const done = ref(false);
const submitting = ref(false);

onMounted(async () => {
  try {
    const status = await api.setupStatus();
    if (!status.setup_required) {
      done.value = true;
      error.value = "管理员已存在，无需初始化。";
    }
  } catch {
    needsSetupKey.value = true; // status unreachable — assume guarded env
  }
});

async function submit() {
  error.value = "";
  if (password.value !== confirm.value) {
    error.value = "两次输入的密码不一致";
    return;
  }
  submitting.value = true;
  try {
    await api.createAdmin({
      username: username.value,
      password: password.value,
      email: email.value || undefined,
      setup_key: setupKey.value || undefined,
    });
    done.value = true;
  } catch (exc) {
    const message = String(exc);
    error.value = message;
    needsSetupKey.value = needsSetupKey.value || message.includes("SETUP_KEY");
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
.setup-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.setup-card {
  width: min(420px, 100%);
  border: 1px solid rgba(127, 127, 127, 0.3);
  border-radius: 12px;
  padding: 28px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.setup-hint {
  font-size: 13px;
  opacity: 0.75;
}
.setup-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.setup-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.setup-input {
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid rgba(127, 127, 127, 0.35);
  background: transparent;
  color: inherit;
}
.setup-error {
  color: #e06c75;
  font-size: 13px;
  margin: 0;
}
.setup-done {
  color: #98c379;
  font-size: 13px;
  margin: 0;
}

.setup-login-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: inherit;
  font-weight: 600;
  text-decoration: none;
}
.setup-submit {
  padding: 10px;
  border-radius: 8px;
  border: none;
  background: rgba(46, 160, 67, 0.35);
  color: inherit;
  cursor: pointer;
}
.setup-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
