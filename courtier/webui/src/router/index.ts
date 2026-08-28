import { createRouter, createWebHistory } from "vue-router";
import type { RouteRecordRaw } from "vue-router";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "Login",
    component: () => import("../views/LoginView.vue"),
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
        path: "extensions",
        name: "AdminExtensions",
        component: () => import("../views/ExtensionManagement.vue"),
      },
      {
        path: "settings",
        name: "AdminSettings",
        component: () => import("../views/SystemSettings.vue"),
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
    path: "/resources",
    name: "ResourceLibrary",
    component: () => import("../views/ResourceLibraryView.vue"),
    meta: { requiresAuth: true },
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
