import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import App from './App.vue';
import { useAppStore } from './stores/appStore';


describe('GUI theme startup error gate', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    delete (window as any).pywebview;
  });

  it('blocks the workspace until the user explicitly selects a built-in theme', async () => {
    const store = useAppStore();
    store.initialized = true;
    store.guiThemeError = {
      code: 'theme_not_found',
      path: 'gui_theme_id',
      message: '活动主题不存在',
    };
    const reset = vi.spyOn(store, 'resetInvalidGuiTheme').mockResolvedValue({
      ok: false,
      error: { code: 'test', path: 'theme', message: 'test' },
    });
    const wrapper = mount(App, {
      global: {
        stubs: {
          Topbar: { template: '<div data-test="topbar-stub" />' },
          Sidebar: true,
          KimiEmptyState: true,
          TurnGroup: true,
          InputArea: true,
          RightPanel: true,
          ConfirmModal: true,
          UpdateModal: true,
          PermCard: true,
          ImageLightbox: true,
          Subpage: true,
          SettingsPage: true,
          AppIcon: true,
        },
      },
    });

    expect(wrapper.get('[data-test="gui-theme-error"]').text()).toContain('活动主题不存在');
    expect(wrapper.find('[data-test="topbar-stub"]').exists()).toBe(false);
    expect(wrapper.find('#main').exists()).toBe(false);
    expect(wrapper.find('#statusbar').exists()).toBe(false);
    expect(wrapper.findAll('[data-test="gui-theme-reset"]')).toHaveLength(2);

    await wrapper.findAll('[data-test="gui-theme-reset"]')[0].trigger('click');
    await wrapper.findAll('[data-test="gui-theme-reset"]')[1].trigger('click');
    expect(reset).toHaveBeenNthCalledWith(1, 'builtin.dark');
    expect(reset).toHaveBeenNthCalledWith(2, 'builtin.light');

    wrapper.unmount();
  });

  it('shows the theme preview bar over the real home and returns to the editor', async () => {
    const store = useAppStore();
    store.initialized = true;
    store.guiThemeError = null;
    store.messages = [{ role: 'user', content: 'hello' }];
    store.themePreviewMode = true;
    store.settingsOpen = false;
    const wrapper = mount(App, {
      global: {
        stubs: {
          Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
          InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
          PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true, AppIcon: true,
        },
      },
    });

    // 预览模式 + 设置页已收起：浮条覆盖在真实首页上
    const bar = wrapper.get('[data-test="theme-preview-bar"]');
    expect(bar.text()).toContain('正在预览主题效果');
    expect(wrapper.find('#main').exists()).toBe(true);

    await wrapper.get('[data-test="preview-back"]').trigger('click');
    expect(store.themePreviewMode).toBe(false);
    expect(store.settingsOpen).toBe(true);

    wrapper.unmount();
  });
});
