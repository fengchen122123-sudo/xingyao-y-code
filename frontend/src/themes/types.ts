export type ThemeAppearance = 'light' | 'dark';

export type ThemeRegionId =
  | 'app_header'
  | 'sidebar'
  | 'conversation_toolbar'
  | 'workspace'
  | 'composer'
  | 'right_panel'
  | 'statusbar';

export interface ThemeRegion {
  background: string;
  text: string;
  border: string;
  accent: string;
  background_opacity: number;
  image: string | null;
  image_opacity: number;
  image_size: 'cover' | 'contain' | 'auto';
  image_position: 'center' | 'top' | 'bottom' | 'left' | 'right';
}

export interface NativeTitlebarTheme {
  background: string;
  text: string;
  border: string;
  mode: ThemeAppearance;
}

export interface GuiTheme {
  schema_version: 1;
  id: string;
  name: string;
  appearance: ThemeAppearance;
  extends: 'builtin.light' | 'builtin.dark';
  builtin: boolean;
  tokens: Record<string, string | number>;
  native_titlebar: NativeTitlebarTheme;
  regions: Record<ThemeRegionId, ThemeRegion>;
}

export interface GuiThemeSummary {
  id: string;
  name: string;
  appearance: ThemeAppearance;
  extends: 'builtin.light' | 'builtin.dark';
  builtin: boolean;
}

export interface GuiThemeError {
  code: string;
  path: string;
  message: string;
}

export type ThemeDraft = Omit<GuiTheme, 'builtin'> & { builtin: false };

export type NativeTitlebarColorCapability =
  | 'supported'
  | 'unsupported'
  | 'pending'
  | 'not_applicable'
  | 'unknown';

export type GuiThemeFailure = { ok: false; error: GuiThemeError };

export type GuiThemeResult<T extends object = Record<never, never>> =
  | ({ ok: true } & T)
  | GuiThemeFailure;
