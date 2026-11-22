<template>
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    :stroke-linecap="linecap"
    :stroke-linejoin="linejoin"
    aria-hidden="true"
    v-html="path"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { ICON_PATHS } from './iconPaths';

const props = withDefaults(
  defineProps<{
    name: string;
    size?: number;
    strokeWidth?: number;
    linecap?: 'round' | 'butt' | 'square';
    linejoin?: 'round' | 'miter' | 'bevel';
  }>(),
  { size: 16, strokeWidth: 1.75, linecap: 'round', linejoin: 'round' },
);

const path = computed(() => ICON_PATHS[props.name] ?? '');
</script>

<!-- 非 scoped：基线对齐与旋转动画是全局工具类，供各组件复用 -->
<style>
.app-icon {
  display: inline-block;
  vertical-align: -0.15em;
  flex-shrink: 0;
  /* 加粗描边时避免贴边被 viewBox 裁掉 */
  overflow: visible;
}
.app-icon.icon-spin {
  animation: app-icon-rotate 1s linear infinite;
}
@keyframes app-icon-rotate {
  to {
    transform: rotate(360deg);
  }
}
@media (prefers-reduced-motion: reduce) {
  .app-icon.icon-spin {
    animation: none;
  }
}
</style>
