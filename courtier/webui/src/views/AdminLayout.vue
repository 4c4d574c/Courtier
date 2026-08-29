<template>
  <div class="admin-layout">
    <aside class="admin-sidebar">
      <router-link to="/" class="admin-back">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <line x1="19" y1="12" x2="5" y2="12" />
          <polyline points="12 19 5 12 12 5" />
        </svg>
        <span>返回主页</span>
      </router-link>
      <nav class="admin-nav">
        <div v-for="group in navGroups" :key="group.label" class="admin-nav-group">
          <div class="admin-nav-group-label">{{ group.label }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.to"
            :to="item.to"
            class="admin-nav-item"
            active-class="active"
          >
            <svg
              class="admin-nav-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
              v-html="ICONS[item.icon]"
            ></svg>
            <span>{{ item.label }}</span>
          </router-link>
        </div>
      </nav>
    </aside>
    <main class="admin-content">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useAuth } from "../composables/useAuth";

// The admin shell doubles as the app's settings page (gear entry in the
// sidebar footer): non-admins land here too and only see the groups they
// can access (资源库).
const { isAdmin } = useAuth();

// Static single-source icon markup (feather-style strokes) — rendered via
// v-html but never built from user input.
const ICONS: Record<string, string> = {
  users:
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  userCheck:
    '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><polyline points="17 11 19 13 23 9"/>',
  package:
    '<line x1="16.5" y1="9.4" x2="7.5" y2="4.21"/><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/>',
  sparkle:
    '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
  library:
    '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
  gear:
    '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
};

interface NavItem {
  to: string;
  label: string;
  icon: string;
}

const navGroups = computed<{ label: string; items: NavItem[] }[]>(() => [
  ...(isAdmin.value
    ? [
        {
          label: "用户与权限",
          items: [
            { to: "/admin/users", label: "用户管理", icon: "users" },
            { to: "/admin/approvals", label: "注册审批", icon: "userCheck" },
          ],
        },
      ]
    : []),
  {
    label: "Agent能力",
    items: [
      { to: "/admin/plugins", label: "插件市场", icon: "package" },
      { to: "/admin/skills", label: "技能", icon: "sparkle" },
    ],
  },
  {
    label: "知识与记忆",
    items: [{ to: "/admin/resources", label: "资源库", icon: "library" }],
  },
  ...(isAdmin.value
    ? [
        {
          label: "系统管理",
          items: [{ to: "/admin/settings", label: "系统设置", icon: "gear" }],
        },
      ]
    : []),
]);
</script>
