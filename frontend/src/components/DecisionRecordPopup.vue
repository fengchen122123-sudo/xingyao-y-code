<template>
  <!-- 会话头部「决策」入口的悬浮面板：展示 JEV 决策记录（只读轻量视图），
       锚定在按钮下方；点面板外部由 App.vue 统一收起 -->
  <div class="decision-pop" data-test="decision-dialog" @click.stop>
    <div class="pop-head">
      <div class="ph-ico"><AppIcon name="brain" :size="15" :stroke-width="2" /></div>
      <div class="ph-title">
        <h3>决策记录</h3>
        <p class="ph-sub">JEV 决策模型调用审计 · 点击卡片展开请求与判定原文</p>
      </div>
      <button
        type="button"
        class="pop-close-btn"
        title="关闭"
        aria-label="关闭"
        data-test="decision-pop-close"
        @click="$emit('close')"
      >
        <AppIcon name="x" :size="14" :stroke-width="2" />
      </button>
    </div>

    <div class="pop-overview">
      <div class="status-line" data-test="decision-pop-status">
        <div class="status-left">
          <span class="dot" :class="{ ok: enabled }"></span>
          <template v-if="enabled">
            <span>已启用</span>
            <span class="model-pill">{{ modelId || 'NeoHorse-Jev-4B' }}</span>
          </template>
          <span v-else>决策模型未启用（可在设置 → 决策模型（JEV）中开启）</span>
        </div>
        <span class="scope-pill" data-test="decision-pop-scope">
          {{ hasRecords ? `当前会话 · 共 ${sessionTotalCount} 次` : '当前会话' }}
        </span>
      </div>

      <div v-if="hasRecords" class="stats-grid" data-test="decision-pop-stats">
        <div class="stat-cell accent" data-test="decision-pop-session-total">
          <span class="k">本会话</span>
          <span class="v">{{ sessionTotalCount }}</span>
        </div>
        <div class="stat-cell">
          <span class="k">本会话今日</span>
          <span class="v">{{ summary!.today_count }}</span>
        </div>
        <div class="stat-cell" :class="{ amber: summary!.today_shadow_count > 0 }">
          <span class="k">影子</span>
          <span class="v">{{ summary!.today_shadow_count }}</span>
        </div>
        <div class="stat-cell" :class="{ green: summary!.today_adopted_count > 0 }">
          <span class="k">采纳</span>
          <span class="v">{{ summary!.today_adopted_count }}</span>
        </div>
        <div class="stat-cell" :class="{ rose: summary!.today_fallback_count > 0 }">
          <span class="k">回退</span>
          <span class="v">{{ summary!.today_fallback_count }}</span>
        </div>
      </div>
      <div v-if="hasRecords && globalTodayHint" class="scope-subline" data-test="decision-pop-global-hint">
        {{ globalTodayHint }}
      </div>
    </div>

    <div class="pop-body">
      <ul v-if="summary && summary.recent.length > 0" class="record-list" data-test="decision-pop-list">
        <li
          v-for="(item, index) in summary.recent"
          :key="item.trace_id || index"
          class="record-card"
          :class="{ shadow: item.shadow, open: expandedIndex === index }"
          data-test="decision-pop-item"
          @click="toggle(index, item)"
        >
          <div class="rec-head" data-test="decision-pop-meta">
            <div class="rec-badges">
              <span class="scene-name" :title="item.scene">{{ sceneLabel(item.scene) }}</span>
              <span class="tag" :class="item.shadow ? 'tag-shadow' : 'tag-live'">{{ item.shadow ? '影子' : '正式' }}</span>
              <span v-if="item.fallback_reason" class="tag tag-fallback" :title="item.fallback_reason">回退：{{ item.fallback_reason }}</span>
              <span v-else-if="item.adopted" class="tag tag-adopted">已采纳</span>
            </div>
            <div class="rec-time-group">
              <span>{{ formatTime(item.ts) }}</span>
              <template v-if="typeof item.latency_ms === 'number'">
                <span>·</span>
                <span>{{ item.latency_ms }}ms</span>
              </template>
              <span class="chevron-ico" :class="{ rotated: expandedIndex === index }">
                <AppIcon name="chevron-right" :size="12" :stroke-width="2" />
              </span>
            </div>
          </div>

          <div v-if="digestText(item.state_digest) || item.answer" class="rec-content" data-test="decision-pop-content">
            <div v-if="digestText(item.state_digest)" class="rec-state">{{ digestText(item.state_digest) }}</div>
            <div v-if="item.answer" class="rec-answer-row">
              <span
                class="answer-pill"
                :class="answerInfo(item.answer, item.scene).tone"
                :title="`模型判定：${item.answer}`"
              >
                <code>→ {{ item.answer }}</code>
                <span v-if="answerInfo(item.answer, item.scene).label" class="zh">{{ answerInfo(item.answer, item.scene).label }}</span>
              </span>
              <span v-if="item.trace_id && expandedIndex !== index" class="expand-hint">点击展开详情</span>
            </div>
          </div>

          <div v-if="expandedIndex === index" class="rec-detail" data-test="decision-pop-detail" @click.stop>
            <div v-if="detailLoading" class="detail-hint">读取中…</div>
            <div v-else-if="detailError" class="detail-hint">{{ detailError }}</div>
            <template v-else-if="detail">
              <div v-if="detail.trace_id || item.trace_id || detail.model_id || item.model_id" class="detail-meta-strip">
                <span v-if="detail.trace_id || item.trace_id">trace_id: {{ detail.trace_id || item.trace_id }}</span>
                <span v-if="detail.model_id || item.model_id">{{ detail.model_id || item.model_id }}</span>
              </div>
              <div class="detail-block">
                <div class="detail-title">发给 JEV 的请求</div>
                <pre v-if="detail.request" class="detail-json" data-test="decision-pop-request">{{ prettyRequest(detail) }}</pre>
                <div v-else class="detail-hint">这条记录来自旧版本，没有保存请求快照</div>
              </div>
              <div class="detail-block">
                <div class="detail-title">JEV 的判定</div>
                <pre class="detail-json" data-test="decision-pop-answers">{{ prettyAnswers(detail) }}</pre>
              </div>
            </template>
          </div>
        </li>
      </ul>
      <div v-else class="pop-empty" data-test="decision-pop-empty">
        本会话还没有决策记录。启用后在设置里跑一次探针就会产生记录；业务场景接入后每次决策都会出现在这里。
      </div>
    </div>

    <div class="pop-foot">
      <div class="foot-left">
        <span class="foot-hint">{{ footHint }}</span>
        <button
          v-if="canLoadAll"
          type="button"
          class="foot-more-btn"
          data-test="decision-pop-load-more"
          @click="loadMore"
        >
          查看全部 {{ sessionTotalCount }} 条
        </button>
      </div>
      <div class="foot-actions">
        <button type="button" class="pop-btn pop-btn-secondary" data-test="decision-pop-open-audit" @click="openAuditDir">
          <AppIcon name="folder-open" :size="13" :stroke-width="2" />
          <span>打开审计目录</span>
        </button>
        <button type="button" class="pop-btn pop-btn-accent" data-test="decision-pop-open-settings" @click="openSettings">
          <AppIcon name="settings" :size="13" :stroke-width="2" />
          <span>去设置</span>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';

import { useAppStore } from '../stores/appStore';
import type { DecisionAuditRecentItem, DecisionAuditSummary, DecisionRecordDetail } from '../types/pywebview';
import AppIcon from './AppIcon.vue';

// 头部「决策」入口的悬浮面板：挂载时拉一次审计汇总与启用状态；
// 任何失败都静默（面板只做观测展示，绝不阻塞或报错）。
const emit = defineEmits<{ close: [] }>();

const store = useAppStore();
const summary = ref<DecisionAuditSummary | null>(null);
const enabled = ref(false);
const modelId = ref('');
const displayLimit = ref(10);

const SCENE_LABELS: Record<string, string> = {
  router: '路由（选模型）',
  secretary: '秘书（派会话）',
  search: '搜索（定条数）',
  safety: '安全（判命令）',
  auto_permission: '权限（自动决策）',
  auto_question: '提问（自动决策）',
  probe: '探针（连通性）',
  loop_guard: '循环止损',
  subagent_review: '子代理验收',
  plan_recovery: '计划恢复',
  memory_filter: '记忆筛选',
};

function sceneLabel(scene: string): string {
  return SCENE_LABELS[scene] || scene || '未知场景';
}

type AnswerTone = 'good' | 'warn' | 'bad' | 'accent';

const ANSWER_META: Record<string, { label: string; tone: AnswerTone }> = {
  accepted: { label: '验收通过', tone: 'good' },
  incomplete: { label: '待补全', tone: 'warn' },
  failed: { label: '未通过', tone: 'bad' },
  continue: { label: '继续排查', tone: 'accent' },
  pivot: { label: '改换思路', tone: 'warn' },
  ask_user: { label: '人工确认', tone: 'warn' },
  retry_same: { label: '直接重试', tone: 'accent' },
  split_smaller: { label: '拆小重试', tone: 'warn' },
  escalate_user: { label: '升级确认', tone: 'warn' },
  keep_all: { label: '全部保留', tone: 'good' },
  keep_facts_only: { label: '仅留事实', tone: 'accent' },
  skip_all: { label: '跳过写入', tone: 'warn' },
  allow: { label: '自动放行', tone: 'good' },
  deny: { label: '拒绝执行', tone: 'bad' },
  main: { label: '使用主模型', tone: 'accent' },
  secondary: { label: '切副模型', tone: 'good' },
  new: { label: '新建会话', tone: 'accent' },
};

function answerInfo(answer?: string, scene?: string): { label: string; tone: AnswerTone } {
  const key = String(answer || '').trim();
  if (!key) return { label: '', tone: 'accent' };
  if (scene === 'auto_question') {
    if (key === 'ask_user') return { label: '人工回答', tone: 'warn' };
    if (/^option_\d+$/.test(key)) return { label: '自动作答', tone: 'accent' };
  }
  if (scene === 'secretary' && key !== 'new') {
    return { label: '派给已有会话', tone: 'accent' };
  }
  return ANSWER_META[key] || { label: '', tone: 'accent' };
}

function formatTime(ts: string): string {
  return ts.length >= 19 ? ts.slice(11, 19) : ts;
}

/** 事由摘要：保留完整文本并自然换行，避免横向滚动截断 */
function digestText(text?: string): string {
  return String(text || '').trim();
}

const hasRecords = computed(() => (summary.value?.recent.length ?? 0) > 0);

const sessionTotalCount = computed(() => {
  const s = summary.value;
  if (!s) return 0;
  if (typeof s.session_total_count === 'number') return s.session_total_count;
  return Math.max(Number(s.today_count ?? 0), s.recent.length);
});

const globalTodayHint = computed(() => {
  const s = summary.value;
  if (!s || typeof s.global_today_count !== 'number') return '';
  return `以上统计仅计当前会话（全部会话今日共 ${s.global_today_count} 次）`;
});

const canLoadAll = computed(() => {
  const shown = summary.value?.recent.length ?? 0;
  return sessionTotalCount.value > shown && shown < 200;
});

const footHint = computed(() => {
  const count = summary.value?.recent.length ?? 0;
  if (count > 0) {
    if (typeof summary.value?.session_total_count === 'number') {
      return `本会话共 ${sessionTotalCount.value} 条 · 显示最近 ${count} 条`;
    }
    return `显示最近 ${count} 条 · 当前会话`;
  }
  return '审计按日保存在本地';
});

const expandedIndex = ref<number | null>(null);
const detail = ref<DecisionRecordDetail['record'] | null>(null);
const detailLoading = ref(false);
const detailError = ref('');
// 详情请求序号：连点两条记录时，先发的请求可能后返回，不能让它的结果
// 覆盖后一条的详情（共享 detail/detailLoading，必须靠序号判归属）
let detailRequestSeq = 0;

function collapse() {
  // 收起时让在途请求作废：迟到的响应不能把已收起的面板重新填上内容
  detailRequestSeq += 1;
  expandedIndex.value = null;
  detail.value = null;
  detailError.value = '';
  detailLoading.value = false;
}

async function toggle(index: number, item: DecisionAuditRecentItem) {
  if (expandedIndex.value === index) {
    collapse();
    return;
  }
  expandedIndex.value = index;
  detail.value = null;
  detailError.value = '';
  const traceId = String(item.trace_id || '').trim();
  if (!traceId) {
    detailError.value = '这条记录没有 trace_id，读不到详情（旧版本记录）';
    return;
  }
  const seq = (detailRequestSeq += 1);
  detailLoading.value = true;
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_record_detail) {
      if (seq === detailRequestSeq) detailError.value = '当前版本不支持查看详情';
      return;
    }
    const res = await api.get_decision_record_detail(traceId);
    if (seq !== detailRequestSeq) return;
    if (res?.ok && res.record) {
      detail.value = res.record;
    } else {
      detailError.value = res?.msg || '未找到这条决策记录';
    }
  } catch {
    if (seq === detailRequestSeq) detailError.value = '读取详情失败';
  } finally {
    if (seq === detailRequestSeq) detailLoading.value = false;
  }
}

function prettyRequest(record: DecisionRecordDetail['record']): string {
  if (!record) return '';
  const request = record.request || {};
  return JSON.stringify(
    {
      model: request.model ?? record.model_id ?? '',
      state: request.state ?? '',
      questions: request.questions ?? null,
    },
    null,
    2,
  );
}

function prettyAnswers(record: DecisionRecordDetail['record']): string {
  if (!record) return '';
  return JSON.stringify(record.answers ?? null, null, 2);
}

// 汇总请求序号：切换会话时旧请求可能后返回，不能让它的结果落到新会话面板上
let summaryRequestSeq = 0;

async function loadSummary() {
  // 按当前会话拉取（session_only=true）：today_*/recent 都只算本会话，
  // 否则面板会把别的会话的「判的是什么事 / 判成什么」显示在当前会话下。
  // 会话名为空（新会话懒创建阶段）时不请求，也不回退到全局口径。
  const sessionName = String(store.currentSessionName || '').trim();
  // 列表刷新后索引会变，展开态必须收起，否则会张冠李戴
  collapse();
  const seq = (summaryRequestSeq += 1);
  if (!sessionName) {
    summary.value = null;
    return;
  }
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_audit_summary) {
      summary.value = null;
      return;
    }
    const res = await api.get_decision_audit_summary(displayLimit.value, sessionName, true);
    if (seq !== summaryRequestSeq) return;
    summary.value = res?.ok ? res : null;
  } catch {
    if (seq === summaryRequestSeq) summary.value = null;
  }
}

async function loadSettings() {
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_settings) return;
    const res = await api.get_decision_settings();
    if (res?.ok && res.settings) {
      enabled.value = res.settings.enabled === true;
      modelId.value = String(res.settings.model_id ?? '');
    }
  } catch {
  }
}

async function openAuditDir() {
  try {
    const api = window.pywebview?.api;
    if (!api?.open_decision_audit_dir) return;
    await api.open_decision_audit_dir();
  } catch {
  }
}

function openSettings() {
  store.settingsSection = 'decision';
  store.settingsOpen = true;
  emit('close');
}

onMounted(() => {
  loadSummary();
  loadSettings();
  store.decisionUnread = 0;
});

watch(
  () => store.decisionEventTick,
  () => {
    loadSummary();
    store.decisionUnread = 0;
  },
);

function loadMore() {
  displayLimit.value = Math.min(200, Math.max(50, sessionTotalCount.value));
  void loadSummary();
}

watch(
  () => store.currentSessionName,
  () => {
    displayLimit.value = 10;
    loadSummary();
  },
);
</script>

<style scoped>
.decision-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 520px;
  max-width: min(520px, calc(100vw - 32px));
  background: var(--xy-card, var(--float-bg));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 14px;
  box-shadow: var(--xy-shadow-pop, 0 12px 32px rgba(0, 0, 0, 0.35));
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(620px, 72vh);
  overflow: hidden;
  text-align: left;
  color: var(--xy-text, var(--text));
}

.pop-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  flex-shrink: 0;
}
.ph-ico {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  flex: none;
}
.ph-title {
  flex: 1;
  min-width: 0;
}
.ph-title h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  line-height: 1.3;
  color: var(--xy-text, var(--text));
}
.ph-sub {
  margin: 2px 0 0;
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
  line-height: 1.35;
}

.pop-close-btn {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text-2, var(--text-dim));
  display: grid;
  place-items: center;
  flex: none;
  cursor: pointer;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.pop-close-btn:hover {
  background: var(--xy-hover, var(--bg3));
  border-color: var(--xy-border-strong, var(--border));
  color: var(--xy-text, var(--text));
}

.pop-overview {
  padding: 10px 14px;
  border-bottom: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}
.status-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--xy-text-2, var(--text-dim));
  font-size: 12px;
}
.status-left {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  flex-wrap: wrap;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--xy-text-3, var(--text-faint));
  flex: none;
}
.dot.ok {
  background: var(--xy-green, var(--green));
  box-shadow: 0 0 0 3px var(--xy-green-soft, color-mix(in srgb, var(--green) 16%, transparent));
}
.model-pill {
  font-family: var(--mono);
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 6px;
  background: var(--xy-card, var(--bg3));
  border: 1px solid var(--xy-border, var(--border));
  color: var(--xy-text, var(--text));
}
.scope-pill {
  font-size: 10.5px;
  color: var(--xy-text-3, var(--text-faint));
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-card, var(--bg3));
  flex: none;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 6px;
}
.stat-cell {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 5px 8px;
  border-radius: 7px;
  background: var(--xy-card, var(--bg3));
  border: 1px solid var(--xy-border, var(--border));
  font-size: 11px;
}
.stat-cell .k {
  color: var(--xy-text-3, var(--text-faint));
  white-space: nowrap;
}
.stat-cell .v {
  font-family: var(--mono);
  font-size: 13px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--xy-text, var(--text));
}
.stat-cell.accent .v {
  color: var(--xy-accent, var(--accent));
}
.stat-cell.amber .v {
  color: var(--xy-amber, var(--orange));
}
.stat-cell.green .v {
  color: var(--xy-green, var(--green));
}
.stat-cell.rose .v {
  color: var(--xy-rose, var(--red));
}
.scope-subline {
  font-size: 10.5px;
  color: var(--xy-text-3, var(--text-faint));
  line-height: 1.35;
}

/* 列表区域：仅纵向滚动，杜绝横向溢出 */
.pop-body {
  padding: 10px 14px;
  overflow-y: auto;
  overflow-x: hidden;
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}
.record-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.record-card {
  padding: 9px 11px;
  border-radius: 10px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  cursor: pointer;
  min-width: 0;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
}
.record-card:hover {
  background: var(--xy-hover, var(--bg3));
  border-color: var(--xy-border-strong, var(--border));
}
.record-card.open {
  background: var(--xy-card, var(--bg3));
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 45%, transparent);
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.06);
}

.rec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.rec-badges {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-wrap: wrap;
  min-width: 0;
}
.scene-name {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--xy-text, var(--text));
}
.tag {
  font-size: 10.5px;
  font-weight: 500;
  padding: 1px 6px;
  border-radius: 5px;
  line-height: 1.45;
}
.tag-shadow {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 13%, transparent));
  color: var(--xy-amber, var(--orange));
}
.tag-live {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.tag-adopted {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 13%, transparent));
  color: var(--xy-green, var(--green));
}
.tag-fallback {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 13%, transparent));
  color: var(--xy-rose, var(--red));
}

.rec-time-group {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-family: var(--mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  color: var(--xy-text-3, var(--text-faint));
  flex: none;
}
.chevron-ico {
  color: var(--xy-text-3, var(--text-faint));
  display: inline-flex;
  transition: transform 0.16s ease;
}
.chevron-ico.rotated {
  transform: rotate(90deg);
}

.rec-content {
  margin-top: 6px;
  min-width: 0;
}
.rec-state {
  font-size: 12px;
  line-height: 1.55;
  color: var(--xy-text-2, var(--text-dim));
  white-space: pre-line;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.rec-answer-row {
  margin-top: 7px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.answer-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  font-weight: 500;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.answer-pill.good {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 13%, transparent));
  color: var(--xy-green, var(--green));
}
.answer-pill.warn {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 13%, transparent));
  color: var(--xy-amber, var(--orange));
}
.answer-pill.bad {
  background: var(--xy-rose-soft, color-mix(in srgb, var(--red) 13%, transparent));
  color: var(--xy-rose, var(--red));
}
.answer-pill code {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 600;
  background: transparent;
  padding: 0;
}
.answer-pill .zh {
  font-size: 11px;
  opacity: 0.9;
}
.expand-hint {
  font-size: 10.5px;
  color: var(--xy-text-3, var(--text-faint));
  opacity: 0;
  transition: opacity 0.15s;
}
.record-card:hover .expand-hint {
  opacity: 1;
}

.rec-detail {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--xy-border-strong, var(--border));
  display: flex;
  flex-direction: column;
  gap: 8px;
  cursor: default;
}
.detail-meta-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--xy-text-3, var(--text-faint));
}
.detail-title {
  color: var(--xy-text-2, var(--text-dim));
  font-size: 11px;
  font-weight: 600;
  margin-bottom: 4px;
}
.detail-hint {
  color: var(--xy-text-3, var(--text-faint));
  font-size: 11px;
}
.detail-json {
  margin: 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--code-bg, var(--bg0));
  border: 1px solid var(--xy-border, var(--border));
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  max-height: 180px;
  overflow-y: auto;
  overflow-x: hidden;
  color: var(--xy-text-2, var(--text-dim));
}

.pop-empty {
  color: var(--xy-text-3, var(--text-faint));
  font-size: 12px;
  line-height: 1.7;
  padding: 12px 4px;
}

.pop-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 14px;
  border-top: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg2));
  flex-shrink: 0;
  flex-wrap: wrap;
}
.foot-left {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.foot-hint {
  font-size: 11px;
  color: var(--xy-text-3, var(--text-faint));
}
.foot-more-btn {
  border: none;
  background: transparent;
  padding: 0;
  font-family: inherit;
  font-size: 11px;
  font-weight: 500;
  color: var(--xy-accent, var(--accent));
  cursor: pointer;
}
.foot-more-btn:hover {
  text-decoration: underline;
}
.foot-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
.pop-btn {
  height: 30px;
  padding: 0 11px;
  border-radius: 8px;
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.pop-btn-secondary {
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-card, var(--bg3));
  color: var(--xy-text-2, var(--text-dim));
}
.pop-btn-secondary:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text));
}
.pop-btn-accent {
  border: 1px solid color-mix(in srgb, var(--xy-accent, var(--accent)) 30%, transparent);
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}
.pop-btn-accent:hover {
  background: color-mix(in srgb, var(--xy-accent, var(--accent)) 16%, transparent);
}
</style>
