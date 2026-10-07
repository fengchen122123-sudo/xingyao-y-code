<template>
  <div class="bottom-panel">
    <div class="bottom-tabs">
      <button
        v-for="item in tabs"
        :key="item.id"
        type="button"
        class="bottom-tab"
        :class="[item.id, { active: store.bottomTab === item.id }]"
        :data-test="`bottom-tab-${item.id}`"
        @click="store.setBottomTab(item.id)"
      >
        {{ item.title }}
      </button>
      <button type="button" class="bottom-close" @click="$emit('close')">×</button>
    </div>
    <div class="bottom-body">
      <!-- 终端与调试控制台都用 v-show 常驻（S3-T12）：切页签只是换显示，组件不销毁，
           调试控制台的行缓冲与终端现场都保得住。占位文案只服务尚未接入的「问题 / 输出」。 -->
      <TerminalPane v-show="store.bottomTab === 'terminal'" />
      <DebugPane v-show="store.bottomTab === 'debug'" />
      <div
        v-if="store.bottomTab === 'problems' || store.bottomTab === 'output'"
        class="bottom-placeholder"
      >
        {{ placeholder }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue';

import DebugPane from './DebugPane.vue';
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

// 页签表：计划 Task 12 里写作 `IDE_BOTTOM_TABS` / `{ id, label }`，本文件才是 S1 的
// 真实交付物（`tabs` / `{ id, title }`）。命名按真实文件保留不动，只新增 debug 一行；
// 「调试控制台」的文案与规格 5.1 一致。
const tabs = [
  { id: 'terminal', title: '终端' },
  { id: 'problems', title: '问题' },
  { id: 'output', title: '输出' },
  { id: 'debug', title: '调试控制台' },
];

// 只服务尚未接入的「问题 / 输出」页签：debug 页签已由 DebugPane 接管
const placeholder = computed(() => {
  if (store.bottomTab === 'problems') return '语言诊断将在 S2 接入后显示';
  if (store.bottomTab === 'output') return '输出通道将在 S2 接入后显示';
  return '';
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
