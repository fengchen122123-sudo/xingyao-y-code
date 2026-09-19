<template>
  <div class="git-diff" data-test="git-diff">
    <div v-if="!diff" class="git-diff-empty">选择文件查看 Diff</div>
    <template v-else>
      <div v-if="diff.is_binary" class="git-diff-note" data-test="git-diff-binary">
        二进制文件：仅显示元数据，不生成文本 Diff
      </div>
      <div v-else-if="visibleHunks.length === 0" class="git-diff-note">无差异</div>
      <div v-for="(hunk, hi) in visibleHunks" :key="hi" class="git-diff-hunk">
        <div class="git-diff-hunk-header">@{{ '@' }} -{{ hunk.old_start }},{{ hunk.old_lines }} +{{ hunk.new_start }},{{ hunk.new_lines }} @@ {{ hunk.header }}</div>
        <div
          v-for="(row, li) in hunkRows(hunk)"
          :key="li"
          class="git-diff-line"
          :class="row.cls"
        ><span class="git-ln">{{ row.oldNo }}</span><span class="git-ln">{{ row.newNo }}</span><span class="git-code">{{ row.text }}</span></div>
      </div>
      <div v-if="truncated" class="git-diff-note">大 Diff 已截断显示</div>
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

// 大 Diff 截断：超过 2000 行只渲染前 2000 行并明确提示（§5.3 截断提示要求）
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
  return '';
}

// 统一 diff 行号推进：- 行只走旧文件行号，+ 行只走新文件行号，
// 上下文行两侧同时推进；"\ No newline" 标记不占行号
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
      // "\ No newline at end of file" 标记：不占行号
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
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  overflow: auto;
  flex: 1;
}
.git-diff-empty,
.git-diff-note {
  color: var(--text-dim, #888);
  padding: 8px 10px;
}
.git-diff-hunk-header {
  background: rgba(127, 127, 127, 0.15);
  color: var(--text-dim, #888);
  padding: 2px 10px;
  user-select: none;
}
.git-line-add { background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green); }
.git-line-del { background: color-mix(in srgb, var(--red) 14%, transparent); color: var(--red); }
.git-line-hunk { color: var(--accent); }
.git-diff-line {
  display: flex;
  align-items: baseline;
  line-height: 1.45;
}
.git-ln {
  flex: 0 0 4em;
  text-align: right;
  padding: 0 6px;
  color: var(--text-faint, #666);
  opacity: 0.8;
  user-select: none;
  font-variant-numeric: tabular-nums;
}
.git-code {
  flex: 1;
  white-space: pre-wrap;
  word-break: break-all;
  padding-right: 10px;
}
</style>
