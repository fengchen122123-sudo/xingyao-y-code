<template>
  <div>
    <div class="file-row" :class="{ open: isOpen }" @click="toggle">
      <span class="fi">{{ node.d ? (isOpen ? '▾' : '▸') : '' }}</span>
      {{ node.d ? '📂 ' + node.n : '📄 ' + node.n }}
    </div>
    <div v-if="node.d && isOpen" class="file-children">
      <FileTreeNode 
        v-for="child in node.children" 
        :key="child.n" 
        :node="child" 
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue';
import type { FileNode } from '../types/pywebview';
import { useAppStore } from '../stores/appStore';

const props = defineProps<{
  node: FileNode;
}>();

const store = useAppStore();
const isOpen = ref(false);

function toggle() {
  if (props.node.d) {
    isOpen.value = !isOpen.value;
  } else {
    store.showToast('选中文件: ' + props.node.n);
  }
}
</script>

<style scoped>
.file-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-dim);
}
.file-row:hover {
  background: var(--bg2);
  color: var(--text);
}
.file-row .fi {
  width: 15px;
  text-align: center;
  font-size: 12px;
}
.file-children {
  margin-left: 16px;
  border-left: 1px solid var(--border);
  padding-left: 6px;
}
</style>
