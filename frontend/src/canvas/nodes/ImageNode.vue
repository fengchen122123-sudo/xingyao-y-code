<template>
  <div class="nc-image" data-test="node-content-image">
    <img
      v-if="src && !broken"
      class="nc-image-el"
      :src="src"
      :alt="node.title || '图片'"
      draggable="false"
      data-test="node-image-el"
      @error="broken = true"
    />
    <div v-else class="nc-placeholder" data-test="node-content-placeholder">
      <span class="nc-placeholder-label">空图片节点</span>
    </div>
  </div>
</template>

<script setup lang="ts">
// ImageNode（M3-T01）：展示 metadata.content（media_server URL）或空占位。
// 缩略图/LOD/全分辨率切换属 M5 素材层，这里只做「有 URL 就画、坏图退占位」。
import { computed, ref, watch } from 'vue';

import type { CanvasNodeData } from '../types';

defineOptions({ name: 'ImageNode' });

const props = defineProps<{ node: CanvasNodeData }>();

const src = computed(() => props.node.metadata?.content || '');
/** 加载失败（URL 失效/后端未就绪）退占位，不把破图图标留在画布上 */
const broken = ref(false);

watch(src, () => {
  broken.value = false;
});
</script>

<style scoped>
.nc-image {
  position: relative;
  display: flex;
  width: 100%;
  height: 100%;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.nc-image-el {
  width: 100%;
  height: 100%;
  object-fit: contain;
  /* 图片节点用图片本身当框内背景，外壳边框仍然可见 */
  background: transparent;
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
</style>
