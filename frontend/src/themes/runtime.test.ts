import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyBootTheme, applyGuiTheme } from './runtime';
import type { GuiTheme, ThemeRegionId } from './types';
import type { PyWebViewAPI } from '../types/pywebview';

const regionIds: ThemeRegionId[] = [
  'app_header', 'sidebar', 'conversation_toolbar', 'workspace',
  'composer', 'right_panel', 'statusbar',
];

function theme(): GuiTheme {
  const region = {
    background: '#faf6f8', text: '#2c2529', border: '#e5ccd8', accent: '#d96b9f',
    background_opacity: 1, image: null, image_opacity: 0.2,
    image_size: 'cover' as const, image_position: 'center' as const,
  };
  return {
    schema_version: 1, id: 'rose', name: 'Rose', appearance: 'light',
    extends: 'builtin.light', builtin: false,
    tokens: {
      accent: '#d96b9f', radius_medium: 8, shadow_strength: 0.35,
      unknown_token: 'must-not-be-mapped',
    },
    native_titlebar: {
      background: '#fff7fb', text: '#2c2529', border: '#e5ccd8', mode: 'light',
    },
    app_image: {
      image: null, image_opacity: 1, image_size: 'cover' as const, image_position: 'center' as const,
    },
    regions: Object.fromEntries(regionIds.map(id => [id, { ...region }])) as GuiTheme['regions'],
  };
}

function themeWithWorkspaceImage(): GuiTheme {
  const value = theme();
  value.regions.workspace.image = 'assets/workspace.webp';
  return value;
}

function fakeApiWithAsset() {
  return {
    get_gui_theme_asset: vi.fn().mockResolvedValue({
      ok: true, mime: 'image/webp', base64: 'AA==',
    }),
  } as unknown as PyWebViewAPI;
}

function fakeApi() {
  return fakeApiWithAsset();
}

beforeEach(() => {
  document.documentElement.removeAttribute('style');
  document.body.removeAttribute('style');
  document.body.className = '';
  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    value: vi.fn().mockReturnValue('blob:workspace'),
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('applyGuiTheme', () => {
  it('maps semantic and region tokens without injecting a style element', async () => {
    const applied = await applyGuiTheme(theme(), fakeApi());

    expect(document.documentElement.style.getPropertyValue('--theme-accent')).toBe('#d96b9f');
    expect(document.documentElement.style.getPropertyValue('--theme-radius-medium')).toBe('8px');
    expect(document.documentElement.style.getPropertyValue('--theme-shadow-strength')).toBe('0.35');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-bg')).toBe('#faf6f8');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-bg-opacity')).toBe('1');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-image')).toBe('none');
    expect(document.documentElement.style.getPropertyValue('--theme-unknown-token')).toBe('');
    expect(document.querySelector('style[data-user-theme]')).toBeNull();

    applied.dispose();
  });

  it('creates and revokes one blob URL per validated asset', async () => {
    const create = vi.mocked(URL.createObjectURL);
    const revoke = vi.mocked(URL.revokeObjectURL);
    const applied = await applyGuiTheme(themeWithWorkspaceImage(), fakeApiWithAsset());

    expect(create).toHaveBeenCalledTimes(1);
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-image'))
      .toBe('url("blob:workspace")');
    applied.dispose();
    applied.dispose();
    expect(revoke).toHaveBeenCalledTimes(1);
    expect(revoke).toHaveBeenCalledWith('blob:workspace');
  });

  it('applies a global app image beneath all regions and shares the blob cache', async () => {
    const value = themeWithWorkspaceImage();
    value.app_image.image = 'assets/workspace.webp';
    const api = fakeApiWithAsset();

    const applied = await applyGuiTheme(value, api);

    // 全局图与 workspace 分区引用同一资源：只解码一次、共用一个 Blob URL
    expect(api.get_gui_theme_asset).toHaveBeenCalledTimes(1);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(document.documentElement.style.getPropertyValue('--theme-app-image'))
      .toBe('url("blob:workspace")');
    expect(document.documentElement.style.getPropertyValue('--theme-app-image-opacity')).toBe('1');
    applied.dispose();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:workspace');
  });

  it('writes "none" for the global image when the theme has none', async () => {
    const applied = await applyGuiTheme(theme(), fakeApi());
    expect(document.documentElement.style.getPropertyValue('--theme-app-image')).toBe('none');
    applied.dispose();
  });

  it('global image overrides every region image (region images become none)', async () => {
    const value = themeWithWorkspaceImage();
    value.app_image.image = 'assets/global.png';
    value.regions.workspace.image = 'assets/workspace.webp';
    value.regions.composer.image = 'assets/composer.png';
    const api = fakeApiWithAsset();

    const applied = await applyGuiTheme(value, api);

    // 全局图生效：分区图全部强制 none，且分区图资源不被解码（只解码全局图一次）
    expect(document.documentElement.style.getPropertyValue('--theme-app-image'))
      .toBe('url("blob:workspace")');
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-image')).toBe('none');
    expect(document.documentElement.style.getPropertyValue('--theme-composer-image')).toBe('none');
    expect(api.get_gui_theme_asset).toHaveBeenCalledTimes(1);
    expect(api.get_gui_theme_asset).toHaveBeenCalledWith('rose', 'assets/global.png');
    applied.dispose();
  });

  it('global image also makes region background colors transparent', async () => {
    const value = themeWithWorkspaceImage();
    value.app_image.image = 'assets/global.png';
    // 分区背景设为不透明——全局图模式下仍必须强制透明，否则实底盖住全局图
    value.regions.workspace.background_opacity = 1;
    const api = fakeApiWithAsset();

    const applied = await applyGuiTheme(value, api);

    expect(document.documentElement.style.getPropertyValue('--theme-workspace-bg-opacity-pct')).toBe('0%');
    // 原始 0-1 值仍如实写入（供编辑器回读）
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-bg-opacity')).toBe('1');
    applied.dispose();

    // 移除全局图后分区背景透明度恢复用户设置
    value.app_image.image = null;
    const restored = await applyGuiTheme(value, api);
    expect(document.documentElement.style.getPropertyValue('--theme-workspace-bg-opacity-pct')).toBe('100%');
    restored.dispose();
  });

  it('reuses a blob URL when multiple regions reference the same asset', async () => {
    const value = themeWithWorkspaceImage();
    value.regions.app_header.image = 'assets/workspace.webp';
    const api = fakeApiWithAsset();

    const applied = await applyGuiTheme(value, api);

    expect(api.get_gui_theme_asset).toHaveBeenCalledTimes(1);
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(document.documentElement.style.getPropertyValue('--theme-app-header-image'))
      .toBe('url("blob:workspace")');
    applied.dispose();
  });

  it('revokes created URLs and leaves current variables untouched when an asset fails', async () => {
    document.documentElement.style.setProperty('--theme-accent', '#old-value');
    const value = themeWithWorkspaceImage();
    value.regions.app_header.image = 'assets/workspace.webp';
    value.regions.sidebar.image = 'assets/missing.png';
    const api = {
      get_gui_theme_asset: vi.fn()
        .mockResolvedValueOnce({ ok: true, mime: 'image/webp', base64: 'AA==' })
        .mockResolvedValueOnce({
          ok: false,
          error: { code: 'asset_not_found', path: 'asset_path', message: 'missing' },
        }),
    } as unknown as PyWebViewAPI;

    await expect(applyGuiTheme(value, api)).rejects.toMatchObject({
      code: 'asset_not_found',
      path: 'asset_path',
      message: 'missing',
    });
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:workspace');
    expect(document.documentElement.style.getPropertyValue('--theme-accent')).toBe('#old-value');
  });

  it('preserves security error details from the asset bridge', async () => {
    const value = themeWithWorkspaceImage();
    const api = {
      get_gui_theme_asset: vi.fn().mockResolvedValue({
        ok: false,
        error: { code: 'asset_escape', path: 'asset_path', message: '资源路径越界' },
      }),
    } as unknown as PyWebViewAPI;

    await expect(applyGuiTheme(value, api)).rejects.toMatchObject({
      code: 'asset_escape',
      path: 'asset_path',
      message: '资源路径越界',
    });
  });

  it('decodes validated assets into blobs with the exact mime and bytes', async () => {
    // base64 'AQID' → 字节 01 02 03
    const api = {
      get_gui_theme_asset: vi.fn().mockResolvedValue({
        ok: true, mime: 'image/png', base64: 'AQID',
      }),
    } as unknown as PyWebViewAPI;

    const applied = await applyGuiTheme(themeWithWorkspaceImage(), api);

    const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob;
    expect(blob.type).toBe('image/png');
    expect(Array.from(new Uint8Array(await blob.arrayBuffer()))).toEqual([1, 2, 3]);
    applied.dispose();
  });

  it('rejects unsupported asset mime or base64 without touching current variables', async () => {
    document.documentElement.style.setProperty('--theme-accent', '#old-value');
    const gifApi = {
      get_gui_theme_asset: vi.fn().mockResolvedValue({
        ok: true, mime: 'image/gif', base64: 'AA==',
      }),
    } as unknown as PyWebViewAPI;
    await expect(applyGuiTheme(themeWithWorkspaceImage(), gifApi))
      .rejects.toMatchObject({ code: 'invalid_asset_response', path: 'asset_path' });

    const brokenBase64Api = {
      get_gui_theme_asset: vi.fn().mockResolvedValue({
        ok: true, mime: 'image/png', base64: '###not-base64###',
      }),
    } as unknown as PyWebViewAPI;
    await expect(applyGuiTheme(themeWithWorkspaceImage(), brokenBase64Api))
      .rejects.toMatchObject({ code: 'invalid_asset_base64', path: 'asset_path' });

    expect(document.documentElement.style.getPropertyValue('--theme-accent')).toBe('#old-value');
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });
});

describe('applyBootTheme', () => {
  it('applies boot appearance and validated six-digit background', () => {
    applyBootTheme('#theme=light&bg=f4f4f1', document.body);

    expect(document.body.classList.contains('light')).toBe(true);
    expect(document.body.style.backgroundColor).toBe('rgb(244, 244, 241)');
  });

  it('applies a valid dark appearance and ignores unrelated startup markers', () => {
    document.body.classList.add('light');

    applyBootTheme('#theme=dark&bg=1a1a18&gui_theme_error=1', document.body);

    expect(document.body.classList.contains('light')).toBe(false);
    expect(document.body.style.backgroundColor).toBe('rgb(26, 26, 24)');
  });

  it('does not apply encoded or malformed fragment values', () => {
    document.body.classList.add('light');
    document.body.style.backgroundColor = 'rgb(1, 2, 3)';

    applyBootTheme('#theme=%64ark&bg=fff', document.body);

    expect(document.body.classList.contains('light')).toBe(true);
    expect(document.body.style.backgroundColor).toBe('rgb(1, 2, 3)');
  });

  it('marks os-win10 class on documentElement for win10 fragment', () => {
    applyBootTheme('#theme=light&bg=f4f4f1&os=win10', document.body);

    expect(document.documentElement.classList.contains('os-win10')).toBe(true);
    document.documentElement.classList.remove('os-win10');
  });

  it('does not mark os-win10 without the fragment flag', () => {
    document.documentElement.classList.remove('os-win10');

    applyBootTheme('#theme=light&bg=f4f4f1', document.body);

    expect(document.documentElement.classList.contains('os-win10')).toBe(false);
  });
});
