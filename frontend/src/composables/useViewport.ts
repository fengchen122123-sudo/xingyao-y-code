import { getCurrentScope, onScopeDispose, ref } from 'vue';

export function useIsMobile() {
  const isMobile = ref(false);
  const mql = typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia('(max-width: 768px)')
    : null;
  if (!mql) return isMobile;

  isMobile.value = mql.matches;
  const apply = (e: MediaQueryListEvent | { matches: boolean }) => { isMobile.value = e.matches; };
  mql.addEventListener('change', apply);
  if (getCurrentScope()) onScopeDispose(() => mql.removeEventListener('change', apply));
  return isMobile;
}
