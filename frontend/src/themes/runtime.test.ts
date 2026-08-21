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

    await expect(applyGuiTheme(value, api)).rejects.toThrow('missing');
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:workspace');
    expect(document.documentElement.style.getPropertyValue('--theme-accent')).toBe('#old-value');
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
});
