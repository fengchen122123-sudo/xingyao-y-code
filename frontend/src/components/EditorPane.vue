<template>
  <div class="editor-pane">
    <div v-if="tab?.externalChange && tab?.dirty" class="external-bar">
      该文件已被星瑶修改
      <button type="button" @click="onReload">以磁盘为准</button>
      <button type="button" @click="onKeepMine">保留我的内容</button>
    </div>
    <div v-if="tab?.conflict" class="external-bar conflict">
      保存冲突：磁盘内容已变
      <button type="button" @click="onReload">以磁盘为准</button>
      <button type="button" @click="onOverwrite">用我的覆盖</button>
    </div>
    <div ref="hostRef" class="editor-host"></div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';

import { buildEditorTheme } from '../editor/theme';
import { createEditorState, EditorView } from '../editor/setup';
import { useIdeStore } from '../stores/ideStore';

const props = defineProps<{ path: string }>();
const store = useIdeStore();
const hostRef = ref<HTMLElement | null>(null);
let view: EditorView | null = null;
let applyingExternal = false;

const tab = computed(() => store.tab(props.path));

function isLightTheme() {
  return document.body.classList.contains('light');
}

async function mountView() {
  if (!hostRef.value || !props.path) return;
  const current = store.tab(props.path);
  if (!current) return;
  const state = await createEditorState(current.content, store.languageId(props.path));
  view?.destroy();
  view = new EditorView({
    state,
    parent: hostRef.value,
    extensions: [
      buildEditorTheme(isLightTheme()),
      EditorView.updateListener.of((update) => {
        if (!update.docChanged || applyingExternal) return;
        store.updateContent(props.path, update.state.doc.toString());
      }),
    ],
  });
}

onMounted(() => {
  void mountView();
  window.addEventListener('keydown', onKeydown, true);
});

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown, true);
  view?.destroy();
  view = null;
});

watch(
  () => props.path,
  () => void mountView(),
);

// 外部改动或重载后，把 store 的内容同步进编辑器
watch(
  () => tab.value?.content,
  (next) => {
    if (!view || next === undefined) return;
    if (next === view.state.doc.toString()) return;
    applyingExternal = true;
    view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: next } });
    applyingExternal = false;
  },
);

// 跳转定义 / 诊断定位：把 store 里的 pendingJump 落到编辑器选区
watch(
  () => store.pendingJump,
  (jump) => {
    if (!view || !jump || jump.path !== props.path) return;
    const total = view.state.doc.lines;
    const lineInfo = view.state.doc.line(Math.min(Math.max(jump.line, 1), total));
    const anchor = Math.min(lineInfo.from + Math.max(jump.character - 1, 0), lineInfo.to);
    view.dispatch({ selection: { anchor }, scrollIntoView: true });
    view.focus();
    store.clearPendingJump();
  },
);

function onKeydown(event: KeyboardEvent) {
  if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 's') return;
  event.preventDefault();
  void store.saveTab(props.path);
}

function onReload() {
  void store.reloadFromDisk(props.path);
}

function onKeepMine() {
  const current = store.tab(props.path);
  if (current) current.externalChange = false;
}

function onOverwrite() {
  const current = store.tab(props.path);
  if (!current) return;
  current.hash = '';
  void store.saveTab(props.path);
}
</script>

<style scoped>
.editor-pane {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.editor-host {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.external-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  font-size: 12px;
  background: color-mix(in srgb, var(--accent) 12%, var(--bg1));
  border-bottom: 1px solid var(--border);
}

.external-bar.conflict {
  background: color-mix(in srgb, var(--red, #d9534f) 14%, var(--bg1));
}

.external-bar button {
  background: var(--bg2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 2px 8px;
  cursor: pointer;
}
</style>
