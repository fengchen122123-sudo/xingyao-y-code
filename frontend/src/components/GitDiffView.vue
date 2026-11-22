<template>
  <div class="git-diff" data-test="git-diff">
    <div v-if="!diff" class="git-diff-empty">选择文件查看 Diff</div>
    <template v-else>
      <div v-if="diff.is_binary" class="git-diff-note" data-test="git-diff-binary">
        二进制文件：仅显示元数据，不生成文本 Diff
      </div>
      <div v-else-if="visibleHunks.length === 0" class="git-diff-note">无差异</div>
      <div v-for="(hunk, hi) in visibleHunks" :key="hi" class="git-diff-hunk">
        <div class="git-diff-hunk-header">
          @{{ '@' }} -{{ hunk.old_start }},{{ hunk.old_lines }} +{{ hunk.new_start }},{{ hunk.new_lines }} @@
          <span v-if="hunk.header" class="git-hunk-context">{{ hunk.header }}</span>
        </div>
        <div
          v-for="(row, li) in hunkRows(hunk)"
          :key="li"
          class="git-diff-line"
          :class="row.cls"
        >
          <span class="git-ln git-ln-old">{{ row.oldNo }}</span>
          <span class="git-ln git-ln-new">{{ row.newNo }}</span>
          <span class="git-code">{{ row.text }}</span>
        </div>
      </div>
      <div v-if="truncated" class="git-diff-note">大 Diff 已截断显示（仅展示前 2000 行）</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { GitDiffDto } from '../types/pywebview.d';

interface DiffRow {
  oldNo: number | '';
  newNo: number | '';
  text: string;
  cls: string;
}

const props = defineProps<{ diff: GitDiffDto | null }>();

const MAX_LINES = 2000;

const visibleHunks = computed(() => {
  if (!props.diff) return [];
  const out: typeof props.diff.hunks = [];
  let budget = MAX_LINES;
  for (const hunk of props.diff.hunks) {
    if (budget <= 0) break;
    const lines = budget >= hunk.lines.length ? hunk.lines : hunk.lines.slice(0, budget);
    budget -= lines.length;
    out.push(lines.length === hunk.lines.length ? hunk : { ...hunk, lines });
  }
  return out;
});

const truncated = computed(() => {
  if (!props.diff) return false;
  const total = props.diff.hunks.reduce((n, h) => n + h.lines.length, 0);
  return total > MAX_LINES;
});

function lineClass(line: string): string {
  if (line.startsWith('+')) return 'git-line-add';
  if (line.startsWith('-')) return 'git-line-del';
  if (line.startsWith('@@')) return 'git-line-hunk';
  return 'git-line-ctx';
}

function hunkRows(hunk: { old_start: number; new_start: number; lines: string[] }): DiffRow[] {
  let oldNo = hunk.old_start;
  let newNo = hunk.new_start;
  const rows: DiffRow[] = [];
  for (const line of hunk.lines) {
    let oldCol: number | '' = '';
    let newCol: number | '' = '';
    if (line.startsWith('-')) {
      oldCol = oldNo++;
    } else if (line.startsWith('+')) {
      newCol = newNo++;
    } else if (line.startsWith('\\')) {
    } else {
      oldCol = oldNo++;
      newCol = newNo++;
    }
    rows.push({ oldNo: oldCol, newNo: newCol, text: line, cls: lineClass(line) });
  }
  return rows;
}
</script>

<style scoped>
.git-diff {
  font-family: var(--mono, Consolas, 'Courier New', monospace);
  font-size: 12px;
  overflow: auto;
  flex: 1;
  background: var(--code-bg, var(--theme-code-background));
}
.git-diff-empty,
.git-diff-note {
  color: var(--xy-text-3, var(--text-dim, #888));
  padding: 24px 14px;
  text-align: center;
  font-family: var(--sans, inherit);
}
.git-diff-hunk + .git-diff-hunk {
  margin-top: 2px;
}
.git-diff-hunk-header {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  background: color-mix(in srgb, var(--xy-accent, var(--accent)) 10%, var(--code-bg, var(--bg1)));
  color: var(--xy-accent, var(--accent));
  border-top: 1px solid var(--xy-border, var(--border));
  border-bottom: 1px solid var(--xy-border, var(--border));
  padding: 4px 12px;
  font-size: 11.5px;
  user-select: none;
}
.git-hunk-context {
  color: var(--xy-text-3, var(--text-faint));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.git-diff-line {
  display: flex;
  align-items: stretch;
  line-height: 1.58;
}
.git-line-ctx .git-code {
  color: var(--xy-text-2, var(--text-dim));
}
.git-line-add {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 14%, transparent));
  box-shadow: inset 3px 0 0 var(--xy-green, var(--green));
  color: var(--xy-green, var(--green));
}
.git-line-del {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 14%, transparent));
  box-shadow: inset 3px 0 0 var(--xy-rose, var(--red));
  color: var(--xy-rose, var(--red));
}
.git-line-hunk {
  color: var(--xy-accent, var(--accent));
}
.git-ln {
  flex: 0 0 3.6em;
  text-align: right;
  padding: 0 7px;
  color: var(--xy-text-3, var(--text-faint, #666));
  opacity: 0.72;
  user-select: none;
  font-variant-numeric: tabular-nums;
}
.git-ln-new {
  border-right: 1px solid var(--xy-border, var(--border));
  margin-right: 10px;
}
.git-code {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-all;
  padding-right: 14px;
}
</style>
