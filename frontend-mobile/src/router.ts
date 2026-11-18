import { createRouter, createWebHistory } from 'vue-router';

// base 与托管路径一致：局域网是 /m/；公网经 nginx 反代时页面在
// /xy/bot/m/<device_id>（前缀还可能更深），路由 base 必须跟着当前地址走，
// 否则 pushState 会把地址改到 /m/convo，之后的资源与刷新都会 404。
function resolveBase(): string {
  const path = window.location.pathname || '';
  const mMatch = path.match(/^(.*?\/m)(?:\/|$)/);
  if (mMatch && mMatch[1]) return mMatch[1] + '/';
  const injected = (window as any).__ycRelayDevice;
  const segs = path.split('/');
  if (typeof injected === 'string' && injected) {
    const at = segs.indexOf(injected);
    if (at > 1) return segs.slice(0, at).join('/') + '/';
  }
  return '/m/';
}

export const router = createRouter({
  history: createWebHistory(resolveBase()),
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
