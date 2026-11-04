<template>
  <div class="nc-text" data-test="node-content-text" @dblclick.stop="startEdit">
    <textarea
      v-if="editing"
      ref="textareaRef"
      v-model="draft"
      class="nc-text-editor"
      data-test="node-text-textarea"
      data-canvas-no-zoom
      spellcheck="false"
      @blur="finishEdit"
      @keydown.esc.prevent="cancelEdit"
      @pointerdown.stop
      @wheel.stop
    ></textarea>
    <div v-else class="nc-text-body" :style="textStyle" data-test="node-text-body">
      <template v-if="content">{{ content }}</template>
      <span v-else class="nc-text-placeholder">双击编辑文本</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// TextNode（M3-T01）：展示 metadata.content；双击进入 textarea 编辑，
// 失焦 emit action 'update-text'（落 store 由 M3-T02 接，节点自己不写 store）。
import { computed, nextTick, ref } from 'vue';

import type { CanvasNodeData } from '../types';
import type { NodeActionEmits } from './actions';

defineOptions({ name: 'TextNode' });

const props = defineProps<{ node: CanvasNodeData }>();
const emit = defineEmits<NodeActionEmits>();

const content = computed(() => props.node.metadata?.content || '');
/** 字号跟 metadata.fontSize 走（参考仓默认 14，行高 1.65 倍） */
const textStyle = computed(() => {
  const fontSize = props.node.metadata?.fontSize || 14;
  return { fontSize: `${fontSize}px`, lineHeight: `${Math.round(fontSize * 1.65)}px` };
});

const editing = ref(false);
const draft = ref('');
const textareaRef = ref<HTMLTextAreaElement | null>(null);

function startEdit(): void {
  if (editing.value) return;
  draft.value = content.value;
  editing.value = true;
  void nextTick(() => textareaRef.value?.focus());
}

function finishEdit(): void {
  if (!editing.value) return;
  editing.value = false;
  // 内容没变就不打扰上层（避免无意义的落盘与历史记录）
  if (draft.value === content.value) return;
  emit('action', { nodeId: props.node.id, action: 'update-text', args: { content: draft.value } });
}

/** Esc = 放弃本次编辑：先把草稿退回原值再走同一条收尾路径，避免误 emit */
function cancelEdit(): void {
  draft.value = content.value;
  finishEdit();
}
</script>

<style scoped>
.nc-text {
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: var(--canvas-node-fill, #26262b);
}

.nc-text-body {
  width: 100%;
  height: 100%;
  padding: 12px 14px;
  box-sizing: border-box;
  overflow: auto;
  color: var(--text-primary, #ddd);
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

.nc-text-placeholder {
  color: var(--text-secondary, #888);
  opacity: 0.6;
}

.nc-text-editor {
  width: 100%;
  height: 100%;
  padding: 12px 14px;
  box-sizing: border-box;
  border: 0;
  outline: none;
  resize: none;
  background: transparent;
  color: var(--text-primary, #ddd);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  /* 输入框内允许选中文本：画布层默认 user-select: none 会穿进来 */
  user-select: text;
  -webkit-user-select: text;
}
</style>
