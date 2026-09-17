import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 子组件全部 stub：只断言对话标题栏自身的按钮，不依赖子组件行为
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

it('对话标题栏不再渲染「清空」按钮，导出入口保留', () => {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });

  const labels = wrapper.findAll('#chat-header .chip-btn').map((b) => b.text());

  expect(labels).not.toContain('清空');
  expect(labels.some((t) => t.includes('导出'))).toBe(true);
  wrapper.unmount();
});
