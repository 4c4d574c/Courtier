<template>
  <div class="auth-page">
    <div class="auth-left">
      <div class="auth-brand">
        <div class="auth-seal">
          <span>审</span>
        </div>
        <h1 class="auth-title">SDTAgent</h1>
        <p class="auth-tagline">文档智能审计平台</p>
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
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useAuth } from "../composables/useAuth";

const router = useRouter();
const route = useRoute();
const { login } = useAuth();

const username = ref("");
const password = ref("");
const error = ref("");
const loading = ref(false);

async function handleLogin() {
  error.value = "";
  loading.value = true;
  try {
    await login(username.value, password.value);
    const redirect = (route.query.redirect as string) || "/";
    router.push(redirect);
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : "登录失败";
  } finally {
    loading.value = false;
  }
}
</script>
