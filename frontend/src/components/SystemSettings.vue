<template>
  <div class="system-settings" data-test="system-settings-page">
    <header class="system-header">
      <div>
        <h1>系统设置</h1>
        <p>管理 Y-CODE 的系统级运行行为；后续系统选项将在此集中提供。</p>
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
      <p v-if="store.systemSettings.command_shell_error" class="setting-error">
        {{ store.systemSettings.command_shell_error }}
      </p>
      <p class="setting-note">
        命令失败时错误会原样返回给大模型，由模型调整语法或通过命令工具显式指定其他已安装解释器；系统不会静默降级。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';

import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const store = useAppStore();
const saving = ref(false);

const turnStepLimitDraft = ref(String(store.systemSettings.agent_turn_step_limit));

watch(
  () => store.systemSettings.agent_turn_step_limit,
  value => { turnStepLimitDraft.value = String(value); },
);

const parsedTurnStepLimit = computed(() => Number(turnStepLimitDraft.value));
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
</script>

<style scoped>
.system-settings { max-width: 920px; }
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
.setting-save { height: 38px; padding: 0 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); color: var(--text); font: inherit; }
.setting-save:hover:not(:disabled) { background: var(--bg3); }
.setting-save:disabled { cursor: not-allowed; opacity: .6; }
.setting-error { margin: 12px 0 0; color: var(--danger); font-size: 12px; }
.setting-note { margin: 14px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }
@media (max-width: 620px) {
  .setting-row { align-items: stretch; flex-direction: column; gap: 12px; padding: 16px 0; }
  .setting-select { flex-basis: auto; max-width: none; width: 100%; }
}
</style>
