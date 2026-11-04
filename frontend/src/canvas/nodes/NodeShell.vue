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

      <!-- 八向缩放手柄：本任务只渲染控件（不接拖拽），world 坐标下的指针换算与
           freeResize/纵横比约束由 M3-T02 接 -->
      <div
        v-for="handle in RESIZE_HANDLES"
        :key="handle"
        class="ns-resize"
        :class="'h-' + handle"
        :data-test="'node-resize-handle'"
        :data-handle="handle"
        aria-hidden="true"
      ></div>

      <!-- 左右连接端口：本任务只渲染（不接线），拖拽连线由 M3-T04 接。
           与参考仓一致：分组只有右侧 source，生成配置没有右侧 source -->
      <div
        v-if="!isGroup"
        class="ns-port left"
        data-test="node-port"
        data-side="left"
        aria-hidden="true"
      ></div>
      <div
        v-if="!isConfig"
        class="ns-port right"
        data-test="node-port"
        data-side="right"
        aria-hidden="true"
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
import { computed, nextTick, ref } from 'vue';

import type { CanvasNodeData } from '../types';
import { CanvasNodeType } from '../types';
import type { NodeActionEmits } from './actions';

defineOptions({ name: 'NodeShell' });

const props = defineProps<{
  node: CanvasNodeData;
  selected: boolean;
  /** 只读/拖拽模式下隐藏手柄与端口（M3-T02/T03 用于只读呈现），M3-T01 恒为 true */
  interactive: boolean;
}>();

const emit = defineEmits<NodeActionEmits>();

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
