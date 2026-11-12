import { describe, expect, it } from 'vitest';

import type { TokenStatisticsDay, TokenStatisticsModel } from '../types/pywebview';
import type { HeatmapWeekColumn } from './tokenStatistics';
import {
  CHART_SERIES_COLORS,
  RANKING_MAX_MODELS,
  formatCostCell,
  buildDonutSlices,
  buildHeatmapMonthLabels,
  buildHeatmapWeeks,
  buildLifetimeSummary,
  buildLinePath,
  buildRankingChart,
  buildSelectionDetail,
  buildSelectionTitle,
  buildTrendChart,
  formatDayFull,
  formatDayShort,
  formatMonthLabel,
  formatTokenCount,
  seriesColor,
} from './tokenStatistics';

function makeModel(key: string, total: number, modelName = key): TokenStatisticsModel {
  return {
    key,
    provider: 'test-provider',
    model: modelName,
    prompt: total,
    completion: 0,
    cached: 0,
    cache_prompt: 0,
    total,
    requests: 1,
    cache_hit_rate: null,
  };
}

function makeDay(
  date: string,
  total: number,
  requests = 1,
  models: TokenStatisticsModel[] = [],
): TokenStatisticsDay {
  return {
    date,
    prompt: total,
    completion: 0,
    cached: 0,
    cache_prompt: 0,
    total,
    requests,
    cache_hit_rate: null,
    models,
  };
}

describe('seriesColor', () => {
  it('按索引取模循环使用色板', () => {
    expect(CHART_SERIES_COLORS).toHaveLength(6);
    expect(seriesColor(0)).toBe(CHART_SERIES_COLORS[0]);
    expect(seriesColor(5)).toBe(CHART_SERIES_COLORS[5]);
    expect(seriesColor(6)).toBe(CHART_SERIES_COLORS[0]);
    expect(seriesColor(13)).toBe(CHART_SERIES_COLORS[1]);
    expect(seriesColor(-1)).toBe(CHART_SERIES_COLORS[5]);
  });
});

describe('buildHeatmapWeeks', () => {
  const dailyDays = [
    makeDay('2026-09-20', 100, 2),
    makeDay('2026-09-22', 50, 1),
    makeDay('2026-09-01', 999, 9),
  ];

  it('daily 模式按 endDate 所在周对齐窗口并补齐 0 值格子', () => {
    const columns = buildHeatmapWeeks(dailyDays, '2026-09-22', 'daily', 3);

    expect(columns).toHaveLength(3);
    expect(columns[0].cells[0].date).toBe('2026-09-06');
    expect(columns[2].cells[0].date).toBe('2026-09-20');
    expect(columns[2].cells[6].date).toBe('2026-09-26');

    for (const column of columns) {
      expect(column.cells).toHaveLength(7);
      expect(new Date(`${column.cells[0].date}T00:00:00Z`).getUTCDay()).toBe(0);
    }

    // 窗口内无数据日期补 0 格
    const emptyCell = columns[0].cells[0];
    expect(emptyCell.total).toBe(0);
    expect(emptyCell.requests).toBe(0);
    expect(emptyCell.level).toBe(0);
    expect(emptyCell.title).toBe('2026年9月6日 · 0 Token · 0 次请求');

    // 窗口外数据被忽略
    const dates = columns.flatMap(column => column.cells.map(cell => cell.date));
    expect(dates).not.toContain('2026-09-01');
    expect(dates).toHaveLength(21);
  });

  it('daily 模式按全窗口最大单日值分级并生成标题', () => {
    const columns = buildHeatmapWeeks(dailyDays, '2026-09-22', 'daily', 3);

    expect(columns[2].cells[0].level).toBe(4); // 100 / 100
    expect(columns[2].cells[1].level).toBe(0); // 9/21 无数据
    expect(columns[2].cells[2].level).toBe(2); // 50 / 100 → ceil(2) = 2
    expect(columns[2].cells[2].title).toBe('2026年9月22日 · 50 Token · 1 次请求');
    expect(columns[2].title).toBeNull();
  });

  it('默认 52 周窗口且全 0 数据分级为 0', () => {
    const columns = buildHeatmapWeeks([], '2026-09-22', 'daily');

    expect(columns).toHaveLength(52);
    const cells = columns.flatMap(column => column.cells);
    expect(cells).toHaveLength(52 * 7);
    expect(cells.every(cell => cell.level === 0)).toBe(true);
  });

  it('weekly 模式按周求和并从底部填充', () => {
    const days = [
      makeDay('2026-09-13', 30, 2),
      makeDay('2026-09-20', 100, 2),
      makeDay('2026-09-22', 50, 1),
    ];
    const columns = buildHeatmapWeeks(days, '2026-09-22', 'weekly', 3);

    expect(columns[0].cells.every(cell => cell.level === 0)).toBe(true);
    expect(columns[0].title).toBe('截至 2026年9月12日 · 0 Token · 0 次请求');

    // 9/13-9/19 周：30 / 150 → 1 级，从底部填充 2 格
    expect(columns[1].title).toBe('截至 2026年9月19日 · 30 Token · 2 次请求');
    expect(columns[1].cells.map(cell => cell.level)).toEqual([0, 0, 0, 0, 0, 1, 1]);

    // 9/20-9/26 周：150 / 150 → 4 级，7 格填满
    expect(columns[2].title).toBe('截至 2026年9月26日 · 150 Token · 3 次请求');
    expect(columns[2].cells.map(cell => cell.level)).toEqual([4, 4, 4, 4, 4, 4, 4]);
  });

  it('cumulative 模式从窗口起点累加', () => {
    const days = [
      makeDay('2026-09-13', 30, 2),
      makeDay('2026-09-20', 100, 2),
      makeDay('2026-09-22', 50, 1),
    ];
    const columns = buildHeatmapWeeks(days, '2026-09-22', 'cumulative', 3);

    expect(columns[0].title).toBe('截至 2026年9月12日 · 累计 0 Token · 0 次请求');
    expect(columns[1].title).toBe('截至 2026年9月19日 · 累计 30 Token · 2 次请求');
    expect(columns[2].title).toBe('截至 2026年9月26日 · 累计 180 Token · 5 次请求');

    expect(columns[1].cells.map(cell => cell.level)).toEqual([0, 0, 0, 0, 0, 1, 1]);
    expect(columns[2].cells.every(cell => cell.level === 4)).toBe(true);
  });

  it('endDate 无效或 weekCount 为 0 时返回空数组', () => {
    expect(buildHeatmapWeeks([], 'not-a-date', 'daily', 3)).toEqual([]);
    expect(buildHeatmapWeeks([], '2026-09-22', 'daily', 0)).toEqual([]);
  });
});

describe('buildHeatmapMonthLabels', () => {
  it('按 monthKey 连续合并 span，并把每月 1 日归入新月份', () => {
    const columns = buildHeatmapWeeks([], '2026-09-22', 'daily', 12);
    const labels = buildHeatmapMonthLabels(columns);

    // 窗口 7/5-9/26：7/26-8/1 与 8/30-9/5 两列包含 1 日，分别归入 8 月与 9 月。
    expect(labels.map(label => label.key)).toEqual(['2026-07', '2026-08', '2026-09']);
    expect(labels.map(label => label.label)).toEqual(['7月', '8月', '9月']);
    expect(labels.map(label => label.span)).toEqual([3, 5, 4]);
    expect(labels.reduce((sum, label) => sum + label.span, 0)).toBe(12);
  });

  it('超过 12 个标签时清空最早的多余文案但保留 span', () => {
    const columns: HeatmapWeekColumn[] = Array.from({ length: 14 }, (_, index) => ({
      key: `column-${index}`,
      monthKey: `2026-${String((index % 12) + 1).padStart(2, '0')}`,
      title: null,
      cells: [],
    }));
    const labels = buildHeatmapMonthLabels(columns);

    expect(labels).toHaveLength(14);
    expect(labels.slice(0, 2).map(label => label.label)).toEqual(['', '']);
    expect(labels[2].label).toBe('3月');
    expect(labels[13].label).toBe('2月');
    expect(labels.reduce((sum, label) => sum + label.span, 0)).toBe(14);
  });
});

describe('buildTrendChart', () => {
  const days = [
    makeDay('2026-09-22', 130, 2, [makeModel('m1', 100, 'Moonshot'), makeModel('m2', 30, '')]),
    makeDay('2026-09-23', 40, 1, [makeModel('m1', 40, 'Moonshot')]),
  ];
  const models = [
    makeModel('m1', 140, 'Moonshot'),
    makeModel('m2', 30, ''),
    makeModel('m3', 10),
    makeModel('m4', 10),
    makeModel('m5', 10),
    makeModel('m6', 10),
    makeModel('m7', 10),
  ];

  it('每个模型逐日取值，缺失记 0，最多取 6 个模型', () => {
    const chart = buildTrendChart(days, models);

    expect(chart.labels).toEqual(['9/22', '9/23']);
    expect(chart.series).toHaveLength(6);
    expect(chart.series[0].key).toBe('m1');
    expect(chart.series[0].label).toBe('Moonshot');
    expect(chart.series[0].color).toBe(seriesColor(0));
    expect(chart.series[0].values).toEqual([100, 40]);
    expect(chart.series[1].label).toBe('m2'); // model 为空时回退 key
    expect(chart.series[1].values).toEqual([30, 0]);
    expect(chart.maxValue).toBe(100);
  });

  it('空数据返回空图表', () => {
    expect(buildTrendChart([], [])).toEqual({ labels: [], series: [], maxValue: 0 });
  });
});

describe('buildLinePath', () => {
  it('把两点映射为贴边的平滑曲线', () => {
    expect(buildLinePath([0, 10], 100, 40, 10)).toBe(
      'M 0.00 40.00 C 16.67 33.33, 83.33 6.67, 100.00 0.00',
    );
  });

  it('多点生成逐段三次贝塞尔曲线且首尾贴边', () => {
    const path = buildLinePath([0, 5, 10], 100, 40, 10);

    expect(path.startsWith('M 0.00 40.00')).toBe(true);
    expect(path.endsWith('100.00 0.00')).toBe(true);
    expect(path.match(/C /g) ?? []).toHaveLength(2);
  });

  it('数据不足或最大值为 0 时返回空字符串', () => {
    expect(buildLinePath([], 100, 40, 10)).toBe('');
    expect(buildLinePath([5], 100, 40, 10)).toBe('');
    expect(buildLinePath([0, 10], 100, 40, 0)).toBe('');
  });
});

describe('buildDonutSlices', () => {
  it('空输入或全 0 返回空切片', () => {
    expect(buildDonutSlices([])).toEqual({ slices: [], total: 0 });
    expect(buildDonutSlices([makeModel('m1', 0)])).toEqual({ slices: [], total: 0 });
  });

  it('单片铺满整圆不留间隙', () => {
    const { slices, total } = buildDonutSlices([makeModel('m1', 42)]);

    expect(total).toBe(42);
    expect(slices).toHaveLength(1);
    expect(slices[0].share).toBe(1);
    expect(slices[0].startRatio).toBe(0);
    expect(slices[0].endRatio).toBe(1);
    expect(slices[0].color).toBe(seriesColor(0));
  });

  it('多片按份额排布并留 0.003 圆周间隙', () => {
    const { slices, total } = buildDonutSlices([makeModel('m1', 75), makeModel('m2', 25)]);

    expect(total).toBe(100);
    expect(slices[0].share).toBeCloseTo(0.75);
    expect(slices[0].startRatio).toBeCloseTo(0.003);
    expect(slices[0].endRatio).toBeCloseTo(0.747);
    expect(slices[1].startRatio).toBeCloseTo(0.753);
    expect(slices[1].endRatio).toBeCloseTo(0.997);
    expect(slices[1].color).toBe(seriesColor(1));
  });

  it('超过 6 片时合并尾部为“其他”', () => {
    const models = Array.from({ length: 7 }, (_, index) => makeModel(`m${index}`, 10));
    const { slices, total } = buildDonutSlices(models);

    expect(total).toBe(70);
    expect(slices).toHaveLength(6);
    expect(slices.slice(0, 5).map(slice => slice.key)).toEqual(['m0', 'm1', 'm2', 'm3', 'm4']);
    expect(slices[5].key).toBe('__other__');
    expect(slices[5].label).toBe('其他');
    expect(slices[5].total).toBe(20);
    expect(slices[5].share).toBeCloseTo(20 / 70);
    expect(slices[5].color).toBe(seriesColor(5));
  });

  it('恰好 6 片不合并', () => {
    const models = Array.from({ length: 6 }, (_, index) => makeModel(`m${index}`, 10));
    const { slices } = buildDonutSlices(models);

    expect(slices).toHaveLength(6);
    expect(slices[5].key).toBe('m5');
    expect(slices.some(slice => slice.key === '__other__')).toBe(false);
  });
});

describe('buildLifetimeSummary', () => {
  it('空输入返回零值摘要', () => {
    expect(buildLifetimeSummary([])).toEqual({
      totalTokens: 0,
      peakDay: null,
      activeDays: 0,
      currentStreak: 0,
      longestStreak: 0,
    });
  });

  it('统计峰值、活跃天数与连续段', () => {
    const days = [
      makeDay('2026-09-01', 10),
      makeDay('2026-09-02', 0),
      makeDay('2026-09-03', 4),
      makeDay('2026-09-04', 5),
      makeDay('2026-09-05', 6),
      makeDay('2026-09-06', 0),
    ];
    const summary = buildLifetimeSummary(days);

    expect(summary.totalTokens).toBe(25);
    expect(summary.peakDay).toEqual({ date: '2026-09-01', total: 10 });
    expect(summary.activeDays).toBe(4);
    expect(summary.currentStreak).toBe(0); // 最后一天为 0
    expect(summary.longestStreak).toBe(3);
  });

  it('当前连续段从最后一天往前数', () => {
    const days = [
      makeDay('2026-09-01', 1),
      makeDay('2026-09-02', 2),
      makeDay('2026-09-03', 0),
      makeDay('2026-09-04', 3),
      makeDay('2026-09-05', 4),
    ];
    const summary = buildLifetimeSummary(days);

    expect(summary.currentStreak).toBe(2);
    expect(summary.longestStreak).toBe(2);
    expect(summary.activeDays).toBe(4);
  });

  it('全 0 数据没有峰值日', () => {
    const summary = buildLifetimeSummary([makeDay('2026-09-01', 0), makeDay('2026-09-02', 0)]);

    expect(summary.totalTokens).toBe(0);
    expect(summary.peakDay).toBeNull();
    expect(summary.activeDays).toBe(0);
    expect(summary.longestStreak).toBe(0);
  });
});

describe('日期格式化与 token 计数', () => {
  it('formatDayShort / formatDayFull / formatMonthLabel', () => {
    expect(formatDayShort('2026-09-22')).toBe('9/22');
    expect(formatDayShort('2026-01-05')).toBe('1/5');
    expect(formatDayFull('2026-09-22')).toBe('2026年9月22日');
    expect(formatMonthLabel('2026-09')).toBe('9月');
    expect(formatMonthLabel('2026-12')).toBe('12月');
  });

  it('无效输入回退原字符串', () => {
    expect(formatDayShort('bad-date')).toBe('bad-date');
    expect(formatDayFull('bad-date')).toBe('bad-date');
    expect(formatMonthLabel('bad')).toBe('bad');
    expect(formatMonthLabel('2026-13')).toBe('2026-13');
  });

  it('formatTokenCount 保持原有格式', () => {
    expect(formatTokenCount(999)).toBe('999');
    expect(formatTokenCount(1_250)).toBe('1.3K');
    expect(formatTokenCount(3_400_000)).toBe('3.4M');
    expect(formatTokenCount(2_000_000_000)).toBe('2B');
    expect(formatTokenCount(0)).toBe('0');
  });
});

// ---------------------------------------------------------------------------
// 热力图点选明细
// ---------------------------------------------------------------------------

function modelMetrics(
  key: string,
  prompt: number,
  completion: number,
  cached = 0,
  cachePrompt = 0,
  requests = 1,
): TokenStatisticsModel {
  return {
    key,
    provider: 'test-provider',
    model: key,
    prompt,
    completion,
    cached,
    cache_prompt: cachePrompt,
    total: prompt + completion,
    requests,
    cache_hit_rate: null,
  };
}

const modelA = modelMetrics('alpha/flash', 180, 40, 100, 200, 2);
const modelB = modelMetrics('beta/max', 70, 10, 30, 80, 1);

describe('buildSelectionDetail', () => {
  // 日级字段与模型合计保持一致（真实后端快照就是这么来的）。
  const dayOne: TokenStatisticsDay = {
    date: '2026-09-22',
    prompt: 250,
    completion: 50,
    cached: 130,
    cache_prompt: 280,
    total: 300,
    requests: 3,
    cache_hit_rate: null,
    models: [modelA, modelB],
  };
  const dayTwo = makeDay('2026-09-21', 120, 1, [modelMetrics('alpha/flash', 100, 20, 50, 100, 1)]);

  it('单日只取当天，模型按总量降序并重算命中率', () => {
    const detail = buildSelectionDetail([dayOne, dayTwo], ['2026-09-22']);

    expect(detail).not.toBeNull();
    expect(detail!.title).toBe('2026年9月22日');
    expect(detail!.dates).toEqual(['2026-09-22']);
    expect(detail!.total).toBe(300);
    expect(detail!.requests).toBe(3);
    expect(detail!.models.map(model => model.key)).toEqual(['alpha/flash', 'beta/max']);
    // 130 / 280 ≈ 46.4%，聚合后的命中率必须按合计数重算。
    expect(detail!.cache_hit_rate).toBe(46.4);
    expect(detail!.models[0].total).toBe(220);
  });

  it('多日按模型 key 合并，标题用同年省略年份的中文区间', () => {
    const detail = buildSelectionDetail([dayOne, dayTwo], ['2026-09-22', '2026-09-21']);

    expect(detail!.title).toBe('9月21日 - 9月22日');
    expect(detail!.total).toBe(420);
    expect(detail!.models).toHaveLength(2);
    const merged = detail!.models.find(model => model.key === 'alpha/flash')!;
    expect(merged.total).toBe(340);
    expect(merged.requests).toBe(3);
    expect(merged.cache_hit_rate).toBe(50);
  });

  it('没有命中的日期返回 0 合计与空模型列表', () => {
    const detail = buildSelectionDetail([dayOne], ['2026-01-01']);

    expect(detail).not.toBeNull();
    expect(detail!.total).toBe(0);
    expect(detail!.models).toEqual([]);
    expect(detail!.cache_hit_rate).toBeNull();
  });

  it('空日期集合返回 null', () => {
    expect(buildSelectionDetail([dayOne], [])).toBeNull();
    expect(buildSelectionDetail([dayOne], ['', ''])).toBeNull();
  });
});

describe('buildSelectionTitle', () => {
  it('跨年日期保留年份', () => {
    expect(buildSelectionTitle(['2025-12-31', '2026-01-01'])).toBe('2025年12月31日 - 2026年1月1日');
    expect(buildSelectionTitle(['2026-09-22'])).toBe('2026年9月22日');
  });
});

describe('buildRankingChart', () => {
  it('按总量降序截断，输出总量/输入/输出三条序列', () => {
    const models = [
      modelMetrics('c', 10, 5),
      modelMetrics('a', 200, 40),
      modelMetrics('b', 50, 10),
    ];
    const chart = buildRankingChart(models, 400, 160);

    expect(chart.hasData).toBe(true);
    expect(chart.labels).toEqual(['a', 'b', 'c']);
    expect(chart.series.map(series => series.key)).toEqual(['total', 'prompt', 'completion']);
    expect(chart.series.map(series => series.color)).toEqual([
      seriesColor(0), seriesColor(1), seriesColor(2),
    ]);
    expect(chart.series[0].values).toEqual([240, 60, 15]);
    expect(chart.series[1].values).toEqual([200, 50, 10]);
    expect(chart.maxValue).toBe(240);
    expect(chart.series[0].path.startsWith('M ')).toBe(true);
  });

  it('单个模型没有连线，只留数据点', () => {
    const chart = buildRankingChart([modelMetrics('only', 10, 2)], 400, 160);

    expect(chart.labels).toEqual(['only']);
    expect(chart.series.every(series => series.path === '')).toBe(true);
    expect(chart.series[0].values).toEqual([12]);
  });

  it(`最多保留 ${RANKING_MAX_MODELS} 个模型，零用量模型不参与`, () => {
    const models = [
      modelMetrics('zero', 0, 0),
      ...Array.from({ length: 8 }, (_, index) => modelMetrics(`m${index}`, 100 - index, 0)),
    ];
    const chart = buildRankingChart(models, 400, 160);

    expect(chart.labels).toHaveLength(RANKING_MAX_MODELS);
    expect(chart.labels[0]).toBe('m0');
    expect(chart.labels).not.toContain('zero');
  });

  it('长模型名截断到 12 字符', () => {
    const chart = buildRankingChart([modelMetrics('a-very-long-model-name', 10, 0)], 400, 160);

    expect(chart.labels[0]).toHaveLength(12);
    expect(chart.labels[0].endsWith('…')).toBe(true);
    expect(chart.fullLabels[0]).toBe('a-very-long-model-name');
  });

  it('全零用量为空态', () => {
    const chart = buildRankingChart([modelMetrics('zero', 0, 0)], 400, 160);

    expect(chart.hasData).toBe(false);
    expect(chart.series).toEqual([]);
    expect(chart.maxValue).toBe(0);
  });
});

describe('formatCostCell（成本账本展示）', () => {
  it('有金额时原样显示', () => {
    expect(formatCostCell('¥0.0123', 0)).toBe('¥0.0123');
  });

  it('未计价显示破折号而不是 0', () => {
    expect(formatCostCell(null, 0)).toBe('—');
    expect(formatCostCell(undefined, 0)).toBe('—');
    expect(formatCostCell('', 0)).toBe('—');
  });

  it('含未计价记录时加星号', () => {
    expect(formatCostCell('¥0.0123', 2)).toBe('¥0.0123*');
    expect(formatCostCell(null, 2)).toBe('—*');
  });
});
