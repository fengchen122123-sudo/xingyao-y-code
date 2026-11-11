import { ref, watch } from 'vue';

/**
 * 全屏玻璃层（夹在全局背景图与界面内容之间，见 style.css 的 `body::after`）。
 *
 * 只调两个数：覆盖透明度（%）与背景模糊强度（px），写进根变量的
 * `--app-glass-opacity` / `--app-glass-blur` 由 CSS 消费。
 * 默认都是 0 —— 也就是**默认完全不生效**，不影响既有观感。
 *
 * 持久化走 localStorage：与侧栏折叠状态、分组顺序等界面偏好同口径
 * （项目里没有统一的 localStorage 封装，各处都是直接读写 + try/catch 兜底），
 * 因此也不需要后端配置键，也就不牵扯主题 schema（core/gui_theme.py 对未知字段是明确报错）。
 */
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
    /* 隐私模式等写不进去：只在本次会话生效，不报错 */
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
    /* 旧值损坏就当没设置过，用默认（关闭） */
  }
  apply();
}

export function setGlassOpacity(value: number) {
  glassOpacity.value = clamp(value, GLASS_OPACITY_MAX);
}

export function setGlassBlur(value: number) {
  glassBlur.value = clamp(value, GLASS_BLUR_MAX);
}

// 模块级 watch：不绑定组件，随应用生命周期常驻。
watch([glassOpacity, glassBlur], () => {
  apply();
  persist();
});
