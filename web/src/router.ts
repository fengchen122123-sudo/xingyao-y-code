import { createRouter, createWebHashHistory } from "vue-router";
import { useAuthStore } from "./stores/auth";
import LoginView from "./views/LoginView.vue";
import ChatView from "./views/ChatView.vue";
import ApprovalsView from "./views/ApprovalsView.vue";
import SettingsView from "./views/SettingsView.vue";
import RemoteView from "./views/RemoteView.vue";
import AdminLayout from "./admin/AdminLayout.vue";
import AccountsView from "./admin/AccountsView.vue";
import ModelsView from "./admin/ModelsView.vue";
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
    { path: "/remote/:deviceId", component: RemoteView, meta: { remote: true } },
    {
      path: "/admin",
      component: AdminLayout,
      children: [
        { path: "", redirect: "/admin/accounts" },
        { path: "accounts", component: AccountsView },
        { path: "models", component: ModelsView },
        { path: "devices", component: DevicesView },
        { path: "workers", component: WorkersView },
        { path: "usage", component: UsageView },
      ],
    },
  ],
});

router.beforeEach((to) => {
  const auth = useAuthStore();
  // 远端页自己用 URL 中的短期令牌建立连接，不依赖服务器会话存储。
  if (to.meta.remote) return true;
  if (to.path !== "/login" && !auth.signedIn) return "/login";
  if (to.path.startsWith("/admin") && auth.role !== "admin") return "/chat";
  return true;
});

export default router;
