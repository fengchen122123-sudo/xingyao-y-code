import { ref, onUnmounted, type Ref } from 'vue';

export function useIsTouch(): Ref<boolean> {
  const supported =
    typeof window !== 'undefined' && typeof window.matchMedia === 'function';
  const mq = supported ? window.matchMedia('(pointer: coarse)') : null;
  const isTouch = ref(mq ? mq.matches : false);
  const onChange = (e: MediaQueryListEvent) => {
    isTouch.value = e.matches;
  };
  mq?.addEventListener?.('change', onChange);
  onUnmounted(() => mq?.removeEventListener?.('change', onChange));
  return isTouch;
}
