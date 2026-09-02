import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 子组件全部 stub：手柄渲染与拖拽接线都在 App 模板内，不依赖子组件行为
const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
};

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

// jsdom 无 matchMedia → useIsMobile 恒为 false，即桌面端行为
it('桌面端渲染左右两个面板拖拽手柄', () => {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });

  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(true);
  wrapper.unmount();
});

it('面板折叠时对应手柄隐藏，展开后恢复', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.sidebarCollapsed = true;
  store.rightPanelCollapsed = true;
  const wrapper = mount(App, { global: { stubs } });

  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(false);

  store.sidebarCollapsed = false;
  store.rightPanelCollapsed = false;
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(true);
  wrapper.unmount();
});
