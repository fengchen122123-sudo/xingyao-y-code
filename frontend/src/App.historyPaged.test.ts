import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { nextTick } from 'vue';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 注意：自动翻页只由「用户滚动」触发（滚动权状态机）。模拟用户滚动时必须先
// 派发 wheel 意图事件、再派发 scroll——纯程序化 scrollTop 赋值不再触发翻页，
// 那正是跳转定位 / 翻页补偿等程序化滚动的语义。
// 子组件全部 stub：滚动翻页接线都在 App 模板与脚本内，不依赖子组件行为
const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: { template: '<div />', methods: { refresh() {}, jumpToLoaded() {} } }, RemoteDirBrowser: true,
};

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
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));

  await vi.waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
  wrapper.unmount();
});

it('scrollTop >= 120 时不触发加载', async () => {
  const { store, wrapper, el } = mountWithHistory();
  const spy = vi.spyOn(store, 'loadOlderMessages').mockResolvedValue(undefined);

  setMetrics(el, { scrollTop: 120, scrollHeight: 2000 });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
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
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
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
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
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
    Object.defineProperty(el, 'scrollHeight', { value: oldHeight + added, configurable: true });
  });

  setMetrics(el, { scrollTop: oldTop, scrollHeight: oldHeight });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));

  await vi.waitFor(() => {
    expect(el.scrollTop).toBe(oldHeight + added - oldHeight + oldTop);
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
  const body = wrapper.find('#chat-body').element;
  expect(body.firstElementChild).toBe(indicator.element);

  store.historyLoading = false;
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="history-loading"]').exists()).toBe(false);
  wrapper.unmount();
});

it('翻页 prepend（historyLoading=true）不得强制吸底', async () => {
  const { store, wrapper, el } = mountWithHistory();
  setMetrics(el, { scrollTop: 0, scrollHeight: 2000 });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await nextTick();

  store.historyLoading = true;
  const older = Array.from({ length: 10 }, (_, i) => ({
    role: i % 2 ? 'assistant' : 'user',
    content: `更早-${i}`,
  }));
  store.messages = [...older, ...store.messages] as any;
  await nextTick();
  await new Promise(r => setTimeout(r, 60));

  // 不得被 force 吸底改写为 scrollHeight
  expect(el.scrollTop).not.toBe(el.scrollHeight);
  wrapper.unmount();
});

it('非翻页场景发送新消息仍然吸底（回归保护）', async () => {
  const { store, wrapper, el } = mountWithHistory();
  setMetrics(el, { scrollTop: 500, scrollHeight: 2000 });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await nextTick();

  store.historyLoading = false;
  store.messages = [...store.messages, { role: 'user', content: '新提问' }] as any;
  await nextTick();
  await new Promise(r => setTimeout(r, 60));

  expect(el.scrollTop).toBe(el.scrollHeight);
  wrapper.unmount();
});

it('浏览器 anchoring 已保持位置时不再重复补偿（防双重跳位）', async () => {
  const { store, wrapper, el } = mountWithHistory();
  setMetrics(el, { scrollTop: 10, scrollHeight: 2000 });
  vi.spyOn(store, 'loadOlderMessages').mockImplementation(async () => {
    Object.defineProperty(el, 'scrollHeight', { value: 2800, configurable: true });
    el.scrollTop = 810;
  });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await vi.waitFor(() => expect(el.scrollTop).toBe(810));
  await new Promise(r => setTimeout(r, 30));
  expect(el.scrollTop).toBe(810); // 不得被二次补偿成 1610
  wrapper.unmount();
});

it('浏览器 anchoring 未生效时兜底补偿内容增量', async () => {
  const { store, wrapper, el } = mountWithHistory();
  setMetrics(el, { scrollTop: 10, scrollHeight: 2000 });
  vi.spyOn(store, 'loadOlderMessages').mockImplementation(async () => {
    Object.defineProperty(el, 'scrollHeight', { value: 2800, configurable: true });
  });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await vi.waitFor(() => expect(el.scrollTop).toBe(810)); // 10 + 800 兜底补偿
  wrapper.unmount();
});

it('真实翻页流程（loading 在 finally 复位）不得触发吸底——时序漏洞复现', async () => {
  const { store, wrapper, el } = mountWithHistory();
  setMetrics(el, { scrollTop: 10, scrollHeight: 2000 });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await nextTick();

  // 真实 loadOlderMessages 流程：内部会把 loading 置位、prepend、finally 复位。
  // Vue watcher 异步 flush，回调执行时 loading 已复位——守卫必须不依赖它也能挡住。
  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockResolvedValue({
        ok: true,
        messages: [
          { role: 'user', content: '更早的提问' },
          { role: 'assistant', content: '更早的回复' },
        ],
        has_more: true,
        oldest_id: 5,
      }),
    },
  };
  await store.loadOlderMessages();
  await nextTick();
  await new Promise(r => setTimeout(r, 80));

  expect(store.historyLoading).toBe(false);
  expect(el.scrollTop).not.toBe(el.scrollHeight);
  wrapper.unmount();
});

it('prepend 后异步内容继续撑高（长对话图片/高亮分批渲染）时视口不得回退', async () => {
  const { store, wrapper, el } = mountWithHistory();
  const oldHeight = 2000;
  const oldTop = 10;
  vi.spyOn(store, 'loadOlderMessages').mockImplementation(async () => {
    Object.defineProperty(el, 'scrollHeight', { value: 2600, configurable: true });
    window.setTimeout(() => {
      Object.defineProperty(el, 'scrollHeight', { value: 3200, configurable: true });
    }, 30);
  });
  setMetrics(el, { scrollTop: oldTop, scrollHeight: oldHeight });
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));

  await new Promise(r => window.setTimeout(r, 150));
  // 视口应停在等效位置 oldTop + (3200 - 2000) = 1210；停在 610 即为「回退到下面」
  expect(el.scrollTop).toBe(1210);
  wrapper.unmount();
});
