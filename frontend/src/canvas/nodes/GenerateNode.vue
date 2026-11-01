<template>
  <!-- 生成节点（视觉稿 .node[data-node=gen]，默认选中态示例）：
       RUN 徽章 + 标题，正文是进度数字 + 阶段文字 + 扫光进度条 + 骨架屏 + 参数行，
       左右各一个端口（左进右出） -->
  <div class="cv-node" :class="{ 'is-selected': selected }" data-test="canvas-node-generate">
    <div class="cv-head">
      <span class="cv-kind cv-kind--gen">RUN</span>
      <span class="cv-title" :title="title">{{ title }}</span>
    </div>

    <div class="cv-gen-body">
      <div class="cv-gen-row">
        <span class="cv-gen-pct" data-test="canvas-generate-progress">
          {{ progress }}<small>%</small>
        </span>
        <span class="cv-gen-stage">{{ stageText }}</span>
      </div>

      <!-- 进度条：宽度随便进度走，::after 扫光由 node-shell.css 提供 -->
      <div class="cv-bar">
        <i :style="{ width: `${progress}%` }"></i>
      </div>

      <!-- 骨架屏：出图过程中的占位块，高光循环扫动 -->
      <div class="cv-skeleton" data-test="canvas-generate-skeleton"></div>

      <div v-if="paramList.length" class="cv-gen-meta">
        <span v-for="param in paramList" :key="param">{{ param }}</span>
      </div>
    </div>

    <!-- 左进右出：生成节点既消费上游提示词/图片，也把结果交给下游 -->
    <Handle id="in" type="target" :position="Position.Left" :connectable="connectable" />
    <Handle id="out" type="source" :position="Position.Right" :connectable="connectable" />
  </div>
</template>

<script setup lang="ts">
// 生成节点：进度/阶段/参数全部来自 meta，节点本身不轮询、不推任务——
// 运行态由上层写进 store，节点只按 meta 渲染（数据变了自然重绘）。
import { computed } from 'vue';
import { Handle, Position, type NodeProps } from '@vue-flow/core';
import {
  clampPercent,
  metaNumber,
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
const title = computed(() => nodeTitle(node.value, '生成任务'));

/** 进度：meta.progress（0~100），夹在合法区间内 */
const progress = computed(() => clampPercent(metaNumber(node.value, 'progress', 0)));

/** 阶段文字：meta.stage，例如 "sampling · 12/20 步" */
const stageText = computed(() => metaString(node.value, 'stage') || '排队中');

/**
 * 参数行：优先 meta.params 数组（文案由上层拼好）；
 * 否则用 meta 里的 size/seed/elapsed 兜底拼三条，避免空行。
 */
const paramList = computed(() => {
  const fromMeta = metaStringList(node.value, 'params');
  if (fromMeta.length) return fromMeta;
  const size = metaString(node.value, 'size');
  const seed = metaNumber(node.value, 'seed', -1);
  const elapsed = metaString(node.value, 'elapsed');
  return [size, seed >= 0 ? `seed ${seed}` : '', elapsed].filter((item) => item !== '');
});
</script>

<style scoped src="./node-shell.css"></style>
