<template>
  <div class="bottom-panel">
    <div class="bottom-tabs">
      <button
        v-for="item in tabs"
        :key="item.id"
        type="button"
        class="bottom-tab"
        :class="[item.id, { active: store.bottomTab === item.id }]"
        @click="store.setBottomTab(item.id)"
      >
        {{ item.title }}
      </button>
      <button type="button" class="bottom-close" @click="$emit('close')">×</button>
    </div>
    <div class="bottom-body">
      <TerminalPane v-if="store.bottomTab === 'terminal'" />
      <div v-else class="bottom-placeholder">{{ placeholder }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';

import TerminalPane from './TerminalPane.vue';
import { useAppStore } from '../stores/appStore';
import { useIdeStore } from '../stores/ideStore';

defineEmits<{ (e: 'close'): void }>();

const store = useIdeStore();
const appStore = useAppStore();

onMounted(() => {
  // TerminalPane 的启动条件是 bridgeReady && terminalPanelOpen，IDE 模式下必须显式打开
  if (!appStore.terminalPanelOpen) appStore.terminalPanelOpen = true;
});

const tabs = [
  { id: 'terminal', title: '终端' },
  { id: 'problems', title: '问题' },
  { id: 'output', title: '输出' },
  { id: 'debug', title: '调试控制台' },
];

const placeholder = computed(() => {
  if (store.bottomTab === 'problems') return '语言诊断将在 S2 接入后显示';
  if (store.bottomTab === 'output') return '输出通道将在 S2 接入后显示';
  return '调试控制台将在 S3 接入后显示';
});
</script>

<style scoped>
.bottom-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--bg1);
  border-top: 1px solid var(--border);
}

.bottom-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 6px;
  border-bottom: 1px solid var(--border);
}

.bottom-tab {
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 12px;
  padding: 3px 8px;
  border-radius: 6px;
  cursor: pointer;
}

.bottom-tab:hover {
  background: var(--bg2);
  color: var(--text);
}

.bottom-tab.active {
  background: var(--bg3);
  color: var(--text-bright, var(--text));
}

.bottom-close {
  margin-left: auto;
  background: transparent;
  border: none;
  color: var(--text-faint);
  cursor: pointer;
}

.bottom-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.bottom-placeholder {
  padding: 12px;
  font-size: 12px;
  color: var(--text-faint);
}
</style>
