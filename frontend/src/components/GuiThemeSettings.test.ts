import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';

import GuiThemeSettings from './GuiThemeSettings.vue';
import { useAppStore } from '../stores/appStore';
import type { GuiTheme, GuiThemeSummary, ThemeRegionId } from '../themes/types';

const REGION_IDS: ThemeRegionId[] = [
  'app_header',
  'sidebar',
  'conversation_toolbar',
  'workspace',
  'composer',
  'right_panel',
  'statusbar',
];

function makeTheme(id: string, builtin: boolean): GuiTheme {
  const appearance = id.includes('light') ? 'light' : 'dark';
  return {
    schema_version: 1,
    id,
    name: id,
    appearance,
    extends: appearance === 'light' ? 'builtin.light' : 'builtin.dark',
    builtin,
    tokens: {
      background_app: '#101010',
      background_panel: '#181818',
      background_card: '#202020',
      background_elevated: '#282828',
      text_primary: '#f0f0f0',
      text_bright: '#ffffff',
      text_secondary: '#b0b0b0',
      text_faint: '#777777',
      accent: '#7867d9',
      accent_secondary: '#8f7de8',
      accent_soft: '#332f55',
      border: '#333333',
      success: '#3ca66b',
      warning: '#d39b35',
      danger: '#d95656',
      orange: '#d98445',
      info: '#4d8bd9',
      purple: '#9b78d9',
      code_background: '#121212',
      float_background: '#181818',
      font_sans: 'sans-serif',
      font_mono: 'monospace',
      radius_small: 4,
      radius_medium: 8,
      radius_large: 12,
      shadow_color: '#000000',
      shadow_strength: 0.3,
    },
    native_titlebar: {
      background: '#101010',
      text: '#f0f0f0',
      border: '#333333',
      mode: appearance,
    },
    app_image: { image: null, image_opacity: 1, image_size: 'cover' as const, image_position: 'center' as const },
    regions: Object.fromEntries(REGION_IDS.map(region => [region, {
      background: '#181818',
      text: '#f0f0f0',
      border: '#333333',
      accent: '#7867d9',
      background_opacity: 1,
      image: null,
      image_opacity: 0.2,
      image_size: 'cover',
      image_position: 'center',
    }])) as GuiTheme['regions'],
  };
}

const builtinLight = () => makeTheme('builtin.light', true);
const userTheme = () => makeTheme('rose', false);

let previewGuiTheme: ReturnType<typeof vi.fn>;
let saveGuiThemeDraft: ReturnType<typeof vi.fn>;
let cancelGuiThemePreview: ReturnType<typeof vi.fn>;
let activateGuiTheme: ReturnType<typeof vi.fn>;
let askConfirm: ReturnType<typeof vi.fn>;
let deleteGuiTheme: ReturnType<typeof vi.fn>;
let api: Record<string, ReturnType<typeof vi.fn>>;

function summary(theme: GuiTheme): GuiThemeSummary {
  return {
    id: theme.id,
    name: theme.name,
    appearance: theme.appearance,
    extends: theme.extends,
    builtin: theme.builtin,
  };
}

function mountThemeSettings(options: { active: GuiTheme; dirty?: boolean }) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  const active = structuredClone(options.active);
  const builtinDark = makeTheme('builtin.dark', true);
  const themes = [builtinDark, builtinLight(), ...(active.builtin ? [] : [active])];
  store.guiTheme = structuredClone(active);
  store.savedGuiTheme = structuredClone(active);
  store.guiThemeDraft = structuredClone(active);
  if (options.dirty && store.guiThemeDraft) store.guiThemeDraft.name += '（未保存）';
  store.guiThemes = themes.map(summary);

  previewGuiTheme = vi.spyOn(store, 'previewGuiTheme').mockResolvedValue({ ok: true, theme: active });
  saveGuiThemeDraft = vi.spyOn(store, 'saveGuiThemeDraft').mockResolvedValue({ ok: true, theme: active });
  cancelGuiThemePreview = vi.spyOn(store, 'cancelGuiThemePreview').mockResolvedValue({ ok: true, theme: active });
  activateGuiTheme = vi.spyOn(store, 'activateGuiTheme').mockResolvedValue({ ok: true, theme: active });
  askConfirm = vi.spyOn(store, 'askConfirm').mockResolvedValue(false);
  deleteGuiTheme = vi.spyOn(store, 'deleteGuiTheme').mockResolvedValue({ ok: true });

  return mount(GuiThemeSettings, { global: { plugins: [pinia] } });
}

beforeEach(() => {
  vi.restoreAllMocks();
  api = {
    list_gui_themes: vi.fn().mockResolvedValue({ ok: true, themes: [] }),
    get_gui_theme: vi.fn().mockResolvedValue({ ok: true, theme: makeTheme('builtin.dark', true) }),
    copy_gui_theme: vi.fn(),
    rename_gui_theme: vi.fn(),
    delete_gui_theme: vi.fn(),
    import_gui_theme: vi.fn(),
    export_gui_theme: vi.fn(),
    import_gui_theme_image: vi.fn(),
    remove_gui_theme_image: vi.fn(),
  };
  (window as any).pywebview = { api };
});

describe('GuiThemeSettings', () => {
  it('opens the editor on global colors', () => {
    const wrapper = mountThemeSettings({ active: userTheme() });

    expect(wrapper.get('[data-test="region-global"]').classes()).toContain('active');
    expect(wrapper.get('[data-test="region-workspace"]').classes()).not.toContain('active');
  });

  it('requires copying a built-in theme before editing', () => {
    const wrapper = mountThemeSettings({ active: builtinLight() });

    expect(wrapper.get('[data-test="theme-name"]').attributes('disabled')).toBeDefined();
    expect(wrapper.get('[data-test="copy-theme"]').text()).toContain('复制为自定义主题');
    expect(wrapper.find('[data-test="rename-theme"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="save-theme"]').exists()).toBe(false);
  });

  it('disables the editor delete button for the active theme (backend refuses)', () => {
    const wrapper = mountThemeSettings({ active: userTheme() });
    // 活动主题（正在使用）不能删除：按钮禁用并给出提示
    const btn = wrapper.get('[data-test="delete-theme"]');
    expect(btn.attributes('disabled')).toBeDefined();
    expect(btn.attributes('title')).toContain('先在左侧切换');
  });

  it('deletes an inactive user theme from the list entry', async () => {
    const other = makeTheme('spare-theme', false);
    const wrapper = mountThemeSettings({ active: userTheme() });
    // 列表里追加一个非活动用户主题
    const store = useAppStore();
    store.guiThemes = [...store.guiThemes, summary(other)];
    await nextTick();

    askConfirm.mockResolvedValueOnce(true);
    await wrapper.get('[data-test="delete-theme-spare-theme"]').trigger('click');

    expect(deleteGuiTheme).toHaveBeenCalledWith('spare-theme');
    // 列表里活动主题自己没有删除入口
    expect(wrapper.find('[data-test="delete-theme-rose"]').exists()).toBe(false);
  });

  it('keeps color picker and hex input synchronized and previews immediately', async () => {
    const wrapper = mountThemeSettings({ active: userTheme() });
    await wrapper.get('[data-test="region-workspace"]').trigger('click');

    await wrapper.get('[data-test="background-hex"]').setValue('#112233');

    expect(wrapper.get<HTMLInputElement>('[data-test="background-picker"]').element.value).toBe('#112233');
    expect(previewGuiTheme).toHaveBeenCalled();
    expect(previewGuiTheme.mock.calls.at(-1)?.[0].regions.workspace.background).toBe('#112233');
  });

  it('rejects invalid hex locally and does not save', async () => {
    const wrapper = mountThemeSettings({ active: userTheme() });

    await wrapper.get('[data-test="region-workspace"]').trigger('click');
    await wrapper.get('[data-test="background-hex"]').setValue('#xyz');
    await wrapper.get('[data-test="save-theme"]').trigger('click');

    expect(wrapper.get('[data-test="field-error"]').text()).toContain('#RRGGBB');
    expect(saveGuiThemeDraft).not.toHaveBeenCalled();
  });

  it('asks before leaving a dirty editor', async () => {
    const wrapper = mountThemeSettings({ active: userTheme(), dirty: true });

    await wrapper.get('[data-test="theme-builtin-dark"]').trigger('click');

    expect(askConfirm).toHaveBeenCalled();
    expect(activateGuiTheme).not.toHaveBeenCalled();
  });

  it('coalesces queued previews without losing the final input value', async () => {
    let releaseFirst!: (value: any) => void;
    const first = new Promise(resolve => { releaseFirst = resolve; });
    const wrapper = mountThemeSettings({ active: userTheme() });
    previewGuiTheme.mockImplementationOnce(() => first);
    await wrapper.get('[data-test="region-workspace"]').trigger('click');
    const input = wrapper.get('[data-test="background-hex"]');

    await input.setValue('#111111');
    await input.setValue('#222222');
    await input.setValue('#333333');
    expect(previewGuiTheme).toHaveBeenCalledTimes(1);

    releaseFirst({ ok: true, theme: userTheme() });
    await flushPromises();

    expect(previewGuiTheme).toHaveBeenCalledTimes(2);
    expect(previewGuiTheme.mock.calls[1][0].regions.workspace.background).toBe('#333333');
  });

  it('imports a region image and previews the returned asset path through the store', async () => {
    const wrapper = mountThemeSettings({ active: userTheme() });
    api.import_gui_theme_image.mockResolvedValue({ ok: true, asset_path: 'assets/workspace.webp' });

    await wrapper.get('[data-test="region-workspace"]').trigger('click');
    await wrapper.get('[data-test="choose-image"]').trigger('click');
    await flushPromises();

    expect(api.import_gui_theme_image).toHaveBeenCalledWith('rose', 'workspace');
    expect(previewGuiTheme.mock.calls.at(-1)?.[0].regions.workspace.image).toBe('assets/workspace.webp');
  });

  it('copies a built-in theme and activates the new custom theme', async () => {
    const copied = makeTheme('my-light', false);
    api.copy_gui_theme.mockResolvedValue({ ok: true, theme: copied });
    const wrapper = mountThemeSettings({ active: builtinLight() });

    await wrapper.get('[data-test="copy-theme"]').trigger('click');
    await wrapper.get('[data-test="copy-id"]').setValue('my-light');
    await wrapper.get('[data-test="copy-name"]').setValue('我的浅色主题');
    await wrapper.get('[data-test="confirm-copy"]').trigger('click');
    await flushPromises();

    expect(api.copy_gui_theme).toHaveBeenCalledWith('builtin.light', 'my-light', '我的浅色主题');
    expect(activateGuiTheme).toHaveBeenCalledWith('my-light');
  });

  it('edits native titlebar colors and mode without exposing image controls', async () => {
    const wrapper = mountThemeSettings({ active: userTheme() });

    await wrapper.get('[data-test="region-native-titlebar"]').trigger('click');
    await wrapper.get('[data-test="native-background-hex"]').setValue('#445566');
    await wrapper.get('[data-test="native-mode"]').setValue('light');

    expect(wrapper.find('[data-test="choose-image"]').exists()).toBe(false);
    expect(previewGuiTheme.mock.calls.at(-1)?.[0].native_titlebar).toMatchObject({
      background: '#445566',
      mode: 'light',
    });
  });

  it('cancels the preview and restores the persisted editor snapshot', async () => {
    const wrapper = mountThemeSettings({ active: userTheme() });
    await wrapper.get('[data-test="region-workspace"]').trigger('click');
    await wrapper.get('[data-test="background-hex"]').setValue('#112233');

    await wrapper.get('[data-test="cancel-theme"]').trigger('click');
    await flushPromises();

    expect(cancelGuiThemePreview).toHaveBeenCalledOnce();
    expect(wrapper.get<HTMLInputElement>('[data-test="background-hex"]').element.value).toBe('#181818');
  });

  it('keeps edits made while a save is in flight instead of overwriting them', async () => {
    let releaseSave!: (value: any) => void;
    const saving = new Promise(resolve => { releaseSave = resolve; });
    const wrapper = mountThemeSettings({ active: userTheme() });
    saveGuiThemeDraft.mockImplementationOnce(() => saving);
    await wrapper.get('[data-test="region-workspace"]').trigger('click');
    await wrapper.get('[data-test="background-hex"]').setValue('#111111');
    await wrapper.get('[data-test="save-theme"]').trigger('click');
    expect(saveGuiThemeDraft).toHaveBeenCalledOnce();

    // 保存飞行中继续输入：最终值不能被服务器返回的保存快照覆盖
    await wrapper.get('[data-test="background-hex"]').setValue('#222222');
    const savedOnServer = userTheme();
    savedOnServer.regions.workspace.background = '#111111';
    releaseSave({ ok: true, theme: savedOnServer });
    await flushPromises();

    expect(wrapper.get<HTMLInputElement>('[data-test="background-hex"]').element.value).toBe('#222222');
  });

  it('removing an image keeps other unsaved region edits in the draft', async () => {
    const wrapper = mountThemeSettings({ active: userTheme() });
    api.import_gui_theme_image.mockResolvedValue({ ok: true, asset_path: 'assets/workspace.webp' });
    await wrapper.get('[data-test="region-workspace"]').trigger('click');
    await wrapper.get('[data-test="choose-image"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="background-hex"]').setValue('#112233');

    // 服务器移除图片后返回的主题不含飞行前的本地颜色修改
    const serverTheme = userTheme();
    serverTheme.regions.workspace.image = null;
    api.remove_gui_theme_image.mockResolvedValue({ ok: true, theme: serverTheme });

    await wrapper.get('[data-test="remove-image"]').trigger('click');
    await flushPromises();

    expect(wrapper.get<HTMLInputElement>('[data-test="background-hex"]').element.value).toBe('#112233');
    expect(previewGuiTheme.mock.calls.at(-1)?.[0].regions.workspace.image).toBeNull();
  });
});
