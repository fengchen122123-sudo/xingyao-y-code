<template>
  <div class="system-settings" data-test="system-settings-page">
    <header class="system-header">
      <div>
        <h1>系统设置</h1>
        <p>管理 星瑶Code 的系统级运行行为；后续系统选项将在此集中提供。</p>
      </div>
      <button class="refresh-button" type="button" title="刷新检测结果" @click="store.refreshSystemSettings()">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="settings-list">
      <div class="setting-row">
        <div class="setting-copy">
          <h2>默认命令解释器</h2>
          <p>AI 命令、后台任务、计划验证和右侧终端统一优先使用所选解释器。</p>
        </div>
        <select
          data-test="command-shell-select"
          class="setting-select"
          :value="store.systemSettings.command_shell"
          :disabled="saving"
          @change="selectShell(($event.target as HTMLSelectElement).value)"
        >
          <option
            v-for="option in store.systemSettings.command_shell_options"
            :key="option.id"
            :data-test="`shell-${option.id}`"
            :value="option.id"
            :disabled="!option.available"
          >
            {{ option.label }}{{ option.available ? '' : `（${option.unavailable_reason || '未安装'}）` }}
          </option>
        </select>
      </div>
      <div class="setting-row">
        <div class="setting-copy">
          <h2>远程控制 token 过期时间</h2>
          <p>手机扫码配对后，token 在所选时长后自动失效，到期需重新扫码。</p>
        </div>
        <select
          data-test="remote-token-ttl-select"
          class="setting-select"
          :value="store.systemSettings.remote_token_ttl"
          :disabled="saving"
          @change="selectTokenTtl(($event.target as HTMLSelectElement).value)"
        >
          <option :value="0">永久</option>
          <option :value="3600">1 小时</option>
          <option :value="43200">12 小时</option>
          <option :value="86400">24 小时</option>
          <option :value="604800">7 天</option>
        </select>
      </div>
      <div class="setting-row">
        <div class="setting-copy">
          <h2>单任务最大模型轮次</h2>
          <p>一次用户任务最多发起多少次模型请求；工具调用数可能高于该值。修改后仅影响新任务。</p>
        </div>
        <div class="setting-number-group">
          <input
            data-test="turn-step-limit-input"
            v-model="turnStepLimitDraft"
            class="setting-number"
            type="number"
            step="1"
            :min="store.systemSettings.agent_turn_step_limit_min"
            :max="store.systemSettings.agent_turn_step_limit_max"
            :disabled="saving"
          />
          <button
            data-test="save-turn-step-limit"
            class="setting-save"
            type="button"
            :disabled="saving || !!turnStepLimitError || !turnStepLimitChanged"
            @click="saveTurnStepLimit"
          >
            保存
          </button>
        </div>
      </div>
      <p v-if="turnStepLimitError" data-test="turn-step-limit-error" class="setting-error">
        {{ turnStepLimitError }}
      </p>
      <div class="setting-row">
        <div class="setting-copy">
          <h2>上下文水位基准</h2>
          <p>触发水位按 min(模型窗口, 本值) 计算；留空或填 0 表示不设基准、直接跟随模型真实窗口。</p>
        </div>
        <div class="setting-number-group">
          <input
            data-test="context-watermark-base-input"
            v-model="contextWatermarkBaseDraft"
            class="setting-number"
            type="number"
            step="1000"
            :min="store.systemSettings.context_watermark_base_min"
            :max="store.systemSettings.context_watermark_base_max"
            :disabled="saving"
            @input="contextWatermarkBaseSaveError = ''"
          />
          <button
            data-test="save-context-watermark-base"
            class="setting-save"
            type="button"
            :disabled="saving || !!contextWatermarkBaseError || !contextWatermarkBaseChanged"
            @click="saveContextWatermarkBase"
          >
            保存
          </button>
        </div>
      </div>
      <p v-if="contextWatermarkBaseError" data-test="context-watermark-base-error" class="setting-error">
        {{ contextWatermarkBaseError }}
      </p>
      <div class="setting-row">
        <div class="setting-copy">
          <h2>AI 生成内容目录</h2>
          <p>AI 生成的图片、下载的网页图片与临时脚本统一落到此目录；留空恢复默认（{{ store.systemSettings.ai_output_dir_default }}）。</p>
        </div>
        <div class="setting-number-group">
          <input
            data-test="ai-output-dir-input"
            v-model="aiOutputDirDraft"
            class="setting-number"
            type="text"
            :placeholder="store.systemSettings.ai_output_dir_default"
            :disabled="saving"
          />
          <button
            data-test="pick-ai-output-dir"
            class="setting-save"
            type="button"
            :disabled="saving"
            @click="pickAiOutputDir"
          >
            选择目录
          </button>
          <button
            data-test="save-ai-output-dir"
            class="setting-save"
            type="button"
            :disabled="saving || !aiOutputDirChanged"
            @click="saveAiOutputDir"
          >
            保存
          </button>
        </div>
      </div>
      <p v-if="aiOutputDirError" data-test="ai-output-dir-error" class="setting-error">
        {{ aiOutputDirError }}
      </p>
      <p v-if="store.systemSettings.command_shell_error" class="setting-error">
        {{ store.systemSettings.command_shell_error }}
      </p>
      <p class="setting-note">
        命令失败时错误会原样返回给模型，由模型调整语法或显式指定其他已安装解释器，系统不会静默降级。
      </p>
    </section>

    <section class="computer-settings" data-test="computer-settings-section">
      <header class="computer-header">
        <h2 class="computer-title"><AppIcon name="zap" :size="14" /> 电脑操控</h2>
      </header>

      <div class="setting-row">
        <div class="setting-copy">
          <h2>浏览器操控</h2>
          <p>在专用 Edge/Chrome 页面中观察、点击、输入、滚动和导航。</p>
        </div>
        <button
          v-if="!isMobile"
          type="button"
          class="setting-switch"
          role="switch"
          data-test="browser-control-switch"
          :aria-checked="store.systemSettings.browser_control_enabled ? 'true' : 'false'"
          :disabled="saving"
          @click="toggleBrowserControl"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <!-- Playwright 按需组件（core/components.py）：安装包不再内置 Playwright，
           未安装时在这里按需下载；下载进度走 playwright_component_progress 桥事件 -->
      <div class="setting-row playwright-component-card" data-test="playwright-component-card">
        <div class="setting-copy">
          <h2>浏览器操控组件</h2>
          <p>浏览器操控依赖 Playwright 组件（约 25 MB），安装包不再内置，需要时在这里下载。</p>
          <div class="playwright-component-state">
            <span
              v-if="playwrightComponent.installed"
              class="playwright-component-ready"
              data-test="playwright-component-ready"
            >已就绪 · v{{ playwrightComponent.version || '未知' }}</span>
            <template v-else-if="playwrightComponent.downloading">
              <div
                class="playwright-component-progress"
                role="progressbar"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="playwrightPercent"
                data-test="playwright-component-progress"
              >
                <div class="playwright-component-progress-bar" :style="{ width: playwrightPercent + '%' }" />
              </div>
              <span class="playwright-component-size" data-test="playwright-component-size">{{ playwrightSizeText }}</span>
            </template>
            <span v-else class="playwright-component-missing" data-test="playwright-component-missing">未安装</span>
          </div>
          <p v-if="playwrightComponent.error" class="setting-error" data-test="playwright-component-error">
            {{ playwrightComponent.error }}
          </p>
        </div>
        <div v-if="!isMobile" class="playwright-component-actions">
          <button
            v-if="!playwrightComponent.installed && !playwrightComponent.downloading"
            data-test="playwright-component-download"
            class="setting-save"
            type="button"
            :disabled="playwrightBusy"
            @click="downloadPlaywrightComponent"
          >
            下载组件
          </button>
          <button
            v-if="playwrightComponent.downloading"
            data-test="playwright-component-cancel"
            class="setting-save"
            type="button"
            :disabled="playwrightBusy"
            @click="cancelPlaywrightComponentDownload"
          >
            取消下载
          </button>
        </div>
      </div>

      <div class="setting-row">
        <div class="setting-copy">
          <h2>默认浏览器</h2>
          <p>操控使用的浏览器与各自专用配置目录绑定{{ activeComputerRun ? '；当前有操控会话进行中，需先停止才能切换' : '' }}。</p>
        </div>
        <select
          v-if="!isMobile"
          data-test="computer-browser-select"
          class="setting-select"
          :value="store.systemSettings.computer_browser"
          :disabled="saving || activeComputerRun"
          @change="selectComputerBrowser(($event.target as HTMLSelectElement).value)"
        >
          <option
            v-for="option in store.systemSettings.computer_browser_options"
            :key="option.browser"
            :data-test="`browser-option-${option.browser}`"
            :value="option.browser"
            :disabled="!option.available"
          >
            {{ option.label }}{{ option.available ? '' : `（${option.unavailable_reason || '未安装'}）` }}
          </option>
        </select>
      </div>

      <div class="setting-row">
        <div class="setting-copy">
          <h2>完整电脑操控</h2>
          <p>模型可操作整个 Windows 桌面（鼠标、键盘与任意可见应用窗口）；开启前要求紧急停止热键可用。</p>
        </div>
        <button
          v-if="!isMobile"
          type="button"
          class="setting-switch"
          role="switch"
          data-test="computer-control-switch"
          :aria-checked="store.systemSettings.computer_control_enabled ? 'true' : 'false'"
          :disabled="saving"
          @click="toggleComputerControl"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <p v-if="isMobile" class="computer-mobile-note" data-test="computer-mobile-note">
        电脑操控开关与默认浏览器请在桌面端开启或修改；手机端仅可查看状态与紧急停止。
      </p>

      <div class="setting-row">
        <div class="setting-copy">
          <h2>专用配置目录</h2>
          <p>登录态与浏览器数据只保存在本机该目录，不读取日常浏览器的配置、Cookie 或扩展。</p>
        </div>
        <span class="setting-path mono" data-test="computer-profile-path">{{ store.systemSettings.computer_profile_path }}</span>
      </div>

      <p
        v-if="computerModelCapabilityBlocked"
        class="computer-model-hint blocked"
        data-test="computer-model-capability-hint"
      >{{ computerModelCapabilityHint }}</p>

      <details class="setting-details" data-test="computer-diagnostics">
        <summary>环境检测与安全提示</summary>
        <div class="setting-details-body">
          <p class="computer-privacy" data-test="computer-privacy-notice"><AppIcon name="triangle-alert" :size="13" />开启后，当前页面或桌面截图会发送给当前模型 Provider；星瑶Code 不会逐动作询问。登录、验证码和凭据请在专用浏览器中由你手动完成。紧急停止：Ctrl+Alt+Esc。</p>

          <p
            v-if="computerModelCapabilityHint && !computerModelCapabilityBlocked"
            class="computer-model-hint"
            data-test="computer-diagnostics-capability-hint"
          >{{ computerModelCapabilityHint }}</p>

          <ul class="computer-preflight" data-test="computer-preflight-list">
            <li v-for="item in preflightItems" :key="item.label" :class="{ ok: item.ok }">
              <AppIcon :name="item.ok ? 'check' : 'x'" :size="12" :stroke-width="2.25" />
              <span>{{ item.label }}</span>
            </li>
          </ul>

          <p class="setting-details-note">
            浏览器操控与完整电脑操控是两个独立开关，默认关闭；开启后 星瑶Code 不再逐动作询问授权。电脑操控要求模型支持视觉（识图）与工具调用，两项能力都开启的模型在开关开启后自动可用，无需逐个配置。
          </p>
        </div>
      </details>
    </section>

    <!-- 网页搜索按需组件（core/components.py 的 SEARCH_SPEC）：ddgs 及其
         primp/lxml/fake_useragent 二进制依赖已从主安装包剔除，未安装时在这里
         按需下载；下载进度走 search_component_progress 桥事件 -->
    <section class="search-component-settings" data-test="search-component-section">
      <header class="computer-header">
        <h2 class="computer-title"><AppIcon name="search" :size="14" /> 网页搜索</h2>
      </header>

      <div class="setting-row search-component-card" data-test="search-component-card">
        <div class="setting-copy">
          <h2>网页搜索组件</h2>
          <p>网页搜索组件 · 约 18 MB · 让星瑶能够联网搜索最新信息</p>
          <div class="search-component-state">
            <span
              v-if="searchComponent.installed"
              class="search-component-ready"
              data-test="search-component-ready"
            >已就绪 · v{{ searchComponent.version || '未知' }}</span>
            <template v-else-if="searchComponent.downloading">
              <div
                class="search-component-progress"
                role="progressbar"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="searchPercent"
                data-test="search-component-progress"
              >
                <div class="search-component-progress-bar" :style="{ width: searchPercent + '%' }" />
              </div>
              <span class="search-component-size" data-test="search-component-size">{{ searchSizeText }}</span>
            </template>
            <span v-else class="search-component-missing" data-test="search-component-missing">未安装</span>
          </div>
          <p v-if="searchComponent.error" class="setting-error" data-test="search-component-error">
            {{ searchComponent.error }}
          </p>
        </div>
        <div v-if="!isMobile" class="search-component-actions">
          <button
            v-if="!searchComponent.installed && !searchComponent.downloading"
            data-test="search-component-download"
            class="setting-save"
            type="button"
            :disabled="searchBusy"
            @click="downloadSearchComponent"
          >
            下载组件
          </button>
          <button
            v-if="searchComponent.downloading"
            data-test="search-component-cancel"
            class="setting-save"
            type="button"
            :disabled="searchBusy"
            @click="cancelSearchComponentDownload"
          >
            取消下载
          </button>
        </div>
      </div>
    </section>

    <section class="doctor-settings" data-test="doctor-settings-section">
      <header class="doctor-header">
        <h2 class="doctor-title"><AppIcon name="zap" :size="14" /> 运行自检</h2>
        <p>一键检查配置、凭据、数据目录、数据库、MCP 服务、模型端点与运行环境，与 CLI 的 /doctor 同源。</p>
      </header>

      <div class="setting-row">
        <div class="setting-copy">
          <h2>聚合自检</h2>
          <p>模型端点检查需要联网探测（最长 3 秒），期间界面不会冻结；完成后逐项展示结果与建议。</p>
        </div>
        <button
          data-test="run-doctor-button"
          class="setting-save"
          type="button"
          :disabled="doctorRunning"
          @click="runDoctor"
        >
          {{ doctorRunning ? '检查中…' : '运行自检' }}
        </button>
      </div>

      <p v-if="doctorError" data-test="doctor-error" class="setting-error">{{ doctorError }}</p>

      <div v-if="doctorChecks.length" data-test="doctor-results" class="doctor-results">
        <p class="doctor-summary" data-test="doctor-summary">{{ doctorSummaryText }}</p>
        <ul class="doctor-list">
          <li
            v-for="check in doctorChecks"
            :key="check.name"
            class="doctor-item"
            :class="`doctor-item--${check.status}`"
            :data-test="`doctor-check-${check.name}`"
          >
            <span class="doctor-status-icon">
              <AppIcon :name="doctorStatusIcon(check.status)" :size="14" :stroke-width="2.25" />
            </span>
            <div class="doctor-item-body">
              <div class="doctor-item-head">
                <span class="doctor-name">{{ check.name }}</span>
                <span class="doctor-status-text">{{ doctorStatusLabel(check.status) }}</span>
              </div>
              <p v-if="check.detail" class="doctor-detail">{{ check.detail }}</p>
              <p v-if="check.hint" class="doctor-hint">建议：{{ check.hint }}</p>
            </div>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';

import { useAppStore } from '../stores/appStore';
import { useIsMobile } from '../composables/useViewport';
import AppIcon from './AppIcon.vue';
import type { DoctorCheck, DoctorCheckStatus } from '../types/pywebview';

const store = useAppStore();
const saving = ref(false);

// 手机 viewport 不显示可点击启用控件（呈现层约束；后端边界由 Task 15 实施）
const isMobile = useIsMobile();

// 每次进入设置页重拉一次：电脑操控资格提示依赖「当前会话生效模型」，
// 会话级模型随时可能被切换，沿用启动时的快照会显示成别的模型
onMounted(() => {
  void store.refreshSystemSettings({ silent: true });
  // Playwright 组件状态来自磁盘探测（安装包已不再内置），进页必拉一次；
  // 下载中的进度由 playwright_component_progress 桥事件持续合并
  void store.refreshPlaywrightComponentStatus();
  // 网页搜索组件同样是磁盘探测得来的（安装包已不含 ddgs），进页也要拉一次
  void store.refreshSearchComponentStatus();
});

// ── Playwright 按需组件（设置页「浏览器操控组件」卡片）──
// 展示状态一律读 Store 快照，不在组件内单独维护副本（桥事件只更新 Store）
const playwrightComponent = computed(() => store.playwrightComponent);
const playwrightPercent = computed(() => {
  const value = Number(store.playwrightComponent.percent) || 0;
  return Math.min(100, Math.max(0, Math.round(value)));
});
// 已下载/总字节按 MB 展示（后端 total 未知时为 0，此时只显示已下载量）
const playwrightSizeText = computed(() => {
  const format = (bytes: number) => `${(Math.max(0, Number(bytes) || 0) / (1024 * 1024)).toFixed(1)} MB`;
  const total = Number(store.playwrightComponent.total_bytes) || 0;
  const done = Number(store.playwrightComponent.downloaded_bytes) || 0;
  return total > 0 ? `${format(done)} / ${format(total)}` : format(done);
});
// 桥调用在途闸门：下载/取消都只发一次请求，避免重复点击叠加调用
const playwrightBusy = ref(false);

async function downloadPlaywrightComponent() {
  if (playwrightBusy.value) return;
  playwrightBusy.value = true;
  try {
    await store.downloadPlaywrightComponent();
  } finally {
    playwrightBusy.value = false;
  }
}

async function cancelPlaywrightComponentDownload() {
  if (playwrightBusy.value) return;
  playwrightBusy.value = true;
  try {
    await store.cancelPlaywrightComponentDownload();
  } finally {
    playwrightBusy.value = false;
  }
}

// ── 网页搜索按需组件（设置页「网页搜索组件」卡片）──
// 与 Playwright 卡片同构：状态只读 Store 快照，组件内不维护副本（桥事件只更新 Store）
const searchComponent = computed(() => store.searchComponent);
const searchPercent = computed(() => {
  const value = Number(store.searchComponent.percent) || 0;
  return Math.min(100, Math.max(0, Math.round(value)));
});
// 已下载/总字节按 MB 展示（后端 total 未知时为 0，此时只显示已下载量）
const searchSizeText = computed(() => {
  const format = (bytes: number) => `${(Math.max(0, Number(bytes) || 0) / (1024 * 1024)).toFixed(1)} MB`;
  const total = Number(store.searchComponent.total_bytes) || 0;
  const done = Number(store.searchComponent.downloaded_bytes) || 0;
  return total > 0 ? `${format(done)} / ${format(total)}` : format(done);
});
// 桥调用在途闸门：下载/取消都只发一次请求，避免重复点击叠加调用
const searchBusy = ref(false);

async function downloadSearchComponent() {
  if (searchBusy.value) return;
  searchBusy.value = true;
  try {
    await store.downloadSearchComponent();
  } finally {
    searchBusy.value = false;
  }
}

async function cancelSearchComponentDownload() {
  if (searchBusy.value) return;
  searchBusy.value = true;
  try {
    await store.cancelSearchComponentDownload();
  } finally {
    searchBusy.value = false;
  }
}

const turnStepLimitDraft = ref(String(store.systemSettings.agent_turn_step_limit));

watch(
  () => store.systemSettings.agent_turn_step_limit,
  value => { turnStepLimitDraft.value = String(value); },
);

const parsedTurnStepLimit = computed(() => Number(turnStepLimitDraft.value));

// 活动模型电脑操控资格：开关已开但模型不合格时给出可操作的原因提示
const computerModelCapability = computed(() => store.systemSettings.computer_active_model_capability);
const computerModelCapabilityBlocked = computed(() => {
  const capability = computerModelCapability.value;
  const anySwitchOn = store.systemSettings.browser_control_enabled || store.systemSettings.computer_control_enabled;
  return Boolean(anySwitchOn && capability && !capability.eligible);
});
const computerModelCapabilityHint = computed(() => {
  const capability = computerModelCapability.value;
  if (!capability || !capability.model) return '';
  if (computerModelCapabilityBlocked.value) {
    return `当前模型 ${capability.model} 不具备电脑操控资格：${capability.reason || '未声明所需能力'}。可在「模型」设置中勾选“多模态（视觉）”（工具调用默认视为支持）。`;
  }
  if (capability.eligible && (store.systemSettings.browser_control_enabled || store.systemSettings.computer_control_enabled)) {
    return `当前模型 ${capability.model} 已具备电脑操控资格（${capability.protocol === 'native_openai' ? '原生协议' : 'generic'}）。`;
  }
  return '';
});
const turnStepLimitError = computed(() => {
  const value = parsedTurnStepLimit.value;
  const min = store.systemSettings.agent_turn_step_limit_min;
  const max = store.systemSettings.agent_turn_step_limit_max;
  // type="number" 的 v-model 会自动把草稿转为 number，这里统一按字符串校验
  if (!String(turnStepLimitDraft.value).trim() || !Number.isInteger(value)) {
    return '请输入整数';
  }
  if (value < min || value > max) {
    return `请输入 ${min}–${max} 之间的整数`;
  }
  return '';
});
const turnStepLimitChanged = computed(
  () => !turnStepLimitError.value
    && parsedTurnStepLimit.value !== store.systemSettings.agent_turn_step_limit,
);

async function saveTurnStepLimit() {
  if (saving.value || turnStepLimitError.value || !turnStepLimitChanged.value) return;
  saving.value = true;
  try {
    const saved = await store.setAgentTurnStepLimit(parsedTurnStepLimit.value);
    if (!saved) {
      turnStepLimitDraft.value = String(store.systemSettings.agent_turn_step_limit);
    }
  } finally {
    saving.value = false;
  }
}

// 上下文水位基准：草稿为字符串，空串表示不设基准（按 0 提交）；
// 快照为 null（未设置）时同样回显为空串
const contextWatermarkBaseDraft = ref(
  store.systemSettings.context_watermark_base === null ? '' : String(store.systemSettings.context_watermark_base),
);
// 保存失败的行内提示：与校验错误共用同一行提示位
const contextWatermarkBaseSaveError = ref('');

watch(
  () => store.systemSettings.context_watermark_base,
  value => { contextWatermarkBaseDraft.value = value === null ? '' : String(value); },
);

// 空串按 0 处理（表示不设基准）；type="number" 的 v-model 会把草稿转为 number
const parsedContextWatermarkBase = computed(
  () => (String(contextWatermarkBaseDraft.value).trim() ? Number(contextWatermarkBaseDraft.value) : 0),
);

const contextWatermarkBaseValidationError = computed(() => {
  const value = parsedContextWatermarkBase.value;
  const min = store.systemSettings.context_watermark_base_min;
  const max = store.systemSettings.context_watermark_base_max;
  // type="number" 的 v-model 会自动把草稿转为 number，这里统一按字符串校验
  if (!Number.isInteger(value)) {
    return '请输入整数';
  }
  // 0 是「不设基准」的合法值，只有非 0 值才受 min/max 约束
  if (value !== 0 && (value < min || value > max)) {
    return `请输入 0（不设基准）或 ${min}–${max} 之间的整数`;
  }
  return '';
});
const contextWatermarkBaseError = computed(
  () => contextWatermarkBaseValidationError.value || contextWatermarkBaseSaveError.value,
);
const contextWatermarkBaseChanged = computed(
  () => !contextWatermarkBaseValidationError.value
    && parsedContextWatermarkBase.value !== (store.systemSettings.context_watermark_base ?? 0),
);

async function saveContextWatermarkBase() {
  if (saving.value || contextWatermarkBaseError.value || !contextWatermarkBaseChanged.value) return;
  contextWatermarkBaseSaveError.value = '';
  saving.value = true;
  try {
    const saved = await store.setContextWatermarkBase(parsedContextWatermarkBase.value);
    if (!saved) {
      // 失败：草稿回滚为快照值（未设置显示空串），失败原因留在行内提示
      const snapshot = store.systemSettings.context_watermark_base;
      contextWatermarkBaseDraft.value = snapshot === null ? '' : String(snapshot);
      contextWatermarkBaseSaveError.value = '保存失败，请检查输入后重试';
    }
  } finally {
    saving.value = false;
  }
}

// AI 生成内容目录（Task 5）：草稿保存的是原始配置值（未配置为空串），
// 生效路径与默认目录都由后端快照给出，前端不自行拼路径
const aiOutputDirDraft = ref(store.systemSettings.ai_output_dir_setting || '');
const aiOutputDirError = ref('');

watch(
  () => store.systemSettings.ai_output_dir_setting,
  value => { aiOutputDirDraft.value = value || ''; },
);

const aiOutputDirChanged = computed(
  () => aiOutputDirDraft.value.trim() !== (store.systemSettings.ai_output_dir_setting || ''),
);

async function saveAiOutputDir() {
  if (saving.value || !aiOutputDirChanged.value) return;
  aiOutputDirError.value = '';
  saving.value = true;
  try {
    const saved = await store.setAiOutputDir(aiOutputDirDraft.value.trim());
    if (!saved) {
      aiOutputDirError.value = '保存失败，请检查路径是否可写';
    }
  } finally {
    saving.value = false;
  }
}

// 选目录：桌面走原生文件夹对话框，触屏/原生不可用时回退内置目录浏览器
// （store.pickFolderPlain 统一入口）；选到路径后回填草稿并直接保存，
// 用户取消时回调不触发，草稿与已保存值都保持原样
async function pickAiOutputDir() {
  if (saving.value) return;
  await store.pickFolderPlain((path: string) => {
    aiOutputDirDraft.value = path;
    void saveAiOutputDir();
  });
}

async function selectShell(shellId: string) {
  if (shellId === store.systemSettings.command_shell || saving.value) return;
  saving.value = true;
  try {
    await store.setCommandShell(shellId);
  } finally {
    saving.value = false;
  }
}

async function selectTokenTtl(value: string) {
  const seconds = Number(value);
  if (seconds === store.systemSettings.remote_token_ttl || saving.value) return;
  saving.value = true;
  try {
    await store.setRemoteTokenTtl(seconds);
  } finally {
    saving.value = false;
  }
}

// ── 电脑操控区（Task 12）：开关状态一律以后端快照渲染，失败自动回滚 ──

// 活动 run：浏览器选择在活动会话中不可修改（设计 8.1）
const activeComputerRun = computed(() => {
  const run = store.systemSettings.computer_run;
  if (!run || !run.run_id) return false;
  return !['idle', 'completed', 'stopped', 'failed'].includes(run.status);
});

// 平台/浏览器/热键 preflight 结果（mini-list 摘要，不塞整表）
const preflightItems = computed(() => {
  const items = [
    {
      label: store.systemSettings.computer_platform_supported
        ? '平台：当前 Windows 平台受支持'
        : '平台：当前平台不受支持（电脑操控仅支持 Windows）',
      ok: store.systemSettings.computer_platform_supported,
    },
  ];
  for (const option of store.systemSettings.computer_browser_options) {
    items.push({
      label: option.available
        ? `浏览器：${option.label} 可用`
        : `浏览器：${option.label} 不可用（${option.unavailable_reason || '未安装'}）`,
      ok: option.available,
    });
  }
  items.push({
    label: store.systemSettings.computer_hotkey_available
      ? `热键：紧急停止热键 ${store.systemSettings.computer_hotkey} 可注册`
      : `热键：无法注册紧急停止热键 ${store.systemSettings.computer_hotkey}，开关无法开启`,
    ok: store.systemSettings.computer_hotkey_available,
  });
  return items;
});

async function toggleBrowserControl() {
  if (saving.value) return;
  saving.value = true;
  try {
    await store.setBrowserControlEnabled(!store.systemSettings.browser_control_enabled);
  } finally {
    saving.value = false;
  }
}

async function toggleComputerControl() {
  if (saving.value) return;
  saving.value = true;
  try {
    await store.setComputerControlEnabled(!store.systemSettings.computer_control_enabled);
  } finally {
    saving.value = false;
  }
}

async function selectComputerBrowser(browser: string) {
  if (browser === store.systemSettings.computer_browser || saving.value) return;
  saving.value = true;
  try {
    await store.setComputerBrowser(browser);
  } finally {
    saving.value = false;
  }
}

// ── /doctor 聚合自检（T3b）：与 CLI /doctor 同源（core.doctor.run_checks）。
// 结果为一次性快照（含 detail/hint 建议），只在本组件内维护，不进全局 Store ──

const DOCTOR_STATUS_LABELS: Record<DoctorCheckStatus, string> = {
  ok: '通过',
  warn: '警告',
  fail: '失败',
  skip: '跳过',
};
const DOCTOR_STATUS_ICONS: Record<DoctorCheckStatus, string> = {
  ok: 'check',
  warn: 'triangle-alert',
  fail: 'x',
  skip: 'circle',
};

const doctorRunning = ref(false);
const doctorError = ref('');
const doctorChecks = ref<DoctorCheck[]>([]);

const doctorSummaryText = computed(() => {
  const count = (status: DoctorCheckStatus) =>
    doctorChecks.value.filter(check => check.status === status).length;
  return `共 ${doctorChecks.value.length} 项：通过 ${count('ok')}、警告 ${count('warn')}、失败 ${count('fail')}、跳过 ${count('skip')}`;
});

function doctorStatusLabel(status: string): string {
  return DOCTOR_STATUS_LABELS[status as DoctorCheckStatus] || status;
}

function doctorStatusIcon(status: string): string {
  return DOCTOR_STATUS_ICONS[status as DoctorCheckStatus] || 'info';
}

async function runDoctor() {
  if (doctorRunning.value) return;
  // 与 SettingsPage 的更新检查一致：直接读 window.pywebview，不弹全局"连接失败"模态框
  const api = window.pywebview?.api;
  if (!api) {
    doctorError.value = '连接失败：请在桌面客户端中运行自检';
    return;
  }
  doctorRunning.value = true;
  doctorError.value = '';
  try {
    const report = await api.run_doctor();
    if (report && report.ok && Array.isArray(report.checks)) {
      doctorChecks.value = report.checks;
    } else {
      // 后端防御性兜底（ok=false）：保留上一次结果，仅提示错误
      doctorError.value = `自检失败：${(report && report.msg) || '未知错误'}`;
    }
  } catch (err) {
    doctorError.value = `自检失败：${err}`;
  } finally {
    doctorRunning.value = false;
  }
}
</script>

<style scoped>
.system-settings { max-width: 920px; margin: 0 auto; }
.system-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.system-header h1 { margin: 0 0 7px; font-size: 25px; }
.system-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.refresh-button { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-dim); }
.refresh-button:hover { color: var(--text); background: var(--bg3); }
.settings-list { border-top: 1px solid var(--border); }
.setting-row { display: flex; align-items: center; justify-content: space-between; gap: 24px; min-height: 90px; border-bottom: 1px solid var(--border); }
.setting-copy { min-width: 0; }
.setting-copy h2 { margin: 0 0 6px; font-size: 15px; }
.setting-copy p { margin: 0; color: var(--text-faint); font-size: 12.5px; line-height: 1.6; }
.setting-select { flex: 0 0 240px; max-width: 45%; height: 38px; padding: 0 34px 0 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); color: var(--text); font: inherit; }
.setting-select:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.setting-select:disabled { cursor: wait; opacity: .7; }
.setting-number-group { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.setting-number { width: 120px; height: 38px; padding: 0 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); color: var(--text); font: inherit; }
.setting-number:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.setting-number:disabled { cursor: wait; opacity: .7; }
/* AI 生成内容目录是长路径：仅加宽该输入框，其余数值型设置项维持原宽度；
   右侧多了「选择目录」按钮，宽度略收避免挤压左侧说明文案 */
.setting-number[data-test="ai-output-dir-input"] { width: 260px; }
.setting-save { height: 38px; padding: 0 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); color: var(--text); font: inherit; }
.setting-save:hover:not(:disabled) { background: var(--bg3); }
.setting-save:disabled { cursor: not-allowed; opacity: .6; }
.setting-error { margin: 12px 0 0; color: var(--danger); font-size: 12px; }
.setting-note { margin: 14px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }

/* ── 电脑操控区（Task 12）── */
.computer-settings { margin-top: 34px; }
.computer-header { margin-bottom: 6px; }
.computer-title { display: flex; align-items: center; gap: 7px; margin: 0 0 7px; font-size: 16px; color: var(--text-bright); }
.computer-header p { margin: 0; color: var(--text-faint); font-size: 12.5px; line-height: 1.6; }
.setting-switch { position: relative; flex: 0 0 auto; width: 44px; height: 24px; border: 1px solid var(--border); border-radius: 999px; background: var(--bg3); cursor: pointer; transition: background .15s ease, border-color .15s ease; }
.setting-switch[aria-checked='true'] { background: var(--accent); border-color: var(--accent); }
.setting-switch .switch-knob { position: absolute; top: 2px; left: 2px; width: 18px; height: 18px; border-radius: 999px; background: var(--text-bright); transition: transform .15s ease; }
.setting-switch[aria-checked='true'] .switch-knob { transform: translateX(20px); }
.setting-switch:disabled { cursor: wait; opacity: .7; }
.computer-mobile-note { margin: 10px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.6; }
/* 专用配置目录是长路径：右侧只读展示，不占整行 */
.setting-path { flex: 0 0 auto; max-width: 50%; color: var(--text-dim); font-size: 12px; text-align: right; word-break: break-all; }

/* ── Playwright 按需组件卡片（安装包不再内置 Playwright）── */
.playwright-component-card { align-items: flex-start; padding: 14px 0; }
.playwright-component-state { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.playwright-component-missing { color: var(--text-faint); font-size: 12px; }
.playwright-component-ready { color: var(--green, #22c55e); font-size: 12px; }
.playwright-component-progress {
  flex: 1 1 auto;
  min-width: 120px;
  max-width: 320px;
  height: 6px;
  border-radius: 999px;
  background: var(--bg3);
  overflow: hidden;
}
.playwright-component-progress-bar { height: 100%; background: var(--accent); transition: width .2s ease; }
.playwright-component-size { flex: 0 0 auto; color: var(--text-faint); font-size: 12px; white-space: nowrap; }
.playwright-component-actions { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; }

/* ── 网页搜索按需组件卡片（安装包不再内置 ddgs）：样式与 Playwright 卡片同构，
      重复写一份而不是复用它，是为了两处互不牵连地各自演进 ── */
/* 与「电脑操控」区块保持同样的段落间距（新卡片单独成一个 section） */
.search-component-settings { margin-top: 34px; }
.search-component-card { align-items: flex-start; padding: 14px 0; }
.search-component-state { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.search-component-missing { color: var(--text-faint); font-size: 12px; }
.search-component-ready { color: var(--green, #22c55e); font-size: 12px; }
.search-component-progress {
  flex: 1 1 auto;
  min-width: 120px;
  max-width: 320px;
  height: 6px;
  border-radius: 999px;
  background: var(--bg3);
  overflow: hidden;
}
.search-component-progress-bar { height: 100%; background: var(--accent); transition: width .2s ease; }
.search-component-size { flex: 0 0 auto; color: var(--text-faint); font-size: 12px; white-space: nowrap; }
.search-component-actions { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; }

/* 环境检测/安全提示等说明性内容默认收起，保持主视图一行一项 */
.setting-details { margin-top: 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.setting-details > summary { display: flex; align-items: center; gap: 6px; padding: 11px 14px; color: var(--text-dim); font-size: 12.5px; cursor: pointer; list-style: none; }
.setting-details > summary::-webkit-details-marker { display: none; }
.setting-details > summary::before { content: '▸'; color: var(--text-faint); font-size: 10px; }
.setting-details[open] > summary::before { content: '▾'; }
.setting-details > summary:hover { color: var(--text); }
.setting-details-body { padding: 0 14px 12px; }
/* 提示本身就在折叠容器里，去掉重复的边框与底色 */
.setting-details-body .computer-privacy { margin-top: 0; padding: 0; border: 0; background: transparent; }
.setting-details-note { margin: 10px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.6; }
.computer-model-hint {
  margin: 12px 0 0;
  font-size: 12px;
  color: var(--text-secondary, #666);
}

.computer-model-hint.blocked {
  color: var(--danger, #c62828);
}

.computer-privacy { display: flex; align-items: flex-start; gap: 8px; margin: 14px 0 0; padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent); border-radius: var(--theme-radius-medium); background: color-mix(in srgb, var(--warning) 8%, transparent); color: var(--text-dim); font-size: 12px; line-height: 1.7; }
.computer-privacy svg { flex-shrink: 0; margin-top: 3px; color: var(--warning); }
.computer-preflight { margin: 14px 0 0; padding: 0; list-style: none; }
.computer-preflight li { display: flex; align-items: center; gap: 7px; margin: 0 0 6px; color: var(--text-faint); font-size: 12px; }
.computer-preflight li.ok { color: var(--text-dim); }
.computer-preflight li:not(.ok) svg { color: var(--danger); }
.computer-preflight li.ok svg { color: var(--green); }

/* ── 运行自检（T3b）── */
.doctor-settings { margin-top: 34px; }
.doctor-header { margin-bottom: 6px; }
.doctor-title { display: flex; align-items: center; gap: 7px; margin: 0 0 7px; font-size: 16px; color: var(--text-bright); }
.doctor-header p { margin: 0; color: var(--text-faint); font-size: 12.5px; line-height: 1.6; }
.doctor-results { margin-top: 16px; }
.doctor-summary { margin: 0 0 10px; color: var(--text-dim); font-size: 12.5px; }
.doctor-list { margin: 0; padding: 0; list-style: none; }
.doctor-item { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 8px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.doctor-status-icon { margin-top: 2px; color: var(--text-faint); }
.doctor-item--ok .doctor-status-icon { color: var(--green); }
.doctor-item--warn .doctor-status-icon { color: var(--warning); }
.doctor-item--fail .doctor-status-icon { color: var(--danger); }
.doctor-item-body { min-width: 0; }
.doctor-item-head { display: flex; align-items: baseline; gap: 8px; }
.doctor-name { font-size: 13px; color: var(--text-bright); }
.doctor-status-text { font-size: 11.5px; color: var(--text-faint); }
.doctor-detail { margin: 4px 0 0; color: var(--text-dim); font-size: 12.5px; line-height: 1.6; word-break: break-word; }
.doctor-hint { margin: 3px 0 0; color: var(--warning); font-size: 12px; line-height: 1.6; word-break: break-word; }

@media (max-width: 620px) {
  .setting-row { align-items: stretch; flex-direction: column; gap: 12px; padding: 16px 0; }
  .setting-select { flex-basis: auto; max-width: none; width: 100%; }
  /* 窄屏下目录输入框与两个按钮换行铺满，不横向溢出 */
  .setting-number-group { flex-wrap: wrap; width: 100%; }
  .setting-number[data-test="ai-output-dir-input"] { flex: 1 1 160px; width: auto; min-width: 0; }
}
</style>
