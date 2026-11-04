<script setup lang="ts">
// NodeContextMenu（M3-T06）：画布右键菜单的纯展示层。
// 只按 ContextMenuState 渲染条目并 emit 语义动作，不 import store、不改 project：
// 复制/删除/成组/取消组的落点全在 CanvasPane 的 onContextMenuAction。
import { computed, onBeforeUnmount, watch, type CSSProperties } from 'vue';

import type { ContextMenuState } from '../types';

defineOptions({ name: 'NodeContextMenu' });

const props = withDefaults(
  defineProps<{
    state: ContextMenuState | null;
    /** 多选（≥2 个可成组节点）才显示「成组」 */
    canGroup?: boolean;
    /** 选中里含分组/隶属分组的节点才显示「取消组」 */
    canUngroup?: boolean;
  }>(),
  { canGroup: false, canUngroup: false },
);

const emit = defineEmits<{
  action: [payload: { type: 'node' | 'connection'; id: string; action: string }];
  close: [];
}>();

type MenuItem = { action: string; label: string; danger?: boolean };

/** 条目：节点 = 复制/删除（+多选成组/取消组）；连线 = 只删连线 */
const items = computed<MenuItem[]>(() => {
  const state = props.state;
  if (!state) return [];
  if (state.type === 'connection') return [{ action: 'delete', label: '删除连线', danger: true }];
  const list: MenuItem[] = [
    { action: 'copy', label: '复制' },
    { action: 'delete', label: '删除', danger: true },
  ];
  if (props.canGroup) list.push({ action: 'group', label: '成组' });
  if (props.canUngroup) list.push({ action: 'ungroup', label: '取消组' });
  return list;
});

/** 菜单落在右键位置（state.x/y 是相对画布容器的像素坐标） */
const menuStyle = computed<CSSProperties>(() => {
  const state = props.state;
  return { left: `${state?.x ?? 0}px`, top: `${state?.y ?? 0}px` };
});

function onItemClick(item: MenuItem): void {
  const state = props.state;
  if (!state) return;
  emit('action', {
    type: state.type,
    id: state.type === 'node' ? state.nodeId : state.connectionId,
    action: item.action,
  });
  emit('close');
}

/** 点菜单外/按 Esc → 关闭：菜单是浮层，任何外部点击都不该继续挂在画面上 */
function onDocumentPointerDown(event: PointerEvent): void {
  const root = document.querySelector('[data-test="canvas-context-menu"]');
  const target = event.target as Node | null;
  if (root && target && root.contains(target)) return;
  emit('close');
}

function onDocumentKeyDown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.stopPropagation();
    emit('close');
  }
}

/** 菜单出现时才挂全局监听（常驻监听会白跑每次点击） */
function bindListeners(): void {
  document.addEventListener('pointerdown', onDocumentPointerDown, true);
  document.addEventListener('keydown', onDocumentKeyDown, true);
  window.addEventListener('resize', emitClose);
  window.addEventListener('blur', emitClose);
}

function unbindListeners(): void {
  document.removeEventListener('pointerdown', onDocumentPointerDown, true);
  document.removeEventListener('keydown', onDocumentKeyDown, true);
  window.removeEventListener('resize', emitClose);
  window.removeEventListener('blur', emitClose);
}

function emitClose(): void {
  emit('close');
}

watch(
  () => props.state,
  (state) => {
    unbindListeners();
    if (state) bindListeners();
  },
  { immediate: true },
);

onBeforeUnmount(unbindListeners);
</script>

<template>
  <div
    v-if="state"
    class="context-menu"
    :style="menuStyle"
    data-test="canvas-context-menu"
    role="menu"
    @pointerdown.stop
    @contextmenu.prevent.stop
  >
    <div class="cm-head" data-test="context-menu-head">
      {{ state.type === 'node' ? '节点' : '连线' }}
    </div>
    <button
      v-for="item in items"
      :key="item.action"
      type="button"
      class="cm-item"
      :class="{ 'is-danger': item.danger }"
      :data-test="'context-menu-' + item.action"
      role="menuitem"
      @click="onItemClick(item)"
    >{{ item.label }}</button>
  </div>
</template>

<style scoped>
/* 右键位置浮层：z 序高于节点（50）与选区工具条（200），压在画布最上层 */
.context-menu {
  position: absolute;
  z-index: 300;
  min-width: 132px;
  padding: 4px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 8px;
  background: var(--bg-elevated, rgba(38, 38, 43, 0.97));
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(8px);
}

.cm-head {
  padding: 2px 8px 4px;
  color: var(--text-secondary, #888);
  font-size: 11px;
}

.cm-item {
  display: block;
  width: 100%;
  padding: 5px 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.cm-item:hover {
  background: rgba(255, 255, 255, 0.1);
}

.cm-item.is-danger {
  color: #e08a8a;
}
</style>