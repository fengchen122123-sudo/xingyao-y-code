import { createRouter, createWebHashHistory } from "vue-router";
import { useAuthStore } from "./stores/auth";
import LoginView from "./views/LoginView.vue";
import ChatView from "./views/ChatView.vue";
import ApprovalsView from "./views/ApprovalsView.vue";
import SettingsView from "./views/SettingsView.vue";
import AdminLayout from "./admin/AdminLayout.vue";
import AccountsView from "./admin/AccountsView.vue";
import DevicesView from "./admin/DevicesView.vue";
import WorkersView from "./admin/WorkersView.vue";
import UsageView from "./admin/UsageView.vue";

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", redirect: "/chat" },
    { path: "/login", component: LoginView },
    { path: "/chat", component: ChatView },
    { path: "/approvals", component: ApprovalsView },
    { path: "/settings", component: SettingsView },
    {
      path: "/admin",
      component: AdminLayout,
      children: [
        { path: "", redirect: "/admin/accounts" },
        { path: "accounts", component: AccountsView },
        { path: "devices", component: DevicesView },
        { path: "workers", component: WorkersView },
        { path: "usage", component: UsageView },
      ],
    },
  ],
});

router.beforeEach((to) => {
  const auth = useAuthStore();
  if (to.path !== "/login" && !auth.signedIn) return "/login";
  if (to.path.startsWith("/admin") && auth.role !== "admin") return "/chat";
  return true;
});

export default router;
