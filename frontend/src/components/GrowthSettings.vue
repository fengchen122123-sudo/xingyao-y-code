<template>
  <div class="growth-settings" data-test="growth-settings-page">
    <header class="growth-header">
      <div>
        <h1>记忆与成长</h1>
        <p>自我演化与经验学习开关、当前成长状态、变化日志与快照导出。</p>
      </div>
      <button class="refresh-button" type="button" title="重新加载" data-test="growth-refresh" @click="loadGrowth()">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="growth-card">
      <div class="growth-switch-row">
        <div class="growth-switch-text">
          <h3>启用自我演化</h3>
          <p>根据长期相处积累的特质、语气与叙事，形成稳定的自我模型。</p>
        </div>
        <label class="growth-switch">
          <input
            v-model="enabledDraft"
            data-test="growth-enabled-toggle"
            type="checkbox"
            :disabled="loading || saving"
            @change="toggleSetting('enabled', enabledDraft)"
          />
          <span>{{ enabledDraft ? '已启用' : '已关闭' }}</span>
        </label>
      </div>

      <div class="growth-switch-row">
        <div class="growth-switch-text">
          <h3>启用经验学习</h3>
          <p>从任务中提炼可复用的经验条目，命中后在后续同类任务里复用。</p>
        </div>
        <label class="growth-switch">
          <input
            v-model="learningDraft"
            data-test="growth-learning-toggle"
            type="checkbox"
            :disabled="loading || saving"
            @change="toggleSetting('learning_enabled', learningDraft)"
          />
          <span>{{ learningDraft ? '已启用' : '已关闭' }}</span>
        </label>
      </div>

      <label class="field-label" for="growth-user-key">用户标识</label>
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
      <p class="growth-hint">同一用户标识下的成长数据互相独立；留空时按当前系统用户名自动解析。</p>

      <p v-if="errorMsg" data-test="growth-error" class="growth-error">{{ errorMsg }}</p>
      <p v-else-if="tipMsg" data-test="growth-tip" class="growth-tip">{{ tipMsg }}</p>
      <p v-else-if="!apiAvailable" data-test="growth-api-unavailable" class="growth-hint">GUI 接口不可用：当前环境未注入 pywebview，无法读取或修改成长数据。</p>
    </section>

    <section class="growth-card">
      <div class="growth-card-head">
        <h3>当前状态</h3>
        <span v-if="loading" class="growth-loading">加载中…</span>
      </div>
      <div class="growth-status-grid">
        <div class="growth-status-item">
          <span class="growth-status-label">当前用户</span>
          <span class="growth-status-value mono" data-test="growth-current-user">{{ snapshot.user_key || '—' }}</span>
        </div>
        <div class="growth-status-item">
          <span class="growth-status-label">称呼</span>
          <span class="growth-status-value" data-test="growth-current-title">{{ snapshot.title || '—' }}</span>
        </div>
        <div class="growth-status-item">
          <span class="growth-status-label">成长阶段</span>
          <span class="growth-status-value" data-test="growth-current-stage">{{ stageLabel(snapshot.stage) }}</span>
        </div>
        <div class="growth-status-item">
          <span class="growth-status-label">经验条数</span>
          <span class="growth-status-value mono" data-test="growth-lessons-count">{{ Number(snapshot.lessons_count || 0) }}</span>
        </div>
        <div class="growth-status-item">
          <span class="growth-status-label">上次学习</span>
          <span class="growth-status-value mono" data-test="growth-last-learning">{{ formatTime(snapshot.last_learning_at) }}</span>
        </div>
      </div>
    </section>

    <section class="growth-card">
      <div class="growth-card-head">
        <h3>变化日志</h3>
        <span class="growth-hint">最近 50 条，最新在上</span>
      </div>
      <div v-if="changelogRows.length" class="growth-changelog" data-test="growth-changelog">
        <div v-for="(row, index) in changelogRows" :key="index" class="growth-changelog-row">{{ row }}</div>
      </div>
      <p v-else class="growth-empty" data-test="growth-changelog-empty">暂无变化日志</p>
    </section>

    <section class="growth-card">
      <div class="growth-card-head">
        <h3>维护操作</h3>
      </div>
      <div class="growth-actions">
        <button
          data-test="growth-reset-self"
          class="secondary-button"
          type="button"
          :disabled="loading || saving"
          @click="resetScope('self')"
        >重置自我模型</button>
        <button
          data-test="growth-reset-user"
          class="secondary-button"
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
        >导出快照</button>
      </div>
      <p class="growth-hint">重置会把旧数据改名为备份目录（不会立即删除）；导出内容仅本地下发，不上传。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

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
  // 后端存的是 ISO 风格时间串：展示到秒，去掉 T 与毫秒
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

/** 只提交本次涉及的字段，其余传 null 表示「不修改」 */
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
  try {
    const res: any = await saveGrowthPatch({ [key]: Boolean(value) } as any);
    if (res?.ok) {
      applySettings(res.settings);
      showTip('设置已保存');
      // 开关变化会影响自我模型是否写入，刷新一次快照保持一致
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
</script>

<style scoped>
.growth-settings { max-width: 920px; margin: 0 auto; }
.growth-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.growth-header h1 { margin: 0 0 7px; font-size: 25px; }
.growth-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.refresh-button { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-dim); flex: none; }
.refresh-button:hover { color: var(--text); background: var(--bg3); }
.growth-card { border-top: 1px solid var(--border); padding: 4px 0 14px; }
.growth-card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 16px 0 10px; }
.growth-card-head h3 { margin: 0; font-size: 15px; }
.growth-switch-row { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 16px 0; border-bottom: 1px solid var(--border); }
.growth-switch-row:last-of-type { border-bottom: none; }
.growth-switch-text h3 { margin: 0 0 5px; font-size: 14px; }
.growth-switch-text p { margin: 0; color: var(--text-faint); font-size: 12px; line-height: 1.6; }
.growth-switch { display: inline-flex; align-items: center; gap: 8px; color: var(--text-dim); font-size: 12px; flex: none; cursor: pointer; }
.growth-switch input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
.field-label { display: block; margin: 18px 0 7px; color: var(--text-dim); font-size: 12px; }
.growth-user-key-row { display: flex; gap: 10px; align-items: center; }
.text-field { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; min-height: 42px; padding: 0 13px; }
.text-field:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.text-field:disabled { cursor: wait; opacity: .7; }
.primary-button, .secondary-button { min-height: 38px; padding: 0 18px; border-radius: 9px; border: 1px solid var(--border); font-weight: 600; cursor: pointer; flex: none; }
.primary-button { background: var(--text); color: var(--bg0); }
.primary-button.compact { min-height: 38px; padding: 0 16px; }
.primary-button:disabled, .secondary-button:disabled { opacity: .5; cursor: not-allowed; }
.secondary-button { background: var(--bg3); color: var(--text); }
.growth-hint { margin: 12px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }
.growth-error { margin: 12px 0 0; color: var(--danger); font-size: 12px; }
.growth-tip { margin: 12px 0 0; color: var(--green); font-size: 12px; }
.growth-loading { color: var(--text-faint); font-size: 12px; }
.growth-status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
.growth-status-item { display: flex; flex-direction: column; gap: 6px; padding: 12px 13px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.growth-status-label { color: var(--text-faint); font-size: 11px; }
.growth-status-value { color: var(--text); font-size: 13px; }
.growth-changelog { max-height: 320px; overflow: auto; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); padding: 10px 13px; }
.growth-changelog-row { font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace); font-size: 11px; line-height: 1.85; color: var(--text-dim); white-space: pre-wrap; word-break: break-word; }
.growth-empty { margin: 12px 0 0; color: var(--text-faint); font-size: 12px; }
.growth-actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }

@media (max-width: 768px) {
  .growth-header { flex-direction: column; }
  .growth-switch-row { flex-direction: column; align-items: flex-start; gap: 10px; }
  .growth-user-key-row { flex-direction: column; align-items: stretch; }
  .growth-actions { flex-direction: column; align-items: stretch; }
}
</style>