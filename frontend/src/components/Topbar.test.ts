import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Topbar from './Topbar.vue';
import { useAppStore } from '../stores/appStore';
import type { GuiTheme, ThemeRegionId } from '../themes/types';


const regionIds: ThemeRegionId[] = [
  'app_header', 'sidebar', 'conversation_toolbar', 'workspace',
  'composer', 'right_panel', 'statusbar',
];

function builtinTheme(id: 'builtin.light' | 'builtin.dark'): GuiTheme {
  const light = id === 'builtin.light';
  const region = {
    background: light ? '#f4f4f1' : '#1a1a18',
    text: light ? '#20201e' : '#f4f4f1',
    border: light ? '#d8d8d2' : '#363632',
    accent: '#6c63ff', background_opacity: 1, image: null, image_opacity: 0,
    image_size: 'cover' as const, image_position: 'center' as const,
  };
  return {
    schema_version: 1, id, name: id, appearance: light ? 'light' : 'dark',
    extends: id, builtin: true,
    tokens: { background_app: region.background, text_primary: region.text, accent: region.accent },
    native_titlebar: {
      background: region.background, text: region.text, border: region.border,
      mode: light ? 'light' : 'dark',
    },
    app_image: { image: null, image_opacity: 1, image_size: 'cover' as const, image_position: 'center' as const },
    regions: Object.fromEntries(regionIds.map(regionId => [regionId, { ...region }])) as GuiTheme['regions'],
  };
}

const setActive = vi.fn();

beforeEach(() => {
  setActivePinia(createPinia());
  document.body.className = '';
  setActive.mockReset().mockImplementation(async (id: 'builtin.light' | 'builtin.dark') => ({
    ok: true,
    theme: builtinTheme(id),
    native_titlebar_color_capability: 'supported',
  }));
  (window as any).pywebview = {
    api: {
      set_active_gui_theme: setActive,
      get_gui_theme_asset: vi.fn(),
    },
  };
});

describe('Topbar GUI theme shortcut', () => {
  it('activates the opposite built-in theme through the store', async () => {
    const store = useAppStore();
    store.guiTheme = builtinTheme('builtin.dark');
    store.savedGuiTheme = builtinTheme('builtin.dark');
    store.themeLight = false;
    const wrapper = mount(Topbar, { global: { stubs: { AppIcon: true } } });

    await wrapper.get('#btn-theme').trigger('click');
    await vi.waitFor(() => expect(setActive).toHaveBeenCalledWith('builtin.light'));
    await vi.waitFor(() => expect(store.savedGuiTheme?.id).toBe('builtin.light'));

    expect(store.themeLight).toBe(true);
    expect(localStorage.getItem('ycode-theme-light')).toBeNull();
  });

  it('keeps the current shortcut appearance when activation fails', async () => {
    setActive.mockResolvedValueOnce({
      ok: false,
      error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '标题栏失败' },
    });
    const store = useAppStore();
    store.guiTheme = builtinTheme('builtin.dark');
    store.savedGuiTheme = builtinTheme('builtin.dark');
    store.themeLight = false;
    const wrapper = mount(Topbar, { global: { stubs: { AppIcon: true } } });

    await wrapper.get('#btn-theme').trigger('click');
    await vi.waitFor(() => expect(setActive).toHaveBeenCalledOnce());

    expect(store.themeLight).toBe(false);
    expect(store.savedGuiTheme?.id).toBe('builtin.dark');
  });
});
