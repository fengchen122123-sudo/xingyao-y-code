<template>
  <div class="nc-audio" data-test="node-content-audio">
    <div class="nc-audio-head">
      <span class="nc-audio-title">{{ node.title || '音频' }}</span>
      <span v-if="durationLabel" class="nc-audio-duration" data-test="node-audio-duration">{{ durationLabel }}</span>
    </div>
    <audio
      v-if="src"
      class="nc-audio-el"
      :src="src"
      controls
      preload="metadata"
      data-test="node-audio"
      @pointerdown.stop
    ></audio>
    <div v-else class="nc-placeholder" data-test="node-content-placeholder">
      <span class="nc-placeholder-label">空音频节点</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// AudioNode（M3-T01）：原生 <audio controls> 播放 metadata.content。
// 波形/裁剪/变速（参考仓 CanvasAudioPlayer）属 M6 音频链路，这里只做展示态。
import { computed } from 'vue';

import type { CanvasNodeData } from '../types';

defineOptions({ name: 'AudioNode' });

const props = defineProps<{ node: CanvasNodeData }>();

const src = computed(() => props.node.metadata?.content || '');

function formatDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}

/** 时长优先取素材真实时长（mediaDurationMs），没有就不显示，不猜 */
const durationLabel = computed(() => {
  const ms = props.node.metadata?.mediaDurationMs;
  return typeof ms === 'number' && ms > 0 ? formatDuration(ms) : '';
});
</script>

<style scoped>
.nc-audio {
  display: flex;
  width: 100%;
  height: 100%;
  flex-direction: column;
  justify-content: center;
  gap: 10px;
  padding: 14px 16px;
  box-sizing: border-box;
  overflow: hidden;
  background: var(--canvas-node-fill, #26262b);
  color: var(--text-primary, #ddd);
}

.nc-audio-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-width: 0;
}

.nc-audio-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  opacity: 0.8;
}

.nc-audio-duration {
  flex: 0 0 auto;
  font-size: 11px;
  opacity: 0.55;
}

.nc-audio-el {
  width: 100%;
  height: 32px;
}

.nc-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 1 1 auto;
  color: var(--text-secondary, #888);
}

.nc-placeholder-label {
  font-size: 11px;
  letter-spacing: 0.18em;
  opacity: 0.6;
}
</style>
