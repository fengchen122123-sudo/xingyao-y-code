import { getCurrentScope, onScopeDispose, ref } from 'vue';

/**
 * 手机端视口判定：与 style.css 全局响应式断点保持一致（≤768px）。
 * 基于 matchMedia 监听跨断点变化，供组件做"列表/详情互斥钻取"之类的
 * 结构性交互切换（纯尺寸适配继续留给 CSS 媒体查询）。
 *
 * 环境兜底：SSR 或无 matchMedia 的测试环境（jsdom）返回恒为 false 的 ref，
 * 保证桌面行为不变、旧测试零感知。
 */
export function useIsMobile() {
  const isMobile = ref(false);
  const mql = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(max-width: 768px)')
    : null;
  if (!mql) return isMobile;

  isMobile.value = mql.matches;
  const apply = (e: MediaQueryListEvent | { matches: boolean }) => { isMobile.value = e.matches; };
  mql.addEventListener('change', apply);
  // 非 Vue 组件作用域调用时（如单元测试）静默跳过清理注册
  if (getCurrentScope()) onScopeDispose(() => mql.removeEventListener('change', apply));
  return isMobile;
}
