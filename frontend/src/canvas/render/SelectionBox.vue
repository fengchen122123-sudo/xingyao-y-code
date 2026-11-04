<template>
  <!-- 框选矩形：挂在 InfiniteCanvas 世界层内（父子 transform 同源），
       故 left/top 直接用世界坐标、不需要再乘 k；虚线蓝框 + 半透明填充，不吃指针事件。 -->
  <div v-if="rect" class="selection-box" :style="rectStyle" data-test="selection-box" aria-hidden="true"></div>
</template>

<script setup lang="ts">
// M3-T03 框选可视层。行为口径对齐参考仓 `[id]/canvas-client-page.tsx` 的 updateSelectionBoxEl：
// left/top 取起点终点的较小值，宽高取绝对差（起点在右下方也画得对）。
import { computed, type CSSProperties } from 'vue';

import type { SelectionBoxState } from '../types';
import { selectionBoxToRect } from '../utils/canvas-selection';

defineOptions({ name: 'SelectionBox' });

const props = defineProps<{
  /** 框选态：null ＝未在框选，不渲染任何 DOM */
  state: SelectionBoxState | null;
}>();

const rect = computed(() => (props.state ? selectionBoxToRect(props.state) : null));

const rectStyle = computed<CSSProperties>(() => {
  const r = rect.value;
  if (!r) return {};
  return {
    left: `${r.x}px`,
    top: `${r.y}px`,
    width: `${r.width}px`,
    height: `${r.height}px`,
  };
});
</script>

<style scoped>
.selection-box {
  position: absolute;
  z-index: 100;
  box-sizing: border-box;
  border: 1px dashed var(--canvas-selection, #2f80ff);
  border-radius: 2px;
  background: var(--canvas-selection-fill, rgba(47, 128, 255, 0.12));
  /* 框本身不吃指针：拖拽期间指针事件继续落在画布容器上 */
  pointer-events: none;
}
</style>
