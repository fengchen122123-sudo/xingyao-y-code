import { createRouter, createWebHistory } from 'vue-router';

// base 与 lan_server 的 /m/ 托管路径一致
export const router = createRouter({
  history: createWebHistory('/m/'),
  routes: [
    { path: '/', redirect: '/convo' },
    { path: '/convo', component: () => import('./views/ConvoListView.vue'), meta: { tab: 'convo' } },
    { path: '/convo/:id', component: () => import('./views/ConvoDetailView.vue'), props: true },
    { path: '/approvals', component: () => import('./views/ApprovalsView.vue'), meta: { tab: 'approvals' } },
    { path: '/settings', component: () => import('./views/SettingsView.vue'), meta: { tab: 'settings' } },
    { path: '/:pathMatch(.*)*', redirect: '/convo' },
  ],
});

// stale-shell self-heal：懒路由 chunk 加载失败（构建更新后命中旧缓存 index）时
// 做一次整页刷新取新资源；按路径防刷新死循环，导航成功即清除。
const RELOAD_FLAG = 'yc-route-reload';
router.onError((error, to) => {
  const msg = String((error as any)?.message || error);
  if (!/import|module|chunk|fetch|load/i.test(msg)) return;
  const to_ = to.fullPath || '/';
  if (sessionStorage.getItem(RELOAD_FLAG) === to_) return;
  sessionStorage.setItem(RELOAD_FLAG, to_);
  window.location.replace(router.resolve(to).href);
});
router.afterEach(() => sessionStorage.removeItem(RELOAD_FLAG));
