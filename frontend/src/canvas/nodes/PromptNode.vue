<template>
  <!-- 提示词节点（视觉稿 .node[data-node=prompt]）：TXT 徽章 + 标题，
       正文是提示词文本 + 标签胶囊，右侧输出端口 -->
  <div class="cv-node" :class="{ 'is-selected': selected }" data-test="canvas-node-prompt">
    <div class="cv-head">
      <span class="cv-kind cv-kind--txt">TXT</span>
      <span class="cv-title" :title="title">{{ title }}</span>
    </div>

    <div class="cv-textbody" data-test="canvas-prompt-body">
      <p class="cv-prompt-text">{{ promptText }}</p>
      <div v-if="tags.length" class="cv-tags">
        <span v-for="tag in tags" :key="tag">{{ tag }}</span>
      </div>
    </div>

    <!-- 右侧输出端口：提示词喂给下游生成节点 -->
    <Handle id="out" type="source" :position="Position.Right" :connectable="connectable" />
  </div>
</template>

<script setup lang="ts">
// 提示词节点：正文取 meta.text，没有就退回标题（导入的纯文本节点常只有 title）。
import { computed } from 'vue';
import { Handle, Position, type NodeProps } from '@vue-flow/core';
import {
  metaString,
  metaStringList,
  nodeTitle,
  type CanvasNodeData,
} from './types';

const props = withDefaults(
  defineProps<{
    id: string;
    selected?: boolean;
    connectable?: boolean;
    data: CanvasNodeData;
  }>(),
  { selected: false, connectable: undefined },
);

// 官方 NodeProps 形状对齐（见 ImageNode 里的说明：泛型实参编译器解析不了）
void (props as unknown as Pick<NodeProps<CanvasNodeData>, 'id' | 'selected' | 'connectable' | 'data'>);

const node = computed(() => props.data.node);
const title = computed(() => nodeTitle(node.value, '未命名提示词'));

/** 正文：meta.text → 标题 → 占位文案，保证正文区永远不空 */
const promptText = computed(() => {
  const text = metaString(node.value, 'text');
  if (text) return text;
  return node.value.title || '（空提示词）';
});

/** 标签胶囊：meta.tags（字符串数组，单个字符串也接受） */
const tags = computed(() => metaStringList(node.value, 'tags'));
</script>

<style scoped src="./node-shell.css"></style>
