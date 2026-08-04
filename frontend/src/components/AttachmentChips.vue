<template>
  <div v-if="items.length" class="attach-chips">
    <div v-for="a in items" :key="a.path" class="attach-chip" :title="a.path">
      <span class="ac-icon">{{ extIcon(a.name) }}</span>
      <span class="ac-text">
        <span class="ac-name">{{ a.name }}</span>
        <span class="ac-meta">{{ (a.ext || 'file').toUpperCase() }}<template v-if="formatFileSize(a.size)"> {{ formatFileSize(a.size) }}</template></span>
      </span>
      <button class="ac-remove" title="移除附件" @click.stop="$emit('remove', a.path)">✕</button>
    </div>
  </div>
</template>

<script setup lang="ts">
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
</style>
