<template>
  <div class="nc-video" data-test="node-content-video">
    <video
      v-if="src"
      class="nc-video-el"
      :src="src"
      controls
      preload="metadata"
      playsinline
      data-test="node-video"
      @pointerdown.stop
    ></video>
    <div v-else class="nc-placeholder" data-test="node-content-placeholder">
      <span class="nc-placeholder-label">空视频节点</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// VideoNode（M3-T01）：原生 <video controls> 播放 metadata.content。
// 首帧截取/播放进度/静音预览属 M6 视频链路，这里只做展示态。
import { computed } from 'vue';

import type { CanvasNodeData } from '../types';

defineOptions({ name: 'VideoNode' });

const props = defineProps<{ node: CanvasNodeData }>();

const src = computed(() => props.node.metadata?.content || '');
</script>

<style scoped>
.nc-video {
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: #000;
}

.nc-video-el {
  width: 100%;
  height: 100%;
  object-fit: contain;
  background: #000;
}

.nc-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  background: var(--canvas-node-fill, #26262b);
  color: var(--text-secondary, #888);
}

.nc-placeholder-label {
  font-size: 11px;
  letter-spacing: 0.18em;
  opacity: 0.6;
}
</style>
