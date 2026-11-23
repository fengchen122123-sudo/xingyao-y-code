<template>
  <div class="decision-settings" data-test="decision-settings-page">
    <header class="decision-header">
      <div>
        <h1>决策模型（JEV）</h1>
        <p>配置 JEV 决策服务、调用策略与业务接入场景；失败或低置信时自动回退原有逻辑。</p>
      </div>
      <div class="header-actions">
        <span v-if="isConfigDirty" class="dirty-pill" data-test="decision-dirty-pill">有未保存修改</span>
        <button
          class="refresh-button"
          type="button"
          title="重新加载"
          data-test="decision-refresh"
          :disabled="loading"
          @click="reloadAll"
        >
          <AppIcon name="refresh" :size="14" :class="{ 'icon-spin': loading }" />
        </button>
      </div>
    </header>

    <!-- 总开关与服务连通状态卡片 -->
    <section class="setting-card hero-card">
      <div class="hero-master-row">
        <div class="hero-copy">
          <div class="hero-icon" :class="{ on: enabledDraft }">
            <AppIcon name="brain" :size="18" :stroke-width="1.9" />
          </div>
          <div class="hero-text">
            <div class="hero-title-line">
              <h2>启用决策模型</h2>
              <span class="state-pill" :class="{ on: enabledDraft }" data-test="decision-enabled-pill">
                {{ enabledDraft ? '已启用' : '已关闭' }}
              </span>
            </div>
            <p>关闭时禁用所有决策请求；开启后，「自动决策」模式与下方已开启的场景将接入 JEV 判定。</p>
          </div>
        </div>
        <button
          type="button"
          role="switch"
          class="setting-switch"
          data-test="decision-enabled"
          aria-label="启用决策模型"
          :aria-checked="enabledDraft ? 'true' : 'false'"
          :disabled="loading || saving || masterToggling"
          @click="toggleMasterEnabled"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <div class="hero-probe-strip">
        <div class="probe-status-left">
          <span class="status-dot" :class="statusDotClass" />
          <span class="status-text" data-test="decision-status">{{ statusText }}</span>
          <span v-if="runtimeState.key_count > 0" class="runtime-pill" data-test="decision-runtime-keys">
            Key 池 {{ runtimeState.key_count }} 把
            <template v-if="runtimeState.active_key_index !== null">
              · 当前 Key#{{ runtimeState.active_key_index + 1 }}
            </template>
          </span>
        </div>
        <button
          data-test="decision-probe"
          class="secondary-button compact"
          type="button"
          :disabled="controlsDisabled || probing"
          @click="runProbe"
        >
          <AppIcon name="zap" :size="13" />
          <span>{{ probing ? '探测中…' : '运行探针' }}</span>
        </button>
      </div>

      <div v-if="probeResults.length || probeMsg" class="probe-detail-box">
        <ul v-if="probeResults.length" class="probe-chips" data-test="decision-probe-results">
          <li
            v-for="item in probeResults"
            :key="item.key_index"
            class="probe-chip"
            :class="item.status === 'available' ? 'ok' : 'bad'"
            :title="item.error || undefined"
          >
            <span class="probe-chip-dot" />
            <span>Key#{{ item.key_index + 1 }}：{{ probeLabel(item) }}</span>
          </li>
        </ul>
        <p v-if="probeModelId && probeResults.length" class="decision-note" data-test="decision-probe-model">
          探针目标模型：{{ probeModelId }}
        </p>
        <p v-if="probeMsg" class="decision-note warn" data-test="decision-probe-msg">{{ probeMsg }}</p>
      </div>
    </section>

    <!-- 模型与连接配置 -->
    <section class="setting-group">
      <div class="group-title">
        <AppIcon name="settings" :size="14" />
        <span>模型与连接配置</span>
      </div>
      <div class="setting-card">
        <div class="setting-row setting-row--top">
          <div class="setting-copy">
            <h3>模型 ID</h3>
            <p>指定 JEV 决策模型标识，或从当前通道上游实时拉取。</p>
          </div>
          <div class="setting-control setting-control--wide">
            <div class="model-row">
              <input
                id="decision-model-id"
                data-test="decision-model-id"
                v-model="modelIdDraft"
                class="text-field mono"
                type="text"
                :disabled="controlsDisabled"
                placeholder="NeoHorse-Jev-4B"
              />
              <button
                data-test="decision-fetch-models"
                class="secondary-button compact"
                type="button"
                :disabled="controlsDisabled || fetchingModels"
                @click="fetchModels"
              >
                {{ fetchingModels ? '拉取中…' : '从上游拉取' }}
              </button>
            </div>
            <select
              v-if="modelOptions.length"
              data-test="decision-model-select"
              class="text-field model-select"
              :disabled="controlsDisabled"
              :value="modelIdDraft"
              @change="pickModel"
            >
              <option value="">共 {{ modelOptions.length }} 个决策模型，点选自动填充</option>
              <option v-for="model in modelOptions" :key="model.id" :value="model.id">{{ modelOptionText(model) }}</option>
            </select>
            <p v-if="fetchHint" class="field-hint" :class="{ warn: fetchFailed }" data-test="decision-fetch-hint">
              {{ fetchHint }}
            </p>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>接口形态</h3>
            <p>决定发往上游网关的协议路径。</p>
          </div>
          <div class="setting-control">
            <div class="seg-group" role="radiogroup" aria-label="接口形态">
              <button
                type="button"
                role="radio"
                class="seg-option"
                :class="{ active: endpointStyleDraft === 'decision' }"
                :aria-checked="endpointStyleDraft === 'decision' ? 'true' : 'false'"
                data-test="decision-endpoint-decision"
                :disabled="controlsDisabled"
                @click="endpointStyleDraft = 'decision'"
              >
                <span class="mono">/v1/decision</span>
                <span class="seg-tag">推荐</span>
              </button>
              <button
                type="button"
                role="radio"
                class="seg-option"
                :class="{ active: endpointStyleDraft === 'systemone' }"
                :aria-checked="endpointStyleDraft === 'systemone' ? 'true' : 'false'"
                data-test="decision-endpoint-systemone"
                :disabled="controlsDisabled"
                @click="endpointStyleDraft = 'systemone'"
              >
                <span class="mono">/v1/systemone</span>
                <span class="seg-tag">兼容</span>
              </button>
            </div>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>调用通道</h3>
            <p>选择调用的厂商通道与对应的 Key 池。</p>
          </div>
          <div class="setting-control">
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
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>Base URL 覆盖（可选）</h3>
            <p>填写可覆盖所选通道默认地址，留空使用通道配置。</p>
          </div>
          <div class="setting-control setting-control--wide">
            <input
              data-test="decision-base-url"
              v-model="baseUrlDraft"
              class="text-field mono"
              type="text"
              placeholder="留空 = 使用通道默认地址"
              :disabled="controlsDisabled"
            />
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>Key 策略</h3>
            <p>多 Key 池跨权限轮换容错，或固定指定某一把 Key。</p>
          </div>
          <div class="setting-control key-policy-control">
            <div class="seg-group" role="radiogroup" aria-label="Key 策略">
              <button
                type="button"
                role="radio"
                class="seg-option"
                :class="{ active: keyModeDraft === 'auto' }"
                :aria-checked="keyModeDraft === 'auto' ? 'true' : 'false'"
                data-test="decision-key-auto"
                :disabled="controlsDisabled"
                @click="keyModeDraft = 'auto'"
              >
                自动（跨权限轮换）
              </button>
              <button
                type="button"
                role="radio"
                class="seg-option"
                :class="{ active: keyModeDraft === 'index' }"
                :aria-checked="keyModeDraft === 'index' ? 'true' : 'false'"
                data-test="decision-key-index"
                :disabled="controlsDisabled"
                @click="keyModeDraft = 'index'"
              >
                指定 Key
              </button>
            </div>
            <label class="key-index-inline" :class="{ muted: keyModeDraft !== 'index' }">
              <span>第</span>
              <input
                data-test="decision-key-index-num"
                v-model.number="keyIndexDraft"
                class="key-index-input mono"
                type="number"
                min="1"
                max="99"
                :disabled="controlsDisabled || keyModeDraft !== 'index'"
              />
              <span>把</span>
            </label>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>超时（毫秒）</h3>
            <p>允许 100 ~ 5000 ms，超时自动回退原有逻辑。</p>
          </div>
          <div class="setting-control timeout-control">
            <div class="preset-chips">
              <button
                v-for="ms in TIMEOUT_PRESETS"
                :key="ms"
                type="button"
                class="preset-chip mono"
                :class="{ active: Number(timeoutDraft) === ms }"
                :disabled="controlsDisabled"
                @click="timeoutDraft = ms"
              >
                {{ ms }}ms
              </button>
            </div>
            <div class="input-with-unit">
              <input
                id="decision-timeout"
                data-test="decision-timeout"
                v-model.number="timeoutDraft"
                class="text-field timeout-input mono"
                type="number"
                :min="100"
                :max="5000"
                step="50"
                :disabled="controlsDisabled"
              />
              <span class="unit-suffix">ms</span>
            </div>
          </div>
        </div>

        <div class="config-footer">
          <div class="config-feedback">
            <p v-if="errorMsg" data-test="decision-error" class="decision-error">{{ errorMsg }}</p>
            <p v-else-if="savedTip" data-test="decision-saved" class="decision-saved">{{ savedTip }}</p>
            <p v-else-if="isConfigDirty" class="decision-dirty-hint">有未保存的连接配置修改</p>
            <p v-else class="decision-muted-hint">修改模型或连接参数后请点击保存生效</p>
          </div>
          <div class="decision-actions">
            <button
              data-test="decision-reload"
              class="secondary-button"
              type="button"
              :disabled="loading || saving || !isConfigDirty"
              @click="loadSettings"
            >
              放弃修改
            </button>
            <button
              data-test="decision-save"
              class="primary-button"
              type="button"
              :disabled="loading || saving"
              @click="saveSettings"
            >
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- 业务场景开关 -->
    <section class="setting-group">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="layers" :size="14" />
          <span>业务场景</span>
        </div>
        <span class="group-meta">开关与阈值即时保存</span>
      </div>

      <div class="setting-card" data-test="decision-scenes">
        <div v-if="!enabledDraft" class="scenes-standby-banner" data-test="decision-scenes-standby">
          <AppIcon name="info" :size="13" />
          <span>需先开启上方「启用决策模型」总开关，才可调整各业务场景。</span>
        </div>

        <div
          v-for="row in sceneRows"
          :key="row.scene"
          class="scene-row"
          :class="{ 'is-live': row.mode === 'live', 'is-disabled': controlsDisabled }"
          :data-test="`decision-scene-${row.scene}`"
        >
          <div class="scene-copy">
            <div class="scene-head">
              <span class="scene-name">{{ row.label }}</span>
              <span class="scene-mode-badge" :class="row.mode">
                {{ row.mode === 'live' ? '已开启' : '已关闭' }}
              </span>
            </div>
            <p class="scene-desc">{{ row.description }}</p>
            <p
              v-if="row.mode === 'live'"
              class="scene-live-warning"
              :data-test="`decision-scene-${row.scene}-live-warning`"
            >
              <AppIcon name="triangle-alert" :size="12" />
              <span>开启后结果会真实影响任务行为（失败或超时自动回退）。</span>
            </p>
          </div>

          <div class="scene-controls">
            <label class="threshold-box">
              <span class="threshold-label">阈值</span>
              <input
                type="range"
                class="threshold-slider"
                min="0"
                max="1"
                step="0.05"
                :value="row.threshold"
                :aria-label="`${row.label}置信阈值滑块`"
                :disabled="controlsDisabled || sceneSaving === row.scene"
                @input="previewSceneThreshold(row, $event)"
                @change="changeSceneThreshold(row, $event)"
              />
              <input
                :data-test="`decision-scene-${row.scene}-threshold`"
                class="text-field scene-threshold-input mono"
                type="number"
                min="0"
                max="1"
                step="0.05"
                :value="row.threshold"
                :aria-label="`${row.label}置信阈值`"
                :disabled="controlsDisabled || sceneSaving === row.scene"
                @change="changeSceneThreshold(row, $event)"
              />
            </label>
            <button
              type="button"
              role="switch"
              class="setting-switch"
              :data-test="`decision-scene-${row.scene}-switch`"
              :aria-checked="row.mode === 'live' ? 'true' : 'false'"
              :aria-label="`启用${row.label}场景`"
              :disabled="controlsDisabled || sceneSaving === row.scene"
              @click="toggleSceneMode(row)"
            >
              <span class="switch-knob" />
            </button>
          </div>
        </div>

        <div v-if="!sceneRows.length" class="scenes-empty">
          场景列表未就绪（后端未响应或接口不可用）
        </div>

        <div class="scenes-footer">
          <p v-if="scenesMsg" class="scenes-msg" :class="{ warn: scenesFailed }" data-test="decision-scenes-msg">
            {{ scenesMsg }}
          </p>
          <div class="auto-scenes-strip">
            <span class="auto-scenes-label">总开关开启后自动接管：</span>
            <span
              v-for="item in AUTO_SCENE_BADGES"
              :key="item"
              class="auto-scene-pill"
              :class="{ active: enabledDraft }"
            >
              {{ item }}
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- 决策记录与审计 -->
    <section class="setting-group">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="history" :size="14" />
          <span>决策记录与审计</span>
        </div>
        <button
          data-test="decision-audit-open"
          class="secondary-button compact"
          type="button"
          :disabled="loading || saving"
          @click="openAuditDir"
        >
          <AppIcon name="folder-open" :size="13" />
          <span>打开审计目录</span>
        </button>
      </div>

      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-copy">
            <h3>记录决策审计日志</h3>
            <p>按天保存在 ~/.ycode/decision_audit/，用于回溯请求摘要、延迟与回退原因。</p>
            <p v-if="auditMsg" class="field-hint" :class="{ warn: auditFailed }" data-test="decision-audit-msg">
              {{ auditMsg }}
            </p>
          </div>
          <button
            type="button"
            role="switch"
            class="setting-switch"
            data-test="decision-audit-enabled"
            aria-label="记录决策审计日志"
            :aria-checked="auditEnabledDraft ? 'true' : 'false'"
            :disabled="loading || saving || auditToggling"
            @click="toggleAuditEnabled"
          >
            <span class="switch-knob" />
          </button>
        </div>

        <div class="audit-body">
          <div class="audit-toolbar">
            <div class="audit-scope-tabs" role="group" aria-label="决策记录范围">
              <button
                data-test="decision-audit-scope-all"
                type="button"
                class="scope-tab"
                :class="{ active: auditScope === 'all' }"
                @click="setAuditScope('all')"
              >
                全部会话
              </button>
              <button
                data-test="decision-audit-scope-session"
                type="button"
                class="scope-tab"
                :class="{ active: auditScope === 'session' }"
                :disabled="!activeSessionName"
                :title="activeSessionName ? `仅查看当前会话（${activeSessionName}）` : '当前暂无已保存的会话'"
                @click="setAuditScope('session')"
              >
                当前会话
              </button>
            </div>
            <span class="audit-summary-text" data-test="decision-audit-summary">{{ auditSummaryText }}</span>
          </div>

          <div v-if="auditHasCounts" class="audit-kpi-grid">
            <div class="audit-kpi-cell">
              <span class="k">{{ auditScope === 'session' ? '本会话今日' : '今日调用' }}</span>
              <span class="v mono">{{ auditSummary?.today_count ?? 0 }}</span>
            </div>
            <div class="audit-kpi-cell green">
              <span class="k">已采纳</span>
              <span class="v mono">{{ auditSummary?.today_adopted_count ?? 0 }}</span>
            </div>
            <div class="audit-kpi-cell" :class="{ warn: (auditSummary?.today_fallback_count ?? 0) > 0 }">
              <span class="k">自动回退</span>
              <span class="v mono">{{ auditSummary?.today_fallback_count ?? 0 }}</span>
            </div>
          </div>

          <ul v-if="auditRecent.length" class="audit-list" data-test="decision-audit-list">
            <li
              v-for="(item, index) in auditRecent"
              :key="item.trace_id || index"
              class="audit-item"
              :class="{ clickable: !!item.trace_id, open: expandedAuditIndex === index }"
              @click="item.trace_id && toggleAuditDetail(index, item)"
            >
              <div class="audit-item-main">
                <div class="audit-item-left">
                  <span class="audit-scene-tag">{{ sceneLabel(item.scene) }}</span>
                  <code v-if="sceneLabel(item.scene) !== item.scene" class="audit-scene-code">{{ item.scene }}</code>
                  <span v-if="item.fallback_reason" class="audit-badge warn">回退：{{ item.fallback_reason }}</span>
                  <span v-else-if="item.adopted" class="audit-badge ok">已采纳</span>
                  <span v-else class="audit-badge muted">未采纳</span>
                  <span v-if="item.answer" class="audit-answer mono">→ {{ item.answer }}</span>
                </div>
                <div class="audit-item-right">
                  <span v-if="auditScope === 'all' && item.session_id" class="audit-session">
                    会话：{{ item.session_id }}
                  </span>
                  <span v-if="typeof item.latency_ms === 'number'" class="audit-meta mono">{{ item.latency_ms }}ms</span>
                  <span class="audit-meta mono">{{ formatAuditTime(item.ts) }}</span>
                  <AppIcon
                    v-if="item.trace_id"
                    name="chevron-right"
                    :size="12"
                    class="audit-chevron"
                    :class="{ rotated: expandedAuditIndex === index }"
                  />
                </div>
              </div>
              <div v-if="item.state_digest" class="audit-digest">{{ item.state_digest }}</div>
              <div v-if="expandedAuditIndex === index" class="audit-detail" @click.stop>
                <p v-if="auditDetailLoading" class="decision-note">读取详情中…</p>
                <p v-else-if="auditDetailError" class="decision-note warn">{{ auditDetailError }}</p>
                <template v-else-if="auditDetail">
                  <div class="audit-detail-grid">
                    <div>
                      <div class="audit-detail-title">发给 JEV 的请求</div>
                      <pre class="audit-detail-json">{{ prettyDetailRequest(auditDetail) }}</pre>
                    </div>
                    <div>
                      <div class="audit-detail-title">JEV 的判定</div>
                      <pre class="audit-detail-json">{{ prettyDetailAnswers(auditDetail) }}</pre>
                    </div>
                  </div>
                </template>
              </div>
            </li>
          </ul>
          <p v-else class="audit-empty">暂无近期决策记录；启用后跑一次探针即可验证链路。</p>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { getActivePinia } from 'pinia';

import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import type {
  DecisionAuditRecentItem,
  DecisionAuditSummary,
  DecisionModelOption,
  DecisionProbeKeyResult,
  DecisionProviderOption,
  DecisionRecordDetail,
  DecisionRuntimeState,
  DecisionSceneMode,
  DecisionSceneRow,
  DecisionSettingsSnapshot,
} from '../types/pywebview';

const PROBE_LABELS: Record<string, string> = {
  available: '可用',
  no_permission: '权限不足',
  invalid_key: '密钥无效',
  timeout: '超时',
  network_error: '网络失败',
  rate_limited: '限流',
  error: '失败',
};

const SCENE_LABELS: Record<string, string> = {
  router: '路由选模',
  secretary: '秘书派发',
  search: '搜索条数',
  safety: '安全判定',
  auto_permission: '自动权限',
  auto_question: '自动提问',
  probe: '探针测试',
  loop_guard: '循环止损',
  subagent_review: '子代理验收',
  plan_recovery: '计划恢复',
  memory_filter: '记忆筛选',
};

const AUTO_SCENE_BADGES = [
  '循环止损',
  '子代理验收',
  '计划恢复',
  '记忆筛选',
  '自动权限 / 自动提问（切到自动决策模式时生效）',
];

const TIMEOUT_PRESETS = [500, 800, 1500, 3000];

const loading = ref(false);
const saving = ref(false);
const masterToggling = ref(false);
const auditToggling = ref(false);
const probing = ref(false);
const fetchingModels = ref(false);
const errorMsg = ref('');
const savedTip = ref('');
const probeMsg = ref('');
const auditMsg = ref('');
const auditFailed = ref(false);

const enabledDraft = ref(false);
const modelIdDraft = ref('');
const endpointStyleDraft = ref<'decision' | 'systemone'>('decision');
const keyModeDraft = ref<'auto' | 'index'>('auto');
const keyIndexDraft = ref(1);
const timeoutDraft = ref(1500);
const providerRef = ref('auto');
const baseUrlDraft = ref('');
const auditEnabledDraft = ref(true);

const savedSnapshot = ref({
  enabled: false,
  model_id: '',
  endpoint_style: 'decision' as 'decision' | 'systemone',
  provider_ref: 'auto',
  base_url: '',
  key_mode: 'auto' as 'auto' | 'index',
  key_index: 1,
  timeout_ms: 1500,
});

const providerOptions = ref<DecisionProviderOption[]>([]);
const channelsLoading = ref(false);

const runtimeState = ref<Pick<DecisionRuntimeState, 'key_count' | 'active_key_index'>>({
  key_count: 0,
  active_key_index: null,
});
const probeResults = ref<DecisionProbeKeyResult[]>([]);
const probeModelId = ref('');
const modelOptions = ref<DecisionModelOption[]>([]);
const fetchHint = ref('');
const fetchFailed = ref(false);
const auditSummary = ref<DecisionAuditSummary | null>(null);
const auditScope = ref<'all' | 'session'>('all');

const expandedAuditIndex = ref<number | null>(null);
const auditDetail = ref<DecisionRecordDetail['record'] | null>(null);
const auditDetailLoading = ref(false);
const auditDetailError = ref('');
let auditDetailSeq = 0;

const sceneRows = ref<DecisionSceneRow[]>([]);
// 正在保存的场景名（只禁用该行，避免整页置灰打断连续调整）
const sceneSaving = ref('');
const scenesMsg = ref('');
const scenesFailed = ref(false);

let savedTipTimer: ReturnType<typeof setTimeout> | null = null;
let auditMsgTimer: ReturnType<typeof setTimeout> | null = null;

const controlsDisabled = computed(() => loading.value || saving.value || !enabledDraft.value);

const isConfigDirty = computed(() => {
  const s = savedSnapshot.value;
  return (
    enabledDraft.value !== s.enabled ||
    modelIdDraft.value.trim() !== s.model_id ||
    endpointStyleDraft.value !== s.endpoint_style ||
    providerRef.value.trim() !== s.provider_ref ||
    baseUrlDraft.value.trim() !== s.base_url ||
    keyModeDraft.value !== s.key_mode ||
    (keyModeDraft.value === 'index' && Math.max(1, Math.round(Number(keyIndexDraft.value) || 1)) !== s.key_index) ||
    Math.round(Number(timeoutDraft.value)) !== s.timeout_ms
  );
});

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

const activeSessionName = computed(() => {
  const pinia = getActivePinia();
  if (!pinia) return '';
  return String(useAppStore(pinia).currentSessionName || '').trim();
});

const auditRecent = computed(() => auditSummary.value?.recent ?? []);

const auditHasCounts = computed(() => {
  const s = auditSummary.value;
  if (!s) return false;
  return s.today_count > 0 || (s.session_total_count ?? 0) > 0;
});

const auditSummaryText = computed(() => {
  const summary = auditSummary.value;
  const prefix = auditScope.value === 'session' ? '本会话今日' : '今日';
  if (!summary || (summary.today_count <= 0 && !(summary.session_total_count ?? 0))) {
    return auditScope.value === 'session'
      ? '本会话暂无决策记录'
      : '今日暂无决策记录（跑一次探针即可产生记录）';
  }
  const base = `${prefix} ${summary.today_count} 次决策 · 采纳 ${summary.today_adopted_count} · 回退 ${summary.today_fallback_count}`;
  if (auditScope.value === 'session' && typeof summary.session_total_count === 'number') {
    return `本会话累计 ${summary.session_total_count} 次 · ${base}`;
  }
  return base;
});

function sceneLabel(scene: string): string {
  return SCENE_LABELS[scene] || scene || '未知场景';
}

function formatAuditTime(ts: string): string {
  return ts.length >= 19 ? ts.slice(11, 19) : ts;
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

function syncStoreDecisionEnabled(enabled: boolean) {
  const pinia = getActivePinia();
  if (!pinia) return;
  const store = useAppStore(pinia);
  store.decisionEnabled = enabled;
  if (!enabled && store.permissionMode === 'auto_decision') {
    store.permissionMode = 'confirm';
  }
}

function showSavedTip(message: string) {
  savedTip.value = message;
  if (savedTipTimer) clearTimeout(savedTipTimer);
  savedTipTimer = setTimeout(() => {
    savedTip.value = '';
  }, 2500);
}

function showAuditMsg(message: string, failed = false) {
  auditMsg.value = message;
  auditFailed.value = failed;
  if (auditMsgTimer) clearTimeout(auditMsgTimer);
  if (!failed) {
    auditMsgTimer = setTimeout(() => {
      auditMsg.value = '';
    }, 2500);
  }
}

function applySettings(settings: Partial<DecisionSettingsSnapshot>) {
  const enabled = settings.enabled === true;
  const modelId = String(settings.model_id ?? '');
  const endpointStyle = settings.endpoint_style === 'systemone' ? 'systemone' : 'decision';
  const mode = String(settings.key_mode ?? 'auto');
  const keyMode: 'auto' | 'index' = mode.startsWith('index:') ? 'index' : 'auto';
  const keyIndex =
    keyMode === 'index' ? (parseInt(mode.slice('index:'.length), 10) || 0) + 1 : 1;
  const timeoutMs = Number(settings.timeout_ms ?? 1500) || 1500;
  const provider = String(settings.provider_ref ?? 'auto');
  const baseUrl = String(settings.base_url ?? '');

  enabledDraft.value = enabled;
  syncStoreDecisionEnabled(enabled);
  modelIdDraft.value = modelId;
  endpointStyleDraft.value = endpointStyle;
  keyModeDraft.value = keyMode;
  keyIndexDraft.value = keyIndex;
  timeoutDraft.value = timeoutMs;
  providerRef.value = provider;
  baseUrlDraft.value = baseUrl;
  auditEnabledDraft.value = settings.audit_enabled !== false;

  savedSnapshot.value = {
    enabled,
    model_id: modelId.trim(),
    endpoint_style: endpointStyle,
    provider_ref: provider.trim() || 'auto',
    base_url: baseUrl.trim(),
    key_mode: keyMode,
    key_index: keyIndex,
    timeout_ms: timeoutMs,
  };
}

function applyRuntime(runtime: Partial<DecisionRuntimeState> | undefined) {
  runtimeState.value = {
    key_count: Number(runtime?.key_count ?? 0) || 0,
    active_key_index:
      typeof runtime?.active_key_index === 'number' ? runtime.active_key_index : null,
  };
  const lastProbe = runtime?.last_probe;
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
      return;
    }
    const res = await api.get_decision_settings();
    if (res?.ok) {
      applySettings(res.settings || {});
      applyRuntime(res.runtime);
    } else {
      errorMsg.value = res?.msg || '加载决策设置失败';
    }
  } catch {
    errorMsg.value = '加载决策设置失败';
  } finally {
    loading.value = false;
  }
}

/** 切换总开关：立即保存；若开启时模型 ID 为空则先解锁配置栏并提示补充后保存 */
async function toggleMasterEnabled() {
  if (loading.value || saving.value || masterToggling.value) return;
  const next = !enabledDraft.value;
  errorMsg.value = '';
  savedTip.value = '';

  if (next && !modelIdDraft.value.trim()) {
    enabledDraft.value = true;
    errorMsg.value = '已解锁配置：请先填写模型 ID 后点击「保存」以启用';
    return;
  }

  const api = window.pywebview?.api;
  if (!api?.set_decision_settings) {
    enabledDraft.value = next;
    syncStoreDecisionEnabled(next);
    return;
  }

  masterToggling.value = true;
  enabledDraft.value = next;
  syncStoreDecisionEnabled(next);
  try {
    const res = await api.set_decision_settings({ enabled: next });
    if (res?.ok) {
      const persisted = res.settings ? res.settings.enabled === true : next;
      enabledDraft.value = persisted;
      savedSnapshot.value.enabled = persisted;
      syncStoreDecisionEnabled(persisted);
      showSavedTip(persisted ? '已启用决策模型' : '已关闭决策模型');
      void loadScenes();
    } else {
      enabledDraft.value = !next;
      syncStoreDecisionEnabled(!next);
      errorMsg.value = res?.msg || '切换决策模型状态失败';
    }
  } catch {
    enabledDraft.value = !next;
    syncStoreDecisionEnabled(!next);
    errorMsg.value = '切换决策模型状态失败';
  } finally {
    masterToggling.value = false;
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
      timeout_ms: timeout,
    });
    if (res?.ok) {
      if (res.settings) applySettings(res.settings);
      showSavedTip(res.msg || '已保存');
      loadScenes();
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
    loadAuditSummary();
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

/** 业务场景条目归一：容忍后端字段缺失/扩展，避免渲染崩溃 */
function normalizeScene(raw: Partial<DecisionSceneRow>): DecisionSceneRow {
  const mode: DecisionSceneMode = raw.mode === 'live' ? 'live' : 'off';
  const threshold = Number(raw.threshold ?? 0.5);
  return {
    scene: String(raw.scene ?? ''),
    label: String(raw.label ?? raw.scene ?? ''),
    description: String(raw.description ?? ''),
    mode,
    mode_text: String(raw.mode_text ?? (mode === 'live' ? '开启' : '关闭')),
    enabled: raw.enabled === true || mode === 'live',
    threshold: Number.isFinite(threshold) ? threshold : 0.5,
    timeout_ms: Number(raw.timeout_ms ?? 1500) || 1500,
  };
}

function applyScenes(rows: Partial<DecisionSceneRow>[] | undefined) {
  sceneRows.value = Array.isArray(rows) ? rows.map(normalizeScene).filter((row) => row.scene) : [];
}

/** 拉取业务场景快照；接口缺失/异常一律返回 null（绝不抛错） */
async function fetchSceneRows(): Promise<Partial<DecisionSceneRow>[] | null> {
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_scenes) return null;
    const res = await api.get_decision_scenes();
    return res?.ok && Array.isArray(res.scenes) ? res.scenes : null;
  } catch {
    return null;
  }
}

/** 保存失败时把界面回滚到落盘状态：不能让开关/阈值停在「看起来已生效」的位置 */
async function restoreScenes() {
  const rows = await fetchSceneRows();
  if (rows) applyScenes(rows);
}

async function loadScenes() {
  const rows = await fetchSceneRows();
  if (rows) {
    applyScenes(rows);
    return;
  }
  scenesFailed.value = true;
  scenesMsg.value = '加载业务场景失败';
}

function toggleSceneMode(row: DecisionSceneRow) {
  if (controlsDisabled.value || sceneSaving.value) return;
  const nextMode: DecisionSceneMode = row.mode === 'live' ? 'off' : 'live';
  void changeSceneMode(row, nextMode);
}

async function changeSceneMode(row: DecisionSceneRow, mode: DecisionSceneMode) {
  if (sceneSaving.value) return;
  const prevMode = row.mode;
  row.mode = mode;
  row.enabled = mode === 'live';
  sceneSaving.value = row.scene;
  scenesMsg.value = '';
  scenesFailed.value = false;
  try {
    const api = window.pywebview?.api;
    if (!api?.set_decision_scene) {
      row.mode = prevMode;
      row.enabled = prevMode === 'live';
      scenesFailed.value = true;
      scenesMsg.value = '后端接口未就绪，无法保存场景设置';
      await restoreScenes();
      return;
    }
    const res = await api.set_decision_scene(row.scene, mode);
    if (res?.ok) {
      if (res.scenes) applyScenes(res.scenes);
      scenesMsg.value = res.hint ? `${res.msg || '已保存'}；${res.hint}` : String(res.msg || '已保存');
      scenesFailed.value = false;
    } else {
      row.mode = prevMode;
      row.enabled = prevMode === 'live';
      scenesFailed.value = true;
      scenesMsg.value = String(res?.msg || '保存场景设置失败');
      await restoreScenes();
    }
  } catch {
    row.mode = prevMode;
    row.enabled = prevMode === 'live';
    scenesFailed.value = true;
    scenesMsg.value = '保存场景设置失败';
    await restoreScenes();
  } finally {
    sceneSaving.value = '';
  }
}

function previewSceneThreshold(row: DecisionSceneRow, event: Event) {
  const value = Number((event.target as HTMLInputElement).value);
  if (Number.isFinite(value)) {
    row.threshold = Math.min(1, Math.max(0, value));
  }
}

async function changeSceneThreshold(row: DecisionSceneRow, event: Event) {
  if (sceneSaving.value) return;
  const input = event.target as HTMLInputElement;
  const raw = Number(input.value);
  if (input.value.trim() === '' || !Number.isFinite(raw)) {
    scenesFailed.value = true;
    scenesMsg.value = `${row.label}场景阈值必须是 0~1 之间的数字`;
    input.value = String(row.threshold);
    return;
  }
  const threshold = Math.round(Math.min(1, Math.max(0, raw)) * 100) / 100;
  row.threshold = threshold;
  sceneSaving.value = row.scene;
  scenesMsg.value = '';
  scenesFailed.value = false;
  try {
    const api = window.pywebview?.api;
    if (!api?.set_decision_scene) {
      scenesFailed.value = true;
      scenesMsg.value = '后端接口未就绪，无法保存场景设置';
      return;
    }
    const res = await api.set_decision_scene(row.scene, row.mode, threshold);
    if (res?.ok) {
      if (res.scenes) applyScenes(res.scenes);
      scenesMsg.value = `${row.label}场景阈值已保存为 ${threshold}`;
    } else {
      scenesFailed.value = true;
      scenesMsg.value = String(res?.msg || '保存阈值失败');
      await restoreScenes();
    }
  } catch {
    scenesFailed.value = true;
    scenesMsg.value = '保存阈值失败';
    await restoreScenes();
  } finally {
    sceneSaving.value = '';
  }
}

async function toggleAuditEnabled() {
  if (loading.value || saving.value || auditToggling.value) return;
  const next = !auditEnabledDraft.value;
  const api = window.pywebview?.api;
  if (!api?.set_decision_settings) {
    showAuditMsg('后端接口未就绪，无法保存审计开关', true);
    return;
  }
  auditToggling.value = true;
  auditEnabledDraft.value = next;
  try {
    const res = await api.set_decision_settings({ audit_enabled: next });
    if (res?.ok) {
      const persisted = res.settings?.audit_enabled !== undefined ? res.settings.audit_enabled !== false : next;
      auditEnabledDraft.value = persisted;
      showAuditMsg(persisted ? '已开启决策审计日志' : '已关闭决策审计日志');
    } else {
      auditEnabledDraft.value = !next;
      showAuditMsg(res?.msg || '保存审计开关失败', true);
    }
  } catch {
    auditEnabledDraft.value = !next;
    showAuditMsg('保存审计开关失败', true);
  } finally {
    auditToggling.value = false;
  }
}

let auditRequestSeq = 0;

function collapseAuditDetail() {
  auditDetailSeq += 1;
  expandedAuditIndex.value = null;
  auditDetail.value = null;
  auditDetailError.value = '';
  auditDetailLoading.value = false;
}

async function toggleAuditDetail(index: number, item: DecisionAuditRecentItem) {
  if (expandedAuditIndex.value === index) {
    collapseAuditDetail();
    return;
  }
  expandedAuditIndex.value = index;
  auditDetail.value = null;
  auditDetailError.value = '';
  const traceId = String(item.trace_id || '').trim();
  if (!traceId) return;
  const seq = (auditDetailSeq += 1);
  auditDetailLoading.value = true;
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_record_detail) {
      if (seq === auditDetailSeq) auditDetailError.value = '当前版本不支持查看详情';
      return;
    }
    const res = await api.get_decision_record_detail(traceId);
    if (seq !== auditDetailSeq) return;
    if (res?.ok && res.record) {
      auditDetail.value = res.record;
    } else {
      auditDetailError.value = res?.msg || '未找到这条决策记录';
    }
  } catch {
    if (seq === auditDetailSeq) auditDetailError.value = '读取详情失败';
  } finally {
    if (seq === auditDetailSeq) auditDetailLoading.value = false;
  }
}

function prettyDetailRequest(record: DecisionRecordDetail['record']): string {
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

function prettyDetailAnswers(record: DecisionRecordDetail['record']): string {
  if (!record) return '';
  return JSON.stringify(record.answers ?? null, null, 2);
}

function setAuditScope(scope: 'all' | 'session') {
  if (auditScope.value === scope) return;
  auditScope.value = scope;
  void loadAuditSummary();
}

async function loadAuditSummary() {
  const seq = (auditRequestSeq += 1);
  collapseAuditDetail();
  try {
    const api = window.pywebview?.api;
    if (!api?.get_decision_audit_summary) return;
    const scope = auditScope.value;
    const sessionName = activeSessionName.value;
    if (scope === 'session' && !sessionName) {
      auditSummary.value = {
        ok: true,
        today_count: 0,
        today_adopted_count: 0,
        today_fallback_count: 0,
        recent: [],
      };
      return;
    }
    const res =
      scope === 'session'
        ? await api.get_decision_audit_summary(10, sessionName, true)
        : await api.get_decision_audit_summary(10);
    if (seq !== auditRequestSeq) return;
    if (res?.ok) {
      auditSummary.value = {
        ok: true,
        today_count: Number(res.today_count ?? 0),
        today_adopted_count: Number(res.today_adopted_count ?? 0),
        today_fallback_count: Number(res.today_fallback_count ?? 0),
        ...(typeof res.session_total_count === 'number'
          ? { session_total_count: res.session_total_count }
          : {}),
        ...(typeof res.global_today_count === 'number'
          ? { global_today_count: res.global_today_count }
          : {}),
        recent: Array.isArray(res.recent) ? res.recent : [],
      };
    }
  } catch {
  }
}

watch(activeSessionName, (name) => {
  if (auditScope.value === 'session') {
    if (!name) auditScope.value = 'all';
    void loadAuditSummary();
  }
});

const initialPinia = getActivePinia();
const appStore = initialPinia ? useAppStore(initialPinia) : null;
if (appStore) {
  watch(
    () => appStore.decisionEventTick,
    () => {
      void loadAuditSummary();
    },
  );
}

onUnmounted(() => {
  if (savedTipTimer) clearTimeout(savedTipTimer);
  if (auditMsgTimer) clearTimeout(auditMsgTimer);
});

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
  loadScenes();
  loadAuditSummary();
}

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
  } finally {
    channelsLoading.value = false;
  }
}

onMounted(() => {
  loadSettings();
  loadScenes();
  loadProviders();
  loadAuditSummary();
});
</script>

<style scoped>
.decision-settings {
  max-width: 880px;
  margin: 0 auto;
  padding-bottom: 24px;
}

.decision-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 22px;
}

.decision-header h1 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}

.decision-header p {
  margin: 0;
  color: var(--text-faint);
  font-size: 13px;
  line-height: 1.5;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.dirty-pill {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--warning, #d97706);
  background: color-mix(in srgb, var(--warning, #d97706) 11%, transparent);
  border: 1px solid color-mix(in srgb, var(--warning, #d97706) 28%, transparent);
}

.refresh-button {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text-dim);
  cursor: pointer;
  transition: background .15s ease, color .15s ease;
}

.refresh-button:hover:not(:disabled) {
  color: var(--text);
  background: var(--bg3);
}

.refresh-button:disabled {
  opacity: .55;
  cursor: wait;
}

/* 通用分组与卡片容器（与系统设置同源） */
.setting-group {
  margin-top: 24px;
}

.group-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 10px 2px;
}

.group-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 10px 2px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-dim);
}

.group-title-row .group-title {
  margin: 0;
}

.group-meta {
  font-size: 11.5px;
  color: var(--text-faint);
}

.setting-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg1);
  overflow: hidden;
}

/* 顶部总控卡片 */
.hero-master-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 20px;
}

.hero-copy {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  min-width: 0;
  flex: 1 1 auto;
}

.hero-icon {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: none;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
  transition: background .18s ease, color .18s ease, border-color .18s ease;
}

.hero-icon.on {
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 32%, transparent);
  color: var(--accent);
}

.hero-text {
  min-width: 0;
}

.hero-title-line {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.hero-title-line h2 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 600;
  color: var(--text);
}

.hero-text p {
  margin: 0;
  color: var(--text-faint);
  font-size: 12.5px;
  line-height: 1.55;
}

.state-pill {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
}

.state-pill.on {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}

.hero-probe-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 11px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
}

.probe-status-left {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  min-width: 0;
  flex: 1 1 auto;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
  flex: none;
}

.status-dot.ok {
  background: var(--green);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 18%, transparent);
}

.status-dot.bad {
  background: var(--red);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--red) 18%, transparent);
}

.status-text {
  color: var(--text-dim);
  font-size: 12.5px;
}

.runtime-pill {
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-faint);
  font-size: 11px;
}

.probe-detail-box {
  padding: 12px 20px 14px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
}

.probe-chips {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.probe-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  border: 1px solid var(--border);
  background: var(--bg1);
}

.probe-chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
}

.probe-chip.ok {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 32%, transparent);
}

.probe-chip.bad {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 32%, transparent);
}

/* 统一开关按钮（role="switch"）：同时适配浅色与深色主题 */
.setting-switch {
  position: relative;
  flex: 0 0 auto;
  width: 44px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--text) 13%, var(--bg2));
  cursor: pointer;
  transition: background .18s ease, border-color .18s ease, opacity .18s ease;
}

.setting-switch:hover:not(:disabled):not([aria-checked='true']) {
  background: color-mix(in srgb, var(--text) 20%, var(--bg2));
}

.setting-switch[aria-checked='true'] {
  background: var(--accent);
  border-color: var(--accent);
}

.setting-switch .switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 999px;
  background: #ffffff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.24);
  transition: transform .18s cubic-bezier(.22, 1, .36, 1);
}

.setting-switch[aria-checked='true'] .switch-knob {
  transform: translateX(20px);
}

.setting-switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 24%, transparent);
}

.setting-switch:disabled {
  cursor: not-allowed;
  opacity: .48;
}

/* 设置项行布局 */
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 15px 20px;
  border-bottom: 1px solid var(--border);
}

.setting-row--top {
  align-items: flex-start;
}

.setting-row:last-child {
  border-bottom: 0;
}

.setting-copy {
  flex: 1 1 auto;
  min-width: 0;
}

.setting-copy h3 {
  margin: 0 0 3px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.setting-copy p {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.55;
}

.setting-control {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}

.setting-control--wide {
  flex: 0 1 430px;
  width: 430px;
  max-width: 100%;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}

.text-field {
  width: 100%;
  box-sizing: border-box;
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
}

.text-field:focus {
  outline: none;
  border-color: var(--accent);
  background: var(--bg1);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent);
}

.text-field:disabled {
  cursor: not-allowed;
  opacity: .55;
}

.mono {
  font-family: var(--mono);
}

.model-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.model-row .text-field {
  flex: 1;
  min-width: 0;
}

.model-select {
  margin-top: 2px;
}

.channel-select {
  min-width: 240px;
  max-width: 340px;
  padding-right: 28px;
}

/* 分段选择按钮（替代原生圆点单选框） */
.seg-group {
  display: inline-flex;
  align-items: center;
  padding: 3px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg2);
  gap: 2px;
}

.seg-option {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 29px;
  padding: 0 11px;
  border: 1px solid transparent;
  border-radius: 7px;
  background: transparent;
  color: var(--text-dim);
  font-size: 12.5px;
  cursor: pointer;
  transition: background .15s ease, color .15s ease, border-color .15s ease, box-shadow .15s ease;
}

.seg-option:hover:not(:disabled):not(.active) {
  color: var(--text);
  background: var(--bg3);
}

.seg-option.active {
  background: var(--bg1);
  border-color: var(--xy-border-strong, var(--border));
  color: var(--text);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
}

.seg-option:disabled {
  cursor: not-allowed;
  opacity: .52;
}

.seg-tag {
  padding: 0 5px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 500;
  color: var(--accent);
  background: var(--accent-soft);
}

.seg-option:not(.active) .seg-tag {
  color: var(--text-faint);
  background: var(--bg3);
}

.key-policy-control {
  flex-wrap: wrap;
}

.key-index-inline {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-dim);
  transition: opacity .15s ease;
}

.key-index-inline.muted {
  opacity: .48;
}

.key-index-input {
  width: 58px;
  min-height: 34px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  text-align: center;
}

.key-index-input:disabled {
  cursor: not-allowed;
  opacity: .6;
}

.timeout-control {
  flex-wrap: wrap;
}

.preset-chips {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.preset-chip {
  min-height: 28px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 11.5px;
  cursor: pointer;
  transition: background .15s ease, color .15s ease, border-color .15s ease;
}

.preset-chip:hover:not(:disabled) {
  color: var(--text);
  background: var(--bg3);
}

.preset-chip.active {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  background: var(--accent-soft);
  font-weight: 600;
}

.preset-chip:disabled {
  opacity: .5;
  cursor: not-allowed;
}

.input-with-unit {
  position: relative;
  display: inline-flex;
  align-items: center;
}

.timeout-input {
  width: 106px;
  padding-right: 30px;
}

.unit-suffix {
  position: absolute;
  right: 10px;
  color: var(--text-faint);
  font-size: 11.5px;
  pointer-events: none;
}

.field-hint {
  margin: 4px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.55;
}

.field-hint.warn {
  color: var(--warning);
}

/* 模型与连接配置底部保存条 */
.config-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 13px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
  flex-wrap: wrap;
}

.config-feedback {
  flex: 1 1 220px;
  min-width: 0;
}

.decision-error {
  margin: 0;
  color: var(--red);
  font-size: 12.5px;
}

.decision-saved {
  margin: 0;
  color: var(--green);
  font-size: 12.5px;
}

.decision-dirty-hint {
  margin: 0;
  color: var(--warning);
  font-size: 12px;
  font-weight: 500;
}

.decision-muted-hint {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
}

.decision-actions {
  display: flex;
  gap: 10px;
  flex: none;
}

.primary-button,
.secondary-button {
  min-height: 34px;
  padding: 0 16px;
  border-radius: 8px;
  border: 1px solid var(--border);
  font-weight: 600;
  cursor: pointer;
  font-size: 12.5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: opacity .15s ease, background .15s ease, border-color .15s ease;
}

.primary-button {
  background: var(--text);
  color: var(--bg0);
  border-color: transparent;
}

.primary-button:hover:not(:disabled) {
  opacity: .88;
}

.primary-button:disabled {
  opacity: .48;
  cursor: not-allowed;
}

.secondary-button {
  background: var(--bg1);
  color: var(--text);
}

.secondary-button:hover:not(:disabled) {
  background: var(--bg3);
}

.secondary-button:disabled {
  opacity: .48;
  cursor: not-allowed;
}

.secondary-button.compact {
  min-height: 32px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 500;
}

/* 业务场景开关 */
.scenes-standby-banner {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-dim);
  font-size: 12px;
}

.scene-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 15px 20px;
  border-bottom: 1px solid var(--border);
  transition: background .15s ease;
}

.scene-row.is-live {
  background: color-mix(in srgb, var(--accent) 3.5%, var(--bg1));
}

.scene-row.is-disabled {
  opacity: .72;
}

.scene-copy {
  flex: 1 1 auto;
  min-width: 0;
}

.scene-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.scene-name {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--text);
}

.scene-mode-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--text-faint);
  background: var(--bg2);
}

.scene-mode-badge.live {
  color: var(--warning);
  border-color: color-mix(in srgb, var(--warning) 40%, transparent);
  background: color-mix(in srgb, var(--warning) 10%, transparent);
}

.scene-desc {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.55;
}

.scene-live-warning {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin: 6px 0 0;
  color: var(--warning);
  font-size: 11.5px;
  line-height: 1.45;
}

.scene-controls {
  display: flex;
  align-items: center;
  gap: 16px;
  flex: none;
}

.threshold-box {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px 4px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
}

.threshold-label {
  color: var(--text-dim);
  font-size: 11.5px;
  white-space: nowrap;
}

.threshold-slider {
  width: 78px;
  accent-color: var(--accent);
  cursor: pointer;
}

.threshold-slider:disabled {
  cursor: not-allowed;
  opacity: .5;
}

.scene-threshold-input {
  width: 66px;
  min-height: 28px;
  padding: 0 7px;
  border-radius: 6px;
  background: var(--bg1);
  font-size: 12.5px;
  text-align: center;
}

.scenes-empty {
  padding: 18px 20px;
  color: var(--text-faint);
  font-size: 12.5px;
}

.scenes-footer {
  padding: 12px 20px 14px;
  background: var(--bg2);
  display: grid;
  gap: 8px;
}

.scenes-msg {
  margin: 0;
  color: var(--green);
  font-size: 12px;
  line-height: 1.55;
}

.scenes-msg.warn {
  color: var(--warning);
}

.auto-scenes-strip {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.auto-scenes-label {
  color: var(--text-faint);
  font-size: 11.5px;
}

.auto-scene-pill {
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-faint);
  font-size: 11px;
}

.auto-scene-pill.active {
  color: var(--text-dim);
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
}

/* 决策记录与审计 */
.audit-body {
  padding: 14px 20px 18px;
}

.audit-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.audit-scope-tabs {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px;
  background: var(--bg2);
  gap: 2px;
}

.scope-tab {
  min-height: 26px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  cursor: pointer;
}

.scope-tab.active {
  background: var(--bg1);
  border-color: var(--xy-border-strong, var(--border));
  color: var(--text);
  font-weight: 600;
}

.scope-tab:disabled {
  opacity: .45;
  cursor: not-allowed;
}

.audit-summary-text {
  color: var(--text-dim);
  font-size: 12px;
}

.audit-kpi-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.audit-kpi-cell {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
}

.audit-kpi-cell .k {
  font-size: 11.5px;
  color: var(--text-faint);
}

.audit-kpi-cell .v {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}

.audit-kpi-cell.green .v {
  color: var(--green);
}

.audit-kpi-cell.warn .v {
  color: var(--warning);
}

.audit-list {
  list-style: none;
  margin: 12px 0 0;
  padding: 0;
  display: grid;
  gap: 6px;
}

.audit-item {
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  padding: 9px 12px;
  font-size: 12px;
  color: var(--text-dim);
}

.audit-item.clickable {
  cursor: pointer;
  transition: border-color .15s ease, background .15s ease;
}

.audit-item.clickable:hover,
.audit-item.open {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
  background: var(--bg1);
}

.audit-item-main {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}

.audit-item-left,
.audit-item-right {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.audit-scene-tag {
  font-weight: 600;
  color: var(--text);
}

.audit-scene-code {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-faint);
  padding: 1px 5px;
  border-radius: 5px;
  background: var(--bg3);
}

.audit-badge {
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  border: 1px solid var(--border);
}

.audit-badge.ok {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}

.audit-badge.warn {
  color: var(--warning);
  border-color: color-mix(in srgb, var(--warning) 38%, transparent);
  background: color-mix(in srgb, var(--warning) 10%, transparent);
}

.audit-badge.muted {
  color: var(--text-faint);
}

.audit-answer {
  color: var(--accent);
  font-size: 11.5px;
}

.audit-session {
  font-size: 11.5px;
  color: var(--text-dim);
}

.audit-meta {
  font-size: 11.5px;
  color: var(--text-faint);
}

.audit-chevron {
  color: var(--text-faint);
  transition: transform .15s ease;
}

.audit-chevron.rotated {
  transform: rotate(90deg);
}

.audit-digest {
  margin-top: 5px;
  color: var(--text-faint);
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}

.audit-detail {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}

.audit-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.audit-detail-title {
  margin-bottom: 5px;
  font-size: 11.5px;
  font-weight: 600;
  color: var(--text-dim);
}

.audit-detail-json {
  margin: 0;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--code-bg, var(--bg2));
  color: var(--text);
  font-family: var(--mono);
  font-size: 11px;
  line-height: 1.5;
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.audit-empty {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
}

.decision-note {
  margin: 8px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}

.decision-note.warn {
  color: var(--warning);
}

@media (max-width: 768px) {
  .decision-header {
    flex-direction: column;
  }
  .hero-master-row {
    align-items: flex-start;
  }
  .setting-row {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 14px 16px;
  }
  .setting-control,
  .setting-control--wide {
    width: 100%;
    justify-content: flex-start;
  }
  .channel-select {
    max-width: none;
    width: 100%;
  }
  .model-row {
    flex-direction: column;
    align-items: stretch;
  }
  .scene-row {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 14px 16px;
  }
  .scene-controls {
    justify-content: space-between;
  }
  .audit-kpi-grid,
  .audit-detail-grid {
    grid-template-columns: 1fr;
  }
}
</style>
