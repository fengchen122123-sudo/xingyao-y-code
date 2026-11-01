<template>
  <!-- 设定文档只读预览浮层：标题栏 + 纯文本内容区，不做编辑 -->
  <div class="novel-doc-viewer" data-test="novel-doc-viewer">
    <div class="novel-doc-head">
      <span class="novel-doc-title">{{ title }}</span>
      <span v-if="doc" class="novel-doc-chars">{{ charsText }}</span>
      <button class="novel-doc-close" data-test="novel-doc-close" title="关闭" @click="emit('close')">
        <AppIcon name="x" :size="13" />
      </button>
    </div>

    <div class="novel-doc-body">
      <div v-if="loading" class="novel-doc-loading">
        <AppIcon name="loader" :size="12" class="icon-spin" />
        <span>正在读取…</span>
      </div>
      <div v-else-if="error" class="novel-doc-error" data-test="novel-doc-error">{{ error }}</div>
      <pre v-else class="novel-doc-text" data-test="novel-doc-text">{{ doc?.text || '' }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import AppIcon from './AppIcon.vue';
import type { NovelDocReadDto } from '../types/pywebview.d';

const props = defineProps<{
  /** 浮层标题：父组件传文档名（通常是相对路径或表名） */
  title: string;
  /** read_novel_doc 返回结果：null 表示还没读（内容区为空文本） */
  doc: NovelDocReadDto | null;
  /** 是否正在读取：为真时显示 loader 提示 */
  loading: boolean;
  /** 读取失败文本，非空时按 --red 显示（优先级高于正文） */
  error: string;
}>();

const emit = defineEmits<{
  /** 关闭浮层：父组件把当前文档状态清空 */
  (e: 'close'): void;
}>();

// 字数与 novel_status 同口径，由后端给出；缺失时按 0 显示不猜测
const charsText = computed(() => {
  const n = Number(props.doc?.chars);
  return `${Number.isFinite(n) && n > 0 ? Math.floor(n) : 0} 字`;
});
</script>

<style scoped>
/* 颜色/字号只用全局 CSS 变量，跟随主题切换 */
.novel-doc-viewer {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
  overflow: hidden;
}

.novel-doc-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}
.novel-doc-title {
  flex: 1;
  min-width: 0;
  color: var(--text-bright);
  font-size: 12.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.novel-doc-chars {
  flex-shrink: 0;
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 10.5px;
}
.novel-doc-close {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 2px;
  border: none;
  border-radius: 4px;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
}
.novel-doc-close:hover {
  color: var(--text-bright);
  background: var(--accent-soft);
}

.novel-doc-body {
  padding: 8px;
  min-height: 0;
}

.novel-doc-loading {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-dim);
  font-size: 12px;
}

.novel-doc-error {
  color: var(--red);
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}

.novel-doc-text {
  margin: 0;
  max-height: 360px;
  overflow-y: auto;
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
