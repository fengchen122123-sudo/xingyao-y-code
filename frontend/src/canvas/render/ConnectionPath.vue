<template>
  <svg
    class="connection-path"
    :class="{ 'is-selected': selected }"
    data-test="connection-path"
    :data-connection-id="connection.id"
    width="1"
    height="1"
    :style="svgStyle"
    aria-hidden="true"
  >
    <!-- 世界坐标 SVG：宽高 1px + overflow visible，路径按世界坐标画在世界层里，
         随 InfiniteCanvas 的 transform 一起平移缩放（不需要自己换算视口） -->
    <!-- 命中区：透明 16px 描边。视觉线只有 2px，没有它基本点不中 -->
    <path
      class="cp-hit"
      :d="pathD"
      fill="none"
      stroke="transparent"
      :stroke-width="hitWidth"
      data-test="connection-hit"
      @pointerdown.stop
      @click.stop="onSelect"
    />
    <path
      class="cp-line"
      :class="{ 'is-selected': selected }"
      :d="pathD"
      fill="none"
      :stroke="selected ? SELECTED_STROKE : IDLE_STROKE"
      :stroke-width="lineWidth"
      :stroke-opacity="selected ? 1 : 0.82"
      data-test="connection-line"
    />
  </svg>
</template>

<script setup lang="ts">
// ConnectionPath（M3-T04）：一条连线的渲染，直译参考仓 components/canvas-connections.tsx 的
// ConnectionPath —— 源节点右侧中点 → 目标节点左侧中点，控制点水平外延 max(水平距/2, 50)。
//
// 与参考仓的差异只有一处：参考仓在 React 里用内联 SVG 组，靠主题取色；
// 这里改成「一条连线一个 svg」，粗细按 `1 / scale` 反缩 —— 世界层被 scale(k) 缩放，
// 线宽不反缩的话放大后线会变成大粗条、缩小后细到看不见。
import { computed, type CSSProperties } from 'vue';

import type { CanvasConnection, CanvasNodeData } from '../types';
import { buildConnectionPathD } from '../utils/canvas-connection-path';

defineOptions({ name: 'ConnectionPath' });

const props = withDefaults(
  defineProps<{
    connection: CanvasConnection;
    from: CanvasNodeData;
    to: CanvasNodeData;
    selected: boolean;
    /** 世界层当前缩放：线宽按 1/k 反缩，屏幕上看起来恒定 */
    scale?: number;
  }>(),
  { scale: 1 },
);

const emit = defineEmits<{ select: [connectionId: string] }>();

const IDLE_STROKE = '#7a7a86';
const SELECTED_STROKE = '#2f80ff';
/** 屏幕像素口径的线宽（再除以 k 拿到世界坐标口径） */
const BASE_LINE_WIDTH = 2;
const SELECTED_LINE_WIDTH = 3.5;
const HIT_WIDTH = 16;

const pathD = computed(() => buildConnectionPathD(props.from, props.to));

/** k 非法（0 / NaN / 负 / 未传）时按 1 处理：绝不让一次脏视口把线宽算成 NaN */
const safeScale = computed(() => {
  const k = props.scale;
  return typeof k === 'number' && Number.isFinite(k) && k > 0 ? k : 1;
});

const lineWidth = computed(
  () => (props.selected ? SELECTED_LINE_WIDTH : BASE_LINE_WIDTH) / safeScale.value,
);

const hitWidth = computed(() => HIT_WIDTH / safeScale.value);

const svgStyle = computed<CSSProperties>(() => ({
  position: 'absolute',
  left: '0px',
  top: '0px',
  overflow: 'visible',
  // 容器自身不吃指针，命中交给 .cp-hit 的 stroke 区
  pointerEvents: 'none',
}));

function onSelect(): void {
  emit('select', props.connection.id);
}
</script>

<style scoped>
.connection-path .cp-hit {
  cursor: pointer;
  pointer-events: stroke;
}

.connection-path .cp-line {
  pointer-events: none;
}
</style>
