<template>
  <!-- 图片节点（视觉稿 .node[data-node=cat]）：IMG 徽章 + 标题 + 尺寸，
       中部 178px 图区，底部「时间 + 送去修改」，右侧输出端口 -->
  <div class="cv-node" :class="{ 'is-selected': selected }" data-test="canvas-node-image">
    <div class="cv-head">
      <span class="cv-kind cv-kind--img">IMG</span>
      <span class="cv-title" :title="title">{{ title }}</span>
      <span v-if="sizeText" class="cv-size">{{ sizeText }}</span>
    </div>

    <div class="cv-thumb">
      <!-- 有 url 才画 img：本项不做 URL 解析，空值直接走占位，绝不出现破图图标 -->
      <img v-if="node.url" class="cv-art" :src="node.url" :alt="title" />
      <div v-else class="cv-ph" data-test="canvas-image-placeholder">
        <svg class="cv-ph-ico" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3.5" y="5" width="17" height="14" rx="2" />
          <circle cx="9" cy="10" r="1.4" />
          <path d="M4.5 17l4.6-4.4 3.4 3.2 2.6-2.3 4.4 3.9" />
        </svg>
        <span class="cv-ph-name">{{ placeholderName }}</span>
        <span class="cv-ph-kind">image</span>
      </div>
      <div class="cv-grain"></div>
      <!-- 左下角模型名徽章 -->
      <span v-if="modelText" class="cv-badge">{{ modelText }}</span>
    </div>

    <div class="cv-foot">
      <!-- 溯源优先于时间：派生节点（改图产物）最要紧的信息是「我从哪来」，
          两段等宽文字挤在 264px 里必然断行，所以有来源时就把时间位让出来 -->
      <span v-if="sourceText" class="cv-from cv-origin" :title="sourceTitle">
        改自 {{ sourceText }}
      </span>
      <span v-else-if="timeText" class="cv-from">{{ timeText }}</span>
      <span class="cv-spacer"></span>
      <!-- nodrag：按钮落在可拖拽节点里，不标 nodrag 会被当成拖拽起点 -->
      <button class="cv-act nodrag" data-test="canvas-send-to-agent" @click="onSend">
        送去修改
      </button>
    </div>

    <!-- 左侧输入端口：改图/参考这类关系从左进。缺了它图片节点只能是链条起点，
         手动也接不上「谁改的它」 -->
    <Handle id="in" type="target" :position="Position.Left" :connectable="connectable" />
    <!-- 右侧输出端口：图片作为下游节点的输入 -->
    <Handle id="out" type="source" :position="Position.Right" :connectable="connectable" />
  </div>
</template>

<script setup lang="ts">
// 图片节点：只负责画，不改数据。选中态完全由 vue-flow 传入的 selected 决定，
// 「送去修改」只发事件，真正的后续动作由父级（CanvasPane）决定。
import { computed } from 'vue';
import { Handle, Position, type NodeProps } from '@vue-flow/core';
import {
  fileBaseName,
  metaNumber,
  metaString,
  nodeTimeText,
  nodeTitle,
  type CanvasNodeData,
  type NodeActionPayload,
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

// 官方 NodeProps 形状在此做一次编译期对齐（字段名/类型写错会直接报错）。
// 不能写成 defineProps<NodeProps<CanvasNodeData>>()：@vue/compiler-sfc 解析不了
// 外部泛型的实参，会抛 Unresolvable type reference，props 退化成空声明、
// selected 恒为 undefined，选中态就失效了。
void (props as unknown as Pick<NodeProps<CanvasNodeData>, 'id' | 'selected' | 'connectable' | 'data'>);

const emit = defineEmits<{ (e: 'send-to-agent', payload: NodeActionPayload): void }>();

const node = computed(() => props.data.node);
const title = computed(() => nodeTitle(node.value, '未命名图片'));

/** 头部右侧尺寸：优先 meta.size 文案，其次由 meta 里的宽高拼出来 */
const sizeText = computed(() => {
  const text = metaString(node.value, 'size');
  if (text) return text;
  const width = metaNumber(node.value, 'width', 0);
  const height = metaNumber(node.value, 'height', 0);
  return width > 0 && height > 0 ? `${width}×${height}` : '';
});

/** 图区左下角徽章：出图模型名 */
const modelText = computed(() => metaString(node.value, 'model'));

/** 占位文案：文件名优先，没有就回落到标题 */
const placeholderName = computed(
  () => fileBaseName(node.value.file) || title.value || '未生成图片',
);

const timeText = computed(() => nodeTimeText(node.value));

/**
 * 溯源文案：改图产物由画布层在落节点时写入 meta.source_file / source_title。
 * 只读展示，不参与任何查找——真正的溯源关系是节点之间那条「改图」边。
 */
const sourceTitle = computed(
  () => metaString(node.value, 'source_title') || metaString(node.value, 'source_file'),
);
const sourceText = computed(
  () =>
    fileBaseName(metaString(node.value, 'source_file')) || metaString(node.value, 'source_title'),
);

function onSend() {
  emit('send-to-agent', { id: props.id, action: 'edit' });
}
</script>

<style scoped src="./node-shell.css"></style>
