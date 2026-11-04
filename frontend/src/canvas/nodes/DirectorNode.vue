<template>
  <div class="nc-director" data-test="node-content-director">
    <div class="nc-director-head">
      <span class="nc-director-title">导演台</span>
      <span class="nc-director-state" data-test="node-director-state">{{ hasProject ? '已编排' : '未编排' }}</span>
    </div>
    <p class="nc-director-hint">机位 / 分镜编排，输出参考图回画布</p>
    <button type="button" class="nc-director-btn" data-test="node-director-open" @click.stop="openDirector">
      打开导演台
    </button>
  </div>
</template>

<script setup lang="ts">
// DirectorNode（M3-T01）：导演台节点的展示态。按钮只 emit action 'open-director'，
// 真正的导演台面板与 project 数据编解码属 M7（本任务不碰 store）。
import { computed } from 'vue';

import type { CanvasNodeData } from '../types';
import type { NodeActionEmits } from './actions';

defineOptions({ name: 'DirectorNode' });

const props = defineProps<{ node: CanvasNodeData }>();
const emit = defineEmits<NodeActionEmits>();

const hasProject = computed(() => Boolean(props.node.metadata?.directorProject));

function openDirector(): void {
  emit('action', { nodeId: props.node.id, action: 'open-director' });
}
</script>

<style scoped>
.nc-director {
  display: flex;
  width: 100%;
  height: 100%;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 16px;
  box-sizing: border-box;
  overflow: hidden;
  background: var(--canvas-node-fill, #26262b);
  color: var(--text-primary, #ddd);
  text-align: center;
}

.nc-director-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.nc-director-title {
  font-size: 13px;
  font-weight: 600;
}

.nc-director-state {
  font-size: 11px;
  opacity: 0.55;
}

.nc-director-hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  opacity: 0.6;
}

.nc-director-btn {
  margin-top: 4px;
  padding: 5px 14px;
  border: 1px solid var(--canvas-selection, #2f80ff);
  border-radius: 999px;
  background: rgba(47, 128, 255, 0.16);
  color: #cfe0ff;
  font-size: 12px;
  cursor: pointer;
  transition: background 140ms ease;
}

.nc-director-btn:hover {
  background: rgba(47, 128, 255, 0.3);
}
</style>
