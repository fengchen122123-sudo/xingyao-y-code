import { ref, watch } from 'vue';

const STORAGE_KEY = 'ycode.appGlass';
export const GLASS_OPACITY_MAX = 100;
export const GLASS_BLUR_MAX = 40;

export const glassOpacity = ref(0);
export const glassBlur = ref(0);

function clamp(value: number, max: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.min(max, Math.max(0, Math.round(value)));
}

function apply() {
  const root = document.documentElement;
  root.style.setProperty('--app-glass-opacity', `${glassOpacity.value}%`);
  root.style.setProperty('--app-glass-blur', `${glassBlur.value}px`);
}

function persist() {
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ opacity: glassOpacity.value, blur: glassBlur.value }),
    );
  } catch {
  }
}

/** 应用启动时调用一次：读回偏好并立即生效（否则要打开设置页才看得到效果）。 */
export function loadAppGlass() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as { opacity?: unknown; blur?: unknown };
      if (typeof parsed.opacity === 'number') glassOpacity.value = clamp(parsed.opacity, GLASS_OPACITY_MAX);
      if (typeof parsed.blur === 'number') glassBlur.value = clamp(parsed.blur, GLASS_BLUR_MAX);
    }
  } catch {
  }
  apply();
}

export function setGlassOpacity(value: number) {
  glassOpacity.value = clamp(value, GLASS_OPACITY_MAX);
}

export function setGlassBlur(value: number) {
  glassBlur.value = clamp(value, GLASS_BLUR_MAX);
}

watch([glassOpacity, glassBlur], () => {
  apply();
  persist();
});
