<template>
  <div
    ref="rootRef"
    class="infinite-canvas"
    :class="{ 'is-hand': handActive, 'is-panning': panning }"
    data-test="canvas-viewport"
    @mouseenter="hovering = true"
    @mouseleave="hovering = false"
    @pointerdown="onPointerDown"
    @wheel="onWheel"
  >
    <!-- 网格层：只做视觉、不接事件。尺寸与偏移随视口换算，画面随平移缩放一起对齐 -->
    <div
      v-if="backgroundMode !== 'plain'"
      class="ic-grid"
      :style="gridStyle"
      aria-hidden="true"
      data-test="canvas-grid"
    ></div>

    <!-- 世界坐标内容层：transform translate(x,y) scale(k)，子内容按世界坐标摆放 -->
    <div class="ic-world" :style="worldStyle" data-test="canvas-world">
      <slot></slot>
    </div>
  </div>
</template>

<script setup lang="ts">
// 自研无限画布渲染底座（M2-T06）：只负责「视口 → transform + 网格 + 输入手势」，
// 不认识节点/连线（M3 起经默认插槽挂世界坐标内容层）。
//
// 行为规格来源：参考仓 web/src/app/(user)/canvas/components/infinite-canvas.tsx。
// 与计划稿的差异（按「以参考仓为准」核正）：
//   1) 缩放系数用 1.1^(-deltaY/100)，即 exp(-deltaY * ln1.1 / 100)（计划稿写 0.001，
//      ln1.1/100 ≈ 0.000953，量级一致，取参考仓原式；系数不同时以参考仓为准）；
//   2) 缩放钳制区间 0.05 ~ 5（计划稿初稿写上界 4，参考仓源码 Math.min(Math.max(k, 0.05), 5)）。
//
// 受控约定：viewport 由父层持有；本组件内部维护一份 live 视口供高频手势即时出画面，
// 落盘只在手势结束时 emit 一次（参考仓同样「拖拽帧内只改 DOM，松手再同步父层」）。
import { computed, onBeforeUnmount, onMounted, ref, watch, type CSSProperties } from 'vue';

import type { CanvasBackgroundMode, Position, SelectionBoxState, ViewportTransform } from '../types';
import { isSelectionArmed, selectionBoxToRect, type SelectionCommitPayload } from '../utils/canvas-selection';

defineOptions({ name: 'InfiniteCanvas' });

const props = withDefaults(
  defineProps<{
    /** 受控视口：父层真值来源 */
    viewport: ViewportTransform;
    /** 背景形态三态（grid 线格 / dots 点阵 / plain 纯色），默认 grid */
    backgroundMode?: CanvasBackgroundMode;
    /** 当前选中集快照：Shift 框选要拿它做加选基底（选中态真值在 store，这里只读） */
    selectedNodeIds?: string[];
    selectedConnectionIds?: string[];
  }>(),
  {
    backgroundMode: 'grid',
    selectedNodeIds: () => [],
    selectedConnectionIds: () => [],
  },
);

const emit = defineEmits<{
  'update:viewport': [v: ViewportTransform];
  /** 框选中（已过阈值）：父层拿去渲染 SelectionBox */
  'selection-change': [state: SelectionBoxState];
  /** 松手提交：世界坐标矩形 + 是否 Shift 加选，父层做命中判定 */
  'selection-commit': [payload: SelectionCommitPayload];
  /** 空白处点击（拖拽未过阈值）：父层清空选中 */
  'canvas-click-empty': [];
}>();

/** 网格基准间距（参考仓 48px 口径，随 k 缩放） */
const GRID_SIZE = 48;
/** 缩放上下限：参考仓 infinite-canvas.tsx 的 0.05 ~ 5 */
const MIN_SCALE = 0.05;
const MAX_SCALE = 5;
/** 滚轮每 100px 缩放 1.1 倍（参考仓 Math.pow(1.1, -deltaY / 100)） */
const ZOOM_STEP = 1.1;
const ZOOM_PIXELS_PER_STEP = 100;
/** 「真的拖动了」阈值：与参考仓 hasMoved 一致（>3px） */
const PAN_MOVE_THRESHOLD = 3;

const rootRef = ref<HTMLDivElement | null>(null);

/** 渲染用视口：受控 prop 的即时副本，手势期间就地更新，画面不等父层回写 */
const live = ref<ViewportTransform>({ x: props.viewport.x, y: props.viewport.y, k: props.viewport.k });

const hovering = ref(false);
const spaceDown = ref(false);
const panning = ref(false);
/** 空格抓手只在指针悬停画布时生效：避免全局拦掉输入框/按钮的空格 */
const handActive = computed(() => spaceDown.value && hovering.value);

const panState = {
  startX: 0,
  startY: 0,
  initialX: 0,
  initialY: 0,
  moved: false,
};

// ── 框选（M3-T03）──────────────────────────────────────────────
// 左键落在空白处即起框：拖拽帧内只维护本组件的框选态（世界坐标），
// 松手才 emit 一次 selection-commit，命中判定与落 store 全在父层。
const selectionBoxState = ref<SelectionBoxState | null>(null);
const selecting = ref(false);

const marquee = {
  startWorldX: 0,
  startWorldY: 0,
  additive: false,
  initialNodeIds: [] as string[],
  initialConnectionIds: [] as string[],
  /** 是否已拖过阈值：没过阈值＝空白点击，松手清选而不是提交空框 */
  armed: false,
};

// prop 变化（打开项目、外部聚焦复位、父层回写）时对齐渲染视口；拖拽中不打断手势
watch(
  () => props.viewport,
  (v) => {
    if (panning.value) return;
    live.value = { x: v.x, y: v.y, k: v.k };
  },
  { deep: true },
);

const worldStyle = computed<CSSProperties>(() => {
  const v = live.value;
  return {
    transform: `translate(${v.x}px, ${v.y}px) scale(${v.k})`,
    // 平移/缩放帧内提升为独立合成层（参考仓 worldRef.style.willChange 同口径）
    willChange: panning.value ? 'transform' : undefined,
    // M3 节点按 1/k 反向缩放绘制 UI 时取用（参考仓 --canvas-inverse-scale）
    '--canvas-inverse-scale': String(1 / Math.max(v.k, 0.0001)),
  } as CSSProperties;
});

const gridStyle = computed<CSSProperties>(() => {
  const v = live.value;
  const size = Math.max(GRID_SIZE * v.k, 0.0001);
  const dotSize = v.k < 0.12 ? 0.8 : 1.15;
  const line = 'var(--canvas-grid-line, #3a3a44)';
  const dot = 'var(--canvas-grid-dot, #55555f)';
  return {
    backgroundImage:
      props.backgroundMode === 'dots'
        ? `radial-gradient(circle, ${dot} ${dotSize}px, transparent ${dotSize + 0.2}px)`
        : `linear-gradient(${line} 1px, transparent 1px), linear-gradient(90deg, ${line} 1px, transparent 1px)`,
    backgroundSize: `${size}px ${size}px`,
    // 无限画布只需对格距取模：视口平移多少，网格就错开多少
    backgroundPosition: `${v.x % size}px ${v.y % size}px`,
  };
});

/**
 * 屏幕坐标（相对画布容器左上角，与滚轮锚点同口径）→ 世界坐标。
 * 用当前渲染视口换算：拖拽帧内也返回指针下真实的世界点。
 */
function screenToWorld(x: number, y: number): Position {
  const v = live.value;
  const k = v.k || 1;
  return { x: (x - v.x) / k, y: (y - v.y) / k };
}

/** 世界坐标 → 屏幕坐标（screenToWorld 的逆运算） */
function worldToScreen(p: Position): Position {
  const v = live.value;
  return { x: p.x * v.k + v.x, y: p.y * v.k + v.y };
}

function localPoint(clientX: number, clientY: number): Position {
  const rect = rootRef.value?.getBoundingClientRect();
  return { x: clientX - (rect?.left ?? 0), y: clientY - (rect?.top ?? 0) };
}

function onWheel(event: WheelEvent): void {
  const target = event.target as Element | null;
  // 节点内的可滚动区域（M3 起有）照常滚，不缩放画布
  if (target?.closest('[data-canvas-no-zoom]')) return;
  event.preventDefault();

  const anchor = localPoint(event.clientX, event.clientY);
  const current = live.value;
  const factor = Math.pow(ZOOM_STEP, -event.deltaY / ZOOM_PIXELS_PER_STEP);
  const nextK = Math.min(Math.max(current.k * factor, MIN_SCALE), MAX_SCALE);
  if (Math.abs(nextK - current.k) < 1e-9) return; // 已顶到缩放上下限，不做无意义 emit

  // 锚点公式：指针下的世界坐标缩放前后不变
  //   world = (m - x) / k；x' = m - world * k'  ⇒  x' = m - (m - x) * k'/k
  const world = screenToWorld(anchor.x, anchor.y);
  const next: ViewportTransform = {
    x: anchor.x - world.x * nextK,
    y: anchor.y - world.y * nextK,
    k: nextK,
  };
  live.value = next;
  emit('update:viewport', { ...next });
}

function onPointerDown(event: PointerEvent): void {
  const target = event.target as Element | null;
  if (target?.closest('[data-canvas-no-zoom]')) return;

  const onContent = Boolean(target?.closest('[data-node-id],[data-connection-id]'));
  // 中键 / 空格临时抓手：平移画布
  const wantPan = event.button === 1 || (event.button === 0 && handActive.value && !onContent);
  if (wantPan) {
    // 拖拽期间不选中：按下即吃掉默认行为（文本选择、原生拖拽、中键自动滚动）
    event.preventDefault();
    panState.startX = event.clientX;
    panState.startY = event.clientY;
    panState.initialX = live.value.x;
    panState.initialY = live.value.y;
    panState.moved = false;
    panning.value = true;
    capturePointer(event.pointerId);
    return;
  }

  // 空白处左键：起框选（节点/连线自身的按下由它们的组件处理，不在这里抢）
  if (event.button !== 0 || onContent) return;
  event.preventDefault();
  startSelection(event);
}

function capturePointer(pointerId: number): void {
  try {
    rootRef.value?.setPointerCapture?.(pointerId);
  } catch {
    // jsdom / 无指针捕获能力的环境：靠 window 上的 pointermove/pointerup 兜住
  }
}

function startSelection(event: PointerEvent): void {
  const local = localPoint(event.clientX, event.clientY);
  const world = screenToWorld(local.x, local.y);
  marquee.startWorldX = world.x;
  marquee.startWorldY = world.y;
  marquee.additive = event.shiftKey;
  // Shift 加选基底：按下瞬间的选中集快照（参考仓 initialSelected* 同口径）
  marquee.initialNodeIds = [...props.selectedNodeIds];
  marquee.initialConnectionIds = [...props.selectedConnectionIds];
  marquee.armed = false;
  selecting.value = true;
  selectionBoxState.value = null;
  capturePointer(event.pointerId);
}

/** 拖拽帧内更新框选态；没过阈值不渲染框、也不发事件（否则一按下就闪一个 0 宽框） */
function updateSelection(event: PointerEvent): void {
  if (event.buttons === 0) {
    // 丢了 pointerup 的兜底：按键已全松 → 直接提交
    finishSelection();
    return;
  }
  const local = localPoint(event.clientX, event.clientY);
  const world = screenToWorld(local.x, local.y);
  const next: SelectionBoxState = {
    startWorldX: marquee.startWorldX,
    startWorldY: marquee.startWorldY,
    currentWorldX: world.x,
    currentWorldY: world.y,
    additive: marquee.additive,
    initialSelectedNodeIds: marquee.initialNodeIds,
    initialSelectedConnectionIds: marquee.initialConnectionIds,
  };
  if (!marquee.armed && !isSelectionArmed(selectionBoxToRect(next))) {
    selectionBoxState.value = null;
    return;
  }
  marquee.armed = true;
  selectionBoxState.value = next;
  emit('selection-change', next);
}

/**
 * 收尾：过了阈值 → emit selection-commit（世界坐标矩形 + additive）；
 * 没过阈值 → 视为空白点击，emit canvas-click-empty 由父层清选。
 */
function finishSelection(): void {
  if (!selecting.value) return;
  selecting.value = false;
  const box = selectionBoxState.value;
  const additive = marquee.additive;
  marquee.armed = false;
  selectionBoxState.value = null;
  if (!box) {
    emit('canvas-click-empty');
    return;
  }
  emit('selection-commit', { rect: selectionBoxToRect(box), additive });
}

/** 失焦等场景的中断：不提交、不清选，只把框撤掉 */
function cancelSelection(): void {
  selecting.value = false;
  marquee.armed = false;
  selectionBoxState.value = null;
}

function onWindowPointerMove(event: PointerEvent): void {
  if (selecting.value) {
    updateSelection(event);
    return;
  }
  if (!panning.value) return;
  // 丢了 pointerup（指针移出窗口等）的兜底：按键已全松 → 收尾
  if (event.buttons === 0) {
    finishPan();
    return;
  }
  const dx = event.clientX - panState.startX;
  const dy = event.clientY - panState.startY;
  if (Math.abs(dx) > PAN_MOVE_THRESHOLD || Math.abs(dy) > PAN_MOVE_THRESHOLD) panState.moved = true;
  live.value = { x: panState.initialX + dx, y: panState.initialY + dy, k: live.value.k };
}

function onWindowPointerUp(): void {
  if (selecting.value) finishSelection();
  if (panning.value) finishPan();
}

function finishPan(): void {
  panning.value = false;
  if (!panState.moved) {
    // 按下没拖动（例如只是想拿回焦点）：渲染视口退回受控值，不发事件
    live.value = { x: props.viewport.x, y: props.viewport.y, k: props.viewport.k };
    return;
  }
  // 松手才提交一次：拖拽帧内不逐帧写 store，避免防抖落盘被顶住
  emit('update:viewport', { ...live.value });
}

function isTypingTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.tagName !== 'string') return false;
  if (el.isContentEditable) return true;
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT';
}

function onWindowKeyDown(event: KeyboardEvent): void {
  if (event.code !== 'Space' || isTypingTarget(event.target)) return;
  spaceDown.value = true;
  // 只在指针悬停画布时接管空格：全局 preventDefault 会连按钮的空格激活一起吞掉
  if (hovering.value) event.preventDefault();
}

function onWindowKeyUp(event: KeyboardEvent): void {
  if (event.code === 'Space') spaceDown.value = false;
}

function onWindowBlur(): void {
  spaceDown.value = false;
  cancelSelection();
  if (panning.value) finishPan();
}

onMounted(() => {
  window.addEventListener('keydown', onWindowKeyDown);
  window.addEventListener('keyup', onWindowKeyUp);
  window.addEventListener('blur', onWindowBlur);
  window.addEventListener('pointermove', onWindowPointerMove);
  window.addEventListener('pointerup', onWindowPointerUp);
  window.addEventListener('pointercancel', onWindowPointerUp);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onWindowKeyDown);
  window.removeEventListener('keyup', onWindowKeyUp);
  window.removeEventListener('blur', onWindowBlur);
  window.removeEventListener('pointermove', onWindowPointerMove);
  window.removeEventListener('pointerup', onWindowPointerUp);
  window.removeEventListener('pointercancel', onWindowPointerUp);
});

defineExpose({ screenToWorld, worldToScreen });
</script>

<style scoped>
.infinite-canvas {
  position: relative;
  width: 100%;
  height: 100%;
  overflow: hidden;
  /* 画布内不参与文本选择/原生拖拽，拖拽手势不会顺手选中 DOM 文本 */
  user-select: none;
  -webkit-user-select: none;
  touch-action: none;
  background: var(--canvas-bg, #202024);
}

.infinite-canvas.is-hand {
  cursor: grab;
}

.infinite-canvas.is-panning {
  cursor: grabbing;
}

.ic-grid {
  position: absolute;
  inset: 0;
  pointer-events: none;
  opacity: 0.4;
}

.ic-world {
  position: absolute;
  top: 0;
  left: 0;
  transform-origin: 0 0;
}
</style>
