// 星瑶 Token 统计可视化的纯计算工具：色板、热力图、趋势折线、环图切片、累计摘要与日期格式化。
// 组件只负责渲染，所有分级、聚合与格式化逻辑集中在这里，便于单元测试。
import type {
  TokenStatisticsDay,
  TokenStatisticsMetrics,
  TokenStatisticsModel,
} from '../types/pywebview';

const DAY_MS = 86_400_000;
const HEATMAP_DAYS_PER_WEEK = 7;
const HEATMAP_VISIBLE_MONTH_LABEL_COUNT = 12;
const TREND_MAX_SERIES = 6;
const DONUT_MAX_SLICES = 6;
const DONUT_SLICE_GAP = 0.003;

/** 图表序列色板，索引取模循环使用。 */
export const CHART_SERIES_COLORS = [
  'var(--accent)',
  'var(--green)',
  'var(--purple)',
  'var(--orange)',
  'var(--cyan)',
  'var(--yellow)',
] as const;

export function seriesColor(index: number): string {
  const size = CHART_SERIES_COLORS.length;
  const normalized = Number.isFinite(index) ? Math.trunc(index) : 0;
  return CHART_SERIES_COLORS[((normalized % size) + size) % size];
}

// ---------------------------------------------------------------------------
// 日期工具：全部按 UTC 运算，避免本地时区把日期偏移一天。
// ---------------------------------------------------------------------------

function parseUtcDate(dateKey: string): Date | null {
  const date = new Date(`${dateKey}T00:00:00.000Z`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function toDateKey(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function formatDayShort(date: string): string {
  const parsed = parseUtcDate(date);
  if (!parsed) return date;
  return `${parsed.getUTCMonth() + 1}/${parsed.getUTCDate()}`;
}

export function formatDayFull(date: string): string {
  const parsed = parseUtcDate(date);
  if (!parsed) return date;
  return `${parsed.getUTCFullYear()}年${parsed.getUTCMonth() + 1}月${parsed.getUTCDate()}日`;
}

export function formatMonthLabel(monthKey: string): string {
  const match = /^(\d{4})-(\d{2})/.exec(monthKey);
  if (!match) return monthKey;
  const month = Number(match[2]);
  if (month < 1 || month > 12) return monthKey;
  return `${month}月`;
}

// ---------------------------------------------------------------------------
// 热力图
// ---------------------------------------------------------------------------

export type HeatmapMode = 'daily' | 'weekly' | 'cumulative';

export interface HeatmapDayCell {
  date: string;
  total: number;
  requests: number;
  level: 0 | 1 | 2 | 3 | 4;
  title: string;
}

export interface HeatmapWeekColumn {
  key: string;
  monthKey: string;
  title: string | null;
  cells: HeatmapDayCell[];
}

interface HeatmapWindowDay {
  date: string;
  total: number;
  requests: number;
}

function levelForValue(value: number, max: number): 0 | 1 | 2 | 3 | 4 {
  if (value <= 0 || max <= 0) return 0;
  return Math.min(4, Math.max(1, Math.ceil((value / max) * 4))) as 0 | 1 | 2 | 3 | 4;
}

function filledRowsForValue(value: number, max: number): number {
  if (value <= 0 || max <= 0) return 0;
  return Math.min(
    HEATMAP_DAYS_PER_WEEK,
    Math.max(1, Math.ceil((value / max) * HEATMAP_DAYS_PER_WEEK)),
  );
}

// 周列包含每月 1 日时优先归入新月份，让月份标签锚定到 1 日所在列；
// 其余列沿用周日起始月份，避免月初文案被推迟一列。
function resolveWeekMonthKey(dates: string[]): string {
  const firstOfMonth = dates.find(date => date.endsWith('-01'));
  return (firstOfMonth ?? dates[0] ?? '').slice(0, 7);
}

export function buildHeatmapWeeks(
  days: TokenStatisticsDay[],
  endDate: string,
  mode: HeatmapMode,
  weekCount = 52,
): HeatmapWeekColumn[] {
  const totalWeeks = Math.max(0, Math.trunc(weekCount));
  const end = parseUtcDate(endDate);
  if (totalWeeks === 0 || !end) return [];

  // 最后一列 = endDate 所在自然周（周日起始），向前共 weekCount 列。
  const lastWeekStart = new Date(end.getTime());
  lastWeekStart.setUTCDate(end.getUTCDate() - end.getUTCDay());
  const firstWeekStart = new Date(lastWeekStart.getTime());
  firstWeekStart.setUTCDate(lastWeekStart.getUTCDate() - (totalWeeks - 1) * HEATMAP_DAYS_PER_WEEK);

  const daysByDate = new Map<string, TokenStatisticsDay>();
  for (const day of days) {
    if (day && typeof day.date === 'string') daysByDate.set(day.date, day);
  }

  // 窗口内补齐缺失日期为 0 值格子，保证网格恒为 weekCount×7。
  const windowDays: HeatmapWindowDay[] = [];
  for (let offset = 0; offset < totalWeeks * HEATMAP_DAYS_PER_WEEK; offset += 1) {
    const date = toDateKey(new Date(firstWeekStart.getTime() + offset * DAY_MS));
    const entry = daysByDate.get(date);
    windowDays.push({
      date,
      total: Number(entry?.total) || 0,
      requests: Number(entry?.requests) || 0,
    });
  }

  if (mode === 'daily') {
    const maxDaily = windowDays.reduce((max, day) => Math.max(max, day.total), 0);
    const columns: HeatmapWeekColumn[] = [];
    for (let weekIndex = 0; weekIndex < totalWeeks; weekIndex += 1) {
      const cells: HeatmapDayCell[] = windowDays
        .slice(weekIndex * HEATMAP_DAYS_PER_WEEK, (weekIndex + 1) * HEATMAP_DAYS_PER_WEEK)
        .map(day => ({
          date: day.date,
          total: day.total,
          requests: day.requests,
          level: levelForValue(day.total, maxDaily),
          title: `${formatDayFull(day.date)} · ${formatTokenCount(day.total)} Token · ${day.requests} 次请求`,
        }));
      columns.push({
        key: `daily-${cells[0].date}`,
        monthKey: resolveWeekMonthKey(cells.map(cell => cell.date)),
        title: null,
        cells,
      });
    }
    return columns;
  }

  // weekly / cumulative：先按周聚合，再按全窗口最大值分级。
  const weeklyTotals: number[] = [];
  const weeklyRequests: number[] = [];
  for (let weekIndex = 0; weekIndex < totalWeeks; weekIndex += 1) {
    const weekDays = windowDays.slice(
      weekIndex * HEATMAP_DAYS_PER_WEEK,
      (weekIndex + 1) * HEATMAP_DAYS_PER_WEEK,
    );
    weeklyTotals.push(weekDays.reduce((sum, day) => sum + day.total, 0));
    weeklyRequests.push(weekDays.reduce((sum, day) => sum + day.requests, 0));
  }

  const isCumulative = mode === 'cumulative';
  const values: number[] = [];
  const requestValues: number[] = [];
  for (let weekIndex = 0; weekIndex < totalWeeks; weekIndex += 1) {
    const previousValue = weekIndex > 0 ? values[weekIndex - 1] : 0;
    const previousRequests = weekIndex > 0 ? requestValues[weekIndex - 1] : 0;
    values.push(isCumulative ? previousValue + weeklyTotals[weekIndex] : weeklyTotals[weekIndex]);
    requestValues.push(
      isCumulative ? previousRequests + weeklyRequests[weekIndex] : weeklyRequests[weekIndex],
    );
  }
  const maxValue = values.reduce((max, value) => Math.max(max, value), 0);

  const columns: HeatmapWeekColumn[] = [];
  for (let weekIndex = 0; weekIndex < totalWeeks; weekIndex += 1) {
    const weekDays = windowDays.slice(
      weekIndex * HEATMAP_DAYS_PER_WEEK,
      (weekIndex + 1) * HEATMAP_DAYS_PER_WEEK,
    );
    const value = values[weekIndex];
    const requestCount = requestValues[weekIndex];
    const level = levelForValue(value, maxValue);
    const filledRows = filledRowsForValue(value, maxValue);
    const weekEndDate = weekDays[weekDays.length - 1].date;
    const title = isCumulative
      ? `截至 ${formatDayFull(weekEndDate)} · 累计 ${formatTokenCount(value)} Token · ${requestCount} 次请求`
      : `截至 ${formatDayFull(weekEndDate)} · ${formatTokenCount(value)} Token · ${requestCount} 次请求`;
    const cells: HeatmapDayCell[] = weekDays.map((day, dayOffset) => ({
      date: day.date,
      total: day.total,
      requests: day.requests,
      // 从底部起填充 filledRows 格，其余保持 0 级。
      level: dayOffset >= HEATMAP_DAYS_PER_WEEK - filledRows ? level : 0,
      title,
    }));
    columns.push({
      key: `${mode}-${weekDays[0].date}`,
      monthKey: resolveWeekMonthKey(cells.map(cell => cell.date)),
      title,
      cells,
    });
  }
  return columns;
}

export function buildHeatmapMonthLabels(
  columns: HeatmapWeekColumn[],
): { key: string; label: string; span: number }[] {
  const labels: { key: string; label: string; span: number }[] = [];
  for (const column of columns) {
    const lastLabel = labels[labels.length - 1];
    if (lastLabel && lastLabel.key === column.monthKey) {
      lastLabel.span += 1;
    } else {
      labels.push({ key: column.monthKey, label: formatMonthLabel(column.monthKey), span: 1 });
    }
  }

  // 52 个自然周可能横跨 13 个月；保留多余起始月份的 span 以维持对齐，
  // 只隐藏其文字，底部始终最多显示最近 12 个月。
  const hiddenCount = Math.max(0, labels.length - HEATMAP_VISIBLE_MONTH_LABEL_COUNT);
  for (let index = 0; index < hiddenCount; index += 1) {
    labels[index].label = '';
  }
  return labels;
}

// ---------------------------------------------------------------------------
// 趋势折线
// ---------------------------------------------------------------------------

export interface TrendSeries {
  key: string;
  label: string;
  color: string;
  values: number[];
}

export interface TrendChartData {
  labels: string[];
  series: TrendSeries[];
  maxValue: number;
}

export function buildTrendChart(
  days: TokenStatisticsDay[],
  models: TokenStatisticsModel[],
): TrendChartData {
  const labels = days.map(day => formatDayShort(day.date));
  const series = models.slice(0, TREND_MAX_SERIES).map((model, index) => ({
    key: model.key,
    label: model.model || model.key,
    color: seriesColor(index),
    values: days.map(day => {
      const match = (day.models ?? []).find(item => item.key === model.key);
      return Number(match?.total) || 0;
    }),
  }));
  let maxValue = 0;
  for (const item of series) {
    for (const value of item.values) {
      if (value > maxValue) maxValue = value;
    }
  }
  return { labels, series, maxValue };
}

export function buildLinePath(
  values: number[],
  width: number,
  height: number,
  maxValue: number,
): string {
  const count = values.length;
  if (count < 2 || !(maxValue > 0)) return '';

  const points = values.map((value, index) => ({
    x: (index / (count - 1)) * width,
    y: height - (value / maxValue) * height,
  }));
  const round = (value: number) => value.toFixed(2);

  let path = `M ${round(points[0].x)} ${round(points[0].y)}`;
  for (let index = 0; index < count - 1; index += 1) {
    const previous = index > 0 ? points[index - 1] : points[index];
    const current = points[index];
    const next = points[index + 1];
    const afterNext = index + 2 < count ? points[index + 2] : next;
    // Catmull-Rom 转三次贝塞尔：控制点偏移为相邻点差值的 1/6。
    const control1x = current.x + (next.x - previous.x) / 6;
    const control1y = current.y + (next.y - previous.y) / 6;
    const control2x = next.x - (afterNext.x - current.x) / 6;
    const control2y = next.y - (afterNext.y - current.y) / 6;
    path += ` C ${round(control1x)} ${round(control1y)}, ${round(control2x)} ${round(control2y)}, ${round(next.x)} ${round(next.y)}`;
  }
  return path;
}

// ---------------------------------------------------------------------------
// 环图切片
// ---------------------------------------------------------------------------

export interface DonutSlice {
  key: string;
  label: string;
  color: string;
  total: number;
  share: number;
  startRatio: number;
  endRatio: number;
}

export function buildDonutSlices(models: TokenStatisticsModel[]): {
  slices: DonutSlice[];
  total: number;
} {
  const positive = models.filter(model => Number(model.total) > 0);
  const total = positive.reduce((sum, model) => sum + Number(model.total), 0);
  if (positive.length === 0 || total <= 0) return { slices: [], total: 0 };

  const sources = positive.map(model => ({
    key: model.key,
    label: model.model || model.key,
    total: Number(model.total),
  }));
  if (sources.length > DONUT_MAX_SLICES) {
    const mergedTotal = sources
      .slice(DONUT_MAX_SLICES - 1)
      .reduce((sum, source) => sum + source.total, 0);
    sources.splice(DONUT_MAX_SLICES - 1, sources.length, {
      key: '__other__',
      label: '其他',
      total: mergedTotal,
    });
  }

  // 多片时每片两端各留 0.003 圆周间隙；单片时整圆铺满。
  const gap = sources.length > 1 ? DONUT_SLICE_GAP : 0;
  let cursor = 0;
  const slices = sources.map((source, index) => {
    const share = source.total / total;
    const startRatio = cursor + gap;
    const endRatio = Math.max(startRatio, cursor + share - gap);
    cursor += share;
    return {
      key: source.key,
      label: source.label,
      color: seriesColor(index),
      total: source.total,
      share,
      startRatio,
      endRatio,
    };
  });
  return { slices, total };
}

// ---------------------------------------------------------------------------
// 累计摘要
// ---------------------------------------------------------------------------

export interface LifetimeSummary {
  totalTokens: number;
  peakDay: { date: string; total: number } | null;
  activeDays: number;
  currentStreak: number;
  longestStreak: number;
}

export function buildLifetimeSummary(days: TokenStatisticsDay[]): LifetimeSummary {
  let totalTokens = 0;
  let peakDay: { date: string; total: number } | null = null;
  let activeDays = 0;
  let longestStreak = 0;
  let runningStreak = 0;

  for (const day of days) {
    const total = Number(day.total) || 0;
    totalTokens += total;
    if (total > 0) {
      activeDays += 1;
      runningStreak += 1;
      if (runningStreak > longestStreak) longestStreak = runningStreak;
      if (!peakDay || total > peakDay.total) peakDay = { date: day.date, total };
    } else {
      runningStreak = 0;
    }
  }

  // 从最后一天往前数连续活跃天数；最后一天为 0 则当前连续为 0。
  let currentStreak = 0;
  for (let index = days.length - 1; index >= 0; index -= 1) {
    const total = Number(days[index].total) || 0;
    if (total <= 0) break;
    currentStreak += 1;
  }

  return { totalTokens, peakDay, activeDays, currentStreak, longestStreak };
}

// ---------------------------------------------------------------------------
// 选中日期明细（热力图点开的单日 / 单周）
// ---------------------------------------------------------------------------

const SELECTION_METRIC_FIELDS = [
  'prompt', 'completion', 'cached', 'cache_prompt', 'total', 'requests',
] as const;

export interface SelectionDetail extends TokenStatisticsMetrics {
  dates: string[];
  title: string;
  models: TokenStatisticsModel[];
}

function emptyMetrics(): TokenStatisticsMetrics {
  return {
    prompt: 0, completion: 0, cached: 0, cache_prompt: 0, total: 0, requests: 0,
    cache_hit_rate: null,
  };
}

// 单日单模型自带 cache_hit_rate，跨模型、跨天聚合后必须按合计数重算。
function withCacheHitRate<T extends TokenStatisticsMetrics>(metrics: T): T {
  const rate = metrics.cache_prompt > 0
    ? Math.round((metrics.cached / metrics.cache_prompt) * 1000) / 10
    : null;
  return { ...metrics, cache_hit_rate: rate };
}

export function formatDayMonthDay(date: string): string {
  const parsed = parseUtcDate(date);
  if (!parsed) return date;
  return `${parsed.getUTCMonth() + 1}月${parsed.getUTCDate()}日`;
}

export function buildSelectionTitle(dates: string[]): string {
  const first = dates[0] ?? '';
  const last = dates[dates.length - 1] ?? '';
  if (!last || first === last) return formatDayFull(first);
  // 同年只留「9月21日 - 9月22日」，跨年才补年份，避免标题过长。
  const sameYear = first.slice(0, 4) === last.slice(0, 4);
  const format = sameYear ? formatDayMonthDay : formatDayFull;
  return `${format(first)} - ${format(last)}`;
}

/** 把选中的日期集合（单日格 1 个、周聚合格 7 个）聚合成一行合计 + 按总量降序的模型列表。 */
export function buildSelectionDetail(
  days: TokenStatisticsDay[],
  dates: string[],
): SelectionDetail | null {
  const wanted = Array.from(
    new Set((dates ?? []).filter(date => typeof date === 'string' && date)),
  ).sort();
  if (wanted.length === 0) return null;

  const daysByDate = new Map<string, TokenStatisticsDay>();
  for (const day of days ?? []) {
    if (day && typeof day.date === 'string') daysByDate.set(day.date, day);
  }

  const totals = emptyMetrics();
  const modelTotals = new Map<string, TokenStatisticsModel>();
  for (const date of wanted) {
    const day = daysByDate.get(date);
    if (!day) continue;
    for (const field of SELECTION_METRIC_FIELDS) totals[field] += Number(day[field]) || 0;
    for (const model of day.models ?? []) {
      const key = typeof model?.key === 'string' ? model.key : '';
      if (!key) continue;
      let current = modelTotals.get(key);
      if (!current) {
        current = {
          key,
          provider: model.provider ?? '',
          model: model.model ?? '',
          ...emptyMetrics(),
        };
        modelTotals.set(key, current);
      }
      for (const field of SELECTION_METRIC_FIELDS) current[field] += Number(model[field]) || 0;
    }
  }

  const models = Array.from(modelTotals.values())
    .map(withCacheHitRate)
    .sort((a, b) => (b.total - a.total) || a.key.localeCompare(b.key));

  return { dates: wanted, title: buildSelectionTitle(wanted), ...withCacheHitRate(totals), models };
}

// ---------------------------------------------------------------------------
// 模型用量排行折线（横轴 = 按总量降序的模型，三条序列：总量 / 输入 / 输出）
// ---------------------------------------------------------------------------

export const RANKING_MAX_MODELS = 6;
const RANKING_LABEL_MAX_LENGTH = 12;

export type RankingSeriesKey = 'total' | 'prompt' | 'completion';

export interface RankingSeries {
  key: RankingSeriesKey;
  label: string;
  color: string;
  values: number[];
  path: string;
}

export interface RankingChart {
  labels: string[];
  fullLabels: string[];
  series: RankingSeries[];
  maxValue: number;
  hasData: boolean;
}

const RANKING_SERIES_DEFINITIONS: { key: RankingSeriesKey; label: string; color: string }[] = [
  { key: 'total', label: '总量', color: seriesColor(0) },
  { key: 'prompt', label: '输入', color: seriesColor(1) },
  { key: 'completion', label: '输出', color: seriesColor(2) },
];

function truncateModelLabel(name: string): string {
  const label = String(name ?? '');
  return label.length > RANKING_LABEL_MAX_LENGTH
    ? `${label.slice(0, RANKING_LABEL_MAX_LENGTH - 1)}…`
    : label;
}

export function buildRankingChart(
  models: TokenStatisticsModel[],
  width: number,
  height: number,
): RankingChart {
  const ranked = (models ?? [])
    .filter(model => Number(model?.total) > 0)
    .sort((a, b) => (Number(b.total) - Number(a.total)) || a.key.localeCompare(b.key))
    .slice(0, RANKING_MAX_MODELS);
  const fullLabels = ranked.map(model => model.model || model.key);
  if (ranked.length === 0) {
    return { labels: [], fullLabels: [], series: [], maxValue: 0, hasData: false };
  }

  let maxValue = 0;
  for (const model of ranked) {
    for (const definition of RANKING_SERIES_DEFINITIONS) {
      maxValue = Math.max(maxValue, Number(model[definition.key]) || 0);
    }
  }
  const series = RANKING_SERIES_DEFINITIONS.map(definition => {
    const values = ranked.map(model => Number(model[definition.key]) || 0);
    return {
      ...definition,
      values,
      // 单个模型时没有连线，组件只画数据点。
      path: values.length >= 2 ? buildLinePath(values, width, height, maxValue) : '',
    };
  });

  return {
    labels: fullLabels.map(truncateModelLabel),
    fullLabels,
    series,
    maxValue,
    hasData: maxValue > 0,
  };
}

// 环图弧段：主统计页与当日明细共用，避免两份同样的字符串拼接。
export function donutDashArray(slice: DonutSlice, circumference: number): string {
  const length = (slice.endRatio - slice.startRatio) * circumference;
  return `${length.toFixed(2)} ${circumference.toFixed(2)}`;
}

export function donutDashOffset(slice: DonutSlice, circumference: number): string {
  return (-slice.startRatio * circumference).toFixed(2);
}

export function formatTokenCount(value: number): string {
  const amount = Number(value) || 0;
  const format = (scaled: number, suffix: string) => (
    `${scaled.toFixed(1).replace(/\.0$/, '')}${suffix}`
  );
  if (Math.abs(amount) >= 1_000_000_000) return format(amount / 1_000_000_000, 'B');
  if (Math.abs(amount) >= 1_000_000) return format(amount / 1_000_000, 'M');
  if (Math.abs(amount) >= 1_000) return format(amount / 1_000, 'K');
  return Math.round(amount).toLocaleString('zh-CN');
}
