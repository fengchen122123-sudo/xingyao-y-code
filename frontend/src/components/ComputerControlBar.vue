<template>
  <div
    v-if="store.computerControlBarVisible"
    class="computer-control-bar"
    data-test="computer-control-bar"
    data-theme-region="conversation_toolbar"
  >
    <span class="ccb-dot" :class="dotClass" data-test="computer-status-dot"></span>
    <span class="ccb-status" data-test="computer-status-text">{{ statusText }}</span>
    <span
      v-if="isCrossSession"
      class="ccb-owner"
      data-test="computer-owner-banner"
    >会话「{{ store.computerRun.owner_session_id }}」操控中</span>
    <span v-if="targetLabel" class="ccb-target" data-test="computer-target">{{ targetLabel }}</span>
    <span v-if="!isCrossSession" class="ccb-meta ccb-secondary" data-test="computer-action-count">{{ store.computerRun.action_count }} 个动作</span>
    <span v-if="!isCrossSession && currentActionLabel" class="ccb-meta ccb-secondary" data-test="computer-current-action">{{ currentActionLabel }}</span>
    <span
      v-if="!isCrossSession && store.computerRun.current_surface"
      class="ccb-meta ccb-surface ccb-secondary"
      data-test="computer-surface"
      :title="store.computerRun.current_surface"
    >{{ store.computerRun.current_surface }}</span>
    <span class="ccb-spacer"></span>
    <span class="ccb-actions">
      <button
        v-if="!isCrossSession && store.computerControlPausable !== 'none'"
        class="chip-btn ccb-pause"
        data-test="computer-pause"
        :disabled="store.computerControlMutating"
        :title="store.computerControlPausable === 'pause' ? '暂停：阻止下一个动作开始' : '恢复当前操控'"
        @click="togglePause"
      >
        <AppIcon :name="store.computerControlPausable === 'pause' ? 'pause' : 'play'" :size="12" :stroke-width="2" />
        {{ store.computerControlPausable === 'pause' ? '暂停' : '恢复' }}
      </button>
      <button
        class="chip-btn danger ccb-stop"
        data-test="computer-stop"
        :disabled="store.computerControlMutating"
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

const ACTION_VERBS: Record<string, string> = {
  observe: '观察',
  click: '点击',
  double_click: '双击',
  move: '移动',
  drag: '拖拽',
  scroll: '滚动',
  type_text: '输入',
  keypress: '按键',
  wait: '等待',
  wait_for: '等待条件',
  navigate: '打开网址',
  back: '后退',
  forward: '前进',
  reload: '刷新',
  request_user_takeover: '请求接管',
  list_surfaces: '列出窗口',
  activate_surface: '切换窗口',
  invoke_surface: '唤醒窗口',
  observe_region: '局部观察',
  sequence: '连续操作',
  list_elements: '列出控件',
  click_element: '点击控件',
  set_value_element: '填写控件',
};
const currentActionLabel = computed(() => {
  if (store.computerRun.status !== 'running') return '';
  const verb = ACTION_VERBS[String(store.computerRun.current_action || '')];
  return verb ? `正在${verb}` : '';
});

const isCrossSession = computed(() => {
  const owner = store.computerRun.owner_session_id;
  return !!owner && owner !== store.currentSessionName;
});

const targetLabel = computed(() => {
  const run = store.computerRun;
  if (run.target === 'desktop') return '桌面';
  const browser = String(run.browser || '').toLowerCase();
  if (browser === 'edge') return 'Edge';
  if (browser === 'chrome') return 'Chrome';
  if (browser === 'embedded') return '内置浏览器';
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
.ccb-owner {
  font-size: 12px;
  color: var(--yellow);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 240px;
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
