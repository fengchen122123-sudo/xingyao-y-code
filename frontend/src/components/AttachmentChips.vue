<template>
  <div v-if="items.length" class="attach-chips">
    <div v-for="a in items" :key="a.path" class="attach-chip" :class="{ loading: a.loading }" :title="a.loading ? a.name + '（路径加载中…）' : a.path">
      <span class="ac-icon">{{ extIcon(a.name) }}</span>
      <span class="ac-text">
        <span class="ac-name">{{ a.name }}</span>
        <span class="ac-meta">{{ a.loading ? '加载中…' : (a.ext || 'file').toUpperCase() }}<template v-if="!a.loading && formatFileSize(a.size)"> {{ formatFileSize(a.size) }}</template></span>
      </span>
      <span v-if="a.loading" class="ac-progress"></span>
      <button class="ac-remove" title="移除附件" @click.stop="$emit('remove', a.path)">
        <AppIcon name="x" :size="11" :stroke-width="2.25" />
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import AppIcon from './AppIcon.vue';
import type { AttachmentItem } from '../composables/useAttachments';
import { extIcon, formatFileSize } from '../utils/fileIcon';

defineProps<{ items: AttachmentItem[] }>();
defineEmits<{ (e: 'remove', path: string): void }>();
</script>

<style scoped>
.attach-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
.attach-chip {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 6px 22px 6px 8px;
  max-width: 230px;
  transition: border-color 0.15s;
}
.attach-chip:hover {
  border-color: var(--text-faint);
}
.ac-icon {
  font-size: 17px;
  flex-shrink: 0;
  width: 24px;
  text-align: center;
}
.ac-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: 1.3;
}
.ac-name {
  font-size: 12px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ac-meta {
  font-size: 10.5px;
  color: var(--text-faint);
}
.ac-remove {
  position: absolute;
  top: 3px;
  right: 4px;
  width: 15px;
  height: 15px;
  border: none;
  background: transparent;
  color: var(--text-faint);
  font-size: 9px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  padding: 0;
}
.ac-remove:hover {
  background: var(--bg3);
  color: var(--text);
}
/* 拖拽后立即上屏的加载态：chip 底部的不确定进度条 */
.attach-chip.loading {
  border-color: var(--accent);
  padding-bottom: 9px;
}
.ac-progress {
  position: absolute;
  left: 8px;
  right: 8px;
  bottom: 3px;
  height: 2px;
  border-radius: 1px;
  background: var(--bg3);
  overflow: hidden;
}
.ac-progress::after {
  content: '';
  position: absolute;
  top: 0;
  width: 40%;
  height: 100%;
  border-radius: 1px;
  background: var(--accent);
  animation: ac-progress-slide 1s ease-in-out infinite;
}
@keyframes ac-progress-slide {
  0% { left: -40%; }
  100% { left: 100%; }
}
</style>
