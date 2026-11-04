<template>
  <div class="nc-config" data-test="node-content-config">
    <div class="nc-config-head">
      <span class="nc-config-mode" data-test="node-config-mode">{{ modeLabel }}</span>
      <span class="nc-config-model" data-test="node-config-model">{{ model || '未选模型' }}</span>
    </div>

    <dl class="nc-config-list">
      <div class="nc-config-row">
        <dt>尺寸</dt>
        <dd data-test="node-config-size">{{ size || '默认' }}</dd>
      </div>
      <div class="nc-config-row">
        <dt>数量</dt>
        <dd data-test="node-config-count">{{ count }}</dd>
      </div>
      <div class="nc-config-row prompt">
        <dt>提示词</dt>
        <dd class="nc-config-prompt" data-test="node-config-prompt" :title="prompt">{{ promptPreview }}</dd>
      </div>
    </dl>
  </div>
</template>

<script setup lang="ts">
// ConfigNode（M3-T01）：生成配置节点的展示态摘要（model / size / count / prompt 截断）。
// 真正的配置面板（模型下拉、尺寸选择、提示词编辑）属 M4-T02 的 config composer。
import { computed } from 'vue';

import type { CanvasNodeData, CanvasGenerationMode } from '../types';

defineOptions({ name: 'ConfigNode' });

const props = defineProps<{ node: CanvasNodeData }>();

/** 提示词摘要上限：节点窄，超出部分交给 title 悬浮全文 */
const PROMPT_PREVIEW_LIMIT = 80;

const MODE_LABEL: Record<CanvasGenerationMode, string> = {
  text: '文本生成',
  image: '图片生成',
  video: '视频生成',
  audio: '音频生成',
};

const modeLabel = computed(() => MODE_LABEL[props.node.metadata?.generationMode || 'image']);
const model = computed(() => props.node.metadata?.model || '');
const size = computed(() => props.node.metadata?.size || '');
const count = computed(() => props.node.metadata?.count ?? 1);
const prompt = computed(() => props.node.metadata?.prompt || '');
const promptPreview = computed(() => {
  const text = prompt.value.trim();
  if (!text) return '（未填写提示词）';
  return text.length > PROMPT_PREVIEW_LIMIT ? `${text.slice(0, PROMPT_PREVIEW_LIMIT)}…` : text;
});
</script>

<style scoped>
.nc-config {
  display: flex;
  width: 100%;
  height: 100%;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  box-sizing: border-box;
  overflow: hidden;
  color: var(--text-primary, #ddd);
  background: var(--canvas-node-fill, #26262b);
  font-size: 12px;
}

.nc-config-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.nc-config-mode {
  flex: 0 0 auto;
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(47, 128, 255, 0.16);
  color: #9cc2ff;
  font-size: 11px;
}

.nc-config-model {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  opacity: 0.78;
}

.nc-config-list {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  min-height: 0;
  overflow: hidden;
}

.nc-config-row {
  display: flex;
  gap: 8px;
  min-width: 0;
  line-height: 1.5;
}

.nc-config-row dt {
  flex: 0 0 40px;
  color: var(--text-secondary, #888);
}

.nc-config-row dd {
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.nc-config-row.prompt dd {
  white-space: pre-wrap;
  word-break: break-word;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}
</style>
