<template>
  <div data-test="token-statistics-page" class="token-statistics-page" :aria-busy="store.tokenStatisticsLoading">
    <header class="usage-header">
      <div>
        <div class="eyebrow">USAGE ANALYTICS</div>
        <h1>Token 统计</h1>
        <p>与 CLI /usage 共用真实服务端用量，按模型查看输入、输出与缓存命中。</p>
      </div>
    </header>

    <div v-if="store.tokenStatisticsLoading && !statistics" class="usage-loading" role="status" aria-live="polite">
      <span class="loading-orbit"></span>
      <p>正在读取本地 Token 统计…</p>
    </div>

    <template v-else>
      <!-- 累计摘要条：数据来自累计（全量）lifetime 快照，无数据时占位 -- -->
      <section class="summary-strip" data-test="token-summary-strip" aria-label="累计用量概览">
        <div v-for="item in lifetimeItems" :key="item.label" class="summary-strip-item" data-test="token-summary-item">
          <div class="summary-strip-value">{{ item.value }}</div>
          <div class="summary-strip-label">{{ item.label }}</div>
        </div>
      </section>

      <!-- 热力图卡片 -->
      <section v-if="heatmap" class="usage-section heatmap-card" aria-label="Token 活动热力图">
        <div class="section-heading">
          <h2>Token 活动<span class="heatmap-hint">点格子看当天明细</span></h2>
          <div class="pill-tabs" role="group" aria-label="热力图聚合方式">
            <button
              v-for="option in heatmapModes"
              :key="option.value"
              :data-test="`heatmap-mode-${option.value}`"
              :class="{ active: heatmapMode === option.value }"
              :aria-pressed="heatmapMode === option.value"
              @click="heatmapMode = option.value"
            >{{ option.label }}</button>
          </div>
        </div>
        <div class="heatmap-grid">
          <div
            v-for="column in heatmap.columns"
            :key="column.key"
            class="heatmap-col"
            :title="column.title || undefined"
            data-test="heatmap-column"
          >
            <button
              v-for="cell in column.cells"
              :key="cell.date"
              type="button"
              class="heatmap-cell"
              :class="[`lvl-${cell.level}`, { 'has-usage': cell.level > 0, 'is-selected': isCellSelected(column, cell) }]"
              :title="cell.title"
              :aria-pressed="isCellSelected(column, cell)"
              data-test="heatmap-cell"
              @click="toggleCell(column, cell)"
            ></button>
          </div>
        </div>
        <div class="heatmap-grid heatmap-months">
          <span
            v-for="label in heatmap.monthLabels"
            :key="label.key"
            class="heatmap-month"
            :style="{ gridColumn: `span ${label.span}` }"
          >{{ label.label }}</span>
        </div>
      </section>

      <!-- 点热力图格子后就地展开的明细：折线排行 + 占比环图 -->
      <TokenUsageDayDetail
        v-if="selectionDetail"
        :detail="selectionDetail"
        :mode="heatmapMode === 'daily' ? 'day' : 'week'"
        @close="clearSelection"
      />

      <!-- 范围切换 -->
      <div class="range-row">
        <div class="range-row-title">Token 用量</div>
        <div class="pill-tabs" role="group" aria-label="统计时间范围">
          <button
            v-for="option in ranges"
            :key="option.days"
            :data-test="`range-${option.days}`"
            :class="{ active: store.tokenStatisticsDays === option.days }"
            :aria-pressed="store.tokenStatisticsDays === option.days"
            @click="selectRange(option.days)"
          >{{ option.label }}</button>
        </div>
      </div>

      <!-- 每日模型用量趋势 -->
      <section class="usage-section" aria-label="每日模型用量">
        <div class="section-heading">
          <h2>每日模型用量</h2>
        </div>
        <template v-if="trend.hasData">
          <div class="trend-legend" role="list">
            <span
              v-for="series in trend.series"
              :key="series.key"
              class="trend-legend-item"
              role="listitem"
              data-test="trend-legend-item"
            >
              <i :style="{ background: series.color }"></i>{{ series.label }}
            </span>
          </div>
          <svg class="trend-svg" viewBox="0 0 720 240" width="100%" role="img" aria-label="每日模型用量趋势">
            <line
              v-for="(y, index) in trendGridLines"
              :key="`grid-${index}`"
              class="trend-grid-line"
              :x1="TREND_PAD_LEFT"
              :x2="TREND_PAD_LEFT + TREND_PLOT_WIDTH"
              :y1="y"
              :y2="y"
              stroke-dasharray="4 4"
            />
            <g :transform="trendLineTransform">
              <path
                v-for="series in trend.series"
                :key="series.key"
                data-test="trend-line"
                :d="series.path"
                :stroke="series.color"
                stroke-width="2"
                fill="none"
                stroke-linejoin="round"
              />
            </g>
            <g class="trend-columns">
              <rect
                v-for="(day, index) in trendDays"
                :key="day.date"
                data-test="trend-col"
                :x="trendColumnX(index)"
                :y="TREND_PAD_TOP"
                :width="trendBandWidth"
                :height="TREND_PLOT_HEIGHT"
                fill="transparent"
              >
                <title>{{ `${dayLabel(day.date)} · 总计 ${formatTokenCount(day.total)} Token` }}</title>
              </rect>
            </g>
            <template v-for="(label, index) in trend.labels" :key="`axis-${index}`">
              <text
                v-if="showAxisLabel(index)"
                class="trend-axis-label"
                :x="trendColumnX(index) + trendBandWidth / 2"
                y="230"
                text-anchor="middle"
              >{{ label }}</text>
            </template>
          </svg>
        </template>
        <div v-else class="chart-empty">暂无用量数据</div>
      </section>

      <!-- 模型用量环图 -->
      <section class="usage-section" aria-label="模型用量">
        <div class="section-heading">
          <h2>模型用量</h2>
        </div>
        <div v-if="donutRows.length" class="donut-grid">
          <div class="donut-wrap">
            <svg class="donut-svg" viewBox="0 0 120 120" role="img" aria-label="模型用量占比">
              <circle
                v-for="slice in donutSlices.slices"
                :key="slice.key"
                cx="60"
                cy="60"
                r="48"
                fill="none"
                :stroke="slice.color"
                stroke-width="16"
                :stroke-dasharray="donutDashArray(slice, DONUT_CIRCUMFERENCE)"
                :stroke-dashoffset="donutDashOffset(slice, DONUT_CIRCUMFERENCE)"
                transform="rotate(-90 60 60)"
              />
            </svg>
            <div class="donut-center">
              <strong>{{ formatTokenCount(donutSlices.total) }}</strong>
              <span>Token</span>
            </div>
          </div>
          <div class="model-list" role="list">
            <div v-for="row in donutRows" :key="row.key" class="model-row" role="listitem" data-test="model-usage-row">
              <span class="model-dot" :style="{ background: row.color }"></span>
              <span class="model-name">{{ row.label }}</span>
              <span class="model-share">{{ formatShare(row.share) }}</span>
              <span class="model-meta">{{ row.meta }}</span>
            </div>
          </div>
        </div>
        <div v-else class="chart-empty">暂无模型用量</div>
      </section>

      <div class="usage-footer">
        <button data-test="token-refresh" class="refresh-button" @click="refreshAll">
          <span :class="{ spinning: store.tokenStatisticsLoading || store.tokenStatisticsLifetimeLoading }">↻</span> 刷新
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import TokenUsageDayDetail from './TokenUsageDayDetail.vue';
import type {
  HeatmapDayCell,
  HeatmapMode,
  HeatmapWeekColumn,
  LifetimeSummary,
} from '../utils/tokenStatistics';
import {
  buildDonutSlices,
  buildHeatmapMonthLabels,
  buildHeatmapWeeks,
  buildLifetimeSummary,
  buildLinePath,
  buildSelectionDetail,
  buildTrendChart,
  donutDashArray,
  donutDashOffset,
  formatDayFull,
  formatTokenCount,
} from '../utils/tokenStatistics';

const store = useAppStore();
const statistics = computed(() => store.tokenStatistics);

const HEATMAP_WEEK_COUNT = 52;
const TREND_WIDTH = 720;
const TREND_HEIGHT = 240;
const TREND_PAD_LEFT = 12;
const TREND_PAD_TOP = 12;
const TREND_PAD_BOTTOM = 34;
const TREND_PLOT_WIDTH = TREND_WIDTH - TREND_PAD_LEFT * 2;
const TREND_PLOT_HEIGHT = TREND_HEIGHT - TREND_PAD_TOP - TREND_PAD_BOTTOM;
const DONUT_RADIUS = 48;
const DONUT_CIRCUMFERENCE = 2 * Math.PI * DONUT_RADIUS;
// 4 条水平虚线：0、1/3、2/3、1 处的绘图区高度。
const trendGridLines = [0, 1, 2, 3].map(step => TREND_PAD_TOP + (step / 3) * TREND_PLOT_HEIGHT);

const ranges = [
  { days: 7 as const, label: '近 7 天' },
  { days: 30 as const, label: '近 30 天' },
];

const heatmapModes: { value: HeatmapMode; label: string }[] = [
  { value: 'daily', label: '每天' },
  { value: 'weekly', label: '每周' },
  { value: 'cumulative', label: '累计' },
];
const heatmapMode = ref<HeatmapMode>('daily');

// 选中的热力图格子：daily 模式落到具体一天，weekly / cumulative 模式整列（自然周）为一格。
const selection = ref<{ columnKey: string; date: string | null } | null>(null);

// 模式切换后列 key 前缀已变，清掉旧选中态，避免留下对不上列的无主明细。
watch(heatmapMode, () => { selection.value = null; });

// 摘要条：lifetime 快照缺省时统一显示 --。
const lifetimeItems = computed(() => {
  const snapshot = store.tokenStatisticsLifetime;
  const summary = snapshot ? buildLifetimeSummary(snapshot.days) : null;
  const fromSummary = (render: (data: LifetimeSummary) => string) => (
    summary ? render(summary) : '--'
  );
  return [
    { label: '总 Token', value: fromSummary(data => formatTokenCount(data.totalTokens)) },
    {
      label: '单日峰值',
      value: summary?.peakDay ? formatTokenCount(summary.peakDay.total) : '--',
    },
    { label: '活跃天数', value: fromSummary(data => `${data.activeDays} 天`) },
    { label: '当前连续', value: fromSummary(data => `${data.currentStreak} 天`) },
    { label: '最长连续', value: fromSummary(data => `${data.longestStreak} 天`) },
  ];
});

// 热力图只在 lifetime 有真实用量时渲染，窗口固定 52 列。
const heatmap = computed(() => {
  const snapshot = store.tokenStatisticsLifetime;
  if (!snapshot || snapshot.summary.total <= 0) return null;
  const columns = buildHeatmapWeeks(
    snapshot.days,
    snapshot.range.end_date,
    heatmapMode.value,
    HEATMAP_WEEK_COUNT,
  );
  return { columns, monthLabels: buildHeatmapMonthLabels(columns) };
});

const trendDays = computed(() => statistics.value?.days ?? []);
const trendBandWidth = computed(() => TREND_PLOT_WIDTH / Math.max(1, trendDays.value.length));

const trend = computed(() => {
  const snapshot = statistics.value;
  const days = snapshot?.days ?? [];
  const chart = buildTrendChart(days, snapshot?.models ?? []);
  // 折线点对齐到逐日命中带中心，让 hover 区域与曲线位置一致。
  const pathWidth = Math.max(0, TREND_PLOT_WIDTH - trendBandWidth.value);
  const series = chart.series.map(item => ({
    ...item,
    path: buildLinePath(item.values, pathWidth, TREND_PLOT_HEIGHT, chart.maxValue),
  }));
  return {
    labels: chart.labels,
    series,
    hasData: days.length > 0 && chart.maxValue > 0,
  };
});

const trendLineTransform = computed(() => {
  const offsetX = (TREND_PAD_LEFT + trendBandWidth.value / 2).toFixed(2);
  return `translate(${offsetX} ${TREND_PAD_TOP})`;
});

const donutSlices = computed(() => buildDonutSlices(statistics.value?.models ?? []));

const donutRows = computed(() => {
  const models = new Map((statistics.value?.models ?? []).map(model => [model.key, model]));
  return donutSlices.value.slices.map(slice => {
    const model = models.get(slice.key);
    const metrics = {
      prompt: model?.prompt ?? 0,
      completion: model?.completion ?? 0,
      cached: model?.cached ?? 0,
      requests: model?.requests ?? 0,
    };
    return {
      key: slice.key,
      label: slice.label,
      color: slice.color,
      share: slice.share,
      meta: `${formatTokenCount(slice.total)} Token · 输入 ${formatTokenCount(metrics.prompt)} · 输出 ${formatTokenCount(metrics.completion)} · 缓存 ${formatTokenCount(metrics.cached)} · ${metrics.requests} 次请求`,
    };
  });
});

// 点选明细：日期集合取自选中的格子（单日一格 / 整列 7 天），明细数据来自累计（全量）快照，
// 热力图窗口本身也是 52 周，所以点击不会触达快照范围外。
const selectionDetail = computed(() => {
  const current = selection.value;
  if (!current) return null;
  const column = (heatmap.value?.columns ?? []).find(item => item.key === current.columnKey);
  if (!column) return null;
  const dates = current.date ? [current.date] : column.cells.map(cell => cell.date);
  return buildSelectionDetail(store.tokenStatisticsLifetime?.days ?? [], dates);
});

function isCellSelected(column: HeatmapWeekColumn, cell: HeatmapDayCell) {
  const current = selection.value;
  if (!current || current.columnKey !== column.key) return false;
  return current.date === null || current.date === cell.date;
}

function toggleCell(column: HeatmapWeekColumn, cell: HeatmapDayCell) {
  const next = {
    columnKey: column.key,
    date: heatmapMode.value === 'daily' ? cell.date : null,
  };
  const current = selection.value;
  const same = Boolean(
    current && current.columnKey === next.columnKey && current.date === next.date,
  );
  selection.value = same ? null : next;
}

function clearSelection() {
  selection.value = null;
}

function trendColumnX(index: number) {
  return TREND_PAD_LEFT + index * trendBandWidth.value;
}

function showAxisLabel(index: number) {
  const total = trend.value.labels.length;
  return total <= 14 || index % 5 === 0;
}

// 趋势列 title 用「9月22日」这种不含年份的中文短日期。
function dayLabel(date: string) {
  return formatDayFull(date).replace(/^\d{4}年/, '');
}

function formatShare(share: number) {
  return share >= 0.1 ? `${Math.round(share * 100)}%` : `${(share * 100).toFixed(1)}%`;
}

async function selectRange(days: 1 | 7 | 30) {
  await store.loadTokenStatistics(days, true);
}

async function refreshAll() {
  await Promise.all([
    store.loadTokenStatistics(store.tokenStatisticsDays, true),
    store.loadTokenStatisticsLifetime(true),
  ]);
}

onMounted(() => {
  // 旧版入口可能留下「今日」（1）或「累计」（0）范围；新版没有这两个 tab，进入时回落到近 7 天。
  const days = store.tokenStatisticsDays === 1 || store.tokenStatisticsDays === 0
    ? 7
    : store.tokenStatisticsDays;
  void store.loadTokenStatistics(
    store.tokenStatistics ? days : 30,
    false,
  );
  void store.loadTokenStatisticsLifetime(false);
});
</script>

<style scoped>
.token-statistics-page { max-width: 1180px; margin: 0 auto; padding-bottom: 48px; }
.usage-header { margin-bottom: 24px; }
.eyebrow { margin-bottom: 9px; color: var(--accent); font-size: 10px; font-weight: 700; letter-spacing: 1.8px; }
.usage-header h1 { margin: 0 0 9px; font-size: 32px; letter-spacing: -1px; }
.usage-header p, .chart-empty { margin: 0; color: var(--text-faint); font-size: 12px; }

/* 累计摘要条：5 格等宽，格间 1px 竖线 */
.summary-strip { display: flex; align-items: stretch; overflow: hidden; margin-bottom: 18px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg1); }
.summary-strip-item { flex: 1 1 0; min-width: 0; padding: 12px 10px; text-align: center; }
.summary-strip-item + .summary-strip-item { border-left: 1px solid var(--border); }
.summary-strip-value { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 17px; font-weight: 600; letter-spacing: -.3px; }
.summary-strip-label { margin-top: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); font-size: 11px; }

.usage-section { margin-top: 18px; padding: 18px 20px; border: 1px solid var(--border); border-radius: 15px; background: var(--bg1); }
.section-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.section-heading h2 { margin: 0; font-size: 14px; }

/* 胶囊 tabs（热力图模式 / 统计范围） */
.pill-tabs { display: flex; align-items: center; flex: 0 0 auto; padding: 3px; border-radius: 999px; background: var(--bg2); }
.pill-tabs button { padding: 4px 10px; border: 0; border-radius: 999px; color: var(--text-dim); background: transparent; font-size: 11.5px; font-weight: 500; cursor: pointer; }
.pill-tabs button.active { color: var(--text); background: var(--bg3); }

/* 热力图：52 列 × 7 格 */
.heatmap-grid { display: grid; grid-template-columns: repeat(52, minmax(0, 1fr)); column-gap: 3px; margin-top: 14px; }
.heatmap-col { display: flex; min-width: 0; flex-direction: column; gap: 2px; }
/* 格子按 button 渲染（键盘可达），清掉默认外观后按原尺寸绘制 */
.heatmap-cell { width: 100%; min-width: 0; aspect-ratio: 1; padding: 0; border: 1px solid transparent; border-radius: 3px; background: none; appearance: none; cursor: pointer; transition: transform .15s ease; }
.heatmap-cell.is-selected { box-shadow: 0 0 0 2px var(--accent); transform: scale(1.15); }
.heatmap-cell.has-usage { border-color: var(--border); }
.heatmap-cell:hover { transform: scale(1.15); }
.heatmap-cell.lvl-0 { background: var(--bg3); }
.heatmap-cell.lvl-1 { background: color-mix(in srgb, var(--accent) 22%, var(--bg2)); }
.heatmap-cell.lvl-2 { background: color-mix(in srgb, var(--accent) 42%, var(--bg2)); }
.heatmap-cell.lvl-3 { background: color-mix(in srgb, var(--accent) 64%, var(--bg2)); }
.heatmap-cell.lvl-4 { background: color-mix(in srgb, var(--accent) 88%, var(--bg2)); }
.heatmap-hint { margin-left: 8px; color: var(--text-faint); font-size: 11px; font-weight: 500; }
.heatmap-months { margin-top: 10px; }
.heatmap-month { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); font-size: 10px; }

/* 范围行 */
.range-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-top: 22px; }
.range-row-title { font-size: 13px; font-weight: 600; }

/* 趋势图 */
.trend-legend { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 12px; }
.trend-legend-item { display: inline-flex; align-items: center; gap: 6px; min-width: 0; color: var(--text-dim); font-size: 11px; }
.trend-legend-item i { flex: 0 0 auto; width: 10px; height: 10px; border-radius: 3px; }
.trend-svg { display: block; width: 100%; margin-top: 6px; }
.trend-grid-line { stroke: var(--border); stroke-width: 1; }
.trend-axis-label { fill: var(--text-faint); font-size: 10px; font-family: var(--mono); }

/* 环图 + 模型列表 */
.donut-grid { display: grid; grid-template-columns: minmax(0, 240px) minmax(0, 1fr); gap: 22px; align-items: center; margin-top: 14px; }
.donut-wrap { position: relative; width: 190px; margin: 0 auto; }
.donut-svg { display: block; width: 100%; height: auto; }
.donut-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; pointer-events: none; }
.donut-center strong { font-size: 18px; }
.donut-center span { margin-top: 2px; color: var(--text-faint); font-size: 10px; text-transform: uppercase; }
.model-list { min-width: 0; }
.model-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 2px 8px; padding: 9px 0; }
.model-row + .model-row { border-top: 1px solid var(--border); }
.model-dot { width: 10px; height: 10px; border-radius: 3px; }
.model-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); font-size: 12px; }
.model-share { color: var(--text-dim); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.model-meta { grid-column: 2 / -1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); font-size: 10.5px; }

/* 空态沿用虚线框 */
.chart-empty { margin-top: 14px; padding: 34px 16px; border: 1px dashed var(--border); border-radius: 12px; text-align: center; color: var(--text-faint); font-size: 12px; }

/* 底部刷新 */
.usage-footer { display: flex; justify-content: flex-end; margin-top: 18px; }
.refresh-button { display: inline-flex; align-items: center; gap: 5px; padding: 8px 12px; border: 1px solid var(--border); border-radius: 9px; color: var(--text-dim); background: var(--bg1); font-size: 12px; cursor: pointer; }
.refresh-button:hover { color: var(--text); }
.refresh-button:disabled { opacity: .5; cursor: default; }
.spinning { display: inline-block; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* 首屏加载 */
.usage-loading { min-height: 320px; display: grid; place-content: center; justify-items: center; gap: 14px; color: var(--text-faint); font-size: 12px; }
.loading-orbit { width: 34px; height: 34px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }

@media (max-width: 860px) {
  .summary-strip { flex-direction: column; }
  .summary-strip-item + .summary-strip-item { border-left: 0; border-top: 1px solid var(--border); }
  .section-heading, .range-row { flex-wrap: wrap; }
  .donut-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>