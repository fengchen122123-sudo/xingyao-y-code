import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';
import type { GuiTheme, GuiThemeError, ThemeRegionId } from '../themes/types';


const regionIds: ThemeRegionId[] = [
  'app_header', 'sidebar', 'conversation_toolbar', 'workspace',
  'composer', 'right_panel', 'statusbar',
];

function makeTheme(id: string, appearance: 'light' | 'dark' = 'dark'): GuiTheme {
  const region = {
    background: appearance === 'light' ? '#faf6f8' : '#1a1a18',
    text: appearance === 'light' ? '#2c2529' : '#f4f4f1',
    border: appearance === 'light' ? '#e5ccd8' : '#363632',
    accent: '#d96b9f',
    background_opacity: 1,
    image: null,
    image_opacity: 0.2,
    image_size: 'cover' as const,
    image_position: 'center' as const,
  };
  return {
    schema_version: 1,
    id,
    name: id,
    appearance,
    extends: appearance === 'light' ? 'builtin.light' : 'builtin.dark',
    builtin: id.startsWith('builtin.'),
    tokens: {
      background_app: region.background,
      text_primary: region.text,
      accent: region.accent,
      radius_medium: 8,
    },
    native_titlebar: {
      background: region.background,
      text: region.text,
      border: region.border,
      mode: appearance,
    },
    regions: Object.fromEntries(
      regionIds.map(regionId => [regionId, { ...region }]),
    ) as GuiTheme['regions'],
  };
}

const activeThemeError: GuiThemeError = {
  code: 'theme_not_found',
  path: 'gui_theme_id',
  message: '活动主题不存在',
};

const saveTheme = vi.fn();
const setActive = vi.fn();
const previewTitlebar = vi.fn();
const restoreTitlebar = vi.fn();
const getInitialState = vi.fn();

beforeEach(() => {
  setActivePinia(createPinia());
  document.documentElement.removeAttribute('style');
  document.body.className = '';
  localStorage.clear();
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn()
      .mockReturnValueOnce('blob:first')
      .mockReturnValueOnce('blob:second')
      .mockReturnValue('blob:later'),
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  });
  saveTheme.mockReset().mockResolvedValue({ ok: true });
  setActive.mockReset().mockImplementation(async (id: string) => ({
    ok: true,
    theme: makeTheme(id, id === 'builtin.light' ? 'light' : 'dark'),
    native_titlebar_color_capability: 'supported',
  }));
  previewTitlebar.mockReset().mockResolvedValue({
    ok: true,
    native_titlebar_color_capability: 'supported',
  });
  restoreTitlebar.mockReset().mockResolvedValue({
    ok: true,
    native_titlebar_color_capability: 'supported',
  });
  getInitialState.mockReset().mockResolvedValue({
    gui_theme: null,
    gui_themes: [],
    gui_theme_error: null,
    gui_theme_list_error: null,
    native_titlebar_color_capability: 'pending',
  });
  (window as any).pywebview = {
    api: {
      save_gui_theme: saveTheme,
      set_active_gui_theme: setActive,
      preview_native_titlebar: previewTitlebar,
      restore_native_titlebar: restoreTitlebar,
      get_gui_theme_asset: vi.fn(),
      get_initial_state: getInitialState,
      check_update: vi.fn().mockResolvedValue({ ok: true, has_update: false }),
    },
  };
});

describe('GUI theme store', () => {
  it('previews a draft without persisting it and cancel restores the saved snapshot', async () => {
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('saved');
    store.guiTheme = makeTheme('saved');

    await store.previewGuiTheme(makeTheme('draft'));

    expect(setActive).not.toHaveBeenCalled();
    expect(previewTitlebar).toHaveBeenCalledWith(makeTheme('draft').native_titlebar);
    expect(store.guiTheme?.id).toBe('draft');
    expect(store.themeDirty).toBe(true);

    await store.cancelGuiThemePreview();

    expect(restoreTitlebar).toHaveBeenCalledOnce();
    expect(store.guiTheme?.id).toBe('saved');
    expect(store.guiThemeDraft?.id).toBe('saved');
    expect(store.themeDirty).toBe(false);
  });

  it('commits only after backend save and activation both succeed', async () => {
    const store = useAppStore();
    store.guiThemeDraft = makeTheme('rose');

    await store.saveGuiThemeDraft();

    expect(saveTheme).toHaveBeenCalledWith(makeTheme('rose'));
    expect(saveTheme.mock.invocationCallOrder[0]).toBeLessThan(setActive.mock.invocationCallOrder[0]);
    expect(setActive).toHaveBeenCalledWith('rose');
    expect(store.savedGuiTheme?.id).toBe('rose');
    expect(store.guiTheme?.id).toBe('rose');
    expect(store.themeDirty).toBe(false);
  });

  it('does not commit the saved snapshot when activation fails after save', async () => {
    setActive.mockResolvedValueOnce({
      ok: false,
      error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '标题栏失败' },
    });
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('saved');
    store.guiTheme = makeTheme('saved');
    store.guiThemeDraft = makeTheme('rose');

    const result = await store.saveGuiThemeDraft();

    expect(result.ok).toBe(false);
    expect(store.savedGuiTheme?.id).toBe('saved');
    expect(store.guiTheme?.id).toBe('saved');
    expect(store.themeDirty).toBe(true);
  });

  it('keeps the last successful theme when preview runtime or titlebar application fails', async () => {
    const store = useAppStore();
    const saved = makeTheme('saved');
    store.savedGuiTheme = saved;
    store.guiTheme = saved;
    const invalidAsset = makeTheme('broken');
    invalidAsset.regions.workspace.image = 'assets/missing.png';
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: false,
      error: { code: 'asset_not_found', path: 'asset_path', message: '图片不存在' },
    });

    const runtimeFailure = await store.previewGuiTheme(invalidAsset);

    expect(runtimeFailure.ok).toBe(false);
    expect(store.guiTheme?.id).toBe('saved');
    expect(previewTitlebar).not.toHaveBeenCalled();

    previewTitlebar.mockResolvedValueOnce({
      ok: false,
      error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '标题栏失败' },
    });
    const titlebarFailure = await store.previewGuiTheme(makeTheme('draft'));

    expect(titlebarFailure.ok).toBe(false);
    expect(store.guiTheme?.id).toBe('saved');
    expect(store.savedGuiTheme?.id).toBe('saved');
  });

  it('keeps the previous applied handle alive when a later preview fails', async () => {
    const store = useAppStore();
    const saved = makeTheme('saved');
    saved.regions.workspace.image = 'assets/saved.png';
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: true,
      mime: 'image/png',
      base64: 'AA==',
    });
    store.savedGuiTheme = saved;
    store.guiTheme = saved;
    await store.previewGuiTheme(saved);
    previewTitlebar.mockResolvedValueOnce({
      ok: false,
      error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '标题栏失败' },
    });

    const result = await store.previewGuiTheme(makeTheme('draft'));

    expect(result.ok).toBe(false);
    expect(store.guiTheme?.id).toBe('saved');
    expect(URL.revokeObjectURL).not.toHaveBeenCalledWith('blob:first');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-image'))
      .toBe('url("blob:first")');
  });

  it('disposes the previous applied handle only after the replacement succeeds', async () => {
    const store = useAppStore();
    const saved = makeTheme('saved');
    const draft = makeTheme('draft');
    saved.regions.workspace.image = 'assets/saved.png';
    draft.regions.workspace.image = 'assets/draft.png';
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: true,
      mime: 'image/png',
      base64: 'AA==',
    });

    await store.previewGuiTheme(saved);
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    await store.previewGuiTheme(draft);

    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-image'))
      .toBe('url("blob:second")');
  });

  it('keeps the preview and dirty draft when restoring the native titlebar fails', async () => {
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('saved');
    store.guiTheme = makeTheme('saved');
    await store.previewGuiTheme(makeTheme('draft'));
    restoreTitlebar.mockResolvedValueOnce({
      ok: false,
      error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '标题栏失败' },
    });

    const result = await store.cancelGuiThemePreview();

    expect(result.ok).toBe(false);
    expect(store.guiTheme?.id).toBe('draft');
    expect(store.guiThemeDraft?.id).toBe('draft');
    expect(store.themeDirty).toBe(true);
  });

  it('hydrates an active theme and keeps list errors non-blocking', async () => {
    const listError = { code: 'theme_io_failed', path: 'gui_themes', message: '列表读取失败' };
    getInitialState.mockResolvedValueOnce({
      gui_theme: makeTheme('saved'),
      gui_themes: [{ id: 'saved', name: 'saved', appearance: 'dark', extends: 'builtin.dark', builtin: false }],
      gui_theme_error: null,
      gui_theme_list_error: listError,
      native_titlebar_color_capability: 'unsupported',
      models: [], sessions: [], skills: [], mcps: [], token_usage: {}, version: '',
    });
    const store = useAppStore();

    const initialized = store.initFromBackend();
    await initialized;

    expect(store.guiTheme?.id).toBe('saved');
    expect(store.savedGuiTheme?.id).toBe('saved');
    expect(store.guiThemeError).toBeNull();
    expect(store.guiThemeListError).toEqual(listError);
    expect(store.nativeTitlebarColorCapability).toBe('unsupported');
  });

  it('keeps the blocking error until the user resets explicitly', async () => {
    getInitialState.mockResolvedValueOnce({
      gui_theme: null,
      gui_themes: [],
      gui_theme_error: activeThemeError,
      gui_theme_list_error: null,
      native_titlebar_color_capability: 'pending',
      models: [], sessions: [], skills: [], mcps: [], token_usage: {}, version: '',
    });
    const store = useAppStore();

    const initialized = store.initFromBackend();
    await initialized;

    expect(store.guiThemeError?.code).toBe('theme_not_found');
    expect(setActive).not.toHaveBeenCalled();

    await store.resetInvalidGuiTheme('builtin.dark');

    expect(setActive).toHaveBeenCalledWith('builtin.dark');
    expect(store.guiThemeError).toBeNull();
    expect(store.savedGuiTheme?.id).toBe('builtin.dark');
  });
});
