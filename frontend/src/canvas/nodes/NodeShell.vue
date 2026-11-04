<template>
  <div
    class="node-shell"
    :class="{
      'is-selected': selected,
      'is-group': isGroup,
      'is-loading': isLoading,
      'is-error': isError,
      'is-static': !interactive,
    }"
    :data-node-id="node.id"
    :data-node-type="node.type"
    :data-selected="selected ? 'true' : 'false'"
    data-test="node-shell"
    @pointerdown="onBodyPointerDown"
  >
    <!-- 标题栏：普通节点挂在边框上方（参考仓 left-3 top-[-28px]），分组标题落在容器内左上角 -->
    <div class="ns-titlebar" :class="{ 'in-group': isGroup }" data-test="node-shell-titlebar" @dblclick.stop="startTitleEdit" @pointerdown.stop>
      <input
        v-if="editingTitle"
        ref="titleInput"
        v-model="titleDraft"
        class="ns-title-input"
        maxlength="64"
        data-test="node-shell-title-input"
        @blur="finishTitleEdit"
        @keydown.enter.prevent="finishTitleEdit"
        @keydown.esc.prevent="cancelTitleEdit"
      />
      <button
        v-else
        type="button"
        class="ns-title"
        :disabled="!interactive"
        title="双击修改节点名称"
        data-test="node-shell-title"
      >{{ node.title || fallbackTitle }}</button>
    </div>

    <div class="ns-frame">
      <div class="ns-body" data-test="node-shell-body">
        <slot></slot>
      </div>

      <!-- 八向缩放手柄：M3-T02 起接指针拖拽，按 freeResize 与纵横比约束 emit resize-node -->
      <div
        v-for="handle in RESIZE_HANDLES"
        :key="handle"
        class="ns-resize"
        :class="'h-' + handle"
        :data-test="'node-resize-handle'"
        :data-handle="handle"
        aria-hidden="true"
        @pointerdown.stop="onResizePointerDown($event, handle)"
      ></div>

      <!-- 左右连接端口（M3-T04 接线）：右端口起拉线，左端口接松手落点。
           - 两端 pointerdown 都 .stop：端口上按下不能触发节点拖拽，否则拉线会顺带挪节点；
           - 左端 pointerup 不 .stop：要让源节点挂在 window 上的收尾监听照常跑（清拖拽态） -->
      <div
        v-if="!isGroup"
        class="ns-port left"
        data-test="node-port"
        data-side="left"
        aria-hidden="true"
        @pointerdown.stop
        @pointerup="onTargetPortPointerUp"
      ></div>
      <div
        v-if="!isConfig"
        class="ns-port right"
        data-test="node-port"
        data-side="right"
        aria-hidden="true"
        @pointerdown.stop="onPortPointerDown($event, 'right')"
      ></div>

      <!-- loading 遮罩：metadata.status === 'loading'（M4 起由 job 状态驱动） -->
      <div v-if="isLoading" class="ns-loading" data-test="node-loading">
        <div class="ns-spinner" aria-hidden="true"></div>
        <span class="ns-loading-text">{{ loadingLabel }}</span>
      </div>

      <!-- error：红边由 .is-error 承担，这里给 errorDetails 摘要 -->
      <div v-if="isError" class="ns-error" data-test="node-error" :title="errorDetails">
        {{ errorDetails || '生成失败' }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// NodeShell（M3-T01）：节点外壳。只负责「框 + 标题 + 手柄/端口控件 + 状态遮罩」，
// 内容由 default 插槽交给 ImageNode/PanoramaNode/… 八类组件。
// 视觉与事件规格对齐参考仓 web/src/app/(user)/canvas/components/canvas-node.tsx：
//   - 圆角 16px、2px 描边、选中蓝 #2f80ff 描边 + 1px 光晕；
//   - 标题在节点外上方（-28px）可双击改名；
//   - 缩放手柄参考仓只有四角，本任务按计划稿要求铺八向（角 4 + 边 4）；
//   - 连接端口 left=target / right=source，鼠标悬停或选中时显现；
//   - 分组为半透明虚线容器，标题在容器内。
import { computed, nextTick, onBeforeUnmount, ref } from 'vue';

import type { CanvasNodeData } from '../types';
import { CanvasNodeType } from '../types';
import type { NodeActionEmits, NodeConnectEmits } from './actions';
import {
  connectionDrag,
  endConnectionDrag,
  moveConnectionDrag,
  startConnectionDrag,
} from './connection-drag';

defineOptions({ name: 'NodeShell' });

const props = defineProps<{
  node: CanvasNodeData;
  selected: boolean;
  /** 只读/拖拽模式下隐藏手柄与端口（M3-T03 框选期用于只读呈现），M3-T01/T02 恒为 true */
  interactive: boolean;
  /** 世界层当前缩放 k：屏幕像素 → 世界坐标的唯一换算因子（M3-T02），缺省 1 */
  scale?: number;
}>();

const emit = defineEmits<NodeActionEmits & NodeConnectEmits>();

/** 八向手柄：角 4 个 + 边 4 个（顺序固定，方便 M3-T02 按 data-handle 挂拖拽） */
const RESIZE_HANDLES = [
  'top-left', 'top', 'top-right', 'right',
  'bottom-right', 'bottom', 'bottom-left', 'left',
] as const;

const FALLBACK_TITLE: Record<CanvasNodeType, string> = {
  image: '未命名图片',
  panorama: '未命名全景图',
  text: '未命名文本',
  config: '生成配置',
  video: '未命名视频',
  audio: '未命名音频',
  director: '导演台',
  group: '分组',
};

const isGroup = computed(() => props.node.type === CanvasNodeType.Group);
const isConfig = computed(() => props.node.type === CanvasNodeType.Config);
const isLoading = computed(() => props.node.metadata?.status === 'loading');
const isError = computed(() => props.node.metadata?.status === 'error');
const errorDetails = computed(() => props.node.metadata?.errorDetails || '');
const fallbackTitle = computed(() => FALLBACK_TITLE[props.node.type] ?? '未命名节点');

const loadingLabel = computed(() => {
  const progress = props.node.metadata?.progress;
  return typeof progress === 'number' && progress > 0 ? `生成中 ${Math.round(progress)}%` : '生成中…';
});

// 标题就地改名：当前只 emit action（落库 M3-T02 接），不碰 store
const editingTitle = ref(false);
const titleDraft = ref('');
const titleInput = ref<HTMLInputElement | null>(null);

function startTitleEdit(): void {
  if (!props.interactive || editingTitle.value) return;
  titleDraft.value = props.node.title || '';
  editingTitle.value = true;
  void nextTick(() => titleInput.value?.focus());
}

function finishTitleEdit(): void {
  if (!editingTitle.value) return;
  editingTitle.value = false;
  const next = titleDraft.value.trim();
  if (!next || next === (props.node.title || '')) return;
  emit('action', { nodeId: props.node.id, action: 'update-title', args: { title: next } });
}

function cancelTitleEdit(): void {
  editingTitle.value = false;
}

// ── M3-T02 指针拖拽 / 缩放 ───────────────────────────────────────
// 几何状态一律由 store 持有：拖拽期间逐帧 emit action（move-nodes / resize-node），
// 组件自己不留位移副本——否则 NodeShell 根元素的 left/top 由 CanvasPane 的 style
// 透传（fallthrough 优先级高于组件自身 style），本地预览会被覆盖，反而两套真相。
const DRAG_THRESHOLD = 3;
const MIN_NODE_SIZE = 48;

type GestureKind = 'move' | 'resize';

interface GestureState {
  kind: GestureKind;
  pointerId: number;
  handle: string;
  startClientX: number;
  startClientY: number;
  lastClientX: number;
  lastClientY: number;
  /** 越过 3px 阈值后置真：未越过的按下-抬起按「点击」处理（只选中，不落位移） */
  moved: boolean;
  additive: boolean;
  /** 手势开始瞬间的世界坐标矩形，缩放全程以它为基准（props.node 会随 store 逐帧变化） */
  startRect: { x: number; y: number; width: number; height: number };
}

let gesture: GestureState | null = null;

/** 世界坐标换算因子：k 非法（0 / NaN / 负）时退化为 1，绝不让一次脏视口把节点拖飞 */
const worldScale = computed(() => {
  const k = props.scale;
  return typeof k === 'number' && Number.isFinite(k) && k > 0 ? k : 1;
});

function isInteractiveTarget(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null;
  if (!el || typeof el.closest !== 'function') return false;
  // 表单/媒体/按钮/链接上的按下属于控件自身，不当作节点拖拽
  return Boolean(el.closest('input, textarea, button, video, audio, a, select, [data-no-node-drag]'));
}

function onBodyPointerDown(event: PointerEvent): void {
  if (!props.interactive || event.button !== 0) return;
  if (editingTitle.value || gesture) return;
  if (isInteractiveTarget(event.target)) return;
  beginGesture(event, 'move', '');
}

function onResizePointerDown(event: PointerEvent, handle: string): void {
  if (!props.interactive || event.button !== 0) return;
  if (gesture) return;
  beginGesture(event, 'resize', handle);
}

function beginGesture(event: PointerEvent, kind: GestureKind, handle: string): void {
  gesture = {
    kind,
    pointerId: event.pointerId,
    handle,
    startClientX: event.clientX,
    startClientY: event.clientY,
    lastClientX: event.clientX,
    lastClientY: event.clientY,
    moved: false,
    additive: event.shiftKey,
    startRect: {
      x: props.node.position.x,
      y: props.node.position.y,
      width: props.node.width,
      height: props.node.height,
    },
  };
  // 监听挂 window：指针移出节点（甚至移出画布）也要跟手；jsdom 下同样可用
  window.addEventListener('pointermove', onGestureMove);
  window.addEventListener('pointerup', onGestureEnd);
  window.addEventListener('pointercancel', onGestureCancel);
}

function detachGestureListeners(): void {
  window.removeEventListener('pointermove', onGestureMove);
  window.removeEventListener('pointerup', onGestureEnd);
  window.removeEventListener('pointercancel', onGestureCancel);
}

function onGestureMove(event: PointerEvent): void {
  const g = gesture;
  if (!g || (event.pointerId !== undefined && g.pointerId !== event.pointerId)) return;
  // 丢了 pointerup 的兜底：按键已全松 → 按松手收尾
  if (event.buttons === 0) {
    finishGesture(g);
    return;
  }

  const totalDx = event.clientX - g.startClientX;
  const totalDy = event.clientY - g.startClientY;

  if (!g.moved) {
    if (Math.abs(totalDx) < DRAG_THRESHOLD && Math.abs(totalDy) < DRAG_THRESHOLD) return;
    g.moved = true;
    // 越过阈值的第一帧把阈值内积累的位移一并补上，节点才不会「落后手 3px」
    applyGesture(g, totalDx, totalDy);
    g.lastClientX = event.clientX;
    g.lastClientY = event.clientY;
    return;
  }

  if (g.kind === 'move') {
    // 平移是世界坐标增量：按上一帧到这一帧的差量下发
    applyGesture(g, event.clientX - g.lastClientX, event.clientY - g.lastClientY);
  } else {
    // 缩放是绝对值：始终相对手势起点重算，累积误差不会滚雪球
    applyGesture(g, totalDx, totalDy);
  }
  g.lastClientX = event.clientX;
  g.lastClientY = event.clientY;
}

function applyGesture(g: GestureState, dxScreen: number, dyScreen: number): void {
  const k = worldScale.value;
  if (g.kind === 'move') {
    const dx = dxScreen / k;
    const dy = dyScreen / k;
    if (dx === 0 && dy === 0) return;
    // ids 只给本节点：分组拖动要带动子节点，展开逻辑在 store.moveNodes 里统一做
    emit('action', { nodeId: props.node.id, action: 'move-nodes', args: { ids: [props.node.id], dx, dy } });
    return;
  }
  const rect = resizeRect(g.startRect, g.handle, dxScreen / k, dyScreen / k, props.node.metadata?.freeResize === true);
  emit('action', { nodeId: props.node.id, action: 'resize-node', args: { rect } });
}

/**
 * 八向手柄 → 新矩形（世界坐标）。
 * 未开 freeResize 时锁纵横比：角手柄取变化更大的一维作主导，边手柄只看自己那一维，
 * 另一维按比例回算，并把未拖动的边/轴固定住（拖右边不挪左边，拖上下边水平居中）。
 */
function resizeRect(
  start: { x: number; y: number; width: number; height: number },
  handle: string,
  dx: number,
  dy: number,
  free: boolean,
): { x: number; y: number; width: number; height: number } {
  const west = handle.includes('left');
  const east = handle.includes('right');
  const north = handle.startsWith('top');
  const south = handle.startsWith('bottom');
  const startRight = start.x + start.width;
  const startBottom = start.y + start.height;

  let width = start.width + (east ? dx : 0) - (west ? dx : 0);
  let height = start.height + (south ? dy : 0) - (north ? dy : 0);
  width = Math.max(MIN_NODE_SIZE, width);
  height = Math.max(MIN_NODE_SIZE, height);

  if (!free && start.width > 0 && start.height > 0) {
    const ratio = start.width / start.height;
    const scaleW = width / start.width;
    const scaleH = height / start.height;
    const scale = west || east
      ? (north || south
        ? (Math.abs(scaleW - 1) >= Math.abs(scaleH - 1) ? scaleW : scaleH)
        : scaleW)
      : scaleH;
    width = Math.max(MIN_NODE_SIZE, start.width * scale);
    height = Math.max(MIN_NODE_SIZE, width / ratio);
    if (height < MIN_NODE_SIZE) {
      // 触到下限时以高度为准回算宽度，保证比例与 48px 下限同时成立
      height = MIN_NODE_SIZE;
      width = Math.max(MIN_NODE_SIZE, height * ratio);
    }
  }

  const x = west ? startRight - width : east ? start.x : start.x + (start.width - width) / 2;
  const y = north ? startBottom - height : south ? start.y : start.y + (start.height - height) / 2;
  return { x, y, width, height };
}

function onGestureEnd(event: PointerEvent): void {
  const g = gesture;
  if (!g || (event.pointerId !== undefined && g.pointerId !== event.pointerId)) return;
  finishGesture(g);
}

function onGestureCancel(event: PointerEvent): void {
  const g = gesture;
  if (!g || (event.pointerId !== undefined && g.pointerId !== event.pointerId)) return;
  // 取消也算收尾：已落下的位移保持在 store 里，但仍走一次吸附，避免节点半挂在组边界
  finishGesture(g);
}

function finishGesture(g: GestureState): void {
  detachGestureListeners();
  gesture = null;

  // 阈值内＝点击：只做选中（M3-T03 会换成 store.setSelection 正式 action）
  if (!g.moved) {
    emit('action', { nodeId: props.node.id, action: 'select', args: { additive: g.additive } });
    return;
  }
  // 拖拽/缩放收尾：交给 store 做进组/出组判定（进组写 groupId，出组清除）
  emit('action', { nodeId: props.node.id, action: 'commit-group-snap', args: {} });
}

// ── M3-T04 端口拉线 ─────────────────────────────────────────────
// 分工：源节点（右端口）只负责「起手势 + 跟手更新 connectionDrag」；
// 目标节点（左端口）在 pointerup 时从单例里读出起点并 emit connect-request。
// 类型兼容/自连/重复/环一律不进节点组件——guard 在 store.connect 里过（M3-T01 约定）。

function onPortPointerDown(event: PointerEvent, side: 'left' | 'right'): void {
  if (!props.interactive || event.button !== 0) return;
  // 左端是 target 端：只接松手落点，不发起拉线
  if (side !== 'right') return;
  if (gesture) return;
  startConnectionDrag(props.node.id, event);
  // 监听挂 window：指针移出节点/移出画布也要跟手，jsdom 下同样可用
  window.addEventListener('pointermove', onConnectionMove);
  window.addEventListener('pointerup', onConnectionUp);
  window.addEventListener('pointercancel', onConnectionUp);
}

function detachConnectionListeners(): void {
  window.removeEventListener('pointermove', onConnectionMove);
  window.removeEventListener('pointerup', onConnectionUp);
  window.removeEventListener('pointercancel', onConnectionUp);
}

function onConnectionMove(event: PointerEvent): void {
  if (!connectionDrag.active) return;
  // 丢了 pointerup 的兜底：按键已全松 → 收尾（空白松手＝不建线）
  if (event.buttons === 0) {
    finishConnectionDrag();
    return;
  }
  moveConnectionDrag(event.clientX, event.clientY);
}

function onConnectionUp(): void {
  finishConnectionDrag();
}

function finishConnectionDrag(): void {
  detachConnectionListeners();
  endConnectionDrag();
}

/**
 * 目标端口松手：有进行中的拉线就请求连线（自连也照样上抛，让 guard 回一句
 * 「不能把节点连到自己」，比默默没反应更容易理解）。
 */
function onTargetPortPointerUp(): void {
  if (!connectionDrag.active) return;
  const fromNodeId = connectionDrag.fromNodeId;
  if (!fromNodeId) return;
  emit('connect-request', { fromNodeId, toNodeId: props.node.id });
}

onBeforeUnmount(() => {
  detachGestureListeners();
  detachConnectionListeners();
  gesture = null;
});

</script>

<style scoped>
.node-shell {
  position: absolute;
  box-sizing: border-box;
  /* 画布内不参与文本选择（正文 textarea 自己开 select-text） */
  user-select: none;
  -webkit-user-select: none;
}

/* ── 标题栏 ─────────────────────────────────────────────── */
.ns-titlebar {
  position: absolute;
  left: 12px;
  right: 12px;
  top: -26px;
  height: 22px;
  display: flex;
  align-items: center;
  z-index: 20;
}

.ns-titlebar.in-group {
  top: 10px;
  left: 12px;
  right: 12px;
  height: 24px;
}

.ns-title {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 2px;
  border: 0;
  border-bottom: 1px dashed transparent;
  background: transparent;
  color: var(--text-primary, #ddd);
  font-size: 12px;
  font-weight: 500;
  text-align: left;
  opacity: 0.78;
  cursor: text;
}

.ns-title:hover:not(:disabled) {
  opacity: 1;
  border-bottom-color: currentColor;
}

.ns-title:disabled {
  cursor: default;
}

.ns-title-input {
  width: 100%;
  height: 20px;
  padding: 0 2px;
  border: 0;
  border-bottom: 1px dashed var(--border-color, #55555f);
  background: transparent;
  color: var(--text-primary, #ddd);
  font-size: 12px;
  font-weight: 500;
  outline: none;
}

/* ── 边框 / 选中 / 错误 ──────────────────────────────────── */
.ns-frame {
  position: relative;
  display: flex;
  width: 100%;
  height: 100%;
  overflow: hidden;
  box-sizing: border-box;
  border: 2px solid var(--canvas-node-stroke, #3a3a44);
  border-radius: 16px;
  background: var(--canvas-node-fill, #26262b);
  transition: border-color 160ms ease, box-shadow 160ms ease;
}

.node-shell.is-group .ns-frame {
  border-style: dashed;
  border-radius: 14px;
  background: rgba(47, 128, 255, 0.06);
}

.node-shell.is-selected .ns-frame {
  border-color: var(--canvas-selection, #2f80ff);
  box-shadow: 0 0 0 1px rgba(47, 128, 255, 0.35);
}

.node-shell.is-error .ns-frame {
  border-color: #ef4444;
  box-shadow: 0 0 0 1px rgba(239, 68, 68, 0.45);
}

.ns-body {
  position: relative;
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
}

.ns-body > * {
  width: 100%;
  height: 100%;
}

/* ── 八向缩放手柄 ────────────────────────────────────────── */
.ns-resize {
  position: absolute;
  z-index: 30;
  border-radius: 3px;
  background: var(--canvas-selection, #2f80ff);
  border: 1px solid rgba(0, 0, 0, 0.35);
  opacity: 0;
  transition: opacity 140ms ease;
}

.node-shell:hover .ns-resize,
.node-shell.is-selected .ns-resize {
  opacity: 1;
}

.node-shell.is-static .ns-resize {
  display: none;
}

.h-top-left,
.h-top-right,
.h-bottom-left,
.h-bottom-right {
  width: 10px;
  height: 10px;
}

.h-top-left { left: -6px; top: -6px; cursor: nwse-resize; }
.h-top-right { right: -6px; top: -6px; cursor: nesw-resize; }
.h-bottom-left { left: -6px; bottom: -6px; cursor: nesw-resize; }
.h-bottom-right { right: -6px; bottom: -6px; cursor: nwse-resize; }

.h-top,
.h-bottom {
  left: 50%;
  width: 22px;
  height: 6px;
  transform: translateX(-50%);
  cursor: ns-resize;
}

.h-top { top: -5px; }
.h-bottom { bottom: -5px; }

.h-left,
.h-right {
  top: 50%;
  width: 6px;
  height: 22px;
  transform: translateY(-50%);
  cursor: ew-resize;
}

.h-left { left: -5px; }
.h-right { right: -5px; }

/* ── 左右连接端口 ────────────────────────────────────────── */
.ns-port {
  position: absolute;
  top: 50%;
  z-index: 25;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--bg-secondary, #202024);
  border: 2px solid var(--canvas-node-port, #7a7a86);
  transform: translateY(-50%);
  opacity: 0;
  cursor: crosshair;
  transition: opacity 140ms ease, transform 140ms ease;
}

.ns-port.left { left: -7px; }
.ns-port.right { right: -7px; }

.node-shell:hover .ns-port,
.node-shell.is-selected .ns-port {
  opacity: 1;
}

.ns-port:hover {
  transform: translateY(-50%) scale(1.25);
}

.node-shell.is-static .ns-port {
  display: none;
}

/* ── loading 遮罩 ────────────────────────────────────────── */
.ns-loading {
  position: absolute;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  background: rgba(24, 24, 28, 0.72);
  color: #cfd6e4;
  font-size: 12px;
}

.ns-spinner {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, 0.18);
  border-top-color: var(--canvas-selection, #2f80ff);
  animation: ns-spin 900ms linear infinite;
}

@keyframes ns-spin {
  to { transform: rotate(360deg); }
}

/* ── error 提示 ──────────────────────────────────────────── */
.ns-error {
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: 8px;
  z-index: 41;
  max-height: 60%;
  overflow: hidden;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  padding: 4px 8px;
  border: 1px solid rgba(239, 68, 68, 0.55);
  border-radius: 8px;
  background: rgba(239, 68, 68, 0.16);
  color: #fecaca;
  font-size: 11px;
  line-height: 1.5;
  word-break: break-all;
}
</style>
