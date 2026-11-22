import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: { template: '<div />', methods: { refresh() {}, jumpToLoaded() {} } }, RemoteDirBrowser: true,
};

function mountApp() {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });
  return { store, wrapper };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('messages 为空且 sessionSwitching=true：渲染「正在加载会话…」指示，不渲染空状态卡片', async () => {
  const { store, wrapper } = mountApp();

  // 场景：已切到目标会话，但历史还在后端返回路上（消息区暂空）——不能误显示"新对话"欢迎卡片
  store.messages = [];
  store.sessionSwitching = true;
  await wrapper.vm.$nextTick();

  const indicator = wrapper.find('[data-test="session-switching"]');
  expect(indicator.exists()).toBe(true);
  expect(indicator.text()).toContain('正在加载会话');
  // 空状态卡片必须缺席，否则用户会以为切换失败、回到了首屏
  expect(wrapper.findComponent({ name: 'KimiEmptyState' }).exists()).toBe(false);
  wrapper.unmount();
});

it('messages 为空且 sessionSwitching=false：渲染空状态卡片，不渲染切换加载指示', async () => {
  const { store, wrapper } = mountApp();

  store.messages = [];
  store.sessionSwitching = false;
  await wrapper.vm.$nextTick();

  expect(wrapper.findComponent({ name: 'KimiEmptyState' }).exists()).toBe(true);
  expect(wrapper.find('[data-test="session-switching"]').exists()).toBe(false);
  wrapper.unmount();
});

it('messages 非空：空状态卡片与切换加载指示都不渲染（走消息流分支）', async () => {
  const { store, wrapper } = mountApp();

  store.messages = [{ role: 'user', content: 'hi' } as any];
  store.sessionSwitching = false;
  await wrapper.vm.$nextTick();

  expect(wrapper.findComponent({ name: 'KimiEmptyState' }).exists()).toBe(false);
  expect(wrapper.find('[data-test="session-switching"]').exists()).toBe(false);
  wrapper.unmount();
});
