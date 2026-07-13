<template>
  <div class="admin-layout">
    <aside class="admin-sidebar">
      <router-link to="/" class="admin-logo">
        <div class="admin-seal"><span>审</span></div>
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
