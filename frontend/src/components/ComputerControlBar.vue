<template>
  <!-- 电脑操控安全条（设计 16.1）：插在聊天标题栏下方、滚动区外——
       对话滚动时保持可见；只对活动期 run 渲染（idle/stopped/completed/completing 不显示） -->
  <div
    v-if="store.computerControlBarVisible"
    class="computer-control-bar"
    data-test="computer-control-bar"
    data-theme-region="conversation_toolbar"
  >
    <span class="ccb-dot" :class="dotClass" data-test="computer-status-dot"></span>
    <span class="ccb-status" data-test="computer-status-text">{{ statusText }}</span>
    <span class="ccb-target" data-test="computer-target">{{ targetLabel }}</span>
    <span class="ccb-meta ccb-secondary" data-test="computer-action-count">{{ store.computerRun.action_count }} 个动作</span>
    <span
      v-if="store.computerRun.current_surface"
      class="ccb-meta ccb-surface ccb-secondary"
      data-test="computer-surface"
      :title="store.computerRun.current_surface"
    >{{ store.computerRun.current_surface }}</span>
    <span class="ccb-spacer"></span>
    <span class="ccb-actions">
      <button
        v-if="store.computerControlPausable !== 'none'"
        class="chip-btn ccb-pause"
        data-test="computer-pause"
        :title="store.computerControlPausable === 'pause' ? '暂停：阻止下一个动作开始' : '恢复当前操控'"
        @click="togglePause"
      >
        <AppIcon :name="store.computerControlPausable === 'pause' ? 'pause' : 'play'" :size="12" :stroke-width="2" />
        {{ store.computerControlPausable === 'pause' ? '暂停' : '恢复' }}
      </button>
      <button
        class="chip-btn danger ccb-stop"
        data-test="computer-stop"
        title="立即停止当前操控（快捷键 Ctrl+Alt+Esc）"
        @click="stop"
      >
        <AppIcon name="square" :size="12" :stroke-width="2" /> 立即停止
      </button>
    </span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const store = useAppStore();

// 状态点颜色：运行绿 / 等待类黄 / 失败红
const dotClass = computed(() => {
  const status = store.computerRun.status;
  if (status === 'running' || status === 'starting') return 'ok';
  if (status === 'failed') return 'bad';
  return 'warn';
});

const STATUS_TEXTS: Record<string, string> = {
  starting: '正在启动操控',
  running: '正在操控',
  paused: '已暂停',
  waiting_for_user: '等待用户',
  provider_safety_required: '等待安全确认',
  stopping: '正在停止',
  failed: '操控失败',
};

const statusText = computed(() => STATUS_TEXTS[store.computerRun.status] || '电脑操控');

// 操控目标标识：Edge / Chrome / 桌面（设计 16.1）
const targetLabel = computed(() => {
  const run = store.computerRun;
  if (run.target === 'desktop') return '桌面';
  const browser = String(run.browser || '').toLowerCase();
  if (browser === 'edge') return 'Edge';
  if (browser === 'chrome') return 'Chrome';
  return run.target === 'browser' ? '浏览器' : '';
});

async function togglePause() {
  if (store.computerControlPausable === 'resume') {
    await store.resumeComputerControl();
  } else if (store.computerControlPausable === 'pause') {
    await store.pauseComputerControl();
  }
}

async function stop() {
  await store.stopComputerControl();
}
</script>

<style scoped>
.computer-control-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
  padding: 6px 16px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  font-size: 12.5px;
  color: var(--text-dim);
  overflow: hidden;
}
.ccb-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--yellow);
}
.ccb-dot.ok {
  background: var(--green);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 22%, transparent);
}
.ccb-dot.warn {
  background: var(--yellow);
}
.ccb-dot.bad {
  background: var(--red);
}
.ccb-status {
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
}
.ccb-target {
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: var(--theme-radius-small);
  background: var(--bg3);
  color: var(--text);
  white-space: nowrap;
  font-size: 11.5px;
}
.ccb-meta {
  white-space: nowrap;
}
.ccb-surface {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  font-family: var(--mono);
  font-size: 11.5px;
}
.ccb-spacer {
  flex: 1;
  min-width: 0;
}
.ccb-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
.ccb-stop {
  font-weight: 600;
}

/* 窄屏：次要文字（动作数/当前窗口）折叠，暂停/恢复与立即停止永不隐藏 */
@media (max-width: 900px) {
  .ccb-secondary {
    display: none;
  }
}
@media (max-width: 560px) {
  .ccb-status {
    font-size: 12px;
  }
  .computer-control-bar {
    gap: 8px;
    padding: 5px 10px;
  }
}
</style>
