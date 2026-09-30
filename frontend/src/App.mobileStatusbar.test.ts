import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('手机状态栏只保留连接、模型、工作区、沙盒、运行状态并维持顺序', () => {
  const store = useAppStore();
  store.initialized = true;
  store.guiThemeError = null;

  const wrapper = mount(App, {
    global: {
      stubs: {
        Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
        InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
        PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
        AppIcon: true, MessageNavRail: true,
      },
    },
  });

  const primaryIds = wrapper
    .findAll('[data-mobile-status="primary"]')
    .map(item => item.attributes('data-test'));
  expect(primaryIds).toEqual([
    'status-connection',
    'status-model',
    'status-workspace',
    'status-sandbox',
    'status-running',
  ]);

  const hiddenIds = wrapper
    .findAll('[data-mobile-status="hidden"]')
    .map(item => item.attributes('data-test'));
  expect(hiddenIds).toEqual([
    'status-token',
    'status-permission',
    'status-skill',
    'status-ai-notice',
    'status-charset',
  ]);

  wrapper.unmount();
});
