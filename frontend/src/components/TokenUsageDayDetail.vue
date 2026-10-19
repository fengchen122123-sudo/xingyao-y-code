<template>
  <section class="usage-section day-detail" data-test="token-day-detail" aria-label="选中日期用量明细">
    <div class="section-heading">
      <div class="detail-head">
        <h2>
          {{ detail.title }}
          <span v-if="mode === 'week'" class="detail-scope">整周合计</span>
        </h2>
        <p class="detail-meta">{{ metaLine }}</p>
      </div>
      <button
        type="button"
        class="detail-close"
        data-test="day-detail-close"
        aria-label="收起用量明细"
        @click="emit('close')"
      >×</button>
    </div>

    <div v-if="hasData" class="detail-grid">
      <!-- 折线：横轴为按总量降序的模型，三条序列分别看总量 / 输入 / 输出 -->
      <div class="detail-panel">
        <div class="detail-panel-title">模型用量排行</div>
        <div class="ranking-legend" data-test="day-detail-legend">
          <span v-for="item in ranking.series" :key="item.key" class="ranking-legend-item">
            <i :style="{ background: item.color }"></i>{{ item.label }}
          </span>
        </div>
        <svg
          class="ranking-svg"
          :viewBox="`0 0 ${RANKING_WIDTH} ${RANKING_HEIGHT}`"
          width="100%"
          role="img"
          aria-label="当日各模型用量排行"
          data-test="day-detail-ranking"
        >
          <line
            v-for="(y, index) in gridLines"
            :key="`grid-${index}`"
            class="ranking-grid-line"
            :x1="RANKING_PAD_LEFT"
            :x2="RANKING_PAD_LEFT + RANKING_PLOT_WIDTH"
            :y1="y"
            :y2="y"
            stroke-dasharray="4 4"
          />
          <path
            v-for="item in ranking.series"
            :key="`line-${item.key}`"
            :d="item.path"
            :stroke="item.color"
            stroke-width="2"
            fill="none"
            stroke-linejoin="round"
          />
          <template v-for="(label, index) in ranking.labels" :key="`point-${index}`">
            <circle
              v-for="item in ranking.series"
              :key="`${item.key}-${index}`"
              :cx="pointX(index)" :cy="pointY(item.values[index])"
              r="3.2" :fill="item.color" stroke="var(--bg1)" stroke-width="1"
            />
            <text
              class="ranking-axis-label"
              :x="pointX(index)" :y="RANKING_HEIGHT - 12"
              text-anchor="middle"
            >{{ label }}</text>
            <rect
              class="ranking-band"
              :x="bandX(index)" :y="RANKING_PAD_TOP"
              :width="bandWidth" :height="RANKING_PLOT_HEIGHT"
              fill="transparent"
              data-test="day-detail-band"
            >
              <title>{{ bandTitle(index) }}</title>
            </rect>
          </template>
        </svg>
      </div>

      <!-- 扇形：当天各模型占比 -->
      <div class="detail-panel">
        <div class="detail-panel-title">模型占比</div>
        <div class="donut-grid">
          <div class="donut-wrap">
            <svg class="donut-svg" viewBox="0 0 120 120" role="img" aria-label="当日模型占比" data-test="day-detail-donut">
              <circle
                v-for="slice in donutSlices.slices"
                :key="slice.key"
                cx="60" cy="60" r="48"
                fill="none" :stroke="slice.color" stroke-width="16"
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
            <div
              v-for="row in donutRows"
              :key="row.key"
              class="model-row"
              role="listitem"
              data-test="day-detail-model-row"
            >
              <span class="model-dot" :style="{ background: row.color }"></span>
              <span class="model-name">{{ row.label }}</span>
              <span class="model-share">{{ formatShare(row.share) }}</span>
              <span class="model-meta">{{ row.meta }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-else class="chart-empty">{{ emptyText }}</div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { SelectionDetail } from '../utils/tokenStatistics';
import {
  buildDonutSlices,
  buildRankingChart,
  donutDashArray,
  donutDashOffset,
  formatTokenCount,
  seriesColor,
} from '../utils/tokenStatistics';

const props = defineProps<{
  detail: SelectionDetail;
  mode: 'day' | 'week';
}>();

const emit = defineEmits<{ (event: 'close'): void }>();

const RANKING_WIDTH = 520;
const RANKING_HEIGHT = 200;
const RANKING_PAD_LEFT = 14;
const RANKING_PAD_TOP = 14;
const RANKING_PAD_BOTTOM = 36;
const RANKING_PAD_RIGHT = 14;
const RANKING_PLOT_WIDTH = RANKING_WIDTH - RANKING_PAD_LEFT - RANKING_PAD_RIGHT;
const RANKING_PLOT_HEIGHT = RANKING_HEIGHT - RANKING_PAD_TOP - RANKING_PAD_BOTTOM;
const DONUT_RADIUS = 48;
const DONUT_CIRCUMFERENCE = 2 * Math.PI * DONUT_RADIUS;
const gridLines = [0, 1, 2, 3].map(step => RANKING_PAD_TOP + (step / 3) * RANKING_PLOT_HEIGHT);

const hasData = computed(() => props.detail.models.length > 0 && props.detail.total > 0);

const emptyText = computed(() => (
  props.mode === 'day' ? '这一天没有用量记录' : '这一周没有用量记录'
));

const metaLine = computed(() => {
  const parts = [
    `共 ${formatTokenCount(props.detail.total)} Token`,
    `${props.detail.requests} 次请求`,
    `输入 ${formatTokenCount(props.detail.prompt)}`,
    `输出 ${formatTokenCount(props.detail.completion)}`,
    `缓存 ${formatTokenCount(props.detail.cached)}`,
  ];
  if (props.detail.cache_hit_rate !== null) {
    parts.push(`命中率 ${props.detail.cache_hit_rate}%`);
  }
  return parts.join(' · ');
});

const ranking = computed(() => buildRankingChart(
  props.detail.models,
  RANKING_PLOT_WIDTH,
  RANKING_PLOT_HEIGHT,
));

const donutSlices = computed(() => buildDonutSlices(props.detail.models));

const donutRows = computed(() => {
  const models = new Map(props.detail.models.map(model => [model.key, model]));
  return donutSlices.value.slices.map((slice, index) => {
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
      color: slice.color ?? seriesColor(index),
      share: slice.share,
      meta: `${formatTokenCount(slice.total)} Token · 输入 ${formatTokenCount(metrics.prompt)} · 输出 ${formatTokenCount(metrics.completion)} · 缓存 ${formatTokenCount(metrics.cached)} · ${metrics.requests} 次请求`,
    };
  });
});

function pointX(index: number) {
  const count = ranking.value.labels.length;
  if (count <= 1) return RANKING_PAD_LEFT + RANKING_PLOT_WIDTH / 2;
  return RANKING_PAD_LEFT + (index / (count - 1)) * RANKING_PLOT_WIDTH;
}

function pointY(value: number) {
  const maxValue = ranking.value.maxValue;
  const ratio = maxValue > 0 ? Math.min(1, Math.max(0, (Number(value) || 0) / maxValue)) : 0;
  return RANKING_PAD_TOP + RANKING_PLOT_HEIGHT * (1 - ratio);
}

// 每个数据点一条等宽命中带，方便 hover 读出该模型的三项数值。
const bandWidth = computed(() => {
  const count = ranking.value.labels.length;
  return count <= 1 ? RANKING_PLOT_WIDTH : RANKING_PLOT_WIDTH / (count - 1);
});

function bandX(index: number) {
  return Math.max(RANKING_PAD_LEFT, pointX(index) - bandWidth.value / 2);
}

function bandTitle(index: number) {
  const model = props.detail.models.filter(item => item.total > 0)[index];
  if (!model) return '';
  return `${ranking.value.fullLabels[index]} · 总量 ${formatTokenCount(model.total)} · 输入 ${formatTokenCount(model.prompt)} · 输出 ${formatTokenCount(model.completion)}`;
}

function formatShare(share: number) {
  return share >= 0.1 ? `${Math.round(share * 100)}%` : `${(share * 100).toFixed(1)}%`;
}
</script>

<style scoped>
.day-detail { margin-top: 18px; }
.detail-head { min-width: 0; }
.detail-head h2 { display: flex; align-items: baseline; gap: 8px; }
.detail-scope { color: var(--text-faint); font-size: 11px; font-weight: 500; }
.detail-meta { margin: 8px 0 0; color: var(--text-faint); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.detail-close { flex: 0 0 auto; width: 26px; height: 26px; padding: 0; border: 1px solid var(--border); border-radius: 8px; color: var(--text-dim); background: var(--bg1); font-size: 15px; line-height: 1; cursor: pointer; }
.detail-close:hover { color: var(--text); }

.detail-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 20px; margin-top: 16px; }
.detail-panel { min-width: 0; }
.detail-panel-title { color: var(--text-dim); font-size: 11.5px; font-weight: 600; }

.ranking-legend { display: flex; flex-wrap: wrap; gap: 6px 12px; margin-top: 8px; }
.ranking-legend-item { display: inline-flex; align-items: center; gap: 6px; color: var(--text-dim); font-size: 11px; }
.ranking-legend-item i { width: 10px; height: 10px; border-radius: 3px; }
.ranking-svg { display: block; width: 100%; margin-top: 4px; }
.ranking-grid-line { stroke: var(--border); stroke-width: 1; }
.ranking-axis-label { fill: var(--text-faint); font-size: 9.5px; font-family: var(--mono); }
.ranking-band:hover { fill: color-mix(in srgb, var(--accent) 8%, transparent); }

.donut-grid { display: grid; grid-template-columns: minmax(0, 150px) minmax(0, 1fr); gap: 16px; align-items: center; margin-top: 10px; }
.donut-wrap { position: relative; width: 130px; margin: 0 auto; }
.donut-svg { display: block; width: 100%; height: auto; }
.donut-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; pointer-events: none; }
.donut-center strong { font-size: 14px; }
.donut-center span { margin-top: 2px; color: var(--text-faint); font-size: 10px; text-transform: uppercase; }

.model-list { min-width: 0; }
.model-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 2px 8px; padding: 8px 0; }
.model-row + .model-row { border-top: 1px solid var(--border); }
.model-dot { width: 10px; height: 10px; border-radius: 3px; }
.model-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); font-size: 12px; }
.model-share { color: var(--text-dim); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.model-meta { grid-column: 2 / -1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); font-size: 10.5px; }

.chart-empty { margin-top: 14px; padding: 34px 16px; border: 1px dashed var(--border); border-radius: 12px; text-align: center; color: var(--text-faint); font-size: 12px; }

@media (max-width: 860px) {
  .detail-grid { grid-template-columns: minmax(0, 1fr); }
  .donut-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
