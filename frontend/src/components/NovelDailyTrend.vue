<template>
  <div class="novel-daily">
    <div v-if="bars.length > 0" class="novel-daily-chart" data-test="novel-daily-chart">
      <div v-for="bar in bars" :key="bar.date" class="novel-daily-col">
        <div class="novel-daily-bars">
          <div
            class="novel-daily-bar"
            :class="{ 'is-zero': bar.zero }"
            data-test="novel-daily-bar"
            :style="{ height: `${bar.height}px` }"
            :title="`${bar.date}：${bar.words} 字`"
          ></div>
        </div>
        <span class="novel-daily-label">{{ bar.label }}</span>
      </div>
    </div>
    <div v-if="showEmpty" class="novel-daily-empty" data-test="novel-daily-empty">
      近 7 天没有新增字数
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { NovelWorkbenchDailyDto } from '../types/pywebview.d';

const props = defineProps<{
  rows: NovelWorkbenchDailyDto[];
}>();

const CHART_HEIGHT = 72;

function toInt(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function shortDate(date: string): string {
  const text = String(date ?? '');
  return text.length >= 10 ? text.slice(5, 10) : text;
}

const bars = computed(() => {
  const max = props.rows.reduce((acc, row) => Math.max(acc, toInt(row.words)), 0);
  return props.rows.map((row) => {
    const words = toInt(row.words);
    // 最大值为 0 时全组都是 0：统一压到 2px 的最小可视高度，避免整图消失
    const height = words > 0 && max > 0 ? Math.max(2, Math.round((words / max) * CHART_HEIGHT)) : 2;
    return { date: String(row.date ?? ''), words, height, zero: words === 0, label: shortDate(row.date) };
  });
});

const totalWords = computed(() => props.rows.reduce((acc, row) => acc + toInt(row.words), 0));

const showEmpty = computed(() => bars.value.length === 0 || totalWords.value === 0);
</script>

<style scoped>
.novel-daily {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.novel-daily-chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
}

.novel-daily-col {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}

.novel-daily-bars {
  width: 100%;
  height: 72px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
}

.novel-daily-bar {
  width: 100%;
  max-width: 22px;
  min-height: 2px;
  border-radius: 3px 3px 0 0;
  background: var(--accent);
}
.novel-daily-bar.is-zero {
  background: var(--border);
}

.novel-daily-label {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 10.5px;
  line-height: 1.3;
  white-space: nowrap;
}

.novel-daily-empty {
  color: var(--text-faint);
  font-size: 11.5px;
}
</style>
