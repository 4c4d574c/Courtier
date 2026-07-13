<template>
  <div class="auth-page">
    <div class="auth-left">
      <div class="auth-brand">
        <div class="auth-seal"><span>审</span></div>
        <h1 class="auth-title">SDTAgent</h1>
        <p class="auth-tagline">文档智能审计平台</p>
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
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useAuth } from "../composables/useAuth";

const { register } = useAuth();

const username = ref("");
const email = ref("");
const password = ref("");
const confirmPassword = ref("");
const error = ref("");
const loading = ref(false);
const submitted = ref(false);

async function handleRegister() {
  error.value = "";
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
