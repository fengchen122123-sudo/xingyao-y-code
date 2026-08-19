<template>
  <!-- 内联 SVG 图标：统一 lucide 风格（24 viewBox / 圆角线帽 / currentColor 描边），
       尺寸由 size 控制，颜色跟随父级 color，与文本基线对齐 -->
  <svg
    class="app-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
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
    /** 渲染尺寸（px），默认 16 */
    size?: number;
    /** 描边宽度，默认 1.75（24 viewBox 下小尺寸仍清晰） */
    strokeWidth?: number;
  }>(),
  { size: 16, strokeWidth: 1.75 },
);

const path = computed(() => ICON_PATHS[props.name] ?? '');
</script>

<!-- 非 scoped：基线对齐与旋转动画是全局工具类，供各组件复用 -->
<style>
.app-icon {
  display: inline-block;
  vertical-align: -0.15em;
  flex-shrink: 0;
}
/* 加载中旋转（配合 loader 弧线图标） */
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
