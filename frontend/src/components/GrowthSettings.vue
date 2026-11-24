<template>
  <div class="growth-settings" data-test="growth-settings-page">
    <header class="page-header">
      <div>
        <h1>记忆与成长</h1>
        <p>管理自我演化、经验学习与成长档案。</p>
      </div>
      <div class="header-actions">
        <button
          class="refresh-button"
          type="button"
          title="重新加载"
          data-test="growth-refresh"
          :disabled="loading"
          @click="loadGrowth()"
        >
          <AppIcon name="refresh" :size="14" :class="{ 'icon-spin': loading }" />
        </button>
      </div>
    </header>

    <!-- 自我演化与学习控制卡片 -->
    <section class="setting-card">
      <div class="setting-row">
        <div class="setting-copy">
          <div class="title-line">
            <h3>启用自我演化</h3>
            <span class="state-pill" :class="{ on: enabledDraft }">
              {{ enabledDraft ? '已启用' : '已关闭' }}
            </span>
          </div>
          <p>随长期对话积累形成稳定的自我模型。</p>
        </div>
        <button
          type="button"
          role="switch"
          class="setting-switch"
          data-test="growth-enabled-toggle"
          aria-label="启用自我演化"
          :aria-checked="enabledDraft ? 'true' : 'false'"
          :disabled="loading || saving"
          @click="toggleSetting('enabled', !enabledDraft)"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <div class="setting-row">
        <div class="setting-copy">
          <div class="title-line">
            <h3>启用经验学习</h3>
            <span class="state-pill" :class="{ on: learningDraft }">
              {{ learningDraft ? '已启用' : '已关闭' }}
            </span>
          </div>
          <p>从历史任务提炼可复用经验并用于后续任务。</p>
        </div>
        <button
          type="button"
          role="switch"
          class="setting-switch"
          data-test="growth-learning-toggle"
          aria-label="启用经验学习"
          :aria-checked="learningDraft ? 'true' : 'false'"
          :disabled="loading || saving"
          @click="toggleSetting('learning_enabled', !learningDraft)"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <div class="setting-row setting-row--top">
        <div class="setting-copy">
          <h3>用户标识</h3>
          <p>不同标识的成长数据相互独立，留空按系统用户名自动识别。</p>
        </div>
        <div class="setting-control--wide">
          <div class="growth-user-key-row">
            <input
              id="growth-user-key"
              v-model="userKeyDraft"
              data-test="growth-user-key"
              class="text-field mono"
              type="text"
              :maxlength="64"
              placeholder="留空表示按系统用户名自动识别"
              :disabled="loading || saving"
              @keydown.enter.prevent="saveUserKey"
            />
            <button
              data-test="growth-save-user-key"
              class="primary-button compact"
              type="button"
              :disabled="loading || saving"
              @click="saveUserKey"
            >保存</button>
          </div>
        </div>
      </div>

      <div v-if="errorMsg || tipMsg || !apiAvailable" class="card-footer">
        <p v-if="errorMsg" data-test="growth-error" class="growth-error">{{ errorMsg }}</p>
        <p v-else-if="tipMsg" data-test="growth-tip" class="growth-tip">{{ tipMsg }}</p>
        <p v-else-if="!apiAvailable" data-test="growth-api-unavailable" class="growth-hint">
          GUI 接口不可用：当前环境未注入 pywebview，无法读取或修改成长数据。
        </p>
      </div>
    </section>

    <!-- 当前状态概览 -->
    <section class="setting-group">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="chart" :size="14" />
          <span>当前状态</span>
        </div>
        <span v-if="loading" class="group-meta">加载中…</span>
      </div>
      <div class="setting-card status-card">
        <div class="growth-status-grid">
          <div class="growth-status-item">
            <span class="growth-status-label">当前用户</span>
            <span class="growth-status-value mono" data-test="growth-current-user">{{ snapshot.user_key || '—' }}</span>
          </div>
          <div class="growth-status-item">
            <span class="growth-status-label">称呼</span>
            <span class="growth-status-value" data-test="growth-current-title">{{ snapshot.title || '—' }}</span>
          </div>
          <div class="growth-status-item accent">
            <span class="growth-status-label">成长阶段</span>
            <span class="growth-status-value" data-test="growth-current-stage">{{ stageLabel(snapshot.stage) }}</span>
          </div>
          <div class="growth-status-item">
            <span class="growth-status-label">经验条数</span>
            <span class="growth-status-value mono" data-test="growth-lessons-count">{{ Number(snapshot.lessons_count || 0) }}</span>
          </div>
          <div class="growth-status-item">
            <span class="growth-status-label">上次学习</span>
            <span class="growth-status-value mono small-time" data-test="growth-last-learning">{{ formatTime(snapshot.last_learning_at) }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 变化日志 -->
    <section class="setting-group">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="history" :size="14" />
          <span>变化日志</span>
        </div>
        <span class="group-meta">最近 50 条，最新在上</span>
      </div>
      <div class="setting-card changelog-card">
        <div v-if="changelogRows.length" class="growth-changelog" data-test="growth-changelog">
          <div v-for="(row, index) in changelogRows" :key="index" class="growth-changelog-row">{{ row }}</div>
        </div>
        <p v-else class="growth-empty" data-test="growth-changelog-empty">暂无变化日志</p>
      </div>
    </section>

    <!-- 维护操作 -->
    <section class="setting-group">
      <div class="group-title">
        <AppIcon name="shield" :size="14" />
        <span>维护操作</span>
      </div>
      <div class="setting-card">
        <div class="setting-row maintenance-row">
          <div class="setting-copy">
            <h3>档案备份与重置</h3>
            <p>重置前会自动备份旧数据；导出快照仅在本地生成 JSON 文件。</p>
          </div>
          <div class="growth-actions">
            <button
              data-test="growth-reset-self"
              class="secondary-button danger-soft"
              type="button"
              :disabled="loading || saving"
              @click="resetScope('self')"
            >重置自我模型</button>
            <button
              data-test="growth-reset-user"
              class="secondary-button danger-soft"
              type="button"
              :disabled="loading || saving"
              @click="resetScope('user')"
            >清空当前用户档案</button>
            <button
              data-test="growth-export"
              class="primary-button"
              type="button"
              :disabled="loading || saving"
              @click="exportSnapshot"
            >
              <AppIcon name="download" :size="13" />
              <span>导出快照</span>
            </button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';

import AppIcon from './AppIcon.vue';

const loading = ref(false);
const saving = ref(false);
const errorMsg = ref('');
const tipMsg = ref('');
const apiAvailable = ref(true);

const enabledDraft = ref(false);
const learningDraft = ref(false);
const userKeyDraft = ref('');

const snapshot = ref<Record<string, any>>({});
let tipTimer: ReturnType<typeof setTimeout> | null = null;

/** 最近 50 条变化日志：后端按写入顺序返回（旧→新），展示最新在上 */
const changelogRows = computed<string[]>(() => {
  const rows = Array.isArray(snapshot.value.changelog) ? snapshot.value.changelog : [];
  return rows
    .filter((row: any) => typeof row === 'string' && row.trim().length > 0)
    .slice(-50)
    .reverse();
});

const STAGE_LABELS: Record<string, string> = {
  awakening: '觉醒',
  growing: '成长',
  settled: '稳定',
};

function stageLabel(stage: unknown): string {
  const key = String(stage || '').trim();
  if (!key) return '—';
  return STAGE_LABELS[key] || key;
}

function formatTime(value: unknown): string {
  const text = String(value || '').trim();
  if (!text) return '—';
  return text.replace('T', ' ').replace(/\.\d+Z?$/, '').replace(/Z$/, '');
}

function showTip(message: string) {
  tipMsg.value = message;
  if (tipTimer) clearTimeout(tipTimer);
  tipTimer = setTimeout(() => { tipMsg.value = ''; }, 2500);
}

function applySettings(settings: any) {
  enabledDraft.value = Boolean(settings?.enabled);
  learningDraft.value = Boolean(settings?.learning_enabled);
  userKeyDraft.value = String(settings?.user_key ?? '');
}

async function loadGrowth(options: { silent?: boolean } = {}) {
  if (!options.silent) loading.value = true;
  errorMsg.value = '';
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.get_growth_settings) {
      // 后端未就绪（如纯浏览器预览）：降级为提示文案，不抛错、不弹窗
      apiAvailable.value = false;
      return;
    }
    apiAvailable.value = true;
    const res = await api.get_growth_settings();
    if (res?.ok) {
      applySettings(res.settings);
      snapshot.value = res.snapshot || {};
    } else {
      errorMsg.value = res?.msg || '加载成长设置失败';
    }
  } catch {
    errorMsg.value = '加载成长设置失败';
  } finally {
    if (!options.silent) loading.value = false;
  }
}

async function saveGrowthPatch(patch: {
  enabled?: boolean;
  learning_enabled?: boolean;
  user_key?: string;
}) {
  const api = (window as any).pywebview?.api;
  if (!api?.set_growth_settings) {
    errorMsg.value = 'GUI 接口不可用，无法保存成长设置';
    return null;
  }
  return api.set_growth_settings(
    patch.enabled ?? null,
    patch.learning_enabled ?? null,
    patch.user_key ?? null,
  );
}

async function toggleSetting(key: 'enabled' | 'learning_enabled', value: boolean) {
  if (saving.value) return;
  saving.value = true;
  errorMsg.value = '';
  if (key === 'enabled') enabledDraft.value = value;
  else learningDraft.value = value;
  try {
    const res: any = await saveGrowthPatch({ [key]: Boolean(value) } as any);
    if (res?.ok) {
      applySettings(res.settings);
      showTip('设置已保存');
      await loadGrowth({ silent: true });
    } else {
      // 失败时回滚开关，避免界面与实际配置不一致
      if (key === 'enabled') enabledDraft.value = !value;
      else learningDraft.value = !value;
      errorMsg.value = res?.msg || '保存成长设置失败';
    }
  } catch {
    if (key === 'enabled') enabledDraft.value = !value;
    else learningDraft.value = !value;
    errorMsg.value = '保存成长设置失败';
  } finally {
    saving.value = false;
  }
}

async function saveUserKey() {
  if (saving.value) return;
  saving.value = true;
  errorMsg.value = '';
  try {
    const res: any = await saveGrowthPatch({ user_key: userKeyDraft.value.trim() });
    if (res?.ok) {
      applySettings(res.settings);
      showTip(userKeyDraft.value.trim() ? '用户标识已保存' : '已恢复按系统用户名自动识别');
      await loadGrowth({ silent: true });
    } else {
      errorMsg.value = res?.msg || '保存用户标识失败';
    }
  } catch {
    errorMsg.value = '保存用户标识失败';
  } finally {
    saving.value = false;
  }
}

const RESET_CONFIRM: Record<'self' | 'user', string> = {
  self: '确定要重置自我模型吗？特质、语气与叙事会回退到初始状态（旧数据改名为备份目录保留）。',
  user: '确定要清空当前用户档案吗？对该用户的称呼、偏好与记忆会回退到初始状态（旧数据改名为备份目录保留）。',
};

async function resetScope(scope: 'self' | 'user') {
  if (saving.value) return;
  // 重置是不可逆的破坏性操作，必须先二次确认
  if (!window.confirm(RESET_CONFIRM[scope])) return;
  saving.value = true;
  errorMsg.value = '';
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.reset_growth) {
      errorMsg.value = 'GUI 接口不可用，无法重置成长数据';
      return;
    }
    const res = await api.reset_growth(scope);
    if (res?.ok) {
      showTip(scope === 'self' ? '自我模型已重置' : '当前用户档案已清空');
      await loadGrowth({ silent: true });
    } else {
      errorMsg.value = res?.msg || '重置成长数据失败';
    }
  } catch {
    errorMsg.value = '重置成长数据失败';
  } finally {
    saving.value = false;
  }
}

async function exportSnapshot() {
  if (saving.value) return;
  saving.value = true;
  errorMsg.value = '';
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.export_growth_snapshot) {
      errorMsg.value = 'GUI 接口不可用，无法导出快照';
      return;
    }
    const res = await api.export_growth_snapshot();
    if (!res?.ok) {
      errorMsg.value = res?.msg || '导出快照失败';
      return;
    }
    const text = JSON.stringify(res.snapshot ?? {}, null, 2);
    const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'growth-snapshot.json';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    // 交给浏览器读完后立即回收，避免内存泄漏
    setTimeout(() => URL.revokeObjectURL(url), 0);
    showTip('快照已导出');
  } catch {
    errorMsg.value = '导出快照失败';
  } finally {
    saving.value = false;
  }
}

onMounted(() => { void loadGrowth(); });
onUnmounted(() => {
  if (tipTimer) clearTimeout(tipTimer);
});
</script>

<style scoped>
.growth-settings {
  max-width: 880px;
  margin: 0 auto;
  padding-bottom: 24px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 22px;
}

.page-header h1 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}

.page-header p {
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

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 16px 20px;
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

.title-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 3px;
}

.setting-copy h3 {
  margin: 0 0 3px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.title-line h3 {
  margin: 0;
}

.setting-copy p {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
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

/* 统一滑动开关按钮 */
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

.setting-control--wide {
  flex: 0 1 430px;
  width: 430px;
  max-width: 100%;
}

.growth-user-key-row {
  display: flex;
  gap: 8px;
  align-items: center;
}

.text-field {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  min-height: 36px;
  padding: 0 12px;
  transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
}

.text-field:focus {
  outline: none;
  border-color: var(--accent);
  background: var(--bg1);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent);
}

.text-field:disabled {
  cursor: wait;
  opacity: .7;
}

.mono {
  font-family: var(--mono);
}

.card-footer {
  padding: 11px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
}

.growth-hint {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}

.growth-error {
  margin: 0;
  color: var(--red);
  font-size: 12.5px;
}

.growth-tip {
  margin: 0;
  color: var(--green);
  font-size: 12.5px;
}

/* 当前状态 5 格均衡布局 */
.status-card {
  padding: 16px 20px;
}

.growth-status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
}

.growth-status-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 11px 13px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg2);
  min-width: 0;
}

.growth-status-item.accent {
  border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
  background: color-mix(in srgb, var(--accent) 5%, var(--bg2));
}

.growth-status-label {
  color: var(--text-faint);
  font-size: 11px;
}

.growth-status-value {
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.growth-status-value.small-time {
  font-size: 12px;
  font-weight: 500;
}

/* 变化日志 */
.changelog-card {
  padding: 14px 16px;
}

.growth-changelog {
  max-height: 300px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  padding: 6px 12px;
}

.growth-changelog-row {
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.75;
  color: var(--text-dim);
  white-space: pre-wrap;
  word-break: break-word;
  padding: 5px 0;
  border-bottom: 1px dashed var(--border);
}

.growth-changelog-row:last-child {
  border-bottom: 0;
}

.growth-empty {
  margin: 0;
  padding: 6px 4px;
  color: var(--text-faint);
  font-size: 12.5px;
}

/* 维护操作 */
.maintenance-row {
  flex-wrap: wrap;
}

.growth-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  flex: none;
}

.primary-button,
.secondary-button {
  min-height: 34px;
  padding: 0 15px;
  border-radius: 8px;
  border: 1px solid var(--border);
  font-weight: 600;
  cursor: pointer;
  flex: none;
  font-size: 12.5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: opacity .15s ease, background .15s ease, border-color .15s ease, color .15s ease;
}

.primary-button {
  background: var(--text);
  color: var(--bg0);
  border-color: transparent;
}

.primary-button.compact {
  min-height: 36px;
  padding: 0 14px;
}

.primary-button:hover:not(:disabled) {
  opacity: .88;
}

.primary-button:disabled,
.secondary-button:disabled {
  opacity: .48;
  cursor: not-allowed;
}

.secondary-button {
  background: var(--bg2);
  color: var(--text);
}

.secondary-button:hover:not(:disabled) {
  background: var(--bg3);
}

.secondary-button.danger-soft:hover:not(:disabled) {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 40%, var(--border));
  background: color-mix(in srgb, var(--red) 7%, var(--bg1));
}

@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
  }
  .setting-row {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 14px 16px;
  }
  .setting-control--wide {
    width: 100%;
  }
  .growth-user-key-row {
    flex-direction: column;
    align-items: stretch;
  }
  .growth-actions {
    width: 100%;
  }
}
</style>
