<template>
  <div data-test="token-statistics-page-legacy" class="token-statistics-page" :aria-busy="store.tokenStatisticsLoading">
    <header class="usage-header">
      <div>
        <div class="eyebrow">USAGE ANALYTICS</div>
        <h1>Token 统计</h1>
        <p>与 CLI /usage 共用真实服务端用量，按模型查看输入、输出与缓存命中。</p>
      </div>
      <div class="usage-actions">
        <div class="range-switch" role="group" aria-label="统计时间范围">
          <button
            v-for="option in ranges"
            :key="option.days"
            :data-test="`range-${option.days}`"
            :class="{ active: store.tokenStatisticsDays === option.days }"
            :aria-pressed="store.tokenStatisticsDays === option.days"
            :disabled="store.tokenStatisticsLoading"
            @click="selectRange(option.days)"
          >{{ option.label }}</button>
        </div>
        <button class="refresh-button" :disabled="store.tokenStatisticsLoading" @click="refresh">
          <span :class="{ spinning: store.tokenStatisticsLoading }">↻</span> 刷新
        </button>
      </div>
    </header>

    <div v-if="store.tokenStatisticsLoading && !statistics" class="usage-loading" role="status" aria-live="polite">
      <span class="loading-orbit"></span>
      <p>正在读取本地 Token 统计…</p>
    </div>

    <template v-else-if="statistics">
      <section class="summary-grid" aria-label="Token 用量概览">
        <article data-test="token-summary-card" class="summary-card total-card">
          <span class="summary-label">总 Token</span>
          <strong :title="String(statistics.summary.total)">{{ formatTokenCount(statistics.summary.total) }}</strong>
          <small>{{ statistics.summary.requests.toLocaleString('zh-CN') }} 次真实请求</small>
        </article>
        <article data-test="token-summary-card" class="summary-card">
          <span class="summary-label">输入 Token</span>
          <strong :title="String(statistics.summary.prompt)">{{ formatTokenCount(statistics.summary.prompt) }}</strong>
          <small>Prompt 与上下文输入</small>
        </article>
        <article data-test="token-summary-card" class="summary-card">
          <span class="summary-label">输出 Token</span>
          <strong :title="String(statistics.summary.completion)">{{ formatTokenCount(statistics.summary.completion) }}</strong>
          <small>模型生成内容</small>
        </article>
        <article data-test="token-summary-card" class="summary-card cache-card">
          <span class="summary-label">缓存命中</span>
          <strong :title="String(statistics.summary.cached)">{{ formatTokenCount(statistics.summary.cached) }}</strong>
          <small>{{ formatRate(statistics.summary.cache_hit_rate) }} 命中率</small>
        </article>
      </section>

      <section class="usage-section daily-section">
        <div class="section-heading">
          <div>
            <h2>每日用量</h2>
            <p>{{ statistics.range.start_date }} 至 {{ statistics.range.end_date }}</p>
          </div>
          <div v-if="statistics.models.length" class="model-legend">
            <span v-for="model in statistics.models" :key="model.key">
              <i :style="{ background: modelColor(model.key) }"></i>{{ model.model || model.key }}
            </span>
          </div>
        </div>

        <div :class="['daily-grid', { compact: statistics.range.days === 30 }]">
          <article v-for="day in statistics.days" :key="day.date" :class="['day-card', { empty: day.total === 0 }]">
            <div
              class="usage-ring"
              role="img"
              :aria-label="dayAriaLabel(day)"
              :style="{ background: donutGradient(day.models) }"
            >
              <div class="ring-center">
                <strong :title="String(day.total)">{{ formatTokenCount(day.total) }}</strong>
                <span>Token</span>
              </div>
            </div>
            <div class="day-meta">
              <b>{{ formatDay(day.date) }}</b>
              <span>{{ day.requests }} 次请求</span>
            </div>
          </article>
        </div>

        <div v-if="statistics.summary.total === 0" class="usage-empty">
          <span>◌</span>
          <div><b>暂无 Token 用量记录</b><p>统计会从模型返回真实 usage 后自动出现。</p></div>
        </div>
      </section>

      <section class="usage-section model-breakdown">
        <div class="section-heading">
          <div><h2>模型用量</h2><p>按总 Token 从高到低排列</p></div>
          <span class="model-count">{{ statistics.models.length }} 个模型</span>
        </div>
        <div v-if="statistics.models.length" class="usage-table-wrap">
          <table class="usage-table">
            <thead>
              <tr>
                <th scope="col">模型</th><th scope="col">输入</th><th scope="col">输出</th>
                <th scope="col">缓存输入</th><th scope="col">命中率</th><th scope="col">请求</th><th scope="col">合计</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="model in statistics.models"
                :key="model.key"
                data-test="model-usage-row"
                :title="`${model.key} · ${model.total} Token`"
              >
                <td>
                  <div class="model-name-cell">
                    <i :style="{ background: modelColor(model.key) }"></i>
                    <span><b>{{ model.model || model.key }}</b><small>{{ model.provider || '未记录厂商' }}</small></span>
                  </div>
                </td>
                <td :title="String(model.prompt)">{{ formatTokenCount(model.prompt) }}</td>
                <td :title="String(model.completion)">{{ formatTokenCount(model.completion) }}</td>
                <td :title="String(model.cached)">{{ formatTokenCount(model.cached) }}</td>
                <td>{{ formatRate(model.cache_hit_rate) }}</td>
                <td>{{ model.requests.toLocaleString('zh-CN') }}</td>
                <td :title="String(model.total)"><strong>{{ formatTokenCount(model.total) }}</strong></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="table-empty">当前范围内暂无模型用量</div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
// 旧版 Token 统计页（2026-09-22 按 ZCode 风格改版前的样式），保留用于对比，去留待定。
import { computed, onMounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { TokenStatisticsDay } from '../types/pywebview';
import { formatTokenCount } from '../utils/tokenStatistics';
import { donutGradient, modelColor } from '../utils/tokenStatisticsLegacy';

const store = useAppStore();
const statistics = computed(() => store.tokenStatistics);
const ranges = [
  { days: 1 as const, label: '今日' },
  { days: 7 as const, label: '近 7 天' },
  { days: 30 as const, label: '近 30 天' },
];

function formatRate(value: number | null) {
  return value === null ? '--' : `${value.toFixed(1)}%`;
}

function formatDay(value: string) {
  const [, month, day] = value.split('-');
  return `${Number(month)}月${Number(day)}日`;
}

function dayAriaLabel(day: TokenStatisticsDay) {
  const models = day.models.length
    ? day.models.map(model => `${model.model || model.key} ${model.total} Token`).join('，')
    : '无模型用量';
  return `${day.date}，${day.total} Token，${models}`;
}

async function selectRange(days: 1 | 7 | 30) {
  await store.loadTokenStatistics(days, true);
}

async function refresh() {
  await store.loadTokenStatistics(store.tokenStatisticsDays, true);
}

onMounted(() => {
  void store.loadTokenStatistics(
    store.tokenStatistics ? store.tokenStatisticsDays : 30,
    false,
  );
});
</script>

<style scoped>
.token-statistics-page { max-width: 1180px; margin: 0 auto; padding-bottom: 48px; }
.usage-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 28px; }
.eyebrow { margin-bottom: 9px; color: var(--accent); font-size: 10px; font-weight: 700; letter-spacing: 1.8px; }
.usage-header h1 { margin: 0 0 9px; font-size: 32px; letter-spacing: -1px; }
.usage-header p, .section-heading p, .usage-empty p { margin: 0; color: var(--text-faint); font-size: 12px; }
.usage-actions { display: flex; align-items: center; gap: 9px; }
.range-switch { display: flex; padding: 3px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg1); }
.range-switch button, .refresh-button { border: 0; color: var(--text-dim); background: transparent; cursor: pointer; }
.range-switch button { padding: 7px 12px; border-radius: 7px; font-size: 12px; }
.range-switch button.active { color: var(--text); background: var(--bg3); box-shadow: 0 1px 7px rgba(0, 0, 0, .18); }
.refresh-button { display: flex; align-items: center; gap: 5px; padding: 9px 12px; border: 1px solid var(--border); border-radius: 10px; }
button:disabled { opacity: .5; cursor: default; }
.spinning { display: inline-block; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
.summary-card { position: relative; overflow: hidden; min-height: 116px; padding: 18px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 14px; background: linear-gradient(145deg, var(--bg2), var(--bg1)); }
.summary-card::after { content: ''; position: absolute; right: -32px; top: -40px; width: 110px; height: 110px; border-radius: 50%; background: color-mix(in srgb, var(--accent) 9%, transparent); }
.summary-label { display: block; margin-bottom: 10px; color: var(--text-faint); font-size: 11px; }
.summary-card strong { display: block; margin-bottom: 7px; font-size: 26px; letter-spacing: -.7px; }
.summary-card small { color: var(--text-faint); font-size: 10.5px; }
.total-card { border-color: color-mix(in srgb, var(--accent) 28%, var(--border)); }
.cache-card strong { color: var(--green); }
.usage-section { margin-top: 18px; padding: 20px; border: 1px solid var(--border); border-radius: 15px; background: var(--bg1); }
.section-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 18px; }
.section-heading h2 { margin: 0 0 5px; font-size: 15px; }
.model-legend { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px 14px; max-width: 65%; }
.model-legend span { display: flex; align-items: center; gap: 6px; color: var(--text-dim); font-size: 10.5px; }
.model-legend i, .model-name-cell > i { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
.daily-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.daily-grid.compact { grid-template-columns: repeat(auto-fill, minmax(92px, 1fr)); }
.day-card { display: grid; justify-items: center; gap: 11px; min-width: 0; padding: 15px 8px 12px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg2); }
.daily-grid.compact .day-card { padding: 12px 5px 10px; }
.usage-ring { position: relative; width: 82px; height: 82px; border-radius: 50%; box-shadow: inset 0 0 0 1px rgba(255,255,255,.04); }
.daily-grid.compact .usage-ring { width: 66px; height: 66px; }
.ring-center { position: absolute; inset: 9px; display: grid; place-content: center; text-align: center; border-radius: 50%; background: var(--bg2); }
.ring-center strong { max-width: 58px; overflow: hidden; text-overflow: ellipsis; font-size: 13px; }
.ring-center span { margin-top: 1px; color: var(--text-faint); font-size: 8px; text-transform: uppercase; }
.day-card.empty .ring-center strong { color: var(--text-faint); }
.day-meta { display: grid; gap: 3px; text-align: center; }
.day-meta b { font-size: 11px; font-weight: 600; }
.day-meta span { color: var(--text-faint); font-size: 9.5px; }
.usage-empty { display: flex; align-items: center; justify-content: center; gap: 12px; margin-top: 16px; padding: 18px; border: 1px dashed var(--border); border-radius: 12px; }
.usage-empty > span { color: var(--text-faint); font-size: 28px; }
.usage-empty b { display: block; margin-bottom: 4px; font-size: 12px; }
.model-count { color: var(--text-faint); font-size: 11px; }
.usage-table-wrap { overflow: hidden; border: 1px solid var(--border); border-radius: 11px; }
.usage-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
.usage-table th { padding: 10px 13px; color: var(--text-faint); background: var(--bg2); font-size: 9.5px; font-weight: 500; text-align: right; text-transform: uppercase; }
.usage-table th:first-child { width: 28%; text-align: left; }
.usage-table td { height: 48px; padding: 0 13px; border-top: 1px solid var(--border); color: var(--text-dim); font-size: 11px; text-align: right; }
.usage-table td:first-child { text-align: left; }
.usage-table td strong { color: var(--text); }
.model-name-cell { display: flex; align-items: center; gap: 9px; min-width: 0; }
.model-name-cell span { display: grid; min-width: 0; }
.model-name-cell b, .model-name-cell small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-name-cell b { color: var(--text); font-size: 11.5px; }
.model-name-cell small { margin-top: 2px; color: var(--text-faint); font-size: 9.5px; }
.table-empty { padding: 30px; text-align: center; color: var(--text-faint); font-size: 11px; }
.usage-loading { min-height: 320px; display: grid; place-content: center; justify-items: center; gap: 14px; color: var(--text-faint); font-size: 12px; }
.loading-orbit { width: 34px; height: 34px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; }
@media (max-width: 1050px) {
  .summary-grid { grid-template-columns: repeat(2, 1fr); }
  .usage-table-wrap { overflow-x: auto; }
  .usage-table { min-width: 760px; }
}
@media (max-width: 760px) {
  .usage-header { align-items: stretch; flex-direction: column; }
  .usage-actions { justify-content: space-between; }
  .model-legend { max-width: 100%; justify-content: flex-start; }
  .section-heading { flex-direction: column; }
}
</style>
