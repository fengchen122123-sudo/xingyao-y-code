<template>
  <!-- 决策模型（JEV）设置分区：总开关默认关；关闭时其余控件全部置灰 -->
  <div class="decision-settings" data-test="decision-settings-page">
    <header class="decision-header">
      <div>
        <h1>决策模型（JEV）</h1>
        <p>接入基元律动 JEV 决策服务；默认关闭，启用后任何失败都会静默回落原有逻辑。</p>
      </div>
      <button class="refresh-button" type="button" title="重新加载" @click="reloadAll">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="decision-card">
      <label class="switch-row">
        <input data-test="decision-enabled" v-model="enabledDraft" type="checkbox" :disabled="loading || saving" />
        <span class="switch-label">启用决策模型</span>
        <span class="switch-hint">默认关闭；关闭时以下配置全部禁用，项目行为与未接入时一致</span>
      </label>

      <div class="status-row">
        <span class="status-dot" :class="statusDotClass"></span>
        <span class="status-text" data-test="decision-status">{{ statusText }}</span>
        <button data-test="decision-probe" class="secondary-button" type="button" :disabled="controlsDisabled || probing" @click="runProbe">
          {{ probing ? '探测中…' : '运行探针' }}
        </button>
      </div>
      <ul v-if="probeResults.length" class="probe-results" data-test="decision-probe-results">
        <li v-for="item in probeResults" :key="item.key_index" :class="item.status === 'available' ? 'ok' : 'bad'">
          Key#{{ item.key_index + 1 }}：{{ probeLabel(item) }}
        </li>
      </ul>
      <p v-if="probeModelId && probeResults.length" class="decision-note" data-test="decision-probe-model">探针目标模型：{{ probeModelId }}</p>
      <p v-if="probeMsg" class="decision-note warn" data-test="decision-probe-msg">{{ probeMsg }}</p>

      <label class="field-label" for="decision-model-id">模型 ID</label>
      <div class="model-row">
        <input id="decision-model-id" data-test="decision-model-id" v-model="modelIdDraft" class="text-field mono" type="text" :disabled="controlsDisabled" placeholder="NeoHorse-Jev-4B" />
        <button data-test="decision-fetch-models" class="secondary-button" type="button" :disabled="controlsDisabled || fetchingModels" @click="fetchModels">
          {{ fetchingModels ? '拉取中…' : '从上游拉取' }}
        </button>
      </div>
      <p v-if="fetchHint" class="decision-note" :class="{ warn: fetchFailed }" data-test="decision-fetch-hint">{{ fetchHint }}</p>
      <select v-if="modelOptions.length" data-test="decision-model-select" class="text-field model-select" :value="modelIdDraft" @change="pickModel">
        <option value="">共 {{ modelOptions.length }} 个决策模型，点选自动填充</option>
        <option v-for="model in modelOptions" :key="model.id" :value="model.id">{{ modelOptionText(model) }}</option>
      </select>

      <div class="field-label">接口形态</div>
      <div class="radio-row">
        <label class="radio-item">
          <input data-test="decision-endpoint-decision" v-model="endpointStyleDraft" type="radio" value="decision" :disabled="controlsDisabled" />
          <span>/v1/decision（推荐）</span>
        </label>
        <label class="radio-item">
          <input data-test="decision-endpoint-systemone" v-model="endpointStyleDraft" type="radio" value="systemone" :disabled="controlsDisabled" />
          <span>/v1/systemone（兼容格式）</span>
        </label>
      </div>

      <div class="field-label">调用通道</div>
      <select
        data-test="decision-channel-select"
        v-model="providerRef"
        class="text-field channel-select"
        :disabled="controlsDisabled || channelsLoading"
      >
        <option value="auto">自动匹配（基元律动）</option>
        <option v-for="item in providerOptions" :key="item.ref" :value="item.ref">
          {{ item.name }}（{{ item.key_count }} 把 Key）
        </option>
        <option v-if="unlistedRef" :value="unlistedRef">{{ unlistedRef }}（当前配置，未在通道列表）</option>
      </select>
      <div class="field-label">Base URL 覆盖（可选）</div>
      <input
        data-test="decision-base-url"
        v-model="baseUrlDraft"
        class="text-field"
        type="text"
        placeholder="留空 = 使用通道默认地址"
        :disabled="controlsDisabled"
      />
      <p class="decision-note">通道决定 Key 从哪来（选谁就用谁配置里的多 Key 池）；Base URL 填写后覆盖通道地址，用于非标端点。自动匹配 = base_url 含 tokenrhythm 的第一个通道。</p>

      <div class="field-label">Key 策略</div>
      <div class="radio-row">
        <label class="radio-item">
          <input data-test="decision-key-auto" v-model="keyModeDraft" type="radio" value="auto" :disabled="controlsDisabled" />
          <span>自动（跨权限轮换）</span>
        </label>
        <label class="radio-item">
          <input data-test="decision-key-index" v-model="keyModeDraft" type="radio" value="index" :disabled="controlsDisabled" />
          <span>指定第</span>
          <input data-test="decision-key-index-num" v-model.number="keyIndexDraft" class="key-index-input" type="number" min="1" max="99" :disabled="controlsDisabled || keyModeDraft !== 'index'" />
          <span>把</span>
        </label>
      </div>

      <label class="switch-row">
        <input data-test="decision-shadow" v-model="shadowModeDraft" type="checkbox" :disabled="controlsDisabled" />
        <span class="switch-label">影子模式（只记录不生效）</span>
        <span class="switch-hint">发请求并写审计，但决策结果不参与业务判断</span>
      </label>

      <label class="field-label" for="decision-timeout">超时（毫秒）</label>
      <div class="timeout-row">
        <input id="decision-timeout" data-test="decision-timeout" v-model.number="timeoutDraft" class="text-field timeout-input" type="number" :min="100" :max="5000" step="50" :disabled="controlsDisabled" />
        <span class="range-hint">100 ~ 5000 ms；超时宁可不判也不拖慢主链路</span>
      </div>

      <div class="field-label">决策记录</div>
      <div class="audit-row">
        <span class="audit-summary-text" data-test="decision-audit-summary">{{ auditSummaryText }}</span>
        <button data-test="decision-audit-open" class="secondary-button" type="button" :disabled="loading || saving" @click="openAuditDir">打开审计目录</button>
      </div>
      <ul v-if="auditRecent.length" class="audit-list" data-test="decision-audit-list">
        <li v-for="(item, index) in auditRecent" :key="index" :class="{ shadow: item.shadow }">
          {{ auditLine(item) }}
        </li>
      </ul>
      <p class="decision-note">审计按天写入 ~/.ycode/decision_audit/YYYYMMDD.jsonl（不含 Key）；跑一次探针即可产生记录，业务场景接入后每次决策都会记录在这里。</p>

      <div class="decision-actions">
        <button data-test="decision-save" class="primary-button" type="button" :disabled="loading || saving" @click="saveSettings">
          {{ saving ? '保存中…' : '保存' }}
        </button>
        <button data-test="decision-reload" class="secondary-button" type="button" :disabled="loading || saving" @click="loadSettings">放弃修改</button>
      </div>

      <p v-if="errorMsg" data-test="decision-error" class="decision-error">{{ errorMsg }}</p>
      <p v-else-if="savedTip" data-test="decision-saved" class="decision-saved">{{ savedTip }}</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

import AppIcon from './AppIcon.vue';
import type {
  DecisionAuditRecentItem,
  DecisionAuditSummary,
  DecisionModelOption,
  DecisionProbeKeyResult,
  DecisionProviderOption,
  DecisionRuntimeState,
  DecisionSettingsSnapshot,
} from '../types/pywebview';

// 探针状态 → 中文标签（与 core.decision_service.probe_decision 的分类一一对应）
const PROBE_LABELS: Record<string, string> = {
  available: '可用',
  no_permission: '权限不足',
  invalid_key: '密钥无效',
  timeout: '超时',
  network_error: '网络失败',
  rate_limited: '限流',
  error: '失败',
};

const loading = ref(false);
const saving = ref(false);
const probing = ref(false);
const fetchingModels = ref(false);
const errorMsg = ref('');
const savedTip = ref('');
const probeMsg = ref('');

const enabledDraft = ref(false);
const modelIdDraft = ref('');
const endpointStyleDraft = ref<'decision' | 'systemone'>('decision');
const keyModeDraft = ref<'auto' | 'index'>('auto');
const keyIndexDraft = ref(1);
const shadowModeDraft = ref(true);
const timeoutDraft = ref(800);
const providerRef = ref('auto');
const baseUrlDraft = ref('');
const providerOptions = ref<DecisionProviderOption[]>([]);
const channelsLoading = ref(false);

const probeResults = ref<DecisionProbeKeyResult[]>([]);
const probeModelId = ref('');
const modelOptions = ref<DecisionModelOption[]>([]);
const fetchHint = ref('');
const fetchFailed = ref(false);
const auditSummary = ref<DecisionAuditSummary | null>(null);

let savedTipTimer: ReturnType<typeof setTimeout> | null = null;

// 总开关关闭时：除总开关与保存按钮外的控件全部置灰（设计第 6 节）
const controlsDisabled = computed(() => loading.value || saving.value || !enabledDraft.value);

const availableEntry = computed(
  () => probeResults.value.find((item) => item.status === 'available') || null,
);

const statusText = computed(() => {
  if (probing.value) return '探测中…';
  if (availableEntry.value) {
    return `可用（Key#${availableEntry.value.key_index + 1}）· 延迟 ${availableEntry.value.latency_ms}ms`;
  }
  if (probeResults.value.length) return '不可用（逐 Key 结果见下方）';
  if (probeMsg.value) return probeMsg.value;
  return enabledDraft.value ? '尚未探测' : '未启用';
});

const statusDotClass = computed(() => {
  if (availableEntry.value) return 'ok';
  if (probeResults.value.length) return 'bad';
  return '';
});

const auditRecent = computed(() => auditSummary.value?.recent ?? []);

const auditSummaryText = computed(() => {
  const summary = auditSummary.value;
  if (!summary || summary.today_count <= 0) {
    return '今日暂无决策记录（跑一次探针即可产生记录）';
  }
  return `今日 ${summary.today_count} 次决策 · 影子 ${summary.today_shadow_count} · 采纳 ${summary.today_adopted_count} · 回退 ${summary.today_fallback_count}`;
});

/** 单条审计记录的展示行：时间 · 场景 · 模式 · 延迟 · 回退原因 */
function auditLine(item: DecisionAuditRecentItem): string {
  const time = item.ts.length >= 19 ? item.ts.slice(11, 19) : item.ts;
  const parts = [time, item.scene || '未知场景', item.shadow ? '影子' : '正式'];
  if (typeof item.latency_ms === 'number') parts.push(`${item.latency_ms}ms`);
  if (item.fallback_reason) parts.push(`回退：${item.fallback_reason}`);
  return parts.join(' · ');
}

/** 当前配置的通道不在已拉取列表里时（历史/插件配置），补一个兜底选项避免下拉显示为空 */
const unlistedRef = computed(() => {
  const refText = providerRef.value.trim();
  if (!refText || refText === 'auto') return '';
  return providerOptions.value.some((item) => item.ref === refText) ? '' : refText;
});

function probeLabel(item: DecisionProbeKeyResult): string {
  const base = PROBE_LABELS[item.status] || '失败';
  return item.status === 'available' ? `${base}（${item.latency_ms} ms）` : base;
}

function modelOptionText(model: DecisionModelOption): string {
  const parts = [model.name || model.id];
  if (model.context) parts.push(`上下文 ${model.context}`);
  if (model.price_text) parts.push(model.price_text);
  return parts.join(' · ');
}

/** 探针条目归一：容忍后端字段缺失/扩展，避免渲染崩溃 */
function normalizeProbe(raw: Partial<DecisionProbeKeyResult>): DecisionProbeKeyResult {
  return {
    key_index: Number(raw.key_index ?? 0),
    status: raw.status ?? 'error',
    latency_ms: Number(raw.latency_ms ?? 0),
    error: String(raw.error ?? ''),
  };
}

function applySettings(settings: Partial<DecisionSettingsSnapshot>) {
  enabledDraft.value = settings.enabled === true;
  modelIdDraft.value = String(settings.model_id ?? '');
  endpointStyleDraft.value = settings.endpoint_style === 'systemone' ? 'systemone' : 'decision';
  const mode = String(settings.key_mode ?? 'auto');
  if (mode.startsWith('index:')) {
    keyModeDraft.value = 'index';
    // key_mode 的 N 是 0 基，界面按「第 N 把」1 基展示
    keyIndexDraft.value = (parseInt(mode.slice('index:'.length), 10) || 0) + 1;
  } else {
    keyModeDraft.value = 'auto';
    keyIndexDraft.value = 1;
  }
  shadowModeDraft.value = settings.shadow_mode !== false;
  timeoutDraft.value = Number(settings.timeout_ms ?? 800) || 800;
  providerRef.value = String(settings.provider_ref ?? 'auto');
  baseUrlDraft.value = String(settings.base_url ?? '');
}

function applyRuntime(lastProbe: DecisionRuntimeState['last_probe'] | undefined) {
  probeResults.value = Array.isArray(lastProbe?.results)
    ? lastProbe.results.map(normalizeProbe)
    : [];
  probeModelId.value = String(lastProbe?.model_id ?? '');
  probeMsg.value = String(lastProbe?.msg ?? '');
}

async function loadSettings() {
  loading.value = true;
  errorMsg.value = '';
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_settings) {
      // 后端未就绪（如纯前端预览）：保留默认草稿，不报错
      return;
    }
    const res = await api.get_decision_settings();
    if (res?.ok) {
      applySettings(res.settings || {});
      applyRuntime(res.runtime?.last_probe);
    } else {
      errorMsg.value = res?.msg || '加载决策设置失败';
    }
  } catch {
    errorMsg.value = '加载决策设置失败';
  } finally {
    loading.value = false;
  }
}

async function saveSettings() {
  if (saving.value) return;
  const timeout = Math.round(Number(timeoutDraft.value));
  if (!Number.isFinite(timeout) || timeout < 100 || timeout > 5000) {
    errorMsg.value = '超时必须是 100~5000 之间的毫秒数';
    return;
  }
  if (enabledDraft.value && !modelIdDraft.value.trim()) {
    errorMsg.value = '启用决策模型时模型 ID 不能为空';
    return;
  }
  saving.value = true;
  errorMsg.value = '';
  try {
    const api = window.pywebview?.api;
    if (!api?.set_decision_settings) {
      errorMsg.value = '后端接口未就绪，无法保存';
      return;
    }
    const index = Math.max(0, Math.round(Number(keyIndexDraft.value) || 1) - 1);
    const res = await api.set_decision_settings({
      enabled: enabledDraft.value,
      model_id: modelIdDraft.value.trim(),
      endpoint_style: endpointStyleDraft.value,
      provider_ref: providerRef.value.trim() || 'auto',
      base_url: baseUrlDraft.value.trim(),
      key_mode: keyModeDraft.value === 'auto' ? 'auto' : `index:${index}`,
      shadow_mode: shadowModeDraft.value,
      timeout_ms: timeout,
    });
    if (res?.ok) {
      if (res.settings) applySettings(res.settings);
      savedTip.value = res.msg || '已保存';
      if (savedTipTimer) clearTimeout(savedTipTimer);
      savedTipTimer = setTimeout(() => { savedTip.value = ''; }, 2500);
    } else {
      // 保存失败：保留用户草稿不回滚，只回显后端原因
      errorMsg.value = res?.msg || '保存失败';
    }
  } catch {
    errorMsg.value = '保存失败';
  } finally {
    saving.value = false;
  }
}

async function runProbe() {
  if (probing.value) return;
  probing.value = true;
  probeMsg.value = '';
  try {
    const api = window.pywebview?.api;
    if (!api?.probe_decision) {
      probeMsg.value = '后端接口未就绪，无法探测';
      return;
    }
    const res = await api.probe_decision();
    probeResults.value = Array.isArray(res?.results) ? res.results.map(normalizeProbe) : [];
    probeModelId.value = String(res?.model_id ?? '');
    if (res?.msg) probeMsg.value = String(res.msg);
    loadAuditSummary(); // 探针会写一条审计记录：刷一次面板立即可见
  } catch {
    probeMsg.value = '探针执行失败';
  } finally {
    probing.value = false;
  }
}

async function fetchModels() {
  if (fetchingModels.value) return;
  fetchingModels.value = true;
  fetchHint.value = '';
  fetchFailed.value = false;
  try {
    const api = window.pywebview?.api;
    if (!api?.list_decision_models) {
      fetchHint.value = '后端接口未就绪，无法拉取';
      fetchFailed.value = true;
      return;
    }
    const res = await api.list_decision_models();
    const models = Array.isArray(res?.models) ? res.models : [];
    modelOptions.value = models;
    if (res?.ok && models.length) {
      fetchHint.value = `已拉取 ${models.length} 个决策模型，可点选下方列表`;
    } else if (res?.ok) {
      fetchHint.value = '上游没有声明 type=decision 的模型；仍可手动输入模型 ID';
      fetchFailed.value = true;
    } else {
      fetchHint.value = String(res?.msg || '拉取失败；仍可手动输入模型 ID');
      fetchFailed.value = true;
    }
  } catch {
    fetchHint.value = '拉取失败；仍可手动输入模型 ID';
    fetchFailed.value = true;
  } finally {
    fetchingModels.value = false;
  }
}

function pickModel(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (value) modelIdDraft.value = value;
}

/** 审计汇总（决策记录面板）：加载失败静默，不阻塞其余控件 */
async function loadAuditSummary() {
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_audit_summary) return;
    const res = await api.get_decision_audit_summary(10);
    if (res?.ok) {
      auditSummary.value = {
        ok: true,
        today_count: Number(res.today_count ?? 0),
        today_shadow_count: Number(res.today_shadow_count ?? 0),
        today_adopted_count: Number(res.today_adopted_count ?? 0),
        today_fallback_count: Number(res.today_fallback_count ?? 0),
        recent: Array.isArray(res.recent) ? res.recent : [],
      };
    }
  } catch {
    // 记录面板失败不弹错
  }
}

async function openAuditDir() {
  try {
    const api = window.pywebview?.api;
    if (!api?.open_decision_audit_dir) return;
    const res = await api.open_decision_audit_dir();
    if (!res?.ok) errorMsg.value = res?.msg || '打开审计目录失败';
  } catch {
    errorMsg.value = '打开审计目录失败';
  }
}

function reloadAll() {
  loadSettings();
  loadAuditSummary();
}

/** 拉取可选调用通道列表（不含 Key 内容）；失败静默，不阻塞页面其余控件 */
async function loadProviders() {
  if (channelsLoading.value) return;
  channelsLoading.value = true;
  try {
    const api = window.pywebview?.api;
    if (!api?.list_decision_providers) return;
    const res = await api.list_decision_providers();
    if (res?.ok && Array.isArray(res.providers)) {
      providerOptions.value = res.providers
        .map((item) => ({
          ref: String(item.ref ?? ''),
          name: String(item.name ?? item.ref ?? ''),
          base_url: String(item.base_url ?? ''),
          key_count: Number(item.key_count ?? 0),
          auto_match: item.auto_match === true,
        }))
        .filter((item) => item.ref);
    }
  } catch {
    // 通道列表失败不弹错：用户仍可用自动匹配或手动输入 Base URL
  } finally {
    channelsLoading.value = false;
  }
}

onMounted(() => {
  loadSettings();
  loadProviders();
  loadAuditSummary();
});
</script>

<style scoped>
.decision-settings { max-width: 920px; margin: 0 auto; }
.decision-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.decision-header h1 { margin: 0 0 7px; font-size: 25px; }
.decision-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.refresh-button { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-dim); flex: none; }
.refresh-button:hover { color: var(--text); background: var(--bg3); }
.decision-card { border-top: 1px solid var(--border); padding-top: 4px; }
.field-label { display: block; margin: 18px 0 7px; color: var(--text-dim); font-size: 12px; }
.text-field { width: 100%; box-sizing: border-box; min-height: 42px; padding: 0 13px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.text-field:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.text-field:disabled { cursor: not-allowed; opacity: .55; }
.switch-row { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; margin-top: 16px; }
.switch-label { font-weight: 600; font-size: 13px; }
.switch-hint { color: var(--text-faint); font-size: 12px; }
.status-row { display: flex; align-items: center; gap: 10px; margin-top: 16px; flex-wrap: wrap; }
.status-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--text-faint); flex: none; }
.status-dot.ok { background: var(--green); }
.status-dot.bad { background: var(--danger); }
.status-text { color: var(--text-dim); font-size: 13px; flex: 1; min-width: 180px; }
.probe-results { list-style: none; margin: 8px 0 0; padding: 0 0 0 19px; display: grid; gap: 4px; font-size: 12px; }
.probe-results li.ok { color: var(--green); }
.probe-results li.bad { color: var(--danger); }
.model-row { display: flex; gap: 10px; }
.model-row .text-field { flex: 1; }
.model-select { margin-top: 8px; }
.radio-row { display: flex; gap: 22px; flex-wrap: wrap; }
.radio-item { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; color: var(--text); }
.key-index-input { width: 64px; min-height: 34px; padding: 0 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text); font-size: 13px; }
.channel-select { padding-right: 13px; }
.timeout-row { display: flex; align-items: center; gap: 12px; }
.timeout-input { width: 120px; }
.range-hint { color: var(--text-faint); font-size: 12px; }
.audit-row { display: flex; align-items: center; gap: 12px; margin-top: 18px; flex-wrap: wrap; }
.audit-hint { color: var(--text-faint); font-size: 12px; }
.audit-summary-text { color: var(--text-dim); font-size: 12px; }
.audit-list { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 4px; font-size: 12px; color: var(--text-dim); }
.audit-list li.shadow { opacity: .75; }
.decision-actions { display: flex; gap: 10px; margin-top: 16px; }
.primary-button, .secondary-button { min-height: 38px; padding: 0 18px; border-radius: 9px; border: 1px solid var(--border); font-weight: 600; cursor: pointer; font-size: 13px; }
.primary-button { background: var(--text); color: var(--bg0); }
.primary-button:disabled { opacity: .5; cursor: not-allowed; }
.secondary-button { background: var(--bg3); color: var(--text); }
.secondary-button:disabled { opacity: .5; cursor: not-allowed; }
.decision-note { margin: 8px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }
.decision-note.warn { color: var(--warning); }
.decision-error { margin: 12px 0 0; color: var(--danger); font-size: 12px; }
.decision-saved { margin: 12px 0 0; color: var(--green); font-size: 12px; }

@media (max-width: 768px) {
  .decision-header { flex-direction: column; }
  .model-row { flex-direction: column; }
  .decision-actions { flex-wrap: wrap; }
}
</style>
