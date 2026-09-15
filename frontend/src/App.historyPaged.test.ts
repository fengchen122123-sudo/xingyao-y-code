import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 子组件全部 stub：滚动翻页接线都在 App 模板与脚本内，不依赖子组件行为
const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
};

/** jsdom 无布局：scrollTop 可直接赋值，scrollHeight 需 defineProperty 模拟 */
function setMetrics(el: HTMLElement, metrics: { scrollTop: number; scrollHeight: number }) {
  Object.defineProperty(el, 'scrollHeight', { value: metrics.scrollHeight, configurable: true });
  el.scrollTop = metrics.scrollTop;
}

function mountWithHistory() {
  const store = useAppStore();
  store.initialized = true;
  store.messages = [{ role: 'user', content: 'hi' } as any];
  store.historyHasMore = true;
  store.historyOldestId = 100;
  const wrapper = mount(App, { global: { stubs } });
  const el = wrapper.find('#chat-scroll').element as HTMLElement;
  return { store, wrapper, el };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('滚动到近顶部（scrollTop < 120）时触发加载更早历史', async () => {
  const { store, wrapper, el } = mountWithHistory();
  const spy = vi.spyOn(store, 'loadOlderMessages').mockResolvedValue(undefined);

  setMetrics(el, { scrollTop: 50, scrollHeight: 2000 });
  el.dispatchEvent(new Event('scroll'));

  await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  wrapper.unmount();
});

it('scrollTop >= 120 时不触发加载', async () => {
  const { store, wrapper, el } = mountWithHistory();
  const spy = vi.spyOn(store, 'loadOlderMessages').mockResolvedValue(undefined);

  setMetrics(el, { scrollTop: 120, scrollHeight: 2000 });
  el.dispatchEvent(new Event('scroll'));
  await flushPromises();

  expect(spy).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('historyHasMore=false 时不触发加载', async () => {
  const { store, wrapper, el } = mountWithHistory();
  store.historyHasMore = false;
  const spy = vi.spyOn(store, 'loadOlderMessages').mockResolvedValue(undefined);

  setMetrics(el, { scrollTop: 0, scrollHeight: 2000 });
  el.dispatchEvent(new Event('scroll'));
  await flushPromises();

  expect(spy).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('historyLoading 进行中时不重复触发加载', async () => {
  const { store, wrapper, el } = mountWithHistory();
  store.historyLoading = true;
  const spy = vi.spyOn(store, 'loadOlderMessages').mockResolvedValue(undefined);

  setMetrics(el, { scrollTop: 0, scrollHeight: 2000 });
  el.dispatchEvent(new Event('scroll'));
  await flushPromises();

  expect(spy).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('prepend 旧消息后 scrollTop 按 scrollHeight 增量补偿（视口不跳动）', async () => {
  const { store, wrapper, el } = mountWithHistory();
  const oldHeight = 2000;
  const oldTop = 60;
  const added = 800;
  vi.spyOn(store, 'loadOlderMessages').mockImplementation(async () => {
    // 模拟 store prepend 一页旧消息后内容变高
    Object.defineProperty(el, 'scrollHeight', { value: oldHeight + added, configurable: true });
  });

  setMetrics(el, { scrollTop: oldTop, scrollHeight: oldHeight });
  el.dispatchEvent(new Event('scroll'));

  await vi.waitFor(() => {
    expect(el.scrollTop).toBe(oldHeight + added - oldHeight + oldTop); // 860
  });
  wrapper.unmount();
});

it('historyLoading 时消息流顶部显示加载指示，结束后隐藏', async () => {
  const { store, wrapper } = mountWithHistory();

  expect(wrapper.find('[data-test="history-loading"]').exists()).toBe(false);

  store.historyLoading = true;
  await wrapper.vm.$nextTick();
  const indicator = wrapper.find('[data-test="history-loading"]');
  expect(indicator.exists()).toBe(true);
  // 指示条位于 chat-body 内最前
  const body = wrapper.find('#chat-body').element;
  expect(body.firstElementChild).toBe(indicator.element);

  store.historyLoading = false;
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="history-loading"]').exists()).toBe(false);
  wrapper.unmount();
});