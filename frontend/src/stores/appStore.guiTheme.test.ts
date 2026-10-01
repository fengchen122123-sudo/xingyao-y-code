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
    app_image: { image: null, image_opacity: 1, image_size: 'cover' as const, image_position: 'center' as const },
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
const getActiveTheme = vi.fn();
const getInitialState = vi.fn();
const listThemes = vi.fn();
const getTheme = vi.fn();
const copyTheme = vi.fn();
const renameTheme = vi.fn();
const deleteTheme = vi.fn();
const importTheme = vi.fn();
const exportTheme = vi.fn();
const importThemeImage = vi.fn();
const removeThemeImage = vi.fn();

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

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
    theme: makeTheme('saved'),
    native_titlebar_color_capability: 'supported',
  });
  getActiveTheme.mockReset().mockResolvedValue({
    ok: true,
    theme: makeTheme('saved'),
    native_titlebar_color_capability: 'supported',
  });
  getInitialState.mockReset().mockResolvedValue({
    gui_theme: null,
    gui_themes: [],
    gui_theme_error: null,
    gui_theme_list_error: null,
    native_titlebar_color_capability: 'pending',
  });
  listThemes.mockReset().mockResolvedValue({
    ok: true,
    themes: [
      { id: 'builtin.dark', name: '内置深色', appearance: 'dark', extends: 'builtin.dark', builtin: true },
      { id: 'builtin.light', name: '内置浅色', appearance: 'light', extends: 'builtin.light', builtin: true },
    ],
  });
  getTheme.mockReset().mockImplementation(async (id: string) => ({
    ok: true,
    theme: makeTheme(id, id.includes('light') ? 'light' : 'dark'),
  }));
  copyTheme.mockReset().mockResolvedValue({ ok: true, theme: makeTheme('copied') });
  renameTheme.mockReset().mockResolvedValue({ ok: true, theme: makeTheme('renamed') });
  deleteTheme.mockReset().mockResolvedValue({ ok: true });
  importTheme.mockReset().mockResolvedValue({ ok: true, theme: makeTheme('imported') });
  exportTheme.mockReset().mockResolvedValue({ ok: true, path: 'C:/tmp/rose.zip' });
  importThemeImage.mockReset().mockResolvedValue({ ok: true, asset_path: 'assets/workspace.webp' });
  removeThemeImage.mockReset().mockResolvedValue({ ok: true, theme: makeTheme('rose') });
  (window as any).pywebview = {
    api: {
      save_gui_theme: saveTheme,
      set_active_gui_theme: setActive,
      preview_native_titlebar: previewTitlebar,
      restore_native_titlebar: restoreTitlebar,
      get_active_gui_theme: getActiveTheme,
      get_gui_theme_asset: vi.fn(),
      get_initial_state: getInitialState,
      list_gui_themes: listThemes,
      get_gui_theme: getTheme,
      copy_gui_theme: copyTheme,
      rename_gui_theme: renameTheme,
      delete_gui_theme: deleteTheme,
      import_gui_theme: importTheme,
      export_gui_theme: exportTheme,
      import_gui_theme_image: importThemeImage,
      remove_gui_theme_image: removeThemeImage,
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

  it('blocks the workspace and resyncs authority when restoring the native titlebar fails', async () => {
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
    expect(store.guiThemeDraft?.id).toBe('saved');
    expect(store.savedGuiTheme?.id).toBe('saved');
    expect(store.themeDirty).toBe(false);
    expect(store.guiThemeError?.code).toBe('native_titlebar_failed');
    expect(getActiveTheme).toHaveBeenCalledOnce();
  });

  it('serializes a slow preview before a newer preview and releases the replaced handle', async () => {
    const firstTitlebar = deferred<any>();
    previewTitlebar
      .mockImplementationOnce(() => firstTitlebar.promise)
      .mockResolvedValueOnce({ ok: true, native_titlebar_color_capability: 'supported' });
    const store = useAppStore();
    const first = makeTheme('first');
    const second = makeTheme('second');
    first.regions.workspace.image = 'assets/first.png';
    second.regions.workspace.image = 'assets/second.png';
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: true, mime: 'image/png', base64: 'AA==',
    });

    const firstOperation = store.previewGuiTheme(first);
    await vi.waitFor(() => expect(previewTitlebar).toHaveBeenCalledTimes(1));
    const secondOperation = store.previewGuiTheme(second);
    await Promise.resolve();
    expect(previewTitlebar).toHaveBeenCalledTimes(1);

    firstTitlebar.resolve({ ok: true, native_titlebar_color_capability: 'supported' });
    await Promise.all([firstOperation, secondOperation]);

    expect(previewTitlebar.mock.calls.map(call => call[0].background))
      .toEqual([first.native_titlebar.background, second.native_titlebar.background]);
    expect(store.guiTheme?.id).toBe('second');
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-image'))
      .toBe('url("blob:second")');
  });

  it('serializes cancel after a slow preview and restores the backend authoritative theme', async () => {
    const firstTitlebar = deferred<any>();
    previewTitlebar.mockImplementationOnce(() => firstTitlebar.promise);
    const authoritative = makeTheme('authoritative', 'light');
    restoreTitlebar.mockResolvedValueOnce({
      ok: true,
      theme: authoritative,
      native_titlebar_color_capability: 'supported',
    });
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('stale-local');
    store.guiTheme = makeTheme('stale-local');

    const preview = store.previewGuiTheme(makeTheme('draft'));
    await vi.waitFor(() => expect(previewTitlebar).toHaveBeenCalledOnce());
    const cancel = store.cancelGuiThemePreview();
    await Promise.resolve();
    expect(restoreTitlebar).not.toHaveBeenCalled();

    firstTitlebar.resolve({ ok: true, native_titlebar_color_capability: 'supported' });
    await Promise.all([preview, cancel]);

    expect(store.guiTheme?.id).toBe('authoritative');
    expect(store.savedGuiTheme?.id).toBe('authoritative');
    expect(store.guiThemeDraft?.id).toBe('authoritative');
    expect(store.themeDirty).toBe(false);
    expect(document.body.classList.contains('light')).toBe(true);
  });

  it('serializes activation after preview and leaves backend and store on the activated theme', async () => {
    const firstTitlebar = deferred<any>();
    previewTitlebar.mockImplementationOnce(() => firstTitlebar.promise);
    const store = useAppStore();
    const preview = store.previewGuiTheme(makeTheme('draft'));
    await vi.waitFor(() => expect(previewTitlebar).toHaveBeenCalledOnce());
    const activation = store.activateGuiTheme('builtin.light');
    await Promise.resolve();
    expect(setActive).not.toHaveBeenCalled();

    firstTitlebar.resolve({ ok: true, native_titlebar_color_capability: 'supported' });
    await Promise.all([preview, activation]);

    expect(setActive).toHaveBeenCalledWith('builtin.light');
    expect(store.guiTheme?.id).toBe('builtin.light');
    expect(store.savedGuiTheme?.id).toBe('builtin.light');
    expect(store.themeDirty).toBe(false);
  });

  it('serializes save after preview and saves the draft produced by that preview', async () => {
    const firstTitlebar = deferred<any>();
    previewTitlebar.mockImplementationOnce(() => firstTitlebar.promise);
    const store = useAppStore();
    store.guiThemeDraft = makeTheme('old-draft');
    const latest = makeTheme('latest-draft');

    const preview = store.previewGuiTheme(latest);
    await vi.waitFor(() => expect(previewTitlebar).toHaveBeenCalledOnce());
    const save = store.saveGuiThemeDraft();
    await Promise.resolve();
    expect(saveTheme).not.toHaveBeenCalled();

    firstTitlebar.resolve({ ok: true, native_titlebar_color_capability: 'supported' });
    await Promise.all([preview, save]);

    expect(saveTheme).toHaveBeenCalledWith(latest);
    expect(setActive).toHaveBeenCalledWith('latest-draft');
    expect(store.savedGuiTheme?.id).toBe('latest-draft');
  });

  it('enters a blocking error and resyncs authority when backend activation rollback fails', async () => {
    const broken = makeTheme('broken');
    broken.regions.workspace.image = 'assets/missing.png';
    const authoritative = makeTheme('authoritative', 'light');
    setActive
      .mockResolvedValueOnce({
        ok: true,
        theme: broken,
        native_titlebar_color_capability: 'supported',
      })
      .mockResolvedValueOnce({
        ok: false,
        error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '回滚失败' },
      });
    getActiveTheme.mockResolvedValueOnce({
      ok: true,
      theme: authoritative,
      native_titlebar_color_capability: 'supported',
    });
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: false,
      error: { code: 'asset_not_found', path: 'asset_path', message: '图片不存在' },
    });
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('saved');
    store.guiTheme = makeTheme('saved');

    const result = await store.activateGuiTheme('broken');

    expect(result).toMatchObject({ ok: false, error: { code: 'native_titlebar_failed' } });
    expect(store.guiThemeError?.code).toBe('native_titlebar_failed');
    expect(getActiveTheme).toHaveBeenCalledOnce();
    expect(store.savedGuiTheme?.id).toBe('authoritative');
    expect(store.guiThemeDraft?.id).toBe('authoritative');
    expect(store.guiTheme?.id).toBe('saved');
  });

  it('reports rollback exceptions explicitly and resyncs the backend authority', async () => {
    const broken = makeTheme('broken');
    broken.regions.workspace.image = 'assets/missing.png';
    setActive
      .mockResolvedValueOnce({
        ok: true,
        theme: broken,
        native_titlebar_color_capability: 'supported',
      })
      .mockRejectedValueOnce(new Error('rollback transport failed'));
    getActiveTheme.mockResolvedValueOnce({
      ok: true,
      theme: makeTheme('still-active'),
      native_titlebar_color_capability: 'supported',
    });
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: false,
      error: { code: 'asset_not_found', path: 'asset_path', message: '图片不存在' },
    });
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('saved');
    store.guiTheme = makeTheme('saved');

    const result = await store.activateGuiTheme('broken');

    expect(result).toMatchObject({ ok: false, error: { code: 'theme_rollback_failed' } });
    expect(store.guiThemeError?.code).toBe('theme_rollback_failed');
    expect(store.savedGuiTheme?.id).toBe('still-active');
    expect(getActiveTheme).toHaveBeenCalledOnce();
  });

  it('blocks and resyncs when restoring the previous preview titlebar fails', async () => {
    previewTitlebar
      .mockResolvedValueOnce({
        ok: false,
        error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '预览失败' },
      })
      .mockResolvedValueOnce({
        ok: false,
        error: { code: 'native_titlebar_failed', path: 'native_titlebar', message: '回滚失败' },
      });
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('saved');
    store.guiTheme = makeTheme('saved');

    const result = await store.previewGuiTheme(makeTheme('draft'));

    expect(result).toMatchObject({ ok: false, error: { message: '回滚失败' } });
    expect(store.guiThemeError?.message).toBe('回滚失败');
    expect(getActiveTheme).toHaveBeenCalledOnce();
    expect(store.guiTheme?.id).toBe('saved');
  });

  it('reports authority resync failure instead of retaining a stale recovery error', async () => {
    restoreTitlebar.mockRejectedValueOnce(new Error('native restore transport failed'));
    getActiveTheme.mockResolvedValueOnce({
      ok: false,
      error: { code: 'theme_io_failed', path: 'gui_theme_id', message: '读取活动主题失败' },
    });
    const store = useAppStore();

    const result = await store.cancelGuiThemePreview();

    expect(result).toMatchObject({ ok: false, error: { code: 'theme_recovery_failed' } });
    expect(store.guiThemeError?.code).toBe('theme_recovery_failed');
  });

  it('preserves the runtime asset error envelope in preview results', async () => {
    const broken = makeTheme('broken');
    broken.regions.workspace.image = 'assets/escape.png';
    (window as any).pywebview.api.get_gui_theme_asset.mockResolvedValue({
      ok: false,
      error: { code: 'asset_escape', path: 'asset_path', message: '资源路径越界' },
    });
    const store = useAppStore();

    const result = await store.previewGuiTheme(broken);

    expect(result).toEqual({
      ok: false,
      error: { code: 'asset_escape', path: 'asset_path', message: '资源路径越界' },
    });
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

describe('GUI theme library management', () => {
  it('refreshes the theme list and only reports list errors as non-blocking', async () => {
    listThemes.mockResolvedValueOnce({
      ok: false,
      error: { code: 'theme_operation_failed', path: 'gui_themes', message: '列表读取失败' },
    });
    const store = useAppStore();

    const failed = await store.refreshGuiThemes();

    expect(failed.ok).toBe(false);
    expect(store.guiThemeListError?.code).toBe('theme_operation_failed');
    expect(store.guiThemeError).toBeNull();

    const ok = await store.refreshGuiThemes();

    expect(ok.ok).toBe(true);
    expect(store.guiThemes.map(t => t.id)).toEqual(['builtin.dark', 'builtin.light']);
    expect(store.guiThemeListError).toBeNull();
  });

  it('copies a theme through the bridge and refreshes the list', async () => {
    const store = useAppStore();

    const result = await store.copyGuiTheme('builtin.light', 'rose', 'Rose');

    expect(copyTheme).toHaveBeenCalledWith('builtin.light', 'rose', 'Rose');
    expect(result.ok).toBe(true);
    expect(result.ok && result.theme.id).toBe('copied');
    expect(listThemes).toHaveBeenCalled();
  });

  it('renames the active theme and updates every snapshot id without losing draft edits', async () => {
    const store = useAppStore();
    const draft = makeTheme('rose');
    draft.regions.workspace.background = '#123456';
    store.guiTheme = makeTheme('rose');
    store.savedGuiTheme = makeTheme('rose');
    store.guiThemeDraft = draft;

    const result = await store.renameGuiTheme('rose', 'sakura', '樱花');

    expect(renameTheme).toHaveBeenCalledWith('rose', 'sakura', '樱花');
    expect(result.ok).toBe(true);
    expect(store.guiTheme?.id).toBe('sakura');
    expect(store.savedGuiTheme?.id).toBe('sakura');
    expect(store.guiThemeDraft?.id).toBe('sakura');
    expect(store.guiThemeDraft?.regions.workspace.background).toBe('#123456');
  });

  it('surfaces backend rejection when deleting the active theme and keeps state', async () => {
    deleteTheme.mockResolvedValueOnce({
      ok: false,
      error: { code: 'active_theme_delete', path: 'theme_id', message: '不能删除活动主题' },
    });
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('rose');

    const result = await store.deleteGuiTheme('rose');

    expect(result.ok).toBe(false);
    expect(result.ok === false && result.error.code).toBe('active_theme_delete');
    expect(store.savedGuiTheme?.id).toBe('rose');
    expect(listThemes).not.toHaveBeenCalled();
  });

  it('imports a theme zip and refreshes the list', async () => {
    const store = useAppStore();

    const result = await store.importGuiTheme();

    expect(importTheme).toHaveBeenCalledOnce();
    expect(result.ok).toBe(true);
    expect(result.ok && result.theme.id).toBe('imported');
    expect(listThemes).toHaveBeenCalled();
  });

  it('exports a theme and returns the destination path', async () => {
    const store = useAppStore();

    const result = await store.exportGuiTheme('rose');

    expect(exportTheme).toHaveBeenCalledWith('rose');
    expect(result.ok && result.path).toBe('C:/tmp/rose.zip');
  });

  it('imports a region image asset path without touching the draft', async () => {
    const store = useAppStore();
    store.guiThemeDraft = makeTheme('rose');
    store.guiThemeDraft.regions.workspace.image = null;

    const result = await store.importGuiThemeImage('rose', 'workspace');

    expect(importThemeImage).toHaveBeenCalledWith('rose', 'workspace');
    expect(result.ok && result.asset_path).toBe('assets/workspace.webp');
    expect(store.guiThemeDraft.regions.workspace.image).toBeNull();
  });

  it('loads an arbitrary theme for inheritance restore', async () => {
    const store = useAppStore();

    const result = await store.loadGuiTheme('builtin.light');

    expect(getTheme).toHaveBeenCalledWith('builtin.light');
    expect(result.ok && result.theme.id).toBe('builtin.light');
  });

  it('stages an explicit final draft when saving directly from the editor', async () => {
    const store = useAppStore();
    store.savedGuiTheme = makeTheme('rose');
    store.guiThemeDraft = makeTheme('rose');
    const finalDraft = makeTheme('rose');
    finalDraft.regions.workspace.background = '#654321';

    await store.saveGuiThemeDraft(finalDraft);

    expect(saveTheme).toHaveBeenCalledOnce();
    const saved = saveTheme.mock.calls[0][0] as GuiTheme;
    expect(saved.regions.workspace.background).toBe('#654321');
    expect(setActive).toHaveBeenCalledWith('rose');
  });

  it('候选主题的原生标题栏预览失败时，data-gui-theme-id 随 DOM 快照一起回滚', async () => {
    const store = useAppStore();
    // 先成功预览旧主题，html 上留下旧主题的身份钩子
    const first = await store.previewGuiTheme(makeTheme('builtin.dark'));
    expect(first.ok).toBe(true);
    expect(document.documentElement.dataset.guiThemeId).toBe('builtin.dark');
    // 候选主题的标题栏预览失败 → 回滚后 html 上应恢复旧主题 id，而不是停在候选主题
    previewTitlebar.mockResolvedValueOnce({ ok: false, error: activeThemeError });
    const result = await store.previewGuiTheme(makeTheme('builtin.qq2009', 'light'));
    expect(result.ok).toBe(false);
    expect(store.guiTheme?.id).toBe('builtin.dark');
    expect(document.documentElement.dataset.guiThemeId).toBe('builtin.dark');
  });
});
