import { ref, onUnmounted, type Ref } from 'vue';

// 触摸设备判定（手机/平板）：与布局宽度断点解耦。
// iPad 横屏再宽 pointer 仍是 coarse，桌面窗口拖再窄仍是 fine。
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
