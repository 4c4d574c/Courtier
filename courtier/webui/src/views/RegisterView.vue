<template>
  <div class="auth-page">
    <div class="auth-left">
      <div class="auth-brand">
        <img class="auth-seal" :src="logoUrl" alt="审衡" />
        <h1 class="auth-title">审衡</h1>
        <p class="auth-tagline">审以明辨，衡以持正</p>
      </div>
    </div>
    <div class="auth-right">
      <div class="auth-card">
        <template v-if="submitted">
          <div class="auth-success">
            <div class="auth-success-icon">✓</div>
            <h2>注册成功</h2>
            <p>请等待管理员审批后即可登录使用</p>
            <router-link
              to="/login"
              class="auth-submit"
              style="
                display: inline-flex;
                align-items: center;
                justify-content: center;
                text-decoration: none;
                margin-top: 24px;
              "
              >返回登录</router-link
            >
          </div>
        </template>
        <template v-else>
          <h2 class="auth-card-title">注册</h2>
          <form @submit.prevent="handleRegister" class="auth-form">
            <div class="auth-field">
              <label for="username">用户名</label>
              <input
                id="username"
                v-model="username"
                type="text"
                required
                minlength="1"
                maxlength="64"
              />
            </div>
            <div class="auth-field">
              <label for="email">邮箱</label>
              <input id="email" v-model="email" type="email" />
            </div>
            <div class="auth-field">
              <label for="password">密码</label>
              <input
                id="password"
                v-model="password"
                type="password"
                required
                minlength="8"
              />
            </div>
            <div class="auth-field">
              <label for="confirmPassword">确认密码</label>
              <input
                id="confirmPassword"
                v-model="confirmPassword"
                type="password"
                required
              />
            </div>
            <p v-if="error" class="auth-error">{{ error }}</p>
            <button type="submit" class="auth-submit" :disabled="loading">
              {{ loading ? "注册中..." : "注册" }}
            </button>
          </form>
          <p class="auth-switch">
            已有账号？<router-link to="/login">登录</router-link>
          </p>
        </template>
      </div>
    </div>
    <button
      class="auth-theme-toggle"
      type="button"
      title="切换主题"
      @click="toggleTheme"
    >
      <svg
        v-if="theme === 'dark'"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
      >
        <circle cx="12" cy="12" r="5" />
        <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" />
      </svg>
      <svg
        v-else
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
      >
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
      </svg>
    </button>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAuth } from "../composables/useAuth";
import { useTheme } from "../composables/useTheme";
import logoUrl from "../assets/logo.png";

const { register } = useAuth();
const { theme, initTheme, toggleTheme } = useTheme();

onMounted(initTheme);

const username = ref("");
const email = ref("");
const password = ref("");
const confirmPassword = ref("");
const error = ref("");
const loading = ref(false);
const submitted = ref(false);

const USERNAME_PATTERN = /^[a-zA-Z0-9_\-.@]+$/;

async function handleRegister() {
  error.value = "";
  if (!USERNAME_PATTERN.test(username.value)) {
    error.value = "用户名只能包含字母、数字、下划线、连字符、点和 @";
    return;
  }
  if (password.value !== confirmPassword.value) {
    error.value = "两次密码不一致";
    return;
  }
  if (password.value.length < 8) {
    error.value = "密码至少8位";
    return;
  }
  loading.value = true;
  try {
    await register(username.value, email.value, password.value);
    submitted.value = true;
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "注册失败";
  } finally {
    loading.value = false;
  }
}
</script>
