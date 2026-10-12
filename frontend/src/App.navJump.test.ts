import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { defineComponent } from 'vue';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 红点跳转的滚动权状态机（following）：
// - 跳转发起（jumpStart）与跨页翻页都显式退出跟随（following=false）；
// - 跳转期间与结束后，内容变化（prepend/流式/新消息）都不吸底；
// - 只有用户滚回底部（用户意图 + 落点贴底）才恢复跟随。
// 旧实现在这里依赖「抑制窗口 + located 后延迟恢复」，窗口时序脆弱：
// 恢复后任何内容变化都会把跳转后的视口拽回底部（"无法到最上面"的根因）。

const RailStub = defineComponent({
  name: 'MessageNavRail',
  template: '<div data-test="rail-stub" />',
  methods: {
    refresh() {},
    jumpToLoaded() {},
    cancelJump() {},
  },
});

const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: RailStub, RemoteDirBrowser: true,
};

function setMetrics(el: HTMLElement, metrics: { scrollTop: number; scrollHeight: number }) {
  Object.defineProperty(el, 'scrollHeight', { value: metrics.scrollHeight, configurable: true });
  el.scrollTop = metrics.scrollTop;
}

function mountApp() {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'A';
  store.messages = [{ role: 'user', content: '第一条' } as any];
  store.historyHasMore = true;
  store.historyOldestId = 100;
  const wrapper = mount(App, { global: { stubs } });
  const el = wrapper.find('#chat-scroll').element as HTMLElement;
  return { store, wrapper, el };
}

/** 屏蔽后端桥：翻页由真实 store.loadOlderMessages 驱动（含 loading 置位/复位时序） */
function stubOlderPage() {
  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockResolvedValue({
        ok: true,
        messages: [
          { role: 'user', content: '更早的提问', _msg_id: 1 },
          { role: 'assistant', content: '更早的回复', _msg_id: 2 },
        ],
        has_more: false,
        oldest_id: 1,
      }),
    },
  };
}

const wait = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms));

/** 模拟真实 rail 的跳转发起序列：jumpStart（退出跟随）→ jump（跨页翻页） */
function emitJump(wrapper: ReturnType<typeof mountApp>['wrapper'], payload: { index: number; id: number }) {
  const rail = wrapper.findComponent(RailStub);
  rail.vm.$emit('jumpStart');
  rail.vm.$emit('jump', payload);
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('红点跨页跳转：翻页 prepend 期间视口不被自动吸到底部', async () => {
  const { store, wrapper, el } = mountApp();
  stubOlderPage();
  // 用户原本停在底部（stickToBottom=true）
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(150);

  // 确认跳转真的翻了页（否则测试没覆盖到目标路径）
  expect((window as any).pywebview.api.load_older_messages).toHaveBeenCalledTimes(1);
  expect((store.messages[0] as any)._msg_id).toBe(1);
  // 翻页期间视口不得被吸到底部
  expect(el.scrollTop).not.toBe(el.scrollHeight);
  wrapper.unmount();
});

it('跳转后内容变化不吸底；用户滚回底部才恢复跟随', async () => {
  const { store, wrapper, el } = mountApp();
  stubOlderPage();
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(300); // 翻页 + 补偿完成

  // 跳转后：内容变化（新回复追加）不吸底（following=false 持续，无"恢复延迟"）
  el.scrollTop = 100;
  store.messages = [...store.messages, { role: 'assistant', content: '新回复' } as any];
  await wait(80);
  expect(el.scrollTop).toBe(100);

  // 用户滚回底部（用户意图 + 落点贴底）→ 恢复跟随
  el.scrollTop = 2000; // 贴底（scrollHeight - scrollTop - clientHeight = 0）
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: 120 }));
  el.dispatchEvent(new Event('scroll'));
  await wait(80);
  store.messages = [...store.messages, { role: 'assistant', content: '再一条' } as any];
  await wait(80);
  expect(el.scrollTop).toBe(el.scrollHeight);
  wrapper.unmount();
});

it('用户上滚后流式更新不吸底（跟随冻结）', async () => {
  const { store, wrapper, el } = mountApp();
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  // 用户上滚离开底部
  el.scrollTop = 1000;
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await wait(80);

  // 流式 token 更新：不得把阅读位置拽回底部
  store.currentStreamingText = '新的流式内容';
  await wait(80);
  expect(el.scrollTop).toBe(1000);
  wrapper.unmount();
});

it('跳转后用键盘滚动：取消跳转校正（用户接管）', async () => {
  const { wrapper, el } = mountApp();
  stubOlderPage();
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });
  const rail = wrapper.findComponent(RailStub);
  const cancelSpy = vi.spyOn(rail.vm as any, 'cancelJump');

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(50);
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp' }));
  expect(cancelSpy).toHaveBeenCalled();

  wrapper.unmount();
});

it('跳转后用滚动条交互（pointerdown）：取消跳转校正（用户接管）', async () => {
  const { wrapper, el } = mountApp();
  stubOlderPage();
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });
  const rail = wrapper.findComponent(RailStub);
  const cancelSpy = vi.spyOn(rail.vm as any, 'cancelJump');

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(50);
  // 命中滚动容器自身（滚动条区域）：滚动条拖动不产生 wheel/touchmove
  el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
  expect(cancelSpy).toHaveBeenCalled();

  wrapper.unmount();
});

it('翻页 prepend 后视口按高度增量补偿（长列表不再逐页跳动）', async () => {
  const { store, wrapper, el } = mountApp();
  // 翻页过程中内容增高 500px（2000 → 2500），模拟长列表 prepend 后的布局变化
  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockImplementation(async () => {
        Object.defineProperty(el, 'scrollHeight', { value: 2500, configurable: true });
        return {
          ok: true,
          messages: [
            { role: 'user', content: '更早的提问', _msg_id: 1 },
            { role: 'assistant', content: '更早的回复', _msg_id: 2 },
          ],
          has_more: false,
          oldest_id: 1,
        };
      }),
    },
  };
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(200);

  // 视口保持等效位置：1900 + (2500 - 2000) = 2400
  expect(el.scrollTop).toBe(2400);
  expect((store.messages[0] as any)._msg_id).toBe(1);
  wrapper.unmount();
});

it('跳转后流式文本更新不吸底（不再被 token 流打断）', async () => {
  const { store, wrapper, el } = mountApp();
  stubOlderPage();
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(60);
  // 流式 token 更新：内容变化 watch 会请求吸底，但 following=false 时不得滚动
  store.currentStreamingText = '新的流式内容';
  await wait(80);

  expect(el.scrollTop).not.toBe(el.scrollHeight);
  wrapper.unmount();
});

it('程序化滚动到顶部不触发翻页（自动翻页只由用户滚动触发）', async () => {
  const { wrapper, el } = mountApp();
  // has_more: true：若守卫失效，滚到顶部会再次触发 maybeLoadOlder 的翻页
  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockResolvedValue({
        ok: true,
        messages: [
          { role: 'user', content: '更早的提问', _msg_id: 1 },
          { role: 'assistant', content: '更早的回复', _msg_id: 2 },
        ],
        has_more: true,
        oldest_id: 1,
      }),
    },
  };
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  emitJump(wrapper, { index: 0, id: 1 });
  await wait(120);
  const n = (window as any).pywebview.api.load_older_messages.mock.calls.length;
  expect(n).toBe(1); // 跳转本身翻了一页，确认覆盖到目标路径

  // 无用户意图的程序化滚动到顶部（跳转定位/补偿路过）：不得再抢翻页
  el.scrollTop = 50;
  el.dispatchEvent(new Event('scroll'));
  await wait(100);

  expect((window as any).pywebview.api.load_older_messages).toHaveBeenCalledTimes(n);
  wrapper.unmount();
});

it('用户滚动到顶部正常触发翻页（意图守卫不误伤）', async () => {
  const { wrapper, el } = mountApp();
  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockResolvedValue({
        ok: true,
        messages: [
          { role: 'user', content: '更早的提问', _msg_id: 1 },
          { role: 'assistant', content: '更早的回复', _msg_id: 2 },
        ],
        has_more: true,
        oldest_id: 1,
      }),
    },
  };
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  // 用户滚轮上滚到顶部：意图新鲜 + 落点在顶部 → 触发翻页
  el.scrollTop = 50;
  el.dispatchEvent(new WheelEvent('wheel', { deltaY: -120 }));
  el.dispatchEvent(new Event('scroll'));
  await wait(120);

  expect((window as any).pywebview.api.load_older_messages).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

// ---------------------------------------------------------------------------
// 回归（2026-10-11 用户反馈「点右侧最上面一个点，怎么都无法加载到最上面」）：
// 会话分页状态被失败响应清空（has_more=false + 游标 null）时，跨页跳转不得
// 直接放弃 —— 必须真发一次翻页请求，把更早历史加载进来。
// ---------------------------------------------------------------------------

it('分页状态被清空（has_more=false、游标 null）时，跨页跳转仍会加载更早历史', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.currentSessionName = 'A';
  // 历史窗口只剩尾部：已加载首条是红点索引 2，前两个红点（含最上面那个）未加载
  store.messages = [
    { role: 'user', content: '第三条', _msg_id: 30 },
    { role: 'assistant', content: '回复三', _msg_id: 31 },
  ] as any;
  store.navItems = [
    { id: 10, preview: '第一条' },
    { id: 20, preview: '第二条' },
    { id: 30, preview: '第三条' },
  ];
  store.historyHasMore = false;   // 被失败响应清成「没有更早历史」
  store.historyOldestId = null;   // 游标同时丢失

  (window as any).pywebview = {
    api: {
      load_older_messages: vi.fn().mockResolvedValue({
        ok: true,
        messages: [
          { role: 'user', content: '第一条', _msg_id: 10 },
          { role: 'assistant', content: '回复一', _msg_id: 11 },
          { role: 'user', content: '第二条', _msg_id: 20 },
          { role: 'assistant', content: '回复二', _msg_id: 21 },
        ],
        has_more: false,
        oldest_id: 10,
      }),
    },
  };

  const wrapper = mount(App, { global: { stubs } });
  const el = wrapper.find('#chat-scroll').element as HTMLElement;
  setMetrics(el, { scrollTop: 0, scrollHeight: 600 });

  emitJump(wrapper, { index: 0, id: 10 });
  await wait(200);

  // 关键回归：没被 historyHasMore=false 挡住，真发了一次翻页（游标由已加载消息自愈而来）
  expect((window as any).pywebview.api.load_older_messages).toHaveBeenCalledWith('A', 30);
  // 更早历史已进入消息流，最上面一个红点对应的消息已被加载
  expect(store.messages.map(m => m.content)).toEqual([
    '第一条', '回复一', '第二条', '回复二', '第三条', '回复三',
  ]);
  expect(store.firstLoadedMsgId).toBe(10);
  expect(store.historyHasMore).toBe(false);
  expect(store.historyOldestId).toBe(10);
  wrapper.unmount();
});
