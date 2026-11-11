<template>
  <!-- 伏笔追踪列表：右面板很窄，用行列表而非 HTML 表格；两个超期维度各自独立标记 -->
  <div v-if="displayRows.length === 0" class="novel-fore-empty" data-test="novel-foreshadow-empty">
    追踪里还没有伏笔记录
  </div>
  <div v-else class="novel-fore-list">
    <div
      v-for="item in displayRows"
      :key="item.row.id"
      class="novel-fore-row"
      data-test="novel-foreshadow-row"
    >
      <div class="novel-fore-head">
        <span class="novel-fore-id">{{ item.row.id }}</span>
        <span class="novel-fore-badge" :class="statusClass(item.row.status)">
          {{ item.row.status || '未知状态' }}
        </span>
        <!-- overdue 与 planned_overdue 语义不同（体检超期 / 承诺回收点已错过），可同时出现 -->
        <span v-if="item.row.overdue" class="novel-fore-flag overdue">体检超期</span>
        <span v-if="item.row.planned_overdue" class="novel-fore-flag planned">计划超期</span>
      </div>
      <div class="novel-fore-summary">{{ item.row.summary || '（无摘要）' }}</div>
      <div v-if="item.meta.length > 0" class="novel-fore-meta">{{ item.meta.join(' · ') }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { NovelWorkbenchForeshadowDto } from '../types/pywebview.d';

const props = defineProps<{
  /** 伏笔表单项：来自 get_novel_workbench 的 foreshadows 数组（缺失时父组件传空数组） */
  rows: NovelWorkbenchForeshadowDto[];
}>();

function toInt(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

// 状态徽章配色：已埋=黄（进行中）、已回收=绿、已过期=红，放弃与其他状态一律灰，
// 状态文案原样展示（后端枚举中文，未知值不猜测含义）
function statusClass(status: string): string {
  switch (String(status || '').trim()) {
    case '已埋':
      return 'is-active';
    case '已回收':
      return 'is-resolved';
    case '已过期':
      return 'is-expired';
    default:
      return 'is-dim';
  }
}

// 第三行小字：只列已知维度，值为 0 或缺失的项不显示（章号未知 ≠ 第 0 章）
function metaOf(row: NovelWorkbenchForeshadowDto): string[] {
  const parts: string[] = [];
  const planted = toInt(row.planted_chapter);
  if (planted > 0) parts.push(`埋设第${planted}章`);
  const planned = toInt(row.planned_resolution_chapter);
  if (planned > 0) parts.push(`计划第${planned}章回收`);
  const stale = toInt(row.stale_chapters);
  if (stale > 0) parts.push(`搁置${stale}章`);
  return parts;
}

const displayRows = computed(() => props.rows.map((row) => ({ row, meta: metaOf(row) })));
</script>

<style scoped>
/* 颜色/字号只用全局 CSS 变量，跟随主题，不引入新 UI 依赖 */
.novel-fore-empty {
  padding: 10px 2px;
  color: var(--text-faint);
  font-size: 12px;
}

.novel-fore-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 300px;
  overflow-y: auto;
  padding-right: 2px;
}

.novel-fore-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}

.novel-fore-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.novel-fore-id {
  color: var(--text-bright);
  font-family: var(--mono);
  font-size: 12px;
  font-weight: 600;
}

.novel-fore-badge {
  padding: 0 5px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 10.5px;
  line-height: 1.5;
}
.novel-fore-badge.is-active {
  color: var(--yellow);
}
.novel-fore-badge.is-resolved {
  color: var(--green);
}
.novel-fore-badge.is-expired {
  color: var(--red);
}
.novel-fore-badge.is-dim {
  color: var(--text-dim);
}

.novel-fore-flag {
  font-size: 10.5px;
  line-height: 1.5;
  padding: 0 4px;
  border-radius: 3px;
}
.novel-fore-flag.overdue {
  color: var(--red);
  background: var(--bg);
}
.novel-fore-flag.planned {
  color: var(--yellow);
  background: var(--bg);
}

.novel-fore-summary {
  color: var(--text);
  font-size: 12px;
  line-height: 1.55;
  word-break: break-word;
}

.novel-fore-meta {
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.5;
  word-break: break-word;
}
</style>
