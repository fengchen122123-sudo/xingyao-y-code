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

    <section class="system-card">
      <div class="setting-heading">
        <div>
          <h2>默认命令解释器</h2>
          <p>AI 命令、后台任务、计划验证和右侧终端统一优先使用所选解释器。</p>
        </div>
      </div>

      <div class="shell-options">
        <label
          v-for="option in store.systemSettings.command_shell_options"
          :key="option.id"
          :data-test="`shell-${option.id}`"
          class="shell-option"
          :class="{ unavailable: !option.available, selected: option.id === store.systemSettings.command_shell }"
        >
          <input
            type="radio"
            name="command-shell"
            :value="option.id"
            :checked="option.id === store.systemSettings.command_shell"
            :disabled="!option.available || saving"
            @change="selectShell(option.id)"
          />
          <span class="shell-copy">
            <span class="shell-title">
              {{ option.label }}
              <span v-if="!option.available" class="unavailable-badge">{{ option.unavailable_reason || '未安装' }}</span>
            </span>
            <span class="shell-description">{{ option.description }}</span>
            <span v-if="option.executable" class="shell-path">{{ option.executable }}</span>
          </span>
        </label>
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
.system-card { border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); padding: 24px 26px; }
.setting-heading h2 { margin: 0 0 7px; font-size: 15px; }
.setting-heading p { margin: 0; color: var(--text-faint); font-size: 12.5px; line-height: 1.6; }
.shell-options { display: grid; gap: 10px; margin-top: 20px; }
.shell-option { display: flex; align-items: flex-start; gap: 12px; padding: 14px 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--bg2); cursor: pointer; }
.shell-option:hover:not(.unavailable) { border-color: var(--accent); }
.shell-option.selected { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, var(--bg2)); }
.shell-option.unavailable { opacity: .58; cursor: not-allowed; }
.shell-option input { margin-top: 3px; accent-color: var(--accent); }
.shell-copy { min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.shell-title { display: flex; align-items: center; gap: 8px; font-size: 13.5px; font-weight: 600; }
.shell-description { color: var(--text-faint); font-size: 12px; }
.shell-path { color: var(--text-dim); font: 11px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; overflow-wrap: anywhere; }
.unavailable-badge { padding: 2px 6px; border-radius: 999px; background: var(--bg3); color: var(--text-faint); font-size: 10px; font-weight: 500; }
.setting-error { margin: 16px 0 0; color: var(--danger); font-size: 12px; }
.setting-note { margin: 18px 0 0; padding-top: 16px; border-top: 1px solid var(--border); color: var(--text-faint); font-size: 12px; line-height: 1.7; }
</style>
