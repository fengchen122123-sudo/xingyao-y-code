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
          v-for="(line, li) in hunk.lines"
          :key="li"
          class="git-diff-line"
          :class="lineClass(line)"
        >{{ line }}</div>
      </div>
      <div v-if="truncated" class="git-diff-note">大 Diff 已截断显示</div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { GitDiffDto } from '../types/pywebview.d';

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
.git-line-add { background: rgba(76, 175, 80, 0.18); color: #7ec97e; }
.git-line-del { background: rgba(244, 67, 54, 0.15); color: #e57373; }
.git-line-hunk { color: #64b5f6; }
.git-diff-line {
  white-space: pre-wrap;
  word-break: break-all;
  padding: 0 10px;
  line-height: 1.45;
}
</style>
