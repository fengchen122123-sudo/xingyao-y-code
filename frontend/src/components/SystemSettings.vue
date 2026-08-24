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
import { ref } from 'vue';

import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const store = useAppStore();
const saving = ref(false);

async function selectShell(shellId: string) {
  if (shellId === store.systemSettings.command_shell || saving.value) return;
  saving.value = true;
  try {
    await store.setCommandShell(shellId);
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
.setting-error { margin: 12px 0 0; color: var(--danger); font-size: 12px; }
.setting-note { margin: 14px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }
@media (max-width: 620px) {
  .setting-row { align-items: stretch; flex-direction: column; gap: 12px; padding: 16px 0; }
  .setting-select { flex-basis: auto; max-width: none; width: 100%; }
}
</style>
