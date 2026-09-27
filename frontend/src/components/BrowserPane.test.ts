import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import BrowserPane from './BrowserPane.vue';
import { useAppStore } from '../stores/appStore';

// jsdom 无 ResizeObserver：显式提供空实现，覆盖「存在但不回调」的分支
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// 遮挡检测的命中结果：默认返回表面自身（不遮挡）
let surfaceEl: Element | null = null;

function mockApi() {
  return {
    browser_pane_open: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_set_bounds: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_navigate: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_back: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_forward: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_reload: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_stop: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_hide: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_get_state: vi.fn().mockResolvedValue({ ok: true, state: null }),
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
  surfaceEl = null;
  (globalThis as any).ResizeObserver = ResizeObserverStub;
  // jsdom 的 getBoundingClientRect 恒为 0 矩形，会让「宽高 < 2 视为不可见」
  // 命中，open 永远不会被调用：给所有元素一个真实的表面矩形
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, width: 300, height: 400,
    right: 300, bottom: 400, toJSON: () => ({}),
  } as DOMRect);
  (document as any).elementFromPoint = vi.fn(() => surfaceEl);
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function mountPane(active = true) {
  const wrapper = mount(BrowserPane, { props: { active } });
  surfaceEl = wrapper.find('[data-test="bp-surface"]').element;
  // 等 composable 的 onMounted → nextTick(syncNow) 跑完（首次矩形上报/open）
  await nextTick();
  await nextTick();
  return wrapper;
}

it('无 pywebview 桥时显示不支持态文案并禁用工具栏', async () => {
  const wrapper = await mountPane(true);

  const notice = wrapper.find('[data-test="bp-unsupported"]');
  expect(notice.exists()).toBe(true);
  expect(notice.text()).toContain('浏览器面板需要桌面端 GUI 宿主支持');
  expect(notice.text()).toContain('内置浏览器仅在 Windows 桌面端 GUI 中可用。');
  // 工具栏整体禁用，且不渲染起始页
  expect(wrapper.find('[data-test="bp-back"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-forward"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-addr"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(false);
  wrapper.unmount();
});

it('active 且已有页面时同步矩形并首次 open(rect, "")', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  // 非起始页：允许原生表面显示（起始页态会主动 hide）
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);

  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  const [rect, url] = api.browser_pane_open.mock.calls[0];
  expect(rect).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  expect(url).toBe('');
  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  wrapper.unmount();
});

it('地址栏回车提交：普通词走必应搜索，域名补 https://', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  const addr = wrapper.find('[data-test="bp-addr"]');

  await addr.trigger('focus');
  await addr.setValue('hello world');
  await addr.trigger('keydown', { key: 'Enter' });
  expect(api.browser_pane_navigate).toHaveBeenCalledWith(
    'https://www.bing.com/search?q=hello%20world',
  );

  await addr.trigger('focus');
  await addr.setValue('example.com');
  await addr.trigger('keydown', { key: 'Enter' });
  expect(api.browser_pane_navigate).toHaveBeenCalledWith('https://example.com');
  wrapper.unmount();
});

it('IME 组合态下的 Enter 不提交（交由输入法上屏）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  const addr = wrapper.find('[data-test="bp-addr"]');
  await addr.trigger('focus');
  await addr.setValue('中文候选');
  await addr.trigger('keydown', { key: 'Enter', isComposing: true });
  expect(api.browser_pane_navigate).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('起始页快捷卡片点击 → navigate 对应站点', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };

  const wrapper = await mountPane(true);
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(true);

  await wrapper.find('[data-test="bp-site-github"]').trigger('click');
  // 首次导航被串行化到 open 之后（B1），用 waitFor 等 Promise 链落地
  await vi.waitFor(() =>
    expect(api.browser_pane_navigate).toHaveBeenCalledWith('https://github.com'),
  );
  wrapper.unmount();
});

it('首次导航串行等待 open 落地后才发出（B1 竞态回归）', async () => {
  const api = mockApi();
  let resolveOpen: (value: unknown) => void = () => {};
  api.browser_pane_open.mockReturnValue(
    new Promise((resolve) => { resolveOpen = resolve; }),
  );
  (window as any).pywebview = { api };

  const wrapper = await mountPane(true);
  // 起始页态：表面隐藏，open 尚未调用
  expect(api.browser_pane_open).not.toHaveBeenCalled();

  await wrapper.find('[data-test="bp-site-github"]').trigger('click');
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  // open 还挂着（模拟 WebView2 创建/内核初始化耗时）：navigate 不得抢跑
  expect(api.browser_pane_navigate).not.toHaveBeenCalled();

  resolveOpen({ ok: true });
  await vi.waitFor(() =>
    expect(api.browser_pane_navigate).toHaveBeenCalledWith('https://github.com'),
  );
  wrapper.unmount();
});

it('矩形未变化时 set_bounds 去重不重发', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  // 多次触发同步源：矩形不变 → 不应产生任何 set_bounds 调用
  window.dispatchEvent(new Event('resize'));
  window.dispatchEvent(new Event('resize'));
  await nextTick();
  await nextTick();
  expect(api.browser_pane_set_bounds).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('表面被 DOM 覆盖层遮挡时让宿主 hide 原生表面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  // elementFromPoint 命中 null（模拟设置页/弹窗压住表面中心）→ 判遮挡
  surfaceEl = null;
  window.dispatchEvent(new Event('resize'));
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());
  wrapper.unmount();
});

it('组件卸载时让宿主 hide 原生表面（清理不残留）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  wrapper.unmount();
  expect(api.browser_pane_hide).toHaveBeenCalled();
});

it('can_go_back=false 时后退禁用；前进/刷新/停止调对应方法', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.embeddedBrowser.url = 'https://example.com';
  store.embeddedBrowser.can_go_back = false;
  store.embeddedBrowser.can_go_forward = true;

  const wrapper = await mountPane(true);

  expect(wrapper.find('[data-test="bp-back"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-forward"]').attributes('disabled')).toBeUndefined();

  await wrapper.find('[data-test="bp-forward"]').trigger('click');
  expect(api.browser_pane_forward).toHaveBeenCalled();

  await wrapper.find('[data-test="bp-reload"]').trigger('click');
  expect(api.browser_pane_reload).toHaveBeenCalled();

  // loading 时刷新按钮变停止：图标旋转 + 调用 browser_pane_stop
  store.embeddedBrowser.loading = true;
  await nextTick();
  await wrapper.find('[data-test="bp-reload"]').trigger('click');
  expect(api.browser_pane_stop).toHaveBeenCalled();
  wrapper.unmount();
});

it('页签切走（active 由 true 变 false）时让宿主 hide 原生表面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  expect(api.browser_pane_hide).not.toHaveBeenCalled();

  // 切走（hide 只在可见性翻转时调用，去重避免重复指令）
  await wrapper.setProps({ active: false });
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());
  wrapper.unmount();
});

it('open 返回 unsupported_platform 时切不支持态', async () => {
  const api = mockApi();
  api.browser_pane_open.mockResolvedValue({ ok: false, error: 'unsupported_platform' });
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);

  await vi.waitFor(() =>
    expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(true),
  );
  wrapper.unmount();
});
