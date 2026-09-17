import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { defineComponent } from 'vue';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 红点跨页跳转（点未加载区红点 → 连续向前翻页）期间的自动吸底抑制。
// 真实故障：跳转要往前翻数页，每页 prepend 都会触发「内容变化 → 吸底」，
// 把视口反复拽回底部，并与 rail 的最终定位相互打断——表现为
// 「点第一个红点定不到、视口闪动乱跳」。

const RailStub = defineComponent({
  name: 'MessageNavRail',
  template: '<div data-test="rail-stub" />',
  methods: { refresh() {}, jumpToLoaded() {} },
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

  wrapper.findComponent(RailStub).vm.$emit('jump', { index: 0, id: 1 });
  await wait(150);

  // 确认跳转真的翻了页（否则测试没覆盖到目标路径）
  expect((window as any).pywebview.api.load_older_messages).toHaveBeenCalledTimes(1);
  expect((store.messages[0] as any)._msg_id).toBe(1);
  // 翻页期间视口不得被吸到底部
  expect(el.scrollTop).not.toBe(el.scrollHeight);
  wrapper.unmount();
});

it('跳转结束后自动吸底恢复（抑制窗口是临时的）', async () => {
  const { store, wrapper, el } = mountApp();
  stubOlderPage();
  setMetrics(el, { scrollTop: 1900, scrollHeight: 2000 });

  wrapper.findComponent(RailStub).vm.$emit('jump', { index: 0, id: 1 });
  await wait(700); // 跨过抑制窗口

  // 非跳转场景的内容变化（新消息/工具时间线追加）仍应自动吸底
  el.scrollTop = 100;
  store.messages = [...store.messages, { role: 'assistant', content: '新回复' } as any];
  await wait(80);

  expect(el.scrollTop).toBe(el.scrollHeight);
  wrapper.unmount();
});
