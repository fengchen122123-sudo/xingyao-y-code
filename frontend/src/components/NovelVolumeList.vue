<template>
  <!-- 卷纲列表：窄面板里一行一卷，点击后由父组件去读该卷卷纲文档 -->
  <div v-if="rows.length === 0" class="novel-volume-empty" data-test="novel-volume-empty">
    大纲目录还没有卷纲文件
  </div>
  <div v-else class="novel-volume-list">
    <div
      v-for="row in rows"
      :key="row.no"
      class="novel-volume-row"
      :class="{ 'is-current': row.current }"
      data-test="novel-volume-row"
      role="button"
      tabindex="0"
      @click="emit('select', row)"
      @keydown.enter.prevent="emit('select', row)"
      @keydown.space.prevent="emit('select', row)"
    >
      <span v-if="row.current" class="novel-volume-current">当前卷</span>
      <span class="novel-volume-name">{{ row.name }}</span>
      <span class="novel-volume-chars">{{ charsOf(row) }} 字</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { NovelWorkbenchVolumeDto } from '../types/pywebview.d';

defineProps<{
  /** 卷纲表单项：来自 get_novel_workbench 的 volumes 数组（缺失时父组件传空数组） */
  rows: NovelWorkbenchVolumeDto[];
}>();

const emit = defineEmits<{
  /** 点击某卷：父组件据此读取该卷卷纲文档（只读，不改状态） */
  (e: 'select', volume: NovelWorkbenchVolumeDto): void;
}>();

function charsOf(row: NovelWorkbenchVolumeDto): number {
  const n = Number(row.chars);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}
</script>

<style scoped>
/* 颜色/字号只用全局 CSS 变量，跟随主题切换 */
.novel-volume-empty {
  padding: 10px 2px;
  color: var(--text-faint);
  font-size: 12px;
}

.novel-volume-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.novel-volume-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 6px;
  border-radius: 5px;
  font-size: 12px;
  cursor: pointer;
}
.novel-volume-row:hover {
  background: var(--accent-soft);
}
.novel-volume-row:focus-visible {
  outline: none;
  background: var(--accent-soft);
}

.novel-volume-current {
  padding: 0 5px;
  border: 1px solid var(--accent);
  border-radius: 999px;
  color: var(--accent);
  font-size: 10.5px;
  line-height: 1.5;
}

.novel-volume-name {
  flex: 1;
  min-width: 0;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.novel-volume-chars {
  flex-shrink: 0;
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 11.5px;
}
</style>
