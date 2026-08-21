import type { PyWebViewAPI } from '../types/pywebview';
import type { GuiTheme, GuiThemeResult, ThemeRegionId } from './types';

const SEMANTIC_VARIABLES = {
  background_app: '--theme-background-app',
  background_panel: '--theme-background-panel',
  background_card: '--theme-background-card',
  background_elevated: '--theme-background-elevated',
  text_primary: '--theme-text-primary',
  text_bright: '--theme-text-bright',
  text_secondary: '--theme-text-secondary',
  text_faint: '--theme-text-faint',
  accent: '--theme-accent',
  accent_secondary: '--theme-accent-secondary',
  accent_soft: '--theme-accent-soft',
  border: '--theme-border',
  success: '--theme-success',
  warning: '--theme-warning',
  danger: '--theme-danger',
  orange: '--theme-orange',
  info: '--theme-info',
  purple: '--theme-purple',
  code_background: '--theme-code-background',
  float_background: '--theme-float-background',
  font_sans: '--theme-font-sans',
  font_mono: '--theme-font-mono',
  radius_small: '--theme-radius-small',
  radius_medium: '--theme-radius-medium',
  radius_large: '--theme-radius-large',
  shadow_color: '--theme-shadow-color',
  shadow_strength: '--theme-shadow-strength',
} as const;

const PIXEL_TOKENS = new Set(['radius_small', 'radius_medium', 'radius_large']);

const REGION_VARIABLE_PREFIXES: Record<ThemeRegionId, string> = {
  app_header: '--theme-app-header',
  sidebar: '--theme-sidebar',
  conversation_toolbar: '--theme-conversation-toolbar',
  workspace: '--theme-workspace',
  composer: '--theme-composer',
  right_panel: '--theme-right-panel',
  statusbar: '--theme-statusbar',
};

const SUPPORTED_IMAGE_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);

type AssetSuccess = { ok: true; mime: string; base64: string };

export interface AppliedTheme {
  dispose(): void;
}

function tokenValue(name: string, value: string | number): string {
  return typeof value === 'number' && PIXEL_TOKENS.has(name) ? `${value}px` : String(value);
}

function assetError(result: GuiThemeResult<AssetSuccess>): Error {
  if (!result.ok) return new Error(result.error.message);
  return new Error('主题图片响应格式无效');
}

function decodeAsset(result: GuiThemeResult<AssetSuccess>): Blob {
  if (!result.ok || !SUPPORTED_IMAGE_MIME_TYPES.has(result.mime) || typeof result.base64 !== 'string') {
    throw assetError(result);
  }
  let binary: string;
  try {
    binary = atob(result.base64);
  } catch {
    throw new Error('主题图片 Base64 数据无效');
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return new Blob([bytes], { type: result.mime });
}

function setRegionVariables(
  values: Map<string, string>,
  regionId: ThemeRegionId,
  region: GuiTheme['regions'][ThemeRegionId],
  imageValue: string,
): void {
  const prefix = REGION_VARIABLE_PREFIXES[regionId];
  values.set(`${prefix}-bg`, region.background);
  values.set(`${prefix}-text`, region.text);
  values.set(`${prefix}-border`, region.border);
  values.set(`${prefix}-accent`, region.accent);
  values.set(`${prefix}-bg-opacity`, String(region.background_opacity));
  values.set(`${prefix}-image`, imageValue);
  values.set(`${prefix}-image-opacity`, String(region.image_opacity));
  values.set(`${prefix}-image-size`, region.image_size);
  values.set(`${prefix}-image-position`, region.image_position);
}

export async function applyGuiTheme(theme: GuiTheme, api: PyWebViewAPI): Promise<AppliedTheme> {
  const values = new Map<string, string>();
  for (const [name, cssVariable] of Object.entries(SEMANTIC_VARIABLES)) {
    const value = theme.tokens[name];
    if (value !== undefined) values.set(cssVariable, tokenValue(name, value));
  }

  const assetUrls = new Map<string, string>();
  try {
    for (const regionId of Object.keys(REGION_VARIABLE_PREFIXES) as ThemeRegionId[]) {
      const region = theme.regions[regionId];
      let imageValue = 'none';
      if (region.image !== null) {
        let objectUrl = assetUrls.get(region.image);
        if (objectUrl === undefined) {
          const result = await api.get_gui_theme_asset(theme.id, region.image);
          objectUrl = URL.createObjectURL(decodeAsset(result));
          assetUrls.set(region.image, objectUrl);
        }
        imageValue = `url("${objectUrl}")`;
      }
      setRegionVariables(values, regionId, region, imageValue);
    }
  } catch (error) {
    for (const objectUrl of assetUrls.values()) URL.revokeObjectURL(objectUrl);
    throw error;
  }

  for (const [name, value] of values) document.documentElement.style.setProperty(name, value);
  document.body.classList.toggle('light', theme.appearance === 'light');

  let disposed = false;
  return {
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const objectUrl of assetUrls.values()) URL.revokeObjectURL(objectUrl);
    },
  };
}

function literalFragmentValue(fragment: string, name: string): string | null {
  const match = fragment.match(new RegExp(`(?:^|&)${name}=([^&]*)(?:&|$)`));
  return match?.[1] ?? null;
}

export function applyBootTheme(hash: string, body: HTMLElement): void {
  const fragment = hash.startsWith('#') ? hash.slice(1) : hash;
  const appearance = literalFragmentValue(fragment, 'theme');
  if (appearance === 'light' || appearance === 'dark') {
    body.classList.toggle('light', appearance === 'light');
  }

  const background = literalFragmentValue(fragment, 'bg');
  if (background !== null && /^[0-9a-fA-F]{6}$/.test(background)) {
    body.style.backgroundColor = `#${background}`;
  }
}
