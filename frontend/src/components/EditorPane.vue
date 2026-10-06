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
import { changeDocument, lspExtensions, openDocument } from '../editor/lsp';
import { lspNavigationExtensions } from '../editor/lspNavigation';
import { samePath, useIdeStore } from '../stores/ideStore';

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
  const languageId = store.languageId(current.path);
  // LSP 扩展按文件实例化：补全源、诊断 lint 源、视图注册（诊断到达时 forceLinting 要用）
  // + 导航类（悬停 / F12 跳转定义 / Shift-Alt-f 格式化 / F2 重命名）。
  // 跳转只写 store.jumpTo：编辑器视图的定位由下方 pendingJump watcher 独占负责，
  // 这里绝不自己 dispatch selection（S1 冻结契约：定位菜谱只有跳转请求的消费者那份）。
  const state = await createEditorState(
    current.content,
    languageId,
    [
      ...lspExtensions({ path: current.path, languageId }),
      ...lspNavigationExtensions({
        path: current.path,
        onJump: (locations) => {
          const first = locations[0];
          if (!first) return;
          // openFile 在 !res.ok（越权 / 读失败）时会 throw，jumpTo 内部也会再 openFile
          // 一次：跳转失败必须静默回落，绝不让 rejected promise 逃逸成 unhandled
          // rejection（评审 M-4）。定位仍由 store.jumpTo 与下方待跳转 watcher 独占负责，
          // 这里绝不自己动编辑器视图。
          void store
            .openFile(first.path)
            .then(() => store.jumpTo(first.path, first.line, first.character))
            .catch(() => {});
        },
        requestNewName: (oldName) => window.prompt('重命名为', oldName),
      }),
    ],
  );
  view?.destroy();
  view = new EditorView({
    state,
    parent: hostRef.value,
    extensions: [
      buildEditorTheme(isLightTheme()),
      EditorView.updateListener.of((update) => {
        if (!update.docChanged || applyingExternal) return;
        const text = update.state.doc.toString();
        store.updateContent(props.path, text);
        // 文档同步走防抖（CHANGE_DEBOUNCE_MS），不会因为一次击键打一次桥调用
        changeDocument(current.path, text);
      }),
    ],
  });
  // 打开/切换标签后把文档登记到语言服务器：拿不到服务器（未配置 / 越权）时
  // openDocument 内部静默降级为纯文本，不会打断编辑器
  void openDocument(current.path, languageId, current.content);
  // 视图此刻才真正就绪：消费可能在 createEditorState 等待期间到达的待跳转请求
  //（否则会被上一个 watch 因 `view === null` 跳过，之后再无触发点 → 静默丢失）
  consumePendingJump();
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
    // 外部改动（磁盘重载 / 「以磁盘为准」）不经过 docChanged 监听，必须单独同步给
    // 语言服务器：否则服务端文档比编辑器旧，诊断会一直停在旧内容上。
    const current = store.tab(props.path);
    if (current) changeDocument(current.path, next);
  },
);

// 跳转定义 / 诊断定位：把 store 里的 pendingJump 落到编辑器选区。
// 路径比较走 store 的同一查表口径（`tab()` = 精确 + 分隔符统一，且认 openFile 记录过的
// 调用方原路径）——原先的严格 `!==` 会让 S2 用原始路径写下的跳转永远不被消费
// （标签一直挂着待跳转），也会与本组件的 `store.tab(props.path)` 口径打架。
//
// 视图未就绪（view 为 null）时**只保留、不丢弃**：IdeLayout 的
// `:key="ide.activePath()"` 会在 openFile 后重建 EditorPane —— 旧实例卸载、新实例的
// view 要等 createEditorState（异步）就绪，而 onJump 的 `openFile().then(jumpTo)`
// 恰好在这个窗口里写入 pendingJump。早先的写法在这里 `return` 既不定位也不
// clearPendingJump：跳转静默丢失，残留的请求还会被后续跳转错误消费。现在改由
// mountView 在 view 就绪后调用 consumePendingJump() 兜底消费；`immediate: true`
// 则覆盖「watch 注册前就已写入」的情况（非 immediate 不会评估初始值）。
function consumePendingJump() {
  const jump = store.pendingJump;
  if (!view || !jump) return;
  // 两侧都先按 store 的查表解析成权威路径（realpath），再比同一口径的键
  const target = store.tab(jump.path)?.path ?? jump.path;
  const current = store.tab(props.path)?.path ?? props.path;
  if (!samePath(target, current)) return;
  const total = view.state.doc.lines;
  const lineInfo = view.state.doc.line(Math.min(Math.max(jump.line, 1), total));
  const anchor = Math.min(lineInfo.from + Math.max(jump.character - 1, 0), lineInfo.to);
  view.dispatch({ selection: { anchor }, scrollIntoView: true });
  view.focus();
  store.clearPendingJump();
}

watch(() => store.pendingJump, () => consumePendingJump(), { immediate: true });

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
