<template>
  <!-- 选区工具条：跟随视口底部居中的浮层（挂在 canvas-mount 内，absolute 相对画布容器定位）。
       按钮只 emit，不 import store、不改 project —— 落库统一由 CanvasPane 的 onToolbarAction 处理。 -->
  <div
    v-if="visible && totalCount > 0"
    class="selection-toolbar"
    data-test="selection-toolbar"
    @pointerdown.stop
    @mousedown.stop
  >
    <span class="st-count" data-test="selection-count">已选 {{ totalCount }}</span>
    <button
      v-for="item in BUTTONS"
      :key="item.action"
      type="button"
      class="st-btn"
      :class="{ 'is-danger': item.action === 'delete' }"
      :data-test="'selection-' + item.action"
      :disabled="isDisabled(item.action)"
      :title="isDisabled(item.action) ? item.disabledHint || item.title : item.title"
      @click="emit('action', item.action)"
    >{{ item.label }}</button>
  </div>
</template>

<script setup lang="ts">
// M3-T03 选区工具条。按钮口径参考参考仓 canvas-selection-toolbar.tsx（打组/拆组/删除），
// 本任务按计划 Interfaces 定为「删除 / 复制 / 成组 / 取消组」四键；删除与复制在 M3-T05 才实装。
// script setup 内不允许 export，动作类型放在 utils/canvas-selection.ts，CanvasPane 共用同一份。
import { computed } from 'vue';

import type { SelectionToolbarAction } from '../utils/canvas-selection';

defineOptions({ name: 'SelectionToolbar' });

const props = withDefaults(
  defineProps<{
    selectedNodeCount: number;
    selectedConnectionCount: number;
    /** 由父层判定「当前有选中」；本层再叠一道 totalCount > 0 兜底 */
    visible: boolean;
  }>(),
  { selectedNodeCount: 0, selectedConnectionCount: 0, visible: false },
);

const emit = defineEmits<{ action: [action: SelectionToolbarAction] }>();

type ToolbarButton = {
  action: SelectionToolbarAction;
  label: string;
  title: string;
  /** 置灰理由（成组至少要两个节点，与参考仓 canGroup 同口径） */
  disabledHint?: string;
};

const BUTTONS: ToolbarButton[] = [
  { action: 'delete', label: '删除', title: '删除选中节点（M3-T05 实装）' },
  { action: 'copy', label: '复制', title: '复制选中节点（M3-T05 实装）' },
  { action: 'group', label: '成组', title: '将选中节点打成一组', disabledHint: '至少选中两个节点才能成组' },
  { action: 'ungroup', label: '取消组', title: '拆散选中的分组' },
];

const totalCount = computed(() => props.selectedNodeCount + props.selectedConnectionCount);

function isDisabled(action: SelectionToolbarAction): boolean {
  // 成组至少两个节点（参考仓 canGroup）；其余按钮只要有选中就能点
  if (action === 'group') return props.selectedNodeCount < 2;
  return totalCount.value === 0;
}
</script>

<style scoped>
.selection-toolbar {
  position: absolute;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%);
  z-index: 200;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 10px;
  background: var(--bg-elevated, rgba(38, 38, 43, 0.94));
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(8px);
  pointer-events: auto;
}

.st-count {
  padding: 0 4px;
  color: var(--text-secondary, #999);
  font-size: 11px;
}

.st-btn {
  padding: 4px 10px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: inherit;
  font-size: 12px;
  cursor: pointer;
}

.st-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
}

.st-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.st-btn.is-danger {
  color: #e08a8a;
}
</style>
