<template>
  <div class="cfp">
    <button class="cfp-head" type="button" :aria-expanded="open" @click="open = !open">
      <AppIcon class="cfp-ico" name="file" :size="14" />
      <span class="cfp-title">{{ changes.length }} 个文件已更改</span>
      <span class="cfp-stats">
        <span class="cfp-add">+{{ totalAdded }}</span>
        <span class="cfp-del">-{{ totalRemoved }}</span>
      </span>
      <AppIcon
        class="cfp-caret"
        :class="{ 'is-open': open }"
        name="chevron-right"
        :size="12"
        :stroke-width="2"
      />
    </button>
    <div v-show="open" class="cfp-body">
      <div v-for="c in changes" :key="c.path" class="cfp-item">
        <!-- 文件行：点击切换展开/收起该文件的改动详情 -->
        <div
          class="cfp-row"
          role="button"
          tabindex="0"
          :aria-expanded="expandedPath === c.path"
          :title="expandedPath === c.path ? '收起改动详情' : '查看改动详情'"
          @click="toggleExpand(c.path)"
          @keydown.enter.prevent="toggleExpand(c.path)"
        >
          <AppIcon class="cfp-row-ico" name="file" :size="13" />
          <span class="cfp-row-name">{{ c.name }}</span>
          <span class="cfp-row-path">{{ c.path }}</span>
          <span class="cfp-row-stats">
            <span class="cfp-add">+{{ c.added }}</span>
            <span class="cfp-del">-{{ c.removed }}</span>
          </span>
          <button
            class="cfp-open-btn"
            type="button"
            :title="`在默认程序中打开 ${c.path}`"
            @click.stop="openFile(c.path)"
          ><AppIcon name="external-link" :size="12" /></button>
          <AppIcon
            class="cfp-row-caret"
            :class="{ 'is-open': expandedPath === c.path }"
            name="chevron-right"
            :size="11"
            :stroke-width="2"
          />
        </div>

        <!-- 展开：该文件的改动 diff（红删/绿增，改动行上下各 5 行，带真实行号） -->
        <div v-if="expandedPath === c.path" class="cfp-diff">
          <template v-for="(block, bi) in diffBlocks(c.diffText)" :key="bi">
            <div v-if="bi > 0 || block.topHidden > 0" class="cfp-diff-gap">⋯</div>
            <div
              v-for="(l, li) in block.lines"
              :key="li"
              class="cfp-diff-line"
              :class="`d-${l.kind}`"
            >
              <span class="cfp-no">{{ l.oldNo }}</span>
              <span class="cfp-no">{{ l.newNo }}</span>
              <span class="cfp-sign">{{ l.kind === 'add' ? '+' : l.kind === 'del' ? '-' : ' ' }}</span>
              <span class="cfp-code">{{ l.content }}</span>
            </div>
            <div v-if="block.bottomHidden > 0" class="cfp-diff-gap">⋯</div>
          </template>
          <div v-if="!c.diffText" class="cfp-diff-empty">无可用改动详情</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import type { FileChangeEntry } from '../utils/turnGroups';
import { buildDiffBlocks, type DiffViewBlock } from '../utils/diffView';

const props = defineProps<{
  changes: FileChangeEntry[];
}>();

const store = useAppStore();
const open = ref(false);
// 当前展开查看 diff 的文件路径；同一时间只展开一个，再次点击收起
const expandedPath = ref('');

const totalAdded = computed(() => props.changes.reduce((s, c) => s + c.added, 0));
const totalRemoved = computed(() => props.changes.reduce((s, c) => s + c.removed, 0));

function diffBlocks(diffText: string): DiffViewBlock[] {
  return buildDiffBlocks(diffText, 5);
}

function toggleExpand(path: string) {
  expandedPath.value = expandedPath.value === path ? '' : path;
}

async function openFile(path: string) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !path) return;
  try {
    const res = await api.open_path(path);
    if (res && res.ok === false && res.msg) {
      store.notifyModal('打开失败', res.msg);
    }
  } catch (err) {
    store.notifyModal('打开失败', String(err));
  }
}
</script>

<style scoped>
.cfp {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg2);
  overflow: hidden;
}
.cfp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 12px;
  background: none;
  border: none;
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-dim);
  text-align: left;
}
.cfp-head:hover {
  background: var(--bg3);
}
.cfp-head:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.cfp-ico {
  flex-shrink: 0;
  color: var(--text-faint);
}
.cfp-title {
  font-weight: 600;
  color: var(--text);
}
.cfp-stats {
  margin-left: auto;
  display: flex;
  gap: 8px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.cfp-caret {
  flex-shrink: 0;
  color: var(--text-faint);
  transition: transform 0.15s ease;
}
.cfp-caret.is-open {
  transform: rotate(90deg);
}
.cfp-body {
  border-top: 1px solid var(--border);
  padding: 4px 0;
}
.cfp-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 12px;
  user-select: none;
}
.cfp-row:hover {
  background: var(--bg3);
}
.cfp-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.cfp-row-ico {
  flex-shrink: 0;
  color: var(--text-faint);
}
.cfp-row-name {
  font-weight: 600;
  color: var(--text);
  flex-shrink: 0;
}
.cfp-row-path {
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.cfp-row-stats {
  margin-left: auto;
  display: flex;
  gap: 6px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.cfp-open-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 4px;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 0;
}
.cfp-open-btn:hover {
  background: var(--bg1);
  color: var(--accent);
}
.cfp-row-caret {
  flex-shrink: 0;
  color: var(--text-faint);
  transition: transform 0.15s ease;
}
.cfp-row-caret.is-open {
  transform: rotate(90deg);
}
.cfp-add {
  color: var(--green);
}
.cfp-del {
  color: var(--red);
}

/* 展开的 diff 视图 */
.cfp-diff {
  margin: 0 8px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
  overflow-x: auto;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.5;
  padding: 4px 0;
}
.cfp-diff-line {
  display: flex;
  white-space: pre;
}
.cfp-no {
  width: 3.2em;
  flex-shrink: 0;
  text-align: right;
  padding-right: 0.6em;
  color: var(--text-faint);
  opacity: 0.75;
  user-select: none;
}
.cfp-sign {
  width: 1.2em;
  flex-shrink: 0;
  text-align: center;
  user-select: none;
}
.cfp-code {
  flex: 1;
  padding-right: 12px;
}
.cfp-diff-line.d-add {
  background: rgba(78, 169, 106, 0.13);
}
.cfp-diff-line.d-add .cfp-sign,
.cfp-diff-line.d-add .cfp-code {
  color: var(--green);
}
.cfp-diff-line.d-del {
  background: rgba(217, 99, 91, 0.13);
}
.cfp-diff-line.d-del .cfp-sign,
.cfp-diff-line.d-del .cfp-code {
  color: var(--red);
}
.cfp-diff-line.d-ctx .cfp-code {
  color: var(--text-dim);
}
.cfp-diff-gap {
  padding: 1px 0 1px 8.6em;
  color: var(--text-faint);
  user-select: none;
}
.cfp-diff-empty {
  padding: 8px 12px;
  color: var(--text-faint);
}
@media (prefers-reduced-motion: reduce) {
  .cfp-caret,
  .cfp-row-caret {
    transition: none;
  }
}
</style>
