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
        <h2 class="auth-card-title">登录</h2>
        <form @submit.prevent="handleLogin" class="auth-form">
          <div class="auth-field">
            <label for="username">用户名</label>
            <input
              id="username"
              v-model="username"
              type="text"
              autocomplete="username"
              required
            />
          </div>
          <div class="auth-field">
            <label for="password">密码</label>
            <input
              id="password"
              v-model="password"
              type="password"
              autocomplete="current-password"
              required
            />
          </div>
          <p v-if="error" class="auth-error">{{ error }}</p>
          <button type="submit" class="auth-submit" :disabled="loading">
            {{ loading ? "登录中..." : "登录" }}
          </button>
        </form>
        <p class="auth-switch">
          还没有账号？<router-link to="/register">注册</router-link>
        </p>
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
import { useRouter, useRoute } from "vue-router";
import { useAuth } from "../composables/useAuth";
import { useTheme } from "../composables/useTheme";
import logoUrl from "../assets/logo.png";

const router = useRouter();
const route = useRoute();
const { login } = useAuth();
const { theme, initTheme, toggleTheme } = useTheme();

onMounted(initTheme);

const username = ref("");
const password = ref("");
const error = ref("");
const loading = ref(false);

async function handleLogin() {
  error.value = "";
  loading.value = true;
  try {
    await login(username.value, password.value);
    // Only allow in-app redirects (block scheme-relative/external URLs).
    const raw = (route.query.redirect as string) || "/";
    const redirect = raw.startsWith("/") && !raw.startsWith("//") ? raw : "/";
    router.push(redirect);
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "登录失败";
  } finally {
    loading.value = false;
  }
}
</script>
