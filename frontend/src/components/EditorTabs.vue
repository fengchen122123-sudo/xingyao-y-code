<template>
  <div class="editor-tabs">
    <div
      v-for="tab in store.tabs()"
      :key="tab.path"
      class="tab"
      :class="{ active: tab.path === store.activePath() }"
      :title="tab.path"
      @click="store.activate(tab.path)"
    >
      <span class="tab-name">{{ baseName(tab.path) }}</span>
      <span v-if="tab.dirty" class="tab-dot" aria-label="未保存"></span>
      <span class="tab-close" @click.stop="store.closeTab(tab.path)">×</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useIdeStore } from '../stores/ideStore';

const store = useIdeStore();

function baseName(path: string) {
  const parts = path.split(/[\\/]/);
  return parts[parts.length - 1] || path;
}
</script>

<style scoped>
.editor-tabs {
  display: flex;
  align-items: stretch;
  gap: 2px;
  overflow-x: auto;
  background: var(--bg1);
  border-bottom: 1px solid var(--border);
  scrollbar-width: thin;
}

.tab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px 5px 12px;
  font-size: 12px;
  color: var(--text-dim);
  border-right: 1px solid var(--border);
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}

.tab:hover {
  background: var(--bg2);
  color: var(--text);
}

.tab.active {
  background: var(--bg2);
  color: var(--text-bright, var(--text));
  box-shadow: inset 0 -2px 0 var(--accent);
}

.tab-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
}

.tab-close {
  padding: 0 3px;
  border-radius: 4px;
  color: var(--text-faint);
}

.tab-close:hover {
  background: var(--bg3);
  color: var(--text);
}
</style>
