<template>
  <!-- 会话头部「决策」入口的悬浮面板：展示 JEV 决策记录（只读轻量视图），
       锚定在按钮下方；点外部/Esc 由 App.vue 统一收起 -->
  <div class="decision-pop" data-test="decision-dialog" @click.stop>
    <div class="pop-head">
      <div class="ph-ico"><AppIcon name="brain" :size="14" :stroke-width="2" /></div>
      <div class="ph-title">
        <h3>决策记录</h3>
        <p class="ph-sub">JEV 决策模型调用记录（每条记录显示判定的事由与结论）</p>
      </div>
      <button class="chip-btn" title="关闭" @click="$emit('close')"><AppIcon name="x" :size="12" :stroke-width="2" /> 关闭</button>
    </div>

    <div class="pop-body">
      <div class="status-line" data-test="decision-pop-status">
        <span class="dot" :class="{ ok: enabled }"></span>
        <span>{{ statusText }}</span>
      </div>

      <template v-if="summary && summary.today_count > 0">
        <div class="stats-line" data-test="decision-pop-stats">
          今日 {{ summary.today_count }} 次决策 · 影子 {{ summary.today_shadow_count }} · 采纳 {{ summary.today_adopted_count }} · 回退 {{ summary.today_fallback_count }}
        </div>
        <ul class="record-list" data-test="decision-pop-list">
          <li
            v-for="(item, index) in summary.recent"
            :key="index"
            :class="{ shadow: item.shadow, open: expandedIndex === index }"
            data-test="decision-pop-item"
            @click="toggle(index, item)"
          >
            <div class="rec-meta" data-test="decision-pop-meta">{{ line(item) }}</div>
            <div v-if="item.state_digest || item.answer" class="rec-content" data-test="decision-pop-content">
              <span class="rec-state" :title="item.state_digest || ''">{{ clamp(item.state_digest) }}</span>
              <span v-if="item.answer" class="rec-answer" :title="`模型判定：${item.answer}`">→ {{ item.answer }}</span>
            </div>
            <div v-if="expandedIndex === index" class="rec-detail" data-test="decision-pop-detail" @click.stop>
              <div v-if="detailLoading" class="detail-hint">读取中…</div>
              <div v-else-if="detailError" class="detail-hint">{{ detailError }}</div>
              <template v-else-if="detail">
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
      </template>
      <div v-else class="pop-empty" data-test="decision-pop-empty">
        本会话还没有决策记录。启用后在设置里跑一次探针就会产生记录；业务场景接入后每次决策都会出现在这里。
      </div>
    </div>

    <div class="pop-foot">
      <button class="chip-btn" data-test="decision-pop-open-settings" @click="openSettings">去设置</button>
      <button class="chip-btn" data-test="decision-pop-open-audit" @click="openAuditDir">打开审计目录</button>
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

const statusText = computed(() => {
  if (!enabled.value) return '决策模型未启用（可在设置 → 决策模型（JEV）中开启）';
  return `已启用 · 模型 ${modelId.value || 'NeoHorse-Jev-4B'}`;
});

/** 场景中文标签：面板第一眼要能看出这次决策是在判什么 */
const SCENE_LABELS: Record<string, string> = {
  router: '路由（选模型）',
  secretary: '秘书（派会话）',
  search: '搜索（定条数）',
  safety: '安全（判命令）',
  probe: '探针（连通性）',
};

function sceneLabel(scene: string): string {
  return SCENE_LABELS[scene] || scene || '未知场景';
}

/** 单条记录展示行：时间 · 场景 · 模式 · 延迟 · 采纳/回退 */
function line(item: DecisionAuditRecentItem): string {
  const time = item.ts.length >= 19 ? item.ts.slice(11, 19) : item.ts;
  const parts = [time, sceneLabel(item.scene), item.shadow ? '影子' : '正式'];
  if (typeof item.latency_ms === 'number') parts.push(`${item.latency_ms}ms`);
  if (item.fallback_reason) parts.push(`回退：${item.fallback_reason}`);
  else if (item.adopted) parts.push('已采纳');
  return parts.join(' · ');
}

/** 输入摘要截断：完整内容放在 title 里，列表里只留一行 */
function clamp(text?: string, max = 72): string {
  const value = String(text || '').trim();
  if (!value) return '';
  return value.length > max ? `${value.slice(0, max)}…` : value;
}

// ---------------------------------------------------------------------------
// 点开一条记录：按 trace_id 回查「发给 JEV 的请求」与「JEV 的判定」原文
// ---------------------------------------------------------------------------
const expandedIndex = ref<number | null>(null);
const detail = ref<DecisionRecordDetail['record'] | null>(null);
const detailLoading = ref(false);
const detailError = ref('');

function collapse() {
  expandedIndex.value = null;
  detail.value = null;
  detailError.value = '';
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
  detailLoading.value = true;
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_record_detail) {
      detailError.value = '当前版本不支持查看详情';
      return;
    }
    const res = await api.get_decision_record_detail(traceId);
    if (res?.ok && res.record) {
      detail.value = res.record;
    } else {
      detailError.value = res?.msg || '未找到这条决策记录';
    }
  } catch {
    detailError.value = '读取详情失败';
  } finally {
    detailLoading.value = false;
  }
}

/** 请求体：只展示真正发出去的三个字段，缺失的省略成 null 而不是报错 */
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

/** 响应体：上游原样返回的 answers（含 choice 与 probabilities） */
function prettyAnswers(record: DecisionRecordDetail['record']): string {
  if (!record) return '';
  return JSON.stringify(record.answers ?? null, null, 2);
}

async function loadSummary() {
  // 按当前会话拉取：入口/面板口径是「本会话的决策记录」，会话名为空
  // （新会话懒创建阶段）时不请求，也不回退全局口径
  const sessionName = String(store.currentSessionName || '').trim();
  // 列表刷新后索引会变，展开态必须收起，否则会张冠李戴
  collapse();
  if (!sessionName) {
    summary.value = null;
    return;
  }
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_audit_summary) return;
    const res = await api.get_decision_audit_summary(10, sessionName);
    if (res?.ok) {
      summary.value = {
        ok: true,
        today_count: Number(res.today_count ?? 0),
        today_shadow_count: Number(res.today_shadow_count ?? 0),
        today_adopted_count: Number(res.today_adopted_count ?? 0),
        today_fallback_count: Number(res.today_fallback_count ?? 0),
        recent: Array.isArray(res.recent) ? res.recent : [],
      };
    }
  } catch {
    // 静默：记录面板失败不影响头部其余功能
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
    // 静默
  }
}

async function openAuditDir() {
  try {
    const api = window.pywebview?.api;
    if (!api?.open_decision_audit_dir) return;
    await api.open_decision_audit_dir();
  } catch {
    // 静默
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
  // 打开面板即视为已读：清零头部徽章
  store.decisionUnread = 0;
});

// 面板开启期间有新决策落盘（decision_recorded 事件）：立即刷新记录并保持已读
watch(
  () => store.decisionEventTick,
  () => {
    loadSummary();
    store.decisionUnread = 0;
  },
);

// 切换会话：记录按会话隔离，必须重新拉取本会话的决策记录
watch(
  () => store.currentSessionName,
  () => { loadSummary(); },
);
</script>

<style scoped>
.decision-pop {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: 460px;
  max-width: min(460px, calc(100vw - 32px));
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(14px);
  z-index: 210;
  display: flex;
  flex-direction: column;
  max-height: min(560px, 62vh);
  overflow: hidden;
  text-align: left;
}
.pop-head { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-bottom: 1px solid var(--border); }
.ph-ico { width: 26px; height: 26px; display: grid; place-items: center; border-radius: 8px; background: var(--bg3); color: var(--accent); flex: none; }
.ph-title { flex: 1; min-width: 0; }
.ph-title h3 { margin: 0; font-size: 14px; }
.ph-sub { margin: 2px 0 0; color: var(--text-faint); font-size: 11px; }
.pop-body { padding: 12px 14px; overflow: auto; display: grid; gap: 10px; }
.status-line { display: flex; align-items: center; gap: 8px; color: var(--text-dim); font-size: 12px; }
.status-line .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); flex: none; }
.status-line .dot.ok { background: var(--green); }
.stats-line { color: var(--text-dim); font-size: 12px; }
.record-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 5px; font-size: 12px; color: var(--text-dim); }
.record-list li { padding: 5px 8px; border-radius: 7px; background: var(--bg2); cursor: pointer; }
.record-list li.open { background: var(--bg3); }
.record-list li.shadow { opacity: .75; }
/* 每条记录两行：上面是元信息，下面是「判的是什么事 → 模型结论」 */
.rec-content { margin-top: 3px; display: flex; gap: 6px; align-items: baseline; min-width: 0; }
.rec-state { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-faint); }
.rec-answer { flex: none; color: var(--accent); }
/* 点开后的详情：请求体与响应体原文，等宽、限高、可滚动 */
.rec-detail { margin-top: 7px; display: grid; gap: 7px; cursor: default; }
.detail-title { color: var(--text-dim); font-size: 11px; margin-bottom: 3px; }
.detail-hint { color: var(--text-faint); font-size: 11px; }
.detail-json {
  margin: 0;
  padding: 7px 9px;
  border-radius: 6px;
  background: var(--float-bg);
  border: 1px solid var(--border);
  font-family: ui-monospace, Consolas, "Courier New", monospace;
  font-size: 11px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow: auto;
  color: var(--text-dim);
}
.pop-empty { color: var(--text-faint); font-size: 12px; line-height: 1.7; padding: 6px 0; }
.pop-foot { display: flex; gap: 8px; padding: 10px 14px; border-top: 1px solid var(--border); }
</style>
