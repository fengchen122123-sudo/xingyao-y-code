<template>
  <!-- 图片大图查看：点击遮罩或 Esc 关闭，多图支持左右切换 -->
  <div v-if="store.lightbox.show" class="lightbox-mask" @click="store.closeLightbox()">
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
      alt="查看图片"
      @click.stop
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
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

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
</style>
