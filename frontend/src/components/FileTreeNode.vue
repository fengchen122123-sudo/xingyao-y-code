<template>
  <div>
    <div class="file-row" :class="{ open: isOpen, active: isActiveFile }" @click="toggle">
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
import { useIdeStore } from '../stores/ideStore';
import { extIcon } from '../utils/fileIcon';
import { canOpenInRightPanel } from '../utils/localPath';
import { useIsMobile } from '../composables/useViewport';

// 可在编辑器里打开的文本/代码扩展名（与 frontend/src/editor/setup.ts 的扩展名表同一口径）
const EDITABLE_EXT = new Set([
  'py', 'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'json', 'md', 'html', 'htm',
  'css', 'txt', 'log', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'sql', 'sh',
  'bat', 'ps1', 'go', 'rs', 'java', 'c', 'h', 'cpp', 'hpp', 'cs', 'php', 'rb',
]);

function isEditableText(name: string): boolean {
  const dot = name.lastIndexOf('.');
  if (dot < 0) return false;
  return EDITABLE_EXT.has(name.slice(dot + 1).toLowerCase());
}

const props = defineProps<{
  node: FileNode;
  parentPath?: string;
}>();

const store = useAppStore();
const ide = useIdeStore();
// 移动端（<=768px）右面板整体隐藏：HTML 文件点击只能回退源码预览弹窗
const isMobile = useIsMobile();
// FileNode 本身只带文件名(n)，需要沿递归路径拼接出完整路径供预览接口使用
const fullPath = computed(() => {
  return props.parentPath ? props.parentPath + '\\' + props.node.n : props.node.n;
});
// 选中态（设计稿 .row.sel）：当前活动标签所在行铺 --accent-soft 底色。
// 用 ideStore.tab() 做判等而不是字符串直接比：活动标签记的是后端 realpath，
// 而这里拼出来的是调用方路径，两者在大小写 / 分隔符上可能不同（tab() 是仓库里
// 唯一的路径查表口径，两种形态都能命中）。
const isActiveFile = computed(() => {
  const active = ide.activePath();
  if (!active || props.node.d) return false;
  return ide.tab(fullPath.value)?.path === active;
});
// 展开状态持久化在 store：文件树刷新后新组件实例也能凭路径恢复展开
const isOpen = ref(store.fileTreeExpanded.includes(fullPath.value));
const loading = ref(false);
const loaded = ref(props.node.children !== undefined);

// 目录图标已由模板中的 AppIcon（folder/folder-open）渲染，这里只算文件类型徽标
const icon = computed(() => (props.node.d ? '' : extIcon(props.node.n)));

async function toggle() {
  if (!props.node.d) {
    // IDE 模式下，代码与文本文件直接在编辑器打开；图片等非文本仍走既有预览分流。
    // 必须插在下方既有分流之前，且只接管可编辑文本类型，保证对话模式零回归。
    if (store.ideMode() && isEditableText(props.node.n)) {
      void ide.openFile(fullPath.value);
      return;
    }
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
  /* 行高按设计稿 .row 定 24px（原来由字体行高决定，约 25.5px） */
  min-height: 24px;
  padding: 2px 8px;
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
/* 选中态（设计稿 .row.sel）：活动标签所在文件行 */
.file-row.active {
  background: var(--accent-soft);
  color: var(--text-bright);
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
  /* 每层缩进 14px（含 1px 竖向引导线），对齐设计稿 .row 的 padding-left 递进 */
  margin-left: 10px;
  border-left: 1px solid var(--border);
  padding-left: 4px;
}
.file-loading {
  padding: 3px 8px;
  font-size: 12px;
  color: var(--text-faint);
}
</style>
