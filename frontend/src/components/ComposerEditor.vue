<template>
  <div
    ref="elRef"
    class="ce-input"
    :class="{ 'is-empty': isEmpty }"
    contenteditable="true"
    role="textbox"
    aria-multiline="true"
    :data-placeholder="placeholder || ''"
    :style="rootStyle"
    @input="onInput"
    @keydown="onKeydown"
    @paste="onPaste"
    @mousedown="onMouseDown"
    @compositionstart="onCompositionStart"
    @compositionend="onCompositionEnd"
    @blur="onBlur"
  ></div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { splitRefSegments, refKindOf, refDisplayName, type RefKind } from '../utils/composerRefs';

/**
 * 富文本输入框：把 @ 引用渲染成真正的胶囊（带图标、整块选中、退格一次全删）。
 *
 * 为什么不是 textarea：textarea 是纯文本控件，既不能给某几个字符加背景，
 * 也不能让「一个引用」成为不可分割的单元。所以这里用 contenteditable，
 * DOM 里只有两种节点——普通文本节点与 `contenteditable="false"` 的胶囊元素，
 * 对外（父组件的 composerText / 后端）始终是 `@{会话名}`、`@路径` 这样的纯文本：
 *
 *   文本 → DOM：splitRefSegments 切段，引用段渲染成胶囊（data-cref 存原始 token）
 *   DOM → 文本：遍历子节点，文本节点取文本、胶囊取 data-cref，逐字符拼回
 *
 * 三条硬约束：
 * 1. 胶囊的原始 token 存在 data-cref 上，DOM 里显示的名字（refDisplayName）只是
 *    标签，任何改标签的行为都不能影响序列化结果。
 * 2. 输入法组词期间不写 DOM、不提交文本（compositionend 才提交），否则会把
 *    组词中的拼音/候选打断。
 * 3. 浏览器可能自己插入 <div>/<br>（例如未被拦截的回车），序列化时按换行兜底，
 *    并在 commit 时做一次「结构规范化」重建，避免脏结构越积越多。
 */
const props = defineProps<{
  modelValue: string;
  placeholder?: string;
  /** 固定高度（px）；null/undefined 走 CSS 最小高度 + 内容撑高 */
  height?: number | null;
  /** 最小高度（px），默认 46（约两行） */
  minHeight?: number;
}>();

const emit = defineEmits<{
  'update:modelValue': [value: string];
  input: [];
  keydown: [event: KeyboardEvent];
  submit: [];
  // 剪贴板里带文件（截图/图片）时交回父组件走附件流程，本组件只管纯文本
  paste: [event: ClipboardEvent];
}>();

const CHIP_CLASS = 'ce-chip';

// 胶囊图标：复用项目图标库里的 path 数据（对话气泡 / 文档）
const CHIP_ICON: Record<RefKind, string> = {
  session: '<path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/>',
  file: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
};

const elRef = ref<HTMLElement | null>(null);
const isEmpty = ref(!props.modelValue);
// 输入法组词中：不重建 DOM、不提交文本，等 compositionend
let composing = false;

const rootStyle = computed(() => {
  const style: Record<string, string> = {};
  if (props.height != null) style.height = `${props.height}px`;
  if (props.minHeight != null) style.minHeight = `${props.minHeight}px`;
  return Object.keys(style).length ? style : undefined;
});

function isChip(node: Node | null | undefined): node is HTMLElement {
  return (
    !!node &&
    node.nodeType === Node.ELEMENT_NODE &&
    (node as HTMLElement).classList.contains(CHIP_CLASS)
  );
}

/** DOM → 文本：文本节点取文本、胶囊取原始 token、br/块级元素按换行兜底 */
function collect(node: Node): string {
  let out = '';
  node.childNodes.forEach(child => {
    if (child.nodeType === Node.TEXT_NODE) {
      out += child.textContent || '';
      return;
    }
    if (isChip(child)) {
      out += child.dataset.cref || '';
      return;
    }
    if (child.nodeName === 'BR') {
      out += '\n';
      return;
    }
    // 浏览器在未拦截的回车下会插块级元素，按「换行 + 内容」处理并在下次
    // 提交时规范化掉
    if (child.nodeName === 'DIV' || child.nodeName === 'P') {
      out += '\n' + collect(child);
      return;
    }
    out += collect(child);
  });
  return out;
}

/** 单个节点在文本坐标里的长度（必须与 collect 的拼接口径一致） */
function nodeLen(node: Node): number {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent || '').length;
  if (isChip(node)) return (node.dataset.cref || '').length;
  if (node.nodeName === 'BR') return 1;
  if (node.nodeName === 'DIV' || node.nodeName === 'P') return 1 + collect(node).length;
  return collect(node).length;
}

function createChip(raw: string): HTMLElement {
  const kind = refKindOf(raw);
  const chip = document.createElement('span');
  chip.className = `${CHIP_CLASS} ${CHIP_CLASS}-${kind}`;
  chip.contentEditable = 'false';
  chip.dataset.cref = raw;
  chip.title = kind === 'session' ? `引用的对话：${refDisplayName(raw)}` : `引用的文件：${refDisplayName(raw)}`;

  const icon = document.createElement('span');
  icon.className = `${CHIP_CLASS}-icon`;
  icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${CHIP_ICON[kind]}</svg>`;

  const label = document.createElement('span');
  label.className = `${CHIP_CLASS}-label`;
  label.textContent = refDisplayName(raw);

  chip.append(icon, label);
  return chip;
}

/** 文本 → DOM（只由外部赋值/规范化调用，输入过程中不调用） */
function render(text: string) {
  const el = elRef.value;
  if (!el) return;
  el.textContent = '';
  const frag = document.createDocumentFragment();
  for (const seg of splitRefSegments(text)) {
    if (seg.kind === 'ref') frag.appendChild(createChip(seg.text));
    else if (seg.text) frag.appendChild(document.createTextNode(seg.text));
  }
  el.appendChild(frag);
  isEmpty.value = text.length === 0;
}

/** 文本坐标（相对整个编辑内容）→ DOM 位置 */
function locatePoint(root: HTMLElement, offset: number): { node: Node; offset: number } {
  let acc = 0;
  const kids = Array.from(root.childNodes);
  for (let i = 0; i < kids.length; i++) {
    const child = kids[i];
    const len = nodeLen(child);
    if (offset <= acc + len) {
      if (child.nodeType === Node.TEXT_NODE) {
        const max = (child.textContent || '').length;
        return { node: child, offset: Math.max(0, Math.min(offset - acc, max)) };
      }
      // 落在胶囊/换行上：定位到该节点之前
      return { node: root, offset: i };
    }
    acc += len;
  }
  return { node: root, offset: kids.length };
}

/** DOM 位置 → 文本坐标 */
function offsetOfPoint(root: Node, container: Node, offset: number): number {
  let acc = 0;
  function walk(node: Node): boolean {
    if (node === container) {
      if (node.nodeType === Node.TEXT_NODE) {
        acc += offset;
        return true;
      }
      const kids = Array.from(node.childNodes);
      for (let i = 0; i < offset && i < kids.length; i++) acc += nodeLen(kids[i]);
      return true;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      acc += (node.textContent || '').length;
      return false;
    }
    if (isChip(node)) {
      acc += (node.dataset.cref || '').length;
      return false;
    }
    if (node.nodeName === 'BR') {
      acc += 1;
      return false;
    }
    for (const child of Array.from(node.childNodes)) {
      if (walk(child)) return true;
    }
    return false;
  }
  walk(root);
  return acc;
}

function clampPoint(point: { node: Node; offset: number }): number {
  const max =
    point.node.nodeType === Node.TEXT_NODE
      ? (point.node.textContent || '').length
      : point.node.childNodes.length;
  return Math.max(0, Math.min(point.offset, max));
}

function textLength(): number {
  const el = elRef.value;
  return el ? collect(el).length : 0;
}

function caretOffset(): number {
  const el = elRef.value;
  if (!el) return 0;
  const sel = document.getSelection();
  if (!sel || !sel.rangeCount) return textLength();
  const range = sel.getRangeAt(0);
  if (!el.contains(range.startContainer)) return textLength();
  return offsetOfPoint(el, range.startContainer, range.startOffset);
}

function setCaretRange(start: number, end: number = start) {
  const el = elRef.value;
  if (!el) return;
  const s = locatePoint(el, start);
  const e = locatePoint(el, end);
  const range = document.createRange();
  try {
    range.setStart(s.node, clampPoint(s));
    range.setEnd(e.node, clampPoint(e));
  } catch {
    return;
  }
  const sel = document.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(range);
}

function selectNode(node: Node) {
  const range = document.createRange();
  range.selectNode(node);
  const sel = document.getSelection();
  if (!sel) return;
  sel.removeAllRanges();
  sel.addRange(range);
}

/** 在光标处插入纯文本（换行用 \n，pre-wrap 下正常成行） */
function insertText(text: string) {
  const el = elRef.value;
  if (!el) return;
  const sel = document.getSelection();
  const inEditor =
    !!sel && sel.rangeCount > 0 && el.contains(sel.getRangeAt(0).commonAncestorContainer);
  if (!inEditor) {
    el.appendChild(document.createTextNode(text));
    setCaretRange(textLength());
    return;
  }
  const range = sel!.getRangeAt(0);
  range.deleteContents();
  const node = document.createTextNode(text);
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  sel!.removeAllRanges();
  sel!.addRange(range);
}

/** 存在浏览器自插的块级/换行结构时重建一次，保持文本↔DOM 双向稳定 */
function normalizeIfNeeded() {
  const el = elRef.value;
  if (!el) return;
  let dirty = false;
  el.childNodes.forEach(child => {
    if (child.nodeType === Node.TEXT_NODE) return;
    if (isChip(child)) return;
    dirty = true;
  });
  if (!dirty) return;
  const offset = caretOffset();
  const text = collect(el);
  render(text);
  setCaretRange(Math.min(offset, text.length));
}

function commit() {
  const el = elRef.value;
  if (!el) return;
  normalizeIfNeeded();
  const text = collect(el);
  isEmpty.value = text.length === 0;
  emit('update:modelValue', text);
  emit('input');
}

function onInput() {
  // 组词期间 DOM 里是拼音/候选，不能提交（同 textarea 的 composing 守卫）
  if (composing) return;
  commit();
}

function onCompositionStart() {
  composing = true;
}

function onCompositionEnd() {
  composing = false;
  commit();
}

function onBlur() {
  // 失焦时把可能的脏结构一次性规范化（不影响输入过程）
  if (!composing) normalizeIfNeeded();
}

function onPaste(e: ClipboardEvent) {
  // 剪贴板里带文件（截图、复制的图片文件）：交回父组件走附件流程，
  // 否则粘贴图片会被这里当纯文本吞掉
  const items = Array.from(e.clipboardData?.items || []);
  if (items.some(item => item.kind === 'file')) {
    emit('paste', e);
    return;
  }
  // 纯文本粘贴：拒绝富文本，外部 span/style 会污染编辑器结构
  e.preventDefault();
  const text = e.clipboardData?.getData('text/plain') ?? '';
  if (!text) return;
  insertText(text.replace(/\r\n?/g, '\n'));
  commit();
}

/** 光标紧邻的胶囊（dir=-1 取光标前一个，dir=1 取后一个） */
function adjacentChip(dir: -1 | 1): HTMLElement | null {
  const el = elRef.value;
  const sel = document.getSelection();
  if (!el || !sel || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  if (!range.collapsed) return null; // 有选区交给浏览器（跨胶囊选区会整块删除）

  const node = dir === -1 ? range.startContainer : range.endContainer;
  const offset = dir === -1 ? range.startOffset : range.endOffset;

  if (node.nodeType === Node.TEXT_NODE) {
    const text = node.textContent || '';
    if (dir === -1 && offset > 0) return null; // 光标在文本中间，正常删字
    if (dir === 1 && offset < text.length) return null;
    const sibling = dir === -1 ? node.previousSibling : node.nextSibling;
    return isChip(sibling) ? sibling : null;
  }
  if (node === el) {
    const kids = Array.from(el.childNodes);
    const target = dir === -1 ? kids[offset - 1] : kids[offset];
    return isChip(target) ? target : null;
  }
  return null;
}

/** 删掉胶囊并把光标放到它原来的位置；返回是否已处理 */
function deleteAdjacentChip(dir: -1 | 1): boolean {
  const chip = adjacentChip(dir);
  const el = elRef.value;
  if (!chip || !el) return false;
  const index = Array.prototype.indexOf.call(el.childNodes, chip);
  chip.remove();
  const range = document.createRange();
  range.setStart(el, Math.max(0, Math.min(index, el.childNodes.length)));
  range.collapse(true);
  const sel = document.getSelection();
  if (sel) {
    sel.removeAllRanges();
    sel.addRange(range);
  }
  commit();
  return true;
}

function onKeydown(e: KeyboardEvent) {
  // 先让父组件跑补全导航与发送判定（Enter 发送由父组件 preventDefault 接管）
  emit('keydown', e);
  if (e.defaultPrevented) {
    commit();
    return;
  }
  const isComposing = composing || e.isComposing;
  if (isComposing) return; // 组词中的 Enter/方向键属于输入法

  if (e.key === 'Enter') {
    e.preventDefault();
    if (e.shiftKey) {
      insertText('\n');
      commit();
    } else {
      emit('submit');
    }
    return;
  }
  if (e.key === 'Backspace' && deleteAdjacentChip(-1)) {
    e.preventDefault();
    return;
  }
  if (e.key === 'Delete' && deleteAdjacentChip(1)) {
    e.preventDefault();
  }
}

/** 点胶囊 → 整块选中（再按退格/Delete 就是整个删掉） */
function onMouseDown(e: MouseEvent) {
  const target = e.target as HTMLElement | null;
  const chip = target?.closest?.(`.${CHIP_CLASS}`) as HTMLElement | null;
  if (!chip) return;
  e.preventDefault();
  elRef.value?.focus();
  selectNode(chip);
}

watch(
  () => props.modelValue,
  value => {
    const el = elRef.value;
    if (!el) return;
    // 自己在 commit 里发出的值不该触发重建（重建会打断光标/输入法）
    if (collect(el) === value) return;
    if (composing) return;
    render(value || '');
  },
);

onMounted(() => render(props.modelValue || ''));

// 供 useSuggest 与父组件使用的鸭子类型控件（与 HTMLTextAreaElement 的三个成员
// 同名，本组件可以替换 textarea 而 useSuggest 一行都不用改）
const control = {
  get value() {
    const el = elRef.value;
    return el ? collect(el) : '';
  },
  get selectionStart() {
    return caretOffset();
  },
  setSelectionRange(start: number, end?: number) {
    setCaretRange(start, end ?? start);
  },
  focus() {
    elRef.value?.focus();
  },
};

defineExpose({ el: elRef, control, insertText, focus: () => elRef.value?.focus() });
</script>

<!-- 非 scoped：胶囊是运行期 createElement 出来的，没有 scoped 的 data-v 标记，
     scoped 样式对其无效。类名统一 ce- 前缀避免污染 -->
<style>
.ce-input {
  min-height: 46px;
  max-height: 100%;
  overflow-y: auto;
  /* 隐藏滚动条：编辑器内滚动条会挤压内容宽度，与输入框视觉不符 */
  scrollbar-width: none;
  outline: none;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  word-break: normal;
  color: var(--text);
  caret-color: var(--text);
  cursor: text;
  font: inherit;
  line-height: inherit;
}
.ce-input::-webkit-scrollbar {
  width: 0;
  height: 0;
}
.ce-input.is-empty::before {
  content: attr(data-placeholder);
  color: var(--text-faint);
  pointer-events: none;
}
/* @ 引用胶囊：带图标的小标签，语义上是「不可分割的一个词」 */
.ce-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 260px;
  padding: 0 7px 0 5px;
  margin: 0 1px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--accent) 26%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 0.94em;
  line-height: 1.45;
  vertical-align: middle;
  white-space: nowrap;
  overflow: hidden;
  cursor: default;
}
.ce-chip-session {
  border-color: color-mix(in srgb, var(--accent) 26%, transparent);
  background: var(--accent-soft);
  color: var(--accent);
}
.ce-chip-file {
  border-color: color-mix(in srgb, var(--accent2) 30%, transparent);
  background: color-mix(in srgb, var(--accent2) 15%, transparent);
  color: var(--accent2);
}
.ce-chip-icon {
  display: inline-flex;
  flex-shrink: 0;
}
.ce-chip-icon svg {
  width: 11px;
  height: 11px;
}
.ce-chip-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>