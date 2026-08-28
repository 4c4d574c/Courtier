import { createRouter, createWebHistory } from "vue-router";
import type { RouteRecordRaw } from "vue-router";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "Login",
    component: () => import("../views/LoginView.vue"),
    meta: { guest: true },
  },  {
    path: "/setup",
    name: "Setup",
    component: () => import("../views/SetupView.vue"),
    meta: { guest: true },
  },
  {
    path: "/register",
    name: "Register",
    component: () => import("../views/RegisterView.vue"),
    meta: { guest: true },
  },
  {
    path: "/",
    name: "Home",
    component: () => import("../views/HomeView.vue"),
    meta: { requiresAuth: true },
  },
  {
    path: "/admin",
    component: () => import("../views/AdminLayout.vue"),
    meta: { requiresAuth: true, requiresAdmin: true },
    children: [
      {
        path: "users",
        name: "AdminUsers",
        component: () => import("../views/UserManagement.vue"),
      },
      {
        path: "approvals",
        name: "AdminApprovals",
        component: () => import("../views/ApprovalManagement.vue"),
      },
      {
        path: "plugins",
        name: "AdminPlugins",
        component: () => import("../views/PluginMarketView.vue"),
      },
      {
        path: "skills",
        name: "AdminSkills",
        component: () => import("../views/SkillsView.vue"),
      },
      {
        path: "skills/:domain/:name",
        name: "AdminSkillEdit",
        component: () => import("../views/SkillEditView.vue"),
      },
      { path: "extensions", redirect: "/admin/plugins" },
      {
        path: "settings",
        name: "AdminSettings",
        component: () => import("../views/SystemSettings.vue"),
      },
      {
        // Resource library lives in the settings shell for every user;
        // the child meta overrides the admin-only parent for the guard.
        path: "resources",
        name: "AdminResources",
        component: () => import("../views/ResourceLibraryView.vue"),
        meta: { requiresAdmin: false },
      },
      { path: "", redirect: "/admin/users" },
    ],
  },
  {
    path: "/profile",
    name: "Profile",
    component: () => import("../views/ProfileView.vue"),
    meta: { requiresAuth: true },
  },
  {
    // Legacy standalone path — the library renders inside the settings
    // shell now; deep links keep working via this redirect.
    path: "/resources",
    redirect: "/admin/resources",
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

const router = createRouter({
  history: createWebHistory(),
  routes,
});

router.beforeEach(async (to, _from, next) => {
  const { useAuth } = await import("../composables/useAuth");
  const { user, isAdmin, initAuth } = useAuth();

  if (!user.value) {
    await initAuth();
  }

  // First-run wizard: unauthenticated visits land on /setup while the
  // backend reports that no admin exists yet (checked once per session).
  if (!user.value && to.name !== "Setup") {
    const { api } = await import("../api/client");
    try {
      const status = await api.setupStatus();
      if (status.setup_required) return next({ name: "Setup" });
    } catch {
      /* status endpoint unavailable — proceed with normal auth flow */
    }
  }

  if (to.meta.requiresAuth && !user.value) {
    return next({ name: "Login", query: { redirect: to.fullPath } });
  }

  if (to.meta.guest && user.value) {
    return next({ name: "Home" });
  }

  if (to.meta.requiresAdmin && !isAdmin.value) {
    return next({ name: "Home" });
  }

  next();
});

export default router;
