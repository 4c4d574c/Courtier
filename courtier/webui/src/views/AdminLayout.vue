<template>
  <div class="admin-layout">
    <aside class="admin-sidebar">
      <router-link to="/" class="admin-logo">
        <img class="admin-seal" :src="logoUrl" alt="审衡" />
        <span class="admin-logo-text">审衡</span>
      </router-link>
      <nav class="admin-nav">
        <router-link
          to="/admin/users"
          class="admin-nav-item"
          active-class="active"
          >用户管理</router-link
        >
        <router-link
          to="/admin/approvals"
          class="admin-nav-item"
          active-class="active"
          >注册审批</router-link
        >
        <router-link
          to="/admin/plugins"
          class="admin-nav-item"
          active-class="active"
          >插件市场</router-link
        >
        <router-link
          to="/admin/skills"
          class="admin-nav-item"
          active-class="active"
          >技能</router-link
        >
        <router-link
          to="/admin/settings"
          class="admin-nav-item"
          active-class="active"
          >系统设置</router-link
        >
      </nav>
      <div class="admin-sidebar-footer">
        <router-link to="/" class="admin-nav-item">← 返回主页</router-link>
      </div>
    </aside>
    <main class="admin-content">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import logoUrl from "../assets/logo.png";
</script>
