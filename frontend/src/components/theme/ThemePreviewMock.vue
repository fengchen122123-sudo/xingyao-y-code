<template>
  <!-- 迷你模拟首页：仅用主题变量着色，不挂载真实组件，作为设置页内的效果预览 -->
  <div class="mock-app" :style="rootStyle" data-test="theme-preview-mock">
    <div class="mock-app-image" :style="appImageStyle"></div>
    <!-- 原生标题栏（DWM 颜色，无图片） -->
    <div class="mock-native" :style="nativeStyle">
      <span class="mock-dot" v-for="n in 3" :key="n"></span>
      <span class="mock-native-title">Y-CODE · 星瑶</span>
    </div>
    <div class="mock-body">
      <div class="mock-sidebar" :style="regionStyle('sidebar')">
        <div class="mock-line" v-for="n in 5" :key="n"></div>
      </div>
      <div class="mock-center">
        <div class="mock-toolbar" :style="regionStyle('conversation_toolbar')">
          <div class="mock-line short"></div>
        </div>
        <div class="mock-workspace" :style="regionStyle('workspace')">
          <div class="mock-bubble"></div>
          <div class="mock-bubble alt"></div>
          <div class="mock-bubble"></div>
        </div>
        <div class="mock-composer" :style="regionStyle('composer')">
          <div class="mock-input"></div>
        </div>
      </div>
      <div class="mock-right" :style="regionStyle('right_panel')">
        <div class="mock-line" v-for="n in 4" :key="n"></div>
      </div>
    </div>
    <div class="mock-statusbar" :style="regionStyle('statusbar')">
      <div class="mock-line short"></div>
    </div>
    <!-- 应用顶栏 -->
    <div class="mock-header" :style="regionStyle('app_header')">
      <div class="mock-logo"></div>
      <div class="mock-line short"></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { ResolvedThemePreview } from '../../themes/runtime';
import type { ThemeRegionId } from '../../themes/types';

const props = defineProps<{ preview: ResolvedThemePreview }>();

const rootStyle = computed(() => ({
  ...props.preview.tokens,
  // 迷你预览用变量着色；accent 等直接引用 token 变量
}));

const appImageStyle = computed(() => ({
  backgroundImage: props.preview.appImage['--theme-app-image'],
  backgroundSize: props.preview.appImage['--theme-app-image-size'],
  backgroundPosition: props.preview.appImage['--theme-app-image-position'],
  opacity: props.preview.appImage['--theme-app-image-opacity'],
}));

const nativeStyle = computed(() => ({
  background: 'var(--theme-background-panel)',
}));

function regionStyle(region: ThemeRegionId) {
  const map = props.preview.regions[region] || {};
  const prefix = `--theme-${region.replace(/_/g, '-')}`;
  return {
    background: `color-mix(in srgb, ${map[`${prefix}-bg`] || 'transparent'} calc(${map[`${prefix}-bg-opacity`] || '1'} * 100%), transparent)`,
    color: map[`${prefix}-text`],
    '--mock-region-image': map[`${prefix}-image`] || 'none',
    '--mock-region-image-opacity': map[`${prefix}-image-opacity`] || '1',
    '--mock-region-image-size': map[`${prefix}-image-size`] || 'cover',
    '--mock-region-image-position': map[`${prefix}-image-position`] || 'center',
    '--mock-region-accent': map[`${prefix}-accent`],
  } as Record<string, string | undefined>;
}
</script>

<style scoped>
.mock-app {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 320px;
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--theme-background-app);
  font-size: 0;
  user-select: none;
  isolation: isolate;
}
.mock-app-image {
  position: absolute;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background-repeat: no-repeat;
}
.mock-native {
  position: relative;
  z-index: 1;
  height: 22px;
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 0 8px;
  flex-shrink: 0;
}
.mock-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--theme-text-faint); opacity: .6; }
.mock-native-title { font-size: 9px; color: var(--theme-text-secondary); margin-left: 6px; }
.mock-body { position: relative; z-index: 1; flex: 1; display: flex; min-height: 0; }
.mock-sidebar { width: 22%; padding: 6px; display: flex; flex-direction: column; gap: 5px; position: relative; }
.mock-center { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.mock-right { width: 24%; padding: 6px; display: flex; flex-direction: column; gap: 5px; position: relative; }
.mock-toolbar { height: 18px; display: flex; align-items: center; padding: 0 8px; flex-shrink: 0; position: relative; }
.mock-workspace { flex: 1; padding: 8px; display: flex; flex-direction: column; gap: 6px; overflow: hidden; position: relative; }
.mock-composer { padding: 6px 8px; flex-shrink: 0; position: relative; }
.mock-statusbar { height: 16px; display: flex; align-items: center; padding: 0 8px; flex-shrink: 0; position: relative; z-index: 1; }
.mock-header { position: absolute; top: 22px; left: 0; right: 0; height: 20px; display: flex; align-items: center; gap: 6px; padding: 0 8px; z-index: 2; }
.mock-logo { width: 12px; height: 12px; border-radius: 4px; background: var(--mock-region-accent, var(--theme-accent)); }
.mock-line { height: 6px; border-radius: 3px; background: currentColor; opacity: .25; }
.mock-line.short { width: 40%; }
.mock-bubble { height: 16px; border-radius: 6px; background: var(--theme-background-card); border: 1px solid var(--theme-border); }
.mock-bubble.alt { width: 70%; align-self: flex-end; background: var(--theme-background-elevated); }
.mock-input { height: 18px; border-radius: 6px; background: var(--theme-background-card); border: 1px solid var(--theme-border); }

/* 分区背景图：各分区用伪元素垫在内容下，变量由 regionStyle 注入 */
.mock-sidebar::before, .mock-toolbar::before, .mock-workspace::before,
.mock-composer::before, .mock-right::before, .mock-statusbar::before, .mock-header::before {
  content: '';
  position: absolute;
  inset: 0;
  z-index: -1;
  pointer-events: none;
  background-image: var(--mock-region-image);
  background-size: var(--mock-region-image-size);
  background-position: var(--mock-region-image-position);
  background-repeat: no-repeat;
  opacity: var(--mock-region-image-opacity);
}
.mock-sidebar, .mock-toolbar, .mock-workspace, .mock-composer, .mock-right, .mock-statusbar, .mock-header {
  isolation: isolate;
}
</style>
