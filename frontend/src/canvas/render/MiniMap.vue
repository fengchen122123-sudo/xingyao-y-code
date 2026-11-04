<script lang="ts">
// 几何纯函数放普通 <script> 块（与 <script setup> 同模块作用域）：
// 既给组件内部用，也让单测能直接 import 断言归一化/反算结果，不必靠 DOM 样式猜。
// 口径来源：参考仓 canvas-mini-map.tsx —— 世界包围盒（节点矩形 ∪ 视口矩形）归一化到
// 定宽缩略图，节点画小矩形、视口画取景框，点击/拖拽把缩略图坐标反算成世界坐标。
import type { CanvasNodeData, Position, ViewportTransform } from '../types';

/** 缩略图宽度上限（计划口径 180px）；高度另设上限，避免竖长画布把浮层拉出屏幕 */
export const MINI_MAP_MAX_WIDTH = 180;
export const MINI_MAP_MAX_HEIGHT = 132;
/** 世界包围盒外扩比例：小矩形不贴着缩略图边缘 */
export const MINI_MAP_PADDING_RATIO = 0.06;
/** 退化的世界尺寸下限：空节点集/零尺寸容器时避免除零 */
export const MINI_MAP_MIN_WORLD_SIZE = 1;

export type MiniMapWorldRect = { x: number; y: number; width: number; height: number };
export type MiniMapRect = { left: number; top: number; width: number; height: number };
export type MiniMapLayout = {
  /** 归一化后的世界包围盒（缩略图左上角 = 这里的 x/y） */
  world: MiniMapWorldRect;
  /** 缩略图尺寸（px，宽 ≤ MINI_MAP_MAX_WIDTH） */
  width: number;
  height: number;
  /** 世界 → 缩略图的缩放系数 */
  scale: number;
};

function safeScale(k: number | undefined): number {
  return typeof k === 'number' && Number.isFinite(k) && k > 0 ? k : 1;
}

/** 视口在当前容器里覆盖的世界矩形（屏幕左上角 = 世界 (x,y) 的换算逆运算） */
export function getViewportWorldRect(
  viewport: ViewportTransform,
  containerSize: { width: number; height: number },
): MiniMapWorldRect {
  const k = safeScale(viewport.k);
  const width = Math.max(Number(containerSize.width) || 0, MINI_MAP_MIN_WORLD_SIZE);
  const height = Math.max(Number(containerSize.height) || 0, MINI_MAP_MIN_WORLD_SIZE);
  return { x: -viewport.x / k, y: -viewport.y / k, width: width / k, height: height / k };
}

/** 节点包围盒；空节点集返回 null（负坐标照常参与取 min/max） */
export function getNodesWorldRect(nodes: CanvasNodeData[]): MiniMapWorldRect | null {
  if (!Array.isArray(nodes) || !nodes.length) return null;
  let left = Infinity;
  let top = Infinity;
  let right = -Infinity;
  let bottom = -Infinity;
  for (const node of nodes) {
    if (!node) continue;
    left = Math.min(left, node.position.x);
    top = Math.min(top, node.position.y);
    right = Math.max(right, node.position.x + node.width);
    bottom = Math.max(bottom, node.position.y + node.height);
  }
  if (!Number.isFinite(left) || !Number.isFinite(top)) return null;
  return {
    x: left,
    y: top,
    width: Math.max(right - left, MINI_MAP_MIN_WORLD_SIZE),
    height: Math.max(bottom - top, MINI_MAP_MIN_WORLD_SIZE),
  };
}

function unionRect(a: MiniMapWorldRect, b: MiniMapWorldRect): MiniMapWorldRect {
  const left = Math.min(a.x, b.x);
  const top = Math.min(a.y, b.y);
  const right = Math.max(a.x + a.width, b.x + b.width);
  const bottom = Math.max(a.y + a.height, b.y + b.height);
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/**
 * 世界包围盒（节点 ∪ 视口）+ 外扩 → 缩略图尺寸与缩放系数。
 * 视口始终并入：平移到空区域时缩略图不会突然跳到「只剩节点」那一片。
 */
export function computeMiniMapLayout(
  nodes: CanvasNodeData[],
  viewport: ViewportTransform,
  containerSize: { width: number; height: number },
): MiniMapLayout {
  const viewRect = getViewportWorldRect(viewport, containerSize);
  const nodesRect = getNodesWorldRect(nodes);
  const union = nodesRect ? unionRect(nodesRect, viewRect) : viewRect;
  const pad = Math.max(union.width, union.height) * MINI_MAP_PADDING_RATIO;
  const world: MiniMapWorldRect = {
    x: union.x - pad,
    y: union.y - pad,
    width: Math.max(union.width + pad * 2, MINI_MAP_MIN_WORLD_SIZE),
    height: Math.max(union.height + pad * 2, MINI_MAP_MIN_WORLD_SIZE),
  };
  // 先按宽度上限归一化，超出高度上限时再按高度收一次 → 宽高都不越界
  const scale = Math.min(MINI_MAP_MAX_WIDTH / world.width, MINI_MAP_MAX_HEIGHT / world.height);
  return {
    world,
    width: Math.max(world.width * scale, 1),
    height: Math.max(world.height * scale, 1),
    scale: scale > 0 && Number.isFinite(scale) ? scale : 1,
  };
}

/** 世界矩形 → 缩略图内的像素矩形（节点小方块与视口取景框共用） */
export function worldRectToMiniRect(rect: MiniMapWorldRect, layout: MiniMapLayout): MiniMapRect {
  const scale = layout.scale > 0 ? layout.scale : 1;
  return {
    left: (rect.x - layout.world.x) * scale,
    top: (rect.y - layout.world.y) * scale,
    width: Math.max(rect.width * scale, 1),
    height: Math.max(rect.height * scale, 1),
  };
}

/** 缩略图内坐标 → 世界坐标（点击/拖拽导航的反算） */
export function miniMapPointToWorld(
  localX: number,
  localY: number,
  layout: MiniMapLayout,
): Position {
  const scale = layout.scale > 0 ? layout.scale : 1;
  return {
    x: layout.world.x + (Number.isFinite(localX) ? localX : 0) / scale,
    y: layout.world.y + (Number.isFinite(localY) ? localY : 0) / scale,
  };
}
</script>

<script setup lang="ts">
// MiniMap（M3-T06）：右下角浮层缩略图。
// 只投递语义事件（navigate = 希望居中的世界坐标），改视口由 CanvasPane 调 store.setViewport。
import { computed, ref, type CSSProperties } from 'vue';

defineOptions({ name: 'MiniMap' });

const props = withDefaults(
  defineProps<{
    nodes: CanvasNodeData[];
    viewport: ViewportTransform;
    containerSize: { width: number; height: number };
  }>(),
  {
    nodes: () => [],
    viewport: () => ({ x: 0, y: 0, k: 1 }),
    containerSize: () => ({ width: 0, height: 0 }),
  },
);

const emit = defineEmits<{ navigate: [center: Position] }>();

const rootRef = ref<HTMLElement | null>(null);
/** 拖拽取景框：按下后 pointermove 持续 emit（调用方据此连续居中） */
const dragging = ref(false);

const layout = computed(() =>
  computeMiniMapLayout(
    Array.isArray(props.nodes) ? props.nodes : [],
    props.viewport ?? { x: 0, y: 0, k: 1 },
    props.containerSize ?? { width: 0, height: 0 },
  ),
);

const rootStyle = computed<CSSProperties>(() => ({
  width: `${layout.value.width}px`,
  height: `${layout.value.height}px`,
}));

/** 节点小矩形：按类型上不同色（分组更淡，视觉上垫底） */
const nodeRects = computed(() =>
  (Array.isArray(props.nodes) ? props.nodes : []).map((node) => {
    const rect = worldRectToMiniRect(
      { x: node.position.x, y: node.position.y, width: node.width, height: node.height },
      layout.value,
    );
    return {
      id: node.id,
      type: node.type,
      style: {
        left: `${rect.left}px`,
        top: `${rect.top}px`,
        width: `${rect.width}px`,
        height: `${rect.height}px`,
      } as CSSProperties,
    };
  }),
);

/** 视口取景框：至少 2px，缩到极小时仍能看见 */
const viewportStyle = computed<CSSProperties>(() => {
  const v = props.viewport ?? { x: 0, y: 0, k: 1 };
  const rect = worldRectToMiniRect(
    getViewportWorldRect(v, props.containerSize ?? { width: 0, height: 0 }),
    layout.value,
  );
  return {
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    width: `${Math.max(rect.width, 2)}px`,
    height: `${Math.max(rect.height, 2)}px`,
  };
});

/** 指针位置 → 缩略图内坐标（容器在 CSS 上等于 layout 尺寸，仍按实测比例归一化） */
function localPoint(clientX: number, clientY: number): Position {
  const el = rootRef.value;
  const rect = el?.getBoundingClientRect();
  const boxWidth = rect && rect.width > 0 ? rect.width : layout.value.width;
  const boxHeight = rect && rect.height > 0 ? rect.height : layout.value.height;
  const scaleX = boxWidth > 0 ? layout.value.width / boxWidth : 1;
  const scaleY = boxHeight > 0 ? layout.value.height / boxHeight : 1;
  const x = (clientX - (rect?.left ?? 0)) * scaleX;
  const y = (clientY - (rect?.top ?? 0)) * scaleY;
  return {
    x: Math.min(Math.max(x, 0), layout.value.width),
    y: Math.min(Math.max(y, 0), layout.value.height),
  };
}

function emitNavigateAt(clientX: number, clientY: number): void {
  if (!layout.value.width || !layout.value.height) return;
  const local = localPoint(clientX, clientY);
  emit('navigate', miniMapPointToWorld(local.x, local.y, layout.value));
}

function onPointerDown(event: PointerEvent): void {
  if (event.button !== 0) return;
  event.preventDefault();
  event.stopPropagation();
  dragging.value = true;
  try {
    rootRef.value?.setPointerCapture?.(event.pointerId);
  } catch {
    // jsdom / 无指针捕获能力的环境：靠 pointerup/pointercancel 兜底
  }
  emitNavigateAt(event.clientX, event.clientY);
}

function onPointerMove(event: PointerEvent): void {
  if (!dragging.value) return;
  // 丢了 pointerup 的兜底：按键已全松 → 结束拖拽
  if (event.buttons === 0) {
    dragging.value = false;
    return;
  }
  emitNavigateAt(event.clientX, event.clientY);
}

function endDrag(): void {
  dragging.value = false;
}
</script>

<template>
  <div
    ref="rootRef"
    class="mini-map"
    :class="{ 'is-dragging': dragging }"
    :style="rootStyle"
    data-test="canvas-minimap"
    @pointerdown="onPointerDown"
    @pointermove="onPointerMove"
    @pointerup="endDrag"
    @pointercancel="endDrag"
    @pointerleave="endDrag"
    @wheel.stop
    @contextmenu.stop.prevent
  >
    <div
      v-for="item in nodeRects"
      :key="item.id"
      class="mm-node"
      :class="'is-' + item.type"
      :style="item.style"
      :data-node-id="item.id"
      :data-test="'minimap-node-' + item.id"
    ></div>
    <div class="mm-viewport" :style="viewportStyle" data-test="minimap-viewport"></div>
  </div>
</template>

<style scoped>
/* 右下角浮层：与 SelectionToolbar 同口径（组件自己定位），z 序压在连线/节点之上 */
.mini-map {
  position: absolute;
  right: 12px;
  bottom: 12px;
  z-index: 40;
  overflow: hidden;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 8px;
  background: rgba(18, 18, 22, 0.82);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
  cursor: crosshair;
  touch-action: none;
}

.mini-map.is-dragging {
  cursor: grabbing;
}

.mm-node {
  position: absolute;
  border-radius: 1px;
  background: rgba(160, 170, 190, 0.55);
}

/* 分组更淡（视觉垫底）；生成类节点用品牌蓝，文本/配置用灰 */
.mm-node.is-group {
  background: rgba(140, 150, 170, 0.22);
  border: 1px solid rgba(160, 170, 190, 0.4);
}

.mm-node.is-image,
.mm-node.is-panorama,
.mm-node.is-video,
.mm-node.is-audio,
.mm-node.is-director {
  background: rgba(75, 139, 245, 0.6);
}

.mm-node.is-text,
.mm-node.is-config {
  background: rgba(170, 178, 192, 0.45);
}

/* 视口取景框：亮色描边，半透明底，压在节点之上（pointer-events: none 不挡导航） */
.mm-viewport {
  position: absolute;
  z-index: 2;
  border: 1px solid rgba(255, 255, 255, 0.85);
  border-radius: 2px;
  background: rgba(255, 255, 255, 0.08);
  pointer-events: none;
}
</style>