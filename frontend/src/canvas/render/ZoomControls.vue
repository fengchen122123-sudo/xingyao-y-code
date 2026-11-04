<script setup lang="ts">
// ZoomControls（M3-T06）：左下角浮层缩放条。
// 只算「下一个视口」并 emit（不 import store）：落 store.setViewport 由 CanvasPane 负责。
// 缩放锚点：有容器尺寸时按容器中心（视觉最稳），没有时退化为屏幕左上角。
import { computed } from 'vue';

import type { Position, ViewportTransform } from '../types';

defineOptions({ name: 'ZoomControls' });

const props = withDefaults(
  defineProps<{
    viewport: ViewportTransform;
    /** 可选：容器像素尺寸，用于把缩放锚点定在视口中心 */
    containerSize?: { width: number; height: number } | null;
  }>(),
  { containerSize: null },
);

const emit = defineEmits<{
  'update:viewport': [v: ViewportTransform];
  fit: [];
}>();

/** 与 InfiniteCanvas 同口径的缩放区间与步长（0.05 ~ 5，每步 1.1 倍） */
const MIN_SCALE = 0.05;
const MAX_SCALE = 5;
const ZOOM_STEP = 1.1;

const currentScale = computed(() => {
  const k = props.viewport?.k;
  return typeof k === 'number' && Number.isFinite(k) && k > 0 ? k : 1;
});

/** 百分比显示：整数四舍五入，最小 1%（k=0.05 显示 5%，不会出现 0%） */
const percentLabel = computed(() => `${Math.round(currentScale.value * 100)}%`);

function clampScale(k: number): number {
  return Math.min(Math.max(k, MIN_SCALE), MAX_SCALE);
}

/** 锚点（屏幕坐标）：容器中心 or 左上角 */
function zoomAnchor(): Position {
  const size = props.containerSize;
  const width = Number(size?.width);
  const height = Number(size?.height);
  if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) {
    return { x: width / 2, y: height / 2 };
  }
  return { x: 0, y: 0 };
}

/**
 * 缩放到目标系数：锚点下的世界坐标保持不动（与滚轮缩放同一公式）
 *   world = (anchor - x) / k；x' = anchor - world * k'
 */
function applyScale(nextK: number): void {
  const k = currentScale.value;
  const target = clampScale(nextK);
  if (Math.abs(target - k) < 1e-6) return;
  const anchor = zoomAnchor();
  const viewport = props.viewport ?? { x: 0, y: 0, k };
  const worldX = (anchor.x - viewport.x) / k;
  const worldY = (anchor.y - viewport.y) / k;
  emit('update:viewport', {
    x: anchor.x - worldX * target,
    y: anchor.y - worldY * target,
    k: target,
  });
}

/** ±：按固定步长缩放（1.1 / 1.1） */
function zoomBy(factor: number): void {
  applyScale(currentScale.value * factor);
}

/** 点百分比：复位 100%（锚点不变，画面不跳） */
function resetZoom(): void {
  applyScale(1);
}
</script>

<template>
  <div class="zoom-controls" data-test="zoom-controls" @pointerdown.stop @wheel.stop>
    <button type="button" class="zc-btn" data-test="zoom-out" title="缩小" @click="zoomBy(1 / ZOOM_STEP)">−</button>
    <button
      type="button"
      class="zc-percent"
      data-test="zoom-percent"
      title="点击复位到 100%"
      @click="resetZoom"
    >{{ percentLabel }}</button>
    <button type="button" class="zc-btn" data-test="zoom-in" title="放大" @click="zoomBy(ZOOM_STEP)">+</button>
    <button type="button" class="zc-fit" data-test="zoom-fit" title="适应内容（节点包围盒 + 80px 内边距）" @click="emit('fit')">适应内容</button>
  </div>
</template>

<style scoped>
/* 左下角浮层：避开右下角小地图与底部居中的选区工具条 */
.zoom-controls {
  position: absolute;
  left: 12px;
  bottom: 12px;
  z-index: 45;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 8px;
  background: var(--bg-elevated, rgba(38, 38, 43, 0.94));
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(8px);
}

.zc-btn,
.zc-percent,
.zc-fit {
  padding: 3px 8px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  font-size: 12px;
  line-height: 1.4;
  cursor: pointer;
}

.zc-btn {
  min-width: 24px;
  font-size: 14px;
  text-align: center;
}

.zc-percent {
  min-width: 48px;
  font-variant-numeric: tabular-nums;
}

.zc-btn:hover,
.zc-percent:hover,
.zc-fit:hover {
  background: rgba(255, 255, 255, 0.1);
}

.zc-fit {
  border-left: 1px solid var(--border-color, #3a3a3a);
  border-radius: 0 6px 6px 0;
  color: var(--text-secondary, #aaa);
}
</style>