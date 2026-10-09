<template>
  <div class="debug-toolbar" data-test="debug-toolbar">
    <button
      class="dt-btn"
      type="button"
      title="运行当前文件（输出在底部终端）"
      data-test="debug-run"
      :disabled="!hasActiveFile"
      @click="runActiveFile"
    >
      <AppIcon name="play" :size="13" />
      <span>运行</span>
    </button>

    <span class="dt-sep"></span>

    <button
      class="dt-btn"
      type="button"
      title="启动调试"
      data-test="debug-start"
      :disabled="startDisabled"
      @click="startDebugging"
    >
      <AppIcon name="chevron-right" :size="13" />
      <span>调试</span>
    </button>

    <template v-if="debug.active">
      <button
        v-if="debug.paused"
        class="dt-btn"
        type="button"
        title="继续"
        data-test="debug-continue"
        @click="command('continue')"
      >
        <AppIcon name="play" :size="13" />
        <span>继续</span>
      </button>
      <button
        v-else
        class="dt-btn"
        type="button"
        title="暂停"
        data-test="debug-pause"
        @click="pauseDebugging"
      >
        <AppIcon name="pause" :size="13" />
        <span>暂停</span>
      </button>

      <button
        v-if="debug.paused"
        class="dt-btn"
        type="button"
        title="单步跳过"
        data-test="debug-step-over"
        @click="command('next')"
      >
        <AppIcon name="chevron-right" :size="13" />
        <span>跳过</span>
      </button>
      <button
        v-if="debug.paused"
        class="dt-btn"
        type="button"
        title="单步进入"
        data-test="debug-step-in"
        @click="command('stepIn')"
      >
        <AppIcon name="chevron-down" :size="13" />
        <span>步入</span>
      </button>
      <button
        v-if="debug.paused"
        class="dt-btn"
        type="button"
        title="单步跳出"
        data-test="debug-step-out"
        @click="command('stepOut')"
      >
        <AppIcon name="chevron-up" :size="13" />
        <span>步出</span>
      </button>

      <button
        class="dt-btn"
        type="button"
        title="重启调试"
        data-test="debug-restart"
        @click="command('restart')"
      >
        <AppIcon name="refresh" :size="13" />
        <span>重启</span>
      </button>
      <button
        class="dt-btn dt-stop"
        type="button"
        title="停止调试"
        data-test="debug-stop"
        @click="debug.stopDebug()"
      >
        <AppIcon name="square" :size="13" />
        <span>停止</span>
      </button>
    </template>

    <span class="dt-spacer"></span>
    <span class="dt-state" data-test="debug-state" :class="{ paused: debug.paused }">{{ stateLabel }}</span>
    <span
      v-if="debug.lastError"
      class="dt-error"
      data-test="debug-error"
      :title="debug.lastError"
    >{{ debug.lastError }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import AppIcon from './AppIcon.vue';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { useIdeStore } from '../stores/ideStore';

const ide = useIdeStore();
const debug = useIdeDebugStore();

const hasActiveFile = computed(() => Boolean(ide.activeTab?.path));

// 启动中（sessionId 还没回填、active 仍是 false）不能重复点「调试」：否则会撞上
// 后端「已有调试会话在运行」的判重，用户看到的却是一句莫名其妙的失败
const startDisabled = computed(
  () => !hasActiveFile.value || debug.active || debug.debugState === 'starting',
);

// 状态文案：不可用状态靠 disabled + 视觉弱化区分，状态本身让用户一眼看出现在能做什么
const stateLabel = computed(() => {
  switch (debug.debugState) {
    case 'starting':
      return '启动中…';
    case 'running':
      return debug.adapter ? `运行中（${debug.adapter}）` : '运行中';
    case 'paused':
      return debug.stopLine > 0 ? `已暂停：第 ${debug.stopLine} 行` : '已暂停';
    case 'stopped':
      return '已停止';
    default:
      return '未启动';
  }
});

function revealTerminal() {
  // 输出落在底部终端页签：先把面板切过去再发命令，否则用户看不到输出
  ide.setBottomTab('terminal');
  if (!ide.bottomPanelOpen) ide.toggleBottomPanel();
}

async function runActiveFile() {
  const path = ide.activeTab?.path;
  if (!path) return;
  revealTerminal();
  await debug.runFile(path);
}

async function startDebugging() {
  const path = ide.activeTab?.path;
  if (!path) return;
  await debug.startDebug(path);
}

async function command(name: string) {
  // 桥方法失败会写进 debug.lastError，工具条右侧的 dt-error 负责让失败可见（不静默）
  await debug.sendCommand(name);
}

async function pauseDebugging() {
  // 运行中还没有 dap_stopped 记录：store 会先取线程表拿到真实线程号再发暂停
  await debug.pauseDebug();
}
</script>

<style scoped>
.debug-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg0);
  font-size: 11.5px;
  color: var(--text-dim);
  user-select: none;
}
.dt-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 7px;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-dim);
  font-size: inherit;
  cursor: pointer;
}
.dt-btn:hover:not(:disabled) {
  background: var(--bg2);
  color: var(--text);
}
.dt-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.dt-stop:hover {
  color: var(--red, #e5534b);
}
.dt-sep {
  width: 1px;
  height: 14px;
  margin: 0 3px;
  background: var(--border);
}
.dt-spacer {
  flex: 1;
}
.dt-state {
  flex-shrink: 0;
  color: var(--text-dim);
}
.dt-state.paused {
  color: var(--orange);
}
.dt-error {
  max-width: 46%;
  margin-left: 8px;
  overflow: hidden;
  color: var(--orange);
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
