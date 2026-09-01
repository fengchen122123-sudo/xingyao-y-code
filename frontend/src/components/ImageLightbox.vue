<template>
  <!-- 图片大图查看：点击遮罩或 Esc 关闭，多图支持左右切换；
       滚轮缩放（0.5x–6x）、放大后拖动平移、双击复位。mermaid 图经
       data URL 进 <img> 后按矢量渲染，任意倍率缩放依旧清晰 -->
  <div
    v-if="store.lightbox.show"
    class="lightbox-mask"
    @click="store.closeLightbox()"
    @wheel.prevent="onWheel"
  >
    <button class="lb-close" title="关闭 (Esc)" @click.stop="store.closeLightbox()">
      <AppIcon name="x" :size="16" :stroke-width="2" />
    </button>
    <button
      v-if="store.lightbox.images.length > 1"
      class="lb-nav prev"
      title="上一张 (←)"
      @click.stop="store.lightboxNav(-1)"
    ><AppIcon name="chevron-left" :size="22" :stroke-width="2" /></button>
    <img
      class="lb-img"
      :src="store.lightbox.images[store.lightbox.index]"
      :style="imgStyle"
      alt="查看图片"
      @click.stop
      @dblclick.stop="resetView"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerUp"
    >
    <button
      v-if="store.lightbox.images.length > 1"
      class="lb-nav next"
      title="下一张 (→)"
      @click.stop="store.lightboxNav(1)"
    ><AppIcon name="chevron-right" :size="22" :stroke-width="2" /></button>
    <div v-if="store.lightbox.images.length > 1" class="lb-counter">
      {{ store.lightbox.index + 1 }} / {{ store.lightbox.images.length }}
    </div>
    <div v-if="zoom !== 1" class="lb-zoom">{{ Math.round(zoom * 100) }}%</div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

// 缩放/平移是组件本地视图状态：不进 store，换图或重新打开即复位
const zoom = ref(1);
const panX = ref(0);
const panY = ref(0);
const dragging = ref(false);
let dragStartX = 0;
let dragStartY = 0;
let dragBaseX = 0;
let dragBaseY = 0;

const ZOOM_MIN = 0.5;
const ZOOM_MAX = 6;
const ZOOM_STEP = 1.2;

function resetView() {
  zoom.value = 1;
  panX.value = 0;
  panY.value = 0;
  dragging.value = false;
}

// 乘除 1.2 的浮点误差会让 zoom 回不到精确的 1（角标残留 100%）：
// 四位小数取齐后再 clamp，保证缩放序列可逆、边界值精确
function clampZoom(v: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, Math.round(v * 10000) / 10000));
}

function onWheel(e: WheelEvent) {
  zoom.value = clampZoom(zoom.value * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP));
}

// 只有放大后才允许拖动（zoom=1 时图片居中展示，拖动无意义）；
// 指针捕获保证移出图片仍能继续平移
function onPointerDown(e: PointerEvent) {
  if (zoom.value <= 1) return;
  dragging.value = true;
  dragStartX = e.clientX;
  dragStartY = e.clientY;
  dragBaseX = panX.value;
  dragBaseY = panY.value;
  const el = e.currentTarget as HTMLElement;
  if (el.setPointerCapture) {
    try { el.setPointerCapture(e.pointerId); } catch { /* jsdom 等无活动指针环境忽略 */ }
  }
}

function onPointerMove(e: PointerEvent) {
  if (!dragging.value) return;
  panX.value = dragBaseX + (e.clientX - dragStartX);
  panY.value = dragBaseY + (e.clientY - dragStartY);
}

function onPointerUp() {
  dragging.value = false;
}

// 拖动中禁用 transition，避免平移滞后；滚轮缩放/复位走 0.15s 缓动
const imgStyle = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
  transition: dragging.value ? 'none' : 'transform 0.15s ease',
  cursor: zoom.value > 1 ? (dragging.value ? 'grabbing' : 'grab') : 'default',
}));

// 打开灯箱或左右切换图片时复位视图
watch(() => [store.lightbox.show, store.lightbox.index], resetView);

function onKeydown(e: KeyboardEvent) {
  if (!store.lightbox.show) return;
  if (e.key === 'Escape') store.closeLightbox();
  else if (e.key === 'ArrowLeft') store.lightboxNav(-1);
  else if (e.key === 'ArrowRight') store.lightboxNav(1);
}

onMounted(() => document.addEventListener('keydown', onKeydown));
onUnmounted(() => document.removeEventListener('keydown', onKeydown));
</script>

<style scoped>
.lightbox-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.82);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1500;
  backdrop-filter: blur(3px);
}
.lb-img {
  max-width: 90vw;
  max-height: 88vh;
  object-fit: contain;
  border-radius: 8px;
  box-shadow: 0 20px 80px rgba(0, 0, 0, 0.6);
  /* 放大后允许溢出视口，配合拖动查看局部 */
  user-select: none;
  -webkit-user-drag: none;
}
.lb-close {
  position: absolute;
  top: 18px;
  right: 22px;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  font-size: 16px;
  cursor: pointer;
}
.lb-close:hover {
  background: rgba(255, 255, 255, 0.25);
}
.lb-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: none;
  background: rgba(255, 255, 255, 0.12);
  color: #fff;
  font-size: 26px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
.lb-nav:hover {
  background: rgba(255, 255, 255, 0.25);
}
.lb-nav.prev {
  left: 22px;
}
.lb-nav.next {
  right: 22px;
}
.lb-counter {
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  color: rgba(255, 255, 255, 0.75);
  font-size: 13px;
  background: rgba(0, 0, 0, 0.4);
  border-radius: 12px;
  padding: 3px 12px;
}
.lb-zoom {
  position: absolute;
  bottom: 20px;
  right: 22px;
  color: rgba(255, 255, 255, 0.85);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
  background: rgba(0, 0, 0, 0.4);
  border-radius: 12px;
  padding: 3px 12px;
}
</style>
