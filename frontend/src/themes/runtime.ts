import type { PyWebViewAPI } from '../types/pywebview';
import type { GuiTheme, GuiThemeError, GuiThemeResult, ThemeRegionId } from './types';

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

export class GuiThemeRuntimeError extends Error implements GuiThemeError {
  readonly code: string;
  readonly path: string;

  constructor(code: string, path: string, message: string) {
    super(message);
    this.name = 'GuiThemeRuntimeError';
    this.code = code;
    this.path = path;
  }
}

function tokenValue(name: string, value: string | number): string {
  return typeof value === 'number' && PIXEL_TOKENS.has(name) ? `${value}px` : String(value);
}

function assetError(result: GuiThemeResult<AssetSuccess>): Error {
  if (!result.ok) {
    return new GuiThemeRuntimeError(
      result.error.code,
      result.error.path,
      result.error.message,
    );
  }
  return new GuiThemeRuntimeError('invalid_asset_response', 'asset_path', '主题图片响应格式无效');
}

function decodeAsset(result: GuiThemeResult<AssetSuccess>): Blob {
  if (!result.ok || !SUPPORTED_IMAGE_MIME_TYPES.has(result.mime) || typeof result.base64 !== 'string') {
    throw assetError(result);
  }
  let binary: string;
  try {
    binary = atob(result.base64);
  } catch {
    throw new GuiThemeRuntimeError('invalid_asset_base64', 'asset_path', '主题图片 Base64 数据无效');
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

function setAppImageVariables(values: Map<string, string>, appImage: GuiTheme['app_image'], imageValue: string): void {
  values.set('--theme-app-image', imageValue);
  values.set('--theme-app-image-opacity', String(appImage.image_opacity));
  values.set('--theme-app-image-size', appImage.image_size);
  values.set('--theme-app-image-position', appImage.image_position);
}

export async function applyGuiTheme(theme: GuiTheme, api: PyWebViewAPI): Promise<AppliedTheme> {
  const values = new Map<string, string>();
  for (const [name, cssVariable] of Object.entries(SEMANTIC_VARIABLES)) {
    const value = theme.tokens[name];
    if (value !== undefined) values.set(cssVariable, tokenValue(name, value));
  }

  const assetUrls = new Map<string, string>();
  try {
    const loadAsset = async (assetPath: string): Promise<string> => {
      let objectUrl = assetUrls.get(assetPath);
      if (objectUrl === undefined) {
        const result = await api.get_gui_theme_asset(theme.id, assetPath);
        objectUrl = URL.createObjectURL(decodeAsset(result));
        assetUrls.set(assetPath, objectUrl);
      }
      return `url("${objectUrl}")`;
    };
    // 全局背景图：垫在所有分区之下（body::before 消费 --theme-app-image-*）
    const appImage = theme.app_image;
    const appImageValue = appImage.image !== null ? await loadAsset(appImage.image) : 'none';
    setAppImageVariables(values, appImage, appImageValue);
    for (const regionId of Object.keys(REGION_VARIABLE_PREFIXES) as ThemeRegionId[]) {
      const region = theme.regions[regionId];
      const imageValue = region.image !== null ? await loadAsset(region.image) : 'none';
      setRegionVariables(values, regionId, region, imageValue);
    }
  } catch (error) {
    for (const objectUrl of assetUrls.values()) URL.revokeObjectURL(objectUrl);
    throw error;
  }

  for (const [name, value] of values) document.documentElement.style.setProperty(name, value);
  // light 类必须同时落在 html 与 body 上：html 上的 :root.light 提供无主题时的
  // 浅色语义默认值，运行时写入 documentElement 的 inline 主题变量才能覆盖它；
  // body 上的 light 继续服务既有 body.light 后代选择器（代码高亮等）。
  document.documentElement.classList.toggle('light', theme.appearance === 'light');
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

// ── 设置页内嵌预览：把草稿解析成 CSS 变量 + 资源 URL，不写真实 DOM ──
export interface ResolvedThemePreview {
  /** 语义 token 变量名（--theme-*）→ 值 */
  tokens: Record<string, string>;
  /** 各分区变量前缀（--theme-app-header 等）→ 完整变量映射 */
  regions: Record<string, Record<string, string>>;
  /** 全局背景图变量（--theme-app-image 等） */
  appImage: Record<string, string>;
  dispose(): void;
}

export async function resolveThemePreview(theme: GuiTheme, api: PyWebViewAPI): Promise<ResolvedThemePreview> {
  const tokens: Record<string, string> = {};
  for (const [name, cssVariable] of Object.entries(SEMANTIC_VARIABLES)) {
    const value = theme.tokens[name];
    if (value !== undefined) tokens[cssVariable] = tokenValue(name, value);
  }
  const assetUrls = new Map<string, string>();
  try {
    const loadAsset = async (assetPath: string): Promise<string> => {
      let objectUrl = assetUrls.get(assetPath);
      if (objectUrl === undefined) {
        const result = await api.get_gui_theme_asset(theme.id, assetPath);
        objectUrl = URL.createObjectURL(decodeAsset(result));
        assetUrls.set(assetPath, objectUrl);
      }
      return `url("${objectUrl}")`;
    };
    const appImage: Record<string, string> = {};
    const appImageValue = theme.app_image.image !== null ? await loadAsset(theme.app_image.image) : 'none';
    appImage['--theme-app-image'] = appImageValue;
    appImage['--theme-app-image-opacity'] = String(theme.app_image.image_opacity);
    appImage['--theme-app-image-size'] = theme.app_image.image_size;
    appImage['--theme-app-image-position'] = theme.app_image.image_position;

    const regions: Record<string, Record<string, string>> = {};
    for (const regionId of Object.keys(REGION_VARIABLE_PREFIXES) as ThemeRegionId[]) {
      const region = theme.regions[regionId];
      const imageValue = region.image !== null ? await loadAsset(region.image) : 'none';
      const regionValues = new Map<string, string>();
      setRegionVariables(regionValues, regionId, region, imageValue);
      regions[regionId] = Object.fromEntries(regionValues);
    }
    let disposed = false;
    return {
      tokens, regions, appImage,
      dispose() {
        if (disposed) return;
        disposed = true;
        for (const objectUrl of assetUrls.values()) URL.revokeObjectURL(objectUrl);
      },
    };
  } catch (error) {
    for (const objectUrl of assetUrls.values()) URL.revokeObjectURL(objectUrl);
    throw error;
  }
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
    body.ownerDocument.documentElement.classList.toggle('light', appearance === 'light');
  }

  const background = literalFragmentValue(fragment, 'bg');
  if (background !== null && /^[0-9a-fA-F]{6}$/.test(background)) {
    body.style.backgroundColor = `#${background}`;
  }
}
