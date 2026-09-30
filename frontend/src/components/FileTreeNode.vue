<template>
  <div>
    <div class="file-row" :class="{ open: isOpen }" @click="toggle">
      <span class="fi">
        <AppIcon v-if="node.d" name="chevron-right" :size="12" :class="{ 'is-open': isOpen }" />
      </span>
      <span class="ficon">
        <AppIcon v-if="node.d" :name="isOpen ? 'folder-open' : 'folder'" :size="14" />
        <template v-else>{{ icon }}</template>
      </span>
      <span class="fname">{{ node.n }}</span>
    </div>
    <div v-if="node.d && isOpen" class="file-children">
      <div v-if="loading" class="file-loading">加载中…</div>
      <FileTreeNode
        v-for="child in node.children"
        :key="child.n"
        :node="child"
        :parent-path="fullPath"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import AppIcon from './AppIcon.vue';
import type { FileNode } from '../types/pywebview';
import { useAppStore } from '../stores/appStore';
import { extIcon } from '../utils/fileIcon';
import { canOpenInRightPanel } from '../utils/localPath';
import { useIsMobile } from '../composables/useViewport';

const props = defineProps<{
  node: FileNode;
  parentPath?: string;
}>();

const store = useAppStore();
// 移动端（<=768px）右面板整体隐藏：HTML 文件点击只能回退源码预览弹窗
const isMobile = useIsMobile();
// FileNode 本身只带文件名(n)，需要沿递归路径拼接出完整路径供预览接口使用
const fullPath = computed(() => {
  return props.parentPath ? props.parentPath + '\\' + props.node.n : props.node.n;
});
// 展开状态持久化在 store：文件树刷新后新组件实例也能凭路径恢复展开
const isOpen = ref(store.fileTreeExpanded.includes(fullPath.value));
const loading = ref(false);
const loaded = ref(props.node.children !== undefined);

// 目录图标已由模板中的 AppIcon（folder/folder-open）渲染，这里只算文件类型徽标
const icon = computed(() => (props.node.d ? '' : extIcon(props.node.n)));

async function toggle() {
  if (!props.node.d) {
    // html / pdf / md 交给右栏（网页直接渲染，md 先渲染成预览页）；
    // 移动端没有右面板，一律回退源码预览弹窗
    if (canOpenInRightPanel(fullPath.value) && !isMobile.value) {
      void store.openInRightPanel(fullPath.value);
      return;
    }
    store.openFilePreview(fullPath.value);
    return;
  }
  isOpen.value = !isOpen.value;
  // 同步到 store 的展开记录：刷新文件树时据此恢复展开并逐层刷新内容
  if (isOpen.value) {
    if (!store.fileTreeExpanded.includes(fullPath.value)) {
      store.fileTreeExpanded.push(fullPath.value);
    }
  } else {
    store.fileTreeExpanded = store.fileTreeExpanded.filter(p => p !== fullPath.value);
  }
  // 初始文件树有深度上限，超出层级的目录没有预载 children；
  // 展开这类目录时按需向后端取下一层，任意层级都能展开。
  // 空目录 children 同样缺省，取回空数组后用 loaded 标记避免重复请求。
  if (isOpen.value && !loaded.value && !loading.value && props.node.children === undefined) {
    loading.value = true;
    try {
      const api = (window as any).pywebview?.api;
      const res = await api?.get_dir_children(fullPath.value);
      props.node.children = (res && res.children) || [];
    } catch {
      props.node.children = [];
    } finally {
      loading.value = false;
      loaded.value = true;
    }
  }
}
</script>

<style scoped>
.file-row {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 4px 8px;
  padding-right: 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--text-dim);
  /* 行宽跟随内容（深层缩进 + 全名），超出由面板底部横向滚动条查看；
     min-width 保证浅层行的悬停底色仍铺满面板 */
  width: max-content;
  min-width: 100%;
}
.file-row:hover {
  background: var(--bg2);
  color: var(--text);
}
.file-row .fi {
  width: 15px;
  display: inline-flex;
  justify-content: center;
  flex-shrink: 0;
}
.file-row .fi .is-open {
  transform: rotate(90deg);
}
.file-row .ficon {
  width: 18px;
  display: inline-flex;
  justify-content: center;
  font-size: 13px;
  flex-shrink: 0;
}
.file-row .fname {
  /* 完整文件名不截断，超出部分用横向滚动条查看 */
  white-space: nowrap;
}
.file-children {
  margin-left: 16px;
  border-left: 1px solid var(--border);
  padding-left: 6px;
}
.file-loading {
  padding: 3px 8px;
  font-size: 12px;
  color: var(--text-faint);
}
</style>
