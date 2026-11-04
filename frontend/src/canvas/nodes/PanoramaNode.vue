<template>
  <div class="nc-panorama" data-test="node-content-panorama">
    <img
      v-if="src && !broken"
      class="nc-panorama-el"
      :src="src"
      :alt="node.title || '全景图'"
      draggable="false"
      data-test="node-panorama-el"
      @error="broken = true"
    />
    <div v-else class="nc-placeholder" data-test="node-content-placeholder">
      <span class="nc-placeholder-label">空全景图节点</span>
    </div>
    <!-- 等距柱状投影角标：真正可拖拽观看的球面查看器属 M6 全景链路 -->
    <span class="nc-panorama-badge">全景 2:1</span>
  </div>
</template>

<script setup lang="ts">
// PanoramaNode（M3-T01）：2:1 等距柱状投影图展示态。
// 参考仓用 CanvasPanoramaViewer 做球面拖拽；本任务只要求展示态，
// 先按普通图片铺满（默认尺寸 340×170 就是 2:1），查看器 M6 接。
import { computed, ref, watch } from 'vue';

import type { CanvasNodeData } from '../types';

defineOptions({ name: 'PanoramaNode' });

const props = defineProps<{ node: CanvasNodeData }>();

const src = computed(() => props.node.metadata?.content || '');
const broken = ref(false);

watch(src, () => {
  broken.value = false;
});
</script>

<style scoped>
.nc-panorama {
  position: relative;
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.nc-panorama-el {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.nc-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  color: var(--text-secondary, #888);
}

.nc-placeholder-label {
  font-size: 11px;
  letter-spacing: 0.18em;
  opacity: 0.6;
}

.nc-panorama-badge {
  position: absolute;
  right: 8px;
  top: 8px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.45);
  color: #cfd6e4;
  font-size: 10px;
  letter-spacing: 0.08em;
  pointer-events: none;
}
</style>
